#!/bin/sh
# 線上端 PostgreSQL 每日備份 (docker compose 的 backup 服務執行；手動：docker compose exec backup pg_backup.sh)
#   pg_dump 自訂格式 → /backups/kiosk-YYYYmmdd-HHMMSS.dump，保留 BACKUP_KEEP_DAYS 天 (預設 14)
set -eu
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="/backups/kiosk-${STAMP}.dump"
mkdir -p /backups
pg_dump -h "${PGHOST:-postgres}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc -f "${TARGET}.tmp"
mv "${TARGET}.tmp" "${TARGET}"
find /backups -name 'kiosk-*.dump' -type f -mtime +"${KEEP_DAYS}" -delete
echo "$(date '+%F %T') 備份完成：${TARGET} ($(du -h "${TARGET}" | cut -f1))"
