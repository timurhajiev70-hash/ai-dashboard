#!/usr/bin/env bash
# Автозапуск при входе в macOS через launchd (KeepAlive — перезапуск при падении)
set -euo pipefail

LABEL="com.timur.ai-dashboard"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$DIR/scripts/$LABEL.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/$LABEL.plist"
NODE="${NODE:-$(command -v node || true)}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ошибка: автозапуск через launchd работает только на macOS." >&2
  exit 1
fi
if [[ -z "$NODE" ]]; then
  echo "Ошибка: node не найден. Установите Node.js ≥ 18." >&2
  exit 1
fi
NODE="$(cd "$(dirname "$NODE")" && pwd)/$(basename "$NODE")"   # абсолютный путь
if [[ ! -f "$TEMPLATE" ]]; then
  echo "Ошибка: нет шаблона $TEMPLATE" >&2
  exit 1
fi
case "$DIR$NODE" in
  *'|'*|*'&'*) echo "Ошибка: путь содержит недопустимые символы | или &: $DIR" >&2; exit 1 ;;
esac

# Сервер, запущенный через start.sh, займёт порт — останавливаем его
"$DIR/scripts/stop.sh" >/dev/null 2>&1 || true

mkdir -p "$TARGET_DIR" "$DIR/data"
sed -e "s|__NODE__|$NODE|g" -e "s|__DIR__|$DIR|g" "$TEMPLATE" >"$TARGET"
plutil -lint "$TARGET" >/dev/null

# Перезагружаем, если уже был установлен
launchctl unload "$TARGET" >/dev/null 2>&1 || true
launchctl load -w "$TARGET"

echo "Автозапуск установлен: $TARGET"
echo "Node: $NODE"
echo "Сервер запускается при входе в систему и перезапускается при сбое. Лог: $DIR/data/server.log"
echo "Остановить и убрать автозапуск: ./scripts/uninstall-launchd.sh"
