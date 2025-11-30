import json
import os
from typing import List, Tuple

import pytest

import topology as fattree
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

CORE_ROUTE_CASES: List[Tuple[int, int, int, str]] = [
    (core_idx, pod_idx, edge_idx, fattree.net_24(pod_idx, edge_idx))
    for core_idx in range(N_CORES)
    for pod_idx in range(N_PODS)
    for edge_idx in range(N_EDGE_PER_POD)
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
