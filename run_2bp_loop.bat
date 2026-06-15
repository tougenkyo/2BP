@echo off
cd /d "%~dp0"
title 2BP - Auto Restart

:LOOP
echo.
echo ========================================
echo  Starting 2BP...
echo ========================================
echo.

python futaba2b_qt.py
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% == 0 (
    echo.
    echo 2BP exited normally.
    goto END
)

echo.
python -c "import sys,datetime; sys.stdout.reconfigure(encoding='utf-8'); now=datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S'); print('\033[91m\033[1m=========================================\033[0m'); print('\033[91m\033[1m  [CRASH] 2BP crashed!\033[0m'); print('\033[91m\033[1m  Date/Time : ' + now + '\033[0m'); print('\033[91m\033[1m  Exit Code : %EXIT_CODE%\033[0m'); print('\033[91m\033[1m=========================================\033[0m')"
echo.
echo Restarting in 5 seconds... (Ctrl+C to cancel)
echo.
timeout /t 5 /nobreak > nul

goto LOOP

:END
pause
