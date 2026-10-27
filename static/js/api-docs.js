/*
 * API 說明文件頁的「線上試打」：使用者貼上金鑰、選端點、填參數，直接以 fetch 呼叫本站 /api/v1。
 * 金鑰只存在這個頁面的記憶體中 (不寫入 localStorage / Cookie)。
 */
(function () {
    'use strict';

    const data = Kiosk.pageData();
    const endpoints = data.endpoints || [];
    const form = document.getElementById('tryItForm');
    if (!form) return;
    const select = document.getElementById('tryEndpoint');
    const paramsBox = document.getElementById('tryParams');
    const bodyBox = document.getElementById('tryBodyBox');
    const bodyInput = document.getElementById('tryBody');
    const statusEl = document.getElementById('tryStatus');
    const resultEl = document.getElementById('tryResult');

    function renderParams() {
        const ep = endpoints[Number(select.value)] || endpoints[0];
        if (!ep) return;
        paramsBox.replaceChildren();
        const row = document.createElement('div');
        row.className = 'row g-2';
        ep.params.forEach((p) => {
            const col = document.createElement('div');
            col.className = 'col-md-3';
            const label = document.createElement('label');
            label.className = 'form-label small';
            label.textContent = p.name + (p.required ? ' *' : '');
            const input = document.createElement('input');
            input.className = 'form-control form-control-sm';
            input.name = p.name;
            input.dataset.where = p.where;
            input.placeholder = p.description;
            input.value = p.example || '';
            input.required = !!p.required;
            input.id = 'tryParam_' + p.name;
            label.htmlFor = input.id;
            col.append(label, input);
            row.append(col);
        });
        paramsBox.append(row);
        bodyBox.classList.toggle('d-none', !ep.body);
        bodyInput.value = ep.body ? JSON.stringify(ep.body, null, 2) : '';
    }

    function buildUrl(ep) {
        let path = ep.path.split('?')[0];
        const query = new URLSearchParams();
        paramsBox.querySelectorAll('input').forEach((input) => {
            const value = input.value.trim();
            if (input.dataset.where === 'path') {
                path = path.replace('{' + input.name + '}', encodeURIComponent(value));
            } else if (value !== '') {
                query.set(input.name, value);
            }
        });
        const qs = query.toString();
        return path + (qs ? '?' + qs : '');
    }

    select.addEventListener('change', renderParams);
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const ep = endpoints[Number(select.value)];
        const key = document.getElementById('tryKey').value.trim();
        if (!ep || !key) return;
        const url = buildUrl(ep);
        const init = { method: ep.method, headers: { Authorization: 'Bearer ' + key }, credentials: 'omit' };
        if (ep.method !== 'GET') {
            try {
                init.body = JSON.stringify(JSON.parse(bodyInput.value || '{}'));
            } catch (err) {
                statusEl.textContent = '請求本文不是有效的 JSON';
                return;
            }
            init.headers['Content-Type'] = 'application/json';
        }
        statusEl.textContent = `${ep.method} ${url} …`;
        const started = performance.now();
        try {
            const response = await fetch(url, init);
            const text = await response.text();
            let pretty = text;
            try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (err) { /* 非 JSON */ }
            statusEl.textContent = `${ep.method} ${url} → HTTP ${response.status} (${Math.round(performance.now() - started)} ms)`;
            resultEl.textContent = pretty;
            resultEl.classList.remove('d-none');
        } catch (err) {
            statusEl.textContent = '連線失敗：' + err.message;
        }
    });
    renderParams();
})();
