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
import os
import subprocess
import sys
import time
import urllib.error
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
        self.child = None  # type: Optional[subprocess.Popen]

    def raw(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
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
            status, data = self.raw('GET', '/api/health')
        except FokusUnavailable:
            return False
        return status == 200 and isinstance(data, dict) and data.get('ok') is True

    def ensure_running(self) -> None:
        if self.healthy():
            return
        if not self.autostart:
            raise FokusUnavailable('Fokus не запущен на %s. Запустите Fokus.command.' % self.base)
        server_py = os.path.join(HERE, 'server.py')
        if not os.path.isfile(server_py):
            raise FokusUnavailable('не найден %s' % server_py)
        log('Fokus не запущен — запускаю %s' % server_py)
        env = dict(os.environ)
        env['FOKUS_PORT'] = str(self.port)
        # лог сервера — рядом с его БД (data/server.log; при FOKUS_DB — рядом с указанным файлом)
        db_path = env.get('FOKUS_DB', '').strip() or os.path.join(HERE, 'data', 'fokus.db')
        log_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(log_dir, exist_ok=True)
        logfile = open(os.path.join(log_dir, 'server.log'), 'ab')
        # stdout сервера НЕ должен попасть в наш stdout (там только JSON-RPC)
        self.child = subprocess.Popen(
            [sys.executable, server_py], cwd=HERE, env=env,
            stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
            start_new_session=True,
        )
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self.healthy():
                log('Fokus запущен: %s' % self.base)
                return
            if self.child.poll() is not None:
                break
            time.sleep(0.25)
        raise FokusUnavailable('не удалось запустить Fokus (см. data/server.log)')

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
GROUP_ENUM = ['now', 'priority', 'queue', 'delegate', 'someday', 'waiting']


def _answers_schema(required: bool) -> dict:
    return {'type': 'object', 'properties': dict(ANSWER_PROPERTIES),
            'required': ANSWER_REQUIRED if required else [], 'additionalProperties': False}


def _task_line(t: dict) -> str:
    parts = ['#%s' % t.get('id'), '«%s»' % t.get('title', ''), GROUP_LABEL.get(t.get('group'), t.get('group')),
             '%.1f' % float(t.get('score') or 0)]
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
                            'Возвращает группу, балл и подсказки; add_to_today=true — сразу поставить в «Сегодня» (только если группа 🔴 и есть место).',
             'inputSchema': {'type': 'object',
                             'properties': {'title': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': 'Короткое название'},
                                            'note': {'type': 'string', 'maxLength': 2000, 'description': 'Заметка (необязательно)'},
                                            'answers': _answers_schema(True),
                                            'add_to_today': {'type': 'boolean', 'description': 'Сразу поставить в «Сегодня», если можно'}},
                             'required': ['title', 'answers'], 'additionalProperties': False}},
            {'name': 'fokus_board',
             'description': 'Доска групп: количество задач в каждой из 6 групп и сами задачи, плюс список задач, требующих переоценки.',
             'inputSchema': {'type': 'object', 'properties': {'sort': {'type': 'string', 'enum': ['score', 'due', 'created'],
                                                                      'description': 'Сортировка внутри групп'}},
                             'additionalProperties': False}},
            {'name': 'fokus_list_tasks',
             'description': 'Список задач с фильтрами: группа, срок, кто делает, длительность, статус (open/done/all), needs_review.',
             'inputSchema': {'type': 'object',
                             'properties': {'group': {'type': 'string', 'enum': GROUP_ENUM},
                                            'due': ANSWER_PROPERTIES['due'], 'who': ANSWER_PROPERTIES['who'],
                                            'duration': ANSWER_PROPERTIES['duration'],
                                            'status': {'type': 'string', 'enum': ['open', 'done', 'all'], 'description': 'по умолчанию open'},
                                            'needs_review': {'type': 'boolean', 'description': 'только задачи, которым нужна переоценка'},
                                            'sort': {'type': 'string', 'enum': ['score', 'due', 'created']}},
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
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id, 'group': {'type': 'string', 'enum': GROUP_ENUM}},
                             'required': ['task_id', 'group'], 'additionalProperties': False}},
            {'name': 'fokus_reevaluate',
             'description': 'Пройти 6 вопросов заново для существующей задачи (после 3 переносов или 7+ дней). Сбрасывает счётчик переносов.',
             'inputSchema': {'type': 'object', 'properties': {'task_id': task_id, 'answers': _answers_schema(True)},
                             'required': ['task_id', 'answers'], 'additionalProperties': False}},
            {'name': 'fokus_edit',
             'description': 'Изменить название и/или заметку задачи.',
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
             'description': 'Настройки Fokus: веса вопросов, пороги групп, лимит задач в день, язык.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        ]

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
    def _tid(args: dict, key: str = 'task_id') -> int:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 1:
            raise ValueError('%s: нужен целый id ≥ 1' % key)
        return int(value)

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

    def tool_add_task(self, args: dict) -> Tuple[str, Any, bool]:
        body = {'title': args.get('title'), 'note': args.get('note') or '', 'answers': args.get('answers') or {}}
        status, d = self._api('POST', '/api/tasks', body)
        if status != 201:
            return self._error(status, d)
        task = d['task']
        text = 'Добавлено: %s' % _task_line(task)
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
        sort = args.get('sort') or 'score'
        status, d = self._api('GET', '/api/board?sort=%s' % sort)
        if status != 200:
            return self._error(status, d)
        lines = []
        for g in GROUP_ENUM:
            grp = d['groups'].get(g) or {'count': 0, 'tasks': []}
            lines.append('%s — %d' % (GROUP_LABEL[g], grp['count']))
            lines += ['  ' + _task_line(t) for t in grp['tasks']]
        if d.get('review'):
            lines.append('Нужна переоценка: %d' % len(d['review']))
            lines += ['  ' + _task_line(t) for t in d['review']]
        return '\n'.join(lines), d, False

    def tool_list_tasks(self, args: dict) -> Tuple[str, Any, bool]:
        qs = []
        for key in ('group', 'due', 'who', 'duration', 'status', 'sort'):
            if args.get(key):
                qs.append('%s=%s' % (key, args[key]))
        if args.get('needs_review'):
            qs.append('needs_review=1')
        status, d = self._api('GET', '/api/tasks' + ('?' + '&'.join(qs) if qs else ''))
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
        return self._simple('POST', '/api/tasks/%d/move' % self._tid(args), 'Перенесено (вручную): ', {'group': args.get('group')})

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
            qs.append('limit=%d' % int(args['limit']))
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
        return json.dumps(d, ensure_ascii=False, indent=1), d, False


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
        data = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
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
            except ValueError:
                self.fail(None, -32700, 'Parse error')
                continue
            if isinstance(message, list):  # batch (устаревший, но поддержим)
                for item in message:
                    self.handle(item)
            else:
                self.handle(message)

    # ---- диспетчер
    def handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            self.fail(None, -32600, 'Invalid Request')
            return
        msg_id = message.get('id')
        method = message.get('method')
        params = message.get('params') or {}
        if method is None:
            return  # ответ клиента (на наш ping и т.п.) — игнорируем
        is_notification = 'id' not in message
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
                                 'Перед добавлением задай вопросы пользователю и покажи итог через fokus_preview.'),
            }
        if method == 'notifications/initialized':
            self.initialized = True
            return None
        if method.startswith('notifications/'):
            return None
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': self.tools.definitions()}
        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            if not isinstance(name, str):
                raise McpError(-32602, 'Invalid params: name')
            known = set(t['name'] for t in self.tools.definitions())
            if name not in known:
                raise McpError(-32602, 'Unknown tool: %s' % name)
            try:
                text, structured, is_error = self.tools.call(name, args)
            except ValueError as e:
                text, structured, is_error = 'Неверные параметры: %s' % e, None, True
            result = {'content': [{'type': 'text', 'text': text}], 'isError': bool(is_error)}  # type: Dict[str, Any]
            if structured is not None and isinstance(structured, dict) and not is_error:
                result['structuredContent'] = structured
            return result
        if method == 'prompts/list':
            return {'prompts': PROMPTS}
        if method == 'prompts/get':
            name = params.get('name')
            try:
                text = prompt_text(name, params.get('arguments') or {})
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
            target = argv[argv.index('--path') + 1]
        path = install(target)
        print('Прописано в %s (копия старого файла рядом, .bak). Перезапустите Claude Desktop.' % path)
        return 0
    port = int(os.environ.get('FOKUS_PORT', '4321') or 4321)
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
