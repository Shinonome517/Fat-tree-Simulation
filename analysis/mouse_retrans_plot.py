"""
Compute and plot the fraction of Mouse flows with retransmissions (any > 0).

Each mouse CSV (one flow) is marked as "retrans" if max(retrans, 0) > 0,
ignoring the spurious column. The script aggregates per-run ratios and
produces a bar chart.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Sequence, Tuple

import matplotlib
import pandas as pd

# Headless backend for batch environments.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:
    from analysis.csv_utils import find_retrans_column, normalize_columns
except ImportError:  # pragma: no cover - fallback for direct script execution
    from csv_utils import find_retrans_column, normalize_columns

DEFAULT_LOG_ROOT = Path("./logs/whitebox")
DEFAULT_OUTPUT_DIR = Path("./analysis/plots")
DEFAULT_FILENAME = "whitebox_mouse_retrans_ratio.png"


@dataclass
class RetransSummary:
    label: str
    run_dir: Path
    retrans_flows: int
    total_flows: int

    @property
    def ratio(self) -> float:
        if self.total_flows == 0:
            return 0.0
        return self.retrans_flows / self.total_flows


def _flow_has_retrans(csv_path: Path) -> bool:
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        logging.warning("Mouse CSV not found: %s", csv_path)
        return False
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read %s: %s", csv_path, exc)
        return False

    df = normalize_columns(df)
    retrans_col = find_retrans_column(df.columns)
    if retrans_col is None:
        logging.warning("Column 'retrans.' missing in %s", csv_path)
        return False

    retrans_vals = pd.to_numeric(df[retrans_col], errors="coerce").fillna(0)
    return bool((retrans_vals.clip(lower=0) > 0).any())


def _list_mouse_csvs(run_dir: Path) -> List[Path]:
    return sorted(run_dir.glob("mouse*.csv"))


def summarize_run(run_dir: Path, label: str | None = None) -> RetransSummary:
    csv_paths = _list_mouse_csvs(run_dir)
    retrans_flows = sum(1 for path in csv_paths if _flow_has_retrans(path))
    return RetransSummary(
        label=label or run_dir.name,
        run_dir=run_dir,
        retrans_flows=retrans_flows,
        total_flows=len(csv_paths),
    )


def summarize_runs(run_dirs: Iterable[Tuple[str, Path]]) -> List[RetransSummary]:
    return [summarize_run(run_dir, label) for label, run_dir in run_dirs]


def plot_retrans_ratios(
    summaries: Sequence[RetransSummary],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    filename: str | None = None,
    title: str | None = None,
    color_map: Mapping[str, str] | None = None,
) -> Path:
    if not summaries:
        raise ValueError("No run summaries provided for plotting.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (filename or f"mouse_retrans_{int(time.time())}.png")

    labels = [s.label for s in summaries]
    values = [s.ratio * 100 for s in summaries]

    fig_width = max(4.0, len(labels) * 1.4)
    fig, ax = plt.subplots(figsize=(fig_width, 4.0))

    if color_map:
        colors = [
            color_map.get(label.split(":", 1)[0].lower(), "#e67e22")
            for label in labels
        ]
        bars = ax.bar(labels, values, color=colors)
    else:
        bars = ax.bar(labels, values, color="#e67e22")
    ax.set_ylabel("Flows with retransmissions (%)")
    if title:
        ax.set_title(title)
    ax.set_ylim(0, 100)
    for tick in ax.get_xticklabels():
        tick.set_rotation(20)
        tick.set_ha("right")

    for bar, summary in zip(bars, summaries):
        ratio_pct = summary.ratio * 100
        text = f"{summary.retrans_flows}/{summary.total_flows} ({ratio_pct:.1f}%)"
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1,
            text,
            ha="center",
            va="bottom",
            fontsize=8,
        )

    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    return output_path


def _list_run_dirs(log_root: Path, proto: str) -> List[Path]:
    proto_dir = log_root / proto
    if not proto_dir.exists() or not proto_dir.is_dir():
        logging.warning("Protocol directory not found: %s", proto_dir)
        return []
    return sorted([d for d in proto_dir.iterdir() if d.is_dir() and d.name.startswith("run_")])


def _select_run_dirs(
    log_root: Path, protos: Sequence[str], run_ids: Sequence[str] | None
) -> List[Tuple[str, Path]]:
    run_dirs: List[Tuple[str, Path]] = []
    for proto in protos:
        candidates = _list_run_dirs(log_root, proto)
        if not candidates:
            continue
        if not run_ids:
            selected = candidates[-1:]
        else:
            selected = [d for d in candidates if d.name in set(run_ids)]
        for run_dir in selected:
            run_dirs.append((f"{proto}:{run_dir.name}", run_dir))
    return run_dirs


def _infer_protos(log_root: Path) -> List[str]:
    if not log_root.exists() or not log_root.is_dir():
        return []
    return sorted([d.name for d in log_root.iterdir() if d.is_dir()])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate and plot the ratio of mouse flows with retransmissions."
    )
    parser.add_argument(
        "--log-root",
        type=Path,
        default=DEFAULT_LOG_ROOT,
        help="Root directory containing whitebox logs (default: logs/whitebox).",
    )
    parser.add_argument(
        "--proto",
        action="append",
        help="Protocols to include (default: all subdirectories under log-root).",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        help="Run ID(s) to include (e.g., run_20251202-074952). Default: latest per proto.",
    )
    parser.add_argument(
        "--run-dir",
        action="append",
        type=Path,
        help="Explicit run directories; bypasses log-root scanning when provided.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the plot (default: analysis/plots).",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help=f"Output filename (default: {DEFAULT_FILENAME}).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional plot title.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.run_dir:
        run_dirs = [(rd.name, rd) for rd in args.run_dir]
    else:
        protos = args.proto or _infer_protos(args.log_root)
        if not protos:
            raise SystemExit(f"No protocol directories found under {args.log_root}")
        run_dirs = _select_run_dirs(args.log_root, protos, args.run_id)

    if not run_dirs:
        raise SystemExit("No run directories found for plotting.")

    summaries = summarize_runs(run_dirs)
    output_path = plot_retrans_ratios(
        summaries,
        output_dir=args.output_dir,
        filename=args.output_name or DEFAULT_FILENAME,
        title=args.title or "Mouse retransmission ratio",
    )
    total_flows = sum(s.total_flows for s in summaries)
    retrans_flows = sum(s.retrans_flows for s in summaries)
    logging.info(
        "Saved plot: %s (flows with retrans: %d/%d)",
        output_path,
        retrans_flows,
        total_flows,
    )


if __name__ == "__main__":  # pragma: no cover - manual execution
    main()
