import json
from typing import List, Tuple

import pytest

import mininet_fattree_k4 as fattree
from test.util_debug import DumpSpec, fail_with_dumps

CORE_ROUTE_CASES: List[Tuple[int, int, int, str]] = [
    (core_idx, pod_idx, edge_idx, fattree.net_24(pod_idx, edge_idx))
    for core_idx in range(4)
    for pod_idx in range(4)
    for edge_idx in range(2)
]


@pytest.mark.parametrize("core_idx,pod_idx,edge_idx,subnet", CORE_ROUTE_CASES)
def test_core_has_route_to_edge_subnet(
    fattree_net, core_idx: int, pod_idx: int, edge_idx: int, subnet: str
) -> None:
    core = fattree_net["cores"][core_idx]
    table_raw = core.cmd(f"ip -j route show {subnet}") or "[]"
    try:
        entries = json.loads(table_raw)
    except json.JSONDecodeError:
        entries = []

    if entries:
        return

    label = f"c{core_idx} -> {subnet}"
    fail_with_dumps(
        f"{label} missing route",
        [
            DumpSpec(core, f"ip route show {subnet}", label=f"{label} ip route"),
            DumpSpec(core, f"ip -j route show {subnet}", label=f"{label} ip -j route"),
            DumpSpec(core, "ip route show", label=f"{label} full route table", limit=1200),
        ],
    )

