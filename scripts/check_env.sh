#!/usr/bin/env bash
set -euo pipefail

ok=1

header() { echo "-- $1 --"; }
need() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    local v
    v=$({ "$cmd" --version 2>&1 || true; } | head -n1)
    echo "[OK] $cmd: ${v:-present}"
  else
    echo "[NG] $cmd: not found"
    ok=0
  fi
}

header "Binaries"
need ovs-vsctl
need ovs-vswitchd
need mn
need iperf3
need osken-manager
need python3

header "OVS status"
if ovs-vsctl --no-wait show >/dev/null 2>&1; then
  echo "[OK] ovs-vsctl show"
else
  echo "[NG] ovs-vsctl show failed (is OVS running?). Try: 'sudo service openvswitch-switch start'"
  ok=0
fi

if command -v lsmod >/dev/null 2>&1; then
  if lsmod | grep -q '^openvswitch\b'; then
    echo "[OK] kernel module openvswitch loaded"
  else
    echo "[WARN] kernel module openvswitch not listed by lsmod"
  fi
fi

header "Python modules"
if python3 - <<'PY'
import importlib, sys
try:
    importlib.import_module('os_ken')
    print('[OK] python3: os_ken importable')
except Exception as e:
    print('[NG] python3: os_ken import failed:', e)
    sys.exit(1)
PY
then
  :
else
  ok=0
fi

header "Permissions"
if [ "$(id -u)" != "0" ]; then
  echo "[WARN] Not running as root; Mininet may require root privileges."
fi

echo
if [ "$ok" -eq 1 ]; then
  echo "[t1-check] All checks passed."
  exit 0
else
  echo "[t1-check] Some checks failed. Install deps via:"
  echo "  sudo apt-get update && sudo apt-get install -y openvswitch-switch mininet python3-os-ken iperf3"
  echo "Then start OVS:"
  echo "  sudo service openvswitch-switch start"
  exit 1
fi
