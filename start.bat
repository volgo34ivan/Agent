@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "PYTHONIOENCODING=utf-8"
title Agent launcher
pushd "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"
if /i "%MODE%"=="check" set "MODE=--check"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto have_py

where python >nul 2>nul
if errorlevel 1 (
    echo [ERR] Python not found.
    echo       Install Python 3.10+ with "Add to PATH",
    echo       or create venv: py -m venv .venv
    pause
    popd
    exit /b 1
)
set "PY=python"

:have_py
echo [INFO] Python: %PY%
echo [INFO] Folder: %~dp0
echo [INFO] Mode:   %MODE%
echo.
"%PY%" start.py %MODE%
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [ERR] Exit code %RC%
    pause
)
popd
endlocal
exit /b %RC%
