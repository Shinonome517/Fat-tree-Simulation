"""
Analyze whitebox experiment outputs (QUIC vs MPQUIC).

This script scans the whitebox log directory, aggregates elephant goodput,
mouse FCT, and switch tx byte deltas, then produces comparison plots and a
text summary.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List, Tuple

import pandas as pd

from fattree_heatmap import plot_fattree_heatmap, plot_fattree_topology
from whitebox_loader import collect_all_data, select_run_dirs
from whitebox_metrics import compute_fairness, write_summary
from scatter import plot_run_p99_scatter
from plots_fct import plot_fct_ccdf
from plots_goodput import plot_goodput_bar
from plots_link import plot_link_heatmap


LOG_ROOT_BASE = Path("./logs/whitebox")
DEFAULT_LOG_DIR_NAME = Path("default")
OUTPUT_ROOT = Path("./analysis/plots/white")
PROTO_ORDER = ("quic", "mpquic")
MOUSE_DROPLOSS_FILENAME = "whitebox_mouse_droploss_ratio.png"
MOUSE_RETRANS_FILENAME = "whitebox_mouse_retrans_ratio.png"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze whitebox experiment logs.")
    parser.add_argument(
        "--log-dir",
        dest="log_dir_name",
        type=Path,
        default=DEFAULT_LOG_DIR_NAME,
        help="Directory name under logs/whitebox to analyze (default: default).",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir_name",
        type=Path,
        default=Path("default"),
        help="Output subdirectory name under analysis/plots/white (default: default).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    run_select = parser.add_mutually_exclusive_group()
    run_select.add_argument(
        "--run-id",
        action="append",
        help=(
            "Run ID(s) to include (e.g., run_20251202-074952). "
            "Can be specified multiple times. "
            "Default: use the latest run_* per protocol."
        ),
    )
    run_select.add_argument(
        "--latest-n",
        type=_positive_int,
        help=(
            "Select the latest N run_* per protocol. "
            "Requires at least N runs for each protocol."
        ),
    )
    parser.add_argument(
        "--k",
        type=int,
        default=4,
        help="Fat-tree k parameter used to build the graph layout (default: 4).",
    )
    parser.add_argument(
        "--heatmap-mode",
        choices=["graph", "pivot"],
        default="graph",
        help="Heatmap style: 'graph' overlays on fat-tree, 'pivot' keeps the original matrix view.",
    )
    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _subset_mouse(mouse_df: pd.DataFrame, proto: str) -> pd.DataFrame:
    if "proto" not in mouse_df.columns:
        return mouse_df.iloc[0:0]
    return mouse_df[mouse_df["proto"] == proto]


def _count_true(subset: pd.DataFrame, column: str) -> int:
    if column not in subset.columns:
        return 0
    return int(subset[column].fillna(False).astype(bool).sum())


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    output_subdir = args.output_dir_name
    output_dir: Path = OUTPUT_ROOT / output_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Writing outputs to %s", output_dir)

    log_root = LOG_ROOT_BASE / args.log_dir_name
    try:
        run_dirs_by_proto = select_run_dirs(
            log_root,
            PROTO_ORDER,
            args.run_id,
            args.latest_n,
        )
    except ValueError as exc:
        logging.error("%s", exc)
        raise SystemExit(1) from exc
    total_runs = sum(len(v) for v in run_dirs_by_proto.values())
    if total_runs == 0:
        logging.error(
            "No run directories selected under %s for protocols: %s",
            log_root,
            ", ".join(PROTO_ORDER),
        )
        return

    for proto in PROTO_ORDER:
        runs = run_dirs_by_proto.get(proto, [])
        if runs:
            logging.info(
                "Using %d run(s) for %s: %s",
                len(runs),
                proto,
                ", ".join(d.name for d in runs),
            )
        else:
            logging.warning("No runs selected for %s", proto)

    run_list: List[Tuple[str, Path]] = [
        (f"{proto}:{run_dir.name}", run_dir)
        for proto in PROTO_ORDER
        for run_dir in run_dirs_by_proto.get(proto, [])
    ]

    data = collect_all_data(log_root, PROTO_ORDER, run_dirs_by_proto)
    elephant_df = data["elephant"]
    mouse_df = data["mouse"]
    link_df = data["link"]

    if elephant_df.empty and mouse_df.empty and link_df.empty:
        logging.error("No data found under %s", log_root)
        return

    fairness_df = compute_fairness(link_df)

    plot_fattree_topology(
        output_dir / f"fattree_topology_k{args.k}.png",
        k=args.k,
    )
    plot_goodput_bar(
        elephant_df,
        output_dir / "whitebox_goodput_bar.png",
        PROTO_ORDER,
    )
    plot_fct_ccdf(
        mouse_df,
        output_dir / "whitebox_fct_ccdf.png",
        PROTO_ORDER,
        mark_outliers=True,
    )
    plot_fct_ccdf(
        mouse_df,
        output_dir / "whitebox_fct_ccdf_no_outliers.png",
        PROTO_ORDER,
        exclude_outliers=True,
    )
    plot_run_p99_scatter(
        mouse_df,
        output_dir / "whitebox_mouse_p99_scatter.png",
        PROTO_ORDER,
    )

    if args.heatmap_mode == "pivot":
        plot_link_heatmap(
            link_df,
            output_dir / "whitebox_link_heatmap.png",
            PROTO_ORDER,
        )
    else:
        plot_fattree_heatmap(
            link_df,
            output_dir / "whitebox_link_heatmap.png",
            PROTO_ORDER,
            k=args.k,
        )

    write_summary(
        output_dir / "whitebox_summary.txt",
        elephant_df,
        mouse_df,
        fairness_df,
        PROTO_ORDER,
    )

    if not run_list:
        logging.warning("No runs available; skipping mouse drop/retrans plots.")
        return

    try:
        import analysis.mouse_droploss_plot as droploss
        import analysis.mouse_retrans_plot as retrans
    except ImportError:  # pragma: no cover - support script execution
        import mouse_droploss_plot as droploss
        import mouse_retrans_plot as retrans

    droploss_summaries: List[droploss.DropLossSummary] = []
    for proto in PROTO_ORDER:
        runs = run_dirs_by_proto.get(proto, [])
        if not runs:
            continue
        subset = _subset_mouse(mouse_df, proto)
        drop_flows = _count_true(subset, "had_drop_retrans")
        droploss_summaries.append(
            droploss.DropLossSummary(
                label=proto,
                run_dir=runs[-1],
                drop_flows=drop_flows,
                total_flows=len(subset),
            )
        )
    if droploss_summaries:
        droploss_path = droploss.plot_drop_retrans_ratios(
            droploss_summaries,
            output_dir=output_dir,
            filename=MOUSE_DROPLOSS_FILENAME,
            title="Mouse drop-induced retransmissions",
        )
        drop_flows = sum(s.drop_flows for s in droploss_summaries)
        total_flows = sum(s.total_flows for s in droploss_summaries)
        logging.info(
            "Wrote drop-loss plot to %s (drop-induced retrans flows: %d/%d).",
            droploss_path,
            drop_flows,
            total_flows,
        )
    else:
        logging.warning("No runs available for drop-loss plot.")

    retrans_summaries: List[retrans.RetransSummary] = []
    for proto in PROTO_ORDER:
        runs = run_dirs_by_proto.get(proto, [])
        if not runs:
            continue
        subset = _subset_mouse(mouse_df, proto)
        retrans_flows = _count_true(subset, "had_retrans")
        retrans_summaries.append(
            retrans.RetransSummary(
                label=proto,
                run_dir=runs[-1],
                retrans_flows=retrans_flows,
                total_flows=len(subset),
            )
        )

    if retrans_summaries:
        retrans_path = retrans.plot_retrans_ratios(
            retrans_summaries,
            output_dir=output_dir,
            filename=MOUSE_RETRANS_FILENAME,
            title="Mouse retransmission ratio",
        )
        retrans_flows = sum(s.retrans_flows for s in retrans_summaries)
        total_flows = sum(s.total_flows for s in retrans_summaries)
        logging.info(
            "Wrote retrans plot to %s (flows with retrans: %d/%d).",
            retrans_path,
            retrans_flows,
            total_flows,
        )
    else:
        logging.warning("No runs available for retrans plot.")


if __name__ == "__main__":
    main()
