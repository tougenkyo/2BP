"""
futaba2b_qt.py ─ PySide6 版エントリポイント
使い方: python futaba2b_qt.py
"""
import os, sys


def _respawn_without_console() -> bool:
    """コンソールを持たない pythonw.exe で自分を起動し直す。

    ログ出力OFFの時、2BP は起動時にコンソールの窓を ShowWindow(SW_HIDE) で
    隠していた。ところが Windows 11 の既定ターミナルは Windows ターミナルで、
    そこで GetConsoleWindow() が返すのは見えない代理の窓
    （PseudoConsoleWindow）でしかない。見えている窓は別プロセスの持ち物なので
    いくら隠しても消えず、しかもその窓を閉じると、同じコンソールにぶら下がって
    いる 2BP まで道連れで終了する（Windows 10 から 11 に乗り換えたら黒い窓が
    消えなくなった、という報告の原因）。

    隠すのではなく、最初からコンソールを持たない pythonw.exe で起動し直す。
    窓が無ければ隠す必要も、閉じられて落ちる事も無い。ターミナルの種類にも
    Windows の版にも左右されない。

    起動し直した時 True（呼び出し元はそのまま終了してよい）。
    何もしなかった時 False（今までどおり自分で起動する）。"""
    if not sys.platform.startswith("win"):
        return False
    if os.environ.get("BP2_NO_RESPAWN"):
        return False           # 起動し直した先／コンソールを見せたい .bat から
    try:
        import ctypes
        if not ctypes.windll.kernel32.GetConsoleWindow():
            return False       # そもそもコンソールが無い（pythonw 等）
    except Exception:
        return False
    # 設定を先読み。ログ出力ONなら今までどおりコンソールを出したままにする
    try:
        import json
        _sf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "futaba2b_settings.json")
        if os.path.exists(_sf):
            with open(_sf, encoding="utf-8") as _fp:
                if bool(json.load(_fp).get("show_console", False)):
                    return False
    except Exception:
        pass                   # 読めない時は既定(OFF)扱い＝コンソールを出さない
    # PySide6 が入っていない時は起動し直さない。黙って消えると原因が分からず、
    # run_2bp.bat の「setup_2bp.bat を先に実行してください」も出せなくなる。
    try:
        import importlib.util
        if importlib.util.find_spec("PySide6") is None:
            return False
    except Exception:
        return False
    _pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(_pyw):
        return False           # pythonw が無い環境。従来の「隠す」方に任せる
    try:
        import subprocess
        _env = dict(os.environ)
        _env["BP2_NO_RESPAWN"] = "1"     # 起動し直しの繰り返しを止める
        _here = os.path.abspath(__file__)
        subprocess.Popen(
            [_pyw, _here] + sys.argv[1:],
            cwd=os.path.dirname(_here), env=_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception:
        return False           # 起動し直せなかった。このまま自分で起動する
    return True


if __name__ == "__main__":
    if not _respawn_without_console():
        from futaba2b_main_window import main
        main()
