"""
static/js/shared/ 純邏輯模組 (點餐機與 POS 共用的計價 / 加購配額 / 合併鍵 / payload / 發票 / 預覽排程) 的單元測試：
以 node 執行 tests/js/shared_spec.mjs。沒有 node 時跳過 (CI 的 ubuntu 映像與 Playwright 工作都有 node)。
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, 'tests', 'js', 'shared_spec.mjs')


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node 才能執行前端純邏輯測試')
def test_shared_js_modules():
    result = subprocess.run([shutil.which('node'), SPEC], capture_output=True, text=True, encoding='utf-8', cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FAIL' not in result.stdout and '/' in result.stdout.strip().splitlines()[-1], result.stdout
