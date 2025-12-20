"""Whitebox2 incast experiment: 1 Elephant vs 8 Mouse flows converging at c3.

This scenario keeps log/analysis compatibility with whitebox: outputs land under
logs/whitebox/default by default (override with --output-dir) and use the same
CSV/JSON filenames so analyze_whitebox.py works unchanged.
"""

import argparse
import json
import random
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

# Ensure the repository root is on sys.path when executed as a script.
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

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

WARMUP_SECONDS = 2
DEFAULT_DURATION = 20.0  # measurement window (total runtime = warmup + duration)
DEFAULT_KILL_GRACE_SECONDS = 10.0  # grace before SIGKILL when stopping processes
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
DEFAULT_SEED = 12345
DEFAULT_SCENARIO = "*1:1000:1000;"  # minimal valid perf scenario (1 stream, 1KB each way)
DEFAULT_LINK_BW_MBPS = 1000  # keep in sync with create_fattree call
DEFAULT_ELEPHANT_LOAD_FRAC = 0.7  # fraction of link capacity to target when auto-sizing Elephant payload
MOUSE_SIZE_MIN = 4 * 1024
MOUSE_SIZE_MAX = 64 * 1024
MOUSE_TARGET_FRAC_OF_ELEPHANT = 0.25  # 8:2 (elephant:mouse) byte ratio -> mouse is 1/4 of elephant
MOUSE_MEAN_SIZE_BYTES = (MOUSE_SIZE_MIN + MOUSE_SIZE_MAX) / 2

# Poisson rate per Mouse host (flows/sec), pre-computed to meet the 8:2 ratio under defaults:
# link=1Gbps, elephant load=0.7, warmup+duration=22s, mean Mouse size=34,816B, 8 Mouse hosts, ratio=0.25.
MOUSE_LAMBDA_PER_HOST = 78.53788488051471

# TODO: Adjust server options (certs/logging/paths) for actual experiments.
PICOQUIC_CERT_PATH = "/etc/picoquic/server-cert.pem"
PICOQUIC_KEY_PATH = "/etc/picoquic/server-key.pem"
ELEPHANT_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} {qlog_flag} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)
MOUSE_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} {qlog_flag} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)

# Roles for picoquic extra-arg selection.
ROLE_ELEPHANT_SERVER = "elephant-server"
ROLE_ELEPHANT_CLIENT = "elephant-client"
ROLE_MOUSE_SERVER = "mouse-server"
ROLE_MOUSE_CLIENT = "mouse-client"

# Hostnames used in this scenario (k=4 assumed unless overridden).
ELEPHANT_HOSTNAME = "h001"
MOUSE_HOSTNAMES = [
    "h100",
    "h101",
    "h110",
    "h111",
    "h200",
    "h201",
    "h210",
    "h211",
]
SERVER_HOSTNAME = "h311"

# Source IPs for marking (primary addresses of all senders).
S_IP_ELEPHANT = host_ip(0, 0, 1).split("/")[0]
MOUSE_SOURCE_COORDS = [
    (1, 0, 0),
    (1, 0, 1),
    (1, 1, 0),
    (1, 1, 1),
    (2, 0, 0),
    (2, 0, 1),
    (2, 1, 0),
    (2, 1, 1),
]
S_IP_MICE = [host_ip(p, e, h).split("/")[0] for (p, e, h) in MOUSE_SOURCE_COORDS]
SRC_IPS_FOR_MARK = [S_IP_ELEPHANT] + S_IP_MICE

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
    "run_whitebox2_once",
    "configure_paths_for_whitebox2",
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


def configure_paths_for_whitebox2(ctx, proto: str) -> None:
    """Configure fwmark-based policy routing so all senders collide at c3."""
    print(f"[whitebox2] Configuring whitebox2 paths for proto={proto}")

    n_hosts_per_edge = ctx.k // 2
    n_edges_per_pod = ctx.k // 2
    if ctx.k <= DST_POD or n_edges_per_pod <= DST_EDGE or n_hosts_per_edge <= 1:
        print("[whitebox2] Topology smaller than expected; skipping policy routing setup.")
        return
    if len(ctx.cores) <= C3_INDEX:
        print("[whitebox2] Core c3 not present; skipping policy routing setup.")
        return

    def _run(node, cmd: str) -> None:
        res = node.cmd(cmd)
        if res:
            print(f"[whitebox2] {node.name}: {cmd.strip()} -> {res.strip()}")

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
        for src_ip in SRC_IPS_FOR_MARK:
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
            print(f"[whitebox2] Missing edge/agg for pod={pod}, edge={edge_idx}, agg={agg_idx}")
            return
        if not intf:
            print(f"[whitebox2] Interface e{pod}{edge_idx}-to-a{pod}{agg_idx} not found")
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
            print(f"[whitebox2] Missing agg for pod={pod}, agg={agg_idx}")
            return
        if core_idx >= len(ctx.cores):
            print(f"[whitebox2] Core index {core_idx} missing")
            return
        agg_ip, core_ip = ip_core_agg(pod, agg_idx, core_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-c{core_idx}")
        if not intf:
            print(f"[whitebox2] Interface a{pod}{agg_idx}-to-c{core_idx} not found")
            return
        routes.append(
            (
                agg_node,
                f"ip route replace {DST_SUBNET} via {core_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _core_route_to_agg(core_idx: int, pod: int, agg_idx: int) -> None:
        if core_idx >= len(ctx.cores):
            print(f"[whitebox2] Core index {core_idx} missing")
            return
        core_node = ctx.cores[core_idx]
        agg_ip, _ = ip_core_agg(pod, agg_idx, core_idx)
        intf = core_node.intf(f"c{core_idx}-to-a{pod}{agg_idx}")
        if not intf:
            print(f"[whitebox2] Interface c{core_idx}-to-a{pod}{agg_idx} not found")
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
            print(f"[whitebox2] Missing agg for pod={pod}, agg={agg_idx}")
            return
        edge_ip, _ = ip_agg_edge(pod, agg_idx, edge_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-e{pod}{edge_idx}")
        if not intf:
            print(f"[whitebox2] Interface a{pod}{agg_idx}-to-e{pod}{edge_idx} not found")
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
            print(f"[whitebox2] Missing edge for pod={pod}, edge={edge_idx}")
            return
        br = f"br_e{pod}{edge_idx}"
        routes.append(
            (
                edge_node,
                f"ip route replace {DST_SUBNET} dev {br} table {POLICY_TABLE}",
            )
        )

    # Forward paths for Elephant (pod 0) and Mouse sources (pods 1 and 2, edges 0/1) toward c3 -> a31.
    source_edges = [(0, 0), (1, 0), (1, 1), (2, 0), (2, 1)]
    for pod, edge_idx in source_edges:
        _edge_route_to_agg(pod=pod, edge_idx=edge_idx, agg_idx=DST_AGG)
        _agg_route_to_core(pod=pod, agg_idx=DST_AGG, core_idx=C3_INDEX)

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


def _start_picoquic_server(
    host, port: int, log_path: Path, template: str, extra_args: List[str], enable_qlog: bool
):
    qlog_flag = ""
    if enable_qlog:
        qlog_path = Path(f"{log_path}.qlog")
        qlog_flag = f"-l {shlex.quote(str(qlog_path))}"
    cmd = template.format(
        port=port,
        log_path=shlex.quote(str(log_path)),
        qlog_flag=qlog_flag,
        extra=_format_extra_args(extra_args),
        cert=PICOQUIC_CERT_PATH,
        key=PICOQUIC_KEY_PATH,
    )
    return host.popen(cmd, shell=True)


def _terminate_processes(procs: List, term_timeout: float = 3.0) -> None:
    procs = [proc for proc in procs if proc]
    if not procs:
        return
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            continue
    deadline = time.time() + max(term_timeout, 0.0)
    while time.time() < deadline:
        if all(proc.poll() is not None for proc in procs):
            return
        time.sleep(0.1)
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _wait_for_completion_then_terminate(
    proc, wait_timeout: float, label: str = "", term_timeout: float = 3.0
) -> None:
    """Wait for a proc to exit; send SIGTERM/SIGKILL only if it overruns."""
    if not proc:
        return
    prefix = f"{label} " if label else ""
    try:
        proc.wait(timeout=wait_timeout)
        return
    except subprocess.TimeoutExpired:
        print(f"{prefix}still running after {wait_timeout}s; sending SIGTERM.")
    except Exception as exc:
        print(f"{prefix}error while waiting: {exc}; sending SIGTERM.")
    try:
        proc.terminate()
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        print(f"{prefix}still alive after SIGTERM; sending SIGKILL.")
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_mouse_flows(
    host,
    host_label: str,
    server_ip: str,
    log_dir: Path,
    start_time: float,
    total_duration: float,
    stop_event: threading.Event,
    proc_store: List,
    extra_args: List[str],
    lambda_rate: float,
    heartbeat_interval: float = 10.0,
    run_label: str = "",
):
    seq = 0
    last_heartbeat = start_time
    prefix = f"{run_label} " if run_label else ""
    while not stop_event.is_set():
        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            print(f"{prefix}[mouse-gen:{host_label}] alive t={now - start_time:.1f}s, flows={seq}")
            last_heartbeat = now
        if now >= start_time + total_duration:
            break

        sleep_time = random.expovariate(lambda_rate)
        stop_event.wait(sleep_time)
        if stop_event.is_set():
            break
        if time.time() >= start_time + total_duration:
            break

        seq += 1
        csv_path = log_dir / f"mouse_client_{host_label}_{seq:04d}.csv"
        size_bytes = random.randint(MOUSE_SIZE_MIN, MOUSE_SIZE_MAX)
        scenario = f"*1:{size_bytes}:0;"
        mouse_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=MOUSE_PORT,
            csv_path=csv_path,
            scenario=scenario,
            extra_args=extra_args,
        )
        proc = host.popen(mouse_cmd, shell=True)
        proc_store.append(proc)


def _healthcheck(
    server_host,
    client_host,
    server_ip: str,
    port: int,
    log_dir: Path,
    label: str,
    run_tag: str = "",
    proto: str = "",
) -> None:
    """
    Quick pre-flight to validate server process, IP reachability, and QUIC handshake.
    Raises RuntimeError on failure so the main run aborts before starting traffic.
    """
    prefix = f"{run_tag} " if run_tag else ""
    log_path = log_dir / f"healthcheck_{label}.log"
    lines: List[str] = []

    def _log(msg: str) -> None:
        print(f"{prefix}{msg}")
        lines.append(msg)

    # 1) Server process presence
    procs = server_host.cmd("pgrep -a picoquicdemo")
    procs_clean = procs.strip()
    _log(f"[health:{label}] server procs: {procs_clean or 'none'}")
    server_ok = bool(procs_clean)

    # 2) IP reachability
    ping_out = client_host.cmd(f"ping -c1 -W1 {server_ip}; echo HC_RC=$?")
    _log(f"[health:{label}] ping ->\n{ping_out.strip()}")
    ping_ok = "HC_RC=0" in ping_out

    # 3) QUIC handshake (timeout guards against hanging)
    hc_timeout = 10  # generous to allow small perf exchange to complete
    scenario = DEFAULT_SCENARIO  # minimal bidir traffic to force a quick exit
    is_elephant = label.startswith("elephant")
    client_extra = get_extra_args(proto, ROLE_ELEPHANT_CLIENT if is_elephant else ROLE_MOUSE_CLIENT)
    extra_str = _format_extra_args(client_extra)
    hc_cmd = (
        f"timeout {hc_timeout} "
        f"picoquicdemo -a perf {extra_str} {server_ip} {port} {shlex.quote(scenario)}"
    )
    hc_out = client_host.cmd(f"{hc_cmd}; echo HC_RC=$?")
    _log(f"[health:{label}] quic (scenario={scenario}, extra_args={extra_str or 'none'}) ->\n{hc_out.strip()}")

    hc_exit_match = re.search(r"Client exit with code\s*=\s*(-?\d+)", hc_out)
    client_exit_code = int(hc_exit_match.group(1)) if hc_exit_match else None
    quic_ok = "HC_RC=0" in hc_out and client_exit_code == 0

    log_path.write_text("\n".join(lines) + "\n")

    if not (server_ok and ping_ok and quic_ok):
        raise RuntimeError(
            f"healthcheck {label} failed (server_ok={server_ok}, ping_ok={ping_ok}, quic_ok={quic_ok}); "
            f"see {log_path}"
        )


def run_whitebox2_once(
    proto: str,
    k: int,
    duration: float,
    base_seed: int,
    run_index: int,
    elephant_bytes: Optional[int],
    elephant_load_fraction: float,
    total_runs: Optional[int] = None,
    enable_qlog: bool = False,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    output_subdir: Optional[Path] = None,
) -> Path:
    """Execute one whitebox2 experiment run and return the log directory."""
    seed = base_seed + run_index
    random.seed(seed)

    run_tag = (
        f"[run {run_index + 1}/{total_runs}]" if total_runs is not None else f"[run {run_index + 1}]"
    )
    print(
        f"{run_tag} starting whitebox2 run (proto={proto}, k={k}, duration={duration}s, seed={seed})"
    )
    print(f"{run_tag} building topology...")

    log_root = Path("logs/whitebox") / (output_subdir or Path("default"))
    log_dir = make_log_dir("whitebox", proto, log_root=log_root)

    ctx = None
    elephant_client_proc = None
    mouse_threads: List[threading.Thread] = []
    mouse_stop = threading.Event()
    mouse_procs: List = []
    server_procs: List = []

    try:
        ctx = create_fattree(k=k, bw_mbps=DEFAULT_LINK_BW_MBPS, delay="0.05ms", queue_pkts=75)
        print(f"{run_tag} topology ready.")

        elephant_client = ctx.net.get(ELEPHANT_HOSTNAME)
        mouse_clients = [ctx.net.get(name) for name in MOUSE_HOSTNAMES]
        server_host = ctx.net.get(SERVER_HOSTNAME)

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
                enable_qlog,
            )
        )
        server_procs.append(
            _start_picoquic_server(
                server_host,
                MOUSE_PORT,
                mouse_server_log,
                MOUSE_SERVER_CMD_TEMPLATE,
                mouse_server_args,
                enable_qlog,
            )
        )

        configure_paths_for_whitebox2(ctx, proto)
        print(f"{run_tag} policy routing configured.")

        server_ip = server_host.IP()

        # Pre-flight health checks (abort if connectivity/handshake fails).
        print(f"{run_tag} running health checks...")
        _healthcheck(
            server_host=server_host,
            client_host=elephant_client,
            server_ip=server_ip,
            port=ELEPHANT_PORT,
            log_dir=log_dir,
            label="elephant",
            run_tag=run_tag,
            proto=proto,
        )
        for mc in mouse_clients:
            _healthcheck(
                server_host=server_host,
                client_host=mc,
                server_ip=server_ip,
                port=MOUSE_PORT,
                log_dir=log_dir,
                label=f"mouse_{mc.name}",
                run_tag=run_tag,
                proto=proto,
            )
        print(f"{run_tag} health checks passed.")

        total_duration = WARMUP_SECONDS + duration
        elephant_target_bytes = elephant_bytes
        if elephant_target_bytes is None:
            link_bps = DEFAULT_LINK_BW_MBPS * 1_000_000
            elephant_target_bytes = int(elephant_load_fraction * link_bps / 8 * total_duration)
        if elephant_target_bytes <= 0:
            raise ValueError("elephant_bytes must be positive when provided or computed.")
        elephant_scenario = f"*1:{elephant_target_bytes}:0;"

        elephant_csv = log_dir / "elephant_client.csv"
        elephant_extra = get_extra_args(proto, ROLE_ELEPHANT_CLIENT)
        print(
            f"{run_tag} starting elephant client "
            f"(duration={total_duration}s, payload_bytes={elephant_target_bytes})."
        )
        elephant_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=ELEPHANT_PORT,
            csv_path=elephant_csv,
            scenario=elephant_scenario,
            extra_args=elephant_extra,
        )
        elephant_client_proc = elephant_client.popen(elephant_cmd, shell=True)

        mouse_extra = get_extra_args(proto, ROLE_MOUSE_CLIENT)
        start_time = time.time()
        print(f"{run_tag} starting mouse generator threads for {len(mouse_clients)} hosts.")
        for mc in mouse_clients:
            thread = threading.Thread(
                target=_run_mouse_flows,
                args=(
                    mc,
                    mc.name,
                    server_ip,
                    log_dir,
                    start_time,
                    total_duration,
                    mouse_stop,
                    mouse_procs,
                    mouse_extra,
                    MOUSE_LAMBDA_PER_HOST,
                    10.0,
                    run_tag,
                ),
                daemon=True,
            )
            thread.start()
            mouse_threads.append(thread)

        print(f"{run_tag} traffic running; warmup+duration={total_duration}s.")
        time.sleep(total_duration)
        mouse_stop.set()
        for thread in mouse_threads:
            thread.join()

        print(f"{run_tag} stopping clients (kill grace={kill_grace_seconds}s).")
        _terminate_processes(
            mouse_procs + [elephant_client_proc],
            term_timeout=kill_grace_seconds,
        )

        after_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_after.json").write_text(
            json.dumps(after_stats, indent=2)
        )
        print(f"{run_tag} switch stats captured (after).")
    finally:
        print(f"{run_tag} tearing down topology and processes...")
        mouse_stop.set()
        for thread in mouse_threads:
            if thread.is_alive():
                thread.join(timeout=3)
        _terminate_processes(
            mouse_procs + [elephant_client_proc] + server_procs,
            term_timeout=kill_grace_seconds,
        )
        stop_fattree_topology(ctx)
        print(f"{run_tag} teardown complete.")

    return log_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Whitebox2 incast experiment driver.")
    parser.add_argument("--proto", required=True, choices=["quic", "mpquic"])
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("default"),
        help=(
            "Subdirectory name under logs/whitebox. Each run writes to "
            "logs/whitebox/<output-dir>/<proto>/run_<timestamp> (default: default)."
        ),
    )
    parser.add_argument(
        "--elephant-bytes",
        type=int,
        default=None,
        help="Total payload bytes for the Elephant perf scenario; overrides auto-sizing.",
    )
    parser.add_argument(
        "--elephant-load-frac",
        type=float,
        default=DEFAULT_ELEPHANT_LOAD_FRAC,
        help="If --elephant-bytes is unset, fraction of link capacity to target (default 0.7).",
    )
    parser.add_argument(
        "--enable-qlog",
        action="store_true",
        help="Enable picoquicdemo -l qlog capture for servers (default: disabled for performance).",
    )
    parser.add_argument(
        "--kill-grace",
        type=float,
        default=DEFAULT_KILL_GRACE_SECONDS,
        help="Seconds to wait after SIGTERM before SIGKILL when stopping clients/servers.",
    )
    args = parser.parse_args()

    for run_idx in range(args.runs):
        run_whitebox2_once(
            proto=args.proto,
            k=args.k,
            duration=args.duration,
            base_seed=args.seed,
            run_index=run_idx,
            elephant_bytes=args.elephant_bytes,
            elephant_load_fraction=args.elephant_load_frac,
            total_runs=args.runs,
            enable_qlog=args.enable_qlog,
            kill_grace_seconds=args.kill_grace,
            output_subdir=args.output_dir,
        )


if __name__ == "__main__":
    main()
