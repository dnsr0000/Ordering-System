"""
交易提交後才執行的工作 (呼叫外部服務、列印、推播…)：

    deferred.defer(invoice_service.issue_invoice, invoice_id)

- 只有在交易成功提交後才會執行；交易回滾時一併丟棄 (不會為了失敗的結帳去開發票或印單)。
- 網頁請求中：回應送出前 (after_request) 交給背景執行緒執行，不拖慢結帳；
  測試時 (DEFERRED_INLINE=True) 直接依序執行，方便驗證結果。
- 非請求情境 (同步、背景工作) 不執行；這些工作都有對應的背景重試 (例如待開立的發票、待退款的 LINE Pay)，
  因此即使沒有立即執行也不會遺失。
- 工作必須只依賴傳入的參數 (id、tenant_id)，不可讀取 g 或 session。
"""
import logging
import threading

from flask import current_app, g, has_app_context, has_request_context
from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
_PENDING = 'deferred_tasks'


def defer(fn, *args, **kwargs):
    from app.extensions import db
    db.session.info.setdefault(_PENDING, []).append((fn, args, kwargs))


@event.listens_for(Session, 'after_commit')
def _move_to_ready(session):
    tasks = session.info.pop(_PENDING, None)
    if not tasks:
        return
    if has_request_context():
        g.setdefault('_deferred_ready', []).extend(tasks)


@event.listens_for(Session, 'after_soft_rollback')
def _discard(session, previous_transaction):
    session.info.pop(_PENDING, None)


def _run(app, tasks):
    from app.extensions import db
    with app.app_context():
        for fn, args, kwargs in tasks:
            try:
                fn(*args, **kwargs)
                db.session.commit()
            except Exception:  # noqa: BLE001  (單一工作失敗不影響其他工作；有背景重試)
                db.session.rollback()
                logger.exception('交易後工作 %s 執行失敗', getattr(fn, '__name__', fn))
        db.session.remove()


def run_ready(response=None):
    """after_request：執行本請求已提交交易所累積的工作"""
    if not has_request_context():
        return response
    tasks = g.pop('_deferred_ready', None)
    if not tasks or not has_app_context():
        return response
    app = current_app._get_current_object()
    if app.config.get('DEFERRED_INLINE'):
        from app.extensions import db
        rounds = 0
        while tasks and rounds < 5:   # 工作本身提交後又產生的工作也一併執行
            for fn, args, kwargs in tasks:
                try:
                    fn(*args, **kwargs)
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    logger.exception('交易後工作 %s 執行失敗', getattr(fn, '__name__', fn))
            tasks = g.pop('_deferred_ready', None)
            rounds += 1
    else:
        threading.Thread(target=_run, args=(app, tasks), name='deferred-tasks', daemon=True).start()
    return response


def init_app(app):
    app.after_request(run_ready)
