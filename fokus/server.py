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
- ``main()`` — бэкап → соединение → сервер; корректное завершение по Ctrl-C, SIGTERM или ``POST /api/shutdown``.

v2 (§7, §12): проекты (``/api/projects``), «Входящие» (``POST /api/tasks`` без ``answers``, ``/api/inbox``),
``/api/upcoming``, Google Календарь из кэша (``/api/calendar``; устаревший кэш обновляется в фоновом потоке —
сеть вне Lock, запись под Lock; ``POST /api/calendar/refresh`` — синхронно через ``Deferred`` вне Lock),
``POST /api/shutdown``. Модуль ``calendar_ics`` импортируется лениво: без него календарь отвечает ошибкой, сервер работает.

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
from datetime import date, datetime, timedelta
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

VERSION = '1.0'               # §7: /api/health отдаёт version "1.0" (в v2 спецификация значение не меняла)
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
ALL_GROUPS = engine.GROUPS + (engine.INBOX,)   # v2: фильтр group= и доска включают «Входящие»
UPCOMING_DEFAULT_DAYS = 7
UPCOMING_MAX_DAYS = 31
CALENDAR_DEFAULT_SPAN = 7     # GET /api/calendar без from/to: сегодня..+7
CALENDAR_MAX_SPAN = 62        # дней между from и to
CALENDAR_FETCH_TIMEOUT = 10.0  # §7: таймаут загрузки каждого .ics
CALENDAR_RETRY_SECONDS = 60.0  # фоновое обновление — не чаще раза в минуту (защита от «дёргания» при сбоях сети)
CALENDAR_DAYS_BACK = 7
CALENDAR_DAYS_FORWARD = 60
SHUTDOWN_DELAY = 0.5          # §7: /api/shutdown — ответ, затем остановка через 0.5 с
_URL_RE = re.compile(r'https?://\S+')  # для вычищения секретных адресов из сообщений об ошибках

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
    # ---- v2
    ('GET', r'^/api/inbox$', 'inbox'),
    ('GET', r'^/api/upcoming$', 'upcoming'),
    ('GET', r'^/api/projects$', 'projects_list'),
    ('POST', r'^/api/projects$', 'projects_create'),
    ('PATCH', r'^/api/projects/(?P<pid>[^/]+)$', 'project_patch'),
    ('DELETE', r'^/api/projects/(?P<pid>[^/]+)$', 'project_delete'),
    ('GET', r'^/api/calendar$', 'calendar_get'),
    ('POST', r'^/api/calendar/refresh$', 'calendar_refresh'),
    ('POST', r'^/api/shutdown$', 'shutdown'),
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


def enrich(task: Dict[str, Any], day: date, projects: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Объект Task для API: строка БД + days_left + hints + project_name/project_color/evaluated (§7).

    projects — {slug: project} (включая архивные); без него имя = slug, цвет = умолчание.
    """
    out = dict(task)
    out['days_left'] = engine.days_left(task.get('due_date'), day)
    if task.get('group') == engine.INBOX:
        out['hints'] = []  # ответы-заглушки подсказок не дают
    else:
        out['hints'] = engine.hints_for(_task_answers(task), task.get('group'))
    slug = task.get('project') or db.DEFAULT_PROJECT
    out['project'] = slug
    project = (projects or {}).get(slug) or {}
    if not out.get('project_name'):  # db.list_tasks/get_task уже отдают имя и цвет (LEFT JOIN projects)
        out['project_name'] = project.get('name') or slug
    if not out.get('project_color'):
        out['project_color'] = project.get('color') or db.PROJECT_COLOR_DEFAULT
    out['evaluated'] = 0 if task.get('evaluated') in (0, False, '0') else 1
    return out


def _redact(text: Any) -> str:
    """Секретные адреса календаря — пароль; в сообщениях об ошибках заменяются на <url>."""
    return _URL_RE.sub('<url>', '' if text is None else str(text))


def _events_between(events: List[Dict[str, Any]], start: date, end: date) -> List[Dict[str, Any]]:
    """События кэша (Event §7: start/end — 'YYYY-MM-DD' или 'YYYY-MM-DDTHH:MM', end исключительный),
    пересекающие [start, end). Сравнение строк корректно: префикс — дата ISO. Сортировка по start."""
    lo = start.isoformat()
    hi = end.isoformat()
    out = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        s = str(ev.get('start') or '')
        e = str(ev.get('end') or s)
        if not s:
            continue
        if e <= s:  # событие нулевой длины: считаем, что занимает момент начала
            e = s + '\x7f'
        if s < hi and e > lo:
            out.append(ev)
    out.sort(key=lambda ev: (str(ev.get('start') or ''), str(ev.get('summary') or '')))
    return out


class Deferred(object):
    """Результат обработчика, который надо завершить ВНЕ глобального Lock (сеть), см. App.handle.
    fn() -> (status, payload); внутри fn сам берёт app.lock на запись результата."""

    def __init__(self, fn: Any) -> None:
        self.fn = fn


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
        # ---- v2
        self.server = None             # type: Optional[ThreadingHTTPServer]  # ставит make_server; нужен для /api/shutdown
        self.shutdown_hook = None      # type: Any  # альтернатива server.shutdown() (тесты)
        self._cal_thread = None        # type: Optional[threading.Thread]  # не больше одного фонового обновления
        self._cal_last_attempt = 0.0   # monotonic: когда последний раз начинали обновление календаря

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
                if isinstance(payload, Deferred):
                    status, payload = payload.fn()  # сеть — вне Lock (POST /api/calendar/refresh)
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

    def _projects_map(self) -> Dict[str, Dict[str, Any]]:
        """{slug: project} по всем проектам, включая архивные (для имён/цветов в Task)."""
        return dict((p['id'], p) for p in db.list_projects(self.conn, include_archived=True))

    def _tasks(self, tasks: List[Dict[str, Any]], day: date,
               projects: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        if projects is None:
            projects = self._projects_map()
        return [enrich(t, day, projects) for t in tasks]

    def _task(self, task: Dict[str, Any], day: date) -> Dict[str, Any]:
        return enrich(task, day, self._projects_map())

    def _today_payload(self, day: date, settings: Dict[str, Any]) -> Dict[str, Any]:
        tasks = db.today_tasks(self.conn, day)
        limit = self._limit(settings)
        return {'tasks': self._tasks(tasks, day), 'free_slots': max(0, limit - len(tasks))}

    def _project_param(self, req: Request) -> Optional[str]:
        """?project=slug — существующий проект (архивный допустим); иначе 400 validation project."""
        raw = req.param('project')
        if raw is None:
            return None
        if not db.PROJECT_ID_RE.match(raw) or db.get_project(self.conn, raw) is None:
            raise _validation('project', 'проект не найден')
        return raw

    def _project_or_404(self, project_id: str) -> Dict[str, Any]:
        project = db.get_project(self.conn, project_id) if db.PROJECT_ID_RE.match(project_id or '') else None
        if project is None:
            raise _not_found('проект не найден')
        return project

    @staticmethod
    def _public_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
        """Настройки для API: calendar_cache заменяется сводкой (список событий отдаёт /api/calendar)."""
        out = dict(settings)
        cache = out.get('calendar_cache')
        if isinstance(cache, dict):
            out['calendar_cache'] = {
                'fetched_at': cache.get('fetched_at'),
                'events_count': len(cache.get('events') or []),
                'errors': cache.get('errors') or [],
                'window': cache.get('window'),
            }
        else:
            out['calendar_cache'] = None
        return out

    def _evaluate(self, raw_answers: Any, day: date, settings: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return engine.evaluate(_clean_answers(raw_answers), day, settings)
        except ValueError as e:
            raise _validation(str(e) or 'answers')

    # ------------------------------------------------------------------ обработчики

    def h_health(self, req: Request, m: Any) -> Dict[str, Any]:
        return {'ok': True, 'version': VERSION, 'today': req.day.isoformat(), 'db': self.db_path, 'pid': os.getpid()}

    def h_i18n_strings(self, req: Request, m: Any) -> Dict[str, Any]:
        lang = req.param('lang')
        if lang is None:
            lang = str(self._settings().get('language') or 'ru')
        if lang not in i18n.LANGUAGES:
            lang = 'ru'
        return {'lang': lang, 'strings': i18n.get_strings(lang)}

    def h_settings_get(self, req: Request, m: Any) -> Dict[str, Any]:
        return self._public_settings(self._settings())

    def h_settings_put(self, req: Request, m: Any) -> Dict[str, Any]:
        patch = dict(req.json_object())
        for key in ('last_rollover', 'schema_version', 'calendar_cache'):
            patch.pop(key, None)  # служебные поля, извне не меняются
        before = self._settings()
        try:
            result = db.update_settings(self.conn, patch)
        except ValueError as e:
            raise _validation(str(e) or 'settings')
        if result.get('calendar_urls') != before.get('calendar_urls') or result.get('timezone') != before.get('timezone'):
            db.calendar_cache_set(self.conn, None)  # события старых адресов/зоны не показывать; следующий запрос обновит
            self._cal_last_attempt = 0.0
        return self._public_settings(result)

    def h_tasks_list(self, req: Request, m: Any) -> Dict[str, Any]:
        group = req.choice('group', ALL_GROUPS)
        who = req.choice('who', engine.WHO_VALUES)
        duration = req.choice('duration', engine.DURATION_VALUES)
        due = req.choice('due', engine.DUE_VALUES)
        status = req.choice('status', STATUS_FILTERS, 'open')
        sort = req.choice('sort', SORTS, 'score')
        needs_review = req.choice('needs_review', FLAG_VALUES)
        project = self._project_param(req)
        tasks = db.list_tasks(self.conn, group=group, who=who, duration=duration, due=due,
                              status=status, sort=sort, needs_review=needs_review, project=project)
        return {'tasks': self._tasks(tasks, req.day)}

    def _project_for_create(self, body: Dict[str, Any], raw_title: str) -> Tuple[str, Optional[str]]:
        """(название без #тега, slug проекта или None=умолчание). #Проект в тексте сильнее поля project (§7)."""
        active = db.list_projects(self.conn)  # только неархивные
        parsed = engine.parse_quick_add(raw_title, [{'id': p['id'], 'name': p['name']} for p in active])
        title = _clean_title(parsed['title'])
        project = parsed['project']  # type: Optional[str]
        if project is None and body.get('project') is not None:
            raw = body.get('project')
            if not isinstance(raw, str) or not db.PROJECT_ID_RE.match(raw.strip()):
                raise _validation('project', 'ожидается slug проекта')
            found = db.get_project(self.conn, raw.strip())
            if found is None or found.get('archived'):
                raise _validation('project', 'проект не найден или в архиве')
            project = found['id']
        return title, project

    def h_tasks_create(self, req: Request, m: Any) -> Tuple[int, Dict[str, Any]]:
        body = req.json_object()
        raw_title = _clean_title(body.get('title'))
        note = _clean_note(body.get('note'))
        title, project = self._project_for_create(body, raw_title)
        now = _now_for(req.day)
        try:
            if body.get('answers') is None:  # v2: без мастера → «Входящие»
                task = db.create_task(self.conn, title, note, None, None, now, project=project)
                hints = []  # type: List[str]
            else:
                evaluation = self._evaluate(body.get('answers'), req.day, self._settings())
                task = db.create_task(self.conn, title, note, evaluation['answers'], evaluation, now, project=project)
                hints = evaluation['hints']
        except ValueError as e:
            raise _validation(str(e) or 'task')
        return 201, {'task': self._task(task, req.day), 'hints': hints}

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
        return {'task': self._task(task, req.day)}

    def h_task_patch(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        title = _clean_title(body['title']) if body.get('title') is not None else None
        note = _clean_note(body['note']) if body.get('note') is not None else None
        now = _now_for(req.day)
        if title is not None or note is not None:
            task = db.edit_task(self.conn, task_id, title=title, note=note, now=now)
        if body.get('project') is not None:  # v2: смена проекта → журнал project_change
            raw = body.get('project')
            if not isinstance(raw, str) or not db.PROJECT_ID_RE.match(raw.strip()):
                raise _validation('project', 'ожидается slug проекта')
            try:
                task = db.set_project(self.conn, task_id, raw.strip(), now)
            except ValueError:
                raise _validation('project', 'проект не найден или в архиве')
        return {'task': self._task(task, req.day)}

    def h_task_delete(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        task = db.delete_task(self.conn, task_id, _now_for(req.day))
        return {'task': self._task(task, req.day)}

    def h_task_evaluate(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        task_id = int(m.group('id'))
        self._live_task_or_404(task_id)
        evaluation = self._evaluate(body.get('answers'), req.day, self._settings())
        task = db.apply_evaluation(self.conn, task_id, evaluation['answers'], evaluation, _now_for(req.day))
        return {'task': self._task(task, req.day), 'hints': evaluation['hints']}

    def h_task_move(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        group = body.get('group')
        if not isinstance(group, str) or group.strip() not in engine.GROUPS:
            raise _validation('group', 'неизвестная группа')
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        if task.get('group') == engine.INBOX:
            raise _validation('group', 'задачу из «Входящих» нельзя перенести вручную — сначала пройдите мастер')
        try:
            task = db.move_task(self.conn, task_id, group.strip(), _now_for(req.day))
        except ValueError:
            raise _validation('group', 'неизвестная группа')
        return {'task': self._task(task, req.day)}

    def h_task_done(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        if task.get('status') != 'done':  # повторный клик — идемпотентно, без второй записи в журнал
            task = db.set_done(self.conn, task_id, _now_for(req.day))
        return {'task': self._task(task, req.day)}

    def h_task_reopen(self, req: Request, m: Any) -> Dict[str, Any]:
        task_id = int(m.group('id'))
        task = self._live_task_or_404(task_id)
        if task.get('status') != 'open':
            task = db.reopen_task(self.conn, task_id, _now_for(req.day))
        return {'task': self._task(task, req.day)}

    def h_today_get(self, req: Request, m: Any) -> Dict[str, Any]:
        settings = self._settings()
        limit = self._limit(settings)
        tasks = db.today_tasks(self.conn, req.day)
        pool = [t for t in db.list_tasks(self.conn, group='now', status='open') if not t.get('is_today')]
        candidates = engine.suggest_today(pool, tasks, limit)
        # pool — все открытые задачи 🔴 вне дня в том же порядке, что и кандидаты
        # (нужен, когда день полный: пользователь выбирает, на что заменить одну из трёх)
        ordered_pool = engine.suggest_today(pool, [], max(len(pool), 1))
        projects = self._projects_map()
        calendar = self._calendar_view(settings, req.day, req.day + timedelta(days=1))
        return {
            'date': req.day.isoformat(),
            'limit': limit,
            'tasks': self._tasks(tasks, req.day, projects),
            'candidates': self._tasks(candidates, req.day, projects),
            'pool': self._tasks(ordered_pool, req.day, projects),
            'free_slots': max(0, limit - len(tasks)),
            'events': calendar['events'],
            'calendar': calendar['status'],
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
        project = self._project_param(req)
        projects = self._projects_map()
        groups = {}  # type: Dict[str, Any]
        for group in ALL_GROUPS:  # 6 групп + inbox
            tasks = db.list_tasks(self.conn, group=group, status='open', sort=sort, project=project)
            groups[group] = {'count': len(tasks), 'tasks': self._tasks(tasks, req.day, projects)}
        review = db.list_tasks(self.conn, status='open', sort=sort, needs_review=1, project=project)
        return {
            'groups': groups,
            'review': self._tasks(review, req.day, projects),
            'projects': db.list_projects(self.conn),
            'project': project,
        }

    # ------------------------------------------------------------------ v2: «Входящие», предстоящие, проекты

    def h_inbox(self, req: Request, m: Any) -> Dict[str, Any]:
        project = self._project_param(req)
        tasks = db.list_tasks(self.conn, group=engine.INBOX, status='open', sort='created', project=project)
        return {'tasks': self._tasks(tasks, req.day)}

    def h_upcoming(self, req: Request, m: Any) -> Dict[str, Any]:
        if 'days' in req.query and req.param('days') is None:
            raise _validation('days', 'days: 1..%d' % UPCOMING_MAX_DAYS)  # ?days= (пусто) — не умолчание, а ошибка
        days = _int_param(req.param('days'), 'days', 1, UPCOMING_MAX_DAYS, UPCOMING_DEFAULT_DAYS)
        project = self._project_param(req)
        start = req.day
        end = start + timedelta(days=days)
        projects = self._projects_map()
        settings = self._settings()
        calendar = self._calendar_view(settings, start, end)
        open_tasks = db.list_tasks(self.conn, status='open', sort='due', project=project)
        by_day = {}  # type: Dict[str, List[Dict[str, Any]]]
        overdue = []  # type: List[Dict[str, Any]]
        for task in open_tasks:
            if task.get('group') == engine.INBOX or not task.get('due_date'):
                continue
            due = engine.parse_date(task['due_date'])
            if due is None:
                continue
            if due < start:
                overdue.append(task)
            elif due < end:
                by_day.setdefault(due.isoformat(), []).append(task)
        day_items = []
        for i in range(days):
            day = start + timedelta(days=i)
            key = day.isoformat()
            day_items.append({
                'date': key,
                'weekday': day.isoweekday(),  # 1 = понедельник … 7 = воскресенье
                'tasks': self._tasks(by_day.get(key, []), req.day, projects),
                'events': _events_between(calendar['events'], day, day + timedelta(days=1)),
            })
        return {
            'from': start.isoformat(),
            'to': (end - timedelta(days=1)).isoformat(),
            'days': day_items,
            'overdue': self._tasks(overdue, req.day, projects),
            'calendar': calendar['status'],
        }

    def h_projects_list(self, req: Request, m: Any) -> Dict[str, Any]:
        archived = req.choice('archived', FLAG_VALUES)
        include = archived in ('1', 'true')
        return {'projects': db.list_projects(self.conn, include_archived=include)}

    def h_projects_create(self, req: Request, m: Any) -> Tuple[int, Dict[str, Any]]:
        body = req.json_object()
        try:
            project = db.create_project(self.conn, body.get('name'), body.get('color'), _now_for(req.day),
                                        project_id=body.get('id') if isinstance(body.get('id'), str) else None)
        except ValueError as e:
            raise _validation(str(e) or 'project')
        return 201, {'project': project}

    def h_project_patch(self, req: Request, m: Any) -> Dict[str, Any]:
        body = req.json_object()
        project = self._project_or_404(m.group('pid'))
        fields = {}  # type: Dict[str, Any]
        for key in ('name', 'color', 'sort', 'archived'):
            if body.get(key) is not None:
                fields[key] = body[key]
        try:
            updated = db.update_project(self.conn, project['id'], now=_now_for(req.day), **fields)
        except ValueError as e:
            code = str(e) or 'project'
            message = 'проект Praxis нельзя архивировать' if code == 'archived' and project['id'] == db.DEFAULT_PROJECT else None
            raise _validation(code, message)
        except db.NotFound:
            raise _not_found('проект не найден')
        return {'project': updated}

    def h_project_delete(self, req: Request, m: Any) -> Dict[str, Any]:
        project = self._project_or_404(m.group('pid'))
        try:
            db.delete_project(self.conn, project['id'])
        except ValueError as e:
            if str(e) == 'not_empty':
                raise ApiError(409, 'not_empty', 'в проекте есть задачи — сначала перенесите или удалите их',
                               open_count=project.get('open_count', 0))
            raise _validation('id', 'проект Praxis удалить нельзя')
        except db.NotFound:
            raise _not_found('проект не найден')
        return {'ok': True}

    # ------------------------------------------------------------------ v2: Google Календарь (§12)

    @staticmethod
    def _calendar_module() -> Any:
        """calendar_ics импортируется лениво: модуль пишется отдельно, его отсутствие — не падение сервера."""
        try:
            import calendar_ics  # noqa: WPS433
        except ImportError:
            return None
        return calendar_ics

    @staticmethod
    def _cache_age_minutes(cache: Optional[Dict[str, Any]]) -> Optional[float]:
        """Возраст кэша в минутах по fetched_at (ISO локального времени); None, если кэша нет/дата битая."""
        if not isinstance(cache, dict):
            return None
        raw = cache.get('fetched_at')
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            fetched = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
        if fetched.tzinfo is not None:
            fetched = fetched.astimezone().replace(tzinfo=None)
        return max(0.0, (datetime.now() - fetched).total_seconds() / 60.0)

    @staticmethod
    def _calendar_error_text(cache: Optional[Dict[str, Any]], total: int) -> Optional[str]:
        errors = (cache or {}).get('errors') or []
        if not errors:
            return None
        parts = []
        for err in errors[:3]:
            if isinstance(err, dict):
                idx = err.get('url_index')
                prefix = ('адрес %d: ' % (int(idx) + 1)) if isinstance(idx, int) and idx >= 0 else ''
                parts.append(prefix + _redact(err.get('message') or 'ошибка'))
            else:
                parts.append(_redact(err))
        head = 'не загрузились %d из %d адресов' % (min(len(errors), total), total) if total else 'ошибка загрузки'
        return head + ': ' + '; '.join(parts)

    def _calendar_view(self, settings: Dict[str, Any], start: date, end: date) -> Dict[str, Any]:
        """События кэша в [start, end) + статус {configured, fetched_at, error}; при устаревшем кэше
        запускает фоновое обновление (ответ не ждёт сети). Вызывается под Lock."""
        urls = [u for u in (settings.get('calendar_urls') or []) if isinstance(u, str) and u.strip()]
        configured = bool(urls)
        cache = db.calendar_cache_get(self.conn)
        status = {'configured': configured, 'fetched_at': None, 'error': None}  # type: Dict[str, Any]
        if not configured:
            return {'events': [], 'status': status}
        module = self._calendar_module()
        if module is None:
            status['error'] = 'calendar_ics не найден'
            return {'events': [], 'status': status}
        if cache is not None:
            status['fetched_at'] = cache.get('fetched_at')
            status['error'] = self._calendar_error_text(cache, len(urls))
        if self._calendar_stale(settings, cache):
            self._start_calendar_refresh(urls, settings, start)
        status['refreshing'] = self._cal_thread is not None and self._cal_thread.is_alive()
        if cache is None:
            return {'events': [], 'status': status}
        return {'events': _events_between(cache.get('events') or [], start, end), 'status': status}

    def _calendar_stale(self, settings: Dict[str, Any], cache: Optional[Dict[str, Any]]) -> bool:
        try:
            refresh_min = float(settings.get('calendar_refresh_min') or engine.DEFAULT_SETTINGS['calendar_refresh_min'])
        except (TypeError, ValueError):
            refresh_min = float(engine.DEFAULT_SETTINGS['calendar_refresh_min'])
        age = self._cache_age_minutes(cache)
        return age is None or age >= refresh_min

    def _start_calendar_refresh(self, urls: List[str], settings: Dict[str, Any], day: date) -> bool:
        """Фоновое обновление: не больше одного потока одновременно и не чаще CALENDAR_RETRY_SECONDS.
        Вызывается под Lock (флаги защищены им)."""
        if self._cal_thread is not None and self._cal_thread.is_alive():
            return False
        if time.monotonic() - self._cal_last_attempt < CALENDAR_RETRY_SECONDS:
            return False
        self._cal_last_attempt = time.monotonic()
        tz = str(settings.get('timezone') or engine.DEFAULT_SETTINGS['timezone'])
        thread = threading.Thread(target=self._calendar_worker, args=(list(urls), tz, day),
                                  name='fokus-calendar', daemon=True)
        self._cal_thread = thread
        thread.start()
        return True

    def _fetch_calendar(self, urls: List[str], tz: str, day: date) -> Dict[str, Any]:
        """Сеть: calendar_ics.refresh по всем адресам. БЕЗ Lock. Исключений не бросает."""
        module = self._calendar_module()
        if module is None:
            return {'events': None, 'fetched_at': None,
                    'errors': [{'url_index': -1, 'message': 'calendar_ics не найден'}]}
        try:
            result = module.refresh(urls, day, tz, days_back=CALENDAR_DAYS_BACK, days_forward=CALENDAR_DAYS_FORWARD)
        except Exception as e:  # noqa: BLE001 — любой сбой модуля: кэш не трогаем, ошибку показываем
            return {'events': None, 'fetched_at': None,
                    'errors': [{'url_index': -1, 'message': _redact('%s: %s' % (type(e).__name__, e))}]}
        if not isinstance(result, dict):
            return {'events': None, 'fetched_at': None,
                    'errors': [{'url_index': -1, 'message': 'calendar_ics.refresh вернул не dict'}]}
        events = result.get('events')
        errors = []
        for err in result.get('errors') or []:
            if isinstance(err, dict):
                errors.append({'url_index': err.get('url_index'), 'message': _redact(err.get('message'))})
            else:
                errors.append({'url_index': -1, 'message': _redact(err)})
        return {
            'events': events if isinstance(events, list) else [],
            'fetched_at': result.get('fetched_at') or datetime.now().isoformat(timespec='seconds'),
            'errors': errors,
            'window': result.get('window'),
        }

    def _store_calendar(self, result: Dict[str, Any], urls: List[str]) -> Dict[str, Any]:
        """Запись результата в кэш — ПОД Lock. Если адреса за время загрузки изменились — результат
        отбрасывается. Если не загрузился ни один адрес, прежние события сохраняются (ошибка — в errors)."""
        current = [u for u in (db.get_settings(self.conn).get('calendar_urls') or []) if isinstance(u, str)]
        if current != list(urls):
            return db.calendar_cache_get(self.conn) or {'events': [], 'fetched_at': None, 'errors': []}
        previous = db.calendar_cache_get(self.conn)
        all_failed = result.get('events') is None or (urls and len(result.get('errors') or []) >= len(urls))
        if all_failed and previous is not None:
            previous = dict(previous)
            previous['errors'] = result.get('errors') or []
            previous['failed_at'] = datetime.now().isoformat(timespec='seconds')
            db.calendar_cache_set(self.conn, previous)
            return previous
        payload = {
            'events': result.get('events') or [],
            'fetched_at': result.get('fetched_at') or datetime.now().isoformat(timespec='seconds'),
            'errors': result.get('errors') or [],
            'window': result.get('window'),
            'urls_count': len(urls),
        }
        db.calendar_cache_set(self.conn, payload)
        return payload

    def _calendar_worker(self, urls: List[str], tz: str, day: date) -> None:
        try:
            result = self._fetch_calendar(urls, tz, day)
            with self.lock:
                self._store_calendar(result, urls)
        except Exception as e:  # noqa: BLE001 — фоновый поток не должен падать (и печатать трейсбек с адресами)
            sys.stderr.write('календарь: фоновое обновление не удалось: %s\n' % _redact(e))

    def h_calendar_get(self, req: Request, m: Any) -> Dict[str, Any]:
        start = _parse_day(req.param('from'), 'from') if req.param('from') is not None else req.day
        end_incl = _parse_day(req.param('to'), 'to') if req.param('to') is not None else start + timedelta(days=CALENDAR_DEFAULT_SPAN)
        if end_incl < start:
            raise _validation('to', 'to раньше from')
        if (end_incl - start).days > CALENDAR_MAX_SPAN:
            raise _validation('to', 'диапазон не больше %d дней' % CALENDAR_MAX_SPAN)
        view = self._calendar_view(self._settings(), start, end_incl + timedelta(days=1))
        payload = {'from': start.isoformat(), 'to': end_incl.isoformat(), 'events': view['events']}
        payload.update(view['status'])
        return payload

    def h_calendar_refresh(self, req: Request, m: Any) -> Deferred:
        settings = self._settings()
        urls = [u for u in (settings.get('calendar_urls') or []) if isinstance(u, str) and u.strip()]
        if not urls:
            raise ApiError(400, 'not_configured', 'адреса календаря не заданы (Настройки → Google Календарь)')
        if self._calendar_module() is None:
            raise ApiError(500, 'internal', 'модуль calendar_ics не найден')
        tz = str(settings.get('timezone') or engine.DEFAULT_SETTINGS['timezone'])
        day = req.day
        self._cal_last_attempt = time.monotonic()

        def finish() -> Tuple[int, Dict[str, Any]]:
            result = self._fetch_calendar(urls, tz, day)  # сеть — вне Lock
            with self.lock:
                stored = self._store_calendar(result, urls)
            return 200, {
                'events_count': len(stored.get('events') or []),
                'fetched_at': stored.get('fetched_at'),
                'errors': result.get('errors') or [],
            }

        return Deferred(finish)

    # ------------------------------------------------------------------ v2: выключение

    def _shutdown_later(self) -> None:
        time.sleep(SHUTDOWN_DELAY)
        try:
            if self.shutdown_hook is not None:
                self.shutdown_hook()
            elif self.server is not None:
                self.server.shutdown()  # из другого потока: serve_forever() завершится, main() закроет БД
        except Exception as e:  # noqa: BLE001
            sys.stderr.write('shutdown: %s\n' % e)

    def h_shutdown(self, req: Request, m: Any) -> Dict[str, Any]:
        threading.Thread(target=self._shutdown_later, name='fokus-shutdown', daemon=True).start()
        return {'ok': True}

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
    app.server = server  # для POST /api/shutdown (server.shutdown() из отдельного потока)
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
        _log_line('остановка (POST /api/shutdown)')  # serve_forever() вернулся штатно — только после server.shutdown()
    except KeyboardInterrupt:
        _log_line('остановка (Ctrl-C / SIGTERM)')
    finally:
        server.server_close()
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
