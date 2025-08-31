#!/usr/bin/env bash
set -euo pipefail

echo "[postCreate] Verifying toolchain..."
command -v ovs-vswitchd >/dev/null && ovs-vswitchd --version || true
command -v ovs-vsctl    >/dev/null && ovs-vsctl --version || true
command -v mn           >/dev/null && mn --version || true
command -v iperf3       >/dev/null && iperf3 --version || true
command -v picoquicdemo >/dev/null && picoquicdemo -h | head -n 1 || true

echo "[postCreate] Done."
