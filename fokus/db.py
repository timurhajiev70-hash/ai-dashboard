# -*- coding: utf-8 -*-
"""Fokus — слой данных (блок A): схема SQLite, CRUD, журнал изменений, бэкап, настройки.

Единственный источник правды — AGENTS.md (§3, §4, §4.1, §4.2, §6).
Только стандартная библиотека; синтаксис совместим с Python 3.9.

Договорённости:
- все datetime хранятся как ISO 8601 без зоны (``2026-10-25T18:30:00``), даты — ``YYYY-MM-DD``;
- задача отдаётся как dict со всеми колонками таблицы, колонка ``grp`` переименована в ``group``;
- ``days_left``/``hints`` db не считает — это делает сервер через engine;
- функции, меняющие задачу, бросают ``NotFound`` (подкласс KeyError), если задачи нет;
  ``get_task`` возвращает None;
- каждое изменение задачи обновляет ``updated_at`` и (кроме rescore) пишет строку в ``journal``.
"""
import copy
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- словарь §3 / §4.2

GROUPS = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
DUE_VALUES = ('today', 'tomorrow', 'week', 'month', 'none')
CONSEQUENCE_VALUES = ('none', 'unpleasant', 'serious')
WHO_VALUES = ('me', 'delegate')
DURATION_VALUES = ('5m', '30m', '2h', 'half_day')
STATUS_VALUES = ('open', 'done', 'deleted')
LANGUAGES = ('ru', 'de')

DEFAULT_SETTINGS = {
    'weights': {'importance': 0.45, 'urgency': 0.30, 'consequence': 0.20, 'duration': 0.05},
    'thresholds': {'now': 65, 'priority': 45, 'queue': 25},
    'day_limit': 3,
    'stale_days': 7,
    'carry_limit': 3,
    'language': 'ru',
    'last_rollover': None,
}

# целочисленные настройки: ключ -> (мин, макс)
_INT_SETTINGS = {'day_limit': (1, 10), 'stale_days': (1, 90), 'carry_limit': (1, 10)}

TASK_COLUMNS = (
    'id', 'title', 'note', 'importance', 'due', 'due_date', 'consequence', 'who',
    'delegate_to', 'duration', 'depends_on', 'score', 'grp', 'group_manual', 'group_since',
    'evaluated_at', 'needs_review', 'review_reason', 'is_today', 'today_date', 'carry_count',
    'status', 'done_at', 'created_at', 'updated_at',
)
_UPDATABLE = frozenset(TASK_COLUMNS) - frozenset(('id',))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  importance    INTEGER NOT NULL,
  due           TEXT NOT NULL,
  due_date      TEXT,
  consequence   TEXT NOT NULL,
  who           TEXT NOT NULL,
  delegate_to   TEXT NOT NULL DEFAULT '',
  duration      TEXT NOT NULL,
  depends_on    TEXT NOT NULL DEFAULT '',
  score         REAL NOT NULL,
  grp           TEXT NOT NULL,
  group_manual  INTEGER NOT NULL DEFAULT 0,
  group_since   TEXT NOT NULL,
  evaluated_at  TEXT NOT NULL,
  needs_review  INTEGER NOT NULL DEFAULT 0,
  review_reason TEXT NOT NULL DEFAULT '',
  is_today      INTEGER NOT NULL DEFAULT 0,
  today_date    TEXT,
  carry_count   INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'open',
  done_at       TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   INTEGER NOT NULL REFERENCES tasks(id),
  ts        TEXT NOT NULL,
  event     TEXT NOT NULL,
  old_value TEXT NOT NULL DEFAULT '',
  new_value TEXT NOT NULL DEFAULT '',
  note      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_status_grp ON tasks(status, grp);
CREATE INDEX IF NOT EXISTS idx_tasks_today ON tasks(is_today, today_date);
CREATE INDEX IF NOT EXISTS idx_journal_task ON journal(task_id, id);
"""

_SORTS = {
    'score': 'score DESC, (due_date IS NULL) ASC, due_date ASC, created_at ASC, id ASC',
    'due': '(due_date IS NULL) ASC, due_date ASC, score DESC, created_at ASC, id ASC',
    'created': 'created_at DESC, id DESC',
}

_BACKUP_RE = re.compile(r'^fokus-(\d{8}-\d{6})(?:-(\d+))?\.db$')


class NotFound(KeyError):
    """Задача с таким id не существует."""


# --------------------------------------------------------------------------- вспомогательные

def _s(value: Any) -> str:
    """None -> '', остальное -> str."""
    return '' if value is None else str(value)


def _ts(now: Any) -> str:
    """datetime -> ISO без зоны с точностью до секунд; строка — как есть; None -> сейчас."""
    if now is None:
        now = datetime.now()
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)
        return now.isoformat(timespec='seconds')
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day).isoformat(timespec='seconds')
    return str(now)


def _dstr(value: Any) -> Optional[str]:
    """date/datetime/строка -> 'YYYY-MM-DD' или None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _fmt_score(score: Any) -> str:
    try:
        return '%.1f' % float(score)
    except (TypeError, ValueError):
        return _s(score)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _as_int(value: Any) -> Optional[int]:
    """Целое число (int или float с целым значением); bool и прочее -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for key in row.keys():
        out['group' if key == 'grp' else key] = row[key]
    return out


def _fetch_row(conn: sqlite3.Connection, task_id: Any) -> sqlite3.Row:
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise NotFound(task_id)
    row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
    if row is None:
        raise NotFound(tid)
    return row


def _log(conn: sqlite3.Connection, task_id: int, event: str, old: Any, new: Any, note: Any, now: Any) -> None:
    """Строка журнала (§4.1). Без commit — вызывающий код управляет транзакцией."""
    conn.execute(
        'INSERT INTO journal (task_id, ts, event, old_value, new_value, note) VALUES (?, ?, ?, ?, ?, ?)',
        (int(task_id), _ts(now), _s(event), _s(old), _s(new), _s(note)),
    )


def _update(conn: sqlite3.Connection, task_id: int, fields: Dict[str, Any], now: Any) -> None:
    """UPDATE tasks SET ... (+ updated_at = now). Без commit. 'group' -> 'grp'."""
    sets = []  # type: List[str]
    values = []  # type: List[Any]
    for key, value in fields.items():
        col = 'grp' if key == 'group' else key
        if col == 'updated_at':
            continue
        if col not in _UPDATABLE:
            raise ValueError(str(key))
        sets.append('%s = ?' % col)
        values.append(value)
    sets.append('updated_at = ?')
    values.append(_ts(now))
    values.append(int(task_id))
    conn.execute('UPDATE tasks SET %s WHERE id = ?' % ', '.join(sets), values)


def _normalize_answers(answers: Any) -> Dict[str, Any]:
    """Ответы мастера -> колонки задачи. ValueError('поле') при неверном коде (§3)."""
    if not isinstance(answers, dict):
        raise ValueError('answers')
    importance = _as_int(answers.get('importance'))
    if importance is None or not 1 <= importance <= 5:
        raise ValueError('importance')
    due = _s(answers.get('due')).strip()
    if due not in DUE_VALUES:
        raise ValueError('due')
    consequence = _s(answers.get('consequence')).strip()
    if consequence not in CONSEQUENCE_VALUES:
        raise ValueError('consequence')
    who = _s(answers.get('who')).strip()
    if who not in WHO_VALUES:
        raise ValueError('who')
    duration = _s(answers.get('duration')).strip()
    if duration not in DURATION_VALUES:
        raise ValueError('duration')
    return {
        'importance': importance,
        'due': due,
        'consequence': consequence,
        'who': who,
        'delegate_to': _s(answers.get('delegate_to')).strip(),
        'duration': duration,
        'depends_on': _s(answers.get('depends_on')).strip(),
    }


def _parse_evaluation(evaluation: Any):
    """Результат engine.evaluate -> (score, group, due_date)."""
    if not isinstance(evaluation, dict):
        raise ValueError('evaluation')
    group = evaluation.get('group')
    if group not in GROUPS:
        raise ValueError('group')
    try:
        score = float(evaluation.get('score'))
    except (TypeError, ValueError):
        raise ValueError('score')
    if not math.isfinite(score):
        raise ValueError('score')
    return score, group, _dstr(evaluation.get('due_date'))


# --------------------------------------------------------------------------- соединение и схема

def connect(path: Any) -> sqlite3.Connection:
    """Открывает БД (создавая папку), row_factory=Row, WAL, foreign_keys=ON, init_db."""
    path = os.fspath(path)
    if path != ':memory:':
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')  # для :memory: вернёт 'memory' — не ошибка
    except sqlite3.Error:
        pass
    conn.execute('PRAGMA foreign_keys=ON')
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS + настройки по умолчанию (не перезаписывает существующие)."""
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, json.dumps(value, ensure_ascii=False)),
        )
    conn.commit()


# --------------------------------------------------------------------------- бэкап

def backup(db_path: Any, backups_dir: Any, keep: int = 10) -> Optional[str]:
    """Копия БД через sqlite backup API в backups_dir/fokus-YYYYMMDD-HHMMSS.db.

    Возвращает путь к копии или None, если файла БД ещё нет. Хранит ``keep`` последних
    (по времени в имени файла), остальные удаляет. При двух бэкапах в одну секунду
    добавляет суффикс ``-1``, ``-2``...
    """
    db_path = os.fspath(db_path)
    backups_dir = os.fspath(backups_dir)
    if not os.path.isfile(db_path):
        return None
    os.makedirs(backups_dir, exist_ok=True)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 10
    if keep < 1:
        keep = 1

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    # Несколько бэкапов в одну секунду: суффикс = максимальный существующий для этой метки + 1
    # (а не первое свободное имя — иначе после удаления старых новый файл сортировался бы как старый).
    suffix = -1
    for name in os.listdir(backups_dir):
        m = _BACKUP_RE.match(name)
        if m and m.group(1) == stamp:
            suffix = max(suffix, int(m.group(2) or 0))
    suffix += 1
    if suffix == 0:
        dest = os.path.join(backups_dir, 'fokus-%s.db' % stamp)
    else:
        dest = os.path.join(backups_dir, 'fokus-%s-%d.db' % (stamp, suffix))

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # удалить самые старые сверх keep
    found = []
    for name in os.listdir(backups_dir):
        m = _BACKUP_RE.match(name)
        if m:
            found.append((m.group(1), int(m.group(2) or 0), name))
    found.sort()
    excess = found[:-keep] if len(found) > keep else []
    for _stamp, _suffix, name in excess:
        full = os.path.join(backups_dir, name)
        if os.path.abspath(full) == os.path.abspath(dest):
            continue  # только что созданную копию не удаляем никогда
        try:
            os.remove(full)
        except OSError:
            pass
    return dest


# --------------------------------------------------------------------------- настройки

def _set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_settings(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Полные настройки §4.2: сохранённые значения поверх умолчаний (вложенные — по ключам)."""
    result = copy.deepcopy(DEFAULT_SETTINGS)
    for row in conn.execute('SELECT key, value FROM settings').fetchall():
        key = row['key']
        if key not in DEFAULT_SETTINGS:
            continue
        try:
            value = json.loads(row['value'])
        except (TypeError, ValueError):
            continue
        if isinstance(DEFAULT_SETTINGS[key], dict):
            if isinstance(value, dict):
                for sub, sub_value in value.items():
                    if sub in result[key]:
                        result[key][sub] = sub_value
        else:
            result[key] = value
    return result


def update_settings(conn: sqlite3.Connection, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Частичное обновление настроек с валидацией §4.2. ValueError('ключ') при ошибке.

    Неизвестные ключи (и неизвестные под-ключи weights/thresholds) игнорируются.
    """
    if not isinstance(patch, dict):
        raise ValueError('settings')
    current = get_settings(conn)
    changed = {}  # type: Dict[str, Any]

    for key, value in patch.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key == 'weights':
            if not isinstance(value, dict):
                raise ValueError(key)
            merged = dict(current['weights'])
            for sub, sub_value in value.items():
                if sub not in merged:
                    continue
                if not _is_number(sub_value) or sub_value < 0:
                    raise ValueError(key)
                merged[sub] = float(sub_value)
            if sum(merged.values()) <= 0:
                raise ValueError(key)
            changed[key] = merged
        elif key == 'thresholds':
            if not isinstance(value, dict):
                raise ValueError(key)
            merged = dict(current['thresholds'])
            for sub, sub_value in value.items():
                if sub not in merged:
                    continue
                if not _is_number(sub_value):
                    raise ValueError(key)
                merged[sub] = sub_value
            if not (merged['now'] > merged['priority'] > merged['queue'] > 0):
                raise ValueError(key)
            changed[key] = merged
        elif key in _INT_SETTINGS:
            low, high = _INT_SETTINGS[key]
            ivalue = _as_int(value)
            if ivalue is None or not low <= ivalue <= high:
                raise ValueError(key)
            changed[key] = ivalue
        elif key == 'language':
            if value not in LANGUAGES:
                raise ValueError(key)
            changed[key] = value
        elif key == 'last_rollover':
            if value is None:
                changed[key] = None
            elif isinstance(value, (date, datetime)):
                changed[key] = _dstr(value)
            elif isinstance(value, str):
                try:
                    date.fromisoformat(value.strip())
                except ValueError:
                    raise ValueError(key)
                changed[key] = value.strip()
            else:
                raise ValueError(key)

    with conn:
        for key, value in changed.items():
            _set_setting(conn, key, value)
    return get_settings(conn)


# --------------------------------------------------------------------------- задачи: CRUD

def create_task(conn: sqlite3.Connection, title: Any, note: Any, answers: Dict[str, Any],
                evaluation: Dict[str, Any], now: Any) -> Dict[str, Any]:
    """Новая задача. answers — нормализованные ответы, evaluation — результат engine.evaluate.

    group_since = evaluated_at = created_at = updated_at = now; journal 'create' ('' -> group).
    """
    data = _normalize_answers(answers)
    score, group, due_date = _parse_evaluation(evaluation)
    ts = _ts(now)
    with conn:
        cur = conn.execute(
            'INSERT INTO tasks (title, note, importance, due, due_date, consequence, who, '
            'delegate_to, duration, depends_on, score, grp, group_manual, group_since, '
            'evaluated_at, needs_review, review_reason, is_today, today_date, carry_count, '
            'status, done_at, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, 0, NULL, 0, ?, NULL, ?, ?)',
            (
                _s(title), _s(note), data['importance'], data['due'], due_date,
                data['consequence'], data['who'], data['delegate_to'], data['duration'],
                data['depends_on'], score, group, ts, ts, '', 'open', ts, ts,
            ),
        )
        task_id = cur.lastrowid
        _log(conn, task_id, 'create', '', group, '', ts)
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: Any) -> Optional[Dict[str, Any]]:
    """Задача любого статуса как dict (grp -> group) или None."""
    try:
        row = _fetch_row(conn, task_id)
    except NotFound:
        return None
    return _row_to_task(row)


def list_tasks(conn: sqlite3.Connection, group: Optional[str] = None, who: Optional[str] = None,
               duration: Optional[str] = None, due: Optional[str] = None, status: str = 'open',
               sort: str = 'score', needs_review: Any = None) -> List[Dict[str, Any]]:
    """Список задач с фильтрами. status: 'open' | 'done' | 'all' (= open + done; deleted не показывается).

    sort: 'score' (score ↓, due_date ↑ (None в конце), created_at ↑) |
          'due' (due_date ↑, None в конце, score ↓) | 'created' (created_at ↓). Неизвестный -> 'score'.
    """
    where = []  # type: List[str]
    params = []  # type: List[Any]
    if status == 'all':
        where.append("status IN ('open', 'done')")
    elif status == 'done':
        where.append("status = 'done'")
    else:
        where.append("status = 'open'")
    if group is not None and group != '':
        where.append('grp = ?')
        params.append(group)
    if who is not None and who != '':
        where.append('who = ?')
        params.append(who)
    if duration is not None and duration != '':
        where.append('duration = ?')
        params.append(duration)
    if due is not None and due != '':
        where.append('due = ?')
        params.append(due)
    if needs_review is not None and needs_review != '':
        flag = 1 if str(needs_review).strip().lower() in ('1', 'true', 'yes') else 0
        where.append('needs_review = ?')
        params.append(flag)
    order = _SORTS.get(sort or 'score', _SORTS['score'])
    sql = 'SELECT * FROM tasks WHERE %s ORDER BY %s' % (' AND '.join(where), order)
    return [_row_to_task(r) for r in conn.execute(sql, params).fetchall()]


def update_task_fields(conn: sqlite3.Connection, task_id: Any, fields: Dict[str, Any], now: Any,
                       event: Optional[str] = None, old: Any = '', new: Any = '',
                       note: Any = '') -> Dict[str, Any]:
    """Низкоуровневое: UPDATE указанных колонок (+updated_at) и, если задан event, строка журнала."""
    row = _fetch_row(conn, task_id)
    with conn:
        _update(conn, row['id'], dict(fields or {}), now)
        if event:
            _log(conn, row['id'], event, old, new, note, now)
    return get_task(conn, row['id'])


def edit_task(conn: sqlite3.Connection, task_id: Any, title: Optional[str] = None,
              note: Optional[str] = None, now: Any = None) -> Dict[str, Any]:
    """Изменение названия/заметки; journal 'edit' (старый title -> новый, note = что менялось).

    Параметр now обязателен по смыслу (в §6 записан после параметров со значениями по умолчанию,
    что в Python невозможно) — поэтому формально ``now=None`` означает «текущее время».
    """
    row = _fetch_row(conn, task_id)
    fields = {}  # type: Dict[str, Any]
    changed = []  # type: List[str]
    if title is not None and _s(title) != row['title']:
        fields['title'] = _s(title)
        changed.append('title')
    if note is not None and _s(note) != row['note']:
        fields['note'] = _s(note)
        changed.append('note')
    if not fields:
        return _row_to_task(row)
    ts = _ts(now)
    with conn:
        _update(conn, row['id'], fields, ts)
        _log(conn, row['id'], 'edit', row['title'], fields.get('title', row['title']), ','.join(changed), ts)
    return get_task(conn, row['id'])


def apply_evaluation(conn: sqlite3.Connection, task_id: Any, answers: Dict[str, Any],
                     evaluation: Dict[str, Any], now: Any) -> Dict[str, Any]:
    """Повторный мастер: все поля ответов, due_date, score, group; group_manual=0, carry_count=0,
    needs_review=0, review_reason='', evaluated_at=now; group_since=now только при смене группы.
    Если задача была в «Сегодня», а новая группа != 'now' — снять с дня (today_remove, note 'evaluated').
    journal 'evaluate': "now/72.5" -> "priority/51.0".
    """
    row = _fetch_row(conn, task_id)
    data = _normalize_answers(answers)
    score, group, due_date = _parse_evaluation(evaluation)
    ts = _ts(now)
    fields = dict(data)
    fields.update({
        'due_date': due_date,
        'score': score,
        'grp': group,
        'group_manual': 0,
        'carry_count': 0,
        'needs_review': 0,
        'review_reason': '',
        'evaluated_at': ts,
    })
    if group != row['grp']:
        fields['group_since'] = ts
    with conn:
        if row['is_today'] and group != 'now':
            fields['is_today'] = 0
            fields['today_date'] = None
            _log(conn, row['id'], 'today_remove', '', row['today_date'] or '', 'evaluated', ts)
        _update(conn, row['id'], fields, ts)
        _log(conn, row['id'], 'evaluate',
             '%s/%s' % (row['grp'], _fmt_score(row['score'])),
             '%s/%s' % (group, _fmt_score(score)), '', ts)
    return get_task(conn, row['id'])


def move_task(conn: sqlite3.Connection, task_id: Any, group: str, now: Any) -> Dict[str, Any]:
    """Ручной перенос: group_manual=1, group_since=now (при смене группы); если новая группа != 'now'
    и задача в «Сегодня» — снять с дня (today_remove, note 'moved'); journal 'manual_move'.
    Неизвестная группа -> ValueError('group').
    """
    if group not in GROUPS:
        raise ValueError('group')
    row = _fetch_row(conn, task_id)
    ts = _ts(now)
    fields = {'grp': group, 'group_manual': 1}  # type: Dict[str, Any]
    if group != row['grp']:
        fields['group_since'] = ts
    with conn:
        if row['is_today'] and group != 'now':
            fields['is_today'] = 0
            fields['today_date'] = None
            _log(conn, row['id'], 'today_remove', '', row['today_date'] or '', 'moved', ts)
        _update(conn, row['id'], fields, ts)
        _log(conn, row['id'], 'manual_move', row['grp'], group, '', ts)
    return get_task(conn, row['id'])


def set_done(conn: sqlite3.Connection, task_id: Any, now: Any) -> Dict[str, Any]:
    """status='done', done_at=now; is_today не трогается (галочка в «Сегодня»); journal 'done'."""
    row = _fetch_row(conn, task_id)
    ts = _ts(now)
    with conn:
        _update(conn, row['id'], {'status': 'done', 'done_at': ts}, ts)
        _log(conn, row['id'], 'done', row['status'], 'done', '', ts)
    return get_task(conn, row['id'])


def reopen_task(conn: sqlite3.Connection, task_id: Any, now: Any) -> Dict[str, Any]:
    """status='open', done_at=NULL; journal 'reopen'."""
    row = _fetch_row(conn, task_id)
    ts = _ts(now)
    with conn:
        _update(conn, row['id'], {'status': 'open', 'done_at': None}, ts)
        _log(conn, row['id'], 'reopen', row['status'], 'open', '', ts)
    return get_task(conn, row['id'])


def delete_task(conn: sqlite3.Connection, task_id: Any, now: Any) -> Dict[str, Any]:
    """Мягкое удаление: status='deleted', is_today=0, today_date=NULL; journal 'delete'."""
    row = _fetch_row(conn, task_id)
    ts = _ts(now)
    with conn:
        _update(conn, row['id'], {'status': 'deleted', 'is_today': 0, 'today_date': None}, ts)
        _log(conn, row['id'], 'delete', row['status'], 'deleted', '', ts)
    return get_task(conn, row['id'])


# --------------------------------------------------------------------------- «Сегодня»

def today_tasks(conn: sqlite3.Connection, today: Any) -> List[Dict[str, Any]]:
    """Задачи дня: is_today=1, today_date == today, status in (open, done); open раньше done, затем score ↓."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE is_today = 1 AND today_date = ? AND status IN ('open', 'done') "
        "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, score DESC, id ASC",
        (_dstr(today),),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def _remove_today_tx(conn: sqlite3.Connection, row: sqlite3.Row, now: Any, note: Any) -> bool:
    """Снять задачу с дня внутри открытой транзакции. True, если задача была в «Сегодня»."""
    if not row['is_today']:
        return False
    _update(conn, row['id'], {'is_today': 0, 'today_date': None}, now)
    _log(conn, row['id'], 'today_remove', '', row['today_date'] or '', note, now)
    return True


def add_today(conn: sqlite3.Connection, task_id: Any, today: Any, now: Any,
              replace_id: Any = None, note: Any = '') -> Dict[str, Any]:
    """Добавить в «Сегодня» (лимит не проверяется — это делает сервер).

    Если задан replace_id — сначала снять его с дня (today_remove, note 'replace'),
    затем is_today=1, today_date=today; journal today_add ('' -> дата, note).
    """
    row = _fetch_row(conn, task_id)
    day = _dstr(today)
    ts = _ts(now)
    with conn:
        if replace_id is not None and int(replace_id) != row['id']:
            _remove_today_tx(conn, _fetch_row(conn, replace_id), ts, 'replace')
        _update(conn, row['id'], {'is_today': 1, 'today_date': day}, ts)
        _log(conn, row['id'], 'today_add', '', day or '', note, ts)
    return get_task(conn, row['id'])


def remove_today(conn: sqlite3.Connection, task_id: Any, now: Any, note: Any = '') -> Dict[str, Any]:
    """Снять с дня: is_today=0, today_date=NULL; journal today_remove ('' -> дата, note).
    Если задача и так не в «Сегодня» — ничего не меняется."""
    row = _fetch_row(conn, task_id)
    with conn:
        _remove_today_tx(conn, row, _ts(now), note)
    return get_task(conn, row['id'])


# --------------------------------------------------------------------------- перенос дня (rollover)

def _plan_int_keys(mapping: Any) -> Dict[int, Any]:
    out = {}  # type: Dict[int, Any]
    if not isinstance(mapping, dict):
        return out
    for key, value in mapping.items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def apply_rollover(conn: sqlite3.Connection, plan: Dict[str, Any], today: Any, now: Any) -> Dict[str, int]:
    """Применяет план engine.plan_rollover одной транзакцией:

    carry   -> is_today=0, today_date=NULL, carry_count+1; journal 'carry' (N -> N+1)
    review  -> needs_review=1, review_reason; journal 'review_required' ('' -> reason) — только если needs_review было 0
    regroup -> grp=new, group_since=now; journal 'group_change' (old -> new, note 'auto')
    rescore -> score (без журнала)
    затем settings.last_rollover = today. Ключи id могут быть int или str.
    Возвращает {'carried', 'review', 'regrouped', 'rescored'}.
    """
    plan = plan or {}
    ts = _ts(now)
    day = _dstr(today)
    counters = {'carried': 0, 'review': 0, 'regrouped': 0, 'rescored': 0}

    carry_ids = []  # type: List[int]
    for raw in plan.get('carry') or []:
        try:
            carry_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    review = _plan_int_keys(plan.get('review'))
    regroup = _plan_int_keys(plan.get('regroup'))
    rescore = _plan_int_keys(plan.get('rescore'))

    with conn:
        for tid in carry_ids:
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
            if row is None:
                continue
            old_count = int(row['carry_count'] or 0)
            _update(conn, tid, {'is_today': 0, 'today_date': None, 'carry_count': old_count + 1}, ts)
            _log(conn, tid, 'carry', str(old_count), str(old_count + 1), '', ts)
            counters['carried'] += 1

        for tid, reason in review.items():
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
            if row is None or row['needs_review']:
                continue
            reason = _s(reason)
            _update(conn, tid, {'needs_review': 1, 'review_reason': reason}, ts)
            _log(conn, tid, 'review_required', '', reason, '', ts)
            counters['review'] += 1

        for tid, change in regroup.items():
            if isinstance(change, (list, tuple)):
                new_group = change[-1] if change else None
            else:
                new_group = change
            if new_group not in GROUPS:
                continue
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
            if row is None or row['grp'] == new_group:
                continue
            _update(conn, tid, {'grp': new_group, 'group_since': ts}, ts)
            _log(conn, tid, 'group_change', row['grp'], new_group, 'auto', ts)
            counters['regrouped'] += 1

        for tid, new_score in rescore.items():
            try:
                new_score = float(new_score)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(new_score):
                continue
            row = conn.execute('SELECT id, score FROM tasks WHERE id = ?', (tid,)).fetchone()
            if row is None:
                continue
            if float(row['score']) != new_score:
                _update(conn, tid, {'score': new_score}, ts)
            counters['rescored'] += 1

        _set_setting(conn, 'last_rollover', day)
    return counters


# --------------------------------------------------------------------------- журнал и сводки

def journal(conn: sqlite3.Connection, task_id: Any = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Последние записи журнала (новые сверху): все колонки + title задачи (JOIN)."""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 200
    if lim <= 0:
        lim = -1  # SQLite: отрицательный LIMIT = без ограничения
    params = []  # type: List[Any]
    where = ''
    if task_id is not None:
        where = ' WHERE j.task_id = ?'
        params.append(int(task_id))
    params.append(lim)
    rows = conn.execute(
        'SELECT j.id, j.task_id, j.ts, j.event, j.old_value, j.new_value, j.note, t.title AS title '
        'FROM journal j LEFT JOIN tasks t ON t.id = j.task_id' + where +
        ' ORDER BY j.id DESC LIMIT ?',
        params,
    ).fetchall()
    return [dict(zip(r.keys(), tuple(r))) for r in rows]


def count_by_group(conn: sqlite3.Connection) -> Dict[str, int]:
    """Количество открытых задач по каждой из 6 групп (нули для пустых)."""
    result = dict((g, 0) for g in GROUPS)
    for row in conn.execute("SELECT grp, COUNT(*) AS n FROM tasks WHERE status = 'open' GROUP BY grp"):
        if row['grp'] in result:
            result[row['grp']] = int(row['n'])
    return result
