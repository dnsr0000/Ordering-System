"""
「先檢查、再新增」的併發保護。

兩台機器 / 兩個請求同時「檢查還有沒有名額 → 新增」時，兩邊都會看到還有名額 (例如兩台 POS 同時開班、
兩位顧客同時訂最後的座位)。做法：先鎖、新增、再數一次，超過就整筆取消。
- PostgreSQL：SELECT ... FOR UPDATE 鎖住店家資料列，同一店家的這類操作依序執行 (READ COMMITTED 下，
  取得鎖之後的查詢看得到前一筆已提交的資料)。
- SQLite：FOR UPDATE 不適用；新增 (flush) 時就取得整個資料庫的寫入鎖，之後的查詢看得到最新資料。
"""
from sqlalchemy import select

from app.extensions import db


def lock_tenant(tenant_id):
    """鎖住店家資料列到交易結束 (PostgreSQL)；SQLite 由接下來的寫入取得資料庫寫入鎖"""
    from app.models.tenant import Tenant
    db.session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
