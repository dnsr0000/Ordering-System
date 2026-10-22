"""Alembic 環境 (Flask-Migrate)。同一份遷移腳本同時支援本機 SQLite 與線上 PostgreSQL。"""
import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app

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


def run_migrations_online():
    def skip_empty_autogenerate(ctx, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False) and directives[0].upgrade_ops.is_empty():
            directives[:] = []

    with db.engine.connect() as connection:
        is_sqlite = connection.dialect.name == 'sqlite'
        if is_sqlite:
            # SQLite 修改欄位需重建資料表 (batch mode)；重建期間暫停外鍵檢查，避免舊資料的孤兒參照中斷遷移
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            connection.commit()
        # Flask-Migrate 預設 render_as_batch=True：SQLite 以重建資料表方式修改欄位，PostgreSQL 直接 ALTER
        context.configure(
            connection=connection,
            target_metadata=db.metadata,
            process_revision_directives=skip_empty_autogenerate,
            **current_app.extensions['migrate'].configure_args,
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
