"""測試基礎設施的回歸測試：TestConfig 固定雲端 AI / Webhook 設定、noon_clock 固定實際讀時鐘的訂單模組"""


def test_testconfig_pins_cloud_ai_and_webhook_settings(app):
    """shell 匯出 AI_API_KEYS / GEMINI_MODEL / WEBHOOK_ALLOW_* 時，測試仍不可拿真實金鑰呼叫雲端或放寬 SSRF 防護"""
    assert app.config['AI_API_KEYS'] == '' and app.config['GEMINI_API_KEY'] == ''
    assert app.config['GEMINI_MODEL'] == '' and app.config['GEMINI_FALLBACK_MODEL'] == ''
    assert app.config['WEBHOOK_ALLOW_PRIVATE'] is False and app.config['WEBHOOK_ALLOW_HTTP'] is False
    from app.services import ai_service
    with app.test_request_context('/'):
        assert [c.source for c in ai_service.credentials() if c.source.startswith('env:')] == []


def test_noon_clock_freezes_order_modules(noon_clock):
    """order_service 只是轉匯出入口：換日結案 / 今天範圍 / 取餐號實際用的是 order_lifecycle / order_stock 的時鐘"""
    from app.services import analytics_service, order_checkout, order_lifecycle, order_stock
    start, end = order_lifecycle.todays_range()
    assert start <= noon_clock < end and start.date() == noon_clock.date()
    assert order_lifecycle.datetime.now() == noon_clock
    assert order_checkout.datetime.now() == noon_clock
    assert order_stock.datetime.now() == noon_clock and order_stock.business_day() == noon_clock.date()   # 取餐號的營業日
    assert analytics_service.date.today() == noon_clock.date()
