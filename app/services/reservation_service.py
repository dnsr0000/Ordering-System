"""訂位與現場候位"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.reservation import Reservation, WaitlistEntry
from app.services import settings_service
from app.utils import validate_phone


def _parse_hm(value, default):
    try:
        return datetime.strptime(value or default, '%H:%M').time()
    except ValueError:
        return datetime.strptime(default, '%H:%M').time()


def slots_for(day, tenant_id=None):
    """某一天可訂的時段 [(datetime, 已訂人數, 剩餘)]"""
    cfg = settings_service.get('reservation', tenant_id)
    start = datetime.combine(day, _parse_hm(cfg.get('open_time'), '11:00'))
    end = datetime.combine(day, _parse_hm(cfg.get('close_time'), '20:30'))
    step = timedelta(minutes=max(10, cfg.get('slot_minutes') or 30))
    capacity = cfg.get('capacity_per_slot') or 20
    booked = dict(db.session.query(Reservation.reserved_at, func.sum(Reservation.party_size))
                  .filter(Reservation.reserved_at >= start, Reservation.reserved_at <= end,
                          Reservation.status.in_(('booked', 'seated')))
                  .group_by(Reservation.reserved_at).all())
    out, t = [], start
    now = datetime.now()
    while t <= end:
        used = int(booked.get(t) or 0)
        if t > now:
            out.append((t, used, max(0, capacity - used)))
        t += step
    return out


def create_reservation(tenant_id, name, phone, party_size, reserved_at, note='', source='staff', table_name=''):
    cfg = settings_service.get('reservation', tenant_id)
    name = str(name or '').strip()[:60]
    phone = str(phone or '').strip()
    party = int(party_size or 0)
    if not name:
        raise AppError('請輸入訂位姓名')
    if source == 'online' and not validate_phone(phone):
        raise AppError('請輸入正確的手機號碼 (09 開頭 10 碼)')
    if party <= 0 or party > (cfg.get('max_party_size') or 10):
        raise AppError(f"訂位人數需為 1 ~ {cfg.get('max_party_size') or 10} 位")
    if not isinstance(reserved_at, datetime):
        raise AppError('訂位時間格式不正確')
    if reserved_at < datetime.now() - timedelta(minutes=5):
        raise AppError('訂位時間已過')
    if source == 'online':
        if reserved_at.date() > date.today() + timedelta(days=cfg.get('days_ahead') or 30):
            raise AppError(f"只能預訂 {cfg.get('days_ahead') or 30} 天內的時段")
        slot = next((s for s in slots_for(reserved_at.date(), tenant_id) if s[0] == reserved_at), None)
        if slot is None:
            raise AppError('此時段不開放訂位')
        if slot[2] < party:
            raise AppError(f'此時段剩餘 {slot[2]} 位，請改選其他時段')
    deposit = 0
    if source == 'online':
        from app.services import deposit_service
        deposit_service.check_no_show_limit(tenant_id, phone)   # 多次未到：暫停線上訂位
        deposit = deposit_service.deposit_for(tenant_id, party)
        from app.services.db_locks import lock_tenant
        lock_tenant(tenant_id)   # 兩位顧客同時訂同一時段的最後座位：依序處理
    r = Reservation(tenant_id=tenant_id, name=name, phone=phone[:20], party_size=party, reserved_at=reserved_at,
                    note=str(note or '')[:200], source=source, table_name=str(table_name or '')[:50])
    if deposit:   # 線上訂位須預付訂金：30 分鐘內付款，否則自動取消 (期間保留座位)
        from app.services import payment_ecpay
        r.deposit_amount, r.deposit_status, r.deposit_trade_no = deposit, 'pending', payment_ecpay._new_trade_no()
    db.session.add(r)
    if source == 'online':
        db.session.flush()
        capacity = cfg.get('capacity_per_slot') or 20
        used = db.session.query(func.coalesce(func.sum(Reservation.party_size), 0)).filter(
            Reservation.tenant_id == tenant_id, Reservation.reserved_at == reserved_at,
            Reservation.status.in_(('booked', 'seated'))).scalar() or 0
        if used > capacity:   # 新增後再數一次：剛好被別人訂走
            db.session.rollback()
            raise AppError(f'此時段剩餘 {max(0, capacity - (used - party))} 位，請改選其他時段', 409)
    return r


def set_reservation_status(reservation, status, table_name=None, by_customer=False):
    """改變訂位狀態並處理訂金 (退還 / 不退 / 折抵)；回傳訂金說明 (沒有訂金時為空字串)"""
    from app.services import deposit_service
    if status not in ('booked', 'seated', 'cancelled', 'no_show'):
        raise AppError('狀態不正確')
    if reservation.deposit_status == 'pending' and status == 'seated':
        raise AppError('此訂位的訂金尚未付款')
    previous = reservation.status
    # 條件式更新：顧客取消與店員入座同時發生時只有一個成功
    changed = db.session.execute(update(Reservation).where(Reservation.id == reservation.id, Reservation.status == previous)
                                 .values(status=status, status_changed_at=datetime.now())
                                 .execution_options(synchronize_session=False)).rowcount
    db.session.refresh(reservation)
    if not changed:
        raise AppError('此訂位剛被其他人更新，請重新整理', 409)
    if table_name is not None:
        reservation.table_name = str(table_name)[:50]
    if status == previous:
        return ''
    return deposit_service.on_status(reservation, status, by_customer=by_customer)


def join_waitlist(tenant_id, name, phone, party_size):
    """現場取號 (每天從 1 號開始)；回傳 WaitlistEntry"""
    party = int(party_size or 0)
    if party <= 0 or party > 50:
        raise AppError('請輸入候位人數 (1 ~ 50)')
    today = date.today()
    for _ in range(3):   # 兩人同時取號撞到同一號碼 (唯一索引) 時重試
        last = db.session.query(func.max(WaitlistEntry.number)).filter(WaitlistEntry.business_date == today).scalar() or 0
        entry = WaitlistEntry(tenant_id=tenant_id, number=last + 1, business_date=today,
                              name=str(name or '').strip()[:60], phone=str(phone or '').strip()[:20], party_size=party)
        try:
            with db.session.begin_nested():
                db.session.add(entry)
            return entry
        except IntegrityError:
            continue
    raise AppError('取號失敗，請再試一次', 409)


def waiting_ahead(entry):
    return WaitlistEntry.query.filter(WaitlistEntry.business_date == entry.business_date,
                                      WaitlistEntry.status == 'waiting', WaitlistEntry.number < entry.number).count()


def set_waitlist_status(entry, status, table_name=''):
    if status not in ('waiting', 'called', 'seated', 'cancelled'):
        raise AppError('狀態不正確')
    now = datetime.now()
    values = {'status': status}
    if status == 'called':
        values['called_at'] = now
    elif status == 'seated':
        values['seated_at'] = now
        values['table_name'] = str(table_name or '')[:50]
    db.session.execute(update(WaitlistEntry).where(WaitlistEntry.id == entry.id).values(**values)
                       .execution_options(synchronize_session=False))
    db.session.refresh(entry)
    return entry


def notify_called(entry, tenant_id):
    """叫號時以簡訊通知 (有設定簡訊服務且留了手機)"""
    from app.services import sms_service
    if entry.phone and sms_service.is_enabled(tenant_id):
        try:
            sms_service.send_sms(tenant_id, entry.phone, f'候位號碼 {entry.label} 已輪到您，請於 10 分鐘內至櫃檯入座。')
        except AppError:
            pass


def day_bounds(day):
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)
