#!/usr/bin/env bash
# Перезапуск сервера
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/stop.sh"
"$DIR/start.sh"
