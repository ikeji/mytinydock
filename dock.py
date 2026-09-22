#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pydock — X11 の画面端に常駐する gkrellm 風ステータスモニタ(wmdock 風の縦長表示)

依存: python3-gi + gir1.2-gtk-3.0 (それと領域予約に x11-utils の xprop)
情報源は /proc と /sys だけなので psutil などは不要。

カスタマイズは基本的にこのファイルを直接編集する:
  - 下の「設定」ブロック: 位置・幅・更新間隔・色
  - ファイル末尾の MONITORS: 表示するモニタの種類と並び順
  - 新しいモニタを足すには Monitor を継承したクラスを書く(README.md 参照)
"""

import array
import collections
import ctypes
import ctypes.util
import fcntl
import glob
import json
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import cairo

# ======================================================================
# 設定
# ======================================================================

EDGE = "right"        # ドックする画面端: "left" or "right"
ALIGN = "top"         # 端の中での位置: "top" / "center" / "bottom"
MONITOR = "edge"      # 表示するモニタ: "edge"=EDGE 側で一番端にあるモニタ,
                      # None=プライマリ, 数値=インデックス,
                      # それ以外の文字列=xrandr の出力名 ("HDMI-A-0" など)
WIDTH = 64            # パネルの幅 (px)。wmdock/dockapp の伝統に合わせて 64
UPDATE_MS = 1000      # 更新間隔 (ミリ秒)
RESERVE_SPACE = True  # True なら WM に領域を予約し、他のウィンドウが重ならない
KEEP_BELOW = True     # True なら自分を最背面に下げる。xmonad などは dock を管理外に
                      # するのでスタッキングを触らず、これが無いと ToggleStruts で
                      # 領域予約を無効にしたとき dock が他のウィンドウの手前に残る
FONT = "DejaVu Sans Mono"  # 描画に使うフォントファミリ
# 注意: "monospace" だと環境によっては CJK フォールバックフォントに解決されて
# 英数字が豆腐(□)になることがあるので、具体的なフォント名を指定している

NET_IFACE = None      # ネットワークインタフェース名。None なら lo 以外を全部合算
FS_PATHS = ["/"]      # 使用率を表示するマウントポイント
PING_TARGET = "8.8.8.8"  # Ping モニタの宛先

# 色 (R, G, B を 0.0〜1.0 で)。gkrellm っぽい暗背景 + 緑系
COL_BG = (0.03, 0.06, 0.03)      # ウィンドウ背景
COL_PANEL = (0.07, 0.12, 0.07)   # 各パネルの背景
COL_BORDER = (0.20, 0.35, 0.20)  # パネルの枠線
COL_GRAPH_BG = (0.02, 0.05, 0.02)
COL_GRID = (0.10, 0.22, 0.10)    # グラフのグリッド線
COL_GRAPH = (0.25, 0.90, 0.35)   # グラフ主系列(CPU、受信、読み込み)
COL_GRAPH2 = (0.95, 0.65, 0.20)  # グラフ副系列(送信、書き込み)
COL_TEXT = (0.80, 0.95, 0.80)    # 値の文字
COL_LABEL = (0.45, 0.65, 0.45)   # ラベルの文字
COL_BAR = (0.25, 0.80, 0.35)     # バーの通常色
COL_BAR_HI = (0.95, 0.35, 0.25)  # バーの警告色(90% 超えなど)

# ======================================================================
# 描画ヘルパ
# ======================================================================

def draw_text(cr, s, x, y, size=9, color=COL_TEXT, bold=False, align="left", width=0):
    """cairo でテキストを描く。align は "left"/"center"/"right"(width 基準)。"""
    cr.select_font_face(FONT, cairo.FontSlant.NORMAL,
                        cairo.FontWeight.BOLD if bold else cairo.FontWeight.NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(s)
    if align == "center":
        x += (width - ext.width) / 2 - ext.x_bearing
    elif align == "right":
        x += width - ext.width - ext.x_bearing
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    cr.show_text(s)


def draw_bar(cr, x, y, w, h, frac, color=None):
    """横向きの使用率バー。frac は 0.0〜1.0。"""
    frac = max(0.0, min(1.0, frac))
    if color is None:
        color = COL_BAR_HI if frac > 0.9 else COL_BAR
    cr.set_source_rgb(*COL_GRAPH_BG)
    cr.rectangle(x, y, w, h)
    cr.fill()
    cr.set_source_rgb(*color)
    cr.rectangle(x, y, w * frac, h)
    cr.fill()
    cr.set_source_rgb(*COL_GRID)
    cr.set_line_width(1)
    cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
    cr.stroke()


def draw_graph(cr, x, y, w, h, series, vmax):
    """スクロールする履歴グラフ。series は [(History, color), ...]。
    最新値が右端。vmax がスケール上限。"""
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    cr.set_source_rgb(*COL_GRAPH_BG)
    cr.rectangle(x, y, w, h)
    cr.fill()
    # 25%/50%/75% のグリッド線
    cr.set_source_rgb(*COL_GRID)
    cr.set_line_width(1)
    for frac in (0.25, 0.5, 0.75):
        yy = round(y + h * (1 - frac)) + 0.5
        cr.move_to(x, yy)
        cr.line_to(x + w, yy)
    cr.stroke()
    # 各系列を 1px 幅の縦棒で描く
    vmax = max(vmax, 1e-9)
    for hist, color in series:
        vals = hist.last(int(w))
        cr.set_source_rgba(*color, 0.85)
        for i, v in enumerate(vals):
            bh = min(v, vmax) / vmax * (h - 1)
            if bh <= 0:
                continue
            cr.rectangle(x + w - len(vals) + i, y + h - bh, 1, bh)
        cr.fill()
    cr.restore()


def fmt_size(v):
    """バイト数を 4 文字程度の短い表記にする (例: 999, 12K, 1.2M, 3.4G)。"""
    for unit in ("B", "K", "M", "G", "T"):
        if v < 1000:
            if unit == "B":
                return f"{v:.0f}"
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
        v /= 1024.0
    return f"{v:.0f}P"


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


class History:
    """グラフ用のリングバッファ。"""
    def __init__(self, size=512):
        self.data = collections.deque(maxlen=size)

    def push(self, v):
        self.data.append(v)

    def last(self, n):
        d = list(self.data)
        return d[-n:]

    def max_of(self, n):
        d = self.last(n)
        return max(d) if d else 0.0

# ======================================================================
# X11 プロパティ読み取りヘルパ (EWMH 用)
# ======================================================================

class XProps:
    """ctypes 経由で libX11 を叩いてウィンドウプロパティ (EWMH) を読む。
    Gdk.property_get は PyGObject から呼べない (introspection 非対応) ため。"""
    _inst = None

    @classmethod
    def instance(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self.dpy = None
        path = ctypes.util.find_library("X11")
        if not path:
            return
        x = ctypes.cdll.LoadLibrary(path)
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        dpy = x.XOpenDisplay(None)
        if not dpy:
            return
        # 消えたウィンドウへの問い合わせ (BadWindow) でプロセスが落ちないように
        # エラーハンドラを黙らせる。参照を保持しないと GC で SEGV するので注意
        self._err_cb = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
        x.XSetErrorHandler(self._err_cb)
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_void_p)]
        x.XFree.argtypes = [ctypes.c_void_p]
        self.x = x
        self.dpy = dpy
        self.root = x.XDefaultRootWindow(dpy)
        self._atoms = {}

    def ok(self):
        return self.dpy is not None

    def _atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.x.XInternAtom(self.dpy, name.encode(), False)
        return self._atoms[name]

    def get(self, win, prop):
        """プロパティを読む。format 32 なら int リスト、format 8 なら bytes。
        取れなければ None。"""
        if not self.dpy:
            return None
        atype = ctypes.c_ulong()
        afmt = ctypes.c_int()
        nitems = ctypes.c_ulong()
        after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        r = self.x.XGetWindowProperty(
            self.dpy, win, self._atom(prop), 0, 4096, False,
            0,  # AnyPropertyType
            ctypes.byref(atype), ctypes.byref(afmt), ctypes.byref(nitems),
            ctypes.byref(after), ctypes.byref(data))
        if r != 0 or not data.value or nitems.value == 0:
            if data.value:
                self.x.XFree(data)
            return None
        try:
            n = nitems.value
            if afmt.value == 32:
                arr = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
                return [arr[i] for i in range(n)]
            return ctypes.string_at(data, n)
        finally:
            self.x.XFree(data)

# ======================================================================
# モニタ基底クラス
# ======================================================================

class Monitor:
    """各パネルの基底クラス。継承して tick() と draw() を実装する。

    height    : パネルの高さ (px)
    available : False にすると表示されない(ハードが無い場合など)
    tick()    : UPDATE_MS ごとに呼ばれる。/proc などを読んで状態を更新する
    draw(cr, w, h) : パネル内部 (0,0)-(w,h) に cairo で描画する
    on_click(ev)   : パネル内のクリック。処理したら True を返す
    on_scroll(dir) : パネル上のホイール (+1=上 / -1=下)。処理したら True を返す
    """
    height = 40
    available = True

    def tick(self):
        pass

    def draw(self, cr, w, h):
        pass

    def on_click(self, ev):
        return False

    def on_scroll(self, direction):
        return False

# ======================================================================
# 各モニタの実装
# ======================================================================

class Clock(Monitor):
    """時計 + 日付 + 年/ISO 週 + 稼働時間。"""
    height = 56

    def draw(self, cr, w, h):
        draw_text(cr, time.strftime("%H:%M:%S"), 0, 14, size=12, bold=True,
                  align="center", width=w)
        # %a はロケールによって漢字になり、フォントに無いと豆腐になるので自前で
        wday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[time.localtime().tm_wday]
        draw_text(cr, time.strftime("%m/%d ") + wday, 0, 27, size=8,
                  align="center", width=w)
        # %G/%V は ISO 週番号 (年末年始は %Y とずれることがある)
        draw_text(cr, time.strftime("%G W%V"), 0, 39, size=8,
                  align="center", width=w)
        up = float(read_file("/proc/uptime").split()[0] or 0)
        d, rem = divmod(int(up), 86400)
        hh, rem = divmod(rem, 3600)
        mm = rem // 60
        s = f"up {d}d{hh:02}:{mm:02}" if d else f"up {hh:02}:{mm:02}"
        draw_text(cr, s, 0, 51, size=7, color=COL_LABEL, align="center", width=w)


class Moon(Monitor):
    """月齢。満ち欠けの形を cairo で直接描く (フォント任せだと豆腐になるため)。
    北半球から見た向き (満ちるとき右側が光る)。"""
    height = 36
    SYNODIC = 29.530588853           # 朔望月 (日)
    REF_NEW_MOON = 947182440         # 2000-01-06 18:14 UTC の新月 (epoch 秒)

    def draw(self, cr, w, h):
        age = ((time.time() - self.REF_NEW_MOON) / 86400.0) % self.SYNODIC
        a = age / self.SYNODIC       # 0=新月, 0.5=満月
        illum = (1 - math.cos(2 * math.pi * a)) / 2
        cx, cy, r = 18, h / 2, 13

        # 影の円 (地)
        cr.set_source_rgb(*COL_GRAPH_BG)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        # 光っている部分: 半円 + ターミネータ (楕円弧) で囲む
        k = math.cos(2 * math.pi * a)  # +1=新月, 0=半月, -1=満月
        cr.set_source_rgb(0.85, 0.85, 0.70)
        if a < 0.5:  # 満ちる: 右側が光る
            cr.arc(cx, cy, r, -math.pi / 2, math.pi / 2)
            self._terminator(cr, cx, cy, r, -k, math.pi / 2, 3 * math.pi / 2)
        else:        # 欠ける: 左側が光る
            cr.arc(cx, cy, r, math.pi / 2, 3 * math.pi / 2)
            self._terminator(cr, cx, cy, r, -k, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.fill()
        # 輪郭
        cr.set_source_rgb(*COL_GRID)
        cr.set_line_width(1)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()

        draw_text(cr, "MOON", 36, 10, size=7, color=COL_LABEL)
        draw_text(cr, f"{illum * 100:3.0f}%", 36, 21, size=8)
        draw_text(cr, f"{age:4.1f}d", 36, 32, size=8, color=COL_LABEL)

    def _terminator(self, cr, cx, cy, r, kx, a0, a1):
        """明暗境界線: 横半径 kx*r の楕円弧 (kx≈0 は直線)。"""
        if abs(kx) < 0.01:
            return  # ちょうど半月: arc の終点から close_path で直線になる
        cr.save()
        cr.translate(cx, cy)
        cr.scale(kx, 1)
        cr.arc(0, 0, r, a0, a1)
        cr.restore()


class Workspaces(Monitor):
    """xmonad のワークスペース状態 (EWMH 経由)。
    塗り = 現在、枠 = ウィンドウあり、暗 = 空。
    xmonad 側で XMonad.Hooks.EwmhDesktops の ewmh を有効にしておくこと。"""
    CELL_H = 14
    COLS = 3

    def __init__(self):
        self.xp = XProps.instance()
        self.names = []
        self.current = -1
        self.occupied = set()
        self.available = self.xp.ok()
        if self.available:
            self.tick()
        rows = max(1, -(-max(len(self.names), 1) // self.COLS))  # 切り上げ
        self.height = rows * self.CELL_H + 6

    def tick(self):
        raw = self.xp.get(self.xp.root, "_NET_DESKTOP_NAMES")
        self.names = ([s.decode("utf-8", "replace")
                       for s in raw.split(b"\0") if s] if raw else [])
        cur = self.xp.get(self.xp.root, "_NET_CURRENT_DESKTOP")
        self.current = cur[0] if cur else -1
        occ = set()
        for w in (self.xp.get(self.xp.root, "_NET_CLIENT_LIST") or []):
            d = self.xp.get(w, "_NET_WM_DESKTOP")
            if d:
                occ.add(d[0])  # sticky (0xFFFFFFFF) はどのセルにも一致しない
        self.occupied = occ

    def draw(self, cr, w, h):
        if not self.names:
            # xmonad の設定に EwmhDesktops が入っていないと名前が取れない
            draw_text(cr, "no EWMH", 0, 12, size=8, color=COL_LABEL,
                      align="center", width=w)
            return
        cw = (w - 4) / self.COLS
        for i, name in enumerate(self.names):
            x = 2 + (i % self.COLS) * cw
            y = 3 + (i // self.COLS) * self.CELL_H
            label = name[:3]
            if i == self.current:
                cr.set_source_rgb(*COL_BAR)
                cr.rectangle(x, y, cw - 1, self.CELL_H - 2)
                cr.fill()
                draw_text(cr, label, x, y + 10, size=8, color=COL_GRAPH_BG,
                          bold=True, align="center", width=cw - 1)
            elif i in self.occupied:
                cr.set_source_rgb(*COL_BORDER)
                cr.set_line_width(1)
                cr.rectangle(x + 0.5, y + 0.5, cw - 2, self.CELL_H - 3)
                cr.stroke()
                draw_text(cr, label, x, y + 10, size=8, color=COL_TEXT,
                          align="center", width=cw - 1)
            else:
                draw_text(cr, label, x, y + 10, size=8, color=COL_LABEL,
                          align="center", width=cw - 1)


class Cpu(Monitor):
    """CPU 使用率のグラフ + ロードアベレージ。"""
    height = 58

    def __init__(self):
        self.hist = History()
        self.prev = None
        self.pct = 0.0
        self.load = 0.0

    def tick(self):
        parts = read_file("/proc/stat").split("\n")[0].split()[1:]
        vals = [int(v) for v in parts]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        if self.prev:
            dt = total - self.prev[0]
            di = idle - self.prev[1]
            self.pct = 100.0 * (dt - di) / dt if dt > 0 else 0.0
        self.prev = (total, idle)
        self.hist.push(self.pct)
        self.load = os.getloadavg()[0]

    def draw(self, cr, w, h):
        draw_text(cr, "CPU", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{self.pct:3.0f}%", 0, 10, size=9, align="right", width=w - 2)
        draw_graph(cr, 2, 14, w - 4, 30, [(self.hist, COL_GRAPH)], 100.0)
        draw_text(cr, f"load {self.load:.2f}", 0, 54, size=7, color=COL_LABEL,
                  align="center", width=w)


class CpuFreq(Monitor):
    """CPU クロック周波数 (全コアの最大値)。"""
    height = 26

    def __init__(self):
        self.paths = glob.glob(
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")
        self.available = bool(self.paths)
        self.cur = 0.0
        maxes = []
        for p in self.paths:
            try:
                maxes.append(int(read_file(
                    p.replace("scaling_cur_freq", "cpuinfo_max_freq"))))
            except ValueError:
                pass
        self.hwmax = max(maxes, default=0) / 1e6  # kHz → GHz

    def tick(self):
        vals = []
        for p in self.paths:
            try:
                vals.append(int(read_file(p)))
            except ValueError:
                pass
        self.cur = max(vals, default=0) / 1e6

    def draw(self, cr, w, h):
        draw_text(cr, "GHZ", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{self.cur:.2f}", 0, 10, size=9, align="right", width=w - 2)
        if self.hwmax:
            draw_bar(cr, 2, 14, w - 4, 8, self.cur / self.hwmax, color=COL_BAR)


class Top(Monitor):
    """CPU を一番使っているプロセス。% は 1 コア分が 100%。"""
    height = 26

    def __init__(self):
        self.prev = {}
        self.t0 = None
        self.pct = 0.0
        self.name = ""
        self.tck = os.sysconf("SC_CLK_TCK")

    def tick(self):
        now = time.monotonic()
        cur = {}
        best_delta, best_name = 0, ""
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat") as f:
                    line = f.read()
                # comm は空白や ) を含みうるので、最後の ) で区切る
                rp = line.rindex(")")
                name = line[line.index("(") + 1:rp]
                f2 = line[rp + 2:].split()
                t = int(f2[11]) + int(f2[12])  # utime + stime
            except (OSError, ValueError, IndexError):
                continue
            cur[pid] = t
            if pid in self.prev:
                d = t - self.prev[pid]
                if d > best_delta:
                    best_delta, best_name = d, name
        if self.t0 is not None and now > self.t0:
            self.pct = 100.0 * best_delta / self.tck / (now - self.t0)
            self.name = best_name
        self.prev = cur
        self.t0 = now

    def draw(self, cr, w, h):
        draw_text(cr, "TOP", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{self.pct:3.0f}%", 0, 10, size=9, align="right", width=w - 2)
        draw_text(cr, self.name[:11], 0, 22, size=7, align="center", width=w)


class Mem(Monitor):
    """メモリ + スワップの使用量バー。"""
    height = 46

    def __init__(self):
        self.used = self.total = 1
        self.sused = self.stotal = 0

    def tick(self):
        info = {}
        for line in read_file("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0]) * 1024  # kB → bytes
        self.total = info.get("MemTotal", 1)
        self.used = self.total - info.get("MemAvailable", 0)
        self.stotal = info.get("SwapTotal", 0)
        self.sused = self.stotal - info.get("SwapFree", 0)

    def draw(self, cr, w, h):
        draw_text(cr, "MEM", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{100 * self.used / self.total:3.0f}%", 0, 10, size=9,
                  align="right", width=w - 2)
        draw_bar(cr, 2, 14, w - 4, 8, self.used / self.total)
        draw_text(cr, f"{fmt_size(self.used)}/{fmt_size(self.total)}", 0, 32,
                  size=8, align="center", width=w)
        if self.stotal:  # スワップがある時だけ細いバーを出す
            draw_bar(cr, 2, 37, w - 4, 5, self.sused / self.stotal,
                     color=COL_GRAPH2 if self.sused / self.stotal <= 0.9 else COL_BAR_HI)
        else:
            draw_text(cr, "no swap", 0, 43, size=7, color=COL_LABEL,
                      align="center", width=w)


class Net(Monitor):
    """ネットワーク送受信のグラフ(緑=受信、橙=送信)。自動スケール。"""
    height = 60

    def __init__(self, iface=NET_IFACE):
        self.iface = iface
        self.rx_hist = History()
        self.tx_hist = History()
        self.prev = None
        self.rx = self.tx = 0.0

    def _read(self):
        rx = tx = 0
        for line in read_file("/proc/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if self.iface:
                if name != self.iface:
                    continue
            elif name == "lo":
                continue
            f = rest.split()
            rx += int(f[0])
            tx += int(f[8])
        return rx, tx, time.monotonic()

    def tick(self):
        cur = self._read()
        if self.prev:
            dt = max(cur[2] - self.prev[2], 1e-6)
            self.rx = (cur[0] - self.prev[0]) / dt
            self.tx = (cur[1] - self.prev[1]) / dt
        self.prev = cur
        self.rx_hist.push(self.rx)
        self.tx_hist.push(self.tx)

    def draw(self, cr, w, h):
        draw_text(cr, "NET", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"↓{fmt_size(self.rx)}", 0, 10, size=8, color=COL_GRAPH,
                  align="right", width=w - 2)
        vmax = max(self.rx_hist.max_of(w), self.tx_hist.max_of(w), 10 * 1024)
        draw_graph(cr, 2, 14, w - 4, 30,
                   [(self.rx_hist, COL_GRAPH), (self.tx_hist, COL_GRAPH2)], vmax)
        draw_text(cr, self.iface or "all", 2, 56, size=7, color=COL_LABEL)
        draw_text(cr, f"↑{fmt_size(self.tx)}", 0, 56, size=8, color=COL_GRAPH2,
                  align="right", width=w - 2)


class Wifi(Monitor):
    """接続中の Wi-Fi (SSID + 電波強度)。無線 IF が無いマシンでは非表示。"""
    height = 42

    def __init__(self, iface=None):
        self.iface = iface or self._detect()
        self.available = bool(self.iface)
        self.ssid = ""
        self.quality = 0.0
        self.level = 0

    def _detect(self):
        # /sys/class/net/<if>/wireless はカーネルが無線 IF にだけ作るので、
        # この glob にマッチするのは wifi のみ。複数あれば up のものを優先
        cands = [os.path.basename(os.path.dirname(p))
                 for p in sorted(glob.glob("/sys/class/net/*/wireless"))]
        for iface in cands:
            if read_file(f"/sys/class/net/{iface}/operstate").strip() == "up":
                return iface
        return cands[0] if cands else None

    SIOCGIWESSID = 0x8B1B  # wireless extensions: get ESSID

    def _get_ssid(self):
        # 1) wireless extensions の ioctl。外部ツール不要で、
        #    /proc/net/wireless が読める環境なら大抵これで取れる
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                buf = array.array("b", b"\0" * 33)
                ptr, _ = buf.buffer_info()
                # struct iwreq: char ifrn_name[16] + struct iw_point
                # { void *pointer; u16 length; u16 flags; }
                req = struct.pack("16sPHH", self.iface.encode(), ptr, 33, 0)
                fcntl.ioctl(s.fileno(), self.SIOCGIWESSID, req)
                ssid = buf.tobytes().rstrip(b"\0").decode("utf-8", "replace")
                if ssid:
                    return ssid
            finally:
                s.close()
        except OSError:
            pass
        # 2) フォールバック: iw → iwgetid
        for cmd in (["iw", "dev", self.iface, "link"],
                    ["iwgetid", "-r", self.iface]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True).stdout
            except FileNotFoundError:
                continue
            if cmd[0] == "iw":
                m = re.search(r"^\s*SSID: (.+)$", out, re.M)
                if m:
                    return m.group(1)
                if "Not connected" in out:
                    return ""
            elif out.strip():
                return out.strip()
        return self.iface  # SSID が取れなければせめて IF 名を表示

    def tick(self):
        self.quality = 0.0
        self.level = 0
        for line in read_file("/proc/net/wireless").splitlines()[2:]:
            f = line.split()
            if f and f[0].rstrip(":") == self.iface:
                self.quality = float(f[2].rstrip(".")) / 70.0  # link quality
                self.level = int(float(f[3].rstrip(".")))      # dBm
        self.ssid = self._get_ssid() if self.quality > 0 else ""

    def draw(self, cr, w, h):
        draw_text(cr, "WIFI", 2, 10, size=8, color=COL_LABEL)
        if self.ssid:
            if self.level:
                draw_text(cr, f"{self.level}dBm", 0, 10, size=8,
                          align="right", width=w - 2)
            draw_text(cr, self.ssid[:10], 0, 23, size=8,
                      align="center", width=w)
            draw_bar(cr, 2, 27, w - 4, 8, self.quality,
                     color=COL_BAR_HI if self.quality < 0.3 else COL_BAR)
        else:
            draw_text(cr, "not conn", 0, 23, size=8, color=COL_LABEL,
                      align="center", width=w)


class Ping(Monitor):
    """PING_TARGET への RTT グラフ。ping は非同期に走らせ、次の tick で回収。"""
    height = 42

    def __init__(self, target=None):
        self.target = target or PING_TARGET
        self.available = bool(shutil.which("ping"))
        self.hist = History()
        self.rtt = None
        self.proc = None

    def tick(self):
        if self.proc is not None:
            if self.proc.poll() is None:  # -W 1 なので普通はここに来ない
                self.proc.kill()
                self.rtt = None
            else:
                m = re.search(r"time=([\d.]+)", self.proc.stdout.read())
                self.rtt = float(m.group(1)) if m else None
            self.hist.push(self.rtt or 0.0)
            self.proc = None
        self.proc = subprocess.Popen(
            ["ping", "-n", "-c", "1", "-W", "1", self.target],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def draw(self, cr, w, h):
        draw_text(cr, "PING", 2, 10, size=8, color=COL_LABEL)
        txt = f"{self.rtt:.0f}ms" if self.rtt is not None else "--"
        draw_text(cr, txt, 0, 10, size=8,
                  color=COL_TEXT if self.rtt is not None else COL_BAR_HI,
                  align="right", width=w - 2)
        vmax = max(self.hist.max_of(w), 20.0)  # 最低 20ms スケール
        draw_graph(cr, 2, 14, w - 4, 24, [(self.hist, COL_GRAPH)], vmax)


class Tailscale(Monitor):
    """Tailscale の状態と自機の Tailscale IP。10 秒ごとに更新。"""
    height = 34
    INTERVAL = 10

    def __init__(self):
        self.available = bool(shutil.which("tailscale"))
        self.state = "?"
        self.ip = ""
        self.last = -1e9

    def tick(self):
        if time.monotonic() - self.last < self.INTERVAL:
            return
        self.last = time.monotonic()
        try:
            out = subprocess.run(["tailscale", "status", "--json"],
                                 capture_output=True, text=True, timeout=3).stdout
            j = json.loads(out or "{}")
            self.state = j.get("BackendState", "?")
            self.ip = next((i for i in j.get("TailscaleIPs") or [] if "." in i), "")
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            self.state = "err"

    def draw(self, cr, w, h):
        draw_text(cr, "TS", 2, 10, size=8, color=COL_LABEL)
        ok = self.state == "Running"
        draw_text(cr, "up" if ok else self.state[:7].lower(), 0, 10, size=8,
                  color=COL_GRAPH if ok else COL_GRAPH2, align="right", width=w - 2)
        draw_text(cr, self.ip, 0, 26, size=7, align="center", width=w)


class GlobalIp(Monitor):
    """グローバル IP。10 分ごとに api.ipify.org からスレッドで取得。"""
    height = 24
    INTERVAL = 600
    URL = "https://api.ipify.org"

    def __init__(self):
        self.ip = "..."
        self.last = -1e9
        self.thread = None

    def tick(self):
        if self.thread and not self.thread.is_alive():
            self.thread = None
        if not self.thread and time.monotonic() - self.last > self.INTERVAL:
            self.last = time.monotonic()
            self.thread = threading.Thread(target=self._fetch, daemon=True)
            self.thread.start()

    def _fetch(self):
        try:
            self.ip = urllib.request.urlopen(self.URL, timeout=10) \
                .read().decode().strip()
        except OSError:
            self.ip = "n/a"

    def draw(self, cr, w, h):
        draw_text(cr, "GIP", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, self.ip, 0, 20, size=7, align="center", width=w)


class Disk(Monitor):
    """ディスク I/O のグラフ(緑=読み込み、橙=書き込み)。物理ディスクを合算。"""
    height = 60
    DISK_RE = re.compile(r"^(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")

    def __init__(self):
        self.r_hist = History()
        self.w_hist = History()
        self.prev = None
        self.rd = self.wr = 0.0

    def _read(self):
        rd = wr = 0
        for line in read_file("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) >= 10 and self.DISK_RE.match(f[2]):
                rd += int(f[5]) * 512  # セクタ数 → バイト
                wr += int(f[9]) * 512
        return rd, wr, time.monotonic()

    def tick(self):
        cur = self._read()
        if self.prev:
            dt = max(cur[2] - self.prev[2], 1e-6)
            self.rd = (cur[0] - self.prev[0]) / dt
            self.wr = (cur[1] - self.prev[1]) / dt
        self.prev = cur
        self.r_hist.push(self.rd)
        self.w_hist.push(self.wr)

    def draw(self, cr, w, h):
        draw_text(cr, "DSK", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"R{fmt_size(self.rd)}", 0, 10, size=8, color=COL_GRAPH,
                  align="right", width=w - 2)
        vmax = max(self.r_hist.max_of(w), self.w_hist.max_of(w), 1024 * 1024)
        draw_graph(cr, 2, 14, w - 4, 30,
                   [(self.r_hist, COL_GRAPH), (self.w_hist, COL_GRAPH2)], vmax)
        draw_text(cr, f"W{fmt_size(self.wr)}", 0, 56, size=8, color=COL_GRAPH2,
                  align="right", width=w - 2)


class Fs(Monitor):
    """ファイルシステム使用率のバー。"""
    height = 28

    def __init__(self, path="/"):
        self.path = path
        self.frac = 0.0
        self.free = 0

    def tick(self):
        st = os.statvfs(self.path)
        total = st.f_blocks * st.f_frsize
        self.free = st.f_bavail * st.f_frsize
        self.frac = 1.0 - st.f_bavail / st.f_blocks if st.f_blocks else 0.0

    def draw(self, cr, w, h):
        name = self.path if len(self.path) <= 5 else "…" + self.path[-4:]
        draw_text(cr, name, 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{fmt_size(self.free)} free", 0, 10, size=8,
                  align="right", width=w - 2)
        draw_bar(cr, 2, 14, w - 4, 9, self.frac)


class Temp(Monitor):
    """温度センサ (/sys/class/hwmon) の最大値。センサが無ければ非表示。"""
    height = 28

    def __init__(self):
        self.temp = 0.0
        self.available = bool(self._sensors())

    def _sensors(self):
        return glob.glob("/sys/class/hwmon/hwmon*/temp*_input")

    def tick(self):
        vals = []
        for p in self._sensors():
            try:
                vals.append(int(read_file(p)) / 1000.0)
            except ValueError:
                pass
        self.temp = max(vals, default=0.0)

    def draw(self, cr, w, h):
        draw_text(cr, "TMP", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{self.temp:.0f}°C", 0, 10, size=9, align="right", width=w - 2)
        draw_bar(cr, 2, 14, w - 4, 9, self.temp / 100.0,
                 color=COL_BAR_HI if self.temp > 80 else COL_BAR)


class Fan(Monitor):
    """ファン回転数 (/sys/class/hwmon の最大値)。センサが無ければ非表示。"""
    height = 24

    def __init__(self):
        self.paths = glob.glob("/sys/class/hwmon/hwmon*/fan*_input")
        self.available = bool(self.paths)
        self.rpm = 0

    def tick(self):
        vals = []
        for p in self.paths:
            try:
                vals.append(int(read_file(p)))
            except ValueError:
                pass
        self.rpm = max(vals, default=0)

    def draw(self, cr, w, h):
        draw_text(cr, "FAN", 2, 10, size=8, color=COL_LABEL)
        draw_text(cr, f"{self.rpm}rpm" if self.rpm else "stop", 0, 10, size=8,
                  align="right", width=w - 2)


class Battery(Monitor):
    """バッテリー残量。バッテリーが無いマシンでは非表示。"""
    height = 28

    def __init__(self):
        bats = glob.glob("/sys/class/power_supply/BAT*")
        self.path = bats[0] if bats else None
        self.available = bool(self.path)
        self.cap = 0
        self.charging = False

    def tick(self):
        self.cap = int(read_file(self.path + "/capacity") or 0)
        self.charging = "Charging" in read_file(self.path + "/status")

    def draw(self, cr, w, h):
        draw_text(cr, "BAT", 2, 10, size=8, color=COL_LABEL)
        mark = "+" if self.charging else ""
        draw_text(cr, f"{self.cap}%{mark}", 0, 10, size=9, align="right", width=w - 2)
        draw_bar(cr, 2, 14, w - 4, 9, self.cap / 100.0,
                 color=COL_BAR_HI if self.cap < 15 and not self.charging else COL_BAR)

class Volume(Monitor):
    """サウンド音量 (PipeWire の wpctl 優先、PulseAudio の pactl にフォールバック)。
    [-][M][+] ボタン、バーをクリックで直接指定、ホイールでも上下。"""
    height = 44
    STEP = 5  # ボタン/ホイール 1 回で動かす %

    def __init__(self):
        self.backend = None
        for name, probe in (("wpctl", ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]),
                            ("pactl", ["pactl", "get-sink-volume", "@DEFAULT_SINK@"])):
            try:
                if subprocess.run(probe, capture_output=True).returncode == 0:
                    self.backend = name
                    break
            except FileNotFoundError:
                pass
        self.available = self.backend is not None
        self.vol = 0
        self.muted = False

    def _run(self, *args):
        return subprocess.run(args, capture_output=True, text=True).stdout

    def tick(self):
        if self.backend == "wpctl":
            out = self._run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            m = re.search(r"([\d.]+)", out)
            self.vol = round(float(m.group(1)) * 100) if m else 0
            self.muted = "MUTED" in out
        else:
            out = self._run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
            m = re.search(r"(\d+)%", out)
            self.vol = int(m.group(1)) if m else 0
            self.muted = "yes" in self._run("pactl", "get-sink-mute", "@DEFAULT_SINK@")

    def _change(self, ud):
        if self.backend == "wpctl":
            # -l 1.0 で 100% を上限にする
            self._run("wpctl", "set-volume", "-l", "1.0",
                      "@DEFAULT_AUDIO_SINK@", f"{self.STEP}%{ud}")
        else:
            self._run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{ud}{self.STEP}%")
        self.tick()

    def _set_pct(self, pct):
        pct = max(0, min(100, round(pct)))
        if self.backend == "wpctl":
            self._run("wpctl", "set-volume", "-l", "1.0",
                      "@DEFAULT_AUDIO_SINK@", f"{pct}%")
        else:
            self._run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%")
        self.tick()

    def _toggle_mute(self):
        if self.backend == "wpctl":
            self._run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle")
        else:
            self._run("pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle")
        self.tick()

    def on_scroll(self, direction):
        self._change("+" if direction > 0 else "-")
        return True

    def _btn_rects(self, w):
        bw = (w - 4) / 3
        return [("-", 2, 27, bw, 14),
                ("M", 2 + bw, 27, bw, 14),
                ("+", 2 + 2 * bw, 27, bw, 14)]

    def on_click(self, ev):
        if ev.button != 1:
            return False
        w = ev.window.get_width()
        for name, x, y, bw, bh in self._btn_rects(w):
            if x <= ev.x < x + bw and y <= ev.y < y + bh:
                if name == "-":
                    self._change("-")
                elif name == "+":
                    self._change("+")
                else:
                    self._toggle_mute()
                return True
        if 14 <= ev.y < 25:  # バーをクリック → その位置の音量に設定
            self._set_pct((ev.x - 2) / (w - 4) * 100)
            return True
        self._toggle_mute()  # それ以外 (ラベル行など) はミュート切替
        return True

    def draw(self, cr, w, h):
        draw_text(cr, "VOL", 2, 10, size=8, color=COL_LABEL)
        if self.muted:
            draw_text(cr, "MUTE", 0, 10, size=9, color=COL_GRAPH2,
                      align="right", width=w - 2)
        else:
            draw_text(cr, f"{self.vol}%", 0, 10, size=9, align="right", width=w - 2)
        draw_bar(cr, 2, 14, w - 4, 9, self.vol / 100.0,
                 color=COL_GRAPH2 if self.muted else COL_BAR)
        for name, x, y, bw, bh in self._btn_rects(w):
            if name == "M" and self.muted:
                cr.set_source_rgba(*COL_GRAPH2, 0.35)
                cr.rectangle(x, y, bw, bh)
                cr.fill()
            cr.set_source_rgb(*COL_BORDER)
            cr.set_line_width(1)
            cr.rectangle(x + 0.5, y + 0.5, bw - 1, bh - 1)
            cr.stroke()
            draw_text(cr, name, x, y + 11, size=9, bold=(name != "M"),
                      align="center", width=bw)


class Player(Monitor):
    """メディアプレイヤー (playerctl 経由)。クリックで再生/一時停止。
    長いタイトルは 1 秒ごとに横スクロール。"""
    height = 30

    def __init__(self):
        self.available = bool(shutil.which("playerctl"))
        self.status = ""
        self.title = ""
        self.off = 0

    def tick(self):
        r = subprocess.run(["playerctl", "status"], capture_output=True, text=True)
        self.status = r.stdout.strip() if r.returncode == 0 else ""
        if self.status:
            self.title = subprocess.run(
                ["playerctl", "metadata", "--format", "{{artist}} - {{title}}"],
                capture_output=True, text=True).stdout.strip()
        else:
            self.title = ""
        self.off += 1

    def on_click(self, ev):
        if ev.button != 1 or not self.status:
            return False
        subprocess.run(["playerctl", "play-pause"], capture_output=True)
        self.tick()
        return True

    def draw(self, cr, w, h):
        icon = {"Playing": ">", "Paused": "||"}.get(self.status, "--")
        draw_text(cr, icon, 2, 10, size=9, bold=True,
                  color=COL_GRAPH if self.status == "Playing" else COL_LABEL)
        draw_text(cr, "MUSIC", 0, 10, size=7, color=COL_LABEL,
                  align="right", width=w - 2)
        n = 11  # 表示できる文字数
        s = self.title
        if len(s) > n:  # マーキー表示
            s2 = s + "   "
            i = self.off % len(s2)
            s = (s2 + s2)[i:i + n]
        draw_text(cr, s or "no player", 0, 24, size=7,
                  color=COL_TEXT if s else COL_LABEL, align="center", width=w)


class Ime(Monitor):
    """IME の状態 (fcitx5/fcitx/ibus)。クリックでオンオフ切替 (fcitx のみ)。"""
    height = 24
    CANDS = ("fcitx5-remote", "fcitx-remote")

    def __init__(self):
        self.cands = [c for c in self.CANDS if shutil.which(c)]
        self.ibus = shutil.which("ibus")
        self.available = bool(self.cands or self.ibus)
        self.cmd = None  # 実際に応答したコマンド。毎 tick 探し直す
        self.active = False
        self.name = ""

    def _probe(self):
        """応答する fcitx-remote 系コマンドを探す。バイナリがあっても本体が
        動いていない (v4/v5 混在や起動順) ことがあるので毎回確かめる。"""
        for c in self.cands:
            r = subprocess.run([c], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip() in ("0", "1", "2"):
                return c, r.stdout.strip()
        return None, ""

    def tick(self):
        self.cmd, st = self._probe()
        if self.cmd:
            self.active = st == "2"
            # fcitx4 の fcitx-remote に -n は無い → 空なら on/off 表示に落とす
            self.name = subprocess.run([self.cmd, "-n"], capture_output=True,
                                       text=True).stdout.strip()
            if not self.name:
                self.name = "closed" if st == "0" else \
                            ("on" if self.active else "off")
        elif self.ibus:
            self.name = subprocess.run(["ibus", "engine"], capture_output=True,
                                       text=True).stdout.strip()
            self.active = bool(self.name) and not self.name.startswith("xkb:")
        else:
            self.name = ""

    def on_click(self, ev):
        if ev.button != 1 or not self.cmd:
            return False
        subprocess.run([self.cmd, "-t"], capture_output=True)
        self.tick()
        return True

    def draw(self, cr, w, h):
        draw_text(cr, "IME", 2, 10, size=8, color=COL_LABEL)
        # 応答が無いときの "n/a" は IME 本体が動いていないか、DBus セッションに
        # つながっていない (autostart の起動順など) ことを示す
        name = re.sub(r"^(keyboard-|xkb:)", "", self.name)[:8] or "n/a"
        draw_text(cr, name, 0, 10, size=8, bold=self.active,
                  color=COL_GRAPH if self.active else COL_LABEL,
                  align="right", width=w - 2)


class Apt(Monitor):
    """apt の更新可能パッケージ数。10 分ごとに非同期でチェック。"""
    height = 24
    INTERVAL = 600

    def __init__(self):
        self.available = bool(shutil.which("apt"))
        self.count = -1
        self.proc = None
        self.last = -1e9

    def tick(self):
        if self.proc is not None:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read()
                # "pkg/suite version arch [upgradable from ...]" 形式の行を数える
                self.count = sum(1 for l in out.splitlines() if "/" in l)
                self.proc = None
        elif time.monotonic() - self.last > self.INTERVAL:
            self.last = time.monotonic()
            self.proc = subprocess.Popen(
                ["apt", "list", "--upgradable"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def draw(self, cr, w, h):
        draw_text(cr, "APT", 2, 10, size=8, color=COL_LABEL)
        if self.count < 0:
            draw_text(cr, "...", 0, 10, size=9, align="right", width=w - 2)
        else:
            draw_text(cr, str(self.count), 0, 10, size=9, bold=self.count > 0,
                      color=COL_GRAPH2 if self.count > 0 else COL_TEXT,
                      align="right", width=w - 2)

# ======================================================================
# ウィンドウ本体
# ======================================================================

def target_monitor():
    """MONITOR 設定に従って表示先の Gdk.Monitor を返す。"""
    display = Gdk.Display.get_default()
    n = display.get_n_monitors()
    if isinstance(MONITOR, int):
        return display.get_monitor(min(MONITOR, n - 1))
    if MONITOR == "edge":
        mons = [display.get_monitor(i) for i in range(n)]
        if EDGE == "left":
            return min(mons, key=lambda m: m.get_geometry().x)
        return max(mons, key=lambda m: m.get_geometry().x + m.get_geometry().width)
    if isinstance(MONITOR, str):
        for i in range(n):
            m = display.get_monitor(i)
            if m.get_model() == MONITOR:   # X11 では xrandr の出力名が入る
                return m
        print("pydock: モニタ %r が見つからないのでプライマリを使用" % MONITOR)
    return display.get_primary_monitor() or display.get_monitor(0)


def screen_extent():
    """全モニタを合わせたルートウィンドウの大きさ (幅, 高さ)。"""
    display = Gdk.Display.get_default()
    w = h = 0
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        w = max(w, g.x + g.width)
        h = max(h, g.y + g.height)
    return w, h


class Dock(Gtk.Window):
    def __init__(self, monitors):
        super().__init__(title="pydock")
        self.monitors = [m for m in monitors if m.available]
        self._drop_overflow()

        # wmdock 風: 枠なし、タスクバーに出ない、全ワークスペースに表示、DOCK 扱い
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.stick()
        if KEEP_BELOW:
            self.set_keep_below(True)  # EWMH を解釈する WM 向けのヒント

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_margin_start(2)
        box.set_margin_end(2)
        self.add(box)

        for m in self.monitors:
            area = Gtk.DrawingArea()
            area.set_size_request(WIDTH - 4, m.height)
            area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                            Gdk.EventMask.SCROLL_MASK |
                            Gdk.EventMask.SMOOTH_SCROLL_MASK)
            area.connect("draw", self.on_draw_panel, m)
            area.connect("button-press-event", self.on_panel_button, m)
            area.connect("scroll-event", self.on_panel_scroll, m)
            box.pack_start(area, False, False, 0)

        # 背景色は CSS で指定
        css = Gtk.CssProvider()
        r, g, b = (int(c * 255) for c in COL_BG)
        css.load_from_data(f"window {{ background-color: rgb({r},{g},{b}); }}".encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.connect("destroy", Gtk.main_quit)
        self.connect("button-press-event", self.on_button)
        self.connect("map-event", self.on_map)

        GLib.timeout_add(UPDATE_MS, self.on_tick)
        self.on_tick()

    def _drop_overflow(self):
        """画面の縦幅に収まらないパネルは中途半端に切れないよう丸ごと非表示に。
        並び順は保ちつつ、入るものは後ろからでも詰める (背の高いパネルが
        入らなくても、その後ろの小さいパネルは表示される)。"""
        avail_h = target_monitor().get_geometry().height
        used = 4  # 上下マージン
        fits, dropped = [], []
        for m in self.monitors:
            need = m.height + (2 if fits else 0)  # パネル間 spacing
            if used + need <= avail_h:
                fits.append(m)
                used += need
            else:
                dropped.append(type(m).__name__)
        if dropped:
            print("pydock: 画面 (縦%dpx) に収まらないため非表示: %s\n"
                  "        (MONITORS の並びで前にあるものが優先されます)"
                  % (avail_h, ", ".join(dropped)))
        self.monitors = fits

    # ---- 更新 ----
    def on_tick(self):
        for m in self.monitors:
            try:
                m.tick()
            except Exception as e:
                # モニタ 1 個の失敗で全体を止めない
                print(f"pydock: {type(m).__name__}.tick() failed: {e}")
        self.queue_draw()
        return True  # タイマー継続

    # ---- 描画 ----
    def on_draw_panel(self, area, cr, monitor):
        w = area.get_allocated_width()
        h = area.get_allocated_height()
        cr.set_source_rgb(*COL_PANEL)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.set_source_rgb(*COL_BORDER)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()
        monitor.draw(cr, w, h)

    # ---- 配置と領域予約 ----
    def on_map(self, *_):
        geo = target_monitor().get_geometry()
        _, win_h = self.get_size()

        x = geo.x if EDGE == "left" else geo.x + geo.width - WIDTH
        if ALIGN == "top":
            y = geo.y
        elif ALIGN == "bottom":
            y = geo.y + geo.height - win_h
        else:
            y = geo.y + (geo.height - win_h) // 2
        self.move(x, y)

        if RESERVE_SPACE:
            self.set_strut(geo, y, y + win_h)
        if KEEP_BELOW:
            # xmonad は dock を管理外にしてスタッキングを変更しないため、
            # ヒントだけでなく実際に XLowerWindow で最背面へ下げておく
            self.get_window().lower()
        return False

    def set_strut(self, geo, y0, y1):
        """_NET_WM_STRUT(_PARTIAL) をセットして WM に画面端の領域を予約させる。
        Gdk 経由の property 設定は PyGObject で扱いづらいので xprop に頼る。
        strut はルートウィンドウの端からの距離なので、表示先モニタがルートの
        端に無いマルチモニタ構成では、その隙間の分を足す。"""
        try:
            xid = self.get_window().get_xid()
        except AttributeError:
            return  # X11 バックエンドでない (Wayland など)
        # [left, right, top, bottom, l_y0, l_y1, r_y0, r_y1, t_x0, t_x1, b_x0, b_x1]
        part = [0] * 12
        root_w, _ = screen_extent()
        if EDGE == "left":
            part[0], part[4], part[5] = geo.x + WIDTH, y0, y1
        else:
            part[1], part[6], part[7] = root_w - (geo.x + geo.width) + WIDTH, y0, y1
        for name, vals in (("_NET_WM_STRUT_PARTIAL", part),
                           ("_NET_WM_STRUT", part[:4])):
            r = subprocess.run(
                ["xprop", "-id", str(xid), "-f", name, "32c",
                 "-set", name, ",".join(map(str, vals))],
                capture_output=True)
            if r.returncode != 0:
                print("pydock: 領域予約に失敗 (xprop は入っていますか? "
                      "apt install x11-utils)")
                break

    # ---- パネルごとのマウス操作 ----
    def on_panel_button(self, _area, ev, monitor):
        if monitor.on_click(ev):
            self.queue_draw()
            return True
        return False  # 未処理ならウィンドウ側 (右クリックメニュー) へ

    def on_panel_scroll(self, _area, ev, monitor):
        if ev.direction == Gdk.ScrollDirection.UP:
            d = 1
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            d = -1
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            d = -1 if ev.delta_y > 0 else 1
        else:
            return False
        if monitor.on_scroll(d):
            self.queue_draw()
            return True
        return False

    # ---- 右クリックメニュー ----
    def on_button(self, _w, ev):
        if ev.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="終了")
            item.connect("activate", Gtk.main_quit)
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(ev)
            return True
        return False

# ======================================================================
# 表示するモニタと並び順。ここを編集してカスタマイズする。
# ======================================================================

MONITORS = [
    Clock(),  # 時刻 / 日付 / ISO 週 / 稼働時間
    Moon(),  # 月齢
    Workspaces(),  # xmonad 側で EwmhDesktops (ewmh) が必要
    # Ime(),        # IME 状態。クリックで切替
    Cpu(),
    # CpuFreq(),    # CPU クロック
    # Top(),        # CPU を一番使っているプロセス
    Mem(),
    Net(),  # Net("eth0") のように指定も可
    Wifi(),  # 無線 IF が無ければ自動で消える
    # Ping(),         # 宛先は PING_TARGET (Ping("192.168.1.1") でも可)
    # Tailscale(),    # tailscale コマンドが無ければ自動で消える
    # GlobalIp(),     # 10 分ごとに api.ipify.org へ問い合わせ
    Disk(),
    *[Fs(p) for p in FS_PATHS],
    Temp(),  # センサが無ければ自動で消える
    # Fan(),        # ファン回転数
    Battery(),  # バッテリーが無ければ自動で消える
    Volume(),  # [-][M][+] ボタン、バークリックで直接指定、ホイール可
    Player(),  # playerctl が無ければ自動で消える。クリックで再生/停止
    # Apt(),        # 更新可能パッケージ数 (10 分ごと)
]


RC_PATH = os.path.expanduser("~/.dockrc.py")


def load_rc():
    """~/.dockrc.py があれば、このモジュールの名前空間で実行して設定を上書きする。
    EDGE = "left" のような再代入、MONITORS の組み換え、Monitor サブクラスの
    定義がそのまま書ける。エラー時は表示してデフォルト設定で続行する。"""
    if not os.path.exists(RC_PATH):
        return
    try:
        exec(compile(open(RC_PATH).read(), RC_PATH, "exec"), globals())
    except Exception:
        import traceback
        print("pydock: %s の読み込みに失敗 (デフォルト設定で続行):" % RC_PATH,
              flush=True)
        traceback.print_exc()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl-C で終了できるように
    load_rc()
    win = Dock(MONITORS)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
