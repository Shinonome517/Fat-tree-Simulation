"""
Fat-Tree topology construction for Mininet (even k, default 4).

This module focuses purely on building and tearing down the topology:
- LinuxRouter node definition
- Address helpers
- Node/link creation
- Sysctl tuning and interface prep
- IP assignment and static ECMP routing
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from mininet.log import info
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
    'host_ips',
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

PROGRESS_STEP = 25


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
    """
    Return the primary /24 IP address for host h{p}{e}{hidx}.

    Each host is allocated a block of 4 consecutive addresses inside the
    rack's /24 subnet 10.p.e.0/24.

    Example (p=0, e=0):
      h000 (hidx=0) -> 10.0.0.1-10.0.0.4, primary = 10.0.0.1/24
      h001 (hidx=1) -> 10.0.0.5-10.0.0.8, primary = 10.0.0.5/24
    """
    base = 1 + 4 * hidx
    return f"10.{p}.{e}.{base}/24"


def host_ips(p: int, e: int, hidx: int) -> List[str]:
    """
    Return all /24 IP addresses assigned to host h{p}{e}{hidx}.

    Each host gets 4 consecutive addresses inside 10.p.e.0/24:
      h000 -> 10.p.e.1-10.p.e.4
      h001 -> 10.p.e.5-10.p.e.8
      ...
    """
    base = 1 + 4 * hidx
    return [f"10.{p}.{e}.{base + i}/24" for i in range(4)]


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


def _progress(count: int, total: int) -> None:
    """Emit progress as [done/total] every PROGRESS_STEP items (and at the end), with newline."""
    if count % PROGRESS_STEP == 0 or count == total:
        info(f'[{count}/{total}]\n')


def _fattree_dims(k: int):
    """
    Validate k and return the derived fat-tree dimensions.

    Returns tuple:
    (n_pods, n_agg_per_pod, n_edge_per_pod, n_hosts_per_edge, n_core_groups, n_core_per_group, n_cores)
    """
    assert k % 2 == 0, "k must be even"
    assert 2 <= k <= 16, "k must satisfy 2 <= k <= 16"

    n_pods = k
    n_agg_per_pod = k // 2
    n_edge_per_pod = k // 2
    n_hosts_per_edge = k // 2

    n_core_groups = k // 2
    n_core_per_group = k // 2
    n_cores = n_core_groups * n_core_per_group
    return (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        n_hosts_per_edge,
        n_core_groups,
        n_core_per_group,
        n_cores,
    )


@dataclass
class FatTreeContext:
    """Container for the Fat-Tree Mininet objects and configuration."""

    net: Mininet
    cores: List[Node]
    aggs: List[List[Node]]
    edges: List[List[Node]]
    hosts: List[List[List[Node]]]
    k: int
    link_params: Dict[str, object]

    def routers(self) -> List[Node]:
        """Return a flat list of all router-class nodes (core/agg/edge)."""
        return self.cores + flatten(self.aggs) + flatten(self.edges)


def create_nodes_and_links(
    net: Mininet,
    link_params: Optional[Dict[str, object]] = None,
    k: int = 4,
):
    """Create routers, hosts, and interconnect them."""
    (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        n_hosts_per_edge,
        n_core_groups,
        n_core_per_group,
        n_cores,
    ) = _fattree_dims(k)
    link_params = link_params or {}

    cores: List[Node] = []
    info(f'*** Creating {n_cores} core routers (progress every {PROGRESS_STEP}, format [done/total])\n')
    for c in range(n_cores):
        cores.append(net.addHost(f'c{c}', cls=LinuxRouter, ip=None))
        _progress(c + 1, n_cores)
    info('\n')

    aggs: List[List[Node]] = []
    total_aggs = n_pods * n_agg_per_pod
    agg_count = 0
    info(f'*** Creating {total_aggs} aggregation routers (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        pod_aggs = []
        for a in range(n_agg_per_pod):
            pod_aggs.append(net.addHost(f'a{p}{a}', cls=LinuxRouter, ip=None))
            agg_count += 1
            _progress(agg_count, total_aggs)
        aggs.append(pod_aggs)
    info('\n')

    edges: List[List[Node]] = []
    total_edges = n_pods * n_edge_per_pod
    edge_count = 0
    info(f'*** Creating {total_edges} edge routers (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        pod_edges = []
        for e in range(n_edge_per_pod):
            pod_edges.append(net.addHost(f'e{p}{e}', cls=LinuxRouter, ip=None))
            edge_count += 1
            _progress(edge_count, total_edges)
        edges.append(pod_edges)
    info('\n')

    hosts: List[List[List[Node]]] = []
    total_hosts = n_pods * n_edge_per_pod * n_hosts_per_edge
    host_count = 0
    info(f'*** Creating {total_hosts} hosts (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        pod_hosts = []
        for e in range(n_edge_per_pod):
            edge_hosts = []
            for h in range(n_hosts_per_edge):
                edge_hosts.append(net.addHost(f'h{p}{e}{h}', ip=None))
                host_count += 1
                _progress(host_count, total_hosts)
            pod_hosts.append(edge_hosts)
        hosts.append(pod_hosts)
    info('\n')

    # Edge–Host links
    total_edge_host_links = n_pods * n_edge_per_pod * n_hosts_per_edge
    edge_host_link_count = 0
    info(f'*** Creating {total_edge_host_links} edge-host links (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        for e in range(n_edge_per_pod):
            for h in range(n_hosts_per_edge):
                net.addLink(
                    edges[p][e],
                    hosts[p][e][h],
                    intfName1=f'e{p}{e}-h{h}',
                    intfName2=f'h{p}{e}{h}-eth0',
                    **link_params,
                )
                edge_host_link_count += 1
                _progress(edge_host_link_count, total_edge_host_links)
    info('\n')

    # Agg–Edge links (within pod)
    total_agg_edge_links = n_pods * n_agg_per_pod * n_edge_per_pod
    agg_edge_link_count = 0
    info(f'*** Creating {total_agg_edge_links} agg-edge links (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for e in range(n_edge_per_pod):
                net.addLink(
                    aggs[p][a],
                    edges[p][e],
                    intfName1=f'a{p}{a}-to-e{p}{e}',
                    intfName2=f'e{p}{e}-to-a{p}{a}',
                    **link_params,
                )
                agg_edge_link_count += 1
                _progress(agg_edge_link_count, total_agg_edge_links)
    info('\n')

    # Core–Agg links (cross-pod)
    total_core_agg_links = n_pods * n_agg_per_pod * n_core_groups
    core_agg_link_count = 0
    info(f'*** Creating {total_core_agg_links} core-agg links (progress every {PROGRESS_STEP}, format [done/total])\n')
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for i in range(n_core_groups):
                c = i * n_core_per_group + a
                net.addLink(
                    cores[c],
                    aggs[p][a],
                    intfName1=f'c{c}-to-a{p}{a}',
                    intfName2=f'a{p}{a}-to-c{c}',
                    **link_params,
                )
                core_agg_link_count += 1
                _progress(core_agg_link_count, total_core_agg_links)
    info('\n')

    return cores, aggs, edges, hosts


def tune_sysctls(routers: List[Node]) -> None:
    """Configure sysctl knobs required for ECMP to work as expected."""
    for router in routers:
        router.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        router.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        # Encourage multipath hashing to use L3+L4 fields where supported
        router.cmd('sysctl -w net.ipv4.fib_multipath_hash_policy=1')


def setup_edge_tor(edges: List[List[Node]], k: int = 4) -> None:
    """Turn Edge nodes into ToR switches with a bridge SVI and downlink ports."""
    (_, _, _, n_hosts_per_edge, _, _, _,) = _fattree_dims(k)
    for p, pod_edges in enumerate(edges):
        for e, edge_node in enumerate(pod_edges):
            bridge = f'br_e{p}{e}'
            edge_node.cmd(f'ip link add {bridge} type bridge')
            edge_node.cmd(f'ip link set {bridge} up')
            for h in range(n_hosts_per_edge):
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


def assign_addresses(cores, aggs, edges, hosts, k: int = 4) -> None:
    """Assign IP addresses to interconnect links and host interfaces."""
    (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        n_hosts_per_edge,
        n_core_groups,
        n_core_per_group,
        _,
    ) = _fattree_dims(k)
    # Host /24 addresses and default routes
    for p in range(n_pods):
        for e in range(n_edge_per_pod):
            bridge = f'br_e{p}{e}'
            edge_node = edges[p][e]
            edge_node.cmd(f'ip addr replace {svi_ip(p, e)} dev {bridge}')
            gateway = svi_ip(p, e).split('/')[0]
            for h in range(n_hosts_per_edge):
                iface = f"h{p}{e}{h}-eth0"
                h_node = hosts[p][e][h]

                # Each host gets 4 IPs inside 10.p.e.0/24.
                ips = host_ips(p, e, h)

                # Primary IP via Mininet helper (also updates /proc etc.).
                h_node.setIP(intf=iface, ip=ips[0])

                # Remaining IPs as secondary addresses on the same iface.
                for extra_ip in ips[1:]:
                    h_node.cmd(f"ip addr add {extra_ip} dev {iface}")

                # Default route is unchanged: use the SVI as the gateway.
                h_node.setDefaultRoute(f"via {gateway}")

    # Agg–Edge /31 links
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for e in range(n_edge_per_pod):
                a_node = aggs[p][a]
                e_node = edges[p][e]
                a_if = a_node.intf(f'a{p}{a}-to-e{p}{e}')
                e_if = e_node.intf(f'e{p}{e}-to-a{p}{a}')
                edge_ip, agg_ip = ip_agg_edge(p, a, e)
                e_node.setIP(intf=e_if, ip=edge_ip)
                a_node.setIP(intf=a_if, ip=agg_ip)

    # Core–Agg /31 links
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for i in range(n_core_groups):
                c = i * n_core_per_group + a
                c_node = cores[c]
                a_node = aggs[p][a]
                c_if = c_node.intf(f'c{c}-to-a{p}{a}')
                a_if = a_node.intf(f'a{p}{a}-to-c{c}')
                agg_ip, core_ip = ip_core_agg(p, a, c)
                a_node.setIP(intf=a_if, ip=agg_ip)
                c_node.setIP(intf=c_if, ip=core_ip)


def install_routes_ecmp(cores, aggs, edges, k: int = 4) -> None:
    """Install static routes implementing the ECMP policy."""
    (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        _,
        n_core_groups,
        n_core_per_group,
        n_cores,
    ) = _fattree_dims(k)
    # Edge default route via all pod-local Aggs
    for p in range(n_pods):
        for e in range(n_edge_per_pod):
            e_node = edges[p][e]
            nexthops = []
            for a in range(n_agg_per_pod):
                _, agg_ip = ip_agg_edge(p, a, e)
                dev = e_node.intf(f'e{p}{e}-to-a{p}{a}').name
                nexthops.append(
                    f'nexthop via {agg_ip.split("/")[0]} dev {dev} weight 1'
                )
            e_node.cmd('ip route replace default scope global ' + ' '.join(nexthops))

    # Agg routes to pod-local subnets and defaults via cores
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            a_node = aggs[p][a]
            # Pod-local /24 networks via edges
            # Note: hosts may own multiple IPs within 10.p.e.0/24, but routing stays /24-based.
            for e in range(n_edge_per_pod):
                subnet = net_24(p, e)
                edge_ip, _ = ip_agg_edge(p, a, e)
                dev = a_node.intf(f'a{p}{a}-to-e{p}{e}').name
                a_node.cmd(
                    f"ip route replace {subnet} scope global via {edge_ip.split('/')[0]} dev {dev}"
                )

            # Default ECMP via all cores connected to this agg
            nexthops = []
            for i in range(n_core_groups):
                c = i * n_core_per_group + a
                agg_ip, core_ip = ip_core_agg(p, a, c)
                dev_c = a_node.intf(f'a{p}{a}-to-c{c}').name
                nexthops.append(
                    f"nexthop via {core_ip.split('/')[0]} dev {dev_c} weight 1"
                )
            a_node.cmd('ip route replace default ' + ' '.join(nexthops))

    # Core routes to every rack via appropriate Agg
    for c in range(n_cores):
        c_node = cores[c]
        a = c % n_core_per_group
        for p in range(n_pods):
            agg_ip, _ = ip_core_agg(p, a, c)
            dev = c_node.intf(f'c{c}-to-a{p}{a}').name
            for e in range(n_edge_per_pod):
                subnet = net_24(p, e)
                # Hosts have multiple IPs inside each rack /24; ECMP remains per /24 subnet.
                c_node.cmd(f"ip route replace {subnet} via {agg_ip.split('/')[0]} dev {dev}")


def build_fattree_topology(
    bw_mbps: int = 1000,
    delay: str = '0.2ms',
    queue_pkts: int = 150,
    start: bool = True,
    k: int = 4,
) -> FatTreeContext:
    """Construct a k-ary Fat-Tree with the specified TCLink parameters."""
    _fattree_dims(k)  # validates k early
    link_params = dict(cls=TCLink, bw=bw_mbps, delay=delay, max_queue_size=queue_pkts, use_htb=True)
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = create_nodes_and_links(net, link_params, k=k)
    net.build()

    ctx = FatTreeContext(
        net=net,
        cores=cores,
        aggs=aggs,
        edges=edges,
        hosts=hosts,
        k=k,
        link_params=link_params,
    )

    tune_sysctls(ctx.routers())
    setup_edge_tor(ctx.edges, k=k)
    disable_offloads(ctx.routers() + flatten(ctx.hosts))
    assign_addresses(ctx.cores, ctx.aggs, ctx.edges, ctx.hosts, k=k)
    install_routes_ecmp(ctx.cores, ctx.aggs, ctx.edges, k=k)
    if start:
        net.start()
    return ctx


def stop_fattree_topology(ctx: Optional[FatTreeContext]) -> None:
    """Stop and clean up the Mininet topology."""
    if ctx and ctx.net:
        ctx.net.stop()
