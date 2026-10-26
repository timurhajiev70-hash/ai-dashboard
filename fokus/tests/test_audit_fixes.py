# -*- coding: utf-8 -*-
"""Регрессии по аудиту v2: Host (DNS-rebinding), огромный task_id в журнале."""
import http.client
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

FOKUS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


class TestHostAndJournal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='fokus-audit-')
        cls.port = free_port()
        env = dict(os.environ, FOKUS_PORT=str(cls.port), FOKUS_DB=os.path.join(cls.tmp, 'f.db'), FOKUS_TODAY='2026-10-27')
        cls.proc = subprocess.Popen([sys.executable, os.path.join(FOKUS, 'server.py')], cwd=FOKUS, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                c = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=1); c.request('GET', '/api/health'); c.getresponse(); break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.send_signal(signal.SIGTERM)
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, host=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {}
        if host is not None:
            headers['Host'] = host
        c.request('GET', path, headers=headers)
        r = c.getresponse(); body = r.read(); c.close()
        return r.status, body

    def test_foreign_host_is_refused(self):
        status, _ = self.get('/api/settings', host='evil.example.com:%d' % self.port)
        self.assertEqual(status, 403)

    def test_own_host_is_accepted(self):
        status, _ = self.get('/api/settings', host='127.0.0.1:%d' % self.port)
        self.assertEqual(status, 200)
        status, _ = self.get('/api/health', host='localhost:%d' % self.port)
        self.assertEqual(status, 200)

    def test_huge_task_id_in_journal_is_400_not_500(self):
        status, _ = self.get('/api/journal?task_id=99999999999999999999')
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
