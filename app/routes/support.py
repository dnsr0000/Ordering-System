"""後台「客服對話」：顧客從 AI 客服轉接的真人對話；店員在這裡回覆 (店長以上，或授權 feedback 權限的員工)"""
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from app.extensions import db
from app.models.support import SupportChat
from app.services import support_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.utils import get_json_dict, get_own

support_bp = Blueprint('support', __name__)


@support_bp.route('/admin/support', endpoint='index')
@permission_required('feedback')
def index():
    return render_template('admin_support.html')


@support_bp.route('/api/admin/support/chats', endpoint='chats')
@permission_required('feedback')
def chats():
    """?closed=1 時包含已結束的對話"""
    return jsonify({'success': True, 'chats': support_service.list_chats(include_closed=request.args.get('closed') == '1'),
                    'waiting': support_service.waiting_count()})


@support_bp.route('/api/admin/support/chats/<int:chat_id>', endpoint='chat')
@permission_required('feedback')
def chat(chat_id):
    """?after=<最後一則 id> → 對話資訊與較新的訊息；打開即標示為已讀"""
    c = get_own(SupportChat, chat_id, '客服對話')
    support_service.mark_seen(c)
    db.session.commit()
    return jsonify({'success': True, 'chat': support_service.chat_dict(c),
                    'messages': support_service.messages_after(c, request.args.get('after'))})


@support_bp.route('/api/admin/support/chats/<int:chat_id>/reply', methods=['POST'], endpoint='reply')
@permission_required('feedback')
def reply(chat_id):
    c = get_own(SupportChat, chat_id, '客服對話')
    msg = support_service.staff_reply(c, current_user.username, get_json_dict().get('text'))
    db.session.commit()
    return jsonify({'success': True, 'message': support_service.message_dict(msg), 'chat': support_service.chat_dict(c)})


@support_bp.route('/api/admin/support/chats/<int:chat_id>/close', methods=['POST'], endpoint='close')
@permission_required('feedback')
def close(chat_id):
    c = get_own(SupportChat, chat_id, '客服對話')
    support_service.close(c, current_user.username)
    record_staff_audit('SUPPORT_CLOSE', details=f'結束客服對話 #{c.id} ({c.customer_label})')
    db.session.commit()
    return jsonify({'success': True, 'chat': support_service.chat_dict(c)})
