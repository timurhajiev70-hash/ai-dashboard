#!/usr/bin/env bash
# Дымовой тест API через curl.
# Запускает СОБСТВЕННЫЙ экземпляр сервера (по умолчанию порт 8799, пароль test123)
# с временным каталогом данных — рабочие data/config.json и data/.env не затрагиваются.
#   ./scripts/test-api.sh
#   PORT=8800 ./scripts/test-api.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8799}"
PASS="${TEST_PASSWORD:-test123}"
BASE="http://127.0.0.1:$PORT"

for bin in curl node; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Ошибка: нужен $bin" >&2; exit 1; }
done

TMP="$(mktemp -d "${TMPDIR:-/tmp}/aid-test.XXXXXX")"
export AID_DATA_DIR="$TMP/data"
mkdir -p "$AID_DATA_DIR"
printf 'PASSWORD=%s\nPORT=%s\nHOST=127.0.0.1\n' "$PASS" "$PORT" >"$AID_DATA_DIR/.env"
# Переменные окружения важнее .env — убираем возможные значения из оболочки
unset PASSWORD HOST
export PORT

JAR="$TMP/cookies.txt"
OUT="$TMP/out.json"
PASSED=0
FAILED=0

cleanup() {
  "$DIR/scripts/stop.sh" >/dev/null 2>&1 || true
  if [[ "${KEEP_TMP:-0}" == "1" ]]; then echo "Временные файлы: $TMP"; else rm -rf "$TMP"; fi
}
trap cleanup EXIT

ok()   { PASSED=$((PASSED + 1)); echo "  OK    $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL  $1"; [[ -s "$OUT" ]] && echo "        ответ: $(head -c 300 "$OUT")"; return 0; }

# req МЕТОД ПУТЬ [JSON] → печатает HTTP-код, тело в $OUT
# Изменяющие запросы всегда с Content-Type: application/json (строгая защита от CSRF)
req() {
  local method="$1" path="$2" data="${3:-}"
  local args=(-sS -o "$OUT" -w '%{http_code}' -X "$method" -b "$JAR" -c "$JAR")
  if [[ "$method" != "GET" ]]; then args+=(-H 'Content-Type: application/json'); fi
  if [[ -n "$data" ]]; then args+=(--data-binary "$data"); fi
  curl "${args[@]}" "$BASE$path" || echo "000"
}

# jget ВЫРАЖЕНИЕ → значение из $OUT (j — разобранный JSON)
jget() { node -e 'const fs=require("fs");let j;try{j=JSON.parse(fs.readFileSync(process.argv[1],"utf8"))}catch{j=null};const v=(new Function("j","return ("+process.argv[2]+")"))(j);process.stdout.write(v===undefined?"undefined":typeof v==="string"?v:JSON.stringify(v))' "$OUT" "$1"; }

expect() { # expect ОПИСАНИЕ ФАКТ ОЖИДАНИЕ
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 (получено «$2», ожидалось «$3»)"; fi
}

echo "== Запуск тестового сервера на $BASE (данные: $AID_DATA_DIR)"
"$DIR/scripts/start.sh"

echo "== Здоровье и вход"
code=$(req GET /api/health)
expect "GET /api/health → 200" "$code" 200
expect "health.ok = true" "$(jget 'j.ok')" true
expect "health.authRequired = true" "$(jget 'j.authRequired')" true

code=$(req GET /api/config)
expect "GET /api/config без входа → 401" "$code" 401
expect "ошибка на русском" "$(jget 'j.error')" "Требуется вход"
code=$(req POST /api/logout)
expect "logout без сессии → 200" "$code" 200
code=$(req GET /api/icons)
expect "GET /api/icons без входа → 401" "$code" 401

start_ms=$(node -e 'process.stdout.write(String(Date.now()))')
code=$(req POST /api/login '{"password":"wrong"}')
end_ms=$(node -e 'process.stdout.write(String(Date.now()))')
expect "вход с неверным паролем → 401" "$code" 401
if (( end_ms - start_ms >= 450 )); then ok "задержка при неверном пароле ($((end_ms - start_ms)) мс)"; else fail "нет задержки при неверном пароле ($((end_ms - start_ms)) мс)"; fi

code=$(req POST /api/login "{\"password\":\"$PASS\"}")
expect "вход с верным паролем → 200" "$code" 200
if grep -q 'aid_session' "$JAR"; then ok "cookie aid_session установлена"; else fail "cookie aid_session не установлена"; fi

code=$(req GET /api/config)
expect "GET /api/config после входа → 200" "$code" 200
expect "в конфигурации 4 системы" "$(jget 'j.systems.length')" 4

echo "== Некорректные запросы"
code=$(req POST /api/recommend '{"task": "не закрыт')
expect "битый JSON → 400" "$code" 400
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: text/plain' --data 'x' "$BASE/api/recommend" || echo 000)
expect "не-JSON Content-Type → 415" "$code" 415
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/jsonjunk' --data '{"task":"x"}' "$BASE/api/recommend" || echo 000)
expect "Content-Type application/jsonjunk → 415" "$code" 415
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json; charset=utf-8' --data '{"task":"найти письмо"}' "$BASE/api/recommend" || echo 000)
expect "Content-Type с charset → 200" "$code" 200
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST "$BASE/api/check/claude" || echo 000)
expect "POST без тела, без Content-Type и Origin → 415" "$code" 415
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json' -H 'Origin: https://evil.example' --data '{"task":"x"}' "$BASE/api/recommend" || echo 000)
expect "чужой Origin → 403" "$code" 403
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json' -H 'Sec-Fetch-Site: cross-site' --data '{"task":"x"}' "$BASE/api/recommend" || echo 000)
expect "Sec-Fetch-Site: cross-site → 403" "$code" 403
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json' -H "Origin: http://localhost:$PORT" --data '{"task":"x"}' "$BASE/api/recommend" || echo 000)
expect "свой Origin (localhost) → 200" "$code" 200
code=$(curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -H 'Host: evil.example' "$BASE/api/health" || echo 000)
expect "чужой Host → 421" "$code" 421
code=$(curl -sS -o "$OUT" -w '%{http_code}' -H 'Host: evil.example' "$BASE/" || echo 000)
expect "чужой Host для страницы → 421" "$code" 421
code=$(curl -sS -o "$OUT" -w '%{http_code}' --path-as-is "$BASE/../data/.env" || echo 000)
expect "path traversal к .env → 404" "$code" 404
code=$(curl -sS -o "$OUT" -w '%{http_code}' "$BASE/%2e%2e/server.js" || echo 000)
expect "path traversal %2e%2e → 404" "$code" 404

echo "== Системы и связи"
code=$(req POST /api/systems '{"id":"test-sys","name":"Тестовая система","url":"https://example.com","access":["browser"],"models":["m1"],"tasks":["тест"],"limits":"нет"}')
expect "создать систему → 201" "$code" 201
expect "id созданной системы" "$(jget 'j.id')" test-sys
code=$(req POST /api/systems '{"id":"test-sys","name":"Дубль"}')
expect "повторный id → 400" "$code" 400
code=$(req PUT /api/systems/test-sys '{"notes":"заметка"}')
expect "изменить систему → 200" "$code" 200
expect "notes сохранены, name не потерялся" "$(jget 'j.notes + "|" + j.name')" "заметка|Тестовая система"

code=$(req PUT /api/systems/test-sys '{"sources":["https://example.com/a","https://example.com/b"],"icon":"/icons/claude.svg"}')
expect "sources и icon сохраняются" "$(jget 'j.sources.length + "|" + j.icon')" "2|/icons/claude.svg"
code=$(req PUT /api/systems/test-sys '{"icon":"../etc/passwd"}')
expect "некорректный icon → 400" "$code" 400
code=$(req PUT /api/systems/test-sys "{\"sources\":$(node -e 'process.stdout.write(JSON.stringify(Array.from({length:21},(_, i)=>"https://e.com/"+i)))')}")
expect "больше 20 sources → 400" "$code" 400
code=$(req GET /api/icons)
expect "GET /api/icons → 4 файла" "$(jget 'Array.isArray(j) && j.join()')" "chatgpt.svg,claude.svg,gemini.svg,mir.svg"
code=$(req PUT /api/settings '{"language":"de"}')
expect "settings.language = de → 200" "$(jget 'j.language')" de
code=$(req PUT /api/settings '{"language":"fr"}')
expect "settings.language = fr → 400" "$code" 400
code=$(req PUT /api/settings '{"language":"ru"}')

echo "== Защита от SSRF"
code=$(req POST /api/systems '{"id":"local-sys","name":"Локальная","url":"http://127.0.0.1:1"}')
expect "создать систему с внутренним адресом → 201" "$code" 201
code=$(req POST /api/check/local-sys)
expect "проверка http://127.0.0.1:1 → 400" "$code" 400
expect "ошибка о внутренних адресах" "$(jget 'String(j.error).includes("внутренних адресов")')" true
code=$(req PUT /api/systems/local-sys '{"url":"http://localhost:1"}')
code=$(req POST /api/check/local-sys)
expect "проверка http://localhost:1 → 400" "$code" 400
code=$(req PUT /api/systems/local-sys '{"url":"http://[::1]:1"}')
code=$(req POST /api/check/local-sys)
expect "проверка http://[::1]:1 → 400" "$code" 400
code=$(req PUT /api/systems/local-sys '{"url":"http://169.254.169.254/latest"}')
code=$(req POST /api/check/local-sys)
expect "проверка 169.254.169.254 → 400" "$code" 400
code=$(req PUT /api/systems/local-sys '{"url":"http://[::ffff:192.168.1.1]/"}')
code=$(req POST /api/check/local-sys)
expect "проверка ::ffff:192.168.1.1 → 400" "$code" 400
code=$(req DELETE /api/systems/local-sys)

code=$(req PUT /api/connections '{"toolId":"gmail","systemId":"test-sys","status":"verified","source":"тест"}')
expect "verified без checkedAt → 400" "$code" 400

code=$(req PUT /api/connections '{"toolId":"gmail","systemId":"claude","status":"verified","kind":"mcp","method":"Коннектор","actions":["read","search"],"source":"проверил сам","checkedAt":"2026-10-24"}')
expect "upsert связи gmail×claude (verified) → 200" "$code" 200
code=$(req PUT /api/connections '{"toolId":"gmail","systemId":"test-sys","status":"manual","source":"тест"}')
expect "upsert связи gmail×test-sys (manual) → 200" "$code" 200
code=$(req GET /api/connections)
expect "связь gmail×claude обновлена (не продублирована)" "$(jget 'j.filter(c=>c.toolId==="gmail"&&c.systemId==="claude").map(c=>c.status).join()')" verified

echo "== Подбор исполнителя"
code=$(req POST /api/recommend '{"task":"найти письмо"}')
expect "recommend → 200" "$code" 200
expect "правило find-email" "$(jget 'j.matchedRule && j.matchedRule.id')" find-email
expect "best = claude" "$(jget 'j.best && j.best.systemId')" claude
expect "у best есть gmail" "$(jget 'j.best.haveTools.some(t=>t.toolId==="gmail")')" true
expect "alternative = test-sys" "$(jget 'j.alternative && j.alternative.systemId')" test-sys
expect "промпт с подстановкой {task}" "$(jget 'j.prompt.includes("найти письмо") && !j.prompt.includes("{task}")')" true
code=$(req POST /api/recommend '{"task":"абвгд"}')
expect "без совпадений → правило default" "$(jget 'j.matchedRule && j.matchedRule.id')" default

echo "== Экспорт"
code=$(req GET /api/export)
expect "export → 200" "$code" 200
if grep -q 'PASSWORD' "$OUT" || grep -q "$PASS" "$OUT"; then fail "в экспорте есть пароль"; else ok "в экспорте нет PASSWORD"; fi
if grep -qi 'aid_session\|sessions' "$OUT"; then fail "в экспорте есть сессии"; else ok "в экспорте нет сессий"; fi
if grep -q "$PASS" "$AID_DATA_DIR/server.log"; then fail "пароль попал в лог"; else ok "пароля нет в логе"; fi

echo "== Перезапуск и сохранность"
"$DIR/scripts/restart.sh" >/dev/null
code=$(req GET /api/systems)
expect "сессия пережила перезапуск" "$code" 200
expect "созданная система сохранилась" "$(jget 'Array.isArray(j) && j.some(s=>s.id==="test-sys")')" true

echo "== Удаление и выход"
code=$(req DELETE /api/systems/test-sys)
expect "удалить систему → 200" "$code" 200
code=$(req GET /api/connections)
expect "связи удалённой системы удалены" "$(jget 'j.some(c=>c.systemId==="test-sys")')" false
code=$(req POST /api/logout)
expect "logout → 200" "$code" 200
code=$(req GET /api/config)
expect "после выхода → 401" "$code" 401

echo "== Права файлов"
perm() { node -e 'process.stdout.write((require("fs").statSync(process.argv[1]).mode & 0o777).toString(8))' "$1"; }
expect "config.json — права 600" "$(perm "$AID_DATA_DIR/config.json")" 600
expect "config.json.bak — права 600" "$(perm "$AID_DATA_DIR/config.json.bak")" 600

echo "== Смена пароля отзывает сессии"
code=$(req POST /api/login "{\"password\":\"$PASS\"}")
expect "повторный вход → 200" "$code" 200
printf 'PASSWORD=%s\nPORT=%s\nHOST=127.0.0.1\n' "${PASS}-new" "$PORT" >"$AID_DATA_DIR/.env"
"$DIR/scripts/restart.sh" >/dev/null
code=$(req GET /api/config)
expect "после смены пароля старая сессия → 401" "$code" 401

echo
echo "Итого: пройдено $PASSED, ошибок $FAILED"
[[ "$FAILED" -eq 0 ]]
