"""
Compute metrics and write summaries for whitebox experiments.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Sequence

import numpy as np
import pandas as pd


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
