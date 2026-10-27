@echo off
REM 門市主機啟動腳本：校時後啟動點餐系統 (由「工作排程器」在開機時執行，見 install_autostart.ps1)
chcp 65001 >nul
cd /d "%~dp0..\.."
w32tm /resync /nowait >nul 2>&1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" serve_windows.py >> "database\server.log" 2>&1
) else (
    python serve_windows.py >> "database\server.log" 2>&1
)
