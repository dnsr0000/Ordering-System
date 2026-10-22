"""隱私權告知 (個資法第 8 條)、會員同意紀錄、撤回人臉辨識，以及後台的營運者資訊設定"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.extensions import db
from app.services import privacy_service
from app.services.audit import record_staff_audit
from app.services.customer_session import current_member
from app.utils import role_required

privacy_bp = Blueprint('privacy', __name__)


@privacy_bp.route('/privacy', endpoint='notice')
def notice():
    member = current_member()
    return render_template('privacy.html', p=privacy_service.notice_context(g.tenant_id), member=member,
                           needs_consent=privacy_service.needs_consent(member))


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
@role_required('owner', 'manager')
def settings():
    if request.method == 'POST':
        privacy_service.save_setting(g.tenant_id, request.form)
        record_staff_audit('EDIT_PRIVACY_SETTING', details='修改隱私權告知的營運者資訊')
        db.session.commit()
        flash('已儲存隱私權告知設定', 'success')
        return redirect(url_for('privacy.settings'))
    return render_template('admin_privacy.html', p=privacy_service.notice_context(g.tenant_id),
                           setting=privacy_service.get_setting(g.tenant_id))
