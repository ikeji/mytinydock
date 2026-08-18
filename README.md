# pydock

X11 の画面端に常駐する gkrellm 風ステータスモニタ。wmdock 風の幅 64px の縦長パネルで、
時計 (ISO 週付き)・月齢・xmonad ワークスペース・CPU (使用率/クロック/Top プロセス)・
メモリ・ネットワーク・Wi-Fi・Ping・Tailscale・グローバル IP・ディスク I/O・
ファイルシステム・温度・ファン・バッテリー・音量・メディアプレイヤー・IME・
APT 更新数を表示する。

1 ファイル (`dock.py`) だけで完結していて、カスタマイズはソースを直接編集する方式。

## 必要なもの

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 x11-utils
```

- python3-gi + GTK3 (PyGObject)
- x11-utils (`xprop`): 画面端の領域予約 (`_NET_WM_STRUT`) に使用。無くても動くが、
  他のウィンドウが最大化時に重なるようになる
- 情報源は /proc と /sys のみ。psutil などの追加 Python パッケージは不要

## 実行

```sh
./dock.py
```

- 右クリック → 「終了」で終了(Ctrl-C でも可)
- 音量パネル: [-][M][+] ボタン、バーをクリックでその位置の音量に設定、
  ホイールで上下、それ以外の場所のクリックでミュート切替
- メディアパネル: クリックで再生/一時停止
- IME パネル: クリックでオンオフ切替 (fcitx のみ)
- X セッション開始時に自動起動したい場合は `~/.xsessionrc` や WM の autostart に
  `~/work/desktop/dock/dock.py &` を追加する

## カスタマイズ

### 位置・サイズ・色

`dock.py` 先頭の「設定」ブロックを編集する:

- `EDGE` — ドックする画面端 (`"left"` / `"right"`)
- `ALIGN` — 端の中での位置 (`"top"` / `"center"` / `"bottom"`)
- `WIDTH` — パネル幅 (px)
- `UPDATE_MS` — 更新間隔
- `RESERVE_SPACE` — False にすると領域予約せず、他のウィンドウと重なれるようになる
- `KEEP_BELOW` — 自分を最背面に下げる(デフォルト True)。xmonad などは dock を
  管理外にしてスタッキングを触らないので、これが無いと ToggleStruts で領域予約を
  無効にしたとき dock が他のウィンドウの手前に残る
- `COL_*` — 配色。RGB を 0.0〜1.0 で指定
- `FONT` — フォント名。`"monospace"` のような総称名は環境によって CJK フォールバック
  フォント(英数字が豆腐になる)に解決されることがあるので具体名を推奨

### 表示するモニタの選択・並び替え

ファイル末尾の `MONITORS` リストを編集する:

```python
MONITORS = [
    Clock(),
    Cpu(),
    Net("eth0"),   # インタフェースを指定する例。Net() なら lo 以外を合算
    Fs("/home"),   # マウントポイントごとに何個でも置ける
]
```

### モニタの自作

`Monitor` を継承したクラスを書いて `MONITORS` に足すだけ。例としてプロセス数モニタ:

```python
class Procs(Monitor):
    height = 24                       # パネルの高さ (px)

    def __init__(self):
        self.n = 0

    def tick(self):                   # UPDATE_MS ごとに呼ばれる
        self.n = sum(1 for d in os.listdir("/proc") if d.isdigit())

    def draw(self, cr, w, h):         # パネル内部 (0,0)-(w,h) に描画する
        draw_text(cr, "PRC", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, str(self.n), 0, 10, size=9, align="right", width=w - 2)
```

描画には次のヘルパが使える:

- `draw_text(cr, s, x, y, size, color, bold, align, width)` — テキスト
- `draw_bar(cr, x, y, w, h, frac, color)` — 使用率バー(frac は 0.0〜1.0)
- `draw_graph(cr, x, y, w, h, [(History, color), ...], vmax)` — スクロール履歴グラフ
- `History` — グラフ用リングバッファ(`push(v)` で値を追加)
- `fmt_size(v)` — バイト数を `1.2M` のような短い表記へ

`tick()` 内の例外は捕捉してコンソールに出すだけなので、実験中に多少壊れても
ドック全体は落ちない。

## モニタごとの補足

### Workspaces (xmonad ワークスペース)

EWMH 経由で読むので、xmonad 側で `XMonad.Hooks.EwmhDesktops` を有効にしておくこと:

```haskell
import XMonad.Hooks.EwmhDesktops
main = xmonad $ ewmh $ docks $ def { ... }
```

有効でないと「no EWMH」と表示される。塗りつぶし = 現在のワークスペース、
枠付き = ウィンドウあり、暗色 = 空。EWMH の読み取りは ctypes で libX11 を
直接叩いているので追加パッケージは不要。

### Wifi

無線インタフェースが無いマシンでは自動的に非表示。電波強度は /proc/net/wireless
から読む。SSID は wireless extensions の ioctl (SIOCGIWESSID) で直接取るので
外部ツール不要。それが使えない環境では `iw` → `iwgetid` の順にフォールバックし、
どれもダメならインタフェース名を表示する。

### Volume

PipeWire の `wpctl` があればそれを、無ければ PulseAudio の `pactl` を使う。
どちらも無ければ非表示。

### Moon (月齢)

2000-01-06 の新月を基準に朔望月 (29.53 日) で計算する近似 (誤差 ±1 日程度)。
満ち欠けの形は cairo で直接描く。北半球から見た向き。

### Ping

宛先は先頭の `PING_TARGET` で変更 (`Ping("192.168.1.1")` の個別指定も可)。
ping は非同期に走らせて次の tick で回収するので GUI は固まらない。

### Tailscale / GlobalIp / Apt

外部コマンドや通信が絡むものは間隔を空けて実行する:
Tailscale は 10 秒ごと、GlobalIp (api.ipify.org) と Apt は 10 分ごと。
GlobalIp は外部サービスに HTTP アクセスする点に注意 (不要なら MONITORS から外す)。

### Player / Ime

Player は `playerctl`、Ime は `fcitx5-remote` / `fcitx-remote` / `ibus` が必要。
無ければ自動で非表示。長い曲名は 1 秒ごとに横スクロールする。

### Top

% は 1 コア分を 100% とした値 (マルチコアで張り付くと 100% を超える)。

## 既知の制限

- X11 専用(Wayland では領域予約と位置指定が効かない)
- 領域予約はプライマリモニタ基準。マルチモニタでプライマリが端にない構成だと
  予約領域がずれることがある(その場合は `RESERVE_SPACE = False` にする)
