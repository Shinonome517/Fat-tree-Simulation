# Fat-tree-Simulation Docker イメージ（Mininet + OVS(kernel datapath) + picoquic）

このリポジトリはUbuntu 24.04 ホスト上で、**kernel datapath の Open vSwitch (ovsk)** を利用して Mininet と picoquic を動かすための Docker イメージを提供します。  

コンテナはホストのカーネルモジュールを共有する構成で起動する必要があり、`--privileged` / `--network=host` / `-v /lib/modules:/lib/modules` を付与するのが必須です。

---

## 目次
1. 前提（ホスト側）
2. ビルド手順
3. コンテナ起動手順（推奨コマンド）
4. コンテナ内での確認コマンド（OVS / Mininet / picoquic）

---

## 1. 前提（ホスト側）

- ホスト OS: **Ubuntu 24.04**
- Docker がインストール済み
- ホストで `openvswitch` カーネルモジュールが使えること
  - `sudo modprobe openvswitch` でロード可能か確認
- Docker を実行するユーザが `sudo` 権限を持っていること

---

## 2. Docker イメージのビルド

```bash
docker build -t mpquic-lab .
```

---

## 3. コンテナ起動（推奨）

```bash
docker run --privileged --network=host \
  -v /lib/modules:/lib/modules \
  -v "$(pwd)/fat_tree.py":/root/fat_tree.py \
  -it mpquic-lab
```

--privileged：OVS 用
--network=host：ホストとネットワークを共有
-v /lib/modules:/lib/modules：カーネルモジュールを共有
-v "$(pwd)/fat_tree.py":/root/fat_tree.py：Fat-Tree スクリプトをマウント

---

## 4. コンテナ内での確認コマンド

### OVS

```sh
ovs-vsctl show
ovs-dpctl show
ps aux | grep -E 'ovsdb-server|ovs-vswitchd'
```

### Mininet

```sh
sudo mn --test pingall
```

### picoquic

```sh
which picoquicdemo
picoquicdemo -h
```