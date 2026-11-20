#!/usr/bin/env bash
# Проверка, что копия восстанавливается: разворачивает её во ВРЕМЕННУЮ базу и считает записи. Боевая база не затрагивается.
# Использование:  deploy/restore_check.sh /var/backups/lawtracker/lawtracker-YYYYMMDD-HHMMSS.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?укажите файл копии}"
TMP="lt_restore_check_$$"
docker compose exec -T db psql -U lawtracker -d postgres -c "CREATE DATABASE $TMP" >/dev/null
trap 'docker compose exec -T db psql -U lawtracker -d postgres -c "DROP DATABASE IF EXISTS $TMP" >/dev/null' EXIT
gzip -dc "$FILE" | docker compose exec -T db psql -q -U lawtracker -d "$TMP" -v ON_ERROR_STOP=1 >/dev/null
for t in users companies changes; do
  n=$(docker compose exec -T db psql -U lawtracker -d "$TMP" -tAc "SELECT count(*) FROM $t")
  echo "$t: $n"
done
echo "OK: копия восстанавливается"
