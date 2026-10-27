"""前端原始碼靜態檢查：CSP 相容 (無行內程式)、離線可用 (無外部網址)、動態 HTML 一律跳脫"""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = sorted(glob.glob(os.path.join(ROOT, 'templates', '*.html')))
SCRIPTS = sorted(glob.glob(os.path.join(ROOT, 'static', 'js', '**', '*.js'), recursive=True))   # 含 js/pos/ 模組
STYLES = sorted(glob.glob(os.path.join(ROOT, 'static', 'css', '*.css')))


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _ids(paths):
    return [os.path.relpath(p, ROOT) for p in paths]


def _strip_js_comments(src):
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'(^|[^:\\])//[^\n]*', r'\1', src)


def test_frontend_files_found():
    assert len(TEMPLATES) >= 10 and len(SCRIPTS) >= 10


@pytest.mark.parametrize('path', TEMPLATES, ids=_ids(TEMPLATES))
def test_template_has_no_inline_code(path):
    src = _read(path)
    assert not re.findall(r'\son[a-z]+\s*=', src, re.I), '不可使用 onclick 等行內事件 (CSP 會擋下)'
    assert 'javascript:' not in src.lower()
    for tag in re.findall(r'<script\b[^>]*>', src, re.I):
        assert 'src=' in tag or 'application/json' in tag, f'不可使用行內 <script>：{tag}'
    assert '|safe' not in src.replace(' ', ''), '不可用 |safe 輸出資料'


@pytest.mark.parametrize('path', TEMPLATES + SCRIPTS + STYLES, ids=_ids(TEMPLATES + SCRIPTS + STYLES))
def test_no_external_resources(path):
    src = _read(path)
    if path.endswith('.js'):
        src = _strip_js_comments(src)
    urls = re.findall(r'(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}[^\s"\')]*', src, re.I)
    assert not urls, f'不可引用外部資源 (需離線可用)：{urls[:3]}'


@pytest.mark.parametrize('path', [p for p in TEMPLATES if not os.path.basename(p).startswith(('base', '_'))],
                         ids=_ids([p for p in TEMPLATES if not os.path.basename(p).startswith(('base', '_'))]))
def test_pages_extend_base(path):
    assert '{% extends "base.html" %}' in _read(path)


@pytest.mark.parametrize('path', SCRIPTS, ids=_ids(SCRIPTS))
def test_scripts_escape_dynamic_html(path):
    src = _strip_js_comments(_read(path))
    assert not re.search(r'\beval\s*\(|new\s+Function\s*\(', src)
    # 以樣板字串直接寫入 innerHTML / insertAdjacentHTML 不會跳脫，必須改用 Kiosk.html 或 textContent
    assert not re.search(r'\.innerHTML\s*\+?=\s*`', src), '請用 Kiosk.setHtml(el, Kiosk.html`...`)'
    assert not re.search(r'insertAdjacentHTML\s*\([^,]+,\s*`', src)
    assert not re.search(r'\.innerHTML\s*\+?=\s*[^`;]*\+', src), '不可用字串相加組 HTML'


POS_MODULES = sorted(glob.glob(os.path.join(ROOT, 'static', 'js', 'pos', '*.js')))


def test_pos_modules_import_only_existing_exports():
    """POS 拆成 ES 模組 (static/js/pos.js + static/js/pos/)：每個 import 的名稱都要在對方模組 export，不可漏 / 拼錯"""
    exports = {}
    for path in POS_MODULES:
        src = _read(path)
        names = set(re.findall(r'^export (?:async function|function|const|let)\s+([A-Za-z_$][\w$]*)', src, re.M))
        for group in re.findall(r'^export const \{([^}]*)\}', src, re.M):
            names |= {n.strip() for n in group.split(',') if n.strip()}
        exports[os.path.basename(path)] = names
    assert set(exports) >= {'core.js', 'menu.js', 'cart.js', 'member.js', 'totals.js', 'checkout.js', 'live.js'}
    for path in POS_MODULES + [os.path.join(ROOT, 'static', 'js', 'pos.js')]:
        src = _read(path)
        for names, target in re.findall(r"^import \{([^}]*)\} from '\./(?:pos/)?([a-z]+\.js)';", src, re.M):
            for name in (n.strip() for n in names.replace('\n', ' ').split(',') if n.strip()):
                assert name in exports[target], f'{os.path.basename(path)} 匯入的 {name} 不在 {target}'
        assert len(src.split('\n')) <= 500, f'{os.path.basename(path)} 超過 500 行，請再拆分'
    assert 'type="module"' in _read(os.path.join(ROOT, 'templates', 'pos.html')).split('js/pos.js')[0].rsplit('<script', 1)[1]
