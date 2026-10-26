# -*- coding: utf-8 -*-
"""Тесты MCP-сервера Fokus (mcp_server.py): протокол по stdio и инструменты.

Запускает mcp_server.py как подпроцесс с FOKUS_DB во временном файле; HTTP-сервер
Fokus поднимается автозапуском самого MCP-сервера (FOKUS_MCP_AUTOSTART=1).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

FOKUS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FOKUS)
import mcp_server  # noqa: E402

TODAY = '2026-10-25'
ANS_NOW = {'importance': 5, 'due': 'today', 'consequence': 'serious', 'who': 'me', 'duration': '30m'}
ANS_QUEUE = {'importance': 2, 'due': 'week', 'consequence': 'none', 'who': 'me', 'duration': '30m'}


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class McpSession(object):
    """Минимальный клиент: пишет JSON-RPC строки в stdin, читает ответы из stdout."""

    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, os.path.join(FOKUS, 'mcp_server.py')], cwd=FOKUS, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 1

    def send(self, message):
        # ensure_ascii=True: суррогаты (\ud800) уходят в escape-форме — так их шлёт и реальный клиент
        self.proc.stdin.write((json.dumps(message, ensure_ascii=True) + '\n').encode('utf-8'))
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError('MCP-сервер закрыл stdout; stderr: %s' % self.proc.stderr.read().decode('utf-8', 'replace'))
        return json.loads(line.decode('utf-8'))

    def request(self, method, params=None):
        msg_id = self.next_id
        self.next_id += 1
        self.send({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params or {}})
        resp = self.read()
        assert resp.get('id') == msg_id, resp
        return resp

    def notify(self, method, params=None):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def call(self, name, arguments=None):
        resp = self.request('tools/call', {'name': name, 'arguments': arguments or {}})
        assert 'result' in resp, resp
        return resp['result']

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


class TestMcpProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='fokus-mcp-')
        cls.port = free_port()
        env = dict(os.environ, FOKUS_PORT=str(cls.port), FOKUS_DB=os.path.join(cls.tmp, 'fokus.db'),
                   FOKUS_TODAY=TODAY, FOKUS_MCP_AUTOSTART='1')
        cls.s = McpSession(env)
        init = cls.s.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                            'clientInfo': {'name': 'test', 'version': '0'}})
        cls.init_result = init['result']
        cls.s.notify('notifications/initialized')

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        # остановить ТОЛЬКО автозапущенный для теста сервер — по pid из /api/health на тестовом порту
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/health' % cls.port, timeout=1) as resp:
                health = json.loads(resp.read().decode('utf-8'))
            os.kill(int(health['pid']), signal.SIGTERM)
        except Exception:
            pass
        time.sleep(0.5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- протокол
    def test_01_initialize(self):
        r = self.init_result
        self.assertEqual(r['protocolVersion'], '2025-06-18')
        self.assertEqual(r['serverInfo']['name'], 'fokus')
        self.assertIn('tools', r['capabilities'])
        self.assertIn('instructions', r)

    def test_02_ping_and_unknown(self):
        self.assertEqual(self.s.request('ping')['result'], {})
        resp = self.s.request('nope/method')
        self.assertEqual(resp['error']['code'], -32601)

    def test_03_tools_list(self):
        tools = self.s.request('tools/list')['result']['tools']
        names = [t['name'] for t in tools]
        for n in ('fokus_today', 'fokus_add_task', 'fokus_preview', 'fokus_board', 'fokus_done', 'fokus_today_add',
                  'fokus_reevaluate', 'fokus_move', 'fokus_review', 'fokus_journal',
                  # v2: проекты, «Входящие», календарь, «Предстоящие»
                  'fokus_inbox', 'fokus_projects', 'fokus_set_project', 'fokus_calendar', 'fokus_upcoming'):
            self.assertIn(n, names)
        for t in tools:
            self.assertEqual(t['inputSchema']['type'], 'object')
            self.assertTrue(t['description'])

    def test_04_prompts(self):
        prompts = self.s.request('prompts/list')['result']['prompts']
        self.assertEqual(prompts[0]['name'], 'fokus_wizard')
        got = self.s.request('prompts/get', {'name': 'fokus_wizard', 'arguments': {'title': 'Позвонить'}})['result']
        self.assertIn('Позвонить', got['messages'][0]['content']['text'])
        self.assertIn('fokus_add_task', got['messages'][0]['content']['text'])

    def test_05_unknown_tool_and_bad_params(self):
        resp = self.s.request('tools/call', {'name': 'fokus_nope', 'arguments': {}})
        self.assertEqual(resp['error']['code'], -32602)
        r = self.s.call('fokus_done', {'task_id': 'abc'})
        self.assertTrue(r['isError'])
        r = self.s.call('fokus_done', {'task_id': 999999})
        self.assertTrue(r['isError'])
        self.assertIn('404', r['content'][0]['text'])

    def test_06_bad_json_line(self):
        self.s.proc.stdin.write(b'{not json\n')
        self.s.proc.stdin.flush()
        resp = self.s.read()
        self.assertEqual(resp['error']['code'], -32700)

    def test_07_hostile_input_does_not_kill_server(self):
        # одиночный суррогат в id / name (валидный JSON-escape) — ответ в escape-форме, сервер жив
        self.s.send({'jsonrpc': '2.0', 'id': '\ud800', 'method': 'ping'})
        resp = self.s.read()
        self.assertEqual(resp['id'], '\ud800')
        self.s.send({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': '\ud800', 'arguments': {}}})
        self.assertEqual(self.s.read()['error']['code'], -32602)
        # глубоко вложенный JSON → Parse error, не падение
        self.s.proc.stdin.write(('[' * 100000 + ']' * 100000 + '\n').encode('utf-8'))
        self.s.proc.stdin.flush()
        self.assertEqual(self.s.read()['error']['code'], -32700)
        # params не объект → Invalid params; запрос без method → Invalid Request; пустой батч
        self.s.send({'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call', 'params': [1, 2]})
        self.assertEqual(self.s.read()['error']['code'], -32602)
        self.s.send({'jsonrpc': '2.0', 'id': 9})
        self.assertEqual(self.s.read()['error']['code'], -32600)
        self.s.send({'jsonrpc': '2.0', 'id': 10, 'method': 42})
        self.assertEqual(self.s.read()['error']['code'], -32600)
        self.s.proc.stdin.write(b'[]\n'); self.s.proc.stdin.flush()
        self.assertEqual(self.s.read()['error']['code'], -32600)
        # ответ клиента (result без method) игнорируется, сервер жив
        self.s.send({'jsonrpc': '2.0', 'id': 11, 'result': {}})
        self.assertEqual(self.s.request('ping')['result'], {})

    def test_08_tool_argument_validation(self):
        for args in ({'task_id': float('inf')}, {'task_id': 1.5}, {'task_id': 0}, {'task_id': True}):
            r = self.s.call('fokus_done', args)
            self.assertTrue(r['isError'], args)
            self.assertIn('Неверные параметры', r['content'][0]['text'])
        r = self.s.call('fokus_journal', {'limit': [1]})
        self.assertTrue(r['isError'])
        r = self.s.call('fokus_list_tasks', {'group': 'now&status=all'})
        self.assertTrue(r['isError'])
        self.assertIn('group', r['content'][0]['text'])
        r = self.s.call('fokus_board', {'sort': 'a b'})
        self.assertTrue(r['isError'])

    def test_09_annotations(self):
        tools = self.s.request('tools/list')['result']['tools']
        by = dict((t['name'], t) for t in tools)
        self.assertTrue(by['fokus_today']['annotations']['readOnlyHint'])
        self.assertTrue(by['fokus_delete']['annotations']['destructiveHint'])
        self.assertFalse(by['fokus_add_task']['annotations']['readOnlyHint'])
        self.assertIn('tomedo', by['fokus_edit']['description'])

    # --- инструменты (сценарий; порядок важен)
    def test_10_preview(self):
        r = self.s.call('fokus_preview', {'answers': {'importance': 4, 'due': 'week', 'consequence': 'unpleasant',
                                                      'who': 'me', 'duration': '5m'}})
        self.assertFalse(r['isError'])
        self.assertEqual(r['structuredContent']['group'], 'now')
        self.assertAlmostEqual(r['structuredContent']['score'], 66.8, places=1)
        self.assertIn('сделай сразу', r['content'][0]['text'])
        # ничего не сохранилось
        self.assertEqual(self.s.call('fokus_list_tasks', {})['structuredContent']['tasks'], [])

    def test_11_add_task_and_today(self):
        r = self.s.call('fokus_add_task', {'title': 'Позвонить в налоговую', 'answers': ANS_NOW, 'add_to_today': True})
        self.assertFalse(r['isError'], r)
        sc = r['structuredContent']
        self.assertEqual(sc['task']['group'], 'now')
        self.assertAlmostEqual(sc['task']['score'], 98.5, places=1)
        self.assertTrue(sc['added_to_today'])
        self.assertIn('🔴', r['content'][0]['text'])
        today = self.s.call('fokus_today')['structuredContent']
        self.assertEqual(len(today['tasks']), 1)
        self.assertEqual(today['free_slots'], 2)
        # задача не из 🔴 — в день не ставится
        r2 = self.s.call('fokus_add_task', {'title': 'Почитать статью', 'answers': ANS_QUEUE, 'add_to_today': True})
        self.assertFalse(r2['isError'])
        self.assertEqual(r2['structuredContent']['task']['group'], 'queue')
        self.assertFalse(r2['structuredContent']['added_to_today'])

    def test_12_day_full_then_replace(self):
        ids = []
        for i in range(3):
            r = self.s.call('fokus_add_task', {'title': 'Срочная %d' % i, 'answers': dict(ANS_NOW, duration='5m')})
            ids.append(r['structuredContent']['task']['id'])
        self.s.call('fokus_today_add', {'task_id': ids[0]})
        self.s.call('fokus_today_add', {'task_id': ids[1]})
        full = self.s.call('fokus_today_add', {'task_id': ids[2]})
        self.assertTrue(full['isError'])
        self.assertIn('День полный', full['content'][0]['text'])
        rep = self.s.call('fokus_today_add', {'task_id': ids[2], 'replace_id': ids[0]})
        self.assertFalse(rep['isError'], rep)
        today_ids = [t['id'] for t in rep['structuredContent']['tasks']]
        self.assertIn(ids[2], today_ids)
        self.assertNotIn(ids[0], today_ids)
        self.assertEqual(len(today_ids), 3)
        # pool при полном дне
        today = self.s.call('fokus_today')
        self.assertEqual(today['structuredContent']['free_slots'], 0)
        self.assertIn('день полный', today['content'][0].get('text').lower())

    def test_13_done_move_reeval_journal(self):
        tasks = self.s.call('fokus_list_tasks', {'group': 'queue'})['structuredContent']['tasks']
        tid = tasks[0]['id']
        self.assertEqual(self.s.call('fokus_done', {'task_id': tid})['structuredContent']['task']['status'], 'done')
        self.assertEqual(self.s.call('fokus_reopen', {'task_id': tid})['structuredContent']['task']['status'], 'open')
        mv = self.s.call('fokus_move', {'task_id': tid, 'group': 'someday'})['structuredContent']['task']
        self.assertEqual(mv['group'], 'someday')
        self.assertEqual(mv['group_manual'], 1)
        ev = self.s.call('fokus_reevaluate', {'task_id': tid, 'answers': ANS_NOW})['structuredContent']['task']
        self.assertEqual(ev['group'], 'now')
        self.assertEqual(ev['group_manual'], 0)
        ed = self.s.call('fokus_edit', {'task_id': tid, 'title': 'Переименовано'})['structuredContent']['task']
        self.assertEqual(ed['title'], 'Переименовано')
        j = self.s.call('fokus_journal', {'task_id': tid})
        events = [e['event'] for e in j['structuredContent']['entries']]
        for ev_name in ('create', 'done', 'reopen', 'manual_move', 'evaluate', 'edit'):
            self.assertIn(ev_name, events)
        board = self.s.call('fokus_board')['structuredContent']
        # v2: доска содержит 7 групп, включая inbox (§7) — сравниваем со спецификацией, не с GROUP_ENUM
        self.assertEqual(sorted(board['groups'].keys()),
                         sorted(['now', 'priority', 'queue', 'delegate', 'someday', 'waiting', 'inbox']))
        self.assertIn('Переоценка никому не нужна', self.s.call('fokus_review')['content'][0]['text'])
        self.assertEqual(self.s.call('fokus_settings')['structuredContent']['day_limit'], 3)
        r = self.s.call('fokus_edit', {'task_id': tid})
        self.assertTrue(r['isError'])
        d = self.s.call('fokus_delete', {'task_id': tid})
        self.assertEqual(d['structuredContent']['task']['status'], 'deleted')
        g = self.s.call('fokus_get_task', {'task_id': tid})
        self.assertIn('удалена', g['content'][0]['text'])

    def test_14_validation_error_from_api(self):
        r = self.s.call('fokus_add_task', {'title': '', 'answers': ANS_NOW})
        self.assertTrue(r['isError'])
        self.assertIn('title', r['content'][0]['text'])
        r = self.s.call('fokus_add_task', {'title': 'x', 'answers': dict(ANS_NOW, importance=9)})
        self.assertTrue(r['isError'])
        self.assertIn('importance', r['content'][0]['text'])


class TestMcpNoAutostart(unittest.TestCase):
    def test_unavailable_is_tool_error_not_crash(self):
        port = free_port()
        env = dict(os.environ, FOKUS_PORT=str(port), FOKUS_MCP_AUTOSTART='0')
        s = McpSession(env)
        try:
            s.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}})
            s.notify('notifications/initialized')
            r = s.call('fokus_today')
            self.assertTrue(r['isError'])
            self.assertIn('не запущен', r['content'][0]['text'])
            # сервер жив после ошибки
            self.assertEqual(s.request('ping')['result'], {})
        finally:
            s.close()


class TestInstall(unittest.TestCase):
    def test_config_snippet_and_install_merge(self):
        snippet = mcp_server.config_snippet()
        self.assertEqual(snippet['mcpServers']['fokus']['args'][0], os.path.join(FOKUS, 'mcp_server.py'))
        tmp = tempfile.mkdtemp(prefix='fokus-cfg-')
        try:
            path = os.path.join(tmp, 'sub', 'claude_desktop_config.json')
            self.assertEqual(mcp_server.install(path), path)   # файла не было — создан
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
            self.assertIn('fokus', cfg['mcpServers'])
            with open(path, 'w', encoding='utf-8') as f:            # есть чужие серверы — сливаем
                json.dump({'mcpServers': {'other': {'command': 'x'}}, 'theme': 'dark'}, f)
            mcp_server.install(path)
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
            self.assertEqual(set(cfg['mcpServers']), {'other', 'fokus'})
            self.assertEqual(cfg['theme'], 'dark')
            self.assertTrue(os.path.isfile(path + '.bak'))
            mcp_server.install(path)   # повторная установка не затирает первую копию
            self.assertTrue(os.path.isfile(path + '.bak'))
            self.assertTrue(any(n.startswith('claude_desktop_config.json.bak-') for n in os.listdir(os.path.dirname(path))))
            with open(path, 'w', encoding='utf-8') as f:            # битый JSON → ValueError, не трейсбек
                f.write('{broken')
            with self.assertRaises(ValueError):
                mcp_server.install(path)
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mcp_server.main(['--install', '--path', path]), 1)
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mcp_server.main(['--install', '--path']), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
