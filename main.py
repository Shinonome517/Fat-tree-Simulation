#!/usr/bin/env python3
"""CLI entrypoint for the Fat-Tree Mininet topology."""

import argparse

from mininet.log import setLogLevel

from ops import headless_loop, run_cli
from topology import build_fattree_topology, stop_fattree_topology

__all__ = ['parse_args', 'main']


def parse_args():
    parser = argparse.ArgumentParser(description='FatTree Mininet topology (headless by default).')
    parser.add_argument('--bw', type=int, default=100, help='Link bandwidth in Mbps (default: 100).')
    parser.add_argument('--delay', default='0.5ms', help="Link delay applied to all links (default: '0.5ms').")
    parser.add_argument('--q', type=int, default=50, metavar='PKTS', help='Max queue size in packets (default: 50).')
    parser.add_argument(
        '-k',
        '--k',
        type=int,
        default=4,
        metavar='EVEN',
        help='Even fat-tree k (2-16) to build (default: 4).',
    )
    parser.add_argument('--cli', action='store_true', help='Drop into Mininet CLI after bringing up the topology.')
    return parser.parse_args()


def main():
    args = parse_args()
    setLogLevel('info')
    ctx = build_fattree_topology(
        bw_mbps=args.bw,
        delay=args.delay,
        queue_pkts=args.q,
        start=True,
        k=args.k,
    )
    try:
        if args.cli:
            run_cli(ctx.net)
        else:
            headless_loop(ctx.net)
    finally:
        stop_fattree_topology(ctx)


if __name__ == '__main__':
    main()
