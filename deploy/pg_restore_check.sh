#!/bin/sh
# 還原演練：把最新的備份還原到暫時的資料庫 kiosk_restore_check，檢查主要資料表筆數後刪除。
# 用法：docker compose exec backup pg_restore_check.sh  (每月至少演練一次)
set -eu
LATEST="$(ls -1t /backups/kiosk-*.dump 2>/dev/null | head -n 1)"
if [ -z "${LATEST}" ]; then
    echo "找不到備份檔 (/backups/kiosk-*.dump)" >&2
    exit 1
fi
export PGHOST="${PGHOST:-postgres}"
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
DB=kiosk_restore_check
dropdb -U "${POSTGRES_USER}" --if-exists "${DB}"
# 中途失敗也要刪掉暫時資料庫 (內含會員個資的完整資料)
trap 'dropdb -U "${POSTGRES_USER}" --if-exists "${DB}" || true' EXIT
createdb -U "${POSTGRES_USER}" "${DB}"
pg_restore -U "${POSTGRES_USER}" -d "${DB}" --no-owner "${LATEST}"
echo "還原 ${LATEST} 成功，主要資料表筆數："
# user、order 是 PostgreSQL 保留字：資料表名稱一律以 SQL 雙引號包住
for t in tenant menu_item user order order_item payment_record invoice; do
    n="$(psql -U "${POSTGRES_USER}" -d "${DB}" -tAc "SELECT count(*) FROM \"${t}\"")"
    echo "  ${t}: ${n}"
done
dropdb -U "${POSTGRES_USER}" "${DB}"
trap - EXIT
echo "演練完成 (暫時資料庫已刪除)"
