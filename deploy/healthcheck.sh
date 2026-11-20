#!/usr/bin/env bash
# Проверка доступности (запускайте по cron раз в минуту ИЗ ДРУГОГО места — со своего компьютера или второго сервера).
# Шлёт сообщение в Telegram при падении и при восстановлении, а не каждую минуту.
#   * * * * * URL=https://lawtracker.example.ru BOT_TOKEN=... CHAT_ID=... /path/healthcheck.sh
set -uo pipefail
URL="${URL:?укажите URL сайта}"; BOT_TOKEN="${BOT_TOKEN:-}"; CHAT_ID="${CHAT_ID:-}"
STATE="${STATE_FILE:-/tmp/lt-health.state}"
tg() { [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ] && curl -fsS -m 10 "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT_ID" --data-urlencode "text=$1" >/dev/null || true; }
if curl -fsS -m 10 "$URL/api/healthz" | grep -q '"ok"'; then
  [ -f "$STATE" ] && { rm -f "$STATE"; tg "LawTracker снова доступен: $URL"; }
else
  [ -f "$STATE" ] || { date +%s > "$STATE"; tg "LawTracker НЕДОСТУПЕН: $URL"; }
  exit 1
fi
