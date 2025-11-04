#!/usr/bin/env python3
"""
Mininet script: k=4 Fat-Tree with an underlay/host address plan suitable
for DC-style Clos fabrics and ECMP testing. Addressing is as follows:

- Server segments per Edge (Host space): L2 /24 under 10.p.e.0/24
  Servers: 10.p.e.{1,2}; Gateway (Edge SVI): 10.p.e.254/24

- Agg–Edge links (Underlay, fully separated): L3 /31 under 172.(16+p).a.x
  subnet(base) = 172.(16+p).a.(2*e)/31, e∈{0,1}
  Edge = even (lower), Agg = odd (upper)

- Core–Agg links (Underlay, fully separated): L3 /31 under 172.(32+p).c.x
  subnet(base) = 172.(32+p).c.(2*a)/31, a∈{0,1}
  Agg  = even (lower), Core = odd (upper)

- ECMP policy (static routes for simplicity):
  * Hosts default → Edge SVI
  * Edge default → both Aggs in the same Pod (2-way ECMP using multiple nexthops)
  * Agg routes to pod-local server /24 via both Edges (directly connected) AND
    default → its two Core uplinks in the same Core-group (2-way ECMP)
  * Core has specific /24 routes for all racks via the corresponding Agg (no ECMP needed at Core)

Underlay (Agg–Edge/Core–Agg) is completely disjoint from Host space for clarity
and to avoid any address collisions. This layout generalizes to larger k when
pod index p is embedded in the second octet of the underlay blocks.

This script uses LinuxRouter nodes for Core/Agg/Edge to keep routing explicit.
All Core/Agg/Edge LinuxRouter nodes have `net.ipv4.conf.*.rp_filter=0` and
`net.ipv4.fib_multipath_hash_policy=1` applied via `tune_sysctls()` to make
ECMP hashing work consistently.

Edge nodes operate as ToR switches built from a Linux bridge `br_e{p}{e}`.
Two host-facing links `e{p}{e}-h{h}` (h∈{0,1}) are enslaved to that bridge,
which carries the SVI `10.p.e.254/24`. L3 northbound from the Edge uses
underlay /31 links on dedicated interfaces named `e{p}{e}-to-a{p}{a}` /
`a{p}{a}-to-e{p}{e}` (Edge lower IP, Agg higher IP).

The script is OVS-free; tested with Mininet 2.3+ using Linux kernel networking
primitives only.
"""

from mininet.net import Mininet
from mininet.node import Node
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

# ---------------------------
# Linux router node
# ---------------------------
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

# ---------------------------
# Helpers for addressing
# ---------------------------

def ip_core_agg(p: int, a: int, c: int):
    """Return (agg_ip/31, core_ip/31) tuple for Core–Agg link.
    Underlay block: 172.(32+p).c.(2*a)/31
    Agg is even address, Core is odd address.

    Parameters
    - p: pod index (0..3 for k=4), embedded in 2nd octet (32+p)
    - a: agg index within pod (0 or 1), mapped to base host part (2*a)
    - c: core index (0..3), mapped to 3rd octet
    """
    second = 32 + p
    base = 2 * a
    core = f'172.{second}.{c}.{base+1}/31'
    agg = f'172.{second}.{c}.{base}/31'
    return agg, core


def ip_agg_edge(p: int, a: int, e: int):
    """Return (edge_ip/31, agg_ip/31) tuple for Agg–Edge link.
    Underlay block: 172.(16+p).a.(2*e)/31
    Edge is even address, Agg is odd address.

    Parameters
    - p: pod index (0..3 for k=4), embedded in 2nd octet (16+p)
    - a: agg index within pod (0 or 1), mapped to 3rd octet
    - e: edge index within pod (0 or 1), mapped to base host part (2*e)
    """
    second = 16 + p
    base = 2 * e
    agg = f'172.{second}.{a}.{base+1}/31'
    edge = f'172.{second}.{a}.{base}/31'
    return edge, agg


def svi_ip(p: int, e: int):
    return f'10.{p}.{e}.254/24'


def host_ip(p: int, e: int, hidx: int):
    # hidx ∈ {0,1} → hosts: .1, .2
    host = 1 + hidx
    return f'10.{p}.{e}.{host}/24'


def net_24(p: int, e: int):
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


# ---------------------------
# Build topology (k=4)
# ---------------------------

def create_nodes_and_links(net):
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
                    intfName2=f'h{p}{e}{h}-eth0'
                )

    # Agg–Edge links (within pod)
    for p in range(4):
        for a in range(2):
            for e in range(2):
                net.addLink(
                    aggs[p][a],
                    edges[p][e],
                    intfName1=f'a{p}{a}-to-e{p}{e}',
                    intfName2=f'e{p}{e}-to-a{p}{a}'
                )

    # Core–Agg links (cross-pod)
    # Modifications to the logic may be needed for larger k values
    for p in range(4):
        net.addLink(
            cores[0],
            aggs[p][0],
            intfName1=f'c0-to-a{p}0',
            intfName2=f'a{p}0-to-c0'
        )
        net.addLink(
            cores[1],
            aggs[p][0],
            intfName1=f'c1-to-a{p}0',
            intfName2=f'a{p}0-to-c1'
        )
        net.addLink(
            cores[2],
            aggs[p][1],
            intfName1=f'c2-to-a{p}1',
            intfName2=f'a{p}1-to-c2'
        )
        net.addLink(
            cores[3],
            aggs[p][1],
            intfName1=f'c3-to-a{p}1',
            intfName2=f'a{p}1-to-c3'
        )

    return cores, aggs, edges, hosts


def tune_sysctls(routers):
    """Configure sysctl knobs required for ECMP to work as expected."""
    for router in routers:
        router.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        router.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        # Encourage multipath hashing to use L3+L4 fields where supported
        router.cmd('sysctl -w net.ipv4.fib_multipath_hash_policy=1')


def setup_edge_tor(edges):
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


def disable_offloads(nodes):
    """Disable GRO/GSO/TSO/LRO on all non-loopback interfaces for the given nodes."""
    for node in nodes:
        for intf in node.intfList():
            if not intf or not intf.name or intf.name == 'lo':
                continue
            node.cmd(
                f'ethtool -K {intf.name} gro off gso off tso off lro off || true'
            )


def assign_addresses(cores, aggs, edges, hosts):
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


def install_routes_ecmp(cores, aggs, edges):
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


def run_cli(net):
    """Drop into Mininet CLI with a few handy hints."""
    print('\nTopology is up. Quick sanity checks you can try:')
    print('- pingall')
    print('- On an edge (e00/e01/e10/..): ip route show; mtr -n 10.3.1.1 to watch ECMP')
    print('- iperf or iperf3 tests across different pods to exercise ECMP paths')
    CLI(net)
    net.stop()


def build():
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = create_nodes_and_links(net)
    net.build()

    routers = cores + flatten(aggs) + flatten(edges)
    tune_sysctls(routers)
    setup_edge_tor(edges)
    disable_offloads(routers + flatten(hosts))
    assign_addresses(cores, aggs, edges, hosts)
    install_routes_ecmp(cores, aggs, edges)
    run_cli(net)


if __name__ == '__main__':
    setLogLevel('info')
    build()
