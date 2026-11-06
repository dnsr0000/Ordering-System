/*
 * 生日的年 / 月 / 日下拉選單 (註冊頁、後台會員頁共用)。
 * 用法：容器加 data-birthday-select，內含三個 <select data-part="year|month|day">，各自只放一個空白選項；
 * 已有生日時在 select 上帶 data-value。本檔填入選項 (近 120 年、1~12 月、1~31 日)，
 * 並依所選年月停用不存在的日期 (2/30…) 與未來的月、日。後端 app/utils.py 的 parse_birthday 仍會再檢查一次。
 */
(function () {
    'use strict';
    const YEARS = 120;

    function fill(select, from, to) {
        const step = from <= to ? 1 : -1;
        for (let v = from; step > 0 ? v <= to : v >= to; v += step) {
            const opt = document.createElement('option');
            opt.value = String(v);
            opt.textContent = String(v);
            select.append(opt);
        }
        if (select.dataset.value) select.value = String(Number(select.dataset.value));
    }

    function sync(year, month, day) {
        const today = new Date();
        const y = Number(year.value) || 0;
        const isThisYear = y === today.getFullYear();
        Array.from(month.options).forEach((opt) => {
            opt.disabled = Boolean(opt.value) && isThisYear && Number(opt.value) > today.getMonth() + 1;
        });
        if (month.selectedOptions[0] && month.selectedOptions[0].disabled) month.value = '';
        const m = Number(month.value) || 0;
        const lastDay = m ? new Date(y || 2000, m, 0).getDate() : 31;   // 未選年份時以閏年計，2/29 仍可選
        const isThisMonth = isThisYear && m === today.getMonth() + 1;
        Array.from(day.options).forEach((opt) => {
            const v = Number(opt.value);
            opt.disabled = Boolean(opt.value) && (v > lastDay || (isThisMonth && v > today.getDate()));
        });
        if (day.selectedOptions[0] && day.selectedOptions[0].disabled) day.value = '';
    }

    document.querySelectorAll('[data-birthday-select]').forEach((group) => {
        const part = (name) => group.querySelector(`select[data-part="${name}"]`);
        const year = part('year');
        const month = part('month');
        const day = part('day');
        if (!year || !month || !day) return;
        const thisYear = new Date().getFullYear();
        fill(year, thisYear, thisYear - YEARS);
        fill(month, 1, 12);
        fill(day, 1, 31);
        group.addEventListener('change', () => sync(year, month, day));
        sync(year, month, day);
    });
})();
