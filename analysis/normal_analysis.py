"""
Analyze normal experiment outputs (QUIC/MPQUIC/TCP/MPTCP).

This script scans the normal log directory, aggregates elephant goodput,
mouse flow completion time (FCT), and switch tx byte deltas, then produces
comparison plots and a text summary.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import matplotlib
import numpy as np
import pandas as pd

# Use non-interactive backend to work in headless environments.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from fattree_heatmap import plot_fattree_heatmap, plot_fattree_topology
from plots_link import plot_link_heatmap
from scatter import plot_run_p99_scatter
from blackbox_loader import collect_all_data, select_run_dirs
from blackbox_metrics import compute_fairness, write_summary


LOG_ROOT_BASE = Path("./logs/normal")
DEFAULT_LOG_DIR_NAME = Path("default")
OUTPUT_ROOT = Path("./analysis/plots/normal")
PROTO_ORDER = ("mpquic", "quic", "mptcp", "tcp")
MOUSE_DROPLOSS_FILENAME = "normal_mouse_droploss_ratio.png"
MOUSE_RETRANS_FILENAME = "normal_mouse_retrans_ratio.png"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze normal experiment logs.")
    parser.add_argument(
        "--log-dir",
        dest="log_dir_name",
        type=Path,
        default=DEFAULT_LOG_DIR_NAME,
        help="Directory name under logs/normal to analyze (default: default).",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir_name",
        type=Path,
        default=Path("default"),
        help="Output subdirectory name under analysis/plots/normal (default: default).",
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
            "Run ID(s) to include (e.g., run_20251202-074952_seed123). "
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


def _plot_cdf(ax, values: np.ndarray, label: str):
    values = np.sort(values)
    y = np.arange(1, len(values) + 1) / len(values)
    (line,) = ax.step(values, y, where="post", label=label)
    return line


def _outlier_mask(values: np.ndarray) -> Tuple[np.ndarray, float, float]:
    if values.size == 0:
        return np.zeros(0, dtype=bool), float("nan"), float("nan")
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    mask = (values < lower) | (values > upper)
    return mask, lower, upper


def plot_elephant_goodput_bar(
    elephant_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    means: List[float] = []
    stds: List[float] = []
    labels: List[str] = []
    for proto in protos:
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            logging.warning("No elephant goodput data for %s", proto)
            continue
        labels.append(proto)
        means.append(subset["goodput_mbps"].mean())
        stds.append(subset["goodput_mbps"].std(ddof=0))

    if not means:
        ax.text(0.5, 0.5, "No elephant data", ha="center", va="center")
    else:
        x = np.arange(len(labels))
        ax.bar(x, means, yerr=stds, capsize=8, alpha=0.8, label="mean +/- SD")
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylabel("Goodput (Mbps)")
        ax.set_title("Elephant Goodput (error bars = SD)")
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_elephant_goodput_scatter(
    elephant_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    rng = np.random.default_rng(seed=0)
    has_data = False
    positions: List[int] = []
    labels: List[str] = []
    for idx, proto in enumerate(protos):
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            logging.warning("No elephant goodput data for %s", proto)
            continue
        positions.append(idx)
        labels.append(proto)
        x = np.full(len(subset), idx, dtype=float)
        if len(subset) > 1:
            x += rng.uniform(-0.12, 0.12, size=len(subset))
        ax.scatter(
            x,
            subset["goodput_mbps"],
            alpha=0.8,
            edgecolors="black",
            linewidth=0.4,
            label=proto,
        )
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No elephant data", ha="center", va="center")
    else:
        ax.set_xticks(positions)
        ax.set_xticklabels(labels)
        ax.set_ylabel("Goodput (Mbps)")
        ax.set_title("Elephant Goodput per Flow")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_mouse_fct_cdf(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    exclude_outliers: bool = False,
    mark_outliers: bool = False,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    has_data = False
    for proto in protos:
        subset = mouse_df[mouse_df["proto"] == proto]
        if subset.empty:
            logging.warning("No mouse FCT data for %s", proto)
            continue
        values_all = subset["fct_s"].to_numpy()
        if values_all.size == 0:
            continue
        if "had_retrans" in subset.columns:
            had_retrans_all = subset["had_retrans"].to_numpy().astype(bool)
        else:
            had_retrans_all = np.zeros(values_all.shape, dtype=bool)
        outlier_mask, lower, upper = _outlier_mask(values_all)
        values = values_all[~outlier_mask] if exclude_outliers else values_all
        if values.size == 0:
            logging.warning("All mouse FCT values are outliers for %s", proto)
            continue
        line = _plot_cdf(ax, values, proto)
        color = line.get_color()
        p50, p90, p99 = np.percentile(values, [50, 90, 99])
        ax.scatter(
            [p50, p90, p99],
            [0.5, 0.9, 0.99],
            color=color,
            marker="x",
            s=25,
            label=f"{proto} p50/p90/p99",
        )
        line.set_label(f"{proto} (p50={p50:.3f}, p90={p90:.3f}, p99={p99:.3f})")
        if mark_outliers and not exclude_outliers and outlier_mask.any():
            sort_idx = np.argsort(values_all)
            values_sorted = values_all[sort_idx]
            retrans_sorted = had_retrans_all[sort_idx]
            y = np.arange(1, len(values_sorted) + 1) / len(values_sorted)
            sorted_mask = (values_sorted < lower) | (values_sorted > upper)
            outlier_y = y[sorted_mask]
            outlier_values = values_sorted[sorted_mask]
            outlier_retrans = retrans_sorted[sorted_mask]
            no_retrans_mask = ~outlier_retrans
            has_retrans_mask = outlier_retrans
            if np.any(no_retrans_mask):
                ax.scatter(
                    outlier_values[no_retrans_mask],
                    outlier_y[no_retrans_mask],
                    facecolors="none",
                    edgecolors=color,
                    marker="o",
                    s=24,
                    linewidth=1.0,
                    label=f"{proto} outliers (no retrans)",
                )
            if np.any(has_retrans_mask):
                ax.scatter(
                    outlier_values[has_retrans_mask],
                    outlier_y[has_retrans_mask],
                    facecolors=color,
                    edgecolors=color,
                    marker="s",
                    s=24,
                    linewidth=0.8,
                    label=f"{proto} outliers (retrans)",
                )
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
    else:
        ax.set_xlabel("FCT (s)")
        ax.set_ylabel("CDF")
        title = "Mouse FCT CDF"
        if exclude_outliers:
            title += " (outliers removed, 1.5x IQR)"
        ax.set_title(title)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), borderaxespad=0.0)
    fig.tight_layout(rect=[0, 0, 0.8, 1])
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_mouse_fct_histogram(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    exclude_outliers: bool = False,
    mark_outliers: bool = False,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    required_cols = {"proto", "fct_s"}
    if mouse_df.empty or not required_cols.issubset(mouse_df.columns):
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
        fig.tight_layout()
        fig.savefig(output_path, dpi=200)
        plt.close(fig)
        return

    values_by_proto: Dict[str, np.ndarray] = {}
    outliers_by_proto: Dict[str, np.ndarray] = {}
    outlier_retrans_by_proto: Dict[str, np.ndarray] = {}
    for proto in protos:
        subset = mouse_df[mouse_df["proto"] == proto]
        if subset.empty:
            logging.warning("No mouse FCT data for %s", proto)
            continue
        values_all = subset["fct_s"].to_numpy()
        if values_all.size == 0:
            continue
        if "had_retrans" in subset.columns:
            had_retrans_all = subset["had_retrans"].to_numpy().astype(bool)
        else:
            had_retrans_all = np.zeros(values_all.shape, dtype=bool)
        outlier_mask, _, _ = _outlier_mask(values_all)
        outliers_by_proto[proto] = values_all[outlier_mask]
        outlier_retrans_by_proto[proto] = had_retrans_all[outlier_mask]
        values = values_all[~outlier_mask] if exclude_outliers else values_all
        if values.size == 0:
            logging.warning("All mouse FCT values are outliers for %s", proto)
            continue
        values_by_proto[proto] = values

    all_values_ms = (
        np.concatenate([vals * 1000 for vals in values_by_proto.values()])
        if values_by_proto
        else np.array([])
    )
    bins = (
        np.histogram_bin_edges(all_values_ms, bins="auto") if all_values_ms.size else None
    )

    has_data = False
    percentile_annos: List[Tuple[str, Tuple[float, float, float], Any]] = []
    outlier_annos: List[Tuple[np.ndarray, Any, str, bool]] = []
    max_count = 0.0

    for proto in protos:
        values = values_by_proto.get(proto)
        if values is None:
            continue
        values_ms = values * 1000
        if values_ms.size == 0:
            continue
        counts, _, patches = ax.hist(
            values_ms,
            bins=bins if bins is not None and bins.size > 1 else "auto",
            density=True,
            alpha=0.65,
            label=proto,
            edgecolor="black",
            linewidth=0.5,
        )
        max_count = max(max_count, float(np.max(counts)) if counts.size else 0.0)
        p50, p90, p99 = np.percentile(values_ms, [50, 90, 99])
        color = patches[0].get_facecolor() if patches else "C0"
        label = f"{proto} (p50={p50:.3f}, p90={p90:.3f}, p99={p99:.3f})"
        if patches:
            patches[0].set_label(label)
            for patch in patches[1:]:
                patch.set_label("_nolegend_")
        percentile_annos.append((proto, (p50, p90, p99), color))
        if mark_outliers and not exclude_outliers:
            outliers = outliers_by_proto.get(proto)
            outlier_retrans = outlier_retrans_by_proto.get(proto)
            if outliers is not None and outliers.size and outlier_retrans is not None:
                no_retrans = outliers[~outlier_retrans]
                has_retrans = outliers[outlier_retrans]
                if no_retrans.size:
                    outlier_annos.append((no_retrans * 1000, color, proto, False))
                if has_retrans.size:
                    outlier_annos.append((has_retrans * 1000, color, proto, True))
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
    else:
        marker_y = max_count * 1.05 if max_count > 0 else 1.0
        for proto, values, color in percentile_annos:
            p50, p90, p99 = values
            ax.vlines(
                [p50, p90, p99],
                ymin=0,
                ymax=marker_y,
                colors=color,
                linestyles="--",
                linewidth=1,
                alpha=0.8,
            )
            ax.scatter(
                [p50, p90, p99],
                [marker_y] * 3,
                color=color,
                marker="x",
                s=25,
                label=f"{proto} p50/p90/p99",
            )
        if mark_outliers and not exclude_outliers:
            for values_ms, color, proto, had_retrans in outlier_annos:
                if had_retrans:
                    ax.scatter(
                        values_ms,
                        [marker_y] * len(values_ms),
                        facecolors=color,
                        edgecolors=color,
                        marker="s",
                        s=24,
                        linewidth=0.8,
                        label=f"{proto} outliers (retrans)",
                    )
                else:
                    ax.scatter(
                        values_ms,
                        [marker_y] * len(values_ms),
                        facecolors="none",
                        edgecolors=color,
                        marker="o",
                        s=24,
                        linewidth=1.0,
                        label=f"{proto} outliers (no retrans)",
                    )
        ax.set_ylim(top=marker_y * 1.1)
        ax.set_xlabel("FCT (ms)")
        ax.set_ylabel("Density")
        title = "Mouse FCT Distribution"
        if exclude_outliers:
            title += " (outliers removed, 1.5x IQR)"
        ax.set_title(title)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


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

    output_dir: Path = OUTPUT_ROOT / args.output_dir_name
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
    plot_elephant_goodput_bar(
        elephant_df,
        output_dir / "normal_elephant_goodput_bar.png",
        PROTO_ORDER,
    )
    plot_elephant_goodput_scatter(
        elephant_df,
        output_dir / "normal_elephant_goodput_scatter.png",
        PROTO_ORDER,
    )
    plot_mouse_fct_cdf(
        mouse_df,
        output_dir / "normal_mouse_fct_cdf.png",
        PROTO_ORDER,
        mark_outliers=True,
    )
    plot_mouse_fct_cdf(
        mouse_df,
        output_dir / "normal_mouse_fct_cdf_no_outliers.png",
        PROTO_ORDER,
        exclude_outliers=True,
    )
    plot_run_p99_scatter(
        mouse_df,
        output_dir / "normal_mouse_p99_scatter.png",
        PROTO_ORDER,
    )
    plot_mouse_fct_histogram(
        mouse_df,
        output_dir / "normal_mouse_fct_hist.png",
        PROTO_ORDER,
        mark_outliers=True,
    )
    plot_mouse_fct_histogram(
        mouse_df,
        output_dir / "normal_mouse_fct_hist_no_outliers.png",
        PROTO_ORDER,
        exclude_outliers=True,
    )
    if args.heatmap_mode == "pivot":
        plot_link_heatmap(
            link_df,
            output_dir / "normal_link_heatmap.png",
            PROTO_ORDER,
        )
    else:
        plot_fattree_heatmap(
            link_df,
            output_dir / "normal_link_heatmap.png",
            PROTO_ORDER,
            k=args.k,
        )

    write_summary(
        output_dir / "normal_summary.txt",
        elephant_df,
        mouse_df,
        fairness_df,
        PROTO_ORDER,
        experiment_label="Normal",
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
