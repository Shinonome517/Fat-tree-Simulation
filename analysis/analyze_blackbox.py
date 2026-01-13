"""
Analyze blackbox experiment outputs (QUIC vs MPQUIC).

This script scans the blackbox log directory, aggregates elephant goodput,
mouse flow completion time (FCT), and switch tx byte deltas, then produces
comparison plots and a text summary.
"""

from __future__ import annotations

import argparse
import logging
import re
import json
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


LOG_ROOT_BASE = Path("./logs/blackbox")
DEFAULT_LOG_DIR_NAME = Path("default")
OUTPUT_ROOT = Path("./analysis/plots/black")
PROTO_ORDER = ("quic", "mpquic")
MOUSE_DROPLOSS_FILENAME = "blackbox_mouse_droploss_ratio.png"
MOUSE_RETRANS_FILENAME = "blackbox_mouse_retrans_ratio.png"
RUN_ID_PATTERN = re.compile(r"^run_\d{8}-\d{6}(?:_seed\d+)?$")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze blackbox experiment logs.")
    parser.add_argument(
        "--log-dir",
        dest="log_dir_name",
        type=Path,
        default=DEFAULT_LOG_DIR_NAME,
        help="Directory name under logs/blackbox to analyze (default: default).",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir_name",
        type=Path,
        default=Path("default"),
        help="Output subdirectory name under analysis/plots/black (default: default).",
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


def list_run_dirs(log_root: Path, proto: str) -> List[Path]:
    proto_dir = log_root / proto
    if not proto_dir.exists():
        logging.warning("Protocol directory not found: %s", proto_dir)
        return []
    if not proto_dir.is_dir():
        logging.warning("Protocol path is not a directory: %s", proto_dir)
        return []
    run_dirs: List[Path] = []
    for entry in proto_dir.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith("run_"):
            continue
        if not RUN_ID_PATTERN.match(entry.name):
            logging.warning("Ignoring run dir with unexpected name: %s", entry)
            continue
        run_dirs.append(entry)
    return sorted(run_dirs, key=lambda path: path.name)


def select_run_dirs(
    log_root: Path,
    protos: Sequence[str],
    run_ids: Sequence[str] | None,
    latest_n: int | None,
) -> Dict[str, List[Path]]:
    if run_ids and latest_n is not None:
        raise ValueError("Cannot combine --run-id with --latest-n.")
    run_dirs_by_proto: Dict[str, List[Path]] = {}
    for proto in protos:
        available = list_run_dirs(log_root, proto)
        if latest_n is not None and len(available) < latest_n:
            raise ValueError(
                f"Need at least {latest_n} run(s) for {proto} under {log_root}, "
                f"found {len(available)}."
            )
        if not available:
            logging.warning("No run_* directories found for %s under %s", proto, log_root)
            run_dirs_by_proto[proto] = []
            continue

        if run_ids:
            available_by_name = {d.name: d for d in available}
            selected = []
            for rid in run_ids:
                path = available_by_name.get(rid)
                if path is not None:
                    selected.append(path)
                else:
                    logging.warning("Requested run_id %s not found under %s", rid, proto)
        elif latest_n is not None:
            selected = available[-latest_n:]
        else:
            selected = available[-1:]
        run_dirs_by_proto[proto] = selected
    return run_dirs_by_proto


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _find_retrans_column(columns: Sequence[str]) -> str | None:
    for name in ("retrans.", "retrans", "retransmissions", "retransmission"):
        if name in columns:
            return name
    return None


def _find_spurious_column(columns: Sequence[str]) -> str | None:
    for name in ("spurious", "spurious retransmissions"):
        if name in columns:
            return name
    return None


def _pair_id_from_filename(path: Path, prefix: str) -> str:
    """
    Extract a pair identifier from a filename with a known prefix.
    Examples: elephant_00.csv -> "00", mouse_01_0003.csv -> "01_0003".
    """
    stem = path.stem
    if stem.startswith(prefix):
        suffix = stem[len(prefix) :]
        if suffix.startswith("_"):
            suffix = suffix[1:]
        return suffix or stem
    return stem


def load_elephant_goodput(csv_path: Path) -> List[float]:
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        logging.warning("Elephant CSV not found: %s", csv_path)
        return []
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read %s: %s", csv_path, exc)
        return []

    df = _normalize_columns(df)
    if "Duration" not in df or "Sent" not in df:
        logging.warning("Required columns missing in %s", csv_path)
        return []

    durations = pd.to_numeric(df["Duration"], errors="coerce")
    sent = pd.to_numeric(df["Sent"], errors="coerce")
    valid = durations.notna() & sent.notna() & (durations > 0)
    if valid.sum() == 0:
        logging.warning("No valid rows in %s", csv_path)
        return []

    # Goodput based on client transmit direction: Sent bytes over Duration.
    goodput_mbps = (sent[valid] * 8 / durations[valid]) / 1e6
    return goodput_mbps.tolist()


def load_mouse_fcts(csv_path: Path) -> List[Dict[str, object]]:
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        logging.warning("Mouse CSV not found: %s", csv_path)
        return []
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read %s: %s", csv_path, exc)
        return []

    df = _normalize_columns(df)
    if "Duration" not in df:
        logging.warning("Duration column missing in %s", csv_path)
        return []

    durations = pd.to_numeric(df["Duration"], errors="coerce")
    valid = durations.notna() & (durations > 0)
    if valid.sum() == 0:
        return []
    retrans_col = _find_retrans_column(df.columns)
    spurious_col = _find_spurious_column(df.columns)
    if retrans_col is None:
        retrans_vals = pd.Series(0, index=df.index, dtype=float)
    else:
        retrans_vals = pd.to_numeric(df[retrans_col], errors="coerce").fillna(0)
    if spurious_col is None:
        spurious_vals = pd.Series(0, index=df.index, dtype=float)
    else:
        spurious_vals = pd.to_numeric(df[spurious_col], errors="coerce").fillna(0)

    drop_retrans = (retrans_vals - spurious_vals).clip(lower=0)
    rows: List[Dict[str, object]] = []
    for duration, retrans, drop in zip(
        durations[valid], retrans_vals[valid], drop_retrans[valid]
    ):
        rows.append(
            {
                "fct_s": float(duration),
                "had_retrans": bool(float(retrans) > 0),
                "had_drop_retrans": bool(float(drop) > 0),
            }
        )
    return rows


def _extract_tx_bytes(entry: Mapping[str, float]) -> float:
    try:
        return float(entry.get("tx", 0.0))
    except Exception:
        return 0.0


def load_switch_tx_deltas(
    before_path: Path, after_path: Path
) -> Dict[str, float]:
    try:
        with before_path.open() as f:
            before = json.load(f)
        with after_path.open() as f:
            after = json.load(f)
    except FileNotFoundError:
        logging.warning("Switch stats missing: %s or %s", before_path, after_path)
        return {}
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.warning("Failed to read switch stats: %s", exc)
        return {}

    deltas: Dict[str, float] = {}
    for if_name in set(before.keys()) | set(after.keys()):
        delta = _extract_tx_bytes(after.get(if_name, {})) - _extract_tx_bytes(
            before.get(if_name, {})
        )
        deltas[if_name] = float(delta)
    # TODO: Narrow to uplinks only if interface naming allows reliable detection.
    return deltas


def collect_all_data(
    log_root: Path,
    protos: Sequence[str],
    run_dirs_by_proto: Mapping[str, Sequence[Path]] | None = None,
) -> Dict[str, pd.DataFrame]:
    elephant_rows: List[Dict[str, object]] = []
    mouse_rows: List[Dict[str, object]] = []
    link_rows: List[Dict[str, object]] = []

    for proto in protos:
        run_dirs = (
            run_dirs_by_proto.get(proto, [])
            if run_dirs_by_proto is not None
            else list_run_dirs(log_root, proto)
        )
        for run_dir in run_dirs:
            run_id = run_dir.name
            elephant_paths = sorted(run_dir.glob("elephant_*.csv"))
            mouse_paths = sorted(run_dir.glob("mouse_*.csv"))
            before_path = run_dir / "switch_stats_before.json"
            after_path = run_dir / "switch_stats_after.json"

            missing = []
            if not elephant_paths:
                missing.append("elephant_*.csv")
            if not mouse_paths:
                missing.append("mouse_*.csv")
            if not before_path.exists():
                missing.append("switch_stats_before.json")
            if not after_path.exists():
                missing.append("switch_stats_after.json")
            if missing:
                logging.warning(
                    "Skipping %s/%s due to missing files: %s",
                    proto,
                    run_id,
                    ", ".join(missing),
                )
                continue

            for csv_path in elephant_paths:
                pair_id = _pair_id_from_filename(csv_path, "elephant")
                for val in load_elephant_goodput(csv_path):
                    elephant_rows.append(
                        {
                            "proto": proto,
                            "run_id": run_id,
                            "pair_id": pair_id,
                            "goodput_mbps": float(val),
                        }
                    )

            for csv_path in mouse_paths:
                pair_id = _pair_id_from_filename(csv_path, "mouse")
                for row in load_mouse_fcts(csv_path):
                    mouse_rows.append(
                        {
                            "proto": proto,
                            "run_id": run_id,
                            "pair_id": pair_id,
                            **row,
                        }
                    )

            deltas = load_switch_tx_deltas(before_path, after_path)
            for if_name, delta in deltas.items():
                link_rows.append(
                    {
                        "proto": proto,
                        "run_id": run_id,
                        "if_name": if_name,
                        "delta_tx_bytes": float(delta),
                    }
                )

    elephant_df = pd.DataFrame(
        elephant_rows, columns=["proto", "run_id", "pair_id", "goodput_mbps"]
    )
    mouse_df = pd.DataFrame(
        mouse_rows,
        columns=["proto", "run_id", "pair_id", "fct_s", "had_retrans", "had_drop_retrans"],
    )
    link_df = pd.DataFrame(
        link_rows, columns=["proto", "run_id", "if_name", "delta_tx_bytes"]
    )
    return {"elephant": elephant_df, "mouse": mouse_df, "link": link_df}


def jain_index(values: Sequence[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return float("nan")
    numerator = np.square(arr.sum())
    denominator = arr.size * np.square(arr).sum()
    if denominator == 0:
        return float("nan")
    return float(numerator / denominator)


def compute_fairness(link_df: pd.DataFrame) -> pd.DataFrame:
    fairness_rows: List[Dict[str, object]] = []
    if link_df.empty:
        return pd.DataFrame(columns=["proto", "run_id", "jain"])

    for (proto, run_id), group in link_df.groupby(["proto", "run_id"]):
        jain = jain_index(group["delta_tx_bytes"].to_numpy())
        fairness_rows.append({"proto": proto, "run_id": run_id, "jain": jain})
    return pd.DataFrame(fairness_rows)


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
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
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


def write_summary(
    output_path: Path,
    elephant_df: pd.DataFrame,
    mouse_df: pd.DataFrame,
    fairness_df: pd.DataFrame,
    protos: Sequence[str],
) -> None:
    lines: List[str] = []
    lines.append("Blackbox Analysis Summary")
    lines.append("=" * 30)
    lines.append("")

    lines.append("Elephant Goodput (Mbps)")
    for proto in protos:
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            lines.append(f"- {proto}: no data")
            continue
        p50 = np.percentile(subset["goodput_mbps"], 50)
        p90 = np.percentile(subset["goodput_mbps"], 90)
        p99 = np.percentile(subset["goodput_mbps"], 99)
        lines.append(
            f"- {proto}: mean={subset['goodput_mbps'].mean():.3f}, "
            f"std={subset['goodput_mbps'].std(ddof=0):.3f}, "
            f"median={p50:.3f}, p90={p90:.3f}, p99={p99:.3f}, "
            f"samples={len(subset)}"
        )
    lines.append("")

    lines.append("Mouse FCT (s)")
    for proto in protos:
        subset = mouse_df[mouse_df["proto"] == proto]
        if subset.empty:
            lines.append(f"- {proto}: no data")
            continue
        p50 = np.percentile(subset["fct_s"], 50)
        p90 = np.percentile(subset["fct_s"], 90)
        p99 = np.percentile(subset["fct_s"], 99)
        lines.append(
            f"- {proto}: p50={p50:.3f}, p90={p90:.3f}, p99={p99:.3f}, "
            f"samples={len(subset)}"
        )
    lines.append("")

    lines.append("Jain's Fairness Index")
    for proto in protos:
        subset = fairness_df[fairness_df["proto"] == proto]
        if subset.empty:
            lines.append(f"- {proto}: no data")
            continue
        lines.append(
            f"- {proto}: mean={subset['jain'].mean():.3f}, "
            f"std={subset['jain'].std(ddof=0):.3f}, runs={len(subset)}"
        )

    output_path.write_text("\n".join(lines))
    logging.info("Wrote summary to %s", output_path)


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
        logging.error("No data found under %s", args.log_root)
        return

    fairness_df = compute_fairness(link_df)

    plot_fattree_topology(
        output_dir / f"fattree_topology_k{args.k}.png",
        k=args.k,
    )
    plot_elephant_goodput_bar(
        elephant_df,
        output_dir / "blackbox_elephant_goodput_bar.png",
        PROTO_ORDER,
    )
    plot_mouse_fct_cdf(
        mouse_df,
        output_dir / "blackbox_mouse_fct_cdf.png",
        PROTO_ORDER,
        mark_outliers=True,
    )
    plot_mouse_fct_cdf(
        mouse_df,
        output_dir / "blackbox_mouse_fct_cdf_no_outliers.png",
        PROTO_ORDER,
        exclude_outliers=True,
    )
    plot_run_p99_scatter(
        mouse_df,
        output_dir / "blackbox_mouse_p99_scatter.png",
        PROTO_ORDER,
    )
    plot_mouse_fct_histogram(
        mouse_df,
        output_dir / "blackbox_mouse_fct_hist.png",
        PROTO_ORDER,
        mark_outliers=True,
    )
    plot_mouse_fct_histogram(
        mouse_df,
        output_dir / "blackbox_mouse_fct_hist_no_outliers.png",
        PROTO_ORDER,
        exclude_outliers=True,
    )
    if args.heatmap_mode == "pivot":
        plot_link_heatmap(
            link_df,
            output_dir / "blackbox_link_heatmap.png",
            PROTO_ORDER,
        )
    else:
        plot_fattree_heatmap(
            link_df,
            output_dir / "blackbox_link_heatmap.png",
            PROTO_ORDER,
            k=args.k,
        )

    write_summary(
        output_dir / "blackbox_summary.txt",
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

    def _subset_mouse(mouse_df: pd.DataFrame, proto: str) -> pd.DataFrame:
        if "proto" not in mouse_df.columns:
            return mouse_df.iloc[0:0]
        return mouse_df[mouse_df["proto"] == proto]

    def _count_true(subset: pd.DataFrame, column: str) -> int:
        if column not in subset.columns:
            return 0
        return int(subset[column].fillna(False).astype(bool).sum())

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
