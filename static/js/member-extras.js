/*
 * 會員註冊 / 登入頁的附加功能：簡訊驗證碼 (傳送後倒數 60 秒)。
 * 表單上以 data-otp-purpose 指定用途，手機欄位標記 data-otp-phone，訊息顯示在 [data-otp-msg]。
 */
(function () {
    'use strict';

    function countdown(btn, seconds) {
        const label = btn.dataset.label || btn.textContent;
        btn.dataset.label = label;
        btn.disabled = true;
        let left = seconds;
        const timer = setInterval(() => {
            left -= 1;
            btn.textContent = `${left} 秒後可重送`;
            if (left <= 0) {
                clearInterval(timer);
                btn.disabled = false;
                btn.textContent = label;
            }
        }, 1000);
    }

    Kiosk.on(document, 'click', {
        'send-otp': async (btn) => {
            const form = btn.closest('form');
            const phoneInput = form.querySelector('[data-otp-phone]');
            const msg = form.querySelector('[data-otp-msg]');
            const phone = (phoneInput.value || '').trim();
            if (!/^09\d{8}$/.test(phone)) {
                msg.className = 'small mb-2 text-danger';
                msg.textContent = '請先輸入 09 開頭的 10 碼手機號碼';
                return;
            }
            try {
                const data = await Kiosk.api('/api/otp/send', { json: { phone, purpose: form.dataset.otpPurpose || 'register' } });
                msg.className = 'small mb-2 text-success';
                msg.textContent = data.message;
                countdown(btn, 60);
            } catch (err) {
                msg.className = 'small mb-2 text-danger';
                msg.textContent = err.message;
            }
        },
    });
})();
