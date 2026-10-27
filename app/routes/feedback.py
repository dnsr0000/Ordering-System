"""後台「顧客評價」：餐後滿意度統計與留言 (店長以上)"""
from flask import Blueprint, render_template, request

from app.services import feedback_service
from app.utils import role_required

feedback_bp = Blueprint('feedback', __name__)
MANAGERS = ('owner', 'manager')


@feedback_bp.route('/admin/feedback', endpoint='index')
@role_required(*MANAGERS)
def index():
    days = min(365, max(1, request.args.get('days', 30, type=int) or 30))
    return render_template('admin_feedback.html', days=days, s=feedback_service.summary(days),
                           order_label=feedback_service.order_label)
