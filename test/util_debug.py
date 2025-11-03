"""Utilities for emitting targeted debug information when assertions fail."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

import pytest


def dump(node, cmd: str, limit: int = 800) -> str:
    """Execute command on node and return truncated output."""
    out = node.cmd(cmd) or ""
    out = out.strip()
    return out if len(out) <= limit else out[:limit] + "...(truncated)"


@dataclass(frozen=True)
class DumpSpec:
    """Describe a command whose output should be captured on failure."""

    node: Any
    cmd: str
    label: Optional[str] = None
    limit: int = 800


def _format_dump(spec: DumpSpec) -> str:
    body = dump(spec.node, spec.cmd, limit=spec.limit)
    header = f"{spec.label}\n" if spec.label else ""
    return f"{header}$ {spec.cmd}\n{body}".rstrip()


def fail_with_dumps(message: str, specs: Iterable[DumpSpec]) -> None:
    """Raise a pytest failure augmented with the requested command outputs."""
    sections: List[str] = [_format_dump(spec) for spec in specs]
    detail = "\n\n".join(sections)
    pytest.fail(f"{message}\n\n{detail}" if detail else message)
