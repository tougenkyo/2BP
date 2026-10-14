# -*- coding: utf-8 -*-
"""futaba2b_freeze.py ─ 2BP が固まった時の記録。

「2BP 全体が反応しなくなる」と言われても、今までのログには固まった所が
何も残らなかった（ログは何かをした時にだけ書くので、止まっている間は
何も出ない）。強制終了すると、その時に何をしていたかも分からない。

ここでは次の3つを見張り、おかしい時だけログへ書く（普段は何も書かない。
通信もしない）。行の頭は「[固まり]」。

1. 画面の処理（GUI スレッド）が止まっていないか
   0.5 秒ごとに画面の側で合図を出し、別のスレッドがそれを見る。
   3 秒以上合図が無ければ、その瞬間に各スレッドが何をしていたか（処理の場所）を
   書く。止まり続けていれば 15 秒・60 秒の時点でもう一度書く。戻ったら、
   何秒止まっていたかを書く。
   見張りのスレッドまで動けない止まり方（Python を通らない所で止まった時）の
   ために、faulthandler にも頼んでおく（数秒遅れて、テキストファイルにだけ
   「Timeout (0:00:07)!」の形で書かれる）。

2. 画面は動いているのに操作が効かない形になっていないか
   見えない所に出た確認窓（ほかの窓を操作できなくする）・メニュー、つかんだ
   ままのマウス／キー入力が 10 秒以上続いたら、それが何かを書く。
   普通に見えている確認窓（設定画面など）は書かない。

3. スレ・カタログの表示（Chromium）が反応しているか
   見えているスレ・カタログに数秒ごとに小さな JS を送り、8 秒以上返事が
   無ければ書く。返事が来たら何秒かかったかを書く。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

TAG = "[固まり]"


def _log_fd() -> int:
    """テキストファイルの書き口（書いていなければ -1）"""
    try:
        import futaba2b_log as _log
        h = _log.hub()
        return h.fileno() if h is not None else -1
    except Exception:
        return -1


# ── スタックの書き出し ───────────────────────────────────────────────────────
_STD_IDLE = (("threading.py", "wait"), ("queue.py", "get"), ("thread.py", "_worker"),
             ("threading.py", "_wait_for_tstate_lock"), ("selectors.py", "select"))


def _is_idle(frames) -> bool:
    """何もせず待っているだけのスレッドか（プールの待ち・キュー待ちなど）"""
    if not frames:
        return True
    if any(os.path.basename(f.filename).startswith("futaba2b_") for f in frames):
        return False
    last = frames[-1]
    return (os.path.basename(last.filename), last.name) in _STD_IDLE


def stack_lines(skip_ident=None, main_depth: int = 30, other_depth: int = 12) -> list:
    """全スレッドの今の処理場所（新しい呼び出しが下）"""
    names = {t.ident: t.name for t in threading.enumerate()}
    main_ident = threading.main_thread().ident
    frames = sys._current_frames()
    out = []
    idle = 0
    # 画面のスレッドを先に
    order = sorted(frames, key=lambda i: (i != main_ident, names.get(i, "")))
    for ident in order:
        if ident == skip_ident:
            continue
        stack = traceback.extract_stack(frames[ident])
        if ident != main_ident and _is_idle(stack):
            idle += 1
            continue
        depth = main_depth if ident == main_ident else other_depth
        title = names.get(ident, f"0x{ident:x}")
        if ident == main_ident:
            title += "（画面）"
        out.append(f"{TAG} ── {title}")
        shown = stack[-depth:]
        if len(stack) > len(shown):
            out.append(f"    …（上に {len(stack) - len(shown)} 段）")
        for f in shown:
            out.append(f'    {f.filename}:{f.lineno} {f.name}')
            if f.line:
                out.append(f"        {f.line.strip()}")
    if idle:
        out.append(f"{TAG} ── ほかに待機中のスレッド {idle} 本（略）")
    return out


def _describe(w) -> str:
    """窓・部品を人が見て分かる形に"""
    try:
        cls = type(w).__name__
        top = w.window() if w is not None else None
        title = (top.windowTitle() if top is not None else "") or ""
        g = top.frameGeometry() if top is not None else w.geometry()
        on_screen = any(s.availableGeometry().intersects(g)
                        for s in QGuiApplication.screens())
        parts = [cls]
        if title:
            parts.append(f"「{title}」")
        parts.append("見えている" if w.isVisible() else "隠れている")
        if top is not None and top.isMinimized():
            parts.append("最小化")
        parts.append(f"位置=({g.x()},{g.y()},{g.width()}x{g.height()})")
        parts.append("画面内" if on_screen else "画面の外")
        return " ".join(parts)
    except RuntimeError:
        return "（もう消えた部品）"
    except Exception as e:
        return f"（調べられない: {e}）"


def _suspicious_window(w) -> bool:
    """見えない／画面の外／最小化の窓か（確認窓・メニュー用）"""
    try:
        top = w.window()
        if not w.isVisible() or top.isMinimized():
            return True
        g = top.frameGeometry()
        return not any(s.availableGeometry().intersects(g)
                       for s in QGuiApplication.screens())
    except RuntimeError:
        return False
    except Exception:
        return False


class FreezeWatch(QObject):
    """固まった時の記録。start() で始め、stop() で止める（終了処理の最初に）"""

    BEAT_MS = 500            # 画面の側の合図の間隔
    STALL_SEC = 3.0          # これだけ合図が無ければ記録する
    LATER_SEC = (15.0, 60.0)  # 止まり続けていたら、もう一度記録する時点
    NATIVE_SEC = 7.0         # 見張りのスレッドも動けない時の保険（faulthandler）
    NATIVE_REARM_SEC = 2.0   # 保険を掛け直す間隔
    BLOCK_SEC = 10.0         # 確認窓・つかみっぱなしを書くまでの長さ
    PAGE_EVERY_SEC = 4.0     # 表示の反応を見る間隔
    PAGE_SEC = 8.0           # 表示の返事をこれだけ待ったら書く

    def __init__(self, pages_fn=None, parent=None):
        super().__init__(parent)
        self._pages_fn = pages_fn
        self._timer = QTimer(self)
        self._timer.setInterval(self.BEAT_MS)
        self._timer.timeout.connect(self._beat)
        self._last = time.monotonic()      # 最後に画面の側で合図を出した時刻
        self._stall_of = None              # 記録中の止まり（その時の _last）
        self._dumps = 0                    # その止まりで何回書いたか
        self._armed_at = 0.0               # faulthandler の保険を掛けた時刻
        self._stop_ev = threading.Event()
        self._thread = None
        self._blocks = {}                  # 種類 → [部品, 出た時刻, 書いたか]
        self._pings = {}                   # id(page) → [page, ラベル, 送った時刻, 書いたか, 最後に見た時刻]
        self._page_at = 0.0
        self.records = 0                   # 書いた回数（確認用）

    # ── 始める・止める ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._last = time.monotonic()
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._watch, name="2BP-freeze-watch",
                                        daemon=True)
        self._thread.start()
        self._timer.start()
        self._say([f"{TAG} 見張りを始めました（画面の処理が {self.STALL_SEC:g} 秒以上"
                   f"止まったら、その時の処理場所をここに書きます）"])

    def stop(self) -> None:
        self._stop_ev.set()
        try:
            self._timer.stop()
        except RuntimeError:
            pass
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        self._thread = None

    # ── 画面の側（0.5 秒ごと）────────────────────────────────────────────
    def _beat(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        self._last = now
        if self._stall_of is not None:
            self._stall_of = None
            self._dumps = 0
            self._say([f"{TAG} 戻りました。画面の処理が {gap:.1f} 秒止まっていました"])
        elif gap >= self.STALL_SEC + 1.0:
            # 見張りのスレッドが書けないまま戻った（Python を通らない所で止まり、
            # 見張りも動けなかった）。長ければテキストファイルに faulthandler の分がある
            self._say([f"{TAG} 画面の処理が {gap:.1f} 秒止まっていました"
                       f"（その間は見張りも動けませんでした）"])
        self._arm_native(now)
        self._check_blocks(now)
        if now - self._page_at >= self.PAGE_EVERY_SEC:
            self._page_at = now
            self._check_pages(now)

    def _arm_native(self, now: float) -> None:
        """見張りのスレッドも動けない時の保険。テキストファイルにだけ書かれる"""
        if now - self._armed_at < self.NATIVE_REARM_SEC:
            return
        self._armed_at = now
        fd = _log_fd()
        try:
            if fd >= 0:
                faulthandler.dump_traceback_later(self.NATIVE_SEC, repeat=False, file=fd)
            else:
                faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass

    # ── 見張りのスレッド ────────────────────────────────────────────────────
    def _watch(self) -> None:
        due = (self.STALL_SEC,) + tuple(self.LATER_SEC)
        me = threading.get_ident()
        prev = time.monotonic()
        while not self._stop_ev.wait(0.25):
            now = time.monotonic()
            late, prev = (now - prev > 1.0), now
            if late:
                # 見張り自身が動けなかった（画面の側が Python を握ったまま止まって
                # いた）。今ちょうど戻ったところかもしれず、ここで書くと戻った後の
                # 処理場所を「止まっている所」として書いてしまう。次の回で見る
                # （戻っていれば画面の側が「見張りも動けませんでした」と書く）
                continue
            last = self._last
            gap = now - last
            if gap < self.STALL_SEC:
                continue
            if self._last != last:
                continue        # ちょうど今戻った
            if self._stall_of != last:
                self._stall_of, self._dumps = last, 0
            if self._dumps >= len(due) or gap < due[self._dumps]:
                continue
            first = (self._dumps == 0)
            self._dumps += 1
            head = (f"{TAG} 画面の処理が {gap:.1f} 秒止まっています。"
                    f"その時の各スレッドの処理場所（新しい呼び出しが下）:" if first else
                    f"{TAG} まだ止まっています（{gap:.0f} 秒）。今の処理場所:")
            try:
                lines = [head] + stack_lines(skip_ident=me)
            except Exception as e:
                lines = [head, f"{TAG} （処理場所を調べられませんでした: {e}）"]
            self._say(lines)
            if first:
                # 書けたので保険は要らない（二重に書かない）。戻れば掛け直す
                try:
                    faulthandler.cancel_dump_traceback_later()
                except Exception:
                    pass

    def _say(self, lines) -> None:
        self.records += 1
        try:
            import futaba2b_log as _log
            h = _log.hub()
        except Exception:
            h = None
        if h is not None:
            h.add_lines(lines)
        else:
            for ln in lines:
                print(ln, flush=True)

    # ── 確認窓・メニュー・つかみっぱなし ────────────────────────────────────
    def _check_blocks(self, now: float) -> None:
        try:
            modal = QApplication.activeModalWidget()
            popup = QApplication.activePopupWidget()
            mgrab = QWidget.mouseGrabber()
            kgrab = QWidget.keyboardGrabber()
        except RuntimeError:
            return
        # メニューはマウスとキーをつかむので、それはメニューの方で見る
        if mgrab is not None and popup is not None and mgrab is popup:
            mgrab = None
        if kgrab is not None and popup is not None and kgrab is popup:
            kgrab = None
        cur = {
            "確認窓": modal if (modal is not None and _suspicious_window(modal)) else None,
            "メニュー": popup if (popup is not None and _suspicious_window(popup)) else None,
            "マウスのつかみ": mgrab,
            "キー入力のつかみ": kgrab,
        }
        for kind, w in cur.items():
            rec = self._blocks.get(kind)
            if w is None:
                if rec is not None:
                    if rec[2]:
                        self._say([f"{TAG} {kind}が無くなりました（{now - rec[1]:.0f} 秒）"])
                    del self._blocks[kind]
                continue
            if rec is None or rec[0] is not w:
                self._blocks[kind] = [w, now, False]
                continue
            if not rec[2] and now - rec[1] >= self.BLOCK_SEC:
                rec[2] = True
                self._say([f"{TAG} {kind}が {now - rec[1]:.0f} 秒続いています"
                           f"（ほかの操作が効かない原因かもしれません）: {_describe(w)}"])

    # ── スレ・カタログの表示（Chromium）────────────────────────────────────
    def _check_pages(self, now: float) -> None:
        if self._pages_fn is None:
            return
        try:
            pages = list(self._pages_fn() or [])
        except Exception:
            pages = []
        seen = set()
        for label, page in pages:
            key = id(page)
            seen.add(key)
            rec = self._pings.get(key)
            if rec is not None and rec[0] is not page:
                rec = None          # 同じ id の別物（前のものは消えた）
            if rec is not None:
                rec[4] = now
                if not rec[3] and now - rec[2] >= self.PAGE_SEC:
                    rec[3] = True
                    self._say([f"{TAG} 表示（{rec[1]}）が {now - rec[2]:.0f} 秒反応していません"
                               f"（画面の処理は動いています）"])
                continue
            try:
                page.runJavaScript("0", 0, lambda _r, k=key: self._pong(k))
            except RuntimeError:
                continue
            except Exception:
                continue
            self._pings[key] = [page, label, now, False, now]
        # 見えなくなった（閉じた・切り替えた）ものは、しばらくして忘れる
        for key in [k for k, r in self._pings.items()
                    if k not in seen and now - r[4] >= 15.0]:
            del self._pings[key]

    def _pong(self, key) -> None:
        rec = self._pings.pop(key, None)
        if rec is not None and rec[3]:
            self._say([f"{TAG} 表示（{rec[1]}）が戻りました（{time.monotonic() - rec[2]:.0f} 秒）"])
