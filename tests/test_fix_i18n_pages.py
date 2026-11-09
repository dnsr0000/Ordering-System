"""修正小組 i18n-pages 的回歸測試：顧客端後端訊息 (AppError / flash) 的英 / 日文對照 (B-486 ~ B-489)"""
import re

from app.services import i18n_service
from app.utils import WEAK_PIN_MESSAGE

_CJK = re.compile(r'[一-鿿]')


def _no_chinese(text):
    """英文翻譯不可夾雜中文 (日文會有漢字，只檢查英文)"""
    return not _CJK.search(text)


def test_b486_customer_flow_messages_are_translated(app):
    with app.test_request_context('/'):
        tr = i18n_service.translate_message
        # 註冊：弱 PIN (帶 ❌ 圖示)、重複人臉 (帶 ⚠️ 圖示)、推薦碼
        en = tr(WEAK_PIN_MESSAGE, 'en')
        assert en.startswith('❌ ') and _no_chinese(en)
        assert tr(WEAK_PIN_MESSAGE, 'ja') != WEAK_PIN_MESSAGE
        dup = '⚠️ 此人臉已註冊為本店會員，請直接使用「人臉辨識」或「手機 + PIN」登入；如需協助請洽櫃檯。'
        assert _no_chinese(tr(dup, 'en')) and tr(dup, 'en').startswith('⚠️ ')
        assert _no_chinese(tr('推薦碼不存在，請確認後再輸入 (或留空)', 'en'))
        # 簡訊驗證碼
        for msg in ('驗證碼不正確', '驗證碼已過期或不存在，請重新發送', '請先完成手機簡訊驗證', '驗證碼已寄出，10 分鐘內有效'):
            assert _no_chinese(tr(msg, 'en')), msg
            assert tr(msg, 'ja') != msg
        assert tr('驗證碼已寄出，請 42 秒後再重新發送', 'en') == \
            'A code was just sent; please wait 42 seconds before requesting another'
        # 結帳：電子發票、禮物卡
        for msg in ('手機條碼格式錯誤：應為 / 開頭共 8 碼 (例如 /ABC+123)', '愛心碼格式錯誤：應為 3~7 碼數字',
                    '統一編號格式錯誤 (8 碼數字且需通過檢查碼)', '自然人憑證條碼格式錯誤：應為 2 碼英文 + 14 碼數字'):
            assert _no_chinese(tr(msg, 'en')), msg
        gift = tr('禮物卡餘額不足 (餘額 $120，需 $300)，請改用其他付款方式', 'en')
        assert '$120' in gift and '$300' in gift and _no_chinese(gift)
        # 訂位取消 + 訂金退款後綴
        en = tr('已取消訂位；訂金 $500 將退回原信用卡', 'en')
        assert en.startswith('Reservation cancelled;') and '$500' in en and _no_chinese(en)
        assert '$500' in tr('已取消訂位；訂金 $500 將退回原信用卡', 'ja') and '退回' not in tr('已取消訂位；訂金 $500 將退回原信用卡', 'ja')
        assert _no_chinese(tr('已取消訂位；訂位前 24 小時內取消，訂金 $300 不退還', 'en'))
        assert _no_chinese(tr('已取消訂位；訂金 $300 退款處理中，店家將與您聯繫', 'en'))
        assert tr('已取消訂位', 'en') == 'Reservation cancelled'
        # 桌邊呼叫、評分、桌邊結帳、刪除帳號
        assert tr('已通知服務人員：想結帳', 'en') == 'Staff notified: Bill'
        assert tr('已通知過服務人員 (加水 / 餐具)，請稍候', 'ja') == 'スタッフに通知済みです (お水 / カトラリー)。少々お待ちください'
        for msg in ('謝謝您的評分！', '謝謝您的評分！ 歡迎也到 Google 分享您的用餐體驗', '超過 7 天的訂單無法評分',
                    '這一桌目前沒有需要結帳的訂單', '本店未開放桌邊線上結帳，請至櫃檯結帳',
                    '您的會員帳號與個人資料已刪除，歷史訂單已去識別化保存 (帳務依法保存)。謝謝您的支持！'):
            assert _no_chinese(tr(msg, 'en')), msg


def test_b487_line_unbound_message_matches_flash(app):
    with app.test_request_context('/'):
        msg = '這個 LINE 帳號尚未綁定會員：請以手機 + PIN 登入，或加入會員，完成後會自動綁定 LINE。'
        assert i18n_service.translate_message(msg, 'en').startswith('This LINE account is not linked yet')
        assert i18n_service.translate_message(msg, 'ja').startswith('この LINE アカウントは未連携です')


def test_b488_pattern_groups_are_translated_too(app):
    with app.test_request_context('/'):
        tr = i18n_service.translate_message
        en = tr('餐點【珍奶】目前不供應 (供應時段 11:00~14:00)，請移除後再結帳！', 'en')
        assert en == '"珍奶" is not available now (Available 11:00–14:00); please remove it.'
        ja = tr('套餐【A餐】的配餐【紅茶】目前不供應 (供應日：週一、三)，請改選其他套餐！', 'ja')
        assert '提供日：月・水' in ja and '供應' not in ja
        assert tr('出示會員條碼需輸入 PIN 碼確認身分 (您是以人臉辨識登入)', 'en') == \
            'Showing your member barcode requires your PIN (you logged in with face recognition)'
        assert '個人データのダウンロード' in tr('下載個人資料需輸入 PIN 碼確認身分 (您是以人臉辨識登入)', 'ja')
        assert _no_chinese(tr('解除 LINE 綁定需輸入 PIN 碼確認身分 (您是以人臉辨識登入)', 'en'))
        # R2-047：門市 LINE 登入 (line_kiosk) 的需 PIN 訊息
        assert tr('出示會員條碼需輸入 PIN 碼確認身分 (您是在門市以 LINE 登入)', 'en') == \
            'Showing your member barcode requires your PIN (you logged in with LINE at the store)'
        assert '店頭で LINE ログイン' in tr('下載個人資料需輸入 PIN 碼確認身分 (您是在門市以 LINE 登入)', 'ja')
        assert tr('送單失敗：餐點【牛肉堡】已售完，請移除後再結帳！', 'en') == \
            'Submission failed: "牛肉堡" is sold out; please remove it before checking out.'
        # 沒有對照的原因仍原樣代入，不會出錯
        assert tr('送單失敗：未知原因', 'en') == 'Submission failed: 未知原因'
        en = tr('您在 A1 桌：點餐後餐點會送到桌上，用餐後請至櫃檯結帳。', 'en')
        assert en.startswith('You are at table A1:') and _no_chinese(en)
        assert tr('您在 A1 桌：點餐後餐點會送到桌上，用餐後請至櫃檯結帳。', 'ja').startswith('テーブル A1 のお席です')


def test_b489_prepaid_cup_shortage_with_suffix(app):
    with app.test_request_context('/'):
        tr = i18n_service.translate_message
        msg = '寄杯「美式」剩餘 1 次，不足兌換 2 份 (可能剛在其他裝置或門市使用)'
        en = tr(msg, 'en')
        assert en.startswith('Prepaid "美式" has 1 left; not enough for 2') and 'another device' in en
        assert tr(msg, 'ja').startswith('回数券「美式」は残り 1 回で、2 点分に足りません')
        assert tr('寄杯「美式」剩餘 1 次，不足兌換 2 份', 'en') == 'Prepaid "美式" has 1 left; not enough for 2'


def test_cross_file_requests_ui_keys_exist():
    """其他小組的樣板 / JS 引用的新鍵都有三種語言 (英文不夾中文)"""
    keys = ('tp_processing', 'tp_processing_text', 'tp_remaining_a', 'tp_remaining_b', 'pr_cancelled_unpaid',
            'pr_refunded', 'payment_unpaid', 'payment_settled', 'off_member', 'off_invoice', 'addon_sold_out_removed',
            'pass_qty_adjusted', 'pickup_slots_refreshed', 'no_addons', 'promo_verify_failed',
            'reg_birthday_incomplete', 'reg_weak_pin', 'rp_timeout_a', 'offline_banner_queue', 'logout_retrying',
            'rs_dep_pending', 'rs_dep_paid', 'rs_dep_refunded', 'rs_dep_forfeited', 'rs_dep_applied',
            'rs_dep_expired', 'rs_dep_refund_failed', 'rs_dep_refunding', 'pay_online_hint', 'reg_line_bind_register')
    for key in keys:
        langs = i18n_service.UI[key]
        assert langs['zh'] and langs['en'] and _no_chinese(langs['en']), key
    assert '失敗' not in i18n_service.UI['rs_dep_refund_failed']['zh']
    assert i18n_service.UI['rw_type_delete']['en'] == 'Type DELETE to confirm'
    assert '寄杯' in i18n_service.UI['rw_delete_text']['zh'] and 'prepaid' in i18n_service.UI['rw_delete_text']['en']


def test_cross_file_requests_backend_messages(app):
    with app.test_request_context('/'):
        tr = i18n_service.translate_message
        for msg in ('請輸入優惠代碼！', '無效的優惠代碼！', '此為會員專屬優惠券，請先登入會員！',
                    '此為會員專屬優惠券，請先於 POS 綁定會員！', '該會員尚未持有此優惠券，或該券已核銷！',
                    '此優惠代碼限會員使用，請先登入會員！', '此優惠代碼限會員使用，請先於 POS 綁定會員！',
                    '未達使用門檻！需消費滿 $300 元才可折抵。', '輸入錯誤的優惠代碼次數過多，請 5 分鐘後再試！',
                    '此優惠代碼每位會員限用 2 次，已達使用上限！', '此優惠代碼活動已結束 (2026/01/31 止)！',
                    '活動尚未開始 (2026/12/01 起)，無法使用！', '此優惠券已過期，無法使用！', '活動尚未開始，無法使用！',
                    '❌ 此優惠券活動尚未開始 (2026/12/01 起)，暫時無法兌換！',
                    '✅ 已開啟優惠活動訊息', '✅ 已關閉優惠活動訊息，之後不會再收到行銷優惠券與推播',
                    '此優惠券未設定兌換點數，暫時無法兌換！', '加料資料格式不正確，請重新選擇！', '已退還 30 點數',
                    '您是以人臉辨識登入：綁定 LINE 前請先登出，改以手機 + PIN 登入後再綁定', '本店註冊不需簡訊驗證',
                    '您還有未用完的寄杯，請先使用完畢或洽櫃檯辦理退費後再刪除帳號', '找不到您的點餐，可能已被主揪移除',
                    '這一桌未結帳的訂單太多，無法線上結帳，請至櫃檯結帳', '請在確認欄輸入「刪除」兩個字',
                    '您原本選擇的取餐分店目前暫停線上點餐，取餐分店已改為【台北店】，請確認後再結帳',
                    '您目前以「好吃店」員工身分登入，無法開啟其他店家的團購連結；請先登出或改用無痕視窗開啟'):
            en = tr(msg, 'en')
            assert en != msg and _no_chinese(en.replace('台北店', '').replace('好吃店', '')), msg
            assert tr(msg, 'ja') != msg, msg
        assert tr('已套用「85 折」，折抵 $30 元！', 'en') == 'Applied "15% off": $30 off!'
        assert tr('已套用「現折 $50」，折抵 $50 元！', 'ja') == '「$50 引き」を適用し、$50 割引しました！'
        assert tr('🎉 成功兌換【紅茶】！', 'en') == '🎉 Redeemed "紅茶"!'
        assert tr('餐點【紅茶】目前不供應 (供應時段 11:00~14:00)，暫時無法兌換！', 'en') == \
            '"紅茶" is not available now (Available 11:00–14:00) and cannot be redeemed.'
        url = 'https://example.com/g/shop/abc?entry=xyz'
        en = tr(f'此瀏覽器最多記住 20 筆點餐，「小明」已不在「我的點餐」中，要修改請用個人修改連結：{url}', 'en')
        assert en.startswith('This browser remembers up to 20 orders; 小明') and en.endswith(url)
