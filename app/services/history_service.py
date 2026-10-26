"""
原料成本 / 餐點售價的變動歷史 (第四輪)：在 ORM flush 前比對欄位的新舊值，任何途徑的修改都會留下紀錄
(後台編輯、進貨加權平均、調撥收貨、Excel 匯入、同步收到的總部改價)。以 update() 直接改的不經過這裡
(目前原料單位成本與售價都是以 ORM 物件修改)。
"""
from flask import has_request_context
from flask_login import current_user
from sqlalchemy import inspect, select

from app.models.expense import CostHistory
from app.models.inventory import Ingredient
from app.models.menu import MenuItem
from app.sync import tracking

TRACKED = {
    MenuItem: ('menu', ('price', 'discount_price', 'takeout_price', 'online_price', 'delivery_price', 'unit_cost')),
    Ingredient: ('ingredient', ('unit_cost',)),
}
FIELD_LABELS = {'price': '售價', 'discount_price': '特價', 'takeout_price': '外帶價', 'online_price': '線上價',
                'delivery_price': '外送價', 'unit_cost': '單位成本'}


def _staff():
    try:
        if has_request_context() and getattr(current_user, 'is_authenticated', False):
            return current_user.username
    except Exception:  # noqa: BLE001  (沒有登入管理員的情境)
        pass
    return 'system'


def _record_changes(session):
    for obj in list(session.dirty):
        spec = TRACKED.get(type(obj))
        if spec is None or obj.id is None:
            continue
        kind, fields = spec
        state = inspect(obj)
        for name in fields:
            hist = state.attrs[name].history
            if not hist.added:
                continue
            new = hist.added[0]
            old = hist.deleted[0] if hist.deleted else None
            if old is None:   # 舊值未載入：以資料庫目前的值為準
                old = session.execute(select(getattr(type(obj), name)).where(type(obj).id == obj.id)
                                      .execution_options(skip_tenant_filter=True)).scalar()
            old, new = float(old or 0), float(new or 0)
            if abs(new - old) < 0.0001:
                continue
            session.add(CostHistory(tenant_id=obj.tenant_id, kind=kind, ref_id=obj.id, name=obj.name or '', field=name,
                                    old_value=round(old, 4), new_value=round(new, 4), changed_by=_staff()))


tracking.PRE_FLUSH_HOOKS.append(_record_changes)
