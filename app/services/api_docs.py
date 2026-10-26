"""
對外 API 說明文件 (第四輪)：/developers 頁面與 /developers/openapi.json (OpenAPI 3.0)。
端點清單在這裡宣告一次，頁面、OpenAPI 規格與「試打」表單都由此產生；新增 API 時請一併更新 ENDPOINTS
(tests/test_expenses_pnl_cost_history.py 會比對 app/routes/api_v1.py 的實際路由，漏寫會失敗)。
"""
from app.models.integration import API_SCOPES, WEBHOOK_EVENTS

ORDER_SCHEMA = {
    'type': 'object',
    'properties': {
        'uuid': {'type': 'string', 'description': '訂單唯一識別碼 (跨分店不重複)'},
        'id': {'type': 'integer', 'description': '本機流水號 (各分店各自編號，請以 uuid 為準)'},
        'pickup_number': {'type': 'string', 'description': '取餐號碼'},
        'status': {'type': 'string', 'enum': ['pending', 'preparing', 'ready', 'completed', 'cancelled']},
        'payment_status': {'type': 'string', 'description': 'paid / unpaid / awaiting_payment / refunded …'},
        'payment_method': {'type': 'string'},
        'order_type': {'type': 'string', 'description': 'dine_in / takeout / delivery'},
        'order_source': {'type': 'string', 'description': 'kiosk / pos / online / table / delivery …'},
        'total_price': {'type': 'integer'}, 'subtotal': {'type': 'integer'}, 'discount_amount': {'type': 'integer'},
        'note': {'type': 'string'}, 'table': {'type': 'string'},
        'node_uuid': {'type': 'string', 'description': '下單的分店 (線上端 / 單機為空字串)'},
        'created_at': {'type': 'string', 'format': 'date-time'},
        'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'quantity': {'type': 'integer'}, 'price': {'type': 'integer'},
            'customization': {'type': 'string'}, 'voided_qty': {'type': 'integer'}}}},
    },
}
MENU_ITEM_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'}, 'uuid': {'type': 'string'}, 'name': {'type': 'string'}, 'category': {'type': 'string'},
        'price': {'type': 'integer', 'description': '目前售價 (含特價)'},
        'prices': {'type': 'object', 'description': '各通路價格：dine_in / takeout / online / delivery'},
        'stock': {'type': 'integer'}, 'is_sold_out': {'type': 'boolean'}, 'is_available': {'type': 'boolean'},
        'branch_hidden': {'type': 'boolean', 'description': '本分店已下架 (分店主機才可能為 true)'},
        'external_code': {'type': 'string', 'description': '外部系統代碼 (ERP 料號)'},
    },
}

# (方法, 路徑, 權限, 摘要, 說明, 參數 [(名稱, 位置, 必填, 說明, 範例)], 請求本文範例, 回應 schema 名稱)
ENDPOINTS = (
    ('GET', '/api/v1/orders', 'orders:read', '訂單列表 (依時間)',
     '依建立時間排序。回應的 next 有值時，帶入 since 與 after_id 取下一頁 (同一時間的訂單不會漏)。',
     [('since', 'query', False, '起始時間 (不含)，ISO 8601', '2026-10-01T00:00:00'),
      ('after_id', 'query', False, '與 since 一起使用：since 同一時間、id 大於此值的訂單', ''),
      ('until', 'query', False, '結束時間 (含)', ''),
      ('status', 'query', False, '只列此狀態', ''),
      ('limit', 'query', False, '每頁筆數 1 ~ 500，預設 100', '50')], None, 'OrderList'),
    ('GET', '/api/v1/orders?cursor', 'orders:read', '訂單異動追蹤 (cursor)',
     '第一次帶 cursor=0；回傳新增或狀態 / 付款改變的訂單 (依本機提交順序，含之後才同步進來的分店訂單)，'
     '把 next_cursor 存起來，下次帶入就只拿到之後的異動。has_more 為 true 時請立即再呼叫。',
     [('cursor', 'query', True, '上次回傳的 next_cursor，第一次填 0', '0'),
      ('limit', 'query', False, '每頁筆數 1 ~ 500', '100')], None, 'OrderFeed'),
    ('GET', '/api/v1/orders/{uuid}', 'orders:read', '單筆訂單', '以訂單 uuid 查詢。',
     [('uuid', 'path', True, '訂單 uuid', '')], None, 'OrderOne'),
    ('GET', '/api/v1/menu', 'menu:read', '菜單與庫存',
     '全部餐點 (含已下架 / 售完)。分店主機回傳本分店的價格與下架狀態。', [], None, 'Menu'),
    ('POST', '/api/v1/menu/{item_id}/availability', 'menu:write', '更新售完 / 庫存',
     '可只傳其中一個欄位；sold_out=true 立即在點餐機顯示售完。',
     [('item_id', 'path', True, '餐點 id (GET /api/v1/menu 的 id)', '')],
     {'sold_out': False, 'stock': 20}, 'Availability'),
    ('GET', '/api/v1/members/{phone}', 'members:read', '查詢會員',
     '以手機號碼查詢姓名、點數、等級、儲值金。', [('phone', 'path', True, '手機號碼', '0912345678')], None, 'Member'),
    ('GET', '/api/v1/reports/daily', 'reports:read', '每日營業統計',
     '當日訂單數、淨營業額、作廢數、折扣、客單價、各付款方式金額、目前待處理訂單數。',
     [('date', 'query', False, 'YYYY-MM-DD，預設今天', '')], None, 'Daily'),
)

RESPONSES = {
    'OrderList': {'type': 'object', 'properties': {
        'success': {'type': 'boolean'}, 'orders': {'type': 'array', 'items': {'$ref': '#/components/schemas/Order'}},
        'next': {'type': 'object', 'nullable': True, 'properties': {'since': {'type': 'string'}, 'after_id': {'type': 'integer'}}},
        'next_since': {'type': 'string', 'nullable': True}}},
    'OrderFeed': {'type': 'object', 'properties': {
        'success': {'type': 'boolean'}, 'orders': {'type': 'array', 'items': {'$ref': '#/components/schemas/Order'}},
        'next_cursor': {'type': 'integer'}, 'has_more': {'type': 'boolean'}}},
    'OrderOne': {'type': 'object', 'properties': {'success': {'type': 'boolean'}, 'order': {'$ref': '#/components/schemas/Order'}}},
    'Menu': {'type': 'object', 'properties': {
        'success': {'type': 'boolean'}, 'items': {'type': 'array', 'items': {'$ref': '#/components/schemas/MenuItem'}}}},
    'Availability': {'type': 'object', 'properties': {'success': {'type': 'boolean'}, 'item': {'type': 'object', 'properties': {
        'id': {'type': 'integer'}, 'stock': {'type': 'integer'}, 'is_sold_out': {'type': 'boolean'}}}}},
    'Member': {'type': 'object', 'properties': {'success': {'type': 'boolean'}, 'member': {'type': 'object', 'properties': {
        'uuid': {'type': 'string'}, 'name': {'type': 'string'}, 'phone': {'type': 'string'}, 'points': {'type': 'integer'},
        'tier': {'type': 'string'}, 'wallet_balance': {'type': 'integer'}, 'marketing_opt_out': {'type': 'boolean'}}}}},
    'Daily': {'type': 'object', 'properties': {
        'success': {'type': 'boolean'}, 'date': {'type': 'string', 'format': 'date'}, 'orders': {'type': 'integer'},
        'net': {'type': 'integer'}, 'cancelled': {'type': 'integer'}, 'discounts': {'type': 'integer'},
        'avg_ticket': {'type': 'number'}, 'by_method': {'type': 'object'}, 'pending_orders': {'type': 'integer'}}},
}

ERRORS = (
    (400, '參數錯誤 (例如時間格式)'), (401, '缺少或無效的 API 金鑰'), (403, '金鑰沒有此權限'),
    (404, '查無資料'), (429, '超過每分鐘 120 次的限制'),
)

WEBHOOK_HEADERS = (
    ('X-Kiosk-Event', '事件名稱，例如 order.paid'),
    ('X-Kiosk-Delivery', '這次通知的 id (重送時相同，可用來去除重複)'),
    ('X-Kiosk-Timestamp', '送出時間 (Unix 秒)'),
    ('X-Kiosk-Signature', 'sha256=HMAC-SHA256(簽章密鑰, "時間戳.本文") 的十六進位'),
)

VERIFY_SAMPLE = '''import hashlib, hmac, time

def verify(secret, headers, body: bytes):
    ts = headers['X-Kiosk-Timestamp']
    if abs(time.time() - int(ts)) > 300:          # 拒絕 5 分鐘前的舊通知 (防重放)
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), ts.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers['X-Kiosk-Signature'])'''


def endpoints():
    """頁面與試打表單用的端點清單 (dict)"""
    rows = []
    for method, path, scope, summary, desc, params, body, resp in ENDPOINTS:
        rows.append(dict(method=method, path=path, scope=scope, scope_label=API_SCOPES.get(scope, scope),
                         summary=summary, description=desc, body=body, response=resp,
                         params=[dict(name=n, where=w, required=r, description=d, example=e) for n, w, r, d, e in params],
                         anchor=summary.replace(' ', '')))
    return rows


def openapi_spec(base_url=''):
    paths = {}
    for method, path, scope, summary, desc, params, body, resp in ENDPOINTS:
        key = path.split('?')[0]
        op = {
            'summary': summary, 'description': f'{desc}\n\n需要權限：{scope} ({API_SCOPES.get(scope, "")})',
            'operationId': (method.lower() + key.replace('/api/v1', '').replace('/', '_').replace('{', '').replace('}', '')
                            + ('_feed' if '?cursor' in path else '')),
            'tags': [scope.split(':')[0]], 'security': [{'bearerAuth': []}],
            'parameters': [{'name': n, 'in': w, 'required': bool(r), 'description': d,
                            'schema': {'type': 'integer' if n in ('limit', 'after_id', 'cursor', 'item_id') else 'string'},
                            **({'example': e} if e else {})} for n, w, r, d, e in params],
            'responses': {'200': {'description': '成功', 'content': {'application/json': {
                'schema': {'$ref': f'#/components/schemas/{resp}'}}}},
                **{str(code): {'$ref': '#/components/responses/Error'} for code, _ in ERRORS}},
        }
        if body is not None:
            op['requestBody'] = {'required': True, 'content': {'application/json': {
                'schema': {'type': 'object', 'properties': {'sold_out': {'type': 'boolean'}, 'stock': {'type': 'integer',
                                                                                                         'minimum': 0, 'maximum': 100000}}},
                'example': body}}}
        slot = paths.setdefault(key, {})
        verb = method.lower()
        if verb in slot:   # 同一路徑的第二種用法 (cursor 異動追蹤)：合併到同一個 operation 的說明
            slot[verb]['description'] += f'\n\n另一種用法「{summary}」：{desc}'
            names = {p['name'] for p in slot[verb]['parameters']}
            slot[verb]['parameters'] += [dict(p, required=False) for p in op['parameters'] if p['name'] not in names]
            continue
        slot[verb] = op
    schemas = {'Order': ORDER_SCHEMA, 'MenuItem': MENU_ITEM_SCHEMA, **RESPONSES,
               'Error': {'type': 'object', 'properties': {'success': {'type': 'boolean', 'example': False},
                                                          'message': {'type': 'string'}}},
               'WebhookEvent': {'type': 'object', 'properties': {
                   'id': {'type': 'string'}, 'event': {'type': 'string', 'enum': list(WEBHOOK_EVENTS) + ['ping']},
                   'created_at': {'type': 'string', 'format': 'date-time'}, 'data': {'$ref': '#/components/schemas/Order'}}}}
    return {
        'openapi': '3.0.3',
        'info': {'title': '點餐系統對外 API', 'version': '1.0',
                 'description': '以後台「API 與 Webhook」建立的金鑰呼叫 (Authorization: Bearer kk_...)。'
                                '每把金鑰每分鐘最多 120 次。訂單事件請改用 Webhook 接收即時通知。'},
        'servers': [{'url': base_url.rstrip('/') or '/'}],
        'components': {
            'securitySchemes': {'bearerAuth': {'type': 'http', 'scheme': 'bearer', 'description': 'kk_ 開頭的 API 金鑰'}},
            'schemas': schemas,
            'responses': {'Error': {'description': '錯誤 (' + '、'.join(f'{c} {d}' for c, d in ERRORS) + ')',
                                    'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}},
        },
        'paths': paths,
        'x-webhooks': {e: {'description': label, 'headers': dict(WEBHOOK_HEADERS),
                           'payload': {'$ref': '#/components/schemas/WebhookEvent'}} for e, label in WEBHOOK_EVENTS.items()},
    }
