#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""futaba2b_const.py ─ アプリ全体で共有する定数"""

# ── User-Agent / Client Hints ────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SEC_CH_UA          = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
SEC_CH_UA_MOBILE   = "?0"
SEC_CH_UA_PLATFORM = '"Windows"'

# ── URLs ─────────────────────────────────────────────────────────────────────
BBSMENU_URL = "https://www.2chan.net/bbsmenu.html"

# ── 投稿エラー判定パターン ────────────────────────────────────────────────────
FUTABA_ERROR_PATTERNS = [
    "書きこみできませんでした",
    "フォームが正しくありません",
    "投稿できませんでした",
    "クッキーを有効にして",
    "ご利用の環境では",
    "ERROR!",
    "スレッドがありません",
    "スレッドが見つかりません",
]

# ── ファイルパス ──────────────────────────────────────────────────────────────
SETTINGS_FILE_NAME = "futaba2b_settings.json"

# テーマフォルダ
THEME_DIR      = "theme"


# ══════════════════════════════════════════════════════════════════════════════
# テーマ管理
# ══════════════════════════════════════════════════════════════════════════════
import json as _json
from pathlib import Path as _Path

class ThemeManager:
    """
    theme/dark.json または theme/light.json を読み込み、
    Qt スタイルシート文字列とスレッドHTML用CSS変数を提供する。

    使い方:
        ThemeManager.load("dark")   # または "light"
        ss = ThemeManager.qt_stylesheet()
        css = ThemeManager.thread_css_vars()
        c = ThemeManager.color("ui", "panel_bg")
    """
    _data: dict = {}
    _name: str  = "dark"

    @classmethod
    def theme_dir(cls, name: str | None = None) -> "_Path":
        """theme/{name}/ フォルダのPathを返す。name省略時は現在のテーマ。"""
        return _Path(__file__).parent / THEME_DIR / (name or cls._name)

    @classmethod
    def list_themes(cls) -> list:
        """theme/ フォルダをスキャンし、theme.json を持つサブフォルダ名を昇順で返す。"""
        base = _Path(__file__).parent / THEME_DIR
        result = []
        try:
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "theme.json").exists():
                    result.append(d.name)
        except Exception:
            pass
        return result or ["dark"]

    @classmethod
    def load(cls, name: str = "dark") -> None:
        """テーマJSONを読み込む。theme/{name}/theme.json を参照。失敗時はデフォルト値を使う。"""
        cls._name = name
        theme_path = cls.theme_dir(name) / "theme.json"
        try:
            with open(theme_path, encoding="utf-8") as f:
                cls._data = _json.load(f)
        except Exception as e:
            print(f"[Theme] {name}/theme.json 読み込み失敗: {e}")
            cls._data = {}

    @classmethod
    def color(cls, section: str, key: str, fallback: str = "#888") -> str:
        """テーマ色を返す。未定義時は fallback を返す。"""
        return cls._data.get(section, {}).get(key, fallback)

    @classmethod
    def ui(cls, key: str, fallback: str = "#888") -> str:
        return cls.color("ui", key, fallback)

    @classmethod
    def thread(cls, key: str, fallback: str = "#888") -> str:
        return cls.color("thread", key, fallback)

    @classmethod
    def qt_stylesheet(cls) -> str:
        """アプリ全体に適用するQtスタイルシートを返す。"""
        u = cls._data.get("ui", {})
        def c(k, fb="#888"): return u.get(k, fb)

        # チェックマーク PNG を一時ファイルに書き出す（Qt styleSheet は data: URI 非対応）
        import base64, tempfile, os as _os
        _CHECK_PNG_B64 = (
            "iVBORw0KGgoAAAANSUhEUgAAABoAAAAaCAYAAACpSkzOAAAAS0lEQVR42mNgGAWj"
            "YBSQAv4jAbpYQjOL/mMBo5bQxhJiNVHsE2I1UxxcxLiUanGCzyCqRz42w2iSwv4T"
            "AWiafId2hqRriTwKBhQAAC5SdpiVDhmiAAAAAElFTkSuQmCC"
        )
        _check_png_path = ""
        try:
            _tmp = tempfile.NamedTemporaryFile(
                suffix=".png", delete=False, prefix="2bp_chk_")
            _tmp.write(base64.b64decode(_CHECK_PNG_B64))
            _tmp.close()
            _check_png_path = _tmp.name.replace("\\", "/")
        except Exception:
            pass
        _check_img = f'image: url("{_check_png_path}");' if _check_png_path else ""

        return f"""
QWidget {{
    background-color: {c("window_bg", "#1e1e1e")};
    color: {c("text_primary", "#e8e8e8")};
    font-family: "MS Pゴシック", "MS PGothic", sans-serif;
}}
QMainWindow, QDialog {{
    background-color: {c("window_bg", "#1e1e1e")};
}}
QToolBar {{
    background-color: {c("toolbar_bg", "#2d2d2d")};
    border-bottom: 1px solid {c("toolbar_border", "#444")};
    spacing: 2px;
}}
QStatusBar {{
    background: {c("statusbar_bg", "#2d2d2d")};
    border-top: 1px solid {c("statusbar_border", "#555")};
    color: {c("statusbar_fg", "#ccc")};
}}
QStatusBar QLabel {{
    color: {c("statusbar_fg", "#ccc")};
    padding: 0 5px;
    font-size: 8pt;
}}
QProgressBar {{
    max-height: 14px;
    font-size: 7pt;
    color: {c("statusbar_fg", "#ccc")};
    background: {c("progress_bg", "#444")};
    border: 1px solid {c("btn_border", "#666")};
    border-radius: 2px;
}}
QProgressBar::chunk {{
    background: {c("progress_chunk", "#5588cc")};
}}
QPushButton {{
    background-color: {c("btn_bg", "#3a3a3a")};
    color: {c("btn_fg", "#ccc")};
    border: 1px solid {c("btn_border", "#555")};
    border-radius: 3px;
    padding: 2px 8px;
}}
QPushButton:hover {{
    background-color: {c("btn_hover_bg", "#4a4a4a")};
}}
QPushButton:checked {{
    background-color: {c("btn_checked_bg", "#5588cc")};
    color: {c("btn_checked_fg", "#fff")};
    font-weight: bold;
}}
QPushButton:disabled {{
    color: {c("text_muted", "#666")};
}}
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {c("input_bg", "#333")};
    color: {c("input_fg", "#e8e8e8")};
    border: 1px solid {c("input_border", "#555")};
    border-radius: 3px;
    padding: 1px 4px;
    selection-background-color: {c("progress_chunk", "#5588cc")};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c("input_focus_border", "#5588cc")};
}}
QComboBox::drop-down {{
    border: none;
    background: {c("btn_bg", "#3a3a3a")};
}}
QComboBox QAbstractItemView {{
    background-color: {c("input_bg", "#333")};
    color: {c("input_fg", "#e8e8e8")};
    selection-background-color: {c("progress_chunk", "#5588cc")};
    border: 1px solid {c("input_border", "#555")};
}}
QTabWidget::pane {{
    border: 1px solid {c("tab_border", "#555")};
    background: {c("panel_bg", "#252525")};
}}
QTabBar::tab {{
    background: {c("tab_bg", "#2d2d2d")};
    color: {c("tab_fg", "#ccc")};
    border: 1px solid {c("tab_border", "#555")};
    padding: 3px 10px;
    margin-right: 1px;
}}
QTabBar::tab:selected {{
    background: {c("tab_selected_bg", "#3a3a3a")};
    color: {c("tab_selected_fg", "#fff")};
    border-bottom: none;
}}
QTabBar::tab:hover:!selected {{
    background: {c("btn_hover_bg", "#4a4a4a")};
}}
QGroupBox {{
    border: 1px solid {c("panel_border", "#444")};
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 4px;
    color: {c("text_primary", "#e8e8e8")};
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 4px;
    color: {c("text_primary", "#e8e8e8")};
}}
QScrollArea {{
    background: {c("scroll_bg", "#2a2a2a")};
    border: none;
}}
QScrollBar:vertical {{
    background: {c("scroll_bg", "#2a2a2a")};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c("scroll_handle", "#555")};
    min-height: 20px;
    border-radius: 4px;
    margin: 2px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {c("scroll_bg", "#2a2a2a")};
    height: 12px;
}}
QScrollBar::handle:horizontal {{
    background: {c("scroll_handle", "#555")};
    min-width: 20px;
    border-radius: 4px;
    margin: 2px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QTreeWidget, QListWidget, QTableWidget {{
    background-color: {c("panel_bg2", "#2a2a2a")};
    color: {c("text_primary", "#e8e8e8")};
    border: 1px solid {c("panel_border", "#444")};
    alternate-background-color: {c("panel_bg", "#252525")};
}}
QTreeWidget::item:selected, QListWidget::item:selected,
QTableWidget::item:selected {{
    background-color: {c("progress_chunk", "#5588cc")};
    color: #fff;
}}
QHeaderView::section {{
    background-color: {c("panel_header_bg", "#333")};
    color: {c("panel_header_fg", "#ddd")};
    border: 1px solid {c("panel_border", "#444")};
    padding: 2px 6px;
}}
QCheckBox {{
    color: {c("text_primary", "#e8e8e8")};
    spacing: 5px;
}}
QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    border: 1px solid {c("checkbox_border", "#888888")};
    border-radius: 2px;
    background: {c("input_bg", "#3a3a3a")};
}}
QCheckBox::indicator:checked {{
    background: {c("progress_chunk", "#5588cc")};
    border-color: {c("progress_chunk", "#5588cc")};
    {_check_img}
}}
QCheckBox::indicator:disabled {{
    border-color: {c("text_muted", "#555555")};
    background: {c("window_bg", "#1e1e1e")};
}}
QRadioButton {{
    color: {c("text_primary", "#e8e8e8")};
    spacing: 5px;
}}
QLabel {{
    color: {c("text_primary", "#e8e8e8")};
    background: transparent;
}}
QMenuBar {{
    background-color: {c("toolbar_bg", "#2d2d2d")};
    color: {c("text_primary", "#e8e8e8")};
}}
QMenuBar::item:selected {{
    background-color: {c("btn_hover_bg", "#4a4a4a")};
}}
QMenu {{
    background-color: {c("popup_bg", "#2d2d2d")};
    color: {c("text_primary", "#e8e8e8")};
    border: 1px solid {c("popup_border", "#555")};
}}
QMenu::item:selected {{
    background-color: {c("progress_chunk", "#5588cc")};
    color: #fff;
}}
QMenu::separator {{
    height: 1px;
    background: {c("menu_separator", "#666")};
    margin: 4px 8px;
}}
QSplitter::handle {{
    background-color: {c("panel_border", "#444")};
}}
QToolButton {{
    background-color: transparent;
    color: {c("text_primary", "#e8e8e8")};
    border: none;
    padding: 2px;
}}
QToolButton:hover {{
    background-color: {c("btn_hover_bg", "#4a4a4a")};
    border-radius: 3px;
}}
QToolButton:pressed {{
    background-color: {c("btn_checked_bg", "#5588cc")};
}}
"""

    @classmethod
    def thread_css_vars(cls) -> str:
        """スレッドHTML内に埋め込むCSS変数ブロックを返す。"""
        t = cls._data.get("thread", {})
        def c(k, fb="#888"): return t.get(k, fb)

        return f"""
:root {{
  --body-bg:            {c("body_bg",            "#FFFFEE")};
  --body-fg:            {c("body_fg",            "#7B0004")};
  --op-bg:              {c("op_bg",              "#FFFFEE")};
  --reply-bg:           {c("reply_bg",           "#F0E0D6")};
  --new-res-border:     {c("new_res_border",     "#cc1105")};
  --self-res-border:    {c("self_res_border",    "#1a6fd4")};
  --divider-fg:         {c("new_res_divider_fg", "#cc1105")};
  --divider-bg:         {c("new_res_divider_bg", "#fff0f0")};
  --link-color:         {c("link_color",         "#0000EE")};
  --link-hover:         {c("link_hover",         "#DD0000")};
  --quote-color:        {c("quote_color",        "#789922")};
  --name-color:         {c("name_color",         "#117743")};
  --subject-color:      {c("subject_color",      "#cc1105")};
  --date-color:         {c("date_color",         "#800000")};
  --no-color:           {c("no_color",           "#800000")};
  --no-hover:           {c("no_hover",           "#DD0000")};
  --sod-color:          {c("sod_color",          "#800000")};
  --footer-color:       {c("footer_color",       "#888888")};
  --footer-border:      {c("footer_border",      "#dddddd")};
  --thumb-border:       {c("thumb_border",       "#aaaaaa")};
  --thumb-hover-border: {c("thumb_hover_border", "#800000")};
  --expiry-color:       {c("expiry_color",       "#cc0000")};
  --expiry-bg:          {c("expiry_bg",          "#fff8f8")};
  --del-reason-color:   {c("del_reason_color",   "#cc1105")};
  --del-content-color:  {c("del_content_color",  "#800000")};
  --comment-color:      {c("comment_color",      "#7B0004")};
  --id-popup-bg:        {c("id_popup_bg",        "#FFFFEE")};
  --id-popup-border:    {c("id_popup_border",    "#800000")};
}}
"""

    @classmethod
    def name(cls) -> str:
        return cls._name
