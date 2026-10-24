#!/usr/bin/env bash
# Остановка сервера по PID из data/server.pid
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${AID_DATA_DIR:-$DIR/data}"
PID_FILE="$DATA/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Сервер не запущен (нет $PID_FILE)."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  echo "Процесс не найден, удаляю устаревший PID-файл."
  rm -f "$PID_FILE"
  exit 0
fi

kill "$PID"
# Ждём до 5 секунд, затем принудительно
for _ in $(seq 1 25); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.2
done
if kill -0 "$PID" 2>/dev/null; then
  echo "Сервер не остановился за 5 с — принудительная остановка."
  kill -9 "$PID" 2>/dev/null || true
fi
rm -f "$PID_FILE"
echo "Сервер остановлен (PID $PID)."
