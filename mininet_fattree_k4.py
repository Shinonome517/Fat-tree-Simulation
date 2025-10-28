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

# ---------------------------
# Build topology (k=4)
# ---------------------------

def build():
    net = Mininet(link=TCLink, build=False)

    # Core (4 routers): c = 0..3
    cores = [net.addHost(f'c{c}', cls=LinuxRouter) for c in range(4)]

    # Pods: p = 0..3; each has Agg a∈{0,1} and Edge e∈{0,1}
    aggs = [[net.addHost(f'a{p}{a}', cls=LinuxRouter) for a in range(2)] for p in range(4)]
    edges = [[net.addHost(f'e{p}{e}', cls=LinuxRouter) for e in range(2)] for p in range(4)]

    # Hosts: two per Edge (h=0,1)
    hosts = [[[net.addHost(f'h{p}{e}{h}', ip=host_ip(p, e, h), defaultRoute=f'via 10.{p}.{e}.254')
               for h in range(2)] for e in range(2)] for p in range(4)]

    # ---------------- Links ----------------
    # 1) Edge–Server
    for p in range(4):
        for e in range(2):
            for h in range(2):
                net.addLink(edges[p][e], hosts[p][e][h])

    # 2) Agg–Edge (within each pod)
    for p in range(4):
        for a in range(2):
            for e in range(2):
                net.addLink(aggs[p][a], edges[p][e])

    # 3) Core–Agg (cross-pod)
    # Core grouping: c∈{0,1} → a=0 group; c∈{2,3} → a=1 group
    for p in range(4):
        # a=0 group connects to c0 and c1
        net.addLink(cores[0], aggs[p][0])
        net.addLink(cores[1], aggs[p][0])
        # a=1 group connects to c2 and c3
        net.addLink(cores[2], aggs[p][1])
        net.addLink(cores[3], aggs[p][1])

    net.build()

    # ---- System-wide knobs for clean ECMP ----
    routers = []
    routers += cores
    for p in range(4):
        routers += aggs[p]
        routers += edges[p]
    for r in routers:
        r.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        r.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        # Use L3+L4 fields for ECMP hashing where supported
        r.cmd('sysctl -w net.ipv4.fib_multipath_hash_policy=1')

    # After build, we can discover interfaces in a stable order.
    # We'll map interfaces by neighbor names for clarity.
    def link_intfs(node):
        # returns dict peerName -> (intfOnNode, intfOnPeer)
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

    # ---------------- Address assignment ----------------

    # 1) Edge–Server server-facing interfaces: set SVI .254 on BOTH host-facing ifaces of each Edge
    for p in range(4):
        for e in range(2):
            eNode = edges[p][e]
            e_ifmap = link_intfs(eNode)
            # find host peers from names starting with 'h{p}{e}'
            for h in range(2):
                hname = f'h{p}{e}{h}'
                if hname in e_ifmap:
                    e_if, h_if = e_ifmap[hname]
                    # Edge SVI on this host-facing port
                    eNode.setIP(intf=e_if, ip=svi_ip(p, e))
                    # Host IP already set at creation; ensure it's applied to the correct interface
                    host = hosts[p][e][h]
                    host.setIP(intf=h_if, ip=host_ip(p, e, h))

    # 2) Agg–Edge /31 links
    for p in range(4):
        for a in range(2):
            for e in range(2):
                aNode = aggs[p][a]
                eNode = edges[p][e]
                a_if = link_intfs(aNode)[eNode.name][0]
                e_if = link_intfs(eNode)[aNode.name][0]
                edge_ip, agg_ip = ip_agg_edge(p, a, e)
                eNode.setIP(intf=e_if, ip=edge_ip)
                aNode.setIP(intf=a_if, ip=agg_ip)

    # 3) Core–Agg /31 links
    for p in range(4):
        # a=0 group with c0,c1
        for c in (0, 1):
            cNode = cores[c]
            aNode = aggs[p][0]
            c_if = link_intfs(cNode)[aNode.name][0]
            a_if = link_intfs(aNode)[cNode.name][0]
            agg_ip, core_ip = ip_core_agg(p, 0, c)
            aNode.setIP(intf=a_if, ip=agg_ip)
            cNode.setIP(intf=c_if, ip=core_ip)
        # a=1 group with c2,c3
        for c in (2, 3):
            cNode = cores[c]
            aNode = aggs[p][1]
            c_if = link_intfs(cNode)[aNode.name][0]
            a_if = link_intfs(aNode)[cNode.name][0]
            agg_ip, core_ip = ip_core_agg(p, 1, c)
            aNode.setIP(intf=a_if, ip=agg_ip)
            cNode.setIP(intf=c_if, ip=core_ip)

    # ---------------- Static routing & ECMP ----------------

    # Edge: default route via both Aggs (ECMP)
    for p in range(4):
        for e in range(2):
            eNode = edges[p][e]
            # nexthops are the agg IPs on the /31s to A0 & A1
            # A0 side
            edge_ip_a0, agg_ip_a0 = ip_agg_edge(p, 0, e)
            # A1 side
            edge_ip_a1, agg_ip_a1 = ip_agg_edge(p, 1, e)
            # Install ECMP default
            # Find dev names for each uplink
            dev_a0 = link_intfs(eNode)[f'a{p}0'][0].name
            dev_a1 = link_intfs(eNode)[f'a{p}1'][0].name
            eNode.cmd(f'ip route replace default scope global ' \
                      f'nexthop via {agg_ip_a0.split("/")[0]} dev {dev_a0} weight 1 ' \
                      f'nexthop via {agg_ip_a1.split("/")[0]} dev {dev_a1} weight 1')

    # Agg: routes to pod-local server /24 via both Edges (ECMP); default via both Cores in its group
    for p in range(4):
        for a in range(2):
            aNode = aggs[p][a]
            # Build ECMP per /24 using both Edges in the pod
            e0_ip, _ = ip_agg_edge(p, a, 0)
            e1_ip, _ = ip_agg_edge(p, a, 1)
            dev_e0 = link_intfs(aNode)[f'e{p}0'][0].name
            dev_e1 = link_intfs(aNode)[f'e{p}1'][0].name
            # Two rack subnets per pod (e=0,1)
            for e_sub in (0, 1):
                subnet = net_24(p, e_sub)
                aNode.cmd(
                    f"ip route replace {subnet} scope global "\
                    f"nexthop via {e0_ip.split('/') [0]} dev {dev_e0} weight 1 "\
                    f"nexthop via {e1_ip.split('/') [0]} dev {dev_e1} weight 1"
                )

            # Default via two cores of the same group (ECMP)
            core_indices = (0, 1) if a == 0 else (2, 3)
            nexthops = []
            for c in core_indices:
                agg_ip, core_ip = ip_core_agg(p, a, c)
                dev_c = link_intfs(aNode)[f'c{c}'][0].name
                nexthops.append(f"nexthop via {core_ip.split('/') [0]} dev {dev_c} weight 1")
            aNode.cmd('ip route replace default ' + ' '.join(nexthops))

    # Core: specific /24 routes for all racks via the correct Agg (group-matched)
    for c in range(4):
        cNode = cores[c]
        # infer group a from c index
        a_grp = 0 if c in (0, 1) else 1
        for p in range(4):
            # next-hop is the Agg (of this group) in pod p
            agg_ip, core_ip = ip_core_agg(p, a_grp, c)
            dev = link_intfs(cNode)[f'a{p}{a_grp}'][0].name
            # two racks (e=0,1) per pod
            for e in range(2):
                subnet = net_24(p, e)
                cNode.cmd(f"ip route replace {subnet} via {agg_ip.split('/') [0]} dev {dev}")

    # Start CLI for experimentation
    print('\nTopology is up. Quick sanity checks you can try:')
    print('- pingall')
    print('- On an edge (e00/e01/e10/..): ip route show; mtr -n 10.3.1.1 to watch ECMP')
    print('- iperf or iperf3 tests across different pods to exercise ECMP paths')

    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    build()
