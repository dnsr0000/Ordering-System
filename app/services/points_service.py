"""
會員點數。所有點數變動都寫一筆 point_ledger (只新增)，user.points 為合計快取：
- 本機交易：條件式 UPDATE user.points + 同交易寫入一筆異動 (change_points)
- 直接修改 user.points (後台編輯、Excel 匯入、新會員的註冊禮) 由 flush 前掛勾自動補寫異動
- 同步收到其他分店的異動後以 recompute_points 重算合計 (各分店離線時的消費不會互相覆蓋)
"""
from sqlalchemy import func, inspect, select, update

from app.extensions import db
from app.models.sync import PointLedger
from app.models.user import User
from app.sync import tracking


def change_points(tenant_id, user_id, delta, reason, order_id=None, clamp=False):
    """
    原子加減點數並記帳。扣點時餘額需足夠，否則回傳 None (未變動)；
    clamp=True 時改為「最多扣到 0」(取消訂單追回贈點、後台調整)。成功回傳實際變動量 (可能為 0)。
    """
    delta = int(delta or 0)
    if delta == 0:
        return 0
    if clamp and delta < 0:
        current = db.session.execute(select(User.points).where(User.id == user_id)).scalar() or 0
        delta = max(delta, -max(0, current))
        if delta == 0:
            return 0
    stmt = update(User).where(User.id == user_id, User.tenant_id == tenant_id)
    if delta < 0:
        stmt = stmt.where(User.points >= -delta)
    res = db.session.execute(stmt.values(points=User.points + delta).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        return None
    db.session.add(PointLedger(tenant_id=tenant_id, user_id=user_id, delta=delta, reason=reason[:30], order_id=order_id))
    return delta


def recompute_points(user_ids):
    """以異動帳重算會員點數合計"""
    ids = sorted({i for i in user_ids if i})
    if not ids:
        return
    total = select(func.coalesce(func.sum(PointLedger.delta), 0)).where(PointLedger.user_id == User.id) \
        .scalar_subquery()
    db.session.execute(update(User).where(User.id.in_(ids)).values(points=total)
                       .execution_options(synchronize_session=False))


def _ledger_for_direct_changes(session):
    """直接設定 user.points 的地方 (新會員、後台、匯入) 自動補寫異動，維持 合計 = 異動總和"""
    if tracking.is_suppressed(session):
        return
    for obj in list(session.new):
        if isinstance(obj, User) and (obj.points or 0) != 0 and not obj.__dict__.get('_ledger_written'):
            obj._ledger_written = True
            session.add(PointLedger(tenant_id=obj.tenant_id or _scope_tenant(), user=obj, delta=obj.points,
                                    reason='initial'))
    for obj in list(session.dirty):
        if not isinstance(obj, User) or obj.id is None:
            continue
        hist = inspect(obj).attrs.points.history
        if not hist.added:
            continue
        old = hist.deleted[0] if hist.deleted else None
        if old is None:  # 舊值未載入：以資料庫目前的值為準
            old = db.session.execute(select(User.points).where(User.id == obj.id)).scalar() or 0
        delta = (hist.added[0] or 0) - (old or 0)
        if delta:
            session.add(PointLedger(tenant_id=obj.tenant_id, user_id=obj.id, delta=delta, reason='adjust'))


def _scope_tenant():
    from app.tenant_context import _scope_tenant_id
    tid = _scope_tenant_id()
    return tid if tid and tid > 0 else None


tracking.PRE_FLUSH_HOOKS.append(_ledger_for_direct_changes)
