"""寄信 (後台「系統設定 → 寄信伺服器」)：定期報表與系統警示"""
import smtplib
import ssl
from email.message import EmailMessage

from app.errors import AppError
from app.services import settings_service


def is_configured(tenant_id=None):
    cfg = settings_service.get('smtp', tenant_id)
    return bool(cfg.get('host') and (cfg.get('sender') or cfg.get('username')))


class _PinnedMixin:
    """連線固定到已檢查過的 IP；TLS (SNI 與憑證驗證) 仍用原本的主機名稱 (smtplib 以 self._host 包 TLS)"""

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._pinned_ip or host, port, timeout)


class _PinnedSMTP(_PinnedMixin, smtplib.SMTP):
    pass


class _PinnedSMTPSSL(_PinnedMixin, smtplib.SMTP_SSL):
    pass


def _connect(cfg):
    """建立 SMTP 連線 (測試時以 monkeypatch 取代)"""
    from app.services.webhook_service import check_public_host
    from app.sync import runtime as sync_runtime
    # 線上端：不可拿寄信設定連內網；連線固定到檢查過的 IP (檢查後 DNS 被換成內網位址 (rebinding) 也無效)。
    # 門市主機連自己區網內的寄信伺服器是正常的，不檢查
    ip = check_public_host(cfg['host']) if sync_runtime.is_hub() else None
    port = int(cfg.get('port') or 587)
    if port == 465:
        server = _PinnedSMTPSSL(cfg['host'], port, timeout=20, context=ssl.create_default_context(), pinned_ip=ip)
    else:
        server = _PinnedSMTP(cfg['host'], port, timeout=20, pinned_ip=ip)
        if cfg.get('use_tls', True):
            server.starttls(context=ssl.create_default_context())
    if cfg.get('username'):
        server.login(cfg['username'], cfg.get('password') or '')
    return server


def send_mail(tenant_id, recipients, subject, body, attachments=()):
    """attachments：[(檔名, bytes, mime 類型)]；失敗拋 AppError"""
    cfg = settings_service.get('smtp', tenant_id)
    recipients = [r for r in (recipients if isinstance(recipients, (list, tuple)) else settings_service.split_lines(recipients)) if '@' in r]
    if not cfg.get('host'):
        raise AppError('尚未設定寄信伺服器 (SMTP)')
    if not recipients:
        raise AppError('沒有有效的收件者 Email')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = cfg.get('sender') or cfg.get('username')
    msg['To'] = ', '.join(recipients)
    msg.set_content(body)
    for filename, data, mime in attachments:
        maintype, _, subtype = (mime or 'application/octet-stream').partition('/')
        msg.add_attachment(data, maintype=maintype, subtype=subtype or 'octet-stream', filename=filename)
    try:
        server = _connect(cfg)
        try:
            server.send_message(msg)
        finally:
            server.quit()
    except (OSError, smtplib.SMTPException, ValueError) as exc:   # ValueError：'a..b' 這類主機名稱 smtplib 會丟 UnicodeError
        raise AppError(f'寄信失敗：{exc}')
    return len(recipients)


@settings_service.register_tester('smtp')
def _test(cfg):
    recipient = cfg.get('sender') or cfg.get('username')
    if not recipient or '@' not in recipient:
        raise AppError('請填寫寄件者 Email (測試信會寄給寄件者自己)')
    from flask import g
    send_mail(g.tenant_id, [recipient], '點餐系統：寄信測試', '這是一封測試信，收到代表寄信設定正確。')
    return f'已寄出測試信到 {recipient}'
