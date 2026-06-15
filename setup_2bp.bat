@echo off
setlocal
cd /d "%~dp0"
title 2BP - Initial Setup

echo ========================================
echo  2BP (futaba2b) - Initial Setup
echo ========================================
echo.

REM --- Locate Python ---
set PY=
where python >nul 2>&1
if not errorlevel 1 set PY=python
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 set PY=py
)

if not defined PY (
    echo [ERROR] Python was not found on this PC.
    echo.
    echo Please install Python 3.10 or later from the link below.
    echo During installation, be sure to check
    echo   "Add python.exe to PATH" ^(or "Add Python to PATH"^)
    echo.
    echo     https://www.python.org/downloads/
    echo.
    echo After installing Python, run this setup again.
    echo.
    pause
    exit /b 1
)

echo Found Python:
%PY% --version
echo.

echo [1/2] Upgrading pip...
%PY% -m pip install --upgrade pip
echo.

echo [2/2] Installing required packages from requirements.txt...
echo  ^(PySide6, requests, beautifulsoup4, lxml, Pillow, psutil^)
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install required packages.
    echo Please check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Setup complete!
echo  Run "run_2bp.bat" to start 2BP.
echo ========================================
echo.
pause
