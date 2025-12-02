"""Utility helpers for Fat-Tree experiments."""

from pathlib import Path
import shlex
import time
from typing import Dict, Optional

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
    Interfaces without stats files are skipped.
    """
    stats: Dict[str, Dict[str, int]] = {}

    nodes = list(ctx.cores)
    nodes += [agg for pod in ctx.aggs for agg in pod]
    nodes += [edge for pod in ctx.edges for edge in pod]

    for node in nodes:
        for intf in node.intfList():
            name: Optional[str] = getattr(intf, "name", None)
            if not name:
                continue
            tx_path = Path("/sys/class/net") / name / "statistics" / "tx_bytes"
            rx_path = Path("/sys/class/net") / name / "statistics" / "rx_bytes"
            try:
                tx_val = int(tx_path.read_text().strip())
                rx_val = int(rx_path.read_text().strip())
            except (FileNotFoundError, ValueError, OSError):
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
    duration: Optional[float] = 60.0,
    extra_args: str = "",
) -> str:
    """
    Build a picoquicdemo perf-mode command string.

    extra_args is appended as-is when non-empty, allowing callers to inject
    additional picoquicdemo flags.
    """
    parts = ["picoquicdemo", "-a", "perf", "-F", shlex.quote(str(csv_path))]
    if duration is not None:
        parts.extend(["-t", str(duration)])
    parts.extend([shlex.quote(str(server_ip)), str(server_port)])
    if extra_args:
        parts.append(extra_args.strip())
    return " ".join(parts)
