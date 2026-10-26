# -*- coding: utf-8 -*-
"""Интеграционные тесты HTTP API v2 (AGENTS.md §7 v2, §12): проекты, «Входящие»,
быстрое добавление с #Проект, календарь, /api/upcoming, /api/shutdown.

Написаны ПО СПЕЦИФИКАЦИИ, не по коду. Каждый класс поднимает свой экземпляр server.py
с отдельной временной БД (FOKUS_DB), свободным портом (FOKUS_PORT) и фиксированной
датой FOKUS_TODAY=2026-10-27 (вторник). Сети нет: календарные адреса указывают на
несуществующий хост, ожидается ошибка в errors, а не 500.
Если server.py ещё не написан — все классы пропускаются (SkipTest).
Запуск из папки fokus:  python3 -m unittest tests.test_v2_api -v
"""
import collections
import datetime
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

FOKUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FOKUS_DIR)

SERVER_PY = os.path.join(FOKUS_DIR, 'server.py')
TODAY = '2026-10-27'
TOMORROW = '2026-10-28'
WEEK = '2026-11-03'

GROUPS6 = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
GROUPS7 = GROUPS6 + ('inbox',)
PROJECT_KEYS = {'id', 'name', 'color', 'sort', 'archived', 'open_count'}
EVENT_KEYS = {'uid', 'summary', 'location', 'start', 'end', 'all_day', 'calendar'}
INBOX_ANSWERS = {'importance': 3, 'due': 'none', 'consequence': 'none', 'who': 'me',
                 'delegate_to': '', 'duration': '30m', 'depends_on': ''}

# Поля объекта Task по §4 v2 + days_left, hints, project_name, project_color (§7 v2).
TASK_FIELDS = {
    'id', 'title', 'note', 'importance', 'due', 'due_date', 'consequence', 'who',
    'delegate_to', 'duration', 'depends_on', 'score', 'group', 'group_manual',
    'group_since', 'evaluated_at', 'needs_review', 'review_reason', 'is_today',
    'today_date', 'carry_count', 'status', 'done_at', 'created_at', 'updated_at',
    'days_left', 'hints', 'project', 'project_name', 'project_color', 'evaluated',
}

SECRET_URL = 'https://example.invalid/calendar/ical/x/private-SECRET42TOKEN/basic.ics'


def answers(importance, due, consequence, who='me', duration='30m',
            delegate_to=None, depends_on=None):
    a = {'importance': importance, 'due': due, 'consequence': consequence,
         'who': who, 'duration': duration}
    if delegate_to is not None:
        a['delegate_to'] = delegate_to
    if depends_on is not None:
        a['depends_on'] = depends_on
    return a


ANS_NOW = answers(5, 'today', 'serious', 'me', '30m')        # 98.5 now
ANS_TOMORROW = answers(5, 'tomorrow', 'none', 'me', 'half_day')  # 69.0 now
ANS_WEEK = answers(2, 'week', 'none', 'me', '30m')            # 32.8 queue
ANS_NONE = answers(2, 'none', 'none', 'me', '2h')             # 13.3 someday


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _drain(pipe, buf):
    try:
        for line in iter(pipe.readline, b''):
            buf.append(line.decode('utf-8', 'replace'))
    except Exception:
        pass


def http_call(base, method, path, body=None, headers=None, raw=None, timeout=10):
    """-> (status, headers, text). 4xx/5xx не бросают; ошибка соединения → (None, {}, текст)."""
    url = base + path
    hdrs = {'Content-Type': 'application/json'}
    if headers:
        hdrs.update(headers)
    data = None
    if raw is not None:
        data = raw if isinstance(raw, bytes) else raw.encode('utf-8')
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
        finally:
            resp.close()
    except urllib.error.HTTPError as e:
        payload = b''
        try:
            payload = e.read()
        except Exception:
            pass
        return e.code, dict(e.headers) if e.headers is not None else {}, payload.decode('utf-8', 'replace')
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
            socket.timeout, OSError) as e:
        return None, {}, 'connection error: %r' % (e,)


def parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


class ServerProc(object):
    """Один процесс server.py на заданной БД и дате."""

    def __init__(self, db_path, today, port=None):
        self.port = port or free_port()
        self.base = 'http://127.0.0.1:%d' % self.port
        env = dict(os.environ)
        env.update({
            'FOKUS_PORT': str(self.port),
            'FOKUS_DB': db_path,
            'FOKUS_TODAY': today,
            'PYTHONUNBUFFERED': '1',
        })
        # сервер не должен ходить через прокси окружения к 127.0.0.1
        env.setdefault('NO_PROXY', '127.0.0.1,localhost')
        env.setdefault('no_proxy', '127.0.0.1,localhost')
        self.log = collections.deque(maxlen=500)
        self.proc = subprocess.Popen([sys.executable, 'server.py'], cwd=FOKUS_DIR, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._threads = []
        for pipe in (self.proc.stdout, self.proc.stderr):
            t = threading.Thread(target=_drain, args=(pipe, self.log))
            t.daemon = True
            t.start()
            self._threads.append(t)

    def log_text(self):
        return ''.join(self.log)[-4000:]

    def wait_ready(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.stop()
                raise RuntimeError('server.py завершился при старте (код %s):\n%s'
                                   % (self.proc.returncode, self.log_text()))
            status, _, _ = http_call(self.base, 'GET', '/api/health', timeout=2)
            if status == 200:
                return
            time.sleep(0.1)
        self.stop()
        raise RuntimeError('server.py не ответил на /api/health за %s с:\n%s' % (timeout, self.log_text()))

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        """Остановить процесс; не падать, если он уже завершился."""
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        except Exception:
            pass
        for t in self._threads:
            t.join(timeout=2)
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


class ServerTestCase(unittest.TestCase):
    """Базовый класс: один сервер и одна временная БД на класс."""

    TODAY = TODAY
    server = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SERVER_PY):
            raise unittest.SkipTest('server.py ещё нет')
        cls.tmpdir = tempfile.mkdtemp(prefix='fokus-v2-test-')
        cls.db_path = os.path.join(cls.tmpdir, 'fokus-test.db')
        cls.server = cls.start_server(cls.db_path, cls.TODAY)
        cls.base = cls.server.base
        cls.port = cls.server.port

    @classmethod
    def start_server(cls, db_path, today):
        srv = ServerProc(db_path, today)
        srv.wait_ready()
        return srv

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ---- хелперы --------------------------------------------------------

    @classmethod
    def call(cls, method, path, body=None, headers=None, raw=None, timeout=10):
        status, _, text = http_call(cls.base, method, path, body=body, headers=headers, raw=raw, timeout=timeout)
        return status, parse_json(text)

    @classmethod
    def call_full(cls, method, path, body=None, headers=None, raw=None, timeout=10):
        return http_call(cls.base, method, path, body=body, headers=headers, raw=raw, timeout=timeout)

    def post_task(self, title, ans=None, note=None, project=None, expect=201):
        body = {'title': title}
        if ans is not None:
            body['answers'] = ans
        if note is not None:
            body['note'] = note
        if project is not None:
            body['project'] = project
        status, data = self.call('POST', '/api/tasks', body)
        self.assertEqual(status, expect, 'POST /api/tasks %r -> %s %r' % (title, status, data))
        return data

    def create_task(self, title, ans=None, note=None, project=None):
        data = self.post_task(title, ans, note=note, project=project)
        self.assertIn('task', data)
        self.assertIsInstance(data.get('hints'), list)
        return data['task']

    def get_task(self, task_id):
        status, data = self.call('GET', '/api/tasks/%s' % task_id)
        self.assertEqual(status, 200, data)
        return data['task']

    def events(self, task_id):
        status, data = self.call('GET', '/api/journal?task_id=%s' % task_id)
        self.assertEqual(status, 200, data)
        return [e['event'] for e in data['entries']]

    def journal(self, task_id):
        status, data = self.call('GET', '/api/journal?task_id=%s' % task_id)
        self.assertEqual(status, 200, data)
        return data['entries']

    def assertError(self, data, code, field=None):
        self.assertIsInstance(data, dict, 'ошибка должна быть JSON-объектом: %r' % (data,))
        self.assertEqual(data.get('error'), code, data)
        if field is not None:
            self.assertEqual(data.get('field'), field, data)

    def assertTaskShape(self, task):
        self.assertIsInstance(task, dict)
        missing = TASK_FIELDS - set(task)
        self.assertFalse(missing, 'в Task нет полей: %s' % sorted(missing))
        self.assertNotIn('grp', task)
        self.assertIn(task['group'], GROUPS7)
        self.assertIsInstance(task['hints'], list)
        self.assertIn(task['evaluated'], (0, 1))
        self.assertIsInstance(task['project'], str)
        self.assertIsInstance(task['project_name'], str)
        self.assertRegex(task['project_color'], r'^#[0-9a-fA-F]{6}$')

    def projects(self, archived=False):
        status, data = self.call('GET', '/api/projects' + ('?archived=1' if archived else ''))
        self.assertEqual(status, 200, data)
        self.assertIn('projects', data)
        return data['projects']


# =====================================================================
# Проекты: CRUD и коды ошибок
# =====================================================================

class TestProjectsApi(ServerTestCase):
    """Сценарий с состоянием — методы пронумерованы и выполняются по порядку."""

    def test_01_defaults(self):
        projects = self.projects()
        self.assertEqual([p['id'] for p in projects], ['praxis', 'privat', 'projekt'])
        for p in projects:
            self.assertTrue(PROJECT_KEYS <= set(p), PROJECT_KEYS - set(p))
            self.assertEqual(p['archived'], 0)
            self.assertEqual(p['open_count'], 0)
        by = dict((p['id'], p) for p in projects)
        self.assertEqual((by['praxis']['name'], by['praxis']['color']), ('Praxis', '#14b8a6'))
        self.assertEqual((by['privat']['name'], by['privat']['color']), ('Privat', '#22c55e'))
        self.assertEqual((by['projekt']['name'], by['projekt']['color']), ('Projekt', '#a855f7'))

    def test_02_create(self):
        status, data = self.call('POST', '/api/projects', {'name': 'Haus', 'color': '#123456'})
        self.assertEqual(status, 201, data)
        self.assertIn('project', data)
        p = data['project']
        self.assertEqual(p['id'], 'haus')
        self.assertEqual(p['name'], 'Haus')
        self.assertEqual(p['color'], '#123456')
        self.assertEqual(p['archived'], 0)
        self.assertEqual(p.get('open_count', 0), 0)
        self.assertIn('haus', [x['id'] for x in self.projects()])

    def test_03_create_cyrillic_default_color(self):
        status, data = self.call('POST', '/api/projects', {'name': 'Налоги'})
        self.assertEqual(status, 201, data)
        p = data['project']
        self.assertEqual(p['id'], 'nalogi')
        self.assertEqual(p['name'], 'Налоги')
        self.assertEqual(p['color'], '#8b8b8b')

    def test_04_create_validation(self):
        for body, field in (({'name': ''}, 'name'), ({'name': '   '}, 'name'), ({'name': 'x' * 41}, 'name'),
                            ({}, 'name'), ({'name': 'Farbe', 'color': 'red'}, 'color'),
                            ({'name': 'Farbe', 'color': '#FFF'}, 'color'), ({'name': 5}, 'name')):
            with self.subTest(body=body):
                status, data = self.call('POST', '/api/projects', body)
                self.assertEqual(status, 400, (body, data))
                self.assertError(data, 'validation', field)
        status, data = self.call('POST', '/api/projects', raw='{нет')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')
        self.assertEqual(len(self.projects()), 5, 'неудачные запросы ничего не создают')

    def test_05_patch(self):
        status, data = self.call('PATCH', '/api/projects/haus', {'name': 'Haus & Garten', 'color': '#654321', 'sort': 9})
        self.assertEqual(status, 200, data)
        p = data['project']
        self.assertEqual((p['id'], p['name'], p['color'], p['sort']), ('haus', 'Haus & Garten', '#654321', 9))
        status, data = self.call('PATCH', '/api/projects/nope', {'name': 'X'})
        self.assertEqual(status, 404, data)
        self.assertError(data, 'not_found')
        status, data = self.call('PATCH', '/api/projects/haus', {'name': ''})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'name')
        status, data = self.call('PATCH', '/api/projects/haus', {'color': 'green'})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'color')
        status, data = self.call('PATCH', '/api/projects/haus', raw='xxx')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')

    def test_06_praxis_cannot_be_archived(self):
        status, data = self.call('PATCH', '/api/projects/praxis', {'archived': True})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'archived')
        by = dict((p['id'], p) for p in self.projects())
        self.assertEqual(by['praxis']['archived'], 0)
        # переименовать praxis можно
        status, data = self.call('PATCH', '/api/projects/praxis', {'name': 'Praxis Nord'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['project']['name'], 'Praxis Nord')

    def test_07_archive_hides_project(self):
        status, data = self.call('PATCH', '/api/projects/nalogi', {'archived': True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['project']['archived'], 1)
        self.assertNotIn('nalogi', [p['id'] for p in self.projects()])
        self.assertIn('nalogi', [p['id'] for p in self.projects(archived=True)])
        status, data = self.call('PATCH', '/api/projects/nalogi', {'archived': False})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['project']['archived'], 0)
        self.assertIn('nalogi', [p['id'] for p in self.projects()])

    def test_08_delete_not_empty_then_ok(self):
        t = self.create_task('Rasen mähen', ANS_WEEK, project='haus')
        self.assertEqual(t['project'], 'haus')
        by = dict((p['id'], p) for p in self.projects())
        self.assertEqual(by['haus']['open_count'], 1)
        status, data = self.call('DELETE', '/api/projects/haus')
        self.assertEqual(status, 409, data)
        self.assertError(data, 'not_empty')
        self.assertIn('haus', [p['id'] for p in self.projects()])
        # выполненная задача тоже удерживает проект
        status, data = self.call('POST', '/api/tasks/%d/done' % t['id'])
        self.assertEqual(status, 200, data)
        status, data = self.call('DELETE', '/api/projects/haus')
        self.assertEqual(status, 409, data)
        self.assertError(data, 'not_empty')
        # перенести задачу в другой проект → проект пуст → удаление
        status, data = self.call('PATCH', '/api/tasks/%d' % t['id'], {'project': 'privat'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['project'], 'privat')
        status, data = self.call('DELETE', '/api/projects/haus')
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {'ok': True})
        self.assertNotIn('haus', [p['id'] for p in self.projects(archived=True)])
        status, data = self.call('DELETE', '/api/projects/haus')
        self.assertEqual(status, 404, data)
        self.assertError(data, 'not_found')

    def test_09_delete_praxis_and_unknown(self):
        status, data = self.call('DELETE', '/api/projects/praxis')
        self.assertEqual(status, 400, data)
        self.assertIn('error', data)
        self.assertIn('praxis', [p['id'] for p in self.projects()])
        status, data = self.call('DELETE', '/api/projects/nope')
        self.assertEqual(status, 404, data)
        self.assertError(data, 'not_found')

    def test_10_board_has_projects_and_inbox(self):
        status, data = self.call('GET', '/api/board')
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data['groups']), set(GROUPS7))
        self.assertIn('projects', data)
        ids = [p['id'] for p in data['projects']]
        self.assertIn('praxis', ids)
        self.assertIn('privat', ids)
        for p in data['projects']:
            self.assertTrue({'id', 'name', 'color'} <= set(p), p)

    def test_11_open_count_on_projects(self):
        self.create_task('Offen 1', ANS_WEEK, project='projekt')
        self.create_task('Offen 2', ANS_NONE, project='projekt')
        self.create_task('Inbox', None, project='projekt')
        by = dict((p['id'], p) for p in self.projects())
        self.assertEqual(by['projekt']['open_count'], 3)

    def test_12_unknown_method_or_path(self):
        status, data = self.call('GET', '/api/projects/praxis/nope')
        self.assertEqual(status, 404, data)
        self.assertError(data, 'not_found')


# =====================================================================
# «Входящие», быстрое добавление, проект задачи
# =====================================================================

class TestInboxApi(ServerTestCase):
    """Сценарий с состоянием — методы пронумерованы и выполняются по порядку."""

    @classmethod
    def setUpClass(cls):
        super(TestInboxApi, cls).setUpClass()
        cls.ids = {}

    def test_01_create_without_answers(self):
        status, data = self.call('POST', '/api/tasks', {'title': 'Позвонить в банк'})
        self.assertEqual(status, 201, data)
        self.assertIn('task', data)
        self.assertIsInstance(data.get('hints'), list)
        t = data['task']
        self.assertTaskShape(t)
        self.assertEqual(t['title'], 'Позвонить в банк')
        self.assertEqual(t['group'], 'inbox')
        self.assertEqual(t['evaluated'], 0)
        self.assertEqual(t['score'], 0.0)
        self.assertIsNone(t['due_date'])
        self.assertIsNone(t['days_left'])
        self.assertEqual(t['status'], 'open')
        self.assertEqual(t['project'], 'praxis')
        self.assertEqual(t['project_name'], 'Praxis')
        self.assertEqual(t['project_color'], '#14b8a6')
        for key, value in INBOX_ANSWERS.items():
            self.assertEqual(t[key], value, key)
        self.assertEqual(t['is_today'], 0)
        self.assertEqual(t['needs_review'], 0)
        self.__class__.ids['inbox1'] = t['id']
        self.assertEqual(self.events(t['id']), ['create'])

    def test_02_quick_add_tag(self):
        t = self.create_task('Позвонить #Privat')
        self.assertEqual(t['title'], 'Позвонить')
        self.assertEqual(t['project'], 'privat')
        self.assertEqual(t['project_name'], 'Privat')
        self.assertEqual(t['group'], 'inbox')
        self.__class__.ids['tag'] = t['id']
        # регистр не важен, можно по id проекта
        t = self.create_task('Купить лампочку #privat')
        self.assertEqual((t['title'], t['project']), ('Купить лампочку', 'privat'))
        # тег в середине — лишние пробелы схлопываются
        t = self.create_task('Купить  #Projekt   лампочку')
        self.assertEqual((t['title'], t['project']), ('Купить лампочку', 'projekt'))
        # неизвестный тег остаётся в названии, проект по умолчанию
        t = self.create_task('Отчёт по пациенту tomedo #48213')
        self.assertEqual(t['title'], 'Отчёт по пациенту tomedo #48213')
        self.assertEqual(t['project'], 'praxis')
        t = self.create_task('Купить лампочку #Nope')
        self.assertEqual((t['title'], t['project']), ('Купить лампочку #Nope', 'praxis'))

    def test_03_quick_add_tag_with_wizard(self):
        t = self.create_task('Позвонить в налоговую #Privat', ANS_NOW)
        self.assertEqual(t['title'], 'Позвонить в налоговую')
        self.assertEqual(t['project'], 'privat')
        self.assertEqual(t['group'], 'now')
        self.assertEqual(t['evaluated'], 1)
        self.assertAlmostEqual(t['score'], 98.5, places=3)
        self.__class__.ids['now_privat'] = t['id']

    def test_04_explicit_project(self):
        t = self.create_task('Явный проект', None, project='projekt')
        self.assertEqual((t['project'], t['project_name']), ('projekt', 'Projekt'))
        status, data = self.call('POST', '/api/tasks', {'title': 'Нет такого', 'project': 'nope'})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'project')
        status, data = self.call('POST', '/api/tasks', {'title': 'Нет такого', 'project': 'nope', 'answers': ANS_WEEK})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'project')
        status, data = self.call('POST', '/api/tasks', {'title': 'Нет такого', 'project': 7})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'project')
        # тег в названии сильнее явного project (§7: «проект берётся из текста»)
        t = self.create_task('Тег сильнее #Privat', None, project='projekt')
        self.assertEqual((t['title'], t['project']), ('Тег сильнее', 'privat'))

    def test_05_title_only_tag_is_invalid(self):
        status, data = self.call('POST', '/api/tasks', {'title': '#Privat'})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'title')
        status, data = self.call('POST', '/api/tasks', {'title': ''})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'title')

    def test_06_inbox_list(self):
        status, data = self.call('GET', '/api/inbox')
        self.assertEqual(status, 200, data)
        self.assertIn('tasks', data)
        tasks = data['tasks']
        self.assertGreaterEqual(len(tasks), 2)
        for t in tasks:
            self.assertTaskShape(t)
            self.assertEqual(t['group'], 'inbox')
            self.assertEqual(t['evaluated'], 0)
            self.assertEqual(t['status'], 'open')
        created = [t['created_at'] for t in tasks]
        self.assertEqual(created, sorted(created, reverse=True), 'порядок created_at ↓')
        ids = [t['id'] for t in tasks]
        self.assertIn(self.ids['inbox1'], ids)
        self.assertIn(self.ids['tag'], ids)
        self.assertNotIn(self.ids['now_privat'], ids)

    def test_07_tasks_filter_group_inbox_and_board(self):
        status, data = self.call('GET', '/api/tasks?group=inbox')
        self.assertEqual(status, 200, data)
        self.assertTrue(all(t['group'] == 'inbox' for t in data['tasks']))
        inbox_ids = set(t['id'] for t in data['tasks'])
        self.assertIn(self.ids['inbox1'], inbox_ids)
        status, board = self.call('GET', '/api/board')
        self.assertEqual(status, 200, board)
        self.assertEqual(set(board['groups']), set(GROUPS7))
        g = board['groups']['inbox']
        self.assertEqual(g['count'], len(g['tasks']))
        self.assertEqual(set(t['id'] for t in g['tasks']), inbox_ids)
        for t in g['tasks']:
            self.assertEqual(t['group'], 'inbox')
        status, all_tasks = self.call('GET', '/api/tasks')
        self.assertEqual(status, 200)
        self.assertIn(self.ids['inbox1'], [t['id'] for t in all_tasks['tasks']], 'inbox — открытая задача')

    def test_08_today_rejects_inbox(self):
        status, data = self.call('POST', '/api/today', {'task_id': self.ids['inbox1']})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'not_now_group')
        status, today = self.call('GET', '/api/today')
        self.assertEqual(status, 200, today)
        self.assertNotIn(self.ids['inbox1'], [t['id'] for t in today['tasks']])
        self.assertNotIn(self.ids['inbox1'], [t['id'] for t in today['candidates']])
        self.assertNotIn(self.ids['inbox1'], [t['id'] for t in today['pool']])

    def test_09_move_rejects_inbox(self):
        for group in ('now', 'queue', 'someday'):
            with self.subTest(group=group):
                status, data = self.call('POST', '/api/tasks/%d/move' % self.ids['inbox1'], {'group': group})
                self.assertEqual(status, 400, data)
                self.assertError(data, 'validation', 'group')
        t = self.get_task(self.ids['inbox1'])
        self.assertEqual(t['group'], 'inbox')
        self.assertEqual(t['group_manual'], 0)
        self.assertNotIn('manual_move', self.events(self.ids['inbox1']))

    def test_10_evaluate_inbox(self):
        tid = self.ids['inbox1']
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % tid, {'answers': ANS_NOW})
        self.assertEqual(status, 200, data)
        t = data['task']
        self.assertTaskShape(t)
        self.assertEqual(t['group'], 'now')
        self.assertEqual(t['evaluated'], 1)
        self.assertAlmostEqual(t['score'], 98.5, places=3)
        self.assertEqual(t['due_date'], TODAY)
        self.assertEqual(t['days_left'], 0)
        self.assertEqual(t['importance'], 5)
        self.assertIsInstance(data.get('hints'), list)
        evs = self.events(tid)
        self.assertEqual(len(evs), 2)
        self.assertEqual(set(evs), {'create', 'evaluate'})
        entry = [e for e in self.journal(tid) if e['event'] == 'evaluate'][0]
        self.assertTrue(entry['old_value'].startswith('inbox'), entry)
        self.assertEqual(entry['new_value'], 'now/98.5')
        status, data = self.call('GET', '/api/inbox')
        self.assertNotIn(tid, [x['id'] for x in data['tasks']])
        # теперь задачу можно поставить в «Сегодня»
        status, data = self.call('POST', '/api/today', {'task_id': tid})
        self.assertEqual(status, 200, data)
        self.assertIn(tid, [x['id'] for x in data['tasks']])

    def test_11_evaluate_inbox_validation(self):
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % self.ids['tag'],
                                 {'answers': dict(ANS_NOW, importance=9)})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'importance')
        self.assertEqual(self.get_task(self.ids['tag'])['group'], 'inbox')

    def test_12_patch_project(self):
        tid = self.ids['tag']
        status, data = self.call('PATCH', '/api/tasks/%d' % tid, {'project': 'projekt'})
        self.assertEqual(status, 200, data)
        t = data['task']
        self.assertEqual((t['project'], t['project_name'], t['project_color']), ('projekt', 'Projekt', '#a855f7'))
        self.assertEqual(t['group'], 'inbox', 'смена проекта не меняет группу')
        entries = [e for e in self.journal(tid) if e['event'] == 'project_change']
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['old_value'], entries[0]['new_value']), ('privat', 'projekt'))
        status, data = self.call('PATCH', '/api/tasks/%d' % tid, {'project': 'nope'})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'project')
        self.assertEqual(self.get_task(tid)['project'], 'projekt')
        # title и project одним запросом
        status, data = self.call('PATCH', '/api/tasks/%d' % tid, {'title': 'Позвонить маме', 'project': 'privat'})
        self.assertEqual(status, 200, data)
        self.assertEqual((data['task']['title'], data['task']['project']), ('Позвонить маме', 'privat'))
        evs = self.events(tid)
        self.assertIn('edit', evs)
        self.assertEqual(evs.count('project_change'), 2)

    def test_13_tasks_filter_by_project(self):
        status, data = self.call('GET', '/api/tasks?project=privat')
        self.assertEqual(status, 200, data)
        ids = [t['id'] for t in data['tasks']]
        self.assertIn(self.ids['tag'], ids)
        self.assertIn(self.ids['now_privat'], ids)
        self.assertTrue(all(t['project'] == 'privat' for t in data['tasks']))
        status, data = self.call('GET', '/api/tasks?project=praxis')
        self.assertEqual(status, 200, data)
        self.assertNotIn(self.ids['tag'], [t['id'] for t in data['tasks']])
        self.assertTrue(all(t['project'] == 'praxis' for t in data['tasks']))
        status, data = self.call('GET', '/api/tasks?project=privat&group=inbox')
        self.assertEqual(status, 200, data)
        self.assertTrue(all(t['project'] == 'privat' and t['group'] == 'inbox' for t in data['tasks']))
        self.assertIn(self.ids['tag'], [t['id'] for t in data['tasks']])

    def test_14_board_project_filter(self):
        status, data = self.call('GET', '/api/board?project=privat')
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data['groups']), set(GROUPS7))
        seen = []
        for g, body in data['groups'].items():
            self.assertEqual(body['count'], len(body['tasks']), g)
            for t in body['tasks']:
                self.assertEqual(t['project'], 'privat', t)
                self.assertEqual(t['group'], g)
                seen.append(t['id'])
        self.assertIn(self.ids['tag'], seen)
        self.assertIn(self.ids['now_privat'], seen)

    def test_15_task_shape_everywhere(self):
        for path in ('/api/tasks', '/api/inbox', '/api/review'):
            status, data = self.call('GET', path)
            self.assertEqual(status, 200, (path, data))
            for t in data['tasks']:
                self.assertTaskShape(t)
        status, today = self.call('GET', '/api/today')
        self.assertEqual(status, 200)
        for key in ('tasks', 'candidates', 'pool'):
            for t in today[key]:
                self.assertTaskShape(t)

    def test_16_deleted_inbox_task(self):
        t = self.create_task('Удалить меня')
        status, data = self.call('DELETE', '/api/tasks/%d' % t['id'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['status'], 'deleted')
        status, data = self.call('GET', '/api/inbox')
        self.assertNotIn(t['id'], [x['id'] for x in data['tasks']])
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % t['id'], {'answers': ANS_NOW})
        self.assertEqual(status, 404, data)


# =====================================================================
# Календарь: /api/today events, /api/calendar, настройки, refresh
# =====================================================================

class TestCalendarApi(ServerTestCase):
    """Сценарий с состоянием — методы пронумерованы и выполняются по порядку."""

    def test_01_today_has_events_and_calendar(self):
        status, data = self.call('GET', '/api/today')
        self.assertEqual(status, 200, data)
        self.assertIn('events', data)
        self.assertIsInstance(data['events'], list)
        self.assertEqual(data['events'], [])
        self.assertIn('calendar', data)
        cal = data['calendar']
        self.assertIsInstance(cal, dict)
        self.assertIs(cal.get('configured'), False)
        self.assertIn('fetched_at', cal)
        self.assertIn('error', cal)
        self.assertEqual(data['date'], TODAY)

    def test_02_calendar_not_configured(self):
        status, data = self.call('GET', '/api/calendar')
        self.assertEqual(status, 200, data)
        self.assertIs(data.get('configured'), False)
        self.assertEqual(data['events'], [])
        self.assertIn('fetched_at', data)
        self.assertIn('error', data)
        status, data = self.call('GET', '/api/calendar?from=2026-10-27&to=2026-11-03')
        self.assertEqual(status, 200, data)
        self.assertEqual(data['events'], [])

    def test_03_calendar_bad_range(self):
        for q in ('?from=2026-10-01&to=2026-12-31', '?from=abc&to=2026-11-03', '?from=2026-10-27&to=nope',
                  '?from=2026-13-01&to=2026-11-03'):
            with self.subTest(q=q):
                status, data = self.call('GET', '/api/calendar' + q)
                self.assertEqual(status, 400, (q, data))
                self.assertIn('error', data)

    def test_04_refresh_not_configured(self):
        status, data = self.call('POST', '/api/calendar/refresh')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'not_configured')

    def test_05_settings_validation_v2(self):
        bad = [
            {'calendar_urls': ['http://calendar.google.com/calendar/ical/x/basic.ics']},
            {'calendar_urls': 'https://example.invalid/basic.ics'},
            {'calendar_urls': ['https://example.invalid/%d.ics' % i for i in range(11)]},
            {'calendar_urls': [42]},
            {'calendar_refresh_min': 4},
            {'calendar_refresh_min': 1441},
            {'theme': 'blue'},
            {'timezone': 'Nope/Zone'},
            {'timezone': ''},
            {'default_project': 'nope'},
        ]
        for patch in bad:
            with self.subTest(patch=patch):
                status, data = self.call('PUT', '/api/settings', patch)
                self.assertEqual(status, 400, (patch, data))
                self.assertIsInstance(data, dict)
                self.assertIn('error', data)
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(status, 200)
        self.assertEqual(s['calendar_urls'], [])
        self.assertEqual(s['calendar_refresh_min'], 15)
        self.assertEqual(s['theme'], 'dark')
        self.assertEqual(s['timezone'], 'Europe/Berlin')
        self.assertEqual(s['default_project'], 'praxis')
        self.assertEqual(s['schema_version'], 2)
        self.assertIn('calendar_cache', s)

    def test_06_settings_put_ok_and_service_keys_ignored(self):
        status, data = self.call('PUT', '/api/settings', {'theme': 'light', 'timezone': 'UTC',
                                                          'calendar_refresh_min': 5, 'default_project': 'privat'})
        self.assertEqual(status, 200, data)
        self.assertEqual((data['theme'], data['timezone'], data['calendar_refresh_min'], data['default_project']),
                         ('light', 'UTC', 5, 'privat'))
        status, data = self.call('PUT', '/api/settings', {'schema_version': 99, 'calendar_cache': {'events': [1]},
                                                          'theme': 'dark'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['schema_version'], 2)
        self.assertNotEqual(data['calendar_cache'], {'events': [1]})
        self.assertEqual(data['theme'], 'dark')
        status, data = self.call('PUT', '/api/settings', {'timezone': 'Europe/Berlin', 'default_project': 'praxis'})
        self.assertEqual(status, 200, data)

    def test_08_set_calendar_urls(self):
        status, data = self.call('PUT', '/api/settings', {'calendar_urls': ['https://example.invalid/basic.ics']})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['calendar_urls'], ['https://example.invalid/basic.ics'])
        status, data = self.call('PUT', '/api/settings', {'calendar_urls': [SECRET_URL]})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['calendar_urls'], [SECRET_URL], 'в /api/settings адрес показывается как есть')
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(s['calendar_urls'], [SECRET_URL])

    def test_09_health_does_not_leak_secret_url(self):
        status, _, text = self.call_full('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertNotIn('SECRET42TOKEN', text)
        self.assertNotIn('example.invalid', text)

    def test_10_refresh_unreachable_host_is_not_500(self):
        status, _, text = self.call_full('POST', '/api/calendar/refresh', timeout=60)
        data = parse_json(text)
        self.assertIn(status, (200, 502, 504), (status, data))
        self.assertIsInstance(data, dict, data)
        if status == 200:
            self.assertIn('events_count', data)
            self.assertIsInstance(data['events_count'], int)
            self.assertEqual(data['events_count'], 0)
            self.assertIn('fetched_at', data)
            self.assertIsInstance(data['errors'], list)
            self.assertEqual(len(data['errors']), 1)
            err = data['errors'][0]
            self.assertEqual(err.get('url_index'), 0)
            self.assertIn('message', err)
        self.assertNotIn('SECRET42TOKEN', text, 'секретный адрес не должен попадать в ответ об ошибке')
        self.assertTrue(self.server.alive(), 'сервер жив после неудачного обновления')

    def test_11_calendar_configured_after_failed_refresh(self):
        status, data = self.call('GET', '/api/calendar')
        self.assertEqual(status, 200, data)
        self.assertIs(data.get('configured'), True)
        self.assertEqual(data['events'], [])
        status, today = self.call('GET', '/api/today')
        self.assertEqual(status, 200, today)
        self.assertIs(today['calendar'].get('configured'), True)
        self.assertEqual(today['events'], [])
        status, up = self.call('GET', '/api/upcoming?days=3')
        self.assertEqual(status, 200, up)
        for d in up['days']:
            self.assertEqual(d['events'], [])

    def test_12_clear_calendar_urls(self):
        status, data = self.call('PUT', '/api/settings', {'calendar_urls': []})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['calendar_urls'], [])
        status, data = self.call('GET', '/api/calendar')
        self.assertEqual(status, 200, data)
        self.assertIs(data.get('configured'), False)
        status, data = self.call('POST', '/api/calendar/refresh')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'not_configured')


# =====================================================================
# /api/upcoming
# =====================================================================

class TestUpcomingApi(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super(TestUpcomingApi, cls).setUpClass()
        cls.ids = {}
        for name, ans, project in (('today', ANS_NOW, None), ('tomorrow', ANS_TOMORROW, None),
                                   ('week', ANS_WEEK, 'privat'), ('none', ANS_NONE, None),
                                   ('done_today', ANS_NOW, None)):
            body = {'title': 'Задача %s' % name, 'answers': ans}
            if project:
                body['project'] = project
            status, _, text = http_call(cls.base, 'POST', '/api/tasks', body=body)
            assert status == 201, (status, text)
            cls.ids[name] = parse_json(text)['task']['id']
        status, _, text = http_call(cls.base, 'POST', '/api/tasks', body={'title': 'Во входящих'})
        assert status == 201, (status, text)
        cls.ids['inbox'] = parse_json(text)['task']['id']
        status, _, text = http_call(cls.base, 'POST', '/api/tasks/%d/done' % cls.ids['done_today'])
        assert status == 200, (status, text)

    def upcoming(self, query=''):
        status, data = self.call('GET', '/api/upcoming' + query)
        self.assertEqual(status, 200, data)
        return data

    def day_ids(self, data):
        return dict((d['date'], [t['id'] for t in d['tasks']]) for d in data['days'])

    def test_structure(self):
        data = self.upcoming('?days=7')
        for key in ('from', 'days', 'overdue'):
            self.assertIn(key, data)
        self.assertEqual(data['from'], TODAY)
        self.assertEqual(len(data['days']), 7)
        self.assertIsInstance(data['overdue'], list)
        start = datetime.date(2026, 10, 27)
        for i, d in enumerate(data['days']):
            with self.subTest(i=i):
                for key in ('date', 'weekday', 'tasks', 'events'):
                    self.assertIn(key, d)
                self.assertEqual(d['date'], (start + datetime.timedelta(days=i)).isoformat())
                self.assertIsInstance(d['tasks'], list)
                self.assertIsInstance(d['events'], list)
                for t in d['tasks']:
                    self.assertTaskShape(t)
                    self.assertEqual(t['due_date'], d['date'])
                    self.assertEqual(t['status'], 'open')

    def test_tasks_on_their_dates(self):
        data = self.upcoming('?days=8')
        by_date = self.day_ids(data)
        self.assertIn(self.ids['today'], by_date[TODAY])
        self.assertIn(self.ids['tomorrow'], by_date[TOMORROW])
        self.assertIn(self.ids['week'], by_date[WEEK])
        self.assertNotIn(self.ids['tomorrow'], by_date[TODAY])
        self.assertNotIn(self.ids['today'], by_date[TOMORROW])
        all_ids = [i for ids in by_date.values() for i in ids]
        self.assertNotIn(self.ids['none'], all_ids, 'без срока — не в «Предстоящих»')
        self.assertNotIn(self.ids['inbox'], all_ids, 'inbox без срока — не в «Предстоящих»')
        self.assertNotIn(self.ids['done_today'], all_ids, 'выполненная задача не показывается')
        self.assertEqual(data['overdue'], [], 'в день создания просроченных нет')

    def test_week_task_outside_7_days(self):
        data = self.upcoming('?days=7')
        all_ids = [i for ids in self.day_ids(data).values() for i in ids]
        self.assertNotIn(self.ids['week'], all_ids, 'срок today+7 — за пределами 7 дней (0..6)')
        self.assertIn(self.ids['today'], all_ids)

    def test_default_days_7(self):
        self.assertEqual(len(self.upcoming()['days']), 7)

    def test_days_validation(self):
        for q in ('?days=0', '?days=32', '?days=-1', '?days=abc', '?days=1.5', '?days='):
            with self.subTest(q=q):
                status, data = self.call('GET', '/api/upcoming' + q)
                self.assertEqual(status, 400, (q, data))
                self.assertIn('error', data)
        self.assertEqual(len(self.upcoming('?days=1')['days']), 1)
        self.assertEqual(len(self.upcoming('?days=31')['days']), 31)

    def test_project_fields_in_upcoming_tasks(self):
        data = self.upcoming('?days=8')
        week_task = [t for d in data['days'] for t in d['tasks'] if t['id'] == self.ids['week']][0]
        self.assertEqual((week_task['project'], week_task['project_name']), ('privat', 'Privat'))


class TestUpcomingOverdue(ServerTestCase):
    """Задача со сроком «сегодня» создаётся при FOKUS_TODAY=27.10, затем сервер
    перезапускается на той же БД с FOKUS_TODAY=28.10 → задача в overdue."""

    TODAY = TOMORROW

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SERVER_PY):
            raise unittest.SkipTest('server.py ещё нет')
        cls.tmpdir = tempfile.mkdtemp(prefix='fokus-v2-test-')
        cls.db_path = os.path.join(cls.tmpdir, 'fokus-test.db')
        first = cls.start_server(cls.db_path, TODAY)
        try:
            status, _, text = http_call(first.base, 'POST', '/api/tasks',
                                        body={'title': 'Срок вчера', 'answers': ANS_NOW})
            assert status == 201, (status, text)
            cls.overdue_id = parse_json(text)['task']['id']
            status, _, text = http_call(first.base, 'POST', '/api/tasks',
                                        body={'title': 'Срок сегодня (завтра для первого сервера)',
                                              'answers': ANS_TOMORROW})
            assert status == 201, (status, text)
            cls.tomorrow_id = parse_json(text)['task']['id']
        finally:
            first.stop()
        cls.server = cls.start_server(cls.db_path, cls.TODAY)
        cls.base = cls.server.base
        cls.port = cls.server.port

    def test_overdue(self):
        status, data = self.call('GET', '/api/upcoming?days=7')
        self.assertEqual(status, 200, data)
        self.assertEqual(data['from'], TOMORROW)
        self.assertEqual(data['days'][0]['date'], TOMORROW)
        overdue_ids = [t['id'] for t in data['overdue']]
        self.assertEqual(overdue_ids, [self.overdue_id])
        for t in data['overdue']:
            self.assertTaskShape(t)
            self.assertEqual(t['due_date'], TODAY)
            self.assertLess(t['days_left'], 0)
            self.assertEqual(t['status'], 'open')
        by_date = dict((d['date'], [t['id'] for t in d['tasks']]) for d in data['days'])
        self.assertIn(self.tomorrow_id, by_date[TOMORROW])
        self.assertNotIn(self.overdue_id, [i for ids in by_date.values() for i in ids])

    def test_done_overdue_not_listed(self):
        status, data = self.call('POST', '/api/tasks/%d/done' % self.overdue_id)
        self.assertEqual(status, 200, data)
        try:
            status, data = self.call('GET', '/api/upcoming')
            self.assertEqual(status, 200, data)
            self.assertNotIn(self.overdue_id, [t['id'] for t in data['overdue']])
        finally:
            self.call('POST', '/api/tasks/%d/reopen' % self.overdue_id)


# =====================================================================
# /api/shutdown
# =====================================================================

class TestShutdown(ServerTestCase):
    """Отдельный сервер: после POST /api/shutdown процесс должен завершиться сам."""

    @classmethod
    def tearDownClass(cls):
        # процесс, скорее всего, уже умер — stop() это переживает
        super(TestShutdown, cls).tearDownClass()

    def test_shutdown(self):
        t = self.create_task('Перед выключением', ANS_WEEK)
        status, data = self.call('POST', '/api/shutdown')
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {'ok': True})
        try:
            code = self.server.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail('сервер не завершился за 5 с после /api/shutdown:\n%s' % self.server.log_text())
        self.assertIsNotNone(code)
        status, _, _ = http_call(self.base, 'GET', '/api/health', timeout=2)
        self.assertIsNone(status, 'после shutdown сервер не должен отвечать')
        # данные сохранены: новый сервер на той же БД видит задачу
        second = self.start_server(self.db_path, TODAY)
        try:
            status, _, text = http_call(second.base, 'GET', '/api/tasks/%d' % t['id'])
            self.assertEqual(status, 200, text)
            self.assertEqual(parse_json(text)['task']['title'], 'Перед выключением')
        finally:
            second.stop()


if __name__ == '__main__':
    unittest.main()
