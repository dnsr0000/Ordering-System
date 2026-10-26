"""後台：API 金鑰與 Webhook (第三輪 #14)、裝置管理 (#15) 與裝置端的配對 / 心跳"""
from datetime import datetime

from flask import Blueprint, flash, g, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db, limiter
from app.models.integration import API_SCOPES, DEVICE_KINDS, WEBHOOK_EVENTS, ApiKey, Device, WebhookDelivery, \
    WebhookEndpoint
from app.services import device_service, webhook_service
from app.services.audit import record_staff_audit
from app.utils import get_json_dict, role_required, safe_int

integrations_bp = Blueprint('integrations', __name__)
devices_bp = Blueprint('devices', __name__)


def _own(model, oid, label):
    row = db.session.get(model, safe_int(oid, 0))
    if row is None or row.tenant_id != g.tenant_id:
        raise AppError(f'查無此{label}', 404)
    return row


# ---------------------------------------------------------------------------
# API 金鑰與 Webhook (負責人)
# ---------------------------------------------------------------------------
@integrations_bp.route('/admin/integrations', methods=['GET', 'POST'], endpoint='index')
@role_required('owner')
def index():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'create_key':
            name = (f.get('name') or '').strip()[:60]
            scopes = [s for s in f.getlist('scopes') if s in API_SCOPES]
            if not name or not scopes:
                raise AppError('請輸入名稱並至少勾選一項權限')
            key = ApiKey(tenant_id=g.tenant_id, name=name, scopes=','.join(scopes), created_by=current_user.username)
            raw = key.issue()
            db.session.add(key)
            record_staff_audit('API_KEY', details=f'建立 API 金鑰「{name}」({key.prefix}…)：{",".join(scopes)}')
            db.session.commit()
            # 金鑰直接顯示在這次回應 (不放進 Cookie 型的 session，也不會再出現)
            flash('✅ 已建立 API 金鑰，請立即複製 (只顯示這一次)', 'success')
            return _integrations_page(new_key={'name': name, 'key': raw})
        elif action == 'revoke_key':
            key = _own(ApiKey, f.get('key_id'), 'API 金鑰')
            key.is_active = False
            record_staff_audit('API_KEY', details=f'停用 API 金鑰「{key.name}」({key.prefix}…)')
            db.session.commit()
            flash(f'✅ 已停用「{key.name}」', 'success')
        elif action == 'create_hook':
            ep = WebhookEndpoint(tenant_id=g.tenant_id)
            webhook_service.save_endpoint(ep, f, current_user.username)
            db.session.add(ep)
            record_staff_audit('WEBHOOK', details=f'新增 Webhook「{ep.name}」{ep.url}')
            db.session.commit()
            flash(f'✅ 已新增 Webhook「{ep.name}」，簽章密鑰請見下方', 'success')
        elif action in ('toggle_hook', 'delete_hook', 'ping_hook'):
            ep = _own(WebhookEndpoint, f.get('endpoint_id'), 'Webhook')
            if action == 'toggle_hook':
                ep.is_active = not ep.is_active
                ep.fail_streak = 0
            elif action == 'delete_hook':
                record_staff_audit('WEBHOOK', details=f'刪除 Webhook「{ep.name}」')
                db.session.delete(ep)
            else:
                webhook_service.queue_ping(ep)
                flash('✅ 已排入測試通知 (約 15 秒內送出)', 'success')
            db.session.commit()
        elif action == 'retry':
            d = _own(WebhookDelivery, f.get('delivery_id'), '送出紀錄')
            d.status, d.next_attempt_at, d.attempts = 'pending', datetime.now(), 0
            db.session.commit()
            flash('✅ 已重新排入送出', 'success')
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('integrations.index'))
    return _integrations_page()


def _integrations_page(new_key=None):
    resp = make_response(render_template(
        'admin_integrations.html', keys=ApiKey.query.order_by(ApiKey.id.desc()).all(),
        hooks=WebhookEndpoint.query.order_by(WebhookEndpoint.id).all(),
        deliveries=WebhookDelivery.query.order_by(WebhookDelivery.id.desc()).limit(50).all(),
        scopes=API_SCOPES, events=WEBHOOK_EVENTS, new_key=new_key, base_url=request.host_url.rstrip('/')))
    resp.headers['Cache-Control'] = 'no-store'   # 含簽章密鑰 / 新金鑰：不快取
    return resp


# ---------------------------------------------------------------------------
# 對外 API 說明文件 (公開頁面；不含任何店家資料，試打時由使用者自行貼上金鑰，金鑰不會送到本頁以外的地方)
# ---------------------------------------------------------------------------
@integrations_bp.route('/developers', endpoint='docs')
@limiter.limit('60/minute')
def docs():
    from app.services import api_docs
    return render_template('developers.html', endpoints=api_docs.endpoints(), scopes=API_SCOPES, events=WEBHOOK_EVENTS,
                           errors=api_docs.ERRORS, webhook_headers=api_docs.WEBHOOK_HEADERS,
                           verify_sample=api_docs.VERIFY_SAMPLE, base_url=request.host_url.rstrip('/'))


@integrations_bp.route('/developers/openapi.json', endpoint='openapi')
@limiter.limit('60/minute')
def openapi():
    from app.services import api_docs
    resp = jsonify(api_docs.openapi_spec(request.host_url.rstrip('/')))
    resp.headers['Access-Control-Allow-Origin'] = '*'   # 讓 Swagger Editor / Postman 等工具直接匯入
    return resp


# ---------------------------------------------------------------------------
# 裝置管理 (負責人 / 店長)
# ---------------------------------------------------------------------------
@devices_bp.route('/admin/devices', methods=['GET', 'POST'], endpoint='admin')
@role_required('owner', 'manager')
def admin():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'create':
            d = device_service.create(g.tenant_id, f.get('name'), f.get('kind'), f.get('note'))
            record_staff_audit('DEVICE', details=f'新增裝置「{d.name}」({d.kind_label})')
            db.session.commit()
            flash(f'✅ 已新增「{d.name}」，請在該裝置開啟 /device/pair 輸入配對碼 {d.pair_code} (15 分鐘內有效)', 'success')
        else:
            d = _own(Device, f.get('device_id'), '裝置')
            if action == 'lock':
                d.is_locked, d.lock_message = True, (f.get('message') or '').strip()[:120]
                record_staff_audit('DEVICE', details=f'鎖定裝置「{d.name}」：{d.lock_message}')
                flash(f'✅ 已鎖定「{d.name}」(約 1 分鐘內畫面鎖定，操作立即拒絕)', 'success')
            elif action == 'unlock':
                d.is_locked = False
                record_staff_audit('DEVICE', details=f'解除鎖定「{d.name}」')
                flash(f'✅ 已解除鎖定「{d.name}」', 'success')
            elif action == 'reload':
                d.reload_requested_at = datetime.now()
                flash(f'✅「{d.name}」下次回報時會重新整理頁面', 'success')
            elif action == 'pair':
                code = device_service.new_pair_code(d)
                flash(f'✅「{d.name}」的新配對碼：{code} (15 分鐘內有效；配對後舊的瀏覽器失效)', 'success')
            elif action == 'delete':
                record_staff_audit('DEVICE', details=f'刪除裝置「{d.name}」')
                db.session.delete(d)
                flash('✅ 已刪除裝置 (該瀏覽器的註冊立即失效)', 'success')
            else:
                raise AppError('不支援的操作')
            db.session.commit()
        return redirect(url_for('devices.admin'))
    rows = device_service.status_rows(g.tenant_id)
    return render_template('admin_devices.html', rows=rows, kinds=DEVICE_KINDS, version=device_service.app_version(),
                           now=datetime.now())


@devices_bp.route('/device/pair', methods=['GET', 'POST'], endpoint='pair')
@limiter.limit('10/minute', methods=['POST'])
def pair():
    """在裝置上輸入後台產生的配對碼"""
    if request.method == 'POST':
        d, raw = device_service.pair(g.tenant_id, request.form.get('code'))
        db.session.commit()
        resp = redirect(device_service.START_PAGES.get(d.kind, '/'))
        resp.set_cookie(device_service.COOKIE, raw, max_age=2 * 365 * 86400, httponly=True, samesite='Lax',
                        secure=request.is_secure)
        flash(f'✅ 此裝置已註冊為「{d.name}」', 'success')
        return resp
    return render_template('device_pair.html', current=device_service.current())


@devices_bp.route('/api/device/heartbeat', methods=['POST'], endpoint='heartbeat')
@limiter.limit('20/minute')
def heartbeat():
    d = device_service.current()
    if d is None:
        return jsonify({'success': True, 'registered': False})
    data = get_json_dict()
    result = device_service.heartbeat(d, data.get('version'), data.get('path'))
    db.session.commit()
    return jsonify(result)


@devices_bp.app_context_processor
def _inject_device():
    from flask import has_request_context
    if not has_request_context():
        return {}
    registered = bool(request.cookies.get(device_service.COOKIE))
    return {'device_registered': registered, 'app_version': device_service.app_version() if registered else ''}


@devices_bp.before_app_request
def _enforce_device_lock():
    return device_service.enforce_lock()


# ---------------------------------------------------------------------------
# 舊資料封存 (負責人)
# ---------------------------------------------------------------------------
archive_bp = Blueprint('archive', __name__)


@archive_bp.route('/admin/archive', methods=['GET', 'POST'], endpoint='index')
@role_required('owner')
def archive_index():
    from flask import send_file
    from app.services import archive_service
    cutoff = archive_service.cutoff_for(g.tenant_id)
    if request.method == 'POST':
        if request.form.get('action') != 'run':
            raise AppError('不支援的操作')
        counts = archive_service.run(g.tenant_id, cutoff, current_user.username)
        done = '、'.join(f'{archive_service.LABELS[k]} {v} 筆' for k, v in counts.items() if k != 'file' and v)
        flash(f'✅ 已封存 {cutoff:%Y-%m-%d} 以前的資料：{done or "沒有符合的資料"}', 'success')
        return redirect(url_for('archive.index'))
    if request.args.get('download'):
        info = archive_service.file_info(g.tenant_id)
        if info is None:
            raise AppError('尚未產生封存檔', 404)
        return send_file(info['path'], as_attachment=True, download_name=f'archive_{g.tenant.slug}.db')
    q = (request.args.get('q') or '').strip()
    return render_template('admin_archive.html', cutoff=cutoff, preview=archive_service.preview(g.tenant_id, cutoff),
                           labels=archive_service.LABELS, info=archive_service.file_info(g.tenant_id), q=q,
                           results=archive_service.search(g.tenant_id, q) if q else [])
