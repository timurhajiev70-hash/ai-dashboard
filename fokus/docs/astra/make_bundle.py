#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает код Fokus в два markdown-файла для передачи внешнему аудитору (GPT Astra)
и архив fokus-for-astra.zip. Запуск: python3 docs/astra/make_bundle.py [папка_назначения]"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODE = ['AGENTS.md', 'README.md', 'engine.py', 'db.py', 'server.py', 'calendar_ics.py', 'static/index.html', 'i18n.py',
        'mcp_server.py', 'Fokus.command', 'Fokus.app/Contents/MacOS/Fokus', 'tools/make_icon.py']
TESTS = ['tests/test_reference.py', 'tests/test_engine.py', 'tests/test_db.py', 'tests/test_api.py',
         'tests/test_v2_db.py', 'tests/test_v2_api.py', 'tests/test_calendar.py',
         'tests/test_i18n.py', 'tests/test_launcher.py', 'tests/test_app_bundle.py', 'tests/test_mcp.py']
LANG = {'.py': 'python', '.md': 'markdown', '.html': 'html', '.command': 'bash', '': 'bash'}


def bundle(files, title, out_path):
    with open(out_path, 'w', encoding='utf-8') as out:
        out.write('# %s\n\nФайлы: %s\n\n' % (title, ', '.join('`%s`' % f for f in files)))
        for rel in files:
            with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
                text = f.read()
            lang = LANG.get(os.path.splitext(rel)[1], '')
            out.write('\n\n## FILE: %s\n\n```%s\n%s\n```\n' % (rel, lang, text.rstrip('\n')))


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'docs', 'astra')
    os.makedirs(dest, exist_ok=True)
    code_md = os.path.join(dest, 'fokus-code.md')
    tests_md = os.path.join(dest, 'fokus-tests.md')
    bundle(CODE, 'Fokus v1 — исходный код и спецификация', code_md)
    bundle(TESTS, 'Fokus v1 — тесты (написаны по спецификации, не видя код)', tests_md)
    zip_path = os.path.join(dest, 'fokus-for-astra.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(ROOT, 'docs', 'astra', 'PROMPT-ASTRA.md'), 'PROMPT-ASTRA.md')
        z.write(code_md, 'fokus-code.md')
        z.write(tests_md, 'fokus-tests.md')
        for rel in CODE + TESTS:
            z.write(os.path.join(ROOT, rel), 'fokus/' + rel)
    for p in (code_md, tests_md, zip_path):
        print('%s  %d KB' % (p, os.path.getsize(p) // 1024))


if __name__ == '__main__':
    main()
