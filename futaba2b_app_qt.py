"""
futaba2b_app_qt.py ─ PySide6 + QtWebEngine 版 メインアプリケーション
v0.6.011
"""
from __future__ import annotations
import sys, re, time, threading, webbrowser, urllib.parse

def _open_url(url: str) -> None:
    """URLを外部ブラウザで確実に開く。
    QDesktopServices.openUrl が正攻法（GUIスレッドからShellExecuteを正しく呼ぶ）。
    cmd start 経由は & によるURL切断や隠しエラーダイアログ（警告音）の問題があるため使わない。
    必ずメインスレッドから呼ぶこと（Qtイベントループ内）。"""
    if not url:
        return
    # HTMLエンティティが混入している場合に備えて復元
    if "&amp;" in url:
        url = url.replace("&amp;", "&")
    # ふたばの外部リンクラッパー「…bin/jump.php?実URL」を除去
    if "jump.php?" in url:
        url = url.split("jump.php?", 1)[1]
    # ① Qt標準（最も確実）
    try:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl as _QUrl
        if QDesktopServices.openUrl(_QUrl(url)):
            return
    except Exception:
        pass
    # ② Windows: os.startfile（ShellExecute直接）
    import sys as _sys
    if _sys.platform == "win32":
        try:
            import os as _os
            _os.startfile(url)
            return
        except Exception:
            pass
    # ③ フォールバック: webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _video_cache_valid(path) -> bool:
    """動画キャッシュファイルのヘッダを簡易検証する。
    途中で切れた破損ファイル（旧版の直接書き込みやクラッシュの残骸）を
    FFmpegに渡すと 0xC0000005 でプロセスごと落ちるため、再生前に弾く。"""
    try:
        from pathlib import Path as _P
        p = _P(path)
        if not p.exists() or p.stat().st_size < 1024:   # 1KB未満は問答無用で無効
            return False
        with open(p, 'rb') as f:
            head = f.read(12)
        if len(head) < 12:
            return False
        # MP4/MOV系: 4バイト目からboxタイプ
        if head[4:8] in (b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip', b'styp'):
            return True
        # WebM/MKV: EBMLヘッダ
        if head[:4] == b'\x1a\x45\xdf\xa3':
            return True
        return False
    except OSError:
        return False
from pathlib import Path

from PySide6.QtCore    import Qt, QUrl, QTimer, QObject, Signal, Slot, QSize, QRect, QEvent
from PySide6.QtGui     import QAction, QKeySequence, QColor, QShortcut, QIcon, QPixmap, QImage, QGuiApplication, QPainter, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QToolButton, QWidget, QSplitter, QVBoxLayout,
    QHBoxLayout, QToolBar, QLabel, QLineEdit, QPushButton, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QMessageBox, QDialog, QFormLayout,
    QSpinBox, QCheckBox, QComboBox, QTextEdit, QSizePolicy, QStatusBar,
    QGroupBox, QDialogButtonBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMenu, QListWidget, QListWidgetItem,
    QTabBar, QScrollArea, QInputDialog, QButtonGroup, QRadioButton, QStyle,
    QStackedWidget, QFrame,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore    import (
    QWebEngineProfile, QWebEnginePage, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo,
)
from PySide6.QtWebChannel import QWebChannel

from futaba2b_models   import BoardInfo, BoardCategory, AutoRefreshEntry, CatalogEntry
from futaba2b_network  import FutabaFetcher
from futaba2b_settings import AppSettings, NgFilter
from futaba2b_html     import thread_to_html, catalog_to_html, render_res, THREAD_CSS, WEBCHANNEL_JS
from futaba2b_bridge   import ThreadBridge, CatalogBridge
from futaba2b_const    import UA, ThemeManager as _TM


def _play_ng_se() -> None:
    """NG/逆NG通知の効果音 theme/ng_se.wav を非同期再生する。
    テーマフォルダ優先 → theme/直下 fallback。"""
    import threading as _th

    def _play():
        try:
            import sys as _sys, subprocess as _sp
            _theme_root = Path(__file__).parent / "theme"
            wav = None
            for _d in [_TM.theme_dir(), _theme_root]:
                _w = _d / "ng_se.wav"
                if _w.exists():
                    wav = str(_w)
                    break
            if not wav:
                return
            if _sys.platform == "win32":
                import winsound
                winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                _sp.Popen(["aplay", wav],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
    _th.Thread(target=_play, daemon=True).start()


APP_VER = "0.9.272"

# ── アプリ終了中フラグ ───────────────────────────────────────────────────────
# 終了処理(closeEvent)で立てる。自動更新など「バックグラウンドスレッド起点で
# メインスレッドのビューを触る」処理は、終了中はWebEnginePage/Profileが破棄され
# つつあり、isValid=True でも実体が壊れかけているためアクセスするとネイティブ
# クラッシュ(0xC0000005)を起こす。各コールバックの冒頭でこのフラグを見て黙る。
_APP_SHUTTING_DOWN = False


def app_is_shutting_down() -> bool:
    return _APP_SHUTTING_DOWN


def set_app_shutting_down(flag: bool = True) -> None:
    global _APP_SHUTTING_DOWN
    _APP_SHUTTING_DOWN = flag


# ── グローバルfetchスレッドプール（ThreadView・AR共用、同時実行数を制限） ──
from concurrent.futures import ThreadPoolExecutor as _TPE
try:
    from shiboken6 import isValid as _sb_valid   # C++オブジェクト生存判定
except Exception:
    _sb_valid = None


def _safe_run_js(view, js, cb=None) -> bool:
    """破棄済みビューへの runJavaScript を安全に行う。
    タブを閉じた後に QTimer/非同期コールバックが遅延実行され
    「libshiboken: Internal C++ object already deleted」で未処理例外になるのを防ぐ。
    実行できたら True、破棄済み等でスキップしたら False を返す。"""
    try:
        if view is None:
            return False
        if _sb_valid is not None and not _sb_valid(view):
            return False
        if cb is None:
            view.page().runJavaScript(js)
        else:
            view.page().runJavaScript(js, cb)
        return True
    except RuntimeError:
        return False
_FETCH_POOL = _TPE(max_workers=3, thread_name_prefix='2BP_fetch')

class _NoWheelSpinBox(QSpinBox):
    """スクロールで値が変わらない QSpinBox"""
    def wheelEvent(self, event): event.ignore()

class _NoWheelComboBox(QComboBox):
    """スクロールで値が変わらない QComboBox"""
    def wheelEvent(self, event): event.ignore()




def _dispose_tab_view(w):
    """閉じたタブのビューをUIスレッドで確実に破棄するヘルパー。
    removeTab() はウィジェットを削除しないため、参照が切れたビューは
    Python の循環GC任せになる。循環GCは任意のスレッド（自動保存・画像DLの
    BGスレッド等）で走るため、QWebEngineView/Page/Profile が非GUIスレッドで
    破壊され 0xC0000005 クラッシュの原因になる。
    cleanup() + deleteLater() で破棄をメインイベントループに委ね、これを防ぐ。"""
    if w is None:
        return
    try:
        if hasattr(w, 'cleanup'):
            w.cleanup()
    except Exception:
        pass
    try:
        w.deleteLater()
    except Exception:
        pass
    _schedule_gc()   # 破棄後に遅延GCで循環参照(BS4/Qt)を回収しRSSを下げる


_gc_debounce_timer = None
def _schedule_gc(delay_ms: int = 4000):
    """タブ破棄後、最後の呼び出しから delay_ms 後に1回だけ gc.collect() する。
    BeautifulSoup/Qt の循環参照を回収して RSS を下げる。閉じる操作中の同期GC負荷を
    避けるため遅延＋デバウンス（連続クローズでも1回）。profile の遅延削除(3秒)後に
    回収できるよう既定4秒。"""
    global _gc_debounce_timer
    try:
        from PySide6.QtCore import QTimer
        if _gc_debounce_timer is None:
            _gc_debounce_timer = QTimer()
            _gc_debounce_timer.setSingleShot(True)
            def _do_gc():
                import gc as _gc
                _gc.collect()
            _gc_debounce_timer.timeout.connect(_do_gc)
        _gc_debounce_timer.start(delay_ms)
    except Exception:
        pass


def _make_prefetch_destroy_cb(fetcher, holder):
    """ビュー破棄(destroyed)時に、そのスレの未着手先読みを中断するコールバックを返す。
    self を捕捉しないこと（破棄中オブジェクト参照を避ける）。fetcher と holder のみ捕捉する。
    cleanup() を通らない破棄経路（widget/GCカスケード）でも確実に発火させるための保険。"""
    def _cb(*_a):
        try:
            g = holder[0] if holder else ""
            if g:
                fetcher.cancel_prefetch(g)
        except Exception:
            pass
    return _cb


def _cleanup_tmp(path: str):
    """一時HTMLファイルを削除するヘルパー"""
    if path:
        import os
        try:
            os.unlink(path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# リクエストインターセプター
# ══════════════════════════════════════════════════════════════════════════════


# ─ 多段タブバー ────────────────────────────────────────────────────────────
_ROW_H = 26

_JSERR_SEEN: set = set()      # 同一エラーの重複保存を防ぐ


def _save_js_error_source(src: str, line, msg: str) -> None:
    """JSエラーが出たページのHTMLを logs/jserr/ に退避する（原因調査用）。
    生成HTMLは一時ファイルで次のロードまで残っているため、この時点ならコピーできる。
    「Uncaught SyntaxError: Invalid or unexpected token」のように、どのレス/データが
    JSを壊したのかログだけでは追えないケースを後から解析可能にする。"""
    try:
        if not src:
            return
        key = (src, str(line), (msg or "")[:120])
        if key in _JSERR_SEEN:
            return
        _JSERR_SEEN.add(key)
        if len(_JSERR_SEEN) > 200:
            _JSERR_SEEN.clear()
        import shutil, datetime, urllib.parse
        from pathlib import Path as _P
        p = src
        if p.startswith("file:"):
            p = urllib.parse.unquote(urllib.parse.urlparse(p).path)
            if p.startswith("/") and len(p) > 2 and p[2] == ":":
                p = p[1:]                      # Windows: /C:/... → C:/...
        f = _P(p)
        if not f.is_file():
            print(f"[JSERR] 原因HTMLが見つからず保存できません: {src}", flush=True)
            return
        out_dir = _P("logs/jserr")
        out_dir.mkdir(parents=True, exist_ok=True)
        # 溜まりすぎ防止: 古いものから消して最大20組に保つ
        olds = sorted(out_dir.glob("jserr_*.html"), key=lambda x: x.stat().st_mtime)
        for o in olds[:-19]:
            for _t in (o, o.with_suffix(".txt")):
                try: _t.unlink()
                except OSError: pass
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dst = out_dir / f"jserr_{stamp}.html"
        shutil.copyfile(str(f), str(dst))
        dst.with_suffix(".txt").write_text(
            f"src : {src}\nline: {line}\nmsg : {msg}\n", encoding="utf-8")
        print(f"[JSERR] 原因HTMLを保存しました: {dst}", flush=True)
    except Exception as e:
        print(f"[JSERR] 保存失敗: {e}", flush=True)


class _DebugPage(QWebEnginePage):
    """JS の console.log/warn/error を Python stdout に転送する"""
    def javaScriptConsoleMessage(self, level, msg, line, src):
        tag = {0: "LOG", 1: "WARN", 2: "ERR", 3: "INFO"}.get(level, "JS")
        src_short = src.split("/")[-1] if src else ""
        print(f"js [{tag}] {src_short}:{line}  {msg}", flush=True)
        # 構文エラー/未捕捉例外は原因HTMLが無いと追跡できないため退避しておく
        _m = msg or ""
        if "SyntaxError" in _m or "Uncaught" in _m:
            _save_js_error_source(src, line, _m)


class _ImageWebView(QWebEngineView):
    """ImageTabView 用 QWebEngineView：右クリックメニューをカスタマイズする。"""
    copy_image_requested = Signal(str)   # 画像URLを親に通知

    def _current_remote_url(self) -> str:
        """表示中画像の元(リモート)URL。ローカルキャッシュ表示時はページ/メディアが
        file:// になるため、親(ImageTabView)の img_list から元URLを取得する。"""
        p = self.parent()
        try:
            il = getattr(p, "_img_list", None); ix = getattr(p, "_idx", -1)
            if il and 0 <= ix < len(il):
                return il[ix].get("url", "") or ""
        except Exception:
            pass
        return ""

    def contextMenuEvent(self, event):
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest
        from PySide6.QtWidgets import QMenu
        req = self.lastContextMenuRequest()
        on_image = (req is not None and
                    req.mediaType() == QWebEngineContextMenuRequest.MediaType.MediaTypeImage)

        # 削除するアクションのテキスト（画像外・画像内共通で不要なもの）
        REMOVE_ALWAYS = {"Back", "Forward", "Reload", "Save page",
                         "View page source", "&Back", "&Forward", "&Reload"}
        # 画像上のみ削除
        REMOVE_ON_IMAGE = {"Copy image"}

        std = self.createStandardContextMenu()
        keep = []
        save_image_act = None
        copy_img_addr_act = None
        for act in std.actions():
            t = act.text().replace("&", "")
            if t in REMOVE_ALWAYS:
                continue
            if on_image and t in REMOVE_ON_IMAGE:
                continue
            if t == "Save image":
                save_image_act = act
                continue
            if t == "Copy image address":
                copy_img_addr_act = act
                continue
            keep.append(act)

        menu = QMenu(self)
        if on_image:
            # 画像上: 外部で開く → 画像を保存 → 画像をコピー → 画像アドレスをコピー の順
            # ローカルキャッシュ表示時はページ/メディアが file:// になるため、
            # 各操作は親の img_list から得た元(リモート)URLを優先して使う。
            remote_url = self._current_remote_url()
            img_url = remote_url or (req.mediaUrl().toString() if req else "")
            act_ext = menu.addAction("外部で開く")
            act_ext.triggered.connect(lambda: __import__('webbrowser').open(img_url))
            menu.addSeparator()
            if save_image_act:
                save_image_act.setText("画像を保存")
                menu.addAction(save_image_act)
            copy_act = menu.addAction("画像をコピー")
            copy_act.triggered.connect(
                lambda: self.copy_image_requested.emit(img_url))
            def _copy_addr(_u=img_url):
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setText(_u)
            addr_act = menu.addAction("画像アドレスをコピー")
            addr_act.triggered.connect(_copy_addr)
            if keep:
                menu.addSeparator()
                for act in keep:
                    menu.addAction(act)
        else:
            for act in keep:
                menu.addAction(act)

        menu.exec(event.globalPos())


class _JapaneseLineEdit(QLineEdit):
    """URLバー用 QLineEdit：右クリックメニューを日本語化する。"""

    def contextMenuEvent(self, event):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        has_sel  = bool(self.selectedText())
        has_text = bool(self.text())
        cb_text  = QGuiApplication.clipboard().text()

        act_undo = menu.addAction("元に戻す\tCtrl+Z")
        act_undo.triggered.connect(self.undo)
        act_undo.setEnabled(self.isUndoAvailable())

        act_redo = menu.addAction("やり直す\tCtrl+Y")
        act_redo.triggered.connect(self.redo)
        act_redo.setEnabled(self.isRedoAvailable())

        menu.addSeparator()

        act_cut = menu.addAction("切り取り\tCtrl+X")
        act_cut.triggered.connect(self.cut)
        act_cut.setEnabled(has_sel and not self.isReadOnly())

        act_copy = menu.addAction("コピー\tCtrl+C")
        act_copy.triggered.connect(self.copy)
        act_copy.setEnabled(has_sel)

        act_paste = menu.addAction("貼り付け\tCtrl+V")
        act_paste.triggered.connect(self.paste)
        act_paste.setEnabled(bool(cb_text) and not self.isReadOnly())

        act_del = menu.addAction("削除")
        act_del.triggered.connect(lambda: self.insert("") if has_sel else None)
        act_del.setEnabled(has_sel and not self.isReadOnly())

        menu.addSeparator()

        act_sel = menu.addAction("すべて選択\tCtrl+A")
        act_sel.triggered.connect(self.selectAll)
        act_sel.setEnabled(has_text)

        try:
            pos = event.globalPosition().toPoint()
        except AttributeError:
            pos = event.globalPos()
        menu.exec(pos)


class _CatalogWebView(QWebEngineView):
    """CatalogView 用 QWebEngineView：Back/Forward/Reload/Save page を除去する。"""
    _REMOVE = {"Back", "Forward", "Reload", "Save page",
               "&Back", "&Forward", "&Reload"}
    # ソース表示コールバックをセットする（CatalogViewから外から設定）
    _source_callback: "callable | None" = None

    def contextMenuEvent(self, event):
        from PySide6.QtWidgets import QMenu
        std = self.createStandardContextMenu()
        menu = QMenu(self)
        for act in std.actions():
            t = act.text().replace("&", "")
            if t in self._REMOVE:
                continue
            if t == "View page source" and self._source_callback:
                # "View page source" を日本語化してコールバックに差し替え
                act_src = menu.addAction("ソースを表示")
                act_src.triggered.connect(self._source_callback)
                continue
            menu.addAction(act)
        if not menu.isEmpty():
            menu.exec(event.globalPos())


class WrapTabBar(QTabBar):
    """右端でタブを折り返す多段タブバー。"""

    tabCloseRequested = Signal(int)
    _ROW_H  = 26

    @property
    def _C_SEL(self):  return _TM.ui("tab_selected_bg", "#3C3F41")
    @property
    def _C_NRM(self):  return _TM.ui("tab_bg",          "#2B2B2B")
    @property
    def _C_BRD(self):  return _TM.ui("tab_border",      "#555555")
    @property
    def _C_TXT(self):  return _TM.ui("tab_fg",          "#BBBBBB")
    @property
    def _C_STXT(self): return _TM.ui("tab_selected_fg", "#FFFFFF")
    @property
    def _C_CLZ(self):  return _TM.ui("text_muted",      "#888888")
    @property
    def _C_BG(self):   return _TM.ui("window_bg",       "#1E1E1E")

    # ── タブ状態色（theme由来・設定/比較の単一ソース） ──
    # 既定値は従来のハードコード色そのまま。theme.json の ui で上書き可能。
    @classmethod
    def c_error(cls):     return QColor(_TM.ui("tab_error_fg",     "#ff0000"))  # エラー赤(文字)
    @classmethod
    def c_new(cls):       return QColor(_TM.ui("tab_new_fg",       "#4488ff"))  # 新着青(文字)
    @classmethod
    def c_unread_bg(cls): return QColor(_TM.ui("tab_unread_bg",    "#5000b4dc"))  # 未読背景(AARRGGBB)
    @classmethod
    def c_id(cls):        return QColor(_TM.ui("tab_id_fg",        "#ff80c0"))  # ID=ピンク(文字)
    @classmethod
    def c_quar(cls):      return QColor(_TM.ui("tab_quar_fg",      "#ff8800"))  # 隔離=オレンジ(文字)
    @classmethod
    def c_idquar(cls):    return QColor(_TM.ui("tab_id_quar_fg",   "#ff0099"))  # ID+隔離(文字)
    @classmethod
    def c_pin_ind(cls):   return QColor(_TM.ui("tab_pin_indicator","#4caf50"))  # ピン留め緑線
    @classmethod
    def c_pin_icon(cls):  return QColor(_TM.ui("tab_pin_icon",     "#aaccff"))  # ピンアイコン水色

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(False)
        self.setUsesScrollButtons(False)
        self.setDrawBase(False)
        self._cached_rows = 1
        self._tab_colors:   dict = {}   # idx → QColor（文字色: エラー赤・新着青）
        self._tab_bg_colors: dict = {}  # idx → QColor（背景色: 未読水色）
        self._tab_id_set:   set  = set()  # ID表示スレのタブindex（基底色=ピンク）
        self._tab_quar_set: set  = set()  # 隔離スレのタブindex（基底色=オレンジ。ID併発時=#FF0099）
        self._tab_icons:    dict = {}   # idx → QPixmap（タブアイコン）
        self._tab_width_cache: dict = {}  # idx → ((text, has_icon), width)
        self._pinned_widgets: set = set()  # BoardPane._pinnedへの参照（描画用）
        self._pin_pixmap: "QPixmap | None" = None   # テーマのピンアイコン（キャッシュ）
        self._pin_pixmap_loaded = False
        # D&D タブ移動用
        self._drag_idx: int = -1          # ドラッグ開始タブインデックス
        self._drag_start_pos = None       # ドラッグ開始座標
        self._drag_active: bool = False   # ドラッグ中フラグ
        self._drag_widget_order: list = []  # ドラッグ開始時のwidget順（確定用）
        # アクティブタブ切替時にバー全体を再描画（アクティブ行を最下段へ移動するため）
        self.currentChanged.connect(self.update)

    def setTabIcon(self, idx: int, icon):
        """アイコンをローカル辞書に保存して再描画。QPixmap / QIcon どちらも受け取る。"""
        if isinstance(icon, QPixmap):
            pix = icon
        elif isinstance(icon, QIcon):
            # QIcon.pixmap() はデバイスピクセル比の影響で空になることがあるため
            # 先に cacheKey で有効性を確認し、直接サイズ指定で取得する
            sizes = icon.availableSizes()
            if sizes:
                pix = icon.pixmap(sizes[0])
            else:
                pix = icon.pixmap(QSize(16, 16))
        else:
            pix = QPixmap()
        if pix and not pix.isNull():
            # 16x16 にスケール
            if pix.width() != 16 or pix.height() != 16:
                pix = pix.scaled(16, 16,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self._tab_icons[idx] = pix
        else:
            self._tab_icons.pop(idx, None)
        self._tab_width_cache.pop(idx, None)  # キャッシュ無効化
        self.update()



    # ── レイアウト計算 ──────────────────────────────────────────────────────
    def _tab_width(self, i: int) -> int:
        """太字フォントで計算したタブ幅（選択時に見切れないよう太字基準）"""
        # キャッシュ: テキスト・アイコン有無が変わったときだけ再計算
        text = self.tabText(i)
        has_icon = bool(self._tab_icons.get(i))
        cache_key = (text, has_icon)
        cached = self._tab_width_cache.get(i)
        if cached and cached[0] == cache_key:
            return cached[1]
        from PySide6.QtGui import QFont, QFontMetrics
        fnt = QFont(self.font())
        fnt.setBold(True)
        fm = QFontMetrics(fnt)
        # テキスト幅 + 左余白5 + 右余白20(×ボタン分) + アイコン分21
        text_w = fm.horizontalAdvance(text)
        left_pad = 21 if has_icon else 5
        w = max(left_pad + text_w + 20, 80)
        # 設定で最大幅が指定されていればクリップ
        _max_w = getattr(getattr(self, '_settings', None), 'tab_max_width', 0)
        if _max_w and _max_w > 80:
            w = min(w, _max_w)
        self._tab_width_cache[i] = (cache_key, w)
        return w

    def _layout(self, avail: int = 0):
        """幅に合わせてタブを行に振り分ける。"""
        if avail <= 0:
            avail = self.width()
        pw = self.parentWidget().width() if self.parentWidget() else 0
        if pw > avail:
            avail = pw
        if avail <= 0:
            return [list(range(self.count()))] if self.count() else [[]]
        rows, row_w = [[]], 0
        for i in range(self.count()):
            tw = self._tab_width(i)
            if row_w + tw > avail and rows[-1]:
                rows.append([]); row_w = 0
            rows[-1].append(i); row_w += tw
        # アクティブタブのある行を最下段へ（実インデックスは不変・描画順のみ並べ替え）
        if len(rows) > 1:
            cur = self.currentIndex()
            if cur >= 0:
                for ri, row in enumerate(rows):
                    if cur in row:
                        if ri != len(rows) - 1:
                            rows.append(rows.pop(ri))
                        break
        return rows

    def _tab_rects(self):
        # キャッシュ: サイズ・タブ数・テキストが変わらない限り再計算しない
        key = (self.width(), self.count(), self.currentIndex(),
               tuple(self.tabText(i) for i in range(self.count())),
               tuple(bool(self._tab_icons.get(i)) for i in range(self.count())))
        if getattr(self, '_tab_rects_cache_key', None) == key:
            return self._tab_rects_cache_val
        rects = {}
        for ri, row in enumerate(self._layout()):
            x = 0
            for ti in row:
                tw = self._tab_width(ti)
                rects[ti] = QRect(x, ri * self._ROW_H, tw, self._ROW_H)
                x += tw
        self._tab_rects_cache_key = key
        self._tab_rects_cache_val = rects
        return rects

    def _widget_for_paint(self, i: int, parent_tw=None):
        """描画中のタブ位置 i に対応するウィジェットを返す。
        ドラッグ中は _move_tab が表示順のみ入れ替え、stacked の実ウィジェット順は
        リリース時（_sync_stacked_to_tabbar）まで変わらない。そのため
        parent_tw.widget(i) を使うとピンや×が移動前の位置に残ってしまう。
        ドラッグ中は並べ替え済みの _drag_widget_order を優先する。"""
        _wo = self._drag_widget_order
        if _wo and len(_wo) == self.count() and 0 <= i < len(_wo):
            return _wo[i]
        if parent_tw is None:
            parent_tw = self.parentWidget()
        if parent_tw is None:
            return None
        try:
            return parent_tw.widget(i)
        except (RuntimeError, AttributeError):
            return None

    def _idx_at(self, pos):
        for i, r in self._tab_rects().items():
            if r.contains(pos): return i
        return -1

    def _close_rect(self, tab_rect: QRect) -> QRect:
        return QRect(tab_rect.right() - 16, tab_rect.top() + (self._ROW_H - 14) // 2, 14, 14)

    # ── サイズ ──────────────────────────────────────────────────────────────
    def sizeHint(self):
        # 幅は極力大きく返して QTabWidget がフル幅を割り当てるようにする。
        # 破棄途中に呼ばれると parentWidget()/super() が「already deleted」で
        # 例外になるためガードして既定値を返す。
        try:
            pw = self.parentWidget().width() if self.parentWidget() else 0
            w = max(pw, super().sizeHint().width(), 200)
        except RuntimeError:
            return QSize(200, self._ROW_H)
        return QSize(w, getattr(self, "_cached_rows", 1) * self._ROW_H)

    def minimumSizeHint(self):
        return QSize(0, self._ROW_H)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rows = len(self._layout(event.size().width())) or 1
        if rows != getattr(self, "_cached_rows", 1):
            self._cached_rows = rows
            self.updateGeometry()
        self.update()

    def tabLayoutChange(self):
        rows = len(self._layout()) or 1
        if rows != getattr(self, "_cached_rows", 1):
            self._cached_rows = rows
            self.updateGeometry()
        self.update()

    def _refresh_base_color(self, idx: int):
        """基底文字色を反映する。
        優先順位: エラー赤 > {ID+隔離=#FF0099, ID=ピンク, 隔離=オレンジ} > 新着青 > デフォルト。"""
        cur = self._tab_colors.get(idx)
        if cur is not None and cur == self.c_error():
            return  # 赤(エラー)は最優先
        _is_id   = idx in self._tab_id_set
        _is_quar = idx in self._tab_quar_set
        if _is_id and _is_quar:
            self._tab_colors[idx] = self.c_idquar()  # ID+隔離（青より優先）
        elif _is_id:
            self._tab_colors[idx] = self.c_id()  # op-no-id=ピンク（青より優先）
        elif _is_quar:
            self._tab_colors[idx] = self.c_quar()  # 隔離=オレンジ（青より優先）
        elif cur is not None and cur == self.c_new():
            return  # 通常スレの新着青は維持
        elif cur is not None:
            del self._tab_colors[idx]

    def tabRemoved(self, idx: int):
        """タブ削除時に _tab_colors / _tab_icons / _tab_width_cache のインデックスをシフト"""
        super().tabRemoved(idx)
        for d in (self._tab_colors, self._tab_bg_colors, self._tab_icons, self._tab_width_cache):
            new_d = {}
            for k, v in d.items():
                if k < idx:
                    new_d[k] = v
                elif k > idx:
                    new_d[k - 1] = v
            d.clear(); d.update(new_d)
        # _tab_id_set も同様にシフト
        self._tab_id_set = {(k - 1 if k > idx else k)
                            for k in self._tab_id_set if k != idx}
        self._tab_quar_set = {(k - 1 if k > idx else k)
                              for k in self._tab_quar_set if k != idx}

    def tabInserted(self, idx: int):
        super().tabInserted(idx)
        # 挿入位置以降のキャッシュをシフト
        for d in (self._tab_colors, self._tab_bg_colors, self._tab_icons, self._tab_width_cache):
            new_d = {}
            for k, v in d.items():
                new_d[k + 1 if k >= idx else k] = v
            d.clear(); d.update(new_d)
        self._tab_id_set = {(k + 1 if k >= idx else k) for k in self._tab_id_set}
        self._tab_quar_set = {(k + 1 if k >= idx else k) for k in self._tab_quar_set}

    def setTabText(self, idx: int, text: str):
        super().setTabText(idx, text)
        self._tab_width_cache.pop(idx, None)  # テキスト変更でキャッシュ無効化


    # ── 描画 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _tab_path(rect: QRect, sel: bool):
        from PySide6.QtGui import QPainterPath
        r = 4
        path = QPainterPath()
        bottom = rect.bottom() + (2 if sel else 0)
        path.moveTo(rect.left(),          bottom)
        path.lineTo(rect.left(),          rect.top() + r)
        path.quadTo(rect.left(),          rect.top(),
                    rect.left() + r,      rect.top())
        path.lineTo(rect.right() - r - 1, rect.top())
        path.quadTo(rect.right() - 1,     rect.top(),
                    rect.right() - 1,     rect.top() + r)
        path.lineTo(rect.right() - 1,     bottom)
        if not sel:
            path.lineTo(rect.left(), bottom)
        return path

    def paintEvent(self, _event):
        from PySide6.QtGui import QPainter, QFont, QPen
        rects  = self._tab_rects()
        ci     = self.currentIndex()
        p      = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(self._C_BG))
        p.setPen(QPen(QColor(self._C_BRD)))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        draw_order = [i for i in rects if i != ci] + ([ci] if ci in rects else [])
        fnt = QFont(self.font())
        # タブに対応するウィジェットを取得するためQTabWidget親を参照
        parent_tw = self.parentWidget()  # QTabWidget
        for i in draw_order:
            rect = rects[i]; sel = (i == ci)
            path = self._tab_path(rect, sel)
            bg = self._tab_bg_colors.get(i)
            if bg:
                # 通常背景の上に水色をブレンド
                p.fillPath(path, QColor(self._C_SEL if sel else self._C_NRM))
                p.fillPath(path, bg)
            else:
                p.fillPath(path, QColor(self._C_SEL if sel else self._C_NRM))
            p.setPen(QPen(QColor(self._C_BRD), 1)); p.drawPath(path)
            if sel:
                p.setPen(QPen(self.c_pin_ind(), 3))
                p.drawLine(rect.left()+1, rect.bottom()+1, rect.right()-2, rect.bottom()+1)
            fnt.setBold(sel); p.setFont(fnt)

            # ── アイコン領域（左端 3px から 16x16）──
            icon_rect = QRect(rect.x()+3, rect.y()+(self._ROW_H-16)//2, 16, 16)
            # ── ピン留め用アイコン領域（20x20、中央揃え）──
            pin_rect  = QRect(rect.x()+1, rect.y()+(self._ROW_H-20)//2, 20, 20)
            pix = self._tab_icons.get(i)
            has_icon = pix and not pix.isNull()
            if has_icon:
                p.drawPixmap(icon_rect, pix)

            # ── ピン留め: アイコン領域に半透明で重ねる（左端）──
            # ドラッグ中は _move_tab が表示順だけ入れ替え、stacked の実ウィジェット
            # 順はリリース時まで変わらない。parent_tw.widget(i) を見るとピンや×が
            # 元の位置に residual として残るため、ドラッグ中は _drag_widget_order
            # （並べ替え済みのウィジェット順）を優先して参照する。
            w_i = self._widget_for_paint(i, parent_tw)
            is_pinned = bool(self._pinned_widgets) and (w_i is not None
                                                        and w_i in self._pinned_widgets)
            if is_pinned:
                # テーマの pin.png を初回のみ読み込みキャッシュ
                # テーマフォルダ優先 → theme/直下 fallback
                if not self._pin_pixmap_loaded:
                    self._pin_pixmap_loaded = True
                    _theme_root = Path(__file__).parent / "theme"
                    for _d in [_TM.theme_dir(), _theme_root]:
                        for ext in ("png", "svg", "ico"):
                            pin_path = _d / f"pin.{ext}"
                            if pin_path.exists():
                                pix_pin = QPixmap(str(pin_path))
                                if not pix_pin.isNull():
                                    self._pin_pixmap = pix_pin.scaled(
                                        20, 20,
                                        Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
                                    break
                        if self._pin_pixmap and not self._pin_pixmap.isNull():
                            break
                if self._pin_pixmap and not self._pin_pixmap.isNull():
                    # テーマ画像を使用
                    p.drawPixmap(pin_rect, self._pin_pixmap)
                else:
                    # フォールバック: 📌絵文字
                    pin_fnt = QFont(self.font())
                    pin_fnt.setPixelSize(13)
                    p.setFont(pin_fnt)
                    p.setPen(self.c_pin_icon())
                    p.drawText(pin_rect, Qt.AlignmentFlag.AlignCenter, "📌")

            # ── テキスト領域 ──
            if has_icon:
                tr = rect.adjusted(21, 0, -20, 0)
            elif is_pinned:
                tr = rect.adjusted(24, 0, -20, 0)
            else:
                tr = rect.adjusted(5, 0, -20, 0)

            # _tab_colors で上書き色が指定されていればそれを使う（ただしアクティブタブは白優先）
            _ov = self._tab_colors.get(i)
            if sel and _ov and _ov == self.c_new():
                txt_color = QColor(self._C_STXT)  # アクティブタブの青は白に戻す
            else:
                txt_color = _ov or QColor(self._C_STXT if sel else self._C_TXT)
            p.setFont(fnt)
            p.setPen(txt_color)
            p.drawText(tr, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self.tabText(i))
            cr = self._close_rect(rect)
            # カタログタブは閉じられない → × を描画しない
            try:
                _is_catalog = isinstance(w_i, CatalogView)
            except Exception:
                _is_catalog = False
            if not _is_catalog:
                p.setPen(QColor(self._C_CLZ))
                p.drawText(cr, Qt.AlignmentFlag.AlignCenter, "×")
        p.end()

    # ── マウス ──────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        from PySide6.QtCore import Qt as _Qt
        pos = e.position().toPoint()
        i   = self._idx_at(pos)
        if e.button() == _Qt.MouseButton.MiddleButton:
            if i >= 0:
                self.tabCloseRequested.emit(i)   # ミドルクリック→閉じる
            return
        if e.button() == _Qt.MouseButton.RightButton:
            # 右クリック: contextMenuEvent より先に e.position().toPoint() + _tab_rects() で
            # 正確なインデックスを記録する（contextMenuEvent の e.pos() は精度に問題がある）
            self._ctx_idx = i
            return
        # 左クリック: ダブルクリック時の index 解決用に reflow 前の確定 index を保持
        # （1回目の押下で setCurrentIndex → アクティブ行が最下段へ移動し、2回目の
        #   位置からは別タブに解決されてしまうのを防ぐ）
        self._press_idx = i
        if i >= 0 and not self._close_rect(self._tab_rects().get(i, QRect())).contains(pos):
            self.setCurrentIndex(i)              # × 以外の左クリック→選択
            # D&D 開始準備
            self._drag_idx = i
            self._drag_start_pos = pos
            self._drag_active = False
            # widget順・テキスト順のスナップショットを取得
            tw2 = self.parent()
            if tw2 is not None:
                self._drag_widget_order = [tw2.widget(j) for j in range(tw2.count())]
            else:
                self._drag_widget_order = []
            self._drag_text_order = []
            self._drag_tip_order  = []

    def mouseMoveEvent(self, e):
        from PySide6.QtCore import Qt as _Qt
        if not (e.buttons() & _Qt.MouseButton.LeftButton):
            return
        if self._drag_idx < 0 or self._drag_start_pos is None:
            return
        pos = e.position().toPoint()
        if not self._drag_active:
            dist = (pos - self._drag_start_pos).manhattanLength()
            if dist < 6:
                return
            self._drag_active = True
        rects = self._tab_rects()
        my_rect = rects.get(self._drag_idx)
        if my_rect is None:
            return
        # ポインタが乗っているタブを検索
        hover_idx = -1
        for ti, r in rects.items():
            if r.contains(pos):
                hover_idx = ti
                break
        if hover_idx < 0 or hover_idx == self._drag_idx:
            return
        hover_rect = rects[hover_idx]
        cx, cy = hover_rect.center().x(), hover_rect.center().y()
        moving_left  = hover_idx < self._drag_idx
        moving_right = hover_idx > self._drag_idx
        if my_rect.top() == hover_rect.top():
            if moving_left  and pos.x() < cx:
                self._move_tab(self._drag_idx, hover_idx)
                self._drag_idx = hover_idx
            elif moving_right and pos.x() > cx:
                self._move_tab(self._drag_idx, hover_idx)
                self._drag_idx = hover_idx
        else:
            if moving_left  and pos.y() < cy:
                self._move_tab(self._drag_idx, hover_idx)
                self._drag_idx = hover_idx
            elif moving_right and pos.y() > cy:
                self._move_tab(self._drag_idx, hover_idx)
                self._drag_idx = hover_idx

    def _move_tab(self, src: int, dst: int):
        """タブを src から dst へ移動。
        ちらつき防止のためQTabBarのテキスト・キャッシュのみ書き換え。
        stacked widgetはリリース時に _sync_stacked_to_tabbar で一括同期。
        """
        if src == dst:
            return
        n = self.count()
        tw = self.parent()
        if tw is None:
            return

        # スナップショット
        snap_text  = [self.tabText(i)            for i in range(n)]
        snap_tip   = [self.tabToolTip(i)         for i in range(n)]
        snap_icon  = [self._tab_icons.get(i)     for i in range(n)]
        snap_color = [self._tab_colors.get(i)    for i in range(n)]
        snap_bg    = [self._tab_bg_colors.get(i) for i in range(n)]
        snap_width = [self._tab_width_cache.get(i) for i in range(n)]
        snap_id    = [(i in self._tab_id_set)    for i in range(n)]

        order = list(range(n))
        order.insert(dst, order.pop(src))

        for new_i, old_i in enumerate(order):
            QTabBar.setTabText(self, new_i, snap_text[old_i])
            QTabBar.setTabToolTip(self, new_i, snap_tip[old_i])

        self._tab_icons.clear(); self._tab_colors.clear()
        self._tab_bg_colors.clear(); self._tab_width_cache.clear()
        self._tab_id_set.clear()
        for new_i, old_i in enumerate(order):
            if snap_icon[old_i]  is not None: self._tab_icons[new_i]       = snap_icon[old_i]
            if snap_color[old_i] is not None: self._tab_colors[new_i]      = snap_color[old_i]
            if snap_bg[old_i]    is not None: self._tab_bg_colors[new_i]   = snap_bg[old_i]
            if snap_width[old_i] is not None: self._tab_width_cache[new_i] = snap_width[old_i]
            if snap_id[old_i]:                self._tab_id_set.add(new_i)

        # widget順・テキスト順スナップショットも同じ順序で更新
        if len(self._drag_widget_order) == n:
            wo = list(self._drag_widget_order)
            wo.insert(dst, wo.pop(src))
            self._drag_widget_order = wo
        # テキスト順も追跡（リリース時の書き直し用）
        if not hasattr(self, "_drag_text_order") or len(getattr(self, "_drag_text_order", [])) != n:
            self._drag_text_order = [QTabBar.tabText(self, i) for i in range(n)]
            self._drag_tip_order  = [QTabBar.tabToolTip(self, i) for i in range(n)]
        else:
            to = list(self._drag_text_order)
            tp = list(self._drag_tip_order)
            to.insert(dst, to.pop(src))
            tp.insert(dst, tp.pop(src))
            self._drag_text_order = to
            self._drag_tip_order  = tp

        self._tab_rects_cache_key = None
        self.update()

    def mouseReleaseEvent(self, e):
        from PySide6.QtCore import Qt as _Qt
        if e.button() != _Qt.MouseButton.LeftButton:
            self._drag_idx = -1; self._drag_active = False
            return
        pos = e.position().toPoint()
        if self._drag_active:
            self._drag_idx = -1; self._drag_active = False
            tw = self.parent()
            if tw is not None:
                # stacked を TabBar 順に合わせる（リリース時に一括同期）
                self._sync_stacked_to_tabbar(tw)
                # ドラッグ完了後に _on_tab_changed を1回発火
                cur = tw.currentIndex()
                bp = tw.parent()
                while bp is not None:
                    if hasattr(bp, '_on_tab_changed'):
                        bp._on_tab_changed(cur)
                        break
                    bp = bp.parent()
            return
        self._drag_idx = -1; self._drag_active = False
        for i, rect in self._tab_rects().items():
            if self._close_rect(rect).contains(pos):
                self.tabCloseRequested.emit(i)
                return

    def _sync_stacked_to_tabbar(self, tw):
        """リリース時に TabBar+stacked を _drag_widget_order の順序に確定する。
        手順:
        1. super().moveTab() で stacked を target_widgets 順に並べる
           （この時 TabBar テキストも連動して動く）
        2. TabBar テキストを _drag_widget_order から正しく書き直す
        """
        n = self.count()
        if n == 0 or len(self._drag_widget_order) != n:
            return

        target_widgets = self._drag_widget_order
        cur_w = tw.currentWidget()

        bp = tw.parent()
        handler = None
        while bp is not None:
            if hasattr(bp, '_on_tab_changed'):
                handler = bp
                break
            bp = bp.parent()
        if handler:
            tw.currentChanged.disconnect(handler._on_tab_changed)
        try:
            # step1: super().moveTab でstacked順を target_widgets 順に合わせる
            # tw.indexOf で毎回現在位置を再取得
            for i in range(n):
                cur_pos = tw.indexOf(target_widgets[i])
                if cur_pos != i:
                    super().moveTab(cur_pos, i)

            # step2: TabBarテキスト・ツールチップを _drag_widget_order から正しく書き直す
            # _drag_widget_order 作成時のテキストを取得するため、
            # 現時点で tw.widget(i) と target_widgets[i] は一致しているはず
            # _move_tab で積み上げたテキスト順（=_drag_text_order）を使う
            if hasattr(self, '_drag_text_order') and len(self._drag_text_order) == n:
                for i in range(n):
                    QTabBar.setTabText(self, i, self._drag_text_order[i])
                    if i < len(self._drag_tip_order):
                        QTabBar.setTabToolTip(self, i, self._drag_tip_order[i])
        finally:
            if handler:
                tw.currentChanged.connect(handler._on_tab_changed)

        if cur_w is not None:
            ci = tw.indexOf(cur_w)
            if ci >= 0:
                tw.setCurrentIndex(ci)
        self._drag_widget_order = []
        self._drag_text_order   = []
        self._drag_tip_order    = []

    def mouseDoubleClickEvent(self, e):
        # 1回目の押下で setCurrentIndex によりアクティブ行が最下段へ reflow し、
        # このイベント（2回目）の位置からは別タブに解決されてしまう。
        # reflow 前に mousePressEvent で確定した _press_idx を優先して使う。
        i = getattr(self, '_press_idx', -1)
        if i < 0:
            i = self._idx_at(e.position().toPoint())
        if i >= 0: self.tabBarDoubleClicked.emit(i)

    def contextMenuEvent(self, e):
        # _ctx_idx は mousePressEvent で設定済み。ここでは伝播を止めてシグナルを送出するだけ
        e.accept()   # 親ウィジェットへの伝播を防ぐ
        if getattr(self, '_ctx_idx', -1) >= 0:
            self.customContextMenuRequested.emit(e.pos())

    def wheelEvent(self, event):
        """ホイールでタブ切り替え。最後→最初、最初→最後のループ対応。"""
        count = self.count()
        if count <= 1:
            event.ignore(); return
        cur = self.currentIndex()
        delta = event.angleDelta().y()
        new = (cur - 1) % count if delta > 0 else (cur + 1) % count
        self.setCurrentIndex(new)
        event.accept()


# ─ タブスタイル ─────────────────────────────────────────────────────────────
_TAB_STYLE = ""  # WrapTabBar が自前描画するためスタイルシート不要


def _default_zoom() -> float:
    """OS の表示スケールに合わせたズーム係数を返す"""
    try:
        from PySide6.QtGui import QGuiApplication
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        # devicePixelRatio が 1.0 (96dpi) の場合は 1.25 にして見やすくする
        # 1.25 (120dpi) 以上の場合はそのまま 1.0 で OK
        return 1.0  # 常に 100% (OS スケールは QtWebEngine が処理)
    except Exception:
        return 1.25


_USER_CSS_CACHE: dict = {}   # resolved_path -> (mtime, content)

def _load_user_css(settings) -> str:
    """設定の user_css_file を読んで内容を返す。なければ空文字。
    全描画パス（自動更新の毎ティック含む）から呼ばれるため、mtimeで
    無効化するキャッシュを使い毎回のディスク読み込みを避ける。CSSを
    編集すればmtimeが変わり次回読込で自動反映される。"""
    css_file = getattr(settings, "user_css_file", "") or ""
    if not css_file:
        return ""
    try:
        p = Path(css_file)
        if not p.is_absolute():
            p = Path(__file__).parent / p
        key = str(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            _USER_CSS_CACHE.pop(key, None)   # 無くなった → キャッシュ破棄
            return ""
        cached = _USER_CSS_CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        content = p.read_text(encoding="utf-8")
        _USER_CSS_CACHE[key] = (mtime, content)
        return content
    except Exception as e:
        print(f"[UserCSS] 読み込みエラー: {e}")
    return ""


# ── 画像/引用モードの固有CSS ─────────────────────────────────────────────────
# フルレンダー（初回ロード）と、モード切替時のDOM入替注入の両方から使うため
# モジュールレベルに切り出す。引用CSSは静的、画像CSSは列数(cols)に依存。
_QT_MODE_CSS = (".qt-sep{border-top:1px solid #aaa;margin:6px 0;}"
                ".qt-row{padding:2px 4px;line-height:1.0;font-size:9pt;white-space:nowrap;overflow:hidden;"
                "width:60%;}"
                ".qt-root{font-weight:bold;}"
                ".qt-idx{color:#888;font-size:8pt;min-width:20px;display:inline-block;}"
                ".qt-child{transform:translateX(-10px) !important;}"
                ".qt-no{color:#0000EE;text-decoration:none;margin:0 4px;}"
                ".qt-no:hover{text-decoration:underline;color:#cc1105;}"
                ".qt-new{color:#cc1105;font-size:8pt;}"
                ".qt-sod{color:#c55000;font-size:8pt;margin-left:2px;}"
                ".qt-branch{color:#888;margin-right:2px;}"
                ".qt-thumb{max-height:60px;max-width:80px;object-fit:contain;"
                "vertical-align:middle;margin-left:4px;border:1px solid #aaa;cursor:pointer;}"
                ".qt-row.deleted{display:none;}"
                "body.show-deleted .qt-row.deleted{display:block;}"
                ".qt-row.ng-hidden{display:none;}"
                ".qt-row.ng-band{border-left:4px solid #1f9d1f;padding-left:4px;}"
                # 新着=赤帯 / 自分のレス=青帯（返信モードと同色。緑帯より後に定義して優先）
                ".qt-row.new-res{border-left:4px solid #cc1105;padding-left:4px;}"
                ".qt-row.self-res{border-left:4px solid #1a6fd4;padding-left:4px;}"
                ".del-done{color:#cc1105;font-weight:bold;font-size:8pt;}")

def _img_mode_css(cols: int) -> str:
    return (".wrap{display:flex;justify-content:center;padding:8px}.grid{display:inline-grid;"
            "grid-template-columns:repeat(" + str(cols) + ",80px);gap:4px}"
            ".gi{border:1px solid #800000;padding:3px;cursor:pointer;display:flex;flex-direction:column;"
            "width:80px;box-sizing:border-box;position:relative}.gi:hover{background:#F0E0D6}"
            ".gi-qi{position:absolute;top:3px;right:2px;color:#800000;font-size:9pt;line-height:1;"
            "cursor:pointer;user-select:none;z-index:2}.gi-qi:hover{color:#cc0000}"
            # 親.giは flex-column なので cross-axis(=横)は既定で stretch され、
            # .gn の背景が80px幅いっぱいに伸びて右上の▼(.gi-qi)まで到達する。
            # align-self:flex-start で自身のcross-axisサイズを内容分に縮める。
            ".gn{text-align:left;font-size:7pt;line-height:1.3;cursor:help;"
            "align-self:flex-start;"
            "border-radius:2px;padding:0 3px;min-width:6em}"
            ".gt{flex:1;display:flex;align-items:center;justify-content:center;padding:2px 0}"
            ".gt img{max-width:100%;max-height:72px;object-fit:contain}"
            ".gs{text-align:right;font-size:7pt;overflow:hidden;line-height:1.3}"
            ".gi.deleted{display:none}body.show-deleted .gi.deleted{display:flex}"
            ".gi.ng-hidden{display:none}.gi.ng-band{box-shadow:inset 2px 0 0 #1f9d1f}"
            # 新着=赤帯 / 自分のレス=青帯（緑帯より後に定義して優先）
            ".gi.new-res{box-shadow:inset 2px 0 0 #cc1105}"
            ".gi.self-res{box-shadow:inset 2px 0 0 #1a6fd4}"
            ".gi-del{position:absolute;top:12px;left:1px;color:#cc1105;font-weight:bold;font-size:7pt;"
            "line-height:1.1;background:rgba(255,255,255,0.85);padding:0 2px;border-radius:2px;z-index:3}"
            # ── 一括保存の選択UI ──
            ".gi.sel{outline:3px solid #1a6fd4;outline-offset:-3px;background:#dbe9ff!important}"
            "#_selmodebtn{position:fixed;top:8px;right:16px;z-index:9998;"
            "background:rgba(255,255,255,.92);border:1px solid #800000;border-radius:4px;"
            "padding:4px 10px;font-size:9pt;cursor:pointer;user-select:none;color:#800000}"
            "#_selmodebtn.on{background:#1a6fd4;border-color:#1a6fd4;color:#fff;font-weight:bold}"
            "#_selbar{position:fixed;left:0;right:0;bottom:0;display:none;gap:6px;align-items:center;"
            "background:rgba(40,40,40,.92);color:#fff;padding:6px 10px;z-index:9998;"
            "font-size:9pt;flex-wrap:wrap}"
            "#_selbar button{cursor:pointer;padding:3px 10px;font-size:9pt}"
            "#_selbar ._selgrp{display:inline-flex}"
            "#_selbar ._selgrp button{padding:3px 7px}"
            "#_selbar label{cursor:pointer;user-select:none;white-space:nowrap}")


# 画像モードの一括保存・選択UI用JS（window関数として定義・多重定義ガード付き。
# フルレンダーでは<head>に、DOM入替(swap)では入替JSの先頭に含める）
_GAL_SEL_JS = (
    "(function(){"
    "if(window._giClick)return;"
    "window._selMode=false;"
    "function _cnt(){return document.querySelectorAll('.gi.sel').length;}"
    "window._selUpdate=function(){"
    "  var b=document.getElementById('_selbar');if(!b)return;"
    "  var n=_cnt();"
    "  var lbl=document.getElementById('_selcnt');"
    "  if(lbl)lbl.textContent=n+'件選択中';"
    "  b.style.display=(window._selMode||n>0)?'flex':'none';"
    "  var mb=document.getElementById('_selmodebtn');"
    "  if(mb)mb.classList.toggle('on',window._selMode);"
    "};"
    # 開始ボタン: ONの間は通常クリック＝選択トグル（Ctrl押しっぱなし相当）
    "window._selToggleMode=function(){"
    "  window._selMode=!window._selMode;"
    "  if(!window._selMode){window._selClear();return;}"
    "  window._selUpdate();"
    "};"
    "window._giClick=function(ev,idx,el){"
    "  if(window._selMode||ev.ctrlKey){"
    "    el.classList.toggle('sel');"
    "    window._selUpdate();"
    "    ev.preventDefault();ev.stopPropagation();"
    "    return;"
    "  }"
    "  try{openGalleryImg(idx)}catch(e){}"
    "};"
    "window._selAll=function(){"
    "  document.querySelectorAll('.gi').forEach(function(g){g.classList.add('sel');});"
    "  window._selUpdate();"
    "};"
    "window._selClear=function(){"
    "  document.querySelectorAll('.gi.sel').forEach(function(g){g.classList.remove('sel');});"
    "  window._selUpdate();"
    "};"
    "function _selUrls(){"
    "  var urls=[];"
    "  document.querySelectorAll('.gi.sel').forEach(function(g){"
    "    var u=g.getAttribute('data-img-url');if(u)urls.push(u);"
    "  });"
    "  if(!urls.length&&typeof showDelMsg==='function')showDelMsg('画像が選択されていません');"
    "  return urls;"
    "}"
    "window._selSave=function(folder){"
    "  var urls=_selUrls();if(!urls.length)return;"
    "  _b('saveSelectedImages',[folder,urls]);"
    "};"
    "window._selBrowse=function(folder){"
    "  var urls=_selUrls();if(!urls.length)return;"
    "  _b('browseSaveSelected',[folder,urls]);"
    "};"
    "window._selSub=function(folder){"
    "  var urls=_selUrls();if(!urls.length)return;"
    "  _b('subfolderSaveMenu',[folder,urls]);"
    "};"
    "})();"
)


def _has_subdir(path: str) -> bool:
    """直下にサブフォルダがあるか（保存先「▼」ボタンの表示判定用）"""
    import os
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


def _populate_subfolder_menu(menu, folder: str, on_pick) -> None:
    """menu に folder 直下のサブフォルダを列挙する。孫フォルダを持つものは
    サブメニュー化（先頭に「ここに保存」項目）し、開かれた時に遅延展開する。
    on_pick(path) が保存先確定時に呼ばれる。"""
    import os
    try:
        subs = sorted((e.path for e in os.scandir(folder)
                       if e.is_dir(follow_symlinks=False)),
                      key=lambda p: os.path.basename(p).lower())
    except OSError:
        subs = []
    if not subs:
        act = menu.addAction("(サブフォルダなし)")
        act.setEnabled(False)
        return
    for p in subs:
        name = os.path.basename(p)
        if _has_subdir(p):
            sub = menu.addMenu(name)
            head = sub.addAction("ここに保存")
            head.triggered.connect(lambda _=False, pp=p: on_pick(pp))
            sub.addSeparator()
            def _fill(sm=sub, pp=p):
                if sm.property("_subs_filled"):
                    return
                sm.setProperty("_subs_filled", True)
                _populate_subfolder_menu(sm, pp, on_pick)
            sub.aboutToShow.connect(_fill)
        else:
            act = menu.addAction(name)
            act.triggered.connect(lambda _=False, pp=p: on_pick(pp))


def _is_pseudo_red_thread(thread, settings) -> bool:
    """仮赤字判定（保存残りが max_saved の1/10以下、設定ONの場合のみ）。
    サーバー側の本物の赤字(thread.is_expiring)ならFalse（呼び出し側で優先判定するため）。"""
    if not thread or thread.is_expiring:
        return False
    if not getattr(settings, "treat_near_limit_as_expiring", False):
        return False
    board = thread.board
    ms = getattr(board, 'max_saved', 0) if board else 0
    o = settings.global_max_no_by_board.get(board.base_url, 0) if board else 0
    if ms > 0 and o > 0:
        remain = thread.no + ms - o
        return remain <= ms // 10
    return False


# ── 書き込み時間ヒートマップ ─────────────────────────────────────────────────
import datetime as _hm_dt
_HM_EPOCH0 = _hm_dt.datetime(2000, 1, 1)
_HM_DT_RE = re.compile(
    r'(\d{2})/(\d{2})/(\d{2})\([^)]*\)(\d{1,2}):(\d{2})(?::(\d{2}))?')
# 棒の高さ・列幅などのレイアウト定数
_HM_BARH = 56    # 棒グラフの最大高さ(px)
_HM_COLW = 20    # 1列の幅(px)。縦書きラベルが潰れない幅を確保
_HM_BARW = 13    # 棒の幅(px)
_HM_FONT = 11    # 日付・時刻ラベルの文字サイズ(px)
_HM_DATE_H = 46  # 日付セルの高さ(px)。"MM/DD" 5文字が縦に収まる
_HM_TIME_H = 52  # 時刻セルの高さ(px)。"HH:MM" 5文字が縦に収まる
_HM_CANDS = [60, 120, 300, 600, 900, 1800, 3600, 7200,
             10800, 21600, 43200, 86400]


def _hm_parse_dt(s: str):
    """ふたばの日時文字列 '26/07/03(金)22:02:51' を datetime に。失敗時 None。"""
    m = _HM_DT_RE.search(s or "")
    if not m:
        return None
    y, mo, d, h, mi, se = m.groups()
    try:
        return _hm_dt.datetime(2000 + int(y), int(mo), int(d),
                               int(h), int(mi), int(se or 0))
    except ValueError:
        return None


def _hm_buckets(secs: list, target: int = 22, max_buckets: int = 60):
    """秒値リストから (bucket_sec, start_sec, counts[]) を返す。
    バケット幅は _HM_CANDS から時間スパンに応じて自動選択する。"""
    if not secs:
        return None
    lo, hi = min(secs), max(secs)
    span = hi - lo
    if span <= 0:
        bucket = 60
    else:
        raw = span / target
        bucket = next((c for c in _HM_CANDS if c >= raw), _HM_CANDS[-1])
        # 列が多すぎる場合は1段大きいバケットへ
        while span / bucket > max_buckets and bucket < _HM_CANDS[-1]:
            bucket = _HM_CANDS[_HM_CANDS.index(bucket) + 1]
    start = int(lo // bucket) * bucket
    n = int((hi - start) // bucket) + 1
    counts = [0] * n
    for s in secs:
        idx = int((s - start) // bucket)
        if 0 <= idx < n:
            counts[idx] += 1
    return bucket, start, counts


def _hm_unit_label(bucket: int) -> str:
    if bucket < 3600:
        return f"{bucket // 60}分"
    if bucket < 86400:
        return f"{bucket // 3600}時間"
    return f"{bucket // 86400}日"


def _build_heatmap_panel_html(res_list) -> str:
    """レス群から書き込み時間分布ヒートマップのパネルHTML(fixed配置)を返す。
    各列は上から「日付・時刻・縦棒(件数)」。日時が1件も取れなければ空文字。"""
    secs = []
    for r in res_list:
        dt = _hm_parse_dt(getattr(r, "datetime_str", ""))
        if dt is not None:
            secs.append((dt - _HM_EPOCH0).total_seconds())
    if not secs:
        return ""
    res = _hm_buckets(secs)
    if not res:
        return ""
    bucket, start, counts = res
    total = len(secs)
    mx = max(counts) or 1
    daily = bucket >= 86400
    cols = []
    prev_date = None
    for i, c in enumerate(counts):
        bstart = _HM_EPOCH0 + _hm_dt.timedelta(seconds=start + i * bucket)
        date_lbl = f"{bstart.month:02d}/{bstart.day:02d}"
        if daily:
            time_lbl = date_lbl
            show_date = ""
        else:
            time_lbl = f"{bstart.hour:02d}:{bstart.minute:02d}"
            show_date = date_lbl if date_lbl != prev_date else ""
        prev_date = date_lbl
        if c > 0:
            h = max(2, round(c / mx * _HM_BARH))
            light = int(68 - 40 * (c / mx))   # 件数が多いほど濃く
            color = f"hsl(210,75%,{light}%)"
        else:
            h = 0
            color = "transparent"
        cols.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;'
            f'width:{_HM_COLW}px;flex:0 0 auto;">'
            f'<div style="height:{_HM_DATE_H}px;writing-mode:vertical-rl;'
            f'font-size:{_HM_FONT}px;line-height:1.05;color:#a33;'
            f'white-space:nowrap;overflow:hidden;">{show_date}</div>'
            f'<div style="height:{_HM_TIME_H}px;writing-mode:vertical-rl;'
            f'font-size:{_HM_FONT}px;line-height:1.05;color:#555;'
            f'white-space:nowrap;overflow:hidden;">{time_lbl}</div>'
            f'<div title="{time_lbl} — {c}件" style="height:{_HM_BARH}px;width:100%;'
            f'display:flex;align-items:flex-end;justify-content:center;'
            f'border-bottom:1px solid #ccc;">'
            f'<div style="width:{_HM_BARW}px;height:{h}px;background:{color};"></div></div>'
            f'</div>'
        )
    header = (f'<div style="font-weight:bold;font-size:12px;margin-bottom:4px;'
              f'color:#800000;white-space:nowrap;">書き込み分布 '
              f'（{_hm_unit_label(bucket)}/枠・全{total}件）</div>')
    return (
        '<div id="_heatmap_panel" style="position:fixed;right:8px;bottom:8px;'
        'z-index:9500;background:rgba(255,255,255,.94);border:1px solid #800000;'
        'border-radius:4px;padding:4px 6px 5px;box-shadow:2px 2px 8px rgba(0,0,0,.3);'
        'max-width:78vw;overflow-x:auto;pointer-events:auto;">'
        + header
        + '<div style="display:flex;align-items:flex-end;gap:1px;">'
        + ''.join(cols) + '</div></div>'
    )


# ── テーマアイコン読み込み ───────────────────────────────────────────────────
_THEME_ICON_CACHE: dict = {}   # {name: QIcon}

def _theme_icon(name: str, fallback_sp) -> "QIcon":
    """テーマフォルダ優先 → theme/直下 → Qt標準 の順でアイコンを返す"""
    if name in _THEME_ICON_CACHE:
        return _THEME_ICON_CACHE[name]
    theme_root = Path(__file__).parent / "theme"
    # テーマ固有フォルダ → theme/直下 の順で探す
    search_dirs = [_TM.theme_dir(), theme_root]
    for d in search_dirs:
        for ext in ("png", "ico", "svg", "jpg"):
            p = d / f"{name}.{ext}"
            if p.exists():
                icon = QIcon(str(p))
                _THEME_ICON_CACHE[name] = icon
                return icon
    icon = QApplication.style().standardIcon(fallback_sp)
    _THEME_ICON_CACHE[name] = icon
    return icon

class Interceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info: QWebEngineUrlRequestInfo):
        url = info.requestUrl().toString()
        if "2chan.net" in url or "futaba" in url:
            info.setHttpHeader(b"Referer",    b"https://www.2chan.net/")
            info.setHttpHeader(b"User-Agent", UA.encode())


def _carry_over_deleted_content(old_thread, new_thread):
    """削除で本文がサーバから消えた新レスに、旧スレッドが持つ削除前の本文を
    引き継ぐ。ふたばは投稿者削除時に本文を「書き込みをした人によって削除され
    ました」に、画像を除去して返すため、直前まで表示していた本文を残す。
    画像はサーバから消えサムネもリモート404になるため復元表示はせず、元の画像
    ファイル名だけをテキストで残す。サーバが本文を残す削除(mod削除等)は尊重する。"""
    if not old_thread or not new_thread:
        return
    try:
        from futaba2b_html import split_deleted_comment
        old_by_no = {r.no: r for r in old_thread.res_list}
    except Exception:
        return
    for r in new_thread.res_list:
        if not getattr(r, "is_deleted", False) or getattr(r, "deleted_preserved", False):
            continue
        old = old_by_no.get(r.no)
        if old is None:
            continue
        _old_preserved = getattr(old, "deleted_preserved", False)
        _old_has = _old_preserved or (
            not getattr(old, "is_deleted", False)
            and bool((old.comment_html or "").strip()
                     or (old.comment_text or "").strip()
                     or old.image_name))
        if not _old_has:
            continue
        # 新レスが本文を失っている(理由のみ)場合だけ引き継ぐ
        _reason, _body = split_deleted_comment(r.comment_html)
        if _body:
            continue   # サーバが本文を残している → そのまま尊重
        r.deleted_reason = _reason or (getattr(old, "deleted_reason", "") if _old_preserved else "")
        r.comment_html = old.comment_html
        r.comment_text = old.comment_text
        r.deleted_orig_image_name = (getattr(old, "deleted_orig_image_name", "")
                                     if _old_preserved else (old.image_name or ""))
        r.deleted_preserved = True


# ══════════════════════════════════════════════════════════════════════════════
# 板ツリーパネル (板/タブ/お気に入り 3タブ)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# TabManagePane ─ 「タブ」ペイン
# ══════════════════════════════════════════════════════════════════════════════

class _TabItemWidget(QWidget):
    """タブリストの1アイテム（板名・スレ番号・タイトル・レス数・更新ボタン・閉じるボタン）"""
    update_clicked = Signal(str)   # thread_url
    close_clicked  = Signal(str)   # thread_url

    def __init__(self, board_name: str, thread_no: int, title: str,
                 res_count: int, new_count: int, last_update: str,
                 thread_url: str, is_expiring: bool = False,
                 is_catalog: bool = False, parent=None):
        super().__init__(parent)
        self._url = thread_url
        self._is_catalog = is_catalog
        self._lbl_title = None
        self._lbl_info  = None
        self._build(board_name, thread_no, title, res_count, new_count,
                    last_update, is_expiring, is_catalog)

    def _build(self, board_name, no, title, res_count, new_count,
               last_update, is_expiring, is_catalog):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2); lay.setSpacing(1)

        # 1行目: 板名 + [更新][×]
        # 右マージンを確保し、×ボタンがスクロールバーに被らないよう左へずらす
        row1 = QHBoxLayout(); row1.setContentsMargins(0, 0, 22, 0); row1.setSpacing(2)
        lbl_board = QLabel(board_name)
        lbl_board.setStyleSheet("font-size:8pt; color:%(muted)s; background:transparent;" % {"muted": _TM.ui("text_muted", "#888")})
        row1.addWidget(lbl_board)
        row1.addStretch()
        btn_upd = QPushButton("更新"); btn_upd.setFixedSize(38, 17)
        btn_upd.setStyleSheet("font-size:7pt; padding:0; background:%(bg)s; color:%(fg)s; border:1px solid %(bd)s;" % {"bg": _TM.ui("btn_bg","#3a3a3a"), "fg": _TM.ui("text_secondary","#aaa"), "bd": _TM.ui("btn_border","#555")})
        btn_cls = QPushButton("×");   btn_cls.setFixedSize(18, 17)
        btn_cls.setStyleSheet("font-size:8pt; padding:0; color:%(danger)s; background:%(bg)s; border:1px solid %(bd)s;" % {"danger": _TM.ui("text_danger","#f88"), "bg": _TM.ui("btn_bg","#3a3a3a"), "bd": _TM.ui("btn_border","#555")})
        btn_upd.clicked.connect(lambda: self.update_clicked.emit(self._url))
        btn_cls.clicked.connect(lambda: self.close_clicked.emit(self._url))
        row1.addWidget(btn_upd); row1.addWidget(btn_cls)
        lay.addLayout(row1)

        # 2行目: タイトル
        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet(f"font-size:8pt; background:transparent; color:{_TM.ui('text_primary','#e8e8e8')};")
        self._lbl_title.setMinimumHeight(self._lbl_title.sizeHint().height() + 1)
        lay.addWidget(self._lbl_title)

        # 3行目: レス数・最終更新
        self._lbl_info = QLabel()
        self._lbl_info.setStyleSheet(f"font-size:7pt; color:{_TM.ui('text_muted','#777')}; background:transparent;")
        self._lbl_info.setMinimumHeight(self._lbl_info.sizeHint().height() + 2)
        lay.addWidget(self._lbl_info)

        self.update_data(title, res_count, new_count, last_update, is_expiring)

    def update_data(self, title: str, res_count: int, new_count: int,
                    last_update: str, is_expiring):
        """Widget再生成なしにラベルテキスト・色のみ更新する"""
        # タイトル
        display_title = title[:40] + ("…" if len(title) > 40 else "") if title else ""
        if is_expiring == "pseudo":
            display_title += " (仮赤字)"
            title_color = "#e07080"
            title_bold  = ""
        elif is_expiring:
            title_color = "#cc0000"
            title_bold  = "font-weight:bold;"
        else:
            title_color = _TM.ui("text_primary", "#e8e8e8")
            title_bold  = ""
        if self._lbl_title:
            self._lbl_title.setText(display_title)
            self._lbl_title.setStyleSheet(
                f"font-size:8pt; background:transparent; color:{title_color};{title_bold}")

        # レス数・最終更新
        info_parts = []
        if not self._is_catalog:
            info_parts.append(f"レス:{res_count}件")
            if new_count > 0:
                info_parts.append(f"新着:{new_count}")
        if last_update:
            info_parts.append(f"最終更新:{last_update}")
        if self._lbl_info:
            self._lbl_info.setText("  ".join(info_parts))


class TabManagePane(QWidget):
    """タブペイン: 設定セクション + 開いているタブの一覧"""
    # MainWindowへのシグナル
    tab_update_requested = Signal(str)   # thread_url → 更新
    tab_close_requested  = Signal(str)   # thread_url → 閉じる
    tab_select_requested = Signal(str)   # thread_url → アクティブ化

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        # 表示フィルタ設定（セッション内）
        self._show_thread  = True
        self._show_catalog = True
        self._show_image   = False
        self._sync_select  = True
        self._build()

    def _build(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0); root_lay.setSpacing(0)

        # ── 設定セクション (折りたたみ) ─────────────────────────────────
        self._settings_container = QWidget()
        sc_lay = QVBoxLayout(self._settings_container)
        sc_lay.setContentsMargins(2, 0, 2, 0); sc_lay.setSpacing(0)

        # 設定ヘッダー
        self._settings_hdr = QPushButton("設定 [−]")
        self._settings_hdr.setStyleSheet(
            "text-align:left; padding:2px 4px; font-size:8pt;"
            f"background:{_TM.ui('window_bg','#1e1e1e')}; color:{_TM.ui('text_secondary','#bbb')}; border:none; border-bottom:1px solid {_TM.ui('panel_border','#444')};")
        self._settings_hdr.setFixedHeight(20)
        self._settings_hdr.clicked.connect(self._toggle_settings)
        sc_lay.addWidget(self._settings_hdr)

        self._settings_body = QWidget()
        self._settings_body.setStyleSheet(f"background:{_TM.ui('panel_bg','#252525')};")
        sb_lay = QVBoxLayout(self._settings_body)
        sb_lay.setContentsMargins(6, 2, 2, 2); sb_lay.setSpacing(1)

        # [-] 項目フィルタ
        filter_hdr = QPushButton("− 項目フィルタ")
        filter_hdr.setStyleSheet(
            "text-align:left; padding:1px 4px; font-size:8pt;"
            f"background:{_TM.ui('panel_header_bg','#333')}; color:{_TM.ui('text_secondary','#bbb')}; border:none;")
        filter_hdr.setFixedHeight(18)
        sb_lay.addWidget(filter_hdr)

        self._chk_thread  = QCheckBox("スレッド");   self._chk_thread.setChecked(True)
        self._chk_catalog = QCheckBox("カタログ");  self._chk_catalog.setChecked(True)
        self._chk_image   = QCheckBox("画像");       self._chk_image.setChecked(False)
        for chk in [self._chk_thread, self._chk_catalog, self._chk_image]:
            chk.setStyleSheet(f"font-size:8pt; color:{_TM.ui('text_primary','#ddd')}; margin-left:12px; background:transparent;")
            chk.toggled.connect(self._on_filter_changed)
            sb_lay.addWidget(chk)

        # その他オプション
        self._chk_sync = QCheckBox("タブの選択と同期する")
        self._chk_sync.setChecked(True)
        self._chk_sync.setStyleSheet(f"font-size:8pt; color:{_TM.ui('text_primary','#ddd')}; background:transparent;")
        sb_lay.addWidget(self._chk_sync)

        sort_btn = QPushButton("タブ順に並び替える")
        sort_btn.setStyleSheet(
            f"font-size:8pt; padding:2px 6px; background:{_TM.ui('btn_bg','#3a3a3a')}; color:{_TM.ui('btn_fg','#ccc')};"
            f"border:1px solid {_TM.ui('btn_border','#555')}; border-radius:2px;")
        sort_btn.setFixedHeight(22)
        sort_btn.clicked.connect(self._sort_by_tab_order)
        sb_lay.addWidget(sort_btn)

        self._settings_body.setLayout(sb_lay)
        sc_lay.addWidget(self._settings_body)

        self._settings_container.setLayout(sc_lay)
        root_lay.addWidget(self._settings_container)

        # ── タブリスト ──────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"QScrollArea{{background:{_TM.ui('panel_bg2','#2a2a2a')}; border:none;}}")
        self._list_w = QWidget()
        self._list_w.setStyleSheet(f"background:{_TM.ui('panel_bg2','#2a2a2a')};")
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        self._list_lay.addStretch()
        self._scroll.setWidget(self._list_w)
        root_lay.addWidget(self._scroll, 1)

        self._items: dict = {}  # url -> (widget, outer_idx, inner_idx)

    def _toggle_settings(self):
        vis = self._settings_body.isVisible()
        self._settings_body.setVisible(not vis)
        self._settings_hdr.setText("設定 [+]" if vis else "設定 [−]")

    def _on_filter_changed(self):
        self.refresh()

    def _sort_by_tab_order(self):
        """現在のタブリスト順に並び替え（現状はリフレッシュのみ）"""
        self.refresh()

    # ── 公開 API ────────────────────────────────────────────────────────────

    def refresh(self, outer_tabs: QTabWidget = None):
        """outer_tabs から現在開いているタブを差分更新でリスト反映"""
        if outer_tabs is None:
            p = self.parent()
            while p:
                if hasattr(p, '_outer_tabs'):
                    outer_tabs = p._outer_tabs
                    break
                p = p.parent() if hasattr(p, 'parent') else None
        if outer_tabs is None:
            return

        show_thread  = self._chk_thread.isChecked()
        show_catalog = self._chk_catalog.isChecked()

        # ── 新しいタブ情報を収集 ──
        new_order = []   # [(url, board_name, thread_no, title, res_count, new_count, last_update, is_expiring, is_catalog, ti, ii)]
        for ti in range(outer_tabs.count()):
            board_pane = outer_tabs.widget(ti)
            if not isinstance(board_pane, BoardPane):
                continue
            board_name = board_pane._board.name if board_pane._board else ""

            for ii in range(board_pane._tabs.count()):
                tab_w = board_pane._tabs.widget(ii)
                is_catalog_view = isinstance(tab_w, CatalogView)
                is_thread_view  = isinstance(tab_w, ThreadView)

                if is_catalog_view and not show_catalog:
                    continue
                if is_thread_view and not show_thread:
                    continue
                if not (is_catalog_view or is_thread_view):
                    continue

                url         = ""
                thread_no   = 0
                title       = board_pane._tabs.tabText(ii).strip()
                res_count   = 0
                new_count   = 0
                last_update = ""
                is_expiring = False

                if is_thread_view and tab_w._thread:
                    t = tab_w._thread
                    url         = t.url or ""
                    thread_no   = t.no or 0
                    res_count   = len(t.res_list) - 1
                    new_count   = t.new_count if hasattr(t, "new_count") else 0
                    is_expiring = getattr(t, "is_expiring", False)
                    if (not is_expiring
                            and getattr(self._settings, "treat_near_limit_as_expiring", False)):
                        board      = t.board
                        ms         = getattr(board, 'max_saved', 0) if board else 0
                        o          = (self._settings.global_max_no_by_board.get(
                                          board.base_url, 0) if board else 0)
                        if ms > 0 and o > 0:
                            saved_remain = t.no + ms - o
                            if saved_remain <= ms // 10:
                                is_expiring = "pseudo"
                    if t.res_list:
                        last_update = t.res_list[-1].datetime_str or ""
                elif is_thread_view:
                    # 未ロード（復元直後/読込中）のThreadView: _thread が None のため
                    # _board / _thread_no から実URLを構築し、ロード後(_thread.url)と
                    # 同一キーにする。これをしないと url="" の No.NNNNNNN プレースホルダが
                    # 残り続け、ロード後の項目と二重表示になる。
                    _b = getattr(tab_w, "_board", None)
                    _n = getattr(tab_w, "_thread_no", 0) or 0
                    if _b and _n:
                        url       = _b.base_url + f"res/{_n}.htm"
                        thread_no = _n
                elif is_catalog_view:
                    url = board_pane._board.url if board_pane._board else ""

                new_order.append((url, board_name, thread_no, title, res_count,
                                   new_count, last_update, is_expiring, is_catalog_view, ti, ii))

        new_urls = [item[0] for item in new_order]
        old_urls = list(self._items.keys())

        # ── 消えたURLのウィジェットを削除 ──
        removed = [u for u in old_urls if u not in new_urls]
        for u in removed:
            entry = self._items.pop(u, None)
            if entry:
                w = entry[0]
                self._list_lay.removeWidget(w)
                w.setParent(None)
                w.hide()

        # ── 順序変化チェック（順序が変わった場合は全再構築） ──
        existing_order = [u for u in new_urls if u in self._items]
        old_order_filtered = [u for u in old_urls if u in self._items]
        order_changed = existing_order != old_order_filtered

        if order_changed:
            # 順序が変わった場合のみ全Widget削除して再挿入
            for u in list(self._items.keys()):
                entry = self._items.pop(u)
                w = entry[0]
                self._list_lay.removeWidget(w)
                w.setParent(None)
                w.hide()

        # ── 各URLを処理（追加 or 更新） ──
        even = True
        for pos, (url, board_name, thread_no, title, res_count,
                   new_count, last_update, is_expiring, is_catalog, ti, ii) in enumerate(new_order):
            bg = _TM.ui("panel_bg", "#2e2e2e") if even else _TM.ui("window_bg", "#262626")
            even = not even

            if url in self._items and not order_changed:
                # 既存Widget: データのみ更新
                w, _ti, _ii = self._items[url]
                w.update_data(title, res_count, new_count, last_update, is_expiring)
                w.setStyleSheet(
                    f"background:{bg}; border-bottom:1px solid {_TM.ui('panel_border','#525252')}; color:{_TM.ui('text_primary','#ddd')};")
                self._items[url] = (w, ti, ii)
            else:
                # 新規Widget作成
                w = _TabItemWidget(
                    board_name=board_name, thread_no=thread_no,
                    title=title, res_count=res_count, new_count=new_count,
                    last_update=last_update, thread_url=url,
                    is_expiring=is_expiring, is_catalog=is_catalog)
                w.update_clicked.connect(self.tab_update_requested.emit)
                w.close_clicked.connect(self.tab_close_requested.emit)
                w.setStyleSheet(
                    f"background:{bg}; border-bottom:1px solid {_TM.ui('panel_border','#525252')}; color:{_TM.ui('text_primary','#ddd')};")
                w.mousePressEvent = lambda e, u=url, bpi=ti, ii2=ii: (
                    self._on_item_click(u, bpi, ii2, e))
                self._list_lay.insertWidget(pos, w)
                self._items[url] = (w, ti, ii)

    def _on_item_click(self, url, outer_idx, inner_idx, event):
        if self._chk_sync.isChecked():
            self.tab_select_requested.emit(url)


class FavManagePane(QWidget):
    """お気に入りペイン: お気に入り登録済みのURLが現在開いているタブのみ表示。
    TabManagePane と同じ更新・閉じる・選択シグナルを提供する。"""

    tab_update_requested = Signal(str)
    tab_close_requested  = Signal(str)
    tab_select_requested = Signal(str)

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._build()

    def _build(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0); root_lay.setSpacing(0)

        # ── リスト ───────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"QScrollArea{{background:{_TM.ui('panel_bg2','#2a2a2a')}; border:none;}}")
        self._list_w = QWidget()
        self._list_w.setStyleSheet(f"background:{_TM.ui('panel_bg2','#2a2a2a')};")
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(0, 0, 0, 0); self._list_lay.setSpacing(0)
        self._list_lay.addStretch()
        self._scroll.setWidget(self._list_w)
        root_lay.addWidget(self._scroll, 1)

        self._items: dict = {}  # url -> (widget, outer_idx, inner_idx)

    def refresh(self, outer_tabs: QTabWidget = None):
        """outer_tabs から現在開いているお気に入りタブを差分更新でリスト反映"""
        if outer_tabs is None:
            p = self.parent()
            while p:
                if hasattr(p, '_outer_tabs'):
                    outer_tabs = p._outer_tabs; break
                p = p.parent() if hasattr(p, 'parent') else None
        if outer_tabs is None:
            return

        fav_urls = {f.get("url", "") for f in self._settings.favorites if f.get("url")}

        # ── 新しいタブ情報を収集（お気に入り一致のみ） ──
        new_order = []
        for ti in range(outer_tabs.count()):
            board_pane = outer_tabs.widget(ti)
            if not isinstance(board_pane, BoardPane):
                continue
            board_name = board_pane._board.name if board_pane._board else ""

            for ii in range(board_pane._tabs.count()):
                tab_w = board_pane._tabs.widget(ii)
                is_catalog_view = isinstance(tab_w, CatalogView)
                is_thread_view  = isinstance(tab_w, ThreadView)
                if not (is_catalog_view or is_thread_view):
                    continue

                url = ""
                thread_no   = 0
                title       = board_pane._tabs.tabText(ii).strip()
                res_count   = 0
                new_count   = 0
                last_update = ""
                is_expiring = False

                if is_thread_view and tab_w._thread:
                    t = tab_w._thread
                    url         = t.url or ""
                    thread_no   = t.no or 0
                    res_count   = len(t.res_list) - 1
                    new_count   = t.new_count if hasattr(t, "new_count") else 0
                    is_expiring = getattr(t, "is_expiring", False)
                    if (not is_expiring
                            and getattr(self._settings, "treat_near_limit_as_expiring", False)):
                        board = t.board
                        ms    = getattr(board, 'max_saved', 0) if board else 0
                        o     = (self._settings.global_max_no_by_board.get(
                                     board.base_url, 0) if board else 0)
                        if ms > 0 and o > 0:
                            saved_remain = t.no + ms - o
                            if saved_remain <= ms // 10:
                                is_expiring = "pseudo"
                    if t.res_list:
                        last_update = t.res_list[-1].datetime_str or ""
                elif is_catalog_view:
                    url = board_pane._board.url if board_pane._board else ""

                if url not in fav_urls:
                    continue

                new_order.append((url, board_name, thread_no, title, res_count,
                                   new_count, last_update, is_expiring, is_catalog_view, ti, ii))

        new_urls = [item[0] for item in new_order]
        old_urls = list(self._items.keys())

        # ── 消えたURLのウィジェットを削除 ──
        for u in [u for u in old_urls if u not in new_urls]:
            entry = self._items.pop(u, None)
            if entry:
                w = entry[0]
                self._list_lay.removeWidget(w)
                w.setParent(None)
                w.hide()

        # ── 順序変化チェック ──
        existing_order = [u for u in new_urls if u in self._items]
        old_order_filtered = [u for u in old_urls if u in self._items]
        order_changed = existing_order != old_order_filtered

        if order_changed:
            for u in list(self._items.keys()):
                entry = self._items.pop(u)
                w = entry[0]
                self._list_lay.removeWidget(w)
                w.setParent(None)
                w.hide()

        # ── 各URLを処理（追加 or 更新） ──
        even = True
        for pos, (url, board_name, thread_no, title, res_count,
                   new_count, last_update, is_expiring, is_catalog, ti, ii) in enumerate(new_order):
            bg = _TM.ui("panel_bg", "#2e2e2e") if even else _TM.ui("window_bg", "#262626")
            even = not even

            if url in self._items and not order_changed:
                w, _ti, _ii = self._items[url]
                w.update_data(title, res_count, new_count, last_update, is_expiring)
                w.setStyleSheet(
                    f"background:{bg}; border-bottom:1px solid {_TM.ui('panel_border','#525252')}; color:{_TM.ui('text_primary','#ddd')};")
                self._items[url] = (w, ti, ii)
            else:
                w = _TabItemWidget(
                    board_name=board_name, thread_no=thread_no,
                    title=title, res_count=res_count, new_count=new_count,
                    last_update=last_update, thread_url=url,
                    is_expiring=is_expiring, is_catalog=is_catalog)
                w.update_clicked.connect(self.tab_update_requested.emit)
                w.close_clicked.connect(self.tab_close_requested.emit)
                w.setStyleSheet(
                    f"background:{bg}; border-bottom:1px solid {_TM.ui('panel_border','#525252')}; color:{_TM.ui('text_primary','#ddd')};")
                w.mousePressEvent = lambda e, u=url, bpi=ti, ii2=ii: (
                    self._on_item_click(u, bpi, ii2, e))
                self._list_lay.insertWidget(pos, w)
                self._items[url] = (w, ti, ii)

    def _on_item_click(self, url, outer_idx, inner_idx, event):
        self.tab_select_requested.emit(url)


class BoardTreePane(QWidget):
    board_selected = Signal(object)   # BoardInfo
    custom_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._build()

    def _build(self):
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self._nb = QTabWidget(); self._nb.setDocumentMode(True)
        lay.addWidget(self._nb)

        # 板タブ
        board_w = QWidget()
        b_lay   = QVBoxLayout(board_w); b_lay.setContentsMargins(0, 0, 0, 0)
        self._tree = QTreeWidget(); self._tree.setHeaderHidden(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(self._on_double)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_right_click)
        b_lay.addWidget(self._tree)
        self._nb.addTab(board_w, "板")

        # タブタブ
        self._tab_pane = TabManagePane(self._settings, self)
        self._nb.addTab(self._tab_pane, "タブ")

        # お気に入りタブ
        self._fav_pane = FavManagePane(self._settings, self)
        self._nb.addTab(self._fav_pane, "お気に入り")

    # ── 公開 API ────────────────────────────────────────────────────────────

    def set_categories(self, cats: list, custom_urls: set = frozenset()):
        self._tree.clear()
        for cat in cats:
            ci = QTreeWidgetItem([cat.name])
            ci.setData(0, Qt.ItemDataRole.UserRole, ("__cat__", cat.name))
            ci.setExpanded(True)
            f = ci.font(0); f.setBold(True); ci.setFont(0, f)
            self._tree.addTopLevelItem(ci)
            for board in cat.boards:
                is_custom = board.url in custom_urls
                # 「二次元裏」のみサブドメインを付加
                _sv = ''
                if board.name == "二次元裏":
                    _sm = re.match(r'https?://([^.]+)\.2chan\.net/', board.url or '')
                    _sv = f"({_sm.group(1)})" if _sm else ''
                disp = f"★ {board.name}{_sv}" if is_custom else f"{board.name}{_sv}"
                bi = QTreeWidgetItem([disp])
                bi.setData(0, Qt.ItemDataRole.UserRole,
                           ("board", board.url, cat.name, is_custom, board.name))
                if is_custom:
                    bi.setForeground(0, QColor("#0055AA"))
                ci.addChild(bi)
        self._tree.expandAll()  # 全カテゴリを展開

    def refresh_favorites(self):
        p = self.parent()
        outer = getattr(p, '_outer_tabs', None) if p else None
        self._fav_pane.refresh(outer)

    # ── イベント ────────────────────────────────────────────────────────────

    def _on_double(self, item: QTreeWidgetItem, _col: int):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d and d[0] == "board":
            self.board_selected.emit(BoardInfo(name=d[4], url=d[1]))

    def _on_right_click(self, pos):
        item = self._tree.itemAt(pos)
        if not item:
            return
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if not d:
            return
        menu = QMenu(self)
        if d[0] == "__cat__":
            menu.addAction(f"「{d[1]}」に板を追加…",
                           lambda cn=d[1]: self._add_board_to_group(cn))
        else:
            url, cat_name, is_custom, name = d[1], d[2], d[3], d[4]
            menu.addAction("この板を開く",
                           lambda: self.board_selected.emit(BoardInfo(name=name, url=url)))
            menu.addAction("お気に入りに追加",
                           lambda: (self._settings.add_favorite(name, url),
                                    self.refresh_favorites()))
            if is_custom:
                menu.addSeparator()
                menu.addAction("この板を削除（カスタム登録を解除）",
                               lambda u=url, c=cat_name: self._remove_custom(c, u))
        menu.exec(self._tree.mapToGlobal(pos))

    def _add_board_to_group(self, cat_name: str):
        url, ok = QInputDialog.getText(self, "板を追加", "板のURL:")
        if not (ok and url.strip()):
            return
        url = url.strip().rstrip("/") + "/futaba.htm"
        name, ok2 = QInputDialog.getText(self, "板を追加", "板の名前:")
        if ok2 and name.strip():
            self._settings.add_board_to_group(cat_name, name.strip(), url)
            self.custom_changed.emit()

    def _remove_custom(self, cat_name: str, url: str):
        self._settings.remove_board_from_group(cat_name, url)
        self.custom_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# 内側タブ (1板 = 1 InnerTabWidget)
# ══════════════════════════════════════════════════════════════════════════════

class InnerTabWidget(QTabWidget):
    """カタログタブ(index=0) + スレッドタブを管理する板内タブ"""
    tab_closing = Signal(object)   # タブが閉じられる直前にビューを通知

    def __init__(self, board: BoardInfo, parent=None):
        super().__init__(parent)
        self._board = board
        self.setTabsClosable(False)  # WrapTabBar が描画
        self.setMovable(False)
        self.tabCloseRequested.connect(self._on_close)

    def _on_close(self, idx: int):
        w = self.widget(idx)
        if isinstance(w, CatalogView): return   # カタログタブは閉じない
        self.tab_closing.emit(w)   # 閉じる前にビューを通知
        self.removeTab(idx)
        if not isinstance(w, CatalogView):
            _dispose_tab_view(w)



# ══════════════════════════════════════════════════════════════════════════════
# 板ペイン: 共通ツールバー(上) + 内側タブ(下)
# ══════════════════════════════════════════════════════════════════════════════


class _ElideLabel(QLabel):
    """テキストが幅に収まらない時 … で省略表示するラベル。
    minimumSizeHint を (0, height) にすることでウィンドウ縮小を妨げない。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""

    def setFullText(self, text: str):
        self._full_text = text
        self.update()

    def minimumSizeHint(self):
        h = super().minimumSizeHint().height()
        from PySide6.QtCore import QSize as _QS
        return _QS(0, h)

    def sizeHint(self):
        h = super().sizeHint().height()
        from PySide6.QtCore import QSize as _QS
        return _QS(0, h)

    def paintEvent(self, event):
        if not self._full_text:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        fm = QFontMetrics(self.font())
        rect = self.contentsRect()
        elided = fm.elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            rect.width()
        )
        # スタイルシートの color を取得
        palette = self.palette()
        painter.setPen(palette.color(palette.ColorRole.WindowText))
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.end()

class BoardPane(QWidget):
    """1板につき1個。新着/返信/更新/自動更新の共通ツールバー + タブを管理する。"""
    tab_closing = Signal(object)   # タブが閉じられる直前にビューを通知

    def __init__(self, board: BoardInfo, main_window, parent=None):
        super().__init__(parent)
        self._board    = board
        self._main     = main_window
        self._settings = main_window._settings   # update_settings で参照
        self._fetcher  = main_window._fetcher

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── 共通ツールバー (タイトル左・ボタン右固定) ──
        # QToolBar はオーバーフロー時にボタンを隠すため QWidget+QHBoxLayout を使用
        _tb_widget = QWidget()
        _tb_widget.setFixedHeight(50)
        _tb_widget.setStyleSheet("background: transparent;")
        tb = QHBoxLayout(_tb_widget)
        tb.setContentsMargins(2, 2, 2, 2)
        tb.setSpacing(0)

        # QToolBarのaddWidget互換ラッパー（後続コードをそのまま使うため）
        class _TBCompat:
            def __init__(self, lay): self._lay = lay
            def addWidget(self, w):  self._lay.addWidget(w, 0)
            def addSeparator(self):
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setStyleSheet(f"color:{_TM.ui('separator_color','#555')};")
                self._lay.addWidget(sep, 0)
        tb_compat = _TBCompat(tb)

        # addWidget/addSeparator は tb_compat 経由で呼ぶ
        # （以降の tb.addWidget → tb_compat.addWidget に置き換え済み）

        # ── アイコン+テキスト下表示ボタン生成ヘルパー ──
        _SP = QStyle.StandardPixmap
        _BTN_STYLE = (
            "QToolButton {"
            " padding-top: 0px; padding-bottom: 1px;"
            " padding-left: 4px; padding-right: 4px;"
            " text-align: center;"
            "}"
        )
        def _itb(label: str, sp, theme_name: str = ""):
            b = QToolButton()
            b.setText(label)
            b.setIcon(_theme_icon(theme_name or f"btn_{label}", sp))
            b.setIconSize(QSize(24, 24))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setAutoRaise(True)
            b.setFixedHeight(46)
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            b.setStyleSheet(_BTN_STYLE)
            return b

        # 左側: スレタイ表示ラベル（stretch=1 で余白をすべて取る）
        self._title_lbl = _ElideLabel()
        self._title_lbl.setStyleSheet(
            f"QLabel{{font-size:11pt;padding-left:6px;color:{_TM.ui('text_primary','#ddd')};}}")
        # スレタイが極端に切り詰められないよう下限幅を確保（余白があれば stretch=1 で
        # さらに伸びる）。約16〜24文字分。狭くしたい/広げたい時はこの値を調整。
        self._title_lbl.setMinimumWidth(260)
        self._title_lbl.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        tb.addWidget(self._title_lbl, 1)   # stretch=1: タイトルだけが伸縮

        # 各ボタン
        self._btn_stop       = _itb("中止", _SP.SP_BrowserStop,    "btn_stop")
        self._btn_update     = _itb("更新", _SP.SP_BrowserReload,  "btn_reload")
        self._btn_reply      = _itb("レス", _SP.SP_ArrowUp,        "btn_reply")
        self._btn_new_thread = _itb("スレ立", _SP.SP_FileDialogNewFolder, "btn_newthread")

        # 移動▼ (MenuButtonPopup: 左=実行, 右=メニュー)
        self._btn_move = QToolButton()
        self._btn_move.setText("移動")
        self._btn_move.setIcon(_theme_icon("btn_move", _SP.SP_ArrowForward))
        self._btn_move.setIconSize(QSize(24, 24))
        self._btn_move.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._btn_move.setAutoRaise(True)
        self._btn_move.setFixedHeight(46)
        self._btn_move.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._btn_move.setStyleSheet(_BTN_STYLE)
        self._btn_move.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        move_menu = QMenu(self._btn_move)
        move_menu.addAction("ページの先頭へ移動(P)	Alt+↑",   self._scroll_top)
        move_menu.addAction("ページの末尾へ移動(M)	Alt+↓",   self._scroll_bottom)
        move_menu.addAction("新着の先頭に移動(T)	Alt+G",     self._scroll_new)
        move_menu.addAction("前回のレス位置に移動(R)	Alt+H", self._scroll_prev_pos)
        move_menu.addSeparator()
        move_menu.addAction("前のしおりへ(B)	Alt+B",         self._scroll_prev_bookmark)
        move_menu.addAction("次のしおりへ(N)	Alt+V",         self._scroll_next_bookmark)
        self._btn_move.setMenu(move_menu)
        self._btn_move.clicked.connect(self._scroll_new)   # 左クリック = 新着に移動

        # 自動更新ボタン（移動と閉じるの間）
        self._btn_ar = _itb("自動更新", _SP.SP_MediaPlay, "btn_ar")

        # 保存ボタン（閉じるの左）
        self._btn_save = QToolButton()
        self._btn_save.setText("保存")
        self._btn_save.setIcon(_theme_icon("btn_save", _SP.SP_DialogSaveButton))
        self._btn_save.setIconSize(QSize(24, 24))
        self._btn_save.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._btn_save.setAutoRaise(True)
        self._btn_save.setFixedHeight(46)
        self._btn_save.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._btn_save.setStyleSheet(_BTN_STYLE)
        self._btn_save.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        save_menu = QMenu(self._btn_save)
        save_menu.addAction("HTMLとして保存",        self._save_html_current)
        save_menu.addAction("MHTMLとして保存(S)",    self._save_mht_current)
        save_menu.addAction("ZIPとして保存",         self._save_zip_current)
        ss_menu = save_menu.addMenu("スクリーンショット(PNG)")
        for _lbl, _k, _d in (
                ("ファイルに全体を保存…",           "full",   "file"),
                ("ファイルに選択範囲を保存…",       "region", "file"),
                ("ファイルに表示部分を保存…",       "view",   "file"),
                (None, None, None),
                ("クリップボードに全体をコピー",     "full",   "clip"),
                ("クリップボードに選択範囲をコピー", "region", "clip"),
                ("クリップボードに表示部分をコピー", "view",   "clip")):
            if _lbl is None:
                ss_menu.addSeparator()
                continue
            ss_menu.addAction(_lbl, lambda k=_k, d=_d: self._screenshot_current(k, d))
        self._btn_save.setMenu(save_menu)
        self._btn_save.clicked.connect(self._save_current_default)  # 左クリック = 前回の保存種類（初期値zip）

        self._btn_close = _itb("閉じる", _SP.SP_TitleBarCloseButton, "btn_close")

        # NG設定ボタン（URLバー行に移動済み・Signal接続用に保持）
        self._btn_ng_settings = _itb("NG設定", _SP.SP_FileDialogDetailedView, "btn_ng")
        self._btn_ng_settings.hide()   # URLバー行に表示するためここでは非表示

        # ボタン配置: 中止 更新 レス | 移動 | スレ立 | 保存 閉じる
        tb_compat.addWidget(self._btn_stop)
        tb_compat.addWidget(self._btn_update)
        tb_compat.addWidget(self._btn_reply)
        tb_compat.addSeparator()
        tb_compat.addWidget(self._btn_move)
        tb_compat.addSeparator()
        tb_compat.addWidget(self._btn_new_thread)
        tb_compat.addSeparator()
        tb_compat.addWidget(self._btn_save)
        tb_compat.addWidget(self._btn_close)
        lay.addWidget(_tb_widget)

        # 内部用 (表示なし) 自動更新タイマー
        self._auto_lbl = QLabel("")

        # ── 内側タブ (tab bar は上部 = North) ──
        self._tabs = QTabWidget()
        self._wrap_bar = WrapTabBar()   # Python 参照を直接保持
        self._wrap_bar._settings = self._settings  # タブ幅設定参照用
        self._tabs.setTabBar(self._wrap_bar)
        self._tabs.setTabsClosable(False)
        self._tabs.setMovable(False)
        self._tabs.tabBar().setUsesScrollButtons(False)
        self._tabs.setIconSize(QSize(16, 16))  # OP画像アイコンサイズ
        self._tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {_TM.ui('tab_border','#555')};background:{_TM.ui('window_bg','#1E1E1E')};}}")
        # WrapTabBar の Python Signal に直接接続
        self._tabs.tabBar().tabCloseRequested.connect(self._on_close_tab)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._prev_tab_idx: int = -1   # タブ切り替え前のインデックス（検索テキスト保存用）
        self._tab_history: list = []   # タブアクティブ履歴（閉じたとき前のタブに戻る用）
        # ダブルクリックで閉じる
        self._tabs.tabBar().tabBarDoubleClicked.connect(self._on_inner_dbl_click)
        # 右クリックメニュー
        self._tabs.tabBar().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._tabs.tabBar().customContextMenuRequested.connect(
            self._show_inner_ctx_menu)
        self._ctx_tab_idx = -1  # 右クリックされたタブ番号
        self._pinned: set = set()  # ピン留め中のウィジェット
        self._wrap_bar._pinned_widgets = self._pinned  # 描画用に参照を共有

        # ── タブ0枚時のプレースホルダ ──
        self._no_tab_widget = QWidget()
        _ntw_lay = QVBoxLayout(self._no_tab_widget)
        _ntw_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _open_cat_btn = QPushButton("📋 カタログを開く")
        _open_cat_btn.setFixedSize(160, 40)
        _open_cat_btn.clicked.connect(self._reopen_catalog)
        _ntw_lay.addWidget(_open_cat_btn)
        self._no_tab_widget.hide()

        # _tabs と _no_tab_widget を QStackedWidget で管理
        self._tab_stack = QStackedWidget()
        self._tab_stack.addWidget(self._tabs)          # index 0
        self._tab_stack.addWidget(self._no_tab_widget) # index 1
        lay.addWidget(self._tab_stack)

        # 自動更新タイマー
        self._auto_interval = 0
        self._auto_remain   = 0
        self._auto_timer    = QTimer(self)
        self._auto_timer.setInterval(1000)
        self._auto_timer.timeout.connect(self._auto_tick)

        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_update.clicked.connect(self._on_update)
        self._btn_reply.clicked.connect(self._on_reply)
        self._btn_new_thread.clicked.connect(self._on_new_thread)
        self._btn_ar.clicked.connect(self._on_open_ar)
        self._btn_close.clicked.connect(self._on_close_current)
        self._btn_ng_settings.clicked.connect(self._on_open_ng_settings)

        # キーボードショートカット（設定から動的に読む）
        self._sc_map = {}   # {action_id: QShortcut}
        _sc_actions = [
            ("scroll_top",      self._scroll_top),
            ("scroll_bottom",   self._scroll_bottom),
            ("scroll_new",      self._scroll_new),
            ("scroll_prev_pos", self._scroll_prev_pos),
            ("scroll_prev_bm",  self._scroll_prev_bookmark),
            ("scroll_next_bm",  self._scroll_next_bookmark),
        ]
        _sc_defaults = {
            "scroll_top": "Alt+Up", "scroll_bottom": "Alt+Down",
            "scroll_new": "Alt+G",  "scroll_prev_pos": "Alt+H",
            "scroll_prev_bm": "Alt+B", "scroll_next_bm": "Alt+V",
        }
        _saved_sc = getattr(self._settings, 'shortcuts', {}) or {}
        for aid, fn in _sc_actions:
            _key = _saved_sc.get(aid, "") or _sc_defaults[aid]
            sc = QShortcut(QKeySequence(_key), self, fn)
            # 板タブ(BoardPane)ごとに同じキーが登録されるため、既定の
            # WindowShortcut のままだと板を複数開いた時点で同一キーが重複し、
            # Qt が Ambiguous と判定してどれも発火しなくなる。
            # このペイン配下にフォーカスがある時だけ効くよう限定する。
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._sc_map[aid] = sc

    def update_shortcuts(self, settings):
        """設定変更後にショートカットキーを再設定する"""
        _sc_defaults = {
            "scroll_top": "Alt+Up", "scroll_bottom": "Alt+Down",
            "scroll_new": "Alt+G",  "scroll_prev_pos": "Alt+H",
            "scroll_prev_bm": "Alt+B", "scroll_next_bm": "Alt+V",
        }
        _saved = getattr(settings, 'shortcuts', {}) or {}
        for aid, sc in self._sc_map.items():
            _key = _saved.get(aid, "") or _sc_defaults.get(aid, "")
            sc.setKey(QKeySequence(_key))

    # ── タブ操作の委譲 ────────────────────────────────────────────────────────
    def addTab(self, w, l):
        r = self._tabs.addTab(w, l)
        if self._tabs.count() > 0:
            self._tab_stack.setCurrentIndex(0)
        return r
    def insertTab(self, i, w, l):
        r = self._tabs.insertTab(i, w, l)
        # タブが追加されたらプレースホルダを非表示にしてタブを表示
        if self._tabs.count() > 0:
            self._tab_stack.setCurrentIndex(0)
        return r
    def removeTab(self, i):        self._tabs.removeTab(i)
    def count(self):               return self._tabs.count()
    def widget(self, i):           return self._tabs.widget(i)
    def currentWidget(self):       return self._tabs.currentWidget()
    def currentIndex(self):        return self._tabs.currentIndex()
    def setCurrentIndex(self, i):  self._tabs.setCurrentIndex(i)
    def indexOf(self, w):
        # w(ThreadView等)が非同期コールバック経由で渡された場合、その間にタブが
        # 閉じられてC++オブジェクトが破棄済みのことがある。shiboken の
        # 「already deleted」RuntimeErrorが未処理のままQtの仮想関数呼び出し
        # スタックを遡って伝播すると（多重ネスト経由で）クラッシュに至るため、
        # ここで確実に吸収して「見つからない」(-1)として返す。
        try:
            return self._tabs.indexOf(w)
        except RuntimeError:
            return -1
    def tabText(self, i):          return self._tabs.tabText(i)
    def setTabText(self, i, t):    self._tabs.setTabText(i, t)
    def setTabIcon(self, i, ic):   self._wrap_bar.setTabIcon(i, ic)
    def tabBar(self):              return self._wrap_bar

    def _on_close_tab(self, idx: int):
        w = self._tabs.widget(idx)
        if isinstance(w, CatalogView): return   # カタログタブは閉じない
        if w in self._pinned: return
        self.tab_closing.emit(w)     # 閉じる前にビューを通知

        # 履歴から戻り先を決定（画像タブを閉じた時のみ前のタブに戻る）
        target = -1
        if isinstance(w, ImageTabView):
            # 元スレ（_src_thread_view）が同じinner内にあればそこへ戻る
            src_view = getattr(w, '_src_thread_view', None)
            if src_view is not None:
                for i in range(self._tabs.count()):
                    if i != idx and self._tabs.widget(i) is src_view:
                        target = i if i < idx else i - 1
                        break
            # 元スレが見つからない場合は履歴から探す
            if target < 0:
                while self._tab_history:
                    h = self._tab_history.pop()
                    if h == idx:
                        continue
                    target = h if h < idx else h - 1
                    break

        self._tabs.removeTab(idx)
        if not isinstance(w, CatalogView):
            _dispose_tab_view(w)

        # 履歴内の残りインデックスを補正（閉じたタブ以降をデクリメント）
        self._tab_history = [
            (h if h < idx else h - 1)
            for h in self._tab_history if h != idx
        ]

        if self._tabs.count() == 0:
            self._tab_stack.setCurrentIndex(1)  # カタログを開くボタン表示
            self._title_lbl.setFullText("")
            return

        if target >= 0 and target < self._tabs.count():
            self._tabs.setCurrentIndex(target)


    def _get_webview(self, widget) -> "QWebEngineView | None":
        """タブウィジェットからQWebEngineViewを取得する（ThreadView / CatalogView対応）"""
        if isinstance(widget, ThreadView):
            return widget._view
        if hasattr(widget, '_web_view'):
            return widget._web_view
        if hasattr(widget, '_view') and isinstance(widget._view, QWebEngineView):
            return widget._view
        return None

    def _on_tab_changed(self, idx: int):
        # ドラッグ中のcurrentChanged発火は完全スキップ（ちらつき防止）
        bar = self._tabs.tabBar()
        if getattr(bar, '_drag_active', False):
            return

        if self._main: self._main._update_url_from_active()

        # ── 旧タブの検索テキストをウィジェット属性に保存 ──────────────────
        if self._prev_tab_idx >= 0:
            old_w = self._tabs.widget(self._prev_tab_idx)
            if isinstance(old_w, ThreadView):
                old_w._saved_search = old_w._search_edit.text()
                _old_view = old_w._view
                QTimer.singleShot(0, lambda v=_old_view: v.page().runJavaScript(
                    'try{extractPosts("");extractPostsPopup("");}catch(e){}'))
            # 旧タブをFreezeしてCPU・メモリ消費を抑制
            _old_web = self._get_webview(old_w)

        # タブ履歴に追加（閉じたとき前のタブに戻る用）
        _old_idx = self._prev_tab_idx
        if _old_idx >= 0 and _old_idx != idx:
            if not self._tab_history or self._tab_history[-1] != _old_idx:
                self._tab_history.append(_old_idx)
        self._prev_tab_idx = idx

        w = self._tabs.currentWidget()
        is_thread = isinstance(w, ThreadView)
        # 画像タブの場合は元スレを参照
        _src_thread = None
        if isinstance(w, ImageTabView):
            _src_thread = getattr(w, '_src_thread_view', None)
            if not isinstance(_src_thread, ThreadView):
                _src_thread = None
        self._btn_reply.setEnabled(is_thread)
        self._btn_move.setEnabled(is_thread)
        # ── スレタイ更新 ──
        self._update_title_lbl(w)
        if not is_thread and _src_thread is None:
            self._auto_timer.stop()
            self._auto_lbl.setText("")
            return

        # 画像タブで元スレあり → 元スレのカウントダウンを表示して終了
        if _src_thread is not None:
            if self._main and hasattr(self._main, '_ar_mgr') and _src_thread._thread:
                entry = self._main._ar_mgr.find_entry_by_url(_src_thread._thread.url or '')
                if entry:
                    mgr = self._main._ar_mgr
                    for i in range(mgr.entry_count()):
                        if mgr.entry(i) is entry:
                            _src_thread.update_countdown(mgr.remaining(i))
                            break
                else:
                    _src_thread.update_countdown(-1)
            return

        # ── 新タブの検索テキストを復元 ────────────────────────────────────
        saved = getattr(w, '_saved_search', "")
        # NG設定変更で保留された全再描画を最優先で消化する（フル再描画が走るので
        # 差分パスの _pending_redraw ブロックはスキップして良い）
        _ng_consumed = isinstance(w, ThreadView) and w._consume_ng_dirty()
        # 非表示中にAR更新が来ていた場合、ここで最新HTMLを再ロードして
        # 「タブ青→開いたら新着が出ない」取りこぼしを解消する
        if not _ng_consumed and isinstance(w, ThreadView) and getattr(w, '_pending_redraw', False):
            # _pending_redraw は追記/再描画の完了後（_sync_after_redraw）で解除する。
            # ここで即解除すると、後発の status 更新が DOM 反映前に新着込みの数を
            # 先出ししてしまう（アクティブ化直後だけ多く見える不具合の原因）。
            _checked_r = w._mode_grp.checkedButton() if hasattr(w, '_mode_grp') else None
            _cur_mode_r = _checked_r.property("mode") if _checked_r else ""
            if _cur_mode_r in ("image", "quote"):
                w._pending_frags = []   # モード再描画は最新モデルから全生成するため不要
                w._set_view_mode(_cur_mode_r)
            else:
                # 非表示中にたまった新着フラグメントがあり、ページがライブなら
                # フルリロードせずDOM追記で反映する（フルリロードはスクロール復元前に
                # 一瞬先頭が見えてちらつくため）。スクロール位置は一切動かない。
                _frags_r = getattr(w, '_pending_frags', None) or []
                if _frags_r and getattr(w, '_thread_page_live', False):
                    import json as _json_r
                    w._pending_frags = []
                    w._view.page().runJavaScript(
                        f"appendNewReplies({_json_r.dumps(_frags_r, ensure_ascii=False)});"
                        + w._expiry_banner_sync_js(w._thread))
                else:
                    # フラグメントが無い（エラー再ロードで破棄済み等）または
                    # ページ未ロード → 従来どおり最新HTMLをフルリロード（取りこぼし防止）
                    w._pending_frags = []
                    # 自動更新中は _last_html を遅延生成方式にしているため、再ロード前に
                    # dirty なら最新モデルから作り直す（古いHTMLで再ロードして新着が
                    # 消えるのを防ぐ）。
                    if getattr(w, '_last_html_dirty', False) and w._thread:
                        w._rebuild_last_html()
                    if getattr(w, '_last_html', ""):
                        _url_r = (w._thread.url if w._thread else None) or 'https://www.2chan.net/'
                        # 全リロードで先頭に戻るのを防ぐため、再ロード前に現在の
                        # スクロール位置を読み取り _pending_scroll に渡す
                        # （_on_load_finished_scroll が読込完了後に復元する）。
                        _html_r = w._last_html
                        def _reload_keep_scroll(_y, _w=w, _h=_html_r, _u=_url_r):
                            try:
                                _w._pending_scroll = int(_y) if _y else 0
                            except Exception:
                                _w._pending_scroll = 0
                            _w._load_html_via_tempfile(_h, QUrl(_u))
                        w._view.page().runJavaScript("window.scrollY", _reload_keep_scroll)
            # 追記/再描画の完了を待ってから _pending_redraw を解除し、DOM反映済み
            # レス数を同期してステータスを更新する（先出しで多く見える不具合の解消）。
            def _sync_after_redraw(_w=w):
                _th = getattr(_w, '_thread', None)
                if _th is not None:
                    _w._displayed_res_count = len(_th.res_list)
                    # AutoRefresh経由の追記は _update_ui_after_show を通らず
                    # モードボタン左の _lbl_count が古いまま「多く」/「少なく」
                    # ずれるため、DOM反映済み状態に合わせてラベルも同期する。
                    _new_c = sum(1 for r in _th.res_list[1:] if r.is_new)
                    _w._refresh_count_label(_th, _new_c)
                _w._pending_redraw = False
                _w.refresh_status_info()
            QTimer.singleShot(240, _sync_after_redraw)
        # アクティブ化時に「末尾まで表示済みなら未読(青背景)を解除」を再評価する。
        # 背景でロードされ innerHeight=0 のまま初回判定が効かなかった画像モード等で、
        # 表示後に末尾が見えていれば青背景をデフォルトに戻す（少し遅延でレイアウト確定後）。
        if isinstance(w, ThreadView):
            _wv = w._view
            # 180ms後に実行されるため、その間にタブが閉じられている可能性がある
            QTimer.singleShot(180, lambda v=_wv: _safe_run_js(
                v, "try{window._checkUnreadAtBottom&&window._checkUnreadAtBottom();}catch(e){}"))
        self._search_edit.blockSignals(True) if hasattr(self, '_search_edit') else None
        w._search_edit.blockSignals(True)
        w._search_edit.setText(saved)
        w._search_edit.blockSignals(False)
        self._search_edit.blockSignals(False) if hasattr(self, '_search_edit') else None
        # 「ポップアップ」チェックの表示を設定と同期（他タブで切り替えた場合）
        if hasattr(w, '_chk_extract_popup'):
            w._chk_extract_popup.blockSignals(True)
            w._chk_extract_popup.setChecked(
                getattr(w._settings, 'extract_popup', True))
            w._chk_extract_popup.blockSignals(False)
        if saved:
            w._do_extract(saved)   # 現在の抽出モード（スレ内/パネル）で再適用
        # ヒートマップ チェックの表示を設定と同期し、パネルを再適用
        if isinstance(w, ThreadView) and hasattr(w, '_chk_heatmap'):
            w._chk_heatmap.blockSignals(True)
            w._chk_heatmap.setChecked(
                getattr(w._settings, 'show_post_heatmap', False))
            w._chk_heatmap.blockSignals(False)
            w._apply_heatmap()
        # そうだね順 チェックの表示を設定と同期（他タブで切り替えた場合）
        if isinstance(w, ThreadView) and hasattr(w, '_chk_sodane'):
            w._chk_sodane.blockSignals(True)
            w._chk_sodane.setChecked(getattr(w._settings, 'sort_by_sodane', False))
            w._chk_sodane.blockSignals(False)

        # ── 棒読み・スクロールチェックボックスをARエントリと同期 ──
        if isinstance(w, ThreadView) and hasattr(w, '_chk_ar_bouyomi'):
            entry = None
            if self._main and hasattr(self._main, '_ar_mgr') and w._thread:
                entry = self._main._ar_mgr.find_entry_by_url(w._thread.url or '')
            w._chk_ar_bouyomi.blockSignals(True)
            w._chk_ar_bouyomi.setChecked(entry.bouyomi if entry else False)
            w._chk_ar_bouyomi.blockSignals(False)
            if hasattr(w, '_chk_ar_scroll'):
                w._chk_ar_scroll.blockSignals(True)
                w._chk_ar_scroll.setChecked(entry.scroll_to_new if entry else False)
                w._chk_ar_scroll.blockSignals(False)
            # カウントダウン初期表示
            if hasattr(w, 'update_countdown'):
                if entry and self._main:
                    mgr = self._main._ar_mgr
                    for i in range(mgr.entry_count()):
                        if mgr.entry(i) is entry:
                            w.update_countdown(mgr.remaining(i))
                            break
                else:
                    w.update_countdown(-1)

    def _reopen_catalog(self):
        """タブが0枚の状態からカタログを再作成して表示する"""
        if self._main and self._board:
            self._main._ensure_catalog_exists(self._board)
        self._tab_stack.setCurrentIndex(0)

    def _update_title_lbl(self, w=None):
        """ツールバーのスレタイラベルを現在タブに合わせて更新する"""
        if w is None:
            w = self._tabs.currentWidget()
        if isinstance(w, ThreadView):
            t = getattr(w, '_thread', None)
            if t and t.res_list:
                import re as _re
                # OP本文の先頭行を表示（題名が短い/無題でも本文で識別できるように）。
                # 各行頭のIP表示 [xxx] を除去し、最初の非空行を採用。
                raw = _re.sub(r'<[^>]+>', '', (t.res_list[0].comment_text or ""))
                # 先頭のIP表示 [xxx] を除去。IP表示は <br>/<font> で複数行に分断され
                # 「[」だけが先頭行に残ることがあるため、行分割の前に改行をまたいで
                # 先頭の [..] をまとめて除去する（[^\]]* は改行も含む）。
                raw = _re.sub(r'^\s*\[[^\]]*\]\s*', '', raw)
                line = next((_l.strip() for _l in raw.splitlines() if _l.strip()), "")
                if not line:
                    # 本文が無い（画像のみ等）→ 題名にフォールバック
                    _tt = (t.title or "").strip()
                    if _tt and not _re.match(r'^No\.\d+', _tt):
                        line = _tt
                self._title_lbl.setFullText(line[:120])
            else:
                self._title_lbl.setFullText("")
        elif isinstance(w, CatalogView):
            b = getattr(w, '_board', None)
            self._title_lbl.setFullText(b.name if b else "カタログ")
        else:
            self._title_lbl.setFullText("")

    def _current_thread(self):
        w = self._tabs.currentWidget()
        return w if isinstance(w, ThreadView) else None

    # ── ツールバーボタン ───────────────────────────────────────────────────────
    def _on_stop(self):
        w = self._tabs.currentWidget()
        if hasattr(w, '_view'): w._view.stop()

    def _on_reply(self):
        tv = self._current_thread()
        if tv: tv.open_reply_window.emit(0, "")

    def _on_update(self):
        w = self._tabs.currentWidget()
        if isinstance(w, ThreadView):
            w.request_manual_reload()  # リーディングエッジ＋1秒クールダウン（連打抑制）
            # 自動更新に登録済みなら残り時間をリセット
            if self._main and w._thread:
                self._main._ar_mgr.reset_remain_by_url(w._thread.url or "")
        elif isinstance(w, CatalogView) and w._board:
            w.load(w._board)
            # カタログも自動更新登録済みなら残り時間をリセット
            if self._main and w._board:
                self._main._ar_mgr.reset_remain_by_url(w._board.catalog_url)

    def _on_open_ar(self):
        """アクティブビューの自動更新ダイアログを開く"""
        w = self._tabs.currentWidget()
        if isinstance(w, (ThreadView, CatalogView)):
            w.auto_refresh_requested.emit()
        elif isinstance(w, ImageTabView):
            # 画像タブ表示中は元スレに委譲
            src = getattr(w, '_src_thread_view', None)
            if src and isinstance(src, ThreadView):
                src.auto_refresh_requested.emit()

    def _on_new_thread(self):
        if self._main: self._main._new_thread()

    def _on_close_current(self):
        idx = self._tabs.currentIndex()
        if idx < 0: return
        w = self._tabs.widget(idx)
        if w in self._pinned: return
        if isinstance(w, CatalogView): return
        self.tab_closing.emit(w)
        self._tabs.removeTab(idx)
        _dispose_tab_view(w)
        if self._tabs.count() == 0:
            self._tab_stack.setCurrentIndex(1)
            self._title_lbl.setFullText("")

    def _on_open_ng_settings(self):
        """NG設定ダイアログを開く"""
        p = self
        while p and not hasattr(p, '_open_ng_settings'):
            p = p.parent()
        if p:
            p._open_ng_settings()
        else:
            # MainWindowが見つからない場合は直接開く
            from futaba2b_dialogs import NgSettingsDialog
            dlg = NgSettingsDialog(self._settings, parent=self)
            dlg.exec()

    # ── スクロール ────────────────────────────────────────────────────────────
    def _run_js(self, js: str):
        tv = self._current_thread()
        if tv: tv._view.page().runJavaScript(js)

    def _scroll_top(self):
        self._run_js("window.scrollTo(0,0);")

    def _scroll_bottom(self):
        self._run_js("window.scrollTo(0,document.body.scrollHeight);")

    def _scroll_new(self):
        self._run_js(
            "var el=document.querySelector('.new-res');"
            "if(el)el.scrollIntoView({behavior:'smooth',block:'start'});"
            "else window.scrollTo(0,document.body.scrollHeight);"
        )

    def _scroll_prev_pos(self):
        tv = self._current_thread()
        if tv and getattr(tv, '_prev_scroll_y', 0):
            tv._view.page().runJavaScript(f"window.scrollTo(0,{tv._prev_scroll_y});")

    def _scroll_prev_bookmark(self): pass   # TODO: しおり機能

    def _remember_save_format(self, fmt: str):
        """保存ボタン左クリックの初期値として今回の保存種類を記憶する"""
        if getattr(self._settings, "last_save_format", "zip") != fmt:
            self._settings.last_save_format = fmt
            try:
                self._settings.save()
            except Exception:
                pass

    def _save_current_default(self):
        """保存ボタン左クリック: 前回選んだ保存種類で保存（初期値=zip）"""
        fmt = getattr(self._settings, "last_save_format", "zip")
        {"html": self._save_html_current,
         "mht":  self._save_mht_current,
         "zip":  self._save_zip_current}.get(fmt, self._save_zip_current)()

    def _save_mht_current(self):
        self._remember_save_format("mht")
        w = self._tabs.currentWidget()
        if isinstance(w, ThreadView): w.save_as_mht()

    def _save_html_current(self):
        self._remember_save_format("html")
        w = self._tabs.currentWidget()
        if isinstance(w, ThreadView): w.save_as_html()

    def _save_zip_current(self):
        self._remember_save_format("zip")
        w = self._tabs.currentWidget()
        if isinstance(w, ThreadView): w.save_as_zip()

    def _screenshot_current(self, kind: str, dest: str):
        """保存ボタンメニューのスクリーンショット → MainWindowの共通処理へ委譲"""
        w = self.window()
        if hasattr(w, '_screenshot_action'):
            w._screenshot_action(kind, dest)

    def _scroll_next_bookmark(self): pass

    # ── 自動更新 (非表示タイマー) ─────────────────────────────────────────────
    _AUTO_INTERVALS = [0, 30, 60, 120, 300]


    def _auto_tick(self):
        if self._auto_remain <= 0: return
        self._auto_remain -= 1
        if self._auto_remain <= 0:
            self._auto_remain = self._auto_interval
            tv = self._current_thread()
            if tv: tv.reload_thread()

    # ── タブ ダブルクリック ────────────────────────────────────────────────
    def _on_inner_dbl_click(self, idx: int):
        if idx >= 0:
            w = self._tabs.widget(idx)
            if w in self._pinned:
                self._unpin_tab(w)   # ピン済み → ダブルクリックで解除
            else:
                self._on_close_tab(idx)  # 通常 → 閉じる

    # ── 右クリックメニュー ────────────────────────────────────────────────
    def _show_inner_ctx_menu(self, pos):
        bar = self._tabs.tabBar()
        # WrapTabBar が _ctx_idx を設定している場合はそちらを優先
        self._ctx_tab_idx = getattr(bar, "_ctx_idx", -1)
        if self._ctx_tab_idx < 0:
            self._ctx_tab_idx = bar.tabAt(pos)  # fallback
        if self._ctx_tab_idx < 0: return
        w   = self._tabs.widget(self._ctx_tab_idx)
        # インデックスより先にウィジェット参照を保持（_toggle_pin でインデックスがズレても確実に正しいウィジェットを使う）
        self._ctx_tab_widget = w
        is_cat = isinstance(w, CatalogView)

        menu = QMenu(self)
        menu.addAction("閉じる (C)",
            lambda: self._on_close_tab(self._ctx_tab_idx))
        menu.addSeparator()
        menu.addAction("このタブ以外閉じる (B)", self._ctx_close_others)
        menu.addAction("これより左を閉じる (L)", self._ctx_close_left)
        menu.addAction("これより右を閉じる (R)", self._ctx_close_right)
        menu.addSeparator()
        # 自動更新に追加（ThreadView かつスレ読み込み済みのみ有効）
        _w_ar = self._tabs.widget(self._ctx_tab_idx)
        _is_thread_ar = isinstance(_w_ar, ThreadView) and getattr(_w_ar, '_thread', None) is not None
        _ar_act = menu.addAction("自動更新に追加 (A)...")
        if _is_thread_ar and self._main:
            _ar_act.triggered.connect(lambda: self._main._open_ar_dialog(_w_ar))
        else:
            _ar_act.setEnabled(False)
        menu.addAction("お気に入りに追加 (F)",               self._ctx_add_fav)
        menu.addAction("アドレスをクリップボードにコピー (T)", self._ctx_copy_url)
        menu.addAction("外部ブラウザにアドレスを送る (W)  F11", self._ctx_open_browser)
        menu.addSeparator()
        _pin_lbl = "ピンを外す (H)" if self._tabs.widget(self._ctx_tab_idx) in self._pinned else "タブのピン留め (H)"
        menu.addAction(_pin_lbl, self._toggle_pin)
        menu.addSeparator()
        _w_ctx = self._tabs.widget(self._ctx_tab_idx)
        _is_thread_ctx = isinstance(_w_ctx, ThreadView)
        save_menu = menu.addMenu("ログ保存")
        save_menu.setEnabled(_is_thread_ctx)
        if _is_thread_ctx:
            save_menu.addAction("HTMLとして保存",        self._ctx_save_html)
            save_menu.addAction("MHTMLとして保存",       self._ctx_save_mht)
            save_menu.addAction("ZIPとして保存",         self._ctx_save_zip)
        menu.addSeparator()
        # 再取得（開き直し）: スレタブのみ有効・保存ログは不可
        _refetch_act = menu.addAction("再取得 (G)", self._ctx_refetch)
        _refetch_act.setEnabled(_is_thread_ctx and not getattr(_w_ctx, '_is_log', False))
        # ── ヒットした逆NG（ThreadViewのみ・ヒット時のみ表示・選択不可）──
        _w_rev = self._tabs.widget(self._ctx_tab_idx)
        if (isinstance(_w_rev, ThreadView)
                and getattr(_w_rev, "_thread", None) is not None and self._main):
            try:
                _ngf = self._main._settings.ng_filter
                _matched, _seen = [], set()
                # OP（スレ文）のみで判定する。スレ全体の走査はレス数に比例して
                # 重く、メニュー表示が遅延するため行わない。
                _res = _w_rev._thread.res_list or []
                if _res:
                    for _ng in _ngf.get_matched_reverse_ng_words(_res[0]):
                        _p = (_ng.get("pattern", "") or "").strip()
                        if _p and _p not in _seen:
                            _seen.add(_p); _matched.append(_p)
                # カタログ件名でヒットした逆NGも対象に含める
                # （scope_catalog 専用の逆NG等、OPレス側のスコープでは拾えないもの）
                _brd = getattr(_w_rev, "_board", None)
                _cat_entry = self._find_catalog_entry(
                    _brd, getattr(_w_rev, "_thread_no", None))
                _tc_rev = -1   # 板不明=全文（判定不能フォールバック）
                if _brd is not None:
                    try:
                        from futaba2b_settings import get_board_settings as _gbs
                        _cc = getattr(_gbs(_brd.base_url), "cat_chars", None)
                        # 板の cat_chars（0含む）はそのまま採用。None のみ全文(-1)。
                        _tc_rev = int(_cc) if _cc is not None else -1
                    except Exception:
                        _tc_rev = -1
                if _cat_entry is not None:
                    for _ng in _ngf.get_matched_reverse_ng_words_catalog(
                            _cat_entry, title_chars=_tc_rev):
                        _p = (_ng.get("pattern", "") or "").strip()
                        if _p and _p not in _seen:
                            _seen.add(_p); _matched.append(_p)
            except Exception:
                _matched = []
            if _matched:
                menu.addSeparator()
                _hdr = menu.addAction("↓ヒットした逆NG")
                _hdr.setEnabled(False)
                for _p in _matched:
                    menu.addAction(_p).setEnabled(False)
        menu.exec(self._tabs.tabBar().mapToGlobal(pos))

    def _find_catalog_entry(self, board, no):
        """スレに対応するカタログエントリを探す。
        開いているカタログタブ（CatalogView）の _all_entries から、同じ板かつ
        no 一致のエントリを返す。見つからなければ None。"""
        if not no:
            return None
        _burl = (getattr(board, "url", "") or "").rstrip("/")
        for i in range(self._tabs.count()):
            w = self._tabs.widget(i)
            if not isinstance(w, CatalogView):
                continue
            _cb = getattr(w, "_board", None)
            _cburl = (getattr(_cb, "url", "") or "").rstrip("/")
            # 板URLが両方取れる場合のみ板一致を要求（取れない場合は no のみで照合）
            if _burl and _cburl and _burl != _cburl:
                continue
            for e in (getattr(w, "_all_entries", None) or []):
                if getattr(e, "no", None) == no:
                    return e
        return None

    def _get_tab_url(self, idx: int) -> str:
        w = self._tabs.widget(idx)
        if isinstance(w, ThreadView) and w._thread:
            return w._thread.url or ""
        if isinstance(w, CatalogView) and w._board:
            return w._board.catalog_url
        return ""

    def _ctx_close_others(self):
        keep = self._ctx_tab_idx
        for i in range(self._tabs.count() - 1, -1, -1):
            _w = self._tabs.widget(i)
            if i != keep and not isinstance(_w, CatalogView) and _w not in self._pinned:
                self.tab_closing.emit(_w)
                self._tabs.removeTab(i)
                _dispose_tab_view(_w)
                if i < keep: keep -= 1

    def _ctx_close_left(self):
        for i in range(self._ctx_tab_idx - 1, -1, -1):
            _w = self._tabs.widget(i)
            if not isinstance(_w, CatalogView) and _w not in self._pinned:
                self.tab_closing.emit(_w)
                self._tabs.removeTab(i)
                _dispose_tab_view(_w)

    def _ctx_close_right(self):
        for i in range(self._tabs.count() - 1, self._ctx_tab_idx, -1):
            _w = self._tabs.widget(i)
            if not isinstance(_w, CatalogView) and _w not in self._pinned:
                self.tab_closing.emit(_w)
                self._tabs.removeTab(i)
                _dispose_tab_view(_w)

    def _ctx_add_fav(self):
        url = self._get_tab_url(self._ctx_tab_idx)
        lbl = self._tabs.tabText(self._ctx_tab_idx)
        if url and self._main:
            self._main._settings.add_favorite(lbl, url)
            self._main._settings.save()
            self._main._tree_pane.refresh_favorites()

    def _ctx_copy_url(self):
        url = self._get_tab_url(self._ctx_tab_idx)
        if url: QGuiApplication.clipboard().setText(url)

    def _ctx_open_browser(self):
        url = self._get_tab_url(self._ctx_tab_idx)
        if url: _open_url(url)



    def _ctx_save_html(self):
        w = self._tabs.widget(self._ctx_tab_idx)
        if isinstance(w, ThreadView): w.save_as_html()

    def _ctx_save_mht(self):
        w = self._tabs.widget(self._ctx_tab_idx)
        if isinstance(w, ThreadView): w.save_as_mht()

    def _ctx_save_zip(self):
        w = self._tabs.widget(self._ctx_tab_idx)
        if isinstance(w, ThreadView): w.save_as_zip()


    def _ctx_refetch(self):
        """右クリックしたスレタブを再取得（開き直し）する。
        サーバから全再取得＋DOM全再描画し、表示モード・スクロール位置・
        既読(+N)は維持する。保存ログ/非スレタブでは無効。"""
        w = self._tabs.widget(self._ctx_tab_idx)
        if isinstance(w, ThreadView) and not getattr(w, '_is_log', False):
            w.refetch_thread()


    # ── ピン留め ──────────────────────────────────────────────


    def _pin_tab(self, widget):
        self._pinned.add(widget)
        self._tabs.tabBar().update()  # 再描画でピンマーク表示

    def _unpin_tab(self, widget):
        self._pinned.discard(widget)
        self._tabs.tabBar().update()  # 再描画でピンマーク非表示

    def _toggle_pin(self):
        # _ctx_tab_widget を優先 (インデックスより信頼性が高い)
        w = getattr(self, '_ctx_tab_widget', None)
        if w is None:
            idx = self._ctx_tab_idx
            if idx < 0: return
            w = self._tabs.widget(idx)
        if w is None: return
        if w in self._pinned: self._unpin_tab(w)
        else:                  self._pin_tab(w)



# ══════════════════════════════════════════════════════════════════════════════
# ネイティブ動画プレーヤーウィンドウ
# ══════════════════════════════════════════════════════════════════════════════

# ── 動画キャッシュディレクトリ ─────────────────────────────────────────────
import os as _os
_VIDEO_CACHE_DIR = Path(
    _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~"))
) / "2BP" / "video_cache"
_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── 動画キャッシュディレクトリ ─────────────────────────────────────────────
import os as _os
_VIDEO_CACHE_DIR = Path(
    _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~"))
) / "2BP" / "video_cache"
_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── 動画キャッシュディレクトリ ─────────────────────────────────────────────
import os as _os
_VIDEO_CACHE_DIR = Path(
    _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~"))
) / "2BP" / "video_cache"
_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class VideoPlayerWindow(QWidget):
    """QMediaPlayer を使ったネイティブ動画再生ウィンドウ。
    fetcher が渡された場合はキャッシュ確認後にダウンロードして再生
    （HTTPS ストリーミング時の TLS close_notify 警告を回避）。"""

    # バックグラウンドスレッド → メインスレッドへの安全な通知用シグナル
    _sig_local    = Signal(str)       # ダウンロード完了 → ローカルパス
    _sig_url      = Signal(str)       # フォールバック   → URL 直接再生
    _sig_progress = Signal(str)       # 進捗テキスト（スレッドセーフ）

    @staticmethod
    def _cache_path(url: str) -> Path:
        """URL からキャッシュファイルパスを返す（2chan のファイル名はユニーク）"""
        fname = url.rstrip("/").split("/")[-1].split("?")[0]
        if not fname or "." not in fname:
            import hashlib
            fname = hashlib.md5(url.encode()).hexdigest()[:16] + ".mp4"
        return _VIDEO_CACHE_DIR / fname

    def __init__(self, url: str, fetcher=None, parent=None, settings=None):
        super().__init__(parent, Qt.WindowType.Window)
        self._settings = settings
        fname = url.rstrip('/').split('/')[-1]
        self.setWindowTitle(f"動画: {fname}")
        self.resize(720, 460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._tmp_path: str | None = None


        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except ImportError as e:
            lbl = QLabel(
                "PySide6.QtMultimedia が見つかりません。\n"
                "pip install PySide6-Addons を試してください。\n\n"
                "外部プレーヤーで開きます。")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(lbl)
            _open_url(url)
            self.show()
            return

        # ── ローディング表示 ─────────────────────────────────────────────
        self._lbl_load = QLabel("⏳ ダウンロード中...", self)
        self._lbl_load.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_load.setStyleSheet("background:#111;color:#aaa;font-size:11pt;")
        self._lbl_load.setMinimumHeight(300)
        lay.addWidget(self._lbl_load, 1)

        # ── 映像エリア ────────────────────────────────────────────────────
        self._video = QVideoWidget(self)
        self._video.setMinimumSize(320, 240)
        self._video.setStyleSheet("background:#000;")
        self._video.hide()
        lay.addWidget(self._video, 1)

        # ── コントロールバー ──────────────────────────────────────────────
        bar = QWidget(self)
        bar.setFixedHeight(34)
        bar.setStyleSheet("QWidget{background:#1e1e1e;color:#ddd;}"
                          "QPushButton{background:#333;border:none;color:#ddd;"
                          "padding:2px 8px;font-size:13px;}"
                          "QPushButton:hover{background:#555;}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 2, 6, 2)
        bl.setSpacing(6)

        self._btn = QPushButton("▶", bar)
        self._btn.setFixedWidth(34)
        self._btn.clicked.connect(self._toggle_play)
        bl.addWidget(self._btn)

        from PySide6.QtWidgets import QSlider as _QSlider
        _ss = ("QSlider::groove:horizontal{background:#444;height:4px;border-radius:2px;}"
               "QSlider::sub-page:horizontal{background:#999;height:4px;border-radius:2px;}"
               "QSlider::handle:horizontal{background:#ddd;width:12px;height:12px;"
               "margin:-4px 0;border-radius:6px;}")
        self._seek = _QSlider(Qt.Orientation.Horizontal, bar)
        self._seek.setRange(0, 10000)
        self._seek.sliderPressed.connect(self._seek_start)
        self._seek.sliderReleased.connect(self._seek_end)
        self._seek.setStyleSheet(_ss)
        bl.addWidget(self._seek, 1)

        self._lbl = QLabel("--:-- / --:--", bar)
        self._lbl.setFixedWidth(88)
        self._lbl.setStyleSheet("color:#aaa;font-size:8pt;")
        bl.addWidget(self._lbl)

        bl.addWidget(QLabel("🔊", bar))
        vol = _QSlider(Qt.Orientation.Horizontal, bar)
        _init_vol = int(getattr(self._settings, "video_volume", 80)) if self._settings else 80
        vol.setRange(0, 100); vol.setValue(_init_vol); vol.setFixedWidth(72)
        vol.setStyleSheet(_ss)
        bl.addWidget(vol)
        self._vol_slider = vol

        lay.addWidget(bar)

        # ── プレーヤー設定 ────────────────────────────────────────────────
        print("[VID] win_init: QMediaPlayer", flush=True)
        self._player = QMediaPlayer(self)
        print("[VID] win_init: QAudioOutput", flush=True)
        self._audio  = QAudioOutput(self)
        self._audio.setVolume(_init_vol / 100.0)
        vol.valueChanged.connect(self._on_video_volume_changed)
        self._player.setAudioOutput(self._audio)
        # setVideoOutput は _start_* で video が visible になってから呼ぶ

        self._player.playbackStateChanged.connect(self._on_state)
        self._player.positionChanged.connect(self._on_pos)
        self._player.durationChanged.connect(self._on_dur)
        self._player.errorOccurred.connect(self._on_err)

        self._dur     = 0
        self._seeking = False

        # シグナルをメインスレッドのスロットに接続（スレッドセーフな橋渡し）
        self._sig_local.connect(self._start_local)
        self._sig_url.connect(self._start_url)
        self._sig_progress.connect(self._lbl_load.setText)  # スレッドセーフな進捗更新

        if fetcher:
            cp = self._cache_path(url)
            if cp.exists() and not _video_cache_valid(cp):
                # 破損キャッシュ（旧版の書きかけ残骸等）は削除して再ダウンロード
                try: cp.unlink()
                except OSError: pass
            if cp.exists() and cp.stat().st_size > 0:
                # ── キャッシュ HIT ──
                self._tmp_path = str(cp)
                self._lbl_load.setText("⏳ キャッシュから読み込み中...")
                # QTimer で次のイベントループに回してから emit（show() 後）
                QTimer.singleShot(0, lambda: self._sig_local.emit(self._tmp_path))
            else:
                # ── ダウンロード ──
                def _download():
                    import os as _os, threading as _th
                    tmp = cp.with_name(cp.name + f".{_th.get_ident()}.part")
                    try:
                        r = fetcher.session.get(url, timeout=(10, 3600), stream=True)
                        r.raise_for_status()
                        total = int(r.headers.get('content-length', 0))
                        aborted = False
                        with open(tmp, 'wb') as f:
                            downloaded = 0
                            for chunk in r.iter_content(chunk_size=256 * 1024):
                                if getattr(self, '_closed', False):
                                    aborted = True
                                    break   # ※returnすると.partが残るのでbreakで後始末へ
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    # シグナル経由でメインスレッドに通知（スレッドセーフ）
                                    if total > 0:
                                        pct = downloaded * 100 // total
                                        if not getattr(self, '_closed', False):
                                            self._sig_progress.emit(
                                                f"⏳ ダウンロード中... {pct}% "
                                                f"({downloaded//1024//1024}/"
                                                f"{total//1024//1024} MB)")
                        if aborted or getattr(self, '_closed', False):
                            try: tmp.unlink(missing_ok=True)
                            except OSError: pass
                            return
                        _os.replace(tmp, cp)   # 完了後にアトミックに本パスへ
                        self._tmp_path = str(cp)
                        self._sig_local.emit(self._tmp_path)
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        try: tmp.unlink(missing_ok=True)
                        except OSError: pass
                        if not getattr(self, '_closed', False):
                            self._sig_url.emit(url)
                threading.Thread(target=_download, daemon=True).start()
        else:
            self._start_url(url)

        self.show()

    # ── 再生開始ヘルパー ───────────────────────────────────────────────────

    def _start_local(self, path: str):
        """ダウンロード完了後にローカルファイルを再生"""
        import os as _os
        print(f"[VID] win_start path={path} size={_os.path.getsize(path) if _os.path.exists(path) else -1}", flush=True)
        self._lbl_load.hide()
        self._video.show()
        print("[VID] win: setVideoOutput", flush=True)
        self._player.setVideoOutput(self._video)
        print("[VID] win: setSource", flush=True)
        self._player.setSource(QUrl.fromLocalFile(path))
        print("[VID] win: play", flush=True)
        self._player.play()
        print("[VID] win: done", flush=True)

    def _start_url(self, url: str):
        """URL から直接ストリーミング再生"""
        self._lbl_load.hide()
        self._video.show()
        self._player.setVideoOutput(self._video)
        self._player.setSource(QUrl(url))
        self._player.play()

    # ── コントロール ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60:02d}:{s % 60:02d}"

    def _toggle_play(self):
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        if self._player.playbackState() == _MP.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        self._btn.setText("⏸" if state == _MP.PlaybackState.PlayingState else "▶")

    def _on_pos(self, pos: int):
        if not self._seeking and self._dur > 0:
            self._seek.blockSignals(True)
            self._seek.setValue(int(pos * 10000 / self._dur))
            self._seek.blockSignals(False)
        self._lbl.setText(f"{self._fmt(pos)} / {self._fmt(self._dur)}")

    def _on_dur(self, dur: int):
        self._dur = dur
        self._lbl.setText(f"00:00 / {self._fmt(dur)}")

    def _seek_start(self):
        self._seeking = True
        self._player.pause()

    def _seek_end(self):
        if self._dur > 0:
            self._player.setPosition(int(self._seek.value() * self._dur / 10000))
        self._seeking = False
        self._player.play()

    def _on_err(self, _error, error_string: str):
        QMessageBox.warning(self, "動画エラー",
                            f"再生できません:\n{error_string}\n\n"
                            "GStreamer 等のコーデックが不足している可能性があります。")

    def _on_video_volume_changed(self, v: int):
        """音量スライダー変更 → audio反映 + 設定へ保存（再起動後も維持）"""
        if hasattr(self, '_audio'):
            self._audio.setVolume(v / 100.0)
        if self._settings is not None:
            self._settings.video_volume = int(v)
            self._settings.save()

    def closeEvent(self, event):
        self._closed = True   # BGスレッドがemit前にチェックするフラグ
        if hasattr(self, '_player'):
            try:
                # ロード中クローズでもクラッシュしない破棄順序（0xC0000005対策）
                # setVideoOutput(None)/setAudioOutput(None)はPySide6でクラッシュ
                # 報告があるため使わない。stop+ソースクリアでローダーを止め、
                # ウィンドウ(WA_DeleteOnClose)と一緒にplayerも親子関係で破棄される。
                self._player.stop()
                self._player.setSource(QUrl())
            except Exception:
                pass
        # キャッシュファイルは削除しない。テンポラリが設定されていない場合も削除不要
        super().closeEvent(event)



_VIDEO_WINDOWS: list = []   # parent=None の動画ウィンドウのGC防止参照
                            # （ローカル変数だけだと即refcount=0でC++ごと破棄され
                            #   ウィンドウが一瞬で消える）

def _show_video_window(url: str, fetcher, settings=None) -> None:
    """VideoPlayerWindowを参照保持付きで表示する。"""
    win = VideoPlayerWindow(url, fetcher, parent=None, settings=settings)
    win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    _VIDEO_WINDOWS.append(win)
    def _forget(*_a, _w=win):
        try:
            _VIDEO_WINDOWS.remove(_w)
        except ValueError:
            pass
    win.destroyed.connect(_forget)
    win.show()


def _build_error_band_js(text: str) -> str:
    """通信エラー赤帯(._errband)を最上部と最下部に注入/解除するJSを生成する。
    text が空なら解除のみ。全モード(返信/画像/引用/カタログ)の body に適用可。"""
    if not text:
        return ("(function(){try{var l=document.querySelectorAll('._errband');"
                "for(var i=0;i<l.length;i++)l[i].parentNode.removeChild(l[i]);"
                "}catch(_){}})();")
    import json as _json
    _t = (text or "").strip()[:120]
    html = ('<div class="_errband" style="background:#a00;color:#fff;padding:5px 8px;'
            'font-size:9pt;font-weight:bold;text-align:center;">'
            '⚠ 通信エラー: ' + _t + '</div>')
    _h = _json.dumps(html)
    return ("(function(){try{if(!document.body)return;"
            "var l=document.querySelectorAll('._errband');"
            "for(var i=0;i<l.length;i++)l[i].parentNode.removeChild(l[i]);"
            "var h=" + _h + ";"
            "document.body.insertAdjacentHTML('afterbegin',h);"
            "document.body.insertAdjacentHTML('beforeend',h);"
            "}catch(_){}})();")


class ThreadView(QWidget):
    open_reply_window = Signal(int, str)
    open_image_tab    = Signal(str, list, int)
    open_image_tab_bg = Signal(str, list, int)   # 中クリック：非アクティブで開く
    open_thread_url_requested = Signal(str)      # ふたばスレURLをタブで開く
    ng_added          = Signal(str)
    _thread_ready     = Signal(object)  # スレッド→UI の安全な橋渡し
    _reload_again     = Signal()        # 実行中フェッチ完了後の保留再取得（BG→UI）
    _sodane_signal    = Signal(int, int) # (no, count) そうだね更新
    status_info       = Signal(object)  # ステータスバー更新用
    _del_result       = Signal(bool, str)  # 削除結果
    thread_loaded         = Signal(int, int)   # (thread_no, new_count) 未読バッジ用
    thread_error          = Signal(str)         # エラー発生時 (error_msg)
    thread_dead           = Signal(str)         # スレ落ち確定 (url) → 自動更新から削除
    scroll_count_updated  = Signal(int)         # 末尾スクロール残回数 (0=リセット)
    auto_refresh_requested = Signal()           # 自動更新ダイアログを開く要求
    close_requested       = Signal()            # NGスレッド即閉じ要求
    unread_state_changed  = Signal(bool)        # 未読（赤帯）有無通知
    thread_recovered      = Signal()            # エラー→正常更新で復旧（タブのエラー赤解除用）
    _ng_image_apply       = Signal(str, str)    # (img_url, hide_mode) NG画像即時反映
    _ng_image_md5_ready   = Signal(str, str)    # (img_url, md5) MD5取得完了→ダイアログ表示
    img_list_updated      = Signal(list)        # 更新後の img_list → 画像タブに反映
    _bulk_save_msg        = Signal(str)         # 一括保存の進捗/完了トースト（BG→UI）

    def __init__(self, fetcher: FutabaFetcher, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._fetcher   = fetcher
        self._settings  = settings
        # 表示中スレのURL保持（先読みキャンセルのグループキー）。破棄時に必ず
        # 中断できるよう destroyed シグナルへ接続（cleanup 未呼出の破棄経路の保険）。
        self._pf_group_holder = [""]
        self.destroyed.connect(_make_prefetch_destroy_cb(fetcher, self._pf_group_holder))
        self._thread    = None
        self._last_valid_thread = None   # 最後にres_listが有効だったスレ（スレ落ち保存用）
        self._img_list: list = []
        # 画像/引用モードのポップアップ用隠しプール(_respool)を per-res でキャッシュ。
        # {res_no: (sig, html)}。モード切替・モード中の差分更新で変化のないレスの
        # render_res 再実行を避ける（切替の重さ軽減）。
        self._respool_cache: dict = {}
        self._respool_cache_no = None   # キャッシュが属するスレッドNo（同一性ガード）
        # スレッドページ(返信/画像/引用いずれか)のDOMがロード済みで利用可能か。
        # モード切替をページ再読込せずDOM入替で行えるかの判定に使う。
        self._thread_page_live = False
        self._board: BoardInfo | None = None
        self._saved_search: str = ""   # タブ切り替え時に検索テキストを保存
        self._thread_no: int = 0
        self._fetch_seq: int = 0       # 古いfetch結果を破棄するためのシーケンス番号
        self._fetch_inflight_no = None  # フルGET実行中のスレNo（重複起動スキップ用）
        self._reload_pending = False    # 実行中フェッチに重なった更新要求の保留フラグ
        self._tmp_html_path: str = ""  # 大容量HTML用の一時ファイルパス
        self._thread_ready.connect(self._show)
        self._reload_again.connect(self._on_reload_again)
        self._sodane_signal.connect(self._apply_sodane_js)
        self._del_result.connect(self._on_del_result)
        self._pending_scroll  = 0
        self._was_error       = False  # 前回表示がエラー（キャッシュ）バナー付きだったか
        self._error_banner_html = ""   # エラー(キャッシュ表示)時の赤帯バナーHTML（画像/引用モードでも使用）
        self._pending_redraw  = False  # 非表示時にAR更新が来た→アクティブ化時に再描画する
        self._ng_dirty        = False  # NG設定変更で全再描画を保留（非可視タブ用）。
                                        # 差分追記(_pending_frags)ではなく、モデルからの
                                        # 完全再生成が必要なため _pending_redraw とは別扱い。
        self._displayed_res_count = 0  # DOMに実際に反映済みのレス数（ステータスの先出し防止用）
        self._pending_frags: list = []  # 非表示中(返信モード)にARが生成した新着フラグメント。
                                        # アクティブ化時にフルリロードせずDOM追記して
                                        # 「一瞬先頭が見える」ちらつきを防ぐ
        self._pending_self_res_popups: list = []  # 非アクティブ時のそうだね/返信通知→アクティブ化時に表示
        self._scroll_bottom_after_update = False  # 投稿後: 更新完了時に最下部へ送る
        self._prev_scroll_y   = 0   # 前回のスクロール位置 (前回のレス位置に移動 用)
        self._known_res_count = 0   # 差分更新: 前回表示済みレス数
        self._manual_reload   = False  # 手動/スクロール更新か（新着なしトースト用）
        self._ng_enabled = True          # NG:使う/わない トグル
        self._del_showing = False        # 削除:見る/隠す 状態
        self._is_dead   = False     # True=404/スレ落ち確定（リロード不可）
        self._first_load_done = False  # 初回読み込み（最初の表示）が完了したか
        self._is_log    = False     # True=保存ログのオフライン表示（更新/投稿/そうだね無効）
        self._build()
        # デストラクタで一時ファイルを削除
        import weakref as _wr
        _self_ref = _wr.ref(self)
        self.destroyed.connect(lambda: _cleanup_tmp(getattr(_self_ref(), '_tmp_html_path', '')))

    def _build(self):
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)

        # ステータス + 検索バー (返信/更新/自動更新は BoardPane 側)
        tb = QToolBar(); tb.setMovable(False)
        tb.setIconSize(QSize(1, 1))
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setMaximumHeight(28)
        self._lbl_count = QLabel("0 レス")
        self._lbl_count.setFixedWidth(220)
        tb.addWidget(self._lbl_count)
        tb.addSeparator()
        # ── 表示モード: 一覧 / 画像 / 引用 ──
        self._mode_grp = QButtonGroup(self); self._mode_grp.setExclusive(True)
        for _lbl, _mode in [("返信",""), ("画像","image"), ("引用","quote")]:
            _btn = QPushButton(_lbl); _btn.setCheckable(True)
            _btn.setFixedHeight(24); _btn.setFixedWidth(44)
            _btn.setProperty("mode", _mode)
            self._mode_grp.addButton(_btn)
            tb.addWidget(_btn)
        self._mode_grp.buttons()[0].setChecked(True)  # 初期は「一覧」
        self._mode_grp.buttonClicked.connect(
            lambda b: self._set_view_mode(b.property("mode")))
        # ── そうだね順チェックボックス（モードボタンの右） ──
        self._chk_sodane = QCheckBox("そ順")
        self._chk_sodane.setToolTip(
            "そうだね数の多い順に並べる\n"
            "・返信/画像モード: 全体をそうだね降順\n"
            "・引用モード: ツリーを保ったまま各階層内でそうだね降順"
            "（No.の右にそうだね数を表示）")
        self._chk_sodane.setFixedHeight(24)
        self._chk_sodane.setChecked(getattr(self._settings, 'sort_by_sodane', False))
        self._chk_sodane.toggled.connect(self._on_sodane_toggled)
        tb.addWidget(self._chk_sodane)
        tb.addSeparator()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("抽出 (Ctrl+Shift+F)")
        self._search_edit.setFixedWidth(160)
        self._search_edit.textChanged.connect(self._do_extract)
        self._search_edit.returnPressed.connect(
            lambda: self._do_extract(self._search_edit.text()))
        tb.addWidget(self._search_edit)
        # ── 抽出先の切替: ON=右上パネルにポップアップ / OFF=スレ内絞り込み ──
        self._chk_extract_popup = QCheckBox("ポップアップ")
        self._chk_extract_popup.setToolTip(
            "ON: 右上のパネルに抽出結果をポップアップ表示\n"
            "OFF: マッチしないレスを非表示にしてスレ内で抽出")
        self._chk_extract_popup.setChecked(
            getattr(self._settings, 'extract_popup', True))
        self._chk_extract_popup.toggled.connect(self._on_extract_mode_toggled)
        tb.addWidget(self._chk_extract_popup)
        tb.addSeparator()
        # ── NG トグルボタン ──
        self._btn_ng_toggle = QPushButton("NG解除")
        self._btn_ng_toggle.setFixedHeight(24)
        self._btn_ng_toggle.setToolTip("NGフィルタの有効/無効を切り替え")
        self._btn_ng_toggle.clicked.connect(self._on_ng_toggle)
        tb.addWidget(self._btn_ng_toggle)
        # ── 削除記事トグルボタン (削除記事があるときのみ表示) ──
        self._del_btn = QPushButton("削除:見る")
        self._del_btn.setFixedHeight(24)
        self._del_btn.setToolTip("削除された記事の表示/非表示を切り替え")
        self._del_btn.clicked.connect(self._on_del_toggle)
        self._del_btn_action = tb.addWidget(self._del_btn)
        self._del_btn_action.setVisible(False)
        # 棒読みを右寄せにするスペーサー
        _spacer = QWidget(); _spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(_spacer)
        # カウントダウン表示ラベル
        self._lbl_countdown = QLabel("")
        self._lbl_countdown.setFixedHeight(24)
        self._lbl_countdown.setToolTip("次の自動更新まであと何秒/分")
        self._lbl_countdown.setStyleSheet(f"font-size:8pt; color:{_TM.ui('countdown_fg','#ffffff')}; padding:0 4px;")
        tb.addWidget(self._lbl_countdown)
        # スクロールチェックボックス
        self._chk_ar_scroll = QCheckBox("スクロール")
        self._chk_ar_scroll.setToolTip("自動更新時に新着レス位置までスクロールする")
        self._chk_ar_scroll.setFixedHeight(24)
        self._chk_ar_scroll.stateChanged.connect(self._on_scroll_chk_changed)
        tb.addWidget(self._chk_ar_scroll)
        self._chk_ar_bouyomi = QCheckBox("棒読み")
        self._chk_ar_bouyomi.setToolTip("自動更新で新着レスを棒読みちゃんで読み上げる\n（棒読みちゃん設定タブで有効化が必要）")
        self._chk_ar_bouyomi.setFixedHeight(24)
        self._chk_ar_bouyomi.stateChanged.connect(self._on_bouyomi_chk_changed)
        tb.addWidget(self._chk_ar_bouyomi)
        # ── ヒートマップ（書き込み時間分布）チェックボックス ──
        self._chk_heatmap = QCheckBox("ヒートマップ")
        self._chk_heatmap.setToolTip(
            "スレ内の書き込み回数を時間別にレス内右下へ表示する")
        self._chk_heatmap.setFixedHeight(24)
        self._chk_heatmap.setChecked(
            getattr(self._settings, 'show_post_heatmap', False))
        self._chk_heatmap.toggled.connect(self._on_heatmap_chk_changed)
        tb.addWidget(self._chk_heatmap)
        lay.addWidget(tb)

        # WebEngineView
        # off-the-record（名前なし）: ディスクキャッシュを一切作らない
        self._profile     = QWebEngineProfile(self)
        self._profile.setHttpUserAgent(UA)
        self._profile.setUrlRequestInterceptor(Interceptor())
        # file:// 経由でロードしたHTMLからhttps://画像・動画を読み込めるようにする
        self._profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self._page    = _DebugPage(self._profile, self._profile)  # page親=profile→先に削除されwarning回避
        self._channel = QWebChannel(self._page)
        self._bridge  = ThreadBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._page.setWebChannel(self._channel)
        self._view = QWebEngineView(self._page, self)
        self._view.setZoomFactor(_default_zoom())  # テキストを標準サイズに
        lay.addWidget(self._view)
        self._find_bar = _FindBar(lambda: self._page, self)
        lay.addWidget(self._find_bar)
        self.setAcceptDrops(True)  # D&Dでログファイルを開けるようにする

        # ブリッジ接続
        self._bridge.quote_no_requested.connect(
            lambda no: self.open_reply_window.emit(no, f">No.{no}\n"))
        self._bridge.quote_comment_requested.connect(self._quote_comment)
        self._bridge.quote_img_requested.connect(self._quote_img)
        self._bridge.quote_idip_requested.connect(self._quote_idip)
        self._bridge.sodane_requested.connect(self._send_sodane)
        def _img_open(url, idx):
            lst, i = self._resolve_img_list(url, idx)
            self.open_image_tab.emit(url, lst, i)
        def _img_open_bg(url, idx):
            lst, i = self._resolve_img_list(url, idx)
            self.open_image_tab_bg.emit(url, lst, i)
        self._bridge.img_open_requested.connect(_img_open)
        self._bridge.img_open_bg_requested.connect(_img_open_bg)
        self._bridge.url_open_requested.connect(_open_url)
        self._bridge.futaba_thread_open_requested.connect(self.open_thread_url_requested.emit)
        self._bridge.ng_requested.connect(self._on_ng)
        self._bridge.del_requested.connect(self._on_del)
        self._bridge.report_del_requested.connect(self._on_report_del_with_hide)
        self._bridge.delete_res_requested.connect(self._on_delete_res_with_hide)
        self._bridge.gallery_img_requested.connect(self._on_gallery_img)
        self._bridge.play_video_requested.connect(self._play_video)
        self._bridge.quote_text_requested.connect(self._quote_text_selection)
        self._bridge.ng_text_requested.connect(self._on_ng_text)
        self._bridge.extract_text_requested.connect(self._on_extract_text)
        self._bridge.extract_clear_requested.connect(self._clear_extract_field)
        self._bridge.copy_text_requested.connect(self._on_copy_text)
        self._bridge.ng_image_requested.connect(self._on_ng_image)
        self._bridge.url_open_external_requested.connect(_open_url)
        self._bridge.scroll_bottom_reached.connect(self._on_scroll_bottom)
        self._bridge.scroll_top_reached.connect(self._on_scroll_top)
        self._bridge.scroll_count_updated.connect(self._on_scroll_count)
        self._bridge.unread_state_changed.connect(self.unread_state_changed)
        self._bridge.bottom_seen.connect(self._on_bottom_seen)
        self._bridge.save_selected_images_requested.connect(self._save_selected_images)
        self._bridge.browse_save_selected_requested.connect(self._browse_save_selected)
        self._bridge.subfolder_save_requested.connect(self._subfolder_save_menu)
        self._bridge.gal_save_close_changed.connect(self._on_gal_save_close_changed)
        self._ng_image_apply.connect(self._apply_ng_image_dom)
        self._ng_image_md5_ready.connect(self._on_ng_image_md5_ready)
        self._bulk_save_msg.connect(self._on_bulk_save_msg)

        _extract_key = (getattr(self._settings, 'shortcuts', {}) or {}).get("extract_focus", "") or "Ctrl+Shift+F"
        self._sc_extract = QShortcut(QKeySequence(_extract_key), self, self._focus_search)  # 抽出フォーカス
        # スレタブごとに同じキーが登録されるため、WindowShortcut のままだと
        # タブを複数開いた時点で重複し Ambiguous でどれも発火しなくなる。
        # このスレタブ配下にフォーカスがある時だけ効くよう限定する。
        self._sc_extract.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        # スクロール位置復元用
        self._view.loadFinished.connect(self._on_load_finished_scroll)
        self._view.loadFinished.connect(lambda _: QTimer.singleShot(50, self._inject_popup_js))
        # 再描画でHTMLに焼き込まれる TOPNEED/NEED が既定(0)に戻り「先頭/末尾スクロール
        # で更新」が効かなくなる経路（そ順トグル→_rebuild_last_html、自動更新の
        # エラー/全体再描画フォールバック等が scroll_top_count を渡さない）があるため、
        # 読込完了ごとに設定値から再適用して常に有効化する。
        self._view.loadFinished.connect(lambda _: self.apply_scroll_count_setting())
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_thread_context_menu)

    def _play_video(self, url: str):
        """MP4/WebM 動画を VideoPlayerWindow（独立ウィンドウ）で再生"""
        _show_video_window(url, self._fetcher, getattr(self, '_settings', None))

    def _quote_text_selection(self, text: str):
        """テキスト選択メニューの「引用」→ 選択テキストを返信ウィンドウに引用挿入"""
        if not text.strip():
            return
        quoted = '\n'.join(('>' + line) for line in text.splitlines()) + '\n'
        self.open_reply_window.emit(0, quoted)

    def _quote_idip(self, no: int):
        """フッターのID/IPリンク → そのレスのID(なければIP)を返信ウィンドウに引用挿入"""
        th = getattr(self, '_thread', None)
        if th is None:
            return
        r = next((x for x in th.res_list if x.no == no), None)
        if r is None:
            return
        _id = getattr(r, 'id_str', '')
        _ip = getattr(r, 'ip_str', '')
        if _id:
            self.open_reply_window.emit(no, f">ID:{_id}\n")
        elif _ip:
            self.open_reply_window.emit(no, f">IP:{_ip}\n")

    def _on_ng_text(self, text: str):
        """テキスト選択メニューの「NG」→ NGワード追加ダイアログをプリセット状態で起動"""
        text = text.strip()
        if not text:
            return
        from futaba2b_dialogs import NgWordEditDialog
        entry = {
            "pattern": text, "is_regex": False, "enabled": True,
            "ng_type": "ng", "scope_body": True,
        }
        dlg = NgWordEditDialog(entry, parent=self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            result = dlg.get_result()
            if result:
                self._settings.ng_words.append(result)
                self._settings.invalidate_ng_cache()
                self._settings.save()
                self.ng_added.emit(result.get("pattern", text))

    def _on_extract_text(self, text: str):
        """テキスト選択メニューの「抽出」→ ステータスバーの抽出テキストボックスに転送"""
        text = text.strip()
        if not text:
            return
        self._search_edit.setText(text)
        self._search_edit.setFocus()

    def _clear_extract_field(self):
        """抽出ポップアップの×ボタン → 抽出フィールドをクリアする。
        setText("") で textChanged→_do_extract("")→extractPostsPopup("") が走り、
        ポップアップも閉じる。フォーカスは移さない。"""
        if hasattr(self, "_search_edit") and self._search_edit.text():
            self._search_edit.clear()

    def _on_sodane_toggled(self, on: bool):
        """そうだね順 チェック切替 → 設定保存＋現在モードを再描画。
        返信モードは _last_html を作り直して並べ替えを反映。画像モードも
        再描画で反映（引用モードはツリー構造のため並べ替え対象外）。"""
        self._settings.sort_by_sodane = bool(on)
        try:
            self._settings.save()
        except Exception:
            pass
        if not self._thread:
            return
        self._last_html_dirty = True   # 返信HTMLの再生成を強制
        mode = ""
        if hasattr(self, '_mode_grp'):
            b = self._mode_grp.checkedButton()
            mode = b.property("mode") if b else ""
        self._set_view_mode(mode)

    def _on_heatmap_chk_changed(self, on: bool):
        """ヒートマップ チェック切替 → 設定に保存して即反映"""
        self._settings.show_post_heatmap = bool(on)
        try:
            self._settings.save()
        except Exception:
            pass
        self._apply_heatmap()

    def _apply_heatmap(self):
        """書き込み時間ヒートマップのパネルをDOMへ反映（OFF/データ無しなら除去）。
        パネルは position:fixed の独立オーバーレイなので全モード共通で動く。
        (url, レス数) が変わらなければHTMLを再計算しない。"""
        import json as _json
        _remove = ("var e=document.getElementById('_heatmap_panel');"
                   "if(e)e.remove();")
        if not getattr(self._settings, 'show_post_heatmap', False) or not self._thread:
            try:
                self._view.page().runJavaScript(_remove)
            except Exception:
                pass
            return
        key = (self._thread.url, len(self._thread.res_list))
        if getattr(self, '_hm_key', None) == key:
            html = getattr(self, '_hm_html', "")
        else:
            html = _build_heatmap_panel_html(self._thread.res_list)
            self._hm_key = key
            self._hm_html = html
        if not html:
            try:
                self._view.page().runJavaScript(_remove)
            except Exception:
                pass
            return
        js = ("(function(){" + _remove +
              "if(document.body)document.body.insertAdjacentHTML('beforeend',"
              + _json.dumps(html) + ");})();")
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def _on_bottom_seen(self):
        """JS: スレ末尾まで表示した → 既読数を現在のレス数に同期する。
        更新（再取得）を待たずにカタログの+Nが0になり、次回表示でも赤帯が
        付かなくなる（DOM上の赤帯除去はJS側 _checkUnreadAtBottom が行う）。"""
        th = getattr(self, "_thread", None)
        if not th or not th.url or not th.res_list:
            return
        s = self._settings
        # モデル側の新着フラグも落とす（モード切替・再描画での赤帯復活防止）
        for r in th.res_list:
            if r.is_new:
                r.is_new = False
        n_thread = len(th.res_list)
        n_cat = max(0, n_thread - 1)   # カタログの+Nは返信数（OP除外）単位
        if (s.thread_read_counts.get(th.url) == n_thread
                and s.catalog_read_counts.get(th.url) == n_cat):
            return   # 変化なし（末尾滞在中の多重呼び出し対策）
        s.thread_read_counts[th.url] = n_thread
        s.catalog_read_counts[th.url] = n_cat
        try:
            s.save()
        except Exception:
            pass

    def _on_copy_text(self, text: str):
        """テキスト選択メニューの「コピー」→ クリップボードにコピー"""
        if text:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)

    def _on_ng_image(self, img_url: str):
        """画像右クリック「NG画像登録」→ URLから画像を取得してMD5を計算しダイアログ表示"""
        if not img_url:
            return
        import threading as _th
        def _fetch_md5():
            try:
                import hashlib
                data = self._fetcher.fetch_image_bytes(img_url)
                if not data:
                    raise Exception("空データ")
                md5 = hashlib.md5(data).hexdigest()
            except Exception as ex:
                print(f"[NG画像] 取得失敗: {ex}")
                return
            self._ng_image_md5_ready.emit(img_url, md5)
        _th.Thread(target=_fetch_md5, daemon=True).start()

    def _on_ng_image_md5_ready(self, img_url: str, md5: str):
        """メインスレッド: MD5取得完了後にNG画像追加ダイアログを表示"""
        import re as _re
        from futaba2b_dialogs import NgImageEditDialog
        fname = _re.search(r'/([^/?#]+)(?:[?#]|$)', img_url)
        desc = fname.group(1) if fname else img_url
        # 表示中スレのモデルからファイルサイズを取得（-(N B)由来）。
        # MD5照合前のサイズ事前フィルタ（NgFilter._check_image）に使う。
        _sz = 0
        if self._thread:
            for _r in self._thread.res_list:
                if _r.image_url == img_url:
                    _sz = getattr(_r, 'file_size_bytes', 0) or 0
                    break
        preset = {
            "enabled": True, "method": "md5", "md5": md5,
            "description": desc, "last_hit": "", "expires": "無制限",
            "expires_at": "", "is_reverse_ng": False,
            "image_type": "ANY", "width": 0, "height": 0,
            "size_min": 0, "size_max": 0, "file_path": "",
            "hide_mode": "image",
            "known_urls": [img_url],
            "size": _sz,
        }
        # 既登録チェック（同MD5）
        for img in self._settings.ng_images:
            if img.get("md5") == md5:
                known = img.setdefault("known_urls", [])
                if img_url not in known:
                    known.append(img_url)
                self._settings.save()
                hide_mode = img.get("hide_mode", "image")
                self._ng_image_apply.emit(img_url, hide_mode)
                return
        dlg = NgImageEditDialog(preset, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new_entry = dlg.get_result()
        if not new_entry:
            return
        # known_urls・size（サイズ事前フィルタ用）を引き継ぐ
        new_entry.setdefault("known_urls", [img_url])
        if preset.get("size"):
            new_entry.setdefault("size", preset["size"])
        self._settings.ng_images.append(new_entry)
        self._settings.invalidate_ng_cache()
        self._settings.save()
        hide_mode = new_entry.get("hide_mode", "image")
        self._ng_image_apply.emit(img_url, hide_mode)

    def _apply_ng_image_dom(self, img_url: str, hide_mode: str):
        """メインスレッド: NG画像登録後にDOMへ即時反映"""
        ng_class = "ng-hidden" if hide_mode == "res" else "ng-image"
        escaped = img_url.replace("\\", "\\\\").replace("'", "\\'")
        self._view.page().runJavaScript(
            f"(function(){{"
            f"  var u='{escaped}';"
            f"  document.querySelectorAll('.res').forEach(function(r){{"
            f"    if(r.classList.contains('ng-hidden')||r.classList.contains('ng-image')) return;"
            f"    var imgs=r.querySelectorAll('img');"
            f"    for(var i=0;i<imgs.length;i++){{"
            f"      var s=imgs[i].src||'';"
            f"      var d=imgs[i].getAttribute('data-full')||'';"
            f"      if(s===u||d===u||s.indexOf(u)>=0||d.indexOf(u)>=0){{"
            f"        r.classList.add('{ng_class}'); break;"
            f"      }}"
            f"    }}"
            f"  }});"
            f"}})();"
        )

    def refresh_status_info(self):
        """タブがアクティブになった時など、現在のスレ状態から
        ステータスバー（nレス数等）を再計算して再送する。"""
        th = getattr(self, "_thread", None)
        if th is not None:
            try:
                self._emit_status_info(th, 0)
            except Exception:
                pass

    def _emit_status_info(self, thread, new_count: int = 0, log: str = ""):
        """ステータスバー用情報を計算して status_info シグナルで送出"""
        res_list  = thread.res_list
        res_total = len(res_list)
        # 非表示タブの自動更新は res_list を先行して増やすが、DOMへの新着追記は
        # アクティブ化時まで保留される（_pending_redraw）。その間ステータスだけ
        # 新着込みの数になり実表示より多く見えるため、DOM反映済みレス数
        # (_displayed_res_count) を上限に丸める。追記完了後に _sync_after_redraw が
        # _displayed_res_count を更新し、正しい新着込みの数へ再表示する。
        _disp = getattr(self, '_displayed_res_count', 0)
        if _disp > 0 and getattr(self, '_pending_redraw', False):
            res_total = min(res_total, _disp)
        # ステータスバー表示は OP(0レス目)を除いた実レス数 (-1)
        res_count = max(0, res_total - 1)
        img_count = sum(1 for r in res_list if r.image_url)

        # 勢い = レス数 / (最新レス番号 - スレ番号) * 1000
        momentum_str = ""
        if res_total > 1:
            latest_no = max(r.no for r in res_list)
            age = latest_no - thread.no
            if age > 0:
                momentum = round(res_total / age * 1000, 1)
                momentum_str = f"勢い {momentum}"

        board = thread.board
        viewers_str  = f"{board.viewers}人くらい" if getattr(board, 'viewers', 0) > 0 else ""
        ms = getattr(board, 'max_saved', 0)
        # 板別 global_max_no を取得（板をまたいだ汚染を防止）
        o  = self._settings.global_max_no_by_board.get(board.base_url, 0)
        if ms > 0 and o > 0:
            n = thread.no + ms - o
            saved_str = f"保存 {n}/{ms}件"
        elif ms > 0:
            saved_str = f"保存上限 {ms}件"
        else:
            saved_str = ""
        expiry_str   = thread.expiry or ""
        if new_count > 0:
            res_str = f"{res_count}レス(+{new_count}) / {img_count}画像"
        else:
            res_str = f"{res_count}レス / {img_count}画像"

        self.status_info.emit({
            'viewers':  viewers_str,
            'expiry':   expiry_str,
            'saved':    saved_str,
            'momentum': momentum_str,
            'rescount': res_str,
            'log':      log,
            'view':     self,   # sender() の代替: ビュー自身を渡す
            # タイトルバー用追加情報
            'title':     thread.title or f"No.{thread.no}",
            'new_count': new_count,
            'board':     thread.board,
            'die_time':  thread.die_time or thread.expiry or "",
        })

    def load_thread(self, board: BoardInfo, thread_no: int, open_mode: str = ""):
        # 同一スレのフルGETが実行中なら重複起動をスキップする。
        # スクロール更新・既開スレ再オープン・投稿後更新・手動更新などが
        # 近接して発火すると同じスレを並行フルGETして帯域/CPUを浪費するため、
        # 実行中の取得が最新状態を返すのに任せる（open_mode指定の明示オープンは通す）。
        if not open_mode and self._fetch_inflight_no == thread_no:
            # 実行中のフルGETに更新要求が重なった → 黙って捨てず、完了後に
            # 1回だけ再取得を予約する。これにより更新ボタン/スクロール更新が
            # 実行中フェッチと衝突して空振りするのを防ぐ。
            self._reload_pending = True
            return
        # 別スレッドを開く場合は差分更新カウントをリセット
        if thread_no != self._thread_no:
            self._known_res_count = 0
            self._reload_pending = False   # 別スレへ移動 → 保留再取得は破棄
        self._board = board; self._thread_no = thread_no
        self._pending_open_mode = open_mode  # ロード完了後に適用するモード
        self._lbl_count.setText("読み込み中…")
        self._fetch_seq += 1
        seq = self._fetch_seq
        self._fetch_inflight_no = thread_no
        _FETCH_POOL.submit(self._fetch, board, thread_no, seq)

    def load_log_thread(self, board, thread_no: int, html: str,
                        media_base_url: str = "", thread_url: str = "",
                        media_map: dict = None):
        """保存ログ(オフラインスナップショット)を通常スレと同じ描画経路で表示する。
        ・保存htm(ふたば構造)を _parse_thread で ThreadData に復元
        ・画像/サムネURLをローカル(file://)またはdata:に張り替え
        ・「サムネ保存しない」保存分(img srcが /thumb/ を含まない)は補完収集
        ・更新/自動更新/投稿/そうだね送信は _is_log により無効化"""
        self._is_log = True
        self._board = board
        self._thread_no = thread_no
        url = thread_url or (board.base_url + f"res/{thread_no}.htm")
        try:
            thread = self._fetcher._parse_thread(html, board, thread_no, url)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._lbl_count.setText(f"ログ解析エラー: {e}")
            return
        thread.url = url
        self._remap_log_media(thread, html, board.base_url, media_base_url, media_map)
        self._fill_log_media_meta(thread)
        # 全レス既読扱い（新着赤帯を出さない）
        for r in thread.res_list:
            r.is_new = False
        # 初回は必ずフル描画させる（>0だと差分追記パスに入り未ロードページで白画面になる）
        self._known_res_count = 0
        self._thread = thread
        if thread.res_list:
            self._last_valid_thread = thread
        self._show(thread)

    def _remap_log_media(self, thread, html: str, real_base: str, media_base_url: str,
                         media_map: dict = None):
        """ログの画像URLをローカル参照に張り替える。
        ・パーサが urljoin(real_base, ローカル相対) で生成した URL を media_base_url 起点に置換
        ・media_map(元絶対URL→data:) があれば正確一致で張り替える（MHT用）
        ・「サムネ保存しない」保存分(パーサが /thumb/ 不一致で取りこぼした画像)を htm から補完"""
        # 1) パーサ取得済みURLの張り替え（html/zip: real_base → file://, mht: data:はそのまま）
        if media_base_url:
            for r in thread.res_list:
                if r.image_url and r.image_url.startswith(real_base):
                    r.image_url = media_base_url + r.image_url[len(real_base):]
                if r.thumb_url and r.thumb_url.startswith(real_base):
                    r.thumb_url = media_base_url + r.thumb_url[len(real_base):]
        # 1.5) MHT等: 元(絶対)URL → data: の正確マッピングで張り替える。
        #      パースは元URL(/thumb/・-(N B)付き)のままなので拡張子/サイズが取得でき、
        #      表示・画像ウインドウ再生はここで data: 化したログ内蔵ソースを使う。
        if media_map:
            for r in thread.res_list:
                if r.image_url and r.image_url in media_map:
                    r.image_url = media_map[r.image_url]
                if r.thumb_url and r.thumb_url in media_map:
                    r.thumb_url = media_map[r.thumb_url]
        # 2) 画像取りこぼし補完（no_thumb保存: <a href=保存src><img src=保存src>）
        missing = [r for r in thread.res_list if not r.image_url]
        if not missing:
            return
        try:
            from bs4 import BeautifulSoup
            import re as _re
            from urllib.parse import urljoin
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return
        _media = _re.compile(r'\.(jpg|jpeg|png|gif|webp|bmp|mp4|webm|mov|avi|mkv)(\?|$)', _re.IGNORECASE)
        _base_for_join = media_base_url or real_base
        by_no = {}
        # OP(div.thre) と 各レス(td.rtd) を走査して No→(img) を収集
        nodes = []
        op = soup.find(class_="thre")
        if op: nodes.append(op)
        for tbl in soup.find_all("table", attrs={"border": "0"}):
            rtd = tbl.find("td", class_="rtd")
            if rtd: nodes.append(rtd)
        for node in nodes:
            cno = node.find("span", class_="cno")
            if not cno:
                continue
            m = _re.search(r'No\.(\d+)', cno.get_text())
            if not m:
                continue
            rno = int(m.group(1))
            a = None
            for cand in node.find_all("a", href=True):
                # OP(div.thre)はスレ全体を含むため、返信(td.rtd)内の画像を拾うと
                # OP画像削除スレでOPに別レス画像が混入する。OPノードのときは除外。
                if node is op and cand.find_parent("td", class_="rtd"):
                    continue
                if cand.find("img") and _media.search(cand["href"].split("?")[0]):
                    a = cand; break
            if not a:
                continue
            img = a.find("img")
            iu = urljoin(_base_for_join, a["href"])
            tu = urljoin(_base_for_join, img.get("src", "")) if img.get("src") else iu
            tw = int(img.get("width", 0) or 0); th = int(img.get("height", 0) or 0)
            by_no[rno] = (iu, tu, tw, th)
        for r in missing:
            info = by_no.get(r.no)
            if not info:
                continue
            r.image_url, r.thumb_url, _w, _h = info
            if media_map:
                if r.image_url in media_map: r.image_url = media_map[r.image_url]
                if r.thumb_url in media_map: r.thumb_url = media_map[r.thumb_url]
            if _w and not r.thumb_w: r.thumb_w = _w
            if _h and not r.thumb_h: r.thumb_h = _h

    def _fill_log_media_meta(self, thread):
        """ログで欠けがちな image_name / file_size_bytes を補完する。
        保存ログはメディアhrefがローカル化されて /src/ を含まず、パーサの
        ファイル名・サイズ抽出（-(N B)リンク検出）が効かない。また no_thumb 保存の
        補完(_remap_log_media 2)はURLのみ埋める。このままだと画像モードの
        拡張子/サイズ表示が「? / ?」になるため、URL・ローカルファイルから補完する。"""
        import os as _os
        from urllib.parse import urlparse as _up, unquote as _uq
        for r in thread.res_list:
            u = r.image_url or ""
            if not u:
                continue
            # 拡張子表示用のファイル名: URL末尾から補完（data: はMIMEのみで名前が無い）
            if not r.image_name and not u.startswith("data:"):
                r.image_name = _uq(u.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
            if r.file_size_bytes > 0:
                continue
            if u.startswith("file://"):
                p = _uq(_up(u).path)
                if len(p) >= 3 and p[0] == "/" and p[2] == ":":
                    p = p[1:]   # Windows: /C:/... → C:/...
                try:
                    if _os.path.exists(p):
                        r.file_size_bytes = _os.path.getsize(p)
                except OSError:
                    pass
            elif u.startswith("data:"):
                # base64長からバイト数を概算（=3/4、パディング分を減算）
                b64 = u.split(",", 1)[1] if "," in u else ""
                if b64:
                    r.file_size_bytes = max(0, len(b64) * 3 // 4 - b64[-2:].count("="))

    def _notify_title_updated(self):
        """スレ読み込み完了後、BoardPaneのスレタイラベルを更新する"""
        p = self.parent()
        while p:
            if isinstance(p, BoardPane):
                if p.currentWidget() is self:
                    p._update_title_lbl(self)
                break
            p = p.parent()

    def _on_ng_toggle(self):
        """NG:使う/わない トグル → 全体再描画"""
        self._ng_enabled = not self._ng_enabled
        self._btn_ng_toggle.setText("NG解除" if self._ng_enabled else "NG使う")
        if self._thread:
            self._known_res_count = 0
            self._del_showing = False
            self._show_impl(self._thread)

    def _on_del_toggle(self):
        """削除:見る/隠す トグル"""
        self._del_showing = not self._del_showing
        self._view.page().runJavaScript("toggleDeleted()")
        deleted_count = sum(1 for r in self._thread.res_list[1:] if r.is_deleted) if self._thread else 0
        lbl = "削除:隠す" if self._del_showing else "削除:見る"
        self._del_btn.setText(f"{lbl}({deleted_count}件)")

    def _mode_marker_sets(self):
        """画像/引用モードの目印用に (hidden_nos, del_nos, is_ng_fn, reveal) を返す。
        hidden_nos: 手動NG/delして非表示にしたレスNo（NG解除中は空）
        del_nos:    delしたレスNo（No.右に「del済」表示。NG解除と無関係に常時）
        is_ng_fn:   NGワード/NG画像にマッチするレスか（緑帯対象）を返す関数。
                    緑帯はNG解除時にも出したいので NG判定は常に行う。
        reveal:     NG解除（表示）状態か。Trueなら NGレスを隠さず緑帯付きで表示する。"""
        turl = (self._thread.url if self._thread else "") or ""
        reveal = not self._ng_enabled
        # 手動NG登録は緑帯/非表示判定に常に必要なので常時フル。表示切替は reveal 側で。
        hidden = set(self._settings.ng_hidden_res_nos.get(turl, []))
        delnos = set(self._settings.del_res_nos.get(turl, []))
        ng = self._settings.ng_filter   # 緑帯判定は常時（解除中も帯を出すため）
        s = self._settings
        _hide_name  = getattr(s, "ng_thread_hide_name",  True)
        _hide_image = getattr(s, "ng_thread_hide_image", True)
        def _is_ng(res):
            if ng is None or getattr(res, "is_op", False) or getattr(res, "is_deleted", False):
                return False
            try:
                if _hide_name and ng.is_ng(res):
                    return True
                if _hide_image and res.image_url and ng.is_ng_image(res):
                    return True
            except Exception:
                pass
            return False
        return hidden, delnos, _is_ng, reveal

    def _sync_del_btn_after_full_render(self):
        """画像/引用モードを全描画した直後の状態同期。全描画ではbodyが作り直され
        show-deletedクラスが消える（=削除レス非表示）ので、_del_showingとボタン表示も
        「見る」に揃える（_on_del_toggle のトグルが反転しないようにする）。"""
        self._del_showing = False
        try:
            _dc = sum(1 for r in self._thread.res_list[1:] if r.is_deleted) if self._thread else 0
            if hasattr(self, '_del_btn'):
                self._del_btn.setText(f"削除:見る({_dc}件)")
        except Exception:
            pass

    def _on_bouyomi_chk_changed(self, state):
        """棒読みチェックボックス変更 → ARエントリに即反映"""
        checked = bool(state)
        if self._thread and self._thread.url:
            p = self.parent()
            while p and not hasattr(p, "_main"):
                p = p.parent()
            main = getattr(p, "_main", None) if p else None
            if main and hasattr(main, "_ar_mgr"):
                entry = main._ar_mgr.find_entry_by_url(self._thread.url)
                if entry:
                    entry.bouyomi = checked

    def _on_scroll_chk_changed(self, state):
        """スクロールチェックボックス変更 → ARエントリに即反映"""
        checked = bool(state)
        if self._thread and self._thread.url:
            p = self.parent()
            while p and not hasattr(p, "_main"):
                p = p.parent()
            main = getattr(p, "_main", None) if p else None
            if main and hasattr(main, "_ar_mgr"):
                entry = main._ar_mgr.find_entry_by_url(self._thread.url)
                if entry:
                    entry.scroll_to_new = checked

    def update_countdown(self, remaining_sec: int):
        """カウントダウンラベルを更新する（ARマネージャから毎秒呼ばれる）"""
        if remaining_sec < 0:
            self._lbl_countdown.setText("")
            return
        if remaining_sec >= 60:
            m, s = divmod(remaining_sec, 60)
            self._lbl_countdown.setText(f"更新まで {m}:{s:02d}")
        else:
            self._lbl_countdown.setText(f"更新まで {remaining_sec}s")

    def _on_reload_again(self):
        """実行中フェッチに重なって保留された更新要求を、完了後にメインスレッドで実行。"""
        if self._is_log or self._board is None or not self._thread_no:
            return
        self.reload_thread()

    def reload_thread(self):
        # 保存ログのオフライン表示はネット更新しない
        if self._is_log:
            return
        # 404/スレ落ち確定 → リロード不可（ただし1000レス到達スレは再表示可）
        if self._is_dead:
            _is_full = bool(self._thread and getattr(self._thread, 'is_full', False)) or \
                       bool(self._last_valid_thread and getattr(self._last_valid_thread, 'is_full', False))
            if not _is_full:
                return
        if self._board and self._thread_no:
            # 差分更新が使える場合（同スレッド・既表示済み）はスクロール位置を保存不要
            # → _pending_scroll を設定しない（差分更新はDOMを書き換えるだけなのでスクロールが動かない）
            # 差分更新不可の場合のみ現在位置を保存して全体再描画後に復元する
            if self._known_res_count > 0 and self._thread is not None:
                # 差分更新候補: scrollY を prev_scroll_y だけ記録してpendingは立てない
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._reload_with_scroll_diff(y or 0)
                )
            else:
                # 初回ロードや全体再描画が確定している場合
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._reload_with_scroll(y or 0)
                )

    def refetch_thread(self):
        """再取得（開き直し）: サーバから全再取得し、差分ではなくDOMを全再描画する。
        現在の表示モード・スクロール位置・既読(+N)は維持する。過去に開いたスレの
        表示が古い/崩れた時に、タブを開き直さずクリーンな状態へ戻すため使う。
        （更新との違いは描画のみ: 更新は新着をDOMに追記、再取得はDOMを作り直す）"""
        if self._is_log:
            return
        if self._is_dead:
            _is_full = bool(self._thread and getattr(self._thread, 'is_full', False)) or \
                       bool(self._last_valid_thread and getattr(self._last_valid_thread, 'is_full', False))
            if not _is_full:
                return
        if not (self._board and self._thread_no):
            return
        checked  = self._mode_grp.checkedButton()
        cur_mode = checked.property("mode") if checked else ""
        self._view.page().runJavaScript(
            "window.scrollY",
            lambda y, _m=cur_mode: self._refetch_apply(int(y) if y else 0, _m))

    def _refetch_apply(self, scroll_y: int, cur_mode: str):
        self._prev_scroll_y   = scroll_y
        self._known_res_count = 0      # 差分追記でなくDOM全再描画を強制
        self._manual_reload   = True   # 手動更新扱い（実質「更新」の全再描画版）
        if not cur_mode:               # 通常モードは全再描画後に位置復元が必要
            self._pending_scroll = scroll_y
        # 画像/引用モードは open_mode 指定でそのモードのまま全再描画（スクロールは
        # モード描画側がライブページの現在位置を読んで保持する）。
        self.load_thread(self._board, self._thread_no, open_mode=(cur_mode or ""))

    def redraw_with_ng(self):
        """NGフィルタ変更後にキャッシュ済みスレッドを再描画する（サーバー再取得なし）"""
        if self._thread is None:
            return
        self._view.page().runJavaScript(
            "window.scrollY",
            lambda y: self._redraw_ng_impl(int(y) if y else 0)
        )

    def _consume_ng_dirty(self) -> bool:
        """NG再描画保留フラグを消化する。アクティブ化時に呼ぶ。
        - フラグ立ちなし → False
        - フラグ立ちあり → フラグ解除・非可視中に溜まった追記フラグメントを破棄
          （全再描画で最新モデルから作り直すため二重反映不要）・redraw_with_ng を
          発火して True を返す。
        _thread が未ロード時は再描画不要。フラグだけクリアする。"""
        if not getattr(self, '_ng_dirty', False):
            return False
        self._ng_dirty = False
        # NG起因の全再描画は _show_impl で最新モデルから作り直す。
        # 非可視中の差分フラグメント/フル再描画保留は全て飲み込まれるため破棄。
        self._pending_frags = []
        self._pending_redraw = False
        if self._thread is None:
            return False
        self.redraw_with_ng()
        return True

    def _redraw_ng_impl(self, scroll_y: int):
        # window.scrollY 取得は非同期。待機中にタブが閉じられると self._thread が
        # None 化しており（破棄処理でクリア）、そのまま描画すると _show_impl 内の
        # thread.url で AttributeError→後続コールバックやQTimerまで例外状態が
        # 波及する（SystemError/OverflowError の誤報を誘発）。発火時に再チェック。
        if self._thread is None:
            return
        self._known_res_count = 0   # 全体再描画を強制
        self._pending_scroll = scroll_y
        self._show_impl(self._thread)

    def _reload_with_scroll_diff(self, scroll_y: float):
        """差分更新候補の場合: prev_scroll_y のみ記録、pending は立てない"""
        self._prev_scroll_y = int(scroll_y)
        # _pending_scroll は設定しない（差分更新ならスクロール不要）
        self.load_thread(self._board, self._thread_no)

    def _reload_with_scroll(self, scroll_y: float):
        self._prev_scroll_y  = int(scroll_y)  # 前回位置として記録
        self._pending_scroll = int(scroll_y)
        self.load_thread(self._board, self._thread_no)

    def _show_no_new_toast(self):
        """手動更新で新着レスが無かった時、ページ下部中央にトースト通知を出す。
        ページ内JS（showDelMsg等）に依存しない自己完結スニペットを注入する。"""
        js = (
            "(function(){"
            "var el=document.getElementById('_nonewmsg');"
            "if(!el){el=document.createElement('div');el.id='_nonewmsg';"
            "el.style.cssText='position:fixed;bottom:30px;left:50%;"
            "transform:translateX(-50%);background:rgba(255,192,203,0.7);color:#000;"
            "border:1px solid rgba(0,0,0,0.6);"
            "padding:7px 18px;border-radius:5px;z-index:99999;font-size:10pt;"
            "pointer-events:none;opacity:0;transition:opacity 0.2s;';"
            "document.body.appendChild(el);}"
            "el.textContent='新着レスはありません';"
            "if(el._t)clearTimeout(el._t);"
            "el.style.opacity='1';"
            "el._t=setTimeout(function(){el.style.opacity='0';},1800);"
            "})();"
        )
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def _fetch(self, board, no, my_seq: int):
        import time as _t
        _t0 = _t.time()
        try:
            thread = self._fetcher.fetch_thread(board, no)
        except Exception:
            thread = None
        # sdを差分APIから別途取得してスレオブジェクトに付加
        if thread and thread.res_list:
            try:
                _start = thread.res_list[-1].no + 1
                _diff = self._fetcher.fetch_thread_diff(board, no, _start)
                _sd_map = {int(k): int(v) for k, v in _diff.get("sd", {}).items()
                              if str(v).lstrip("-").isdigit()}
                thread._sd_update = _sd_map
                # diff APIのsd値でHTMLパース値を上書き
                for r in thread.res_list:
                    if r.no in _sd_map:
                        r.sodane = _sd_map[r.no]
            except Exception as e:
                import traceback as _tb
                print(f"[SD] diff error: {e}\n{_tb.format_exc()}")
                thread._sd_update = {}
        _t1 = _t.time()
        # 自分より後のfetchが来ていれば結果を破棄（白画面防止）
        if my_seq != self._fetch_seq:
            return
        # 取得完了かつ自分が最新 → in-flight 解除（同一スレの次回更新を許可）
        self._fetch_inflight_no = None
        # 実行中に重なった更新要求があれば、完了後に1回だけ再取得する。
        # （フラグはここで消費し、再取得はメインスレッドで安全に行う）
        if self._reload_pending:
            self._reload_pending = False
            self._reload_again.emit()
        if thread:
            res_n = len(thread.res_list)
            # 削除で本文が消えた新レスに、旧スレッドが持つ削除前の本文を引き継ぐ
            _carry_over_deleted_content(self._thread, thread)
            self._thread = thread
            # is_new の基準: 初回表示（_known_res_count==0）は thread_read_counts を参照
            # 再表示時は _known_res_count（前回表示済みレス数）を基準にする
            # → 手動更新しても「前回見た以降のレス」の赤帯が維持される
            _base = self._known_res_count if self._known_res_count > 0                     else self._settings.thread_read_counts.get(thread.url, 0)
            for i, r in enumerate(thread.res_list):
                r.is_new = (i >= _base)
            if thread.res_list:
                self._last_valid_thread = thread   # スレ落ち保存用に有効なスレを保持
                self._settings.thread_read_counts[thread.url] = len(thread.res_list)
                # スレを開いたタイミングで catalog_read_counts もリセット
                # → カタログに戻ったとき +N が 0 になる（次の更新から再カウント開始）
                # カタログの res_count はOPを含まない返信数なので、res_list（OP含む）
                # から1引いて単位を揃える（揃えないと+Nが1レス分遅れて表示される）
                self._settings.catalog_read_counts[thread.url] = max(0, len(thread.res_list) - 1)
                self._settings.save()
            self._thread_ready.emit(thread)
        else:
            pass

    def _show(self, thread):
        # キャッシュなしのエラー → まず _last_valid_thread で再表示を試みる
        if thread.error and not thread.res_list:
            err = thread.error or ""
            # スレ消滅(404)のみ死亡扱い＝自動保存対象。503等の一時エラーは死亡にしない。
            if (err.split() or [''])[0] == "404":
                self._is_dead = True
                self.thread_dead.emit(thread.url or "")
            # _last_valid_thread があればキャッシュ表示（バナー付き）
            if self._last_valid_thread and self._last_valid_thread.res_list:
                _cached = self._last_valid_thread
                _cached.error     = thread.error  # エラー情報を引き継ぐ
                _cached.is_cached = True
                self._show_impl(_cached)
            else:
                self._show_error(thread)
            return
        try:
            self._show_impl(thread)
        except Exception as e:
            import traceback
            traceback.print_exc()
            thread.error = f"表示エラー: {e}"
            self._show_error(thread)

    _PREFETCH_VID_EXT = ('.mp4', '.webm', '.mov', '.avi', '.mkv')

    def _maybe_prefetch_images(self, thread):
        """表示中スレの本画像を先読みキャッシュへ投入（設定ON時）。
        スレ落ち自動保存で未閲覧画像が404欠落するのを防ぐ。動画は対象外。
        投入はページ描画が落ち着いてから（3秒遅延）行う。表示と同時に大量DLを
        開始すると、スレHTML取得・サムネ読み込みと同一サーバの帯域/接続を
        奪い合い、画像が多いスレの表示が遅くなる（特に閉じて開き直した時、
        前回中断分の先読みが表示より先に再開されて競合していた）。"""
        if not getattr(self._settings, 'prefetch_open_thread_images', True):
            return
        if self._is_log or not thread or not thread.res_list:
            return
        _tno = thread.no
        def _submit(_v=self, _t=thread, _no=_tno):
            # 遅延中にタブが閉じられた／別スレへ切り替わった場合は投入しない
            if _sb_valid is not None and not _sb_valid(_v):
                return
            if _v._thread is None or _v._thread.no != _no:
                return
            urls = []
            for r in _t.res_list:
                u = r.image_url
                if not u:
                    continue
                if u.lower().rsplit('?', 1)[0].endswith(_v._PREFETCH_VID_EXT):
                    continue   # 動画は巨大なので先読みしない
                urls.append(u)
            if urls:
                _grp = _t.url or ""
                _v._pf_group_holder[0] = _grp   # destroyed時のキャンセル対象を記録
                try:
                    _v._fetcher.prefetch_images(urls, group=_grp)
                except Exception:
                    pass
        QTimer.singleShot(3000, _submit)

    def _show_impl(self, thread):
        # NG再描画・画像モード再描画等の非同期コールバック経由で、タブ破棄後に
        # thread=None で呼ばれることがある。描画対象が無ければ何もしない。
        if thread is None:
            return
        import time as _t
        _t0 = _t.time()
        self._maybe_prefetch_images(thread)
        _ucss = _load_user_css(self._settings)
        _ul   = getattr(self._settings, "uploader_links", [])
        # NG判定は常時行う（NG解除時もNGレスに緑帯を出すため）。隠す/帯のみは
        # ng_reveal で切り替える。
        _ng   = self._settings.ng_filter
        _ng_reveal = not self._ng_enabled
        # 手動NG登録レスNo。緑帯/非表示判定に常に必要なので常時フルで渡し、
        # 表示/非表示の切替は ng_reveal 側で行う（NG解除時は隠さず緑帯のみ）。
        _thread_url = thread.url or ""
        _hidden_nos = set(self._settings.ng_hidden_res_nos.get(_thread_url, []))
        # del済マーカーはNG解除状態に関わらず表示する
        _del_nos = set(self._settings.del_res_nos.get(_thread_url, []))

        # ── スレフッターHTML ──────────────────────────────────────────────
        import datetime as _dt
        _DAY_JP = ['月','火','水','木','金','土','日']
        def _fmt_dt(dt):
            return (f"{dt.year}/{dt.month:02d}/{dt.day:02d}"
                    f" ({_DAY_JP[dt.weekday()]})"
                    f" {dt.hour}:{dt.minute:02d}:{dt.second:02d}")
        def _make_thread_footer(th):
            res_count = max(0, len(th.res_list) - 1)  # OP除く
            new_count = getattr(th, '_footer_new_count', 0)
            now_str   = _fmt_dt(_dt.datetime.now())
            return (
                self._expiry_line_html(th)
                + f'<div class="page-footer">'
                f'レス: {res_count}件 ／ 受信: {new_count}件'
                f' ／ 最終更新: {now_str}'
                f' ／ 2BP {APP_VER}'
                f'</div>'
            )

        # ── NGスレッドを開いたら即閉じる ─────────────────────────────────────
        if getattr(self._settings, "ng_thread_close_ng", False):
            op = thread.res_list[0] if thread.res_list else None
            if op and _ng is not None and _ng.is_ng(op):
                self.close_requested.emit()
                return

        new_count = sum(1 for r in thread.res_list[1:] if r.is_new)  # OP(0レス目)は新着に数えない
        # 手動更新フラグを消費（取得結果がどの経路でも1回で消費し自動更新に漏らさない）
        _manual = self._manual_reload
        self._manual_reload = False

        # ── 差分更新判定 ────────────────────────────────────────────────────
        # 条件: ① ページがすでにロード済み ② 同スレッドの更新 ③ エラーなし
        #       ④ 画像/引用モードでない ⑤ 前回がエラー表示でない
        # ⑤ がないと、エラー(赤帯)→正常復旧時に差分更新(DOM追記のみ)となり
        #    HTMLに埋め込んだ赤帯バナーが消えずに残ってしまう。
        _is_error = bool(thread.error and thread.is_cached)
        _recovered_from_error = (self._was_error and not _is_error)
        checked = self._mode_grp.checkedButton()
        cur_mode = checked.property("mode") if checked else ""
        _is_same_thread = (
            self._known_res_count > 0
            and self._thread is not None
            and self._thread.no == thread.no
            and not _is_error
            and not _recovered_from_error
            and cur_mode == ""
        )
        # 画像・引用モード中の同スレッド更新（レス増加あり・なし共通）
        _is_image_quote_same = (
            self._known_res_count > 0
            and self._thread is not None
            and self._thread.no == thread.no
            and not _is_error
            and not _recovered_from_error
            and cur_mode in ("image", "quote")
        )

        if _is_same_thread:
            self._pending_scroll = 0  # 全体再描画しないのでpendingは不要
            new_len = len(thread.res_list)
            if new_len > self._known_res_count:
                # レスが増えた → 差分更新して終了
                self._show_diff(thread, _ng, _ul, _hidden_nos, new_count, _t0, _del_nos, _ng_reveal)
                return
            elif new_len == self._known_res_count:
                # レスが変わっていない or 削除で件数が同じ
                # 手動更新で新着が無ければ下中央トーストで通知
                if _manual and new_count == 0:
                    self._show_no_new_toast()
                # → 削除済みレスがあればDOMを書き換え、なければUIのみ更新
                deleted_nos = [r.no for r in thread.res_list if r.is_deleted]
                if deleted_nos and self._thread:
                    # 旧スレと比較して新たに削除されたレスのみ書き換え
                    old_deleted = {r.no for r in self._thread.res_list if r.is_deleted}
                    newly_deleted = [no for no in deleted_nos if no not in old_deleted]
                    if newly_deleted:
                        self._update_deleted_res_dom(thread, newly_deleted, _ng, _ul, _hidden_nos, _del_nos, _ng_reveal)
                        self._thread = thread
                        self._update_ui_after_show(thread, new_count, False, skip_mode_reload=True)
                        return
                # レス数変化なし・新着なし → appendNewReplies([]) で既読化＋仕切り線更新
                # （新着が無くても赤字/仮赤字状態は変化しうるためバナーも同期する）
                self._view.page().runJavaScript(
                    "appendNewReplies([]);" + self._expiry_banner_sync_js(thread))
                self._update_ui_after_show(thread, new_count, False, skip_mode_reload=True)
                return
            # レスが減った（削除発生）→ 全体再描画に落ちる（return しない）

        if _is_image_quote_same:
            # 画像・引用モード中の同スレッド更新
            # → スレッドHTMLをロードせず、現在のスクロール位置を保持しながらモード再描画
            _res_increased = len(thread.res_list) > self._known_res_count
            self._thread = thread
            self._known_res_count = len(thread.res_list)
            # _last_html / _img_list を更新しておく（返信モードに戻った時に使う）
            _sbc = getattr(self._settings, 'scroll_bottom_count', 5)
            thread._footer_new_count = new_count
            _prev_img_list_len = len(self._img_list)
            _html, self._img_list = thread_to_html(thread, user_css=_ucss, uploaders=_ul,
                                                   ng_filter=_ng, ng_settings=self._settings,
                                                   hidden_nos=_hidden_nos, del_nos=_del_nos, ng_reveal=_ng_reveal,
                                                   scroll_bottom_count=_sbc,
                                                   scroll_top_count=getattr(self._settings,'scroll_top_count',0),
                                                   footer_html=_make_thread_footer(thread),
                                                   my_nos=self._get_my_nos(thread), id_warn_count=getattr(self._settings,'id_warn_count',5),
                                                   pseudo_expiring=_is_pseudo_red_thread(thread, self._settings), sort_by_sodane=getattr(self._settings, 'sort_by_sodane', False))
            self._last_html = _html
            self._last_html_dirty = False
            self._update_ui_after_show(thread, new_count, False, skip_mode_reload=True)
            # スクロール保持しながらモード再描画（スレッドHTMLロードは行わない）
            if cur_mode == "image":
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._render_image_mode_with_scroll(int(y) if y else 0))
            else:
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._render_quote_mode_with_scroll(int(y) if y else 0))
            return

        _sbc = getattr(self._settings, 'scroll_bottom_count', 5)
        thread._footer_new_count = new_count
        self._img_list.clear()  # 全体再描画時のみクリア（差分・モード再描画パスでは保持）
        html, self._img_list = thread_to_html(thread, user_css=_ucss, uploaders=_ul,
                                              ng_filter=_ng, ng_settings=self._settings,
                                              hidden_nos=_hidden_nos, del_nos=_del_nos, ng_reveal=_ng_reveal,
                                              scroll_bottom_count=_sbc,
                                              scroll_top_count=getattr(self._settings,'scroll_top_count',0),
                                              footer_html=_make_thread_footer(thread),
                                              my_nos=self._get_my_nos(thread), id_warn_count=getattr(self._settings,'id_warn_count',5),
                                              pseudo_expiring=_is_pseudo_red_thread(thread, self._settings), sort_by_sodane=getattr(self._settings, 'sort_by_sodane', False))
        _t1 = _t.time()
        html_bytes = html.encode('utf-8')

        if _is_error:
            # network 側で error 文字列に既に「(キャッシュ表示)」が含まれる場合があり、
            # ここでも付けると二重表示になるため未付与のときだけ付ける。
            _cn = '' if 'キャッシュ表示' in (thread.error or '') else ' (キャッシュ表示)'
            banner = (f'<div style="background:#a00;color:#fff;padding:4px 8px;font-size:8pt;">'
                      f'⚠ {thread.error}{_cn}</div>')
            self._error_banner_html = banner
            html = html.replace("<body>", f"<body>{banner}", 1)
        else:
            self._error_banner_html = ""
        # 全体再描画でバナーの有無が確定する → エラー状態を記録
        # （次回更新が差分更新かどうかの判定に使う。復旧時は全体再描画を強制してバナーを消す）
        # ※ タブのエラー赤／上下赤帯の解除は正常系共通の _update_ui_after_show（後段で
        #   必ず呼ばれる）に集約した。新着なし/差分/通信エラー赤帯など全経路で戻る。
        self._was_error = _is_error
        self._last_html = html
        import time as _time
        if self._pending_scroll > 0:
            _t0 = _time.time()
            self._scroll_t0 = _t0
        else:
            self._scroll_t0 = None

        # 一時ファイル経由でロード（setContentはHTTPS URLナビゲーションで白画面になるため禁止）
        base_url = QUrl(thread.url or "https://www.2chan.net/")
        self._del_showing = False  # 全体再描画でshow-deletedクラスが消えるためリセット
        # そうだね通知用キャッシュを現在値で初期化（全体再描画時）
        _my_nos = set(self._settings.my_post_nos.get(thread.url or "", []))
        self._my_sodane_cache = {r.no: r.sodane for r in thread.res_list if r.no in _my_nos}
        self._thread = thread   # is_full/thread_dead判定前に必ず更新
        self._known_res_count = len(thread.res_list)
        # オープンモードが指定されていればloadFinished後に切り替え
        _om = getattr(self, '_pending_open_mode', '')
        # 画像/引用モード表示中にエラー(または復旧)で全体再描画になった場合は、
        # 再描画後に同モードへ復帰させてエラー赤帯を画像/引用モードでも表示する。
        if not _om and cur_mode in ('image', 'quote') and (_is_error or _recovered_from_error):
            _om = cur_mode
        if _om in ('image', 'quote'):
            # image/quoteモードは返信HTMLをロードせず直接モード用HTMLをロード（一瞬返信モードが見えるのを防止）
            self._pending_open_mode = ''
            self._update_ui_after_show(thread, new_count, _is_error)
            self._set_view_mode(_om)
        else:
            self._load_html_via_tempfile(html, base_url)
            self._update_ui_after_show(thread, new_count, _is_error)
            if _om:
                self._pending_open_mode = ''
                def _apply_open_mode(ok, _m=_om, _self=self):
                    if ok:
                        QTimer.singleShot(50, lambda: _self._set_view_mode(_m))
                    try:
                        _self._view.loadFinished.disconnect(_apply_open_mode)
                    except Exception:
                        pass
                self._view.loadFinished.connect(_apply_open_mode)
        if new_count > 0:
            _new_res = thread.res_list[-new_count:]
            self._maybe_speak_bouyomi(_new_res)
            self._notify_ng_word_match(_new_res)
            self._check_self_res_notifications(thread, _new_res)

    def _show_diff(self, thread, _ng, _ul, _hidden_nos, new_count: int, _t0: float, _del_nos=None, _ng_reveal=False):
        """差分更新: 新着レスのみDOMに追記してスクロール位置を保持する"""
        from futaba2b_html import res_fragment_html
        import json, time as _t_mod
        # _pending_scrollが残っていても差分更新ではページロードが発生しないので消費する
        self._pending_scroll = 0

        prev_count = self._known_res_count
        new_res = thread.res_list[prev_count:]   # 新着分のみ

        # id_counts を全レスで再計算（引用インジケータ用）
        id_counts: dict[str, int] = {}
        for r in thread.res_list:
            if r.id_str:
                id_counts[r.id_str] = id_counts.get(r.id_str, 0) + 1

        fragments, self._img_list = res_fragment_html(
            new_res,
            img_list_base=self._img_list,
            uploaders=_ul,
            ng_filter=_ng,
            ng_settings=self._settings,
            hidden_nos=_hidden_nos,
            id_counts=id_counts,
            has_name_field=getattr(self._board, 'has_name_field', True),
            my_nos=self._get_my_nos(thread),
            id_warn_count=getattr(self._settings,'id_warn_count',5),
            del_nos=(_del_nos if _del_nos is not None
                     else set(self._settings.del_res_nos.get(thread.url or "", []))),
            ng_reveal=_ng_reveal,
        )

        if not fragments:
            # 差分なし（NGで全消し等）→ 仕切り線・既読化だけ更新して終了
            self._view.page().runJavaScript(
                "appendNewReplies([]);" + self._expiry_banner_sync_js(thread))
            self._known_res_count = len(thread.res_list)
            self._update_ui_after_show(thread, new_count, False, skip_mode_reload=True)
            return

        # JSON配列として渡す（エスケープ込み）
        frags_json = json.dumps(fragments, ensure_ascii=False)
        js = f"appendNewReplies({frags_json});" + self._expiry_banner_sync_js(thread)
        self._view.page().runJavaScript(js)


        # _last_html は「全体HTML」として保持したいので再生成はしない
        # （画像モード切替・ログ保存などが全体再描画で使うため）
        # → フラグとして「差分更新済み」を記録し、次の全体再描画でリセット
        self._last_html_dirty = True

        self._known_res_count = len(thread.res_list)
        elapsed = _t_mod.time() - _t0

        self._thread = thread   # is_full/thread_dead判定前に必ず更新
        self._update_ui_after_show(thread, new_count, False, skip_mode_reload=True)
        self._maybe_speak_bouyomi(new_res)
        self._notify_ng_word_match(new_res)
        self._check_self_res_notifications(thread, new_res)

    def _refresh_count_label(self, thread, new_count: int):
        """モードボタン左の「N レス (+M新着)」ラベル(_lbl_count)を再計算する。
        _update_ui_after_show 以外（タブ再アクティブ化直後の _sync_after_redraw、
        自動更新の可視タブ appendNewReplies 後など）からも呼べるように分離。
        AutoRefreshManager経由の更新は _update_ui_after_show を通らないため、
        ここを呼ばないと _lbl_count が古い値のまま実表示とずれる。"""
        count_base = f"{len(thread.res_list) - 1} レス (+{new_count}新着)" if new_count else f"{len(thread.res_list) - 1} レス"
        is_expiring = thread.is_expiring
        is_pseudo = _is_pseudo_red_thread(thread, self._settings)
        if is_expiring:
            self._lbl_count.setText(
                f'{count_base} <span style="color:#cc0000;">(赤字)</span>')
        elif is_pseudo:
            self._lbl_count.setText(
                f'{count_base} <span style="color:#e07080;">(仮赤字)</span>')
        else:
            self._lbl_count.setText(count_base)

    def _update_ui_after_show(self, thread, new_count: int, _is_error: bool, skip_mode_reload: bool = False):
        """_show_impl / _show_diff 共通のUI更新処理"""
        self._refresh_count_label(thread, new_count)
        # 削除記事ボタン更新
        deleted_count = sum(1 for r in thread.res_list[1:] if r.is_deleted)
        if deleted_count > 0:
            _del_lbl = "削除:隠す" if self._del_showing else "削除:見る"
            self._del_btn.setText(f"{_del_lbl}({deleted_count}件)")
            self._del_btn_action.setVisible(True)
        else:
            self._del_btn_action.setVisible(False)
            self._del_showing = False
        self.thread_loaded.emit(thread.no, new_count)
        # 更新後のimg_listを画像タブに通知（NG対象レスの画像は除外）
        if self._img_list:
            _flst, _ = self._filter_img_list_for_tab(self._img_list, "")
            self.img_list_updated.emit(_flst)
        # そうだね数をDOMに反映（fetch時に付加された場合）
        _sd = getattr(thread, "_sd_update", {})
        if _sd:
            if skip_mode_reload:
                # 差分更新パス: DOMはすでにある → 即座に反映
                for _no, _cnt in _sd.items():
                    self._view.page().runJavaScript(f"if(typeof updateSodane==='function')updateSodane({_no},{_cnt});")
                self._check_self_res_notifications(thread, [])
            else:
                # 全体再描画パス: loadFinished 後に反映
                _sd_snap = dict(_sd)
                _th_snap = thread
                def _apply_sd_after_load(ok, _s=_sd_snap, _t=_th_snap):
                    if not ok:
                        return
                    for _no, _cnt in _s.items():
                        self._view.page().runJavaScript(f"if(typeof updateSodane==='function')updateSodane({_no},{_cnt});")
                    self._check_self_res_notifications(_t, [])
                    try:
                        self._view.loadFinished.disconnect(_apply_sd_after_load)
                    except Exception:
                        pass
                self._view.loadFinished.connect(_apply_sd_after_load)
        # スレタイラベルを更新（BoardPaneが現在このviewを表示中の場合）
        QTimer.singleShot(0, self._notify_title_updated)
        # フォアグラウンド描画が完了 → DOM反映済みレス数を最新へ同期
        self._displayed_res_count = len(thread.res_list)
        self._emit_status_info(thread, new_count, "スレッド読み込み完了")
        # 差分更新（ページ再読込なし）ではヒートマップを直接更新する。
        # 全描画時は body 未確定なので _inject_popup_js 側の再適用に任せる。
        if skip_mode_reload:
            self._apply_heatmap()
        # 「タブを開いた直後の初回読み込みで既に死んでいた（404/1000到達）」かを記録する。
        # _first_load_done がまだ False の時点で is_error/is_full なら、開いた瞬間に
        # 既に死んでいたスレ → 自動クローズ対象から除外する（_on_thread_dead で参照）。
        _dead_now = _is_error or bool(getattr(thread, 'is_full', False))
        if not self._first_load_done and _dead_now:
            self._opened_dead = True
        self._first_load_done = True
        if _is_error:
            self.thread_error.emit(thread.error)
            _err_code = thread.error.split()[0] if thread.error.split() else ''
            # スレ消滅(404)のみ死亡扱い＝自動保存対象。503等の一時エラーは死亡にしない。
            if _err_code == "404":
                self._is_dead = True
                self.thread_dead.emit(thread.url or "")
        else:
            # 正常更新に到達 → 通信エラーで付いた赤（上下赤帯＋タブ赤）を必ず解除する。
            # 新着の有無や赤化経路（キャッシュ付きエラー/通信エラー赤帯/完全エラー画面）に
            # 依存せず戻す。自動更新(_update_view)は正常時に無条件で解除しているが、
            # 手動更新(スクロール/更新ボタン→_show_impl→本メソッド)は従来
            # _recovered_from_error（前回キャッシュ付きエラー）のときしか解除しておらず、
            # 通信エラー赤帯や「新着なし」サイクルでは一度赤くなると戻らなかった。
            # 赤が無いときは _clear_error_tab 側が no-op（タブ色が c_error のときだけ解除）。
            if getattr(self, '_has_error_band', False):
                self._clear_error_band()
            self.thread_recovered.emit()
        # 1000レス到達 → thread_deadで自動保存・自動更新停止を起動
        if getattr(thread, 'is_full', False):
            QTimer.singleShot(0, lambda: self.thread_dead.emit(thread.url or ""))
        # 更新前の表示モード（画像/引用）を復元（スクロール位置も保持）
        # skip_mode_reload=True のときはロードをスキップ（レス増加なしの場合）
        if not skip_mode_reload:
            checked = self._mode_grp.checkedButton()
            cur_mode = checked.property("mode") if checked else ""
            if cur_mode == "image":
                def _reload_image():
                    # 現在のスクロール位置を記録してからリロード
                    self._view.page().runJavaScript(
                        "window.scrollY",
                        lambda y: self._render_image_mode_with_scroll(int(y) if y else 0))
                QTimer.singleShot(0, _reload_image)
            elif cur_mode == "quote":
                def _reload_quote():
                    self._view.page().runJavaScript(
                        "window.scrollY",
                        lambda y: self._render_quote_mode_with_scroll(int(y) if y else 0))
                QTimer.singleShot(0, _reload_quote)
        else:
            pass

        # 投稿後の最下部スクロール: reload完了（差分追記/全体再描画）後に確実に行う
        if getattr(self, '_scroll_bottom_after_update', False):
            self._scroll_bottom_after_update = False
            # 自分の書き込みで末尾へ飛ぶ間は新着赤帯の既読化を抑制する
            # （_checkUnreadAtBottom が window._suppressUnreadClear を参照）。
            def _scroll_to_bottom():
                self._view.page().runJavaScript(
                    "window._suppressUnreadClear=true;"
                    "window.scrollTo(0,document.body.scrollHeight);")
            # DOM追記・レイアウト確定を待ってからスクロール（複数回保険）
            QTimer.singleShot(120, _scroll_to_bottom)
            QTimer.singleShot(450, _scroll_to_bottom)
            # 投稿後スクロールのデバウンス(200ms)が済む頃に抑制を解除する。
            # 以後はユーザー自身が末尾までスクロールすれば通常どおり赤帯が消える。
            QTimer.singleShot(900, lambda: self._view.page().runJavaScript(
                "window._suppressUnreadClear=false;"))

    def _load_html_via_tempfile(self, html: str, base_url: QUrl):
        """HTMLを一時ファイル経由でロード（setContent の HTTPS ナビゲーション問題を回避）
        file:// ページから qrc:// へのアクセスが制限されるため、
        qwebchannel.js をインライン埋め込みにしてブリッジを維持する。
        """
        import tempfile, os
        from PySide6.QtCore import QFile, QIODevice

        # ページモード追跡: デフォルトは返信モード（image/quoteレンダラーがロード後に上書き）
        self._loaded_page_mode = ''
        # 新規ナビゲーション開始 → ロード完了まではDOM入替不可（loadFinishedで再びTrue）
        self._thread_page_live = False
        # フルロードするHTMLは最新モデルから生成され保留分の新着を含むため、
        # 非表示中にたまった追記用フラグメントは破棄（二重追記防止）
        self._pending_frags = []

        # 旧一時ファイルを削除
        if self._tmp_html_path:
            try:
                os.unlink(self._tmp_html_path)
            except OSError:
                pass

        # qrc:///qtwebchannel/qwebchannel.js を読み込んでインライン化
        # （file:// ページからは qrc:// へのアクセスが制限されるため）
        QRC_TAG = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>'
        if QRC_TAG in html:
            f = QFile(":/qtwebchannel/qwebchannel.js")
            if f.open(QIODevice.OpenModeFlag.ReadOnly):
                qwc_js = bytes(f.readAll()).decode('utf-8', errors='replace')
                f.close()
                html = html.replace(QRC_TAG, f'<script>\n{qwc_js}\n</script>', 1)
            else:
                print('[TEMPFILE] WARNING: qwebchannel.js not found in qrc')

        # <base href> 挿入
        base_href = base_url.toString()
        if '<head>' in html:
            html = html.replace('<head>', f'<head><base href="{base_href}">', 1)

        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.html', encoding='utf-8', delete=False)
        tmp.write(html)
        tmp.close()
        self._tmp_html_path = tmp.name
        self._view.load(QUrl.fromLocalFile(tmp.name))




    def _err_code(self, err: str) -> str:
        """エラー文字列から最初の数字コードを抽出 ('404 Not Found' → '404')"""
        import re as _re
        m = _re.match(r"(\d+)", err.strip())
        return m.group(1) if m else "ERR"

    def _show_error(self, thread):
        """エラースレッドの表示（キャッシュなし）- 上下にバナーを表示"""
        board_name = thread.board.name if thread.board else ""
        url = thread.url or ""
        err = thread.error or "取得に失敗しました"
        code = self._err_code(err)
        _ucss_e = _load_user_css(self._settings)
        _usr_e = f"<style>{_ucss_e}</style>" if _ucss_e else ""
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{THREAD_CSS}"
            ".err-banner{"
            "  background:#cc0000;color:#fff;font-size:10pt;font-weight:bold;"
            "  text-align:center;padding:10px 12px;margin:0;"
            "}"
            ".err-url{text-align:center;color:#cc1105;font-size:9pt;padding:10px 8px;word-break:break-all;}"
            ".err-msg{text-align:center;color:#888;font-size:9pt;padding:4px;}"
            f"</style>{_usr_e}</head>"
            f"<body>"
            f"<div class='err-banner'>⚠ {board_name}  {err}</div>"
            f"<div class='err-url'>{url}</div>"
            f"<div class='err-msg'>スレッドの取得に失敗しました</div>"
            f"<div class='err-banner'>⚠ {board_name}  {err}</div>"
            f"</body></html>"
        )
        self._view.setHtml(html, QUrl("about:blank"))
        self._lbl_count.setText(code)
        self.thread_error.emit(err)

    def _inject_error_band(self, text: str):
        """通信エラー赤帯を表示中ページの上下へ注入（全モード対応・帯のみ）。"""
        try:
            self._view.page().runJavaScript(_build_error_band_js(text))
            self._has_error_band = True
        except Exception:
            pass

    def _clear_error_band(self):
        try:
            self._view.page().runJavaScript(_build_error_band_js(""))
            self._has_error_band = False
        except Exception:
            pass

    def _inject_dead_banner(self):
        """スレ落ち確定時、表示中ページの最上部・最下部へ赤帯(404)を注入する。
        自動更新の diff is_dead 検知は再フェッチせず現ページを保持するため、
        再表示(_show_impl/_update_view)と同等の赤帯を JS で被せる。
        既に赤帯(._deadband)があればスキップして二重注入を防ぐ。"""
        try:
            import json as _json
            banner = ('<div class="_deadband" style="background:#a00;color:#fff;'
                      'padding:6px 8px;font-size:9pt;font-weight:bold;text-align:center;">'
                      '⚠ 404 スレッドが落ちました</div>')
            self._error_banner_html = banner   # モード再描画でも先頭に残す
            _h = _json.dumps(banner)
            js = ("(function(){try{"
                  "if(!document.body) return;"
                  "if(document.querySelector('._deadband')) return;"
                  "var h=" + _h + ";"
                  "document.body.insertAdjacentHTML('afterbegin', h);"
                  "document.body.insertAdjacentHTML('beforeend', h);"
                  "}catch(e){}})();")
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def _inject_popup_js(self):
        # QTimer.singleShot 経由で遅延実行されるため、その間にタブが閉じられて
        # ビューが破棄されていることがある（「already deleted」の未処理例外対策）。
        if _sb_valid is not None and not _sb_valid(self._view):
            return
        js = r"""(function() {
    /* ── コンテナ枠のみ inline style — 背景・文字色は内側の .res クラス CSS に委ねる ── */
    var ST = 'position:fixed;border:1px solid #800000;' +
             'max-width:520px;max-height:430px;overflow-y:auto;' +
             'z-index:9999;box-shadow:2px 2px 8px rgba(0,0,0,.45);' +
             'pointer-events:auto;border-radius:2px';
    var T = {}, P = [];
    function inP(e) { return e && !!e.closest && !!e.closest('._rp'); }
    function _inSel(e) { return e && !!e.closest && !!e.closest('#_selmenu'); }
    function rmAll() {
        while (P.length) { var x = P.pop(); if (x && x.parentNode) x.parentNode.removeChild(x); }
    }
    function schedH() { clearTimeout(T.h); T.h = setTimeout(rmAll, 200); }

    /* ポップアップをカーソルに少し被る位置に配置 */
    function posit(p, x, y) {
        var pw = p.offsetWidth || 460, ph = p.offsetHeight || 80;
        var lx = x + 4, ly = y - 10;                          /* カーソルに少し被る */
        if (lx + pw > window.innerWidth - 4)  lx = Math.max(0, x - pw + 4);
        if (ly < 0)                            ly = y + 4;
        if (ly + ph > window.innerHeight - 4)  ly = Math.max(0, window.innerHeight - ph - 4);
        p.style.left = lx + 'px'; p.style.top = ly + 'px';
    }

    function mkPop(elems) {
        var p = document.createElement('div');
        p.className = '_rp'; p.setAttribute('style', ST);
        elems.forEach(function(el, i) {
            if (i > 0) {
                var hr = document.createElement('hr');
                hr.style.cssText = 'border:none;border-top:1px solid #c8a890;margin:0;display:block';
                p.appendChild(hr);
            }
            var d = document.createElement('div');
            /* .res .reply / .res .op などクラスを継承させて THREAD_CSS を適用 */
            /* ただし deleted / ng-hidden は display:none を持つため、ポップアップ内
               では除去する（削除レス・NG非表示レスを引用したとき、ポップアップが
               空になり「何も表示されない」状態になるのを防ぐ）。削除理由・NG理由は
               innerHTML 内の .del-reason 等にあるのでそのまま表示される。 */
            d.className = el.className.replace(/\b(?:ng-hidden|deleted)\b/g, '')
                                      .replace(/\s+/g, ' ').trim();
            d.style.cssText = 'margin:0 0 0 16px!important;float:none!important;' +
                              'width:auto!important;min-width:0!important;' +
                              'max-width:100%!important;overflow:visible!important;' +
                              'display:block!important;';
            d.innerHTML = el.innerHTML;
            /* data-hooked を除去して hookC/hookQuoteInd が再適用されるようにする */
            d.querySelectorAll('[data-hooked]').forEach(function(n) { n.removeAttribute('data-hooked'); });
            p.appendChild(d);
        });
        return p;
    }

    function showNo(no, x, y, fromEl) {
        var s = document.getElementById('r' + no);
        if (s) showEl([s], x, y, fromEl);
        else showMsg('引用元はありません', x, y, fromEl);
    }
    function showNos(nos, x, y, fromEl) {
        var ss = nos.map(function(n) { return document.getElementById('r' + n); }).filter(Boolean);
        if (ss.length) showEl(ss, x, y, fromEl);
        else showMsg('引用元はありません', x, y, fromEl);
    }
    /* fromEl が属するレスのNo（引用元は必ずこれより前に投稿されたレス）。
       そ順で並びが変わっても正しく辿れるよう、DOM位置ではなくレス番号で判定する。 */
    function _selfNo(fromEl) {
        var el = (fromEl && fromEl.closest) ? fromEl.closest('[id^="r"]') : null;
        var n = el ? parseInt((el.id || '').slice(1)) : NaN;
        return isNaN(n) ? Infinity : n;   /* 特定不能時は全レスを対象 */
    }
    function showText(q, x, y, fromEl) {
        /* 自分より前(No小)のレスで q を通常テキストとして含む、最も近い1件を表示 */
        var ql = q.toLowerCase();
        var selfNo = _selfNo(fromEl);
        var best = null, bestNo = -1, seen = {};
        document.querySelectorAll('.res[id^="r"]').forEach(function(el) {
            var no = parseInt(el.id.slice(1));
            if (isNaN(no) || !(no < selfNo) || seen[no]) return;
            seen[no] = 1;
            var c = el.querySelector('.comment');
            if (!c) return;
            /* span.qt（引用行）を除いたテキストで検索 */
            var clone = c.cloneNode(true);
            clone.querySelectorAll('span.qt').forEach(function(s) { s.remove(); });
            if (clone.textContent.toLowerCase().indexOf(ql) >= 0 && no > bestNo) {
                best = el; bestNo = no;   /* 最も近い（No最大の）引用元 */
            }
        });
        if (best) showEl([best], x, y, fromEl);
        else showMsg('引用元はありません', x, y, fromEl);
    }

    function showEl(elems, x, y, fromEl) {
        /* fromEl が ._rp 内なら親ポップアップを保持して子ポップアップを追加 */
        var parentPop = (fromEl && fromEl.closest) ? fromEl.closest('._rp') : null;
        var newDepth  = parentPop ? (parseInt(parentPop.dataset.depth || '0') + 1) : 0;
        /* newDepth 以上の既存ポップアップを削除（同 depth は置き換え） */
        while (P.length > 0 && parseInt((P[P.length-1].dataset || {}).depth || '0') >= newDepth) {
            var old = P.pop();
            if (old && old.parentNode) old.parentNode.removeChild(old);
        }
        var p = mkPop(elems);
        p.dataset.depth = String(newDepth);
        hookC(p);
        hookQuoteInd(p);
        p.addEventListener('mouseover', function() { clearTimeout(T.h); });
        p.addEventListener('mouseout',  function(e) { if (!inP(e.relatedTarget) && !_inSel(e.relatedTarget)) schedH(); });
        /* ドラッグ（テキスト選択）判定: mousedown位置を記録し、click時に移動量で判別 */
        p.addEventListener('mousedown', function(e) {
            p._downX = e.clientX; p._downY = e.clientY;
        });
        /* クリックでこのポップアップ(depth以上)を閉じる。リンク・ボタンは除外。
           ただしドラッグ（移動>4px）やテキスト選択中は閉じない。 */
        p.addEventListener('click', function(e) {
            if (e.target.closest('a[href], button, span.quote-ind, [data-popup-no]')) return;
            /* ドラッグ判定: mousedown位置からの移動量が大きければ選択操作とみなし閉じない */
            if (typeof p._downX === 'number') {
                var dx = e.clientX - p._downX, dy = e.clientY - p._downY;
                if (dx*dx + dy*dy > 16) return;   /* 4px超 = ドラッグ */
            }
            /* テキスト選択中（範囲が空でない）なら閉じない */
            var sel = window.getSelection && window.getSelection();
            if (sel && !sel.isCollapsed && sel.toString().length > 0) return;
            var myDepth = parseInt((p.dataset||{}).depth||'0');
            clearTimeout(T.h);
            while (P.length > 0 && parseInt((P[P.length-1].dataset||{}).depth||'0') >= myDepth) {
                var old = P.pop();
                if (old && old.parentNode) old.parentNode.removeChild(old);
            }
            e.stopPropagation();
        });
        document.body.appendChild(p);
        posit(p, x, y);
        P.push(p);
    }

    /* 引用元が見つからない時の案内ポップアップ（引用ポップアップと同じ見た目） */
    function showMsg(msg, x, y, fromEl) {
        var d = document.createElement('div');
        d.className = 'res reply';
        d.innerHTML = '<div class="content" style="padding:6px 10px;color:#888;'
                    + 'font-style:italic;white-space:nowrap;">' + msg + '</div>';
        showEl([d], x, y, fromEl || null);
    }

    function hookNo(el, no, delay) {
        delay = (delay !== undefined) ? delay : 300;
        var k = 'n' + no;
        el.style.cursor = 'pointer';
        el.addEventListener('mouseover', function(e) {
            /* 画像モードの選択モード中は、ギャラリーセル内(連番/サムネ)の
               レス内容ポップアップを出さない（返信モードの引用は対象外）。 */
            if (window._selMode && e.currentTarget.closest &&
                e.currentTarget.closest('.gi')) return;
            clearTimeout(T.h); clearTimeout(T[k]);
            var cx = e.clientX, cy = e.clientY;
            var tgt = e.currentTarget;
            T[k] = setTimeout(function() { showNo(no, cx, cy, tgt); }, delay);
            e.stopPropagation();
        });
        el.addEventListener('mouseout', function(e) {
            clearTimeout(T[k]);
            if (!inP(e.relatedTarget)) schedH();
        });
    }

    function hookTxt(el, q) {
        var k = 'q' + q.slice(0, 20);
        el.style.cursor = 'pointer';
        el.addEventListener('mouseover', function(e) {
            clearTimeout(T.h); clearTimeout(T[k]);
            var cx = e.clientX, cy = e.clientY;
            var tgt = e.currentTarget;
            T[k] = setTimeout(function() { showText(q, cx, cy, tgt); }, 300);
            e.stopPropagation();
        });
        el.addEventListener('mouseout', function(e) {
            clearTimeout(T[k]);
            if (!inP(e.relatedTarget)) schedH();
        });
    }

    /* 画像ファイル名引用 (>123456789.jpg) — 引用元レスをポップアップ */
    function hookImgRef(el, fname) {
        el.style.cursor = 'pointer';
        el.addEventListener('mouseover', function(e) {
            clearTimeout(T.h); clearTimeout(T.qi);
            var cx = e.clientX, cy = e.clientY;
            var tgt = e.currentTarget;
            T.qi = setTimeout(function() { window.showImgRef(fname, cx, cy, tgt); }, 300);
            e.stopPropagation();
        });
        el.addEventListener('mouseout', function(e) {
            clearTimeout(T.qi);
            if (!inP(e.relatedTarget)) schedH();
        });
    }

    /* ── コンテナ内の引用リンクにフックを設定 (ポップアップ内にも再帰利用) ── */
    function hookC(c) {
        /* a[href^="#r"]:not(.no) — ヘッダーの「No.番号」リンクは除外 */
        c.querySelectorAll('a[href^="#r"]:not([data-hooked])').forEach(function(a) {
            if (a.classList.contains('no')) return;
            var m = (a.getAttribute('href') || '').match(/#r(\d+)$/);
            if (!m) return;
            a.setAttribute('data-hooked', '1');
            /* inline onmouseenter/onmouseleave を削除して上書き */
            a.removeAttribute('onmouseenter'); a.removeAttribute('onmouseleave');
            hookNo(a, parseInt(m[1]));
        });
        /* span.qt (a タグなし) — 数字引用 or テキスト引用 */
        c.querySelectorAll('span.qt:not([data-hooked])').forEach(function(sp) {
            if (sp.querySelector('a')) return;
            if (sp.dataset && sp.dataset.idRef) return;  /* >ID:xxx はIDポップアップ側で処理 */
            /* >123456789.jpg 等の画像ファイル名引用 → 引用元レスのポップアップ
               (テキスト引用に横取りされないようここで処理する) */
            if (sp.dataset && sp.dataset.imgRef) {
                sp.setAttribute('data-hooked', '1');
                hookImgRef(sp, sp.dataset.imgRef);
                return;
            }
            var t  = (sp.textContent || '').trim();
            var mn = t.match(/^>+(No\.)?(\d+)\s*$/);
            if (mn) { sp.setAttribute('data-hooked', '1'); hookNo(sp, parseInt(mn[2])); return; }
            var q = t.replace(/^>+/, '').trim();
            if (q.length >= 2) { sp.setAttribute('data-hooked', '1'); hookTxt(sp, q); }
        });
        /* [data-popup-no] — 画像タブ連番数字からのポップアップ (500ms 遅延) */
        c.querySelectorAll('[data-popup-no]:not([data-hooked])').forEach(function(el) {
            el.setAttribute('data-hooked', '1');
            hookNo(el, parseInt(el.getAttribute('data-popup-no')), 500);
        });
    }

    /* ── ポップアップ内の ▼ にリスナーを付ける ── */
    function hookQuoteInd(container) {
        container.querySelectorAll('span.quote-ind[data-quoters]:not([data-hooked])').forEach(function(btn) {
            btn.setAttribute('data-hooked', '1');
            var nos = btn.getAttribute('data-quoters').split(',')
                         .map(function(s) { return parseInt(s.trim()); });
            btn.addEventListener('mouseover', function(e) {
                clearTimeout(T.h); clearTimeout(T.qi);
                var cx = e.clientX, cy = e.clientY;
                var tgt = e.currentTarget;
                T.qi = setTimeout(function() { showNos(nos, cx, cy, tgt); }, 300);
                e.stopPropagation();
            });
            btn.addEventListener('mouseout', function(e) {
                clearTimeout(T.qi);
                if (!inP(e.relatedTarget)) schedH();
            });
        });
    }

    /* ── ページ全体にフック適用 ── */
    hookC(document);

    /* ── ▼ 被引用インジケータ (DOMContentLoaded で data-quoters 付与済み) ── */
    document.querySelectorAll('span.quote-ind[data-quoters]').forEach(function(btn) {
        var nos = btn.getAttribute('data-quoters').split(',')
                     .map(function(s) { return parseInt(s.trim()); });
        btn.addEventListener('mouseover', function(e) {
            clearTimeout(T.h); clearTimeout(T.qi);
            var cx = e.clientX, cy = e.clientY;
            var tgt = e.currentTarget;
            T.qi = setTimeout(function() { showNos(nos, cx, cy, tgt); }, 300);
            e.stopPropagation();
        });
        btn.addEventListener('mouseout', function(e) {
            clearTimeout(T.qi);
            if (!inP(e.relatedTarget)) schedH();
        });
    });

    /* ── 画像モード: ギャラリーセル(.gi)に ▼被引用インジケータを付与 ── */
    (function() {
        var cells = document.querySelectorAll('.gi[data-res-no]');
        if (!cells.length) return;                 /* 画像モード以外は何もしない */
        if (typeof _computeQuotedBy !== 'function') return;
        var quotedBy = _computeQuotedBy();          /* respool 内の .res から再計算 */
        cells.forEach(function(cell) {
            if (cell.querySelector('.gi-qi')) return;   /* 二重付与防止 */
            var no = parseInt(cell.getAttribute('data-res-no'));
            var qs = quotedBy[no];
            if (!qs || !qs.length) return;          /* 被引用なしは付けない */
            var btn = document.createElement('span');
            btn.className = 'gi-qi';
            btn.textContent = '▼';
            btn.setAttribute('data-quoters', qs.join(','));
            cell.appendChild(btn);
            var nos = qs.slice();
            function _showQ(e) {
                clearTimeout(T.h); clearTimeout(T.qi);
                showNos(nos, e.clientX, e.clientY, e.currentTarget);
            }
            btn.addEventListener('mouseover', function(e) {
                if (window._selMode) return;   /* 選択モード中は返信一覧を出さない */
                clearTimeout(T.h); clearTimeout(T.qi);
                var cx = e.clientX, cy = e.clientY, tgt = e.currentTarget;
                T.qi = setTimeout(function() { showNos(nos, cx, cy, tgt); }, 300);
                e.stopPropagation();
            });
            btn.addEventListener('mouseout', function(e) {
                clearTimeout(T.qi);
                if (!inP(e.relatedTarget)) schedH();
            });
            /* クリックは画像オープン(.gi onclick)に伝播させず、被引用ポップアップを表示 */
            btn.addEventListener('click', function(e) {
                e.stopPropagation(); e.preventDefault();
                clearTimeout(T.qi);
                if (window._selMode) return;   /* 選択モード中は返信一覧を出さない */
                _showQ(e);
            });
        });
    })();

    /* ── グローバル公開: inline onmouseenter="showPopup()" / hidePopup() 向け ── */
    window.showPopup = function(no, x, y, fromEl) { showNo(no, x, y, fromEl || null); };
    window.hidePopup = function() { schedH(); };
    /* ID引用ポップアップ: 同一IDの全レスをレスカードで表示（番号引用と同デザイン） */
    function showId(id, x, y, fromEl) {
        var seen = {}, elems = [];
        document.querySelectorAll('.post-id[data-id="' + id + '"]').forEach(function(pel) {
            var res = pel.closest('.res[id^="r"]');
            if (!res) return;
            var no = parseInt(res.id.slice(1));
            if (isNaN(no) || seen[no]) return;
            seen[no] = 1; elems.push(res);
        });
        if (elems.length) showEl(elems, x, y, fromEl);
        else showMsg('このIDのレスはありません', x, y, fromEl);
    }
    window.showIdPopup = function(id, x, y, fromEl) { showId(id, x, y, fromEl || null); };
    /* スレ画/スレあき → OP(0レス目)をレスカードで表示（番号引用と同デザイン） */
    window.showOpPopup = function(x, y, fromEl) {
        var op = document.querySelector('.res.op');
        if (op) showEl([op], x, y, fromEl || null);
        else showMsg('0レス目はありません', x, y, fromEl || null);
    };
    /* ── 抽出パネルで引用ホバーを有効にするため hookC/hookQuoteInd もグローバル公開 ── */
    window._hookPopupC        = hookC;
    window._hookPopupQuoteInd = hookQuoteInd;
    /* ── 画像ファイル名引用ポップアップ ── */
    window.showImgRef = function(fname, x, y, fromEl) {
        fname = (fname || '').toLowerCase();
        /* 自分より前(No小)のレスから画像ファイル名一致を探す。そ順で並びが
           変わっても正しく辿れるよう、DOM位置ではなくレス番号で判定する。 */
        var selfNo = _selfNo(fromEl);
        var arr = Array.from(document.querySelectorAll('.res[id^="r"]'));
        arr.sort(function(a, b) { return parseInt(a.id.slice(1)) - parseInt(b.id.slice(1)); });
        var hits = [], seen = {};
        arr.forEach(function(r) {
            var no = parseInt(r.id.slice(1));
            if (isNaN(no) || !(no < selfNo) || seen[no]) return;
            seen[no] = 1;
            var found = false;
            r.querySelectorAll('a[href], img[src]').forEach(function(a) {
                var u = (a.getAttribute('href') || a.getAttribute('src') || '').toLowerCase();
                if (u.indexOf(fname) >= 0) found = true;
            });
            if (!found) {
                r.querySelectorAll('.ul-fname').forEach(function(s) {
                    if ((s.textContent || '').toLowerCase().indexOf(fname) >= 0) found = true;
                });
            }
            if (found) hits.push(r);
        });
        if (hits.length) showEl(hits, x, y, fromEl || null);
        else showMsg('引用元はありません', x, y, fromEl || null);
    };

    /* ── テキスト選択メニュー ── */
    (function() {
        function makeSM() {
            var m = document.createElement('div');
            m.id = '_selmenu';
            m.style.cssText =
                'position:fixed;display:none;align-items:center;gap:3px;' +
                'padding:3px 8px;z-index:19999;font-size:8pt;' +
                'background:#F0E0D6;border:1px solid #800000;border-radius:3px;' +
                'box-shadow:2px 2px 6px rgba(0,0,0,.4);white-space:nowrap;';
            var defs = [
                ['引用',   function(t)    { if(typeof _b==='function') _b('quoteText',[t]); }],
                ['抽出',   function(t)    { if(typeof _b==='function') _b('extractText',[t]); }],
                ['コピー', function(t)    { if(typeof _b==='function') _b('copyText',[t]); }],
                ['NG',     function(t)    { if(typeof _b==='function') _b('ngText',[t]); }],
                ['ググる', function(t)    { if(typeof _b==='function') _b('openUrl',['https://www.google.com/search?q='+encodeURIComponent(t)]); }],
                ['翻訳',   function(t)    { if(typeof _b==='function') _b('openUrl',['https://translate.google.com/?text='+encodeURIComponent(t)+'&sl=ja&tl=en']); }],
            ];
            defs.forEach(function(d) {
                var btn = document.createElement('button');
                btn.textContent = d[0];
                btn.style.cssText =
                    'background:transparent;border:1px solid #c8a890;color:#7B0004;' +
                    'cursor:pointer;padding:2px 7px;font-size:8pt;border-radius:2px;font-family:inherit;';
                btn.addEventListener('mouseenter', function(){ this.style.background='#E0C0B0'; });
                btn.addEventListener('mouseleave', function(){ this.style.background='transparent'; });
                btn.addEventListener('mousedown', function(e) {
                    e.preventDefault(); e.stopPropagation();
                    var t = m._selTxt||'', x = m._selX||0, y = m._selY||0;
                    if (t) d[1](t, x, y);
                    m._suppress = true;   /* 直後の mouseup で再表示させない */
                    m.style.display = 'none';
                });
                m.appendChild(btn);
            });
            /* 選択メニューにカーソルがある間は引用ポップアップを閉じない
               （ドラッグ選択→メニューへ移動でポップアップが消えるのを防ぐ）。
               メニューから離れたら通常どおり閉じ判定する。 */
            m.addEventListener('mouseover', function() { clearTimeout(T.h); });
            m.addEventListener('mouseout', function(e) {
                if (!inP(e.relatedTarget) && !_inSel(e.relatedTarget)) schedH();
            });
            document.body.appendChild(m);
            return m;
        }
        /* 再注入(_inject_popup_js は更新のたびに走る)で document リスナーが
           重複登録されると、1つ目が _suppress を消費し2つ目が再表示してしまう。
           前回分を除去してから現在のクロージャで登録し、常に1個に保つ。 */
        if (window._selMenuUp)   document.removeEventListener('mouseup',   window._selMenuUp);
        if (window._selMenuDown) document.removeEventListener('mousedown', window._selMenuDown);
        window._selMenuUp = function(e) {
            var sm = document.getElementById('_selmenu');
            if (sm && sm.contains(e.target)) return;
            /* ボタン押下直後の mouseup では再表示しない。ボタンの mousedown で
               display:none にするため e.target がメニュー外となり、上の contains
               判定をすり抜けて（選択は残るため）メニューが再表示されるのを防ぐ。 */
            if (sm && sm._suppress) { sm._suppress = false; return; }
            setTimeout(function() {
                var sel = window.getSelection();
                var txt = sel ? sel.toString().trim() : '';
                if (txt.length < 2) { if (sm) sm.style.display = 'none'; return; }
                if (!sm) sm = makeSM();
                sm._selTxt = txt; sm._selX = e.clientX; sm._selY = e.clientY;
                sm.style.display = 'flex';
                var mw = sm.offsetWidth || 380;
                var lx = e.clientX - mw / 2;
                var ly = e.clientY - 42;
                if (lx < 4) lx = 4;
                if (lx + mw > window.innerWidth - 4) lx = window.innerWidth - mw - 4;
                if (ly < 4) ly = e.clientY + 4;
                sm.style.left = lx + 'px'; sm.style.top = ly + 'px';
            }, 10);
        };
        window._selMenuDown = function(e) {
            var sm = document.getElementById('_selmenu');
            if (sm && !sm.contains(e.target)) sm.style.display = 'none';
        };
        document.addEventListener('mouseup',   window._selMenuUp);
        document.addEventListener('mousedown', window._selMenuDown);
    })();

    /* ── 引用モード/画像モードのサムネイル右クリックメニュー ── */
    /* 再注入での重複登録を防ぐため前回分を除去してから登録 */
    if (window._thumbCtx) document.removeEventListener('contextmenu', window._thumbCtx);
    window._thumbCtx = function(e) {
        var img = e.target;
        if (img.tagName !== 'IMG') return;
        /* .qt-thumb (引用モード) か .gi img (画像モード) のみ対象 */
        var inQt = img.classList.contains('qt-thumb');
        var inGi = img.closest && img.closest('.gi');
        if (!inQt && !inGi) return;
        e.preventDefault(); e.stopPropagation();
        var old = document.getElementById('__img_ctx2');
        if (old) old.parentNode.removeChild(old);
        var imgUrl = img.getAttribute('data-full') || img.src || '';
        if (!imgUrl) return;
        var menu = document.createElement('div');
        menu.id = '__img_ctx2';
        menu.style.cssText = 'position:fixed;background:#fff;border:1px solid #999;'
            + 'padding:2px 0;z-index:19998;box-shadow:2px 2px 4px rgba(0,0,0,.3);font-size:9pt;';
        menu.style.left = e.clientX + 'px';
        menu.style.top  = e.clientY + 'px';
        function addItem2(label, fn) {
            var item = document.createElement('div');
            item.textContent = label;
            item.style.cssText = 'padding:4px 16px;cursor:pointer;white-space:nowrap;';
            item.onmouseenter = function(){ this.style.background='#0078d7';this.style.color='#fff'; };
            item.onmouseleave = function(){ this.style.background='';this.style.color=''; };
            item.onclick = function(){ fn(); document.body.removeChild(menu); };
            menu.appendChild(item);
        }
        addItem2('外部ブラウザで開く', function(){ if(typeof _b==='function') _b('openUrlExternal',[imgUrl]); });
        addItem2('この画像をNG登録する', function(){ if(typeof _b==='function') _b('ngImage',[imgUrl]); });
        addItem2('画像URLをコピーする', function(){
            try{ navigator.clipboard.writeText(imgUrl); }catch(er){}
        });
        document.body.appendChild(menu);
        setTimeout(function(){
            document.addEventListener('click', function cleanup2(){
                var m2 = document.getElementById('__img_ctx2');
                if (m2) m2.parentNode.removeChild(m2);
                document.removeEventListener('click', cleanup2);
            });
        }, 0);
    };
    document.addEventListener('contextmenu', window._thumbCtx);

    /* ── スクロール時: 末尾表示の検知（タブ青背景・赤帯・既読数の同期） ── */
    /*    末尾を表示したら「既読（_unreadSeen）」とみなしタブ青背景をデフォルトに   */
    /*    戻し、新着の帯（.new-res）と仕切り線も既読化、bridge.bottomSeen で       */
    /*    Python側の既読数を同期する（更新しなくてもカタログの+Nが0に戻る）。       */
    /*    _unreadSeen は新着到着時に false へリセットされる（青背景を再表示可能に）。 */
    function _checkUnreadAtBottom() {
        /* 自分の書き込みで末尾へ自動スクロールした間は既読化しない。
           （赤帯・仕切り線・カタログ+N・タブ青背景を維持し、まだ読んでいない
           他人の新着レスを見落とさないようにする。投稿後スクロール中のみ
           _suppressUnreadClear=true。ユーザー自身のスクロールでは解除される） */
        if (window._suppressUnreadClear) return;
        var fromBottom = document.documentElement.scrollHeight
                         - window.scrollY - window.innerHeight;
        if (fromBottom <= 80) {
            window._unreadSeen = true;   /* 末尾を表示＝既読扱い */
            /* 実際に見えている時だけ既読化する（バックグラウンドタブの
               自動スクロール等で「見ていないのに既読」になるのを防ぐ）。
               bottomSeen はPython側で冪等（既読数に変化がなければ何もしない）。 */
            if (!document.hidden) {
                var newones = document.querySelectorAll('.res.new-res');
                if (newones.length) {
                    newones.forEach(function(el) { el.classList.remove('new-res'); });
                    if (typeof _updateNewResDivider === 'function') {
                        try { _updateNewResDivider(); } catch(e) {}
                    }
                }
                if (typeof bridge !== 'undefined' && bridge && bridge.bottomSeen) {
                    bridge.bottomSeen();
                }
            }
        }
        var has = (!window._unreadSeen)
                  && (document.querySelectorAll('.res.new-res').length > 0);
        /* bridge は WEBCHANNEL_JS で var 宣言される。エラー/404ページ
           （setHtml about:blank）には WEBCHANNEL_JS が無く bridge が未宣言の
           ことがあり、その場合 `bridge &&` だけでは Reference: bridge is not
           defined を投げる。typeof で未宣言を先にガードする。 */
        if (typeof bridge !== 'undefined' && bridge && bridge.notifyUnread) {
            bridge.notifyUnread(has);
        }
    }
    window.addEventListener('scroll', function() {
        clearTimeout(window._unreadScrollTimer);
        window._unreadScrollTimer = setTimeout(_checkUnreadAtBottom, 200);
    });
    /* タブアクティブ化時にPython側から呼べるよう公開（バックグラウンドで
       innerHeight=0のままロードされた画像モード等で、表示後に末尾が見えていれば
       青背景を解除するため）。 */
    window._checkUnreadAtBottom = _checkUnreadAtBottom;
    /* 初回チェック: スクロールしなくても既にページ末尾が見えている場合
       （画像モードのグリッドが短い等）に青背景を解除する。
       レイアウト確定後に走らせるため少し遅延させる。 */
    setTimeout(_checkUnreadAtBottom, 300);

})();"""
        # 「delしたレスを非表示にする」チェックの記憶状態をJSへ渡す（delResで参照）
        _dh = 'true' if getattr(self._settings, 'del_hide_checked', True) else 'false'
        js = f"window._delHideDefault = {_dh};\n" + js
        if not _safe_run_js(self._view, js):
            return          # タブ破棄後の遅延実行 → 以降の処理も行わない
        # ヒートマップは position:fixed の独立オーバーレイなので、全描画・モード
        # 切替のたびにbodyが作り直される → ここ（描画完了フック）で再適用する。
        self._apply_heatmap()

    def _on_thread_context_menu(self, pos):
        """スレッドビュー右クリックメニュー"""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest
        req = self._view.lastContextMenuRequest()
        on_image = (req is not None and
                    req.mediaType() == QWebEngineContextMenuRequest.MediaType.MediaTypeImage)
        menu = QMenu(self)
        if on_image:
            img_url = req.mediaUrl().toString() if req else ""
            act_open = menu.addAction("外部で開く")
            act_open.triggered.connect(lambda: self._open_url_external(img_url))
            menu.addSeparator()
        act_src = menu.addAction("ソースを表示")
        act_src.triggered.connect(self._show_thread_source)
        menu.addSeparator()
        # WebEngine デフォルトのコピー等
        page = self._view.page()
        act_copy = menu.addAction("コピー")
        act_copy.triggered.connect(lambda: page.triggerAction(
            QWebEnginePage.WebAction.Copy))
        menu.exec(self._view.mapToGlobal(pos))

    def _open_url_external(self, url: str):
        """URLを外部ブラウザで開く"""
        if url:
            _open_url(url)

    def _show_thread_source(self):
        """現在のスレッド HTML ソースをウィンドウ表示"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QPushButton
        def _cb(html):
            dlg = QDialog(self)
            dlg.setWindowTitle("ソース表示")
            dlg.resize(800, 600)
            lay = QVBoxLayout(dlg)
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText(html)
            te.setStyleSheet("font-family: monospace; font-size: 9pt;")
            lay.addWidget(te)
            btn = QPushButton("閉じる")
            btn.clicked.connect(dlg.close)
            lay.addWidget(btn)
            dlg.exec()
        self._view.page().toHtml(_cb)

    def _on_load_finished_scroll(self, _ok: bool):
        """ページ読込完了後にスクロール位置を復元"""
        # スレッドページのDOMがロード完了 → モード切替をDOM入替で行える
        self._thread_page_live = True
        if self._pending_scroll > 0:
            y = self._pending_scroll
            self._pending_scroll = 0
            # loadFinished直後は画像等のレイアウトが未確定で body 高さが不足し、
            # scrollTo(0,y) が上方向にクランプされて「先頭付近に飛ぶ」ことがある
            # （画像キャッシュ有無で再現が時々になる）。目標位置に届かない間は
            # 高さが伸びるのを待って数回リトライし、到達したら停止する。
            # （ユーザが下方向へ動かした場合は scrollY>=y で停止＝操作を妨げない）
            self._view.page().runJavaScript(
                "(function(){var y=" + str(int(y)) + ",tries=0;"
                "function go(){window.scrollTo(0,y);"
                "if(window.scrollY<y-2&&tries++<50){setTimeout(go,33);}}"
                "requestAnimationFrame(function(){requestAnimationFrame(go);});"
                "})();"
            )


    def _set_view_mode(self, mode: str):
        """返信モード / 画像モード / 引用モードを切り替える"""
        # ツールバーのモードボタンを同期（シグナルをブロックして再帰防止）
        if hasattr(self, '_mode_grp'):
            for btn in self._mode_grp.buttons():
                btn.blockSignals(True)
                btn.setChecked(btn.property("mode") == mode)
                btn.blockSignals(False)
        # スレッドページがライブ（DOMロード済み）なら、ページ再読込せず
        # body入替でモードを切り替える（一時ファイル書出し・ナビゲーション・
        # QWebChannel再構築のオーバーヘッドを排除）。未ロード時はフルレンダー。
        _live = getattr(self, '_thread_page_live', False) and self._thread is not None
        if mode == 'image':
            if _live:
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._render_image_mode_with_scroll(int(y) if y else 0))
            else:
                self._render_image_mode()
            return
        if mode == 'quote':
            if _live:
                self._view.page().runJavaScript(
                    "window.scrollY",
                    lambda y: self._render_quote_mode_with_scroll(int(y) if y else 0))
            else:
                self._render_quote_mode()
            return
        # 返信モード: 差分更新で _last_html が古い場合は最新モデルから再生成する
        # （返信→画像→返信 で「開いた時のレスしか出ない」不具合の修正）
        if getattr(self, '_last_html_dirty', False) and self._thread:
            self._rebuild_last_html()
        if hasattr(self, '_last_html') and self._last_html:
            url = (self._thread.url if self._thread else None) or 'https://www.2chan.net/'
            self._load_html_via_tempfile(self._last_html, QUrl(url))
        # ページ読込途中だと document.body が一瞬 null になり
        # 「Cannot read properties of null (reading 'dataset')」を投げるためガードする。
        self._view.page().runJavaScript('if(document.body)document.body.dataset.mode="' + mode + '"')

    def _rebuild_last_html(self):
        """最新のスレッドモデルから _last_html を再生成する。
        差分更新（_show_diff）はDOM追記のみで _last_html を更新しないため、
        返信モードへ戻る際に最新HTMLが必要な場合に呼ぶ。"""
        if not self._thread:
            return
        import datetime as _dt
        thread = self._thread
        _ucss = _load_user_css(self._settings)
        _ul   = getattr(self._settings, "uploader_links", [])
        _ng   = self._settings.ng_filter
        _ng_reveal = not self._ng_enabled
        _thread_url = thread.url or ""
        _hidden_nos = set(self._settings.ng_hidden_res_nos.get(_thread_url, []))
        _del_nos = set(self._settings.del_res_nos.get(_thread_url, []))
        _DAY_JP = ['月','火','水','木','金','土','日']
        def _footer(th):
            res_count = max(0, len(th.res_list) - 1)
            new_count = getattr(th, '_footer_new_count', 0)
            _n = _dt.datetime.now()
            now_str = (f"{_n.year}/{_n.month:02d}/{_n.day:02d}"
                       f" ({_DAY_JP[_n.weekday()]}) {_n.hour}:{_n.minute:02d}:{_n.second:02d}")
            return (self._expiry_line_html(th)
                    + f'<div class="page-footer">レス: {res_count}件 ／ 受信: {new_count}件'
                    f' ／ 最終更新: {now_str} ／ 2BP {APP_VER}</div>')
        _sbc = getattr(self._settings, 'scroll_bottom_count', 5)
        html, self._img_list = thread_to_html(
            thread, user_css=_ucss, uploaders=_ul,
            ng_filter=_ng, ng_settings=self._settings,
            hidden_nos=_hidden_nos, del_nos=_del_nos, ng_reveal=_ng_reveal, scroll_bottom_count=_sbc,
            footer_html=_footer(thread),
            my_nos=self._get_my_nos(thread), id_warn_count=getattr(self._settings,'id_warn_count',5),
            pseudo_expiring=_is_pseudo_red_thread(thread, self._settings), sort_by_sodane=getattr(self._settings, 'sort_by_sodane', False))
        self._last_html = html
        self._last_html_dirty = False


    def _build_respool_html(self, res_list, id_counts=None, id_warn: int = 0) -> str:
        """画像/引用モードのポップアップ用隠しプール(_respool)の内側HTMLを返す。

        全レスを render_res でフルレンダリングするのは重い（1000レスで切替毎に
        1000回）ため、per-res キャッシュ(_respool_cache)で変化のないレスの再実行を
        避ける。署名(sig)は render_res の出力に影響する可変フィールドのみで構成し、
        変化時だけ再生成する。NG状態は respool では描画に影響しない（ng_filterを
        渡さない＝CSSクラスで制御）ためキャッシュ無効化は不要。"""
        # スレッド同一性ガード: ビューが別スレに切り替わった場合は res_no が衝突
        # しうるためキャッシュを破棄する（通常タブは1スレ固定なので発生しない保険）。
        _tno = self._thread.no if self._thread else None
        if getattr(self, '_respool_cache_no', None) != _tno:
            self._respool_cache.clear()
            self._respool_cache_no = _tno
        cache = self._respool_cache
        parts = []
        for r in res_list:
            idc = id_counts.get(r.id_str, 0) if (id_counts and r.id_str) else 0
            sig = (idc, id_warn, r.sodane, r.is_new, r.is_deleted, r.expiry_str)
            ent = cache.get(r.no)
            if ent is not None and ent[0] == sig:
                parts.append(ent[1])
            else:
                frag = render_res(r, r.is_op, [], id_counts=id_counts, id_warn_count=id_warn)
                cache[r.no] = (sig, frag)
                parts.append(frag)
        return ''.join(parts)

    def _respool_inject_js(self, pool_inner_html: str) -> str:
        """隠しプール(_respool)の中身を後注入するJSを返す。
        画像/引用モードのbody入替に全レスのプールHTML(1000レス規模)を含めると
        innerHTML の同期DOMパースで切替が数百ms〜秒単位で固まるため、
        入替時は空プレースホルダのみ入れ、ペイント後にこのJSで流し込む。
        注入後に▼被引用インジケータ構築＋ポップアップ再フックを行う
        （従来swap内にあったrAF遅延処理をこちらへ移動。プール注入前に走ると
        プール内レスの▼が付かないため順序が重要）。"""
        import json as _json
        return (
            "(function(){var rp=document.getElementById('_respool');"
            "if(rp){rp.innerHTML=" + _json.dumps(pool_inner_html, ensure_ascii=False) + ";}"
            "})();\n"
            "requestAnimationFrame(function(){requestAnimationFrame(function(){"
            "if(typeof _rebuildQuoteIndicators==='function')_rebuildQuoteIndicators();"
            "if(typeof window._hookPopupQuoteInd==='function')window._hookPopupQuoteInd(document);"
            "});});\n"
        )

    def _gal_sel_ui_html(self) -> str:
        """画像モードの一括保存UI（選択モード開始ボタン＋下部選択バー）のHTML。
        設定「画像保存」の image_save_folders（画像タブのバーと同じ登録フォルダ）を
        [名前|…|▼] のグループで列挙する: 名前=そのフォルダへ即保存、
        …=フォルダ選択ダイアログ、▼=サブフォルダメニュー（サブフォルダがある時のみ）。"""
        import json as _json, os as _os
        from html import escape as _esc
        folders = [f for f in getattr(self._settings, 'image_save_folders', [])
                   if (f or '').strip()]
        label_len = getattr(self._settings, 'image_save_label_len', 0)
        btns = []
        for f in folders:
            base = _os.path.basename(f.rstrip('\\/')) or f
            name = base[:label_len] if label_len > 0 else base
            # JS文字列は json.dumps でエスケープし、シングルクォートはHTML属性の
            # 区切りと衝突するため文字参照化する
            _fj = _json.dumps(f).replace("'", "&#39;")
            g = (f"<span class='_selgrp'>"
                 f"<button onclick='_selSave({_fj})' title='{_esc(f)} に保存'>{_esc(name)}</button>"
                 f"<button onclick='_selBrowse({_fj})' title='保存先フォルダを選択して保存'>…</button>")
            if _has_subdir(f):
                g += f"<button onclick='_selSub({_fj})' title='サブフォルダを選んで保存'>▼</button>"
            g += "</span>"
            btns.append(g)
        if not btns:
            btns.append('<span style="opacity:.7">保存先フォルダ未登録'
                        '（画像タブの⚙または設定→画像保存で登録）</span>')
        _chk = 'checked ' if getattr(self._settings, 'img_bulk_close_on_save', True) else ''
        return (
            '<div id="_selmodebtn" onclick="_selToggleMode()" '
            'title="選択モード: ONの間はクリックで画像を選択（Ctrl+クリックは常時有効）">'
            '☑ 選択</div>'
            '<div id="_selbar">'
            '<span id="_selcnt">0件選択中</span>'
            + ''.join(btns) +
            '<span style="flex:1"></span>'
            '<label title="保存を開始したら選択モードを閉じる">'
            '<input type="checkbox" id="_selclosechk" ' + _chk +
            'onchange="_b(\'setGalSaveClose\',[this.checked])">保存後に閉じる</label>'
            '<button onclick="_selAll()">全選択</button>'
            '<button onclick="_selClear()">解除</button>'
            '</div>'
        )

    def _save_selected_images(self, folder: str, urls: list):
        """画像モードで選択した画像を指定フォルダへ一括保存する（BGスレッド）。
        先読みディスクキャッシュ(data/img)にあればコピー（再DLなし）、無ければDL。
        保存先に同名ファイルがあればスキップ（ふたばのファイル名はユニーク）。"""
        import os
        folder = (folder or '').strip()
        _urls = [u for u in (urls or []) if u]
        if not folder or not _urls:
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self._on_bulk_save_msg(f"⚠ フォルダ作成失敗: {e}")
            return

        def _do():
            import shutil
            from urllib.parse import urlparse as _up, unquote as _uq
            ok = skip = fail = 0
            total = len(_urls)
            for i, u in enumerate(_urls, 1):
                try:
                    if u.startswith("file://"):   # ログ(zip/html)のローカル画像
                        src = _uq(_up(u).path)
                        if len(src) >= 3 and src[0] == "/" and src[2] == ":":
                            src = src[1:]
                        name = os.path.basename(src)
                        dest = os.path.join(folder, name)
                        if not name or not os.path.exists(src):
                            fail += 1
                        elif os.path.exists(dest):
                            skip += 1
                        else:
                            shutil.copyfile(src, dest)
                            ok += 1
                    else:
                        name = u.split('/')[-1].split('?')[0]
                        if not name:
                            fail += 1
                            continue
                        dest = os.path.join(folder, name)
                        if os.path.exists(dest):
                            skip += 1
                        else:
                            _cp = None
                            try:
                                _p = self._fetcher._img_disk_path(u)
                                if _p.exists():
                                    _cp = str(_p)
                            except Exception:
                                _cp = None
                            if _cp:
                                shutil.copyfile(_cp, dest)
                            else:
                                r = self._fetcher.session.get(u, timeout=(15, 300))
                                r.raise_for_status()
                                with open(dest, 'wb') as f:
                                    f.write(r.content)
                            ok += 1
                except Exception:
                    fail += 1
                if i % 5 == 0 and i < total:
                    self._bulk_save_msg.emit(f"保存中 {i}/{total}")
            msg = f"✅ {ok}件保存"
            if skip:
                msg += f"（既存スキップ{skip}件）"
            if fail:
                msg += f"（失敗{fail}件）"
            self._bulk_save_msg.emit(msg)
            self._bulk_save_msg.emit("__CLEAR_SEL__")

        _fname = os.path.basename(folder.rstrip('\\/')) or folder
        self._on_bulk_save_msg(f"{len(_urls)}件を保存開始 → {_fname}")
        # 「保存後に閉じる」ON: 保存開始が確定したら選択モードを終了してバーを閉じる
        if getattr(self._settings, 'img_bulk_close_on_save', True):
            try:
                self._view.page().runJavaScript(
                    "window._selMode=false;window._selClear&&window._selClear();")
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def _browse_save_selected(self, folder: str, urls: list):
        """一括保存の「…」: フォルダ選択ダイアログで保存先を指定して保存"""
        import os
        from PySide6.QtWidgets import QFileDialog
        start = folder if folder and os.path.isdir(folder) else ""
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダを選択", start)
        if d:
            self._save_selected_images(d, urls)

    def _subfolder_save_menu(self, folder: str, urls: list):
        """一括保存の「▼」: サブフォルダ選択メニューをカーソル位置に表示して保存"""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QCursor
        menu = QMenu(self)
        _populate_subfolder_menu(menu, folder,
                                 lambda p: self._save_selected_images(p, urls))
        menu.exec(QCursor.pos())

    def _on_gal_save_close_changed(self, on: bool):
        """一括保存の「保存後に閉じる」チェック状態を設定へ保存"""
        self._settings.img_bulk_close_on_save = bool(on)
        try:
            self._settings.save()
        except Exception:
            pass

    def _on_bulk_save_msg(self, msg: str):
        """一括保存の進捗/完了をページ下部トースト(showDelMsg)で表示。
        __CLEAR_SEL__ は完了後の選択解除指示。"""
        try:
            if msg == "__CLEAR_SEL__":
                self._view.page().runJavaScript(
                    "window._selClear&&window._selClear();")
                return
            safe = msg.replace("\\", "\\\\").replace('"', '\\"')
            self._view.page().runJavaScript(f'showDelMsg("{safe}")')
        except Exception:
            pass

    def _expiry_line_html(self, thread) -> str:
        """スレ落ち予定（「○時頃消えます」）をフッター直上に左寄せ表示するHTML。
        赤字スレ(is_expiring)は赤、仮赤字(保存残1/10以下)はピンク、それ以外は灰色。
        expiry未取得時は空文字を返す（表示しない）。"""
        if not thread:
            return ""
        txt = (getattr(thread, "expiry", "") or "").strip()
        if not txt:
            return ""
        is_expiring = bool(getattr(thread, "is_expiring", False))
        is_pseudo = _is_pseudo_red_thread(thread, self._settings)
        if is_expiring:
            color = "#cc0000"
        elif is_pseudo:
            color = "#e07080"
        else:
            color = "#888888"
        from html import escape as _esc
        return (f'<div class="thread-expiry-info" '
                f'style="text-align:left;color:{color};font-size:small;'
                f'padding:6px 8px 0;">{_esc(txt)}</div>')

    def _expiry_banner_html(self, thread) -> str:
        """「このスレは古いので、もうすぐ消えます。」バナー。返信モード(thread_to_html)は
        自前で出しているが、画像/引用モードは独自HTMLのためここで同一バナーを提供する。
        赤字(is_expiring)・仮赤字(設定ON時の保存残1/10以下)のどちらでも表示する。"""
        if not thread:
            return ""
        if not (thread.is_expiring or _is_pseudo_red_thread(thread, self._settings)):
            return ""
        return ('<div class="expiry-banner">'
                'このスレは古いので、もうすぐ消えます。'
                '</div>')

    def _expiry_banner_sync_js(self, thread) -> str:
        """差分更新(appendNewReplies等)はページ全体を再生成しないため、赤字/仮赤字状態が
        更新途中で変化してもバナーが追随しない。この JS を差分更新のJSに連結して呼ぶことで、
        現在の状態に合わせて .expiry-banner の追加/削除を行う（.thread-end の直後に配置、
        返信モードのフッター＝page-footerより前）。"""
        want = bool(thread and (thread.is_expiring or _is_pseudo_red_thread(thread, self._settings)))
        return (
            "(function(){"
            f"var want={'true' if want else 'false'};"
            "var el=document.querySelector('.expiry-banner');"
            "if(want&&!el){"
            "var d=document.createElement('div');d.className='expiry-banner';"
            "d.textContent='このスレは古いので、もうすぐ消えます。';"
            "var end=document.querySelector('.thread-end');"
            "if(end&&end.parentNode)end.parentNode.insertBefore(d,end.nextSibling);"
            "else document.body.appendChild(d);"
            "}else if(!want&&el){el.remove();}"
            "})();"
        )

    def _thread_footer_html(self, thread) -> str:
        """スレ表示用フッター（レス数/受信/最終更新/バージョン）HTMLを返す。
        返信モードと同一内容を画像・引用モードでも使うための共通生成。"""
        import datetime as _dt
        if not thread:
            return ""
        _DAY_JP = ['月','火','水','木','金','土','日']
        res_count = max(0, len(thread.res_list) - 1)  # OP除く
        new_count = getattr(thread, '_footer_new_count', 0)
        _n = _dt.datetime.now()
        now_str = (f"{_n.year}/{_n.month:02d}/{_n.day:02d}"
                   f" ({_DAY_JP[_n.weekday()]}) {_n.hour}:{_n.minute:02d}:{_n.second:02d}")
        return (self._expiry_line_html(thread)
                + f'<div class="page-footer">レス: {res_count}件 ／ 受信: {new_count}件'
                f' ／ 最終更新: {now_str} ／ 2BP {APP_VER}</div>')

    def _render_quote_mode(self):
        """引用ツリーモード: 返信関係をツリー表示"""
        if not self._thread:
            return
        import re as _re
        res_list = self._thread.res_list
        if not res_list:
            return

        # --- 親子関係を構築 ---
        res_map = {r.no: r for r in res_list}
        children = {r.no: [] for r in res_list}
        parent_of = {}

        # 画像ファイル名引用 ( >1234567890.png 等 ) → その画像を投稿したレスを親に。
        # サーバ割当ファイル名（画像URLのbasename）で照合する。
        _qt_img_re = _re.compile(
            r"^>+(\d{10,}\.(?:jpe?g|png|gif|webp|bmp|mp4|webm))\s*$", _re.IGNORECASE)
        _img_src_by_name = {}
        for _r in res_list:
            if _r.image_url:
                _fn = _r.image_url.rsplit("/", 1)[-1].split("?")[0].lower()
                if _fn:
                    _img_src_by_name.setdefault(_fn, _r.no)
        for res in res_list:
            txt = (res.comment_text or "").strip()
            quoted = set()
            for line in txt.split("\n"):
                line = line.strip()
                if not line.startswith(">"):
                    continue
                # 画像ファイル名引用を先に判定（>数字.ext は数字引用に誤判定されるため）
                mi = _qt_img_re.match(line)
                if mi:
                    _pno = _img_src_by_name.get(mi.group(1).lower())
                    if _pno is not None and _pno != res.no:
                        quoted.add(_pno)
                    continue
                m = _re.match(r">+(No\.)?(\d+)", line)
                if m:
                    qno = int(m.group(2))
                    if qno in res_map and qno != res.no:
                        quoted.add(qno)
                else:
                    q = line.lstrip(">").strip()
                    if len(q) >= 2:
                        for cand in res_list:
                            if cand.no != res.no and q in (cand.comment_text or ""):
                                quoted.add(cand.no)
                                break
            if quoted:
                par = max(quoted, key=lambda n: res_map[n].res_idx if n in res_map else 0)
                parent_of[res.no] = par
                children[par].append(res.no)

        # --- HTML生成 ---
        def _esc(s): return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        def _short(res):
            lines = (res.comment_text or "").split("\n")
            # 「>テキスト」の緑引用行（>No.XXX以外）を除外し、通常テキストのみ残す
            kept = []
            for ln in lines:
                s = ln.strip()
                if s.startswith(">") and not _re.match(r">+(No\.)?\d+", s):
                    continue  # 引用テキスト行はスキップ
                kept.append(s)
            t = " ".join(kept).strip()
            return t[:60] + ("…" if len(t) > 60 else "")

        rows = []
        seq = [0]
        _hidden, _delnos, _is_ng, _reveal = self._mode_marker_sets()
        _my_nos = self._get_my_nos(self._thread)   # 自分のレス（青帯）用
        _sort_sod = getattr(self._settings, 'sort_by_sodane', False)  # そ順
        from futaba2b_html import ng_info_text as _ngit

        def render_node(no, prefix, is_last, depth):
            res = res_map[no]
            # 連番は返信モードと同じ通し番号(res_idx)を使う
            # （従来はツリー走査順 seq[0] のため返信モードとずれていた）
            sn = f"{res.res_idx:03d}"
            is_new = res.is_new
            new_tag = ' <span class="qt-new">[新着]</span>' if is_new else ""
            _il = getattr(self, '_img_list', [])
            _ii = next((j for j, e in enumerate(_il) if e.get('url') == res.image_url), -1)
            img_tag = (f' <img class="qt-thumb" src="{res.thumb_url}" loading="lazy"'
                       f' onclick="openImg(\'{res.image_url}\',{_ii});return false;"'
                       f' onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{res.image_url}\',{_ii});}}"'
                       f' data-full="{res.image_url}">'
                       if res.image_url and res.thumb_url else "")
            txt = _esc(_short(res))
            no_str = f'<a class="qt-no" href="#r{no}" onclick="delRes({no},this);return false;">No.{no}</a>'
            _sod = (f'<span class="qt-sod">そうだね{res.sodane}</span>'
                    if _sort_sod else '')
            _del_c = " deleted" if res.is_deleted else ""
            if no in _my_nos:   _del_c += " self-res"   # 自分のレス→青帯
            elif res.is_new:    _del_c += " new-res"    # 新着レス→赤帯
            _ngm = _is_ng(res) or (no in _hidden)   # NG対象(NGワード/画像 or 手動NG/del登録)
            if _ngm: _del_c += " ng-band"           # → 緑帯
            if _ngm and not _reveal:                # NG使う時は非表示、解除時は帯付き表示
                _del_c += " ng-hidden"
            _ngi = ""
            if _ngm:
                try:
                    _ngi = _ngit(res, self._settings.ng_filter,
                                 self._settings, no in _hidden)
                except Exception:
                    _ngi = ""
            _ngi_attr = f' data-ng-info="{_esc(_ngi)}"' if _ngi else ""
            _dm = ' <span class="del-done">del済</span>' if no in _delnos else ''

            if depth == 0:
                rows.append('<div class="qt-sep"></div>')
                rows.append(
                    f'<div class="qt-row qt-root{_del_c}"{_ngi_attr}>'
                    f'<span class="qt-idx">{sn}</span> {img_tag}{no_str}{_sod}{_dm} '
                    f'<span class="qt-txt">{txt}</span>{new_tag}</div>'
                )
            else:
                branch = "└" if is_last else "├"
                rows.append(
                    f'<div class="qt-row qt-child{_del_c}"{_ngi_attr} style="margin-left:{depth*20}px">'
                    f'<span class="qt-branch">{branch}</span> '
                    f'<span class="qt-idx">{sn}</span> {img_tag}{no_str}{_sod}{_dm} '
                    f'<span class="qt-txt">{txt}</span>{new_tag}</div>'
                )

            ch = children.get(no, [])
            for i, cno in enumerate(ch):
                render_node(cno, prefix, i == len(ch) - 1, depth + 1)

        roots = [r for r in res_list if r.no not in parent_of]
        if _sort_sod:
            # ツリーは保ったまま各階層内でそうだね降順（同数は投稿順=安定ソート）。
            # まず親(ルート)を、次に各階層の子リストを入れ替える。
            _sk = lambda n: res_map[n].sodane if n in res_map else 0
            for _p in children:
                children[_p].sort(key=_sk, reverse=True)
            roots.sort(key=lambda r: r.sodane, reverse=True)
        for r in roots:
            render_node(r.no, "", True, 0)
        rows.append('<div class="qt-sep"></div>')

        # ポップアップ用：各レスのHTMLを _respool に格納
        res_pool_html = self._build_respool_html(res_list)
        res_pool = ('<div id="_respool" style="position:absolute;left:-9999px;width:520px;'
                    'visibility:hidden;pointer-events:none;">'
                    + res_pool_html + '</div>')

        _qt_add = _QT_MODE_CSS
        _sbc = getattr(self._settings, 'scroll_bottom_count', 5)
        _scroll_js = _make_scroll_bottom_js(_sbc, getattr(self._settings,'scroll_top_count',0))
        _ucss_q = _load_user_css(self._settings)
        _usr_q = f"<style id='__usercss'>{_ucss_q}</style>" if _ucss_q else ""
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{THREAD_CSS}{_qt_add}</style>"
            f"{_usr_q}"
            f"{WEBCHANNEL_JS}"
            f"{_scroll_js}"
            f"</head><body>{getattr(self,'_error_banner_html','')}{chr(10).join(rows)}"
            f"{self._expiry_banner_html(self._thread)}{res_pool}{self._thread_footer_html(self._thread)}</body></html>"
        )
        url = (self._thread.url if self._thread else None) or "https://www.2chan.net/"
        self._load_html_via_tempfile(html, QUrl(url))
        self._loaded_page_mode = 'quote'
        self._sync_del_btn_after_full_render()

    def _render_quote_mode_with_scroll(self, scroll_y: int = 0):
        """スクロール位置を保持しながら引用モードを再描画する（DOM書き換え方式・ページリロードなし）。
        返信/画像モードからの切替でも、引用CSSをheadへ注入してから body を入れ替えるので
        ページ再読込は不要。DOMが未ロードのときのみフルレンダーにフォールバックする。"""
        if not self._thread: return
        if not getattr(self, '_thread_page_live', False):
            self._render_quote_mode()
            return
        import re as _re, json as _json
        res_list = self._thread.res_list
        if not res_list: return

        # --- 親子関係を構築（_render_quote_mode と同じロジック） ---
        res_map = {r.no: r for r in res_list}
        children = {r.no: [] for r in res_list}
        parent_of = {}
        # 画像ファイル名引用 ( >1234567890.png 等 ) → その画像を投稿したレスを親に。
        # サーバ割当ファイル名（画像URLのbasename）で照合する。
        _qt_img_re = _re.compile(
            r"^>+(\d{10,}\.(?:jpe?g|png|gif|webp|bmp|mp4|webm))\s*$", _re.IGNORECASE)
        _img_src_by_name = {}
        for _r in res_list:
            if _r.image_url:
                _fn = _r.image_url.rsplit("/", 1)[-1].split("?")[0].lower()
                if _fn:
                    _img_src_by_name.setdefault(_fn, _r.no)
        for res in res_list:
            txt = (res.comment_text or "").strip()
            quoted = set()
            for line in txt.split("\n"):
                line = line.strip()
                if not line.startswith(">"):
                    continue
                # 画像ファイル名引用を先に判定（>数字.ext は数字引用に誤判定されるため）
                mi = _qt_img_re.match(line)
                if mi:
                    _pno = _img_src_by_name.get(mi.group(1).lower())
                    if _pno is not None and _pno != res.no:
                        quoted.add(_pno)
                    continue
                m = _re.match(r">+(No\.)?(\d+)", line)
                if m:
                    qno = int(m.group(2))
                    if qno in res_map and qno != res.no:
                        quoted.add(qno)
                else:
                    q = line.lstrip(">").strip()
                    if len(q) >= 2:
                        for cand in res_list:
                            if cand.no != res.no and q in (cand.comment_text or ""):
                                quoted.add(cand.no)
                                break
            if quoted:
                par = max(quoted, key=lambda n: res_map[n].res_idx if n in res_map else 0)
                parent_of[res.no] = par
                children[par].append(res.no)

        def _esc(s): return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        def _short(res):
            lines = (res.comment_text or "").split("\n")
            # 「>テキスト」の緑引用行（>No.XXX以外）を除外し、通常テキストのみ残す
            # （初回 _render_quote_mode と同一ロジック。更新後に > が復活するのを防ぐ）
            kept = []
            for ln in lines:
                s = ln.strip()
                if s.startswith(">") and not _re.match(r">+(No\.)?\d+", s):
                    continue  # 引用テキスト行はスキップ
                kept.append(s)
            t = " ".join(kept).strip()
            return t[:60] + ("…" if len(t) > 60 else "")
        rows = []
        seq = [0]
        _hidden, _delnos, _is_ng, _reveal = self._mode_marker_sets()
        _my_nos = self._get_my_nos(self._thread)   # 自分のレス（青帯）用
        _sort_sod = getattr(self._settings, 'sort_by_sodane', False)  # そ順
        from futaba2b_html import ng_info_text as _ngit
        def render_node(no, prefix, is_last, depth):
            res = res_map[no]
            # 連番は返信モードと同じ通し番号(res_idx)を使う
            # （従来はツリー走査順 seq[0] のため返信モードとずれていた）
            sn = f"{res.res_idx:03d}"
            is_new = res.is_new
            new_tag = ' <span class="qt-new">[新着]</span>' if is_new else ""
            _il = getattr(self, '_img_list', [])
            _ii = next((j for j, e in enumerate(_il) if e.get('url') == res.image_url), -1)
            img_tag = (f' <img class="qt-thumb" src="{res.thumb_url}" loading="lazy"'
                       f' onclick="openImg(\'{res.image_url}\',{_ii});return false;"'
                       f' onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{res.image_url}\',{_ii});}}"'
                       f' data-full="{res.image_url}">'
                       if res.image_url and res.thumb_url else "")
            txt = _esc(_short(res))
            no_str = f'<a class="qt-no" href="#r{no}" onclick="delRes({no},this);return false;">No.{no}</a>'
            _sod = (f'<span class="qt-sod">そうだね{res.sodane}</span>'
                    if _sort_sod else '')
            _del_c = " deleted" if res.is_deleted else ""
            if no in _my_nos:   _del_c += " self-res"   # 自分のレス→青帯
            elif res.is_new:    _del_c += " new-res"    # 新着レス→赤帯
            _ngm = _is_ng(res) or (no in _hidden)
            if _ngm: _del_c += " ng-band"
            if _ngm and not _reveal:
                _del_c += " ng-hidden"
            _ngi = ""
            if _ngm:
                try:
                    _ngi = _ngit(res, self._settings.ng_filter,
                                 self._settings, no in _hidden)
                except Exception:
                    _ngi = ""
            _ngi_attr = f' data-ng-info="{_esc(_ngi)}"' if _ngi else ""
            _dm = ' <span class="del-done">del済</span>' if no in _delnos else ''
            if depth == 0:
                rows.append('<div class="qt-sep"></div>')
                rows.append(
                    f'<div class="qt-row qt-root{_del_c}"{_ngi_attr}>'                    f'<span class="qt-idx">{sn}</span> {img_tag}{no_str}{_sod}{_dm} '                    f'<span class="qt-txt">{txt}</span>{new_tag}</div>'
                )
            else:
                branch = "└" if is_last else "├"
                rows.append(
                    f'<div class="qt-row qt-child{_del_c}"{_ngi_attr} style="margin-left:{depth*20}px">'                    f'<span class="qt-branch">{branch}</span> '                    f'<span class="qt-idx">{sn}</span> {img_tag}{no_str}{_sod}{_dm} '                    f'<span class="qt-txt">{txt}</span>{new_tag}</div>'
                )
            ch = children.get(no, [])
            for i, cno in enumerate(ch):
                render_node(cno, prefix, i == len(ch) - 1, depth + 1)

        roots = [r for r in res_list if r.no not in parent_of]
        if _sort_sod:
            # ツリーは保ったまま各階層内でそうだね降順（同数は投稿順=安定ソート）。
            # まず親(ルート)を、次に各階層の子リストを入れ替える。
            _sk = lambda n: res_map[n].sodane if n in res_map else 0
            for _p in children:
                children[_p].sort(key=_sk, reverse=True)
            roots.sort(key=lambda r: r.sodane, reverse=True)
        for r in roots:
            render_node(r.no, "", True, 0)
        rows.append('<div class="qt-sep"></div>')

        # ポップアップ用 _respool も更新。切替を速くするため、body入替には空の
        # プレースホルダのみ入れ、重い全レスプールHTMLのDOMパースは初回ペイント後に
        # 後注入する（_respool_inject_js）。画像/引用切替が「固まる」主因は
        # このプール(1000レス規模)を innerHTML で同時パースしていたことによる。
        res_pool_html = self._build_respool_html(res_list)
        res_pool = ('<div id="_respool" style="position:absolute;left:-9999px;width:520px;'
                    'visibility:hidden;pointer-events:none;"></div>')

        body_html = (getattr(self, "_error_banner_html", "") + "\n".join(rows)
                     + self._expiry_banner_html(self._thread)
                     + res_pool + self._thread_footer_html(self._thread))
        body_js = _json.dumps(body_html, ensure_ascii=False)
        css_js  = _json.dumps(_QT_MODE_CSS, ensure_ascii=False)
        js = (
            # 別モードからの切替時、引用CSSを注入（id重複ガード）。user.css(#__usercss)
            # の直前に挿入し、base→mode→user の順＝user.cssが勝つ順序を保つ。
            "(function(){if(document.getElementById('__qtcss'))return;"
            "var s=document.createElement('style');s.id='__qtcss';"
            "s.textContent=" + css_js + ";"
            "var u=document.getElementById('__usercss');"
            "if(u){document.head.insertBefore(s,u);}else{document.head.appendChild(s);}"
            "})();\n"
            f"document.body.innerHTML = {body_js};\n"
            "document.body.dataset.mode='quote';\n"
            f"window.scrollTo(0, {int(scroll_y)});\n"
        )
        _pool_js = self._respool_inject_js(res_pool_html)
        self._loaded_page_mode = 'quote'
        # swap（即ペイント）→ プール後注入＋▼構築 → ポップアップJS注入 の直列実行
        def _after_pool(_r2):
            QTimer.singleShot(50, self._inject_popup_js)
        def _after_swap(_r):
            # 非同期コールバックのため、この時点でタブが閉じられていることがある
            _safe_run_js(self._view, _pool_js, _after_pool)
        _safe_run_js(self._view, js, _after_swap)

    def _render_image_mode(self):
        if not self._thread: return
        img_res = [(s+1,r) for s,r in enumerate(r for r in self._thread.res_list if r.image_url)]
        if getattr(self._settings, 'sort_by_sodane', False):
            # そうだね降順（同数は元の投稿順=安定）。seqを振り直し、ギャラリー/前後移動も同順にする
            _srt = sorted((r for _, r in img_res), key=lambda r: r.sodane, reverse=True)
            img_res = [(i + 1, r) for i, r in enumerate(_srt)]
        self._gallery_list = [{'url':r.image_url,'thumb':r.thumb_url,'res_no':r.no,'name':r.image_name}
                               for _,r in img_res]
        def _fmt(b):
            if b>=1048576: return f'{b/1048576:.1f}MB'
            if b>=1024: return f'{b//1024}KB'
            return f'{b}B' if b else '?'
        items=[]
        _hidden, _delnos, _is_ng, _reveal = self._mode_marker_sets()
        _my_nos = self._get_my_nos(self._thread)   # 自分のレス（青帯）用
        # ポップアップ用に全レスを隠しプールへ（▼被引用の引用元が画像なしレスでも
        # ポップアップ表示・引用マップ計算ができるよう全件入れる）
        # ID横の書き込み件数[N]を表示するため id_counts / id_warn_count を渡す。
        _id_counts = {}
        for _r in self._thread.res_list:
            if _r.id_str:
                _id_counts[_r.id_str] = _id_counts.get(_r.id_str, 0) + 1
        _id_warn = getattr(self._settings, 'id_warn_count', 5)
        _respool_inner=self._build_respool_html(self._thread.res_list, _id_counts, _id_warn)
        _hover_pop = getattr(self._settings, 'image_mode_hover_popup', True)
        for seq,r in img_res:
            ext=(r.image_name.rsplit('.',1)[-1].upper() if '.' in r.image_name else '?')
            info=ext+' / '+_fmt(r.file_size_bytes)
            idx=seq-1
            # 左上の番号: スレ内通し番号（OP=0 → "OP"、返信は res_idx）
            display_no = "OP" if r.res_idx == 0 else str(r.res_idx)
            _gi_cls = "gi"
            if r.is_deleted:     _gi_cls += " deleted"
            if r.no in _my_nos:  _gi_cls += " self-res"   # 自分のレス→青帯
            elif r.is_new:       _gi_cls += " new-res"    # 新着レス→赤帯
            _ngm = _is_ng(r) or (r.no in _hidden)   # NG対象(NGワード/画像 or 手動NG/del登録)
            if _ngm:             _gi_cls += " ng-band"      # → 緑帯
            if _ngm and not _reveal:                        # NG使う時は非表示、解除時は帯付き表示
                _gi_cls += " ng-hidden"
            _gi_del = '<div class="gi-del">del済</div>' if r.no in _delnos else ''
            items.append(
                '<div class="'+_gi_cls+'" data-res-no="'+str(r.no)+'" data-img-url="'+r.image_url+'"'
                ' onclick="_giClick(event,'+str(idx)+',this)"'
                ' onmousedown="if(event.button===1){event.preventDefault();openImgBg(\''+r.image_url+'\','+str(idx)+');}">'
                + _gi_del +
                '<div class="gn" data-popup-no="'+str(r.no)+'">'+display_no+'</div>'
                '<div class="gt"'+(' data-popup-no="'+str(r.no)+'"' if _hover_pop else '')+'><img src="'+r.thumb_url+'" loading="lazy"></div>'
                '<div class="gs">'+info+'</div>'
                '</div>'
            )
        _cols = max(1, int(getattr(self._settings, "image_mode_cols", 6)))
        _img_add = _img_mode_css(_cols)
        # 画像モード固有関数のみ追加定義（_b/openImgBg/sodane/openUrl等はWEBCHANNEL_JSで共通定義）
        _img_js = "function openGalleryImg(i){_b('openGalleryImg',[i]);}" + _GAL_SEL_JS
        # 隠しレスプール（popup_js が getElementById('rNNNN') で参照する）
        res_pool = ('<div id="_respool" style="position:absolute;left:-9999px;width:520px;'
                    'visibility:hidden;pointer-events:none;">'
                    + _respool_inner + '</div>')
        _sbc_img = getattr(self._settings, 'scroll_bottom_count', 5)
        _scroll_js_img = _make_scroll_bottom_js(_sbc_img, getattr(self._settings,'scroll_top_count',0))
        _ucss_i = _load_user_css(self._settings)
        _usr_i = f'<style id="__usercss">{_ucss_i}</style>' if _ucss_i else ''
        html=('<!DOCTYPE html><html><head><meta charset="utf-8">'
              f'<style>{THREAD_CSS}{_img_add}</style>'
              f'{_usr_i}'
              f'{WEBCHANNEL_JS}'
              '<script>'+_img_js+'</script>'
              f'{_scroll_js_img}'
              f'</head><body>{getattr(self,"_error_banner_html","")}<div class="wrap"><div class="grid">{"".join(items)}</div></div>'
              f'{self._gal_sel_ui_html()}'
              f'{self._expiry_banner_html(self._thread)}'
              f'{res_pool}{self._thread_footer_html(self._thread)}</body></html>')
        url=(self._thread.url if self._thread else None) or 'https://www.2chan.net/'
        self._load_html_via_tempfile(html, QUrl(url))
        self._loaded_page_mode = 'image'
        self._sync_del_btn_after_full_render()

    def _render_image_mode_with_scroll(self, scroll_y: int = 0):
        """スクロール位置を保持しながら画像モードを再描画する（DOM書き換え方式・ページリロードなし）。
        返信/引用モードからの切替でも、グリッドCSSをheadへ注入してから body を入れ替えるので
        ページ再読込は不要。DOMが未ロードのときのみフルレンダーにフォールバックする。"""
        if not self._thread: return
        if not getattr(self, '_thread_page_live', False):
            self._render_image_mode()
            return
        # ── ギャラリーHTML断片を生成（body内コンテンツのみ） ──
        img_res = [(s+1,r) for s,r in enumerate(r for r in self._thread.res_list if r.image_url)]
        if getattr(self._settings, 'sort_by_sodane', False):
            # そうだね降順（同数は元の投稿順=安定）。seqを振り直し、ギャラリー/前後移動も同順にする
            _srt = sorted((r for _, r in img_res), key=lambda r: r.sodane, reverse=True)
            img_res = [(i + 1, r) for i, r in enumerate(_srt)]
        self._gallery_list = [{'url':r.image_url,'thumb':r.thumb_url,'res_no':r.no,'name':r.image_name}
                               for _,r in img_res]
        def _fmt(b):
            if b>=1048576: return f'{b/1048576:.1f}MB'
            if b>=1024: return f'{b//1024}KB'
            return f'{b}B' if b else '?'
        items=[]
        _hidden, _delnos, _is_ng, _reveal = self._mode_marker_sets()
        _my_nos = self._get_my_nos(self._thread)   # 自分のレス（青帯）用
        # ポップアップ用に全レスを隠しプールへ（▼被引用対応のため全件）。
        # ID横の書き込み件数[N]を表示するため id_counts / id_warn_count を渡す。
        _id_counts = {}
        for _r in self._thread.res_list:
            if _r.id_str:
                _id_counts[_r.id_str] = _id_counts.get(_r.id_str, 0) + 1
        _id_warn = getattr(self._settings, 'id_warn_count', 5)
        _respool_inner=self._build_respool_html(self._thread.res_list, _id_counts, _id_warn)
        _hover_pop = getattr(self._settings, 'image_mode_hover_popup', True)
        for seq,r in img_res:
            ext=(r.image_name.rsplit('.',1)[-1].upper() if '.' in r.image_name else '?')
            info=ext+' / '+_fmt(r.file_size_bytes)
            idx=seq-1
            display_no = "OP" if r.res_idx == 0 else str(r.res_idx)
            _gi_cls = "gi"
            if r.is_deleted:     _gi_cls += " deleted"
            if r.no in _my_nos:  _gi_cls += " self-res"   # 自分のレス→青帯
            elif r.is_new:       _gi_cls += " new-res"    # 新着レス→赤帯
            _ngm = _is_ng(r) or (r.no in _hidden)   # NG対象(NGワード/画像 or 手動NG/del登録)
            if _ngm:             _gi_cls += " ng-band"      # → 緑帯
            if _ngm and not _reveal:                        # NG使う時は非表示、解除時は帯付き表示
                _gi_cls += " ng-hidden"
            _gi_del = '<div class="gi-del">del済</div>' if r.no in _delnos else ''
            items.append(
                '<div class="'+_gi_cls+'" data-res-no="'+str(r.no)+'" data-img-url="'+r.image_url+'"'
                ' onclick="_giClick(event,'+str(idx)+',this)"'
                ' onmousedown="if(event.button===1){event.preventDefault();openImgBg(\''+r.image_url+'\','+str(idx)+');}">'
                + _gi_del +
                '<div class="gn" data-popup-no="'+str(r.no)+'">'+display_no+'</div>'
                '<div class="gt"'+(' data-popup-no="'+str(r.no)+'"' if _hover_pop else '')+'><img src="'+r.thumb_url+'" loading="lazy"></div>'
                '<div class="gs">'+info+'</div>'
                '</div>'
            )
        # 切替を速くするため、body入替には空のプレースホルダのみ入れ、重い全レス
        # プールHTMLのDOMパースは初回ペイント後に後注入する（_respool_inject_js）。
        res_pool = ('<div id="_respool" style="position:absolute;left:-9999px;width:520px;'
                    'visibility:hidden;pointer-events:none;"></div>')
        grid_html = (getattr(self, "_error_banner_html", "")
                     + '<div class="wrap"><div class="grid">'
                     + ''.join(items)
                     + '</div></div>'
                     + self._gal_sel_ui_html()
                     + self._expiry_banner_html(self._thread)
                     + res_pool
                     + self._thread_footer_html(self._thread))
        import json as _json
        grid_js = _json.dumps(grid_html, ensure_ascii=False)
        # _gallery_list も JS側に同期
        gallery_js = _json.dumps(self._gallery_list, ensure_ascii=False)
        _cols = max(1, int(getattr(self._settings, "image_mode_cols", 6)))
        css_js = _json.dumps(_img_mode_css(_cols), ensure_ascii=False)
        js = (
            # 別モードからの切替時、グリッドCSSを注入（id重複ガード）。user.css(#__usercss)
            # の直前に挿入し、base→mode→user の順＝user.cssが勝つ順序を保つ。
            "(function(){if(document.getElementById('__imgcss'))return;"
            "var s=document.createElement('style');s.id='__imgcss';"
            "s.textContent=" + css_js + ";"
            "var u=document.getElementById('__usercss');"
            "if(u){document.head.insertBefore(s,u);}else{document.head.appendChild(s);}"
            "})();\n"
            # 一括保存の選択UI関数（返信ページ由来のheadには無いため注入・多重ガード付き）
            + _GAL_SEL_JS + "\n"
            "document.body.innerHTML = " + grid_js + ";\n"
            "document.body.dataset.mode='image';\n"
            "window._galleryList = " + gallery_js + ";\n"
            # body入替で選択セルは消えるため、バー表示/ボタン状態を現状態に再同期
            "window._selUpdate&&window._selUpdate();\n"
            "window.scrollTo(0, " + str(int(scroll_y)) + ");\n"
        )
        _pool_js = self._respool_inject_js(_respool_inner)
        self._loaded_page_mode = 'image'
        # swap（即ペイント）→ プール後注入＋▼構築 → ポップアップJS注入 の直列実行
        def _after_pool(_r2):
            QTimer.singleShot(50, self._inject_popup_js)
        def _after_swap(_r):
            # 非同期コールバックのため、この時点でタブが閉じられていることがある
            _safe_run_js(self._view, _pool_js, _after_pool)
        _safe_run_js(self._view, js, _after_swap)

    def _on_gallery_img(self, idx: int):
        lst = getattr(self, '_gallery_list', [])
        if not (0 <= idx < len(lst)):
            return
        url = lst[idx]['url']
        # 動画（mp4/webm等）はVideoPlayerWindowで再生
        lo = url.lower().split('?')[0]
        if any(lo.endswith(ext) for ext in ('.mp4', '.webm', '.mov', '.m4v')):
            _show_video_window(url, self._fetcher, getattr(self, '_settings', None))
        else:
            flst, fidx = self._filter_img_list_for_tab(lst, url)
            self.open_image_tab.emit(url, flst, fidx)


    def _do_extract(self, query: str = ""):
        """テキストにマッチするレスを抽出する（空のとき解除）。
        「ポップアップ」チェックONなら右上のパネルに表示(extractPostsPopup)、
        OFFならスレ本文を絞り込み(extractPosts)。
        スレ内絞り込みは返信モード専用のため、画像/引用モード中はOFFでも
        パネル表示にフォールバックする。切替時の残留を防ぐため常に
        もう一方のモードを解除してから適用する。"""
        q = (query if isinstance(query, str) else self._search_edit.text()).strip()
        js_q = q.replace("\\", "\\\\").replace('"', '\\"')
        in_thread = (not getattr(self._settings, 'extract_popup', True)
                     and not getattr(self, '_loaded_page_mode', ''))
        if in_thread:
            js = f'try{{extractPostsPopup("");extractPosts("{js_q}");}}catch(e){{}}'
        else:
            js = f'try{{extractPosts("");extractPostsPopup("{js_q}");}}catch(e){{}}'
        self._view.page().runJavaScript(js)

    def _on_extract_mode_toggled(self, on: bool):
        """抽出の「ポップアップ」チェック切替 → 設定に保存し、現在の抽出を再適用"""
        self._settings.extract_popup = bool(on)
        try:
            self._settings.save()
        except Exception:
            pass
        self._do_extract(self._search_edit.text())

    def _get_my_nos(self, thread) -> set:
        """このスレッドで自分が投稿したレス番号のセットを返す"""
        if not getattr(self._settings, "self_res_highlight", True):
            return set()
        url = thread.url if thread else ""
        return set(self._settings.my_post_nos.get(url, []))

    def _check_self_res_notifications(self, thread, new_res: list):
        """新着レスの中に自分のレスへのそうだね増加・返信があれば右上にポップアップ通知する"""
        s = self._settings
        my_nos = set(s.my_post_nos.get(thread.url, []))
        if not my_nos:
            return

        sodane_on  = getattr(s, "self_res_sodane_notify", True)
        reply_on   = getattr(s, "self_res_reply_notify",  True)
        if not sodane_on and not reply_on:
            return

        # そうだね増加: 自分のレスで sodane が前回より増えたもの
        if sodane_on:
            prev_sodane = getattr(self, "_my_sodane_cache", {})
            new_sodane_cache = {}
            for r in thread.res_list:
                if r.no in my_nos:
                    new_sodane_cache[r.no] = r.sodane
                    old = prev_sodane.get(r.no, r.sodane if not prev_sodane else 0)
                    if r.sodane > old and r.no in prev_sodane:
                        import re as _re
                        cmt = _re.sub(r"<[^>]+>", "", r.comment_text or "").strip()[:60]
                        msg = f"No.{r.no} そうだね {r.sodane}件\n{cmt}"
                        self._show_self_res_popup(msg, "sodane")
            self._my_sodane_cache = new_sodane_cache

        # 返信: 新着レスのコメントに自分のレスへの引用がある場合
        if reply_on:
            import re as _re
            import html as _html
            from bs4 import BeautifulSoup as _BS

            # 自分レスを「行」に分解したマップ: no -> list[str]（strip済み・空行除去）。
            # 引用判定は行単位で行う（>を含む引用行 と 地の文 を区別するため）。
            my_res_lines = {}
            for r in thread.res_list:
                if r.no in my_nos:
                    _txt = _html.unescape(r.comment_text or "")
                    my_res_lines[r.no] = [ln.strip() for ln in _txt.splitlines() if ln.strip()]

            for r in new_res:
                if r.no in my_nos:
                    continue

                ct_raw = r.comment_text or ""
                ct = _html.unescape(ct_raw)
                hit_nos = set()

                # ① 数字引用: 行頭 >数字 / >No.数字
                # 引用は必ず過去のレス宛て（n < r.no）。開き直し等で先行レスが
                # 新着扱いになった時、後から書いた自分のレスへの誤判定を防ぐ。
                for m in _re.findall(r'^>+(?:No\.)?(\d+)\s*$', ct, _re.MULTILINE):
                    n = int(m)
                    if n in my_nos and n < r.no:
                        hit_nos.add(n)

                # ② テキスト/画像/ID引用: comment_html の <font color="#789922"> を解析
                if not hit_nos and r.comment_html:
                    soup = _BS(r.comment_html, "html.parser")
                    # 自分レスの画像ファイル名マップ: fname.lower() -> no
                    my_fnames = {r2.image_name.lower(): r2.no
                                 for r2 in thread.res_list
                                 if r2.no in my_nos and r2.image_name}
                    # 自分レスのIDマップ: id_str -> [no...]（同一IDの自分レスは複数あり得る）
                    my_ids: dict = {}
                    for r2 in thread.res_list:
                        if r2.no in my_nos and r2.id_str:
                            my_ids.setdefault(r2.id_str, []).append(r2.no)
                    for font in soup.find_all("font", color="#789922"):
                        for _raw in font.get_text("\n").split("\n"):
                            q = _raw.strip()
                            if not q.startswith(">"):
                                continue
                            # 引用先頭の > は1つだけ外す。
                            #   >X  … 元発言（Xを書いた人）宛て
                            #   >>X … 引用行 >X を持つレス（=自分が >X と引用した側）宛て
                            # 全ての > を剥がすと >X と >>X が同一になり、他人宛ての引用が
                            # 自分の引用行に部分一致して誤って自分宛て判定になっていた。
                            content = q[1:].strip()
                            # 短文引用も通知対象にする（誤検知より取りこぼし回避を優先）。
                            # ただし空文字だけは除外する（"" は全レスに部分一致し、
                            # すべての返信が誤通知になるため）。
                            if not content:
                                continue
                            cl = content.lower()
                            # ※以下すべて: 引用は必ず過去のレス宛て（自分レス番号 < r.no）。
                            #   先に書かれたレスが後の自分のレスを引用したと誤判定しない。
                            # 画像引用: >タイムスタンプ.拡張子 形式
                            if _re.match(r'\d{10,}\.(jpe?g|png|gif|webp|bmp|mp4|webm)$', cl):
                                _n = my_fnames.get(cl)
                                if _n is not None and _n < r.no:
                                    hit_nos.add(_n)
                                continue
                            # ID引用: >ID:xxxxxxxx 形式（自分レスのIDと完全一致なら自分宛て）。
                            # IDは大小文字を区別するため content をそのまま使う。
                            _idm = _re.match(r'ID:(\S+)$', content)
                            if _idm:
                                for _n in my_ids.get(_idm.group(1), []):
                                    if _n < r.no:
                                        hit_nos.add(_n)
                                continue
                            # テキスト引用: 自分レスの「行」と照合。
                            #   ・どの行とも完全一致なら自分宛て（引用行 >X への >>X 返信を含む）。
                            #   ・地の文（>で始まらない行）に限り部分一致も許容（部分引用対策）。
                            for no, lines in my_res_lines.items():
                                if no >= r.no:
                                    continue
                                for ln in lines:
                                    if content == ln or (not ln.startswith(">") and content in ln):
                                        hit_nos.add(no)
                                        break

                if hit_nos:
                    cmt = _re.sub(r"<[^>]+>", "", ct).strip()[:80]
                    target_nos = ", ".join(f"No.{n}" for n in sorted(hit_nos))
                    msg = f"No.{r.no} → {target_nos} への返信\n{cmt}"
                    self._show_self_res_popup(msg, "reply")

    def _show_self_res_popup(self, msg: str, kind: str):
        """そうだね/返信ポップアップ。別のスレを見ている間（このタブが非アクティブ）は
        表示せず保留し、このスレをアクティブにした時(showEvent)にまとめて表示する。"""
        if not self.isVisible():
            q = self._pending_self_res_popups
            q.append((msg, kind))
            if len(q) > 20:        # 肥大防止: 直近のみ保持
                del q[:-20]
            return
        self._show_self_res_popup_now(msg, kind)

    def _show_self_res_popup_now(self, msg: str, kind: str):
        """右上に点滅なしポップアップを表示する。kind: 'sodane'=青, 'reply'=赤"""
        s = self._settings
        duration = (getattr(s, "self_res_sodane_duration", 5000)
                    if kind == "sodane"
                    else getattr(s, "self_res_reply_duration", 5000))

        # ユーザーCSSからスタイルを読み取る
        css_key = ".my-sodane-popup" if kind == "sodane" else ".my-reply-popup"
        c = self._parse_self_res_popup_css(css_key)
        style = (
            f"QDialog{{background:{c['background']};border:2px solid {c['border-color']};"
            f"border-radius:6px;}}"
            f"QLabel{{color:{c['color']};font-size:12px;font-weight:bold;}}"
        )

        dlg = QDialog(self, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        dlg.setModal(False)
        lbl = QLabel(msg, dlg)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lbl.setContentsMargins(14, 10, 14, 10)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(lbl)
        dlg.setStyleSheet(style)
        dlg.adjustSize()

        # 右上配置（複数同時表示でずらす）
        if not hasattr(self, "_self_res_popups"):
            self._self_res_popups = []
        # 閉じた分を除去
        self._self_res_popups = [p for p in self._self_res_popups if p.isVisible()]

        # ThreadView ウィジェット内の右上をグローバル座標で算出
        view_top_right = self.mapToGlobal(self.rect().topRight())
        margin = 8
        x = view_top_right.x() - dlg.width() - margin - 11
        y = view_top_right.y() + margin + 29
        for p in self._self_res_popups:
            y = max(y, p.geometry().bottom() + 4)
        dlg.move(x, y)
        dlg.show()
        self._self_res_popups.append(dlg)

        dlg.mousePressEvent = lambda _e: dlg.close()
        QTimer.singleShot(duration, dlg.close)

    def _parse_self_res_popup_css(self, selector: str) -> dict:
        """ユーザーCSSから指定セレクタのbackground/border-color/colorを取得する"""
        if selector == ".my-sodane-popup":
            defaults = {"background": "#D6E8F7", "border-color": "#1a6fd4", "color": "#1a4a8a"}
        else:
            defaults = {"background": "#F7D6D6", "border-color": "#cc1105", "color": "#7B0004"}
        try:
            css_file = getattr(self._settings, "user_css_file", "")
            if not css_file:
                return defaults
            from pathlib import Path as _Path
            import sys as _sys, re as _re
            p = _Path(css_file)
            if not p.is_absolute():
                p = _Path(_sys.argv[0]).parent / p
            if not p.exists():
                return defaults
            css = p.read_text(encoding="utf-8")
            esc_sel = selector.replace(".", r"\.")
            m = _re.search(rf'{esc_sel}\s*\{{([^}}]*)\}}', css, _re.DOTALL)
            if not m:
                return defaults
            result = dict(defaults)
            for prop, val in _re.findall(r'([\w-]+)\s*:\s*([^;]+)', m.group(1)):
                k = prop.strip().lower()
                if k in result:
                    result[k] = val.strip()
            return result
        except Exception:
            return defaults

    def showEvent(self, event):
        """このスレタブがアクティブ化されたら、保留していたそうだね/返信通知を表示する。"""
        super().showEvent(event)
        q = self._pending_self_res_popups
        if q:
            pending = q[:]
            q.clear()
            # レイアウト確定後に右上へ正しく配置するため少し遅延させる
            QTimer.singleShot(60, lambda items=pending: self._flush_self_res_popups(items))

    def _flush_self_res_popups(self, items: list):
        for i, (msg, kind) in enumerate(items):
            if not self.isVisible():
                # 表示中に再び別タブへ移った場合は残りを先頭へ戻して打ち切る
                self._pending_self_res_popups[:0] = items[i:]
                break
            self._show_self_res_popup_now(msg, kind)

    def _notify_ng_word_match(self, res_list: list):
        """新着レスの中に notify=True な NGワードにマッチするものがあれば通知する。
        効果音: theme/ng_se.wav を再生。棒読みちゃん: ng_word_notify_bouyomi_format で送信。"""
        if not res_list:
            return
        s = self._settings
        # notify 付き NGワードを収集
        notify_words = [w for w in getattr(s, "ng_words", [])
                        if w.get("notify") and w.get("enabled", True)
                        and w.get("ng_type", "ng") == "ng"]
        if not notify_words:
            return

        import re as _re
        matched_res = []
        matched_words = []
        for res in res_list:
            body = res.comment_text or ""
            name = res.name or ""
            for w in notify_words:
                pat = w.get("pattern", "").strip()
                if not pat:
                    continue
                targets = []
                if w.get("scope_body", True):  targets.append(body)
                if w.get("scope_name", False): targets.append(name)
                try:
                    hit = any(_re.search(pat, t) for t in targets if t)
                except Exception:
                    hit = any(pat.lower() in t.lower() for t in targets if t)
                if hit:
                    matched_res.append(res)
                    matched_words.append(w)
                    break  # 1レスで複数ワードにマッチしても1回だけ

        if not matched_res:
            return

        # 通知種別ごとに実行
        sound_needed  = any(w.get("notify_type", "sound") == "sound"  for w in matched_words)
        bouyomi_needed = any(w.get("notify_type", "sound") == "bouyomi" for w in matched_words)

        if sound_needed:
            _play_ng_se()

        if bouyomi_needed:
            fmt = getattr(s, "ng_word_notify_bouyomi_format",
                          "{board} {word}: {comment}")
            p = self.parent()
            while p and not hasattr(p, "_ar_mgr"):
                p = p.parent()
            ar_mgr = getattr(p, "_ar_mgr", None) if p else None
            if ar_mgr:
                board_name = getattr(getattr(self, "_board", None), "name", "") or ""
                speak_res = []
                for res, w in zip(matched_res, matched_words):
                    if w.get("notify_type", "sound") != "bouyomi":
                        continue
                    word_pat = w.get("pattern", "")
                    word1_pat = word_pat.split("|")[0].strip()
                    comment = (res.comment_text or "")[:50]
                    text = fmt.format(
                        board=board_name,
                        word=word_pat,
                        word1=word1_pat,
                        comment=comment,
                        name=res.name or "",
                    )
                    # ダミーResDataに読ませる文字列を入れて _speak_bouyomi を流用
                    class _FakeRes:
                        pass
                    fr = _FakeRes()
                    fr.name = ""; fr.email = ""; fr.comment_text = text; fr.no = 0
                    speak_res.append(fr)
                if speak_res:
                    ar_mgr._speak_bouyomi(speak_res)

    def _maybe_speak_bouyomi(self, res_list: list):
        """手動更新・スクロール更新後に棒読みちゃんで読み上げる。
        _chk_ar_bouyomi が ON かつ res_list が非空の場合のみ送信。"""
        if not res_list:
            return
        if not getattr(self, "_chk_ar_bouyomi", None):
            return
        if not self._chk_ar_bouyomi.isChecked():
            return
        # _ar_mgr._speak_bouyomi を使う（設定読み取りロジック共通化）
        p = self.parent()
        while p and not hasattr(p, "_ar_mgr"):
            p = p.parent()
        ar_mgr = getattr(p, "_ar_mgr", None) if p else None
        if ar_mgr:
            ar_mgr._speak_bouyomi(res_list)

    def request_manual_reload(self):
        """更新ボタン/スクロール更新の共通入口（リーディングエッジ＋1秒クールダウン）。
        最初の要求で即更新し、その後1000msは追加発火を抑制する。これにより
        更新ボタン連打・連続スクロールのどちらでもフルGETは最短1秒間隔に抑えられ、
        新着なしトーストの初動も最速になる。"""
        t = getattr(self, '_reload_cooldown', None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(1000)   # クールダウン: 即更新後この間は追加発火を無視
            self._reload_cooldown = t
        if t.isActive():
            return  # クールダウン中はスキップ（連打・連続スクロールを1秒に1回へ）
        self._manual_reload = True  # 新着なし時の下中央トースト用
        self.reload_thread()        # 即更新（リーディングエッジ）
        t.start()                   # 1000msクールダウン開始

    def _schedule_scroll_reload(self):
        """末尾/先頭スクロール検知時のリロード（共通入口へ委譲）。"""
        self.request_manual_reload()

    def _on_scroll_bottom(self):
        """末尾スクロール検知 → 1秒後にスレッドを更新（デバウンス）"""
        self._schedule_scroll_reload()

    def _on_scroll_top(self):
        """先頭スクロール検知 → 1秒後にスレッドを更新（デバウンス）"""
        self._schedule_scroll_reload()

    def _on_scroll_count(self, remaining: int):
        """末尾スクロール残回数をシグナルで上位へ通知"""
        self.scroll_count_updated.emit(remaining)

    def apply_scroll_count_setting(self):
        """設定変更後に現在表示中のページのスクロール末尾/先頭カウントを即時反映"""
        n = int(getattr(self._settings, 'scroll_bottom_count', 30))
        tn = int(getattr(self._settings, 'scroll_top_count', 0))
        self._view.page().runJavaScript(
            f"if(typeof window._scrollBottomSetCount==='function')"
            f"  window._scrollBottomSetCount({n});"
            f"if(typeof window._scrollTopSetCount==='function')"
            f"  window._scrollTopSetCount({tn});"
        )

    def _focus_search(self):
        def _apply(sel):
            if sel:
                self._search_edit.setText(sel)
            self._search_edit.setFocus()
            self._search_edit.selectAll()
        self._view.page().runJavaScript(
            "window.getSelection().toString()", _apply)


    # ── MHT保存 ──────────────────────────────────────────────────────────────
    def save_as_mht(self):
        """スレッドを MHTML ファイルとして保存（MainWindow の _do_save_mht に委譲）"""
        if not self._thread:
            QMessageBox.information(self, "MHT保存", "先にスレッドを開いてください"); return
        p = self
        while p and not hasattr(p, '_save_mht_view'):
            p = p.parent()
        if p:
            p._save_mht_view(self, self._thread)

    def save_as_html(self):
        """スレッドを HTML ファイルとして保存（MainWindow の _do_save_html に委譲）"""
        if not self._thread:
            QMessageBox.information(self, "HTML保存", "先にスレッドを開いてください"); return
        p = self
        while p and not hasattr(p, '_save_html_view'):
            p = p.parent()
        if p:
            p._save_html_view(self, self._thread)

    def save_as_zip(self):
        """スレッドを ZIP ファイルとして保存（MainWindow の _do_save_zip に委譲）"""
        if not self._thread:
            QMessageBox.information(self, "ZIP保存", "先にスレッドを開いてください"); return
        p = self
        while p and not hasattr(p, '_save_zip_view'):
            p = p.parent()
        if p:
            p._save_zip_view(self, self._thread)

    # ── スクリーンショット ────────────────────────────────────────────────────

    def _nosave_res_nos(self) -> set:
        """画像ウインドウ/タブの表示対象から外すレスNo（NG対象）。
        NGワード/NG画像マッチ or 手動NG登録(ng_hidden_res_nos = フッタNG・del登録)。
        NG解除中(_ng_enabled=False)はNGレスも表示対象に含めるため空集合を返す。"""
        if not self._thread or not self._ng_enabled:
            return set()
        turl = self._thread.url or ""
        out = set(self._settings.ng_hidden_res_nos.get(turl, []))
        _ng = self._settings.ng_filter
        if _ng is not None:
            for r in self._thread.res_list:
                if r.no in out:
                    continue
                try:
                    if _ng.is_ng(r) or (r.image_url and _ng.is_ng_image(r)):
                        out.add(r.no)
                except Exception:
                    pass
        return out

    def _filter_img_list_for_tab(self, lst: list, url: str):
        """img_list/gallery_list から NG対象レスの画像を除き、(filtered, urlの新index) を返す。
        クリックURL自体が除外対象でも保険で残す。res_no が無い要素(うｐろだ等)は残す。"""
        nos = self._nosave_res_nos()
        if nos:
            filtered = [e for e in lst
                        if (e.get("res_no") not in nos) or (e.get("url") == url)]
        else:
            filtered = list(lst)
        for i, e in enumerate(filtered):
            if e.get("url") == url:
                return filtered, i
        return (filtered or list(lst)), 0

    def _resolve_img_list(self, url: str, idx: int):
        """クリックされた画像を解決する。img_list内にURLがあればNG対象を除いた
        リストとURLの新indexを返す。該当しなければ（うｐろだ等）単一要素リスト。"""
        lst = self._img_list or []
        if any(e.get("url") == url for e in lst):
            return self._filter_img_list_for_tab(lst, url)
        name = url.rsplit("/", 1)[-1].split("?")[0] or url
        single = [{"url": url, "name": name, "res_no": ""}]
        return single, 0

    def _quote_comment(self, no: int):
        if not self._thread:
            return
        for r in self._thread.res_list:
            if r.no == no:
                qt = "\n".join(f">{l}" for l in (r.comment_text or "").splitlines()
                               if l.strip()) + "\n"
                self.open_reply_window.emit(0, qt)
                return

    def _quote_img(self, no: int):
        """フッターの「画像」クリック → >ファイル名 を引用してリプライウインドウを開く"""
        if not self._thread:
            return
        for r in self._thread.res_list:
            if r.no == no and r.image_name:
                qt = f">{r.image_name}\n"
                self.open_reply_window.emit(no, qt)
                return

    def _send_sodane(self, no: int):
        if self._is_log:
            return   # 保存ログのオフライン表示では送信しない
        if not self._board:
            return
        def _do():
            cnt = self._fetcher.post_sodane(self._board, no)
            if cnt >= 0:
                self._sodane_signal.emit(no, cnt)
        threading.Thread(target=_do, daemon=True).start()

    def _apply_sodane_js(self, no: int, cnt: int):
        """メインスレッドで JS を実行してそうだね表示を更新"""
        js = f"if(typeof updateSodane==='function')updateSodane({no},{cnt});"
        self._view.page().runJavaScript(js)

    def _on_ng(self, no: int):
        if not self._thread:
            return
        thread_url = self._thread.url or ""
        # ng_hidden_res_nos に追加して永続保存
        if thread_url:
            nos = self._settings.ng_hidden_res_nos.setdefault(thread_url, [])
            if no not in nos:
                nos.append(no)
                self._settings.save()
        # JSでそのレスを即時非表示
        self._view.page().runJavaScript(
            f'(function(){{'
            f'  var el = document.getElementById("r{no}");'
            f'  if (el) el.style.display = "none";'
            f'}})()'
        )

    # ── del ──────────────────────────────────────────────────────────────
    def _update_deleted_res_dom(self, thread, newly_deleted: list, _ng, _ul, _hidden_nos, _del_nos=None, _ng_reveal=False):
        """削除されたレスのDIVをページリロードなしでDOM書き換え"""
        import json as _json
        from futaba2b_html import render_res
        if _del_nos is None:
            _del_nos = set(self._settings.del_res_nos.get(thread.url or "", []))
        res_map = {r.no: r for r in thread.res_list}
        patches = []
        for no in newly_deleted:
            res = res_map.get(no)
            if res is None:
                continue
            html = render_res(res, False, [],
                              uploaders=_ul, ng_filter=_ng,
                              ng_settings=self._settings,
                              hidden_nos=_hidden_nos, del_nos=_del_nos,
                              ng_reveal=_ng_reveal)
            html = html.strip()
            patches.append((no, html))
        if not patches:
            return
        # JS: 各レスのDIVをouterHTMLで置き換え
        js_parts = []
        for no, html in patches:
            js_parts.append(
                f"(function(){{"
                f"var el=document.getElementById('r{no}');"
                f"if(el){{var tmp=document.createElement('div');"
                f"tmp.innerHTML={_json.dumps(html, ensure_ascii=False)};"
                f"el.parentNode.replaceChild(tmp.firstChild,el);}}"
                f"}})();"
            )
        js_parts.append(self._expiry_banner_sync_js(thread))
        self._view.page().runJavaScript("\n".join(js_parts))

    def _on_del(self, no: int):
        pass  # JS側ポップアップで処理（reportDel/deleteRes経由）

    def _on_report_del_with_hide(self, no: int, hide: bool):
        """削除依頼 + hideが立っていたらNGと同様に非表示化"""
        board = self._board
        turl  = (self._thread.url if self._thread else "") or ""
        self._pending_del_no = no
        self._pending_del_onlyimg = False
        # 削除依頼はサーバ削除しないため、非表示は「非表示にする」チェック時のみ。
        self._pending_del_should_hide = bool(hide)
        # チェック状態を次回デフォルトとして記憶
        self._settings.del_hide_checked = bool(hide)
        self._mark_del_res(no)
        if hide:
            self._hide_res_after_del(no)
        self._settings.save()
        def _do():
            ok2, msg = self._fetcher.report_del(board, no, thread_url=turl)
            self._del_result.emit(ok2, msg)
        threading.Thread(target=_do, daemon=True).start()

    def _on_delete_res_with_hide(self, no: int, pwd: str, onlyimg: bool, hide: bool):
        """記事削除 + hideが立っていたらNGと同様に非表示化。
        「画像だけ」(onlyimg)はサーバ上でレス本体が残るため、レスを非表示にせず
        del済マークも付けない（成功時に画像部分のみDOMから取り除く）。"""
        board = self._board
        turl  = (self._thread.url if self._thread else "") or ""
        self._pending_del_no = no
        self._pending_del_onlyimg = bool(onlyimg)
        # 記事削除(レス全体)は実際にサーバ削除されるため常に非表示にする。
        # 画像だけ削除はレスが残るので非表示にしない。
        self._pending_del_should_hide = not onlyimg
        # チェック状態を次回デフォルトとして記憶
        self._settings.del_hide_checked = bool(hide)
        if not onlyimg:
            self._mark_del_res(no)
            if hide:
                self._hide_res_after_del(no)
        self._settings.save()
        def _do():
            ok2, msg = self._fetcher.delete_res(board, no, pwd, onlyimg, thread_url=turl)
            self._del_result.emit(ok2, msg)
        threading.Thread(target=_do, daemon=True).start()

    def _mark_del_res(self, no: int):
        """delしたレスNoを del_res_nos に記録する（No.右の「del済」赤表示用）。"""
        thread_url = (self._thread.url if self._thread else "") or ""
        if not thread_url:
            return
        lst = self._settings.del_res_nos.setdefault(thread_url, [])
        if no not in lst:
            lst.append(no)

    def _hide_res_after_del(self, no: int):
        """削除後にNGと同様にレスを非表示化（設定保存のみ、reload_threadは呼び出し元任せ）"""
        thread_url = (self._thread.url if self._thread else "") or ""
        if not thread_url:
            return
        nos = self._settings.ng_hidden_res_nos.setdefault(thread_url, [])
        if no not in nos:
            nos.append(no)
            self._settings.save()

    def _on_del_result(self, ok: bool, msg: str):
        if ok:
            # 成功: 削除したレスを即座にDOM非表示
            no = getattr(self, "_pending_del_no", None)
            _msg_txt = "登録しました"
            if no is not None and getattr(self, "_pending_del_onlyimg", False):
                # 画像だけ削除: レス本体は残し、画像部分のみ取り除く。
                #   返信モード: .thumb(サムネ)＋.file-info(ファイル名行)
                #   画像モード: ギャラリーセル .gi[data-res-no]（隠しプール内のレスも上記で処理）
                #   引用モード: ツリー行のサムネ img.qt-thumb（No.アンカーから行を特定）
                self._view.page().runJavaScript(
                    f'(function(){{var el=document.getElementById("r{no}");'
                    f'if(el)el.querySelectorAll(".thumb,.file-info")'
                    f'.forEach(function(n){{n.remove();}});'
                    f'document.querySelectorAll(\'.gi[data-res-no="{no}"]\')'
                    f'.forEach(function(n){{n.remove();}});'
                    f'document.querySelectorAll(\'a.qt-no[href="#r{no}"]\')'
                    f'.forEach(function(a){{var row=a.closest(".qt-row");'
                    f'if(row)row.querySelectorAll("img.qt-thumb")'
                    f'.forEach(function(n){{n.remove();}});}});}})();'
                )
                # モデルも画像なしへ更新（モード切替・_rebuild_last_html での復活防止）。
                # _img_list は他レスの openImg インデックスがずれるため触らない。
                if self._thread:
                    for _r in self._thread.res_list:
                        if _r.no == no:
                            _r.image_url = ""; _r.thumb_url = ""
                            _r.image_name = ""; _r.file_size_bytes = 0
                            break
                    self._last_html_dirty = True
                self._respool_cache.pop(no, None)  # ポップアップ用キャッシュも画像なしで再生成させる
                _msg_txt = "画像を削除しました"
            elif no is not None and getattr(self, "_pending_del_should_hide", False):
                self._view.page().runJavaScript(
                    f'(function(){{var el=document.getElementById("r{no}");'
                    f'if(el)el.classList.add("deleted");}})();'
                )
            safe = _msg_txt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            self._view.page().runJavaScript(f'showDelMsg("{safe}")')
            self._refresh_deleted_res_dom()  # 即時実行（2100ms遅延廃止）
        else:
            # 失敗: サーバーエラーメッセージをダイアログで表示
            err = msg if msg else "削除に失敗しました"
            QMessageBox.warning(self, "削除エラー", err)

    def _refresh_deleted_res_dom(self):
        """削除済みレスのDOMをページリロードなしで書き換える"""
        if not self._board or not self._thread_no:
            return
        import threading as _thr
        board = self._board; no = self._thread_no
        def _do():
            from futaba2b_network import FutabaFetcher as _FF
            thread = self._fetcher.fetch_thread(board, no)
            if thread:
                self._thread_ready.emit(thread)
        _thr.Thread(target=_do, daemon=True).start()

    def eventFilter(self, obj, event):
        """WebEngineView のD&Dイベントを自身の dragEnter/dropEvent に転送する"""
        from PySide6.QtCore import QEvent
        if obj is self._view:
            if event.type() == QEvent.Type.DragEnter:
                self.dragEnterEvent(event)
                return True
            if event.type() == QEvent.Type.Drop:
                self.dropEvent(event)
                return True
        return super().eventFilter(obj, event)

    # ── D&D ログファイルを開く ───────────────────────────────────────────────
    def dragEnterEvent(self, event):
        """ログファイル（zip/mht/mhtml/html/htm）のD&Dを受け付ける"""
        import os
        urls = event.mimeData().urls()
        if any(u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower()
               in ('.zip', '.mht', '.mhtml', '.html', '.htm')
               for u in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """ドロップされたログファイルをMainWindowに転送して開く"""
        import os
        p = self
        while p and not hasattr(p, '_open_log_file'):
            p = p.parent()
        if not p:
            event.ignore()
            return
        for url in event.mimeData().urls():
            if url.isLocalFile():
                p._open_log_file(url.toLocalFile())
        event.acceptProposedAction()

    def cleanup(self):
        """タブが閉じられる時のWebEngineリソース解放。
        QWebEngineProfile は、それを使う QWebEnginePage が完全に破棄された後でないと
        「Release of profile requested but WebEnginePage still not deleted」警告＋
        ネイティブクラッシュ(0xC0000005)を起こす。固定タイマーではタブ高速開閉時に
        page削除が間に合わず競合するため、page の destroyed シグナルに連動して
        profile を削除し、順序を確実に保証する。また widget 破棄カスケード
        （self→profile→page）との二重削除を避けるため親子関係を切っておく。"""
        # このスレの未着手の先読みDLを中断（閉じたスレの画像を貯め続けないため）
        try:
            _turl = ""
            if getattr(self, '_thread', None) is not None:
                _turl = self._thread.url or ""
            if not _turl and self._board is not None and self._thread_no:
                _turl = self._board.base_url + f"res/{self._thread_no}.htm"
            if _turl:
                self._fetcher.cancel_prefetch(_turl)
        except Exception:
            pass

        # 一時HTMLファイルを削除
        if getattr(self, '_tmp_html_path', ''):
            _cleanup_tmp(self._tmp_html_path)
            self._tmp_html_path = ''

        # Python側の大きな保持データを即解放する。
        # レス数が多いスレほど res_list / _last_html が巨大で、これらを抱えたまま
        # deleteLater→後続の遅延 gc.collect() を迎えると、巨大ヒープ全体の走査で
        # GUIスレッドが数百ms以上止まる（閉じた直後に時々フリーズ）。先に参照を切れば
        # 大半は参照カウントで即解放され、gc の走査対象も減ってフリーズしにくくなる。
        # （tab_closing → _on_tab_closing 等の読み取りは removeTab 前に済むため安全）
        self._thread = None
        self._last_valid_thread = None
        self._last_html = ""
        self._error_banner_html = ""
        self._img_list = []
        self._respool_cache = {}
        self._pending_self_res_popups = []
        self._pending_frags = []
        self._my_sodane_cache = {}

        _page   = getattr(self, '_page', None)
        _prof   = getattr(self, '_profile', None)
        _chan   = getattr(self, '_channel', None)
        _bridge = getattr(self, '_bridge', None)
        self._page = self._profile = self._channel = self._bridge = None

        # WebEngineView を空ページに差し替えて旧pageを完全に切り離す
        try:
            if getattr(self, '_view', None) is not None:
                blank = QWebEnginePage(self)
                self._view.setPage(blank)
        except Exception:
            pass

        # 親子関係を断ち切り、削除経路を一本化する（widget破棄カスケードとの二重削除回避）
        try:
            if _prof is not None: _prof.setParent(None)
            if _page is not None: _page.setParent(None)
        except Exception:
            pass

        # webChannel を切り離してから channel / bridge を削除
        try:
            if _page is not None:
                _page.setWebChannel(None)
        except Exception:
            pass
        for obj in (_chan, _bridge):
            if obj is not None:
                try:
                    obj.deleteLater()
                except Exception:
                    pass

        # ── 重いDOM(1000レス等)を抱えたpageの破棄でGUIが数秒固まるのを防ぐ ──
        # page をそのまま deleteLater すると、Chromium の WebContents/DOM 解体が
        # GUIスレッド上で同期実行され、レス数が多いスレでは数秒フリーズする。
        # 先に about:blank へ遷移させて巨大DOMをレンダラ側で解放させ、その完了後
        # （保険で最大1.5s後）に page を破棄すると、破棄時のDOMが空同然になり軽い。
        # profile は従来どおり page 破棄(destroyed)後さらに3秒待って解放し、
        # "Release of profile ..." 警告と早期解放クラッシュを回避する。
        if _page is not None:
            _torn = {"done": False}
            def _teardown_page(*_a, _p=_page, _pr=_prof, _flag=_torn):
                if _flag["done"]:
                    return
                _flag["done"] = True
                if _pr is not None:
                    try:
                        _p.destroyed.connect(
                            lambda *a, p=_pr: QTimer.singleShot(3000, p.deleteLater))
                    except Exception:
                        QTimer.singleShot(3000, lambda p=_pr: p.deleteLater())
                try:
                    _p.deleteLater()
                except Exception:
                    pass
            try:
                _page.loadFinished.connect(lambda _ok, f=_teardown_page: f())
                _page.setUrl(QUrl("about:blank"))
                # loadFinished が来ない場合の保険
                QTimer.singleShot(1500, _teardown_page)
            except Exception:
                _teardown_page()
        elif _prof is not None:
            try:
                _prof.deleteLater()
            except Exception:
                pass


def _make_scroll_bottom_js(scroll_bottom_count: int = 5, scroll_top_count: int = 0) -> str:
    """スクロール末尾/先頭検知JSをscript要素として返す（画像・引用モード用）。
    ロジックは futaba2b_html._make_scroll_bottom_js に一本化し、<script>で包むだけ。"""
    from futaba2b_html import _make_scroll_bottom_js as _mk
    return f'<script>{_mk(scroll_bottom_count, scroll_top_count)}</script>'


# ── ページ内検索バー（ThreadView・CatalogView 共用） ──────────────────────
class _FindBar(QWidget):
    """下部に表示するページ内検索バー。
    Ctrl+F で show_and_focus()、Esc / ×ボタンで非表示。
    """
    def __init__(self, page_getter, parent=None):
        """page_getter: 呼び出し時点の QWebEnginePage を返す callable"""
        super().__init__(parent)
        self._page_getter = page_getter
        self._last_query  = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(4)

        lbl = QLabel("検索:")
        lbl.setFixedWidth(36)
        lay.addWidget(lbl)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("ページ内検索 (Ctrl+F)")
        self._edit.setFixedHeight(22)
        self._edit.textChanged.connect(self._on_text_changed)
        self._edit.returnPressed.connect(lambda: self._find(forward=True))
        lay.addWidget(self._edit, 1)

        self._lbl_count = QLabel("")
        self._lbl_count.setFixedWidth(60)
        lay.addWidget(self._lbl_count)

        btn_prev = QPushButton("▲")
        btn_prev.setFixedSize(24, 22)
        btn_prev.setToolTip("前を検索")
        btn_prev.clicked.connect(lambda: self._find(forward=False))
        lay.addWidget(btn_prev)

        btn_next = QPushButton("▼")
        btn_next.setFixedSize(24, 22)
        btn_next.setToolTip("次を検索")
        btn_next.clicked.connect(lambda: self._find(forward=True))
        lay.addWidget(btn_next)

        self._chk_wrap = QCheckBox("先頭（末尾）から再検索")
        self._chk_wrap.setChecked(True)
        lay.addWidget(self._chk_wrap)

        self._chk_regex = QCheckBox("正規表現")
        self._chk_regex.stateChanged.connect(lambda _: self._on_text_changed(self._edit.text()))
        lay.addWidget(self._chk_regex)

        btn_close = QPushButton("×")
        btn_close.setFixedSize(22, 22)
        btn_close.clicked.connect(self.hide_bar)
        lay.addWidget(btn_close)

        self.setVisible(False)
        self.setFixedHeight(28)

    def show_and_focus(self, text: str = ""):
        self.setVisible(True)
        if text:
            self._edit.setText(text)
        self._edit.setFocus()
        self._edit.selectAll()

    def hide_bar(self):
        self.setVisible(False)
        self._clear_highlights()
        self._lbl_count.setText("")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide_bar()
        else:
            super().keyPressEvent(e)

    def _on_text_changed(self, text):
        if not text:
            self._clear_highlights()
            self._lbl_count.setText("")
            return
        self._find(forward=True, new_query=True)

    def _find(self, forward: bool = True, new_query: bool = False):
        query = self._edit.text().strip()
        if not query:
            return
        page = self._page_getter()
        if page is None:
            return
        use_regex = self._chk_regex.isChecked()
        wrap      = self._chk_wrap.isChecked()
        if use_regex:
            # 正規表現: JS側でハイライト＋カウント、QWebEnginePage.findText は使わない
            self._find_regex(page, query, forward, new_query, wrap)
        else:
            # 通常検索: QWebEnginePage.findText
            flags = QWebEnginePage.FindFlag(0)
            if not forward:
                flags |= QWebEnginePage.FindFlag.FindBackward
            if wrap:
                # PySide6のfindTextはデフォルトでwrap動作するため特別なフラグ不要
                pass
            page.findText(query, flags, self._on_find_result)

    def _on_find_result(self, result):
        """findText コールバック: PySide6 では QWebEngineFindTextResult オブジェクト"""
        try:
            cur  = result.activeMatch()
            tot  = result.numberOfMatches()
            if tot == 0:
                self._lbl_count.setStyleSheet("color: #cc0000;")
                self._lbl_count.setText("見つかりません")
            else:
                self._lbl_count.setStyleSheet("")
                self._lbl_count.setText(f"{cur}/{tot}")
        except Exception:
            pass

    def _find_regex(self, page, pattern: str, forward: bool, new_query: bool, wrap: bool):
        """正規表現検索: JSでハイライト + 現在位置管理"""
        import json as _json
        import re as _re
        # 正規表現バリデーション
        try:
            _re.compile(pattern)
        except _re.error:
            self._lbl_count.setStyleSheet("color: #cc0000;")
            self._lbl_count.setText("正規表現エラー")
            return
        direction = "true" if forward else "false"
        wrap_js   = "true" if wrap else "false"
        pat_json  = _json.dumps(pattern)
        js = f"""
(function() {{
    var pat;
    try {{ pat = new RegExp({pat_json}, 'i'); }} catch(e) {{ return JSON.stringify({{count:0,cur:0}}); }}
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var nodes = [], node;
    while (node = walker.nextNode()) {{
        if (node.parentNode && ['SCRIPT','STYLE','NOSCRIPT'].indexOf(node.parentNode.tagName) >= 0) continue;
        if (pat.test(node.nodeValue)) nodes.push(node);
    }}
    var count = nodes.length;
    if (count === 0) return JSON.stringify({{count:0, cur:0}});
    var fwd = {direction};
    var wrap = {wrap_js};
    // インデックス管理: __rgxIdx を body に付ける
    var body = document.body;
    if (typeof body.__rgxIdx === 'undefined' || {str(new_query).lower()} || body.__rgxPat !== {pat_json}) {{
        body.__rgxIdx = fwd ? 0 : count - 1;
        body.__rgxPat = {pat_json};
    }} else {{
        if (fwd) {{
            body.__rgxIdx++;
            if (body.__rgxIdx >= count) body.__rgxIdx = wrap ? 0 : count - 1;
        }} else {{
            body.__rgxIdx--;
            if (body.__rgxIdx < 0) body.__rgxIdx = wrap ? count - 1 : 0;
        }}
    }}
    var targetNode = nodes[body.__rgxIdx];
    var el = targetNode.parentNode;
    if (el && el.scrollIntoView) el.scrollIntoView({{block:'center', behavior:'smooth'}});
    return JSON.stringify({{count:count, cur:body.__rgxIdx + 1}});
}})()
"""
        page.runJavaScript(js, lambda r: self._on_regex_result(r))

    def _on_regex_result(self, result):
        # Qt6.11のQtWebEngineはオブジェクトの戻り値がコールバックに空文字列で
        # 渡されるため、JS側は JSON.stringify で返しここでデコードする
        if isinstance(result, str) and result:
            try:
                import json as _json
                result = _json.loads(result)
            except Exception:
                result = None
        if not isinstance(result, dict) or result.get("count", 0) == 0:
            self._lbl_count.setStyleSheet("color: #cc0000;")
            self._lbl_count.setText("見つかりません")
        else:
            self._lbl_count.setStyleSheet("")
            self._lbl_count.setText(f"{result['cur']}/{result['count']}")

    def _clear_highlights(self):
        page = self._page_getter()
        if page:
            page.findText("")   # ハイライト解除

class CatalogView(QWidget):
    thread_open    = Signal(str)
    thread_open_bg = Signal(str)   # バックグラウンドで開く
    thread_open_mode    = Signal(str, int)  # url, open_mode
    thread_open_bg_mode = Signal(str, int)  # url, open_mode (BG)
    status_info    = Signal(object)  # ステータスバー更新用
    catalog_new_arrivals = Signal(object)  # カタログ更新時 +1以上の新着があったスレURL集合
    auto_refresh_requested = Signal()  # 自動更新ダイアログを開く要求
    _board_info_ready = Signal()   # board情報バックグラウンド取得完了
    _email_data_ready = Signal(object)  # board topから取得したemail情報 {no: email}
    _catalog_json_ready = Signal(object)  # mode=json取得結果 {"map":{no:{email,id}}, "nos":set} or None
    _catalog_del_result = Signal(bool, str)  # 削除依頼(del)の結果（ok, msg）
    _entries_ready = Signal(list)   # スレッド→UI の安全な橋渡し
    _hover_img_ready  = Signal(bytes, object)  # (img_data, cursor_pos) BGスレッド→UIスレッド
    error_band_changed = Signal(str)  # 通信エラー赤帯（text=詳細, ""=解除）をスレタブへ伝播
    quar_nos_changed   = Signal(object)  # 隔離スレNo集合が更新された（スレタブのオレンジ色再評価用）
    _catalog_err_sig   = Signal(str)  # BGスレッド→UI: カタログfetch失敗の詳細

    def __init__(self, fetcher: FutabaFetcher, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._fetcher       = fetcher
        self._settings      = settings
        self._board: BoardInfo | None = None
        self._all_entries: list = []
        self._tmp_html_path: str = ""
        self._cat_page_live = False   # カタログページのDOMがロード完了済みか（body入替可能か）
        self._pending_light_body: str | None = None  # ロード完了前に来たマージ再描画body（loadFinished後に適用）
        self._light_render_once = False  # _re_render_light 実行中フラグ（_renderでbody入替に切替）
        self._hovering: bool = False  # マウスがカタログエントリ上にあるか
        self._pending_catset: callable | None = None  # fetch完了後に1回だけ実行するcatset
        self._email_cache: dict = {}            # {no: email} board_top取得済みemail（二重レンダリング防止）
        self._catalog_json_cache: dict = {}     # {no: {"email","id"}} mode=json取得済み
        self._catalog_json_nos: set = set()     # mode=json に存在したスレNo集合（隔離判定用）
        self._quar_nos: set = set()             # 隔離スレNo集合（json∖cat。スレタブのオレンジ判定用）
        # 検索ボックスのビュー状態保存をデバウンス（キーストローク毎の全量設定保存を防止）
        self._view_state_save_timer = QTimer(self)
        self._view_state_save_timer.setSingleShot(True)
        self._view_state_save_timer.setInterval(500)
        self._view_state_save_timer.timeout.connect(self._save_view_state)
        self._entries_ready.connect(self._on_entries_ready)
        self._hover_img_ready.connect(self._show_hover_img)

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        # チェック時スタイル
        _CS = "QPushButton{padding:1px 5px;} QPushButton:checked{background:#800000;color:white;font-weight:bold;}"

        def _make_btn(label, group, h=22):
            b = QPushButton(label); b.setFixedHeight(h); b.setCheckable(True)
            b.setStyleSheet(_CS); group.addButton(b); return b

        tb = QToolBar(); tb.setMovable(False)
        tb.setIconSize(QSize(1, 1))
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setMaximumHeight(28)

        # ── 並び替え ─────────────────────────────────────────────────────
        tb.addWidget(QLabel(" 並び替え:"))
        self._local_sort_grp = QButtonGroup(self); self._local_sort_grp.setExclusive(True)
        for i, lbl in enumerate(["無し", "読", "50音", "勢▲", "勢▼"]):
            b = _make_btn(lbl, self._local_sort_grp)
            self._local_sort_grp.setId(b, i)
            tb.addWidget(b)
        self._local_sort_grp.buttons()[0].setChecked(True)
        self._local_sort_grp.idClicked.connect(lambda _: (self._re_render(), self._save_view_state()))


        # ── 検索 ─────────────────────────────────────────────────────────
        tb.addWidget(QLabel(" / 検索："))
        self._search = QLineEdit(); self._search.setPlaceholderText("検索")
        self._search.setFixedWidth(260)
        self._search.returnPressed.connect(self._re_render)
        self._search.textChanged.connect(lambda _: self._view_state_save_timer.start())
        tb.addWidget(self._search)
        sr = QPushButton("検索"); sr.setFixedHeight(22); sr.clicked.connect(self._re_render)
        tb.addWidget(sr)

        # ── 設定 ─────────────────────────────────────────────────────────
        tb.addWidget(QLabel(" / "))
        cfg_btn = QPushButton("板設定"); cfg_btn.setFixedWidth(52); cfg_btn.setFixedHeight(22)
        cfg_btn.clicked.connect(self._open_catalog_settings)
        tb.addWidget(cfg_btn)

        # ── 右寄せ: サーバーソート ────────────────────────────────────────
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        # カウントダウン表示ラベル（「通常」の左側）
        self._lbl_countdown = QLabel("")
        self._lbl_countdown.setFixedHeight(24)
        self._lbl_countdown.setToolTip("次の自動更新まであと何秒/分")
        self._lbl_countdown.setStyleSheet(f"font-size:8pt; color:{_TM.ui('countdown_fg','#ffffff')}; padding:0 4px;")
        tb.addWidget(self._lbl_countdown)
        self._sort_grp = QButtonGroup(self); self._sort_grp.setExclusive(True)
        for i, lbl in enumerate(["通常", "新順", "古順", "多順", "少順", "履歴"]):
            b = _make_btn(lbl, self._sort_grp)
            self._sort_grp.setId(b, i)
            tb.addWidget(b)
        self._sort_grp.buttons()[0].setChecked(True)
        self._sort_grp.idClicked.connect(self._on_server_sort_changed)
        lay.addWidget(tb)

        # off-the-record: ディスクキャッシュなし
        self._profile = QWebEngineProfile(self)
        self._profile.setHttpUserAgent(UA)
        self._profile.setUrlRequestInterceptor(Interceptor())
        # file:// 経由でロードしたHTMLからhttps://サムネイルを読み込めるようにする
        self._profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self._page    = _DebugPage(self._profile, self._profile)  # page親=profile→warning回避
        self._channel = QWebChannel(self._page)
        self._bridge  = CatalogBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._page.setWebChannel(self._channel)
        self._view = _CatalogWebView(self._page, self)
        self._view.setZoomFactor(_default_zoom())
        self._view._source_callback = self._show_catalog_source
        self._view.loadFinished.connect(self._on_cat_load_finished)
        lay.addWidget(self._view)
        self._find_bar = _FindBar(lambda: self._page, self)
        lay.addWidget(self._find_bar)
        self.setAcceptDrops(True)  # D&Dでログファイルを開けるようにする

        # スレを開く経路では、開く前に必ずホバーポップアップを閉じる。
        # スレを開いてもマウスはエントリ上に残るため onmouseleave が発火せず、
        # 非同期の画像ロード完了が「開いた後」に届くとサムネが取り残されるため。
        self._bridge.thread_open_requested.connect(self._on_cat_open_thread)
        self._bridge.thread_bg_open_requested.connect(self._on_cat_open_thread_bg)
        self._board_info_ready.connect(self._emit_catalog_status)
        self._email_data_ready.connect(self._merge_email_data)
        self._catalog_json_ready.connect(self._merge_catalog_json)
        self._bridge.url_open_requested.connect(_open_url)
        self._bridge.copy_to_clipboard_requested.connect(self._copy_to_clipboard)
        self._bridge.add_thread_ng_requested.connect(self._on_add_thread_ng)
        self._bridge.catalog_del_requested.connect(self._on_catalog_del)
        self._catalog_del_result.connect(self._on_catalog_del_result)
        self._catalog_err_sig.connect(self._on_catalog_err)
        self._bridge.scroll_bottom_reached.connect(
            lambda: QTimer.singleShot(1000, self.reload))
        self._bridge.scroll_top_reached.connect(
            lambda: QTimer.singleShot(1000, self.reload))
        self._bridge.cat_hover_enter.connect(self._on_cat_hover_enter)
        self._bridge.cat_hover_leave.connect(self._on_cat_hover_leave)

        # ホバーポップアップウィジェット
        # Qt.Tool: システム全体の最前面ではなく本ソフトの前面に表示（親=メインウインドウ）。
        # 表示時に _ensure_hover_parent で親を設定し、_clamp_to_window で窓内に収める。
        self._hover_popup = QLabel()
        self._hover_popup.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput)
        self._hover_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._hover_popup.setStyleSheet(
            f"QLabel{{background:{_TM.thread('id_popup_bg','#FFFFEE')};border:1px solid {_TM.thread('id_popup_border','#800000')};"
            "padding:4px;font-size:9pt;max-width:300px;}")
        self._hover_popup.setWordWrap(True)
        self._hover_popup.hide()
        self._hover_img_popup = QWidget()
        self._hover_img_popup.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput)
        self._hover_img_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._hover_img_popup.setStyleSheet(
            f"QWidget{{background:{_TM.thread('id_popup_bg','#FFFFEE')};border:1px solid {_TM.thread('id_popup_border','#800000')};}}"
            "QLabel{background:transparent;border:none;padding:2px;}")
        _hov_lay = QVBoxLayout(self._hover_img_popup)
        _hov_lay.setContentsMargins(2, 2, 2, 2)
        _hov_lay.setSpacing(2)
        self._hover_img_lbl = QLabel()
        self._hover_img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hover_txt_lbl = QLabel()
        self._hover_txt_lbl.setWordWrap(True)
        self._hover_txt_lbl.setStyleSheet(f"font-size:9pt;max-width:250px;color:{_TM.thread('body_fg','#800000')};")
        self._hover_txt_lbl.setMaximumWidth(250)
        _hov_lay.addWidget(self._hover_img_lbl)
        _hov_lay.addWidget(self._hover_txt_lbl)
        self._hover_img_popup.hide()

    def reload(self):
        """現在の板・ソートで再読み込み（自動更新から呼ばれる）"""
        if self._board:
            saved_ss = self._sort_grp.checkedId()
            sort = self._SERVER_SORTS[saved_ss] \
                   if 0 <= saved_ss < len(self._SERVER_SORTS) else 0
            threading.Thread(target=self._fetch, args=(self._board, sort), daemon=True).start()

    def apply_scroll_count_setting(self):
        """設定変更後にスクロール末尾/先頭カウントを即時反映"""
        n = int(getattr(self._settings, 'scroll_bottom_count', 30))
        tn = int(getattr(self._settings, 'scroll_top_count', 0))
        self._view.page().runJavaScript(
            f"if(typeof window._scrollBottomSetCount==='function')"
            f"  window._scrollBottomSetCount({n});"
            f"if(typeof window._scrollTopSetCount==='function')"
            f"  window._scrollTopSetCount({tn});"
        )

    def _ensure_hover_parent(self):
        """ホバーポップアップの親をメインウインドウに設定（本ソフト前面・非システム最前面）。"""
        mw = self.window()
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowTransparentForInput)
        for w in (self._hover_popup, self._hover_img_popup):
            if w.parent() is not mw:
                w.setParent(mw, flags)
                w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _clamp_to_window(self, gx: int, gy: int, w: int, h: int):
        """ポップアップ左上(gx,gy)・サイズ(w,h)を本ソフトのウインドウ矩形内に収める。"""
        mw = self.window()
        try:
            r = mw.frameGeometry()  # 画面座標でのウインドウ全体矩形
        except Exception:
            return gx, gy
        x = max(r.left(), min(gx, r.right()  - w))
        y = max(r.top(),  min(gy, r.bottom() - h))
        return x, y

    def _on_cat_hover_enter(self, url: str, thumb_url: str, comment: str):
        """カタログエントリへのマウスオーバー：画像拡大・本文ポップアップ"""
        s = self._settings
        zoom_on    = getattr(s, "catalog_hover_zoom",    False)
        comment_on = getattr(s, "catalog_hover_comment", False)
        self._hovering = True
        if not zoom_on and not comment_on:
            return
        from PySide6.QtGui import QCursor
        cursor_pos = QCursor.pos()
        offset_x, offset_y = 16, 16

        # _hover_txt_lbl に常にテキストをセット（zoom_on時は画像の下に表示）
        self._hover_txt_lbl.setText(comment if comment else "")
        self._hover_txt_lbl.setVisible(bool(comment_on and comment))

        if comment_on and comment and not zoom_on:
            # テキストのみ: 独立ポップアップ
            self._hover_popup.setText(comment)
            self._hover_popup.adjustSize()
            self._ensure_hover_parent()
            px, py = self._clamp_to_window(cursor_pos.x() + offset_x,
                                           cursor_pos.y() + offset_y,
                                           self._hover_popup.width(),
                                           self._hover_popup.height())
            self._hover_popup.move(px, py)
            self._hover_popup.show()
            self._hover_popup.raise_()

        if zoom_on and thumb_url:
            import threading as _th, urllib.request as _ur
            _thumb_url = thumb_url.replace("/cat/", "/thumb/")
            def _load_img():
                try:
                    with _ur.urlopen(_thumb_url, timeout=3) as resp:
                        data = resp.read()
                    self._hover_img_ready.emit(data, cursor_pos)
                except Exception:
                    pass
            _th.Thread(target=_load_img, daemon=True).start()

    def _show_hover_img(self, data: bytes, cursor_pos):
        """BGスレッドから渡された画像データをポップアップ表示（UIスレッド）"""
        from PySide6.QtGui import QPixmap
        pix = QPixmap()
        pix.loadFromData(data)
        if pix.isNull():
            return
        pix = pix.scaled(250, 250,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        if not self._hovering:
            return  # マウスが既に離れていたら表示しない
        self._hover_img_lbl.setPixmap(pix)
        # テキストは _on_cat_hover_enter で既にセット済み（_hover_txt_lbl）
        self._hover_img_popup.adjustSize()
        self._ensure_hover_parent()
        px, py = self._clamp_to_window(cursor_pos.x() + 16, cursor_pos.y() + 16,
                                       self._hover_img_popup.width(),
                                       self._hover_img_popup.height())
        self._hover_img_popup.move(px, py)
        self._hover_img_popup.show()
        self._hover_img_popup.raise_()

    def _on_cat_hover_leave(self):
        """カタログエントリからマウスアウト：ポップアップ非表示"""
        self._hovering = False
        self._hover_popup.hide()
        self._hover_img_popup.hide()
        self._hover_img_lbl.clear()
        self._hover_txt_lbl.clear()

    def _on_cat_open_thread(self, url: str):
        """カタログからスレを開く（前面）。開く前にホバーポップアップを閉じる。"""
        self._on_cat_hover_leave()
        self.thread_open.emit(url)

    def _on_cat_open_thread_bg(self, url: str):
        """カタログからスレを開く（背面）。開く前にホバーポップアップを閉じる。"""
        self._on_cat_hover_leave()
        self.thread_open_bg.emit(url)

    def _show_catalog_source(self):
        """現在のカタログ HTML ソースをウィンドウ表示（ThreadView と同形式）"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QPushButton
        def _cb(html):
            dlg = QDialog(self)
            dlg.setWindowTitle("ソース表示（カタログ）")
            dlg.resize(800, 600)
            lay = QVBoxLayout(dlg)
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText(html)
            te.setStyleSheet("font-family: monospace; font-size: 9pt;")
            lay.addWidget(te)
            btn = QPushButton("閉じる")
            btn.clicked.connect(dlg.close)
            lay.addWidget(btn)
            dlg.exec()
        self._view.page().toHtml(_cb)

    def _load_html_via_tempfile(self, html: str, base_url: QUrl):
        """HTMLを一時ファイル経由でロード（qwebchannel.jsをインライン化）"""
        import tempfile, os, time as _time
        from PySide6.QtCore import QFile, QIODevice
        _t0 = _time.perf_counter()
        _board_name = getattr(self._board, 'name', '?') if self._board else '?'
        if self._tmp_html_path:
            _old = self._tmp_html_path
            try:
                os.unlink(self._tmp_html_path)
            except OSError as _e:
                pass
        QRC_TAG = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>'
        if QRC_TAG in html:
            f = QFile(':/qtwebchannel/qwebchannel.js')
            if f.open(QIODevice.OpenModeFlag.ReadOnly):
                qwc_js = bytes(f.readAll()).decode('utf-8', errors='replace')
                f.close()
                html = html.replace(QRC_TAG, f'<script>\n{qwc_js}\n</script>', 1)
            else:
                pass
        if '<head>' in html:
            html = html.replace('<head>', f'<head><base href="{base_url.toString()}">', 1)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", encoding="utf-8", delete=False)
        tmp.write(html)
        tmp.close()
        self._tmp_html_path = tmp.name
        # フルナビゲーション開始 → 完了までbody入替不可。保留中のマージ入替は
        # このフルロードの内容（マージ済み _all_entries から生成）に含まれるため破棄。
        self._cat_page_live = False
        self._pending_light_body = None
        self._view.load(QUrl.fromLocalFile(tmp.name))

    def _on_cat_load_finished(self, ok: bool):
        """カタログページのロード完了。body入替を解禁し、ロード中に届いた
        マージ再描画（_pending_light_body）があればここで適用する。"""
        self._cat_page_live = bool(ok)
        _body = self._pending_light_body
        self._pending_light_body = None
        if ok and _body is not None:
            self._apply_catalog_body_swap(_body)

    def _apply_catalog_body_swap(self, body_inner: str):
        """カタログの body だけを差し替える（ページナビゲーションなし・スクロール位置維持）。
        head の CSS/qwebchannel/スクロールJSはそのまま残る。カタログの body には
        script要素が無くハンドラは全てインライン属性のため、innerHTML入替で機能が保たれる。
        フルリロードで発生する白フラッシュ／スクロールバー伸縮（ちらつき）を避ける。"""
        import json as _json
        body_js = _json.dumps(body_inner, ensure_ascii=False)
        js = ("(function(){var y=window.scrollY;"
              "document.body.innerHTML=" + body_js + ";"
              "window.scrollTo(0,y);})();")
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def _copy_to_clipboard(self, text: str):
        """クリップボードにテキストをコピー"""
        QGuiApplication.clipboard().setText(text)

    def _on_add_thread_ng(self, url: str):
        """右クリック→このスレをNGにする"""
        if not url:
            return
        if url not in self._settings.ng_thread_urls:
            self._settings.ng_thread_urls.append(url)
            self._settings.save()
        # カタログを再描画してNGスレを除外
        self._re_render()

    def _on_catalog_del(self, url: str):
        """右クリック→削除依頼(del): /del.php に削除依頼を送り、
        当該スレをカタログから除外する（セッション中保持）。
        結果メッセージ（例「登録しました」）をカタログ下部にトースト表示する。"""
        if not url or not self._board:
            return
        # セッション用ハイド集合に追加（再描画・自動更新でも除外を維持）
        if not hasattr(self, "_del_hidden_urls"):
            self._del_hidden_urls = set()
        self._del_hidden_urls.add(url)
        # スレ番号を URL から抽出
        m = re.search(r'res/(\d+)\.htm', url)
        if not m:
            return
        no = int(m.group(1))
        board = self._board
        fetcher = self._fetcher
        def _do():
            try:
                ok, msg = fetcher.report_del(board, no, thread_url=url)
                print(f"[CATALOG_DEL] No.{no} ok={ok} msg={msg!r}")
            except Exception as e:
                print(f"[CATALOG_DEL] error: {e}")
                ok, msg = False, str(e)
            self._catalog_del_result.emit(bool(ok), msg or "")
        threading.Thread(target=_do, daemon=True).start()

    def _inject_error_band(self, text: str):
        try:
            self._view.page().runJavaScript(_build_error_band_js(text))
        except Exception:
            pass

    def _clear_error_band(self):
        try:
            self._view.page().runJavaScript(_build_error_band_js(""))
        except Exception:
            pass

    def _on_catalog_err(self, text: str):
        """カタログfetch失敗: 自カタログへ赤帯を注入し、スレタブへも伝播。"""
        self._inject_error_band(text)
        self.error_band_changed.emit(text)

    def _on_catalog_del_result(self, ok: bool, msg: str):
        """カタログ削除依頼の結果をWebView下部にトースト表示する（レスdelと同等）"""
        text = (msg or ("登録しました" if ok else "削除依頼に失敗しました")).strip()
        safe = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        js = (
            "(function(){var el=document.getElementById('_delmsg');"
            "if(!el){el=document.createElement('div');el.id='_delmsg';"
            "el.style.cssText='display:none;position:fixed;bottom:30px;left:50%;"
            "transform:translateX(-50%);background:rgba(0,0,0,0.75);color:#fff;"
            "padding:6px 16px;border-radius:4px;z-index:99999;font-size:10pt;';"
            "document.body.appendChild(el);}"
            f'el.textContent="{safe}";el.style.display="block";'
            "setTimeout(function(){el.style.display='none';},2000);})();"
        )
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def load(self, board: BoardInfo, sort: int = -1):
        if not self._board or self._board.url != board.url:
            self._email_cache.clear()   # 板が変わったらemailキャッシュは無効
            self._catalog_json_cache.clear()
            self._catalog_json_nos = set()
        self._board = board
        self._restore_view_state()   # UI を復元 (シグナルブロック済み)
        # ビュー状態が未保存の板は、板別設定のカタログソートを初期値として適用
        if board.url not in self._settings.catalog_view_states:
            self._apply_board_default_sort(board)
        if sort < 0:  # sort 未指定 → 保存済みサーバーソートを使用
            saved_ss = self._sort_grp.checkedId()
            sort = self._SERVER_SORTS[saved_ss] \
                   if 0 <= saved_ss < len(self._SERVER_SORTS) else 0
        threading.Thread(target=self._fetch, args=(board, sort), daemon=True).start()

    def _fetch(self, board, sort):
        import time as _time
        _t0 = _time.perf_counter()
        try:
            from futaba2b_settings import get_board_settings as _gbs
            _cxyl_base = _gbs(board.base_url).catalog_cxyl_str
        except Exception:
            _cxyl_base = ""
        entries = self._fetcher.fetch_catalog(board, sort=sort, cxyl_base=_cxyl_base)
        if entries is None:
            _err = getattr(self._fetcher, "last_fetch_error", "") or "取得失敗"
            self._catalog_err_sig.emit(_err)
            return
        self._entries_ready.emit(entries)

    def _on_entries_ready(self, entries: list):
        import time as _time
        # カタログ再描画で旧DOM要素が差し替わると onmouseleave が発火せず
        # ポップアップが取り残されるため、描画前に強制的に閉じておく。
        self._on_cat_hover_leave()
        # カタログ取得成功 → 通信エラー赤帯を解除（自カタログ＋スレタブ）
        self._clear_error_band()
        self.error_band_changed.emit("")
        # 削除依頼(del)したスレはカタログから除外（セッション中保持）
        _delh = getattr(self, "_del_hidden_urls", None)
        if _delh:
            entries = [e for e in entries if (e.thread_url or "") not in _delh]
        self._all_entries = entries
        # 取得済みemail情報を初回レンダリング前に先行適用
        # （board_top取得完了後の _merge_email_data で差分なし→二重レンダリング防止）
        if self._email_cache:
            for _e_ent in entries:
                _em = self._email_cache.get(_e_ent.no, "")
                if _em and not _e_ent.email:
                    _e_ent.email = _em
        # mode=json取得済み情報を先行適用（前回json基準）: email/id補完 + 隔離スレ合成
        _prev_quar_nos = set(self._quar_nos)
        self._quar_nos = set()
        if self._catalog_json_cache:
            for _e_ent in entries:
                _ji = self._catalog_json_cache.get(_e_ent.no)
                if _ji:
                    if _ji.get("email"):
                        _e_ent.email = _ji["email"]
                    _e_ent.op_id = _ji.get("id", "")
            # 隔離スレNo集合（json∖cat）を算出。タブのオレンジ判定用で、
            # 「最下部にまとめる」表示設定(catalog_quarantine_bottom)とは独立に持つ。
            if self._catalog_json_nos:
                _real_cat_nos = {e.no for e in entries}  # この時点では実カタログのみ
                self._quar_nos = self._catalog_json_nos - _real_cat_nos
            # 隔離スレ = json にあって cat に無い（隔離されるとカタログから消えてjsonに残る）
            if (self._catalog_json_nos
                    and getattr(self._settings, 'catalog_quarantine_bottom', True)):
                _cat_nos = {e.no for e in entries}
                for _qno in sorted(self._catalog_json_nos - _cat_nos):
                    entries.append(self._make_quarantine_entry(
                        _qno, self._catalog_json_cache.get(_qno, {})))
        # エントリ数 = 現在の保存スレッド数（隔離スレは実カタログ件数に含めない）
        _real_entries = [e for e in entries if not getattr(e, 'is_quarantine', False)]
        if self._board:
            self._board.current_saved = len(_real_entries)

        # ── remaining 計算（仮赤字・隔離判定の共通基盤）────────────────
        if self._board and _real_entries:
            board_url  = self._board.base_url
            max_saved  = self._board.max_saved or 0
            # 学習した max_saved を板別に永続キャッシュ（スレ個別取得で
            # 「保存数はN件」が拾えず0になった場合のフォールバック用）
            if max_saved > 0:
                if self._settings.max_saved_by_board.get(board_url) != max_saved:
                    self._settings.max_saved_by_board[board_url] = max_saved
            global_max = self._settings.global_max_no_by_board.get(board_url, 0)
            # エントリの最大スレNo を記録（異常値が入っていれば強制リセット）
            cur_max = max((e.no for e in _real_entries), default=0)
            _RESET_THRESHOLD = 1_000_000
            if cur_max > global_max or (global_max > cur_max and (global_max - cur_max) > _RESET_THRESHOLD):
                if global_max > cur_max and (global_max - cur_max) > _RESET_THRESHOLD:
                    print(f"[SAVED] global_max異常値リセット: {global_max} → {cur_max} (差={global_max-cur_max})")
                self._settings.global_max_no_by_board[board_url] = cur_max
                global_max = cur_max
            for e in _real_entries:
                if max_saved > 0 and global_max > 0:
                    remaining = e.no + max_saved - global_max
                    # 仮赤字判定: 設定ONかつ残り10%以下（is_redでないもの）
                    if (not e.is_red
                            and getattr(self._settings, 'treat_near_limit_as_expiring', False)):
                        pct = remaining / max_saved * 100
                        # 残保存数がマイナス（上限超過＝最も落ちかけ）でも赤枠を維持する
                        # （旧 0 < pct の下限で remaining<=0 が弾かれ赤枠が消えていた）
                        e.is_quasi_red = (pct <= 10)
                    else:
                        e.is_quasi_red = False
                else:
                    e.is_quasi_red = False
        self._re_render()
        # ── カタログ更新時の新着(+N)スレURLを通知（開いているタブの色付け用）──
        #   catalog_to_html の delta 算出と同条件: 既知スレ(prev_cnt>0)かつ res増 → 新着
        try:
            _rc = self._settings.catalog_read_counts
            _new_urls = {e.thread_url for e in entries
                         if e.thread_url and _rc.get(e.thread_url, 0)
                         and e.res_count > _rc.get(e.thread_url, 0)}
            if _new_urls:
                self.catalog_new_arrivals.emit(_new_urls)
        except Exception:
            pass
        # ステータスバー更新
        self._emit_catalog_status()
        # fetch完了後にpendingのcatset POSTを実行（GETとPOSTの直列化）
        if self._pending_catset is not None:
            _cb = self._pending_catset
            self._pending_catset = None
            threading.Thread(target=_cb, daemon=True).start()
        # futaba.htm をバックグラウンドで取得（board情報 + email情報）
        if self._board:
            threading.Thread(
                target=self._fetch_board_info_bg,
                args=(self._board,), daemon=True).start()
        # 隔離スレNo集合が変化したら、開いているスレタブのオレンジ色を再評価させる
        if self._quar_nos != _prev_quar_nos:
            self.quar_nos_changed.emit(self._quar_nos)





    def _fetch_board_info_bg(self, board: BoardInfo):
        """futaba.htm からboard情報+email情報を取得するバックグラウンド処理。
        バッジ/隔離が有効なら mode=json も取得して email/id/存在Noを得る。"""
        try:
            top_entries = self._fetcher.fetch_board_top(board)
            if top_entries:
                email_map = {e.no: e.email for e in top_entries if e.email}
                if email_map:
                    self._email_data_ready.emit(email_map)
        except Exception as e:
            print(f'[CatalogView] board info fetch error: {e}')
        # mode=json（バッジ②④・隔離①のいずれかが有効なときのみ取得）
        try:
            # mode=json は バッジ / 隔離 / 共通ID表示 のいずれでも必要。共通ID(最下部表示
            # or 非表示)は ON/OFF どちらも op_id 判定に json が要るため実質常に取得する。
            _need_json = True
            if _need_json:
                jinfo = self._fetcher.fetch_catalog_json(board)
                self._catalog_json_ready.emit(jinfo)
        except Exception as e:
            print(f'[CatalogView] catalog json fetch error: {e}')
        self._board_info_ready.emit()

    def _make_quarantine_entry(self, no: int, ji: dict) -> CatalogEntry:
        """json専用スレ（隔離スレ）から合成カタログエントリを作る。
        ※ json にレス数は無いため res_count=0（カタログ未掲載のため不明）。"""
        import re as _re, html as _hh
        com = (ji.get("com", "") or "")
        sub = (ji.get("sub", "") or "").strip()
        # com の HTML を素テキスト化（<br>→空白、タグ除去、実体参照復元）
        t = _re.sub(r'<br\s*/?>', ' ', com, flags=_re.I)
        t = _re.sub(r'<[^>]+>', '', t)
        t = _hh.unescape(t).strip()
        if sub and sub not in ("無念",):
            title = (sub + " " + t).strip()
        else:
            title = t
        board = self._board
        return CatalogEntry(
            no         = no,
            thumb_url  = (ji.get("thumb", "") or ""),
            res_count  = 0,
            thread_url = (board.base_url + f"res/{no}.htm") if board else f"res/{no}.htm",
            title      = title,
            email      = ji.get("email", ""),
            op_id      = ji.get("id", ""),
            is_quarantine = True,
            board      = board,
        )

    def _merge_catalog_json(self, jinfo):
        """mode=json取得結果をカタログエントリにマージ。
        jinfo: {"map":{no:{email,id,com,sub,thumb}}, "nos":set} または None（取得失敗）。
        None のときは隔離判定・id補完を行わない。
        隔離スレ = json にあって cat に無い（隔離されるとカタログから消えてjsonに残る）。"""
        if not jinfo or not self._all_entries:
            return
        jmap = jinfo.get("map") or {}
        jnos = jinfo.get("nos") or set()
        # キャッシュ更新（次回 _on_entries_ready で先行適用）
        self._catalog_json_cache = jmap
        self._catalog_json_nos = jnos
        # ── 大取得検証ログ: 実カタログ件数とjson件数の一致確認 ──
        #   大取得(100x100)が効いていれば cat ≒ json（差は真の隔離数）。
        #   cat << json の場合は cxyl上書きが効いていない可能性（要調査）。
        try:
            _real_now = [e for e in self._all_entries
                         if not getattr(e, 'is_quarantine', False)]
            _cat_cnt = len(_real_now)
            _json_cnt = len(jnos)
            _miss = _json_cnt - _cat_cnt
            _ok = (_json_cnt == 0) or (_cat_cnt >= _json_cnt * 0.9)
        except Exception:
            pass
        changed = False
        # 1) 実カタログ(非隔離)エントリに email/id を補完
        for e in self._all_entries:
            if getattr(e, 'is_quarantine', False):
                continue
            ji = jmap.get(e.no)
            if ji:
                _em = ji.get("email", "")
                _id = ji.get("id", "")
                if _em and e.email != _em:
                    e.email = _em; changed = True
                if e.op_id != _id:
                    e.op_id = _id; changed = True
        # 2) 隔離スレ（json∖cat）を合成して最下部用に同期
        if getattr(self._settings, 'catalog_quarantine_bottom', True):
            real = [e for e in self._all_entries if not getattr(e, 'is_quarantine', False)]
            cat_nos = {e.no for e in real}
            quar_nos = jnos - cat_nos
            prev_quar_nos = {e.no for e in self._all_entries if getattr(e, 'is_quarantine', False)}
            if quar_nos != prev_quar_nos:
                new_quar = [self._make_quarantine_entry(no, jmap.get(no, {}))
                            for no in sorted(quar_nos)]
                self._all_entries = real + new_quar
                changed = True
        else:
            # 機能OFF: 既存の合成隔離エントリを除去
            real = [e for e in self._all_entries if not getattr(e, 'is_quarantine', False)]
            if len(real) != len(self._all_entries):
                self._all_entries = real
                changed = True
        if changed:
            self._re_render_light()

    def _merge_email_data(self, email_map: dict):
        """board topから取得したemail情報をカタログエントリにマージして再描画"""
        if not email_map or not self._all_entries:
            return
        # キャッシュ更新（次回 _on_entries_ready で先行適用される）
        self._email_cache.update(email_map)
        # 現在のエントリに存在しないNoは除去（肥大防止）
        _cur_nos = {e.no for e in self._all_entries}
        for _k in list(self._email_cache.keys()):
            if _k not in _cur_nos:
                del self._email_cache[_k]
        changed = False
        for e in self._all_entries:
            em = email_map.get(e.no, "")
            if em and e.email != em:
                e.email = em
                changed = True
        if changed:
            self._re_render_light()

    def _emit_catalog_status(self):
        """カタログ表示中のステータスバー情報を status_info シグナルで送出"""
        if not self._board:
            return
        board = self._board
        viewers_str = f"{board.viewers}人くらい" if board.viewers > 0 else ""
        ms = board.max_saved
        saved_str = f"保存上限 {ms}件" if ms > 0 else ""
        # 板別 global_max_no を取得
        # 大取得方式: _all_entries は全生存スレ。表示は cols×rows 件に絞られるが、
        # ステータスには板の生存スレ総数（非隔離）を表示する。
        entries = self._all_entries if hasattr(self, '_all_entries') else []
        n_live = len([e for e in entries if not getattr(e, 'is_quarantine', False)])
        self.status_info.emit({
            'viewers':  viewers_str,
            'expiry':   '',
            'saved':    saved_str,
            'momentum': '',
            'rescount': f"{n_live}スレ" if n_live else '',
            'log':      '',
            'view':     self,
        })


    def update_countdown(self, remaining_sec: int):
        """自動更新カウントダウン表示を更新する（AutoRefreshManagerから呼ばれる）"""
        if not hasattr(self, '_lbl_countdown'):
            return
        if remaining_sec < 0:
            self._lbl_countdown.setText("")
        elif remaining_sec >= 60:
            m, s = divmod(remaining_sec, 60)
            self._lbl_countdown.setText(f"更新まで {m}:{s:02d}")
        else:
            self._lbl_countdown.setText(f"更新まで {remaining_sec}s")

    def _display_capacity(self) -> int:
        """表示カタログの最大件数 = cols×rows（cxyl由来）。
        大取得(100x100)した _all_entries を表示用に絞る上限。"""
        cxyl = None
        if self._board:
            from futaba2b_settings import get_board_settings as _gbs
            cxyl = _gbs(self._board.base_url).catalog_cxyl_str
        if not cxyl:
            cxyl = self._fetcher.get_cxyl()
        try:
            p = (cxyl or "14x6").split("x")
            cap = int(p[0]) * int(p[1])
            return cap if cap > 0 else 14 * 6
        except Exception:
            return 14 * 6

    def _reverse_ng_title_chars(self) -> int:
        """逆NG判定に使うタイトル文字数（カタログ表示文字数 cat_chars = cxyl の3番目）。
        catalog_to_html の char_limit と同じ値を逆NG判定にも適用し、カタログに
        表示されていない文字での逆NG＝自動オープンを防ぐ。
        戻り値の意味: 0=タイトル非表示（先頭0文字＝逆NG判定なし）/ N>0=先頭N文字 /
        -1=cxyl不明で判定不能（全文＝従来通りのフォールバック）。"""
        cxyl = None
        if self._board:
            from futaba2b_settings import get_board_settings as _gbs
            cxyl = _gbs(self._board.base_url).catalog_cxyl_str
        if not cxyl:
            cxyl = self._fetcher.get_cxyl()
        try:
            parts = (cxyl or "").split("x")
            # 板の cl 値（0含む）はそのまま返す。cxyl が壊れている/取得不能な時のみ
            # -1（全文フォールバック）にして逆NGが効かなくなるのを防ぐ。
            return int(parts[2]) if len(parts) > 2 else -1
        except Exception:
            return -1

    def _re_render_light(self):
        """マージ再描画（email/mode=json取得後の差分反映）用の _re_render。
        フルページリロードではなく body のみのDOM入替で描画し、カタログ更新直後に
        二度目のリロードが走って画面がちらつく（スクロールバーが二度縮む）のを防ぐ。
        シグナル接続されている _re_render の引数シグネチャは変えず、フラグで伝える。"""
        self._light_render_once = True
        try:
            self._re_render()
        finally:
            self._light_render_once = False

    def _re_render(self):
        """検索 + ローカルソート + レス数フィルタ + NGフィルタを適用してレンダリング"""
        import time as _time
        entries = list(self._all_entries)
        # 隔離スレ（json∖cat の合成エントリ）はフィルタ/ソート対象外にして退避。
        # 過疎非表示(res=0)やローカルソートで消えたり並び替わるのを防ぎ、最後に最下部へ。
        _quar_entries = [e for e in entries if getattr(e, 'is_quarantine', False)]
        entries = [e for e in entries if not getattr(e, 'is_quarantine', False)]
        # 大取得方式: _all_entries は板の全生存スレ。表示は cols×rows 件
        # （サーバ順=取得順の先頭）に絞り「14×6+隔離」相当に振る舞う。
        # あふれた分は隔離検出・read_counts追跡用に _all_entries 側に保持し表示からのみ除外。
        _cap = self._display_capacity()
        if _cap > 0 and len(entries) > _cap:
            entries = entries[:_cap]
        # 1. 過疎スレ非表示（板設定優先、なければAppSettings）
        _few_hide = False
        _few_lim  = 5
        try:
            from futaba2b_settings import get_board_settings as _gbs
            _bsp = _gbs(self._board.base_url) if self._board else None
            if _bsp:
                _few_hide = getattr(_bsp, "catalog_few_res_hide", False)
                _few_lim  = getattr(_bsp, "catalog_few_res_count", 5)
            else:
                _few_hide = getattr(self._settings, 'catalog_few_res_hide', False)
                _few_lim  = getattr(self._settings, 'catalog_few_res_count', 5)
        except Exception:
            _few_hide = getattr(self._settings, 'catalog_few_res_hide', False)
            _few_lim  = getattr(self._settings, 'catalog_few_res_count', 5)
        if _few_hide:
            entries = [e for e in entries if e.res_count > _few_lim]
        # 2. ローカルソート
        if hasattr(self, '_local_sort_grp'):
            idx  = self._local_sort_grp.checkedId()
            # 「読」: thread_read_counts (実際に開いたスレ) を使って既読を上に
            read = self._settings.thread_read_counts
            if idx == 1:
                read_cnt = sum(1 for e in entries if read.get(e.thread_url, 0) > 0)
                entries.sort(key=lambda e: 0 if read.get(e.thread_url, 0) > 0 else 1)
            elif idx == 2:
                entries.sort(key=lambda e: (e.title or ""))
            elif idx == 3:
                entries.sort(key=lambda e: e.res_count)
            elif idx == 4:
                entries.sort(key=lambda e: e.res_count, reverse=True)
        # 3. 掲示板NGフィルタ（ng_board_hide_ng_thread）
        ng_filter = self._settings.ng_filter
        if getattr(self._settings, "ng_board_hide_ng_thread", True):
            entries = [e for e in entries if not ng_filter.is_ng_catalog(e)]
        # 字スレNG（ng_board_hide_ng_threadとは独立して動作）
        ng_empty_mode = getattr(self._settings, "ng_catalog_empty", 2)
        if ng_empty_mode == 1:
            # 「NGにする」: 画像なし（thumb_url空）のスレを除外
            entries = [e for e in entries if (e.thumb_url or "").strip()]
        elif ng_empty_mode == 0:
            # 「本文空のみNG」: 画像なし かつ タイトルも空のスレのみ除外
            entries = [e for e in entries if (e.thumb_url or "").strip() or (e.title or "").strip()]
        # 3.5 IDが出た(共通ID)スレ: 「最下部に表示」OFF なら非表示にする
        if not getattr(self._settings, "catalog_common_id_bottom", True):
            entries = [e for e in entries if not (getattr(e, 'op_id', '') or '').strip()]
        # 4. 検索: ヒットを上に隔離して表示 (正規表現対応)
        kw = self._search.text().strip()
        search_sections = None
        if kw:
            try:
                pat = re.compile(kw, re.IGNORECASE)
                matched   = [e for e in entries
                             if pat.search(e.title or "") or pat.search(str(e.no))]
                unmatched = [e for e in entries
                             if not (pat.search(e.title or "") or pat.search(str(e.no)))]
                search_sections = (matched, unmatched)
            except re.error:
                pass  # 正規表現エラー時は全表示
        # 隔離スレを末尾に合流（catalog_to_html の quarantine_section で最下部に分離表示）
        if _quar_entries:
            if search_sections:
                _m, _u = search_sections
                search_sections = (_m, list(_u) + _quar_entries)
            else:
                entries = entries + _quar_entries
        self._render(entries, search_sections=search_sections, ng_filter=ng_filter)

        # ── 逆NGアクション実行 ────────────────────────────────────────────
        action = getattr(self._settings, "ng_reverse_action", 0)
        if action > 0 and ng_filter:
            _tc = self._reverse_ng_title_chars()
            rev_entries = [e for e in entries
                           if ng_filter.is_reverse_ng_catalog(e, title_chars=_tc)]
            if rev_entries:
                self._exec_reverse_ng_action(rev_entries, action, ng_filter)

    _SERVER_SORTS = [0, 1, 2, 3, 4, 9]  # 通常/新順/古順/多順/少順/履歴

    def _on_server_sort_changed(self, idx: int = -1):
        if idx < 0: idx = self._sort_grp.checkedId()
        if self._board:
            sort = self._SERVER_SORTS[idx] if 0 <= idx < len(self._SERVER_SORTS) else 0
            self._save_view_state()
            self.load(self._board, sort)

    def _open_catalog_settings(self):
        """板ごとの設定ダイアログ（カタログタブ）を開く"""
        p = self.parent()
        while p and not hasattr(p, "_open_board_settings"):
            p = p.parent()
        if p:
            p._open_board_settings(tab_name="カタログ")


    def _exec_reverse_ng_action(self, rev_entries: list, action: int, ng_filter=None):
        """逆NGに一致したエントリに対してアクションを実行
        action: 1=非アクティブで開く, 2=スレッドを開く, 3=ポップアップ通知
        開いたURLはAppSettingsに永続保存し、再起動後も重複開きを防ぐ。
        複数件は200ms間隔の遅延キューで順次処理しUIフリーズを防ぐ。
        """
        opened = self._settings.ng_reverse_opened_urls  # 永続セット

        new_entries = [e for e in rev_entries if e.thread_url not in opened]
        if not new_entries:
            return

        LIMIT = getattr(self._settings, "ng_reverse_max_open", 99)
        targets = new_entries[:LIMIT]

        # 開く前にURLを全件登録して重複防止・設定保存
        _rev_list = self._settings._ng_reverse_opened_list
        for e in targets:
            if e.thread_url not in opened:
                opened.add(e.thread_url)
                _rev_list.append(e.thread_url)
        self._settings.save()  # save時に2000件上限でFIFO削除

        # ── 棒読みちゃん通知（notify_type=="bouyomi" な逆NGワードにマッチした場合）──
        if ng_filter is not None:
            self._exec_reverse_ng_bouyomi(targets, ng_filter)
            self._exec_reverse_ng_sound(targets, ng_filter)

        # 1件なら即時実行、複数件は200ms間隔キューで順次処理
        if len(targets) == 1:
            self._exec_reverse_ng_one(targets[0], action)
            return

        queue = list(targets)  # コピーしてキュー化

        def _pop_one():
            if not queue:
                return
            e = queue.pop(0)
            self._exec_reverse_ng_one(e, action)
            if queue:
                QTimer.singleShot(200, _pop_one)

        _pop_one()

    def _exec_reverse_ng_sound(self, targets: list, ng_filter) -> None:
        """逆NGヒットのうち notify 有効かつ notify_type=='sound' なワードにマッチが
        あれば効果音(ng_se.wav)を1回鳴らす。"""
        _tc = self._reverse_ng_title_chars()
        for e in targets:
            matched = ng_filter.get_matched_reverse_ng_words_catalog(e, title_chars=_tc)
            if any(w.get("notify") and w.get("notify_type", "sound") == "sound"
                   for w in matched):
                _play_ng_se()
                return

    def _exec_reverse_ng_bouyomi(self, targets: list, ng_filter) -> None:
        """逆NGヒットエントリのうち notify_type=='bouyomi' なワードにマッチするものを棒読み送信"""
        s = self._settings
        if not getattr(s, "bouyomi_enabled", False):
            return
        fmt = getattr(s, "ng_reverse_bouyomi_format", "{keyword1}")

        host   = getattr(s, "bouyomi_host",   "localhost")
        port   = getattr(s, "bouyomi_port",   50080)
        speed  = getattr(s, "bouyomi_speed",  -1)
        tone   = getattr(s, "bouyomi_tone",   -1)
        volume = getattr(s, "bouyomi_volume", -1)
        voice  = getattr(s, "bouyomi_voice",   0)

        # 板名（サブドメインあり）: "may/b" 形式
        board = getattr(self, "_board", None)
        board_name = ""
        if board:
            import re as _re2
            m = _re2.match(r'https?://([^.]+)\.2chan\.net/([^/]+)/', board.url or "")
            board_name = f"{m.group(1)}/{m.group(2)}" if m else (board.name or "")

        _tc = self._reverse_ng_title_chars()
        texts = []
        for e in targets:
            matched_words = ng_filter.get_matched_reverse_ng_words_catalog(e, title_chars=_tc)
            bouyomi_words = [w for w in matched_words
                             if w.get("notify_type", "sound") == "bouyomi"]
            if not bouyomi_words:
                continue
            keyword = bouyomi_words[0].get("pattern", "")
            keyword1 = keyword.split("|")[0].strip()
            text = fmt.format(
                keyword=keyword,
                keyword1=keyword1,
                board=board_name,
                title=e.title or "",
                url=e.thread_url or "",
            )
            texts.append(text)

        if not texts:
            return

        import urllib.request, urllib.parse, threading as _th
        def _send():
            for text in texts:
                try:
                    params = urllib.parse.urlencode({
                        "text": text, "speed": speed, "tone": tone,
                        "volume": volume, "voice": voice,
                    })
                    url = f"http://{host}:{port}/Talk?{params}"
                    urllib.request.urlopen(url, timeout=2).read()
                except Exception:
                    pass
        _th.Thread(target=_send, daemon=True).start()

    def _exec_reverse_ng_one(self, e, action: int):
        """逆NG 1件分のアクション実行"""
        if action == 1:
            mode = getattr(self._settings, 'thread_open_bg_mode', 0)
            self.thread_open_bg_mode.emit(e.thread_url, mode)
        elif action == 2:
            mode = getattr(self._settings, 'thread_open_mode', 0)
            self.thread_open_mode.emit(e.thread_url, mode)
        elif action == 3:
            from PySide6.QtWidgets import QToolTip
            from PySide6.QtCore import QPoint
            title = (e.title or e.thread_url)[:40]
            QToolTip.showText(
                self.mapToGlobal(QPoint(0, 0)),
                f"[逆NG] {title}", self, self.rect(), 3000)


    # ────────────────────────────────────────────────────────────
    def update_settings(self):
        """MainWindow の設定変更を BoardPane に即時反映 (タイマーのみ)"""
        if hasattr(self, "_reload_timer"):
            s = getattr(self, "_settings", None) or self._main._settings
            if True:  # 板を開いたとき常に自動取得
                ms = getattr(s, "auto_fetch_interval", 60) * 1000
                self._reload_timer.start(max(10000, ms))
            else:
                self._reload_timer.stop()

    def _save_view_state(self):
        """現在の UI 状態をボード別に保存する"""
        if not self._board or not hasattr(self, "_local_sort_grp"):
            return
        self._settings.catalog_view_states[self._board.url] = {
            "local_sort":  self._local_sort_grp.checkedId(),
            "search":      self._search.text(),
            "server_sort": self._sort_grp.checkedId(),
        }
        self._settings.save()

    def _apply_board_default_sort(self, board: BoardInfo):
        """板別設定「カタログのソート」をローカルソートの初期値として適用する。
        マッピング: なし(0)/URL(1)/勢い(4)→なし、レス数(2)→desc設定で多/少、
        既読(3)→読、50音(5)→50音。URL順・勢いソートは2BPに存在しないため「なし」扱い。"""
        if not hasattr(self, "_local_sort_grp"):
            return
        try:
            from futaba2b_settings import get_board_settings as _gbs
            bs = _gbs(board.base_url)
            stype = getattr(bs, "catalog_sort_type", 0) or 0
            sdesc = bool(getattr(bs, "catalog_sort_desc", False))
        except Exception:
            return
        if stype == 2:          # レス数
            local_id = 4 if sdesc else 3   # 多い順 / 少ない順
        elif stype == 3:        # 既読
            local_id = 1
        elif stype == 5:        # 50音
            local_id = 2
        else:                   # なし / URL / 勢い
            local_id = 0
        if local_id == 0:
            return
        btn = self._local_sort_grp.button(local_id)
        if btn:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    def _restore_view_state(self):
        """保存された UI 状態を復元する (シグナルをブロックして再フェッチを防ぐ)"""
        if not self._board or not hasattr(self, "_local_sort_grp"):
            return
        state = self._settings.catalog_view_states.get(self._board.url, {})
        if not state:
            return
        # シグナルをブロックして復元中の副作用を防ぐ
        for grp in [self._local_sort_grp, self._sort_grp]:
            for btn in grp.buttons():
                btn.blockSignals(True)
        self._search.blockSignals(True)
        try:
            b = self._local_sort_grp.button(state.get("local_sort", 0))
            if b: b.setChecked(True)
            self._search.setText(state.get("search", ""))
            b = self._sort_grp.button(state.get("server_sort", 0))
            if b: b.setChecked(True)
        finally:
            for grp in [self._local_sort_grp, self._sort_grp]:
                for btn in grp.buttons():
                    btn.blockSignals(False)
            self._search.blockSignals(False)

    # style4.css .cs0-.cs6 準拠の画像サイズ (px)
    _IMG_PX = {0: 56, 1: 79, 2: 104, 3: 129, 4: 154, 5: 179, 6: 254}

    def _render(self, entries, **kwargs):
        import datetime as _dt
        _DAY_JP = ['月','火','水','木','金','土','日']
        def _fmt_dt(dt):
            return (f"{dt.year}/{dt.month:02d}/{dt.day:02d}"
                    f" ({_DAY_JP[dt.weekday()]})"
                    f" {dt.hour}:{dt.minute:02d}:{dt.second:02d}")
        n_total  = len(entries)
        now_str  = _fmt_dt(_dt.datetime.now())
        _footer  = (
            f'<div class="page-footer">'
            f'スレッド: {n_total}件 ／ 最終更新: {now_str}'
            f' ／ 2BP {APP_VER}'
            f'</div>'
        )
        cxyl = None
        if self._board:
            from futaba2b_settings import get_board_settings as _gbs
            _bs = _gbs(self._board.base_url)
            cxyl = _bs.catalog_cxyl_str
        if not cxyl:
            cxyl = self._fetcher.get_cxyl()
        try:
            parts      = cxyl.split("x")
            cols       = int(parts[0]) if len(parts) > 0 else 14
            char_limit = int(parts[2]) if len(parts) > 2 else 6
            img_idx    = int(parts[4]) if len(parts) > 4 else 0
            img_size   = self._IMG_PX.get(img_idx, 84)
        except Exception:
            cols, char_limit, img_size = 14, 6, 84

        # 前回レス数との差分を渡してカタログに新着数を表示
        read_counts = self._settings.catalog_read_counts
        thread_read_counts = self._settings.thread_read_counts
        # user_css は BoardSettings から（self._board が未設定なら AppSettings にフォールバック）
        if self._board:
            from futaba2b_settings import get_board_settings as _gbs2
            _ucss = _load_user_css(_gbs2(self._board.base_url))
        else:
            _ucss = _load_user_css(self._settings)
        # NGフィルタを kwargs から受け取るかシングルトンを使用
        _ng_filter = kwargs.get("ng_filter") or self._settings.ng_filter
        _sbc = getattr(self._settings, 'scroll_bottom_count', 5)
        _cat_html = catalog_to_html(entries, char_limit, img_size, cols,
                            read_counts=read_counts,
                            thread_read_counts=thread_read_counts,
                            search_sections=kwargs.get("search_sections"),
                            user_css=_ucss,
                            ng_filter=_ng_filter,
                            ng_settings=self._settings,
                            nowrap_title=(char_limit == 0),
                            scroll_bottom_count=_sbc,
                            scroll_top_count=getattr(self._settings,'scroll_top_count',0),
                            footer_html=_footer,
                            hover_zoom=getattr(self._settings, "catalog_hover_zoom", False),
                            hover_comment=getattr(self._settings, "catalog_hover_comment", False),
                            show_email=False,  # カタログのメール内容バッジ（フッタ）は常に非表示
                            show_badge=getattr(self._settings, "catalog_show_mail_badge", True),
                            quarantine_section=getattr(self._settings, "catalog_quarantine_bottom", True),
                            common_id_section=getattr(self._settings, "catalog_common_id_bottom", True))
        # マージ再描画（_re_render_light 経由）はフルリロードせず body のみ入替える。
        # 通常描画（カタログ取得・ソート・検索等）は従来どおりフルロード（先頭に戻る挙動を維持）。
        _light = self._light_render_once
        _swapped = False
        if _light:
            _bi = _cat_html.find('<body>')
            _bj = _cat_html.rfind('</body>')
            if _bi >= 0 and _bj > _bi:
                _body_inner = _cat_html[_bi + 6:_bj]
                # 入替で旧DOM要素が消えると onmouseleave が発火しないため先に閉じる
                self._on_cat_hover_leave()
                if self._cat_page_live:
                    self._apply_catalog_body_swap(_body_inner)
                else:
                    # 初回フルロードがまだ完了していない → loadFinished 後に適用
                    self._pending_light_body = _body_inner
                _swapped = True
        if not _swapped:
            self._load_html_via_tempfile(_cat_html, QUrl("https://www.2chan.net/"))

        # catalog_read_counts: 未登録スレのみ現在のレス数を基準値として登録する
        # （既登録スレは上書きしない → +N がリセットされない）
        # 大取得方式: 表示は cols×rows 件に絞っているが、read_counts の追跡対象は
        # 板の全生存スレ(_all_entries の非隔離)で行う。表示外(あふれ)スレの基準値が
        # 毎描画で消える/再登録されて +N が壊れるのを防ぐ。
        rc  = self._settings.catalog_read_counts
        trc = self._settings.thread_read_counts
        _live_real = [e for e in getattr(self, '_all_entries', [])
                      if not getattr(e, 'is_quarantine', False)]
        live_urls = {e.thread_url for e in _live_real if e.thread_url}
        if self._board:
            res_prefix = self._board.base_url + "res/"
            for key in [k for k in rc if k.startswith(res_prefix) and k not in live_urls]:
                del rc[key]
            for key in [k for k in trc if k.startswith(res_prefix) and k not in live_urls]:
                del trc[key]
        for e in _live_real:
            if e.thread_url and e.res_count > 0 and e.thread_url not in rc:
                rc[e.thread_url] = e.res_count
        self._settings.save()

    def set_cxyl(self, cxyl: str):
        # cxylはBoardSettingsから直接読むため、クッキー更新のみ行い再描画
        self._fetcher.set_cxyl_cookie(cxyl)
        self._render(self._all_entries)

    # ── D&D ログファイルを開く ───────────────────────────────────────────────
    def dragEnterEvent(self, event):
        """ログファイル（zip/mht/mhtml/html/htm）のD&Dを受け付ける"""
        import os
        urls = event.mimeData().urls()
        if any(u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower()
               in ('.zip', '.mht', '.mhtml', '.html', '.htm')
               for u in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """ドロップされたログファイルをMainWindowに転送して開く"""
        import os
        p = self
        while p and not hasattr(p, '_open_log_file'):
            p = p.parent()
        if not p:
            event.ignore()
            return
        for url in event.mimeData().urls():
            if url.isLocalFile():
                p._open_log_file(url.toLocalFile())
        event.acceptProposedAction()


# ══════════════════════════════════════════════════════════════════════════════
# 画像タブビュー
# ══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# 自動更新 マネージャー & ダイアログ
# ═══════════════════════════════════════════════════════════════════════════════
import weakref as _wr
from datetime import datetime as _dt, time as _time


def _compute_interval_sec(adaptive_intervals: list, pct_remaining: float) -> int:
    """
    段階的更新間隔ルールと残り件数の割合から更新間隔（秒）を計算する。
    pct_remaining: 残り件数 / 最大保存件数 * 100
    有効なルールのうち、pct_remaining <= rule["pct"] を満たす中で
    最も pct が小さい（= 最も厳しい）ルールを適用する。
    ルールには interval_sec（秒）または interval_min（分）を格納できる。
    interval_sec が存在する場合はそちらを優先する。
    """
    def _to_sec(r: dict) -> int:
        if "interval_sec" in r:
            return max(1, int(r["interval_sec"]))
        return max(1, int(r.get("interval_min", 60))) * 60

    matching = [r for r in adaptive_intervals
                if r.get("enabled", True) and pct_remaining <= r.get("pct", 100)]
    if not matching:
        fallback = [r for r in adaptive_intervals if r.get("enabled", True)]
        if fallback:
            best = max(fallback, key=lambda r: r.get("pct", 0))
        else:
            return 3600
    else:
        best = min(matching, key=lambda r: r.get("pct", 100))
    _sec = _to_sec(best)
    # 容量逼迫(残り割合が極小)のスレは、段階更新の設定に関わらず最短60sを下限にする。
    # json が落ちても生存を返し続けるスレを容量チェックのフルGET404で速やかに
    # 検知するため（落ちたのに長時間閉じない/自動更新を無視する症状の対策）。
    if pct_remaining <= 2.0 and _sec > 60:
        _sec = 60
    return _sec


class AutoRefreshManager(QObject):
    """自動更新エントリを管理し、1秒ごとにカウントダウンして更新を実行する"""
    updated       = Signal(int)
    entry_removed = Signal(int)
    entry_added   = Signal()   # エントリが追加されたときに発火
    _view_update  = Signal(object, object, bool, bool)  # view, thread, scroll, bouyomi
    _sd_apply     = Signal(object, object)               # view, {no: count} そうだね反映
    _remove_later_url = Signal(str)          # URLベースの削除要求（スレッドセーフ）
    _catalog_reload   = Signal(object)       # カタログビュー更新要求（スレッドセーフ）
    _fetching_done    = Signal(str)            # BGスレッドからfetching完了通知（URLキー）
    _thread_dead_sig  = Signal(object, str)  # thread_dead をメインスレッドで発火（view, url）
    _thread_full_sig  = Signal(object, str)  # 1000レス到達専用（_is_deadを立てない）
    _errband_sig      = Signal(object, str)  # 通信エラー赤帯（view, text; ""=解除）。帯＋タブ赤化

    def __init__(self, fetcher, settings, parent=None):
        super().__init__(parent)
        self._fetcher  = fetcher
        self._settings = settings
        self._entries: list[AutoRefreshEntry] = []
        self._views:   list               = []
        self._remain:  list[int]          = []
        self._new_cnt: list[int]          = []
        self._res_cnt: list[int]          = []
        self._fetching: set[str]          = set()  # フェッチ中エントリのURL（位置indexだと削除でズレる）
        self._settings_dirty = False       # 設定保存の遅延フラグ（BGスレッドからの全量save防止）
        self._last_settings_save = 0.0     # 最終保存時刻 (time.monotonic)
        self._fetch_pool = _FETCH_POOL  # グローバルプールを共用
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._view_update.connect(self._update_view)
        self._sd_apply.connect(self._apply_sd_to_view)
        self._remove_later_url.connect(self.remove_by_url)
        self._catalog_reload.connect(self._do_catalog_reload)
        self._fetching_done.connect(lambda u: self._fetching.discard(u))
        def _on_thread_dead_sig(v, u):
            if not v or app_is_shutting_down():
                return
            try:
                from shiboken6 import isValid
                if not isValid(v):
                    return
            except Exception:
                pass
            try:
                v._is_dead = True
                # 再フェッチしない経路は表示中ページに赤帯が無いため注入する
                if getattr(v, '_pending_dead_banner', False):
                    v._pending_dead_banner = False
                    v._inject_dead_banner()
                v.thread_error.emit("スレ落ち")
                v.thread_dead.emit(u)
            except RuntimeError:
                pass
        self._thread_dead_sig.connect(_on_thread_dead_sig)

        def _on_errband_sig(v, text):
            if not v or app_is_shutting_down():
                return
            try:
                from shiboken6 import isValid
                if not isValid(v):
                    return
            except Exception:
                pass
            try:
                if text:
                    v._inject_error_band(text)     # 上下赤帯
                    v.thread_error.emit(text)      # タブ赤化
                else:
                    v._clear_error_band()          # 赤帯解除
                    v.thread_recovered.emit()      # タブ赤解除
            except RuntimeError:
                pass
        self._errband_sig.connect(_on_errband_sig)

        def _on_thread_full_sig(v, u):
            if not v or app_is_shutting_down():
                return
            try:
                from shiboken6 import isValid
                if not isValid(v):
                    return
            except Exception:
                pass
            try:
                v.thread_dead.emit(u)
            except RuntimeError:
                pass
        self._thread_full_sig.connect(_on_thread_full_sig)
    # ── エントリ管理 ──
    def add(self, entry: AutoRefreshEntry, view=None):
        # 同じURLの重複登録を防止
        for e in self._entries:
            if e.url == entry.url:
                return
        self._entries.append(entry)
        self._views.append(_wr.ref(view) if view else None)
        self._remain.append(entry.interval_sec)
        self._new_cnt.append(0)
        self._res_cnt.append(0)
        if not self._timer.isActive():
            self._timer.start()
        self.entry_added.emit()

    def has_url(self, url: str) -> bool:
        return any(e.url == url for e in self._entries)

    def find_by_url(self, url: str):
        """URLに対応するエントリを返す（なければNone）"""
        for e in self._entries:
            if e.url == url:
                return e
        return None

    def remove_by_view(self, view):
        """ビューオブジェクトに対応するエントリを削除（タブ閉じ時に呼ぶ）"""
        for i in range(len(self._views) - 1, -1, -1):
            ref = self._views[i]
            if ref is not None and ref() is view:
                self.remove(i)

    def remove_by_url(self, url: str):
        """URLに対応するエントリを削除（スレ落ち・1000レス時に呼ぶ）"""
        for i in range(len(self._entries) - 1, -1, -1):
            if self._entries[i].url == url:
                self.remove(i)

    def remove(self, idx: int):
        if 0 <= idx < len(self._entries):
            self._entries.pop(idx)
            self._views.pop(idx)
            self._remain.pop(idx)
            self._new_cnt.pop(idx)
            self._res_cnt.pop(idx)
            # _fetching はURLキーなので削除によるindexズレの補正は不要
            self.entry_removed.emit(idx)
        if not self._entries:
            self._timer.stop()
            self._flush_settings()

    def entry_count(self):   return len(self._entries)
    def entry(self, i):      return self._entries[i]
    def remaining(self, i):  return self._remain[i]
    def new_count(self, i):  return self._new_cnt[i]
    def res_count(self, i):  return self._res_cnt[i]



    def find_entry_by_url(self, url: str):
        """URLに対応するARエントリを返す（なければNone）"""
        for e in self._entries:
            if e.url == url:
                return e
        return None

    def reset_remain_by_url(self, url: str):
        """URLに対応するエントリのカウントダウンを interval_sec にリセット（手動更新時）"""
        for i, e in enumerate(self._entries):
            if e.url == url:
                self._remain[i] = e.interval_sec
                break

    def update_remain(self, idx: int, new_interval_sec: int):
        """interval_sec を更新し、カウントダウンも短い方に合わせる"""
        if 0 <= idx < len(self._entries):
            self._entries[idx].interval_sec = new_interval_sec
            if new_interval_sec < self._remain[idx]:
                self._remain[idx] = new_interval_sec

    def is_active(self):  return self._timer.isActive()
    def start(self):      self._timer.start()
    def stop(self):
        self._timer.stop()
        self._flush_settings()

    def _flush_settings(self):
        """dirtyな設定を即時保存する（タイマー停止時に呼ぶ・メインスレッド前提）"""
        if self._settings_dirty:
            self._settings_dirty = False
            self._last_settings_save = time.monotonic()
            self._settings.save()

    # ── 1秒ごと ──
    def _tick(self):
        # 終了処理中は自動更新を完全停止（破棄中ビューへのアクセスによるクラッシュ防止）
        if app_is_shutting_down():
            self._timer.stop()
            return
        # 遅延保存フラッシュ（メインスレッド・5秒スロットル）
        if self._settings_dirty and (time.monotonic() - self._last_settings_save) >= 5.0:
            self._settings_dirty = False
            self._last_settings_save = time.monotonic()
            self._settings.save()
        now = _dt.now()
        # 閉じたタブ（弱参照が切れたビュー）を後ろから削除
        for i in range(len(self._entries) - 1, -1, -1):
            ref = self._views[i]
            if ref is not None and ref() is None:
                self.remove(i)
        for i, entry in enumerate(self._entries):
            if not entry.enabled:
                continue
            if entry.stop_hour >= 0:
                stop = _time(entry.stop_hour, entry.stop_min)
                if now.time() >= stop:
                    entry.enabled = False
                    continue
            # 段階的更新間隔を毎ティック再計算し、短くなっていたらカウントダウンを詰める。
            # （従来は更新時にしか再計算せず、長い間隔(例3600s)で開始したスレは容量が
            #  逼迫しても次の更新まで間隔が縮まらず「残り8分」等のまま落ち検知が遅れた）
            try:
                if getattr(entry, 'adaptive_intervals', None) and entry.max_saved > 0:
                    _bk = entry.url.rsplit("/res/", 1)[0] + "/"
                    _o = self._settings.global_max_no_by_board.get(_bk, 0)
                    if _o > 0:
                        _rem = entry.no + entry.max_saved - _o
                        _pct = max(0.0, _rem / entry.max_saved * 100)
                        _isec = _compute_interval_sec(entry.adaptive_intervals, _pct)
                        if _isec != entry.interval_sec:
                            entry.interval_sec = _isec
                        if entry.interval_sec < self._remain[i]:
                            self._remain[i] = entry.interval_sec
            except Exception:
                pass
            self._remain[i] -= 1
            if self._remain[i] <= 0:
                self._remain[i] = entry.interval_sec
                self._do_refresh(i)
            # カウントダウンをviewに通知（_tickはメインスレッドのQTimer起点なので直接呼べる）
            ref = self._views[i]
            view = ref() if ref else None
            if view and hasattr(view, 'update_countdown'):
                try:
                    if _sb_valid is None or _sb_valid(view):
                        view.update_countdown(self._remain[i])
                except RuntimeError:
                    pass

    def _do_refresh(self, idx: int):
        import threading
        if app_is_shutting_down():
            return
        entry = self._entries[idx]
        # 同一エントリが既にフェッチ中なら投入しない（URLキー: BG実行中に他エントリが
        # remove()されてもindexがズレず、完了通知の取りこぼしが起きない）
        _url = entry.url
        if _url in self._fetching:
            return
        self._fetching.add(_url)
        # entry と同時に表示先ビュー参照を捕捉する（メインスレッド）。
        # BG実行時に self._views[idx] を再読みすると、投入〜実行の間に
        # remove() で並列リストが前詰めシフトした場合 idx が別エントリの
        # ビューを指し、別スレの新着が混入する（v0.9.73で修正）。
        ref_v = self._views[idx]

        # ── カタログエントリの場合は reload シグナルを発火するだけ ────────
        if getattr(entry, 'is_catalog', False):
            def _catalog_fetch(_ref=ref_v, _idx=idx, _u=_url):
                try:
                    entry.last_update_str = _dt.now().strftime("%y/%m/%d %H:%M:%S")
                    view = _ref() if _ref else None
                    self._catalog_reload.emit(view)
                    self.updated.emit(_idx)
                finally:
                    self._fetching_done.emit(_u)
            threading.Thread(target=_catalog_fetch, daemon=True).start()
            return

        def _fetch():
            try:
                from futaba2b_models import BoardInfo
                board = BoardInfo(name=entry.board_name, url=entry.url.rsplit("/res/", 1)[0] + "/")
                no    = entry.no

                ref_v_local = ref_v
                view  = ref_v_local() if ref_v_local else None
                th_cur = getattr(view, '_thread', None) if view else None
                # 防御: 捕捉ビューのスレが entry と別物なら append/emit せず中断。
                # （ref_v捕捉で混入は塞がるが、ビュー使い回し等の異常時の二重防御。
                #  1サイクル分スキップするだけで diff API は次回 start_no から復帰）
                if th_cur is not None and th_cur.no != entry.no:
                    return
                # start_no: API は start 以降（含む）を返すので +1 して重複を防ぐ
                start_no = (th_cur.res_list[-1].no + 1
                            if th_cur and th_cur.res_list else no)

                try:
                    diff = self._fetcher.fetch_thread_diff(board, no, start_no)
                except Exception as e:
                    print(f'[AutoRefresh] diff fetch error No.{no}: {e}')
                    return

                if diff["error"]:
                    _code = diff["error"].split()[0] if diff["error"].split() else ''
                    if _code == "404":
                        print(f'[AutoRefresh] HTTP 404 検出 No.{no} → 削除（JSON・スレ消滅）')
                        # フルGETでバナー表示してから削除
                        try:
                            th_err = self._fetcher.fetch_thread(board, no)
                            if view:
                                self._view_update.emit(view, th_err, False, False)
                        except Exception:
                            pass
                        self._remove_later_url.emit(entry.url)
                        # thread_dead をメインスレッドで発火（BGスレッドから直接 QTimer は危険）
                        if view:
                            self._thread_dead_sig.emit(view, entry.url)
                    else:
                        # 503等の一時的エラーは削除・保存せずスキップ（自動更新は継続）。
                        # 表示中スレに上下赤帯＋タブ赤化で通知する。
                        print(f'[AutoRefresh] 一時エラー {diff["error"]} No.{no} → スキップ（保存・削除なし）')
                        if view:
                            self._errband_sig.emit(view, diff["error"])
                    return

                # ここに到達 = diff取得成功（エラーなし）。
                # 直前に通信エラーで赤帯/赤タブが付いていたら復旧として解除する。
                # （新着が無い「変化なし」サイクルでも解除されるよう、ここで判定する）
                if view is not None and getattr(view, '_has_error_band', False):
                    self._errband_sig.emit(view, "")

                # スレ落ち検知（dielong が 1972年以前 = エポック付近）
                if diff["is_dead"]:
                    # 【重要】同じdiffレスポンスに最後のレス群が含まれている。
                    # 適用せずに保存すると末尾レスが欠落するため、先に取り込む。
                    _final = diff.get("new_res") or []
                    if th_cur and _final:
                        _ex = {r.no for r in th_cur.res_list}
                        _add = [r for r in _final if r.no not in _ex]
                        for r in _add:
                            r.is_new = True
                            r.res_idx = len(th_cur.res_list)
                            th_cur.res_list.append(r)
                        th_cur.received_count = len(th_cur.res_list)
                        view._last_valid_thread = th_cur
                        if _add:
                            # data/logキャッシュにも反映（スレ落ち後再表示の末尾欠落防止）
                            self._fetcher.append_diff_to_cache(entry.url, _add)
                            print(f'[AutoRefresh] スレ落ち直前の新着 {len(_add)}件を取り込み No.{no}')
                    print(f'[AutoRefresh] スレ落ち検知（dielong） No.{no} → 削除・自動保存')
                    self._remove_later_url.emit(entry.url)
                    if view:
                        # この経路は再フェッチしないため表示中ページに赤帯が無い。
                        # _on_thread_dead_sig で赤帯を注入させるフラグを立てる。
                        view._pending_dead_banner = True
                        # メインスレッドで thread_dead を発火（BGスレッドから直接 QTimer は危険）
                        self._thread_dead_sig.emit(view, entry.url)
                    return

                # 板容量によるスレ落ち検知（dielongが落ちないまま板から押し出された場合）。
                # JSON diff API は容量落ちを is_dead/404 に反映しないことがあり、その場合
                # HTMLページは404でもタブが残り続ける（手動更新のみフルGETで404検知できる）。
                # 残保存数(no + max_saved - global_max)が0以下の間は、一定間隔(60s)ごとに
                # フルGETで404を確認し続ける。
                # （旧実装は確認を一度きりにしていたため、容量超過に達した瞬間にまだ生存
                #   していると以後二度と確認されず、実際に404になっても自動更新で落ちなかった。
                #   手動更新のフルGETでのみ404検知できる状態になっていた）
                _o  = self._settings.global_max_no_by_board.get(board.base_url, 0)
                _ms = entry.max_saved
                _remaining = (entry.no + _ms - _o) if (_ms > 0 and _o > 0) else None
                _over = (_remaining is not None and _remaining <= 0)
                # 落ち予定時刻(dielong)がサーバー現在時刻(nowtime)を過ぎたか。
                # ふたばの落下は作成No順ではなく最終バンプ順のため、低活性スレは
                # 容量(no+max_saved-global_max)に余裕があっても早期に落ちる。その場合
                # is_dead(=dielongがepoch)も容量超過も発火しないが、dielong<=nowtime
                # （落ち予定時刻の到達）は捉えられる。これを404確認の追加トリガにする。
                _die_passed = False
                try:
                    _dl = diff.get("dielong", ""); _nt = diff.get("nowtime", 0)
                    if _dl and _nt:
                        from email.utils import parsedate_to_datetime as _pdt
                        _de = _pdt(_dl).timestamp()
                        if _de > 0 and _de <= float(_nt):
                            _die_passed = True
                except Exception:
                    pass
                # 落ちが近い/予定時刻到達のスレは更新間隔を強制的に短縮（60s）し、
                # 落ちた直後(最大~60s)に404確認できるようにする。
                if ((_die_passed or (_remaining is not None and _remaining <= 1000))
                        and entry.interval_sec > 60):
                    entry.interval_sec = 60
                    if 0 <= idx < len(self._remain):
                        self._remain[idx] = min(self._remain[idx], 60)
                if _over or _die_passed:
                    import time as _tmod
                    _now = _tmod.monotonic()
                    # 連続フルGET防止: 容量超過中は最短60s間隔で再確認する
                    if (_now - getattr(entry, '_capacity_check_at', 0.0)) >= 60.0:
                        entry._capacity_check_at = _now
                        try:
                            th_chk = self._fetcher.fetch_thread(board, no)
                        except Exception:
                            th_chk = None
                        if th_chk is not None and (th_chk.error or "").split()[:1] == ["404"]:
                            print(f'[AutoRefresh] 落ち検知(404) No.{no} '
                                  f'over={_over} die_passed={_die_passed} '
                                  f'残{_remaining} → 削除・自動保存')
                            if view:
                                self._view_update.emit(view, th_chk, False, False)
                            self._remove_later_url.emit(entry.url)
                            if view:
                                self._thread_dead_sig.emit(view, entry.url)
                            return

                # 1000レス到達検知（maxresフィールドが空でない）
                if diff["is_full"]:
                    print(f'[AutoRefresh] 1000レス到達 No.{no} → フルGET後に削除・自動保存')
                    try:
                        th = self._fetcher.fetch_thread(board, no)
                    except Exception as e:
                        print(f'[AutoRefresh] full fetch error No.{no}: {e}')
                        return
                    if view:
                        self._view_update.emit(view, th, False, False)
                        self._thread_full_sig.emit(view, entry.url)
                    self._remove_later_url.emit(entry.url)
                    return

                new_res = diff["new_res"]

                # そうだね数を既存レスに反映
                if th_cur and diff["sd"]:
                    for r in th_cur.res_list:
                        sd_val = diff["sd"].get(str(r.no), None)
                        if sd_val is not None:
                            try:
                                r.sodane = int(sd_val)
                            except ValueError:
                                pass
                    th_cur._sd_update = {int(k): int(v) for k, v in diff["sd"].items()
                                         if v.lstrip("-").isdigit()}
                else:
                    if th_cur:
                        th_cur._sd_update = {}

                entry.last_update_str = _dt.now().strftime("%y/%m/%d %H:%M:%S")

                # die_time をスレデータに記録
                if th_cur and diff["die"]:
                    th_cur.die_time = diff["die"]

                if th_cur:
                    # 重複を除いて追記（start+1でもAPIが同じNoを返すことがあるため）
                    existing_nos = {r.no for r in th_cur.res_list}
                    added = [r for r in new_res if r.no not in existing_nos]
                    for r in added:
                        r.is_new = True
                        r.res_idx = len(th_cur.res_list)
                        th_cur.res_list.append(r)
                    th_cur.received_count = len(th_cur.res_list)
                    if added:
                        self._settings.thread_read_counts[entry.url] = len(th_cur.res_list)
                        self._settings_dirty = True   # 保存は_tick(メインスレッド)で5秒間隔フラッシュ
                        # data/logキャッシュにも反映（キャッシュフォールバック時の末尾欠落防止）
                        self._fetcher.append_diff_to_cache(entry.url, added)
                        # 新着画像を先読みキャッシュ（非表示タブでもスレ落ち保存の欠落を防ぐ）
                        if getattr(self._settings, 'prefetch_open_thread_images', True):
                            _vx = ('.mp4', '.webm', '.mov', '.avi', '.mkv')
                            _pf = [r.image_url for r in added
                                   if r.image_url and not r.image_url.lower()
                                       .rsplit('?', 1)[0].endswith(_vx)]
                            if _pf:
                                try:
                                    self._fetcher.prefetch_images(_pf, group=entry.url)
                                except Exception:
                                    pass
                    # スレ落ち自動保存用に最新状態を同期
                    view._last_valid_thread = th_cur
                    th = th_cur
                    new_n = len(added)  # 重複除外後の実際の新着数
                else:
                    new_n = len(new_res)

                self._new_cnt[idx] = new_n
                if th_cur:
                    self._res_cnt[idx] = len(th_cur.res_list) - 1

                if new_n == 0:
                    # 新着なし・そうだね数のみ更新
                    _sd = getattr(th_cur, "_sd_update", {}) if th_cur else {}
                    if _sd and view and not view.isHidden():
                        self._sd_apply.emit(view, _sd)
                    # 新着なしでも段階更新間隔は再計算する（global_maxが変化している可能性）
                    _th0 = th_cur
                    # max_saved が未取得(0)ならキャッシュから補完して自己修復
                    if entry.max_saved <= 0:
                        _bk0 = entry.url.rsplit("/res/", 1)[0] + "/"
                        _ms0 = self._settings.max_saved_by_board.get(_bk0, 0)
                        if _ms0 > 0:
                            entry.max_saved = _ms0
                    if getattr(entry, 'adaptive_intervals', None) and entry.max_saved > 0:
                        _o0 = self._settings.global_max_no_by_board.get(
                            entry.url.rsplit("/res/", 1)[0] + "/", 0)
                        if _o0 > 0:
                            _rem0 = entry.no + entry.max_saved - _o0
                            _pct0 = max(0.0, _rem0 / entry.max_saved * 100)
                            _new0 = _compute_interval_sec(entry.adaptive_intervals, _pct0)
                            if _new0 != entry.interval_sec:
                                entry.interval_sec = _new0
                            self._remain[idx] = entry.interval_sec
                    self.updated.emit(idx)
                    return

                if not th_cur:
                    # ビューにスレがない場合はフルGETにフォールバック
                    try:
                        th = self._fetcher.fetch_thread(board, no)
                    except Exception as e:
                        return
                    if not th or not th.res_list:
                        return

                # ── 段階的更新間隔を適用 ──────────────────────────────────────
                # max_saved が未取得(0)ならキャッシュから補完して自己修復
                if entry.max_saved <= 0:
                    _bk = entry.url.rsplit("/res/", 1)[0] + "/"
                    _ms = self._settings.max_saved_by_board.get(_bk, 0)
                    if _ms > 0:
                        entry.max_saved = _ms
                if getattr(entry, 'adaptive_intervals', None) and entry.max_saved > 0:
                    o = self._settings.global_max_no_by_board.get(
                        entry.url.rsplit("/res/", 1)[0] + "/", 0)
                    if o > 0:
                        # remaining = スレOP番号 + max_saved - 板の現在最大No
                        # （ステータスバーと同じ計算式）
                        remaining = entry.no + entry.max_saved - o
                        pct_rem   = max(0.0, remaining / entry.max_saved * 100)
                        new_isec  = _compute_interval_sec(entry.adaptive_intervals, pct_rem)
                        if new_isec != entry.interval_sec:
                            entry.interval_sec = new_isec
                        # _remain はすでに entry.interval_sec（旧値）でリセット済み
                        # → 新interval_secで上書きして次サイクルの待機時間を正しく設定する
                        self._remain[idx] = entry.interval_sec
                    else:
                        pass
                else:
                    pass
                # ─────────────────────────────────────────────────────────────

                # 非表示（バックグラウンド）タブにも発火する。_update_view 側で
                # isVisible() を見て、非表示なら DOM 追記せず _thread を更新して
                # _pending_redraw を立てるため、アクティブ化時に新着込みで再描画される。
                # （以前は not view.isHidden() でゲートしており、非表示の引用/画像
                #  モードタブに新着が反映されない不具合があった）
                if view is not None:
                    self._view_update.emit(view, th, entry.scroll_to_new,
                                           getattr(entry, "bouyomi", False))
                self.updated.emit(idx)
            except Exception as e:
                print(f'[AutoRefresh] _fetch error No.{entry.no}: {e}')
            finally:
                self._fetching_done.emit(_url)

        _FETCH_POOL.submit(_fetch)

    def _apply_sd_to_view(self, view, sd: dict):
        """そうだね数をDOMに反映し通知チェック（メインスレッドで実行）"""
        if not sd or app_is_shutting_down():
            return
        try:
            from shiboken6 import isValid
            if not isValid(view):
                return
        except Exception:
            pass
        for no, cnt in sd.items():
            view._view.page().runJavaScript(f"if(typeof updateSodane==='function')updateSodane({no},{cnt});")
        th = getattr(view, '_thread', None)
        if th:
            view._check_self_res_notifications(th, [])

    def _update_view(self, view, thread, scroll, bouyomi=False):
        if app_is_shutting_down():
            return
        try:
            from shiboken6 import isValid
            if not isValid(view):
                return
        except Exception:
            pass
        # 削除で本文が消えた新レスに、旧スレッドが持つ削除前の本文を引き継ぐ
        # （view._thread 差し替え前に適用。全分岐＝エラー/新着なし/差分で有効）
        if thread is not None:
            _carry_over_deleted_content(getattr(view, "_thread", None), thread)
        from futaba2b_html import thread_to_html, res_fragment_html
        import json
        _ucss = _load_user_css(self._settings)
        _ul   = getattr(self._settings, "uploader_links", [])
        _ng   = self._settings.ng_filter
        _del_nos = set(self._settings.del_res_nos.get(thread.url or "", [])) if thread else set()
        # entry.max_res_no を更新（段階更新間隔計算の精度向上）
        if thread and thread.res_list:
            _latest_no = thread.res_list[-1].no
            for _e in self._entries:
                if _e.url == getattr(thread, 'url', ''):
                    _e.max_res_no = max(getattr(_e, 'max_res_no', 0), _latest_no)
                    break
        # is_new の基準: _known_res_count（前回表示済みレス数）を使う
        # _known_res_count==0（初回）のみ thread_read_counts にフォールバック
        _base = view._known_res_count if view._known_res_count > 0                 else self._settings.thread_read_counts.get(thread.url, 0)
        for i, r in enumerate(thread.res_list):
            r.is_new = (i >= _base)
        # フッター（レス数/受信/最終更新/version）用の受信数を確定。
        # _update_view 内の thread_to_html 群は footer_html を渡さないと
        # フッター無しHTMLになり、返信モードでそれを _last_html 経由で
        # 再ロードした際にフッターが消える（画像/引用モードは毎回再生成のため無影響）。
        thread._footer_new_count = sum(1 for r in thread.res_list[1:] if r.is_new)  # OP除外

        # エラーがある場合は差分更新しない
        if thread.error:
            # 画像・引用モード中は返信HTML（エラーバナー付き）でモード表示を破壊しない
            # → 表示はそのまま維持し、エラー通知のみ行う
            _checked_e = view._mode_grp.checkedButton() if hasattr(view, '_mode_grp') else None
            _cur_mode_e = _checked_e.property("mode") if _checked_e else ""
            if _cur_mode_e in ("image", "quote"):
                _code = thread.error.split()[0] if thread.error.split() else ''
                view.thread_error.emit(thread.error)        # エラー通知（赤タブ等）は全エラー
                if _code != "404":
                    view._has_error_band = True             # 差分成功時に復旧解除させる
                if _code == "404":                          # スレ消滅のみ死亡＝自動保存
                    view.thread_dead.emit(thread.url or "")
                return
            html, _ = thread_to_html(thread, user_css=_ucss, uploaders=_ul,
                                      ng_filter=_ng, ng_settings=self._settings,
                                      del_nos=_del_nos,
                                      scroll_bottom_count=getattr(self._settings,'scroll_bottom_count',5),
                                      footer_html=view._thread_footer_html(thread),
                                      my_nos=self._get_my_nos_for_view(view, thread),
                id_warn_count=getattr(self._settings,'id_warn_count',5),
                pseudo_expiring=_is_pseudo_red_thread(thread, self._settings), sort_by_sodane=getattr(self._settings, 'sort_by_sodane', False))
            _cn = '' if 'キャッシュ表示' in (thread.error or '') else ' (キャッシュ表示)'
            banner = (f'<div style="background:#a00;color:#fff;padding:6px 8px;'
                      f'font-size:9pt;font-weight:bold;text-align:center;">'
                      f'⚠ {thread.error}{_cn}</div>')
            html = html.replace("<body>", f"<body>{banner}", 1)
            html = html.replace("</body>", f"{banner}</body>", 1)
            view._thread = thread
            view._known_res_count = 0
            view._load_html_via_tempfile(html, QUrl(thread.url or 'https://www.2chan.net/'))
            _code = thread.error.split()[0] if thread.error.split() else ''
            view.thread_error.emit(thread.error)        # エラー通知（赤タブ等）は全エラー
            if _code != "404":
                view._has_error_band = True             # 差分成功時に復旧解除させる
            if _code == "404":                          # スレ消滅のみ死亡＝自動保存
                view.thread_dead.emit(thread.url or "")
            return

        # ── 差分更新判定 ──────────────────────────────────────────────────
        # ここまで来た = エラーなしで更新成功 → エラー赤タブだった場合の復旧を通知
        # （タブのエラー赤は thread_error で付くが、自動更新での復旧時は
        #   thread_loaded/_was_error 経路を通らず赤が残るため明示的にクリアさせる）
        # 通信エラー赤帯が残っていれば解除（503等から復旧）
        if getattr(view, '_has_error_band', False):
            view._clear_error_band()
        view.thread_recovered.emit()
        _is_same_thread = (
            view._known_res_count > 0
            and view._thread is not None
            and view._thread.no == thread.no
        )

        if _is_same_thread:
            if len(thread.res_list) <= view._known_res_count:
                # 新着なし → DOM変更不要（スクロール位置も動かさない）。
                # ただし赤字/仮赤字は新着と無関係に変化しうるため、その状態が
                # 変わった時だけバナーを同期し、_last_html も次回全体再描画時に
                # 作り直されるようdirty化する（無変化時は余計な再構築をしない）。
                _old_t = view._thread
                _old_exp = bool(_old_t and (_old_t.is_expiring or _is_pseudo_red_thread(_old_t, self._settings)))
                _new_exp = bool(thread.is_expiring or _is_pseudo_red_thread(thread, self._settings))
                view._thread = thread
                if _new_exp != _old_exp:
                    view._last_html_dirty = True
                    if view.isVisible():
                        view._view.page().runJavaScript(view._expiry_banner_sync_js(thread))
                return

            prev_count = view._known_res_count
            new_res = thread.res_list[prev_count:]
            id_counts: dict[str, int] = {}
            for r in thread.res_list:
                if r.id_str:
                    id_counts[r.id_str] = id_counts.get(r.id_str, 0) + 1
            fragments, view._img_list = res_fragment_html(
                new_res,
                img_list_base=view._img_list,
                uploaders=_ul,
                ng_filter=_ng,
                ng_settings=self._settings,
                id_counts=id_counts,
                has_name_field=getattr(view._board, 'has_name_field', True),
                my_nos=self._get_my_nos_for_view(view, thread),
                id_warn_count=getattr(self._settings,'id_warn_count',5),
                del_nos=_del_nos,
            )
            view._thread = thread
            view._known_res_count = len(thread.res_list)
            # _last_html は「全体HTML」として保持したいが、ここで毎回 thread_to_html
            # で全レスを文字列化すると、自動更新のたびに（非アクティブタブ含め）
            # 1000レス規模のHTML生成がGUIスレッドで走りフリーズの原因になる。
            # _show_diff と同様に dirty フラグだけ立て、実際に _last_html が要る瞬間
            # （モード切替・タブ再表示・ログ保存）で _rebuild_last_html により遅延生成する。
            view._last_html_dirty = True

            # 現在の表示モードを確認
            _checked = view._mode_grp.checkedButton() if hasattr(view, '_mode_grp') else None
            _cur_mode = _checked.property("mode") if _checked else ""

            if _cur_mode == "image":
                # 画像モード中: appendNewReplies は呼ばず、スクロール位置保持でグリッド再描画
                if not view.isVisible():
                    view._pending_redraw = True
                else:
                    view._view.page().runJavaScript(
                        "window.scrollY",
                        lambda y, v=view: v._render_image_mode_with_scroll(int(y) if y else 0))
            elif _cur_mode == "quote":
                # 引用モード中: 同様にスクロール位置保持で再描画
                if not view.isVisible():
                    view._pending_redraw = True
                else:
                    view._view.page().runJavaScript(
                        "window.scrollY",
                        lambda y, v=view: v._render_quote_mode_with_scroll(int(y) if y else 0))
            else:
                # 通常（返信）モード: 差分追記
                # ただし非アクティブ（非表示）タブのWebViewへ appendNewReplies しても
                # 反映が失われる（DOMが描画状態でない/freeze）ことがあり、
                # 「タブ青→開いたら新着が無い」不具合になる。
                # 非表示時は追記せずフラグメントを蓄積し、アクティブ化時にまとめて
                # DOM追記する（フルリロードだとスクロール復元前に一瞬先頭が見えて
                # ちらつくため。取りこぼし時は _last_html 再ロードにフォールバック）。
                if not view.isVisible():
                    view._pending_redraw = True
                    if fragments:
                        if not hasattr(view, '_pending_frags'):
                            view._pending_frags = []
                        view._pending_frags.extend(fragments)
                elif fragments:
                    frags_json = json.dumps(fragments, ensure_ascii=False)
                    view._view.page().runJavaScript(
                        f"appendNewReplies({frags_json});" + view._expiry_banner_sync_js(thread))
                else:
                    view._view.page().runJavaScript(
                        "appendNewReplies([]);" + view._expiry_banner_sync_js(thread))
                if scroll and view.isVisible():
                    QTimer.singleShot(100, lambda v=view: v._view.page().runJavaScript(
                        "var el=document.querySelector('.new-res');"
                        "if(el)el.scrollIntoView({behavior:'smooth',block:'start'});"
                        "else window.scrollTo(0,document.body.scrollHeight);"))
            # そうだね数をDOMに反映
            _sd = getattr(thread, "_sd_update", {})
            if _sd:
                self._apply_sd_to_view(view, _sd)
            # 棒読みちゃん送信（差分更新）
            if bouyomi:
                _base2 = view._known_res_count - len(new_res)
                self._speak_bouyomi(thread.res_list[_base2:])
            view._notify_ng_word_match(new_res)
            view._check_self_res_notifications(thread, new_res)
            if hasattr(view, '_apply_heatmap'):
                view._apply_heatmap()   # 新着でヒートマップの分布を更新
            return

        # ── 全体再描画（フォールバック） ──────────────────────────────────
        html, _ = thread_to_html(thread, user_css=_ucss, uploaders=_ul,
                                  ng_filter=_ng, ng_settings=self._settings,
                                  del_nos=_del_nos,
                                  scroll_bottom_count=getattr(self._settings,'scroll_bottom_count',5),
                                  footer_html=view._thread_footer_html(thread),
                                  my_nos=self._get_my_nos_for_view(view, thread), id_warn_count=getattr(self._settings,'id_warn_count',5),
                                  pseudo_expiring=_is_pseudo_red_thread(thread, self._settings), sort_by_sodane=getattr(self._settings, 'sort_by_sodane', False))
        view._thread = thread
        view._known_res_count = len(thread.res_list)
        # _last_html を更新（モード切替・ログ保存で使われる）
        view._last_html = html
        view._last_html_dirty = False
        # 画像・引用モード中は返信HTMLをロードせずモード用HTMLで再描画
        _checked_f = view._mode_grp.checkedButton() if hasattr(view, '_mode_grp') else None
        _cur_mode_f = _checked_f.property("mode") if _checked_f else ""
        if _cur_mode_f in ("image", "quote"):
            view._set_view_mode(_cur_mode_f)
            if bouyomi:
                self._speak_bouyomi(thread.res_list)
            return
        view._load_html_via_tempfile(html, QUrl(thread.url or 'https://www.2chan.net/'))
        if scroll:
            QTimer.singleShot(800, lambda v=view: v._view.page().runJavaScript(
                "var el=document.querySelector('.new-res');"
                "if(el)el.scrollIntoView({behavior:'smooth',block:'start'});"
                "else window.scrollTo(0,document.body.scrollHeight);"))
        # 棒読みちゃん送信（フォールバック全体再描画）
        if bouyomi:
            self._speak_bouyomi(thread.res_list)

    def _get_my_nos_for_view(self, view, thread) -> set:
        """ARマネージャ用: スレッドの自分のレス番号セットを返す"""
        url = thread.url if thread else ""
        return set(self._settings.my_post_nos.get(url, []))

    def _speak_bouyomi(self, res_list: list):
        """新着レスを棒読みちゃんに送信する（BGスレッドで実行）"""
        s = self._settings
        if not getattr(s, "bouyomi_enabled", False):
            return
        host   = getattr(s, "bouyomi_host",   "localhost")
        port   = getattr(s, "bouyomi_port",   50080)
        speed  = getattr(s, "bouyomi_speed",  -1)
        tone   = getattr(s, "bouyomi_tone",   -1)
        volume = getattr(s, "bouyomi_volume", -1)
        voice  = getattr(s, "bouyomi_voice",   0)
        fmt    = getattr(s, "bouyomi_format",  "{comment}")

        import re as _re, urllib.request, urllib.parse, threading as _th
        def _send():
            for r in res_list:
                name    = (r.name or "名無しさん").strip()
                raw = _re.sub(r"<[^>]+>", "", r.comment_text or "").strip()
                raw = raw.replace("&gt;", ">").replace("&lt;", "<")\
                         .replace("&amp;", "&").replace("&quot;", '"')
                # comment_res: 引用行を含む全文（先頭100文字）
                comment_res = raw[:100]
                # comment: >で始まる引用行を除いた本文（先頭100文字）
                lines = raw.splitlines()
                no_quote = "\n".join(l for l in lines if not l.startswith(">"))
                comment = no_quote[:100]
                text = fmt.format(name=name, no=r.no,
                                  comment=comment, comment_res=comment_res)
                try:
                    params = urllib.parse.urlencode({
                        "text": text, "speed": speed, "tone": tone,
                        "volume": volume, "voice": voice,
                    })
                    url = f"http://{host}:{port}/Talk?{params}"
                    urllib.request.urlopen(url, timeout=2).read()
                except Exception:
                    pass  # 棒読みちゃん未起動などは無視
        _th.Thread(target=_send, daemon=True).start()

    def _do_catalog_reload(self, view):
        """カタログビューを安全にリロードする（メインスレッド）"""
        if app_is_shutting_down():
            return
        try:
            if view:
                view.reload()
        except RuntimeError:
            pass


class AutoRefreshDialog(QDialog):
    """自動更新ダイアログ"""

    # 段階的更新間隔の定義 (pct%, デフォルト秒, ラベル)
    # 100% ルールは常時有効・チェックボックスなし（baseline）
    # 残り行はチェックボックス付き
    # interval_sec 単位で統一（旧 interval_min との後方互換は _collect で処理）
    _ADAPTIVE_DEFS = [
        (100, 3600, "最大保存件数の 100% 以下"),  # チェックボックスなし・常時有効
        (50,  1800, "最大保存件数の  50% 以下"),
        (25,   600, "最大保存件数の  25% 以下"),
        (10,   120, "最大保存件数の  10% 以下"),
        (5,     60, "最大保存件数の   5% 以下"),
        (1,     30, "最大保存件数の   1% 以下"),
    ]
    _INTERVAL_LIST_SEC = [3600, 1800, 1200, 600, 300, 180, 120, 90, 60, 30, 20, 10, 5, 3, 1]

    def __init__(self, manager: AutoRefreshManager, parent=None,
                 init_entry: AutoRefreshEntry = None, init_view=None,
                 settings=None):
        super().__init__(parent)
        self._mgr      = manager
        self._settings = settings   # AppSettings（最後に設定した値の保存に使う）
        self._init_entry = init_entry
        self._init_view  = init_view
        self._modify_row = -1          # 変更対象の行インデックス (-1=新規)
        self._updating   = False       # itemChanged の再入防止
        self.setWindowTitle("自動更新")
        self.resize(860, 440)
        self._build()
        self._list_timer = QTimer(self)
        self._list_timer.setInterval(1000)
        self._list_timer.timeout.connect(self._refresh_list)
        self._list_timer.start()
        self._mgr.entry_removed.connect(self._on_entry_removed)
        self._mgr.entry_added.connect(self._refresh_list)   # 追加時即反映
        # 変更・追加タブに切り替わったときにアクティブビューを同期
        self._tabs.currentChanged.connect(self._on_dialog_tab_changed)
        # 初回は常に一覧タブを表示
        self._tabs.setCurrentIndex(0)

    # ── 板URLからサーバー名を抽出 (may/jun/dec 等) ──────────────────────────
    @staticmethod
    def _server_from_url(url: str) -> str:
        m = re.match(r'https?://(\w+)\.2chan\.net/', url)
        return m.group(1) if m else ""

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        # ── ツールバー ──
        self._tb = QToolBar(); self._tb.setMovable(False)
        self._act_toggle = QAction("▶ 監視中", self)
        self._act_toggle.triggered.connect(self._on_toggle_monitor)
        self._act_del = QAction("✕ 削除", self)
        self._act_del.triggered.connect(self._on_delete)
        self._tb.addAction(self._act_toggle)
        self._tb.addAction(self._act_del)
        lay.addWidget(self._tb)

        self._tabs = QTabWidget()
        lay.addWidget(self._tabs)

        # ════════════════════════════════════════════════════
        # ── 一覧タブ ──
        # ════════════════════════════════════════════════════
        list_w = QWidget()
        ll = QVBoxLayout(list_w); ll.setContentsMargins(4, 4, 4, 4)
        # 列: [☑, 板, スレ名, 新着, レス数, 更新まで, 最終更新, 停止, 更新間隔]
        self._table = QTableWidget(0, 9)
        self._table.setHorizontalHeaderLabels(
            ["☑", "板", "スレ名", "新着", "レス", "更新まで", "最終更新", "停止", "間隔"])
        _ar_hh = self._table.horizontalHeader()
        _ar_hh.setStretchLastSection(False)
        _ar_hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().hide()
        self._table.setStyleSheet("QTableWidget { font-size: 11px; }")
        # デフォルト列幅
        self._table.setColumnWidth(0, 28)
        self._table.setColumnWidth(1, 110)
        self._table.setColumnWidth(2, 200)
        self._table.setColumnWidth(3, 50)   # 新着
        self._table.setColumnWidth(4, 50)   # レス数
        self._table.setColumnWidth(5, 90)   # 更新まで
        self._table.setColumnWidth(6, 110)  # 最終更新
        self._table.setColumnWidth(7, 60)   # 停止
        self._table.setColumnWidth(8, 90)   # 間隔
        if self._settings:
            from futaba2b_dialogs import _restore_col_widths
            _restore_col_widths(self._table, self._settings, "table_col_widths_ar")
        _ar_hh.sectionResized.connect(self._on_ar_col_resized)
        # ヘッダクリックでソート（毎秒再構築と競合しないよう手動ソートで実装）
        self._sort_col  = -1    # -1=ソートなし
        self._sort_asc  = True
        _ar_hh.setSectionsClickable(True)
        _ar_hh.sectionClicked.connect(self._on_ar_header_clicked)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        ll.addWidget(self._table)
        self._tabs.addTab(list_w, "一覧")

        # ════════════════════════════════════════════════════
        # ── 変更タブ ──
        # ════════════════════════════════════════════════════
        mod_w = QWidget()
        al = QVBoxLayout(mod_w)
        al.setContentsMargins(8, 8, 8, 8)
        al.setSpacing(6)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._lbl_view  = QLabel(self._init_entry.title if self._init_entry else "")
        self._lbl_view.setWordWrap(False)
        self._lbl_board = QLabel(self._init_entry.board_name if self._init_entry else "")
        self._lbl_url   = QLabel(self._init_entry.url if self._init_entry else "")
        self._lbl_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("スレ名：", self._lbl_view)
        form.addRow("板：",     self._lbl_board)
        form.addRow("URL：",    self._lbl_url)
        al.addLayout(form)
        _hint = QLabel("※ アクティブなタブが自動的に反映されます（一覧でダブルクリックすると選択）")
        _hint.setStyleSheet("color: gray; font-size: 9pt;")
        _hint.setWordWrap(True)
        al.addWidget(_hint)

        # ── 段階的更新間隔テーブル ──
        _is_cat = bool(self._init_entry and getattr(self._init_entry, 'is_catalog', False))
        al.addWidget(QLabel("自動更新間隔：" if _is_cat else
                            "更新間隔（残り件数の割合に応じて自動切替）："))

        # デフォルト設定 or 最後に使った設定を初期値に
        _s = self._settings
        if _s is None:
            last_vals = [60, 30, 10, 2, 1]
            last_chks = [False, False, False, False]
        elif _is_cat and getattr(_s, "ar_use_default_catalog", False):
            _c_ivals = getattr(_s, "ar_default_catalog_intervals", [60])
            last_vals = _c_ivals + [30, 10, 2, 1]  # カタログは1行だけ使うが長さ合わせ
            last_chks = [False, False, False, False]
        elif not _is_cat and getattr(_s, "ar_use_default_thread", False):
            last_vals = list(getattr(_s, "ar_default_thread_intervals", [60, 30, 10, 2, 1]))
            last_chks = list(getattr(_s, "ar_default_thread_checks",    [False, False, False, False]))
        else:
            last_vals = list(getattr(_s, "ar_last_intervals", [60, 30, 10, 2, 1]))
            last_chks = list(getattr(_s, "ar_last_checks",    [False, False, False, False]))
        defaults = [pct_def for _, pct_def, _ in self._ADAPTIVE_DEFS]
        while len(last_vals) < len(self._ADAPTIVE_DEFS):
            last_vals.append(defaults[len(last_vals)])
        while len(last_chks) < len(self._ADAPTIVE_DEFS) - 1:   # 100%行はカウントしない
            last_chks.append(False)

        # 板設定「デフォルト更新間隔」枠と同じデザイン:
        #   各行 = 「ラベル: [チェック「有効」][スピン]」
        #   チェックはスピンの左隣、100%〜5%は分・1%は秒、チェックOFFでスピン編集不可
        #   内部値は秒で保持（_collect_adaptive_intervals で変換）、表示/入力時のみ分↔秒変換
        from PySide6.QtWidgets import QFormLayout as _QFormLayout
        _rows_w = QWidget(); _rows_form = _QFormLayout(_rows_w)
        _rows_form.setContentsMargins(0, 0, 0, 0)
        self._adaptive_rows: list = []  # (QCheckBox or None, QSpinBox, pct, row_w, lbl_widget, is_sec)
        chk_idx = 0   # last_chks のインデックス（100%行はスキップ）
        for i, (pct, _default_sec, label) in enumerate(self._ADAPTIVE_DEFS):
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(6)

            is_sec = (pct == 1)   # 1%行のみ秒、それ以外は分

            if pct == 100:
                chk = None
            else:
                chk = QCheckBox("有効")
                chk.setChecked(last_chks[chk_idx])
                row_lay.addWidget(chk)
                chk_idx += 1

            sp = _NoWheelSpinBox()
            if is_sec:
                sp.setRange(1, 99999); sp.setSuffix(" 秒")
            else:
                sp.setRange(1, 9999); sp.setSuffix(" 分")
            sp.setFixedWidth(90)
            # last_vals[i] は秒。分行は分に変換して表示
            _sec_val = last_vals[i]
            sp.setValue(_sec_val if is_sec else max(1, _sec_val // 60))
            row_lay.addWidget(sp)
            row_lay.addStretch()

            # チェックOFFでスピン編集不可（板設定と同じ挙動）
            if chk is not None:
                chk.toggled.connect(sp.setEnabled)
                sp.setEnabled(chk.isChecked())

            # カタログ時: 100%行のラベルを「自動更新間隔」に、50〜1%行は非表示
            _lbl_text = "自動更新間隔" if (_is_cat and pct == 100) else label
            lbl_w = QLabel(_lbl_text)
            _rows_form.addRow(lbl_w, row_w)
            if _is_cat and pct != 100:
                lbl_w.hide(); row_w.hide()
            self._adaptive_rows.append((chk, sp, pct, row_w, lbl_w, is_sec))
        al.addWidget(_rows_w)

        # ── 更新停止 ──
        al.addSpacing(4)
        stop_grp = QWidget()
        stop_lay = QHBoxLayout(stop_grp)
        stop_lay.setContentsMargins(0, 0, 0, 0)
        stop_lay.setSpacing(4)
        stop_lay.addWidget(QLabel("更新停止："))
        self._cmb_stop_h = _NoWheelComboBox(); self._cmb_stop_h.addItem("--")
        self._cmb_stop_m = _NoWheelComboBox(); self._cmb_stop_m.addItem("--")
        for h in range(24): self._cmb_stop_h.addItem(f"{h:02d}")
        for m in range(0, 60, 5): self._cmb_stop_m.addItem(f"{m:02d}")
        self._spin_stop_after = _NoWheelSpinBox()
        self._spin_stop_after.setRange(0, 1440); self._spin_stop_after.setFixedWidth(60)
        stop_lay.addWidget(self._cmb_stop_h)
        stop_lay.addWidget(self._cmb_stop_m)
        stop_lay.addWidget(self._spin_stop_after)
        stop_lay.addWidget(QLabel("分後"))
        stop_lay.addStretch()
        al.addWidget(stop_grp)

        # ── オプション ──
        self._chk_scroll  = QCheckBox("新着レス位置までスクロールする")
        self._chk_scroll.setChecked(False)
        self._chk_bouyomi = QCheckBox("新着レスを棒読みちゃんで読み上げる")
        al.addWidget(self._chk_scroll)
        al.addWidget(self._chk_bouyomi)
        if _is_cat:
            self._chk_scroll.hide()
            self._chk_bouyomi.hide()
        al.addStretch()

        # ── 適用ボタン ──
        self._btn_apply = QPushButton("追加"); self._btn_apply.setFixedWidth(80)
        self._btn_apply.clicked.connect(self._on_apply)
        btn_row = QHBoxLayout(); btn_row.addStretch(); btn_row.addWidget(self._btn_apply)
        al.addLayout(btn_row)

        self._tabs.addTab(mod_w, "変更・追加")

        if self._init_entry:
            self._fill_form_from_entry(self._init_entry)

        self._refresh_list()

    # ── 一覧の表示更新 ───────────────────────────────────────────────────────

    def _on_dialog_tab_changed(self, idx: int):
        """ダイアログタブ切り替え: 変更・追加タブ表示時にアクティブビューを反映"""
        if idx == 1 and self._modify_row < 0:
            self._sync_active_view()

    def _sync_active_view(self):
        """変更タブのフォームを現在アクティブなビューに同期する"""
        parent = self.parent()
        if not parent:
            return
        get_inner = getattr(parent, '_active_inner', None)
        if not get_inner:
            return
        inner = get_inner()
        if not inner:
            return
        w = inner.currentWidget()
        if w is None:
            return

        # ThreadView
        th = getattr(w, '_thread', None)
        if th and th.url:
            # 既に登録済みなら既存エントリを使う
            existing = self._mgr.find_by_url(th.url)
            if existing is not None:
                self._fill_form_from_entry(existing, keep_intervals=False)
            else:
                from futaba2b_models import AutoRefreshEntry as _ARE
                e = _ARE(
                    no=th.no,
                    url=th.url,
                    title=th.title or f"No.{th.no}",
                    board_name=th.board.name if th.board else "",
                    is_catalog=False,
                )
                self._fill_form_from_entry(e, keep_intervals=True)
            return

        # CatalogView
        board = getattr(w, '_board', None)
        if board:
            from futaba2b_models import AutoRefreshEntry as _ARE
            cat_url = board.base_url + "futaba.php?mode=cat"
            # 既に登録済みなら既存エントリを使う（interval_minが正しく反映される）
            existing = self._mgr.find_by_url(cat_url) if hasattr(self._mgr, 'find_by_url') else None
            if existing is not None:
                self._fill_form_from_entry(existing, keep_intervals=False)
            else:
                e = _ARE(
                    no=0,
                    url=cat_url,
                    title=f"カタログ - {board.name}",
                    board_name=board.name,
                    is_catalog=True,
                )
                self._fill_form_from_entry(e, keep_intervals=True)

    def _on_ar_header_clicked(self, col: int):
        """ヘッダクリックでソート列・方向を切り替え。同列再クリックで昇降逆転。"""
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        # ソートインジケータを表示（Qt標準の△▽）
        h = self._table.horizontalHeader()
        h.setSortIndicatorShown(True)
        h.setSortIndicator(col, Qt.SortOrder.AscendingOrder if self._sort_asc else Qt.SortOrder.DescendingOrder)
        self._refresh_list()

    def _refresh_list(self):
        # 変更タブが表示中かつ手動選択中でない場合、アクティブビューを反映
        if self._tabs.currentIndex() == 1 and self._modify_row < 0:
            self._sync_active_view()
        self._updating = True
        n = self._mgr.entry_count()
        self._table.setRowCount(n)

        # ── ソート順の決定 ──
        # 各エントリの「ソートキー」を col 番号に応じて生成し、エントリインデックス順を決める
        _sc = getattr(self, "_sort_col", -1)
        order = list(range(n))
        if _sc >= 1:  # Col0(checkbox)はソート対象外
            def _key(i):
                e   = self._mgr.entry(i)
                rem = self._mgr.remaining(i)
                new = self._mgr.new_count(i)
                res = self._mgr.res_count(i)
                if _sc == 1:   return self._server_from_url(e.url) + e.board_name
                if _sc == 2:   return e.title.lower()
                if _sc == 3:   return -new           # 新着: 多い順が「昇順」(降順に見せるため負)
                if _sc == 4:   return -res           # レス数: 同上
                if _sc == 5:   return rem            # 更新まで
                if _sc == 6:   return e.last_update_str
                if _sc == 7:   return f"{e.stop_hour:02d}:{e.stop_min:02d}"
                if _sc == 8:   return e.interval_sec
                return i
            order.sort(key=_key, reverse=not self._sort_asc)

        # row→エントリインデックスのマッピングを保存（行ハンドラで使う）
        self._row_to_entry_idx: dict = {row: idx for row, idx in enumerate(order)}

        for row, i in enumerate(order):
            e   = self._mgr.entry(i)
            rem = self._mgr.remaining(i)
            new = self._mgr.new_count(i)
            m, s = divmod(rem, 60)
            rem_str  = f"あと{m}分{s:02d}秒" if e.enabled else "停止中"
            stop_str = f"{e.stop_hour:02d}:{e.stop_min:02d}" if e.stop_hour >= 0 else "--"

            # 板名にサーバー名を付加
            server = self._server_from_url(e.url)
            board_display = f"{e.board_name}({server})" if server else e.board_name

            # Col 0: チェックボックス
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Checked if e.enabled else Qt.CheckState.Unchecked)
            self._table.setItem(row, 0, chk)

            # スレ名から「 - 板名」部分を削除
            title_short = re.sub(r'\s*-\s*[^-]+$', '', e.title).strip() \
                          if ' - ' in e.title else e.title

            # 更新間隔を「60/30/10/2/1」形式（分）で生成
            adaptive = getattr(e, 'adaptive_intervals', [])
            if adaptive:
                parts = []
                for r in adaptive:
                    if not r.get("enabled", True):
                        continue
                    if "interval_sec" in r:
                        sec = max(1, int(r["interval_sec"]))
                    else:
                        sec = max(1, int(r.get("interval_min", 60))) * 60
                    m, s = divmod(sec, 60)
                    parts.append(f"{m}:{s:02d}" if s else str(m))
                ivstr = "/".join(parts) if parts else "-"
            else:
                m, s = divmod(e.interval_sec, 60)
                ivstr = f"{m}:{s:02d}" if s else str(m)

            # Col 1-8: データ（新着の右にレス数を追加）
            res_total = self._mgr.res_count(i)
            res_str = str(res_total) if (res_total > 0 and not e.is_catalog) else ""
            for j, v in enumerate([board_display, title_short, f"{new}",
                                    res_str, rem_str, e.last_update_str, stop_str, ivstr], 1):
                item = QTableWidgetItem(v)
                if not e.enabled:
                    item.setForeground(QColor("#888"))
                elif j == 5 and rem < 10:   # Col5=更新まで: 残り10秒未満は赤
                    item.setForeground(QColor("#E00"))
                else:
                    from futaba2b_const import ThemeManager as _TM2
                    item.setForeground(QColor(_TM2.ui("text_primary", "#e8e8e8")))
                self._table.setItem(row, j, item)
        self._updating = False
        self._act_toggle.setText("⏸ 停止中" if not self._mgr.is_active() else "▶ 監視中")

    def _on_ar_col_resized(self, *_):
        if self._settings:
            from futaba2b_dialogs import _save_col_widths
            _save_col_widths(self._table, self._settings, "table_col_widths_ar")

    def _row_to_entry_idx_safe(self, row: int) -> int:
        """ソートを考慮して表示行→エントリインデックスに変換する。
        _row_to_entry_idx が未生成の場合は行番号をそのまま返す（後方互換）。"""
        mapping = getattr(self, "_row_to_entry_idx", None)
        if mapping is not None:
            return mapping.get(row, row)
        return row

    def _on_item_changed(self, item):
        """チェックボックス変更 → enabled トグル"""
        if self._updating or item.column() != 0:
            return
        row = item.row()
        idx = self._row_to_entry_idx_safe(row)
        if 0 <= idx < self._mgr.entry_count():
            checked = item.checkState() == Qt.CheckState.Checked
            self._mgr._entries[idx].enabled = checked

    def _on_double_click(self, row, _col):
        """ダブルクリック → 変更タブに移動して選択エントリを表示"""
        self._load_entry_to_form(row)
        self._tabs.setCurrentIndex(1)

    def _load_entry_to_form(self, row: int):
        idx = self._row_to_entry_idx_safe(row)
        if idx < 0 or idx >= self._mgr.entry_count():
            return
        self._modify_row = idx
        e = self._mgr.entry(idx)
        self._fill_form_from_entry(e)
        self._btn_apply.setText("変更")

    def _fill_form_from_entry(self, e: AutoRefreshEntry, keep_intervals: bool = False):
        """フォームにエントリの値を反映する。
        keep_intervals=True のとき adaptive_intervals が空なら既存コンボ値を維持する。"""
        self._lbl_view.setText(e.title)
        self._lbl_board.setText(e.board_name)
        self._lbl_url.setText(e.url)

        # エントリがカタログかスレかに応じて行の表示/非表示とラベルを切り替える
        is_cat = getattr(e, 'is_catalog', False)
        for _i, (pct_def, _def_sec, label) in enumerate(self._ADAPTIVE_DEFS):
            _chk, _sp, _pct, _row_w, _lbl_w, _is_sec = self._adaptive_rows[_i]
            if is_cat:
                _row_w.setVisible(_pct == 100)
                _lbl_w.setVisible(_pct == 100)
                if _pct == 100:
                    _lbl_w.setText("自動更新間隔")
            else:
                _row_w.setVisible(True)
                _lbl_w.setVisible(True)
                _lbl_w.setText(label)

        # adaptive_intervals を pct で辞書化して各行に適用（100%行はchk=None）
        adaptive = getattr(e, 'adaptive_intervals', [])
        
        if not adaptive and keep_intervals:
            return   # 新規フォーム同期時：デフォルト設定で初期化済みのスピンを維持
        rule_map = {r.get('pct'): r for r in adaptive}
        for chk, sp, pct, row_w, lbl_w, is_sec in self._adaptive_rows:
            rule = rule_map.get(pct, {})
            if chk is not None:
                chk.setChecked(rule.get("enabled", False))
            # interval_sec 優先、なければ interval_min * 60 で変換（内部は秒）
            if "interval_sec" in rule:
                val = int(rule["interval_sec"])
            elif "interval_min" in rule:
                val = int(rule["interval_min"]) * 60
            else:
                val = self._INTERVAL_LIST_SEC[0]
            # スピンには 秒行はそのまま、分行は分に変換して表示
            sp.setValue(val if is_sec else max(1, val // 60))
        self._chk_scroll.setChecked(getattr(e, 'scroll_to_new', False))
        self._chk_bouyomi.setChecked(getattr(e, 'bouyomi', False))
        if e.stop_hour >= 0:
            h_idx = e.stop_hour + 1
            m_choices = list(range(0, 60, 5))
            m_idx = (m_choices.index(e.stop_min) + 1
                     if e.stop_min in m_choices else 0)
            self._cmb_stop_h.setCurrentIndex(min(h_idx, self._cmb_stop_h.count() - 1))
            self._cmb_stop_m.setCurrentIndex(min(m_idx, self._cmb_stop_m.count() - 1))
        else:
            self._cmb_stop_h.setCurrentIndex(0)
            self._cmb_stop_m.setCurrentIndex(0)
        self._spin_stop_after.setValue(getattr(e, 'stop_after_min', 0))

    def _collect_adaptive_intervals(self) -> list:
        result = []
        for chk, sp, pct, row_w, lbl_w, is_sec in self._adaptive_rows:
            # スピンは秒行はそのまま秒、分行は分なので *60 して秒に変換
            try:
                v = max(1, int(sp.value()))
            except (ValueError, TypeError):
                v = 60
            val = v if is_sec else v * 60
            result.append({
                "enabled":      True if chk is None else chk.isChecked(),
                "pct":          pct,
                "interval_sec": val,   # 秒単位で保持
            })
        return result

    def _selected_row(self):
        rows = self._table.selectedItems()
        return self._table.currentRow() if rows else -1

    def _on_entry_removed(self, idx: int):
        self._refresh_list()

    def _on_toggle_monitor(self):
        if self._mgr.is_active(): self._mgr.stop()
        else:                     self._mgr.start()
        self._refresh_list()

    def _on_delete(self):
        r = self._selected_row()
        if r >= 0:
            idx = self._row_to_entry_idx_safe(r)
            self._mgr.remove(idx)
            self._refresh_list()

    def _on_apply(self):
        url = self._lbl_url.text().strip()
        if not url:
            return
        _is_cat_apply = bool(self._init_entry and
                             getattr(self._init_entry, 'is_catalog', False))
        try:
            no = int(url.rstrip("/").split("/")[-1].replace(".htm",""))
        except ValueError:
            if not _is_cat_apply:
                return   # カタログ以外で変換失敗は無効
            no = 0       # カタログは no=0 で登録
        sh = -1; sm = 0
        if self._cmb_stop_h.currentIndex() > 0:
            sh = int(self._cmb_stop_h.currentText())
            sm = int(self._cmb_stop_m.currentText()) if self._cmb_stop_m.currentIndex() > 0 else 0

        adaptive = self._collect_adaptive_intervals()

        # 最後に設定した値とチェック状態を記憶
        if self._settings is not None:
            self._settings.ar_last_intervals = [r["interval_sec"] for r in adaptive]
            self._settings.ar_last_checks    = [r["enabled"]      for r in adaptive]
            self._settings.save()

        # ── 実際の残り件数%でカウントダウン初期値を計算 ──────────────────
        # max_saved と global_max_no_by_board から実残り%を算出する
        # （固定 100% ではなく現在の状況に合った間隔から開始する）
        pct_remaining = 100.0
        if self._settings is not None and "/res/" in url:
            board_url = url.rsplit("/res/", 1)[0] + "/"
            o = self._settings.global_max_no_by_board.get(board_url, 0)
            max_saved = (self._init_entry.max_saved
                         if self._init_entry else 0)
            if o > 0 and max_saved > 0:
                remaining = no + max_saved - o
                pct_remaining = max(0.0, remaining / max_saved * 100)
        new_interval_sec = _compute_interval_sec(adaptive, pct_remaining)

        # ── 変更対象インデックスの確定 ───────────────────────────────────
        # _modify_row が指定されていればそのまま変更。
        # 新規追加でも同一URLが既に登録済みなら自動的に変更モードに切り替える。
        target_row = self._modify_row
        if target_row < 0:
            # URL重複チェック: 既存エントリなら変更として扱う
            for i in range(self._mgr.entry_count()):
                if self._mgr.entry(i).url == url:
                    target_row = i
                    break

        if target_row >= 0 and target_row < self._mgr.entry_count():
            # ── 既存エントリを変更 ──
            e = self._mgr.entry(target_row)
            e.adaptive_intervals = adaptive
            e.interval_sec       = new_interval_sec
            e.stop_hour          = sh; e.stop_min = sm
            e.stop_after_min     = self._spin_stop_after.value()
            e.scroll_to_new      = self._chk_scroll.isChecked()
            e.bouyomi            = self._chk_bouyomi.isChecked()
            # カウントダウンも新しい間隔に追従（短くなる場合のみ即反映）
            self._mgr.update_remain(target_row, new_interval_sec)
            self._modify_row = -1
            self._btn_apply.setText("追加")
        else:
            # ── 新規追加 ──
            # 1000レス到達スレは追加を拒否
            _th = getattr(self._init_view, '_thread', None)
            if _th and getattr(_th, 'is_full', False):
                QMessageBox.information(
                    self, "自動更新に追加できません",
                    f"「{self._lbl_view.text()}」は1000レスに達しているため自動更新に追加できません。")
                return
            entry = AutoRefreshEntry(
                no=no, url=url,
                title=self._lbl_view.text(),
                board_name=self._lbl_board.text(),
                interval_sec=new_interval_sec,
                stop_hour=sh, stop_min=sm,
                stop_after_min=self._spin_stop_after.value(),
                scroll_to_new=self._chk_scroll.isChecked(),
                bouyomi=self._chk_bouyomi.isChecked(),
                adaptive_intervals=adaptive,
                max_saved=(self._init_entry.max_saved
                           if self._init_entry else 0),
                is_catalog=(self._init_entry.is_catalog
                            if self._init_entry else False),
                board_url=(self._init_entry.board_url
                           if self._init_entry else ""),
            )
            self._mgr.add(entry, self._init_view)
        self._tabs.setCurrentIndex(0)
        self._refresh_list()

    def set_entry(self, entry: AutoRefreshEntry, view=None):
        self._init_entry = entry; self._init_view = view
        self._fill_form_from_entry(entry)
        self._modify_row = -1
        self._btn_apply.setText("追加")

    def closeEvent(self, event):
        """閉じるときにタイマーを停止してリソースを解放する"""
        self._list_timer.stop()
        super().closeEvent(event)


class ImageTabView(QWidget):
    # バックグラウンドスレッド → メインスレッドへのシグナル（スレッドセーフ）
    _sig_mp4_ready    = Signal(str)   # ダウンロード完了 → ローカルパス
    _sig_mp4_progress = Signal(str)   # 進捗テキスト
    _sig_save_status  = Signal(str)   # 保存完了/失敗メッセージ
    _sig_info_text    = Signal(str)   # 情報オーバーレイ更新（BGスレッド→UI）
    _media_dl_done    = Signal(int, str, str, bool, str)  # (seq,url,kind,ok,prev_zoom) 優先DL完了
    _media_dl_progress = Signal(int, int, int)            # (seq, downloaded, total) 優先DL進捗
    _sig_clip_image    = Signal(QImage)   # BGで取得した画像をメインスレッドでクリップボードへ反映
    open_settings         = Signal()             # 設定ボタン → MainWindowが接続
    image_navigated       = Signal(str, list, int)  # 前へ/次へ → MainWindowがrecord_recent_imageに接続
    open_image_tab_bg     = Signal(str, list, int)  # 中クリック → 非アクティブで画像タブを開く

    def __init__(self, url: str, img_list: list, idx: int,
                 fetcher: FutabaFetcher, parent=None):
        super().__init__(parent)
        self._img_list = img_list; self._idx = idx; self._fetcher = fetcher
        self._src_thread_view = None  # 開いたThreadView（img_list更新追跡用）
        self._media_seq = 0     # 表示/優先DLのシーケンス（前へ次への古いDL破棄用）
        self._media_failed: set[str] = set()  # 優先DLに失敗しリモート表示にフォールバックしたURL
        self._fit_mode = True   # True=全体表示 False=等倍
        self._zoom_last_pct = 100  # 「画面に合わせる」↔%トグル用
        self._zoom_center = None   # クリック拡大時に中心にする相対座標(fx,fy)。使用後None
        # インライン MP4 プレーヤー
        self._mp_player   = None
        self._mp_audio    = None
        self._mp_video_w  = None

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)

        # ── フォルダ保存バー ──────────────────────────────────────────────
        self._folder_bar = QWidget()
        self._folder_bar.setFixedHeight(28)
        self._folder_bar_lay = QHBoxLayout(self._folder_bar)
        self._folder_bar_lay.setContentsMargins(4, 2, 4, 2)
        self._folder_bar_lay.setSpacing(3)
        self._folder_bar_lay.addStretch()
        self._fetcher_ref = fetcher  # _save_to_folder用
        self._settings_ref = None    # MainWindowから設定参照を後でセットする
        # ⚙ ボタン（フォルダバー右端、_rebuild_folder_bar で使い回す）
        self._cfg_btn = QPushButton("⚙")
        self._cfg_btn.setFixedWidth(28); self._cfg_btn.setFixedHeight(22)
        self._cfg_btn.setToolTip("画像保存の設定")
        self._cfg_btn.clicked.connect(self.open_settings.emit)
        self._folder_bar_lay.addWidget(self._cfg_btn)
        lay.addWidget(self._folder_bar)
        ctrl = QWidget(); ctrl.setFixedHeight(32)
        ctrl_lay = QHBoxLayout(ctrl)
        ctrl_lay.setContentsMargins(6, 2, 6, 2); ctrl_lay.setSpacing(6)
        self._info = QLabel()
        self._info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._info.setStyleSheet("font-size:8pt;")
        ctrl_lay.addWidget(self._info, 1)
        # ── 中央固定ナビゲーション ──
        ctrl_lay.addStretch(1)
        prev_btn = QPushButton("← 前"); prev_btn.setFixedWidth(64)
        prev_btn.clicked.connect(lambda: self._nav(-1))
        next_btn = QPushButton("次 →"); next_btn.setFixedWidth(64)
        next_btn.clicked.connect(lambda: self._nav(1))
        ctrl_lay.addWidget(prev_btn); ctrl_lay.addWidget(next_btn)
        ctrl_lay.addStretch(1)
        # ── 拡大縮小コンボ ──
        # ── 画像検索ボタン (Google Lens / ascii2d / SauceNAO) ────────────────
        def _make_search_btn(label: str, url_tpl: str) -> QPushButton:
            btn = QPushButton(label)
            btn.setFixedWidth(52)
            btn.setToolTip(url_tpl.split("?")[0])
            def _open_search(*, _tpl=url_tpl):
                from urllib.parse import quote as _q
                if not self._img_list:
                    return
                raw_url = self._img_list[self._idx].get("url", "")
                if raw_url:
                    import webbrowser as _wb
                    _open_url(_tpl.replace("{url}", _q(raw_url, safe="")))
            btn.clicked.connect(_open_search)
            return btn
        _g_btn = _make_search_btn("Google", "https://lens.google.com/uploadbyurl?url={url}")
        _g_btn.setFixedWidth(62)
        ctrl_lay.addWidget(_g_btn)
        ctrl_lay.addWidget(_make_search_btn("二次元", "https://ascii2d.net/search/url/{url}"))
        ctrl_lay.addWidget(_make_search_btn("NAO", "https://saucenao.com/search.php?url={url}"))
        # ── 拡大縮小コンボ ──
        self._zoom_combo = QComboBox()
        self._zoom_combo.addItems(["画面に合わせる", "25%", "50%", "75%", "100%", "150%", "200%", "400%"])
        self._zoom_combo.setFixedWidth(113)
        self._zoom_combo.setToolTip("拡大率 (Ctrl+ホイール・Ctrl++/−)")
        self._zoom_combo.currentTextChanged.connect(self._on_zoom_combo)
        ctrl_lay.addWidget(self._zoom_combo)
        # ── 拡大縮小 −/＋ ボタン（コンボの選択を上下させて反映）──
        _zoom_minus = QPushButton("−"); _zoom_minus.setFixedWidth(26)
        _zoom_minus.setToolTip("拡大率を下げる")
        _zoom_minus.clicked.connect(lambda: self._zoom_combo_step(-1))
        _zoom_plus = QPushButton("＋"); _zoom_plus.setFixedWidth(26)
        _zoom_plus.setToolTip("拡大率を上げる")
        _zoom_plus.clicked.connect(lambda: self._zoom_combo_step(1))
        ctrl_lay.addWidget(_zoom_minus); ctrl_lay.addWidget(_zoom_plus)
        ext_btn = QPushButton("外部ブラウザ"); ext_btn.setFixedWidth(100)
        ext_btn.clicked.connect(lambda: _open_url(
            self._img_list[self._idx]["url"]) if self._img_list else None)
        ctrl_lay.addWidget(ext_btn)
        # ── 「レス」チェックボックス ──
        self._res_chk = QCheckBox("レス")
        self._res_chk.setToolTip("現在の画像のレスを右上に表示")
        self._res_chk.setFixedWidth(48)
        self._res_chk.toggled.connect(self._on_res_chk_toggled)
        ctrl_lay.addWidget(self._res_chk)
        # ── 「情報」チェックボックス ──
        self._info_chk = QCheckBox("情報")
        self._info_chk.setToolTip("画像に埋め込まれた情報を右下に表示")
        self._info_chk.setFixedWidth(52)
        self._info_chk.toggled.connect(self._on_info_chk_toggled)
        ctrl_lay.addWidget(self._info_chk)
        lay.addWidget(ctrl)

        # ── WebEngine ビュー（画像・WebM 用）────────────────────────────
        profile = QWebEngineProfile(self)  # off-the-record: ディスクキャッシュなし
        profile.setHttpUserAgent(UA); profile.setUrlRequestInterceptor(Interceptor())
        # ローカルキャッシュ(file://)からの画像/webm表示と、リモートURLフォールバックの
        # 両方を許可する（ページのベースURLを file:// にするため両属性が必要）。
        profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self._view = _ImageWebView(_DebugPage(profile, profile), self)
        self._view.setZoomFactor(_default_zoom())
        self._view.loadFinished.connect(self._inject_fit_bridge)
        self._media_dl_done.connect(self._on_media_dl_done)
        self._view.copy_image_requested.connect(self._copy_image_to_clipboard)
        self._sig_clip_image.connect(self._apply_clip_image)   # BG→メインでクリップボード反映
        lay.addWidget(self._view, 1)

        # ── 情報オーバーレイ（右下・半透明・テキスト選択可） ────────────
        self._info_overlay = QTextEdit(self)
        self._info_overlay.setReadOnly(True)
        self._info_overlay.setStyleSheet(
            "QTextEdit{background:rgba(0,0,0,160);color:#e8e8e8;"
            "font-size:8pt;font-family:monospace;border:none;"
            "border-top-left-radius:6px;padding:6px;}"
        )
        self._info_overlay.setFixedWidth(320)
        self._info_overlay.setFixedHeight(200)
        self._info_overlay.hide()
        self._info_overlay_visible = False

        # ── レスオーバーレイ（右上・WebEngineView・半透明） ──────────────
        self._res_overlay_widget = QWidget(self)
        self._res_overlay_widget.setFixedWidth(500)
        self._res_overlay_widget.setFixedHeight(220)
        self._res_overlay_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        res_ov_lay = QVBoxLayout(self._res_overlay_widget)
        res_ov_lay.setContentsMargins(0, 0, 0, 0)
        res_ov_profile = QWebEngineProfile(self)  # off-the-record
        res_ov_profile.setHttpUserAgent(UA)
        res_ov_profile.setUrlRequestInterceptor(Interceptor())
        # file:// 経由でロードしたHTMLからhttps://サムネイルを読み込めるようにする
        # （これが無いとレス元のサムネイル画像が表示されない）
        res_ov_profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self._res_overlay_view = QWebEngineView(_DebugPage(res_ov_profile, res_ov_profile), self._res_overlay_widget)
        self._res_overlay_view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._res_overlay_view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        res_ov_lay.addWidget(self._res_overlay_view)
        self._res_overlay_widget.hide()
        self._res_overlay_visible = False

        # ── MP4 インライン再生コンテナ ───────────────────────────────────
        self._mp_ctr = QWidget(self); self._mp_ctr.hide()
        mp_lay = QVBoxLayout(self._mp_ctr); mp_lay.setContentsMargins(0, 0, 0, 0)
        mp_lay.setSpacing(0)
        # ローディングラベル
        self._mp_lbl = QLabel("⏳ ダウンロード中...", self._mp_ctr)
        self._mp_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mp_lbl.setStyleSheet("background:#222;color:#888;font-size:11pt;min-height:200px;")
        mp_lay.addWidget(self._mp_lbl, 1)
        # MP4 用コントロールバー
        mp_bar = QWidget(self._mp_ctr); mp_bar.setFixedHeight(34)
        mp_bar.setStyleSheet("QWidget{background:#1e1e1e;color:#ddd;}"
                             "QPushButton{background:#333;border:none;color:#ddd;"
                             "padding:2px 8px;font-size:13px;}"
                             "QPushButton:hover{background:#555;}")
        mb = QHBoxLayout(mp_bar); mb.setContentsMargins(6, 2, 6, 2); mb.setSpacing(6)
        self._mp_btn = QPushButton("▶"); self._mp_btn.setFixedWidth(34)
        self._mp_btn.clicked.connect(self._mp_toggle)
        mb.addWidget(self._mp_btn)
        from PySide6.QtWidgets import QSlider as _QS
        _ss = ("QSlider::groove:horizontal{background:#444;height:4px;border-radius:2px;}"
               "QSlider::sub-page:horizontal{background:#999;height:4px;border-radius:2px;}"
               "QSlider::handle:horizontal{background:#ddd;width:12px;height:12px;"
               "margin:-4px 0;border-radius:6px;}")
        self._mp_seek = _QS(Qt.Orientation.Horizontal, mp_bar)
        self._mp_seek.setRange(0, 10000)
        self._mp_seek.sliderPressed.connect(self._mp_seek_start)
        self._mp_seek.sliderReleased.connect(self._mp_seek_end)
        self._mp_seek.setStyleSheet(_ss)
        mb.addWidget(self._mp_seek, 1)
        self._mp_lbl_time = QLabel("--:-- / --:--", mp_bar); self._mp_lbl_time.setFixedWidth(88)
        self._mp_lbl_time.setStyleSheet("color:#aaa;font-size:8pt;")
        mb.addWidget(self._mp_lbl_time)
        mb.addWidget(QLabel("🔊", mp_bar))
        mp_vol = _QS(Qt.Orientation.Horizontal, mp_bar)
        mp_vol.setRange(0, 100); mp_vol.setValue(80); mp_vol.setFixedWidth(72)
        mp_vol.setStyleSheet(_ss)
        mb.addWidget(mp_vol)
        self._mp_vol = mp_vol
        self._mp_vol.valueChanged.connect(self._on_vol_changed)
        mp_lay.addWidget(mp_bar)
        lay.addWidget(self._mp_ctr, 1)

        self._mp_dur = 0; self._mp_seeking = False
        self._img_page_ready = False  # 静止画ページ初期化済みフラグ

        self._sig_mp4_ready.connect(self._on_mp4_ready)
        self._sig_mp4_progress.connect(self._mp_lbl.setText)
        self._sig_save_status.connect(self._info.setText)
        self._sig_info_text.connect(self._info_overlay.setPlainText)

        # ── 読込中インジケータ（砂時計オーバーレイ） ───────────────────────
        # 前/次で画像をプリロードしている間だけ中央に表示し、表示完了で消す。
        self._img_spinner = QLabel("⏳", self)
        self._img_spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_spinner.setStyleSheet(
            "QLabel{background:rgba(0,0,0,150);color:#fff;font-size:30pt;"
            "border-radius:10px;padding:12px 16px;}")
        self._img_spinner.hide()
        # キャッシュ即読込時のチラ見え防止: 150ms 経過後に初めて表示
        self._img_spinner_timer = QTimer(self)
        self._img_spinner_timer.setSingleShot(True)
        self._img_spinner_timer.timeout.connect(self._do_show_img_spinner)
        # 取りこぼし時に出しっぱなしを防ぐ保険タイマー
        self._img_spinner_safety = QTimer(self)
        self._img_spinner_safety.setSingleShot(True)
        self._img_spinner_safety.timeout.connect(self._hide_img_spinner)

        # ── ダウンロード進捗バー（大きい画像・動画の優先DL中に中央表示） ────────
        from PySide6.QtWidgets import QProgressBar as _QPB
        self._dl_bar = _QPB(self)
        self._dl_bar.setFixedWidth(260)
        self._dl_bar.setTextVisible(True)
        self._dl_bar.setStyleSheet(
            "QProgressBar{background:rgba(0,0,0,170);color:#fff;border:1px solid #555;"
            "border-radius:6px;height:22px;text-align:center;font-size:9pt;}"
            "QProgressBar::chunk{background:#3a7bd5;border-radius:5px;}")
        self._dl_bar.hide()
        self._media_dl_progress.connect(self._on_media_dl_progress)

        self._show_current()

    # ── 砂時計オーバーレイ制御 ─────────────────────────────────────────────
    def _show_img_spinner(self):
        """読込開始: 150ms 後に砂時計を表示（即読込時はチラ見えさせない）。"""
        self._img_spinner_timer.start(150)
        self._img_spinner_safety.start(15000)

    def _do_show_img_spinner(self):
        if not self.isVisible():
            return
        self._position_img_spinner()
        self._img_spinner.show()
        self._img_spinner.raise_()

    def _hide_img_spinner(self):
        """読込完了/失敗: 砂時計を消す。"""
        self._img_spinner_timer.stop()
        self._img_spinner_safety.stop()
        self._img_spinner.hide()

    def _position_img_spinner(self):
        self._img_spinner.adjustSize()
        w = self._img_spinner.width()
        h = self._img_spinner.height()
        self._img_spinner.move(max(0, (self.width() - w) // 2),
                               max(0, (self.height() - h) // 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()
        if getattr(self, '_img_spinner', None) is not None and self._img_spinner.isVisible():
            self._position_img_spinner()
        if getattr(self, '_dl_bar', None) is not None and self._dl_bar.isVisible():
            self._position_dl_bar()
        # フィットモード中はリサイズ後に再フィット（デバウンス80ms）。
        # BGタブ初回表示直後のレイアウト確定もこの経路で正しいサイズに収束する
        if getattr(self, '_img_page_ready', False) and self._fit_mode:
            if not hasattr(self, '_refit_timer'):
                self._refit_timer = QTimer(self)
                self._refit_timer.setSingleShot(True)
                self._refit_timer.timeout.connect(self._apply_fit_js)
            self._refit_timer.start(80)

    def showEvent(self, event):
        super().showEvent(event)
        # 非アクティブ（バックグラウンド）で開いたタブは
        # ① 非表示のまま loadFinished → fit計算が viewport=0px で走り width=0px
        # ② 非表示のままレンダリングされた WebEngine がアクティブ化後も
        #    再コンポジットされず黒画面のまま
        # の2要因で真っ黒になる。表示された瞬間に両方を修正する。
        QTimer.singleShot(0, self._refit_on_show)

    def _refit_on_show(self):
        """表示時に再コンポジット強制＋フィット再適用（BGタブ初回表示の黒画面対策）"""
        if not self.isVisible():
            return
        # ── ① WebEngine の再コンポジットを強制（リサイズジグル）──────────
        try:
            if self._view.isVisible():
                sz = self._view.size()
                self._view.resize(sz.width(), sz.height() + 1)
                self._view.resize(sz)
        except Exception:
            pass
        # ── ② フィット再適用 ─────────────────────────────────────────────
        if not getattr(self, '_img_page_ready', False):
            # MP4ネイティブ再生中は _img_page_ready=False が正常状態。
            # ここで _show_current() を再実行すると _start_mp4 が二重に走り、
            # FFmpegロード中の QMediaPlayer を stop/破棄してクラッシュする
            # （0xC0000005 / "Immediate exit requested"）ため絶対にスキップ。
            if self._mp_player is not None or self._mp_ctr.isVisible():
                self._shown_once = True
                return
            # 非表示中にロードが完了していない/固まっている場合は
            # ページ全体を作り直す（「←前」「次→」と同じ経路で確実に復旧）
            if not getattr(self, '_shown_once', False):
                self._shown_once = True
                self._show_current()
            return
        self._shown_once = True
        if not self._fit_mode:
            return
        # レイアウト確定を待ってからフィット適用（即時だとviewportが極小）
        QTimer.singleShot(120, self._apply_fit_js)

    def _force_recomposite(self):
        """QtWebEngine の強制再コンポジット（1px resizeジグル）。
        file:// 画像を <img>.src 差し替えで表示した際、DOM/src は更新されても
        コンポジタが描画を更新せず古いフレームが残る（ウインドウ移動でしか直らない）
        問題への対処。黒画面対策の _refit_on_show と同じ手法。

        伸ばす→戻すを同一イベントループ内で連続実行すると、Qtが差分を相殺して
        リサイズイベントを1度も配送せず、再コンポジットが起きないことがある。
        1回イベントループを跨いでから元に戻すことで、確実にリサイズを2回配送する。
        """
        try:
            if not self._view.isVisible():
                return
            sz = self._view.size()
            self._view.resize(sz.width(), sz.height() + 1)
            QTimer.singleShot(0, lambda s=sz: self._restore_after_jiggle(s))
        except Exception:
            pass

    def _restore_after_jiggle(self, sz):
        """_force_recomposite で1px伸ばしたサイズを元に戻す。"""
        try:
            from shiboken6 import isValid
            if not isValid(self) or not isValid(self._view):
                return
        except Exception:
            pass
        try:
            if self._view.isVisible():
                self._view.resize(sz)
        except Exception:
            pass

    def _apply_fit_js(self):
        """現在のviewportサイズでフィットを適用するJSを実行"""
        if not getattr(self, '_img_page_ready', False) or not self._fit_mode:
            return
        if not self.isVisible():
            return
        js = (
            "window._fitMode=true;"
            "var el=document.getElementById('img');"
            "function doFit(){"
            "  var vw=window.innerWidth,vh=window.innerHeight;"
            "  if(vw<50||vh<50) return;"   # 極小viewport時は適用しない
            "  var nw=el.naturalWidth||el.videoWidth||0;"
            "  var nh=el.naturalHeight||el.videoHeight||0;"
            "  if(nw>0&&nh>0){"
            # 画面に合わせる＝上限なし（画面より小さい画像も拡大してフィット）。
            # この関数は fit_mode 中のみ実行されるため常にフィット倍率を使う。
            "    var s=Math.min(vw/nw,vh/nh);"
            "    el.style.width=Math.round(nw*s)+'px';"
            "    el.style.height='auto';"
            "    if(window._zoomState===undefined)window._zoomState='fit';"
            "  } else {"
            "    el.style.width='100%';el.style.height='auto';"
            "  }"
            "  el.style.maxWidth='none';el.style.maxHeight='none';"
            "  el.style.display='block';el.style.margin='auto';"
            "  el.style.visibility='visible';"
            "}"
            "if(el&&!el.classList.contains('actual')){"
            "  if(el.complete&&el.naturalWidth)doFit();"
            "  else el.onload=function(){if(window._fitMode)doFit();};"
            "}"
        )
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    def _reposition_overlays(self):
        """情報オーバーレイ（右下）・レスオーバーレイ（右上から75px下・3px左）を配置"""
        if self._info_overlay.isVisible():
            ow = self._info_overlay.width()
            oh = self._info_overlay.height()
            self._info_overlay.move(self.width() - ow, self.height() - oh)
            self._info_overlay.raise_()
        if self._res_overlay_widget.isVisible():
            rw = self._res_overlay_widget.width()
            self._res_overlay_widget.move(self.width() - rw - 3, 75)
            self._res_overlay_widget.raise_()


    # ── MP4 インライン再生 ─────────────────────────────────────────────────

    def _start_mp4(self, url: str):
        """MP4 をキャッシュ確認後にダウンロードし、インライン再生する"""
        # 同一URLの再入ガード：ロード/再生中に再度呼ばれると
        # _stop_mp4 がFFmpegロード中のplayerを破棄してクラッシュするため
        if getattr(self, '_mp_cur_url', None) == url and (
                self._mp_player is not None or self._mp_ctr.isVisible()):
            return
        self._stop_mp4()
        self._mp_cur_url = url
        self._view.hide()
        self._mp_lbl.setText("⏳ ダウンロード中...")
        self._mp_lbl.show()
        self._mp_ctr.show()

        cp = VideoPlayerWindow._cache_path(url)
        _mp_seq = self._media_seq   # 進捗バーのシーケンス（前へ/次へで古い進捗を破棄）

        def _download():
            import os as _os
            # ── ログのローカルソース（ネット取得しない） ──
            # file://: 保存済みファイルをそのまま再生。data:: デコードして一時再生。
            if url.startswith("file://"):
                from urllib.parse import urlparse, unquote
                p = unquote(urlparse(url).path)
                if len(p) >= 3 and p[0] == "/" and p[2] == ":":
                    p = p[1:]   # Windows: /C:/... → C:/...
                if _os.path.exists(p):
                    self._sig_mp4_ready.emit(p)
                else:
                    self._sig_mp4_progress.emit("⚠ ログ内の動画が見つかりません")
                return
            if url.startswith("data:"):
                try:
                    import base64 as _b64
                    b64data = url.split(",", 1)[1] if "," in url else ""
                    raw = _b64.b64decode(b64data)
                    tmp = cp.with_name(cp.name + f".{threading.get_ident()}.part")
                    with open(tmp, 'wb') as f:
                        f.write(raw)
                    _os.replace(tmp, cp)
                    self._sig_mp4_ready.emit(str(cp))
                except Exception as e:
                    self._sig_mp4_progress.emit(f"⚠ ログ動画の展開に失敗:\n{e}")
                return
            # キャッシュHIT時はヘッダ検証（破損ファイルをFFmpegに渡すとクラッシュ）
            if cp.exists():
                if _video_cache_valid(cp):
                    self._sig_mp4_ready.emit(str(cp))
                    return
                try: cp.unlink()   # 破損ファイルは削除して再ダウンロード
                except OSError: pass
            import os as _os
            tmp = cp.with_name(cp.name + f".{threading.get_ident()}.part")
            try:
                r = self._fetcher.session.get(url, stream=True, timeout=(15, 3600))
                r.raise_for_status()
                total = int(r.headers.get('content-length', 0))
                with open(tmp, 'wb') as f:
                    downloaded = 0
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk); downloaded += len(chunk)
                            if total > 0:
                                pct = downloaded * 100 // total
                                self._sig_mp4_progress.emit(
                                    f"⏳ ダウンロード中... {pct}% "
                                    f"({downloaded//1048576}/{total//1048576} MB)")
                                self._media_dl_progress.emit(_mp_seq, downloaded, total)
                _os.replace(tmp, cp)   # 完了後にアトミックに本パスへ
                self._sig_mp4_ready.emit(str(cp))
            except Exception as e:
                import traceback; traceback.print_exc()
                try: tmp.unlink(missing_ok=True)
                except OSError: pass
                self._sig_mp4_progress.emit(f"⚠ ダウンロード失敗:\n{e}")

        threading.Thread(target=_download, daemon=True).start()

    def _on_mp4_ready(self, path: str):
        """ダウンロード完了 → QMediaPlayer で再生"""
        if getattr(self, '_dl_bar', None) is not None:
            self._dl_bar.hide()   # DL完了 → 進捗バーを消す
        # 既に別画像/動画へ切り替え済みの場合、古い完了シグナルは無視
        _cur_url = getattr(self, '_mp_cur_url', None)
        if not _cur_url:
            return
        try:
            # 期待されるローカルパス: 通常はDLキャッシュパス。ログ動画(file://)は
            # _download がキャッシュを経由せず元ファイルのパスをそのまま emit する
            # ため、そのパスも正として扱う（キャッシュパスとだけ比較すると正当な
            # ready が破棄され「ダウンロード中...」のまま止まる）。
            _ok_paths = {str(VideoPlayerWindow._cache_path(_cur_url))}
            if _cur_url.startswith("file://"):
                from urllib.parse import urlparse, unquote
                _p = unquote(urlparse(_cur_url).path)
                if len(_p) >= 3 and _p[0] == "/" and _p[2] == ":":
                    _p = _p[1:]   # Windows: /C:/... → C:/...
                _ok_paths.add(_p)
            if path not in _ok_paths:
                return
        except Exception:
            pass
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except ImportError:
            self._mp_lbl.setText("⚠ QtMultimedia が見つかりません")
            return

        # 既存プレーヤーを破棄（_stop_mp4 は _mp_cur_url をクリアするため保持・復元）
        _cur = getattr(self, '_mp_cur_url', None)
        self._stop_mp4()
        self._mp_cur_url = _cur

        import os as _os
        print(f"[VID] ready path={path} size={_os.path.getsize(path) if _os.path.exists(path) else -1}", flush=True)
        print("[VID] step1: QAudioOutput", flush=True)
        self._mp_audio  = QAudioOutput(self)
        self._mp_audio.setVolume(self._mp_vol.value() / 100.0)

        print("[VID] step2: QMediaPlayer", flush=True)
        self._mp_player = QMediaPlayer(self)
        print("[VID] step3: setAudioOutput", flush=True)
        self._mp_player.setAudioOutput(self._mp_audio)
        self._mp_player.playbackStateChanged.connect(self._mp_on_state)
        self._mp_player.positionChanged.connect(self._mp_on_pos)
        self._mp_player.durationChanged.connect(self._mp_on_dur)

        # QVideoWidget を _mp_ctr の先頭に挿入（ローディングラベルの上）
        print("[VID] step4: QVideoWidget", flush=True)
        self._mp_video_w = QVideoWidget(self._mp_ctr)
        self._mp_video_w.setStyleSheet("background:#000;")
        self._mp_ctr.layout().insertWidget(0, self._mp_video_w, 1)
        self._mp_lbl.hide()
        self._mp_video_w.show()

        print("[VID] step5: setVideoOutput", flush=True)
        self._mp_player.setVideoOutput(self._mp_video_w)
        print("[VID] step6: setSource", flush=True)
        self._mp_player.setSource(QUrl.fromLocalFile(path))
        # タブが現在表示中の場合のみ再生開始（バックグラウンドタブでは再生しない）
        if self.isVisible():
            print("[VID] step7: play", flush=True)
            self._mp_player.play()
        print("[VID] step8: done", flush=True)

    def _stop_mp4(self):
        """プレーヤーを停止・破棄（破棄順序が重要：0xC0000005対策）
        setVideoOutput(None)/setAudioOutput(None) はPySide6で
        クラッシュ報告があるため使わず、player削除→遅延widget削除の順序で
        dangling sink を回避する。"""
        self._mp_cur_url = None
        if self._mp_player:
            print("[VID] stop: player teardown", flush=True)
            try:
                # ロード/再生を確実に中断（ソースクリアでFFmpegローダー停止）
                self._mp_player.stop()
                self._mp_player.setSource(QUrl())
            except Exception:
                pass
            try:
                self._mp_player.deleteLater()
            except Exception:
                pass
            self._mp_player = None
        # widget/audio はplayerのdeleteLaterが処理された後に削除されるよう
        # 300ms 遅延（playerが先に消えればsinkへのフレーム配送は起きない）
        _w, _a = self._mp_video_w, self._mp_audio
        if _w is not None:
            try:
                self._mp_ctr.layout().removeWidget(_w)
                _w.hide()
            except Exception:
                pass
        if _w is not None or _a is not None:
            def _late_delete(w=_w, a=_a):
                for obj in (w, a):
                    if obj is not None:
                        try: obj.deleteLater()
                        except Exception: pass
            QTimer.singleShot(300, _late_delete)
        self._mp_video_w = None
        self._mp_audio = None
        self._mp_dur = 0

    def pause_media(self):
        """タブが非アクティブになったとき音声・動画を一時停止する"""
        # MP4（QMediaPlayer）を一時停止
        if self._mp_player:
            try:
                from PySide6.QtMultimedia import QMediaPlayer as _MP
                if self._mp_player.playbackState() == _MP.PlaybackState.PlayingState:
                    self._mp_player.pause()
            except Exception:
                pass
        # WebM / GIF（WebEngineView）の動画要素を一時停止
        try:
            self._view.page().runJavaScript(
                "var v=document.querySelector('video');if(v&&!v.paused)v.pause();")
        except Exception:
            pass

    # ── MP4 コントロール ───────────────────────────────────────────────────

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000; return f"{s//60:02d}:{s%60:02d}"

    def _mp_toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        if not self._mp_player: return
        if self._mp_player.playbackState() == _MP.PlaybackState.PlayingState:
            self._mp_player.pause()
        else:
            self._mp_player.play()

    def _mp_on_state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        self._mp_btn.setText("⏸" if state == _MP.PlaybackState.PlayingState else "▶")

    def _mp_on_pos(self, pos: int):
        if not self._mp_seeking and self._mp_dur > 0:
            self._mp_seek.blockSignals(True)
            self._mp_seek.setValue(int(pos * 10000 / self._mp_dur))
            self._mp_seek.blockSignals(False)
        self._mp_lbl_time.setText(f"{self._fmt(pos)} / {self._fmt(self._mp_dur)}")

    def _mp_on_dur(self, dur: int):
        self._mp_dur = dur

    def _mp_seek_start(self):
        self._mp_seeking = True
        if self._mp_player: self._mp_player.pause()

    def _mp_seek_end(self):
        if self._mp_player and self._mp_dur > 0:
            self._mp_player.setPosition(int(self._mp_seek.value() * self._mp_dur / 10000))
            self._mp_player.play()
        self._mp_seeking = False

    # ── 表示切替 ───────────────────────────────────────────────────────────

    def update_img_list(self, new_img_list: list):
        """スレ更新後にimg_listを差し替えてカウント表示を更新する"""
        if not new_img_list:
            return
        # 現在表示中の画像URLで新リスト内の位置を特定
        cur_url = (self._img_list[self._idx].get("url", "")
                   if self._img_list and 0 <= self._idx < len(self._img_list) else "")
        self._img_list = new_img_list
        if cur_url:
            for i, entry in enumerate(new_img_list):
                if entry.get("url", "") == cur_url:
                    self._idx = i
                    break
        # インデックスが範囲外になった場合はクランプ
        self._idx = max(0, min(self._idx, len(new_img_list) - 1))
        # カウント表示のみ更新（画像の再ロードはしない）
        if 0 <= self._idx < len(new_img_list):
            info = new_img_list[self._idx]
            name = info.get("name", info.get("url", "").split("/")[-1])
            self._info.setText(
                f"{self._idx+1}/{len(new_img_list)}  No.{info.get('res_no','')}  {name}")

    def _show_current(self):
        if not self._img_list:
            return
        if not (0 <= self._idx < len(self._img_list)):
            return
        # 表示シーケンスを進める（実行中の優先DLは古いものとして破棄される）
        self._media_seq += 1
        if getattr(self, '_dl_bar', None) is not None:
            self._dl_bar.hide()   # 前画像の進捗バーが残らないよう毎回隠す
        info = self._img_list[self._idx]
        # 情報オーバーレイが表示中なら更新
        if getattr(self, '_info_overlay_visible', False):
            self._load_image_info()
        # レスオーバーレイが表示中なら更新
        if getattr(self, '_res_overlay_visible', False):
            self._show_res_overlay()
        url  = info.get("url", ""); name = info.get("name", url.split("/")[-1])
        self._info.setText(f"{self._idx+1}/{len(self._img_list)}  No.{info.get('res_no','')}  {name}")
        # 親タブウィジェットのタブ名を更新（「画像」固定にならないよう）
        p = self.parent()
        if p is not None:
            from PySide6.QtWidgets import QTabWidget as _QTW
            tw = p if isinstance(p, _QTW) else p.parent() if isinstance(getattr(p, 'parent', lambda: None)(), _QTW) else None
            if tw is None:
                # parentWidget() チェーンを上る
                _w = self
                while _w is not None:
                    if isinstance(_w.parentWidget(), _QTW):
                        tw = _w.parentWidget(); break
                    _w = _w.parentWidget()
            if tw is not None:
                i = tw.indexOf(self)
                if i >= 0:
                    short = name[:14] if name else "画像"
                    tw.setTabText(i, f"🖼 {short}")
        # ローカルキャッシュ(data/img・video_cache)があれば file:// で表示し再DLを避ける。
        # 無ければ優先DL→保存→完了後に再描画（DL中はNoneを返しspinner表示）。
        disp = self._resolve_or_download(url, self._media_seq)
        if disp is None:
            return
        url = disp
        _lo = url.lower()
        if _lo.startswith('data:'):
            # ログ(MHT)の data:URI は拡張子が無いので MIME で判定する
            _mime = _lo[5:].split(';', 1)[0].split(',', 1)[0]
            is_native_video = _mime in ('video/mp4', 'video/quicktime', 'video/x-m4v')
            is_web_video    = _mime in ('video/webm',)
        else:
            ext = _lo.split('?')[0].rsplit('.', 1)[-1] if '.' in _lo else ''
            is_native_video = ext in ('mp4', 'mov', 'm4v')
            is_web_video    = ext in ('webm',)  # gifはimgタグで表示（アニメーション対応）
        # 現在の拡大率を継承（前へ/次へ時にリセットしない）
        _prev_zoom = self._zoom_combo.currentText()  # 継承用
        self._fit_mode = (_prev_zoom == "画面に合わせる")
        base_css = (
            # html/body の背景・高さモデルは !important で固定する。user.css は base_css の
            # 後ろに連結され、user.css の body{background:#FFFFEE} が画像ウインドウ背景を
            # 上書きして白/黒分裂を起こすため、ここを user.css より優先させる。
            "html{margin:0;padding:0;width:100%;height:100%;background:#222 !important;}"
            "body{margin:0!important;padding:0!important;width:100%;min-height:100% !important;background:#222 !important;}"
            # justify-content/align-items に safe を付与: 画像がビューポートより
            # 大きい場合、通常の center だと左/上にはみ出した部分がスクロール不可
            # （scrollLeft が負にならない）になり左側が永久に見切れる。safe は
            # はみ出し時に start へフォールバックするため左端までスクロールできる。
            "body{display:flex;justify-content:safe center;align-items:safe center;"
            "overflow:auto;box-sizing:border-box;}"
            "img,video{display:block;cursor:zoom-in;visibility:hidden;user-select:none;-webkit-user-drag:none;}"
            "#img.actual{max-width:none!important;max-height:none!important;"
            "width:auto!important;}"
            # pannable = ドラッグで表示位置を動かせる状態（100%含む全ての%表示）。
            # 従来は actual(=100%専用) にカーソル/ドラッグが結合しており、
            # 200%/400%等でも手アイコンにならずパンできなかった。
            "#img.pannable{cursor:grab;}"
            "#img.pannable.dragging{cursor:grabbing;}"
        )
        # user.css をハードコードCSSの後に連結（後勝ちで user.css 側が優先適用される）
        _ucss_b = _load_user_css(self._settings_ref) if self._settings_ref else ""
        if _ucss_b:
            base_css = base_css + _ucss_b
        if is_native_video:
            self._hide_img_spinner()
            self._img_page_ready = False  # MP4→静止画切替でJS差し替え不可
            self._is_media_page  = True
            self._start_mp4(url)
        elif is_web_video:
            self._hide_img_spinner()
            self._stop_mp4()
            self._img_page_ready = False  # 動画→静止画切替でJS差し替え不可
            self._is_media_page  = True   # 次ナビは必ずsetHtml（src差し替え不可）
            self._mp_ctr.hide(); self._view.show()
            # 動画は #img 用のフィットJS（doFit/jspz）の対象外で、CSSの
            # visibility:hidden のままになり何も表示されない。専用の
            # インラインスクリプトで loadedmetadata 時にフィット＋可視化する。
            _vfit = "true" if _prev_zoom == "画面に合わせる" else "false"
            _vid_js = (
                "(function(){"
                "function fitV(){"
                "  var v=document.querySelector('video'); if(!v) return;"
                "  var vw=window.innerWidth,vh=window.innerHeight;"
                "  var nw=v.videoWidth||0,nh=v.videoHeight||0;"
                "  if(nw>0&&nh>0){"
                # 画面に合わせる: 小さい動画も拡大。それ以外は原寸
                f"    var s={_vfit}?Math.min(vw/nw,vh/nh):1;"
                "    v.style.width=Math.round(nw*s)+'px'; v.style.height='auto';"
                "  } else { v.style.maxWidth='100%'; v.style.maxHeight='100vh'; }"
                "  v.style.maxWidth='none'; v.style.maxHeight='none';"
                "  v.style.display='block'; v.style.margin='auto';"
                "  v.style.visibility='visible';"
                "}"
                "document.addEventListener('DOMContentLoaded',function(){"
                "  var v=document.querySelector('video'); if(!v) return;"
                "  if(v.videoWidth>0) fitV(); else v.addEventListener('loadedmetadata',fitV);"
                "  window.addEventListener('resize',fitV);"
                "});"
                "})();"
            )
            self._view.setHtml(
                f'<html><head><style>{base_css}</style>'
                f'<script>{_vid_js}</script></head>'
                f'<body><video src="{url}" controls autoplay loop>'
                f'お使いのブラウザは動画に対応していません</video></body></html>',
                self._html_base())
            # フィットはインラインJSで処理するため pending は立てない
            self._pending_fit  = False
            self._pending_zoom = None
        else:
            self._is_media_page = False
            self._stop_mp4()
            self._mp_ctr.hide(); self._view.show()
            self._show_img_spinner()   # 読込完了(__imgloaded__/loadFinished)で消す
            _hover_cmt = info.get("comment", "").replace("\\", "\\\\").replace("'", "\\'")\
                             .replace("\n", " ").replace("\r", "")[:100]
            click_js = (
                "document.addEventListener('DOMContentLoaded',function(){"
                "  var el=document.getElementById('img');"
                "  if(!el) return;"
                # ── クリックでズームトグル ──
                "  var _dragMoved=false;"
                "  el.addEventListener('mousedown',function(e){"
                "    if(e.button!==0) return;"
                "    if(!el.classList.contains('pannable')) return;"  # fit時はドラッグ無効（%表示は全て可）
                "    _dragMoved=false;"
                "    var startX=e.clientX,startY=e.clientY;"
                "    var startSX=window.scrollX,startSY=window.scrollY;"
                "    el.classList.add('dragging');"
                "    function onMove(ev){"
                "      var dx=ev.clientX-startX,dy=ev.clientY-startY;"
                "      if(Math.abs(dx)>3||Math.abs(dy)>3) _dragMoved=true;"
                "      window.scrollTo(startSX-dx,startSY-dy);"
                "    }"
                "    function onUp(){"
                "      el.classList.remove('dragging');"
                "      document.removeEventListener('mousemove',onMove);"
                "      document.removeEventListener('mouseup',onUp);"
                "    }"
                "    document.addEventListener('mousemove',onMove);"
                "    document.addEventListener('mouseup',onUp);"
                "    e.preventDefault();"
                "  });"
                "  el.addEventListener('click',function(e){"
                "    if(_dragMoved){_dragMoved=false;return;}"  # ドラッグ後はクリック無視
                # 拡大率トグルは Python 側(_on_fit_title)で判定する。JS側で
                # fit/%/100% の3状態を持つと取り違えて空振り・ちらつきが起きるため、
                # クリックは通知のみ行い、Python が現在の _fit_mode に基づき
                # 「画面に合わせる ↔ 直近%」を切り替える。
                # クリック位置の相対座標(0〜1)も渡し、フィット→拡大時に
                # その位置を中心にできるようにする。
                "    var fx=0.5,fy=0.5,rw=el.clientWidth,rh=el.clientHeight;"
                "    if(rw>0&&rh>0){"
                "      fx=Math.min(1,Math.max(0,e.offsetX/rw));"
                "      fy=Math.min(1,Math.max(0,e.offsetY/rh));"
                "    }"
                "    document.title='__imgclick__:'+fx.toFixed(4)+','+fy.toFixed(4);"
                "  });"
                "  el.addEventListener('mouseenter',function(){"
                "    document.title='__hover__:{}';"
                "  });"
                "  el.addEventListener('mouseleave',function(){"
                "    document.title='__hoverout__';"
                "  });"
                "  el.addEventListener('auxclick',function(e){"
                "    if(e.button===1){e.preventDefault();document.title='__midclick__';}"
                "  });"
                "});"
                "window._fitMode=true;"
            )
            # hoverコメントをf-stringで埋め込む
            click_js = click_js.replace(
                "'__hover__:{}';",
                f"'__hover__:{_hover_cmt}';")
            import json as _json
            # 読込完了時に適用するサイズ指定JS片（前回ズームを継承）
            # window._fitMode はページ内の「現在フィットモードか」の生きた状態。
            # フィット表示時に #img へ恒久登録される load リスナー(doFit)が、
            # %表示へ切替後の src 差し替え（前へ/次へ）でも発火してフィットサイズに
            # 上書きする（→一瞬フィット表示→220ms後に%へ戻る、のちらつき）ため、
            # 全サイズ適用JSでこのフラグを同期し、リスナー側はフィット時のみ動作させる。
            if _prev_zoom == "画面に合わせる":
                _size_js = ("var s=Math.min(vw/nw,vh/nh);"
                            "el.style.width=Math.round(nw*s)+'px';el.style.height='auto';"
                            "el.classList.remove('actual');el.classList.remove('pannable');"
                            "window._zoomState='fit';"
                            "window._fitMode=true;")
            else:
                try:
                    _pct = int(_prev_zoom.rstrip('%'))
                except ValueError:
                    _pct = 100
                if _pct == 100:
                    _size_js = ("el.style.width=nw+'px';el.style.height='auto';"
                                "el.classList.add('actual');el.classList.add('pannable');"
                                "window._zoomState='100';"
                                "window._fitMode=false;")
                else:
                    _size_js = (f"el.style.width=Math.round(nw*{_pct}/100)+'px';el.style.height='auto';"
                                f"el.classList.remove('actual');el.classList.add('pannable');"
                                f"window._zoomState='fit';"
                                f"window._fitMode=false;")
            if self._img_page_ready:
                _esc = _json.dumps(url)
                # プリロード＋アトミック差し替え:
                # 旧画像を表示したまま new Image() で新画像をデコードし、完了時に
                # src・サイズ・可視を一括適用する。中間状態（原寸/空白）が描画されず
                # ちらつかない。リモートURLのままなのでオリジン制約も無い。
                swap_js = (
                    "(function(){var el=document.getElementById('img');if(!el)return;"
                    # 連打対策: このswapのシーケンス番号を記録。デコード完了が
                    # ナビ順と前後しても、最新(window._imgSeq)以外は適用しない。
                    f"var _sq={self._media_seq};window._imgSeq={self._media_seq};"
                    "var tmp=new Image();"
                    "tmp.onload=function(){"
                    "  if(window._imgSeq!==_sq)return;"  # 古いナビ → 破棄
                    "  var vw=window.innerWidth,vh=window.innerHeight;"
                    "  var nw=tmp.naturalWidth||0,nh=tmp.naturalHeight||0;"
                    # el が新画像をロード完了した時点で通知（Python側で強制再コンポジット）。
                    # file:// は el の再ロードが走り、完了前に通知すると古いフレームのまま
                    # コンポジットされない（ウインドウ移動でしか直らない）問題への対処。
                    "  el.onload=function(){if(window._imgSeq!==_sq)return;document.title='__imgloaded__:'+_sq;};"
                    "  el.src=tmp.src;"
                    "  el.style.maxWidth='none';el.style.maxHeight='none';"
                    "  el.style.display='block';el.style.margin='auto';"
                    "  if(nw>0&&nh>0){" + _size_js + "}"
                    "  el.style.visibility='visible';"
                    "};"
                    "tmp.onerror=function(){if(window._imgSeq!==_sq)return;"
                    "el.onload=function(){document.title='__imgloaded__:'+_sq;};"
                    "el.src=" + _esc + ";el.style.visibility='visible';};"
                    "tmp.src=" + _esc + ";"
                    "})()"
                )
                self._view.page().runJavaScript(swap_js)
                # アトミック適用済みのため pending は不要
                self._pending_fit = False
                self._pending_zoom = None
            else:
                self._img_page_ready = False
                self._view.setHtml(
                    f'<html><head><style>{base_css}</style>'
                    f'<script>{click_js}</script></head>'
                    f'<body><img id="img" src="{url}"></body></html>',
                    self._html_base())
                if _prev_zoom == "画面に合わせる":
                    self._pending_fit = True
                else:
                    self._pending_fit = False
                    self._pending_zoom = _prev_zoom  # loadFinished後に%適用
            # 前の拡大率を継承してコンボ設定
            self._zoom_combo.blockSignals(True)
            self._zoom_combo.setCurrentText(_prev_zoom)
            self._zoom_combo.blockSignals(False)
            # ナビ(前へ/次へ)後、継承した%が確実に表示へ反映されるよう少し遅れて
            # 再適用する。swap/setHtml/file://プリロードの経路差で稀に反映漏れし、
            # 画面に合わせる相当に戻ることがあるための保証（フィット時は対象外）。
            if _prev_zoom and _prev_zoom != "画面に合わせる":
                _rz_seq = self._media_seq
                def _reassert_zoom(_z=_prev_zoom, _s=_rz_seq):
                    if _s != self._media_seq:
                        return   # 連打で別画像へ移動済み
                    if self._zoom_combo.currentText() == _z:
                        self._on_zoom_combo(_z)
                QTimer.singleShot(220, _reassert_zoom)


    # ── ローカルキャッシュ表示（②③）──────────────────────────────────────────
    def _html_base(self):
        """画像タブページの固定ベースURL（file:// オリジン）。
        file:// ローカル画像も https リモート画像も同一ページで読めるようにし、
        前へ/次への new Image() アトミック差し替えがクロススキームで失敗しないようにする。"""
        from pathlib import Path as _P
        return QUrl.fromLocalFile(str(_P("data/img").resolve()) + "/")

    def _media_cache_path(self, url: str, kind: str):
        """表示用ローカルキャッシュのパスを返す。img=data/img、webm=video_cache。
        QUrl.fromLocalFile は絶対パス必須のため必ず絶対パス化して返す
        （IMAGE_CACHE_DIR は相対パスなので resolve しないと file:// が不正になる）。"""
        try:
            if kind == 'img':
                return self._fetcher._img_disk_path(url).resolve()
            if kind == 'webm':
                return VideoPlayerWindow._cache_path(url).resolve()
        except Exception:
            pass
        return None

    def _resolve_or_download(self, url: str, seq: int):
        """表示すべき src を解決する。
        ・data:/file:/その他スキーム、mp4 はそのまま（mp4 は _start_mp4 が別途キャッシュ）
        ・http(s) の画像/webm はローカルキャッシュがあれば file:// を返す
        ・無ければ優先DLを開始し None を返す（完了後 _on_media_dl_done が再描画）
        ・DL失敗済みURLはリモートURLのまま返す（再DLループ防止）"""
        lo = (url or "").lower()
        if lo.startswith("data:"):
            # MHTログ内蔵メディア: data:のまま表示すると数MBのbase64を表示のたびに
            # setHtml/runJavaScriptでレンダラへ送ることになり非常に遅い →
            # 一時ファイルへ実体化して file:// で表示する（初回のみ変換）
            return self._dataurl_to_file(url)
        if not lo.startswith(("http://", "https://")):
            return url
        base = lo.split("?", 1)[0]
        if base.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv")):
            return url   # ネイティブ動画は _start_mp4 がキャッシュ管理
        kind = 'webm' if base.endswith(".webm") else 'img'
        if url in self._media_failed:
            return url   # 取得失敗済み → リモート表示にフォールバック
        path = self._media_cache_path(url, kind)
        if path is not None and path.exists():
            return QUrl.fromLocalFile(str(path)).toString()
        # 未キャッシュ → 優先DL（先読みプールとは別スレッド・並列可）
        # DL中はネイティブ動画プレーヤーを止めてWebビュー＋砂時計を前面化する
        self._stop_mp4()
        try:
            self._mp_ctr.hide()
        except Exception:
            pass
        self._view.show()
        self._show_img_spinner()
        import threading as _th
        def _dl(_seq=seq, _url=url, _kind=kind, _path=path):
            # 画像・webm ともストリーミングDLして進捗バーを出しつつキャッシュへ保存。
            # （画像も _img_disk_path と同一パスへ保存するので先読み/保存と整合する）
            ok = False
            try:
                ok = self._download_media_file(_url, _path, _kind, _seq)
            except Exception:
                ok = False
            self._media_dl_done.emit(_seq, _url, _kind, ok, "")
        _th.Thread(target=_dl, daemon=True).start()
        return None

    def _dataurl_to_file(self, url: str) -> str:
        """data:URI（MHTログ内蔵メディア）を一時ファイルへ実体化して file:// URL を返す。
        変換は初回のみ: URL文字列キーのメモ（同一str再利用でハッシュはキャッシュ済み）＋
        内容ハッシュ名ファイルの存在チェックで、←→ナビの再表示はファイル参照だけになる。
        変換に失敗した場合は従来どおり data: のまま返す（表示は可能・遅いだけ）。"""
        memo = getattr(self, '_dataurl_memo', None)
        if memo is None:
            memo = self._dataurl_memo = {}
        hit = memo.get(url)
        if hit:
            return hit
        try:
            import base64 as _b64, hashlib as _hl, os as _os, tempfile as _tf
            head, b64 = url.split(",", 1)
            mime = head[5:].split(";", 1)[0].strip().lower()
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                   "image/webp": ".webp", "image/bmp": ".bmp", "image/avif": ".avif",
                   "video/webm": ".webm", "video/mp4": ".mp4",
                   "video/quicktime": ".mov"}.get(mime, ".bin")
            # 内容ハッシュ: 全量md5は数MBで無駄なので 長さ+先頭4KB+末尾64B で同定
            sig = (str(len(b64)) + b64[:4096] + b64[-64:]).encode("ascii", "ignore")
            name = _hl.md5(sig).hexdigest()
            d = _os.path.join(_tf.gettempdir(), "2bp_logmedia")
            _os.makedirs(d, exist_ok=True)
            p = _os.path.join(d, name + ext)
            if not _os.path.exists(p):
                raw = _b64.b64decode(b64)
                tmp = p + f".{threading.get_ident()}.part"
                with open(tmp, "wb") as f:
                    f.write(raw)
                _os.replace(tmp, p)
            res = QUrl.fromLocalFile(p).toString()
            memo[url] = res
            return res
        except Exception:
            return url

    def _on_media_dl_progress(self, seq: int, downloaded: int, total: int):
        """優先DLの進捗をプログレスバーに反映（総量不明時は砂時計のまま）。"""
        if seq != self._media_seq:
            return
        if total < 1048576:
            return   # 総量不明 or 1MB未満の小ファイル → バーは出さず砂時計のまま
        self._hide_img_spinner()   # 砂時計を消してバーに切り替え
        pct = min(100, downloaded * 100 // total) if total else 0
        self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)
        if total >= 1048576:
            self._dl_bar.setFormat(f"%p%  ({downloaded//1048576}/{total//1048576} MB)")
        else:
            self._dl_bar.setFormat("%p%")
        self._position_dl_bar()
        self._dl_bar.show()
        self._dl_bar.raise_()

    def _position_dl_bar(self):
        self._dl_bar.adjustSize()
        self._dl_bar.setFixedWidth(260)
        w = self._dl_bar.width(); h = self._dl_bar.height()
        self._dl_bar.move(max(0, (self.width() - w) // 2),
                          max(0, (self.height() - h) // 2))

    def _on_media_dl_done(self, seq: int, url: str, kind: str, ok: bool, _pz: str):
        """優先DL完了 → 最新表示なら再描画（成功=file://表示、失敗=リモート表示）。"""
        if seq != self._media_seq:
            return   # 既に別の画像へ移動済み
        self._dl_bar.hide()
        if ok:
            # 成功扱いでも実ファイルが無ければ失敗とみなす（再DLループ防止）
            p = self._media_cache_path(url, kind)
            if not (p is not None and p.exists()):
                ok = False
        if not ok:
            self._media_failed.add(url)
        self._show_current()

    def _download_media_file(self, url: str, path, kind: str = 'img',
                             seq: int = -1) -> bool:
        """画像/webm を requests でストリーミング取得しキャッシュへ保存。成功で True。
        Content-Length が取れればチャンクごとに進捗(_media_dl_progress)を発火する。"""
        if path is None:
            return False
        import os as _os
        from urllib.parse import urlparse as _up
        tmp = str(path) + f".{__import__('threading').get_ident()}.part"
        try:
            pu = _up(url)
            segs = [s for s in pu.path.split("/") if s]
            referer = f"{pu.scheme}://{pu.hostname}/"
            if segs:
                referer += segs[0] + "/"
            hdr = {
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                          if kind == 'img' else "*/*",
                "Sec-Fetch-Dest": "image" if kind == 'img' else "video",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "same-origin",
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._fetcher.session.get(url, headers=hdr, stream=True,
                                           timeout=self._fetcher.timeout) as r:
                if not r.ok:
                    return False
                total = int(r.headers.get("content-length", 0) or 0)
                downloaded = 0
                last_emit = 0
                if seq >= 0:
                    self._media_dl_progress.emit(seq, 0, total)
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        # 進捗発火を間引く（256KBごと、または完了時）
                        if seq >= 0 and (downloaded - last_emit >= 262144
                                         or (total and downloaded >= total)):
                            last_emit = downloaded
                            self._media_dl_progress.emit(seq, downloaded, total)
            _os.replace(tmp, str(path))
            return True
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            return False

    def _set_zoom_combo_value(self, pct):
        """コンボに倍率をセット（既存項目になければ追加）"""
        if pct is None or not isinstance(pct, (int, float)):
            return
        pct = int(round(pct))
        label = f"{pct}%"
        cb = self._zoom_combo
        cb.blockSignals(True)
        idx = cb.findText(label)
        if idx < 0:
            # 数値順に挿入
            inserted = False
            for i in range(cb.count()):
                t = cb.itemText(i).rstrip("%")
                if not t.lstrip("-").isdigit(): continue  # 「画面に合わせる」等をスキップ
                if pct < int(t):
                    cb.insertItem(i, label); inserted = True; break
            if not inserted:
                cb.addItem(label)
            idx = cb.findText(label)
        cb.setCurrentIndex(idx)
        cb.blockSignals(False)

    def _zoom_combo_step(self, direction: int):
        """−/＋ボタン: 拡大率を1段階上/下へ。
        ・%表示中: コンボのインデックスを±1（並び順どおり、両端クランプ）
        ・「画面に合わせる」中: 実表示倍率を取得し、その隣の段階%へ移動する。
          （フィットの隣＝25% に飛んで急に小さくなるのを防ぐ）"""
        cb = self._zoom_combo
        n = cb.count()
        if n == 0:
            return
        if getattr(self, "_fit_mode", False):
            # フィットの実表示倍率(表示幅÷画像実寸)を取得して隣の段階へ
            self._view.page().runJavaScript(
                "(function(){var el=document.querySelector('img,video');"
                "if(!el)return 0;"
                "var nw=el.naturalWidth||el.videoWidth||0;"
                "if(!nw)return 0;"
                "return Math.round((el.offsetWidth/nw)*100);})()",
                lambda cur, d=direction: self._apply_step_from_scale(cur or 0, d))
            return
        idx = cb.currentIndex()
        new_idx = max(0, min(n - 1, idx + direction))
        if new_idx != idx:
            cb.setCurrentIndex(new_idx)   # currentTextChanged → _on_zoom_combo で反映

    def _apply_step_from_scale(self, cur_pct: int, direction: int):
        """フィット実倍率 cur_pct(%) を基準に、隣の段階%を適用する。"""
        steps = self._ZOOM_STEPS
        if not cur_pct or cur_pct <= 0:
            # 実倍率取得失敗 → 従来どおりインデックス移動でフォールバック
            cb = self._zoom_combo
            idx = cb.currentIndex()
            cb.setCurrentIndex(max(0, min(cb.count() - 1, idx + direction)))
            return
        if direction > 0:
            nxt = next((s for s in steps if s > cur_pct), steps[-1])
        else:
            nxt = next((s for s in reversed(steps) if s < cur_pct), steps[0])
        self._set_zoom_combo_value(nxt)
        self._on_zoom_combo(f"{nxt}%")

    def _on_zoom_combo(self, text: str):
        """コンボ選択→zoomFactor適用。「画面に合わせる」は画面フィット、%値は固定サイズ"""
        # クリックトグルの判定に使う表示状態を確実に同期する
        self._fit_mode = (text == "画面に合わせる")
        if text == "画面に合わせる":
            # 画像・動画をビューポートにフィット（上限なし＝画面より小さい画像も拡大）
            js = (
                "window._fitMode=true;"
                "var el=document.querySelector('img,video');"
                "if(el){"
                "  el.classList.remove('actual');el.classList.remove('pannable');"
                "  var vw=window.innerWidth,vh=window.innerHeight;"
                "  var nw=el.naturalWidth||el.videoWidth||vw;"
                "  var nh=el.naturalHeight||el.videoHeight||vh;"
                "  if(nw>0&&nh>0){"
                "    var s=Math.min(vw/nw,vh/nh);"
                "    el.style.width=Math.round(nw*s)+'px';"
                "    el.style.height='auto';"
                "    window._zoomState='fit';"
                "  } else {"
                "    el.style.width='100%';el.style.height='auto';"
                "  }"
                "  el.style.maxWidth='none';el.style.maxHeight='none';"
                "  el.style.display='block';el.style.margin='auto';"
                "  el.style.visibility='visible';"
                "}"
            )
            self._view.page().runJavaScript(js)
        else:
            try:
                pct = int(text.rstrip("%"))
            except ValueError:
                return
            self._zoom_last_pct = pct
            # クリック位置中心スクロール（フィット→拡大クリック時のみセットされる）。
            # サイズ確定後の getBoundingClientRect で実位置を測り、クリック相対点が
            # 画面中心に来るよう scrollBy する。通常のコンボ/ホイール操作では None。
            _ctr = getattr(self, "_zoom_center", None)
            self._zoom_center = None
            _center_js = ""
            if _ctr:
                _cfx, _cfy = _ctr
                _center_js = (
                    "  var _vw=window.innerWidth,_vh=window.innerHeight;"
                    "  var _r=el.getBoundingClientRect();"
                    f"  window.scrollBy(Math.round(_r.left+{_cfx}*_r.width-_vw/2),"
                    f"    Math.round(_r.top+{_cfy}*_r.height-_vh/2));"
                )
            # %はnaturalWidth基準のpx指定に変換（ビューポート幅基準だと縦長画像が切れる）
            js = (
                f"window._fitMode=false;"
                f"var el=document.querySelector('img,video');"
                f"if(el){{"
                f"  var nw=el.naturalWidth||el.videoWidth||0;"
                f"  var nh=el.naturalHeight||el.videoHeight||0;"
                f"  el.style.maxWidth='none';el.style.maxHeight='none';"
                f"  if(nw>0){{"
                f"    el.style.width=Math.round(nw*{pct}/100)+'px';"
                f"    el.style.height='auto';"
                f"  }} else {{"
                f"    el.style.width='{pct}%';el.style.height='auto';"
                f"  }}"
                f"  if({pct}===100){{el.classList.add('actual');window._zoomState='100';}}"
                f"  else{{el.classList.remove('actual');}}"
                f"  el.classList.add('pannable');"
                f"  el.style.display='block';el.style.margin='auto';"
                f"  el.style.visibility='visible';"
                + _center_js +
                f"}}"
            )
            self._view.page().runJavaScript(js)


    def _zoom_in(self):
        self._step_zoom(1)

    def _zoom_out(self):
        self._step_zoom(-1)

    def _zoom_reset(self):
        self._set_zoom_pct(100)


    def _inject_fit_bridge(self, ok: bool):
        """loadFinished後にtitleChangedをfit通知として接続（初回のみ）"""
        if not ok:
            return
        # 動画ページは #img 用のJS差し替え・フィット対象外。可視化は専用の
        # インラインJSが行う。ここで _img_page_ready を True にすると次ナビが
        # src差し替え経路に入り、#img を持たない動画ページ上で空振りして
        # 白画面になるため、False のままにして必ず setHtml させる。
        if getattr(self, '_is_media_page', False):
            self._img_page_ready = False
            return
        # 静止画ページが準備完了 → 次回ナビはJS src差し替えで白フラッシュ防止
        self._img_page_ready = True
        # setHtml 経路（初回/動画→静止画）の読込完了 → 砂時計を消す
        self._hide_img_spinner()
        if getattr(self, '_fit_title_connected', False):
            try:
                self._view.page().titleChanged.disconnect(self._on_fit_title)
            except Exception:
                pass
        self._view.page().titleChanged.connect(self._on_fit_title)
        self._fit_title_connected = True
        # 画像読み込み完了後にフィットを適用（小さい画像でも正しく拡大）
        # %指定で継承した場合はnaturalWidth取得後にpx適用
        if getattr(self, "_pending_zoom", None):
            pz = self._pending_zoom; self._pending_zoom = None
            try:
                pct = int(pz.rstrip("%"))
            except ValueError:
                pct = 100
            jspz = (
                f"window._fitMode=false;"
                f"var el=document.getElementById('img');"
                f"function applyZ(){{"
                f"  var nw=el.naturalWidth||0;"
                f"  el.style.maxWidth='none';el.style.maxHeight='none';"
                f"  el.style.width=(nw>0?Math.round(nw*{pct}/100):'auto')+'px';"
                f"  el.style.height='auto';el.style.display='block';el.style.margin='auto';"
                f"  if({pct}===100){{el.classList.add('actual');window._zoomState='100';}}"
                f"  else{{el.classList.remove('actual');}}"
                f"  el.classList.add('pannable');"
                f"  el.style.visibility='visible';}}"
                f"if(el){{if(el.complete&&el.naturalWidth)applyZ();else el.onload=applyZ;}}"
            )
            self._view.page().runJavaScript(jspz)
        if getattr(self, "_pending_fit", False):
            self._pending_fit = False
            # img.onloadを待ってからfit適用
            js = (
                "var el=document.getElementById('img');"
                "if(el){"
                # 状態を先に確定する。初回表示で load イベントの取りこぼしや
                # キャッシュ済み画像のタイミングずれで doFit が一度も走らないと、
                # 画像が原寸のまま（スクロールバーが出る）で _zoomState が未定義になり、
                # 最初のクリックが空振り（100%表示なのに拡大されない）になる。
                # 先に 'fit' を入れ、load/即時/遅延の複数経路で doFit を確実に当てる。
                "  window._zoomState='fit';"
                "  window._fitMode=true;"
                "  function doFit(){"
                "    var vw=window.innerWidth,vh=window.innerHeight;"
                "    var nw=el.naturalWidth||el.videoWidth||0;"
                "    var nh=el.naturalHeight||el.videoHeight||0;"
                "    if(nw>0&&nh>0){"
                "      var s=Math.min(vw/nw,vh/nh);"
                "      el.style.width=Math.round(nw*s)+'px';"
                "      el.style.height='auto';"
                # 画面に合わせるモードでは拡大・縮小いずれもfit状態
                # （小さい画像も表示領域に合わせて拡大する）
                "      window._zoomState='fit';"
                "    } else {"
                "      el.style.width='100%';el.style.height='auto';"
                "    }"
                "    el.style.maxWidth='none';el.style.maxHeight='none';"
                "    el.style.display='block';el.style.margin='auto';"
                "    el.style.visibility='visible';"
                "  }"
                # loadリスナーはページ再利用（src差し替えナビ）後も残り続けるため、
                # %表示へ切替後に発火してフィットへ上書きしないよう _fitMode で
                # ガードする（登録解除はできないのでフラグ制御）
                "  el.addEventListener('load',function(){if(window._fitMode)doFit();});"
                "  if(el.complete&&el.naturalWidth)doFit();"
                "  setTimeout(function(){if(window._fitMode&&el.naturalWidth&&!el.classList.contains('actual'))doFit();},60);"
                "  setTimeout(function(){if(window._fitMode&&el.naturalWidth&&!el.classList.contains('actual'))doFit();},250);"
                "}"
            )
            self._view.page().runJavaScript(js)

    def _on_fit_title(self, title: str):
        """titleを使ったJS→Python通知を受け取る"""
        if title == "__imgloaded__" or title.startswith("__imgloaded__:"):
            self._hide_img_spinner()
            self._force_recomposite()   # 新画像ロード完了 → 強制再描画でフレーム残留を防ぐ
            self._view.page().runJavaScript("document.title='';")
            return
        if title.startswith("__hover__:"):
            self._view.page().runJavaScript("document.title='';")
            return
        if title == "__hoverout__":
            self._view.page().runJavaScript("document.title='';")
            return
        if title == "__midclick__":
            self._view.page().runJavaScript("document.title='';")
            info = self._img_list[self._idx] if self._img_list and 0 <= self._idx < len(self._img_list) else None
            if info:
                self.open_image_tab_bg.emit(info.get("url", ""), self._img_list, self._idx)
            return
        if title.startswith("__imgclick__"):
            # 画像クリック: 現在の表示状態に応じて「画面に合わせる ↔ 直近%」をトグル
            self._view.page().runJavaScript("document.title='';")
            # クリック位置の相対座標(0〜1)。フィット→拡大時にその位置を中心にする。
            _ctr = None
            if title.startswith("__imgclick__:"):
                try:
                    _fx, _fy = title.split(":", 1)[1].split(",")
                    _ctr = (float(_fx), float(_fy))
                except (ValueError, IndexError):
                    _ctr = None
            if getattr(self, "_fit_mode", True):
                # フィット中 → 直近の% へ（クリック位置を中心に拡大）
                self._zoom_center = _ctr
                pct = getattr(self, "_zoom_last_pct", 100) or 100
                self._set_zoom_combo_value(pct)
                self._on_zoom_combo(f"{pct}%")
            else:
                # %表示中 → 画面に合わせる
                cb = self._zoom_combo
                cb.blockSignals(True)
                cb.setCurrentText("画面に合わせる")
                cb.blockSignals(False)
                self._on_zoom_combo("画面に合わせる")
            return
        if title == "__fit__":
            self._fit_mode = True
            cb = self._zoom_combo
            cb.blockSignals(True)
            cb.setCurrentText("画面に合わせる")
            cb.blockSignals(False)
            self._on_zoom_combo("画面に合わせる")
            self._view.page().runJavaScript("document.title='';")
        elif title == "__actual__":
            self._fit_mode = False
            self._set_zoom_combo_value(self._zoom_last_pct)
            self._on_zoom_combo(f"{self._zoom_last_pct}%")
            self._view.page().runJavaScript("document.title='';")

    _ZOOM_STEPS = [25, 50, 75, 100, 150, 200, 400]

    def _step_zoom(self, direction: int):
        try:
            cur = int(self._zoom_combo.currentText().rstrip("%"))
        except (ValueError, AttributeError):
            cur = 100
        steps = self._ZOOM_STEPS
        if direction > 0:
            nxt = next((s for s in steps if s > cur), steps[-1])
        else:
            nxt = next((s for s in reversed(steps) if s < cur), steps[0])
        self._set_zoom_pct(nxt)

    def _set_zoom_pct(self, pct: int):
        cb = self._zoom_combo
        label = f"{pct}%"
        idx = cb.findText(label)
        if idx >= 0:
            cb.setCurrentIndex(idx)
        else:
            cb.blockSignals(True)
            cb.setCurrentText(label)
            cb.blockSignals(False)
            self._on_zoom_combo(label)

    def wheelEvent(self, event):
        """Ctrl+ホイールで拡大縮小"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self._zoom_in()
            elif delta < 0:
                self._zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        """Ctrl++/−/0 で拡大縮小・等倍"""
        mod = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        key = event.key()
        if mod and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom_in(); event.accept(); return
        if mod and key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self._zoom_out(); event.accept(); return
        if mod and key == Qt.Key.Key_0:
            self._zoom_reset(); event.accept(); return
        # 左右矢印キーでナビゲーション
        if key == Qt.Key.Key_Left:
            self._nav(-1); event.accept(); return
        if key == Qt.Key.Key_Right:
            self._nav(1); event.accept(); return
        super().keyPressEvent(event)

    def _nav(self, delta: int):
        if not self._img_list:
            return
        # idx は即更新（連打分を積算）。実際の表示は最後のクリックから少し待って
        # 1回だけ行う（中間画像の高速 src 差し替えで QtWebEngine が古いフレームを
        # 描画する問題を回避するための集約＝デバウンス）。
        self._idx = (self._idx + delta) % len(self._img_list)
        t = getattr(self, '_nav_debounce', None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._do_nav_show)
            self._nav_debounce = t
        t.start(90)

    def _do_nav_show(self):
        """デバウンス後の実表示（最後に選ばれた idx を1回だけ表示）。"""
        if not self._img_list or not (0 <= self._idx < len(self._img_list)):
            return
        self._stop_mp4()
        self._show_current()
        self.image_navigated.emit(
            self._img_list[self._idx].get("url", ""),
            self._img_list, self._idx)

    def load_image(self, url: str, img_list: list, idx: int):
        """ウインドウ再利用時などに、表示中の画像を別の画像に差し替える。"""
        self._stop_mp4()
        self._img_list = img_list or []
        self._idx = idx if (img_list and 0 <= idx < len(img_list)) else 0
        self._show_current()

    def cleanup(self):
        """タブが閉じられる時のクリーンアップ"""
        self._stop_mp4()
        # _view の WebEngine リソースを解放
        try:
            blank = QWebEnginePage(self)
            self._view.setPage(blank)
        except Exception:
            pass
        try:
            self._view.deleteLater()
        except Exception:
            pass
        # レスオーバーレイの WebEngine リソースを解放
        try:
            blank2 = QWebEnginePage(self)
            self._res_overlay_view.setPage(blank2)
        except Exception:
            pass
        try:
            self._res_overlay_view.deleteLater()
        except Exception:
            pass

    def _on_vol_changed(self, v: int):
        """音量スライダー変更 → QAudioOutput反映 + 設定保存"""
        if self._mp_audio:
            self._mp_audio.setVolume(v / 100.0)
        if self._settings_ref:
            self._settings_ref.video_volume = v
            self._settings_ref.save()

    def _copy_image_to_clipboard(self, url: str):
        """画像をクリップボードへコピー。ローカルキャッシュ優先→file://→data:→リモート。"""
        import threading
        def _read_bytes() -> bytes | None:
            lo = (url or "").lower()
            # ローカルキャッシュ（data/img）があれば優先
            if lo.startswith(("http://", "https://")):
                try:
                    p = self._media_cache_path(url, 'img')
                    if p is not None and p.exists():
                        return p.read_bytes()
                except Exception:
                    pass
                r = self._fetcher.session.get(url, timeout=(10, 30))
                r.raise_for_status()
                return r.content
            if lo.startswith("file:"):
                from urllib.parse import urlparse, unquote
                pth = unquote(urlparse(url).path)
                if pth.startswith("/") and len(pth) > 2 and pth[2] == ":":
                    pth = pth[1:]   # Windows: /C:/... → C:/...
                with open(pth, "rb") as f:
                    return f.read()
            if lo.startswith("data:"):
                import base64
                head, _, b64 = url.partition(",")
                return base64.b64decode(b64) if ";base64" in head else b64.encode()
            return None
        def _fetch():
            try:
                data = _read_bytes()
                from PySide6.QtGui import QImage
                img = QImage()
                if data and img.loadFromData(data):
                    # QClipboard はGUI(メイン)スレッド専用。BGスレッドから
                    # setImage しても Windows では反映されない（コピーされない）ため、
                    # シグナル経由でメインスレッドに渡して反映する。
                    self._sig_clip_image.emit(img)
                else:
                    print(f"[IMG_COPY] QImage.loadFromData 失敗: {url}")
            except Exception as e:
                print(f"[IMG_COPY] コピー失敗: {e}")
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_clip_image(self, img):
        """クリップボードへ画像を反映（メインスレッドで実行）。
        QClipboard はGUIスレッド専用のため、BG取得後にシグナル経由でここへ渡す。"""
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setImage(img)

    def set_settings(self, settings):
        """MainWindowから設定を渡してフォルダバーを構築"""
        self._settings_ref = settings
        self._rebuild_folder_bar()
        # チェックボックスの状態を設定から復元（シグナルを一時ブロック）
        self._res_chk.blockSignals(True)
        self._info_chk.blockSignals(True)
        self._res_chk.setChecked(getattr(settings, 'img_overlay_res', False))
        self._info_chk.setChecked(getattr(settings, 'img_overlay_info', False))
        self._res_chk.blockSignals(False)
        self._info_chk.blockSignals(False)
        # 動画音量を設定から復元
        vol = getattr(settings, 'video_volume', 80)
        self._mp_vol.blockSignals(True)
        self._mp_vol.setValue(vol)
        self._mp_vol.blockSignals(False)
        # チェックがONなら表示（レイアウト確定後に配置するためQTimerで遅延）
        _res_on  = self._res_chk.isChecked()
        _info_on = self._info_chk.isChecked()
        if _res_on:
            self._res_overlay_visible = True
            self._res_overlay_widget.show()
            self._show_res_overlay()
        if _info_on:
            self._info_overlay_visible = True
            self._info_overlay.show()
            self._load_image_info()
        if _res_on or _info_on:
            QTimer.singleShot(0, self._reposition_overlays)

    def _rebuild_folder_bar(self):
        """設定からフォルダ保存ボタンを再構築。各フォルダは [名前|…|▼] のグループ:
        名前=そのフォルダへ即保存、…=フォルダ選択ダイアログ、
        ▼=サブフォルダメニュー（サブフォルダがある時のみ表示）。"""
        import os
        # ⚙ボタン以外を全て取り外して作り直す
        while self._folder_bar_lay.count():
            item = self._folder_bar_lay.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._cfg_btn:
                w.deleteLater()
        _btn_css = "QPushButton{font-size:8pt;padding:0 6px;}"
        folders = [f for f in getattr(self._settings_ref, "image_save_folders", [])
                   if (f or '').strip()] if self._settings_ref else []
        label_len = getattr(self._settings_ref, "image_save_label_len", 0)
        for folder in folders:
            base = os.path.basename(folder.rstrip('\\/')) or folder
            lbl  = (base[:label_len] if label_len > 0 else base)
            grp  = QWidget()
            gl   = QHBoxLayout(grp)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setSpacing(0)
            btn = QPushButton(lbl)
            btn.setToolTip(f"保存先: {folder}")
            btn.setFixedHeight(22)
            btn.setStyleSheet(_btn_css)
            btn.clicked.connect(lambda _=None, f=folder: self._save_to_folder(f))
            gl.addWidget(btn)
            dots = QPushButton("…")
            dots.setToolTip("保存先フォルダを選択して保存")
            dots.setFixedHeight(22); dots.setFixedWidth(20)
            dots.setStyleSheet(_btn_css)
            dots.clicked.connect(lambda _=None, f=folder: self._save_to_folder_browse(f))
            gl.addWidget(dots)
            if _has_subdir(folder):
                dd = QPushButton("▼")
                dd.setToolTip("サブフォルダを選んで保存")
                dd.setFixedHeight(22); dd.setFixedWidth(20)
                dd.setStyleSheet(_btn_css)
                dd.clicked.connect(lambda _=None, f=folder, b=dd: self._folder_dd_menu(f, b))
                gl.addWidget(dd)
            self._folder_bar_lay.addWidget(grp)
        self._folder_bar_lay.addStretch()
        self._folder_bar_lay.addWidget(self._cfg_btn)

    def _save_to_folder_browse(self, folder: str):
        """「…」: フォルダ選択ダイアログで保存先を指定して現在画像を保存"""
        import os
        from PySide6.QtWidgets import QFileDialog
        start = folder if folder and os.path.isdir(folder) else ""
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダを選択", start)
        if d:
            self._save_to_folder(d)

    def _folder_dd_menu(self, folder: str, btn):
        """「▼」: サブフォルダ選択メニューをボタン直下に表示して現在画像を保存"""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        menu = QMenu(self)
        _populate_subfolder_menu(menu, folder, self._save_to_folder)
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))

    def _save_to_folder(self, folder: str):
        """現在表示中の画像をダイアログなしで指定フォルダに保存"""
        import os, threading
        if not self._img_list or not (0 <= self._idx < len(self._img_list)):
            return
        info = self._img_list[self._idx]
        url  = info.get("url", "")
        name = info.get("name", "") or url.split("/")[-1].split("?")[0]
        if not url or not name:
            return
        dest = os.path.join(folder, name)
        os.makedirs(folder, exist_ok=True)

        def _do_save():
            try:
                r = self._fetcher.session.get(url, timeout=(15, 120))
                r.raise_for_status()
                with open(dest, "wb") as f:
                    f.write(r.content)
                self._sig_save_status.emit(f"✅ 保存完了: {os.path.basename(dest)}")
            except Exception as e:
                self._sig_save_status.emit(f"⚠ 保存失敗: {e}")

        self._info.setText(f"保存中... {name}")
        threading.Thread(target=_do_save, daemon=True).start()

    # ── 情報オーバーレイ ─────────────────────────────────────────────────

    def _on_info_chk_toggled(self, checked: bool):
        """「情報」チェックボックスのON/OFF"""
        self._info_overlay_visible = checked
        if self._settings_ref is not None:
            self._settings_ref.img_overlay_info = checked
            self._settings_ref.save()
        if checked:
            self._info_overlay.show()
            self._reposition_overlays()
            self._load_image_info()
        else:
            self._info_overlay.hide()

    def _load_image_info(self):
        """現在画像のEXIF/埋め込み情報を非同期で取得してオーバーレイに表示"""
        if not self._img_list or not (0 <= self._idx < len(self._img_list)):
            self._sig_info_text.emit("(画像なし)")
            return
        info = self._img_list[self._idx]
        url  = info.get("url", "")
        if not url:
            self._sig_info_text.emit("(URL不明)")
            return
        ext = url.lower().rsplit(".", 1)[-1].split("?")[0]
        if ext not in ("jpg", "jpeg", "png", "gif", "webp", "tiff", "tif", "bmp"):
            self._sig_info_text.emit("(対応形式外)")
            return
        self._sig_info_text.emit("⏳ 読み込み中...")

        def _fetch():
            try:
                r = self._fetcher.session.get(url, timeout=(15, 60))
                r.raise_for_status()
                data = r.content
            except Exception as e:
                self._sig_info_text.emit(f"⚠ 取得失敗:\n{e}")
                return
            lines = []
            try:
                import io
                from PIL import Image as _PILImage
                from PIL.ExifTags import TAGS as _TAGS
                img = _PILImage.open(io.BytesIO(data))
                lines.append(f"形式: {img.format or ext.upper()}")
                lines.append(f"サイズ: {img.width} × {img.height} px")
                lines.append(f"モード: {img.mode}")
                lines.append(f"ファイルサイズ: {len(data)//1024} KB")
                # EXIF
                exif_data = None
                try:
                    exif_data = img._getexif()
                except Exception:
                    pass
                if exif_data:
                    lines.append("── EXIF ──")
                    _SKIP = {
                        "MakerNote", "UserComment", "PrintImageMatching",
                        "InteroperabilityIFD", "ExifIFD", "GPSIFD",
                    }
                    for tag_id, val in exif_data.items():
                        tag = _TAGS.get(tag_id, str(tag_id))
                        if tag in _SKIP:
                            continue
                        v = str(val)
                        if len(v) > 80:
                            v = v[:80] + "…"
                        lines.append(f"{tag}: {v}")
                # PNG テキストチャンク
                if img.format == "PNG" and hasattr(img, "info") and img.info:
                    lines.append("── PNG info ──")
                    for k, v in img.info.items():
                        sv = str(v)
                        if len(sv) > 80:
                            sv = sv[:80] + "…"
                        lines.append(f"{k}: {sv}")
            except Exception as e:
                lines.append(f"(解析エラー: {e})")
            self._sig_info_text.emit("\n".join(lines))

        import threading as _thr
        _thr.Thread(target=_fetch, daemon=True).start()

    # ── レスオーバーレイ ──────────────────────────────────────────────────

    def _on_res_chk_toggled(self, checked: bool):
        """「レス」チェックボックスのON/OFF"""
        self._res_overlay_visible = checked
        if self._settings_ref is not None:
            self._settings_ref.img_overlay_res = checked
            self._settings_ref.save()
        if checked:
            self._res_overlay_widget.show()
            self._reposition_overlays()
            self._show_res_overlay()
        else:
            self._res_overlay_widget.hide()

    def _show_res_overlay(self):
        """現在画像のレスをレスオーバーレイに表示"""
        if not self._res_overlay_visible:
            return
        if not self._img_list or not (0 <= self._idx < len(self._img_list)):
            return
        info = self._img_list[self._idx]
        res_no = info.get("res_no", "")
        if not res_no:
            return
        src = self._src_thread_view
        if src is None or not hasattr(src, "_thread") or src._thread is None:
            return
        thread = src._thread
        res = next((r for r in thread.res_list if r.no == res_no), None)
        if res is None:
            return
        try:
            res_html = render_res(res, res.is_op, [])
            _ucss_o = _load_user_css(self._settings_ref) if self._settings_ref else ""
            _usr_o = f'<style>{_ucss_o}</style>' if _ucss_o else ''
            html = (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                    f'<style>{THREAD_CSS}'
                    f'html,body{{margin:0;padding:4px;overflow:auto;'
                    f'background:rgba(0,0,0,0)!important;}}'
                    f'.res{{margin:0 0 0 16px!important;float:none!important;'
                    f'background:rgba(240,224,214,0.82)!important;'
                    f'border:1px solid #800000;}}'
                    f'</style>'
                    f'{_usr_o}'
                    f'<script>'
                    f'function openImg(){{}} function openImgBg(){{}} '
                    f'function openThread(){{}} function openThreadBg(){{}} '
                    f'function quoteNo(){{}} function sodane(){{}} '
                    f'function ngRes(){{}} function delRes(){{}} '
                    f'</script>'
                    f'</head><body>{res_html}</body></html>')
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                             encoding='utf-8', delete=False) as tf:
                tf.write(html); tmp_path = tf.name
            self._res_overlay_view.setUrl(QUrl.fromLocalFile(tmp_path))
        except Exception:
            pass


class ImageWindow(QMainWindow):
    """画像表示モード=ウインドウ のときに使う専用ウインドウ。
    ImageTabView を内包し、画像タブと同じ機能を持つ。アプリ内で1つだけ
    生成して再利用する（新しい画像は同じウインドウに表示）。
    閉じても破棄せず非表示にして再利用する（WebEngine の再生成によるクラッシュ回避）。"""

    def __init__(self, image_view: "ImageTabView", settings=None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("画像")
        self._image_view = image_view
        self.setCentralWidget(image_view)
        # ジオメトリ復元
        try:
            geo = getattr(settings, "image_window_geometry", None) if settings else None
            if geo and isinstance(geo, (list, tuple)) and len(geo) == 4:
                self.setGeometry(int(geo[0]), int(geo[1]), int(geo[2]), int(geo[3]))
            else:
                self.resize(900, 700)
        except Exception:
            self.resize(900, 700)

    @property
    def image_view(self) -> "ImageTabView":
        return self._image_view

    def _save_geometry(self):
        if not self._settings:
            return
        try:
            g = self.geometry()
            self._settings.image_window_geometry = [g.x(), g.y(), g.width(), g.height()]
            self._settings.save()
        except Exception:
            pass

    def moveEvent(self, event):
        super().moveEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        # 本体ウインドウの裏（未露出）にいる間に画像を差し替えると、QtWebEngine の
        # コンポジタが新フレームを出さず前の画像が残ることがある。前面化・最小化解除
        # の直後に強制再コンポジットして取りこぼしを回収する（中クリックで裏の
        # ウインドウに読み込んだ場合など）。
        if event.type() in (QEvent.Type.ActivationChange,
                            QEvent.Type.WindowStateChange):
            iv = self._image_view
            if (iv is not None and self.isVisible() and not self.isMinimized()
                    and hasattr(iv, "_force_recomposite")):
                QTimer.singleShot(0, iv._force_recomposite)

    def closeEvent(self, event):
        # 破棄せず隠して再利用する（位置・サイズは保存）
        self._save_geometry()
        try:
            if self._image_view:
                self._image_view.pause_media()
        except Exception:
            pass
        self.hide()
        event.ignore()


# ══════════════════════════════════════════════════════════════════════════════
# スレッド履歴パネル
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# ダイアログ群は futaba2b_dialogs.py へ移動
# MainWindow は futaba2b_main_window.py へ移動
# ══════════════════════════════════════════════════════════════════════════════
