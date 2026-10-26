# Fokus — личный дневник задач с приоритетами. Архитектура и решения

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
  static/index.html    — интерфейс, 5 экранов (блок D)
  data/fokus.db, data/backups/   — данные (в .gitignore)
  tests/test_engine.py, test_db.py, test_api.py, test_reference.py — unittest, запуск: python3 -m unittest discover -s tests
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
| `group` | `now` 🔴, `priority` 🟠, `queue` 🔵, `delegate` ⚪, `someday` ⚫, `waiting` ⏸ |
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
  updated_at    TEXT NOT NULL
);
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
| `today_add` / `today_remove` | '' → дата | добавлена/убрана из «Сегодня» (note=`replace` при замене) |
| `carry` | N → N+1 | не выполнена к концу дня, перенесена |
| `review_required` | '' → `carried` / `stale` | система требует пройти вопросы заново |
| `done` / `reopen` | status → status | |
| `edit` | старый title → новый | изменение названия/заметки |
| `delete` | status → `deleted` | |

### 4.2 Настройки (таблица settings, ключ → JSON) и значения по умолчанию
```json
{
  "weights":    {"importance": 0.45, "urgency": 0.30, "consequence": 0.20, "duration": 0.05},
  "thresholds": {"now": 65, "priority": 45, "queue": 25},
  "day_limit":  3,
  "stale_days": 7,
  "carry_limit": 3,
  "language":   "ru",
  "last_rollover": null
}
```
Валидация при PUT: веса ≥ 0 и сумма > 0 (нормализуются движком); `thresholds.now > priority > queue > 0`; `day_limit` 1..10; `stale_days` 1..90; `carry_limit` 1..10; `language` ∈ {ru, de}. Иначе 400.

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
plan_rollover(tasks: list, today: date, settings: dict) -> dict
    # чистая функция: по списку открытых задач (dict как в API) решает, что делать при наступлении нового дня:
    # {'carry': [task_id...],           # is_today=1 и today_date < today → перенос, carry_count+1
    #  'review': {task_id: reason},     # carry_count+1 ≥ carry_limit → 'carried'; (today − evaluated_at) ≥ stale_days → 'stale'
    #  'regroup': {task_id: (old, new)}, # пересчёт по датам, только для group_manual == 0 и group ∉ {delegate, waiting}
    #  'rescore': {task_id: new_score}}  # новые баллы для всех открытых задач
```
Правила переноса (детали): задача, выбранная на день и не сделанная к следующему дню → `is_today=0`, `today_date=NULL`, `carry_count += 1`, журнал `carry`. Если `carry_count ≥ carry_limit` → `needs_review=1, review_reason='carried'`. Любая открытая задача с `evaluated_at` старше `stale_days` дней → `needs_review=1, review_reason='stale'` (если ещё не стоит). `needs_review` сбрасывается только повторным прохождением мастера (evaluate). Повторное прохождение мастера сбрасывает `carry_count=0`, `group_manual=0`, обновляет `evaluated_at`, `group_since` (если группа сменилась).

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
count_by_group(conn) -> dict                # {'now': 3, ...} только open
```
Все `datetime` в ISO 8601 без зоны (`2026-10-25T18:30:00`), даты `YYYY-MM-DD`. Время берётся локальное (Mac).

## 7. HTTP API (server.py). Все ответы JSON UTF-8, ошибки `{"error": "код", "message": "текст"}`
| Метод | Путь | Тело / параметры | Ответ |
|---|---|---|---|
| GET | `/` | | `static/index.html` |
| GET | `/api/health` | | `{ok:true, version:"1.0", today:"YYYY-MM-DD", db:"путь"}` |
| GET | `/api/i18n?lang=ru` | | словарь строк (блок E) |
| GET | `/api/settings` | | настройки §4.2 |
| PUT | `/api/settings` | частичный объект | итоговые настройки; 400 при невалидных |
| GET | `/api/tasks` | `?group=&who=&duration=&due=&status=open|done|all&sort=score|due|created&needs_review=1` | `{tasks:[Task]}` |
| POST | `/api/tasks` | `{title, note?, answers:{importance, due, consequence, who, delegate_to?, duration, depends_on?}}` | 201 `{task: Task, hints:[...]}`; 400 `{error:"validation", field:"importance"}` |
| POST | `/api/tasks/preview` | `{answers}` | `{score, group, due_date, hints}` — итог мастера без сохранения |
| GET | `/api/tasks/{id}` | | `{task}`; 404 |
| PATCH | `/api/tasks/{id}` | `{title?, note?}` | `{task}` |
| POST | `/api/tasks/{id}/evaluate` | `{answers}` | `{task, hints}` — повторный мастер |
| POST | `/api/tasks/{id}/move` | `{group}` | `{task}`; 400 при неизвестной группе |
| POST | `/api/tasks/{id}/done` | | `{task}` |
| POST | `/api/tasks/{id}/reopen` | | `{task}` |
| DELETE | `/api/tasks/{id}` | | `{task}` (status=deleted) |
| GET | `/api/today` | | `{date, limit, tasks:[Task], candidates:[Task], free_slots:int}` |
| POST | `/api/today` | `{task_id, replace_id?}` | 200 `{tasks, free_slots}`; 409 `{error:"day_full", limit, tasks}` если мест нет и нет replace_id; 400 `{error:"not_now_group"}` если задача не в группе `now`; 400 `{error:"not_open"}` |
| DELETE | `/api/today/{task_id}` | | `{tasks, free_slots}` |
| GET | `/api/board` | `?sort=score|due|created` | `{groups:{now:{count, tasks:[...]}, priority:{...}, queue, delegate, someday, waiting}, review:[Task]}` |
| GET | `/api/review` | | `{tasks:[Task]}` — needs_review=1 |
| GET | `/api/journal` | `?task_id=&limit=` | `{entries:[...]}` — каждая запись + `title` задачи (JOIN) |
| POST | `/api/rollover` | `{today?: "YYYY-MM-DD"}` | `{carried, review, regrouped, rescored, today}` |

Объект `Task` = строка таблицы как dict (`grp`→`group`), плюс `days_left` (int|null) и `hints` (list).

Правила сервера:
- Перед обработкой любого `/api/*` запроса: если `settings.last_rollover` < сегодняшней даты → выполнить rollover автоматически (один раз в день).
- «Сегодня» для сервера = локальная дата; переменная окружения `FOKUS_TODAY=YYYY-MM-DD` переопределяет (для тестов).
- Тело запроса — JSON ≤ 64 КБ, иначе 413; невалидный JSON → 400 `{error:"bad_json"}`.
- `title` после trim: 1..200 символов, иначе 400 `{error:"validation", field:"title"}`.
- Неизвестный путь `/api/...` → 404 `{error:"not_found"}`. Статика только `/` и `/index.html`.
- Сервер слушает только 127.0.0.1. Запрос с заголовком `Origin`, не равным `http://127.0.0.1:PORT`/`http://localhost:PORT`, → 403 (защита от чужих сайтов).
- Запуск: `python3 server.py` → бэкап БД → сервер (`ThreadingHTTPServer`), один глобальный `threading.Lock` на операции с БД.
- Переменные: `FOKUS_PORT` (4321), `FOKUS_DB` (путь к БД, по умолчанию `data/fokus.db` рядом со скриптом), `FOKUS_TODAY`.

## 8. Интерфейс (static/index.html)
Один файл, тёмный фон `#0f1115`, крупный белый текст (базовый 18px, задачи дня 26–32px), без внешних библиотек.
Цвета групп: now `#e5484d`, priority `#f5a623`, queue `#3b82f6`, delegate `#d4d4d8`, someday `#52525b`, waiting `#8b5cf6`.
Нижняя/боковая навигация из 5 экранов:
1. **Сегодня** — до 3 задач крупно, галочка «готово», кнопка «+ добавить из 🔴» показывает кандидатов; при полном дне — диалог «замени одну из трёх». Счётчик «перенесено N раз». Задачи с `needs_review` помечены «пройти вопросы заново».
2. **Добавить** — поле названия (подсказка «пациенты — только номер tomedo») → 6 шагов мастера (по одному вопросу на экран, крупные кнопки) → итог: группа + балл + подсказки (`/api/tasks/preview`) → «Сохранить».
3. **Доска** — 6 групп с количеством, сортировка балл/срок/дата; у задачи: балл, срок, перенос в другую группу (пометка «вручную»), «готово», «пройти вопросы заново», удалить.
4. **Фильтры** — по группе, сроку, «кто делает», длительности; результат списком.
5. **Настройки** — веса (4 ползунка/поля), пороги групп, лимит в день, язык RU/DE, журнал последних изменений.
Язык: все подписи из `/api/i18n?lang=`, переключение без перезагрузки.

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

## 10. Открытые вопросы к владельцу (не блокируют v1, приняты допущения из §9)
1. «На этой неделе» — в течение 7 дней (принято) или до воскресенья?
2. Нужна ли оценка по `who='me'` в балле (сейчас 0)?
3. Выполненные задачи: показывать статистику «сделано за неделю»? (в v1 — нет)
4. Нужен ли экспорт задач (JSON/CSV)? (в v1 — нет, есть бэкап БД)
