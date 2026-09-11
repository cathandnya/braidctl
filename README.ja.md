# braidctl

Linux の btrfs RAID1 に Drobo BeyondRAID 風のプール管理を載せるツール。

容量の異なるドライブを好きに束ね、「使える容量」を 1 つの数字で見て、ドライブが壊れたら
**交換ドライブを挿さなくても自動で冗長性が戻る**。

```
$ sudo braidctl status
braid  /srv/pool   PROTECTED (RAID1)
Raw    7.50 TB   Usable    3.75 TB   Used  412.31 GB (11%)   Free    3.34 TB

ID  device   model                       size     alloc  errors  health
 1  sdd      ThinkPlus                2.00 TB 206.16 GB       0  OK 40°C
 2  sde      ThinkPlus                2.00 TB 204.03 GB       0  OK 41°C
 3  sdf      WDC WD25EZRX-00MMMB0     2.50 TB 309.24 GB       0  OK 47°C
 4  sdg      WDC WD10EADS-00L5B1      1.00 TB 105.19 GB       0  WARN reallocated=1 44°C

Any single drive can be lost and re-protected (losing the largest, sdf 2.50 TB,
leaves 2.50 TB usable >= 412.31 GB used)
```

## なぜ作ったか

[Drobo](https://ja.wikipedia.org/wiki/Drobo) は 2023 年に清算され後継が無い。BeyondRAID が快適だった
理由は 3 つある。

1. **どんなドライブでも、どんな容量でも混ぜられる。** 余っていたドライブを足す。1 台抜く。揃える必要が無い
2. **数字が 1 つ。** 「4 台 7.5 TB を 2 重化して…」ではなく、*結局いくつ入るのか*
3. **交換用ドライブ無しで自己修復する。** 1 台失うと、**残ったドライブの間でデータを配置し直す**。
   入るだけの量しか入れていなければ、代わりのドライブを注文する前に冗長性が戻っている

btrfs は (1) と (3) の難しい部分を既に実装している。`raid1` プロファイルはディスク単位ではなく
1 GiB のチャンク単位でミラーするので、メンバーの容量はバラバラでよく、`device add` / `device remove`
がそのチャンクをオンラインで再配置する。足りないのは Drobo がファームウェアでやっていた周辺 —
容量の表示、故障の検知、そもそも再構築が*可能かどうか*の判断、そして実行 — だけ。それが `braidctl`。

## btrfs がやること / braidctl が足すこと

| | btrfs | braidctl |
|---|---|---|
| 容量混在・チャンク単位のミラー | ✅ | |
| オンラインでの `device add` / `device remove` | ✅ | |
| チェックサム、scrub、読み出し時の自己修復 | ✅ | |
| 「結局いくつ入るのか」 | `btrfs fi usage` を読めれば分かる | 1 行。加えて各ドライブの*耐性* |
| ドライブが消えたことに気づく | `dmesg` にエラーが出る | 定期的に見て、デバウンスして、知らせる |
| 故障後の再構築 | コマンドを自分で打つ | 容量が足りるなら自動 |
| 再構築が*不可能*だと判断する | — | そう言って、あと何 GB 必要か教える |
| 起動時に縮退したプールをマウント | mount が失敗するだけ | `-o degraded` にフォールバックして通知 |
| 間違ったディスクを食わない | `device add` はシステムディスクでも喜んで受け取る | 多重の安全装置 |

## 使える容量

すべてを 2 重に持つので、あるドライブ群の理想的な使える容量は

```
usable = min( 合計 / 2 ,  合計 − 最大ドライブ )
```

見落としやすいのは後半の項。1 TB × 3 台の中に 4 TB を 1 台混ぜても、その 4 TB は半分しか使えない。
もう一方のコピーを置ける相手がいないからだ。`braidctl status` が表示するのはこの理想値ではなく
btrfs の実測値だが、この式は本当に知りたいこと — *このドライブが死んだとき、残りにデータが収まるか* —
をプール内の全ドライブについて答えるのに使っている。それが `status` の最終行であり、
緑ランプと赤ランプの違いそのもの。

## ドライブが壊れたとき

```
ドライブが消える
  └─ 「脱落。N 秒待つ」を通知                              (初回検知時刻を記録)
     ├─ debounce_sec (既定 600) 以内に戻った
     │    └─ 再マウントで組み戻し、scrub で取りこぼした差分を同期
     └─ 戻らない
          ├─ 残りのドライブにデータが収まる
          │    └─ compaction balance → btrfs device remove <devid>
          │       (カーネルがライブで外せない場合: Samba を止めて -o degraded で再マウント →
          │        remove missing → Samba 再開。数十秒の停止)
          │    └─ 「再冗長化完了」を通知。少ない台数で再び PROTECTED
          └─ 収まらない
               └─ 「あと X GB 分のドライブを追加」を毎日通知。Drobo の赤ランプ。
                  データは読み書きできる。冗長でなくなっただけ
```

起動時に 1 台欠けていた場合、`fstab` の通常マウントは（`nofail` のおかげで無害に）失敗し、
3 分後に monitor が `-o degraded` でマウントして、あとは同じ流れに入る。

死んではいないが死にかけているドライブ — SMART の失敗、保留セクタ、増え続ける btrfs エラーカウンタ —
は通知するだけで何もしない。まだ読めるドライブを抜く方が普通は事態を悪くするからだ。
納得できなければ `auto_evict_on_errors = true` にする。

## インストール

必要なもの: systemd のある Linux、btrfs-progs、Python 3.11 以上 (`tomllib` のため)、
健康状態の表示に `smartctl`。Python パッケージは不要 — 標準ライブラリのみの単一ファイル。

```bash
git clone https://github.com/cathandnya/braidctl
sudo braidctl/install.sh        # /usr/local/sbin/braidctl, /etc/braid/braid.toml, systemd timer
```

プールが無ければ作る。**ドライブの中身は消える**。

```bash
sudo mkfs.btrfs -L braid -d raid1 -m raid1 /dev/sdX /dev/sdY /dev/sdZ
sudo mkdir -p /srv/pool && sudo chattr +i /srv/pool   # 後述の「安全装置」
sudo systemctl start braid-mount.service              # マウント。起動時は install.sh が有効化済み
sudo braidctl status
```

**プールを `/etc/fstab` に書いてはいけない。** systemd は fstab のマウントを
**特定の 1 台**に紐付け、そのデバイスが消えた瞬間にファイルシステムをアンマウントする。
冗長なマルチデバイスプールにとって、これはドライブを失ったときの対応として完全に逆であり、
無害な USB バスの再列挙でも発動する。代わりに `braid-mount.service` がマウントすることで、
マウントユニットにデバイス依存が一切付かなくなる。根拠と実測は
[docs/design.md](docs/design.md)。

ファイルシステムのラベル、設定ディレクトリ、systemd ユニットはすべて `braid` — プールの名前。
`braidctl` はそれを管理するコマンド。マウントポイント・ラベル・再構築中に止めるサービスが
既定と違うなら `/etc/braid/braid.toml` を編集する。

## コマンド

```bash
sudo braidctl status              # 容量、保護状態、各ドライブの健康
sudo braidctl status --json       # 同じ内容を機械可読で
sudo braidctl add /dev/sdX        # 追加 (中身があれば --force が要る) して balance
sudo braidctl remove 3            # devid 3 を外す。残りに収まる場合のみ
sudo braidctl remove /dev/sdg     # パス指定でも可
sudo braidctl remove missing      # 死んだドライブを手で外す (monitor が自動でやるのと同じ)
sudo braidctl monitor             # 検知を 1 回。timer から 5 分毎に呼ばれる
sudo braidctl scrub               # 全ブロックをチェックサムと照合
sudo braidctl mount               # プールをマウント (--wait N でドライブ出現を待つ)
sudo braidctl mount-degraded      # 1 台欠けた状態で手動マウント
```

つまり故障ドライブの交換は「何もしない（monitor が外して再冗長化する）」→「新しいドライブが
届いたら `braidctl add`」の 2 手。冷や汗をかきながら再構築を見守る時間は無い。既に終わっているから。

通知はメールで届く。既定では
nas-monitor 形式の既存設定から SMTP ブロックを借りる。
`notification.email` に `{smtp_server, smtp_port, username, password, from, to, use_tls}` を持つ
JSON なら何でも `notify.config_file` に指定できる。設定が無くても全部ジャーナルには残る —
`journalctl -u braid-monitor`。

## 安全装置

デバイス名の打ち間違いでアレイを壊すのはこの種のツールの現実的なリスクなので:

- **`add` が拒否するもの**: システムディスク、マウント中のもの、holder があるもの (LUKS, LVM, MD)、
  `protected_devices` に一致するもの、そして `--force` 無しではファイルシステム署名や
  パーティションを持つもの
- **`remove` が拒否する条件**: 残りのドライブに収まらないとき。あと何 GB 必要かを表示する
- **戻ってきたドライブを黙って組み込まない。** まだプールのメンバーなら組み戻して再同期する。
  既に外した後のものなら `add --force` を勧める通知を出すだけで、何も触らない
- **マウントポイントは immutable。** `chattr +i /srv/pool` により、プールが*マウントされていない*とき
  そのパスへの書き込みは失敗する。黙ってルートファイルシステムを埋め尽くす — NAS のデータを
  `/` の枯渇で失う古典的な失敗 — を防ぐ
- **変更は一度に 1 つ。** `monitor` はロックを取る。balance 中は `add`/`remove` を拒否する
- **再構築はデバウンスする。** 数秒で復帰する USB の瞬断で再配置を始めてはいけない
- **udev がメンバーの再出現を監視する。** バス再列挙の後、全ドライブは*新しい*デバイスノードで
  戻ってくる。btrfs はマウント済みファイルシステムに新ノードを差し替えないので、マウントは
  死んだデバイスを指したまま**書き込みを黙って捨てる**（[docs/design.md](docs/design.md)）。
  この窓を timer の 5 分から 1 秒未満に縮める

## 制約

- **`raid1` は「2 コピー」であって「2 台までの故障に耐える」ではない。** 1 台は死んでよい。2 台は駄目。
  注意: USB 筐体には 2 台のディスクを 1 デバイスとして見せるものがあり、その筐体が壊れると 2 台同時に
  失う。btrfs はそれらが関連していたことを知らない。筐体を分けるか、3 コピーにする:
  `btrfs balance start -dconvert=raid1c3 -mconvert=raid1c3 /srv/pool`
  （使える容量は実容量の約 1/3 になる）。`braidctl` は `raid1c3` のプールも表示・再構築できる
- `raid5`/`raid6` は意図的に非対応。btrfs のパリティ RAID には依然として write hole がある
- 縮退再マウントのフォールバックが必要になった再構築では、ファイル共有が数十秒止まる
- 再構築と balance は I/O を食う。USB では実行中すべてが遅くなる
- 使用率 90% を超えると `balance` と `device remove` が `ENOSPC` で失敗し始める。`status` が警告する
- 再構築が成功した後も、次に再マウントするまで `degraded` マウントオプションが残ることがある。無害
- 容量は 10 進表記 (1 TB = 10¹² バイト)。ドライブのラベルと同じ、Drobo と同じ。`df` は TiB なので
  常に小さく見える

## 状態

1 台のマシンで開発・検証している — Ubuntu 26.04、Samba と Time Machine を提供する USB ドライブ
4 台 (1.8/1.8/2.3/0.9 TB)。ドライブ脱落、自動再構築、再追加、scrub、容量不足による拒否は実機と
ループデバイスで確認済み。[docs/testing.md](docs/testing.md) に再現手順がある。データを預ける前に
自分で確かめてほしい。これはディスクに対して鋭いことをする小さなツールだ。バックアップを取ること。

btrfs RAID1 を選んだ理由は [docs/design.md](docs/design.md)、日常の運用は
[docs/operations.md](docs/operations.md)。

## ライセンス

MIT
