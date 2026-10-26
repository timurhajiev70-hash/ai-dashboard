# -*- coding: utf-8 -*-
"""Эталонный («золотой») набор AGENTS.md §5.4 — регрессионные тесты движка.

15 строк таблицы при весах/порогах по умолчанию, today = дата создания = 2026-10-25.
Названия задач обезличены (как в таблице; номер tomedo допустим, имён пациентов нет).
Запуск: cd fokus && python3 -m unittest discover -s tests -t .
"""
import copy
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402

TODAY = datetime.date(2026, 10, 25)
DUE_OFFSET = {'today': 0, 'tomorrow': 1, 'week': 7, 'month': 30, 'none': None}

# (название, importance, due, consequence, who, duration, depends_on, delegate_to, score, group)
REFERENCE = [
    ('Позвонить в налоговую до конца дня',
     5, 'today', 'serious', 'me', '30m', '', '', 98.5, 'now'),
    ('Отправить отчёт по пациенту tomedo #48213',
     4, 'today', 'serious', 'me', '30m', '', '', 87.3, 'now'),
    ('Записаться к зубному',
     4, 'week', 'unpleasant', 'me', '5m', '', '', 66.8, 'now'),
    ('Подготовить документы к практике',
     3, 'today', 'unpleasant', 'me', '30m', '', '', 66.0, 'now'),
    ('Купить подарок к дню рождения через месяц',
     4, 'month', 'unpleasant', 'me', '2h', '', '', 54.8, 'priority'),
    ('Оформить страховку (срока нет, критично)',
     5, 'none', 'none', 'me', 'half_day', '', '', 45.0, 'priority'),
    ('Разобрать фотоархив',
     2, 'none', 'none', 'me', '2h', '', '', 13.3, 'someday'),
    ('Почитать статью про ИИ',
     2, 'week', 'none', 'me', '30m', '', '', 32.8, 'queue'),
    ('Прочитать договор, средняя важность, в этом месяце',
     3, 'month', 'none', 'me', '30m', '', '', 35.0, 'queue'),
    ('Заказать канцтовары (поручить Свете)',
     3, 'week', 'unpleasant', 'delegate', '30m', '', 'Света', 54.0, 'delegate'),
    ('Проверить сайт после обновления (поручить Миру)',
     2, 'none', 'none', 'delegate', '5m', '', 'Мир', 16.3, 'delegate'),
    ('Подать заявку — ждёт справку из банка',
     5, 'week', 'serious', 'me', '30m', 'справка из банка', '', 86.5, 'waiting'),
    ('Купить лампочку',
     1, 'today', 'none', 'me', '5m', '', '', 35.0, 'queue'),
    ('Написать благодарность коллеге',
     2, 'none', 'unpleasant', 'me', '30m', '', '', 24.8, 'someday'),
    ('Критично, завтра, последствий нет, полдня',
     5, 'tomorrow', 'none', 'me', 'half_day', '', '', 69.0, 'now'),
]


def build_answers(row):
    _, imp, due, cons, who, dur, dep, delegate_to, _, _ = row
    return {
        'importance': imp,
        'due': due,
        'consequence': cons,
        'who': who,
        'delegate_to': delegate_to,
        'duration': dur,
        'depends_on': dep,
    }


def row_by_title(prefix):
    for row in REFERENCE:
        if row[0].startswith(prefix):
            return row
    raise KeyError(prefix)


class TestReferenceTable(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(engine.DEFAULT_SETTINGS)

    def evaluate(self, row):
        return engine.evaluate(build_answers(row), TODAY, self.settings)

    def test_table_has_15_rows(self):
        self.assertEqual(len(REFERENCE), 15)
        self.assertEqual(len(set(r[0] for r in REFERENCE)), 15)

    def test_scores_and_groups(self):
        for row in REFERENCE:
            title, expected_score, expected_group = row[0], row[8], row[9]
            with self.subTest(task=title):
                r = self.evaluate(row)
                self.assertAlmostEqual(r['score'], expected_score, places=1,
                                       msg='балл для «%s»' % title)
                self.assertEqual(r['group'], expected_group, 'группа для «%s»' % title)

    def test_scores_exact_after_half_up_rounding(self):
        # Балл хранится с одним знаком; эталон должен совпадать точно (half up, §5.3)
        for row in REFERENCE:
            with self.subTest(task=row[0]):
                self.assertEqual(self.evaluate(row)['score'], row[8])

    def test_due_dates(self):
        for row in REFERENCE:
            with self.subTest(task=row[0]):
                off = DUE_OFFSET[row[2]]
                r = self.evaluate(row)
                if off is None:
                    self.assertIsNone(r['due_date'])
                else:
                    self.assertEqual(r['due_date'],
                                     (TODAY + datetime.timedelta(days=off)).isoformat())

    def test_row_1_manual_sum(self):
        # 0.45×10×10 + 0.30×10×10 + 0.20×10×10 + 0.05×7×10 = 45 + 30 + 20 + 3.5 = 98.5
        row = row_by_title('Позвонить в налоговую')
        self.assertEqual(engine.compute_score(build_answers(row), TODAY, self.settings), 98.5)

    def test_row_2_half_up_87_3(self):
        # 33.75 + 30 + 20 + 3.5 = 87.25 → 87.3 (не 87.2)
        row = row_by_title('Отправить отчёт по пациенту tomedo')
        self.assertEqual(self.evaluate(row)['score'], 87.3)

    def test_dentist_has_do_now_hint(self):
        row = row_by_title('Записаться к зубному')
        r = self.evaluate(row)
        self.assertEqual(r['group'], 'now')
        self.assertIn('do_now', r['hints'])

    def test_delegate_rows_have_delegate_hint(self):
        for prefix in ('Заказать канцтовары', 'Проверить сайт после обновления'):
            with self.subTest(task=prefix):
                r = self.evaluate(row_by_title(prefix))
                self.assertEqual(r['group'], 'delegate')
                self.assertIn('delegate', r['hints'])

    def test_waiting_row_has_waiting_hint(self):
        r = self.evaluate(row_by_title('Подать заявку'))
        self.assertEqual(r['group'], 'waiting')
        self.assertIn('waiting', r['hints'])

    def test_someday_rows_have_someday_review_hint(self):
        for prefix in ('Разобрать фотоархив', 'Написать благодарность коллеге'):
            with self.subTest(task=prefix):
                r = self.evaluate(row_by_title(prefix))
                self.assertEqual(r['group'], 'someday')
                self.assertIn('someday_review', r['hints'])

    def test_non_someday_rows_have_no_someday_review_hint(self):
        for row in REFERENCE:
            if row[9] != 'someday':
                with self.subTest(task=row[0]):
                    self.assertNotIn('someday_review', self.evaluate(row)['hints'])

    def test_no_do_now_where_not_applicable(self):
        # 5m, но importance 1 → нет; 30m → нет
        self.assertNotIn('do_now', self.evaluate(row_by_title('Купить лампочку'))['hints'])
        self.assertNotIn('do_now', self.evaluate(row_by_title('Позвонить в налоговую'))['hints'])
        self.assertNotIn('do_now', self.evaluate(row_by_title('Проверить сайт после обновления'))['hints'])

    def test_me_rows_have_no_delegate_hint(self):
        for row in REFERENCE:
            if row[4] == 'me':
                with self.subTest(task=row[0]):
                    self.assertNotIn('delegate', self.evaluate(row)['hints'])

    def test_rows_without_dependency_have_no_waiting_hint(self):
        for row in REFERENCE:
            if not row[6]:
                with self.subTest(task=row[0]):
                    self.assertNotIn('waiting', self.evaluate(row)['hints'])

    def test_normalized_answers_returned(self):
        for row in REFERENCE:
            with self.subTest(task=row[0]):
                r = self.evaluate(row)
                self.assertEqual(r['answers']['importance'], row[1])
                self.assertEqual(r['answers']['due'], row[2])
                self.assertEqual(r['answers']['who'], row[4])
                self.assertEqual(r['answers']['depends_on'], row[6])
                self.assertEqual(r['answers']['delegate_to'], row[7])

    def test_group_distribution(self):
        groups = [self.evaluate(row)['group'] for row in REFERENCE]
        self.assertEqual(groups.count('now'), 5)
        self.assertEqual(groups.count('priority'), 2)
        self.assertEqual(groups.count('queue'), 3)
        self.assertEqual(groups.count('delegate'), 2)
        self.assertEqual(groups.count('someday'), 2)
        self.assertEqual(groups.count('waiting'), 1)

    def test_default_settings_untouched(self):
        snapshot = copy.deepcopy(engine.DEFAULT_SETTINGS)
        for row in REFERENCE:
            engine.evaluate(build_answers(row), TODAY, engine.DEFAULT_SETTINGS)
        self.assertEqual(engine.DEFAULT_SETTINGS, snapshot)


if __name__ == '__main__':
    unittest.main()
