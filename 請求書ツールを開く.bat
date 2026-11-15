@echo off
rem Double-click to open the invoice reader screen in the browser.
rem chcp 65001: show Japanese messages (this file is saved as UTF-8).
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 準備がまだできていません。管理者に README.md の「準備」をしてもらってください。
  echo 何かキーを押すと閉じます。
  pause >nul
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 scripts\open_app.py
if errorlevel 1 (
  echo 何かキーを押すと閉じます。
  pause >nul
)
