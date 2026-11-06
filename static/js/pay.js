/* 付款頁：自動送出表單到綠界 (按鈕保留給自動送出失敗時手動點選) */
(function () {
    'use strict';
    const form = document.getElementById('ecpay-form');
    if (!form) return;
    // 顧客在自動送出前自己按了「前往付款」：取消自動送出，避免同一個交易編號送兩次給綠界
    const timer = setTimeout(() => form.submit(), 600);
    form.addEventListener('submit', () => clearTimeout(timer));
})();
