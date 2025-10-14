#!/usr/bin/env bash
set -euo pipefail

# 1) OVSDB に接続できるか（軽量）
ovs-vsctl --timeout=2 show >/dev/null

# 2) vswitchd が制御ソケットに応答するか
ovs-appctl -t ovs-vswitchd version >/dev/null

# 3) データパス一覧が取得できるか（任意だが有用）
ovs-appctl -t ovs-vswitchd dpif/show >/dev/null

# すべて成功 → 0 で終了（healthy）
exit 0
