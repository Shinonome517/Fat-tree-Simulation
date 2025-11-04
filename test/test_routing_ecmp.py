import time
from typing import Dict, List, Tuple

import pytest

import mininet_fattree_k4 as fattree
from test.util_debug import DumpSpec, fail_with_dumps
from test.util_routing import has_multipath
from test.util_stats import tx_bytes_kernel


HOST_ROUTE_CASES: List[Tuple[int, int, int, str]] = [
    (p, e, h, f"h{p}{e}{h}-eth0")
    for p in range(4)
    for e in range(2)
    for h in range(2)
]

ECMP_EDGE_DEFAULT_CASES: List[Tuple[int, int]] = [(p, e) for p in range(4) for e in range(2)]

ECMP_AGG_DEFAULT_CASES: List[Tuple[int, int]] = [(p, a) for p in range(4) for a in range(2)]


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

    receiver.cmd("pkill iperf3")
    receiver.cmd("iperf3 -s -D")
    time.sleep(1)

    client_out = sender.cmd(f"iperf3 -c {dst_ip} -P 10 -t 5")
    client_summary = client_out.strip().splitlines()[-1] if client_out.strip() else ""

    time.sleep(1)
    after: Dict[str, int] = {iface: tx_bytes_kernel(edge, iface) for iface in egress_ports}

    receiver.cmd("pkill iperf3")

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
