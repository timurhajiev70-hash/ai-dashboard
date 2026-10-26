#!/bin/bash
# Fokus.command — лаунчер Fokus для macOS. Запуск двойным кликом в Finder или из Терминала.
#
# Что делает:
#   1. переходит в папку проекта (разрешает символическую ссылку, если ярлык лежит на рабочем столе);
#   2. если Fokus уже отвечает на /api/health — просто открывает браузер и выходит;
#   3. проверяет Python 3 (>= 3.9), запускает `python3 server.py` в фоне;
#   4. ждёт до 5 секунд ответа /api/health, открывает браузер;
#   5. держит окно Терминала открытым, пока работает сервер. Ctrl+C или закрытие окна останавливают его.
# Бэкап базы (data/backups, 10 последних) делает сам server.py при старте — здесь ничего не копируется.
#
# Переменные окружения:
#   FOKUS_PORT    порт (по умолчанию 4321)
#   FOKUS_DB      путь к базе (по умолчанию data/fokus.db рядом со скриптом) — читает server.py
#   FOKUS_TODAY   дата «сегодня» YYYY-MM-DD (для тестов) — читает server.py
#   FOKUS_PYTHON  интерпретатор вместо python3 из PATH (для тестов)
#   FOKUS_OPEN    команда открытия браузера вместо `open` (для тестов: FOKUS_OPEN=true)
#
# Совместимо с /bin/bash 3.2 (системный bash macOS): без массивов bash 4, ${var,,}, |& и т.п.

set -u

PORT="${FOKUS_PORT:-4321}"
URL="http://127.0.0.1:$PORT"
HEALTH_URL="$URL/api/health"
OPEN_CMD="${FOKUS_OPEN:-open}"
PYTHON="$(command -v "${FOKUS_PYTHON:-python3}" 2>/dev/null || true)"
SERVER_PID=""

# ----------------------------------------------------------------- вспомогательные функции

say() { printf '%s\n' "$*"; }

# Ждать клавишу только если окно интерактивное (из тестов stdin не терминал — не висим).
pause_if_tty() {
  if [ -t 0 ]; then
    say ""
    read -n1 -r -s -p "Нажмите любую клавишу, чтобы закрыть это окно…"
    say ""
  fi
}

# fail "что случилось" ["что делать"] — сообщение, ожидание клавиши, выход с кодом 1.
fail() {
  say ""
  say "ОШИБКА: $1"
  [ -n "${2:-}" ] && say "$2"
  pause_if_tty
  exit 1
}

# Разрешить цепочку символических ссылок без `readlink -f` (его нет в старых macOS).
resolve_path() {
  local target="$1" dir
  while [ -L "$target" ]; do
    dir="$(cd -P "$(dirname "$target")" && pwd)" || return 1
    target="$(readlink "$target")" || return 1
    case "$target" in
      /*) ;;
      *) target="$dir/$target" ;;
    esac
  done
  dir="$(cd -P "$(dirname "$target")" && pwd)" || return 1
  printf '%s/%s\n' "$dir" "$(basename "$target")"
}

# 0 = на порту уже отвечает Fokus (в ответе /api/health есть "ok").
is_running() {
  local body=""
  if command -v curl >/dev/null 2>&1; then
    body="$(curl -s --max-time 1 "$HEALTH_URL" 2>/dev/null || true)"
  elif [ -n "$PYTHON" ]; then
    body="$("$PYTHON" -c 'import sys, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=1).read().decode("utf-8", "replace"))
except Exception:
    pass' "$HEALTH_URL" 2>/dev/null || true)"
  fi
  case "$body" in
    *'"ok"'*) return 0 ;;
  esac
  return 1
}

open_browser() {
  if ! "$OPEN_CMD" "$URL" >/dev/null 2>&1; then
    say "Не удалось открыть браузер автоматически — откройте адрес вручную: $URL"
  fi
}

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
}

on_signal() {
  say ""
  say "Останавливаю Fokus…"
  stop_server
  exit 0
}

# ----------------------------------------------------------------- 1. папка проекта

SELF="$(resolve_path "$0" 2>/dev/null || printf '%s' "$0")"
cd "$(dirname "$SELF")" || fail "Не удалось перейти в папку $(dirname "$SELF")."
SCRIPT_DIR="$(pwd -P)"
DB_PATH="${FOKUS_DB:-$SCRIPT_DIR/data/fokus.db}"

say "Fokus — запуск"
say "  Папка:  $SCRIPT_DIR"
say "  Адрес:  $URL"
say "  База:   $DB_PATH"
say "  Копии:  data/backups (10 последних; создаёт сервер при каждом запуске)"
say ""

# ----------------------------------------------------------------- 2. уже запущен?

if is_running; then
  say "Fokus уже запущен на $URL — открываю браузер."
  open_browser
  exit 0
fi

# ----------------------------------------------------------------- 3. Python 3 >= 3.9

if [ -z "$PYTHON" ]; then
  fail "Не найден Python 3 (${FOKUS_PYTHON:-python3})." \
       "Нужен Python 3 (xcode-select --install): откройте Терминал, выполните xcode-select --install, затем запустите Fokus снова."
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
  fail "Python по пути $PYTHON не запускается или его версия ниже 3.9." \
       "Нужен Python 3 >= 3.9 (xcode-select --install)."
fi

# ----------------------------------------------------------------- 4. server.py рядом?

if [ ! -f "$SCRIPT_DIR/server.py" ]; then
  if [ -f "$HOME/Projects/fokus/server.py" ]; then
    cd "$HOME/Projects/fokus" || fail "Не удалось перейти в $HOME/Projects/fokus."
    SCRIPT_DIR="$(pwd -P)"
    say "Рядом с Fokus.command нет server.py — использую проект в $SCRIPT_DIR"
  else
    fail "Рядом с Fokus.command нет server.py (искал в $SCRIPT_DIR и ~/Projects/fokus)." \
         "Положите Fokus.command в папку проекта или сделайте ярлык: ln -s ~/Projects/fokus/Fokus.command ~/Desktop/Fokus.command"
  fi
fi

# ----------------------------------------------------------------- 5. запуск сервера

export FOKUS_PORT="$PORT"
trap on_signal INT TERM HUP

"$PYTHON" server.py &
SERVER_PID=$!

# ждём ответа /api/health до 5 секунд (10 × 0.5 с)
i=0
up=0
while [ "$i" -lt 10 ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  if is_running; then
    up=1
    break
  fi
  sleep 0.5
  i=$((i + 1))
done

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  wait "$SERVER_PID" 2>/dev/null
  code=$?
  trap - INT TERM HUP
  fail "Сервер завершился сразу после запуска (код $code) — см. сообщения выше." \
       "Если порт $PORT занят: lsof -i :$PORT — остановите тот процесс или задайте другой порт: FOKUS_PORT=4322"
fi

if [ "$up" -eq 1 ]; then
  say "Сервер отвечает (PID $SERVER_PID). Открываю браузер…"
else
  say "Сервер запущен (PID $SERVER_PID), но за 5 секунд не ответил — открываю браузер; если страница пустая, обновите её."
fi
open_browser

say ""
say "Fokus работает: $URL"
say "Закройте это окно (или нажмите Ctrl+C), чтобы остановить Fokus."

wait "$SERVER_PID"
code=$?
trap - INT TERM HUP
SERVER_PID=""

say ""
say "Сервер Fokus завершил работу (код $code)."
if [ "$code" -ne 0 ]; then
  pause_if_tty
fi
exit "$code"
