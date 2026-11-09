"""跨檔請求 settings-2：系統設定表單樣板的字數上限 (R2-119) 與只顯示欄位 (B-330)"""
import re


def _tag(html, name):
    m = re.search(r'<(?:input|textarea)[^>]*\bname="%s"[^>]*>' % re.escape(name), html)
    assert m, name
    return m.group(0)


def test_text_fields_show_server_max_length(admin_client):
    html = admin_client.get('/admin/settings/chatbot').get_data(as_text=True)
    assert 'maxlength="10000"' in _tag(html, 'faq'), 'textarea 用欄位自訂上限'
    assert 'maxlength="300"' in _tag(html, 'bot_name'), '一般文字欄位預設 300 字'
    assert 'maxlength' not in _tag(html, 'llm_enabled')
    html = admin_client.get('/admin/settings/feedback').get_data(as_text=True)
    assert 'maxlength' not in _tag(html, 'google_review_url'), '網址欄位伺服器端另有 500 字規則'
    assert 'maxlength' not in _tag(html, 'review_min_rating'), '數字欄位不加字數上限'


def test_order_intake_runtime_state_is_display_only(admin_client):
    html = admin_client.get('/admin/settings/order_intake').get_data(as_text=True)
    assert 'disabled' in _tag(html, 'paused')
    resume = _tag(html, 'resume_at')
    assert 'readonly' in resume and 'disabled' in resume
    msg = _tag(html, 'message')
    assert 'disabled' not in msg and 'readonly' not in msg, '其他欄位仍可編輯'
    assert 'disabled' not in _tag(html, 'window_minutes')
