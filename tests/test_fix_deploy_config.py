"""部署腳本與設定的回歸測試 (門市一鍵更新、開機自動啟動、CI、PostgreSQL 還原演練、換行設定)"""
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
UPDATE_CMD = os.path.join(ROOT, 'deploy', 'windows', 'update_store.cmd')


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def _bytes(rel):
    with open(os.path.join(ROOT, rel), 'rb') as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 換行 / 編碼：cmd.exe 與 Windows PowerShell 5.1 的讀檔限制
# ---------------------------------------------------------------------------
def test_gitattributes_forces_crlf_for_batch_files():
    attrs = _read('.gitattributes')
    assert re.search(r'^\*\.cmd\s+text\s+eol=crlf\s*$', attrs, re.M)
    assert re.search(r'^\*\.bat\s+text\s+eol=crlf\s*$', attrs, re.M)


def test_windows_batch_files_use_crlf_only():
    files = glob.glob(os.path.join(ROOT, 'deploy', 'windows', '*.cmd'))
    assert files
    for path in files:
        with open(path, 'rb') as fh:
            data = fh.read()
        assert data.count(b'\n') == data.count(b'\r\n') > 0, f'{path} 必須是 CRLF 換行'


def test_update_script_is_pure_ascii():
    # cmd.exe 在 950 字碼頁 (繁中 Windows、後台以無主控台方式啟動) 讀含 UTF-8 中文的批次檔會錯位，把註解片段當指令執行
    data = _bytes('deploy/windows/update_store.cmd')
    assert max(data) < 128, '請勿在 update_store.cmd 寫入非 ASCII 字元'
    assert not data.startswith(b'\xef\xbb\xbf')


def test_powershell_scripts_have_utf8_bom():
    files = glob.glob(os.path.join(ROOT, 'deploy', '**', '*.ps1'), recursive=True)
    assert files
    for path in files:
        with open(path, 'rb') as fh:
            assert fh.read(3) == b'\xef\xbb\xbf', f'{path} 必須存成 UTF-8 with BOM (Windows PowerShell 5.1)'


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 only')
def test_install_autostart_parses_in_windows_powershell():
    exe = shutil.which('powershell')
    if not exe:
        pytest.skip('powershell not found')
    path = os.path.join(ROOT, 'deploy', 'windows', 'install_autostart.ps1')
    cmd = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + path +
           "', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run([exe, '-NoProfile', '-Command', cmd], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == '0', out.stdout + out.stderr


def test_install_autostart_does_not_promise_crash_restart():
    text = _read('deploy/windows/install_autostart.ps1')
    assert '失敗時每分鐘重試，最多 999 次)' not in text.replace('\r', '')
    assert 'NSSM' in text
    deploy = _read('docs/DEPLOY.txt')
    assert '先校時再啟動，失敗每分鐘重試' not in deploy and '不會自動重啟' in deploy


# ---------------------------------------------------------------------------
# CI 與 PostgreSQL 還原演練
# ---------------------------------------------------------------------------
def test_ci_coverage_step_uses_pipefail():
    wf = _read('.github/workflows/tests.yml')
    m = re.search(r'^(\s*)- run: pytest [^\n]*\| tee coverage\.txt\n((?:\1  [^\n]*\n)*)', wf, re.M)
    assert m, 'coverage step not found'
    assert re.search(r'^\s*shell: bash\s*$', m.group(2), re.M) or 'pipefail' in m.group(0)


def test_pg_restore_check_quotes_reserved_table_names_and_cleans_up():
    script = _read('deploy/pg_restore_check.sh')
    assert 'FROM \\"${t}\\"' in script, 'user / order 是保留字，資料表名稱要用 SQL 雙引號'
    loop = re.search(r'^for t in ([^;]+);', script, re.M).group(1)
    assert '"' not in loop and set(loop.split()) >= {'user', 'order', 'invoice'}
    assert re.search(r"^trap '.*dropdb[^']*--if-exists[^']*' EXIT", script, re.M), '中途失敗也要刪除暫時資料庫'


# ---------------------------------------------------------------------------
# 門市一鍵更新 (update_store.cmd)：靜態檢查
# ---------------------------------------------------------------------------
def test_update_script_static_safeguards():
    s = _read('deploy/windows/update_store.cmd')
    # 手動執行時先轉交複本執行
    assert 'if /i not "%~nx0"=="update_store.run.cmd"' in s and 'call "database\\updates\\update_store.run.cmd" %*' in s
    # 自動挑 zip 時排除 previous-*.zip，並檢查 run.py
    assert '"previous-"' in s and 'run.py not found' in s
    # 資料庫快照在停掉伺服器之後，用 SQLite backup API
    assert s.index('taskkill') < s.index('s.backup(d)') and 'copy /y database\\menu.db' not in s
    # 還原時刪除 -wal / -shm、刪除新版新增的檔案
    rollback = s[s.index('\n:rollback'):s.index('\n:restart')]
    assert 'menu.db-wal' in rollback and 'menu.db-shm' in rollback and '%ADDED%' in rollback
    assert rollback.index('menu.db-wal') < rollback.index('copy /y "%SNAP%" database\\menu.db')
    # 舊備份只保留 3 份，程式備份不含模型檔等大檔
    assert '-Skip 3' in s and "'models','logs','chrome-kiosk'" in s
    # 無主控台 (後台啟動) 時 cmd 管線會卡住：批次檔本身不可使用 | 管線 (PowerShell 字串裡的除外)
    for line in s.splitlines():
        outside = re.sub(r'"[^"]*"', '', line)
        assert '|' not in outside.replace('||', '').replace('^|', ''), line


# ---------------------------------------------------------------------------
# 門市一鍵更新：在隔離的資料夾實際執行 (Windows；假的 pip / flask / start_store)
# ---------------------------------------------------------------------------
def _real_autostart_installed():
    for args in (['schtasks', '/query', '/tn', 'KioskPOS-Server'], ['sc', 'query', 'KioskPOS']):
        try:
            if subprocess.run(args, capture_output=True, timeout=30).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            return True
    return False


SERVER = ('import sqlite3,sys,time\n'
          'c=sqlite3.connect(sys.argv[1],isolation_level=None)\n'
          'c.execute("pragma journal_mode=wal");c.execute("pragma wal_autocheckpoint=0")\n'
          'c.execute("create table if not exists t (v text)")\n'
          'c.execute("pragma wal_checkpoint(truncate)")\n'
          'for i in range(5): c.execute("insert into t values (?)", (f"order{i}",))\n'
          'print("ready", flush=True)\n'
          'time.sleep(120)\n')


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data.encode('utf-8') if isinstance(data, str) else data)


def _sandbox(tmp_path):
    s = str(tmp_path / 'store')
    with open(UPDATE_CMD, 'rb') as fh:
        _write(os.path.join(s, 'deploy', 'windows', 'update_store.cmd'), fh.read())
    _write(os.path.join(s, 'deploy', 'windows', 'start_store.cmd'),
           '@echo off\r\ncd /d "%~dp0..\\.."\r\necho started> database\\started.txt\r\n')
    _write(os.path.join(s, 'run.py'), 'old\n')
    _write(os.path.join(s, 'app', 'a.py'), 'v1\n')
    _write(os.path.join(s, 'migrations', 'versions', '0001_init.py'), '# 0001\n')
    _write(os.path.join(s, 'requirements-store.txt'), '# none\n')
    # python -m pip / -m flask 以目前資料夾優先：用假的套件模擬安裝與資料庫升級 (升級會寫入資料庫後失敗)
    _write(os.path.join(s, 'pip', '__init__.py'), '')
    _write(os.path.join(s, 'pip', '__main__.py'), 'import sys\nsys.exit(0)\n')
    _write(os.path.join(s, 'flask', '__init__.py'), '')
    _write(os.path.join(s, 'flask', '__main__.py'),
           'import sqlite3,sys\nc=sqlite3.connect("database/menu.db")\n'
           'c.execute("insert into t values (\'upgrade\')");c.commit();c.close()\nsys.exit(1)\n')
    _write(os.path.join(s, 'models', 'big.onnx'), 'x')
    os.makedirs(os.path.join(s, 'database', 'updates'))
    zpath = os.path.join(s, 'database', 'updates', 'v2.zip')
    with zipfile.ZipFile(zpath, 'w') as zf:
        for name, body in (('run.py', 'new\n'), ('app/a.py', 'v2\n'), ('app/added.py', 'x\n'),
                           ('migrations/versions/0002_new.py', '# 0002\n'),
                           ('deploy/windows/update_store.cmd', '@echo off\necho replaced\n')):
            zf.writestr('owner-repo-abc/' + name, body)
    old = time.time() - 3600
    os.utime(zpath, (old, old))
    # 上次更新留下、時間較新的扁平程式備份：不可被當成更新檔
    with zipfile.ZipFile(os.path.join(s, 'database', 'updates', 'previous-20000101-000000.zip'), 'w') as zf:
        zf.writestr('app/a.py', 'stale\n')
    return s


@pytest.mark.skipif(os.name != 'nt', reason='Windows batch file')
def test_update_script_rollback_restores_wal_data_and_removes_new_migration(tmp_path):
    if _real_autostart_installed():
        pytest.skip('this machine has the real KioskPOS service / task; the script would restart it')
    s = _sandbox(tmp_path)
    db_path = os.path.join(s, 'database', 'menu.db')
    server = subprocess.Popen([sys.executable, '-c', SERVER, db_path], stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout.readline().strip() == 'ready'
        assert os.path.getsize(db_path + '-wal') > 0, 'orders still only in the WAL'
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        script = os.path.join(s, 'deploy', 'windows', 'update_store.cmd')
        # 從繁中 Windows 的 950 字碼頁開始，手動執行 (不給 zip，自動挑選) 並傳入伺服器 PID
        cmdline = f'cmd.exe /c "chcp 950 >nul & "{script}" "" {server.pid} "{sys.executable}""'
        r = subprocess.run(cmdline, cwd=s, stdin=subprocess.DEVNULL, capture_output=True, timeout=180,
                           creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
        server.wait(timeout=30)
    finally:
        if server.poll() is None:
            server.kill()
    with open(os.path.join(s, 'database', 'updates', 'update.log'), encoding='utf-8', errors='replace') as fh:
        log = fh.read()
    assert b'is not recognized' not in r.stderr and '不是內部或外部命令'.encode('cp950') not in r.stderr, r.stderr
    assert 'v2.zip' in log and 'FAILED: database upgrade' in log and 'rolled back' in log, log
    with open(os.path.join(s, 'app', 'a.py'), encoding='utf-8') as fh:
        assert fh.read() == 'v1\n'
    assert not os.path.exists(os.path.join(s, 'migrations', 'versions', '0002_new.py'))
    assert not os.path.exists(os.path.join(s, 'app', 'added.py'))
    assert os.path.exists(os.path.join(s, 'migrations', 'versions', '0001_init.py'))
    assert not os.path.exists(db_path + '-wal') or os.path.getsize(db_path + '-wal') == 0
    conn = sqlite3.connect(db_path)
    try:
        assert [row[0] for row in conn.execute('select v from t')] == [f'order{i}' for i in range(5)]
    finally:
        conn.close()
    names = os.listdir(os.path.join(s, 'database', 'updates'))
    assert not any(n.startswith(('extract-', 'added-')) for n in names), names
    for _ in range(50):
        if os.path.exists(os.path.join(s, 'database', 'started.txt')):
            break
        time.sleep(0.1)
    assert os.path.exists(os.path.join(s, 'database', 'started.txt')), 'server restarted'
