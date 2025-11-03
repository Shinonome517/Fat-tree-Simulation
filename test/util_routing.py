"""Route inspection helpers used by the fat-tree test-suite."""

from __future__ import annotations

import json
from typing import Any, Dict, List


def _ip_route_json(node: Any, target: str) -> List[Dict[str, Any]]:
    raw = node.cmd(f"ip -j route show {target}") or "[]"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def has_multipath(node: Any, dst: str) -> bool:
    """Return True when the selected route contains >=2 nexthops."""
    routes = _ip_route_json(node, dst)
    return any("multipath" in r and len(r["multipath"]) >= 2 for r in routes)
