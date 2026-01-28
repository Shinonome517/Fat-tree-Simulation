"""
NetworkX helpers for building and laying out Fat-Tree topologies.
"""

import sys
from pathlib import Path

import networkx as nx


# Ensure the repo root is on sys.path so direct script execution works.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topology import _fattree_dims


def build_nx_graph_from_params(k=4):
    """
    Build a networkx.Graph with the exact same fat-tree structure as topology.py.

    Node names must match the Mininet switch/host names exactly
    (e.g., "c0", "c1", "a0_0", "e2_1", "h1_1_0").

    Each node includes at least these attributes:
      - 'layer': one of 'core' / 'agg' / 'edge' / 'host'
      - 'pod'  : pod number for non-core nodes; None/omitted for core
      - 'idx'  : index within the layer (e.g., for k=4 this is 0 or 1)

    Edges are undirected and connect the same node pairs as Mininet addLink().
    """
    (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        n_hosts_per_edge,
        n_core_groups,
        n_core_per_group,
        n_cores,
    ) = _fattree_dims(k)

    G = nx.Graph()

    # Core switches
    for c in range(n_cores):
        idx = c % n_core_per_group
        group = c // n_core_per_group
        G.add_node(f"c{c}", layer="core", pod=None, idx=idx, group=group)

    # Aggregation / Edge switches and Hosts
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            G.add_node(f"a{p}{a}", layer="agg", pod=p, idx=a)

        for e in range(n_edge_per_pod):
            edge_name = f"e{p}{e}"
            G.add_node(edge_name, layer="edge", pod=p, idx=e)
            for h in range(n_hosts_per_edge):
                host_name = f"h{p}{e}{h}"
                G.add_node(host_name, layer="host", pod=p, idx=h, edge_idx=e)
                G.add_edge(edge_name, host_name)

    # Agg–Edge links (within pod)
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for e in range(n_edge_per_pod):
                G.add_edge(f"a{p}{a}", f"e{p}{e}")

    # Core–Agg links (cross-pod)
    for p in range(n_pods):
        for a in range(n_agg_per_pod):
            for i in range(n_core_groups):
                c = i * n_core_per_group + a
                G.add_edge(f"c{c}", f"a{p}{a}")

    return G


def fattree_layout(G, k=4):
    """
    Compute a fat-tree layout and return {node: (x, y)}.

    Layout rules:
      - Core switches on a single top row
      - Pods arranged horizontally
      - Within each pod:
          Agg switches above Edge switches
          Hosts below Edge switches
      - Roughly resembles the textbook-style fat-tree diagram
    """
    (
        n_pods,
        n_agg_per_pod,
        n_edge_per_pod,
        n_hosts_per_edge,
        n_core_groups,
        n_core_per_group,
        _,
    ) = _fattree_dims(k)

    y_core = 3.0
    y_agg = 2.0
    y_edge = 1.0
    y_host = 0.0

    # Expand pod width horizontally by spacing edge switches further apart.
    agg_gap = 3.2
    edge_gap = 3.2
    # Spread hosts horizontally to reduce overlap under edge switches.
    host_gap = 1.6
    pod_gap = max(n_edge_per_pod * edge_gap, n_agg_per_pod * agg_gap) + 4.0
    # Keep core switches readable when the overall width increases.
    core_gap = 3.2
    core_group_gap = 6.0

    positions = {}
    core_center = ((n_pods - 1) * pod_gap) / 2

    for node, data in G.nodes(data=True):
        layer = data.get("layer")
        if layer == "core":
            idx = data.get("idx", 0)
            group = data.get("group", 0)
            x = core_center + idx * core_gap
            if n_core_groups > 1:
                x += (group - (n_core_groups - 1) / 2) * core_group_gap
            y = y_core
        elif layer == "agg":
            pod = data.get("pod", 0)
            idx = data.get("idx", 0)
            x = pod * pod_gap + idx * agg_gap
            y = y_agg
        elif layer == "edge":
            pod = data.get("pod", 0)
            idx = data.get("idx", 0)
            x = pod * pod_gap + idx * edge_gap
            y = y_edge
        elif layer == "host":
            pod = data.get("pod", 0)
            edge_idx = data.get("edge_idx", 0)
            h_idx = data.get("idx", 0)
            x = pod * pod_gap + edge_idx * edge_gap
            if n_hosts_per_edge > 1:
                x += (h_idx - (n_hosts_per_edge - 1) / 2) * host_gap
            y = y_host
        else:
            x, y = 0.0, 0.0
        positions[node] = (x, y)

    return positions
