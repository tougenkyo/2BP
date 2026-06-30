"""
futaba2b_main_window.py ─ メインウィンドウ + エントリポイント
v0.6.011
"""
from __future__ import annotations
import sys, os, re, threading, webbrowser
from futaba2b_app_qt import _open_url
from pathlib import Path

from PySide6.QtCore    import Qt, QUrl, QTimer, QObject, Signal, Slot, QSize, QRect
from PySide6.QtGui     import QAction, QKeySequence, QColor, QShortcut, QIcon, QPixmap, QImage, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QDialog, QFormLayout, QSpinBox, QCheckBox, QComboBox,
    QTextEdit, QSizePolicy, QStatusBar, QGroupBox, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMenu,
    QListWidget, QListWidgetItem, QTabBar, QScrollArea, QInputDialog,
    QButtonGroup, QRadioButton, QStyle, QToolButton, QFrame, QToolTip,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore    import (
    QWebEngineProfile, QWebEnginePage,
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo,
)
from PySide6.QtWebChannel import QWebChannel

from futaba2b_models   import BoardInfo, BoardCategory, AutoRefreshEntry
from futaba2b_network  import FutabaFetcher
from futaba2b_settings import AppSettings, NgFilter, get_board_settings
from futaba2b_html     import thread_to_html, catalog_to_html, render_res, THREAD_CSS, WEBCHANNEL_JS
from futaba2b_bridge   import ThreadBridge, CatalogBridge
from futaba2b_const    import UA, ThemeManager

# ── 同パッケージからインポート ──────────────────────────────────────────────
from futaba2b_app_qt import (
    APP_VER, _DebugPage, WrapTabBar, Interceptor, InnerTabWidget,
    BoardTreePane, BoardPane,
    VideoPlayerWindow, ThreadView, CatalogView, ImageTabView, ImageWindow,
    AutoRefreshManager, AutoRefreshDialog,
    _compute_interval_sec,
    _default_zoom, _load_user_css, _theme_icon, _dispose_tab_view,
    _schedule_gc,
    _JapaneseLineEdit,
)
from futaba2b_dialogs import (
    ThreadHistoryPane, PostDialog, NgSettingsDialog, AppSettingsDialog,
    BoardSettingsDialog, BookmarkEditDialog,
)

def _pin_safe_set(inner, view, new_text: str):
    """setTabText のラッパー。ピン表示は WrapTabBar の paintEvent で行うため
    テキスト変更のみ行う。"""
    idx = inner.indexOf(view)
    if idx < 0:
        return
    cur = inner.tabText(idx)
    if cur != new_text:
        inner.setTabText(idx, new_text)


# ── アップデート機能 ──────────────────────────────────────────────────────
_UPDATE_REPO          = "tougenkyo/2BP"
_UPDATE_BRANCH        = "main"
_UPDATE_VERSION_URL   = f"https://raw.githubusercontent.com/{_UPDATE_REPO}/{_UPDATE_BRANCH}/futaba2b_app_qt.py"
_UPDATE_ZIP_URL       = f"https://codeload.github.com/{_UPDATE_REPO}/zip/refs/heads/{_UPDATE_BRANCH}"


class MainWindow(QMainWindow):
    _bbsmenu_signal   = Signal(list)   # スレッド→UIの安全な橋渡し
    _tab_icon_signal  = Signal(object, object, bytes)  # (inner, view, data) タブアイコン設定
    _catset_reload_signal = Signal(object, object)     # (CatalogView, BoardInfo) BGスレッド→UIでload()
    _catset_cxyl_signal   = Signal(object, str)        # (CatalogView, cxyl) BGスレッド→UIでset_cxyl()
    _main_thread_call     = Signal(object)             # BGスレッド→UIスレッドでcallableを実行（QTimer.singleShot代替）

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"2BP v{APP_VER}")
        self.resize(1280, 820)
        self.setAcceptDrops(True)   # D&Dでログファイルを開けるようにする
        self._ph_idx        = -1    # "2BP" プレースホルダタブのインデックス
        self._ph_widget     = None
        self._welcome_idx   = -1    # ウェルカムタブのインデックス
        self._settings     = AppSettings()
        self._fetcher      = FutabaFetcher(self._settings)
        self._image_window = None   # 画像表示モード=ウインドウ の単一インスタンス
        self._ar_mgr       = AutoRefreshManager(self._fetcher, self._settings, self)
        self._ar_dlg: "AutoRefreshDialog | None" = None
        self._ng_filter    = self._settings.ng_filter  # シングルトン参照
        self._bbsmenu_cats = []
        self._hist_visible = self._settings._app.get("hist_visible", True)
        self._current_board: BoardInfo | None = None
        self._auto_save_done:  set[str] = set()  # 自動保存済みURL（二重保存防止）
        self._auto_close_done: set[str] = set()  # 自動クローズ済みURL（再表示後はクローズしない）
        # タブアイコン設定シグナル（BGスレッド→メインスレッド）
        self._tab_icon_signal.connect(self._on_tab_icon_ready)
        self._main_thread_call.connect(lambda f: f())
        # カタログアイコンキャッシュ
        self._catalog_icon_cache: "QIcon | None" = None
        self._catalog_icon_checked: bool = False
        # 閉じたタブのスタック: [(board_url, board_name, thread_no, tab_label), ...]
        # 閉じたタブのスタック: [(board_url, board_name, thread_no, thread_url, label), ...]
        # settings から復元（永続化）
        self._closed_tabs: list[tuple] = [
            (r.get("board_url",""), r.get("board_name",""),
             r.get("thread_no",0),  r.get("thread_url",""), r.get("label",""))
            for r in getattr(self._settings, "recent_closed_list", [])
        ]
        # 最近開いた画像: [{url, name, board_name, board_url}, ...]
        self._recent_images: list[dict] = list(getattr(self._settings, "recent_images_list", []))

        self._build_ui()
        self._build_menu()
        self._bbsmenu_signal.connect(self._on_bbsmenu_loaded)
        self._catset_reload_signal.connect(lambda v, b: v.load(b))
        self._catset_cxyl_signal.connect(lambda v, c: v.set_cxyl(c))
        self._load_bbsmenu()
        self._restore_window_state()            # ウィンドウサイズ・位置を復元
        # 起動時にカタログ設定を cxyl クッキーへ反映
        self._fetcher.set_cxyl_cookie(self._settings.catalog_cxyl_str)
        # QtWebEngineのレンダラープロセスを事前起動（初回板表示時のウィンドウ消え防止）
        self._webengine_warmup()
        # Ctrl+F: _build_menu内のQShortcutで管理
        QTimer.singleShot(500, self._restore_tab_state)
        QTimer.singleShot(3000, self._startup_cache_cleanup)


    # ── UI 構築 ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        self.setStatusBar(self._status)

        # ── ステータスバー: 左寄せ・白文字・UIセパレータ ────────────────────
        self._status.setStyleSheet(
            f"QStatusBar{{background:{ThemeManager.ui('statusbar_bg','#2d2d2d')};border-top:1px solid {ThemeManager.ui('statusbar_border','#555')}}}"
            "QStatusBar::item{border:none;}"
            f"QStatusBar QLabel{{color:{ThemeManager.ui('statusbar_fg','#ccc')};padding:0 5px;font-size:8pt;}}"
            f"QProgressBar{{max-height:14px;font-size:7pt;color:{ThemeManager.ui('statusbar_fg','#ccc')};"
            f"background:{ThemeManager.ui('progress_bg','#444')};border:1px solid {ThemeManager.ui('btn_border','#666')};border-radius:2px;}}"
            f"QProgressBar::chunk{{background:{ThemeManager.ui('progress_chunk','#5588cc')}}}")

        def _lbl(txt=""):
            w = QLabel(txt)
            w.setStyleSheet(f"color:{ThemeManager.ui('statusbar_fg','#ccc')}; padding:0 5px; font-size:8pt;")
            return w

        def _sep():
            w = QWidget()
            w.setFixedSize(1, 14)
            w.setStyleSheet(f"background:{ThemeManager.ui('text_muted','#888')};")
            return w

        self._st_viewers  = _lbl()
        self._st_expiry   = _lbl()
        self._st_saved    = _lbl()
        self._st_momentum = _lbl()
        self._st_rescount = _lbl()

        from PySide6.QtWidgets import QProgressBar as _QPB
        self._st_progress = _QPB()
        self._st_progress.setFixedWidth(110); self._st_progress.setFixedHeight(14)
        self._st_progress.setTextVisible(True); self._st_progress.hide()
        self._st_progress.setStyleSheet(
            f"QProgressBar{{color:{ThemeManager.ui('statusbar_fg','#ccc')};background:{ThemeManager.ui('progress_bg','#444')};border:1px solid {ThemeManager.ui('btn_border','#666')};"
            f"border-radius:2px;font-size:7pt;text-align:center;}}"
            f"QProgressBar::chunk{{background:{ThemeManager.ui('progress_chunk','#5588cc')}}}")

        self._st_log = _lbl("起動中…")  # ログ領域（右端まで伸びる）
        self._st_scroll = _lbl()         # 末尾スクロール残回数

        # 左寄せで順番に追加（ログだけ stretch=1 で残余幅を埋める）
        for w in [self._st_viewers, _sep(),
                  self._st_expiry,  _sep(),
                  self._st_saved,   _sep(),
                  self._st_momentum,_sep(),
                  self._st_rescount,_sep(),
                  self._st_progress,_sep(),
                  self._st_scroll,  _sep()]:
            self._status.addWidget(w)
        self._status.addWidget(self._st_log, 1)  # stretch=1

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self._splitter
        self.setCentralWidget(splitter)

        # 左: 板ツリー
        self._tree_pane = BoardTreePane(self._settings, self)
        self._tree_pane.setFixedWidth(210)
        self._tree_pane.board_selected.connect(self._on_board_selected)
        self._tree_pane.custom_changed.connect(self._rebuild_tree)
        # タブペインのシグナル接続
        self._tree_pane._tab_pane.tab_update_requested.connect(
            self._on_tab_pane_update)
        self._tree_pane._tab_pane.tab_close_requested.connect(
            self._on_tab_pane_close)
        self._tree_pane._tab_pane.tab_select_requested.connect(
            self._on_tab_pane_select)
        # お気に入りペインのシグナル接続
        self._tree_pane._fav_pane.tab_update_requested.connect(
            self._on_tab_pane_update)
        self._tree_pane._fav_pane.tab_close_requested.connect(
            self._on_tab_pane_close)
        self._tree_pane._fav_pane.tab_select_requested.connect(
            self._on_tab_pane_select)
        splitter.addWidget(self._tree_pane)

        # 右: URL バー + タブ + 履歴
        right = QWidget()
        self._r_lay = QVBoxLayout(right)
        self._r_lay.setContentsMargins(0, 0, 0, 0); self._r_lay.setSpacing(0)

        # 外側タブ (板単位) ─ 板設定・URLバーは右コーナーウィジェットに配置
        self._outer_tabs = QTabWidget()
        self._outer_tab_history: list = []   # 板タブアクティブ履歴
        self._outer_prev_idx: int = -1       # 直前のアクティブ板タブ
        self._ar_dlg: AutoRefreshDialog | None = None
        self._outer_wrap_bar = WrapTabBar()   # Python 参照を直接保持
        self._outer_wrap_bar._settings = self._settings  # タブ幅設定参照用
        self._outer_tabs.setTabBar(self._outer_wrap_bar)
        self._outer_tabs.setTabsClosable(False)  # WrapTabBar が自前描画
        self._outer_tabs.setMovable(False)
        self._outer_tabs.tabBar().setUsesScrollButtons(False)

        # ── 上部バー: 自動更新 NG設定 板設定 設定 | − 100% + | [URL___________] ──
        top_bar = QWidget()
        top_lay = QHBoxLayout(top_bar)
        top_lay.setContentsMargins(2, 1, 4, 1); top_lay.setSpacing(4)

        def _smb(label, w=60):
            b = QPushButton(label); b.setFixedWidth(w); b.setFixedHeight(28)
            b.setStyleSheet("font-size:8pt; padding:0 2px;")
            return b

        ar_btn  = _smb("自動更新", 68)
        ar_btn.clicked.connect(lambda: (
            lambda inner: inner._on_open_ar() if inner else None
        )(self._active_inner()))
        ng_btn  = _smb("NG設定", 60)
        ng_btn.clicked.connect(self._open_ng_settings)
        cfg_btn = _smb("板設定", 60)
        cfg_btn.clicked.connect(self._open_board_settings)
        set_btn = _smb("設定", 52)
        set_btn.clicked.connect(self._open_settings)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet(f"color:{ThemeManager.ui('separator_color','#555')};"); sep1.setFixedWidth(6)

        self._zoom_lbl = QLabel("100%")
        self._zoom_lbl.setFixedWidth(36)
        self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_lbl.setStyleSheet(f"font-size:8pt; color:{ThemeManager.ui('text_muted','#888')};")
        zoom_in  = QPushButton("+"); zoom_in.setFixedWidth(22)
        zoom_out = QPushButton("−"); zoom_out.setFixedWidth(22)
        zoom_in.clicked.connect(lambda: self._change_zoom(+0.1))
        zoom_out.clicked.connect(lambda: self._change_zoom(-0.1))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet(f"color:{ThemeManager.ui('separator_color','#555')};"); sep2.setFixedWidth(6)

        self._url_bar = _JapaneseLineEdit()
        self._url_bar.setPlaceholderText("URL を入力… (Enter で移動)")
        self._url_bar.returnPressed.connect(self._on_url_enter)

        top_lay.addWidget(ar_btn)
        top_lay.addWidget(ng_btn)
        top_lay.addWidget(cfg_btn)
        top_lay.addWidget(set_btn)
        top_lay.addWidget(sep1)
        top_lay.addWidget(zoom_out); top_lay.addWidget(self._zoom_lbl); top_lay.addWidget(zoom_in)
        top_lay.addWidget(sep2)
        top_lay.addWidget(self._url_bar, 1)
        self._r_lay.addWidget(top_bar)

        # WrapTabBar の Python Signal に直接接続
        self._outer_tabs.tabBar().tabCloseRequested.connect(self._close_outer_tab)
        self._outer_tabs.tabBar().tabBarDoubleClicked.connect(
            lambda idx: self._close_outer_tab(idx))
        self._outer_tabs.currentChanged.connect(self._on_outer_tab_changed)
        # 2BP プレースホルダ (板が無いときだけ表示 – 起動時は作らない)
        self._ph_widget = QLabel("板ツリーから板をダブルクリックして開いてください")
        self._ph_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ph_widget.setStyleSheet(f"color:{ThemeManager.ui('text_muted','#888')}; font-size:11pt;")
        # _ph_idx = -1 のまま。全板タブが閉じられた時に _update_placeholder_visibility で追加される

        # ウェルカムタブ
        welcome_w = QWidget()
        wc_lay = QVBoxLayout(welcome_w); wc_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._welcome_lbl = QLabel(
            f"2BP ─ ふたばちゃんねる専用ブラウザ v{APP_VER}\n\n"
            "板一覧を読み込み中…")
        self._welcome_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._welcome_lbl.setStyleSheet("font-size:13pt;")
        wc_lay.addWidget(self._welcome_lbl)
        wc_lay.addSpacing(16)
        self._retry_btn = QPushButton("板一覧を再取得")
        self._retry_btn.setFixedWidth(140)
        self._retry_btn.clicked.connect(self._load_bbsmenu)
        self._retry_btn.setEnabled(False)
        wc_lay.addWidget(self._retry_btn, 0, Qt.AlignmentFlag.AlignCenter)
        self._welcome_idx = self._outer_tabs.addTab(welcome_w, "  2BP  ")
        self._outer_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self._r_lay.addWidget(self._outer_tabs, 1)

        # 履歴パネル
        self._hist_pane = ThreadHistoryPane(self._settings, self)
        self._hist_pane.thread_open_requested.connect(self._open_from_history)
        self._hist_pane.hide_requested.connect(self._toggle_history)
        if self._hist_visible:
            self._r_lay.addWidget(self._hist_pane)
        else:
            # 非表示状態で起動した場合は親を外してフローティング表示を防止
            self._hist_pane.setParent(None)

        splitter.addWidget(right)
        splitter.setSizes([210, 1070])

    def _build_menu(self):
        mb = self.menuBar()

        # ショートカットのデフォルト定義 {action_id: default_key}
        self._shortcut_defaults = {
            "exit":             "Alt+F4",
            "catalog":          "F9",
            "refresh_board":    "F6",
            "refresh_current":  "F5",
            "reply":            "Ctrl+D",
            "close_tab":        "Ctrl+W",
            "reopen_tab":       "Ctrl+Shift+T",
            "find_in_view":     "Ctrl+F",
            "extract_focus":    "Ctrl+Shift+F",
            "open_log":         "Ctrl+Shift+O",
            "toggle_tree":      "F2",
            "toggle_history":   "Shift+F2",
            "scroll_top":       "Alt+Up",
            "scroll_bottom":    "Alt+Down",
            "scroll_new":       "Alt+G",
            "scroll_prev_pos":  "Alt+H",
            "scroll_prev_bm":   "Alt+B",
            "scroll_next_bm":   "Alt+V",
        }

        def _sc(action_id: str) -> QKeySequence:
            """設定に保存されたキー → なければデフォルト"""
            key = getattr(self._settings, "shortcuts", {}).get(
                action_id, self._shortcut_defaults.get(action_id, ""))
            return QKeySequence(key) if key else QKeySequence()

        fm = mb.addMenu("ファイル(&F)")
        self._menu_recent_closed = fm.addMenu("最近閉じたスレ(&R)")
        self._menu_recent_closed.aboutToShow.connect(self._build_recent_closed_menu)
        self._menu_recent_images = fm.addMenu("最近開いた画像(&I)")
        self._menu_recent_images.aboutToShow.connect(self._build_recent_images_menu)
        fm.addSeparator()
        fm.addAction(QAction("終了(&Q)", self, triggered=self.close,
                             shortcut=_sc("exit")))

        bm = mb.addMenu("板・スレッド(&B)")
        bm.addAction(QAction("カタログ(&C)", self,
                             triggered=lambda: self._show_board_view("catalog"),
                             shortcut=_sc("catalog")))
        bm.addAction(QAction("この板の更新(&B)", self,
                             triggered=self._refresh_board, shortcut=_sc("refresh_board")))
        bm.addAction(QAction("このビューの更新(&F)", self,
                             triggered=self._refresh_current, shortcut=_sc("refresh_current")))
        bm.addSeparator()
        bm.addAction(QAction("返信(&D)…", self,
                             triggered=self._reply_current, shortcut=_sc("reply")))
        bm.addAction(QAction("スレッド作成(&R)…", self, triggered=self._new_thread))
        bm.addSeparator()
        bm.addAction(QAction("このビューを閉じる(&L)", self,
                             triggered=self._close_current_tab, shortcut=_sc("close_tab")))
        bm.addAction(QAction("閉じたタブを開き直す(&Z)", self,
                             triggered=self._reopen_closed_tab,
                             shortcut=_sc("reopen_tab")))
        bm.addSeparator()
        _find_hint = _sc("find_in_view").toString() or "Ctrl+F"
        bm.addAction(QAction(f"スレ内を検索(&F)\t{_find_hint}", self,
                             triggered=self._find_in_view))
        bm.addSeparator()
        log_menu = bm.addMenu("ログを保存(&W)")
        log_menu.addAction(QAction("HTML として保存…", self,
                                   triggered=lambda: self._save_log("html")))
        log_menu.addAction(QAction("MHT として保存…", self,
                                   triggered=lambda: self._save_log("mht")))
        log_menu.addAction(QAction("ZIP として保存…", self,
                                   triggered=lambda: self._save_log("zip")))
        log_menu.addAction(QAction("スクリーンショット(PNG) として保存…", self,
                                   triggered=self._save_log_screenshot))
        bm.addAction(QAction("ログを開く(&O)…", self,
                             triggered=self._open_log_file,
                             shortcut=_sc("open_log")))

        # ── ブックマーク（設定メニューの左） ──
        self._bookmark_menu = mb.addMenu("ブックマーク(&K)")
        self._build_bookmark_menu()

        sm = mb.addMenu("設定(&S)")
        sm.addAction(QAction("2BPの設定(&F)…", self, triggered=self._open_settings))
        sm.addAction(QAction("板の設定(&B)…", self, triggered=self._open_board_settings))
        sm.addAction(QAction("NG設定(&N)…", self, triggered=self._show_ng_settings))
        sm.addSeparator()
        sm.addAction(QAction("板ツリーを表示/非表示(&B)", self,
                             triggered=self._toggle_tree, shortcut=_sc("toggle_tree")))
        sm.addAction(QAction("スレッド履歴を表示/非表示(&H)", self,
                             triggered=self._toggle_history,
                             shortcut=_sc("toggle_history")))
        sm.addSeparator()
        sm.addAction(QAction("お気に入りに追加(&A)", self, triggered=self._add_to_favorites))

        hm = mb.addMenu("ヘルプ(&H)")
        hm.addAction(QAction("アップデートを確認(&U)…", self, triggered=self._check_for_update))
        hm.addAction(QAction("GitHub 2BPを開く", self,
            triggered=lambda: _open_url("https://github.com/tougenkyo/2BP")))
        hm.addSeparator()
        hm.addAction(QAction("バージョン情報(&A)…", self, triggered=self._show_about))

        # Ctrl+F は WindowShortcut で一元管理（メニューに表示しない）
        self._sc_find = QShortcut(QKeySequence(_sc("find_in_view") or QKeySequence("Ctrl+F")), self, self._find_in_view)

    # ── ブックマーク ─────────────────────────────────────────────────────────
    def _build_bookmark_menu(self):
        """ブックマークメニューを設定内容から再構築する。
        最上部に「ブックマークを編集」、区切り線の後に各ブックマークを並べる。"""
        m = getattr(self, "_bookmark_menu", None)
        if m is None:
            return
        m.clear()
        m.addAction(QAction("ブックマークを編集(&E)…", self,
                            triggered=self._open_bookmark_edit))
        m.addSeparator()
        for bm in (getattr(self._settings, "bookmarks", None) or []):
            if bm.get("sep"):
                m.addSeparator()
                continue
            title = bm.get("title", "") or bm.get("url", "")
            url   = bm.get("url", "")
            if not url:
                continue
            m.addAction(QAction(title, self,
                triggered=lambda checked=False, u=url: _open_url(u)))

    def _open_bookmark_edit(self):
        """ブックマーク編集ウィンドウを開き、OKなら保存してメニューを再構築する。"""
        dlg = BookmarkEditDialog(getattr(self._settings, "bookmarks", []) or [], self)
        if dlg.exec():
            self._settings.bookmarks = dlg.bookmarks()
            try:
                self._settings.save()
            except Exception:
                pass
            self._build_bookmark_menu()

    # ── bbsmenu ─────────────────────────────────────────────────────────────

    def _load_bbsmenu(self):
        self._st_log.setText("板一覧を取得中…")
        self._welcome_lbl.setText(
            f"2BP ─ ふたばちゃんねる専用ブラウザ v{APP_VER}\n\n"
            "板一覧を読み込み中…")
        if hasattr(self, "_retry_btn"):
            self._retry_btn.setEnabled(False)
        threading.Thread(target=self._do_load_bbsmenu, daemon=True).start()

    def _do_load_bbsmenu(self):
        # bbsmenu は 10秒でタイムアウト
        orig = self._fetcher.timeout
        self._fetcher.timeout = 10
        try:
            cats = self._fetcher.fetch_board_menu()
        finally:
            self._fetcher.timeout = orig
        self._bbsmenu_signal.emit(cats)  # Signal 経由でメインスレッドに渡す

    def _on_bbsmenu_loaded(self, cats: list):
        if hasattr(self, "_retry_btn"):
            self._retry_btn.setEnabled(True)
        # デフォルト3板がまだ未登録なら追加する（初回起動判定）
        _default_urls = {b.url for b in self._DEFAULT_NIJI_BOARDS}
        _registered_urls = {
            b.get("url", "")
            for g in self._settings.custom_board_groups
            for b in g.get("boards", [])
        }
        # 既存エントリの name が古い場合（"img"等）→「二次元裏」に修正
        _need_save = False
        for _g in self._settings.custom_board_groups:
            for _bd in _g.get("boards", []):
                if _bd.get("url", "") in _default_urls and _bd.get("name") != "二次元裏":
                    _bd["name"] = "二次元裏"
                    _need_save = True
        if not _default_urls.issubset(_registered_urls):
            for _b in self._DEFAULT_NIJI_BOARDS:
                self._settings.add_board_to_group("二次元裏", _b.name, _b.url)
            _need_save = True
        if _need_save:
            self._settings.save()
        if not cats:
            self._st_log.setText("⚠ 板一覧取得失敗 ─ URLバーから直接板を開けます")
            self._welcome_lbl.setText(
                "⚠ 板一覧の取得に失敗しました\n\n"
                "下のフォームに板のURLを入力して開くか、\n"
                "左ツリーのお気に入りから開いてください")
            self._rebuild_tree()
            return
        self._bbsmenu_cats = cats
        self._rebuild_tree()
        total = sum(len(c.boards) for c in cats)
        self._st_log.setText(f"板一覧取得完了: {total} 板")
        self._welcome_lbl.setText(
            f"2BP ─ ふたばちゃんねる専用ブラウザ v{APP_VER}\n\n"
            f"左の板ツリーから板をダブルクリックして開いてください\n"
            f"({total} 板を読み込みました)")

    def _rebuild_tree(self):
        merged, custom_urls = self._merge_categories(self._bbsmenu_cats, self._settings)
        if not merged:
            return
        # デフォルト3板は★・青色表示しない
        _default_urls = {b.url for b in self._DEFAULT_NIJI_BOARDS}
        display_custom_urls = custom_urls - _default_urls
        self._tree_pane.set_categories(merged, display_custom_urls)

    # デフォルト二次元裏板（bbsmenu_cache.html が存在しない場合のフォールバック）
    _DEFAULT_NIJI_BOARDS = [
        BoardInfo(name="二次元裏", url="https://img.2chan.net/b/futaba.htm"),
        BoardInfo(name="二次元裏", url="https://cgi.2chan.net/b/futaba.htm"),
        BoardInfo(name="二次元裏", url="https://dat.2chan.net/b/futaba.htm"),
    ]

    def _merge_categories(self, bbsmenu_cats: list, settings) -> tuple:
        """bbsmenu + カスタムを合成し二次元裏を先頭にまとめる"""
        custom_urls: set = settings.all_custom_urls()
        niji_boards: list = []; niji_seen: set = set()
        for cat in bbsmenu_cats:
            for board in cat.boards:
                if board.name == "二次元裏" and board.url not in niji_seen:
                    try:
                        sub   = urllib.parse.urlparse(board.url).hostname.split(".")[0]
                        short = sub if sub != "www" else board.name
                    except Exception:
                        short = board.name
                    niji_boards.append(BoardInfo(name=short, url=board.url))
                    niji_seen.add(board.url)
        cg = settings.get_custom_group("二次元裏")
        if cg:
            for b in cg.get("boards", []):
                url = b.get("url", "")
                if url and url not in niji_seen:
                    niji_boards.append(BoardInfo(name=b.get("name", ""), url=url))
                    niji_seen.add(url)
        filtered = [BoardCategory(name=cat.name,
                                  boards=[b for b in cat.boards if b.name != "二次元裏"])
                    for cat in bbsmenu_cats
                    if any(b.name != "二次元裏" for b in cat.boards)]
        result = []
        if niji_boards:
            result.append(BoardCategory(name="二次元裏", boards=niji_boards))
        result.extend(filtered)
        return result, custom_urls

    # ── ナビゲーション ────────────────────────────────────────────────────────

    def _on_board_selected(self, board: BoardInfo):
        self._current_board = board
        self._url_bar.setText(board.url)
        self._st_log.setText(f"板を開いています: {board.name}")
        self._show_board_view("catalog", board)

    def _show_board_view(self, view: str, board: BoardInfo | None = None):
        board = board or self._current_board
        if not board:
            return
        inner = self._get_or_create_board_tab(board)
        # 既存カタログタブを探して再ロード（catset POSTも実行）
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, CatalogView):
                inner.setCurrentIndex(i)
                self._st_log.setText(f"カタログ更新中: {board.name}")
                def _catset_existing(_b=board, _v=w):
                    _bs = get_board_settings(_b.base_url)
                    import urllib.parse as _up
                    _bd = _up.urlparse(_b.base_url).hostname or ""
                    ok = self._fetcher.post_catset(_b, _bs)
                    if not ok:
                        self._fetcher.set_cxyl_cookie(_bs.catalog_cxyl_str, board_domain=_bd)
                    else:
                        self._catset_reload_signal.emit(_v, _b)
                w._pending_catset = _catset_existing
                w.load(board)
                return
        # 新規作成
        cat_view = CatalogView(self._fetcher, self._settings, inner)
        cat_view.thread_open.connect(self._open_thread_url)
        cat_view.thread_open_bg.connect(self._open_thread_url_bg)
        cat_view.thread_open_mode.connect(self._open_thread_url_mode)
        cat_view.thread_open_bg_mode.connect(self._open_thread_url_bg_mode)
        cat_view.status_info.connect(self._on_thread_status)
        cat_view.error_band_changed.connect(
            lambda text, p=inner: self._broadcast_error_band(p, text))
        cat_view.catalog_new_arrivals.connect(
            lambda urls, _inner=inner: self._on_catalog_new_arrivals(_inner, urls))
        cat_view.quar_nos_changed.connect(
            lambda nos, _inner=inner, _cv=cat_view: self._recolor_quar_tabs(_inner, _cv))
        cat_view.auto_refresh_requested.connect(
            lambda v=cat_view: self._open_ar_dialog(v))
        inner.insertTab(0, cat_view, "カタログ"); inner.setCurrentIndex(0)
        _cat_ico = self._catalog_icon()
        if not _cat_ico.isNull():
            inner._wrap_bar.setTabIcon(0, _cat_ico)

        # カタログを開いたとき自動更新に自動追加（板設定から判断）
        _bs_cat = get_board_settings(board.base_url)
        if getattr(_bs_cat, 'auto_add_catalog_to_ar', False):
            self._auto_add_catalog_to_ar(cat_view, board)

        # まず即座にカタログ取得を開始（白画面を防ぐ）
        # catset POST は fetch 完了後に直列実行（同時HTTP接続によるウィンドウ消え防止）
        def _do_catset_then_reload(_b=board, _v=cat_view):
            _bs = get_board_settings(_b.base_url)
            import urllib.parse as _up
            _bd = _up.urlparse(_b.base_url).hostname or ""
            ok = self._fetcher.post_catset(_b, _bs)
            if not ok:
                self._fetcher.set_cxyl_cookie(_bs.catalog_cxyl_str, board_domain=_bd)
            else:
                self._catset_reload_signal.emit(_v, _b)
        cat_view._pending_catset = _do_catset_then_reload
        cat_view.load(board)
        self._st_log.setText(f"カタログ取得中: {board.name}")

    def _get_or_create_board_tab(self, board: BoardInfo, activate: bool = True) -> "BoardPane | None":
        for i in range(self._outer_tabs.count()):
            w = self._outer_tabs.widget(i)
            if isinstance(w, BoardPane) and w._board.url == board.url:
                if activate:
                    self._outer_tabs.setCurrentIndex(i)
                return w
        pane = BoardPane(board, self, self)
        # インナータブ切り替え時にステータスバー + タブペインを更新
        pane._tabs.currentChanged.connect(
            lambda _: self._refresh_status_for_active_tab())
        pane._tabs.currentChanged.connect(
            lambda _: self._refresh_tab_pane())
        pane._tabs.currentChanged.connect(
            lambda idx, _pane=pane: self._on_tab_clicked(_pane, idx))
        pane._tabs.currentChanged.connect(
            lambda idx, _pane=pane: self._on_inner_tab_changed(_pane, idx))
        pane._tabs.tabBar().tabBarClicked.connect(
            lambda idx, _pane=pane: self._on_tab_clicked(_pane, idx))
        # 閉じたタブをスタックに積む
        pane.tab_closing.connect(self._on_tab_closing)
        pane.tab_closing.connect(self._ar_mgr.remove_by_view)
        _sm = re.match(r'https?://([^.]+)\.', board.url or '')
        _sv = f"（{_sm.group(1)}）" if _sm else ''
        _tab_name = f"{board.name}{_sv}" if board.name else "板"
        # addTab の前にプレースホルダ・ウェルカムタブを削除する
        # （addTab後に removeTab すると一瞬タブが0枚になりウィンドウが消える）
        if self._ph_idx >= 0:
            self._outer_tabs.removeTab(self._ph_idx)
            self._ph_idx = -1
        if self._welcome_idx >= 0:
            for i in range(self._outer_tabs.count()):
                if self._outer_tabs.tabText(i).strip() == "2BP":
                    self._outer_tabs.removeTab(i)
                    self._welcome_idx = -1
                    break
        idx  = self._outer_tabs.addTab(pane, _tab_name)
        if activate:
            self._outer_tabs.setCurrentIndex(idx)
        self._update_placeholder_visibility()
        return pane

    def _open_thread_url(self, url: str):
        m = re.search(r"(https?://[^/]+/[^/]+/).*?res/(\d+)", url)
        if not m:
            return
        base = m.group(1); no = int(m.group(2))
        board = None
        for i in range(self._outer_tabs.count()):
            w = self._outer_tabs.widget(i)
            if isinstance(w, BoardPane) and url.startswith(w._board.base_url):
                board = w._board; break
        if not board:
            board = BoardInfo(name="", url=base + "futaba.htm")
        self._open_thread(board, no)

    def _open_thread(self, board: BoardInfo, no: int,
                     open_mode_override: str | None = None):
        """open_mode_override: None=設定に従う / ''=通常モード強制 / 'image' / 'quote'"""
        inner = self._get_or_create_board_tab(board)
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ThreadView) and w._thread_no == no:
                inner.setCurrentIndex(i); w.reload_thread(); return

        view = ThreadView(self._fetcher, self._settings, inner)
        view.open_reply_window.connect(
            lambda qno, qt, b=board, n=no: self._open_reply(b, n, qno, qt))
        view.open_image_tab.connect(self._open_image_tab)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        view.open_thread_url_requested.connect(self._open_thread_url)
        view.status_info.connect(self._on_thread_status)
        # 更新後のimg_listを同inner内の画像タブに反映
        def _on_img_list_updated(img_list, _inner=inner, _src_view=view):
            found = False
            for i in range(_inner.count()):
                w = _inner.widget(i)
                if isinstance(w, ImageTabView):
                    if w._src_thread_view is _src_view:
                        w.update_img_list(img_list)
                        found = True
            if not found:
                pass
            self._update_image_window_img_list(_src_view, img_list)

        view.img_list_updated.connect(_on_img_list_updated)
        idx = inner.addTab(view, f"No.{no}"); inner.setCurrentIndex(idx)  # ← タブに追加
        self._refresh_tab_pane()  # タブ追加を左ペインに即時反映
        view.thread_loaded.connect(
            lambda no2, cnt, _inner=inner, _view=view:
            self._update_thread_badge(_inner, _view, no2, cnt))
        # _refresh_tab_pane は _update() 内でタブテキスト設定後に呼ぶため、ここでは接続不要
        # エラー時にタブを赤文字にする（タブ名は変更しない）
        def _set_error_tab(msg, _inner=inner, _view=view):
            idx2 = _inner.indexOf(_view)
            if idx2 < 0:
                return
            # タブ名は変更せず色だけ赤にする（ERR表示しない）
            tb = _inner.tabBar()
            if hasattr(tb, "_tab_colors"):
                tb._tab_colors[idx2] = WrapTabBar.c_error()
                tb.update()
        view.thread_error.connect(_set_error_tab)
        # 復旧時にタブのエラー赤を解除する
        def _clear_error_tab(_inner=inner, _view=view):
            idx2 = _inner.indexOf(_view)
            if idx2 < 0:
                return
            tb = _inner.tabBar()
            if hasattr(tb, "_tab_colors"):
                cur = tb._tab_colors.get(idx2)
                if cur is not None and cur == WrapTabBar.c_error():
                    del tb._tab_colors[idx2]
                    tb._refresh_base_color(idx2)  # ID表示スレならピンクへ復帰
                    tb.update()
        view.thread_recovered.connect(_clear_error_tab)
        view.thread_dead.connect(
            lambda url, _v=view: self._on_thread_dead(url, _v))
        view.scroll_count_updated.connect(self._on_scroll_count_updated)
        view.auto_refresh_requested.connect(
            lambda v=view: self._open_ar_dialog(v))
        # NGスレッド即閉じ
        view.close_requested.connect(
            lambda _v=view, _inner=inner: self._close_ng_thread(_v, _inner))
        view.unread_state_changed.connect(
            lambda has, _inner=inner, _view=view: self._on_unread_state(_inner, _view, has))
        # ── スレを開いた時に自動的に自動更新に追加（板設定から判断）──────────
        _bs_thr1 = get_board_settings(board.base_url)
        if getattr(_bs_thr1, 'auto_add_to_ar', False):
            view.thread_loaded.connect(
                lambda _no, _cnt, _v=view, _b=board:
                self._auto_add_to_ar(_v, _b))
        if open_mode_override is not None:
            _open_mode = open_mode_override
        else:
            _mode_idx = getattr(self._settings, 'thread_open_mode', 0)
            _mode_map = {0: '', 1: 'image', 2: 'quote'}
            _open_mode = _mode_map.get(_mode_idx, '')
        view.load_thread(board, no, open_mode=_open_mode)
        self._settings.add_history(board.name, no, f"No.{no}", board.url)
        self._settings.save(); self._hist_pane.refresh()
        self._st_log.setText(f"スレッド読み込み中: No.{no}")

        # スレ読込完了 → 即タイトル更新 (thread_loaded シグナルで駆動)
        # _refresh_tab_pane は _update() 内でタブテキスト設定「後」に呼ぶ（順序が重要）
        def _update():
            # エラーから正常復旧した場合、タブのエラー赤色をクリアする
            # （_was_error は ThreadView 側で全体再描画時に現在のエラー状態へ更新される）
            if not getattr(view, "_was_error", False):
                idx_e = inner.indexOf(view)
                if idx_e >= 0:
                    tb_e = inner.tabBar()
                    if hasattr(tb_e, "_tab_colors"):
                        cur = tb_e._tab_colors.get(idx_e)
                        if cur is not None and cur == WrapTabBar.c_error():
                            del tb_e._tab_colors[idx_e]
                            tb_e._refresh_base_color(idx_e)  # ID表示スレならピンクへ復帰
                            tb_e.update()
            if view._thread and view._thread.title:
                t = view._thread.title.rsplit(" - ", 1)[0]
                new_title = t[:20] + ("…" if len(t) > 20 else "")
                _pin_safe_set(inner, view, new_title)
                self._settings.add_history(board.name, no, view._thread.title, board.url)
                self._settings.save(); self._hist_pane.refresh()
                self._st_log.setText(
                    f"スレッド読込完了: {len(view._thread.res_list)} レス  No.{no}")
                if inner.currentWidget() is view:
                    self._update_url_from_active()
            # OP サムネをタブアイコンに設定（titleチェック外・設定でONの場合のみ）
            if view._thread:  # タブアイコンは常に表示
                op_thumb = (view._thread.res_list[0].thumb_url
                            if view._thread.res_list else "")
                if op_thumb:
                    def _load_icon(url=op_thumb, _inner=inner, _view=view):
                        try:
                            data = self._fetcher.fetch_image_bytes(url)
                        except Exception as e:
                            data = None
                        if data:
                            # シグナル経由でメインスレッドに渡す（QTimer.singleShotより確実）
                            self._tab_icon_signal.emit(_inner, _view, data)
                    threading.Thread(target=_load_icon, daemon=True).start()
            # タイトル更新「後」に左ペインを更新（先に呼ぶとNo.数字のまま）
            self._refresh_tab_pane()
        view.thread_loaded.connect(lambda _n, _c: _update())

    def _update_tab_id_flag(self, tb, view, idx: int):
        """ID表示スレ(op-no-id)のピンク基底フラグを _tab_id_set に反映する。
        条件: IDが表示されている かつ OPメール欄が "id表示" 要求でない。
        ※IDの有無は OP(=res_list[0]) の id_str で判定する。
          （JSON差分API mode=json&res= の返信レスは非ID表示スレでも id を持つため、
            any(r.id_str) では誤検出する。OPは常にHTMLパース由来で信頼できる）"""
        if not hasattr(tb, "_tab_id_set") or not isinstance(view, ThreadView):
            return
        _rl = (view._thread.res_list if getattr(view, "_thread", None) else None) or []
        if not _rl:
            tb._tab_id_set.discard(idx)
            return
        _op = _rl[0]
        _op_email = (_op.email or "").strip()
        _enabled = getattr(self._settings, "tab_pink_op_no_id", False)
        _pink = _enabled and bool(_op.id_str) and _op_email.lower() != "id表示"
        if _pink:
            tb._tab_id_set.add(idx)
        else:
            tb._tab_id_set.discard(idx)

    def _update_tab_quar_flag(self, tb, cat_view, view, idx: int):
        """隔離スレ(json∖cat)のオレンジ基底フラグを _tab_quar_set に反映する。
        判定: 同ペイン CatalogView の隔離No集合(_quar_nos)に view のスレNoが含まれるか。"""
        if not hasattr(tb, "_tab_quar_set") or not isinstance(view, ThreadView):
            return
        _quar = False
        if getattr(self._settings, "tab_orange_quarantine", True) and cat_view is not None:
            _th = getattr(view, "_thread", None)
            _no = getattr(_th, "no", None) if _th else None
            # スレ落ち確定(_is_dead)のタブはオレンジにしない。
            # json∖cat には「隔離(生存・カタログ非表示)」だけでなく
            # 「落ちた(消滅)」スレも一時的に混入するため、生死を知る
            # ThreadView 側で除外する（隔離スレは生存中なので _is_dead=False）。
            if (_no is not None
                    and not getattr(view, "_is_dead", False)
                    and _no in getattr(cat_view, "_quar_nos", set())):
                _quar = True
        if _quar:
            tb._tab_quar_set.add(idx)
        else:
            tb._tab_quar_set.discard(idx)

    def _pane_catalog_view(self, inner):
        """インナータブ(QTabWidget)内の CatalogView を返す（無ければ None）。"""
        try:
            for i in range(inner.count()):
                w = inner.widget(i)
                if isinstance(w, CatalogView):
                    return w
        except Exception:
            pass
        return None

    def _clear_dead_tab_quar(self, view):
        """スレ落ち確定 view のタブについて、隔離(オレンジ)基底色を再評価して解除する。
        _update_tab_quar_flag が _is_dead を見て False を返すため、ここで再評価＋再描画する。"""
        try:
            for i in range(self._outer_tabs.count()):
                pane = self._outer_tabs.widget(i)
                if not isinstance(pane, BoardPane):
                    continue
                inner = pane._tabs
                idx = inner.indexOf(view)
                if idx < 0:
                    continue
                tb = inner.tabBar()
                if not hasattr(tb, "_tab_quar_set"):
                    return
                self._update_tab_quar_flag(tb, self._pane_catalog_view(inner), view, idx)
                tb._refresh_base_color(idx)
                tb.update()
                return
        except Exception:
            pass

    def _recolor_quar_tabs(self, inner, cat_view):
        """隔離No集合の変化時、開いている全スレタブのオレンジ色を再評価する。"""
        tb = inner.tabBar()
        if not hasattr(tb, "_tab_quar_set"):
            return
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ThreadView):
                self._update_tab_quar_flag(tb, cat_view, w, i)
                tb._refresh_base_color(i)
        tb.update()

    def _update_thread_badge(self, inner: "BoardPane", view, no: int, new_count: int):
        """タブに未読レス数バッジを表示"""
        idx = inner.indexOf(view)
        if idx < 0: return
        tb = inner.tabBar()
        if hasattr(tb, "_tab_colors"):
            # ID表示スレのピンク基底色フラグを更新（全表示経路共通・赤/青より下位）
            self._update_tab_id_flag(tb, view, idx)
            # 隔離スレのオレンジ基底色フラグを更新（同上）
            self._update_tab_quar_flag(tb, self._pane_catalog_view(inner), view, idx)
            if new_count > 0:
                # 新着あり → 色を決定（エラー赤は最優先で維持）
                cur = tb._tab_colors.get(idx)
                if cur != WrapTabBar.c_error():
                    if idx in tb._tab_id_set or idx in tb._tab_quar_set:
                        # ID/隔離/両方 は青より優先（#ff80c0 / #ff8800 / #FF0099）
                        if cur and cur == WrapTabBar.c_new():
                            del tb._tab_colors[idx]
                        tb._refresh_base_color(idx)
                    elif view.isVisible():
                        # 表示中（自分が見ている）タブ → 青にせず基底色（=既読扱い）
                        if cur and cur == WrapTabBar.c_new():
                            del tb._tab_colors[idx]
                        tb._refresh_base_color(idx)
                    else:
                        tb._tab_colors[idx] = WrapTabBar.c_new()   # 背景タブの新着 → 青
            else:
                # 新着なし → 青を解除し基底色（ピンク or デフォルト）を反映（エラー赤は維持）
                cur = tb._tab_colors.get(idx)
                if cur and cur == WrapTabBar.c_new():
                    del tb._tab_colors[idx]
                tb._refresh_base_color(idx)
            tb.update()
        base = re.sub(r' \(\+\d+\)$', '', inner.tabText(idx))
        new_text = f"{base} (+{new_count})" if new_count > 0 else base
        if inner.tabText(idx) != new_text:
            inner.setTabText(idx, new_text)
        if new_count > 0:
            # 新着あり → 即座に水色背景をセット（バックグラウンドタブでもDOMに依存しない）
            self._on_unread_state(inner, view, True)
            # 新着到着 → JS側の「末尾を見た（既読）」フラグを解除（全モード共通）
            try:
                view._view.page().runJavaScript("window._unreadSeen=false;")
            except Exception:
                pass
        else:
            # 新着なし → JSに問い合わせて赤帯の有無で判定（100ms後にDOM確定）
            QTimer.singleShot(100, lambda: self._check_unread_bg(inner, view))


    def _on_tab_clicked(self, pane, idx: int):
        """タブクリック時：青文字（新着）だった場合のみ文字色をリセット"""
        tb = pane._tabs.tabBar()
        if not hasattr(tb, "_tab_colors"): return
        cur = tb._tab_colors.get(idx)
        if cur and cur == WrapTabBar.c_new():
            del tb._tab_colors[idx]
            tb._refresh_base_color(idx)  # ID表示スレならピンクへ復帰
            tb.update()

    def _on_inner_tab_changed(self, pane, idx: int):
        """インナータブ切替時：非アクティブになったImageTabViewのメディアを一時停止"""
        for i in range(pane._tabs.count()):
            if i == idx:
                continue
            w = pane._tabs.widget(i)
            if isinstance(w, ImageTabView) and hasattr(w, 'pause_media'):
                w.pause_media()
        self._sync_post_dialog_roll()
        QTimer.singleShot(0, self._refresh_title_bar)
        # アクティブになったビューのステータスバー（nレス数等）を最新化する
        cur = pane._tabs.widget(idx)
        if isinstance(cur, ThreadView):
            cur.refresh_status_info()
        elif isinstance(cur, CatalogView) and hasattr(cur, "_emit_catalog_status"):
            try:
                cur._emit_catalog_status()
            except Exception:
                pass

    def _activate_thread_tab(self, thread_no: int):
        """PostDialogのタイトルバークリック時：対応するThreadViewタブをアクティブにする"""
        for i in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(i)
            if not isinstance(pane, BoardPane):
                continue
            for j in range(pane._tabs.count()):
                w = pane._tabs.widget(j)
                if isinstance(w, ThreadView) and w._thread_no == thread_no:
                    self._outer_tabs.setCurrentIndex(i)
                    pane._tabs.setCurrentIndex(j)
                    self.raise_(); self.activateWindow()
                    return

    def _sync_post_dialog_roll(self):
        """アクティブなThreadViewに対応するPostDialogを復元、他は縮小する"""
        pdlgs = getattr(self, "_post_dialogs", {})
        if not pdlgs:
            return
        # アクティブなThreadViewのthread_noを取得
        active_no = 0
        active_title = ""
        inner = self._active_inner()
        if inner:
            w = inner.currentWidget()
            if isinstance(w, ThreadView):
                active_no = w._thread_no or 0
                if active_no and w._thread:
                    active_title = (w._thread.title or f"No.{active_no}")
        for resto, dlg in list(pdlgs.items()):
            try:
                if not dlg.isVisible():
                    continue
                if active_no and resto == active_no:
                    if hasattr(dlg, "roll_restore"):
                        dlg.roll_restore()
                else:
                    if hasattr(dlg, "roll_up"):
                        dlg.roll_up()
            except Exception:
                pass

    def _on_catalog_new_arrivals(self, inner, urls):
        """カタログ更新で +1以上の新着があったスレの、同一板内で開いているタブを
        青文字（#4488ff）＋青背景（水色）にする。"""
        if not urls:
            return
        try:
            tb = inner.tabBar()
        except Exception:
            return
        if not hasattr(tb, "_tab_colors"):
            return
        for ii in range(inner.count()):
            w = inner.widget(ii)
            if isinstance(w, ThreadView) and w._thread and (w._thread.url in urls):
                # op-no-idフラグを更新してから文字色を決定（赤は維持）
                self._update_tab_id_flag(tb, w, ii)
                cur = tb._tab_colors.get(ii)
                if cur != WrapTabBar.c_error():
                    if ii in tb._tab_id_set:
                        tb._tab_colors[ii] = WrapTabBar.c_id()    # op-no-id は常にピンク
                    elif w.isVisible():
                        # 表示中タブ → 青にせず基底色（既読扱い）
                        if cur and cur == WrapTabBar.c_new():
                            del tb._tab_colors[ii]
                        tb._refresh_base_color(ii)
                    else:
                        tb._tab_colors[ii] = WrapTabBar.c_new()    # 背景タブ → 青
                # 青背景（水色）
                self._on_unread_state(inner, w, True)
        tb.update()

    def _on_unread_state(self, inner, view, has_unread: bool):
        """未読（赤帯）有無に応じてタブ背景色を水色/デフォルトに切り替える"""
        idx = inner.indexOf(view)
        if idx < 0: return
        tb = inner.tabBar()
        if not hasattr(tb, "_tab_bg_colors"): return
        if has_unread:
            tb._tab_bg_colors[idx] = WrapTabBar.c_unread_bg()  # 水色・半透明
        else:
            tb._tab_bg_colors.pop(idx, None)
            # 末尾表示＝既読 → 青文字を解除し基底色（ピンク or デフォルト）を反映
            # （バッジ処理の前後どちらで来ても正しくなるよう、ここでもフラグを算出）
            if hasattr(tb, "_tab_colors"):
                self._update_tab_id_flag(tb, view, idx)
                cur = tb._tab_colors.get(idx)
                if cur and cur == WrapTabBar.c_new():
                    del tb._tab_colors[idx]
                if hasattr(tb, "_refresh_base_color"):
                    tb._refresh_base_color(idx)
        tb.update()

    def _check_unread_bg(self, inner, view):
        """JSに問い合わせてnew-resの有無でタブ背景色を更新する"""
        try:
            page = view._view.page()
        except Exception:
            return
        def _cb(count, _inner=inner, _view=view):
            self._on_unread_state(_inner, _view, bool(count))
        page.runJavaScript(
            "(!window._unreadSeen && "
            "document.querySelectorAll('.res.new-res').length>0)?1:0;", _cb)

    def _open_ar_dialog(self, view=None):
        """自動更新ダイアログを開く（スレッド・カタログ両対応）"""
        init_entry = None

        # ── スレッドビューの場合 ──────────────────────────────────────────
        if view and hasattr(view, '_thread') and view._thread:
            th = view._thread
            init_entry = AutoRefreshEntry(
                no=th.no,
                url=th.url or "",
                title=th.title or f"No.{th.no}",
                board_name=(th.board.name if th.board else ""),
                max_saved=(th.board.max_saved if th.board else 0),
            )
        # ── カタログビューの場合 ──────────────────────────────────────────
        elif view and hasattr(view, '_board') and view._board and not hasattr(view, '_thread'):
            board = view._board
            cat_url = board.base_url + "futaba.php?mode=cat"
            init_entry = AutoRefreshEntry(
                no=0,
                url=cat_url,
                title=f"カタログ - {board.name}",
                board_name=board.name,
                is_catalog=True,
                board_url=board.base_url,
            )

        # ── ダイアログの有効性チェック ──────────────────────────────────
        dlg_alive = False
        if self._ar_dlg is not None:
            try:
                dlg_alive = self._ar_dlg.isVisible()
            except RuntimeError:
                self._ar_dlg = None

        if not dlg_alive:
            self._ar_dlg = AutoRefreshDialog(
                self._ar_mgr, self, init_entry=init_entry, init_view=view,
                settings=self._settings)
            self._ar_dlg.finished.connect(
                lambda _=None: setattr(self, '_ar_dlg', None))
            self._ar_dlg.show()
            self._ar_dlg.raise_()
            self._ar_dlg.activateWindow()
        else:
            if init_entry:
                self._ar_dlg.set_entry(init_entry, view)
            # 最小化されていれば元に戻す
            from PySide6.QtCore import Qt as _Qt
            ws = self._ar_dlg.windowState()
            if ws & _Qt.WindowState.WindowMinimized:
                self._ar_dlg.setWindowState(ws & ~_Qt.WindowState.WindowMinimized)
            self._ar_dlg.show()
            self._ar_dlg.raise_()
            self._ar_dlg.activateWindow()
            if init_entry:
                self._ar_dlg._tabs.setCurrentIndex(1)

    def _auto_add_to_ar(self, view, board):
        """スレを開いた時に自動的に自動更新に追加する"""
        if not view._thread:
            return
        th  = view._thread
        url = th.url or ""
        has = self._ar_mgr.has_url(url)
        if not url or has:
            return   # URL 未確定 or 既に登録済み

        # スレが落ちている・1000レス到達の場合は追加しない（サイレント）
        if th.error or getattr(th, 'is_full', False):
            return

        # デフォルト設定 or 最後に使った設定から adaptive_intervals を構築
        from futaba2b_models import AR_ADAPTIVE_DEFAULTS
        adaptive  = [dict(r) for r in AR_ADAPTIVE_DEFAULTS]
        _bs = get_board_settings(board.base_url) if board else None
        if _bs and _bs.ar_use_default_thread:
            vals = list(_bs.ar_default_thread_intervals or [3600, 1800, 600, 120, 60, 30])
            chks = list(_bs.ar_default_thread_checks    or [False]*5)
        else:
            vals = list(getattr(self._settings, "ar_last_intervals", [3600, 1800, 600, 120, 60, 30]))
            chks = list(getattr(self._settings, "ar_last_checks",    [False]*5))
        for i, rule in enumerate(adaptive):
            if i < len(vals):
                # v0.8.078以降は秒単位で統一。interval_min が残っていると
                # _compute_interval_sec がデフォルトの interval_sec を優先して
                # ユーザー設定値が無視されるため、必ず interval_sec に書き込む
                rule["interval_sec"] = max(1, int(vals[i]))
                rule.pop("interval_min", None)
            if i == 0:
                rule["enabled"] = True
            elif (i - 1) < len(chks):
                rule["enabled"] = chks[i - 1]

        # 実際の残り件数%でカウントダウン初期値を決定
        # max_saved はスレHTMLの「保存数はN件」由来だが、稀に拾えず0になる。
        # その場合は板別キャッシュ(カタログ取得で学習)→板設定の順でフォールバック。
        board_url = url.rsplit("/res/", 1)[0] + "/" if "/res/" in url else ""
        max_saved = th.board.max_saved if (th.board and th.board.max_saved) else 0
        if max_saved <= 0 and board_url:
            max_saved = self._settings.max_saved_by_board.get(board_url, 0)
        if max_saved <= 0 and board:
            max_saved = getattr(board, 'max_saved', 0) or 0
        pct       = 100.0
        if max_saved > 0 and board_url:
            o = self._settings.global_max_no_by_board.get(board_url, 0)
            if o > 0:
                remaining = th.no + max_saved - o
                pct       = max(0.0, remaining / max_saved * 100)

        interval_sec = _compute_interval_sec(adaptive, pct)

        entry = AutoRefreshEntry(
            no           = th.no,
            url          = url,
            title        = th.title or f"No.{th.no}",
            board_name   = (th.board.name if th.board else board.name),
            interval_sec = interval_sec,
            max_saved    = max_saved,
            adaptive_intervals = adaptive,
        )
        self._ar_mgr.add(entry, view)
        _disp = (f"{interval_sec}秒" if interval_sec < 60
                 else f"{interval_sec // 60}分")
        self._st_log.setText(
            f"自動更新に追加: No.{th.no}  間隔 {_disp}")

    def _auto_add_catalog_to_ar(self, cat_view, board: "BoardInfo"):
        """カタログを開いたとき自動的に自動更新に追加する"""
        if not board:
            return
        cat_url = board.base_url + "futaba.php?mode=cat"
        if self._ar_mgr.has_url(cat_url):
            return  # 既に登録済み

        from futaba2b_models import AR_ADAPTIVE_DEFAULTS
        # カタログ用デフォルト間隔を使う（板ごとの設定から取得）
        # v0.8.078以降、設定値は秒単位（×60は不要）
        _bs = get_board_settings(board.base_url)
        if _bs.ar_use_default_catalog:
            ivals = list(_bs.ar_default_catalog_intervals or [600])
            interval_sec = max(1, int(ivals[0] if ivals else 600))
        else:
            last_vals = list(getattr(self._settings, 'ar_last_intervals', [3600]))
            interval_sec = max(1, int(last_vals[0] if last_vals else 3600))

        adaptive = [dict(r) for r in AR_ADAPTIVE_DEFAULTS]
        # カタログはpct=100の1行のみ使用。interval_secを実際の間隔に合わせる
        for r in adaptive:
            if r.get("pct") == 100:
                r["interval_sec"] = interval_sec
                r.pop("interval_min", None)
                break

        entry = AutoRefreshEntry(
            no=0,
            url=cat_url,
            title=f"カタログ - {board.name}",
            board_name=board.name,
            interval_sec=interval_sec,
            is_catalog=True,
            board_url=board.base_url,
            adaptive_intervals=adaptive,
        )
        self._ar_mgr.add(entry, cat_view)
        _disp = (f"{interval_sec}秒" if interval_sec < 60
                 else f"{interval_sec // 60}分")
        self._st_log.setText(f"カタログを自動更新に追加: {board.name}  間隔 {_disp}")

    def _open_thread_url_mode(self, url: str, mode: int):
        """スレをアクティブで開き、読込後に表示モードを切り替える"""
        _mode_map = {0: '', 1: 'image', 2: 'quote'}
        _open_mode = _mode_map.get(mode, '')
        m = re.search(r"(https?://[^/]+/[^/]+/).*?res/(\d+)", url)
        if not m: return
        base = m.group(1); no = int(m.group(2))
        board = None
        for i in range(self._outer_tabs.count()):
            w = self._outer_tabs.widget(i)
            if isinstance(w, BoardPane) and url.startswith(w._board.base_url):
                board = w._board; break
        if not board:
            board = BoardInfo(name="", url=base + "futaba.htm")
        inner = self._get_or_create_board_tab(board)
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ThreadView) and w._thread_no == no:
                inner.setCurrentIndex(i); w.reload_thread(); return
        from futaba2b_app_qt import ThreadView as _TV
        view = _TV(self._fetcher, self._settings, inner)
        view.open_reply_window.connect(
            lambda qno, qt, b=board, n=no: self._open_reply(b, n, qno, qt))
        view.open_image_tab.connect(self._open_image_tab)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        view.open_thread_url_requested.connect(self._open_thread_url)
        view.status_info.connect(self._on_thread_status)
        def _on_img_list_updated_m(img_list, _inner=inner, _sv=view):
            for i in range(_inner.count()):
                w = _inner.widget(i)
                if isinstance(w, ImageTabView) and w._src_thread_view is _sv:
                    w.update_img_list(img_list)
            self._update_image_window_img_list(_sv, img_list)
        view.img_list_updated.connect(_on_img_list_updated_m)
        idx = inner.addTab(view, f"No.{no}"); inner.setCurrentIndex(idx)
        self._refresh_tab_pane()
        view.thread_loaded.connect(
            lambda no2, cnt, _inner=inner, _view=view:
            self._update_thread_badge(_inner, _view, no2, cnt))
        def _set_err_m(msg, _inner=inner, _view=view):
            idx2 = _inner.indexOf(_view)
            if idx2 < 0: return
            tb = _inner.tabBar()
            if hasattr(tb, "_tab_colors"):
                tb._tab_colors[idx2] = WrapTabBar.c_error()
                tb.update()
        view.thread_error.connect(_set_err_m)
        def _clear_err_m(_inner=inner, _view=view):
            idx2 = _inner.indexOf(_view)
            if idx2 < 0: return
            tb = _inner.tabBar()
            if hasattr(tb, "_tab_colors"):
                cur = tb._tab_colors.get(idx2)
                if cur is not None and cur == WrapTabBar.c_error():
                    del tb._tab_colors[idx2]
                    tb._refresh_base_color(idx2)
                    tb.update()
        view.thread_recovered.connect(_clear_err_m)
        view.thread_dead.connect(
            lambda url, _v=view: self._on_thread_dead(url, _v))
        view.scroll_count_updated.connect(self._on_scroll_count_updated)
        view.auto_refresh_requested.connect(
            lambda v=view: self._open_ar_dialog(v))
        view.close_requested.connect(
            lambda _v=view, _inner=inner: self._close_ng_thread(_v, _inner))
        view.unread_state_changed.connect(
            lambda has, _inner=inner, _view=view: self._on_unread_state(_inner, _view, has))
        # ── 板設定の自動更新自動登録チェック ─────────────────────────────
        _bs_m = get_board_settings(board.base_url)
        if getattr(_bs_m, 'auto_add_to_ar', False):
            view.thread_loaded.connect(
                lambda _no, _cnt, _v=view, _b=board:
                self._auto_add_to_ar(_v, _b))
        def _update_mode():
            if view._thread and view._thread.title:
                t = view._thread.title.rsplit(" - ", 1)[0]
                new_title = t[:20] + ("…" if len(t) > 20 else "")
                _pin_safe_set(inner, view, new_title)
                self._settings.add_history(board.name, no, view._thread.title, board.url)
                self._settings.save(); self._hist_pane.refresh()
            if view._thread:
                op_thumb = (view._thread.res_list[0].thumb_url
                            if view._thread.res_list else "")
                if op_thumb:
                    def _load_icon_m(url=op_thumb, _inner=inner, _view=view):
                        try:
                            data = self._fetcher.fetch_image_bytes(url)
                        except Exception:
                            data = None
                        if data:
                            self._tab_icon_signal.emit(_inner, _view, data)
                    threading.Thread(target=_load_icon_m, daemon=True).start()
            self._refresh_tab_pane()
        view.thread_loaded.connect(lambda _n, _c: _update_mode())
        view.load_thread(board, no, open_mode=_open_mode)

    def _open_thread_url_bg(self, url: str):
        """スレをバックグラウンドタブで開く (現在タブを切り替えない)"""
        board = self._find_board_by_url(url)
        if not board:
            m = re.match(r"(https?://[^/]+/[^/]+/)", url)
            if m: board = BoardInfo(name="", url=m.group(1))
            else: return
        # 板タブが現在 _outer_tabs に存在しない場合は開かない
        _board_open = any(
            isinstance(self._outer_tabs.widget(i), BoardPane)
            and self._outer_tabs.widget(i)._board.url == board.url
            for i in range(self._outer_tabs.count())
        )
        if not _board_open: return
        m = re.search(r"/res/(\d+)", url)
        if not m: return
        no = int(m.group(1))
        _bg_mode_idx = getattr(self._settings, 'thread_open_bg_mode', 0)
        self._open_thread_url_bg_mode(url, _bg_mode_idx)

    def _open_thread_url_bg_mode(self, url: str, mode: int):
        """スレをBGで開き、読込後に表示モードを切り替える"""
        _mode_map = {0: '', 1: 'image', 2: 'quote'}
        _open_mode = _mode_map.get(mode, '')
        board = self._find_board_by_url(url)
        if not board:
            m = re.match(r"(https?://[^/]+/[^/]+/)", url)
            if m: board = BoardInfo(name="", url=m.group(1))
            else: return
        _board_open = any(
            isinstance(self._outer_tabs.widget(i), BoardPane)
            and self._outer_tabs.widget(i)._board.url == board.url
            for i in range(self._outer_tabs.count())
        )
        if not _board_open: return
        m = re.search(r"/res/(\d+)", url)
        if not m: return
        no = int(m.group(1))
        pane = self._get_or_create_board_tab(board, activate=False)
        for i in range(pane.count()):
            w = pane.widget(i)
            if isinstance(w, ThreadView) and getattr(w, '_thread_no', None) == no:
                return
        view = ThreadView(self._fetcher, self._settings, pane)
        view.open_reply_window.connect(
            lambda qno, qt, b=board, n=no: self._open_reply(b, n, qno, qt))
        view.open_image_tab.connect(self._open_image_tab)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        view.open_thread_url_requested.connect(self._open_thread_url)
        view.status_info.connect(self._on_thread_status)
        def _on_img_list_updated_bg(img_list, _p=pane, _sv=view):
            for i in range(_p.count()):
                w = _p.widget(i)
                if isinstance(w, ImageTabView) and w._src_thread_view is _sv:
                    w.update_img_list(img_list)
            self._update_image_window_img_list(_sv, img_list)
        view.img_list_updated.connect(_on_img_list_updated_bg)
        view.thread_loaded.connect(
            lambda n2, cnt, _p=pane, _v=view:
            self._update_thread_badge(_p, _v, n2, cnt))
        def _set_err_bg(msg, _p=pane, _v=view):
            idx2 = _p.indexOf(_v)
            if idx2 < 0: return
            tb = _p.tabBar()
            if hasattr(tb, "_tab_colors"):
                tb._tab_colors[idx2] = WrapTabBar.c_error()
                tb.update()
        view.thread_error.connect(_set_err_bg)
        def _clear_err_bg(_p=pane, _v=view):
            idx2 = _p.indexOf(_v)
            if idx2 < 0: return
            tb = _p.tabBar()
            if hasattr(tb, "_tab_colors"):
                cur = tb._tab_colors.get(idx2)
                if cur is not None and cur == WrapTabBar.c_error():
                    del tb._tab_colors[idx2]
                    tb._refresh_base_color(idx2)
                    tb.update()
        view.thread_recovered.connect(_clear_err_bg)
        view.thread_dead.connect(
            lambda url, _v=view: self._on_thread_dead(url, _v))
        view.scroll_count_updated.connect(self._on_scroll_count_updated)
        view.auto_refresh_requested.connect(
            lambda v=view: self._open_ar_dialog(v))
        view.close_requested.connect(
            lambda _v=view, _p=pane: self._close_ng_thread(_v, _p))
        view.unread_state_changed.connect(
            lambda has, _p=pane, _view=view: self._on_unread_state(_p, _view, has))
        # ── 板設定の自動更新自動登録チェック ─────────────────────────────
        _bs_bg = get_board_settings(board.base_url)
        if getattr(_bs_bg, 'auto_add_to_ar', False):
            view.thread_loaded.connect(
                lambda _no, _cnt, _v=view, _b=board:
                self._auto_add_to_ar(_v, _b))
        _cur = pane.currentIndex()
        pane.addTab(view, f"No.{no}")
        pane.setCurrentIndex(_cur)
        def _bg_update():
            if view._thread and view._thread.title:
                t = view._thread.title.rsplit(" - ", 1)[0]
                new_title = t[:20] + ("…" if len(t) > 20 else "")
                _pin_safe_set(pane, view, new_title)
                self._settings.add_history(board.name, no, view._thread.title, board.url)
                self._settings.save(); self._hist_pane.refresh()
            if view._thread:
                op_thumb = (view._thread.res_list[0].thumb_url
                            if view._thread.res_list else "")
                if op_thumb:
                    def _load_icon_bg(url=op_thumb, _pane=pane, _view=view):
                        try:
                            data = self._fetcher.fetch_image_bytes(url)
                        except Exception:
                            data = None
                        if data:
                            self._tab_icon_signal.emit(_pane, _view, data)
                    threading.Thread(target=_load_icon_bg, daemon=True).start()
            self._refresh_tab_pane()
        view.thread_loaded.connect(lambda _n, _c: _bg_update())
        view.load_thread(board, no, open_mode=_open_mode)

    def _open_from_history(self, h: dict):
        no        = h.get("no", 0)
        board_url = h.get("url", "")
        board_nm  = h.get("board", "")
        if no and board_url:
            board = BoardInfo(name=board_nm, url=board_url)
            self._ensure_catalog_exists(board)  # 板タブ+カタログを確保
            self._open_thread(board, no)
        elif no:
            inner = self._active_inner()
            board = inner._board if inner else self._current_board
            if board:
                self._open_thread(board, no)
            else:
                self._st_log.setText("履歴に板情報がありません (再ログインして再試行)")

    def _broadcast_error_band(self, pane, text: str):
        """カタログの通信エラー赤帯を、同じ板ペインのスレタブ（返信/画像/引用モード）へ伝播する。"""
        try:
            for i in range(pane.count()):
                w = pane.widget(i)
                if isinstance(w, ThreadView):
                    if text:
                        w._inject_error_band(text)
                    else:
                        w._clear_error_band()
        except Exception:
            pass

    def _ensure_catalog_exists(self, board: BoardInfo):
        """板タブを作成し、カタログタブがなければ追加する。"""
        pane = self._get_or_create_board_tab(board)
        for i in range(pane.count()):
            if isinstance(pane.widget(i), CatalogView): return
        cat = CatalogView(self._fetcher, self._settings, pane)
        cat.thread_open.connect(self._open_thread_url)
        cat.thread_open_bg.connect(self._open_thread_url_bg)
        cat.thread_open_mode.connect(self._open_thread_url_mode)
        cat.thread_open_bg_mode.connect(self._open_thread_url_bg_mode)
        cat.status_info.connect(self._on_thread_status)
        cat.error_band_changed.connect(
            lambda text, p=pane: self._broadcast_error_band(p, text))
        pane.insertTab(0, cat, "カタログ")
        _cat_ico = self._catalog_icon()
        if not _cat_ico.isNull():
            pane._wrap_bar.setTabIcon(0, _cat_ico)
        if True:  # 板を開いたとき常に自動取得
            cat.load(board)

    # ── タブ操作 ─────────────────────────────────────────────────────────────

    def _update_placeholder_visibility(self):
        """板タブが1枚もなければ 2BP プレースホルダを表示"""  
        has_board = any(isinstance(self._outer_tabs.widget(i), BoardPane)
                        for i in range(self._outer_tabs.count()))
        if not has_board and self._ph_idx < 0:
            self._ph_idx = self._outer_tabs.addTab(self._ph_widget, "2BP")
            self._outer_tabs.tabBar().setTabButton(
                self._ph_idx, self._outer_tabs.tabBar().ButtonPosition.RightSide, None)
            self._outer_tabs.setCurrentIndex(self._ph_idx)
        elif has_board and self._ph_idx >= 0:
            self._outer_tabs.removeTab(self._ph_idx)
            self._ph_idx = -1
        # 板タブが存在したらウェルカムタブも削除
        if has_board and self._welcome_idx >= 0:
            # removeTab すると後ろのインデックスがずれるので widget で検索
            for i in range(self._outer_tabs.count()):
                if self._outer_tabs.tabText(i).strip() == "2BP":
                    self._outer_tabs.removeTab(i)
                    self._welcome_idx = -1
                    break

    def _find_board_by_url(self, url: str) -> "BoardInfo | None":
        """URL から BoardInfo を探す (開いている板タブ → 設定保存板の順)"""
        for i in range(self._outer_tabs.count()):
            w = self._outer_tabs.widget(i)
            if isinstance(w, BoardPane) and url.startswith(w._board.base_url):
                return w._board
        for b in getattr(self._settings, "boards", []):
            try:
                base = b.url.rsplit("/futaba.htm", 1)[0].rstrip("/") + "/"
                if url.startswith(base):
                    return b
            except Exception:
                pass
        return None

    def _active_inner(self) -> "BoardPane | None":
        w = self._outer_tabs.currentWidget()
        return w if isinstance(w, BoardPane) else None

    def _close_outer_tab(self, idx: int):
        w = self._outer_tabs.widget(idx)
        # プレースホルダタブを閉じたら _ph_idx をリセット
        if w is self._ph_widget:
            self._outer_tabs.removeTab(idx)
            self._ph_idx = -1
            return
        if not isinstance(w, BoardPane):
            return

        # 履歴から戻り先を決定
        target = -1
        while self._outer_tab_history:
            h = self._outer_tab_history.pop()
            if h == idx:
                continue
            target = h if h < idx else h - 1
            break

        self._outer_tabs.removeTab(idx)
        self._dispose_pane(w)

        # 履歴内の残りインデックスを補正
        self._outer_tab_history = [
            (h if h < idx else h - 1)
            for h in self._outer_tab_history if h != idx
        ]

        if target >= 0 and target < self._outer_tabs.count():
            self._outer_tabs.setCurrentIndex(target)
        self._update_placeholder_visibility()

    def _dispose_pane(self, pane):
        """板ペイン（BoardPane）をUIスレッドで確実に破棄する。
        内側の各ビューの cleanup()（profile遅延削除等）を先に実行し、
        ペイン自体は deleteLater で子ごとメインスレッド破棄に委ねる。
        参照切れペインを循環GCがBGスレッドで回収すると
        QtWebEngineが非GUIスレッドで破壊されクラッシュするため。"""
        if pane is None or not isinstance(pane, BoardPane):
            return
        try:
            tabs = pane._tabs
            for i in range(tabs.count()):
                w = tabs.widget(i)
                try:
                    if hasattr(w, 'cleanup'):
                        w.cleanup()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pane.deleteLater()
        except Exception:
            pass
        _schedule_gc()   # 板タブ破棄後に遅延GCで回収

    def _on_scroll_count_updated(self, remaining: int):
        """末尾スクロール残回数をステータスバーに表示"""
        if remaining > 0:
            self._st_scroll.setText(f"↓あと{remaining}回で更新")
        else:
            self._st_scroll.setText("")

    # ── スレ落ち監視 ─────────────────────────────────────────────────────────

    def _on_thread_dead(self, url: str, view):
        """スレ落ち・1000レス検出: 自動更新から削除 + ステータスに表示 + 自動保存 + 自動クローズ"""
        # スレ落ち確定 → このタブの隔離(オレンジ)基底色を即解除する。
        # （json∖cat に落ちたスレが残っている間、次のカタログ更新を待たずに反映）
        self._clear_dead_tab_quar(view)
        thread = (getattr(view, "_last_valid_thread", None)
                  or getattr(view, "_thread", None))
        is_full = bool(thread and getattr(thread, "is_full", False))
        reason = "1000レス到達" if is_full else "スレ落ち"
        # AR削除は url の有無にかかわらず view 基準で必ず実行する。
        # 404キャッシュフォールバック等で thread.url が空文字になる場合があり、
        # `if url:` の旧ガードだと remove_by_url が呼ばれず AR エントリが
        # 残り続け、毎ティック thread_dead が再発火→下の自動クローズが
        # 何度もスケジュールされてしまう（→後述のRuntimeError連発の原因）。
        self._ar_mgr.remove_by_view(view)
        if url:
            self._ar_mgr.remove_by_url(url)
            self._st_log.setText(f"{reason} → 自動更新から削除: {url}")

        # ── 自動保存 ──────────────────────────────────────────────────────
        s = self._settings
        # 1000レス到達時は log_auto_save_full がONのときのみ保存
        _should_save = (getattr(s, "log_auto_save", False)
                        and (not is_full or getattr(s, "log_auto_save_full", False)))
        if _should_save:
            # 二重保存防止（同一URLが既に保存済みならスキップ）
            if url and url in self._auto_save_done:
                return  # 既に保存済み
            else:
                if url:
                    self._auto_save_done.add(url)
                # is_full時は view._thread が有効なスレッドを持っている
                if not thread or not thread.res_list:
                    self._st_log.setText(f"{reason}自動保存: 保存データなし（未読み込み）")
                else:
                    fmts = []
                    if getattr(s, "log_auto_save_html", True): fmts.append("html")
                    if getattr(s, "log_auto_save_mht",  True): fmts.append("mht")
                    if getattr(s, "log_auto_save_zip",  True): fmts.append("zip")
                    if fmts:
                        save_html = self._build_log_html(view, thread)
                        save_dir  = self._log_save_dir()

                        def _do_save(_reason=reason, _thread=thread, _fmts=fmts,
                                     _html=save_html, _dir=save_dir):
                            try:
                                saved = []; skipped = []
                                for fmt in _fmts:
                                    rel   = self._log_filename(_thread, fmt)
                                    path  = os.path.join(_dir, rel)
                                    os.makedirs(os.path.dirname(path), exist_ok=True)
                                    fname = os.path.basename(path)
                                    if os.path.exists(path):
                                        skipped.append(fname); continue
                                    if fmt == "html":
                                        self._do_save_html(path, _html, _thread, show_progress=False)
                                    elif fmt == "mht":
                                        self._do_save_mht(path, _html, _thread, show_progress=False)
                                    else:
                                        self._do_save_zip(path, _html, _thread, show_progress=False)
                                    saved.append(fname)
                                if saved:
                                    msg = f"{_reason}自動保存: {', '.join(saved)}"
                                    if skipped: msg += f"  (スキップ: {', '.join(skipped)})"
                                elif skipped:
                                    msg = f"{_reason}自動保存: スキップ（保存済み: {', '.join(skipped)}）"
                                else:
                                    msg = f"{_reason}自動保存: 保存なし"
                                self._main_thread_call.emit(lambda _m=msg: self._st_log.setText(_m))
                            except Exception as e:
                                self._main_thread_call.emit(lambda _r=_reason, _e=e:
                                    self._st_log.setText(f"{_r}自動保存エラー: {_e}"))

                        import threading as _threading
                        _threading.Thread(target=_do_save, daemon=True).start()
                        self._st_log.setText(f"{reason}自動保存中...")

        # ── タブ自動クローズ ──────────────────────────────────────────────
        _close_dead = getattr(s, "auto_close_dead_tab", False)
        _close_full = getattr(s, "auto_close_full_tab", False)
        # 逆NG自動オープン由来の落ちスレは、グローバル設定に関わらず閉じてメモリ解放。
        # （多数の逆NGスレが自動オープン→落ち後も残存しメモリが膨張するのを防ぐ。
        #   自動保存されるためタブを閉じても内容は失われない。手動オープン由来は対象外。）
        _rev_auto_close = (
            getattr(s, "auto_close_dead_reverse_ng", True)
            and (not is_full)
            and bool(url)
            and url in getattr(self._settings, "ng_reverse_opened_urls", set())
        )
        if _close_dead or _close_full or _rev_auto_close:
            should_close = ((is_full and _close_full)
                            or (not is_full and _close_dead)
                            or _rev_auto_close)
            # カタログから開いた直後（一度も正常表示していない）は自動クローズしない。
            # ただし逆NG自動オープン由来は例外。img板など回転が速い板では、開いた
            # 瞬間に既に404（dead-on-arrival, _known_res_count==0）のスレが大量に
            # 自動オープンされ、放置するとそれらが閉じられず404タブが累積するため、
            # 逆NG由来は開いた直後の404でも閉じる。
            if getattr(view, "_known_res_count", 0) == 0 and not _rev_auto_close:
                should_close = False
            # タブを開いた瞬間に既に死んでいた（1000到達済み・最初から404）スレは
            # 自動クローズしない（ユーザーが意図的に開いた死亡スレを勝手に閉じない）。
            # _opened_dead は初回読み込み時に死亡を検出した場合のみ True になる。
            # 逆NG自動オープン由来は「意図的に開いた」ではないため例外（上と同じ理由で閉じる）。
            if getattr(view, "_opened_dead", False) and not _rev_auto_close:
                should_close = False
            if should_close:
                # 一度自動クローズ済みのスレは再表示後クローズしない（url基準・既存ロジック維持）
                if url and url in self._auto_close_done:
                    return  # 既にクローズ済み
                # このビューに対して既に自動クローズをスケジュール済みなら何もしない。
                # url が空文字（404キャッシュフォールバック等）の場合、上の url 基準の
                # ガードだけでは毎ティック thread_dead 発火ごとに
                # QTimer.singleShot(_close_thread_view) が積み重なり、
                # 1回目のクローズでviewが破棄された後の2回目以降が
                # 「ThreadView already deleted」のRuntimeErrorとなって
                # 未処理例外が連発 → リソースリークからクラッシュに至るため、
                # view単位のフラグで多重スケジュールを防止する。
                if getattr(view, "_auto_close_scheduled", False):
                    return
                else:
                    skip_pinned = getattr(s, "auto_close_skip_pinned", False)
                    _pinned = False
                    if skip_pinned:
                        for ti in range(self._outer_tabs.count()):
                            pane = self._outer_tabs.widget(ti)
                            if not isinstance(pane, BoardPane):
                                continue
                            if view in getattr(pane, "_pinned", set()):
                                _pinned = True; break
                    if not _pinned:
                        if url:
                            self._auto_close_done.add(url)
                        view._auto_close_scheduled = True
                        QTimer.singleShot(1500, lambda _v=view: self._close_thread_view(_v))

    def _close_thread_view(self, view):
        """ThreadViewのタブを閉じる（自動クローズ用）。
        QTimer.singleShot(1500, ...) で予約後、その間に別経路で
        view のC++オブジェクトが既に破棄されている場合があり、
        その状態で tabs.indexOf(view) 等を呼ぶと
        RuntimeError: Internal C++ object already deleted で
        未処理例外となり、AutoRefreshManager にゾンビ参照が残って
        無限リフレッシュ→WebEngineProfile解放警告の連発→クラッシュ
        に繋がるため、isValid チェックと例外処理で確実に後始末する。"""
        try:
            from shiboken6 import isValid
            if not isValid(view):
                # C++側は既に破棄済み。AR等の残留参照だけ掃除して終了
                try:
                    self._ar_mgr.remove_by_view(view)
                except Exception:
                    pass
                return
        except ImportError:
            pass
        try:
            for ti in range(self._outer_tabs.count()):
                pane = self._outer_tabs.widget(ti)
                if not isinstance(pane, BoardPane):
                    continue
                tabs = pane._tabs
                idx = tabs.indexOf(view)
                if idx >= 0:
                    # pane.tab_closing を発火して _closed_tabs に積む（Ctrl+Shift+T で復帰可能に）
                    pane.tab_closing.emit(view)
                    self._ar_mgr.remove_by_view(view)
                    if tabs.count() > 1:
                        tabs.removeTab(idx)
                        _dispose_tab_view(view)
                    return
        except RuntimeError:
            # view のC++オブジェクトが処理中に破棄された場合の保険
            try:
                self._ar_mgr.remove_by_view(view)
            except Exception:
                pass

    def _on_tab_closing(self, view):
        """タブが閉じられる直前に情報をスタックに積む"""
        if not isinstance(view, ThreadView):
            return
        board = view._board
        if not board:
            return
        thread_no  = view._thread_no or 0
        thread_url = (view._thread.url if view._thread else "") or ""
        # スレタイトルを優先（ERR表示になっていても元のタイトルを使う）
        label = ""
        th = view._thread or getattr(view, '_last_valid_thread', None)
        if th and getattr(th, 'title', ''):
            label = th.title
        # スレタイトルが取れなければタブテキストにフォールバック（ERRは除外）
        if not label:
            for ti in range(self._outer_tabs.count()):
                pane = self._outer_tabs.widget(ti)
                if not isinstance(pane, BoardPane):
                    continue
                idx = pane._tabs.indexOf(view)
                if idx >= 0:
                    tab_text = pane._tabs.tabText(idx).strip()
                    if tab_text and tab_text != "ERR":
                        label = tab_text
                    break
        if not label:
            label = f"No.{thread_no}"
        # 「 - ○○@ふたば」などのサフィックスを除去
        import re as _re
        label = _re.sub(r'\s*-\s*[^-]+@\S+$', '', label).strip()
        if thread_no:
            self._closed_tabs.append(
                (board.url, board.name, thread_no, thread_url, label))
            # スタックは設定件数まで
            _max = getattr(self._settings, "recent_closed_max", 30)
            if len(self._closed_tabs) > _max:
                self._closed_tabs.pop(0)

    def _reopen_closed_tab(self):
        """Ctrl+Shift+T: 閉じたタブを再オープン"""
        if not self._closed_tabs:
            self._st_log.setText("再オープンできるタブがありません")
            return
        board_url, board_name, thread_no, thread_url, label = self._closed_tabs.pop()
        # 既存の板タブを検索（base_urlで一致判定）
        board_base = board_url.rsplit("/futaba.htm", 1)[0].rstrip("/") + "/"
        target_board: BoardInfo | None = None
        for ti in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(ti)
            if isinstance(pane, BoardPane) and pane._board:
                if pane._board.base_url == board_base:
                    target_board = pane._board
                    break
        if target_board is None:
            # 板タブが閉じられている場合はスタックに戻す
            self._closed_tabs.append((board_url, board_name, thread_no, thread_url, label))
            self._st_log.setText(f"板タブが閉じられているため復元できません: {board_name}")
            return
        self._open_thread(target_board, thread_no)

    def _board_display_name(self, board_name: str, board_url: str) -> str:
        """板名表示用：二次元裏の場合にサブドメインを付加する"""
        if board_name == "二次元裏" and board_url:
            import re as _re
            m = _re.search(r'//(\w+)\.2chan\.net/', board_url)
            if m:
                return f"二次元裏({m.group(1)})"
        return board_name

    def _build_recent_closed_menu(self):
        """「最近閉じたスレ」サブメニューを動的構築"""
        self._menu_recent_closed.clear()
        if not self._closed_tabs:
            a = self._menu_recent_closed.addAction("（なし）")
            a.setEnabled(False)
            return
        # 新しい順（末尾が最新）で表示
        for i, (board_url, board_name, thread_no, thread_url, label) in enumerate(
                reversed(self._closed_tabs)):
            bdn = self._board_display_name(board_name, board_url)
            text = f"{bdn} / {label}" if label else f"{bdn} / No.{thread_no}"
            act = self._menu_recent_closed.addAction(text)
            _idx = len(self._closed_tabs) - 1 - i
            act.triggered.connect(lambda checked=False, idx=_idx: self._reopen_closed_at(idx))
        self._menu_recent_closed.addSeparator()
        self._menu_recent_closed.addAction("すべてクリア").triggered.connect(
            lambda: (self._closed_tabs.clear(),
                     self._st_log.setText("閉じたスレの履歴をクリアしました")))

    def _reopen_closed_at(self, idx: int):
        """指定インデックスの閉じたタブを再オープン"""
        if idx < 0 or idx >= len(self._closed_tabs):
            return
        board_url, board_name, thread_no, thread_url, label = self._closed_tabs.pop(idx)
        board_base = board_url.rsplit("/futaba.htm", 1)[0].rstrip("/") + "/"
        target_board = None
        for ti in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(ti)
            if isinstance(pane, BoardPane) and pane._board:
                if pane._board.base_url == board_base:
                    target_board = pane._board; break
        if target_board is None:
            self._closed_tabs.insert(idx, (board_url, board_name, thread_no, thread_url, label))
            self._st_log.setText(f"板タブが閉じられているため復元できません: {board_name}")
            return
        self._open_thread(target_board, thread_no)

    def _build_recent_images_menu(self):
        """「最近開いた画像」サブメニューを動的構築"""
        self._menu_recent_images.clear()
        if not self._recent_images:
            a = self._menu_recent_images.addAction("（なし）")
            a.setEnabled(False)
            return
        for rec in self._recent_images:
            url  = rec.get("url", "")
            name = rec.get("name", "") or url.split("/")[-1]
            bname = rec.get("board_name", "")
            burl  = rec.get("board_url", "")
            if bname:
                bdn = self._board_display_name(bname, burl)
                text = f"{bdn} / {name[:40]}"
            else:
                text = name[:50]
            act = self._menu_recent_images.addAction(text)
            act.setToolTip(url)
            act.triggered.connect(lambda checked=False, u=url: self._open_recent_image(u))

        # ホバー時にサムネをQToolTipで表示
        _thumb_cache: dict = {}   # url -> QPixmap (取得済み) or None (失敗)

        def _thumb_url(url: str) -> str:
            """/src/XXX.ext → /thumb/XXXs.jpg に変換"""
            import re as _re
            m = _re.match(r'(https?://.+)/src/(.+?)(\.[^.]+)$', url)
            if m:
                return f"{m.group(1)}/thumb/{m.group(2)}s.jpg"
            return url

        def _on_hovered(action):
            url = action.toolTip()
            if not url:
                return
            turl = _thumb_url(url)
            if turl in _thumb_cache:
                _show_thumb(action, turl)
                return
            # BGスレッドでサムネ取得
            def _fetch():
                try:
                    data = self._fetcher.fetch_image_bytes(turl)
                    # QPixmapはGUIスレッド専用のためBGスレッドではQImageを使う
                    img = QImage()
                    if data and img.loadFromData(data):
                        img = img.scaled(120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                        _thumb_cache[turl] = img
                    else:
                        _thumb_cache[turl] = None
                except Exception:
                    _thumb_cache[turl] = None
                # BGスレッドからのQTimer.singleShotは禁止 → Signal経由
                self._main_thread_call.emit(lambda u=turl, a=action: _show_thumb(a, u))
            threading.Thread(target=_fetch, daemon=True).start()

        def _show_thumb(action, url):
            img = _thumb_cache.get(url)
            if img is None:
                return
            # QToolTipはHTMLを表示できるのでimg埋め込み
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            b64 = buf.data().toBase64().data().decode()
            QToolTip.showText(
                self._menu_recent_images.pos()
                + self._menu_recent_images.actionGeometry(action).topRight(),
                f'<img src="data:image/png;base64,{b64}">',
                self._menu_recent_images)

        self._menu_recent_images.hovered.connect(_on_hovered)
        self._menu_recent_images.addSeparator()
        self._menu_recent_images.addAction("すべてクリア").triggered.connect(
            lambda: (self._recent_images.clear(),
                     self._st_log.setText("開いた画像の履歴をクリアしました")))

    def _open_recent_image(self, url: str):
        """最近開いた画像をImageTabViewで開く"""
        _mode = getattr(self._settings, "image_display_mode", 0)
        if _mode == 2 and url.startswith(("http://", "https://")):  # 外部ブラウザ
            _open_url(url)
            return
        inner = self._active_inner()
        if not inner:
            self._st_log.setText("画像を開ける板タブがありません")
            return
        # 既存の画像タブに同一URLがあれば切り替え
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ImageTabView) and w._img_list and 0 <= w._idx < len(w._img_list):
                if w._img_list[w._idx].get("url") == url:
                    inner.setCurrentIndex(i); return
        name = url.split("/")[-1][:14]
        img_list = [{"url": url, "name": url.split("/")[-1]}]
        view = ImageTabView(url, img_list, 0, self._fetcher, inner)
        view.set_settings(self._settings)
        view.open_settings.connect(lambda: self._open_settings("画像保存"))
        view.image_navigated.connect(self._record_recent_image)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        if _mode == 3:  # 隣タブ
            i = inner.insertTab(inner.currentIndex() + 1, view, f"🖼 {name}")
        else:
            i = inner.addTab(view, f"🖼 {name}")
        inner.setCurrentIndex(i)

    def _close_ng_thread(self, view, inner):
        """NGスレッドを開いたら即閉じる"""
        idx = inner.indexOf(view)
        if idx >= 0:
            inner.removeTab(idx)
            _dispose_tab_view(view)

    def _close_current_tab(self):
        inner = self._active_inner()
        if inner:
            cur = inner.currentIndex()
            if inner.count() > 1:
                w = inner.widget(cur)
                inner.removeTab(cur)
                if not isinstance(w, CatalogView):
                    _dispose_tab_view(w)
        else:
            idx = self._outer_tabs.currentIndex()
            if idx > 0:
                w = self._outer_tabs.widget(idx)
                self._outer_tabs.removeTab(idx)
                self._dispose_pane(w)

    def _on_outer_tab_changed(self, _idx: int):
        # 外側タブ履歴（閉じたとき前のタブに戻る用）
        if hasattr(self, '_outer_prev_idx'):
            _old = self._outer_prev_idx
            if _old >= 0 and _old != _idx:
                if not self._outer_tab_history or self._outer_tab_history[-1] != _old:
                    self._outer_tab_history.append(_old)
        self._outer_prev_idx = _idx
        # 非アクティブになった板ペイン内のImageTabViewのメディアを一時停止
        for i in range(self._outer_tabs.count()):
            if i == _idx:
                continue
            pane = self._outer_tabs.widget(i)
            if not isinstance(pane, BoardPane):
                continue
            for j in range(pane._tabs.count()):
                w = pane._tabs.widget(j)
                if isinstance(w, ImageTabView) and hasattr(w, 'pause_media'):
                    w.pause_media()
        self._update_url_from_active()
        self._refresh_tab_pane()
        QTimer.singleShot(0, self._refresh_status_for_active_tab)
        QTimer.singleShot(0, self._refresh_title_bar)
        self._sync_post_dialog_roll()

    def _refresh_tab_pane(self):
        """タブペインを現在のタブ構成で更新（メインスレッド保証）"""
        QTimer.singleShot(0, self._do_refresh_tab_pane)

    def _do_refresh_tab_pane(self):
        try:
            self._tree_pane._tab_pane.refresh(self._outer_tabs)
            self._tree_pane._fav_pane.refresh(self._outer_tabs)
        except Exception as e:
            print(f"[_refresh_tab_pane] ERROR: {e}")

    def _catalog_icon(self) -> "QIcon":
        """カタログタブ用アイコン。設定OFFならQIcon()。theme/catalog.pngがなければQIcon()。"""
        if False:  # カタログアイコンは常に表示（設定削除済み）
            return QIcon()
        if self._catalog_icon_checked:
            return self._catalog_icon_cache or QIcon()
        self._catalog_icon_checked = True
        from pathlib import Path as _Path
        _theme_root = _Path(__file__).parent / "theme"
        # テーマフォルダ優先 → theme/直下 fallback
        _found = None
        for _d in [ThemeManager.theme_dir(), _theme_root]:
            for ext in ("png", "svg", "ico", "jpg"):
                _p = _d / f"catalog.{ext}"
                if _p.exists():
                    _found = _p; break
            if _found:
                break
        if _found:
            pix = QPixmap(str(_found))
            if not pix.isNull():
                pix = pix.scaled(16, 16,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                self._catalog_icon_cache = QIcon(pix)
                return self._catalog_icon_cache
        return QIcon()

    def _on_tab_icon_ready(self, inner, view, data: bytes):
        """BGスレッドから届いた画像データをメインスレッドでタブアイコンに設定"""
        try:
            pix = QPixmap()
            ok = pix.loadFromData(data)
            if ok and not pix.isNull():
                scaled = pix.scaled(16, 16,
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                idx2 = inner.indexOf(view)
                if idx2 >= 0:
                    inner.setTabIcon(idx2, scaled)
        except Exception as e:
            pass

    def _on_tab_pane_update(self, url: str):
        """タブペインの[更新]ボタン"""
        for ti in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(ti)
            if not isinstance(pane, BoardPane):
                continue
            for ii in range(pane._tabs.count()):
                w = pane._tabs.widget(ii)
                if isinstance(w, ThreadView) and w._thread and w._thread.url == url:
                    w.reload_thread(); return
                if isinstance(w, CatalogView) and pane._board and pane._board.url == url:
                    w.reload(); return

    def _on_tab_pane_close(self, url: str):
        """タブペインの[×]ボタン"""
        for ti in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(ti)
            if not isinstance(pane, BoardPane):
                continue
            for ii in range(pane._tabs.count()):
                w = pane._tabs.widget(ii)
                if isinstance(w, ThreadView) and w._thread and w._thread.url == url:
                    if pane._tabs.count() > 1:
                        pane._tabs.removeTab(ii)
                        _dispose_tab_view(w)
                    self._refresh_tab_pane(); return

    def _on_tab_pane_select(self, url: str):
        """タブペインのアイテムクリック → 該当タブをアクティブに"""
        for ti in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(ti)
            if not isinstance(pane, BoardPane):
                continue
            for ii in range(pane._tabs.count()):
                w = pane._tabs.widget(ii)
                if isinstance(w, ThreadView) and w._thread and w._thread.url == url:
                    self._outer_tabs.setCurrentIndex(ti)
                    pane._tabs.setCurrentIndex(ii); return
        self._update_zoom_label()
        self._refresh_status_for_active_tab()

    def _refresh_status_for_active_tab(self):
        """アクティブなビューのステータス情報をステータスバーに反映"""
        inner = self._active_inner()
        if not inner:
            self._clear_thread_status(); return
        view = inner.currentWidget()
        if isinstance(view, ThreadView) and getattr(view, '_thread', None):
            view._emit_status_info(view._thread, 0)
        elif isinstance(view, CatalogView):
            view._emit_catalog_status()
        else:
            self._clear_thread_status()


    def _change_zoom(self, delta: float):
        """現在の ThreadView / CatalogView のズームを変更し表示を更新"""
        inner = self._active_inner()
        if not inner:
            return
        cur = inner.currentWidget()
        views = []
        if hasattr(cur, "_view"):
            views.append(cur._view)
        # 全タブの view も更新
        if inner:
            for i in range(inner.count()):
                w = inner.widget(i)
                if hasattr(w, "_view"):
                    views.append(w._view)
        if not views:
            return
        cur_zoom = views[0].zoomFactor()
        new_zoom = max(0.5, min(3.0, round(cur_zoom + delta, 1)))
        for v in views:
            v.setZoomFactor(new_zoom)
        self._zoom_lbl.setText(f"{int(new_zoom*100)}%")

    def _update_zoom_label(self):
        """アクティブビューのズーム率を表示"""
        inner = self._active_inner()
        if not inner:
            self._zoom_lbl.setText(f"{int(_default_zoom()*100)}%")
            return
        cur = inner.currentWidget()
        if hasattr(cur, "_view"):
            z = cur._view.zoomFactor()
            self._zoom_lbl.setText(f"{int(round(z*100))}%")
        else:
            self._zoom_lbl.setText(f"{int(_default_zoom()*100)}%")

    def _update_url_from_active(self, _=None):
        """アクティブなビューに合わせて URL バーを更新"""
        inner = self._active_inner()
        if not inner:
            return
        w = inner.currentWidget()
        if isinstance(w, ThreadView) and w._thread:
            self._url_bar.setText(w._thread.url or "")
        elif isinstance(w, CatalogView) and inner._board:
            self._url_bar.setText(inner._board.catalog_url)
        else:
            self._url_bar.clear()

    # ── アクション ────────────────────────────────────────────────────────────

    def _refresh_current(self):
        inner = self._active_inner()
        if not inner: return
        w = inner.currentWidget()
        if isinstance(w, ThreadView):       w.reload_thread()
        elif isinstance(w, CatalogView) and inner._board: w.load(inner._board)

    def _refresh_board(self):
        inner = self._active_inner()
        if inner: self._show_board_view("catalog", inner._board)

    def _open_reply(self, board: BoardInfo, resto: int, quote_no: int, qt: str):
        inner = self._active_inner()

        def _on_success(new_no: int = 0):
            """投稿成功後: 該当スレッドを自動更新"""
            if not inner:
                return
            # 自分のレスとして記録
            if new_no and new_no > 0:
                thread_url = board.base_url + f"res/{resto}.htm"
                nos = self._settings.my_post_nos.setdefault(thread_url, [])
                if new_no not in nos:
                    nos.append(new_no)
            # スレッド履歴に「最後に書き込んだ日時」を記録
            self._settings.mark_history_posted(board.name, resto)
            self._settings.save()
            if getattr(self, "_hist_pane", None) is not None:
                try: self._hist_pane.refresh()
                except Exception: pass
            for i in range(inner.count()):
                w = inner.widget(i)
                if isinstance(w, ThreadView) and w._thread_no == resto:
                    # 投稿後スクロール設定がONなら、更新完了後に最下部へ送るフラグを立てる
                    if getattr(self._settings, "scroll_after_post", True):
                        w._scroll_bottom_after_update = True
                    w.reload_thread()
                    return

        # 同一restoのダイアログが既に開いていれば前面表示して引用追記
        if not hasattr(self, '_post_dialogs'):
            self._post_dialogs = {}
        existing = self._post_dialogs.get(resto)
        if existing is not None and existing.isVisible():
            if qt:
                existing.append_quote(qt)
            else:
                existing.raise_()
                existing.activateWindow()
            return

        # スレタイ取得（同一restoのThreadViewから）
        _thread_title = ""
        if inner:
            for _i in range(inner.count()):
                _w = inner.widget(_i)
                if isinstance(_w, ThreadView) and _w._thread_no == resto and _w._thread:
                    _thread_title = _w._thread.title or f"No.{resto}"
                    break
        dlg = PostDialog(board, self._fetcher, self._settings,
                         resto=resto, quote_text=qt,
                         on_success=_on_success, parent=self)
        dlg._thread_title = _thread_title
        self._post_dialogs[resto] = dlg
        # ダイアログが閉じたら辞書から削除
        dlg.finished.connect(lambda _, r=resto: self._post_dialogs.pop(r, None))
        # タイトルバークリック → 対応スレタブをアクティブ化
        dlg.activate_tab.connect(lambda r=resto: self._activate_thread_tab(r))
        # 投稿後ピン留め設定がONなら、投稿成功時に現在のスレタブをピン打ち
        def _pin_current_tab():
            _inner = self._active_inner()
            if not _inner: return
            w = _inner.currentWidget()
            if isinstance(w, ThreadView):
                _inner._pin_tab(w)
        dlg.pin_after_post.connect(_pin_current_tab)
        # 投稿後最下部スクロールは ThreadView._scroll_bottom_after_update フラグで
        # 更新完了後に行う（_on_success 内でセット）。旧 scroll_after_post の
        # 固定遅延スクロールは更新完了前に走り最下部に届かないため接続しない。
        dlg.setModal(False); dlg.show()

    def _reply_current(self):
        inner = self._active_inner()
        if not inner: return
        w = inner.currentWidget()
        if isinstance(w, ThreadView) and inner._board:
            self._open_reply(inner._board, w._thread_no, 0, "")

    def _new_thread(self):
        inner = self._active_inner()
        if not inner:
            QMessageBox.information(self, "情報", "先に板を選択してください"); return
        board = inner._board

        def _on_new_thread_success(new_no: int):
            if new_no:
                print(f'[new_thread] 新スレ立て成功 No.{new_no} → タブで開く')
                # 新スレはレス1件のみのため、画像/引用モード設定だと空表示に
                # なってしまう → 通常モードを強制して開く
                self._open_thread(board, new_no, open_mode_override='')
                # 新スレ作成も「書き込み」として履歴に記録（エントリが未作成なら作る）
                if not self._settings.mark_history_posted(board.name, new_no):
                    self._settings.add_history(board.name, new_no, f"No.{new_no}", board.url)
                    self._settings.mark_history_posted(board.name, new_no)
                self._settings.save()
                if getattr(self, "_hist_pane", None) is not None:
                    try: self._hist_pane.refresh()
                    except Exception: pass
                # PostDialog等の背後に隠れないようメインウィンドウを前面化
                self.raise_()
                self.activateWindow()
            else:
                # No.が取れなかった場合はカタログを更新して対応
                print('[new_thread] 新スレNo.不明 → カタログ更新')
                cur = inner.widget(0)
                if hasattr(cur, 'reload'):
                    cur.reload()

        PostDialog(board, self._fetcher, self._settings,
                   resto=0, on_success=_on_new_thread_success, parent=self).show()

    def _open_image_tab(self, url: str, img_list: list, idx: int):
        _mode = getattr(self._settings, "image_display_mode", 0)
        # 外部ブラウザ (http系URLのみ。ログ内相対パスはタブ表示)
        if _mode == 2 and url.startswith(("http://", "https://")):
            _open_url(url)
            return
        # 画像表示モード=ウインドウ → 専用ウインドウで開く
        if _mode == 1:
            self._open_image_window(url, img_list, idx)
            return
        inner = self._active_inner()
        if not inner: return
        # 既存の画像タブに同一URLがあれば切り替えるだけ
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ImageTabView) and w._img_list and 0 <= w._idx < len(w._img_list):
                if w._img_list[w._idx].get("url") == url:
                    inner.setCurrentIndex(i); return
        view = ImageTabView(url, img_list, idx, self._fetcher, inner)
        # 元のThreadViewを記録（img_list更新追跡用）
        src = inner.currentWidget()
        if isinstance(src, ThreadView):
            view._src_thread_view = src
        else:
            pass
        view.set_settings(self._settings)
        view.open_settings.connect(lambda: self._open_settings("画像保存"))
        view.image_navigated.connect(self._record_recent_image)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        name = (img_list[idx].get("name", "画像")[:14]
                if img_list and 0 <= idx < len(img_list) else "画像")
        if _mode == 3:  # 隣タブ
            i = inner.insertTab(inner.currentIndex() + 1, view, f"🖼 {name}")
        else:
            i = inner.addTab(view, f"🖼 {name}")
        inner.setCurrentIndex(i)
        self._record_recent_image(url, img_list, idx)

    def _open_image_tab_bg(self, url: str, img_list: list, idx: int):
        """中クリック：画像タブを非アクティブ（バックグラウンド）で開く。既存タブがあれば何もしない。"""
        _mode = getattr(self._settings, "image_display_mode", 0)
        if _mode == 2 and url.startswith(("http://", "https://")):  # 外部ブラウザ
            _open_url(url)
            return
        # 画像表示モード=ウインドウ → 専用ウインドウで開く（フォアグラウンドにはしない）
        if _mode == 1:
            self._open_image_window(url, img_list, idx, activate=False)
            return
        inner = self._active_inner()
        if not inner: return
        # 既存の画像タブに同一URLがあれば開かない
        for i in range(inner.count()):
            w = inner.widget(i)
            if isinstance(w, ImageTabView) and w._img_list and 0 <= w._idx < len(w._img_list):
                if w._img_list[w._idx].get("url") == url:
                    return
        view = ImageTabView(url, img_list, idx, self._fetcher, inner)
        # 元のThreadViewを記録（img_list更新追跡用）
        src = inner.currentWidget()
        if isinstance(src, ThreadView):
            view._src_thread_view = src
        view.set_settings(self._settings)
        view.open_settings.connect(lambda: self._open_settings("画像保存"))
        view.image_navigated.connect(self._record_recent_image)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        name = (img_list[idx].get("name", "画像")[:14]
                if img_list and 0 <= idx < len(img_list) else "画像")
        if _mode == 3:  # 隣タブ
            inner.insertTab(inner.currentIndex() + 1, view, f"🖼 {name}")   # 非アクティブ
        else:
            inner.addTab(view, f"🖼 {name}")   # setCurrentIndex しない → 非アクティブ
        self._record_recent_image(url, img_list, idx)

    def _open_image_window(self, url: str, img_list: list, idx: int, activate: bool = True):
        """画像表示モード=ウインドウ。専用ウインドウ(1つのみ)で画像を開く。
        既存ウインドウがあれば再利用し、新しい画像に差し替える。"""
        # 元のThreadView（img_list更新追跡用）
        src = None
        inner = self._active_inner()
        if inner is not None:
            cw = inner.currentWidget()
            if isinstance(cw, ThreadView):
                src = cw
        win = self._image_window
        # 既存ウインドウが破棄済み(参照切れ)なら作り直す
        try:
            import shiboken6
            if win is not None and not shiboken6.isValid(win):
                win = self._image_window = None
        except Exception:
            pass
        if win is None:
            view = ImageTabView(url, img_list, idx, self._fetcher, None)
            view._src_thread_view = src
            view.set_settings(self._settings)
            view.open_settings.connect(lambda: self._open_settings("画像保存"))
            view.image_navigated.connect(self._record_recent_image)
            # ウインドウモード中の中クリックは同じウインドウに表示
            view.open_image_tab_bg.connect(
                lambda u, l, i: self._open_image_window(u, l, i, activate=False))
            win = ImageWindow(view, self._settings, self)
            self._image_window = win
        else:
            view = win.image_view
            view._src_thread_view = src
            view.set_settings(self._settings)
            view.load_image(url, img_list, idx)
        win.show()
        if activate:
            win.raise_()
            win.activateWindow()
        self._record_recent_image(url, img_list, idx)

    def _update_image_window_img_list(self, src_view, img_list):
        """画像ウインドウ(ウインドウモード)の画像が src_view 由来なら img_list を更新する。"""
        win = getattr(self, "_image_window", None)
        if win is None:
            return
        try:
            import shiboken6
            if not shiboken6.isValid(win):
                self._image_window = None
                return
        except Exception:
            pass
        try:
            iv = win.image_view
            if iv is not None and iv._src_thread_view is src_view:
                iv.update_img_list(img_list)
        except Exception:
            pass

    def _record_recent_image(self, url: str, img_list: list, idx: int):
        """最近開いた画像を記録する"""
        name = img_list[idx].get("name", "") if img_list and 0 <= idx < len(img_list) else ""
        # アクティブ板の情報を取得
        pane = self._active_inner()
        board_name = pane._board.name if pane and pane._board else ""
        board_url  = pane._board.url  if pane and pane._board else ""
        # 重複除去（同じURLは先頭に移動）
        self._recent_images = [r for r in self._recent_images if r.get("url") != url]
        self._recent_images.insert(0, {
            "url": url, "name": name,
            "board_name": board_name, "board_url": board_url,
        })
        _max = getattr(self._settings, "recent_images_max", 30)
        if len(self._recent_images) > _max:
            self._recent_images = self._recent_images[:_max]

    # ── ログ保存 共通 ────────────────────────────────────────────────────────
    def _log_save_dir(self) -> str:
        """保存先ディレクトリを返す（設定がなければ実行ファイル隣の logs/）。
        設定先が作成できない場合（ドライブ未接続等）は既定の logs/ にフォールバックし、
        保存ダイアログの表示まで処理を継続させる。"""
        import sys, os
        base_default = os.path.join(
            os.path.dirname(os.path.abspath(sys.argv[0])), "logs")
        d = getattr(self._settings, "log_save_dir", "").strip() or base_default
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"[LOG] 保存先が見つかりません: {d} ({e}) → 既定の logs/ にフォールバック")
            d = base_default
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
            try:
                self._st_log.setText("ログ保存先が見つかりません → 既定のlogsフォルダを使用")
            except Exception:
                pass
        return d

    def _log_full_path(self, thread, ext: str) -> str:
        """保存先ディレクトリ + テンプレートから完全パスを返し、必要なフォルダを作成する"""
        base = self._log_save_dir()
        rel  = self._log_filename(thread, ext)   # 例: "20240601_123456/No.123_title.html"
        full = os.path.join(base, rel)
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
        except OSError:
            pass   # 作成不可でもパスは返す（保存ダイアログで選び直せる）
        return full

    def _log_filename(self, thread, ext: str) -> str:
        """設定のテンプレートに従いファイル名を生成する。
        変数: {no} {title} {board} {date} {time} {datetime} {逆NG}
        テンプレートに '/' が含まれる場合、先頭部分をサブフォルダとして扱う。"""
        import re, datetime
        tpl = getattr(self._settings, "log_filename_template", "No.{no}_{title}")
        now  = datetime.datetime.now()
        no   = thread.no
        # board名（二次元裏の場合はサブドメインを付加）
        if hasattr(thread, 'board') and thread.board:
            _bname = thread.board.name or ""
            _burl  = getattr(thread.board, 'url', '') or ""
            if _bname == "二次元裏" and _burl:
                _m = re.search(r'//(\w+)\.2chan\.net/', _burl)
                board = f"二次元裏({_m.group(1)})" if _m else _bname
            else:
                board = _bname
        else:
            board = ""
        # OP1行目
        title = ""
        if thread.res_list:
            op   = thread.res_list[0]
            raw  = (op.comment_text or "").strip()
            lines_raw = raw.splitlines()
            lines = []
            skip_bracket = False
            for _l in lines_raw:
                _s = _l.strip()
                if not _s:
                    continue
                if _s == '[':
                    skip_bracket = True
                    continue
                if skip_bracket:
                    if re.match(r'^[\w.\-:]+$', _s):
                        continue
                    if _s == ']':
                        skip_bracket = False
                        continue
                    skip_bracket = False
                if re.match(r'^\[[\w.\-:]+\]$', _s):
                    continue
                lines.append(_s)
            line = lines[0] if lines else ""
            line = re.sub(r'[\\/:*?"<>|]', '', line)
            title = line[:40]
        # 逆NG: OPがマッチした逆NGワード（レススコープ＋カタログスコープ、重複除去）
        revng = ""
        _ngf = getattr(self._settings, "ng_filter", None)
        if _ngf is not None and thread.res_list:
            _op = thread.res_list[0]
            _pats = []
            _seen: set = set()
            def _collect(_lst):
                for _w in (_lst or []):
                    _p = (_w.get("pattern") or "").strip() if isinstance(_w, dict) else ""
                    if _p and _p not in _seen:
                        _seen.add(_p)
                        _pats.append(_p)
            try:
                _collect(_ngf.get_matched_reverse_ng_words(_op))
            except Exception:
                pass
            try:
                _t = (getattr(_op, "subject", "") or "").strip() or title
                if _t:
                    class _CE:        # get_matched_reverse_ng_words_catalog は .title のみ参照
                        pass
                    _ce = _CE(); _ce.title = _t
                    _collect(_ngf.get_matched_reverse_ng_words_catalog(_ce))
            except Exception:
                pass
            revng = re.sub(r'[\\/:*?"<>|]', '', "_".join(_pats))[:60]

        # {逆NG} と {逆NG:代替文字} を先に解決する。
        # ・マッチあり → マッチ語、・マッチなし → ':' 以降の代替文字（無ければ空）。
        # str.format は {逆NG:...} を書式指定と誤解するため、ここで literal 化しておく。
        def _resolve_revng(m):
            _d = m.group(1)                       # ':' 以降（無ければ None）
            _v = revng if revng else (_d if _d is not None else "")
            return _v.replace("{", "{{").replace("}", "}}")
        tpl = re.sub(r'\{(?:逆NG|revng)(?::([^}]*))?\}', _resolve_revng, tpl)

        # テンプレート変数展開
        vars_ = dict(
            no       = no,
            title    = title,
            board    = re.sub(r'[\\/:*?"<>|]', '', board),
            date     = now.strftime("%Y%m%d"),
            time     = now.strftime("%H%M%S"),
            datetime = now.strftime("%Y%m%d_%H%M%S"),
        )
        try:
            expanded = tpl.format(**vars_)
        except (KeyError, IndexError, ValueError):
            # 未知の変数等で失敗した場合は既定テンプレートにフォールバック
            expanded = "{date}/{date}_No.{no}_{title}".format(**vars_)
        # '/' でフォルダとファイル名に分割
        parts = expanded.split('/')
        file_part = parts[-1]
        folder_parts = parts[:-1]
        # 各パートからファイル名不正文字を除去
        def _clean_part(s):
            return re.sub(r'[\\:*?"<>|]', '', s).strip("_. ")
        file_part    = _clean_part(file_part)
        folder_parts = [_clean_part(p) for p in folder_parts if p.strip()]
        return os.path.join(*folder_parts, f"{file_part}.{ext}") if folder_parts \
               else f"{file_part}.{ext}"

    def _get_thread_for_log(self):
        """アクティブな ThreadView とスレッドデータを返す。なければ None,None"""
        inner = self._active_inner()
        if not inner: return None, None
        cur = inner.currentWidget()
        if not isinstance(cur, ThreadView): return None, None
        thread = getattr(cur, '_thread', None)
        if not thread:
            self.statusBar().showMessage("スレッドが読み込まれていません", 3000)
            return None, None
        return cur, thread

    def _build_log_html(self, cur, thread) -> str:
        """方式A: ふたば原本HTMLを取得し、広告のみ除去した素のhtmを返す。
        ・user.css / THREAD_CSS / [google]Lensリンク等のアプリ要素は一切含まない
        ・NGは無視（原本どおり全レス・全画像を残す）
        ・保存時に1回フルGETして最新を取得（差分込みで完全化、失敗時はキャッシュ原本）
        ・「サムネ保存しない」加工・画像のローカル化・URL絶対化は _do_save_* 側で適用
        取得もキャッシュも無い場合、または取得した原本htmのレス数が
        モデル(全レス保持)より少ない場合は、完全なモデルから描画する。"""
        raw = None
        try:
            raw = self._fetcher.fetch_raw_thread_html(thread.board, thread.no)
        except Exception as e:
            print(f"[LOG] 原本html取得失敗: {e}")
        if not raw:
            return self._set_log_title(
                self._strip_nosave_images(
                    self._build_log_html_rendered(cur, thread), thread), thread)
        # 自動更新は差分APIのみで生htmキャッシュを更新しないため、フルGET失敗時に
        # 古いキャッシュ(=開いた時点の少ないレス数)へフォールバックすることがある。
        # 原本htmのレス数(OP1 + class=rtd) がモデルより少なければ、
        # 不足分のレスを「ふたば生htm形式」で末尾に補完する（方式A形式を維持）。
        # ※ 従来は方式B(アプリ描画)へフォールバックしていたため、保存ログに
        #   [google]等のアプリ要素が混入し「サムネ保存しない」も不発になっていた。
        model_n = len(thread.res_list)
        if model_n > 1:
            raw_n = raw.count("class=rtd") + raw.count('class="rtd"') + 1  # +1=OP
            if raw_n < model_n:
                print(f"[LOG] 原本htmレス不足 raw≈{raw_n} < model={model_n} "
                      f"→ 不足レスを生htm形式で補完")
                raw = self._append_missing_res_raw(raw, thread, raw_n)
        return self._set_log_title(
            self._strip_nosave_images(self._strip_futaba_ads(raw), thread), thread)

    def _op_thread_name(self, thread) -> str:
        """0レスめ(OP)のスレ名（ブラウザtitle用）。OPコメント先頭の有効行→題名→
        thread.title の順でフォールバック。ファイル名生成と同じくOP先頭行を優先する。"""
        import re as _re
        try:
            op = thread.res_list[0]
        except Exception:
            op = None
        if op is not None:
            for _l in (getattr(op, "comment_text", "") or "").splitlines():
                _s = _l.strip()
                if _s and not _re.match(r'^\[[\w.\-:]+\]$', _s):
                    return _s[:80]
            sub = (getattr(op, "subject", "") or "").strip()
            if sub:
                return sub[:80]
        t = (getattr(thread, "title", "") or "").rsplit(" - ", 1)[0].strip()
        return t[:80] or f"No.{getattr(thread, 'no', '')}"

    def _set_log_title(self, html: str, thread) -> str:
        """保存HTMLの<title>を0レスめのスレ名に置換する（無ければ</head>直前に挿入）。
        原本futabaの切り詰め/汎用title・空titleのままだとブラウザのタブに
        ファイル名(20260625_No…)が出てしまうのを防ぐ。"""
        import re as _re, html as _hm
        name = self._op_thread_name(thread)
        if not name:
            return html
        tag = f"<title>{_hm.escape(name)}</title>"
        if _re.search(r"<title>.*?</title>", html, _re.I | _re.S):
            return _re.sub(r"<title>.*?</title>", lambda _m: tag, html,
                           count=1, flags=_re.I | _re.S)
        if _re.search(r"</head>", html, _re.I):
            return _re.sub(r"</head>", lambda _m: tag + "</head>", html,
                           count=1, flags=_re.I)
        return html

    def _append_missing_res_raw(self, raw: str, thread, raw_n: int) -> str:
        """生futaba htm に、モデルの不足レス(res_list[raw_n:])を
        ふたば生htm形式の返信ブロックとして末尾(スレッド終了直前)に挿入する。
        方式Aの構造(<a href=本画像><img src=サムネ>)を保つため、
        「サムネ保存しない」差し替え・メディア収集・ローカル化が正しく機能する。"""
        try:
            missing = thread.res_list[raw_n:]
        except Exception:
            return raw
        if not missing:
            return raw
        blocks = []
        for r in missing:
            if getattr(r, "is_op", False):
                continue
            blocks.append(self._res_to_raw_reply_html(r))
        if not blocks:
            return raw
        inject = "".join(blocks)
        # 挿入アンカー（優先順）: clear:left div → スレッド終了コメント → </body>
        for anchor in ('<div style="clear:left"></div>',
                       '<!--スレッド終了-->',
                       '</body>'):
            pos = raw.find(anchor)
            if pos >= 0:
                return raw[:pos] + inject + raw[pos:]
        return raw + inject

    def _res_to_raw_reply_html(self, res) -> str:
        """ResData 1件を、ふたば生htm形式の返信ブロック(<table>...</table>)に変換する。"""
        import html as _hm
        no   = res.no
        name = _hm.escape(res.name or "としあき")
        dts  = _hm.escape(res.datetime_str or "")
        ids  = res.id_str or ""
        # cnw = 日時 + " ID:xxxx"（datetime_str に既に ID: を含む場合は二重付与しない）
        if ids and "ID:" not in dts:
            cnw = f"{dts} ID:{_hm.escape(ids)}"
        else:
            cnw = dts
        rsc = res.res_idx if res.res_idx else ""
        # 画像部
        img_part = ""
        bq_style = ""
        if res.image_url:
            iu    = res.image_url
            tu    = res.thumb_url or iu
            iname = _hm.escape(res.image_name or iu.rsplit("/", 1)[-1])
            fsz   = res.file_size_bytes or 0
            tw    = getattr(res, "thumb_w", 0) or 0
            th    = getattr(res, "thumb_h", 0) or 0
            if tw > 0 and th > 0:
                _wh     = f" width={tw} height={th}"
                _istyle = ""
            else:
                # サムネ寸法が無い差分追記レスは width/height が付かず、かつ /src/(原寸)を
                # 参照するため外部ブラウザで等倍表示になる。CSSでサムネ相当(250px)に
                # 上限を掛けて縮小する（アスペクト比はブラウザが維持）。
                _wh     = ""
                _istyle = " style=\"max-width:250px;max-height:250px;width:auto;height:auto\""
            img_part = (
                f"<br> &nbsp; &nbsp; <a href=\"{iu}\" target='_blank'>{iname}</a>"
                f"-({fsz} B) <br>"
                f"<a href=\"{iu}\" target='_blank'>"
                f"<img src='{tu}' border=0 align=left{_wh}{_istyle} hspace=20 "
                f"alt=\"{fsz} B\" loading=\"lazy\"></a>"
            )
            bq_style = ' style="margin-left:286px;"'
        comment = res.comment_html or ""
        return (
            f"<table border=0><tr><td class=rts>…</td><td class=rtd>"
            f'<span id="delcheck{no}" class="rsc">{rsc}</span>'
            f'<span class="csb">無念</span>Name'
            f'<span class="cnm">{name}</span>'
            f'<span class="cnw">{cnw}</span>'
            f'<span class="cno">No.{no}</span>'
            f'<a href="javascript:void(0);" onclick="sd({no});return(false);" '
            f'class=sod id=sd{no}>+</a>'
            f"{img_part}"
            f"<blockquote{bq_style}>{comment}</blockquote>"
            f"</td></tr></table>\n"
        )

    def _strip_futaba_ads(self, html: str) -> str:
        """ふたば原本HTMLから広告・トラッキングを除去する。
        ・<script>/<iframe>/<ins>/<noscript> を全除去（広告・計測・SPタグはここに集約）
        ・既知の広告ブロック(id/class)を除去
        本文(レスのtable構造・画像リンク・フォーム)は残す。"""
        try:
            from bs4 import BeautifulSoup, Comment
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return html
        # コメントノードを除去（ふたばのコメントは広告コードやレイアウトマーカーのみで
        # 本文価値が無く、コメント内に閉じ込められた広告ブートストラップを一掃する）
        for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
            try:
                c.extract()
            except Exception:
                pass
        for tag in soup.find_all(["script", "iframe", "ins", "noscript"]):
            try:
                tag.decompose()
            except Exception:
                pass
        _AD_IDS = {"rightad", "rightadfloat", "radtop"}
        _AD_CLASSES = {"tue2", "dmps", "rad", "radabs"}
        # 親を先に除去すると子(リスト内)が無効化されるため、先に対象を収集する
        targets = []
        for el in soup.find_all(id=True):
            _id = el.get("id", "")
            if _id in _AD_IDS or _id.startswith("ads-") or (
                    len(_id) == 32 and all(c in "0123456789abcdef" for c in _id)):
                targets.append(el)
        for el in soup.find_all(class_=True):
            cls = el.get("class", [])
            if isinstance(cls, str):
                cls = cls.split()
            if any(c in _AD_CLASSES for c in cls):
                targets.append(el)
        for el in targets:
            try:
                if getattr(el, "decomposed", False) or el.attrs is None:
                    continue
                el.decompose()
            except Exception:
                pass
        return str(soup)

    def _strip_nosave_images(self, html: str, thread) -> str:
        """保存対象外レスの画像（サムネ/本画像リンク）を保存HTMLから除去する。
        対象 = 削除レス(is_deleted) ＋ NGレス（NGワード/NG画像マッチ、または
        手動NG登録 ng_hidden_res_nos = フッタNG・delして非表示で登録したレス）。
        これらの画像/サムネのファイル名(basename)で一致する <img>・<a href> を削るので、
        保存ログに対象レスの画像が残らず、後段のメディア収集(URL正規表現)にも
        引っかからずダウンロード/埋め込みもされない。レス本文はそのまま残す。"""
        if not thread:
            return html
        _ng = getattr(self._settings, "ng_filter", None)
        _hidden = set(self._settings.ng_hidden_res_nos.get(getattr(thread, "url", "") or "", []))
        def _is_nosave(r) -> bool:
            if getattr(r, "is_deleted", False):
                return True
            if getattr(r, "no", None) in _hidden:
                return True
            if _ng is not None:
                try:
                    if _ng.is_ng(r):
                        return True
                    if getattr(r, "image_url", "") and _ng.is_ng_image(r):
                        return True
                except Exception:
                    pass
            return False
        names: set[str] = set()
        for r in getattr(thread, "res_list", []) or []:
            if not _is_nosave(r):
                continue
            for u in (getattr(r, "image_url", ""), getattr(r, "thumb_url", "")):
                if u:
                    bn = u.rsplit("/", 1)[-1].split("?")[0]
                    if bn:
                        names.add(bn)
        if not names:
            return html
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return html
        def _bn(u: str) -> str:
            return (u or "").rsplit("/", 1)[-1].split("?")[0]
        # サムネ<img>（多くは <a href=本画像> に内包）→ 内包する<a>ごと削除
        for img in list(soup.find_all("img", src=True)):
            if _bn(img.get("src", "")) in names:
                a = img.find_parent("a")
                try:
                    (a or img).decompose()
                except Exception:
                    pass
        # 本画像へのテキストリンク（ファイル名リンク）→ 削除
        for a in list(soup.find_all("a", href=True)):
            if _bn(a.get("href", "")) in names:
                try:
                    a.decompose()
                except Exception:
                    pass
        return str(soup)

    def _replace_thumb_urls_with_src_raw(self, html: str) -> str:
        """方式A「サムネ保存しない」用: 原本htm内の画像レスについて、
        サムネ<img>のsrcを本画像URL(同<a>のhref)に差し替える。
        ・<a href="本画像"><img src="サムネ" width=W height=H></a> 構造を対象
        ・動画(.mp4/.webm等)のサムネは差し替えない（thumb/に保存して参照）
        ・width/heightはサムネ寸法のまま残し、本画像を縮小表示させる"""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return html
        import re as _re
        _vid = _re.compile(r'\.(mp4|webm|mov|avi|mkv)$', _re.IGNORECASE)
        _img = _re.compile(r'\.(jpg|jpeg|png|gif|webp|bmp)$', _re.IGNORECASE)
        for a in soup.find_all("a", href=True):
            img = a.find("img")
            if not img or not img.get("src"):
                continue
            href = a["href"]
            base = href.split("?")[0]
            if _vid.search(base):
                continue            # 動画サムネは保持
            if not _img.search(base):
                continue            # 画像以外のリンクは対象外
            img["src"] = href       # サムネ→本画像（寸法はそのまま）
        return str(soup)

    def _build_local_html_raw(self, html: str, thread, folder_name: str,
                              no_thumb: bool = False,
                              show_progress: bool = True):
        """方式A: 原本htm内のメディア(本画像/動画 = /src/、サムネ = /thumb/)を
        ローカルにDLし、htm内のURL参照を相対パスに書き換えた
        (html_local, media_files) を返す。NGは無視（原本どおり全て保存）。"""
        import re
        from urllib.parse import urljoin
        media_files: dict = {}
        if not thread:
            return html, media_files
        save_img = getattr(self._settings, "log_save_images", True)
        save_vid = getattr(self._settings, "log_save_videos", True)
        base = thread.board.base_url
        _vid = re.compile(r'\.(mp4|webm|mov|avi|mkv)$', re.IGNORECASE)
        _src_re   = re.compile(r'(?:href|src)=["\']([^"\']*?/src/[^"\']+)["\']', re.IGNORECASE)
        _thumb_re = re.compile(r'src=["\']([^"\']*?/thumb/[^"\']+)["\']', re.IGNORECASE)
        # rel(原本htmの記述) -> (絶対URL, ファイル名)
        src_set: dict = {}
        thumb_set: dict = {}
        for m in _src_re.finditer(html):
            rel = m.group(1)
            absu = urljoin(base, rel)
            fn = absu.rsplit('/', 1)[-1].split('?')[0]
            is_v = bool(_vid.search(fn))
            if is_v and not save_vid:
                continue
            if (not is_v) and not save_img:
                continue
            src_set[rel] = (absu, fn)
        # サムネ収集: 通常は全サムネ。no_thumb時は画像サムネは本画像に差替済みなので
        # 残るthumb参照(=動画サムネ)のみ収集される
        for m in _thumb_re.finditer(html):
            rel = m.group(1)
            absu = urljoin(base, rel)
            fn = absu.rsplit('/', 1)[-1].split('?')[0]
            thumb_set[rel] = (absu, fn)
        all_items = list(src_set.values()) + list(thumb_set.values())
        if not all_items:
            return html, media_files
        media_data = self._download_media(all_items, show_progress=show_progress)
        for rel, (absu, fn) in src_set.items():
            raw = media_data.get(absu)
            if not raw:
                continue
            relpath = f"{folder_name}/src/{fn}"
            media_files[relpath] = raw
            html = html.replace(rel, relpath)
        for rel, (absu, fn) in thumb_set.items():
            raw = media_data.get(absu)
            if not raw:
                continue
            relpath = f"{folder_name}/thumb/{fn}"
            media_files[relpath] = raw
            html = html.replace(rel, relpath)
        return html, media_files

    def _absolutize_futaba_urls(self, html: str, base_url: str) -> str:
        """ローカル化後に残るルート相対(/...)・プロトコル相対(//...)のURLを
        サーバ基準で絶対URL化する。ローカル化済みパス(folder/...)は純相対のため対象外。"""
        import re
        from urllib.parse import urlparse
        try:
            pu = urlparse(base_url)
            origin = f"{pu.scheme}://{pu.netloc}"
        except Exception:
            return html
        def _repl(m):
            attr, q, val = m.group(1), m.group(2), m.group(3)
            if val.startswith("//"):
                return f'{attr}={q}{pu.scheme}:{val}{q}'
            if val.startswith("/"):
                return f'{attr}={q}{origin}{val}{q}'
            return m.group(0)
        return re.sub(r'\b(href|src|action)=(["\'])([^"\']*)\2', _repl, html)

    def _build_log_html_rendered(self, cur, thread) -> str:
        """旧方式(アプリ描画)。原本htmが取得もキャッシュもできない場合のフォールバック。
        スレッドHTMLを生成してbody中身を返す（広告除去済み）"""
        from futaba2b_html import thread_to_html, THREAD_CSS
        import re, html as _hm
        _ul = getattr(self._settings, "uploader_links", [])
        _ng = getattr(cur, '_ng', None) or self._settings.ng_filter
        html_body, _ = thread_to_html(
            thread,
            ng_filter=_ng,
            hidden_nos=self._settings.ng_hidden_res_nos.get(thread.url, []),
            ng_settings=self._settings,
            uploaders=_ul,
            for_save=True,   # 新着セパレータをログに残さない
        )
        clean = re.sub(r'<script\b[^>]*>.*?</script>', '', html_body,
                       flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r'<ins\b[^>]*>.*?</ins>', '', clean,
                       flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r'<iframe\b[^>]*>.*?</iframe>', '', clean,
                       flags=re.DOTALL | re.IGNORECASE)
        # 新着帯（new-res クラス）を除去して保存
        clean = re.sub(r'\bnew-res\b', '', clean)
        m = re.search(r'<body[^>]*>(.*?)</body>', clean, re.DOTALL | re.IGNORECASE)
        body_content = m.group(1) if m else clean
        # スレタイを使用（ｷﾀ━スレなど <title> が板名になるケース対策）
        _title_text = thread.title or f"No.{thread.no}"
        title = _hm.escape(_title_text)
        return (
            '<!DOCTYPE html>\n<html lang="ja">\n<head>\n'
            '<meta charset="utf-8">\n'
            f'<title>{title}</title>\n'
            f'<style>\n{THREAD_CSS}\n</style>\n'
            '</head>\n<body>\n'
            f'{body_content}\n'
            '</body>\n</html>\n'
        )

    def _replace_thumb_urls_with_src(self, html: str, thread) -> str:
        """「サムネイルを保存しない」用: HTML内のサムネイルURLを本画像URLに差し替える。
        ・動画(.mp4/.webm等)のサムネは差し替えない（thumb/に保存して通常どおり参照）
        ・差し替え後の<img>にサムネ寸法 width/height を付与し本画像の等倍表示を防ぐ"""
        if not thread:
            return html
        import re as _re
        _video = _re.compile(r'\.(mp4|webm|mov|avi|mkv)$', _re.IGNORECASE)
        for res in thread.res_list:
            tu, iu = res.thumb_url, res.image_url
            if not tu or not iu or tu == iu:
                continue
            if _video.search(iu):
                continue   # 動画サムネは保持
            tw = getattr(res, "thumb_w", 0) or 0
            th = getattr(res, "thumb_h", 0) or 0
            if tw > 0 and th > 0:
                # render_res 生成の <img src="..."> にサムネ寸法を注入
                html = html.replace(
                    f'<img src="{tu}"',
                    f'<img src="{iu}" width="{tw}" height="{th}"')
            else:
                # 寸法不明（JSON diff API経由の新着レスは thumb_w/h を持たない）
                # → max-width/height制限を注入して本画像の等倍表示を防ぐ
                html = html.replace(
                    f'<img src="{tu}"',
                    f'<img src="{iu}" style="max-width:250px;max-height:250px;'
                    f'width:auto;height:auto"')
            html = html.replace(tu, iu)   # 残りの出現も差し替え（安全網）
        return html

    def _collect_media_urls(self, thread,
                            images: bool = True,
                            videos: bool = True,
                            thumbnails: bool = True,
                            src_only: bool = False,
                            video_thumbs_only: bool = False) -> list[tuple[str,str]]:
        """(url, filename) のリストを返す。NGワード・NG画像・手動非表示のレスは除外。
        thumbnails=True かつ src_only=False → サムネイルのみ収集
        thumbnails=False かつ src_only=True  → 本画像・動画のみ収集
        thumbnails=True  かつ src_only=False → 両方（デフォルト動作）
        """
        import re
        _video_ext = re.compile(r'\.(mp4|webm|mov|avi|mkv)$', re.IGNORECASE)
        # NG除外対象: 手動非表示Noセット + NGワード/NG画像マッチレス
        _ng = self._settings.ng_filter
        _hidden = set()
        if thread and thread.url:
            _hidden = set(self._settings.ng_hidden_res_nos.get(thread.url, []))
        seen, result = set(), []
        for res in thread.res_list:
            # NGワード・手動非表示・NG画像のレスは画像もサムネイルも保存しない
            if res.no in _hidden:
                continue
            if _ng.is_ng(res):
                continue
            if res.image_url and _ng.is_ng_image(res):
                continue
            # サムネイル収集（src_only=Falseの時のみ）
            if thumbnails and not src_only and res.thumb_url and res.thumb_url not in seen:
                _is_vid_src = bool(res.image_url and
                                   _video_ext.search(res.image_url.rsplit('/', 1)[-1]))
                if (not video_thumbs_only) or _is_vid_src:
                    seen.add(res.thumb_url)
                    tfname = res.thumb_url.rsplit('/', 1)[-1]
                    result.append((res.thumb_url, tfname))
            # 本画像・動画収集（thumbnails=Falseまたはsrc_only=Trueの時のみ）
            if (not thumbnails or src_only):
                if not res.image_url or res.image_url in seen:
                    continue
                seen.add(res.image_url)
                fname = res.image_url.rsplit('/', 1)[-1]
                is_video = bool(_video_ext.search(fname))
                if is_video and not videos:
                    continue
                if not is_video and not images:
                    continue
                result.append((res.image_url, fname))
        return result

    def _collect_uploader_urls(self, html: str) -> list[tuple[str, str]]:
        """保存HTML内のうpろだ画像・動画URLを収集して返す（ul-thumb画像, ul-link動画）。
        ng-hidden クラスを持つレス要素内のURLは除外する。
        戻り値: [(url, filename), ...]
        """
        from bs4 import BeautifulSoup as _BS
        import re
        # ng-hidden レス内のURLを除外するため BeautifulSoup で解析
        soup = _BS(html, "html.parser")
        # ng-hidden クラスを持つ要素内の全URLをブラックリスト化
        ng_urls: set[str] = set()
        for el in soup.find_all(class_="ng-hidden"):
            for img in el.find_all("img", src=True):
                ng_urls.add(img["src"])
            for a in el.find_all("a", href=True):
                ng_urls.add(a["href"])
        seen, result = set(), []
        # <img class="ul-thumb"> を収集
        pattern = re.compile(
            r'<img\b[^>]*\bclass=["\'][^"\']*ul-thumb[^"\']*["\'][^>]*\bsrc=["\']([^"\']+)["\']'
            r'|<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*\bclass=["\'][^"\']*ul-thumb[^"\']*["\']',
            re.IGNORECASE)
        for m in pattern.finditer(html):
            url = m.group(1) or m.group(2)
            if not url or url in seen or url in ng_urls:
                continue
            if re.search(r'/[a-z]+/(src|thumb)/', url) and '/up/' not in url and '/up2/' not in url:
                continue
            seen.add(url)
            fname = url.rsplit('/', 1)[-1].split('?')[0] or 'uploader_img'
            result.append((url, fname))
        # <a class="ul-link"> を収集
        link_pat = re.compile(
            r'<a\b[^>]*\bclass=["\'][^"\']*ul-link[^"\']*["\'][^>]*\bhref=["\']([^"\']+)["\']'
            r'|<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\bclass=["\'][^"\']*ul-link[^"\']*["\']',
            re.IGNORECASE)
        for m in link_pat.finditer(html):
            url = m.group(1) or m.group(2)
            if not url or url in seen or url in ng_urls or url.startswith('#'):
                continue
            if re.search(r'/[a-z]+/(src|thumb)/', url) and '/up/' not in url and '/up2/' not in url:
                continue
            seen.add(url)
            fname = url.rsplit('/', 1)[-1].split('?')[0] or 'uploader_file'
            result.append((url, fname))
        return result

    def _download_media(self, urls: list[tuple[str,str]],
                        parent=None,
                        show_progress: bool = True) -> dict[str,bytes]:
        """(url, filename) リストを並列ダウンロード。{url: bytes} を返す。
        show_progress=False の場合はダイアログを作らない（バックグラウンドスレッドから安全に呼べる）。
        fetcher の메모리/ディスクキャッシュを活用し、ThreadPoolExecutor で並列取得。"""
        import concurrent.futures
        from threading import Lock

        if not urls:
            return {}

        data: dict[str, bytes] = {}
        lock = Lock()
        completed = [0]
        canceled  = [False]

        # プログレスダイアログ（メインスレッドからの呼び出し時のみ）
        dlg = None
        if show_progress:
            from PySide6.QtWidgets import QProgressDialog
            from PySide6.QtCore import Qt
            dlg = QProgressDialog("画像・動画をダウンロード中...", "キャンセル",
                                   0, len(urls), parent or self)
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumDuration(300)
            dlg.setValue(0)

        def _fetch_one(item):
            url, fname = item
            if canceled[0]:
                return
            raw = None
            try:
                # fetch_image_bytes はメモリ→ディスク→HTTPの順でキャッシュ活用
                # スレ落ち直後の一時的404を救済するため retry_404=True
                raw = self._fetcher.fetch_image_bytes(url, retry_404=True)
            except Exception:
                pass
            with lock:
                if raw:
                    data[url] = raw
                completed[0] += 1
                if dlg is not None:
                    # プログレスはメインスレッドから更新（BGスレッドからのQTimerは禁止）
                    self._main_thread_call.emit(lambda n=completed[0], f=fname:
                        (dlg.setValue(n), dlg.setLabelText(f)))

        workers = min(getattr(self._settings, 'download_workers', 4), len(urls))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_fetch_one, item) for item in urls]
            import time as _time
            while not all(f.done() for f in futures):
                if dlg is not None:
                    QApplication.processEvents()
                    if dlg.wasCanceled():
                        canceled[0] = True
                        break
                _time.sleep(0.05)
            concurrent.futures.wait(futures, timeout=30)

        if dlg is not None:
            dlg.setValue(len(urls))
        return data

    # ── HTML 保存 ────────────────────────────────────────────────────────────
    def _save_log(self, fmt: str = "html"):
        """fmt: 'html' | 'mht' | 'zip'"""
        cur, thread = self._get_thread_for_log()
        if not thread: return

        ext_map  = {"html": "html", "mht": "mht", "zip": "zip"}
        flt_map  = {
            "html": "HTML ファイル (*.html *.htm);;すべてのファイル (*)",
            "mht":  "MHT ファイル (*.mht *.mhtml);;すべてのファイル (*)",
            "zip":  "ZIP ファイル (*.zip);;すべてのファイル (*)",
        }
        ext = ext_map[fmt]
        default_path = self._log_full_path(thread, ext)

        path, _ = QFileDialog.getSaveFileName(
            self, "ログを保存", default_path, flt_map[fmt])
        if not path: return

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_html = self._build_log_html(cur, thread)
            if fmt == "html":
                self._do_save_html(path, save_html, thread)
            elif fmt == "mht":
                self._do_save_mht(path, save_html, thread)
            else:
                self._do_save_zip(path, save_html, thread)
            self.statusBar().showMessage(f"保存しました: {path}", 5000)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "保存エラー", str(e))

    # ── スクリーンショット保存 ────────────────────────────────────────────
    def _save_log_screenshot(self):
        """スレ全体をスクリーンショット(PNG)として保存。
        長いスレ（1000レス等）は1ファイル最大高さで自動分割保存する。"""
        cur, thread = self._get_thread_for_log()
        if not thread: return
        default_path = self._log_full_path(thread, "png")
        path, _ = QFileDialog.getSaveFileName(
            self, "スクリーンショットで保存", default_path,
            "PNG 画像 (*.png);;すべてのファイル (*)")
        if not path: return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        self._capture_thread_screenshot(cur, path)

    def _capture_thread_screenshot(self, tview, path):
        """WebEngineViewをスクロールしながら分割キャプチャしてPNG保存する。
        非同期ステートマシン（メインスレッドのみ・QTimer駆動）。
        1ファイルの最大高さは SEG_MAX デバイスpx（QImage/PNG上限32767の安全圏）。
        超える場合は path_001.png, path_002.png ... に分割する。"""
        import math
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QProgressDialog, QMessageBox

        web  = tview._view
        page = web.page()
        SEG_MAX    = 28000   # 1セグメント最大高さ（デバイスpx）
        SCROLL_WAIT = 300    # スクロール後の描画待ち(ms)

        st = {
            "total": 0, "vh": 0, "k": 1.0, "total_dev": 0, "nfiles": 1,
            "prev_end": 0,            # キャプチャ済み末尾(JS px)
            "seg_img": None, "seg_painter": None,
            "seg_dev_y": 0, "seg_idx": 0, "files": [],
            "orig_scroll": 0, "cancelled": False, "first": True,
        }

        dlg = QProgressDialog("スクリーンショットを撮影中...", "キャンセル", 0, 100, self)
        dlg.setWindowTitle("スクリーンショットで保存")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(lambda: st.__setitem__("cancelled", True))

        def _seg_path(idx: int) -> str:
            if st["nfiles"] <= 1:
                return path
            base, ext = os.path.splitext(path)
            return f"{base}_{idx+1:03d}{ext or '.png'}"

        def _open_segment():
            # 残り全体高さ = total_dev - 保存済み高さ
            saved_dev = sum_saved()
            h = min(SEG_MAX, max(1, st["total_dev"] - saved_dev))
            img = QImage(web.grab().width(), h, QImage.Format.Format_RGB32)
            img.fill(0xFF222222)
            st["seg_img"] = img
            st["seg_painter"] = QPainter(img)
            st["seg_dev_y"] = 0

        def sum_saved() -> int:
            return st["seg_idx"] * SEG_MAX

        def _flush_segment():
            try:
                if st["seg_painter"]:
                    st["seg_painter"].end()
                    st["seg_painter"] = None
            except Exception:
                pass
            if st["seg_img"] is not None:
                fp = _seg_path(st["seg_idx"])
                # 実際に書き込んだ高さまで切り出して保存
                out = st["seg_img"] if st["seg_dev_y"] >= st["seg_img"].height() \
                      else st["seg_img"].copy(0, 0, st["seg_img"].width(), max(1, st["seg_dev_y"]))
                out.save(fp, "PNG")
                st["files"].append(fp)
                st["seg_img"] = None
                st["seg_idx"] += 1

        def _finish(ok: bool, msg: str = ""):
            try:
                if st["seg_painter"]:
                    st["seg_painter"].end(); st["seg_painter"] = None
            except Exception:
                pass
            if ok and st["seg_img"] is not None and st["seg_dev_y"] > 0:
                _flush_segment()
            # スクロールバー復元・元位置に戻す
            page.runJavaScript(
                "var e=document.getElementById('_sscap');if(e)e.remove();"
                f"window.scrollTo(0,{st['orig_scroll']});")
            dlg.close()
            if ok and st["files"]:
                n = len(st["files"])
                self.statusBar().showMessage(
                    f"保存しました: {st['files'][0]}"
                    + (f" ほか {n-1} ファイル" if n > 1 else ""), 8000)
            elif not ok and msg:
                QMessageBox.warning(self, "スクリーンショット保存", msg)

        def _grab_chunk(y_req: int):
            if st["cancelled"]:
                _finish(False); return
            if not web.isVisible():
                _finish(False, "キャプチャ中にタブが非表示になりました。\n"
                               "スレッドタブを表示したまま実行してください。")
                return
            pm = web.grab()
            if pm.isNull() or pm.height() == 0:
                _finish(False, "画面のキャプチャに失敗しました。"); return

            if st["first"]:
                st["first"] = False
                st["k"] = pm.height() / max(1, st["vh"])
                st["total_dev"] = int(math.ceil(st["total"] * st["k"]))
                st["nfiles"] = max(1, math.ceil(st["total_dev"] / SEG_MAX))
                _open_segment()

            # ブラウザは total-vh までしかスクロールできない → 実位置をクランプ
            y_act = min(y_req, max(0, st["total"] - st["vh"]))
            overlap_js = max(0, st["prev_end"] - y_act)
            crop_top = int(round(overlap_js * st["k"]))
            chunk_h  = pm.height() - crop_top
            if chunk_h > 0:
                src_y = crop_top
                while chunk_h > 0:
                    if st["seg_img"] is None:
                        _open_segment()
                    space = st["seg_img"].height() - st["seg_dev_y"]
                    draw_h = min(space, chunk_h)
                    st["seg_painter"].drawPixmap(
                        0, st["seg_dev_y"], pm,
                        0, src_y, pm.width(), draw_h)
                    st["seg_dev_y"] += draw_h
                    src_y   += draw_h
                    chunk_h -= draw_h
                    if st["seg_dev_y"] >= st["seg_img"].height():
                        _flush_segment()

            st["prev_end"] = y_act + st["vh"]
            dlg.setValue(min(99, int(st["prev_end"] / max(1, st["total"]) * 100)))

            if st["prev_end"] >= st["total"]:
                _finish(True)
            else:
                _scroll_to(st["prev_end"])

        def _scroll_to(y: int):
            if st["cancelled"]:
                _finish(False); return
            page.runJavaScript(f"window.scrollTo(0,{y});")
            QTimer.singleShot(SCROLL_WAIT, lambda: _grab_chunk(y))

        def _on_metrics(res):
            try:
                total, vh, sy = int(res[0]), int(res[1]), int(res[2])
            except Exception:
                _finish(False, "ページ情報の取得に失敗しました。"); return
            if total <= 0 or vh <= 0:
                _finish(False, "ページ情報の取得に失敗しました。"); return
            st["total"] = total; st["vh"] = vh; st["orig_scroll"] = sy
            _scroll_to(0)

        # スクロールバーを隠してページ寸法を取得
        page.runJavaScript(
            "(function(){"
            "var s=document.createElement('style');s.id='_sscap';"
            "s.textContent='::-webkit-scrollbar{display:none!important}';"
            "document.head.appendChild(s);"
            "return [Math.max(document.body.scrollHeight,"
            "document.documentElement.scrollHeight),"
            "window.innerHeight, window.scrollY];})()",
            _on_metrics)

    def _save_html_view(self, view, thread):
        """ThreadViewから呼ばれるHTML保存"""
        save_html = self._build_log_html(view, thread)
        path, _ = QFileDialog.getSaveFileName(
            self, "HTMLとして保存",
            self._log_full_path(thread, "html"),
            "HTML (*.html *.htm)")
        if not path: return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._do_save_html(path, save_html, thread)
            self.statusBar().showMessage(f"保存しました: {path}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "保存エラー", str(e))

    def _save_zip_view(self, view, thread):
        """ThreadViewから呼ばれるZIP保存"""
        save_html = self._build_log_html(view, thread)
        path, _ = QFileDialog.getSaveFileName(
            self, "ZIPとして保存",
            self._log_full_path(thread, "zip"),
            "ZIP (*.zip)")
        if not path: return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._do_save_zip(path, save_html, thread)
            self.statusBar().showMessage(f"保存しました: {path}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "保存エラー", str(e))

    def _save_mht_view(self, view, thread):
        """ThreadViewから呼ばれるMHT保存（_save_html_view/_save_zip_viewと同パターン）"""
        save_html = self._build_log_html(view, thread)
        path, _ = QFileDialog.getSaveFileName(
            self, "MHTとして保存",
            self._log_full_path(thread, "mht"),
            "MHT (*.mht *.mhtml)")
        if not path: return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._do_save_mht(path, save_html, thread)
            self.statusBar().showMessage(f"保存しました: {path}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "保存エラー", str(e))

    def _build_local_html(self, html: str, thread, folder_name: str,
                           show_progress: bool = True):
        """HTML・ZIP共通: メディアをローカルに保存し、URL参照を相対パスに書き換えた
        (html_local, media_files) を返す。"""
        import re
        save_img = getattr(self._settings, "log_save_images", True)
        save_vid = getattr(self._settings, "log_save_videos", True)
        save_ul  = getattr(self._settings, "log_save_uploader", True)
        no_thumb = getattr(self._settings, "log_save_no_thumb", False)
        media_files: dict = {}
        if not thread:
            return html, media_files
        if no_thumb:
            # 画像サムネは保存せずHTML内のURLを本画像URLに差し替える
            # （以降の src_urls 置換でローカル src/ パスに書き換わる）
            # 動画サムネのみ従来どおり thumb/ に保存してローカル参照
            html = self._replace_thumb_urls_with_src(html, thread)
            thumb_urls = self._collect_media_urls(thread, images=True, videos=False,
                                                  thumbnails=True, src_only=False,
                                                  video_thumbs_only=True)
        else:
            thumb_urls = self._collect_media_urls(thread, images=True, videos=False,
                                                  thumbnails=True, src_only=False)
        src_urls   = self._collect_media_urls(thread, images=save_img, videos=save_vid,
                                              thumbnails=False, src_only=True)
        ul_urls    = self._collect_uploader_urls(html) if save_ul else []
        all_urls   = thumb_urls + src_urls + ul_urls
        if not all_urls:
            return html, media_files
        media_data = self._download_media(all_urls, show_progress=show_progress)
        for url, fname in thumb_urls:
            raw = media_data.get(url)
            if not raw: continue
            rel = f"{folder_name}/thumb/{fname}"
            media_files[rel] = raw
            esc = re.escape(url)
            html = re.sub(f'(src|href)="{esc}"',
                          lambda m, r=rel: f'{m.group(1)}="{r}"', html)
        for url, fname in src_urls:
            raw = media_data.get(url)
            if not raw: continue
            rel = f"{folder_name}/src/{fname}"
            media_files[rel] = raw
            esc = re.escape(url)
            html = re.sub(f'(src|href)="{esc}"',
                          lambda m, r=rel: f'{m.group(1)}="{r}"', html)
            html = html.replace(f"openImg('{url}'", f"openImg('{rel}'")
            html = html.replace(f"openImgBg('{url}'", f"openImgBg('{rel}'")
        for url, fname in ul_urls:
            raw = media_data.get(url)
            if not raw: continue
            rel = f"{folder_name}/uploader/{fname}"
            media_files[rel] = raw
            esc = re.escape(url)
            html = re.sub(f'(src|href)="{esc}"',
                          lambda m, r=rel: f'{m.group(1)}="{r}"', html)
            html = html.replace(f"openImg('{url}'", f"openImg('{rel}'")
            html = html.replace(f"openImgBg('{url}'", f"openImgBg('{rel}'")
        return html, media_files

    def _do_save_html(self, path: str, html: str, thread=None, show_progress: bool = True):
        """HTML保存(方式A)。原本htmのサムネ→folder/thumb/、本画像・動画→folder/src/。
        「サムネ保存しない」時はサムネを本画像に差し替えてから保存する。"""
        no_thumb = getattr(self._settings, "log_save_no_thumb", False)
        if no_thumb:
            html = self._replace_thumb_urls_with_src_raw(html)
        folder_name = os.path.splitext(os.path.basename(path))[0]
        html_local, media_files = self._build_local_html_raw(
            html, thread, folder_name, no_thumb=no_thumb, show_progress=show_progress)
        if thread:
            html_local = self._absolutize_futaba_urls(html_local, thread.board.base_url)
        for rel, raw in media_files.items():
            dest = os.path.join(os.path.dirname(path), rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(raw)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html_local)

    def _inject_log_fallback_js(self, html: str) -> str:
        """ログ保存用HTML: 外部ブラウザで画像を新タブで開くフォールバックJSを注入する。"""
        import re as _re_fb
        fallback = (
            "<script>\n"
            "// 外部ブラウザ用フォールバック: _b ブリッジが無い場合に画像を新タブで開く\n"
            "if (typeof _b === 'undefined') {\n"
            "  function openImg(url, idx)   { window.open(url, '_blank'); }\n"
            "  function openImgBg(url, idx) { window.open(url, '_blank'); }\n"
            "}\n"
            "</script>\n"
        )
        if _re_fb.search(r'</head>', html, _re_fb.IGNORECASE):
            return _re_fb.sub(r'(</head>)', fallback + r'\1', html, count=1, flags=_re_fb.IGNORECASE)
        if _re_fb.search(r'<body', html, _re_fb.IGNORECASE):
            return _re_fb.sub(r'(<body)', fallback + r'\1', html, count=1, flags=_re_fb.IGNORECASE)
        return fallback + html

    def _do_save_mht(self, path: str, html: str, thread, show_progress: bool = True):
        """MHTML (multipart/related) として保存(方式A)。
        原本htmのメディアURLを絶対化し、絶対URLでメディアを取得して埋め込む。"""
        import base64, mimetypes, uuid, re as _re_mht
        from urllib.parse import urljoin
        _no_thumb = getattr(self._settings, "log_save_no_thumb", False)
        if _no_thumb:
            html = self._replace_thumb_urls_with_src_raw(html)
        if thread:
            html = self._absolutize_futaba_urls(html, thread.board.base_url)
        boundary = f"----=_2B_{uuid.uuid4().hex}"
        html_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
        html_b64_wrapped = "\r\n".join(html_b64[i:i+76] for i in range(0, len(html_b64), 76))
        lines = [
            "MIME-Version: 1.0",
            f'Content-Type: multipart/related; type="text/html"; boundary="{boundary}"',
            "",
            f"--{boundary}",
            'Content-Type: text/html; charset="utf-8"',
            "Content-Transfer-Encoding: base64",
            f"Content-Location: {thread.url if thread else 'index.htm'}",
            "",
            html_b64_wrapped,
        ]
        # 絶対URL化済みhtmから埋め込み対象メディア(絶対URL)を収集
        save_img = getattr(self._settings, "log_save_images", True)
        save_vid = getattr(self._settings, "log_save_videos", True)
        _vid = _re_mht.compile(r'\.(mp4|webm|mov|avi|mkv)$', _re_mht.IGNORECASE)
        _src_re   = _re_mht.compile(r'(?:href|src)=["\'](https?://[^"\']*?/src/[^"\']+)["\']', _re_mht.IGNORECASE)
        _thumb_re = _re_mht.compile(r'src=["\'](https?://[^"\']*?/thumb/[^"\']+)["\']', _re_mht.IGNORECASE)
        seen, media_urls = set(), []
        for m in _src_re.finditer(html):
            u = m.group(1)
            if u in seen: continue
            fn = u.rsplit('/', 1)[-1].split('?')[0]
            is_v = bool(_vid.search(fn))
            if is_v and not save_vid: continue
            if (not is_v) and not save_img: continue
            seen.add(u); media_urls.append((u, fn))
        for m in _thumb_re.finditer(html):
            u = m.group(1)
            if u in seen: continue
            seen.add(u); media_urls.append((u, u.rsplit('/', 1)[-1].split('?')[0]))
        if media_urls:
            media_data = self._download_media(media_urls, show_progress=show_progress)
            for url, fname in media_urls:
                raw = media_data.get(url)
                if not raw: continue
                mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
                b64  = base64.b64encode(raw).decode('ascii')
                b64_wrapped = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
                lines += [f"--{boundary}", f"Content-Type: {mime}",
                           "Content-Transfer-Encoding: base64",
                           f"Content-Location: {url}", "", b64_wrapped]
        lines += [f"--{boundary}--", ""]
        with open(path, 'wb') as f:
            f.write("\r\n".join(lines).encode('ascii'))

    def _do_save_zip(self, path: str, html: str, thread, show_progress: bool = True):
        """ZIP保存(方式A)。index.htm + thumb/ src/ を格納。"""
        import zipfile
        no_thumb = getattr(self._settings, "log_save_no_thumb", False)
        if no_thumb:
            html = self._replace_thumb_urls_with_src_raw(html)
        folder_name = os.path.splitext(os.path.basename(path))[0]
        html_local, media_files = self._build_local_html_raw(
            html, thread, folder_name, no_thumb=no_thumb, show_progress=show_progress)
        if thread:
            html_local = self._absolutize_futaba_urls(html_local, thread.board.base_url)
        # zip内はindex.htm基準の相対参照にするため folder_name/ プレフィックスを除去
        html_local = html_local.replace(f"{folder_name}/", "")
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.htm", html_local.encode('utf-8'))
            for rel, raw in media_files.items():
                arc = rel[len(folder_name) + 1:] if rel.startswith(folder_name + "/") else rel
                zf.writestr(arc, raw)
    # ── ログを開く ───────────────────────────────────────────────────────────
    def _open_log_file(self, path: str = ""):
        """MHT / ZIP / HTML ログファイルを開く。
        path を省略するとファイル選択ダイアログを開く（メニュー用）。
        path を指定するとそのファイルを直接開く（D&D用）。"""
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "ログを開く", self._log_save_dir(),
                "ログファイル (*.mht *.mhtml *.zip *.html *.htm);;すべてのファイル (*)")
        if not path: return
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in ('.mht', '.mhtml'):
                self._open_log_mht(path)
            elif ext == '.zip':
                self._open_log_zip(path)
            else:
                self._open_log_html(path)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "読み込みエラー", str(e))

    def _recover_log_thread_info(self, html: str):
        """保存ログHTMLから (board, no, thread_url) を復元する。
        <link rel="canonical" href=".../res/NNNN.htm"> を優先。失敗時 None。"""
        import re
        m = re.search(r'rel=["\']canonical["\']\s+href=["\']'
                      r'(https?://[^/]+/[^/"\']+/)res/(\d+)\.htm', html, re.IGNORECASE)
        if not m:
            m = re.search(r'(https?://[^/"\']+/[^/"\']+/)res/(\d+)\.htm', html)
        if not m:
            return None
        base = m.group(1); no = int(m.group(2))
        thread_url = base + f"res/{no}.htm"
        board = self._find_board_by_url(thread_url)
        if not board:
            tm = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            bname = ""
            if tm:
                parts = tm.group(1).rsplit(" - ", 1)
                bname = parts[-1].strip() if len(parts) > 1 else ""
            board = BoardInfo(name=bname, url=base + "futaba.htm")
        return board, no, thread_url

    def _open_thread_log(self, html: str, media_base_url: str, tab_label: str,
                         media_map: dict = None):
        """保存ログを通常スレと同じ ThreadView で開く（オフライン表示）。
        投稿/自動更新/そうだね送信/スレ落ち処理は配線せず無効化する。"""
        info = self._recover_log_thread_info(html)
        if not info:
            raise ValueError("ログHTMLからスレッド情報を復元できませんでした")
        board, no, thread_url = info
        inner = self._get_or_create_board_tab(board)
        if not inner:
            self.statusBar().showMessage("板タブを開いてからログを開いてください", 3000)
            return
        view = ThreadView(self._fetcher, self._settings, inner)
        # 表示・ローカル操作系のみ配線（投稿/AR/スレ落ち/NG即閉じは配線しない）
        view.open_image_tab.connect(self._open_image_tab)
        view.open_image_tab_bg.connect(self._open_image_tab_bg)
        view.open_thread_url_requested.connect(self._open_thread_url)
        view.status_info.connect(self._on_thread_status)

        def _on_img_list_updated(img_list, _inner=inner, _src_view=view):
            for i in range(_inner.count()):
                w = _inner.widget(i)
                if isinstance(w, ImageTabView) and w._src_thread_view is _src_view:
                    w.update_img_list(img_list)
        view.img_list_updated.connect(_on_img_list_updated)

        short = (tab_label or f"No.{no}")[:20]
        idx = inner.addTab(view, f"📄 {short}")
        inner.setCurrentIndex(idx)
        self._refresh_tab_pane()
        view.load_log_thread(board, no, html, media_base_url, thread_url, media_map)
        # 保存ログのタブ名はファイル名のままになるため、読み込み後に
        # 0レスめ(OP)のスレ名へ更新する（通常スレの _pin_safe_set 相当）。
        _th = getattr(view, '_thread', None)
        if _th is not None:
            _op = self._op_thread_name(_th)
            if _op:
                _pin_safe_set(inner, view, f"📄 {_op[:20]}")

    @staticmethod
    def _is_futaba_log(html: str) -> bool:
        """保存ログがふたば構造(方式A)か判定。Falseなら旧アプリ描画(方式B)ログ。"""
        return ('class="thre"' in html or "class='thre'" in html
                or 'class="rtd"' in html or "class=rtd" in html)

    def _open_log_html(self, path: str):
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
        from PySide6.QtCore import QUrl
        if not self._is_futaba_log(html):
            # 方式B(アプリ描画)ログ → 従来の静的表示
            self._open_log_view(QUrl.fromLocalFile(path), os.path.basename(path))
            return
        base = QUrl.fromLocalFile(os.path.dirname(path) + os.sep).toString()
        self._open_thread_log(
            html, base, os.path.splitext(os.path.basename(path))[0])

    def _open_log_mht(self, path: str):
        """MHTをemailで解析し、メディアをdata:URL化したHTMLとして通常スレ表示する。"""
        import email as _email, base64, re
        with open(path, 'rb') as f:
            raw = f.read()
        try:
            msg = _email.message_from_bytes(raw)
        except Exception as e:
            raise ValueError(f"MHTの解析に失敗しました: {e}")
        html_part = None; html_charset = 'utf-8'
        media: dict[str, str] = {}   # Content-Location → data:URL
        for part in msg.walk():
            ct = part.get_content_type()
            cl = part.get('Content-Location', '') or ''
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if ct == 'text/html' and html_part is None:
                html_charset = part.get_content_charset('utf-8') or 'utf-8'
                html_part = payload.decode(html_charset, errors='replace')
            elif cl:
                b64 = base64.b64encode(payload).decode('ascii')
                media[cl] = f"data:{ct};base64,{b64}"
        if not html_part:
            raise ValueError("MHTファイルから HTML を抽出できませんでした")
        if not self._is_futaba_log(html_part):
            # 方式B(非ふたば)ログ: URLをdata:に置換した上で静的表示する
            html_replaced = html_part
            for orig_url, data_url in media.items():
                esc = re.escape(orig_url)
                html_replaced = re.sub(
                    f'(src|href)="{esc}"',
                    lambda m, d=data_url: f'{m.group(1)}="{d}"', html_replaced)
            import tempfile
            from PySide6.QtCore import QUrl
            with tempfile.NamedTemporaryFile(
                    'w', suffix='.html', encoding='utf-8', delete=False) as tf:
                tf.write(html_replaced); tmp_path = tf.name
            self._open_log_view(QUrl.fromLocalFile(tmp_path), os.path.basename(path))
            return
        # ふたば構造ログ: 元(絶対)URLのままパースさせて拡張子/サイズを取得し、
        # media(元URL→data:) を media_map として渡して表示・再生時に data: を使う。
        self._open_thread_log(
            html_part, "", os.path.splitext(os.path.basename(path))[0],
            media_map=media)

    def _open_log_zip(self, path: str):
        """ZIP から index.htm を取り出し一時ディレクトリに展開して通常スレ表示する"""
        import zipfile, tempfile
        tmp = tempfile.mkdtemp(prefix="2b_log_")
        with zipfile.ZipFile(path, 'r') as zf:
            zf.extractall(tmp)
        index = os.path.join(tmp, "index.htm")
        if not os.path.exists(index):
            for name in os.listdir(tmp):
                if name.endswith(('.htm', '.html')):
                    index = os.path.join(tmp, name)
                    break
        with open(index, encoding="utf-8", errors="replace") as f:
            html = f.read()
        from PySide6.QtCore import QUrl
        if not self._is_futaba_log(html):
            self._open_log_view(QUrl.fromLocalFile(index), os.path.basename(path))
            return
        base = QUrl.fromLocalFile(tmp + os.sep).toString()
        self._open_thread_log(
            html, base, os.path.splitext(os.path.basename(path))[0])

    def _open_log_view(self, url, tab_label: str):
        """ログHTMLをシンプルな WebEngineView タブで表示"""
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
        inner = self._active_inner()
        if not inner:
            self.statusBar().showMessage("板タブを開いてからログを開いてください", 3000)
            return
        profile = QWebEngineProfile(self)  # off-the-record: ディスクキャッシュなし
        profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineDownloadRequest)
        from PySide6.QtGui import QDesktopServices
        def _on_download(req: QWebEngineDownloadRequest):
            req.cancel()
            QDesktopServices.openUrl(req.url())
        profile.downloadRequested.connect(_on_download)

        page = QWebEnginePage(profile, profile)

        def _create_window(wtype):
            dummy_page = QWebEnginePage(profile, profile)
            def _nav(req):
                QDesktopServices.openUrl(req.url())
                dummy_page.deleteLater()
            dummy_page.navigationRequested.connect(_nav)
            return dummy_page
        page.createWindow = _create_window

        view = QWebEngineView(page, self)
        view.load(url)
        # まずファイル名でタブを追加
        short = tab_label[:20]
        i = inner.addTab(view, f"📄 {short}")
        inner.setCurrentIndex(i)

        # ページロード後にOPコメントを取得してタブ名を「(ログ)スレ文」に更新
        _inner_ref = inner   # クロージャキャプチャ
        _tab_idx   = i
        def _update_title():
            js = (
                "(function(){"
                # .comment を最優先（csb は「無念」等の感情欄なので使わない）
                "  var com = document.querySelector('.res.op .comment');"
                "  if(com){"
                "    var t = com.textContent.trim().replace(/\\s+/g,' ');"
                "    if(t) return t.slice(0,40);"
                "  }"
                "  return document.title || '';"
                "})()"
            )
            page.runJavaScript(
                js,
                lambda title: _inner_ref.setTabText(
                    _tab_idx,
                    f"📄 (ログ){title[:40]}" if title else f"📄 (ログ){short}"
                )
            )
        view.loadFinished.connect(lambda _ok: _update_title())

    # ── URL バー ──────────────────────────────────────────────────────────────

    def _on_url_enter(self):
        url = self._url_bar.text().strip()
        if not url: return
        # http → https に統一
        if url.startswith("http://"):
            url = "https://" + url[7:]
            self._url_bar.setText(url)
        if "futaba.htm" in url:
            parts = urllib.parse.urlparse(url)
            name  = parts.path.split("/")[-2]
            self._on_board_selected(BoardInfo(name=name, url=url))
        elif "/res/" in url:
            m = re.search(r"res/(\d+)\.htm", url)
            if m:
                no   = int(m.group(1))
                base = url.rsplit("/res/", 1)[0].rstrip("/") + "/futaba.htm"
                self._open_thread(BoardInfo(name="", url=base), no)
        elif "mode=cat" in url:
            base = url.rsplit("futaba.php", 1)[0]
            self._on_board_selected(BoardInfo(name="", url=base + "futaba.htm"))

    # ── 設定・表示切替 ─────────────────────────────────────────────────────────

    def _toggle_tree(self):
        if self._tree_pane.isVisible(): self._tree_pane.hide()
        else: self._tree_pane.show()

    # ── タイトルバー更新 ────────────────────────────────────────────────────────

    @staticmethod
    def _board_label_for_title(board) -> str:
        """板名。二次元裏のみ may/img/cgi/dat 等のサブドメインを付加する。
        例: 「二次元裏 (may)」「二次元裏 (img)」「二次元裏 (cgi)」それ以外は「板名」のみ"""
        name = board.name if board else ""
        if name == "二次元裏" and board:
            try:
                from urllib.parse import urlparse as _up
                sub = _up(board.url).hostname.split(".")[0]  # 例: "may", "img", "cgi", "dat"
                return f"{name} ({sub})"
            except Exception:
                pass
        return name

    def _refresh_title_bar(self, info: dict | None = None):
        """ウィンドウタイトルを更新する。
        info が None（タブ切替等）のとき: アクティブビューから直接取得。
        info が dict（status_info シグナル）のとき: シグナル内の情報を使用。

        書式: 板名 (サブドメイン)　スレタイトル (+新着) 　消滅予定 - 2BP v{VER}
        """
        from futaba2b_app_qt import APP_VER
        base_title = f"2BP v{APP_VER}"

        if info is not None:
            # status_info シグナル経由
            board     = info.get('board')
            title     = info.get('title', '')
            new_count = info.get('new_count', 0)
            die_time  = info.get('die_time', '')
        else:
            # タブ切替等の直接呼び出し
            board, title, new_count, die_time = None, '', 0, ''
            inner = self._active_inner()
            if inner:
                w = inner.currentWidget()
                if isinstance(w, ThreadView) and getattr(w, '_thread', None):
                    th = w._thread
                    board     = th.board
                    title     = th.title or f"No.{th.no}"
                    die_time  = th.die_time or th.expiry or ''
                elif isinstance(w, CatalogView) and inner._board:
                    board = inner._board

        parts = []
        if board:
            parts.append(self._board_label_for_title(board))
        if title:
            new_str = f" (+{new_count})" if new_count > 0 else ''
            parts.append(f"{title}{new_str}")
        if die_time:
            parts.append(die_time)

        if parts:
            self.setWindowTitle("　".join(parts) + f" - {base_title}")
        else:
            self.setWindowTitle(base_title)

    def _on_thread_status(self, info: dict):
        """ThreadView.status_info シグナルを受けてステータスバーを更新"""
        sender_view = info.get('view')
        inner  = self._active_inner()
        active = inner.currentWidget() if inner else None
        match  = active is sender_view
        if sender_view is not None and not match:
            return

        self._st_viewers.setText(info.get('viewers',  ''))
        self._st_expiry.setText( info.get('expiry',   ''))
        self._st_saved.setText(  info.get('saved',    ''))
        self._st_momentum.setText(info.get('momentum',''))
        self._st_rescount.setText(info.get('rescount',''))
        if info.get('log'):
            self._st_log.setText(info['log'])
        self._refresh_title_bar(info)

    def _clear_thread_status(self):
        """スレッド非選択時にステータスバーをクリア"""
        for w in [self._st_viewers, self._st_expiry, self._st_saved,
                  self._st_momentum, self._st_rescount]:
            w.setText('')
        self._st_log.setText('')
        self._st_progress.hide()

    def _toggle_history(self):
        if self._hist_visible:
            self._r_lay.removeWidget(self._hist_pane)
            self._hist_pane.setParent(None)
            self._hist_visible = False
        else:
            self._r_lay.addWidget(self._hist_pane)
            self._hist_pane.show()
            self._hist_visible = True
        self._settings._app["hist_visible"] = self._hist_visible
        self._settings.save()

    def _add_to_favorites(self):
        inner = self._active_inner()
        board = inner._board if inner else self._current_board
        if board:
            self._settings.add_favorite(board.name, board.url)
            self._tree_pane.refresh_favorites()
            self._st_log.setText(f"お気に入りに追加: {board.name}")

    def _open_settings(self, tab_name: str = ""):
        dlg = AppSettingsDialog(self._settings, on_apply=self._on_settings_applied,
                                parent=self)
        if tab_name:
            nb = dlg.findChild(QTabWidget)
            if nb:
                for i in range(nb.count()):
                    if nb.tabText(i) == tab_name:
                        nb.setCurrentIndex(i); break
        dlg.exec()

    def _open_board_settings(self, tab_name: str = ""):
        """現在の板の設定ダイアログを開く（カタログ・自動更新・スタイル）"""
        inner = self._active_inner()
        board = inner._board if inner else self._current_board
        if not board:
            return
        board_key = board.base_url
        display_name = self._board_display_name(board.name, board.url)
        bs = get_board_settings(board_key)

        def _on_apply(updated_bs):
            self._on_board_settings_applied(board, updated_bs)

        dlg = BoardSettingsDialog(bs, display_name, on_apply=_on_apply, parent=self)
        if tab_name:
            dlg.show_tab(tab_name)
        dlg.exec()

    def _on_board_settings_applied(self, board, bs):
        """板設定適用後: 開いているカタログを catset POST → 再取得"""
        from futaba2b_app_qt import CatalogView
        _posted = False
        inner = self._active_inner()
        if not inner or inner._board.url != board.url:
            return
        for j in range(inner.count()):
            w = inner.widget(j)
            if not isinstance(w, CatalogView):
                continue
            if not _posted:
                _posted = True
                def _do_catset(_w=w, _b=board, _bs=bs):
                    import urllib.parse as _up
                    _bd = _up.urlparse(_b.base_url).hostname or ""
                    ok = self._fetcher.post_catset_bs(_b, _bs)
                    if ok:
                        self._catset_reload_signal.emit(_w, _b)
                    else:
                        self._fetcher.set_cxyl_cookie(_bs.catalog_cxyl_str, board_domain=_bd)
                        self._catset_cxyl_signal.emit(_w, _bs.catalog_cxyl_str)
                import threading
                threading.Thread(target=_do_catset, daemon=True).start()

    def _find_in_view(self):
        """現在のビュー（ThreadView / CatalogView）の検索バーを表示"""
        inner = self._active_inner()
        if not inner:
            return
        cur = inner.currentWidget()
        if not hasattr(cur, "_find_bar"):
            return
        fb = cur._find_bar
        # 先に検索バーを確実に表示する。runJavaScript のコールバックは
        # WebEngine の状態によっては発火しないことがあり、表示をそれに
        # 依存させると「Ctrl+Fで出ない」事象が起きるため、表示は同期で行う。
        fb.show_and_focus()
        # 選択テキストがあれば後追いで反映（ベストエフォート）
        view = getattr(cur, "_view", None)
        if view and hasattr(view, "page"):
            def _apply(sel):
                if sel:
                    fb.show_and_focus(sel)
            try:
                view.page().runJavaScript("window.getSelection().toString()", _apply)
            except Exception:
                pass

    def _open_ng_settings(self):
        from futaba2b_dialogs import NgSettingsDialog
        dlg = NgSettingsDialog(self._settings, parent=self)
        dlg.exec()
        # NGフィルタ変更を全開きスレッドに即時反映
        self._settings.invalidate_ng_cache()
        self._settings.save()
        self._redraw_all_threads_with_ng()

    def _redraw_all_threads_with_ng(self):
        """開いている全ThreadViewをNGフィルタ再適用で再描画する"""
        for i in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(i)
            if not isinstance(pane, BoardPane):
                continue
            for j in range(pane._tabs.count()):
                w = pane._tabs.widget(j)
                if isinstance(w, ThreadView) and hasattr(w, 'redraw_with_ng'):
                    w.redraw_with_ng()

    def _on_settings_applied(self):
        # ショートカット設定変更を反映（メニュー再構築）
        self.menuBar().clear()
        self._build_menu()
        # 開いている全BoardPaneのショートカットを更新
        for i in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(i)
            if isinstance(pane, BoardPane) and hasattr(pane, 'update_shortcuts'):
                pane.update_shortcuts(self._settings)
        # 最近閉じたスレ・最近開いた画像のリストを新しいmax件数でトリム
        _max_closed = getattr(self._settings, "recent_closed_max", 30)
        if len(self._closed_tabs) > _max_closed:
            self._closed_tabs = self._closed_tabs[-_max_closed:]
        _max_images = getattr(self._settings, "recent_images_max", 30)
        if len(self._recent_images) > _max_images:
            self._recent_images = self._recent_images[-_max_images:]
        # スクロール末尾カウント設定を開いている全ThreadView/CatalogViewに即時反映
        for i in range(self._outer_tabs.count()):
            inner = self._outer_tabs.widget(i)
            if not isinstance(inner, BoardPane):
                continue
            for j in range(inner.count()):
                w = inner.widget(j)
                if isinstance(w, (ThreadView, CatalogView)):
                    w.apply_scroll_count_setting()
        # 開いている全カタログビューに対して catset POST → 再取得
        _posted_boards: set = set()  # 板ごとに1回だけ POST
        for i in range(self._outer_tabs.count()):
            inner = self._outer_tabs.widget(i)
            if not isinstance(inner, BoardPane):
                continue
            board = inner._board
            for j in range(inner.count()):
                w = inner.widget(j)
                if not isinstance(w, CatalogView):
                    continue
                # catset POST (板ごとに1回) - 板ごとのBoardSettingsを使う
                if board and board.url not in _posted_boards:
                    def _do_catset(_b=board, _w=w):
                        import urllib.parse as _up
                        _bd = _up.urlparse(_b.base_url).hostname or ""
                        _bs = get_board_settings(_b.base_url)
                        ok = self._fetcher.post_catset(_b, _bs)
                        if ok:
                            self._catset_reload_signal.emit(_w, _b)
                        else:
                            cxyl = _bs.catalog_cxyl_str
                            self._fetcher.set_cxyl_cookie(cxyl, board_domain=_bd)
                            self._catset_cxyl_signal.emit(_w, cxyl)
                    threading.Thread(target=_do_catset, daemon=True).start()
                    _posted_boards.add(board.url)
        # タブ幅設定変更 → 全WrapTabBarのキャッシュクリア＆再描画
        self._outer_wrap_bar._tab_width_cache.clear()
        self._outer_wrap_bar._tab_rects_cache_key = None
        self._outer_wrap_bar.update()
        for i in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(i)
            if isinstance(pane, BoardPane) and hasattr(pane, '_wrap_bar'):
                pane._wrap_bar._tab_width_cache.clear()
                pane._wrap_bar._tab_rects_cache_key = None
                pane._wrap_bar.update()
        # テーマ変更時: catalog.png / pin.png / icon.png のキャッシュをリセット
        # → 次回 _catalog_icon() / WrapTabBar.paintEvent / _load_window_icon() で再読み込み
        self._catalog_icon_checked = False
        self._catalog_icon_cache = None
        for i in range(self._outer_tabs.count()):
            pane = self._outer_tabs.widget(i)
            if isinstance(pane, BoardPane) and hasattr(pane, '_wrap_bar'):
                pane._wrap_bar._pin_pixmap_loaded = False
                pane._wrap_bar._pin_pixmap = None
                pane._wrap_bar.update()
        self._outer_wrap_bar._pin_pixmap_loaded = False
        self._outer_wrap_bar._pin_pixmap = None
        self._outer_wrap_bar.update()
        self._load_window_icon()

    def _show_ng_settings(self):
        NgSettingsDialog(self._settings, self).exec()

    def _load_window_icon(self):
        """テーマフォルダ優先で icon.ico/icon.png を読み込みウインドウアイコンに設定する。
        起動時は main() から、テーマ変更時は _on_settings_applied から呼ばれる。"""
        from PySide6.QtWidgets import QApplication
        _theme_root = Path(__file__).parent / "theme"
        for _d in [ThemeManager.theme_dir(), _theme_root]:
            for _ext in ("ico", "png"):
                _ic = _d / f"icon.{_ext}"
                if _ic.exists():
                    icon = QIcon(str(_ic))
                    if not icon.isNull():
                        QApplication.instance().setWindowIcon(icon)
                        self.setWindowIcon(icon)
                        return

    def _show_about(self):
        QMessageBox.information(self, "バージョン情報",
            f"2BP ─ ふたばちゃんねる専用ブラウザ\nバージョン {APP_VER}\n\n"
            "PySide6 + QtWebEngine 版\n旧 tkinter 版から全機能を移行")

    # ── アップデート ────────────────────────────────────────────────────

    @staticmethod
    def _ver_tuple(ver: str):
        try:
            return tuple(int(x) for x in ver.strip().split("."))
        except Exception:
            return (0,)

    def _check_for_update(self):
        """GitHub上のfutaba2b_app_qt.pyからAPP_VERを取得し、現在のバージョンと比較する。"""
        self._st_log.setText("アップデートを確認中...")

        def _job():
            try:
                import requests
                r = requests.get(_UPDATE_VERSION_URL, headers={"User-Agent": UA}, timeout=10)
                r.raise_for_status()
                m = re.search(r'APP_VER\s*=\s*"([0-9.]+)"', r.text)
                if not m:
                    raise ValueError("バージョン情報の取得に失敗しました（形式不正）")
                remote_ver = m.group(1)
                self._main_thread_call.emit(
                    lambda v=remote_ver: self._on_update_check_result(v, None))
            except Exception as e:
                self._main_thread_call.emit(
                    lambda e=e: self._on_update_check_result(None, e))

        threading.Thread(target=_job, daemon=True).start()

    def _on_update_check_result(self, remote_ver, error):
        if error is not None:
            self._st_log.setText("アップデートの確認に失敗しました")
            QMessageBox.warning(self, "アップデート確認",
                "アップデートの確認に失敗しました。\n"
                "ネットワーク接続を確認してください。\n\n"
                f"{error}")
            return

        local_ver = APP_VER
        if self._ver_tuple(remote_ver) <= self._ver_tuple(local_ver):
            self._st_log.setText("お使いのバージョンは最新です")
            QMessageBox.information(self, "アップデート確認",
                f"お使いのバージョンは最新です。\n\n現在のバージョン: v{local_ver}")
            return

        self._st_log.setText(f"新しいバージョン v{remote_ver} が利用可能です")
        ret = QMessageBox.question(self, "アップデート",
            "新しいバージョンが公開されています。\n\n"
            f"現在のバージョン: v{local_ver}\n"
            f"最新バージョン　: v{remote_ver}\n\n"
            "ダウンロードして適用します。\n"
            "適用後、2BPは自動的に再起動します。\n\n"
            "バージョンアップしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self._start_update(remote_ver)
        else:
            self._st_log.setText("")

    def _start_update(self, remote_ver: str):
        """GitHubリポジトリのzipをダウンロードする（適用はPython側で行う・update.bat不要）。"""
        self._st_log.setText("アップデートをダウンロード中...")

        def _job():
            try:
                import requests
                r = requests.get(_UPDATE_ZIP_URL, headers={"User-Agent": UA}, timeout=60)
                r.raise_for_status()
                content = r.content
                if not content:
                    raise ValueError("ダウンロードしたデータが空です")
                self._main_thread_call.emit(lambda c=content: self._on_update_downloaded(c, None))
            except Exception as e:
                self._main_thread_call.emit(lambda e=e: self._on_update_downloaded(None, e))

        threading.Thread(target=_job, daemon=True).start()

    def _on_update_downloaded(self, content, error):
        if error is not None or not content:
            self._st_log.setText("アップデートのダウンロードに失敗しました")
            QMessageBox.warning(self, "アップデート",
                f"アップデートのダウンロードに失敗しました。\n\n{error or 'データが空です'}")
            return
        self._apply_update(content)

    def _apply_update(self, content: bytes):
        """ダウンロードしたzipをPythonで直接展開・上書きし、再起動する（update.bat不要）。
        ① 現行 futaba2b_* を old/{日時}.zip にバックアップ
        ② リポジトリの全ファイルを base 直下に展開・上書き
        ③ 旧プロセスの終了を待って新プロセスを起動する一時ランチャを起動し、自分は終了"""
        import zipfile, io, datetime, sys, subprocess, tempfile, os as _os
        base = Path(__file__).resolve().parent
        try:
            src = zipfile.ZipFile(io.BytesIO(content))
            names = src.namelist()
            if not names:
                raise ValueError("ダウンロードしたZIPが空です")
            # GitHubのzipは「リポジトリ名-ブランチ名/」のフォルダを含むため除去する
            prefix = names[0].split("/")[0] + "/"

            # ① バックアップ: 現行 futaba2b_* を old/{日時}.zip へ
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            old_dir = base / "old"
            old_dir.mkdir(exist_ok=True)
            bak = old_dir / f"{ts}.zip"
            with zipfile.ZipFile(bak, "w", zipfile.ZIP_DEFLATED) as bz:
                for f in base.glob("futaba2b_*"):
                    if f.is_file():
                        bz.write(f, f.name)

            # ② 展開・上書き（プレフィックス除去・サブフォルダ作成）
            for info in src.infolist():
                if info.is_dir() or not info.filename.startswith(prefix):
                    continue
                rel = info.filename[len(prefix):]
                if not rel:
                    continue
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as wf:
                    wf.write(src.read(info.filename))
        except Exception as e:
            self._st_log.setText("アップデートの適用に失敗しました")
            QMessageBox.warning(self, "アップデート",
                f"アップデートの適用に失敗しました。\n更新は行われていません。\n\n{e}")
            return

        # ③ 再起動: 旧プロセス(自分)の終了を待ってから新プロセスを起動する
        #    一時ランチャを生成して起動する（単一起動ロックの衝突を避けるため）
        qt_path = str(base / "futaba2b_qt.py")
        launcher_src = (
            "import sys, time, subprocess\n"
            "pid = int(sys.argv[1]); app = sys.argv[2]; cwd = sys.argv[3]\n"
            "try:\n"
            "    import psutil\n"
            "    alive = lambda p: psutil.pid_exists(p)\n"
            "except Exception:\n"
            "    alive = lambda p: False\n"
            "for _ in range(150):\n"          # 最大15秒、旧プロセス終了を待つ
            "    if not alive(pid):\n"
            "        break\n"
            "    time.sleep(0.1)\n"
            "time.sleep(0.5)\n"               # ロックファイル解放の猶予
            "subprocess.Popen([sys.executable, app], cwd=cwd)\n"
        )
        try:
            launcher_path = Path(tempfile.gettempdir()) / f"2bp_update_launcher_{_os.getpid()}.py"
            launcher_path.write_text(launcher_src, encoding="utf-8")
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                    getattr(subprocess, "DETACHED_PROCESS", 0))
            subprocess.Popen(
                [sys.executable, str(launcher_path), str(_os.getpid()), qt_path, str(base)],
                cwd=str(base), **kwargs)
        except Exception as e:
            QMessageBox.warning(self, "アップデート",
                f"更新は完了しましたが再起動の起動に失敗しました。\n"
                f"手動で2BPを起動し直してください。\n\n{e}")
            return
        self._st_log.setText("アップデートを適用しました。再起動します...")
        # closeEvent経由でタブ状態・設定を保存してから終了する
        self.close()

    # ── タブ状態 保存・復元 ────────────────────────────────────────────────────

    def _save_tab_state(self):
        state = {"boards": []}
        active_outer = self._outer_tabs.currentIndex()
        for i in range(self._outer_tabs.count()):
            inner = self._outer_tabs.widget(i)
            if not isinstance(inner, BoardPane):
                continue
            tabs_info = []
            for j in range(inner.count()):
                w = inner.widget(j)
                is_pinned = (w in inner._pinned)
                if isinstance(w, CatalogView):
                    tabs_info.append({"type": "catalog", "no": 0, "pinned": is_pinned})
                elif isinstance(w, ThreadView):
                    tabs_info.append({"type": "thread", "no": w._thread_no, "pinned": is_pinned})
            state["boards"].append({
                "board_name":  inner._board.name,
                "board_url":   inner._board.url,
                "active":      (i == active_outer),
                "active_inner": inner.currentIndex(),
                "inner_tabs":  tabs_info,
            })
        self._settings.tab_state = state

    def _startup_cache_cleanup(self):
        """起動3秒後にバックグラウンドでキャッシュクリーンアップを実行。
        画像・動画・スレHTMLの3種別を設定（日数/サイズ上限）に従って削除し、
        旧named profile時代のQtWebEngineディスクキャッシュも掃除する。"""
        s = self._settings
        jobs = []   # (label, dir, max_days, max_bytes) — メインスレッドで設定値を確定
        from futaba2b_network import (IMAGE_CACHE_DIR, VIDEO_CACHE_DIR,
                                      THREAD_CACHE_DIR)
        if getattr(s, "cache_img_days_enabled", True) or getattr(s, "cache_img_size_enabled", False):
            jobs.append(("画像", IMAGE_CACHE_DIR,
                getattr(s, "cache_max_days", 7) if getattr(s, "cache_img_days_enabled", True) else 0,
                getattr(s, "cache_img_size_mb", 500) * 1048576 if getattr(s, "cache_img_size_enabled", False) else 0))
        if getattr(s, "cache_video_days_enabled", True) or getattr(s, "cache_video_size_enabled", False):
            jobs.append(("動画", VIDEO_CACHE_DIR,
                getattr(s, "cache_video_days", 3) if getattr(s, "cache_video_days_enabled", True) else 0,
                getattr(s, "cache_video_size_mb", 1024) * 1048576 if getattr(s, "cache_video_size_enabled", False) else 0))
        if getattr(s, "cache_thread_days_enabled", False) or getattr(s, "cache_thread_size_enabled", False):
            jobs.append(("スレHTML", THREAD_CACHE_DIR,
                getattr(s, "cache_thread_days", 30) if getattr(s, "cache_thread_days_enabled", False) else 0,
                getattr(s, "cache_thread_size_mb", 200) * 1048576 if getattr(s, "cache_thread_size_enabled", False) else 0))
        import threading
        def _run():
            # ① 旧 QtWebEngine ディスクキャッシュの掃除
            #    v0.8.090以降は全プロファイルが off-the-record / メモリキャッシュの
            #    ため新規には作られないが、過去のnamed profile時代の残骸
            #    （AppData/Local/<org>/<app>/cache/QtWebEngine 等、数十GBに達する）
            #    を起動のたびに削除する。存在しなければ即終了するので低コスト。
            self._purge_legacy_webengine_cache()
            # ② 画像・動画・スレHTMLのクリーンアップ
            from futaba2b_network import cleanup_cache_dir
            for label, cdir, days, max_bytes in jobs:
                cnt, sz = cleanup_cache_dir(cdir, max_days=days, max_bytes=max_bytes)
                if cnt > 0:
                    if sz >= 1048576:
                        print(f"[Cache] {label}自動クリーンアップ: {cnt}件 ({sz/1048576:.0f} MB) 削除")
                    else:
                        print(f"[Cache] {label}自動クリーンアップ: {cnt}件 ({sz/1024:.0f} KB) 削除")
        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _purge_legacy_webengine_cache():
        """過去バージョンのnamed profileが残したQtWebEngineディスクキャッシュを削除する。
        対象（QStandardPathsベース）:
          - <CacheLocation>/QtWebEngine    (例: AppData/Local/futaba2b/2BP/cache/QtWebEngine)
          - <AppDataLocation>/QtWebEngine  (例: AppData/Roaming/futaba2b/2BP/QtWebEngine)
        現行プロファイルは off-the-record のためこれらを使用せず、削除しても安全。
        """
        import shutil
        from pathlib import Path
        from PySide6.QtCore import QStandardPaths
        targets = []
        for loc in (QStandardPaths.StandardLocation.CacheLocation,
                    QStandardPaths.StandardLocation.AppDataLocation):
            base = QStandardPaths.writableLocation(loc)
            if base:
                targets.append(Path(base) / "QtWebEngine")
        for d in targets:
            try:
                if d.is_dir():
                    # サイズ計測は重い（数十GB/数万ファイル）ため省略し即削除
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"[Cache] 旧QtWebEngineキャッシュを削除: {d}")
            except Exception as e:
                print(f"[Cache] 旧QtWebEngineキャッシュ削除失敗: {d} ({e})")

    def _webengine_warmup(self):
        """QtWebEngineのGPU/レンダラープロセスとrequestsの初回TCP接続を事前起動。
        MainWindowの子QWebEngineViewを生成することでWebEngine初期化に伴うhwnd変更を
        板表示前に完了させ、タスクバーからウィンドウが消える現象を防ぐ。"""
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtCore import QUrl
        # MainWindowの子として生成（トップレベルだとCatalogView生成時にhwndが変わる）
        dummy = QWebEngineView(self)
        dummy.setFixedSize(1, 1)
        dummy.move(0, 0)
        dummy.lower()   # 最背面に
        dummy.show()
        dummy.load(QUrl("about:blank"))
        self._warmup_view = dummy
        dummy.loadFinished.connect(self._on_warmup_done)
        # requestsの初回TCP接続（SSLハンドシェイク）をBGスレッドで事前実行
        # 前回開いていた板ドメイン + www.2chan.net に接続してコネクションプールを温める
        import threading, urllib.parse as _up
        def _tcp_warmup():
            import time as _time
            _t0 = _time.perf_counter()
            # 前回タブ状態から板URLを収集してドメインを抽出
            warmup_domains = set()
            warmup_domains.add("www.2chan.net")
            for entry in self._settings.tab_state.get("boards", []):
                _burl = entry.get("board_url", "")
                _h = _up.urlparse(_burl).hostname
                if _h:
                    warmup_domains.add(_h)
            # お気に入りのドメインも追加
            for fav in self._settings.favorites:
                _h = _up.urlparse(fav.get("url", "")).hostname if isinstance(fav, dict) else None
                if _h:
                    warmup_domains.add(_h)
            import concurrent.futures
            def _conn(domain):
                try:
                    self._fetcher.session.get(
                        f"https://{domain}/",
                        timeout=5,
                        headers={"Cache-Control": "no-store"},
                        allow_redirects=False,
                    )
                except Exception:
                    pass
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(_conn, warmup_domains))
        threading.Thread(target=_tcp_warmup, daemon=True).start()

    def _on_warmup_done(self, _ok):
        if hasattr(self, '_warmup_view'):
            self._warmup_view.hide()
            self._warmup_view.deleteLater()
            del self._warmup_view

    def _restore_tab_state(self):
        boards = self._settings.tab_state.get("boards", [])
        if not boards:
            return

        active_url = next((b["board_url"] for b in boards if b.get("active")), "")

        # ① 板タブを保存順で先に全部作成（addTabの順序＝表示順を保証）
        for entry in boards:
            board = BoardInfo(name=entry.get("board_name", ""),
                              url=entry.get("board_url", ""))
            if board.url:
                self._get_or_create_board_tab(board, activate=False)

        # ② タスクを「アクティブ板を先に・各板内は保存順のまま」並べる
        #    → 板内のタブ並び（位置）が終了前と一致する。アクティブ板優先で初期表示も速い
        tasks_active = []   # アクティブ板のタブ（保存順）
        tasks_rest   = []   # その他の板のタブ（保存順）
        for entry in boards:
            board = BoardInfo(name=entry.get("board_name", ""),
                              url=entry.get("board_url", ""))
            if not board.url:
                continue
            inner_tabs   = entry.get("inner_tabs", [])
            active_inner = entry.get("active_inner", 0)
            is_active_board = (board.url == active_url)
            bucket = tasks_active if is_active_board else tasks_rest
            for j, tab in enumerate(inner_tabs):
                # アクティブ板のアクティブタブだけは前面表示で開く（残りはBG）
                foreground = is_active_board and (j == active_inner)
                bucket.append((board, tab, foreground, entry))

        tasks = tasks_active + tasks_rest
        n_active = len(tasks_active)

        def _restore_pins_and_active():
            """全タブ生成後に、保存時の type/no で実タブを照合してピン留め・
            アクティブ内側タブを復元する（タブ位置は保存順のまま保持済み）"""
            def _match_widget(_pane, _type, _no):
                for k in range(_pane.count()):
                    wv = _pane.widget(k)
                    if _type == "catalog" and isinstance(wv, CatalogView):
                        return wv
                    if (_type == "thread" and isinstance(wv, ThreadView)
                            and getattr(wv, "_thread_no", None) == _no):
                        return wv
                return None
            for entry in boards:
                burl = entry.get("board_url", "")
                if not burl:
                    continue
                pane = None
                for i in range(self._outer_tabs.count()):
                    w = self._outer_tabs.widget(i)
                    if isinstance(w, BoardPane) and w._board.url == burl:
                        pane = w; break
                if pane is None:
                    continue
                saved = entry.get("inner_tabs", [])
                # ピン留め復元
                for t in saved:
                    if t.get("pinned"):
                        wv = _match_widget(pane, t.get("type"), t.get("no"))
                        if wv:
                            pane._pin_tab(wv)
                # アクティブ内側タブ復元
                ai_saved = entry.get("active_inner", 0)
                if 0 <= ai_saved < len(saved):
                    at = saved[ai_saved]
                    wv = _match_widget(pane, at.get("type"), at.get("no"))
                    if wv:
                        ai = pane._tabs.indexOf(wv)
                        if ai >= 0:
                            pane._tabs.setCurrentIndex(ai)

        def _finalize():
            # アクティブ外側（板）タブを選択
            if active_url:
                for i in range(self._outer_tabs.count()):
                    w = self._outer_tabs.widget(i)
                    if isinstance(w, BoardPane) and w._board.url == active_url:
                        self._outer_tabs.setCurrentIndex(i); break
            # 全タブ生成完了後にピン留め・アクティブ内側タブを一括復元
            _restore_pins_and_active()
            self._st_log.setText("前回のタブ状態を復元しました")

        def _open_next(idx: int):
            if idx >= len(tasks):
                _finalize()
                return

            board, tab, foreground, entry = tasks[idx]

            if tab["type"] == "catalog":
                self._show_board_view("catalog", board)
            elif tab["type"] == "thread" and tab.get("no"):
                if foreground:
                    self._open_thread(board, tab["no"])
                else:
                    # バックグラウンドで開く（レンダリングコスト削減・タブ位置は追加順を維持）
                    base = board.url.rsplit("/futaba.htm", 1)[0].rstrip("/") + "/"
                    thread_url = f"{base}res/{tab['no']}.htm"
                    self._open_thread_url_bg(thread_url)

            # アクティブ板のタブを開き終えたら一旦アクティブ板を前面に出してUIを使える状態に
            if idx == n_active - 1:
                if active_url:
                    for i in range(self._outer_tabs.count()):
                        w = self._outer_tabs.widget(i)
                        if isinstance(w, BoardPane) and w._board.url == active_url:
                            self._outer_tabs.setCurrentIndex(i); break

            QTimer.singleShot(0, lambda i=idx+1: _open_next(i))

        _open_next(0)

    # ── 終了処理 ─────────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        """ログファイル（zip/mht/mhtml/html/htm）のD&Dを受け付ける"""
        urls = event.mimeData().urls()
        if any(u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower()
               in ('.zip', '.mht', '.mhtml', '.html', '.htm')
               for u in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """ドロップされたログファイルを開く"""
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            ext  = os.path.splitext(path)[1].lower()
            if ext not in ('.zip', '.mht', '.mhtml', '.html', '.htm'):
                continue
            try:
                if ext in ('.mht', '.mhtml'):
                    self._open_log_mht(path)
                elif ext == '.zip':
                    self._open_log_zip(path)
                else:
                    self._open_log_html(path)
            except Exception as e:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "読み込みエラー", str(e))
        event.acceptProposedAction()

    def showEvent(self, event):
        super().showEvent(event)
        self._fix_taskbar_style()

    def _fix_taskbar_style(self):
        """WS_EX_APPWINDOWを強制設定してタスクバーから消えないようにする。
        QtWebEngineの子ウィンドウがフォーカスを得たときにメインウィンドウが
        タスクバーから外れるWindowsの挙動を防ぐ。"""
        import sys
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE   = -20
            WS_EX_APPWINDOW = 0x00040000
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur | WS_EX_APPWINDOW)
        except Exception as e:
            pass

    def closeEvent(self, event):
        self._save_tab_state()          # ← タブ状態を保存
        # 最近閉じたスレ・最近開いた画像を永続化
        self._settings.recent_closed_list = [
            {"board_url": t[0], "board_name": t[1],
             "thread_no": t[2], "thread_url": t[3], "label": t[4]}
            for t in self._closed_tabs
        ]
        self._settings.recent_images_list = list(self._recent_images)
        self._settings.window_geometry = self.saveGeometry().toHex().data().decode()
        if hasattr(self, "_splitter"):
            self._settings.window_splitter = self._splitter.saveState().toHex().data().decode()
        self._settings.save()
        # 画像ウインドウ（ウインドウモード）の WebEngine を明示クリーンアップ
        win = getattr(self, "_image_window", None)
        if win is not None:
            try:
                win._save_geometry()
            except Exception:
                pass
            try:
                win.image_view.cleanup()
            except Exception:
                pass
            try:
                win.deleteLater()
            except Exception:
                pass
            self._image_window = None
        super().closeEvent(event)

    def _restore_window_state(self):
        from PySide6.QtCore import QByteArray
        if self._settings.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromHex(
                    self._settings.window_geometry.encode()))
            except Exception: pass
        if self._settings.window_splitter and hasattr(self, "_splitter"):
            try:
                self._splitter.restoreState(QByteArray.fromHex(
                    self._settings.window_splitter.encode()))
            except Exception: pass


# ══════════════════════════════════════════════════════════════════════════════
# エントリポイント
# ══════════════════════════════════════════════════════════════════════════════

def main():
    from futaba2b_app_qt import APP_VER
    print(f"2BP v{APP_VER}  起動", flush=True)

    # ── ログ出力（黒いコンソール）の表示/非表示 ──────────────────────────
    # 設定 show_console が False（既定）なら、起動時に Windows のコンソール
    # ウィンドウを隠す。設定は futaba2b_settings.json から先読みする。
    try:
        import json as _jc
        from pathlib import Path as _Pc
        _show_console = False
        _scf = _Pc(__file__).parent / "futaba2b_settings.json"
        if _scf.exists():
            _scd = _jc.loads(_scf.read_text(encoding="utf-8"))
            _show_console = bool(_scd.get("show_console", False))
        if not _show_console and sys.platform.startswith("win"):
            import ctypes
            _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if _hwnd:
                ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────────

    # FFmpeg の詳細ログを抑制（swscaler警告・hwaccel通知等を非表示）
    import os as _os
    _os.environ.setdefault("AV_LOG_FORCE_NOCOLOR", "1")
    _os.environ.setdefault("LIBAV_LOG_LEVEL", "16")   # AV_LOG_ERROR=16
    _os.environ.setdefault("AV_LOG_LEVEL",    "16")

    from PySide6.QtWebEngineCore import QWebEngineUrlScheme
    scheme = QWebEngineUrlScheme(b"futaba")
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme |
                    QWebEngineUrlScheme.Flag.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(scheme)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")          # OS間のUI差異を解消（3299行目）
    app.setApplicationName("2BP"); app.setOrganizationName("futaba2b")

    # ── defaultProfile をメモリキャッシュ化 ──────────────────────────────
    # 各ビューは off-the-record プロファイルだが、warmup用ダミービュー等
    # page指定なしの生成は defaultProfile（ディスク永続）を使うため、
    # こちらもディスクに書かないよう設定する。
    from PySide6.QtWebEngineCore import QWebEngineProfile as _QWEP
    _defp = _QWEP.defaultProfile()
    _defp.setHttpCacheType(_QWEP.HttpCacheType.MemoryHttpCache)
    _defp.setPersistentCookiesPolicy(_QWEP.PersistentCookiesPolicy.NoPersistentCookies)
    # ─────────────────────────────────────────────────────────────────────

    # ── 循環GCをメインスレッドに固定（0xC0000005対策） ──────────────────
    # Pythonの循環GCは任意のスレッド（自動保存・画像DL等のBGスレッド）で
    # 発動し、参照循環に含まれるQtオブジェクト（QWebEngineView等）を
    # 非GUIスレッドで破壊してアクセス違反クラッシュの原因になる。
    # 自動GCを無効化し、メインスレッドのQTimerから定期実行することで
    # Qtオブジェクトの破棄を常にGUIスレッドで行う。
    import gc as _gc
    _gc.disable()
    _gc_timer = QTimer()
    _gc_timer.setInterval(10_000)   # 10秒ごと
    _gc_timer.timeout.connect(lambda: _gc.collect())
    _gc_timer.start()
    app._gc_timer = _gc_timer       # 参照保持（GC/破棄防止）
    # ─────────────────────────────────────────────────────────────────────

    # ── テーマ読み込み & 適用 ─────────────────────────────────────────────
    # 設定ファイルからテーマ名を読む（設定未ロード時は dark をデフォルト）
    _theme_name = "dark"
    try:
        import json as _j
        from pathlib import Path as _P
        _sf = _P(__file__).parent / "futaba2b_settings.json"
        if _sf.exists():
            _sd = _j.loads(_sf.read_text(encoding="utf-8"))
            _theme_name = _sd.get("theme", "dark")
    except Exception:
        pass
    ThemeManager.load(_theme_name)
    app.setStyleSheet(ThemeManager.qt_stylesheet())
    # ─────────────────────────────────────────────────────────────────────

    # ── 複数起動チェック ───────────────────────────────────────────────────
    import tempfile as _tf
    from PySide6.QtCore import QLockFile
    _lock_path = str(Path(_tf.gettempdir()) / "futaba2b_single_instance.lock")
    _pid_path  = str(Path(_tf.gettempdir()) / "futaba2b_single_instance.pid")
    _lock = QLockFile(_lock_path)
    _lock.setStaleLockTime(0)
    if not _lock.tryLock(200):
        # 前のプロセスのPIDを読む
        _prev_pid = None
        try:
            with open(_pid_path) as _pf:
                _prev_pid = int(_pf.read().strip())
        except Exception:
            pass
        ret = QMessageBox.question(
            None, "2BP ─ 既に起動しています",
            "2BP は既に起動しています。\n\n"
            "前のウィンドウを閉じて新しく起動しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            sys.exit(0)
        # 前のプロセスを終了
        if _prev_pid:
            try:
                import subprocess as _sp
                _sp.call(["taskkill", "/F", "/PID", str(_prev_pid)],
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception:
                pass
        import time as _time; _time.sleep(0.8)
        _lock.setStaleLockTime(0)
        if not _lock.tryLock(1500):
            QMessageBox.critical(None, "エラー",
                                 "前のウィンドウを終了できませんでした。\n"
                                 "タスクマネージャーで手動で終了してください。")
            sys.exit(1)
    # 自分のPIDを書き込む
    try:
        import os as _os2
        with open(_pid_path, 'w') as _pf:
            _pf.write(str(_os2.getpid()))
    except Exception:
        pass
    app._single_instance_lock = _lock   # GC されないよう保持
    # ─────────────────────────────────────────────────────────────────────

    # テーマフォルダからアイコンを読み込む（テーマフォルダ優先 → theme/直下 fallback）
    _theme_root_icon = Path(__file__).parent / "theme"
    _icon_found = False
    for _d in [ThemeManager.theme_dir(), _theme_root_icon]:
        for _ext in ("ico", "png"):
            _ic = _d / f"icon.{_ext}"
            if _ic.exists():
                app.setWindowIcon(QIcon(str(_ic)))
                _icon_found = True
                break
        if _icon_found:
            break

    win = MainWindow(); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
