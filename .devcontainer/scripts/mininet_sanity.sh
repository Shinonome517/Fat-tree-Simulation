#!/usr/bin/env bash
set -euo pipefail

echo "[sanity] Checking Open vSwitch status..."
# entrypoint.sh が ovsdb-server/ovs-vswitchd を起動済みの想定
if ! ovs-vsctl show >/dev/null 2>&1; then
  echo "[sanity] ovs-vsctl cannot talk to ovsdb-server. Dumping logs if any..."
  [ -f /var/log/openvswitch/ovs-vswitchd.log ] && tail -n 200 /var/log/openvswitch/ovs-vswitchd.log || true
  exit 0  # DevContainer 起動は継続したいため hard fail しない
fi

# カーネルモジュールの有無をチェック（kernel datapath 利用可否）
DP="ovsl"
if grep -qw openvswitch /proc/modules; then
  DP="ovsk"
  echo "[sanity] Detected kernel module 'openvswitch' -> using kernel datapath (${DP})."
else
  echo "[sanity] Kernel module not found -> falling back to userspace datapath (${DP})."
  echo "[sanity] For accurate measurements, run on a Linux host with /lib/modules mounted & privileged."
fi

# Mininet の残骸掃除
mn -c || true

# ごく軽い疎通テスト（失敗しても起動は続行）
set +e
echo "[sanity] Running minimal Mininet test: pingall with --switch=${DP}"
mn --switch=${DP} --test pingall
RC=$?
set -e
echo "[sanity] pingall exit code: ${RC} (non-zero is tolerable at startup)"

echo "[sanity] Tips:"
echo "  - Run an explicit test when ready:"
echo "      mn --switch=${DP} --test iperf"
echo "  - Kernel datapath required for accurate throughput/fairness metrics."
