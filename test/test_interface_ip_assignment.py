import os
import time
from typing import Dict, List, Tuple

import pytest
from mininet.link import TCLink
from mininet.net import Mininet

import mininet_fattree_k4 as fattree
from test.util_debug import DumpSpec, fail_with_dumps
from test.util_routing import has_multipath
from test.util_stats import tx_bytes_kernel

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Mininet requires root privileges")


def _ipv4_addrs(node, interface: str) -> List[str]:
    """Return list of IPv4 CIDRs configured on the interface."""
    output = node.cmd(f"ip -o -4 addr show dev {interface}").strip()
    if not output:
        return []
    return [line.split()[3] for line in output.splitlines()]


EDGE_SVI_CASES: List[Tuple[int, int, str, str]] = [
    (p, e, f"br_e{p}{e}", fattree.svi_ip(p, e)) for p in range(4) for e in range(2)
]

EDGE_DOWNLINK_CASES: List[Tuple[int, int, int, str]] = [
    (p, e, h, f"e{p}{e}-h{h}") for p in range(4) for e in range(2) for h in range(2)
]

HOST_ADDRESS_CASES: List[Tuple[int, int, int, str, str]] = [
    (p, e, h, f"h{p}{e}{h}-eth0", fattree.host_ip(p, e, h))
    for p in range(4)
    for e in range(2)
    for h in range(2)
]

HOST_ROUTE_CASES: List[Tuple[int, int, int, str]] = [
    (p, e, h, f"h{p}{e}{h}-eth0")
    for p in range(4)
    for e in range(2)
    for h in range(2)
]

EDGE_TO_AGG_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(4):
    for e in range(2):
        for a in range(2):
            edge_ip, _ = fattree.ip_agg_edge(p, a, e)
            EDGE_TO_AGG_CASES.append((p, e, a, f"e{p}{e}-to-a{p}{a}", edge_ip))

AGG_TO_EDGE_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(4):
    for a in range(2):
        for e in range(2):
            _, agg_ip = fattree.ip_agg_edge(p, a, e)
            AGG_TO_EDGE_CASES.append((p, a, e, f"a{p}{a}-to-e{p}{e}", agg_ip))

AGG_TO_CORE_CASES: List[Tuple[int, int, int, str, str]] = []
CORE_TO_AGG_CASES: List[Tuple[int, int, int, str, str]] = []
for p in range(4):
    for a, cores in ((0, (0, 1)), (1, (2, 3))):
        for c in cores:
            agg_ip, core_ip = fattree.ip_core_agg(p, a, c)
            AGG_TO_CORE_CASES.append((p, a, c, f"a{p}{a}-to-c{c}", agg_ip))
            CORE_TO_AGG_CASES.append((p, c, a, f"c{c}-to-a{p}{a}", core_ip))

ECMP_EDGE_DEFAULT_CASES: List[Tuple[int, int]] = [(p, e) for p in range(4) for e in range(2)]

ECMP_AGG_SUBNET_CASES: List[Tuple[int, int, str]] = []
for p in range(4):
    for a in range(2):
        for e_sub in range(2):
            ECMP_AGG_SUBNET_CASES.append((p, a, fattree.net_24(p, e_sub)))

ECMP_AGG_DEFAULT_CASES: List[Tuple[int, int]] = [(p, a) for p in range(4) for a in range(2)]


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


@pytest.mark.parametrize("pod_idx, edge_idx, bridge, expected", EDGE_SVI_CASES)
def test_edge_svi_addresses(fattree_net, pod_idx, edge_idx, bridge, expected):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    addrs = _ipv4_addrs(edge, bridge)
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
    addrs = _ipv4_addrs(edge, iface)
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
    addrs = _ipv4_addrs(host, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{host.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(host, f"ip addr show dev {iface}", label=f"{host.name} ip addr"),
                DumpSpec(host, f"ip -j addr show dev {iface}", label=f"{host.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx, host_idx, iface", HOST_ROUTE_CASES)
def test_host_default_route(fattree_net, pod_idx, edge_idx, host_idx, iface):
    host = fattree_net["hosts"][pod_idx][edge_idx][host_idx]
    gateway = fattree.svi_ip(pod_idx, edge_idx).split("/")[0]
    expected = f"default via {gateway} dev {iface}"
    default_route = host.cmd("ip route show default").strip()
    if default_route != expected:
        fail_with_dumps(
            f"{host.name} expected default route '{expected}', got '{default_route}'",
            [
                DumpSpec(host, "ip route show default", label=f"{host.name} ip route"),
                DumpSpec(host, "ip -j route show default", label=f"{host.name} ip -j route"),
                DumpSpec(host, f"ip addr show dev {iface}", label=f"{host.name} ip addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx, _agg_idx, iface, expected", EDGE_TO_AGG_CASES)
def test_edge_to_agg_link_addresses(fattree_net, pod_idx, edge_idx, _agg_idx, iface, expected):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    addrs = _ipv4_addrs(edge, iface)
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
    addrs = _ipv4_addrs(agg, iface)
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
    addrs = _ipv4_addrs(agg, iface)
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
    addrs = _ipv4_addrs(core, iface)
    if addrs != [expected]:
        fail_with_dumps(
            f"{core.name}:{iface} expected {expected}, got {addrs}",
            [
                DumpSpec(core, f"ip addr show dev {iface}", label=f"{core.name} ip addr"),
                DumpSpec(core, f"ip -j addr show dev {iface}", label=f"{core.name} ip -j addr"),
            ],
        )


@pytest.mark.parametrize("pod_idx, edge_idx", ECMP_EDGE_DEFAULT_CASES)
def test_ecmp_configured_on_edges(fattree_net, pod_idx, edge_idx):
    edge = fattree_net["edges"][pod_idx][edge_idx]
    if not has_multipath(edge, "default"):
        fail_with_dumps(
            f"{edge.name} missing ECMP default route",
            [
                DumpSpec(edge, "ip route show default", label=f"{edge.name} ip route"),
                DumpSpec(edge, "ip -j route show default", label=f"{edge.name} ip -j route"),
            ],
        )


@pytest.mark.parametrize("pod_idx, agg_idx, subnet", ECMP_AGG_SUBNET_CASES)
def test_ecmp_configured_on_aggs_for_servers(fattree_net, pod_idx, agg_idx, subnet):
    agg = fattree_net["aggs"][pod_idx][agg_idx]
    if not has_multipath(agg, subnet):
        fail_with_dumps(
            f"{agg.name} missing ECMP for {subnet}",
            [
                DumpSpec(agg, f"ip route show {subnet}", label=f"{agg.name} ip route {subnet}"),
                DumpSpec(agg, f"ip -j route show {subnet}", label=f"{agg.name} ip -j route {subnet}"),
            ],
        )


@pytest.mark.parametrize("pod_idx, agg_idx", ECMP_AGG_DEFAULT_CASES)
def test_ecmp_configured_on_aggs_default(fattree_net, pod_idx, agg_idx):
    agg = fattree_net["aggs"][pod_idx][agg_idx]
    if not has_multipath(agg, "default"):
        fail_with_dumps(
            f"{agg.name} missing ECMP default route",
            [
                DumpSpec(agg, "ip route show default", label=f"{agg.name} ip route"),
                DumpSpec(agg, "ip -j route show default", label=f"{agg.name} ip -j route"),
            ],
        )


@pytest.mark.slow
def test_ecmp_spreads_traffic(fattree_net):
    sender = fattree_net["hosts"][0][0][0]
    receiver = fattree_net["hosts"][1][0][0]
    edge = fattree_net["edges"][0][0]
    dst_ip = fattree.host_ip(1, 0, 0).split("/")[0]

    egress_ports = ["e00-to-a00", "e00-to-a01"]
    before: Dict[str, int] = {iface: tx_bytes_kernel(edge, iface) for iface in egress_ports}

    receiver.cmd("pkill iperf")
    receiver.cmd("iperf -s -D")
    time.sleep(1)

    client_out = sender.cmd(f"iperf -c {dst_ip} -P 10 -t 5")
    client_summary = client_out.strip().splitlines()[-1] if client_out.strip() else ""

    time.sleep(1)
    after: Dict[str, int] = {iface: tx_bytes_kernel(edge, iface) for iface in egress_ports}

    receiver.cmd("pkill iperf")

    deltas: Dict[str, int] = {iface: max(after[iface] - before[iface], 0) for iface in egress_ports}
    total_bytes = sum(deltas.values())
    used_ports = [iface for iface, delta in deltas.items() if delta > 0]
    max_share = max((delta / total_bytes for delta in deltas.values() if total_bytes > 0), default=1.0)

    reasons = []
    if total_bytes == 0:
        reasons.append("no traffic observed on egress ports")
    if len(used_ports) < 2:
        reasons.append(f"only {len(used_ports)} egress port(s) carried traffic")
    if max_share > 0.8:
        reasons.append(f"max share {max_share:.2f} exceeds 0.80 threshold")

    if reasons:
        delta_desc = ", ".join(f"{iface}:{deltas[iface]}" for iface in egress_ports)
        message = (
            "ECMP traffic did not spread as expected: "
            + "; ".join(reasons)
            + f". deltas=({delta_desc}), iperf_summary='{client_summary}'"
        )
        dumps = [
            DumpSpec(edge, f"ip -s -j link show dev {iface}", label=f"{edge.name} stats {iface}")
            for iface in egress_ports
        ] + [
            DumpSpec(edge, "ip route show default", label=f"{edge.name} ip route"),
            DumpSpec(edge, "ip -j route show default", label=f"{edge.name} ip -j route"),
        ]
        fail_with_dumps(message, dumps)
