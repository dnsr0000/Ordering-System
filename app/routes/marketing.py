"""後台：會員分群 (RFM) 與自動行銷 (久未消費自動發券、排程推播)；說明見 app/services/marketing_service.py"""
import io
from datetime import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from openpyxl import Workbook

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon
from app.models.marketing import CAMPAIGN_KINDS, MarketingCampaign
from app.services import marketing_service
from app.services.audit import record_staff_audit
from app.utils import get_own, neutralize_formulas, role_required

marketing_bp = Blueprint('marketing', __name__)


def _campaign(cid):
    c = get_own(MarketingCampaign, cid, '活動')
    return c


@marketing_bp.route('/admin/marketing', methods=['GET', 'POST'], endpoint='index')
@role_required(*MANAGER_ROLES)
def index():
    can_run = marketing_service.can_run()
    if request.method == 'POST':
        if not can_run:
            raise AppError('自動行銷由總部 (線上端) 執行', 403)
        f = request.form
        action = f.get('action')
        if action == 'create':
            c = MarketingCampaign(tenant_id=g.tenant_id)
            marketing_service.save_campaign(c, f, current_user.username)
            db.session.add(c)
            record_staff_audit('MARKETING', details=f'新增自動行銷「{c.name}」({c.kind_label})')
            db.session.commit()
            flash(f'已建立「{c.name}」' + ('，每天 10:00 自動檢查' if c.kind == 'winback'
                                             else f'，將於 {c.send_at:%m/%d %H:%M} 發送'), 'success')
        elif action == 'toggle':
            c = _campaign(f.get('campaign_id'))
            if c.status in ('done', 'running'):
                raise AppError('此活動已發送完畢或正在執行中')
            c.status = 'paused' if c.status == 'active' else 'active'
            db.session.commit()
            flash(f"「{c.name}」已{'暫停' if c.status == 'paused' else '啟用'}", 'success')
        elif action == 'run_now':
            c = _campaign(f.get('campaign_id'))
            sent = marketing_service.run_campaign(c, staff=current_user.username)
            record_staff_audit('MARKETING', details=f'立即執行自動行銷「{c.name}」：{sent} 位會員')
            db.session.commit()
            flash(f'「{c.name}」已發送給 {sent} 位會員', 'success')
        elif action == 'delete':
            c = _campaign(f.get('campaign_id'))
            record_staff_audit('MARKETING', details=f'刪除自動行銷「{c.name}」(已發出的券保留)')
            db.session.delete(c)
            db.session.commit()
            flash('已刪除活動 (已發出的券不受影響)', 'success')
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('marketing.index'))

    rows = marketing_service.member_stats(g.tenant_id)
    segment = request.args.get('segment') or ''
    members = sorted([r for r in rows if r['segment'] == segment], key=lambda r: -r['spend']) if segment else []
    if segment and request.args.get('format') == 'xlsx':
        wb = Workbook()
        ws = wb.active
        ws.title = marketing_service.SEGMENTS.get(segment, {}).get('label', segment)
        ws.append(['姓名', '手機', '最近消費', '距今天數', '近一年次數', '近一年金額', 'R', 'F', 'M', 'LINE', '拒絕行銷'])
        for r in members:
            ws.append([r['name'], r['phone'], r['last_at'].strftime('%Y-%m-%d') if r['last_at'] else '', r['days'],
                       r['orders'], r['spend'], r['r'], r['f'], r['m'], '是' if r['line'] else '', '是' if r['opt_out'] else ''])
        neutralize_formulas(wb)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f'會員分群_{ws.title}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    campaigns = MarketingCampaign.query.order_by(MarketingCampaign.id.desc()).all()
    coupons = [c for c in Coupon.query.order_by(Coupon.id.desc()).all() if c.period_error() is None]
    return render_template('admin_marketing.html', can_run=can_run, summary=marketing_service.summary(rows),
                           segments=marketing_service.SEGMENTS, segment=segment, members=members[:300],
                           member_total=len(rows), campaigns=campaigns,
                           stats={c.id: marketing_service.stats(c) for c in campaigns}, coupons=coupons,
                           kinds=CAMPAIGN_KINDS, now=datetime.now())
