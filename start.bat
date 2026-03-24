@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Feishu-OpenCode Bridge

echo ========================================
echo    Feishu - OpenCode Bridge
echo ========================================
echo.

cd /d "%~dp0"

python feishu_bridge.py

if errorlevel 1 (
    echo.
    echo [ERROR] Bridge exited with error
    pause
)
