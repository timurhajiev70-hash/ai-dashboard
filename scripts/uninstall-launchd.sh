#!/usr/bin/env bash
# Убрать автозапуск через launchd
set -euo pipefail

LABEL="com.timur.ai-dashboard"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ошибка: launchd есть только на macOS." >&2
  exit 1
fi

if [[ -f "$TARGET" ]]; then
  launchctl unload -w "$TARGET" >/dev/null 2>&1 || true
  rm -f "$TARGET"
  echo "Автозапуск удалён, сервер остановлен."
else
  launchctl remove "$LABEL" >/dev/null 2>&1 || true
  echo "Автозапуск не был установлен."
fi
