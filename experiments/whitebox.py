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
from topology import stop_fattree_topology

WARMUP_SECONDS = 5
ELEPHANT_PORT = 4443
MOUSE_PORT = 4444
DEFAULT_SEED = 12345

# TODO: Set appropriate picoquicdemo CLI options for single-path QUIC Elephant runs.
EXTRA_ARGS_QUIC = ""
# TODO: Set appropriate picoquicdemo CLI options for multipath QUIC Elephant runs.
EXTRA_ARGS_MPQUIC = ""
# TODO: Set picoquicdemo CLI options that bound Mouse flow size (e.g., 16-64KB).
EXTRA_ARGS_MOUSE = ""

# TODO: Adjust server options (certs/logging/paths) for actual experiments.
ELEPHANT_SERVER_CMD_TEMPLATE = "picoquicdemo -a server -p {port} {extra} > {log_path} 2>&1"
MOUSE_SERVER_CMD_TEMPLATE = "picoquicdemo -a server -p {port} {extra} > {log_path} 2>&1"

__all__ = [
    "run_whitebox_once",
    "configure_paths_for_whitebox",
]


def configure_paths_for_whitebox(ctx, proto: str) -> None:
    """Stub for Step 4: configure fwmark-based policy routing to steer paths."""
    # Placeholder: actual path manipulation will be implemented in Step 4.
    return


def _start_picoquic_server(host, port: int, log_path: Path, template: str):
    cmd = template.format(
        port=port,
        log_path=shlex.quote(str(log_path)),
        extra="",
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
):
    seq = 0
    while not stop_event.is_set():
        now = time.time()
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
            extra_args=EXTRA_ARGS_MOUSE,
        )
        proc = host.popen(mouse_cmd, shell=True)
        proc_store.append(proc)


def run_whitebox_once(
    proto: str,
    k: int,
    duration: float,
    base_seed: int,
    run_index: int,
) -> None:
    """Execute one whitebox experiment run."""
    seed = base_seed + run_index
    random.seed(seed)

    log_dir = make_log_dir("whitebox", proto)

    ctx = None
    elephant_client_proc = None
    mouse_thread: Optional[threading.Thread] = None
    mouse_stop = threading.Event()
    mouse_procs: List = []
    server_procs: List = []

    try:
        ctx = create_fattree(k=k, bw_mbps=1000, delay="0.05ms", queue_pkts=75)

        elephant_client = ctx.net.get("h001")
        mouse_client = ctx.net.get("h201")
        server_host = ctx.net.get("h311")

        before_stats = snapshot_switch_bytes(ctx)
        (log_dir / "switch_stats_before.json").write_text(
            json.dumps(before_stats, indent=2)
        )

        # Start picoquicdemo servers for Elephant and Mouse.
        elephant_server_log = log_dir / "elephant_server.log"
        mouse_server_log = log_dir / "mouse_server.log"
        server_procs.append(
            _start_picoquic_server(
                server_host, ELEPHANT_PORT, elephant_server_log, ELEPHANT_SERVER_CMD_TEMPLATE
            )
        )
        server_procs.append(
            _start_picoquic_server(
                server_host, MOUSE_PORT, mouse_server_log, MOUSE_SERVER_CMD_TEMPLATE
            )
        )

        configure_paths_for_whitebox(ctx, proto)

        server_ip = server_host.IP()
        total_duration = WARMUP_SECONDS + duration

        elephant_csv = log_dir / "elephant_client.csv"
        elephant_extra = EXTRA_ARGS_MPQUIC if proto == "mpquic" else EXTRA_ARGS_QUIC
        elephant_cmd = picoquic_perf_cmd(
            server_ip=server_ip,
            server_port=ELEPHANT_PORT,
            csv_path=elephant_csv,
            duration=total_duration,
            extra_args=elephant_extra,
        )
        elephant_client_proc = elephant_client.popen(elephant_cmd, shell=True)

        start_time = time.time()
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
            ),
            daemon=True,
        )
        mouse_thread.start()

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
    finally:
        mouse_stop.set()
        if mouse_thread and mouse_thread.is_alive():
            mouse_thread.join(timeout=3)
        _terminate_processes(mouse_procs + [elephant_client_proc] + server_procs)
        stop_fattree_topology(ctx)


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
        )


if __name__ == "__main__":
    main()
