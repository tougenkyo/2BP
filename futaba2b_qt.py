"""
futaba2b_qt.py ─ PySide6 版エントリポイント
使い方: python futaba2b_qt.py
"""
import os, sys


def _exit_code(rc) -> int:
    """Windows の終了コード（落ちた時の 0xC0000005 等）を sys.exit に渡せる値にする。
    そのままだと大きすぎて渡せない（.bat の %errorlevel% には負の数で出る）"""
    try:
        rc = int(rc)
    except (TypeError, ValueError):
        return 1
    if rc >= 0x80000000:
        rc -= 0x100000000
    return rc


def _respawn_without_console():
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

    以前はログ出力ONの時だけコンソールに残していたが、それだとコンソールを
    閉じた（誤って閉じた）時に 2BP まで終了する。ログは 2BP の中の
    ログウインドウとテキストファイルに出すようにした（futaba2b_log.py）ので、
    設定にかかわらず、いつもコンソールを持たずに動かす。

    run_2bp_loop.bat から（BP2_WAIT=1）の時は、起動し直した 2BP が終わるまで
    待って、その終了コードを返す（落ちた時に .bat が起動し直せるように）。
    この時もコンソールを閉じて終わるのは待っている側だけで、2BP は終わらない。

    起動し直した時: 呼び出し元がそのまま終了に使う終了コード（待たない時は 0）。
    何もしなかった時: None（今までどおり自分で起動する）。
    開発などでコンソールにログを出したい時は BP2_NO_RESPAWN=1 で起動する。"""
    if not sys.platform.startswith("win"):
        return None
    if os.environ.get("BP2_NO_RESPAWN"):
        return None            # 起動し直した先／コンソールに出したい時
    try:
        import ctypes
        if not ctypes.windll.kernel32.GetConsoleWindow():
            return None        # そもそもコンソールが無い（pythonw 等）
    except Exception:
        return None
    # PySide6 が入っていない時は起動し直さない。黙って消えると原因が分からず、
    # run_2bp.bat の「setup_2bp.bat を先に実行してください」も出せなくなる。
    try:
        import importlib.util
        if importlib.util.find_spec("PySide6") is None:
            return None
    except Exception:
        return None
    _pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(_pyw):
        return None            # pythonw が無い環境。従来の「隠す」方に任せる
    try:
        import subprocess
        _env = dict(os.environ)
        _env["BP2_NO_RESPAWN"] = "1"     # 起動し直しの繰り返しを止める
        _env.pop("BP2_WAIT", None)
        _here = os.path.abspath(__file__)
        # 標準出力は捨て場(NUL)で渡す。何も渡さないと起動し直した先では C の
        # stderr が最初から無く、Chromium のエラー等をログへ付け替えられない
        _proc = subprocess.Popen(
            [_pyw, _here] + sys.argv[1:],
            cwd=os.path.dirname(_here), env=_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception:
        return None            # 起動し直せなかった。このまま自分で起動する
    if not os.environ.get("BP2_WAIT"):
        return 0
    try:
        print("2BP is running in its own window. Closing this window does not close"
              " 2BP (only the auto-restart stops).", flush=True)
    except Exception:
        pass
    try:
        return _exit_code(_proc.wait())
    except KeyboardInterrupt:
        return 0               # 待つのをやめた（2BP はそのまま動いている）


def _report_startup_error() -> None:
    """起動に失敗した事を知らせる。

    コンソールが無いと、読み込みに失敗しても（ライブラリが足りない等）黙って
    消えるだけで原因が分からない。ログに残し、メッセージでも出す。"""
    import traceback
    text = traceback.format_exc()
    try:
        print(text, flush=True)    # ログへ（テキストに書いていればファイルにも）
    except Exception:
        pass
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return                 # コンソールに出ている
        ctypes.windll.user32.MessageBoxW(
            None,
            "2BP を起動できませんでした。\n\n" + text[-1500:]
            + "\n\n初めて使う時・更新した後は、setup_2bp.bat を実行してから"
              "もう一度起動してください。",
            "2BP", 0x10)           # MB_ICONERROR
    except Exception:
        pass


if __name__ == "__main__":
    _rc = _respawn_without_console()
    if _rc is not None:
        sys.exit(_rc)
    # print の出力を 2BP の中（ログウインドウ・テキストファイル）へ集める。
    # 本体を読み込む前に入れて、読み込み中の出力も取りこぼさない。
    # （PySide6 が無い時はここで失敗する。その時はコンソールのまま動いていて、
    #   本体の読み込みで出るエラーは今までどおりコンソールに出る）
    try:
        import futaba2b_log
        futaba2b_log.install()
    except Exception:
        pass
    try:
        from futaba2b_main_window import main
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        _report_startup_error()
        sys.exit(1)
