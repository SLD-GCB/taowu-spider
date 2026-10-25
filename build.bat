@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ============================================================
echo    Taowu Spider v12.1.2 - Build Script (PySide6/Qt)
echo ============================================================
echo.
python build.py
pause
