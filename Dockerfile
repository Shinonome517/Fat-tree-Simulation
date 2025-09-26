# Ubuntu 24.04 (noble) ベース
FROM ubuntu:24.04

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
    iproute2 iputils-ping ethtool net-tools tcpdump iperf3 \
    python3 python3-pip \
    git ca-certificates curl \
    build-essential cmake pkg-config ninja-build \
    libssl-dev \
    zsh \
    # あると便利な診断
    procps less vim \
 && rm -rf /var/lib/apt/lists/*

# OVS/Mininet 用の sysctl（実行時に適用）
RUN printf 'net.ipv4.ip_forward=1\nnet.core.rmem_max=268435456\nnet.core.wmem_max=268435456\n' \
    > /etc/sysctl.d/99-mininet.conf

# picoquic を取得してビルド（Picotls を自動取得）
RUN git clone --depth=1 https://github.com/private-octopus/picoquic.git "$PICOQUIC_HOME" \
 && cmake -S "$PICOQUIC_HOME" -B "$PICOQUIC_HOME/build" \
      -DPICOQUIC_FETCH_PTLS=Y -DCMAKE_BUILD_TYPE=Release -G Ninja \
 && cmake --build "$PICOQUIC_HOME/build" -j"$(nproc)"

# よく使うバイナリにアクセスしやすいように symlink（install ターゲット非依存）
RUN ln -sf "$PICOQUIC_HOME/build/picoquicdemo" /usr/local/bin/picoquicdemo \
 && ln -sf "$PICOQUIC_HOME/build/picoquic_ct"  /usr/local/bin/picoquic_ct

# エントリポイント：sysctl適用 & OVSデーモン起動（systemdなし運用）
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /root
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/zsh"]
