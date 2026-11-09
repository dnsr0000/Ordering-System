"""
修正小組 support-feedback-api-demo-2 的回歸測試：
- R2-185：不符合點餐頁顯示條件 (未滿 4 星或沒有留言) 的評價勾公開時，提示不會出現在點餐頁
- R2-188：只有 feedback 權限的員工在客服對話頁看不到「客服機器人設定」按鈕
"""
from app.extensions import db
from app.models.order import OrderFeedback
from tests.support import checkout, flashes, guest_client, line, login_staff, make_item


def _feedback(app, rating, comment):
    item = make_item(app, f'評價測試餐{rating}{len(comment)}', price=60)
    oid = checkout(guest_client(app), [line(item)]).get_json()['order_id']
    with app.app_context():
        from app.models.order import Order
        order = db.session.get(Order, oid)
        fb = OrderFeedback(tenant_id=order.tenant_id, order_id=oid, rating=rating, comment=comment)
        db.session.add(fb)
        db.session.commit()
        return fb.id


def test_public_flag_on_ineligible_feedback_warns(app, admin_client):
    low = _feedback(app, 3, '整體還不錯')
    no_comment = _feedback(app, 5, '')
    good = _feedback(app, 5, '非常好吃')
    for fb_id in (low, no_comment):
        r = admin_client.post(f'/admin/feedback/{fb_id}/reply', data={'reply': '謝謝', 'is_public': '1', 'days': '30'})
        assert r.status_code == 302
        msgs = flashes(admin_client)
        assert any('不會出現在點餐頁' in m for m in msgs), msgs
        assert not any('並公開顯示' in m for m in msgs), msgs
    admin_client.post(f'/admin/feedback/{good}/reply', data={'reply': '謝謝', 'is_public': '1', 'days': '30'})
    msgs = flashes(admin_client)
    assert any('並公開顯示' in m for m in msgs) and not any('不會出現' in m for m in msgs), msgs
    admin_client.post(f'/admin/feedback/{low}/reply', data={'reply': '', 'days': '30'})
    msgs = flashes(admin_client)
    assert msgs == ['已更新回覆'], msgs
    # 不留下公開評價，避免影響其他測試對點餐頁「顧客好評」的斷言
    with app.app_context():
        for fb in OrderFeedback.query.filter(OrderFeedback.id.in_((low, no_comment, good))).all():
            db.session.delete(fb)
        db.session.commit()


def test_support_page_hides_chatbot_settings_without_settings_permission(app, admin_client):
    from app.models.tenant import StaffAccount
    staff = login_staff(app, 'support_fb_only', 'staff')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='support_fb_only').first()
        acc.permissions = 'feedback'
        db.session.commit()
    page = staff.get('/admin/support')
    assert page.status_code == 200
    assert '客服機器人設定' not in page.get_data(as_text=True)
    assert '客服機器人設定' in admin_client.get('/admin/support').get_data(as_text=True)
