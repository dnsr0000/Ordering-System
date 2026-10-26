/*
 * 出餐進度頁 (/track/<訂單>)：每 10 秒更新一次；餐點完成時震動、提示音，若已允許則顯示瀏覽器通知。
 */
(function () {
    'use strict';

    const data = Kiosk.pageData();
    const statusEl = document.getElementById('trackStatus');
    const numberEl = document.getElementById('trackNumber');
    const hintEl = document.getElementById('trackHint');
    let lastStatus = data.status;
    let notified = data.status === 'Completed';

    function beep() {
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            const ctx = new Ctx();
            const osc = ctx.createOscillator();
            osc.frequency.value = 880;
            osc.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.4);
        } catch (err) { /* 瀏覽器不允許自動播放 */ }
    }

    function announceReady(d) {
        if (notified) return;
        notified = true;
        if (navigator.vibrate) navigator.vibrate([300, 150, 300, 150, 600]);
        beep();
        if (window.Notification && Notification.permission === 'granted') {
            try { new Notification('餐點已完成', { body: `取餐號碼 #${String(d.pickup_number).padStart(3, '0')}，請至櫃檯取餐` }); } catch (err) { /* 部分手機不支援 */ }
        }
    }

    function apply(d) {
        numberEl.textContent = d.pickup_pending ? '待分店確認' : `#${String(d.pickup_number).padStart(3, '0')}`;
        statusEl.textContent = d.status_label;
        statusEl.className = `fs-4 fw-bold mt-2 ${d.status === 'Completed' ? 'text-success' : (d.status === 'Cancelled' ? 'text-danger' : '')}`;
        if (d.status === 'Completed') {
            hintEl.textContent = '餐點已完成，請至櫃檯取餐！';
            announceReady(d);
        } else if (d.status === 'PickedUp') {
            hintEl.textContent = '已取餐，祝您用餐愉快！';
        } else if (d.status === 'Cancelled') {
            hintEl.textContent = '此訂單已取消，如有疑問請洽櫃檯。';
        }
        lastStatus = d.status;
        if (d.delivery_status) {
            document.getElementById('trackDelivery').classList.remove('d-none');
            document.getElementById('trackDeliveryLabel').textContent = d.delivery_label;
            document.getElementById('trackDriver').textContent = d.driver ? ` (${d.driver})` : '';
            if (d.status === 'Completed' && ['pending', 'assigned'].includes(d.delivery_status)) {
                hintEl.textContent = '餐點已完成，等待外送員出發配送';
            }
            if (d.delivery_status === 'out') hintEl.textContent = '外送員已出發，請留意電話！';
            if (d.delivery_status === 'delivered') hintEl.textContent = '已送達，祝您用餐愉快！';
        }
        applyFeedback(d.feedback || {});
    }

    // ---------------------------------------------------------------- 餐後評分
    let rating = 0;

    function applyFeedback(fb) {
        const box = document.getElementById('feedbackBox');
        if (!box) return;
        box.classList.toggle('d-none', !(fb.enabled || fb.done));
        if (fb.done) showThanks(fb.rating, fb.review_url, '謝謝您的評分！');
    }

    function paintStars(n) {
        document.querySelectorAll('.feedback-star').forEach((b) => {
            const on = Number(b.dataset.value) <= n;
            b.classList.toggle('btn-warning', on);
            b.classList.toggle('btn-outline-warning', !on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    function showThanks(n, reviewUrl, message) {
        paintStars(n);
        document.querySelectorAll('.feedback-star, #feedbackSendBtn, #feedbackComment').forEach((el) => { el.disabled = true; });
        document.getElementById('feedbackMsg').textContent = message;
        const review = document.getElementById('feedbackReview');
        review.classList.toggle('d-none', !reviewUrl);
        if (reviewUrl) review.href = reviewUrl;
    }

    async function refresh() {
        if (['PickedUp', 'Cancelled'].includes(lastStatus)) return;
        try {
            apply(await Kiosk.api(`/api/track/${data.uuid}`));
        } catch (err) { /* 暫時斷線：下次再試 */ }
    }

    Kiosk.on(document, 'click', {
        rate: (btn) => {
            rating = Number(btn.dataset.value);
            paintStars(rating);
            document.getElementById('feedbackSendBtn').disabled = false;
        },
        'send-feedback': async (btn) => {
            if (!rating) return;
            btn.disabled = true;
            try {
                const res = await Kiosk.api(`/api/track/${data.uuid}/feedback`,
                    { json: { rating, comment: document.getElementById('feedbackComment').value } });
                showThanks(res.rating, res.review_url, res.message);
            } catch (err) {
                document.getElementById('feedbackMsg').textContent = err.message;
                btn.disabled = false;
            }
        },
        'enable-notify': async (btn) => {
            if (!window.Notification) { btn.textContent = '此瀏覽器不支援通知'; return; }
            const result = await Notification.requestPermission();
            btn.textContent = result === 'granted' ? '已開啟完成通知' : '未允許通知 (仍會震動提示)';
        },
    });
    apply(data);
    setInterval(refresh, 10000);
})();
