"""Interface statistics helpers."""

from __future__ import annotations

import json
import re
from typing import Any


def tx_bytes_ovs(node: Any, ifname: str) -> int:
    """Retrieve tx_bytes from an OVS interface row."""
    out = node.cmd(
        f"ovs-vsctl --format=csv --columns=statistics list interface {ifname}"
    ) or ""
    match = re.search(r"statistics=\{([^}]*)\}", out)
    if not match:
        return 0
    pairs = match.group(1).split(",")
    kv = {}
    for raw in pairs:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        kv[key.strip()] = value.strip()
    try:
        return int(kv.get("tx_bytes", "0"))
    except ValueError:
        return 0


def tx_bytes_kernel(node: Any, ifname: str) -> int:
    """Retrieve tx_bytes for a plain Linux interface."""
    raw = node.cmd(f"ip -s -j link show dev {ifname}") or "[]"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not payload:
        return 0
    info = payload[0]

    for key in ("stats64", "statistics"):
        section = info.get(key, {})
        if isinstance(section, dict):
            tx = section.get("tx")
            if isinstance(tx, dict) and "bytes" in tx:
                try:
                    return int(tx["bytes"])
                except (TypeError, ValueError):
                    return 0

    tx_section = info.get("tx")
    if isinstance(tx_section, dict) and "bytes" in tx_section:
        try:
            return int(tx_section["bytes"])
        except (TypeError, ValueError):
            return 0

    return 0
