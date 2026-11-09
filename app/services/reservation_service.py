"""訂位與現場候位"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.reservation import Reservation, WaitlistEntry
from app.services import settings_service
from app import utils
from app.utils import parse_hm, validate_phone

STAFF_MAX_PARTY = 500   # 店員代訂 (電話訂位、包場) 的人數上限 (線上訂位依設定的「單筆訂位人數上限」)
DUPLICATE_WINDOW = timedelta(minutes=2)   # 這段時間內送出內容完全相同的訂位視為重複送出


def _parse_hm(value, default):
    """'HH:MM' → time；格式錯誤 (或 24:00，time 不接受) 用 default"""
    hm = parse_hm(value, default)
    if hm is None or hm[0] == 24:
        hm = parse_hm(default)
    return time(*hm)


def _slot_step(cfg):
    return timedelta(minutes=max(10, cfg.get('slot_minutes') or 30))


def slots_for(day, tenant_id=None):
    """
    某一天可訂的時段 [(datetime, 已訂人數, 剩餘)]。
    已訂人數以時段區間 [t, t + 間隔) 加總：店員以任意分鐘登錄的電話訂位 (例如 12:15)、或改了起始時間 / 間隔後
    對不上新格線的舊訂位，都計入它所在的時段 (不會因為時間點不完全相同而不扣名額)。
    """
    cfg = settings_service.get('reservation', tenant_id)
    start = datetime.combine(day, _parse_hm(cfg.get('open_time'), '11:00'))
    end = datetime.combine(day, _parse_hm(cfg.get('close_time'), '20:30'))
    if end < start:   # 跨午夜營業 (例如 18:00~00:30)：結束時間是隔天
        end += timedelta(days=1)
    step = _slot_step(cfg)
    capacity = cfg.get('capacity_per_slot') or 20
    booked = {}
    for at, size in (db.session.query(Reservation.reserved_at, Reservation.party_size)
                     .filter(Reservation.reserved_at >= start, Reservation.reserved_at < end + step,
                             Reservation.status.in_(('booked', 'seated'))).all()):
        slot = start + step * ((at - start) // step)
        booked[slot] = booked.get(slot, 0) + (size or 0)
    out, t = [], start
    now = datetime.now()
    while t <= end:
        used = int(booked.get(t) or 0)
        if t > now:
            out.append((t, used, max(0, capacity - used)))
        t += step
    return out


def _duplicate(tenant_id, name, phone, party, reserved_at, source):
    """
    同店、同時段、同來源、同姓名電話人數、仍為預約中，且剛在 DUPLICATE_WINDOW 內建立的訂位 (重複送出的同一筆)。
    只認剛建立的：避免知道別人姓名電話的人事後送出相同內容，被導向對方的訂位頁 (可取消)。
    """
    return Reservation.query.filter(Reservation.tenant_id == tenant_id, Reservation.reserved_at == reserved_at,
                                    Reservation.name == name, Reservation.phone == phone[:20],
                                    Reservation.party_size == party, Reservation.source == source,
                                    Reservation.status == 'booked',
                                    Reservation.created_at >= datetime.now() - DUPLICATE_WINDOW) \
        .order_by(Reservation.id).first()


def create_reservation(tenant_id, name, phone, party_size, reserved_at, note='', source='staff', table_name=''):
    cfg = settings_service.get('reservation', tenant_id)
    name = str(name or '').strip()[:60]
    phone = str(phone or '').strip()
    party = int(party_size or 0)
    if not name:
        raise AppError('請輸入訂位姓名')
    if source == 'online' and not validate_phone(phone):
        raise AppError('請輸入正確的手機號碼 (09 開頭 10 碼)')
    # 「單筆訂位人數上限」只限線上訂位；店員代訂 (電話訂位、包場) 只檢查合理範圍
    max_party = (cfg.get('max_party_size') or 10) if source == 'online' else STAFF_MAX_PARTY
    if party <= 0 or party > max_party:
        raise AppError(f'訂位人數需為 1 ~ {max_party} 位')
    if not isinstance(reserved_at, datetime):
        raise AppError('訂位時間格式不正確')
    if reserved_at.tzinfo is not None:   # 帶時區 (+08:00 / Z) 的輸入：換成本機時間 (資料庫與比較都用本機時間)
        reserved_at = reserved_at.astimezone().replace(tzinfo=None)
    if reserved_at < datetime.now() - timedelta(minutes=5):
        raise AppError('訂位時間已過')
    # 重複送出 (連按兩次送出、按上一頁再送)：已有一筆完全相同的預約中訂位時直接回傳那一筆 (先於容量檢查：
    # 第一筆剛好訂滿時，第二次送出不該變成「剩餘 0 位」)
    dup = _duplicate(tenant_id, name, phone, party, reserved_at, source)
    if dup is not None:
        return dup
    if source == 'online':
        # 跨午夜營業時，凌晨的時段屬於前一天的可訂時段 (slots_for 的結束時間在隔天)
        slot, slot_day = None, reserved_at.date()
        for d in (reserved_at.date(), reserved_at.date() - timedelta(days=1)):
            slot = next((s for s in slots_for(d, tenant_id) if s[0] == reserved_at), None)
            if slot is not None:
                slot_day = d
                break
        if slot_day > date.today() + timedelta(days=cfg.get('days_ahead') or 30):
            raise AppError(f"只能預訂 {cfg.get('days_ahead') or 30} 天內的時段")
        if slot is None:
            raise AppError('此時段不開放訂位')
        if slot[2] < party:
            raise AppError(f'此時段剩餘 {slot[2]} 位，請改選其他時段')
    deposit = 0
    if source == 'online':
        from app.services import deposit_service
        deposit_service.check_no_show_limit(tenant_id, phone)   # 多次未到：暫停線上訂位
        deposit = deposit_service.deposit_for(tenant_id, party)
        if deposit:   # 收不了訂金 (綠界未設定正式金鑰) 時不成立待付款訂位：付款頁打不開，只會佔住名額 30 分鐘
            from app.services import payment_ecpay
            try:
                payment_ecpay.require_keys_allowed()
            except payment_ecpay.TestKeysNotAllowed:
                raise AppError('本店目前無法線上收取訂金，請來電預約')
        from app.services.db_locks import lock_tenant
        lock_tenant(tenant_id)   # 兩位顧客同時訂同一時段的最後座位：依序處理
        dup = _duplicate(tenant_id, name, phone, party, reserved_at, source)   # 同時送出兩次：取得鎖後再確認一次
        if dup is not None:
            return dup
    r = Reservation(tenant_id=tenant_id, name=name, phone=phone[:20], party_size=party, reserved_at=reserved_at,
                    note=str(note or '')[:200], source=source, table_name=str(table_name or '')[:50])
    if deposit:   # 線上訂位須預付訂金：30 分鐘內付款，否則自動取消 (期間保留座位)
        from app.services import payment_ecpay
        r.deposit_amount, r.deposit_status, r.deposit_trade_no = deposit, 'pending', payment_ecpay.new_trade_no()
    db.session.add(r)
    if source == 'online':
        db.session.flush()
        capacity = cfg.get('capacity_per_slot') or 20
        used = db.session.query(func.coalesce(func.sum(Reservation.party_size), 0)).filter(
            Reservation.tenant_id == tenant_id, Reservation.reserved_at >= reserved_at,
            Reservation.reserved_at < reserved_at + _slot_step(cfg),   # 與 slots_for 相同的時段區間
            Reservation.status.in_(('booked', 'seated'))).scalar() or 0
        if used > capacity:   # 新增後再數一次：剛好被別人訂走
            db.session.rollback()
            raise AppError(f'此時段剩餘 {max(0, capacity - (used - party))} 位，請改選其他時段', 409)
    return r


def set_reservation_status(reservation, status, table_name=None, by_customer=False, expected=None, by_store=False):
    """
    改變訂位狀態並處理訂金 (退還 / 不退 / 折抵)；回傳訂金說明 (沒有訂金時為空字串)。
    by_store=True：店家因素取消，不論距訂位時間多久一律全額退訂金。
    expected：操作畫面上看到的原狀態 (看板 / 顧客頁只對「已訂位」操作)；與目前狀態不同 (過期畫面) 時拒絕 (409)，
    避免把已取消 / 已入座的訂位改成未到等。
    """
    from app.services import deposit_service
    if status not in ('booked', 'seated', 'cancelled', 'no_show'):
        raise AppError('狀態不正確')
    if expected is not None and reservation.status != expected:
        raise AppError('此訂位已被其他人更新，請重新整理', 409)
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
    return deposit_service.on_status(reservation, status, by_customer=by_customer, by_store=by_store)


def join_waitlist(tenant_id, name, phone, party_size):
    """現場取號 (每個營業日從 1 號開始，依 BUSINESS_DAY_START_HOUR 換日)；回傳 WaitlistEntry"""
    from app.services.order_service import todays_range
    party = int(party_size or 0)
    if party <= 0 or party > 50:
        raise AppError('請輸入候位人數 (1 ~ 50)')
    today = todays_range()[0].date()
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
    """同店同一天號碼較小、仍在候位的組數 (顧客頁在 tenant_bypass 內呼叫：明確限定店家)"""
    return WaitlistEntry.query.filter(WaitlistEntry.tenant_id == entry.tenant_id,
                                      WaitlistEntry.business_date == entry.business_date,
                                      WaitlistEntry.status == 'waiting', WaitlistEntry.number < entry.number) \
        .execution_options(skip_tenant_filter=True).count()


def set_waitlist_status(entry, status, table_name=''):
    """只有候位中 / 已叫號的號碼可以變更 (過期畫面對已入座 / 已過號的號碼操作時 409，不重新叫號)"""
    if status not in ('waiting', 'called', 'seated', 'cancelled'):
        raise AppError('狀態不正確')
    now = datetime.now()
    values = {'status': status}
    if status == 'called':
        values['called_at'] = now
    elif status == 'seated':
        values['seated_at'] = now
        values['table_name'] = str(table_name or '')[:50]
    changed = db.session.execute(update(WaitlistEntry)
                                 .where(WaitlistEntry.id == entry.id, WaitlistEntry.status.in_(('waiting', 'called')))
                                 .values(**values).execution_options(synchronize_session=False)).rowcount
    db.session.refresh(entry)
    if not changed:
        raise AppError('此候位號碼已被其他人更新，請重新整理', 409)
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
    """某一天的半開 datetime 區間 (同 app.utils.day_bounds)"""
    return utils.day_bounds(day)
