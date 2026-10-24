#!/usr/bin/env bash
# Запуск сервера в фоне (nohup). PID — data/server.pid, вывод — data/server.log
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${AID_DATA_DIR:-$DIR/data}"
PID_FILE="$DATA/server.pid"
LOG_FILE="$DATA/server.log"
NODE="${NODE:-$(command -v node || true)}"

if [[ -z "$NODE" ]]; then
  echo "Ошибка: node не найден. Установите Node.js ≥ 18." >&2
  exit 1
fi
if ! "$NODE" -e 'process.exit(parseInt(process.versions.node) >= 18 ? 0 : 1)'; then
  echo "Ошибка: нужен Node.js ≥ 18 (сейчас $("$NODE" -v))." >&2
  exit 1
fi

mkdir -p "$DATA"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "Сервер уже запущен (PID $PID)."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Читаем HOST/PORT из .env только для подсказки (сам сервер читает .env сам)
ENV_FILE="$DATA/.env"
env_get() { [[ -f "$ENV_FILE" ]] && grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d "\"' \r" || true; }
HOST="${HOST:-$(env_get HOST)}"; HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-$(env_get PORT)}"; PORT="${PORT:-8787}"

cd "$DIR"
nohup "$NODE" "$DIR/server.js" >>"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" >"$PID_FILE"

# Ждём до 5 секунд, пока сервер начнёт отвечать
for _ in $(seq 1 25); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Ошибка: сервер не запустился. Последние строки лога:" >&2
    tail -n 5 "$LOG_FILE" >&2 || true
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    CHECK_HOST="$HOST"; [[ "$CHECK_HOST" == "0.0.0.0" ]] && CHECK_HOST="127.0.0.1"
    if curl -fsS -o /dev/null "http://$CHECK_HOST:$PORT/api/health" 2>/dev/null; then break; fi
  fi
  sleep 0.2
done

echo "Сервер запущен (PID $PID): http://$HOST:$PORT"
