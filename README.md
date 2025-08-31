# Fat-Tree　Simulation Docker イメージ（Mininet + OVS(kernel datapath) + picoquic）

このリポジトリはUbuntu 24.04 ホスト上で、**kernel datapath の Open vSwitch (ovsk)** を利用して Mininet と picoquic を動かすための Docker イメージと Fat-Tree に Multipath quic を適用するエミュレート用のスクリプトを提供します。  

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
docker run --name your-container-name \
  --ulimit memlock=-1 \
  --privileged --network=host \
  -v /lib/modules:/lib/modules \
  -v "$PWD:/workspace" -w /workspace \
  -it mpquic-lab
```

- --privileged：にコンテナへカーネル機能へのアクセス権を付与（OVS用）
- --network=host：ホストとネットワークを共有
- -v /lib/modules:/lib/modules：カーネルモジュールを共有
- -v -v "$PWD:/workspace"：ホスト側カレントディレクトリを

---

## 4. コンテナ内での確認コマンド

### OVS

OVSの動作確認

```sh
ovs-vsctl show
ovs-dpctl show
ps aux | grep -E 'ovsdb-server|ovs-vswitchd'
```

### Mininet

Mininetの動作確認

```sh
sudo mn --test pingall
```

### picoquic

picoquicが正しくビルドされているかの確認

```sh
which picoquicdemo
picoquicdemo -h
```

---