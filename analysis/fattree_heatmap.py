"""
Plot helpers for Fat-Tree link heatmaps on a NetworkX layout.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence, Tuple

import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd

from fattree_nx import build_nx_graph_from_params, fattree_layout

LOG = logging.getLogger(__name__)


def _edge_key(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted((a, b)))


def map_interface_to_edge(if_name: str) -> Tuple[str, str] | None:
    """
    Convert an interface name (as used in Mininet addLink) to a pair of node names.
    Returns None if the name is not recognized as a fat-tree link.
    """
    if if_name == "lo" or if_name.startswith("br_"):
        return None

    patterns = [
        # Core <-> Agg
        (r"^c(?P<c>\d+)-to-a(?P<p>\d+)(?P<a>\d+)$", lambda m: (f"c{m['c']}", f"a{m['p']}{m['a']}")),
        (r"^a(?P<p>\d+)(?P<a>\d+)-to-c(?P<c>\d+)$", lambda m: (f"a{m['p']}{m['a']}", f"c{m['c']}")),
        # Agg <-> Edge
        (r"^a(?P<p>\d+)(?P<a>\d+)-to-e(?P<p2>\d+)(?P<e>\d+)$", lambda m: (f"a{m['p']}{m['a']}", f"e{m['p2']}{m['e']}")),
        (r"^e(?P<p>\d+)(?P<e>\d+)-to-a(?P<p2>\d+)(?P<a>\d+)$", lambda m: (f"e{m['p']}{m['e']}", f"a{m['p2']}{m['a']}")),
        # Edge <-> Host
        (r"^e(?P<p>\d+)(?P<e>\d+)-h(?P<h>\d+)$", lambda m: (f"e{m['p']}{m['e']}", f"h{m['p']}{m['e']}{m['h']}")),
    ]

    for pat, builder in patterns:
        m = re.match(pat, if_name)
        if m:
            return builder(m.groupdict())
    return None


def _draw_topology(
    G: nx.Graph,
    pos: Mapping[str, Tuple[float, float]],
    ax: plt.Axes,
    *,
    node_size: float = 120,
    node_color: str = "#dddddd",
    node_edgecolor: str = "#666666",
    edge_color: str = "#888888",
    edge_width: float = 1.5,
    draw_edges: bool = True,
    with_labels: bool = True,
) -> None:
    """Draw a fat-tree topology without load overlays for reuse across plots."""
    nx.draw_networkx_nodes(
        G,
        pos=pos,
        ax=ax,
        node_size=node_size,
        node_color=node_color,
        edgecolors=node_edgecolor,
        linewidths=0.5,
    )
    if draw_edges:
        nx.draw_networkx_edges(G, pos=pos, ax=ax, edge_color=edge_color, width=edge_width)
    if with_labels:
        nx.draw_networkx_labels(G, pos=pos, ax=ax, font_size=7)


def aggregate_link_loads(
    link_df: pd.DataFrame,
    protos: Sequence[str],
    *,
    warn: bool = True,
) -> Dict[str, Dict[Tuple[str, str], float]]:
    """
    Aggregate interface-level tx deltas into undirected link loads.

    For each proto/run_id:
      - map interface names to graph edges
      - sum both directions of the same link
    Then average per-link across runs for each proto.
    """
    if link_df.empty:
        return {proto: {} for proto in protos}

    required_cols = {"proto", "run_id", "if_name", "delta_tx_bytes"}
    missing = required_cols - set(link_df.columns)
    if missing:
        raise ValueError(f"link_df missing required columns: {missing}")

    warned: set[str] = set()
    per_proto_run: Dict[Tuple[str, str], Dict[Tuple[str, str], float]] = {}

    for _, row in link_df.iterrows():
        proto = str(row["proto"])
        run_id = str(row["run_id"])
        if_name = str(row["if_name"])
        delta = float(row["delta_tx_bytes"])

        # Skip common non-link interfaces without warning.
        if if_name == "lo" or if_name.startswith("br_"):
            continue

        edge = map_interface_to_edge(if_name)
        if edge is None:
            if warn and if_name not in warned:
                LOG.warning("Unrecognized interface name for heatmap: %s", if_name)
                warned.add(if_name)
            continue

        key = _edge_key(*edge)
        proto_run_key = (proto, run_id)
        per_link = per_proto_run.setdefault(proto_run_key, {})
        per_link[key] = per_link.get(key, 0.0) + delta

    per_proto: Dict[str, Dict[Tuple[str, str], float]] = {proto: {} for proto in protos}
    for proto in protos:
        runs = {k: v for k, v in per_proto_run.items() if k[0] == proto}
        if not runs:
            continue
        # Collect all edges across runs
        all_edges = set().union(*(links.keys() for links in runs.values()))
        for edge in all_edges:
            values = [links.get(edge, 0.0) for links in runs.values()]
            per_proto[proto][edge] = float(sum(values) / len(values))
    return per_proto


def plot_fattree_heatmap(
    link_df: pd.DataFrame,
    output_path: Path,
    protos: Sequence[str],
    *,
    k: int = 4,
) -> None:
    """Plot per-proto link loads over a fat-tree NetworkX layout."""
    agg = aggregate_link_loads(link_df, protos)
    G = build_nx_graph_from_params(k=k)
    pos = fattree_layout(G, k=k)

    n_cols = max(1, len(protos))
    fig, axes = plt.subplots(
        1,
        n_cols,
        figsize=(6 * n_cols, 4.5),
        squeeze=False,
    )
    axes = axes[0]

    for ax, proto in zip(axes, protos):
        loads = agg.get(proto, {}) or {}
        _draw_topology(G, pos, ax=ax, draw_edges=False, with_labels=False)

        edges = list(G.edges())
        edge_loads = [float(loads.get(_edge_key(*edge), 0.0)) for edge in edges]
        max_load = max(edge_loads) if edge_loads else 0.0
        if max_load <= 0:
            ax.text(0.5, 0.5, "No link data", ha="center", va="center")
            ax.set_title(f"{proto} link heatmap")
            ax.axis("off")
            continue

        cmap = cm.viridis
        norm = mcolors.Normalize(vmin=0, vmax=max_load)
        colors = [cmap(norm(val)) for val in edge_loads]
        # Scale edge widths linearly with intercept 2 so low-load links remain visible.
        widths = [2.0 + 2.0 * (val / max_load) for val in edge_loads]

        nx.draw_networkx_edges(
            G,
            pos=pos,
            ax=ax,
            edgelist=edges,
            edge_color=colors,
            width=widths,
        )
        nx.draw_networkx_labels(G, pos=pos, ax=ax, font_size=7)

        sm = cm.ScalarMappable(norm=norm, cmap=cmap)
        cbar = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("delta tx bytes")
        ax.set_title(f"{proto} link heatmap")
        ax.axis("off")

    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_fattree_topology(
    output_path: Path,
    *,
    k: int = 4,
    with_labels: bool = True,
) -> None:
    """Plot a plain fat-tree topology without load overlays (for slides/papers)."""
    G = build_nx_graph_from_params(k=k)
    pos = fattree_layout(G, k=k)

    fig, ax = plt.subplots(figsize=(6, 4.5))
    _draw_topology(
        G,
        pos,
        ax=ax,
        node_size=120,
        node_color="#dddddd",
        node_edgecolor="#666666",
        edge_color="#888888",
        edge_width=1.5,
        with_labels=with_labels,
    )
    ax.axis("off")

    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
