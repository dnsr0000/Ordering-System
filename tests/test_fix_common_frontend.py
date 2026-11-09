"""
修正小組 common-frontend 的回歸測試：全站共用的 static/js/common.js、static/css/theme.css、templates/base.html。

前端部分以 node 執行 (以假的 document / window / fetch / EventSource 與可控的計時器載入 common.js)；沒有 node 時跳過。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support import get_or_create_tenant

ROOT = Path(__file__).resolve().parent.parent

SPEC = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const ROOT = process.argv[2];
const SRC = fs.readFileSync(`${ROOT}/static/js/common.js`, 'utf8');
const tick = () => new Promise((r) => setImmediate(r));

function makeEnv({ shop = null, hidden = false, onLine = true } = {}) {
    let now = 0;
    let seq = 0;
    const timers = new Map();
    const add = (fn, ms, repeat) => { const id = ++seq; timers.set(id, { fn, at: now + Math.max(0, ms || 0), ms, repeat }); return id; };
    const clear = (id) => { timers.delete(id); };
    const docListeners = {};
    const winListeners = {};
    function el(tag) {
        const cls = new Set();
        return {
            tagName: tag, id: '', className: '', textContent: '', children: [], attrs: {}, offsetHeight: 37, parentNode: null,
            style: { props: {}, setProperty(k, v) { this.props[k] = v; }, removeProperty(k) { delete this.props[k]; } },
            classList: { toggle(c, on) { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); },
                         add(c) { cls.add(c); }, remove(c) { cls.delete(c); }, contains(c) { return cls.has(c); } },
            setAttribute(k, v) { this.attrs[k] = String(v); },
            getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
            appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
            remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((x) => x !== this); },
        };
    }
    const body = el('body');
    const find = (root, pred) => {
        for (const c of root.children) {
            if (pred(c)) return c;
            const f = find(c, pred);
            if (f) return f;
        }
        return null;
    };
    const document = {
        hidden, body, documentElement: el('html'),
        addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
        querySelector(sel) {
            if (sel === 'meta[name="shop"]' && shop !== null) return { getAttribute: () => shop };
            return null;
        },
        querySelectorAll() { return []; },
        getElementById(id) { return find(body, (c) => c.id === id); },
        createElement: el,
        contains: () => true,
    };
    const sources = [];
    class EventSource {
        constructor(url) { this.url = url; this.closed = false; this.l = {}; sources.push(this); }
        addEventListener(t, f) { (this.l[t] ||= []).push(f); }
        close() { this.closed = true; }
        emit(t) { (this.l[t] || []).forEach((f) => f({})); if (t === 'message' && this.onmessage) this.onmessage({}); }
    }
    const fetchCalls = [];
    const env = { fetchImpl: async () => ({ status: 200, ok: true }) };
    const storage = {};
    const ctx = {
        document, navigator: { onLine }, console, location: { href: '/order', reload() {} },
        setTimeout: (fn, ms) => add(fn, ms, false), clearTimeout: clear,
        setInterval: (fn, ms) => add(fn, ms, true), clearInterval: clear,
        EventSource,
        fetch: (...a) => { fetchCalls.push(a); return env.fetchImpl(...a); },
        sessionStorage: { getItem: () => null, setItem() {} },
        localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); },
                        removeItem: (k) => { delete storage[k]; } },
        addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
        Element: class {}, HTMLFormElement: class {}, HTMLImageElement: class {},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(SRC, ctx);
    async function advance(ms) {
        const target = now + ms;
        for (;;) {
            let next = null;
            for (const [id, t] of timers) if (t.at <= target && (!next || t.at < next[1].at)) next = [id, t];
            if (!next) break;
            const [id, t] = next;
            now = t.at;
            if (t.repeat) t.at = now + Math.max(1, t.ms); else timers.delete(id);
            t.fn();
            await tick();
        }
        now = target;
        await tick();
    }
    const fireDoc = (type, ev = { target: {} }) => (docListeners[type] || []).forEach((f) => f(ev));
    const fireWin = (type) => (winListeners[type] || []).forEach((f) => f({}));
    return { ctx, Kiosk: ctx.Kiosk, document, sources, fetchCalls, env, advance, fireDoc, fireWin };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('B-479：閒置登出斷線 / 5xx 時不跳頁，顯示鎖定畫面並重試到成功', async () => {
    const e = makeEnv();
    e.env.fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
    e.Kiosk.idleLogout({ timeoutMs: 100, retryMs: 1000 });
    await e.advance(100);
    assert.equal(e.fetchCalls.length, 1);
    assert.equal(e.fetchCalls[0][1].method, 'POST');
    assert.equal(e.ctx.location.href, '/order');
    assert.ok(e.document.getElementById('kiosk-logout-lock'), '應顯示鎖定畫面');
    // 鎖定期間有人操作也不會重新計時 / 重複送出
    e.fireDoc('click');
    e.env.fetchImpl = async () => ({ status: 500, ok: false });
    await e.advance(1000);
    assert.equal(e.fetchCalls.length, 2);
    assert.equal(e.ctx.location.href, '/order');
    e.env.fetchImpl = async () => ({ status: 200, ok: true });
    await e.advance(1000);
    assert.equal(e.fetchCalls.length, 3);
    assert.equal(e.ctx.location.href, '/');
});

test('B-479：登出成功 (或 4xx 重試也不會成功) 時直接回首頁', async () => {
    for (const status of [200, 403]) {
        const e = makeEnv();
        e.env.fetchImpl = async () => ({ status, ok: status < 300 });
        e.Kiosk.idleLogout({ timeoutMs: 50 });
        await e.advance(50);
        assert.equal(e.ctx.location.href, '/', String(status));
        assert.equal(e.document.getElementById('kiosk-logout-lock'), null);
    }
});

test('B-480：備註不分單型都顯示；餐具只在外帶 / 外送到府顯示', () => {
    const { Kiosk } = makeEnv();
    const s = (o) => String(Kiosk.takeoutBlock(o));
    const dine = s({ order_type: '內用', note: '不要花生', need_cutlery: false });
    assert.ok(dine.includes('備註：不要花生'), dine);
    assert.ok(!dine.includes('餐具'), dine);
    assert.equal(s({ order_type: '內用', note: '' }), '');
    const home = s({ order_type: '外送到府', note: '', need_cutlery: true });
    assert.ok(home.includes('要餐具'), home);
    const platform = s({ order_type: '外送', note: '多給醬料', need_cutlery: false });
    assert.ok(platform.includes('備註：多給醬料') && !platform.includes('餐具'), platform);
    const takeout = s({ order_type: '外帶', note: '', need_cutlery: false });
    assert.ok(takeout.includes('不用餐具'), takeout);
});

test('B-482：分頁在背景時關閉 SSE 改輪詢，切回時重新連線', async () => {
    const e = makeEnv();
    let calls = 0;
    const handle = e.Kiosk.onOrdersChanged(() => { calls += 1; });
    assert.equal(e.sources.length, 1);
    e.sources[0].emit('open');
    e.document.hidden = true;
    e.fireDoc('visibilitychange');
    await e.advance(5000);
    assert.equal(e.sources[0].closed, false, '短暫切換不關閉');
    await e.advance(10000);
    assert.equal(e.sources[0].closed, true, '背景超過時限要關閉 SSE');
    const before = calls;
    await e.advance(10000);
    assert.ok(calls > before, '背景期間改為輪詢');
    e.document.hidden = false;
    e.fireDoc('visibilitychange');
    assert.equal(e.sources.length, 2, '切回時重新連線');
    e.sources[1].emit('open');
    const visibleBase = calls;
    await e.advance(20000);
    assert.equal(calls, visibleBase + 1, '切回時更新一次，連線後停止輪詢');
    e.sources[1].emit('message');
    await e.advance(2000);
    assert.equal(calls, visibleBase + 2);
    handle.close();
    assert.equal(e.sources[1].closed, true);
    e.document.hidden = true;
    e.fireDoc('visibilitychange');
    e.document.hidden = false;
    e.fireDoc('visibilitychange');
    assert.equal(e.sources.length, 2, 'close 之後不再重新連線');
});

test('B-483：點餐機有斷線暫存時，提示條改說明現金單會暫存，並記錄提示條高度', async () => {
    const e = makeEnv({ onLine: false });
    e.ctx.KioskOffline = {};
    e.fireDoc('DOMContentLoaded');
    const banner = e.document.getElementById('kiosk-offline-banner');
    assert.ok(banner && banner.textContent.includes('暫存'), banner && banner.textContent);
    assert.equal(e.document.documentElement.style.props['--offline-banner-h'], '37px');
    e.ctx.navigator.onLine = true;
    e.fireWin('online');
    assert.equal(e.document.getElementById('kiosk-offline-banner'), null);
    assert.equal(e.document.documentElement.style.props['--offline-banner-h'], undefined);

    const plain = makeEnv({ onLine: false });
    plain.fireDoc('DOMContentLoaded');
    assert.ok(plain.document.getElementById('kiosk-offline-banner').textContent.includes('無法送出'));
});

test('B-484：購物車鍵依店家分開，預設店家沿用舊鍵', () => {
    assert.equal(makeEnv({ shop: 'b-shop' }).Kiosk.CART_KEY, 'kiosk_cart_b-shop');
    assert.equal(makeEnv({ shop: 'default' }).Kiosk.CART_KEY, 'kiosk_cart');
    assert.equal(makeEnv({ shop: '' }).Kiosk.CART_KEY, 'kiosk_cart');
    assert.equal(makeEnv().Kiosk.CART_KEY, 'kiosk_cart');
});

test('B-399：送出按鈕上的 data-confirm 只在按那顆按鈕時確認，其餘沿用 form 的 data-confirm', () => {
    const e = makeEnv();
    const asked = [];
    let answer = false;
    e.ctx.confirm = (msg) => { asked.push(msg); return answer; };
    const form = (confirm) => Object.assign(new e.ctx.HTMLFormElement(), { dataset: confirm ? { confirm } : {} });
    const submit = (target, submitter) => {
        const ev = { target, submitter, prevented: false, preventDefault() { this.prevented = true; } };
        e.fireDoc('submit', ev);
        return ev;
    };
    const po = form(null);
    // 「儲存」(按鈕沒有 data-confirm、form 也沒有)：不確認
    assert.equal(submit(po, { dataset: {} }).prevented, false);
    assert.deepEqual(asked, []);
    // 「送出」：用按鈕上的訊息確認，取消就不送出
    assert.equal(submit(po, { dataset: { confirm: '確定送出？' } }).prevented, true);
    assert.deepEqual(asked, ['確定送出？']);
    answer = true;
    assert.equal(submit(po, { dataset: { confirm: '確定送出？' } }).prevented, false);
    // form 上的 data-confirm 照舊 (沒有 submitter，例如 requestSubmit / Enter)
    answer = false;
    assert.equal(submit(form('確定刪除？'), undefined).prevented, true);
    assert.equal(asked.at(-1), '確定刪除？');
    // 非表單的 submit 事件不處理
    assert.equal(submit({}, { dataset: { confirm: 'x' } }).prevented, false);
});

let failed = 0;
for (const [name, fn] of tests) {
    try { await fn(); console.log(`ok   ${name}`); }
    catch (err) { failed += 1; console.log(`FAIL ${name}\n${err && err.stack}`); }
}
process.exit(failed ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node 才能執行前端測試')
def test_common_js_behaviour(tmp_path):
    spec = tmp_path / 'common_frontend_spec.mjs'
    spec.write_text(SPEC, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(spec), ROOT.as_posix()], capture_output=True, text=True,
                            encoding='utf-8', cwd=str(ROOT), timeout=60, env=dict(os.environ))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FAIL' not in result.stdout, result.stdout


def _html_tag(html):
    return re.search(r'<html[^>]*>', html).group(0)


def test_html_lang_follows_customer_language(app, store_settings):
    """B-485：顧客頁切成英 / 日文時 <html lang> 跟著改；中文維持 zh-TW"""
    store_settings('menu', languages=['en', 'ja'])
    c = app.test_client()
    assert 'lang="en"' in _html_tag(c.get('/?shop=default&lang=en').get_data(as_text=True))
    assert 'lang="ja"' in _html_tag(c.get('/?lang=ja').get_data(as_text=True))
    assert 'lang="zh-TW"' in _html_tag(c.get('/?lang=zh').get_data(as_text=True))


def test_shop_meta_for_cart_key(app):
    """B-484：頁面以 <meta name="shop"> 提供店家代號 (common.js 依它分開購物車)"""
    c = app.test_client()
    assert '<meta name="shop" content="default">' in c.get('/?shop=default').get_data(as_text=True)
    get_or_create_tenant(app, 'cf-cart-shop')
    html = c.get('/?shop=cf-cart-shop').get_data(as_text=True)
    assert '<meta name="shop" content="cf-cart-shop">' in html


def test_light_pages_keep_bootstrap_light_variables(app):
    """R2-189：樣板宣告淺色的頁面標上 data-page-theme="light"，theme.css 的深色 Bootstrap 變數不套用"""
    from flask import render_template_string
    with app.test_request_context('/'):
        light = render_template_string("{% extends 'base.html' %}{% block theme %}light{% endblock %}")
        dark = render_template_string("{% extends 'base.html' %}")
    assert 'data-bs-theme="light"' in _html_tag(light) and 'data-page-theme="light"' in _html_tag(light)
    assert 'data-bs-theme="dark"' in _html_tag(dark) and 'data-page-theme="dark"' in _html_tag(dark)

    css = (ROOT / 'static/css/theme.css').read_text(encoding='utf-8')
    # 深色的 --bs-* 只放在「非淺色頁面」與 [data-bs-theme="dark"]；--kiosk-* 仍在 :root (淺色頁也取得到)
    bs_block = re.search(r'([^{}]*)\{[^}]*--bs-body-bg:', css).group(1)
    assert ':root:not([data-page-theme="light"])' in bs_block and '[data-bs-theme="dark"]' in bs_block
    assert not re.search(r'(^|,)\s*:root\s*(,|$)', bs_block.strip())
    assert re.search(r':root\s*\{[^}]*--kiosk-bg:', css)


def test_offline_queue_badge_sits_above_banner():
    """B-483：暫存單按鈕依提示條高度往上移"""
    css = (ROOT / 'static/css/theme.css').read_text(encoding='utf-8')
    assert re.search(r'#offlineQueueBadge\s*\{[^}]*var\(--offline-banner-h', css)
