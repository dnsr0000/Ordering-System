/* 後台桌位管理：平面圖拖拉擺放並儲存 */
(() => {
    const { api, on } = window.Kiosk;
    const editor = document.getElementById('floorPlanEditor');
    if (!editor || !window.FloorPlan) return;
    const tables = window.Kiosk.pageData().floor || [];
    const areaSel = document.getElementById('floorArea');
    const draw = () => window.FloorPlan.render(editor, tables, { editable: true, area: areaSel ? areaSel.value : '' });

    /** 先把目前畫面上的位置寫回資料 (切換區域前不遺失尚未儲存的擺放) */
    function collect() {
        window.FloorPlan.positions(editor).forEach((p) => {
            const t = tables.find((x) => x.id === p.id);
            if (t) Object.assign(t, { x: p.x, y: p.y });
        });
    }

    if (areaSel) areaSel.addEventListener('change', () => { collect(); draw(); });
    on(document, 'click', {
        'save-layout': async (btn) => {
            collect();
            const msg = document.getElementById('floorSaveMsg');
            btn.disabled = true;
            try {
                const res = await api('/admin/tables/layout', { json: { tables: tables.map(({ id, x, y }) => ({ id, x, y })) } });
                msg.className = 'small mt-2 text-success';
                msg.textContent = `✅ 已儲存 ${res.updated} 桌的位置`;
            } catch (err) {
                msg.className = 'small mt-2 text-danger';
                msg.textContent = `❌ ${err.message}`;
            } finally {
                btn.disabled = false;
            }
        },
    });
    draw();
})();
