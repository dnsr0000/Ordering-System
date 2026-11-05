"""
顧客端多語系：介面文字 (UI，含 i18n_pages.PAGES 的其他顧客頁面) 與菜單內容 (品名、說明、分類) 的英文 / 日文。
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
    'clear_filters': {'zh': '清除', 'en': 'Clear', 'ja': 'クリア'},   # 不可叫 clear (Jinja 的 ui.clear 會取到 dict.clear)
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
    # ---- 第五輪補齊：點餐頁其餘固定文字 (橫幅、歡迎、常點、購物車、結帳、收據、對話框、未登入頁) ----
    'site_title': {'zh': '自助點餐系統', 'en': 'Self-ordering', 'ja': 'セルフオーダー'},
    'welcome_guest_a': {'zh': '歡迎光臨，', 'en': 'Welcome, ', 'ja': 'ようこそ、'},
    'guest': {'zh': '訪客', 'en': 'guest', 'ja': 'ゲスト'},
    'welcome_guest_b': {'zh': '！', 'en': '!', 'ja': 'さん！'},
    'welcome_back_b': {'zh': '！', 'en': '!', 'ja': 'さん！'},
    'welcome_back': {'zh': '歡迎回來，', 'en': 'Welcome back, ', 'ja': 'おかえりなさい、'},
    'points_label': {'zh': '紅利點數', 'en': 'Points', 'ja': 'ポイント'},
    'points_unit': {'zh': '點', 'en': 'pts', 'ja': 'pt'},
    'bind_line': {'zh': '綁定 LINE', 'en': 'Link LINE', 'ja': 'LINE 連携'},
    'privacy_prompt': {'zh': '請閱讀本店的', 'en': 'Please read our ', 'ja': '当店の'},
    'privacy_notice': {'zh': '隱私權告知', 'en': 'privacy notice', 'ja': 'プライバシー通知'},
    'privacy_suffix': {'zh': ' (會員資料的蒐集與使用方式)。', 'en': ' (how member data is collected and used).', 'ja': 'をお読みください (会員情報の取り扱い)。'},
    'privacy_agree': {'zh': '我已閱讀並同意', 'en': 'I have read and agree', 'ja': '読んで同意します'},
    'paused_title': {'zh': '暫停接單中', 'en': 'Not taking orders right now', 'ja': 'ただいま注文停止中'},
    'table_expired': {'zh': '桌位 QR code 已失效 (掃碼超過 4 小時、桌位已停用或已更換 QR code)，請重新掃描桌上的 QR code 後再點餐。',
                      'en': 'This table QR code has expired (scanned over 4 hours ago, table disabled or QR code replaced). Please scan the QR code on your table again.',
                      'ja': 'テーブルの QR コードが無効です (4 時間経過、テーブル停止、または QR 変更)。テーブルの QR コードをもう一度読み取ってください。'},
    'table_no': {'zh': '桌號', 'en': 'Table', 'ja': 'テーブル'},
    'table_banner': {'zh': '：內用，餐點完成後送到桌上；用餐後請至櫃檯結帳。', 'en': ': dine-in, food is brought to your table; please pay at the counter after your meal.',
                     'ja': '：店内でお召し上がり、料理はお席までお届けします。お食事後はレジでお支払いください。'},
    'call_water': {'zh': '加水 / 餐具', 'en': 'Water / cutlery', 'ja': 'お水 / カトラリー'},
    'call_bill': {'zh': '想結帳', 'en': 'Bill please', 'ja': 'お会計'},
    'call_help': {'zh': '呼叫服務', 'en': 'Call staff', 'ja': '店員を呼ぶ'},
    'table_pay_online': {'zh': '線上結帳', 'en': 'Pay online', 'ja': 'オンライン決済'},
    'no_online_nodes': {'zh': '目前沒有開放線上點餐的分店，請稍後再試或直接至門市點餐。', 'en': 'No branch is accepting online orders right now. Please try again later or order in store.',
                        'ja': '現在オンライン注文を受け付けている店舗がありません。後ほどお試しいただくか、店舗でご注文ください。'},
    'pickup_branch': {'zh': '取餐分店', 'en': 'Pickup branch', 'ja': '受取店舗'},
    'choose_branch': {'zh': '-- 請選擇取餐分店 --', 'en': '-- Choose a pickup branch --', 'ja': '-- 受取店舗を選択 --'},
    'change_branch': {'zh': '更換分店', 'en': 'Change branch', 'ja': '店舗を変更'},
    'ok': {'zh': '確定', 'en': 'OK', 'ja': '決定'},
    'current': {'zh': '目前：', 'en': 'Current: ', 'ja': '現在：'},
    'pickup_no_hint': {'zh': '取餐號碼於分店接單後產生，可在「我的訂單」查看。', 'en': 'Your pickup number is assigned when the branch accepts the order; see "My orders".',
                       'ja': 'お呼び出し番号は店舗が受注後に発行され、「注文履歴」で確認できます。'},
    'hero_guest_title': {'zh': '快速自助點餐', 'en': 'Quick self-ordering', 'ja': 'かんたんセルフオーダー'},
    'hero_guest_text': {'zh': '請自由挑選喜愛的餐點，加入購物車後即可快速結帳！', 'en': 'Pick your favourites, add them to the cart and check out in seconds!',
                        'ja': 'お好きな料理を選んでカートに入れ、すぐにお会計できます！'},
    'hero_member_title': {'zh': '快速點餐，會員專屬積點折抵', 'en': 'Quick ordering with member points', 'ja': '会員ポイントでお得に注文'},
    'hero_earn_a': {'zh': '消費每滿 $', 'en': 'Earn 1 point for every $', 'ja': '$'},
    'hero_earn_b': {'zh': ' 即贈送 1 點紅利', 'en': ' spent', 'ja': ' ごとに 1 ポイント進呈'},
    'hero_redeem_a': {'zh': '（', 'en': '(', 'ja': '（'},
    'hero_redeem_b': {'zh': ' 點 = $1）', 'en': ' pts = $1)', 'ja': ' pt = $1）'},
    'hero_max_discount': {'zh': '，單筆最高折抵 $', 'en': ', up to $', 'ja': '、1 回の注文で最大 $'},
    'hero_max_discount_b': {'zh': '', 'en': ' per order', 'ja': ' まで'},
    'hero_redeem_c': {'zh': '，結帳直接折現！', 'en': ', redeemed at checkout!', 'ja': '、お会計時に割引！'},
    'hero_mall': {'zh': '，累積點數可至紅利商城兌換專屬優惠！', 'en': '. Redeem your points for rewards in the rewards shop!', 'ja': '。貯めたポイントは特典ショップで交換できます！'},
    'choose_branch_first': {'zh': '請先在上方選擇取餐分店，即可查看該分店的菜單供應狀況並開始點餐。', 'en': 'Choose a pickup branch above to see its menu and start ordering.',
                            'ja': '上で受取店舗を選ぶと、その店舗のメニューと在庫が表示されます。'},
    'favorites_title': {'zh': '專屬於您的常點餐點', 'en': 'Your usual favourites', 'ja': 'いつものメニュー'},
    'favorites_hint': {'zh': '依您的歷史點餐偏好精選推薦', 'en': 'Based on your order history', 'ja': 'ご注文履歴からおすすめ'},
    'favorites_empty': {'zh': '您目前尚無點餐紀錄，完成首筆訂單後，系統將自動為您統計最常點選的專屬餐點！',
                        'en': 'No orders yet. After your first order we will show the dishes you order most.',
                        'ja': 'まだ注文履歴がありません。最初のご注文後、よく頼むメニューをここに表示します。'},
    'usual': {'zh': '常點', 'en': 'Usual', 'ja': 'いつもの'},
    'out_of_stock': {'zh': '缺貨中', 'en': 'Out of stock', 'ja': '在庫切れ'},
    'price_sort': {'zh': '價格 ⇅', 'en': 'Price ⇅', 'ja': '価格 ⇅'},
    'uncategorized': {'zh': '未分類', 'en': 'Other', 'ja': 'その他'},
    'cart_empty': {'zh': '購物車目前空空如也', 'en': 'Your cart is empty', 'ja': 'カートは空です'},
    'my_passes': {'zh': '我的寄杯', 'en': 'My prepaid cups', 'ja': 'マイ回数券'},
    'left_a': {'zh': '剩 ', 'en': '', 'ja': '残り '},
    'left_b': {'zh': ' 次', 'en': ' left', 'ja': ' 回'},
    'redeem_one': {'zh': '兌換一份', 'en': 'Redeem one', 'ja': '1 つ使う'},
    'promo_code': {'zh': '促銷優惠碼', 'en': 'Promo code', 'ja': 'クーポンコード'},
    'no_coupons': {'zh': '-- 尚無可用優惠券 --', 'en': '-- No coupons available --', 'ja': '-- 使えるクーポンはありません --'},
    'enter_promo': {'zh': '輸入促銷折扣碼', 'en': 'Enter promo code', 'ja': 'コードを入力'},
    'apply': {'zh': '套用', 'en': 'Apply', 'ja': '適用'},
    'points_redeem': {'zh': '紅利折抵', 'en': 'Redeem points', 'ja': 'ポイント利用'},
    'you_have': {'zh': '現有 ', 'en': 'You have ', 'ja': '保有 '},
    'use_points': {'zh': '使用點數', 'en': 'Use points', 'ja': 'ポイント使用'},
    'per_points_a': {'zh': '每 ', 'en': 'Every ', 'ja': ''},
    'per_points_b': {'zh': ' 點折 $1', 'en': ' pts = $1 off', 'ja': ' pt = $1 割引'},
    'max_per_order': {'zh': '・單筆最高折抵 $', 'en': ' · up to $', 'ja': '・1 回最大 $'},
    'coupon_discount': {'zh': '優惠券折扣', 'en': 'Coupon discount', 'ja': 'クーポン割引'},
    'delivery_fee': {'zh': '外送費', 'en': 'Delivery fee', 'ja': '配達料'},
    'eta_wait': {'zh': '預估等候', 'en': 'Estimated wait', 'ja': '待ち時間の目安'},
    'about': {'zh': '約 ', 'en': '~', 'ja': '約 '},
    'minutes': {'zh': ' 分鐘', 'en': ' min', 'ja': ' 分'},
    'calories_total': {'zh': '熱量合計 (約)', 'en': 'Total calories (approx.)', 'ja': 'カロリー合計 (目安)'},
    'kcal': {'zh': ' 大卡', 'en': ' kcal', 'ja': ' kcal'},
    'points_to_earn': {'zh': '預估獲得紅利', 'en': 'Points you will earn', 'ja': '獲得予定ポイント'},
    'table_dine_in': {'zh': ' 桌 • 內用 • 用餐後至櫃檯結帳', 'en': ' • dine-in • pay at the counter after your meal', 'ja': ' • 店内 • お食事後にレジでお支払い'},
    'home_delivery': {'zh': '外送到府', 'en': 'Home delivery', 'ja': '宅配'},
    'pay_cash': {'zh': '現金 Cash', 'en': 'Cash', 'ja': '現金'},
    'pay_card': {'zh': '信用卡', 'en': 'Credit card', 'ja': 'クレジットカード'},
    'pay_wallet': {'zh': '儲值金', 'en': 'Stored value', 'ja': 'チャージ残高'},
    'pickup_time': {'zh': '取餐時間', 'en': 'Pickup time', 'ja': '受取時間'},
    'asap': {'zh': '盡快 (付款後開始製作)', 'en': 'ASAP (prepared after payment)', 'ja': 'できるだけ早く (お支払い後に調理)'},
    'scheduled': {'zh': '預約 ', 'en': 'Scheduled ', 'ja': '予約 '},
    'min_spend': {'zh': '，滿 $', 'en': ', min. $', 'ja': '、$'},
    'min_spend_b': {'zh': '', 'en': '', 'ja': ' 以上'},
    'eta_delivery_a': {'zh': '預估 ', 'en': 'Estimated delivery in ', 'ja': 'お届けまで約 '},
    'eta_delivery_b': {'zh': ' 分鐘送達', 'en': ' min', 'ja': ' 分'},
    'free_over_a': {'zh': '；商品滿 $', 'en': '; free delivery over $', 'ja': '；$'},
    'free_over_b': {'zh': ' 免外送費', 'en': '', 'ja': ' 以上で配達料無料'},
    'need_cutlery': {'zh': '需要餐具', 'en': 'Cutlery needed', 'ja': 'カトラリー要'},
    'note': {'zh': '備註', 'en': 'Note', 'ja': '備考'},
    'note_placeholder': {'zh': '例如：不要太辣、請幫我裝袋、外送時間請盡快', 'en': 'e.g. not too spicy, please bag it', 'ja': '例：辛さ控えめ、袋に入れてください'},
    'combo_badge': {'zh': '組合設定', 'en': 'Combo', 'ja': 'セット'},
    'combo_title': {'zh': '請選擇享用方式', 'en': 'How would you like it?', 'ja': 'お召し上がり方を選択'},
    'combo_hint': {'zh': '點選卡片即可直接進入下一步選項（客製化 Modifiers 或加入購物車）：', 'en': 'Tap a card to continue (customize or add to cart):',
                   'ja': 'カードをタップして次へ (カスタマイズまたはカートに追加)：'},
    'customize': {'zh': '客製化選擇', 'en': 'Customize', 'ja': 'カスタマイズ'},
    'confirm_add': {'zh': '確認加入購物車', 'en': 'Add to cart', 'ja': 'カートに追加'},
    'close': {'zh': '關閉', 'en': 'Close', 'ja': '閉じる'},
    'items_subtotal': {'zh': '商品小計', 'en': 'Items subtotal', 'ja': '商品小計'},
    'promo_discount': {'zh': '促銷折扣', 'en': 'Promo discount', 'ja': 'クーポン割引'},
    'points_discount': {'zh': '點數折抵', 'en': 'Points discount', 'ja': 'ポイント割引'},
    'total_due': {'zh': '應付總額', 'en': 'Total due', 'ja': 'お支払い合計'},
    'eta_about': {'zh': '預估等候約 ', 'en': 'Estimated wait ~', 'ja': '待ち時間 約 '},
    'points_preview': {'zh': '結帳預估新增點數', 'en': 'Points to earn', 'ja': '獲得予定ポイント'},
    'invoice_default': {'zh': '紙本 / 依店家預設', 'en': 'Paper / store default', 'ja': '紙 / 店舗の既定'},
    'invoice_mobile': {'zh': '手機條碼載具', 'en': 'Mobile barcode carrier', 'ja': '携帯バーコード'},
    'invoice_company': {'zh': '打統一編號', 'en': 'Company tax ID', 'ja': '統一番号'},
    'invoice_donate': {'zh': '捐贈發票', 'en': 'Donate invoice', 'ja': '寄付'},
    'order_no': {'zh': '訂單編號', 'en': 'Order', 'ja': '注文'},
    'pickup_no_label': {'zh': '取餐編號：', 'en': 'Pickup #: ', 'ja': 'お呼び出し番号：'},
    'system_no': {'zh': '系統單號：', 'en': 'Order ID: ', 'ja': '注文 ID：'},
    'pickup_branch_label': {'zh': '取餐分店：', 'en': 'Pickup branch: ', 'ja': '受取店舗：'},
    'pending_hint': {'zh': '分店接單後會產生取餐號碼，請稍後於「我的訂單」查看。', 'en': 'The pickup number is assigned once the branch accepts the order; check "My orders" later.',
                     'ja': 'お呼び出し番号は店舗受注後に発行されます。「注文履歴」でご確認ください。'},
    'customer_name': {'zh': '顧客姓名：', 'en': 'Name: ', 'ja': 'お名前：'},
    'order_type': {'zh': '用餐方式：', 'en': 'Type: ', 'ja': '形式：'},
    'payment': {'zh': '付款方式：', 'en': 'Payment: ', 'ja': 'お支払い：'},
    'note_label': {'zh': '備註：', 'en': 'Note: ', 'ja': '備考：'},
    'delivery_label': {'zh': '外送到府：', 'en': 'Delivery: ', 'ja': '宅配：'},
    'scheduled_pickup': {'zh': '預約取餐：', 'en': 'Scheduled pickup: ', 'ja': '受取予約：'},
    'ready_at': {'zh': '預計完成：', 'en': 'Ready at about: ', 'ja': '完成予定：'},
    'order_items': {'zh': '點餐明細：', 'en': 'Items:', 'ja': '注文内容：'},
    'th_item': {'zh': '品項', 'en': 'Item', 'ja': '品名'},
    'th_qty': {'zh': '數量', 'en': 'Qty', 'ja': '数量'},
    'th_price': {'zh': '單價', 'en': 'Price', 'ja': '単価'},
    'th_subtotal': {'zh': '小計', 'en': 'Subtotal', 'ja': '小計'},
    'subtotal_label': {'zh': '小計：', 'en': 'Subtotal: ', 'ja': '小計：'},
    'total_discount': {'zh': '總折扣（促銷/點數）：', 'en': 'Total discount (promo / points): ', 'ja': '割引合計 (クーポン / ポイント)：'},
    'amount_paid': {'zh': '實付金額：', 'en': 'Amount paid: ', 'ja': 'お支払い金額：'},
    'points_earned': {'zh': '🌟 本次獲得紅利點數：', 'en': '🌟 Points earned: ', 'ja': '🌟 獲得ポイント：'},
    'points_balance': {'zh': '目前點數餘額：', 'en': 'Points balance: ', 'ja': 'ポイント残高：'},
    'table_bill': {'zh': '本桌帳單', 'en': 'Table bill', 'ja': 'テーブルのお会計'},
    'go_pay': {'zh': '前往付款', 'en': 'Pay now', 'ja': '支払いへ'},
    'order_history': {'zh': '我的歷史訂單記錄', 'en': 'My order history', 'ja': '注文履歴'},
    'landing_table': {'zh': '桌：請選擇以訪客或會員身分點餐，用餐後至櫃檯結帳。', 'en': ': order as a guest or member; pay at the counter after your meal.',
                      'ja': '：ゲストまたは会員としてご注文ください。お食事後はレジでお支払い。'},
    'you_are_at': {'zh': '您在 ', 'en': 'You are at table ', 'ja': 'テーブル '},
    'landing_title': {'zh': '歡迎使用自助點餐系統', 'en': 'Welcome to self-ordering', 'ja': 'セルフオーダーへようこそ'},
    'landing_text_a': {'zh': '加入會員可累積點數！消費滿 $', 'en': 'Join as a member to earn points! 1 point per $', 'ja': '会員登録でポイントが貯まります！$'},
    'landing_text_b': {'zh': ' 積 1 點', 'en': ' spent', 'ja': ' ごとに 1 pt'},
    'join_member': {'zh': '加入會員', 'en': 'Join', 'ja': '会員登録'},
    'member_login': {'zh': '會員登入', 'en': 'Member login', 'ja': '会員ログイン'},
    'line_login': {'zh': 'LINE 登入', 'en': 'Log in with LINE', 'ja': 'LINE でログイン'},
    'guest_order': {'zh': '訪客直接點餐', 'en': 'Order as guest', 'ja': 'ゲストで注文'},
    'group_order': {'zh': '團體預購 (多人一起訂、一起取餐)', 'en': 'Group pre-order (order together, pick up together)', 'ja': 'グループ予約注文 (まとめて注文・まとめて受取)'},
    # 前端 JS (我的訂單卡片 / 再點一次 / 提示) 也從 pageData.ui 取用
    'order_hash': {'zh': '訂單 #', 'en': 'Order #', 'ja': '注文 #'},
    'pickup_pending': {'zh': '取餐號：待分店確認', 'en': 'Pickup #: pending', 'ja': '番号：店舗確認待ち'},
    'pickup_hash': {'zh': '取餐 #', 'en': 'Pickup #', 'ja': '番号 #'},
    'eta_badge': {'zh': '預計 ', 'en': 'Ready ~', 'ja': '完成予定 '},
    'reorder': {'zh': '再點一次', 'en': 'Order again', 'ja': 'もう一度注文'},
    'paid_label': {'zh': '實付：$', 'en': 'Paid: $', 'ja': 'お支払い：$'},
    'discount_summary_a': {'zh': '總折扣折抵: -$', 'en': 'Discounts: -$', 'ja': '割引：-$'},
    'discount_summary_pts': {'zh': '(扣除 {n} 點)', 'en': '({n} pts used)', 'ja': '({n} pt 使用)'},
    'discount_summary_b': {'zh': ' | 獲得紅利: +', 'en': ' | Earned: +', 'ja': ' | 獲得：+'},
    'no_orders': {'zh': '目前尚無任何訂單紀錄！', 'en': 'No orders yet.', 'ja': 'まだ注文がありません。'},
    'reorder_added': {'zh': '已加入 {n} 份到購物車', 'en': 'Added {n} item(s) to your cart', 'ja': '{n} 点をカートに追加しました'},
    'reorder_none': {'zh': '沒有可加入的品項', 'en': 'Nothing could be added', 'ja': '追加できる品目がありません'},
    'reorder_skipped': {'zh': '略過 (已下架、售完或不適用)：', 'en': 'Skipped (unavailable or not applicable): ', 'ja': 'スキップ (販売終了・売切れ・対象外)：'},
    'pay_online': {'zh': '前往付款', 'en': 'Pay now', 'ja': '支払いへ'},
    'view_payment_info': {'zh': '查看繳費資訊', 'en': 'Payment details', 'ja': '支払い情報'},
    'online_member_hint': {'zh': '線上點餐僅限會員：請以手機號碼 + PIN 登入，或先加入會員。', 'en': 'Online ordering is for members only: log in with your phone number + PIN, or join first.',
                           'ja': 'オンライン注文は会員限定です：電話番号 + PIN でログインするか、会員登録してください。'},
    'privacy_link': {'zh': '隱私權告知', 'en': 'Privacy notice', 'ja': 'プライバシー通知'},
    'admin_hint': {'zh': '點擊後台管理可進入店家管理', 'en': 'Staff: open the admin console below', 'ja': 'スタッフ：下の管理画面から'},
    'admin_link': {'zh': '後台管理', 'en': 'Admin', 'ja': '管理画面'},
    'a11y': {'zh': '大字 / 高對比', 'en': 'Large text / high contrast', 'ja': '大きな文字 / 高コントラスト'},
    'rewards_store_title': {'zh': '點擊進入紅利回饋商店', 'en': 'Open the rewards shop', 'ja': '特典ショップを開く'},
    'delivery_address': {'zh': '外送地址', 'en': 'Delivery address', 'ja': '配達先住所'},
    'contact_phone': {'zh': '聯絡電話', 'en': 'Contact phone', 'ja': '連絡先電話'},
    'delivery_zone': {'zh': '配送區域', 'en': 'Delivery zone', 'ja': '配達エリア'},
    'invoice_type': {'zh': '發票類型', 'en': 'Invoice type', 'ja': 'レシート種類'},
    'invoice_value': {'zh': '載具 / 統編 / 愛心碼', 'en': 'Carrier / tax ID / donation code', 'ja': 'キャリア / 番号 / 寄付コード'},
    'calories': {'zh': '熱量', 'en': 'Calories', 'ja': 'カロリー'},
    'a11y_short': {'zh': '大字', 'en': 'Aa', 'ja': '大きく'},
    'delivery_address_ph': {'zh': '外送地址 (含樓層)', 'en': 'Delivery address (with floor)', 'ja': '配達先住所 (階数も)'},
    # 點餐機 JS (套餐 / 客製化視窗)：選項的值 (正常冰、半糖…) 是送到廚房的文字，維持中文，只翻顯示的標籤
    'enjoy_how': {'zh': '享用方式', 'en': 'options', 'ja': 'お召し上がり方'},
    'single_main': {'zh': '單點主餐', 'en': 'Single item', 'ja': '単品'},
    'single_suffix': {'zh': ' (單點)', 'en': ' (single)', 'ja': ' (単品)'},
    'single_desc': {'zh': '僅單點本品項，不搭配任何附餐或飲品。', 'en': 'Just this item, without sides or drinks.', 'ja': 'この品のみ。サイドやドリンクは付きません。'},
    'choose_single': {'zh': '選擇單點', 'en': 'Choose single', 'ja': '単品を選ぶ'},
    'combo_sold_out': {'zh': '配餐售罄', 'en': 'Side sold out', 'ja': 'サイド売切れ'},
    'combo_upgrade': {'zh': '升級超值套餐', 'en': 'Upgrade to combo', 'ja': 'セットにアップグレード'},
    'combo_total': {'zh': '總價 $', 'en': 'total $', 'ja': '合計 $'},
    'combo_desc_default': {'zh': '含特選附餐與指定飲品組合', 'en': 'Includes selected sides and a drink', 'ja': 'サイドとドリンク付き'},
    'combo_side_unavailable': {'zh': '配餐缺貨中', 'en': 'Side unavailable', 'ja': 'サイド在庫切れ'},
    'upgrade_combo': {'zh': '升級套餐', 'en': 'Make it a combo', 'ja': 'セットにする'},
    'ice': {'zh': '冰塊量', 'en': 'Ice', 'ja': '氷の量'},
    'sugar': {'zh': '甜度', 'en': 'Sweetness', 'ja': '甘さ'},
    'spicy': {'zh': '辣度調整', 'en': 'Spiciness', 'ja': '辛さ'},
    'addons': {'zh': '加料升級', 'en': 'Add-ons', 'ja': 'トッピング'},
    'addon_customize': {'zh': '加購客製化 - ', 'en': 'Add-on options - ', 'ja': '追加のカスタマイズ - '},
    'customize_for': {'zh': '客製化選擇 - ', 'en': 'Customize - ', 'ja': 'カスタマイズ - '},
    'combo_prefix': {'zh': '[套餐: ', 'en': '[combo: ', 'ja': '[セット: '},
    'addon_prefix': {'zh': '加購：', 'en': 'Add-on: ', 'ja': '追加：'},
    'main_prefix': {'zh': '主餐：', 'en': 'Main: ', 'ja': 'メイン：'},
    'side_prefix': {'zh': '配餐：', 'en': 'Side: ', 'ja': 'サイド：'},
    'pick_one': {'zh': ' (任選一項)', 'en': ' (pick one)', 'ja': ' (1 つ選択)'},
    'sold_out_paren': {'zh': ' (售完)', 'en': ' (sold out)', 'ja': ' (売切れ)'},
    'customize_suffix': {'zh': ' 客製化', 'en': ' options', 'ja': ' のカスタマイズ'},
    'price_asc': {'zh': '價格 ↑ (低到高)', 'en': 'Price ↑ (low to high)', 'ja': '価格 ↑ (安い順)'},
    'price_desc': {'zh': '價格 ↓ (高到低)', 'en': 'Price ↓ (high to low)', 'ja': '価格 ↓ (高い順)'},
    'ice_values': {'zh': '正常冰|少冰|去冰|熱飲', 'en': 'Regular ice|Less ice|No ice|Hot', 'ja': '氷普通|氷少なめ|氷なし|ホット'},
    'sugar_values': {'zh': '全糖|半糖|微糖|無糖', 'en': 'Full sugar|Half sugar|Light sugar|No sugar', 'ja': '甘さ普通|半分|少なめ|無糖'},
    'spicy_values': {'zh': '不辣|微辣|中辣|大辣', 'en': 'Not spicy|Mild|Medium|Hot', 'ja': '辛くない|少し辛い|中辛|大辛'},
}


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


for _key, _zh, _en, _ja in PAGES:
    assert _key not in UI, f'i18n 鍵重複：{_key}'
    UI[_key] = {'zh': _zh, 'en': _en, 'ja': _ja}

# 員工頁面 (後台 / POS / 廚房 / 取餐 / 看板…) 一律中文；其餘 (顧客頁) 依 session 語言
STAFF_BLUEPRINTS = frozenset({
    'admin', 'pos', 'kitchen', 'pickup', 'orders', 'sync_admin', 'settings', 'settings_admin', 'invoices', 'refunds', 'shifts',
    'printing', 'menu_pro', 'inventory', 'members_pro', 'promotions', 'reports', 'timeclock', 'system', 'security', 'feedback',
    'gift_cards', 'purchasing', 'live', 'dispatch', 'passes', 'branches', 'marketing', 'integrations', 'devices', 'archive',
    'pnl', 'audit', 'tenants', 'api_v1', 'sync_api', 'delivery', 'health',
})
_ICON_RE = re.compile(r'^(✅|❌|⚠️|ℹ️|🔔|🖨️)\s*')


def ui_strings(lang):
    return {k: (v.get(lang) if v.get(lang) is not None else v['zh']) for k, v in UI.items()}


def effective_language():
    """目前請求要用的語言：員工頁面固定 zh；顧客頁依 current_language()"""
    if request.blueprint in STAFF_BLUEPRINTS:
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
    hit = MESSAGES.get(body) or MESSAGES.get(text)
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
