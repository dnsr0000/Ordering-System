"""POS「快速填入桌號 / 稱呼」標籤：有標題、用桌位管理的桌號 + 收銀規則設定的稱呼，去重並可關閉"""
from app.extensions import db
from app.models.table import DiningTable
from tests.support import default_tenant_id


def test_pos_quick_table_chips(app, staff_client, store_settings):
    tid = default_tenant_id(app)
    with app.app_context():
        # 其他測試建立的桌位先停用 (快捷鍵最多 16 個，避免被擠掉)，測試結束還原
        others = [t.id for t in DiningTable.query.filter_by(tenant_id=tid, is_active=True).all()
                  if not t.name.startswith('快')]
        DiningTable.query.filter(DiningTable.id.in_(others)).update({'is_active': False}, synchronize_session=False)
        for name, active, order in (('快A1', True, 1), ('快B2', True, 2), ('快停用', False, 3)):
            row = DiningTable.query.filter_by(tenant_id=tid, name=name).first()
            if row is None:
                db.session.add(DiningTable(tenant_id=tid, name=name, is_active=active, sort_order=order))
            else:
                row.is_active, row.sort_order = active, order
        db.session.commit()
    try:
        _run(app, staff_client, store_settings, sold=None)
    finally:
        with app.app_context():
            DiningTable.query.filter(DiningTable.id.in_(others)).update({'is_active': True}, synchronize_session=False)
            db.session.commit()


def _run(app, staff_client, store_settings, sold):
    store_settings('pos', quick_tables='外帶客, 電話預訂, 快A1')
    page = staff_client.get('/pos').get_data(as_text=True)
    assert '快速填入桌號 / 稱呼' in page and 'id="posQuickTables"' in page
    chips = [p.split('">', 1)[0] for p in page.split('data-action="setTable" data-value="')[1:]]
    assert chips.index('快A1') < chips.index('快B2') < chips.index('外帶客'), '桌號在前、稱呼在後'
    assert chips.count('快A1') == 1 and '快停用' not in chips and '電話預訂' in chips
    assert 'A2' not in chips, '不再出現寫死的 A1~B2'
    store_settings('pos', quick_tables='')
    page = staff_client.get('/pos').get_data(as_text=True)
    assert '外帶客' not in page.split('posQuickTables')[1].split('</div>')[0]
