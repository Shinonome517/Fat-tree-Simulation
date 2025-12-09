"""
Analyze whitebox experiment outputs (QUIC vs MPQUIC).

This script scans the whitebox log directory, aggregates elephant goodput,
mouse FCT, and switch tx byte deltas, then produces comparison plots and a
text summary.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import matplotlib
import numpy as np
import pandas as pd

# Use non-interactive backend to work in headless environments.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from fattree_heatmap import plot_fattree_heatmap


DEFAULT_LOG_ROOT = Path("./logs/whitebox")
DEFAULT_OUTPUT_DIR = Path("./analysis/plots")
PROTO_ORDER = ("quic", "mpquic")
HEATMAP_MAX_IFACES = 20  # Limit for readability; trim if there are many ifaces.
MOUSE_DROPLOSS_FILENAME = "whitebox_mouse_droploss_ratio.png"
MOUSE_RETRANS_FILENAME = "whitebox_mouse_retrans_ratio.png"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze whitebox experiment logs.")
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
        "--output-subdir",
        type=Path,
        help=(
            "Optional subdirectory name; outputs go to <output-dir>/white/<name>. "
            "Default subdir: default."
        ),
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

    # Goodput is based on client transmit direction: Sent bytes over Duration.
    goodput_mbps = (sent[valid] * 8 / durations[valid]) / 1e6
    return goodput_mbps.tolist()


def load_mouse_fcts(csv_paths: Iterable[Path]) -> List[float]:
    durations: List[float] = []
    for csv_path in csv_paths:
        try:
            df = pd.read_csv(csv_path)
        except FileNotFoundError:
            logging.warning("Mouse CSV not found: %s", csv_path)
            continue
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.warning("Failed to read %s: %s", csv_path, exc)
            continue
        df = _normalize_columns(df)
        if "Duration" not in df:
            logging.warning("Duration column missing in %s", csv_path)
            continue
        vals = pd.to_numeric(df["Duration"], errors="coerce")
        vals = vals[(vals.notna()) & (vals > 0)]
        durations.extend(vals.tolist())
    return durations


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
        delta = _extract_tx_bytes(after.get(if_name, {})) - _extract_tx_bytes(before.get(if_name, {}))
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
            elephant_path = run_dir / "elephant_client.csv"
            mouse_paths = sorted(run_dir.glob("mouse_client_*.csv"))
            before_path = run_dir / "switch_stats_before.json"
            after_path = run_dir / "switch_stats_after.json"

            missing = []
            if not elephant_path.exists():
                missing.append("elephant_client.csv")
            if not mouse_paths:
                missing.append("mouse_client_*.csv")
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

            goodputs = load_elephant_goodput(elephant_path)
            for val in goodputs:
                elephant_rows.append(
                    {"proto": proto, "run_id": run_id, "goodput_mbps": float(val)}
                )

            fcts = load_mouse_fcts(mouse_paths)
            for fct in fcts:
                mouse_rows.append({"proto": proto, "run_id": run_id, "fct_s": float(fct)})

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


def plot_goodput_bar(
    elephant_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    means = []
    stds = []
    labels = []
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


def plot_fct_cdf(
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
        if values.size == 0:
            continue
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


def plot_fct_histogram(
    mouse_df: pd.DataFrame, output_path: Path, protos: Sequence[str]
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    required_cols = {"proto", "fct_s"}
    if mouse_df.empty or not required_cols.issubset(mouse_df.columns):
        ax.text(0.5, 0.5, "No mouse data", ha="center", va="center")
        fig.tight_layout()
        fig.savefig(output_path, dpi=200)
        plt.close(fig)
        return

    has_data = False
    percentile_annos: List[Tuple[str, Tuple[float, float, float], Any]] = []
    max_count = 0.0
    all_values_ms = (mouse_df["fct_s"] * 1000).to_numpy()
    bins = (
        np.histogram_bin_edges(all_values_ms, bins="auto") if all_values_ms.size else None
    )

    for proto in protos:
        subset = mouse_df[mouse_df["proto"] == proto]
        if subset.empty:
            logging.warning("No mouse FCT data for %s", proto)
            continue
        values_ms = subset["fct_s"].to_numpy() * 1000
        if values_ms.size == 0:
            continue
        counts, _, patches = ax.hist(
            values_ms,
            bins=bins if bins is not None and bins.size > 1 else "auto",
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
        ax.set_ylim(top=marker_y * 1.1)
        ax.set_xlabel("FCT (ms)")
        ax.set_ylabel("Frequency")
        ax.set_title("Mouse FCT Distribution")
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
    # Keep columns in requested order when available.
    pivot = pivot[[p for p in protos if p in pivot.columns]]

    if len(pivot) > HEATMAP_MAX_IFACES:
        # Keep interfaces with largest total load to keep the plot readable.
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
    lines.append("Whitebox Analysis Summary")
    lines.append("=" * 30)
    lines.append("")

    lines.append("Elephant Goodput (Mbps)")
    for proto in protos:
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            lines.append(f"- {proto}: no data")
            continue
        lines.append(
            f"- {proto}: mean={subset['goodput_mbps'].mean():.3f}, "
            f"std={subset['goodput_mbps'].std(ddof=0):.3f}, "
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

    output_subdir = args.output_subdir or Path("default")
    output_dir: Path = args.output_dir / "white" / output_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Writing outputs to %s", output_dir)

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

    run_list: List[Tuple[str, Path]] = [
        (f"{proto}:{run_dir.name}", run_dir)
        for proto in PROTO_ORDER
        for run_dir in run_dirs_by_proto.get(proto, [])
    ]

    data = collect_all_data(args.log_root, PROTO_ORDER, run_dirs_by_proto)
    elephant_df = data["elephant"]
    mouse_df = data["mouse"]
    link_df = data["link"]

    if elephant_df.empty and mouse_df.empty and link_df.empty:
        logging.error("No data found under %s", args.log_root)
        return

    fairness_df = compute_fairness(link_df)

    plot_goodput_bar(
        elephant_df,
        output_dir / "whitebox_goodput_bar.png",
        PROTO_ORDER,
    )
    plot_fct_cdf(
        mouse_df,
        output_dir / "whitebox_fct_cdf.png",
        PROTO_ORDER,
    )
    plot_fct_histogram(
        mouse_df,
        output_dir / "whitebox_fct_hist.png",
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
    else:
        import analysis.mouse_droploss_plot as droploss
        import analysis.mouse_retrans_plot as retrans

        # Drop-induced retrans (per proto, aggregated across selected runs)
        droploss_summaries: List[droploss.DropLossSummary] = []
        for proto in PROTO_ORDER:
            runs = run_dirs_by_proto.get(proto, [])
            if not runs:
                continue
            drop_total = 0
            total_flows = 0
            for run_dir in runs:
                summary = droploss.summarize_run(run_dir, label=proto)
                drop_total += summary.drop_flows
                total_flows += summary.total_flows
            droploss_summaries.append(
                droploss.DropLossSummary(
                    label=proto,
                    run_dir=runs[-1],
                    drop_flows=drop_total,
                    total_flows=total_flows,
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

        # Any retrans (per proto, aggregated across selected runs)
        retrans_summaries = []
        for proto in PROTO_ORDER:
            runs = run_dirs_by_proto.get(proto, [])
            if not runs:
                continue
            retrans_total = 0
            total_flows = 0
            for run_dir in runs:
                summary = retrans.summarize_run(run_dir, label=proto)
                retrans_total += summary.retrans_flows
                total_flows += summary.total_flows
            retrans_summaries.append(
                retrans.RetransSummary(
                    label=proto,
                    run_dir=runs[-1],
                    retrans_flows=retrans_total,
                    total_flows=total_flows,
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
