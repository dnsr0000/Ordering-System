"""Alembic 環境 (Flask-Migrate)。同一份遷移腳本同時支援本機 SQLite 與線上 PostgreSQL。"""
import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app
from sqlalchemy import event

config = context.config
if config.config_file_name and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

db = current_app.extensions['migrate'].db
config.set_main_option('sqlalchemy.url', db.engine.url.render_as_string(hide_password=False).replace('%', '%%'))


def run_migrations_offline():
    context.configure(url=config.get_main_option('sqlalchemy.url'), target_metadata=db.metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _sqlite_begin(conn):
    """SQLite 遷移連線開始交易時真正送出 BEGIN (讓 DDL 也在交易內)"""
    conn.exec_driver_sql('BEGIN')


def run_migrations_online():
    def skip_empty_autogenerate(ctx, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False) and directives[0].upgrade_ops.is_empty():
            directives[:] = []

    with db.engine.connect() as connection:
        is_sqlite = connection.dialect.name == 'sqlite'
        driver_conn = saved_isolation = None
        if is_sqlite:
            # SQLite 修改欄位需重建資料表 (batch mode)；重建期間暫停外鍵檢查，避免舊資料的孤兒參照中斷遷移
            # (PRAGMA foreign_keys 在交易中無效，必須在下面改成明確 BEGIN 之前執行)
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            connection.commit()
            # pysqlite 只在 INSERT / UPDATE / DELETE 前才自動 BEGIN，CREATE / ALTER / DROP 會在交易外立即生效：
            # 改成由這裡明確送 BEGIN，每個遷移 (連同 alembic_version 的更新) 整個成功或整個復原，
            # 升級中途斷電、關視窗或出錯，下次重跑不會卡在「欄位 / 資料表已存在」
            driver_conn = connection.connection.driver_connection
            saved_isolation = driver_conn.isolation_level
            driver_conn.isolation_level = None
            event.listen(connection, 'begin', _sqlite_begin)
        try:
            # Flask-Migrate 預設 render_as_batch=True：SQLite 以重建資料表方式修改欄位，PostgreSQL 直接 ALTER
            context.configure(
                connection=connection,
                target_metadata=db.metadata,
                process_revision_directives=skip_empty_autogenerate,
                **current_app.extensions['migrate'].configure_args,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            # 遷移失敗 (例如還原舊備份後升級) 也要恢復外鍵檢查：這條連線會回到連線池給之後的請求用，
            # 外鍵關著時 ON DELETE CASCADE / SET NULL 都不會執行，刪訂單 / 會員會留下孤兒資料
            if is_sqlite:
                try:
                    if connection.in_transaction():
                        connection.rollback()
                    # 連線回到連線池前恢復 pysqlite 原本的交易模式 (一般請求不受影響)
                    if driver_conn is not None:
                        event.remove(connection, 'begin', _sqlite_begin)
                        driver_conn.isolation_level = saved_isolation
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                except Exception:  # noqa: BLE001  恢復不了就讓連線作廢，不要帶著關閉的外鍵回到連線池
                    connection.invalidate()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
