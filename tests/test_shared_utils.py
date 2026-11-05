"""app/utils.py 共用的小工具：日期 / 時間解析、分頁、ETag、Excel 防公式注入、門市 / 線上端限定裝飾器"""
from datetime import date, datetime, timedelta

import pytest
from openpyxl import Workbook, load_workbook

from app.errors import AppError
from app.models.tenant import Tenant
from app.sync import runtime as sync_runtime
from app.utils import (day_bounds, hub_only, json_etag_response, paginate, parse_date, parse_date_range, parse_hm,
                       parse_local_dt, sha256_hex, store_only, workbook_bytes)


# ---------------------------------------------------------------- 日期 / 時間
def test_parse_date():
    assert parse_date('2026-03-05') == date(2026, 3, 5)
    assert parse_date(' 2026-03-05 ') == date(2026, 3, 5)
    assert parse_date('') is None and parse_date(None) is None
    with pytest.raises(AppError, match='請填寫生日'):
        parse_date('', '生日', required=True)
    with pytest.raises(AppError, match='生日格式不正確：2026/13/40'):
        parse_date('2026/13/40', '生日')


def test_parse_date_range_defaults_and_swap(app):
    today = date.today()
    with app.test_request_context('/?start=&end='):
        assert parse_date_range() == (today - timedelta(days=6), today)
        assert parse_date_range(default_days=30) == (today - timedelta(days=29), today)
    with app.test_request_context('/?start=2026-02-10&end=2026-02-01'):
        assert parse_date_range() == (date(2026, 2, 1), date(2026, 2, 10))
    with app.test_request_context('/?start=bad&end=2026-02-01'):   # 格式錯誤：預設 (不嚴格)
        start, end = parse_date_range()
        assert (start, end) == tuple(sorted((today - timedelta(days=6), date(2026, 2, 1))))


def test_parse_date_range_week(app):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    with app.test_request_context('/'):
        assert parse_date_range(week=True) == (monday, monday + timedelta(days=6))
    with app.test_request_context('/?start=2026-03-02'):
        assert parse_date_range(week=True) == (date(2026, 3, 2), date(2026, 3, 8))


def test_parse_date_range_presets(app):
    today = date.today()
    with app.test_request_context('/?preset=today'):
        assert parse_date_range(presets=True) == (today, today)
        assert parse_date_range() == (today - timedelta(days=6), today)   # presets=False 時忽略
    with app.test_request_context('/?preset=yesterday'):
        assert parse_date_range(presets=True) == (today - timedelta(days=1), today - timedelta(days=1))
    with app.test_request_context('/?preset=month'):
        assert parse_date_range(presets=True) == (today.replace(day=1), today)
    with app.test_request_context('/?preset=last_month'):
        start, end = parse_date_range(presets=True)
        assert start.day == 1 and end == today.replace(day=1) - timedelta(days=1) and start.month == end.month


def test_parse_date_range_strict_and_max_days(app):
    with app.test_request_context('/?start=2026-13-01&end=2026-02-01'):
        with pytest.raises(AppError, match='日期格式錯誤'):
            parse_date_range(strict=True)
    with app.test_request_context('/?start=2024-01-01&end=2026-02-01'):
        with pytest.raises(AppError, match='查詢區間最長 400 天'):
            parse_date_range(max_days=400)
        assert parse_date_range() == (date(2024, 1, 1), date(2026, 2, 1))


def test_day_bounds():
    assert day_bounds(date(2026, 3, 5)) == (datetime(2026, 3, 5), datetime(2026, 3, 6))
    assert day_bounds(date(2026, 3, 5), date(2026, 3, 7)) == (datetime(2026, 3, 5), datetime(2026, 3, 8))


def test_parse_local_dt():
    assert parse_local_dt('2026-03-05T18:30') == datetime(2026, 3, 5, 18, 30)
    assert parse_local_dt('2026-03-05T18:30:45') == datetime(2026, 3, 5, 18, 30)   # 秒數忽略
    assert parse_local_dt('') is None and parse_local_dt('nope') is None
    with pytest.raises(AppError, match='取餐時間格式不正確'):
        parse_local_dt('nope', '取餐時間')


def test_parse_hm():
    assert parse_hm('09:30') == (9, 30)
    assert parse_hm('bad', '10:00') == (10, 0)
    assert parse_hm('25:00', '10:00') == (10, 0)   # 超出範圍視為格式錯誤
    assert parse_hm(None) is None and parse_hm('x', 'y') is None


# ---------------------------------------------------------------- 分頁 / 雜湊
def test_paginate(app):
    with app.test_request_context('/?page=1'):
        query = Tenant.query.order_by(Tenant.id)
        total = query.count()
        rows, page, pages, got_total = paginate(query, per_page=1)
        assert (page, pages, got_total, len(rows)) == (1, max(1, total), total, min(1, total))
    with app.test_request_context('/?page=999'):   # 超過頁數夾回最後一頁
        rows, page, pages, _ = paginate(Tenant.query.order_by(Tenant.id), per_page=1)
        assert page == pages
    with app.test_request_context('/?page=abc'):
        assert paginate(Tenant.query, per_page=50)[1] == 1


def test_sha256_hex():
    assert sha256_hex('abc') == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert sha256_hex(None) == sha256_hex('')


# ---------------------------------------------------------------- 回應
def test_json_etag_response_304(app):
    body = '{"a":1}'
    with app.test_request_context('/'):
        resp = json_etag_response(body)
        assert resp.status_code == 200 and resp.get_data(as_text=True) == body
        assert resp.mimetype == 'application/json' and resp.headers['Cache-Control'] == 'no-cache'
        etag = resp.headers['ETag']
    with app.test_request_context('/', headers={'If-None-Match': etag}):
        resp = json_etag_response(body)
        assert resp.status_code == 304 and resp.headers['ETag'] == etag and not resp.get_data()
    with app.test_request_context('/', headers={'If-None-Match': etag}):
        assert json_etag_response('{"a":2}').status_code == 200


def test_workbook_bytes_neutralizes_formulas():
    wb = Workbook()
    ws = wb.active
    ws.append(['=HYPERLINK("http://evil")', 'ok', 3])
    assert ws['A1'].data_type == 'f'
    buf = workbook_bytes(wb)
    assert buf.tell() == 0
    ws2 = load_workbook(buf).active
    assert ws2['A1'].value == '=HYPERLINK("http://evil")' and ws2['A1'].data_type == 's'
    assert ws2['C1'].value == 3


# ---------------------------------------------------------------- 門市 / 線上端限定
def test_store_only_and_hub_only(app, monkeypatch):
    from werkzeug.exceptions import NotFound

    @store_only
    def store_page():
        return 'store'

    @hub_only
    def hub_page():
        return 'hub'

    with app.test_request_context('/'):
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
        assert store_page() == 'store'
        with pytest.raises(NotFound):
            hub_page()
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
        assert hub_page() == 'hub'
        with pytest.raises(NotFound):
            store_page()
    assert store_page.__name__ == 'store_page'
