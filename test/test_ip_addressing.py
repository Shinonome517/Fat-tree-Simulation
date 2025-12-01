import os
from typing import List, Tuple

import pytest

import topology as fattree
from test.util_addr import ipv4_addrs
from test.util_debug import DumpSpec, fail_with_dumps

K = int(os.environ.get("FATTREE_K", "4"))
assert K % 2 == 0 and 2 <= K <= 16
N_PODS = K
N_AGG_PER_POD = K // 2
N_EDGE_PER_POD = K // 2
N_HOSTS_PER_EDGE = K // 2
N_CORE_GROUPS = K // 2
N_CORE_PER_GROUP = K // 2
N_CORES = N_CORE_GROUPS * N_CORE_PER_GROUP

EDGE_SVI_CASES: List[Tuple[int, int, str, str]] = [
    (p, e, f"br_e{p}{e}", fattree.svi_ip(p, e)) for p in range(N_PODS) for e in range(N_EDGE_PER_POD)
]

EDGE_DOWNLINK_CASES: List[Tuple[int, int, int, str]] = [
    (p, e, h, f"e{p}{e}-h{h}") for p in range(N_PODS) for e in range(N_EDGE_PER_POD) for h in range(N_HOSTS_PER_EDGE)
]

HOST_ADDRESS_CASES: List[Tuple[int, int, int, str, List[str]]] = [
    (p, e, h, f"h{p}{e}{h}-eth0", fattree.host_ips(p, e, h))
    for p in range(N_PODS)
    for e in range(N_EDGE_PER_POD)
    for h in range(N_HOSTS_PER_EDGE)
]

EDGE_TO_AGG_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(N_PODS):
    for e in range(N_EDGE_PER_POD):
        for a in range(N_AGG_PER_POD):
            edge_ip, _ = fattree.ip_agg_edge(p, a, e)
            EDGE_TO_AGG_CASES.append((p, e, a, f"e{p}{e}-to-a{p}{a}", edge_ip))

AGG_TO_EDGE_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(N_PODS):
    for a in range(N_AGG_PER_POD):
        for e in range(N_EDGE_PER_POD):
            _, agg_ip = fattree.ip_agg_edge(p, a, e)
            AGG_TO_EDGE_CASES.append((p, a, e, f"a{p}{a}-to-e{p}{e}", agg_ip))

AGG_TO_CORE_CASES: List[Tuple[int, int, int, str, str]] = []
CORE_TO_AGG_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(N_PODS):
    for a in range(N_AGG_PER_POD):
        for i in range(N_CORE_GROUPS):
            c = i * N_CORE_PER_GROUP + a
            agg_ip, core_ip = fattree.ip_core_agg(p, a, c)
            AGG_TO_CORE_CASES.append((p, a, c, f"a{p}{a}-to-c{c}", agg_ip))
            CORE_TO_AGG_CASES.append((p, c, a, f"c{c}-to-a{p}{a}", core_ip))


@pytest.mark.parametrize("pod_idx, edge_idx, bridge, expected", EDGE_SVI_CASES)
def test_edge_svi_addresses(fattree_net, pod_idx, edge_idx, bridge, expected):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    addrs = ipv4_addrs(edge, bridge)
    if addrs != [expected]:
        fail_with_dumps(
            f"{edge.name}:{bridge} expected {expected}, got {addrs}",
            [
                DumpSpec(edge, f"ip addr show {bridge}", label=f"{edge.name} ip addr"),
                DumpSpec(edge, f"ip -j addr show dev {bridge}", label=f"{edge.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx, _host_idx, iface", EDGE_DOWNLINK_CASES)
def test_edge_downlink_interfaces_have_no_ipv4(fattree_net, pod_idx, edge_idx, _host_idx, iface):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    addrs = ipv4_addrs(edge, iface)
    if addrs:
        fail_with_dumps(
            f"{edge.name}:{iface} expected no IPv4 address, got {addrs}",
            [
                DumpSpec(edge, f"ip addr show dev {iface}", label=f"{edge.name} ip addr"),
                DumpSpec(edge, f"ip -j addr show dev {iface}", label=f"{edge.name} ip -j addr"),
                DumpSpec(
                    edge,
                    f"ip route show to exact {addrs[0].split('/')[0]}" if addrs else "true",
                    label=f"{edge.name} routes",
                ),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx, host_idx, iface, expected", HOST_ADDRESS_CASES)
def test_host_interface_addresses(fattree_net, pod_idx, edge_idx, host_idx, iface, expected):
    host = fattree_net["hosts"][pod_idx][edge_idx][host_idx]
    addrs = ipv4_addrs(host, iface)
    if sorted(addrs) != sorted(expected):
        fail_with_dumps(
            f"{host.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(host, f"ip addr show dev {iface}", label=f"{host.name} ip addr"),
                DumpSpec(host, f"ip -j addr show dev {iface}", label=f"{host.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx, _agg_idx, iface, expected", EDGE_TO_AGG_CASES)
def test_edge_to_agg_link_addresses(fattree_net, pod_idx, edge_idx, _agg_idx, iface, expected):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    addrs = ipv4_addrs(edge, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{edge.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(edge, f"ip addr show dev {iface}", label=f"{edge.name} ip addr"),
                DumpSpec(edge, f"ip -j addr show dev {iface}", label=f"{edge.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, agg_idx, _edge_idx, iface, expected", AGG_TO_EDGE_CASES)
def test_agg_to_edge_link_addresses(fattree_net, pod_idx, agg_idx, _edge_idx, iface, expected):
    agg = fattree_net["aggs"][pod_idx][agg_idx]
    addrs = ipv4_addrs(agg, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{agg.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(agg, f"ip addr show dev {iface}", label=f"{agg.name} ip addr"),
                DumpSpec(agg, f"ip -j addr show dev {iface}", label=f"{agg.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, agg_idx, core_idx, iface, expected", AGG_TO_CORE_CASES)
def test_agg_to_core_link_addresses(fattree_net, pod_idx, agg_idx, core_idx, iface, expected):
    agg = fattree_net["aggs"][pod_idx][agg_idx]
    addrs = ipv4_addrs(agg, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{agg.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(agg, f"ip addr show dev {iface}", label=f"{agg.name} ip addr"),
                DumpSpec(agg, f"ip -j addr show dev {iface}", label=f"{agg.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, core_idx, agg_idx, iface, expected", CORE_TO_AGG_CASES)
def test_core_to_agg_link_addresses(fattree_net, pod_idx, core_idx, agg_idx, iface, expected):
    core = fattree_net["cores"][core_idx]
    addrs = ipv4_addrs(core, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{core.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(core, f"ip addr show dev {iface}", label=f"{core.name} ip addr"),
                DumpSpec(core, f"ip -j addr show dev {iface}", label=f"{core.name} ip -j addr"),
            ],
        )
