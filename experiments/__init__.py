"""Utility helpers for Fat-Tree experiments."""

from pathlib import Path
import shlex
import time
from typing import Dict, Iterable, List, Optional

from topology import FatTreeContext, build_fattree_topology

__all__ = [
    "create_fattree",
    "snapshot_switch_bytes",
    "make_log_dir",
    "picoquic_perf_cmd",
]


def create_fattree(
    k: int = 4,
    bw_mbps: int = 1000,
    delay: str = "0.05ms",
    queue_pkts: int = 75,
) -> FatTreeContext:
    """Build and start a Fat-Tree topology with the given parameters."""
    return build_fattree_topology(
        bw_mbps=bw_mbps,
        delay=delay,
        queue_pkts=queue_pkts,
        start=True,
        k=k,
    )


def snapshot_switch_bytes(ctx: FatTreeContext) -> Dict[str, Dict[str, int]]:
    """
    Capture tx/rx byte counters for all switch interfaces in the topology.

    Returns a mapping of interface name -> {"tx": int, "rx": int}.
    Interfaces without stats files are skipped. Counters are read from each
    node's namespace (via node.cmd) to avoid missing interfaces that are not
    visible from the root namespace.
    """
    stats: Dict[str, Dict[str, int]] = {}

    nodes = list(ctx.cores)
    nodes += [agg for pod in ctx.aggs for agg in pod]
    nodes += [edge for pod in ctx.edges for edge in pod]

    for node in nodes:
        # Reading via node.cmd ensures we look inside the node's netns.
        for intf in node.intfList():
            name: Optional[str] = getattr(intf, "name", None)
            if not name:
                continue
            tx_path = f"/sys/class/net/{name}/statistics/tx_bytes"
            rx_path = f"/sys/class/net/{name}/statistics/rx_bytes"
            tx_raw = node.cmd(f"cat {tx_path} 2>/dev/null").strip()
            rx_raw = node.cmd(f"cat {rx_path} 2>/dev/null").strip()
            try:
                tx_val = int(tx_raw)
                rx_val = int(rx_raw)
            except (TypeError, ValueError):
                continue
            stats[name] = {"tx": tx_val, "rx": rx_val}
    return stats


def make_log_dir(exp_kind: str, proto: str) -> Path:
    """Create and return a timestamped log directory for the given experiment."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = Path("logs") / exp_kind / proto / f"run_{ts}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def picoquic_perf_cmd(
    server_ip: str,
    server_port: int,
    csv_path: str,
    scenario: Optional[str] = "*1:1000:1000;",
    duration: Optional[float] = None,
    extra_args: Optional[Iterable[str]] = None,
) -> str:
    """
    Build a picoquicdemo perf-mode command string.

    Scenarios are the preferred way to drive perf tests. If scenario is provided,
    it is appended after the server host/port. If scenario is None and duration
    is provided, duration is passed via -t for backward compatibility.
    extra_args is appended as-is when non-empty, allowing callers to inject
    additional picoquicdemo flags.
    """
    parts = ["picoquicdemo", "-a", "perf"]
    if extra_args:
        # Preserve historical behavior if a string is passed; otherwise expand the iterable.
        if isinstance(extra_args, str):
            extras: List[str] = [extra_args.strip()] if extra_args.strip() else []
        else:
            extras = [str(arg) for arg in extra_args if str(arg)]
        parts.extend(extras)
    parts.extend(["-F", shlex.quote(str(csv_path))])
    if duration is not None and scenario is None:
        parts.extend(["-t", str(duration)])
    parts.extend([shlex.quote(str(server_ip)), str(server_port)])
    if scenario:
        parts.append(shlex.quote(str(scenario)))
    return " ".join(parts)
