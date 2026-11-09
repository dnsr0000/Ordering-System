"""
店家設定 (後台「系統設定」)。每個區塊一個 key，欄位定義集中在 SECTIONS，後台頁面依定義自動產生表單：
- 金鑰類欄位 (secret) 不會回傳到瀏覽器；表單留空 = 保持原值，勾選「清除」才會刪除。
- 讀取：get('einvoice') 回傳合併預設值後的 dict (同一個請求內快取)。
- 外部服務的「測試連線」由各服務在 TESTERS 註冊。
- 總部統一設定：不含金鑰的區塊 (hq_managed) 在線上端儲存時同步寫入 HqSetting 並下發到各分店；
  分店沒有自己的設定時使用總部的值，在分店後台儲存 = 本店自訂 (覆寫總部)，可隨時「改回總部設定」。
  含金鑰的區塊 (電子發票、LINE Pay…) 與本機性質的區塊 (自動備份) 一律各台主機自己設定。
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from flask import g, has_request_context

from app.errors import AppError
from app.extensions import db
from app.models.settings import HqSetting, StoreSetting
from app.utils import safe_float, safe_int

TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
ZERO_WIDTH_RE = re.compile('[​‌‍⁠﻿]')


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = 'text'          # text / secret / bool / int / float / select / multi / textarea / time / url / email
    default: object = ''
    options: tuple = ()         # select / multi：((值, 顯示文字), ...)
    help: str = ''
    placeholder: str = ''
    min: int | None = None
    max: int | None = None
    max_len: int | None = None  # 文字欄位長度上限 (None = 多行 2000 / 單行 300 字)；超過時拒絕儲存，不靜默截斷


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    group: str
    icon: str
    description: str
    fields: tuple
    enabled_field: str = ''      # 用來判斷「已啟用」的欄位
    enabled_fields: tuple = ()   # 有多個子功能時：任一欄位有值 (且不是 'none') 就算已啟用；空白 = 只看 enabled_field
    docs: tuple = field(default_factory=tuple)   # ((說明, 網址), ...)
    subgroup: str = ''           # 系統設定頁左欄：同一群組內的子群組標題 (只影響版面)
    keywords: str = ''           # 系統設定頁搜尋用的額外關鍵字 (標題沒出現、但使用者會這樣找的字)


ONLINE_PAYMENT_CHOICES = (('Credit', '信用卡'), ('ApplePay', 'Apple Pay'), ('ATM', 'ATM 轉帳 (虛擬帳號)'),
                          ('CVS', '超商代碼繳費'))
PLATFORM_CODE_HELP = '同一台主機有兩家以上店家啟用此平台時必填，否則平台訂單無法判斷店家而全部被拒收'
PAYMENT_CHOICES = (('Cash', '現金'), ('Credit Card', '信用卡 (刷卡機)'), ('Line Pay', 'LINE Pay'),
                   ('街口支付', '街口支付 (記帳)'), ('Apple Pay', 'Apple Pay (記帳)'), ('儲值金', '會員儲值金'))

SECTIONS = {s.key: s for s in (
    # ------------------------------------------------------------------ A 金流與法規
    Section('einvoice', '電子發票 (綠界加值中心)', 'A 金流與法規', 'bi-receipt',
            '結帳時開立 B2C 電子發票；取消訂單自動作廢 (跨期改開折讓)、部分退款開折讓。測試環境可先用綠界公開的測試帳號。',
            (
                Field('enabled', '啟用電子發票', 'bool', False),
                Field('stage', '使用綠界測試環境', 'bool', True, help='正式開立前請取消勾選並填入正式特店資料'),
                Field('merchant_id', '特店編號 MerchantID', placeholder='測試環境留空 = 2000132'),
                Field('hash_key', 'HashKey', 'secret', placeholder='測試環境留空 = 綠界公開測試金鑰'),
                Field('hash_iv', 'HashIV', 'secret'),
                Field('auto_issue', '結帳後自動開立', 'bool', True, help='關閉時改由後台「電子發票」頁面手動開立'),
                Field('default_carrier', '顧客未選擇時', 'select', 'print',
                      (('print', '開立紙本證明聯'), ('donate', '捐贈給預設愛心碼'))),
                Field('default_love_code', '預設捐贈愛心碼', placeholder='例如 168001 (財團法人...)'),
                Field('void_on_cancel', '取消訂單時自動作廢 / 折讓', 'bool', True),
                Field('customer_addr', '紙本證明聯的買受人地址', default='門市現場消費', help='綠界規定列印紙本時地址必填'),
                Field('fallback_email', '發票通知 Email (顧客未提供時)', 'email', help='開立、作廢、折讓通知會寄到這個信箱'),
                Field('item_tax_type', '課稅別', 'select', '1', (('1', '應稅'), ('3', '免稅'))),
            ), enabled_field='enabled',
            docs=(('綠界 B2C 電子發票 API', 'https://developers.ecpay.com.tw/?p=7809'),),
            subgroup='發票與退款', keywords='綠界 發票 統編 載具'),
    Section('linepay', 'LINE Pay (掃顧客付款碼)', 'A 金流與法規', 'bi-phone',
            '櫃檯以掃描器掃顧客 LINE Pay App 的付款條碼完成扣款；退款也會自動退回 LINE Pay。',
            (
                Field('enabled', '啟用 LINE Pay 串接', 'bool', False,
                      help='未啟用時 POS 選 LINE Pay 只記帳 (自行用其他設備收款)'),
                Field('sandbox', '使用 Sandbox 測試環境', 'bool', True),
                Field('channel_id', 'Channel ID'),
                Field('channel_secret', 'Channel Secret Key', 'secret'),
                Field('device_profile_id', '收銀機識別 (X-LINE-MerchantDeviceProfileId)', placeholder='選填'),
                Field('product_name', '交易名稱', default='餐點'),
            ), enabled_field='enabled',
            docs=(('LINE Pay Offline API', 'https://pay.line.me/tw/developers/apis/onlineApis?locale=zh_TW'),),
            subgroup='收款', keywords='LINE Pay 行動支付'),
    Section('card_terminal', '信用卡刷卡機', 'A 金流與法規', 'bi-credit-card',
            '使用銀行提供的刷卡機刷卡後，在 POS 輸入授權碼與卡號末四碼記帳 (方便對帳與退款)。',
            (
                Field('require_auth_code', '必須輸入授權碼', 'bool', True),
                Field('require_last4', '必須輸入卡號末四碼', 'bool', False),
                Field('terminal_name', '刷卡機 / 收單銀行名稱', placeholder='例如：聯合信用卡中心 EDC'),
            ),
            subgroup='收款', keywords='刷卡 信用卡'),
    Section('payments', '付款方式', 'A 金流與法規', 'bi-wallet2',
            '各點餐管道可選擇的付款方式。',
            (
                Field('pos_methods', 'POS 收銀', 'multi', ['Cash', 'Credit Card', 'Line Pay', '街口支付'], PAYMENT_CHOICES),
                Field('kiosk_methods', '自助點餐機 / 手機點餐', 'multi', ['Cash', 'Credit Card', 'Line Pay', '街口支付'],
                      PAYMENT_CHOICES, help='點餐機的非現金付款仍需至櫃檯完成 (記帳用)'),
                Field('online_methods', '線上點餐 (綠界) 的付款方式', 'multi', ['Credit'], ONLINE_PAYMENT_CHOICES,
                      help='線上端適用。ATM / 超商代碼為非即時付款：顧客取得繳費資訊後於期限內繳費，付款完成才送到分店'),
                Field('atm_expire_days', 'ATM 轉帳繳費期限 (天)', 'int', 1, min=1, max=60),
                Field('cvs_expire_minutes', '超商代碼繳費期限 (分鐘)', 'int', 120, min=30, max=43200),
                Field('ecpay_auto_refund', '取消已付款的線上訂單時自動向綠界退刷', 'bool', False,
                      help='僅信用卡 / Apple Pay；綠界測試環境不支援退刷 API，正式環境才會成功。失敗時在「作廢與退款」頁重試或人工退款'),
            ),
            subgroup='收款', keywords='付款 現金 支付'),
    Section('refund', '作廢與退款', 'A 金流與法規', 'bi-arrow-counterclockwise',
            '作廢 (整單取消) 與部分退款的規則；沒有「作廢 / 退款」權限的員工需主管輸入帳號密碼核准。',
            (
                Field('void_requires_approval', '員工作廢已收款訂單需主管核准', 'bool', True),
                Field('require_reason', '部分退款必須填寫原因', 'bool', True),
                Field('allow_partial', '允許部分退款', 'bool', True),
                Field('restock_on_cancel', '作廢時回補庫存', 'bool', True),
                Field('restock_picked_up', '已取餐的訂單作廢時也回補庫存', 'bool', False,
                      help='已被顧客帶走的現做餐點不應回補；只賣預包裝商品、退貨會放回貨架的店可開啟'),
            ),
            subgroup='發票與退款', keywords='退款 作廢'),
    # ------------------------------------------------------------------ B 收銀營運
    Section('pos', '收銀規則', 'B 收銀營運', 'bi-calculator',
            '服務費、折扣與改價授權、開班與列印。',
            (
                Field('service_charge_percent', '服務費 (%)', 'int', 0, min=0, max=30, help='0 = 不收服務費'),
                Field('service_charge_scope', '收服務費的訂單', 'multi', ['pos_dine_in', 'table'],
                      (('pos_dine_in', 'POS 內用'), ('table', '桌邊掃碼點餐'), ('kiosk_dine_in', '點餐機內用'))),
                Field('discount_approval_percent', '整單折扣超過多少 % 需主管核准', 'int', 0, min=0, max=100,
                      help='0 = 沒有「折扣」權限的員工任何折扣都需核准'),
                Field('allow_price_override', '允許收銀改價 (需權限或主管核准)', 'bool', True),
                Field('require_shift', '必須先開班才能結帳', 'bool', False),
                Field('auto_print_receipt', '結帳後自動列印收據 (網路印表機)', 'bool', False),
                Field('auto_print_kitchen', '結帳後自動列印廚房單', 'bool', True),
                Field('open_drawer_on_cash', '收 / 退現金時自動開錢櫃 (需在印表機勾選「接錢櫃」)', 'bool', True),
                Field('display_message', '客顯螢幕待機標語', default='歡迎光臨',
                      help='POS「客顯」視窗 (第二螢幕) 沒有點餐時顯示的文字'),
                Field('receipt_footer', '收據頁尾文字', default='謝謝光臨，請憑取餐號碼取餐！'),
                Field('quick_notes', '常用備註快捷鍵 (逗號分隔)', default='少油, 少鹽, 不要蔥, 分開裝, 餐具 2 份',
                      help='POS 備註欄上方的快捷按鈕，按一下就加進整單備註'),
                Field('quick_tables', '桌號 / 顧客稱呼快捷鍵 (逗號分隔)', default='外帶客, 電話預訂',
                      help='POS「桌號或顧客稱呼」欄位上方的快速填入標籤；桌位管理裡啟用的桌號會自動排在前面，'
                           '這裡填的是桌號以外的稱呼 (例如 外帶客、電話預訂、外送平台)。按「外帶客」會同時切成外帶'),
            ),
            keywords='收銀 服務費 折扣 開班 收據 桌號'),
    # ------------------------------------------------------------------ C 廚房出餐
    Section('kds', '廚房看板', 'C 廚房出餐', 'bi-fire',
            '出餐計時與逾時警示、工作站完成規則。',
            (
                Field('warn_minutes', '超過幾分鐘顯示黃色', 'int', 8, min=1, max=120),
                Field('late_minutes', '超過幾分鐘顯示紅色 (逾時)', 'int', 15, min=1, max=240),
                Field('sound', '新訂單提示音', 'bool', True),
                Field('eta_enabled', '顯示預估等候時間 (點餐頁、收據、出餐進度)', 'bool', True,
                      help='依「製作中」訂單的工作量與餐點製作時間估算 (菜單進階設定可設定各餐點的製作時間)'),
                Field('eta_base_minutes', '基本等候 (分鐘)', 'int', 3, min=0, max=60, help='沒有排隊時的最少等候時間 (裝盤、叫號)'),
                Field('eta_default_prep', '餐點預設製作時間 (分鐘)', 'int', 5, min=1, max=120, help='餐點未設定製作時間時使用'),
                Field('eta_parallel', '廚房同時製作幾張訂單', 'int', 3, min=1, max=50, help='排隊訂單的工作量會除以這個數'),
            ),
            subgroup='廚房', keywords='廚房 看板 逾時'),
    Section('order_intake', '接單控制', 'C 廚房出餐', 'bi-pause-circle',
            '暫停接單 (POS 右上角一鍵切換) 與每時段訂單上限：只擋點餐機、顧客手機 (桌邊 / 線上) 與團購的手動送單，'
            'POS 櫃檯、外送平台訂單與團購排程自動送單不受影響。分店的暫停狀態會隨同步回報線上端，線上點餐選到該分店時一併擋下。',
            (
                # 暫停狀態由 POS 隨時切換：這裡只顯示目前實際狀態，儲存此頁不會變更 (避免用頁面載入時的舊值蓋掉 POS 剛切換的狀態)
                Field('paused', '目前暫停接單中 (僅顯示，請在 POS 右上角切換)', 'bool', False,
                      help='此頁儲存不會變更暫停狀態；暫停 / 恢復與自動恢復時間請在 POS 設定'),
                Field('resume_at', '自動恢復時間 (僅顯示，由 POS 設定；空白 = 手動恢復)'),
                Field('message', '暫停時顯示給顧客的訊息', default='目前訂單較多，暫停接單中，請稍後再試或洽櫃檯。'),
                Field('window_minutes', '時段長度 (分鐘)', 'int', 15, min=5, max=120),
                Field('max_orders_per_window', '每時段最多接幾張訂單 (0 = 不限)', 'int', 0, min=0, max=1000,
                      help='盡快取餐的訂單以下單時間計算；預約取餐以取餐時間所在的時段計算'),
            ),
            subgroup='廚房', keywords='暫停接單 上限'),
    Section('notify', '取餐通知', 'C 廚房出餐', 'bi-megaphone',
            '叫號看板語音、顧客手機追蹤頁、LINE 推播。',
            (
                Field('voice_enabled', '叫號看板語音播報', 'bool', True),
                Field('voice_template', '播報內容', default='取餐號碼 {number}，請到櫃檯取餐', help='{number} 會換成取餐號'),
                Field('voice_repeat', '重複次數', 'int', 2, min=1, max=5),
                Field('line_ready', '餐點完成時 LINE 通知綁定的會員', 'bool', False, help='需先設定 LINE Messaging API'),
                Field('line_ready_template', 'LINE 通知內容', default='{shop}：您的餐點 (取餐號 {number}) 已完成，請至櫃檯取餐！'),
            ),
            subgroup='顧客通知', keywords='叫號 語音 取餐'),
    Section('receipt', '電子收據寄送', 'C 廚房出餐', 'bi-envelope-paper',
            '結帳後顧客可在收據視窗把明細寄到 Email (需設定 SMTP) 或傳到已綁定的 LINE；也可結帳時自動傳給 LINE 綁定會員。',
            (
                Field('email_enabled', 'Email 電子收據', 'bool', False, help='需先設定「寄信伺服器 (SMTP)」'),
                Field('line_enabled', 'LINE 電子收據', 'bool', False, help='需先設定 LINE Messaging API；只有綁定 LINE 的會員'),
                Field('auto_line', '結帳時自動傳給 LINE 綁定會員', 'bool', False),
                Field('footer', '收據結尾文字', default='感謝您的惠顧'),
            ),
            subgroup='顧客通知', keywords='電子收據 email'),
    # ------------------------------------------------------------------ D 菜單
    Section('push', '手機推播通知 (Web Push)', 'C 廚房出餐', 'bi-phone-vibrate',
            '顧客在出餐進度頁 (收據 QR code) 按「開啟完成通知」後，餐點完成 / 外送出發時推播到手機，離開頁面也收得到。'
            '只在 HTTPS 網址有效 (門市 Wi-Fi 以 http://IP 開啟時退回頁面內通知)；啟用後第一次訂閱時自動產生 VAPID 金鑰。',
            (
                Field('enabled', '啟用 Web Push', 'bool', False),
                Field('contact_email', '聯絡 Email (VAPID sub，推播服務商聯絡用)', 'email', placeholder='admin@example.com'),
                Field('vapid_public_key', 'VAPID 公鑰 (自動產生)', placeholder='留空 = 第一次訂閱時自動產生'),
                Field('vapid_private_key', 'VAPID 私鑰 (自動產生)', 'secret'),
            ), enabled_field='enabled',
            subgroup='顧客通知', keywords='推播 web push'),
    Section('menu', '菜單顯示', 'D 菜單', 'bi-translate',
            '點餐頁的語言與標示。',
            (
                Field('languages', '提供的語言', 'multi', ['en', 'ja'], (('en', 'English'), ('ja', '日本語'))),
                Field('show_allergens', '顯示過敏原與素食標示', 'bool', True),
                Field('show_calories', '顯示熱量與營養標示', 'bool', True,
                      help='餐點的熱量 / 營養在「菜單進階設定」填寫；購物車顯示合計熱量'),
                Field('show_carbon', '顯示碳足跡 (參考值)', 'bool', True,
                      help='餐點碳足跡依配方 (原料用量 × 排放係數) 自動估算，或在「菜單進階設定」手動填寫；'
                           '點餐頁以「參考值」標示，購物車顯示合計'),
                Field('attract_seconds', '自助機待機輪播 (閒置秒數)', 'int', 60, min=0, max=3600,
                      help='點餐機首頁閒置多久後全螢幕輪播推薦餐點 (0 = 不輪播)；觸碰螢幕即回到首頁'),
                Field('attract_message', '待機輪播標語', default='觸碰螢幕開始點餐'),
                Field('accessibility_button', '顯示「大字 / 高對比」切換按鈕', 'bool', True),
                Field('voice_ordering', '語音點餐 (點餐頁麥克風按鈕)', 'bool', True,
                      help='顧客用說的點餐：瀏覽器語音辨識 (需 HTTPS 或本機網址) → 先以菜單規則解析，聽不懂再交給 AI '
                           '(雲端 AI → 本機模型)；一律先顯示確認畫面才加入購物車'),
                Field('voice_llm', '規則解析不到時使用 AI 解析', 'bool', True),
            ),
            subgroup='菜單與點餐頁', keywords='語言 過敏原 熱量 碳足跡 語音點餐'),
    Section('signage', '數位看板 (門市大螢幕)', 'D 菜單', 'bi-tv',
            '門市上方大螢幕的菜單看板 (網址 /signage，免登入)：與菜單售完狀態即時同步，輪播促銷標語與取餐叫號。'
            '以瀏覽器全螢幕 (F11) 開啟即可。',
            (
                Field('enabled', '啟用數位看板', 'bool', True),
                Field('headline', '看板標題', default='', placeholder='空白 = 店名'),
                Field('categories', '顯示的分類 (逗號分隔，依此順序)', default='', help='空白 = 全部分類 (依名稱排序)'),
                Field('columns', '每頁分類欄數', 'int', 3, min=1, max=4),
                Field('page_seconds', '分類超過一頁時幾秒換頁', 'int', 10, min=3, max=120),
                Field('show_images', '顯示餐點照片', 'bool', True),
                Field('show_english', '顯示英文名稱 (菜單進階設定的翻譯)', 'bool', True),
                Field('hide_sold_out', '售完的餐點直接隱藏', 'bool', False, help='關閉時仍顯示，並標示「售完 Sold Out」'),
                Field('show_pickup', '底部顯示取餐叫號與候位叫號', 'bool', True),
                Field('promo_lines', '輪播促銷標語 (一行一則)', 'textarea', '',
                      help='空白時自動輪播「促銷活動」中進行中的活動名稱'),
                Field('promo_seconds', '標語輪播秒數', 'int', 8, min=3, max=120),
            ), enabled_field='enabled',
            subgroup='菜單與點餐頁', keywords='看板 電視 大螢幕'),
    Section('inventory', '原物料庫存', 'D 菜單', 'bi-box-seam',
            '依配方 (每份餐點的原料用量) 自動扣料、低庫存警示。',
            (
                Field('enabled', '啟用原物料扣料', 'bool', False),
                Field('block_when_short', '原料不足時禁止販售', 'bool', False, help='關閉時照常販售，庫存可能變為負數'),
                Field('alert_notify', '低於安全量時通知 (LINE / Email / Webhook)', 'bool', True),
                Field('expiry_warn_days', '原料批號到期前幾天提醒', 'int', 3, min=0, max=60),
                Field('po_email_supplier', '送出採購單時 Email 給供應商', 'bool', False, help='需設定寄信伺服器 (SMTP)'),
            ), enabled_field='enabled',
            subgroup='庫存', keywords='原料 扣料 庫存 批號'),
    Section('ai', 'AI 服務 (金鑰自動辨識)', 'D 菜單', 'bi-stars',
            '客服小助手、AI 翻譯菜單、語音點餐、菜單文案與營運建議用的雲端 AI。貼上金鑰即可，系統依金鑰開頭與各家的模型清單'
            '自動辨識廠商並挑最新的模型 (客服、語音點餐等即時回應用快速便宜的一檔，翻譯菜單用主力模型)；'
            '金鑰 1 失敗 (過載 / 限流) 時自動改用金鑰 2、3，都不行才用本機模型。'
            '.env 的 GEMINI_API_KEY / AI_API_KEYS 排在這些之後。送出的內容 (菜單、顧客的提問) 會傳到該 AI 廠商。',
            (
                Field('key1', '金鑰 1 (優先使用)', 'secret',
                      help='支援 Google Gemini、Anthropic Claude、OpenAI、DeepSeek、Groq、xAI Grok、OpenRouter、Mistral'),
                Field('model1', '金鑰 1 指定模型 (留空 = 自動挑最新)', placeholder='例如 claude-sonnet-5-5；多個以逗號分隔 (主要, 備援)'),
                Field('key2', '金鑰 2 (金鑰 1 失敗時)', 'secret'),
                Field('model2', '金鑰 2 指定模型 (留空 = 自動)'),
                Field('key3', '金鑰 3', 'secret'),
                Field('model3', '金鑰 3 指定模型 (留空 = 自動)'),
                Field('monthly_budget_usd', '每月預算上限 (美元，0 = 不限)', 'int', 0, min=0, max=100000,
                      help='依各家公開牌價估算本月費用 (系統監控頁可看)，達上限後本月改用本機模型 / 規則字典，下個月自動恢復；'
                           '估計值僅供參考，實際以各家帳單為準'),
            ), enabled_field='key1', enabled_fields=('key1', 'key2', 'key3'),
            docs=(('Google AI Studio 金鑰', 'https://aistudio.google.com/apikey'),
                  ('Anthropic Console 金鑰', 'https://console.anthropic.com/settings/keys'),
                  ('OpenAI 金鑰', 'https://platform.openai.com/api-keys')),
            keywords='AI Gemini Claude Anthropic OpenAI ChatGPT DeepSeek Groq Grok OpenRouter Mistral 金鑰 API key 翻譯 客服 模型'),
    Section('chatbot', 'AI 客服機器人', 'D 菜單', 'bi-robot',
            '點餐頁與出餐進度頁右下角的對話窗：過敏原 / 素食 / 辣度只從菜單標示直接查 (不經 AI 改寫)，訂單進度直接讀訂單，'
            '營業資訊與常見問答用這裡填的文字；其他問題才交給 AI (系統設定「AI 服務」的金鑰 → 本機模型)，AI 回答會標示「請以櫃檯為準」。',
            (
                Field('enabled', '啟用客服機器人', 'bool', False),
                Field('bot_name', '機器人名稱', default='小幫手'),
                Field('welcome', '開場白', default='您好！可以問我餐點內容、過敏原、營業時間或訂單進度。'),
                Field('store_info', '營業資訊 (時間 / 地址 / 電話 / 停車)', 'textarea', '', max_len=10000,
                      help='顧客問營業時間、地址、電話時直接回這段文字，例如「每日 11:00–21:00，週一公休。地址：…；電話：…」'),
                Field('faq', '常見問答 (每行「關鍵字1 關鍵字2 => 回答」)', 'textarea', '', max_len=10000,
                      help='例如「停車 車位 => 店門口有 3 個機車位，汽車請停對面公有停車場」；關鍵字命中越多的優先'),
                Field('llm_enabled', '其他問題交給 AI 回答', 'bool', True,
                      help='關閉時不在資料裡的問題一律回「請洽櫃檯」；每則 AI 回答會呼叫雲端 AI (有費用) 或本機模型'),
                Field('fallback', '答不出來時的回覆', default='這個問題我不太確定，請直接洽詢櫃檯人員，謝謝！'),
                Field('off_topic', '與本店無關的問題的回覆', default='',
                      help='空白時使用「我是本店的點餐小幫手，只能回答餐點、點餐、付款、會員點數、營業資訊與訂單相關的問題喔！」'),
                Field('handoff_enabled', '允許轉接真人客服', 'bool', True,
                      help='顧客可在對話窗按「轉接真人客服」，店員在後台「顧客與行銷 → 客服對話」回覆'),
            ), enabled_field='enabled',
            subgroup='菜單與點餐頁', keywords='AI 客服 聊天 機器人'),
    # ------------------------------------------------------------------ E 會員行銷
    Section('line', 'LINE 登入與官方帳號', 'E 會員行銷', 'bi-chat-dots',
            'LINE Login 讓會員以 LINE 登入 / 綁定；Messaging API 用來推播取餐通知、生日禮與行銷訊息。',
            (
                Field('login_enabled', '啟用 LINE 登入', 'bool', False),
                Field('login_channel_id', 'LINE Login Channel ID'),
                Field('login_channel_secret', 'LINE Login Channel Secret', 'secret'),
                Field('messaging_enabled', '啟用官方帳號推播', 'bool', False),
                Field('messaging_access_token', 'Messaging API Channel Access Token', 'secret'),
                Field('admin_user_ids', '接收系統警示的 LINE userId (每行一個)', 'textarea'),
            ), enabled_field='login_enabled', enabled_fields=('login_enabled', 'messaging_enabled'),
            docs=(('LINE Login', 'https://developers.line.biz/en/docs/line-login/'),
                  ('Messaging API', 'https://developers.line.biz/en/docs/messaging-api/')),
            subgroup='通訊服務', keywords='LINE 登入 官方帳號 推播'),
    Section('member', '會員等級 / 生日禮 / 儲值金', 'E 會員行銷', 'bi-award',
            '等級依累積消費自動升級 (等級在「會員經營」頁設定)；生日當月自動贈送；儲值金可在 POS 加值與扣款。',
            (
                Field('tiers_enabled', '啟用會員等級', 'bool', True),
                Field('birthday_enabled', '啟用生日禮', 'bool', False),
                Field('birthday_points', '生日禮點數', 'int', 0, min=0, max=100000),
                Field('birthday_coupon_code', '生日禮優惠券代碼', help='需先在「回饋商城管理」建立該優惠券 (可設為 0 點的會員券)'),
                Field('birthday_message', '生日推播 (LINE)', default='{name} 生日快樂！{shop} 送上生日禮，已放入您的會員帳戶。'),
                Field('wallet_enabled', '啟用儲值金', 'bool', False,
                      help='儲值金屬預收款，依法需提供履約保證並揭露於定型化契約，啟用前請確認'),
                Field('pos_verify', 'POS 動用儲值金 / 點數 / 寄杯需驗證會員身分', 'bool', True,
                      help='掃描會員手機的動態條碼或由會員輸入 PIN；都不行時由店長 (或有「免驗證會員身分」權限的員工) 核准。關閉 = 只憑手機號碼即可 (信任櫃檯)'),
                Field('topup_bonus', '加值贈送 (金額:贈送，逗號分隔)', placeholder='例如 1000:100, 3000:400'),
                Field('wallet_max_balance', '儲值金餘額上限', 'int', 10000, min=0, max=1000000),
                Field('points_expire_months', '點數有效期限 (月，0 = 永不到期)', 'int', 0, min=0, max=60,
                      help='先得到的點數先使用、先到期；每天由線上端 (或單機) 執行到期扣點'),
                Field('points_expiry_notice_days', '到期前幾天 LINE 提醒', 'int', 30, min=0, max=180,
                      help='0 = 不提醒；需會員已綁定 LINE'),
                Field('referral_enabled', '啟用推薦好友', 'bool', False,
                      help='新會員註冊時填推薦碼，完成第一筆消費後雙方得點；啟用簡訊驗證註冊可防止灌帳號'),
                Field('referrer_points', '推薦人得點', 'int', 100, min=0, max=100000),
                Field('referee_points', '新會員得點', 'int', 50, min=0, max=100000),
                Field('referral_min_spend', '第一筆消費至少 (元)', 'int', 0, min=0, max=100000),
            ),
            subgroup='會員', keywords='會員 等級 生日 儲值'),
    Section('stamps', '集點卡', 'E 會員行銷', 'bi-postage',
            '會員消費集章，集滿自動兌換點數或優惠券 (例如買 10 杯送 1 杯)。',
            (
                Field('enabled', '啟用集點卡', 'bool', False),
                Field('card_size', '集滿幾章', 'int', 10, min=2, max=50),
                Field('mode', '集章方式', 'select', 'item', (('item', '每購買 1 份指定分類餐點得 1 章'),
                                                       ('order', '每筆消費滿額得 1 章'))),
                Field('categories', '計章的餐點分類 (逗號分隔，空白 = 全部)', placeholder='例如 飲料, 咖啡'),
                Field('min_spend', '每筆消費滿額 (元，「每筆消費」方式使用)', 'int', 100, min=0, max=100000),
                Field('reward_points', '集滿送點數', 'int', 0, min=0, max=100000),
                Field('reward_coupon_code', '集滿送優惠券 (代碼)', help='需先在「回饋商城管理」建立 (可設為 0 點的會員券)'),
                Field('card_title', '集點卡名稱', default='集點卡'),
            ), enabled_field='enabled',
            subgroup='會員', keywords='集點 寄杯'),
    Section('gift_card', '禮物卡', 'E 會員行銷', 'bi-gift',
            'POS 售出禮物卡 (商品禮券)，結帳時以卡號扣款；餘額跨分店通用。',
            (
                Field('enabled', '啟用禮物卡', 'bool', False,
                      help='依法禮券不得記載使用期限，且須提供履約保證 (銀行保證 / 信託 / 同業連帶保證)，啟用前請確認'),
                Field('preset_amounts', '常用面額 (逗號分隔)', default='500, 1000, 2000'),
                Field('max_amount', '單張面額上限', 'int', 10000, min=100, max=100000),
                Field('kiosk_enabled', '點餐機可用禮物卡付款', 'bool', False,
                      help='顧客在自助點餐機輸入 16 碼卡號查餘額並扣款 (餘額不足時請至櫃檯以混合付款補差額)'),
            ), enabled_field='enabled',
            subgroup='會員', keywords='禮物卡'),
    Section('feedback', '顧客評價', 'E 會員行銷', 'bi-star-half',
            '顧客在出餐追蹤頁 (收據 QR code) 為餐點評分；高分邀請到 Google 留下評論，低分通知店家。',
            (
                Field('enabled', '出餐後邀請顧客評分', 'bool', True),
                Field('google_review_url', 'Google 評論連結', 'url', '',
                      help='Google 商家檔案 →「取得更多評論」的連結；留空則不顯示'),
                Field('review_min_rating', '幾星以上邀請到 Google 評論', 'int', 4, min=1, max=5),
                Field('alert_max_rating', '幾星以下通知店家 (0 = 不通知)', 'int', 2, min=0, max=5,
                      help='透過「監控與錯誤通知」設定的管道 (Webhook / Email / LINE) 通知'),
            ), enabled_field='enabled',
            subgroup='會員', keywords='評價 評分 google'),
    Section('sms', '簡訊驗證碼', 'E 會員行銷', 'bi-chat-square-text',
            '註冊與忘記 PIN 時以簡訊驗證手機。測試時可選「只記錄 (不發送)」，驗證碼會顯示在系統監控頁。',
            (
                Field('provider', '簡訊服務', 'select', 'none',
                      (('none', '不使用'), ('log', '只記錄 (測試用，不發送)'), ('mitake', '三竹簡訊'), ('every8d', '每日簡訊 EVERY8D'))),
                Field('username', '帳號'),
                Field('password', '密碼', 'secret'),
                Field('otp_on_register', '註冊時需簡訊驗證手機', 'bool', False),
                Field('allow_pin_reset', '允許以簡訊重設 PIN', 'bool', False),
                Field('template', '簡訊內容', default='【{shop}】驗證碼 {code}，10 分鐘內有效，請勿提供給他人。'),
                Field('tenant_hourly_limit', '全店每小時驗證碼上限', 'int', 60, min=1, max=100000,
                      help='每支手機另有每小時 5 次的限制；達到上限會暫停發送並通知，避免被惡意灌簡訊消耗點數'),
                Field('tenant_daily_limit', '全店每天驗證碼上限', 'int', 300, min=1, max=1000000),
            ), enabled_field='provider',
            subgroup='通訊服務', keywords='簡訊 驗證碼 OTP'),
    # ------------------------------------------------------------------ F 通路
    Section('delivery', '外送平台', 'F 通路', 'bi-bicycle',
            'Uber Eats / foodpanda 訂單自動匯入 (需向平台申請 POS 串接資格)；或在 POS 手動登錄外送單。'
            '平台品項的代碼請填在餐點的「外送 / 條碼代碼」。',
            (
                Field('ubereats_enabled', '啟用 Uber Eats 串接', 'bool', False),
                Field('ubereats_client_id', 'Uber Client ID'),
                Field('ubereats_client_secret', 'Uber Client Secret (也用來驗證 Webhook 簽章)', 'secret'),
                Field('ubereats_store_id', 'Uber Eats Store ID', help=PLATFORM_CODE_HELP),
                Field('ubereats_auto_accept', '自動接單', 'bool', True,
                      help='關閉後本系統不會替您接單，請在 Uber Eats 平板上手動接單；逾時未接單平台會自動取消 '
                           '(本店訂單會依平台取消通知自動取消並回補庫存)'),
                Field('foodpanda_enabled', '啟用 foodpanda 串接', 'bool', False),
                Field('foodpanda_token', 'foodpanda Webhook 權杖 (Bearer)', 'secret'),
                Field('foodpanda_vendor_code', 'foodpanda 店家代碼', help=PLATFORM_CODE_HELP),
                Field('manual_platforms', 'POS 手動登錄的平台名稱 (逗號分隔)', default='Uber Eats, foodpanda'),
            ), enabled_field='ubereats_enabled', enabled_fields=('ubereats_enabled', 'foodpanda_enabled'),
            subgroup='線上與外送', keywords='uber eats foodpanda 外送平台'),
    Section('own_delivery', '自有外送', 'F 通路', 'bi-scooter',
            '本店人員外送到府：線上點餐與 POS 電話訂餐可選「外送到府」，依配送區域收外送費，後台「外送派車」指派外送員與追蹤。',
            (
                Field('enabled', '啟用外送到府', 'bool', False),
                Field('channels', '開放的管道', 'multi', ['online', 'pos'], (('online', '線上點餐'), ('pos', 'POS 電話訂餐'))),
                Field('zones', '配送區域 (每行「名稱:外送費:最低消費」)', 'textarea', '3 公里內:30:200\n3~6 公里:60:400',
                      help='例如「3 公里內:30:200」：外送費 $30、商品金額滿 $200 才外送'),
                Field('free_over', '商品金額滿多少免外送費 (0 = 不免)', 'int', 0, min=0, max=100000),
                Field('eta_minutes', '預估送達時間 (分鐘)', 'int', 45, min=10, max=240),
                Field('drivers', '外送員 (每行一位)', 'textarea', '', help='派車時從名單中選擇'),
                Field('gps_tracking', '外送員手機回報位置 (顧客追蹤頁顯示外送員位置)', 'bool', True,
                      help='指派外送員後，派車頁出現外送員專用連結 / QR code；外送員用手機開啟並允許定位即可。'
                           '手機需能連到本店的公開網址 (總部 / 線上端網址，或系統設定的 QR code 網址)'),
                Field('gps_interval', '位置回報間隔 (秒)', 'int', 15, min=5, max=120),
            ), enabled_field='enabled',
            subgroup='線上與外送', keywords='外送 外送員 GPS 派車 配送'),
    Section('table_pay', '桌邊線上結帳', 'F 通路', 'bi-phone',
            '桌邊掃碼點餐的顧客可用手機線上付清這一桌未結帳的訂單 (綠界信用卡)，不用到櫃檯。',
            (
                Field('enabled', '啟用桌邊線上結帳', 'bool', False,
                      help='需設定綠界特店金鑰 (.env 的 ECPAY_*，未設定時為綠界測試環境)，且門市網址要能從外部連線 '
                           '(綠界會回傳付款結果到 /payment/ecpay/table_notify)'),
                Field('note', '付款頁說明', default='付款完成後即完成結帳，可直接離開；需要發票載具請洽櫃檯。'),
            ), enabled_field='enabled',
            subgroup='店內', keywords='桌邊 結帳 掃碼'),
    Section('online_order', '線上點餐', 'F 通路', 'bi-clock-history',
            '線上點餐 (線上端) 的預約取餐時間：顧客結帳時可選「盡快」或指定時段。',
            (
                Field('scheduled_pickup', '開放預約取餐時間', 'bool', False),
                Field('open_time', '最早取餐時間', 'time', '10:00'),
                Field('close_time', '最晚取餐時間', 'time', '21:00'),
                Field('slot_minutes', '時段間隔 (分鐘)', 'int', 15, min=5, max=120),
                Field('lead_minutes', '至少提前幾分鐘', 'int', 20, min=0, max=480, help='顧客最快可選的時間 = 現在 + 此分鐘數'),
                Field('days_ahead', '可預約天數 (0 = 只能今天)', 'int', 0, min=0, max=7),
            ), enabled_field='scheduled_pickup',
            subgroup='線上與外送', keywords='線上點餐 預約 取餐時段'),
    Section('reservation', '訂位 / 候位', 'F 通路', 'bi-calendar-check',
            '線上訂位頁 /reserve 與現場取號候位 /waitlist。',
            (
                Field('online_enabled', '開放線上訂位', 'bool', False),
                Field('open_time', '可訂位起始時間', 'time', '11:00'),
                Field('close_time', '可訂位結束時間', 'time', '20:30'),
                Field('slot_minutes', '時段間隔 (分鐘)', 'int', 30, min=10, max=240),
                Field('capacity_per_slot', '每時段可訂人數', 'int', 20, min=1, max=1000),
                Field('max_party_size', '單筆訂位人數上限', 'int', 10, min=1, max=100),
                Field('days_ahead', '可預訂天數', 'int', 30, min=1, max=180),
                Field('waitlist_public', '開放顧客自行取號候位', 'bool', True),
                Field('deposit_per_person', '線上訂位每位訂金 (元，0 = 不收)', 'int', 0, min=0, max=10000,
                      help='以綠界信用卡預付；門市主機需能對外 HTTPS (PUBLIC_BASE_URL) 並設定綠界特店，30 分鐘內未付款自動取消'),
                Field('deposit_min_party', '幾位以上收訂金', 'int', 1, min=1, max=100),
                Field('deposit_refund_hours', '訂位前幾小時以前取消可退訂金', 'int', 24, min=0, max=720),
                Field('deposit_on_seat', '入座後的訂金', 'select', 'refund',
                      (('refund', '自動退還 (保證金)'), ('apply', '結帳時折抵 (店員以整單折扣扣除)'))),
                Field('reminder_hours', '訂位前幾小時發提醒 (0 = 不提醒)', 'int', 0, min=0, max=72,
                      help='有設定簡訊服務時以簡訊，否則以 LINE (同手機的會員已綁定 LINE)'),
                Field('no_show_limit', '近 180 天未到幾次後暫停線上訂位 (0 = 不限)', 'int', 0, min=0, max=20),
            ), enabled_field='online_enabled',
            subgroup='店內', keywords='訂位 候位 訂金'),
    # ------------------------------------------------------------------ G 報表
    Section('smtp', '寄信伺服器 (SMTP)', 'G 報表與總部', 'bi-envelope',
            '寄送定期報表與系統警示。Gmail 請使用「應用程式密碼」。',
            (
                Field('host', 'SMTP 主機', placeholder='smtp.gmail.com'),
                Field('port', '埠號', 'int', 587, min=1, max=65535),
                Field('use_tls', '使用 STARTTLS', 'bool', True),
                Field('username', '帳號'),
                Field('password', '密碼', 'secret'),
                Field('sender', '寄件者', 'email'),
            ), enabled_field='host',
            subgroup='報表', keywords='寄信 email 伺服器'),
    Section('report_email', '定期寄送報表', 'G 報表與總部', 'bi-send',
            '每天 (或每週一) 把前一天 (或上週) 的營運報表 Excel 寄給指定信箱。',
            (
                Field('enabled', '啟用', 'bool', False),
                Field('recipients', '收件者 (逗號分隔)', max_len=2000),
                Field('send_time', '寄送時間', 'time', '08:00'),
                Field('frequency', '頻率', 'select', 'daily', (('daily', '每天 (前一天)'), ('weekly', '每週一 (上週)'))),
            ), enabled_field='enabled',
            subgroup='報表', keywords='報表 寄送 排程'),
    Section('accounting', '會計匯出科目', 'G 報表與總部', 'bi-journal-text',
            '「會計匯出」產生每日傳票 (依收款紀錄) 與發票明細 CSV / Excel，匯入會計軟體。科目代號請依貴公司會計科目表修改。',
            (
                Field('tax_rate', '營業稅率 (%)', 'int', 5, min=0, max=20, help='售價內含營業稅 (一般 B2C)'),
                Field('acct_cash', '現金', default='1111 現金'),
                Field('acct_card', '信用卡 / Apple Pay', default='1141 應收帳款-信用卡'),
                Field('acct_mobile', '行動支付 (LINE Pay、街口)', default='1142 應收帳款-行動支付'),
                Field('acct_online', '線上金流 (綠界 ATM / 超商)', default='1143 應收帳款-線上金流'),
                Field('acct_platform', '外送平台代收', default='1144 應收帳款-外送平台'),
                Field('acct_wallet', '儲值金 (預收)', default='2151 預收款項-儲值金'),
                Field('acct_gift', '禮物卡 (預收)', default='2152 預收款項-禮物卡'),
                Field('acct_sales', '銷貨收入', default='4111 銷貨收入'),
                Field('acct_returns', '銷貨退回', default='4171 銷貨退回'),
                Field('acct_tax', '銷項稅額', default='2204 銷項稅額'),
                Field('acct_petty', '零用金', default='1112 零用金'),
                Field('acct_misc', '雜項費用 (零用金支出)', default='6188 雜項費用'),
                Field('acct_bonus', '儲值加值贈送 (行銷費用)', default='6150 儲值贈送 (行銷費用)'),
            ),
            subgroup='財務', keywords='會計 科目 傳票'),
    Section('pnl', '損益表手續費', 'G 報表與總部', 'bi-percent',
            '損益表依收款方式估算金流 / 平台手續費 (百分比)；實際費率請依合約填寫。',
            (
                Field('card_fee_percent', '信用卡 / Apple Pay (刷卡機) %', 'float', 2.0, min=0, max=50),
                Field('mobile_fee_percent', '行動支付 (LINE Pay、街口) %', 'float', 2.5, min=0, max=50),
                Field('online_fee_percent', '線上金流 (綠界信用卡 / ATM / 超商) %', 'float', 2.75, min=0, max=50),
                Field('platform_fee_percent', '外送平台抽成 %', 'float', 30.0, min=0, max=80),
            ),
            subgroup='財務', keywords='損益 手續費'),
    Section('staffing', '排班建議', 'G 報表與總部', 'bi-people',
            '「排班建議」依備料預測的各時段來客數估算每小時需要的人力，與班表比較並建議班別。',
            (
                Field('orders_per_staff_hour', '每位員工每小時可處理幾張訂單', 'int', 12, min=1, max=200),
                Field('min_staff', '營業時段最少人數', 'int', 1, min=0, max=50),
                Field('shift_hours', '建議班別長度 (小時)', 'int', 4, min=2, max=12),
                Field('weeks', '參考過去幾週同一個星期幾', 'int', 4, min=1, max=12),
                Field('max_hours_day', '自動排班：每日工時上限 (小時)', 'int', 8, min=1, max=12, help='勞基法 §30 正常工時每日 8 小時'),
                Field('max_hours_week', '自動排班：每週工時上限 (小時)', 'int', 40, min=1, max=84,
                      help='勞基法 §30 每週 40 小時；員工可在「可排時段」另設較低的個人上限'),
                Field('max_consecutive_days', '自動排班：最多連續工作天數', 'int', 6, min=1, max=12, help='勞基法 §36 每七天至少一天例假'),
            ),
            subgroup='人力', keywords='排班 人力 工時 勞基法'),
    Section('group_order', '團體預購', 'F 通路', 'bi-people-fill',
            '主揪建立團購連結分享給同事，每個人自己點餐，截止時合併成一張預約取餐訂單 (到店付款)。'
            '門市 / 單機使用 (線上端不提供)。',
            (
                Field('enabled', '開放團體預購', 'bool', False, help='開放後點餐首頁顯示「團體預購」入口'),
                Field('min_lead_minutes', '取餐時間至少要在幾分鐘後', 'int', 120, min=30, max=10080),
                Field('max_days', '最多可預約幾天後', 'int', 14, min=1, max=60),
                Field('max_people', '每團最多幾人', 'int', 50, min=2, max=300),
                Field('max_qty_per_person', '每人最多幾份', 'int', 10, min=1, max=100),
                Field('deadline_minutes', '預設在取餐前幾分鐘截止收單', 'int', 60, min=15, max=1440),
            ), enabled_field='enabled',
            subgroup='店內', keywords='團購 團體 預購'),
    Section('anomaly', '員工異常偵測', 'G 報表與總部', 'bi-shield-exclamation',
            '依收銀員比較作廢 / 退款、折扣 / 改價、交班帳差，明顯高於全店平均或超過門檻時標示 (後台「員工異常偵測」) 並每日通知。',
            (
                Field('notify', '每天通知前一天的異常', 'bool', False, help='以「監控與錯誤通知」的管道送出'),
                Field('notify_time', '通知時間', 'time', '08:30'),
                Field('rate_multiple', '高於全店平均幾 % 視為異常', 'int', 200, min=110, max=1000),
                Field('void_rate_limit', '作廢 / 退款金額佔營業額超過 % 視為異常', 'int', 5, min=1, max=100),
                Field('discount_rate_limit', '折扣 / 改價金額佔營業額超過 % 視為異常', 'int', 10, min=1, max=100),
                Field('min_events', '至少發生幾次才判斷', 'int', 3, min=1, max=100),
                Field('shift_diff_limit', '交班帳差超過多少元立即通知 (0 = 不通知)', 'int', 200, min=0, max=100000),
            ), enabled_field='notify',
            subgroup='人力', keywords='異常 作廢 折扣'),
    # ------------------------------------------------------------------ H 維運
    Section('monitor', '監控與錯誤通知', 'H 維運', 'bi-activity',
            '系統發生錯誤、印表機或發票失敗、時鐘誤差過大時通知 (同一問題 10 分鐘內只通知一次)。',
            (
                Field('webhook_url', 'Webhook 網址 (Slack / Discord / Teams)', 'secret'),
                Field('email_recipients', '通知 Email (逗號分隔)', max_len=2000),
                Field('line_alerts', '以 LINE 通知 (LINE 設定中的管理者)', 'bool', False),
                Field('clock_server', '校時伺服器 (NTP)', default='time.stdtime.gov.tw'),
                Field('clock_warn_seconds', '時鐘誤差警告 (秒)', 'int', 60, min=5, max=3600),
            ), enabled_field='webhook_url', enabled_fields=('webhook_url', 'email_recipients', 'line_alerts'),
            keywords='監控 錯誤 通知 webhook'),
    Section('archive', '舊資料封存', 'H 維運', 'bi-archive',
            '把超過指定月數的已結束訂單與日誌搬到封存檔 (database/archive)，主資料庫保持輕量；封存檔可下載與查詢。',
            (
                Field('months', '封存幾個月以前的資料', 'int', 24, min=6, max=120,
                      help='帳簿憑證依法應保存 5 年以上：封存檔不會刪除，請一併備份'),
                Field('auto', '每月 1 日自動封存', 'bool', False),
            ), enabled_field='auto',
            keywords='封存 舊資料'),
    Section('backup', '自動備份 (本機資料庫)', 'H 維運', 'bi-hdd',
            '門市主機 (SQLite) 每天自動備份到 database/backups；線上端 (PostgreSQL) 由 docker-compose 的 backup 服務備份。'
            '備份的是整台主機的資料庫 (含所有店家)，整台主機共用一份設定 (預設店家)，只有平台管理者可以檢視與修改。',
            (
                Field('enabled', '啟用每日自動備份', 'bool', True),
                Field('time', '備份時間', 'time', '03:30'),
                Field('keep_days', '保留天數', 'int', 14, min=1, max=365),
                Field('offsite', '異地備份 (每日備份後上傳)', 'select', 'none',
                      (('none', '不上傳'), ('webdav', 'WebDAV (Nextcloud / Synology / 群暉 WebDAV…)'),
                       ('s3', 'S3 相容儲存 (AWS S3 / MinIO / Cloudflare R2 / Backblaze B2)'),
                       ('command', '自訂指令 (例如 rclone copy {file} gdrive:backups)')),
                      help='上傳失敗會進「監控與錯誤通知」；按「測試」會上傳一個小檔案確認設定'),
                Field('offsite_url', 'WebDAV 資料夾網址 / S3 端點', 'url', '',
                      placeholder='https://nas.example.com/remote.php/dav/files/user/backups  或  https://s3.ap-northeast-1.amazonaws.com'),
                Field('offsite_user', 'WebDAV 帳號 / S3 Access Key'),
                Field('offsite_password', 'WebDAV 密碼 / S3 Secret Key', 'secret'),
                Field('offsite_bucket', 'S3 Bucket (可加路徑，例如 shop-backups/store1)'),
                Field('offsite_region', 'S3 Region', default='auto', help='AWS 填區域代碼 (ap-northeast-1)；MinIO / R2 / B2 填 auto'),
                Field('offsite_command', '自訂上傳指令', placeholder='rclone copy {file} gdrive:kiosk-backups',
                      help='{file} 會換成備份檔完整路徑；在門市主機上必須能直接執行'),
                Field('offsite_keep', '異地保留份數 (WebDAV / S3)', 'int', 30, min=1, max=365),
            ), enabled_field='enabled',
            keywords='備份'),
    Section('security', '帳號安全', 'H 維運', 'bi-shield-lock',
            '員工帳號的兩步驟驗證與閒置自動登出。每位員工在後台「帳號安全」頁自行設定驗證 App。',
            (
                Field('require_2fa_roles', '必須啟用兩步驟驗證的角色', 'multi', [],
                      (('owner', '負責人'), ('manager', '店長')),
                      help='勾選後，該角色登入時若尚未啟用，只能進入「帳號安全」頁完成設定'),
                Field('staff_idle_minutes', '閒置幾分鐘自動登出 (0 = 不限制)', 'int', 0, min=0, max=1440,
                      help='收銀台、廚房螢幕通常整天開著，建議只在後台電腦共用時設定'),
            ),
            keywords='密碼 兩步驟 2FA 登入'),
)}

GROUPS = []
for _s in SECTIONS.values():
    if _s.group not in GROUPS:
        GROUPS.append(_s.group)


# ---------------------------------------------------------------------------
# 讀取 / 儲存
# ---------------------------------------------------------------------------
# 不由總部下發的區塊 (沒有金鑰但屬於這台主機本身的設定)
LOCAL_ONLY_SECTIONS = {'backup', 'order_intake'}
# 整台主機共用、只有平台管理者 (預設店家負責人) 可以檢視與修改的區塊：自動備份是整個資料庫 (含所有店家) 的備份，
# 設定一律存放 / 讀取預設店家 (slug=default) 的那一份，其他店家的同名設定不會生效
PLATFORM_SECTIONS = {'backup'}
# 不含金鑰、但只有負責人可以檢視與修改的區塊 (帳號安全政策不能讓受它約束的店長自己取消)
OWNER_ONLY_SECTIONS = {'security'}


def hq_managed(key):
    section = SECTIONS[key]
    return key not in LOCAL_ONLY_SECTIONS and not any(f.type == 'secret' for f in section.fields)


def has_secret(key):
    return any(f.type == 'secret' for f in SECTIONS[key].fields)


def owner_only(key):
    """只有負責人可以檢視 / 修改 / 匯出 / 匯入的區塊：含金鑰，或屬於 OWNER_ONLY_SECTIONS"""
    return key in OWNER_ONLY_SECTIONS or has_secret(key)


def _platform_tenant_id(tid):
    from app.models.tenant import Tenant
    cache = _cache()
    if cache is not None and ('_platform_tid', None) in cache:
        return cache[('_platform_tid', None)] or tid
    t = Tenant.query.filter_by(slug='default').first()
    pid = t.id if t is not None else None
    if cache is not None:
        cache[('_platform_tid', None)] = pid
    return pid or tid


def _resolve_tenant_id(key, tenant_id):
    tid = _current_tenant_id(tenant_id)
    return _platform_tenant_id(tid) if key in PLATFORM_SECTIONS else tid


def _row(model, tid, key):
    return model.query.filter_by(tenant_id=tid, key=key).execution_options(skip_tenant_filter=True).first()


def _is_node():
    from app.sync import runtime as sync_runtime
    return sync_runtime.is_node()


def source(key, tenant_id=None):
    """目前生效的設定來源：local (本機 / 本店自訂) / hq (總部下發) / default (預設值)"""
    tid = _resolve_tenant_id(key, tenant_id)
    if _row(StoreSetting, tid, key) is not None:
        return 'local'
    if _is_node() and hq_managed(key) and _row(HqSetting, tid, key) is not None:
        return 'hq'
    return 'default'


def has_hq(key, tenant_id=None):
    """分店：總部是否已下發過此區塊 (有 HqSetting)"""
    tid = _current_tenant_id(tenant_id)
    return hq_managed(key) and _row(HqSetting, tid, key) is not None


def _defaults(section):
    return {f.name: (list(f.default) if isinstance(f.default, list) else f.default) for f in section.fields}


def _cache():
    if not has_request_context():
        return None
    if not hasattr(g, '_store_settings'):
        g._store_settings = {}
    return g._store_settings


def _current_tenant_id(tenant_id):
    if tenant_id is not None:
        return tenant_id
    tid = getattr(g, 'tenant_id', None) if has_request_context() else None
    if tid is None:
        raise RuntimeError('缺少店家資訊，無法讀取設定')
    return tid


def get(key, tenant_id=None):
    """回傳合併預設值後的設定 dict (同一請求內快取)"""
    section = SECTIONS[key]
    tid = _resolve_tenant_id(key, tenant_id)
    cache = _cache()
    if cache is not None and (tid, key) in cache:
        return dict(cache[(tid, key)])
    data = _defaults(section)
    row = _row(StoreSetting, tid, key)
    if row is None and hq_managed(key) and _is_node():
        row = _row(HqSetting, tid, key)   # 分店沒有自己的設定：使用總部下發的值
    if row is not None:
        try:
            stored = json.loads(row.value or '{}')
        except ValueError:
            stored = {}
        for f in section.fields:
            if f.name in stored:
                data[f.name] = stored[f.name]
    if cache is not None:
        cache[(tid, key)] = dict(data)
    return data


def reader(key):
    """各服務模組的 config()：`config = settings_service.reader('chatbot')` 等同 def config(tenant_id=None): return get('chatbot', tenant_id)"""
    def config(tenant_id=None):
        return get(key, tenant_id)
    config.__name__ = config.__qualname__ = 'config'
    config.__doc__ = f'設定區塊 {key!r}'
    return config


def flag(key, field_name='enabled', default=False):
    """各服務模組的 enabled()：`enabled = settings_service.flag('chatbot')` 讀該區塊的布林欄位"""
    def enabled(tenant_id=None):
        return bool(get(key, tenant_id).get(field_name, default))
    enabled.__name__ = enabled.__qualname__ = 'enabled'
    enabled.__doc__ = f'設定區塊 {key!r} 的 {field_name}'
    return enabled


def is_enabled(key, tenant_id=None):
    section = SECTIONS[key]
    names = section.enabled_fields or ((section.enabled_field,) if section.enabled_field else ())
    if not names:
        return True
    cfg = get(key, tenant_id)

    def on(value):
        if isinstance(value, str):
            value = value.strip()
        return bool(value) and value != 'none'
    return any(on(cfg.get(n)) for n in names)


def put(key, values, tenant_id=None, changed_by='system'):
    """直接寫入 (程式內部或測試使用；不做表單解析)。由呼叫端 commit。"""
    tid = _resolve_tenant_id(key, tenant_id)
    current = get(key, tid)
    current.update({k: v for k, v in values.items() if any(f.name == k for f in SECTIONS[key].fields)})
    row = StoreSetting.query.filter_by(tenant_id=tid, key=key).execution_options(skip_tenant_filter=True).first()
    if row is None:
        row = StoreSetting(tenant_id=tid, key=key)
        db.session.add(row)
    row.value = json.dumps(current, ensure_ascii=False)
    row.changed_by, row.changed_at = changed_by, datetime.now()
    cache = _cache()
    if cache is not None:
        cache.pop((tid, key), None)
    return current


def publish_hq(key, tenant_id=None, changed_by='system'):
    """線上端：把目前的設定複製到 HqSetting (同步下發到所有分店)。由呼叫端 commit。"""
    if not hq_managed(key):
        return None
    tid = _current_tenant_id(tenant_id)
    data = get(key, tid)
    row = _row(HqSetting, tid, key)
    if row is None:
        row = HqSetting(tenant_id=tid, key=key)
        db.session.add(row)
    row.value = json.dumps(data, ensure_ascii=False)
    row.changed_by, row.changed_at = changed_by, datetime.now()
    return row


def revert_to_hq(key, tenant_id=None):
    """分店：刪除本店自訂，改回使用總部設定。由呼叫端 commit。"""
    if not hq_managed(key):
        raise AppError('此設定含金鑰或屬於本機設定，不由總部下發')
    tid = _current_tenant_id(tenant_id)
    if _row(HqSetting, tid, key) is None:   # 總部從未下發：刪掉本店設定只會退回預設值，不是「改回總部設定」
        raise AppError('總部尚未下發此設定，無法改回')
    row = _row(StoreSetting, tid, key)
    if row is not None:
        db.session.delete(row)
    cache = _cache()
    if cache is not None:
        cache.pop((tid, key), None)
    return row is not None


def text_max_len(f):
    """文字欄位的長度上限 (設定頁與匯入共用同一套規則)"""
    return f.max_len or (2000 if f.type == 'textarea' else 300)


def _check_len(f, text):
    limit = text_max_len(f)
    if len(text) > limit:
        raise AppError(f'「{f.label}」最多 {limit} 字，目前 {len(text)} 字')
    return text


# 設定頁只顯示、儲存時不寫回的欄位 (執行期狀態，由其他頁面切換；表單送出的值一律忽略)
FORM_READONLY_FIELDS = {'order_intake': ('paused', 'resume_at')}


def parse_form(key, form):
    """把後台表單轉成設定值 (驗證失敗拋 AppError)；secret 欄位留空保持原值"""
    section = SECTIONS[key]
    current = get(key)
    readonly = FORM_READONLY_FIELDS.get(key, ())
    values = {}
    for f in section.fields:
        if f.name in readonly:
            continue   # 不放進 values：put() 保留資料庫目前的值
        raw = form.get(f.name)
        if f.type == 'bool':
            values[f.name] = raw not in (None, '', '0')
        elif f.type == 'multi':
            allowed = {o[0] for o in f.options}
            values[f.name] = [v for v in form.getlist(f.name) if v in allowed]
        elif f.type == 'secret':
            if form.get(f'{f.name}__clear'):
                values[f.name] = ''
            elif raw:
                secret = raw.strip()
                if key == 'ai':   # 從 LINE / Notion 複製常夾帶零寬字元；金鑰放進 HTTP 標頭只能是 ASCII
                    secret = ZERO_WIDTH_RE.sub('', raw).strip()
                    if not (secret.isascii() and secret.isprintable()) or ' ' in secret:
                        raise AppError(f'「{f.label}」含無效字元 (全形字、中文或空白)，請重新貼上金鑰')
                values[f.name] = secret[:500]
            else:
                values[f.name] = current.get(f.name, '')
        elif f.type == 'int':
            num = safe_int(raw, f.default if isinstance(f.default, int) else 0)
            if f.min is not None:
                num = max(f.min, num)
            if f.max is not None:
                num = min(f.max, num)
            values[f.name] = num
        elif f.type == 'float':
            num = safe_float(raw, f.default or 0.0)   # NaN / Infinity 回預設值
            if f.min is not None:
                num = max(f.min, num)
            if f.max is not None:
                num = min(f.max, num)
            values[f.name] = num
        elif f.type == 'select':
            allowed = {o[0] for o in f.options}
            values[f.name] = raw if raw in allowed else f.default
        elif f.type == 'time':
            raw = (raw or '').strip()
            if raw and not TIME_RE.match(raw):
                raise AppError(f'「{f.label}」時間格式應為 HH:MM')
            values[f.name] = raw or f.default
        elif f.type == 'url':
            raw = (raw or '').strip()
            if raw and not re.match(r'^https?://', raw):
                raise AppError(f'「{f.label}」必須是 http:// 或 https:// 開頭的網址')
            # 反斜線等字元：urlparse 與 requests 解析出的主機不同，可繞過內網位址檢查
            try:
                netloc = urlparse(raw).netloc
            except ValueError:   # 方括號不成對等 (Invalid IPv6 URL)
                raise AppError(f'「{f.label}」網址格式不正確')
            if any(c == '\\' or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in netloc):
                raise AppError(f'「{f.label}」網址含不允許的字元')
            values[f.name] = raw[:500]
        else:
            values[f.name] = _check_len(f, (raw or '').strip())
    _validate(key, values, form)
    return values


def _validate(key, values, form):
    """區塊專屬的交叉驗證 (設定頁儲存時)：存進去會讓執行期出錯的組合直接拒絕"""
    if key == 'einvoice' and values.get('default_carrier') == 'donate':
        if not re.fullmatch(r'[0-9]{3,7}', str(values.get('default_love_code') or '').strip()):
            raise AppError('「顧客未選擇時」選擇捐贈時，預設捐贈愛心碼需為 3~7 碼數字')
    elif key == 'delivery':
        _validate_delivery_codes(values)
    elif key == 'monitor':
        # Webhook 是金鑰欄位：留空 = 沿用原值，只檢查這次實際送出的新網址
        new_hook = (form.get('webhook_url') or '').strip()
        if new_hook and not form.get('webhook_url__clear'):
            from app.services import webhook_service
            webhook_service.validate_url(new_hook)
            webhook_service.check_public(new_hook)   # 不可送到內網或本機位址 (WEBHOOK_ALLOW_PRIVATE=1 放寬)
        server = str(values.get('clock_server') or '').strip()
        if server and not _valid_host(server):
            raise AppError('校時伺服器名稱格式不正確 (例如 time.stdtime.gov.tw)')


def _valid_host(host):
    """主機名稱 (含中文網域) 或 IP；不可含空白、路徑、埠號，空標籤 (a..b) 與超過 63 字的標籤也不行"""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if any(c.isspace() or c in '/:\\@?#' or ord(c) < 32 or ord(c) == 127 for c in host):
        return False
    try:
        host.encode('idna')
    except ValueError:   # UnicodeError 屬於 ValueError
        return False
    return True


def _validate_delivery_codes(values):
    """同一台主機有其他店家也啟用同一平台且沒填代碼時，本店必須填代碼 (否則平台訂單無法判斷店家而全部被拒收)"""
    from app.services import delivery_service
    tid = _current_tenant_id(None)
    for platform, code_field in (('ubereats', 'ubereats_store_id'), ('foodpanda', 'foodpanda_vendor_code')):
        if not values.get(f'{platform}_enabled') or str(values.get(code_field) or '').strip():
            continue
        if any(t != tid for t in delivery_service.unconfigured_tenants(platform, code_field)):
            label = next(f.label for f in SECTIONS['delivery'].fields if f.name == code_field)
            raise AppError(f'「{label}」{PLATFORM_CODE_HELP}')


def form_view(key):
    """後台表單用：secret 欄位只回傳是否已設定"""
    data = get(key)
    view = {}
    for f in SECTIONS[key].fields:
        view[f.name] = bool(data.get(f.name)) if f.type == 'secret' else data.get(f.name)
    if key == 'order_intake':   # 顯示實際狀態 (已過自動恢復時間 = 已恢復接單)，不是原始值
        from app.services import intake_service
        st = intake_service.status()
        view['paused'], view['resume_at'] = st['paused'], st['resume_at']
    return view


def change_summary(key, old, new, limit=40):
    """稽核用：只列出有變動的欄位 (一般欄位「舊值→新值」，金鑰只記已設定 / 更新 / 清除)；沒有變動回傳空清單"""
    def short(v):
        text = ', '.join(map(str, v)) if isinstance(v, list) else str(v if v is not None else '')
        text = text.replace('\r', ' ').replace('\n', ' ')
        return text if len(text) <= limit else text[:limit] + '…'
    out = []
    for f in SECTIONS[key].fields:
        if f.name not in new or old.get(f.name) == new.get(f.name):
            continue
        if f.type == 'secret':
            state = '已清除' if not new.get(f.name) else ('已設定' if not old.get(f.name) else '已更新')
            out.append(f'{f.label} {state}')
        else:
            out.append(f'{f.label}: {short(old.get(f.name))}→{short(new.get(f.name))}')
    return out


# 「測試連線」：{key: callable(settings_dict) -> 訊息字串 (失敗拋 AppError)}，由各服務模組註冊
TESTERS: dict = {}


def register_tester(key):
    def deco(fn):
        TESTERS[key] = fn
        return fn
    return deco


def split_lines(value):
    return [v.strip() for v in re.split(r'[\n,，;]+', str(value or '')) if v.strip()]


# ---------------------------------------------------------------------------
# 設定匯出 / 匯入：開新店快速複製營運規則；金鑰 (secret 欄位) 與本機專屬區塊不匯出
# ---------------------------------------------------------------------------
EXPORT_VERSION = 1


def export_all(tenant_id=None, include_secrets=False, allow_owner_only=True):
    """{'version', 'exported_at', 'sections': {key: {欄位: 值}}}
    allow_owner_only=False (非負責人)：只有負責人能檢視的區塊 (含金鑰 / 帳號安全) 整個不匯出，與設定頁、匯入的權限一致。"""
    out = {}
    for key, section in SECTIONS.items():
        if key in LOCAL_ONLY_SECTIONS or key in PLATFORM_SECTIONS:
            continue
        if not allow_owner_only and owner_only(key):
            continue
        data = get(key, tenant_id)
        out[key] = {f.name: data.get(f.name) for f in section.fields if include_secrets or f.type != 'secret'}
    return {'version': EXPORT_VERSION, 'exported_at': datetime.now().isoformat(timespec='seconds'), 'sections': out}


_TRUE_TEXT = {'true', '1', 'yes', 'on'}
_FALSE_TEXT = {'false', '0', 'no', 'off', ''}


def _import_bool(v):
    """匯入檔的布林值：只接受 true/false、0/1 與明確的文字 ("false" 不會被當成 True)；其他回 None (略過)"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in _TRUE_TEXT | _FALSE_TEXT:
        return v.strip().lower() in _TRUE_TEXT
    return None


def import_all(payload, tenant_id=None, changed_by='import', only_keys=None, allow_secrets=False, applied_keys=None):
    """套用匯出檔：只認得的區塊 / 欄位、型別檢查後寫入；回傳 (套用的區塊數, 略過的鍵)。由呼叫端 commit。
    allow_secrets=False (非負責人)：只有負責人能改的區塊 (含金鑰 / 帳號安全) 整個略過 (與設定頁相同)，
    金鑰欄位一律不從匯入檔寫入。applied_keys 傳入 list 時會加入實際套用的區塊鍵。"""
    if not isinstance(payload, dict) or not isinstance(payload.get('sections'), dict):
        raise AppError('匯入檔格式不正確 (需要 sections)')
    version = payload.get('version')
    version_num = 0 if version in (None, '') else safe_float(version, None)
    if version_num is None or isinstance(version, bool):
        raise AppError('匯入檔的版本欄位不正確')
    if version_num > EXPORT_VERSION:
        raise AppError('匯入檔版本比目前系統新，請先更新系統')
    applied, skipped = 0, []
    for key, values in payload['sections'].items():
        section = SECTIONS.get(key)
        if (section is None or key in LOCAL_ONLY_SECTIONS or key in PLATFORM_SECTIONS
                or (only_keys is not None and key not in only_keys)):
            skipped.append(key)
            continue
        if not allow_secrets and owner_only(key):
            skipped.append(key)
            continue
        if not isinstance(values, dict):
            skipped.append(key)
            continue
        clean = {}
        for f in section.fields:
            if f.name not in values or f.type == 'secret':   # 金鑰不經匯入檔寫入 (匯出檔也不含)
                continue
            v = values[f.name]
            if f.type == 'bool':
                b = _import_bool(v)
                if b is None:
                    skipped.append(f'{key}.{f.name}')
                    continue
                clean[f.name] = b
            elif f.type in ('int', 'float'):
                num = None if isinstance(v, (bool, list, dict)) else safe_float(v, None)   # NaN / Infinity → None
                if num is None:
                    skipped.append(f'{key}.{f.name}')
                    continue
                clean[f.name] = int(num) if f.type == 'int' else num
                if f.min is not None:
                    clean[f.name] = max(f.min, clean[f.name])
                if f.max is not None:
                    clean[f.name] = min(f.max, clean[f.name])
            elif f.type == 'multi':
                allowed = {o[0] for o in f.options}
                clean[f.name] = [x for x in (v if isinstance(v, list) else []) if isinstance(x, str) and x in allowed]
            elif f.type == 'select':
                if isinstance(v, str) and v in {o[0] for o in f.options}:
                    clean[f.name] = v
                else:
                    skipped.append(f'{key}.{f.name}')
            elif isinstance(v, (list, dict)):   # 文字類欄位不接受陣列 / 物件
                skipped.append(f'{key}.{f.name}')
                continue
            elif f.type == 'time':
                # 與設定頁相同的檢查：9:00 這種格式會讓每日排程的字串比較永遠不成立 (備份、定期報表不再執行)
                t = str(v or '').strip()
                if t and not TIME_RE.match(t):
                    skipped.append(f'{key}.{f.name}')
                    continue
                clean[f.name] = t or f.default
            elif f.type == 'url':
                u = str(v or '').strip()
                if u and not re.match(r'^https?://', u):
                    skipped.append(f'{key}.{f.name}')
                    continue
                clean[f.name] = u[:500]
            else:
                text = str(v if v is not None else '')
                if len(text) > text_max_len(f):   # 與設定頁相同的長度上限：超過就略過，不靜默截斷
                    skipped.append(f'{key}.{f.name}')
                    continue
                clean[f.name] = text
        if clean:
            put(key, clean, tenant_id, changed_by=changed_by)
            applied += 1
            if applied_keys is not None:
                applied_keys.append(key)
    return applied, skipped


# 啟用後一定要有金鑰才能運作的設定：{區塊: ((判斷是否啟用的函式, (需要的金鑰欄位, ...)), ...)}
_REQUIRED_SECRETS = {
    'sms': ((lambda c: c.get('provider') in ('mitake', 'every8d'), ('password',)),),
    'einvoice': ((lambda c: c.get('enabled') and not c.get('stage'), ('hash_key', 'hash_iv')),),
    'linepay': ((lambda c: c.get('enabled'), ('channel_secret',)),),
    'line': ((lambda c: c.get('login_enabled'), ('login_channel_secret',)),
             (lambda c: c.get('messaging_enabled'), ('messaging_access_token',))),
    'delivery': ((lambda c: c.get('ubereats_enabled'), ('ubereats_client_secret',)),
                 (lambda c: c.get('foodpanda_enabled'), ('foodpanda_token',))),
}


def missing_secrets(keys, tenant_id=None):
    """已啟用但還沒填金鑰的區塊標題 (匯入設定後提醒負責人補填)"""
    out = []
    for key in keys:
        rules = _REQUIRED_SECRETS.get(key)
        if not rules:
            continue
        cfg = get(key, tenant_id)
        if any(on(cfg) and any(not str(cfg.get(name) or '').strip() for name in names) for on, names in rules):
            out.append(SECTIONS[key].title)
    return out
