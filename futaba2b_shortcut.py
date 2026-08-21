# -*- coding: utf-8 -*-
"""Windows のショートカット(.lnk)を作る ─ タスクバーへのピン留め用。

2BP は python.exe から起動するため、実行中の窓をそのままタスクバーに
ピン留めすると「Python 本体」が登録されてしまい、そこから 2BP を起動できない。

Windows はタスクバーのボタンとピン留めを AppUserModelID で結び付けている。
そこで

  ・アプリ側は起動時に SetCurrentProcessExplicitAppUserModelID(APP_ID)
  ・ショートカット側にも同じ APP_ID を書き込む

の両方をそろえる。こうするとショートカットをピン留めしたアイコンと実行中の
窓が同じボタンにまとまり、閉じてもピン留めが残り、そこから起動できる。

.lnk へ AppUserModelID を書き込むには COM(IShellLink + IPropertyStore)が要る。
pywin32 は 2BP の必須依存ではないので ctypes で直接叩く。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# タスクバーの識別子。アプリ側とショートカット側で必ず同じ値を使う
APP_ID = "tougenkyo.2BP"


# ── アプリ側 ────────────────────────────────────────────────────────────────

def set_process_app_id(app_id: str = APP_ID) -> bool:
    """このプロセスのタスクバー識別子を決める。ウィンドウを作る前に呼ぶこと。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False


# ── アイコン ────────────────────────────────────────────────────────────────

def build_ico(png_path, out_path) -> bool:
    """テーマの icon.png から .ico を作る。ショートカットは png を扱えない。

    元画像は 42x41 と小さく正方形でもないので、透明な正方形に載せてから
    拡大する（そのまま拡大すると横に伸びる）。Pillow があれば多サイズ、
    無ければ Qt で 1 枚だけ書き出す。"""
    png_path, out_path = Path(png_path), Path(out_path)
    if not png_path.exists():
        return False
    try:
        from PIL import Image
        im = Image.open(str(png_path)).convert("RGBA")
        side = max(im.size)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        sq = sq.resize((256, 256), Image.LANCZOS)
        sq.save(str(out_path), format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                       (64, 64), (128, 128), (256, 256)])
        return out_path.exists()
    except Exception:
        pass
    try:
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt as _Qt
        im = QImage(str(png_path))
        if im.isNull():
            return False
        side = max(im.width(), im.height())
        sq = QImage(side, side, QImage.Format.Format_ARGB32)
        sq.fill(0)
        from PySide6.QtGui import QPainter
        p = QPainter(sq)
        p.drawImage((side - im.width()) // 2, (side - im.height()) // 2, im)
        p.end()
        sq = sq.scaled(64, 64, _Qt.AspectRatioMode.KeepAspectRatio,
                       _Qt.TransformationMode.SmoothTransformation)
        return bool(sq.save(str(out_path), "ICO"))
    except Exception:
        return False


# ── .lnk ────────────────────────────────────────────────────────────────────

def create_shortcut(lnk_path, target: str, args: str = "", workdir: str = "",
                    icon: str = "", desc: str = "", app_id: str = APP_ID) -> bool:
    """.lnk を作る。app_id を渡すと AppUserModelID も書き込む。

    IShellLinkW / IPropertyStore / IPersistFile を ctypes で直接呼ぶ。
    vtable の並びは Windows SDK のヘッダ順そのままで、番号がずれると
    別のメソッドを呼んでしまうため触るときは注意すること。"""
    if not sys.platform.startswith("win"):
        return False
    import ctypes
    from ctypes import wintypes, POINTER, byref, c_void_p, c_wchar_p, Structure

    class GUID(Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

        def __init__(self, s: str):
            super().__init__()
            ctypes.oledll.ole32.CLSIDFromString(s, byref(self))

    class PROPERTYKEY(Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(Structure):
        # 先頭 8 バイトが vt + 予約、そのあとが値の共用体
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT),
                    ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                    ("data", ctypes.c_byte * 16)]

    CLSID_ShellLink    = GUID("{00021401-0000-0000-C000-000000000046}")
    IID_IShellLinkW    = GUID("{000214F9-0000-0000-C000-000000000046}")
    IID_IPersistFile   = GUID("{0000010B-0000-0000-C000-000000000046}")
    IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    PKEY_AppUserModel_ID = PROPERTYKEY(
        GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
    CLSCTX_INPROC_SERVER = 1
    VT_LPWSTR = 31

    def call(ptr, index, *a, argtypes=()):
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtbl[index])
        return fn(ptr, *a)

    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitialize(None)
    except OSError:
        pass                      # 既に別のモードで初期化済みでも続行できる
    psl = ppf = pps = None
    try:
        psl = c_void_p()
        ole32.CoCreateInstance(byref(CLSID_ShellLink), None,
                               CLSCTX_INPROC_SERVER,
                               byref(IID_IShellLinkW), byref(psl))
        call(psl, 20, c_wchar_p(target), argtypes=(c_wchar_p,))   # SetPath
        if args:
            call(psl, 11, c_wchar_p(args), argtypes=(c_wchar_p,))  # SetArguments
        if workdir:
            call(psl, 9, c_wchar_p(workdir), argtypes=(c_wchar_p,))  # SetWorkingDirectory
        if desc:
            call(psl, 7, c_wchar_p(desc[:259]), argtypes=(c_wchar_p,))  # SetDescription
        if icon:
            call(psl, 17, c_wchar_p(icon), 0,
                 argtypes=(c_wchar_p, ctypes.c_int))              # SetIconLocation

        if app_id:
            pps = c_void_p()
            if call(psl, 0, byref(IID_IPropertyStore), byref(pps),
                    argtypes=(POINTER(GUID), POINTER(c_void_p))) == 0:
                pv = PROPVARIANT()
                sz = c_void_p()
                # InitPropVariantFromString は inline 関数で DLL に無いので手で組む
                ctypes.oledll.shlwapi.SHStrDupW(c_wchar_p(app_id), byref(sz))
                pv.vt = VT_LPWSTR
                ctypes.memmove(ctypes.byref(pv, 8), byref(sz),
                               ctypes.sizeof(c_void_p))
                call(pps, 6, byref(PKEY_AppUserModel_ID), byref(pv),
                     argtypes=(POINTER(PROPERTYKEY), POINTER(PROPVARIANT)))  # SetValue
                call(pps, 7, argtypes=())                                    # Commit
                ctypes.oledll.ole32.PropVariantClear(byref(pv))

        ppf = c_void_p()
        call(psl, 0, byref(IID_IPersistFile), byref(ppf),
             argtypes=(POINTER(GUID), POINTER(c_void_p)))
        hr = call(ppf, 6, c_wchar_p(str(lnk_path)), 1,
                  argtypes=(c_wchar_p, wintypes.BOOL))            # Save
        return hr == 0 and os.path.exists(str(lnk_path))
    except Exception:
        return False
    finally:
        for p in (pps, ppf, psl):
            if p:
                try:
                    call(p, 2, argtypes=())                       # Release
                except Exception:
                    pass


# ── 置き場所 ────────────────────────────────────────────────────────────────

def _shell_folder(csidl: int) -> "Path | None":
    """CSIDL からフォルダを引く。OneDrive にデスクトップを移していても正しく取れる。"""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(
                None, csidl, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return None


def desktop_dir() -> "Path | None":
    return _shell_folder(0x0010)        # CSIDL_DESKTOPDIRECTORY


def start_menu_dir() -> "Path | None":
    return _shell_folder(0x0002)        # CSIDL_PROGRAMS


# ── まとめ ──────────────────────────────────────────────────────────────────

def install_shortcuts(app_dir, icon_png="", desktop: bool = True,
                      start_menu: bool = True, name: str = "2BP") -> dict:
    """デスクトップ／スタートメニューに 2BP のショートカットを置く。

    Returns: {"created": [作れた.lnkのパス], "failed": [作れなかった場所],
              "icon": 使ったアイコン, "target": 起動に使う python.exe}"""
    app_dir = Path(app_dir)
    script  = app_dir / "futaba2b_qt.py"
    target  = sys.executable or "python.exe"
    ico = ""
    if icon_png:
        cand = app_dir / f"{name}.ico"
        if build_ico(icon_png, cand):
            ico = str(cand)
    out = {"created": [], "failed": [], "icon": ico, "target": target}
    places = []
    if desktop:
        places.append(desktop_dir())
    if start_menu:
        places.append(start_menu_dir())
    for d in places:
        if d is None:
            out["failed"].append("(場所が分かりませんでした)")
            continue
        lnk = d / f"{name}.lnk"
        ok = create_shortcut(lnk, target, f'"{script}"', str(app_dir), ico,
                             "2BP ─ ふたばちゃんねる専用ブラウザ", APP_ID)
        (out["created"] if ok else out["failed"]).append(str(lnk))
    return out
