"""templates/pos.html 跨檔請求的回歸測試 (B-201 / B-225 / B-304)"""


def test_pos_header_links_carry_shop_and_admin_entries(staff_client, manager_client):
    page = staff_client.get('/pos').get_data(as_text=True)
    # B-201：叫號看板 / 打卡頁帶店家代碼，多店家主機在新瀏覽器開啟時不會落到預設店家
    assert 'href="/counter"' not in page
    assert '/counter?shop=' in page and '/timeclock?shop=' in page
    # B-225：收據有外送費列 (checkout.js 依 delivery_fee 顯示)
    assert 'id="rcpt-delivery-row"' in page and 'id="rcpt-delivery"' in page
    # B-304：櫃檯可從 POS 進入外送派車與團體預購；沒有 feedback 權限看不到客服對話
    assert 'href="/admin/dispatch"' in page and 'href="/admin/group-orders"' in page
    assert 'href="/admin/support"' not in page
    # 店長預設有 feedback 權限 → 顯示客服對話
    assert 'href="/admin/support"' in manager_client.get('/pos').get_data(as_text=True)
