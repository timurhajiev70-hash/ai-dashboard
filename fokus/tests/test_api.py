# -*- coding: utf-8 -*-
"""Интеграционные тесты HTTP API Fokus (AGENTS.md §7).

Написаны ПО СПЕЦИФИКАЦИИ, не по коду. Каждый класс поднимает свой экземпляр
server.py с отдельной временной БД (FOKUS_DB), свободным портом (FOKUS_PORT)
и фиксированной датой FOKUS_TODAY=2026-10-25.

Запуск из папки fokus:  python3 -m unittest tests.test_api -v
Если server.py ещё не написан — все классы пропускаются (SkipTest).
"""
import collections
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
TODAY = '2026-10-25'
TOMORROW = '2026-10-26'
WEEK = '2026-11-01'
MONTH = '2026-11-24'

DEFAULT_WEIGHTS = {'importance': 0.45, 'urgency': 0.30, 'consequence': 0.20, 'duration': 0.05}
DEFAULT_THRESHOLDS = {'now': 65, 'priority': 45, 'queue': 25}
GROUPS = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')

# Поля объекта Task по §4 + days_left и hints (§7).
TASK_FIELDS = {
    'id', 'title', 'note', 'importance', 'due', 'due_date', 'consequence', 'who',
    'delegate_to', 'duration', 'depends_on', 'score', 'group', 'group_manual',
    'group_since', 'evaluated_at', 'needs_review', 'review_reason', 'is_today',
    'today_date', 'carry_count', 'status', 'done_at', 'created_at', 'updated_at',
    'days_left', 'hints',
}


def answers(importance, due, consequence, who='me', duration='30m',
            delegate_to=None, depends_on=None):
    """Собрать объект answers для мастера (§3)."""
    a = {'importance': importance, 'due': due, 'consequence': consequence,
         'who': who, 'duration': duration}
    if delegate_to is not None:
        a['delegate_to'] = delegate_to
    if depends_on is not None:
        a['depends_on'] = depends_on
    return a


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _drain(pipe, buf):
    """Читать поток процесса в кольцевой буфер, чтобы сервер не блокировался на PIPE."""
    try:
        for line in iter(pipe.readline, b''):
            buf.append(line.decode('utf-8', 'replace'))
    except Exception:
        pass


def http_call(base, method, path, body=None, headers=None, raw=None, timeout=10):
    """Выполнить HTTP-запрос. Возвращает (status, headers, text).

    4xx/5xx не бросают исключений. Ошибки соединения → (None, {}, текст ошибки).
    """
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
        hdrs_out = dict(e.headers) if e.headers is not None else {}
        return e.code, hdrs_out, payload.decode('utf-8', 'replace')
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
            socket.timeout, OSError) as e:
        return None, {}, 'connection error: %r' % (e,)


def parse_json(text):
    if text is None or text == '':
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def i18n_strings(data):
    """Ответ /api/i18n: либо словарь строк, либо {lang, strings}."""
    if isinstance(data, dict) and isinstance(data.get('strings'), dict):
        return data['strings']
    return data


class ServerTestCase(unittest.TestCase):
    """Базовый класс: один процесс server.py и одна временная БД на класс."""

    proc = None
    tmpdir = None
    base = None
    port = None

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SERVER_PY):
            raise unittest.SkipTest('server.py ещё нет')
        cls.port = free_port()
        cls.tmpdir = tempfile.mkdtemp(prefix='fokus-test-')
        cls.db_path = os.path.join(cls.tmpdir, 'fokus-test.db')
        env = dict(os.environ)
        env.update({
            'FOKUS_PORT': str(cls.port),
            'FOKUS_DB': cls.db_path,
            'FOKUS_TODAY': TODAY,
            'PYTHONUNBUFFERED': '1',
        })
        cls.base = 'http://127.0.0.1:%d' % cls.port
        cls.log = collections.deque(maxlen=500)
        cls.proc = subprocess.Popen(
            [sys.executable, 'server.py'], cwd=FOKUS_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls._threads = []
        for pipe in (cls.proc.stdout, cls.proc.stderr):
            t = threading.Thread(target=_drain, args=(pipe, cls.log))
            t.daemon = True
            t.start()
            cls._threads.append(t)
        deadline = time.time() + 10
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                cls._cleanup()
                raise RuntimeError('server.py завершился при старте (код %s):\n%s'
                                   % (cls.proc.returncode, cls._log_text()))
            status, _, text = http_call(cls.base, 'GET', '/api/health', timeout=2)
            if status == 200:
                break
            time.sleep(0.1)
        else:
            cls._cleanup()
            raise RuntimeError('server.py не ответил на /api/health за 10 с:\n%s'
                               % cls._log_text())

    @classmethod
    def _log_text(cls):
        return ''.join(cls.log)[-4000:]

    @classmethod
    def _cleanup(cls):
        if cls.proc is not None and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        for t in getattr(cls, '_threads', []):
            t.join(timeout=2)
        for pipe in (getattr(cls.proc, 'stdout', None), getattr(cls.proc, 'stderr', None)):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    # ---- хелперы --------------------------------------------------------

    @classmethod
    def call(cls, method, path, body=None, headers=None, raw=None):
        """-> (status, json). 4xx не бросает."""
        status, _, text = http_call(cls.base, method, path, body=body, headers=headers, raw=raw)
        return status, parse_json(text)

    @classmethod
    def call_full(cls, method, path, body=None, headers=None, raw=None):
        """-> (status, headers, text)."""
        return http_call(cls.base, method, path, body=body, headers=headers, raw=raw)

    def create(self, title, ans, note=None, expect=201):
        body = {'title': title, 'answers': ans}
        if note is not None:
            body['note'] = note
        status, data = self.call('POST', '/api/tasks', body)
        self.assertEqual(status, expect, 'POST /api/tasks %r -> %s %r' % (title, status, data))
        return data

    def create_task(self, title, ans, note=None):
        """Создать задачу, вернуть объект Task."""
        data = self.create(title, ans, note=note)
        self.assertIn('task', data)
        return data['task']

    def get_task(self, task_id):
        status, data = self.call('GET', '/api/tasks/%s' % task_id)
        self.assertEqual(status, 200, data)
        return data['task']

    def journal(self, task_id=None, limit=None):
        q = []
        if task_id is not None:
            q.append('task_id=%s' % task_id)
        if limit is not None:
            q.append('limit=%s' % limit)
        path = '/api/journal' + ('?' + '&'.join(q) if q else '')
        status, data = self.call('GET', path)
        self.assertEqual(status, 200, data)
        self.assertIn('entries', data)
        return data['entries']

    def events(self, task_id):
        return [e['event'] for e in self.journal(task_id)]

    def assertError(self, data, code, field=None):
        self.assertIsInstance(data, dict, 'ошибка должна быть JSON-объектом: %r' % (data,))
        self.assertEqual(data.get('error'), code, data)
        if field is not None:
            self.assertEqual(data.get('field'), field, data)

    def assertTaskShape(self, task):
        self.assertIsInstance(task, dict)
        missing = TASK_FIELDS - set(task)
        self.assertFalse(missing, 'в Task нет полей: %s' % sorted(missing))
        self.assertNotIn('grp', task, 'поле grp должно быть переименовано в group')
        self.assertIn(task['group'], GROUPS)
        self.assertIsInstance(task['hints'], list)
        self.assertIsInstance(task['score'], (int, float))
        self.assertTrue(0 <= task['score'] <= 100)
        self.assertAlmostEqual(task['score'], round(task['score'], 1), places=6,
                               msg='score должен иметь один знак после запятой')


# =====================================================================
# health / i18n / settings / безопасность
# =====================================================================

class TestHealthI18nSettingsSecurity(ServerTestCase):

    # ---- health ---------------------------------------------------------

    def test_health(self):
        status, headers, text = self.call_full('GET', '/api/health')
        self.assertEqual(status, 200)
        ctype = headers.get('Content-Type', headers.get('Content-type', ''))
        self.assertTrue(ctype.lower().startswith('application/json'), ctype)
        data = parse_json(text)
        self.assertIs(data.get('ok'), True)
        self.assertEqual(data.get('today'), TODAY)
        self.assertEqual(data.get('version'), '1.0')
        self.assertIn('db', data)
        self.assertIn(os.path.basename(self.db_path), data['db'])

    # ---- i18n -----------------------------------------------------------

    def test_i18n_ru_de(self):
        status, ru = self.call('GET', '/api/i18n?lang=ru')
        self.assertEqual(status, 200)
        ru = i18n_strings(ru)
        self.assertIsInstance(ru, dict)
        self.assertIn('nav_today', ru)
        status, de = self.call('GET', '/api/i18n?lang=de')
        self.assertEqual(status, 200)
        de = i18n_strings(de)
        self.assertIn('nav_today', de)
        self.assertEqual(set(ru), set(de), 'наборы ключей ru и de должны совпадать')
        for v in list(ru.values()) + list(de.values()):
            self.assertIsInstance(v, str)
            self.assertTrue(v.strip(), 'пустая строка в i18n')

    def test_i18n_unknown_lang_falls_back_to_ru(self):
        status, ru = self.call('GET', '/api/i18n?lang=ru')
        self.assertEqual(status, 200)
        status, xx = self.call('GET', '/api/i18n?lang=xx')
        self.assertEqual(status, 200, 'неизвестный язык не должен давать 4xx')
        self.assertEqual(i18n_strings(xx), i18n_strings(ru))
        status, nolang = self.call('GET', '/api/i18n')
        self.assertEqual(status, 200)
        self.assertEqual(set(i18n_strings(nolang)), set(i18n_strings(ru)))

    # ---- settings -------------------------------------------------------

    def test_settings_defaults(self):
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(status, 200)
        self.assertEqual(s['weights'], DEFAULT_WEIGHTS)
        self.assertEqual(s['thresholds'], DEFAULT_THRESHOLDS)
        self.assertEqual(s['day_limit'], 3)
        self.assertEqual(s['stale_days'], 7)
        self.assertEqual(s['carry_limit'], 3)
        self.assertEqual(s['language'], 'ru')
        self.assertIn('last_rollover', s)

    def test_settings_put_day_limit(self):
        status, before = self.call('GET', '/api/settings')
        self.assertEqual(status, 200)
        try:
            status, after = self.call('PUT', '/api/settings', {'day_limit': 2})
            self.assertEqual(status, 200, after)
            self.assertEqual(after['day_limit'], 2)
            b = dict(before)
            a = dict(after)
            b.pop('day_limit')
            a.pop('day_limit')
            self.assertEqual(a, b, 'PUT частичного объекта не должен менять остальные настройки')
            status, again = self.call('GET', '/api/settings')
            self.assertEqual(again['day_limit'], 2, 'настройка должна сохраняться')
        finally:
            self.call('PUT', '/api/settings', {'day_limit': 3})

    def test_settings_put_invalid_thresholds(self):
        status, data = self.call('PUT', '/api/settings', {'thresholds': {'now': 10}})
        self.assertEqual(status, 400, data)
        self.assertIsInstance(data, dict)
        self.assertIn('error', data)
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(s['thresholds'], DEFAULT_THRESHOLDS, 'невалидный PUT не должен ничего менять')

    def test_settings_put_invalid_values(self):
        bad = [
            {'language': 'en'},
            {'day_limit': 0},
            {'day_limit': 11},
            {'stale_days': 0},
            {'stale_days': 91},
            {'carry_limit': 0},
            {'carry_limit': 11},
            {'weights': {'importance': -1}},
            {'weights': {'importance': 0, 'urgency': 0, 'consequence': 0, 'duration': 0}},
            {'thresholds': {'now': 65, 'priority': 45, 'queue': 0}},
            {'thresholds': {'now': 65, 'priority': 65, 'queue': 25}},
        ]
        for patch in bad:
            with self.subTest(patch=patch):
                status, data = self.call('PUT', '/api/settings', patch)
                self.assertEqual(status, 400, (patch, data))
                self.assertIsInstance(data, dict)
                self.assertIn('error', data)
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(s['language'], 'ru')
        self.assertEqual(s['weights'], DEFAULT_WEIGHTS)

    def test_settings_put_language_de_ok(self):
        try:
            status, data = self.call('PUT', '/api/settings', {'language': 'de'})
            self.assertEqual(status, 200, data)
            self.assertEqual(data['language'], 'de')
        finally:
            self.call('PUT', '/api/settings', {'language': 'ru'})

    def test_settings_put_bad_json(self):
        status, data = self.call('PUT', '/api/settings', raw='xxx')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')

    # ---- безопасность ---------------------------------------------------

    def test_foreign_origin_forbidden(self):
        status, data = self.call('GET', '/api/health', headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403, data)
        status, data = self.call('POST', '/api/tasks/preview',
                                 {'answers': answers(5, 'today', 'serious')},
                                 headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403, data)

    def test_own_origin_allowed(self):
        for origin in ('http://127.0.0.1:%d' % self.port, 'http://localhost:%d' % self.port):
            with self.subTest(origin=origin):
                status, data = self.call('GET', '/api/health', headers={'Origin': origin})
                self.assertEqual(status, 200, data)

    def test_unknown_api_path_404(self):
        status, data = self.call('GET', '/api/nope')
        self.assertEqual(status, 404)
        self.assertError(data, 'not_found')
        status, data = self.call('POST', '/api/nope', {'x': 1})
        self.assertEqual(status, 404)
        self.assertError(data, 'not_found')

    def test_static_only_index(self):
        for path in ('/server.py', '/../server.py', '/db.py', '/static/index.html',
                     '/data/fokus.db', '/AGENTS.md'):
            with self.subTest(path=path):
                status, _, text = self.call_full('GET', path)
                self.assertEqual(status, 404, 'статика только / и /index.html: %s -> %s' % (path, status))
                self.assertNotIn('import ', text, 'исходник не должен отдаваться')

    def test_index_html(self):
        for path in ('/', '/index.html'):
            with self.subTest(path=path):
                status, headers, text = self.call_full('GET', path)
                self.assertEqual(status, 200)
                ctype = headers.get('Content-Type', headers.get('Content-type', ''))
                self.assertTrue(ctype.lower().startswith('text/html'), ctype)
                self.assertIn('<!DOCTYPE html>', text[:200])


# =====================================================================
# Задачи: создание, валидация, preview, CRUD, evaluate, move, фильтры
# =====================================================================

class TestTasks(ServerTestCase):

    # ---- создание -------------------------------------------------------

    def test_create_task_ok(self):
        status, data = self.call('POST', '/api/tasks', {
            'title': 'Позвонить в налоговую до конца дня',
            'answers': answers(5, 'today', 'serious', 'me', '30m')})
        self.assertEqual(status, 201, data)
        self.assertIn('task', data)
        self.assertIsInstance(data.get('hints'), list)
        t = data['task']
        self.assertTaskShape(t)
        self.assertIsInstance(t['id'], int)
        self.assertEqual(t['title'], 'Позвонить в налоговую до конца дня')
        self.assertEqual(t['note'], '')
        self.assertAlmostEqual(t['score'], 98.5, places=3)
        self.assertEqual(t['group'], 'now')
        self.assertEqual(t['due_date'], TODAY)
        self.assertEqual(t['days_left'], 0)
        self.assertEqual(t['status'], 'open')
        self.assertEqual(t['importance'], 5)
        self.assertEqual(t['due'], 'today')
        self.assertEqual(t['consequence'], 'serious')
        self.assertEqual(t['who'], 'me')
        self.assertEqual(t['duration'], '30m')
        self.assertEqual(t['delegate_to'], '')
        self.assertEqual(t['depends_on'], '')
        self.assertEqual(t['group_manual'], 0)
        self.assertEqual(t['needs_review'], 0)
        self.assertEqual(t['review_reason'], '')
        self.assertEqual(t['is_today'], 0)
        self.assertIsNone(t['today_date'])
        self.assertEqual(t['carry_count'], 0)
        self.assertIsNone(t['done_at'])
        self.assertTrue(t['created_at'].startswith(TODAY), t['created_at'])
        self.assertTrue(t['evaluated_at'].startswith(TODAY), t['evaluated_at'])
        self.assertEqual(self.events(t['id']), ['create'])

    def test_create_with_note_and_trim(self):
        t = self.create_task('   Задача с пробелами   ', answers(3, 'week', 'none'), note='заметка')
        self.assertEqual(t['title'], 'Задача с пробелами')
        self.assertEqual(t['note'], 'заметка')

    def test_create_title_boundary_200(self):
        t = self.create_task('ы' * 200, answers(3, 'week', 'none'))
        self.assertEqual(len(t['title']), 200)

    def test_create_validation_errors(self):
        good = answers(5, 'today', 'serious', 'me', '30m')
        cases = [
            ('нет title', {'answers': good}, 'title'),
            ('title 201 символ', {'title': 'a' * 201, 'answers': good}, 'title'),
            ('title из пробелов', {'title': '   ', 'answers': good}, 'title'),
            ('title пустой', {'title': '', 'answers': good}, 'title'),
            ('importance 7', {'title': 'x', 'answers': answers(7, 'today', 'serious')}, 'importance'),
            ('importance 0', {'title': 'x', 'answers': answers(0, 'today', 'serious')}, 'importance'),
            ('due yesterday', {'title': 'x', 'answers': answers(5, 'yesterday', 'serious')}, 'due'),
            ('consequence bad', {'title': 'x', 'answers': answers(5, 'today', 'fatal')}, 'consequence'),
            ('who bad', {'title': 'x', 'answers': answers(5, 'today', 'serious', who='robot')}, 'who'),
            ('duration bad', {'title': 'x', 'answers': answers(5, 'today', 'serious', duration='1y')}, 'duration'),
        ]
        for name, body, field in cases:
            with self.subTest(case=name):
                status, data = self.call('POST', '/api/tasks', body)
                self.assertEqual(status, 400, (name, data))
                self.assertError(data, 'validation', field)

    def test_create_without_answers_400(self):
        status, data = self.call('POST', '/api/tasks', {'title': 'без ответов'})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation')

    def test_create_bad_json(self):
        status, data = self.call('POST', '/api/tasks', raw='{title: нет кавычек')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')

    def test_create_body_too_large_413(self):
        big = json.dumps({'title': 'a' * 70000, 'answers': answers(5, 'today', 'serious')})
        self.assertGreater(len(big), 64 * 1024)
        status, data = self.call('POST', '/api/tasks', raw=big)
        self.assertEqual(status, 413, data)

    # ---- preview --------------------------------------------------------

    def test_preview_does_not_save(self):
        status, before = self.call('GET', '/api/tasks?status=all')
        self.assertEqual(status, 200)
        n_before = len(before['tasks'])
        status, data = self.call('POST', '/api/tasks/preview',
                                 {'answers': answers(4, 'week', 'unpleasant', 'me', '5m')})
        self.assertEqual(status, 200, data)
        self.assertAlmostEqual(data['score'], 66.8, places=3)
        self.assertEqual(data['group'], 'now')
        self.assertEqual(data['due_date'], WEEK)
        self.assertIn('do_now', data['hints'])
        status, after = self.call('GET', '/api/tasks?status=all')
        self.assertEqual(len(after['tasks']), n_before, 'preview не должен сохранять задачу')

    def test_preview_validation(self):
        status, data = self.call('POST', '/api/tasks/preview', {'answers': answers(9, 'week', 'none')})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'importance')

    # ---- get / patch / delete ---------------------------------------------

    def test_get_task(self):
        t = self.create_task('GET одной задачи', answers(2, 'none', 'none'))
        status, data = self.call('GET', '/api/tasks/%d' % t['id'])
        self.assertEqual(status, 200)
        self.assertEqual(data['task']['id'], t['id'])
        self.assertTaskShape(data['task'])

    def test_get_missing_task_404(self):
        status, data = self.call('GET', '/api/tasks/999999')
        self.assertEqual(status, 404)
        self.assertError(data, 'not_found')
        for path in ('/api/tasks/999999/done', '/api/tasks/999999/evaluate', '/api/tasks/999999/move'):
            with self.subTest(path=path):
                status, data = self.call('POST', path, {'group': 'now', 'answers': answers(3, 'week', 'none')})
                self.assertEqual(status, 404, data)
                self.assertError(data, 'not_found')
        status, data = self.call('PATCH', '/api/tasks/999999', {'title': 'x'})
        self.assertEqual(status, 404)
        status, data = self.call('DELETE', '/api/tasks/999999')
        self.assertEqual(status, 404)

    def test_patch_title_and_note(self):
        t = self.create_task('Старое название', answers(3, 'week', 'none'))
        status, data = self.call('PATCH', '/api/tasks/%d' % t['id'], {'title': 'Новое название'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['title'], 'Новое название')
        self.assertAlmostEqual(data['task']['score'], t['score'], places=3, msg='PATCH не меняет балл')
        status, data = self.call('PATCH', '/api/tasks/%d' % t['id'], {'note': 'заметка'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['note'], 'заметка')
        self.assertEqual(data['task']['title'], 'Новое название')
        edits = [e for e in self.journal(t['id']) if e['event'] == 'edit']
        self.assertEqual(len(edits), 2, 'два PATCH -> две записи edit')
        pairs = [(e['old_value'], e['new_value']) for e in edits]
        self.assertIn(('Старое название', 'Новое название'), pairs, pairs)

    def test_patch_invalid_title(self):
        t = self.create_task('Для PATCH', answers(3, 'week', 'none'))
        for bad in ('', '   ', 'x' * 201):
            with self.subTest(title=bad):
                status, data = self.call('PATCH', '/api/tasks/%d' % t['id'], {'title': bad})
                self.assertEqual(status, 400, data)
                self.assertError(data, 'validation', 'title')
        self.assertEqual(self.get_task(t['id'])['title'], 'Для PATCH')

    def test_delete_soft(self):
        t = self.create_task('Удалить меня', answers(3, 'week', 'none'))
        status, data = self.call('DELETE', '/api/tasks/%d' % t['id'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['status'], 'deleted')
        for q in ('?status=all', '?status=open', '?status=done', ''):
            with self.subTest(query=q):
                status, data = self.call('GET', '/api/tasks' + q)
                self.assertEqual(status, 200)
                self.assertNotIn(t['id'], [x['id'] for x in data['tasks']])
        ev = self.journal(t['id'])
        self.assertIn('delete', [e['event'] for e in ev])
        d = [e for e in ev if e['event'] == 'delete'][0]
        self.assertEqual(d['new_value'], 'deleted')
        status, board = self.call('GET', '/api/board')
        ids = [x['id'] for g in board['groups'].values() for x in g['tasks']]
        self.assertNotIn(t['id'], ids, 'удалённая задача не на доске')

    # ---- done / reopen --------------------------------------------------

    def test_done_and_reopen(self):
        t = self.create_task('Сделать и вернуть', answers(3, 'week', 'none'))
        status, data = self.call('POST', '/api/tasks/%d/done' % t['id'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['status'], 'done')
        self.assertIsNotNone(data['task']['done_at'])
        status, lst = self.call('GET', '/api/tasks')
        self.assertNotIn(t['id'], [x['id'] for x in lst['tasks']], 'done не в списке open по умолчанию')
        status, lst = self.call('GET', '/api/tasks?status=done')
        self.assertIn(t['id'], [x['id'] for x in lst['tasks']])
        status, data = self.call('POST', '/api/tasks/%d/reopen' % t['id'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['status'], 'open')
        self.assertIsNone(data['task']['done_at'])
        ev = self.events(t['id'])
        self.assertIn('done', ev)
        self.assertIn('reopen', ev)

    # ---- evaluate -------------------------------------------------------

    def test_evaluate_recomputes_and_resets(self):
        t = self.create_task('Повторный мастер', answers(5, 'today', 'serious', 'me', '30m'))
        self.assertEqual(t['group'], 'now')
        status, data = self.call('POST', '/api/tasks/%d/move' % t['id'], {'group': 'someday'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['group_manual'], 1)
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % t['id'],
                                 {'answers': answers(3, 'month', 'none', 'me', '30m')})
        self.assertEqual(status, 200, data)
        self.assertIn('task', data)
        self.assertIsInstance(data.get('hints'), list)
        u = data['task']
        self.assertTaskShape(u)
        self.assertAlmostEqual(u['score'], 35.0, places=3)
        self.assertEqual(u['group'], 'queue')
        self.assertEqual(u['due_date'], MONTH)
        self.assertEqual(u['days_left'], 30)
        self.assertEqual(u['importance'], 3)
        self.assertEqual(u['due'], 'month')
        self.assertEqual(u['consequence'], 'none')
        self.assertEqual(u['group_manual'], 0)
        self.assertEqual(u['carry_count'], 0)
        self.assertEqual(u['needs_review'], 0)
        self.assertEqual(u['review_reason'], '')
        self.assertEqual(u['title'], 'Повторный мастер', 'evaluate не меняет название')
        ev = self.journal(t['id'])
        self.assertIn('evaluate', [e['event'] for e in ev])
        e = [x for x in ev if x['event'] == 'evaluate'][0]
        self.assertIn('someday', e['old_value'])
        self.assertIn('queue', e['new_value'])
        self.assertIn('35.0', e['new_value'])

    def test_evaluate_validation(self):
        t = self.create_task('Мастер с ошибкой', answers(3, 'week', 'none'))
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % t['id'],
                                 {'answers': answers(3, 'never', 'none')})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'validation', 'due')
        self.assertEqual(self.get_task(t['id'])['due'], 'week')

    # ---- move -----------------------------------------------------------

    def test_move_manual(self):
        t = self.create_task('Перенос вручную', answers(5, 'today', 'serious', 'me', '30m'))
        status, data = self.call('POST', '/api/tasks/%d/move' % t['id'], {'group': 'someday'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['group'], 'someday')
        self.assertEqual(data['task']['group_manual'], 1)
        self.assertAlmostEqual(data['task']['score'], 98.5, places=3, msg='балл при переносе не меняется')
        ev = self.journal(t['id'])
        mm = [e for e in ev if e['event'] == 'manual_move']
        self.assertEqual(len(mm), 1)
        self.assertEqual(mm[0]['old_value'], 'now')
        self.assertEqual(mm[0]['new_value'], 'someday')
        status, lst = self.call('GET', '/api/tasks?group=someday')
        self.assertIn(t['id'], [x['id'] for x in lst['tasks']])

    def test_move_unknown_group_400(self):
        t = self.create_task('Перенос в никуда', answers(3, 'week', 'none'))
        for bad in ('xxx', '', 'NOW', None):
            with self.subTest(group=bad):
                status, data = self.call('POST', '/api/tasks/%d/move' % t['id'], {'group': bad})
                self.assertEqual(status, 400, data)
                self.assertIsInstance(data, dict)
                self.assertIn('error', data)
        self.assertEqual(self.get_task(t['id'])['group'], 'queue')

    def test_move_out_of_now_removes_from_today(self):
        t = self.create_task('Задача дня, уходит из now', answers(5, 'today', 'serious', 'me', '30m'))
        status, data = self.call('POST', '/api/today', {'task_id': t['id']})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.get_task(t['id'])['is_today'], 1)
        status, data = self.call('POST', '/api/tasks/%d/move' % t['id'], {'group': 'queue'})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['group'], 'queue')
        self.assertEqual(data['task']['is_today'], 0)
        self.assertIsNone(data['task']['today_date'])
        status, today = self.call('GET', '/api/today')
        self.assertEqual(status, 200)
        self.assertNotIn(t['id'], [x['id'] for x in today['tasks']])
        ev = self.journal(t['id'])
        tr = [e for e in ev if e['event'] == 'today_remove']
        self.assertEqual(len(tr), 1)
        self.assertEqual(tr[0]['note'], 'moved')

    # ---- список и фильтры -----------------------------------------------

    def test_list_filters_and_sort(self):
        p = 'F%d ' % int(time.time() * 1000)
        f_now = self.create_task(p + 'now', answers(5, 'today', 'serious', 'me', '30m'))
        f_del = self.create_task(p + 'delegate', answers(3, 'week', 'unpleasant', 'delegate', '30m', delegate_to='Света'))
        f_5m = self.create_task(p + '5m', answers(4, 'week', 'unpleasant', 'me', '5m'))
        f_none = self.create_task(p + 'none', answers(5, 'none', 'none', 'me', 'half_day'))
        f_done = self.create_task(p + 'done', answers(3, 'month', 'none', 'me', '30m'))
        f_wait = self.create_task(p + 'waiting', answers(5, 'week', 'serious', 'me', '30m', depends_on='справка'))
        status, _ = self.call('POST', '/api/tasks/%d/done' % f_done['id'])
        self.assertEqual(status, 200)

        def ids(query):
            status, data = self.call('GET', '/api/tasks' + query)
            self.assertEqual(status, 200, (query, data))
            self.assertIn('tasks', data)
            return data['tasks']

        tasks = ids('?group=now')
        self.assertTrue(all(t['group'] == 'now' for t in tasks))
        got = [t['id'] for t in tasks]
        self.assertIn(f_now['id'], got)
        self.assertIn(f_5m['id'], got)
        self.assertNotIn(f_del['id'], got)

        tasks = ids('?who=delegate')
        self.assertTrue(all(t['who'] == 'delegate' for t in tasks))
        self.assertIn(f_del['id'], [t['id'] for t in tasks])
        self.assertEqual([t for t in tasks if t['id'] == f_del['id']][0]['delegate_to'], 'Света')

        tasks = ids('?duration=5m')
        self.assertTrue(all(t['duration'] == '5m' for t in tasks))
        self.assertIn(f_5m['id'], [t['id'] for t in tasks])

        tasks = ids('?due=none')
        self.assertTrue(all(t['due'] == 'none' and t['due_date'] is None for t in tasks))
        self.assertIn(f_none['id'], [t['id'] for t in tasks])

        tasks = ids('?group=waiting')
        self.assertIn(f_wait['id'], [t['id'] for t in tasks])

        tasks = ids('?status=done')
        self.assertTrue(all(t['status'] == 'done' for t in tasks))
        self.assertIn(f_done['id'], [t['id'] for t in tasks])

        tasks = ids('')
        self.assertTrue(all(t['status'] == 'open' for t in tasks))
        self.assertNotIn(f_done['id'], [t['id'] for t in tasks])

        tasks = ids('?status=all')
        got = [t['id'] for t in tasks]
        self.assertIn(f_done['id'], got)
        self.assertIn(f_now['id'], got)

        tasks = ids('?group=now&who=me&duration=5m')
        self.assertTrue(all(t['group'] == 'now' and t['who'] == 'me' and t['duration'] == '5m' for t in tasks))
        self.assertIn(f_5m['id'], [t['id'] for t in tasks])

        # сортировки
        tasks = ids('?sort=score')
        scores = [t['score'] for t in tasks]
        self.assertEqual(scores, sorted(scores, reverse=True))

        tasks = ids('?sort=due')
        dates = [t['due_date'] for t in tasks]
        with_date = [d for d in dates if d is not None]
        self.assertEqual(with_date, sorted(with_date), 'sort=due: даты по возрастанию')
        if None in dates:
            first_none = dates.index(None)
            self.assertTrue(all(d is None for d in dates[first_none:]), 'sort=due: без даты — в конце')
        self.assertTrue(with_date, 'ожидались задачи с датой')
        self.assertIn(None, dates, 'ожидались задачи без даты')

        tasks = ids('?sort=created')
        created = [t['created_at'] for t in tasks]
        self.assertEqual(created, sorted(created, reverse=True), 'sort=created: новые сверху')

        tasks = ids('?needs_review=1')
        self.assertTrue(all(t['needs_review'] == 1 for t in tasks))

    # ---- board ----------------------------------------------------------

    def test_board(self):
        self.create_task('Доска now', answers(5, 'today', 'serious', 'me', '30m'))
        self.create_task('Доска delegate', answers(3, 'week', 'unpleasant', 'delegate', '30m', delegate_to='Мир'))
        status, data = self.call('GET', '/api/board')
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data['groups']), set(GROUPS))
        for g, body in data['groups'].items():
            with self.subTest(group=g):
                self.assertIn('count', body)
                self.assertIn('tasks', body)
                self.assertEqual(body['count'], len(body['tasks']))
                for t in body['tasks']:
                    self.assertEqual(t['group'], g)
                    self.assertEqual(t['status'], 'open')
        self.assertIsInstance(data['review'], list)
        self.assertGreaterEqual(data['groups']['now']['count'], 1)
        self.assertGreaterEqual(data['groups']['delegate']['count'], 1)
        for sort in ('score', 'due', 'created'):
            status, d2 = self.call('GET', '/api/board?sort=%s' % sort)
            self.assertEqual(status, 200, d2)
            self.assertEqual(set(d2['groups']), set(GROUPS))
        scores = [t['score'] for t in data['groups']['now']['tasks']]
        self.assertEqual(scores, sorted(scores, reverse=True))

    # ---- journal --------------------------------------------------------

    def test_journal(self):
        t = self.create_task('Журнал задачи', answers(3, 'week', 'none'))
        self.call('PATCH', '/api/tasks/%d' % t['id'], {'title': 'Журнал задачи 2'})
        self.call('POST', '/api/tasks/%d/done' % t['id'])
        entries = self.journal(t['id'])
        self.assertGreaterEqual(len(entries), 3)
        for e in entries:
            self.assertEqual(e['task_id'], t['id'])
            self.assertIn('title', e)
            self.assertIn('event', e)
            self.assertIn('ts', e)
            self.assertIn('old_value', e)
            self.assertIn('new_value', e)
            self.assertIn('note', e)
            self.assertEqual(e['title'], 'Журнал задачи 2', 'title — текущий заголовок из JOIN')
        self.assertEqual(set(e['event'] for e in entries) & {'create', 'edit', 'done'}, {'create', 'edit', 'done'})
        one = self.journal(t['id'], limit=1)
        self.assertEqual(len(one), 1)
        status, data = self.call('GET', '/api/journal')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data['entries']), 3)
        status, data = self.call('GET', '/api/journal?limit=2')
        self.assertEqual(len(data['entries']), 2)

    # ---- XSS / странный ввод ---------------------------------------------

    def test_titles_stored_verbatim(self):
        titles = [
            '<script>alert(1)</script>',
            'пациент tomedo #48213',
            'Эмодзи 🔴🟠 и кириллица — ёЁ «кавычки» & <b>',
            '"quotes" \\ backslash / slash',
            'Umlaute äöü ß',
        ]
        for title in titles:
            with self.subTest(title=title):
                t = self.create_task(title, answers(3, 'week', 'none'))
                self.assertEqual(t['title'], title)
                self.assertEqual(self.get_task(t['id'])['title'], title)
                entries = self.journal(t['id'])
                self.assertEqual(entries[0]['title'], title)

    def test_delegate_to_and_depends_on_text(self):
        t = self.create_task('Поручить', answers(3, 'week', 'unpleasant', 'delegate', '30m', delegate_to='  Света  '))
        self.assertEqual(t['group'], 'delegate')
        self.assertEqual(t['delegate_to'], 'Света')
        self.assertIn('delegate', t['hints'])
        t2 = self.create_task('Поручить без имени', answers(3, 'week', 'unpleasant', 'delegate', '30m'))
        self.assertEqual(t2['group'], 'delegate')
        self.assertEqual(t2['delegate_to'], '')
        t3 = self.create_task('Ждёт', answers(5, 'week', 'serious', 'me', '30m', depends_on='справка из банка'))
        self.assertEqual(t3['group'], 'waiting')
        self.assertEqual(t3['depends_on'], 'справка из банка')
        self.assertIn('waiting', t3['hints'])
        t4 = self.create_task('Не ждёт', answers(5, 'week', 'serious', 'me', '30m', depends_on=None))
        self.assertEqual(t4['group'], 'now')
        t5 = self.create_task('Не ждёт 2', answers(5, 'week', 'serious', 'me', '30m', depends_on='   '))
        self.assertEqual(t5['group'], 'now', 'пустая/пробельная зависимость = не зависит')
        self.assertEqual(t5['depends_on'], '')


# =====================================================================
# Эталон §5.4 — 15 задач через API
# =====================================================================

# (title, imp, due, cons, who, dur, delegate_to, depends_on, score, group, due_date, days_left, обязательные hints)
REFERENCE = [
    ('Позвонить в налоговую до конца дня', 5, 'today', 'serious', 'me', '30m', None, None, 98.5, 'now', TODAY, 0, ()),
    ('Отправить отчёт по пациенту tomedo #48213', 4, 'today', 'serious', 'me', '30m', None, None, 87.3, 'now', TODAY, 0, ()),
    ('Записаться к зубному', 4, 'week', 'unpleasant', 'me', '5m', None, None, 66.8, 'now', WEEK, 7, ('do_now',)),
    ('Подготовить документы к практике', 3, 'today', 'unpleasant', 'me', '30m', None, None, 66.0, 'now', TODAY, 0, ()),
    ('Купить подарок к дню рождения через месяц', 4, 'month', 'unpleasant', 'me', '2h', None, None, 54.8, 'priority', MONTH, 30, ()),
    ('Оформить страховку (срока нет, критично)', 5, 'none', 'none', 'me', 'half_day', None, None, 45.0, 'priority', None, None, ()),
    ('Разобрать фотоархив', 2, 'none', 'none', 'me', '2h', None, None, 13.3, 'someday', None, None, ('someday_review',)),
    ('Почитать статью про ИИ', 2, 'week', 'none', 'me', '30m', None, None, 32.8, 'queue', WEEK, 7, ()),
    ('Прочитать договор, средняя важность, в этом месяце', 3, 'month', 'none', 'me', '30m', None, None, 35.0, 'queue', MONTH, 30, ()),
    ('Заказать канцтовары (поручить Свете)', 3, 'week', 'unpleasant', 'delegate', '30m', 'Света', None, 54.0, 'delegate', WEEK, 7, ('delegate',)),
    ('Проверить сайт после обновления (поручить Миру)', 2, 'none', 'none', 'delegate', '5m', 'Мир', None, 16.3, 'delegate', None, None, ('delegate',)),
    ('Подать заявку — ждёт справку из банка', 5, 'week', 'serious', 'me', '30m', None, 'справка из банка', 86.5, 'waiting', WEEK, 7, ('waiting',)),
    ('Купить лампочку', 1, 'today', 'none', 'me', '5m', None, None, 35.0, 'queue', TODAY, 0, ()),
    ('Написать благодарность коллеге', 2, 'none', 'unpleasant', 'me', '30m', None, None, 24.8, 'someday', None, None, ('someday_review',)),
    ('Критично, завтра, последствий нет, полдня', 5, 'tomorrow', 'none', 'me', 'half_day', None, None, 69.0, 'now', TOMORROW, 1, ()),
]


class TestReferenceTable(ServerTestCase):

    def test_reference_rows(self):
        self.assertEqual(len(REFERENCE), 15)
        for row in REFERENCE:
            (title, imp, due, cons, who, dur, delegate_to, depends_on,
             score, group, due_date, days_left, must_hints) = row
            with self.subTest(title=title):
                data = self.create(title, answers(imp, due, cons, who, dur,
                                                  delegate_to=delegate_to, depends_on=depends_on))
                t = data['task']
                self.assertTaskShape(t)
                self.assertAlmostEqual(t['score'], score, places=3,
                                       msg='%s: score %s != %s' % (title, t['score'], score))
                self.assertEqual(t['group'], group, '%s: group %s != %s' % (title, t['group'], group))
                self.assertEqual(t['due_date'], due_date)
                self.assertEqual(t['days_left'], days_left)
                for h in must_hints:
                    self.assertIn(h, data['hints'], '%s: нет подсказки %s' % (title, h))
                    self.assertIn(h, t['hints'])
                # preview для тех же ответов даёт тот же результат
                status, pv = self.call('POST', '/api/tasks/preview', {
                    'answers': answers(imp, due, cons, who, dur, delegate_to=delegate_to, depends_on=depends_on)})
                self.assertEqual(status, 200, pv)
                self.assertAlmostEqual(pv['score'], score, places=3)
                self.assertEqual(pv['group'], group)
                self.assertEqual(pv['due_date'], due_date)

    def test_extremes(self):
        t = self.create_task('Максимум', answers(5, 'today', 'serious', 'me', '5m'))
        self.assertAlmostEqual(t['score'], 100.0, places=3)
        self.assertEqual(t['group'], 'now')
        self.assertIn('do_now', t['hints'])
        t = self.create_task('Минимум', answers(1, 'none', 'none', 'me', 'half_day'))
        self.assertAlmostEqual(t['score'], 0.0, places=3)
        self.assertEqual(t['group'], 'someday')
        self.assertIn('someday_review', t['hints'])

    def test_do_now_hint_needs_importance_3(self):
        t = self.create_task('5 минут, важность 2', answers(2, 'week', 'none', 'me', '5m'))
        self.assertNotIn('do_now', t['hints'])
        t = self.create_task('5 минут, важность 3', answers(3, 'week', 'none', 'me', '5m'))
        self.assertIn('do_now', t['hints'])

    def test_delegate_beats_waiting(self):
        t = self.create_task('Поручить, но есть зависимость',
                             answers(5, 'today', 'serious', 'delegate', '30m',
                                     delegate_to='Света', depends_on='ответ банка'))
        self.assertEqual(t['group'], 'delegate', '§9.4: delegate сильнее waiting')
        self.assertIn('delegate', t['hints'])

    def test_counts_by_group_on_board(self):
        """После эталона на доске count по группам равен числу задач в списке."""
        status, board = self.call('GET', '/api/board')
        self.assertEqual(status, 200)
        for g in GROUPS:
            status, lst = self.call('GET', '/api/tasks?group=%s' % g)
            self.assertEqual(status, 200)
            self.assertEqual(board['groups'][g]['count'], len(lst['tasks']), g)


# =====================================================================
# «Сегодня»: лимит 3, кандидаты, замена, ошибки
# =====================================================================

class TestToday(ServerTestCase):
    """Сценарий с состоянием — методы пронумерованы и выполняются по порядку."""

    @classmethod
    def setUpClass(cls):
        super(TestToday, cls).setUpClass()
        cls.ids = {}   # name -> id
        cls.scores = {}

    def _create(self, name, ans):
        data = self.create(name, ans)
        self.ids[name] = data['task']['id']
        self.scores[name] = data['task']['score']
        return data['task']

    def today(self):
        status, data = self.call('GET', '/api/today')
        self.assertEqual(status, 200, data)
        for k in ('date', 'limit', 'tasks', 'candidates', 'pool', 'free_slots'):
            self.assertIn(k, data, 'в /api/today нет поля %s' % k)
        return data

    def test_01_create_now_tasks(self):
        self._create('n100', answers(5, 'today', 'serious', 'me', '5m'))       # 100.0
        self._create('n98', answers(5, 'today', 'serious', 'me', '30m'))       # 98.5
        self._create('n87', answers(4, 'today', 'serious', 'me', '30m'))       # 87.3
        self._create('n69', answers(5, 'tomorrow', 'none', 'me', 'half_day'))  # 69.0
        self._create('n66', answers(3, 'today', 'unpleasant', 'me', '30m'))    # 66.0
        self._create('queue', answers(3, 'month', 'none', 'me', '30m'))        # 35.0 queue
        self._create('done_now', answers(4, 'today', 'serious', 'me', '5m'))   # now, будет done
        for name in ('n100', 'n98', 'n87', 'n69', 'n66', 'done_now'):
            self.assertEqual(self.get_task(self.ids[name])['group'], 'now', name)
        self.assertEqual(self.get_task(self.ids['queue'])['group'], 'queue')
        self.assertEqual(len(set(self.scores[n] for n in ('n100', 'n98', 'n87', 'n69', 'n66'))), 5,
                         'баллы пяти задач должны различаться')
        status, data = self.call('POST', '/api/tasks/%d/done' % self.ids['done_now'])
        self.assertEqual(status, 200, data)

    def test_02_empty_day(self):
        d = self.today()
        self.assertEqual(d['date'], TODAY)
        self.assertEqual(d['limit'], 3)
        self.assertEqual(d['tasks'], [])
        self.assertEqual(d['free_slots'], 3)
        cands = d['candidates']
        self.assertTrue(1 <= len(cands) <= 3, 'кандидатов не больше limit - 0 = 3: %d' % len(cands))
        scores = [c['score'] for c in cands]
        self.assertEqual(scores, sorted(scores, reverse=True), 'кандидаты по score ↓')
        self.assertAlmostEqual(cands[0]['score'], 100.0, places=3)
        self.assertEqual(cands[0]['id'], self.ids['n100'])
        for c in cands:
            self.assertEqual(c['group'], 'now')
            self.assertEqual(c['status'], 'open')
            self.assertEqual(c['is_today'], 0)
        # pool — все открытые now вне дня
        pool_ids = set(p['id'] for p in d['pool'])
        self.assertEqual(pool_ids, set(self.ids[n] for n in ('n100', 'n98', 'n87', 'n69', 'n66')))
        self.assertNotIn(self.ids['done_now'], pool_ids, 'сделанная задача не в pool')
        self.assertNotIn(self.ids['queue'], pool_ids)

    def test_03_not_now_group(self):
        status, data = self.call('POST', '/api/today', {'task_id': self.ids['queue']})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'not_now_group')
        self.assertEqual(self.today()['tasks'], [])

    def test_04_not_open(self):
        status, data = self.call('POST', '/api/today', {'task_id': self.ids['done_now']})
        self.assertEqual(status, 400, data)
        self.assertError(data, 'not_open')
        self.assertEqual(self.today()['tasks'], [])

    def test_05_bad_requests(self):
        status, data = self.call('POST', '/api/today', {'task_id': 999999})
        self.assertEqual(status, 404, data)
        self.assertError(data, 'not_found')
        status, data = self.call('POST', '/api/today', {})
        self.assertEqual(status, 400, data)
        self.assertIn('error', data)
        status, data = self.call('POST', '/api/today', raw='не json')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')
        self.assertEqual(self.today()['tasks'], [])

    def test_06_add_three(self):
        for i, name in enumerate(('n66', 'n69', 'n87')):
            status, data = self.call('POST', '/api/today', {'task_id': self.ids[name]})
            self.assertEqual(status, 200, (name, data))
            self.assertIn('tasks', data)
            self.assertIn('free_slots', data)
            self.assertEqual(len(data['tasks']), i + 1)
            self.assertEqual(data['free_slots'], 3 - (i + 1))
            self.assertIn(self.ids[name], [t['id'] for t in data['tasks']])
            t = self.get_task(self.ids[name])
            self.assertEqual(t['is_today'], 1)
            self.assertEqual(t['today_date'], TODAY)
        d = self.today()
        self.assertEqual(len(d['tasks']), 3)
        self.assertEqual(d['free_slots'], 0)
        self.assertEqual(d['candidates'], [], 'при полном дне candidates пусто')
        pool_ids = set(p['id'] for p in d['pool'])
        self.assertEqual(pool_ids, set(self.ids[n] for n in ('n100', 'n98')), 'pool = now вне дня')
        ev = self.journal(self.ids['n66'])
        ta = [e for e in ev if e['event'] == 'today_add']
        self.assertEqual(len(ta), 1)
        self.assertEqual(ta[0]['new_value'], TODAY)

    def test_07_fourth_is_day_full(self):
        status, data = self.call('POST', '/api/today', {'task_id': self.ids['n100']})
        self.assertEqual(status, 409, data)
        self.assertError(data, 'day_full')
        self.assertEqual(data['limit'], 3)
        self.assertEqual(len(data['tasks']), 3)
        self.assertNotIn(self.ids['n100'], [t['id'] for t in data['tasks']])
        self.assertEqual(self.get_task(self.ids['n100'])['is_today'], 0)

    def test_08_replace(self):
        status, data = self.call('POST', '/api/today',
                                 {'task_id': self.ids['n100'], 'replace_id': self.ids['n66']})
        self.assertEqual(status, 200, data)
        got = [t['id'] for t in data['tasks']]
        self.assertEqual(len(got), 3)
        self.assertIn(self.ids['n100'], got)
        self.assertNotIn(self.ids['n66'], got)
        self.assertEqual(data['free_slots'], 0)
        old = self.get_task(self.ids['n66'])
        self.assertEqual(old['is_today'], 0)
        self.assertIsNone(old['today_date'])
        self.assertEqual(old['status'], 'open', 'замена не меняет статус')
        new = self.get_task(self.ids['n100'])
        self.assertEqual(new['is_today'], 1)
        self.assertEqual(new['today_date'], TODAY)
        ev_new = self.journal(self.ids['n100'])
        self.assertIn('replace', [e['note'] for e in ev_new if e['event'] == 'today_add'])
        ev_old = self.journal(self.ids['n66'])
        self.assertIn('today_remove', [e['event'] for e in ev_old])
        d = self.today()
        self.assertEqual(set(t['id'] for t in d['tasks']), set(got))
        self.assertIn(self.ids['n66'], [p['id'] for p in d['pool']])

    def test_09_replace_with_bad_ids(self):
        # replace_id не из «Сегодня»
        status, data = self.call('POST', '/api/today',
                                 {'task_id': self.ids['n98'], 'replace_id': self.ids['n66']})
        self.assertIn(status, (400, 404, 409), data)
        self.assertIn('error', data)
        self.assertEqual(len(self.today()['tasks']), 3)
        self.assertEqual(self.get_task(self.ids['n98'])['is_today'], 0)

    def test_10_remove_from_today(self):
        status, data = self.call('DELETE', '/api/today/%d' % self.ids['n69'])
        self.assertEqual(status, 200, data)
        self.assertNotIn(self.ids['n69'], [t['id'] for t in data['tasks']])
        self.assertEqual(len(data['tasks']), 2)
        self.assertEqual(data['free_slots'], 1)
        t = self.get_task(self.ids['n69'])
        self.assertEqual(t['is_today'], 0)
        self.assertIsNone(t['today_date'])
        self.assertIn('today_remove', self.events(self.ids['n69']))
        d = self.today()
        self.assertEqual(d['free_slots'], 1)
        self.assertEqual(len(d['candidates']), 1, 'limit - 2 = 1 кандидат')
        self.assertEqual(d['candidates'][0]['id'], self.ids['n98'], 'лучший из оставшихся по score')
        status, data = self.call('DELETE', '/api/today/999999')
        self.assertEqual(status, 404, data)

    def test_11_day_limit_one(self):
        # оставить в дне одну задачу
        d = self.today()
        for t in d['tasks'][1:]:
            status, data = self.call('DELETE', '/api/today/%d' % t['id'])
            self.assertEqual(status, 200, data)
        d = self.today()
        self.assertEqual(len(d['tasks']), 1)
        keep = d['tasks'][0]['id']
        try:
            status, s = self.call('PUT', '/api/settings', {'day_limit': 1})
            self.assertEqual(status, 200, s)
            d = self.today()
            self.assertEqual(d['limit'], 1)
            self.assertEqual(d['free_slots'], 0)
            self.assertEqual(d['candidates'], [])
            other = [self.ids[n] for n in ('n100', 'n98', 'n87', 'n69', 'n66') if self.ids[n] != keep][0]
            status, data = self.call('POST', '/api/today', {'task_id': other})
            self.assertEqual(status, 409, data)
            self.assertError(data, 'day_full')
            self.assertEqual(data['limit'], 1)
            self.assertEqual([t['id'] for t in data['tasks']], [keep])
            # замена при лимите 1 работает
            status, data = self.call('POST', '/api/today', {'task_id': other, 'replace_id': keep})
            self.assertEqual(status, 200, data)
            self.assertEqual([t['id'] for t in data['tasks']], [other])
        finally:
            self.call('PUT', '/api/settings', {'day_limit': 3})
        d = self.today()
        self.assertEqual(d['limit'], 3)
        self.assertEqual(d['free_slots'], 2)


class TestTodayDoneAndDoubleClick(ServerTestCase):

    def test_done_task_stays_in_today_and_counts(self):
        a = self.create_task('День: сделаю', answers(5, 'today', 'serious', 'me', '30m'))
        b = self.create_task('День: вторая', answers(4, 'today', 'serious', 'me', '30m'))
        status, data = self.call('POST', '/api/today', {'task_id': a['id']})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['free_slots'], 2)
        status, data = self.call('POST', '/api/tasks/%d/done' % a['id'])
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['status'], 'done')
        self.assertEqual(data['task']['is_today'], 1, 'сделанная задача дня остаётся в дне')
        status, d = self.call('GET', '/api/today')
        self.assertEqual(status, 200)
        mine = [t for t in d['tasks'] if t['id'] == a['id']]
        self.assertEqual(len(mine), 1, 'сделанная задача видна в «Сегодня» до конца дня')
        self.assertEqual(mine[0]['status'], 'done')
        self.assertEqual(d['free_slots'], 2, 'сделанные считаются в лимите')
        self.assertNotIn(a['id'], [c['id'] for c in d['candidates']])
        self.assertNotIn(a['id'], [p['id'] for p in d['pool']])
        self.assertIn(b['id'], [c['id'] for c in d['candidates']])
        status, data = self.call('POST', '/api/today', {'task_id': b['id']})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['free_slots'], 1)
        self.assertEqual(len(data['tasks']), 2)

    def test_double_click_add_is_safe(self):
        t = self.create_task('Двойной клик', answers(5, 'today', 'serious', 'me', '5m'))
        status1, d1 = self.call('POST', '/api/today', {'task_id': t['id']})
        self.assertEqual(status1, 200, d1)
        status2, d2 = self.call('POST', '/api/today', {'task_id': t['id']})
        self.assertTrue(status2 == 200 or 400 <= status2 < 500,
                        'повторное добавление: 200 идемпотентно или 4xx, получено %s %r' % (status2, d2))
        status, d = self.call('GET', '/api/today')
        self.assertEqual(status, 200)
        ids = [x['id'] for x in d['tasks']]
        self.assertEqual(ids.count(t['id']), 1, 'задача в дне ровно один раз')
        self.assertEqual(d['free_slots'], d['limit'] - len(d['tasks']))
        self.assertEqual(len(ids), len(set(ids)), 'дубликатов в tasks нет')
        self.assertEqual(self.get_task(t['id'])['is_today'], 1)


# =====================================================================
# Rollover (§5.6, §7): перенос, needs_review, авто-перегруппировка
# =====================================================================

class RolloverMixin(object):

    def rollover(self, today):
        status, data = self.call('POST', '/api/rollover', {'today': today})
        self.assertEqual(status, 200, data)
        for k in ('carried', 'review', 'regrouped', 'rescored', 'today'):
            self.assertIn(k, data, 'в ответе rollover нет %s' % k)
        self.assertEqual(data['today'], today)
        return data

    def add_today(self, task_id):
        status, data = self.call('POST', '/api/today', {'task_id': task_id})
        self.assertEqual(status, 200, data)
        return data


class TestRolloverCarry(RolloverMixin, ServerTestCase):

    def test_carry_sequence_to_review(self):
        t = self.create_task('Переносимая задача', answers(5, 'today', 'serious', 'me', '30m'))
        self.assertEqual(t['group'], 'now')
        self.add_today(t['id'])
        cur = self.get_task(t['id'])
        self.assertEqual(cur['is_today'], 1)
        self.assertEqual(cur['today_date'], TODAY)

        # день 1: 2026-10-26
        r = self.rollover('2026-10-26')
        self.assertGreaterEqual(r['carried'], 1)
        cur = self.get_task(t['id'])
        self.assertEqual(cur['is_today'], 0)
        self.assertIsNone(cur['today_date'])
        self.assertEqual(cur['carry_count'], 1)
        self.assertEqual(cur['group'], 'now', 'после переноса задача снова кандидат (group now, не в дне)')
        self.assertEqual(cur['status'], 'open')
        self.assertEqual(cur['needs_review'], 0)
        self.assertEqual(cur['review_reason'], '')
        carries = [e for e in self.journal(t['id']) if e['event'] == 'carry']
        self.assertEqual(len(carries), 1)
        self.assertEqual(carries[0]['old_value'], '0')
        self.assertEqual(carries[0]['new_value'], '1')

        # повторный rollover на ту же дату — carry не дублируется
        r = self.rollover('2026-10-26')
        self.assertEqual(r['carried'], 0)
        self.assertEqual(self.get_task(t['id'])['carry_count'], 1)
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(s['last_rollover'], '2026-10-26')

        # день 2
        self.add_today(t['id'])
        self.rollover('2026-10-27')
        cur = self.get_task(t['id'])
        self.assertEqual(cur['carry_count'], 2)
        self.assertEqual(cur['needs_review'], 0, 'carry_limit=3: после 2 переносов ещё рано')
        self.assertEqual(cur['is_today'], 0)

        # день 3 → carry_count 3 ≥ carry_limit → needs_review 'carried'
        self.add_today(t['id'])
        r = self.rollover('2026-10-28')
        self.assertGreaterEqual(r['review'], 1)
        cur = self.get_task(t['id'])
        self.assertEqual(cur['carry_count'], 3)
        self.assertEqual(cur['needs_review'], 1)
        self.assertEqual(cur['review_reason'], 'carried')
        self.assertEqual(cur['is_today'], 0)
        status, rv = self.call('GET', '/api/review')
        self.assertEqual(status, 200, rv)
        self.assertIn(t['id'], [x['id'] for x in rv['tasks']])
        status, lst = self.call('GET', '/api/tasks?needs_review=1')
        self.assertIn(t['id'], [x['id'] for x in lst['tasks']])
        status, board = self.call('GET', '/api/board')
        self.assertIn(t['id'], [x['id'] for x in board['review']])
        ev = self.journal(t['id'])
        self.assertEqual(len([e for e in ev if e['event'] == 'carry']), 3)
        rr = [e for e in ev if e['event'] == 'review_required']
        self.assertEqual(len(rr), 1, 'review_required пишется один раз')
        self.assertEqual(rr[0]['new_value'], 'carried')

        # повторный мастер сбрасывает needs_review и carry_count
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % t['id'],
                                 {'answers': answers(5, 'today', 'serious', 'me', '30m')})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['needs_review'], 0)
        self.assertEqual(data['task']['review_reason'], '')
        self.assertEqual(data['task']['carry_count'], 0)
        status, rv = self.call('GET', '/api/review')
        self.assertNotIn(t['id'], [x['id'] for x in rv['tasks']])

    def test_done_task_in_today_is_not_carried(self):
        t = self.create_task('Сделана в день', answers(4, 'today', 'serious', 'me', '5m'))
        self.add_today(t['id'])
        status, data = self.call('POST', '/api/tasks/%d/done' % t['id'])
        self.assertEqual(status, 200, data)
        self.rollover('2026-10-29')
        cur = self.get_task(t['id'])
        self.assertEqual(cur['status'], 'done')
        self.assertEqual(cur['carry_count'], 0, 'сделанная задача не переносится')
        self.assertEqual(cur['needs_review'], 0)
        self.assertNotIn('carry', self.events(t['id']))


class TestRolloverMisc(RolloverMixin, ServerTestCase):

    def test_rollover_without_body_uses_server_today(self):
        status, data = self.call('POST', '/api/rollover', {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get('today'), TODAY, 'без today — локальная дата сервера (FOKUS_TODAY)')
        for k in ('carried', 'review', 'regrouped', 'rescored'):
            self.assertIn(k, data)
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(s['last_rollover'], TODAY)

    def test_rollover_bad_body(self):
        status, data = self.call('POST', '/api/rollover', raw='xxx')
        self.assertEqual(status, 400, data)
        self.assertError(data, 'bad_json')
        status, data = self.call('POST', '/api/rollover', {'today': '25.10.2026'})
        self.assertEqual(status, 400, data)
        self.assertIn('error', data)

    def test_auto_rollover_on_start(self):
        # §7: перед любым /api/* запросом, если last_rollover < сегодня → rollover; на старте БД пуста
        status, s = self.call('GET', '/api/settings')
        self.assertEqual(status, 200)
        # last_rollover=null на старте: спецификация не говорит, считается ли null < сегодня,
        # поэтому допускаем null или сегодняшнюю дату, но не чужую.
        self.assertIn(s['last_rollover'], (None, TODAY), s['last_rollover'])


class TestRolloverStale(RolloverMixin, ServerTestCase):

    def test_stale_after_11_days(self):
        in_day = self.create_task('В дне, станет stale', answers(5, 'today', 'serious', 'me', '30m'))
        idle = self.create_task('Не в дне, станет stale', answers(3, 'month', 'none', 'me', '30m'))
        done = self.create_task('Сделана, не stale', answers(3, 'week', 'none', 'me', '30m'))
        self.add_today(in_day['id'])
        status, _ = self.call('POST', '/api/tasks/%d/done' % done['id'])
        self.assertEqual(status, 200)

        r = self.rollover('2026-11-05')
        self.assertGreaterEqual(r['carried'], 1)
        self.assertGreaterEqual(r['review'], 2)

        a = self.get_task(in_day['id'])
        self.assertEqual(a['carry_count'], 1)
        self.assertEqual(a['is_today'], 0)
        self.assertEqual(a['needs_review'], 1)
        self.assertEqual(a['review_reason'], 'stale', 'carry_count 1 < 3 → причина stale')
        b = self.get_task(idle['id'])
        self.assertEqual(b['carry_count'], 0)
        self.assertEqual(b['needs_review'], 1)
        self.assertEqual(b['review_reason'], 'stale')
        self.assertEqual(b['group'], 'queue', 'imp3/month/none/30m: 19 дней → urgency 3 → 35.0 queue')
        self.assertAlmostEqual(b['score'], 35.0, places=3)
        c = self.get_task(done['id'])
        self.assertEqual(c['needs_review'], 0, 'review только для открытых задач')

        status, rv = self.call('GET', '/api/review')
        self.assertEqual(status, 200)
        got = [x['id'] for x in rv['tasks']]
        self.assertIn(in_day['id'], got)
        self.assertIn(idle['id'], got)
        self.assertNotIn(done['id'], got)
        for tid in (in_day['id'], idle['id']):
            rr = [e for e in self.journal(tid) if e['event'] == 'review_required']
            self.assertEqual(len(rr), 1, tid)
            self.assertEqual(rr[0]['new_value'], 'stale')

        # повторный rollover: review не дублируется
        r = self.rollover('2026-11-06')
        self.assertEqual(self.get_task(idle['id'])['review_reason'], 'stale')
        self.assertEqual(len([e for e in self.journal(idle['id']) if e['event'] == 'review_required']), 1)

        # evaluate снимает stale
        status, data = self.call('POST', '/api/tasks/%d/evaluate' % idle['id'],
                                 {'answers': answers(3, 'month', 'none', 'me', '30m')})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['task']['needs_review'], 0)
        self.assertEqual(data['task']['review_reason'], '')
        status, rv = self.call('GET', '/api/review')
        self.assertNotIn(idle['id'], [x['id'] for x in rv['tasks']])


class TestRolloverRegroup(RolloverMixin, ServerTestCase):

    def test_auto_regroup_by_urgency(self):
        # imp3/month/unpleasant/me/2h: 22.5 + 9 + 10 + 2 = 43.5 → queue (due 2026-11-24)
        auto = self.create_task('Авто-перегруппировка', answers(3, 'month', 'unpleasant', 'me', '2h'))
        self.assertAlmostEqual(auto['score'], 43.5, places=3)
        self.assertEqual(auto['group'], 'queue')
        self.assertEqual(auto['due_date'], MONTH)
        # та же задача, перенесённая вручную — группа не меняется
        manual = self.create_task('Вручную в someday', answers(3, 'month', 'unpleasant', 'me', '2h'))
        status, data = self.call('POST', '/api/tasks/%d/move' % manual['id'], {'group': 'someday'})
        self.assertEqual(status, 200, data)
        # delegate и waiting не перегруппируются
        deleg = self.create_task('Поручено', answers(3, 'month', 'unpleasant', 'delegate', '2h', delegate_to='Света'))
        self.assertEqual(deleg['group'], 'delegate')
        wait = self.create_task('Ждёт', answers(3, 'month', 'unpleasant', 'me', '2h', depends_on='документ'))
        self.assertEqual(wait['group'], 'waiting')
        # задача, которая НЕ должна менять группу: imp4/month/unpleasant/2h 54.8 → 63.8, всё ещё priority
        stay = self.create_task('Остаётся priority', answers(4, 'month', 'unpleasant', 'me', '2h'))
        self.assertAlmostEqual(stay['score'], 54.8, places=3)
        self.assertEqual(stay['group'], 'priority')

        # 2026-11-20: до 2026-11-24 — 4 дня → urgency 6 → 22.5 + 18 + 10 + 2 = 52.5 → priority
        r = self.rollover('2026-11-20')
        self.assertGreaterEqual(r['regrouped'], 1)
        self.assertGreaterEqual(r['rescored'], 1)

        a = self.get_task(auto['id'])
        self.assertEqual(a['group'], 'priority')
        self.assertAlmostEqual(a['score'], 52.5, places=3)
        self.assertEqual(a['group_manual'], 0)
        self.assertEqual(a['due_date'], MONTH, 'due_date при rollover не меняется')
        self.assertEqual(a['due'], 'month')
        gc = [e for e in self.journal(auto['id']) if e['event'] == 'group_change']
        self.assertEqual(len(gc), 1)
        self.assertEqual(gc[0]['old_value'], 'queue')
        self.assertEqual(gc[0]['new_value'], 'priority')
        self.assertEqual(gc[0]['note'], 'auto')
        self.assertNotIn('manual_move', self.events(auto['id']))

        m = self.get_task(manual['id'])
        self.assertEqual(m['group'], 'someday', '§9.3: ручная группа сильнее формулы')
        self.assertEqual(m['group_manual'], 1)
        self.assertAlmostEqual(m['score'], 52.5, places=3, msg='балл пересчитывается у всех открытых задач')
        self.assertNotIn('group_change', self.events(manual['id']))

        self.assertEqual(self.get_task(deleg['id'])['group'], 'delegate')
        self.assertEqual(self.get_task(wait['id'])['group'], 'waiting')
        self.assertAlmostEqual(self.get_task(wait['id'])['score'], 52.5, places=3)

        s = self.get_task(stay['id'])
        self.assertEqual(s['group'], 'priority')
        self.assertAlmostEqual(s['score'], 63.8, places=3)
        self.assertNotIn('group_change', self.events(stay['id']))

        # повторный rollover на ту же дату ничего не меняет
        r2 = self.rollover('2026-11-20')
        self.assertEqual(r2['regrouped'], 0)
        self.assertEqual(len([e for e in self.journal(auto['id']) if e['event'] == 'group_change']), 1)


class TestRolloverOverdue(RolloverMixin, ServerTestCase):

    def test_overdue_task_rises_to_now(self):
        # imp3/tomorrow/unpleasant/me/2h: 22.5 + 24 + 10 + 2 = 58.5 → priority
        t = self.create_task('Станет просроченной', answers(3, 'tomorrow', 'unpleasant', 'me', '2h'))
        self.assertAlmostEqual(t['score'], 58.5, places=3)
        self.assertEqual(t['group'], 'priority')
        # 2026-10-27: due 2026-10-26 просрочена → urgency 10 → 22.5 + 30 + 10 + 2 = 64.5 → priority (<65)
        self.rollover('2026-10-27')
        cur = self.get_task(t['id'])
        self.assertAlmostEqual(cur['score'], 64.5, places=3)
        self.assertEqual(cur['group'], 'priority')
        # imp4/week/none/me/30m: 33.75 + 18 + 0 + 3.5 = 55.3 priority → за день до срока (1 день → 8): 33.75+24+3.5 = 61.3 priority
        # → в день срока (0 → 10): 33.75 + 30 + 3.5 = 67.3 → now
        t2 = self.create_task('Поднимется в now', answers(4, 'week', 'none', 'me', '30m'))
        self.assertAlmostEqual(t2['score'], 55.3, places=3)
        self.assertEqual(t2['group'], 'priority')
        self.rollover('2026-10-31')
        cur = self.get_task(t2['id'])
        self.assertAlmostEqual(cur['score'], 61.3, places=3)
        self.assertEqual(cur['group'], 'priority')
        self.rollover(WEEK)
        cur = self.get_task(t2['id'])
        self.assertAlmostEqual(cur['score'], 67.3, places=3)
        self.assertEqual(cur['group'], 'now')
        gc = [e for e in self.journal(t2['id']) if e['event'] == 'group_change']
        self.assertEqual(len(gc), 1)
        self.assertEqual((gc[0]['old_value'], gc[0]['new_value'], gc[0]['note']), ('priority', 'now', 'auto'))


if __name__ == '__main__':
    unittest.main()
