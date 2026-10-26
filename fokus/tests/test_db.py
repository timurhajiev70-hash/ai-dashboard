# -*- coding: utf-8 -*-
"""Блок A — тесты слоя данных (db.py) по AGENTS.md §4 и §6.

Тесты написаны ПО СПЕЦИФИКАЦИИ, без чтения кода. Не зависят от engine:
evaluation собирается вручную как dict {'score', 'group', 'due_date', 'hints'}.
БД — db.connect(':memory:'), время фиксировано: 2026-10-25T09:00:00.
Данные обезличены (пациенты — только номера tomedo).
Запуск: cd fokus && python3 -m unittest discover -s tests -t .
"""
import datetime
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

NOW = datetime.datetime(2026, 10, 25, 9, 0, 0)
NOW_ISO = '2026-10-25T09:00:00'
TODAY = datetime.date(2026, 10, 25)
YESTERDAY = datetime.date(2026, 10, 24)
TOMORROW = datetime.date(2026, 10, 26)

TASK_COLUMNS = {
    'id', 'title', 'note', 'importance', 'due', 'due_date', 'consequence', 'who',
    'delegate_to', 'duration', 'depends_on', 'score', 'grp', 'group_manual',
    'group_since', 'evaluated_at', 'needs_review', 'review_reason', 'is_today',
    'today_date', 'carry_count', 'status', 'done_at', 'created_at', 'updated_at',
    'project', 'evaluated',   # v2: §4 — проект задачи и флаг «мастер пройден»
}
JOURNAL_COLUMNS = {'id', 'task_id', 'ts', 'event', 'old_value', 'new_value', 'note'}
GROUPS = ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting')
GROUPS_V2 = GROUPS + ('inbox',)   # v2: count_by_group → 7 ключей (§6)
SETTINGS_KEYS = {'weights', 'thresholds', 'day_limit', 'stale_days', 'carry_limit',
                 'language', 'last_rollover',
                 # v2: §4.2 — новые ключи настроек
                 'schema_version', 'default_project', 'theme', 'timezone',
                 'calendar_urls', 'calendar_refresh_min', 'calendar_cache'}
DEFAULT_WEIGHTS = {'importance': 0.45, 'urgency': 0.30, 'consequence': 0.20, 'duration': 0.05}
DEFAULT_THRESHOLDS = {'now': 65, 'priority': 45, 'queue': 25}


def dt(day=25, hour=9, minute=0, month=10, year=2026):
    return datetime.datetime(year, month, day, hour, minute, 0)


def answers(importance=3, due='none', consequence='none', who='me',
            duration='30m', depends_on='', delegate_to=''):
    """Нормализованные ответы мастера (§3), как их отдаёт engine.validate_answers."""
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
    """Результат мастера в формате engine.evaluate, собранный вручную."""
    return {'score': score, 'group': group, 'due_date': due_date, 'hints': list(hints or [])}


class DBCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')

    def tearDown(self):
        self.conn.close()

    def make(self, title='Задача', score=50.0, group='priority', due_date=None,
             now=NOW, note='', **ans):
        return db.create_task(self.conn, title, note, answers(**ans),
                              evaluation(score, group, due_date), now)

    def events(self, task_id, event=None):
        rows = db.journal(self.conn, task_id=task_id)
        if event is not None:
            rows = [r for r in rows if r['event'] == event]
        return rows

    def ids(self, rows):
        return [r['id'] for r in rows]


# ---------------------------------------------------------------------------
# §4 схема, §6 connect/init_db
# ---------------------------------------------------------------------------
class TestSchema(DBCase):
    def test_tables_exist(self):
        names = set(r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertTrue({'tasks', 'journal', 'settings'} <= names, names)

    def test_tasks_columns(self):
        cols = set(r[1] for r in self.conn.execute('PRAGMA table_info(tasks)'))
        self.assertTrue(TASK_COLUMNS <= cols, TASK_COLUMNS - cols)

    def test_journal_columns(self):
        cols = set(r[1] for r in self.conn.execute('PRAGMA table_info(journal)'))
        self.assertTrue(JOURNAL_COLUMNS <= cols, JOURNAL_COLUMNS - cols)

    def test_settings_columns(self):
        cols = set(r[1] for r in self.conn.execute('PRAGMA table_info(settings)'))
        self.assertTrue({'key', 'value'} <= cols)

    def test_foreign_keys_on(self):
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_row_factory(self):
        self.assertIs(self.conn.row_factory, sqlite3.Row)

    def test_init_db_idempotent(self):
        t = self.make('Задача до повторной инициализации')
        db.init_db(self.conn)
        db.init_db(self.conn)
        self.assertIsNotNone(db.get_task(self.conn, t['id']))
        self.assertEqual(db.get_settings(self.conn)['day_limit'], 3)

    def test_file_database_wal_and_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fokus.db')
            conn = db.connect(path)
            try:
                self.assertTrue(os.path.exists(path))
                mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
                self.assertEqual(str(mode).lower(), 'wal')
                self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                names = set(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"))
                self.assertTrue({'tasks', 'journal', 'settings'} <= names)
            finally:
                conn.close()

    def test_file_database_reopen_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fokus.db')
            conn = db.connect(path)
            t = db.create_task(conn, 'Сохранённая задача', '', answers(), evaluation(26.0, 'queue'), NOW)
            conn.close()
            conn2 = db.connect(path)
            try:
                again = db.get_task(conn2, t['id'])
                self.assertIsNotNone(again)
                self.assertEqual(again['title'], 'Сохранённая задача')
                self.assertEqual(len(db.journal(conn2, task_id=t['id'])), 1)
            finally:
                conn2.close()


# ---------------------------------------------------------------------------
# §4.2 / §6 настройки
# ---------------------------------------------------------------------------
class TestSettings(DBCase):
    def test_defaults_all_keys(self):
        s = db.get_settings(self.conn)
        self.assertEqual(set(s.keys()) & SETTINGS_KEYS, SETTINGS_KEYS)
        self.assertEqual(s['weights'], DEFAULT_WEIGHTS)
        self.assertEqual(s['thresholds'], DEFAULT_THRESHOLDS)
        self.assertEqual(s['day_limit'], 3)
        self.assertEqual(s['stale_days'], 7)
        self.assertEqual(s['carry_limit'], 3)
        self.assertEqual(s['language'], 'ru')
        self.assertIsNone(s['last_rollover'])

    def test_settings_stored_in_table(self):
        n = self.conn.execute('SELECT COUNT(*) FROM settings').fetchone()[0]
        self.assertGreaterEqual(n, 1)

    def test_partial_update_scalar(self):
        r = db.update_settings(self.conn, {'day_limit': 5})
        self.assertEqual(r['day_limit'], 5)
        self.assertEqual(r['stale_days'], 7)
        self.assertEqual(r['weights'], DEFAULT_WEIGHTS)
        s = db.get_settings(self.conn)
        self.assertEqual(s['day_limit'], 5)
        self.assertEqual(s['language'], 'ru')

    def test_partial_update_weights_merge(self):
        r = db.update_settings(self.conn, {'weights': {'importance': 0.6}})
        self.assertEqual(r['weights']['importance'], 0.6)
        self.assertEqual(r['weights']['urgency'], 0.30)
        self.assertEqual(r['weights']['consequence'], 0.20)
        self.assertEqual(r['weights']['duration'], 0.05)
        self.assertEqual(db.get_settings(self.conn)['weights']['importance'], 0.6)

    def test_partial_update_thresholds_merge(self):
        r = db.update_settings(self.conn, {'thresholds': {'now': 70}})
        self.assertEqual(r['thresholds'], {'now': 70, 'priority': 45, 'queue': 25})

    def test_update_returns_full_settings(self):
        r = db.update_settings(self.conn, {'language': 'de'})
        self.assertEqual(set(r.keys()) & SETTINGS_KEYS, SETTINGS_KEYS)
        self.assertEqual(r['language'], 'de')

    def test_multiple_keys_at_once(self):
        r = db.update_settings(self.conn, {'day_limit': 4, 'stale_days': 14, 'carry_limit': 2})
        self.assertEqual((r['day_limit'], r['stale_days'], r['carry_limit']), (4, 14, 2))

    def test_valid_boundaries_accepted(self):
        self.assertEqual(db.update_settings(self.conn, {'day_limit': 1})['day_limit'], 1)
        self.assertEqual(db.update_settings(self.conn, {'day_limit': 10})['day_limit'], 10)
        self.assertEqual(db.update_settings(self.conn, {'stale_days': 1})['stale_days'], 1)
        self.assertEqual(db.update_settings(self.conn, {'stale_days': 90})['stale_days'], 90)
        self.assertEqual(db.update_settings(self.conn, {'carry_limit': 1})['carry_limit'], 1)
        self.assertEqual(db.update_settings(self.conn, {'carry_limit': 10})['carry_limit'], 10)
        self.assertEqual(db.update_settings(self.conn, {'language': 'de'})['language'], 'de')
        self.assertEqual(db.update_settings(self.conn, {'language': 'ru'})['language'], 'ru')

    def test_weights_zero_allowed_if_sum_positive(self):
        r = db.update_settings(self.conn, {'weights': {'duration': 0}})
        self.assertEqual(r['weights']['duration'], 0)

    def assertSettingsError(self, patch, key):
        with self.assertRaises(ValueError) as cm:
            db.update_settings(self.conn, patch)
        self.assertEqual(cm.exception.args[0], key)

    def test_thresholds_now_equal_priority_rejected(self):
        self.assertSettingsError({'thresholds': {'now': 45}}, 'thresholds')

    def test_thresholds_now_below_priority_rejected(self):
        self.assertSettingsError({'thresholds': {'now': 40}}, 'thresholds')

    def test_thresholds_priority_not_above_queue_rejected(self):
        self.assertSettingsError({'thresholds': {'priority': 25}}, 'thresholds')
        self.assertSettingsError({'thresholds': {'priority': 20}}, 'thresholds')

    def test_thresholds_queue_zero_rejected(self):
        self.assertSettingsError({'thresholds': {'queue': 0}}, 'thresholds')

    def test_day_limit_out_of_range(self):
        self.assertSettingsError({'day_limit': 0}, 'day_limit')
        self.assertSettingsError({'day_limit': 11}, 'day_limit')

    def test_stale_days_out_of_range(self):
        self.assertSettingsError({'stale_days': 0}, 'stale_days')
        self.assertSettingsError({'stale_days': 91}, 'stale_days')

    def test_carry_limit_out_of_range(self):
        self.assertSettingsError({'carry_limit': 0}, 'carry_limit')
        self.assertSettingsError({'carry_limit': 11}, 'carry_limit')

    def test_language_unknown(self):
        self.assertSettingsError({'language': 'en'}, 'language')

    def test_weights_negative(self):
        self.assertSettingsError({'weights': {'importance': -0.1}}, 'weights')

    def test_weights_all_zero(self):
        self.assertSettingsError({'weights': {'importance': 0, 'urgency': 0,
                                              'consequence': 0, 'duration': 0}}, 'weights')

    def test_unknown_keys_ignored(self):
        r = db.update_settings(self.conn, {'foo': 1, 'day_limit': 4})
        self.assertNotIn('foo', r)
        self.assertEqual(r['day_limit'], 4)
        self.assertNotIn('foo', db.get_settings(self.conn))

    def test_only_unknown_keys_is_noop(self):
        before = db.get_settings(self.conn)
        after = db.update_settings(self.conn, {'foo': 'bar'})
        self.assertEqual(after, before)

    def test_failed_update_does_not_change_settings(self):
        with self.assertRaises(ValueError):
            db.update_settings(self.conn, {'day_limit': 0, 'language': 'de'})
        s = db.get_settings(self.conn)
        self.assertEqual(s['day_limit'], 3)
        self.assertEqual(s['language'], 'ru')

    def test_empty_patch(self):
        self.assertEqual(db.update_settings(self.conn, {}), db.get_settings(self.conn))


# ---------------------------------------------------------------------------
# §6 create_task / get_task
# ---------------------------------------------------------------------------
class TestCreateTask(DBCase):
    def test_returns_dict_with_group_not_grp(self):
        t = self.make('Отчёт по пациенту tomedo #48213', score=87.3, group='now',
                      due_date='2026-10-25', importance=4, due='today', consequence='serious')
        self.assertIsInstance(t, dict)
        self.assertIn('group', t)
        self.assertNotIn('grp', t)
        self.assertEqual(t['group'], 'now')

    def test_fields_from_answers_and_evaluation(self):
        t = self.make('Отчёт по пациенту tomedo #48213', score=87.3, group='now',
                      due_date='2026-10-25', importance=4, due='today',
                      consequence='serious', duration='30m', note='заметка')
        self.assertEqual(t['title'], 'Отчёт по пациенту tomedo #48213')
        self.assertEqual(t['note'], 'заметка')
        self.assertEqual(t['importance'], 4)
        self.assertEqual(t['due'], 'today')
        self.assertEqual(t['due_date'], '2026-10-25')
        self.assertEqual(t['consequence'], 'serious')
        self.assertEqual(t['who'], 'me')
        self.assertEqual(t['delegate_to'], '')
        self.assertEqual(t['duration'], '30m')
        self.assertEqual(t['depends_on'], '')
        self.assertEqual(t['score'], 87.3)

    def test_defaults_on_create(self):
        t = self.make()
        self.assertEqual(t['is_today'], 0)
        self.assertIsNone(t['today_date'])
        self.assertEqual(t['carry_count'], 0)
        self.assertEqual(t['status'], 'open')
        self.assertEqual(t['group_manual'], 0)
        self.assertEqual(t['needs_review'], 0)
        self.assertEqual(t['review_reason'], '')
        self.assertIsNone(t['done_at'])

    def test_timestamps(self):
        t = self.make()
        self.assertEqual(t['created_at'], NOW_ISO)
        self.assertEqual(t['updated_at'], NOW_ISO)
        self.assertEqual(t['evaluated_at'], NOW_ISO)
        self.assertEqual(t['group_since'], NOW_ISO)

    def test_due_date_none(self):
        t = self.make(due='none', due_date=None)
        self.assertIsNone(t['due_date'])

    def test_all_api_keys_present(self):
        t = self.make()
        expected = (TASK_COLUMNS - {'grp'}) | {'group'}
        self.assertTrue(expected <= set(t.keys()), expected - set(t.keys()))

    def test_journal_create_entry(self):
        t = self.make(group='queue', score=26.0)
        rows = self.events(t['id'])
        self.assertEqual(len(rows), 1)
        e = rows[0]
        self.assertEqual(e['event'], 'create')
        self.assertEqual(e['task_id'], t['id'])
        self.assertEqual(e['old_value'], '')
        self.assertEqual(e['new_value'], 'queue')
        self.assertEqual(e['ts'], NOW_ISO)

    def test_get_task(self):
        t = self.make('Задача для чтения')
        g = db.get_task(self.conn, t['id'])
        self.assertIsNotNone(g)
        self.assertEqual(g['id'], t['id'])
        self.assertEqual(g['title'], 'Задача для чтения')
        self.assertIn('group', g)
        self.assertNotIn('grp', g)

    def test_get_task_missing(self):
        self.assertIsNone(db.get_task(self.conn, 999999))

    def test_ids_increment(self):
        a = self.make('Первая')
        b = self.make('Вторая')
        self.assertGreater(b['id'], a['id'])

    def test_delegate_task(self):
        t = self.make('Заказать канцтовары', score=54.0, group='delegate', due_date='2026-11-01',
                      importance=3, due='week', consequence='unpleasant', who='delegate',
                      delegate_to='Света')
        self.assertEqual(t['who'], 'delegate')
        self.assertEqual(t['delegate_to'], 'Света')
        self.assertEqual(t['group'], 'delegate')

    def test_waiting_task(self):
        t = self.make('Подать заявку', score=86.5, group='waiting', due_date='2026-11-01',
                      importance=5, due='week', consequence='serious',
                      depends_on='справка из банка')
        self.assertEqual(t['depends_on'], 'справка из банка')
        self.assertEqual(t['group'], 'waiting')

    def test_score_stored_as_float(self):
        t = self.make(score=35.0)
        self.assertEqual(t['score'], 35.0)
        self.assertIsInstance(t['score'], float)


# ---------------------------------------------------------------------------
# §6 list_tasks
# ---------------------------------------------------------------------------
class TestListTasks(DBCase):
    def setUp(self):
        super().setUp()
        self.a = self.make('A срочная', score=90.0, group='now', due_date='2026-10-25',
                           due='today', duration='5m', now=dt(hour=9, minute=0))
        self.b = self.make('B срочная завтра', score=90.0, group='now', due_date='2026-10-26',
                           due='tomorrow', duration='30m', now=dt(hour=9, minute=5))
        self.c = self.make('C поручить', score=54.0, group='delegate', due_date='2026-11-01',
                           due='week', who='delegate', delegate_to='Света', duration='2h',
                           now=dt(hour=9, minute=10))
        self.d = self.make('D когда-нибудь', score=13.3, group='someday', due_date=None,
                           due='none', duration='half_day', now=dt(hour=9, minute=15))
        self.e = self.make('E очередь', score=35.0, group='queue', due_date='2026-11-24',
                           due='month', duration='30m', now=dt(hour=9, minute=20))
        self.f = self.make('F как A, создана позже', score=90.0, group='now', due_date='2026-10-25',
                           due='today', duration='2h', now=dt(hour=9, minute=25))

    def test_default_returns_all_open(self):
        rows = db.list_tasks(self.conn)
        self.assertEqual(set(self.ids(rows)),
                         {self.a['id'], self.b['id'], self.c['id'], self.d['id'], self.e['id'], self.f['id']})

    def test_rows_are_api_dicts(self):
        for r in db.list_tasks(self.conn):
            self.assertIn('group', r)
            self.assertNotIn('grp', r)

    def test_sort_score_default(self):
        # score ↓, затем due_date ↑, затем created_at ↑
        rows = db.list_tasks(self.conn)
        self.assertEqual(self.ids(rows),
                         [self.a['id'], self.f['id'], self.b['id'], self.c['id'], self.e['id'], self.d['id']])

    def test_sort_score_explicit(self):
        self.assertEqual(db.list_tasks(self.conn, sort='score'), db.list_tasks(self.conn))

    def test_sort_due(self):
        # due_date ↑ (None в конце), затем score ↓
        rows = self.ids(db.list_tasks(self.conn, sort='due'))
        self.assertEqual(set(rows[:2]), {self.a['id'], self.f['id']})
        self.assertEqual(rows[2:], [self.b['id'], self.c['id'], self.e['id'], self.d['id']])

    def test_sort_due_score_tiebreak(self):
        g = self.make('G тот же срок, выше балл', score=95.0, group='now', due_date='2026-10-26',
                      due='tomorrow', now=dt(hour=9, minute=30))
        rows = self.ids(db.list_tasks(self.conn, sort='due'))
        self.assertLess(rows.index(g['id']), rows.index(self.b['id']))
        self.assertEqual(rows[-1], self.d['id'])

    def test_sort_created(self):
        rows = self.ids(db.list_tasks(self.conn, sort='created'))
        self.assertEqual(rows, [self.f['id'], self.e['id'], self.d['id'], self.c['id'], self.b['id'], self.a['id']])

    def test_filter_group(self):
        self.assertEqual(set(self.ids(db.list_tasks(self.conn, group='now'))),
                         {self.a['id'], self.b['id'], self.f['id']})
        self.assertEqual(self.ids(db.list_tasks(self.conn, group='someday')), [self.d['id']])
        self.assertEqual(self.ids(db.list_tasks(self.conn, group='waiting')), [])

    def test_filter_who(self):
        self.assertEqual(self.ids(db.list_tasks(self.conn, who='delegate')), [self.c['id']])
        self.assertEqual(len(db.list_tasks(self.conn, who='me')), 5)

    def test_filter_duration(self):
        self.assertEqual(set(self.ids(db.list_tasks(self.conn, duration='30m'))),
                         {self.b['id'], self.e['id']})
        self.assertEqual(self.ids(db.list_tasks(self.conn, duration='5m')), [self.a['id']])

    def test_filter_due(self):
        self.assertEqual(self.ids(db.list_tasks(self.conn, due='none')), [self.d['id']])
        self.assertEqual(set(self.ids(db.list_tasks(self.conn, due='today'))),
                         {self.a['id'], self.f['id']})

    def test_filters_combined(self):
        self.assertEqual(self.ids(db.list_tasks(self.conn, group='now', duration='5m')), [self.a['id']])
        self.assertEqual(self.ids(db.list_tasks(self.conn, group='now', due='tomorrow')), [self.b['id']])
        self.assertEqual(self.ids(db.list_tasks(self.conn, group='someday', who='delegate')), [])

    def test_status_open_done_all(self):
        db.set_done(self.conn, self.b['id'], dt(hour=10))
        open_ids = self.ids(db.list_tasks(self.conn, status='open'))
        done_ids = self.ids(db.list_tasks(self.conn, status='done'))
        all_ids = self.ids(db.list_tasks(self.conn, status='all'))
        self.assertNotIn(self.b['id'], open_ids)
        self.assertEqual(len(open_ids), 5)
        self.assertEqual(done_ids, [self.b['id']])
        self.assertEqual(len(all_ids), 6)
        self.assertIn(self.b['id'], all_ids)

    def test_deleted_never_listed(self):
        db.delete_task(self.conn, self.d['id'], dt(hour=10))
        for status in ('open', 'done', 'all'):
            with self.subTest(status=status):
                self.assertNotIn(self.d['id'], self.ids(db.list_tasks(self.conn, status=status)))

    def test_needs_review_filter(self):
        db.update_task_fields(self.conn, self.e['id'],
                              {'needs_review': 1, 'review_reason': 'stale'}, dt(hour=10))
        rows = db.list_tasks(self.conn, needs_review=1)
        self.assertEqual(self.ids(rows), [self.e['id']])
        self.assertEqual(rows[0]['needs_review'], 1)
        self.assertEqual(rows[0]['review_reason'], 'stale')

    def test_needs_review_none_means_no_filter(self):
        db.update_task_fields(self.conn, self.e['id'], {'needs_review': 1}, dt(hour=10))
        self.assertEqual(len(db.list_tasks(self.conn, needs_review=None)), 6)

    def test_empty_db(self):
        conn = db.connect(':memory:')
        try:
            self.assertEqual(db.list_tasks(conn), [])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# §6 edit_task / move_task / update_task_fields
# ---------------------------------------------------------------------------
class TestEditTask(DBCase):
    def test_edit_title(self):
        t = self.make('Старое название', note='заметка')
        r = db.edit_task(self.conn, t['id'], title='Новое название', note=None, now=dt(hour=10))
        self.assertEqual(r['title'], 'Новое название')
        self.assertEqual(r['note'], 'заметка')
        self.assertEqual(r['updated_at'], dt(hour=10).isoformat())
        self.assertEqual(db.get_task(self.conn, t['id'])['title'], 'Новое название')

    def test_edit_title_journal(self):
        t = self.make('Старое название')
        db.edit_task(self.conn, t['id'], title='Новое название', note=None, now=dt(hour=10))
        rows = self.events(t['id'], 'edit')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'Старое название')
        self.assertEqual(rows[0]['new_value'], 'Новое название')
        self.assertEqual(rows[0]['ts'], dt(hour=10).isoformat())

    def test_edit_note_only(self):
        t = self.make('Название', note='')
        r = db.edit_task(self.conn, t['id'], title=None, note='новая заметка', now=dt(hour=10))
        self.assertEqual(r['title'], 'Название')
        self.assertEqual(r['note'], 'новая заметка')
        self.assertEqual(len(self.events(t['id'], 'edit')), 1)

    def test_edit_both(self):
        t = self.make('Название', note='')
        r = db.edit_task(self.conn, t['id'], title='Другое', note='текст', now=dt(hour=10))
        self.assertEqual((r['title'], r['note']), ('Другое', 'текст'))

    def test_edit_does_not_touch_score_or_group(self):
        t = self.make('Название', score=66.0, group='now')
        r = db.edit_task(self.conn, t['id'], title='Другое', note=None, now=dt(hour=10))
        self.assertEqual(r['score'], 66.0)
        self.assertEqual(r['group'], 'now')
        self.assertEqual(r['group_manual'], 0)

    def test_update_task_fields_low_level(self):
        t = self.make()
        r = db.update_task_fields(self.conn, t['id'], {'carry_count': 2}, dt(hour=10))
        self.assertEqual(r['carry_count'], 2)
        self.assertEqual(db.get_task(self.conn, t['id'])['carry_count'], 2)

    def test_update_task_fields_with_event_writes_journal(self):
        t = self.make()
        db.update_task_fields(self.conn, t['id'], {'note': 'x'}, dt(hour=10),
                              event='edit', old='', new='x', note='тест')
        rows = self.events(t['id'], 'edit')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['new_value'], 'x')
        self.assertEqual(rows[0]['note'], 'тест')


class TestMoveTask(DBCase):
    def test_move_sets_manual_and_group_since(self):
        t = self.make(group='priority', score=50.0)
        r = db.move_task(self.conn, t['id'], 'someday', dt(hour=10))
        self.assertEqual(r['group'], 'someday')
        self.assertEqual(r['group_manual'], 1)
        self.assertEqual(r['group_since'], dt(hour=10).isoformat())
        self.assertEqual(r['score'], 50.0)
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['group'], 'someday')
        self.assertEqual(g['group_manual'], 1)

    def test_move_journal(self):
        t = self.make(group='priority')
        db.move_task(self.conn, t['id'], 'now', dt(hour=10))
        rows = self.events(t['id'], 'manual_move')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'priority')
        self.assertEqual(rows[0]['new_value'], 'now')

    def test_move_to_all_groups(self):
        for group in GROUPS:
            with self.subTest(group=group):
                t = self.make(group='queue', score=30.0)
                r = db.move_task(self.conn, t['id'], group, dt(hour=10))
                self.assertEqual(r['group'], group)

    def test_move_unknown_group(self):
        t = self.make()
        with self.assertRaises(ValueError):
            db.move_task(self.conn, t['id'], 'urgent', dt(hour=10))
        self.assertEqual(db.get_task(self.conn, t['id'])['group'], 'priority')

    def test_move_today_task_out_of_now_removes_from_today(self):
        t = self.make(group='now', score=70.0, due_date='2026-10-25', due='today')
        db.add_today(self.conn, t['id'], TODAY, dt(hour=9, minute=30))
        self.assertEqual(db.get_task(self.conn, t['id'])['is_today'], 1)
        r = db.move_task(self.conn, t['id'], 'priority', dt(hour=10))
        self.assertEqual(r['group'], 'priority')
        self.assertEqual(r['is_today'], 0)
        self.assertIsNone(r['today_date'])
        self.assertEqual(db.today_tasks(self.conn, TODAY), [])
        removes = self.events(t['id'], 'today_remove')
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]['note'], 'moved')
        self.assertEqual(len(self.events(t['id'], 'manual_move')), 1)

    def test_move_today_task_into_now_keeps_today(self):
        t = self.make(group='priority', score=50.0, due_date='2026-10-25', due='today')
        db.add_today(self.conn, t['id'], TODAY, dt(hour=9, minute=30))
        r = db.move_task(self.conn, t['id'], 'now', dt(hour=10))
        self.assertEqual(r['group'], 'now')
        self.assertEqual(r['is_today'], 1)
        self.assertEqual(r['today_date'], '2026-10-25')
        self.assertEqual(self.events(t['id'], 'today_remove'), [])

    def test_move_not_today_task_no_today_remove(self):
        t = self.make(group='now', score=70.0)
        db.move_task(self.conn, t['id'], 'queue', dt(hour=10))
        self.assertEqual(self.events(t['id'], 'today_remove'), [])


# ---------------------------------------------------------------------------
# §6 apply_evaluation
# ---------------------------------------------------------------------------
class TestApplyEvaluation(DBCase):
    def test_updates_answers_score_group(self):
        t = self.make('Задача', score=35.0, group='queue', due_date='2026-11-24', due='month')
        new_ans = answers(importance=5, due='today', consequence='serious', duration='5m',
                          who='me', depends_on='', delegate_to='')
        r = db.apply_evaluation(self.conn, t['id'], new_ans,
                                evaluation(100.0, 'now', '2026-10-26'), dt(day=26, hour=11))
        self.assertEqual(r['importance'], 5)
        self.assertEqual(r['due'], 'today')
        self.assertEqual(r['due_date'], '2026-10-26')
        self.assertEqual(r['consequence'], 'serious')
        self.assertEqual(r['duration'], '5m')
        self.assertEqual(r['score'], 100.0)
        self.assertEqual(r['group'], 'now')
        self.assertEqual(r['evaluated_at'], dt(day=26, hour=11).isoformat())
        self.assertEqual(r['updated_at'], dt(day=26, hour=11).isoformat())

    def test_resets_manual_carry_and_review(self):
        t = self.make('Задача', score=35.0, group='queue', due_date='2026-11-24', due='month')
        db.move_task(self.conn, t['id'], 'someday', dt(hour=10))
        db.update_task_fields(self.conn, t['id'],
                              {'carry_count': 2, 'needs_review': 1, 'review_reason': 'carried'},
                              dt(hour=10, minute=5))
        before = db.get_task(self.conn, t['id'])
        self.assertEqual((before['group_manual'], before['carry_count'], before['needs_review']), (1, 2, 1))
        r = db.apply_evaluation(self.conn, t['id'], answers(importance=4, due='today', consequence='serious'),
                                evaluation(87.3, 'now', '2026-10-25'), dt(hour=11))
        self.assertEqual(r['group_manual'], 0)
        self.assertEqual(r['carry_count'], 0)
        self.assertEqual(r['needs_review'], 0)
        self.assertEqual(r['review_reason'], '')
        g = db.get_task(self.conn, t['id'])
        self.assertEqual((g['group_manual'], g['carry_count'], g['needs_review']), (0, 0, 0))

    def test_journal_evaluate(self):
        t = self.make('Задача', score=35.0, group='queue', due_date='2026-11-24', due='month')
        db.apply_evaluation(self.conn, t['id'], answers(importance=5, due='today', consequence='serious'),
                            evaluation(98.5, 'now', '2026-10-25'), dt(hour=11))
        rows = self.events(t['id'], 'evaluate')
        self.assertEqual(len(rows), 1)
        old_group, old_score = rows[0]['old_value'].split('/')
        new_group, new_score = rows[0]['new_value'].split('/')
        self.assertEqual(old_group, 'queue')
        self.assertEqual(float(old_score), 35.0)
        self.assertEqual(new_group, 'now')
        self.assertEqual(float(new_score), 98.5)

    def test_group_since_updated_when_group_changes(self):
        t = self.make('Задача', score=35.0, group='queue')
        r = db.apply_evaluation(self.conn, t['id'], answers(importance=5, due='today', consequence='serious'),
                                evaluation(98.5, 'now', '2026-10-25'), dt(hour=11))
        self.assertEqual(r['group_since'], dt(hour=11).isoformat())

    def test_group_since_kept_when_group_same(self):
        t = self.make('Задача', score=35.0, group='queue')
        r = db.apply_evaluation(self.conn, t['id'], answers(importance=3, due='week'),
                                evaluation(44.0, 'queue', '2026-11-01'), dt(hour=11))
        self.assertEqual(r['group'], 'queue')
        self.assertEqual(r['group_since'], NOW_ISO)

    def test_does_not_touch_today_or_status(self):
        t = self.make('Задача', score=70.0, group='now', due_date='2026-10-25', due='today')
        db.add_today(self.conn, t['id'], TODAY, dt(hour=9, minute=30))
        r = db.apply_evaluation(self.conn, t['id'], answers(importance=5, due='today', consequence='serious'),
                                evaluation(98.5, 'now', '2026-10-25'), dt(hour=11))
        self.assertEqual(r['is_today'], 1)
        self.assertEqual(r['status'], 'open')

    def test_delegate_answers_applied(self):
        t = self.make('Задача', score=35.0, group='queue')
        r = db.apply_evaluation(self.conn, t['id'],
                                answers(importance=3, due='week', consequence='unpleasant',
                                        who='delegate', delegate_to='Света'),
                                evaluation(54.0, 'delegate', '2026-11-01'), dt(hour=11))
        self.assertEqual(r['who'], 'delegate')
        self.assertEqual(r['delegate_to'], 'Света')
        self.assertEqual(r['group'], 'delegate')


# ---------------------------------------------------------------------------
# §6 set_done / reopen_task / delete_task
# ---------------------------------------------------------------------------
class TestDoneReopenDelete(DBCase):
    def test_set_done(self):
        t = self.make()
        r = db.set_done(self.conn, t['id'], dt(hour=12))
        self.assertEqual(r['status'], 'done')
        self.assertEqual(r['done_at'], dt(hour=12).isoformat())
        self.assertEqual(r['updated_at'], dt(hour=12).isoformat())
        rows = self.events(t['id'], 'done')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'open')
        self.assertEqual(rows[0]['new_value'], 'done')

    def test_reopen(self):
        t = self.make()
        db.set_done(self.conn, t['id'], dt(hour=12))
        r = db.reopen_task(self.conn, t['id'], dt(hour=13))
        self.assertEqual(r['status'], 'open')
        self.assertIsNone(r['done_at'])
        rows = self.events(t['id'], 'reopen')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'done')
        self.assertEqual(rows[0]['new_value'], 'open')
        self.assertIn(t['id'], self.ids(db.list_tasks(self.conn)))

    def test_delete(self):
        t = self.make()
        r = db.delete_task(self.conn, t['id'], dt(hour=12))
        self.assertEqual(r['status'], 'deleted')
        rows = self.events(t['id'], 'delete')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'open')
        self.assertEqual(rows[0]['new_value'], 'deleted')

    def test_deleted_not_listed_any_status(self):
        t = self.make()
        db.delete_task(self.conn, t['id'], dt(hour=12))
        for status in ('open', 'done', 'all'):
            with self.subTest(status=status):
                self.assertNotIn(t['id'], self.ids(db.list_tasks(self.conn, status=status)))

    def test_delete_is_soft(self):
        t = self.make()
        db.delete_task(self.conn, t['id'], dt(hour=12))
        row = self.conn.execute('SELECT status FROM tasks WHERE id = ?', (t['id'],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'deleted')
        g = db.get_task(self.conn, t['id'])
        self.assertIsNotNone(g)
        self.assertEqual(g['status'], 'deleted')
        self.assertGreaterEqual(len(self.events(t['id'])), 2)  # create + delete

    def test_delete_done_task(self):
        t = self.make()
        db.set_done(self.conn, t['id'], dt(hour=12))
        r = db.delete_task(self.conn, t['id'], dt(hour=13))
        self.assertEqual(r['status'], 'deleted')
        self.assertEqual(self.events(t['id'], 'delete')[0]['old_value'], 'done')

    def test_done_task_excluded_from_open_and_counts(self):
        t = self.make(group='now', score=70.0)
        db.set_done(self.conn, t['id'], dt(hour=12))
        self.assertNotIn(t['id'], self.ids(db.list_tasks(self.conn)))
        self.assertEqual(db.count_by_group(self.conn)['now'], 0)


# ---------------------------------------------------------------------------
# §6 today_tasks / add_today / remove_today
# ---------------------------------------------------------------------------
class TestToday(DBCase):
    def now_task(self, title, score=70.0, minute=0):
        return self.make(title, score=score, group='now', due_date='2026-10-25', due='today',
                         now=dt(hour=8, minute=minute))

    def test_add_today(self):
        t = self.now_task('A')
        r = db.add_today(self.conn, t['id'], TODAY, NOW)
        self.assertIsInstance(r, dict)
        g = db.get_task(self.conn, t['id'])
        self.assertEqual(g['is_today'], 1)
        self.assertEqual(g['today_date'], '2026-10-25')
        rows = self.events(t['id'], 'today_add')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['new_value'], '2026-10-25')
        self.assertEqual(rows[0]['old_value'], '')

    def test_today_tasks_lists_only_that_date(self):
        a = self.now_task('A')
        b = self.now_task('B', minute=1)
        db.add_today(self.conn, a['id'], TODAY, NOW)
        db.add_today(self.conn, b['id'], YESTERDAY, dt(day=24))
        self.assertEqual(self.ids(db.today_tasks(self.conn, TODAY)), [a['id']])
        self.assertEqual(self.ids(db.today_tasks(self.conn, YESTERDAY)), [b['id']])
        self.assertEqual(db.today_tasks(self.conn, TOMORROW), [])

    def test_today_tasks_empty(self):
        self.now_task('A')
        self.assertEqual(db.today_tasks(self.conn, TODAY), [])

    def test_today_tasks_returns_api_dicts(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        rows = db.today_tasks(self.conn, TODAY)
        self.assertIn('group', rows[0])
        self.assertNotIn('grp', rows[0])
        self.assertEqual(rows[0]['is_today'], 1)

    def test_today_tasks_includes_done_same_day(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        db.set_done(self.conn, a['id'], dt(hour=12))
        rows = db.today_tasks(self.conn, TODAY)
        self.assertEqual(self.ids(rows), [a['id']])
        self.assertEqual(rows[0]['status'], 'done')
        self.assertEqual(rows[0]['is_today'], 1)

    def test_today_tasks_excludes_deleted(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        db.delete_task(self.conn, a['id'], dt(hour=12))
        self.assertNotIn(a['id'], self.ids(db.today_tasks(self.conn, TODAY)))

    def test_remove_today(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        r = db.remove_today(self.conn, a['id'], dt(hour=10))
        self.assertIsInstance(r, dict)
        g = db.get_task(self.conn, a['id'])
        self.assertEqual(g['is_today'], 0)
        self.assertIsNone(g['today_date'])
        self.assertEqual(db.today_tasks(self.conn, TODAY), [])
        rows = self.events(a['id'], 'today_remove')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['new_value'], '2026-10-25')

    def test_remove_today_with_note(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        db.remove_today(self.conn, a['id'], dt(hour=10), note='moved')
        self.assertEqual(self.events(a['id'], 'today_remove')[0]['note'], 'moved')

    def test_add_today_with_replace(self):
        a = self.now_task('A')
        b = self.now_task('B', minute=1)
        c = self.now_task('C', minute=2)
        db.add_today(self.conn, a['id'], TODAY, dt(hour=9, minute=0))
        db.add_today(self.conn, b['id'], TODAY, dt(hour=9, minute=1))
        db.add_today(self.conn, c['id'], TODAY, dt(hour=9, minute=2), replace_id=a['id'])
        ga = db.get_task(self.conn, a['id'])
        gc = db.get_task(self.conn, c['id'])
        self.assertEqual(ga['is_today'], 0)
        self.assertIsNone(ga['today_date'])
        self.assertEqual(gc['is_today'], 1)
        self.assertEqual(gc['today_date'], '2026-10-25')
        self.assertEqual(set(self.ids(db.today_tasks(self.conn, TODAY))), {b['id'], c['id']})
        removes = self.events(a['id'], 'today_remove')
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]['note'], 'replace')
        self.assertEqual(len(self.events(c['id'], 'today_add')), 1)

    def test_add_today_does_not_check_limit(self):
        # лимит проверяет сервер через engine.can_add_today — db принимает 4-ю задачу
        tasks = [self.now_task('T%d' % i, minute=i) for i in range(4)]
        for t in tasks:
            db.add_today(self.conn, t['id'], TODAY, NOW)
        self.assertEqual(len(db.today_tasks(self.conn, TODAY)), 4)

    def test_add_today_does_not_change_group_or_carry(self):
        a = self.now_task('A')
        db.add_today(self.conn, a['id'], TODAY, NOW)
        g = db.get_task(self.conn, a['id'])
        self.assertEqual(g['group'], 'now')
        self.assertEqual(g['carry_count'], 0)
        self.assertEqual(g['group_manual'], 0)


# ---------------------------------------------------------------------------
# §6 apply_rollover
# ---------------------------------------------------------------------------
class TestApplyRollover(DBCase):
    def setUp(self):
        super().setUp()
        # P: выбрана на вчера, не сделана → перенос
        self.p = self.make('P перенос', score=70.0, group='now', due_date='2026-10-24', due='today',
                           importance=4, consequence='unpleasant', now=dt(day=24, hour=9))
        db.add_today(self.conn, self.p['id'], YESTERDAY, dt(day=24, hour=9, minute=5))
        # Q: мастер пройден 15 дней назад → stale
        self.q = self.make('Q устарела', score=60.0, group='priority', now=dt(day=10, hour=9))
        # R: создана 25 дней назад с due=month → срочность выросла → queue → priority
        self.r = self.make('R перегруппировка', score=44.8, group='queue', due_date='2026-10-30',
                           due='month', importance=4, duration='2h', now=dt(month=9, day=30, hour=9))

    def full_plan(self):
        return {
            'carry': [self.p['id']],
            'review': {self.q['id']: 'stale'},
            'regroup': {self.r['id']: ('queue', 'priority')},
            'rescore': {self.p['id']: 70.0, self.q['id']: 60.0, self.r['id']: 53.8},
        }

    def test_counters(self):
        res = db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        self.assertEqual(res['carried'], 1)
        self.assertEqual(res['review'], 1)
        self.assertEqual(res['regrouped'], 1)
        self.assertEqual(res['rescored'], 3)

    def test_last_rollover_set(self):
        self.assertIsNone(db.get_settings(self.conn)['last_rollover'])
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        self.assertEqual(db.get_settings(self.conn)['last_rollover'], '2026-10-25')

    def test_carry(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        g = db.get_task(self.conn, self.p['id'])
        self.assertEqual(g['is_today'], 0)
        self.assertIsNone(g['today_date'])
        self.assertEqual(g['carry_count'], 1)
        self.assertEqual(g['status'], 'open')
        rows = self.events(self.p['id'], 'carry')
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]['old_value']), '0')
        self.assertEqual(str(rows[0]['new_value']), '1')
        self.assertEqual(rows[0]['ts'], NOW_ISO)
        self.assertEqual(db.today_tasks(self.conn, YESTERDAY), [])
        self.assertEqual(db.today_tasks(self.conn, TODAY), [])

    def test_carry_increments_existing_count(self):
        db.update_task_fields(self.conn, self.p['id'], {'carry_count': 2}, dt(day=24, hour=10))
        plan = {'carry': [self.p['id']], 'review': {self.p['id']: 'carried'},
                'regroup': {}, 'rescore': {self.p['id']: 70.0}}
        db.apply_rollover(self.conn, plan, TODAY, NOW)
        g = db.get_task(self.conn, self.p['id'])
        self.assertEqual(g['carry_count'], 3)
        self.assertEqual(g['needs_review'], 1)
        self.assertEqual(g['review_reason'], 'carried')
        rows = self.events(self.p['id'], 'carry')
        self.assertEqual(str(rows[0]['old_value']), '2')
        self.assertEqual(str(rows[0]['new_value']), '3')
        rr = self.events(self.p['id'], 'review_required')
        self.assertEqual(len(rr), 1)
        self.assertEqual(rr[0]['new_value'], 'carried')

    def test_review_stale(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        g = db.get_task(self.conn, self.q['id'])
        self.assertEqual(g['needs_review'], 1)
        self.assertEqual(g['review_reason'], 'stale')
        rows = self.events(self.q['id'], 'review_required')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], '')
        self.assertEqual(rows[0]['new_value'], 'stale')
        self.assertEqual(self.ids(db.list_tasks(self.conn, needs_review=1)), [self.q['id']])

    def test_regroup(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        g = db.get_task(self.conn, self.r['id'])
        self.assertEqual(g['group'], 'priority')
        self.assertEqual(g['group_manual'], 0)
        self.assertEqual(g['group_since'], NOW_ISO)
        rows = self.events(self.r['id'], 'group_change')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['old_value'], 'queue')
        self.assertEqual(rows[0]['new_value'], 'priority')
        self.assertEqual(rows[0]['note'], 'auto')
        self.assertEqual(self.events(self.r['id'], 'manual_move'), [])

    def test_rescore(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        self.assertEqual(db.get_task(self.conn, self.r['id'])['score'], 53.8)
        self.assertEqual(db.get_task(self.conn, self.p['id'])['score'], 70.0)
        self.assertEqual(db.get_task(self.conn, self.q['id'])['score'], 60.0)

    def test_rollover_does_not_reset_evaluated_at(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        self.assertEqual(db.get_task(self.conn, self.q['id'])['evaluated_at'],
                         dt(day=10, hour=9).isoformat())

    def test_untouched_tasks_unchanged(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        g = db.get_task(self.conn, self.q['id'])
        self.assertEqual(g['group'], 'priority')
        self.assertEqual(g['carry_count'], 0)
        self.assertEqual(g['is_today'], 0)

    def test_empty_plan(self):
        plan = {'carry': [], 'review': {}, 'regroup': {}, 'rescore': {}}
        res = db.apply_rollover(self.conn, plan, TODAY, NOW)
        self.assertEqual((res['carried'], res['review'], res['regrouped'], res['rescored']), (0, 0, 0, 0))
        self.assertEqual(db.get_settings(self.conn)['last_rollover'], '2026-10-25')
        self.assertEqual(db.get_task(self.conn, self.p['id'])['is_today'], 1)
        self.assertEqual(len(self.events(self.p['id'], 'carry')), 0)

    def test_regroup_as_list_pair(self):
        # план после JSON-сериализации может содержать списки вместо кортежей
        plan = {'carry': [], 'review': {}, 'regroup': {self.r['id']: ['queue', 'priority']},
                'rescore': {self.r['id']: 53.8}}
        db.apply_rollover(self.conn, plan, TODAY, NOW)
        self.assertEqual(db.get_task(self.conn, self.r['id'])['group'], 'priority')

    def test_second_rollover_next_day(self):
        db.apply_rollover(self.conn, self.full_plan(), TODAY, NOW)
        plan2 = {'carry': [], 'review': {}, 'regroup': {}, 'rescore': {}}
        db.apply_rollover(self.conn, plan2, TOMORROW, dt(day=26))
        self.assertEqual(db.get_settings(self.conn)['last_rollover'], '2026-10-26')


# ---------------------------------------------------------------------------
# §6 journal / count_by_group
# ---------------------------------------------------------------------------
class TestJournal(DBCase):
    def test_entry_fields(self):
        t = self.make()
        rows = db.journal(self.conn)
        self.assertEqual(len(rows), 1)
        e = rows[0]
        for key in JOURNAL_COLUMNS:
            self.assertIn(key, e.keys())
        self.assertEqual(e['task_id'], t['id'])
        self.assertEqual(e['event'], 'create')
        self.assertEqual(e['ts'], NOW_ISO)
        self.assertEqual(e['note'], '')

    def test_filter_by_task(self):
        a = self.make('A')
        b = self.make('B')
        db.edit_task(self.conn, b['id'], title='B2', note=None, now=dt(hour=10))
        rows_a = db.journal(self.conn, task_id=a['id'])
        rows_b = db.journal(self.conn, task_id=b['id'])
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(len(rows_b), 2)
        self.assertTrue(all(r['task_id'] == b['id'] for r in rows_b))
        self.assertEqual(len(db.journal(self.conn)), 3)

    def test_limit(self):
        t = self.make()
        for i in range(5):
            db.edit_task(self.conn, t['id'], title='Название %d' % i, note=None, now=dt(hour=10, minute=i))
        self.assertEqual(len(db.journal(self.conn)), 6)
        self.assertEqual(len(db.journal(self.conn, limit=2)), 2)
        self.assertEqual(len(db.journal(self.conn, task_id=t['id'], limit=1)), 1)
        self.assertEqual(len(db.journal(self.conn, limit=100)), 6)

    def test_empty_journal(self):
        self.assertEqual(db.journal(self.conn), [])
        self.assertEqual(db.journal(self.conn, task_id=12345), [])

    def test_all_events_recorded_in_lifecycle(self):
        t = self.make(group='now', score=70.0, due_date='2026-10-25', due='today')
        db.edit_task(self.conn, t['id'], title='Переименована', note=None, now=dt(hour=10))
        db.add_today(self.conn, t['id'], TODAY, dt(hour=10, minute=1))
        db.remove_today(self.conn, t['id'], dt(hour=10, minute=2))
        db.move_task(self.conn, t['id'], 'priority', dt(hour=10, minute=3))
        db.apply_evaluation(self.conn, t['id'], answers(importance=5, due='today', consequence='serious'),
                            evaluation(98.5, 'now', '2026-10-25'), dt(hour=10, minute=4))
        db.set_done(self.conn, t['id'], dt(hour=10, minute=5))
        db.reopen_task(self.conn, t['id'], dt(hour=10, minute=6))
        db.delete_task(self.conn, t['id'], dt(hour=10, minute=7))
        events = [r['event'] for r in db.journal(self.conn, task_id=t['id'])]
        for ev in ('create', 'edit', 'today_add', 'today_remove', 'manual_move',
                   'evaluate', 'done', 'reopen', 'delete'):
            with self.subTest(event=ev):
                self.assertIn(ev, events)


class TestCountByGroup(DBCase):
    def test_empty_db_seven_zero_keys(self):
        # v2: count_by_group возвращает 7 ключей — шесть групп + inbox (§6)
        c = db.count_by_group(self.conn)
        self.assertEqual(set(c.keys()), set(GROUPS_V2))
        for g in GROUPS_V2:
            self.assertEqual(c[g], 0)

    def test_counts_open_only(self):
        self.make('A', group='now', score=70.0)
        self.make('B', group='now', score=66.0)
        self.make('C', group='someday', score=13.3)
        d = self.make('D', group='queue', score=30.0)
        e = self.make('E', group='queue', score=30.0)
        db.set_done(self.conn, d['id'], dt(hour=10))
        db.delete_task(self.conn, e['id'], dt(hour=10))
        c = db.count_by_group(self.conn)
        self.assertEqual(c, {'now': 2, 'priority': 0, 'queue': 0,
                             'delegate': 0, 'someday': 1, 'waiting': 0,
                             'inbox': 0})   # v2: ключ inbox присутствует всегда

    def test_counts_follow_move(self):
        t = self.make('A', group='now', score=70.0)
        db.move_task(self.conn, t['id'], 'waiting', dt(hour=10))
        c = db.count_by_group(self.conn)
        self.assertEqual(c['now'], 0)
        self.assertEqual(c['waiting'], 1)


# ---------------------------------------------------------------------------
# §1 / §6 backup
# ---------------------------------------------------------------------------
class TestBackup(unittest.TestCase):
    NAME_RE = re.compile(r'^fokus-\d{8}-\d{6}\.db$')

    def test_backup_keeps_only_last_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fokus.db')
            backups_dir = os.path.join(tmp, 'backups')
            conn = db.connect(db_path)
            try:
                db.create_task(conn, 'Задача для бэкапа', '', answers(), evaluation(26.0, 'queue'), NOW)
                conn.commit()
                paths = []
                for i in range(3):
                    if i:
                        time.sleep(1.1)  # имена файлов с точностью до секунды
                    p = db.backup(db_path, backups_dir, keep=2)
                    self.assertIsNotNone(p)
                    paths.append(p)
                self.assertEqual(len(set(paths)), 3)
                files = sorted(os.listdir(backups_dir))
                self.assertEqual(len(files), 2, files)
                for f in files:
                    self.assertRegex(f, self.NAME_RE)
                self.assertFalse(os.path.exists(paths[0]))
                self.assertTrue(os.path.exists(paths[1]))
                self.assertTrue(os.path.exists(paths[2]))
                self.assertEqual(os.path.dirname(os.path.abspath(paths[2])), os.path.abspath(backups_dir))
            finally:
                conn.close()

    def test_backup_copy_is_valid_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fokus.db')
            backups_dir = os.path.join(tmp, 'backups')
            conn = db.connect(db_path)
            try:
                db.create_task(conn, 'Задача для бэкапа', '', answers(), evaluation(26.0, 'queue'), NOW)
                conn.commit()
                p = db.backup(db_path, backups_dir, keep=10)
            finally:
                conn.close()
            self.assertTrue(os.path.exists(p))
            self.assertRegex(os.path.basename(p), self.NAME_RE)
            copy_conn = sqlite3.connect(p)
            try:
                n = copy_conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                title = copy_conn.execute('SELECT title FROM tasks').fetchone()[0]
                j = copy_conn.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
            finally:
                copy_conn.close()
            self.assertEqual(n, 1)
            self.assertEqual(title, 'Задача для бэкапа')
            self.assertEqual(j, 1)

    def test_backup_creates_backups_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fokus.db')
            backups_dir = os.path.join(tmp, 'nested', 'backups')
            conn = db.connect(db_path)
            conn.close()
            self.assertFalse(os.path.exists(backups_dir))
            p = db.backup(db_path, backups_dir, keep=10)
            self.assertIsNotNone(p)
            self.assertTrue(os.path.isdir(backups_dir))

    def test_backup_default_keep_is_10(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fokus.db')
            backups_dir = os.path.join(tmp, 'backups')
            conn = db.connect(db_path)
            conn.close()
            p = db.backup(db_path, backups_dir)
            self.assertIsNotNone(p)
            self.assertEqual(len(os.listdir(backups_dir)), 1)

    def test_backup_missing_db_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = db.backup(os.path.join(tmp, 'нет-такой.db'), os.path.join(tmp, 'backups'), keep=10)
            self.assertIsNone(p)

    def test_backup_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fokus.db')
            backups_dir = os.path.join(tmp, 'backups')
            conn = db.connect(db_path)
            try:
                t = db.create_task(conn, 'Задача', '', answers(), evaluation(26.0, 'queue'), NOW)
                conn.commit()
                db.backup(db_path, backups_dir, keep=10)
                self.assertEqual(db.get_task(conn, t['id'])['title'], 'Задача')
                self.assertEqual(len(db.list_tasks(conn)), 1)
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
