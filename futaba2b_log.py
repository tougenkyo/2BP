# -*- coding: utf-8 -*-
"""futaba2b_log.py ─ 2BP の動作ログ（print の出力）を受け取って配る。

2BP はコンソール（黒い窓）を持たずに動く（futaba2b_qt.py が pythonw.exe で
起動し直す）。以前はログを出す設定の時だけコンソールに残していたが、その窓を
閉じると（誤って閉じても）2BP まで道連れで終了していた。

print の出力はここで受け取り、
  ・ログウインドウ …… 2BP とは別の窓。閉じても 2BP は終わらない（隠れるだけ）
  ・テキストファイル … logs/console/2bp_日時.log（新しいものから20個まで残す）
へ配る。どちらも設定（その他 → ログ）で別々に ON/OFF でき、すぐに反映される。

テキストファイルには、2BP が落ちた時の記録（faulthandler: 落ちた瞬間に各
スレッドが何をしていたか）と、Python を通らない出力（Chromium のエラー等）も
同じファイルに残す。以前はどちらもコンソールにしか出ず（落ちた記録は出もせず）、
落ちた後で「落ちたのか、コンソールを閉じたのか」すら分からなかった。
Qt の警告は Qt に受け口を登録して受け取る（窓にも出る）。

行の頭には時刻を付け、日付が変わったら区切りの行を入れる。
"""
from __future__ import annotations

import collections
import datetime
import io
import itertools
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QByteArray, QUrl
from PySide6.QtGui import QFontDatabase, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QLabel, QPushButton, QSizePolicy,
)

# ログウインドウで遡れる行数（テキストファイルには全部残る）
KEEP_LINES = 10000
# テキストファイルを何個残すか（新しいものから）
KEEP_FILES = 20
LOG_DIR = Path(__file__).resolve().parent / "logs" / "console"

_HUB: "LogHub | None" = None


def hub() -> "LogHub | None":
    """入れたログの受け口。まだ入れていなければ None（テストから本体を作った時など）"""
    return _HUB


def installed() -> bool:
    return _HUB is not None


# ══════════════════════════════════════════════════════════════════════════════
# 受け口
# ══════════════════════════════════════════════════════════════════════════════

class LogHub:
    """print の出力を行ごとに受け取り、時刻を付けて覚え、テキストファイルへ書く。

    どのスレッドから呼んでもよい。ログウインドウ（Qt）にはここから触らない。
    窓の方がタイマーで lines_since() を呼んで取りに来る（別スレッドから Qt の
    部品を触ると落ちるため）。

    テキストファイルは追記で開き、1回ごとに OS へそのまま渡す（Python の中で
    溜めない）。落ちてもそこまでは残る。落ちた時の記録（faulthandler）や
    Chromium のエラーも同じファイルへ直接書かれるが、どれも同じ開き口を共有して
    末尾へ足すので、互いを上書きしない。"""

    def __init__(self):
        self._lock = threading.RLock()   # 同じスレッドから入れ子で来ても固まらない
        self._lines = collections.deque(maxlen=KEEP_LINES)
        self._count = 0          # これまでに受け取った行数（窓がどこまで出したかの目印）
        self._day = None         # 日付が変わったら区切りの行を入れる
        self._echo = None        # 本物のコンソール（開発用に残した時だけ）にも出す
        self._native = False     # Python を通らない出力の行き先をこちらで決めるか
        self._fd = -1            # テキストファイル（書いていない時は -1）
        self._path = None
        self._header = []        # 起動時の版数など（後から書き始めたファイルの頭にも付ける）
        self._header_seq = -1    # その1行目の通し番号

    @property
    def file_path(self) -> "Path | None":
        """書いているテキストファイル（書いていなければ None）"""
        return self._path

    # ── 受け取る ─────────────────────────────────────────────────────────────
    def _stamp(self, lines) -> list:
        """時刻を付ける。日付が変わった所には区切りの行を入れる（ロックの中で呼ぶ）"""
        now = datetime.datetime.now()
        out = []
        if now.date() != self._day:
            if self._day is not None:
                out.append(f"──────── {now:%Y-%m-%d} ────────")
            self._day = now.date()
        t = now.strftime("%H:%M:%S")
        out.extend(f"{t} {ln}" for ln in lines)
        return out

    def _keep_and_write(self, out) -> None:
        self._lines.extend(out)
        self._count += len(out)
        text = "\n".join(out) + "\n"
        if self._fd >= 0:
            try:
                os.write(self._fd, text.encode("utf-8", "replace"))
            except OSError:
                pass
        if self._echo is not None:
            try:
                self._echo.write(text)
                self._echo.flush()
            except Exception:
                pass

    def add_lines(self, lines) -> None:
        """行（改行を含まない文字列）のリストを受け取る"""
        if not lines:
            return
        with self._lock:
            self._keep_and_write(self._stamp(lines))

    def print_header(self, lines) -> None:
        """起動時の版数などを出す。途中からテキストに書き始めた時もファイルの頭に
        付ける（不具合報告のログから版数と環境を特定できるように）"""
        lines = list(lines)
        with self._lock:
            out = self._stamp(lines)
            self._header_seq = self._count + len(out) - len(lines)
            self._header = out[len(out) - len(lines):]
            self._keep_and_write(out)

    # ── 窓へ渡す ─────────────────────────────────────────────────────────────
    def lines_since(self, seen: int):
        """seen 行目より後の行を返す: (今までの行数, 行のリスト, 途中が抜けたか)。
        覚えきれない数の行が来て、窓がまだ出していない行が押し出されていた時は、
        残っている分だけ返す（途中が抜けたか=True）"""
        with self._lock:
            new = self._count - seen
            if new <= 0:
                return self._count, [], False
            n = len(self._lines)
            if new > n:
                return self._count, list(self._lines), True
            return self._count, list(itertools.islice(self._lines, n - new, n)), False

    # ── テキストファイル ─────────────────────────────────────────────────────
    def open_file(self, logdir=None) -> "Path | None":
        """テキストファイルへ書き始める（もう書いていれば何もしない）。
        それまでに受け取っていた分（窓に出ていた分）も先に書く"""
        with self._lock:
            if self._fd >= 0:
                return self._path
        d = Path(logdir) if logdir else LOG_DIR
        try:
            d.mkdir(parents=True, exist_ok=True)
            _prune_old_logs(d, KEEP_FILES - 1)          # 新しく1つ足すので1つ空ける
            path = d / f"2bp_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT
                         | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as e:
            self.add_lines([f"[LOG] テキストに書き出せません: {e}"])
            return None
        with self._lock:
            if self._fd >= 0:                            # 別の所から先に開かれていた
                os.close(fd)
                return self._path
            pre = list(self._lines)
            first = self._count - len(pre)              # 残っている一番古い行の通し番号
            if self._header and self._header_seq < first:
                pre = (self._header
                       + [f"…… ここから下は、書き始める前の直近 {len(pre)} 行"] + pre)
            if pre:
                try:
                    os.write(fd, ("\n".join(pre) + "\n").encode("utf-8", "replace"))
                except OSError:
                    pass
            self._fd, self._path = fd, path
        if self._native:
            _point_native(fd)
        _fault_on(fd)
        self.add_lines([f"[LOG] ファイル出力: {path}"])
        return path

    def close_file(self) -> None:
        """テキストファイルへ書くのをやめる"""
        with self._lock:
            if self._fd < 0:
                return
        self.add_lines(["[LOG] ファイル出力を止めます"])
        _fault_off()
        if self._native:
            _point_native(None)
        with self._lock:
            fd, self._fd, self._path = self._fd, -1, None
        try:
            os.close(fd)
        except OSError:
            pass


def _prune_old_logs(d: Path, keep: int) -> None:
    """古いログを消して、新しいものから keep 個だけ残す"""
    try:
        olds = sorted(d.glob("2bp_*.log"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    except OSError:
        return
    for p in olds[max(0, keep):]:
        try:
            p.unlink()
        except OSError:
            pass


def _point_native(fd) -> None:
    """Python を通らない出力（C の標準出力・標準エラー出力と、Windows の
    標準ハンドル）の行き先を fd にする。fd=None は捨て場（NUL）。

    Chromium のエラーや Python の致命的エラーはここへ直接書かれる。コンソールが
    無いと行き場が無く消えていた。起動し直す時に標準出力を NUL で渡している
    ので、C の stderr もこの付け替えに付いてくる（pythonw.exe を直接起動した時は
    C の stderr が最初から無く、そちら経由の分は拾えない。落ちた時の記録は
    faulthandler が別に書くので残る）"""
    tmp = None
    try:
        if fd is None:
            tmp = os.open(os.devnull, os.O_WRONLY)
            fd = tmp
        for n in (1, 2):
            try:
                os.dup2(fd, n, inheritable=False)
            except OSError:
                pass
        if sys.platform.startswith("win"):
            import ctypes
            import msvcrt
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
            k32.SetStdHandle.restype = wintypes.BOOL
            for n, std in ((1, 0xFFFFFFF5), (2, 0xFFFFFFF4)):   # STD_OUTPUT / STD_ERROR
                try:
                    k32.SetStdHandle(std, msvcrt.get_osfhandle(n))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                os.close(tmp)
            except OSError:
                pass


def _fault_on(fd: int) -> None:
    """落ちた時（アクセス違反・abort 等）に、全スレッドのその時の処理を fd へ書く。
    Windows では、落ちずに済んだ例外（COM の一時的なエラー等）でも
    「Windows fatal exception: code 0x8001010d」のように書かれる事がある。
    その後もログが続いていれば落ちていない"""
    try:
        import faulthandler
        faulthandler.enable(file=fd, all_threads=True)
    except Exception:
        pass


def _fault_off() -> None:
    try:
        import faulthandler
        faulthandler.disable()
    except Exception:
        pass


def _has_console() -> bool:
    """本物のコンソール（黒い窓）がつながっているか。
    NUL も「端末」扱いになる（isatty が真）ので、Windows では窓の有無で見る"""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            return bool(ctypes.windll.kernel32.GetConsoleWindow())
        except Exception:
            return False
    out = sys.__stdout__
    try:
        return out is not None and out.isatty()
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# sys.stdout / sys.stderr の代わり
# ══════════════════════════════════════════════════════════════════════════════

class _LineStream(io.TextIOBase):
    """print の出力を受け取り、行がそろったら LogHub へ渡す。

    書きかけの行はスレッドごとに持つ。print は本文と改行を別々に書くので、
    1本にまとめて受けると、別のスレッドの print と1行の中で混ざる。"""

    def __init__(self, hub_: LogHub):
        super().__init__()
        self._hub = hub_
        self._local = threading.local()

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if not s:
            return 0
        loc = self._local
        buf = getattr(loc, "buf", "") + s
        if "\n" not in buf:
            loc.buf = buf
            return len(s)
        parts = buf.split("\n")
        loc.buf = parts.pop()
        self._hub.add_lines([p.rstrip("\r") for p in parts])
        return len(s)

    def flush(self):
        loc = self._local
        buf = getattr(loc, "buf", "")
        if buf:
            loc.buf = ""
            self._hub.add_lines([buf.rstrip("\r")])


def install() -> LogHub:
    """print の出力をここへ集める。何度呼んでもよい（2回目からは何もしない）。

    アプリ本体を読み込む前に呼ぶ（読み込み中の出力も取りこぼさないように）。"""
    global _HUB
    if _HUB is not None:
        return _HUB
    h = LogHub()
    if _has_console():
        # 開発用にコンソールを残した時（BP2_NO_RESPAWN=1）。そこにも出す
        h._echo = sys.__stdout__
    else:
        # コンソールが無い（普段）。Python を通らない出力の行き先はこちらで決める。
        # まず捨て場にそろえる（引き継いだハンドル＝前の 2BP のログファイル等へ
        # 書き足さないように）。テキストを書く時にそのファイルへ付け替える
        h._native = True
        _point_native(None)
    sys.stdout = _LineStream(h)
    sys.stderr = _LineStream(h)
    _HUB = h
    return h


def print_header(lines) -> None:
    h = _HUB
    if h is not None:
        h.print_header(lines)
    else:
        for ln in lines:
            print(ln, flush=True)


def set_file_output(on: bool) -> "Path | None":
    """テキストファイルへ書くかを切り替える。書いているファイルを返す"""
    h = _HUB
    if h is None:
        return None
    if on:
        return h.open_file()
    h.close_file()
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Qt の警告
# ══════════════════════════════════════════════════════════════════════════════
# コンソールが無いと、Qt の警告（「WebEnginePage がまだ消えていない」等）は
# どこにも出ない。受け口を登録して、ほかのログと同じ所へ出す。
# 終わる時（app.exec() の後）に外す。Python の後片付けが始まってから Qt が
# 警告を出すと、呼び先の Python が既に無くて落ちるため。

_QT_HANDLER_ON = False
_QT_LEVELS = None


def _qt_level(mode) -> str:
    global _QT_LEVELS
    if _QT_LEVELS is None:
        from PySide6.QtCore import QtMsgType
        _QT_LEVELS = {
            QtMsgType.QtDebugMsg: "debug", QtMsgType.QtInfoMsg: "info",
            QtMsgType.QtWarningMsg: "warning", QtMsgType.QtCriticalMsg: "critical",
            QtMsgType.QtFatalMsg: "fatal",
        }
    return _QT_LEVELS.get(mode, "")


def _qt_message(mode, ctx, msg) -> None:
    h = _HUB
    if h is None:
        return
    try:
        lvl = _qt_level(mode)
        cat = ""
        try:
            cat = ctx.category or ""
        except Exception:
            pass
        tag = "[Qt" + (f" {lvl}" if lvl else "") + (
            f" {cat}" if cat and cat != "default" else "") + "]"
        h.add_lines([f"{tag} {ln}" for ln in (str(msg).splitlines() or [""])])
    except Exception:
        pass


def install_qt_handler() -> None:
    global _QT_HANDLER_ON
    if _QT_HANDLER_ON or _HUB is None:
        return
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_message)
        _QT_HANDLER_ON = True
    except Exception:
        pass


def uninstall_qt_handler() -> None:
    global _QT_HANDLER_ON
    if not _QT_HANDLER_ON:
        return
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(None)
    except Exception:
        pass
    _QT_HANDLER_ON = False


# ══════════════════════════════════════════════════════════════════════════════
# ログウインドウ
# ══════════════════════════════════════════════════════════════════════════════

class LogWindow(QWidget):
    """ログウインドウ。2BP とは別の窓（タスクバーにも別に出る）。

    閉じても隠れるだけで 2BP は終わらない。2BP の窓を閉じた時は、これが
    開いていても 2BP を終える（WA_QuitOnClose を切ってある）。
    ［ヘルプ］→［ログウインドウ］でいつでも開ける。"""

    PULL_MS = 250

    def __init__(self, settings=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle("2BP ログ")
        self._settings = settings
        self._seen = 0                   # どこまで出したか（LogHub の通し番号）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setUndoRedoEnabled(False)
        self._view.setMaximumBlockCount(KEEP_LINES)
        # 以前のコンソールと同じ等幅（ＭＳ ゴシック）。アプリ全体のスタイルで
        # 「MS Pゴシック」が決まっていて setFont より強いので、この部品だけ
        # スタイルで指定する（色などはアプリ全体のスタイルのまま）
        self._view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._view.setStyleSheet(
            'QPlainTextEdit { font-family: "MS Gothic", "ＭＳ ゴシック", "Consolas",'
            ' monospace; font-size: 10pt; }')
        lay.addWidget(self._view, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        lay.addLayout(row)
        self._file_label = QLabel(self)
        self._file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        # 長い場所でも窓の幅を押し広げない（はみ出た分は切れる。全部はヒントに出す）
        self._file_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        row.addWidget(self._file_label, 1)
        for text, tip, slot in (
                ("ログのフォルダを開く", "テキストファイルの置き場所（logs\\console）を開きます",
                 self._open_dir),
                ("すべてコピー", "表示中のログをすべてクリップボードへコピーします",
                 self._copy_all),
                ("表示を消す", "ここまでの表示を消します（テキストファイルはそのまま）",
                 self._clear),
                ("閉じる", "ウインドウを閉じます（2BP は終了しません）", self.close)):
            b = QPushButton(text, self)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)

        self._timer = QTimer(self)
        self._timer.setInterval(self.PULL_MS)
        self._timer.timeout.connect(self._pull)
        self._restore_geometry()         # 出す前に戻す（出してからだと一瞬ずれる）

    # ── 表示 ────────────────────────────────────────────────────────────────
    def _pull(self) -> None:
        """LogHub から新しい行を取ってきて足す。一番下を見ていた時だけ付いて行く"""
        h = _HUB
        if h is None:
            return
        self._update_file_label(h)
        count, lines, gap = h.lines_since(self._seen)
        self._seen = count
        if not lines:
            return
        sb = self._view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 2
        if gap:
            self._view.clear()
        self._view.appendPlainText("\n".join(lines))
        if at_end or gap:
            sb.setValue(sb.maximum())

    def _update_file_label(self, h) -> None:
        p = h.file_path
        if p is not None:
            try:
                shown = str(Path(p).relative_to(Path(__file__).resolve().parent))
            except ValueError:
                shown = str(p)           # 2BP のフォルダの外（テスト等）はそのまま
            text = f"テキスト: {shown}"
            tip = str(p)
        else:
            text = "テキスト: 保存していません（設定 → その他 → ログ）"
            tip = ""
        if self._file_label.text() != text:
            self._file_label.setText(text)
            self._file_label.setToolTip(tip)

    def _open_dir(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self._view.toPlainText())

    def _clear(self) -> None:
        self._view.clear()

    # ── 位置と大きさ ─────────────────────────────────────────────────────────
    def save_geometry(self) -> None:
        if self._settings is None:
            return
        try:
            self._settings.log_window_geometry = \
                self.saveGeometry().toHex().data().decode()
        except Exception:
            pass

    def _restore_geometry(self) -> None:
        g = getattr(self._settings, "log_window_geometry", "") if self._settings else ""
        if g:
            try:
                if self.restoreGeometry(QByteArray.fromHex(g.encode())):
                    return
            except Exception:
                pass
        self.resize(960, 420)

    def showEvent(self, event):
        super().showEvent(event)
        self._pull()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self.save_geometry()
        super().hideEvent(event)
