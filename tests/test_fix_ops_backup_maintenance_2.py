"""修正小組 ops-backup-maintenance-2 的回歸測試：系統監控事件跨店外洩、一鍵更新路徑含空白、待處理磁磚連結"""
import html as html_lib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta

from app.extensions import db
from tests.support import default_tenant_id, get_or_create_tenant, login_staff, other_shop_owner_client


# ---------------------------------------------------------------------------
# B-451：其他店家的店長不能在系統監控頁看到整台主機的事件紀錄
# ---------------------------------------------------------------------------
def test_system_events_not_leaked_to_other_tenant(app, admin_client):
    from app.services import monitor_service
    other_tid = get_or_create_tenant(app, 'fix-b451-shop')
    other = other_shop_owner_client(app, slug='fix-b451-shop')
    secret = f'0912{uuid.uuid4().int % 1000000:06d}'
    own = f'本店事件-{uuid.uuid4().hex[:8]}'
    with app.app_context():
        monitor_service.alert(f'團體預購「家庭聚餐」(王大明 {secret}) 送單失敗', key=f'b451-{secret}')
        # 已標記店家的事件 (monitor_service 之後寫入 tenant_id 時)：只給該店看
        with open(monitor_service._events_path(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': datetime.now().isoformat(timespec='seconds'), 'level': 'warning',
                                 'source': 'test', 'message': own, 'tenant_id': other_tid}, ensure_ascii=False) + '\n')
    page = other.get('/admin/system')
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert secret not in text, '其他店家不可看到未標記店家的系統事件'
    assert own in text, '標記為本店的事件仍顯示'
    assert '僅平台管理者' in text
    # 平台管理者 (預設店家負責人) 看得到全部
    admin_page = admin_client.get('/admin/system').get_data(as_text=True)
    assert secret in admin_page and own in admin_page
    # 預設店家的店長 (非平台管理者) 也只看本店事件
    manager = login_staff(app, 'fix_b451_manager', 'manager')
    assert secret not in manager.get('/admin/system').get_data(as_text=True)


# ---------------------------------------------------------------------------
# B-462：專案路徑含空白時更新腳本仍能執行
# ---------------------------------------------------------------------------
def test_update_apply_quotes_paths_with_spaces(app, monkeypatch, tmp_path):
    from app.services import update_service as svc
    root = tmp_path / 'John Smith' / 'project'
    (root / 'deploy' / 'windows').mkdir(parents=True)
    updates = root / 'database' / 'updates'
    updates.mkdir(parents=True)
    marker = root / 'marker.txt'
    (root / 'deploy' / 'windows' / 'update_store.cmd').write_text(
        f'@echo off\r\n>"{marker}" echo [%~1][%~2][%~3]\r\n', encoding='ascii')
    zip_path = updates / 'v2099.1.1.zip'
    zip_path.write_bytes(b'PK')
    monkeypatch.setattr(svc, 'ROOT', str(root))
    monkeypatch.setattr(svc, 'updates_dir', lambda: str(updates))
    monkeypatch.setattr(svc.os, 'name', 'nt')
    captured = {}

    def fake_popen(args, **kw):
        captured['args'] = args
        return None
    monkeypatch.setattr(svc.subprocess, 'Popen', fake_popen)
    with app.app_context():
        running = svc.apply(str(zip_path))
    cmd = captured['args']
    assert isinstance(cmd, str) and cmd.startswith('cmd.exe /s /c ""') and cmd.endswith('""')
    assert f'"{running}"' in cmd and f'"{zip_path}"' in cmd
    if os.name == 'nt':   # 實際以 cmd 執行一次 (不分離行程，等它跑完)，確認含空白的路徑沒有被截斷
        monkeypatch.undo()
        rc = subprocess.run(cmd, cwd=str(root), check=False, timeout=30).returncode
        assert rc == 0 and marker.exists()
        out = marker.read_text(encoding='mbcs', errors='replace')
        assert f'[{zip_path}]' in out


# ---------------------------------------------------------------------------
# R2-181：待處理磁磚的數字與點進去的清單一致 (失敗退款、30 天前的紀錄)
# ---------------------------------------------------------------------------
def test_backlog_tiles_link_to_matching_lists(app, admin_client):
    from app.models.payment import Refund
    reason_failed = f'失敗退款-{uuid.uuid4().hex[:8]}'
    reason_manual = f'人工退款-{uuid.uuid4().hex[:8]}'
    with app.app_context():
        tid = default_tenant_id(app)
        old = datetime.now() - timedelta(days=90)
        db.session.add(Refund(tenant_id=tid, amount=10, method='LINE Pay', reason=reason_failed, status='failed', created_at=old))
        db.session.add(Refund(tenant_id=tid, amount=20, method='Credit Card', reason=reason_manual, status='manual', created_at=old))
        db.session.commit()
    page = admin_client.get('/admin/system').get_data(as_text=True)
    links = {}
    for href, label, value in re.findall(r'<a class="stat-tile[^"]*" href="([^"]+)"><div class="stat-label">([^<]+)</div>'
                                         r'<div class="stat-value[^"]*">(\d+)</div>', page):
        links[label] = (html_lib.unescape(href), int(value))
    assert '退款失敗' in links and '需人工退款' in links
    failed_url, failed_count = links['退款失敗']
    manual_url, manual_count = links['需人工退款']
    assert 'status=failed' in failed_url and 'start=2000-01-01' in failed_url and failed_count >= 1
    assert 'status=manual' in manual_url and manual_count >= 1
    failed_list = admin_client.get(failed_url).get_data(as_text=True)
    assert reason_failed in failed_list and reason_manual not in failed_list
    assert reason_manual in admin_client.get(manual_url).get_data(as_text=True)
    for label in ('待開立發票', '開立失敗發票'):
        assert 'start=2000-01-01' in links[label][0]


# ---------------------------------------------------------------------------
# B-384：線上端的待處理發票磁磚不計入分店上傳的發票 (分店的發票由分店處理)
# ---------------------------------------------------------------------------
def _backlog_counts(client):
    page = client.get('/admin/system').get_data(as_text=True)
    return {label: int(value) for label, value in re.findall(
        r'<div class="stat-label">([^<]+)</div><div class="stat-value[^"]*">(\d+)</div>', page)}


def test_hub_backlog_excludes_branch_invoices(app, admin_client, staff_client, monkeypatch):
    from app.models.order import Order
    from app.models.payment import Invoice, InvoiceStatus
    from app.services import einvoice_ecpay as ecpay
    from tests.support import checkout, line, make_item
    iid = make_item(app, f'B384 分店發票-{uuid.uuid4().hex[:6]}', price=100, stock=20)
    r = checkout(staff_client, [line(iid)], url='/api/pos/checkout', cash_received=100)
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    with app.app_context():
        db.session.get(Order, oid).node_uuid = f'branch-{uuid.uuid4().hex[:8]}'
        db.session.commit()
    # 以加入發票前的數字為基準比較增減 (同一個資料庫裡其他測試留下的發票，門市與線上端的計算方式本來就不同)
    role = app.config.get('SYNC_ROLE')
    store0 = _backlog_counts(admin_client)
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    hub0 = _backlog_counts(admin_client)
    monkeypatch.setitem(app.config, 'SYNC_ROLE', role)
    with app.app_context():
        for status in (InvoiceStatus.PENDING, InvoiceStatus.FAILED):
            db.session.add(Invoice(tenant_id=default_tenant_id(app), order_id=oid, relate_number=ecpay.new_relate_number(),
                                   status=status, amount=100))
        db.session.commit()
    store = _backlog_counts(admin_client)          # 門市 / 單機：本機的發票都要處理
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    hub = _backlog_counts(admin_client)
    for label in ('待開立發票', '開立失敗發票'):
        assert store[label] == store0[label] + 1, f'{label}：門市要計入本機的發票'
        assert hub[label] == hub0[label], f'{label}：線上端不應計入分店上傳的發票'
    # 改成線上點餐 (線上端自己的訂單) 後照常計入
    with app.app_context():
        db.session.get(Order, oid).order_source = 'Online'
        db.session.commit()
    hub = _backlog_counts(admin_client)
    for label in ('待開立發票', '開立失敗發票'):
        assert hub[label] == hub0[label] + 1


# ---------------------------------------------------------------------------
# B-459：測試通知送出失敗時顯示失敗原因，不再誤報「尚未設定任何通知管道」
# ---------------------------------------------------------------------------
def test_test_alert_reports_channel_errors(admin_client, monkeypatch):
    from app.services import monitor_service
    from tests.support import flashes

    def failing(tenant_id, level, message, errors=None):
        errors.append('Webhook：不可指向內網位址')
        return []
    monkeypatch.setattr(monitor_service, 'send_alert_now', failing)
    admin_client.post('/admin/system/test_alert')
    msgs = flashes(admin_client)
    assert any('通知送出失敗' in m and '不可指向內網位址' in m for m in msgs), msgs
    assert not any('尚未設定任何通知管道' in m for m in msgs)

    def partial(tenant_id, level, message, errors=None):
        errors.append('Webhook：連線失敗 (ConnectionError)')
        return ['email']
    monkeypatch.setattr(monitor_service, 'send_alert_now', partial)
    admin_client.post('/admin/system/test_alert')
    msgs = flashes(admin_client)
    assert any('通知送出失敗' in m and '已送出：email' in m for m in msgs), msgs
