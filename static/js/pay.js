/* 付款頁：自動送出表單到綠界 (按鈕保留給自動送出失敗時手動點選) */
(function () {
    'use strict';
    const form = document.getElementById('ecpay-form');
    if (form) setTimeout(() => form.submit(), 600);
})();
