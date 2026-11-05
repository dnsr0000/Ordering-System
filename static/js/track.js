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
    let lastDelivery = data.delivery_status || '';
    let notified = data.status === 'Completed';

    function announceReady(d) {
        if (notified) return;
        notified = true;
        if (navigator.vibrate) navigator.vibrate([300, 150, 300, 150, 600]);
        Kiosk.beep(880, 400);
        if (window.Notification && Notification.permission === 'granted') {
            try { new Notification(Kiosk.t('track_ready_title', '餐點已完成'), { body: `${Kiosk.t('track_pickup_no', '取餐號碼')} ${Kiosk.pickupNo(d.pickup_number)}${Kiosk.t('track_ready_body', '，請至櫃檯取餐')}` }); } catch (err) { /* 部分手機不支援 */ }
        }
    }

    // ---------------------------------------------------------------- Web Push 訂閱
    function urlBase64ToUint8Array(base64) {
        const padding = '='.repeat((4 - (base64.length % 4)) % 4);
        const raw = window.atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
        return Uint8Array.from([...raw].map((ch) => ch.charCodeAt(0)));
    }

    async function subscribePush() {
        if (!data.push_enabled || !('serviceWorker' in navigator) || !('PushManager' in window)) return false;
        try {
            const vapid = await Kiosk.api('/api/push/vapid');
            if (!vapid.enabled || !vapid.public_key) return false;
            const reg = await navigator.serviceWorker.ready;
            const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(vapid.public_key) });
            await Kiosk.api(`/api/track/${data.uuid}/push`, { json: { subscription: sub.toJSON() } });
            return true;
        } catch (err) {
            console.warn('Web Push 訂閱失敗:', err.message);
            return false;
        }
    }

    function apply(d) {
        // 待分店確認的文字沿用本頁的 pending_branch 字串 (共用的 pickup_pending 字串多了「取餐號：」前綴)
        numberEl.textContent = d.pickup_pending ? Kiosk.t('pending_branch', '待分店確認') : Kiosk.pickupNo(d.pickup_number);
        statusEl.textContent = Kiosk.t(`status_${d.status}`, d.status_label);
        statusEl.className = `fs-4 fw-bold mt-2 ${d.status === 'Completed' ? 'text-success' : (d.status === 'Cancelled' ? 'text-danger' : '')}`;
        if (d.status === 'Completed') {
            hintEl.textContent = Kiosk.t('track_hint_ready', '餐點已完成，請至櫃檯取餐！');
            announceReady(d);
        } else if (d.status === 'PickedUp') {
            hintEl.textContent = Kiosk.t('track_hint_picked', '已取餐，祝您用餐愉快！');
        } else if (d.status === 'Cancelled') {
            hintEl.textContent = Kiosk.t('track_hint_cancelled', '此訂單已取消，如有疑問請洽櫃檯。');
        }
        lastStatus = d.status;
        lastDelivery = d.delivery_status || '';
        if (d.delivery_status) {
            document.getElementById('trackDelivery').classList.remove('d-none');
            document.getElementById('trackDeliveryLabel').textContent = Kiosk.t(`delivery_${d.delivery_status}`, d.delivery_label);
            document.getElementById('trackDriver').textContent = d.driver ? ` (${d.driver})` : '';
            if (d.status === 'Completed' && ['pending', 'assigned'].includes(d.delivery_status)) {
                hintEl.textContent = Kiosk.t('track_hint_wait_driver', '餐點已完成，等待外送員出發配送');
            }
            if (d.delivery_status === 'out') hintEl.textContent = Kiosk.t('track_hint_out', '外送員已出發，請留意電話！');
            if (d.delivery_status === 'delivered') hintEl.textContent = Kiosk.t('track_hint_delivered', '已送達，祝您用餐愉快！');
        }
        applyDriver(d);
        applyFeedback(d.feedback || {});
    }

    // ---------------------------------------------------------------- 外送員位置 (自有外送 GPS 回報)
    let myLocation = null;
    let lastPosition = null;

    function distanceKm(a, b) {
        const r = 6371;
        const p1 = (a.lat * Math.PI) / 180;
        const p2 = (b.lat * Math.PI) / 180;
        const dp = ((b.lat - a.lat) * Math.PI) / 180;
        const dl = ((b.lng - a.lng) * Math.PI) / 180;
        const x = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
        return 2 * r * Math.asin(Math.sqrt(x));
    }

    function showDistance() {
        const el = document.getElementById('trackDriverDistance');
        if (!el) return;
        if (!myLocation || !lastPosition) { el.classList.add('d-none'); return; }
        const km = distanceKm(myLocation, lastPosition);
        const label = km >= 1 ? `${km.toFixed(1)} km` : `${Math.round(km * 1000)} m`;
        const minutes = Math.max(1, Math.round((km / 25) * 60));   // 市區機車約 25 km/h
        el.textContent = Kiosk.t('track_distance', '外送員距離您約 {d}').replace('{d}', label)
            + Kiosk.t('track_distance_eta', '，預計 {n} 分鐘').replace('{n}', String(minutes));
        el.classList.remove('d-none');
    }

    function applyDriver(d) {
        const box = document.getElementById('trackDriverBox');
        if (!box) return;
        const show = d.delivery_status === 'out' && 'driver_position' in d;
        box.classList.toggle('d-none', !show);
        if (!show) return;
        const pos = d.driver_position;
        const updated = document.getElementById('trackDriverUpdated');
        const map = document.getElementById('trackDriverMap');
        if (!pos) {
            updated.textContent = Kiosk.t('track_driver_waiting', '等待外送員回報位置…');
            map.classList.add('d-none');
            lastPosition = null;
            showDistance();
            return;
        }
        lastPosition = { lat: pos.lat, lng: pos.lng };
        const age = Number(pos.age_seconds) || 0;
        const ago = age >= 60 ? Kiosk.t('track_driver_ago_min', '{n} 分鐘前').replace('{n}', String(Math.round(age / 60)))
            : Kiosk.t('track_driver_ago', '{n} 秒前').replace('{n}', String(age));
        updated.textContent = `${Kiosk.t('track_driver_updated', '更新於')} ${pos.at} (${ago})`;
        map.href = pos.map_url;
        map.classList.remove('d-none');
        showDistance();
    }

    // ---------------------------------------------------------------- 餐後評分
    let rating = 0;

    function applyFeedback(fb) {
        const box = document.getElementById('feedbackBox');
        if (!box) return;
        box.classList.toggle('d-none', !(fb.enabled || fb.done));
        if (fb.done) showThanks(fb.rating, fb.review_url, Kiosk.t('fb_thanks', '謝謝您的評分！'));
        const reply = document.getElementById('feedbackReply');
        if (reply) {
            reply.classList.toggle('d-none', !fb.reply);
            document.getElementById('feedbackReplyText').textContent = fb.reply || '';
        }
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
        if (lastStatus === 'Cancelled' || ['delivered', 'failed'].includes(lastDelivery)) return;
        if (lastStatus === 'PickedUp' && !lastDelivery) return;
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
        'share-location': (btn) => {
            if (!navigator.geolocation) { btn.textContent = Kiosk.t('track_loc_denied', '未允許定位，仍可用地圖連結查看'); return; }
            btn.disabled = true;
            navigator.geolocation.getCurrentPosition((p) => {
                myLocation = { lat: p.coords.latitude, lng: p.coords.longitude };
                btn.classList.add('d-none');
                showDistance();
            }, () => {
                btn.disabled = false;
                btn.textContent = Kiosk.t('track_loc_denied', '未允許定位，仍可用地圖連結查看');
            }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
        },
        'enable-notify': async (btn) => {
            if (!window.Notification) { btn.textContent = Kiosk.t('notify_unsupported', '此瀏覽器不支援通知'); return; }
            const result = await Notification.requestPermission();
            if (result !== 'granted') { btn.textContent = Kiosk.t('notify_denied', '未允許通知 (仍會震動提示)'); return; }
            btn.textContent = Kiosk.t('notify_on', '已開啟完成通知');
            // Web Push：離開頁面也收得到 (需店家啟用、HTTPS 網址與瀏覽器支援；否則只有頁面開著時通知)
            const pushed = await subscribePush();
            btn.textContent = pushed ? Kiosk.t('notify_on_push', '已開啟完成通知 (離開頁面也會通知)') : Kiosk.t('notify_on_page', '已開啟完成通知 (請保持此頁開啟)');
        },
    });
    apply(data);
    setInterval(refresh, 10000);
})();
