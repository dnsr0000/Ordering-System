"""
顧客端多語系：介面文字 (全部在 i18n_pages.PAGES) 與菜單內容 (品名、說明、分類) 的英文 / 日文。
菜單內容在後台「菜單進階設定」填寫；沒有翻譯的品項顯示中文。語言存在 session (?lang=en 切換；第一次依瀏覽器語言)。
只翻顧客看的頁面：後台 / POS / 廚房一律中文 (effective_language 對員工頁面固定回 zh)。
後端訊息 (AppError / flash) 維持中文，顯示時以 translate_message 對照 (i18n_pages.MESSAGES / MESSAGE_PATTERNS)。
"""
import re

from flask import g, request, session

from app.services.i18n_pages import MESSAGES, MESSAGE_PATTERNS, PAGES

from app.models.menu import ALLERGENS, ALLERGENS_I18N, DIETS, DIETS_I18N
from app.services import settings_service

LANG_KEY = 'lang'
LANG_LABELS = {'zh': '中文', 'en': 'English', 'ja': '日本語'}

# 所有顧客端字串都在 i18n_pages.PAGES；這裡組成 UI[鍵] = {zh, en, ja} 供樣板 (ui.<鍵>) 與前端 (ui-data) 使用
UI = {key: {'zh': zh, 'en': en, 'ja': ja} for key, zh, en, ja in PAGES}


def available_languages():
    if getattr(g, 'tenant_id', None) is None:   # 尚未解析店家 (例如 CSRF 錯誤頁)：只有中文
        return ['zh']
    langs = settings_service.get('menu').get('languages') or []
    return ['zh'] + [lang for lang in ('en', 'ja') if lang in langs]


def current_language():
    """?lang=xx 切換 (只接受後台開啟的語言)，記在 session；第一次進站依瀏覽器語言 (Accept-Language) 選"""
    allowed = available_languages()
    asked = request.args.get('lang')
    if asked in allowed:
        session[LANG_KEY] = asked
    lang = session.get(LANG_KEY)
    if lang is None:
        lang = _browser_language(allowed)
        if lang != 'zh':
            session[LANG_KEY] = lang
    return lang if lang in allowed else 'zh'


def _browser_language(allowed):
    """Accept-Language 裡第一個本店有開的語言 (中文一律 zh)；都沒有就 zh"""
    for code, _q in request.accept_languages:
        base = (code or '').split('-')[0].lower()
        if base == 'zh':
            return 'zh'
        if base in allowed:
            return base
    return 'zh'


# 員工頁面 (後台 / POS / 廚房 / 取餐 / 看板…) 一律中文；其餘 (顧客頁) 依 session 語言
STAFF_BLUEPRINTS = frozenset({
    'admin', 'pos', 'kitchen', 'pickup', 'orders', 'sync_admin', 'settings', 'settings_admin', 'invoices', 'refunds', 'shifts',
    'printing', 'menu_pro', 'inventory', 'members_pro', 'promotions', 'reports', 'timeclock', 'system', 'security', 'feedback',
    'support',
    'gift_cards', 'purchasing', 'live', 'dispatch', 'passes', 'branches', 'marketing', 'integrations', 'devices', 'archive',
    'pnl', 'audit', 'tenants', 'api_v1', 'sync_api', 'delivery', 'health',
})
# 同一 blueprint 兼有顧客頁 (掃碼點餐、訂位、候位、隱私權、團購) 與員工頁的：員工頁另列 endpoint
STAFF_ENDPOINTS = frozenset({
    'tables.manage', 'tables.save_layout', 'tables.print_cards', 'reservations.board', 'privacy.settings',
    'group_orders.admin',
})
_ICON_RE = re.compile(r'^(✅|❌|⚠️|ℹ️|🔔|🖨️)\s*')
# 帶圖示的 MESSAGES 鍵也能以去掉圖示的本文查到 (flash 顯示時 flash_text 會先去掉開頭圖示再交給 tr_msg)
_MESSAGES_BODY = {_ICON_RE.sub('', k): v for k, v in MESSAGES.items() if _ICON_RE.match(k)}


def ui_strings(lang):
    return {k: (v.get(lang) if v.get(lang) is not None else v['zh']) for k, v in UI.items()}


def effective_language():
    """目前請求要用的語言：員工頁面固定 zh；顧客頁依 current_language()"""
    if request.blueprint in STAFF_BLUEPRINTS or request.endpoint in STAFF_ENDPOINTS:
        return 'zh'
    return current_language()


def translate_message(text, lang=None):
    """後端的中文訊息 (AppError / flash) → 目前語言；沒有對照時原樣回傳。開頭的圖示保留。"""
    lang = lang or effective_language()
    text = str(text or '')
    if lang == 'zh' or not text:
        return text
    icon = ''
    m = _ICON_RE.match(text)
    body = text
    if m:
        icon, body = m.group(0), text[m.end():]
    hit = MESSAGES.get(body) or MESSAGES.get(text) or _MESSAGES_BODY.get(body)
    if hit:
        return icon + hit[0 if lang == 'en' else 1]
    for pattern, (en, ja) in MESSAGE_PATTERNS:
        pm = pattern.match(body)
        if pm:
            groups = {k: v or '' for k, v in pm.groupdict().items()}
            if callable(en):
                return icon + en(lang, **groups)
            return icon + (en if lang == 'en' else ja).format(**groups)
    return text


def install(app):
    """所有樣板都拿得到 ui / lang / langs / lang_labels；flash 訊息以 tr_msg 過濾器翻譯"""
    @app.context_processor
    def _inject_i18n():
        if not hasattr(g, '_i18n'):
            lang = effective_language()
            g._i18n = {'ui': ui_strings(lang), 'lang': lang, 'langs': available_languages(), 'lang_labels': LANG_LABELS}
        return g._i18n

    @app.template_filter('tr_msg')
    def _tr_msg(text):
        return translate_message(text)


def allergen_labels(lang):
    if lang in ALLERGENS_I18N:
        return ALLERGENS_I18N[lang]
    return ALLERGENS


def diet_labels(lang):
    if lang in DIETS_I18N:
        return DIETS_I18N[lang]
    return DIETS


def category_labels(lang):
    from app.models.menu import CategoryTranslation
    if lang not in ('en', 'ja'):
        return {}
    return {t.category: getattr(t, f'name_{lang}') for t in CategoryTranslation.query.all() if getattr(t, f'name_{lang}')}
