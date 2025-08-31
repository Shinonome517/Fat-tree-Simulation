#!/usr/bin/env bash
set -euo pipefail

# sysctl を適用（--privileged 時に有効、失敗しても続行）
sysctl --system || true

# OVS DB の初期化
mkdir -p /var/run/openvswitch /var/lib/openvswitch
if [ ! -f /var/lib/openvswitch/conf.db ]; then
  ovsdb-tool create /var/lib/openvswitch/conf.db /usr/share/openvswitch/vswitch.ovsschema
fi

# カーネルモジュール（ホスト側に依存、失敗しても続行）
modprobe openvswitch || true

# デーモン起動
ovsdb-server --remote=punix:/var/run/openvswitch/db.sock \
             --remote=db:Open_vSwitch,Open_vSwitch,manager_options \
             --pidfile --detach
ovs-vsctl --no-wait init || true
ovs-vswitchd --pidfile --detach

echo "[entrypoint] Open vSwitch is up."
exec "$@"
