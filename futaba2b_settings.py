#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""futaba2b_settings.py  ─  設定管理・NGフィルタ"""

from __future__ import annotations
import json, re, time, random, string
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from futaba2b_const import SETTINGS_FILE_NAME
# BoardInfo は実行時にも使うため TYPE_CHECKING ブロックの外でインポート
from futaba2b_models import BoardInfo

if TYPE_CHECKING:
    from futaba2b_models import ResData

SETTINGS_FILE = Path(SETTINGS_FILE_NAME)


# ──────────────────────────────────────────────────────────────────────────────
# 板ごとの設定
# ──────────────────────────────────────────────────────────────────────────────

# 全板設定を1ファイルで管理
_BOARDS_FILE = Path("futaba2b_boards.json")




class BoardSettings:
    """板ごとの設定（カタログ・自動更新・スタイル）。
    全板設定を futaba2b_boards.json の { board_key: {...} } 形式で一元管理する。"""

    def __init__(self, board_key: str) -> None:
        self._board_key = board_key
        # ── カタログ ──
        self.cat_cols: int          = 14
        self.cat_rows: int          = 6
        self.cat_chars: int         = 4
        self.cat_text_pos: str      = "0:下"
        self.cat_img_size_str: str  = "0:小"
        self.catalog_sort_type: int = 0
        self.catalog_sort_desc: bool = False
        # ── 自動更新 ──
        self.ar_use_default_thread: bool  = True
        self.ar_use_default_catalog: bool = True
        self.ar_default_thread_intervals: list  = [3600, 1800, 600, 120, 60, 30]
        self.ar_default_thread_checks:    list  = [True, True, True, True, True]
        self.ar_default_catalog_intervals: list = [600]
        self.ar_default_catalog_checks:   list  = []
        # ── スタイル ──
        self.user_css_file: str = "theme/user.css"
        self.auto_add_to_ar:         bool = True
        self.auto_add_catalog_to_ar: bool = True
        # 過疎スレ非表示（use_own_few_res=TrueのときAppSettings値を無視して板設定を使う）
        self.use_own_few_res:        bool = False
        self.catalog_few_res_hide:   bool = False
        self.catalog_few_res_count:  int  = 5

        self._load_from_file()
        self._remove_old_file()  # 旧形式ファイルが残っていれば削除

    # ── ファイル操作 ───────────────────────────────────────────────────────────
    def _load_from_file(self) -> None:
        """futaba2b_boards.json から自分のキー分を読み込む。
        旧形式 futaba2b_board_*.json も参照してマイグレーションする。"""
        raw = self._read_my_section()
        if raw is None:
            return
        def _g(k, d):
            return raw.get(k, d)
        self.cat_cols           = _g("cat_cols",  14)
        self.cat_rows           = _g("cat_rows",  6)
        self.cat_chars          = _g("cat_chars", 4)
        self.cat_text_pos       = _g("cat_text_pos",      "0:下")
        self.cat_img_size_str   = _g("cat_img_size_str",  "0:小")
        self.catalog_sort_type  = _g("catalog_sort_type", 0)
        self.catalog_sort_desc  = _g("catalog_sort_desc", False)
        self.ar_use_default_thread   = _g("ar_use_default_thread",   True)
        self.ar_use_default_catalog  = _g("ar_use_default_catalog",  True)
        self.ar_default_thread_intervals  = _g("ar_default_thread_intervals",  [3600, 1800, 600, 120, 60, 30])
        # 旧形式の移行（v0.8.078で分→秒に変更。旧=5要素分単位、新=6要素秒単位）
        if isinstance(self.ar_default_thread_intervals, list) and len(self.ar_default_thread_intervals) == 5:
            self.ar_default_thread_intervals = [int(v) * 60 for v in self.ar_default_thread_intervals] + [30]
        self.ar_default_thread_checks     = _g("ar_default_thread_checks",     [False]*5)
        # checks 旧4要素 → 5要素（1%行追加分）
        if isinstance(self.ar_default_thread_checks, list) and len(self.ar_default_thread_checks) == 4:
            self.ar_default_thread_checks = self.ar_default_thread_checks + [False]
        self.ar_default_catalog_intervals = _g("ar_default_catalog_intervals", [600])
        # カタログ間隔の旧分単位移行: 120以下なら旧形式（分）とみなして秒に変換
        if (isinstance(self.ar_default_catalog_intervals, list)
                and self.ar_default_catalog_intervals
                and int(self.ar_default_catalog_intervals[0]) <= 120):
            self.ar_default_catalog_intervals = [int(self.ar_default_catalog_intervals[0]) * 60]
        self.ar_default_catalog_checks    = _g("ar_default_catalog_checks",    [])
        self.user_css_file           = _g("user_css_file", "theme/user.css")
        self.auto_add_to_ar          = _g("auto_add_to_ar", False)
        self.auto_add_catalog_to_ar  = _g("auto_add_catalog_to_ar", False)
        self.use_own_few_res         = bool(_g("use_own_few_res",       False))
        self.catalog_few_res_hide    = bool(_g("catalog_few_res_hide",  False))
        self.catalog_few_res_count   = int( _g("catalog_few_res_count", 5))

    def _read_my_section(self) -> dict | None:
        """futaba2b_boards.json 内の自分のキーを返す。
        なければ旧形式ファイルを試みる。どちらもなければ None。"""
        # 新形式を優先
        if _BOARDS_FILE.exists():
            try:
                with open(_BOARDS_FILE, encoding="utf-8") as f:
                    all_boards = json.load(f)
                if self._board_key in all_boards:
                    return all_boards[self._board_key]
            except Exception:
                pass
        # 旧形式にフォールバック
        old_path = self._old_file_path()
        if old_path and old_path.exists():
            try:
                with open(old_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def _old_file_path(self) -> Path | None:
        """旧形式ファイルパスを返す（存在確認なし）"""
        safe = re.sub(r'[^\w\-]', '_', self._board_key).strip('_')
        return Path(f"futaba2b_board_{safe}.json")

    def _remove_old_file(self) -> None:
        """旧形式ファイルが残っていれば削除する（load 後に呼ぶ）"""
        old = self._old_file_path()
        if old and old.exists():
            try:
                old.unlink()
                print(f"[BoardSettings] migrated & removed: {old}")
            except Exception:
                pass

    def load(self) -> None:
        """外部から再読み込みする際のエントリポイント"""
        self._load_from_file()

    def save(self) -> None:
        """自分の設定を futaba2b_boards.json に書き込む（他板のキーは保持）"""
        try:
            all_boards: dict = {}
            if _BOARDS_FILE.exists():
                try:
                    with open(_BOARDS_FILE, encoding="utf-8") as f:
                        all_boards = json.load(f)
                except Exception:
                    pass
            all_boards[self._board_key] = {
                "cat_cols":          self.cat_cols,
                "cat_rows":          self.cat_rows,
                "cat_chars":         self.cat_chars,
                "cat_text_pos":      self.cat_text_pos,
                "cat_img_size_str":  self.cat_img_size_str,
                "catalog_sort_type": self.catalog_sort_type,
                "catalog_sort_desc": self.catalog_sort_desc,
                "ar_use_default_thread":   self.ar_use_default_thread,
                "ar_use_default_catalog":  self.ar_use_default_catalog,
                "ar_default_thread_intervals":  self.ar_default_thread_intervals,
                "ar_default_thread_checks":     self.ar_default_thread_checks,
                "ar_default_catalog_intervals": self.ar_default_catalog_intervals,
                "ar_default_catalog_checks":    self.ar_default_catalog_checks,
                "user_css_file":            self.user_css_file,
                "auto_add_to_ar":           self.auto_add_to_ar,
                "auto_add_catalog_to_ar":   self.auto_add_catalog_to_ar,
            "use_own_few_res":        self.use_own_few_res,
            "catalog_few_res_hide":   self.catalog_few_res_hide,
            "catalog_few_res_count":  self.catalog_few_res_count,
            }
            _tmp_b = _BOARDS_FILE.with_suffix(".tmp")
            with open(_tmp_b, "w", encoding="utf-8") as f:
                json.dump(all_boards, f, ensure_ascii=False, indent=2)
            import os as _os2
            _os2.replace(_tmp_b, _BOARDS_FILE)
        except Exception as e:
            print(f"[BoardSettings] save error: {e}")

    # ── AppSettings との互換ヘルパー（カタログ cxyl 用） ─────────────────────
    @property
    def catalog_cxyl_str(self) -> str:
        """cxyl クッキー文字列生成（AppSettings.catalog_cxyl_str と同ロジック）"""
        _pos_map = {"0:下": 0, "1:右": 1, "2:左": 2, "3:上": 3, "下": 0, "右": 1}
        _img_map = {"0:小": 0, "1:中": 2, "2:大": 6,
                    "小": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "大": 6}
        cols  = self.cat_cols
        rows  = self.cat_rows
        chars = self.cat_chars
        pos   = _pos_map.get(self.cat_text_pos, 0)
        imgsz = _img_map.get(self.cat_img_size_str, 0)
        return f"{cols}x{rows}x{chars}x{pos}x{imgsz}"


# ── グローバルキャッシュ（板URL → BoardSettings インスタンス） ────────────────
_board_settings_cache: dict[str, "BoardSettings"] = {}


def get_board_settings(board_key: str) -> "BoardSettings":
    """board_key に対応する BoardSettings を返す（キャッシュ付き）"""
    if board_key not in _board_settings_cache:
        _board_settings_cache[board_key] = BoardSettings(board_key)
    return _board_settings_cache[board_key]


# ── デフォルトアップローダーリスト ──────────────────────────────────────────
_DEFAULT_UPLOADERS = [
    {"name":"塩辛大瓶",  "pattern":r"sz\d+\.\w+",  "url":"http://www.siokarabin.com/futabafiles/big/src/auth.redirect.php?$MATCH", "popup":True,  "new_tab":False},
    {"name":"塩辛中瓶",  "pattern":r"sq\d+\.\w+",  "url":"http://www.nijibox6.com/futabafiles/mid/src/$MATCH.html",                "popup":True,  "new_tab":False},
    {"name":"塩辛塩粒",  "pattern":r"su\d+\.\w+",  "url":"http://www.nijibox5.com/futabafiles/tubu/src/$MATCH",                    "popup":True,  "new_tab":False},
    {"name":"塩辛小瓶",  "pattern":r"ss\d+\.\w+",  "url":"http://www.nijibox5.com/futabafiles/kobin/src/$MATCH",                   "popup":True,  "new_tab":False},
    {"name":"塩辛３ｍｌ","pattern":r"sp\d+\.\w+",  "url":"http://www.nijibox2.com/futabafiles/003/src/$MATCH",                     "popup":True,  "new_tab":False},
    {"name":"塩辛空瓶",  "pattern":r"sa\d+\.\w+",  "url":"http://www.nijibox6.com/futabafiles/001/src/$MATCH",                     "popup":True,  "new_tab":False},
    {"name":"ふたログmay","pattern":r"may\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/may/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログimg","pattern":r"img\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/img/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログdat","pattern":r"dat\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/dat/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログjun","pattern":r"jun\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/jun/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログdec","pattern":r"dec\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/dec/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログjik","pattern":r"jik\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/jik/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログid", "pattern":r"id\d+\.mht",  "url":"http://www.nijibox.ohflip.com/futalog/id/src/$MATCH.html",                "popup":False, "new_tab":False},
    {"name":"ふたログnar","pattern":r"nar\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/nar/src/$MATCH.html",               "popup":False, "new_tab":False},
    {"name":"ふたログoth","pattern":r"oth\d+\.mht", "url":"http://www.nijibox.ohflip.com/futalog/other/src/$MATCH.html",              "popup":False, "new_tab":False},
    {"name":"あぷ＠ふたば",  "pattern":r"f\d+\.\w+",  "url":"http://dec.2chan.net/up/src/$MATCH",   "popup":True, "new_tab":False},
    {"name":"あぷ小＠ふたば","pattern":r"fu\d+\.\w+", "url":"http://dec.2chan.net/up2/src/$MATCH",  "popup":True, "new_tab":False},
]

# デフォルトのブックマーク（メニューバー「ブックマーク」に表示）。
# {"sep": True} は区切り線、それ以外は {"title":..., "url":...} のリンク。
_DEFAULT_BOOKMARKS = [
    {"title": "2Bサポート他",       "url": "http://www2.ezbbs.net/13/futabe/"},
    {"title": "ふたば鯖☆偽監視所",  "url": "https://appsweets.net/serverstat/"},
    {"sep": True},
    {"title": "あぷ小＠ふたば",         "url": "https://dec.2chan.net/up2/"},
    {"title": "あぷ＠ふたば",           "url": "https://dec.2chan.net/up/up.htm"},
    {"sep": True},
    {"title": "ふたポ",             "url": "https://futapo.futakuro.com/"},
    {"title": "出ちゃいましたねぇ",  "url": "https://futaba-id.site/"},
    {"title": "FTBucket",          "url": "https://www.ftbucket.info/scrapshot/ftb/"},
    {"title": "つまんね。",          "url": "https://tsumanne.net/"},
    {"title": "リブレjp",           "url": "https://sportschan.org/librejp/catalog.html"},
    {"title": "めぶき☆ちゃんねる",   "url": "https://mebuki.moe/"},
]

class AppSettings:
    def __init__(self) -> None:
        self._app: dict                   = {}
        self.favorites: list[dict]        = []
        self.ng_words: list[dict]         = []
        self.thread_history: list[dict]   = []
        # ブックマーク（メニューバー「ブックマーク」）
        self.bookmarks: list[dict]        = [dict(b) for b in _DEFAULT_BOOKMARKS]
        # ログを出力する（黒いコンソールを表示する）。Falseで起動時にコンソールを隠す
        self.show_console: bool           = False
        # ★ 追加: 板リスト (BoardInfo オブジェクトのフラットなリスト)
        self.boards: list[BoardInfo]      = []
        # カスタム板グループ: [{"name":"二次元裏","boards":[{"name":"img","url":"..."}]}]
        self.custom_board_groups: list[dict] = []
        # 前回のタブ状態
        self.tab_state: dict = {}
        # ウィンドウ・スプリッター状態 (hex文字列)
        self.window_geometry: str = ""
        self.window_splitter: str = ""
        # スレ既読カウント {url: last_seen_res_count}
        self.thread_read_counts: dict = {}
        # カタログ既読レス数 {thread_url: last_seen_res_count}
        self.catalog_read_counts: dict = {}
        # カタログビューの UI 状態 {board_url: {local_sort, few_res, search, server_sort}}
        self.catalog_view_states: dict = {}
        # アップローダーリンク [{name, pattern, url, popup, new_tab}]
        self.uploader_links: list[dict] = list(_DEFAULT_UPLOADERS)
        # ユーザースタイルシートファイルパス
        self.user_css_file: str = "theme/user.css"
        # 板別の最大スレOP No.（落ちるまで残り件数の計算用）
        # キー: board.base_url  値: その板で見た最大OP No.
        self.global_max_no_by_board: dict = {}
        # 板別の最大保存件数（保存数はN件）。スレHTMLから抽出できない場合の
        # フォールバック用。カタログ取得時に学習し永続化する。
        # キー: board.base_url  値: max_saved
        self.max_saved_by_board: dict = {}
        # 旧フィールド互換のため残す（移行用）
        self.global_max_no: int = 0
        # 自動更新ダイアログ: 各行で最後に設定した分数 [100%, 50%, 30%, 15%, 5%]
        # 4行分（100%は非表示）[50%, 25%, 10%, 5%]
        self.ar_last_intervals: list[int]  = [3600, 1800, 600, 120, 60, 30]
        # 自動更新ダイアログ: 各行のチェック状態 [50%, 25%, 10%, 5%, 1%]
        self.ar_last_checks:   list[bool]  = [False, False, False, False, False]
        # 自動追加時のデフォルト間隔（スレ・カタログ別）
        self.ar_default_thread_intervals:  list = [3600, 1800, 600, 120, 60, 30]
        self.ar_default_thread_checks:     list = [False, False, False, False, False]
        self.ar_default_catalog_intervals: list = [3600]
        self.ar_default_catalog_checks:    list = []
        self.ar_use_default_thread:  bool = False
        self.ar_use_default_catalog: bool = False
        # スレを開いたとき自動的に自動更新に追加する
        self.auto_add_to_ar:        bool = False
        self.auto_add_catalog_to_ar: bool = False  # カタログを開いたとき自動更新に追加

        # ─────────────────────────────────────────────────────
        # 投稿設定 ★ 追加
        # ─────────────────────────────────────────────────────
        self.post_name: str = ""
        self.post_mail: str = ""
        self.post_save_name: bool = False  # おなまえを記憶するか
        self.post_save_mail: bool = False  # E-mailを記憶するか
        self.post_dialog_pin: bool = False # レスウィンドウを投稿後も閉じない
        self.del_hide_checked: bool = True # 「delしたレスを非表示にする」の記憶状態（既定ON）
        # ── ログ保存 ──────────────────────────────────────────────────────────
        self.log_save_dir: str = ""        # 保存先ディレクトリ（空=プログラム隣の logs/）
        self.log_save_images: bool = True  # アーカイブに画像を含める
        self.log_save_videos: bool = True  # アーカイブに動画を含める
        self.log_save_uploader: bool = True  # アーカイブにうｐろだを含める
        self.log_save_no_thumb: bool = False # サムネイルを保存せず本画像URLに差し替える
        # ── スレ落ち自動保存 ──────────────────────────────────────────────────
        self.log_auto_save:      bool = False  # スレ落ち時に自動保存
        self.log_auto_save_html: bool = True   # HTML を保存
        self.log_auto_save_mht:  bool = True   # MHT を保存
        self.log_auto_save_zip:  bool = True   # ZIP を保存
        # 開いているスレの本画像を表示中に先読みキャッシュ（スレ落ち保存の画像欠落防止）
        self.prefetch_open_thread_images: bool = True
        # クリップボード貼付け画像の形式 ("jpg" / "png")
        self.post_img_format:  str = "jpg"
        # JPEG 圧縮率 (1-100, 初期値 80)
        self.post_img_quality: int = 80
        # レスウインドウのサイズ記憶 [w, h]
        self.post_dialog_size: list = []
        # レスウインドウの位置記憶 [x, y]
        self.post_dialog_pos:  list = []
        # レスウインドウの分割位置記憶 (QSplitter.saveState hex文字列)
        self.post_dialog_splitter: str = ""
        # サンプル画像ビューア（返信ウインドウのプレビュークリック）のサイズ・位置記憶
        self.post_sample_view_size: list = []
        self.post_sample_view_pos:  list = []
        # レスウインドウのプレビュー/フォルダ分割位置 (QSplitter.saveState hex文字列)
        self.post_dialog_splitter2: str = ""
        # テーブル列幅 (JSON list文字列、空=""=デフォルト)
        self.table_col_widths_ng_word: str = ""
        self.table_col_widths_ar:      str = ""
        self.table_col_widths_history: str = ""
        # 板ごとの注意事項 開閉状態 { board_url: bool }  True=開く
        self.post_rules_open: dict = {}
        # 投稿後に自動でタブをピン留めする
        self.pin_after_post: bool = False
        # タブにスレ画サムネのアイコンを表示する
        self.show_tab_icon: bool = True
        # カタログタブにアイコンを表示する（theme/catalog.png）
        self.show_catalog_icon: bool = True
        # 保存残1/10以下のスレを赤字として扱う
        self.treat_near_limit_as_expiring: bool = False
        self.scroll_bottom_count: int = 30   # 末尾スクロール何回で更新するか
        self.scroll_top_count: int = 0   # 先頭スクロール何回で更新するか（0=無効）
        self.catalog_hover_zoom:    bool = False  # カタログオンマウスで画像拡大
        self.catalog_hover_comment: bool = False  # カタログオンマウスでスレ本文表示
        self.catalog_show_mail_badge:  bool = True  # サムネ右上にメール欄/IDバッジ表示
        self.catalog_quarantine_bottom: bool = True  # 隔離スレ(json非存在)を最下部表示
        self.catalog_common_id_bottom:  bool = False # 共通ID(mode=json id)スレを最下部にまとめる
        self.catalog_show_email:    bool = False  # カタログのメール欄バッジ表示
        self.recent_closed_max: int = 30     # 最近閉じたスレの保持件数
        self.recent_images_max: int = 30     # 最近開いた画像の保持件数
        self.cache_max_days: int = 7         # 画像キャッシュ保持日数（0=無制限）
        # ── キャッシュクリーンアップ設定（種別ごとに 日数 / サイズ上限） ──
        self.cache_img_days_enabled:    bool = True   # 画像: 日数で削除
        self.cache_img_size_enabled:    bool = False  # 画像: サイズ上限で削除
        self.cache_img_size_mb:         int  = 500    # 画像: 上限MB
        self.cache_video_days_enabled:  bool = True   # 動画: 日数で削除
        self.cache_video_days:          int  = 3      # 動画: 保持日数
        self.cache_video_size_enabled:  bool = False  # 動画: サイズ上限で削除
        self.cache_video_size_mb:       int  = 1024   # 動画: 上限MB
        self.cache_thread_days_enabled: bool = False  # スレHTML: 日数で削除
        self.cache_thread_days:         int  = 30     # スレHTML: 保持日数
        self.cache_thread_size_enabled: bool = False  # スレHTML: サイズ上限で削除
        self.cache_thread_size_mb:      int  = 200    # スレHTML: 上限MB
        # 永続化リスト（再起動後も保持）
        self.recent_closed_list: list = []   # [{board_url,board_name,thread_no,thread_url,label}, ...]
        self.recent_images_list: list = []   # [{url,name,board_name,board_url}, ...]
        self.download_workers: int = 4       # 並列ダウンロード数
        # 並行パースのセマフォ閾値 (KB)
        # この値以上のHTMLは同時に1スレッドしかパースしない（メモリ節約）
        # 小さいほどメモリ安定・大きいほど更新速度向上
        # 目安: 低スペックPC=50、普通=100〜150、ハイスペック=200以上
        self.parse_sem_kb: int = 50
        # タブ最大幅 (px, 0=無制限)
        self.tab_max_width: int = 0
        # IDが出ちゃったスレ(メール欄にID表示要求が無いのにIDが出ている)のタブをピンクにする
        self.tab_pink_op_no_id: bool = False
        # 隔離されたスレ(json∖cat)のタブをオレンジ、ID+隔離同時は #FF0099 にする
        self.tab_orange_quarantine: bool = True
        self.image_mode_cols: int = 6   # 画像モードの折り返し列数
        self.id_warn_count: int = 5   # ID出現回数がこの値以上ならIDを赤くする
        # スレオープンモード 0=通常 1=返信 2=画像 3=引用
        self.thread_open_mode:    int = 0  # アクティブで開く (0=返信, 1=画像, 2=引用)
        self.thread_open_bg_mode: int = 0  # バックグラウンドで開く
        self.image_display_mode:  int = 0  # 画像表示モード (0=タブ, 1=ウインドウ, 2=外部ブラウザ, 3=隣タブ)
        self.image_window_geometry: list | None = None  # 画像ウインドウの位置・サイズ [x,y,w,h]
        self.auto_close_dead_tab:    bool = False  # スレ落ち時にタブを自動で閉じる
        self.auto_close_full_tab:    bool = False  # 1000レス到達時にタブを自動で閉じる
        # 逆NG自動オープン由来のスレが落ちた時、グローバル設定に関わらず
        # タブを自動で閉じてメモリを解放する（自動オープン＋自動保存のため安全）。
        self.auto_close_dead_reverse_ng: bool = True
        self.auto_close_skip_pinned: bool = False  # ピン留めタブは閉じない
        self.log_auto_save_full:     bool = False  # 1000レス到達時にも自動保存する
        # ── 画像保存フォルダ ─────────────────────────────────────────────────
        self.image_save_folders: list = []   # 保存先フォルダリスト（先頭がデフォルト）
        self.image_save_btn_wrap: int = 3    # フォルダボタンの折り返し列数
        self.image_save_label_len: int = 0   # ボタンラベルの最大文字数（0=全表示）
        # 保存ボタン左クリックの保存形式（前回選んだ種類を記憶。初期値=zip）
        self.last_save_format: str = "zip"   # "html" | "mht" | "zip"
        # ログファイル命名テンプレート
        # 変数: {no}=スレ番号, {title}=OP1行目, {board}=板名,
        #        {date}=YYYYMMDD, {time}=HHMMSS, {datetime}=YYYYMMDD_HHMMSS,
        #        {逆NG}=マッチした逆NGワード（未マッチは空。{逆NG:文字}で未マッチ時の文字を指定）
        self.log_filename_template: str = "{date}/{date}_No.{no}_{title}"
        # ── 棒読みちゃん連携 ──────────────────────────────────────────────────
        self.bouyomi_enabled: bool = False
        self.bouyomi_host:    str  = "localhost"
        self.bouyomi_port:    int  = 50080
        self.bouyomi_speed:   int  = -1   # -1=棒読みちゃん側の設定に従う
        self.bouyomi_tone:    int  = -1
        self.bouyomi_volume:  int  = -1
        self.bouyomi_voice:   int  = 0    # 0=デフォルト
        self.bouyomi_format:  str  = "{comment}"  # 読み上げテキストテンプレート

        # ─────────────────────────────────────────────────────
        # NijiAppConfig 相当: アプリ全板共通設定 (デフォルト値)
        # ─────────────────────────────────────────────────────
        # [基本設定]
        self.http_link: bool             = True
        self.http_link_warn: bool        = True
        self.smooth_scroll: bool         = False
        self.smooth_scroll_interval: int = 20
        self.show_image_popup: bool      = False
        self.task_tray_type: int         = 0
        # [スレッドタブ]
        self.thread_tab_multi_row: bool      = True
        self.thread_tab_style: int           = 0
        self.thread_next_focus_loop: int     = 0
        self.tab_mouse_wheel_select: bool    = True
        self.thread_caption_len: int         = 20
        self.thread_header_auto: bool        = False
        self.thread_header_auto_interval: int = 500
        self.board_start_action: int         = 3
        self.url_count: int                  = 1
        # [レスポップアップ]
        self.res_popup_interval: int           = 500
        self.res_popup_selection_interval: int = 800
        self.res_popup_auto_remove: bool       = True
        self.res_popup_alt_key: bool           = False
        # [ログ]
        self.log_image_cache: bool  = True
        self.log_deleter: bool      = False
        self.log_deleter_day: int   = 7
        self.log_delete_exit: bool  = False
        self.sql_log_days: int      = 30
        # [カタログ]
        self.catalog_read_mark: bool       = True
        self.catalog_image_size: int       = 100
        self.catalog_image_turn: int       = 14
        self.catalog_turn_enabled: bool    = True
        self.catalog_turn_count: int       = 14
        self.catalog_few_res_hide: bool  = False
        self.catalog_few_res_count: int    = 5
        self.catalog_sort_type: int        = 0
        self.catalog_sort_desc: bool       = False
        self.theme: str                    = "dark"  # "dark" or "light"
        self.img_overlay_res: bool         = False   # 画像タブ「レス」オーバーレイ
        self.img_overlay_info: bool        = False   # 画像タブ「情報」オーバーレイ
        self.video_volume: int             = 80      # 動画音量 (0-100)
        # [表示]
        self.image_resize_use: bool     = True
        self.image_resize_size: int     = 200
        self.show_image_external: bool  = False
        self.disp_ikioi: bool           = False
        self.show_self_res_mark: bool   = True
        # 削除キー: 未設定の場合はランダム英数字8文字を自動生成
        _saved_key = ""  # load() で上書きされる
        self.delete_key: str = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8))
        # cxyl クッキー相当 (カタログ表示設定)
        self.cat_cols: int        = 14
        self.cat_rows: int        = 6
        self.cat_chars: int       = 4
        self.cat_text_pos: str     = "0:下"
        self.cat_img_size_str: str = "0:小"
        self.auto_fetch_on_open: bool      = True
        self.timeout_ms: int               = 30000
        self.thread_active: bool           = False
        self.link_memorized: bool          = True
        self.image_next_tab: bool          = False
        self.res_change_check: bool        = True
        self.thread_split_page_count: int  = 500
        self.show_res_number: bool         = True
        self.show_res_extraction: bool     = True
        self.show_res_controller: bool     = True
        self.res_ctrl_res: bool            = True
        self.res_ctrl_bookmark: bool       = False
        self.res_ctrl_ng: bool             = True
        self.res_ctrl_progress: bool       = True
        self.quotation_res_omission: bool  = True
        self.res_setting_share: bool       = False
        self.catalog_use_resize: bool      = False
        self.catalog_resize_size: int      = 50
        self.catalog_pool_show: bool       = False
        self.catalog_pool_capacity: int    = 150
        self.view_image_for_catalog: bool  = True
        self.board_thread_turn_count: int  = 1
        self.board_pool_show: bool         = False

        # ── キーボードショートカット ──────────────────────────────────────────
        # キー: アクションID  値: キーシーケンス文字列（空=デフォルト使用）
        self.shortcuts: dict = {}

        # ─────────────────────────────────────────────────────
        # NG画像リスト
        # 各エントリ: {
        #   "enabled": bool, "method": "type_size"|"file"|"md5",
        #   "image_type": str, "width": int, "height": int,
        #   "size_min": int, "size_max": int,
        #   "file_path": str, "md5": str,
        #   "last_hit": str, "expires": str,
        #   "is_reverse_ng": bool, "description": str
        # }
        # ─────────────────────────────────────────────────────
        self.ng_images: list[dict] = []

        # ─────────────────────────────────────────────────────
        # NG設定 – 設定[掲示板]タブ
        # ─────────────────────────────────────────────────────
        self.ng_board_hide_ng_thread: bool = True  # スレあきがNGの場合はスレッドを表示させない

        # ─────────────────────────────────────────────────────
        # NG設定 – 設定[スレッド・書き込み]タブ
        # ─────────────────────────────────────────────────────
        self.ng_thread_hide_name:  bool = True   # 名前・トリップ・書き込みがNGならレスを透明
        self.ng_thread_hide_image: bool = True   # 画像がNGならレスを透明
        self.ng_thread_close_ng:   bool = False  # NGスレッドを開いたら即閉じる

        # ─────────────────────────────────────────────────────
        # NG設定 – 設定[カタログ]タブ
        # ─────────────────────────────────────────────────────
        # ng_catalog_empty: 0=本文空のみNG, 1=NGにする, 2=何もしない
        self.ng_catalog_empty: int   = 2
        self.ng_catalog_pack:  bool  = True  # 無視スレを詰める
        self.ng_catalog_hide_common_id: bool = False  # 共通IDのスレをカタログから非表示

        # ─────────────────────────────────────────────────────
        # NG設定 – 設定[逆NG]タブ
        # ─────────────────────────────────────────────────────
        # ng_reverse_action: 0=何もしない, 1=非アクティブで開く, 2=開く,
        #                    3=ポップアップ通知, 4=カスタムアクション実行
        self.ng_reverse_action:         int  = 0
        self.ng_reverse_custom_action:  str  = ""
        self.ng_reverse_max_open:       int  = 99   # 逆NG同時ピップアップ件数上限
        self.ng_reverse_bouyomi_format: str  = "{keyword1}"  # 棒読み書式
        self.ng_word_notify_bouyomi_format: str = "{board} {word}: {comment}"  # NGワード通知棒読み書式
        # 優先順位コンボの選択インデックス (0〜)
        self.ng_priority_word_idx:  int = 0   # NGワード > 逆NGワード
        self.ng_priority_image_idx: int = 0   # NG画像 < 逆NG画像
        # 逆NG通知色（読む前）
        self.ng_reverse_unread_border: str = ""
        self.ng_reverse_unread_bg:     str = "#9B59B6"
        # 逆NG通知色（読んだ後）
        self.ng_reverse_read_border:   str = ""
        self.ng_reverse_read_bg:       str = "#E8E8E8"
        self.ng_reverse_use_default_color: bool = True

        # 逆NGで一度開いたスレURLのセット（再起動後も重複開きを防ぐ）
        self.ng_reverse_opened_urls: set[str] = set()
        self._ng_reverse_opened_list: list[str] = []  # FIFO順序管理用（上限2000件）

        # NGスレッドURL直接登録リスト
        self.ng_thread_urls: list[str] = []

        # レス内NGボタンで非表示にしたレスNo（キー=スレッドURL、値=レスNoリスト）
        self.ng_hidden_res_nos: dict[str, list[int]] = {}

        # del（削除依頼/記事削除）したレスNo（キー=スレッドURL、値=レスNoリスト）
        # 非表示とは別管理。No.の右に「del済」赤表示する目印に使う。
        self.del_res_nos: dict[str, list[int]] = {}

        # ── 自分のレス追跡 ──────────────────────────────────────────────────
        self.my_post_nos: dict[str, list[int]] = {}   # スレURL → レス番号リスト
        # ハイライト
        self.self_res_highlight:   bool = True   # 自分のレスに青帯
        # そうだね増加通知
        self.self_res_sodane_notify: bool = True
        self.self_res_sodane_duration: int = 5000   # ms
        # 返信通知
        self.self_res_reply_notify: bool = True
        self.self_res_reply_duration: int = 5000    # ms

        self.load()

    # ── ロード ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                raw = json.load(f)

            self._app                = raw.get("app", {})
            self.favorites           = raw.get("favorites", [])
            self.ng_words            = raw.get("ng_words", [])
            self.uploader_links      = raw.get("uploader_links", list(_DEFAULT_UPLOADERS))
            self.user_css_file       = raw.get("user_css_file", "theme/user.css")
            self.thread_history      = raw.get("thread_history", [])
            self.bookmarks           = raw.get("bookmarks", [dict(b) for b in _DEFAULT_BOOKMARKS])
            self.show_console        = bool(raw.get("show_console", False))
            self.custom_board_groups = raw.get("custom_board_groups", [])
            self.tab_state           = raw.get("tab_state", {})
            self.thread_read_counts  = raw.get("thread_read_counts", {})
            self.catalog_read_counts = raw.get("catalog_read_counts", {})
            self.catalog_view_states = raw.get("catalog_view_states", {})
            self.window_geometry     = raw.get("window_geometry", "")
            self.window_splitter     = raw.get("window_splitter", "")
            self.global_max_no       = raw.get("global_max_no", 0)
            _raw_gm = raw.get("global_max_no_by_board", {})
            # 異常値（隣接するNoと1,000,000以上差がある値）を除去してロード
            if isinstance(_raw_gm, dict):
                _all_nos = [v for v in _raw_gm.values() if isinstance(v, int) and v > 0]
                _median = sorted(_all_nos)[len(_all_nos)//2] if _all_nos else 0
                _sanitized = {}
                for k, v in _raw_gm.items():
                    if isinstance(v, int) and v > 0:
                        if _median == 0 or abs(v - _median) <= 5_000_000:
                            _sanitized[k] = v
                self.global_max_no_by_board = _sanitized
            else:
                self.global_max_no_by_board = {}
            _raw_ms = raw.get("max_saved_by_board", {})
            self.max_saved_by_board = {
                k: v for k, v in _raw_ms.items()
                if isinstance(v, int) and v > 0
            } if isinstance(_raw_ms, dict) else {}
            self.ar_last_intervals   = raw.get("ar_last_intervals", [3600, 1800, 600, 120, 60, 30])
            self.ar_last_checks      = raw.get("ar_last_checks",    [False]*5)
            self.ar_default_thread_intervals  = raw.get("ar_default_thread_intervals",  [3600, 1800, 600, 120, 60, 30])
            self.ar_default_thread_checks     = raw.get("ar_default_thread_checks",     [False]*5)
            self.ar_default_catalog_intervals = raw.get("ar_default_catalog_intervals", [600])
            self.ar_default_catalog_checks    = raw.get("ar_default_catalog_checks",    [])
            self.ar_use_default_thread        = raw.get("ar_use_default_thread",        False)
            self.ar_use_default_catalog       = raw.get("ar_use_default_catalog",       False)
            self.auto_add_to_ar        = raw.get("auto_add_to_ar",        False)
            self.auto_add_catalog_to_ar = raw.get("auto_add_catalog_to_ar", False)
            self.post_img_format     = raw.get("post_img_format",   "jpg")
            self.post_img_quality    = raw.get("post_img_quality",  80)
            self.post_dialog_size    = raw.get("post_dialog_size",  [])
            self.post_dialog_pos     = raw.get("post_dialog_pos",   [])
            self.post_sample_view_size = raw.get("post_sample_view_size", [])
            self.post_sample_view_pos  = raw.get("post_sample_view_pos",  [])
            self.post_dialog_splitter = raw.get("post_dialog_splitter", "")
            self.post_dialog_splitter2 = raw.get("post_dialog_splitter2", "")
            self.table_col_widths_ng_word = raw.get("table_col_widths_ng_word", "")
            self.table_col_widths_ar      = raw.get("table_col_widths_ar", "")
            self.table_col_widths_history = raw.get("table_col_widths_history", "")
            self.shortcuts = raw.get("shortcuts", {})
            # ── 自動更新間隔の旧形式移行（v0.8.078で分→秒・1%行追加）──
            # 旧: 5要素・分単位 / 新: 6要素・秒単位
            if isinstance(self.ar_last_intervals, list) and len(self.ar_last_intervals) == 5:
                self.ar_last_intervals = [int(v) * 60 for v in self.ar_last_intervals] + [30]
            if isinstance(self.ar_last_checks, list) and len(self.ar_last_checks) == 4:
                self.ar_last_checks = self.ar_last_checks + [False]
            if isinstance(self.ar_default_thread_intervals, list) and len(self.ar_default_thread_intervals) == 5:
                self.ar_default_thread_intervals = [int(v) * 60 for v in self.ar_default_thread_intervals] + [30]
            if isinstance(self.ar_default_thread_checks, list) and len(self.ar_default_thread_checks) == 4:
                self.ar_default_thread_checks = self.ar_default_thread_checks + [False]
            if (isinstance(self.ar_default_catalog_intervals, list)
                    and self.ar_default_catalog_intervals
                    and int(self.ar_default_catalog_intervals[0]) <= 120):
                self.ar_default_catalog_intervals = [int(self.ar_default_catalog_intervals[0]) * 60]

            # ★ 板リストのデシリアライズ
            self.boards = [
                BoardInfo(
                    name  = b["name"],
                    url   = b["url"],
                    group = b.get("group", "未分類"),
                )
                for b in raw.get("boards", [])
                if b.get("name") and b.get("url")
            ]

            # ★ NG画像・NG追加設定
            self.ng_images               = raw.get("ng_images", [])
            self.ng_board_hide_ng_thread = raw.get("ng_board_hide_ng_thread", True)
            self.ng_thread_hide_name     = raw.get("ng_thread_hide_name",  True)
            self.ng_thread_hide_image    = raw.get("ng_thread_hide_image", True)
            self.ng_thread_close_ng      = raw.get("ng_thread_close_ng",  False)
            self.ng_catalog_empty        = raw.get("ng_catalog_empty", 2)
            self.ng_catalog_pack         = raw.get("ng_catalog_pack",  True)
            self.ng_catalog_hide_common_id = bool(raw.get("ng_catalog_hide_common_id", False))
            self.ng_reverse_action        = raw.get("ng_reverse_action", 0)
            self.ng_reverse_custom_action  = raw.get("ng_reverse_custom_action", "")
            self.ng_reverse_max_open       = raw.get("ng_reverse_max_open", 99)
            self.ng_reverse_bouyomi_format = raw.get("ng_reverse_bouyomi_format", "{keyword1}")
            self.ng_word_notify_bouyomi_format = raw.get("ng_word_notify_bouyomi_format", "{board} {word}: {comment}")
            self.ng_priority_word_idx    = raw.get("ng_priority_word_idx",  0)
            self.ng_priority_image_idx   = raw.get("ng_priority_image_idx", 0)
            self.ng_reverse_unread_border = raw.get("ng_reverse_unread_border", "")
            self.ng_reverse_unread_bg    = raw.get("ng_reverse_unread_bg", "#9B59B6")
            self.ng_reverse_read_border  = raw.get("ng_reverse_read_border", "")
            self.ng_reverse_read_bg      = raw.get("ng_reverse_read_bg",  "#E8E8E8")
            self.ng_reverse_use_default_color = raw.get("ng_reverse_use_default_color", True)
            _rev_list = raw.get("ng_reverse_opened_urls", [])
            self.ng_reverse_opened_urls = set(_rev_list)
            self._ng_reverse_opened_list = list(_rev_list)  # FIFO順序管理用
            self.ng_thread_urls = raw.get("ng_thread_urls", [])
            self.ng_hidden_res_nos = {
                k: list(map(int, v))
                for k, v in raw.get("ng_hidden_res_nos", {}).items()
            }
            self.del_res_nos = {
                k: list(map(int, v))
                for k, v in raw.get("del_res_nos", {}).items()
            }
            # 自分のレス追跡
            self.my_post_nos = {
                k: list(map(int, v))
                for k, v in raw.get("my_post_nos", {}).items()
            }
            self.self_res_highlight        = bool(raw.get("self_res_highlight",        True))
            self.self_res_sodane_notify    = bool(raw.get("self_res_sodane_notify",    True))
            self.self_res_sodane_duration  = int( raw.get("self_res_sodane_duration",  5000))
            self.self_res_reply_notify     = bool(raw.get("self_res_reply_notify",     True))
            self.self_res_reply_duration   = int( raw.get("self_res_reply_duration",   5000))

            # ★ 投稿設定
            self.post_name = raw.get("post_name", "")
            self.post_mail = raw.get("post_mail", "")
            self.post_save_name = raw.get("post_save_name", False)
            self.post_save_mail = raw.get("post_save_mail", False)
            self.post_dialog_pin = raw.get("post_dialog_pin", False)
            self.del_hide_checked = raw.get("del_hide_checked", True)
            self.post_rules_open = raw.get("post_rules_open", {})
            self.pin_after_post  = raw.get("pin_after_post",  False)
            self.show_tab_icon      = raw.get("show_tab_icon",      True)
            self.show_catalog_icon  = raw.get("show_catalog_icon",  True)
            self.treat_near_limit_as_expiring = raw.get("treat_near_limit_as_expiring", False)
            self.scroll_bottom_count = int(raw.get("scroll_bottom_count", 30))
            self.scroll_top_count = int(raw.get("scroll_top_count", 0))
            self.catalog_hover_zoom    = bool(raw.get("catalog_hover_zoom",    False))
            self.catalog_hover_comment = bool(raw.get("catalog_hover_comment", False))
            self.catalog_show_mail_badge  = bool(raw.get("catalog_show_mail_badge",  True))
            self.catalog_quarantine_bottom = bool(raw.get("catalog_quarantine_bottom", True))
            self.catalog_common_id_bottom = bool(raw.get("catalog_common_id_bottom", False))
            self.catalog_show_email    = bool(raw.get("catalog_show_email",    False))
            self.recent_closed_max = min(100, max(1, int(raw.get("recent_closed_max", 30))))
            self.recent_images_max = min(100, max(1, int(raw.get("recent_images_max", 30))))
            self.cache_max_days = max(0, int(raw.get("cache_max_days", 7)))
            self.cache_img_days_enabled    = bool(raw.get("cache_img_days_enabled", True))
            self.cache_img_size_enabled    = bool(raw.get("cache_img_size_enabled", False))
            self.cache_img_size_mb         = max(1, int(raw.get("cache_img_size_mb", 500)))
            self.cache_video_days_enabled  = bool(raw.get("cache_video_days_enabled", True))
            self.cache_video_days          = max(1, int(raw.get("cache_video_days", 3)))
            self.cache_video_size_enabled  = bool(raw.get("cache_video_size_enabled", False))
            self.cache_video_size_mb       = max(1, int(raw.get("cache_video_size_mb", 1024)))
            self.cache_thread_days_enabled = bool(raw.get("cache_thread_days_enabled", False))
            self.cache_thread_days         = max(1, int(raw.get("cache_thread_days", 30)))
            self.cache_thread_size_enabled = bool(raw.get("cache_thread_size_enabled", False))
            self.cache_thread_size_mb      = max(1, int(raw.get("cache_thread_size_mb", 200)))
            self.recent_closed_list = raw.get("recent_closed_list", [])
            self.recent_images_list = raw.get("recent_images_list", [])
            self.download_workers = int(raw.get("download_workers", 4))
            self.parse_sem_kb = int(raw.get("parse_sem_kb", 50))
            self.tab_max_width = int(raw.get("tab_max_width", 0))
            self.tab_pink_op_no_id = bool(raw.get("tab_pink_op_no_id", False))
            self.tab_orange_quarantine = bool(raw.get("tab_orange_quarantine", True))
            self.image_mode_cols = int(raw.get("image_mode_cols", 6))
            self.id_warn_count = int(raw.get("id_warn_count", 5))
            self.theme = str(raw.get("theme", "dark"))
            self.thread_open_mode    = int(raw.get("thread_open_mode", 0))
            self.thread_open_bg_mode = int(raw.get("thread_open_bg_mode", 0))
            self.image_display_mode  = int(raw.get("image_display_mode", 0))
            # 旧設定の移行: 「外部ブラウザ/隣タブ」を画像表示モードに統合 (v0.9.42)
            if self.image_display_mode == 0:
                if raw.get("show_image_external", False):
                    self.image_display_mode = 2
                elif raw.get("image_next_tab", False):
                    self.image_display_mode = 3
            self.image_window_geometry = raw.get("image_window_geometry", None)
            self.auto_close_dead_tab    = raw.get("auto_close_dead_tab",    False)
            self.auto_close_full_tab    = raw.get("auto_close_full_tab",    False)
            self.auto_close_dead_reverse_ng = raw.get("auto_close_dead_reverse_ng", True)
            self.auto_close_skip_pinned = raw.get("auto_close_skip_pinned", False)
            self.log_auto_save_full     = raw.get("log_auto_save_full",     False)
            self.image_save_folders  = raw.get("image_save_folders",  [])
            self.image_save_btn_wrap = int(raw.get("image_save_btn_wrap", 3))
            self.image_save_label_len = int(raw.get("image_save_label_len", 0))
            _lsf = str(raw.get("last_save_format", "zip")).lower()
            self.last_save_format = _lsf if _lsf in ("html", "mht", "zip") else "zip"
            self.log_filename_template = raw.get("log_filename_template", "{date}/{date}_No.{no}_{title}")
            self.bouyomi_enabled = raw.get("bouyomi_enabled", False)
            self.bouyomi_host    = raw.get("bouyomi_host",    "localhost")
            self.bouyomi_port    = int(raw.get("bouyomi_port", 50080))
            self.bouyomi_speed   = int(raw.get("bouyomi_speed",  -1))
            self.bouyomi_tone    = int(raw.get("bouyomi_tone",   -1))
            self.bouyomi_volume  = int(raw.get("bouyomi_volume", -1))
            self.bouyomi_voice   = int(raw.get("bouyomi_voice",   0))
            self.bouyomi_format  = raw.get("bouyomi_format", "{comment}")
            self.log_save_dir    = raw.get("log_save_dir",    "")
            self.log_save_images = raw.get("log_save_images", True)
            self.log_save_no_thumb = raw.get("log_save_no_thumb", False)
            self.log_save_videos = raw.get("log_save_videos", True)
            self.log_save_uploader = raw.get("log_save_uploader", True)
            self.log_auto_save      = raw.get("log_auto_save",      False)
            self.log_auto_save_html = raw.get("log_auto_save_html", True)
            self.log_auto_save_mht  = raw.get("log_auto_save_mht",  True)
            self.log_auto_save_zip  = raw.get("log_auto_save_zip",  True)
            self.prefetch_open_thread_images = raw.get("prefetch_open_thread_images", True)

            # NijiAppConfig 相当設定の読み込み
            cfg = raw.get("app_config", {})
            for key, default in [
                ("http_link", True), ("http_link_warn", True),
                ("smooth_scroll", False), ("smooth_scroll_interval", 20),
                ("show_image_popup", False), ("task_tray_type", 0),
                ("thread_tab_multi_row", True), ("thread_tab_style", 0),
                ("thread_next_focus_loop", 0), ("tab_mouse_wheel_select", True),
                ("thread_caption_len", 20), ("thread_header_auto", False),
                ("thread_header_auto_interval", 500),
                ("res_popup_interval", 500), ("res_popup_selection_interval", 800),
                ("res_popup_auto_remove", True), ("res_popup_alt_key", False),
                ("log_image_cache", True), ("log_deleter", False),
                ("log_deleter_day", 7), ("log_delete_exit", False), ("sql_log_days", 30),
                ("catalog_read_mark", True), ("catalog_image_size", 100),
                ("catalog_image_turn", 14), ("catalog_turn_enabled", True),
                ("catalog_turn_count", 14),
                ("catalog_few_res_hide", False), ("catalog_few_res_count", 5), ("catalog_sort_type", 0),
                ("catalog_sort_desc", False),
                ("img_overlay_res", False), ("img_overlay_info", False),
                ("video_volume", 80),
                ("image_resize_use", True), ("image_resize_size", 200),
                ("show_image_external", False), ("disp_ikioi", False),
                ("show_self_res_mark", True), ("delete_key", None),
                ("board_start_action", 3), ("url_count", 1),
                ("cat_cols", 14), ("cat_rows", 6), ("cat_chars", 4),
                ("cat_text_pos", "0:下"), ("cat_img_size_str", "0:小"),
            ]:
                if hasattr(self, key):
                    setattr(self, key, cfg.get(key, default))

            # delete_key: JSON に保存されていない (None) か空ならランダム生成済み値を保持
            if not self.delete_key:
                self.delete_key = "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=8))

        except Exception as e:
            print(f"[Settings] 読み込みエラー: {e}")

    # ── セーブ ──────────────────────────────────────────────────────────────

    def save(self) -> None:
        try:
            # イテレーション中に別スレッドから変更される可能性のある辞書はコピーを取る
            _thread_read  = dict(self.thread_read_counts)
            _catalog_read = dict(self.catalog_read_counts)
            _ng_hidden    = {k: list(v) for k, v in self.ng_hidden_res_nos.items() if v}
            _del_res      = {k: list(v) for k, v in self.del_res_nos.items() if v}
            _global_max   = dict(self.global_max_no_by_board)
            _max_saved_bb = dict(self.max_saved_by_board)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "app":                  self._app,
                        "favorites":            self.favorites,
                        "ng_words":             self.ng_words,
                        "ng_images":            self.ng_images,
                        "ng_board_hide_ng_thread": self.ng_board_hide_ng_thread,
                        "ng_thread_hide_name":  self.ng_thread_hide_name,
                        "ng_thread_hide_image": self.ng_thread_hide_image,
                        "ng_thread_close_ng":   self.ng_thread_close_ng,
                        "ng_catalog_empty":     self.ng_catalog_empty,
                        "ng_catalog_pack":      self.ng_catalog_pack,
                        "ng_catalog_hide_common_id": self.ng_catalog_hide_common_id,
                        "ng_reverse_action":         self.ng_reverse_action,
                        "ng_reverse_custom_action":  self.ng_reverse_custom_action,
                        "ng_reverse_max_open":        self.ng_reverse_max_open,
                        "ng_reverse_bouyomi_format":  self.ng_reverse_bouyomi_format,
                        "ng_word_notify_bouyomi_format": self.ng_word_notify_bouyomi_format,
                        "ng_priority_word_idx": self.ng_priority_word_idx,
                        "ng_priority_image_idx":self.ng_priority_image_idx,
                        "ng_reverse_unread_border": self.ng_reverse_unread_border,
                        "ng_reverse_unread_bg": self.ng_reverse_unread_bg,
                        "ng_reverse_read_border": self.ng_reverse_read_border,
                        "ng_reverse_read_bg":   self.ng_reverse_read_bg,
                        "ng_reverse_use_default_color": self.ng_reverse_use_default_color,
                        "ng_reverse_opened_urls": self._ng_reverse_opened_list[-2000:],
                        "ng_thread_urls": self.ng_thread_urls,
                        "ng_hidden_res_nos": _ng_hidden,
                        "del_res_nos": _del_res,
                        "my_post_nos": {k: list(v) for k, v in self.my_post_nos.items() if v},
                        "self_res_highlight":       self.self_res_highlight,
                        "self_res_sodane_notify":   self.self_res_sodane_notify,
                        "self_res_sodane_duration": self.self_res_sodane_duration,
                        "self_res_reply_notify":    self.self_res_reply_notify,
                        "self_res_reply_duration":  self.self_res_reply_duration,
                        "uploader_links":       self.uploader_links,
                        "user_css_file":        self.user_css_file,
                        "thread_history":       self.thread_history,
                        "bookmarks":            self.bookmarks,
                        "show_console":         self.show_console,
                        "custom_board_groups":  self.custom_board_groups,
                        "tab_state":            self.tab_state,
                        "thread_read_counts":   _thread_read,
                        "catalog_read_counts":  _catalog_read,
                        # ★ 板リストのシリアライズ
                        "boards": [
                            {"name": b.name, "url": b.url, "group": b.group}
                            for b in self.boards
                        ],
                        # ★ 投稿設定
                        "post_name": self.post_name,
                        "post_mail": self.post_mail,
                        "post_save_name": self.post_save_name,
                        "post_save_mail": self.post_save_mail,
                        "post_dialog_pin": self.post_dialog_pin,
                        "del_hide_checked": self.del_hide_checked,
                        "post_rules_open": self.post_rules_open,
                        "pin_after_post":  self.pin_after_post,
                        "show_tab_icon":        self.show_tab_icon,
                        "show_catalog_icon":    self.show_catalog_icon,
                        "treat_near_limit_as_expiring": self.treat_near_limit_as_expiring,
                        "scroll_bottom_count": self.scroll_bottom_count,
                        "scroll_top_count": self.scroll_top_count,
                        "catalog_hover_zoom":    self.catalog_hover_zoom,
                        "catalog_hover_comment": self.catalog_hover_comment,
                        "catalog_show_mail_badge":  self.catalog_show_mail_badge,
                        "catalog_quarantine_bottom": self.catalog_quarantine_bottom,
                        "catalog_common_id_bottom":  self.catalog_common_id_bottom,
                        "catalog_show_email":    self.catalog_show_email,
                        "recent_closed_max": self.recent_closed_max,
                        "recent_images_max": self.recent_images_max,
                        "cache_max_days": self.cache_max_days,
                        "cache_img_days_enabled":    self.cache_img_days_enabled,
                        "cache_img_size_enabled":    self.cache_img_size_enabled,
                        "cache_img_size_mb":         self.cache_img_size_mb,
                        "cache_video_days_enabled":  self.cache_video_days_enabled,
                        "cache_video_days":          self.cache_video_days,
                        "cache_video_size_enabled":  self.cache_video_size_enabled,
                        "cache_video_size_mb":       self.cache_video_size_mb,
                        "cache_thread_days_enabled": self.cache_thread_days_enabled,
                        "cache_thread_days":         self.cache_thread_days,
                        "cache_thread_size_enabled": self.cache_thread_size_enabled,
                        "cache_thread_size_mb":      self.cache_thread_size_mb,
                        "recent_closed_list": self.recent_closed_list,
                        "recent_images_list": self.recent_images_list,
                        "download_workers": self.download_workers,
                        "parse_sem_kb": self.parse_sem_kb,
                        "tab_max_width": self.tab_max_width,
                        "tab_pink_op_no_id": self.tab_pink_op_no_id,
                        "tab_orange_quarantine": self.tab_orange_quarantine,
                        "image_mode_cols": self.image_mode_cols,
                        "id_warn_count": self.id_warn_count,
                        "theme": self.theme,
                        "thread_open_mode":    self.thread_open_mode,
                        "thread_open_bg_mode": self.thread_open_bg_mode,
                        "image_display_mode":  self.image_display_mode,
                        "image_window_geometry": self.image_window_geometry,
                        "auto_close_dead_tab":    self.auto_close_dead_tab,
                        "auto_close_full_tab":    self.auto_close_full_tab,
                        "auto_close_dead_reverse_ng": self.auto_close_dead_reverse_ng,
                        "auto_close_skip_pinned": self.auto_close_skip_pinned,
                        "log_auto_save_full":     self.log_auto_save_full,
                        "image_save_folders":   self.image_save_folders,
                        "image_save_btn_wrap":  self.image_save_btn_wrap,
                        "image_save_label_len": self.image_save_label_len,
                        "last_save_format":     self.last_save_format,
                        "log_filename_template": self.log_filename_template,
                        "bouyomi_enabled": self.bouyomi_enabled,
                        "bouyomi_host":    self.bouyomi_host,
                        "bouyomi_port":    self.bouyomi_port,
                        "bouyomi_speed":   self.bouyomi_speed,
                        "bouyomi_tone":    self.bouyomi_tone,
                        "bouyomi_volume":  self.bouyomi_volume,
                        "bouyomi_voice":   self.bouyomi_voice,
                        "bouyomi_format":  self.bouyomi_format,
                        "log_save_dir":    self.log_save_dir,
                        "log_save_images": self.log_save_images,
                        "log_save_no_thumb": self.log_save_no_thumb,
                        "log_save_videos": self.log_save_videos,
                        "log_save_uploader": self.log_save_uploader,
                        "log_auto_save":      self.log_auto_save,
                        "log_auto_save_html": self.log_auto_save_html,
                        "log_auto_save_mht":  self.log_auto_save_mht,
                        "log_auto_save_zip":  self.log_auto_save_zip,
                        "prefetch_open_thread_images": self.prefetch_open_thread_images,
                        "app_config": self._dump_app_config(),
                        "img_overlay_res":  self.img_overlay_res,
                        "img_overlay_info": self.img_overlay_info,
                        "window_geometry": self.window_geometry,
                        "window_splitter": self.window_splitter,
                        "catalog_view_states": self.catalog_view_states,
                        "global_max_no": self.global_max_no,
                        "global_max_no_by_board": _global_max,
                        "max_saved_by_board": _max_saved_bb,
                        "ar_last_intervals": self.ar_last_intervals,
                        "ar_last_checks":    self.ar_last_checks,
                        "ar_default_thread_intervals":  self.ar_default_thread_intervals,
                        "ar_default_thread_checks":     self.ar_default_thread_checks,
                        "ar_default_catalog_intervals": self.ar_default_catalog_intervals,
                        "ar_default_catalog_checks":    self.ar_default_catalog_checks,
                        "ar_use_default_thread":        self.ar_use_default_thread,
                        "ar_use_default_catalog":       self.ar_use_default_catalog,
                        "auto_add_to_ar":         self.auto_add_to_ar,
                        "auto_add_catalog_to_ar": self.auto_add_catalog_to_ar,
                        "post_img_format":   self.post_img_format,
                        "post_img_quality":  self.post_img_quality,
                        "post_dialog_size":  self.post_dialog_size,
                        "post_dialog_pos":   self.post_dialog_pos,
                        "post_sample_view_size": self.post_sample_view_size,
                        "post_sample_view_pos":  self.post_sample_view_pos,
                        "post_dialog_splitter": self.post_dialog_splitter,
                        "post_dialog_splitter2": self.post_dialog_splitter2,
                        "table_col_widths_ng_word": self.table_col_widths_ng_word,
                        "table_col_widths_ar":      self.table_col_widths_ar,
                        "table_col_widths_history": self.table_col_widths_history,
                        "shortcuts": self.shortcuts,
                    },
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            print(f"[Settings] 保存エラー: {e}")

    def _dump_app_config(self) -> dict:
        """NijiAppConfig 相当の全設定を dict に変換"""
        keys = [
            "http_link", "http_link_warn", "smooth_scroll", "smooth_scroll_interval",
            "show_image_popup", "task_tray_type", "thread_tab_multi_row", "thread_tab_style",
            "thread_next_focus_loop", "tab_mouse_wheel_select", "thread_caption_len",
            "thread_header_auto", "thread_header_auto_interval",
            "res_popup_interval", "res_popup_selection_interval",
            "res_popup_auto_remove", "res_popup_alt_key",
            "log_image_cache", "log_deleter", "log_deleter_day", "log_delete_exit", "sql_log_days",
            "catalog_read_mark", "catalog_image_size", "catalog_image_turn",
            "catalog_turn_enabled", "catalog_turn_count",
            "catalog_few_res_hide", "catalog_few_res_count", "catalog_sort_type", "catalog_sort_desc",
            "video_volume",
            "image_resize_use", "image_resize_size", "show_image_external",
            "disp_ikioi", "show_self_res_mark", "delete_key", "board_start_action", "url_count",
            "cat_cols", "cat_rows", "cat_chars", "cat_text_pos", "cat_img_size_str",
            "auto_fetch_on_open", "timeout_ms", "thread_active", "link_memorized",
            "image_next_tab", "res_change_check", "thread_split_page_count",
            "show_res_number", "show_res_extraction", "show_res_controller",
            "res_ctrl_res", "res_ctrl_bookmark", "res_ctrl_ng", "res_ctrl_progress",
            "quotation_res_omission", "res_setting_share",
            "catalog_use_resize", "catalog_resize_size", "catalog_pool_show",
            "catalog_pool_capacity", "view_image_for_catalog",
            "board_thread_turn_count", "board_pool_show",
        ]
        return {k: getattr(self, k) for k in keys if hasattr(self, k)}

    # ── 汎用 getter/setter ──────────────────────────────────────────────────

    def get(self, key, default=None):
        return self._app.get(key, default)

    def set(self, key, value) -> None:
        self._app[key] = value

    # ── カタログ設定 ─────────────────────────────────────────────────────────

    @property
    def catalog_cxyl_str(self) -> str:
        """AppSettings のカタログ設定から cxyl クッキー文字列を生成する。"""
        # 新形式(下/右/左/上) と旧形式(0:下 等) 両対応
        _pos_map = {"下": 0, "右": 1, "左": 2, "上": 3,
                    "0:下": 0, "1:右": 1, "2:左": 2, "3:上": 3}
        # 新形式(小/1/2/3/4/5/大) と旧形式(0:小 等) 両対応
        _img_map = {"小": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "大": 6,
                    "0:小": 0, "1:中": 2, "2:大": 6}
        cols  = self.cat_cols
        rows  = self.cat_rows
        chars = self.cat_chars
        pos   = _pos_map.get(self.cat_text_pos, 0)
        imgsz = _img_map.get(self.cat_img_size_str, 0)
        return f"{cols}x{rows}x{chars}x{pos}x{imgsz}"

    # ── 履歴・お気に入り ──────────────────────────────────────────────────────

    def add_history(self, board_name: str, no: int, title: str, board_url: str = "") -> None:
        # 既存エントリの「最後に書き込んだ日時」を引き継ぐ（再オープンで消さない）
        _prev_posted = ""
        for h in self.thread_history:
            if h.get("board") == board_name and h.get("no") == no:
                _prev_posted = h.get("posted", "")
                break
        entry = {
            "board": board_name, "no": no, "title": title,
            "time": time.strftime("%Y/%m/%d %H:%M:%S"),
            "url":  board_url,
            "posted": _prev_posted,
        }
        self.thread_history = [
            h for h in self.thread_history
            if not (h["board"] == board_name and h["no"] == no)
        ]
        self.thread_history.insert(0, entry)
        self.thread_history = self.thread_history[:500]

    def mark_history_posted(self, board_name: str, no: int) -> bool:
        """スレッド履歴の該当エントリに「最後に書き込んだ日時」を記録する。
        投稿成功時に呼ぶ。該当エントリが無ければ False。"""
        ts = time.strftime("%Y/%m/%d %H:%M:%S")
        for h in self.thread_history:
            if h.get("board") == board_name and h.get("no") == no:
                h["posted"] = ts
                return True
        return False

    def add_favorite(self, name: str, url: str) -> None:
        if any(f["url"] == url for f in self.favorites):
            return
        self.favorites.append({"name": name, "url": url})
        self.save()


    # ── カスタム板グループ ────────────────────────────────────────────────────

    def get_custom_group(self, name: str) -> Optional[dict]:
        return next((g for g in self.custom_board_groups if g["name"] == name), None)

    def add_board_to_group(self, group_name: str, board_name: str, url: str) -> bool:
        group = self.get_custom_group(group_name)
        if group is None:
            group = {"name": group_name, "boards": []}
            self.custom_board_groups.append(group)
        if any(b["url"] == url for b in group["boards"]):
            return False
        group["boards"].append({"name": board_name, "url": url})
        self.save()
        return True

    def remove_board_from_group(self, group_name: str, url: str) -> bool:
        group = self.get_custom_group(group_name)
        if group is None:
            return False
        before = len(group["boards"])
        group["boards"] = [b for b in group["boards"] if b["url"] != url]
        if len(group["boards"]) < before:
            self.save()
            return True
        return False

    def all_custom_urls(self) -> set:
        result = set()
        for g in self.custom_board_groups:
            for b in g.get("boards", []):
                result.add(b["url"])
        return result

    # ── NgFilter シングルトン ─────────────────────────────────────────────────
    @property
    def ng_filter(self) -> "NgFilter":
        """NgFilterインスタンスを返す（初回のみ生成、以降使い回し）"""
        if not hasattr(self, "_ng_filter_instance") or self._ng_filter_instance is None:
            self._ng_filter_instance = NgFilter(self)
        return self._ng_filter_instance

    def invalidate_ng_cache(self) -> None:
        """ng_words/ng_images変更後にNgFilterのキャッシュを無効化する"""
        if hasattr(self, "_ng_filter_instance") and self._ng_filter_instance is not None:
            self._ng_filter_instance.invalidate_cache()


# ══════════════════════════════════════════════════════════════════════════════
_SENTINEL = object()   # NgFilter._compiled の「未登録」マーカー

class NgFilter:
    """NGワード・NG画像・逆NGの判定クラス"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._compiled: dict[str, re.Pattern | None] = {}  # コンパイル済みパターンキャッシュ
        self._flat_words:   list | None = None
        self._flat_replaces: list | None = None
        self._cat_cls_cache: dict[tuple, tuple[bool, bool]] = {}  # (title,title_chars) → (is_ng,is_rev) カタログ分類メモ

    def invalidate_cache(self) -> None:
        """ng_words/ng_images変更後にキャッシュをすべてクリアする"""
        self._compiled.clear()
        self._flat_words    = None
        self._flat_replaces = None
        self._cat_cls_cache.clear()

    # ── ヘルパー: 有効期限チェック ───────────────────────────────────────────
    @staticmethod
    def _is_expired(ng: dict) -> bool:
        """エントリが期限切れかどうか（expires_atフィールドで判定）"""
        expires_at = ng.get("expires_at", "")
        if not expires_at:
            return False
        import datetime
        try:
            exp_date = datetime.date.fromisoformat(expires_at)
            return datetime.date.today() > exp_date
        except ValueError:
            return False

    # ── ヘルパー: パターンコンパイル ─────────────────────────────────────────
    def _compile_one(self, pattern: str) -> "re.Pattern | None":
        rxp = self._compiled.get(pattern, _SENTINEL)
        if rxp is _SENTINEL:
            try:
                rxp = re.compile(pattern, re.IGNORECASE)
            except re.error:
                rxp = None
            self._compiled[pattern] = rxp
        return rxp  # type: ignore[return-value]


    # ── フラット化キャッシュ構築 ──────────────────────────────────────────────
    def _ensure_flat(self) -> None:
        if self._flat_words is not None:
            return
        self._cat_cls_cache.clear()   # フラット再構築時は分類メモも無効
        import datetime
        today = datetime.date.today()
        flat_words:   list = []
        flat_replaces: list = []
        for ng in self._settings.ng_words:
            if not ng.get("enabled", True):
                continue
            ea = ng.get("expires_at", "")
            if ea:
                try:
                    if today > datetime.date.fromisoformat(ea):
                        continue
                except ValueError:
                    pass
            pat = ng.get("pattern", "").strip()
            ng_type = ng.get("ng_type", "ng")
            # 芝刈り置換はパターン不要なのでpattern空チェックを先に分岐
            if ng_type == "mow_replace":
                flat_replaces.append((None, "", "mow_replace", ng.get("replace_str", "")))
                continue
            if not pat:
                continue
            rxp = self._compile_one(pat)
            if ng_type in ("replace", "mow_replace"):
                flat_replaces.append((rxp, pat, ng_type, ng.get("replace_str", "")))
            else:
                has_any = any(ng.get(k) for k in
                    ("scope_body","scope_name","scope_subject",
                     "scope_mail","scope_id","scope_ip"))
                scope = (
                    bool(ng.get("scope_body",    not has_any)),
                    bool(ng.get("scope_name",    False)),
                    bool(ng.get("scope_subject", False)),
                    bool(ng.get("scope_mail",    False)),
                    bool(ng.get("scope_id",      False)),
                    bool(ng.get("scope_ip",      False)),
                    bool(ng.get("scope_catalog", False)),
                )
                flat_words.append((rxp, pat, ng_type, scope))
        self._flat_words    = flat_words
        self._flat_replaces = flat_replaces

    # ── NG/逆NG共通ワード判定（フラット化キャッシュ使用） ────────────────────
    def classify_res(self, res: "ResData") -> str:
        """レスのNG分類を返す: 'ng' / 'reverse_ng' / 'none'"""
        self._ensure_flat()
        is_ng = is_rev = False
        body    = res.comment_text or ""
        name    = res.name or ""
        trip    = res.trip or ""
        subject = res.subject if hasattr(res, "subject") else ""
        mail    = res.email or ""
        id_str  = res.id_str or ""
        for rxp, pat, ng_type, scope in self._flat_words:
            targets = []
            if scope[0] and body:    targets.append(body)
            if scope[1]:
                if name: targets.append(name)
                if trip: targets.append(trip)
            if scope[2] and subject: targets.append(subject)
            if scope[3] and mail:    targets.append(mail)
            if scope[4] and id_str:  targets.append(id_str)
            if not targets:
                continue
            matched = any(bool(rxp.search(t)) if rxp else pat.lower() in t.lower()
                          for t in targets)
            if not matched:
                continue
            if ng_type == "ng":           is_ng  = True
            elif ng_type == "reverse_ng": is_rev = True
            if is_ng and is_rev:
                break
        # レス単位の逆NGは「NG非表示の打ち消し（ホワイトリスト）」には使わない。
        # NGワードにヒットしていれば、逆NGの有無・優先設定に関わらず "ng" を返す。
        # （ng_priority_word_idx はカタログ判定 classify_catalog 側でのみ有効）
        if is_ng:  return "ng"
        if is_rev: return "reverse_ng"
        return "none"

    def is_ng(self, res: "ResData") -> bool:
        return self.classify_res(res) == "ng"

    def is_reverse_ng(self, res: "ResData") -> bool:
        return self.classify_res(res) == "reverse_ng"


    # ── NGワード: カタログエントリ判定（フラット化キャッシュ使用） ────────────
    def _classify_ng_catalog_1pass(self, entry, title_chars: int = -1) -> tuple[bool, bool]:
        self._ensure_flat()
        title = entry.title or ""
        if not title:
            return False, False
        # 逆NGは板のカタログ表示文字数(cat_chars)までに切り詰めたタイトルで判定する
        # （カタログに表示されない文字での逆NG＝自動オープンを防ぐ）。
        #   title_chars <  0 … 制限なし（全文）。判定不能時のフォールバック等。
        #   title_chars == 0 … 空（カタログがタイトル非表示の板＝逆NG判定対象なし）。
        #   title_chars >  0 … 先頭 title_chars 文字。
        # NG(非表示)はタイトル全文のまま（title_chars 非依存）。
        rev_title = title if title_chars < 0 else title[:title_chars]
        # タイトル＋判定文字数でメモ化（同一レンダリング内の多重判定・再計算を排除）
        cache_key = (title, title_chars)
        cached = self._cat_cls_cache.get(cache_key)
        if cached is not None:
            return cached
        is_ng = is_rev = False
        for rxp, pat, ng_type, scope in self._flat_words:
            if not scope[0] and not scope[6]:
                continue
            _t = rev_title if ng_type == "reverse_ng" else title
            hit = bool(rxp.search(_t)) if rxp else pat.lower() in _t.lower()
            if not hit:
                continue
            if ng_type == "ng":           is_ng  = True
            elif ng_type == "reverse_ng": is_rev = True
            if is_ng and is_rev:
                break
        if len(self._cat_cls_cache) > 5000:   # 肥大防止
            self._cat_cls_cache.clear()
        self._cat_cls_cache[cache_key] = (is_ng, is_rev)
        return is_ng, is_rev

    def classify_catalog(self, entry, title_chars: int = -1) -> str:
        """カタログエントリのNG分類を返す: 'ng' / 'reverse_ng' / 'none'
        逆NGの判定タイトル長: <0=全文 / 0=空（判定なし）/ >0=先頭N文字。"""
        url = getattr(entry, "thread_url", "")
        if url and url in self._settings.ng_thread_urls:
            return "ng"
        is_ng, is_rev_ng = self._classify_ng_catalog_1pass(entry, title_chars)
        if is_ng and is_rev_ng:
            priority = getattr(self._settings, "ng_priority_word_idx", 0)
            return "ng" if priority == 0 else "reverse_ng"
        if is_ng:      return "ng"
        if is_rev_ng:  return "reverse_ng"
        return "none"

    def is_ng_catalog(self, entry, title_chars: int = -1) -> bool:
        return self.classify_catalog(entry, title_chars) == "ng"

    def is_reverse_ng_catalog(self, entry, title_chars: int = -1) -> bool:
        return self.classify_catalog(entry, title_chars) == "reverse_ng"


    def get_matched_reverse_ng_words_catalog(self, entry, title_chars: int = -1) -> list[dict]:
        """逆NGにマッチしたワード辞書（notify/notify_type含む）のリストを返す（カタログエントリ用）。
        判定タイトル長: <0=全文 / 0=空（マッチなし）/ >0=先頭N文字。"""
        self._ensure_flat()
        title = entry.title or ""
        if not title:
            return []
        if title_chars >= 0:
            title = title[:title_chars]
        # _flat_words にはnotify情報がないので ng_words から直接引く
        result = []
        seen_pats: set = set()
        for ng in self._settings.ng_words:
            if not ng.get("enabled", True):
                continue
            if ng.get("ng_type", "ng") != "reverse_ng":
                continue
            pat = ng.get("pattern", "").strip()
            if not pat or pat in seen_pats:
                continue
            scope_body    = ng.get("scope_body",    True)
            scope_catalog = ng.get("scope_catalog", False)
            if not scope_body and not scope_catalog:
                continue
            rxp = self._compile_one(pat)
            hit = bool(rxp.search(title)) if rxp else pat.lower() in title.lower()
            if hit:
                seen_pats.add(pat)
                result.append(ng)
        return result

    def get_matched_reverse_ng_words(self, res: "ResData") -> list[dict]:
        """逆NGにマッチしたワード辞書のリストを返す（レス用・スコープ考慮）"""
        body    = res.comment_text or ""
        name    = res.name or ""
        trip    = res.trip or ""
        subject = getattr(res, "subject", "") or ""
        mail    = res.email or ""
        id_str  = res.id_str or ""
        result = []
        seen_pats: set = set()
        for ng in self._settings.ng_words:
            if not ng.get("enabled", True):
                continue
            if ng.get("ng_type", "ng") != "reverse_ng":
                continue
            pat = ng.get("pattern", "").strip()
            if not pat or pat in seen_pats:
                continue
            has_any = any(ng.get(k) for k in
                ("scope_body", "scope_name", "scope_subject",
                 "scope_mail", "scope_id", "scope_ip"))
            targets = []
            if ng.get("scope_body", not has_any) and body: targets.append(body)
            if ng.get("scope_name", False):
                if name: targets.append(name)
                if trip: targets.append(trip)
            if ng.get("scope_subject", False) and subject: targets.append(subject)
            if ng.get("scope_mail", False)    and mail:    targets.append(mail)
            if ng.get("scope_id", False)      and id_str:  targets.append(id_str)
            if not targets:
                continue
            rxp = self._compile_one(pat)
            hit = any(bool(rxp.search(t)) if rxp else pat.lower() in t.lower()
                      for t in targets)
            if hit:
                seen_pats.add(pat)
                result.append(ng)
        return result

    # ── NG画像判定（優先順位対応・LastHit更新） ──────────────────────────────
    def classify_image(self, res: "ResData") -> str:
        """画像のNG分類: 'ng' / 'reverse_ng' / 'none'"""
        if not res.image_url:
            return "none"
        is_ng     = self._check_image(res, is_reverse=False)
        is_rev_ng = self._check_image(res, is_reverse=True)
        if is_ng and is_rev_ng:
            priority = getattr(self._settings, "ng_priority_image_idx", 0)
            # 0 = "NG画像 > 逆NG画像" (NGが優先)
            # 1 = "NG画像 < 逆NG画像" (逆NGが優先)
            return "ng" if priority == 0 else "reverse_ng"
        if is_ng:      return "ng"
        if is_rev_ng:  return "reverse_ng"
        return "none"

    def is_ng_image(self, res: "ResData") -> bool:
        return self.classify_image(res) == "ng"


    def get_ng_image_hide_mode(self, res: "ResData") -> str:
        """NG画像にマッチしたエントリの hide_mode を返す ('image' or 'res')"""
        if not res.image_url:
            return "image"
        url = res.image_url
        import hashlib
        for img_ng in self._settings.ng_images:
            if not img_ng.get("enabled", True):
                continue
            if img_ng.get("is_reverse_ng", False):
                continue
            if self._is_expired(img_ng):
                continue
            method = img_ng.get("method", "md5")
            matched = False
            if method == "type_size":
                ext = (url.rsplit(".", 1)[-1].upper() if "." in url else "")
                size, width, height = res.file_size_bytes, res.thumb_w, res.thumb_h
                ng_type = img_ng.get("image_type", "ANY").upper()
                ng_w = img_ng.get("width", 0); ng_h = img_ng.get("height", 0)
                ng_smin = img_ng.get("size_min", 0); ng_smax = img_ng.get("size_max", 0)
                if ng_type not in ("ANY", ""):
                    if not (ng_type == "JPG" and ext in ("JPG", "JPEG")):
                        if ext != ng_type:
                            continue
                if ng_w > 0 and width != ng_w: continue
                if ng_h > 0 and height != ng_h: continue
                if ng_smin > 0 and size < ng_smin: continue
                if ng_smax > 0 and size > ng_smax: continue
                matched = True
            elif method in ("md5", "file"):
                stored_md5 = img_ng.get("md5", "").lower()
                if not stored_md5:
                    continue
                known_urls = img_ng.get("known_urls", [])
                if url in known_urls:
                    matched = True
                else:
                    cached_path = self._get_cached_path(url)
                    if cached_path:
                        try:
                            with open(cached_path, "rb") as f:
                                if hashlib.md5(f.read()).hexdigest().lower() == stored_md5:
                                    matched = True
                        except OSError:
                            pass
            if matched:
                return img_ng.get("hide_mode", "image")
        return "image"

    # ── 内部: レスフィールドマッチ ────────────────────────────────────────────

    # ── 内部: 画像マッチ（LastHit更新） ──────────────────────────────────────
    def _check_image(self, res: "ResData", is_reverse: bool) -> bool:
        import hashlib, datetime
        url = res.image_url
        ext = (url.rsplit(".", 1)[-1].upper() if "." in url else "")
        size, width, height = res.file_size_bytes, res.thumb_w, res.thumb_h

        for img_ng in self._settings.ng_images:
            if not img_ng.get("enabled", True):
                continue
            if img_ng.get("is_reverse_ng", False) != is_reverse:
                continue
            if self._is_expired(img_ng):
                continue
            method = img_ng.get("method", "md5")

            matched = False
            if method == "type_size":
                ng_type = img_ng.get("image_type", "ANY").upper()
                ng_w    = img_ng.get("width",    0)
                ng_h    = img_ng.get("height",   0)
                ng_smin = img_ng.get("size_min", 0)
                ng_smax = img_ng.get("size_max", 0)
                if ng_type not in ("ANY", ""):
                    if not (ng_type == "JPG" and ext in ("JPG", "JPEG")):
                        if ext != ng_type:
                            continue
                if ng_w > 0 and width  != ng_w:  continue
                if ng_h > 0 and height != ng_h:  continue
                if ng_smin > 0 and size < ng_smin: continue
                if ng_smax > 0 and size > ng_smax: continue
                matched = True

            elif method in ("md5", "file"):
                stored_md5 = img_ng.get("md5", "").lower()
                if not stored_md5:
                    continue
                # 1) known_urls による URL直接照合（キャッシュ不要）
                known_urls = img_ng.get("known_urls", [])
                if url in known_urls:
                    matched = True
                else:
                    # 2) ディスクキャッシュが存在すればMD5で照合
                    cached_path = self._get_cached_path(url)
                    if cached_path:
                        try:
                            with open(cached_path, "rb") as f:
                                file_md5 = hashlib.md5(f.read()).hexdigest()
                            if file_md5.lower() == stored_md5:
                                matched = True
                                # URL を known_urls に追記して次回は高速照合
                                if url not in known_urls:
                                    img_ng.setdefault("known_urls", []).append(url)
                        except OSError:
                            pass

            if matched:
                # LastHit 日時を更新
                img_ng["last_hit"] = datetime.date.today().strftime("%Y/%m/%d")
                return True

        return False

    def _get_cached_path(self, url: str) -> str | None:
        from futaba2b_network import IMAGE_CACHE_DIR
        from urllib.parse import urlparse
        import os
        try:
            parsed = urlparse(url)
            p = IMAGE_CACHE_DIR / (parsed.hostname or "unknown") / parsed.path.lstrip("/")
        except Exception:
            return None
        return str(p) if os.path.exists(p) else None

    # ── 置換・芝刈り置換（フラット化キャッシュ使用） ────────────────────────
    _MOW_PAT = re.compile(
        r"(?<![a-vx-zA-VX-Z\uff41-\uff56\uff58-\uff5a\uff21-\uff36\uff38-\uff3a])"
        r"([wW\uff57\uff37]+)"
        r"(?![a-vx-zA-VX-Z\uff41-\uff56\uff58-\uff5a\uff21-\uff36\uff38-\uff3a])"
    )
    _URL_PAT = re.compile(r'https?://\S+')

    def apply_replace(self, text: str) -> str:
        """置換/芝刈り置換NGワードをテキストに適用して返す"""
        self._ensure_flat()
        if not self._flat_replaces:
            return text
        result = text
        for rxp, pat, ng_type, replace_str in self._flat_replaces:
            if ng_type == "replace":
                repl_fn = lambda m, _r=replace_str: _r
                if rxp is not None:
                    try:
                        result = rxp.sub(repl_fn, result)
                    except re.error:
                        pass
                else:
                    try:
                        result = re.sub(pat, repl_fn, result, flags=re.IGNORECASE)
                    except re.error:
                        pass
            elif ng_type == "mow_replace":
                # URLをプレースホルダに退避してからw置換し復元する
                urls: list = []
                def _stash(m, _u=urls):
                    _u.append(m.group(0))
                    return f"\x00URL{len(_u)-1}\x00"
                tmp = self._URL_PAT.sub(_stash, result)
                tmp = self._MOW_PAT.sub(lambda m, _r=replace_str or ".": _r, tmp)
                for i, u in enumerate(urls):
                    tmp = tmp.replace(f"\x00URL{i}\x00", u)
                result = tmp
        return result
