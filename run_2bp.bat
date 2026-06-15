@echo off
cd /d "%~dp0"
title 2BP

python futaba2b_qt.py
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start 2BP.
    echo If this is your first time running 2BP, please run setup_2bp.bat first.
    echo.
    pause
)
