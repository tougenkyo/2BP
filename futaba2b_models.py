#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""futaba2b_models.py  ─  データモデル"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


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
    board: Optional[BoardInfo] = None


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
