"""
在前面所有測試 (結帳、取消、兌換、併發…) 跑完之後檢查同步相關的不變量：
- 沒有殘留的交易標記 (sync_rev < 0 代表提交時沒有換成正式序號，這些資料永遠不會被同步)
- 每位會員的點數 = 點數異動帳的合計
"""
import sqlalchemy as sa

from app.extensions import db
from app.sync.registry import SPECS


def test_no_uncommitted_sync_markers_left(app):
    with app.app_context():
        for spec in SPECS:
            table = spec.model.__table__
            bad = db.session.execute(sa.select(sa.func.count()).select_from(table).where(table.c.sync_rev < 0)).scalar()
            assert bad == 0, f'{spec.name} 有 {bad} 筆資料的 sync_rev 仍是交易標記'


def test_points_equal_ledger_sum(app):
    from app.models.sync import PointLedger
    from app.models.user import User
    with app.app_context():
        total = sa.select(sa.func.coalesce(sa.func.sum(PointLedger.delta), 0)).where(
            PointLedger.user_id == User.id).scalar_subquery()
        rows = db.session.execute(sa.select(User.id, User.points, total).where(User.points != total)).all()
        assert not rows, f'點數合計與異動帳不一致 (user_id, points, ledger)：{rows[:10]}'


def test_pass_remaining_equals_ledger_sum(app):
    """寄杯剩餘次數 = 寄杯異動帳合計 (同點數)"""
    from app.models.member import MemberPass, PassLedger
    with app.app_context():
        total = sa.select(sa.func.coalesce(sa.func.sum(PassLedger.delta), 0)).where(
            PassLedger.member_pass_id == MemberPass.id).scalar_subquery()
        bad = db.session.execute(sa.select(MemberPass.id, MemberPass.remaining, total)
                                 .where(MemberPass.remaining != total)).all()
        assert not bad, f'寄杯剩餘次數與異動帳不一致：{bad[:5]}'
