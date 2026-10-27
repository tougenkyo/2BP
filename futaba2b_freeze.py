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
   返事はあるのに描画（画面への描き出し）だけが止まっている時も書く。GPU の
   処理は 2BP の中で動いていて、そこが止まると画面の処理も中身も動いたまま
   表示だけが止まる（スクロールしても動かない）。送る JS で描画の合図
   （requestAnimationFrame）を1回だけ頼み、次に送った時に、それが来たかを見る
   （描き続ける仕掛けは置かない）。窓が最小化されている時・2BP 以外の窓を
   使っている時・ページが見えていない時は、描かないのが普通なので数えない。

ほかに、メモリの掃除（gc.collect）にかかった時間も測る（gc_collect_timed）。
掃除の仕方・時機は変えない。1 回で 1 秒以上かかった時はその場で、ほかは
30 分ごとにまとめて書く。行の頭は「[GC]」。30 分ごとに、抱えている
オブジェクトの数と増えた種類・メモリの様子も書く（掃除が日ごとに長くなる
理由を見分けるため）。
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
    DRAW_SEC = 8.0           # 返事はあるのに描画がこれだけ止まっていたら書く

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
        # id(page) → [page, ラベル, 描画の合図の回数, 見えていたか, 止まり始め, 書いたか, 返事の時刻]
        self._draws = {}
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
                page.runJavaScript(_PING_JS, 0, lambda r, k=key: self._pong(k, r))
            except RuntimeError:
                continue
            except Exception:
                continue
            self._pings[key] = [page, label, now, False, now]
        # 見えなくなった（閉じた・切り替えた）ものは、しばらくして忘れる
        for key in [k for k, r in self._pings.items()
                    if k not in seen and now - r[4] >= 15.0]:
            del self._pings[key]
        for key, d in list(self._draws.items()):
            if key in seen:
                continue
            if now - d[6] >= 15.0:
                del self._draws[key]
            else:
                d[3] = "unseen"     # 見えていない間は描かないのが普通。戻ったら数え直す

    def _pong(self, key, r=None) -> None:
        rec = self._pings.pop(key, None)
        if rec is not None and rec[3]:
            self._say([f"{TAG} 表示（{rec[1]}）が戻りました（{time.monotonic() - rec[2]:.0f} 秒）"])
        if rec is not None:
            self._check_draw(key, rec[0], rec[1], r)

    def _check_draw(self, key, page, label: str, r) -> None:
        """返事（"描画の合図の回数,見えているか"）から、描画が止まっていないかを見る。
        前に送った時に頼んだ描画の合図が、今になっても来ていなければ止まっている"""
        now = time.monotonic()
        try:
            _c, _v = str(r).split(",", 1)
            cnt, vis = int(_c), _v.strip()
        except Exception:
            self._draws.pop(key, None)   # 返事の形が違う（読み込み中など）
            return
        if vis == "visible":
            if not _app_active():
                vis = "inactive"        # 2BP 以外を使っている（画面のロック中も）
            elif _page_minimized(page):
                vis = "minimized"
        d = self._draws.get(key)
        if d is None or d[0] is not page or now - d[6] > self.PAGE_EVERY_SEC * 2.5:
            # 初めて・別のページ・前の返事から間が空いた → ここから数える
            self._draws[key] = [page, label, cnt, vis, None, False, now]
            return
        stalled = (vis == "visible" and d[3] == "visible" and cnt == d[2])
        if stalled:
            if d[4] is None:
                d[4] = d[6]             # 前に頼んだ時から一度も描かれていない
            if not d[5] and now - d[4] >= self.DRAW_SEC:
                d[5] = True
                self._say([f"{TAG} 描画（{label}）が {now - d[4]:.0f} 秒止まっています"
                           f"（中身と画面の処理は動いています。GPU の処理が止まっている"
                           f"疑いがあります）"])
        else:
            if d[5]:
                self._say([f"{TAG} 描画（{label}）が戻りました（{now - d[4]:.0f} 秒）"])
            d[4] = None
            d[5] = False
        d[1], d[2], d[3], d[6] = label, cnt, vis, now


# 表示の反応を見る JS。返事は "描画の合図の回数,見えているか"（配列で返すと
# Python 側に届かない事があるので文字列にする）。描画の合図
# （requestAnimationFrame）は、まだ来ていない分が無い時だけ1回頼む（描き続けない）
_PING_JS = (
    "(function(){var w=window,c=w.__2bpRaf||0;"
    "if(!w.__2bpRafReq){w.__2bpRafReq=1;"
    "requestAnimationFrame(function(){w.__2bpRafReq=0;w.__2bpRaf=(w.__2bpRaf||0)+1;});}"
    "return c+','+document.visibilityState;})()"
)


def _app_active() -> bool:
    """2BP の窓を使っているか（2BP 以外を使っている・画面のロック中は False）"""
    try:
        from PySide6.QtCore import Qt
        return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive
    except Exception:
        return True


def _page_minimized(page) -> bool:
    """そのページを出している窓が最小化されているか"""
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView
        v = QWebEngineView.forPage(page)
        return v is not None and v.window().isMinimized()
    except Exception:
        return False


# ── メモリの掃除にかかった時間 ───────────────────────────────────────────────
# 2BP は Python の自動の掃除を止め、画面の処理で 10 秒ごとに全体を掃除している
# （Qt の部品を画面以外のスレッドで壊さないため）。全体の掃除は使っている量が
# 多いほど長くなり、その間は画面が止まる（利用者のサブPCで 3.4 秒の記録）。
# 掃除の仕方を変える前に、実際に何秒かかっているかを見る。
# サブPCでは掃除が日ごとに長くなった（1日目の平均 0.05 秒が6日目に 0.5 秒。
# 片付けた数はほぼ0）。抱えている物が増えたのか、Windows が退避したメモリを
# 掃除のたびに読み戻しているのかを見分けるため、次も書く。
#   ・掃除のたびに、その間のページの読み込みの回数と、使っているメモリの増え方
#     （退避したメモリを読み戻していれば、ここが大きくなる）
#   ・30 分ごとに、抱えているオブジェクト（掃除で調べる物）の数と、増えた種類
#     （数えるのは画面の処理での掃除の時だけ。掃除1回より少し長くかかる）
#   ・30 分ごとに、確保しているメモリとパソコンの空き
GC_TAG = "[GC]"
GC_SLOW_SEC = 1.0           # 1 回でこれ以上かかったら、その場で書く
GC_SUMMARY_SEC = 30 * 60    # まとめを書く間隔
GC_NOTE_SEC = 0.2           # まとめで「長め」と数える長さ
GC_CENSUS_TOP = 8           # 増えた種類を、多い順にいくつまで書くか
_gc_lock = threading.Lock()
_gc_stats: dict = {}        # どこでの掃除か → [回数, 合計秒, 最長秒, 長めの回数, ページの読み込み]
_gc_since = time.monotonic()
_gc_pf_since = None         # 前のまとめの時のページの読み込みの回数（プロセス全体）
_census_due = False         # 抱えている物を数える番（画面の処理での掃除の時に数える）
_census_prev = None         # 前に数えた時の {種類: 数}
_census_first = None        # 最初に数えた時の {種類: 数}
_census_total_prev = 0
_census_total_first = 0
_psproc = None


def _mem_probe():
    """(ページの読み込みの回数, 使っているメモリMB, 確保しているメモリMB)。
    取れなければ None。回数・確保は取れない環境では None になる"""
    global _psproc
    try:
        if _psproc is None:
            import psutil
            _psproc = psutil.Process(os.getpid())
        mi = _psproc.memory_info()
        priv = getattr(mi, "private", None)
        return (getattr(mi, "num_page_faults", None), mi.rss / 1048576,
                priv / 1048576 if priv is not None else None)
    except Exception:
        return None


def _mem_summary(m) -> str:
    """30 分ごとのメモリの様子の1行（取れなければ空）"""
    if not m:
        return ""
    parts = [f"使っているメモリ {m[1]:.0f}MB"]
    if m[2] is not None:
        parts.append(f"確保しているメモリ {m[2]:.0f}MB")
    try:
        import psutil
        vm = psutil.virtual_memory()
        parts.append(f"パソコンの空き {vm.available / 2**30:.1f}GB／{vm.total / 2**30:.1f}GB")
    except Exception:
        pass
    if _gc_pf_since is not None and m[0] is not None:
        parts.append(f"この間のページの読み込み {m[0] - _gc_pf_since:,} 回")
    return f"{GC_TAG} メモリの様子: " + "・".join(parts)


def _type_name(t) -> str:
    mod = getattr(t, "__module__", "") or ""
    name = getattr(t, "__qualname__", None) or getattr(t, "__name__", "?")
    if mod in ("builtins", ""):
        return name
    return f"{mod}.{name}".replace("PySide6.", "").replace("shiboken6.", "")


def _census_lines() -> list:
    """抱えているオブジェクト（掃除で調べる物）を種類ごとに数え、前回・最初と比べる。
    画面の処理からだけ呼ぶ（取り出した一覧を手放す時に、そこが最後の持ち主だった
    物が壊れる。それを画面の処理で起こすため。掃除を画面の処理に寄せたのと同じ理由）"""
    import gc
    import collections
    global _census_prev, _census_first, _census_total_prev, _census_total_first
    t0 = time.perf_counter()
    objs = gc.get_objects()
    total = len(objs)
    cnt = collections.Counter(map(type, objs))
    del objs
    cur: dict = {}
    for t, c in cnt.items():
        k = _type_name(t)
        cur[k] = cur.get(k, 0) + c
    del cnt
    dt = time.perf_counter() - t0

    def _grown(base):
        inc = sorted(((c - base.get(k, 0), k, c) for k, c in cur.items()), reverse=True)
        s = "、".join(f"{k} +{d:,}（計 {c:,}）" for d, k, c in inc[:GC_CENSUS_TOP] if d > 0)
        return s or "なし"

    lines = []
    if _census_prev is None:
        top = sorted(((c, k) for k, c in cur.items()), reverse=True)[:GC_CENSUS_TOP]
        lines.append(f"{GC_TAG} 抱えているオブジェクト {total:,} 個（数えるのに {dt:.2f} 秒）"
                     "・多い種類: " + "、".join(f"{k} {c:,}" for c, k in top))
        _census_first = cur
        _census_total_first = total
    else:
        lines.append(f"{GC_TAG} 抱えているオブジェクト {total:,} 個"
                     f"（前回より {total - _census_total_prev:+,}・最初より {total - _census_total_first:+,}"
                     f"・数えるのに {dt:.2f} 秒）・前回より増えた種類: {_grown(_census_prev)}")
        lines.append(f"{GC_TAG} 最初に数えた時より増えた種類: {_grown(_census_first)}")
    _census_prev = cur
    _census_total_prev = total
    return lines


def gc_collect_timed(where: str) -> int:
    """gc.collect() をして、かかった時間を測る（掃除の中身は gc.collect() と同じ）。
    where は「定期」など、どこでの掃除か（ログに出す）。戻り値も gc.collect() と同じ"""
    import gc
    m0 = _mem_probe()
    t0 = time.perf_counter()
    n = gc.collect()
    dt = time.perf_counter() - t0
    m1 = _mem_probe()
    pf = (m1[0] - m0[0]) if (m0 and m1 and m0[0] is not None and m1[0] is not None) else None
    lines = []
    census = False
    global _gc_since, _gc_pf_since, _census_due
    with _gc_lock:
        if _gc_pf_since is None and m0 and m0[0] is not None:
            _gc_pf_since = m0[0]
        st = _gc_stats.setdefault(where, [0, 0.0, 0.0, 0, 0])
        st[0] += 1
        st[1] += dt
        st[2] = max(st[2], dt)
        if dt >= GC_NOTE_SEC:
            st[3] += 1
        if pf:
            st[4] += pf
        now = time.monotonic()
        if now - _gc_since >= GC_SUMMARY_SEC:
            parts = [f"{w} {c} 回・平均 {tot / c:.2f} 秒・最長 {mx:.2f} 秒"
                     f"（{GC_NOTE_SEC:g} 秒以上 {nl} 回）"
                     + (f"・ページの読み込み 平均 {pfs // c:,} 回" if pfs else "")
                     for w, (c, tot, mx, nl, pfs) in _gc_stats.items() if c]
            if parts:
                lines.append(f"{GC_TAG} この {int((now - _gc_since) // 60)} 分のメモリの掃除: "
                             + "／".join(parts))
            mem = _mem_summary(m1)
            if mem:
                lines.append(mem)
            _gc_stats.clear()
            _gc_since = now
            if m1 and m1[0] is not None:
                _gc_pf_since = m1[0]
            _census_due = True
        if _census_due and threading.current_thread() is threading.main_thread():
            _census_due = False
            census = True
    if dt >= GC_SLOW_SEC:
        mem = ""
        if m1:
            mem += f"・使っているメモリ {m1[1]:.0f}MB"
            if m0 and m1[1] - m0[1] >= 1:
                mem += f"・掃除の間に増えた分 +{m1[1] - m0[1]:.0f}MB"
        if pf is not None:
            mem += f"・ページの読み込み {pf:,} 回"
        lines.insert(0, f"{GC_TAG} メモリの掃除に {dt:.1f} 秒かかりました"
                        f"（{where}・片付けた数 {n}{mem}）")
    if census:
        try:
            lines.extend(_census_lines())
        except Exception as e:
            lines.append(f"{GC_TAG} 抱えているオブジェクトを数えられませんでした: {e}")
    for ln in lines:
        print(ln, flush=True)
    return n
