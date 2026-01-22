"""
Analyze normal experiment outputs (QUIC/MPQUIC/TCP/MPTCP).

This script scans the normal log directory, aggregates elephant goodput,
mouse flow completion time (FCT), and switch tx byte deltas, then produces
comparison plots and a text summary. It expects logs under
logs/normal/<log-dir>/<proto>/<elephant-num>/<elephant-MBytes>/run_* and
writes per-(elephant, elephant-MBytes) results to analysis/plots/normal.
"""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import matplotlib
from matplotlib import colors as mcolors
import numpy as np
import pandas as pd

# Use non-interactive backend to work in headless environments.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from fattree_heatmap import plot_fattree_heatmap, plot_fattree_topology
from plots_goodput import plot_goodput_violin_proto
from plots_link import plot_link_heatmap
from plots_link_util_summary import plot_run_metric_bar, plot_run_metric_scatter
from scatter import plot_run_p99_scatter
from normal_loader import collect_all_data, select_run_dirs
from normal_metrics import (
    compute_fairness,
    compute_link_util_series,
    compute_u_max_percentiles,
    write_summary,
)


LOG_ROOT_BASE = Path("./logs/normal")
DEFAULT_LOG_DIR_NAME = Path("default")
OUTPUT_ROOT = Path("./analysis/plots/normal")
PROTO_ORDER = ("mpquic", "quic", "mptcp", "tcp")
PROTO_COLORS = {
    "tcp": "#7f7f7f",
    "mptcp": "#2ca02c",
    "quic": "#17becf",
    "mpquic": "#ff7f0e",
}
ELEPHANT_GOODPUT_YLIM = (0, 1000)
MOUSE_DROPLOSS_FILENAME = "normal_mouse_droploss_ratio.png"
MOUSE_RETRANS_FILENAME = "normal_mouse_retrans_ratio.png"
LINK_UTIL_SUBDIR = Path("link_utilization")
LINK_UTIL_RUN_SUMMARY_SUBDIR = LINK_UTIL_SUBDIR / "run_summary"
DEFAULT_ELEPHANT_NUM = 4
MOUSE_EXCLUDE_HEAD_S = 1.0
MOUSE_EXCLUDE_TAIL_S = 1.0


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid float: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive number.")
    return parsed


def _collect_combos(*dfs: pd.DataFrame) -> set[tuple[int, str]]:
    combos: set[tuple[int, str]] = set()
    for df in dfs:
        if df is None or df.empty:
            continue
        if "elephant_num" not in df.columns or "elephant_MBytes" not in df.columns:
            continue
        combos.update(zip(df["elephant_num"], df["elephant_MBytes"]))
    return combos


def _split_pair_id(pair_id: Any) -> tuple[str | None, int | None]:
    if not isinstance(pair_id, str):
        return None, None
    parts = pair_id.split("_", 1)
    pair_label = parts[0] if parts else None
    seq: int | None = None
    if len(parts) == 2:
        try:
            seq = int(parts[1])
        except ValueError:
            seq = None
    return pair_label, seq


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
        "--elephant-num",
        action="append",
        type=_positive_int,
        help=(
            "Elephant flow count directory to include (can be provided multiple times). "
            "Default: 4 (normal.py default)."
        ),
    )
    parser.add_argument(
        "--elephant-MBytes",
        dest="elephant_MBytes",
        action="append",
        type=int,
        help=(
            "Elephant payload size directory name(s) in integer MB (can be provided multiple times). "
            "Default: include all sizes."
        ),
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
    parser.add_argument(
        "--mouse-lambda",
        type=_positive_float,
        required=True,
        help="Mouse flow generation rate for the experiment (Poisson mean, flows/s).",
    )
    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _plot_cdf(ax, values: np.ndarray, label: str, color: str | None = None):
    values = np.sort(values)
    y = np.arange(1, len(values) + 1) / len(values)
    (line,) = ax.step(values, y, where="post", label=label, color=color)
    return line


def _proto_color(proto: str) -> str:
    return PROTO_COLORS.get(str(proto).lower(), "tab:blue")


def _darken_color(color: str, factor: float = 0.8) -> tuple[float, float, float, float]:
    rgba = mcolors.to_rgba(color)
    return (rgba[0] * factor, rgba[1] * factor, rgba[2] * factor, rgba[3])


def _outlier_mask(values: np.ndarray) -> Tuple[np.ndarray, float, float]:
    if values.size == 0:
        return np.zeros(0, dtype=bool), float("nan"), float("nan")
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    mask = (values < lower) | (values > upper)
    return mask, lower, upper


def plot_mouse_fct_cdf(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    exclude_outliers: bool = False,
    mark_outliers: bool = False,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    unit_label = "ms" if exclude_outliers else "s"
    scale = 1000.0 if exclude_outliers else 1.0
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
        if exclude_outliers:
            p99_threshold = np.percentile(values_all, 99)
            values = values_all[values_all <= p99_threshold]
        else:
            outlier_mask, lower, upper = _outlier_mask(values_all)
            values = values_all
        if values.size == 0:
            logging.warning("No mouse FCT values <= p99 for %s", proto)
            continue
        label = proto if not exclude_outliers else f"{proto} (<=p99)"
        line_color = _proto_color(proto)
        line = _plot_cdf(ax, values * scale, label, color=line_color)
        color = line.get_color()
        if not exclude_outliers:
            p50, p90, p99 = np.percentile(values * scale, [50, 90, 99])
            ax.scatter(
                [p50, p90, p99],
                [0.5, 0.9, 0.99],
                facecolors=color,
                edgecolors="black",
                marker="X",
                s=40,
                linewidths=1.0,
                label=f"{proto} p50/p90/p99",
            )
            line.set_label(f"{proto} (p50={p50:.3f}, p90={p90:.3f}, p99={p99:.3f})")
            if mark_outliers:
                sort_idx = np.argsort(values_all)
                values_sorted = values_all[sort_idx]
                retrans_sorted = had_retrans_all[sort_idx]
                y = np.arange(1, len(values_sorted) + 1) / len(values_sorted)
                sorted_mask = (values_sorted < lower) | (values_sorted > upper)
                outlier_y = y[sorted_mask]
                outlier_values = values_sorted[sorted_mask] * scale
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
                    retrans_color = _darken_color(color, factor=0.75)
                    ax.scatter(
                        outlier_values[has_retrans_mask],
                        outlier_y[has_retrans_mask],
                        facecolors=retrans_color,
                        edgecolors=retrans_color,
                        marker="s",
                        s=24,
                        linewidth=0.8,
                        label=f"{proto} outliers (retrans)",
                    )
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
    else:
        ax.set_xlabel(f"FCT ({unit_label})")
        ax.set_ylabel("CDF")
        title = "Mouse FCT CDF"
        if exclude_outliers:
            title += " (outliers removed, <=p99)"
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


def _exclude_mouse_warmup_tail(
    mouse_df: pd.DataFrame,
    mouse_lambda_total: float,
    *,
    warmup_s: float,
    tail_s: float,
) -> pd.DataFrame:
    """
    Remove flows that likely fall into the warmup and tail windows by using the
    Poisson arrival expectation and per-pair sequence numbers.
    """
    if mouse_df.empty:
        return mouse_df
    if "pair_id" not in mouse_df.columns:
        logging.warning("pair_id column missing in mouse_df; skipping warmup/tail exclusion.")
        return mouse_df

    pair_labels, pair_seqs = zip(*mouse_df["pair_id"].map(_split_pair_id))
    df = mouse_df.copy()
    df["_pair_label"] = pair_labels
    df["_flow_seq"] = pd.to_numeric(pair_seqs, errors="coerce")
    mask = pd.Series(True, index=df.index)

    for (proto, run_id, elephant_num, elephant_MBytes), group in df.groupby(
        ["proto", "run_id", "elephant_num", "elephant_MBytes"],
        sort=False,
    ):
        labels = [p for p in group["_pair_label"].unique() if isinstance(p, str)]
        pair_count = len(labels)
        if pair_count == 0:
            logging.warning(
                "Unable to infer mouse pairs for %s run %s (E=%s, M=%s); keeping all flows.",
                proto,
                run_id,
                elephant_num,
                elephant_MBytes,
            )
            continue

        lambda_per_pair = mouse_lambda_total / pair_count
        head_drop_target = math.ceil(lambda_per_pair * warmup_s) if warmup_s > 0 else 0
        tail_drop_target = math.ceil(lambda_per_pair * tail_s) if tail_s > 0 else 0
        dropped = 0

        for pair_label in labels:
            pair_group = group[group["_pair_label"] == pair_label]
            if pair_group.empty:
                continue
            if pair_group["_flow_seq"].notna().any():
                pair_group = pair_group.sort_values("_flow_seq", na_position="last")
            else:
                logging.warning(
                    "Missing sequence numbers for pair %s in %s run %s (E=%s, M=%s); skipping warmup/tail exclusion for this pair.",
                    pair_label,
                    proto,
                    run_id,
                    elephant_num,
                    elephant_MBytes,
                )
                continue

            head_drop = min(head_drop_target, len(pair_group))
            tail_drop = min(tail_drop_target, max(len(pair_group) - head_drop, 0))
            if head_drop:
                mask.loc[pair_group.index[:head_drop]] = False
            if tail_drop:
                mask.loc[pair_group.index[-tail_drop:]] = False
            dropped += head_drop + tail_drop

        if dropped:
            logging.info(
                "Excluded %d mouse flows for %s run %s (E=%s, M=%s): lambda=%.3f flows/s, pairs=%d, head=%d, tail=%d.",
                dropped,
                proto,
                run_id,
                elephant_num,
                elephant_MBytes,
                mouse_lambda_total,
                pair_count,
                head_drop_target,
                tail_drop_target,
            )

    filtered = df[mask].drop(columns=["_pair_label", "_flow_seq"])
    return filtered


def _exclude_link_warmup_tail(
    util_df: pd.DataFrame,
    *,
    warmup_s: float,
    tail_s: float,
) -> pd.DataFrame:
    if util_df.empty:
        return util_df
    required = {"proto", "run_id", "elapsed_s"}
    missing = [col for col in required if col not in util_df.columns]
    if missing:
        logging.warning(
            "Link utilization missing columns %s; skipping warmup/tail exclusion.",
            ", ".join(missing),
        )
        return util_df

    df = util_df.copy()
    df["elapsed_s"] = pd.to_numeric(df["elapsed_s"], errors="coerce")
    mask = pd.Series(True, index=df.index)

    for (proto, run_id), group in df.groupby(["proto", "run_id"], sort=False):
        max_elapsed = group["elapsed_s"].max()
        if pd.isna(max_elapsed):
            logging.warning(
                "elapsed_s missing for %s run %s; keeping all link-util samples.",
                proto,
                run_id,
            )
            continue

        min_keep = warmup_s if warmup_s > 0 else float("-inf")
        max_keep = max_elapsed - tail_s if tail_s > 0 else max_elapsed
        keep = (group["elapsed_s"] >= min_keep) & (group["elapsed_s"] <= max_keep)
        dropped = int((~keep).sum())
        if dropped:
            logging.info(
                "Excluded %d link-util samples for %s run %s: warmup=%.3f s, tail=%.3f s, max_elapsed=%.3f s.",
                dropped,
                proto,
                run_id,
                warmup_s,
                tail_s,
                max_elapsed,
            )
        if keep.sum() == 0:
            logging.warning(
                "All link-util samples excluded for %s run %s (warmup=%.3f s, tail=%.3f s, max_elapsed=%.3f s).",
                proto,
                run_id,
                warmup_s,
                tail_s,
                max_elapsed,
            )
        mask.loc[group.index] = keep

    return df[mask]


def _count_valid_true(subset: pd.DataFrame, column: str) -> tuple[int, int]:
    if column not in subset.columns:
        return 0, 0
    series = subset[column].dropna()
    if series.empty:
        return 0, 0
    return int(series.astype(bool).sum()), len(series)


def _summarize_link_util_runs(util_df: pd.DataFrame) -> pd.DataFrame:
    if util_df.empty:
        return pd.DataFrame(
            columns=["proto", "run_id", "std_time_avg", "u_mean_time_avg", "u_max_time_avg"]
        )
    required = {"proto", "run_id", "u_mean", "u_max", "std"}
    missing = [col for col in required if col not in util_df.columns]
    if missing:
        logging.warning("Link utilization summary missing columns: %s", ", ".join(missing))
        return pd.DataFrame(
            columns=["proto", "run_id", "std_time_avg", "u_mean_time_avg", "u_max_time_avg"]
        )

    df = util_df.copy()
    summary = (
        df.groupby(["proto", "run_id"], sort=False)
        .agg(
            std_time_avg=("std", "mean"),
            u_mean_time_avg=("u_mean", "mean"),
            u_max_time_avg=("u_max", "mean"),
        )
        .reset_index()
    )
    return summary


def _plot_link_util_run_summaries(
    run_summary_df: pd.DataFrame,
    output_root: Path,
    protos: Sequence[str],
) -> None:
    if run_summary_df.empty:
        logging.warning("No link utilization run summaries to plot.")
        return

    summary_root = output_root / LINK_UTIL_RUN_SUMMARY_SUBDIR
    summary_root.mkdir(parents=True, exist_ok=True)
    summary_specs = [
        (
            "std_time_avg",
            "Std dev of utilization (time avg, %)",
            "std",
            "Link utilization std dev (per-run time avg)",
            None,
        ),
        (
            "u_mean_time_avg",
            "Mean utilization (time avg, %)",
            "u_mean",
            "Link utilization mean (per-run time avg)",
            (0.0, 100.0),
        ),
        (
            "u_max_time_avg",
            "Max utilization (time avg, %)",
            "u_max",
            "Link utilization max (per-run time avg)",
            (0.0, 100.0),
        ),
    ]
    for metric, ylabel, prefix, title, y_lim in summary_specs:
        metric_df = run_summary_df[run_summary_df[metric].notna()]
        plot_run_metric_bar(
            metric_df,
            summary_root / f"{prefix}_bar.png",
            protos,
            metric,
            ylabel,
            title=title,
            y_lim=y_lim,
            proto_colors=PROTO_COLORS,
        )
        plot_run_metric_scatter(
            metric_df,
            summary_root / f"{prefix}_scatter.png",
            protos,
            metric,
            ylabel,
            title=title,
            y_lim=y_lim,
            proto_colors=PROTO_COLORS,
        )


def _plot_link_series(
    x: np.ndarray,
    y: np.ndarray,
    title: str,
    ylabel: str,
    output_path: Path,
    *,
    p95: float | None = None,
    p99: float | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 3))
    (line,) = ax.plot(x, y, label=title, linewidth=1.4)
    color = line.get_color()
    markers: List[Tuple[float, float, str]] = []
    if p95 is not None:
        ax.axhline(p95, color=color, linestyle="--", linewidth=1.0, alpha=0.65, label=f"P95={p95:.3f}")
        markers.append((x[-1] if len(x) else 0.0, p95, "P95"))
    if p99 is not None:
        ax.axhline(p99, color=color, linestyle="--", linewidth=1.0, alpha=0.65, label=f"P99={p99:.3f}")
        markers.append((x[-1] if len(x) else 0.0, p99, "P99"))
    if markers:
        for xpos, ypos, label in markers:
            ax.scatter(
                xpos,
                ypos,
                color=color,
                marker="x",
                s=60,
                linewidth=1.2,
                label=label,
            )
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_link_util_timeseries(
    util_df: pd.DataFrame,
    percentiles: Dict[Tuple[str, str], Tuple[float, float]],
    output_root: Path,
) -> None:
    if util_df.empty:
        logging.warning("No link utilization timeseries data to plot.")
        return

    plot_root = output_root / LINK_UTIL_SUBDIR
    for (proto, run_id), group in util_df.groupby(["proto", "run_id"]):
        subdir = plot_root / proto / run_id
        subdir.mkdir(parents=True, exist_ok=True)
        sorted_group = group.sort_values("elapsed_s")
        x = sorted_group["elapsed_s"].to_numpy()

        _plot_link_series(
            x,
            sorted_group["u_mean"].to_numpy(),
            title=f"{proto} {run_id} u_mean",
            ylabel="Mean utilization (%)",
            output_path=subdir / "u_mean.png",
        )

        p95 = p99 = None
        if (proto, run_id) in percentiles:
            p95, p99 = percentiles[(proto, run_id)]
        _plot_link_series(
            x,
            sorted_group["u_max"].to_numpy(),
            title=f"{proto} {run_id} u_max",
            ylabel="Max utilization (%)",
            output_path=subdir / "u_max.png",
            p95=p95,
            p99=p99,
        )

        _plot_link_series(
            x,
            sorted_group["std"].to_numpy(),
            title=f"{proto} {run_id} std_u",
            ylabel="Std dev of utilization (%)",
            output_path=subdir / "std.png",
        )


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    elephant_filter = args.elephant_num or [DEFAULT_ELEPHANT_NUM]
    mbytes_filter = args.elephant_MBytes

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
            elephant_filter,
            mbytes_filter,
        )
    except ValueError as exc:
        logging.error("%s", exc)
        raise SystemExit(1) from exc

    if all(not combos for combos in run_dirs_by_proto.values()):
        logging.error(
            "No run directories selected under %s for protocols: %s (elephant=%s, MBytes=%s)",
            log_root,
            ", ".join(PROTO_ORDER),
            ", ".join(map(str, elephant_filter)),
            ", ".join(map(str, mbytes_filter)) if mbytes_filter else "all",
        )
        return

    for proto in PROTO_ORDER:
        combos = run_dirs_by_proto.get(proto, {})
        if not combos:
            logging.warning(
                "No runs selected for %s under %s (elephant=%s, MBytes=%s)",
                proto,
                log_root,
                ", ".join(map(str, elephant_filter)),
                ", ".join(map(str, mbytes_filter)) if mbytes_filter else "all",
            )
            continue
        for (elephant_num, elephant_MBytes), runs in combos.items():
            if runs:
                logging.info(
                    "Using %d run(s) for %s (E=%s, M=%s MB): %s",
                    len(runs),
                    proto,
                    elephant_num,
                    elephant_MBytes,
                    ", ".join(d.name for d in runs),
                )
            else:
                logging.warning(
                    "No runs selected for %s (E=%s, M=%s)",
                    proto,
                    elephant_num,
                    elephant_MBytes,
                )

    data = collect_all_data(log_root, PROTO_ORDER, run_dirs_by_proto)
    elephant_df = data["elephant"]
    mouse_df = data["mouse"]
    mouse_df = _exclude_mouse_warmup_tail(
        mouse_df,
        args.mouse_lambda,
        warmup_s=MOUSE_EXCLUDE_HEAD_S,
        tail_s=MOUSE_EXCLUDE_TAIL_S,
    )
    link_df = data["link"]
    link_ts_df = data.get("link_ts", pd.DataFrame())

    combos = _collect_combos(elephant_df, mouse_df, link_df, link_ts_df)
    if not combos:
        logging.error(
            "No data found under %s for filters elephant=%s, MBytes=%s",
            log_root,
            ", ".join(map(str, elephant_filter)),
            ", ".join(map(str, mbytes_filter)) if mbytes_filter else "all",
        )
        return

    try:
        import analysis.mouse_droploss_plot as droploss
        import analysis.mouse_retrans_plot as retrans
    except ImportError:  # pragma: no cover - support script execution
        import mouse_droploss_plot as droploss
        import mouse_retrans_plot as retrans

    for elephant_num, elephant_MBytes in sorted(combos, key=lambda x: (x[0], x[1])):
        combo_label = f"elephant={elephant_num}, MBytes={elephant_MBytes}"
        combo_output_dir = output_dir / str(elephant_num) / str(elephant_MBytes)
        combo_output_dir.mkdir(parents=True, exist_ok=True)
        logging.info("Analyzing %s -> %s", combo_label, combo_output_dir)

        elephant_combo = elephant_df[
            (elephant_df["elephant_num"] == elephant_num)
            & (elephant_df["elephant_MBytes"] == elephant_MBytes)
        ]
        mouse_combo = mouse_df[
            (mouse_df["elephant_num"] == elephant_num)
            & (mouse_df["elephant_MBytes"] == elephant_MBytes)
        ]
        link_combo = link_df[
            (link_df["elephant_num"] == elephant_num)
            & (link_df["elephant_MBytes"] == elephant_MBytes)
        ]
        link_ts_combo = link_ts_df[
            (link_ts_df["elephant_num"] == elephant_num)
            & (link_ts_df["elephant_MBytes"] == elephant_MBytes)
        ]

        if (
            elephant_combo.empty
            and mouse_combo.empty
            and link_combo.empty
            and link_ts_combo.empty
        ):
            logging.warning("No data available for %s; skipping.", combo_label)
            continue

        fairness_df = compute_fairness(link_combo)
        util_df = compute_link_util_series(link_ts_combo)
        util_df = _exclude_link_warmup_tail(
            util_df,
            warmup_s=MOUSE_EXCLUDE_HEAD_S,
            tail_s=MOUSE_EXCLUDE_TAIL_S,
        )
        u_max_percentiles = compute_u_max_percentiles(util_df)
        run_summary_df = _summarize_link_util_runs(util_df)

        plot_fattree_topology(
            combo_output_dir / f"fattree_topology_k{args.k}.png",
            k=args.k,
        )
        plot_goodput_violin_proto(
            elephant_combo,
            combo_output_dir / "normal_elephant_goodput_violin.png",
            PROTO_ORDER,
            proto_colors=PROTO_COLORS,
            y_lim=ELEPHANT_GOODPUT_YLIM,
        )
        plot_mouse_fct_cdf(
            mouse_combo,
            combo_output_dir / "normal_mouse_fct_cdf.png",
            PROTO_ORDER,
            mark_outliers=True,
        )
        plot_mouse_fct_cdf(
            mouse_combo,
            combo_output_dir / "normal_mouse_fct_cdf_no_outliers.png",
            PROTO_ORDER,
            exclude_outliers=True,
        )
        plot_run_p99_scatter(
            mouse_combo,
            combo_output_dir / "normal_mouse_p99_scatter.png",
            PROTO_ORDER,
        )
        if args.heatmap_mode == "pivot":
            plot_link_heatmap(
                link_combo,
                combo_output_dir / "normal_link_heatmap.png",
                PROTO_ORDER,
            )
        else:
            plot_fattree_heatmap(
                link_combo,
                combo_output_dir / "normal_link_heatmap.png",
                PROTO_ORDER,
                k=args.k,
            )
        plot_link_util_timeseries(
            util_df,
            u_max_percentiles,
            output_root=combo_output_dir,
        )
        _plot_link_util_run_summaries(
            run_summary_df,
            combo_output_dir,
            PROTO_ORDER,
        )

        write_summary(
            combo_output_dir / "normal_summary.txt",
            elephant_combo,
            mouse_combo,
            fairness_df,
            PROTO_ORDER,
            experiment_label=f"Normal (E={elephant_num}, M={elephant_MBytes} MB)",
        )

        droploss_summaries: List[droploss.DropLossSummary] = []
        retrans_summaries: List[retrans.RetransSummary] = []
        for proto in PROTO_ORDER:
            runs = run_dirs_by_proto.get(proto, {}).get(
                (elephant_num, elephant_MBytes), []
            )
            if not runs:
                continue
            subset = _subset_mouse(mouse_combo, proto)
            drop_flows, drop_total = _count_valid_true(subset, "had_drop_retrans")
            retrans_flows, retrans_total = _count_valid_true(subset, "had_retrans")
            if drop_total == 0:
                logging.warning(
                    "Drop-loss data nodata for %s (E=%s, M=%s MB): had_drop_retrans all NULL or missing.",
                    proto,
                    elephant_num,
                    elephant_MBytes,
                )
            else:
                droploss_summaries.append(
                    droploss.DropLossSummary(
                        label=proto,
                        run_dir=runs[-1],
                        drop_flows=drop_flows,
                        total_flows=drop_total,
                    )
                )
            if retrans_total == 0:
                logging.warning(
                    "Retrans data nodata for %s (E=%s, M=%s MB): had_retrans all NULL or missing.",
                    proto,
                    elephant_num,
                    elephant_MBytes,
                )
            else:
                retrans_summaries.append(
                    retrans.RetransSummary(
                        label=proto,
                        run_dir=runs[-1],
                        retrans_flows=retrans_flows,
                        total_flows=retrans_total,
                    )
                )

        if droploss_summaries:
            droploss_path = droploss.plot_drop_retrans_ratios(
                droploss_summaries,
                output_dir=combo_output_dir,
                filename=MOUSE_DROPLOSS_FILENAME,
                title="Mouse drop-induced retransmissions",
                color_map=PROTO_COLORS,
            )
            drop_flows = sum(s.drop_flows for s in droploss_summaries)
            total_flows = sum(s.total_flows for s in droploss_summaries)
            logging.info(
                "Wrote drop-loss plot for %s to %s (drop-induced retrans flows: %d/%d).",
                combo_label,
                droploss_path,
                drop_flows,
                total_flows,
            )
        else:
            logging.warning("No runs available for drop-loss plot (%s).", combo_label)

        if retrans_summaries:
            retrans_path = retrans.plot_retrans_ratios(
                retrans_summaries,
                output_dir=combo_output_dir,
                filename=MOUSE_RETRANS_FILENAME,
                title="Mouse retransmission ratio",
                color_map=PROTO_COLORS,
            )
            retrans_flows = sum(s.retrans_flows for s in retrans_summaries)
            total_flows = sum(s.total_flows for s in retrans_summaries)
            logging.info(
                "Wrote retrans plot for %s to %s (flows with retrans: %d/%d).",
                combo_label,
                retrans_path,
                retrans_flows,
                total_flows,
            )
        else:
            logging.warning("No runs available for retrans plot (%s).", combo_label)


if __name__ == "__main__":
    main()
