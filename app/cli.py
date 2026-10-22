"""Flask CLI 指令：flask --app run <指令>"""
import json

import click
from flask import Flask
from flask.cli import AppGroup


def register_cli(app: Flask):
    @app.cli.command('setup')
    def setup_command():
        """升級資料庫到最新結構並建立預設資料 (部署時在啟動伺服器前執行；可重複執行)"""
        from app.services.db_migrate import upgrade_database
        upgrade_database()
        click.echo('資料庫已是最新結構')

    sync_cli = AppGroup('sync', help='線上 / 分店同步')

    @sync_cli.command('create-node')
    @click.option('--tenant', 'slug', default='default', show_default=True, help='店家代號 (slug)')
    @click.option('--name', required=True, help='分店名稱')
    def create_node_command(slug, name):
        """(線上端) 新增分店並顯示權杖"""
        from app.models.tenant import Tenant
        from app.sync.nodes import create_node
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            raise click.ClickException(f'找不到店家 {slug}')
        node, token = create_node(tenant.id, name)
        click.echo(f'已建立分店「{node.name}」({node.uuid})')
        click.echo(f'權杖 (只顯示這一次，請設定到分店的 SYNC_NODE_TOKEN)：{token}')

    @sync_cli.command('now')
    def sync_now_command():
        """(分店) 立即同步一次"""
        from app.sync import runtime
        from app.sync.node import NodeSync, make_transport
        if not runtime.is_node():
            raise click.ClickException('只有分店 (SYNC_ROLE=node) 可以執行同步')
        pushed, pulled = NodeSync(make_transport(app)).run_once()
        click.echo(f'同步完成：上傳 {pushed} 筆、下載 {pulled} 筆')

    @sync_cli.command('status')
    def sync_status_command():
        """(分店) 顯示同步狀態"""
        from app.sync.node import sync_status
        click.echo(json.dumps(sync_status(), ensure_ascii=False, indent=2))

    app.cli.add_command(sync_cli)

    privacy_cli = AppGroup('privacy', help='個人資料保護')

    @privacy_cli.command('purge-inactive')
    @click.option('--tenant', 'slug', default='default', show_default=True, help='店家代號 (slug)')
    @click.option('--apply', 'apply_', is_flag=True, help='實際刪除 (未加時只列出名單)')
    def purge_inactive_command(slug, apply_):
        """刪除停止使用超過「隱私權告知設定」月數的會員 (多分店時請在線上端執行，刪除會同步到各分店)"""
        from app.models.tenant import Tenant
        from app.services import privacy_service
        from app.sync import runtime
        if runtime.is_node() and apply_:
            raise click.ClickException('分店只看得到自己的訂單紀錄，請在線上端執行刪除')
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            raise click.ClickException(f'找不到店家 {slug}')
        months = privacy_service.notice_context(tenant.id)['retention_months']
        users = privacy_service.purge_inactive_members(tenant.id, months, dry_run=not apply_)
        for u in users:
            click.echo(f'#{u.id} {u.name} {u.phone[:4]}****{u.phone[-2:]}')
        click.echo(f'{"已刪除" if apply_ else "將刪除 (加上 --apply 執行)"} {len(users)} 位停止使用超過 {months} 個月的會員')

    app.cli.add_command(privacy_cli)

    tables_cli = AppGroup('tables', help='門市桌位與掃碼點餐')

    @tables_cli.command('check')
    @click.option('--stale-hours', default=24, show_default=True, help='未結帳超過幾小時列為「可能忘了結帳」')
    @click.option('--fix', is_flag=True, help='自動修正可修正的項目 (已取消卻已收款 → 需退款)')
    def tables_check_command(stale_hours, fix):
        """檢查桌邊點餐的資料衝突：沒有桌位的未結帳訂單、已取消卻已收款、跨店桌位、久未結帳。有問題時結束代碼為 1"""
        from app.services import table_service
        found = table_service.find_conflicts(stale_hours)
        for item in found:
            click.echo(f"[{item['kind']}] 店家 {item['tenant_id']}：{item['message']}")
        if fix and found:
            click.echo(f'已自動修正 {table_service.fix_conflicts(found)} 筆 (其餘請人工確認)')
        if not found:
            click.echo('沒有發現衝突')
        raise SystemExit(1 if found and not fix else 0)

    app.cli.add_command(tables_cli)

    payments_cli = AppGroup('payments', help='線上付款')

    @payments_cli.command('expire')
    def expire_payments_command():
        """(線上端) 取消逾時未付款的線上訂單 (伺服器也會在背景每分鐘執行)"""
        from app.services.payment_ecpay import expire_unpaid_orders
        click.echo(f'取消 {expire_unpaid_orders(force=True)} 張逾時未付款的訂單')

    app.cli.add_command(payments_cli)
