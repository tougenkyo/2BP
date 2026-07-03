"""
futaba2b_bridge.py ─ QWebChannel ブリッジ (JavaScript ↔ Python)
各 WebEngineView に 1 つずつ紐付ける。
"""
from __future__ import annotations
from PySide6.QtCore import QObject, Slot, Signal


class ThreadBridge(QObject):
    """スレッドビューの JS → Python コールバック"""

    # Python 側が受け取るシグナル
    quote_no_requested    = Signal(int)          # 番号クリック → >>No.NNNN
    quote_comment_requested = Signal(int)        # コメントクリック → >本文
    quote_img_requested   = Signal(int)          # 画像クリック → 画像URLを引用
    ng_requested          = Signal(int)          # NG クリック
    sodane_requested      = Signal(int)          # そうだね
    img_open_requested    = Signal(str, int)     # 画像タブを開く (url, idx)
    img_open_bg_requested = Signal(str, int)     # 画像タブをバックグラウンドで開く (url, idx)
    scroll_bottom_reached = Signal()             # スクロール末尾 → 更新トリガー
    scroll_top_reached    = Signal()             # スクロール先頭 → 更新トリガー
    scroll_count_updated  = Signal(int)          # スクロール残回数 (0=リセット)
    url_open_requested    = Signal(str)          # 外部ブラウザ
    futaba_thread_open_requested = Signal(str)  # ふたばスレをタブで開く
    reply_window_needed   = Signal()             # レスウィンドウを開く
    unread_state_changed  = Signal(bool)           # new-res有無通知
    del_requested         = Signal(int)          # del リンク
    report_del_requested  = Signal(int, bool)    # 削除依頼 (no, hide)
    delete_res_requested  = Signal(int, str, bool, bool)  # 記事削除 (no, pwd, onlyimg, hide)
    gallery_img_requested = Signal(int)          # 画像ギャラリー → 画像タブ
    play_video_requested  = Signal(str)          # ネイティブ動画再生
    quote_text_requested  = Signal(str)          # テキスト選択引用
    ng_text_requested     = Signal(str)          # テキスト選択NG
    extract_text_requested = Signal(str)         # テキスト選択抽出（ステータスバーへ転送）
    extract_clear_requested = Signal()           # 抽出ポップアップの×で抽出フィールドをクリア
    copy_text_requested   = Signal(str)          # テキスト選択コピー
    ng_image_requested    = Signal(str)          # img_url
    url_open_external_requested = Signal(str)    # 外部ブラウザで直接開く
    save_selected_images_requested = Signal(str, list)  # 画像モード一括保存 (folder, urls)

    def __init__(self, parent=None):
        super().__init__(parent)

    # ─ JS から呼ばれるスロット ─────────────────────────────────────────────

    @Slot(int)
    def quoteNo(self, no: int):
        self.quote_no_requested.emit(no)

    @Slot(int)
    def quoteComment(self, no: int):
        self.quote_comment_requested.emit(no)

    @Slot(int)
    def quoteImg(self, no: int):
        """フッター「画像」クリック → そのレスの画像URLを引用"""
        self.quote_img_requested.emit(no)

    @Slot(int)
    def ngRes(self, no: int):
        self.ng_requested.emit(no)

    @Slot(int)
    def sodane(self, no: int):
        self.sodane_requested.emit(no)

    @Slot(str, int)
    def openImg(self, url: str, idx: int):
        self.img_open_requested.emit(url, idx)

    @Slot(str, int)
    def openImgBg(self, url: str, idx: int):
        """中クリック → バックグラウンドタブで画像を開く"""
        self.img_open_bg_requested.emit(url, idx)

    @Slot(str)
    def openUrl(self, url: str):
        # ふたば内スレURLはタブで開く、それ以外は外部ブラウザ
        import re as _re
        if _re.search(r'https?://[a-z0-9]+\.2chan\.net/[^/]+/res/\d+\.htm', url):
            self.futaba_thread_open_requested.emit(url)
        else:
            self.url_open_requested.emit(url)

    @Slot(int)
    def delRes(self, no: int):
        self.del_requested.emit(no)

    @Slot(int, bool)
    def reportDel(self, no: int, hide: bool):
        """JS: reportDel(no, hide) → 削除依頼送信"""
        self.report_del_requested.emit(no, hide)

    @Slot(int, str, bool, bool)
    def deleteRes(self, no: int, pwd: str, onlyimg: bool, hide: bool):
        """JS: deleteRes(no, pwd, img, hide) → 記事削除"""
        self.delete_res_requested.emit(no, pwd, onlyimg, hide)

    @Slot(int)
    def openGalleryImg(self, idx: int):
        self.gallery_img_requested.emit(idx)

    @Slot(str, 'QVariantList')
    def saveSelectedImages(self, folder: str, urls):
        """JS: 画像モードで選択した画像の一括保存 (保存先フォルダ, 画像URL配列)"""
        self.save_selected_images_requested.emit(folder, list(urls or []))

    @Slot(str)
    def playVideo(self, url: str):
        """JS からネイティブ動画再生を要求"""
        self.play_video_requested.emit(url)

    @Slot(str)
    def quoteText(self, text: str):
        """テキスト選択 → 引用として返信ウィンドウに渡す"""
        self.quote_text_requested.emit(text)

    @Slot(str)
    def ngText(self, text: str):
        """テキスト選択 → NGワード追加ダイアログを開く"""
        self.ng_text_requested.emit(text)

    @Slot(str)
    def extractText(self, text: str):
        """テキスト選択 → ステータスバーの抽出テキストボックスに転送"""
        self.extract_text_requested.emit(text)

    @Slot()
    def clearExtract(self):
        """抽出ポップアップの×ボタン → ツールバーの抽出フィールドをクリア"""
        self.extract_clear_requested.emit()

    @Slot(str)
    def copyText(self, text: str):
        """テキスト選択 → クリップボードにコピー"""
        self.copy_text_requested.emit(text)

    @Slot(str)
    def ngImage(self, url: str):
        """画像右クリック → NG画像として登録 (img_url)"""
        self.ng_image_requested.emit(url)

    @Slot(str)
    def openUrlExternal(self, url: str):
        """外部ブラウザで直接開く"""
        self.url_open_external_requested.emit(url)

    @Slot()
    def scrollBottom(self):
        """スクロール末尾5回検知 → 更新トリガー"""
        self.scroll_bottom_reached.emit()

    @Slot()
    def scrollTop(self):
        """スクロール先頭検知 → 更新トリガー"""
        self.scroll_top_reached.emit()

    @Slot(int)
    def scrollCountUpdate(self, remaining: int):
        """末尾スクロールの残り回数通知 (0=リセット)"""
        self.scroll_count_updated.emit(remaining)

    @Slot(bool)
    def notifyUnread(self, has_unread: bool):
        """new-res（赤帯）が1件以上あるかどうかを通知"""
        self.unread_state_changed.emit(has_unread)


class CatalogBridge(QObject):
    """カタログビューの JS → Python コールバック"""

    thread_open_requested    = Signal(str)   # スレッド URL
    thread_bg_open_requested = Signal(str)   # バックグラウンドで開く
    url_open_requested       = Signal(str)   # 外部ブラウザ
    copy_to_clipboard_requested = Signal(str)  # クリップボードコピー
    add_thread_ng_requested  = Signal(str)   # スレッドURLをNGに追加
    catalog_del_requested    = Signal(str)   # 削除依頼(del) → スレッドURL
    scroll_bottom_reached    = Signal()      # スクロール末尾 → 更新トリガー
    scroll_top_reached       = Signal()      # スクロール先頭 → 更新トリガー
    cat_hover_enter          = Signal(str, str, str)  # (url, thumb_url, comment)
    cat_hover_leave          = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

    @Slot(str)
    def openThread(self, url: str):
        self.thread_open_requested.emit(url)

    @Slot(str)
    def openThreadBg(self, url: str):
        self.thread_bg_open_requested.emit(url)

    @Slot(str)
    def openUrl(self, url: str):
        self.url_open_requested.emit(url)

    @Slot(str)
    def copyToClipboard(self, text: str):
        self.copy_to_clipboard_requested.emit(text)

    @Slot(str)
    def addThreadNg(self, url: str):
        self.add_thread_ng_requested.emit(url)

    @Slot(str)
    def catalogDel(self, url: str):
        self.catalog_del_requested.emit(url)

    @Slot()
    def scrollBottom(self):
        """スクロール末尾5回検知 → 更新トリガー"""
        self.scroll_bottom_reached.emit()

    @Slot()
    def scrollTop(self):
        """スクロール先頭検知 → 更新トリガー"""
        self.scroll_top_reached.emit()

    @Slot(str, str, str)
    def catHoverEnter(self, url: str, thumb_url: str, comment: str):
        """カタログエントリにマウスオーバー"""
        self.cat_hover_enter.emit(url, thumb_url, comment)

    @Slot()
    def catHoverLeave(self):
        """カタログエントリからマウスアウト"""
        self.cat_hover_leave.emit()
