"""端對端：觸控點餐機在待機輪播時，喚醒的那一下不可同時點到底下的按鈕"""
import pytest

from tests.support import seed_demo

pytestmark = pytest.mark.e2e


def test_touch_wake_does_not_click_through(app, browser, live_url, store_settings):
    import os
    from tests.support import jpeg_bytes, make_item
    seed_demo(app)
    with open(os.path.join(app.config['UPLOAD_FOLDER_MENU'], 'e2e_touch.jpg'), 'wb') as fh:
        fh.write(jpeg_bytes().getvalue())
    make_item(app, 'E2E 觸控特餐', price=88, image_path='e2e_touch.jpg', is_recommended=True)
    store_settings('menu', attract_seconds=2, attract_message='摸我開始點餐')
    ctx = browser.new_context(has_touch=True, viewport={'width': 800, 'height': 1000}, locale='zh-TW')
    page = ctx.new_page()
    page.goto(f'{live_url}/?shop=default')
    overlay = page.locator('#attractOverlay')
    overlay.wait_for(state='visible', timeout=8000)
    btn = page.get_by_role('button', name='訪客直接點餐')
    box = btn.bounding_box()
    url_before = page.url
    page.touchscreen.tap(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.wait_for_timeout(800)
    assert overlay.is_hidden()
    assert page.url == url_before, f'喚醒的那一下點到了底下的按鈕：{page.url}'
    page.touchscreen.tap(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.locator('#menuList').wait_for(timeout=8000)   # 喚醒後再點一次要正常進入點餐
    ctx.close()
