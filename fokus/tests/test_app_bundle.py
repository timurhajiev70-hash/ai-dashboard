# -*- coding: utf-8 -*-
"""Тесты Fokus.app (AGENTS.md §13): bundle, иконка, генератор иконки, запуск скрипта. Python 3.9, только stdlib.

Проверяем то, что можно проверить на любой ОС: Info.plist, PkgInfo, права и синтаксис Contents/MacOS/Fokus,
структуру Fokus.icns, воспроизводимость tools/make_icon.py и реальный запуск сервера через скрипт приложения
(браузер подменяется FOKUS_OPEN=true, интерпретатор — FOKUS_PYTHON, osascript на Linux отсутствует → stderr).
"""
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import warnings
import xml.etree.ElementTree as ET
import zlib
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_DIR, 'Fokus.app')
CONTENTS = os.path.join(APP_DIR, 'Contents')
INFO_PLIST = os.path.join(CONTENTS, 'Info.plist')
PKGINFO = os.path.join(CONTENTS, 'PkgInfo')
APP_EXEC = os.path.join(CONTENTS, 'MacOS', 'Fokus')
ICNS = os.path.join(CONTENTS, 'Resources', 'Fokus.icns')
ICON_PNG = os.path.join(PROJECT_DIR, 'static', 'icon-256.png')
MAKE_ICON = os.path.join(PROJECT_DIR, 'tools', 'make_icon.py')
LAUNCHER = os.path.join(PROJECT_DIR, 'Fokus.command')
SERVER_PY = os.path.join(PROJECT_DIR, 'server.py')
BASH = shutil.which('bash')

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
ICNS_PNG_TYPES = {'icp4': 16, 'icp5': 32, 'icp6': 64, 'ic07': 128, 'ic08': 256, 'ic09': 512, 'ic10': 1024}


# ----------------------------------------------------------------- вспомогательные функции

def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def fetch_health(port: int, timeout: float = 1.0) -> Optional[dict]:
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


def wait_stopped(port: int, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fetch_health(port, 0.5) is None:
            return True
        time.sleep(0.2)
    return fetch_health(port, 0.5) is None


def post_shutdown(port: int) -> Optional[int]:
    """HTTP-код ответа POST /api/shutdown (404 — маршрута ещё нет) или None при сетевой ошибке."""
    req = urllib.request.Request('http://127.0.0.1:%d/api/shutdown' % port, data=b'', method='POST')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def parse_icns(data: bytes) -> list:
    """[(тип, длина чанка, (w, h) PNG или None)] — независимый от make_icon разбор ICNS."""
    if data[:4] != b'icns':
        raise ValueError('нет сигнатуры icns')
    total = struct.unpack('>I', data[4:8])[0]
    if total != len(data):
        raise ValueError('длина в заголовке %d != размер файла %d' % (total, len(data)))
    chunks = []
    pos = 8
    while pos < total:
        kind = data[pos:pos + 4].decode('ascii')
        length = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        if length < 8 or pos + length > total:
            raise ValueError('плохая длина чанка %s: %d' % (kind, length))
        payload = data[pos + 8:pos + length]
        dims = None
        if payload[:8] == PNG_SIGNATURE and payload[12:16] == b'IHDR':
            dims = struct.unpack('>II', payload[16:24])
        chunks.append((kind, length, dims))
        pos += length
    return chunks


def decode_png_rgba(data: bytes) -> tuple:
    """(width, height, rows) для несжатого по фильтрам PNG RGBA 8 бит (как пишет make_icon)."""
    assert data[:8] == PNG_SIGNATURE
    pos = 8
    width = height = 0
    idat = b''
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        crc = struct.unpack('>I', data[pos + 8 + length:pos + 12 + length])[0]
        if (zlib.crc32(kind + chunk) & 0xffffffff) != crc:
            raise ValueError('CRC чанка %r не совпадает' % kind)
        if kind == b'IHDR':
            width, height, depth, ctype = struct.unpack('>IIBB', chunk[:10])
            if (depth, ctype) != (8, 6):
                raise ValueError('ожидался RGBA 8 бит, получено depth=%d type=%d' % (depth, ctype))
        elif kind == b'IDAT':
            idat += chunk
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 4
    if len(raw) != stride * height:
        raise ValueError('длина данных %d != %d' % (len(raw), stride * height))
    rows = []
    for j in range(height):
        row = raw[j * stride:(j + 1) * stride]
        if row[0] != 0:
            raise ValueError('ожидался фильтр 0 в строке %d' % j)
        rows.append(row[1:])
    return width, height, rows


def pixel(rows, x, y):
    return tuple(rows[y][x * 4:x * 4 + 4])


def load_make_icon():
    spec = importlib.util.spec_from_file_location('fokus_make_icon', MAKE_ICON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def app_env(**extra) -> dict:
    env = dict(os.environ)
    env['FOKUS_OPEN'] = 'true'            # вместо macOS `open`
    env['FOKUS_PORT'] = str(free_port())  # на 4321 может жить настоящий Fokus
    env.pop('FOKUS_DB', None)
    env.pop('FOKUS_TODAY', None)
    env.pop('FOKUS_BROWSER', None)
    env.update(extra)
    return env


def run_app(path: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [path], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )


# ----------------------------------------------------------------- bundle: plist, PkgInfo, скрипт

class InfoPlistTests(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.exists(INFO_PLIST), 'нет Fokus.app/Contents/Info.plist')

    def test_valid_xml_and_plist(self):
        ET.parse(INFO_PLIST)   # валидный XML
        with open(INFO_PLIST, 'rb') as f:
            data = plistlib.load(f)
        self.assertIsInstance(data, dict)

    def test_required_keys(self):
        with open(INFO_PLIST, 'rb') as f:
            data = plistlib.load(f)
        expected = {
            'CFBundleName': 'Fokus',
            'CFBundleDisplayName': 'Fokus',
            'CFBundleIdentifier': 'de.fokus.app',
            'CFBundleVersion': '2.0',
            'CFBundleShortVersionString': '2.0',
            'CFBundleExecutable': 'Fokus',
            'CFBundleIconFile': 'Fokus',
            'CFBundlePackageType': 'APPL',
            'LSMinimumSystemVersion': '11.0',
            'LSApplicationCategoryType': 'public.app-category.productivity',
        }
        for key, value in expected.items():
            self.assertEqual(data.get(key), value, 'Info.plist: %s' % key)
        self.assertIs(data.get('NSHighResolutionCapable'), True)

    def test_executable_and_icon_referenced_exist(self):
        with open(INFO_PLIST, 'rb') as f:
            data = plistlib.load(f)
        self.assertTrue(os.path.isfile(os.path.join(CONTENTS, 'MacOS', data['CFBundleExecutable'])))
        self.assertTrue(os.path.isfile(os.path.join(CONTENTS, 'Resources', data['CFBundleIconFile'] + '.icns')))

    def test_pkginfo(self):
        self.assertTrue(os.path.exists(PKGINFO), 'нет Fokus.app/Contents/PkgInfo')
        with open(PKGINFO, 'rb') as f:
            self.assertEqual(f.read(), b'APPL????')


class AppExecutableStaticTests(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.exists(APP_EXEC), 'нет Fokus.app/Contents/MacOS/Fokus')
        with open(APP_EXEC, encoding='utf-8') as f:
            self.text = f.read()

    def test_executable_bit(self):
        mode = os.stat(APP_EXEC).st_mode
        self.assertTrue(mode & stat.S_IXUSR, 'нужен chmod +x Fokus.app/Contents/MacOS/Fokus')
        self.assertTrue(os.access(APP_EXEC, os.X_OK))

    def test_shebang_and_set_u(self):
        self.assertTrue(self.text.startswith('#!/bin/bash\n'), 'первая строка должна быть #!/bin/bash')
        self.assertRegex(self.text, r'(?m)^set -u\s*$', 'нет set -u')

    def test_bash_syntax(self):
        if not BASH:
            self.skipTest('bash не найден')
        proc = subprocess.run([BASH, '-n', APP_EXEC], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', 'replace'))

    def test_required_fragments(self):
        for fragment in ('nohup', '--app=', '/api/health', 'osascript', 'server.py', 'data/server.log',
                         'FOKUS_PORT', 'FOKUS_PYTHON', 'FOKUS_OPEN', 'FOKUS_BROWSER', 'pwd -P',
                         'Google Chrome', 'Brave Browser', 'Microsoft Edge', 'Arc', 'xcode-select --install'):
            self.assertIn(fragment, self.text, 'в скрипте нет фрагмента %r' % fragment)

    def test_no_bash4_constructs(self):
        code = '\n'.join(line for line in self.text.splitlines() if not line.lstrip().startswith('#'))
        for pattern in (r'declare\s+-A', r'\bmapfile\b', r'\breadarray\b', r'\|&', r'\$\{[A-Za-z_]+,,\}',
                        r'\$\{[A-Za-z_]+\^\^\}', r'&>>?\s'):
            self.assertNotRegex(code, pattern, 'конструкция bash 4+: %s' % pattern)


# ----------------------------------------------------------------- иконка

class IcnsTests(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.exists(ICNS), 'нет Fokus.app/Contents/Resources/Fokus.icns')
        with open(ICNS, 'rb') as f:
            self.data = f.read()

    def test_magic_and_size(self):
        self.assertEqual(self.data[:4], b'icns')
        self.assertLess(len(self.data), 1536 * 1024, 'Fokus.icns больше 1.5 МБ')

    def test_chunks(self):
        chunks = parse_icns(self.data)
        types = [c[0] for c in chunks]
        self.assertTrue('ic08' in types or 'ic09' in types, 'нет чанка ic08/ic09: %s' % types)
        self.assertEqual(len(types), len(set(types)), 'повторяющиеся чанки')
        for kind, _length, dims in chunks:
            self.assertIn(kind, ICNS_PNG_TYPES, 'неизвестный тип чанка %s' % kind)
            self.assertIsNotNone(dims, 'чанк %s не PNG' % kind)
            self.assertEqual(dims, (ICNS_PNG_TYPES[kind], ICNS_PNG_TYPES[kind]), 'размер PNG в %s' % kind)

    def test_preview_png_matches_ic08(self):
        self.assertTrue(os.path.exists(ICON_PNG), 'нет static/icon-256.png')
        with open(ICON_PNG, 'rb') as f:
            png = f.read()
        width, height, rows = decode_png_rgba(png)
        self.assertEqual((width, height), (256, 256))
        # углы прозрачные (скруглённый квадрат), центр — красная точка, рядом с центром тёмный фон квадрата
        self.assertEqual(pixel(rows, 0, 0)[3], 0)
        self.assertEqual(pixel(rows, 255, 255)[3], 0)
        self.assertEqual(pixel(rows, 128, 128), (0xe5, 0x48, 0x4d, 255))
        self.assertEqual(pixel(rows, 128, 24), (0x1f, 0x1f, 0x1f, 255))
        for kind, length, _dims in parse_icns(self.data):
            if kind == 'ic08':
                self.assertEqual(length - 8, len(png), 'icon-256.png и чанк ic08 разной длины')


class MakeIconTests(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.exists(MAKE_ICON), 'нет tools/make_icon.py')

    def test_reproducible_in_temp_dir(self):
        tmp = tempfile.mkdtemp(prefix='fokus-icon-')
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = subprocess.run([sys.executable, MAKE_ICON, '--out', tmp],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', 'replace'))
        with open(os.path.join(tmp, 'Fokus.icns'), 'rb') as f:
            fresh = parse_icns(f.read())
        with open(ICNS, 'rb') as f:
            committed = parse_icns(f.read())
        self.assertEqual([(c[0], c[2]) for c in fresh], [(c[0], c[2]) for c in committed],
                         'набор чанков свежей иконки отличается от Fokus.icns в репозитории')
        self.assertTrue(os.path.exists(os.path.join(tmp, 'icon-256.png')))

    def test_check_mode_parses_committed_icon(self):
        proc = subprocess.run([sys.executable, MAKE_ICON, '--check', ICNS],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn('ic08', out)

    def test_module_functions(self):
        mod = load_make_icon()
        png = mod.make_png(16)
        width, height, rows = decode_png_rgba(png)
        self.assertEqual((width, height), (16, 16))
        self.assertEqual(pixel(rows, 0, 0)[3], 0, 'угол 16px должен быть прозрачным')
        icns = mod.build_icns({'icp4': png})
        chunks = mod.parse_icns(icns)
        self.assertEqual([(c['type'], c['png']) for c in chunks], [('icp4', (16, 16))])
        with self.assertRaises(ValueError):
            mod.parse_icns(b'icns' + b'\x00\x00\x00\x20' + b'xxxx')   # длина не совпадает
        with self.assertRaises(ValueError):
            mod.parse_icns(b'not an icns file')


# ----------------------------------------------------------------- запуск скрипта (Linux/macOS)

class AppExecutableFailureTests(unittest.TestCase):
    """Без Python / без server.py — сообщение (в stderr, т.к. osascript нет) и код 1, скрипт не висит."""

    def setUp(self):
        if not BASH:
            self.skipTest('bash не найден')
        if shutil.which('osascript'):
            self.skipTest('на macOS сообщение уходит в диалог osascript, а не в stderr')

    def test_missing_python(self):
        proc = run_app(APP_EXEC, app_env(FOKUS_PYTHON='/nonexistent/python3'))
        err = proc.stderr.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn('Python', err)
        self.assertIn('xcode-select --install', err)

    def test_old_python(self):
        tmp = tempfile.mkdtemp(prefix='fokus-oldpy-')
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, 'python3')
        with open(fake, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(fake, 0o755)
        proc = run_app(APP_EXEC, app_env(FOKUS_PYTHON=fake))
        err = proc.stderr.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn('3.9', err)

    def test_bundle_outside_project(self):
        tmp = tempfile.mkdtemp(prefix='fokus-bundle-')
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(APP_DIR, os.path.join(tmp, 'Fokus.app'))
        proc = run_app(os.path.join(tmp, 'Fokus.app', 'Contents', 'MacOS', 'Fokus'), app_env())
        err = proc.stderr.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn('server.py', err)

    def test_symlinked_bundle_resolves_project(self):
        tmp = tempfile.mkdtemp(prefix='fokus-desktop-')
        self.addCleanup(shutil.rmtree, tmp, True)
        link = os.path.join(tmp, 'Fokus.app')
        try:
            os.symlink(APP_DIR, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('символические ссылки недоступны: %s' % exc)
        proc = run_app(os.path.join(link, 'Contents', 'MacOS', 'Fokus'), app_env(FOKUS_PYTHON='/nonexistent/python3'))
        err = proc.stderr.decode('utf-8', 'replace')
        # проект найден (server.py есть) → споткнулись уже на Python, а не на «нет server.py»
        self.assertEqual(proc.returncode, 1, err)
        self.assertNotIn('server.py', err)
        self.assertIn('Python', err)


class AppStartsServerTests(unittest.TestCase):
    """Реальный запуск: скрипт стартует server.py через nohup, открывает «окно» (FOKUS_OPEN=true) и сразу выходит."""

    def setUp(self):
        if not BASH:
            self.skipTest('bash не найден')
        if not os.path.exists(SERVER_PY):
            self.skipTest('server.py нет')
        self.tmp = tempfile.mkdtemp(prefix='fokus-app-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.port = free_port()
        self.server_pid = None
        self.addCleanup(self._kill_server)

    def _kill_server(self):
        pid = self.server_pid
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _stop_server(self, health: dict):
        """POST /api/shutdown; если маршрута ещё нет — SIGTERM по pid из /api/health; без pid — предупреждение."""
        code = post_shutdown(self.port)
        if code == 200 and wait_stopped(self.port, 10):
            self.server_pid = None
            return
        pid = health.get('pid')
        if not pid:
            warnings.warn('/api/shutdown ответил %r и /api/health без pid — сервер на порту %d не остановлен'
                          % (code, self.port))
            return
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as exc:
            self.fail('SIGTERM процессу %s: %s' % (pid, exc))
        self.assertTrue(wait_stopped(self.port, 10), 'сервер не остановился после SIGTERM')
        self.server_pid = None

    def test_starts_server_opens_window_and_exits(self):
        env = app_env(
            FOKUS_PYTHON=sys.executable,
            FOKUS_PORT=str(self.port),
            FOKUS_DB=os.path.join(self.tmp, 'fokus.db'),
        )
        proc = subprocess.Popen([APP_EXEC], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=15)   # скрипт обязан завершиться сам, сервер живёт дальше
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail('скрипт приложения не завершился за 15 с (держит pipe или ждёт сервер)')
        text = (out + err).decode('utf-8', 'replace')
        health = wait_health(self.port, 10)
        if health and health.get('pid'):
            self.server_pid = int(health['pid'])
        self.assertIsNotNone(health, 'сервер не ответил на /api/health за 10 с:\n' + text)
        self.assertEqual(proc.returncode, 0, text)
        self.assertIs(health.get('ok'), True, health)
        self.assertEqual(health.get('db'), os.path.join(self.tmp, 'fokus.db'))

        # повторный запуск при работающем сервере: только «открыть окно», новый сервер не стартует
        proc2 = run_app(APP_EXEC, env, timeout=15)
        self.assertEqual(proc2.returncode, 0, (proc2.stdout + proc2.stderr).decode('utf-8', 'replace'))
        self.assertEqual(fetch_health(self.port).get('pid'), health.get('pid'), 'pid сервера изменился')

        self._stop_server(health)
        self.assertIsNone(fetch_health(self.port, 0.5), 'сервер остался работать после остановки')


class LauncherHintTests(unittest.TestCase):
    """Fokus.command: подсказка про Fokus.app и остановка уже запущенного сервера."""

    def test_launcher_mentions_app_and_shutdown(self):
        with open(LAUNCHER, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('Fokus.app', text)
        self.assertIn('/api/shutdown', text)
        self.assertIn('[s]', text)


if __name__ == '__main__':
    unittest.main()
