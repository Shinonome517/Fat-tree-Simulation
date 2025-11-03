import os

import pytest
from mininet.link import TCLink
from mininet.net import Mininet

import mininet_fattree_k4 as fattree

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Mininet requires root privileges")


def _ipv4_addrs(node, interface):
    """Return list of IPv4 CIDRs configured on the interface."""
    output = node.cmd(f"ip -o -4 addr show dev {interface}").strip()
    if not output:
        return []
    return [line.split()[3] for line in output.splitlines()]


@pytest.fixture(scope="module")
def fattree_net():
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = fattree.create_nodes_and_links(net)
    net.build()

    routers = cores + fattree.flatten(aggs) + fattree.flatten(edges)
    fattree.tune_sysctls(routers)
    fattree.setup_edge_tor(edges)
    fattree.assign_addresses(cores, aggs, edges, hosts)
    fattree.install_routes_ecmp(cores, aggs, edges)

    try:
        yield {
            "net": net,
            "cores": cores,
            "aggs": aggs,
            "edges": edges,
            "hosts": hosts,
        }
    finally:
        net.stop()


def test_edge_svi_addresses(fattree_net):
    edges = fattree_net["edges"]
    for p, pod_edges in enumerate(edges):
        for e, edge in enumerate(pod_edges):
            svi = fattree.svi_ip(p, e)
            bridge = f"br_e{p}{e}"
            addrs = _ipv4_addrs(edge, bridge)
            assert [svi] == addrs, f"{bridge} should have only {svi}, got {addrs}"


def test_edge_downlink_interfaces_have_no_ipv4(fattree_net):
    edges = fattree_net["edges"]
    for p, pod_edges in enumerate(edges):
        for e, edge in enumerate(pod_edges):
            for h in range(2):
                iface = f"e{p}{e}-h{h}"
                addrs = _ipv4_addrs(edge, iface)
                assert addrs == [], f"{iface} should not have IPv4 addresses, got {addrs}"


def test_host_addresses_and_routes(fattree_net):
    hosts = fattree_net["hosts"]
    for p, pod_hosts in enumerate(hosts):
        for e, edge_hosts in enumerate(pod_hosts):
            gateway = fattree.svi_ip(p, e).split("/")[0]
            for h, host in enumerate(edge_hosts):
                iface = f"h{p}{e}{h}-eth0"
                expected_ip = fattree.host_ip(p, e, h)
                addrs = _ipv4_addrs(host, iface)
                assert [expected_ip] == addrs, f"{host.name} expected {expected_ip} on {iface}, got {addrs}"
                default_route = host.cmd("ip route show default").strip()
                assert default_route == f"default via {gateway} dev {iface}", (
                    f"{host.name} default route mismatch: {default_route}"
                )


def test_agg_edge_link_addresses(fattree_net):
    aggs = fattree_net["aggs"]
    edges = fattree_net["edges"]
    for p in range(4):
        for a in range(2):
            for e in range(2):
                edge_ip, agg_ip = fattree.ip_agg_edge(p, a, e)
                edge_intf = f"e{p}{e}-to-a{p}{a}"
                agg_intf = f"a{p}{a}-to-e{p}{e}"
                edge_addrs = _ipv4_addrs(edges[p][e], edge_intf)
                agg_addrs = _ipv4_addrs(aggs[p][a], agg_intf)
                assert [edge_ip] == edge_addrs, f"{edge_intf} expected {edge_ip}, got {edge_addrs}"
                assert [agg_ip] == agg_addrs, f"{agg_intf} expected {agg_ip}, got {agg_addrs}"


def test_core_agg_link_addresses(fattree_net):
    cores = fattree_net["cores"]
    aggs = fattree_net["aggs"]
    for p in range(4):
        for c in (0, 1):
            agg_ip, core_ip = fattree.ip_core_agg(p, 0, c)
            agg_iface = f"a{p}0-to-c{c}"
            core_iface = f"c{c}-to-a{p}0"
            agg_addrs = _ipv4_addrs(aggs[p][0], agg_iface)
            core_addrs = _ipv4_addrs(cores[c], core_iface)
            assert [agg_ip] == agg_addrs, f"{agg_iface} expected {agg_ip}, got {agg_addrs}"
            assert [core_ip] == core_addrs, f"{core_iface} expected {core_ip}, got {core_addrs}"
        for c in (2, 3):
            agg_ip, core_ip = fattree.ip_core_agg(p, 1, c)
            agg_iface = f"a{p}1-to-c{c}"
            core_iface = f"c{c}-to-a{p}1"
            agg_addrs = _ipv4_addrs(aggs[p][1], agg_iface)
            core_addrs = _ipv4_addrs(cores[c], core_iface)
            assert [agg_ip] == agg_addrs, f"{agg_iface} expected {agg_ip}, got {agg_addrs}"
            assert [core_ip] == core_addrs, f"{core_iface} expected {core_ip}, got {core_addrs}"
