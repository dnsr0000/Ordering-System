"""修正小組 sync-engine-1 的回歸測試 (直接以 Applier 套用同步資料；套用的結果一律 rollback，不影響其他測試)"""
import json
import os
import sqlite3
import uuid as uuidlib
from contextlib import closing, contextmanager
from datetime import datetime, timedelta

import sqlalchemy as sa

from app.extensions import db
from tests.support import get_or_create_tenant

OLD = datetime(2024, 1, 2, 3, 4, 5)
NEW = datetime(2025, 6, 7, 8, 9, 10)


def _uuid():
    return str(uuidlib.uuid4())


def _encoded(model, row_id):
    from app.sync.engine import encode_rows
    from app.sync.registry import BY_MODEL
    table = model.__table__
    row = db.session.execute(sa.select(table).where(table.c.id == row_id)).mappings().one()
    return encode_rows(BY_MODEL[model], [row])[0]


def _set_updated_at(model, row_id, when):
    table = model.__table__
    db.session.execute(sa.update(table).where(table.c.id == row_id).values(updated_at=when))
    db.session.commit()


@contextmanager
def _ctx(app, slug):
    with app.app_context():
        tid = get_or_create_tenant(app, slug)
        try:
            yield tid
        finally:
            db.session.rollback()


def _add(obj):
    db.session.add(obj)
    db.session.commit()
    return obj.id


# ---------------------------------------------------------------------------
# B-168：每店一筆 / 自然鍵第一次配對不套用 LWW
# ---------------------------------------------------------------------------
def test_b168_hub_keeps_configured_reward_setting_against_new_node_defaults(app):
    from app.models.user import RewardSetting
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b168') as tid:
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        if rs is None:
            rs = RewardSetting(tenant_id=tid)
            db.session.add(rs)
        rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = 5, 200, 30
        db.session.commit()
        _set_updated_at(RewardSetting, rs.id, OLD)
        hub_uuid = rs.uuid
        incoming = _encoded(RewardSetting, rs.id)
        incoming.update(uuid=_uuid(), points_per_dollar=1, max_discount_per_order=0, spend_per_point=100,
                        updated_at=NEW.isoformat())   # 新分店開機建立的預設值 (較新)
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'reward_setting': [incoming]})
        rs = db.session.get(RewardSetting, rs.id)
        assert (rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point) == (5, 200, 30)
        assert rs.uuid == hub_uuid
        assert rs.updated_at > NEW   # 比分店那一筆新：分店改用線上 uuid 後也會收到線上的設定
        assert result.aliases and result.aliases[0]['canonical'] == hub_uuid


def test_b168_node_takes_hub_reward_setting_over_local_seed(app):
    from app.models.user import RewardSetting
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b168n') as tid:
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        if rs is None:
            rs = RewardSetting(tenant_id=tid)
            db.session.add(rs)
        rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = 1, 0, 100
        db.session.commit()
        _set_updated_at(RewardSetting, rs.id, NEW)   # 本機預設列較新
        incoming = _encoded(RewardSetting, rs.id)
        hub_uuid = _uuid()
        incoming.update(uuid=hub_uuid, points_per_dollar=5, max_discount_per_order=200, spend_per_point=30,
                        updated_at=OLD.isoformat())
        Applier(tid, 'node').apply({'reward_setting': [incoming]})
        rs = db.session.get(RewardSetting, rs.id)
        assert rs.uuid == hub_uuid
        assert (rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point) == (5, 200, 30)


# ---------------------------------------------------------------------------
# B-171：同手機重新註冊的空白資料不可蓋掉原會員
# ---------------------------------------------------------------------------
def test_b171_natural_key_member_merge_keeps_canonical_fields(app):
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b171') as tid:
        uid = _add(User(tenant_id=tid, name='原會員', phone='0917171171', photo_path='', line_user_id='Uabc',
                        marketing_opt_out=True, birthday_gift_year=2026, email='a@b.c'))
        _set_updated_at(User, uid, OLD)
        incoming = _encoded(User, uid)
        incoming.update(uuid=_uuid(), name='新註冊', line_user_id='', marketing_opt_out=False, birthday_gift_year=0,
                        email='', updated_at=NEW.isoformat())
        Applier(tid, 'hub', source_node_uuid='node-x').apply({'user': [incoming]})
        u = db.session.get(User, uid)
        assert (u.name, u.line_user_id, u.marketing_opt_out, u.birthday_gift_year, u.email) == \
            ('原會員', 'Uabc', True, 2026, 'a@b.c')


def test_b171_blank_canonical_fields_are_filled_from_other_row(app):
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b171b') as tid:
        uid = _add(User(tenant_id=tid, name='原會員', phone='0917171172', photo_path='', email=''))
        incoming = _encoded(User, uid)
        incoming.update(uuid=_uuid(), name='分店', email='x@y.z', marketing_opt_out=True)
        Applier(tid, 'hub', source_node_uuid='node-x').apply({'user': [incoming]})
        u = db.session.get(User, uid)
        assert (u.name, u.email, u.marketing_opt_out) == ('原會員', 'x@y.z', True)


# ---------------------------------------------------------------------------
# B-169：不可使用其他店家的餐點圖片路徑
# ---------------------------------------------------------------------------
def test_b169_hub_rejects_menu_image_used_by_other_tenant(app):
    from app.models.menu import MenuItem
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b169') as tid:
        victim = get_or_create_tenant(app, 'fix-se1-b169-victim')
        path = '987/menu_victim0001.jpg'
        _add(MenuItem(tenant_id=victim, name='受害餐點', category='主餐', price=100, image_path=path))
        own = _add(MenuItem(tenant_id=tid, name='本店餐點', category='主餐', price=50, image_path=''))
        row = _encoded(MenuItem, own)
        row.update(uuid=_uuid(), name='假餐點', image_path=path)
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'menu_item': [row]})
        assert result.conflicts and '其他店家' in result.conflicts[0]['reason']
        assert MenuItem.query.filter_by(tenant_id=tid, image_path=path) \
            .execution_options(skip_tenant_filter=True).count() == 0
        # 本店自己的新圖片照收
        row2 = _encoded(MenuItem, own)
        row2.update(uuid=_uuid(), name='新餐點', image_path='1/menu_ownimage0001.jpg')
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'menu_item': [row2]})
        assert not result.conflicts and result.applied == 1


# ---------------------------------------------------------------------------
# B-173 / B-177 / R2-060 / R2-061：同一批的刪除與改名、刪除後重建
# ---------------------------------------------------------------------------
def test_b173_rename_tier_to_deleted_name_in_same_batch(app):
    from app.models.member import MemberTier
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b173') as tid:
        gold = _add(MemberTier(tenant_id=tid, name='金卡'))
        silver = _add(MemberTier(tenant_id=tid, name='銀卡'))
        gold_uuid = db.session.get(MemberTier, gold).uuid
        _set_updated_at(MemberTier, silver, OLD)
        row = _encoded(MemberTier, silver)
        row.update(name='金卡', updated_at=NEW.isoformat())
        tomb = {'uuid': _uuid(), 'table_name': 'member_tier', 'row_uuid': gold_uuid, 'deleted_at': NEW.isoformat()}
        Applier(tid, 'node').apply({'member_tier': [row], 'sync_tombstone': [tomb]})
        db.session.flush()
        assert db.session.get(MemberTier, gold) is None
        assert db.session.get(MemberTier, silver).name == '金卡'


def test_b173_rename_chain_and_swap_do_not_raise(app):
    from app.models.member import MemberTier
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b173b') as tid:
        a = _add(MemberTier(tenant_id=tid, name='銀卡'))
        b = _add(MemberTier(tenant_id=tid, name='金卡'))
        for i in (a, b):
            _set_updated_at(MemberTier, i, OLD)
        ra, rb = _encoded(MemberTier, a), _encoded(MemberTier, b)
        ra.update(name='金卡', updated_at=NEW.isoformat())     # 銀卡→金卡 (id 較小，先處理)
        rb.update(name='鑽石', updated_at=NEW.isoformat())     # 金卡→鑽石
        result = Applier(tid, 'node').apply({'member_tier': [ra, rb]})
        db.session.flush()
        assert (db.session.get(MemberTier, a).name, db.session.get(MemberTier, b).name) == ('金卡', '鑽石')
        assert not result.conflicts
        # 對調名稱：不可丟出 IntegrityError，記成衝突
        ra, rb = _encoded(MemberTier, a), _encoded(MemberTier, b)
        later = (NEW + timedelta(days=1)).isoformat()
        ra.update(name='鑽石', updated_at=later)
        rb.update(name='金卡', updated_at=later)
        result = Applier(tid, 'node').apply({'member_tier': [ra, rb]})
        db.session.flush()
        assert len(result.conflicts) == 2


def test_b177_coupon_code_reuse_after_delete_in_same_batch(app):
    from app.models.coupon import Coupon
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b177') as tid:
        x = _add(Coupon(tenant_id=tid, code='SALE177', title='X', discount_value=10))
        y = _add(Coupon(tenant_id=tid, code='OLD177', title='Y', discount_value=20))
        _set_updated_at(Coupon, y, OLD)
        row = _encoded(Coupon, y)
        row.update(code='SALE177', updated_at=NEW.isoformat())
        tomb = {'uuid': _uuid(), 'table_name': 'coupon', 'row_uuid': db.session.get(Coupon, x).uuid}
        result = Applier(tid, 'node').apply({'coupon': [row], 'sync_tombstone': [tomb]})
        db.session.flush()
        assert db.session.get(Coupon, x) is None
        assert db.session.get(Coupon, y).code == 'SALE177'
        assert not result.conflicts


def test_r2_061_node_reregistered_member_does_not_inherit_deleted_account(app):
    from app.models.sync import PointLedger
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-r2061') as tid:
        old = _add(User(tenant_id=tid, name='舊帳號', phone='0926161061', photo_path='', points=300))
        assert PointLedger.query.filter_by(user_id=old).execution_options(skip_tenant_filter=True).count() == 1
        old_uuid = db.session.get(User, old).uuid
        row = _encoded(User, old)
        new_uuid = _uuid()
        row.update(uuid=new_uuid, name='新帳號')
        tomb = {'uuid': _uuid(), 'table_name': 'user', 'row_uuid': old_uuid}
        Applier(tid, 'node').apply({'user': [row], 'sync_tombstone': [tomb]})
        db.session.flush()
        users = User.query.filter_by(tenant_id=tid, phone='0926161061').execution_options(skip_tenant_filter=True).all()
        assert [(u.uuid, u.name) for u in users] == [(new_uuid, '新帳號')]
        assert PointLedger.query.filter_by(user_id=users[0].id).execution_options(skip_tenant_filter=True).count() == 0


def test_r2_060_hub_recreated_member_is_not_aliased_to_deleted_one(app):
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-r2060') as tid:
        old = _add(User(tenant_id=tid, name='舊帳號', phone='0926060060', photo_path=''))
        row = _encoded(User, old)
        new_uuid = _uuid()
        row.update(uuid=new_uuid, name='重新註冊')
        tomb = {'uuid': _uuid(), 'table_name': 'user', 'row_uuid': db.session.get(User, old).uuid}
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'user': [row], 'sync_tombstone': [tomb]})
        db.session.flush()
        assert not result.aliases
        users = User.query.filter_by(tenant_id=tid, phone='0926060060').execution_options(skip_tenant_filter=True).all()
        assert [u.uuid for u in users] == [new_uuid]


# ---------------------------------------------------------------------------
# B-176：不可上傳其他分店訂單的刪除紀錄
# ---------------------------------------------------------------------------
def test_b176_hub_rejects_tombstone_for_other_nodes_order(app):
    from app.models.order import Order
    from app.models.sync import SyncTombstone
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b176') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-b'))
        order_uuid = db.session.get(Order, oid).uuid
        tomb = {'uuid': _uuid(), 'table_name': 'order', 'row_uuid': order_uuid}
        result = Applier(tid, 'hub', source_node_uuid='node-a').apply({'sync_tombstone': [tomb]})
        db.session.flush()
        assert result.conflicts
        assert SyncTombstone.query.filter_by(row_uuid=order_uuid).execution_options(skip_tenant_filter=True).count() == 0


# ---------------------------------------------------------------------------
# B-178 / R2-063：訂單合併
# ---------------------------------------------------------------------------
def test_b178_cancelled_and_refunded_in_one_batch_stays_refunded(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.services.order_lifecycle import flag_refund_if_paid
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b178') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, payment_status='paid', node_uuid='node-a'))
        row = _encoded(Order, oid)
        row.update(status=OrderStatus.CANCELLED, payment_status='refunded', updated_at=NEW.isoformat())
        result = Applier(tid, 'node').apply({'order': [row]})
        for order in result.cancelled_orders:
            flag_refund_if_paid(order.id)
        db.session.flush()
        db.session.expire_all()
        order = db.session.get(Order, oid)
        assert (order.status, order.payment_status) == (OrderStatus.CANCELLED, 'refunded')


def test_r2_063_rush_flag_is_merged(app):
    from app.models.order import Order
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-r2063') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-a'))
        _set_updated_at(Order, oid, OLD)
        row = _encoded(Order, oid)
        row.update(is_rush=True, updated_at=NEW.isoformat())
        Applier(tid, 'node').apply({'order': [row]})
        assert db.session.get(Order, oid).is_rush is True


# ---------------------------------------------------------------------------
# B-170 / B-180：帳本補 order_id、取消訂單的憑證改為已退點
# ---------------------------------------------------------------------------
def test_b170_insert_only_ledger_gets_order_id_later(app):
    from app.models.order import Order
    from app.models.sync import PointLedger
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b170') as tid:
        uid = _add(User(tenant_id=tid, name='會員', phone='0917017017', photo_path=''))
        oid = _add(Order(tenant_id=tid, total_price=100, payment_status='paid'))
        lid = _add(PointLedger(tenant_id=tid, user_id=uid, delta=0, reason='reward_item'))
        row = _encoded(PointLedger, lid)
        row['order_id'] = db.session.get(Order, oid).uuid
        Applier(tid, 'node').apply({'point_ledger': [row]})
        assert db.session.get(PointLedger, lid).order_id == oid


def test_b180_used_voucher_becomes_refunded_when_order_cancelled(app):
    from app.models.enums import OrderStatus, VoucherStatus
    from app.models.order import Order
    from app.models.user import RewardRedemption, User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b180') as tid:
        uid = _add(User(tenant_id=tid, name='會員', phone='0918018018', photo_path=''))
        oid = _add(Order(tenant_id=tid, total_price=100, status=OrderStatus.CANCELLED))
        vid = _add(RewardRedemption(tenant_id=tid, user_id=uid, points_cost=30, status=VoucherStatus.USED, order_id=oid))
        row = _encoded(RewardRedemption, vid)
        row['status'] = VoucherStatus.REFUNDED
        Applier(tid, 'node').apply({'reward_redemption': [row]})
        assert db.session.get(RewardRedemption, vid).status == VoucherStatus.REFUNDED
        # 線上端：取消訂單的退點不算重複使用；之後舊的「已使用」也不可蓋回去
        db.session.get(RewardRedemption, vid).status = VoucherStatus.USED
        db.session.flush()
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'reward_redemption': [row]})
        assert db.session.get(RewardRedemption, vid).status == VoucherStatus.REFUNDED
        assert not result.reused_vouchers
        row['status'] = VoucherStatus.USED
        Applier(tid, 'node').apply({'reward_redemption': [row]})
        assert db.session.get(RewardRedemption, vid).status == VoucherStatus.REFUNDED


def test_b179_used_then_refunded_elsewhere_is_reported_on_hub(app):
    from app.models.enums import VoucherStatus
    from app.models.user import RewardRedemption, User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b179b') as tid:
        uid = _add(User(tenant_id=tid, name='會員', phone='0917917918', photo_path=''))
        vid = _add(RewardRedemption(tenant_id=tid, user_id=uid, points_cost=30, status=VoucherStatus.USED))
        row = _encoded(RewardRedemption, vid)
        row['status'] = VoucherStatus.REFUNDED
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'reward_redemption': [row]})
        assert db.session.get(RewardRedemption, vid).status == VoucherStatus.USED
        assert [v.id for v in result.reused_vouchers] == [vid]
        assert not Applier(tid, 'node').apply({'reward_redemption': [row]}).reused_vouchers


def test_b179_refunded_then_used_voucher_is_reported(app):
    from app.models.enums import VoucherStatus
    from app.models.user import RewardRedemption, User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b179') as tid:
        uid = _add(User(tenant_id=tid, name='會員', phone='0917917917', photo_path=''))
        vid = _add(RewardRedemption(tenant_id=tid, user_id=uid, points_cost=30, status=VoucherStatus.REFUNDED))
        row = _encoded(RewardRedemption, vid)
        row['status'] = VoucherStatus.USED
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'reward_redemption': [row]})
        assert db.session.get(RewardRedemption, vid).status == VoucherStatus.USED
        assert [v.id for v in result.reused_vouchers] == [vid]


# ---------------------------------------------------------------------------
# B-172 / B-175：員工帳號安全優先
# ---------------------------------------------------------------------------
def _staff(tid, username, role='staff', **kw):
    from app.models.tenant import StaffAccount
    acc = StaffAccount(tenant_id=tid, username=username, role=role, **kw)
    acc.set_password('old-password-123')
    return _add(acc)


def test_b172_node_applies_hub_deactivation_even_if_local_row_is_newer(app):
    from app.models.tenant import StaffAccount
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b172') as tid:
        sid = _staff(tid, f'se1-b172-{_uuid()[:8]}')
        _set_updated_at(StaffAccount, sid, NEW)   # 本機登入時更新過，較新
        row = _encoded(StaffAccount, sid)
        row.update(is_active=False, session_epoch=1, updated_at=OLD.isoformat())
        Applier(tid, 'node').apply({'staff_account': [row]})
        acc = db.session.get(StaffAccount, sid)
        assert acc.is_active is False and acc.session_epoch == 1


def test_b172_stale_epoch_row_does_not_revert_password(app):
    from app.models.tenant import StaffAccount
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b172b') as tid:
        sid = _staff(tid, f'se1-b172b-{_uuid()[:8]}')
        stale = _encoded(StaffAccount, sid)
        acc = db.session.get(StaffAccount, sid)
        acc.set_password('new-password-456')
        acc.is_active, acc.session_epoch = False, 1
        db.session.commit()
        _set_updated_at(StaffAccount, sid, OLD)
        stale.update(updated_at=NEW.isoformat())   # 分店舊資料 (序號 0) 因登入而較新
        Applier(tid, 'hub', source_node_uuid='node-x').apply({'staff_account': [stale]})
        acc = db.session.get(StaffAccount, sid)
        assert acc.is_active is False and acc.check_password('new-password-456')


def test_b175_owner_backup_code_use_on_node_is_accepted_by_hub(app):
    from app.models.tenant import StaffAccount
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b175') as tid:
        codes = ['h1', 'h2', 'h3']
        sid = _staff(tid, f'se1-b175-{_uuid()[:8]}', role='owner', totp_backup_codes=json.dumps(codes))
        row = _encoded(StaffAccount, sid)
        row.update(totp_backup_codes=json.dumps(['h1', 'h3']), updated_at=NEW.isoformat())
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'staff_account': [row]})
        assert not result.conflicts
        assert json.loads(db.session.get(StaffAccount, sid).totp_backup_codes) == ['h1', 'h3']
        # 分店新增 (重新產生) 備用碼仍拒收
        row.update(totp_backup_codes=json.dumps(['h1', 'h3', 'zz']), updated_at=(NEW + timedelta(days=1)).isoformat())
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'staff_account': [row]})
        assert result.conflicts


def test_b175_used_backup_code_is_not_restored_by_newer_row(app):
    from app.models.tenant import StaffAccount
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b175b') as tid:
        sid = _staff(tid, f'se1-b175b-{_uuid()[:8]}', totp_backup_codes=json.dumps(['h1', 'h3']))
        _set_updated_at(StaffAccount, sid, OLD)
        row = _encoded(StaffAccount, sid)
        row.update(totp_backup_codes=json.dumps(['h1', 'h2', 'h3']), permissions='x', updated_at=NEW.isoformat())
        Applier(tid, 'node').apply({'staff_account': [row]})
        acc = db.session.get(StaffAccount, sid)
        assert json.loads(acc.totp_backup_codes) == ['h1', 'h3'] and acc.permissions == 'x'


# ---------------------------------------------------------------------------
# B-174：已封存的訂單不插回主資料庫
# ---------------------------------------------------------------------------
def test_b174_archived_order_is_not_reinserted(app):
    from app.models.order import Order
    from app.services import archive_service
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b174') as tid:
        oid = _add(Order(tenant_id=tid, total_price=200, created_at=datetime.now() - timedelta(days=300)))
        row = _encoded(Order, oid)
        archived_uuid = _uuid()
        row.update(uuid=archived_uuid, table_number='已刪除會員')
        path = archive_service.archive_path(tid)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS "order" (uuid VARCHAR(36) PRIMARY KEY)')
            conn.execute('INSERT INTO "order" (uuid) VALUES (?)', (archived_uuid,))
            conn.commit()
        try:
            result = Applier(tid, 'node').apply({'order': [row]})
            assert not result.new_orders
            assert Order.query.filter_by(uuid=archived_uuid).execution_options(skip_tenant_filter=True).count() == 0
            # 沒封存的舊訂單照常新增
            row.update(uuid=_uuid())
            result = Applier(tid, 'node').apply({'order': [row]})
            assert len(result.new_orders) == 1
        finally:
            os.remove(path)


# ---------------------------------------------------------------------------
# R2-064：推薦人合併後 referred_by_uuid 跟著改
# ---------------------------------------------------------------------------
def test_r2_064_referred_by_uuid_follows_alias(app):
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-r2064') as tid:
        rh = _add(User(tenant_id=tid, name='推薦人', phone='0926464064', photo_path=''))
        rh_uuid = db.session.get(User, rh).uuid
        ra_uuid = _uuid()
        ra = _encoded(User, rh)
        ra.update(uuid=ra_uuid)
        friend_id = _add(User(tenant_id=tid, name='好友', phone='0926464065', photo_path=''))
        friend = _encoded(User, friend_id)
        friend.update(uuid=_uuid(), phone='0926464066', name='好友二', referred_by_uuid=ra_uuid)
        db.session.get(User, friend_id).referred_by_uuid = ra_uuid
        db.session.flush()
        Applier(tid, 'hub', source_node_uuid='node-x').apply({'user': [ra, friend]})
        db.session.flush()
        db.session.expire_all()
        assert db.session.get(User, friend_id).referred_by_uuid == rh_uuid
        friend2 = User.query.filter_by(tenant_id=tid, phone='0926464066').execution_options(skip_tenant_filter=True).one()
        assert friend2.referred_by_uuid == rh_uuid


# ---------------------------------------------------------------------------
# 跨檔請求：B-051 / B-085 / B-118 / R2-109 / B-273 / B-386 / B-420 / B-149 / R2-182 / B-472 / B-348
# ---------------------------------------------------------------------------
def test_b051_node_closes_manual_cancel_refund_when_hub_refunded(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.models.payment import Refund
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b051') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, payment_status='refund_required', status=OrderStatus.CANCELLED))
        rid = _add(Refund(tenant_id=tid, order_id=oid, kind='cancel', amount=100, status='manual'))
        row = _encoded(Order, oid)
        row.update(payment_status='refunded', updated_at=NEW.isoformat())
        Applier(tid, 'node').apply({'order': [row]})
        refund = db.session.get(Refund, rid)
        assert db.session.get(Order, oid).payment_status == 'refunded'
        assert refund.status == 'done' and refund.completed_at is not None


def test_b085_voided_gift_card_is_not_reactivated_by_newer_row(app):
    from app.models.member import GiftCard
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b085') as tid:
        gid = _add(GiftCard(tenant_id=tid, code='GC0850850850850', initial_amount=500, status='void',
                            voided_at=OLD))
        _set_updated_at(GiftCard, gid, OLD)
        row = _encoded(GiftCard, gid)
        row.update(status='active', voided_at=None, note='改備註', updated_at=NEW.isoformat())
        Applier(tid, 'node').apply({'gift_card': [row]})
        card = db.session.get(GiftCard, gid)
        assert (card.status, card.voided_at, card.note) == ('void', OLD, '改備註')


def test_b118_overlong_string_is_truncated(app):
    from app.models.user import CustomerLog
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b118') as tid:
        row_uuid = _uuid()
        row = {'uuid': row_uuid, 'user_type': '會員', 'identifier': '名' * 120, 'login_at': NEW.isoformat(),
               'updated_at': NEW.isoformat()}
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'customer_log': [row]})
        assert not result.conflicts
        log = CustomerLog.query.filter_by(uuid=row_uuid).execution_options(skip_tenant_filter=True).one()
        assert len(log.identifier) == 100


def test_r2_109_out_of_range_integer_is_rejected_as_conflict(app):
    from app.models.order import Order
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-r2109') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-a'))
        row = _encoded(Order, oid)
        row.update(uuid=_uuid(), total_price=3_000_000_000)
        result = Applier(tid, 'hub', source_node_uuid='node-a').apply({'order': [row]})
        assert not result.new_orders
        assert result.conflicts and '整數超出範圍' in result.conflicts[0]['reason']


def test_b273_member_tombstone_deidentifies_local_orders(app):
    from app.models.order import Order
    from app.models.user import User
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b273') as tid:
        uid = _add(User(tenant_id=tid, name='王小明', phone='0927327327', photo_path=''))
        user_uuid = db.session.get(User, uid).uuid
        oid = _add(Order(tenant_id=tid, total_price=100, user_id=uid, table_number='王小明',
                         delivery_address='台北市', delivery_phone='0927327327', delivery_status='delivered'))
        tomb = {'uuid': _uuid(), 'table_name': 'user', 'row_uuid': user_uuid}
        Applier(tid, 'node').apply({'sync_tombstone': [tomb]})
        db.session.expire_all()
        order = db.session.get(Order, oid)
        assert (order.table_number, order.delivery_address, order.delivery_phone) == ('已刪除會員', '', '')
        assert db.session.get(User, uid) is None


def test_b386_hub_records_payment_under_uploading_node(app):
    from app.models.payment import PaymentRecord
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b386') as tid:
        pid = _add(PaymentRecord(tenant_id=tid, amount=80))
        row = _encoded(PaymentRecord, pid)
        row.update(uuid=_uuid(), node_uuid=None)
        Applier(tid, 'hub', source_node_uuid='node-p').apply({'payment_record': [row]})
        rec = PaymentRecord.query.filter_by(uuid=row['uuid']).execution_options(skip_tenant_filter=True).one()
        assert rec.node_uuid == 'node-p'


def test_b420_b149_r2182_hub_order_merge(app):
    from app.models.order import DeliveryPosition, Order
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b420') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-a', table_number='A1',
                         delivery_status='assigned', driver_name='甲', driver_token=_uuid()))
        order_uuid = db.session.get(Order, oid).uuid
        _add(DeliveryPosition(tenant_id=tid, order_uuid=order_uuid, lat=25.0, lng=121.5))
        _set_updated_at(Order, oid, OLD)
        row = _encoded(Order, oid)
        row.update(table_number='B2', driver_name='乙', driver_token=_uuid(), updated_at=NEW.isoformat())
        result = Applier(tid, 'hub', source_node_uuid='node-a').apply({'order': [row]})
        order = db.session.get(Order, oid)
        assert order.table_number == 'B2'
        assert order.driver_token == row['driver_token']
        assert DeliveryPosition.query.filter_by(order_uuid=order_uuid) \
            .execution_options(skip_tenant_filter=True).count() == 0
        assert oid in result.changed_order_ids
        # 已去識別化的訂單不被舊名稱蓋回
        order.table_number = '已刪除會員'
        db.session.flush()
        row.update(table_number='王小明', updated_at=(NEW + timedelta(days=1)).isoformat())
        Applier(tid, 'hub', source_node_uuid='node-a').apply({'order': [row]})
        assert db.session.get(Order, oid).table_number == '已刪除會員'


def test_b348_hub_notifies_synced_order_status(app, monkeypatch):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.services import push_service
    from app.sync.engine import Applier
    calls = []
    monkeypatch.setattr(push_service, 'notify_synced_order', lambda o, s, d: calls.append((o.id, s, d)))
    with _ctx(app, 'fix-se1-b348') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-a'))
        row = _encoded(Order, oid)
        row.update(status=OrderStatus.COMPLETED, updated_at=NEW.isoformat())
        Applier(tid, 'hub', source_node_uuid='node-a').apply({'order': [row]})
        assert calls == [(oid, OrderStatus.PENDING, '')]
        calls.clear()
        db.session.get(Order, oid).status = OrderStatus.PENDING
        db.session.flush()
        Applier(tid, 'node').apply({'order': [row]})   # 分店端不送
        assert not calls


def test_b472_feedback_reply_is_merged_by_replied_at(app):
    from app.models.order import Order, OrderFeedback
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b472') as tid:
        oid = _add(Order(tenant_id=tid, total_price=100, node_uuid='node-a'))
        fid = _add(OrderFeedback(tenant_id=tid, order_id=oid, rating=4, comment='好吃'))
        row = _encoded(OrderFeedback, fid)
        row.update(reply='謝謝', replied_at=NEW.isoformat(), is_public=True, rating=1)
        Applier(tid, 'hub', source_node_uuid='node-a').apply({'order_feedback': [row]})
        fb = db.session.get(OrderFeedback, fid)
        assert (fb.reply, fb.replied_at, fb.is_public, fb.rating) == ('謝謝', NEW, True, 4)
        row.update(reply='舊回覆', replied_at=OLD.isoformat(), is_public=False)
        Applier(tid, 'hub', source_node_uuid='node-a').apply({'order_feedback': [row]})
        assert db.session.get(OrderFeedback, fid).reply == '謝謝'


# ---------------------------------------------------------------------------
# B-499：線上端一列寫入失敗 (違反約束 / 欄位過長) 只放棄那一列，其餘照常套用
# ---------------------------------------------------------------------------
def test_b499_hub_skips_only_the_row_that_fails_to_write(app):
    import pytest

    from app.models.menu import MenuItem
    from app.sync.engine import Applier
    with _ctx(app, 'fix-se1-b499') as tid:
        own = _add(MenuItem(tenant_id=tid, name='本店餐點', category='主餐', price=50, image_path=''))
        bad = _encoded(MenuItem, own)
        bad_uuid = _uuid()
        bad.update(uuid=bad_uuid, name=None, image_path='1/menu_badrow00001.jpg')   # name 不可為空：寫入失敗
        good = _encoded(MenuItem, own)
        good_uuid = _uuid()
        good.update(uuid=good_uuid, name='正常餐點', image_path='1/menu_goodrow0001.jpg')
        result = Applier(tid, 'hub', source_node_uuid='node-x').apply({'menu_item': [bad, good]})
        assert [c['uuid'] for c in result.conflicts] == [bad_uuid]
        assert '無法寫入線上端資料庫' in result.conflicts[0]['reason']
        assert result.applied == 1
        assert result.menu_images == {'1/menu_goodrow0001.jpg'}
        q = MenuItem.query.execution_options(skip_tenant_filter=True)
        assert q.filter_by(uuid=good_uuid).count() == 1
        assert q.filter_by(uuid=bad_uuid).count() == 0
        db.session.flush()   # 交易仍可使用

    # 分店端行為不變：寫入失敗照常拋出 (交由同步流程整批回滾重試)
    with _ctx(app, 'fix-se1-b499-node') as tid:
        own = _add(MenuItem(tenant_id=tid, name='本店餐點', category='主餐', price=50, image_path=''))
        bad = _encoded(MenuItem, own)
        bad.update(uuid=_uuid(), name=None)
        with pytest.raises(sa.exc.IntegrityError):
            Applier(tid, 'node').apply({'menu_item': [bad]})
