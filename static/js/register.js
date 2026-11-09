/*
 * 會員註冊 (/register) 與會員登入 (/face_login)。
 * - 登入模式：人臉辨識 (相機截圖送 /face_login) 或手機 + PIN (/phone_login)。
 * - 註冊模式：一般表單送出；照片可由檔案上傳，或用相機彈窗拍照後放入檔案欄位。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const { loginMode, faceEnabled = true } = Kiosk.pageData();
    const HOME_URL = '/';
    const CAMERA_CONSTRAINTS = { video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } } };

    const show = (el, visible) => el && el.classList.toggle('d-none', !visible);

    function stopStream(stream) {
        if (stream) stream.getTracks().forEach((track) => track.stop());
        return null;
    }

    async function openCamera(video) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error(Kiosk.t('reg_no_camera', '此裝置或瀏覽器不支援相機'));
        }
        const stream = await navigator.mediaDevices.getUserMedia(CAMERA_CONSTRAINTS);
        video.srcObject = stream;
        return stream;
    }

    /**
     * 鏡頭開關：start() 已開啟或開啟中不重複開 (不會覆寫舊 stream 而漏關)；等待瀏覽器權限期間呼叫 stop()
     * (關閉相機視窗、切到手機頁籤、離開頁面) 時，之後授權拿到的 stream 立即關閉，鏡頭不會在背景開著。
     * start() 回傳 true = 已開啟，false = 等待期間已被 stop() 取消；開啟失敗時拋出錯誤。
     */
    function cameraController(video) {
        let stream = null;
        let pending = null;
        let seq = 0;
        function stop() {
            seq += 1;
            pending = null;
            stream = stopStream(stream);
            video.srcObject = null;
        }
        function start() {
            if (stream) return Promise.resolve(true);
            if (pending) return pending;
            const token = ++seq;
            pending = openCamera(video).then((opened) => {
                if (token !== seq) {
                    stopStream(opened);
                    if (video.srcObject === opened) video.srcObject = null;
                    return false;
                }
                pending = null;
                stream = opened;
                return true;
            }, (err) => {
                if (token !== seq) return false;
                pending = null;
                throw err;
            });
            return pending;
        }
        return { start, stop };
    }

    /** 送出前的弱 PIN 檢查：規則與後端 app/utils.py 的 is_weak_pin 相同 (後端仍會再檢查一次) */
    function isWeakPin(pin, phone) {
        const value = String(pin || '').trim();
        if (!/^[0-9]+$/.test(value)) return false;
        const digits = Array.from(value, Number);
        const steps = new Set(digits.slice(1).map((d, i) => (d - digits[i] + 10) % 10));
        if (new Set(value).size === 1) return true;
        if (steps.size === 1 && (steps.has(1) || steps.has(9))) return true;
        if (value.length % 2 === 0 && value === value.slice(0, 2).repeat(value.length / 2)) return true;
        const tel = String(phone || '').trim();
        return Boolean(tel) && tel.endsWith(value);
    }

    /** 把 video 目前畫面畫到 canvas，回傳 JPEG Blob */
    function captureFrame(video, canvas) {
        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.95));
    }

    function setButton(btn, content, disabled) {
        btn.disabled = disabled;
        Kiosk.setHtml(btn, content);
    }

    // ==========================================================================
    // 1. 會員登入模式 (人臉辨識 / 手機號碼 + PIN)
    // ==========================================================================
    function initLogin() {
        const video = document.getElementById('loginVideo');
        const canvas = document.getElementById('loginCanvas');
        const cameraStatus = document.getElementById('loginCameraStatus');
        const faceBtn = document.getElementById('btn-login-snap');
        const phoneForm = document.getElementById('phoneLoginForm');
        const phoneBtn = document.getElementById('btn-phone-login');
        const camera = cameraController(video);

        const faceBtnIdle = html`<i class="bi bi-camera-fill me-1"></i> ${Kiosk.t('login', '登入')}`;
        const faceBtnRetry = html`<i class="bi bi-camera-fill me-1"></i> ${Kiosk.t('reg_face_retry', '重新辨識登入')}`;
        const phoneBtnIdle = html`<i class="bi bi-box-arrow-in-right me-1"></i> ${Kiosk.t('reg_phone_tab', '手機號碼登入')}`;

        async function startCamera() {
            try {
                if (await camera.start()) show(cameraStatus, false);
            } catch (err) {
                console.warn('相機開啟失敗:', err);
                cameraStatus.textContent = Kiosk.t('reg_camera_failed', '無法開啟相機，請確認瀏覽器權限，或改用手機號碼登入');
                show(cameraStatus, true);
            }
        }

        function stopCamera() {
            camera.stop();
        }

        function welcome(data) {
            alert(`🎉 ${Kiosk.t('welcome_back', '歡迎回來，')}${data.user_name}${Kiosk.t('welcome_back_b', '！')}`);
            window.location.href = HOME_URL;
        }

        async function faceLogin() {
            setButton(faceBtn, html`<span class="spinner-border spinner-border-sm me-2"></span>${Kiosk.t('reg_face_matching', '人臉比對中...')}`, true);
            const blob = await captureFrame(video, canvas);
            if (!blob) {
                alert(Kiosk.t('reg_capture_failed', '擷取畫面失敗，請確認鏡頭正常！'));
                setButton(faceBtn, faceBtnIdle, false);
                return;
            }
            const formData = new FormData();
            formData.append('photo', blob, 'login_snapshot.jpg');
            try {
                const data = await Kiosk.api('/face_login', { body: formData });
                stopCamera();
                welcome(data);
            } catch (err) {
                alert(err.message || Kiosk.t('reg_face_failed', '人臉辨識未通過，請對準鏡頭重新嘗試！'));
                setButton(faceBtn, faceBtnRetry, false);
            }
        }

        async function phoneLogin(ev) {
            ev.preventDefault();
            const phone = document.getElementById('loginPhoneInput').value.trim();
            const pin = document.getElementById('loginPinInput').value.trim();
            if (!phone || !pin) {
                alert(Kiosk.t('reg_need_phone_pin', '請輸入手機號碼與 4~6 位安全 PIN 碼！'));
                return;
            }
            setButton(phoneBtn, html`<span class="spinner-border spinner-border-sm me-2"></span>${Kiosk.t('reg_logging_in', '登入中...')}`, true);
            try {
                welcome(await Kiosk.api('/phone_login', { json: { phone, pin } }));
            } catch (err) {
                alert(err.message);
                setButton(phoneBtn, phoneBtnIdle, false);
            }
        }

        phoneForm.addEventListener('submit', phoneLogin);
        // 線上點餐 (線上端) 沒有人臉登入：只有手機 + PIN
        if (!faceEnabled) {
            document.getElementById('loginPhoneInput').focus();
            return;
        }
        // 切到手機頁籤時關閉鏡頭，切回人臉頁籤時重新開啟
        document.getElementById('face-tab').addEventListener('shown.bs.tab', startCamera);
        document.getElementById('phone-tab').addEventListener('shown.bs.tab', () => {
            stopCamera();
            document.getElementById('loginPhoneInput').focus();
        });
        window.addEventListener('pagehide', stopCamera);
        Kiosk.on(document, 'click', { 'face-login': () => faceLogin() });
        startCamera();
    }

    // ==========================================================================
    // 2. 會員註冊模式 (照片上傳 / 相機彈窗拍照)
    // ==========================================================================
    function initRegister() {
        const form = document.getElementById('registerForm');
        const submitBtn = document.getElementById('registerSubmitBtn');

        // 送出後鎖定按鈕，避免重複註冊
        const submitIdle = submitBtn.innerHTML;
        form.addEventListener('submit', () => {
            setButton(submitBtn, html`<span class="spinner-border spinner-border-sm me-2"></span>${Kiosk.t('reg_registering', '註冊中...')}`, true);
        });
        // 從下一頁按「上一頁」回來 (bfcache) 時恢復按鈕
        window.addEventListener('pageshow', () => {
            submitBtn.disabled = false;
            submitBtn.innerHTML = submitIdle;
        });

        // 弱 PIN (0000、1234、1212、手機末幾碼) 在送出前就由瀏覽器擋下並提示，不必送到後端被擋、整份表單清空
        const pinInput = document.getElementById('regPin');
        const phoneInput = document.getElementById('regPhone');
        if (pinInput && phoneInput) {
            const checkPin = () => pinInput.setCustomValidity(isWeakPin(pinInput.value, phoneInput.value)
                ? Kiosk.t('reg_weak_pin', 'PIN 碼太容易被猜中 (例如 0000、1234、1212 或手機末幾碼)，請換一組！') : '');
            pinInput.addEventListener('input', checkPin);
            phoneInput.addEventListener('input', checkPin);
            checkPin();
        }

        // 人臉辨識為選填：勾選同意才顯示拍照區並要求照片 (線上端沒有這個區塊)
        const faceConsent = document.getElementById('faceConsent');
        const photoInput = document.getElementById('photoInput');
        if (!faceConsent || !photoInput) return;
        const faceSection = document.getElementById('facePhotoSection');
        const syncFace = () => {
            show(faceSection, faceConsent.checked);
            photoInput.required = faceConsent.checked;
        };
        faceConsent.addEventListener('change', syncFace);
        syncFace();

        const photoStatus = document.getElementById('photoStatus');
        const modalEl = document.getElementById('cameraModal');
        const video = document.getElementById('cameraVideo');
        const canvas = document.getElementById('cameraCanvas');
        const guide = document.getElementById('faceGuide');
        const snapBtn = document.getElementById('snapBtn');
        const retakeBtn = document.getElementById('retakeBtn');
        const confirmBtn = document.getElementById('confirmBtn');
        const modal = Kiosk.modal(modalEl);
        const camera = cameraController(video);
        let capturedBlob = null;

        function setPhotoStatus(content) {
            Kiosk.setHtml(photoStatus, content);
            show(photoStatus, Boolean(content));
        }

        function stopCamera() {
            camera.stop();
        }

        function retake() {
            capturedBlob = null;
            show(video, true);
            show(canvas, false);
            show(guide, true);
            show(snapBtn, true);
            show(retakeBtn, false);
            show(confirmBtn, false);
        }

        async function openCameraModal() {
            retake();
            modal.show();
            try {
                await camera.start();
            } catch (err) {
                console.warn('相機存取失敗:', err);
                alert(Kiosk.t('reg_camera_denied', '無法存取攝影機，請檢查瀏覽器權限！'));
                modal.hide();
            }
        }

        async function snap() {
            capturedBlob = await captureFrame(video, canvas);
            show(video, false);
            show(canvas, true);
            show(guide, false);
            show(snapBtn, false);
            show(retakeBtn, true);
            show(confirmBtn, true);
        }

        function usePhoto() {
            if (!capturedBlob) return;
            const file = new File([capturedBlob], `camera_snap_${Date.now()}.jpg`, { type: 'image/jpeg' });
            const dt = new DataTransfer();
            dt.items.add(file);
            photoInput.files = dt.files;
            setPhotoStatus(html`<i class="bi bi-check-circle-fill text-success"></i> ${Kiosk.t('reg_photo_taken', '已成功拍攝相片：')}<strong>${file.name}</strong>`);
            modal.hide();
        }

        photoInput.addEventListener('change', () => {
            const file = photoInput.files && photoInput.files[0];
            setPhotoStatus(file ? html`<i class="bi bi-file-earmark-image text-info"></i> ${Kiosk.t('reg_file_chosen', '已選取檔案：')}<strong>${file.name}</strong>` : '');
        });

        // 彈窗關閉 (取消、右上角 X、使用此照片) 一律關閉鏡頭
        modalEl.addEventListener('hidden.bs.modal', stopCamera);
        window.addEventListener('pagehide', stopCamera);

        Kiosk.on(document, 'click', {
            'open-camera': () => openCameraModal(),
            snap: () => snap(),
            retake: () => retake(),
            'use-photo': () => usePhoto(),
        });
    }

    if (loginMode) initLogin(); else initRegister();
})();
