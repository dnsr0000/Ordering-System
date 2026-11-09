"""修正小組 sync-engine-2 的回歸測試 (app/sync/tracking.py)"""
from tests.support import login_staff, make_item, pos_checkout


def _revs(app, order_id):
    from app.extensions import db
    from app.models.order import Order
    with app.app_context():
        order = db.session.get(Order, order_id)
        return order.feed_rev, order.sync_rev


def test_local_order_commit_bumps_feed_rev_with_sync_rev(app):
    """R2-182：本機提交的訂單新增 / 異動，feed_rev 與 sync_rev 一起更新為本次提交序號 (API v1 cursor 收得到)"""
    from app.extensions import db
    from app.models.order import Order
    item_id = make_item(app, 'R2-182 feed_rev 測試品', price=80, stock=50)
    client = login_staff(app, 'fix_sync_engine_2_cashier', 'staff')
    resp = pos_checkout(client, item_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    order_id = resp.get_json()['order_id']

    feed_rev, sync_rev = _revs(app, order_id)
    assert sync_rev > 0
    assert feed_rev == sync_rev

    with app.app_context():
        order = db.session.get(Order, order_id)
        order.note = 'R2-182 本機修改'
        db.session.commit()
    feed_after, sync_after = _revs(app, order_id)
    assert sync_after > sync_rev
    assert feed_after == sync_after
