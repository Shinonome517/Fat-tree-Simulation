"""
Link heatmap plots for whitebox analysis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from plot_backend import use_agg_backend

use_agg_backend()
import matplotlib.pyplot as plt  # noqa: E402


HEATMAP_MAX_IFACES = 20  # Limit for readability; trim if there are many ifaces.


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

    fig, ax = plt.subplots(figsize=(6, max(3.0, 0.35 * len(pivot.index))))
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
