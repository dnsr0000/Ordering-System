"""(線上端) 分店登記與權杖管理"""
from app.extensions import db
from app.models.sync import SyncNode


def create_node(tenant_id, name, accepts_online_orders=True):
    """新增分店，回傳 (分店, 權杖)。權杖只在此時顯示一次。"""
    node = SyncNode(tenant_id=tenant_id, name=(name or '').strip()[:100] or '分店',
                    accepts_online_orders=bool(accepts_online_orders), token_hash='')
    token = node.issue_token()
    db.session.add(node)
    db.session.commit()
    return node, token


def rotate_token(node):
    """重新產生權杖 (舊權杖立即失效)"""
    token = node.issue_token()
    db.session.commit()
    return token
