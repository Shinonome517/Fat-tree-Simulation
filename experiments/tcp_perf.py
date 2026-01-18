#!/usr/bin/env python3
"""
Minimal TCP/MPTCP perf helper for normal.py.

Server: accept() loop, discard all received data.
Client: connect, send N bytes, close, write CSV with Duration/Sent/Received/retrans./spurious.
"""

from __future__ import annotations

import argparse
import csv
import ssl
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Tuple

DEFAULT_CHUNK_SIZE = 1024 * 1024
PICOQUIC_CERT_PATH = "/etc/picoquic/server-cert.pem"
PICOQUIC_KEY_PATH = "/etc/picoquic/server-key.pem"

# struct tcp_info (linux/tcp.h) up to tcpi_total_retrans is 104 bytes.
# We parse only stable early fields to avoid kernel-version sensitivity.
_TCP_INFO_FMT = "=8B24I"
_TCP_INFO_BASE_LEN = struct.calcsize(_TCP_INFO_FMT)
_TCP_INFO_LOST_IDX = 8 + 6
_TCP_INFO_TOTAL_RETRANS_IDX = 8 + 23
_TCP_INFO_REQ_LEN = 256  # Large enough to include tcpi_dsack_dups on modern kernels.
_TCP_INFO_DSACK_DUPS_OFFSET = 224  # tcpi_dsack_dups sits after tcpi_bytes_retrans.
_TCP_INFO_DSACK_DUPS_LEN = 4


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


def _server_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    except AttributeError:
        pass
    ctx.load_cert_chain(PICOQUIC_CERT_PATH, PICOQUIC_KEY_PATH)
    return ctx


def _client_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    except AttributeError:
        pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _tcp_info_counters(sock: socket.socket) -> Tuple[int, int, int | None]:
    """
    Best-effort per-connection counters from TCP_INFO.

    Returns (total_retrans, lost, dsack_dups). If unavailable, dsack_dups is None.
    """
    tcp_info_opt = getattr(socket, "TCP_INFO", None)
    if tcp_info_opt is None:
        return 0, 0, None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, tcp_info_opt, _TCP_INFO_REQ_LEN)
    except OSError:
        return 0, 0, None
    if len(raw) < _TCP_INFO_BASE_LEN:
        return 0, 0, None
    try:
        vals = struct.unpack(_TCP_INFO_FMT, raw[:_TCP_INFO_BASE_LEN])
    except Exception:
        return 0, 0, None
    lost = int(vals[_TCP_INFO_LOST_IDX])
    total_retrans = int(vals[_TCP_INFO_TOTAL_RETRANS_IDX])
    dsack_dups: int | None = None
    if len(raw) >= (_TCP_INFO_DSACK_DUPS_OFFSET + _TCP_INFO_DSACK_DUPS_LEN):
        try:
            dsack_dups = int(
                struct.unpack_from(
                    "=I", raw, _TCP_INFO_DSACK_DUPS_OFFSET
                )[0]
            )
        except Exception:
            dsack_dups = None
    return total_retrans, lost, dsack_dups


def _drain_connection(conn: socket.socket, buf_size: int) -> None:
    buf = bytearray(max(int(buf_size), 1))
    view = memoryview(buf)
    try:
        while True:
            n = conn.recv_into(view)
            if n <= 0:
                break
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_server(bind_ip: str, port: int, backlog: int, proto: str, chunk_size: int) -> int:
    srv = _make_socket(proto)
    ssl_ctx = _server_ssl_context()
    chunk_size = max(int(chunk_size), 1)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        # Best-effort; continue even if this fails on some stacks.
        pass
    srv.bind((bind_ip, port))
    srv.listen(backlog)
    print(f"[tcp_perf] listening on {bind_ip}:{port} ({proto}, backlog={backlog}, chunk_size={chunk_size})")
    try:
        while True:
            conn, addr = srv.accept()
            try:
                tls_conn = ssl_ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError as exc:
                print(f"[tcp_perf] TLS handshake failed from {addr}: {exc}", file=sys.stderr)
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                thread = threading.Thread(
                    target=_drain_connection, args=(tls_conn, chunk_size), daemon=True
                )
                thread.start()
            except Exception:
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
        writer.writerow(
            ["Duration", "Sent", "Received", "retrans.", "spurious", "lost"]
        )
        writer.writerow([f"{duration:.6f}", sent, received, 0, 0, 0])


def run_client(
    host: str,
    port: int,
    payload_bytes: int,
    csv_path: Path,
    proto: str,
    chunk_size: int,
) -> int:
    sock = _make_socket(proto)
    ssl_ctx = _client_ssl_context()
    chunk_size = max(int(chunk_size), 1)
    sent = 0
    received = 0
    total_retrans = 0
    lost = 0
    dsack_dups: int | None = None
    tls_sock: socket.socket | None = None
    start = time.monotonic()
    try:
        sock.connect((host, port))
        try:
            tls_sock = ssl_ctx.wrap_socket(sock, server_hostname=None)
        except ssl.SSLError as exc:
            raise SystemExit(f"TLS handshake failed: {exc}") from exc

        try:
            tls_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        buf = bytearray(chunk_size)
        view = memoryview(buf)
        remaining = payload_bytes
        while remaining > 0:
            to_send = min(len(view), remaining)
            tls_sock.sendall(view[:to_send])
            sent += to_send
            remaining -= to_send
        try:
            tls_sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        # Wait for peer FIN so Duration better matches "transfer completed" semantics.
        try:
            while True:
                data = tls_sock.recv(1)
                if not data:
                    break
                received += len(data)
        except Exception:
            pass

        total_retrans, lost, dsack_dups = _tcp_info_counters(tls_sock)
    finally:
        try:
            if tls_sock is not None:
                tls_sock.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
    end = time.monotonic()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Duration", "Sent", "Received", "retrans.", "spurious", "lost"])
        writer.writerow(
            [
                f"{(end - start):.6f}",
                sent,
                received,
                total_retrans,
                "" if dsack_dups is None else dsack_dups,
                lost,
            ]
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal TCP/MPTCP perf helper.")
    sub = parser.add_subparsers(dest="mode", required=True)

    srv = sub.add_parser("server", help="Run as TCP/MPTCP server (discard).")
    srv.add_argument("--bind", default="0.0.0.0")
    srv.add_argument("--port", type=int, required=True)
    srv.add_argument("--backlog", type=int, default=65535)
    srv.add_argument("--proto", choices=["tcp", "mptcp"], default="tcp")
    srv.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Byte size for application recv buffer per connection.",
    )

    cli = sub.add_parser("client", help="Run as TCP/MPTCP client.")
    cli.add_argument("--host", required=True)
    cli.add_argument("--port", type=int, required=True)
    cli.add_argument("--bytes", type=int, required=True, dest="payload_bytes")
    cli.add_argument("--csv", type=Path, required=True)
    cli.add_argument("--proto", choices=["tcp", "mptcp"], default="tcp")
    cli.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Byte size for send loop chunks.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "server":
        rc = run_server(args.bind, args.port, args.backlog, args.proto, args.chunk_size)
    else:
        rc = run_client(
            args.host, args.port, args.payload_bytes, args.csv, args.proto, args.chunk_size
        )
    sys.exit(rc)


if __name__ == "__main__":
    main()
