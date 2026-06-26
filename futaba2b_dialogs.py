"""
futaba2b_dialogs.py ─ スタンドアロン ダイアログ群
    ThreadHistoryPane / PostDialog / NgSettingsDialog / AppSettingsDialog
"""
from __future__ import annotations
import threading
import os
import re
import configparser

from PySide6.QtCore    import Qt, QTimer, Signal, QSize, QPoint, QRect
from PySide6.QtGui     import QPixmap, QIcon, QImage, QColor
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout, QGroupBox,
    QLabel, QLineEdit, QTextEdit, QPushButton, QFileDialog, QMessageBox,
    QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QTabBar, QScrollArea, QSpinBox, QCheckBox, QComboBox,
    QButtonGroup, QRadioButton, QListWidget, QListWidgetItem, QInputDialog,
    QApplication, QStyle, QStylePainter, QStyleOptionTab, QSizePolicy,
    QListView,
)

from futaba2b_models   import BoardInfo
from futaba2b_network  import FutabaFetcher
from futaba2b_settings import AppSettings, NgFilter, BoardSettings, get_board_settings
from futaba2b_const    import ThemeManager as _TM



class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, e): e.ignore()

class _NoWheelComboBox(QComboBox):
    def wheelEvent(self, e): e.ignore()

class _VerticalTabBar(QTabBar):
    """West位置での縦文字タブバー（テキストを90度回転して表示）"""
    def tabSizeHint(self, index: int) -> QSize:
        s = super().tabSizeHint(index)
        return QSize(s.height(), s.width())

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionTab()
        for i in range(self.count()):
            self.initStyleOption(opt, i)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, opt)
            painter.save()
            s = opt.rect.size(); s.transpose()
            r = QRect(QPoint(), s)
            r.moveCenter(opt.rect.center())
            opt.rect = r
            c = self.tabRect(i).center()
            painter.translate(c)
            painter.rotate(90)
            painter.translate(-c)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, opt)
            painter.restore()


class _VertTabLabel(QLabel):
    """縦文字タブの1項目（クリックでタブ切り替え）"""
    clicked = Signal(int)

    def __init__(self, text: str, idx: int, parent=None):
        super().__init__(parent)
        self._idx = idx
        self.setText("\n".join(list(text)))   # 1文字ずつ改行
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.setFixedWidth(18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_active(False)

    def set_active(self, active: bool):
        if active:
            self.setStyleSheet(
                f"font-weight:bold; color:{_TM.ui('btn_checked_fg','#fff')}; background:{_TM.ui('btn_checked_bg','#555')};"
                "border-radius:3px; padding:2px 0;")
        else:
            self.setStyleSheet(
                f"color:{_TM.ui('text_muted','#999')}; background:transparent; padding:2px 0;")

    def mousePressEvent(self, event):
        self.clicked.emit(self._idx)
        super().mousePressEvent(event)


class _VertSideBar(QWidget):
    """コメント/手書きのサイドバー（─ 区切り＋縦文字）"""
    currentChanged = Signal(int)

    def __init__(self, labels: list, parent=None):
        super().__init__(parent)
        self.setFixedWidth(20)
        self._current = 0
        self._btns: list = []
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 4, 1, 4)
        v.setSpacing(0)

        def _sep():
            lbl = QLabel("─")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color:#666; font-size:7pt;")
            lbl.setFixedWidth(18)
            return lbl

        for i, text in enumerate(labels):
            v.addWidget(_sep())
            btn = _VertTabLabel(text, i, self)
            btn.clicked.connect(self._on_click)
            v.addWidget(btn)
            self._btns.append(btn)

        v.addWidget(_sep())
        v.addStretch()
        self._update()

    def _on_click(self, idx: int):
        self._current = idx
        self._update()
        self.currentChanged.emit(idx)

    def _update(self):
        for i, btn in enumerate(self._btns):
            btn.set_active(i == self._current)

    def setCurrentIndex(self, idx: int):
        self._on_click(idx)

# ── テーブル列幅 保存/復元ユーティリティ ─────────────────────────────────────
import json as _json

def _save_col_widths(table, settings, attr: str):
    """QTableWidget の全列幅を AppSettings に保存する"""
    widths = [table.columnWidth(c) for c in range(table.columnCount())]
    setattr(settings, attr, _json.dumps(widths))
    settings.save()

def _restore_col_widths(table, settings, attr: str):
    """AppSettings から列幅を復元する。保存値がなければ何もしない"""
    raw = getattr(settings, attr, "")
    if not raw:
        return
    try:
        widths = _json.loads(raw)
    except Exception:
        return
    for c, w in enumerate(widths):
        if c < table.columnCount() and isinstance(w, int) and w > 0:
            table.setColumnWidth(c, w)

class ThreadHistoryPane(QWidget):
    thread_open_requested = Signal(dict)
    hide_requested        = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._build()

    def _build(self):
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 2); lay.setSpacing(0)

        # ヘッダー
        hdr = QWidget(); hdr.setStyleSheet(f"background:{_TM.ui('panel_header_bg','#B0B0B0')};")
        h_lay = QHBoxLayout(hdr); h_lay.setContentsMargins(6, 2, 2, 2)
        lbl = QLabel("スレッド履歴"); lbl.setStyleSheet("font-weight:bold;font-size:8pt;")
        h_lay.addWidget(lbl); h_lay.addStretch()
        close_btn = QPushButton("×"); close_btn.setFixedSize(20, 20)
        close_btn.setFlat(True); close_btn.clicked.connect(self.hide_requested.emit)
        h_lay.addWidget(close_btn); lay.addWidget(hdr)

        # テーブル
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["板", "スレッド", "最後に更新した時間"])
        _th = self._table.horizontalHeader()
        _th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _th.setSortIndicatorShown(True)
        _th.sectionClicked.connect(self._sort_table)
        _th.sectionResized.connect(
            lambda *_: _save_col_widths(self._table, self._settings, "table_col_widths_history"))
        self._sort_col = -1
        self._sort_asc = True
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMaximumHeight(120)
        self._table.verticalHeader().setDefaultSectionSize(17)
        self._table.cellDoubleClicked.connect(self._on_double)
        # デフォルト列幅
        self._table.setColumnWidth(0, 70)
        self._table.setColumnWidth(1, 200)
        self._table.setColumnWidth(2, 120)
        _restore_col_widths(self._table, self._settings, "table_col_widths_history")
        lay.addWidget(self._table)
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        for h in self._settings.thread_history:
            row = self._table.rowCount(); self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(h.get("board", "")))
            self._table.setItem(row, 1, QTableWidgetItem(h.get("title", "")))
            self._table.setItem(row, 2, QTableWidgetItem(h.get("time", "")))

    def _on_double(self, row: int, _col: int):
        if row < len(self._settings.thread_history):
            self.thread_open_requested.emit(self._settings.thread_history[row])

    def _sort_table(self, col: int):
        """ヘッダクリックでスレッド履歴をソート（データも並び替え）"""
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        key_map = {0: "board", 1: "title", 2: "time"}
        key_name = key_map.get(col, "time")
        self._settings.thread_history.sort(
            key=lambda h: str(h.get(key_name, "")).lower(),
            reverse=not self._sort_asc)
        self.refresh()


# ══════════════════════════════════════════════════════════════════════════════
# 投稿ダイアログ (画像・件名・削除キー対応)
# ══════════════════════════════════════════════════════════════════════════════

class _ScaledPreviewLabel(QLabel):
    """リサイズ時に常にアスペクト比を維持しながら全体フィット表示するプレビューラベル。
    画像をセットしても枠を押し広げない（枠に収まるようスケーリングする）。"""
    clicked = Signal()   # 画像セット中に左クリックで発火

    def __init__(self, parent=None):
        super().__init__(parent)
        self._orig_pix: QPixmap | None = None
        # ウィジェットが画像サイズに引っ張られないよう Ignored に設定
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(1, 1)

    def set_preview(self, pix: QPixmap):
        self._orig_pix = pix
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rescale()

    def clear_preview(self):
        self._orig_pix = None
        self.unsetCursor()
        self.clear()

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._orig_pix and not self._orig_pix.isNull()
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._orig_pix:
            self._rescale()

    def sizeHint(self):
        # 元画像サイズを返さず固定値を返すことでsplitterへの影響をゼロにする
        from PySide6.QtCore import QSize
        return QSize(200, 80)

    def _rescale(self):
        if not self._orig_pix or self._orig_pix.isNull():
            return
        scaled = self._orig_pix.scaled(
            max(1, self.width() - 4), max(1, self.height() - 4),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        super().setPixmap(scaled)


class _CommentEdit(QTextEdit):
    """コメント入力欄。Ctrl+V は画像優先・プレーンテキスト専用。
    画像/動画ファイルのD&Dは添付ファイル欄に転送する。"""

    _DD_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'webm', 'mp4'}

    def __init__(self, dialog: "PostDialog", parent=None):
        super().__init__(parent)
        self._dlg = dialog
        self.setAcceptDrops(True)

    def keyPressEvent(self, event):
        # Shift+Enter → 投稿
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and
                event.modifiers() == Qt.KeyboardModifier.ShiftModifier):
            self._dlg._post()
            return
        if (event.key() == Qt.Key.Key_V and
                event.modifiers() == Qt.KeyboardModifier.ControlModifier):
            cb = QApplication.clipboard()
            img = cb.image()
            if not img.isNull():
                self._dlg._paste_clipboard_image(img)
                return
            text = cb.text()
            if text:
                self.insertPlainText(text)
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        """ドロップ/ペーストで画像が混入しないようプレーンテキストのみ受付。
        ただしファイルURLは _dlg に転送するため、ここでは何もしない。"""
        if source.hasUrls():
            # ファイルURLは dragEnterEvent/dropEvent で処理済みなのでここでは無視
            return
        if source.hasText():
            self.insertPlainText(source.text())

    def _is_media_url(self, url) -> str:
        """ローカルファイルURLなら対応拡張子のパスを返す、それ以外は空文字"""
        path = url.toLocalFile()
        if path:
            ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
            if ext in self._DD_EXTS:
                return path
        return ''

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if self._is_media_url(u):
                    event.acceptProposedAction()
                    return
        # メディアファイル以外はデフォルト処理（テキストD&Dを許可）
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if self._is_media_url(u):
                    event.acceptProposedAction()
                    return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                path = self._is_media_url(u)
                if path:
                    self._dlg._set_file_path(path)
                    event.acceptProposedAction()
                    return
        # メディアファイル以外はデフォルト処理
        super().dropEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# フォルダビューアパネル（PostDialogのプレビュー右側）
# ──────────────────────────────────────────────────────────────────────────────
class _FolderViewerPanel(QWidget):
    """登録フォルダをボタン一覧で表示し、押すとそのフォルダをサムネ大で開くウィジェット"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(3)
        lay.addWidget(QLabel("📁 フォルダ"))
        # スクロールエリアの中にボタングリッドを配置
        from PySide6.QtWidgets import QScrollArea
        self._btn_area = QWidget()
        self._btn_lay  = QGridLayout(self._btn_area)
        self._btn_lay.setContentsMargins(0, 0, 0, 0)
        self._btn_lay.setSpacing(2)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self._btn_area)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(sa, 1)
        self._rebuild()

    def _rebuild(self):
        """設定からフォルダボタンを再構築"""
        # 既存ボタンを削除
        while self._btn_lay.count():
            item = self._btn_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        folders   = getattr(self._settings, "image_save_folders", [])
        wrap      = max(1, getattr(self._settings, "image_save_btn_wrap", 3))
        label_len = getattr(self._settings, "image_save_label_len", 0)
        for i, folder in enumerate(folders):
            import os
            base = os.path.basename(folder) or folder
            lbl  = (base[:label_len] if label_len > 0 else base)
            btn  = QPushButton(lbl)
            btn.setToolTip(folder)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _=None, f=folder: self._open_folder(f))
            row, col = divmod(i, wrap)
            self._btn_lay.addWidget(btn, row, col)
        self._btn_lay.setRowStretch(
            (len(folders) - 1) // wrap + 1 if folders else 0, 1)

    def _open_folder(self, folder: str):
        """フォルダの標準ファイル選択ダイアログを開き、選択画像を添付欄にセット"""
        import os
        if not os.path.isdir(folder):
            self._show_folder_error(f"フォルダが見つかりません:\n{folder}")
            return
        fdlg = QFileDialog(self.window(), "画像を選択", folder,
                           "画像/動画 (*.jpg *.jpeg *.png *.gif *.webp *.webm *.mp4);;全て (*)")
        fdlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        fdlg.setViewMode(QFileDialog.ViewMode.Detail)
        # 大アイコン表示に切り替え
        for lv in fdlg.findChildren(QListView):
            lv.setViewMode(QListView.ViewMode.IconMode)
            lv.setIconSize(QSize(96, 96))
            break
        if fdlg.exec() != QDialog.DialogCode.Accepted:
            return
        paths = fdlg.selectedFiles()
        if not paths:
            return
        path = paths[0]
        post_dlg = self.window()
        if isinstance(post_dlg, PostDialog) and hasattr(post_dlg, "_set_file_path"):
            post_dlg._set_file_path(path)

    def _show_folder_error(self, msg: str):
        """フォルダが存在しない時の赤点滅ポップアップ"""
        win = self.window()
        dlg = QDialog(win, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        dlg.setModal(False)
        lbl = QLabel(dlg)
        import html as _html
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setText(_html.escape(msg).replace("\n", "<br>"))
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setContentsMargins(16, 12, 16, 12)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(lbl)
        _N = "QDialog{background:#EFDFD6;border:2px solid #7B0004;border-radius:6px;}QLabel{color:#7B0004;font-size:12px;font-weight:bold;}"
        _B = "QDialog{background:#FFAAAA;border:2px solid #7B0004;border-radius:6px;}QLabel{color:#7B0004;font-size:12px;font-weight:bold;}"
        dlg.setStyleSheet(_N)
        dlg.adjustSize()
        wr = win.geometry()
        dlg.move(wr.x() + (wr.width() - dlg.width()) // 2,
                 wr.y() + 80)
        dlg.show()
        _st = [0]
        def _blink():
            _st[0] += 1
            dlg.setStyleSheet(_B if _st[0] % 2 == 1 else _N)
            if _st[0] < 4:
                QTimer.singleShot(300, _blink)
        QTimer.singleShot(200, _blink)
        dlg.mousePressEvent = lambda _e: dlg.close()
        QTimer.singleShot(5000, dlg.close)

    def _update_btn_width(self):
        """splitterで幅が変わった時にボタン幅を均等調整する"""
        wrap = max(1, getattr(self._settings, "image_save_btn_wrap", 3))
        # 利用可能幅 = パネル幅 - マージン - スペーシング
        avail = self.width() - 4 - 2 * (wrap - 1)
        btn_w = max(40, avail // wrap)
        for i in range(self._btn_lay.count()):
            item = self._btn_lay.itemAt(i)
            if item and item.widget():
                item.widget().setFixedWidth(btn_w)

    def refresh(self):
        """設定変更後に呼び出してボタンを再構築"""
        self._rebuild()


# ──────────────────────────────────────────────────────────────────────────────
# フォルダサムネイルダイアログ
# ──────────────────────────────────────────────────────────────────────────────
_IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',
             '.mp4', '.webm', '.mov', '.m4v'}

class _FolderThumbDialog(QDialog):
    """登録フォルダの画像をサムネ大で一覧表示し選択できるダイアログ"""

    image_selected = Signal(str)   # ダブルクリックで選択 → フルパスを通知
    _thumb_ready   = Signal(int, object, str)  # BGスレッド→UI サムネ完成通知 (i, QImage, fname)
    _THUMB = 160  # サムネ表示サイズ

    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        import os
        self._folder = folder
        self.setWindowTitle(f"📁 {os.path.basename(folder) or folder}")
        self.resize(780, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        # パス表示
        lbl_path = QLabel(folder)
        lbl_path.setStyleSheet("font-size:8pt;color:#aaa;")
        lay.addWidget(lbl_path)
        # リストウィジェット（アイコン大）
        self._lw = QListWidget()
        self._lw.setViewMode(QListWidget.ViewMode.IconMode)
        self._lw.setIconSize(QSize(self._THUMB, self._THUMB))
        self._lw.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._lw.setSpacing(6)
        self._lw.setWordWrap(True)
        self._lw.setDragEnabled(False)
        self._lw.itemDoubleClicked.connect(self._on_item_dblclick)
        lay.addWidget(self._lw, 1)
        # フォルダを開くボタン
        btn_row = QHBoxLayout()
        btn_open = QPushButton("フォルダを開く"); btn_open.setFixedWidth(120)
        btn_open.clicked.connect(lambda: self._open_explorer())
        btn_row.addWidget(btn_open); btn_row.addStretch()
        # 選択ヒント
        hint = QLabel("ダブルクリックで添付に設定")
        hint.setStyleSheet("font-size:8pt;color:#888;")
        btn_row.addWidget(hint)
        lay.addLayout(btn_row)
        # 非同期でサムネを読み込む
        self._thumb_ready.connect(self._set_thumb)
        self._files = []   # _load_thumbs で設定
        self._load_thumbs()

    def _load_thumbs(self):
        import os, threading
        files = sorted(
            [f for f in os.listdir(self._folder)
             if os.path.splitext(f)[1].lower() in _IMG_EXTS],
            key=lambda f: os.path.getmtime(os.path.join(self._folder, f)),
            reverse=True
        )
        self._files = files   # ダブルクリック時にインデックスからパスを取得するため保持
        # まずアイテムだけ追加（プレースホルダ）
        for fname in files:
            item = QListWidgetItem(fname)
            item.setSizeHint(QSize(self._THUMB + 10, self._THUMB + 28))
            self._lw.addItem(item)

        # バックグラウンドでサムネ生成
        def _worker():
            # QPixmapはGUIスレッド専用 → BGスレッドではQImageを使う
            # BGスレッドからのQTimer.singleShotは禁止 → Signal経由でUIスレッドへ
            for i, fname in enumerate(files):
                path = os.path.join(self._folder, fname)
                img = QImage()
                ext = os.path.splitext(fname)[1].lower()
                if ext in ('.mp4', '.webm', '.mov', '.m4v'):
                    # 動画はフィルムアイコン的なテキストで代替
                    pass
                else:
                    try:
                        img.load(path)
                    except Exception:
                        pass
                if not img.isNull():
                    img = img.scaled(self._THUMB, self._THUMB,
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                self._thumb_ready.emit(i, img, fname)
        threading.Thread(target=_worker, daemon=True).start()

    def _set_thumb(self, i: int, img, fname: str):
        if i < self._lw.count():
            item = self._lw.item(i)
            if item:
                if img is not None and not img.isNull():
                    item.setIcon(QIcon(QPixmap.fromImage(img)))
                else:
                    item.setText(f"🎞 {fname}")

    def _on_item_dblclick(self, item):
        """アイテムをダブルクリックしたらフルパスをシグナルで通知して閉じる"""
        import os
        row = self._lw.row(item)
        if 0 <= row < len(self._files):
            path = os.path.join(self._folder, self._files[row])
            self.image_selected.emit(path)
            self.accept()

    def _open_explorer(self):
        import subprocess, sys, os
        folder = self._folder
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])


class _SampleImageWindow(QDialog):
    """返信ウインドウのプレビュー（サンプル）クリックで開く画像ビューア。
    クリックで「フィット ↔ 等倍」切替。等倍中はドラッグで表示位置を移動できる。
    サイズ・位置を設定に記憶し、次回の初期サイズ・位置に反映する。既定 600x400。"""
    def __init__(self, pix: QPixmap, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._orig_pix = pix
        self._fit_mode = True          # True=フィット表示, False=等倍表示
        self._dragging = False
        self._drag_moved = False
        self._drag_pos = None
        self.setWindowTitle("プレビュー")
        self.setWindowFlags(Qt.WindowType.Window)
        self.setSizeGripEnabled(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)

        # スクロールエリア（等倍時のパン用）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._lbl = QLabel()
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setWidget(self._lbl)
        lay.addWidget(self._scroll)

        self._apply_fit()
        self.resize(600, 400)
        _sz = getattr(settings, "post_sample_view_size", [])
        if isinstance(_sz, list) and len(_sz) == 2:
            try: self.resize(int(_sz[0]), int(_sz[1]))
            except Exception: pass
        _pos = getattr(settings, "post_sample_view_pos", [])
        if isinstance(_pos, list) and len(_pos) == 2:
            try: self.move(int(_pos[0]), int(_pos[1]))
            except Exception: pass

    def set_image(self, pix: QPixmap):
        self._orig_pix = pix
        self._fit_mode = True
        self._apply_fit()
        QTimer.singleShot(0, self._apply_fit)

    # ── 表示モード ────────────────────────────────────────────────────────
    def _apply_fit(self):
        """ウインドウに収まるよう縮小表示（拡大はしない）。
        元画像がビューポートより小さければ等倍のまま、大きければ
        アスペクト比を保って縮小する。常にビューポート内に全体が収まる
        ため上下左右が欠けない。"""
        if not self._orig_pix or self._orig_pix.isNull():
            return
        vp = self._scroll.viewport().size()
        avail_w = max(1, vp.width())
        avail_h = max(1, vp.height())
        ow = max(1, self._orig_pix.width())
        oh = max(1, self._orig_pix.height())
        # ビューポートに収まる倍率と 1.0（=等倍）の小さい方 → 拡大しない
        scale = min(avail_w / ow, avail_h / oh, 1.0)
        if scale >= 1.0:
            scaled = self._orig_pix                 # 等倍（拡大なし）
        else:
            scaled = self._orig_pix.scaled(
                max(1, int(ow * scale)), max(1, int(oh * scale)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        self._lbl.setPixmap(scaled)
        self._lbl.resize(scaled.size())
        self._scroll.setWidgetResizable(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _apply_actual(self):
        """等倍（100%）表示"""
        if not self._orig_pix or self._orig_pix.isNull():
            return
        self._lbl.setPixmap(self._orig_pix)
        self._lbl.resize(self._orig_pix.size())
        self._scroll.setWidgetResizable(False)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        # 中央にスクロール
        vp = self._scroll.viewport().size()
        h = self._scroll.horizontalScrollBar()
        v = self._scroll.verticalScrollBar()
        h.setValue((h.maximum()) // 2)
        v.setValue((v.maximum()) // 2)

    def _toggle_mode(self):
        self._fit_mode = not self._fit_mode
        if self._fit_mode:
            self._apply_fit()
        else:
            self._apply_actual()

    # ── イベント ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._fit_mode:
                # 等倍モード: ドラッグ開始準備
                self._dragging = True
                self._drag_moved = False
                self._drag_pos = event.globalPosition().toPoint()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_pos
            if abs(delta.x()) > 3 or abs(delta.y()) > 3:
                self._drag_moved = True
            self._drag_pos = event.globalPosition().toPoint()
            h = self._scroll.horizontalScrollBar()
            v = self._scroll.verticalScrollBar()
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._dragging
            self._dragging = False
            self._drag_pos = None
            if was_dragging and not self._drag_moved:
                # ドラッグなしクリック → フィットに戻す
                self._toggle_mode()
            elif was_dragging:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                # フィットモードでクリック → 等倍に切替
                self._toggle_mode()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode:
            # 即時フィット（ドラッグ追従用）に加え、レイアウト確定後に再フィット。
            # リサイズ直後は viewport().size() が旧サイズのままのことがあり、
            # 縮小時に画像が新ビューポートをはみ出して上下が欠けるのを防ぐ。
            self._apply_fit()
            QTimer.singleShot(0, self._apply_fit)

    def showEvent(self, event):
        super().showEvent(event)
        # 表示直後はレイアウトが確定してから全体フィットさせる
        if self._fit_mode:
            QTimer.singleShot(0, self._apply_fit)

    def _save_geometry(self):
        self._settings.post_sample_view_size = [self.width(), self.height()]
        self._settings.post_sample_view_pos  = [self.x(), self.y()]
        self._settings.save()

    def closeEvent(self, event):
        self._save_geometry()
        super().closeEvent(event)


class PostDialog(QDialog):
    _result_signal = Signal(bool, str, int)  # 投稿結果 thread-safe (ok, msg, new_thread_no)
    pin_after_post    = Signal()        # 投稿成功後にピン留め要求
    scroll_after_post = Signal()        # 投稿成功後に最下部スクロール要求
    activate_tab      = Signal(int)     # タイトルバークリック → 対応タブをアクティブ化

    def __init__(self, board: BoardInfo, fetcher: FutabaFetcher,
                 settings: AppSettings, resto: int = 0,
                 quote_text: str = "", on_success=None, parent=None):
        super().__init__(parent)
        is_new_thread = (resto == 0)
        self.setWindowTitle(f"{'返信' if resto else 'スレッド作成'} ─ {board.name}")
        self.resize(580, 460)
        # 記憶済みサイズ・位置を復元
        _sz = getattr(settings, "post_dialog_size", [])
        if isinstance(_sz, list) and len(_sz) == 2:
            try:
                self.resize(int(_sz[0]), int(_sz[1]))
            except Exception:
                pass
        _pos = getattr(settings, "post_dialog_pos", [])
        if isinstance(_pos, list) and len(_pos) == 2:
            try:
                self.move(int(_pos[0]), int(_pos[1]))
            except Exception:
                pass
        # スレ立てウインドウはタイトルバーを赤くする
        if is_new_thread:
            self.setStyleSheet(
                "QDialog { border-top: 3px solid #cc0000; }"
            )
            title_lbl = QLabel(f"🧵 スレッド作成 ─ {board.name}")
            title_lbl.setStyleSheet(
                "background:#cc0000;color:#fff;font-weight:bold;"
                "padding:4px 8px;font-size:10pt;")
        self._board      = board; self._fetcher = fetcher
        self._settings   = settings; self._resto = resto
        self._img_path   = ""
        self._img_is_tmp = False
        self._clip_image = None      # クリップボード貼付時の元QImage（品質再適用用）
        self._on_success = on_success
        self._result_signal.connect(self._on_result)
        self.setAcceptDrops(True)  # D&Dを有効化

        lay = QVBoxLayout(self)

        if is_new_thread:
            lay.addWidget(title_lbl)

        form = QFormLayout()

        # おなまえ + ☑記憶 + 📌ピンボタン（右端）
        name_lay = QHBoxLayout(); name_lay.setSpacing(6)
        self._name = QLineEdit(settings.post_name or "")
        self._name.setFixedWidth(200)
        self._chk_save_name = QCheckBox("記憶")
        self._chk_save_name.setChecked(getattr(settings, 'post_save_name', True))
        name_lay.addWidget(self._name)
        name_lay.addWidget(self._chk_save_name)
        name_lay.addStretch()
        self._pin_btn = QPushButton("📌")
        self._pin_btn.setFixedWidth(30)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(getattr(settings, 'post_dialog_pin', False))
        self._pin_btn.setToolTip("ON: 投稿後もウィンドウを閉じない\nOFF: 投稿後にウィンドウを閉じる")
        self._pin_btn.setStyleSheet(
            "QPushButton{border:1px solid #555;border-radius:3px;padding:1px 4px;}"
            "QPushButton:checked{background:#664400;border-color:#aa6600;}"
        )
        name_lay.addWidget(self._pin_btn)
        self._chk_scroll_bottom = None  # 返信時は削除キー行に配置
        form.addRow("おなまえ", name_lay)

        # E-mail + ☑記憶 + sage
        mail_lay = QHBoxLayout(); mail_lay.setSpacing(6)
        # 記憶がONかつ保存済みのmailを表示（sageも含めてそのまま表示）
        _save_mail = getattr(settings, 'post_save_mail', True)
        _initial_mail = (settings.post_mail or "") if _save_mail else ""
        self._mail = QLineEdit(_initial_mail)
        self._mail.setFixedWidth(200)
        self._chk_save_mail = QCheckBox("記憶")
        self._chk_save_mail.setChecked(_save_mail)
        sage_btn = QPushButton("sage"); sage_btn.setFixedWidth(44)
        sage_btn.setToolTip("メールにsageを入力")
        sage_btn.setDefault(False)
        sage_btn.setAutoDefault(False)
        sage_btn.clicked.connect(lambda: self._mail.setText("sage"))
        mail_lay.addWidget(self._mail)
        mail_lay.addWidget(self._chk_save_mail)
        mail_lay.addWidget(sage_btn)
        mail_lay.addStretch()
        form.addRow("E-mail", mail_lay)

        # 題名 + 投稿ボタン（返信時は「返信する」、スレ立ては「スレッドを作成」）
        sub_lay = QHBoxLayout(); sub_lay.setSpacing(6)
        self._sub = QLineEdit()
        self._sub.setFixedWidth(240)
        sub_lay.addWidget(self._sub)
        _post_label = "スレッドを作成" if is_new_thread else "返信する(SHIFT+ENTER)"
        self._btn_post = QPushButton(_post_label)
        self._btn_post.setFixedWidth(160)
        self._btn_post.setDefault(False)
        self._btn_post.setAutoDefault(False)
        self._btn_post.clicked.connect(self._post)
        sub_lay.addWidget(self._btn_post)
        sub_lay.addStretch()
        form.addRow("題名", sub_lay)
        lay.addLayout(form)

        # ── コメント / 手書き エリア（左サイドバー + QStackedWidget）──────
        from PySide6.QtWidgets import QStackedWidget
        comment_area = QWidget()
        ca_lay = QHBoxLayout(comment_area)
        ca_lay.setContentsMargins(0, 0, 0, 0)
        ca_lay.setSpacing(0)

        self._side_bar = _VertSideBar(["コメント", "手書き"])
        self._side_bar.currentChanged.connect(self._on_comment_tab_changed)

        self._content_stack = QStackedWidget()

        # コメントページ
        self._comment = _CommentEdit(self)
        self._comment.setStyleSheet("font-size: 16px;")
        self._comment.setPlaceholderText("コメントを入力…")
        if quote_text:
            self._comment.setPlainText(quote_text)
            cur = self._comment.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            self._comment.setTextCursor(cur)
        self._content_stack.addWidget(self._comment)

        # 手書きページ
        self._tegaki_view = self._build_tegaki_tab()
        self._content_stack.addWidget(self._tegaki_view)

        ca_lay.addWidget(self._side_bar)
        ca_lay.addWidget(self._content_stack, 1)
        lay.addWidget(comment_area, 1)

        # 初期フォーカスはコメント欄
        QTimer.singleShot(0, self._comment.setFocus)

        # ── 添付File行（コメントとプレビューの間）────────────────────────
        img_w = QWidget()
        img_lay = QHBoxLayout(img_w)
        img_lay.setContentsMargins(0, 2, 0, 2)
        img_lay.addWidget(QLabel("添付File"))
        self._img_edit = QLineEdit()
        self._img_edit.setPlaceholderText("画像ファイルのパス (任意・直接入力可)")
        self._img_edit.setToolTip(
            "ファイルのパスを直接入力できます。\n"
            "Enter またはフォーカスを外すと、存在するファイルをプレビューに表示します。")
        # 直接入力対応: 入力確定（Enter / フォーカスアウト）でパスを反映
        self._img_edit.editingFinished.connect(self._on_img_edit_finished)
        img_lay.addWidget(self._img_edit, 1)
        browse_btn = QPushButton("参照…"); browse_btn.setFixedWidth(60)
        browse_btn.clicked.connect(self._browse_image); img_lay.addWidget(browse_btn)
        self._paste_btn = QPushButton("📋貼付"); self._paste_btn.setFixedWidth(60)
        self._paste_btn.setToolTip("クリップボードから画像を貼り付け")
        self._paste_btn.clicked.connect(self._paste_from_clipboard_btn)
        img_lay.addWidget(self._paste_btn)
        self._clear_btn = QPushButton("×解除"); self._clear_btn.setFixedWidth(52)
        self._clear_btn.clicked.connect(self._clear_image)
        self._clear_btn.hide()   # 添付なし時は非表示
        img_lay.addWidget(self._clear_btn)
        # 貼付け形式・品質を同じ行に配置
        img_lay.addWidget(QLabel("  貼付け形式:"))
        self._clip_fmt = QComboBox()
        self._clip_fmt.addItems(["jpg", "png"])
        self._clip_fmt.setCurrentText(getattr(settings, "post_img_format", "jpg"))
        self._clip_fmt.setFixedWidth(60)
        self._clip_fmt.currentTextChanged.connect(self._on_fmt_changed)
        img_lay.addWidget(self._clip_fmt)
        self._lbl_quality = QLabel("  品質:")
        self._spin_quality = QSpinBox()
        self._spin_quality.setRange(1, 100)
        self._spin_quality.setValue(getattr(settings, "post_img_quality", 80))
        self._spin_quality.setSuffix(" %")
        self._spin_quality.setFixedWidth(68)
        img_lay.addWidget(self._lbl_quality)
        img_lay.addWidget(self._spin_quality)
        img_lay.addStretch()
        self._on_fmt_changed(self._clip_fmt.currentText())
        self._spin_quality.valueChanged.connect(self._regenerate_clip_image)
        self._clip_fmt.currentTextChanged.connect(self._regenerate_clip_image)

        # ── プレビュー ────────────────────────────────────────────────────
        from PySide6.QtWidgets import QSplitter
        self._preview_lbl = _ScaledPreviewLabel()
        self._preview_lbl.setText("Ctrl+V でクリップボード画像を貼付けするとここにプレビューが表示されます")
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_lbl.setStyleSheet(
            f"background:{_TM.ui('panel_bg2','#2a2a2a')};color:{_TM.ui('text_muted','#888')};border:1px solid {_TM.ui('panel_border','#444')};"
            "border-radius:4px;font-size:9pt;padding:8px;")
        self._preview_lbl.setMinimumHeight(40)
        self._preview_lbl.clicked.connect(self._open_sample_window)

        # ── フォルダビューアパネル（プレビュー右側）────────────────────────
        self._folder_panel = _FolderViewerPanel(settings, self)
        self._folder_panel.setMinimumWidth(80)

        # プレビュー + フォルダパネルを横並び Splitter で包む
        self._preview_folder_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._preview_folder_splitter.addWidget(self._preview_lbl)
        self._preview_folder_splitter.addWidget(self._folder_panel)
        self._preview_folder_splitter.setStretchFactor(0, 3)
        self._preview_folder_splitter.setStretchFactor(1, 1)
        self._preview_folder_splitter.setSizes([300, 150])
        # splitter サイズ変更時にフォルダボタン幅を動的更新
        self._preview_folder_splitter.splitterMoved.connect(
            lambda pos, idx: self._folder_panel._update_btn_width())
        # 保存済み位置を復元
        _sp2_hex = getattr(settings, "post_dialog_splitter2", "")
        if _sp2_hex:
            from PySide6.QtCore import QByteArray
            self._preview_folder_splitter.restoreState(
                QByteArray.fromHex(_sp2_hex.encode()))

        preview_row = self._preview_folder_splitter

        # コメント + 添付File行 + プレビューを縦3段の Splitter で包む
        # コメントエリアと添付Fileをまとめた上段ウィジェットを作る
        upper_w = QWidget()
        upper_lay = QVBoxLayout(upper_w)
        upper_lay.setContentsMargins(0, 0, 0, 0)
        upper_lay.setSpacing(0)
        upper_lay.addWidget(comment_area, 1)
        upper_lay.addWidget(img_w)

        _splitter = QSplitter(Qt.Orientation.Vertical)
        _splitter.addWidget(upper_w)
        _splitter.addWidget(preview_row)
        _splitter.setStretchFactor(0, 3)
        _splitter.setStretchFactor(1, 1)
        _splitter.setSizes([260, 100])
        # lay から comment_area を取り出して splitter に差し替え
        idx = lay.indexOf(comment_area)
        lay.removeWidget(comment_area)
        lay.insertWidget(idx, _splitter, 1)
        self._preview_splitter = _splitter
        # 保存済み分割位置を復元
        _sp_hex = getattr(settings, "post_dialog_splitter", "")
        if _sp_hex:
            from PySide6.QtCore import QByteArray
            _splitter.restoreState(QByteArray.fromHex(_sp_hex.encode()))



        # 書き込みルール（スレッドを一度開いた後は板情報に格納済み）
        rules_html = getattr(board, 'board_rules_html', '')
        rules_txt  = getattr(board, 'board_rules_text', '')
        if rules_html or rules_txt:
            from PySide6.QtWidgets import QTextBrowser
            from PySide6.QtGui import QDesktopServices
            import re as _re

            # 保存された開閉状態を読み込む（デフォルトは閉じ）
            _rules_key   = getattr(board, 'url', '') or ''
            _rules_open  = settings.post_rules_open.get(_rules_key, False)

            # ── トグルボタン ──────────────────────────────────────────────
            toggle_btn = QPushButton("▶ 板の注意事項" if not _rules_open else "▼ 板の注意事項")
            toggle_btn.setCheckable(True)
            toggle_btn.setChecked(_rules_open)
            self._rules_toggle_btn = toggle_btn
            toggle_btn.setStyleSheet(
                "QPushButton{background:#222;color:#aaa;border:none;"
                "text-align:left;padding:2px 6px;font-size:8pt;}"
                "QPushButton:hover{color:#fff;}")
            toggle_btn.setFlat(True)
            lay.addWidget(toggle_btn)

            # ── ルール本文 ────────────────────────────────────────────────
            rules = QTextBrowser()
            self._rules_widget = rules
            rules.setReadOnly(True)
            rules.setOpenLinks(False)   # anchorClicked で手動処理

            # board.base_url = "https://may.2chan.net/b/"
            # board.url      = "https://may.2chan.net/b/futaba.htm"
            _base = getattr(board, 'base_url', None) or ''
            # origin = "https://may.2chan.net"
            _origin = _base.rstrip('/').rsplit('/', 1)[0] if _base else ''

            def _to_abs(href: str) -> str:
                """相対URLを絶対URLに補完"""
                if href.startswith(('http://', 'https://')):
                    return href
                if href.startswith('//'):
                    return 'https:' + href
                if href.startswith('/'):
                    return _origin + href   # /b/futaba.php?... → https://may.2chan.net/b/futaba.php?...
                return _base + href         # 相対パス → base_url + href

            def _open_link(url):
                s = _to_abs(url.toString())
                from PySide6.QtCore import QUrl
                QDesktopServices.openUrl(QUrl(s))

            rules.anchorClicked.connect(_open_link)

            # HTML優先（リンクを保持）、なければプレーンテキスト
            if rules_html:
                # href の全パターンを絶対URLに補完
                def _fix_href(m):
                    return f'href="{_to_abs(m.group(1))}"'
                fixed_html = _re.sub(r'href="([^"]*)"', _fix_href, rules_html)
                styled = (
                    "<style>"
                    "body{background:#000;color:#fff;font-size:8pt;margin:0;padding:0;}"
                    "a{color:#88ccff;}"
                    "li{margin:1px 0;list-style:none;}"
                    "ul{margin:0;padding:2px 4px;}"
                    "</style>"
                    + fixed_html
                )
                rules.setHtml(styled)
            else:
                rules.setPlainText(rules_txt)

            rules.setStyleSheet(
                "QTextBrowser{background:#000;color:#fff;font-size:8pt;"
                "border:none;padding:4px 6px;}")
            rules.setFixedHeight(90)
            rules.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            # デフォルト状態を反映（閉じ or 開き）
            rules.setVisible(_rules_open)
            lay.addWidget(rules)

            # トグル接続（状態を settings に保存）
            def _on_toggle(checked, _key=_rules_key):
                rules.setVisible(checked)
                toggle_btn.setText("▼ 板の注意事項" if checked else "▶ 板の注意事項")
                settings.post_rules_open[_key] = checked
                settings.save()
            toggle_btn.toggled.connect(_on_toggle)

        # ── 削除キー行 ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addWidget(QLabel("削除キー:"))
        self._key = QLineEdit(getattr(settings, "delete_key", ""))
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setFixedWidth(120)
        btn_row.addWidget(self._key)
        btn_row.addWidget(QLabel("（削除用 英数字で8字以内）"))
        btn_row.addStretch()
        if not is_new_thread:
            self._chk_scroll_bottom = QCheckBox("投稿後に最下部へ")
            self._chk_scroll_bottom.setToolTip("投稿成功後にスレッドの最下部へスクロールする")
            self._chk_scroll_bottom.setChecked(getattr(settings, 'scroll_after_post', True))
            btn_row.addWidget(self._chk_scroll_bottom)
        lay.addLayout(btn_row)

    # ── 手書きjsタブ ────────────────────────────────────────────────────────
    def _build_tegaki_tab(self) -> QWidget:
        """手書きjs入力エリア（HTML5 Canvas）"""
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEngineProfile
        _BG   = "#EFDFD6"
        _PEN  = "#7B0004"
        _SIZE = 5
        _W, _H = 344, 135
        _html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;width:100%;height:100%;background:#d0d0d0;
             display:flex;flex-direction:column;overflow:hidden;}}
  #toolbar{{padding:3px 6px;background:#f0f0f0;border-bottom:1px solid #ccc;
            display:flex;gap:4px;align-items:center;flex-wrap:wrap;flex-shrink:0;}}
  #toolbar button{{padding:2px 7px;font-size:11px;cursor:pointer;border:1px solid #aaa;
                   background:#e8e8e8;border-radius:3px;}}
  #toolbar button.active{{background:#b0c8e8;border-color:#5588cc;font-weight:bold;}}
  #toolbar input[type=color]{{width:30px;height:22px;padding:0;border:1px solid #888;cursor:pointer;}}
  #toolbar input[type=range]{{width:64px;vertical-align:middle;}}
  #toolbar select{{font-size:11px;padding:1px 2px;}}
  #toolbar span.lbl{{font-size:11px;}}
  #canvas-wrap{{flex:1;overflow:auto;display:flex;justify-content:center;align-items:center;
                padding:8px;box-sizing:border-box;}}
  #canvas-scaler{{display:inline-flex;justify-content:center;align-items:center;
                  transform-origin:center center;}}
  #canvas{{display:block;border:1px solid #aaa;}}
</style></head><body>
<div id="toolbar">
  <button id="btn_pen"    onclick="setTool('pen')"   class="active">✏️ペン</button>
  <button id="btn_eraser" onclick="setTool('eraser')">🧹消しゴム</button>
  <input type="color" id="color" value="{_PEN}" title="色">
  <span class="lbl">太さ:</span>
  <input type="range" id="size" min="1" max="30" value="{_SIZE}" oninput="syncSize()">
  <span id="size-label" style="font-size:11px;min-width:18px;text-align:right;">{_SIZE}</span>
  <span class="lbl" style="margin-left:4px;">手ブレ補正:</span>
  <input type="range" id="smooth" min="0" max="12" value="0" step="1" title="手ブレ補正（平均点数）">
  <span id="smooth-label" style="font-size:11px;min-width:14px;">0</span>
  <span class="lbl" style="margin-left:4px;">ペン先:</span>
  <select id="cursor-sel" onchange="updateCursor()">
    <option value="cross_thick" selected>十字(太)</option>
    <option value="cross_thin">十字(細)</option>
    <option value="dot">・</option>
    <option value="none">無し</option>
  </select>
  <button onclick="undo()">↩元に戻す</button>
  <button onclick="redo()">↪やり直す</button>
  <button onclick="flipCanvas()">↔反転</button>
  <button onclick="clearCanvas()">🗑クリア</button>
  <span class="lbl" style="margin-left:4px;">W:</span>
  <input type="number" id="cw" value="{_W}" min="1" max="2000" style="width:44px;font-size:11px;">
  <span class="lbl">H:</span>
  <input type="number" id="ch" value="{_H}" min="1" max="2000" style="width:44px;font-size:11px;">
  <button onclick="resizeCanvas()">適用</button>
  <button onclick="zoom(-0.25)">🔍−</button>
  <span id="zoom-label" style="font-size:11px;min-width:34px;text-align:center;">100%</span>
  <button onclick="zoom(+0.25)">🔍＋</button>
</div>
<div id="canvas-wrap">
  <div id="canvas-scaler">
    <canvas id="canvas" width="{_W}" height="{_H}"></canvas>
  </div>
</div>
<script>
const canvas  = document.getElementById('canvas');
const scaler  = document.getElementById('canvas-scaler');
const wrap    = document.getElementById('canvas-wrap');
const ctx     = canvas.getContext('2d');
const BG      = '{_BG}';
let tool='pen', drawing=false;
let history=[], future=[];
let scale=1.0;
let rawBuf=[], smX=0, smY=0;

function fillBg(){{
  ctx.fillStyle=BG;
  ctx.fillRect(0,0,canvas.width,canvas.height);
}}
fillBg();

function syncSize(){{
  document.getElementById('size-label').textContent=
    document.getElementById('size').value;
  updateCursor();
}}
document.getElementById('smooth').addEventListener('input',function(){{
  document.getElementById('smooth-label').textContent=this.value;
}});

// ── ペン先カーソル ──
function makeCursorSvg(type, sz){{
  let w,h,svg;
  if(type==='dot'){{
    // ペンの太さに合わせた丸
    const r=Math.max(Math.round(sz/2),2);
    w=r*2+2; h=r*2+2;
    const cx=r+1,cy=r+1;
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+w+'" height="'+h+'">'
      +'<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="#000" stroke-width="1.5"/>'
      +'<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="#fff" stroke-width="0.6" stroke-dasharray="2,2"/>'
      +'</svg>';
    return 'url("data:image/svg+xml,'+encodeURIComponent(svg)+'") '+cx+' '+cy+', crosshair';
  }} else if(type==='cross_thick'){{
    w=21; h=21; const c=10;
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="21" height="21">'
      +'<line x1="'+c+'" y1="0" x2="'+c+'" y2="21" stroke="#fff" stroke-width="3"/>'
      +'<line x1="0" y1="'+c+'" x2="21" y2="'+c+'" stroke="#fff" stroke-width="3"/>'
      +'<line x1="'+c+'" y1="0" x2="'+c+'" y2="21" stroke="#000" stroke-width="1.5"/>'
      +'<line x1="0" y1="'+c+'" x2="21" y2="'+c+'" stroke="#000" stroke-width="1.5"/>'
      +'</svg>';
    return 'url("data:image/svg+xml,'+encodeURIComponent(svg)+'") '+c+' '+c+', crosshair';
  }} else if(type==='cross_thin'){{
    w=15; h=15; const c=7;
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15">'
      +'<line x1="'+c+'" y1="0" x2="'+c+'" y2="15" stroke="#fff" stroke-width="2"/>'
      +'<line x1="0" y1="'+c+'" x2="15" y2="'+c+'" stroke="#fff" stroke-width="2"/>'
      +'<line x1="'+c+'" y1="0" x2="'+c+'" y2="15" stroke="#000" stroke-width="1"/>'
      +'<line x1="0" y1="'+c+'" x2="15" y2="'+c+'" stroke="#000" stroke-width="1"/>'
      +'</svg>';
    return 'url("data:image/svg+xml,'+encodeURIComponent(svg)+'") '+c+' '+c+', crosshair';
  }} else {{
    return 'default';
  }}
}}

function updateCursor(){{
  const type=document.getElementById('cursor-sel').value;
  const sz=+document.getElementById('size').value;
  canvas.style.cursor=makeCursorSvg(type,sz);
}}
updateCursor();

// ── ツール選択 ──
function setTool(t){{
  tool=t;
  document.getElementById('btn_pen').classList.toggle('active', t==='pen');
  document.getElementById('btn_eraser').classList.toggle('active', t==='eraser');
}}

// ── ズーム（常に中央） ──
function zoom(delta){{
  scale=Math.max(0.25,Math.min(4.0, Math.round((scale+delta)*100)/100));
  scaler.style.transform='scale('+scale+')';
  scaler.style.width =(canvas.width *scale)+'px';
  scaler.style.height=(canvas.height*scale)+'px';
  document.getElementById('zoom-label').textContent=Math.round(scale*100)+'%';
}}

// ── キャンバスリサイズ ──
function resizeCanvas(){{
  const nw=parseInt(document.getElementById('cw').value)||{_W};
  const nh=parseInt(document.getElementById('ch').value)||{_H};
  const tmp=document.createElement('canvas');
  tmp.width=canvas.width; tmp.height=canvas.height;
  tmp.getContext('2d').drawImage(canvas,0,0);
  canvas.width=nw; canvas.height=nh;
  fillBg();
  ctx.drawImage(tmp,0,0);
  history=[]; future=[];
  zoom(0);
}}

// ── 反転 ──
function flipCanvas(){{
  saveSnap();
  const tmp=document.createElement('canvas');
  tmp.width=canvas.width; tmp.height=canvas.height;
  const tc=tmp.getContext('2d');
  tc.translate(canvas.width,0); tc.scale(-1,1);
  tc.drawImage(canvas,0,0);
  ctx.drawImage(tmp,0,0);
}}

// ── 座標変換 ──
function getPos(e){{
  const r=canvas.getBoundingClientRect();
  return[(e.clientX-r.left)/scale,(e.clientY-r.top)/scale];
}}

// ── ポインタキャプチャで範囲外描画 ──
canvas.addEventListener('pointerdown', start);
window.addEventListener('pointermove', move);
window.addEventListener('pointerup',   end);
canvas.addEventListener('touchstart',  startT, {{passive:false}});
window.addEventListener('touchmove',   moveT,  {{passive:false}});
window.addEventListener('touchend',    end);

function saveSnap(){{
  history.push(ctx.getImageData(0,0,canvas.width,canvas.height));
  future=[];
  if(history.length>50) history.shift();
}}

function getColor(){{ return tool==='eraser'?BG:document.getElementById('color').value; }}
function getSize() {{ return +document.getElementById('size').value; }}

function start(e){{
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  drawing=true;
  saveSnap();
  const[x,y]=getPos(e);
  rawBuf=[{{x,y}}];
  smX=x; smY=y;
  ctx.beginPath(); ctx.moveTo(x,y);
  ctx.strokeStyle=getColor(); ctx.lineWidth=getSize(); ctx.lineCap='round'; ctx.lineJoin='round';
}}

function move(e){{
  if(!drawing)return;
  const[rx,ry]=getPos(e);
  const smooth=+document.getElementById('smooth').value;
  rawBuf.push({{x:rx,y:ry}});
  let ax=rx,ay=ry;
  if(smooth>0){{
    const n=Math.min(smooth+1,rawBuf.length);
    ax=0; ay=0;
    for(let i=rawBuf.length-n;i<rawBuf.length;i++){{ax+=rawBuf[i].x;ay+=rawBuf[i].y;}}
    ax/=n; ay/=n;
  }}
  ctx.lineTo(ax,ay); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(ax,ay);
  ctx.strokeStyle=getColor(); ctx.lineWidth=getSize(); ctx.lineCap='round'; ctx.lineJoin='round';
  smX=ax; smY=ay;
  if(rawBuf.length>200) rawBuf=rawBuf.slice(-100);
}}

function end(){{
  if(!drawing)return;
  drawing=false;
  rawBuf=[];
}}

function startT(e){{e.preventDefault();start(e.touches[0]);}}
function moveT(e){{e.preventDefault();move(e.touches[0]);}}

function undo(){{
  if(!history.length)return;
  future.push(ctx.getImageData(0,0,canvas.width,canvas.height));
  ctx.putImageData(history.pop(),0,0);
}}
function redo(){{
  if(!future.length)return;
  history.push(ctx.getImageData(0,0,canvas.width,canvas.height));
  ctx.putImageData(future.pop(),0,0);
}}
function clearCanvas(){{saveSnap();fillBg();}}

// ── キーボードショートカット ──
document.addEventListener('keydown',function(e){{
  if(e.ctrlKey||e.metaKey){{
    if(e.key==='z'||e.key==='Z'){{
      if(e.shiftKey){{redo();}} else {{undo();}}
      e.preventDefault();
    }} else if(e.key==='y'||e.key==='Y'){{
      redo(); e.preventDefault();
    }}
  }}
}});
</script></body></html>"""

        self._tegaki_profile = QWebEngineProfile(self)  # off-the-record
        from PySide6.QtWebEngineCore import QWebEnginePage
        self._tegaki_page = QWebEnginePage(self._tegaki_profile, self)
        page = self._tegaki_page
        w = QWebEngineView(page, self)
        w.setHtml(_html)
        return w

    def _on_comment_tab_changed(self, idx: int):
        """サイドバータブ切り替え → コンテンツスタックを切り替え"""
        prev_idx = self._content_stack.currentIndex()
        self._content_stack.setCurrentIndex(idx)
        if idx == 1:
            # コメント→手書きに切り替えた時、手書き由来の_img_pathをクリア（再編集対応）
            if self._img_is_tmp and self._img_path and \
                    self._img_edit.text().startswith("[手書き]"):
                self._cleanup_tmp()
                self._img_path = ""
                self._img_is_tmp = False
                self._img_edit.clear()
                self._clear_btn.hide()
                self._preview_lbl.clear_preview()
                self._preview_lbl.setText(
                    "Ctrl+V でクリップボード画像を貼付けするとここにプレビューが表示されます")
                self._preview_lbl.setStyleSheet(
                    f"background:{_TM.ui('panel_bg2','#2a2a2a')};color:{_TM.ui('text_muted','#888')};border:1px solid {_TM.ui('panel_border','#444')};"
                    "border-radius:4px;font-size:9pt;padding:8px;")
        if idx == 0:
            QTimer.singleShot(0, self._comment.setFocus)
            # 手書きタブ→コメントタブ切り替え時にcanvasをPNG化して_img_pathにセット
            if prev_idx == 1:
                self._capture_tegaki_to_img()

    def _capture_tegaki_to_img(self, on_done=None):
        """手書きcanvasの内容をPNG一時ファイルにして_img_pathにセットする。
        on_done: 完了後に呼ぶコールバック（省略可）
        """
        if page := getattr(self, "_tegaki_page", None):
            pass
        else:
            if on_done: on_done()
            return
        def _on_dataurl(data_url):
            if not (data_url and data_url.startswith("data:image/png;base64,")):
                if on_done: on_done()
                return
            import base64 as _b64, tempfile as _tf, re as _re, datetime as _dt, sys as _sys
            try:
                raw = _b64.b64decode(data_url[len("data:image/png;base64,"):])
                tf = _tf.NamedTemporaryFile(suffix=".png", delete=False)
                tf.write(raw); tf.close()
                self._cleanup_tmp()
                self._img_path   = tf.name
                self._img_is_tmp = True
                img_q = QImage()
                if img_q.loadFromData(raw):
                    sz = img_q.width(), img_q.height()
                    self._img_edit.setText(f"[手書き] {sz[0]}x{sz[1]} .png")
                    self._clear_btn.show()
                    pix = QPixmap.fromImage(img_q)
                    self._preview_lbl.set_preview(pix)
                    self._preview_lbl.setStyleSheet(
                        f"background:{_TM.ui('window_bg','#111')};border:1px solid {_TM.ui('input_border','#555')};"
                        "border-radius:4px;padding:4px;")
                # ── logsへ保存 ────────────────────────────────────────
                try:
                    import os as _os
                    _log_dir = getattr(self._settings, "log_save_dir", "").strip()
                    if not _log_dir:
                        _base = _os.path.dirname(_os.path.abspath(_sys.argv[0]))
                        _log_dir = _os.path.join(_base, "logs")
                    _os.makedirs(_log_dir, exist_ok=True)
                    _tpl = getattr(self._settings, "log_filename_template", "{date}/{date}_No.{no}_{title}")
                    _now = _dt.datetime.now()
                    _board_name = _re.sub(r'[\\/:*?"<>|]', '',
                        getattr(self._board, "name", "") or "" if self._board else "")
                    # {逆NG}/{逆NG:代替文字} を解決（手書き保存は逆NGマッチ無し）
                    def _rv_hw(m):
                        _d = m.group(1)
                        _v = _d if _d is not None else ""
                        return _v.replace("{", "{{").replace("}", "}}")
                    _tpl = _re.sub(r'\{(?:逆NG|revng)(?::([^}]*))?\}', _rv_hw, _tpl)
                    _fname = _tpl.format(
                        no=self._resto or 0, title="", board=_board_name,
                        date=_now.strftime("%Y%m%d"), time=_now.strftime("%H%M%S"),
                        datetime=_now.strftime("%Y%m%d_%H%M%S"),
                    )
                    _fname = _re.sub(r'[\\/:*?"<>|]', '', _fname).strip("_. ")
                    with open(_os.path.join(_log_dir, f"{_fname}.png"), "wb") as _fp:
                        _fp.write(raw)
                except Exception:
                    pass
            except Exception as e:
                QMessageBox.warning(self, "手書き画像エラー",
                                    f"手書き画像の変換に失敗しました:\n{e}")
                if on_done: on_done()
                return
            if on_done: on_done()
        self._tegaki_view.page().runJavaScript(
            "document.getElementById('canvas').toDataURL('image/png');",
            _on_dataurl)

    # ── 形式切り替え ────────────────────────────────────────────────────────
    def _on_fmt_changed(self, fmt: str):
        visible = fmt.lower() == "jpg"
        self._lbl_quality.setVisible(visible)
        self._spin_quality.setVisible(visible)

    def _regenerate_clip_image(self, _=None):
        """品質・形式変更時にクリップボード由来の一時ファイルを再生成する。"""
        if self._clip_image is None:
            return
        import tempfile as _tf, os as _os
        fmt     = self._clip_fmt.currentText().lower()
        quality = self._spin_quality.value() if fmt == "jpg" else -1
        try:
            tf = _tf.NamedTemporaryFile(suffix=f".{fmt}", delete=False)
            tmp = tf.name; tf.close()
            ok = self._clip_image.save(tmp, fmt.upper(), quality)
            if not ok:
                _os.unlink(tmp)
                return
        except Exception:
            return
        # 旧一時ファイル削除 → 差し替え
        self._cleanup_tmp()
        self._img_path   = tmp
        self._img_is_tmp = True
        sz = self._clip_image.width(), self._clip_image.height()
        self._img_edit.setText(f"[クリップボード] {sz[0]}x{sz[1]} .{fmt}")

    # ── クリップボードから貼り付けボタン ─────────────────────────────────────
    def _paste_from_clipboard_btn(self):
        cb = QApplication.clipboard()
        img = cb.image()
        if not img.isNull():
            self._paste_clipboard_image(img)
        else:
            QMessageBox.information(self, "貼り付け", "クリップボードに画像がありません。")

    # ── 画像クリア ──────────────────────────────────────────────────────────
    def _clear_image(self):
        self._cleanup_tmp()
        self._img_path   = ""
        self._img_is_tmp = False
        self._clip_image = None
        self._img_edit.clear()
        self._clear_btn.hide()   # 添付なし → ×解除を非表示
        self._preview_lbl.clear_preview()
        self._preview_lbl.setText("📋 クリップボードから貼り付け または 参照でファイルを選択")
        self._preview_lbl.setStyleSheet(
            f"background:{_TM.ui('panel_bg2','#2a2a2a')};color:{_TM.ui('text_muted','#888')};border:1px solid {_TM.ui('panel_border','#444')};"
            "border-radius:4px;font-size:9pt;padding:8px;")

    def _cleanup_tmp(self):
        if self._img_is_tmp and self._img_path:
            try:
                import os as _os; _os.unlink(self._img_path)
            except Exception:
                pass

    # ── Ctrl+V クリップボード貼付け ─────────────────────────────────────────
    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_V and
                event.modifiers() == Qt.KeyboardModifier.ControlModifier):
            clipboard = QApplication.clipboard()
            img = clipboard.image()
            if not img.isNull():
                self._paste_clipboard_image(img)
                return
        super().keyPressEvent(event)

    def _paste_clipboard_image(self, img):
        import tempfile as _tf
        fmt     = self._clip_fmt.currentText().lower()
        quality = self._spin_quality.value() if fmt == "jpg" else -1
        try:
            tf = _tf.NamedTemporaryFile(suffix=f".{fmt}", delete=False)
            tmp = tf.name; tf.close()
            ok = img.save(tmp, fmt.upper(), quality)
            if not ok:
                raise RuntimeError("save failed")
        except Exception as e:
            QMessageBox.warning(self, "貼付けエラー",
                                f"クリップボードの画像を保存できませんでした:\n{e}")
            return
        # 前の一時ファイルを削除してから差し替え
        self._cleanup_tmp()
        self._img_path   = tmp
        self._img_is_tmp = True
        self._clip_image = img   # 品質/形式変更時に再生成するため元画像を保持
        sz = img.width(), img.height()
        self._img_edit.setText(f"[クリップボード] {sz[0]}x{sz[1]} .{fmt}")
        self._clear_btn.show()   # 添付あり → ×解除を表示
        # プレビュー表示（大きめに表示、アスペクト比を維持）
        pix = QPixmap.fromImage(img)
        self._preview_lbl.set_preview(pix)
        self._preview_lbl.setStyleSheet(
            f"background:{_TM.ui('window_bg','#111')};border:1px solid {_TM.ui('input_border','#555')};border-radius:4px;padding:4px;")
        # 形式・品質を記憶
        self._settings.post_img_format  = fmt
        if fmt == "jpg":
            self._settings.post_img_quality = quality
        self._settings.save()

    def _browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "画像を選択", "",
            "画像/動画 (*.jpg *.jpeg *.png *.gif *.webp *.webm *.mp4);;全て (*)")
        if path:
            self._set_file_path(path)

    def _set_file_path(self, path: str):
        """ファイルパスを添付ファイルに設定する共通処理（参照ボタン・D&D共用）"""
        self._cleanup_tmp()
        self._img_path   = path
        self._img_is_tmp = False
        self._clip_image = None
        self._img_edit.setText(path)
        self._clear_btn.show()
        # 画像なら簡易プレビュー表示
        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
        if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
            pix = QPixmap(path)
            if not pix.isNull():
                self._preview_lbl.set_preview(pix)
                self._preview_lbl.setStyleSheet(
                    f"background:{_TM.ui('window_bg','#111')};border:1px solid {_TM.ui('input_border','#555')};border-radius:4px;padding:4px;")
                return
        # 動画や画像以外はテキスト表示
        import os as _os
        size_kb = _os.path.getsize(path) // 1024 if _os.path.exists(path) else 0
        self._preview_lbl.clear()
        self._preview_lbl.setText(f"📎 {_os.path.basename(path)}  ({size_kb} KB)")
        self._preview_lbl.setStyleSheet(
            "background:#1a2a1a;color:#8f8;border:1px solid #555;"
            "border-radius:4px;font-size:9pt;padding:8px;")

    def _on_img_edit_finished(self):
        """添付File欄に直接入力されたパスを反映する。
        実在するファイルなら _set_file_path で添付＋プレビュー表示。
        空なら添付解除。存在しない／クリップボード表示文字列は無視。"""
        import os as _os
        text = self._img_edit.text().strip()
        # クリップボード貼付け由来の表示文字列（[クリップボード] ...）はパスではない
        if text.startswith("[クリップボード]"):
            return
        # 入力なし → 添付があれば解除
        if not text:
            if self._img_path:
                self._clear_image()
            return
        # エクスプローラ等からのコピーで前後に付く引用符を除去
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1].strip()
        # すでに同じパスが反映済みなら何もしない（editingFinished二重発火対策）
        if text == self._img_path:
            return
        if _os.path.isfile(text):
            self._set_file_path(text)
        # 存在しないパスはプレビューを変えず放置（投稿時に別途チェックされる）

    # ── ドラッグ&ドロップ ───────────────────────────────────────────────────
    _DD_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'webm', 'mp4'}

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                p = u.toLocalFile() or u.toString()
                ext = p.rsplit('.', 1)[-1].lower().split('?')[0] if '.' in p else ''
                if ext in self._DD_EXTS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            u    = urls[0]
            path = u.toLocalFile()
            if path:
                ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
                if ext in self._DD_EXTS:
                    self._set_file_path(path)
                    event.acceptProposedAction()
                    return
            # ローカルファイルでない場合（スレ内画像のURL等）はスキップ
        event.ignore()

    def _post(self):
        text = self._comment.toPlainText().strip()
        # 記憶チェックの状態を保存（チェックONの時のみ値も保存）
        self._settings.post_save_name = self._chk_save_name.isChecked()
        self._settings.post_save_mail = self._chk_save_mail.isChecked()
        if self._chk_save_name.isChecked():
            self._settings.post_name = self._name.text()
        if self._chk_save_mail.isChecked():
            self._settings.post_mail = self._mail.text()
        self._settings.post_img_format  = self._clip_fmt.currentText()
        if self._clip_fmt.currentText() == "jpg":
            self._settings.post_img_quality = self._spin_quality.value()
        self._settings.post_dialog_pin = self._pin_btn.isChecked()
        self._settings.save()

        # 手書きタブ選択中 かつ _img_path 未設定 → Canvas内容をPNGに変換してから投稿
        _tegaki_page = getattr(self, "_tegaki_page", None)
        if self._content_stack.currentIndex() == 1 and not self._img_path and _tegaki_page:
            # キャプチャ完了後に投稿を実行するコールバックを渡す
            self._capture_tegaki_to_img(on_done=lambda: self._do_submit(text))
        else:
            self._do_submit(text)

    def _do_submit(self, text: str):
        """投稿スレッドを起動する（_postから分離）"""
        name, mail, sub, key, img = (
            self._name.text(), self._mail.text(),
            self._sub.text(), self._key.text(), self._img_path)
        self._btn_post.setEnabled(False)
        def _do():
            try:
                ok, msg, new_no = self._fetcher.post_res(
                    self._board, self._resto,
                    name=name, email=mail, subject=sub,
                    comment=text, image_path=img, delete_key=key)
            except Exception as _e:
                ok, msg, new_no = False, str(_e), 0
            self._result_signal.emit(ok, msg, new_no)
        threading.Thread(target=_do, daemon=True).start()

    def roll_up(self, title_hint: str = ""):
        """タブ切替時：コンテンツを隠してタイトルバーだけにする"""
        if getattr(self, "_rolled_up", False):
            return
        self._rolled_up   = True
        self._rolled_size = self.size()
        # このダイアログが返信しているスレのタイトルを表示
        thread_title = getattr(self, "_thread_title", "").strip()
        if thread_title:
            self.setWindowTitle(f"[返信] {thread_title}")
        else:
            base = f"{'返信' if self._resto else 'スレッド作成'} ─ {self._board.name}"
            self.setWindowTitle(f"[縮小] {base}")
        _lay = self.layout()
        for i in range(_lay.count()):
            item = _lay.itemAt(i)
            if item and item.widget():
                item.widget().hide()
        self.setFixedHeight(1)
        # roll_up中はchangeEventでウィンドウアクティブ化を検知してタブに通知
        self._roll_active = True
        # 注意事項が開いていれば閉じる（復元時に再度開かないよう状態も保存）
        if getattr(self, "_rules_widget", None) and getattr(self, "_rules_toggle_btn", None):
            if self._rules_toggle_btn.isChecked():
                self._rules_toggle_btn.setChecked(False)

    def roll_restore(self):
        """タブを戻したとき：コンテンツを復元する"""
        if not getattr(self, "_rolled_up", False):
            return
        self._rolled_up = False
        base = f"{'返信' if self._resto else 'スレッド作成'} ─ {self._board.name}"
        self.setWindowTitle(base)
        _lay = self.layout()
        _rules_w = getattr(self, "_rules_widget", None)
        for i in range(_lay.count()):
            item = _lay.itemAt(i)
            if item and item.widget():
                w = item.widget()
                # 注意事項本文は toggle_btn の状態に従う（強制showしない）
                if w is _rules_w:
                    continue
                w.show()
        self.setMaximumHeight(16777215); self.setMinimumHeight(0)
        sz = getattr(self, "_rolled_size", None)
        if sz:
            self.resize(sz)
        else:
            self.resize(580, 460)
        self._roll_active = False

    def _on_result(self, ok: bool, msg: str, new_thread_no: int = 0):
        self._btn_post.setEnabled(True)
        if ok:
            # プレビュー（サンプル）ウインドウが開いていれば一緒に閉じる
            _sw = getattr(self, "_sample_win", None)
            if _sw is not None:
                try:
                    _sw.close()
                except Exception:
                    pass
                self._sample_win = None
            if self._pin_btn.isChecked():
                # ピンON: 閉じずにコメント・画像をクリアして次の書き込みを待機
                self._comment.clear()
                self._clear_image()
            else:
                # ピンOFF: サイズを保存してからダイアログを閉じる
                self._save_geometry()
                self.accept()
            if self._on_success:
                _no = new_thread_no
                QTimer.singleShot(100, lambda _n=_no: self._on_success(_n))
            if getattr(self._settings, "pin_after_post", False):
                self.pin_after_post.emit()
            # 投稿後スクロール設定を保存してシグナル発火
            if self._chk_scroll_bottom is not None:
                _scroll = self._chk_scroll_bottom.isChecked()
                self._settings.scroll_after_post = _scroll
                self._settings.save()
                if _scroll:
                    self.scroll_after_post.emit()
        else:
            print(f"[PostDialog] 投稿失敗: {msg}")
            self._show_post_error(msg)

    @staticmethod
    def _parse_post_error_popup_css(settings) -> dict:
        """ユーザーCSSから .post-error-popup ブロックを読み取り色辞書を返す。
        キー: background, border-color, color（blink-background, blink-border-color も任意）
        見つからないキーはデフォルト値を返す。"""
        defaults = {
            "background":       "#EFDFD6",
            "border-color":     "#7B0004",
            "color":            "#7B0004",
            "blink-background": "#FFAAAA",
            "blink-border-color": "#7B0004",
        }
        try:
            css_file = getattr(settings, "user_css_file", "")
            if not css_file:
                return defaults
            from pathlib import Path as _Path
            p = _Path(css_file)
            if not p.is_absolute():
                import sys as _sys
                p = _Path(_sys.argv[0]).parent / p
            if not p.exists():
                return defaults
            css = p.read_text(encoding="utf-8")
            # .post-error-popup { ... } ブロックを抽出
            m = re.search(r'\.post-error-popup\s*\{([^}]*)\}', css, re.DOTALL)
            if not m:
                return defaults
            block = m.group(1)
            result = dict(defaults)
            for prop, val in re.findall(r'([\w-]+)\s*:\s*([^;]+)', block):
                key = prop.strip().lower()
                if key in result:
                    result[key] = val.strip()
            return result
        except Exception:
            return defaults

    def _show_post_error(self, msg: str):
        """投稿エラーを赤く2回点滅するポップアップで表示する。
        色はユーザーCSSの .post-error-popup セレクタから読み取る。"""
        c = self._parse_post_error_popup_css(self._settings)

        def _make_style(bg: str, border: str, fg: str) -> str:
            return (
                f"QDialog {{ background: {bg}; border: 2px solid {border}; border-radius: 6px; }}"
                f"QLabel  {{ color: {fg}; font-size: 13px; font-weight: bold; }}"
            )

        _NORMAL_STYLE = _make_style(c["background"],       c["border-color"],       c["color"])
        _BLINK_STYLE  = _make_style(c["blink-background"], c["blink-border-color"], c["color"])

        dlg = QDialog(self, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        dlg.setModal(False)

        lbl = QLabel(msg, dlg)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setContentsMargins(16, 12, 16, 12)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(lbl)

        dlg.setStyleSheet(_NORMAL_STYLE)
        dlg.adjustSize()

        # PostDialog の中央上部に配置
        parent_rect = self.geometry()
        x = parent_rect.x() + (parent_rect.width()  - dlg.width())  // 2
        y = parent_rect.y() + 60
        dlg.move(x, y)
        dlg.show()

        # 2回点滅: ON→OFF→ON→OFF → 通常色で確定
        _blink_state = [0]
        def _blink():
            _blink_state[0] += 1
            n = _blink_state[0]
            dlg.setStyleSheet(_BLINK_STYLE if n % 2 == 1 else _NORMAL_STYLE)
            if n < 4:
                QTimer.singleShot(300, _blink)

        QTimer.singleShot(200, _blink)

        # クリックで閉じる
        dlg.mousePressEvent = lambda _e: dlg.close()
        # 5秒後に自動クローズ
        QTimer.singleShot(5000, dlg.close)

    def append_quote(self, quote_text: str):
        """外部から引用テキストを追記する（ピンON中に別レスを引用した場合）"""
        if not quote_text:
            return
        cur_text = self._comment.toPlainText()
        if cur_text and not cur_text.endswith("\n"):
            quote_text = "\n" + quote_text
        self._comment.setPlainText(cur_text + quote_text)
        cur = self._comment.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self._comment.setTextCursor(cur)
        self._comment.setFocus()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        super().changeEvent(event)
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            if getattr(self, "_roll_active", False):
                self.activate_tab.emit(self._resto)

    def _open_sample_window(self):
        """プレビュー（サンプル）クリック → 別ウインドウで画像を表示"""
        pix = getattr(self._preview_lbl, "_orig_pix", None)
        if not pix or pix.isNull():
            return
        w = getattr(self, "_sample_win", None)
        if w is not None and w.isVisible():
            w.set_image(pix)
            w.raise_(); w.activateWindow()
            return
        self._sample_win = _SampleImageWindow(pix, self._settings, self)
        self._sample_win.show()

    def _save_geometry(self):
        """サイズ・位置を設定に保存（roll_up中は縮小前サイズを使う）"""
        if getattr(self, "_rolled_up", False):
            sz = getattr(self, "_rolled_size", None)
            if sz:
                self._settings.post_dialog_size = [sz.width(), sz.height()]
                self._settings.post_dialog_pos  = [self.x(), self.y()]
        else:
            self._settings.post_dialog_size = [self.width(), self.height()]
            self._settings.post_dialog_pos  = [self.x(), self.y()]

    def closeEvent(self, event):
        # サイズ・位置・分割位置を記憶
        self._save_geometry()
        # ピン状態を保存（投稿せずバツボタンで閉じても現在のON/OFFを反映する）
        if hasattr(self, "_pin_btn"):
            self._settings.post_dialog_pin = self._pin_btn.isChecked()
        if hasattr(self, "_preview_splitter"):
            self._settings.post_dialog_splitter = \
                self._preview_splitter.saveState().toHex().data().decode()
        if hasattr(self, "_preview_folder_splitter"):
            self._settings.post_dialog_splitter2 = \
                self._preview_folder_splitter.saveState().toHex().data().decode()
        self._settings.save()
        # キャンセル時に一時ファイルを削除
        self._cleanup_tmp()
        # WebEngineProfile より先に Page・View を破棄しないと警告が出るため明示的に削除
        if hasattr(self, '_tegaki_page') and self._tegaki_page is not None:
            self._tegaki_page.deleteLater()
            self._tegaki_page = None
        if hasattr(self, '_tegaki_profile') and self._tegaki_profile is not None:
            self._tegaki_profile.deleteLater()
            self._tegaki_profile = None
        super().closeEvent(event)


_REVERSE_NG_HELP_TEXT = (
    "カタログを更新した時に NG の設定と一致するスレッドを\n"
    "ピップアップしてくれる機能です\n\n"
    "仕様\n"
    "・一度にピップアップ出来るスレッド数は設定値まで（デフォルト99件）\n"
    "  (4つ以上ある場合は次の更新時にピップアップされます)\n"
    "・一度ピップアップされたスレッドは反応しません\n"
    "・一度開いた事のあるスレッドにも反応しません\n"
    "・NGでは無いのでNG処理はされません ご安心を...\n"
    "・カタログのみ適用されます"
)


class _ReverseNgHelpLabel(QLabel):
    """「逆NGって何？」リンクラベル — クリック or 1秒ホバーでポップアップ表示"""
    def __init__(self, parent=None):
        super().__init__('<a href="#">逆NGって何？</a>', parent)
        self.setOpenExternalLinks(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._show_popup)
        self.linkActivated.connect(lambda _: self._show_popup())

    def _show_popup(self):
        from PySide6.QtWidgets import QToolTip
        from PySide6.QtCore import QPoint
        pos = self.mapToGlobal(QPoint(0, self.height()))
        QToolTip.showText(pos, _REVERSE_NG_HELP_TEXT, self, self.rect(), 8000)

    def enterEvent(self, event):
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._timer.stop()
        super().leaveEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
# NG画像一括修正ダイアログ
# ══════════════════════════════════════════════════════════════════════════════

class NgImageBulkEditDialog(QDialog):
    """複数NG画像の一括修正ダイアログ。
    チェックした項目だけを選択中の全エントリに上書き適用する。"""

    def __init__(self, entries: list[dict], parent=None):
        super().__init__(parent)
        self._entries = entries
        self.setWindowTitle(f"NG画像の一括修正 ({len(entries)} 件)")
        self.resize(380, 280)
        self._result: list[dict] | None = None
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        note = QLabel(f"☑ チェックした項目を {len(self._entries)} 件まとめて変更します。\n"
                      "チェックしていない項目は変更されません。")
        note.setStyleSheet("font-size:9pt;color:#555;")
        note.setWordWrap(True)
        lay.addWidget(note)

        # ── NG対象 ──────────────────────────────────────────────────
        self._chk_type = QCheckBox("NG対象を変更する")
        lay.addWidget(self._chk_type)
        type_row = QHBoxLayout(); type_row.addSpacing(20)
        self._rb_ng  = QRadioButton("NG")
        self._rb_rev = QRadioButton("逆NG")
        self._rb_ng.setChecked(True)
        tg = QButtonGroup(self); tg.addButton(self._rb_ng); tg.addButton(self._rb_rev)
        type_row.addWidget(self._rb_ng); type_row.addWidget(self._rb_rev); type_row.addStretch()
        lay.addLayout(type_row)

        # ── 有効/無効 ───────────────────────────────────────────────
        self._chk_enabled = QCheckBox("有効/無効を変更する")
        lay.addWidget(self._chk_enabled)
        en_row = QHBoxLayout(); en_row.addSpacing(20)
        self._rb_enabled  = QRadioButton("有効")
        self._rb_disabled = QRadioButton("無効")
        self._rb_enabled.setChecked(True)
        eg = QButtonGroup(self); eg.addButton(self._rb_enabled); eg.addButton(self._rb_disabled)
        en_row.addWidget(self._rb_enabled); en_row.addWidget(self._rb_disabled); en_row.addStretch()
        lay.addLayout(en_row)

        # ── 有効期限 ────────────────────────────────────────────────
        self._chk_expires = QCheckBox("有効期限を変更する")
        lay.addWidget(self._chk_expires)
        exp_row = QHBoxLayout(); exp_row.addSpacing(20)
        self._cmb_expires = _NoWheelComboBox()
        self._cmb_expires.addItems(["無制限", "1日", "3日", "7日", "14日", "30日"])
        self._cmb_expires.setFixedWidth(100)
        exp_row.addWidget(self._cmb_expires); exp_row.addStretch()
        lay.addLayout(exp_row)

        # ── 表示モード ──────────────────────────────────────────────
        self._chk_hide_mode = QCheckBox("表示モードを変更する")
        lay.addWidget(self._chk_hide_mode)
        hm_row = QHBoxLayout(); hm_row.addSpacing(20)
        self._rb_hide_image = QRadioButton("画像のみ透明")
        self._rb_hide_res   = QRadioButton("レス全体を非表示")
        self._rb_hide_image.setChecked(True)
        hm_grp = QButtonGroup(self)
        hm_grp.addButton(self._rb_hide_image); hm_grp.addButton(self._rb_hide_res)
        hm_row.addWidget(self._rb_hide_image); hm_row.addWidget(self._rb_hide_res); hm_row.addStretch()
        lay.addLayout(hm_row)

        # 連動
        def _sync():
            for w in [self._rb_ng, self._rb_rev]:
                w.setEnabled(self._chk_type.isChecked())
            for w in [self._rb_enabled, self._rb_disabled]:
                w.setEnabled(self._chk_enabled.isChecked())
            self._cmb_expires.setEnabled(self._chk_expires.isChecked())
            for w in [self._rb_hide_image, self._rb_hide_res]:
                w.setEnabled(self._chk_hide_mode.isChecked())
        _sync()
        for chk in [self._chk_type, self._chk_enabled, self._chk_expires, self._chk_hide_mode]:
            chk.toggled.connect(lambda _: _sync())

        lay.addStretch()
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル(C)")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        import datetime, copy
        _exp_days = {"無制限": 0, "1日": 1, "3日": 3, "7日": 7, "14日": 14, "30日": 30}
        result = []
        for entry in self._entries:
            e = copy.deepcopy(entry)
            if self._chk_type.isChecked():
                e["is_reverse_ng"] = self._rb_rev.isChecked()
            if self._chk_enabled.isChecked():
                e["enabled"] = self._rb_enabled.isChecked()
            if self._chk_expires.isChecked():
                days = _exp_days.get(self._cmb_expires.currentText(), 0)
                e["expires"] = self._cmb_expires.currentText()
                e["expires_at"] = (
                    (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
                    if days > 0 else ""
                )
            if self._chk_hide_mode.isChecked():
                e["hide_mode"] = "res" if self._rb_hide_res.isChecked() else "image"
            result.append(e)
        self._result = result
        self.accept()

    def get_result(self) -> list[dict] | None:
        return self._result


# ══════════════════════════════════════════════════════════════════════════════
# NGワード一括修正ダイアログ
# ══════════════════════════════════════════════════════════════════════════════

class NgWordBulkEditDialog(QDialog):
    """複数NGワードの一括修正ダイアログ。
    チェックした項目だけを選択中の全エントリに上書き適用する。"""

    def __init__(self, entries: list[dict], parent=None):
        super().__init__(parent)
        self._entries = entries
        self.setWindowTitle(f"NGワードの一括修正 ({len(entries)} 件)")
        self.resize(420, 420)
        self._result: list[dict] | None = None
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        note = QLabel(f"☑ チェックした項目を {len(self._entries)} 件まとめて変更します。\n"
                      "チェックしていない項目は変更されません。")
        note.setStyleSheet("font-size:9pt;color:#555;")
        note.setWordWrap(True)
        lay.addWidget(note)

        # ── NGタイプ ────────────────────────────────────────────────
        self._chk_type = QCheckBox("NGタイプを変更する")
        lay.addWidget(self._chk_type)
        type_row = QHBoxLayout()
        type_row.addSpacing(20)
        self._type_grp = QButtonGroup(self)
        _types = [("NGワード", "ng"), ("逆NG", "reverse_ng"),
                  ("置換", "replace"), ("芝刈り置換", "mow_replace")]
        for i, (lbl, _) in enumerate(_types):
            rb = QRadioButton(lbl); self._type_grp.addButton(rb, i)
            type_row.addWidget(rb)
        self._type_grp.button(0).setChecked(True)
        lay.addLayout(type_row)

        # ── 有効/無効 ───────────────────────────────────────────────
        self._chk_enabled = QCheckBox("有効/無効を変更する")
        lay.addWidget(self._chk_enabled)
        en_row = QHBoxLayout(); en_row.addSpacing(20)
        self._rb_enabled  = QRadioButton("有効")
        self._rb_disabled = QRadioButton("無効")
        self._rb_enabled.setChecked(True)
        en_grp = QButtonGroup(self); en_grp.addButton(self._rb_enabled); en_grp.addButton(self._rb_disabled)
        en_row.addWidget(self._rb_enabled); en_row.addWidget(self._rb_disabled); en_row.addStretch()
        lay.addLayout(en_row)

        # ── 有効期限 ────────────────────────────────────────────────
        self._chk_expires = QCheckBox("有効期限を変更する")
        lay.addWidget(self._chk_expires)
        exp_row = QHBoxLayout(); exp_row.addSpacing(20)
        self._cmb_expires = _NoWheelComboBox()
        self._cmb_expires.addItems(["無制限", "1日", "3日", "7日", "14日", "30日"])
        self._cmb_expires.setFixedWidth(100)
        exp_row.addWidget(self._cmb_expires); exp_row.addStretch()
        lay.addLayout(exp_row)

        # ── 通知 ────────────────────────────────────────────────────
        self._chk_notify_chg = QCheckBox("通知設定を変更する")
        lay.addWidget(self._chk_notify_chg)
        ntf_row = QHBoxLayout(); ntf_row.addSpacing(20)
        self._rb_notify_on  = QRadioButton("通知する")
        self._rb_notify_off = QRadioButton("通知しない")
        self._rb_notify_off.setChecked(True)
        ntf_grp = QButtonGroup(self)
        ntf_grp.addButton(self._rb_notify_on); ntf_grp.addButton(self._rb_notify_off)
        self._cmb_notify_type = _NoWheelComboBox()
        self._cmb_notify_type.addItems(["効果音", "棒読みちゃん"])
        self._cmb_notify_type.setFixedWidth(110)
        ntf_row.addWidget(self._rb_notify_on); ntf_row.addWidget(self._rb_notify_off)
        ntf_row.addWidget(self._cmb_notify_type); ntf_row.addStretch()
        lay.addLayout(ntf_row)

        # ── 適用範囲 ────────────────────────────────────────────────
        self._chk_scope_chg = QCheckBox("適用範囲を変更する")
        lay.addWidget(self._chk_scope_chg)
        scope_box = QGroupBox(); scope_lay = QHBoxLayout(scope_box)
        scope_lay.setContentsMargins(20, 4, 4, 4)
        self._sc_body    = QCheckBox("レス本文")
        self._sc_name    = QCheckBox("名前")
        self._sc_subject = QCheckBox("題名")
        self._sc_mail    = QCheckBox("メール")
        self._sc_id      = QCheckBox("ID")
        self._sc_ip      = QCheckBox("IP/Host")
        self._sc_catalog = QCheckBox("カタログ")
        for w in [self._sc_body, self._sc_name, self._sc_subject,
                  self._sc_mail, self._sc_id, self._sc_ip, self._sc_catalog]:
            scope_lay.addWidget(w)
        scope_lay.addStretch()
        lay.addWidget(scope_box)

        # 各コントロールの有効/無効をチェックボックスと連動
        def _sync():
            for w in [self._type_grp.button(i) for i in range(4)]:
                w.setEnabled(self._chk_type.isChecked())
            for w in [self._rb_enabled, self._rb_disabled]:
                w.setEnabled(self._chk_enabled.isChecked())
            self._cmb_expires.setEnabled(self._chk_expires.isChecked())
            for w in [self._rb_notify_on, self._rb_notify_off, self._cmb_notify_type]:
                w.setEnabled(self._chk_notify_chg.isChecked())
            scope_box.setEnabled(self._chk_scope_chg.isChecked())
        _sync()
        for chk in [self._chk_type, self._chk_enabled, self._chk_expires,
                    self._chk_notify_chg, self._chk_scope_chg]:
            chk.toggled.connect(lambda _: _sync())

        lay.addStretch()
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル(C)")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        import datetime, copy
        _type_vals = ["ng", "reverse_ng", "replace", "mow_replace"]
        _exp_days  = {"無制限": 0, "1日": 1, "3日": 3, "7日": 7, "14日": 14, "30日": 30}
        _notify_types = ["sound", "bouyomi"]

        result = []
        for entry in self._entries:
            e = copy.deepcopy(entry)
            if self._chk_type.isChecked():
                e["ng_type"] = _type_vals[self._type_grp.checkedId()]
            if self._chk_enabled.isChecked():
                e["enabled"] = self._rb_enabled.isChecked()
            if self._chk_expires.isChecked():
                days = _exp_days.get(self._cmb_expires.currentText(), 0)
                e["expires_at"] = (
                    (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
                    if days > 0 else ""
                )
            if self._chk_notify_chg.isChecked():
                e["notify"]      = self._rb_notify_on.isChecked()
                e["notify_type"] = _notify_types[self._cmb_notify_type.currentIndex()]
            if self._chk_scope_chg.isChecked():
                e["scope_body"]    = self._sc_body.isChecked()
                e["scope_name"]    = self._sc_name.isChecked()
                e["scope_subject"] = self._sc_subject.isChecked()
                e["scope_mail"]    = self._sc_mail.isChecked()
                e["scope_id"]      = self._sc_id.isChecked()
                e["scope_ip"]      = self._sc_ip.isChecked()
                e["scope_catalog"] = self._sc_catalog.isChecked()
            result.append(e)
        self._result = result
        self.accept()

    def get_result(self) -> list[dict] | None:
        return self._result


# NGワード追加/修正ダイアログ
# ══════════════════════════════════════════════════════════════════════════════

class NgWordEditDialog(QDialog):
    """NGワードの追加・修正ダイアログ"""
    def __init__(self, ng_entry: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NGワードの追加" if ng_entry is None else "NGワードの修正")
        self.resize(440, 340)
        self._result: dict | None = None
        self._build(ng_entry or {})

    def _build(self, d: dict):
        lay = QVBoxLayout(self)

        # ── NGタイプ ラジオボタン ──────────────────────────────────
        type_lay = QHBoxLayout()
        self._type_grp = QButtonGroup(self)
        _types = [("NGワード", "ng"), ("逆NG", "reverse_ng"),
                  ("置換", "replace"), ("芝刈り置換", "mow_replace")]
        for i, (lbl, val) in enumerate(_types):
            rb = QRadioButton(lbl)
            self._type_grp.addButton(rb, i)
            type_lay.addWidget(rb)
        type_lay.addStretch()
        cur_type = d.get("ng_type", "ng")
        _type_map = {"ng": 0, "reverse_ng": 1, "replace": 2, "mow_replace": 3}
        btn = self._type_grp.button(_type_map.get(cur_type, 0))
        if btn: btn.setChecked(True)
        lay.addLayout(type_lay)

        # ── 適用範囲 ──────────────────────────────────────────────
        self._scope_box = QGroupBox("適用範囲")
        scope_grid = QHBoxLayout(self._scope_box)
        left_col = QVBoxLayout(); right_col = QVBoxLayout()

        self._chk_body    = QCheckBox("レス本文");    left_col.addWidget(self._chk_body)
        self._chk_mail    = QCheckBox("メール");      left_col.addWidget(self._chk_mail)
        self._chk_catalog = QCheckBox("カタログ");   left_col.addWidget(self._chk_catalog)

        # 右列ヘッダー行に「逆NGって何？」ヘルプラベル
        name_row = QHBoxLayout()
        self._chk_name = QCheckBox("名前/トリップ"); name_row.addWidget(self._chk_name)
        rev_help = _ReverseNgHelpLabel()
        name_row.addStretch(); name_row.addWidget(rev_help)
        right_col.addLayout(name_row)

        self._chk_id      = QCheckBox("ID");          right_col.addWidget(self._chk_id)
        sub_row = QHBoxLayout()
        self._chk_subject = QCheckBox("題名");        sub_row.addWidget(self._chk_subject)
        self._chk_ip      = QCheckBox("IP/Host");     sub_row.addWidget(self._chk_ip)
        right_col.addLayout(sub_row)

        scope_grid.addLayout(left_col); scope_grid.addLayout(right_col)
        lay.addWidget(self._scope_box)

        # デフォルト選択
        self._chk_body.setChecked(d.get("scope_body", True))
        self._chk_name.setChecked(d.get("scope_name", False))
        self._chk_subject.setChecked(d.get("scope_subject", False))
        self._chk_mail.setChecked(d.get("scope_mail", False))
        self._chk_id.setChecked(d.get("scope_id", False))
        self._chk_ip.setChecked(d.get("scope_ip", False))
        self._chk_catalog.setChecked(d.get("scope_catalog", False))

        # ── マッチングパターン ────────────────────────────────────
        form = QFormLayout()
        self._pattern_lbl = QLabel("マッチングパターン (正規表現)")
        self._pattern = QLineEdit(d.get("pattern", ""))
        self._pattern.setPlaceholderText("（正規表現）")
        form.addRow(self._pattern_lbl, self._pattern)

        self._replace_lbl = QLabel("置換文字列")
        self._replace = QLineEdit(d.get("replace_str", ""))
        form.addRow(self._replace_lbl, self._replace)
        lay.addLayout(form)

        # ── 有効期限 ──────────────────────────────────────────────
        exp_lay = QHBoxLayout()
        exp_lay.addWidget(QLabel("有効期限"))
        self._expires = QComboBox()
        self._expires.addItems(["無制限", "1日", "3日", "7日", "14日", "30日"])
        self._expires.setFixedWidth(100)
        cur_exp = d.get("expires", "無制限")
        idx = self._expires.findText(cur_exp)
        self._expires.setCurrentIndex(idx if idx >= 0 else 0)
        exp_lay.addWidget(self._expires); exp_lay.addStretch()
        lay.addLayout(exp_lay)

        # ── 通知 ──────────────────────────────────────────────────
        notify_lay = QHBoxLayout()
        self._chk_notify = QCheckBox("通知する")
        self._chk_notify.setChecked(d.get("notify", False))
        notify_lay.addWidget(self._chk_notify)
        self._cmb_notify_type = QComboBox()
        self._cmb_notify_type.addItems(["効果音", "棒読みちゃん"])
        ntype = d.get("notify_type", "sound")
        self._cmb_notify_type.setCurrentIndex(0 if ntype == "sound" else 1)
        self._cmb_notify_type.setFixedWidth(120)
        notify_lay.addWidget(self._cmb_notify_type)
        notify_lay.addStretch()
        lay.addLayout(notify_lay)

        def _update_notify_enabled():
            self._cmb_notify_type.setEnabled(self._chk_notify.isChecked())
        self._chk_notify.toggled.connect(_update_notify_enabled)
        _update_notify_enabled()

        lay.addStretch()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル(C)")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        # ── タイプ切り替え時のUI更新 ──────────────────────────────
        self._type_grp.idClicked.connect(lambda _: self._update_type_ui())
        self._update_type_ui()
        # 追加時はパターン欄にフォーカス
        QTimer.singleShot(0, self._pattern.setFocus)

    def _update_type_ui(self):
        idx = self._type_grp.checkedId()
        is_mow = (idx == 3)  # 芝刈り置換
        is_rep = (idx in (2, 3))  # 置換 or 芝刈り

        # 置換文字列: 置換/芝刈りで有効
        self._replace.setEnabled(is_rep)
        if is_mow and not self._replace.text():
            self._replace.setText(".")

        # 芝刈り置換: 適用範囲＋パターンをグレーアウト
        self._scope_box.setEnabled(not is_mow)
        self._pattern.setEnabled(not is_mow)
        self._pattern_lbl.setEnabled(not is_mow)
        if is_mow:
            self._chk_body.setChecked(True)

    def _ok(self):
        idx = self._type_grp.checkedId()
        is_mow = (idx == 3)

        pat = self._pattern.text().strip()
        # 芝刈り置換はパターン不要
        if not is_mow and not pat:
            QMessageBox.warning(self, "入力エラー", "マッチングパターンを入力してください")
            return

        _type_vals = ["ng", "reverse_ng", "replace", "mow_replace"]
        exp_text = self._expires.currentText()

        import datetime
        _exp_days = {"無制限": 0, "1日": 1, "3日": 3, "7日": 7, "14日": 14, "30日": 30}
        days = _exp_days.get(exp_text, 0)
        expires_at = ""
        if days > 0:
            expires_at = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

        _notify_types = ["sound", "bouyomi"]
        self._result = {
            "pattern":       pat,
            "is_regex":      True,
            "enabled":       True,
            "ng_type":       _type_vals[idx] if 0 <= idx < 4 else "ng",
            "scope_body":    True if is_mow else self._chk_body.isChecked(),
            "scope_name":    False if is_mow else self._chk_name.isChecked(),
            "scope_subject": False if is_mow else self._chk_subject.isChecked(),
            "scope_mail":    False if is_mow else self._chk_mail.isChecked(),
            "scope_id":      False if is_mow else self._chk_id.isChecked(),
            "scope_ip":      False if is_mow else self._chk_ip.isChecked(),
            "scope_catalog": False if is_mow else self._chk_catalog.isChecked(),
            "replace_str":   self._replace.text() if is_mow else self._replace.text(),
            "expires":       exp_text,
            "expires_at":    expires_at,
            "notify":        self._chk_notify.isChecked(),
            "notify_type":   _notify_types[self._cmb_notify_type.currentIndex()],
        }
        self.accept()

    def get_result(self) -> dict | None:
        return self._result


# ══════════════════════════════════════════════════════════════════════════════
# NG画像追加/修正ダイアログ
# ══════════════════════════════════════════════════════════════════════════════

class NgImageEditDialog(QDialog):
    """NG画像の追加・修正ダイアログ"""
    def __init__(self, ng_entry: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NG画像の追加" if ng_entry is None else "NG画像の修正")
        self.resize(400, 380)
        self._result: dict | None = None
        self._build(ng_entry or {})

    def _build(self, d: dict):
        import hashlib
        lay = QVBoxLayout(self)

        # ── 一致方法 ──────────────────────────────────────────────
        self._method_grp = QButtonGroup(self)
        self._rb_typesize = QRadioButton("画像の拡張子・幅・高さ・サイズと一致")
        self._rb_file     = QRadioButton("このファイルと一致")
        self._rb_md5      = QRadioButton("MD5ハッシュと一致")
        self._method_grp.addButton(self._rb_typesize, 0)
        self._method_grp.addButton(self._rb_file,     1)
        self._method_grp.addButton(self._rb_md5,      2)
        lay.addWidget(self._rb_typesize)
        lay.addWidget(self._rb_file)
        lay.addWidget(self._rb_md5)

        # ── タイプ/サイズ設定 ────────────────────────────────────
        self._typesize_w = QWidget()
        tsf = QFormLayout(self._typesize_w)
        self._img_type = QComboBox()
        self._img_type.addItems(["JPG", "PNG", "GIF", "WEBP", "BMP", "ANY"])
        self._img_type.setCurrentText(d.get("image_type", "JPG"))
        tsf.addRow("画像のタイプ", self._img_type)

        self._img_w = QSpinBox(); self._img_w.setRange(0, 99999)
        self._img_w.setSuffix(" px"); self._img_w.setValue(d.get("width", 0))
        tsf.addRow("画像の幅（単位:ピクセル）", self._img_w)

        self._img_h = QSpinBox(); self._img_h.setRange(0, 99999)
        self._img_h.setSuffix(" px"); self._img_h.setValue(d.get("height", 0))
        tsf.addRow("画像の高さ（単位:ピクセル）", self._img_h)

        size_row = QHBoxLayout()
        self._size_min = QSpinBox(); self._size_min.setRange(0, 999999999)
        self._size_min.setValue(d.get("size_min", 0))
        self._size_max = QSpinBox(); self._size_max.setRange(0, 999999999)
        self._size_max.setValue(d.get("size_max", 0))
        size_row.addWidget(self._size_min); size_row.addWidget(QLabel("〜"))
        size_row.addWidget(self._size_max); size_row.addStretch()
        tsf.addRow("画像のサイズ（単位:byte）", size_row)
        lay.addWidget(self._typesize_w)

        # ── ファイル一致 ──────────────────────────────────────────
        self._file_w = QWidget()
        file_lay = QVBoxLayout(self._file_w)
        file_row = QHBoxLayout()
        self._file_path = QLineEdit(d.get("file_path", ""))
        file_row.addWidget(self._file_path)
        browse_btn = QPushButton("…"); browse_btn.setFixedWidth(28)
        def _browse():
            p, _ = QFileDialog.getOpenFileName(self, "ファイルを選択", "", "画像 (*.jpg *.jpeg *.png *.gif *.webp);;全て (*)")
            if p: self._file_path.setText(p)
        browse_btn.clicked.connect(_browse)
        file_row.addWidget(browse_btn); file_lay.addLayout(file_row)

        calc_btn = QPushButton("ファイルバイナリからMD5ハッシュを計算する")
        def _calc_md5():
            path = self._file_path.text().strip()
            if not path:
                QMessageBox.warning(self, "エラー", "ファイルパスを入力してください"); return
            try:
                with open(path, "rb") as f:
                    md5 = hashlib.md5(f.read()).hexdigest()
                self._md5_edit.setText(md5)
                self._rb_md5.setChecked(True)
                self._update_method_ui()
            except Exception as e:
                QMessageBox.warning(self, "エラー", str(e))
        calc_btn.clicked.connect(_calc_md5)
        file_lay.addWidget(calc_btn)
        lay.addWidget(self._file_w)

        # ── MD5 ──────────────────────────────────────────────────
        self._md5_w = QWidget()
        md5_lay = QFormLayout(self._md5_w)
        self._md5_edit = QLineEdit(d.get("md5", ""))
        self._md5_edit.setPlaceholderText("32文字の16進数ハッシュ")
        md5_lay.addRow("MD5ハッシュ値", self._md5_edit)
        lay.addWidget(self._md5_w)

        # ── 有効期限 ──────────────────────────────────────────────
        exp_lay = QHBoxLayout()
        exp_lay.addWidget(QLabel("有効期限"))
        self._expires = QComboBox()
        self._expires.addItems(["無制限", "1日", "3日", "7日", "14日", "30日"])
        self._expires.setFixedWidth(100)
        cur_exp = d.get("expires", "無制限")
        idx = self._expires.findText(cur_exp)
        self._expires.setCurrentIndex(idx if idx >= 0 else 0)
        exp_lay.addWidget(self._expires); exp_lay.addStretch()
        lay.addLayout(exp_lay)

        # ── 逆NG チェック ──────────────────────────────────────────
        rev_row = QHBoxLayout()
        self._chk_reverse = QCheckBox("★ 逆NG ★")
        self._chk_reverse.setChecked(d.get("is_reverse_ng", False))
        rev_row.addWidget(self._chk_reverse)
        rev_link = QLabel('<a href="#">逆NGって何？</a>')
        rev_link.setOpenExternalLinks(False)
        rev_row.addWidget(rev_link); rev_row.addStretch()
        lay.addLayout(rev_row)

        # ── 表示モード ────────────────────────────────────────────
        hide_box = QGroupBox("NGにマッチした場合の表示")
        hide_lay = QVBoxLayout(hide_box)
        self._hide_mode_grp = QButtonGroup(self)
        self._rb_hide_image = QRadioButton("画像のみ透明表示（クリックで展開可）")
        self._rb_hide_res   = QRadioButton("レス全体を非表示")
        self._hide_mode_grp.addButton(self._rb_hide_image, 0)
        self._hide_mode_grp.addButton(self._rb_hide_res,   1)
        hide_lay.addWidget(self._rb_hide_image)
        hide_lay.addWidget(self._rb_hide_res)
        _hide_mode = d.get("hide_mode", "image")
        if _hide_mode == "res":
            self._rb_hide_res.setChecked(True)
        else:
            self._rb_hide_image.setChecked(True)
        lay.addWidget(hide_box)

        # ── 説明 ──────────────────────────────────────────────────
        desc_lay = QFormLayout()
        self._description = QLineEdit(d.get("description", ""))
        desc_lay.addRow("説明", self._description)
        lay.addLayout(desc_lay)

        lay.addStretch()
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル(C)")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        # 初期選択
        _method_map = {"type_size": 0, "file": 1, "md5": 2}
        btn = self._method_grp.button(_method_map.get(d.get("method", "md5"), 2))
        if btn: btn.setChecked(True)
        self._method_grp.idClicked.connect(lambda _: self._update_method_ui())
        self._update_method_ui()

    def _update_method_ui(self):
        idx = self._method_grp.checkedId()
        self._typesize_w.setVisible(idx == 0)
        self._file_w.setVisible(idx == 1)
        self._md5_w.setVisible(idx == 2)

    def _ok(self):
        idx = self._method_grp.checkedId()
        _methods = ["type_size", "file", "md5"]
        exp_text = self._expires.currentText()

        import datetime
        _exp_days = {"無制限": 0, "1日": 1, "3日": 3, "7日": 7, "14日": 14, "30日": 30}
        days = _exp_days.get(exp_text, 0)
        expires_at = ""
        if days > 0:
            expires_at = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

        entry = {
            "enabled":       True,
            "method":        _methods[idx] if 0 <= idx < 3 else "md5",
            "image_type":    self._img_type.currentText(),
            "width":         self._img_w.value(),
            "height":        self._img_h.value(),
            "size_min":      self._size_min.value(),
            "size_max":      self._size_max.value(),
            "file_path":     self._file_path.text().strip(),
            "md5":           self._md5_edit.text().strip(),
            "last_hit":      "",
            "expires":       exp_text,
            "expires_at":    expires_at,
            "is_reverse_ng": self._chk_reverse.isChecked(),
            "hide_mode":     "res" if self._rb_hide_res.isChecked() else "image",
            "description":   self._description.text().strip(),
        }
        if idx == 2 and not entry["md5"]:
            QMessageBox.warning(self, "入力エラー", "MD5ハッシュを入力してください"); return
        self._result = entry
        self.accept()

    def get_result(self) -> dict | None:
        return self._result


# ══════════════════════════════════════════════════════════════════════════════
# NG設定ダイアログ
# ══════════════════════════════════════════════════════════════════════════════

class NgSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NG設定"); self.resize(690, 460)
        self._settings = settings
        self._build()
        self._load()

    # ──────────────────────────────────────────────────────────────────────────
    def _build(self):
        from PySide6.QtGui import QColor
        lay = QVBoxLayout(self)
        nb  = QTabWidget(); lay.addWidget(nb, 1)

        # ────────────────────────────────────────
        # Tab 0: NGワード
        # ────────────────────────────────────────
        ng_w = QWidget(); nb.addTab(ng_w, "NGワード")
        ng_lay = QVBoxLayout(ng_w)
        self._chk_ng_all = QCheckBox(
            "レス本文・題名・名前・メール・ID・IPが以下と一致する場合は無視する")
        ng_lay.addWidget(self._chk_ng_all)

        # フィルタ入力欄
        self._word_filter = QLineEdit()
        self._word_filter.setPlaceholderText("抽出（入力するとリスト内を絞り込みます）")
        self._word_filter.setClearButtonEnabled(True)
        self._word_filter.textChanged.connect(self._apply_word_filter)
        ng_lay.addWidget(self._word_filter)

        self._word_table = QTableWidget(0, 6)
        self._word_table.setHorizontalHeaderLabels(
            ["", "NGワード（正規表現）", "期限", "NGタイプ", "適用範囲", "通知"])
        hh = self._word_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSortIndicatorShown(True)
        hh.sectionClicked.connect(self._sort_word_table)
        hh.sectionResized.connect(
            lambda *_: _save_col_widths(self._word_table, self._settings, "table_col_widths_ng_word"))
        self._word_sort_col = -1
        self._word_sort_asc = True
        # デフォルト列幅
        self._word_table.setColumnWidth(0, 24)
        self._word_table.setColumnWidth(1, 220)
        self._word_table.setColumnWidth(2, 60)
        self._word_table.setColumnWidth(3, 70)
        self._word_table.setColumnWidth(4, 120)
        self._word_table.setColumnWidth(5, 30)
        self._word_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._word_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._word_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vh = self._word_table.verticalHeader(); vh.setVisible(False); vh.setDefaultSectionSize(17)
        self._word_table.itemDoubleClicked.connect(lambda _: self._word_edit())
        ng_lay.addWidget(self._word_table, 1)

        wbtn = QHBoxLayout()
        for lbl, fn in [("追加(A)…", self._word_add),
                        ("修正(R)…", self._word_edit),
                        ("削除(D)",  self._word_delete)]:
            b = QPushButton(lbl); b.clicked.connect(fn); wbtn.addWidget(b)
        wbtn.addStretch()
        btn_ini_w = QPushButton("旧2B INIインポート…")
        btn_ini_w.clicked.connect(self._import_ini)
        wbtn.addWidget(btn_ini_w)
        ng_lay.addLayout(wbtn)

        # ────────────────────────────────────────
        # Tab 1: NG画像
        # ────────────────────────────────────────
        img_w = QWidget(); nb.addTab(img_w, "NG画像")
        img_lay = QVBoxLayout(img_w)
        self._chk_ng_image = QCheckBox("画像が以下の条件と一致する場合は透明（表示させない）にする")
        img_lay.addWidget(self._chk_ng_image)

        # 抽出入力欄
        self._img_filter = QLineEdit()
        self._img_filter.setPlaceholderText("抽出（入力するとリスト内を絞り込みます）")
        self._img_filter.setClearButtonEnabled(True)
        self._img_filter.textChanged.connect(self._apply_img_filter)
        img_lay.addWidget(self._img_filter)

        self._img_table = QTableWidget(0, 7)
        self._img_table.setHorizontalHeaderLabels(
            ["", "説明", "方法", "最終HIT", "期限", "NG対象", "表示"])
        ih = self._img_table.horizontalHeader()
        ih.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        ih.setSortIndicatorShown(True)
        ih.sectionResized.connect(
            lambda *_: _save_col_widths(self._img_table, self._settings, "table_col_widths_ng_image"))
        ih.sectionClicked.connect(self._sort_img_table)
        self._img_sort_col = -1
        self._img_sort_asc = True
        # デフォルト列幅
        self._img_table.setColumnWidth(0, 24)
        self._img_table.setColumnWidth(1, 190)
        self._img_table.setColumnWidth(2, 80)
        self._img_table.setColumnWidth(3, 70)
        self._img_table.setColumnWidth(4, 60)
        self._img_table.setColumnWidth(5, 50)
        self._img_table.setColumnWidth(6, 70)
        self._img_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._img_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._img_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vh2 = self._img_table.verticalHeader(); vh2.setVisible(False); vh2.setDefaultSectionSize(17)
        self._img_table.itemDoubleClicked.connect(lambda _: self._img_edit())
        img_lay.addWidget(self._img_table, 1)

        ibtn = QHBoxLayout()
        for lbl, fn in [("追加(A)…", self._img_add),
                        ("修正(R)…", self._img_edit),
                        ("削除(D)",  self._img_delete)]:
            b = QPushButton(lbl); b.clicked.connect(fn); ibtn.addWidget(b)
        ibtn.addStretch()
        btn_ini_i = QPushButton("旧2B INIインポート…")
        btn_ini_i.clicked.connect(self._import_ini)
        ibtn.addWidget(btn_ini_i)
        img_lay.addLayout(ibtn)

        # ────────────────────────────────────────
        # Tab 2: 設定[掲示板]
        # ────────────────────────────────────────
        brd_w = QWidget(); nb.addTab(brd_w, "設定 [掲示板]")
        brd_lay = QVBoxLayout(brd_w); brd_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chk_thr_name  = QCheckBox(
            "名前・トリップ・書き込みがNGの場合はレスを透明（表示させない）にする")
        self._chk_thr_image = QCheckBox(
            "画像がNGの場合はレスを透明（表示させない）にする")
        brd_lay.addWidget(self._chk_thr_name)
        brd_lay.addWidget(self._chk_thr_image)
        brd_lay.addStretch()

        # ────────────────────────────────────────
        # Tab 3: 設定[カタログ]
        # ────────────────────────────────────────
        cat_w = QWidget(); nb.addTab(cat_w, "設定 [カタログ]")
        cat_lay = QVBoxLayout(cat_w); cat_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        # [掲示板]タブから移動: NGワードに一致したスレをカタログから非表示
        self._chk_board_hide = QCheckBox(
            "NGワードに一致したスレをカタログから非表示にする")
        cat_lay.addWidget(self._chk_board_hide)
        # 共通ID(mode=json id)のスレをカタログから非表示
        self._chk_cat_hide_common_id = QCheckBox(
            "共通IDをカタログから非表示にする")
        self._chk_cat_hide_common_id.setToolTip(
            "mode=json の共通IDが出ているスレをカタログに表示しません")
        cat_lay.addWidget(self._chk_cat_hide_common_id)
        self._chk_thr_close = QCheckBox("NGスレッドを開いたら即閉じる")
        cat_lay.addWidget(self._chk_thr_close)
        g_empty = QGroupBox("字スレ"); cat_lay.addWidget(g_empty)
        empty_lay = QVBoxLayout(g_empty)
        self._cat_empty_grp = QButtonGroup(self)
        for i, lbl in enumerate([
                "レス本文が空の場合（何も表示されないスレ）のみNGにする",
                "NGにする", "何もしない"]):
            rb = QRadioButton(lbl)
            self._cat_empty_grp.addButton(rb, i)
            empty_lay.addWidget(rb)
        self._chk_cat_pack = QCheckBox(
            "カタログ・スレッド一覧[ビュー]で無視されたスレッドは詰める")
        cat_lay.addWidget(self._chk_cat_pack)
        cat_lay.addStretch()

        # ────────────────────────────────────────
        # Tab 4: 設定[逆NG]
        # ────────────────────────────────────────
        rev_w = QWidget(); nb.addTab(rev_w, "設定 [逆NG]")
        rev_lay = QVBoxLayout(rev_w)

        # 左右2列レイアウト
        top_row = QHBoxLayout()

        # ── 左: ピップアップ方法 ────────────────────────────────
        left_box = QGroupBox("逆NGに引っかかったスレのピップアップ方法")
        left_lay = QVBoxLayout(left_box)
        self._rev_action_grp = QButtonGroup(self)
        # ラベル順 = ButtonGroup ID 順。処理(_exec_reverse_ng_one)が
        # action 1=非アクティブで開く / 2=アクティブで開く のためラベルもこの順に合わせる
        _rev_actions = ["何もしない", "スレッドを非アクティブで開く",
                        "スレッドを開く", "ポップアップ通知"]
        for i, lbl in enumerate(_rev_actions):
            rb = QRadioButton(lbl)
            self._rev_action_grp.addButton(rb, i)
            left_lay.addWidget(rb)
        top_row.addWidget(left_box)

        # ── 右: 優先順位 ────────────────────────────────────────
        right_box = QGroupBox("優先順位")
        right_lay = QFormLayout(right_box)
        self._pri_word = QComboBox()
        self._pri_word.addItems(["NGワード > 逆NGワード", "NGワード < 逆NGワード"])
        right_lay.addRow("", self._pri_word)
        self._pri_image = QComboBox()
        self._pri_image.addItems(["NG画像 > 逆NG画像", "NG画像 < 逆NG画像"])
        right_lay.addRow("", self._pri_image)
        # 同時に開く件数
        self._rev_max_open = QSpinBox()
        self._rev_max_open.setRange(1, 99)
        self._rev_max_open.setValue(99)
        self._rev_max_open.setSuffix(" 件")
        right_lay.addRow("一度に開く件数:", self._rev_max_open)
        # ── 逆NGワード通知 棒読みちゃん書式（右列・優先順位の下）───────────
        notify_fmt_box = QGroupBox("逆NGワード通知 棒読みちゃん書式")
        notify_fmt_lay = QVBoxLayout(notify_fmt_box)
        self._ng_notify_bouyomi_fmt = QLineEdit()
        self._ng_notify_bouyomi_fmt.setPlaceholderText("{keyword}: {title}")
        self._ng_notify_bouyomi_fmt.setToolTip(
            "{keyword} … マッチした逆NGワードパターン\n"
            "{keyword1} … パターンの最初の|区切り部分\n"
            "{board} … 板名（サブドメインあり）\n"
            "{title} … スレタイ\n{url} … スレURL")
        notify_fmt_lay.addWidget(self._ng_notify_bouyomi_fmt)
        hint = QLabel("{keyword}  {keyword1}  {board}  {title}  {url}  が使用できます")
        hint.setStyleSheet("color:#888;font-size:8pt;")
        notify_fmt_lay.addWidget(hint)

        # 右列: 優先順位 + NGワード通知を縦積み
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addWidget(right_box)
        right_col.addWidget(notify_fmt_box)
        right_col.addStretch()
        top_row.addLayout(right_col)
        rev_lay.addLayout(top_row)


        # ── 通知色 ──────────────────────────────────────────────
        color_row = QHBoxLayout()

        # 読む前
        unread_box = QGroupBox("通知色（読む前）")
        unread_lay = QVBoxLayout(unread_box)
        self._unread_color_swatch = QLabel()
        self._unread_color_swatch.setFixedSize(48, 48)
        self._unread_color_swatch.setStyleSheet(
            "background:#9B59B6;border:1px solid #888;")
        unread_btns = QVBoxLayout()
        btn_ub = QPushButton("枠…");   btn_ub.setFixedWidth(60)
        btn_ubg = QPushButton("背景…"); btn_ubg.setFixedWidth(60)
        btn_ud  = QPushButton("デフォルト"); btn_ud.setFixedWidth(76)
        unread_btns.addWidget(btn_ub); unread_btns.addWidget(btn_ubg)
        unread_btns.addWidget(btn_ud)
        unread_inner = QHBoxLayout()
        unread_inner.addWidget(self._unread_color_swatch)
        unread_inner.addLayout(unread_btns)
        unread_lay.addLayout(unread_inner)
        color_row.addWidget(unread_box)

        # 読んだ後
        read_box = QGroupBox("通知色（読んだ後）")
        read_lay = QVBoxLayout(read_box)
        self._read_color_swatch = QLabel()
        self._read_color_swatch.setFixedSize(48, 48)
        self._read_color_swatch.setStyleSheet(
            "background:#E8E8E8;border:1px solid #888;")
        read_btns = QVBoxLayout()
        self._btn_rb  = QPushButton("枠…");       self._btn_rb.setFixedWidth(60)
        self._btn_rbg = QPushButton("背景…");     self._btn_rbg.setFixedWidth(60)
        self._btn_rd  = QPushButton("デフォルト"); self._btn_rd.setFixedWidth(76)
        read_btns.addWidget(self._btn_rb); read_btns.addWidget(self._btn_rbg)
        read_btns.addWidget(self._btn_rd)
        read_inner = QHBoxLayout()
        read_inner.addWidget(self._read_color_swatch)
        read_inner.addLayout(read_btns)
        read_lay.addLayout(read_inner)
        color_row.addWidget(read_box)
        rev_lay.addLayout(color_row)

        self._chk_default_color = QCheckBox("デフォルト色を使用する")
        rev_lay.addWidget(self._chk_default_color)

        # デフォルト色チェックで読んだ後を無効化
        def _update_read_color_enabled():
            en = not self._chk_default_color.isChecked()
            for w in [self._btn_rb, self._btn_rbg, self._btn_rd,
                      self._read_color_swatch]:
                w.setEnabled(en)
        self._chk_default_color.toggled.connect(_update_read_color_enabled)

        # 色ボタン処理
        self._unread_border_color = ""
        self._unread_bg_color     = "#9B59B6"
        self._read_border_color   = ""
        self._read_bg_color       = "#E8E8E8"

        def _pick_color(attr: str, swatch: QLabel | None = None):
            from PySide6.QtWidgets import QColorDialog
            cur = getattr(self, attr, "") or "#ffffff"
            c = QColorDialog.getColor(QColor(cur), self, "色を選択")
            if c.isValid():
                setattr(self, attr, c.name())
                if swatch:
                    swatch.setStyleSheet(
                        f"background:{c.name()};border:1px solid #888;")

        btn_ub.clicked.connect(
            lambda: _pick_color("_unread_border_color", self._unread_color_swatch))
        btn_ubg.clicked.connect(
            lambda: _pick_color("_unread_bg_color", self._unread_color_swatch))
        btn_ud.clicked.connect(lambda: (
            setattr(self, "_unread_bg_color", "#9B59B6"),
            setattr(self, "_unread_border_color", ""),
            self._unread_color_swatch.setStyleSheet(
                "background:#9B59B6;border:1px solid #888;")))
        self._btn_rbg.clicked.connect(
            lambda: _pick_color("_read_bg_color", self._read_color_swatch))
        self._btn_rb.clicked.connect(
            lambda: _pick_color("_read_border_color", self._read_color_swatch))
        self._btn_rd.clicked.connect(lambda: (
            setattr(self, "_read_bg_color", "#E8E8E8"),
            setattr(self, "_read_border_color", ""),
            self._read_color_swatch.setStyleSheet(
                "background:#E8E8E8;border:1px solid #888;")))

        # 逆NGって何？リンク
        rev_link = _ReverseNgHelpLabel()
        rev_lay.addWidget(rev_link)

        rev_lay.addStretch()

        # ────────────────────────────────────────
        # 下部: NGスレッドリストクリア + 逆NG記録リセット + OK/Cancel
        # ────────────────────────────────────────
        bottom = QHBoxLayout()
        self._clear_ng_btn = QPushButton("NGスレッドリストをクリアする(0件)…")
        self._clear_ng_btn.clicked.connect(self._clear_ng_threads)
        bottom.addWidget(self._clear_ng_btn)
        self._reset_reverse_btn = QPushButton("開いた記録をリセット（0件）")
        self._reset_reverse_btn.clicked.connect(self._reset_reverse_ng_opened)
        bottom.addWidget(self._reset_reverse_btn)
        bottom.addStretch()
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        bottom.addWidget(btns)
        lay.addLayout(bottom)

    # ──────────────────────────────────────────────────────────────────────────
    def _load(self):
        s = self._settings

        # NGワードテーブル
        self._chk_ng_all.setChecked(True)   # 互換
        self._refresh_word_table()

        # NG画像テーブル
        self._chk_ng_image.setChecked(True)
        self._refresh_img_table()
        _restore_col_widths(self._img_table, self._settings, "table_col_widths_ng_image")

        # 掲示板タブ
        self._chk_board_hide.setChecked(getattr(s, "ng_board_hide_ng_thread", True))
        self._chk_cat_hide_common_id.setChecked(getattr(s, "ng_catalog_hide_common_id", False))

        # スレッドタブ
        self._chk_thr_name.setChecked(getattr(s, "ng_thread_hide_name",  True))
        self._chk_thr_image.setChecked(getattr(s, "ng_thread_hide_image", True))
        self._chk_thr_close.setChecked(getattr(s, "ng_thread_close_ng",  False))

        # カタログタブ
        empty_idx = getattr(s, "ng_catalog_empty", 2)
        btn = self._cat_empty_grp.button(empty_idx)
        if btn: btn.setChecked(True)
        self._chk_cat_pack.setChecked(getattr(s, "ng_catalog_pack", True))

        # 逆NGタブ
        action_idx = min(getattr(s, "ng_reverse_action", 1), 3)
        btn = self._rev_action_grp.button(action_idx)
        if btn: btn.setChecked(True)
        self._ng_notify_bouyomi_fmt.setText(
            getattr(s, "ng_reverse_bouyomi_format", "{keyword1}"))

        self._pri_word.setCurrentIndex(getattr(s, "ng_priority_word_idx",  0))
        self._pri_image.setCurrentIndex(getattr(s, "ng_priority_image_idx", 0))
        self._rev_max_open.setValue(getattr(s, "ng_reverse_max_open", 3))

        self._unread_border_color = getattr(s, "ng_reverse_unread_border", "")
        self._unread_bg_color     = getattr(s, "ng_reverse_unread_bg", "#9B59B6")
        self._read_border_color   = getattr(s, "ng_reverse_read_border", "")
        self._read_bg_color       = getattr(s, "ng_reverse_read_bg",   "#E8E8E8")

        use_def = getattr(s, "ng_reverse_use_default_color", True)
        self._chk_default_color.setChecked(use_def)

        bg = self._unread_bg_color or "#9B59B6"
        self._unread_color_swatch.setStyleSheet(f"background:{bg};border:1px solid #888;")
        bg2 = self._read_bg_color or "#E8E8E8"
        self._read_color_swatch.setStyleSheet(f"background:{bg2};border:1px solid #888;")

        # 読んだ後の色ボタン有効状態
        for w in [self._btn_rb, self._btn_rbg, self._btn_rd, self._read_color_swatch]:
            w.setEnabled(not use_def)

        # クリアボタン件数表示
        self._update_clear_btn()
        # リセットボタン件数表示
        _rev_count = len(getattr(self._settings, "ng_reverse_opened_urls", set()))
        self._reset_reverse_btn.setText(
            f"開いた記録をリセット（{_rev_count}件）")

    # ──────────────────────────────────────────────────────────────────────────
    def _refresh_word_table(self):
        from PySide6.QtGui import QColor
        self._word_table.blockSignals(True)
        self._word_table.setRowCount(0)
        for ng in self._settings.ng_words:
            row = self._word_table.rowCount()
            self._word_table.insertRow(row)

            # チェックボックス列
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(
                Qt.CheckState.Checked if ng.get("enabled", True)
                else Qt.CheckState.Unchecked)
            self._word_table.setItem(row, 0, chk)

            # NGワード列
            self._word_table.setItem(row, 1, QTableWidgetItem(ng.get("pattern", "")))

            # 期限列
            self._word_table.setItem(row, 2, QTableWidgetItem(ng.get("expires", "無制限")))

            # NGタイプ列
            ng_type = ng.get("ng_type", "ng")
            if ng_type == "reverse_ng":
                item = QTableWidgetItem("[逆NG]")
                item.setForeground(QColor("#cc0000"))
            elif ng_type == "replace":
                item = QTableWidgetItem("[置換]")
            elif ng_type == "mow_replace":
                item = QTableWidgetItem("[芝刈り]")
            else:
                item = QTableWidgetItem("NG")
            self._word_table.setItem(row, 3, item)

            # 適用範囲列
            scope_parts = []
            for key, label in [("scope_body", "本文"), ("scope_name", "名前"),
                                ("scope_mail", "メール"), ("scope_subject", "件名"),
                                ("scope_id", "ID"), ("scope_ip", "IP"),
                                ("scope_catalog", "カタログ")]:
                if ng.get(key, False):
                    scope_parts.append(label)
            scope_item = QTableWidgetItem(", ".join(scope_parts) or "-")
            self._word_table.setItem(row, 4, scope_item)

            # 通知列
            if ng.get("notify", False):
                ntype = ng.get("notify_type", "sound")
                notify_label = "棒" if ntype == "bouyomi" else "効"
            else:
                notify_label = ""
            self._word_table.setItem(row, 5, QTableWidgetItem(notify_label))

        # チェック状態変更をデータに同期（二重connect防止）
        self._word_table.blockSignals(False)
        if not getattr(self, '_word_table_connected', False):
            self._word_table.itemChanged.connect(self._on_word_check_changed)
            self._word_table_connected = True
        # 列幅を復元（再描画後に適用）
        _restore_col_widths(self._word_table, self._settings, "table_col_widths_ng_word")

    def _on_word_check_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            row = item.row()
            if 0 <= row < len(self._settings.ng_words):
                self._settings.ng_words[row]["enabled"] = (
                    item.checkState() == Qt.CheckState.Checked)
                self._settings.invalidate_ng_cache()

    def _apply_word_filter(self, text: str):
        """フィルタテキストでNGワードテーブルの行を絞り込む"""
        q = text.strip().lower()
        for row in range(self._word_table.rowCount()):
            item = self._word_table.item(row, 1)
            if not q or (item and q in item.text().lower()):
                self._word_table.setRowHidden(row, False)
            else:
                self._word_table.setRowHidden(row, True)

    def _sort_word_table(self, col: int):
        """NGワードテーブルをヘッダクリックでソート（データも並び替え）"""
        if self._word_sort_col == col:
            self._word_sort_asc = not self._word_sort_asc
        else:
            self._word_sort_col = col
            self._word_sort_asc = True
        # データ側もソート
        _SCOPE_KEYS = ["scope_body", "scope_name", "scope_mail",
                       "scope_subject", "scope_id", "scope_ip", "scope_catalog"]
        _SCOPE_LABELS = ["本文", "名前", "メール", "件名", "ID", "IP", "カタログ"]
        def _sort_key(ng):
            if col == 0:   # 有効/無効
                return 0 if ng.get("enabled", True) else 1
            if col == 1:   # NGワード
                return str(ng.get("pattern", "")).lower()
            if col == 2:   # 期限
                v = ng.get("expires", "無制限")
                return str(v).lower() if v else ""
            if col == 3:   # NGタイプ
                return str(ng.get("ng_type", "")).lower()
            if col == 4:   # 適用範囲: 有効なラベルを "," 結合してソートキーに
                parts = [lbl for k, lbl in zip(_SCOPE_KEYS, _SCOPE_LABELS)
                         if ng.get(k, False)]
                return ", ".join(parts).lower()
            if col == 5:   # 通知: 有無 → "棒"/"効"/"" の順
                if not ng.get("notify", False):
                    return "zzz"  # 末尾に
                ntype = ng.get("notify_type", "sound")
                return "棒" if ntype == "bouyomi" else "効"
            return ""
        self._settings.ng_words.sort(key=_sort_key, reverse=not self._word_sort_asc)
        self._refresh_word_table()
        self._apply_word_filter(self._word_filter.text())
        hh = self._word_table.horizontalHeader()
        hh.setSortIndicator(col,
            Qt.SortOrder.AscendingOrder if self._word_sort_asc
            else Qt.SortOrder.DescendingOrder)

    def _sort_img_table(self, col: int):
        """NG画像テーブルをヘッダクリックでソート（データも並び替え）"""
        if self._img_sort_col == col:
            self._img_sort_asc = not self._img_sort_asc
        else:
            self._img_sort_col = col
            self._img_sort_asc = True
        _METHOD_ORDER = {"type_size": 0, "file": 1, "md5": 2}
        def _sort_key(img):
            if col == 0:   # 有効/無効
                return 0 if img.get("enabled", True) else 1
            if col == 1:   # 説明
                v = img.get("description", "") or img.get("md5", "")
                return str(v).lower()
            if col == 2:   # 方法
                return _METHOD_ORDER.get(img.get("method", "md5"), 99)
            if col == 3:   # 最終HIT
                return str(img.get("last_hit", "")).lower()
            if col == 4:   # 期限
                v = img.get("expires", "無制限")
                # "無制限" を末尾にするため先頭に "zzz" を付けてソート
                return "zzz" if v == "無制限" else str(v)
            if col == 5:   # NG対象（逆NG / NG）
                return 0 if img.get("is_reverse_ng") else 1
            if col == 6:   # 表示（レス非表示 / 透明）
                return img.get("hide_mode", "image")
            return ""
        self._settings.ng_images.sort(key=_sort_key, reverse=not self._img_sort_asc)
        self._refresh_img_table()
        if hasattr(self, '_img_filter'):
            self._apply_img_filter(self._img_filter.text())
        # ソートインジケーターを更新
        ih = self._img_table.horizontalHeader()
        ih.setSortIndicator(col,
            Qt.SortOrder.AscendingOrder if self._img_sort_asc
            else Qt.SortOrder.DescendingOrder)

    def _refresh_img_table(self):
        from PySide6.QtGui import QColor
        self._img_table.setRowCount(0)
        _method_labels = {"type_size": "タイプ/サイズ", "file": "ファイル", "md5": "MD5"}
        for img in self._settings.ng_images:
            row = self._img_table.rowCount()
            self._img_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(
                Qt.CheckState.Checked if img.get("enabled", True)
                else Qt.CheckState.Unchecked)
            self._img_table.setItem(row, 0, chk)

            desc = img.get("description", "") or img.get("md5", "")[:20]
            self._img_table.setItem(row, 1, QTableWidgetItem(desc))
            method = _method_labels.get(img.get("method", "md5"), "MD5")
            self._img_table.setItem(row, 2, QTableWidgetItem(method))
            self._img_table.setItem(row, 3, QTableWidgetItem(img.get("last_hit", "")))
            self._img_table.setItem(row, 4, QTableWidgetItem(img.get("expires", "無制限")))

            ng_target = "逆NG" if img.get("is_reverse_ng") else "NG"
            titem = QTableWidgetItem(ng_target)
            if img.get("is_reverse_ng"):
                titem.setForeground(QColor("#cc0000"))
            self._img_table.setItem(row, 5, titem)

            hide_mode = img.get("hide_mode", "image")
            hide_lbl = "レス非表示" if hide_mode == "res" else "透明"
            hitem = QTableWidgetItem(hide_lbl)
            if hide_mode == "res":
                hitem.setForeground(QColor("#884400"))
            self._img_table.setItem(row, 6, hitem)

        self._img_table.itemChanged.connect(self._on_img_check_changed)
        # フィルタが入力済みなら再適用
        if hasattr(self, '_img_filter'):
            self._apply_img_filter(self._img_filter.text())

    def _on_img_check_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            row = item.row()
            if 0 <= row < len(self._settings.ng_images):
                self._settings.ng_images[row]["enabled"] = (
                    item.checkState() == Qt.CheckState.Checked)
                self._settings.invalidate_ng_cache()

    def _apply_img_filter(self, text: str):
        """フィルタテキストでNG画像テーブルの行を絞り込む"""
        q = text.strip().lower()
        for row in range(self._img_table.rowCount()):
            item = self._img_table.item(row, 1)
            if not q or (item and q in item.text().lower()):
                self._img_table.setRowHidden(row, False)
            else:
                self._img_table.setRowHidden(row, True)

    # ──────────────────────────────────────────────────────────────────────────
    # 旧2B INIインポート
    # ──────────────────────────────────────────────────────────────────────────
    def _import_ini(self):
        dlg = ImportIniDialog(self._settings, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try: self._word_table.itemChanged.disconnect()
            except RuntimeError: pass
            try: self._img_table.itemChanged.disconnect()
            except RuntimeError: pass
            self._refresh_word_table()
            self._refresh_img_table()

    # ──────────────────────────────────────────────────────────────────────────
    # NGワード CRUD
    # ──────────────────────────────────────────────────────────────────────────
    def _word_add(self):
        dlg = NgWordEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
            if result:
                self._settings.ng_words.append(result)
                self._settings.invalidate_ng_cache()
                try: self._word_table.itemChanged.disconnect()
                except RuntimeError: pass
                self._refresh_word_table()

    def _word_edit(self):
        rows = sorted(set(
            idx.row() for idx in self._word_table.selectedIndexes()
        ))
        if not rows:
            return
        if len(rows) == 1:
            # 1件: 従来の個別編集ダイアログ
            row = rows[0]
            if 0 <= row < len(self._settings.ng_words):
                dlg = NgWordEditDialog(self._settings.ng_words[row], parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    result = dlg.get_result()
                    if result:
                        self._settings.ng_words[row] = result
                        self._settings.invalidate_ng_cache()
                        try: self._word_table.itemChanged.disconnect()
                        except RuntimeError: pass
                        self._refresh_word_table()
        else:
            # 複数件: 一括修正ダイアログ
            valid_rows = [r for r in rows if 0 <= r < len(self._settings.ng_words)]
            if not valid_rows:
                return
            entries = [self._settings.ng_words[r] for r in valid_rows]
            dlg = NgWordBulkEditDialog(entries, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                results = dlg.get_result()
                if results:
                    for r, updated in zip(valid_rows, results):
                        self._settings.ng_words[r] = updated
                    self._settings.invalidate_ng_cache()
                    try: self._word_table.itemChanged.disconnect()
                    except RuntimeError: pass
                    self._refresh_word_table()

    def _word_delete(self):
        rows = sorted(set(
            idx.row() for idx in self._word_table.selectedIndexes()
        ), reverse=True)
        if not rows:
            return
        # 削除対象ワード一覧を作成して確認ダイアログを表示
        targets = [self._settings.ng_words[r].get("pattern", "") for r in rows
                   if 0 <= r < len(self._settings.ng_words)]
        _MAX_SHOW = 10
        if len(targets) <= _MAX_SHOW:
            msg = "以下のNGワードを削除しますか？\n\n" + "\n".join(f"・{p}" for p in targets)
        else:
            shown = "\n".join(f"・{p}" for p in targets[:_MAX_SHOW])
            msg = (f"以下のNGワードを削除しますか？\n\n{shown}\n"
                   f"　…他 {len(targets) - _MAX_SHOW} 件\n\n計 {len(targets)} 件")
        reply = QMessageBox.question(self, "削除確認", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            if 0 <= r < len(self._settings.ng_words):
                del self._settings.ng_words[r]
        self._settings.invalidate_ng_cache()
        try: self._word_table.itemChanged.disconnect()
        except RuntimeError: pass
        self._refresh_word_table()

    # ──────────────────────────────────────────────────────────────────────────
    # NG画像 CRUD
    # ──────────────────────────────────────────────────────────────────────────
    def _img_add(self):
        dlg = NgImageEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()
            if result:
                self._settings.ng_images.append(result)
                self._settings.invalidate_ng_cache()
                try: self._img_table.itemChanged.disconnect()
                except RuntimeError: pass
                self._refresh_img_table()

    def _img_edit(self):
        rows = sorted(set(
            idx.row() for idx in self._img_table.selectedIndexes()
        ))
        if not rows:
            return
        if len(rows) == 1:
            row = rows[0]
            if 0 <= row < len(self._settings.ng_images):
                dlg = NgImageEditDialog(self._settings.ng_images[row], parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    result = dlg.get_result()
                    if result:
                        self._settings.ng_images[row] = result
                        self._settings.invalidate_ng_cache()
                        try: self._img_table.itemChanged.disconnect()
                        except RuntimeError: pass
                        self._refresh_img_table()
        else:
            # 複数件: 一括修正ダイアログ
            valid_rows = [r for r in rows if 0 <= r < len(self._settings.ng_images)]
            if not valid_rows:
                return
            entries = [self._settings.ng_images[r] for r in valid_rows]
            dlg = NgImageBulkEditDialog(entries, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                results = dlg.get_result()
                if results:
                    for r, updated in zip(valid_rows, results):
                        self._settings.ng_images[r] = updated
                    self._settings.invalidate_ng_cache()
                    try: self._img_table.itemChanged.disconnect()
                    except RuntimeError: pass
                    self._refresh_img_table()

    def _img_delete(self):
        rows = sorted(set(
            idx.row() for idx in self._img_table.selectedIndexes()
        ), reverse=True)
        if not rows:
            return
        targets = [self._settings.ng_images[r].get("description", "")
                   or self._settings.ng_images[r].get("md5", "")[:16]
                   for r in rows if 0 <= r < len(self._settings.ng_images)]
        _MAX_SHOW = 10
        if len(targets) <= _MAX_SHOW:
            msg = "以下のNG画像を削除しますか？\n\n" + "\n".join(f"・{p}" for p in targets)
        else:
            shown = "\n".join(f"・{p}" for p in targets[:_MAX_SHOW])
            msg = (f"以下のNG画像を削除しますか？\n\n{shown}\n"
                   f"　…他 {len(targets) - _MAX_SHOW} 件\n\n計 {len(targets)} 件")
        reply = QMessageBox.question(self, "削除確認", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            if 0 <= r < len(self._settings.ng_images):
                del self._settings.ng_images[r]
        self._settings.invalidate_ng_cache()
        try: self._img_table.itemChanged.disconnect()
        except RuntimeError: pass
        self._refresh_img_table()

    # ──────────────────────────────────────────────────────────────────────────
    def _update_clear_btn(self):
        count = len(self._settings.ng_thread_urls)
        self._clear_ng_btn.setText(f"NGスレッドリストをクリアする({count}件)…")

    def _clear_ng_threads(self):
        count = len(self._settings.ng_thread_urls)
        if count == 0:
            QMessageBox.information(self, "NGスレッドリスト", "登録されているNGスレッドはありません。")
            return
        reply = QMessageBox.question(
            self, "NGスレッドリストのクリア",
            f"NGスレッドURLリスト（{count}件）をクリアします。よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._settings.ng_thread_urls.clear()
            self._update_clear_btn()

    def _reset_reverse_ng_opened(self):
        """逆NGで開いたURL記録をクリアし、もう一度開けるようにする"""
        count = len(getattr(self._settings, "ng_reverse_opened_urls", set()))
        if count == 0:
            QMessageBox.information(self, "逆NG開いた記録", "記録されているURLはありません。")
            return
        reply = QMessageBox.question(
            self, "逆NG開いた記録のリセット",
            f"逆NGで開いたURL記録（{count}件）をリセットします。\n"
            "次回カタログ更新時にもう一度開けるようになります。よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._settings.ng_reverse_opened_urls.clear()
            self._settings._ng_reverse_opened_list.clear()
            self._settings.save()
            self._reset_reverse_btn.setText("開いた記録をリセット（0件）")

    # ──────────────────────────────────────────────────────────────────────────
    def _ok(self):
        s = self._settings

        # 掲示板タブ
        s.ng_board_hide_ng_thread = self._chk_board_hide.isChecked()
        s.ng_catalog_hide_common_id = self._chk_cat_hide_common_id.isChecked()

        # スレッドタブ
        s.ng_thread_hide_name  = self._chk_thr_name.isChecked()
        s.ng_thread_hide_image = self._chk_thr_image.isChecked()
        s.ng_thread_close_ng   = self._chk_thr_close.isChecked()

        # カタログタブ
        s.ng_catalog_empty = self._cat_empty_grp.checkedId()
        s.ng_catalog_pack  = self._chk_cat_pack.isChecked()

        # 逆NGタブ
        s.ng_reverse_action          = self._rev_action_grp.checkedId()
        s.ng_reverse_bouyomi_format = self._ng_notify_bouyomi_fmt.text().strip() or "{keyword1}"
        s.ng_priority_word_idx     = self._pri_word.currentIndex()
        s.ng_priority_image_idx    = self._pri_image.currentIndex()
        s.ng_reverse_max_open      = self._rev_max_open.value()
        s.ng_reverse_unread_border = self._unread_border_color
        s.ng_reverse_unread_bg     = self._unread_bg_color
        s.ng_reverse_read_border   = self._read_border_color
        s.ng_reverse_read_bg       = self._read_bg_color
        s.ng_reverse_use_default_color = self._chk_default_color.isChecked()

        s.save()
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
# 全体設定ダイアログ (板・スレッド / レス / カタログ・一覧)
# ══════════════════════════════════════════════════════════════════════════════

class AppSettingsDialog(QDialog):
    """全板共通設定ダイアログ
    タブ: スレッド / カタログ・レス・投稿 / ログ保存 / アップローダー / 画像保存 / 棒読みちゃん
    """
    def __init__(self, settings: AppSettings, on_apply=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("2BPの設定"); self.resize(780, 680)
        self._settings = settings; self._on_apply = on_apply
        self._build(); self._load()

    # ──────────────────────────────────────────────────────────────────────
    def _build(self):
        lay = QVBoxLayout(self)
        nb  = QTabWidget(); lay.addWidget(nb, 1)

        def _scroll(widget):
            sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(widget)
            return sa

        def _spin(lo, hi, suffix="", tip="", width=None):
            w = _NoWheelSpinBox(); w.setRange(lo, hi)
            if suffix: w.setSuffix(suffix)
            if tip:    w.setToolTip(tip)
            if width:  w.setFixedWidth(width)
            return w

        def _combo(items, tip=""):
            w = _NoWheelComboBox(); w.addItems(items)
            if tip: w.setToolTip(tip)
            return w

        # ══════════════════════════════════════════════════════════════════
        # Tab0: 全般
        # ══════════════════════════════════════════════════════════════════
        w0 = QWidget(); f0 = QVBoxLayout(w0)

        # カタログ
        g_cat_hover = QGroupBox("カタログ"); f0.addWidget(g_cat_hover)
        cat_hover_lay = QVBoxLayout(g_cat_hover)
        self._cat_hover_zoom    = QCheckBox("オンマウスでサムネイル画像を拡大表示する")
        self._cat_hover_comment = QCheckBox("オンマウスでスレ本文（先頭120文字）を表示する")
        self._cat_mail_badge    = QCheckBox("メール欄/IDをサムネ右上にバッジ表示する")
        self._cat_quarantine    = QCheckBox("隔離スレを最下部に表示する")
        self._cat_common_id_bottom = QCheckBox("IDが出たスレを下にまとめる")
        self._cat_common_id_bottom.setToolTip(
            "IDが出ているスレ（mode=json id）を、ID別に分けず最下部に一括でまとめます")
        cat_hover_lay.addWidget(self._cat_hover_zoom)
        cat_hover_lay.addWidget(self._cat_hover_comment)
        cat_hover_lay.addWidget(self._cat_mail_badge)
        cat_hover_lay.addWidget(self._cat_quarantine)
        cat_hover_lay.addWidget(self._cat_common_id_bottom)

        # スレ落ち時のタブ自動クローズ
        g_close = QGroupBox("スレ落ち時のタブ自動クローズ"); f0.addWidget(g_close); clf2 = QVBoxLayout(g_close)
        self._auto_close = QCheckBox("スレ落ちを検知したらタブを自動で閉じる"); clf2.addWidget(self._auto_close)
        self._auto_close_full = QCheckBox("1000レス到達時もタブを自動で閉じる"); clf2.addWidget(self._auto_close_full)
        self._auto_close_skip_pinned = QCheckBox("ピン留めしているタブは閉じない"); clf2.addWidget(self._auto_close_skip_pinned)
        def _toggle_close(checked): self._auto_close_skip_pinned.setEnabled(checked or self._auto_close_full.isChecked())
        def _toggle_close_full(checked): self._auto_close_skip_pinned.setEnabled(checked or self._auto_close.isChecked())
        self._auto_close.toggled.connect(_toggle_close); _toggle_close(False)
        self._auto_close_full.toggled.connect(_toggle_close_full)

        # 画像モード 折り返し列数
        g_imgmode = QGroupBox("画像モード"); f0.addWidget(g_imgmode)
        imf = QFormLayout(g_imgmode)
        self._image_mode_cols = _spin(1, 30, " 列",
            tip="画像モードで画像を何列で折り返すか", width=80)
        imf.addRow("折り返し列数:", self._image_mode_cols)

        # タブ表示設定
        g_tab = QGroupBox("タブ"); f0.addWidget(g_tab); taf = QFormLayout(g_tab)
        self._tab_max_width = _spin(0, 1000, " px",
            tip="タブの最大幅を指定します（0=無制限・テキスト長に合わせて自動調整）",
            width=80)
        taf.addRow("タブ最大幅:", self._tab_max_width)
        _tab_hint = QLabel("0=無制限（テキスト幅に自動調整）　数値を指定すると長いタブ名が切れます")
        _tab_hint.setStyleSheet("color: gray; font-size: 11px;")
        taf.addRow("", _tab_hint)
        self._tab_pink_op_no_id = QCheckBox("IDが出ちゃったスレのタブをピンク色にする")
        self._tab_pink_op_no_id.setToolTip(
            "メール欄にID表示の指定が無いのにIDが出ているスレのタブ文字をピンクにします")
        taf.addRow(self._tab_pink_op_no_id)
        self._tab_orange_quarantine = QCheckBox("隔離されたスレのタブをオレンジ色にする")
        self._tab_orange_quarantine.setToolTip(
            "カタログから消えて隔離されたスレ(json∖cat)のタブ文字をオレンジにします。"
            "IDと隔離が同時の場合は #FF0099。\n"
            "※判定にはカタログのmode=json取得（メール欄/IDバッジ か 隔離まとめ表示）が必要です")
        taf.addRow(self._tab_orange_quarantine)

        # 保持件数
        g_keep = QGroupBox("保持件数"); f0.addWidget(g_keep); kpf = QFormLayout(g_keep)
        self._recent_closed_max = _spin(1, 100, " 件", width=80,
            tip="「最近閉じたスレ」メニューの保持件数")
        kpf.addRow("最近閉じたスレ:", self._recent_closed_max)
        self._recent_images_max = _spin(1, 100, " 件", width=80,
            tip="「最近開いた画像」メニューの保持件数")
        kpf.addRow("最近開いた画像:", self._recent_images_max)

        # スクロール更新
        g_scroll = QGroupBox("スクロール更新"); f0.addWidget(g_scroll); scf = QFormLayout(g_scroll)
        self._scroll_top_count = _spin(0, 99, " 回",
            tip="先頭付近で何回ホイールスクロールしたら更新するか（0=無効）", width=80)
        scf.addRow("先頭スクロールで更新（回数）:", self._scroll_top_count)
        self._scroll_bottom_count = _spin(0, 99, " 回",
            tip="末尾付近で何回ホイールスクロールしたら更新するか（0=無効）", width=80)
        scf.addRow("末尾スクロールで更新（回数）:", self._scroll_bottom_count)
        _scroll_hint = QLabel("0=無効（その方向のスクロール更新を行わない）")
        _scroll_hint.setStyleSheet("color: gray; font-size: 11px;")
        scf.addRow("", _scroll_hint)

        f0.addStretch(); nb.addTab(_scroll(w0), "全般")

        # ══════════════════════════════════════════════════════════════════
        # Tab1: スレッド
        # ══════════════════════════════════════════════════════════════════
        w1 = QWidget(); f1 = QVBoxLayout(w1)

        g_th = QGroupBox("スレッド表示"); f1.addWidget(g_th); tf = QFormLayout(g_th)
        self._pin_after_post = QCheckBox("レスしたスレを自動的にピン留めする")
        tf.addRow(self._pin_after_post)
        self._near_limit_chk = QCheckBox("最大保存数1/10以下のスレを仮赤字（薄ピンク）として扱う")
        tf.addRow(self._near_limit_chk)
        self._id_warn_count = _spin(1, 9999, " 件", width=90,
            tip="同一IDの書き込み数がこの件数以上のレスはIDを赤く表示します（基本5）")
        tf.addRow("ID件数で赤字にする閾値:", self._id_warn_count)

        # スレオープン時の表示モード
        _open_mode_items = ["返信", "画像", "引用"]
        g_open = QGroupBox("スレを開く時の表示モード"); f1.addWidget(g_open); of = QFormLayout(g_open)
        _mode_hint = QLabel("スレを開いた時にツールバーの表示モードを自動で切り替えます")
        _mode_hint.setStyleSheet("color: gray; font-size: 11px;")
        of.addRow("", _mode_hint)
        self._thread_open_mode = _combo(_open_mode_items,
            tip="スレをアクティブで開いた時に自動で切り替える表示モード\n通常=そのまま、返信=返信モード、画像=画像モード、引用=引用モード")
        of.addRow("スレを開く:", self._thread_open_mode)
        self._thread_open_bg_mode = _combo(_open_mode_items,
            tip="スレをバックグラウンドで開いた時に自動で切り替える表示モード")
        of.addRow("バックグラウンドで開く:", self._thread_open_bg_mode)

        # 画像表示モード（タブ / ウインドウ）
        g_imgmode = QGroupBox("画像表示モード"); f1.addWidget(g_imgmode)
        imf = QFormLayout(g_imgmode)
        self._image_display_mode = _combo(["タブ", "ウインドウ", "外部ブラウザ", "隣タブ"],
            tip="タブ=画像タブで開く（従来）\nウインドウ=専用の画像ウインドウで開く（1つのみ）\n"
                "外部ブラウザ=画像を常に外部ブラウザで開く（http系URLのみ／ログ内画像はタブ）\n"
                "隣タブ=画像タブを現在のタブの隣に開く")
        imf.addRow("画像表示モード:", self._image_display_mode)

        # ── 自分のレス ──
        g_self = QGroupBox("自分のレス"); f1.addWidget(g_self); sf = QFormLayout(g_self)
        self._self_res_highlight   = QCheckBox("自分のレスを青帯でハイライト表示する（新着赤より優先）")
        sf.addRow(self._self_res_highlight)
        self._self_res_sodane_notify = QCheckBox("自分のレスのそうだねが増えたときにポップアップ通知する")
        sf.addRow(self._self_res_sodane_notify)
        self._self_res_sodane_dur = _spin(500, 30000, " ms", width=120,
            tip="そうだね通知の表示時間（ms）")
        sf.addRow("　表示時間:", self._self_res_sodane_dur)
        self._self_res_reply_notify  = QCheckBox("自分のレスへの返信があった時にポップアップ通知する")
        sf.addRow(self._self_res_reply_notify)
        self._self_res_reply_dur  = _spin(500, 30000, " ms", width=120,
            tip="返信通知の表示時間（ms）")
        sf.addRow("　表示時間:", self._self_res_reply_dur)
        css_hint = QLabel(
            "ユーザーCSSで .my-sodane-popup / .my-reply-popup の"
            "background, border-color, color を指定してデザインを変更できます")
        css_hint.setStyleSheet("color:#888;font-size:8pt;")
        css_hint.setWordWrap(True)
        sf.addRow(css_hint)
        def _update_self_dur():
            self._self_res_sodane_dur.setEnabled(self._self_res_sodane_notify.isChecked())
            self._self_res_reply_dur.setEnabled(self._self_res_reply_notify.isChecked())
        self._self_res_sodane_notify.toggled.connect(lambda _: _update_self_dur())
        self._self_res_reply_notify.toggled.connect(lambda _:  _update_self_dur())

        g_post = QGroupBox("投稿設定"); f1.addWidget(g_post); pf = QFormLayout(g_post)
        key_row = QHBoxLayout()
        self._del_key = QLineEdit()
        self._del_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._del_key.setPlaceholderText("未設定の場合はランダム生成")
        key_row.addWidget(self._del_key, 1)
        self._btn_show_key = QPushButton("👁"); self._btn_show_key.setFixedWidth(30)
        self._btn_show_key.setCheckable(True)
        self._btn_show_key.toggled.connect(
            lambda on: self._del_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row.addWidget(self._btn_show_key)
        pf.addRow("削除キー:", key_row)

        f1.addStretch(); nb.addTab(_scroll(w1), "スレッド")

        # ══════════════════════════════════════════════════════════════════
        # Tab: 外観 (テーマ)  ← ウィジェット定義だけ先に作る
        # ══════════════════════════════════════════════════════════════════
        w_ap = QWidget(); f_ap = QVBoxLayout(w_ap)

        g_theme = QGroupBox("テーマ"); f_ap.addWidget(g_theme)
        tf2 = QFormLayout(g_theme)
        _theme_list = _TM.list_themes()
        self._theme_combo = _combo(_theme_list, tip="ダークモード／ライトモードを切り替えます")
        self._theme_combo.setFixedWidth(150)
        tf2.addRow("カラーテーマ:", self._theme_combo)
        _theme_hint = QLabel("テーマは theme/{テーマ名}/theme.json で定義されます。\n"
                             "フォルダを追加すると自動でリストに表示されます。")
        _theme_hint.setStyleSheet("color: gray; font-size: 11px;")
        tf2.addRow("", _theme_hint)

        def _apply_theme_now(name: str):
            _TM.load(name)
            QApplication.instance().setStyleSheet(_TM.qt_stylesheet())
        self._theme_combo.currentTextChanged.connect(_apply_theme_now)

        # パフォーマンス設定
        g_perf = QGroupBox("パフォーマンス"); f_ap.addWidget(g_perf); prf = QFormLayout(g_perf)
        self._parse_sem_kb = _spin(10, 500, " KB",
            tip="この値以上のHTMLはメモリ節約のため同時に1スレッドしかパースしません\n"
                "小さいほどメモリ安定・大きいほど更新速度向上\n"
                "目安: 低スペック=50、普通=100〜150、ハイスペック=200以上",
            width=120)
        prf.addRow("並行パース制限閾値:", self._parse_sem_kb)
        _sem_hint = QLabel("目安: 低スペック=50KB、普通=100〜150KB、ハイスペック=200KB以上\n"
                           "値を大きくすると更新速度が上がりますが、大きくしすぎるとメモリ不足で強制終了することがあります")
        _sem_hint.setStyleSheet("color: gray; font-size: 11px;")
        prf.addRow("", _sem_hint)

        # ログ（コンソール）設定
        g_log = QGroupBox("ログ"); f_ap.addWidget(g_log); lgf = QVBoxLayout(g_log)
        self._show_console = QCheckBox("ログを出力する（黒いコンソールウィンドウを表示する）")
        lgf.addWidget(self._show_console)
        _log_hint = QLabel("チェックを外すと、起動時に黒いコンソールウィンドウを非表示にします。\n"
                           "設定の反映には再起動が必要です。")
        _log_hint.setStyleSheet("color: gray; font-size: 11px;")
        _log_hint.setWordWrap(True)
        lgf.addWidget(_log_hint)

        f_ap.addStretch()
        _w_ap = _scroll(w_ap)   # 棒読みちゃんタブの後で addTab する

        # ══════════════════════════════════════════════════════════════════
        # Tab2: カタログ
        # ══════════════════════════════════════════════════════════════════
        # Tab3: ログ保存
        # ══════════════════════════════════════════════════════════════════
        w6 = QWidget(); f6 = QVBoxLayout(w6)

        g_auto = QGroupBox("スレ落ち時の自動保存"); f6.addWidget(g_auto); af = QVBoxLayout(g_auto)
        self._log_auto = QCheckBox("スレ落ちを検知したら自動保存する"); af.addWidget(self._log_auto)
        fmt_row = QHBoxLayout(); fmt_row.setContentsMargins(20, 0, 0, 0)
        self._log_auto_html = QCheckBox("HTML")
        self._log_auto_mht  = QCheckBox("MHT")
        self._log_auto_zip  = QCheckBox("ZIP")
        fmt_row.addWidget(self._log_auto_html); fmt_row.addWidget(self._log_auto_mht)
        fmt_row.addWidget(self._log_auto_zip);  fmt_row.addStretch()
        af.addLayout(fmt_row)
        self._log_auto_full = QCheckBox("1000レス到達時も自動保存する（スレ落ち・1000レス到達のどちらか先で1回）")
        af.addWidget(self._log_auto_full)
        def _toggle_auto(checked):
            for w in (self._log_auto_html, self._log_auto_mht, self._log_auto_zip):
                w.setEnabled(checked)
        self._log_auto.toggled.connect(_toggle_auto); _toggle_auto(False)

        g_log = QGroupBox("保存先・形式"); f6.addWidget(g_log); lf = QFormLayout(g_log)
        dir_row = QHBoxLayout()
        self._log_dir = QLineEdit()
        self._log_dir.setPlaceholderText("空欄 = プログラムと同じ場所の logs/ フォルダ")
        dir_row.addWidget(self._log_dir, 1)
        log_browse = QPushButton("参照…"); log_browse.setFixedWidth(70)
        def _browse_log_dir():
            p = QFileDialog.getExistingDirectory(self, "保存先フォルダを選択",
                                                 self._log_dir.text() or "")
            if p: self._log_dir.setText(p)
        log_browse.clicked.connect(_browse_log_dir)
        dir_row.addWidget(log_browse); lf.addRow("保存先:", dir_row)
        self._log_images = QCheckBox("画像を含める")
        self._log_videos = QCheckBox("動画を含める（mp4/webm）")
        self._log_uploader = QCheckBox("うｐろだも含める")
        self._log_no_thumb = QCheckBox("サムネイルを保存しない（本画像URLに差し替え）")
        lf.addRow("", self._log_images); lf.addRow("", self._log_videos)
        lf.addRow("", self._log_uploader)
        lf.addRow("", self._log_no_thumb)

        g_name = QGroupBox("ファイル命名テンプレート"); f6.addWidget(g_name); nf = QFormLayout(g_name)
        self._log_tpl = QLineEdit(); self._log_tpl.setPlaceholderText("No.{no}_{title}")
        nf.addRow("テンプレート:", self._log_tpl)
        nf.addRow(QLabel(
            "<span style='font-size:9pt;'>"
            "{no}=スレ番号　{title}=OP1行目(40文字)　{board}=板名<br>"
            "{date}=YYYYMMDD　{time}=HHMMSS　{datetime}=YYYYMMDD_HHMMSS<br>"
            "{逆NG}=マッチした逆NGワード（未マッチは空。{逆NG:文字}で未マッチ時の文字指定）"
            "</span>"))

        # ダウンロード
        g_dl = QGroupBox("ダウンロード"); f6.addWidget(g_dl); dlf = QFormLayout(g_dl)
        self._download_workers = _spin(1, 16, " 並列",
            tip="ログ保存時の画像・動画並列ダウンロード数", width=120)
        dlf.addRow("並列ダウンロード数:", self._download_workers)

        f6.addStretch(); nb.addTab(_scroll(w6), "ログ保存")

        # ══════════════════════════════════════════════════════════════════
        # Tab: キャッシュ
        # ══════════════════════════════════════════════════════════════════
        w_ca = QWidget(); f_ca = QVBoxLayout(w_ca)

        def _cache_group(title: str, note: str = ""):
            g = QGroupBox(title); f_ca.addWidget(g)
            gl = QVBoxLayout(g)
            if note:
                lbl = QLabel(note); lbl.setStyleSheet("font-size:8pt;color:#888;")
                lbl.setWordWrap(True); gl.addWidget(lbl)
            return gl

        # ── 画像キャッシュ ──
        gl_i = _cache_group("画像キャッシュ (data/img)")
        row = QHBoxLayout()
        self._cache_img_days_chk = QCheckBox("日数で削除:")
        self._cache_max_days = _spin(1, 365, " 日", width=80,
            tip="起動時に指定日数より古いファイルを自動削除")
        row.addWidget(self._cache_img_days_chk); row.addWidget(self._cache_max_days)
        row.addStretch(); gl_i.addLayout(row)
        row = QHBoxLayout()
        self._cache_img_size_chk = QCheckBox("サイズ上限:")
        self._cache_img_size_mb = _spin(1, 99999, " MB", width=100,
            tip="合計サイズが上限を超えたら古いファイルから削除")
        row.addWidget(self._cache_img_size_chk); row.addWidget(self._cache_img_size_mb)
        row.addStretch(); gl_i.addLayout(row)

        # ── 動画キャッシュ ──
        gl_v = _cache_group("動画キャッシュ (AppData/Local/2BP/video_cache)")
        row = QHBoxLayout()
        self._cache_video_days_chk = QCheckBox("日数で削除:")
        self._cache_video_days = _spin(1, 365, " 日", width=80)
        row.addWidget(self._cache_video_days_chk); row.addWidget(self._cache_video_days)
        row.addStretch(); gl_v.addLayout(row)
        row = QHBoxLayout()
        self._cache_video_size_chk = QCheckBox("サイズ上限:")
        self._cache_video_size_mb = _spin(1, 99999, " MB", width=100)
        row.addWidget(self._cache_video_size_chk); row.addWidget(self._cache_video_size_mb)
        row.addStretch(); gl_v.addLayout(row)

        # ── スレHTMLキャッシュ ──
        gl_t = _cache_group("スレHTMLキャッシュ (data/log)",
            "※ 過去ログ表示に使用します。削除すると落ちたスレのログが開けなくなります。")
        row = QHBoxLayout()
        self._cache_thread_days_chk = QCheckBox("日数で削除:")
        self._cache_thread_days = _spin(1, 365, " 日", width=80)
        row.addWidget(self._cache_thread_days_chk); row.addWidget(self._cache_thread_days)
        row.addStretch(); gl_t.addLayout(row)
        row = QHBoxLayout()
        self._cache_thread_size_chk = QCheckBox("サイズ上限:")
        self._cache_thread_size_mb = _spin(1, 99999, " MB", width=100)
        row.addWidget(self._cache_thread_size_chk); row.addWidget(self._cache_thread_size_mb)
        row.addStretch(); gl_t.addLayout(row)

        # チェックOFF時はスピナーを無効化
        for chk, sp in [
            (self._cache_img_days_chk,    self._cache_max_days),
            (self._cache_img_size_chk,    self._cache_img_size_mb),
            (self._cache_video_days_chk,  self._cache_video_days),
            (self._cache_video_size_chk,  self._cache_video_size_mb),
            (self._cache_thread_days_chk, self._cache_thread_days),
            (self._cache_thread_size_chk, self._cache_thread_size_mb),
        ]:
            chk.toggled.connect(sp.setEnabled); sp.setEnabled(False)

        # ── 使用量表示 + 手動削除 ──
        g_info = QGroupBox("使用量"); f_ca.addWidget(g_info)
        gi_lay = QVBoxLayout(g_info)
        self._cache_info_label = QLabel("計測中...")
        self._cache_info_label.setStyleSheet("font-size:9pt;")
        gi_lay.addWidget(self._cache_info_label)
        row = QHBoxLayout()
        btn_cache_recalc = QPushButton("再計測")
        btn_cache_recalc.clicked.connect(self._update_cache_info)
        row.addWidget(btn_cache_recalc)
        btn_clear_cache = QPushButton("今すぐ削除")
        btn_clear_cache.setToolTip("上記の設定に従って今すぐクリーンアップを実行します")
        btn_clear_cache.clicked.connect(self._clear_cache_now)
        row.addWidget(btn_clear_cache)
        row.addStretch(); gi_lay.addLayout(row)
        note = QLabel("※ クリーンアップは起動時にも自動実行されます。チェックが両方OFFの種別は削除されません。")
        note.setStyleSheet("font-size:8pt;color:#888;"); note.setWordWrap(True)
        f_ca.addWidget(note)

        f_ca.addStretch(); nb.addTab(_scroll(w_ca), "キャッシュ")

        # ══════════════════════════════════════════════════════════════════
        # Tab4: アップローダー
        # ══════════════════════════════════════════════════════════════════
        w7 = QWidget(); f7 = QVBoxLayout(w7)

        g_ul = QGroupBox("アップローダーリンク一覧"); f7.addWidget(g_ul, 1)
        ulf = QVBoxLayout(g_ul)
        self._ul_list = QListWidget(); ulf.addWidget(self._ul_list)
        ul_btns = QHBoxLayout()
        ul_add = QPushButton("追加"); ul_del = QPushButton("削除")
        ul_up  = QPushButton("↑");   ul_dn  = QPushButton("↓")
        ul_add.setFixedWidth(60); ul_del.setFixedWidth(60)
        ul_up.setFixedWidth(30);  ul_dn.setFixedWidth(30)
        ul_btns.addWidget(ul_add); ul_btns.addWidget(ul_del)
        ul_btns.addWidget(ul_up);  ul_btns.addWidget(ul_dn)
        ul_btns.addStretch(); ulf.addLayout(ul_btns)
        form_ul = QFormLayout()
        self._ul_name    = QLineEdit(); form_ul.addRow("名前:", self._ul_name)
        self._ul_pattern = QLineEdit(); form_ul.addRow("パターン(正規表現):", self._ul_pattern)
        self._ul_url     = QLineEdit(); form_ul.addRow("URL ($MATCHで置換):", self._ul_url)
        ulf.addLayout(form_ul)

        def _ul_sel():
            row = self._ul_list.currentRow()
            uls = getattr(self._settings, "uploader_links", [])
            if 0 <= row < len(uls):
                u = uls[row]
                self._ul_name.setText(u.get("name",""))
                self._ul_pattern.setText(u.get("pattern",""))
                self._ul_url.setText(u.get("url",""))
        self._ul_list.currentRowChanged.connect(_ul_sel)

        def _ul_add():
            name = self._ul_name.text().strip()
            pat  = self._ul_pattern.text().strip()
            url  = self._ul_url.text().strip()
            if not (name and pat and url): return
            self._settings.uploader_links.append(
                {"name": name, "pattern": pat, "url": url, "popup": True, "new_tab": False})
            self._ul_list.addItem(f"{name}  [{pat}]")

        def _ul_del():
            row = self._ul_list.currentRow()
            uls = getattr(self._settings, "uploader_links", [])
            if 0 <= row < len(uls):
                del uls[row]; self._ul_list.takeItem(row)

        def _ul_move(delta):
            row = self._ul_list.currentRow()
            uls = getattr(self._settings, "uploader_links", [])
            nrow = row + delta
            if 0 <= row < len(uls) and 0 <= nrow < len(uls):
                uls[row], uls[nrow] = uls[nrow], uls[row]
                item = self._ul_list.takeItem(row)
                self._ul_list.insertItem(nrow, item)
                self._ul_list.setCurrentRow(nrow)

        ul_add.clicked.connect(_ul_add); ul_del.clicked.connect(_ul_del)
        ul_up.clicked.connect(lambda: _ul_move(-1))
        ul_dn.clicked.connect(lambda: _ul_move(1))

        nb.addTab(w7, "うｐろだ")

        # ══════════════════════════════════════════════════════════════════
        # Tab5: 画像保存
        # ══════════════════════════════════════════════════════════════════
        w8 = QWidget(); f8 = QVBoxLayout(w8)

        g_isf = QGroupBox("画像保存パネル"); f8.addWidget(g_isf, 1)
        isf_lay = QVBoxLayout(g_isf)

        # フォルダリスト
        self._isf_list = QListWidget()
        self._isf_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        isf_lay.addWidget(self._isf_list)

        # 追加・削除ボタン行
        isf_btn_row = QHBoxLayout()
        isf_add_btn = QPushButton("追加..."); isf_add_btn.setFixedWidth(80)
        isf_del_btn = QPushButton("削除");    isf_del_btn.setFixedWidth(60)
        isf_btn_row.addWidget(isf_add_btn); isf_btn_row.addWidget(isf_del_btn)
        isf_btn_row.addStretch(); isf_lay.addLayout(isf_btn_row)

        # ↑↓ボタン + 「上が表示する▼」ラベル
        isf_ud_row = QHBoxLayout()
        isf_up_btn = QPushButton("↑"); isf_up_btn.setFixedWidth(30)
        isf_dn_btn = QPushButton("↓"); isf_dn_btn.setFixedWidth(30)
        isf_ud_row.addStretch(); isf_ud_row.addWidget(isf_up_btn); isf_ud_row.addWidget(isf_dn_btn)
        isf_lay.addLayout(isf_ud_row)
        isf_lay.addWidget(QLabel("上が先頭（デフォルト保存先）"))

        # 折り返し・表示文字数 + インポートボタン（右寄せ）
        isf_form = QFormLayout()
        self._isf_wrap  = _spin(1, 20, tip="ボタンを何列で折り返すか", width=60)
        self._isf_chars = _spin(0, 80, tip="0=全表示", width=60)
        isf_form.addRow("折り返し列数:", self._isf_wrap)
        _chars_row = QHBoxLayout()
        _chars_row.addWidget(self._isf_chars)
        _chars_row.addStretch()
        isf_ini_btn = QPushButton("旧2B からフォルダ一覧をインポート")
        isf_ini_btn.clicked.connect(self._import_nijivb_ini)
        _chars_row.addWidget(isf_ini_btn)
        isf_form.addRow("表示文字数 (0=全表示):", _chars_row)
        f8.addLayout(isf_form)
        f8.addStretch()

        # イベント接続
        def _isf_add():
            p = QFileDialog.getExistingDirectory(self, "フォルダを追加", "")
            if p:
                # 重複チェック
                for i in range(self._isf_list.count()):
                    if self._isf_list.item(i).text() == p:
                        return
                self._isf_list.addItem(p)
        def _isf_del():
            row = self._isf_list.currentRow()
            if row >= 0:
                self._isf_list.takeItem(row)
        def _isf_move(delta):
            row = self._isf_list.currentRow()
            nrow = row + delta
            if 0 <= row < self._isf_list.count() and 0 <= nrow < self._isf_list.count():
                item = self._isf_list.takeItem(row)
                self._isf_list.insertItem(nrow, item)
                self._isf_list.setCurrentRow(nrow)
        isf_add_btn.clicked.connect(_isf_add)
        isf_del_btn.clicked.connect(_isf_del)
        isf_up_btn.clicked.connect(lambda: _isf_move(-1))
        isf_dn_btn.clicked.connect(lambda: _isf_move(1))

        nb.addTab(_scroll(w8), "画像保存")

        # ══════════════════════════════════════════════════════════════════
        # Tab6: 棒読みちゃん
        # ══════════════════════════════════════════════════════════════════
        w9 = QWidget(); f9 = QVBoxLayout(w9)

        g_by = QGroupBox("棒読みちゃん連携"); f9.addWidget(g_by)
        byf = QFormLayout(g_by)
        self._by_enabled = QCheckBox("棒読みちゃんで読み上げを有効にする")
        byf.addRow(self._by_enabled)
        self._by_host = QLineEdit(); self._by_host.setFixedWidth(160)
        byf.addRow("ホスト:", self._by_host)
        self._by_port = _spin(1, 65535, width=80)
        byf.addRow("ポート:", self._by_port)
        self._by_speed  = _spin(-1, 200, " (-1=既定)", width=130)
        self._by_tone   = _spin(-1, 200, " (-1=既定)", width=130)
        self._by_volume = _spin(-1, 200, " (-1=既定)", width=130)
        byf.addRow("速さ:", self._by_speed)
        byf.addRow("音程:", self._by_tone)
        byf.addRow("音量:", self._by_volume)
        self._by_voice = _spin(0, 20, " (0=デフォルト)", width=155)
        byf.addRow("声質:", self._by_voice)

        g_by_tpl = QGroupBox("新着読み上げテンプレート"); f9.addWidget(g_by_tpl)
        tpl_lay = QVBoxLayout(g_by_tpl)
        self._by_format = QLineEdit()
        self._by_format.setPlaceholderText("{comment}")
        self._by_format.setToolTip(
            "新着レスの読み上げテキストテンプレート\n"
            "{comment} … コメント本文（引用行を除く、先頭100文字）\n"
            "{comment_res} … コメント本文（引用行を含む、先頭100文字）\n"
            "{name} … 名前  {no} … レス番号")
        tpl_lay.addWidget(self._by_format)
        fmt_hint = QLabel(
            "{comment}（引用除く）  {comment_res}（引用含む）  {name}  {no}  が使用できます")
        fmt_hint.setStyleSheet("color:#888;font-size:8pt;")
        fmt_hint.setWordWrap(True)
        tpl_lay.addWidget(fmt_hint)

        g_by_test = QGroupBox("テスト送信"); f9.addWidget(g_by_test)
        test_lay = QHBoxLayout(g_by_test)
        self._by_test_text = QLineEdit("棒読みちゃんのテストです")
        btn_by_test = QPushButton("送信テスト")
        btn_by_test.setFixedWidth(90)
        btn_by_test.clicked.connect(self._bouyomi_test)
        test_lay.addWidget(self._by_test_text, 1)
        test_lay.addWidget(btn_by_test)
        self._by_test_result = QLabel("")
        self._by_test_result.setStyleSheet("font-size:9pt;")
        test_lay.addWidget(self._by_test_result)

        g_by_note = QGroupBox("使い方"); f9.addWidget(g_by_note)
        note_lay = QVBoxLayout(g_by_note)
        note_lbl = QLabel(
            "棒読みちゃん（http://chi.usamimi.info/Program/Application/BouyomiChan/）を\n"
            "起動してから、自動更新ダイアログの「棒読みちゃん」チェックをONにすると\n"
            "新着レスを読み上げます。")
        note_lbl.setWordWrap(True)
        note_lay.addWidget(note_lbl)

        f9.addStretch(); nb.addTab(_scroll(w9), "棒読みちゃん")

        # 外観タブ（棒読みちゃんの右）
        nb.addTab(_w_ap, "その他")

        # ══════════════════════════════════════════════════════════════════
        # Tab: ショートカット
        # ══════════════════════════════════════════════════════════════════
        w_sc = QWidget(); f_sc = QVBoxLayout(w_sc)

        _sc_hint = QLabel(
            "キーシーケンスを入力してください（例: Ctrl+D, F5, Shift+F2）\n"
            "空欄にするとデフォルトのキーに戻ります。変更は再起動不要で即時反映されます。")
        _sc_hint.setStyleSheet("color: gray; font-size: 11px;")
        _sc_hint.setWordWrap(True)
        f_sc.addWidget(_sc_hint)

        # アクション定義: (action_id, 表示名, デフォルトキー)
        _SC_DEFS = [
            # ── メニュー ──────────────────────────────────────────────────────
            ("catalog",         "カタログ表示",                     "F9"),
            ("refresh_board",   "この板の更新",                     "F6"),
            ("refresh_current", "このビューの更新",                 "F5"),
            ("reply",           "返信ダイアログを開く",             "Ctrl+D"),
            ("close_tab",       "このビューを閉じる",               "Ctrl+W"),
            ("reopen_tab",      "閉じたタブを開き直す",             "Ctrl+Shift+T"),
            ("find_in_view",    "ページ内検索",                     "Ctrl+F"),
            ("extract_focus",   "抽出フィールドにフォーカス",       "Ctrl+Shift+F"),
            ("open_log",        "ログを開く",                       "Ctrl+Shift+O"),
            ("toggle_tree",     "板ツリー表示切替",                 "F2"),
            ("toggle_history",  "スレッド履歴表示切替",             "Shift+F2"),
            ("exit",            "終了",                             "Alt+F4"),
            # ── 板ペイン内スクロール ──────────────────────────────────────────
            ("scroll_top",      "ページ先頭へ移動",                 "Alt+Up"),
            ("scroll_bottom",   "ページ末尾へ移動",                 "Alt+Down"),
            ("scroll_new",      "新着の先頭に移動",                 "Alt+G"),
            ("scroll_prev_pos", "前回のレス位置に移動",             "Alt+H"),
            ("scroll_prev_bm",  "前のしおりへ",                     "Alt+B"),
            ("scroll_next_bm",  "次のしおりへ",                     "Alt+V"),
        ]

        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        _sc_table = QTableWidget(len(_SC_DEFS), 3)
        _sc_table.setHorizontalHeaderLabels(["アクション", "デフォルト", "カスタムキー"])
        _sc_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        _sc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        _sc_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        _sc_table.verticalHeader().setVisible(False)
        _sc_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        _sc_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked |
                                  QTableWidget.EditTrigger.SelectedClicked)

        self._sc_defs = _SC_DEFS
        self._sc_table = _sc_table
        for row, (aid, label, default) in enumerate(_SC_DEFS):
            lbl_item = QTableWidgetItem(label)
            lbl_item.setFlags(lbl_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            def_item = QTableWidgetItem(default)
            def_item.setFlags(def_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            def_item.setForeground(QColor(_TM.ui("text_muted", "#888")))
            _sc_table.setItem(row, 0, lbl_item)
            _sc_table.setItem(row, 1, def_item)
            _sc_table.setItem(row, 2, QTableWidgetItem(""))  # カスタムキー（_loadで埋める）

        f_sc.addWidget(_sc_table, 1)

        _sc_reset_btn = QPushButton("全てデフォルトに戻す")
        _sc_reset_btn.setFixedWidth(180)
        def _reset_shortcuts():
            for row in range(_sc_table.rowCount()):
                _sc_table.item(row, 2).setText("")
        _sc_reset_btn.clicked.connect(_reset_shortcuts)
        _sc_btn_lay = QHBoxLayout()
        _sc_btn_lay.addWidget(_sc_reset_btn)
        _sc_btn_lay.addStretch()
        f_sc.addLayout(_sc_btn_lay)

        nb.addTab(w_sc, "ショートカット")
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ──────────────────────────────────────────────────────────────────────
    def _load(self):
        s = self._settings

        # スレッド
        self._pin_after_post.setChecked(getattr(s, "pin_after_post", False))
        self._near_limit_chk.setChecked(getattr(s, "treat_near_limit_as_expiring", False))
        self._scroll_bottom_count.setValue(getattr(s, "scroll_bottom_count", 30))
        self._scroll_top_count.setValue(getattr(s, "scroll_top_count", 0))
        self._parse_sem_kb.setValue(getattr(s, "parse_sem_kb", 50))
        self._show_console.setChecked(getattr(s, "show_console", False))
        self._tab_max_width.setValue(getattr(s, "tab_max_width", 0))
        self._tab_pink_op_no_id.setChecked(getattr(s, "tab_pink_op_no_id", False))
        self._tab_orange_quarantine.setChecked(getattr(s, "tab_orange_quarantine", True))
        self._image_mode_cols.setValue(getattr(s, "image_mode_cols", 6))
        self._recent_closed_max.setValue(getattr(s, "recent_closed_max", 30))
        self._recent_images_max.setValue(getattr(s, "recent_images_max", 30))
        self._id_warn_count.setValue(getattr(s, "id_warn_count", 5))
        self._cache_max_days.setValue(max(1, getattr(s, "cache_max_days", 7)))
        self._cache_img_days_chk.setChecked(
            getattr(s, "cache_img_days_enabled", True) and getattr(s, "cache_max_days", 7) > 0)
        self._cache_img_size_chk.setChecked(getattr(s, "cache_img_size_enabled", False))
        self._cache_img_size_mb.setValue(getattr(s, "cache_img_size_mb", 500))
        self._cache_video_days_chk.setChecked(getattr(s, "cache_video_days_enabled", True))
        self._cache_video_days.setValue(getattr(s, "cache_video_days", 3))
        self._cache_video_size_chk.setChecked(getattr(s, "cache_video_size_enabled", False))
        self._cache_video_size_mb.setValue(getattr(s, "cache_video_size_mb", 1024))
        self._cache_thread_days_chk.setChecked(getattr(s, "cache_thread_days_enabled", False))
        self._cache_thread_days.setValue(getattr(s, "cache_thread_days", 30))
        self._cache_thread_size_chk.setChecked(getattr(s, "cache_thread_size_enabled", False))
        self._cache_thread_size_mb.setValue(getattr(s, "cache_thread_size_mb", 200))
        self._update_cache_info()
        self._self_res_highlight.setChecked(getattr(s, "self_res_highlight",        True))
        self._self_res_sodane_notify.setChecked(getattr(s, "self_res_sodane_notify",True))
        self._self_res_sodane_dur.setValue(getattr(s, "self_res_sodane_duration",   5000))
        self._self_res_reply_notify.setChecked(getattr(s, "self_res_reply_notify",  True))
        self._self_res_reply_dur.setValue(getattr(s, "self_res_reply_duration",     5000))
        self._self_res_sodane_dur.setEnabled(self._self_res_sodane_notify.isChecked())
        self._self_res_reply_dur.setEnabled(self._self_res_reply_notify.isChecked())

        # レス・投稿
        self._del_key.setText(s.delete_key or "")
        self._download_workers.setValue(getattr(s, "download_workers", 4))
        self._thread_open_mode.setCurrentIndex(getattr(s, 'thread_open_mode', 0))
        self._thread_open_bg_mode.setCurrentIndex(getattr(s, 'thread_open_bg_mode', 0))
        self._image_display_mode.setCurrentIndex(getattr(s, 'image_display_mode', 0))
        self._cat_hover_zoom.setChecked(getattr(s, "catalog_hover_zoom", False))
        self._cat_hover_comment.setChecked(getattr(s, "catalog_hover_comment", False))
        self._cat_mail_badge.setChecked(getattr(s, "catalog_show_mail_badge", True))
        self._cat_quarantine.setChecked(getattr(s, "catalog_quarantine_bottom", True))
        self._cat_common_id_bottom.setChecked(getattr(s, "catalog_common_id_bottom", False))

        # 外観
        _theme_idx = self._theme_combo.findText(_TM.name())
        if _theme_idx < 0:
            _theme_idx = self._theme_combo.findText(getattr(s, "theme", "dark"))
        # 初期値セットでは _apply_theme_now を発火させない
        # （発火すると設定を開く度に app.setStyleSheet で全体が再ポリッシュされ重くなる）
        self._theme_combo.blockSignals(True)
        self._theme_combo.setCurrentIndex(max(0, _theme_idx))
        self._theme_combo.blockSignals(False)

        # ログ保存
        self._log_dir.setText(getattr(s, "log_save_dir", ""))
        self._log_images.setChecked(getattr(s, "log_save_images", True))
        self._log_videos.setChecked(getattr(s, "log_save_videos", True))
        self._log_uploader.setChecked(getattr(s, "log_save_uploader", True))
        self._log_no_thumb.setChecked(getattr(s, "log_save_no_thumb", False))
        self._log_tpl.setText(getattr(s, "log_filename_template", "{date}/{date}_No.{no}_{title}"))
        auto = getattr(s, "log_auto_save", False)
        self._log_auto.setChecked(auto)
        self._log_auto_html.setChecked(getattr(s, "log_auto_save_html", True))
        self._log_auto_mht.setChecked(getattr(s,  "log_auto_save_mht",  True))
        self._log_auto_zip.setChecked(getattr(s,  "log_auto_save_zip",  True))
        self._log_auto_full.setChecked(getattr(s, "log_auto_save_full", False))
        for w in (self._log_auto_html, self._log_auto_mht, self._log_auto_zip):
            w.setEnabled(auto)
        close_en = getattr(s, "auto_close_dead_tab", False)
        self._auto_close.setChecked(close_en)
        self._auto_close_full.setChecked(getattr(s, "auto_close_full_tab", False))
        self._auto_close_skip_pinned.setChecked(getattr(s, "auto_close_skip_pinned", False))
        self._auto_close_skip_pinned.setEnabled(close_en or self._auto_close_full.isChecked())

        # アップローダー
        self._ul_list.clear()
        for ul in getattr(s, "uploader_links", []):
            self._ul_list.addItem(f"{ul.get('name','')}  [{ul.get('pattern','')}]")

        # 画像保存
        self._isf_list.clear()
        for f in getattr(s, "image_save_folders", []):
            self._isf_list.addItem(f)
        self._isf_wrap.setValue(getattr(s, "image_save_btn_wrap", 3))
        self._isf_chars.setValue(getattr(s, "image_save_label_len", 0))

        # 棒読みちゃん
        self._by_enabled.setChecked(getattr(s, "bouyomi_enabled", False))
        self._by_host.setText(getattr(s, "bouyomi_host", "localhost"))
        self._by_port.setValue(getattr(s, "bouyomi_port", 50080))
        self._by_speed.setValue(getattr(s, "bouyomi_speed", -1))
        self._by_tone.setValue(getattr(s, "bouyomi_tone", -1))
        self._by_volume.setValue(getattr(s, "bouyomi_volume", -1))
        self._by_voice.setValue(getattr(s, "bouyomi_voice", 0))
        self._by_format.setText(getattr(s, "bouyomi_format", "{comment}"))

        # ショートカット
        _saved_sc = getattr(s, "shortcuts", {})
        for row, (aid, label, default) in enumerate(self._sc_defs):
            custom = _saved_sc.get(aid, "")
            self._sc_table.item(row, 2).setText(custom)

    # ──────────────────────────────────────────────────────────────────────
    def _import_nijivb_ini(self):
        """NijiVb32.ini を選択して画像保存フォルダをインポートする"""
        path, _ = QFileDialog.getOpenFileName(
            self, "NijiVb32.ini を選択", "",
            "INI ファイル (NijiVb32.ini);;すべてのファイル (*)")
        if not path:
            return
        try:
            folders = _parse_imagesave_ini(path)
        except Exception as e:
            QMessageBox.critical(self, "読み込みエラー",
                                 f"NijiVb32.ini の解析に失敗しました。\n{e}")
            return
        if not folders:
            QMessageBox.information(self, "インポート",
                                    "[IMAGESAVE] セクションにフォルダが見つかりませんでした。")
            return
        existing = {self._isf_list.item(i).text()
                    for i in range(self._isf_list.count())}
        added = 0
        for folder in folders:
            if folder not in existing:
                self._isf_list.addItem(folder)
                existing.add(folder)
                added += 1
        skipped = len(folders) - added
        msg = f"画像保存フォルダ {added} 件をインポートしました。"
        if skipped:
            msg += f"（重複 {skipped} 件スキップ）"
        QMessageBox.information(self, "インポート完了", msg)

    # ──────────────────────────────────────────────────────────────────────
    def _ok(self):
        s = self._settings

        # スレッド
        s.pin_after_post          = self._pin_after_post.isChecked()
        s.treat_near_limit_as_expiring = self._near_limit_chk.isChecked()
        s.scroll_bottom_count     = self._scroll_bottom_count.value()
        s.scroll_top_count        = self._scroll_top_count.value()
        s.parse_sem_kb            = self._parse_sem_kb.value()
        s.show_console            = self._show_console.isChecked()
        s.tab_max_width           = self._tab_max_width.value()
        s.tab_pink_op_no_id       = self._tab_pink_op_no_id.isChecked()
        s.tab_orange_quarantine   = self._tab_orange_quarantine.isChecked()
        s.image_mode_cols         = self._image_mode_cols.value()
        s.recent_closed_max       = self._recent_closed_max.value()
        s.recent_images_max       = self._recent_images_max.value()
        s.id_warn_count           = self._id_warn_count.value()
        s.cache_max_days          = self._cache_max_days.value()
        s.cache_img_days_enabled    = self._cache_img_days_chk.isChecked()
        s.cache_img_size_enabled    = self._cache_img_size_chk.isChecked()
        s.cache_img_size_mb         = self._cache_img_size_mb.value()
        s.cache_video_days_enabled  = self._cache_video_days_chk.isChecked()
        s.cache_video_days          = self._cache_video_days.value()
        s.cache_video_size_enabled  = self._cache_video_size_chk.isChecked()
        s.cache_video_size_mb       = self._cache_video_size_mb.value()
        s.cache_thread_days_enabled = self._cache_thread_days_chk.isChecked()
        s.cache_thread_days         = self._cache_thread_days.value()
        s.cache_thread_size_enabled = self._cache_thread_size_chk.isChecked()
        s.cache_thread_size_mb      = self._cache_thread_size_mb.value()
        s.self_res_highlight        = self._self_res_highlight.isChecked()
        s.self_res_sodane_notify    = self._self_res_sodane_notify.isChecked()
        s.self_res_sodane_duration  = self._self_res_sodane_dur.value()
        s.self_res_reply_notify     = self._self_res_reply_notify.isChecked()
        s.self_res_reply_duration   = self._self_res_reply_dur.value()

        # レス・投稿
        s.delete_key              = self._del_key.text()
        s.download_workers        = self._download_workers.value()
        s.thread_open_mode        = self._thread_open_mode.currentIndex()
        s.thread_open_bg_mode     = self._thread_open_bg_mode.currentIndex()
        s.image_display_mode      = self._image_display_mode.currentIndex()
        s.catalog_hover_zoom      = self._cat_hover_zoom.isChecked()
        s.catalog_hover_comment   = self._cat_hover_comment.isChecked()
        s.catalog_show_mail_badge  = self._cat_mail_badge.isChecked()
        s.catalog_quarantine_bottom = self._cat_quarantine.isChecked()
        s.catalog_common_id_bottom = self._cat_common_id_bottom.isChecked()
        s.catalog_show_email      = False  # メール欄バッジは常にOFF

        # 外観
        s.theme                   = self._theme_combo.currentText()

        # ログ保存
        s.log_save_dir            = self._log_dir.text().strip()
        s.log_save_images         = self._log_images.isChecked()
        s.log_save_videos         = self._log_videos.isChecked()
        s.log_save_uploader       = self._log_uploader.isChecked()
        s.log_save_no_thumb       = self._log_no_thumb.isChecked()
        tpl = self._log_tpl.text().strip()
        s.log_filename_template   = tpl if tpl else "{date}/{date}_No.{no}_{title}"
        s.log_auto_save           = self._log_auto.isChecked()
        s.log_auto_save_html      = self._log_auto_html.isChecked()
        s.log_auto_save_mht       = self._log_auto_mht.isChecked()
        s.log_auto_save_zip       = self._log_auto_zip.isChecked()
        s.log_auto_save_full      = self._log_auto_full.isChecked()
        s.auto_close_dead_tab     = self._auto_close.isChecked()
        s.auto_close_full_tab     = self._auto_close_full.isChecked()
        s.auto_close_skip_pinned  = self._auto_close_skip_pinned.isChecked()

        # 画像保存
        s.image_save_folders  = [self._isf_list.item(i).text()
                                  for i in range(self._isf_list.count())]
        s.image_save_btn_wrap  = self._isf_wrap.value()
        s.image_save_label_len = self._isf_chars.value()

        # 棒読みちゃん
        s.bouyomi_enabled = self._by_enabled.isChecked()
        s.bouyomi_host    = self._by_host.text().strip() or "localhost"
        s.bouyomi_port    = self._by_port.value()
        s.bouyomi_speed   = self._by_speed.value()
        s.bouyomi_tone    = self._by_tone.value()
        s.bouyomi_volume  = self._by_volume.value()
        s.bouyomi_voice   = self._by_voice.value()
        s.bouyomi_format  = self._by_format.text().strip() or "{comment}"

        # ショートカット
        sc_map = {}
        for row, (aid, label, default) in enumerate(self._sc_defs):
            val = self._sc_table.item(row, 2).text().strip()
            if val:
                sc_map[aid] = val
        s.shortcuts = sc_map

        s.save()
        if self._on_apply: self._on_apply()
        self.accept()

    def _bouyomi_test(self):
        """棒読みちゃんにテスト送信"""
        host = self._by_host.text().strip() or "localhost"
        port = self._by_port.value()
        text = self._by_test_text.text()
        speed  = self._by_speed.value()
        tone   = self._by_tone.value()
        volume = self._by_volume.value()
        voice  = self._by_voice.value()
        try:
            import urllib.request, urllib.parse
            params = urllib.parse.urlencode({
                "text": text, "speed": speed, "tone": tone,
                "volume": volume, "voice": voice,
            })
            url = f"http://{host}:{port}/Talk?{params}"
            with urllib.request.urlopen(url, timeout=3) as r:
                _ = r.read()
            self._by_test_result.setText("✓ 送信成功")
            self._by_test_result.setStyleSheet("font-size:9pt;color:green;")
        except Exception as e:
            self._by_test_result.setText(f"✗ {e}")
            self._by_test_result.setStyleSheet("font-size:9pt;color:red;")

    @staticmethod
    def _fmt_size(sz: int) -> str:
        if sz >= 1073741824:
            return f"{sz/1073741824:.1f} GB"
        if sz >= 1048576:
            return f"{sz/1048576:.0f} MB"
        return f"{sz/1024:.0f} KB"

    def _update_cache_info(self):
        """画像・動画・スレHTMLキャッシュの現在サイズをBGスレッドで計測して表示"""
        import threading
        self._cache_info_label.setText("計測中...")
        def _calc():
            from futaba2b_network import (get_dir_size, IMAGE_CACHE_DIR,
                                          VIDEO_CACHE_DIR, THREAD_CACHE_DIR)
            parts = []
            for label, d in [("画像", IMAGE_CACHE_DIR),
                             ("動画", VIDEO_CACHE_DIR),
                             ("スレHTML", THREAD_CACHE_DIR)]:
                cnt, sz = get_dir_size(d)
                parts.append(f"{label}: {cnt}件 / {self._fmt_size(sz)}")
            txt = "　".join(parts)
            from PySide6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
            QMetaObject.invokeMethod(
                self._cache_info_label, "setText", _Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, txt))
        threading.Thread(target=_calc, daemon=True).start()

    def _clear_cache_now(self):
        """「今すぐ削除」ボタン：UI上の設定に従って3種別をクリーンアップ"""
        from futaba2b_network import (cleanup_cache_dir, IMAGE_CACHE_DIR,
                                      VIDEO_CACHE_DIR, THREAD_CACHE_DIR)
        jobs = [
            ("画像", IMAGE_CACHE_DIR,
             self._cache_max_days.value()    if self._cache_img_days_chk.isChecked()    else 0,
             self._cache_img_size_mb.value() if self._cache_img_size_chk.isChecked()    else 0),
            ("動画", VIDEO_CACHE_DIR,
             self._cache_video_days.value()    if self._cache_video_days_chk.isChecked() else 0,
             self._cache_video_size_mb.value() if self._cache_video_size_chk.isChecked() else 0),
            ("スレHTML", THREAD_CACHE_DIR,
             self._cache_thread_days.value()    if self._cache_thread_days_chk.isChecked() else 0,
             self._cache_thread_size_mb.value() if self._cache_thread_size_chk.isChecked() else 0),
        ]
        if all(d == 0 and s == 0 for _, _, d, s in jobs):
            self._cache_info_label.setText("削除条件が設定されていません（チェックボックスをONにしてください）")
            return
        parts = []
        for label, cdir, days, size_mb in jobs:
            if days == 0 and size_mb == 0:
                continue
            cnt, sz = cleanup_cache_dir(cdir, max_days=days,
                                        max_bytes=size_mb * 1048576)
            parts.append(f"{label}: {cnt}件 ({self._fmt_size(sz)}) 削除")
        self._cache_info_label.setText("　".join(parts))
        # 2秒後に使用量を再計測して表示を更新
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, self._update_cache_info)



# ══════════════════════════════════════════════════════════════════════════════
# 旧2B Filter.ini インポートダイアログ
# ══════════════════════════════════════════════════════════════════════════════

def _parse_imagesave_ini(path: str) -> list[str]:
    """NijiVb32.ini の [IMAGESAVE] セクションからフォルダリストを返す。
    数値キー（1=, 2=, ...）の値のみ取得。DISP/POSITION/CLOSE/COL/NAMELEN は無視。
    """
    import os
    for enc in ("cp932", "utf-8"):
        try:
            raw = open(path, encoding=enc).read()
            break
        except (UnicodeDecodeError, FileNotFoundError):
            raw = None
    if not raw:
        return []

    m = re.search(r"\[IMAGESAVE\](.*?)(?=^\[|\Z)", raw, re.DOTALL | re.MULTILINE)
    if not m:
        return []

    folders: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if not key.strip().isdigit():
            continue  # DISP/COL 等をスキップ
        folder = val.strip()
        if folder:
            folders.append(folder)
    return folders


def _parse_filter_ini(path: str) -> tuple[list[dict], list[dict]]:
    """Filter.ini を解析して (ng_words, ng_images) を返す。
    いずれかのセクションが存在しない場合は空リストを返す。
    """
    try:
        raw = open(path, encoding="cp932").read()
    except UnicodeDecodeError:
        raw = open(path, encoding="utf-8", errors="replace").read()

    # ──── NGワード ────
    # scope ビットフラグ（旧2B実測値から推定）
    # bit0=本文, bit1=名前, bit2=メール, bit3=件名, bit4=ID, bit5=IP, bit6=カタログ
    def _scope_flags(scope_val: str) -> dict:
        try:
            v = int(scope_val)
        except (ValueError, TypeError):
            v = 1  # デフォルト: 本文のみ
        return {
            "scope_body":     bool(v & 0x01),
            "scope_name":     bool(v & 0x02),
            "scope_mail":     bool(v & 0x04),
            "scope_subject":  bool(v & 0x08),
            "scope_id":       bool(v & 0x40),
            "scope_ip":       bool(v & 0x20),
            "scope_catalog":  bool(v & 0x10),
        }

    _type_map = {"0": "ng", "1": "reverse_ng", "2": "replace", "3": "mow_replace"}

    ng_words: list[dict] = []
    m = re.search(r"\[FILTER_RESNGWORD\](.*?)(?=^\[|\Z)", raw,
                  re.DOTALL | re.MULTILINE)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            _, v = line.split("=", 1)
            parts = v.split("\x01")
            if len(parts) < 2:
                continue
            pattern = parts[1].strip()
            if not pattern:
                continue
            enabled   = parts[2].strip().lower() == "true" if len(parts) > 2 else True
            scope_str = parts[3].strip()              if len(parts) > 3 else "1"
            ng_type   = _type_map.get(parts[5].strip(), "ng") if len(parts) > 5 else "ng"
            replace   = parts[6].strip()              if len(parts) > 6 else ""
            entry = {
                "pattern":      pattern,
                "is_regex":     True,
                "enabled":      enabled,
                "ng_type":      ng_type,
                "replace_str":  replace,
                "expires":      "無制限",
                "expires_at":   "",
            }
            entry.update(_scope_flags(scope_str))
            ng_words.append(entry)

    # ──── NG画像 ────
    # フォーマット(14フィールド):
    # [0]id [1]? [2]w [3]h [4]size [5]size2 [6]? [7]enabled [8]type [9]?
    # [10]md5 [11]date [12]? [13]is_reverse
    ng_images: list[dict] = []
    m2 = re.search(r"\[FILTER_NGIMAGE\](.*?)(?=^\[|\Z)", raw,
                   re.DOTALL | re.MULTILINE)
    if m2:
        for line in m2.group(1).splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            _, v = line.split("=", 1)
            parts = v.split("\x01")
            if len(parts) < 11:
                continue
            md5 = parts[10].strip()
            if not md5:
                continue
            enabled    = parts[7].strip().lower() == "true" if len(parts) > 7  else True
            date_str   = parts[11].strip()                  if len(parts) > 11 else ""
            is_rev     = parts[13].strip().lower() == "true" if len(parts) > 13 else False
            try: w = int(parts[2]); h = int(parts[3])
            except ValueError: w = h = 0
            try: sz = int(parts[4])
            except ValueError: sz = 0
            ng_images.append({
                "enabled":      enabled,
                "method":       "md5",
                "image_type":   "",
                "width":        w,
                "height":       h,
                "size_min":     sz,
                "size_max":     sz,
                "file_path":    "",
                "md5":          md5,
                "last_hit":     "",
                "expires":      "無制限",
                "expires_at":   "",
                "is_reverse_ng": is_rev,
                "description":  f"旧2Bインポート {date_str}".strip(),
            })

    return ng_words, ng_images


class ImportIniDialog(QDialog):
    """旧2B INI → NGワード・NG画像・画像保存フォルダ インポートダイアログ"""

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("旧2B INI インポート")
        self.resize(700, 540)
        self._settings        = settings
        self._ng_words:       list[dict] = []
        self._ng_images:      list[dict] = []
        self._build()

    # ──────────────────────────────────────────────────────────────────────────
    def _build(self):
        lay = QVBoxLayout(self)

        # ── ファイル選択行 ──
        file_row = QHBoxLayout()
        self._path_edit = QLineEdit(); self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("Filter.ini を選択")
        btn_browse = QPushButton("参照…")
        btn_browse.clicked.connect(self._browse)
        file_row.addWidget(QLabel("Filter.ini:")); file_row.addWidget(self._path_edit, 1)
        file_row.addWidget(btn_browse)
        lay.addLayout(file_row)

        # ── オプション行 ──
        opt_row = QHBoxLayout()
        self._chk_skip_dup = QCheckBox("重複をスキップ（パターン/MD5/パスが一致するものを除外）")
        self._chk_skip_dup.setChecked(True)
        opt_row.addWidget(self._chk_skip_dup); opt_row.addStretch()
        lay.addLayout(opt_row)

        # ── プレビュータブ ──
        self._nb = QTabWidget(); lay.addWidget(self._nb, 1)

        # NGワードプレビュー
        w_page = QWidget(); self._nb.addTab(w_page, "NGワード (0件)")
        w_lay = QVBoxLayout(w_page)
        self._word_table = QTableWidget(0, 4)
        self._word_table.setHorizontalHeaderLabels(["有効", "パターン", "タイプ", "スコープ"])
        hh = self._word_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._word_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vh = self._word_table.verticalHeader(); vh.setVisible(False); vh.setDefaultSectionSize(17)
        w_lay.addWidget(self._word_table)
        self._word_status = QLabel(""); w_lay.addWidget(self._word_status)

        # NG画像プレビュー
        i_page = QWidget(); self._nb.addTab(i_page, "NG画像 (0件)")
        i_lay = QVBoxLayout(i_page)
        self._img_table = QTableWidget(0, 4)
        self._img_table.setHorizontalHeaderLabels(["有効", "MD5", "サイズ", "登録日"])
        ih = self._img_table.horizontalHeader()
        ih.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ih.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        ih.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ih.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._img_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vh2 = self._img_table.verticalHeader(); vh2.setVisible(False); vh2.setDefaultSectionSize(17)
        i_lay.addWidget(self._img_table)
        self._img_status = QLabel(""); i_lay.addWidget(self._img_status)

        # ── ボタン行 ──
        self._chk_skip_dup.toggled.connect(self._refresh_preview)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("インポート実行")
        self._ok_btn.setEnabled(False)
        btns.accepted.connect(self._do_import)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ──────────────────────────────────────────────────────────────────────────
    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Filter.ini を選択", "", "INI ファイル (Filter.ini);;すべてのファイル (*)")
        if not path:
            return
        self._path_edit.setText(path)
        try:
            words, images = _parse_filter_ini(path)
        except Exception as e:
            QMessageBox.critical(self, "読み込みエラー", f"Filter.ini の解析に失敗しました。\n{e}")
            return
        self._ng_words  = words
        self._ng_images = images

        self._refresh_preview()
        self._ok_btn.setEnabled(True)

    # ──────────────────────────────────────────────────────────────────────────
    def _refresh_preview(self):
        skip_dup = self._chk_skip_dup.isChecked()

        existing_pats = {w.get("pattern", "") for w in self._settings.ng_words}
        existing_md5s = {i.get("md5", "")     for i in self._settings.ng_images}

        _type_labels = {
            "ng": "NG", "reverse_ng": "逆NG",
            "replace": "置換", "mow_replace": "芝刈り置換",
        }

        # ── NGワードテーブル ──
        self._word_table.setRowCount(0)
        skipped_w = 0
        shown_w   = 0
        for w in self._ng_words:
            pat = w.get("pattern", "")
            if skip_dup and pat in existing_pats:
                skipped_w += 1; continue
            row = self._word_table.rowCount()
            self._word_table.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if w.get("enabled", True)
                              else Qt.CheckState.Unchecked)
            self._word_table.setItem(row, 0, chk)
            self._word_table.setItem(row, 1, QTableWidgetItem(pat))
            self._word_table.setItem(row, 2,
                QTableWidgetItem(_type_labels.get(w.get("ng_type", "ng"), "NG")))
            scope_parts = []
            for key, label in [("scope_body","本文"),("scope_name","名前"),
                                ("scope_mail","メール"),("scope_subject","件名"),
                                ("scope_id","ID"),("scope_ip","IP"),
                                ("scope_catalog","カタログ")]:
                if w.get(key, False): scope_parts.append(label)
            self._word_table.setItem(row, 3, QTableWidgetItem(", ".join(scope_parts) or "-"))
            shown_w += 1

        total_w = len(self._ng_words)
        self._nb.setTabText(0, f"NGワード ({shown_w}件)")
        if skip_dup and skipped_w:
            self._word_status.setText(
                f"合計 {total_w} 件 / 重複スキップ {skipped_w} 件 / インポート対象 {shown_w} 件")
        else:
            self._word_status.setText(f"合計 {total_w} 件")

        # ── NG画像テーブル ──
        self._img_table.setRowCount(0)
        skipped_i = 0
        shown_i   = 0
        for img in self._ng_images:
            md5 = img.get("md5", "")
            if skip_dup and md5 in existing_md5s:
                skipped_i += 1; continue
            row = self._img_table.rowCount()
            self._img_table.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if img.get("enabled", True)
                              else Qt.CheckState.Unchecked)
            self._img_table.setItem(row, 0, chk)
            self._img_table.setItem(row, 1, QTableWidgetItem(md5))
            w_val = img.get("width", 0); h_val = img.get("height", 0)
            sz    = img.get("size_min", 0)
            self._img_table.setItem(row, 2,
                QTableWidgetItem(f"{w_val}×{h_val} {sz}bytes" if sz else f"{w_val}×{h_val}"))
            desc = img.get("description", "")
            self._img_table.setItem(row, 3, QTableWidgetItem(desc))
            shown_i += 1

        total_i = len(self._ng_images)
        self._nb.setTabText(1, f"NG画像 ({shown_i}件)")
        if skip_dup and skipped_i:
            self._img_status.setText(
                f"合計 {total_i} 件 / 重複スキップ {skipped_i} 件 / インポート対象 {shown_i} 件")
        else:
            self._img_status.setText(f"合計 {total_i} 件")


    # ──────────────────────────────────────────────────────────────────────────
    def _do_import(self):
        skip_dup = self._chk_skip_dup.isChecked()
        existing_pats = {w.get("pattern", "") for w in self._settings.ng_words}
        existing_md5s = {i.get("md5", "")     for i in self._settings.ng_images}

        added_w = 0
        for row in range(self._word_table.rowCount()):
            # チェックが外れていたらスキップ
            chk = self._word_table.item(row, 0)
            if chk and chk.checkState() != Qt.CheckState.Checked:
                continue
            pat = self._word_table.item(row, 1)
            if pat is None:
                continue
            pat_text = pat.text()
            if skip_dup and pat_text in existing_pats:
                continue
            # 元データから該当エントリを探す
            for w in self._ng_words:
                if w.get("pattern", "") == pat_text:
                    self._settings.ng_words.append(w)
                    existing_pats.add(pat_text)
                    added_w += 1
                    break

        added_i = 0
        for row in range(self._img_table.rowCount()):
            chk = self._img_table.item(row, 0)
            if chk and chk.checkState() != Qt.CheckState.Checked:
                continue
            md5_item = self._img_table.item(row, 1)
            if md5_item is None:
                continue
            md5_text = md5_item.text()
            if skip_dup and md5_text in existing_md5s:
                continue
            for img in self._ng_images:
                if img.get("md5", "") == md5_text:
                    self._settings.ng_images.append(img)
                    existing_md5s.add(md5_text)
                    added_i += 1
                    break

        self._settings.save()
        self._settings.invalidate_ng_cache()
        msg = f"NGワード {added_w} 件、NG画像 {added_i} 件をインポートしました。"
        QMessageBox.information(self, "インポート完了", msg)
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
# メインウィンドウ
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# 板ごとの設定ダイアログ
# タブ: カタログ / 自動更新 / スタイル
# ══════════════════════════════════════════════════════════════════════════════

class BoardSettingsDialog(QDialog):
    """板ごとの設定ダイアログ（カタログ・自動更新・スタイル）"""

    def __init__(self, board_settings: BoardSettings, display_name: str,
                 on_apply=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{display_name}の設定")
        self.resize(600, 560)
        self._bs = board_settings
        self._on_apply = on_apply
        self._build()
        self._load()

    # ──────────────────────────────────────────────────────────────────────────
    def _build(self):
        lay = QVBoxLayout(self)
        nb  = QTabWidget(); lay.addWidget(nb, 1)

        def _scroll(widget):
            sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(widget)
            return sa

        def _spin(lo, hi, suffix="", tip="", width=None):
            w = _NoWheelSpinBox(); w.setRange(lo, hi)
            if suffix: w.setSuffix(suffix)
            if tip:    w.setToolTip(tip)
            if width:  w.setFixedWidth(width)
            return w

        def _combo(items, tip=""):
            w = _NoWheelComboBox(); w.addItems(items)
            if tip: w.setToolTip(tip)
            return w

        # ══════════════════════════════════════════════════════════════════
        # Tab1: カタログ
        # ══════════════════════════════════════════════════════════════════
        w1 = QWidget(); f1 = QVBoxLayout(w1)

        g_cat = QGroupBox("カタログ表示"); f1.addWidget(g_cat); cf = QFormLayout(g_cat)
        self._cat_cols  = _spin(1, 100); cf.addRow("横スレ数 (初期値:14):", self._cat_cols)
        self._cat_rows  = _spin(1, 100); cf.addRow("縦スレ数 (初期値:6):",  self._cat_rows)
        self._cat_chars = _spin(0, 100); cf.addRow("文字数 (初期値:4):",    self._cat_chars)

        pos_w = QWidget(); pos_lay = QHBoxLayout(pos_w); pos_lay.setContentsMargins(0,0,0,0)
        self._pos_group = QButtonGroup(self)
        for i, lbl in enumerate(["下(0)", "右(1)"]):
            rb = QRadioButton(lbl); self._pos_group.addButton(rb, i); pos_lay.addWidget(rb)
        pos_lay.addStretch()
        cf.addRow("文字位置:", pos_w)

        img_w = QWidget(); img_lay = QHBoxLayout(img_w); img_lay.setContentsMargins(0,0,0,0)
        self._img_group = QButtonGroup(self)
        for i, lbl in enumerate(["小", "1", "2", "3", "4", "5", "大"]):
            rb = QRadioButton(lbl); self._img_group.addButton(rb, i); img_lay.addWidget(rb)
        img_lay.addStretch()
        cf.addRow("画像サイズ:", img_w)

        g_sort = QGroupBox("ソート"); f1.addWidget(g_sort); sf = QFormLayout(g_sort)
        self._cat_sort = _combo(["なし", "URL", "レス数", "既読", "勢い", "50音"])
        sf.addRow("カタログのソート:", self._cat_sort)

        g_few = QGroupBox("過疎スレ非表示"); f1.addWidget(g_few)
        few_lay = QVBoxLayout(g_few)
        few_row = QHBoxLayout()
        self._bs_few_res_hide  = QCheckBox()
        self._bs_few_res_count = _spin(0, 9999, " レス以下のスレを非表示", width=220,
            tip="チェックONのとき、指定レス数以下のスレをカタログに表示しない")
        few_row.addWidget(self._bs_few_res_hide); few_row.addWidget(self._bs_few_res_count)
        few_row.addStretch(); few_lay.addLayout(few_row)
        self._bs_few_res_hide.toggled.connect(self._bs_few_res_count.setEnabled)
        self._bs_few_res_count.setEnabled(False)  # 初期状態（チェックOFF時）は無効

        f1.addStretch(); nb.addTab(_scroll(w1), "カタログ")

        # ══════════════════════════════════════════════════════════════════
        # Tab2: 自動更新
        # ══════════════════════════════════════════════════════════════════
        w2 = QWidget(); f2 = QVBoxLayout(w2)

        g_ar_def = QGroupBox("デフォルト更新間隔"); f2.addWidget(g_ar_def)
        ar_lay = QVBoxLayout(g_ar_def)

        self._ar_use_default_thread = QCheckBox(
            "スレのデフォルト間隔を使う（OFFにすると最後の設定を引き継ぐ）")
        ar_lay.addWidget(self._ar_use_default_thread)
        _thread_rows = QWidget(); _tr_form = QFormLayout(_thread_rows)
        _tr_form.setContentsMargins(16, 0, 0, 0)
        _LABELS_T = [
            "最大保存件数の 100% 以下 (通常):",
            "最大保存件数の  50% 以下:",
            "最大保存件数の  25% 以下:",
            "最大保存件数の  10% 以下:",
            "最大保存件数の   5% 以下:",
            "最大保存件数の   1% 以下:",
        ]
        self._ar_def_thread_spins = []; self._ar_def_thread_chks = []
        for i, lbl in enumerate(_LABELS_T):
            row = QHBoxLayout()
            chk = None
            if i > 0:
                chk = QCheckBox("有効"); self._ar_def_thread_chks.append(chk)
                row.addWidget(chk)
            # 1%行(i=5)は秒単位、それ以外は分単位
            if i == 5:
                sp = _spin(1, 99999, " 秒", width=90)
            else:
                sp = _spin(1, 9999, " 分", width=90)
            self._ar_def_thread_spins.append(sp)
            row.addWidget(sp); row.addStretch()
            _tr_form.addRow(lbl, row)
            # 「有効」チェックOFF時は右側スピンを編集不可にする（i=1..5）
            if chk is not None:
                chk.toggled.connect(sp.setEnabled)
                sp.setEnabled(chk.isChecked())
        ar_lay.addWidget(_thread_rows)

        self._ar_use_default_catalog = QCheckBox("カタログのデフォルト間隔を使う")
        ar_lay.addWidget(self._ar_use_default_catalog)
        _catalog_rows = QWidget(); _tc_form = QFormLayout(_catalog_rows)
        _tc_form.setContentsMargins(16, 0, 0, 0)
        self._ar_def_catalog_spin = _spin(1, 9999, " 分", width=90)
        _tc_form.addRow("更新間隔:", self._ar_def_catalog_spin)
        ar_lay.addWidget(_catalog_rows)

        def _toggle_thread_default(checked):  _thread_rows.setEnabled(checked)
        def _toggle_catalog_default(checked): _catalog_rows.setEnabled(checked)
        self._ar_use_default_thread.toggled.connect(_toggle_thread_default)
        self._ar_use_default_catalog.toggled.connect(_toggle_catalog_default)
        _toggle_thread_default(False); _toggle_catalog_default(False)

        g_auto_add = QGroupBox("自動更新への自動登録"); f2.addWidget(g_auto_add)
        aa_lay = QVBoxLayout(g_auto_add)
        self._bd_auto_add_ar         = QCheckBox("スレを開いたとき自動的に自動更新に追加する")
        self._bd_auto_add_catalog_ar = QCheckBox("カタログを開いたとき自動的に自動更新に追加する")
        aa_lay.addWidget(self._bd_auto_add_ar)
        aa_lay.addWidget(self._bd_auto_add_catalog_ar)

        f2.addStretch(); nb.addTab(_scroll(w2), "自動更新")

        # ══════════════════════════════════════════════════════════════════
        # Tab3: スタイル
        # ══════════════════════════════════════════════════════════════════
        w3 = QWidget(); f3 = QVBoxLayout(w3)

        g_css = QGroupBox("ユーザースタイルシート"); f3.addWidget(g_css)
        cfl = QVBoxLayout(g_css)
        cfl.addWidget(QLabel("CSSファイルパス (theme/user.css を推奨):"))
        css_row = QHBoxLayout()
        self._css_path = QLineEdit(); css_row.addWidget(self._css_path, 1)
        css_browse = QPushButton("参照…"); css_browse.setFixedWidth(70)
        def _browse_css():
            p, _ = QFileDialog.getOpenFileName(
                self, "CSSファイルを選択", "", "CSS Files (*.css);;All Files (*)")
            if p: self._css_path.setText(p)
        css_browse.clicked.connect(_browse_css)
        css_row.addWidget(css_browse); cfl.addLayout(css_row)
        cfl.addWidget(QLabel("※ スレ・カタログを再読み込みすると反映されます"))

        f3.addStretch(); nb.addTab(_scroll(w3), "スタイル")

        # ── OK / Cancel ────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ──────────────────────────────────────────────────────────────────────────
    def _load(self):
        bs = self._bs

        # カタログ
        self._cat_cols.setValue(bs.cat_cols)
        self._cat_rows.setValue(bs.cat_rows)
        self._cat_chars.setValue(bs.cat_chars)
        _POS_LABELS = ["下", "右"]
        _POS_OLD    = {"0:下": 0, "1:右": 1, "2:左": 0, "3:上": 0}
        pos_idx = _POS_OLD.get(bs.cat_text_pos,
                    (_POS_LABELS.index(bs.cat_text_pos) if bs.cat_text_pos in _POS_LABELS else 0))
        btn = self._pos_group.button(pos_idx)
        if btn: btn.setChecked(True)
        _IMG_LABELS = ["小", "1", "2", "3", "4", "5", "大"]
        _IMG_OLD    = {"0:小": 0, "1:中": 2, "2:大": 6}
        img_idx = _IMG_OLD.get(bs.cat_img_size_str,
                    (_IMG_LABELS.index(bs.cat_img_size_str) if bs.cat_img_size_str in _IMG_LABELS else 0))
        btn = self._img_group.button(img_idx)
        if btn: btn.setChecked(True)
        self._cat_sort.setCurrentIndex(bs.catalog_sort_type)
        # 過疎スレ非表示
        _hide = getattr(bs, "catalog_few_res_hide", False)
        self._bs_few_res_hide.setChecked(_hide)
        self._bs_few_res_count.setValue(getattr(bs, "catalog_few_res_count", 5))
        self._bs_few_res_count.setEnabled(_hide)

        # 自動更新
        use_t = bs.ar_use_default_thread
        use_c = bs.ar_use_default_catalog
        self._ar_use_default_thread.setChecked(use_t)
        self._ar_use_default_catalog.setChecked(use_c)
        t_ivals = bs.ar_default_thread_intervals or [3600, 1800, 600, 120, 60, 30]
        t_chks  = bs.ar_default_thread_checks    or [False]*5
        _t_defaults = [3600, 1800, 600, 120, 60, 30]
        for i, sp in enumerate(self._ar_def_thread_spins):
            sec = t_ivals[i] if i < len(t_ivals) else _t_defaults[i]
            # 1%行(i=5)は秒表示、それ以外は分表示
            sp.setValue(sec if i == 5 else max(1, sec // 60))
        for i, chk in enumerate(self._ar_def_thread_chks):
            chk.setChecked(t_chks[i] if i < len(t_chks) else False)
        c_ivals = bs.ar_default_catalog_intervals or [3600]
        self._ar_def_catalog_spin.setValue(max(1, (c_ivals[0] if c_ivals else 3600) // 60))
        self._ar_use_default_thread.toggled.emit(use_t)
        self._ar_use_default_catalog.toggled.emit(use_c)

        # スタイル
        self._css_path.setText(bs.user_css_file or "theme/user.css")
        # 自動登録
        self._bd_auto_add_ar.setChecked(getattr(bs, "auto_add_to_ar", False))
        self._bd_auto_add_catalog_ar.setChecked(getattr(bs, "auto_add_catalog_to_ar", False))

    # ──────────────────────────────────────────────────────────────────────────
    def _ok(self):
        bs = self._bs

        # カタログ
        bs.cat_cols        = self._cat_cols.value()
        bs.cat_rows        = self._cat_rows.value()
        bs.cat_chars       = self._cat_chars.value()
        _POS_LABELS        = ["下", "右"]
        pos_id             = self._pos_group.checkedId()
        bs.cat_text_pos    = _POS_LABELS[pos_id] if 0 <= pos_id < 2 else "下"
        _IMG_LABELS        = ["小", "1", "2", "3", "4", "5", "大"]
        img_id             = self._img_group.checkedId()
        bs.cat_img_size_str = _IMG_LABELS[img_id] if 0 <= img_id < 7 else "小"
        bs.catalog_sort_type = self._cat_sort.currentIndex()
        # 過疎スレ非表示
        bs.use_own_few_res       = True   # 板設定が常に有効
        bs.catalog_few_res_hide  = self._bs_few_res_hide.isChecked()
        bs.catalog_few_res_count = self._bs_few_res_count.value()

        # 自動更新
        bs.ar_use_default_thread       = self._ar_use_default_thread.isChecked()
        bs.ar_use_default_catalog      = self._ar_use_default_catalog.isChecked()
        bs.ar_default_thread_intervals = [sp.value() if i == 5 else sp.value() * 60
                                              for i, sp in enumerate(self._ar_def_thread_spins)]
        bs.ar_default_thread_checks    = [chk.isChecked() for chk in self._ar_def_thread_chks]
        bs.ar_default_catalog_intervals = [self._ar_def_catalog_spin.value() * 60]
        bs.ar_default_catalog_checks    = []

        # スタイル
        bs.user_css_file             = self._css_path.text().strip() or "theme/user.css"
        # 自動登録
        bs.auto_add_to_ar            = self._bd_auto_add_ar.isChecked()
        bs.auto_add_catalog_to_ar    = self._bd_auto_add_catalog_ar.isChecked()

        bs.save()
        if self._on_apply: self._on_apply(bs)
        self.accept()

    def show_tab(self, tab_name: str):
        """指定タブ名をアクティブにする"""
        nb = self.findChild(QTabWidget)
        if not nb:
            return
        for i in range(nb.count()):
            if nb.tabText(i) == tab_name:
                nb.setCurrentIndex(i)
                break


class BookmarkEditDialog(QDialog):
    """ブックマーク編集ウィンドウ。
    ・タイトル/URL を2列のテーブルで管理（ダブルクリックで直接編集）
    ・追加 / ───を追加する / 削除 / ↑ / ↓ ボタンで編集・並べ替え
    ・OK で結果を返す（呼び出し側が AppSettings.bookmarks に保存する）
    """
    _SEP_LABEL = "──────────"

    def __init__(self, bookmarks: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ブックマークの編集")
        self.resize(620, 480)

        v = QVBoxLayout(self)

        self._table = QTableWidget(0, 2, self)
        self._table.setHorizontalHeaderLabels(["タイトル", "URL"])
        _hh = self._table.horizontalHeader()
        _hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        _hh.setStretchLastSection(True)
        self._table.setColumnWidth(0, 220)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # ダブルクリック / Enter で直接編集できるようにする
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed)
        v.addWidget(self._table, 1)

        # 操作ボタン
        row = QHBoxLayout()
        b_add  = QPushButton("追加")
        b_sep  = QPushButton("───を追加する")
        b_del  = QPushButton("削除")
        b_up   = QPushButton("↑")
        b_down = QPushButton("↓")
        b_up.setFixedWidth(36)
        b_down.setFixedWidth(36)
        for b in (b_add, b_sep, b_del):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_up)
        row.addWidget(b_down)
        v.addLayout(row)

        b_add.clicked.connect(
            lambda: self._add_row({"title": "新しいブックマーク", "url": "https://"},
                                  select=True, edit=True))
        b_sep.clicked.connect(lambda: self._add_row({"sep": True}, select=True))
        b_del.clicked.connect(self._del_row)
        b_up.clicked.connect(lambda: self._move(-1))
        b_down.clicked.connect(lambda: self._move(1))

        # OK / キャンセル
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        for bm in (bookmarks or []):
            self._add_row(bm)

    # ── 行操作 ────────────────────────────────────────────────
    def _add_row(self, bm: dict, *, select: bool = False, edit: bool = False):
        is_sep = bool(bm.get("sep"))
        r = self._table.rowCount()
        self._table.insertRow(r)
        if is_sep:
            it0 = QTableWidgetItem(self._SEP_LABEL)
            it0.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it0.setFlags(it0.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it0.setData(Qt.ItemDataRole.UserRole, "sep")
            it1 = QTableWidgetItem("")
            it1.setFlags(it1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it1.setData(Qt.ItemDataRole.UserRole, "sep")
        else:
            it0 = QTableWidgetItem(str(bm.get("title", "")))
            it0.setData(Qt.ItemDataRole.UserRole, "link")
            it1 = QTableWidgetItem(str(bm.get("url", "")))
            it1.setData(Qt.ItemDataRole.UserRole, "link")
        self._table.setItem(r, 0, it0)
        self._table.setItem(r, 1, it1)
        if select:
            self._table.setCurrentCell(r, 0)
        if edit:
            self._table.editItem(it0)

    def _row_dict(self, r: int) -> dict:
        it0 = self._table.item(r, 0)
        kind = it0.data(Qt.ItemDataRole.UserRole) if it0 else "link"
        if kind == "sep":
            return {"sep": True}
        it1 = self._table.item(r, 1)
        return {"title": (it0.text() if it0 else "").strip(),
                "url":   (it1.text() if it1 else "").strip()}

    def _set_row(self, r: int, bm: dict):
        is_sep = bool(bm.get("sep"))
        it0 = self._table.item(r, 0)
        it1 = self._table.item(r, 1)
        if is_sep:
            it0.setText(self._SEP_LABEL)
            it0.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it0.setFlags(it0.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it0.setData(Qt.ItemDataRole.UserRole, "sep")
            it1.setText("")
            it1.setFlags(it1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it1.setData(Qt.ItemDataRole.UserRole, "sep")
        else:
            it0.setText(str(bm.get("title", "")))
            it0.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            it0.setFlags(it0.flags() | Qt.ItemFlag.ItemIsEditable)
            it0.setData(Qt.ItemDataRole.UserRole, "link")
            it1.setText(str(bm.get("url", "")))
            it1.setFlags(it1.flags() | Qt.ItemFlag.ItemIsEditable)
            it1.setData(Qt.ItemDataRole.UserRole, "link")

    def _del_row(self):
        r = self._table.currentRow()
        if r < 0:
            return
        self._table.removeRow(r)

    def _move(self, d: int):
        r = self._table.currentRow()
        if r < 0:
            return
        nr = r + d
        if not (0 <= nr < self._table.rowCount()):
            return
        a = self._row_dict(r)
        b = self._row_dict(nr)
        self._set_row(r, b)
        self._set_row(nr, a)
        self._table.setCurrentCell(nr, 0)

    # ── 結果取得 ──────────────────────────────────────────────
    def bookmarks(self) -> list:
        out = []
        for r in range(self._table.rowCount()):
            bm = self._row_dict(r)
            if bm.get("sep"):
                out.append({"sep": True})
            else:
                if not bm.get("title") and not bm.get("url"):
                    continue   # 空行は捨てる
                out.append({"title": bm.get("title", ""), "url": bm.get("url", "")})
        return out
