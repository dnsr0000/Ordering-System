"""修正小組 core-infra-1 的回歸測試：設定、工具函式、啟動預設資料、即時通知、CLI"""
import queue
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from flask import Flask, session
from itsdangerous import TimestampSigner
from werkzeug.security import generate_password_hash

import app.config as config_module
from app.errors import AppError
from app.extensions import db
from app.models.tenant import StaffAccount
from tests.conftest import ADMIN_USERNAME
from tests.support import get_or_create_tenant


# ---------------------------------------------------------------------------
# B-204 SENTRY_TRACES_SAMPLE_RATE 格式錯誤不可讓設定模組 import 失敗
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('raw, expected', [('10%', 0.0), ('0,1', 0.0), ('nan', 0.0), ('0.25', 0.25), ('5', 1.0),
                                           ('-1', 0.0), ('', 0.0)])
def test_env_float_tolerates_bad_values(monkeypatch, raw, expected):
    monkeypatch.setenv('SENTRY_TRACES_SAMPLE_RATE', raw)
    assert config_module._env_float('SENTRY_TRACES_SAMPLE_RATE', 0.0, 0.0, 1.0) == expected


# ---------------------------------------------------------------------------
# B-198 Redis 斷線時頻率限制不可讓路由 500
# ---------------------------------------------------------------------------
def test_rate_limit_survives_storage_errors():
    assert config_module.Config.RATELIMIT_SWALLOW_ERRORS is True
    assert config_module.Config.RATELIMIT_IN_MEMORY_FALLBACK_ENABLED == bool(config_module.Config.REDIS_URL)


# ---------------------------------------------------------------------------
# B-195 顧客 session 不受員工 30 分鐘期限限制
# ---------------------------------------------------------------------------
def _session_app():
    from app.utils import KioskSessionInterface
    app = Flask(__name__)
    app.config.update(SECRET_KEY='core-infra-1', PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
                      CUSTOMER_SESSION_LIFETIME=timedelta(hours=24))
    app.session_interface = KioskSessionInterface()

    @app.route('/read')
    def read():
        return dict(session)

    return app


def _cookie(app, data, age_seconds, monkeypatch):
    """以 age_seconds 秒前的時間簽出 session cookie"""
    real = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time() - age_seconds))
    try:
        return app.session_interface.get_signing_serializer(app).dumps(data)
    finally:
        monkeypatch.setattr(TimestampSigner, 'get_timestamp', real)


@pytest.mark.parametrize('data, age, kept', [
    ({'csrf_token': 'x', 'dining_table': {'id': 1}}, 3600, True),        # 顧客：閒置 1 小時仍有效
    ({'csrf_token': 'x'}, 25 * 3600, False),                             # 顧客：超過 24 小時失效
    ({'_permanent': True, '_user_id': '1'}, 3600, False),                # 員工：閒置 30 分鐘以上失效
    ({'_user_id': '1'}, 3600, False),                                    # 已登入員工但未標 permanent：同樣是員工期限
    ({'_permanent': True, '_user_id': '1'}, 600, True),
])
def test_customer_and_staff_session_lifetimes(monkeypatch, data, age, kept):
    app = _session_app()
    cookie = _cookie(app, data, age, monkeypatch)
    client = app.test_client()
    client.set_cookie('session', cookie)
    got = client.get('/read').get_json()
    assert (got == data) is kept, got


# ---------------------------------------------------------------------------
# B-209 / B-210 / B-211 / B-212 工具函式
# ---------------------------------------------------------------------------
def test_safe_id_and_bounded_int_converter(app):
    from app.models.menu import MenuItem
    from app.utils import MAX_DB_ID, find_own, get_own, safe_id
    assert safe_id('99999999999999999999') == 0 and safe_id(-3) == 0 and safe_id('abc') == 0
    assert safe_id(str(MAX_DB_ID)) > 0 and safe_id('42') == 42
    from tests.support import default_tenant_id, tenant_request
    with tenant_request(app, default_tenant_id(app)):
        assert find_own(MenuItem, '99999999999999999999') is None
        with pytest.raises(AppError) as exc:
            get_own(MenuItem, '99999999999999999999')
        assert exc.value.status == 404

    from app.utils import BoundedIntConverter
    probe = Flask(__name__)
    probe.url_map.converters['int'] = BoundedIntConverter
    probe.add_url_rule('/x/<int:oid>', 'x', lambda oid: str(oid))
    c = probe.test_client()
    assert c.get('/x/12').get_data(as_text=True) == '12'
    assert c.get('/x/99999999999999999999').status_code == 404


def test_parse_birthday_huge_year_is_400():
    from app.utils import parse_birthday
    with pytest.raises(AppError) as exc:
        parse_birthday({'birthday_year': '9' * 25, 'birthday_month': '1', 'birthday_day': '1'})
    assert exc.value.status == 400


def test_parse_date_range_and_day_bounds_extreme_dates(app):
    from app.utils import day_bounds, parse_date_range
    today = date.today()
    with app.test_request_context('/?end=9999-12-31'):
        start, end = parse_date_range(default_days=7)
        assert end == today
    with app.test_request_context('/?start=9999-12-30'):
        start, end = parse_date_range(week=True)
        assert end - start == timedelta(days=6) and start <= today
    with app.test_request_context('/?end=9999-12-31'):
        with pytest.raises(AppError):
            parse_date_range(strict=True)
    assert day_bounds(date.max) == (datetime.combine(date.max, datetime.min.time()), datetime.max)
    assert day_bounds(date(2026, 1, 1))[1] == datetime(2026, 1, 2)


def test_public_display_name_masks_phone_numbers():
    from app.utils import public_display_name

    def order(table, source='POS'):
        return SimpleNamespace(user_id=None, user=None, table_number=table, order_source=source)

    assert public_display_name(order('0912345678')) == '○○○○○○○678'
    assert public_display_name(order('A12')) == 'A12'
    assert public_display_name(order('櫃檯POS')) == '櫃檯POS'
    assert '12345678' not in public_display_name(order('POS 0912345678'))


# ---------------------------------------------------------------------------
# B-196 / B-199 / B-203 / B-205 啟動預設資料
# ---------------------------------------------------------------------------
def test_seeded_defaults_are_older_than_any_edit(app):
    from app.models.menu import ModifierOption
    from app.models.user import RewardSetting
    from app.services.bootstrap import SEED_UPDATED_AT, seed_tenant_defaults
    tid = get_or_create_tenant(app, 'core-infra-1-seed')
    with app.app_context():
        seed_tenant_defaults(tid)
        db.session.commit()
        mods = ModifierOption.query.filter_by(tenant_id=tid).all()
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        assert mods and all(m.updated_at == SEED_UPDATED_AT for m in mods)
        assert rs.updated_at == SEED_UPDATED_AT
        rs.points_per_dollar = 5
        db.session.commit()
        assert rs.updated_at > SEED_UPDATED_AT, '修改後的值在同步時要比預設值新'
    tid2 = get_or_create_tenant(app, 'core-infra-1-node')
    with app.app_context():
        seed_tenant_defaults(tid2, modifiers=False)
        db.session.commit()
        assert ModifierOption.query.filter_by(tenant_id=tid2).count() == 0
        assert RewardSetting.query.filter_by(tenant_id=tid2).count() == 1


@pytest.fixture
def owner_cfg(app):
    keys = ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'ADMIN_PASSWORD_HASH', 'ADMIN_CREDENTIALS_GENERATED',
            'ADMIN_PASSWORD_GENERATED', 'RESET_ADMIN_PASSWORD_ON_START')
    saved = {k: app.config.get(k) for k in keys}
    with app.app_context():
        owner = StaffAccount.query.filter_by(username=ADMIN_USERNAME).first()
        # 還原成原本的雜湊值 (不是重新 set_password)：雜湊含隨機鹽，換掉會讓 session_version 改變，
        # 之後共用的 admin_client (session 範圍的 fixture) 全部被登出
        original = (owner.password_hash, owner.role, owner.session_epoch)
    yield app.config
    app.config.update(saved)
    with app.app_context():
        owner = StaffAccount.query.filter_by(username=ADMIN_USERNAME).first()
        owner.password_hash, owner.role, owner.session_epoch = original
        owner.is_active = True
        db.session.commit()


def test_admin_username_of_other_tenant_is_not_reset(app, owner_cfg):
    from app.services.bootstrap import ensure_defaults
    tid = get_or_create_tenant(app, 'core-infra-1-other')
    with app.app_context():
        other = StaffAccount.query.filter_by(username='ci1_other_admin').first()
        if other is None:
            other = StaffAccount(tenant_id=tid, username='ci1_other_admin', role='owner', is_active=True)
            db.session.add(other)
        other.set_password('OtherShop!Pass2026')
        db.session.commit()
    owner_cfg.update(ADMIN_USERNAME='ci1_other_admin', ADMIN_PASSWORD='Hijack!Pass2026',
                     ADMIN_PASSWORD_HASH=generate_password_hash('Hijack!Pass2026'),
                     ADMIN_CREDENTIALS_GENERATED=False, RESET_ADMIN_PASSWORD_ON_START=True)
    with app.app_context():
        ensure_defaults()
        other = StaffAccount.query.filter_by(username='ci1_other_admin').first()
        assert other.tenant_id == tid and other.check_password('OtherShop!Pass2026'), '不可改到其他店家的帳號'


def test_reset_with_only_password_keeps_owner_username(app, owner_cfg):
    from app.services.bootstrap import ensure_defaults
    owner_cfg.update(ADMIN_USERNAME='owner-random-name', ADMIN_PASSWORD='Rescue!Pass2026',
                     ADMIN_PASSWORD_HASH=generate_password_hash('Rescue!Pass2026'),
                     ADMIN_CREDENTIALS_GENERATED=True, ADMIN_PASSWORD_GENERATED=False,
                     RESET_ADMIN_PASSWORD_ON_START=True)
    with app.app_context():
        ensure_defaults()
        owner = StaffAccount.query.filter_by(username=ADMIN_USERNAME).first()
        assert owner is not None and owner.check_password('Rescue!Pass2026')
        assert StaffAccount.query.filter_by(username='owner-random-name').first() is None


def test_generated_password_not_written_to_log(caplog, capsys):
    from app.services.bootstrap import _announce
    cfg = {'ADMIN_USERNAME': 'owner-abc', 'ADMIN_PASSWORD': 'Sup3r-Secret-Generated'}
    with caplog.at_level('INFO'):
        _announce(cfg, '已建立')
    assert 'Sup3r-Secret-Generated' not in caplog.text and 'owner-abc' in caplog.text
    assert 'Sup3r-Secret-Generated' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# B-206 / B-207 / B-208 即時通知
# ---------------------------------------------------------------------------
def test_sse_quota_evicts_busiest_tenant_first():
    from app.services.event_bus import OrderEventBus
    bus = OrderEventBus(max_public_subscribers=4, max_per_ip=10)
    a = [bus.subscribe(1, client_ip=f'10.0.0.{i}') for i in range(4)]
    b1 = bus.subscribe(2, client_ip='10.0.1.1')
    assert a[0].get_nowait() is None, '店家 1 連線最多：淘汰它最舊的連線'
    b2 = bus.subscribe(2, client_ip='10.0.1.2')
    assert a[1].get_nowait() is None
    bus.subscribe(1, client_ip='10.0.0.9')   # 店家 1 (2 條 + 新的 1 條) 已不少於店家 2：淘汰自己的
    assert a[2].get_nowait() is None
    for q in (b1, b2):
        with pytest.raises(queue.Empty):
            q.get_nowait()


class _DownPubSub:
    def subscribe(self, channel):
        raise ConnectionError('Redis 斷線')

    def listen(self):
        raise ConnectionError('Redis 斷線')

    def close(self):
        pass


class _DownRedis:
    def __init__(self):
        self.publishes = 0

    def pubsub(self, **kw):
        return _DownPubSub()

    def publish(self, channel, payload):
        self.publishes += 1
        raise ConnectionError('Redis 斷線')


def test_subscribe_survives_redis_outage_and_notify_falls_back():
    from app.services.event_bus import OrderEventBus
    bus = OrderEventBus()
    bus.retry_base = 30
    bus._redis = _DownRedis()
    q = bus.subscribe(3, client_ip='10.1.0.1')   # 不可拋例外 (B-208)
    assert q in bus._subscribers and bus._listener is not None and bus._listener.is_alive()
    bus.notify(3)
    assert q.get_nowait() is not None, 'Redis 發布失敗時改為直接通知本行程'
    bus.notify(3)
    assert bus._redis.publishes == 1, '失敗後一段時間內不再每次都卡在連線逾時'
    assert q.get_nowait() is not None
    bus.unsubscribe(q)


def test_init_app_keeps_redis_client_when_ping_fails():
    from app.services.event_bus import OrderEventBus
    bus = OrderEventBus()
    bus.init_app(SimpleNamespace(config={'REDIS_URL': 'redis://127.0.0.1:1/0'}))
    assert bus._redis is not None, '啟動時連不上也保留用戶端，之後自動重試'
    assert bus._redis_down_until > time.monotonic()


def test_notify_recovers_after_retry_window():
    from app.services.event_bus import OrderEventBus

    class _UpRedis:
        def __init__(self):
            self.publishes = 0

        def publish(self, channel, payload):
            self.publishes += 1

    bus = OrderEventBus()
    bus._redis = _UpRedis()
    bus._redis_down_until = time.monotonic() - 1
    bus._listening = True
    bus.notify(5)
    assert bus._redis.publishes == 1


# ---------------------------------------------------------------------------
# B-197 / B-202 CLI
# ---------------------------------------------------------------------------
def test_backup_keep_days_must_be_positive(app, monkeypatch):
    from app.services import maintenance_service
    called = []
    monkeypatch.setattr(maintenance_service, 'run_sqlite_backup', lambda keep: called.append(keep) or 'x.zip')
    runner = app.test_cli_runner()
    for bad in ('0', '-1'):
        result = runner.invoke(args=['ops', 'backup', '--keep-days', bad])
        assert result.exit_code == 2, result.output
    assert called == []
    assert runner.invoke(args=['ops', 'backup', '--keep-days', '1']).exit_code == 0 and called == [1]


def test_tables_check_fix_exit_code_reflects_remaining_issues(app, monkeypatch):
    from app.services import table_service
    conflicts = [{'kind': 'cancelled_paid', 'tenant_id': 1, 'message': '#1 已取消卻已收款', 'order_id': 1},
                 {'kind': 'orphan', 'tenant_id': 1, 'message': '#2 沒有桌位', 'order_id': 2}]
    state = {'found': list(conflicts)}

    def fix(found):
        state['found'] = [i for i in state['found'] if i['kind'] != 'cancelled_paid']
        return 1

    monkeypatch.setattr(table_service, 'find_conflicts', lambda hours: list(state['found']))
    monkeypatch.setattr(table_service, 'fix_conflicts', fix)
    runner = app.test_cli_runner()
    assert runner.invoke(args=['tables', 'check', '--fix']).exit_code == 1, '仍有需人工確認的項目'
    state['found'] = [conflicts[0]]
    assert runner.invoke(args=['tables', 'check', '--fix']).exit_code == 0, '全部已自動修正'


# ---------------------------------------------------------------------------
# B-066 營業日換日時間設定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('raw, expected', [('', 0), ('5', 5), ('-3', 0), ('30', 12), ('abc', 0)])
def test_business_day_start_hour_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv('BUSINESS_DAY_START_HOUR', raw)
    assert min(12, max(0, config_module._env_int('BUSINESS_DAY_START_HOUR', 0))) == expected
    assert config_module.Config.BUSINESS_DAY_START_HOUR in range(0, 13)


# ---------------------------------------------------------------------------
# B-269 清除長期未使用會員：列出因儲值金 / 寄杯而保留的會員
# ---------------------------------------------------------------------------
def test_purge_inactive_cli_lists_members_kept_for_balance(app):
    from app.models.sync import PointLedger
    from app.models.user import User
    from tests.support import make_member
    slug = 'core-infra-1-privacy'
    tid = get_or_create_tenant(app, slug)
    plain = make_member(app, '修測CLI舊會員', '0977820001', tenant_id=tid)
    wallet = make_member(app, '修測CLI儲值會員', '0977820002', tenant_id=tid)
    long_ago = datetime.now() - timedelta(days=900)
    with app.app_context():
        for uid in (plain, wallet):
            PointLedger.query.filter(PointLedger.user_id == uid).update({'created_at': long_ago})
            db.session.get(User, uid).last_login_at = long_ago
        db.session.get(User, wallet).wallet_balance = 500
        db.session.commit()
    runner = app.test_cli_runner()
    out = runner.invoke(args=['privacy', 'purge-inactive', '--tenant', slug]).output
    assert '修測CLI舊會員' in out and '將刪除' in out
    assert '另有 1 位會員' in out and f'#{wallet} 修測CLI儲值會員 0977****02 儲值金 $500' in out
    out = runner.invoke(args=['privacy', 'purge-inactive', '--tenant', slug, '--apply']).output
    assert '已刪除 1 位' in out and '修測CLI儲值會員' in out
    with app.app_context():
        assert db.session.get(User, plain) is None and db.session.get(User, wallet) is not None
