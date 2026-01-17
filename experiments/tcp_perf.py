#!/usr/bin/env python3
"""
Minimal TCP/MPTCP perf helper for normal.py.

Server: accept() loop, discard all received data.
Client: connect, send N bytes, close, write CSV with Duration/Sent/Received/retrans./spurious.
"""

from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from pathlib import Path
from typing import Tuple

DEFAULT_CHUNK_SIZE = 64 * 1024


def _pick_proto(proto: str) -> int:
    proto = (proto or "").lower()
    if proto == "mptcp":
        return getattr(socket, "IPPROTO_MPTCP", 262)
    return socket.IPPROTO_TCP


def _make_socket(proto: str) -> socket.socket:
    ip_proto = _pick_proto(proto)
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM, ip_proto)
    except OSError as exc:  # pragma: no cover - runtime guard
        raise SystemExit(f"Failed to create {proto} socket: {exc}") from exc


def run_server(bind_ip: str, port: int, backlog: int, proto: str) -> int:
    srv = _make_socket(proto)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        # Best-effort; continue even if this fails on some stacks.
        pass
    srv.bind((bind_ip, port))
    srv.listen(backlog)
    print(f"[tcp_perf] listening on {bind_ip}:{port} ({proto}, backlog={backlog})")
    try:
        while True:
            conn, addr = srv.accept()
            try:
                while True:
                    data = conn.recv(DEFAULT_CHUNK_SIZE)
                    if not data:
                        break
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"[tcp_perf] server error ({proto}): {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            srv.close()
        except Exception:
            pass
    return 0


def _write_csv(csv_path: Path, duration: float, sent: int, received: int) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Duration", "Sent", "Received", "retrans.", "spurious"])
        writer.writerow([f"{duration:.6f}", sent, received, 0, 0])


def run_client(host: str, port: int, payload_bytes: int, csv_path: Path, proto: str) -> int:
    sock = _make_socket(proto)
    sent = 0
    received = 0
    start = time.monotonic()
    try:
        sock.connect((host, port))
        chunk = b"\0" * DEFAULT_CHUNK_SIZE
        remaining = payload_bytes
        while remaining > 0:
            to_send = min(len(chunk), remaining)
            sent_now = sock.send(chunk[:to_send])
            sent += sent_now
            remaining -= sent_now
        try:
            sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    end = time.monotonic()
    _write_csv(csv_path, end - start, sent, received)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal TCP/MPTCP perf helper.")
    sub = parser.add_subparsers(dest="mode", required=True)

    srv = sub.add_parser("server", help="Run as TCP/MPTCP server (discard).")
    srv.add_argument("--bind", default="0.0.0.0")
    srv.add_argument("--port", type=int, required=True)
    srv.add_argument("--backlog", type=int, default=65535)
    srv.add_argument("--proto", choices=["tcp", "mptcp"], default="tcp")

    cli = sub.add_parser("client", help="Run as TCP/MPTCP client.")
    cli.add_argument("--host", required=True)
    cli.add_argument("--port", type=int, required=True)
    cli.add_argument("--bytes", type=int, required=True, dest="payload_bytes")
    cli.add_argument("--csv", type=Path, required=True)
    cli.add_argument("--proto", choices=["tcp", "mptcp"], default="tcp")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "server":
        rc = run_server(args.bind, args.port, args.backlog, args.proto)
    else:
        rc = run_client(args.host, args.port, args.payload_bytes, args.csv, args.proto)
    sys.exit(rc)


if __name__ == "__main__":
    main()
