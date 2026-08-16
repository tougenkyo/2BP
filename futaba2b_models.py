#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""futaba2b_models.py  ─  データモデル"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


def board_display_name(board_name: str, board_url: str) -> str:
    """板名の表示用文字列。二次元裏はサーバーが may / img 等に分かれていて
    板名だけでは区別が付かないため、サブドメインを括弧書きで添える。
      例: 二次元裏(may) / 二次元裏(img)
    それ以外の板・URL不明時は板名をそのまま返す。"""
    if board_name == "二次元裏" and board_url:
        m = re.search(r'//(\w+)\.2chan\.net/', board_url)
        if m:
            return f"二次元裏({m.group(1)})"
    return board_name


@dataclass
class BoardCategory:
    name: str
    boards: list["BoardInfo"] = field(default_factory=list)


@dataclass
class BoardInfo:
    name: str
    url: str
    group: str = "未分類"   # ★ 追加: 板グループ名
    # ── 板ページから取得する動的情報 ──────────────────────────────────────────
    viewers:          int  = 0    # 現在の視聴者数
    max_saved:        int  = 0    # 最大保存スレッド数
    current_saved:    int  = 0    # 現在の保存スレッド数（カタログエントリ数）
    board_desc:       str  = ""   # 板の説明
    board_rules_text: str = ""   # スレHTMLから取得した書き込みルール（プレーンテキスト）
    board_rules_html: str = ""   # 同上HTML版
    has_name_field:   bool = True  # 名前欄が存在する板かどうか（img板等はFalse）
    max_file_bytes:   int  = 0    # 添付ファイルサイズ上限（MAX_FILE_SIZE, バイト。0=不明）
    # 返信フォームに upfile 欄があるか＝レスに画像を添付できる板か。
    # img板の返信フォームには upfile が無く、送っても捨てられる（本文が空だと
    # 「何か書いてください」で弾かれる）。未確認のうちは True 扱い。
    can_upload_res:   bool = True

    @property
    def base_url(self) -> str:
        """板のベース URL (末尾 '/' 付き、https 正規化済み)"""
        # url が futaba.htm を含む場合とベース URL の場合を両方吸収する
        base = self.url.rsplit("/futaba.htm", 1)[0].rstrip("/") + "/"
        # http → https に統一
        if base.startswith("http://"):
            base = "https://" + base[7:]
        return base

    @property
    def catalog_url(self) -> str:
        return self.base_url + "futaba.php?mode=cat"

    @property
    def post_url(self) -> str:
        return self.base_url + "futaba.php?guid=on"


@dataclass
class ResData:
    no: int
    name: str
    trip: str
    email: str
    datetime_str: str
    subject: str
    comment_html: str
    comment_text: str
    image_url: str
    thumb_url: str
    image_name: str
    image_size: int
    thumb_w: int
    thumb_h: int
    sodane: int
    is_op: bool = False
    csb: str = "無念"            # 投稿感情 (csb span の内容)
    expiry_str: str = ""         # 消えます表示テキスト
    is_deleted: bool = False
    res_idx: int = 0
    is_new: bool = False         # 前回閲覧後の新着レスか
    file_size_bytes: int = 0     # 添付ファイルサイズ (bytes)
    id_str: str = ""             # 投稿者ID (例: fNsjIPH6)
    ip_str: str = ""             # 投稿者IP (例: 1.2.3.4) ※IP表示板のみ
    deleted_preserved: bool = False   # 削除されたが削除前の本文を保持しているか
    deleted_reason: str = ""          # 削除理由 (例: 書き込みをした人によって削除されました)
    deleted_orig_image_name: str = "" # 削除前に添付されていた画像のファイル名 (表示は名のみ)


@dataclass
class ThreadData:
    no: int
    board: BoardInfo
    title: str
    url: str = ""
    expiry: str = ""
    is_expiring: bool = False       # contdispを赤字にするJSが存在する = 落ちかけ
    is_full: bool = False           # 上限1000レスに達した
    error: str = ""
    deleted_count: int = 0
    is_cached: bool = False       # キャッシュから表示中
    received_count: int = 0       # 今回の受信件数
    last_updated: str = ""        # 最終更新日時文字列
    die_time: str = ""            # スレ落ち予定時刻 (JSON APIの "die" フィールド)
    res_list: list[ResData] = field(default_factory=list)

    @property
    def thread_url(self) -> str:
        return self.board.base_url + f"res/{self.no}.htm"


@dataclass
class CatalogEntry:
    no: int
    thumb_url: str
    res_count: int
    thread_url: str
    title: str = ""
    email: str = ""              # OPのメール欄（id表示/ip表示 など）
    op_id: str = ""              # OPのID文字列（mode=json の id フィールド。空=ID無し）
    is_red: bool = False         # 赤字スレ判定（サーバー側）
    is_quasi_red: bool = False   # 仮赤字（残り10%以下）
    is_quarantine: bool = False  # 隔離スレ（mode=cat にあって mode=json に無い）
    # 履歴表示（並び替え=履歴）用のフラグ
    is_dead:   bool = False      # 既に落ちたスレ（mode=json に存在しない）
    has_cache: bool = False      # スレHTMLキャッシュが残っている
    has_posted: bool = False     # 自分が書き込んだことがある（履歴の posted 由来）
    # オンマウス表示用のOP本文（mode=json の com を素テキスト化したもの）。
    # title はカタログHTML由来で板の「文字数」までしか無いため、ホバー表示だけ
    # こちらを使う。NG判定・カタログのセル文字は従来どおり title を使う。
    op_comment: str = ""
    # NG画像の照合に使うスレ画URL。履歴表示ではサムネがローカルキャッシュの
    # file:// になり、ふたばのファイル名（＝画像の番号）が失われて照合できない
    # ため、元のふたば上のサムネURLをここに残す。空なら thumb_url を使う。
    ng_thumb_url: str = ""
    board: Optional[BoardInfo] = None


# ── 板内検索（ふたばの検索モード mode=search）───────────────────────────────

@dataclass
class SearchHit:
    """検索モードが返す1件。スレではなくレス単位で返ってくる。
    resto はそのレスが属するスレの番号。0 のときはスレ本文(OP)自身。"""
    no:            int
    resto:         int
    datetime_str:  str = ""
    name:          str = ""
    email:         str = ""
    subject:       str = ""
    comment_html:  str = ""
    comment_text:  str = ""
    thumb_url:     str = ""
    image_url:     str = ""
    image_name:    str = ""
    image_size:    int = 0
    thumb_w:       int = 0
    thumb_h:       int = 0

    @property
    def thread_no(self) -> int:
        return self.resto or self.no

    @property
    def is_op(self) -> bool:
        return self.resto == 0


@dataclass
class SearchResult:
    """板内検索の結果一式。

    ふたばの検索は板の全部を見てくれるわけではなく、途中で走査をやめる。
    よくある語・短い語ほど早く止まり、古い側だけが返る。どこまで見たのかは
    レス番号で分かるので、範囲もそのまま持って表示に使う。"""
    keyword:    str
    board_url:  str  = ""
    hits:       list = field(default_factory=list)
    error:      str  = ""
    server_now: str  = ""    # 応答ヘッダ Date を板の時刻(JST)にしたもの "HH:MM:SS"
    source:     str  = "futaba"   # "futaba"=板の検索モード / "cache"=手元のキャッシュ
    scanned:    int  = 0     # cache のとき、走査したスレ数
    capped:     bool = False  # cache のとき、上限で打ち切ったか

    @property
    def count(self) -> int:
        return len(self.hits)

    @property
    def first_no(self) -> int:
        return min((h.no for h in self.hits), default=0)

    @property
    def last_no(self) -> int:
        return max((h.no for h in self.hits), default=0)

    @property
    def thread_count(self) -> int:
        return len({h.thread_no for h in self.hits})

    def newest_time(self) -> str:
        """一番新しいヒットの時刻 "HH:MM:SS"（取れなければ空）"""
        if not self.hits:
            return ""
        newest = max(self.hits, key=lambda h: h.no)
        m = re.search(r'(\d{1,2}:\d{2}:\d{2})', newest.datetime_str or "")
        return m.group(1) if m else ""

    def stale_minutes(self) -> int:
        """一番新しいヒットが、サーバーの現在時刻から何分前か。
        判定材料が無いときは -1。日付をまたぐケースは 0 に丸める。"""
        hi, now = self.newest_time(), self.server_now
        if not (hi and now):
            return -1

        def _sec(t: str) -> int:
            h, m, s = (int(x) for x in t.split(":"))
            return h * 3600 + m * 60 + s
        return max(0, (_sec(now) - _sec(hi)) // 60)


# ── 自動更新 ─────────────────────────────────────────────────────────────────

# 段階的更新間隔のデフォルト定義
# pct: 最大保存件数に対する残り件数の割合（以下のとき適用）
# interval_sec: 更新間隔（秒）
AR_ADAPTIVE_DEFAULTS: list[dict] = [
    {"enabled": True,  "pct": 100, "interval_sec": 3600},  # 常時有効・UI非表示
    {"enabled": False, "pct": 50,  "interval_sec": 1800},
    {"enabled": False, "pct": 25,  "interval_sec": 600},
    {"enabled": False, "pct": 10,  "interval_sec": 120},
    {"enabled": False, "pct": 5,   "interval_sec": 60},
    {"enabled": False, "pct": 1,   "interval_sec": 30},
]


@dataclass
class AutoRefreshEntry:
    """自動更新エントリ"""
    no:           int
    url:          str
    title:        str
    board_name:   str
    interval_sec: int  = 60   # 現在の更新間隔（秒）※adaptive で自動更新
    stop_hour:    int  = -1   # 更新停止 時 (-1=なし)
    stop_min:     int  = 0    # 更新停止 分
    stop_after_min: int = 0   # N分後に停止 (0=なし)
    scroll_to_new: bool = False  # 初期値: チェックなし
    bouyomi:      bool = False
    enabled:      bool = True
    last_update_str: str = "--"
    max_saved:    int  = 0    # 板の最大保存件数（段階更新間隔計算用）
    max_res_no:   int  = 0    # 最後にfetchした時点での最新レスNo（段階更新計算用）
    is_catalog:   bool = False   # カタログの自動更新エントリかどうか
    board_url:    str  = ""      # カタログ用板URL (is_catalog=True時)
    # 段階的更新間隔ルール（各行: enabled/pct/interval_min）
    adaptive_intervals: list = field(
        default_factory=lambda: [dict(r) for r in AR_ADAPTIVE_DEFAULTS]
    )


# ── マウスジェスチャー ───────────────────────────────────────────────────────
# 右ボタンドラッグの軌跡（↑↓←→の並び）に割り当てるアクション。
# action_id は既存のキーボードショートカット定義と共通。表示名も既存表記に合わせる。
MOUSE_GESTURE_ACTIONS: list[tuple[str, str]] = [
    ("close_tab",       "このビューを閉じる"),
    ("close_all_tabs",  "全てのビューを閉じる"),
    ("reopen_tab",      "閉じたタブを開き直す"),
    ("prev_tab",        "左のタブへ移動"),
    ("next_tab",        "右のタブへ移動"),
    ("refresh_current", "このビューの更新"),
    ("refresh_board",   "この板の更新"),
    ("refresh_all_tabs", "この板の全タブを更新"),
    ("catalog",         "カタログ表示"),
    # 2BPには futaba.htm のスレ一覧ページを表示するビューが無いため、これは
    # 「カタログ表示」と同じ動作になる。旧2Bからの割り当てをそのまま使えるよう
    # 項目自体は残し、同じものだと分かるラベルにしている。
    ("board_top",       "掲示板を表示する（カタログ表示と同じ）"),
    ("reply",           "返信ダイアログを開く"),
    ("new_thread",      "スレッドを立てる"),
    ("find_in_view",    "ページ内検索"),
    ("toggle_pin",      "ピン留め"),
    ("save_last",       "最後に保存した形式で保存"),
    ("save_mht",        "MHT形式で保存"),
    ("save_html",       "HTML形式で保存"),
    ("save_zip",        "ZIP形式で保存"),
    ("scroll_top",      "ページ先頭へ移動"),
    ("scroll_bottom",   "ページ末尾へ移動"),
    ("open_browser",    "外部ブラウザにアドレスを送る"),
]

# 既定の割り当て（旧2B準拠）。キーは方向列。
MOUSE_GESTURE_DEFAULTS: dict[str, str] = {
    "↓→": "close_tab",
    "↓":  "refresh_current",
    "↓↑": "refresh_board",
    "←→": "catalog",
    "←":  "scroll_top",
    "→":  "scroll_bottom",
    "↑":  "save_last",
    "→←": "board_top",
}
