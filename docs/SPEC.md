# AI Dashboard «Мои ИИ системы» — спецификация и контракт

Стек: Node.js ≥ 18, **без npm-зависимостей** (только встроенные модули `http`, `fs`, `path`, `crypto`, `url`).
Фронтенд: один `public/index.html` + `public/app.js` + `public/style.css`, vanilla JS, без сборки.
Хранилище: `data/config.json` (вся конфигурация). Секреты: `data/.env` (не входит в экспорт, не отдаётся фронтенду, не логируется).

## 1. Файлы проекта
```
ai-dashboard/
  server.js            — HTTP-сервер, API, статика, авторизация
  public/index.html    — разметка (статичный текст с data-i18n)
  public/locales/ru.json, de.json — строки интерфейса (i18n)
  public/icons/*.svg   — иконки систем
  public/app.js        — логика
  public/style.css     — стили, светлая/тёмная темы, адаптив (≥360px)
  data/config.json     — данные (создаётся из data/config.default.json при первом запуске)
  data/config.default.json — стартовые данные, demo:true помечены
  data/.env.example    — PASSWORD, PORT, HOST, ALLOWED_HOSTS, CHECK_ALLOW_PRIVATE_HOSTS
  scripts/start.sh, stop.sh, restart.sh, install-launchd.sh, uninstall-launchd.sh
  scripts/com.timur.ai-dashboard.plist (шаблон, пути подставляет install-launchd.sh)
  docs/SPEC.md, docs/01-comparison.md, docs/TASKS-FOR-GPT.md
  README.md            — короткая инструкция запуска
```

## 2. Схема `config.json`
```jsonc
{
  "version": 1,
  "updatedAt": "2026-10-24T08:00:00Z",
  "systems": [ System ],
  "tools": [ Tool ],
  "connections": [ Connection ],
  "rules": [ Rule ],
  "settings": { "theme": "auto" | "light" | "dark", "language": "ru" | "de" }   // language по умолчанию "ru"
}
```
### System
```jsonc
{
  "id": "chatgpt",                  // slug, уникальный
  "name": "ChatGPT",
  "url": "https://chatgpt.com",      // ссылка открытия
  "access": ["app","browser","api","local"], // способы доступа (несколько)
  "models": ["GPT-5", "..."],        // свободный список строк
  "tasks": ["...", "..."],           // основные задачи, строки
  "subscription": "ChatGPT Pro",     // подписка на приложение (строка, может быть пустой)
  "apiAccess": "unknown" | "yes" | "no", // доступ по API — отдельно от подписки
  "limits": "текст",                 // ограничения и стоимость
  "status": {                        // три независимых флага
    "site":  "ok" | "down" | "unchecked",
    "api":   "ok" | "down" | "unchecked" | "n/a",
    "tools": "ok" | "partial" | "unchecked"
  },
  "checkedAt": "2026-10-24" | null,  // дата последней проверки информации
  "handoff": { "type": "none" | "url" | "clipboard", "template": "https://chatgpt.com/?q={prompt}" },
    // type=url: проверенный способ передать промпт через URL (оставить "none", если не подтверждён)
  "notes": "",
  "sources": ["https://…"],          // v1.1: ссылки-источники данных; ≤ 20 строк, каждая ≤ 500 символов
  "icon": "/icons/claude.svg",       // v1.1: "" или /^\/icons\/[a-z0-9_-]+\.svg$/, ≤ 200 символов
  "demo": true | false
}
```
### Tool
```jsonc
{ "id": "gmail", "name": "Gmail", "category": "mail|calendar|files|code|browser|terminal|design|tasks|notes|skill|other", "demo": false }
```
### Connection (ячейка матрицы: инструмент × система)
```jsonc
{
  "toolId": "gmail",
  "systemId": "claude",
  "status": "verified" | "manual" | "available" | "none" | "unknown",
     // verified = подключено и проверено; manual = указано вручную; available = доступно для подключения; none = не подключено; unknown = неизвестно
  "kind": "plugin" | "mcp" | "skill" | "link" | "builtin" | "",   // вид связи
  "method": "Connector в настройках claude.ai",  // способ подключения, текст
  "actions": ["read","search","send"],           // разрешённые действия, свободные строки
  "source": "проверил сам 24.10.2026",            // источник информации
  "checkedAt": "2026-10-24" | null,
  "demo": false
}
```
Отсутствие записи = `unknown`. Статусы `verified` только с `checkedAt` и `source` (сервер валидирует: если status=verified, а checkedAt пуст — 400).

### Rule (правила подбора исполнителя — редактируемые)
```jsonc
{
  "id": "find-email",
  "name": "Найти письмо",
  "keywords": ["письм", "почт", "mail", "gmail", "email"],   // подстроки, регистронезависимо
  "requiredTools": ["gmail"],      // id инструментов, нужных для задачи
  "capabilities": ["поиск по почте"], // текст — какие возможности нужны
  "preferredSystems": ["claude", "chatgpt"], // приоритет при равных условиях (может быть пустым)
  "promptTemplate": "Найди в моей почте: {task}. Покажи отправителя, дату и краткое содержание.",
  "priority": 10,                  // выше = важнее при нескольких совпадениях
  "demo": false
}
```

## 3. Алгоритм подбора (`POST /api/recommend`, реализуется в `server.js`, модуль `recommend(task, config)`)
1. Найти правила, у которых хотя бы одно `keyword` входит в текст задачи (lowercase). Взять с максимальным числом совпадений, при равенстве — больший `priority`. Если ни одно не подошло — использовать правило `default` (если есть) или вернуть `matchedRule: null` с советом уточнить.
2. Для каждой системы посчитать очки:
   - за каждый `requiredTools[i]`: статус связи `verified` → +3, `manual` → +2, `available` → +0.5, `none`/`unknown` → 0; если `none` — пометить в `missing`
   - `preferredSystems` содержит систему → +1 (порядок: первый +1, второй +0.5)
   - `status.site === "down"` → −5
3. Отсортировать; первая — `best`, вторая — `alternative`.
4. Вернуть:
```jsonc
{
  "task": "...",
  "matchedRule": { "id", "name" } | null,
  "capabilities": [...],
  "candidates": [ { "systemId", "name", "url", "score", "haveTools": [{toolId,status}], "missingTools": [toolId], "limits": "...", "reasons": ["строки объяснения на русском"] } ],
  "best": candidate | null,
  "alternative": candidate | null,
  "prompt": "подготовленный промпт (promptTemplate с подстановкой {task})",
  "handoff": { "type", "url" | null }   // url только если у best handoff.type==="url"
}
```

## 4. HTTP API (все JSON, UTF-8)
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/health` | `{ok:true, version, authRequired:bool}` — без авторизации |
| POST | `/api/login` | body `{password}` → ставит cookie `aid_session` (HttpOnly, SameSite=Lax, 30 дней), `{ok:true}`; 401 при ошибке; задержка 500 мс при неверном пароле |
| POST | `/api/logout` | требует сессию; без действующей сессии — всё равно 200 и очистка cookie |
| GET | `/api/config` | вся конфигурация |
| PUT | `/api/config` | заменить целиком (валидация схемы, атомарная запись: tmp → rename) |
| GET/POST | `/api/systems` | список / создать |
| PUT/DELETE | `/api/systems/:id` | изменить / удалить (удаление каскадно убирает connections) |
| GET/POST | `/api/tools`, PUT/DELETE `/api/tools/:id` | аналогично |
| PUT | `/api/connections` | body `Connection` — upsert по (toolId, systemId); status `unknown` удаляет запись |
| GET/POST | `/api/rules`, PUT/DELETE `/api/rules/:id` | аналогично |
| POST | `/api/recommend` | body `{task}` → см. п.3 |
| GET | `/api/export` | скачивание `config.json` (Content-Disposition), **без секретов** |
| POST | `/api/import` | body = config → валидация → замена; `?merge=1` — слияние по id |
| POST | `/api/check/:systemId` | HEAD на `system.url` с таймаутом 5 с → обновляет `status.site` (`ok`/`down`) и `checkedAt`. Только сайт; API и инструменты не проверяются автоматически (остаются как есть). Внутренние адреса → 400 (см. §5). |
| GET | `/api/icons` | список файлов `*.svg` из `public/icons/` (`["chatgpt.svg", …]`) |
| GET | `/api/settings`, PUT `/api/settings` | настройки; PUT сливает поля (`theme`, `language`) |

Ошибки: `{error: "текст на русском"}` с кодом 400/401/403/404/413/415/421/429/500.

## 5. Авторизация и безопасность
- `data/.env`: `PASSWORD=` (обязателен, если `HOST` ≠ 127.0.0.1; если пароль пуст и HOST=127.0.0.1 — авторизация выключена, `authRequired:false`).
- Сессия: случайный токен (crypto.randomBytes 32) → в памяти + файл `data/.sessions.json` (чтобы переживать перезапуск), сравнение пароля через `crypto.timingSafeEqual`. На диске хранится только `HMAC-SHA256(ключ = SHA-256(PASSWORD), токен)`: смена пароля отзывает все сессии.
- Все `/api/*` кроме `health`/`login` требуют сессию при `authRequired`. Без сессии: API → 401, страница → показывает форму входа (фронтенд сам проверяет `/api/health` + 401).
- Перебор пароля: 10 неудач за 15 мин с одного IP → 429; таблица неудач чистится раз в минуту, не больше 1000 записей.
- **Host**: принимаются только `localhost`, `127.0.0.1`, `[::1]`, сам `HOST` (при `HOST=0.0.0.0` — адреса сетевых интерфейсов) и записи `ALLOWED_HOSTS` (через запятую; запись без порта — любой порт). Иначе 421 (защита от DNS rebinding).
- **CSRF** для POST/PUT/DELETE/PATCH: `Sec-Fetch-Site: cross-site` → 403; `Origin`, если есть, — только http(s) и host из того же списка, что и Host → иначе 403. Запрос с телом — media type ровно `application/json` (параметры после `;` допустимы) → иначе 415. Запрос без тела — `application/json` или проверенный `Origin`.
- **SSRF** в `/api/check`: имя разрешается через `dns.promises.lookup({all:true})`; если хоть один адрес loopback/частный/link-local/unique-local/CGNAT/multicast/0.0.0.0 (или имя `localhost`) → 400 «Проверка внутренних адресов запрещена; добавьте хост в CHECK_ALLOW_PRIVATE_HOSTS». Соединение закреплено за проверенным адресом (`lookup` в `http.request`, SNI/Host — исходное имя). Исключения — `CHECK_ALLOW_PRIVATE_HOSTS` (имена через запятую, например `localhost` для «Мира»).
- Права файлов: `config.json`, `config.json.bak`, `.sessions.json` — 0600; если `data/.env` читается группой/другими — предупреждение в логе при запуске.
- Никаких секретов в `config.json`, в экспорте, в логах. Логи: `data/server.log` — метод, путь, код, время; тело запросов не логируется. Заголовки `Cookie`/`Authorization` не логируются.
- Заголовки: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` для API; CSP для HTML, для SVG — `default-src 'none'`.
- Для доступа с телефона: HOST=0.0.0.0 + пароль; в README — рекомендация Tailscale/WireGuard, а не открытый порт.

## 6. Фронтенд — экраны
Навигация сверху (вкладки): **Системы · Матрица · Кому дать задачу · Настройки**. Поиск (общий input) + фильтры.
1. **Системы** — сетка карточек (на телефоне 1 колонка). Карточка: иконка (28 px, если задана), название, кнопка «Открыть», бейджи доступа, модели (чипы), задачи, подключённые инструменты (только статусы verified/manual, с иконкой статуса), ограничения/стоимость, подписка и «API: да/нет/неизвестно» отдельно, три индикатора: Сайт / API / Инструменты (цвет + подпись «не проверено»), «Проверено: дата». Сворачиваемый список «Источники» (только http(s), `rel="noopener noreferrer"`). Кнопки: редактировать, проверить сайт, удалить. В окне системы: выбор иконки из `/api/icons`, источники — textarea, одна ссылка на строку. Кнопка «+ Добавить систему». Пометка «ДЕМО» на карточках с demo:true.
2. **Матрица** — таблица: строки инструменты, столбцы системы, ячейка — цветной бейдж статуса; клик → модальное окно редактирования связи (status, kind, method, actions, source, checkedAt). Фильтры по статусу и категории. На телефоне — горизонтальная прокрутка с закреплённым первым столбцом. Кнопки «+ Инструмент».
3. **Кому дать задачу** — textarea, кнопка «Подобрать», ниже результат: лучший вариант (карточка с объяснением по пунктам: нужные возможности, есть доступ к данным, недостающие подключения, ограничения), альтернатива, подготовленный промпт (textarea) + кнопки «Открыть систему», «Скопировать промпт», «Передать задачу» (активна только если handoff.url). Ссылка «Правила подбора» → список правил с редактированием (поля из Rule).
4. **Настройки** — язык (Русский / Deutsch), тема (авто/светлая/тёмная), экспорт (скачать JSON), импорт (file input, выбор «заменить/слить»), выход, версия, ссылка на README.
Легенда статусов — внизу матрицы и в карточках: ✅ подключено и проверено · ✍️ указано вручную · ➕ доступно для подключения · ⛔ не подключено · ❔ неизвестно.
Все изменения сохраняются сразу через API; при ошибке — toast.

### i18n (v1.1)
- Строки интерфейса — `public/locales/ru.json` и `de.json` (одинаковый набор ключей, плейсхолдеры `{name}`). Загружаются `fetch('/locales/<lang>.json')`.
- `t(key, params)` в `app.js`: строка текущего языка → русская → сам ключ (ничего не ломается при отсутствии ключа).
- Статичный текст `index.html` — атрибуты `data-i18n`, `data-i18n-placeholder`, `data-i18n-aria`, `data-i18n-title`; `<html lang>` и `<title>` ставятся при загрузке.
- Выбор языка: `localStorage.aid_lang` (приоритет) + `settings.language` в `config.json`.
- Данные пользователя (названия, задачи, лимиты) и тексты сервера (объяснения подбора `reasons`, ошибки API) не переводятся — остаются на русском.

## 7. Стартовые данные (`config.default.json`)
Системы: chatgpt, claude, gemini. Для всех `status` = unchecked, `checkedAt` = null, `apiAccess` = "unknown", `demo: true`.
Инструменты: gmail, calendar, drive, github, browser, files, terminal, figma, todoist, obsidian, skills (category skill).
Connections: **пустые** (всё unknown) — кроме 2–3 примеров со статусом `available` и `demo:true` для демонстрации.
v1.1: chatgpt/claude/gemini заполнены по результату GPT-6 Sol (24.10.2026, `demo:false`, `sources`), у всех систем задан `icon`; к правилам добавлены 15 правил из `gpt-results/02-rules.json` (`demo:false`).
Исходные правила (demo:true, 8 шт.): найти письмо (gmail), исправить сайт (files/terminal/github), изучить исследования (browser), написать Android-приложение (terminal/github/files), подготовить документ (files/drive), обработать локальные файлы (files/terminal), календарь (calendar), задачи (todoist), + `default` без keywords.
