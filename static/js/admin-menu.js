/*
 * 店家管理後台：菜單品項 (新增/編輯/防呆/加購條件/AI 文案/圖片裁切) 與套餐組合。
 * 依賴 admin.js 提供的 window.AdminPage。
 */
(function () {
    'use strict';
    const { html } = Kiosk;
    const { data, menuItems, autoExpand, showModal } = window.AdminPage;
    const $ = (id) => document.getElementById(id);

    const allItems = [...menuItems.values()];
    const menuCategories = [...new Set(allItems.map((i) => (i.category || '').trim()).filter(Boolean))].sort();
    const menuItemNames = allItems.map((i) => i.name).filter(Boolean);

    // ---------------------------------------------------------------- 加購條件
    /** 依條件類型 (any / category / item) 產生目標下拉選單；selected 為要預選的值 */
    function toggleAddonTarget(prefix, selected = '') {
        const typeSel = $(`${prefix}-addon-type`);
        const targetSel = $(`${prefix}-addon-target`);
        if (!typeSel || !targetSel) return;

        if (typeSel.value === 'any') {
            targetSel.style.display = 'none';
            targetSel.required = false;
            targetSel.replaceChildren();
            return;
        }
        const isCategory = typeSel.value === 'category';
        const selfName = ($(`${prefix}-name`)?.value || '').trim();
        // 指定餐點時排除自己
        const options = isCategory ? menuCategories : menuItemNames.filter((name) => name !== selfName);
        targetSel.style.display = 'block';
        targetSel.required = true;
        Kiosk.setHtml(targetSel, html`
            <option value="" disabled selected>${isCategory ? '-- 請選擇指定分類 --' : '-- 請選擇指定餐點 --'}</option>
            ${options.length
                ? options.map((v) => html`<option value="${v}">${v}</option>`)
                : html`<option value="" disabled>${isCategory ? '無其他分類可選' : '無其他餐點可選'}</option>`}`);
        if (selected && options.includes(selected)) targetSel.value = selected;
    }

    /** 加購價與「開放作為加購品」開關雙向連動 */
    function bindAddonSync(prefix) {
        const input = $(`${prefix}-addon-price`);
        const toggle = $(`${prefix}-can-addon`);
        const rules = $(`${prefix}-addon-rules`);
        if (!input || !toggle) return;
        const syncRules = () => { if (rules) rules.style.display = toggle.checked ? 'block' : 'none'; };
        input.addEventListener('input', () => {
            toggle.checked = Number(input.value) > 0;
            syncRules();
        });
        toggle.addEventListener('change', () => {
            if (!toggle.checked) {
                input.value = 0;
            } else if (Number(input.value) <= 0) {
                input.value = '';
                input.focus();
            }
            syncRules();
        });
    }

    /** 庫存數量與「標記為售完」開關雙向連動 */
    function bindStockSoldOutSync(prefix) {
        const stock = $(`${prefix}-stock`);
        const soldOut = $(`${prefix}-sold-out`);
        if (!stock || !soldOut) return;
        stock.addEventListener('input', () => {
            if (stock.value === '') return; // 刪除中的空白不判斷
            soldOut.checked = Number(stock.value) <= 0;
        });
        soldOut.addEventListener('change', () => { if (soldOut.checked) stock.value = 0; });
    }

    /** 特價金額與「限時特價」開關連動，送出前檢查單價 / 特價 / 加購價 */
    function bindPriceValidation(prefix, form) {
        const price = $(`${prefix}-price`);
        const discount = $(`${prefix}-discount-price`);
        const discountToggle = $(`${prefix}-discount`);
        const addon = $(`${prefix}-addon-price`);
        const addonToggle = $(`${prefix}-can-addon`);
        if (!form || !price) return;

        discount.addEventListener('input', () => { discountToggle.checked = Number(discount.value) > 0; });
        discountToggle.addEventListener('change', () => {
            if (!discountToggle.checked) {
                discount.value = 0;
            } else if (Number(discount.value) <= 0) {
                discount.value = '';
                discount.focus();
            }
        });

        form.addEventListener('submit', (ev) => {
            const fail = (msg, el) => {
                ev.preventDefault();
                window.alert(msg);
                el.focus();
            };
            const priceVal = Number(price.value || 0);
            if (priceVal <= 0) return fail('⚠️ 原單價必須大於 0！', price);
            if (discountToggle.checked) {
                const val = Number(discount.value || 0);
                if (val <= 0) return fail('⚠️ 已開啟特價標籤，請輸入大於 0 的特價金額！', discount);
                if (val >= priceVal) return fail('⚠️ 特價金額必須小於原單價！', discount);
            }
            if (addonToggle.checked) {
                const val = Number(addon.value || 0);
                if (val <= 0) return fail('⚠️ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！', addon);
                if (val >= priceVal) return fail(`⚠️ 加購專屬價 ($${val}) 不得高於或等於原單價 ($${priceVal})！`, addon);
            }
            return undefined;
        });
    }

    /** 編輯品項時的紅利商城設定防呆 */
    function bindRewardValidation(form) {
        const price = $('edit-price');
        const toggle = $('edit-is-reward');
        const points = $('edit-reward-points');
        const discount = $('edit-reward-discount');
        if (!form || !toggle) return;
        form.addEventListener('submit', (ev) => {
            points.classList.remove('is-invalid');
            discount.classList.remove('is-invalid');
            if (!toggle.checked || ev.defaultPrevented) return;
            const fail = (msg, el) => {
                ev.preventDefault();
                el.classList.add('is-invalid');
                el.focus();
                window.alert(msg);
            };
            const priceVal = Number(price.value || 0);
            const pts = Number(points.value || 0);
            const disc = Number(discount.value || 0);
            if (pts <= 0) {
                fail('⚠️ 啟用紅利兌換時，「兌換所需點數」必須大於 0！', points);
            } else if (priceVal > 0 && pts > priceVal) {
                fail(`⚠️ 兌換所需點數 (${pts} 點) 不得高於原單價 ($${priceVal})！`, points);
            } else if (disc > 0 && disc >= pts) {
                fail('⚠️ 「限時優惠點數」必須小於「兌換所需點數」！', discount);
            }
        });
    }

    // ---------------------------------------------------------------- 編輯品項
    function openEditItem(id) {
        const d = menuItems.get(String(id));
        if (!d) return;
        $('editItemForm').action = `/admin/edit/${encodeURIComponent(d.id)}`;
        $('edit-name').value = d.name;
        $('edit-category').value = d.category;
        $('edit-modifiers').value = d.modifiers || 'none';
        $('edit-price').value = Math.round(Number(d.price) || 0);
        $('edit-stock').value = d.stock || 0;
        $('edit-total-stock').value = d.total_stock || 0;
        $('edit-loaded-stock').value = d.stock || 0;
        $('edit-loaded-total-stock').value = d.total_stock || 0;
        $('edit-sold-out').checked = !!d.is_sold_out;
        $('edit-discount').checked = !!d.is_discount;
        $('edit-discount-price').value = Math.round(Number(d.discount_price) || 0);
        $('edit-can-addon').checked = !!d.can_be_add_on;
        $('edit-addon-price').value = Math.round(Number(d.add_on_price) || 0);
        $('edit-description').value = d.description || '';
        $('edit-recommended').checked = !!d.is_recommended;
        $('edit-new').checked = !!d.is_new;
        $('edit-is-reward').checked = !!d.is_reward;
        $('edit-reward-points').value = d.reward_points || 0;
        $('edit-reward-discount').value = d.reward_discount_points || 0;
        $('edit-from-tab').value = window.AdminPage.activeTab() === 'rewards' ? 'rewards' : 'menu';
        $('edit-image-file').value = '';
        $('edit-image-preview').src = d.image_path ? data.staticMenuUrl + d.image_path : data.placeholderImg;

        $('edit-addon-type').value = d.addon_trigger_type || 'any';
        toggleAddonTarget('edit', d.addon_trigger_target || '');
        $('edit-addon-rules').style.display = d.can_be_add_on ? 'block' : 'none';

        showModal('editItemModal');
    }

    // ---------------------------------------------------------------- AI 文案
    async function generateDescription(btn) {
        const mode = btn.dataset.mode;
        const nameInput = $(`${mode}-name`);
        const descInput = $(`${mode}-description`);
        const name = (nameInput?.value || '').trim();
        const category = ($(`${mode}-category`)?.value || '').trim();
        if (!name) {
            window.alert('⚠️ 請先輸入「餐點名稱」，AI 才能為您量身打造文案！');
            nameInput?.focus();
            return;
        }
        const original = [...btn.childNodes];
        btn.disabled = true;
        Kiosk.setHtml(btn, html`<span class="spinner-border spinner-border-sm me-1" role="status"></span> 生成中...`);
        try {
            const res = await Kiosk.api('/api/generate_item_description', { json: { name, category } });
            descInput.value = res.description || '';
            autoExpand(descInput);
            descInput.focus();
        } catch (err) {
            window.alert(`❌ ${err.message}`);
        } finally {
            btn.disabled = false;
            btn.replaceChildren(...original);
        }
    }

    // ---------------------------------------------------------------- 圖片裁切 (1:1)
    const cropModalEl = $('cropModal');
    const cropTarget = $('crop-image-target');
    let cropper = null;
    let cropInput = null;

    function startCrop(input) {
        const file = input.files && input.files[0];
        if (!file) return;
        cropInput = input;
        const reader = new FileReader();
        reader.addEventListener('load', () => {
            cropTarget.src = reader.result;
            showModal('cropModal');
        });
        reader.readAsDataURL(file);
    }

    function confirmCrop() {
        if (!cropper || !cropInput) return;
        const canvas = cropper.getCroppedCanvas({ width: 600, height: 600 });
        const input = cropInput;
        canvas.toBlob((blob) => {
            if (!blob) return;
            const transfer = new DataTransfer();
            transfer.items.add(new File([blob], `cropped_${Date.now()}.jpg`, { type: 'image/jpeg' }));
            input.files = transfer.files;
            const preview = $(input.dataset.preview);
            if (preview) {
                preview.src = canvas.toDataURL('image/jpeg');
                preview.hidden = false;
            }
            bootstrap.Modal.getInstance(cropModalEl).hide();
        }, 'image/jpeg', 0.9);
    }

    if (cropModalEl) {
        cropModalEl.addEventListener('shown.bs.modal', () => {
            if (cropper) cropper.destroy();
            cropper = new Cropper(cropTarget, { aspectRatio: 1, viewMode: 1, autoCropArea: 0.9, responsive: true, background: false });
        });
        cropModalEl.addEventListener('hidden.bs.modal', () => {
            if (cropper) cropper.destroy();
            cropper = null;
        });
    }

    // ---------------------------------------------------------------- 套餐配餐
    const sideSelect = (prefix, level) => $(`${prefix}-side${level}`);

    /** 已在其他欄位選取的配餐，在其餘下拉選單中停用 */
    function updateComboSideOptions(prefix) {
        const selects = [1, 2, 3].map((n) => sideSelect(prefix, n)).filter(Boolean);
        const chosen = selects.map((s) => s.value).filter(Boolean);
        selects.forEach((s) => {
            Array.from(s.options).forEach((opt) => {
                if (opt.value) opt.disabled = chosen.includes(opt.value) && s.value !== opt.value;
            });
        });
    }

    /** 選了第 n 項配餐才展開第 n+1 項；清空時一併收合後面的欄位 */
    function checkComboSides(prefix, level) {
        const current = sideSelect(prefix, level);
        if (current.value) {
            const dup = [1, 2, 3].some((n) => n !== level && sideSelect(prefix, n)?.value === current.value);
            if (dup) {
                window.alert('⚠️ 此配餐已在其他欄位選取，不可重複選擇！');
                current.value = '';
            }
        }
        if (current.value) {
            const next = $(`${prefix}-side${level + 1}-group`);
            if (next) next.style.display = 'block';
        } else {
            for (let n = level + 1; n <= 3; n += 1) {
                const group = $(`${prefix}-side${n}-group`);
                if (group) group.style.display = 'none';
                const s = sideSelect(prefix, n);
                if (s) {
                    s.value = '';
                    syncSideCustomizable(prefix, n);
                }
            }
        }
        updateComboSideOptions(prefix);
    }

    /** 配餐本身有客製化群組時才顯示「開放此配餐客製化」開關 */
    function syncSideCustomizable(prefix, level, customizable = null) {
        const select = sideSelect(prefix, level);
        const checkbox = $(`${prefix}-side${level}-cust`);
        if (!select || !checkbox) return;
        const box = checkbox.closest('.form-check');
        const opt = select.options[select.selectedIndex];
        const modifiers = opt ? (opt.dataset.modifiers || 'none') : 'none';
        if (!select.value || modifiers === 'none') {
            checkbox.checked = false;
            checkbox.disabled = true;
            box.style.display = 'none';
        } else {
            box.style.display = 'block';
            checkbox.disabled = false;
            checkbox.checked = customizable === null ? true : Boolean(customizable);
        }
    }

    function openEditCombo(btn) {
        const d = btn.dataset;
        $('editComboForm').action = `/admin/edit_combo/${encodeURIComponent(d.id)}`;
        $('edit-combo-main').value = d.mainId || '';
        $('edit-combo-name').value = d.name || '';
        $('edit-combo-price').value = d.price || 0;
        $('edit-combo-desc').value = d.desc || '';
        [1, 2, 3].forEach((n) => {
            sideSelect('edit-combo', n).value = d[`item${n}`] || '';
            $(`edit-combo-side${n}-group`).style.display = (n === 1 || d[`item${n - 1}`]) ? 'block' : 'none';
            syncSideCustomizable('edit-combo', n, d[`item${n}Cust`] === '1');
        });
        $('edit-combo-can-addon').checked = d.canAddon !== '0';
        updateComboSideOptions('edit-combo');
        showModal('editComboModal');
    }

    // ---------------------------------------------------------------- 綁定事件
    const addForm = $('addItemForm');
    const editForm = $('editItemForm');
    ['add', 'edit'].forEach((prefix) => {
        bindAddonSync(prefix);
        bindStockSoldOutSync(prefix);
        bindPriceValidation(prefix, prefix === 'add' ? addForm : editForm);
    });
    bindRewardValidation(editForm);

    document.querySelectorAll('.addon-type-select').forEach((sel) => {
        sel.addEventListener('change', () => toggleAddonTarget(sel.dataset.prefix));
    });
    document.querySelectorAll('.crop-input').forEach((input) => {
        input.addEventListener('change', () => startCrop(input));
    });
    document.querySelectorAll('.combo-side-select').forEach((sel) => {
        sel.addEventListener('change', () => {
            const level = Number(sel.dataset.level);
            checkComboSides(sel.dataset.prefix, level);
            syncSideCustomizable(sel.dataset.prefix, level);
        });
    });
    // 編輯視窗顯示後才能正確計算描述欄高度
    $('editItemModal')?.addEventListener('shown.bs.modal', () => autoExpand($('edit-description')));

    Kiosk.on(document, 'click', {
        'edit-item': (el) => openEditItem(el.dataset.id),
        'edit-combo': (el) => openEditCombo(el),
        'ai-description': (el) => generateDescription(el),
        'confirm-crop': confirmCrop,
    });
})();
