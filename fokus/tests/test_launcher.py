# -*- coding: utf-8 -*-
"""Тесты лаунчера Fokus.command (блок E). Совместимо с Python 3.9, только stdlib.

Лаунчер — bash-скрипт для macOS; здесь проверяем то, что можно проверить на любой ОС:
синтаксис, права, обязательные фрагменты, поведение без Python, разрешение символической ссылки,
и — если server.py уже существует — полный запуск с реальным сервером.
Браузер подменяется через FOKUS_OPEN=true, интерпретатор — через FOKUS_PYTHON.
"""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(PROJECT_DIR, 'Fokus.command')
SERVER_PY = os.path.join(PROJECT_DIR, 'server.py')
BASH = shutil.which('bash')


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def fetch_health(port: int, timeout: float = 1.0) -> Optional[dict]:
    """JSON ответа /api/health или None, если сервер не отвечает."""
    url = 'http://127.0.0.1:%d/api/health' % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def wait_health(port: int, seconds: float) -> Optional[dict]:
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = fetch_health(port)
        if data is not None:
            return data
        time.sleep(0.2)
    return None


def launcher_env(**extra) -> dict:
    env = dict(os.environ)
    env['FOKUS_OPEN'] = 'true'           # вместо macOS `open`
    env['FOKUS_PORT'] = str(free_port())  # свободный порт: на 4321 может жить настоящий Fokus
    env.pop('FOKUS_DB', None)
    env.pop('FOKUS_TODAY', None)
    env.update(extra)
    return env


def run_launcher(path: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    # stdin не терминал → лаунчер не ждёт клавишу (read -n1 только при [ -t 0 ])
    return subprocess.run(
        [path], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )


class LauncherStaticTests(unittest.TestCase):
    """(а)–(в): файл на месте, синтаксис, права, обязательные фрагменты."""

    def setUp(self):
        self.assertTrue(os.path.exists(LAUNCHER), 'нет файла Fokus.command')
        with open(LAUNCHER, encoding='utf-8') as f:
            self.text = f.read()

    def test_bash_syntax(self):
        if not BASH:
            self.skipTest('bash не найден')
        proc = subprocess.run([BASH, '-n', LAUNCHER], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', 'replace'))

    def test_shebang(self):
        self.assertTrue(self.text.startswith('#!/bin/bash\n'), 'первая строка должна быть #!/bin/bash')

    def test_executable(self):
        mode = os.stat(LAUNCHER).st_mode
        self.assertTrue(mode & stat.S_IXUSR, 'нужен chmod +x Fokus.command')
        self.assertTrue(os.access(LAUNCHER, os.X_OK))

    def test_required_fragments(self):
        self.assertRegex(self.text, r'\bcd\s+"\$\(dirname\s+"', 'нет cd в папку скрипта')
        for fragment in ('FOKUS_PORT', '4321', 'server.py', '/api/health',
                         'FOKUS_OPEN', 'FOKUS_PYTHON', '[ -t 0 ]', 'read -n1'):
            self.assertIn(fragment, self.text, 'в лаунчере нет фрагмента %r' % fragment)

    def test_no_backup_copy_in_launcher(self):
        # бэкап БД делает server.py; лаунчер ничего не копирует
        self.assertNotRegex(self.text, r'^\s*cp\s+', 'лаунчер не должен копировать базу сам')


class LauncherNoPythonTests(unittest.TestCase):
    """(г): без Python 3 — понятное сообщение и ненулевой код, окно не висит на read."""

    def setUp(self):
        if not BASH:
            self.skipTest('bash не найден')

    def test_missing_python_reports_and_exits(self):
        proc = run_launcher(LAUNCHER, launcher_env(FOKUS_PYTHON='/nonexistent/python3'))
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn('Python', out)
        self.assertIn('xcode-select --install', out)

    def test_old_python_version_rejected(self):
        tmp = tempfile.mkdtemp(prefix='fokus-oldpy-')
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, 'python3')
        with open(fake, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nexit 1\n')   # «версия ниже 3.9»: проверка версии возвращает 1
        os.chmod(fake, 0o755)
        proc = run_launcher(LAUNCHER, launcher_env(FOKUS_PYTHON=fake))
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn('3.9', out)

    def test_symlink_resolves_to_project_dir(self):
        """Ярлык на рабочем столе: ln -s ~/Projects/fokus/Fokus.command ~/Desktop/Fokus.command."""
        tmp = tempfile.mkdtemp(prefix='fokus-desktop-')
        self.addCleanup(shutil.rmtree, tmp, True)
        link = os.path.join(tmp, 'Fokus.command')
        try:
            os.symlink(LAUNCHER, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('символические ссылки недоступны: %s' % exc)
        proc = run_launcher(link, launcher_env(FOKUS_PYTHON='/nonexistent/python3'))
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertIn(os.path.realpath(PROJECT_DIR), out, out)
        self.assertNotIn(os.path.realpath(tmp), out.split('Python')[0], 'лаунчер остался в папке ярлыка')


class LauncherStartsServerTests(unittest.TestCase):
    """(д): полный запуск с настоящим server.py — пропускается, пока блока C нет."""

    def setUp(self):
        if not BASH:
            self.skipTest('bash не найден')
        if not os.path.exists(SERVER_PY):
            self.skipTest('server.py ещё нет (блок C)')
        self.tmp = tempfile.mkdtemp(prefix='fokus-launch-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = None
        self.addCleanup(self._kill_group)

    def _kill_group(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def test_launcher_starts_server_and_health_ok(self):
        port = free_port()
        env = launcher_env(
            FOKUS_PYTHON=sys.executable,
            FOKUS_PORT=str(port),
            FOKUS_DB=os.path.join(self.tmp, 'fokus.db'),
        )
        log_path = os.path.join(self.tmp, 'launcher.log')
        with open(log_path, 'wb') as log:
            self.proc = subprocess.Popen(
                [LAUNCHER], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,   # своя группа процессов → killpg снимает bash и python вместе
            )
            health = wait_health(port, 10)
            with open(log_path, 'rb') as f:
                out = f.read().decode('utf-8', 'replace')
            self.assertIsNotNone(health, 'сервер не ответил на /api/health за 10 с:\n' + out)
            self.assertIs(health.get('ok'), True, health)

            # лаунчер сам опрашивает health раз в 0.5 с — дождёмся его финальных строк
            deadline = time.time() + 10
            while time.time() < deadline and 'остановить' not in out:
                time.sleep(0.2)
                with open(log_path, 'rb') as f:
                    out = f.read().decode('utf-8', 'replace')
            self.assertIn('остановить', out, 'лаунчер не дошёл до строки «закройте окно, чтобы остановить»:\n' + out)
            self.assertIn('http://127.0.0.1:%d' % port, out)
            self.assertIsNone(self.proc.poll(), 'лаунчер должен ждать сервер (wait), а не выходить:\n' + out)

            # остановка: как Ctrl+C / закрытие окна Терминала
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.fail('лаунчер не завершился после SIGTERM')

        deadline = time.time() + 5
        while time.time() < deadline and fetch_health(port, 0.5) is not None:
            time.sleep(0.2)
        self.assertIsNone(fetch_health(port, 0.5), 'сервер остался работать после остановки лаунчера')


if __name__ == '__main__':
    unittest.main()
