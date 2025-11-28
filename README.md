# Fat-Tree Simulation Docker イメージ（Mininet + picoquic）

このリポジトリはUbuntu 22.04 ホスト上で Mininet と picoquic を動かすための Docker イメージと、Fat-Tree (デフォルト k=4、偶数 k<=16 に対応) をエミュレートするためのスクリプトを提供します。  

コンテナはホストのカーネルモジュールを共有する構成で起動する必要があり、`--privileged` / `--network=host` / `-v /lib/modules:/lib/modules` を付与するのが必須です。

---

## 目次
1. 前提（ホスト側）
2. ビルド手順
3. コンテナ起動手順（推奨コマンド）
4. コンテナ内の初期化
5. Fat-Tree の起動方法
6. コンテナ内での確認コマンド（Mininet / picoquic）
7. テスト（pytest）

---

## 1. 前提（ホスト側）

- ホスト OS: **Ubuntu 22.04**
- Docker がインストール済み
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

- --privileged：コンテナへカーネル機能へのアクセス権を付与
- --network=host：ホストとネットワークを共有
- -v /lib/modules:/lib/modules：カーネルモジュールを共有
- -v "$PWD:/workspace"：ホスト側カレントディレクトリをコンテナの`/workspace`として利用

---

## 4. コンテナ内の初期化

コンテナに入った直後に、以下を実行して初期化してください（devcontainer では自動実行されるため不要です）。

```bash
sudo /usr/local/bin/setup.sh
```

---

## 5. Fat-Tree の起動方法

CLIに入って操作する場合の例:

```bash
sudo python3 main.py --cli
```

主なオプションと既定値:
- `--bw` (デフォルト: 1000 Mbps)
- `--delay` (デフォルト: 0.2ms)
- `--q` (デフォルト: 150 パケット)
- `-k`/`--k` (デフォルト: 4; 偶数かつ 2〜16 の範囲で Fat-Tree の k を指定)
- `--cli` を付けない場合はヘッドレスで常駐

停止は `Ctrl+C`。

---

## 6. コンテナ内での確認コマンド

### Mininet

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

## 7. テスト（pytest）

ルーティング・ECMP・アドレス割り当てを検証するpytestスイートがあります（root必須）。`iperf3` を使うテストも含まれるため、同梱のバイナリをそのまま利用できます。

```bash
sudo pytest -q
```

- Fat-Tree の k を変える場合: `FATTREE_K=8 sudo pytest -q` のように環境変数で指定（偶数 2〜16）。
- `@pytest.mark.slow` は長時間/トラフィック多めのテスト（例: 全ホスト疎通、ECMP 負荷分散）。時間を節約する場合は `sudo pytest -q -m "not slow"` で除外できます。
