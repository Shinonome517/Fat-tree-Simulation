#!/usr/bin/env bash

set -euo pipefail

# increase memlock limit in this shell so ovs-vswitchd inherits it
ulimit -l unlimited || true

# sysctl を適用（--privileged 時に有効、失敗しても続行）
sysctl --system || true

# OVS DB の初期化
mkdir -p /var/run/openvswitch /var/lib/openvswitch
if [ ! -f /var/lib/openvswitch/conf.db ]; then
  ovsdb-tool create /var/lib/openvswitch/conf.db /usr/share/openvswitch/vswitch.ovsschema
fi

# カーネルモジュール（ホスト側に依存、失敗しても続行）
modprobe openvswitch || true

# ovsdb-server を起動（既に起動済みならスキップ）
if ! pgrep -x ovsdb-server >/dev/null 2>&1; then
  ovsdb-server --remote=punix:/var/run/openvswitch/db.sock \
               --remote=db:Open_vSwitch,Open_vSwitch,manager_options \
               --pidfile --detach
else
  echo "[setup] ovsdb-server is already running."
fi

# DB 初期化（再実行しても安全）
ovs-vsctl --no-wait init || true

# ovs-vswitchd を起動（既に起動済みならスキップ）
if ! pgrep -x ovs-vswitchd >/dev/null 2>&1; then
  ovs-vswitchd --pidfile --detach
else
  echo "[setup] ovs-vswitchd is already running."
fi

echo "[setup] Open vSwitch is ready."
