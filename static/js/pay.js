/* 付款頁：自動送出表單到綠界 (按鈕保留給自動送出失敗時手動點選) */
(function () {
    'use strict';
    const form = document.getElementById('ecpay-form');
    if (!form) return;
    const button = form.querySelector('button[type="submit"]');
    // 已送出 (自動或手動)：同一組欄位 (同一個交易編號) 不再送第二次，避免綠界回「訂單編號重複」
    let submitted = false;
    const lock = () => {
        submitted = true;
        if (button) button.disabled = true;
    };
    // 顧客在自動送出前自己按了「前往付款」：取消自動送出，避免同一個交易編號送兩次給綠界
    const timer = setTimeout(() => {
        if (submitted) return;
        lock();
        form.submit();   // form.submit() 不會觸發 submit 事件
    }, 600);
    form.addEventListener('submit', () => clearTimeout(timer));
    form.addEventListener('submit', (ev) => {
        if (submitted) {
            ev.preventDefault();
            return;
        }
        lock();
    });
    // 從綠界按返回、頁面由瀏覽器快取還原：表單是舊的交易編號，重新載入以取得新的編號
    window.addEventListener('pageshow', (ev) => {
        if (ev.persisted) window.location.reload();
    });
})();
