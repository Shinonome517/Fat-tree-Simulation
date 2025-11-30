import os
from typing import Iterator, Tuple

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
TOTAL_HOSTS = N_PODS * N_EDGE_PER_POD * N_HOSTS_PER_EDGE
EXHAUSTIVE_HOST_LIMIT = 64
if TOTAL_HOSTS > EXHAUSTIVE_HOST_LIMIT:
    pytest.skip(
        f"Host matrix too large for exhaustive connectivity test (k={K}, hosts={TOTAL_HOSTS}).",
        allow_module_level=True,
    )


def _host_cases() -> Iterator[Tuple[int, int, int, int, int, int]]:
    for sp in range(N_PODS):
        for se in range(N_EDGE_PER_POD):
            for sh in range(N_HOSTS_PER_EDGE):
                for dp in range(N_PODS):
                    for de in range(N_EDGE_PER_POD):
                        for dh in range(N_HOSTS_PER_EDGE):
                            if (sp, se, sh) == (dp, de, dh):
                                continue
                            yield sp, se, sh, dp, de, dh


HOST_PAIR_CASES = list(_host_cases())


@pytest.mark.slow
@pytest.mark.parametrize(
    "src_pod,src_edge,src_host,dst_pod,dst_edge,dst_host",
    HOST_PAIR_CASES,
)
def test_all_hosts_reach_each_other(
    fattree_net,
    src_pod: int,
    src_edge: int,
    src_host: int,
    dst_pod: int,
    dst_edge: int,
    dst_host: int,
) -> None:
    src = fattree_net["hosts"][src_pod][src_edge][src_host]
    dst_ip = fattree.host_ip(dst_pod, dst_edge, dst_host).split("/")[0]
    description = f"h{src_pod}{src_edge}{src_host} -> h{dst_pod}{dst_edge}{dst_host}"

    ping_output = src.cmd(f"ping -c2 -W1 {dst_ip}")
    if " 0% packet loss" in ping_output:
        return

    dst = fattree_net["hosts"][dst_pod][dst_edge][dst_host]
    iface = f"h{src_pod}{src_edge}{src_host}-eth0"
    dumps = [
        DumpSpec(src, f"ip addr show dev {iface}", label=f"{description} src ip addr"),
        DumpSpec(src, "ip route show", label=f"{description} src routes"),
        DumpSpec(src, f"ip neigh show dev {iface}", label=f"{description} src neigh"),
        DumpSpec(dst, "ip addr show", label=f"{description} dst ip addr"),
    ]
    fail_with_dumps(
        f"{description} ping failed (target {dst_ip})\n\n{ping_output.strip()}",
        dumps,
    )
