"""
futaba2b_html.py ─ スレッド/カタログを HTML 文字列に変換する
PySide6 + QtWebEngine 方式で使用する。
"""
from __future__ import annotations
import html as _html
import re
import urllib.parse
import datetime as _dt
import warnings
from typing import TYPE_CHECKING

# レス本文が ">>1782546435472.jpg" 等のファイル名風だけの場合、BeautifulSoup が
# 「HTMLよりファイル名に見える」と推測して MarkupResemblesLocatorWarning を出す。
# パース自体は正常で実害が無いため抑止する（ログ汚染防止）。
try:
    from bs4 import MarkupResemblesLocatorWarning as _MRLW
    warnings.filterwarnings("ignore", category=_MRLW)
except Exception:
    pass

if TYPE_CHECKING:
    from futaba2b_models import ResData, ThreadData, CatalogEntry

# ══════════════════════════════════════════════════════════════════════════════
# CSS / JS テンプレート
# ══════════════════════════════════════════════════════════════════════════════

THREAD_CSS = """
/* ─ CSS変数 (テーマ差し替え可) ─ */
:root {
  --body-bg:            #FFFFEE;
  --body-fg:            #7B0004;
  --op-bg:              #FFFFEE;
  --reply-bg:           #F0E0D6;
  --new-res-border:     #cc1105;
  --self-res-border:    #1a6fd4;
  --divider-fg:         #cc1105;
  --divider-bg:         #fff0f0;
  --link-color:         #0000EE;
  --link-hover:         #DD0000;
  --quote-color:        #789922;
  --name-color:         #117743;
  --subject-color:      #cc1105;
  --date-color:         #800000;
  --no-color:           #800000;
  --no-hover:           #DD0000;
  --sod-color:          #800000;
  --footer-color:       #888888;
  --footer-border:      #dddddd;
  --thumb-border:       #aaaaaa;
  --thumb-hover-border: #800000;
  --expiry-color:       #cc0000;
  --expiry-bg:          #fff8f8;
  --del-reason-color:   #cc1105;
  --del-content-color:  #800000;
  --comment-color:      #7B0004;
  --id-popup-bg:        #FFFFEE;
  --id-popup-border:    #800000;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: var(--body-bg);
    font-family: "Meiryo", "MS PGothic", sans-serif;
    color: var(--body-fg);
    padding: 4px;
}

/* ─ OP ─ */
.res.op {
    background: var(--op-bg);
    position: relative;
    padding: 6px 8px 4px 8px;
    margin: 0 0 4px 16px;
}
/* OP サムネイル: 左フロート → OP div 外に逃がして返信が隣に並ぶ */
.res.op .thumb {
    float: left;
    margin-right: 14px;
    margin-bottom: 6px;
}
/* OP テキスト列: BFC + fit-content → フロートを避け幅をコンテンツに合わせる
   これにより footer の text-align:right がテキスト列の右端に収まる */
.op-text {
    display: flow-root;     /* BFC: フロートを回避して右側に収まる（overflowクリップなし） */
    width: fit-content;     /* テキスト列の幅をコンテンツ幅に制限 */
    min-width: 200px;
}

/* ─ 新着レス ─ */
.res.reply.new-res  { border-left: 3px solid var(--new-res-border); }
/* ─ 自分のレス ─ */
.res.reply.self-res { border-left: 3px solid var(--self-res-border); }
/* ─ 新着仕切り線 ─ */
.new-res-divider {
    clear: both;
    margin: 4px 0 2px 0;
    padding: 2px 0;
    text-align: center;
    font-size: 8pt;
    color: var(--divider-fg);
    border-top: 1px solid var(--divider-fg);
    border-bottom: 1px solid var(--divider-fg);
    background: var(--divider-bg);
    user-select: none;
}
/* op-text内末尾（画像右コンテンツの下）ではclearなし・幅いっぱい */
.op-text .new-res-divider {
    clear: none;
    display: block;
    width: 100%;
    margin: 6px 0 0 0;
}

/* ─ 表示モード (一覧/画像/引用) ─ */
[data-mode="image"] .res:not(.has-img) { display: none !important; }
[data-mode="image"] .res.op            { display: block !important; }
[data-mode="quote"] .res:not(.has-qt)  { display: none !important; }
[data-mode="quote"] .res.op            { display: block !important; }

/* ─ 削除レスは初期非表示 ─ */
.res.reply.deleted { display: none; }
body.show-deleted .res.reply.deleted { display: block; }

/* ─ レス: BFC + fit-content でフロートを回避して隣に並ぶ ─ */
.res.reply {
    display: flow-root;
    position: relative;
    background: var(--reply-bg);
    width: fit-content;
    min-width: 260px;
    max-width: 100%;
    padding: 3px 10px 3px 6px;
    margin: 1px 0 3px 16px;
}
/* コメントを「無念 Name」の間くらいにインデント */
.res.reply .content { padding-left: 50px; }
/* スレ末尾でフロートをクリア */
.thread-end { clear: both; height: 4px; }

/* ─ ファイル情報 ─ */
.file-info { font-size: 9pt; margin-bottom: 4px; }
.file-info a { color: var(--link-color); text-decoration: none; }
a.fi-inline { font-size: 9pt; color: var(--link-color); text-decoration: none; margin: 0 6px; }
a.fi-inline:hover { text-decoration: underline; color: var(--link-hover); }
.file-info a:hover { text-decoration: underline; color: var(--link-hover); }
/* 返信レスのファイル情報行（ヘッダー下に独立表示） */
.fi-sub { font-size: 9pt; margin: 1px 0 2px 20px; }
.fi-sub a { color: var(--link-color); text-decoration: none; }
.fi-sub a:hover { text-decoration: underline; color: var(--link-hover); }

/* ─ ヘッダー行 ─ */
.header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0 2px;
    margin-bottom: 3px;
    line-height: 1.9;
    position: relative;
}
.rsc    { color: var(--no-color); font-size: small; margin-right: 4px; min-width: 18px; text-align: right; }
/* ▼ 被引用インジケータ / … 被引用なし: レス枠の左外側に配置（位置は連番の左側のまま） */
.quote-ind {
    position: absolute;
    left: -16px;
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    text-align: center;
    color: var(--no-color);
    font-size: small;
    cursor: pointer;
    user-select: none;
}
.quote-ind:hover { color: var(--no-hover); }
.quote-ind.no-quote { cursor: default; opacity: 0.4; }
.quote-ind.no-quote:hover { color: var(--no-color); }
/* サブジェクト: style4.css .csb と同一 */
.csb    { color: var(--subject-color); font-weight: bold; margin: 0 5px; }
/* "Name" テキスト: body text color に合わせる */
.csb-nm { color: var(--no-color); margin: 0 2px; }
/* 名前: style4.css .cnm と同一 (緑太字) */
.nm     { color: var(--name-color); font-weight: bold; margin: 0 5px; }
a.nm    { color: var(--name-color); font-weight: bold; margin: 0 5px; text-decoration: none; }
a.nm:hover { text-decoration: underline; }
.trip   { color: var(--name-color); }
/* メアドバッジ: [sage] / [email内容] */
.email-badge { color: var(--name-color); font-size: 9pt; margin: 0 2px; }
/* 日時: body text color に合わせる */
.dt     { color: var(--date-color); margin: 0 5px; }
/* No.: body text color + underline (style4.css .cno 準拠) */
.no     { color: var(--no-color); text-decoration: underline; cursor: pointer; margin: 0 5px; }
.no:hover { color: var(--no-hover); }
.del    { color: var(--no-color); text-decoration: underline;
          cursor: pointer; margin: 0 3px; }
.del:hover { color: var(--no-hover); }
.sod    { color: var(--sod-color); background: transparent; border: none;
          font-size: 100%; cursor: pointer; padding: 0 8px; margin: 0 8px; font-family: inherit; white-space: nowrap; }
.sod:hover { text-decoration: underline; }
.expiry { color: var(--expiry-color); font-size: small; margin-left: 4px; }
.expiry-banner {
    color: var(--expiry-color); font-weight: bold; text-align: center;
    padding: 6px 12px; margin: 8px 4px 2px;
    border: 1px solid var(--expiry-color); border-radius: 3px;
    background: var(--expiry-bg); font-size: 9pt;
}

/* ─ コンテンツ ─ */
.content {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin: 2px 0 3px 0;
}
/* flexレイアウト(.content)内でコメントに押されてサムネが実寸より
   縮小されないようにする（ふたば本家と同様、サムネは常に実寸表示） */
.thumb { flex-shrink: 0; }
.thumb img {
    cursor: pointer;
    display: block;
    border: 1px solid var(--thumb-border);
    max-width: 100%;
    height: auto;
}
.thumb img:hover { border-color: var(--thumb-hover-border); }
/* ─ 動画サムネイル: インライン再生 ─ */
.thumb video {
    display: block;
    border: 1px solid var(--thumb-border);
    max-width: 300px;
    max-height: 200px;
    background: #000;
}
.thumb video:hover { border-color: var(--thumb-hover-border); }
/* OP サムネのスタイルは thumb img / video で共有 */
.comment {
    font-size: medium;
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.6;
    color: var(--comment-color);
}
.comment .qt  { color: var(--quote-color); cursor: pointer; }
.comment .qt a { color: var(--quote-color); text-decoration: none; }
.comment .qt a:hover { color: var(--quote-color); text-decoration: none; }
.comment .del { color: #FF0000; }
/* ─ 削除レスの削除理由ラベル ─ */
.del-reason { color: var(--del-reason-color); font-size: 8pt; font-style: italic; }
.del-content { color: var(--del-content-color); font-size: 8pt; margin-top: 2px; white-space: pre-wrap; }
.comment a    { color: var(--link-color); }
.comment a:hover { color: var(--link-hover); text-decoration: underline; }
.yt-thumb {
    max-width: 320px; max-height: 180px; display: block;
    margin: 4px 0; cursor: pointer; border: 1px solid var(--thumb-border);
}
.yt-thumb:hover { border-color: var(--subject-color); }
.ul-link { color: var(--link-color); text-decoration: underline; font-weight: bold; }
.ul-link:hover { color: var(--subject-color); }
/* うｐロダ直リン画像サムネイル */
.ul-img-wrap { display: inline-block; vertical-align: top; margin: 2px 4px; }
.ul-fname    { display: block; font-size: 8pt; color: var(--link-color);
               word-break: break-all; max-width: 200px; margin-bottom: 2px; }
.ul-thumb    { display: block; max-width: 200px; max-height: 160px;
               border: 1px solid #888; cursor: pointer; }
.ul-thumb:hover { opacity: 0.85; }

/* ─ フッター ─ */
.footer {
    text-align: right; font-size: 9pt; color: var(--footer-color);
    margin-top: 2px; border-top: 1px dashed var(--footer-border); padding-top: 1px;
}
.footer a { color: var(--footer-color); text-decoration: underline; cursor: pointer; }
.footer a:hover { color: var(--link-hover); }
.footer a.ng { color: var(--no-color); }
.footer a.ng:hover { color: var(--no-hover); }

/* ─ ID ホバーポップアップ ─ */
.id-popup {
    display: none;
    position: fixed;
    background: var(--id-popup-bg);
    border: 1px solid var(--id-popup-border);
    padding: 4px 8px;
    max-width: 420px;
    max-height: 320px;
    overflow-y: auto;
    z-index: 9999;
    box-shadow: 2px 2px 6px rgba(0,0,0,0.4);
    font-size: 8pt;
}
.id-popup-hdr {
    font-weight: bold; color: var(--id-popup-border);
    border-bottom: 1px solid #c8a890; margin-bottom: 3px; padding-bottom: 2px;
}
.id-popup-item {
    color: var(--body-fg); padding: 2px 0;
    border-bottom: 1px dashed var(--footer-border);
    white-space: pre-wrap; word-break: break-all;
}
.id-popup-item:last-child { border-bottom: none; }

/* ─ 動画サムネイル: クリックで再生 ─ */
.video-thumb { position: relative; display: inline-block; cursor: pointer; }
.play-btn {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 80px; height: 80px;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.5);
    display: flex; align-items: center; justify-content: center;
    box-sizing: border-box; padding-left: 6px;
    color: #fff; font-size: 42px; line-height: 1;
    pointer-events: none;
}
.video-thumb:hover img { border-color: var(--thumb-hover-border); }
.video-thumb:hover .play-btn {
    background: rgba(0, 0, 0, 0.65);
    color: var(--reply-bg);
}
/* ─ レス抽出パネル ─ */
.extract-panel {
    display: none;
    position: fixed;
    top: 10px; right: 10px;
    width: 520px;
    max-height: 80vh;
    overflow-y: auto;
    background: var(--id-popup-bg);
    border: 2px solid var(--id-popup-border);
    z-index: 8888;
    box-shadow: 4px 4px 10px rgba(0,0,0,0.5);
}
.extract-header {
    background: #E04000;
    color: white;
    padding: 4px 8px;
    font-weight: bold;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky; top: 0;
}
.extract-header button {
    background: transparent; border: 1px solid white;
    color: white; cursor: pointer; padding: 0 6px; font-size: 12pt;
}
/* ─ ID ─ */
/* ID表示 ───────────────────────────────────────────────────────────────── */
/* デフォルト: グレー（IDが出るはずのないスレで出ている場合） */
.post-id            { cursor: pointer; margin: 0 3px; }
.post-id:hover      { text-decoration: underline; color: var(--no-hover); }
/* 「id表示」メール欄のスレ: ボディと同じ色（CSS変数で上書き可） */
body.id-board .post-id   { color: var(--id-normal-color, #444); }
/* OP にIDがないのにIDが出ているスレ: 赤太字 */
body.op-no-id .post-id   { color: #cc0000; font-weight: bold; }
/* ID件数バッジ */
.post-id-prefix { }                                   /* "ID:" 部分 */
.post-id-value  { }                                   /* "AABBCCDD" 部分 */
.post-id-count  { color: var(--footer-color); font-size: 8pt; }     /* "[54]" 部分 */
/* ID出現回数が閾値以上のレス: ID全体を赤太字（[N]含む）。既存のIDボードCSSを上書き */
.post-id.post-id-warn,
body.id-board .post-id.post-id-warn,
body.op-no-id .post-id.post-id-warn {
    color: var(--id-warn-color, #ff0000) !important;
    font-weight: bold;
}
.post-id.post-id-warn .post-id-count { color: var(--id-warn-color, #ff0000); }

/* ─ NGレス（非表示） ─ */
.res.ng-hidden {
    display: none !important;
}
/* ─ NGレス（NGワード/NG画像）: 表示された時に左へ緑の帯 ─ */
.res.ng-band {
    border-left: 4px solid #1f9d1f;
    padding-left: 5px;
}
/* ─ del（削除依頼/記事削除）済みレスの目印 ─ */
.del-done {
    color: #cc1105;
    font-weight: bold;
    font-size: 8pt;
    margin-left: 4px;
}
/* ─ NGレス（折りたたみ表示） ─ */
.res.ng-collapsed .content,
.res.ng-collapsed .footer,
.res.ng-collapsed .file-info {
    display: none;
}
.res.ng-collapsed .header::after {
    content: " [NG]";
    color: #aaa;
    font-size: 8pt;
}
.res.ng-collapsed .header {
    cursor: pointer;
    opacity: 0.5;
}
/* ─ NG画像（半透明表示 + クリックで展開） ─ */
.res.ng-image .thumb img {
    opacity: 0.05;
    filter: blur(4px);
    cursor: pointer;
    transition: opacity 0.2s, filter 0.2s;
}
.res.ng-image .thumb img:hover {
    opacity: 0.3;
}
.res.ng-image.ng-image-revealed .thumb img {
    opacity: 1 !important;
    filter: none !important;
}
/* ─ 抽出（スレ内絞り込み）で非表示にするレス ─ */
.res._ext_hide { display: none !important; }
.page-footer {
    font-size: 8pt; color: var(--footer-color); padding: 6px 8px 4px;
    border-top: 1px solid var(--footer-border); margin-top: 8px;
    text-align: right;
}
"""

CATALOG_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #FFFFEE;
    font-family: "Meiryo", "MS PGothic", sans-serif;
    font-size: 8pt;
    padding: 6px;
}
#grid {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 3px;
    width: 100%;
}
.entry {
    display: inline-flex; flex-direction: column;
    border: 1px solid #800000; overflow: hidden;
    background: #FFF; cursor: pointer;
    width: var(--cell-w, 84px); vertical-align: top;
    flex-shrink: 0;
}
.entry:hover { border-color: #DD0000; box-shadow: 1px 1px 3px #aaa; }
.entry-img {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; min-height: 30px; background: #eeeee0;
    position: relative;
}
/* メール欄/IDバッジ（サムネ右上の小アイコン） */
.cat-badge {
    position: absolute; top: 1px; right: 1px;
    font-size: 7pt; font-weight: bold; line-height: 1;
    color: #fff; padding: 1px 2px; border-radius: 2px;
    pointer-events: none; z-index: 2;
}
.cat-badge-g { background: #2E7D32; }   /* 緑: ID表示/IP表示/・3・/他 */
.cat-badge-r { background: #cc1105; }   /* 赤: 非ID表示でID実在 */
/* 隔離バッジ（サムネ右下・オレンジ） */
.cat-badge-quar {
    position: absolute; bottom: 1px; right: 1px; top: auto;
    font-size: 7pt; font-weight: bold; line-height: 1;
    color: #fff; background: #E08000; padding: 1px 2px; border-radius: 2px;
    pointer-events: none; z-index: 2;
}
.entry img {
    display: block;
    max-width: calc(var(--cell-w, 84px) - 4px);
    max-height: calc(var(--cell-w, 84px) - 4px);
}
/* 文字のみスレ（サムネなし） */
.entry.text-only .entry-img::after {
    content: "文";
    font-size: 16pt; color: #bbb; font-weight: bold;
}
.entry-title {
    font-size: 7pt; color: #7B0004; padding: 1px 3px;
    white-space: normal; overflow: hidden;
}
.entry-foot {
    display: flex; justify-content: space-between;
    font-size: 6pt; color: #7B0004; font-weight: bold;
    padding: 0 3px 2px; line-height: 1.5;
}
.res-new { color: #cc1105; font-weight: bold; }
.entry.red-thread { border: 2px solid #cc1105; }
.entry.red-thread .entry-title { color: #cc1105; font-weight: bold; }
/* 検索セクション: flex と grid 両対応 */
.sec-hdr {
    flex-basis: 100%; width: 100%;
    grid-column: 1 / -1;
    text-align: center;
    color: #00008B; font-size: 9pt; font-weight: bold;
    padding: 3px 6px; background: #F0F0FF;
    border-bottom: 2px solid #0000CC;
    margin-bottom: 2px;
}
.sec-div {
    flex-basis: 100%; width: 100%;
    grid-column: 1 / -1;
    border-top: 2px solid #0000CC;
    margin: 4px 0;
}
/* 隔離スレ セクション見出し（mode=json に無いスレ） */
.sec-hdr.quar-hdr {
    color: #8a4b00; background: #FFF3E0;
    border-bottom: 2px solid #E08000;
}
/* 共通ID(mode=json id) まとめセクション見出し */
.sec-hdr.cid-hdr {
    color: #8b0000; background: #FFEBEE;
    border-bottom: 2px solid #cc1105;
}
/* ─ 逆NGエントリハイライト ─ */
.entry.reverse-ng {
    border: 2px solid #9B59B6;
    background: rgba(155, 89, 182, 0.08);
}
.entry.reverse-ng .entry-title { color: #6C3483; font-weight: bold; }
/* ─ 仮赤字（残り10%以下）─ */
.entry.quasi-red-thread {
    border: 2px dashed #cc1105;
}
.entry.quasi-red-thread .entry-title { color: #cc1105; }
.entry.quasi-red-thread .entry-foot  { color: #cc1105; }
/* ─ 既読スレ（1度でも閲覧済み）─ */
.entry.already-read { background: #ffe8f0; }
.page-footer {
    font-size: 8pt; color: #888; padding: 4px 8px;
    width: 100%; margin-top: 4px; text-align: right;
}
"""

ID_POPUP_JS = """
// ── ID ホバーポップアップ ──────────────────────────────────────────────────
let _idPopTm = null;
function showIdPopup(id, x, y) {
    clearTimeout(_idPopTm);
    const nodes = document.querySelectorAll('.post-id[data-id="' + id + '"]');
    if (!nodes.length) return;
    let html = ['<div class="id-popup-hdr">ID:' + id + '（' + nodes.length + '件）</div>'];
    nodes.forEach(el => {
        const res = el.closest('.res');
        if (!res) return;
        const hdr = res.querySelector('.header');
        const com = res.querySelector('.comment');
        if (hdr) {
            html.push('<div class="id-popup-item">' +
                hdr.innerText.trim() + '<br>' +
                (com ? com.innerText.trim() : '') +
                '</div>');
        }
    });
    let pop = document.getElementById('_idpop');
    if (!pop) {
        pop = document.createElement('div');
        pop.id = '_idpop'; pop.className = 'id-popup';
        pop.addEventListener('mouseenter', () => clearTimeout(_idPopTm));
        pop.addEventListener('mouseleave', hideIdPopup);
        document.body.appendChild(pop);
    }
    pop.innerHTML = html.join('');
    pop.style.display = 'block';
    // マウス付近に配置（画面外補正）
    requestAnimationFrame(() => {
        const vw = window.innerWidth, vh = window.innerHeight;
        const pw = pop.offsetWidth, ph = pop.offsetHeight;
        let px = x + 14, py = y + 6;
        if (px + pw > vw) px = x - pw - 4;
        if (py + ph > vh) py = y - ph - 4;
        pop.style.left = Math.max(0,px) + 'px';
        pop.style.top  = Math.max(0,py) + 'px';
    });
}
function hideIdPopup() {
    _idPopTm = setTimeout(() => {
        const p = document.getElementById('_idpop');
        if (p) p.style.display = 'none';
    }, 200);
}
"""

WEBCHANNEL_JS = """
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script>

let _delNo = null;
function delRes(no, el) {
    _delNo = no;
    _ensureDelPop();
    // 削除キーを8文字ランダム生成（空欄なら自動入力、すでに入力済みなら維持）
    const pwdEl = document.getElementById('del-pwd');
    if (!pwdEl.value) {
        const chars='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
        let pwd=''; for(let i=0;i<8;i++) pwd+=chars[Math.floor(Math.random()*chars.length)];
        pwdEl.value = pwd;
    }
    document.getElementById('del-img').checked = false;
    document.getElementById('del-hide').checked =
        (typeof window._delHideDefault === 'boolean') ? window._delHideDefault : true;
    // 自分の書き込みか否かでポップアップ内の該当セクションをグレーアウトする（操作は可能）。
    //   自分のレス → 「削除依頼(del)＋非表示」部分をグレー（記事削除がメイン）
    //   他人のレス → 「記事削除」部分をグレー（削除キー不明で使えない。削除依頼がメイン）
    var _resEl = document.getElementById('r' + no);
    var _mine  = !!(_resEl && _resEl.classList.contains('self-res'));
    var _reqsec = document.getElementById('del-reqsec');
    var _dsec   = document.getElementById('del-delsec');
    if (_reqsec) _reqsec.style.background = _mine ? '#d0d0d0' : '';
    if (_dsec)   _dsec.style.background   = _mine ? '' : '#d0d0d0';
    // NGレスなら「どのNGワード/NG画像でNGに入ったか」を最下部に表示
    // （返信モードはレスdiv、引用モードはqt-rowの data-ng-info 属性から取得）
    var _ngsec = document.getElementById('del-ngsec');
    if (_ngsec) {
        var _src  = (el && el.closest) ? el.closest('[data-ng-info]') : null;
        var _info = (_src ? _src.getAttribute('data-ng-info') : '') ||
                    (_resEl ? (_resEl.getAttribute('data-ng-info') || '') : '');
        if (_info) { _ngsec.textContent = _info; _ngsec.style.display = 'block'; }
        else _ngsec.style.display = 'none';
    }
    const pop = document.getElementById('del-pop');
    pop.style.display = 'block';
    const btn = el || event.target;
    const r = btn.getBoundingClientRect();
    // ポップアップが画面右端・下端にはみ出ないよう調整
    const pw = pop.offsetWidth || 220;
    const ph = pop.offsetHeight || 120;
    let left = r.left;
    let top  = r.bottom + 4;
    if (left + pw > window.innerWidth)  left = Math.max(0, window.innerWidth - pw - 4);
    if (top  + ph > window.innerHeight) top  = Math.max(0, r.top - ph - 4);
    pop.style.left = left + 'px';
    pop.style.top  = top  + 'px';
}
function _ensureDelPop() {
    if (document.getElementById('del-pop')) return;
    const pop = document.createElement('div');
    pop.id = 'del-pop';
    pop.style.cssText = 'display:none;position:fixed;border:2px solid #800000;background:#FFFFEE;padding:8px;z-index:9999;box-shadow:2px 2px 6px rgba(0,0,0,.3);min-width:220px;font-size:9pt;';
    pop.innerHTML =
        '<div id="del-reqsec" style="padding:3px;border-radius:3px;">'
      +   '<div style="margin-bottom:4px;">'
      +     '<span onclick="delReport()" style="color:#800000;text-decoration:underline;cursor:pointer;font-size:9pt;">削除依頼(del)</span>'
      +   '</div>'
      +   '<label style="display:block;margin:3px 0;font-size:8pt;">'
      +     '<input id="del-hide" type="checkbox"> delしたレスを非表示にする'
      +   '</label>'
      + '</div>'
      + '<div id="del-delsec" style="padding:3px;border-radius:3px;">'
      +   '<hr style="border:none;border-top:1px solid #ccc;margin:4px 0">'
      +   '<div style="margin:3px 0;">'
      +     '削除キー&nbsp;<input id="del-pwd" type="password" size="10" style="border:1px inset #aaa;padding:1px">'
      +   '</div>'
      +   '<div style="display:flex;align-items:center;gap:6px;margin-top:4px;">'
      +     '<label style="font-size:8pt;white-space:nowrap;">'
      +       '<input id="del-img" type="checkbox"> 画像だけ'
      +     '</label>'
      +     '<button onclick="delArticle()" style="flex:1;background:#F0E0D6;border:1px solid #800;padding:3px;cursor:pointer;font-size:9pt;">記事削除</button>'
      +     '<button onclick="delClose()" style="padding:3px 8px;cursor:pointer;font-size:9pt;">×</button>'
      +   '</div>'
      + '</div>'
      + '<div id="del-ngsec" style="display:none;margin-top:5px;padding:3px;'
      +   'border-top:1px dashed #b99;color:#1f9d1f;font-size:8pt;'
      +   'max-width:260px;word-break:break-all;"></div>';
    document.body.appendChild(pop);
    document.addEventListener('click', function(e){
        const pop2 = document.getElementById('del-pop');
        if (pop2 && !pop2.contains(e.target)
            && !e.target.classList.contains('no')
            && !e.target.classList.contains('qt-no'))
            pop2.style.display = 'none';
    });
}
function delReport() {
    if (_delNo===null) return;
    const hide = document.getElementById('del-hide').checked;
    _b('reportDel', [_delNo, hide]);
    document.getElementById('del-pop').style.display='none';
}
function delArticle() {
    if (_delNo===null) return;
    const pwd  = document.getElementById('del-pwd').value;
    const img  = document.getElementById('del-img').checked;
    const hide = document.getElementById('del-hide').checked;
    _b('deleteRes', [_delNo, pwd, img, hide]);
    document.getElementById('del-pop').style.display='none';
}
function delClose() { const p=document.getElementById('del-pop'); if(p) p.style.display='none'; }
function showDelMsg(msg) {
    let el=document.getElementById('_delmsg');
    if(!el){ el=document.createElement('div'); el.id='_delmsg';
      el.style.cssText='display:none;position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.75);color:#fff;padding:6px 16px;border-radius:4px;z-index:99999;font-size:10pt;';
      document.body.appendChild(el); }
    el.textContent=msg; el.style.display='block';
    setTimeout(function(){ el.style.display='none'; }, 2000);
}
var bridge = null;
document.addEventListener('DOMContentLoaded', function() {
    if (typeof QWebChannel !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function(ch) {
            bridge = ch.objects.bridge;
        });
    }
});
function _b(method, args) {
    if (bridge && bridge[method]) bridge[method].apply(bridge, args);
}
function quoteNo(no)            { _b('quoteNo',       [no]); }
function quoteComment(no)       { _b('quoteComment',  [no]); }
function quoteImg(no)           { _b('quoteImg',       [no]); }
function ngRes(no)              { _b('ngRes',          [no]); }
function sodane(no)             { _b('sodane',         [no]); }
/* NG画像をクリックすると reveal クラスを付けて表示 */
document.addEventListener('click', function(e) {
    var img = e.target;
    if (img.tagName !== 'IMG') return;
    var res = img.closest && img.closest('.res.ng-image');
    if (!res) return;
    if (res.classList.contains('ng-image-revealed')) return;
    e.preventDefault(); e.stopPropagation();
    res.classList.add('ng-image-revealed');
});
/* スレッド内画像の右クリック → NG画像登録メニュー */
document.addEventListener('contextmenu', function(e) {
    var img = e.target;
    if (img.tagName !== 'IMG') return;
    var res = img.closest && img.closest('.res');
    if (!res) return;
    e.preventDefault(); e.stopPropagation();
    var old = document.getElementById('__img_ctx');
    if (old) old.parentNode.removeChild(old);
    /* フル画像URLを優先、なければsrc */
    var imgUrl = img.getAttribute('data-full') || img.src || '';
    if (!imgUrl) return;
    var menu = document.createElement('div');
    menu.id = '__img_ctx';
    menu.style.cssText = 'position:fixed;background:#fff;border:1px solid #999;'
        + 'padding:2px 0;z-index:19998;box-shadow:2px 2px 4px rgba(0,0,0,.3);font-size:9pt;';
    menu.style.left = e.clientX + 'px';
    menu.style.top  = e.clientY + 'px';
    function addItem(label, fn) {
        var item = document.createElement('div');
        item.textContent = label;
        item.style.cssText = 'padding:4px 16px;cursor:pointer;white-space:nowrap;';
        item.onmouseenter = function(){ this.style.background='#0078d7';this.style.color='#fff'; };
        item.onmouseleave = function(){ this.style.background='';this.style.color=''; };
        item.onclick = function(){ fn(); document.body.removeChild(menu); };
        menu.appendChild(item);
    }
    addItem('外部ブラウザで開く', function(){ _b('openUrlExternal',[imgUrl]); });
    addItem('この画像をNG登録する', function(){ _b('ngImage',[imgUrl]); });
    addItem('画像URLをコピーする',  function(){
        try{navigator.clipboard.writeText(imgUrl);}catch(er){}
    });
    document.body.appendChild(menu);
    setTimeout(function(){
        document.addEventListener('click', function cleanup(){
            var m = document.getElementById('__img_ctx');
            if (m) m.parentNode.removeChild(m);
            document.removeEventListener('click', cleanup);
        });
    }, 0);
});
function openImg(url, idx)      { _b('openImg',        [url, idx]); }
function openImgBg(url, idx)    { _b('openImgBg',      [url, idx]); }
/* 画像モードのギャラリークリック。モード切替をページ再読込せずDOM入替で
   行うため、画像モード以外のページにも定義しておく（未定義だとbody入替後の
   グリッドクリックが失敗する）。 */
function openGalleryImg(i)      { _b('openGalleryImg', [i]); }
function openUrl(url)           { _b('openUrl',        [url]); }
function openThread(url)        { _b('openThread',     [url]); }
function openThreadBg(url)      { _b('openThreadBg',  [url]); }
/* フッターの「動画」リンク: VideoPlayerWindow で再生 */
function playVideoInline_footer(url) { _b('playVideo', [url]); }
function handleCatClick(url, e) {
    if(e.shiftKey){ e.preventDefault(); openThreadBg(url); return; }
    openThread(url);
}
function handleCatMouseDown(url, e) {
    if(e.button===1){ e.preventDefault(); openThreadBg(url); return; }
    if(e.button===0 && e.ctrlKey){ e.preventDefault(); openThreadBg(url); }
}
function addThreadNg(url) { _b('addThreadNg', [url]); }
function catalogDel(url)   { _b('catalogDel',  [url]); }
document.addEventListener('contextmenu', function(e) {
    var el = e.target.closest('.entry');
    if (!el) return;
    e.preventDefault();
    // 既存メニューがあれば先に削除（右クリックのたびに増殖するのを防ぐ）
    var old = document.getElementById('__ng_ctx');
    if (old) old.parentNode.removeChild(old);
    var url = null;
    var oc = el.getAttribute('onclick') || '';
    var m = oc.match(/handleCatClick\\('([^']+)'/);
    if (m) url = m[1];
    if (!url) return;
    var menu = document.createElement('div');
    menu.id = '__ng_ctx';
    menu.style.cssText = 'position:fixed;background:#fff;border:1px solid #999;'
        + 'padding:2px 0;z-index:9999;box-shadow:2px 2px 4px rgba(0,0,0,.3);font-size:9pt;';
    menu.style.left = e.clientX + 'px';
    menu.style.top  = e.clientY + 'px';
    function addMenuItem(label, fn) {
        var item = document.createElement('div');
        item.textContent = label;
        item.style.cssText = 'padding:4px 16px;cursor:pointer;white-space:nowrap;';
        item.onmouseenter = function(){ this.style.background='#0078d7';this.style.color='#fff'; };
        item.onmouseleave = function(){ this.style.background='';this.style.color=''; };
        item.onclick = function(){ fn(); document.body.removeChild(menu); };
        menu.appendChild(item);
    }
    addMenuItem('外部ブラウザで開く', function(){ openUrl(url); });
    addMenuItem('URLをコピーする',    function(){
        if(typeof _b==='function') _b('copyToClipboard',[url]);
    });
    var sep = document.createElement('div');
    sep.style.cssText = 'border-top:1px solid #ddd;margin:2px 0;';
    menu.appendChild(sep);
    var item = document.createElement('div');
    item.textContent = 'このスレをNGにする';
    item.style.cssText = 'padding:4px 16px;cursor:pointer;white-space:nowrap;';
    item.onmouseenter = function(){ this.style.background='#0078d7';this.style.color='#fff'; };
    item.onmouseleave = function(){ this.style.background='';this.style.color=''; };
    item.onclick = function(){ addThreadNg(url); document.body.removeChild(menu); };
    menu.appendChild(item);
    var itemDel = document.createElement('div');
    itemDel.textContent = '削除依頼(del)';
    itemDel.style.cssText = 'padding:4px 16px;cursor:pointer;white-space:nowrap;color:#a00;';
    itemDel.onmouseenter = function(){ this.style.background='#a00';this.style.color='#fff'; };
    itemDel.onmouseleave = function(){ this.style.background='';this.style.color='#a00'; };
    itemDel.onclick = function(){
        catalogDel(url);
        if (el && el.parentNode) el.parentNode.removeChild(el);  /* カタログから即除去 */
        document.body.removeChild(menu);
    };
    menu.appendChild(itemDel);
    document.body.appendChild(menu);
    function cleanup(){ var m=document.getElementById('__ng_ctx'); if(m) m.parentNode.removeChild(m); }
    setTimeout(function(){ document.addEventListener('click', cleanup, {once:true}); }, 0);
});
function updateSodane(no, cnt) {
    // ポップアップは元レスの innerHTML を複製するため id="sodNNN" が重複する。
    // getElementById だと本体側1個しか取れずポップアップが更新されないので、
    // querySelectorAll で同id要素（本体＋表示中の全ポップアップ）を一括更新する。
    var txt = cnt > 0 ? 'そうだねx' + cnt : '+';
    var els = document.querySelectorAll('#sod' + no);
    for (var i = 0; i < els.length; i++) els[i].textContent = txt;
}
/* ── 引用ポップアップ: ThreadView._inject_popup_js() で後付け注入 ── */
function showPopup(no, x, y) { /* injected after load */ }
function hidePopup()         { /* injected after load */ }
function toggleDeleted() {
    var body = document.body;
    var link = document.getElementById('del-toggle');
    if (body.classList.contains('show-deleted')) {
        body.classList.remove('show-deleted');
        if (link) link.textContent = '見る';
    } else {
        body.classList.add('show-deleted');
        if (link) link.textContent = '隠す';
    }
}
/* ─ ▼ 被引用インジケータ: 引用されたレスの通し番号左に挿入 ─ */
/* 引用マップ構築を関数化（画像モードのギャラリーセルからも再利用するため）。
   quotedBy[no] = [引用者No のリスト] を返す。 */
function _computeQuotedBy() {
    var quotedBy = {};
    // 事前計算: 各resの引用除去済みプレーンテキストとURL/ファイル名集を1回だけ作る。
    // テキスト引用・画像名引用の探索でO(n^2)のDOM深クローン/再クエリを避ける。
    var _allRes = Array.from(document.querySelectorAll('.res'));
    var _info = _allRes.map(function(el) {
        var im = (el.id || '').match(/^r(\\d+)$/);
        var no = im ? parseInt(im[1]) : -1;
        var plain = '';
        var c0 = el.querySelector('.comment');
        if (c0) {
            var cl = c0.cloneNode(true);
            cl.querySelectorAll('span.qt').forEach(function(s) { s.remove(); });
            plain = (cl.textContent || '').toLowerCase();
        }
        var urls = '';
        el.querySelectorAll('a[href], img[src]').forEach(function(a) {
            urls += (a.getAttribute('href') || a.getAttribute('src') || '').toLowerCase() + ' ';
        });
        el.querySelectorAll('.ul-fname').forEach(function(s) {
            urls += (s.textContent || '').toLowerCase() + ' ';
        });
        return { no: no, plain: plain, urls: urls };
    });
    var _idxByNo = {};
    _info.forEach(function(o, i) { if (o.no >= 0) _idxByNo[o.no] = i; });
    document.querySelectorAll('.res').forEach(function(el) {
        var m = (el.id || '').match(/^r(\\d+)$/);
        if (!m) return;
        var myNo = parseInt(m[1]);
        /* comment 内の #r{no} リンク */
        el.querySelectorAll('.comment a[href^="#r"]').forEach(function(a) {
            var m2 = (a.getAttribute('href') || '').match(/#r(\\d+)/);
            if (!m2) return;
            var tgt = parseInt(m2[1]);
            if (!quotedBy[tgt]) quotedBy[tgt] = [];
            if (quotedBy[tgt].indexOf(myNo) < 0) quotedBy[tgt].push(myNo);
        });
        /* span.qt で数字引用 + テキスト引用 */
        el.querySelectorAll('.comment span.qt').forEach(function(sp) {
            if (sp.querySelector('a')) return;
            if (sp.dataset && sp.dataset.idRef) return;
            var t = (sp.textContent || '').trim();
            /* 数字引用: >数字 / >No.数字 */
            var m3 = t.match(/^>+(No\\.)?(\\d+)\\s*$/);
            if (m3) {
                var tgt = parseInt(m3[2]);
                if (!quotedBy[tgt]) quotedBy[tgt] = [];
                if (quotedBy[tgt].indexOf(myNo) < 0) quotedBy[tgt].push(myNo);
                return;
            }
            /* テキスト引用: 自分より前のレスの中で引用文を（引用行以外で）含む最近接1件のみ記録
               （事前計算 _info[].plain を使い、DOM深クローンを排除） */
            var q = t.replace(/^>+/, '').trim();
            if (q.length < 2) return;
            var ql = q.toLowerCase();
            var selfIdx = _idxByNo[myNo];
            if (selfIdx === undefined) selfIdx = _info.length;
            var hit = null;
            for (var ri = 0; ri < selfIdx; ri++) {
                if (_info[ri].no < 0) continue;
                if (_info[ri].plain.indexOf(ql) >= 0) hit = _info[ri].no;
            }
            if (hit !== null) {
                if (!quotedBy[hit]) quotedBy[hit] = [];
                if (quotedBy[hit].indexOf(myNo) < 0) quotedBy[hit].push(myNo);
            }
        });
        /* 画像ファイル名引用: span.qt[data-img-ref] → 同名画像を持つ前方レスに ▼
           （事前計算 _info[].urls を使用） */
        el.querySelectorAll('.comment span.qt[data-img-ref]').forEach(function(sp) {
            var fname = (sp.getAttribute('data-img-ref') || '').toLowerCase();
            if (!fname) return;
            for (var ri = 0; ri < _info.length; ri++) {
                var o2 = _info[ri];
                if (o2.no < 0 || o2.no >= myNo) continue;
                if (o2.urls.indexOf(fname) >= 0) {
                    if (!quotedBy[o2.no]) quotedBy[o2.no] = [];
                    if (quotedBy[o2.no].indexOf(myNo) < 0) quotedBy[o2.no].push(myNo);
                }
            }
        });
    });
    return quotedBy;
}
document.addEventListener('DOMContentLoaded', function() {
    var quotedBy = _computeQuotedBy();
    /* 全レスの .header 先頭に ▼(被引用あり) / …(被引用なし) を追加 (inject_popup_js でフックを後付け) */
    document.querySelectorAll('.res').forEach(function(el) {
        var m = (el.id || '').match(/^r(\\d+)$/);
        if (!m) return;
        var no = parseInt(m[1]);
        var qs = quotedBy[no];
        var header = el.querySelector('.header');
        if (!header) return;
        var btn = document.createElement('span');
        if (qs && qs.length) {
            btn.className = 'quote-ind';
            btn.textContent = '▼';
            btn.setAttribute('data-quoters', qs.join(','));
        } else {
            btn.className = 'quote-ind no-quote';
            btn.textContent = '…';
        }
        header.insertBefore(btn, header.firstChild);
    });
});
/* ─ レス抽出 ─ */
function showExtraction(no) {
    var posts = [];
    var target = document.getElementById('r' + no);
    if (target) posts.push('<div style="background:#ffe8e8;border-bottom:1px solid #ddd;">' + target.innerHTML + '</div>');
    document.querySelectorAll('.res.reply').forEach(function(el) {
        if (el.id !== 'r' + no) {
            var links = el.querySelectorAll('a[href="#r' + no + '"]');
            if (links.length > 0) posts.push('<div style="border-bottom:1px solid #ddd;">' + el.innerHTML + '</div>');
        }
    });
    if (posts.length === 0) { alert('No.' + no + ' への引用はありません'); return; }
    var panel = document.getElementById('_extract_panel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = '_extract_panel';
        panel.className = 'extract-panel';
        document.body.appendChild(panel);
    }
    panel.innerHTML = '<div class="extract-header"><span>No.' + no + ' のレス抽出 (' + posts.length + '件)</span><button onclick="closeExtraction()">×</button></div>' + posts.join('');
    panel.style.display = 'block';
}
function closeExtraction() {
    var p = document.getElementById('_extract_panel');
    if (p) p.style.display = 'none';
    // ツールバーの抽出フィールドもクリアする（×で抽出解除）
    if (typeof _b === 'function') { try { _b('clearExtract', []); } catch(e) {} }
}
/* ─ ID 抽出 ─ */
function showIdExtraction(id) {
    /* ツールバーの抽出フィールドに ID を転送 → extractPostsPopup で一元表示 */
    if (typeof _b === 'function') _b('extractText', [id]);
    else {
        /* ブリッジなし（ログファイル等）: 従来の自前パネル表示 */
        var posts = [];
        document.querySelectorAll('.res').forEach(function(el) {
            var idEl = el.querySelector('[data-id="' + id + '"]');
            if (idEl) posts.push('<div style="border-bottom:1px solid #ddd;">' + el.innerHTML + '</div>');
        });
        if (posts.length === 0) return;
        var panel = document.getElementById('_extract_panel');
        if (!panel) {
            panel = document.createElement('div');
            panel.id = '_extract_panel';
            panel.className = 'extract-panel';
            document.body.appendChild(panel);
        }
        panel.innerHTML = '<div class="extract-header"><span>ID:' + id + ' のレス (' + posts.length + '件)</span><button onclick="closeExtraction()">×</button></div>' + posts.join('');
        panel.style.display = 'block';
    }
}
/* ─ 動画インライン再生 ─ */
function playVideoInline(container) {
    var url  = container.getAttribute('data-video');
    var ext  = (url.split('.').pop().split('?')[0] || '').toLowerCase();

    /* MP4 / MOV は QtWebEngine が H.264 非対応 → 即ネイティブプレーヤーへ */
    if (ext === 'mp4' || ext === 'mov' || ext === 'm4v') {
        _b('playVideo', [url]);
        return;
    }

    /* WebM 等はまず HTML5 で試みる */
    var poster = container.getAttribute('data-poster');
    container.innerHTML = '';
    container.className = 'thumb';
    container.removeAttribute('onclick');
    var v = document.createElement('video');
    v.controls = true;
    if (poster) v.poster = poster;
    v.style.cssText = 'display:block;border:1px solid #aaa;max-width:300px;max-height:200px;background:#000;';
    /* HTML5 で再生不可ならネイティブプレーヤーへ */
    v.addEventListener('error', function() {
        _b('playVideo', [url]);
    });
    container.appendChild(v);
    v.src = url;
    v.play().catch(function(err) {
        console.warn('[video] HTML5 play failed:', err.message || err);
    });
}

// ── レス抽出（スレ内絞り込み） ─────────────────────────────────────────────
// 非表示は _ext_hide クラスで行い、NG非表示等のインラインstyleには触れない。
function extractPosts(query) {
    const op    = document.querySelector('.res.op');
    /* respool（ポップアップ用隠しプール）内のレスは対象外 */
    const all   = Array.prototype.filter.call(
        document.querySelectorAll('.res.reply'),
        function(el) { return !el.closest('#_respool'); });
    let   noMsg = document.getElementById('_extract_no_result');
    if (!query) {
        all.forEach(el => el.classList.remove('_ext_hide'));
        if (op) op.classList.remove('_ext_hide');
        if (noMsg) noMsg.remove();
        return;
    }
    const q = query.toLowerCase();
    let hits = 0;
    all.forEach(el => {
        el.classList.remove('_ext_hide');
        /* NG・削除等で元々非表示のレスは対象外（表示状態を変えない） */
        if (window.getComputedStyle(el).display === 'none') return;
        const txt = el.textContent.toLowerCase();
        if (txt.includes(q)) { hits++; }
        else                  { el.classList.add('_ext_hide'); }
    });
    // OP（0レス目）はヒット数に応じて表示/非表示
    if (op) op.classList.toggle('_ext_hide', hits === 0);
    // ヒット0件のとき赤字メッセージを表示
    if (hits === 0) {
        if (!noMsg) {
            noMsg = document.createElement('div');
            noMsg.id = '_extract_no_result';
            noMsg.style.cssText = 'color:#cc0000;font-weight:bold;padding:12px;font-size:10pt;';
            noMsg.textContent = '一致するスレ文・レスは見つかりません';
            document.body.appendChild(noMsg);
        }
    } else if (noMsg) {
        noMsg.remove();
    }
}

// ── テキスト抽出（ポップアップ表示） ────────────────────────────────────────
function extractPostsPopup(query) {
    var panel = document.getElementById('_extract_panel');
    if (!query) {
        if (panel) panel.style.display = 'none';
        return;
    }
    var q = query.toLowerCase();
    var posts = [];
    var matchedEls = [];
    document.querySelectorAll('.res').forEach(function(el) {
        var txt = el.textContent.toLowerCase();
        if (txt.indexOf(q) >= 0) {
            matchedEls.push(el);
            posts.push('<div style="border-bottom:1px solid #ddd;" data-extract-item="1">' + el.innerHTML + '</div>');
        }
    });
    if (!panel) {
        panel = document.createElement('div');
        panel.id = '_extract_panel';
        panel.className = 'extract-panel';
        document.body.appendChild(panel);
    }
    if (posts.length === 0) {
        panel.innerHTML = '<div class="extract-header"><span>「' + query + '」の抽出 (0件)</span><button onclick="closeExtraction()">×</button></div>'
            + '<div style="color:#cc0000;padding:10px;">一致するレスは見つかりません</div>';
    } else {
        panel.innerHTML = '<div class="extract-header"><span>「' + query + '」の抽出 (' + posts.length + '件)</span><button onclick="closeExtraction()">×</button></div>'
            + posts.join('');
        // 引用ホバーポップアップを有効化
        if (typeof window._hookPopupC === 'function') {
            window._hookPopupC(panel);
        }
        if (typeof window._hookPopupQuoteInd === 'function') {
            window._hookPopupQuoteInd(panel);
        }
    }
    panel.style.display = 'block';
}

// ── 差分更新: 新着レスをDOMに追記 ─────────────────────────────────────────
// 残存 .new-res の先頭に仕切り線を更新する（既読化は appendNewReplies で行う）
function _updateNewResDivider() {
    // 既存の仕切り線を全て除去（OP内のものも含む）
    document.querySelectorAll('.new-res-divider').forEach(function(el) {
        el.parentNode.removeChild(el);
    });
    var newReses = document.querySelectorAll('.res.reply.new-res');
    if (!newReses.length) return;
    var cnt = newReses.length;
    var div = document.createElement('div');
    div.className = 'new-res-divider';
    div.textContent = '─────── 新着ここから ' + cnt + '件 ───────';
    var first = newReses[0];
    // OP直後から新着の場合：firstがOPの直後の兄弟要素かチェック
    var opEl = document.querySelector('.res.op');
    if (opEl && first.previousElementSibling === opEl) {
        // OP直後から新着の場合：op-text末尾（画像右コンテンツの下）に挿入
        var opText = opEl.querySelector('.op-text');
        if (opText) {
            opText.appendChild(div);
        } else {
            // 画像なしOPはOP末尾に
            opEl.appendChild(div);
        }
    } else {
        // 通常: firstの直前に挿入
        first.parentNode.insertBefore(div, first);
    }
}

function appendNewReplies(htmlArray) {
    // 更新時点のスクロール位置で画面内（ビューポート内）の .new-res を既読化
    // getBoundingClientRect() はビューポート相対なので innerHeight と直接比較
    document.querySelectorAll('.res.new-res').forEach(function(el) {
        var rect = el.getBoundingClientRect();
        if (rect.bottom <= window.innerHeight) {
            el.classList.remove('new-res');
        }
    });
    // 新着レスがない場合（更新後に差分なし）：仕切り線を更新して終了
    if (!htmlArray || htmlArray.length === 0) {
        _updateNewResDivider();
        return;
    }
    // 新着が届いた → 「末尾を見た（既読）」フラグを解除し、タブ青背景を再表示可能にする
    window._unreadSeen = false;
    // .thread-end の直前に挿入（なければ body 末尾）
    var anchor = document.querySelector('.thread-end');
    var frag = document.createDocumentFragment();
    for (var i = 0; i < htmlArray.length; i++) {
        var tmp = document.createElement('div');
        tmp.innerHTML = htmlArray[i];
        while (tmp.firstChild) frag.appendChild(tmp.firstChild);
    }
    if (anchor) {
        anchor.parentNode.insertBefore(frag, anchor);
    } else {
        document.body.appendChild(frag);
    }
    // 仕切り線を未読最初のレスの直前に更新（画面内の既読化も兼ねる）
    _updateNewResDivider();
    // 引用インジケータ再構築・クリックハンドラ再付与
    _rebuildQuoteIndicators();
    // _inject_popup_js 注入済みならクロージャ内の正しい hookC/hookQuoteInd を使う
    // 未注入なら _rehookQuoteIndicators でフォールバック（showNos が undefined でも無害）
    if (typeof window._hookPopupC === 'function') {
        window._hookPopupC(document);
    }
    if (typeof window._hookPopupQuoteInd === 'function') {
        window._hookPopupQuoteInd(document);
    } else {
        _rehookQuoteIndicators();
    }
}

// 引用インジケータを全体再構築（差分更新後に呼ぶ）
function _rebuildQuoteIndicators() {
    var quotedBy = {};
    // 事前計算: 各resの「引用除去済みプレーンテキスト」とURL/ファイル名集を1回だけ作る。
    // テキスト引用・画像名引用の探索でO(n^2)のDOM深クローン/再クエリが起きるのを防ぐ。
    var _allRes = Array.from(document.querySelectorAll('.res'));
    var _info = _allRes.map(function(el) {
        var im = (el.id || '').match(/^r(\\d+)$/);
        var no = im ? parseInt(im[1]) : -1;
        var plain = '';
        var c0 = el.querySelector('.comment');
        if (c0) {
            var cl = c0.cloneNode(true);
            cl.querySelectorAll('span.qt').forEach(function(s) { s.remove(); });
            plain = (cl.textContent || '').toLowerCase();
        }
        var urls = '';
        el.querySelectorAll('a[href], img[src]').forEach(function(a) {
            urls += (a.getAttribute('href') || a.getAttribute('src') || '').toLowerCase() + ' ';
        });
        el.querySelectorAll('.ul-fname').forEach(function(s) {
            urls += (s.textContent || '').toLowerCase() + ' ';
        });
        return { no: no, plain: plain, urls: urls };
    });
    var _idxByNo = {};
    _info.forEach(function(o, i) { if (o.no >= 0) _idxByNo[o.no] = i; });
    document.querySelectorAll('.res').forEach(function(el) {
        var m = (el.id || '').match(/^r(\\d+)$/);
        if (!m) return;
        var myNo = parseInt(m[1]);
        var comEl = el.querySelector('.comment');
        // #r{no} リンク（_comment_html変換済み）
        el.querySelectorAll('.comment a[href^="#r"]').forEach(function(a) {
            var m2 = (a.getAttribute('href') || '').match(/#r(\\d+)/);
            if (!m2) return;
            var tgt = parseInt(m2[1]);
            if (!quotedBy[tgt]) quotedBy[tgt] = [];
            if (quotedBy[tgt].indexOf(myNo) < 0) quotedBy[tgt].push(myNo);
        });
        // span.qt（_comment_html変換済み・aタグなし）
        el.querySelectorAll('.comment span.qt').forEach(function(sp) {
            if (sp.querySelector('a')) return;
            if (sp.dataset && sp.dataset.idRef) return;
            var t = (sp.textContent || '').trim();
            var m3 = t.match(/^>+(No\\.)?(\\d+)\\s*$/);
            if (m3) {
                var tgt = parseInt(m3[2]);
                if (!quotedBy[tgt]) quotedBy[tgt] = [];
                if (quotedBy[tgt].indexOf(myNo) < 0) quotedBy[tgt].push(myNo);
                return;
            }
            // テキスト引用: 自分より前のレスで引用文を含む最近接1件を記録
            // （事前計算 _info[].plain を使い、DOM深クローンを排除）
            var q = t.replace(/^>+/, '').trim();
            if (q.length < 2) return;
            var ql = q.toLowerCase();
            var selfIdx = _idxByNo[myNo];
            if (selfIdx === undefined) selfIdx = _info.length;
            var hit = null;
            for (var ri = 0; ri < selfIdx; ri++) {
                if (_info[ri].no < 0) continue;
                if (_info[ri].plain.indexOf(ql) >= 0) hit = _info[ri].no;
            }
            if (hit !== null) {
                if (!quotedBy[hit]) quotedBy[hit] = [];
                if (quotedBy[hit].indexOf(myNo) < 0) quotedBy[hit].push(myNo);
            }
        });
        // JSON差分: font[color="#789922"] の生HTML（>No.XXX 形式）
        el.querySelectorAll('.comment font').forEach(function(f) {
            var c = (f.getAttribute('color') || '').toLowerCase().replace('#','');
            if (c !== '789922') return;
            var t = (f.textContent || '').trim();
            var m4 = t.match(/^>+(No\\.)?(\\d+)\\s*$/);
            if (m4) {
                var tgt = parseInt(m4[2]);
                if (!quotedBy[tgt]) quotedBy[tgt] = [];
                if (quotedBy[tgt].indexOf(myNo) < 0) quotedBy[tgt].push(myNo);
            }
        });
        // 画像ファイル名引用: span.qt[data-img-ref]（事前計算 _info[].urls を使用）
        el.querySelectorAll('.comment span.qt[data-img-ref]').forEach(function(sp) {
            var fname = (sp.getAttribute('data-img-ref') || '').toLowerCase();
            if (!fname) return;
            for (var ri = 0; ri < _info.length; ri++) {
                var o2 = _info[ri];
                if (o2.no < 0 || o2.no >= myNo) continue;
                if (o2.urls.indexOf(fname) >= 0) {
                    if (!quotedBy[o2.no]) quotedBy[o2.no] = [];
                    if (quotedBy[o2.no].indexOf(myNo) < 0) quotedBy[o2.no].push(myNo);
                }
            }
        });
        // 最後のレスの .comment innerHTML をダンプ（デバッグ用）
    });
    // 既存インジケータを一度クリアして再挿入
    document.querySelectorAll('.quote-ind').forEach(function(el) { el.remove(); });
    var inserted = 0;
    document.querySelectorAll('.res').forEach(function(el) {
        var m = (el.id || '').match(/^r(\\d+)$/);
        if (!m) return;
        var no = parseInt(m[1]);
        var qs = quotedBy[no];
        var header = el.querySelector('.header');
        if (!header) return;
        var btn = document.createElement('span');
        if (qs && qs.length) {
            btn.className = 'quote-ind';
            btn.textContent = '▼';
            btn.setAttribute('data-quoters', qs.join(','));
        } else {
            btn.className = 'quote-ind no-quote';
            btn.textContent = '…';
        }
        header.insertBefore(btn, header.firstChild);
    });
}

// ▼クリックハンドラを未付与のインジケータに再付与（差分更新後に呼ぶ）
function _rehookQuoteIndicators() {
    document.querySelectorAll('span.quote-ind[data-quoters]:not([data-hooked])').forEach(function(btn) {
        btn.setAttribute('data-hooked', '1');
        var nos = btn.getAttribute('data-quoters').split(',')
                     .map(function(s) { return parseInt(s.trim()); });
        btn.addEventListener('mouseover', function(e) {
            if (typeof showNos === 'function') {
                clearTimeout(window._qiTm);
                var cx = e.clientX, cy = e.clientY, tgt = e.currentTarget;
                window._qiTm = setTimeout(function() { showNos(nos, cx, cy, tgt); }, 300);
            }
            e.stopPropagation();
        });
        btn.addEventListener('mouseout', function(e) {
            clearTimeout(window._qiTm);
            if (typeof schedH === 'function') schedH();
        });
    });
    // 画像ファイル名引用 span のポップアップ（_inject_popup_js後に有効）
    document.querySelectorAll('.comment span.qt[data-img-ref]:not([data-hooked])').forEach(function(sp) {
        sp.setAttribute('data-hooked', '1');
        var fname = (sp.getAttribute('data-img-ref') || '').toLowerCase();
        sp.addEventListener('mouseover', function(e) {
            if (typeof showImgRef !== 'function') return;
            clearTimeout(window._qiTm);
            var cx = e.clientX, cy = e.clientY, tgt = e.currentTarget;
            window._qiTm = setTimeout(function() { showImgRef(fname, cx, cy, tgt); }, 300);
            e.stopPropagation();
        });
        sp.addEventListener('mouseout', function(e) {
            clearTimeout(window._qiTm);
            if (typeof schedH === 'function') schedH();
        });
    });
}

// そうだね更新（差分更新でも既存関数をそのまま利用）
function updateSodaneForNew(no, cnt) { updateSodane(no, cnt); }

// オープンモードスクロール: 0=通常, 1=返信, 2=画像, 3=引用
function scrollToMode(mode) {
    if (mode === 0) return;
    var sel = null;
    if (mode === 1) {
        sel = document.querySelector('.res.is_new') ||
              document.querySelector('.res:not(:first-child)');
    } else if (mode === 2) {
        var els = document.querySelectorAll('.res');
        for (var i = 0; i < els.length; i++) {
            if (els[i].querySelector('img.thumb, img.image')) { sel = els[i]; break; }
        }
    } else if (mode === 3) {
        var els = document.querySelectorAll('.res');
        for (var i = 0; i < els.length; i++) {
            if (els[i].querySelector('font[color="#789922"]')) { sel = els[i]; break; }
        }
    }
    if (sel) { sel.scrollIntoView({behavior:'smooth', block:'start'}); }
}
</script>
"""


# ══════════════════════════════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════════════════════════════

def _e(s) -> str:
    return _html.escape(str(s or ""), quote=True)

def _elapsed(datetime_str: str) -> str:
    m = re.search(r"(\d+)/(\d+)/(\d+)[^)]*\)(\d+):(\d+):(\d+)", str(datetime_str))
    if not m:
        return ""
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        delta = _dt.datetime.now() - _dt.datetime(2000 + y, mo, d, h, mi, s)
        sec = int(delta.total_seconds())
        if sec < 0:     return ""
        if sec < 60:    return f"{sec}秒経過"
        if sec < 3600:  return f"{sec//60}分{sec%60}秒経過"
        if sec < 86400: return f"{sec//3600}時間{(sec%3600)//60}分経過"
        return f"{sec//86400}日{(sec%86400)//3600}時間経過"
    except Exception:
        return ""


def _apply_uploaders(text: str, uploaders: list,
                     img_list: list = None, res_no: int = 0) -> list:
    """テキスト中のアップローダーパターンをリンク（または画像サムネイル）に変換する。
    img_list が渡された場合、画像URLをimg_listに登録してidxをopenImgに渡す。
    戻り値: str (通常テキスト) と str (リンクHTML) の混在リスト"""
    if not uploaders or not text.strip():
        return [text]
    _IMG_EXTS = re.compile(r'\.(jpe?g|png|gif|webp|bmp)$', re.IGNORECASE)
    result = []
    pos = 0
    matches = []
    for ul in uploaders:
        try:
            for m in re.finditer(ul["pattern"], text, re.IGNORECASE):
                matches.append((m.start(), m.end(), m.group(0), ul))
        except Exception:
            pass
    if not matches:
        return [text]
    matches.sort(key=lambda x: x[0])
    for start, end, match_str, ul in matches:
        if start < pos:
            continue
        if start > pos:
            result.append(text[pos:start])
        url = ul["url"].replace("$MATCH", match_str)
        eu  = _e(url)
        em  = _e(match_str)
        en  = _e(ul["name"])
        # 直リン画像の場合はサムネイルを表示
        _is_img = bool(_IMG_EXTS.search(url.split('?')[0]))
        if _is_img:
            if img_list is not None:
                _ul_idx = len(img_list)
                img_list.append({"url": url, "name": match_str, "res_no": res_no})
            else:
                _ul_idx = -1
            link = (
                f'<span class="ul-img-wrap">'
                f'<span class="ul-fname">{em}</span>'
                f'<a href="{eu}" class="ul-link" title="{en}" '
                f'onclick="openImg(\'{eu}\',{_ul_idx});return false;" '
                f'onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{eu}\',{_ul_idx});}}">'
                f'<img src="{eu}" class="ul-thumb" loading="lazy" '
                f'onerror="this.parentElement.parentElement.style.display=\'none\'">'
                f'</a>'
                f'</span>'
            )
        elif _is_video(url):
            # うｐろだ動画: ファイル名クリックで VideoPlayerWindow を開く
            link = (
                f'<a href="{eu}" class="ul-link" title="{en}" '
                f'onclick="playVideoInline_footer(\'{eu}\');return false;">&#9654; {em}</a>'
            )
        else:
            link = (
                f'<a href="{eu}" class="ul-link" title="{en}" '
                f'onclick="openUrl(\'{eu}\');return false;">{em}</a>'
            )
        result.append(link)
        pos = end
    if pos < len(text):
        result.append(text[pos:])
    return result if result else [text]

# 画像/動画ファイル名引用パターン: >1234567890.jpg など
# ── レス毎ホットパス用の事前コンパイル済みパターン ──────────────────────────
_QT_NO_RE     = re.compile(r"^>+(No\.)?(\d+)\s*$")
_QT_ID_RE     = re.compile(r"^>+ID:([A-Za-z0-9+/]{4,})\s*$")
_A_NO_RE      = re.compile(r"No\.(\d+)")
_A_GTGT_RE    = re.compile(r">>(\d+)$")
_A_RES_RE     = re.compile(r"RES\s+(\d+)")
_YT_RE        = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_\-]{11})")
_URL_SPLIT_RE = re.compile(r"(https?://[^\s\u3000\u3002\uff0c\uff01]+)")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_ID_STRIP_RE  = re.compile(r"\s*ID:\S+")
_EMAIL_IDIP_RE = re.compile(r'\s*(?:id|ip)\s*表示\s*', re.IGNORECASE)

_IMG_QUOTE_RE = re.compile(
    r'^>+(\d{10,}\.(jpe?g|png|gif|webp|bmp|mp4|webm))$',
    re.IGNORECASE
)

def _comment_html(raw_html: str, _uploaders: list = None,
                  _img_list: list = None, _res_no: int = 0) -> str:
    from bs4 import BeautifulSoup
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    bq   = soup.find("blockquote")
    # blockquoteなし（JSON差分APIのcomフィールド等）はsoup直下をパース
    if not bq:
        bq = soup
    parts = []
    for child in bq.children:
        tag = getattr(child, "name", None)
        if tag == "br":
            parts.append("<br>")
        elif tag == "font":
            color = (child.get("color") or "").lower()
            if "ff0000" in color:
                parts.append(f'<span class="del">{_e(child.get_text())}</span>')
            elif "789922" in color:
                for line in child.get_text().splitlines(keepends=True):
                    m_q  = _QT_NO_RE.match(line.strip())
                    m_id = _QT_ID_RE.match(line.strip())
                    if m_q:
                        qno = int(m_q.group(2))
                        parts.append(
                            f'<span class="qt"><a href="#r{qno}" '
                            f'onmouseenter="showPopup({qno},event.clientX,event.clientY)" '
                            f'onmouseleave="hidePopup()" '
                            f'onclick="quoteNo({qno});return false;">'
                            f'{_e(line.rstrip())}</a></span>')
                    elif m_id:
                        # >ID:xxx → ホバーでそのIDのレス一覧をポップアップ表示
                        # （番号引用 >>No. のホバーと同様の挙動）。クリックは従来
                        # どおり抽出（showIdExtraction）を維持する。
                        id_val = _e(m_id.group(1))
                        parts.append(
                            f'<span class="qt" data-id-ref="{id_val}" '
                            f'onmouseenter="showIdPopup(\'{id_val}\',event.clientX,event.clientY)" '
                            f'onmouseleave="hideIdPopup()" '
                            f'onclick="showIdExtraction(\'{id_val}\')">'
                            f'{_e(line.rstrip())}</span>')
                    else:
                        m_img = _IMG_QUOTE_RE.match(line.strip())
                        if m_img:
                            fname = _e(m_img.group(1).lower())
                            parts.append(
                                f'<span class="qt" data-img-ref="{fname}">'
                                f'{_e(line.rstrip())}</span>')
                        else:
                            parts.append(f'<span class="qt">{_e(line)}</span>')
            else:
                parts.append(_e(child.get_text()))
        elif tag == "a":
            href = child.get("href") or ""
            # ふたばの外部リンクラッパー「bin/jump.php?実URL」を除去
            if "jump.php?" in href:
                href = href.split("jump.php?", 1)[1]
            href_e = _e(href)
            text = child.get_text()
            m = _A_NO_RE.match(text)
            m2 = _A_GTGT_RE.match(text.strip()) if not m else None
            m3 = _A_RES_RE.match(text.strip()) if not (m or m2) else None
            if m or m2 or m3:
                no = int((m or m2 or m3).group(1))
                parts.append(
                    f'<a href="#r{no}" '
                    f'onmouseenter="showPopup({no},event.clientX,event.clientY)" '
                    f'onmouseleave="hidePopup()" '
                    f'onclick="quoteNo({no});return false;">'
                    f'{_e(text)}</a>')
            elif href:
                parts.append(
                    f'<a href="{href_e}" onclick="openUrl(\'{href_e}\');return false;">'
                    f'{_e(text)}</a>')
                # YouTube サムネイル（<a>タグのURLにも対応）
                yt_m = _YT_RE.search(href)
                if yt_m:
                    vid   = yt_m.group(1)
                    thumb = f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"
                    parts.append(
                        f'<br><img class="yt-thumb" src="{thumb}" '
                        f'onerror="this.style.display=\'none\'" '
                        f'onclick="openUrl(\'{href_e}\');return false;">')
            else:
                parts.append(_e(text))
        elif tag in ("del", "s"):
            parts.append(f'<span class="del">{_e(child.get_text())}</span>')
        elif tag is None:
            raw = str(child)
            for seg in _URL_SPLIT_RE.split(raw):
                if _URL_SPLIT_RE.match(seg):
                    eu = _e(seg)
                    # YouTube サムネイル
                    yt_m = _YT_RE.search(seg)
                    if yt_m:
                        vid = yt_m.group(1)
                        thumb = f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"
                        parts.append(
                            f'<a href="{eu}" onclick="playVideoInline_footer(\'{eu}\');return false;">'
                            f'{eu}</a>'
                            f'<br><img class="yt-thumb" src="{thumb}" '
                            f'onerror="this.style.display=\'none\'" '
                            f'onclick="playVideoInline_footer(\'{eu}\');return false;">')
                    else:
                        parts.append(
                            f'<a href="{eu}" onclick="playVideoInline_footer(\'{eu}\');return false;">'
                            f'{eu}</a>')
                else:
                    for chunk in _apply_uploaders(seg, _uploaders,
                                                    img_list=_img_list, res_no=_res_no):
                        if isinstance(chunk, str) and '<a ' not in chunk:
                            for line in chunk.splitlines(keepends=True):
                                if line.startswith(">"):
                                    m_img = _IMG_QUOTE_RE.match(line.strip())
                                    if m_img:
                                        fname = _e(m_img.group(1).lower())
                                        parts.append(
                                            f'<span class="qt" data-img-ref="{fname}">'
                                            f'{_e(line.rstrip())}</span>')
                                    else:
                                        parts.append(f'<span class="qt">{_e(line)}</span>')
                                else:
                                    parts.append(_e(line))
                        else:
                            parts.append(chunk)
    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# スレッド HTML 生成
# ══════════════════════════════════════════════════════════════════════════════

_VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.m4v')

def _is_video(url: str) -> bool:
    """URLが動画ファイルかどうかを判定"""
    lo = url.lower().split('?')[0]
    return any(lo.endswith(ext) for ext in _VIDEO_EXTS)


def _media_type_label(image_url: str, image_name: str, thumb_url: str) -> str:
    """ファイル名右側に表示するメディアタイプラベル（太字赤字）を返す。
    mp4/webm → 'MP4'/'WEBM'、アニメーションGIF → 'GIF'、静止画GIF → ''"""
    lo = (image_url or "").lower().split('?')[0]
    lo_name = (image_name or "").lower()
    if lo.endswith('.mp4') or lo_name.endswith('.mp4'):
        return '<b style="color:#cc0000;margin-left:4px;">MP4</b>'
    if lo.endswith('.webm') or lo_name.endswith('.webm'):
        return '<b style="color:#cc0000;margin-left:4px;">WEBM</b>'
    # GIF: サムネイルURLが.gifで終わる場合はアニメーションGIFと判定
    if lo.endswith('.gif') or lo_name.endswith('.gif'):
        thumb_lo = (thumb_url or "").lower().split('?')[0]
        if thumb_lo.endswith('.gif'):
            return '<b style="color:#cc0000;margin-left:4px;">GIF</b>'
        # サムネイルがjpg変換済み（静止画GIF）→ ラベルなし
        return ''
    return ''


def _apply_replace_to_html(html: str, ng_filter) -> str:
    """HTMLのテキストノード部分のみにapply_replaceを適用（タグ内は変更しない）"""
    parts = re.split(r'(<[^>]+>)', html)
    result = []
    for part in parts:
        if part.startswith('<'):
            result.append(part)          # タグはそのまま
        else:
            result.append(ng_filter.apply_replace(part))
    return "".join(result)


def ng_info_text(res, ng_filter, ng_settings, manual_hidden: bool = False) -> str:
    """NGレスが「どのNGワード/NG画像でNGに入ったか」を1行テキストで返す。
    No.クリックポップアップ(del-pop)の最下部表示用。
    NGワードマッチ（適用範囲考慮）→ マッチした全パターンを列挙。
    NGワードが無ければNG画像 → マッチしたエントリの要約。
    どちらも無く手動NGなら「手動NG」。非NGレスは空文字。"""
    parts = []
    if ng_filter is not None and not getattr(res, "is_op", False) \
            and not getattr(res, "is_deleted", False):
        _hide_name  = getattr(ng_settings, "ng_thread_hide_name",  True)
        _hide_image = getattr(ng_settings, "ng_thread_hide_image", True)
        try:
            if _hide_name:
                ws = ng_filter.get_matched_ng_words(res)
                if ws:
                    parts.append("NGワード: " + "、".join(f"「{w}」" for w in ws))
        except Exception:
            pass
        try:
            if not parts and _hide_image and res.image_url \
                    and ng_filter.is_ng_image(res):
                d = ng_filter.get_matched_ng_image_desc(res)
                parts.append(f"NG画像: {d}" if d else "NG画像")
        except Exception:
            pass
    if manual_hidden and not parts:
        parts.append("手動NG（NGボタン/delによる非表示）")
    return " ／ ".join(parts)


def render_res(res, is_op: bool, img_list: list, uploaders: list = None,
               ng_filter=None, ng_settings=None, hidden_nos: set = None,
               id_counts: dict = None, has_name_field: bool = True,
               my_nos: set = None, divider_html: str = "",
               id_warn_count: int = 0, del_nos: set = None,
               ng_reveal: bool = False) -> str:
    no = res.no
    # del（削除依頼/記事削除）したレスか。No.の右に「del済」を赤表示する。
    _is_del_done = bool(del_nos and no in del_nos and not is_op)
    # ── 手動NG（永続非表示）判定 ───────────────────────────────────────────
    # 内容は通常どおり描画しつつ display:none で隠す。空divにすると、この
    # レスを引用しているレスの引用ポップアップ（クローン元）が空になり
    # 「何も表示されない」状態になるため、中身は保持する。
    _manual_hidden = bool(hidden_nos and no in hidden_nos and not is_op)

    # ── NG判定 ──────────────────────────────────────────────────────────────
    ng_class = ""
    ng_style = ""       # 逆NG用インラインスタイル
    _ng_reason = ""     # NGで非表示にされた理由（引用ポップアップ用）。空なら通常描画。
    _is_ng_match = False  # NGワード/NG画像にマッチした（=緑帯対象）か
    if ng_filter is not None and not is_op and not res.is_deleted:
        _hide_name  = getattr(ng_settings, "ng_thread_hide_name",  True)
        _hide_image = getattr(ng_settings, "ng_thread_hide_image", True)

        # 名前/トリップ/書き込みNG
        if _hide_name and ng_filter.is_ng(res):
            ng_class = " ng-hidden"
            _ng_reason = "NGワード・名前により非表示"
            _is_ng_match = True
        # 画像NG（hide_modeによって透明 or レス全体非表示）
        elif _hide_image and res.image_url and ng_filter.is_ng_image(res):
            _hm = ng_filter.get_ng_image_hide_mode(res)
            if _hm == "res":
                ng_class = " ng-hidden"
                _ng_reason = "NG画像により非表示"
            else:
                ng_class = " ng-image"
            _is_ng_match = True
    # 緑帯/非表示の対象 = NG[使う/解除]の対象レス:
    #   NGワード/NG画像マッチ or 手動NG登録(ng_hidden_res_nos: フッタNGボタン・del登録)。
    # これらに緑帯(ng-band)を付け、NG解除(ng_reveal)時は隠さず緑帯のみで表示する。
    _is_ng_target = _is_ng_match or _manual_hidden
    if _is_ng_target:
        ng_class += " ng-band"
    # NG理由詳細（No.クリックポップアップの最下部に表示。data-ng-info 属性で埋め込む）
    _ng_info = ng_info_text(res, ng_filter, ng_settings, _manual_hidden) \
               if _is_ng_target else ""
    if ng_reveal:
        # NG解除（表示）状態: NG対象を隠さず緑帯のみ（理由のみ表示/透明化/手動非表示も解除）
        _ng_reason = ""
        ng_style = ""
        _manual_hidden = False
        ng_class = ng_class.replace(" ng-hidden", "").replace(" ng-image", "")
    elif _manual_hidden and not _ng_reason:
        # 手動NG（永続非表示）の理由を記録（フィルタ理由が無いときのみ）
        _ng_reason = "NG設定により非表示"


    # 削除レスは "reply deleted" クラス、新着は "new-res" クラス、自分のレスは "self-res"
    if is_op:
        bg_class = "op self-res" if (my_nos and res.no in my_nos) else "op"
    elif res.is_deleted:
        bg_class = "reply deleted"
    else:
        _is_my   = bool(my_nos and res.no in my_nos)
        new_cls  = " self-res" if _is_my else (" new-res" if res.is_new else "")
        bg_class = f"reply{new_cls}"

    # ── 画像を img_list に事前登録してインデックスを確定 ──
    img_tab_idx = None
    if res.image_url:
        img_tab_idx = len(img_list)
        _cmt_raw = _TAG_STRIP_RE.sub("", res.comment_text or "").strip()[:120]
        img_list.append({"url": res.image_url,
                         "name": res.image_name or res.image_url.split("/")[-1],
                         "res_no": no,
                         "comment": _cmt_raw})

    # ── ファイル情報 (動画はplayVideoInline_footer、画像はopenImg) ──
    fi_html   = ""   # OP用（ヘッダー上）
    fi_inline = ""   # 未使用（互換性のため残す）
    fi_sub    = ""   # 返信用（ヘッダー下段）
    if res.image_url and res.image_name and img_tab_idx is not None:
        eu  = _e(res.image_url)
        sz_str = f"({res.file_size_bytes} B)" if res.file_size_bytes else ""
        _mlabel = _media_type_label(res.image_url, res.image_name, res.thumb_url or "")
        if _is_video(res.image_url):
            _il = (f'<a class="fi-inline" href="{eu}"'
                   f' onclick="playVideoInline_footer(\'{eu}\');return false;">'
                   f'{_e(res.image_name)}</a>')
            if is_op:
                fi_html = (f'<div class="file-info">動画ファイル名: {_il}{_mlabel}-{sz_str}</div>')
            else:
                _lnk = (f'<a href="{eu}"'
                        f' onclick="playVideoInline_footer(\'{eu}\');return false;">'
                        f'{_e(res.image_name)}</a>')
                fi_sub = f'<div class="fi-sub">{_lnk}{_mlabel}-{sz_str}</div>'
        else:
            gl  = f"https://lens.google.com/uploadbyurl?url={urllib.parse.quote(res.image_url, safe='')}"
            a2d = f"https://ascii2d.net/search/url/{urllib.parse.quote(res.image_url, safe='')}"
            _il = (f'<a class="fi-inline" href="{eu}"'
                   f' onclick="openImg(\'{eu}\',{img_tab_idx});return false;"'
                   f' onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{eu}\',{img_tab_idx});}}">'
                   f'{_e(res.image_name)}</a>')
            snao = f"https://saucenao.com/search.php?url={urllib.parse.quote(res.image_url, safe='')}"
            _gl  = f'<a href="{_e(gl)}"  onclick="openUrl(\'{_e(gl)}\');return false;">[google]</a>'
            _a2d = f'<a href="{_e(a2d)}" onclick="openUrl(\'{_e(a2d)}\');return false;">[二次元]</a>'
            _snl = f'<a href="{_e(snao)}" onclick="openUrl(\'{_e(snao)}\');return false;">[NAO]</a>'
            if is_op:
                fi_html = (f'<div class="file-info">画像ファイル名: {_il}{_mlabel}-{sz_str} {_gl} {_a2d} {_snl}'
                           f'</div>')
            else:
                _lnk = (f'<a href="{eu}"'
                        f' onclick="openImg(\'{eu}\',{img_tab_idx});return false;"'
                        f' onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{eu}\',{img_tab_idx});}}">'
                        f'{_e(res.image_name)}</a>')
                fi_sub = f'<div class="fi-sub">{_lnk}{_mlabel}-{sz_str} {_gl} {_a2d} {_snl}</div>'

    # ── ヘッダー ──
    rsc_html = '' if is_op else \
               f'<span class="rsc">{res.res_idx}</span>'
    csb_text_raw = res.csb or ("無念" if has_name_field else "")
    csb  = _e(csb_text_raw)
    csb_html = f'<span class="csb">{csb}</span>' if csb_text_raw else ""
    # has_name_field=Falseの板（img板等）は名前欄を非表示だがemailバッジは表示
    if has_name_field:
        nm   = _e(res.name or "としあき")
        trip = f'<span class="trip"> {_e(res.trip)}</span>' if res.trip else ""
        # メアドがある場合は名前を mailto リンクに（sage はリンクなし）
        _email = (res.email or "").strip()
        if _email and _email.lower() != "sage":
            nm_html = (f'<a class="nm" href="mailto:{_e(_email)}"'
                       f' onclick="return false;" title="{_e(_email)}">{nm}</a>')
            email_badge = f'<span class="email-badge">[{_e(_email)}]</span>'
        elif _email.lower() == "sage":
            nm_html = f'<span class="nm">{nm}</span>'
            email_badge = '<span class="email-badge">[sage]</span>'
        else:
            nm_html = f'<span class="nm">{nm}</span>'
            email_badge = ''
        name_block = f'<span class="csb-nm">Name</span>{nm_html}{trip}{email_badge}'
    else:
        # name欄なし板: nameラベル・名前・tripは非表示、emailバッジのみ表示
        _email = (res.email or "").strip()
        if _email and _email.lower() != "sage":
            email_badge = f'<span class="email-badge">[{_e(_email)}]</span>'
        elif _email.lower() == "sage":
            email_badge = '<span class="email-badge">[sage]</span>'
        else:
            email_badge = ''
        name_block = email_badge
    dt   = _e(_ID_STRIP_RE.sub("", res.datetime_str).strip())
    sod  = _e(f"そうだねx{res.sodane}" if res.sodane > 0 else "+")
    exp  = f'<span class="expiry">&nbsp;{_e(res.expiry_str)}</span>' \
           if is_op and res.expiry_str else ""
    _eid = _e(res.id_str)
    if res.id_str:
        _cnt = (id_counts or {}).get(res.id_str, 0)
        _cnt_html = f'<span class="post-id-count">[{_cnt}]</span>' if _cnt > 1 else ''
        # ID出現回数が閾値以上なら赤字クラスを付与
        _warn_cls = ' post-id-warn' if (id_warn_count and _cnt >= id_warn_count) else ''
        id_html = (
            f'<span class="post-id{_warn_cls}" data-id="{_eid}" '
            f'onclick="showIdExtraction(\'{_eid}\')">' 
            f'<span class="post-id-prefix">ID:</span>'
            f'<span class="post-id-value">{_eid}</span>'
            f'{_cnt_html}'
            f'</span>'
        )
    else:
        id_html = ''
    _del_mark = '<span class="del-done">del済</span>' if _is_del_done else ''
    hdr_html = (
        f'<div class="header">'
        f'{rsc_html}'
        f'{csb_html}'
        f'{name_block if has_name_field else ""}'
        f'<span class="dt">{dt}</span>'
        f'{name_block if not has_name_field else ""}'
        f'{id_html}'
        f'<a class="no" href="#r{no}" onclick="delRes({no},this);return false;">No.{no}</a>'
        f'{_del_mark}'
        f'<button class="sod" id="sod{no}" onclick="sodane({no})">{sod}</button>'
        f'{exp}'
        f'</div>'
    )

    # ── サムネイル: 動画はインライン <video>、画像は <img> ──
    img_html = ""
    if (res.thumb_url and res.image_url and img_tab_idx is not None):
        tu = _e(res.thumb_url)
        eu = _e(res.image_url)
        if _is_video(res.image_url):
            # 左クリック・中クリック → VideoPlayerWindow で再生
            img_html = (
                f'<div class="thumb video-thumb" data-video="{eu}" data-poster="{tu}"'
                f' onclick="playVideoInline_footer(\'{eu}\')"'
                f' onmousedown="if(event.button===1){{event.preventDefault();playVideoInline_footer(\'{eu}\');}}">'
                f'<img src="{tu}" loading="lazy">'
                f'<span class="play-btn">&#9654;</span>'
                f'</div>'
            )
        else:
            _tw = getattr(res, "thumb_w", 0) or 0
            _th = getattr(res, "thumb_h", 0) or 0
            # サムネ寸法を明示（futaba原本と同様）。no_thumb保存ログで
            # サムネ=本画像のとき等倍表示になるのを防ぐ
            # aspect-ratio も明示することで、画像読み込み後に本来の縦横比が
            # height:auto に優先適用されて表示が崩れる（潰れた画像が縦長になる等）のを防ぐ
            # 寸法不明（JSON差分API由来の新着レス・旧バージョン保存ログ等で
            # thumb_w/h が無い）の場合は max-width/height で上限を設け、
            # 本画像が等倍（フルサイズ）表示されるのを防ぐ。
            _dim = (f' width="{_tw}" height="{_th}" style="aspect-ratio:{_tw}/{_th}"'
                    if (_tw > 0 and _th > 0)
                    else ' style="max-width:250px;max-height:250px;width:auto;height:auto"')
            img_html = (
                f'<div class="thumb">'
                f'<img src="{tu}"{_dim} loading="lazy" data-full="{eu}" '
                f'onclick="openImg(\'{eu}\',{img_tab_idx})" '
                f'onmousedown="if(event.button===1){{event.preventDefault();openImgBg(\'{eu}\',{img_tab_idx});}}">'
                f'</div>'
            )

    # ── コメント: 削除レスは削除理由とレス内容を分離 ──
    if res.is_deleted:
        _bq_html = res.comment_html or ""
        # comment_html = <blockquote><font color="#ff0000">理由</font><br>内容</blockquote>
        # BeautifulSoupでパースして分離
        try:
            from bs4 import BeautifulSoup as _BS
            _bq_soup = _BS(_bq_html, "html.parser")
            _bq = _bq_soup.find("blockquote") or _bq_soup
            _font = _bq.find("font", color="#ff0000")
            if _font:
                _reason_txt = _font.get_text(strip=True)
                # fontタグとその直後の<br>を除去してレス内容を取得
                _font.extract()
                for _br in _bq.find_all("br", limit=1):
                    _br.extract()
                    break
                _body_txt = _bq.get_text(separator="\n").strip()
            else:
                _reason_txt = ""
                _body_txt = _bq.get_text(separator="\n").strip()
        except Exception:
            _reason_txt = ""
            _body_txt = (res.comment_text or "").strip()
        _reason_part = f'<span class="del-reason">{_e(_reason_txt) if _reason_txt else "削除済み"}</span>'
        _body_part   = f'<span class="del-content">{_e(_body_txt)}</span>' if _body_txt else ""
        com = _reason_part + (f'<br>{_body_part}' if _body_part else "")
    else:
        com_html = res.comment_html or ""
        # 置換/芝刈り置換NGをテキストノードのみに適用
        if ng_filter is not None:
            com_html = _apply_replace_to_html(com_html, ng_filter)
        com = _comment_html(com_html, _uploaders=uploaders,
                              _img_list=img_list, _res_no=no)

    # ── フッター ──
    elapsed      = _elapsed(res.datetime_str)
    elapsed_html = f" / {_e(elapsed)}" if elapsed else ""

    # 画像・動画リンク（画像ありのレスのみ）
    media_html = ""
    if res.image_url and img_tab_idx is not None:
        eu = _e(res.image_url)
        if _is_video(res.image_url):
            # 動画: クリックで VideoPlayerWindow を開く
            media_html = (
                f' - <a href="#" onclick="playVideoInline_footer(\'{eu}\');return false;">動画</a>'
            )
        else:
            # 画像: クリックでそのレスの画像を引用
            media_html = (
                f' - <a href="#" onclick="quoteImg({no});return false;">画像</a>'
            )

    ft_html = (
        f'<div class="footer">'
        f'RES <a href="#" onclick="quoteNo({no});return false;">番号</a>'
        f' - <a href="#" onclick="quoteComment({no});return false;">コメント</a>'
        f'{media_html}'
        f' / <a class="ng" href="#" onclick="ngRes({no});return false;">NG</a>'
        f'{elapsed_html}'
        f'</div>'
    )

    # ── 構造組み立て ──
    if is_op:
        if img_html:
            body = (
                f'{img_html}'
                f'<div class="op-text">'
                f'{hdr_html}'
                f'<div class="comment">{com}</div>'
                f'{ft_html}'
                f'</div>'
            )
        else:
            body = (
                f'{hdr_html}'
                f'<div class="comment">{com}</div>'
                f'{ft_html}'
            )
        # 仕切り線はop-text内末尾（画像右のコンテンツ下）に配置
        if img_html:
            body = (
                f'{img_html}'
                f'<div class="op-text">'
                f'{hdr_html}'
                f'<div class="comment">{com}</div>'
                f'{ft_html}'
                f'{divider_html}'
                f'</div>'
            )
        else:
            body = (
                f'{hdr_html}'
                f'<div class="comment">{com}</div>'
                f'{ft_html}'
                f'{divider_html}'
            )
        return f'<div class="res op" id="r{no}">{fi_html}{body}</div>\n'
    else:
        has_cls = ""
        if res.image_url: has_cls += " has-img"
        if res.comment_html and (">>" in res.comment_html or "qt" in res.comment_html): has_cls += " has-qt"
        img_attr = f' data-img="{res.image_name}"' if res.image_name else ""
        # NG非表示レス（手動NG/NGワード/NG画像）は display:none で隠す。
        # スレッドでは見えないが、このレスを引用しているレスの引用ポップアップ
        # （クローン元）として中身が必要。本文は出さず「削除理由（NG理由）」のみ
        # を表示する。
        _styles = []
        if ng_style: _styles.append(ng_style)
        if (_manual_hidden or _ng_reason):
            _styles.append("display:none")
        style_attr = f' style="{";".join(_styles)}"' if _styles else ""
        if (_manual_hidden or _ng_reason) and "ng-hidden" not in ng_class:
            ng_class += " ng-hidden"
        _ngi_attr = f' data-ng-info="{_e(_ng_info)}"' if _ng_info else ""
        if _ng_reason:
            # 理由のみ表示（本文・画像・フッターは出さない）
            ct_html = (f'<div class="content">'
                       f'<span class="del-reason">{_e(_ng_reason)}</span></div>')
            return (f'<div class="res {bg_class}{has_cls}{ng_class}" id="r{no}"'
                    f'{img_attr}{style_attr}{_ngi_attr}>{hdr_html}{ct_html}</div>\n')
        ct_html = f'<div class="content">{img_html}<div class="comment">{com}</div></div>'
        return f'<div class="res {bg_class}{has_cls}{ng_class}" id="r{no}"{img_attr}{style_attr}{_ngi_attr}>{hdr_html}{fi_sub}{ct_html}{ft_html}</div>\n'


def res_fragment_html(res_list: list, img_list_base: list,
                      uploaders: list = None,
                      ng_filter=None, ng_settings=None,
                      hidden_nos: set = None,
                      id_counts: dict = None,
                      has_name_field: bool = True,
                      my_nos: set = None,
                      id_warn_count: int = 0,
                      del_nos: set = None,
                      ng_reveal: bool = False) -> tuple[list[str], list]:
    """新着レス群を HTML 断片のリストに変換する（差分更新用）。

    Parameters
    ----------
    res_list : 新着 ResData のリスト（OP を含まない）
    img_list_base : 既存の img_list（新着分はここに append される）
    uploaders / ng_filter / ng_settings / hidden_nos / id_counts :
        render_res に渡す各種設定

    Returns
    -------
    (fragments, img_list_base)
        fragments  : 各レスの HTML 断片文字列リスト
        img_list_base : 新着分が追記された img_list
    """
    fragments = []
    new_count = len(res_list)
    for res in res_list:
        frag = render_res(res, is_op=False, img_list=img_list_base,
                          uploaders=uploaders,
                          ng_filter=ng_filter, ng_settings=ng_settings,
                          hidden_nos=hidden_nos, id_counts=id_counts,
                          has_name_field=has_name_field,
                          my_nos=my_nos,
                          id_warn_count=id_warn_count,
                          del_nos=del_nos,
                          ng_reveal=ng_reveal)
        fragments.append(frag)
    return fragments, img_list_base


def thread_to_html(thread, show_deleted: bool = False,
                   user_css: str = "", uploaders: list = None,
                   ng_filter=None, ng_settings=None,
                   hidden_nos: set = None,
                   scroll_bottom_count: int = 5,
                   footer_html: str = "",
                   my_nos: set = None,
                   for_save: bool = False,
                   id_warn_count: int = 0,
                   scroll_top_count: int = 0,
                   del_nos: set = None,
                   ng_reveal: bool = False,
                   pseudo_expiring: bool = False,
                   sort_by_sodane: bool = False) -> tuple[str, list]:
    """ThreadData → (HTML文字列, 画像リスト)"""
    img_list: list = []
    rows = []
    # 描画順: そうだね順ONならOP先頭固定＋残りをそうだね降順（同数は投稿順=安定ソート）。
    # 新着「ここから」仕切り線は並びが変わると位置が無意味なので出さない。
    if sort_by_sodane and len(thread.res_list) > 1:
        _draw_list = [thread.res_list[0]] + sorted(
            thread.res_list[1:], key=lambda r: r.sodane, reverse=True)
    else:
        _draw_list = thread.res_list
    deleted_count = sum(1 for r in thread.res_list[1:] if r.is_deleted)
    # ID ごとの投稿件数を事前集計
    id_counts: dict[str, int] = {}
    for r in thread.res_list:
        if r.id_str:
            id_counts[r.id_str] = id_counts.get(r.id_str, 0) + 1
    _has_name = getattr(thread.board, 'has_name_field', True)
    # 新着件数を事前集計（仕切り線の N件 表示用）。ログ保存時は仕切り線を出さない
    _new_count = 0 if (for_save or sort_by_sodane) else sum(
        1 for r in thread.res_list if r.is_new and not r.is_op)
    _divider_inserted = False
    # OP直後から新着が始まる場合、仕切り線をOP内に埋め込む
    _op_divider = ""
    if _new_count > 0 and len(thread.res_list) >= 2 and thread.res_list[1].is_new:
        _op_divider = (
            f'<div class="new-res-divider">'
            f'─────── 新着ここから {_new_count}件 ───────'
            f'</div>'
        )
        _divider_inserted = True
    for i, res in enumerate(_draw_list):
        # 最初の is_new レスの直前に仕切り線を挿入（OP直後以外）
        if _new_count > 0 and not _divider_inserted and res.is_new and not res.is_op:
            rows.append(
                f'<div class="new-res-divider">'
                f'─────── 新着ここから {_new_count}件 ───────'
                f'</div>'
            )
            _divider_inserted = True
        rows.append(render_res(res, is_op=(i == 0), img_list=img_list,
                               uploaders=uploaders,
                               ng_filter=ng_filter, ng_settings=ng_settings,
                               hidden_nos=hidden_nos,
                               id_counts=id_counts,
                               has_name_field=_has_name,
                               my_nos=my_nos,
                               id_warn_count=id_warn_count,
                               del_nos=del_nos,
                               ng_reveal=ng_reveal,
                               divider_html=_op_divider if i == 0 else ""))
    rows.append('<div class="thread-end"></div>')
    # 落ちかけ判定: contdispを赤字にするJSが存在する = thread.is_expiring
    # 仮赤字(pseudo_expiring)設定ONで保存残りが少ない場合も同じバナーを出す
    is_expiring = thread.is_expiring or pseudo_expiring
    if is_expiring:
        rows.append(
            f'<div class="expiry-banner">'
            f'このスレは古いので、もうすぐ消えます。'
            f'</div>'
        )
    body = "\n".join(rows)
    # ── body クラス（IDの色分け） ───────────────────────────────────────
    op_email = (thread.res_list[0].email or "").strip() if thread.res_list else ""
    any_id   = any(r.id_str for r in thread.res_list)
    # 「ID表示」「id表示」など大文字小文字混在に対応
    if op_email.lower() == "id表示" and any_id:
        body_class = ' class="id-board"'         # 通常IDあり板
    elif any_id:
        body_class = ' class="op-no-id"'         # OP無IDなのにIDあり
    else:
        body_class = ''
    _usr = f'<style>{user_css}</style>' if user_css else ''
    _scroll_js = _make_scroll_bottom_js(scroll_bottom_count, scroll_top_count)
    # テーマCSS変数を注入（ThemeManagerが利用可能なら）
    try:
        from futaba2b_const import ThemeManager as _TM
        _theme_vars = f'<style>{_TM.thread_css_vars()}</style>'
    except Exception:
        _theme_vars = ''
    html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<style>{THREAD_CSS}</style>'
        f'{_theme_vars}'
        f'{_usr}'
        f'<script>{ID_POPUP_JS}</script>'
        f'{WEBCHANNEL_JS}'
        f'<script>{_scroll_js}</script>'
        f'</head><body{body_class}>{body}{footer_html}</body></html>'
    )
    return html, img_list


# ══════════════════════════════════════════════════════════════════════════════
# カタログ HTML 生成
# ══════════════════════════════════════════════════════════════════════════════

def _make_scroll_bottom_js(n: int = 5, top_n: int = 0) -> str:
    """スクロール末尾/先頭検知JSを返す（<script>タグなし、中身のみ）
    n=末尾スクロール更新の必要回数（0=無効）, top_n=先頭スクロール更新の必要回数（0=無効）"""
    n = int(n); top_n = int(top_n)
    return (
        f'(function(){{'
        f'  var NEED={n},TOPNEED={top_n},THRESHOLD=80,COOLDOWN=1000;'
        f'  var count=0,topCount=0,cooling=false,_indTimer=null;'
        f'  function _getInd(){{'
        f'    var el=document.getElementById("_scroll_ind");'
        f'    if(!el){{'
        f'      el=document.createElement("div");el.id="_scroll_ind";'
        f'      el.style.cssText="position:fixed;right:18px;background:rgba(0,0,0,.62);'
        f'color:#fff;font-size:11pt;font-weight:bold;padding:5px 13px;border-radius:16px;'
        f'pointer-events:none;opacity:0;transition:opacity 0.18s;z-index:99999;white-space:nowrap;";'
        f'      document.body.appendChild(el);'
        f'    }}'
        f'    return el;'
        f'  }}'
        f'  function _setIndPos(el,atTop){{'
        f'    if(atTop){{el.style.top="18px";el.style.bottom="auto";}}'
        f'    else{{el.style.bottom="18px";el.style.top="auto";}}'
        f'  }}'
        f'  function _showInd(r,need,atTop){{'
        f'    var el=_getInd();_setIndPos(el,atTop);'
        f'    if(r<=0){{el.textContent="\U0001f504 更新";el.style.opacity="1";'
        f'      clearTimeout(_indTimer);_indTimer=setTimeout(function(){{el.style.opacity="0";}},900);'
        f'    }}else{{'
        f'      var done=need-r,dots="\u25cf".repeat(done)+"\u25cb".repeat(r);'
        f'      el.textContent=dots+" 更新";el.style.opacity="1";'
        f'      clearTimeout(_indTimer);_indTimer=setTimeout(function(){{el.style.opacity="0";}},1200);'
        f'    }}'
        f'  }}'
        f'  window._scrollBottomSetCount=function(n){{'
        f'    NEED=Math.max(0,parseInt(n)||0);count=0;'
        f'    var el=document.getElementById("_scroll_ind");if(el)el.style.opacity="0";'
        f'  }};'
        f'  window._scrollTopSetCount=function(n){{'
        f'    TOPNEED=Math.max(0,parseInt(n)||0);topCount=0;'
        f'    var el=document.getElementById("_scroll_ind");if(el)el.style.opacity="0";'
        f'  }};'
        f'  window.addEventListener("wheel",function(e){{'
        f'    if(cooling)return;'
        f'    var fromBottom=document.documentElement.scrollHeight-window.scrollY-window.innerHeight;'
        f'    if(NEED>0&&fromBottom<=THRESHOLD&&e.deltaY>0){{'
        f'      topCount=0;count++;'
        f'      if(count>=NEED){{'
        f'        count=0;cooling=true;_showInd(0,NEED,false);'
        f'        setTimeout(function(){{cooling=false;}},COOLDOWN);'
        f'        if(typeof _b==="function")_b("scrollBottom",[]);'
        f'      }}else{{_showInd(NEED-count,NEED,false);}}'
        f'    }}else if(TOPNEED>0&&window.scrollY<=THRESHOLD&&e.deltaY<0){{'
        f'      count=0;topCount++;'
        f'      if(topCount>=TOPNEED){{'
        f'        topCount=0;cooling=true;_showInd(0,TOPNEED,true);'
        f'        setTimeout(function(){{cooling=false;}},COOLDOWN);'
        f'        if(typeof _b==="function")_b("scrollTop",[]);'
        f'      }}else{{_showInd(TOPNEED-topCount,TOPNEED,true);}}'
        f'    }}else{{if(count!==0||topCount!==0){{count=0;topCount=0;var el=document.getElementById("_scroll_ind");if(el)el.style.opacity="0";}}}}'
        f'  }},{{passive:true}});'
        f'}})();'
    )


def catalog_to_html(entries: list, char_limit: int = 6, img_size: int = 84,
                     cols: int = 0, read_counts: dict = None,
                     thread_read_counts: dict = None,
                     search_sections=None,
                     user_css: str = "",
                     ng_filter=None, ng_settings=None,
                     nowrap_title: bool = False,
                     scroll_bottom_count: int = 30,
                     scroll_top_count: int = 0,
                     footer_html: str = "",
                     hover_zoom: bool = False,
                     hover_comment: bool = False,
                     show_email: bool = False,
                     show_badge: bool = False,
                     quarantine_section: bool = False,
                     common_id_section: bool = False) -> str:
    """
    search_sections: None | (matched_list, unmatched_list)
    指定された場合、matched を上にセクション表示する
    正規表現は呼び出し元で処理済み
    ng_filter: NgFilter インスタンス（Noneの場合はNG非表示なし）
    ng_settings: AppSettings インスタンス
    """

    # NG設定の取得
    _ng_pack        = getattr(ng_settings, "ng_catalog_pack",  True)  if ng_settings else True
    _ng_empty_mode  = getattr(ng_settings, "ng_catalog_empty", 2)     if ng_settings else 2
    # 逆NG通知色（未読/既読）
    _rev_unread_bg     = getattr(ng_settings, "ng_reverse_unread_bg",     "#9B59B6") if ng_settings else "#9B59B6"
    _rev_unread_border = getattr(ng_settings, "ng_reverse_unread_border", "")        if ng_settings else ""
    _rev_read_bg       = getattr(ng_settings, "ng_reverse_read_bg",       "#E8E8E8") if ng_settings else "#E8E8E8"
    _rev_read_border   = getattr(ng_settings, "ng_reverse_read_border",   "")        if ng_settings else ""
    _use_default       = getattr(ng_settings, "ng_reverse_use_default_color", True)  if ng_settings else True

    def _is_ng_entry(e) -> bool:
        """エントリがNGかどうか判定（ng_catalog_emptyモードに従う）"""
        if ng_filter is None:
            return False
        # URLの直接NG登録は ng_catalog_empty モードに関係なく常にNG
        url = getattr(e, "thread_url", "")
        if url and ng_settings and url in getattr(ng_settings, "ng_thread_urls", []):
            return True
        # ng_catalog_empty: 0=空タイトルのみNG, 1=NGワードに一致, 2=何もしない
        if _ng_empty_mode == 0:
            return not (e.title or "").strip()
        elif _ng_empty_mode == 1:
            return ng_filter.is_ng_catalog(e)
        return False

    def _make_entry(e) -> str:
        title    = (e.title or "")[:char_limit]
        tu       = _e(e.thumb_url or "")
        url      = _e(e.thread_url or "")
        img_elem = f'<img src="{tu}" loading="lazy">' if tu else ""
        res_cnt  = e.res_count
        prev_cnt = (read_counts or {}).get(e.thread_url, 0)
        delta    = max(0, res_cnt - prev_cnt) if prev_cnt else 0
        delta_s  = f'<span class="res-new">+{delta}</span>' if delta > 0 else ""
        # 隔離スレはjsonにレス数が無いため件数を非表示
        _rc_disp = '' if getattr(e, 'is_quarantine', False) else res_cnt


        # エントリのクラス決定
        if e.is_red:
            ecls = "entry red-thread"
        elif getattr(e, 'is_quasi_red', False):
            ecls = "entry quasi-red-thread"
        elif not tu:
            ecls = "entry text-only"
        else:
            ecls = "entry"

        # 既読判定（1度でも閲覧済みなら薄ピンク背景）
        if (thread_read_counts or {}).get(e.thread_url, 0) > 0:
            ecls += " already-read"

        # 逆NGチェック（デフォルト色ONならCSSに任せ、OFFなら既読/未読で色を切り替え）
        _extra_style = ""
        if ng_filter is not None:
            is_rev_ng = ng_filter.is_reverse_ng_catalog(e, title_chars=char_limit)
            if is_rev_ng:
                ecls += " reverse-ng"
                if not _use_default:
                    is_read = bool((thread_read_counts or {}).get(e.thread_url, 0) > 0)
                    if is_read:
                        _bg, _border = _rev_read_bg, _rev_read_border
                    else:
                        _bg, _border = _rev_unread_bg, _rev_unread_border
                    styles = []
                    if _border: styles.append(f"border-color:{_border}")
                    if _bg:     styles.append(f"background:{_bg}")
                    if styles:  _extra_style = ";".join(styles)

        style_attr = f' style="{_extra_style}"' if _extra_style else ""
        _title_style = (
            ' style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis"'
            if nowrap_title else ''
        )
        # メール欄バッジ（show_email=True: 全表示、False: 非表示）
        _em = (getattr(e, 'email', '') or '').strip()
        if show_email and _em:
            _email_badge = f'<span class="email-badge">[{_e(_em)}]</span>'
        else:
            _email_badge = ''

        # サムネ右上バッジ（show_badge=True のとき。緑=メール欄種別/赤=非ID表示でID実在）
        # 優先: ID表示(緑ID) > ID実在のみ(赤ID) > IP表示(緑IP) > ・3・(緑･3･) > 他(緑他)
        _thumb_badge = ''
        if show_badge:
            _embl  = _em.lower()
            _hasid = bool((getattr(e, 'op_id', '') or '').strip())
            _bt = ''; _bc = ''
            if _hasid:
                # 共通ID(mode=json id)が出ている → 赤IDバッジ優先（ID表示要求の有無を問わず）
                _bt, _bc = 'ID', 'cat-badge-r'
            elif _embl == 'id表示':
                _bt, _bc = 'ID', 'cat-badge-g'
            elif _embl == 'ip表示':
                _bt, _bc = 'IP', 'cat-badge-g'
            elif _em == '・3・':
                _bt, _bc = '･3･', 'cat-badge-g'
            elif _em:
                _bt, _bc = '他', 'cat-badge-g'
            if _bt:
                _thumb_badge = f'<span class="cat-badge {_bc}" title="{_e(_em) or "ID"}">{_bt}</span>'

        # 隔離スレは右下にオレンジの隔離バッジを表示
        _quar_badge = ''
        if getattr(e, 'is_quarantine', False):
            _quar_badge = '<span class="cat-badge-quar" title="隔離スレ">隔離</span>'

        # hover用データ属性とイベント
        _hover_attrs = ""
        if hover_zoom or hover_comment:
            _raw_comment = (e.title or '').replace('\\', '\\\\').replace("'", "\\'").replace('\n', ' ').replace('\r', '')
            _raw_thumb   = (e.thumb_url or '').replace("'", "\\'")
            _raw_url     = (e.thread_url or '').replace("'", "\\'")
            _hover_attrs = (
                f" onmouseenter=\"_b('catHoverEnter',['{_raw_url}','{_raw_thumb}','{_raw_comment}'])\""
                f" onmouseleave=\"_b('catHoverLeave',[])\""
            )

        return (
            f'<div class="{ecls}"{style_attr} '
            f'onclick="handleCatClick(\'{url}\',event)" '
            f'onmousedown="handleCatMouseDown(\'{url}\',event)"'
            f'{_hover_attrs}>' +
            f'<div class="entry-img">{img_elem}{_thumb_badge}{_quar_badge}</div>' +
            f'<div class="entry-title"{_title_style}>{_e(title)}</div>' +
            f'<div class="entry-foot"><span>{_rc_disp}</span><span>{delta_s}</span>'
            f'{_email_badge}</div>' +
            f'</div>'
        )

    def _filter_entries(elist):
        """NGエントリを詰める（ng_catalog_packがTrueの場合のみ除外）"""
        if not _ng_pack or ng_filter is None:
            return elist
        return [e for e in elist if not _is_ng_entry(e)]

    def _quar_section(quar):
        """隔離スレを最下部セクションとして描画"""
        if not quar:
            return ""
        return (
            f'<div class="sec-div"></div>'
            f'<div class="sec-hdr quar-hdr">↓ 隔離スレ ({len(quar)}件)</div>'
            + "".join(_make_entry(e) for e in quar)
        )

    def _pull_quar(elist):
        """quarantine_section=True のとき隔離スレを分離（(normal, quar) を返す）"""
        if not quarantine_section:
            return elist, []
        normal = [e for e in elist if not getattr(e, 'is_quarantine', False)]
        quar   = [e for e in elist if getattr(e, 'is_quarantine', False)]
        return normal, quar

    def _pull_common_id(elist):
        """common_id_section=True のとき op_id(ID) が出ているスレを分離して最下部送りにする。
        IDはランダム化されID別グルーピングが無意味になったため、ID別に分けず一括でまとめる。
        戻り値: (rest, [id付きentries...])（元の並び順を維持）"""
        if not common_id_section:
            return elist, []
        rest = []
        idres = []
        for e in elist:
            _cid = (getattr(e, 'op_id', '') or '').strip()
            if _cid:
                idres.append(e)
            else:
                rest.append(e)
        return rest, idres

    def _common_id_section(idres):
        """IDが出たスレを最下部に一括表示（ID別に分けず下にまとめるだけ）"""
        if not idres:
            return ""
        parts = ['<div class="sec-div"></div>',
                 f'<div class="sec-hdr cid-hdr">↓ IDが出たスレ ({len(idres)}件)</div>']
        parts.extend(_make_entry(e) for e in idres)
        return "".join(parts)

    if search_sections:
        matched, unmatched = search_sections
        matched   = _filter_entries(matched)
        unmatched = _filter_entries(unmatched)
        matched,   mq = _pull_quar(matched)
        # 共通IDを隔離より優先で抽出（隔離かつ共通IDのスレは共通ID側へ）
        unmatched, ucid = _pull_common_id(unmatched)
        unmatched, uq = _pull_quar(unmatched)
        parts = []
        if matched:
            parts.append(
                f'<div class="sec-hdr">'
                f'↓ 検索で見つけたスレ ({len(matched)}件)</div>'
            )
            parts.extend(_make_entry(e) for e in matched)
        parts.append('<div class="sec-div"></div>')
        parts.extend(_make_entry(e) for e in unmatched)
        parts.append(_common_id_section(ucid))
        parts.append(_quar_section(mq + uq))
        inner = "".join(parts)
    else:
        filtered = _filter_entries(entries)
        # 共通IDを隔離より優先で抽出（隔離かつ共通IDのスレは共通ID側へ）
        filtered, cid_groups = _pull_common_id(filtered)
        filtered, quar = _pull_quar(filtered)
        inner = ("".join(_make_entry(e) for e in filtered)
                 + _common_id_section(cid_groups)
                 + _quar_section(quar))

    if cols > 0:
        grid_style = (
            f"display:grid;"
            f"grid-template-columns:repeat({cols},{img_size}px);"
            f"gap:3px;padding:4px;width:100%;"
        )
    else:
        grid_style = "display:flex;flex-wrap:wrap;gap:3px;padding:4px;width:100%;"

    _usr_cat = f'<style>{user_css}</style>' if user_css else ''
    html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<style>{CATALOG_CSS}</style>'
        f'<style>:root{{--cell-w:{img_size}px}}</style>'
        f'{_usr_cat}'
        f'{WEBCHANNEL_JS}'
        f'<script>{_make_scroll_bottom_js(scroll_bottom_count, scroll_top_count)}</script>'
        f'</head><body>'
        f'<div id="grid" style="{grid_style}">{inner}</div>'
        f'{footer_html}'
        f'</body></html>'
    )
    return html
