#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""futaba2b_network.py  ─  HTTP通信・HTML解析・画像キャッシュ"""

from __future__ import annotations
import datetime, gc, hashlib, json, re, sys, threading, time, traceback, urllib.parse, zlib
try:
    import psutil as _psutil
except ImportError:
    _psutil = None
    print('[MEM] psutil not installed. Run: pip install psutil')

try:
    import lxml  # noqa: F401
    _BS4_PARSER = "lxml"
except ImportError:
    _BS4_PARSER = "html.parser"

# BeautifulSoup の再帰スタックをデフォルト(1000)から拡張
if sys.getrecursionlimit() < 5000:
    sys.setrecursionlimit(5000)

# 大容量HTML（200KB超）のパースを同時1スレッドに制限するセマフォ
# 448KB スレを複数並行パースするとメモリが 600MB 超になりOSにkillされるため
_large_parse_sem = threading.Semaphore(1)


def _update_board_max_no(settings, board_base_url: str, no: int, source: str) -> None:
    """板別 global_max_no_by_board を更新（板をまたいだ汚染を防止）
    通常は単調増加。ただし保存値との差が著しく大きい場合は異常値とみなしてスキップする。
    """
    d = settings.global_max_no_by_board
    prev = d.get(board_base_url, 0)
    _RESET_THRESHOLD = 1_000_000  # この差を超えたら異常値とみなす
    if no > prev:
        if prev > 0 and (no - prev) > _RESET_THRESHOLD:
            # 急増しすぎ（広告URLや別ページのNoが混入した可能性）→ スキップ
            print(f'[o] {source}: 異常増加スキップ {prev} → {no} (差={no-prev})')
            return
        d[board_base_url] = no
        print(f'[o] {source}: {prev} → {no} (+{no-prev})')
    elif prev > no and (prev - no) > _RESET_THRESHOLD:
        # 保存値が異常に大きい（レスNoなどが混入した可能性）→ 強制リセット
        d[board_base_url] = no
        print(f'[o] {source}: 異常値リセット {prev} → {no} (差={prev-no})')
    else:
        print(f'[o] {source}: {no} <= {prev} (変化なし)')
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

# ── レス毎パース用の事前コンパイル済みパターン（_parse_res_node / _parse_op）──
_THUMB_SRC_RE = re.compile(r"/thumb/")
_SRC_HREF_RE  = re.compile(r"/src/")
_RES_NO_RE    = re.compile(r"No\.(\d+)")
_TRIP_RE      = re.compile(r"[!◆★☆].+")
_SODANE_RE    = re.compile(r"そうだねx(\d+)")
_FSIZE_RE     = re.compile(r"[\-\(](\d+)\s*B")
_RES_ID_RE    = re.compile(r"\bID:(\S+)")
_RES_IP_RE    = re.compile(r"\bIP:(\S+)")

from futaba2b_const import UA, BBSMENU_URL, FUTABA_ERROR_PATTERNS, SEC_CH_UA, SEC_CH_UA_MOBILE, SEC_CH_UA_PLATFORM
from futaba2b_models import (
    BoardCategory, BoardInfo, CatalogEntry, ResData, ThreadData,
)

BBSMENU_CACHE   = Path("data/log/bbsmenu_cache.html")
THREAD_CACHE_DIR = Path("data/log")   # スレキャッシュ保存先
IMAGE_CACHE_DIR  = Path("data/img")   # 画像キャッシュ保存先
import os as _os
VIDEO_CACHE_DIR  = Path(              # 動画キャッシュ保存先（app_qt と同一パス）
    _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~"))
) / "2BP" / "video_cache"
IMAGE_CACHE_MAX  = 120                # メモリキャッシュ最大件数（上限・副次）
IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024  # メモリキャッシュ最大バイト数（主・48MB）
COOKIES_FILE   = Path("futaba2b_cookies.json")  # セッションクッキー永続化


def cleanup_image_cache(max_days: int = 7) -> tuple[int, int]:
    """画像キャッシュから max_days 日より古いファイルを削除する。
    max_days==0 の場合は何もしない。
    Returns: (削除ファイル数, 削除バイト数)
    """
    if max_days <= 0 or not IMAGE_CACHE_DIR.exists():
        return 0, 0
    import time
    cutoff = time.time() - max_days * 86400
    deleted_count = 0
    deleted_bytes = 0
    for f in IMAGE_CACHE_DIR.rglob("*"):
        if not f.is_file():
            continue
        try:
            st = f.stat()
            if st.st_mtime < cutoff:
                sz = st.st_size
                f.unlink()
                deleted_count += 1
                deleted_bytes += sz
        except OSError:
            pass
    # 空ディレクトリを削除
    for d in sorted(IMAGE_CACHE_DIR.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()  # 空でなければ例外
            except OSError:
                pass
    return deleted_count, deleted_bytes


def cleanup_cache_dir(cache_dir, max_days: int = 0,
                      max_bytes: int = 0) -> tuple[int, int]:
    """キャッシュディレクトリをクリーンアップする汎用関数。

    max_days  > 0: max_days日より古いファイルを削除
    max_bytes > 0: 日数削除後も合計がmax_bytesを超える場合、
                   古い順（mtime昇順=LRU）に超過分を削除
    両方0なら何もしない。
    Returns: (削除ファイル数, 削除バイト数)
    """
    from pathlib import Path
    cache_dir = Path(cache_dir)
    if (max_days <= 0 and max_bytes <= 0) or not cache_dir.exists():
        return 0, 0
    import time
    deleted_count = 0
    deleted_bytes = 0
    files: list[tuple[float, int, Path]] = []   # (mtime, size, path)
    for f in cache_dir.rglob("*"):
        if not f.is_file():
            continue
        try:
            st = f.stat()
            files.append((st.st_mtime, st.st_size, f))
        except OSError:
            pass

    # ① 日数超過分を削除
    if max_days > 0:
        cutoff = time.time() - max_days * 86400
        remain = []
        for mtime, size, f in files:
            if mtime < cutoff:
                try:
                    f.unlink()
                    deleted_count += 1
                    deleted_bytes += size
                except OSError:
                    remain.append((mtime, size, f))
            else:
                remain.append((mtime, size, f))
        files = remain

    # ② サイズ上限超過分を古い順に削除（LRU）
    if max_bytes > 0:
        total = sum(sz for _, sz, _ in files)
        if total > max_bytes:
            files.sort(key=lambda t: t[0])   # mtime昇順
            for mtime, size, f in files:
                if total <= max_bytes:
                    break
                try:
                    f.unlink()
                    total -= size
                    deleted_count += 1
                    deleted_bytes += size
                except OSError:
                    pass

    # ③ 空ディレクトリを削除
    for d in sorted(cache_dir.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()  # 空でなければ例外
            except OSError:
                pass
    return deleted_count, deleted_bytes


def get_dir_size(cache_dir) -> tuple[int, int]:
    """ディレクトリの合計サイズを返す汎用関数。Returns: (ファイル数, バイト数)"""
    from pathlib import Path
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return 0, 0
    total_files = 0
    total_bytes = 0
    for f in cache_dir.rglob("*"):
        if f.is_file():
            try:
                total_bytes += f.stat().st_size
                total_files += 1
            except OSError:
                pass
    return total_files, total_bytes


def get_cache_size() -> tuple[int, int]:
    """画像キャッシュの合計サイズを返す。Returns: (ファイル数, バイト数)"""
    if not IMAGE_CACHE_DIR.exists():
        return 0, 0
    total_files = 0
    total_bytes = 0
    for f in IMAGE_CACHE_DIR.rglob("*"):
        if f.is_file():
            try:
                total_bytes += f.stat().st_size
                total_files += 1
            except OSError:
                pass
    return total_files, total_bytes


class FutabaFetcher:
    """
    ふたばへのHTTP通信とHTML解析を一手に担う。
    表示はブラウザが行うため、このクラスは主に:
      - 板一覧 (bbsmenu) の取得・パース
      - 投稿 (POST)
      - スレッド履歴用のメタ情報取得
    に使用する。
    """

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self.session = requests.Session()
        self._img_cache: dict[str, bytes] = {}  # url→bytes メモリキャッシュ
        self._img_cache_bytes: int = 0          # _img_cache の合計バイト数（上限管理用）
        self._prefetch_seen: set[str] = set()   # 先読み済み/投入済みURL（重複投入防止）
        self._prefetch_pool = None              # 本画像先読み用の小プール（遅延生成）
        self._prefetch_cancel: dict = {}        # group(スレURL)→Event（タブ閉じ時の一括中断）
        # CURL と同一のヘッダーセット (Edge 148)
        self.session.headers.update({
            "User-Agent":          UA,
            "sec-ch-ua":           SEC_CH_UA,
            "sec-ch-ua-mobile":    SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform":  SEC_CH_UA_PLATFORM,
            "Accept":              "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language":     "ja,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
            "Accept-Encoding":     "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
        })
        self.timeout = 60   # タイムアウト固定60秒
        # 前回セッションのクッキーを復元 (ブラウザと同様の信頼関係を維持)
        self._load_cookies()

    def _get_cookie(self, name: str, default: str = "") -> str:
        """
        同名クッキーが複数ドメインに存在しても安全に取得する。
        requests の session.cookies.get() は CookieConflictError を出すため
        このメソッドを代わりに使用する。
        """
        return next(
            (c.value for c in self.session.cookies if c.name == name),
            default,
        )


    def get_cxyl(self) -> str:
        """cxyl クッキーを返す (例: \"14x6x6x0x0\")\n        cols x rows x chars x text_pos x img_size"""
        return self._get_cookie("cxyl", "14x6x6x0x0")

    # cxyl カラム数テーブル: cx インデックス → 実際の列数
    _CX_VALS = [4, 6, 8, 10, 14, 18, 22, 28, 36]

    def _clear_cxyl_cookies(self) -> None:
        """セッション内の cxyl cookie を全ドメイン/パスから除去する。
        大取得時のドメイン競合（.2chan.net と板ドメインの併存）を解消するため。"""
        jar = self.session.cookies
        for c in list(jar):
            if c.name == "cxyl":
                try:
                    jar.clear(c.domain, c.path, c.name)
                except Exception:
                    pass

    def set_cxyl_cookie(self, cxyl: str, board_domain: str = "") -> None:
        """cxyl 設定をセッションクッキーに上書きする。
        board_domain を指定すると板固有ドメイン（例: may.2chan.net）のみ更新。
        未指定の場合は .2chan.net 全体に設定（初期化用）。"""
        if board_domain:
            # 板固有ドメイン（例: may.2chan.net）と先頭ドットあり両方に設定
            for d in (board_domain, "." + board_domain.lstrip(".")):
                self.session.cookies.set("cxyl", cxyl, domain=d)
        else:
            for domain in (".2chan.net", "2chan.net"):
                self.session.cookies.set("cxyl", cxyl, domain=domain)

    def post_catset(self, board: "BoardInfo", settings) -> bool:
        """
        カタログ設定を futaba.php?mode=catset に POST してサーバー側 cxyl を更新する。
        サーバーが返す Set-Cookie（値の正規化・丸め）は無視し、
        ユーザー設定値を強制的にセッションクッキーに上書きする。
        """
        try:
            cx = getattr(settings, "cat_cols",  14)
            cy = getattr(settings, "cat_rows",   6)
            cl = getattr(settings, "cat_chars",  4)
            _pos_map = {"下": 0, "右": 1, "0:下": 0, "1:右": 1, "左": 0, "上": 0}
            cm = _pos_map.get(getattr(settings, "cat_text_pos", "下"), 0)
            _img_map = {"小": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "大": 6,
                        "0:小": 0, "1:中": 2, "2:大": 6}
            ci = _img_map.get(getattr(settings, "cat_img_size_str", "小"), 0)

            # 板固有ドメイン（例: may.2chan.net）
            import urllib.parse as _up
            _bd = _up.urlparse(board.base_url).hostname or ""

            catset_url = board.base_url + "futaba.php?mode=catset"
            data = {"mode": "catset", "cx": str(cx), "cy": str(cy),
                    "cl": str(cl), "cm": str(cm), "ci": str(ci), "vh": "on"}
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": catset_url,
            }
            print(f"[Catset] POST {_bd} → {data}")
            r = self.session.post(catset_url, data=data,
                                  headers=headers, timeout=self.timeout)
            print(f"[Catset] response status={r.status_code}  "
                  f"Set-Cookie cxyl={r.cookies.get('cxyl', '(none)')}")
            if r.ok:
                forced_cxyl = f"{cx}x{cy}x{cl}x{cm}x{ci}"
                self.set_cxyl_cookie(forced_cxyl, board_domain=_bd)
                print(f"[Catset] 設定反映 domain={_bd} cxyl={forced_cxyl}")
                return True
            else:
                print(f"[Catset] POST 失敗: status={r.status_code}")
        except Exception as e:
            print(f"[Catset] エラー: {e}")
        return False

    def post_catset_bs(self, board, board_settings) -> bool:
        """BoardSettings を受け取る post_catset のエイリアス"""
        return self.post_catset(board, board_settings)

    def _load_cookies(self) -> None:
        """保存済みクッキーをセッションに復元する"""
        if not COOKIES_FILE.exists():
            return
        try:
            with open(COOKIES_FILE, encoding="utf-8") as f:
                cookies = json.load(f)
            for c in cookies:
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                )
            print(f"[Cookies] {len(cookies)} 件のクッキーを復元しました")
        except Exception as e:
            print(f"[Cookies] 読み込みエラー: {e}")

    def save_cookies(self) -> None:
        """
        現在のセッションクッキーをファイルに保存する。
        投稿成功後・アプリ終了時に呼ぶことでブラウザと同様の
        セッション継続性を持たせる。
        """
        try:
            cookies = [
                {
                    "name":   c.name,
                    "value":  c.value,
                    "domain": c.domain,
                    "path":   c.path,
                }
                for c in self.session.cookies
                if c.value  # 空値のクッキーは除外
            ]
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Cookies] 保存エラー: {e}")

    # ── スレキャッシュ ──────────────────────────────────────────────────────

    def _cache_path(self, url: str) -> Path:
        """URL → キャッシュファイルパス (data/log/host/b/res/NNNN.htm)"""
        p = urllib.parse.urlparse(url)
        # ホスト名を含めたパス: may.2chan.net/b/res/NNNN.htm
        return THREAD_CACHE_DIR / p.hostname / p.path.lstrip("/")

    def _save_thread_cache(self, url: str, html: str) -> None:
        try:
            p = self._cache_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html, encoding="utf-8")
        except Exception as e:
            print(f"[Cache] 保存エラー: {e}")

    def _load_thread_cache(self, url: str) -> Optional[str]:
        try:
            p = self._cache_path(url)
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[Cache] 読み込みエラー: {e}")
        return None

    # ── diffサイドカー（JSON diff API由来レスの永続化） ─────────────────────
    # HTMLキャッシュは最後のフルGET時点の内容しか持たないため、
    # diff APIで追記されたレスを別ファイルに保存し、キャッシュ
    # フォールバック時（スレ落ち後の再表示等）にマージして末尾欠落を防ぐ。

    def _diff_sidecar_path(self, url: str) -> Path:
        p = self._cache_path(url)
        return p.with_name(p.name + ".diff.json")

    def append_diff_to_cache(self, url: str, res_list: list) -> None:
        """diff APIで取得した新着レスをサイドカーに追記保存する。"""
        if not res_list:
            return
        import json, os, dataclasses
        try:
            p = self._diff_sidecar_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            for r in res_list:
                data[str(r.no)] = dataclasses.asdict(r)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            print(f"[Cache] diffサイドカー保存エラー: {e}")

    def _clear_diff_sidecar(self, url: str) -> None:
        try:
            self._diff_sidecar_path(url).unlink(missing_ok=True)
        except OSError:
            pass

    def _merge_diff_sidecar(self, url: str, thread) -> None:
        """キャッシュフォールバック表示時にサイドカーのレスをマージする。"""
        import json, dataclasses as _dc
        try:
            p = self._diff_sidecar_path(url)
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            from futaba2b_models import ResData
            fields = {f.name for f in _dc.fields(ResData)}
            existing = {r.no for r in thread.res_list}
            added = []
            for k, d in data.items():
                try:
                    if int(k) in existing:
                        continue
                    added.append(ResData(**{kk: vv for kk, vv in d.items()
                                            if kk in fields}))
                except Exception:
                    continue
            added.sort(key=lambda r: r.no)
            for r in added:
                r.is_new = False
                r.res_idx = len(thread.res_list)
                thread.res_list.append(r)
            if added:
                thread.received_count = len(thread.res_list)
                print(f"[Cache] diffサイドカーから {len(added)}件を補完")
        except Exception as e:
            print(f"[Cache] diffサイドカー読込エラー: {e}")

    # ── 板一覧 ────────────────────────────────────────────────────────────────

    def fetch_board_menu(self) -> list[BoardCategory]:
        """bbsmenu_cache.html があればそれを使う（起動ごとの自動取得なし）。
        キャッシュがない場合のみネットから取得して保存する。"""
        if BBSMENU_CACHE.exists():
            html_text = self._load_bbsmenu_fallback()
        else:
            html_text = self._get_html(BBSMENU_URL)
            if html_text:
                try:
                    BBSMENU_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    BBSMENU_CACHE.write_text(html_text, encoding="utf-8")
                except Exception:
                    pass
            else:
                html_text = self._load_bbsmenu_fallback()
        return self._parse_bbsmenu(html_text) if html_text else []

    def _load_bbsmenu_fallback(self) -> Optional[str]:
        candidates = [
            BBSMENU_CACHE,
            Path("bbsmenu.html"),
            Path(__file__).parent / "bbsmenu.html",
        ]
        for path in candidates:
            if path.exists():
                for enc in ("utf-8", "cp932"):
                    try:
                        return path.read_text(encoding=enc, errors="replace")
                    except Exception:
                        pass
        return None

    def _parse_bbsmenu(self, html_text: str) -> list[BoardCategory]:
        """
        bbsmenu.html のパース。
        板リンクは <body> 直下ではなく <font size="2"> の中にある。
        """
        soup = BeautifulSoup(html_text, "html.parser")

        font_tag = None
        best = 0
        for f in soup.find_all("font"):
            cnt = len(f.find_all("a"))
            if cnt > best:
                best, font_tag = cnt, f

        container = font_tag or soup.find("body")
        if not container:
            return []

        cats: list[BoardCategory] = []
        cur = BoardCategory(name="ふたば・ちゃんねる")

        for elem in container.children:
            tag = getattr(elem, "name", None)
            if tag == "b":
                if cur.boards:
                    cats.append(cur)
                cur = BoardCategory(name=elem.get_text(strip=True))
            elif tag == "a":
                href = elem.get("href", "")
                name = elem.get_text(strip=True)
                if "futaba.htm" in href and name:
                    if not href.startswith("http"):
                        href = urllib.parse.urljoin(BBSMENU_URL, href)
                    cur.boards.append(BoardInfo(name=name, url=href))

        if cur.boards:
            cats.append(cur)
        return cats

    # ── スレッドメタ取得 (タイトル・レス数など) ──────────────────────────────


    # ── 投稿 ─────────────────────────────────────────────────────────────────

    # ── Phase 2: 投稿完全対応 ──────────────────────────────────────────────────

    def fetch_post_form(self, board: BoardInfo, thread_no: int = 0) -> dict:
        """
        投稿フォームの hidden フィールドを取得する。

        フィールド取得元 (実HTMLで確認済み):
          hash / ptua / chrenc / baseform  → サーバーが HTML に埋め込む
          pthb / pthc / scsz              → 空欄。JavaScript が現在値をセット
          js                              → HTML では "off"。JS が "on" に変更

        手順: GET → HTML パース → JS フィールドを補完
        """
        # GET でページを取得 → ptmt 等の認証クッキーをセッションに取得
        page_url = (board.base_url + f"res/{thread_no}.htm"
                    if thread_no else board.url)
        html = self._get_html(page_url, referer=board.url)

        fields: dict = {}

        if html:
            soup = BeautifulSoup(html, "html.parser")
            # 投稿フォームを特定: name="hash" を持つ hidden input があるフォーム
            form = next(
                (f for f in soup.find_all("form")
                 if f.find("input", attrs={"name": "hash"})),
                None,
            )
            if form:
                for inp in form.find_all("input", type="hidden"):
                    n = inp.get("name", "")
                    v = inp.get("value") or ""
                    if n:
                        fields[n] = v
            else:
                print("[PostForm] 投稿フォームが見つかりませんでした")

        # JS が空フィールドを埋める処理を Python で代替
        ts_ms = str(int(time.time() * 1000))

        # posttime クッキー:
        #   サーバーが GET レスポンスの Set-Cookie で送ってくる。
        #   pthb / pthc はこの値と一致させる必要がある。
        #   セッションにすでに存在する場合は上書きしない。
        # CookieConflictError 対策:
        # session.cookies.get() は同名クッキーが複数ドメインにある場合に例外を出すため
        # list() で走査して最初に見つかった値を使う
        posttime = next(
            (c.value for c in self.session.cookies if c.name == "posttime"),
            "",
        )
        if not posttime:
            # サーバーが Set-Cookie しなかった場合のみ生成
            posttime = ts_ms
            try:
                domain = urllib.parse.urlparse(page_url).hostname or ""
                self.session.cookies.set("posttime", posttime, domain=domain)
            except Exception:
                pass

        fields["pthb"] = posttime
        fields["pthc"] = posttime
        fields.setdefault("pthd", "")
        fields["scsz"] = "1920x1080x24"
        fields["js"]   = "on"

        return fields

    def _build_post_headers(self, board: BoardInfo, thread_no: int = 0) -> dict:
        """投稿用 HTTP ヘッダーを構築する"""
        referer = (board.base_url + f"res/{thread_no}.htm"
                   if thread_no else board.url)
        return {
            "Referer":         referer,
            "Origin":          "https://" + urllib.parse.urlparse(board.url).hostname,
            "Accept":          "*/*",
            "sec-fetch-site":  "same-origin",
            "sec-fetch-mode":  "cors",
            "sec-fetch-dest":  "empty",
            "Accept-Language": "ja,en;q=0.9",
        }

    def post_res(
        self,
        board: BoardInfo,
        resto: int,
        name: str,
        email: str,
        subject: str,
        comment: str,
        image_path: str = "",
        delete_key: str = "",
    ) -> tuple[bool, str, int]:
        """
        レス / スレ立て投稿。

        fetch_post_form() でサーバー発行の hash/ptua 等を取得してから
        multipart/form-data で送信する。
        """
        # ── hidden フィールドを取得 (ptmt クッキーも同時にセット) ──
        hidden = self.fetch_post_form(board, thread_no=resto)

        # ── POST データを組み立て ──
        # サーバー由来の hidden フィールド全体を展開し、
        # ユーザー入力値で上書き (順番は CURL に合わせる)
        data: dict = {
            **hidden,
            # responsemode=ajax を送ることでサーバーが JSON を返す
            # これがないと HTML が返り、正常応答かエラーかを判断できない
            "responsemode": "ajax",
            "name":  name,
            "email": email,
            "sub":   subject,
            "com":   comment,
        }
        if resto:
            data["resto"] = str(resto)
        if delete_key:
            data["pwd"] = delete_key

        headers = self._build_post_headers(board, thread_no=resto)

        try:
            # ふたばは text-only 投稿でも multipart/form-data が必要
            # (CURL 解析で確認。application/x-www-form-urlencoded では受理されない)
            if image_path and Path(image_path).exists():
                mime = self._guess_mime(image_path)
                with open(image_path, "rb") as fp:
                    files = {"upfile": (Path(image_path).name, fp, mime)}
                    resp = self.session.post(
                        board.post_url, data=data, files=files,
                        headers=headers, timeout=60,
                    )
            else:
                # ファイルなしでも multipart を強制 (空の upfile フィールドを付加)
                files = {"upfile": ("", b"", "application/octet-stream")}
                resp = self.session.post(
                    board.post_url, data=data, files=files,
                    headers=headers, timeout=60,
                )

            # ── レスポンス解析 ──
            # responsemode=ajax を送った場合はサーバーが JSON を返す:
            #   成功: {"status":"ok","jumpto":NNN,"thisno":NNN,...}
            #   失敗: {"status":"error","message":"..."} 等
            body = resp.content.decode("cp932", errors="replace").strip()

            if body.startswith("{"):
                # JSON レスポンス
                try:
                    result = json.loads(body)
                    status = result.get("status", "")
                    if status == "ok":
                        self.save_cookies()
                        self.save_cookies()
                        new_no = int(result.get("thisno") or result.get("jumpto") or 0)
                        return True, "", new_no
                    else:
                        # エラーメッセージを取得 (複数キーを試みる)
                        msg = (result.get("message")
                               or result.get("error")
                               or result.get("reason")
                               or str(result))
                        return False, msg, 0
                except json.JSONDecodeError:
                    pass  # JSON 解析失敗 → 以下の HTML 判定へ

            # 想定外レスポンス（JSON以外）の診断ログ。JSON が返らない環境で
            # 投稿が弾かれる真因（サーバーの実応答）を追えるようにする。
            print(f"[NET] post_res non-JSON resp  len={len(body)}  "
                  f"head={body[:200].replace(chr(10), ' ')!r}")

            # ── METAリダイレクト ──
            # ふたばは投稿成功時にも <meta refresh> で書き込んだスレへ誘導する
            # （responsemode=ajax が効かず HTML が返る場合）。無条件でスレ落ちと
            # せず、リダイレクト先が対象スレ (res/<resto>) か、本文に成功メッセージが
            # あれば成功、板トップ等へ飛ばすなら失敗（スレ落ち）と判別する。
            _body_low = body.lower()
            if "<meta" in _body_low and "refresh" in _body_low:
                m = re.search(r'url\s*=\s*["\']?([^"\'>\s]+)', body, re.I)
                dest = (m.group(1) if m else "").lower()
                posted_ok = bool(resto) and (
                    f"res/{resto}" in dest or f"/{resto}." in dest)
                if not posted_ok:
                    posted_ok = any(k in body for k in (
                        "書きこみました", "書き込みました", "投稿しました"))
                if posted_ok:
                    self.save_cookies()
                    return True, "", 0
                return False, "スレッドがありません\n（スレ落ち）", 0

            # ── プレーンテキストエラー（HTMLなし・短文）──
            # 「スレッドがありません」等、サーバーが直接テキストを返すケース
            if body and "<html" not in body.lower() and len(body) < 300:
                return False, body.strip(), 0

            # ── HTML フォールバック ──
            # responsemode=ajax が効かなかった or 旧サーバーの場合
            if any(pat in body for pat in FUTABA_ERROR_PATTERNS):
                soup = BeautifulSoup(body, "html.parser")
                err = (soup.find(id="errmsg")
                       or soup.find("h2")
                       or soup.find("font", color=re.compile(r"red|ff0000", re.I)))
                if err:
                    msg = err.get_text(strip=True)
                else:
                    for pat in FUTABA_ERROR_PATTERNS:
                        if pat in body:
                            idx = body.index(pat)
                            msg = body[max(0,idx-10):idx+80].strip()
                            break
                    else:
                        msg = "投稿に失敗しました"
                return False, msg, 0

            # それ以外（HTMLページ丸ごと等）→ 成功とみなす
            self.save_cookies()
            return True, "", 0

        except Exception as e:
            return False, str(e), 0

    @staticmethod
    def _guess_mime(path: str) -> str:
        """ファイル拡張子から MIME タイプを推定"""
        ext = Path(path).suffix.lower()
        return {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".gif":  "image/gif",
            ".webm": "video/webm", ".mp4": "video/mp4",
        }.get(ext, "application/octet-stream")

    # ── 内部 HTTP ─────────────────────────────────────────────────────────────

    def _get_html(self, url: str, referer: str = "") -> Optional[str]:
        import time as _time
        _t0 = _time.perf_counter()
        try:
            headers: dict = {}
            if referer:
                headers["Referer"] = referer
            # ブラウザと同様の Sec-Fetch ヘッダー
            headers.update({
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin" if referer else "none",
                "Sec-Fetch-User": "?1",
            })
            r = self.session.get(url, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            _t1 = _time.perf_counter()
            text = r.content.decode("cp932", errors="replace")
            self.last_fetch_error = ""
            return text
        except requests.RequestException as e:
            print(f"[Fetch] エラー [{url}]: {e}")
            try:
                _resp = getattr(e, "response", None)
                self.last_fetch_error = (f"{_resp.status_code} {_resp.reason}"
                                         if _resp is not None else f"接続エラー: {e}")
            except Exception:
                self.last_fetch_error = "通信エラー"
            return None

    # ── カタログ ──

    def fetch_catalog(self, board: BoardInfo, sort: int = 0,
                      cxyl_base: str = "") -> list[CatalogEntry] | None:
        """sort: 0=通常 1=新順 2=古順 3=多順 4=少順 6=勢順 7=見歴 8=そ順 9=履歴
        取得失敗（404等）の場合は None を返す（空リストとの区別用）

        大取得方式: 隔離(json∖cat)の誤判定を防ぐため、取得時のみ cxyl の
        cols×rows を 100x100 に上書きして板の全生存スレを取得する。
        表示側(CatalogView)が cols×rows 件に絞って「14×6+隔離」相当に振る舞う。
        取得後はユーザ設定の cxyl に必ず復元する（描画やフォールバックの汚染防止）。"""
        import time as _time
        url = (board.base_url + f"futaba.php?mode=cat&sort={sort}"
               if sort > 0 else board.catalog_url)
        # ── cxyl を 100x100 に一時上書き（chars/pos/img はユーザ値を維持） ──
        # 注意: 起動時に .2chan.net 全体へ、板別に板ドメインへ、と複数ドメインに
        # cxyl cookie が併存し得る。板ドメインだけ上書きすると .2chan.net 側の
        # 旧値もサブドメイン一致で同時送信され、サーバがそちらを採用して 100x100 が
        # 効かない。よって「全 cxyl cookie 除去 → 100x100 を単一設定 → GET →
        # 元の cookie 群を完全復元」とする。
        # chars/pos/img は板別設定(catalog_cxyl_str)を正本とする。
        # get_cxyl() は複数ドメインに併存する cxyl cookie の先頭1個を返すため、
        # .2chan.net グローバル既定(chars=4 等)を拾って板別 chars を無視する不具合があった。
        _orig_cxyl = cxyl_base or self.get_cxyl()
        _saved_cxyl = [(c.domain, c.path, c.value)
                       for c in self.session.cookies if c.name == "cxyl"]
        try:
            _p = (_orig_cxyl or "14x6x6x0x0").split("x")
            while len(_p) < 5:
                _p.append("0")
            _p[0] = "100"; _p[1] = "100"
            self._clear_cxyl_cookies()
            self.set_cxyl_cookie("x".join(_p[:5]))   # 板ドメイン無し=.2chan.net 全体に単一設定
        except Exception:
            pass
        try:
            html = self._get_html(url, referer=board.url)
        finally:
            # 元の cxyl cookie 群を完全復元（描画フォールバック/per-board設定の汚染防止）
            try:
                self._clear_cxyl_cookies()
                for _dom, _path, _val in _saved_cxyl:
                    self.session.cookies.set("cxyl", _val, domain=_dom, path=_path)
            except Exception:
                pass
        if html is None:
            return None
        _t0 = _time.perf_counter()
        result = self._parse_catalog(html, board)
        return result

    def _parse_catalog(self, html: str, board: BoardInfo) -> list[CatalogEntry]:
        soup = BeautifulSoup(html, "html.parser")
        entries: list[CatalogEntry] = []
        cat_table = soup.find("table", id="cattable")
        if not cat_table: return []
        for td in cat_table.find_all("td"):
            a=td.find("a"); img=td.find("img"); fnt=td.find("font")
            if not a: continue                       # リンクなしはスキップ
            href=a.get("href",""); m=re.search(r"res/(\d+)\.htm",href)
            if not m: continue
            # 他サーバー・他板のスレリンク混入を除外（board_top と同様のガード）
            _full = urllib.parse.urljoin(board.base_url, href)
            if not _full.startswith(board.base_url):
                continue
            sml = td.find("small")
            title = sml.get_text(strip=True) if sml else ""
            # [id表示][ip表示][ID表示] などをスペースあり・なし両対応で除去
            import re as _re
            title = _re.sub(r'\[\s*(?:id|ip)\s*表示\s*\]', '', title, flags=_re.IGNORECASE).strip()
            is_red = False
            if sml:
                rf = sml.find("font", color=re.compile(r"^#?[Ff][Ff]0{4}$|^red$", re.I))
                is_red = bool(rf)
            # img なし = 文字のみスレ → thumb_url を空文字で登録
            thumb = urllib.parse.urljoin(board.base_url, img.get("src", "")) if img else ""
            # メール欄（td内にcnmがある場合）
            _email = ""
            _cnm = td.find("span", class_="cnm")
            if _cnm:
                _a = _cnm.find("a")
                if _a:
                    _href2 = _a.get("href", "")
                    if _href2.startswith("mailto:"):
                        _email = _href2[len("mailto:"):]
            entries.append(CatalogEntry(
                no         = int(m.group(1)),
                thumb_url  = thumb,
                res_count  = int("".join(c for c in fnt.get_text(strip=True) if c.isdigit()) or "0") if fnt else 0,
                thread_url = urllib.parse.urljoin(board.base_url, href),
                title      = title,
                email      = _email,
                is_red     = is_red,
                board      = board,
            ))
        board.current_saved = len(entries)
        # 板別 global_max_no を更新
        if entries:
            _update_board_max_no(self._settings, board.base_url,
                                 max(e.no for e in entries), f'catalog/{board.name}')
        return entries

    def fetch_board_top(self, board: BoardInfo) -> list[CatalogEntry]:
        html = self._get_html(board.url)
        return self._parse_board_top(html, board) if html else []

    def _parse_board_top(self, html: str, board: BoardInfo) -> list[CatalogEntry]:
        soup = BeautifulSoup(html, "html.parser")
        entries: list[CatalogEntry] = []; seen: set[int] = set()

        # ── 板情報テキストをパース（ページ下部の <hr> 間テキスト） ───────────
        desc_lines = []
        for tag in soup.find_all(string=True):
            txt = tag.strip()
            if not txt:
                continue
            m_viewers = re.search(r'(\d[\d,]+)人くらいが見てます', txt)
            if m_viewers:
                board.viewers = int(m_viewers.group(1).replace(',', ''))
            m_saved = re.search(r'保存数は([\d,]+)件', txt)
            if m_saved:
                board.max_saved = int(m_saved.group(1).replace(',', ''))
            # 説明文（1行以上ある意味のあるテキスト）を収集
            if len(txt) > 5 and tag.parent and tag.parent.name not in ('script', 'style', 'head'):
                desc_lines.append(txt)
        if not board.board_desc and desc_lines:
            board.board_desc = '\n'.join(desc_lines[:10])  # 先頭10行まで保持

        for a in soup.find_all("a", href=re.compile(r"res/\d+\.htm")):
            href=a.get("href",""); m=re.search(r"res/(\d+)\.htm",href)
            if not m: continue
            # 他サーバー・他板のスレリンク（コメント本文中の引用リンク等）を除外。
            # 解決後URLが自板の base_url 配下でなければ別板のNoなのでスキップ
            # （global_max_no への他板カウンタ混入 = 「異常増加スキップ」の根本原因）
            _full = urllib.parse.urljoin(board.base_url, href)
            if not _full.startswith(board.base_url):
                continue
            no=int(m.group(1))
            if no in seen: continue
            seen.add(no)
            # <div class="thre"> まで遡る（なければ直親）
            thre_div = a.find_parent("div", class_="thre") or a.parent
            thumb = ""
            if thre_div:
                img = thre_div.find("img", src=re.compile(r"/thumb/"))
                if img: thumb = urllib.parse.urljoin(board.base_url, img.get("src", ""))
            # メール欄（cnm の mailto: href）を取得
            email = ""
            if thre_div:
                cnm = thre_div.find("span", class_="cnm")
                if cnm:
                    _a = cnm.find("a")
                    if _a:
                        _href = _a.get("href", "")
                        if _href.startswith("mailto:"):
                            email = _href[len("mailto:"):]
            entries.append(CatalogEntry(no=no, thumb_url=thumb, res_count=0,
                thread_url=urllib.parse.urljoin(board.base_url, href),
                email=email, board=board))
        # 板別 global_max_no を更新
        if entries:
            _update_board_max_no(self._settings, board.base_url,
                                 max(e.no for e in entries), f'board_top/{board.name}')
        return entries

    def fetch_catalog_json(self, board: BoardInfo) -> Optional[dict]:
        """板単位 mode=json を取得し、各OPスレの email / id と存在Noを返す。

        GET /futaba.php?mode=json （res= を付けない板単位呼び出し）。
        ふたばのJSON API は UTF-8（HTMLページの Shift_JIS とは異なる）。

        戻り値 dict:
          "map" : {no(int): {"email","id","com","sub","thumb"}}  各OPスレの情報
          "nos" : set[int]                                       json に存在するスレNo集合
        失敗時は None（呼び出し側で隔離判定・バッジ補完をスキップさせる）。
        ※ json 内の "rsc" は板単位では連番インデックスでありレス数ではない点に注意。
        ※ 隔離スレ（カタログから消えて json に残る）合成用に com/sub/thumb も返す。
        """
        import random as _rand, json as _json
        url = board.base_url + f"futaba.php?mode=json&{_rand.random()}"
        try:
            hdr = {
                "Referer": board.url,
                "Accept": "application/json, */*",
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            }
            r = self.session.get(url, headers=hdr, timeout=self.timeout)
            if not r.ok:
                print(f"[CatalogJSON] fetch失敗: {r.status_code} {r.reason}")
                return None
            data = _json.loads(r.content.decode("utf-8", errors="replace"))
        except Exception as e:
            print(f"[CatalogJSON] error: {e}")
            return None
        res = data.get("res")
        if not isinstance(res, dict):
            return None
        info_map: dict = {}
        nos: set = set()
        for k, v in res.items():
            try:
                no = int(k)
            except (TypeError, ValueError):
                continue
            if not isinstance(v, dict):
                continue
            nos.add(no)
            _thumb = (v.get("thumb", "") or "").strip()
            info_map[no] = {
                "email": (v.get("email", "") or "").strip(),
                "id":    (v.get("id", "") or "").strip(),
                "com":   v.get("com", "") or "",
                "sub":   v.get("sub", "") or "",
                # 隔離スレ合成用に絶対URL化（json は "/b/thumb/..." 形式）
                "thumb": urllib.parse.urljoin(board.base_url, _thumb) if _thumb else "",
            }
        return {"map": info_map, "nos": nos}

    # ── スレッド ──

    def fetch_raw_thread_html(self, board: BoardInfo, no: int) -> Optional[str]:
        """ログ保存(方式A)用: スレッドの原本HTML(広告込みの生htm)を取得して返す。
        ・成功時はディスクキャッシュを更新し、差分サイドカーをクリアする
          （フルGETで完全化されるため diff 不要）
        ・取得失敗時はディスクキャッシュの原本htmにフォールバックする
        ThreadData は作らず、生のHTML文字列のみ返す。"""
        url = board.base_url + f"res/{no}.htm"
        try:
            hdr: dict = {"Referer": board.url}
            hdr.update({
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            })
            r = self.session.get(url, headers=hdr, timeout=self.timeout)
            if r.ok:
                html = r.content.decode("cp932", errors="replace")
                self._save_thread_cache(url, html)
                self._clear_diff_sidecar(url)
                print(f'[NET] fetch_raw_thread_html  ok  len={len(html)}  url={url}')
                return html
            print(f'[NET] fetch_raw_thread_html  status={r.status_code} → cache fallback')
        except Exception as e:
            print(f'[NET] fetch_raw_thread_html error: {e} → cache fallback')
        return self._load_thread_cache(url)

    def fetch_thread(self, board: BoardInfo, no: int) -> ThreadData:
        """
        スレッドを取得する。失敗時はキャッシュから復元する。
        取得成功時は data/log/{path}.htm にキャッシュを保存する。
        """
        url = board.base_url + f"res/{no}.htm"
        now_str = datetime.datetime.now().strftime("%Y/%m/%d (%a) %H:%M:%S")
        print(f'[NET] fetch_thread  url={url}')
        try:
            hdr: dict = {"Referer": board.url}
            hdr.update({
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            })
            r = self.session.get(url, headers=hdr, timeout=self.timeout)
            print(f'[NET] fetch_thread  status={r.status_code}  size={len(r.content)}B'
                  f'  encoding={r.encoding}')
            if not r.ok:
                # 4xx/5xx → キャッシュで代替
                cached = self._load_thread_cache(url)
                if cached:
                    thread = self._parse_thread(cached, board, no, url)
                    self._merge_diff_sidecar(url, thread)   # diff由来の末尾レス補完
                    thread.is_cached    = True
                    thread.last_updated = now_str
                    thread.error        = f"{r.status_code} {r.reason}  (キャッシュ表示)"
                    print(f'[NET] fetch_thread  → cache fallback  res={len(thread.res_list)}')
                    return thread
                print(f'[NET] fetch_thread  → error, no cache')
                return ThreadData(
                    no=no, board=board,
                    title=f"No.{no} - {board.name}",
                    url=url, error=f"{r.status_code} {r.reason}",
                )
            # ふたばは charset=Shift_JIS を返すが、requestsの自動判定だと
            # Python厳密shift_jis扱いになり ①②(NEC拡張)が �@�A に化ける。
            # CP932で明示デコードする。
            html = r.content.decode("cp932", errors="replace")
            print(f'[NET] fetch_thread  html_len={len(html)}  decoded_ok=True')
            if len(html) >= 300_000:
                print(f'[NET] fetch_thread  WARNING: large html ({len(html)//1024}KB)  url={url}')
            self._save_thread_cache(url, html)     # キャッシュ保存
            self._clear_diff_sidecar(url)          # フルGETで完全化 → diff不要
            thread = self._parse_thread(html, board, no, url)
            thread.last_updated    = now_str
            thread.received_count  = len(thread.res_list)
            res_count = len(thread.res_list)
            print(f'[NET] fetch_thread  parse done  res={res_count}'
                  f'  is_expiring={thread.is_expiring}')
            # 大きなスレッド（res200超 or html 200KB超）はメモリ情報も出力
            if res_count >= 200 or len(html) >= 200_000:
                if _psutil:
                    import os as _os
                    mb = _psutil.Process(_os.getpid()).memory_info().rss / 1024 / 1024
                    print(f'[MEM] rss={mb:.0f}MB  html_len={len(html)}  res={res_count}  url={url}')
                else:
                    print(f'[MEM] html_len={len(html)}  res={res_count}  url={url}')
                # 大容量スレのsoupオブジェクトはメモリを大量消費するため即時GC
                gc.collect()
            return thread
        except requests.RequestException as e:
            print(f'[NET] fetch_thread  RequestException: {e}')
            cached = self._load_thread_cache(url)
            if cached:
                thread = self._parse_thread(cached, board, no, url)
                self._merge_diff_sidecar(url, thread)   # diff由来の末尾レス補完
                thread.is_cached    = True
                thread.last_updated = now_str
                thread.error        = f"接続エラー (キャッシュ表示): {e}"
                return thread
            return ThreadData(
                no=no, board=board,
                title=f"No.{no} - {board.name}",
                url=url, error=str(e),
            )
        except Exception as e:
            tb = traceback.format_exc()
            print(f'[NET] fetch_thread  UNHANDLED EXCEPTION: {e}\n{tb}')
            return ThreadData(
                no=no, board=board,
                title=f"No.{no} - {board.name}",
                url=url, error=f"内部エラー: {e}",
            )

    def _parse_thread(self, html: str, board: BoardInfo, no: int, url: str) -> ThreadData:
        print(f'[NET] _parse_thread  start  html_len={len(html)}  parser={_BS4_PARSER}  url={url}')
        # <script>/<style>を除去してからパース（大容量HTML対策）
        _html_to_parse = re.sub(r'<script[^>]*>.*?</script>', '', html,
                                flags=re.DOTALL | re.IGNORECASE)
        _html_to_parse = re.sub(r'<style[^>]*>.*?</style>', '', _html_to_parse,
                                flags=re.DOTALL | re.IGNORECASE)
        # ── charset宣言metaを除去 ──────────────────────────────────────────
        # htmlは既に cp932 で正しくデコード済みのUnicode。だが
        # <META http-equiv="Content-type" content="text/html; charset=Shift_JIS">
        # が残っていると、BS4経由でlxml(libxml2)がこの宣言を信じて入力を
        # Shift_JISとして再デコードしようとし、UTF-8側のバイト列で
        # "input conversion failed" を起こした地点以降を切り捨てる
        # （= 末尾レス欠落。1000レスが957等に化ける。画像/特定文字が多いスレほど
        #   変換失敗バイトに早く当たり欠落数が増える）。charset宣言を取り除いて
        # libxml2がShift_JISを選べないようにする。
        _html_to_parse, _meta_n = re.subn(
            r'<meta[^>]*charset[^>]*>', '', _html_to_parse, flags=re.IGNORECASE)
        if _meta_n:
            print(f'[NET] _parse_thread  charset-meta removed x{_meta_n}  no={no}')
        _stripped = len(html) - len(_html_to_parse)
        if _stripped > 0:
            print(f'[NET] _parse_thread  stripped  {len(html)}B->{len(_html_to_parse)}B (-{_stripped}B)')
        # 設定値KBを超えるHTMLは同時1スレッドに制限（並行パースによるメモリ不足防止）
        _sem_thresh = getattr(self._settings, 'parse_sem_kb', 50) * 1024
        _use_sem = (len(_html_to_parse) >= _sem_thresh)
        if _use_sem:
            print(f'[NET] _parse_thread  waiting sem  {len(_html_to_parse)//1024}KB  no={no}')
            _large_parse_sem.acquire()
            print(f'[NET] _parse_thread  sem acquired  no={no}')
        try:
            try:
                soup = BeautifulSoup(_html_to_parse, _BS4_PARSER)
            except RecursionError:
                print(f'[NET] _parse_thread  RecursionError with {_BS4_PARSER}, fallback lxml')
                try:
                    soup = BeautifulSoup(_html_to_parse, 'lxml')
                except Exception as _e2:
                    print(f'[NET] _parse_thread  fallback also failed: {_e2}')
                    raise
        finally:
            if _use_sem:
                _large_parse_sem.release()
                print(f'[NET] _parse_thread  sem released  no={no}')
        print(f'[NET] _parse_thread  soup_ok  no={no}')
        import re as _re
        title=soup.title.get_text(strip=True) if soup.title else f"No.{no}"
        # ふたばの <title> は「スレ名 - 板名」形式。スレ名が空のスレ（subjectなし・
        # bodyのみ。例: ｷﾀ━━(ﾟ∀ﾟ)━━ !!!!!）では <title> が板名だけになる。
        # board.name は短縮名（例「二次元裏」）で <title> 末尾の正式名（例「二次元裏＠ふたば」）
        # と完全一致しないことがあるため、以下のいずれかなら blockquote 本文をタイトルにする：
        #   ① title が board.name と一致 / 空
        #   ② title に " - " が無い（=スレ名部分が存在しない＝板名のみ）
        #   ③ " - " 手前のスレ名部分が空
        _title_subject = title.split(" - ", 1)[0].strip() if " - " in title else ""
        _is_board_only = (
            title == board.name
            or not title
            or " - " not in title
            or not _title_subject
        )
        if _is_board_only:
            bq = soup.find("blockquote")
            if bq:
                # <br> のみを改行として扱う（<font>等インライン要素では改行を入れない。
                # IP表示の [ ] が <font> で分断されるのを防ぐ）。元soupを壊さないため
                # blockquoteを文字列から再パースしたコピーに対して処理する
                from bs4 import BeautifulSoup as _BS
                bq_copy = _BS(str(bq), "html.parser")
                for _br in bq_copy.find_all("br"):
                    _br.replace_with("\n")
                bq_text = bq_copy.get_text()
                if bq_text:
                    lines = [l.strip() for l in bq_text.splitlines() if l.strip()]
                    # 先頭がIP表示行（行全体が [xxx]）ならスキップして次行をタイトルに
                    while lines and _re.match(r'^\[[^\]]*\]$', lines[0]):
                        lines.pop(0)
                    bq_text = lines[0] if lines else ""
                    title = bq_text[:50] if bq_text else title
        else:
            # 通常スレ: <title>=「スレ名 - 板名」→ スレ名のみを採用（末尾の板名を除去）
            title = _title_subject or title
        # ── メール欄に ・3・ 指定のスレはタイトル先頭に [IPアドレス] が付くため除去 ──
        # どの経路（<title>由来・body由来）で作られたタイトルでも最後に共通で掃除する
        title = _re.sub(r'^\s*\[[^\]]*\]\s*', '', title).strip() or title
        thread=ThreadData(no=no, board=board, title=title, url=url)
        span=soup.find("span",class_="cntd")
        if span: thread.expiry=span.get_text(strip=True)
        # 落ちかけ判定: contdisp を赤字にする JS が存在するか
        # 例: m();if(document.getElementById("contdisp")!=null){...style.color="#ff0000"};
        thread.is_expiring = 'contdisp' in html and 'style.color' in html and 'ff0000' in html
        # 1000レス上限判定: <span class="maxres">上限1000レスに達しました</span>
        # 空のmaxresスパンが存在する場合があるのでテキストも確認する
        _maxres = soup.find('span', class_='maxres')
        thread.is_full = bool(_maxres and _maxres.get_text(strip=True))
        # 削除された記事数
        ddnum=soup.find("span",id="ddnum")
        if ddnum:
            try: thread.deleted_count=int(ddnum.get_text(strip=True))
            except ValueError: pass

        # ── 板情報をスレHTMLの <table class="ftb2"> から取得してboard に反映 ──
        ftb2 = soup.find("table", class_="ftb2")
        if ftb2:
            chui = ftb2.find("td", class_="chui")
            if chui:
                ul = chui.find("ul")
                if ul:
                    rules_lines = []
                    for li in ul.find_all("li", recursive=False):
                        # カタログリンク行は除外
                        if "catlink" in (li.get("class") or []):
                            continue
                        txt = li.get_text(separator=" ", strip=True)
                        if not txt:
                            continue
                        # 視聴者数: "現在1944人くらいが見てます"
                        m_v = re.search(r'(\d[\d,]*)人くらいが見てます', txt)
                        if m_v:
                            board.viewers = int(m_v.group(1).replace(',', ''))
                        # 保存数: "保存数はn件"
                        m_s = re.search(r'保存数は([\d,]+)件', txt)
                        if m_s:
                            board.max_saved = int(m_s.group(1).replace(',', ''))
                        rules_lines.append(txt)
                    board.board_rules_text = '\n'.join(rules_lines)
                    board.board_rules_html = str(ul)

        # ── 添付ファイルサイズ上限を投稿フォームの MAX_FILE_SIZE から取得 ──
        # （script/style/charset-meta 除去後も投稿フォームは soup に残る）
        _mfs = soup.find("input", attrs={"name": "MAX_FILE_SIZE"})
        if _mfs is not None:
            try:
                _mfs_v = int(str(_mfs.get("value", "")).strip())
                if _mfs_v > 0:
                    board.max_file_bytes = _mfs_v
            except (ValueError, TypeError):
                pass

        op=self._parse_op(soup, board, no)
        if op: thread.res_list.append(op)
        for tbl in soup.find_all("table", attrs={"border":"0"}):
            rtd=tbl.find("td",class_="rtd")
            if rtd:
                res=self._parse_res_node(rtd, board)
                if res: thread.res_list.append(res)

        # cnmスパンが1件もない板（img板等）はhas_name_field=False
        if not soup.find("span", class_="cnm"):
            board.has_name_field = False

        # global_max_no = スレOPのNo.最大値（レスNoは使わない）
        # OP No.＝そのスレが作成された時点のカウンタ値 → 板の現在位置の正確な指標
        # 板別 global_max_no を更新（OP No. および最新レスNo の大きい方を使用）
        _max_res_no = max((r.no for r in thread.res_list), default=thread.no)
        _gmax_no = max(thread.no, _max_res_no)
        _update_board_max_no(self._settings, board.base_url, _gmax_no,
                             f'thread/{thread.no}')

        # soupオブジェクトへの参照を明示的に解放（大容量HTMLのメモリ節約）
        del soup
        return thread

    def _parse_op(self, soup, board, no) -> Optional[ResData]:
        tu=iu=iname=""; isz=tw=th=0; fsz=0

        # img板判定: OPが div.thre で構成されている
        thre = soup.find("div", class_="thre")
        is_img_board = (thre is not None)

        # サムネ取得（img板: thre内、may板: soup全体）
        ctx = thre if is_img_board else soup
        ti = None
        op_a = None
        if is_img_board:
            # OP自身のメディアは返信(td.rtd)の外にある。「サムネ保存しない」ログでは
            # OPサムネが /thumb/ → /src/ に差し替わり、旧来の find(img, /thumb/) が
            # OP画像を素通りして返信(動画)の /thumb/ サムネを誤取得し、OPに動画ファイル名が
            # 混入していた。/thumb/・/src/ を問わず、form・td.rtd の外にある最初の
            # 画像リンク(<a href=src><img></a>)をOP自身のメディアとして採用する。
            for a in ctx.find_all("a", href=_SRC_HREF_RE):
                if a.find_parent("form") or a.find_parent("td", class_="rtd"):
                    continue
                if a.find("img"):
                    op_a = a
                    break
            if op_a is not None:
                ti = op_a.find("img")
        if op_a is not None and ti is not None:
            tu=urllib.parse.urljoin(board.base_url,ti.get("src",""))
            tw=int(ti.get("width",0) or 0); th=int(ti.get("height",0) or 0)
            m=re.match(r"(\d+)",ti.get("alt",""))
            if m: isz=int(m.group(1))
            full=op_a.get("href",""); iu=urllib.parse.urljoin(board.base_url,full)
            iname=full.split("/")[-1]
        else:
            # OP画像が削除されると（30超スレでOP画像を消すと本文が「ｷﾀ━━━」化）
            # ctx(thre)内の最初のサムネが最初の返信(td.rtd内)画像になり、OPに
            # 別レスの画像/動画が誤表示される。返信(td.rtd)・フォームの外にある
            # サムネのみをOP画像として採用する。
            ti = None
            for _ti in ctx.find_all("img", src=_THUMB_SRC_RE):
                if _ti.find_parent("form") or _ti.find_parent("td", class_="rtd"):
                    continue
                ti = _ti
                break
            if ti:
                tu=urllib.parse.urljoin(board.base_url,ti.get("src",""))
                tw=int(ti.get("width",0) or 0); th=int(ti.get("height",0) or 0)
                m=re.match(r"(\d+)",ti.get("alt",""))
                if m: isz=int(m.group(1))
                pa=ti.find_parent("a")
                if pa:
                    full=pa.get("href",""); iu=urllib.parse.urljoin(board.base_url,full)
                    iname=full.split("/")[-1]

        # OP ファイルサイズ: "画像ファイル名：<a>name</a>-(N B)" 形式
        if iu:
            src_ctx = thre if is_img_board else soup
            for a in src_ctx.find_all("a", href=_SRC_HREF_RE):
                if a.find_parent("form"): continue
                if not a.find("img"):
                    nxt = a.next_sibling
                    if nxt:
                        m2 = _FSIZE_RE.search(str(nxt))
                        if m2: fsz = int(m2.group(1))
                    break

        # No.取得
        cno_ctx = thre if is_img_board else soup
        cno=cno_ctx.find("span",class_="cno"); rno=no
        if cno:
            m2=_RES_NO_RE.search(cno.get_text())
            if m2: rno=int(m2.group(1))

        # name / email（cnmがあれば取得。img板でも may板 等 cnm がある板に対応）
        name=email=""
        cnm_ctx = thre if is_img_board else soup
        cnm=cnm_ctx.find("span",class_="cnm")
        if cnm:
            a=cnm.find("a")
            if a:
                href = a.get("href", "")
                if href.startswith("mailto:"):
                    email = href[len("mailto:"):]
                    name  = a.get_text(strip=True)
                else:
                    name = a.get_text(strip=True)
            else:
                name=cnm.get_text(strip=True)

        # 日時（cnw）
        cnw_ctx = thre if is_img_board else soup
        cnw=cnw_ctx.find("span",class_="cnw")
        if cnw:
            cnw_a = cnw.find("a")
            if cnw_a and cnw_a.get("href","").startswith("mailto:"):
                email = cnw_a.get("href","")[len("mailto:"):]
            dts = cnw.get_text(strip=True)
        else:
            dts = ""

        # csb（img板はcsbなし → 空文字、may板等はcsb取得）
        csb_ctx = thre if is_img_board else soup
        csb_el = csb_ctx.find("span",class_="csb")
        csb_text = csb_el.get_text(strip=True) if csb_el else ""

        # trip（cnmまたはcnteから取得）
        trip = ""
        trip_ctx = thre if is_img_board else soup
        trip_el = trip_ctx.find("span",class_="cnm") or trip_ctx.find("span",class_="cnte")
        if trip_el:
            raw = trip_el.get_text(strip=True)
            m_trip = _TRIP_RE.search(raw)
            trip = m_trip.group(0) if m_trip else ""

        # コメント（blockquote）
        bq_ctx = thre if is_img_board else soup
        bq=bq_ctx.find("blockquote"); ch=str(bq) if bq else ""; ct=bq.get_text(separator="\n") if bq else ""

        # そうだね
        sod=0
        sod_ctx = thre if is_img_board else soup
        s=sod_ctx.find("a",class_="sod",id=f"sd{no}")
        if s:
            m3=_SODANE_RE.search(s.get_text())
            if m3: sod=int(m3.group(1))

        # ID/IP (cnwテキスト内)
        id_m = _RES_ID_RE.search(dts)
        id_str = id_m.group(1) if id_m else ""
        ip_m = _RES_IP_RE.search(dts)
        ip_str = ip_m.group(1) if ip_m else ""

        return ResData(no=rno,name=name,trip=trip,email=email,datetime_str=dts,subject="",
            comment_html=ch,comment_text=ct,image_url=iu,thumb_url=tu,
                    csb=csb_text,
            image_name=iname,image_size=isz,thumb_w=tw,thumb_h=th,sodane=sod,is_op=True,
            res_idx=0,file_size_bytes=fsz,id_str=id_str,ip_str=ip_str)

    def _parse_res_node(self, node, board) -> Optional[ResData]:
        cno=node.find("span",class_="cno")
        if not cno: return None
        m=_RES_NO_RE.search(cno.get_text())
        if not m: return None
        rno=int(m.group(1))
        cnm=node.find("span",class_="cnm"); name=email=""
        if cnm:
            a=cnm.find("a")
            if a:
                href = a.get("href", "")
                if href.startswith("mailto:"):
                    email = href[len("mailto:"):]
                    name  = a.get_text(strip=True)
                else:
                    name = a.get_text(strip=True)
            else: name=cnm.get_text(strip=True)
        cnw=node.find("span",class_="cnw")
        if cnw:
            # img板: cnwの日付が <a href="mailto:..."> で囲まれている場合がある
            cnw_a = cnw.find("a")
            if cnw_a and cnw_a.get("href","").startswith("mailto:"):
                email = cnw_a.get("href","")[len("mailto:"):]
            dts = cnw.get_text(strip=True)
        else:
            dts = ""
        # csb（感情）を正しく取得。csbがない板（img板等）は空文字
        csb_el = node.find("span", class_="csb")
        csb_text = csb_el.get_text(strip=True) if csb_el else ""
        # trip は名前に付く !◆ から始まるものだけ
        cnm_el = cnm   # 冒頭で取得済みのものを再利用（二重ツリー走査防止）
        trip = ""
        if cnm_el:
            raw = cnm_el.get_text(strip=True)
            m_trip = _TRIP_RE.search(raw)
            trip = m_trip.group(0) if m_trip else ""
        bq=node.find("blockquote"); ch=str(bq) if bq else ""; ct=bq.get_text(separator="\n") if bq else ""
        # 削除判定: ① テーブルに class="deleted" がある、または
        # ② blockquote内の <font color="#ff0000"> の前に "[" がない
        _tbl_cls = node.parent.parent.get("class", []) if (
            node.parent and node.parent.parent) else []
        isdel = "deleted" in _tbl_cls
        if not isdel:
            _font_el = bq.find("font", color="#ff0000") if bq else None
            if _font_el:
                _prev = _font_el.previous_sibling
                _prev_str = str(_prev) if _prev is not None else ""
                isdel = "[" not in _prev_str   # "[" があればIP表示、なければ削除
        sod=0; s=node.find("a",class_="sod")
        if s:
            m2=_SODANE_RE.search(s.get_text())
            if m2: sod=int(m2.group(1))
        # rsc: スレ内連番
        rsc=node.find("span",class_="rsc")
        res_idx=0
        if rsc:
            try: res_idx=int(rsc.get_text(strip=True))
            except ValueError: pass
        tu=iu=iname=""; isz=tw=th=0; fsz=0
        ti=node.find("img",src=_THUMB_SRC_RE)
        if ti:
            tu=urllib.parse.urljoin(board.base_url,ti.get("src",""))
            tw=int(ti.get("width",0) or 0); th=int(ti.get("height",0) or 0)
            pa=ti.find_parent("a")
            if pa:
                full=pa.get("href",""); iu=urllib.parse.urljoin(board.base_url,full)
                iname=full.split("/")[-1]
        # ファイルサイズ: テキストリンクの後 "-(N B)"
        if iu:
            for a in node.find_all("a", href=_SRC_HREF_RE):
                if not a.find("img"):
                    nxt = a.next_sibling
                    if nxt:
                        m3 = _FSIZE_RE.search(str(nxt))
                        if m3: fsz = int(m3.group(1))
                    fn = a.get_text(strip=True)
                    if fn: iname = fn
                    break
        id_m = _RES_ID_RE.search(dts)
        id_str = id_m.group(1) if id_m else ""
        ip_m = _RES_IP_RE.search(dts)
        ip_str = ip_m.group(1) if ip_m else ""
        return ResData(no=rno,name=name,trip=trip,email=email,datetime_str=dts,subject="",
            comment_html=ch,comment_text=ct,image_url=iu,thumb_url=tu,
            image_name=iname,image_size=isz,thumb_w=tw,thumb_h=th,
            sodane=sod,is_op=False,is_deleted=isdel,
            res_idx=res_idx,file_size_bytes=fsz,id_str=id_str,ip_str=ip_str,csb=csb_text)



    def fetch_thread_diff(self, board: "BoardInfo", no: int, start_no: int) -> dict:
        """
        JSON差分APIでスレの新着レスのみ取得する。
        GET /futaba.php?mode=json&res={no}&start={start_no}&{乱数}

        戻り値 dict:
          "new_res"   : list[ResData]  新着レスのリスト（なければ空リスト）
          "rsc"       : int            現在の総レス数（新着の最後のrscフィールド）
          "is_full"   : bool           maxres が空でない = 1000レス到達
          "die"       : str            スレ落ち予定時刻文字列 ("03:47" 形式)
          "dielong"   : str            スレ落ち予定日時 (RFC形式)
          "nowtime"   : int            サーバー現在時刻 (Unixタイム)
          "sd"        : dict           そうだね数 {レスNo文字列: 件数文字列}
          "error"     : str            エラー文字列（正常時は空）
        """
        import random as _rand, json as _json
        result = {
            "new_res": [], "rsc": 0, "is_full": False, "is_dead": False,
            "die": "", "dielong": "", "nowtime": 0, "sd": {}, "error": ""
        }
        url = board.base_url + f"futaba.php?mode=json&res={no}&start={start_no}&{_rand.random()}"
        try:
            hdr = {
                "Referer": board.base_url + f"res/{no}.htm",
                "Accept": "application/json, */*",
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            }
            r = self.session.get(url, headers=hdr, timeout=self.timeout)
            if not r.ok:
                result["error"] = f"{r.status_code} {r.reason}"
                return result
            # ふたばのJSON差分API(futaba.php?mode=json)は application/json; charset=utf-8。
            # HTMLページ(Shift_JIS)とは異なり UTF-8 なので UTF-8 でデコードする。
            # （cp932でデコードすると自動更新で取得する新着レスが文字化けする）
            data = _json.loads(r.content.decode("utf-8", errors="replace"))
        except Exception as e:
            result["error"] = str(e)
            return result

        result["die"]      = data.get("die", "")
        result["dielong"]  = data.get("dielong", "")
        result["nowtime"]  = data.get("nowtime", 0)
        _sd_raw = data.get("sd", {})
        # sd がlistで返ってくる場合（そうだね0件時は空配列 [] が正常仕様）
        if isinstance(_sd_raw, dict):
            result["sd"] = _sd_raw
        else:
            if _sd_raw:   # 空でないlist等、想定外の形だけ警告
                print(f"[SD] warn: sd is {type(_sd_raw).__name__} (expected dict), raw={repr(_sd_raw)[:120]}")
            result["sd"] = {}
        result["is_full"]  = bool(data.get("maxres", ""))
        # dielong が 1972年以前 = Unixエポック付近 → スレ落ち
        _dielong = result["dielong"]
        if _dielong:
            try:
                from email.utils import parsedate_to_datetime as _pdt
                _dt = _pdt(_dielong)
                result["is_dead"] = (_dt.year < 1972)
            except Exception:
                pass

        res_dict = data.get("res", {})
        if not res_dict:
            return result  # 新着なし

        new_res = []
        for res_no_str, rd in res_dict.items():
            try:
                rno = int(res_no_str)
            except ValueError:
                continue
            com_html = rd.get("com", "")
            # 改行コードを <br> に変換（HTMLではなくテキストで来る場合の正規化）
            com_text = com_html.replace("<br>", "\n").replace("<br/>", "\n")
            # HTMLタグ除去でプレーンテキスト
            import re as _re
            com_text_plain = _re.sub(r"<[^>]+>", "", com_text).strip()

            ext  = rd.get("ext", "")
            tim  = rd.get("tim", "")
            src  = rd.get("src", "")
            thumb = rd.get("thumb", "")
            # src/thumb が相対パスなら絶対URLに
            # /b/src/... のような絶対パスも urljoin で正しく結合する
            _host_base = board.base_url.split("/", 3)[:3]  # ['https:', '', 'may.2chan.net']
            _host = "/".join(_host_base) + "/"             # 'https://may.2chan.net/'
            if src and not src.startswith("http"):
                src = _host + src.lstrip("/") if src.startswith("/") else board.base_url + src
            if thumb and not thumb.startswith("http"):
                thumb = _host + thumb.lstrip("/") if thumb.startswith("/") else board.base_url + thumb
            # ext がある場合はsrcを構築（src が空のケース対応）
            if ext and tim and not src:
                src   = f"{board.base_url}src/{tim}{ext}"
                thumb = f"{board.base_url}thumb/{tim}s.jpg"
            iname = f"{tim}{ext}" if tim and ext else ""

            rsc = rd.get("rsc", 0)
            res = ResData(
                no=rno,
                name=rd.get("name", ""),
                trip="",
                email=rd.get("email", ""),
                datetime_str=rd.get("now", ""),
                subject=rd.get("sub", ""),
                comment_html=com_html,
                comment_text=com_text_plain,
                image_url=src,
                thumb_url=thumb,
                image_name=iname,
                image_size=rd.get("w", 0) * rd.get("h", 0),  # ピクセル数
                # JSONの w/h はサムネの表示寸法（HTMLの<img width/height>と同値）。
                # 0のままだと差分由来レスだけ width/height 属性なしで描画され、
                # flexレイアウト内でサムネが実寸より小さく表示されることがある。
                thumb_w=rd.get("w", 0), thumb_h=rd.get("h", 0),
                sodane=0,
                is_op=False,
                res_idx=rsc,
                file_size_bytes=rd.get("fsize", 0),
                id_str=rd.get("id", ""),
                ip_str=rd.get("ip", ""),
            )
            new_res.append(res)
            if rsc:
                result["rsc"] = rsc

        # レスNoでソート（辞書の順序が保証されない場合のため）
        new_res.sort(key=lambda r: r.no)
        result["new_res"] = new_res
        return result

    def delete_res(self, board: "BoardInfo", no: int, pwd: str,
                   onlyimg: bool = False,
                   thread_url: str = "") -> tuple[bool, str]:
        """記事削除 (usrdel mode) – Referer はスレッドURL"""
        import urllib.parse as _up
        onlyimgdel = "on" if onlyimg else ""
        safe_pwd   = _up.quote(pwd, safe="")
        data    = f"{no}=delete&responsemode=ajax&pwd={safe_pwd}&onlyimgdel={onlyimgdel}&mode=usrdel"
        parsed  = _up.urlparse(board.url)
        host    = parsed.hostname or ""
        referer = thread_url or board.url  # スレURLを優先
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": referer,
            "Origin": f"https://{host}",
        }
        try:
            resp = self.session.post(
                board.post_url, data=data, headers=headers, timeout=self.timeout)
            # ふたばは Shift_JIS でエラーメッセージを返すため明示的にデコード
            body = resp.content.decode("cp932", errors="replace").strip()
            if body == "ok":
                return True, "登録しました"
            # サーバーによって "OK" や空レスポンスで成功することがある
            if resp.status_code == 200 and not body:
                return True, "登録しました"
            return False, body[:200] or "削除に失敗しました"
        except Exception as e:
            return False, str(e)

    def report_del(self, board: "BoardInfo", no: int,
                   thread_url: str = "") -> tuple[bool, str]:
        """削除依頼を board と同じサーバーの /del.php に送信"""
        import urllib.parse as _up
        parsed  = _up.urlparse(board.url)
        host    = parsed.hostname or ""
        # bcode: board.url の path 1段目 (例: /b/futaba.htm → "b", /junbi/futaba.htm → "junbi")
        bcode   = parsed.path.strip("/").split("/")[0]
        # del.php は www.2chan.net ではなく board と同じサーバーに送る
        del_url = f"https://{host}/del.php"
        referer = thread_url or board.url
        data    = f"mode=post&b={bcode}&d={no}&reason=110&responsemode=ajax"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": referer,
            "Origin":  f"https://{host}",
        }
        try:
            resp = self.session.post(
                del_url, data=data, headers=headers, timeout=self.timeout)
            body = resp.content.decode("cp932", errors="replace").strip()
            print(f"[REPORT_DEL] status={resp.status_code}  body={body[:80]!r}")
            return (True, "登録しました") if body == "ok" else (False, body[:200] or "削除依頼に失敗しました")
        except Exception as e:
            return False, str(e)

    def post_sodane(self, board: "BoardInfo", res_no: int) -> int:
        """
        そうだね (いいね) を送信して更新後のカウントを返す。
        GET /sd.php?{board_code}.{res_no}
        """
        parsed = urllib.parse.urlparse(board.url)
        hostname = parsed.hostname or ""
        board_code = parsed.path.split("/")[1] if parsed.path else ""
        # Charles ログで確認: そうだねは http で送信される
        url = f"https://{hostname}/sd.php?{board_code}.{res_no}"
        try:
            r = self.session.get(url, headers={"Referer": board.url}, timeout=self.timeout)
            if r.ok:
                try:
                    return int(r.text.strip())
                except ValueError:
                    pass
        except Exception as e:
            print(f"[Sodane] エラー: {e}")
        return -1

    def fetch_image_bytes(self, url: str, retry_404: bool = False) -> Optional[bytes]:
        """
        画像データを返す。
        1) メモリキャッシュ → 2) ディスクキャッシュ → 3) HTTP の順で探す。
        retry_404=True の場合、404/接続エラー時に間隔を空けて数回リトライする。
        スレ落ち直後の自動保存で、ふたば側のスレ削除処理中に src/ が一時的に
        404 を返すケース（少し待つと取得できる）を救済するため。
        """
        # メモリキャッシュ
        if url in self._img_cache:
            return self._img_cache[url]
        # ディスクキャッシュ
        cache_path = self._img_disk_path(url)
        if cache_path.exists():
            data = cache_path.read_bytes()
            self._store_img_cache(url, data)
            return data
        # HTTP 取得（ブラウザの <img> 読込と同等のヘッダを付与）
        # ふたばの src/ はリクエストヘッダで弾く対策があり、Referer 無し／
        # Sec-Fetch 無しの素のリクエストに 404 を返すことがある。
        # ブラウザが <img> を読む時と同じヘッダ構成にする。
        parsed  = urllib.parse.urlparse(url)
        segs    = [s for s in parsed.path.split("/") if s]
        referer = f"{parsed.scheme}://{parsed.hostname}/"
        if segs:
            referer += segs[0] + "/"
        hdr = {
            "Referer":        referer,
            "Accept":         "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
            # 画像取得ではナビゲーション用ヘッダを送らない（None で除去）
            "Upgrade-Insecure-Requests": None,
        }
        attempts = 3 if retry_404 else 1
        last_err = None
        for i in range(attempts):
            try:
                r = self.session.get(url, headers=hdr, timeout=self.timeout)
                # スレ落ち直後の一時的 404 → 間隔を空けて再試行
                if r.status_code == 404 and retry_404 and i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                    continue
                r.raise_for_status()
                data = r.content
                self._save_img_cache(url, data)
                return data
            except Exception as e:
                last_err = e
                if retry_404 and i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                    continue
                break
        print(f"[Fetch] 画像エラー [{url}]: {last_err}")
        return None

    # ── 本画像の先読みキャッシュ ──────────────────────────────────────────────
    # スレ表示中に本画像(/src/)をディスクキャッシュへ先読みしておく。これにより、
    # スレ落ち時の自動保存で「未閲覧の画像が既にサーバから消えて404」になる欠落を防ぐ。
    # （スレ落ち時点でふたばは画像ファイルも削除するため、保存時のDLでは間に合わない）
    def prefetch_images(self, urls, group: str = "") -> None:
        """本画像URLのリストを低優先・低並列で先読みしてディスクへ保存する。
        ・既にメモリ/ディスクにある画像、投入済みURLはスキップ
        ・取得失敗は黙殺（先読みのためログを汚さない）
        ・group（スレURL等）を渡すと cancel_prefetch(group) で未着手分を一括中断できる"""
        if not urls:
            return
        # 投入済みセットが肥大化しすぎたらクリア（長時間運用の保険）
        if len(self._prefetch_seen) > 20000:
            self._prefetch_seen.clear()
        # グループのキャンセルトークン（イベント）。既存が無い/既にセット済み
        # （前回キャンセル済み）なら作り直して新規バッチを有効化する。
        ev = None
        if group:
            ev = self._prefetch_cancel.get(group)
            if ev is None or ev.is_set():
                ev = threading.Event()
                self._prefetch_cancel[group] = ev
        pool = self._get_prefetch_pool()
        for url in urls:
            if not url or url in self._prefetch_seen or url in self._img_cache:
                continue
            self._prefetch_seen.add(url)
            try:
                pool.submit(self._prefetch_one, url, ev)
            except Exception:
                pass

    def cancel_prefetch(self, group: str) -> None:
        """指定グループ（スレURL等）の先読みを中断する。
        キュー待ちの未着手タスクは _prefetch_one 冒頭でスキップされ、実行中の1件も
        チャンク間で中断される。タブを閉じた時などに呼ぶ。"""
        if not group:
            return
        ev = self._prefetch_cancel.pop(group, None)
        if ev is not None:
            ev.set()

    def shutdown_prefetch(self) -> None:
        """全ての先読みを中断してプールを畳む（アプリ終了時に呼ぶ）。
        未着手タスクをキャンセルし実行中DLも中断させることで、終了処理中に
        バックグラウンドのネットワーク/ディスクI/Oが走り続けるのを止め、
        プロセス終了を速める。"""
        for ev in list(self._prefetch_cancel.values()):
            try:
                ev.set()
            except Exception:
                pass
        self._prefetch_cancel.clear()
        pool = self._prefetch_pool
        self._prefetch_pool = None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:   # Python 3.8 以前は cancel_futures 未対応
                pool.shutdown(wait=False)
            except Exception:
                pass

    def _get_prefetch_pool(self):
        if self._prefetch_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._prefetch_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="imgprefetch")
        return self._prefetch_pool

    def _prefetch_one(self, url: str, cancel=None) -> None:
        """1枚を先読みしてディスクへ保存（メモリキャッシュは汚さない）。失敗は黙殺。"""
        # キャンセル済み（タブを閉じた等）なら取得せずにスキップ。
        # 投入済みセットから外し、再表示時に再投入できるようにする。
        if cancel is not None and cancel.is_set():
            self._prefetch_seen.discard(url)
            return
        try:
            p = self._img_disk_path(url)
            if p.exists():
                return
            parsed  = urllib.parse.urlparse(url)
            segs    = [s for s in parsed.path.split("/") if s]
            referer = f"{parsed.scheme}://{parsed.hostname}/"
            if segs:
                referer += segs[0] + "/"
            hdr = {
                "Referer":        referer,
                "Accept":         "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": None,
            }
            # ストリーミング取得し、チャンク間でキャンセルを確認して即中断する
            # （閉じた瞬間に実行中の大きな画像DLも止める）。
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".{threading.get_ident()}.part")
            cancelled = False
            with self.session.get(url, headers=hdr, stream=True,
                                  timeout=self.timeout) as r:
                if not r.ok:
                    return
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
                        if chunk:
                            f.write(chunk)
            if cancelled:
                try: tmp.unlink(missing_ok=True)
                except OSError: pass
                self._prefetch_seen.discard(url)   # 再表示で再投入可能に
                return
            tmp.replace(p)   # 完了後にアトミックに本パスへ
            # サーバ絞り対策: 連続DLの間隔を空け、表示系（スレHTML取得・サムネ
            # 読み込み）と同一サーバの接続/帯域を占有し続けないようにする。
            # ディスクキャッシュ済みで exists スキップした場合は待たない。
            import time as _time
            if cancel is None or not cancel.is_set():
                _time.sleep(0.3)
        except Exception:
            try: tmp.unlink(missing_ok=True)
            except (OSError, NameError, UnboundLocalError): pass

    def _img_disk_path(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)
        return IMAGE_CACHE_DIR / (parsed.hostname or "unknown") / parsed.path.lstrip("/")

    def _store_img_cache(self, url: str, data: bytes) -> None:
        # バイト数主体のLRU（件数上限は副次）。フルサイズ画像を大量に抱えて
        # RSSが膨らむのを防ぐため、合計バイト数で上限を設ける。
        old = self._img_cache.pop(url, None)
        if old is not None:
            self._img_cache_bytes -= len(old)
        self._img_cache[url] = data
        self._img_cache_bytes += len(data)
        while self._img_cache and (
                len(self._img_cache) > IMAGE_CACHE_MAX
                or self._img_cache_bytes > IMAGE_CACHE_MAX_BYTES):
            _k, _v = next(iter(self._img_cache.items()))
            del self._img_cache[_k]
            self._img_cache_bytes -= len(_v)

    def _save_img_cache(self, url: str, data: bytes) -> None:
        self._store_img_cache(url, data)
        try:
            p = self._img_disk_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except Exception as e:
            print(f"[ImgCache] 保存エラー: {e}")


