/**
 * 桌位平面圖 (後台擺放 / POS 即時狀態共用)。
 * 座標以平面圖寬高的千分比 (0~1000) 保存，任何螢幕大小都等比例顯示。
 *   FloorPlan.render(container, tables, { editable, area, onPick })
 *   FloorPlan.positions(container) → [{id, x, y}] (編輯模式)
 */
(() => {
    const { html, setHtml } = window.Kiosk;
    const STATUS = {
        free: { cls: 'fp-free', label: '空桌' },
        occupied: { cls: 'fp-occupied', label: '用餐中' },
        calling: { cls: 'fp-calling', label: '呼叫中' },
        reserved: { cls: 'fp-reserved', label: '即將訂位' },
    };

    function tableHtml(t, editable) {
        const st = STATUS[t.status] || STATUS.free;
        return html`<div class="fp-table fp-${t.shape || 'square'} ${editable ? 'fp-edit' : st.cls}" data-table-id="${t.id}"
                         data-name="${t.name}" role="button" tabindex="0" title="${t.name} (${t.seats} 位)">
            <div class="fp-name">${t.name}</div>
            <div class="fp-meta">${t.seats} 位</div>
            ${!editable && t.status === 'occupied' ? html`<div class="fp-meta">$${t.total} • ${t.minutes} 分</div>` : ''}
            ${!editable && t.status === 'calling' ? html`<div class="fp-meta">${(t.calls || []).join('、')}</div>` : ''}
            ${!editable && t.status === 'reserved' ? html`<div class="fp-meta">${t.reservation}</div>` : ''}
        </div>`;
    }

    function place(el, t) {
        el.style.left = `${(t.x || 0) / 10}%`;
        el.style.top = `${(t.y || 0) / 10}%`;
    }

    function render(container, tables, { editable = false, area = '', onPick = null } = {}) {
        const shown = tables.filter((t) => !area || t.area === area);
        const placed = shown.filter((t) => t.x !== null && t.x !== undefined && t.y !== null && t.y !== undefined);
        const unplaced = shown.filter((t) => !placed.includes(t));
        setHtml(container, html`
            <div class="fp-canvas" data-fp-canvas>${placed.map((t) => tableHtml(t, editable))}</div>
            ${unplaced.length ? html`<div class="fp-tray mt-2" data-fp-tray>
                <span class="small text-white-50 me-2">${editable ? '尚未擺放 (拖到上方平面圖)：' : '未擺放：'}</span>
                ${unplaced.map((t) => tableHtml(t, editable))}</div>` : ''}`);
        container.querySelectorAll('[data-fp-canvas] .fp-table').forEach((el) => {
            place(el, shown.find((t) => String(t.id) === el.dataset.tableId));
        });
        if (editable) enableDrag(container);
        if (onPick) {
            container.querySelectorAll('.fp-table').forEach((el) => el.addEventListener('click', () => onPick(Number(el.dataset.tableId))));
        }
    }

    function enableDrag(container) {
        const canvas = container.querySelector('[data-fp-canvas]');
        container.querySelectorAll('.fp-table').forEach((el) => {
            el.addEventListener('pointerdown', (ev) => {
                ev.preventDefault();
                const rect = canvas.getBoundingClientRect();
                if (el.parentElement !== canvas) {   // 從待擺放區拖進平面圖
                    canvas.appendChild(el);
                    el.style.left = `${((ev.clientX - rect.left) / rect.width) * 100}%`;
                    el.style.top = `${((ev.clientY - rect.top) / rect.height) * 100}%`;
                }
                el.setPointerCapture(ev.pointerId);
                el.classList.add('fp-dragging');
                const move = (e) => {
                    const x = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 0.95);
                    const y = Math.min(Math.max((e.clientY - rect.top) / rect.height, 0), 0.92);
                    el.style.left = `${x * 100}%`;
                    el.style.top = `${y * 100}%`;
                };
                const up = () => {
                    el.classList.remove('fp-dragging');
                    el.removeEventListener('pointermove', move);
                    el.removeEventListener('pointerup', up);
                };
                el.addEventListener('pointermove', move);
                el.addEventListener('pointerup', up);
            });
            el.addEventListener('dblclick', () => {   // 雙擊：移回待擺放區
                let tray = container.querySelector('[data-fp-tray]');
                if (!tray) {
                    tray = document.createElement('div');
                    tray.className = 'fp-tray mt-2';
                    tray.dataset.fpTray = '';
                    container.appendChild(tray);
                }
                el.style.left = '';
                el.style.top = '';
                tray.appendChild(el);
            });
        });
    }

    function positions(container) {
        const canvas = container.querySelector('[data-fp-canvas]');
        return [...container.querySelectorAll('.fp-table')].map((el) => {
            if (el.parentElement !== canvas) return { id: Number(el.dataset.tableId), x: null, y: null };
            return { id: Number(el.dataset.tableId), x: Math.round(parseFloat(el.style.left) * 10),
                     y: Math.round(parseFloat(el.style.top) * 10) };
        });
    }

    window.FloorPlan = { render, positions, STATUS };
})();
