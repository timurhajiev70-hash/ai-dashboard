# -*- coding: utf-8 -*-
"""Блок A v2 — тесты слоя данных (db.py) по AGENTS.md §4 v2 / §6 v2.

Миграция schema_version 2, таблица projects, колонки project/evaluated, проекты (CRUD),
«Входящие» (create_task без ответов), count_by_group с 7 ключами, count_by_project,
новые ключи настроек (theme, timezone, calendar_urls, calendar_refresh_min, default_project),
кэш календаря. Написаны ПО СПЕЦИФИКАЦИИ, без чтения кода. БД — db.connect(':memory:'),
время фиксировано: 2026-10-27T09:00:00. Данные обезличены (пациенты — только номера tomedo).
Запуск: cd fokus && python3 -m unittest tests.test_v2_db -v
"""
import datetime
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

NOW = datetime.datetime(2026, 10, 27, 9, 0, 0)
NOW_ISO = '2026-10-27T09:00:00'
TODAY = datetime.date(2026, 10, 27)
SLUG_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
GROUPS6 = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
GROUPS7 = GROUPS6 + ('inbox',)
PROJECT_KEYS = {'id', 'name', 'color', 'sort', 'archived', 'created_at', 'open_count'}
# id → (name, color, sort) — умолчания §4
DEFAULT_PROJECTS = {
    'praxis': ('Praxis', '#14b8a6', 0),
    'privat': ('Privat', '#22c55e', 1),
    'projekt': ('Projekt', '#a855f7', 2),
}
# заглушки ответов для «Входящих» (§4)
INBOX_ANSWERS = {'importance': 3, 'due': 'none', 'consequence': 'none', 'who': 'me',
                 'delegate_to': '', 'duration': '30m', 'depends_on': ''}
SETTINGS_V1_KEYS = {'weights', 'thresholds', 'day_limit', 'stale_days', 'carry_limit',
                    'language', 'last_rollover'}
SETTINGS_V2_DEFAULTS = {
    'schema_version': 2,
    'default_project': 'praxis',
    'theme': 'dark',
    'timezone': 'Europe/Berlin',
    'calendar_urls': [],
    'calendar_refresh_min': 15,
    'calendar_cache': None,
}
GOOGLE_URL = 'https://calendar.google.com/calendar/ical/abc%40gmail.com/private-0123456789abcdef/basic.ics'


def dt(day=27, hour=9, minute=0, month=10, year=2026):
    return datetime.datetime(year, month, day, hour, minute, 0)


def answers(importance=3, due='none', consequence='none', who='me',
            duration='30m', depends_on='', delegate_to=''):
    return {
        'importance': importance,
        'due': due,
        'consequence': consequence,
        'who': who,
        'delegate_to': delegate_to,
        'duration': duration,
        'depends_on': depends_on,
    }


def evaluation(score=50.0, group='priority', due_date=None, hints=None):
    return {'score': score, 'group': group, 'due_date': due_date, 'hints': list(hints or [])}


class DBCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')

    def tearDown(self):
        self.conn.close()

    def make(self, title='Задача', score=50.0, group='priority', due_date=None,
             now=NOW, note='', project=None, **ans):
        """Обычная задача (после мастера); project=None → сигнатура v1 без параметра."""
        if project is None:
            return db.create_task(self.conn, title, note, answers(**ans),
                                  evaluation(score, group, due_date), now)
        return db.create_task(self.conn, title, note, answers(**ans),
                              evaluation(score, group, due_date), now, project=project)

    def make_inbox(self, title='Быстрая запись', now=NOW, note='', project=None):
        """Задача во «Входящих»: answers=None и evaluation=None (§6 v2)."""
        if project is None:
            return db.create_task(self.conn, title, note, None, None, now)
        return db.create_task(self.conn, title, note, None, None, now, project=project)

    def events(self, task_id, event=None):
        rows = db.journal(self.conn, task_id=task_id)
        if event is not None:
            rows = [r for r in rows if r['event'] == event]
        return rows

    def project_ids(self, **kw):
        return [p['id'] for p in db.list_projects(self.conn, **kw)]


# ---------------------------------------------------------------------------
# §4 v2 / §6 v2 — миграция и схема
# ---------------------------------------------------------------------------
class TestMigration(DBCase):
    def test_schema_version_2(self):
        self.assertEqual(db.get_settings(self.conn)['schema_version'], 2)

    def test_migrate_returns_2_and_is_idempotent(self):
        self.assertEqual(db.migrate(self.conn), 2)
        self.assertEqual(db.migrate(self.conn), 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 3)
        self.assertEqual(db.get_settings(self.conn)['schema_version'], 2)

    def test_projects_table_with_defaults(self):
        names = set(r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertIn('projects', names)
        cols = set(r[1] for r in self.conn.execute('PRAGMA table_info(projects)'))
        self.assertTrue({'id', 'name', 'color', 'sort', 'archived', 'created_at'} <= cols, cols)
        rows = self.conn.execute('SELECT id, name, color, sort, archived FROM projects ORDER BY sort').fetchall()
        self.assertEqual([r[0] for r in rows], ['praxis', 'privat', 'projekt'])
        for r in rows:
            name, color, sort = DEFAULT_PROJECTS[r[0]]
            self.assertEqual((r[1], r[2], r[3], r[4]), (name, color, sort, 0), r[0])

    def test_tasks_columns_project_and_evaluated(self):
        cols = set(r[1] for r in self.conn.execute('PRAGMA table_info(tasks)'))
        self.assertIn('project', cols)
        self.assertIn('evaluated', cols)

    def test_default_settings_v2(self):
        s = db.get_settings(self.conn)
        self.assertTrue(SETTINGS_V1_KEYS <= set(s.keys()), SETTINGS_V1_KEYS - set(s.keys()))
        for key, value in SETTINGS_V2_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIn(key, s)
                self.assertEqual(s[key], value)

    def test_file_db_reconnect_keeps_version_and_no_duplicate_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fokus.db')
            conn = db.connect(path)
            try:
                db.create_project(conn, 'Haus', now=NOW)
                t = self.make_inbox_on(conn)
                self.assertEqual(db.get_settings(conn)['schema_version'], 2)
            finally:
                conn.close()
            conn2 = db.connect(path)
            try:
                self.assertEqual(db.get_settings(conn2)['schema_version'], 2)
                ids = [p['id'] for p in db.list_projects(conn2)]
                self.assertEqual(sorted(ids), ['haus', 'praxis', 'privat', 'projekt'])
                self.assertEqual(conn2.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 4)
                again = db.get_task(conn2, t['id'])
                self.assertEqual(again['group'], 'inbox')
                self.assertEqual(again['evaluated'], 0)
                # повторная миграция ничего не ломает
                self.assertEqual(db.migrate(conn2), 2)
                self.assertEqual(conn2.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 4)
            finally:
                conn2.close()

    def make_inbox_on(self, conn):
        return db.create_task(conn, 'Во входящих', '', None, None, NOW)

    def test_init_db_after_migration_keeps_projects(self):
        db.create_project(self.conn, 'Haus', now=NOW)
        db.init_db(self.conn)
        self.assertEqual(sorted(self.project_ids()), ['haus', 'praxis', 'privat', 'projekt'])
        self.assertEqual(db.get_settings(self.conn)['schema_version'], 2)


# ---------------------------------------------------------------------------
# §6 v2 — list_projects / get_project
# ---------------------------------------------------------------------------
class TestListProjects(DBCase):
    def test_defaults_order_and_keys(self):
        projects = db.list_projects(self.conn)
        self.assertEqual([p['id'] for p in projects], ['praxis', 'privat', 'projekt'])
        for p in projects:
            self.assertTrue(PROJECT_KEYS <= set(p.keys()), PROJECT_KEYS - set(p.keys()))
            name, color, sort = DEFAULT_PROJECTS[p['id']]
            self.assertEqual(p['name'], name)
            self.assertEqual(p['color'], color)
            self.assertEqual(p['sort'], sort)
            self.assertEqual(p['archived'], 0)
            self.assertEqual(p['open_count'], 0)
            self.assertIsInstance(p['created_at'], str)

    def test_open_count_counts_only_open(self):
        self.make('A', project='praxis')
        self.make('B', project='praxis')
        done = self.make('C', project='praxis')
        deleted = self.make('D', project='praxis')
        self.make('E', project='privat')
        self.make_inbox('F', project='privat')
        db.set_done(self.conn, done['id'], dt(hour=10))
        db.delete_task(self.conn, deleted['id'], dt(hour=10))
        counts = dict((p['id'], p['open_count']) for p in db.list_projects(self.conn))
        self.assertEqual(counts, {'praxis': 2, 'privat': 2, 'projekt': 0})

    def test_include_archived(self):
        db.update_project(self.conn, 'projekt', archived=True, now=NOW)
        self.assertEqual(self.project_ids(), ['praxis', 'privat'])
        self.assertEqual(self.project_ids(include_archived=False), ['praxis', 'privat'])
        self.assertEqual(self.project_ids(include_archived=True), ['praxis', 'privat', 'projekt'])

    def test_get_project(self):
        p = db.get_project(self.conn, 'privat')
        self.assertIsNotNone(p)
        self.assertEqual(p['id'], 'privat')
        self.assertEqual(p['name'], 'Privat')
        self.assertEqual(p['color'], '#22c55e')
        self.assertIsNone(db.get_project(self.conn, 'nope'))

    def test_list_is_sorted_by_sort(self):
        db.create_project(self.conn, 'Zuletzt', now=NOW)
        db.update_project(self.conn, 'praxis', sort=10, now=NOW)
        ids = self.project_ids()
        self.assertEqual(ids[-1], 'praxis')
        sorts = [p['sort'] for p in db.list_projects(self.conn)]
        self.assertEqual(sorts, sorted(sorts))


# ---------------------------------------------------------------------------
# §6 v2 — create_project
# ---------------------------------------------------------------------------
class TestCreateProject(DBCase):
    def test_latin_name(self):
        p = db.create_project(self.conn, 'Haus', now=NOW)
        self.assertEqual(p['id'], 'haus')
        self.assertEqual(p['name'], 'Haus')
        self.assertEqual(p['color'], '#8b8b8b')
        self.assertEqual(p['archived'], 0)
        self.assertIsInstance(p['sort'], int)
        self.assertEqual(p['created_at'], NOW_ISO)
        self.assertEqual(p.get('open_count', 0), 0)
        self.assertIn('haus', self.project_ids())
        self.assertEqual(db.get_project(self.conn, 'haus')['name'], 'Haus')

    def test_explicit_color(self):
        p = db.create_project(self.conn, 'Garten', color='#ff8800', now=NOW)
        self.assertEqual(p['color'], '#ff8800')

    def test_cyrillic_name_transliterated(self):
        p = db.create_project(self.conn, 'Налоги', now=NOW)
        self.assertEqual(p['name'], 'Налоги')
        self.assertEqual(p['id'], 'nalogi')
        self.assertRegex(p['id'], SLUG_RE)

    def test_slug_always_valid(self):
        for name in ('Mein Haus', 'Steuer 2026', 'Ärzte & Co', 'Проект Икс', 'QM/Hygiene', '  Kita  ',
                     'a' * 40, 'Ö', '東京'):
            with self.subTest(name=name):
                p = db.create_project(self.conn, name, now=NOW)
                self.assertRegex(p['id'], SLUG_RE, name)
                self.assertLessEqual(len(p['id']), 32)

    def test_symbols_only_falls_back_to_p_n(self):
        p = db.create_project(self.conn, '!!!', now=NOW)
        self.assertRegex(p['id'], r'^p-\d+$')
        self.assertEqual(p['name'], '!!!')

    def test_explicit_project_id(self):
        p = db.create_project(self.conn, 'Eigene Kennung', now=NOW, project_id='my_id-1')
        self.assertEqual(p['id'], 'my_id-1')
        self.assertEqual(db.get_project(self.conn, 'my_id-1')['name'], 'Eigene Kennung')

    def test_explicit_project_id_invalid(self):
        for bad in ('Bad Slug!', 'UPPER', 'a' * 33, '', 'с-кириллицей'):
            with self.subTest(project_id=bad):
                with self.assertRaises(ValueError) as cm:
                    db.create_project(self.conn, 'X', now=NOW, project_id=bad)
                self.assertEqual(cm.exception.args[0], 'id')

    def test_explicit_project_id_taken(self):
        with self.assertRaises(ValueError) as cm:
            db.create_project(self.conn, 'Noch ein Praxis', now=NOW, project_id='praxis')
        self.assertEqual(cm.exception.args[0], 'id')
        self.assertEqual(db.get_project(self.conn, 'praxis')['name'], 'Praxis')

    def test_duplicate_names_never_share_an_id(self):
        first = db.create_project(self.conn, 'Haus', now=NOW)
        try:
            second = db.create_project(self.conn, 'Haus', now=dt(hour=10))
        except ValueError:
            second = None   # отказ — допустимо; главное — не два проекта с одним id
        if second is not None:
            self.assertNotEqual(first['id'], second['id'])
            self.assertRegex(second['id'], SLUG_RE)
        ids = self.project_ids(include_archived=True)
        self.assertEqual(len(ids), len(set(ids)))

    def test_name_validation(self):
        for bad in ('', '   ', 'x' * 41, None, 42):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError) as cm:
                    db.create_project(self.conn, bad, now=NOW)
                self.assertEqual(cm.exception.args[0], 'name')
        ok = db.create_project(self.conn, 'y' * 40, now=NOW)
        self.assertEqual(len(ok['name']), 40)
        self.assertEqual(len(self.project_ids()), 4)

    def test_color_validation(self):
        for bad in ('red', '#FFF', '#12345g', '14b8a6', '#1234567', 'rgb(1,2,3)', 123):
            with self.subTest(color=bad):
                with self.assertRaises(ValueError) as cm:
                    db.create_project(self.conn, 'Farbe', color=bad, now=NOW)
                self.assertEqual(cm.exception.args[0], 'color')
        self.assertEqual(len(self.project_ids()), 3, 'неудачное создание ничего не добавляет')
        ok = db.create_project(self.conn, 'Farbe', color='#aabbcc', now=NOW)
        self.assertEqual(ok['color'], '#aabbcc')

    def test_now_none_uses_current_time(self):
        p = db.create_project(self.conn, 'Jetzt')
        datetime.datetime.strptime(p['created_at'][:19], '%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# §6 v2 — update_project
# ---------------------------------------------------------------------------
class TestUpdateProject(DBCase):
    def test_rename(self):
        p = db.update_project(self.conn, 'privat', name='Zuhause', now=NOW)
        self.assertEqual(p['id'], 'privat', 'slug при переименовании не меняется')
        self.assertEqual(p['name'], 'Zuhause')
        self.assertEqual(db.get_project(self.conn, 'privat')['name'], 'Zuhause')

    def test_color_and_sort(self):
        p = db.update_project(self.conn, 'privat', color='#000000', sort=7, now=NOW)
        self.assertEqual(p['color'], '#000000')
        self.assertEqual(p['sort'], 7)
        self.assertEqual(p['name'], 'Privat', 'остальные поля не трогаются')

    def test_archive_and_unarchive(self):
        p = db.update_project(self.conn, 'projekt', archived=True, now=NOW)
        self.assertEqual(p['archived'], 1)
        self.assertNotIn('projekt', self.project_ids())
        self.assertIn('projekt', self.project_ids(include_archived=True))
        p = db.update_project(self.conn, 'projekt', archived=False, now=NOW)
        self.assertEqual(p['archived'], 0)
        self.assertIn('projekt', self.project_ids())

    def test_archive_int_flag(self):
        self.assertEqual(db.update_project(self.conn, 'projekt', archived=1, now=NOW)['archived'], 1)
        self.assertEqual(db.update_project(self.conn, 'projekt', archived=0, now=NOW)['archived'], 0)

    def test_praxis_cannot_be_archived(self):
        with self.assertRaises(ValueError):
            db.update_project(self.conn, 'praxis', archived=True, now=NOW)
        self.assertEqual(db.get_project(self.conn, 'praxis')['archived'], 0)
        self.assertIn('praxis', self.project_ids())

    def test_praxis_can_be_renamed_and_recolored(self):
        p = db.update_project(self.conn, 'praxis', name='Praxis Nord', color='#111111', now=NOW)
        self.assertEqual((p['name'], p['color']), ('Praxis Nord', '#111111'))

    def test_unknown_project(self):
        with self.assertRaises(LookupError):
            db.update_project(self.conn, 'nope', name='X', now=NOW)

    def test_invalid_values(self):
        for kw, code in (({'name': ''}, 'name'), ({'name': 'x' * 41}, 'name'),
                         ({'color': 'blue'}, 'color'), ({'color': '#12'}, 'color')):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError) as cm:
                    db.update_project(self.conn, 'privat', now=NOW, **kw)
                self.assertEqual(cm.exception.args[0], code)
        p = db.get_project(self.conn, 'privat')
        self.assertEqual((p['name'], p['color']), ('Privat', '#22c55e'))

    def test_noop_update_returns_project(self):
        p = db.update_project(self.conn, 'privat', now=NOW)
        self.assertEqual(p['id'], 'privat')
        self.assertEqual(p['name'], 'Privat')

    def test_task_sees_new_project_name(self):
        t = self.make('A', project='privat')
        db.update_project(self.conn, 'privat', name='Zuhause', color='#010203', now=NOW)
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['project_name'], 'Zuhause')
        self.assertEqual(g['project_color'], '#010203')


# ---------------------------------------------------------------------------
# §6 v2 — delete_project
# ---------------------------------------------------------------------------
class TestDeleteProject(DBCase):
    def test_delete_empty(self):
        db.create_project(self.conn, 'Temp', now=NOW)
        self.assertIsNone(db.delete_project(self.conn, 'temp'))
        self.assertIsNone(db.get_project(self.conn, 'temp'))
        self.assertNotIn('temp', self.project_ids(include_archived=True))

    def test_delete_with_open_task(self):
        db.create_project(self.conn, 'Temp', now=NOW)
        self.make('A', project='temp')
        with self.assertRaises(ValueError) as cm:
            db.delete_project(self.conn, 'temp')
        self.assertEqual(cm.exception.args[0], 'not_empty')
        self.assertIsNotNone(db.get_project(self.conn, 'temp'))

    def test_delete_with_done_task(self):
        db.create_project(self.conn, 'Temp', now=NOW)
        t = self.make('A', project='temp')
        db.set_done(self.conn, t['id'], dt(hour=10))
        with self.assertRaises(ValueError) as cm:
            db.delete_project(self.conn, 'temp')
        self.assertEqual(cm.exception.args[0], 'not_empty')

    def test_delete_with_inbox_task(self):
        db.create_project(self.conn, 'Temp', now=NOW)
        self.make_inbox('A', project='temp')
        with self.assertRaises(ValueError) as cm:
            db.delete_project(self.conn, 'temp')
        self.assertEqual(cm.exception.args[0], 'not_empty')

    def test_delete_after_moving_task_out(self):
        db.create_project(self.conn, 'Temp', now=NOW)
        t = self.make('A', project='temp')
        db.set_project(self.conn, t['id'], 'praxis', dt(hour=10))
        db.delete_project(self.conn, 'temp')
        self.assertIsNone(db.get_project(self.conn, 'temp'))
        self.assertEqual(db.get_task(self.conn, t['id'])['project'], 'praxis')

    def test_praxis_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            db.delete_project(self.conn, 'praxis')
        self.assertIsNotNone(db.get_project(self.conn, 'praxis'))

    def test_unknown_project(self):
        with self.assertRaises(LookupError):
            db.delete_project(self.conn, 'nope')

    def test_default_projects_privat_projekt_deletable_when_empty(self):
        db.delete_project(self.conn, 'projekt')
        self.assertEqual(self.project_ids(), ['praxis', 'privat'])


# ---------------------------------------------------------------------------
# §6 v2 — set_project
# ---------------------------------------------------------------------------
class TestSetProject(DBCase):
    def test_move_and_journal(self):
        t = self.make('Отчёт по пациенту tomedo #48213')
        self.assertEqual(t['project'], 'praxis')
        r = db.set_project(self.conn, t['id'], 'privat', dt(hour=10))
        self.assertEqual(r['project'], 'privat')
        self.assertEqual(r['project_name'], 'Privat')
        self.assertEqual(r['project_color'], '#22c55e')
        self.assertEqual(r['updated_at'], dt(hour=10).isoformat())
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['project'], 'privat')
        rows = self.events(t['id'], 'project_change')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'praxis')
        self.assertEqual(rows[0]['new_value'], 'privat')
        self.assertEqual(rows[0]['ts'], dt(hour=10).isoformat())

    def test_unknown_project(self):
        t = self.make('A')
        with self.assertRaises(ValueError):
            db.set_project(self.conn, t['id'], 'nope', dt(hour=10))
        self.assertEqual(db.get_task(self.conn, t['id'])['project'], 'praxis')
        self.assertEqual(self.events(t['id'], 'project_change'), [])

    def test_unknown_task(self):
        with self.assertRaises(LookupError):
            db.set_project(self.conn, 999999, 'privat', dt(hour=10))

    def test_inbox_task_can_change_project(self):
        t = self.make_inbox('Быстрая')
        r = db.set_project(self.conn, t['id'], 'projekt', dt(hour=10))
        self.assertEqual(r['project'], 'projekt')
        self.assertEqual(r['group'], 'inbox')
        self.assertEqual(r['evaluated'], 0)

    def test_counts_follow_project_change(self):
        t = self.make('A')
        db.set_project(self.conn, t['id'], 'privat', dt(hour=10))
        c = db.count_by_project(self.conn)
        self.assertEqual(c.get('praxis', 0), 0)
        self.assertEqual(c['privat'], 1)


# ---------------------------------------------------------------------------
# §6 v2 — create_task с проектом, list_tasks(project=), count_by_project
# ---------------------------------------------------------------------------
class TestCreateTaskProject(DBCase):
    def test_default_project_praxis_and_evaluated(self):
        t = self.make('A')
        self.assertEqual(t['project'], 'praxis')
        self.assertEqual(t['project_name'], 'Praxis')
        self.assertEqual(t['project_color'], '#14b8a6')
        self.assertEqual(t['evaluated'], 1)
        self.assertEqual(t['group'], 'priority')

    def test_explicit_project(self):
        t = self.make('B', project='privat')
        self.assertEqual(t['project'], 'privat')
        self.assertEqual(t['project_name'], 'Privat')
        self.assertEqual(t['project_color'], '#22c55e')
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['project'], 'privat')
        self.assertEqual(g['project_name'], 'Privat')

    def test_custom_project(self):
        db.create_project(self.conn, 'Haus', color='#123456', now=NOW)
        t = self.make('C', project='haus')
        self.assertEqual((t['project'], t['project_name'], t['project_color']), ('haus', 'Haus', '#123456'))

    def test_unknown_project(self):
        with self.assertRaises(ValueError) as cm:
            self.make('D', project='nope')
        self.assertEqual(cm.exception.args[0], 'project')
        self.assertEqual(db.list_tasks(self.conn), [])

    def test_list_tasks_filter_by_project(self):
        a = self.make('A', project='praxis')
        b = self.make('B', project='privat')
        c = self.make_inbox('C', project='privat')
        self.assertEqual(set(t['id'] for t in db.list_tasks(self.conn, project='praxis')), {a['id']})
        self.assertEqual(set(t['id'] for t in db.list_tasks(self.conn, project='privat')), {b['id'], c['id']})
        self.assertEqual(db.list_tasks(self.conn, project='projekt'), [])
        self.assertEqual(set(t['id'] for t in db.list_tasks(self.conn)), {a['id'], b['id'], c['id']})
        # комбинация с группой
        self.assertEqual([t['id'] for t in db.list_tasks(self.conn, project='privat', group='inbox')], [c['id']])
        self.assertEqual([t['id'] for t in db.list_tasks(self.conn, project='privat', group='priority')], [b['id']])

    def test_list_rows_have_project_fields(self):
        self.make('A', project='privat')
        for r in db.list_tasks(self.conn):
            for key in ('project', 'project_name', 'project_color', 'evaluated'):
                self.assertIn(key, r.keys())

    def test_count_by_project_open_only(self):
        self.make('A', project='praxis')
        self.make('B', project='praxis')
        d = self.make('C', project='praxis')
        e = self.make('D', project='privat')
        self.make_inbox('E', project='privat')
        db.set_done(self.conn, d['id'], dt(hour=10))
        db.delete_task(self.conn, e['id'], dt(hour=10))
        c = db.count_by_project(self.conn)
        self.assertIsInstance(c, dict)
        self.assertEqual(c['praxis'], 2)
        self.assertEqual(c['privat'], 1)
        self.assertEqual(c.get('projekt', 0), 0)

    def test_count_by_project_empty(self):
        c = db.count_by_project(self.conn)
        self.assertIsInstance(c, dict)
        self.assertEqual(sum(c.values()), 0)


# ---------------------------------------------------------------------------
# §4 v2 / §6 v2 — «Входящие»
# ---------------------------------------------------------------------------
class TestInbox(DBCase):
    def test_create_inbox_fields(self):
        t = self.make_inbox('Позвонить в банк')
        self.assertEqual(t['title'], 'Позвонить в банк')
        self.assertEqual(t['group'], 'inbox')
        self.assertEqual(t['evaluated'], 0)
        self.assertEqual(t['score'], 0.0)
        self.assertIsNone(t['due_date'])
        self.assertEqual(t['status'], 'open')
        self.assertEqual(t['project'], 'praxis')
        self.assertEqual(t['project_name'], 'Praxis')
        for key, value in INBOX_ANSWERS.items():
            with self.subTest(key=key):
                self.assertEqual(t[key], value)
        self.assertEqual(t['group_manual'], 0)
        self.assertEqual(t['needs_review'], 0)
        self.assertEqual(t['is_today'], 0)
        self.assertEqual(t['carry_count'], 0)
        self.assertEqual(t['created_at'], NOW_ISO)
        self.assertEqual(t['group_since'], NOW_ISO)
        self.assertNotIn('grp', t)

    def test_create_inbox_with_project_and_note(self):
        t = self.make_inbox('Купить лампочку', project='privat', note='для коридора')
        self.assertEqual(t['project'], 'privat')
        self.assertEqual(t['note'], 'для коридора')
        self.assertEqual(t['group'], 'inbox')

    def test_create_inbox_unknown_project(self):
        with self.assertRaises(ValueError) as cm:
            self.make_inbox('X', project='nope')
        self.assertEqual(cm.exception.args[0], 'project')

    def test_journal_create_inbox(self):
        t = self.make_inbox()
        rows = self.events(t['id'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event'], 'create')
        self.assertEqual(rows[0]['old_value'], '')
        self.assertEqual(rows[0]['new_value'], 'inbox')

    def test_get_task_inbox(self):
        t = self.make_inbox()
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['group'], 'inbox')
        self.assertEqual(g['evaluated'], 0)
        self.assertEqual(g['score'], 0.0)

    def test_list_tasks_group_inbox(self):
        a = self.make_inbox('A', now=dt(hour=9))
        b = self.make_inbox('B', now=dt(hour=10))
        c = self.make('C', group='now', score=70.0)
        inbox = db.list_tasks(self.conn, group='inbox')
        self.assertEqual(set(t['id'] for t in inbox), {a['id'], b['id']})
        self.assertTrue(all(t['group'] == 'inbox' and t['evaluated'] == 0 for t in inbox))
        self.assertEqual([t['id'] for t in db.list_tasks(self.conn, group='now')], [c['id']])
        self.assertEqual([t['id'] for t in db.list_tasks(self.conn, group='inbox', sort='created')],
                         [b['id'], a['id']])

    def test_inbox_included_in_open_list_and_sorted_last_by_score(self):
        i = self.make_inbox('I')
        n = self.make('N', group='now', score=70.0)
        s = self.make('S', group='someday', score=13.3)
        rows = db.list_tasks(self.conn)
        self.assertEqual([t['id'] for t in rows], [n['id'], s['id'], i['id']])

    def test_regular_task_evaluated_1(self):
        t = self.make('A', group='queue', score=30.0)
        self.assertEqual(t['evaluated'], 1)
        self.assertNotEqual(t['group'], 'inbox')

    def test_apply_evaluation_leaves_inbox(self):
        t = self.make_inbox('Позвонить в налоговую')
        r = db.apply_evaluation(self.conn, t['id'],
                                answers(importance=5, due='today', consequence='serious'),
                                evaluation(98.5, 'now', '2026-10-27'), dt(hour=10))
        self.assertEqual(r['group'], 'now')
        self.assertEqual(r['evaluated'], 1)
        self.assertEqual(r['score'], 98.5)
        self.assertEqual(r['due_date'], '2026-10-27')
        self.assertEqual(r['importance'], 5)
        self.assertEqual(r['due'], 'today')
        self.assertEqual(r['consequence'], 'serious')
        self.assertEqual(r['evaluated_at'], dt(hour=10).isoformat())
        self.assertEqual(r['group_since'], dt(hour=10).isoformat())
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['group'], 'now')
        self.assertEqual(g['evaluated'], 1)
        rows = self.events(t['id'], 'evaluate')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['old_value'].startswith('inbox'), rows[0]['old_value'])
        self.assertEqual(rows[0]['new_value'], 'now/98.5')
        self.assertEqual(db.list_tasks(self.conn, group='inbox'), [])
        self.assertEqual(db.count_by_group(self.conn)['inbox'], 0)
        self.assertEqual(db.count_by_group(self.conn)['now'], 1)

    def test_apply_evaluation_keeps_project(self):
        t = self.make_inbox('A', project='privat')
        r = db.apply_evaluation(self.conn, t['id'], answers(), evaluation(26.0, 'queue'), dt(hour=10))
        self.assertEqual(r['project'], 'privat')
        self.assertEqual(r['group'], 'queue')

    def test_inbox_can_be_done_reopened_deleted(self):
        t = self.make_inbox('A')
        self.assertEqual(db.set_done(self.conn, t['id'], dt(hour=10))['status'], 'done')
        self.assertEqual(db.count_by_group(self.conn)['inbox'], 0)
        self.assertEqual(db.reopen_task(self.conn, t['id'], dt(hour=11))['status'], 'open')
        self.assertEqual(db.count_by_group(self.conn)['inbox'], 1)
        self.assertEqual(db.delete_task(self.conn, t['id'], dt(hour=12))['status'], 'deleted')
        self.assertEqual(db.count_by_group(self.conn)['inbox'], 0)
        self.assertEqual(db.list_tasks(self.conn, group='inbox'), [])

    def test_edit_inbox_title(self):
        t = self.make_inbox('Опечатка')
        r = db.edit_task(self.conn, t['id'], title='Исправлено', now=dt(hour=10))
        self.assertEqual(r['title'], 'Исправлено')
        self.assertEqual(r['group'], 'inbox')

    def test_today_tasks_without_inbox(self):
        self.make_inbox('A')
        self.assertEqual(db.today_tasks(self.conn, TODAY), [])


# ---------------------------------------------------------------------------
# §6 v2 — count_by_group: 7 ключей
# ---------------------------------------------------------------------------
class TestCountByGroup7(DBCase):
    def test_empty_seven_zero_keys(self):
        c = db.count_by_group(self.conn)
        self.assertEqual(set(c.keys()), set(GROUPS7))
        self.assertEqual(set(c.values()), {0})

    def test_counts_with_inbox(self):
        self.make('A', group='now', score=70.0)
        self.make_inbox('B')
        self.make_inbox('C')
        d = self.make_inbox('D')
        db.set_done(self.conn, d['id'], dt(hour=10))
        c = db.count_by_group(self.conn)
        self.assertEqual(c, {'now': 1, 'priority': 0, 'queue': 0, 'delegate': 0,
                             'someday': 0, 'waiting': 0, 'inbox': 2})


# ---------------------------------------------------------------------------
# §4.2 v2 — новые ключи настроек
# ---------------------------------------------------------------------------
class TestSettingsV2(DBCase):
    def assertSettingsError(self, patch, key):
        before = db.get_settings(self.conn)
        with self.assertRaises(ValueError) as cm:
            db.update_settings(self.conn, patch)
        self.assertEqual(cm.exception.args[0], key)
        self.assertEqual(db.get_settings(self.conn), before, 'неудачный patch ничего не меняет')

    def test_theme(self):
        self.assertEqual(db.update_settings(self.conn, {'theme': 'light'})['theme'], 'light')
        self.assertEqual(db.get_settings(self.conn)['theme'], 'light')
        self.assertEqual(db.update_settings(self.conn, {'theme': 'dark'})['theme'], 'dark')
        for bad in ('blue', 'Light', '', 1, None):
            with self.subTest(theme=bad):
                self.assertSettingsError({'theme': bad}, 'theme')

    def test_timezone(self):
        for tz in ('Europe/Berlin', 'UTC', 'America/New_York', 'Asia/Tokyo', 'Europe/Moscow'):
            with self.subTest(tz=tz):
                self.assertEqual(db.update_settings(self.conn, {'timezone': tz})['timezone'], tz)
        for bad in ('Mars/Olympus', '', 'CET+1', 'Europe/Berlin; DROP', 5, None, '../../etc/passwd'):
            with self.subTest(tz=bad):
                self.assertSettingsError({'timezone': bad}, 'timezone')

    def test_calendar_urls_ok(self):
        r = db.update_settings(self.conn, {'calendar_urls': [GOOGLE_URL]})
        self.assertEqual(r['calendar_urls'], [GOOGLE_URL])
        self.assertEqual(db.get_settings(self.conn)['calendar_urls'], [GOOGLE_URL])
        two = [GOOGLE_URL, 'https://example.invalid/other.ics']
        self.assertEqual(db.update_settings(self.conn, {'calendar_urls': two})['calendar_urls'], two)
        self.assertEqual(db.update_settings(self.conn, {'calendar_urls': []})['calendar_urls'], [])

    def test_calendar_urls_ten_ok_eleven_rejected(self):
        ten = ['https://example.invalid/cal%d.ics' % i for i in range(10)]
        self.assertEqual(len(db.update_settings(self.conn, {'calendar_urls': ten})['calendar_urls']), 10)
        self.assertSettingsError({'calendar_urls': ten + ['https://example.invalid/cal10.ics']}, 'calendar_urls')

    def test_calendar_urls_invalid(self):
        for bad in (['http://calendar.google.com/calendar/ical/x/basic.ics'],
                    ['ftp://example.invalid/x.ics'],
                    ['calendar.google.com/basic.ics'],
                    [''],
                    [123],
                    [None],
                    GOOGLE_URL,
                    'not a list',
                    42,
                    None,
                    {'url': GOOGLE_URL},
                    ['https://example.invalid/' + 'a' * 2048],
                    [GOOGLE_URL, 'http://example.invalid/b.ics']):
            with self.subTest(value=bad):
                self.assertSettingsError({'calendar_urls': bad}, 'calendar_urls')

    def test_calendar_url_max_length_2048(self):
        url = 'https://example.invalid/' + 'a' * (2048 - len('https://example.invalid/'))
        self.assertEqual(len(url), 2048)
        self.assertEqual(db.update_settings(self.conn, {'calendar_urls': [url]})['calendar_urls'], [url])

    def test_calendar_refresh_min(self):
        self.assertEqual(db.update_settings(self.conn, {'calendar_refresh_min': 5})['calendar_refresh_min'], 5)
        self.assertEqual(db.update_settings(self.conn, {'calendar_refresh_min': 1440})['calendar_refresh_min'], 1440)
        self.assertEqual(db.update_settings(self.conn, {'calendar_refresh_min': 60})['calendar_refresh_min'], 60)
        for bad in (4, 1441, 0, -5, 'x', None, True):
            with self.subTest(value=bad):
                self.assertSettingsError({'calendar_refresh_min': bad}, 'calendar_refresh_min')

    def test_default_project(self):
        self.assertEqual(db.update_settings(self.conn, {'default_project': 'privat'})['default_project'], 'privat')
        self.assertEqual(db.get_settings(self.conn)['default_project'], 'privat')
        db.create_project(self.conn, 'Haus', now=NOW)
        self.assertEqual(db.update_settings(self.conn, {'default_project': 'haus'})['default_project'], 'haus')
        for bad in ('nope', '', None, 7):
            with self.subTest(value=bad):
                self.assertSettingsError({'default_project': bad}, 'default_project')

    def test_default_project_archived_rejected(self):
        db.update_project(self.conn, 'projekt', archived=True, now=NOW)
        self.assertSettingsError({'default_project': 'projekt'}, 'default_project')

    def test_service_keys_ignored_in_patch(self):
        before = db.get_settings(self.conn)
        r = db.update_settings(self.conn, {'schema_version': 99, 'calendar_cache': {'events': [1]},
                                           'last_rollover': '2020-01-01', 'theme': 'light'})
        self.assertEqual(r['schema_version'], 2)
        self.assertEqual(r['calendar_cache'], before['calendar_cache'])
        self.assertEqual(r['last_rollover'], before['last_rollover'])
        self.assertEqual(r['theme'], 'light')
        s = db.get_settings(self.conn)
        self.assertEqual(s['schema_version'], 2)
        self.assertEqual(s['calendar_cache'], before['calendar_cache'])

    def test_only_service_keys_is_noop(self):
        before = db.get_settings(self.conn)
        self.assertEqual(db.update_settings(self.conn, {'schema_version': 1}), before)
        self.assertEqual(db.update_settings(self.conn, {'calendar_cache': None}), before)

    def test_atomic_mixed_patch(self):
        self.assertSettingsError({'theme': 'light', 'timezone': 'Nope/Zone'}, 'timezone')
        self.assertEqual(db.get_settings(self.conn)['theme'], 'dark')

    def test_v1_keys_still_work_with_v2(self):
        r = db.update_settings(self.conn, {'day_limit': 5, 'theme': 'light', 'calendar_refresh_min': 30})
        self.assertEqual((r['day_limit'], r['theme'], r['calendar_refresh_min']), (5, 'light', 30))
        self.assertEqual(r['weights'], {'importance': 0.45, 'urgency': 0.30, 'consequence': 0.20, 'duration': 0.05})

    def test_update_returns_all_keys(self):
        r = db.update_settings(self.conn, {'theme': 'light'})
        expected = SETTINGS_V1_KEYS | set(SETTINGS_V2_DEFAULTS)
        self.assertTrue(expected <= set(r.keys()), expected - set(r.keys()))


# ---------------------------------------------------------------------------
# §6 v2 / §12 — calendar_cache_get / calendar_cache_set
# ---------------------------------------------------------------------------
class TestCalendarCache(DBCase):
    PAYLOAD = {
        'events': [
            {'uid': 'a1', 'summary': 'Teambesprechung', 'location': 'Raum 2',
             'start': '2026-10-27T09:00', 'end': '2026-10-27T09:30', 'all_day': False, 'calendar': 0},
            {'uid': 'a2', 'summary': 'Urlaub', 'location': '',
             'start': '2026-10-26', 'end': '2026-10-28', 'all_day': True, 'calendar': 1},
        ],
        'fetched_at': '2026-10-27T08:55:00',
        'errors': [{'url_index': 2, 'message': 'timed out'}],
        'window': ['2026-10-20', '2026-12-26'],
    }

    def test_initially_none(self):
        self.assertIsNone(db.calendar_cache_get(self.conn))
        self.assertIsNone(db.get_settings(self.conn)['calendar_cache'])

    def test_set_then_get(self):
        db.calendar_cache_set(self.conn, self.PAYLOAD)
        got = db.calendar_cache_get(self.conn)
        self.assertEqual(got, self.PAYLOAD)
        self.assertEqual(json.loads(json.dumps(got)), self.PAYLOAD)
        self.assertEqual(db.get_settings(self.conn)['calendar_cache'], self.PAYLOAD)

    def test_overwrite(self):
        db.calendar_cache_set(self.conn, self.PAYLOAD)
        newer = dict(self.PAYLOAD, events=[], fetched_at='2026-10-27T09:10:00', errors=[])
        db.calendar_cache_set(self.conn, newer)
        self.assertEqual(db.calendar_cache_get(self.conn), newer)

    def test_survives_other_settings_update(self):
        db.calendar_cache_set(self.conn, self.PAYLOAD)
        db.update_settings(self.conn, {'theme': 'light', 'calendar_refresh_min': 30})
        self.assertEqual(db.calendar_cache_get(self.conn), self.PAYLOAD)

    def test_cannot_be_set_via_update_settings(self):
        db.calendar_cache_set(self.conn, self.PAYLOAD)
        db.update_settings(self.conn, {'calendar_cache': {'events': []}})
        self.assertEqual(db.calendar_cache_get(self.conn), self.PAYLOAD)

    def test_empty_payload(self):
        empty = {'events': [], 'fetched_at': '2026-10-27T09:00:00', 'errors': [], 'window': ['2026-10-20', '2026-12-26']}
        db.calendar_cache_set(self.conn, empty)
        self.assertEqual(db.calendar_cache_get(self.conn), empty)


if __name__ == '__main__':
    unittest.main()
