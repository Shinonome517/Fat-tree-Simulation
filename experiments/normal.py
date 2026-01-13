"""Normal experiments (Blackbox-derived; adds TCP/MPTCP).

This module launches random Elephant/Mouse flows across the Fat-Tree and
captures switch statistics to compare QUIC/MPQUIC/TCP/MPTCP under ECMP.
"""

import argparse
import csv
import json
import random
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

# Ensure repo root is on sys.path when executed as a script.
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

from experiments import (
    create_fattree,
    make_log_dir,
    picoquic_perf_cmd,
    snapshot_switch_bytes,
)
from topology import host_ips, stop_fattree_topology

DEFAULT_SCENARIO = "*1:1000:1000;"  # minimal valid perf scenario
DEFAULT_LINK_BW_MBPS = 1000  # keep in sync with create_fattree call
DEFAULT_ELEPHANT_LOAD_FRAC = 0.7  # fraction of link capacity to target when auto-sizing Elephant payload
DEFAULT_KILL_GRACE_SECONDS = 3.0  # grace before SIGKILL when stopping processes
SERVER_IDLE_TIMEOUT_MS = 5000
CONGESTION_CONTROL = "cubic"
PROTO_CHOICES = ("quic", "mpquic", "tcp", "mptcp")
ROLE_MODE_CHOICES = ("mixed", "split")
LOG_ROOT_NAME = "normal"
TCP_PERF_PATH = Path(__file__).resolve().parent / "tcp_perf.py"
PYTHON_BIN = "/usr/bin/python3"  # Use absolute python path inside Mininet hosts.
LINK_SAMPLE_INTERVAL_S = 0.1

# TCP short-flow survivability (λ=80 flows/s) – intentionally aggressive for the closed DCNW lab.
TCP_SYSCTL_SETTINGS = {
    "net.ipv4.ip_local_port_range": "1024 65535",
    "net.ipv4.tcp_tw_reuse": "1",
    "net.ipv4.tcp_fin_timeout": "10",
    "net.core.somaxconn": "65535",
    "net.ipv4.tcp_max_syn_backlog": "65535",
}

# Experiment parameters.
ELEPHANT_PAIR_COUNT = 4
MOUSE_PAIR_COUNT = 20
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
MOUSE_LAMBDA = 80.0
MOUSE_HEARTBEAT_INTERVAL = 10.0
MOUSE_SIZE_MIN = 16 * 1024
MOUSE_SIZE_MAX = 64 * 1024
DEFAULT_SEED = 12345

# picoquicdemo server templates (same paths as whitebox.py).
PICOQUIC_CERT_PATH = "/etc/picoquic/server-cert.pem"
PICOQUIC_KEY_PATH = "/etc/picoquic/server-key.pem"
ELEPHANT_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} {qlog_flag} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)
MOUSE_SERVER_CMD_TEMPLATE = (
    "picoquicdemo -a server -p {port} {qlog_flag} -c {cert} -k {key} {extra} > {log_path} 2>&1"
)

# Extra args for picoquicdemo perf mode.
# Keep these aligned with experiments.whitebox get_extra_args for Elephant clients.
ELEPHANT_ALT_ADDRS_MPQUIC = ""  # Multipath address advertisement deferred for normal.py; keep single-path for now.
ROLE_ELEPHANT_SERVER = "elephant-server"
ROLE_ELEPHANT_CLIENT = "elephant-client"
ROLE_MOUSE_SERVER = "mouse-server"
ROLE_MOUSE_CLIENT = "mouse-client"
ELEPHANT_MAX_WAIT_PAD_SECONDS = 60.0  # extra wait beyond duration before forcing elephant teardown
HOSTNAME_RE = re.compile(r"h(\d+)(\d)(\d)$")


def _format_extra_args(extra_args: Union[Iterable[str], str, None]) -> str:
    if not extra_args:
        return ""
    if isinstance(extra_args, str):
        return extra_args.strip()
    return " ".join(shlex.quote(str(a)) for a in extra_args if str(a))


def _normalize_extra_args(extra_args) -> List[str]:
    if not extra_args:
        return []
    if isinstance(extra_args, str):
        return [extra_args.strip()] if extra_args.strip() else []
    return [str(a) for a in extra_args if str(a)]


def _is_udp_listening(host, port: int) -> Tuple[bool, str]:
    """
    Check if a UDP listener exists on the given port inside a host namespace.
    Returns (is_listening, raw_output_snippet).
    """
    out = host.cmd(f"ss -H -lun 'sport = :{port}' 2>/dev/null || true").strip()
    if out:
        return True, out
    # Fallback to netstat for environments lacking ss details.
    netstat_out = host.cmd(f"netstat -lun 2>/dev/null | grep ':{port} ' || true").strip()
    return bool(netstat_out), netstat_out


def _is_tcp_listening(host, port: int) -> Tuple[bool, str]:
    """
    Check if a TCP listener exists on the given port inside a host namespace.
    Returns (is_listening, raw_output_snippet).
    """
    out = host.cmd(f"ss -H -ltn 'sport = :{port}' 2>/dev/null || true").strip()
    if out:
        return True, out
    netstat_out = host.cmd(f"netstat -ltn 2>/dev/null | grep ':{port} ' || true").strip()
    return bool(netstat_out), netstat_out


def _start_picoquic_server(
    host, port: int, log_path: Path, extra_args, enable_qlog: bool
) -> object:
    # Build argv directly so we can run without a shell and kill reliably later.
    args: List[str] = ["picoquicdemo", "-a", "server", "-p", str(port)]
    qlog_path = None
    if enable_qlog:
        qlog_path = Path(f"{log_path}.qlog")
        args.extend(["-l", str(qlog_path)])
    args.extend(["-c", PICOQUIC_CERT_PATH, "-k", PICOQUIC_KEY_PATH])
    args.extend(["-d", str(SERVER_IDLE_TIMEOUT_MS)])
    args.extend(_normalize_extra_args(extra_args))

    with log_path.open("w") as log_file:
        proc = host.popen(args, stdout=log_file, stderr=subprocess.STDOUT, shell=False)
    return proc


def _tcp_perf_server_cmd(port: int, proto: str, bind_ip: str = "0.0.0.0") -> List[str]:
    return [
        PYTHON_BIN,
        str(TCP_PERF_PATH),
        "server",
        "--proto",
        proto,
        "--bind",
        bind_ip,
        "--port",
        str(port),
    ]


def _tcp_perf_client_cmd(
    server_ip: str, port: int, payload_bytes: int, csv_path: Path, proto: str
) -> List[str]:
    return [
        "python3",
        str(TCP_PERF_PATH),
        "client",
        "--proto",
        proto,
        "--host",
        server_ip,
        "--port",
        str(port),
        "--bytes",
        str(payload_bytes),
        "--csv",
        str(csv_path),
    ]


def _verify_udp_servers(hosts: Iterable, port: int, label: str) -> None:
    """
    Ensure each host has a UDP listener on the given port. Raises RuntimeError otherwise.
    """
    missing: List[str] = []
    for h in hosts:
        ok, raw = _is_udp_listening(h, port)
        if not ok:
            missing.append(f"{h.name} (udp/{port}) [{raw or 'no ss output'}]")
    if missing:
        raise RuntimeError(
            f"{label} server(s) not listening: " + ", ".join(missing)
        )


def _verify_tcp_servers(hosts: Iterable, port: int, label: str) -> None:
    """
    Ensure each host has a TCP listener on the given port. Raises RuntimeError otherwise.
    """
    missing: List[str] = []
    for h in hosts:
        ok, raw = _is_tcp_listening(h, port)
        if not ok:
            missing.append(f"{h.name} (tcp/{port}) [{raw or 'no ss output'}]")
    if missing:
        raise RuntimeError(
            f"{label} server(s) not listening: " + ", ".join(missing)
        )


class LinkSampler:
    """Sample agg->core tx_bytes and compute utilization per LINK_SAMPLE_INTERVAL_S."""

    def __init__(self, ctx, log_dir: Path, bw_mbps: int, run_tag: str, interval_s: float = LINK_SAMPLE_INTERVAL_S):
        self.ctx = ctx
        self.log_dir = log_dir
        self.interval_s = interval_s
        self.bw_mbps = bw_mbps
        self.run_tag = run_tag
        self.log_path = log_dir / "link_timeseries.csv"
        self.meta_path = log_dir / "link_timeseries_meta.json"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._interface_map = self._collect_interfaces()

    def _collect_interfaces(self) -> Dict[object, List[str]]:
        """Return {agg_node: [agg-to-core interfaces]}."""
        iface_map: Dict[object, List[str]] = {}
        pattern = re.compile(r"a\d+\d+-to-c\d+")
        for pod_idx, pod_aggs in enumerate(self.ctx.aggs):
            for agg_idx, agg_node in enumerate(pod_aggs):
                names: List[str] = []
                for intf in agg_node.intfList():
                    name: Optional[str] = getattr(intf, "name", None)
                    if not name:
                        continue
                    if name.startswith(f"a{pod_idx}{agg_idx}-to-c") and pattern.match(name):
                        names.append(name)
                if names:
                    iface_map[agg_node] = sorted(names)
        if iface_map:
            meta = {
                "bw_mbps": self.bw_mbps,
                "sample_interval_s": self.interval_s,
                "interfaces": sorted([name for names in iface_map.values() for name in names]),
            }
            self.meta_path.write_text(json.dumps(meta, indent=2))
        return iface_map

    def start(self) -> None:
        if not self._interface_map:
            print(f"{self.run_tag} link sampler: no agg->core interfaces found; skipping.")
            return
        print(f"{self.run_tag} starting link sampler for {sum(len(v) for v in self._interface_map.values())} interfaces.")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _read_tx(self) -> Dict[str, float]:
        readings: Dict[str, float] = {}
        for node, names in self._interface_map.items():
            if not names:
                continue
            paths = " ".join(f"/sys/class/net/{n}/statistics/tx_bytes" for n in names)
            raw = node.cmd(f"cat {paths} 2>/dev/null").strip().split()
            if len(raw) != len(names):
                continue
            for name, val in zip(names, raw):
                try:
                    readings[name] = float(val)
                except Exception:
                    continue
        return readings

    def _run(self) -> None:
        prev = self._read_tx()
        if not prev:
            print(f"{self.run_tag} link sampler: initial read failed; stopping.")
            return

        start_time = time.perf_counter()
        last_time = start_time
        sample_idx = 0

        with self.log_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sample_idx", "elapsed_s", "if_name", "delta_tx_bytes", "u_l", "dt_s"])
            while not self._stop.wait(self.interval_s):
                now = time.perf_counter()
                dt = now - last_time
                if dt <= 0:
                    continue
                curr = self._read_tx()
                if not curr:
                    continue
                elapsed = now - start_time
                for if_name in sorted(prev.keys()):
                    curr_val = curr.get(if_name)
                    if curr_val is None:
                        continue
                    delta = max(0.0, curr_val - prev.get(if_name, 0.0))
                    util = (delta * 8.0) / (self.bw_mbps * 1_000_000 * dt)
                    writer.writerow([sample_idx, elapsed, if_name, delta, util, dt])
                prev = curr
                last_time = now
                sample_idx += 1


def _terminate_processes(procs: List[object], term_timeout: float = DEFAULT_KILL_GRACE_SECONDS) -> None:
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
            proc.wait(timeout=max(term_timeout, 0.0))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _wait_for_completion_then_terminate(
    proc, wait_timeout: float, label: str = "", term_timeout: float = 3.0
) -> None:
    """Wait for a proc to exit; only terminate/kill if it exceeds the wait."""
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


def _apply_tcp_sysctls(hosts: Sequence, proto: str) -> None:
    """
    Apply TCP/MPTCP-related sysctl tuning on all hosts for high-rate short flows.
    """
    proto = (proto or "").lower()
    if proto not in ("tcp", "mptcp"):
        return

    settings = TCP_SYSCTL_SETTINGS.copy()
    if CONGESTION_CONTROL:
        settings["net.ipv4.tcp_congestion_control"] = CONGESTION_CONTROL
    if proto == "mptcp":
        settings["net.mptcp.enabled"] = "1"

    for host in hosts:
        for key, val in settings.items():
            host.cmd(f"sysctl -w {key}={val}")


def get_extra_args(proto: str, role: str, alt_addrs: Optional[str] = None) -> List[str]:
    """
    Return picoquicdemo extra CLI arguments based on proto and role.

    All roles enforce congestion control selection via -G. MPQUIC adds -M/-A
    for Elephant roles; Mouse remains single-path.
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
        alt = alt_addrs or ELEPHANT_ALT_ADDRS_MPQUIC
        if alt:
            args += ["-A", alt]
        return args
    if role in (ROLE_MOUSE_SERVER, ROLE_MOUSE_CLIENT):
        return base_args
    return base_args


def _host_coords_from_name(name: str) -> Optional[Tuple[int, int, int]]:
    match = HOSTNAME_RE.fullmatch(name or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _build_host_coord_map(ctx) -> Dict[object, Tuple[int, int, int]]:
    coords: Dict[object, Tuple[int, int, int]] = {}
    for p, pod_hosts in enumerate(ctx.hosts):
        for e, edge_hosts in enumerate(pod_hosts):
            for h, host in enumerate(edge_hosts):
                coords[host] = (p, e, h)
    return coords


def _select_primary_intf(host) -> Optional[str]:
    if not host:
        return None
    for intf in host.intfList():
        name = getattr(intf, "name", None)
        if name and name != "lo":
            return name
    return None


def _extra_host_ips(coords: Optional[Tuple[int, int, int]]) -> List[str]:
    if not coords:
        return []
    p, e, h = coords
    ips = host_ips(p, e, h)
    return [ip.split("/")[0] for ip in ips[1:]]  # skip primary, keep extras only


def _mpquic_alt_addrs(host, iface: Optional[str], coords: Optional[Tuple[int, int, int]]) -> str:
    alt_ips = _extra_host_ips(coords)
    if not alt_ips or not iface:
        return ""
    ifindex_raw = host.cmd(f"cat /sys/class/net/{iface}/ifindex 2>/dev/null").strip()
    try:
        ifindex = int(ifindex_raw)
    except Exception:
        print(f"[normal] warning: failed to read ifindex for {host.name}:{iface} ({ifindex_raw}); skipping -A.")
        return ""
    return ",".join(f"{ip}/{ifindex}" for ip in alt_ips)


def _ensure_mptcp_endpoints(host, iface: Optional[str], coords: Optional[Tuple[int, int, int]]) -> None:
    alt_ips = _extra_host_ips(coords)
    if not alt_ips or not iface:
        return
    host.cmd("ip mptcp limits set add_addr_accepted 4 subflow 4 signal 4 2>/dev/null || true")
    for ip in alt_ips:
        host.cmd(
            f"ip mptcp endpoint show | grep -w '{ip}' >/dev/null 2>&1 || "
            f"ip mptcp endpoint add {ip} dev {iface} signal"
        )


def _pick_random_pairs(
    hosts: Sequence,
    count: int,
    src_pool: Optional[Sequence] = None,
    dst_pool: Optional[Sequence] = None,
) -> List[Tuple]:
    pairs: List[Tuple] = []
    if len(hosts) < 2:
        return pairs
    sources = list(src_pool) if src_pool is not None else list(hosts)
    destinations = list(dst_pool) if dst_pool is not None else list(hosts)
    if not sources or not destinations:
        return pairs
    for _ in range(count):
        src = random.choice(sources)
        dst = random.choice(destinations)
        while dst == src and len(destinations) > 1:
            dst = random.choice(destinations)
        pairs.append((src, dst))
    return pairs


def _split_sender_pools(hosts: Sequence) -> Tuple[List, List]:
    """
    Split hosts into two non-empty, disjoint pools for Elephant/Mouse senders.
    Falls back to mixed use if hosts are insufficient.
    """
    hosts_copy = list(hosts)
    if len(hosts_copy) < 2:
        return hosts_copy, hosts_copy
    random.shuffle(hosts_copy)
    mid = max(1, min(len(hosts_copy) - 1, len(hosts_copy) // 2))
    return hosts_copy[:mid], hosts_copy[mid:]


def _mouse_generator(
    pair_index: int,
    src_host,
    dst_ip: str,
    log_dir: Path,
    start_time: float,
    total_duration: Optional[float],
    stop_event: threading.Event,
    proc_store: List[object],
    extra_args: List[str],
    proto: str,
    run_tag: str,
) -> None:
    """Poisson arrivals of short Mouse flows with periodic heartbeats."""
    seq = 0
    next_heartbeat = start_time + MOUSE_HEARTBEAT_INTERVAL
    prefix = f"{run_tag} " if run_tag else ""
    end_time = start_time + total_duration if total_duration is not None else None
    while not stop_event.is_set():
        now = time.time()
        if end_time is not None and now >= end_time:
            break
        if now >= next_heartbeat:
            print(
                f"{prefix}[mouse-{pair_index:02d}] t={now - start_time:.1f}s flows={seq}"
            )
            next_heartbeat += MOUSE_HEARTBEAT_INTERVAL

        sleep_time = random.expovariate(MOUSE_LAMBDA)
        stop_event.wait(sleep_time)
        if stop_event.is_set() or (end_time is not None and time.time() >= end_time):
            break

        seq += 1
        size_bytes = random.randint(MOUSE_SIZE_MIN, MOUSE_SIZE_MAX)
        csv_path = log_dir / f"mouse_{pair_index:02d}_{seq:04d}.csv"
        if proto in ("quic", "mpquic"):
            scenario = f"*1:{size_bytes}:0;"
            mouse_cmd = picoquic_perf_cmd(
                server_ip=dst_ip,
                server_port=MOUSE_PORT,
                csv_path=csv_path,
                scenario=scenario,
                extra_args=extra_args,
                as_list=True,
            )
        else:
            mouse_cmd = _tcp_perf_client_cmd(
                server_ip=dst_ip,
                port=MOUSE_PORT,
                payload_bytes=size_bytes,
                csv_path=csv_path,
                proto=proto,
            )
        proc = src_host.popen(mouse_cmd, shell=False)
        proc_store.append(proc)
        # Drop completed processes to keep the list small.
        proc_store[:] = [p for p in proc_store if p and p.poll() is None]


def _elephant_client_extra(proto: str, alt_addrs: Optional[str] = None) -> List[str]:
    return get_extra_args(proto, ROLE_ELEPHANT_CLIENT, alt_addrs=alt_addrs)


def _elephant_server_extra(proto: str) -> List[str]:
    return get_extra_args(proto, ROLE_ELEPHANT_SERVER)


def _healthcheck(
    label: str,
    proto: str,
    server_host,
    client_host,
    server_ip: str,
    port: int,
    log_dir: Path,
    run_tag: str = "",
    client_alt_addrs: Optional[str] = None,
) -> None:
    """
    Lightweight pre-flight: ensure server process exists, reachability works, and a short
    session completes using the same protocol as the real run.
    """
    log_path = log_dir / f"healthcheck_{label}.log"
    lines: List[str] = []
    prefix = f"{run_tag} " if run_tag else ""

    def _log(msg: str) -> None:
        print(f"{prefix}{msg}")
        lines.append(msg)

    if proto in ("quic", "mpquic"):
        procs = server_host.cmd(f"pgrep -fa 'picoquicdemo.*-p {port}'")
        procs_clean = procs.strip()
        server_ok = bool(procs_clean)
        _log(f"[health:{label}] server procs (port {port}): {procs_clean or 'none'}")

        listen_ok, listen_raw = _is_udp_listening(server_host, port)
        _log(f"[health:{label}] listen check udp/{port}: {listen_raw or 'none'}")
    else:
        procs = server_host.cmd(f"pgrep -fa 'tcp_perf.py.*server.*--port {port}'")
        procs_clean = procs.strip()
        server_ok = bool(procs_clean)
        _log(f"[health:{label}] server procs (port {port}): {procs_clean or 'none'}")

        listen_ok, listen_raw = _is_tcp_listening(server_host, port)
        _log(f"[health:{label}] listen check tcp/{port}: {listen_raw or 'none'}")

    ping_out = client_host.cmd(f"ping -c1 -W1 {server_ip}; echo HC_RC=$?")
    ping_ok = "HC_RC=0" in ping_out
    _log(f"[health:{label}] ping ->\n{ping_out.strip()}")

    hc_timeout = 10
    scenario = DEFAULT_SCENARIO
    if proto in ("quic", "mpquic"):
        client_extra = get_extra_args(
            proto,
            ROLE_ELEPHANT_CLIENT if label == "elephant" else ROLE_MOUSE_CLIENT,
            alt_addrs=client_alt_addrs if label == "elephant" else None,
        )
        extra_str = _format_extra_args(client_extra)
        hc_cmd = (
            f"timeout {hc_timeout} "
            f"picoquicdemo -a perf {extra_str} -F /tmp/normal_hc_{label}.csv "
            f"{server_ip} {port} {shlex.quote(scenario)}"
        )
        hc_out = client_host.cmd(f"{hc_cmd}; echo HC_RC=$?")
        _log(
            f"[health:{label}] {proto} (scenario={scenario}, extra_args={extra_str or 'none'}) ->\n{hc_out.strip()}"
        )

        hc_exit_code = None
        for line in hc_out.splitlines():
            if "Client exit with code" in line:
                try:
                    hc_exit_code = int(line.rsplit("=", 1)[-1].strip())
                except Exception:
                    pass
        sess_ok = "HC_RC=0" in hc_out and hc_exit_code == 0
    else:
        # TCP/MPTCP: short send using tcp_perf client.
        csv_tmp = log_dir / f"hc_{label}_{proto}.csv"
        hc_cmd = _tcp_perf_client_cmd(
            server_ip=server_ip,
            port=port,
            payload_bytes=1024,
            csv_path=csv_tmp,
            proto=proto,
        )
        hc_out_raw = client_host.cmd(" ".join(shlex.quote(c) for c in hc_cmd) + "; echo HC_RC=$?")
        _log(f"[health:{label}] {proto} ->\n{hc_out_raw.strip()}")
        sess_ok = "HC_RC=0" in hc_out_raw

    log_path.write_text("\n".join(lines) + "\n")

    if not (server_ok and listen_ok and ping_ok and sess_ok):
        raise RuntimeError(
            f"healthcheck {label} failed (server_ok={server_ok}, listen_ok={listen_ok}, ping_ok={ping_ok}, sess_ok={sess_ok}); "
            f"see {log_path}"
        )


def run_normal_once(
    proto: str,
    k: int,
    duration: float,
    base_seed: int,
    run_index: int,
    elephant_bytes: Optional[int],
    elephant_load_fraction: float,
    enable_qlog: bool = False,
    output_subdir: Optional[Path] = None,
    role_mode: str = "mixed",
) -> None:
    """Execute one normal experiment run."""
    seed = base_seed + run_index
    random.seed(seed)
    run_tag = f"[run{run_index:04d}]"
    print(
        f"{run_tag} starting normal run (proto={proto}, k={k}, duration={duration}s, seed={seed})"
    )

    log_root = Path("logs") / LOG_ROOT_NAME / (output_subdir or Path("default"))
    log_dir = make_log_dir(LOG_ROOT_NAME, proto, log_root=log_root, suffix=f"seed{seed}")

    ctx = None
    server_procs: List[object] = []
    elephant_procs: List[object] = []
    mouse_threads: List[threading.Thread] = []
    mouse_proc_lists: List[List[object]] = []
    mouse_stop = threading.Event()
    all_mouse_procs: List[object] = []
    link_sampler: Optional[LinkSampler] = None

    try:
        ctx = create_fattree(k=k, bw_mbps=DEFAULT_LINK_BW_MBPS, delay="0.05ms", queue_pkts=75)
        hosts_flat = [h for pod in ctx.hosts for edge in pod for h in edge]
        print(f"{run_tag} hosts ready: {len(hosts_flat)} total.")
        _apply_tcp_sysctls(hosts_flat, proto)

        print(f"{run_tag} capturing switch stats (before).")
        before_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_before.json").write_text(
            json.dumps(before_stats, indent=2)
        )
        link_sampler = LinkSampler(
            ctx,
            log_dir=log_dir,
            bw_mbps=DEFAULT_LINK_BW_MBPS,
            run_tag=run_tag,
            interval_s=LINK_SAMPLE_INTERVAL_S,
        )
        link_sampler.start()

        if role_mode not in ROLE_MODE_CHOICES:
            raise ValueError(f"role_mode must be one of {ROLE_MODE_CHOICES}")
        if role_mode == "split":
            ele_src_pool, mouse_src_pool = _split_sender_pools(hosts_flat)
            if not ele_src_pool or not mouse_src_pool:
                print(f"{run_tag} warning: unable to split sender pools; falling back to mixed.")
                ele_src_pool = mouse_src_pool = hosts_flat
        else:
            ele_src_pool = mouse_src_pool = hosts_flat

        elephant_pairs = _pick_random_pairs(hosts_flat, ELEPHANT_PAIR_COUNT, src_pool=ele_src_pool)
        mouse_pairs = _pick_random_pairs(hosts_flat, MOUSE_PAIR_COUNT, src_pool=mouse_src_pool)
        print(
            f"{run_tag} selected pairs: elephants={len(elephant_pairs)}, mice={len(mouse_pairs)}"
        )

        # Persist pair selection to ease debugging when flows fail.
        pair_log = log_dir / "pair_map.txt"
        with pair_log.open("w") as f:
            f.write("Elephant pairs (src -> dst):\n")
            for idx, (src, dst) in enumerate(elephant_pairs):
                f.write(f"{idx:02d}: {src.name} -> {dst.name} (dst_ip={dst.IP()})\n")
            f.write("\nMouse pairs (src -> dst):\n")
            for idx, (src, dst) in enumerate(mouse_pairs):
                f.write(f"{idx:02d}: {src.name} -> {dst.name} (dst_ip={dst.IP()})\n")

        host_coord_map = _build_host_coord_map(ctx)
        elephant_senders = {src for src, _ in elephant_pairs}
        elephant_iface_map: Dict[object, Optional[str]] = {}
        elephant_alt_map: Dict[object, str] = {}

        # Prepare per-Elephant sender multipath setup.
        for host in elephant_senders:
            iface = _select_primary_intf(host)
            elephant_iface_map[host] = iface
            coords = host_coord_map.get(host) or _host_coords_from_name(host.name)
            if proto == "mpquic":
                alt = _mpquic_alt_addrs(host, iface, coords)
                elephant_alt_map[host] = alt
            if proto == "mptcp":
                _ensure_mptcp_endpoints(host, iface, coords)

        elephant_server_hosts = {dst.name: dst for _, dst in elephant_pairs}.values()
        mouse_server_hosts = {dst.name: dst for _, dst in mouse_pairs}.values()
        mouse_proto = "tcp" if proto == "mptcp" else proto

        print(f"{run_tag} starting servers for destinations (proto={proto}).")
        if proto in ("quic", "mpquic"):
            for host in elephant_server_hosts:
                log_path = log_dir / f"elephant_server_{host.name}.log"
                server_procs.append(
                    _start_picoquic_server(
                        host,
                        ELEPHANT_PORT,
                        log_path,
                        _elephant_server_extra(proto),
                        enable_qlog,
                    )
                )
            for host in mouse_server_hosts:
                log_path = log_dir / f"mouse_server_{host.name}.log"
                server_procs.append(
                    _start_picoquic_server(
                        host,
                        MOUSE_PORT,
                        log_path,
                        get_extra_args(proto, ROLE_MOUSE_SERVER),
                        enable_qlog,
                    )
                )
            _verify_udp_servers(elephant_server_hosts, ELEPHANT_PORT, "elephant")
            _verify_udp_servers(mouse_server_hosts, MOUSE_PORT, "mouse")
        else:
            for host in elephant_server_hosts:
                log_path = log_dir / f"elephant_server_{host.name}.log"
                proc = host.popen(
                    _tcp_perf_server_cmd(ELEPHANT_PORT, proto),
                    stdout=log_path.open("w"),
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                server_procs.append(proc)
            for host in mouse_server_hosts:
                log_path = log_dir / f"mouse_server_{host.name}.log"
                proc = host.popen(
                    _tcp_perf_server_cmd(MOUSE_PORT, mouse_proto),
                    stdout=log_path.open("w"),
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                server_procs.append(proc)
            time.sleep(0.5)
            verify_ok = False
            for _ in range(3):
                try:
                    _verify_tcp_servers(elephant_server_hosts, ELEPHANT_PORT, "elephant")
                    _verify_tcp_servers(mouse_server_hosts, MOUSE_PORT, "mouse")
                    verify_ok = True
                    break
                except RuntimeError:
                    time.sleep(0.5)
            if not verify_ok:
                debug = {h.name: h.cmd("pgrep -fa tcp_perf.py || true") for h in elephant_server_hosts}
                raise RuntimeError(f"tcp servers not listening after retries; pgrep={debug}")

        start_time = time.time()
        mouse_extra = (
            get_extra_args(mouse_proto, ROLE_MOUSE_CLIENT) if mouse_proto in ("quic", "mpquic") else []
        )
        elephant_target_bytes = elephant_bytes
        if elephant_target_bytes is None:
            link_bps = DEFAULT_LINK_BW_MBPS * 1_000_000
            elephant_target_bytes = int(elephant_load_fraction * link_bps / 8 * duration)
        if elephant_target_bytes <= 0:
            raise ValueError("elephant_bytes must be positive when provided or computed.")
        elephant_scenario = f"*1:{elephant_target_bytes}:0;" if proto in ("quic", "mpquic") else None

        # Pre-flight health checks on one elephant pair and one mouse pair (if present)
        if elephant_pairs:
            src, dst = elephant_pairs[0]
            _healthcheck(
                label="elephant",
                proto=proto,
                server_host=dst,
                client_host=src,
                server_ip=dst.IP(),
                port=ELEPHANT_PORT,
                log_dir=log_dir,
                run_tag=run_tag,
                client_alt_addrs=elephant_alt_map.get(src) if proto == "mpquic" else None,
            )
        if mouse_pairs:
            src, dst = mouse_pairs[0]
            _healthcheck(
                label="mouse",
                proto=mouse_proto,
                server_host=dst,
                client_host=src,
                server_ip=dst.IP(),
                port=MOUSE_PORT,
                log_dir=log_dir,
                run_tag=run_tag,
            )

        print(f"{run_tag} starting elephant clients.")
        for idx, (src, dst) in enumerate(elephant_pairs):
            dst_ip = dst.IP()  # Use the primary/representative IP; current experiments do not require alternate addresses.
            csv_path = log_dir / f"elephant_{idx:02d}.csv"
            print(
                f"{run_tag} elephant {idx:02d}: payload_bytes={elephant_target_bytes}, dst={dst_ip}"
            )
            if proto in ("quic", "mpquic"):
                extra = _elephant_client_extra(
                    proto,
                    elephant_alt_map.get(src) if proto == "mpquic" else None,
                )
                elephant_cmd = picoquic_perf_cmd(
                    server_ip=dst_ip,
                    server_port=ELEPHANT_PORT,
                    csv_path=csv_path,
                    scenario=elephant_scenario,
                    extra_args=extra,
                    as_list=True,
                )
            else:
                elephant_cmd = _tcp_perf_client_cmd(
                    server_ip=dst_ip,
                    port=ELEPHANT_PORT,
                    payload_bytes=elephant_target_bytes,
                    csv_path=csv_path,
                    proto=proto,
                )
            proc = src.popen(elephant_cmd, shell=False)
            elephant_procs.append(proc)

        print(f"{run_tag} starting mouse generator threads.")
        for idx, (src, dst) in enumerate(mouse_pairs):
            dst_ip = dst.IP()
            proc_list: List[object] = []
            mouse_proc_lists.append(proc_list)
            thread = threading.Thread(
                target=_mouse_generator,
                args=(
                    idx,
                    src,
                    dst_ip,
                    log_dir,
                    start_time,
                    None,
                    mouse_stop,
                    proc_list,
                    mouse_extra,
                    mouse_proto,
                    run_tag,
                ),
                daemon=True,
            )
            mouse_threads.append(thread)
            thread.start()

        print(f"{run_tag} traffic running; waiting for elephant completion.")
        elephant_deadline = start_time + duration + ELEPHANT_MAX_WAIT_PAD_SECONDS
        while time.time() < elephant_deadline:
            if all(proc.poll() is not None for proc in elephant_procs if proc):
                break
            time.sleep(0.5)
        for idx, proc in enumerate(elephant_procs):
            if not proc:
                continue
            if proc.poll() is not None:
                print(f"{run_tag} elephant {idx:02d} exited with code {proc.returncode}.")
                continue
            print(
                f"{run_tag} elephant {idx:02d} exceeded duration+{ELEPHANT_MAX_WAIT_PAD_SECONDS:.0f}s; sending SIGTERM."
            )
            _wait_for_completion_then_terminate(
                proc,
                wait_timeout=0.0,
                label=f"{run_tag} elephant {idx:02d}",
            )

        mouse_stop.set()
        for thread in mouse_threads:
            thread.join()

        all_mouse_procs: List[object] = [proc for plist in mouse_proc_lists for proc in plist]
        _terminate_processes(all_mouse_procs, term_timeout=DEFAULT_KILL_GRACE_SECONDS)

        after_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_after.json").write_text(
            json.dumps(after_stats, indent=2)
        )
        print(f"{run_tag} switch stats captured (after).")
        if link_sampler:
            link_sampler.stop()
    finally:
        mouse_stop.set()
        for thread in mouse_threads:
            if thread.is_alive():
                thread.join(timeout=2)
        all_mouse_procs = [proc for plist in mouse_proc_lists for proc in plist]
        _terminate_processes(
            elephant_procs + server_procs + all_mouse_procs,
            term_timeout=DEFAULT_KILL_GRACE_SECONDS,
        )
        if link_sampler:
            link_sampler.stop()
        stop_fattree_topology(ctx)
        print(f"{run_tag} teardown complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Normal experiment driver (QUIC/MPQUIC/TCP/MPTCP).")
    parser.add_argument("--proto", required=True, choices=list(PROTO_CHOICES))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("default"),
        help=(
            "Subdirectory name under logs/normal. Each run writes to "
            "logs/normal/<output-dir>/<proto>/run_<timestamp>_seed<seed> (default: default)."
        ),
    )
    parser.add_argument(
        "--elephant-bytes",
        type=int,
        default=None,
        help="Total payload bytes per Elephant perf scenario; overrides auto-sizing.",
    )
    parser.add_argument(
        "--elephant-load-frac",
        type=float,
        default=DEFAULT_ELEPHANT_LOAD_FRAC,
        help="If --elephant-bytes is unset, fraction of link capacity to target per Elephant flow (default 0.7).",
    )
    parser.add_argument(
        "--enable-qlog",
        action="store_true",
        help="Enable picoquicdemo -l qlog capture for servers (default: disabled for performance).",
    )
    parser.add_argument(
        "--role-mode",
        choices=list(ROLE_MODE_CHOICES),
        default="mixed",
        help="Sender role selection: 'split' uses disjoint Elephant/Mouse source pools, 'mixed' allows overlap.",
    )
    args = parser.parse_args()

    for run_idx in range(args.runs):
        run_normal_once(
            proto=args.proto,
            k=args.k,
            duration=args.duration,
            base_seed=args.seed,
            run_index=run_idx,
            elephant_bytes=args.elephant_bytes,
            elephant_load_fraction=args.elephant_load_frac,
            enable_qlog=args.enable_qlog,
            output_subdir=args.output_dir,
            role_mode=args.role_mode,
        )


if __name__ == "__main__":
    main()
