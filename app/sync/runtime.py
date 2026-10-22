"""目前這個伺服器在同步架構中的角色，以及與角色相關的小工具"""
from flask import current_app

from app.extensions import db
from app.models.sync import SyncState

STANDALONE, HUB, NODE = 'standalone', 'hub', 'node'


def role():
    return current_app.config.get('SYNC_ROLE', STANDALONE)


def is_hub():
    return role() == HUB


def is_node():
    return role() == NODE


def get_state(key, default=''):
    row = db.session.get(SyncState, key)
    return row.value if row is not None else default


def set_state(key, value):
    row = db.session.get(SyncState, key)
    if row is None:
        db.session.add(SyncState(key=key, value=str(value)))
    else:
        row.value = str(value)


def local_node_uuid():
    """分店端：本分店在線上端登記的 uuid (尚未完成初次連線時為 None)；其他角色為 None"""
    if not is_node():
        return None
    return get_state('node_uuid') or None


def owns_stock(order):
    """這張訂單的庫存是否由本資料庫管理 (只有實際出餐的分店會扣 / 回補實體庫存)"""
    if is_hub():
        return False
    if is_node():
        return order.node_uuid in (None, local_node_uuid())
    return True
