#!/usr/bin/env python3
"""Backward-compatible wrapper for the Fat-Tree topology CLI and helpers."""

from main import main as _main
from topology import *  # re-export topology helpers for compatibility
from topology import __all__ as topology_exports

__all__ = list(topology_exports) + ['main']


def main():
    _main()


if __name__ == '__main__':
    main()
