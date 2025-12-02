"""Blackbox experiments (Step 5).

This module launches random Elephant/Mouse flows across the Fat-Tree and
captures switch statistics to compare QUIC vs MPQUIC under ECMP.
"""

import argparse
import json
import random
import shlex
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from experiments import (
    create_fattree,
    make_log_dir,
    picoquic_perf_cmd,
    snapshot_switch_bytes,
)
from topology import stop_fattree_topology

DEFAULT_SCENARIO = "*1:1000:1000;"  # minimal valid perf scenario
DEFAULT_LINK_BW_MBPS = 1000  # keep in sync with create_fattree call
DEFAULT_ELEPHANT_LOAD_FRAC = 0.7  # fraction of link capacity to target when auto-sizing Elephant payload

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
ELEPHANT_ALT_ADDRS_MPQUIC = "10.0.0.6/2,10.0.0.7/2,10.0.0.8/2"  # TODO: validate against actual host IPs/ifindex.
EXTRA_ARGS_QUIC = ""
EXTRA_ARGS_MPQUIC = f"-M -A {ELEPHANT_ALT_ADDRS_MPQUIC}"
# Mouse flows remain single-path; payload size is controlled via the scenario string.
EXTRA_ARGS_MOUSE = ""


def _format_extra_args(extra_args: Union[Iterable[str], str, None]) -> str:
    if not extra_args:
        return ""
    if isinstance(extra_args, str):
        return extra_args.strip()
    return " ".join(shlex.quote(str(a)) for a in extra_args if str(a))


def _start_picoquic_server(
    host, port: int, log_path: Path, template: str, extra_args, enable_qlog: bool
) -> object:
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


def _terminate_processes(procs: List[object]) -> None:
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


def _pick_random_pairs(hosts: Sequence, count: int) -> List[Tuple]:
    pairs: List[Tuple] = []
    if len(hosts) < 2:
        return pairs
    for _ in range(count):
        src = random.choice(hosts)
        dst = random.choice(hosts)
        while dst == src:
            dst = random.choice(hosts)
        pairs.append((src, dst))
    return pairs


def _mouse_generator(
    pair_index: int,
    src_host,
    dst_ip: str,
    log_dir: Path,
    start_time: float,
    total_duration: float,
    stop_event: threading.Event,
    proc_store: List[object],
    run_tag: str,
) -> None:
    """Poisson arrivals of short Mouse flows with periodic heartbeats."""
    seq = 0
    next_heartbeat = start_time + MOUSE_HEARTBEAT_INTERVAL
    prefix = f"{run_tag} " if run_tag else ""
    end_time = start_time + total_duration
    while not stop_event.is_set():
        now = time.time()
        if now >= end_time:
            break
        if now >= next_heartbeat:
            print(
                f"{prefix}[mouse-{pair_index:02d}] t={now - start_time:.1f}s flows={seq}"
            )
            next_heartbeat += MOUSE_HEARTBEAT_INTERVAL

        sleep_time = random.expovariate(MOUSE_LAMBDA)
        stop_event.wait(sleep_time)
        if stop_event.is_set() or time.time() >= end_time:
            break

        seq += 1
        size_bytes = random.randint(MOUSE_SIZE_MIN, MOUSE_SIZE_MAX)
        scenario = f"*1:{size_bytes}:0;"
        csv_path = log_dir / f"mouse_{pair_index:02d}_{seq:04d}.csv"
        mouse_cmd = picoquic_perf_cmd(
            server_ip=dst_ip,
            server_port=MOUSE_PORT,
            csv_path=csv_path,
            scenario=scenario,
            extra_args=EXTRA_ARGS_MOUSE,
        )
        proc = src_host.popen(mouse_cmd, shell=True)
        proc_store.append(proc)
        # Drop completed processes to keep the list small.
        proc_store[:] = [p for p in proc_store if p and p.poll() is None]


def _elephant_client_extra(proto: str) -> List[str]:
    proto = (proto or "").lower()
    if proto == "mpquic":
        return [a for a in EXTRA_ARGS_MPQUIC.split() if a]
    return [a for a in EXTRA_ARGS_QUIC.split() if a]


def _elephant_server_extra(proto: str) -> List[str]:
    proto = (proto or "").lower()
    if proto == "mpquic":
        return ["-M"]
    return []


def _healthcheck(
    label: str,
    proto: str,
    server_host,
    client_host,
    server_ip: str,
    port: int,
    log_dir: Path,
    run_tag: str = "",
) -> None:
    """
    Lightweight pre-flight: ensure server process exists, ping works, and a short perf
    session completes using the same extra args as the real run.
    """
    log_path = log_dir / f"healthcheck_{label}.log"
    lines: List[str] = []
    prefix = f"{run_tag} " if run_tag else ""

    def _log(msg: str) -> None:
        print(f"{prefix}{msg}")
        lines.append(msg)

    # Server presence
    procs = server_host.cmd("pgrep -a picoquicdemo")
    procs_clean = procs.strip()
    server_ok = bool(procs_clean)
    _log(f"[health:{label}] server procs: {procs_clean or 'none'}")

    # Ping reachability
    ping_out = client_host.cmd(f"ping -c1 -W1 {server_ip}; echo HC_RC=$?")
    ping_ok = "HC_RC=0" in ping_out
    _log(f"[health:{label}] ping ->\n{ping_out.strip()}")

    # QUIC perf short run with correct extra args
    client_extra = _elephant_client_extra(proto) if label == "elephant" else [a for a in EXTRA_ARGS_MOUSE.split() if a]
    extra_str = _format_extra_args(client_extra)
    hc_timeout = 10
    scenario = DEFAULT_SCENARIO
    hc_cmd = (
        f"timeout {hc_timeout} "
        f"picoquicdemo -a perf {extra_str} -F /tmp/blackbox_hc_{label}.csv "
        f"{server_ip} {port} {shlex.quote(scenario)}"
    )
    hc_out = client_host.cmd(f"{hc_cmd}; echo HC_RC=$?")
    _log(f"[health:{label}] quic (scenario={scenario}, extra_args={extra_str or 'none'}) ->\n{hc_out.strip()}")

    hc_exit_code = None
    for line in hc_out.splitlines():
        if "Client exit with code" in line:
            try:
                hc_exit_code = int(line.rsplit("=", 1)[-1].strip())
            except Exception:
                pass
    quic_ok = "HC_RC=0" in hc_out and hc_exit_code == 0

    log_path.write_text("\n".join(lines) + "\n")

    if not (server_ok and ping_ok and quic_ok):
        raise RuntimeError(
            f"healthcheck {label} failed (server_ok={server_ok}, ping_ok={ping_ok}, quic_ok={quic_ok}); "
            f"see {log_path}"
        )


def run_blackbox_once(
    proto: str,
    k: int,
    duration: float,
    base_seed: int,
    run_index: int,
    elephant_bytes: Optional[int],
    elephant_load_fraction: float,
    enable_qlog: bool = False,
) -> None:
    """Execute one blackbox experiment run."""
    seed = base_seed + run_index
    random.seed(seed)
    run_tag = f"[run{run_index:04d}]"
    print(
        f"{run_tag} starting blackbox run (proto={proto}, k={k}, duration={duration}s, seed={seed})"
    )

    log_dir = make_log_dir("blackbox", proto)

    ctx = None
    server_procs: List[object] = []
    elephant_procs: List[object] = []
    mouse_threads: List[threading.Thread] = []
    mouse_proc_lists: List[List[object]] = []
    mouse_stop = threading.Event()

    try:
        ctx = create_fattree(k=k, bw_mbps=DEFAULT_LINK_BW_MBPS, delay="0.05ms", queue_pkts=75)
        hosts_flat = [h for pod in ctx.hosts for edge in pod for h in edge]
        print(f"{run_tag} hosts ready: {len(hosts_flat)} total.")

        print(f"{run_tag} capturing switch stats (before).")
        before_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_before.json").write_text(
            json.dumps(before_stats, indent=2)
        )

        elephant_pairs = _pick_random_pairs(hosts_flat, ELEPHANT_PAIR_COUNT)
        mouse_pairs = _pick_random_pairs(hosts_flat, MOUSE_PAIR_COUNT)
        print(
            f"{run_tag} selected pairs: elephants={len(elephant_pairs)}, mice={len(mouse_pairs)}"
        )

        elephant_server_hosts = {dst.name: dst for _, dst in elephant_pairs}.values()
        mouse_server_hosts = {dst.name: dst for _, dst in mouse_pairs}.values()

        print(f"{run_tag} starting picoquicdemo servers for destinations.")
        for host in elephant_server_hosts:
            log_path = log_dir / f"elephant_server_{host.name}.log"
            server_procs.append(
                _start_picoquic_server(
                    host,
                    ELEPHANT_PORT,
                    log_path,
                    ELEPHANT_SERVER_CMD_TEMPLATE,
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
                    MOUSE_SERVER_CMD_TEMPLATE,
                    [],
                    enable_qlog,
                )
            )

        start_time = time.time()
        elephant_extra = _elephant_client_extra(proto)
        elephant_target_bytes = elephant_bytes
        if elephant_target_bytes is None:
            link_bps = DEFAULT_LINK_BW_MBPS * 1_000_000
            elephant_target_bytes = int(elephant_load_fraction * link_bps / 8 * duration)
        if elephant_target_bytes <= 0:
            raise ValueError("elephant_bytes must be positive when provided or computed.")
        elephant_scenario = f"*1:{elephant_target_bytes}:0;"

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
            )
        if mouse_pairs:
            src, dst = mouse_pairs[0]
            _healthcheck(
                label="mouse",
                proto=proto,
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
            elephant_cmd = picoquic_perf_cmd(
                server_ip=dst_ip,
                server_port=ELEPHANT_PORT,
                csv_path=csv_path,
                scenario=elephant_scenario,
                extra_args=elephant_extra,
            )
            proc = src.popen(elephant_cmd, shell=True)
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
                    duration,
                    mouse_stop,
                    proc_list,
                    run_tag,
                ),
                daemon=True,
            )
            mouse_threads.append(thread)
            thread.start()

        print(f"{run_tag} traffic running for {duration}s.")
        time.sleep(duration)
        mouse_stop.set()
        for thread in mouse_threads:
            thread.join()

        for proc in elephant_procs:
            if not proc:
                continue
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        after_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_after.json").write_text(
            json.dumps(after_stats, indent=2)
        )
        print(f"{run_tag} switch stats captured (after).")
    finally:
        mouse_stop.set()
        for thread in mouse_threads:
            if thread.is_alive():
                thread.join(timeout=2)
        all_mouse_procs: List[object] = [
            proc for plist in mouse_proc_lists for proc in plist
        ]
        _terminate_processes(elephant_procs + server_procs + all_mouse_procs)
        stop_fattree_topology(ctx)
        print(f"{run_tag} teardown complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blackbox experiment driver.")
    parser.add_argument("--proto", required=True, choices=["quic", "mpquic"])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
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
    args = parser.parse_args()

    for run_idx in range(args.runs):
        run_blackbox_once(
            proto=args.proto,
            k=args.k,
            duration=args.duration,
            base_seed=args.seed,
            run_index=run_idx,
            elephant_bytes=args.elephant_bytes,
            elephant_load_fraction=args.elephant_load_frac,
            enable_qlog=args.enable_qlog,
        )


if __name__ == "__main__":
    main()
