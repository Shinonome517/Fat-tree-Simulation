"""Skeleton for whitebox experiments (Step 3).

This module wires together the Fat-Tree setup, basic traffic generation, and
switch statistics capture. Routing/policy tweaks are intentionally stubbed and
will be implemented in Step 4.
"""

import argparse
import json
import random
import shlex
import threading
import time
from pathlib import Path
from typing import List, Optional

from experiments import (
    create_fattree,
    make_log_dir,
    picoquic_perf_cmd,
    snapshot_switch_bytes,
)
from topology import (
    host_ip,
    ip_agg_edge,
    ip_core_agg,
    net_24,
    stop_fattree_topology,
)

WARMUP_SECONDS = 5
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
DEFAULT_SEED = 12345

# TODO: Adjust server options (certs/logging/paths) for actual experiments.
PICOQUIC_CERT_PATH = "/etc/picoquic/server-cert.pem"
PICOQUIC_KEY_PATH = "/etc/picoquic/server-key.pem"
ELEPHANT_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)
MOUSE_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)

# Roles for picoquic extra-arg selection.
ROLE_ELEPHANT_SERVER = "elephant-server"
ROLE_ELEPHANT_CLIENT = "elephant-client"
ROLE_MOUSE_SERVER = "mouse-server"
ROLE_MOUSE_CLIENT = "mouse-client"

# Source IPs for marking (primary address of h001 and h201).
S_IP_ELEPHANT = host_ip(0, 0, 1).split("/")[0]
S_IP_MOUSE = host_ip(2, 0, 1).split("/")[0]

# Multipath QUIC: advertise client-only extra addresses (do not include S_IP_ELEPHANT).
# Mininet hosts typically use ifindex 2 for eth0; adjust if assign_addresses changes.
ELEPHANT_ALT_ADDRS_MPQUIC = "10.0.0.6/2,10.0.0.7/2,10.0.0.8/2"  # TODO: validate against actual ifindex/IPs

# Destination rack for whitebox collision (h311 lives in pod 3, edge 1).
DST_POD = 3
DST_EDGE = 1
DST_AGG = 1  # Aggregation switch below c3 used for this rack.
DST_SUBNET = net_24(DST_POD, DST_EDGE)

# Core/uplink selection: force fwmark=0x1 traffic to traverse c3 -> a31.
C3_INDEX = 3
POLICY_TABLE = 100
FW_MARK = "0x1"

__all__ = [
    "run_whitebox_once",
    "configure_paths_for_whitebox",
]


def get_extra_args(proto: str, role: str) -> List[str]:
    """
    Return picoquicdemo extra CLI arguments based on proto and role.

    - quic: always single-path (no extra args).
    - mpquic:
        * Elephant server: -M
        * Elephant client: -M plus -A (client-only multi-IP advertisement)
        * Mouse server/client: keep single-path (no -M/-A)
    """
    proto = (proto or "").lower()
    if proto != "mpquic":
        return []

    if role == ROLE_ELEPHANT_SERVER:
        return ["-M"]
    if role == ROLE_ELEPHANT_CLIENT:
        args: List[str] = ["-M"]
        if ELEPHANT_ALT_ADDRS_MPQUIC:
            args += ["-A", ELEPHANT_ALT_ADDRS_MPQUIC]
        return args
    if role in (ROLE_MOUSE_SERVER, ROLE_MOUSE_CLIENT):
        return []
    return []


def configure_paths_for_whitebox(ctx, proto: str) -> None:
    """Configure fwmark-based policy routing so Elephant/Mouse collide at c3."""
    print(f"[whitebox] Configuring whitebox paths for proto={proto}")

    # Basic sanity checks to avoid crashes on unexpected k.
    n_hosts_per_edge = ctx.k // 2
    n_edges_per_pod = ctx.k // 2
    if ctx.k <= DST_POD or n_edges_per_pod <= DST_EDGE or n_hosts_per_edge <= 1:
        print("[whitebox] Topology smaller than expected; skipping policy routing setup.")
        return
    if len(ctx.cores) <= C3_INDEX:
        print("[whitebox] Core c3 not present; skipping policy routing setup.")
        return

    def _run(node, cmd: str) -> None:
        res = node.cmd(cmd)
        if res:
            print(f"[whitebox] {node.name}: {cmd.strip()} -> {res.strip()}")

    routers = list(ctx.cores)
    routers += [agg for pod_aggs in ctx.aggs for agg in pod_aggs]
    routers += [edge for pod_edges in ctx.edges for edge in pod_edges]

    def _ensure_nft_prerouting(node) -> None:
        # Create mangle table/PREROUTING chain if missing; ignore errors if they already exist.
        _run(node, "nft list table ip mangle 2>/dev/null || nft add table ip mangle")
        _run(
            node,
            "nft list chain ip mangle PREROUTING 2>/dev/null || "
            "nft 'add chain ip mangle PREROUTING { type filter hook prerouting priority mangle; policy accept; }'",
        )

    for node in routers:
        _ensure_nft_prerouting(node)
        for src_ip in (S_IP_ELEPHANT, S_IP_MOUSE):
            _run(
                node,
                f"nft add rule ip mangle PREROUTING ip protocol udp ip saddr {src_ip} meta mark set {FW_MARK} 2>/dev/null || true",
            )
        _run(node, f"ip rule add fwmark {FW_MARK} table {POLICY_TABLE} 2>/dev/null")

    routes = []

    def _edge_route_to_agg(pod: int, edge_idx: int, agg_idx: int) -> None:
        try:
            edge_node = ctx.edges[pod][edge_idx]
            _, agg_ip = ip_agg_edge(pod, agg_idx, edge_idx)
            intf = edge_node.intf(f"e{pod}{edge_idx}-to-a{pod}{agg_idx}")
        except IndexError:
            print(f"[whitebox] Missing edge/agg for pod={pod}, edge={edge_idx}, agg={agg_idx}")
            return
        if not intf:
            print(f"[whitebox] Interface e{pod}{edge_idx}-to-a{pod}{agg_idx} not found")
            return
        routes.append(
            (
                edge_node,
                f"ip route replace {DST_SUBNET} via {agg_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _agg_route_to_core(pod: int, agg_idx: int, core_idx: int) -> None:
        try:
            agg_node = ctx.aggs[pod][agg_idx]
        except IndexError:
            print(f"[whitebox] Missing agg for pod={pod}, agg={agg_idx}")
            return
        if core_idx >= len(ctx.cores):
            print(f"[whitebox] Core index {core_idx} missing")
            return
        agg_ip, core_ip = ip_core_agg(pod, agg_idx, core_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-c{core_idx}")
        if not intf:
            print(f"[whitebox] Interface a{pod}{agg_idx}-to-c{core_idx} not found")
            return
        routes.append(
            (
                agg_node,
                f"ip route replace {DST_SUBNET} via {core_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _core_route_to_agg(core_idx: int, pod: int, agg_idx: int) -> None:
        if core_idx >= len(ctx.cores):
            print(f"[whitebox] Core index {core_idx} missing")
            return
        core_node = ctx.cores[core_idx]
        agg_ip, _ = ip_core_agg(pod, agg_idx, core_idx)
        intf = core_node.intf(f"c{core_idx}-to-a{pod}{agg_idx}")
        if not intf:
            print(f"[whitebox] Interface c{core_idx}-to-a{pod}{agg_idx} not found")
            return
        routes.append(
            (
                core_node,
                f"ip route replace {DST_SUBNET} via {agg_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _agg_route_to_edge(pod: int, agg_idx: int, edge_idx: int) -> None:
        try:
            agg_node = ctx.aggs[pod][agg_idx]
        except IndexError:
            print(f"[whitebox] Missing agg for pod={pod}, agg={agg_idx}")
            return
        edge_ip, _ = ip_agg_edge(pod, agg_idx, edge_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-e{pod}{edge_idx}")
        if not intf:
            print(f"[whitebox] Interface a{pod}{agg_idx}-to-e{pod}{edge_idx} not found")
            return
        routes.append(
            (
                agg_node,
                f"ip route replace {DST_SUBNET} via {edge_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _edge_route_to_hosts(pod: int, edge_idx: int) -> None:
        try:
            edge_node = ctx.edges[pod][edge_idx]
        except IndexError:
            print(f"[whitebox] Missing edge for pod={pod}, edge={edge_idx}")
            return
        br = f"br_e{pod}{edge_idx}"
        routes.append(
            (
                edge_node,
                f"ip route replace {DST_SUBNET} dev {br} table {POLICY_TABLE}",
            )
        )

    # Forward path for Elephant (pod 0) and Mouse (pod 2) toward the c3 -> a31 uplink.
    _edge_route_to_agg(pod=0, edge_idx=0, agg_idx=DST_AGG)
    _agg_route_to_core(pod=0, agg_idx=DST_AGG, core_idx=C3_INDEX)
    _edge_route_to_agg(pod=2, edge_idx=0, agg_idx=DST_AGG)
    _agg_route_to_core(pod=2, agg_idx=DST_AGG, core_idx=C3_INDEX)

    # Downstream from c3 into the destination rack (pod 3, edge 1).
    _core_route_to_agg(core_idx=C3_INDEX, pod=DST_POD, agg_idx=DST_AGG)
    _agg_route_to_edge(pod=DST_POD, agg_idx=DST_AGG, edge_idx=DST_EDGE)
    _edge_route_to_hosts(pod=DST_POD, edge_idx=DST_EDGE)

    for node, cmd in routes:
        _run(node, cmd)


def _format_extra_args(args: List[str]) -> str:
    if not args:
        return ""
    return " ".join(shlex.quote(str(a)) for a in args if a)


def _start_picoquic_server(host, port: int, log_path: Path, template: str, extra_args: List[str]):
    cmd = template.format(
        port=port,
        log_path=shlex.quote(str(log_path)),
        extra=_format_extra_args(extra_args),
        cert=PICOQUIC_CERT_PATH,
        key=PICOQUIC_KEY_PATH,
    )
    return host.popen(cmd, shell=True)


def _terminate_processes(procs: List) -> None:
    for proc in procs:
        if not proc:
            continue
        try:
            proc.terminate()
        except Exception:
            continue
    for proc in procs:
        if not proc:
            continue
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _run_mouse_flows(
    host,
    server_ip: str,
    log_dir: Path,
    start_time: float,
    total_duration: float,
    stop_event: threading.Event,
    proc_store: List,
    extra_args: List[str],
    heartbeat_interval: float = 10.0,
    run_label: str = "",
):
    seq = 0
    last_heartbeat = start_time
    prefix = f"{run_label} " if run_label else ""
    while not stop_event.is_set():
        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            print(f"{prefix}[mouse-gen] alive t={now - start_time:.1f}s, flows={seq}")
            last_heartbeat = now
        if now >= start_time + total_duration:
            break

        sleep_time = random.expovariate(80.0)
        stop_event.wait(sleep_time)
        if stop_event.is_set():
            break
        if time.time() >= start_time + total_duration:
            break

        seq += 1
        csv_path = log_dir / f"mouse_client_{seq:04d}.csv"
        mouse_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=MOUSE_PORT,
            csv_path=csv_path,
            duration=1.0,
            extra_args=extra_args,
        )
        proc = host.popen(mouse_cmd, shell=True)
        proc_store.append(proc)


def run_whitebox_once(
    proto: str,
    k: int,
    duration: float,
    base_seed: int,
    run_index: int,
    total_runs: Optional[int] = None,
) -> None:
    """Execute one whitebox experiment run."""
    seed = base_seed + run_index
    random.seed(seed)

    run_tag = (
        f"[run {run_index + 1}/{total_runs}]" if total_runs is not None else f"[run {run_index + 1}]"
    )
    print(
        f"{run_tag} starting whitebox run (proto={proto}, k={k}, duration={duration}s, seed={seed})"
    )
    print(f"{run_tag} building topology...")

    log_dir = make_log_dir("whitebox", proto)

    ctx = None
    elephant_client_proc = None
    mouse_thread: Optional[threading.Thread] = None
    mouse_stop = threading.Event()
    mouse_procs: List = []
    server_procs: List = []

    try:
        ctx = create_fattree(k=k, bw_mbps=1000, delay="0.05ms", queue_pkts=75)
        print(f"{run_tag} topology ready.")

        elephant_client = ctx.net.get("h001")
        mouse_client = ctx.net.get("h201")
        server_host = ctx.net.get("h311")

        print(f"{run_tag} capturing switch stats (before).")
        before_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_before.json").write_text(
            json.dumps(before_stats, indent=2)
        )

        print(f"{run_tag} starting servers (elephant & mouse).")
        # Start picoquicdemo servers for Elephant and Mouse.
        elephant_server_log = log_dir / "elephant_server.log"
        mouse_server_log = log_dir / "mouse_server.log"
        elephant_server_args = get_extra_args(proto, ROLE_ELEPHANT_SERVER)
        mouse_server_args = get_extra_args(proto, ROLE_MOUSE_SERVER)
        server_procs.append(
            _start_picoquic_server(
                server_host,
                ELEPHANT_PORT,
                elephant_server_log,
                ELEPHANT_SERVER_CMD_TEMPLATE,
                elephant_server_args,
            )
        )
        server_procs.append(
            _start_picoquic_server(
                server_host,
                MOUSE_PORT,
                mouse_server_log,
                MOUSE_SERVER_CMD_TEMPLATE,
                mouse_server_args,
            )
        )

        configure_paths_for_whitebox(ctx, proto)
        print(f"{run_tag} policy routing configured.")

        server_ip = server_host.IP()
        total_duration = WARMUP_SECONDS + duration

        elephant_csv = log_dir / "elephant_client.csv"
        elephant_extra = get_extra_args(proto, ROLE_ELEPHANT_CLIENT)
        print(f"{run_tag} starting elephant client (duration={total_duration}s).")
        elephant_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=ELEPHANT_PORT,
            csv_path=elephant_csv,
            duration=total_duration,
            extra_args=elephant_extra,
        )
        elephant_client_proc = elephant_client.popen(elephant_cmd, shell=True)

        mouse_extra = get_extra_args(proto, ROLE_MOUSE_CLIENT)
        start_time = time.time()
        print(f"{run_tag} starting mouse generator thread.")
        mouse_thread = threading.Thread(
            target=_run_mouse_flows,
            args=(
                mouse_client,
                server_ip,
                log_dir,
                start_time,
                total_duration,
                mouse_stop,
                mouse_procs,
                mouse_extra,
                10.0,
                run_tag,
            ),
            daemon=True,
        )
        mouse_thread.start()

        print(f"{run_tag} traffic running; warmup+duration={total_duration}s.")
        time.sleep(total_duration)
        mouse_stop.set()
        if mouse_thread:
            mouse_thread.join()

        if elephant_client_proc:
            elephant_client_proc.wait(timeout=5)

        after_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_after.json").write_text(
            json.dumps(after_stats, indent=2)
        )
        print(f"{run_tag} switch stats captured (after).")
    finally:
        print(f"{run_tag} tearing down topology and processes...")
        mouse_stop.set()
        if mouse_thread and mouse_thread.is_alive():
            mouse_thread.join(timeout=3)
        _terminate_processes(mouse_procs + [elephant_client_proc] + server_procs)
        stop_fattree_topology(ctx)
        print(f"{run_tag} teardown complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Whitebox experiment driver.")
    parser.add_argument("--proto", required=True, choices=["quic", "mpquic"])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    for run_idx in range(args.runs):
        run_whitebox_once(
            proto=args.proto,
            k=args.k,
            duration=args.duration,
            base_seed=args.seed,
            run_index=run_idx,
            total_runs=args.runs,
        )


if __name__ == "__main__":
    main()
