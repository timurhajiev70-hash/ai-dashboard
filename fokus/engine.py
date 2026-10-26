# -*- coding: utf-8 -*-
"""Fokus — движок приоритета (блок B).

Чистые функции без БД: ответы мастера → балл 0..100 → группа; подсказки;
выбор задач дня (топ-3); план переноса и переоценки при наступлении нового дня.
Единственный источник правды по правилам — AGENTS.md §5.
Совместимо с Python 3.9 (только стандартная библиотека).
"""
import copy
import math
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    'DEFAULT_SETTINGS', 'GROUPS', 'DUE_VALUES', 'CONSEQUENCE_VALUES', 'WHO_VALUES',
    'DURATION_VALUES', 'REVIEW_REASONS', 'validate_answers', 'due_date_for', 'days_left',
    'urgency_points', 'importance_points', 'consequence_points', 'duration_points',
    'compute_score', 'assign_group', 'hints_for', 'evaluate', 'rescore',
    'suggest_today', 'can_add_today', 'plan_rollover', 'merge_settings',
    'parse_date', 'parse_datetime',
]

DEFAULT_SETTINGS = {
    'weights': {'importance': 0.45, 'urgency': 0.30, 'consequence': 0.20, 'duration': 0.05},
    'thresholds': {'now': 65, 'priority': 45, 'queue': 25},
    'day_limit': 3,
    'stale_days': 7,
    'carry_limit': 3,
    'language': 'ru',
    'last_rollover': None,
}

GROUPS = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
DUE_VALUES = ('today', 'tomorrow', 'week', 'month', 'none')
CONSEQUENCE_VALUES = ('none', 'unpleasant', 'serious')
WHO_VALUES = ('me', 'delegate')
DURATION_VALUES = ('5m', '30m', '2h', 'half_day')
REVIEW_REASONS = ('carried', 'stale')

_DUE_OFFSET = {'today': 0, 'tomorrow': 1, 'week': 7, 'month': 30}
_CONSEQUENCE_POINTS = {'none': 0.0, 'unpleasant': 5.0, 'serious': 10.0}
_DURATION_POINTS = {'5m': 10.0, '30m': 7.0, '2h': 4.0, 'half_day': 0.0}

DateLike = Union[str, date, None]


# ---------------------------------------------------------------- вспомогательные

def parse_date(value: DateLike) -> Optional[date]:
    """'YYYY-MM-DD' | date | datetime | None → date | None. Пустая строка → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    return date.fromisoformat(s[:10])


def parse_datetime(value: Union[str, datetime, date, None]) -> Optional[datetime]:
    """ISO-строка без зоны | datetime | date → datetime (date → полночь)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.combine(date.fromisoformat(s[:10]), datetime.min.time())


def merge_settings(settings: Optional[dict]) -> dict:
    """Дополняет частичные настройки значениями по умолчанию (вложенно для weights/thresholds)."""
    result = copy.deepcopy(DEFAULT_SETTINGS)
    if not settings:
        return result
    for key, value in settings.items():
        if key in ('weights', 'thresholds') and isinstance(value, dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def _round_half_up(x: float, digits: int = 1) -> float:
    factor = 10 ** digits
    return math.floor(x * factor + 0.5 + 1e-9) / factor


# ---------------------------------------------------------------- валидация

def validate_answers(answers: dict) -> dict:
    """Нормализует ответы мастера. ValueError('<поле>') при ошибке.

    Возвращает dict с ключами importance (int), due, consequence, who, delegate_to,
    duration, depends_on (строки; delegate_to/depends_on могут быть '').
    """
    if not isinstance(answers, dict):
        raise ValueError('answers')

    raw_imp = answers.get('importance')
    if isinstance(raw_imp, bool) or raw_imp is None:
        raise ValueError('importance')
    try:
        if isinstance(raw_imp, str):
            raw_imp = raw_imp.strip()
        if isinstance(raw_imp, float) and not math.isfinite(raw_imp):
            raise ValueError('importance')
        importance = int(raw_imp)
        if isinstance(raw_imp, float) and raw_imp != importance:
            raise ValueError('importance')
    except (TypeError, ValueError, OverflowError):
        raise ValueError('importance')
    if importance < 1 or importance > 5:
        raise ValueError('importance')

    def pick(field: str, allowed: Tuple[str, ...]) -> str:
        value = answers.get(field)
        if not isinstance(value, str):
            raise ValueError(field)
        value = value.strip()
        if value not in allowed:
            raise ValueError(field)
        return value

    due = pick('due', DUE_VALUES)
    consequence = pick('consequence', CONSEQUENCE_VALUES)
    who = pick('who', WHO_VALUES)
    duration = pick('duration', DURATION_VALUES)

    def text(field: str, limit: int = 500) -> str:
        value = answers.get(field)
        if value is None:
            return ''
        if not isinstance(value, str):
            raise ValueError(field)
        value = value.strip()
        if len(value) > limit:
            raise ValueError(field)
        return value

    delegate_to = text('delegate_to', 200)
    depends_on = text('depends_on', 500)
    if who != 'delegate':
        delegate_to = ''

    return {
        'importance': importance,
        'due': due,
        'consequence': consequence,
        'who': who,
        'delegate_to': delegate_to,
        'duration': duration,
        'depends_on': depends_on,
    }


# ---------------------------------------------------------------- баллы

def due_date_for(due: str, today: date) -> Optional[date]:
    """Код срока → дата. today→today, tomorrow→+1, week→+7, month→+30, none→None."""
    if due not in DUE_VALUES:
        raise ValueError('due')
    if due == 'none':
        return None
    return today + timedelta(days=_DUE_OFFSET[due])


def days_left(due_date: DateLike, today: date) -> Optional[int]:
    d = parse_date(due_date)
    if d is None:
        return None
    return (d - today).days


def urgency_points(due_date: DateLike, today: date) -> float:
    """Нет даты→0; ≤0 дней→10; 1→8; 2..7→6; 8..30→3; >30→0."""
    left = days_left(due_date, today)
    if left is None:
        return 0.0
    if left <= 0:
        return 10.0
    if left == 1:
        return 8.0
    if left <= 7:
        return 6.0
    if left <= 30:
        return 3.0
    return 0.0


def importance_points(importance: int) -> float:
    importance = int(importance)
    if importance < 1 or importance > 5:
        raise ValueError('importance')
    return (importance - 1) * 2.5


def consequence_points(consequence: str) -> float:
    if consequence not in _CONSEQUENCE_POINTS:
        raise ValueError('consequence')
    return _CONSEQUENCE_POINTS[consequence]


def duration_points(duration: str) -> float:
    if duration not in _DURATION_POINTS:
        raise ValueError('duration')
    return _DURATION_POINTS[duration]


def _resolve_due_date(answers: dict, today: date) -> Optional[date]:
    """due_date из answers (если задан), иначе по коду due."""
    if 'due_date' in answers and answers.get('due_date') not in (None, ''):
        return parse_date(answers['due_date'])
    return due_date_for(answers['due'], today)


def compute_score(answers: dict, today: date, settings: Optional[dict] = None) -> float:
    """Балл 0..100 с округлением half-up до одного знака. Веса нормализуются по сумме."""
    cfg = merge_settings(settings)
    weights = cfg['weights']
    total = 0.0
    for key in ('importance', 'urgency', 'consequence', 'duration'):
        w = float(weights.get(key, 0.0))
        if w < 0:
            raise ValueError('weights')
        total += w
    if total <= 0:
        raise ValueError('weights')

    due_date = _resolve_due_date(answers, today)
    points = {
        'importance': importance_points(answers['importance']),
        'urgency': urgency_points(due_date, today),
        'consequence': consequence_points(answers['consequence']),
        'duration': duration_points(answers['duration']),
    }
    raw = 0.0
    for key, pts in points.items():
        raw += (float(weights.get(key, 0.0)) / total) * pts * 10.0
    score = _round_half_up(raw, 1)
    return max(0.0, min(100.0, score))


# ---------------------------------------------------------------- группа и подсказки

def assign_group(answers: dict, score: float, settings: Optional[dict] = None) -> str:
    """Порядок правил: delegate → waiting → пороги балла."""
    cfg = merge_settings(settings)
    if answers.get('who') == 'delegate':
        return 'delegate'
    depends_on = answers.get('depends_on')
    if isinstance(depends_on, str) and depends_on.strip():
        return 'waiting'
    th = cfg['thresholds']
    if score >= float(th['now']):
        return 'now'
    if score >= float(th['priority']):
        return 'priority'
    if score >= float(th['queue']):
        return 'queue'
    return 'someday'


def hints_for(answers: dict, group: str) -> List[str]:
    hints = []
    try:
        imp = int(answers.get('importance', 0))
    except (TypeError, ValueError):
        imp = 0
    if answers.get('duration') == '5m' and imp >= 3:
        hints.append('do_now')
    if answers.get('who') == 'delegate':
        hints.append('delegate')
    depends_on = answers.get('depends_on')
    if isinstance(depends_on, str) and depends_on.strip():
        hints.append('waiting')
    if group == 'someday':
        hints.append('someday_review')
    return hints


def evaluate(answers: dict, today: date, settings: Optional[dict] = None) -> dict:
    """Полный проход мастера: нормализация → due_date → балл → группа → подсказки."""
    normalized = validate_answers(answers)
    if isinstance(answers, dict) and answers.get('due_date') not in (None, ''):
        due_date = parse_date(answers['due_date'])
    else:
        due_date = due_date_for(normalized['due'], today)
    scoring = dict(normalized)
    scoring['due_date'] = due_date
    score = compute_score(scoring, today, settings)
    group = assign_group(normalized, score, settings)
    return {
        'score': score,
        'group': group,
        'due_date': due_date.isoformat() if due_date else None,
        'hints': hints_for(normalized, group),
        'answers': normalized,
    }


def _task_answers(task: dict) -> dict:
    return {
        'importance': task['importance'],
        'due': task['due'],
        'due_date': task.get('due_date'),
        'consequence': task['consequence'],
        'who': task['who'],
        'delegate_to': task.get('delegate_to') or '',
        'duration': task['duration'],
        'depends_on': task.get('depends_on') or '',
    }


def rescore(task: dict, today: date, settings: Optional[dict] = None) -> dict:
    """Пересчёт существующей задачи по сохранённым ответам и due_date → {'score','group'}.
    Учитывать group_manual должен вызывающий код."""
    answers = _task_answers(task)
    score = compute_score(answers, today, settings)
    return {'score': score, 'group': assign_group(answers, score, settings)}


# ---------------------------------------------------------------- задачи дня

def _candidate_key(task: dict):
    due = parse_date(task.get('due_date'))
    created = task.get('created_at') or ''
    return (
        -float(task.get('score') or 0.0),
        due is None,
        due.toordinal() if due else 0,
        str(created),
        int(task.get('id') or 0),
    )


def suggest_today(candidates: List[dict], today_tasks: List[dict], limit: int) -> List[dict]:
    """Первые (limit − len(today_tasks)) кандидатов по score↓, due_date↑ (None в конце), created_at↑."""
    free = int(limit) - len(today_tasks)
    if free <= 0:
        return []
    today_ids = set(t.get('id') for t in today_tasks)
    pool = [t for t in candidates if t.get('id') not in today_ids]
    pool.sort(key=_candidate_key)
    return pool[:free]


def can_add_today(today_tasks: List[dict], limit: int) -> bool:
    return len(today_tasks) < int(limit)


# ---------------------------------------------------------------- новый день

def plan_rollover(tasks: List[dict], today: date, settings: Optional[dict] = None) -> dict:
    """План действий при наступлении нового дня (чистая функция, см. AGENTS.md §5.6).

    {'carry': [id], 'review': {id: reason}, 'regroup': {id: (old, new)}, 'rescore': {id: score},
     'clear': [id]}   # clear — выполненные вчера задачи дня: снять is_today без переноса
    """
    cfg = merge_settings(settings)
    carry_limit = int(cfg['carry_limit'])
    stale_days = int(cfg['stale_days'])
    plan = {'carry': [], 'review': {}, 'regroup': {}, 'rescore': {}, 'clear': []}

    for task in tasks:
        status = task.get('status', 'open')
        if status == 'done':
            done_day = parse_date(task.get('today_date'))
            if int(task.get('is_today') or 0) == 1 and done_day is not None and done_day < today:
                plan['clear'].append(task['id'])
            continue
        if status != 'open':
            continue
        tid = task['id']
        already_review = bool(task.get('needs_review'))

        # 1. перенос невыполненных задач дня
        carry_count = int(task.get('carry_count') or 0)
        today_date = parse_date(task.get('today_date'))
        if int(task.get('is_today') or 0) == 1 and today_date is not None and today_date < today:
            plan['carry'].append(tid)
            carry_count += 1
            if carry_count >= carry_limit and not already_review:
                plan['review'][tid] = 'carried'

        # 2. залежавшиеся задачи
        if tid not in plan['review'] and not already_review:
            evaluated = parse_datetime(task.get('evaluated_at')) or parse_datetime(task.get('created_at'))
            if evaluated is not None and (today - evaluated.date()).days >= stale_days:
                plan['review'][tid] = 'stale'

        # 3. пересчёт балла (срочность зависит от даты)
        result = rescore(task, today, cfg)
        plan['rescore'][tid] = result['score']

        # 4. авто-смена группы — только если не вручную и не delegate/waiting
        old_group = task.get('group')
        if not int(task.get('group_manual') or 0) and old_group not in ('delegate', 'waiting'):
            if result['group'] != old_group:
                plan['regroup'][tid] = (old_group, result['group'])

    return plan
