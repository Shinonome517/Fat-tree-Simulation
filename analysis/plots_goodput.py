"""
Goodput plots for whitebox analysis.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from plot_backend import use_agg_backend

use_agg_backend()
import matplotlib.pyplot as plt  # noqa: E402


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
