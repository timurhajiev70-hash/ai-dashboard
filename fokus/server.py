#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fokus — HTTP-сервер и JSON API (блок C).

Только стандартная библиотека (``http.server``, ``json``, ``urllib``), синтаксис Python 3.9.
Единственный источник правды по маршрутам и правилам — AGENTS.md §7.

Устройство:
- ``today()`` — «сегодня» сервера: ``FOKUS_TODAY=YYYY-MM-DD`` (читается при каждом вызове) или локальная дата;
- ``App`` — вся логика API без привязки к сокетам: ``App.handle(method, path, headers, body) -> (status, headers, body)``;
  таблица маршрутов ``ROUTES`` (метод, regex → имя обработчика ``h_<имя>``);
- ``make_app(conn, today_fn)`` / ``make_server(app, host, port)`` — сборка для запуска и для тестов;
- один глобальный ``threading.Lock`` на все операции с БД (включая авто-rollover) — операции атомарны;
- ``main()`` — бэкап → соединение → сервер; корректное завершение по Ctrl-C.

Переменные окружения: ``FOKUS_PORT`` (4321), ``FOKUS_DB`` (``<папка server.py>/data/fokus.db``), ``FOKUS_TODAY``.
"""
import json
import sqlite3
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402
import engine  # noqa: E402
import i18n  # noqa: E402

__all__ = [
    'VERSION', 'HOST', 'DEFAULT_PORT', 'MAX_BODY', 'ApiError', 'App', 'Request',
    'today', 'enrich', 'make_app', 'make_handler', 'make_server', 'main',
]

VERSION = '1.0'
HOST = '127.0.0.1'
DEFAULT_PORT = 4321
MAX_BODY = 64 * 1024          # §7: тело запроса ≤ 64 КБ, иначе 413
DISCARD_LIMIT = 16 * 1024 * 1024  # сверх этого слишком большое тело не вычитываем, а рвём соединение
TITLE_MAX = 200
NOTE_MAX = 2000
JOURNAL_DEFAULT_LIMIT = 200
JOURNAL_MAX_LIMIT = 1000
STATUS_FILTERS = ('open', 'done', 'all')
SORTS = ('score', 'due', 'created')
FLAG_VALUES = ('1', '0', 'true', 'false')

JSON_HEADERS = [
    ('Content-Type', 'application/json; charset=utf-8'),
    ('Cache-Control', 'no-store'),
    ('X-Content-Type-Options', 'nosniff'),
]
HTML_HEADERS = [
    ('Content-Type', 'text/html; charset=utf-8'),
    ('Cache-Control', 'no-store'),
    ('X-Content-Type-Options', 'nosniff'),
]

# (метод, regex пути, имя обработчика App.h_<имя>). Порядок важен: /tasks/preview раньше /tasks/{id}.
ROUTES = [
    ('GET', r'^/api/health$', 'health'),
    ('GET', r'^/api/i18n$', 'i18n_strings'),
    ('GET', r'^/api/settings$', 'settings_get'),
    ('PUT', r'^/api/settings$', 'settings_put'),
    ('GET', r'^/api/tasks$', 'tasks_list'),
    ('POST', r'^/api/tasks$', 'tasks_create'),
    ('POST', r'^/api/tasks/preview$', 'tasks_preview'),
    ('GET', r'^/api/tasks/(?P<id>\d+)$', 'task_get'),
    ('PATCH', r'^/api/tasks/(?P<id>\d+)$', 'task_patch'),
    ('DELETE', r'^/api/tasks/(?P<id>\d+)$', 'task_delete'),
    ('POST', r'^/api/tasks/(?P<id>\d+)/evaluate$', 'task_evaluate'),
    ('POST', r'^/api/tasks/(?P<id>\d+)/move$', 'task_move'),
    ('POST', r'^/api/tasks/(?P<id>\d+)/done$', 'task_done'),
    ('POST', r'^/api/tasks/(?P<id>\d+)/reopen$', 'task_reopen'),
    ('GET', r'^/api/today$', 'today_get'),
    ('POST', r'^/api/today$', 'today_add'),
    ('DELETE', r'^/api/today/(?P<id>\d+)$', 'today_remove'),
    ('GET', r'^/api/board$', 'board'),
    ('GET', r'^/api/review$', 'review'),
    ('GET', r'^/api/journal$', 'journal'),
    ('POST', r'^/api/rollover$', 'rollover_post'),
]


# --------------------------------------------------------------------------- «сегодня» и время

def today() -> date:
    """Дата сервера. FOKUS_TODAY читается при каждом вызове (не кэшируется); иначе локальная дата."""
    raw = os.environ.get('FOKUS_TODAY', '').strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date.today()


def _now_for(day: date) -> datetime:
    """Отметка времени для журнала: дата сервера + локальное время (без микросекунд).

    При переопределении FOKUS_TODAY created_at/evaluated_at согласованы с «сегодня» сервера,
    иначе проверка «залежалась» (stale_days) считала бы от реальной даты.
    """
    return datetime.combine(day, datetime.now().time()).replace(microsecond=0)


# --------------------------------------------------------------------------- ошибки и ответы

class ApiError(Exception):
    """Ошибка API: статус + код + текст (+ дополнительные поля ответа, напр. field/limit/tasks)."""

    def __init__(self, status: int, error: str, message: Optional[str] = None, **extra: Any) -> None:
        Exception.__init__(self, message or error)
        self.status = status
        self.error = error
        self.message = message or error
        self.extra = extra

    def payload(self) -> Dict[str, Any]:
        out = {'error': self.error, 'message': self.message}  # type: Dict[str, Any]
        out.update(self.extra)
        return out


def _validation(field: str, message: Optional[str] = None) -> ApiError:
    return ApiError(400, 'validation', message or ('неверное значение поля %s' % field), field=field)


def _not_found(message: str = 'не найдено') -> ApiError:
    return ApiError(404, 'not_found', message)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')


# --------------------------------------------------------------------------- валидация входа

def _reject_constant(name: str) -> Any:
    raise ValueError('JSON: %s недопустимо' % name)


def _as_task_id(value: Any, field: str) -> int:
    """Целый id из JSON (int, целый float или строка цифр); иначе 400 validation."""
    if isinstance(value, bool):
        raise _validation(field)
    result = None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    if result is None or not 0 < result < 2 ** 63:
        raise _validation(field)
    return result


_ZERO_WIDTH = re.compile(u'[\u200b\u200c\u200d\u2060\ufeff]')


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise _validation('title')
    title = _ZERO_WIDTH.sub('', value).strip()
    if not any(ch.isprintable() and not ch.isspace() for ch in title):
        raise _validation('title', 'название не может быть пустым')
    if not 1 <= len(title) <= TITLE_MAX:
        raise _validation('title', 'название: 1..%d символов' % TITLE_MAX)
    return title


def _clean_note(value: Any) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise _validation('note')
    if len(value) > NOTE_MAX:
        raise _validation('note', 'заметка: не больше %d символов' % NOTE_MAX)
    return value


def _clean_answers(value: Any) -> Dict[str, Any]:
    """Ответы мастера без клиентского due_date: срок всегда считается от «сегодня» (§5.1)."""
    if not isinstance(value, dict):
        raise _validation('answers')
    answers = dict(value)
    answers.pop('due_date', None)
    return answers


def _parse_day(value: Any, field: str) -> date:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise _validation(field, 'ожидается дата YYYY-MM-DD')


def _int_param(raw: Optional[str], field: str, low: int, high: int, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _validation(field)
    if not low <= value <= high:
        raise _validation(field, '%s: %d..%d' % (field, low, high))
    return value


def _task_answers(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'importance': task.get('importance'),
        'due': task.get('due'),
        'due_date': task.get('due_date'),
        'consequence': task.get('consequence'),
        'who': task.get('who'),
        'delegate_to': task.get('delegate_to') or '',
        'duration': task.get('duration'),
        'depends_on': task.get('depends_on') or '',
    }


def enrich(task: Dict[str, Any], day: date) -> Dict[str, Any]:
    """Объект Task для API: строка БД + days_left + hints (§7)."""
    out = dict(task)
    out['days_left'] = engine.days_left(task.get('due_date'), day)
    out['hints'] = engine.hints_for(_task_answers(task), task.get('group'))
    return out


# --------------------------------------------------------------------------- запрос

class Request(object):
    """Разобранный запрос: метод, путь, параметры строки запроса, тело (dict или None)."""

    def __init__(self, method: str, path: str, query: str, body: Optional[Dict[str, Any]],
                 headers: Any, day: date) -> None:
        self.method = method
        self.path = path
        self.query = parse_qs(query or '', keep_blank_values=True)
        self.body = body
        self.headers = headers
        self.day = day

    def param(self, name: str) -> Optional[str]:
        """Первое значение параметра без пробелов; пустое/отсутствующее → None."""
        values = self.query.get(name)
        if not values:
            return None
        value = values[0].strip()
        return value or None

    def choice(self, name: str, allowed: Tuple[str, ...], default: Optional[str] = None) -> Optional[str]:
        value = self.param(name)
        if value is None:
            return default
        if value not in allowed:
            raise _validation(name)
        return value

    def json_object(self) -> Dict[str, Any]:
        """Тело обязано быть JSON-объектом."""
        if not isinstance(self.body, dict):
            raise ApiError(400, 'bad_json', 'ожидается JSON-объект')
        return self.body

    def json_optional(self) -> Dict[str, Any]:
        """Тело необязательно: пустое → {}."""
        return self.body if isinstance(self.body, dict) else {}


# --------------------------------------------------------------------------- приложение

class App(object):
    """Логика API поверх db/engine. Все операции с БД — под одним Lock."""

    def __init__(self, conn: Any, today_fn: Any = None, db_path: str = '', port: Optional[int] = None,
                 static_dir: Optional[str] = None, log_stream: Any = None) -> None:
        self.conn = conn
        self.today_fn = today_fn or today
        self.db_path = db_path
        self.port = port
        self.static_dir = static_dir or os.path.join(HERE, 'static')
        self.log_stream = log_stream  # None → sys.stderr на момент записи
        self.lock = threading.Lock()
        self.routes = [(m, re.compile(rx), name) for m, rx, name in ROUTES]

    # ------------------------------------------------------------------ вход/выход

    def handle(self, method: str, raw_path: str, headers: Any,
               body_bytes: bytes) -> Tuple[int, List[Tuple[str, str]], bytes]:
        """Полная обработка запроса → (статус, заголовки, байты тела). Исключений не бросает."""
        parts = urlsplit(raw_path or '/')
        path = unquote(parts.path) or '/'
        if len(path) > 1 and path.endswith('/'):
            path = path.rstrip('/') or '/'

        if path == '/api' or path.startswith('/api/'):
            try:
                with self.lock:
                    status, payload = self.handle_api(method, path, parts.query, headers, body_bytes)
            except ApiError as e:
                status, payload = e.status, e.payload()
            except db.NotFound:
                status, payload = 404, _not_found('задача не найдена').payload()
            except Exception as e:  # noqa: BLE001 — любой сбой → 500, сервер живёт дальше
                traceback.print_exc()
                status, payload = 500, {'error': 'internal', 'message': str(e)}
            return self.json_response(status, payload)

        if method == 'GET' and path in ('/', '/index.html'):
            return self.serve_index()
        return self.json_response(404, _not_found('нет такого пути').payload())

    def json_response(self, status: int, payload: Any) -> Tuple[int, List[Tuple[str, str]], bytes]:
        return status, list(JSON_HEADERS), _json_bytes(payload)

    def error_response(self, status: int, error: str, message: Optional[str] = None,
                       **extra: Any) -> Tuple[int, List[Tuple[str, str]], bytes]:
        return self.json_response(status, ApiError(status, error, message, **extra).payload())

    def serve_index(self) -> Tuple[int, List[Tuple[str, str]], bytes]:
        """Статика — только static/index.html; никакой раздачи файлов по пути."""
        index = os.path.join(self.static_dir, 'index.html')
        try:
            with open(index, 'rb') as fh:
                data = fh.read()
        except OSError as e:
            return self.error_response(500, 'internal', 'static/index.html недоступен: %s' % e)
        return 200, list(HTML_HEADERS), data

    def log(self, method: str, path: str, status: int, elapsed: float) -> None:
        """Одна строка на запрос: время метод путь код мс."""
        stream = self.log_stream or sys.stderr
        try:
            stream.write('%s %s %s %d %dms\n' % (
                datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), method, path, status, int(elapsed * 1000)))
            stream.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ общие шаги /api/*

    def allowed_origins(self) -> Tuple[str, ...]:
        port = self.port or DEFAULT_PORT
        return ('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port)

    def origin_allowed(self, origin: str) -> bool:
        return origin.strip().rstrip('/').lower() in self.allowed_origins()

    @staticmethod
    def parse_body(body_bytes: bytes) -> Optional[Dict[str, Any]]:
        """Пустое тело → None; иначе обязан быть JSON-объект, иначе 400 bad_json."""
        if not body_bytes or not body_bytes.strip():
            return None
        try:
            data = json.loads(body_bytes.decode('utf-8'), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, 'bad_json', 'тело запроса — не JSON')
        if not isinstance(data, dict):
            raise ApiError(400, 'bad_json', 'ожидается JSON-объект')
        return data

    def handle_api(self, method: str, path: str, query: str, headers: Any,
                   body_bytes: bytes) -> Tuple[int, Any]:
        origin = headers.get('Origin') if headers is not None else None
        if origin is not None and not self.origin_allowed(origin):
            raise ApiError(403, 'forbidden_origin', 'запрос с чужого сайта отклонён')

        body = self.parse_body(body_bytes)
        day = self.today_fn()
        self.auto_rollover(day)

        req = Request(method, path, query, body, headers, day)
        path_known = False
        for route_method, regex, name in self.routes:
            match = regex.match(path)
            if not match:
                continue
            path_known = True
            if route_method == method:
                result = getattr(self, 'h_' + name)(req, match)
                if isinstance(result, tuple):
                    return result
                return 200, result
        if path_known:
            raise ApiError(405, 'method_not_allowed', 'метод %s не поддерживается для %s' % (method, path))
        raise _not_found('нет такого маршрута')

    # ------------------------------------------------------------------ rollover

    def run_rollover(self, day: date) -> Dict[str, Any]:
        """Перенос дня на дату day: план по всем открытым и выполненным задачам → применение → счётчики.
        Выполненные нужны, чтобы снять с дня вчерашние сделанные (plan['clear'])."""
        settings = db.get_settings(self.conn)
        tasks = db.list_tasks(self.conn, status='all')
        plan = engine.plan_rollover(tasks, day, settings)
        counters = dict(db.apply_rollover(self.conn, plan, day, _now_for(day)))
        counters['today'] = day.isoformat()
        return counters

    def auto_rollover(self, day: date) -> Optional[Dict[str, Any]]:
        """Один раз в день: если last_rollover пуст или раньше сегодня — выполнить перенос."""
        settings = db.get_settings(self.conn)
        last = settings.get('last_rollover')
        last_day = None
        if isinstance(last, str) and last.strip():
            try:
                last_day = date.fromisoformat(last.strip())
            except ValueError:
                last_day = None
        if last_day is None or last_day < day:
            return self.run_rollover(day)
        return None

    # ------------------------------------------------------------------ вспомогательные

    def _settings(self) -> Dict[str, Any]:
        return db.get_settings(self.conn)

    def _limit(self, settings: Dict[str, Any]) -> int:
        try:
            return int(settings.get('day_limit') or engine.DEFAULT_SETTINGS['day_limit'])
        except (TypeError, ValueError):
            return int(engine.DEFAULT_SETTINGS['day_limit'])

    def _task_or_404(self, task_id: Any) -> Dict[str, Any]:
        try:
            task = db.get_task(self.conn, task_id)
        except (OverflowError, sqlite3.Error):
            task = None
        if task is None:
            raise _not_found('задача не найдена')
        return task

    def _live_task_or_404(self, task_id: Any) -> Dict[str, Any]:
        """Для изменяющих операций: удалённая задача = не найдена (§9 п.12)."""
        task = self._task_or_404(task_id)
        if task.get('status') == 'deleted':
            raise _not_found('задача удалена')
        return task

    def _tasks(self, tasks: List[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
        return [enrich(t, day) for t in tasks]

    def _today_payload(self, day: date, settings: Dict[str, Any]) -> Dict[str, Any]:
        tasks = db.today_tasks(self.conn, day)
        limit = self._limit(settings)
        return {'tasks': self._tasks(tasks, day), 'free_slots': max(0, limit - len(tasks))}

    def _evaluate(self, raw_answers: Any, day: date, settings: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return engine.evaluate(_clean_answers(raw_answers), day, settings)
        except ValueError as e:
            raise _validation(str(e) or 'answers')

    # ------------------------------------------------------------------ обработчики

    def h_health(self, req: Request, m: Any) -> Dict[str, Any]:
        return {'ok': True, 'version': VERSION, 'today': req.day.isoformat(), 'db': self.db_path}

    def h_i18n_strings(self, req: Request, m: Any) -> Dict[str, Any]:
        lang = req.param('lang')
        if lang is None:
            lang = str(self._settings().get('language') or 'ru')
        if lang not in i18n.LANGUAGES:
            lang = 'ru'
        return {'lang': lang, 'strings': i18n.get_strings(lang)}

    def h_settings_get(self, req: Request, m: Any) -> Dict[str, Any]:
        return self._settings()

    def h_settings_put(self, req: Request, m: Any) -> Dict[str, Any]:
        patch = dict(req.json_object())
        patch.pop('last_rollover', None)  # служебное поле, извне не меняется
        try:
            return db.update_settings(self.conn, patch)
        except ValueError as e:
            raise _validation(str(e) or 'settings')

    def h_tasks_list(self, req: Request, m: Any) -> Dict[str, Any]:
        group = req.choice('group', engine.GROUPS)
        who = req.choice('who', engine.WHO_VALUES)
        duration = req.choice('duration', engine.DURATION_VALUES)
        due = req.choice('due', engine.DUE_VALUES)
        status = req.choice('status', STATUS_FILTERS, 'open')
        sort = req.choice('sort', SORTS, 'score')
        needs_review = req.choice('needs_review', FLAG_VALUES)
        tasks = db.list_tasks(self.conn, group=group, who=who, duration=duration, due=due,
                              status=status, sort=sort, needs_review=needs_review)
        return {'tasks': self._tasks(tasks, req.day)}

    def h_tasks_create(self, req: Request, m: Any) -> Tuple[int, Dict[str, Any]]:
        body = req.json_object()
        title = _clean_title(body.get('title'))
        note = _clean_note(body.get('note'))
        settings = self._settings()
        evaluation = self._evaluate(body.get('answers'), req.day, settings)
        task = db.create_task(self.conn, title, note, evaluation['answers'], evaluation, _now_for(req.day))
        return 201, {'task': enrich(task, req.day), 'hints': evaluation['hints']}

    def h_tasks_preview(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        evaluation = self._evaluate(body.get('answers'), req.day, self._settings())
        return {
            'score': evaluation['score'],
            'group': evaluation['group'],
            'due_date': evaluation['due_date'],
            'hints': evaluation['hints'],
        }

    def h_task_get(self, req: Request, m: Any) -> Dict[str, Any]:
        task = self._task_or_404(int(m.group('id')))
        return {'task': enrich(task, req.day)}

    def h_task_patch(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        title = _clean_title(body['title']) if body.get('title') is not None else None
        note = _clean_note(body['note']) if body.get('note') is not None else None
        task = db.edit_task(self.conn, task_id, title=title, note=note, now=_now_for(req.day))
        return {'task': enrich(task, req.day)}

    def h_task_delete(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        task = db.delete_task(self.conn, task_id, _now_for(req.day))
        return {'task': enrich(task, req.day)}

    def h_task_evaluate(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        evaluation = self._evaluate(body.get('answers'), req.day, self._settings())
        task = db.apply_evaluation(self.conn, task_id, evaluation['answers'], evaluation, _now_for(req.day))
        return {'task': enrich(task, req.day), 'hints': evaluation['hints']}

    def h_task_move(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        group = body.get('group')
        if not isinstance(group, str) or group.strip() not in engine.GROUPS:
            raise _validation('group', 'неизвестная группа')
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        task = db.move_task(self.conn, task_id, group.strip(), _now_for(req.day))
        return {'task': enrich(task, req.day)}

    def h_task_done(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        if task.get('status') != 'done':  # повторный клик — идемпотентно, без второй записи в журнал
            task = db.set_done(self.conn, task_id, _now_for(req.day))
        return {'task': enrich(task, req.day)}

    def h_task_reopen(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        if task.get('status') != 'open':
            task = db.reopen_task(self.conn, task_id, _now_for(req.day))
        return {'task': enrich(task, req.day)}

    def h_today_get(self, req: Request, m: Any) -> Dict[str, Any]:
        settings = self._settings()
        limit = self._limit(settings)
        tasks = db.today_tasks(self.conn, req.day)
        pool = [t for t in db.list_tasks(self.conn, group='now', status='open') if not t.get('is_today')]
        candidates = engine.suggest_today(pool, tasks, limit)
        # pool — все открытые задачи 🔴 вне дня в том же порядке, что и кандидаты
        # (нужен, когда день полный: пользователь выбирает, на что заменить одну из трёх)
        ordered_pool = engine.suggest_today(pool, [], max(len(pool), 1))
        return {
            'date': req.day.isoformat(),
            'limit': limit,
            'tasks': self._tasks(tasks, req.day),
            'candidates': self._tasks(candidates, req.day),
            'pool': self._tasks(ordered_pool, req.day),
            'free_slots': max(0, limit - len(tasks)),
        }

    def h_today_add(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        task_id = _as_task_id(body.get('task_id'), 'task_id')
        task = self._live_task_or_404(task_id)
        if task.get('status') != 'open':
            raise ApiError(400, 'not_open', 'в «Сегодня» можно добавить только открытую задачу')
        if task.get('group') != 'now':
            raise ApiError(400, 'not_now_group', 'в «Сегодня» можно добавить только задачу из группы now')

        settings = self._settings()
        limit = self._limit(settings)
        day = req.day
        tasks = db.today_tasks(self.conn, day)
        if task.get('is_today') and task.get('today_date') == day.isoformat():
            return self._today_payload(day, settings)  # §9 п.10: идемпотентно, без изменений и журнала

        replace_raw = body.get('replace_id')
        if replace_raw is not None:
            replace_id = _as_task_id(replace_raw, 'replace_id')
            if not any(t['id'] == replace_id and t.get('status') == 'open' for t in tasks):
                raise _validation('replace_id', 'заменяемая задача должна быть открытой задачей дня')
            db.add_today(self.conn, task_id, day, _now_for(day), replace_id=replace_id, note='replace')
        else:
            if not engine.can_add_today(tasks, limit):
                raise ApiError(409, 'day_full', 'на сегодня уже выбрано %d задач' % limit,
                               limit=limit, tasks=self._tasks(tasks, day))
            db.add_today(self.conn, task_id, day, _now_for(day))
        return self._today_payload(day, settings)

    def h_today_remove(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        db.remove_today(self.conn, task_id, _now_for(req.day))
        return self._today_payload(req.day, self._settings())

    def h_board(self, req: Request, m: Any) -> Dict[str, Any]:
        sort = req.choice('sort', SORTS, 'score')
        groups = {}  # type: Dict[str, Any]
        for group in engine.GROUPS:
            tasks = db.list_tasks(self.conn, group=group, status='open', sort=sort)
            groups[group] = {'count': len(tasks), 'tasks': self._tasks(tasks, req.day)}
        review = db.list_tasks(self.conn, status='open', sort=sort, needs_review=1)
        return {'groups': groups, 'review': self._tasks(review, req.day)}

    def h_review(self, req: Request, m: Any) -> Dict[str, Any]:
        sort = req.choice('sort', SORTS, 'score')
        tasks = db.list_tasks(self.conn, status='open', sort=sort, needs_review=1)
        return {'tasks': self._tasks(tasks, req.day)}

    def h_journal(self, req: Request, m: Any) -> Dict[str, Any]:
        raw_task = req.param('task_id')
        task_id = None
        if raw_task is not None:
            if not raw_task.isdigit():
                raise _validation('task_id')
            task_id = int(raw_task)
        limit = _int_param(req.param('limit'), 'limit', 1, JOURNAL_MAX_LIMIT, JOURNAL_DEFAULT_LIMIT)
        return {'entries': db.journal(self.conn, task_id=task_id, limit=limit)}

    def h_rollover_post(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_optional()
        day = req.day
        if body.get('today') is not None:
            day = _parse_day(body.get('today'), 'today')
        if day > req.day and not os.environ.get('FOKUS_TODAY', '').strip():
            # вне тестового режима перенос «в будущее» заморозил бы ежедневный авто-перенос до этой даты
            raise _validation('today', 'дата в будущем допустима только в тестовом режиме (FOKUS_TODAY)')
        last = db.get_settings(self.conn).get('last_rollover')
        if isinstance(last, str) and last.strip():
            try:
                if day < date.fromisoformat(last.strip()):
                    raise _validation('today', 'перенос назад во времени запрещён (last_rollover=%s)' % last)
            except ValueError:
                pass
        return self.run_rollover(day)


# --------------------------------------------------------------------------- HTTP-обвязка

def make_app(conn: Any, today_fn: Any = None, db_path: str = '', port: Optional[int] = None,
             static_dir: Optional[str] = None, log_stream: Any = None) -> App:
    return App(conn, today_fn=today_fn, db_path=db_path, port=port, static_dir=static_dir,
               log_stream=log_stream)


def make_handler(app: App):
    """Класс обработчика http.server, замкнутый на приложение."""

    class Handler(BaseHTTPRequestHandler):
        server_version = 'Fokus/' + VERSION
        sys_version = ''

        def log_message(self, fmt, *args):  # стандартный шум отключён — пишем свою строку в App.log
            pass

        def do_GET(self):
            self._run('GET')

        def do_POST(self):
            self._run('POST')

        def do_PUT(self):
            self._run('PUT')

        def do_PATCH(self):
            self._run('PATCH')

        def do_DELETE(self):
            self._run('DELETE')

        def _content_length(self) -> int:
            raw = self.headers.get('Content-Length')
            if raw is None or not raw.strip():
                return 0
            try:
                length = int(raw)
            except ValueError:
                return -1
            return length if length >= 0 else -1

        def _discard(self, length: int) -> None:
            """Вычитать слишком большое тело, чтобы клиент получил ответ 413, а не сброс соединения."""
            if length > DISCARD_LIMIT:
                return
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _send(self, status: int, headers: List[Tuple[str, str]], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _run(self, method: str) -> None:
            started = time.monotonic()
            status = 500
            try:
                length = self._content_length()
                if length < 0:
                    status, headers, body = app.error_response(400, 'bad_json', 'неверный Content-Length')
                    self.close_connection = True
                elif length > MAX_BODY:
                    self._discard(length)
                    status, headers, body = app.error_response(413, 'too_large',
                                                               'тело запроса больше %d байт' % MAX_BODY)
                    self.close_connection = True
                else:
                    body_bytes = self.rfile.read(length) if length else b''
                    status, headers, body = app.handle(method, self.path, self.headers, body_bytes)
                self._send(status, headers, body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # клиент ушёл — не наша ошибка
            except Exception as e:  # noqa: BLE001 — сервер не должен падать
                traceback.print_exc()
                try:
                    status, headers, body = app.error_response(500, 'internal', str(e))
                    self._send(status, headers, body)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                app.log(method, self.path, status, time.monotonic() - started)

    return Handler


def make_server(app: App, host: str = HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """ThreadingHTTPServer на 127.0.0.1; фактический порт (важно при port=0) записывается в app.port."""
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server


# --------------------------------------------------------------------------- запуск

def _log_line(text: str) -> None:
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def main(argv: Optional[List[str]] = None) -> int:
    raw_port = os.environ.get('FOKUS_PORT', '').strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
        if not 0 <= port <= 65535:
            raise ValueError(raw_port)
    except ValueError:
        _log_line('FOKUS_PORT: ожидается число 0..65535, получено %r' % raw_port)
        return 2

    raw_today = os.environ.get('FOKUS_TODAY', '').strip()
    if raw_today:
        try:
            date.fromisoformat(raw_today)
        except ValueError:
            _log_line('FOKUS_TODAY: ожидается дата YYYY-MM-DD, получено %r' % raw_today)
            return 2

    db_path = os.environ.get('FOKUS_DB', '').strip() or os.path.join(HERE, 'data', 'fokus.db')
    db_path = os.path.abspath(db_path)
    backups_dir = os.path.join(os.path.dirname(db_path), 'backups')

    try:
        backup_path = db.backup(db_path, backups_dir, keep=10)  # до открытия соединения
        _log_line('бэкап: %s' % (backup_path or 'нет (БД ещё не создана)'))
    except Exception as e:  # бэкап не должен мешать работе: предупредить и продолжить
        backup_path = None
        _log_line('ВНИМАНИЕ: бэкап не удался (%s: %s) — продолжаю без бэкапа' % (type(e).__name__, e))

    try:
        conn = db.connect(db_path)
    except (sqlite3.Error, OSError) as e:
        _log_line('ОШИБКА: база %s недоступна: %s. Проверьте права на папку data/ и свободное место.' % (db_path, e))
        return 1
    app = make_app(conn, today, db_path=db_path, port=port)
    try:
        server = make_server(app, HOST, port)
    except OSError as e:
        _log_line('не удалось открыть порт %d: %s' % (port, e))
        conn.close()
        return 1

    print('Fokus: http://127.0.0.1:%d  БД: %s  бэкап: %s' % (app.port, db_path, backup_path or 'нет'), flush=True)

    def _terminate(signum, frame):  # SIGTERM от лаунчера (kill) → то же корректное завершение, что и Ctrl-C
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        pass  # не главный поток или платформа без SIGTERM — завершение штатное, по Ctrl-C
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log_line('остановка (Ctrl-C / SIGTERM)')
    finally:
        server.server_close()
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
