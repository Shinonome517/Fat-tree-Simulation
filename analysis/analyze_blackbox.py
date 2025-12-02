"""
Analyze blackbox experiment outputs (QUIC vs MPQUIC).

This script scans the blackbox log directory, aggregates elephant goodput,
mouse flow completion time (FCT), and switch tx byte deltas, then produces
comparison plots and a text summary.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

import matplotlib
import numpy as np
import pandas as pd

# Use non-interactive backend to work in headless environments.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


DEFAULT_LOG_ROOT = Path("./logs/blackbox")
DEFAULT_OUTPUT_DIR = Path("./analysis/plots")
PROTO_ORDER = ("quic", "mpquic")
HEATMAP_MAX_IFACES = 20  # Limit for readability; trim if there are many ifaces.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze blackbox experiment logs.")
    parser.add_argument(
        "--log-root",
        type=Path,
        default=DEFAULT_LOG_ROOT,
        help="Root directory containing per-proto run_* subdirectories.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write plots and summary text.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        help=(
            "Run ID(s) to include (e.g., run_20251202-074952). "
            "Can be specified multiple times. "
            "Default: use the latest run_* per protocol."
        ),
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
    return sorted(
        [d for d in proto_dir.iterdir() if d.is_dir() and d.name.startswith("run_")]
    )


def select_run_dirs(
    log_root: Path, protos: Sequence[str], run_ids: Sequence[str] | None
) -> Dict[str, List[Path]]:
    run_dirs_by_proto: Dict[str, List[Path]] = {}
    for proto in protos:
        available = list_run_dirs(log_root, proto)
        if not available:
            logging.warning("No run_* directories found for %s under %s", proto, log_root)
            run_dirs_by_proto[proto] = []
            continue

        if not run_ids:
            selected = available[-1:]
        else:
            available_by_name = {d.name: d for d in available}
            selected = []
            for rid in run_ids:
                path = available_by_name.get(rid)
                if path is not None:
                    selected.append(path)
                else:
                    logging.warning("Requested run_id %s not found under %s", rid, proto)
        run_dirs_by_proto[proto] = selected
    return run_dirs_by_proto


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


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


def load_mouse_fcts(csv_path: Path) -> List[float]:
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
    durations = durations[(durations.notna()) & (durations > 0)]
    return durations.tolist()


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
                for fct in load_mouse_fcts(csv_path):
                    mouse_rows.append(
                        {
                            "proto": proto,
                            "run_id": run_id,
                            "pair_id": pair_id,
                            "fct_s": float(fct),
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

    elephant_df = pd.DataFrame(elephant_rows)
    mouse_df = pd.DataFrame(mouse_rows)
    link_df = pd.DataFrame(link_rows)
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


def plot_elephant_goodput_cdf(
    elephant_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    has_data = False
    for proto in protos:
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            logging.warning("No elephant goodput data for %s", proto)
            continue
        values = subset["goodput_mbps"].to_numpy()
        _plot_cdf(ax, values, proto)
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No elephant data", ha="center", va="center")
    else:
        ax.set_xlabel("Elephant goodput (Mbps)")
        ax.set_ylabel("CDF")
        ax.set_title("Elephant Goodput CDF")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_mouse_fct_cdf(
    mouse_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    has_data = False
    for proto in protos:
        subset = mouse_df[mouse_df["proto"] == proto]
        if subset.empty:
            logging.warning("No mouse FCT data for %s", proto)
            continue
        values = subset["fct_s"].to_numpy()
        line = _plot_cdf(ax, values, proto)
        color = line.get_color()
        p50 = np.percentile(values, 50)
        p90 = np.percentile(values, 90)
        p99 = np.percentile(values, 99)
        ax.scatter(
            [p50, p90, p99],
            [0.5, 0.9, 0.99],
            color=color,
            marker="x",
            s=25,
            label=f"{proto} p50/p90/p99",
        )
        line.set_label(f"{proto} (p50={p50:.3f}, p90={p90:.3f}, p99={p99:.3f})")
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
    else:
        ax.set_xlabel("FCT (s)")
        ax.set_ylabel("CDF")
        ax.set_title("Mouse FCT CDF")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_link_heatmap(
    link_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    if link_df.empty:
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.text(0.5, 0.5, "No link data", ha="center", va="center")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(output_path, dpi=200)
        plt.close(fig)
        return

    pivot = (
        link_df.groupby(["if_name", "proto"])["delta_tx_bytes"]
        .mean()
        .unstack(fill_value=0.0)
    )
    pivot = pivot[[p for p in protos if p in pivot.columns]]

    if len(pivot) > HEATMAP_MAX_IFACES:
        total = pivot.sum(axis=1).sort_values(ascending=False)
        keep = total.head(HEATMAP_MAX_IFACES).index
        pivot = pivot.loc[keep]

    fig, ax = plt.subplots(
        figsize=(6, max(3.0, 0.35 * len(pivot.index)))
    )
    im = ax.imshow(pivot.values, aspect="auto", cmap="viridis")
    ax.set_xticks(np.arange(pivot.shape[1]))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(np.arange(pivot.shape[0]))
    ax.set_yticklabels(pivot.index)
    ax.set_title("Switch delta TX bytes (avg)")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Bytes")
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

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    run_dirs_by_proto = select_run_dirs(args.log_root, PROTO_ORDER, args.run_id)
    total_runs = sum(len(v) for v in run_dirs_by_proto.values())
    if total_runs == 0:
        logging.error(
            "No run directories selected under %s for protocols: %s",
            args.log_root,
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

    data = collect_all_data(args.log_root, PROTO_ORDER, run_dirs_by_proto)
    elephant_df = data["elephant"]
    mouse_df = data["mouse"]
    link_df = data["link"]

    if elephant_df.empty and mouse_df.empty and link_df.empty:
        logging.error("No data found under %s", args.log_root)
        return

    fairness_df = compute_fairness(link_df)

    plot_elephant_goodput_cdf(
        elephant_df,
        output_dir / "blackbox_elephant_goodput_cdf.png",
        PROTO_ORDER,
    )
    plot_mouse_fct_cdf(
        mouse_df,
        output_dir / "blackbox_mouse_fct_cdf.png",
        PROTO_ORDER,
    )
    plot_link_heatmap(
        link_df,
        output_dir / "blackbox_link_heatmap.png",
        PROTO_ORDER,
    )

    write_summary(
        output_dir / "blackbox_summary.txt",
        elephant_df,
        mouse_df,
        fairness_df,
        PROTO_ORDER,
    )


if __name__ == "__main__":
    main()
