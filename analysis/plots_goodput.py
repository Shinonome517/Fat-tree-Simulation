"""
Goodput plots for whitebox analysis.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Sequence

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


def plot_goodput_bar_proto(
    elephant_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    proto_colors: Mapping[str, str] | None = None,
    y_lim: tuple[float, float] | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    means = []
    stds = []
    labels = []
    colors = []
    missing_color = False
    for proto in protos:
        subset = elephant_df[elephant_df["proto"] == proto]
        if subset.empty:
            logging.warning("No elephant goodput data for %s", proto)
            continue
        labels.append(proto)
        means.append(subset["goodput_mbps"].mean())
        stds.append(subset["goodput_mbps"].std(ddof=0))
        color = proto_colors.get(str(proto).lower()) if proto_colors else None
        if color is None and proto_colors is not None:
            missing_color = True
        colors.append(color)

    if not means:
        ax.text(0.5, 0.5, "No elephant data", ha="center", va="center")
    else:
        x = np.arange(len(labels))
        bar_kwargs = dict(yerr=stds, capsize=8, alpha=0.8)
        if proto_colors is not None and not missing_color:
            bar_kwargs["color"] = colors
        ax.bar(x, means, **bar_kwargs)
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylabel("Goodput (Mbps)")
        ax.set_title("Elephant Goodput mean (error bars = SD)")
        if y_lim is not None:
            ax.set_ylim(y_lim)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_goodput_scatter_proto(
    elephant_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    proto_colors: Mapping[str, str] | None = None,
    y_lim: tuple[float, float] | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    rng = np.random.default_rng(seed=0)
    has_data = False
    positions = []
    labels = []
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
        scatter_kwargs = dict(
            alpha=0.8,
            edgecolors="black",
            linewidth=0.4,
            label=proto,
        )
        color = proto_colors.get(str(proto).lower()) if proto_colors else None
        if color is not None:
            scatter_kwargs["color"] = color
        ax.scatter(x, subset["goodput_mbps"], **scatter_kwargs)
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
        if y_lim is not None:
            ax.set_ylim(y_lim)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
