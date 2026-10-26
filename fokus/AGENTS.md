# Fokus — личный дневник задач с приоритетами. Архитектура и решения

Версия 2 (интерфейс в стиле Todoist, проекты, Google Календарь, Fokus.app). Разделы §12–§14 — новое; §1–§11 обновлены по месту.

Версия 1. Этот файл — единственный источник правды для всех исполнителей (код, тесты, ревью, red team).
Если код расходится с этим файлом — ошибка в коде, либо решение должно быть сначала записано сюда.

## 0. Что это и зачем
Не список дел, а инструмент выбора: что делать первым. Каждая задача проходит мастер из 6 вопросов,
получает балл 0–100 и группу (матрица Эйзенхауэра + баллы). В день — не больше 3 главных задач.

Готовые решения проверены (Eisenhower-приложения на GitHub): нужного мастера вопросов и правила «топ-3» нет → код свой.

## 1. Технические рамки (жёстко)
| Пункт | Решение |
|---|---|
| Язык | Python **только стандартная библиотека** (`sqlite3`, `http.server`, `json`, `datetime`, `shutil`, `os`, `urllib`). Синтаксис совместим с Python 3.9 (macOS-системный): никаких `match`, `X | Y` в аннотациях, `dataclass(slots=)`. |
| БД | SQLite, файл `data/fokus.db`, `PRAGMA journal_mode=WAL`, `foreign_keys=ON` |
| UI | одна страница `static/index.html` (CSS и JS внутри), без внешних библиотек и CDN |
| Адрес | `http://127.0.0.1:4321` (порт — переменная окружения `FOKUS_PORT`) |
| Запуск | `Fokus.command` двойным кликом; `cd` в папку скрипта, поэтому папка может лежать где угодно (`~/Projects/fokus`) |
| Бэкап | при каждом запуске сервера копия `data/backups/fokus-YYYYMMDD-HHMMSS.db`, хранится 10 последних |
| Журнал | каждое изменение задачи → строка в `journal` |
| Пациенты | никаких имён — только номера tomedo. Подсказка в поле названия. В тестах только обезличенные данные |
| Интеграции | в v1 нет (телефон, Мир, Todoist, календарь — позже) |
| Git | один блок/задача = один коммит |

## 2. Файлы
```
fokus/
  AGENTS.md            — этот файл
  README.md            — как запустить, что где лежит
  Fokus.command        — лаунчер macOS (bash): бэкап делает сервер; скрипт запускает server.py и открывает браузер
  server.py            — HTTP-сервер и JSON API (блок C)
  db.py                — схема SQLite, CRUD, журнал, бэкап, настройки (блок A)
  engine.py            — чистые функции: балл, группа, подсказки, топ-3, перенос (блок B)
  i18n.py              — строки интерфейса RU/DE, отдаются как /api/i18n (блок E)
  mcp_server.py        — MCP-сервер (stdio) для Claude Desktop / Claude Code / агента «Мир» (§11)
  calendar_ics.py      — Google Календарь по секретному iCal-адресу: загрузка, разбор, развёртка повторов (§12)
  static/index.html    — интерфейс в стиле Todoist: боковая панель + список (§8, v2)
  Fokus.app/           — macOS-приложение (bundle): Contents/MacOS/Fokus (bash), Info.plist, Resources/Fokus.icns (§13)
  tools/make_icon.py   — генератор иконки (PNG → ICNS) на чистом Python
  data/fokus.db, data/backups/   — данные (в .gitignore)
  tests/test_engine.py, test_db.py, test_api.py, test_reference.py, test_i18n.py, test_launcher.py, test_mcp.py — unittest, запуск: python3 -m unittest discover -s tests
```

## 3. Словарь значений (коды хранятся в БД и API; подписи RU/DE — в i18n)
| Поле | Коды |
|---|---|
| `importance` | целое 1..5 |
| `due` (вопрос 2) | `today`, `tomorrow`, `week`, `month`, `none` |
| `consequence` (вопрос 3) | `none`, `unpleasant`, `serious` |
| `who` (вопрос 4) | `me`, `delegate`; при `delegate` поле `delegate_to` — свободный текст (напр. «Света», «Мир», «другой»), может быть пустым |
| `duration` (вопрос 5) | `5m`, `30m`, `2h`, `half_day` |
| `depends_on` (вопрос 6) | текст; пустая строка/`null` = не зависит |
| `group` | `now` 🔴, `priority` 🟠, `queue` 🔵, `delegate` ⚪, `someday` ⚫, `waiting` ⏸; плюс `inbox` 📥 — задача добавлена быстро, без мастера (`evaluated=0`), в балле/переносах не участвует |
| `project` | slug проекта `[a-z0-9_-]{1,32}`; по умолчанию три: `praxis` (Praxis — дела по праксису), `privat` (Privat), `projekt` (Projekt); пользователь может добавлять свои |
| `status` | `open`, `done`, `deleted` (мягкое удаление) |

## 4. Модель данных (SQLite)
```sql
CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,              -- 1..200 символов после trim
  note          TEXT NOT NULL DEFAULT '',
  importance    INTEGER NOT NULL,           -- 1..5
  due           TEXT NOT NULL,              -- код вопроса 2
  due_date      TEXT,                       -- 'YYYY-MM-DD' или NULL (due='none')
  consequence   TEXT NOT NULL,
  who           TEXT NOT NULL,
  delegate_to   TEXT NOT NULL DEFAULT '',
  duration      TEXT NOT NULL,
  depends_on    TEXT NOT NULL DEFAULT '',
  score         REAL NOT NULL,              -- 0..100, один знак после запятой
  grp           TEXT NOT NULL,              -- группа (слово group зарезервировано в SQL)
  group_manual  INTEGER NOT NULL DEFAULT 0, -- 1 = перенесена вручную
  group_since   TEXT NOT NULL,              -- ISO datetime, когда задача попала в текущую группу
  evaluated_at  TEXT NOT NULL,              -- ISO datetime последнего прохождения мастера
  needs_review  INTEGER NOT NULL DEFAULT 0, -- 1 = система просит пройти вопросы заново
  review_reason TEXT NOT NULL DEFAULT '',   -- 'carried' | 'stale' | ''
  is_today      INTEGER NOT NULL DEFAULT 0,
  today_date    TEXT,                       -- на какую дату выбрана в «Сегодня»
  carry_count   INTEGER NOT NULL DEFAULT 0, -- перенесено N раз
  status        TEXT NOT NULL DEFAULT 'open',
  done_at       TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  project       TEXT NOT NULL DEFAULT 'praxis' REFERENCES projects(id),   -- v2
  evaluated     INTEGER NOT NULL DEFAULT 1                                -- v2: 0 = во «Входящих», мастер не пройден
);
CREATE TABLE projects (                      -- v2
  id         TEXT PRIMARY KEY,               -- slug
  name       TEXT NOT NULL,                  -- 1..40 символов
  color      TEXT NOT NULL DEFAULT '#8b8b8b',-- #rrggbb
  sort       INTEGER NOT NULL DEFAULT 0,
  archived   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
-- Умолчания: ('praxis','Praxis','#14b8a6',0), ('privat','Privat','#22c55e',1), ('projekt','Projekt','#a855f7',2).
-- Миграция v1 → v2 (db.migrate): settings.schema_version отсутствует/1 → ALTER TABLE tasks ADD COLUMN project/evaluated,
-- CREATE TABLE projects + умолчания, schema_version=2. Выполняется в connect() до любых запросов, идемпотентна.
-- Задачи во «Входящих»: grp='inbox', evaluated=0, ответы — заглушки (importance 3, due 'none', consequence 'none',
-- who 'me', duration '30m', depends_on ''), score 0.0, due_date NULL.
CREATE TABLE journal (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   INTEGER NOT NULL REFERENCES tasks(id),
  ts        TEXT NOT NULL,                  -- ISO datetime
  event     TEXT NOT NULL,                  -- см. §4.1
  old_value TEXT NOT NULL DEFAULT '',
  new_value TEXT NOT NULL DEFAULT '',
  note      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- value = JSON
```
В JSON-ответах API поле группы называется `group` (db.py переименовывает `grp` → `group`).

### 4.1 События журнала (`journal.event`)
| event | old → new | когда |
|---|---|---|
| `create` | '' → group | задача создана |
| `evaluate` | старая группа/балл `"now/72.5"` → новая | повторное прохождение мастера |
| `group_change` | группа → группа | авто-смена группы при пересчёте (rollover), note = `auto` |
| `manual_move` | группа → группа | пользователь перенёс вручную |
| `today_add` / `today_remove` | '' → дата | добавлена/убрана из «Сегодня» (note: `replace` — замена, `moved` — ручной перенос из 🔴, `evaluated` — переоценка понизила группу, `day_end` — выполненная задача прошедшего дня снята с дня при rollover) |
| `carry` | N → N+1 | не выполнена к концу дня, перенесена |
| `review_required` | '' → `carried` / `stale` | система требует пройти вопросы заново |
| `done` / `reopen` | status → status | |
| `edit` | старый title → новый | изменение названия/заметки |
| `delete` | status → `deleted` | |
| `project_change` | slug → slug | задача перенесена в другой проект |
| `create` для «Входящих» | '' → `inbox` | быстрое добавление без мастера; первый `evaluate` переводит из inbox в группу |

### 4.2 Настройки (таблица settings, ключ → JSON) и значения по умолчанию
```json
{
  "weights":    {"importance": 0.45, "urgency": 0.30, "consequence": 0.20, "duration": 0.05},
  "thresholds": {"now": 65, "priority": 45, "queue": 25},
  "day_limit":  3,
  "stale_days": 7,
  "carry_limit": 3,
  "language":   "ru",
  "last_rollover": null,
  "schema_version": 2,
  "default_project": "praxis",
  "theme": "dark",
  "timezone": "Europe/Berlin",
  "calendar_urls": [],
  "calendar_refresh_min": 15,
  "calendar_cache": null
}
```
v2: `theme` ∈ {dark, light}; `timezone` — имя IANA (проверяется через `zoneinfo`); `calendar_urls` — список строк `https://…` (до 10, каждая ≤ 2048 символов; Google отдаёт `https://calendar.google.com/calendar/ical/…/private-…/basic.ics`); `calendar_refresh_min` 5..1440; `default_project` — существующий неархивный проект. `calendar_cache` и `schema_version` — служебные, через PUT не меняются.
Валидация при PUT: веса 0..1000 и сумма > 0 (нормализуются движком); `100 ≥ thresholds.now > priority > queue > 0`; `day_limit` 1..10; `stale_days` 1..90; `carry_limit` 1..10; `language` ∈ {ru, de}. Иначе 400. `last_rollover` — служебное поле: через API не меняется (игнорируется в PUT).

## 5. Движок приоритета (engine.py, чистые функции, без БД)

### 5.1 Срок → дата
`due_date_for(due, today)`: `today`→today, `tomorrow`→today+1, `week`→today+7, `month`→today+30, `none`→None.
Решение: «на этой неделе» = в течение 7 дней (а не до воскресенья) — проще и предсказуемее; можно поменять в одном месте.

### 5.2 Баллы по измерениям (каждое 0..10)
| Измерение | Баллы |
|---|---|
| importance | `(importance − 1) × 2.5` → 1→0, 2→2.5, 3→5, 4→7.5, 5→10 |
| urgency | по `days_left = due_date − today` (в днях): нет даты→0; ≤0 (сегодня/просрочено)→10; 1→8; 2..7→6; 8..30→3; >30→0 |
| consequence | none→0, unpleasant→5, serious→10 |
| duration | 5m→10, 30m→7, 2h→4, half_day→0 (короткие задачи чуть выше — «быстрые победы») |

`who` и `depends_on` в балле не участвуют — они маршрутизируют в группу.

### 5.3 Балл
`score = Σ_i (w_i / Σw) × points_i × 10`, округление до 1 знака **half up** (`math.floor(x × 10 + 0.5) / 10`, т.е. 87.25 → 87.3, не банковское round). Диапазон 0..100. Веса из настроек, нормализуются по сумме.
Проверка: importance 5, due today, serious, 5m → 100.0; importance 1, none, none, half_day → 0.0.

### 5.4 Группа (порядок правил важен)
1. `who == 'delegate'` → `delegate` (независимо от балла и зависимости).
2. `depends_on` непустой → `waiting`.
3. `score ≥ thresholds.now` → `now`; `≥ priority` → `priority`; `≥ queue` → `queue`; иначе `someday`.

Эталонные примеры при весах/порогах по умолчанию (today = дата создания):
| Задача (обезличенно) | imp | due | cons | who | dur | dep | score | group |
|---|---|---|---|---|---|---|---|---|
| Позвонить в налоговую до конца дня | 5 | today | serious | me | 30m | — | 98.5 | now |
| Отправить отчёт по пациенту tomedo #48213 | 4 | today | serious | me | 30m | — | 87.3 | now |
| Записаться к зубному | 4 | week | unpleasant | me | 5m | — | 66.8 | now (+подсказка «сделай сразу») |
| Подготовить документы к практике | 3 | today | unpleasant | me | 30m | — | 66.0 | now |
| Купить подарок к дню рождения через месяц | 4 | month | unpleasant | me | 2h | — | 54.8 | priority |
| Оформить страховку (срока нет, критично) | 5 | none | none | me | half_day | — | 45.0 | priority |
| Разобрать фотоархив | 2 | none | none | me | 2h | — | 13.3 | someday |
| Почитать статью про ИИ | 2 | week | none | me | 30m | — | 32.8 | queue |
| Прочитать договор, средняя важность, в этом месяце | 3 | month | none | me | 30m | — | 35.0 | queue |
| Заказать канцтовары (поручить Свете) | 3 | week | unpleasant | delegate | 30m | — | 54.0 | delegate |
| Проверить сайт после обновления (поручить Миру) | 2 | none | none | delegate | 5m | — | 16.3 | delegate |
| Подать заявку — ждёт справку из банка | 5 | week | serious | me | 30m | «справка из банка» | 86.5 | waiting |
| Купить лампочку | 1 | today | none | me | 5m | — | 35.0 | queue |
| Написать благодарность коллеге | 2 | none | unpleasant | me | 30m | — | 24.8 | someday |
| Критично, завтра, последствий нет, полдня | 5 | tomorrow | none | me | half_day | — | 69.0 | now |

Расчёт строки 1: 0.45×10×10 + 0.30×10×10 + 0.20×10×10 + 0.05×7×10 = 45+30+20+3.5 = 98.5.

### 5.5 Подсказки (`hints`, список строк-кодов)
| Код | Условие |
|---|---|
| `do_now` | `duration == '5m'` и `importance ≥ 3` → «сделай сразу» |
| `delegate` | `who == 'delegate'` → «поручить: {delegate_to}» |
| `waiting` | есть зависимость |
| `someday_review` | группа `someday` → «раз в неделю: удалить или вернуть» |

### 5.6 Публичный API движка (сигнатуры обязательны — на них пишутся тесты)
```python
DEFAULT_SETTINGS: dict                      # см. §4.2
GROUPS = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
DUE_VALUES, CONSEQUENCE_VALUES, WHO_VALUES, DURATION_VALUES: tuple

validate_answers(answers: dict) -> dict     # нормализует (int importance, strip строк, delegate_to/depends_on → ''), 
                                            # бросает ValueError с кодом поля при ошибке: ValueError('importance')
due_date_for(due: str, today: date) -> Optional[date]
days_left(due_date: Optional[date], today: date) -> Optional[int]
urgency_points(due_date, today) -> float
importance_points(importance) -> float
consequence_points(consequence) -> float
duration_points(duration) -> float
compute_score(answers: dict, today: date, settings: dict) -> float     # due_date = answers['due_date'] если задан (строка или date), иначе due_date_for(answers['due'], today)
assign_group(answers: dict, score: float, settings: dict) -> str
hints_for(answers: dict, group: str) -> list
evaluate(answers: dict, today: date, settings: dict) -> dict
    # → {'score': float, 'group': str, 'due_date': 'YYYY-MM-DD'|None, 'hints': [...], 'answers': normalized}
    # если в answers есть 'due_date' (пересчёт существующей задачи) — используется он, иначе due_date_for(due, today)
rescore(task: dict, today: date, settings: dict) -> dict
    # пересчёт существующей задачи по сохранённым ответам и due_date → {'score','group'}; group_manual учитывается вызывающим кодом
suggest_today(candidates: list, today_tasks: list, limit: int) -> list
    # кандидаты = открытые задачи группы now, не в «Сегодня»; сортировка: score ↓, due_date ↑ (None в конце), created_at ↑; 
    # возвращает первые (limit − len(today_tasks)) задач, [] если мест нет
can_add_today(today_tasks: list, limit: int) -> bool
# ---- v2
INBOX = 'inbox'; INBOX_ANSWERS = {'importance': 3, 'due': 'none', 'consequence': 'none', 'who': 'me', 'delegate_to': '', 'duration': '30m', 'depends_on': ''}
parse_quick_add(text: str, projects: list) -> dict
    # «Позвонить в налоговую #Praxis» → {'title': 'Позвонить в налоговую', 'project': 'praxis'}; projects = [{'id','name'}, ...];
    # #слово сравнивается без учёта регистра с name и id; неизвестный #тег остаётся в названии, project=None; лишние пробелы схлопываются
# plan_rollover: задачи group=='inbox' пропускаются целиком (ни carry, ни review, ни rescore); suggest_today берёт только group=='now'
plan_rollover(tasks: list, today: date, settings: dict) -> dict
    # чистая функция: по списку открытых задач (dict как в API) решает, что делать при наступлении нового дня:
    # {'carry': [task_id...],           # is_today=1 и today_date < today → перенос, carry_count+1
    #  'review': {task_id: reason},     # carry_count+1 ≥ carry_limit → 'carried'; (today − evaluated_at) ≥ stale_days → 'stale'
    #  'regroup': {task_id: (old, new)}, # пересчёт по датам, только для group_manual == 0 и group ∉ {delegate, waiting}
    #  'rescore': {task_id: new_score},  # новые баллы для всех открытых задач
    #  'clear': [task_id...]}            # выполненные (status=done) задачи с is_today=1 и today_date < today → снять с дня без переноса
    # на вход подаются открытые И выполненные задачи (list_tasks(status='all'))
```
Правила переноса (детали): задача, выбранная на день и не сделанная к следующему дню → `is_today=0`, `today_date=NULL`, `carry_count += 1`, журнал `carry`. Если `carry_count ≥ carry_limit` → `needs_review=1, review_reason='carried'`. Любая открытая задача, у которой `(today − date(evaluated_at)) ≥ stale_days` (ровно 7 дней — уже stale) → `needs_review=1, review_reason='stale'` (если `needs_review` ещё не стоит). Если в один rollover задача и перенесена в 3-й раз, и залежалась — причина `carried` (проверяется первой). `needs_review` сбрасывается только повторным прохождением мастера (evaluate). Повторное прохождение мастера сбрасывает `carry_count=0`, `group_manual=0`, обновляет `evaluated_at`, `group_since` (если группа сменилась).

## 6. Слой данных (db.py). Публичные функции
```python
connect(path) -> sqlite3.Connection        # row_factory=Row, создаёт схему, применяет PRAGMA
init_db(conn)                               # CREATE TABLE IF NOT EXISTS, настройки по умолчанию
backup(db_path, backups_dir, keep=10) -> Optional[str]  # копия БД (sqlite backup API), удаляет старые сверх keep, возвращает путь или None если БД ещё нет
get_settings(conn) -> dict                  # все ключи §4.2, с умолчаниями
update_settings(conn, patch: dict) -> dict  # валидирует (§4.2), сливает, возвращает итог; ValueError('ключ') при ошибке
create_task(conn, title, note, answers, evaluation, now: datetime) -> dict   # evaluation = результат engine.evaluate; пишет journal 'create'
get_task(conn, task_id) -> Optional[dict]
list_tasks(conn, group=None, who=None, duration=None, due=None, status='open', sort='score', needs_review=None) -> list
    # sort: 'score' (↓, затем due_date ↑, created_at ↑) | 'due' (due_date ↑, None в конце, затем score ↓) | 'created' (created_at ↓)
update_task_fields(conn, task_id, fields: dict, now, event=None, old='', new='', note='') -> dict  # низкоуровневое: UPDATE + journal
edit_task(conn, task_id, title=None, note=None, now=None) -> dict          # journal 'edit' (только если что-то изменилось); now=None → datetime.now()
apply_evaluation(conn, task_id, answers, evaluation, now) -> dict          # повторный мастер: все поля ответов, score, group, group_manual=0, carry_count=0, needs_review=0, evaluated_at; journal 'evaluate'
move_task(conn, task_id, group, now) -> dict                               # ручной перенос: group_manual=1, group_since; если group != 'now' и is_today → снять с дня (journal today_remove, note 'moved'); journal 'manual_move'
set_done(conn, task_id, now) -> dict / reopen_task(conn, task_id, now) -> dict / delete_task(conn, task_id, now) -> dict
today_tasks(conn, today: date) -> list      # is_today=1 и today_date == today (open и done — выполненные остаются видны галочкой)
add_today(conn, task_id, today, now, replace_id=None, note='') -> dict      # не проверяет лимит — это делает сервер через engine.can_add_today
remove_today(conn, task_id, now, note='') -> dict
apply_rollover(conn, plan: dict, today, now) -> dict                        # применяет план engine.plan_rollover, пишет журнал, settings.last_rollover = today; возвращает счётчики {'carried','review','regrouped','rescored'}
journal(conn, task_id=None, limit=200) -> list
count_by_group(conn) -> dict                # {'now': 3, ..., 'inbox': 2} — 7 ключей, только open
# ---- v2
migrate(conn) -> int                        # приводит схему к version 2, возвращает версию; вызывается из connect()
list_projects(conn, include_archived=False) -> list        # dict: id, name, color, sort, archived, created_at, open_count
get_project(conn, project_id) -> Optional[dict]
create_project(conn, name, color=None, now=None, project_id=None) -> dict   # slug из name (транслитерация/латиница, иначе 'p-<n>'); ValueError('name'|'color'|'id')
update_project(conn, project_id, name=None, color=None, sort=None, archived=None, now=None) -> dict  # 'praxis' нельзя архивировать
delete_project(conn, project_id) -> None    # только если нет открытых и сделанных задач, иначе ValueError('not_empty'); 'praxis' нельзя
set_project(conn, task_id, project_id, now) -> dict        # journal 'project_change'
create_task(conn, title, note, answers, evaluation, now, project='praxis')  # evaluation=None и answers=None → «Входящие» (см. §4)
apply_evaluation(...)                       # дополнительно: evaluated=1 (выход из inbox)
list_tasks(..., project=None)               # фильтр по проекту; group='inbox' допустим
count_by_project(conn) -> dict              # {'praxis': 5, ...} только open
calendar_cache_get(conn) -> Optional[dict] / calendar_cache_set(conn, payload: dict)
```
Все `datetime` в ISO 8601 без зоны (`2026-10-25T18:30:00`), даты `YYYY-MM-DD`. Время берётся локальное (Mac).

## 7. HTTP API (server.py). Все ответы JSON UTF-8, ошибки `{"error": "код", "message": "текст"}`
| Метод | Путь | Тело / параметры | Ответ |
|---|---|---|---|
| GET | `/` | | `static/index.html` |
| GET | `/api/health` | | `{ok:true, version:"1.0", today:"YYYY-MM-DD", db:"путь", pid:int}` |
| GET | `/api/i18n?lang=ru` | | словарь строк (блок E) |
| GET | `/api/settings` | | настройки §4.2 |
| PUT | `/api/settings` | частичный объект | итоговые настройки; 400 при невалидных |
| GET | `/api/tasks` | `?group=&who=&duration=&due=&status=open|done|all&sort=score|due|created&needs_review=1` | `{tasks:[Task]}` |
| POST | `/api/tasks` | `{title, note?, project?, answers?:{importance, due, consequence, who, delegate_to?, duration, depends_on?}}` | 201 `{task: Task, hints:[...]}`; без `answers` → задача во «Входящих» (`group:"inbox"`); `title` может содержать `#Проект` (engine.parse_quick_add) — тогда проект берётся из текста, а из названия удаляется; 400 `{error:"validation", field:"importance"|"project"}` |
| POST | `/api/tasks/preview` | `{answers}` | `{score, group, due_date, hints}` — итог мастера без сохранения |
| GET | `/api/tasks/{id}` | | `{task}`; 404 |
| PATCH | `/api/tasks/{id}` | `{title?, note?, project?}` | `{task}`; смена проекта → журнал `project_change` |
| POST | `/api/tasks/{id}/evaluate` | `{answers}` | `{task, hints}` — повторный мастер |
| POST | `/api/tasks/{id}/move` | `{group}` | `{task}`; 400 при неизвестной группе |
| POST | `/api/tasks/{id}/done` | | `{task}`; повторный вызов для уже сделанной — идемпотентно (без записи в журнал) |
| POST | `/api/tasks/{id}/reopen` | | `{task}`; идемпотентно для открытой |
| DELETE | `/api/tasks/{id}` | | `{task}` (status=deleted) |
| GET | `/api/today` | | `{date, limit, tasks:[Task], candidates:[Task], pool:[Task], free_slots:int, events:[Event], calendar:{configured:bool, fetched_at, error}}` — `candidates` = предложение на свободные места (пусто при полном дне), `pool` = все открытые задачи 🔴 вне дня; `events` — события календаря на эту дату (§12) |
| GET | `/api/upcoming` | `?days=7` (1..31) | `{from, days:[{date, weekday, tasks:[Task], events:[Event]}], overdue:[Task]}` — задачи по `due_date` (open), события календаря |
| GET | `/api/inbox` | | `{tasks:[Task]}` — `group='inbox'`, порядок created_at ↓ |
| GET | `/api/projects` | `?archived=1` | `{projects:[{id,name,color,sort,archived,open_count}]}` |
| POST | `/api/projects` | `{name, color?}` | 201 `{project}`; 400 validation |
| PATCH | `/api/projects/{id}` | `{name?, color?, sort?, archived?}` | `{project}`; 404; 400 `{error:"validation", field:"archived"}` для `praxis` |
| DELETE | `/api/projects/{id}` | | `{ok:true}`; 409 `{error:"not_empty"}`; 400 для `praxis` |
| GET | `/api/calendar` | `?from=YYYY-MM-DD&to=YYYY-MM-DD` (≤ 62 дней; по умолчанию сегодня..+7) | `{events:[Event], configured, fetched_at, error}` — из кэша; если кэш старше `calendar_refresh_min` минут → обновляется в фоне (ответ из старого кэша не ждёт сети) |
| POST | `/api/calendar/refresh` | | синхронно загружает все `calendar_urls` (таймаут 10 с каждый), `{events_count, fetched_at, errors:[...]}`; 400 `{error:"not_configured"}` |
| POST | `/api/shutdown` | | `{ok:true}` и сервер завершается через 0.5 с (кнопка «Выключить Fokus» в приложении) |

`Event` = `{uid, summary, location, start, end, all_day, calendar}`; `start`/`end` — `YYYY-MM-DDTHH:MM` в часовом поясе настроек или `YYYY-MM-DD` для событий на весь день (`end` исключительный).
| POST | `/api/today` | `{task_id, replace_id?}` | 200 `{tasks, free_slots}`; 409 `{error:"day_full", limit, tasks}` если мест нет и нет replace_id; 400 `{error:"not_now_group"}` если задача не в группе `now`; 400 `{error:"not_open"}` |
| DELETE | `/api/today/{task_id}` | | `{tasks, free_slots}` |
| GET | `/api/board` | `?sort=score|due|created&project=` | `{groups:{now:{count, tasks:[...]}, priority:{...}, queue, delegate, someday, waiting, inbox}, review:[Task], projects:[...]}` |
| GET | `/api/review` | | `{tasks:[Task]}` — needs_review=1 |
| GET | `/api/journal` | `?task_id=&limit=` | `{entries:[...]}` — каждая запись + `title` задачи (JOIN) |
| POST | `/api/rollover` | `{today?: "YYYY-MM-DD"}` | `{carried, review, regrouped, rescored, cleared, today}`. 400, если `today` раньше `last_rollover` (назад во времени нельзя) или `today` в будущем без тестового режима `FOKUS_TODAY` |

Объект `Task` = строка таблицы как dict (`grp`→`group`), плюс `days_left` (int|null), `hints` (list), `project` (slug), `project_name`, `project_color`, `evaluated` (0|1).
Задачу из `inbox` нельзя поставить в «Сегодня» (400 `not_now_group`) и переносить вручную в группу (400 `validation` field `group`) — сначала мастер.

Правила сервера:
- Перед обработкой любого `/api/*` запроса: если `settings.last_rollover` < сегодняшней даты → выполнить rollover автоматически (один раз в день).
- «Сегодня» для сервера = локальная дата; переменная окружения `FOKUS_TODAY=YYYY-MM-DD` переопределяет (для тестов).
- Тело запроса — JSON ≤ 64 КБ, иначе 413; невалидный JSON → 400 `{error:"bad_json"}`.
- `title` после trim: 1..200 символов, иначе 400 `{error:"validation", field:"title"}`.
- Неизвестный путь `/api/...` → 404 `{error:"not_found"}`. Статика только `/` и `/index.html`.
- Изменяющие операции (PATCH, DELETE, evaluate, move, done, reopen, today) над удалённой задачей → 404; `GET /api/tasks/{id}` удалённую отдаёт (status=deleted).
- JSON-константы `Infinity`/`NaN` → 400 `bad_json`; id вне 1..2^63−1 → 400/404; `title` из одних невидимых символов (zero-width) → 400.
- Бэкап при старте не блокирует запуск: при ошибке — предупреждение в stderr, сервер работает; недоступная БД → понятное сообщение и код 1.
- Сервер слушает только 127.0.0.1. Запрос с заголовком `Origin`, не равным `http://127.0.0.1:PORT`/`http://localhost:PORT`, → 403 (защита от чужих сайтов).
- Запуск: `python3 server.py` → бэкап БД → сервер (`ThreadingHTTPServer`), один глобальный `threading.Lock` на операции с БД.
- Переменные: `FOKUS_PORT` (4321), `FOKUS_DB` (путь к БД, по умолчанию `data/fokus.db` рядом со скриптом), `FOKUS_TODAY`.

## 8. Интерфейс v2 (static/index.html) — «как Todoist, но под мои возможности»
Один файл, без внешних библиотек. Тёмная тема по умолчанию (`theme: dark`), светлая — по настройке. Шрифт системный, базовый 15px, строки списка 44–48px, крупные заголовки.
Цвета (тёмная): фон окна `#1f1f1f`, боковая панель `#282828`, карточки/ховер `#2e2e2e`, границы `#3a3a3a`, текст `#f2f2f2`, вторичный `#a3a3a3`, акцент (кнопка «Добавить») `#e5484d`.
Цвета групп как в v1: now `#e5484d`, priority `#f5a623`, queue `#3b82f6`, delegate `#d4d4d8`, someday `#52525b`, waiting `#8b5cf6`, inbox `#a3a3a3`.

**Раскладка**: слева боковая панель 260px (сворачивается кнопкой ☰ на узких экранах), справа основная область до 760px по центру.
Боковая панель сверху вниз:
1. Заголовок «Fokus» + дата; кнопка **«+ Добавить задачу»** (красная, как в Todoist; клавиша `q`).
2. Навигация: **Сегодня** (счётчик «n из limit»), **Входящие** (счётчик), **Предстоящие** (7 дней: календарь + задачи по сроку), **Нужна переоценка** (показывается только если > 0).
3. Раздел **Группы**: 🔴 Сейчас (группа `now`) · 🟠 Приоритет · 🔵 Очередь · ⚪ Делегировать · ⚫ Отложить · ⏸ Ждут — с цветной точкой и счётчиком открытых.
4. Раздел **Проекты**: Praxis · Privat · Projekt · … (цветная точка, счётчик) + «+ Проект». Клик — список задач проекта, сгруппированный по группам.
5. Внизу: **Настройки**, переключатель RU/DE, кнопка «Выключить Fokus».

**Список задач** (общий компонент для всех видов): строка = круглый чекбокс (обводка цветом группы; клик → сделано с анимацией и тостом «Сделано · Отменить»), название, справа тонкие бейджи; вторая строка мелко: срок (красным, если сегодня/просрочено), длительность, балл, проект (цветная точка + имя), «перенесено N раз», «вручную», «пройти вопросы заново». При наведении — иконки действий: ✎ изменить, ⇄ группа, 📁 проект, ☆ в «Сегодня» (только для `now`), 🗑. Клик по строке — **панель задачи** справа (или модально на узком экране): название (редактируемое), заметка, проект, все 6 ответов с подписями, балл и группа, кнопки «Пройти вопросы заново», «В «Сегодня»/Убрать из дня», журнал задачи.
**Быстрое добавление**: по кнопке или `q` вверху списка раскрывается композер: поле «Название задачи… (#Praxis, #Privat — проект)», подсказка про tomedo, выбор проекта (чип), кнопки **«Оценить» (Enter)** → модальный мастер 6 вопросов → итог (группа, балл, подсказки) → «Сохранить» (+ предложение поставить в «Сегодня», если 🔴 и есть место) и **«Во Входящие» (Shift+Enter)** → сохранить без мастера. `Esc` закрывает.
**Сегодня**: заголовок «Сегодня · пн, 27 окт.» и «n из limit»; секция «Задачи дня» (строки чуть крупнее, нумерация 1–3; при полном дне кнопка «+» открывает выбор замены); секция «Календарь» — события дня из Google (время, название, место), если календарь не подключён — строка-ссылка «Подключить Google Календарь»; секция «Кандидаты из 🔴» с кнопкой ☆ «в день»; при полном дне — «Заменить одну из задач дня».
**Входящие**: список + у каждой строки кнопка «Оценить» (мастер). Пустое состояние: «Входящие пусты. Нажми q, чтобы быстро записать задачу».
**Предстоящие**: сначала «Просрочено» (если есть), затем 7 дней: заголовок дня (дата, день недели), события календаря, задачи с этим сроком.
**Группа / Проект**: заголовок с описанием группы, сортировка (балл/срок/дата), фильтр-чипы по проекту (в группе) или по группе (в проекте).
**Нужна переоценка**: список с причиной и кнопкой «Пройти вопросы заново».
**Настройки**: тема (тёмная/светлая), язык, лимит задач в день, переоценка (дней/переносов), веса и пороги (свернуты в «Расширенные»), проекты (переименовать/цвет/архив/добавить), **Google Календарь**: поле «Секретные адреса iCal (по одному в строке)» + пошаговая подсказка, где взять адрес, кнопка «Проверить и обновить» (показывает число событий и ошибки), часовой пояс; журнал (последние 50); кнопка «Выключить Fokus».
**Клавиатура**: `q` — добавить, `Esc` — закрыть, `1`–`6` в мастере — выбор варианта, `Enter` — далее.
**Состояния**: ошибка сети → тост «Сервер не отвечает»; смена дня → тост и перерисовка; загрузка — скелетоны строк. Все тексты — из `/api/i18n`, DE полностью.
**Без innerHTML с пользовательскими данными** — только `textContent`.

## 9. Решения, принятые координатором (можно оспорить — тогда правим здесь и в коде)
| № | Решение | Почему |
|---|---|---|
| 1 | `week` = +7 дней, `month` = +30 дней (а не календарные) | предсказуемо, одинаково в любой день недели |
| 2 | Срочность считается от `due_date` и пересчитывается каждый день автоматически (rollover) | «приоритет со временем меняется» без участия пользователя |
| 3 | Авто-пересчёт не трогает группу задач, перенесённых вручную, и групп `delegate`/`waiting` | ручное решение сильнее формулы |
| 4 | `delegate` сильнее `waiting` | поручаемую задачу надо отдать сейчас, зависимость — забота исполнителя |
| 5 | В «Сегодня» можно добавить только задачу из группы `now` | по спецификации; хочется другую — сначала перенести вручную в 🔴 |
| 6 | Выполненные задачи дня остаются на экране «Сегодня» до конца дня (галочка) | видно прогресс |
| 7 | Повторный мастер сбрасывает счётчик переносов и ручную группу | иначе требование пройти вопросы повторялось бы бесконечно |
| 8 | Удаление мягкое (`status=deleted`) | журнал и история не теряются |
| 9 | Веса нормализуются по сумме | пользователь может вводить любые числа, балл остаётся 0..100 |
| 10 | Повторное добавление задачи, уже стоящей в «Сегодня», — идемпотентно (200, без изменений и без записи в журнал) | защита от двойного клика |
| 11 | Модифицирующие функции db бросают `db.NotFound` (KeyError) для несуществующей задачи; `get_task` → None | сервер превращает в 404 |
| 12 | `get_task` удалённой задачи возвращает dict со `status='deleted'`; `list_tasks` её не показывает ни при каком `status` | мягкое удаление |
| 13 | `update_settings` атомарна: невалидный patch не применяется даже частично | предсказуемость |
| 14 | Выполненная задача дня после смены дня снимается с дня (`today_remove`, note `day_end`) без переноса и без счётчика | иначе reopen назавтра оставлял задачу в «подвешенном» состоянии, а rollover считал ложный перенос |
| 15 | UI: один запрос за раз (повторный клик игнорируется, пока идёт предыдущий); сервер: done/reopen идемпотентны | двойной клик не создаёт дублей |
| 16 | UI раз в минуту и при возврате на вкладку проверяет дату сервера; при смене дня — уведомление и перерисовка | вкладка, оставленная с вечера |
| 17 | Название и заметку можно изменить кнопкой ✎ (PATCH, журнал `edit`) | опечатка в номере tomedo не требует пересоздания |
| 18 | Два сервера на одной БД (разные порты) не поддерживаются: лаунчер защищает от второго запуска на том же порту | известное ограничение v1 |

## 10. Открытые вопросы к владельцу (не блокируют v1, приняты допущения из §9)
1. «На этой неделе» — в течение 7 дней (принято) или до воскресенья?
2. Нужна ли оценка по `who='me'` в балле (сейчас 0)?
3. Выполненные задачи: показывать статистику «сделано за неделю»? (в v1 — нет)
4. Нужен ли экспорт задач (JSON/CSV)? (в v1 — нет, есть бэкап БД)

## 11. MCP-сервер (mcp_server.py)
Fokus доступен ИИ-агентам по Model Context Protocol (транспорт stdio, JSON-RPC 2.0 по строкам). Реализация — только stdlib.
| Решение | Почему |
|---|---|
| Тонкий клиент к HTTP API (§7), а не прямой доступ к БД | вся логика (баллы, лимит дня, авто-перенос, Lock) остаётся в одном процессе server.py; два процесса не гоняются за rollover |
| Если Fokus не запущен — MCP-сервер сам запускает `server.py` (отключить: `FOKUS_MCP_AUTOSTART=0`); stdout сервера уходит в `data/server.log` | в stdout MCP-процесса допустим только JSON-RPC |
| Версии протокола: `2024-11-05`, `2025-03-26`, `2025-06-18` (клиентская, если известна; иначе последняя) | совместимость с Claude Desktop и Claude Code |
| Ошибки Fokus (400/404/409) → результат инструмента с `isError: true` и человекочитаемым текстом; `day_full` возвращает список задач дня, чтобы агент спросил, какую заменить | агент получает контекст для следующего шага, а не исключение |
| Результат инструмента: `content[0].text` (краткая сводка по-русски) + `structuredContent` (ответ API как есть) | текст — для модели, структура — для точных id/баллов |

Инструменты: `fokus_today`, `fokus_preview`, `fokus_add_task` (с `add_to_today`), `fokus_board`, `fokus_list_tasks`, `fokus_get_task`,
`fokus_done`, `fokus_reopen`, `fokus_today_add` (с `replace_id`), `fokus_today_remove`, `fokus_move`, `fokus_reevaluate`, `fokus_edit`,
`fokus_delete`, `fokus_review`, `fokus_journal`, `fokus_settings`. Промпт `fokus_wizard` — сценарий 6 вопросов для агента.
Коды ответов (`today`, `5m`, `now` …) те же, что в §3. Установка: `python3 mcp_server.py --install` (Claude Desktop, со слиянием конфига и копией `.bak`)
или `claude mcp add fokus -- python3 ~/Projects/fokus/mcp_server.py` (Claude Code). Тесты: `tests/test_mcp.py`.

## 12. Google Календарь (calendar_ics.py) — только чтение, без OAuth
Пользователь копирует в Настройках «Секретный адрес в формате iCal» (Google Календарь → Настройки → календарь → «Интеграция календаря»). Fokus загружает `.ics` по https, разбирает и показывает события в «Сегодня» и «Предстоящие». Записи в календарь нет (это v3 через OAuth).
Публичный API модуля (чистые функции, только stdlib; `zoneinfo` для часовых поясов):
```python
fetch_ics(url: str, timeout: float = 10.0, max_bytes: int = 5_000_000) -> str   # только https; ValueError('url') / OSError
unfold(text: str) -> List[str]                 # склейка продолжений строк (RFC 5545 §3.1), CRLF/LF
parse_ics(text: str) -> List[dict]             # VEVENT → {'uid','summary','location','description','dtstart','dtend','all_day','rrule','exdates','recurrence_id','duration','status'}
    # dtstart/dtend: date (VALUE=DATE) или datetime с tzinfo (TZID=… через zoneinfo; Z → UTC; без зоны → "floating", трактуется в tz календаря X-WR-TIMEZONE или заданном)
    # DTEND отсутствует → для даты +1 день, для datetime = dtstart (+DURATION, если есть); STATUS:CANCELLED → пропускается
expand(vevents: List[dict], start: date, end: date, tz: str) -> List[dict]   # вхождения в [start, end): RRULE FREQ=DAILY|WEEKLY|MONTHLY|YEARLY, INTERVAL, COUNT, UNTIL, BYDAY (в т.ч. 2MO, -1FR для MONTHLY), BYMONTHDAY, BYMONTH, WKST; EXDATE; RECURRENCE-ID переопределяет конкретное вхождение; неизвестные части RRULE → игнорировать, но не падать
    # результат Event (§7): start/end в tz как 'YYYY-MM-DDTHH:MM' или 'YYYY-MM-DD' (all_day), отсортирован по start; ограничение 5000 вхождений
refresh(urls: List[str], today: date, tz: str, days_back: int = 7, days_forward: int = 60, fetch=fetch_ics) -> dict
    # {'events': [...], 'fetched_at': 'ISO', 'errors': [{'url_index': i, 'message': '...'}], 'window': ['YYYY-MM-DD','YYYY-MM-DD']}; одна сломанная ссылка не отменяет остальные; 'calendar' = индекс ссылки (0..)
events_between(cache: dict, start: date, end: date) -> List[dict]
```
Сервер: кэш в `settings.calendar_cache`; `/api/today` и `/api/upcoming` берут события из кэша; если кэш старше `calendar_refresh_min` → обновление в фоновом потоке (не под Lock на время сети; запись результата — под Lock). Секретный адрес — это пароль к календарю: не логировать, не отдавать в `/api/health`, в `/api/settings` показывать как есть (локальное приложение), в экспорт/бэкап он попадает вместе с БД.

## 13. Fokus.app (macOS-приложение)
Папка `Fokus.app` внутри проекта — обычный bundle без подписи: `Contents/Info.plist` (CFBundleName Fokus, CFBundleIdentifier de.fokus.app, CFBundleExecutable Fokus, CFBundleIconFile Fokus, LSMinimumSystemVersion 11.0), `Contents/MacOS/Fokus` (bash 3.2, исполняемый), `Contents/Resources/Fokus.icns` (генерируется `tools/make_icon.py` без PIL: PNG через zlib → ICNS с типами icp4, icp5, icp6, ic07, ic08, ic09, ic10 = 16…1024 px).
Скрипт `Contents/MacOS/Fokus`: корень проекта = `../../..` от скрипта (разрешая симлинк); если `/api/health` отвечает — только открыть окно; иначе запустить `python3 server.py` через `nohup … >> data/server.log 2>&1 &` (сервер живёт после выхода скрипта), подождать health до 8 с; открыть окно: если установлен Chromium-браузер (Google Chrome, Chromium, Brave, Microsoft Edge, Arc) → `open -na "<браузер>" --args --app=http://127.0.0.1:PORT` (окно без адресной строки), иначе `open URL` в браузере по умолчанию; при ошибке — `osascript -e 'display dialog …'` с понятным текстом. Терминал не открывается. Остановка — кнопка «Выключить Fokus» в Настройках (`POST /api/shutdown`) или `Fokus.command` → Ctrl+C.
Карантин: при скачивании zip с GitHub macOS ставит quarantine → «не удаётся проверить разработчика» → правый клик → «Открыть» (один раз). При `git clone` карантина нет. Альтернатива окну: Safari → «Файл → Добавить в Dock» на http://127.0.0.1:4321.

## 14. Решения v2
| № | Решение | Почему |
|---|---|---|
| 19 | «Входящие» — задачи без мастера (`inbox`, `evaluated=0`): в балле, переносах, «Сегодня» не участвуют, пока не пройдут мастер | быстрый захват как в Todoist, но правило «каждая задача проходит фильтры» сохраняется |
| 20 | Проекты — плоский список (Praxis/Privat/Projekt + свои), без вложенности и секций | достаточно для одного человека; вложенность усложняет выбор |
| 21 | Календарь — только чтение по iCal-адресу, без OAuth и без записи | подключение за минуту без Google Cloud; запись в календарь — v3 |
| 22 | Fokus.app — bash-bundle без подписи; окно через `--app=` Chromium-браузера, иначе обычная вкладка | нет компиляции и зависимостей; Safari «Добавить в Dock» как альтернатива |
| 23 | Тёмная тема по умолчанию, светлая — опция | по требованию владельца |
| 24 | Клавиша `q` — добавить, `Esc` — закрыть, Enter в композере — мастер, Shift+Enter — во Входящие | как в Todoist, с уклоном в «сначала оцени» |
| 25 | Окно календаря — по пересечению: многодневное событие, начавшееся до окна, возвращается; `refresh` берёт `[today−7, today+60)`; `calendar` у `expand` по умолчанию 0; переопределение (RECURRENCE-ID) со STATUS:CANCELLED убирает вхождение; BOM в начале .ics игнорируется | как ведёт себя Google Календарь |
| 26 | `create_project` с уже занятым именем → slug с суффиксом `-2`, `-3`…; удалить проект можно, только если на него не ссылается ни одна задача любого статуса (иначе `not_empty`); неизвестный проект/задача в функциях db → `db.NotFound` | внешний ключ `tasks.project` |
| 27 | `#Тег` в названии сильнее явного `project`; задача без проекта и тега получает `settings.default_project`; `list_tasks()` без фильтра включает `inbox`; `count_by_project` отдаёт все неархивные проекты, в т.ч. с нулём | предсказуемо для быстрого ввода |
| 28 | `/api/upcoming`: `weekday` — ISO-номер 1..7 (понедельник = 1); `/api/calendar` принимает окно до 62 дней включительно; `/api/calendar/refresh` при недоступном хосте → 200 с `errors` (не 5xx) | одна сломанная ссылка не ломает экран |
