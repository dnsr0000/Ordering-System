"""隱私權告知 (個資法第 8 條)、會員同意紀錄、撤回人臉辨識、會員自助下載資料 / 刪除帳號，以及後台的營運者資訊設定"""
import json

from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.services import privacy_service
from app.services.audit import record_staff_audit
from app.services.customer_session import current_member, needs_pin_confirm, require_confirmed
from app.utils import role_required

privacy_bp = Blueprint('privacy', __name__)


@privacy_bp.route('/privacy', endpoint='notice')
def notice():
    member = current_member()
    return render_template('privacy.html', p=privacy_service.notice_context(g.tenant_id), member=member,
                           needs_consent=privacy_service.needs_consent(member), needs_pin=needs_pin_confirm())


@privacy_bp.route('/privacy/consent', methods=['POST'], endpoint='consent')
def consent():
    member = current_member()
    if member is None:
        return redirect(url_for('privacy.notice'))
    privacy_service.record_consent(member)
    db.session.commit()
    flash('已記錄您同意本隱私權告知，謝謝！', 'success')
    target = request.form.get('next') or ''
    # 只允許站內相對路徑 (避免被拿來導向其他網站)
    if not target.startswith('/') or target.startswith('//') or '\\' in target:
        target = url_for('customer.customer_index')
    return redirect(target)


@privacy_bp.route('/privacy/my_data', methods=['GET', 'POST'], endpoint='my_data')
@limiter.limit('10/minute')
def my_data():
    """會員下載自己的個人資料 (JSON)；人臉登入的工作階段需以 POST 附上 PIN"""
    member = current_member()
    if member is None:
        return redirect(url_for('auth.member_login'))
    require_confirmed(member, request.form.get('pin'), '下載個人資料')
    body = json.dumps(privacy_service.export_member_data(member), ensure_ascii=False, indent=2)
    return Response(body, mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename="my_member_data.json"',
                             'Cache-Control': 'no-store'})


@privacy_bp.route('/privacy/delete_account', methods=['POST'], endpoint='delete_account')
@limiter.limit('5/minute')
def delete_account():
    """會員刪除帳號：需再次輸入 PIN 並輸入「刪除」確認"""
    from app.services.customer_session import close_customer_session
    member = current_member()
    if member is None:
        return redirect(url_for('auth.member_login'))
    if (request.form.get('confirm') or '').strip() != '刪除':
        raise AppError('請在確認欄輸入「刪除」兩個字')
    if not member.check_pin(request.form.get('pin')):
        raise AppError('PIN 碼不正確', 403)
    privacy_service.delete_member_account(member)
    db.session.commit()
    close_customer_session()   # 會員已刪除：只結束進場紀錄並清除登入狀態
    flash('您的會員帳號與個人資料已刪除，歷史訂單已去識別化保存 (帳務依法保存)。謝謝您的支持！', 'info')
    return redirect(url_for('customer.customer_index'))


@privacy_bp.route('/privacy/face/delete', methods=['POST'], endpoint='delete_face')
def delete_face():
    member = current_member()
    if member is None:
        return redirect(url_for('privacy.notice'))
    privacy_service.withdraw_face(member)
    db.session.commit()
    flash('已刪除您的人臉辨識資料，之後請以手機號碼 + PIN 登入。', 'success')
    return redirect(url_for('privacy.notice'))


@privacy_bp.route('/admin/privacy', methods=['GET', 'POST'], endpoint='settings')
@role_required(*MANAGER_ROLES)
def settings():
    if request.method == 'POST':
        privacy_service.save_setting(g.tenant_id, request.form)
        record_staff_audit('EDIT_PRIVACY_SETTING', details='修改隱私權告知的營運者資訊')
        db.session.commit()
        flash('已儲存隱私權告知設定', 'success')
        return redirect(url_for('privacy.settings'))
    return render_template('admin_privacy.html', p=privacy_service.notice_context(g.tenant_id),
                           setting=privacy_service.get_setting(g.tenant_id))
