"""
變更追蹤：讓每一列同步資料都帶有「本資料庫的提交序號」(sync_rev)，同步時只送出序號大於游標的列。

做法 (不依賴各機器的時鐘，也不會因為併發交易而漏掉資料)：
1. 交易進行中，被新增 / 修改的同步資料列先寫入一個「本交易專屬的負數標記」。
   ORM 物件在 before_flush 設定；以 update() 直接更新的語句在 do_orm_execute 補上 sync_rev 欄位。
2. 提交前 (before_commit) 才把 sync_clock 加一，並把帶有本交易標記的列改成這個序號。
   sync_clock 那一列會鎖到提交結束，因此序號順序 = 提交順序；讀到 clock = N 時，序號 <= N 的交易都已提交。
3. 只修改「本機專屬欄位」(例如庫存、點數合計) 的更新不標記、也不更新 updated_at，避免不必要的同步與時間戳變動。
4. 刪除同步資料時寫一筆 sync_tombstone，讓其他資料庫也刪除。
5. 分店套用線上送來的資料時以 suppressed() 暫停標記，避免把剛收到的資料再送回去。
"""
import secrets
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.sync import SyncClock, SyncTombstone
from app.sync.registry import BY_MODEL, spec_for

# 有同步資料提交後呼叫 (分店端用來立即觸發上傳)
COMMIT_LISTENERS = []
# before_flush 時先執行的掛勾 (例如：會員點數被直接修改時補寫點數異動帳)，確保它們新增的列也會被標記
PRE_FLUSH_HOOKS = []

_INFO_MARKER, _INFO_TABLES, _INFO_SUPPRESS = 'sync_marker', 'sync_tables', 'sync_suppress'
_IGNORED_FOR_DIRTY = {'updated_at', 'sync_rev'}


def _marker(session):
    marker = session.info.get(_INFO_MARKER)
    if marker is None:
        marker = -(secrets.randbelow(2 ** 62) + 1)
        session.info[_INFO_MARKER] = marker
    return marker


def _touch(session, table_name):
    session.info.setdefault(_INFO_TABLES, set()).add(table_name)
    return _marker(session)


def is_suppressed(session):
    return bool(session.info.get(_INFO_SUPPRESS))


@contextmanager
def suppressed(session):
    """區塊內的寫入不標記為本機變更 (分店套用線上資料時使用)"""
    prev = session.info.get(_INFO_SUPPRESS)
    session.info[_INFO_SUPPRESS] = True
    try:
        yield
    finally:
        session.info[_INFO_SUPPRESS] = prev


def _changed_columns(obj):
    state = inspect(obj)
    return {attr.key for attr in state.mapper.column_attrs if state.attrs[attr.key].history.has_changes()}


@event.listens_for(Session, 'before_flush')
def _mark_flushed_rows(session, flush_context, instances):
    for hook in PRE_FLUSH_HOOKS:
        hook(session)
    if is_suppressed(session):
        return
    for obj in list(session.new):
        if spec_for(obj) is not None:
            obj.sync_rev = _touch(session, spec_for(obj).name)
    for obj in list(session.dirty):
        spec = spec_for(obj)
        if spec is None:
            continue
        changed = _changed_columns(obj) - _IGNORED_FOR_DIRTY
        if changed - spec.local_only:
            obj.sync_rev = _touch(session, spec.name)
        elif changed:
            flag_modified(obj, 'updated_at')  # 只改本機欄位：保留原本的 updated_at
    for obj in list(session.deleted):
        spec = spec_for(obj)
        if spec is not None and spec.model is not SyncTombstone and obj.uuid:
            session.add(SyncTombstone(tenant_id=obj.tenant_id, table_name=spec.name, row_uuid=obj.uuid,
                                      sync_rev=_touch(session, SyncTombstone.__tablename__)))


def _statement_columns(stmt):
    keys = set()
    for source in (getattr(stmt, '_values', None) or {}, dict(getattr(stmt, '_ordered_values', None) or ())):
        for key in source:
            keys.add(getattr(key, 'key', None) or getattr(key, 'name', None) or str(key))
    return keys


@event.listens_for(Session, 'do_orm_execute')
def _mark_bulk_updates(state):
    if not state.is_update:
        return
    try:
        entity = state.statement.entity_description.get('entity')
    except (AttributeError, KeyError):  # 以 Table 建立的 update() 沒有 ORM 實體
        entity = None
    spec = BY_MODEL.get(entity)
    if spec is None or is_suppressed(state.session):
        return
    columns = _statement_columns(state.statement) - _IGNORED_FOR_DIRTY
    if columns - spec.local_only:
        state.statement = state.statement.values(sync_rev=_touch(state.session, spec.name))
    elif 'updated_at' not in _statement_columns(state.statement):
        state.statement = state.statement.values(updated_at=entity.__table__.c.updated_at)


def mark_rows(session, model, *criteria):
    """把符合條件的列標記為本交易的變更 (內容不變，只是讓它們在下次同步時再送一次)"""
    spec = BY_MODEL[model]
    table = model.__table__
    marker = _touch(session, spec.name)
    session.execute(sa.update(table).where(*criteria).values(sync_rev=marker, updated_at=table.c.updated_at))
    return marker


def next_rev(session):
    """sync_clock + 1 並回傳新值 (該列鎖到交易結束)"""
    stmt = sa.update(SyncClock.__table__).where(SyncClock.__table__.c.id == 1) \
        .values(rev=SyncClock.__table__.c.rev + 1).returning(SyncClock.__table__.c.rev)
    rev = session.execute(stmt).scalar()
    if rev is None:  # 尚未建立計數列 (理論上遷移已建立)
        session.execute(sa.insert(SyncClock.__table__).values(id=1, rev=0))
        rev = session.execute(stmt).scalar()
    return rev


def current_rev(session):
    return session.execute(sa.select(SyncClock.__table__.c.rev).where(SyncClock.__table__.c.id == 1)).scalar() or 0


@event.listens_for(Session, 'before_commit')
def _assign_revision(session):
    if session.new or session.dirty or session.deleted:
        session.flush()  # 先 flush，才知道這筆交易動到哪些同步資料表
    if not session.info.get(_INFO_TABLES):
        return
    tables = session.info.pop(_INFO_TABLES, set())
    marker = session.info.pop(_INFO_MARKER, None)
    if not tables or marker is None:
        return
    rev = next_rev(session)
    session.info['sync_committed'] = True
    metadata = SyncClock.metadata
    for name in sorted(tables):
        table = metadata.tables[name]
        session.execute(sa.update(table).where(table.c.sync_rev == marker)
                        .values(sync_rev=rev, updated_at=table.c.updated_at))


@event.listens_for(Session, 'after_commit')
def _notify_commit(session):
    if session.info.pop('sync_committed', False):
        for callback in COMMIT_LISTENERS:
            callback()


@event.listens_for(Session, 'after_transaction_end')
def _reset(session, transaction):
    if transaction.parent is None:
        session.info.pop(_INFO_TABLES, None)
        session.info.pop(_INFO_MARKER, None)
        session.info.pop('sync_committed', None)
