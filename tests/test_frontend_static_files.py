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


SHARED_MODULES = sorted(glob.glob(os.path.join(ROOT, 'static', 'js', 'shared', '*.js')))   # 顧客點餐與 POS 共用的純邏輯


def _module_exports(paths):
    """每個模組 export 的名稱 (export function / const / let 與 export const { a, b } = …)"""
    exports = {}
    for path in paths:
        src = _read(path)
        names = set(re.findall(r'^export (?:async function|function|const|let)\s+([A-Za-z_$][\w$]*)', src, re.M))
        for group in re.findall(r'^export const \{([^}]*)\}', src, re.M):
            names |= {n.strip() for n in group.split(',') if n.strip()}
        exports[os.path.basename(path)] = names
    return exports


def _check_es_modules(folder, entry, template, required):
    """拆成 ES 模組的頁面 (static/js/<entry>.js + static/js/<folder>/)：每個 import 的名稱都要在對方模組 (含 shared/) export，
    不可漏 / 拼錯；每檔 500 行內"""
    modules = sorted(glob.glob(os.path.join(ROOT, 'static', 'js', folder, '*.js')))
    exports = _module_exports(modules)
    shared = _module_exports(SHARED_MODULES)
    assert set(exports) >= required
    for path in modules + [os.path.join(ROOT, 'static', 'js', entry)]:
        src = _read(path)
        for names, target in re.findall(r"^import \{([^}]*)\} from '\./(?:" + folder + r"/)?([a-z]+\.js)';", src, re.M):
            for name in (n.strip() for n in names.replace('\n', ' ').split(',') if n.strip()):
                assert name in exports[target], f'{os.path.basename(path)} 匯入的 {name} 不在 {target}'
        for names, target in re.findall(r"^import \{([^}]*)\} from '\.\.?/shared/([a-z]+\.js)';", src, re.M):
            assert target in shared, f'{os.path.basename(path)} 匯入的 shared/{target} 不存在'
            for name in (n.strip() for n in names.replace('\n', ' ').split(',') if n.strip()):
                name = name.split(' as ')[0].strip()   # import { x as y }
                assert name in shared[target], f'{os.path.basename(path)} 匯入的 {name} 不在 shared/{target}'
        for target in re.findall(r"^import \* as \w+ from '\.\.?/shared/([a-z]+\.js)';", src, re.M):
            assert target in shared, f'{os.path.basename(path)} 匯入的 shared/{target} 不存在'
        assert len(src.split('\n')) <= 500, f'{os.path.basename(path)} 超過 500 行，請再拆分'
    assert 'type="module"' in _read(os.path.join(ROOT, 'templates', template)).split('js/' + entry)[0].rsplit('<script', 1)[1]


def test_pos_modules_import_only_existing_exports():
    _check_es_modules('pos', 'pos.js', 'pos.html', {'core.js', 'menu.js', 'cart.js', 'member.js', 'totals.js', 'checkout.js', 'live.js'})


def test_customer_modules_import_only_existing_exports():
    _check_es_modules('customer', 'customer.js', 'customer.html', {'core.js', 'cart.js', 'totals.js', 'menu.js', 'checkout.js'})


def test_shared_modules_are_pure_and_small():
    """static/js/shared/：顧客點餐與 POS 共用的模組，檔名只用小寫字母、500 行內、只匯入 shared/ 內的模組 (不依賴頁面狀態)"""
    assert {'addons.js', 'pricing.js', 'cartline.js', 'modifiers.js', 'totals.js', 'orderpayload.js', 'promo.js', 'invoice.js', 'combos.js'} \
        <= {os.path.basename(p) for p in SHARED_MODULES}
    shared = _module_exports(SHARED_MODULES)
    for path in SHARED_MODULES:
        name = os.path.basename(path)
        assert re.fullmatch(r'[a-z]+\.js', name), f'shared/{name} 檔名只能用小寫字母'
        src = _read(path)
        assert len(src.split('\n')) <= 500, f'shared/{name} 超過 500 行，請再拆分'
        assert shared[name], f'shared/{name} 沒有任何 export'
        for names, target in re.findall(r"^import \{([^}]*)\} from '([^']+)';", src, re.M):
            assert re.fullmatch(r'\./[a-z]+\.js', target), f'shared/{name} 只能匯入 shared/ 內的模組：{target}'
            for n in (n.strip().split(' as ')[0].strip() for n in names.replace('\n', ' ').split(',') if n.strip()):
                assert n in shared[os.path.basename(target)], f'shared/{name} 匯入的 {n} 不在 {target}'


@pytest.mark.parametrize('template, folder', [('customer.html', 'customer'), ('pos.html', 'pos')])
def test_module_pages_preload_every_module(template, folder):
    """點餐機 / POS 的 <link rel=modulepreload> 清單要涵蓋入口、頁面模組與 shared/ 的每一個檔案 (新增模組時一併加入)"""
    src = _read(os.path.join(ROOT, 'templates', template))
    block = re.search(r"\{% for m in \[([^\]]*)\] %\}<link rel=\"modulepreload\"", src)
    assert block, f'{template} 沒有 modulepreload 清單'
    listed = {m.strip().strip("'") for m in block.group(1).split(',')}
    expected = {f'{folder}.js'} | {f'{folder}/{os.path.basename(p)}' for p in glob.glob(os.path.join(ROOT, 'static', 'js', folder, '*.js'))} \
        | {f'shared/{os.path.basename(p)}' for p in SHARED_MODULES}
    assert listed == expected, f'{template} 的 modulepreload 清單與檔案不一致：多 {listed - expected}，少 {expected - listed}'
