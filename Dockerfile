# Ubuntu 22.04 (jammy) ベース
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC \
    PICOQUIC_HOME=/opt/picoquic \
    PATH=/opt/picoquic/build:$PATH

# サービス自動起動を抑止（コンテナ内での apt postinst を安全に）
RUN printf '#!/bin/sh\nexit 0\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d

# 必要パッケージの導入（Mininet/OVS + ビルド系 + 計測の基本ツール）
RUN apt-get update && apt-get install -y --no-install-recommends \
    mininet \
    openvswitch-switch openvswitch-common \
    iproute2 iputils-ping iputils-tracepath ethtool net-tools tcpdump iperf3 \
    conntrack \
    traceroute mtr-tiny \
    python3 python3-pip python3-pytest \
    git ca-certificates curl \
    build-essential cmake pkg-config ninja-build \
    libssl-dev openssl \
    zsh \
    nftables \
    # あると便利な診断
    procps less vim \
 && rm -rf /var/lib/apt/lists/*

# 解析用の Python ライブラリ
RUN python3 -m pip install --no-cache-dir numpy pandas matplotlib networkx

# OVS/Mininet 用の sysctl（実行時に適用）
RUN printf 'net.ipv4.ip_forward=1\nnet.core.rmem_max=268435456\nnet.core.wmem_max=268435456\n' \
    > /etc/sysctl.d/99-mininet.conf

# 固定の自己署名証明書（picoquicdemo サーバ用）
RUN mkdir -p /etc/picoquic && \
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /etc/picoquic/server-key.pem \
      -out /etc/picoquic/server-cert.pem \
      -days 3650 \
      -subj "/CN=test/"

# ---- picoquic を最新固定（2025-11-03 時点の master HEAD）+ サブモジュール shallow 取得 ----
ARG PICOQUIC_COMMIT=73231489b616e61bef3733cc6b5953c2b91d5348
ARG PICOQUIC_HOME=/opt/picoquic

# 拡張フラグ付きデモ（-F, -G など）を有効化するためのビルド引数。
# 既定では「デモの拡張を全開にする」ことを意図した C 定義群を付与します。
# もし将来のコミットで不要/名称変更になっても副作用はありません（未使用マクロの定義は無害）。
ARG PICOQUIC_DEMO_CDEFS="-DPICOQUIC_DEMO_FULL -DPICOQUIC_DEMO_EXPERIMENTAL -DPICOQUIC_VNEG_GREASE -DPICOQUIC_ENABLE_GREASE"
# qlog など可視化も有効化（未使用なら無害）
ARG PICOQUIC_ENABLE_QLOG=ON

# 特定コミットを shallow で取得し、サブモジュールも shallow 更新
RUN git init "$PICOQUIC_HOME" \
 && git -C "$PICOQUIC_HOME" remote add origin https://github.com/private-octopus/picoquic.git \
 && git -C "$PICOQUIC_HOME" fetch --depth=1 origin $PICOQUIC_COMMIT \
 && git -C "$PICOQUIC_HOME" checkout --detach FETCH_HEAD \
 && git -C "$PICOQUIC_HOME" submodule update --init --recursive --depth=1 --jobs 4 \
 # CMake 構成（失敗時にログをダンプ）
 && cmake -S "$PICOQUIC_HOME" -B "$PICOQUIC_HOME/build" \
      -DPICOQUIC_FETCH_PTLS=Y \
      -DPICOQUIC_ENABLE_QLOG=${PICOQUIC_ENABLE_QLOG} \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo -G Ninja \
      -DCMAKE_C_FLAGS="${PICOQUIC_DEMO_CDEFS}" \
  || (echo '---- CMakeError.log ----' \
      && cat "$PICOQUIC_HOME/build/CMakeFiles/CMakeError.log" || true \
      && echo '---- CMakeOutput.log ----' \
      && cat "$PICOQUIC_HOME/build/CMakeFiles/CMakeOutput.log" || true \
      && false) \
 && cmake --build "$PICOQUIC_HOME/build" -j"$(nproc)"

# よく使うバイナリにアクセスしやすいように symlink（install ターゲット非依存）
RUN ln -sf "$PICOQUIC_HOME/build/picoquicdemo" /usr/local/bin/picoquicdemo \
 && ln -sf "$PICOQUIC_HOME/build/picoquic_ct"  /usr/local/bin/picoquic_ct

# セットアップスクリプト（コンテナ起動後に devcontainer が実行）
COPY setup.sh /usr/local/bin/setup.sh
RUN chmod +x /usr/local/bin/setup.sh

WORKDIR /root
CMD ["/bin/zsh"]
