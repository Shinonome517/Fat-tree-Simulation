"""
Per-run P99 scatter plot for mouse FCT (QUIC vs MPQUIC).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from plot_backend import use_agg_backend

use_agg_backend()
import matplotlib.pyplot as plt  # noqa: E402


SEED_SUFFIX_RE = re.compile(r"_seed(\d+)$")


def _extract_seed(series: pd.Series) -> pd.Series:
    """Extract numeric seed suffix from run_id; returns None when missing."""
    matches = series.str.extract(SEED_SUFFIX_RE)
    # matches is a DataFrame with one column; squeeze to Series while preserving index.
    return matches[0]


def plot_run_p99_scatter(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
) -> None:
    """
    Plot run-level P99 mouse FCT scatter between protos[0] (x-axis) and protos[1] (y-axis).

    - Requires run_id to end with _seed<value> for both protos; otherwise warn and skip output.
    - If seeds do not align between protos, warn and skip output.
    - Avoids re-reading data: consumes the provided mouse_df only.
    """
    if len(protos) < 2:
        logging.warning("Need at least two protocols to plot scatter; got %s", protos)
        return
    proto_x, proto_y = protos[0], protos[1]
    required_cols = {"proto", "run_id", "fct_s"}
    if mouse_df.empty or not required_cols.issubset(mouse_df.columns):
        logging.warning("Mouse DataFrame missing required data for scatter plot.")
        return

    # Compute per-run P99 once.
    p99_df = (
        mouse_df.groupby(["proto", "run_id"])["fct_s"]
        .quantile(0.99)
        .reset_index(name="p99_fct_s")
    )

    def _prep(proto: str) -> pd.DataFrame:
        subset = p99_df[p99_df["proto"] == proto].copy()
        subset["seed"] = _extract_seed(subset["run_id"])
        return subset

    df_x = _prep(proto_x)
    df_y = _prep(proto_y)
    if df_x.empty or df_y.empty:
        logging.warning("No per-run P99 data for %s or %s; skipping scatter plot.", proto_x, proto_y)
        return

    # Require seeds for all runs.
    for proto, df in ((proto_x, df_x), (proto_y, df_y)):
        if df["seed"].isnull().any():
            logging.warning(
                "Seed suffix missing in run_id for %s; cannot build scatter plot.", proto
            )
            return
        if df["seed"].duplicated().any():
            logging.warning("Duplicate seed values for %s runs; scatter plot skipped.", proto)
            return

    merged = pd.merge(
        df_x[["seed", "p99_fct_s"]],
        df_y[["seed", "p99_fct_s"]],
        on="seed",
        how="inner",
        suffixes=("_x", "_y"),
    )
    if merged.empty:
        logging.warning(
            "No matching seeds between %s and %s; scatter plot will not be generated.",
            proto_x,
            proto_y,
        )
        return

    # Convert to ms for readability.
    merged["p99_ms_x"] = merged["p99_fct_s_x"] * 1000.0
    merged["p99_ms_y"] = merged["p99_fct_s_y"] * 1000.0

    x_vals = merged["p99_ms_x"].to_numpy()
    y_vals = merged["p99_ms_y"].to_numpy()
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.scatter(x_vals, y_vals, alpha=0.8, edgecolors="none")

    lo = float(np.nanmin([x_vals.min(), y_vals.min()]))
    hi = float(np.nanmax([x_vals.max(), y_vals.max()]))
    ax.plot([lo, hi], [lo, hi], linestyle="--", color="gray", label="y = x")

    ax.set_xlabel(f"{proto_x} P99 FCT (ms)")
    ax.set_ylabel(f"{proto_y} P99 FCT (ms)")
    ax.set_title("Per-run P99 Mouse FCT (seed-aligned)")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.set_aspect("equal", "box")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
    logging.info("Wrote scatter plot to %s", output_path)
