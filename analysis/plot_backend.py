"""
Matplotlib backend helper for headless environments.
"""

from __future__ import annotations

import sys

import matplotlib


def use_agg_backend() -> None:
    if "matplotlib.pyplot" in sys.modules:
        return
    matplotlib.use("Agg")
