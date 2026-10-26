/* 打卡頁：時鐘；打卡後清除密碼欄位 (共用平板) */
(function () {
    'use strict';
    Kiosk.startClock(document.getElementById('tcClock'));
    const pass = document.getElementById('tcPass');
    if (pass) pass.value = '';
})();
