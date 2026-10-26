"""
點餐頁多語系：介面文字 (UI) 與菜單內容 (品名、說明、分類) 的英文 / 日文。
菜單內容在後台「菜單進階設定」填寫；沒有翻譯的品項顯示中文。語言存在 session (?lang=en 切換)。
"""
from flask import request, session

from app.models.menu import ALLERGENS, ALLERGENS_I18N, DIETS, DIETS_I18N
from app.services import settings_service

LANG_KEY = 'lang'
LANG_LABELS = {'zh': '中文', 'en': 'English', 'ja': '日本語'}

UI = {
    'my_orders': {'zh': '我的訂單', 'en': 'My orders', 'ja': '注文履歴'},
    'cart': {'zh': '購物車', 'en': 'Cart', 'ja': 'カート'},
    'logout': {'zh': '登出', 'en': 'Log out', 'ja': 'ログアウト'},
    'hot': {'zh': '熱門推薦', 'en': 'Popular', 'ja': '人気メニュー'},
    'tap_to_add': {'zh': '點擊卡片直接加入購物車', 'en': 'Tap a card to add it', 'ja': 'タップでカートに追加'},
    'search': {'zh': '搜尋餐點名稱...', 'en': 'Search menu...', 'ja': 'メニューを検索...'},
    'all_categories': {'zh': '所有分類', 'en': 'All categories', 'ja': 'すべてのカテゴリ'},
    'max_price': {'zh': '最高價格', 'en': 'Max price', 'ja': '上限価格'},
    'unlimited': {'zh': '不限', 'en': 'Any', 'ja': '指定なし'},
    'clear': {'zh': '清除', 'en': 'Clear', 'ja': 'クリア'},
    'sort_popular': {'zh': '🔥 熱門優先', 'en': '🔥 Popular first', 'ja': '🔥 人気順'},
    'sort_new': {'zh': '🆕 新品優先', 'en': '🆕 New first', 'ja': '🆕 新商品順'},
    'sort_discount': {'zh': '🏷️ 特價優先', 'en': '🏷️ On sale first', 'ja': '🏷️ セール順'},
    'price': {'zh': '價格', 'en': 'Price', 'ja': '価格'},
    'sold_out': {'zh': '已售完', 'en': 'Sold out', 'ja': '売り切れ'},
    'add_to_cart': {'zh': '加入購物車', 'en': 'Add to cart', 'ja': 'カートに追加'},
    'no_desc': {'zh': '無描述', 'en': '', 'ja': ''},
    'popular': {'zh': '熱門', 'en': 'Popular', 'ja': '人気'},
    'new': {'zh': '新品', 'en': 'New', 'ja': '新商品'},
    'sale': {'zh': '特價', 'en': 'Sale', 'ja': 'セール'},
    'cart_title': {'zh': '點餐清單購物車', 'en': 'Your order', 'ja': 'ご注文内容'},
    'subtotal': {'zh': '小計', 'en': 'Subtotal', 'ja': '小計'},
    'payable': {'zh': '應付金額', 'en': 'Total', 'ja': 'お支払い金額'},
    'dine_in': {'zh': '內用 (Dine-in)', 'en': 'Dine-in', 'ja': '店内'},
    'take_out': {'zh': '外帶 (Take-out)', 'en': 'Take-out', 'ja': '持ち帰り'},
    'confirm_before': {'zh': '下單前確認', 'en': 'Review order', 'ja': '注文内容を確認'},
    'confirm_title': {'zh': '訂單送出確認', 'en': 'Confirm your order', 'ja': 'ご注文の確認'},
    'confirm_send': {'zh': '確認送出訂單', 'en': 'Place order', 'ja': '注文する'},
    'cancel': {'zh': '取消', 'en': 'Cancel', 'ja': 'キャンセル'},
    'order_success': {'zh': '訂單送出成功！', 'en': 'Order placed!', 'ja': 'ご注文ありがとうございます'},
    'pickup_no': {'zh': '取餐編號', 'en': 'Pickup number', 'ja': 'お呼び出し番号'},
    'continue': {'zh': '繼續點餐 (Back to Menu)', 'en': 'Back to menu', 'ja': 'メニューに戻る'},
    'promotion': {'zh': '促銷活動', 'en': 'Promotion', 'ja': 'キャンペーン'},
    'service_charge': {'zh': '服務費', 'en': 'Service charge', 'ja': 'サービス料'},
    'member_discount': {'zh': '會員折扣', 'en': 'Member discount', 'ja': '会員割引'},
    'invoice': {'zh': '電子發票', 'en': 'e-Invoice', 'ja': '電子レシート'},
    'track': {'zh': '用手機掃描 QR code 查看出餐進度', 'en': 'Scan to track your order', 'ja': 'QRコードで調理状況を確認'},
    'allergens': {'zh': '過敏原', 'en': 'Allergens', 'ja': 'アレルゲン'},
    'not_now': {'zh': '目前不供應', 'en': 'Not available now', 'ja': '現在提供なし'},
    'gift': {'zh': '贈品', 'en': 'Free gift', 'ja': 'プレゼント'},
    'size': {'zh': '規格', 'en': 'Size', 'ja': 'サイズ'},
}


def available_languages():
    langs = settings_service.get('menu').get('languages') or []
    return ['zh'] + [lang for lang in ('en', 'ja') if lang in langs]


def current_language():
    """?lang=xx 切換 (只接受後台開啟的語言)，記在 session"""
    allowed = available_languages()
    asked = request.args.get('lang')
    if asked in allowed:
        session[LANG_KEY] = asked
    lang = session.get(LANG_KEY, 'zh')
    return lang if lang in allowed else 'zh'


def ui_strings(lang):
    return {k: (v.get(lang) if v.get(lang) is not None else v['zh']) for k, v in UI.items()}


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
