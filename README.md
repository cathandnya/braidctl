# braid — btrfs raid1 の上に Drobo BeyondRAID 風の運用層を載せる

Drobo の BeyondRAID がやっていたこと:

1. 容量の異なるドライブを束ねて 1 つのプールにする
2. 実容量 / 使える容量 / 使用量 を表示する
3. ドライブが抜けたり壊れたりしたら、残りの「使える容量」に収まる限り自動で冗長性を回復する
4. ドライブを追加・削除したら適切に再配置する

1 と 4、および 3 の「仕組み」は btrfs の raid1 プロファイルが既に持っている（チャンク単位の 2 重化、
容量混在、オンラインでの device add/remove）。このリポジトリの `braidctl` は、Drobo が
ファームウェアでやっていた残りの部分 — 表示・検知・自動復旧・安全装置 — を担当する。

```
/dev/sdd /dev/sde /dev/sdf /dev/sdg      容量はバラバラでよい
        └ btrfs LABEL=braid -d raid1 -m raid1  →  /srv/pool
                ├ share/          Samba [pool]
                └ timemachine/    Samba [timemachine]

braidctl  ─ status   実容量 / 使える容量 / 使用量 / 保護状態 / 各ドライブ (SMART・エラー)
          ─ add      空ドライブ検査 → device add → balance
          ─ remove   外した後も収まるか試算 → device remove
          ─ monitor  5 分毎 (timer)。脱落・故障の検知 → 猶予 → 自動再冗長化 or 通知
          ─ scrub    月次 (timer)。全データをチェックサムで検証・修復
```

## 使える容量の考え方

raid1 は全チャンクを 2 台に置くので、使える容量は理想的には

    usable = min( 合計 / 2 ,  合計 − 最大ドライブ )

例: 1.82 + 1.82 + 2.27 + 0.91 = 6.82 TB → usable ≈ 3.4 TB。
`status` の「使える容量」は理想値ではなく btrfs の実測 (`Free (estimated)` + 使用量) を使う。
「あるドライブを失ったときに再冗長化できるか」は、残りのドライブで上式を計算し
`使用量 <= usable_after × rebuild_margin (0.95)` で判定する。Drobo の緑/黄/赤ランプに相当。

## 自動再冗長化の流れ (monitor)

```
ドライブが消えた
  → 即通知「脱落を検知。N 秒待つ」          (state.json に初回検知時刻)
  → debounce_sec (既定 600 秒) の間に戻ったら
        再マウントで組み戻し + scrub で差分同期   ← USB 瞬断・抜き差し
  → 戻らなければ usable_after を計算
        収まる  → compaction balance → btrfs device remove <devid>
                   (ライブで外せなければ smbd を止めて degraded 再マウント → remove missing)
                 → 完了通知。プールは残りのドライブで再び PROTECTED
        収まらない → 「あと X GB 分のドライブを追加してください」を通知 (1 日 1 回リマインド)
                     データは読み書きできるが、もう 1 台失うと消える
起動時に 1 台欠けていた
  → fstab の通常マウントは失敗する (nofail で起動は続く)
  → monitor (OnBootSec=3min) が degraded でマウントして通知 → 以降は上と同じ
```

生きているがエラーが増えているドライブは既定では通知のみ (`auto_evict_on_errors = true` で自動退避)。

## 安全装置

- `/srv/pool` は `chattr +i`。未マウント時に Samba/Time Machine がルート FS へ書き込むのを防ぐ
- `add` はシステムディスク・マウント中・LUKS 等の holder 付き・`protected_devices` を常に拒否。
  署名やパーティションがあるドライブは `--force` が無ければ拒否
- `remove` は外した後の容量を試算し、収まらなければ拒否
- 一度外し終えた古いメンバーを挿すと「古いメンバーです。add --force で再利用」と通知するだけで、勝手には触らない
- balance 実行中は add/remove/再構築を始めない。monitor は flock で多重実行しない

## インストール

```bash
sudo ~/rc/braid/install.sh          # /usr/local/sbin/braidctl, /etc/braid/braid.toml, timer 有効化
sudo braidctl status
```

初回構築 (既存 mergerfs プールの解体を含む) は `rebuild-pool.sh`。データは消える。

## 日常の操作

```bash
sudo braidctl status                 # 状態
sudo braidctl add /dev/sdX           # 追加 (空ドライブ)。中身があれば --force
sudo braidctl remove 3               # devid 3 を外す (容量が足りるときだけ)
sudo braidctl remove /dev/sdg        # パスでも可
sudo braidctl remove missing         # 欠けたドライブを手動で外す (monitor が自動でやるのと同じ)
sudo braidctl scrub                  # 手動 scrub
sudo braidctl mount-degraded         # 1 台欠けた状態で手動マウント
journalctl -u braid-monitor -n 50    # monitor のログ
```

故障ドライブの交換 = 「monitor が自動で外す (or `remove`)」→「新しいドライブを `add`」の 2 手。
Drobo と同じく、交換前に残りの容量で冗長性が回復しているので急ぐ必要はない。

## 検証方法

ループデバイスで一通り再現できる (`~/.cache/braid-test` に 10G/10G/15G/5G の疎ファイル):

```bash
sudo losetup -f a.img ... ; sudo mkfs.btrfs -L braidtest -d raid1 -m raid1 /dev/loop3..6
# test.toml: mountpoint/label/state_file を試験用に、debounce_sec=5, samba_units=[], notify.config_file を存在しないパスに
sudo umount mnt; sudo losetup -d /dev/loop6            # 脱落を模擬
sudo braidctl -c test.toml monitor                     # degraded マウント + 検知
sudo braidctl -c test.toml monitor --debounce 0        # 自動再冗長化 or 「追加が必要」
sudo losetup -f d.img; sudo udevadm trigger /dev/loop6 # 復帰を模擬 → 組み戻し + scrub
```

実機 (USB) では `echo 1 | sudo tee /sys/block/sdX/device/delete` で 1 台だけ抜線と同じ状態にできる
(`/sys/bus/usb/drivers/usb/unbind` は同じ筐体の 2 台がまとめて消えるので使わない)。
復帰は `echo '- - -' | sudo tee /sys/class/scsi_host/hostN/scan`。

## 制約・注意

- **2 重化 (raid1) なので同時 2 台故障には耐えない。** galbo の 4 台は 2 台ずつ同じ USB 筐体
  (ThinkPlus ×2 = 1 台の USB デバイス、WDC ×2 = 1 台の USB デバイス) に入っており、
  **筐体 1 つの故障・抜線 = 2 台同時喪失** になる。btrfs は筐体を区別してコピーを置かないので、
  この構成では筐体障害でデータを失いうる。対策は raid1c3 (3 重化、効率 33%:
  `btrfs balance start -dconvert=raid1c3 -mconvert=raid1c3 /srv/pool`) か、筐体を分けること
- 自動再冗長化の途中で degraded 再マウントが必要になった場合、その間 (数十秒) smbd を止める
- 再構築 (device remove) と balance は USB バス帯域を使うので、その間 Time Machine は遅くなる
- 再冗長化後もマウントオプションに `degraded` が残ることがある。無害で、次の再マウント/再起動で消える
- 容量が 90% を超えると balance/remove が ENOSPC で失敗しやすい。`status` が警告する
- Drobo と同じく、表示は 10 進 (1 TB = 10^12 B)。`df` の TiB より数字が大きく見える
