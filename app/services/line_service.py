"""
LINE：
- LINE Login (OAuth 2.0 / OpenID Connect)：會員以 LINE 登入或綁定既有會員。
- Messaging API：推播 (取餐完成、生日禮、行銷訊息、系統警示給管理者)。
金鑰在後台「系統設定 → LINE 登入與官方帳號」填寫。
"""
import logging
import secrets
import urllib.parse

import requests

from app.errors import AppError
from app.services import settings_service

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://access.line.me/oauth2/v2.1/authorize'
TOKEN_URL = 'https://api.line.me/oauth2/v2.1/token'
VERIFY_URL = 'https://api.line.me/oauth2/v2.1/verify'
PUSH_URL = 'https://api.line.me/v2/bot/message/push'
MULTICAST_URL = 'https://api.line.me/v2/bot/message/multicast'
BOT_INFO_URL = 'https://api.line.me/v2/bot/info'


def _post(url, **kwargs):
    """實際送出 (測試時以 monkeypatch 取代)；回傳 (狀態碼, JSON dict)"""
    resp = requests.post(url, timeout=kwargs.pop('timeout', 15), **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def _get(url, **kwargs):
    resp = requests.get(url, timeout=kwargs.pop('timeout', 15), **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


# ---------------------------------------------------------------------------
# LINE Login
# ---------------------------------------------------------------------------
def login_enabled(tenant_id=None):
    cfg = settings_service.get('line', tenant_id)
    return bool(cfg.get('login_enabled') and cfg.get('login_channel_id') and cfg.get('login_channel_secret'))


def authorize_url(redirect_uri, state, nonce, tenant_id=None):
    cfg = settings_service.get('line', tenant_id)
    query = urllib.parse.urlencode({
        'response_type': 'code', 'client_id': cfg['login_channel_id'], 'redirect_uri': redirect_uri,
        'state': state, 'scope': 'openid profile', 'nonce': nonce,
    })
    return f'{AUTHORIZE_URL}?{query}'


def new_state():
    return secrets.token_urlsafe(24), secrets.token_urlsafe(16)


def exchange_code(code, redirect_uri, nonce, tenant_id=None):
    """授權碼換 id_token 並驗證，回傳 {'user_id', 'name'}；失敗拋 AppError"""
    cfg = settings_service.get('line', tenant_id)
    try:
        status, token = _post(TOKEN_URL, data={
            'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
            'client_id': cfg['login_channel_id'], 'client_secret': cfg['login_channel_secret']})
    except requests.RequestException as exc:
        raise AppError(f'無法連線到 LINE：{exc}', 502)
    if status != 200 or not token.get('id_token'):
        raise AppError('LINE 登入失敗 (授權碼無效或已過期)，請重新登入', 401)
    try:
        status, claims = _post(VERIFY_URL, data={'id_token': token['id_token'], 'client_id': cfg['login_channel_id'],
                                                 'nonce': nonce})
    except requests.RequestException as exc:
        raise AppError(f'無法連線到 LINE：{exc}', 502)
    if status != 200 or not claims.get('sub'):
        raise AppError('LINE 身分驗證失敗，請重新登入', 401)
    return {'user_id': claims['sub'], 'name': claims.get('name') or ''}


# ---------------------------------------------------------------------------
# Messaging API
# ---------------------------------------------------------------------------
def messaging_enabled(tenant_id=None):
    cfg = settings_service.get('line', tenant_id)
    return bool(cfg.get('messaging_enabled') and cfg.get('messaging_access_token'))


def _auth(tenant_id):
    cfg = settings_service.get('line', tenant_id)
    return {'Authorization': f"Bearer {cfg.get('messaging_access_token') or ''}", 'Content-Type': 'application/json'}


def push_text(tenant_id, line_user_id, text):
    """推播文字給一位使用者；回傳是否成功 (失敗只記錄，不影響主要流程)"""
    if not line_user_id or not messaging_enabled(tenant_id):
        return False
    try:
        status, body = _post(PUSH_URL, headers=_auth(tenant_id),
                             json={'to': line_user_id, 'messages': [{'type': 'text', 'text': text[:4900]}]})
    except requests.RequestException as exc:
        logger.warning('LINE 推播失敗：%s', exc)
        return False
    if status != 200:
        logger.warning('LINE 推播失敗 (%s)：%s', status, body)
    return status == 200


def multicast_text(tenant_id, user_ids, text):
    """群發 (每次最多 500 人)；回傳成功送出的人數"""
    if not messaging_enabled(tenant_id):
        raise AppError('尚未啟用 LINE 官方帳號推播 (系統設定 → LINE)')
    ids = [u for u in dict.fromkeys(user_ids) if u]
    sent = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            status, body = _post(MULTICAST_URL, headers=_auth(tenant_id),
                                 json={'to': chunk, 'messages': [{'type': 'text', 'text': text[:4900]}]})
        except requests.RequestException as exc:
            raise AppError(f'LINE 推播失敗：{exc}', 502)
        if status != 200:
            raise AppError(f"LINE 推播失敗 ({status})：{body.get('message') or body}", 502)
        sent += len(chunk)
    return sent


@settings_service.register_tester('line')
def _test(cfg):
    messages = []
    if cfg.get('messaging_access_token'):
        try:
            status, body = _get(BOT_INFO_URL, headers={'Authorization': f"Bearer {cfg['messaging_access_token']}"})
        except requests.RequestException as exc:
            raise AppError(f'無法連線到 LINE：{exc}')
        if status != 200:
            raise AppError(f"Messaging API 金鑰無效 ({status})：{body.get('message') or body}")
        messages.append(f"官方帳號：{body.get('displayName') or body.get('basicId') or '已連線'}")
    if cfg.get('login_channel_id'):
        messages.append('LINE Login Channel ID 已填寫 (實際登入需在 LINE Developers 設定 Callback URL：/auth/line/callback)')
    if not messages:
        raise AppError('請先填寫 Channel Access Token 或 LINE Login Channel ID')
    return '；'.join(messages)
