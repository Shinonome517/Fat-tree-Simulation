"""
Compute metrics and summaries for abnormal experiments.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "jain_index",
    "compute_fairness",
    "compute_link_util_series",
    "compute_u_max_percentiles",
    "write_summary",
]


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
    fairness_rows: List[dict[str, object]] = []
    if link_df.empty:
        return pd.DataFrame(columns=["proto", "run_id", "jain"])

    for (proto, run_id), group in link_df.groupby(["proto", "run_id"]):
        jain = jain_index(group["delta_tx_bytes"].to_numpy())
        fairness_rows.append({"proto": proto, "run_id": run_id, "jain": jain})
    return pd.DataFrame(fairness_rows)


def compute_link_util_series(link_ts_df: pd.DataFrame) -> pd.DataFrame:
    if link_ts_df.empty:
        return pd.DataFrame(
            columns=["proto", "run_id", "sample_idx", "elapsed_s", "u_mean", "u_max", "cv"]
        )
    required = ["proto", "run_id", "sample_idx", "elapsed_s", "u_l"]
    missing = [col for col in required if col not in link_ts_df.columns]
    if missing:
        logging.warning("Link timeseries missing columns: %s", ", ".join(missing))
        return pd.DataFrame(
            columns=["proto", "run_id", "sample_idx", "elapsed_s", "u_mean", "u_max", "cv"]
        )

    df = link_ts_df.copy()
    df["u_l"] = pd.to_numeric(df["u_l"], errors="coerce")
    df["elapsed_s"] = pd.to_numeric(df["elapsed_s"], errors="coerce")

    rows: List[Dict[str, object]] = []
    for (proto, run_id, sample_idx), group in df.groupby(["proto", "run_id", "sample_idx"]):
        values = group["u_l"].dropna().to_numpy()
        if values.size == 0:
            continue
        elapsed_vals = group["elapsed_s"].dropna().to_numpy()
        elapsed = float(elapsed_vals.max()) if elapsed_vals.size else float("nan")
        u_mean = float(values.mean())
        u_max = float(values.max())
        cv = float(values.std(ddof=0) / u_mean) if u_mean > 0 else 0.0
        rows.append(
            {
                "proto": proto,
                "run_id": run_id,
                "sample_idx": sample_idx,
                "elapsed_s": elapsed,
                "u_mean": u_mean,
                "u_max": u_max,
                "cv": cv,
            }
        )
    return pd.DataFrame(rows)


def compute_u_max_percentiles(util_df: pd.DataFrame) -> Dict[Tuple[str, str], Tuple[float, float]]:
    percentiles: Dict[Tuple[str, str], Tuple[float, float]] = {}
    if util_df.empty:
        return percentiles
    for (proto, run_id), group in util_df.groupby(["proto", "run_id"]):
        values = group["u_max"].dropna().to_numpy()
        if values.size == 0:
            continue
        p95, p99 = np.percentile(values, [95, 99])
        percentiles[(proto, run_id)] = (float(p95), float(p99))
    return percentiles


def write_summary(
    output_path: Path,
    elephant_df: pd.DataFrame,
    mouse_df: pd.DataFrame,
    fairness_df: pd.DataFrame,
    protos: Sequence[str],
    *,
    experiment_label: str = "Abnormal",
) -> None:
    lines: List[str] = []
    lines.append(f"{experiment_label} Analysis Summary")
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
