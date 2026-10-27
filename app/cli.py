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

    ops_cli = AppGroup('ops', help='維運：發票重送、每日排程、備份、外部服務連線檢查')

    @ops_cli.command('retry-invoices')
    def retry_invoices_command():
        """重送待開立的電子發票 (伺服器背景每 5 分鐘也會執行)"""
        from app.services.einvoice_ecpay import retry_pending
        click.echo(f'開立成功 {retry_pending(older_than_seconds=0, limit=200)} 張')

    @ops_cli.command('daily')
    def daily_command():
        """立即執行今天到期的每日工作 (自動備份、生日禮、定期報表)"""
        from app.services.maintenance_service import run_daily_jobs
        done = run_daily_jobs()
        click.echo('已執行：' + ('、'.join(done) if done else '沒有到期的工作'))

    @ops_cli.command('backup')
    @click.option('--keep-days', default=14, show_default=True)
    def backup_command(keep_days):
        """立即備份門市資料庫 (SQLite) 到 database/backups"""
        from app.services.maintenance_service import run_sqlite_backup
        path = run_sqlite_backup(keep_days)
        if path is None:
            raise click.ClickException('只有 SQLite 可以使用此指令；PostgreSQL 請用 deploy/pg_backup.sh')
        click.echo(f'已備份：{path}')

    @ops_cli.command('check-integrations')
    @click.option('--tenant', 'slug', default='default', show_default=True)
    def check_integrations_command(slug):
        """以目前的設定測試各項外部服務 (電子發票、LINE Pay、LINE、簡訊、寄信、外送平台、監控)"""
        from flask import g
        from app.errors import AppError
        from app.models.tenant import Tenant
        from app.services import settings_service
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            raise click.ClickException(f'找不到店家 {slug}')
        with app.test_request_context('/'):
            g.tenant, g.tenant_id = tenant, tenant.id
            for key, tester in settings_service.TESTERS.items():
                title = settings_service.SECTIONS[key].title
                if settings_service.SECTIONS[key].enabled_field and not settings_service.is_enabled(key, tenant.id):
                    click.echo(f'- {title}：未啟用，略過')
                    continue
                try:
                    click.echo(f'✔ {title}：{tester(settings_service.get(key, tenant.id))}')
                except AppError as exc:
                    click.echo(f'✘ {title}：{exc.message}')
                except Exception as exc:  # noqa: BLE001
                    click.echo(f'✘ {title}：{exc}')

    app.cli.add_command(ops_cli)

    demo_cli = AppGroup('demo', help='展示用的示範店家 (不影響其他店家)')

    @demo_cli.command('seed')
    @click.option('--slug', default='demo', show_default=True, help='示範店家的網址代號')
    @click.option('--days', default=60, show_default=True, help='產生過去幾天的營運資料')
    @click.option('--orders-per-day', default=80, show_default=True, help='平日每天大約幾張訂單 (假日多 35%)')
    @click.option('--seed', 'seed_value', default=42, show_default=True, help='亂數種子 (相同種子產生相同資料)')
    @click.option('--password', default=None, help='示範帳號的密碼 (未指定時隨機產生並顯示)')
    @click.option('--reset', is_flag=True, help='示範店家已存在時刪除後重新建立')
    def demo_seed_command(slug, days, orders_per_day, seed_value, password, reset):
        """建立示範店家與過去 N 天的訂單、會員、成本、費用、打卡等資料 (所有報表都有內容)"""
        from app.services import demo_service
        if password is not None and len(password) < 10:
            raise click.ClickException('密碼至少 10 個字元')
        try:
            result = demo_service.seed(slug=slug, days=days, orders_per_day=orders_per_day, seed_value=seed_value,
                                       password=password, reset=reset, progress=click.echo)
        except demo_service.DemoError as exc:
            raise click.ClickException(exc.message)
        click.echo(f"示範店家「示範店家」已建立：網址 /shop/{result['slug']}，共 {result['orders']} 張訂單")
        click.echo(f"帳號：{'、'.join(result['accounts'])} (負責人 {result['owner']})")
        click.echo(f"密碼 (所有示範帳號相同)：{result['password']}")
        click.echo(f"示範會員：手機 0900000001 ~ 09000000{result['members']:02d}，PIN {demo_service.MEMBER_PIN}")

    @demo_cli.command('remove')
    @click.option('--slug', default='demo', show_default=True, help='示範店家的網址代號')
    @click.option('--yes', is_flag=True, help='不再詢問直接刪除')
    def demo_remove_command(slug, yes):
        """刪除示範店家與它的所有資料 (只會刪除由 demo seed 建立的店家)"""
        from app.services import demo_service
        if not yes:
            click.confirm(f'確定刪除示範店家「{slug}」的所有資料？', abort=True)
        try:
            n = demo_service.remove(slug)
        except demo_service.DemoError as exc:
            raise click.ClickException(exc.message)
        click.echo(f'已刪除 {n} 筆資料' if n else f'找不到示範店家「{slug}」')

    app.cli.add_command(demo_cli)
