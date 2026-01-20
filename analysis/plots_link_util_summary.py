"""
Run-level link utilization summary plots.
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


def _proto_color(proto: str, proto_colors: Mapping[str, str] | None) -> str | None:
    if proto_colors is None:
        return None
    return proto_colors.get(str(proto).lower())


def plot_run_metric_bar(
    run_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    metric: str,
    ylabel: str,
    *,
    title: str | None = None,
    y_lim: tuple[float, float] | None = None,
    proto_colors: Mapping[str, str] | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    means = []
    stds = []
    labels = []
    colors = []
    missing_color = False
    if run_df.empty or "proto" not in run_df.columns or metric not in run_df.columns:
        run_df = pd.DataFrame()
    for proto in protos:
        subset = run_df[run_df["proto"] == proto] if not run_df.empty else run_df
        if subset.empty:
            logging.warning("No run summary data for %s (%s).", proto, metric)
            continue
        values = pd.to_numeric(subset[metric], errors="coerce").dropna()
        if values.empty:
            logging.warning("No valid run summary values for %s (%s).", proto, metric)
            continue
        labels.append(proto)
        means.append(values.mean())
        stds.append(values.std(ddof=0))
        color = _proto_color(proto, proto_colors)
        if color is None and proto_colors is not None:
            missing_color = True
        colors.append(color)

    if not means:
        ax.text(0.5, 0.5, "No run summary data", ha="center", va="center")
    else:
        x = np.arange(len(labels))
        bar_kwargs = dict(yerr=stds, capsize=8, alpha=0.8)
        if proto_colors is not None and not missing_color:
            bar_kwargs["color"] = colors
        ax.bar(x, means, **bar_kwargs)
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylabel(ylabel)
        if title:
            ax.set_title(title)
        if y_lim is not None:
            ax.set_ylim(*y_lim)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_run_metric_scatter(
    run_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    metric: str,
    ylabel: str,
    *,
    title: str | None = None,
    y_lim: tuple[float, float] | None = None,
    proto_colors: Mapping[str, str] | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    rng = np.random.default_rng(seed=0)
    has_data = False
    positions = []
    labels = []
    if run_df.empty or "proto" not in run_df.columns or metric not in run_df.columns:
        run_df = pd.DataFrame()
    for idx, proto in enumerate(protos):
        subset = run_df[run_df["proto"] == proto] if not run_df.empty else run_df
        if subset.empty:
            logging.warning("No run summary data for %s (%s).", proto, metric)
            continue
        values = pd.to_numeric(subset[metric], errors="coerce").dropna()
        if values.empty:
            logging.warning("No valid run summary values for %s (%s).", proto, metric)
            continue
        positions.append(idx)
        labels.append(proto)
        x = np.full(len(values), idx, dtype=float)
        if len(values) > 1:
            x += rng.uniform(-0.12, 0.12, size=len(values))
        scatter_kwargs = dict(
            alpha=0.8,
            edgecolors="black",
            linewidth=0.4,
            label=proto,
        )
        color = _proto_color(proto, proto_colors)
        if color is not None:
            scatter_kwargs["color"] = color
        ax.scatter(x, values, **scatter_kwargs)
        has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No run summary data", ha="center", va="center")
    else:
        ax.set_xticks(positions)
        ax.set_xticklabels(labels)
        ax.set_ylabel(ylabel)
        if title:
            ax.set_title(title)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
        if y_lim is not None:
            ax.set_ylim(*y_lim)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
