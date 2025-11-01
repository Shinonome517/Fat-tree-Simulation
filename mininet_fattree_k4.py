#!/usr/bin/env python3
"""
Mininet script: k=4 Fat-Tree with the following IP plan (from the design brief)

- Core–Agg links:  L3 /31 under 10.4.c.x
  subnet(base) = 10.4.c.(2*p)/31, p∈{0..3}
  Agg = even (lower), Core = odd (upper)

- Agg–Edge links:  L3 /31 under 10.p.a.x
  subnet(base) = 10.p.a.(2*e)/31, e∈{0,1}
  Edge = even (lower), Agg = odd (upper)

- Server segments per Edge: L2 /24 under 10.p.e.0/24
  Servers: 10.p.e.{1,2};  Gateway (Edge SVI): 10.p.e.254/24

- ECMP policy:
  * Hosts default → Edge SVI
  * Edge default → both Aggs in the same Pod (2-way ECMP using multiple nexthops)
  * Agg routes to pod-local server /24 via both Edges (directly connected) AND
    default → its two Core uplinks in the same Core-group (2-way ECMP)
  * Core has specific /24 routes for all 8 racks via the corresponding Agg (no ECMP needed at Core)

This script uses LinuxRouter nodes for Core/Agg/Edge to keep routing explicit.
Tested with Mininet 2.3+ / standard OVS.
"""

from mininet.net import Mininet
from mininet.node import Node, Host, OVSSwitch
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
    Subnet base = 10.4.c.(2*p). Agg is even address, Core is odd address.
    Note: 'a' is ignored (kept for call-site compatibility).
    """
    base = 2 * p
    core = f'10.4.{c}.{base+1}/31'
    agg = f'10.4.{c}.{base}/31'
    return agg, core


def ip_agg_edge(p: int, a: int, e: int):
    """Return (edge_ip/31, agg_ip/31) tuple for Agg–Edge link.
    subnet base = 10.p.a.(2*e)
    Edge is even address, Agg is odd address.
    """
    base = 2 * e
    agg = f'10.{p}.{a}.{base+1}/31'
    edge = f'10.{p}.{a}.{base}/31'
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


def link_intfs(node):
    """Return mapping peerName -> (intfOnNode, intfOnPeer)."""
    mapping = {}
    for intf in node.intfList():
        if not intf.link or not intf.link.intf1 or not intf.link.intf2:
            continue
        i1, i2 = intf.link.intf1, intf.link.intf2
        if i1.node is node:
            peer = i2.node
            mapping[peer.name] = (i1, i2)
        elif i2.node is node:
            peer = i1.node
            mapping[peer.name] = (i2, i1)
    return mapping

# ---------------------------
# Build topology (k=4)
# ---------------------------

def create_nodes_and_links(net):
    """Create routers, hosts, and interconnect them."""
    cores = [net.addHost(f'c{c}', cls=LinuxRouter) for c in range(4)]
    aggs = [[net.addHost(f'a{p}{a}', cls=LinuxRouter) for a in range(2)] for p in range(4)]
    edges = [[net.addHost(f'e{p}{e}', cls=LinuxRouter) for e in range(2)] for p in range(4)]
    access = [[net.addSwitch(f'b{p}{e}', cls=OVSSwitch) for e in range(2)] for p in range(4)]
    hosts = [[[
        net.addHost(
            f'h{p}{e}{h}',
            ip=host_ip(p, e, h),
            defaultRoute=f'via 10.{p}.{e}.254'
        )
        for h in range(2)
    ] for e in range(2)] for p in range(4)]

    # Edge–Server links
    for p in range(4):
        for e in range(2):
            for h in range(2):
                net.addLink(access[p][e], hosts[p][e][h])
            net.addLink(edges[p][e], access[p][e])

    # Agg–Edge links (within pod)
    for p in range(4):
        for a in range(2):
            for e in range(2):
                net.addLink(aggs[p][a], edges[p][e])

    # Core–Agg links (cross-pod)
    # Modifications to the logic may be needed for larger k values
    for p in range(4):
        net.addLink(cores[0], aggs[p][0])
        net.addLink(cores[1], aggs[p][0])
        net.addLink(cores[2], aggs[p][1])
        net.addLink(cores[3], aggs[p][1])

    return cores, aggs, edges, hosts, access


def tune_sysctls(routers):
    """Configure sysctl knobs required for ECMP to work as expected."""
    for router in routers:
        router.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        router.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        # Encourage multipath hashing to use L3+L4 fields where supported
        router.cmd('sysctl -w net.ipv4.fib_multipath_hash_policy=1')


def assign_addresses(cores, aggs, edges, hosts, access):
    """Assign IP addresses to all interconnect links and host segments."""
    # Edge-facing SVIs and host IPs
    for p in range(4):
        for e in range(2):
            e_node = edges[p][e]
            bridge_name = access[p][e].name
            e_if = link_intfs(e_node)[bridge_name][0]
            e_node.setIP(intf=e_if, ip=svi_ip(p, e))
            for h in range(2):
                h_node = hosts[p][e][h]
                h_if = link_intfs(h_node)[bridge_name][0]
                h_node.setIP(intf=h_if, ip=host_ip(p, e, h))

    # Agg–Edge /31 links
    for p in range(4):
        for a in range(2):
            for e in range(2):
                a_node = aggs[p][a]
                e_node = edges[p][e]
                a_if = link_intfs(a_node)[e_node.name][0]
                e_if = link_intfs(e_node)[a_node.name][0]
                edge_ip, agg_ip = ip_agg_edge(p, a, e)
                e_node.setIP(intf=e_if, ip=edge_ip)
                a_node.setIP(intf=a_if, ip=agg_ip)

    # Core–Agg /31 links
    for p in range(4):
        for c in (0, 1):
            c_node = cores[c]
            a_node = aggs[p][0]
            c_if = link_intfs(c_node)[a_node.name][0]
            a_if = link_intfs(a_node)[c_node.name][0]
            agg_ip, core_ip = ip_core_agg(p, 0, c)
            a_node.setIP(intf=a_if, ip=agg_ip)
            c_node.setIP(intf=c_if, ip=core_ip)
        for c in (2, 3):
            c_node = cores[c]
            a_node = aggs[p][1]
            c_if = link_intfs(c_node)[a_node.name][0]
            a_if = link_intfs(a_node)[c_node.name][0]
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
            dev_a0 = link_intfs(e_node)[f'a{p}0'][0].name
            dev_a1 = link_intfs(e_node)[f'a{p}1'][0].name
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
            dev_e0 = link_intfs(a_node)[f'e{p}0'][0].name
            dev_e1 = link_intfs(a_node)[f'e{p}1'][0].name
            for e_sub in (0, 1):
                subnet = net_24(p, e_sub)
                a_node.cmd(
                    f"ip route replace {subnet} scope global "
                    f"nexthop via {e0_ip.split('/') [0]} dev {dev_e0} weight 1 "
                    f"nexthop via {e1_ip.split('/') [0]} dev {dev_e1} weight 1"
                )

            core_indices = (0, 1) if a == 0 else (2, 3)
            nexthops = []
            for c in core_indices:
                agg_ip, core_ip = ip_core_agg(p, a, c)
                dev_c = link_intfs(a_node)[f'c{c}'][0].name
                nexthops.append(f"nexthop via {core_ip.split('/') [0]} dev {dev_c} weight 1")
            a_node.cmd('ip route replace default ' + ' '.join(nexthops))

    # Core routes to every rack via appropriate Agg
    for c in range(4):
        c_node = cores[c]
        a_grp = 0 if c in (0, 1) else 1
        for p in range(4):
            agg_ip, _ = ip_core_agg(p, a_grp, c)
            dev = link_intfs(c_node)[f'a{p}{a_grp}'][0].name
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
    cores, aggs, edges, hosts, access = create_nodes_and_links(net)
    net.build()

    routers = cores + flatten(aggs) + flatten(edges)
    tune_sysctls(routers)
    assign_addresses(cores, aggs, edges, hosts, access)
    install_routes_ecmp(cores, aggs, edges)
    run_cli(net)


if __name__ == '__main__':
    setLogLevel('info')
    build()
