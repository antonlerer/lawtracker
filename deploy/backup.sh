#!/usr/bin/env bash
# Ежедневный дамп PostgreSQL с хранением KEEP дней. Запуск из корня репозитория:  deploy/backup.sh
# Cron (каждый день в 03:30):  30 3 * * * cd /opt/lawtracker && deploy/backup.sh >> /var/log/lt-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${BACKUP_DIR:-/var/backups/lawtracker}"
KEEP="${KEEP:-14}"
mkdir -p "$DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DIR/lawtracker-$STAMP.sql.gz"
docker compose exec -T db pg_dump -U lawtracker --no-owner lawtracker | gzip -9 > "$OUT.tmp"
# пустой или обрезанный дамп — это сбой, а не копия
if [ "$(gzip -dc "$OUT.tmp" | head -c 2000 | wc -c)" -lt 500 ] || ! gzip -dc "$OUT.tmp" | tail -n 5 | grep -q "PostgreSQL database dump complete"; then
  rm -f "$OUT.tmp"; echo "ОШИБКА: дамп повреждён или пуст" >&2; exit 1
fi
mv "$OUT.tmp" "$OUT"
find "$DIR" -name 'lawtracker-*.sql.gz' -mtime +"$KEEP" -delete
echo "OK $OUT ($(du -h "$OUT" | cut -f1))"
