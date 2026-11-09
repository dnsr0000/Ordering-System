"""後台「顧客評價」：餐後滿意度統計與留言 (店長以上，或授權 feedback 權限的員工)；回覆與公開精選"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models.order import OrderFeedback
from app.services import feedback_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.utils import get_own

feedback_bp = Blueprint('feedback', __name__)

# 與 feedback_service.public_reviews 的預設條件一致：點餐頁只顯示 4 星以上且有留言的公開評價 (最新 5 筆)
PUBLIC_MIN_RATING = 4
PUBLIC_LIMIT = 5


def public_eligible(fb):
    """這筆評價勾「公開」後是否符合點餐頁「顧客好評」的顯示條件"""
    return (fb.rating or 0) >= PUBLIC_MIN_RATING and bool(fb.comment)


@feedback_bp.route('/admin/feedback', endpoint='index')
@permission_required('feedback')
def index():
    days = min(365, max(1, request.args.get('days', 30, type=int) or 30))
    return render_template('admin_feedback.html', days=days, s=feedback_service.summary(days),
                           order_label=feedback_service.order_label, public_eligible=public_eligible,
                           public_min_rating=PUBLIC_MIN_RATING, public_limit=PUBLIC_LIMIT)


@feedback_bp.route('/admin/feedback/<int:fb_id>/reply', methods=['POST'], endpoint='reply')
@permission_required('feedback')
def reply(fb_id):
    """回覆一句 (通知顧客) 並設定是否公開；空白回覆 = 取消回覆"""
    from flask_login import current_user
    fb = get_own(OrderFeedback, fb_id, '評價')
    text = (request.form.get('reply') or '').strip()
    feedback_service.reply(fb, text, is_public=request.form.get('is_public') == '1', staff=current_user.username)
    record_staff_audit('FEEDBACK_REPLY', details=f'回覆評價 #{fb.id} (訂單 #{fb.order_id})：{text[:60] or "(取消回覆)"}'
                       f'{"，公開" if fb.is_public else ""}')
    db.session.commit()
    if fb.is_public and not public_eligible(fb):
        flash(f'已更新回覆並標為公開，但點餐頁只顯示 {PUBLIC_MIN_RATING} 星以上且有留言的評價，這筆不會出現在點餐頁', 'warning')
    else:
        flash('已更新回覆' + (f'並公開顯示 (點餐頁顯示最新 {PUBLIC_LIMIT} 筆公開好評)' if fb.is_public else ''), 'success')
    return redirect(url_for('feedback.index', days=request.form.get('days') or 30, _anchor=f'fb-{fb.id}'))
