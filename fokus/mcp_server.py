#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fokus — MCP-сервер (Model Context Protocol, транспорт stdio).

Позволяет Claude Desktop, Claude Code и другим MCP-клиентам работать с Fokus:
добавлять задачи через мастер из 6 вопросов, смотреть «Сегодня» и доску,
отмечать «готово», переносить, переоценивать. Только стандартная библиотека,
синтаксис Python 3.9 (macOS-системный python3).

Как работает: это тонкий клиент к HTTP API Fokus (http://127.0.0.1:4321, AGENTS.md §7).
Если сервер Fokus не запущен — запускает `server.py` рядом с собой (можно запретить:
FOKUS_MCP_AUTOSTART=0); вывод сервера — в data/server.log рядом с БД. Вся логика приоритетов остаётся в одном процессе server.py.

Протокол: JSON-RPC 2.0, по одному сообщению в строке на stdin/stdout, UTF-8.
В stdout — только JSON-RPC; служебные сообщения — в stderr.

Запуск вручную:      python3 mcp_server.py
Показать конфиг:     python3 mcp_server.py --config
Прописать в Claude Desktop: python3 mcp_server.py --install   (делает копию старого конфига)
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = '1.0'
SERVER_NAME = 'fokus'
PROTOCOL_VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18')
DEFAULT_PROTOCOL = '2025-06-18'

GROUP_LABEL = {
    'now': '🔴 Очень важно', 'priority': '🟠 Приоритет', 'queue': '🔵 Очередь',
    'delegate': '⚪ Поручить', 'someday': '⚫ Отложено / может быть', 'waiting': '⏸ Ждёт',
    'inbox': '📥 Входящие (без мастера)',
}
HINT_LABEL = {
    'do_now': '⚡ ≤ 5 минут и важно — сделай сразу',
    'delegate': '⚪ поручить',
    'waiting': '⏸ ждёт зависимости',
    'someday_review': '⚫ раз в неделю: удалить или вернуть',
}


def log(text: str) -> None:
    sys.stderr.write('fokus-mcp: %s\n' % text)
    sys.stderr.flush()


# --------------------------------------------------------------------------- клиент к Fokus

class FokusUnavailable(Exception):
    pass


class FokusClient(object):
    """HTTP-клиент к server.py с автозапуском."""

    def __init__(self, port: int, autostart: bool = True, timeout: float = 10.0) -> None:
        self.port = port
        self.base = 'http://127.0.0.1:%d' % port
        self.autostart = autostart
        self.timeout = timeout
        self.health_timeout = 2.0          # localhost отвечает мгновенно; «молчащий» чужой порт не должен держать 10 с
        self.child = None  # type: Optional[subprocess.Popen]
        self.failed_until = 0.0            # после неудачного автозапуска не пытаться снова N секунд
        self.log_path = ''

    def raw(self, method: str, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Tuple[int, Any]:
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8') or 'null')
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode('utf-8') or 'null')
            except ValueError:
                payload = {'error': 'http_%d' % e.code, 'message': str(e)}
            return e.code, payload
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FokusUnavailable(str(e))

    def healthy(self) -> bool:
        try:
            status, data = self.raw('GET', '/api/health', timeout=self.health_timeout)
        except FokusUnavailable:
            return False
        return status == 200 and isinstance(data, dict) and data.get('ok') is True

    def _last_log_line(self) -> str:
        try:
            with open(self.log_path, 'rb') as f:
                lines = [ln for ln in f.read()[-4000:].decode('utf-8', 'replace').splitlines() if ln.strip()]
            return lines[-1].strip() if lines else ''
        except OSError:
            return ''

    def ensure_running(self) -> None:
        if self.healthy():
            return
        if not self.autostart:
            raise FokusUnavailable('Fokus не запущен на %s. Запустите Fokus.command.' % self.base)
        if time.time() < self.failed_until:
            raise FokusUnavailable('Fokus не запускается на %s (повторная попытка через несколько секунд); лог: %s'
                                   % (self.base, self.log_path or 'data/server.log'))
        server_py = os.path.join(HERE, 'server.py')
        if not os.path.isfile(server_py):
            raise FokusUnavailable('не найден %s' % server_py)
        log('Fokus не запущен — запускаю %s' % server_py)
        env = dict(os.environ)
        env['FOKUS_PORT'] = str(self.port)
        # путь к БД — как его поймёт server.py (относительный → от папки проекта); лог — рядом с БД
        db_path = env.get('FOKUS_DB', '').strip() or os.path.join(HERE, 'data', 'fokus.db')
        if not os.path.isabs(db_path):
            db_path = os.path.join(HERE, db_path)
        env['FOKUS_DB'] = db_path
        log_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, 'server.log')
        logfile = open(self.log_path, 'ab')
        try:
            # stdout сервера НЕ должен попасть в наш stdout (там только JSON-RPC)
            self.child = subprocess.Popen(
                [sys.executable, server_py], cwd=HERE, env=env,
                stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
                start_new_session=True,
            )
        finally:
            logfile.close()
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self.healthy():
                log('Fokus запущен: %s' % self.base)
                return
            if self.child.poll() is not None:
                break
            time.sleep(0.25)
        if self.healthy():  # сервер мог подняться другим клиентом (Claude Desktop + Claude Code одновременно)
            return
        self.failed_until = time.time() + 15.0
        reason = self._last_log_line()
        raise FokusUnavailable('не удалось запустить Fokus на %s%s. Лог: %s'
                               % (self.base, (' — ' + reason) if reason else '', self.log_path))

    def call(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        self.ensure_running()
        return self.raw(method, path, body)


# --------------------------------------------------------------------------- инструменты

ANSWER_PROPERTIES = {
    'importance': {'type': 'integer', 'minimum': 1, 'maximum': 5,
                   'description': 'Насколько важно: 1 — неважно … 5 — критично (здоровье, практика, семья, деньги)'},
    'due': {'type': 'string', 'enum': ['today', 'tomorrow', 'week', 'month', 'none'],
            'description': 'Когда нужно сделать: today — сегодня, tomorrow — завтра, week — на этой неделе (7 дней), month — в этом месяце (30 дней), none — без срока'},
    'consequence': {'type': 'string', 'enum': ['none', 'unpleasant', 'serious'],
                    'description': 'Что будет, если не сделать вовремя: none — ничего, unpleasant — неприятно, serious — серьёзные последствия'},
    'who': {'type': 'string', 'enum': ['me', 'delegate'],
            'description': 'Кто должен делать: me — я сам, delegate — можно поручить'},
    'delegate_to': {'type': 'string', 'description': 'Кому поручить (Света, агент Мир, другой) — только при who=delegate'},
    'duration': {'type': 'string', 'enum': ['5m', '30m', '2h', 'half_day'],
                 'description': 'Сколько займёт: 5m — ≤ 5 мин, 30m — ≤ 30 мин, 2h — ≤ 2 ч, half_day — полдня и больше'},
    'depends_on': {'type': 'string', 'description': 'От чего зависит (текст); пусто — не зависит'},
}
ANSWER_REQUIRED = ['importance', 'due', 'consequence', 'who', 'duration']
MOVE_GROUP_ENUM = ['now', 'priority', 'queue', 'delegate', 'someday', 'waiting']   # ручной перенос: без inbox
GROUP_ENUM = MOVE_GROUP_ENUM + ['inbox']                                          # фильтры/доска: с «Входящими»
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PROJECT_ARG = {'type': 'string', 'minLength': 1, 'maxLength': 40,
               'description': 'Проект: slug (praxis, privat, projekt, …) или имя без учёта регистра'}


def _answers_schema(required: bool) -> dict:
    return {'type': 'object', 'properties': dict(ANSWER_PROPERTIES),
            'required': ANSWER_REQUIRED if required else [], 'additionalProperties': False}


def _task_line(t: dict) -> str:
    parts = ['#%s' % t.get('id'), '«%s»' % t.get('title', ''), GROUP_LABEL.get(t.get('group'), t.get('group'))]
    if t.get('group') != 'inbox':
        parts.append('%.1f' % float(t.get('score') or 0))
    project_name = t.get('project_name') or t.get('project')
    if project_name:
        parts.append('📁 %s' % project_name)
    dl = t.get('days_left')
    if dl is None:
        parts.append('без срока')
    elif dl < 0:
        parts.append('просрочено на %d дн.' % -dl)
    elif dl == 0:
        parts.append('срок сегодня')
    elif dl == 1:
        parts.append('срок завтра')
    else:
        parts.append('через %d дн.' % dl)
    if t.get('status') == 'done':
        parts.append('✓ сделано')
    if t.get('status') == 'deleted':
        parts.append('🗑 удалена')
    if t.get('is_today'):
        parts.append('[сегодня]')
    if t.get('carry_count'):
        parts.append('перенесено %d раз' % t['carry_count'])
    if t.get('needs_review'):
        parts.append('⚠ пройти вопросы заново')
    if t.get('who') == 'delegate':
        parts.append('поручить: %s' % (t.get('delegate_to') or '—'))
    if t.get('depends_on'):
        parts.append('ждёт: %s' % t['depends_on'])
    return ' · '.join(parts)


def _hints_text(hints: List[str], answers: Optional[dict] = None) -> str:
    out = []
    for h in hints or []:
        if h == 'delegate' and answers and answers.get('delegate_to'):
            out.append('⚪ поручить: %s' % answers['delegate_to'])
        elif h == 'waiting' and answers and answers.get('depends_on'):
            out.append('⏸ ждёт: %s' % answers['depends_on'])
        else:
            out.append(HINT_LABEL.get(h, h))
    return '; '.join(out)


class Tools(object):
    def __init__(self, client: FokusClient) -> None:
        self.client = client

    # ---- описание инструментов (MCP tools/list)
    def definitions(self) -> List[dict]:
        task_id = {'type': 'integer', 'minimum': 1, 'description': 'id задачи'}
        return [
            {'name': 'fokus_today',
             'description': 'Экран «Сегодня»: выбранные задачи дня (не больше лимита, обычно 3), свободные места, '
                            'кандидаты из группы 🔴 по баллу. Начинай с этого инструмента, когда спрашивают «что делать».',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'name': 'fokus_preview',
             'description': 'Итог мастера без сохранения: балл 0..100, группа и подсказки по ответам на 6 вопросов. '
                            'Используй, чтобы показать пользователю, куда попадёт задача, прежде чем сохранять.',
             'inputSchema': {'type': 'object', 'properties': {'answers': _answers_schema(True)},
                             'required': ['answers'], 'additionalProperties': False}},
            {'name': 'fokus_add_task',
             'description': 'Добавить задачу через мастер из 6 вопросов (важность, срок, последствия, кто, длительность, '
                            'зависимость). Перед вызовом задай пользователю эти вопросы. Пациенты — только номер tomedo, без имён. '
                            'Возвращает группу, балл и подсказки; add_to_today=true — сразу поставить в «Сегодня» (только если группа 🔴 и есть место). '
                            'quick=true — быстро записать во «Входящие» без вопросов (answers не нужны; оценить позже через fokus_reevaluate). '
                            'project — проект (slug или имя); #Проект в названии тоже распознаётся.',
             'inputSchema': {'type': 'object',
                             'properties': {'title': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': 'Короткое название (можно с #Проект)'},
                                            'note': {'type': 'string', 'maxLength': 2000, 'description': 'Заметка (необязательно)'},
                                            'answers': _answers_schema(True),
                                            'project': PROJECT_ARG,
                                            'quick': {'type': 'boolean', 'description': 'true — во «Входящие» без мастера (answers не передавать)'},
                                            'add_to_today': {'type': 'boolean', 'description': 'Сразу поставить в «Сегодня», если можно'}},
                             'required': ['title'], 'additionalProperties': False}},
            {'name': 'fokus_board',
             'description': 'Доска групп: количество задач в каждой из 6 групп и во «Входящих», сами задачи, список задач, '
                            'требующих переоценки, и проекты. project — показать только один проект.',
             'inputSchema': {'type': 'object', 'properties': {'sort': {'type': 'string', 'enum': ['score', 'due', 'created'],
                                                                      'description': 'Сортировка внутри групп'},
                                                              'project': PROJECT_ARG},
                             'additionalProperties': False}},
            {'name': 'fokus_list_tasks',
             'description': 'Список задач с фильтрами: группа (включая inbox), проект, срок, кто делает, длительность, статус (open/done/all), needs_review.',
             'inputSchema': {'type': 'object',
                             'properties': {'group': {'type': 'string', 'enum': GROUP_ENUM},
                                            'project': PROJECT_ARG,
                                            'due': ANSWER_PROPERTIES['due'], 'who': ANSWER_PROPERTIES['who'],
                                            'duration': ANSWER_PROPERTIES['duration'],
                                            'status': {'type': 'string', 'enum': ['open', 'done', 'all'], 'description': 'по умолчанию open'},
                                            'needs_review': {'type': 'boolean', 'description': 'только задачи, которым нужна переоценка'},
                                            'sort': {'type': 'string', 'enum': ['score', 'due', 'created']}},
                             'additionalProperties': False}},
            {'name': 'fokus_inbox',
             'description': '«Входящие»: задачи, записанные быстро без мастера (evaluated=0). Их надо оценить через fokus_reevaluate, '
                            'прежде чем они попадут в группу или в «Сегодня».',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'name': 'fokus_projects',
             'description': 'Проекты (Praxis, Privat, Projekt и свои): slug, имя, цвет, число открытых задач. archived=true — вместе с архивными.',
             'inputSchema': {'type': 'object', 'properties': {'archived': {'type': 'boolean', 'description': 'показать и архивные'}},
                             'additionalProperties': False}},
            {'name': 'fokus_set_project',
             'description': 'Перенести задачу в другой проект (slug или имя). Журнал: project_change.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id, 'project': PROJECT_ARG},
                             'required': ['task_id', 'project'], 'additionalProperties': False}},
            {'name': 'fokus_calendar',
             'description': 'События Google Календаря (только чтение, из кэша Fokus) на дату или диапазон: date=YYYY-MM-DD (один день) '
                            'или from/to (до 62 дней); без параметров — сегодня..+7. Если календарь не подключён — configured=false.',
             'inputSchema': {'type': 'object',
                             'properties': {'date': {'type': 'string', 'description': 'YYYY-MM-DD — один день'},
                                            'from': {'type': 'string', 'description': 'YYYY-MM-DD — начало диапазона'},
                                            'to': {'type': 'string', 'description': 'YYYY-MM-DD — конец диапазона (включительно)'}},
                             'additionalProperties': False}},
            {'name': 'fokus_upcoming',
             'description': 'Предстоящие: просроченные задачи и по дням на days вперёд (1..31, по умолчанию 7) — задачи по сроку и события календаря.',
             'inputSchema': {'type': 'object', 'properties': {'days': {'type': 'integer', 'minimum': 1, 'maximum': 31},
                                                              'project': PROJECT_ARG},
                             'additionalProperties': False}},
            {'name': 'fokus_get_task',
             'description': 'Одна задача по id со всеми полями.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id}, 'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_done',
             'description': 'Отметить задачу сделанной.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id}, 'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_reopen',
             'description': 'Вернуть сделанную задачу в открытые.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id}, 'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_today_add',
             'description': 'Поставить задачу в «Сегодня» (только из группы 🔴). Если день полный — вернёт список задач дня: '
                            'спроси пользователя, какую заменить, и повтори с replace_id.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id,
                                                              'replace_id': {'type': 'integer', 'minimum': 1, 'description': 'id задачи дня, которую заменить'}},
                             'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_today_remove',
             'description': 'Убрать задачу из «Сегодня» (задача остаётся в своей группе).',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id}, 'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_move',
             'description': 'Перенести задачу в другую группу вручную (пометка «вручную»; авто-пересчёт её больше не трогает).',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id, 'group': {'type': 'string', 'enum': MOVE_GROUP_ENUM}},
                             'required': ['task_id', 'group'], 'additionalProperties': False}},
            {'name': 'fokus_reevaluate',
             'description': 'Пройти 6 вопросов заново для существующей задачи (после 3 переносов или 7+ дней). Сбрасывает счётчик переносов.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id, 'answers': _answers_schema(True)},
                             'required': ['task_id', 'answers'], 'additionalProperties': False}},
            {'name': 'fokus_edit',
             'description': 'Изменить название и/или заметку задачи. Пациенты — только номер tomedo, без имён.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id,
                                                              'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                                                              'note': {'type': 'string', 'maxLength': 2000}},
                             'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_delete',
             'description': 'Удалить задачу (мягко: остаётся в журнале). Спроси подтверждение у пользователя.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id}, 'required': ['task_id'], 'additionalProperties': False}},
            {'name': 'fokus_review',
             'description': 'Задачи, которым нужна переоценка (3 переноса или 7+ дней без пересмотра).',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'name': 'fokus_journal',
             'description': 'Журнал изменений: по задаче (task_id) или последние записи.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id,
                                                              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}},
                             'additionalProperties': False}},
            {'name': 'fokus_settings',
             'description': 'Настройки Fokus: веса вопросов, пороги групп, лимит задач в день, язык, тема, проект по умолчанию, '
                            'часовой пояс, календарь (адреса iCal, период обновления).',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        ]

    ANNOTATIONS = {'fokus_today': ('Сегодня', {'readOnlyHint': True}), 'fokus_preview': ('Итог мастера (без сохранения)', {'readOnlyHint': True}), 'fokus_add_task': ('Добавить задачу', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False}), 'fokus_board': ('Доска групп', {'readOnlyHint': True}), 'fokus_list_tasks': ('Список задач', {'readOnlyHint': True}), 'fokus_get_task': ('Задача', {'readOnlyHint': True}), 'fokus_done': ('Готово', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_reopen': ('Вернуть', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_today_add': ('В «Сегодня»', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_today_remove': ('Убрать из дня', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_move': ('Перенести в группу', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_reevaluate': ('Пройти вопросы заново', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False}), 'fokus_edit': ('Изменить название', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}), 'fokus_delete': ('Удалить', {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True}), 'fokus_review': ('Нужна переоценка', {'readOnlyHint': True}), 'fokus_journal': ('Журнал', {'readOnlyHint': True}), 'fokus_settings': ('Настройки', {'readOnlyHint': True}),
                   # ---- v2
                   'fokus_inbox': ('Входящие', {'readOnlyHint': True}), 'fokus_projects': ('Проекты', {'readOnlyHint': True}),
                   'fokus_set_project': ('Перенести в проект', {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}),
                   'fokus_calendar': ('Календарь', {'readOnlyHint': True}), 'fokus_upcoming': ('Предстоящие', {'readOnlyHint': True})}

    def definitions_annotated(self) -> List[dict]:
        out = []
        for tool in self.definitions():
            title, hints = self.ANNOTATIONS.get(tool['name'], (None, {}))
            item = dict(tool)
            if title:
                item['title'] = title
            annotations = dict(hints)
            annotations['openWorldHint'] = False  # только локальный Fokus, не интернет
            item['annotations'] = annotations
            out.append(item)
        return out

    # ---- выполнение
    def call(self, name: str, args: dict) -> Tuple[str, Any, bool]:
        """→ (текст для модели, структурированный результат, isError)."""
        handler = getattr(self, 'tool_' + name[len('fokus_'):], None) if name.startswith('fokus_') else None
        if handler is None:
            return 'Неизвестный инструмент: %s' % name, None, True
        if not isinstance(args, dict):
            args = {}
        return handler(args)

    def _api(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        return self.client.call(method, path, body)

    def _error(self, status: int, data: Any) -> Tuple[str, Any, bool]:
        if isinstance(data, dict):
            msg = data.get('message') or data.get('error') or 'ошибка'
            if data.get('field'):
                msg += ' (поле: %s)' % data['field']
            if data.get('error') == 'day_full':
                lines = ['День полный (лимит %s). Какую задачу заменить? Повтори fokus_today_add с replace_id.' % data.get('limit')]
                lines += ['  ' + _task_line(t) for t in data.get('tasks') or []]
                return '\n'.join(lines), data, True
            return 'Ошибка %d: %s' % (status, msg), data, True
        return 'Ошибка %d' % status, data, True

    @staticmethod
    def _int_arg(args: dict, key: str, low: int, high: int, what: str) -> int:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('%s: %s' % (key, what))
        if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
            raise ValueError('%s: %s' % (key, what))
        value = int(value)
        if not low <= value <= high:
            raise ValueError('%s: %s' % (key, what))
        return value

    @classmethod
    def _tid(cls, args: dict, key: str = 'task_id') -> int:
        return cls._int_arg(args, key, 1, 2 ** 63 - 1, 'нужен целый id ≥ 1')

    @staticmethod
    def _enum(args: dict, key: str, allowed: List[str], required: bool = False) -> Optional[str]:
        value = args.get(key)
        if value is None:
            if required:
                raise ValueError('%s: обязательный параметр, одно из %s' % (key, ', '.join(allowed)))
            return None
        if not isinstance(value, str) or value not in allowed:
            raise ValueError('%s: одно из %s' % (key, ', '.join(allowed)))
        return value

    def tool_today(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('GET', '/api/today')
        if status != 200:
            return self._error(status, d)
        lines = ['Сегодня %s: %d из %d, свободных мест: %d' % (d['date'], len(d['tasks']), d['limit'], d['free_slots'])]
        for i, t in enumerate(d['tasks']):
            lines.append('%d. %s' % (i + 1, _task_line(t)))
        if not d['tasks']:
            lines.append('(задач дня нет)')
        if d['free_slots'] > 0:
            lines.append('Кандидаты из 🔴 (по баллу):' if d['candidates'] else 'Кандидатов из 🔴 нет — добавьте задачу через fokus_add_task.')
            lines += ['  ' + _task_line(t) for t in d['candidates']]
        elif d.get('pool'):
            lines.append('Ещё в 🔴 (день полный, можно заменить):')
            lines += ['  ' + _task_line(t) for t in d['pool']]
        return '\n'.join(lines), d, False

    def tool_preview(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('POST', '/api/tasks/preview', {'answers': args.get('answers') or {}})
        if status != 200:
            return self._error(status, d)
        text = 'Группа: %s · балл %.1f' % (GROUP_LABEL.get(d['group'], d['group']), d['score'])
        if d.get('due_date'):
            text += ' · срок %s' % d['due_date']
        hints = _hints_text(d.get('hints'), args.get('answers'))
        if hints:
            text += ' · ' + hints
        return text, d, False

    # ---- проекты (v2): slug или имя → slug через /api/projects
    def _projects(self, include_archived: bool = False) -> List[dict]:
        status, d = self._api('GET', '/api/projects' + ('?archived=1' if include_archived else ''))
        if status != 200 or not isinstance(d, dict):
            raise FokusUnavailable('не удалось получить список проектов (HTTP %s)' % status)
        return list(d.get('projects') or [])

    def _resolve_project(self, args: dict, key: str = 'project', required: bool = False) -> Optional[str]:
        """Параметр project (slug или имя, без учёта регистра) → slug существующего проекта. ValueError, если не найден."""
        raw = args.get(key)
        if raw is None:
            if required:
                raise ValueError('%s: обязательный параметр (slug или имя проекта)' % key)
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('%s: строка — slug или имя проекта' % key)
        wanted = raw.strip().casefold()
        projects = self._projects(include_archived=True)
        for p in projects:  # сначала точный slug, затем имя
            if str(p.get('id', '')).casefold() == wanted:
                return p['id']
        for p in projects:
            if str(p.get('name', '')).casefold() == wanted:
                return p['id']
        known = ', '.join('%s (%s)' % (p.get('id'), p.get('name')) for p in projects if not p.get('archived'))
        raise ValueError('%s: проект «%s» не найден. Есть: %s' % (key, raw.strip(), known or '—'))

    def tool_add_task(self, args: dict) -> Tuple[str, Any, bool]:
        quick = bool(args.get('quick'))
        answers = args.get('answers')
        if not quick and not isinstance(answers, dict):
            raise ValueError('answers: нужны ответы на 6 вопросов — или quick: true, чтобы записать во «Входящие» без мастера')
        body = {'title': args.get('title'), 'note': args.get('note') or ''}  # type: Dict[str, Any]
        if not quick:
            body['answers'] = answers
        project = self._resolve_project(args)
        if project:
            body['project'] = project
        status, d = self._api('POST', '/api/tasks', body)
        if status != 201:
            return self._error(status, d)
        task = d['task']
        text = ('Записано во «Входящие»: %s' if task.get('group') == 'inbox' else 'Добавлено: %s') % _task_line(task)
        if task.get('group') == 'inbox':
            text += '\nОценить позже: fokus_reevaluate с ответами на 6 вопросов.'
        hints = _hints_text(d.get('hints'), task)
        if hints:
            text += '\nПодсказки: ' + hints
        result = {'task': task, 'hints': d.get('hints'), 'added_to_today': False}
        if args.get('add_to_today'):
            if task['group'] != 'now':
                text += '\nВ «Сегодня» не поставлена: группа не 🔴.'
            else:
                st2, d2 = self._api('POST', '/api/today', {'task_id': task['id']})
                if st2 == 200:
                    result['added_to_today'] = True
                    text += '\nПоставлена в «Сегодня» (свободных мест: %d).' % d2.get('free_slots', 0)
                else:
                    err_text, _, _ = self._error(st2, d2)
                    text += '\nВ «Сегодня» не поставлена: ' + err_text
                    result['today_error'] = d2
        return text, result, False

    def tool_board(self, args: dict) -> Tuple[str, Any, bool]:
        sort = self._enum(args, 'sort', ['score', 'due', 'created']) or 'score'
        params = [('sort', sort)]
        project = self._resolve_project(args)
        if project:
            params.append(('project', project))
        status, d = self._api('GET', '/api/board?' + urllib.parse.urlencode(params))
        if status != 200:
            return self._error(status, d)
        lines = []
        if project:
            lines.append('Проект: %s' % project)
        for g in GROUP_ENUM:
            grp = d['groups'].get(g) or {'count': 0, 'tasks': []}
            lines.append('%s — %d' % (GROUP_LABEL[g], grp['count']))
            lines += ['  ' + _task_line(t) for t in grp['tasks']]
        if d.get('review'):
            lines.append('Нужна переоценка: %d' % len(d['review']))
            lines += ['  ' + _task_line(t) for t in d['review']]
        if d.get('projects'):
            lines.append('Проекты: ' + ', '.join('%s (%s) — %s' % (p.get('name'), p.get('id'), p.get('open_count', 0))
                                                 for p in d['projects']))
        return '\n'.join(lines), d, False

    def tool_list_tasks(self, args: dict) -> Tuple[str, Any, bool]:
        params = []
        for key, allowed in (('group', GROUP_ENUM), ('due', ANSWER_PROPERTIES['due']['enum']),
                             ('who', ANSWER_PROPERTIES['who']['enum']), ('duration', ANSWER_PROPERTIES['duration']['enum']),
                             ('status', ['open', 'done', 'all']), ('sort', ['score', 'due', 'created'])):
            value = self._enum(args, key, allowed)
            if value:
                params.append((key, value))
        project = self._resolve_project(args)
        if project:
            params.append(('project', project))
        if args.get('needs_review'):
            params.append(('needs_review', '1'))
        status, d = self._api('GET', '/api/tasks' + ('?' + urllib.parse.urlencode(params) if params else ''))
        if status != 200:
            return self._error(status, d)
        lines = ['Найдено: %d' % len(d['tasks'])] + ['  ' + _task_line(t) for t in d['tasks']]
        return '\n'.join(lines), d, False

    def tool_get_task(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('GET', '/api/tasks/%d' % self._tid(args))
        if status != 200:
            return self._error(status, d)
        t = d['task']
        text = _task_line(t)
        if t.get('note'):
            text += '\nЗаметка: ' + t['note']
        text += '\nОтветы: важность %s, срок %s, последствия %s, кто %s, длительность %s' % (
            t['importance'], t['due'], t['consequence'], t['who'], t['duration'])
        return text, d, False

    def _simple(self, method: str, path: str, prefix: str, body: Optional[dict] = None) -> Tuple[str, Any, bool]:
        status, d = self._api(method, path, body)
        if status != 200:
            return self._error(status, d)
        if isinstance(d, dict) and 'task' in d:
            return prefix + _task_line(d['task']), d, False
        return prefix.strip(), d, False

    def tool_done(self, args: dict) -> Tuple[str, Any, bool]:
        return self._simple('POST', '/api/tasks/%d/done' % self._tid(args), 'Сделано: ')

    def tool_reopen(self, args: dict) -> Tuple[str, Any, bool]:
        return self._simple('POST', '/api/tasks/%d/reopen' % self._tid(args), 'Возвращено: ')

    def tool_today_add(self, args: dict) -> Tuple[str, Any, bool]:
        body = {'task_id': self._tid(args)}  # type: Dict[str, Any]
        if args.get('replace_id') is not None:
            body['replace_id'] = self._tid(args, 'replace_id')
        status, d = self._api('POST', '/api/today', body)
        if status != 200:
            return self._error(status, d)
        lines = ['В «Сегодня» теперь %d задач, свободных мест: %d' % (len(d['tasks']), d['free_slots'])]
        lines += ['%d. %s' % (i + 1, _task_line(t)) for i, t in enumerate(d['tasks'])]
        return '\n'.join(lines), d, False

    def tool_today_remove(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('DELETE', '/api/today/%d' % self._tid(args))
        if status != 200:
            return self._error(status, d)
        return 'Убрано из «Сегодня». Осталось %d, свободных мест: %d' % (len(d['tasks']), d['free_slots']), d, False

    def tool_move(self, args: dict) -> Tuple[str, Any, bool]:
        group = self._enum(args, 'group', GROUP_ENUM, required=True)
        return self._simple('POST', '/api/tasks/%d/move' % self._tid(args), 'Перенесено (вручную): ', {'group': group})

    def tool_reevaluate(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('POST', '/api/tasks/%d/evaluate' % self._tid(args), {'answers': args.get('answers') or {}})
        if status != 200:
            return self._error(status, d)
        text = 'Переоценено: ' + _task_line(d['task'])
        hints = _hints_text(d.get('hints'), d['task'])
        if hints:
            text += '\nПодсказки: ' + hints
        return text, d, False

    def tool_edit(self, args: dict) -> Tuple[str, Any, bool]:
        body = {}
        for key in ('title', 'note'):
            if args.get(key) is not None:
                body[key] = args[key]
        if not body:
            raise ValueError('укажите title и/или note')
        return self._simple('PATCH', '/api/tasks/%d' % self._tid(args), 'Изменено: ', body)

    def tool_delete(self, args: dict) -> Tuple[str, Any, bool]:
        return self._simple('DELETE', '/api/tasks/%d' % self._tid(args), 'Удалено: ')

    def tool_review(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('GET', '/api/review')
        if status != 200:
            return self._error(status, d)
        if not d['tasks']:
            return 'Переоценка никому не нужна.', d, False
        lines = ['Нужна переоценка (%d):' % len(d['tasks'])]
        for t in d['tasks']:
            reason = 'переносилась %d раз' % t.get('carry_count', 0) if t.get('review_reason') == 'carried' \
                else 'лежит с %s' % str(t.get('evaluated_at') or '')[:10]
            lines.append('  %s — %s' % (_task_line(t), reason))
        return '\n'.join(lines), d, False

    def tool_journal(self, args: dict) -> Tuple[str, Any, bool]:
        qs = []
        if args.get('task_id') is not None:
            qs.append('task_id=%d' % self._tid(args))
        if args.get('limit') is not None:
            qs.append('limit=%d' % self._int_arg(args, 'limit', 1, 1000, 'целое 1..1000'))
        status, d = self._api('GET', '/api/journal' + ('?' + '&'.join(qs) if qs else ''))
        if status != 200:
            return self._error(status, d)
        lines = []
        for e in d['entries']:
            change = ''
            if e.get('old_value') or e.get('new_value'):
                change = ' %s → %s' % (e.get('old_value') or '', e.get('new_value') or '')
            if e.get('note'):
                change += ' (%s)' % e['note']
            lines.append('%s  #%s «%s»  %s%s' % (str(e.get('ts', ''))[:16].replace('T', ' '), e.get('task_id'),
                                                 e.get('title') or '', e.get('event'), change))
        return '\n'.join(lines) if lines else 'Журнал пуст.', d, False

    def tool_settings(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('GET', '/api/settings')
        if status != 200:
            return self._error(status, d)
        shown = dict(d) if isinstance(d, dict) else d
        if isinstance(shown, dict) and shown.get('calendar_urls'):
            # секретные адреса — пароль к календарю: модели показываем только их число
            shown['calendar_urls'] = ['<скрыто: %d адрес(ов)>' % len(shown['calendar_urls'])]
        return json.dumps(shown, ensure_ascii=False, indent=1), shown, False

    # ---- v2: «Входящие», проекты, календарь, предстоящие

    def tool_inbox(self, args: dict) -> Tuple[str, Any, bool]:
        status, d = self._api('GET', '/api/inbox')
        if status != 200:
            return self._error(status, d)
        if not d['tasks']:
            return 'Входящие пусты.', d, False
        lines = ['Входящие (%d) — оценить через fokus_reevaluate:' % len(d['tasks'])]
        lines += ['  ' + _task_line(t) for t in d['tasks']]
        return '\n'.join(lines), d, False

    def tool_projects(self, args: dict) -> Tuple[str, Any, bool]:
        include = bool(args.get('archived'))
        status, d = self._api('GET', '/api/projects' + ('?archived=1' if include else ''))
        if status != 200:
            return self._error(status, d)
        lines = []
        for p in d.get('projects') or []:
            line = '%s — slug %s, цвет %s, открытых задач: %s' % (p.get('name'), p.get('id'), p.get('color'), p.get('open_count', 0))
            if p.get('archived'):
                line += ' (в архиве)'
            lines.append(line)
        return '\n'.join(lines) if lines else 'Проектов нет.', d, False

    def tool_set_project(self, args: dict) -> Tuple[str, Any, bool]:
        project = self._resolve_project(args, required=True)
        return self._simple('PATCH', '/api/tasks/%d' % self._tid(args), 'Проект изменён: ', {'project': project})

    @staticmethod
    def _date_arg(args: dict, key: str) -> Optional[str]:
        value = args.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not DATE_RE.match(value.strip()):
            raise ValueError('%s: ожидается дата YYYY-MM-DD' % key)
        return value.strip()

    @staticmethod
    def _event_line(e: dict) -> str:
        start = str(e.get('start') or '')
        end = str(e.get('end') or '')
        if e.get('all_day'):
            when = start + (' — %s' % end if end and end != start else '') + ' (весь день)'
        else:
            when = start.replace('T', ' ')
            if end and end[:10] == start[:10]:
                when += '–' + end[11:16]
            elif end:
                when += ' — ' + end.replace('T', ' ')
        line = '%s  %s' % (when, e.get('summary') or '(без названия)')
        if e.get('location'):
            line += ' · %s' % e['location']
        return line

    def tool_calendar(self, args: dict) -> Tuple[str, Any, bool]:
        one_day = self._date_arg(args, 'date')
        params = []
        if one_day:
            params = [('from', one_day), ('to', one_day)]
        else:
            start = self._date_arg(args, 'from')
            end = self._date_arg(args, 'to')
            if start:
                params.append(('from', start))
            if end:
                params.append(('to', end))
        status, d = self._api('GET', '/api/calendar' + ('?' + urllib.parse.urlencode(params) if params else ''))
        if status != 200:
            return self._error(status, d)
        if not d.get('configured'):
            return 'Календарь не подключён: в Настройках Fokus → Google Календарь вставьте секретный адрес iCal.', d, False
        lines = ['Календарь %s — %s: %d событий' % (d.get('from'), d.get('to'), len(d.get('events') or []))]
        if d.get('error'):
            lines.append('⚠ ' + str(d['error']))
        if d.get('fetched_at'):
            lines.append('обновлено: %s' % str(d['fetched_at'])[:16].replace('T', ' '))
        lines += ['  ' + self._event_line(e) for e in d.get('events') or []]
        return '\n'.join(lines), d, False

    def tool_upcoming(self, args: dict) -> Tuple[str, Any, bool]:
        params = []
        if args.get('days') is not None:
            params.append(('days', str(self._int_arg(args, 'days', 1, 31, 'целое 1..31'))))
        project = self._resolve_project(args)
        if project:
            params.append(('project', project))
        status, d = self._api('GET', '/api/upcoming' + ('?' + urllib.parse.urlencode(params) if params else ''))
        if status != 200:
            return self._error(status, d)
        lines = ['Предстоящие с %s по %s' % (d.get('from'), d.get('to'))]
        if d.get('overdue'):
            lines.append('Просрочено (%d):' % len(d['overdue']))
            lines += ['  ' + _task_line(t) for t in d['overdue']]
        for day in d.get('days') or []:
            tasks = day.get('tasks') or []
            events = day.get('events') or []
            if not tasks and not events:
                continue
            lines.append('%s (день недели %s):' % (day.get('date'), day.get('weekday')))
            lines += ['  🗓 ' + self._event_line(e) for e in events]
            lines += ['  ' + _task_line(t) for t in tasks]
        if len(lines) == 1:
            lines.append('Ни задач по сроку, ни событий.')
        cal = d.get('calendar') or {}
        if cal.get('error'):
            lines.append('⚠ календарь: %s' % cal['error'])
        return '\n'.join(lines), d, False


PROMPTS = [
    {'name': 'fokus_wizard',
     'description': 'Провести пользователя через мастер из 6 вопросов и добавить задачу в Fokus',
     'arguments': [{'name': 'title', 'description': 'Название задачи (если уже известно)', 'required': False}]},
]


def prompt_text(name: str, args: dict) -> str:
    if name != 'fokus_wizard':
        raise KeyError(name)
    title = (args or {}).get('title') or ''
    return (
        'Помоги добавить задачу в Fokus%s. Задай по одному вопросу за раз, крупно и коротко:\n'
        '1. Насколько это важно? 1 — неважно … 5 — критично (здоровье, практика, семья, деньги).\n'
        '2. Когда это нужно сделать? сегодня / завтра / на этой неделе / в этом месяце / без срока.\n'
        '3. Что будет, если не сделать вовремя? ничего / неприятно / серьёзные последствия.\n'
        '4. Кто должен это сделать? я сам / можно поручить (кому: Света, агент Мир, другой).\n'
        '5. Сколько времени займёт? ≤ 5 мин / ≤ 30 мин / ≤ 2 ч / полдня и больше.\n'
        '6. Зависит от чего-то? нет / да — от чего.\n'
        'Затем вызови fokus_preview, покажи группу и балл, и после подтверждения — fokus_add_task. '
        'Если группа 🔴 — предложи поставить в «Сегодня» (fokus_today покажет, есть ли место). '
        'Пациентов называй только номером tomedo, без имён.'
        % ((': «%s»' % title) if title else '')
    )


# --------------------------------------------------------------------------- JSON-RPC / MCP

class McpServer(object):
    def __init__(self, tools: Tools, reader=None, writer=None) -> None:
        self.tools = tools
        self.reader = reader or sys.stdin.buffer
        self.writer = writer or sys.stdout.buffer
        self.initialized = False
        self.protocol = DEFAULT_PROTOCOL

    # ---- транспорт
    def send(self, message: dict) -> None:
        try:
            data = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        except UnicodeEncodeError:  # одиночные суррогаты из входа (\ud800) — отдаём в escape-форме
            data = (json.dumps(message, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
        self.writer.write(data)
        self.writer.flush()

    def reply(self, msg_id: Any, result: Any) -> None:
        self.send({'jsonrpc': '2.0', 'id': msg_id, 'result': result})

    def fail(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        err = {'code': code, 'message': message}  # type: Dict[str, Any]
        if data is not None:
            err['data'] = data
        self.send({'jsonrpc': '2.0', 'id': msg_id, 'error': err})

    def serve_forever(self) -> None:
        for raw in self.reader:
            line = raw.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (ValueError, RecursionError):
                self.fail(None, -32700, 'Parse error')
                continue
            try:
                if isinstance(message, list):  # batch (устаревший, но поддержим)
                    if not message:
                        self.fail(None, -32600, 'Invalid Request')
                    for item in message:
                        self.handle(item)
                else:
                    self.handle(message)
            except Exception as e:  # страховка: одно сообщение не должно убить сервер
                log('необработанная ошибка: %r' % e)

    # ---- диспетчер
    def handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            self.fail(None, -32600, 'Invalid Request')
            return
        msg_id = message.get('id')
        method = message.get('method')
        params = message.get('params')
        if method is None and ('result' in message or 'error' in message):
            return  # ответ клиента (на наш ping и т.п.) — игнорируем
        is_notification = 'id' not in message
        if not isinstance(method, str):
            if not is_notification:
                self.fail(msg_id, -32600, 'Invalid Request')
            return
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if not is_notification:
                self.fail(msg_id, -32602, 'Invalid params')
            return
        try:
            result = self.dispatch(method, params)
        except McpError as e:
            if not is_notification:
                self.fail(msg_id, e.code, e.message, e.data)
            return
        except FokusUnavailable as e:
            if not is_notification:
                self.reply(msg_id, {'content': [{'type': 'text', 'text': 'Fokus недоступен: %s' % e}], 'isError': True})
            return
        except Exception as e:  # не падать из-за одной ошибки
            log('внутренняя ошибка в %s: %r' % (method, e))
            if not is_notification:
                self.fail(msg_id, -32603, 'Internal error', str(e))
            return
        if not is_notification:
            self.reply(msg_id, result if result is not None else {})

    def dispatch(self, method: str, params: dict) -> Any:
        if method == 'initialize':
            requested = params.get('protocolVersion')
            self.protocol = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
            return {
                'protocolVersion': self.protocol,
                'capabilities': {'tools': {'listChanged': False}, 'prompts': {'listChanged': False}},
                'serverInfo': {'name': SERVER_NAME, 'version': VERSION},
                'instructions': ('Fokus — личный дневник задач с приоритетами. Каждая задача проходит 6 вопросов '
                                 '(fokus_add_task), в день не больше 3 задач (fokus_today). Пациенты — только номер tomedo. '
                                 'Перед добавлением задай вопросы пользователю и покажи итог через fokus_preview. '
                                 'Быстрая запись без вопросов — fokus_add_task с quick=true (во «Входящие», fokus_inbox); '
                                 'проекты — fokus_projects (Praxis, Privat, Projekt); календарь — fokus_calendar, fokus_upcoming.'),
            }
        if method == 'notifications/initialized':
            self.initialized = True
            return None
        if method.startswith('notifications/'):
            return None
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': self.tools.definitions_annotated()}
        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments')
            if args is None:
                args = {}
            if not isinstance(name, str) or not isinstance(args, dict):
                raise McpError(-32602, 'Invalid params: name/arguments')
            known = set(t['name'] for t in self.tools.definitions())
            if name not in known:
                raise McpError(-32602, 'Unknown tool: %s' % name)
            try:
                text, structured, is_error = self.tools.call(name, args)
            except (ValueError, TypeError, OverflowError) as e:
                text, structured, is_error = 'Неверные параметры: %s' % e, None, True
            result = {'content': [{'type': 'text', 'text': text}], 'isError': bool(is_error)}  # type: Dict[str, Any]
            if structured is not None and isinstance(structured, dict) and not is_error:
                result['structuredContent'] = structured
            return result
        if method == 'prompts/list':
            return {'prompts': PROMPTS}
        if method == 'prompts/get':
            name = params.get('name')
            arguments = params.get('arguments')
            if arguments is not None and not isinstance(arguments, dict):
                raise McpError(-32602, 'Invalid params: arguments')
            try:
                text = prompt_text(name, arguments or {})
            except KeyError:
                raise McpError(-32602, 'Unknown prompt: %s' % name)
            return {'description': PROMPTS[0]['description'],
                    'messages': [{'role': 'user', 'content': {'type': 'text', 'text': text}}]}
        if method in ('resources/list', 'resources/templates/list'):
            return {'resources': []} if method == 'resources/list' else {'resourceTemplates': []}
        raise McpError(-32601, 'Method not found: %s' % method)


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------- установка

def config_snippet() -> dict:
    return {'mcpServers': {SERVER_NAME: {'command': sys.executable,
                                         'args': [os.path.join(HERE, 'mcp_server.py')]}}}


def claude_desktop_config_path() -> str:
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/Claude/claude_desktop_config.json')
    if os.name == 'nt':
        return os.path.join(os.environ.get('APPDATA', ''), 'Claude', 'claude_desktop_config.json')
    return os.path.expanduser('~/.config/Claude/claude_desktop_config.json')


def install(path: Optional[str] = None) -> str:
    """Прописать сервер в конфиг Claude Desktop (слияние; старый файл → .bak)."""
    path = path or claude_desktop_config_path()
    config = {}  # type: Dict[str, Any]
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        if text.strip():
            config = json.loads(text)
            if not isinstance(config, dict):
                raise ValueError('%s: ожидается JSON-объект' % path)
        backup = path + '.bak'
        if os.path.exists(backup):  # не затирать самую первую копию
            backup = path + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
        with open(backup, 'w', encoding='utf-8') as f:
            f.write(text)
    servers = config.get('mcpServers')
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = config_snippet()['mcpServers'][SERVER_NAME]
    config['mcpServers'] = servers
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return path


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--config' in argv:
        print(json.dumps(config_snippet(), ensure_ascii=False, indent=2))
        print('\nClaude Code:  claude mcp add fokus -- %s %s' % (sys.executable, os.path.join(HERE, 'mcp_server.py')))
        return 0
    if '--install' in argv:
        target = None
        if '--path' in argv:
            idx = argv.index('--path') + 1
            if idx >= len(argv):
                sys.stderr.write('--path: укажите путь к файлу конфигурации\n')
                return 2
            target = argv[idx]
        try:
            path = install(target)
        except (ValueError, OSError) as e:
            sys.stderr.write('Не удалось прописать конфиг: %s\nПроверьте файл вручную или используйте --config.\n' % e)
            return 1
        print('Прописано в %s (копия старого файла рядом, .bak). Перезапустите Claude Desktop.' % path)
        return 0
    raw_port = os.environ.get('FOKUS_PORT', '4321').strip() or '4321'
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        log('FOKUS_PORT: ожидается число 1..65535, получено %r' % raw_port)
        return 2
    port = int(raw_port)
    autostart = os.environ.get('FOKUS_MCP_AUTOSTART', '1').strip() not in ('0', 'false', 'no')
    client = FokusClient(port, autostart=autostart)
    server = McpServer(Tools(client))
    log('старт, Fokus на %s (автозапуск: %s)' % (client.base, 'да' if autostart else 'нет'))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log('стоп')
    return 0


if __name__ == '__main__':
    sys.exit(main())
