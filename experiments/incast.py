"""Incast experiment: 1 Elephant vs 24 Mouse flows converging at c5.

This scenario keeps log/analysis compatibility with whitebox: outputs land under
logs/incast/default by default (override with --output-dir) and use the same
CSV/JSON filenames so analyze_whitebox.py can be reused if pointed there.
"""

import argparse
import json
import math
import random
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

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
DEFAULT_DURATION = 20.0  # measurement window used for auto-sizing Elephant payload
DEFAULT_KILL_GRACE_SECONDS = 10.0  # grace before SIGKILL when stopping processes
SERVER_IDLE_TIMEOUT_MS = 5000
CONGESTION_CONTROL = "cubic"
ELEPHANT_PROGRESS_INTERVAL = 10.0
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
DEFAULT_SEED = 12345
DEFAULT_SCENARIO = "*1:1000:1000;"  # minimal valid perf scenario (1 stream, 1KB each way)
DEFAULT_LINK_BW_MBPS = 1000  # keep in sync with create_fattree call
DEFAULT_ELEPHANT_LOAD_FRAC = 0.7  # fraction of link capacity to target when auto-sizing Elephant payload
MOUSE_SIZE_BYTES = 64 * 1024
MOUSE_PERIOD_S = 0.20
MOUSE_JITTER_STD_S = 0.0
MOUSE_JITTER_CLIP_S = 0.0
MOUSE_START_DELAY_S = 0.050

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

# Hostnames used in this scenario (k=6 default, requires k>=6).
ELEPHANT_HOSTNAME = "h000"
MOUSE_HOSTNAMES = [
    "h100",
    "h102",
    "h110",
    "h101",
    "h111",
    "h112",
    "h200",
    "h201",
    "h202",
    "h210",
    "h211",
    "h212",
    "h300",
    "h301",
    "h302",
    "h310",
    "h311",
    "h312",
    "h400",
    "h402",
    "h410",
    "h401",
    "h411",
    "h412",
]
SERVER_HOSTNAME = "h522"

# Source IPs for marking (primary addresses of all senders only).
def _host_coords_from_name(hostname: str):
    match = re.fullmatch(r"h(\d)(\d)(\d)", hostname)
    if not match:
        raise ValueError(f"Unexpected host name format: {hostname}")
    return tuple(int(part) for part in match.groups())


S_IP_ELEPHANT = host_ip(*_host_coords_from_name(ELEPHANT_HOSTNAME)).split("/")[0]
MOUSE_SOURCE_COORDS = [_host_coords_from_name(name) for name in MOUSE_HOSTNAMES]
S_IP_MICE = [host_ip(p, e, h).split("/")[0] for (p, e, h) in MOUSE_SOURCE_COORDS]
SRC_IPS_FOR_MARK = [S_IP_ELEPHANT] + S_IP_MICE

# Multipath QUIC: advertise client-only extra addresses (do not include S_IP_ELEPHANT).
# Mininet hosts typically use ifindex 2 for eth0; adjust if assign_addresses changes.
ELEPHANT_ALT_ADDRS_MPQUIC = "10.0.0.2/2,10.0.0.3/2,10.0.0.4/2"  # TODO: validate against actual ifindex/IPs

# Destination rack for whitebox collision (h522 lives in pod 5, edge 2).
DST_POD = 5
DST_EDGE = 2
DST_AGG = 2  # Aggregation switch below c5 used for this rack.
DST_SUBNET = net_24(DST_POD, DST_EDGE)

# Core/uplink selection: force fwmark=0x1 traffic to traverse c5 -> a52.
C5_INDEX = 5
POLICY_TABLE = 100
FW_MARK = "0x1"

__all__ = [
    "run_incast_once",
    "configure_paths_for_incast",
]


def get_extra_args(proto: str, role: str) -> List[str]:
    """
    Return picoquicdemo extra CLI arguments based on proto and role.

    - quic: single-path, but always enforce congestion control selection.
    - mpquic:
        * Elephant server: -M
        * Elephant client: -M plus -A (client-only multi-IP advertisement)
        * Mouse server/client: keep single-path (no -M/-A)
    """
    proto = (proto or "").lower()
    base_args: List[str] = []
    if CONGESTION_CONTROL:
        base_args = ["-G", CONGESTION_CONTROL]
    if proto != "mpquic":
        return base_args

    if role == ROLE_ELEPHANT_SERVER:
        return base_args + ["-M"]
    if role == ROLE_ELEPHANT_CLIENT:
        args: List[str] = base_args + ["-M"]
        if ELEPHANT_ALT_ADDRS_MPQUIC:
            args += ["-A", ELEPHANT_ALT_ADDRS_MPQUIC]
        return args
    if role in (ROLE_MOUSE_SERVER, ROLE_MOUSE_CLIENT):
        return base_args
    return base_args


def configure_paths_for_incast(ctx, proto: str) -> None:
    """Configure fwmark-based policy routing so all senders collide at c5."""
    print(f"[incast] Configuring incast paths for proto={proto}")

    n_hosts_per_edge = ctx.k // 2
    n_edges_per_pod = ctx.k // 2
    if ctx.k <= DST_POD or n_edges_per_pod <= DST_EDGE or n_hosts_per_edge <= 1:
        print("[incast] Topology smaller than expected; skipping policy routing setup.")
        return
    if len(ctx.cores) <= C5_INDEX:
        print("[incast] Core c5 not present; skipping policy routing setup.")
        return

    def _run(node, cmd: str) -> None:
        res = node.cmd(cmd)
        if res:
            print(f"[incast] {node.name}: {cmd.strip()} -> {res.strip()}")

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
            print(f"[incast] Missing edge/agg for pod={pod}, edge={edge_idx}, agg={agg_idx}")
            return
        if not intf:
            print(f"[incast] Interface e{pod}{edge_idx}-to-a{pod}{agg_idx} not found")
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
            print(f"[incast] Missing agg for pod={pod}, agg={agg_idx}")
            return
        if core_idx >= len(ctx.cores):
            print(f"[incast] Core index {core_idx} missing")
            return
        agg_ip, core_ip = ip_core_agg(pod, agg_idx, core_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-c{core_idx}")
        if not intf:
            print(f"[incast] Interface a{pod}{agg_idx}-to-c{core_idx} not found")
            return
        routes.append(
            (
                agg_node,
                f"ip route replace {DST_SUBNET} via {core_ip.split('/')[0]} dev {intf.name} table {POLICY_TABLE}",
            )
        )

    def _core_route_to_agg(core_idx: int, pod: int, agg_idx: int) -> None:
        if core_idx >= len(ctx.cores):
            print(f"[incast] Core index {core_idx} missing")
            return
        core_node = ctx.cores[core_idx]
        agg_ip, _ = ip_core_agg(pod, agg_idx, core_idx)
        intf = core_node.intf(f"c{core_idx}-to-a{pod}{agg_idx}")
        if not intf:
            print(f"[incast] Interface c{core_idx}-to-a{pod}{agg_idx} not found")
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
            print(f"[incast] Missing agg for pod={pod}, agg={agg_idx}")
            return
        edge_ip, _ = ip_agg_edge(pod, agg_idx, edge_idx)
        intf = agg_node.intf(f"a{pod}{agg_idx}-to-e{pod}{edge_idx}")
        if not intf:
            print(f"[incast] Interface a{pod}{agg_idx}-to-e{pod}{edge_idx} not found")
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
            print(f"[incast] Missing edge for pod={pod}, edge={edge_idx}")
            return
        br = f"br_e{pod}{edge_idx}"
        routes.append(
            (
                edge_node,
                f"ip route replace {DST_SUBNET} dev {br} table {POLICY_TABLE}",
            )
        )

    # Forward paths for Elephant (pod 0) and Mouse sources (pods 1-4, edges 0/1) toward c5 -> a52.
    source_edges = [
        (0, 0),
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
        (3, 0),
        (3, 1),
        (4, 0),
        (4, 1),
    ]
    for pod, edge_idx in source_edges:
        _edge_route_to_agg(pod=pod, edge_idx=edge_idx, agg_idx=DST_AGG)
        _agg_route_to_core(pod=pod, agg_idx=DST_AGG, core_idx=C5_INDEX)

    # Downstream from c5 into the destination rack (pod 5, edge 2).
    _core_route_to_agg(core_idx=C5_INDEX, pod=DST_POD, agg_idx=DST_AGG)
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


def _select_primary_intf(host) -> Optional[str]:
    if not host:
        return None
    for intf in host.intfList():
        name = getattr(intf, "name", None)
        if name and name != "lo":
            return name
    return None


def _read_host_tx_bytes(host, intf_name: str) -> Optional[int]:
    if not host or not intf_name:
        return None
    raw = host.cmd(f"cat /sys/class/net/{intf_name}/statistics/tx_bytes 2>/dev/null").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _log_elephant_progress(
    host,
    proc,
    target_bytes: int,
    stop_event: threading.Event,
    interval: float = ELEPHANT_PROGRESS_INTERVAL,
    run_label: str = "",
) -> None:
    prefix = f"{run_label} " if run_label else ""
    # Use interface tx_bytes as a coarse proxy for Elephant send progress.
    intf_name = _select_primary_intf(host)
    if not intf_name:
        print(f"{prefix}[elephant-progress] no interface found; progress logging disabled.")
        return
    start_bytes = _read_host_tx_bytes(host, intf_name)
    if start_bytes is None:
        print(f"{prefix}[elephant-progress] tx_bytes unavailable; progress logging disabled.")
        return
    next_log = time.time() + max(interval, 1.0)
    while not stop_event.is_set():
        if proc and proc.poll() is not None:
            break
        now = time.time()
        if now >= next_log:
            current_bytes = _read_host_tx_bytes(host, intf_name)
            if current_bytes is not None and target_bytes > 0:
                sent_bytes = max(0, current_bytes - start_bytes)
                progress = min(sent_bytes / target_bytes, 1.0)
                print(
                    f"{prefix}[elephant-progress] sent={sent_bytes} bytes "
                    f"({progress * 100:.1f}%), target={target_bytes}"
                )
            next_log = now + max(interval, 1.0)
        stop_event.wait(0.5)

    current_bytes = _read_host_tx_bytes(host, intf_name)
    if current_bytes is not None and target_bytes > 0:
        sent_bytes = max(0, current_bytes - start_bytes)
        progress = min(sent_bytes / target_bytes, 1.0)
        print(
            f"{prefix}[elephant-progress] final sent={sent_bytes} bytes "
            f"({progress * 100:.1f}%), target={target_bytes}"
        )


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


def _summarize_lateness_ms(values: List[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    vals = sorted(values)
    count = len(vals)
    mean = sum(vals) / count
    p95_idx = max(0, math.ceil(0.95 * count) - 1)
    p95 = vals[p95_idx]
    return {
        "count": float(count),
        "min_ms": vals[0],
        "mean_ms": mean,
        "p95_ms": p95,
        "max_ms": vals[-1],
        "gt1ms": float(sum(1 for v in vals if v > 1.0)),
        "gt2ms": float(sum(1 for v in vals if v > 2.0)),
        "gt4ms": float(sum(1 for v in vals if v > 4.0)),
    }


def _write_mouse_thread_start_log(
    log_dir: Path,
    start_time: float,
    grid_t0: float,
    schedule_stats: List[Dict[str, object]],
) -> None:
    if not schedule_stats:
        return
    grid_offset = grid_t0 - start_time
    lines = ["host,thread_start_offset_s,grid_t0_offset_s"]
    for stats in schedule_stats:
        host = stats.get("host", "")
        thread_start = stats.get("thread_start_s")
        if thread_start is None:
            continue
        offset = float(thread_start) - start_time
        lines.append(f"{host},{offset:.6f},{grid_offset:.6f}")
    (log_dir / "mouse_thread_start.csv").write_text("\n".join(lines) + "\n")


def _write_mouse_schedule_summary(
    log_dir: Path,
    start_time: float,
    grid_t0: float,
    schedule_stats: List[Dict[str, object]],
) -> None:
    if not schedule_stats:
        return
    lines: List[str] = []
    grid_offset = grid_t0 - start_time
    lines.append("Mouse schedule summary")
    lines.append(f"grid_t0_offset_s={grid_offset:.6f}")
    all_values: List[float] = []
    for stats in schedule_stats:
        host = stats.get("host", "")
        values = stats.get("lateness_ms") or []
        all_values.extend(values)
        summary = _summarize_lateness_ms(values)
        if summary is None:
            lines.append(f"{host}: no data")
            continue
        lines.append(
            f"{host}: count={int(summary['count'])} "
            f"min_ms={summary['min_ms']:.3f} "
            f"mean_ms={summary['mean_ms']:.3f} "
            f"p95_ms={summary['p95_ms']:.3f} "
            f"max_ms={summary['max_ms']:.3f} "
            f"gt1ms={int(summary['gt1ms'])} "
            f"gt2ms={int(summary['gt2ms'])} "
            f"gt4ms={int(summary['gt4ms'])}"
        )
    lines.append("")
    overall = _summarize_lateness_ms(all_values)
    if overall is None:
        lines.append("overall: no data")
    else:
        lines.append(
            f"overall: count={int(overall['count'])} "
            f"min_ms={overall['min_ms']:.3f} "
            f"mean_ms={overall['mean_ms']:.3f} "
            f"p95_ms={overall['p95_ms']:.3f} "
            f"max_ms={overall['max_ms']:.3f} "
            f"gt1ms={int(overall['gt1ms'])} "
            f"gt2ms={int(overall['gt2ms'])} "
            f"gt4ms={int(overall['gt4ms'])}"
        )
    (log_dir / "mouse_schedule_summary.txt").write_text("\n".join(lines) + "\n")


def _run_mouse_flows(
    host,
    host_label: str,
    server_ip: str,
    log_dir: Path,
    grid_t0: float,
    start_time: float,
    total_duration: Optional[float],
    stop_event: threading.Event,
    proc_store: List,
    extra_args: List[str],
    heartbeat_interval: float = 10.0,
    run_label: str = "",
    schedule_stats: Optional[Dict[str, object]] = None,
):
    seq = 0
    last_heartbeat = start_time
    has_duration = total_duration is not None
    prefix = f"{run_label} " if run_label else ""
    if schedule_stats is not None:
        schedule_stats["thread_start_s"] = time.monotonic()
        schedule_stats.setdefault("lateness_ms", [])
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            print(f"{prefix}[mouse-gen:{host_label}] alive t={now - start_time:.1f}s, flows={seq}")
            last_heartbeat = now
        if has_duration and now >= start_time + total_duration:
            break

        if now < grid_t0:
            base_time = grid_t0
        else:
            n = math.floor((now - grid_t0) / MOUSE_PERIOD_S) + 1
            base_time = grid_t0 + n * MOUSE_PERIOD_S

        jitter = random.gauss(0.0, MOUSE_JITTER_STD_S)
        if jitter > MOUSE_JITTER_CLIP_S:
            jitter = MOUSE_JITTER_CLIP_S
        elif jitter < -MOUSE_JITTER_CLIP_S:
            jitter = -MOUSE_JITTER_CLIP_S

        send_time = base_time + jitter
        wait_time = send_time - now
        if wait_time > 0:
            stop_event.wait(wait_time)
        if stop_event.is_set():
            break
        if has_duration and time.monotonic() >= start_time + total_duration:
            break

        actual_time = time.monotonic()
        if schedule_stats is not None:
            schedule_stats["lateness_ms"].append((actual_time - send_time) * 1000.0)

        seq += 1
        csv_path = log_dir / f"mouse_client_{host_label}_{seq:04d}.csv"
        scenario = f"*1:{MOUSE_SIZE_BYTES}:0;"
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


def run_incast_once(
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
    """Execute one incast experiment run and return the log directory."""
    seed = base_seed + run_index
    random.seed(seed)

    run_tag = (
        f"[run {run_index + 1}/{total_runs}]" if total_runs is not None else f"[run {run_index + 1}]"
    )
    print(
        f"{run_tag} starting incast run (proto={proto}, k={k}, duration={duration}s, seed={seed})"
    )
    print(f"{run_tag} building topology...")

    log_root = Path("logs/incast") / (output_subdir or Path("default"))
    log_dir = make_log_dir("incast", proto, log_root=log_root)

    ctx = None
    elephant_client_proc = None
    elephant_server_proc = None
    mouse_server_proc = None
    elephant_progress_stop = threading.Event()
    elephant_progress_thread: Optional[threading.Thread] = None
    mouse_threads: List[threading.Thread] = []
    mouse_stop = threading.Event()
    mouse_procs: List = []
    server_procs: List = []
    mouse_schedule_stats: List[Dict[str, object]] = []
    start_time: Optional[float] = None
    grid_t0: Optional[float] = None

    try:
        ctx = create_fattree(k=k, bw_mbps=DEFAULT_LINK_BW_MBPS, delay="0.05ms", queue_pkts=50)
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
        elephant_server_args = get_extra_args(proto, ROLE_ELEPHANT_SERVER) + [
            "-d",
            str(SERVER_IDLE_TIMEOUT_MS),
        ]
        mouse_server_args = get_extra_args(proto, ROLE_MOUSE_SERVER) + [
            "-d",
            str(SERVER_IDLE_TIMEOUT_MS),
        ]
        elephant_server_proc = _start_picoquic_server(
            server_host,
            ELEPHANT_PORT,
            elephant_server_log,
            ELEPHANT_SERVER_CMD_TEMPLATE,
            elephant_server_args,
            enable_qlog,
        )
        mouse_server_proc = _start_picoquic_server(
            server_host,
            MOUSE_PORT,
            mouse_server_log,
            MOUSE_SERVER_CMD_TEMPLATE,
            mouse_server_args,
            enable_qlog,
        )
        server_procs.extend([elephant_server_proc, mouse_server_proc])

        configure_paths_for_incast(ctx, proto)
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
            f"(target_duration={total_duration}s, payload_bytes={elephant_target_bytes})."
        )
        elephant_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=ELEPHANT_PORT,
            csv_path=elephant_csv,
            scenario=elephant_scenario,
            extra_args=elephant_extra,
        )
        elephant_client_proc = elephant_client.popen(elephant_cmd, shell=True)
        if elephant_client_proc:
            elephant_progress_thread = threading.Thread(
                target=_log_elephant_progress,
                args=(
                    elephant_client,
                    elephant_client_proc,
                    elephant_target_bytes,
                    elephant_progress_stop,
                ),
                kwargs={"interval": ELEPHANT_PROGRESS_INTERVAL, "run_label": run_tag},
                daemon=True,
            )
            elephant_progress_thread.start()

        mouse_extra = get_extra_args(proto, ROLE_MOUSE_CLIENT)
        start_time = time.monotonic()
        grid_t0 = math.ceil((start_time + MOUSE_START_DELAY_S) / MOUSE_PERIOD_S) * MOUSE_PERIOD_S
        print(f"{run_tag} starting mouse generator threads for {len(mouse_clients)} hosts.")
        for mc in mouse_clients:
            stats: Dict[str, object] = {
                "host": mc.name,
                "thread_start_s": None,
                "lateness_ms": [],
            }
            mouse_schedule_stats.append(stats)
            thread = threading.Thread(
                target=_run_mouse_flows,
                args=(
                    mc,
                    mc.name,
                    server_ip,
                    log_dir,
                    grid_t0,
                    start_time,
                    None,
                    mouse_stop,
                    mouse_procs,
                    mouse_extra,
                ),
                kwargs={"heartbeat_interval": 10.0, "run_label": run_tag, "schedule_stats": stats},
                daemon=True,
            )
            thread.start()
            mouse_threads.append(thread)

        print(f"{run_tag} traffic running; waiting for elephant completion.")
        elephant_exit_code = None
        if elephant_client_proc:
            try:
                elephant_exit_code = elephant_client_proc.wait()
            except Exception as exc:
                print(f"{run_tag} elephant wait error: {exc}")
        elephant_progress_stop.set()
        if elephant_progress_thread:
            elephant_progress_thread.join(timeout=2)
        if elephant_exit_code is not None:
            print(f"{run_tag} elephant client exited with code {elephant_exit_code}.")

        print(f"{run_tag} waiting for elephant server idle timeout ({SERVER_IDLE_TIMEOUT_MS}ms).")
        time.sleep(SERVER_IDLE_TIMEOUT_MS / 1000 + 0.2)
        if elephant_server_proc and elephant_server_proc.poll() is None:
            print(f"{run_tag} elephant server still running after idle timeout.")

        mouse_stop.set()
        for thread in mouse_threads:
            thread.join()

        print(f"{run_tag} stopping clients (kill grace={kill_grace_seconds}s).")
        _terminate_processes(
            mouse_procs,
            term_timeout=kill_grace_seconds,
        )

        after_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_after.json").write_text(
            json.dumps(after_stats, indent=2)
        )
        print(f"{run_tag} switch stats captured (after).")
    finally:
        print(f"{run_tag} tearing down topology and processes...")
        elephant_progress_stop.set()
        if elephant_progress_thread and elephant_progress_thread.is_alive():
            elephant_progress_thread.join(timeout=2)
        mouse_stop.set()
        for thread in mouse_threads:
            if thread.is_alive():
                thread.join(timeout=3)
        if (
            start_time is not None
            and grid_t0 is not None
            and mouse_schedule_stats
            and log_dir is not None
        ):
            _write_mouse_thread_start_log(log_dir, start_time, grid_t0, mouse_schedule_stats)
            _write_mouse_schedule_summary(log_dir, start_time, grid_t0, mouse_schedule_stats)
        _terminate_processes(
            mouse_procs + [elephant_client_proc] + server_procs,
            term_timeout=kill_grace_seconds,
        )
        stop_fattree_topology(ctx)
        print(f"{run_tag} teardown complete.")

    return log_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Incast experiment driver.")
    parser.add_argument("--proto", required=True, choices=["quic", "mpquic"])
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--k", type=int, default=6)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("default"),
        help=(
            "Subdirectory name under logs/incast. Each run writes to "
            "logs/incast/<output-dir>/<proto>/run_<timestamp> (default: default)."
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

    if args.k < 6:
        parser.error(f"k must be >= 6 for this incast scenario (got {args.k}).")

    for run_idx in range(args.runs):
        run_incast_once(
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
