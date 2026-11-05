/* 系統設定：左欄與卡片的即時搜尋 (標題、子群組、關鍵字)；Enter 直接開第一個符合的區塊 */
(function () {
    'use strict';

    const input = document.getElementById('settingsSearch');
    if (!input) return;
    const navLinks = Array.from(document.querySelectorAll('#settingsNav .settings-nav-link'));
    const navGroups = Array.from(document.querySelectorAll('#settingsNav .settings-nav-group'));
    const cards = Array.from(document.querySelectorAll('#settingsCards .settings-card-col'));
    const cardGroups = Array.from(document.querySelectorAll('#settingsCards .settings-group'));

    function apply() {
        const q = input.value.trim().toLowerCase();
        let navHits = 0;
        navLinks.forEach((a) => {
            const hit = !q || (a.dataset.search || '').includes(q);
            a.classList.toggle('d-none', !hit);
            if (hit) navHits += 1;
        });
        navGroups.forEach((g) => g.classList.toggle('d-none', !g.querySelector('.settings-nav-link:not(.d-none)')));
        const noMatch = document.getElementById('settingsNoMatch');
        if (noMatch) noMatch.classList.toggle('d-none', navHits > 0);
        if (!cards.length) return;
        let cardHits = 0;
        cards.forEach((c) => {
            const hit = !q || (c.dataset.search || '').includes(q);
            c.classList.toggle('d-none', !hit);
            if (hit) cardHits += 1;
        });
        cardGroups.forEach((g) => {
            g.querySelectorAll('.settings-subgroup-title').forEach((t) => {
                const row = t.nextElementSibling;
                const any = row && row.querySelector('.settings-card-col:not(.d-none)');
                t.classList.toggle('d-none', !any);
                if (row) row.classList.toggle('d-none', !any);
            });
            g.classList.toggle('d-none', !g.querySelector('.settings-card-col:not(.d-none)'));
        });
        const cardsNoMatch = document.getElementById('settingsCardsNoMatch');
        if (cardsNoMatch) cardsNoMatch.classList.toggle('d-none', cardHits > 0);
    }

    input.addEventListener('input', apply);
    input.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        const first = navLinks.find((a) => !a.classList.contains('d-none'));
        if (first) { ev.preventDefault(); window.location.href = first.href; }
    });
    try {
        const saved = sessionStorage.getItem('settings-search');
        if (saved && !input.value) { input.value = saved; apply(); }
        input.addEventListener('input', () => sessionStorage.setItem('settings-search', input.value));
    } catch (err) { /* 私密模式 */ }
    apply();
})();
