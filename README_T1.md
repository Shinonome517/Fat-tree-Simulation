# T1: k=4 Fat-Tree + ECMP（Mininet / OVS / OS-Ken）

本ドキュメントは、k=4 の Fat-Tree トポロジを Mininet 上に構築し、Open vSwitch(OVS) と OS-Ken(OpenFlow 1.3) を用いて ECMP（Group SELECT）を有効化、ping/iperf3 による疎通・帯域確認と OVS 証跡の採取を行う最小手順をまとめたものです。

---

## 目次
1. 前提
2. トポロジとアドレス体系
3. ECMP（OS-Ken アプリ）
4. クイックスタート
5. 生成物（ファイル）
6. 受入基準（T1 Done 条件）
7. ヒント
8. ofproto/trace 例
9. トラブルシュート

---

## 1. 前提
- OS: Ubuntu 24.04 互換
- Python 3.10+
- Mininet、OVS 3.3.0（kernel datapath）、OS-Ken controller
- ツール: `mn`, `ovs-vsctl`, `ovs-ofctl`, `ovs-appctl`, `ip`, `tcpdump`, `iperf3`, `ping`

依存のインストール（再現性重視: APT 推奨）
- 推奨: `sudo apt-get update && sudo apt-get install -y openvswitch-switch mininet python3-os-ken iperf3`
- 参考（任意）: 仮想環境で `pip install os-ken` も可。その場合は `OSKEN_CMD="python -m os_ken.cmd.manager"` を設定してください（後方互換として `RYU_CMD` でも可）。

---

## 2. トポロジとアドレス体系
- 構成: pods=4、core=4、agg=8、edge=8（計20台）、hosts=16
- 命名: `br_c_i_j`, `br_a_p_i`, `br_e_p_i`、ホストは `h_p_i_h`
- サブネット: S(p,i) = `10.p.i.0/24`
- ホスト IP: `10.p.i.(h+1)`（h=1→.2、h=2→.3）
- ポート割当（決め打ち）:
  - edge: 1/2=ホスト、3/4=上流(agg)
  - agg: 1/2=edge 下流、3/4=上流(core)
  - core: 1..4=各 pod へ上流

---

## 3. ECMP（OS-Ken アプリ）
- OpenFlow 1.3 固定、IPv4 を前広でフロー投入
- ECMP は edge/agg の上流ポート集合に対して Group `type=SELECT` を作成
- OVS の拡張を用い、可能なら `selection_method=hash` と `fields=ip_src,ip_dst,ip_proto,tp_src,tp_dst` を `ovs-ofctl mod-group` で適用（失敗時はフォールバック）

---

## 4. クイックスタート

1) クリーンアップ
```bash
make t1-clean
```

2) ECMP 有効で起動・疎通/帯域テスト・証跡採取
```bash
make t1-ecmp
```

3) ECMP 無効（単一路）で起動して基本確認
```bash
make t1-up
```

4) 直近ログで証跡のみ再取得
```bash
make t1-evidence
```

出力は `logs/<timestamp>/` 以下に保存されます（`evidence/` およびコントローラログを含む）。

---

## 5. 生成物（ファイル）
- `topo/fattree.py` — Mininet トポロジ (`FatTreeTopo(k=4, bw_edge, bw_agg, bw_core, delay, loss, max_queue)`)
- `controller/ryu_ecmp.py` — ECMP（Group SELECT）を設定する OS-Ken アプリ
- `controller/ryu_l2.py` — ECMP 無効（単一路）の静的 IPv4（OS-Ken）アプリ
- `scripts/run_fattree.py` — 起動・テスト・証跡採取・後片付けを一括
- `scripts/evidence.py` — `ip route`, `dump-groups/stats`, `ofproto/trace` の要約を保存
- `scripts/diags.py` — 失敗時の自己診断ダンプ（OVS/フロー/ポート等）
- `Makefile` — `t1-up`, `t1-ecmp`, `t1-test`, `t1-evidence`, `t1-clean`

---

## 6. 受入基準（T1 Done 条件）
1. `logs/.../evidence/` に以下が存在:
   - `ip_route.txt`, `dump_groups.txt`, `dump_group_stats.txt`, `trace_summary.tsv`
   - `trace_summary.tsv` に `selected bucket` の多様性が確認できること（ECMP 証跡）
2. 代表ペアで ping/iperf3 が成功し、iperf3 の JSON 出力が保存されていること
3. 異常終了時、`logs/.../diags/` が生成され、エラーメッセージに `[T1]` と `python3 scripts/diags.py --now` の提案が含まれること

---

## 7. ヒント
- コントローラは L2 学習ではなく、IPv4 フローを前広に投入してブロードキャストを最小化します。
- `selection_method=hash` の適用はベストエフォートです。未対応環境でも ECMP 自体は動作しますが、ハッシュキーが既定と異なる可能性があります。
- `scripts/evidence.py` は `ofproto/trace` のサマリを TSV で保存し、バケット選択の多様性を可視化します。

---

## 8. ofproto/trace 例
エッジスイッチでの例:
```bash
ovs-appctl ofproto/trace br_e_0_0 \
  in_port=1,tcp,nw_src=10.0.0.1,nw_dst=10.1.0.2,tp_src=5001,tp_dst=5201
```

---

## 9. トラブルシュート
- まずクリーン: `make t1-clean`
- コントローラログ: `logs/<ts>/osken_ecmp.log`
- 詳細診断の即時採取: `python3 scripts/diags.py --now`
- ブリッジ/グループ確認: `ovs-vsctl list-br`, `ovs-ofctl -O OpenFlow13 dump-groups br_e_0_0`
