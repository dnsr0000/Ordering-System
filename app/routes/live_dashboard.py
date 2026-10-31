"""總部即時看板 (/admin/live)：線上端看全部分店，單機 / 分店看本店 (店長以上)"""
from flask import Blueprint, g, jsonify, render_template

from app.models.enums import MANAGER_ROLES
from app.services import live_dashboard_service
from app.utils import role_required

live_bp = Blueprint('live', __name__)


@live_bp.route('/admin/live', endpoint='index')
@role_required(*MANAGER_ROLES)
def index():
    return render_template('admin_live.html', data=live_dashboard_service.snapshot(g.tenant_id))


@live_bp.route('/api/admin/live', endpoint='api')
@role_required(*MANAGER_ROLES)
def api():
    return jsonify(dict(live_dashboard_service.snapshot(g.tenant_id), success=True))
