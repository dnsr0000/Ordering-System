@echo off
REM 門市主機一鍵更新：update_store.cmd <更新 zip> [伺服器 PID] [python 路徑]
REM 由後台「系統監控 → 版本更新 → 套用並重啟」啟動 (也可手動雙擊，會自己找 database\updates 最新的 zip)。
REM 流程：備份程式檔與資料庫 → 停掉伺服器 → 解壓覆蓋 (不碰 database\ uploads\ .env .venv) → pip install → 資料庫升級 → 重啟。
REM 升級失敗時還原程式檔並重啟；記錄在 database\updates\update.log。
chcp 65001 >nul
setlocal enabledelayedexpansion
REM 由後台啟動時是從 database\updates\update_store.run.cmd 執行 (複本，避免覆蓋到正在執行的批次檔)：專案根目錄 = 複本的上兩層
REM 兩個位置都在根目錄下兩層 (deploy\windows 或 database\updates)
cd /d "%~dp0..\.."
set ROOT=%cd%
set ZIP=%~1
set PID=%~2
set PY=%~3
set LOG=database\updates\update.log
if not exist database\updates mkdir database\updates
if "%ZIP%"=="" for /f "delims=" %%F in ('dir /b /o-d database\updates\*.zip 2^>nul') do if "!ZIP!"=="" set ZIP=database\updates\%%F
if "%ZIP%"=="" ( echo 找不到更新檔 & exit /b 1 )
if "%PY%"=="" if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if "%PY%"=="" if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if "%PY%"=="" set PY=python
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%T
echo [%STAMP%] 開始更新：%ZIP% >> "%LOG%"

REM 1. 備份：程式檔 (不含資料 / 虛擬環境) 與資料庫快照
set PREV=database\updates\previous-%STAMP%.zip
powershell -NoProfile -Command "$items = Get-ChildItem -Force | Where-Object { $_.Name -notin @('database','uploads','.venv','venv','.git','.env','node_modules') }; Compress-Archive -Path $items.FullName -DestinationPath '%PREV%' -Force" >> "%LOG%" 2>&1
if errorlevel 1 ( echo 備份程式檔失敗，中止 >> "%LOG%" & exit /b 1 )
if exist database\menu.db copy /y database\menu.db "database\updates\menu-before-%STAMP%.db" >nul

REM 2. 停掉目前的伺服器 (等 3 秒讓後台回應送完)
timeout /t 3 /nobreak >nul
REM 只殺伺服器行程本身 (不加 /t：這個腳本是它啟動的子行程，/t 會連自己一起殺掉)
if not "%PID%"=="" taskkill /pid %PID% /f >nul 2>&1
sc query KioskPOS >nul 2>&1 && ( nssm stop KioskPOS >nul 2>&1 || net stop KioskPOS >nul 2>&1 )
timeout /t 2 /nobreak >nul

REM 3. 解壓到暫存資料夾，再把最上層資料夾的內容覆蓋過來
set TMPDIR=database\updates\extract-%STAMP%
rmdir /s /q "%TMPDIR%" >nul 2>&1
powershell -NoProfile -Command "Expand-Archive -Path '%ZIP%' -DestinationPath '%TMPDIR%' -Force" >> "%LOG%" 2>&1
if errorlevel 1 ( echo 解壓失敗 >> "%LOG%" & goto :restart )
set SRC=
for /d %%D in ("%TMPDIR%\*") do if "!SRC!"=="" set SRC=%%D
if "%SRC%"=="" set SRC=%TMPDIR%
robocopy "%SRC%" "%ROOT%" /e /xd database uploads .venv venv .git node_modules /xf .env .env.* /np /nfl /ndl >> "%LOG%" 2>&1
if errorlevel 8 ( echo 覆蓋程式檔失敗，還原 >> "%LOG%" & goto :rollback )
rmdir /s /q "%TMPDIR%" >nul 2>&1

REM 4. 套件與資料庫
"%PY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 ( echo pip install 失敗，還原 >> "%LOG%" & goto :rollback )
"%PY%" -m flask --app run db upgrade >> "%LOG%" 2>&1
if errorlevel 1 ( echo 資料庫升級失敗，還原程式檔與資料庫 >> "%LOG%" & goto :rollback )
echo 更新完成 >> "%LOG%"
goto :restart

:rollback
powershell -NoProfile -Command "Expand-Archive -Path '%PREV%' -DestinationPath '%ROOT%' -Force" >> "%LOG%" 2>&1
if exist "database\updates\menu-before-%STAMP%.db" copy /y "database\updates\menu-before-%STAMP%.db" database\menu.db >nul
echo 已還原到更新前 >> "%LOG%"

:restart
sc query KioskPOS >nul 2>&1 && ( nssm start KioskPOS >nul 2>&1 || net start KioskPOS >nul 2>&1 ) && goto :done
schtasks /query /tn KioskPOS-Server >nul 2>&1 && ( schtasks /run /tn KioskPOS-Server >nul 2>&1 ) && goto :done
start "" /min cmd /c "deploy\windows\start_store.cmd"
:done
echo 已重新啟動伺服器 >> "%LOG%"
endlocal
