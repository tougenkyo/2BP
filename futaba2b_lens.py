# -*- coding: utf-8 -*-
"""futaba2b_lens.py ─ Google 画像検索（レンズ）を 2BP の窓で行う。

以前は外部ブラウザで lens.google.com/uploadbyurl?url=<ふたばの画像URL> を開いて
いた。この方法では Google が自分でふたばへ画像を取りに行く。ふたばは同じ IP
からの連続した読み込みを厳しく絞るので、Google が取りに行けない事が多く、
「この URL には画像がありません」になっていた（押し直すと通る事もある）。

ここでは画像そのものを Google へ送る（Google のページで「画像をアップロード」
するのと同じ）。画像は 2BP のキャッシュから読み、無ければ 2BP が取りに行く。

外部ブラウザでこれをする方法は無い。
  ・別のサイト（ローカルのページを含む）から Google へ画像を送ると 403 で断られる
    （Google のページの中から送った時だけ受け付ける）
  ・2BP が送って結果の URL だけを外部ブラウザで開いても、結果は送った側の
    cookie に結び付いていて表示されない
なので Google のページを 2BP の窓に開き、そのページの中から送る。

結果の中のリンク（Google 以外のサイト）は外部ブラウザで開く。
"""
from __future__ import annotations

import base64
import json
import threading

from PySide6.QtCore import Qt, QUrl, Signal, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSizePolicy,
)
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from futaba2b_const import UA

# 画像を送る前に開いておく Google のページ（ここから送ると同じサイト扱いになる）
GOOGLE_HOME = "https://www.google.com/?hl=ja"
UPLOAD_URL = "https://lens.google.com/v3/upload?ep=subb&re=df&hl=ja&st="
# これより大きい画像は縮めて送る（探すのに大きさは要らない。送る量を減らす）
MAX_SIDE = 1600
MAX_BYTES = 2 * 1024 * 1024

# この窓の中で開いてよいサイト（それ以外は外部ブラウザへ）
_GOOGLE_SUFFIXES = ("google.com", "google.co.jp", "gstatic.com",
                    "googleusercontent.com", "googleapis.com")


def is_google_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in _GOOGLE_SUFFIXES)


def _sniff(data: bytes) -> str:
    """画像の種類（jpeg/png/webp/gif/""）"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


def prepare_upload(data: bytes):
    """送る画像を用意する。(バイト列, MIME, ファイル名, 幅, 高さ) か、
    画像として読めない（動画など）時は None。
    jpeg/png/webp で大き過ぎなければそのまま送る。それ以外（gif など）や
    大きいものは、縮めて JPEG（透明があれば PNG）にする。"""
    if not data:
        return None
    img = QImage.fromData(QByteArray(data))
    if img.isNull():
        return None
    kind = _sniff(data)
    w, h = img.width(), img.height()
    if kind in ("jpeg", "png", "webp") and max(w, h) <= MAX_SIDE and len(data) <= MAX_BYTES:
        return data, f"image/{kind}", f"image.{'jpg' if kind == 'jpeg' else kind}", w, h
    if max(w, h) > MAX_SIDE:
        img = img.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if img.hasAlphaChannel() and kind != "jpeg":
        fmt, mime, name = "PNG", "image/png", "image.png"
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    else:
        fmt, mime, name = "JPEG", "image/jpeg", "image.jpg"
        if img.hasAlphaChannel():
            # JPEG は透明を持てない。黒くならないよう白の上に描く
            bg = QImage(img.size(), QImage.Format.Format_RGB32)
            bg.fill(QColor("white"))
            p = QPainter(bg); p.drawImage(0, 0, img); p.end()
            img = bg
    ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, fmt, 90)
    buf.close()
    return bytes(ba.data()), mime, name, img.width(), img.height()


def upload_js(data: bytes, mime: str, name: str, w: int, h: int) -> str:
    """Google のページの中で、画像をフォームで lens.google.com へ送る JS"""
    b64 = base64.b64encode(data).decode("ascii")
    return """(() => {
  const s = atob(%s); const u = new Uint8Array(s.length);
  for (let k = 0; k < s.length; k++) u[k] = s.charCodeAt(k);
  const f = new File([u], %s, {type: %s});
  const fm = document.createElement("form");
  fm.method = "POST"; fm.enctype = "multipart/form-data";
  fm.action = %s + Date.now();
  const i = document.createElement("input"); i.type = "file"; i.name = "encoded_image";
  const dt = new DataTransfer(); dt.items.add(f); i.files = dt.files; fm.appendChild(i);
  const d = document.createElement("input"); d.type = "hidden";
  d.name = "processed_image_dimensions"; d.value = %s; fm.appendChild(d);
  fm.style.display = "none";
  document.documentElement.appendChild(fm); fm.submit();
  return true;
})()""" % (json.dumps(b64), json.dumps(name), json.dumps(mime),
           json.dumps(UPLOAD_URL), json.dumps(f"{w},{h}"))


class _LensPage(QWebEnginePage):
    """Google 以外のサイトへ行くリンクは外部ブラウザで開く"""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if is_main_frame and url.scheme() in ("http", "https") \
                and not is_google_host(url.host()):
            from futaba2b_app_qt import _open_url
            _open_url(url.toString())
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, _type):
        # 新しいタブ・窓で開くリンク: 行き先が分かったら外部ブラウザで開いて捨てる
        return _ExternalCatcher(self.profile(), self.profile())


class _ExternalCatcher(QWebEnginePage):
    """新しい窓を開くリンクの受け皿。読み込まずに外部ブラウザへ回して消える"""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.scheme() not in ("http", "https"):
            return True     # about:blank など（行き先はまだ）
        from futaba2b_app_qt import _open_url
        _open_url(url.toString())
        self.deleteLater()
        return False


class LensSearchWindow(QWidget):
    """Google 画像検索の窓（1つを使い回す）。search(画像URL) で検索する"""

    _bytes_ready = Signal(int, object, str)    # 順番, 画像のバイト列(None=失敗), URL

    def __init__(self, fetcher, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Google 画像検索 - 2BP")
        self.resize(1100, 860)
        self._fetcher = fetcher
        self._seq = 0
        self._pending_js = ""      # Google のページを開き終えたら送る
        self._loading = False

        # off-the-record（名前なし）: cookie もキャッシュもディスクに残さない
        self._profile = QWebEngineProfile(self)
        self._profile.setHttpUserAgent(UA)
        self._page = _LensPage(self._profile, self._profile)   # page親=profile（先に消す）
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)

        bar = QWidget(); bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 3, 6, 3); bl.setSpacing(4)
        self._back = QPushButton("←"); self._back.setFixedWidth(32)
        self._back.setToolTip("戻る")
        self._back.clicked.connect(self._view.back)
        self._fwd = QPushButton("→"); self._fwd.setFixedWidth(32)
        self._fwd.setToolTip("進む")
        self._fwd.clicked.connect(self._view.forward)
        self._status = QLabel("")
        self._status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bl.addWidget(self._back); bl.addWidget(self._fwd); bl.addWidget(self._status, 1)

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        lay.addWidget(bar); lay.addWidget(self._view, 1)

        self._page.loadStarted.connect(self._on_load_started)
        self._page.loadFinished.connect(self._on_load_finished)
        self._bytes_ready.connect(self._on_bytes)

    # ── 検索 ────────────────────────────────────────────────────────────
    def search(self, img_url: str) -> None:
        """画像を読み（キャッシュ → 無ければふたばから）、Google へ送って検索する"""
        self._seq += 1
        seq = self._seq
        self._pending_js = ""
        self._set_status("画像を読み込んでいます…")
        self._present()
        fetcher = self._fetcher

        def _job():
            try:
                data = fetcher.fetch_image_bytes(img_url)
            except Exception as e:
                print(f"[Lens] 画像を読めませんでした [{img_url}]: {e}")
                data = None
            try:
                self._bytes_ready.emit(seq, data, img_url)
            except RuntimeError:
                pass    # 読んでいる間に窓が消えた
        threading.Thread(target=_job, daemon=True).start()

    def _on_bytes(self, seq: int, data, img_url: str) -> None:
        if seq != self._seq:
            return      # 後から別の画像で検索し直した
        if not data:
            self._set_status("画像を読み込めませんでした（消えた画像かもしれません）")
            return
        up = prepare_upload(data)
        if up is None:
            self._set_status("この画像は Google 画像検索に送れません（動画など）")
            return
        payload, mime, name, w, h = up
        js = upload_js(payload, mime, name, w, h)
        print(f"[Lens] 送ります  {img_url}  {w}x{h}  {len(payload) // 1024}KB  {mime}")
        self._set_status("Google へ画像を送っています…")
        if not self._loading and is_google_host(self._page.url().host()):
            self._page.runJavaScript(js, 0)
        else:
            # 同じサイトから送るため、先に Google のページを開く
            self._pending_js = js
            self._view.load(QUrl(GOOGLE_HOME))

    def _on_load_started(self) -> None:
        self._loading = True

    def _on_load_finished(self, ok: bool) -> None:
        self._loading = False
        js, self._pending_js = self._pending_js, ""
        if js:
            if ok and is_google_host(self._page.url().host()):
                self._page.runJavaScript(js, 0)
                return
            self._set_status("Google のページを開けませんでした")
            return
        if ok and self._page.url().path().startswith("/search"):
            self._set_status("")
        self._back.setEnabled(self._view.history().canGoBack())
        self._fwd.setEnabled(self._view.history().canGoForward())

    # ── 表示 ────────────────────────────────────────────────────────────
    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _present(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
