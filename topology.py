"""
Fat-Tree (k=4) topology construction for Mininet.

This module focuses purely on building and tearing down the topology:
- LinuxRouter node definition
- Address helpers
- Node/link creation
- Sysctl tuning and interface prep
- IP assignment and static ECMP routing
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import Node

__all__ = [
    'LinuxRouter',
    'FatTreeContext',
    'ip_core_agg',
    'ip_agg_edge',
    'svi_ip',
    'host_ip',
    'net_24',
    'create_nodes_and_links',
    'tune_sysctls',
    'setup_edge_tor',
    'disable_offloads',
    'assign_addresses',
    'install_routes_ecmp',
    'build_fattree_topology',
    'stop_fattree_topology',
    'flatten',
]


class LinuxRouter(Node):
    def config(self, **params):
        super(LinuxRouter, self).config(**params)
        # Enable IPv4 forwarding
        self.cmd('sysctl -w net.ipv4.ip_forward=1')
        # Reduce ARP flux side-effects if we reuse the same SVI IP on two host-facing ifaces
        self.cmd('sysctl -w net.ipv4.conf.all.arp_ignore=1')
        self.cmd('sysctl -w net.ipv4.conf.all.arp_announce=2')

    def terminate(self):
        self.cmd('sysctl -w net.ipv4.ip_forward=0')
        super(LinuxRouter, self).terminate()


def ip_core_agg(p: int, a: int, c: int) -> Tuple[str, str]:
    """Return (agg_ip/31, core_ip/31) tuple for Core–Agg link."""
    second = 32 + p
    base = 2 * a
    core = f'172.{second}.{c}.{base+1}/31'
    agg = f'172.{second}.{c}.{base}/31'
    return agg, core


def ip_agg_edge(p: int, a: int, e: int) -> Tuple[str, str]:
    """Return (edge_ip/31, agg_ip/31) tuple for Agg–Edge link."""
    second = 16 + p
    base = 2 * e
    agg = f'172.{second}.{a}.{base+1}/31'
    edge = f'172.{second}.{a}.{base}/31'
    return edge, agg


def svi_ip(p: int, e: int) -> str:
    return f'10.{p}.{e}.254/24'


def host_ip(p: int, e: int, hidx: int) -> str:
    # hidx ∈ {0,1} → hosts: .1, .2
    host = 1 + hidx
    return f'10.{p}.{e}.{host}/24'


def net_24(p: int, e: int) -> str:
    return f'10.{p}.{e}.0/24'


def flatten(nested):
    """Recursively flatten nested lists/tuples."""
    result = []
    for item in nested:
        if isinstance(item, (list, tuple)):
            result.extend(flatten(item))
        else:
            result.append(item)
    return result


@dataclass
class FatTreeContext:
    """Container for the Fat-Tree Mininet objects and configuration."""

    net: Mininet
    cores: List[Node]
    aggs: List[List[Node]]
    edges: List[List[Node]]
    hosts: List[List[List[Node]]]
    link_params: Dict[str, object]

    def routers(self) -> List[Node]:
        """Return a flat list of all router-class nodes (core/agg/edge)."""
        return self.cores + flatten(self.aggs) + flatten(self.edges)


def create_nodes_and_links(net: Mininet, link_params: Dict[str, object]):
    """Create routers, hosts, and interconnect them."""
    cores = [net.addHost(f'c{c}', cls=LinuxRouter, ip=None) for c in range(4)]
    aggs = [[net.addHost(f'a{p}{a}', cls=LinuxRouter, ip=None) for a in range(2)] for p in range(4)]
    edges = [[net.addHost(f'e{p}{e}', cls=LinuxRouter, ip=None) for e in range(2)] for p in range(4)]
    hosts = [[[
        net.addHost(f'h{p}{e}{h}', ip=None)
        for h in range(2)
    ] for e in range(2)] for p in range(4)]

    # Edge–Host links
    for p in range(4):
        for e in range(2):
            for h in range(2):
                net.addLink(
                    edges[p][e],
                    hosts[p][e][h],
                    intfName1=f'e{p}{e}-h{h}',
                    intfName2=f'h{p}{e}{h}-eth0',
                    **link_params,
                )

    # Agg–Edge links (within pod)
    for p in range(4):
        for a in range(2):
            for e in range(2):
                net.addLink(
                    aggs[p][a],
                    edges[p][e],
                    intfName1=f'a{p}{a}-to-e{p}{e}',
                    intfName2=f'e{p}{e}-to-a{p}{a}',
                    **link_params,
                )

    # Core–Agg links (cross-pod)
    for p in range(4):
        net.addLink(
            cores[0],
            aggs[p][0],
            intfName1=f'c0-to-a{p}0',
            intfName2=f'a{p}0-to-c0',
            **link_params,
        )
        net.addLink(
            cores[1],
            aggs[p][0],
            intfName1=f'c1-to-a{p}0',
            intfName2=f'a{p}0-to-c1',
            **link_params,
        )
        net.addLink(
            cores[2],
            aggs[p][1],
            intfName1=f'c2-to-a{p}1',
            intfName2=f'a{p}1-to-c2',
            **link_params,
        )
        net.addLink(
            cores[3],
            aggs[p][1],
            intfName1=f'c3-to-a{p}1',
            intfName2=f'a{p}1-to-c3',
            **link_params,
        )

    return cores, aggs, edges, hosts


def tune_sysctls(routers: List[Node]) -> None:
    """Configure sysctl knobs required for ECMP to work as expected."""
    for router in routers:
        router.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        router.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        # Encourage multipath hashing to use L3+L4 fields where supported
        router.cmd('sysctl -w net.ipv4.fib_multipath_hash_policy=1')


def setup_edge_tor(edges: List[List[Node]]) -> None:
    """Turn Edge nodes into ToR switches with a bridge SVI and downlink ports."""
    for p, pod_edges in enumerate(edges):
        for e, edge_node in enumerate(pod_edges):
            bridge = f'br_e{p}{e}'
            edge_node.cmd(f'ip link add {bridge} type bridge')
            edge_node.cmd(f'ip link set {bridge} up')
            for h in range(2):
                down_if = f'e{p}{e}-h{h}'
                edge_node.cmd(f'ip link set {down_if} up')
                edge_node.cmd(f'ip link set {down_if} master {bridge}')
            edge_node.cmd('sysctl -w net.ipv4.ip_forward=1')
            edge_node.cmd('sysctl -w net.bridge.bridge-nf-call-iptables=0')
            edge_node.cmd('sysctl -w net.bridge.bridge-nf-call-ip6tables=0')


def disable_offloads(nodes: List[Node]) -> None:
    """Disable GRO/GSO/TSO/LRO on all non-loopback interfaces for the given nodes."""
    for node in nodes:
        for intf in node.intfList():
            if not intf or not intf.name or intf.name == 'lo':
                continue
            node.cmd(
                f'ethtool -K {intf.name} gro off gso off tso off lro off || true'
            )


def assign_addresses(cores, aggs, edges, hosts) -> None:
    """Assign IP addresses to interconnect links and host interfaces."""
    # Host /24 addresses and default routes
    for p in range(4):
        for e in range(2):
            bridge = f'br_e{p}{e}'
            edge_node = edges[p][e]
            edge_node.cmd(f'ip addr replace {svi_ip(p, e)} dev {bridge}')
            gateway = svi_ip(p, e).split('/')[0]
            for h in range(2):
                iface = f'h{p}{e}{h}-eth0'
                h_node = hosts[p][e][h]
                h_node.setIP(intf=iface, ip=host_ip(p, e, h))
                h_node.setDefaultRoute(f'via {gateway}')

    # Agg–Edge /31 links
    for p in range(4):
        for a in range(2):
            for e in range(2):
                a_node = aggs[p][a]
                e_node = edges[p][e]
                a_if = a_node.intf(f'a{p}{a}-to-e{p}{e}')
                e_if = e_node.intf(f'e{p}{e}-to-a{p}{a}')
                edge_ip, agg_ip = ip_agg_edge(p, a, e)
                e_node.setIP(intf=e_if, ip=edge_ip)
                a_node.setIP(intf=a_if, ip=agg_ip)

    # Core–Agg /31 links
    for p in range(4):
        for c in (0, 1):
            c_node = cores[c]
            a_node = aggs[p][0]
            c_if = c_node.intf(f'c{c}-to-a{p}0')
            a_if = a_node.intf(f'a{p}0-to-c{c}')
            agg_ip, core_ip = ip_core_agg(p, 0, c)
            a_node.setIP(intf=a_if, ip=agg_ip)
            c_node.setIP(intf=c_if, ip=core_ip)
        for c in (2, 3):
            c_node = cores[c]
            a_node = aggs[p][1]
            c_if = c_node.intf(f'c{c}-to-a{p}1')
            a_if = a_node.intf(f'a{p}1-to-c{c}')
            agg_ip, core_ip = ip_core_agg(p, 1, c)
            a_node.setIP(intf=a_if, ip=agg_ip)
            c_node.setIP(intf=c_if, ip=core_ip)


def install_routes_ecmp(cores, aggs, edges) -> None:
    """Install static routes implementing the ECMP policy."""
    # Edge default route via both Aggs
    for p in range(4):
        for e in range(2):
            e_node = edges[p][e]
            _, agg_ip_a0 = ip_agg_edge(p, 0, e)
            _, agg_ip_a1 = ip_agg_edge(p, 1, e)
            dev_a0 = e_node.intf(f'e{p}{e}-to-a{p}0').name
            dev_a1 = e_node.intf(f'e{p}{e}-to-a{p}1').name
            e_node.cmd(
                'ip route replace default scope global '
                f'nexthop via {agg_ip_a0.split("/")[0]} dev {dev_a0} weight 1 '
                f'nexthop via {agg_ip_a1.split("/")[0]} dev {dev_a1} weight 1'
            )

    # Agg routes to pod-local subnets and defaults via cores
    for p in range(4):
        for a in range(2):
            a_node = aggs[p][a]
            e0_ip, _ = ip_agg_edge(p, a, 0)
            e1_ip, _ = ip_agg_edge(p, a, 1)
            dev_e0 = a_node.intf(f'a{p}{a}-to-e{p}0').name
            dev_e1 = a_node.intf(f'a{p}{a}-to-e{p}1').name
            edge_routes = [
                (e0_ip.split('/')[0], dev_e0),
                (e1_ip.split('/')[0], dev_e1),
            ]
            for e_sub in (0, 1):
                subnet = net_24(p, e_sub)
                nh_ip, dev = edge_routes[e_sub]
                a_node.cmd(
                    f"ip route replace {subnet} scope global via {nh_ip} dev {dev}"
                )

            core_indices = (0, 1) if a == 0 else (2, 3)
            nexthops = []
            for c in core_indices:
                agg_ip, core_ip = ip_core_agg(p, a, c)
                dev_c = a_node.intf(f'a{p}{a}-to-c{c}').name
                nexthops.append(f"nexthop via {core_ip.split('/') [0]} dev {dev_c} weight 1")
            a_node.cmd('ip route replace default ' + ' '.join(nexthops))

    # Core routes to every rack via appropriate Agg
    for c in range(4):
        c_node = cores[c]
        a_grp = 0 if c in (0, 1) else 1
        for p in range(4):
            agg_ip, _ = ip_core_agg(p, a_grp, c)
            dev = c_node.intf(f'c{c}-to-a{p}{a_grp}').name
            for e in range(2):
                subnet = net_24(p, e)
                c_node.cmd(f"ip route replace {subnet} via {agg_ip.split('/') [0]} dev {dev}")


def build_fattree_topology(
    bw_mbps: int = 1000,
    delay: str = '0.2ms',
    queue_pkts: int = 150,
    start: bool = True,
) -> FatTreeContext:
    """Construct the k=4 Fat-Tree with the specified TCLink parameters."""
    link_params = dict(cls=TCLink, bw=bw_mbps, delay=delay, max_queue_size=queue_pkts, use_htb=True)
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = create_nodes_and_links(net, link_params)
    net.build()

    ctx = FatTreeContext(
        net=net,
        cores=cores,
        aggs=aggs,
        edges=edges,
        hosts=hosts,
        link_params=link_params,
    )

    tune_sysctls(ctx.routers())
    setup_edge_tor(ctx.edges)
    disable_offloads(ctx.routers() + flatten(ctx.hosts))
    assign_addresses(ctx.cores, ctx.aggs, ctx.edges, ctx.hosts)
    install_routes_ecmp(ctx.cores, ctx.aggs, ctx.edges)
    if start:
        net.start()
    return ctx


def stop_fattree_topology(ctx: Optional[FatTreeContext]) -> None:
    """Stop and clean up the Mininet topology."""
    if ctx and ctx.net:
        ctx.net.stop()
