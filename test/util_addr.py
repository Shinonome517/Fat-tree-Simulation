"""Address inspection helpers used across fat-tree tests."""

from __future__ import annotations

from typing import Any, List


def ipv4_addrs(node: Any, interface: str) -> List[str]:
    """Return list of IPv4 CIDRs configured on the interface."""
    output = node.cmd(f"ip -o -4 addr show dev {interface}").strip()
    if not output:
        return []
    return [line.split()[3] for line in output.splitlines()]
