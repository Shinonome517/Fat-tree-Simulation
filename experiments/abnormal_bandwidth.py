"""Abnormal bandwidth injection experiments (Blackbox-derived; adds TCP/MPTCP).

This module launches random Elephant/Mouse flows across the Fat-Tree, applies
a static bandwidth cap on a specific link before traffic starts, and captures
switch statistics to compare QUIC/MPQUIC/TCP/MPTCP under ECMP.
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
DEFAULT_LINK_BW_MBPS = 100  # keep in sync with create_fattree call
DEFAULT_LINK_DELAY_MS = 0.5
DEFAULT_SWITCH_QUEUE_PKTS = 50
DEFAULT_KILL_GRACE_SECONDS = 3.0  # grace before SIGKILL when stopping processes
SERVER_IDLE_TIMEOUT_MS = 5000
CONGESTION_CONTROL = "cubic"
PROTO_CHOICES = ("quic", "mpquic", "tcp", "mptcp")
LOG_ROOT_NAME = "abnormal"
TCP_PERF_PATH = Path(__file__).resolve().parent / "tcp_perf.py"
PYTHON_BIN = "/usr/bin/python3"  # Use absolute python path inside Mininet hosts.
LINK_SAMPLE_INTERVAL_S = 0.1
WARMUP_SECONDS = 1.0
ELEPHANT_CLIENT_TIMEOUT_S = 120.0  # watchdog for elephant clients after traffic start
ELEPHANT_MBYTES = 1000

BW_TARGET_POD = 0
BW_TARGET_AGG = 1
BW_TARGET_CORE = 3

# TCP short-flow survivability (λ=80 flows/s) – intentionally aggressive for the closed DCNW lab.
TCP_SYSCTL_SETTINGS = {
    "net.ipv4.ip_local_port_range": "1024 65535",
    "net.ipv4.tcp_tw_reuse": "1",
    "net.ipv4.tcp_fin_timeout": "10",
    "net.core.somaxconn": "65535",
    "net.ipv4.tcp_max_syn_backlog": "65535",
    "net.ipv4.tcp_sack": "1",
    "net.ipv4.tcp_dsack": "1",
}
TCP_ELEPHANT_CHUNK_SIZE = 1024 * 1024
TCP_MOUSE_CHUNK_SIZE = 64 * 1024

# Experiment parameters.
MOUSE_PAIR_COUNT = 10
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
MOUSE_TOTAL_LAMBDA = 16.0
MOUSE_HEARTBEAT_INTERVAL = 10.0
MOUSE_SIZE_MIN = 4 * 1024
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
ELEPHANT_ALT_ADDRS_MPQUIC = ""  # Multipath address advertisement is computed per host at runtime when using MPQUIC.
ROLE_ELEPHANT_SERVER = "elephant-server"
ROLE_ELEPHANT_CLIENT = "elephant-client"
ROLE_MOUSE_SERVER = "mouse-server"
ROLE_MOUSE_CLIENT = "mouse-client"
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


def _format_bw_dir(bw_mbps: int) -> str:
    return f"bw-{bw_mbps:g}"


def _bw_target_interfaces(ctx) -> List[object]:
    try:
        agg_node = ctx.aggs[BW_TARGET_POD][BW_TARGET_AGG]
    except Exception as exc:
        raise RuntimeError(
            f"Bandwidth target agg a{BW_TARGET_POD}{BW_TARGET_AGG} not available"
        ) from exc
    try:
        core_node = ctx.cores[BW_TARGET_CORE]
    except Exception as exc:
        raise RuntimeError(f"Bandwidth target core c{BW_TARGET_CORE} not available") from exc

    agg_if = f"a{BW_TARGET_POD}{BW_TARGET_AGG}-to-c{BW_TARGET_CORE}"
    core_if = f"c{BW_TARGET_CORE}-to-a{BW_TARGET_POD}{BW_TARGET_AGG}"
    agg_intf = agg_node.intf(agg_if)
    core_intf = core_node.intf(core_if)
    if agg_intf is None:
        raise RuntimeError(f"Interface {agg_if} not found on {agg_node.name}")
    if core_intf is None:
        raise RuntimeError(f"Interface {core_if} not found on {core_node.name}")
    return [agg_intf, core_intf]


def _apply_static_bandwidth(
    intfs: Sequence[object], link_params: Dict[str, object], bw_mbps: int
) -> None:
    cfg: Dict[str, object] = {"bw": bw_mbps, "use_htb": True}
    for key in ("bw", "delay", "max_queue_size", "use_htb"):
        if key in link_params:
            cfg[key] = link_params[key]
    for intf in intfs:
        intf.config(**cfg)


def _mark_run_invalid(log_dir: Path, run_tag: str, reason: str) -> None:
    path = log_dir / "run_invalid.txt"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(f"[{timestamp}] {run_tag} {reason}\n")


def _positive_bw_mbps(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid bandwidth: {value}") from exc
    if parsed < 10 or parsed > 1000:
        raise argparse.ArgumentTypeError("Bandwidth must be between 10 and 1000 Mbps.")
    return parsed


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


def _tcp_perf_server_cmd(
    port: int, proto: str, bind_ip: str = "0.0.0.0", chunk_size: Optional[int] = None
) -> List[str]:
    cmd = [
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
    if chunk_size is not None:
        cmd += ["--chunk-size", str(chunk_size)]
    return cmd


def _tcp_perf_client_cmd(
    server_ip: str,
    port: int,
    payload_bytes: int,
    csv_path: Path,
    proto: str,
    chunk_size: Optional[int] = None,
) -> List[str]:
    cmd = [
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
    if chunk_size is not None:
        cmd += ["--chunk-size", str(chunk_size)]
    return cmd


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

    def __init__(
        self,
        ctx,
        log_dir: Path,
        bw_mbps: int,
        run_tag: str,
        interval_s: float = LINK_SAMPLE_INTERVAL_S,
        override_bw_mbps: Optional[Dict[str, int]] = None,
    ):
        self.ctx = ctx
        self.log_dir = log_dir
        self.interval_s = interval_s
        self.bw_mbps = bw_mbps
        self.run_tag = run_tag
        self.override_bw_mbps = override_bw_mbps or {}
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
                "override_bw_mbps": self.override_bw_mbps,
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
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                print(f"{self.run_tag} link sampler: stop timed out; thread still alive.")

    def _read_tx(self) -> Dict[str, float]:
        readings: Dict[str, float] = {}
        for node, names in self._interface_map.items():
            if not names:
                continue
            paths = [f"/sys/class/net/{n}/statistics/tx_bytes" for n in names]
            proc = None
            try:
                proc = node.popen(
                    ["cat", *paths],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                out, _ = proc.communicate(timeout=1.0)
            except Exception:
                if proc is not None:
                    try:
                        proc.kill()
                        proc.communicate(timeout=1.0)
                    except Exception:
                        pass
                continue
            raw = (out or "").strip().split()
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

        try:
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
                        bw_mbps = self.override_bw_mbps.get(if_name, self.bw_mbps)
                        util = (delta * 8.0) / (bw_mbps * 1_000_000 * dt)
                        writer.writerow([sample_idx, elapsed, if_name, delta, util, dt])
                    prev = curr
                    last_time = now
                    sample_idx += 1
        except Exception as exc:
            print(f"{self.run_tag} link sampler: crashed: {exc}")


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
        if proto == "mptcp":
            host.cmd("ip mptcp limits set subflow 3 2>/dev/null || true")


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
        print(f"[abnormal] warning: failed to read ifindex for {host.name}:{iface} ({ifindex_raw}); skipping -A.")
        return ""
    return ",".join(f"{ip}/{ifindex}" for ip in alt_ips)


def _ensure_mptcp_endpoints(host, iface: Optional[str], coords: Optional[Tuple[int, int, int]]) -> None:
    alt_ips = _extra_host_ips(coords)
    if not alt_ips or not iface:
        return
    for ip in alt_ips:
        host.cmd(
            f"ip mptcp endpoint show | grep -w '{ip}' >/dev/null 2>&1 || "
            f"ip mptcp endpoint add {ip} dev {iface} subflow"
        )


def _pick_random_pairs(
    hosts: Sequence,
    count: int,
    src_pool: Optional[Sequence] = None,
    dst_pool: Optional[Sequence] = None,
    rng: Optional[random.Random] = None,
) -> List[Tuple]:
    pairs: List[Tuple] = []
    if len(hosts) < 2:
        return pairs
    sources = list(src_pool) if src_pool is not None else list(hosts)
    destinations = list(dst_pool) if dst_pool is not None else list(hosts)
    if not sources or not destinations:
        return pairs
    rng = rng or random
    for _ in range(count):
        src = rng.choice(sources)
        dst = rng.choice(destinations)
        while dst == src and len(destinations) > 1:
            dst = rng.choice(destinations)
        pairs.append((src, dst))
    return pairs


def _pick_cross_pod_pairs(
    hosts: Sequence,
    count: int,
    host_coord_map: Dict[object, Tuple[int, int, int]],
    src_pool: Optional[Sequence] = None,
    dst_pool: Optional[Sequence] = None,
    rng: Optional[random.Random] = None,
) -> List[Tuple]:
    """
    Select src/dst pairs such that src and dst belong to different pods.
    Hosts may repeat across pairs.
    """
    pairs: List[Tuple] = []
    if len(hosts) < 2 or not host_coord_map:
        return pairs
    rng = rng or random
    src_by_pod: Dict[int, List[object]] = {}
    dst_by_pod: Dict[int, List[object]] = {}
    src_candidates = set(src_pool) if src_pool is not None else set(hosts)
    dst_candidates = set(dst_pool) if dst_pool is not None else set(hosts)
    for host, coords in host_coord_map.items():
        pod_idx = coords[0]
        if host in src_candidates:
            src_by_pod.setdefault(pod_idx, []).append(host)
        if host in dst_candidates:
            dst_by_pod.setdefault(pod_idx, []).append(host)
    src_pods = [p for p, hs in src_by_pod.items() if hs]
    dst_pods = [p for p, hs in dst_by_pod.items() if hs]
    if len(src_pods) < 1 or len(dst_pods) < 2:
        return pairs

    for _ in range(count):
        src_pod = rng.choice(src_pods)
        dst_pod_choices = [p for p in dst_pods if p != src_pod]
        if not dst_pod_choices:
            break
        dst_pod = rng.choice(dst_pod_choices)
        src_hosts = src_by_pod.get(src_pod, [])
        dst_hosts = dst_by_pod.get(dst_pod, [])
        if not src_hosts or not dst_hosts:
            continue
        src = rng.choice(src_hosts)
        dst = rng.choice(dst_hosts)
        pairs.append((src, dst))
    return pairs


def _pick_cross_pod_pairs_unique_hosts(
    hosts: Sequence,
    count: int,
    host_coord_map: Dict[object, Tuple[int, int, int]],
    src_pool: Optional[Sequence] = None,
    dst_pool: Optional[Sequence] = None,
    rng: Optional[random.Random] = None,
) -> List[Tuple]:
    """
    Select src/dst pairs across pods with globally unique hosts.
    Raises RuntimeError if the requested count cannot be satisfied.
    """
    if count <= 0:
        return []
    if len(hosts) < 2 or not host_coord_map:
        raise RuntimeError("Unable to select elephant pairs: insufficient hosts.")

    rng = rng or random
    src_candidates_raw = list(src_pool) if src_pool is not None else list(hosts)
    dst_candidates_raw = list(dst_pool) if dst_pool is not None else list(hosts)

    def _dedup(seq: List[object]) -> List[object]:
        seen = set()
        out: List[object] = []
        for h in seq:
            if h in seen:
                continue
            if h not in host_coord_map:
                continue
            out.append(h)
            seen.add(h)
        return out

    src_candidates = _dedup(src_candidates_raw)
    dst_candidates = _dedup(dst_candidates_raw)

    if len(src_candidates) < count or len(dst_candidates) < count:
        raise RuntimeError(
            f"Unable to select {count} elephant pairs: need at least {count} unique "
            f"senders and receivers (have {len(src_candidates)} senders, {len(dst_candidates)} receivers)."
        )

    src_candidates.sort(key=lambda h: h.name)
    dst_candidates.sort(key=lambda h: h.name)
    rng.shuffle(src_candidates)
    rng.shuffle(dst_candidates)

    pairs: List[Tuple] = []
    used_src = set()
    used_dst = set()

    for src in src_candidates:
        if len(pairs) >= count:
            break
        if src in used_src:
            continue
        src_pod = host_coord_map.get(src, (None, None, None))[0]
        dst_choices = [
            dst
            for dst in dst_candidates
            if dst not in used_dst
            and dst != src
            and host_coord_map.get(dst, (None, None, None))[0] != src_pod
        ]
        if not dst_choices:
            continue
        dst = rng.choice(dst_choices)
        used_src.add(src)
        used_dst.add(dst)
        pairs.append((src, dst))

    if len(pairs) < count:
        raise RuntimeError(
            f"Unable to select {count} elephant pairs with unique hosts across pods; "
            f"selected {len(pairs)}."
        )
    return pairs


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
    chunk_size: int,
    run_tag: str,
    pair_lambda: float,
    rng: random.Random,
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

        if pair_lambda <= 0:
            break
        sleep_time = rng.expovariate(pair_lambda)
        stop_event.wait(sleep_time)
        if stop_event.is_set() or (end_time is not None and time.time() >= end_time):
            break

        seq += 1
        size_bytes = rng.randint(MOUSE_SIZE_MIN, MOUSE_SIZE_MAX)
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
                chunk_size=chunk_size,
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
            f"picoquicdemo -a perf {extra_str} -F /tmp/abnormal_hc_{label}.csv "
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
            chunk_size=TCP_MOUSE_CHUNK_SIZE if label == "mouse" else TCP_ELEPHANT_CHUNK_SIZE,
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


def run_abnormal_once(
    proto: str,
    k: int,
    base_seed: int,
    run_index: int,
    elephant_num: int,
    bw_mbps: int,
    enable_qlog: bool = False,
    output_subdir: Optional[Path] = None,
) -> None:
    """Execute one abnormal bandwidth-injection experiment run."""
    seed = base_seed + run_index
    rng = random.Random(seed)
    run_tag = f"[run{run_index:04d}]"
    print(
        f"{run_tag} starting abnormal run (proto={proto}, k={k}, seed={seed}, "
        f"E={elephant_num}, bw={bw_mbps}Mbps)"
    )

    log_root = Path("logs") / LOG_ROOT_NAME / (output_subdir or Path("default"))
    log_dir = make_log_dir(
        LOG_ROOT_NAME,
        proto,
        log_root=log_root,
        suffix=f"seed{seed}",
        extra_parts=[str(elephant_num), _format_bw_dir(bw_mbps)],
    )

    ctx = None
    server_procs: List[object] = []
    elephant_procs: List[object] = []
    mouse_threads: List[threading.Thread] = []
    mouse_proc_lists: List[List[object]] = []
    mouse_stop = threading.Event()
    all_mouse_procs: List[object] = []
    link_sampler: Optional[LinkSampler] = None
    def _ensure_servers_alive(context: str) -> None:
        dead = [proc for proc in server_procs if proc and proc.poll() is not None]
        if dead:
            raise RuntimeError(
                f"Server process died {context}; see server logs under {log_dir} "
                f"(elephant_server_*.log / mouse_server_*.log)."
            )

    try:
        ctx = create_fattree(
            k=k,
            bw_mbps=DEFAULT_LINK_BW_MBPS,
            delay=DEFAULT_LINK_DELAY_MS,
            queue_pkts=DEFAULT_SWITCH_QUEUE_PKTS,
        )
        hosts_flat = [h for pod in ctx.hosts for edge in pod for h in edge]
        print(f"{run_tag} hosts ready: {len(hosts_flat)} total.")
        _apply_tcp_sysctls(hosts_flat, proto)
        try:
            bw_intfs = _bw_target_interfaces(ctx)
            _apply_static_bandwidth(bw_intfs, ctx.link_params, bw_mbps)
            print(
                f"{run_tag} bw={bw_mbps}Mbps applied on "
                f"a{BW_TARGET_POD}{BW_TARGET_AGG}-c{BW_TARGET_CORE} before traffic."
            )
        except Exception as exc:
            reason = f"Static bandwidth apply failed: {exc}"
            print(f"{run_tag} {reason}", file=sys.stderr)
            _mark_run_invalid(log_dir, run_tag, reason)
            return

        ele_src_pool = mouse_src_pool = hosts_flat

        host_coord_map = _build_host_coord_map(ctx)

        elephant_pairs = _pick_cross_pod_pairs_unique_hosts(
            hosts_flat,
            elephant_num,
            host_coord_map,
            src_pool=ele_src_pool,
            dst_pool=hosts_flat,
            rng=rng,
        )

        mouse_pairs = _pick_cross_pod_pairs(
            hosts_flat,
            MOUSE_PAIR_COUNT,
            host_coord_map,
            src_pool=mouse_src_pool,
            dst_pool=hosts_flat,
            rng=rng,
        )
        if len(mouse_pairs) < MOUSE_PAIR_COUNT:
            print(
                f"{run_tag} warning: cross-pod mouse selection yielded {len(mouse_pairs)} pairs; falling back to random."
            )
            mouse_pairs = _pick_random_pairs(
                hosts_flat, MOUSE_PAIR_COUNT, src_pool=mouse_src_pool, rng=rng
            )

        print(
            f"{run_tag} selected pairs: elephants={len(elephant_pairs)}, mice={len(mouse_pairs)}"
        )

        mouse_lambda_per_pair = MOUSE_TOTAL_LAMBDA / len(mouse_pairs) if mouse_pairs else 0.0

        # Persist pair selection to ease debugging when flows fail.
        pair_log = log_dir / "pair_map.txt"
        with pair_log.open("w") as f:
            f.write(f"Total mouse lambda: {MOUSE_TOTAL_LAMBDA} flows/s, per-pair lambda: {mouse_lambda_per_pair:.3f} flows/s\n\n")
            f.write("Elephant pairs (src -> dst):\n")
            for idx, (src, dst) in enumerate(elephant_pairs):
                f.write(f"{idx:02d}: {src.name} -> {dst.name} (dst_ip={dst.IP()})\n")
            f.write("\nMouse pairs (src -> dst):\n")
            for idx, (src, dst) in enumerate(mouse_pairs):
                f.write(f"{idx:02d}: {src.name} -> {dst.name} (dst_ip={dst.IP()})\n")
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
                    _tcp_perf_server_cmd(ELEPHANT_PORT, proto, chunk_size=TCP_ELEPHANT_CHUNK_SIZE),
                    stdout=log_path.open("w"),
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                server_procs.append(proc)
            for host in mouse_server_hosts:
                log_path = log_dir / f"mouse_server_{host.name}.log"
                proc = host.popen(
                    _tcp_perf_server_cmd(MOUSE_PORT, mouse_proto, chunk_size=TCP_MOUSE_CHUNK_SIZE),
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

        mouse_extra = (
            get_extra_args(mouse_proto, ROLE_MOUSE_CLIENT) if mouse_proto in ("quic", "mpquic") else []
        )
        elephant_target_bytes = int(ELEPHANT_MBYTES * 1_000_000)
        if elephant_target_bytes <= 0:
            raise ValueError("elephant bytes must be positive (check ELEPHANT_MBYTES).")
        elephant_scenario = f"*1:{elephant_target_bytes}:0;" if proto in ("quic", "mpquic") else None
        with (log_dir / "pair_map.txt").open("a") as f:
            f.write(
                f"\nElephant target bytes per flow: {elephant_target_bytes} "
                f"(M={ELEPHANT_MBYTES} MBytes)\n"
            )

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
        _ensure_servers_alive("after healthcheck")

        bw_override: Dict[str, int] = {}
        for intf in bw_intfs:
            name = getattr(intf, "name", None)
            if name:
                bw_override[name] = bw_mbps

        link_sampler = LinkSampler(
            ctx,
            log_dir=log_dir,
            bw_mbps=DEFAULT_LINK_BW_MBPS,
            run_tag=run_tag,
            interval_s=LINK_SAMPLE_INTERVAL_S,
            override_bw_mbps=bw_override,
        )

        print(f"{run_tag} warming up for {WARMUP_SECONDS}s.")
        time.sleep(WARMUP_SECONDS)

        print(f"{run_tag} capturing switch stats (before measurement).")
        before_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_before.json").write_text(
            json.dumps(before_stats, indent=2)
        )
        _ensure_servers_alive("after warmup")
        link_sampler.start()

        start_time = time.time()

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
                    chunk_size=TCP_ELEPHANT_CHUNK_SIZE,
                )
            proc = src.popen(elephant_cmd, shell=False)
            elephant_procs.append(proc)

        print(f"{run_tag} starting mouse generator threads.")
        for idx, (src, dst) in enumerate(mouse_pairs):
            dst_ip = dst.IP()
            proc_list: List[object] = []
            mouse_proc_lists.append(proc_list)
            mouse_rng = random.Random(seed + 1000 + idx)
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
                    TCP_MOUSE_CHUNK_SIZE,
                    run_tag,
                    mouse_lambda_per_pair,
                    mouse_rng,
                ),
                daemon=True,
            )
            mouse_threads.append(thread)
            thread.start()

        print(f"{run_tag} traffic running; waiting for elephant completion (timeout {ELEPHANT_CLIENT_TIMEOUT_S:.0f}s).")
        elephant_deadline = start_time + ELEPHANT_CLIENT_TIMEOUT_S
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
                f"{run_tag} elephant {idx:02d} exceeded {ELEPHANT_CLIENT_TIMEOUT_S:.0f}s; sending SIGTERM."
            )
            _wait_for_completion_then_terminate(
                proc,
                wait_timeout=0.0,
                term_timeout=1.0,
                label=f"{run_tag} elephant {idx:02d}",
            )

        mouse_stop.set()
        for thread in mouse_threads:
            thread.join()

        all_mouse_procs: List[object] = [proc for plist in mouse_proc_lists for proc in plist]
        _terminate_processes(all_mouse_procs, term_timeout=DEFAULT_KILL_GRACE_SECONDS)

        # Stop link sampler before snapshot to finalize the timeseries and reduce background load.
        if link_sampler:
            link_sampler.stop()

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
    parser = argparse.ArgumentParser(
        description="Abnormal bandwidth experiment driver (QUIC/MPQUIC/TCP/MPTCP)."
    )
    parser.add_argument("--proto", required=True, choices=list(PROTO_CHOICES))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("default"),
        help=(
            "Subdirectory name under logs/abnormal. Each run writes to "
            "logs/abnormal/<output-dir>/<proto>/<elephant-num>/bw-<Mbps>/"
            "run_<timestamp>_seed<seed+run_index> (default: default)."
        ),
    )
    parser.add_argument(
        "--bw",
        type=_positive_bw_mbps,
        required=True,
        help="Bandwidth cap to inject on the target link in Mbps (10-1000).",
    )
    parser.add_argument(
        "--elephant-num",
        type=int,
        default=4,
        help="Number of Elephant flows to launch (default 4).",
    )
    parser.add_argument(
        "--enable-qlog",
        action="store_true",
        help="Enable picoquicdemo -l qlog capture for servers (default: disabled for performance).",
    )
    args = parser.parse_args()

    for run_idx in range(args.runs):
        run_abnormal_once(
            proto=args.proto,
            k=args.k,
            base_seed=args.seed,
            run_index=run_idx,
            elephant_num=args.elephant_num,
            bw_mbps=args.bw,
            enable_qlog=args.enable_qlog,
            output_subdir=args.output_dir,
        )


if __name__ == "__main__":
    main()
