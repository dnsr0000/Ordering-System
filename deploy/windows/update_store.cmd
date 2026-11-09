@echo off
chcp 65001 >nul 2>&1
REM ==========================================================================================
REM Store host one-click update: update_store.cmd <update zip> [server PID] [python path]
REM (Chinese notes: docs/DEPLOY.txt 3-1)
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS. cmd.exe re-reads a batch file by byte
REM offset after every line; non-ASCII (UTF-8) text or LF line endings make it lose its place
REM under the zh-TW code page (950) and run fragments of comments as commands.
REM tests/test_fix_deploy_config.py checks both.
REM
REM Started by the admin page (System -> Version update -> Apply and restart), or by
REM double-clicking deploy\windows\update_store.cmd (then the newest zip in database\updates
REM is used, never a previous-*.zip program backup).
REM Steps: extract + check the zip -> back up program files -> stop the server -> SQLite
REM snapshot -> copy new files (database\ uploads\ .env .venv untouched) -> pip install ->
REM db upgrade -> restart. On failure: delete files the new version added, restore program
REM files and the database, restart. Log: database\updates\update.log
REM ==========================================================================================
setlocal enabledelayedexpansion
REM Both possible locations (deploy\windows and database\updates) are two levels below the root.
cd /d "%~dp0..\.."
set ROOT=%cd%
REM Always run from the copy database\updates\update_store.run.cmd: robocopy below overwrites
REM deploy\windows\update_store.cmd and cmd.exe would continue from a wrong offset of the new
REM file. The whole block is parsed before it runs, so this file is never read again.
if /i not "%~nx0"=="update_store.run.cmd" (
    if not exist database\updates mkdir database\updates
    copy /y "%~f0" "database\updates\update_store.run.cmd" >nul || ( echo Cannot copy the update script to database\updates & exit /b 1 )
    call "database\updates\update_store.run.cmd" %*
    exit /b !errorlevel!
)
set ZIP=%~1
set PID=%~2
set PY=%~3
set LOG=database\updates\update.log
if not exist database\updates mkdir database\updates
REM Pick the newest zip, skipping previous-*.zip (program backups left by earlier updates)
if "%ZIP%"=="" for /f "delims=" %%F in ('dir /b /o-d database\updates\*.zip 2^>nul') do if "!ZIP!"=="" ( set "NAME=%%F" & if /i not "!NAME:~0,9!"=="previous-" set "ZIP=database\updates\%%F" )
if "%ZIP%"=="" ( echo No update zip found in database\updates & exit /b 1 )
if "%PY%"=="" if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if "%PY%"=="" if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if "%PY%"=="" set PY=python
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%T
echo [%STAMP%] update start: %ZIP% >> "%LOG%"

REM 1. Extract to a temp folder and check it is a full program package (has run.py).
REM    The server is still running here, so on failure just stop.
set TMPDIR=database\updates\extract-%STAMP%
rmdir /s /q "%TMPDIR%" >nul 2>&1
powershell -NoProfile -Command "Expand-Archive -Path '%ZIP%' -DestinationPath '%TMPDIR%' -Force" >> "%LOG%" 2>&1
if errorlevel 1 ( echo FAILED: cannot extract the zip, update aborted >> "%LOG%" & rmdir /s /q "%TMPDIR%" >nul 2>&1 & exit /b 1 )
set SRC=
if exist "%TMPDIR%\run.py" set SRC=%TMPDIR%
if "%SRC%"=="" for /d %%D in ("%TMPDIR%\*") do if "!SRC!"=="" set SRC=%%D
if not exist "%SRC%\run.py" ( echo FAILED: not a program package - run.py not found, update aborted >> "%LOG%" & rmdir /s /q "%TMPDIR%" >nul 2>&1 & exit /b 1 )

REM 2. Back up program files (no data, virtualenv, model files, logs or kiosk browser profile)
set PREV=database\updates\previous-%STAMP%.zip
powershell -NoProfile -Command "$items = Get-ChildItem -Force | Where-Object { $_.Name -notin @('database','uploads','.venv','venv','.git','.env','node_modules','models','logs','chrome-kiosk') }; Compress-Archive -Path $items.FullName -DestinationPath '%PREV%' -Force" >> "%LOG%" 2>&1
if errorlevel 1 ( echo FAILED: cannot back up program files, update aborted >> "%LOG%" & rmdir /s /q "%TMPDIR%" >nul 2>&1 & exit /b 1 )

REM 3. Stop the server (wait 3 seconds so the admin page response is sent first)
timeout /t 3 /nobreak >nul 2>&1
REM Kill only the server process itself (no /t: this script is its child and would be killed too)
if not "%PID%"=="" taskkill /pid %PID% /f >nul 2>&1
sc query KioskPOS >nul 2>&1 && ( nssm stop KioskPOS >nul 2>&1 || net stop KioskPOS >nul 2>&1 )
REM Wait (up to 15 seconds) until the server process is gone before the database snapshot.
REM (No pipes in this script: a "a | b" pipe hangs when started without a console by the admin page.)
if not "%PID%"=="" powershell -NoProfile -Command "try { Wait-Process -Id %PID% -Timeout 15 -ErrorAction Stop } catch {}" >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1

REM Database snapshot with the SQLite online backup API: it includes transactions still in
REM menu.db-wal. Copying only menu.db would lose the latest orders on rollback.
set SNAP=database\updates\menu-before-%STAMP%.db
if exist database\menu.db (
    "%PY%" -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d);d.close();s.close()" database\menu.db "%SNAP%" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo FAILED: database snapshot, update aborted >> "%LOG%" & del /f /q "%SNAP%" >nul 2>&1 & goto :restart )
)

REM 4. Record files the new version adds (deleted on rollback, e.g. new migrations), then copy
REM    the contents of the top folder over the program files.
set ADDED=database\updates\added-%STAMP%.txt
powershell -NoProfile -Command "$s = (Resolve-Path -LiteralPath '%SRC%').Path; $x = @('database','uploads','.venv','venv','.git','node_modules'); Get-ChildItem -LiteralPath $s -Recurse -File -Force | ForEach-Object { $_.FullName.Substring($s.Length).TrimStart('\') } | Where-Object { $d = Split-Path $_ -Parent; $n = Split-Path $_ -Leaf; -not ($d -and ($d.Split('\') | Where-Object { $x -contains $_ })) -and $n -ne '.env' -and $n -notlike '.env.*' -and -not (Test-Path -LiteralPath (Join-Path '%ROOT%' $_)) } | Set-Content -LiteralPath '%ADDED%' -Encoding UTF8" >> "%LOG%" 2>&1
if errorlevel 1 ( echo FAILED: cannot list new files, update aborted >> "%LOG%" & goto :restart )
robocopy "%SRC%" "%ROOT%" /e /xd database uploads .venv venv .git node_modules /xf .env .env.* /np /nfl /ndl >> "%LOG%" 2>&1
if errorlevel 8 ( echo FAILED: copying program files, rolling back >> "%LOG%" & goto :rollback )
rmdir /s /q "%TMPDIR%" >nul 2>&1

REM 5. Packages and database
"%PY%" -m pip install -r requirements-store.txt >> "%LOG%" 2>&1
if errorlevel 1 ( echo FAILED: pip install, rolling back >> "%LOG%" & goto :rollback )
"%PY%" -m flask --app run db upgrade >> "%LOG%" 2>&1
if errorlevel 1 ( echo FAILED: database upgrade, rolling back program files and database >> "%LOG%" & goto :rollback )
echo update finished >> "%LOG%"
goto :restart

:rollback
REM Delete files the new version added first (a new migration left behind stays the Alembic
REM head and AUTO_MIGRATE would apply it again on restart), then restore the program backup.
if exist "%ADDED%" powershell -NoProfile -Command "Get-Content -LiteralPath '%ADDED%' -Encoding UTF8 | Where-Object { $_ } | ForEach-Object { Remove-Item -LiteralPath (Join-Path '%ROOT%' $_) -Force -ErrorAction SilentlyContinue }" >> "%LOG%" 2>&1
powershell -NoProfile -Command "Expand-Archive -Path '%PREV%' -DestinationPath '%ROOT%' -Force" >> "%LOG%" 2>&1
REM Restore the database: delete -wal / -shm first (they do not belong to the snapshot and
REM would be replayed onto it), then copy the snapshot back.
if exist "%SNAP%" (
    del /f /q database\menu.db-wal database\menu.db-shm >nul 2>&1
    copy /y "%SNAP%" database\menu.db >nul
)
echo rolled back to the version before the update >> "%LOG%"

:restart
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%" >nul 2>&1
if defined ADDED del /f /q "%ADDED%" >nul 2>&1
sc query KioskPOS >nul 2>&1 && ( nssm start KioskPOS >nul 2>&1 || net start KioskPOS >nul 2>&1 ) && goto :done
schtasks /query /tn KioskPOS-Server >nul 2>&1 && ( schtasks /run /tn KioskPOS-Server >nul 2>&1 ) && goto :done
start "" /min cmd /c "deploy\windows\start_store.cmd"
:done
echo server restarted >> "%LOG%"
REM Keep only the newest 3 program backups, database snapshots (they contain member data) and
REM update zips (never the one just applied) so the disk does not keep filling up.
powershell -NoProfile -Command "$d = 'database\updates'; foreach ($p in @('previous-*.zip','menu-before-*.db')) { Get-ChildItem -LiteralPath $d -Filter $p -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip 3 | Remove-Item -Force -ErrorAction SilentlyContinue }; $keep = (Resolve-Path -LiteralPath '%ZIP%').Path; Get-ChildItem -LiteralPath $d -Filter *.zip -File | Where-Object { $_.Name -notlike 'previous-*' -and $_.FullName -ne $keep } | Sort-Object LastWriteTime -Descending | Select-Object -Skip 3 | Remove-Item -Force -ErrorAction SilentlyContinue" >> "%LOG%" 2>&1
endlocal
