"""
Mouse FCT plots for whitebox analysis.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from plot_backend import use_agg_backend

use_agg_backend()
import matplotlib.pyplot as plt  # noqa: E402


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


def _resolve_fct_unit(exclude_outliers: bool) -> Tuple[str, float]:
    if exclude_outliers:
        return "ms", 1000.0
    return "s", 1.0


def plot_fct_cdf(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    exclude_outliers: bool = False,
    mark_outliers: bool = False,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    unit_label, scale = _resolve_fct_unit(exclude_outliers)
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
        line = _plot_cdf(ax, values * scale, proto)
        color = line.get_color()
        p50, p90, p99 = np.percentile(values * scale, [50, 90, 99])
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
            outlier_values = values_sorted[sorted_mask] * scale
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
        ax.set_xlabel(f"FCT ({unit_label})")
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


def plot_fct_histogram(
    mouse_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    exclude_outliers: bool = False,
    mark_outliers: bool = False,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    unit_label, scale = _resolve_fct_unit(exclude_outliers)
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

    all_values_scaled = (
        np.concatenate([vals * scale for vals in values_by_proto.values()])
        if values_by_proto
        else np.array([])
    )
    bins = (
        np.histogram_bin_edges(all_values_scaled, bins="auto")
        if all_values_scaled.size
        else None
    )

    has_data = False
    percentile_annos: List[Tuple[str, Tuple[float, float, float], Any]] = []
    outlier_annos: List[Tuple[np.ndarray, Any, str, bool]] = []
    max_count = 0.0

    for proto in protos:
        values = values_by_proto.get(proto)
        if values is None:
            continue
        values_scaled = values * scale
        if values_scaled.size == 0:
            continue
        counts, _, patches = ax.hist(
            values_scaled,
            bins=bins if bins is not None and bins.size > 1 else "auto",
            density=True,
            alpha=0.65,
            label=proto,
            edgecolor="black",
            linewidth=0.5,
        )
        max_count = max(max_count, float(np.max(counts)) if counts.size else 0.0)
        p50, p90, p99 = np.percentile(values_scaled, [50, 90, 99])
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
                    outlier_annos.append((no_retrans * scale, color, proto, False))
                if has_retrans.size:
                    outlier_annos.append((has_retrans * scale, color, proto, True))
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
            for values_scaled, color, proto, had_retrans in outlier_annos:
                if had_retrans:
                    ax.scatter(
                        values_scaled,
                        [marker_y] * len(values_scaled),
                        facecolors=color,
                        edgecolors=color,
                        marker="s",
                        s=24,
                        linewidth=0.8,
                        label=f"{proto} outliers (retrans)",
                    )
                else:
                    ax.scatter(
                        values_scaled,
                        [marker_y] * len(values_scaled),
                        facecolors="none",
                        edgecolors=color,
                        marker="o",
                        s=24,
                        linewidth=1.0,
                        label=f"{proto} outliers (no retrans)",
                    )
        ax.set_ylim(top=marker_y * 1.1)
        ax.set_xlabel(f"FCT ({unit_label})")
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
