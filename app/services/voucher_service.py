"""
紅利兌換憑證 (reward_redemption) 的原子狀態轉換。

憑證有三種互斥的結局：被結帳核銷 (is_used)、被退點 (is_refunded)、被綁定到訂單 (order_id)。
結帳、手動退點、登入/登出時的自動退還可能同時發生，因此每一次轉換都必須是
「單一條件式 UPDATE」：只有在憑證仍是「未使用、未退點、未綁定訂單」時才會成功 (rowcount == 1)，
先搶到的一方成功、其他方一定失敗，絕不會出現「餐點拿了、點數也退了」的雙重使用。
"""
from app.extensions import db

_OPEN_FILTER = "is_used = 0 AND is_refunded = 0 AND order_id IS NULL"


def list_open_vouchers(tenant_id, user_id, menu_item_id=None, limit=None):
    """列出會員目前可用的憑證 [(id, points_cost), ...]，先建立者優先 (FIFO)。僅為候選清單，實際歸屬以原子 UPDATE 為準。"""
    sql = (
        "SELECT id, points_cost FROM reward_redemption "
        f"WHERE tenant_id = :t AND user_id = :u AND {_OPEN_FILTER}"
    )
    params = {"t": tenant_id, "u": user_id}
    if menu_item_id is not None:
        sql += " AND menu_item_id = :m"
        params["m"] = menu_item_id
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT :n"
        params["n"] = int(limit)
    return [(row[0], int(row[1] or 0)) for row in db.session.execute(db.text(sql), params).fetchall()]


def try_claim_voucher(tenant_id, user_id, voucher_id):
    """結帳核銷：原子地把憑證標記為已使用。回傳 True 代表這張憑證確實由本次呼叫取得。"""
    res = db.session.execute(
        db.text(
            "UPDATE reward_redemption SET is_used = 1 "
            f"WHERE id = :id AND tenant_id = :t AND user_id = :u AND {_OPEN_FILTER}"
        ),
        {"id": voucher_id, "t": tenant_id, "u": user_id},
    )
    return res.rowcount == 1


def try_refund_voucher(tenant_id, user_id, voucher_id):
    """退點：原子地把憑證標記為已退點。回傳 True 代表這張憑證確實由本次呼叫退點 (呼叫端才可加回點數)。"""
    res = db.session.execute(
        db.text(
            "UPDATE reward_redemption SET is_refunded = 1 "
            f"WHERE id = :id AND tenant_id = :t AND user_id = :u AND {_OPEN_FILTER}"
        ),
        {"id": voucher_id, "t": tenant_id, "u": user_id},
    )
    return res.rowcount == 1


def claim_vouchers_for_item(tenant_id, user_id, menu_item_id, quantity):
    """為指定餐點最多搶占 quantity 張憑證，回傳實際搶到的憑證 ID 清單 (可能少於 quantity)。"""
    claimed = []
    for voucher_id, _cost in list_open_vouchers(tenant_id, user_id, menu_item_id, limit=quantity):
        if try_claim_voucher(tenant_id, user_id, voucher_id):
            claimed.append(voucher_id)
    return claimed


def bind_vouchers_to_order(tenant_id, voucher_ids, order_id):
    """把已搶占的憑證綁定到訂單"""
    for voucher_id in voucher_ids:
        db.session.execute(
            db.text("UPDATE reward_redemption SET order_id = :oid WHERE id = :id AND tenant_id = :t"),
            {"oid": order_id, "id": voucher_id, "t": tenant_id},
        )
