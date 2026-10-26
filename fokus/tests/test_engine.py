# -*- coding: utf-8 -*-
"""Блок B — тесты движка приоритета (engine.py) по AGENTS.md §5 и §5.6.

Тесты написаны ПО СПЕЦИФИКАЦИИ, без чтения кода движка.
«Сегодня» во всех тестах — 2026-10-25, настройки — явная копия DEFAULT_SETTINGS.
Данные обезличены (никаких имён пациентов).
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

# §5.1: срок → смещение в днях от даты создания
DUE_OFFSET = {'today': 0, 'tomorrow': 1, 'week': 7, 'month': 30, 'none': None}


def days(n):
    return datetime.timedelta(days=n)


def iso(d, hour=9):
    """date → ISO datetime без зоны, как хранится в БД (§6)."""
    return datetime.datetime(d.year, d.month, d.day, hour, 0, 0).isoformat()


def settings():
    """Явная независимая копия настроек по умолчанию (§4.2)."""
    return copy.deepcopy(engine.DEFAULT_SETTINGS)


def answers(importance=3, due='none', consequence='none', who='me',
            duration='30m', depends_on='', delegate_to='', **extra):
    """Нормализованные ответы мастера (§3)."""
    a = {
        'importance': importance,
        'due': due,
        'consequence': consequence,
        'who': who,
        'delegate_to': delegate_to,
        'duration': duration,
        'depends_on': depends_on,
    }
    a.update(extra)
    return a


def task(task_id, score, group, importance=3, due='none', consequence='none',
         who='me', duration='30m', depends_on='', delegate_to='',
         created=TODAY, evaluated=None, group_manual=0, is_today=0,
         today_date=None, carry_count=0, needs_review=0, review_reason=''):
    """Задача как объект API (поля §4, grp → group). due_date считается от даты создания."""
    off = DUE_OFFSET[due]
    due_date = None if off is None else (created + days(off)).isoformat()
    if evaluated is None:
        evaluated = created
    return {
        'id': task_id,
        'title': 'Задача %d' % task_id,
        'note': '',
        'importance': importance,
        'due': due,
        'due_date': due_date,
        'consequence': consequence,
        'who': who,
        'delegate_to': delegate_to,
        'duration': duration,
        'depends_on': depends_on,
        'score': score,
        'group': group,
        'group_manual': group_manual,
        'group_since': iso(created),
        'evaluated_at': iso(evaluated),
        'needs_review': needs_review,
        'review_reason': review_reason,
        'is_today': is_today,
        'today_date': today_date,
        'carry_count': carry_count,
        'status': 'open',
        'done_at': None,
        'created_at': iso(created),
        'updated_at': iso(created),
    }


# ---------------------------------------------------------------------------
# Константы §5.6 / §4.2
# ---------------------------------------------------------------------------
class TestConstants(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(tuple(engine.GROUPS),
                         ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting'))

    def test_value_tuples(self):
        self.assertEqual(set(engine.DUE_VALUES), {'today', 'tomorrow', 'week', 'month', 'none'})
        self.assertEqual(set(engine.CONSEQUENCE_VALUES), {'none', 'unpleasant', 'serious'})
        self.assertEqual(set(engine.WHO_VALUES), {'me', 'delegate'})
        self.assertEqual(set(engine.DURATION_VALUES), {'5m', '30m', '2h', 'half_day'})

    def test_default_settings_values(self):
        s = engine.DEFAULT_SETTINGS
        self.assertEqual(s['weights'], {'importance': 0.45, 'urgency': 0.30,
                                        'consequence': 0.20, 'duration': 0.05})
        self.assertEqual(s['thresholds'], {'now': 65, 'priority': 45, 'queue': 25})
        self.assertEqual(s['day_limit'], 3)
        self.assertEqual(s['stale_days'], 7)
        self.assertEqual(s['carry_limit'], 3)
        self.assertEqual(s['language'], 'ru')
        self.assertIsNone(s['last_rollover'])

    def test_default_settings_has_all_keys(self):
        self.assertEqual(set(engine.DEFAULT_SETTINGS.keys()),
                         {'weights', 'thresholds', 'day_limit', 'stale_days',
                          'carry_limit', 'language', 'last_rollover'})


# ---------------------------------------------------------------------------
# §5.1 due_date_for
# ---------------------------------------------------------------------------
class TestDueDateFor(unittest.TestCase):
    def test_today(self):
        self.assertEqual(engine.due_date_for('today', TODAY), TODAY)

    def test_tomorrow(self):
        self.assertEqual(engine.due_date_for('tomorrow', TODAY), datetime.date(2026, 10, 26))

    def test_week_is_plus_7_days(self):
        self.assertEqual(engine.due_date_for('week', TODAY), datetime.date(2026, 11, 1))

    def test_month_is_plus_30_days(self):
        self.assertEqual(engine.due_date_for('month', TODAY), datetime.date(2026, 11, 24))

    def test_none(self):
        self.assertIsNone(engine.due_date_for('none', TODAY))

    def test_returns_date_type(self):
        for due in ('today', 'tomorrow', 'week', 'month'):
            with self.subTest(due=due):
                self.assertIsInstance(engine.due_date_for(due, TODAY), datetime.date)

    def test_month_across_year_end(self):
        self.assertEqual(engine.due_date_for('month', datetime.date(2026, 12, 20)),
                         datetime.date(2027, 1, 19))


# ---------------------------------------------------------------------------
# §5.2 days_left
# ---------------------------------------------------------------------------
class TestDaysLeft(unittest.TestCase):
    def test_none_date(self):
        self.assertIsNone(engine.days_left(None, TODAY))

    def test_same_day_is_zero(self):
        self.assertEqual(engine.days_left(TODAY, TODAY), 0)

    def test_tomorrow_is_one(self):
        self.assertEqual(engine.days_left(TODAY + days(1), TODAY), 1)

    def test_week(self):
        self.assertEqual(engine.days_left(TODAY + days(7), TODAY), 7)

    def test_month(self):
        self.assertEqual(engine.days_left(TODAY + days(30), TODAY), 30)

    def test_overdue_is_negative(self):
        self.assertEqual(engine.days_left(TODAY - days(3), TODAY), -3)
        self.assertEqual(engine.days_left(TODAY - days(1), TODAY), -1)

    def test_returns_int(self):
        self.assertIsInstance(engine.days_left(TODAY + days(2), TODAY), int)


# ---------------------------------------------------------------------------
# §5.2 баллы по измерениям
# ---------------------------------------------------------------------------
class TestUrgencyPoints(unittest.TestCase):
    def u(self, n):
        return engine.urgency_points(TODAY + days(n), TODAY)

    def test_no_date_is_zero(self):
        self.assertEqual(engine.urgency_points(None, TODAY), 0)

    def test_today_is_10(self):
        self.assertEqual(self.u(0), 10)

    def test_overdue_is_10(self):
        self.assertEqual(self.u(-1), 10)
        self.assertEqual(self.u(-30), 10)
        self.assertEqual(self.u(-365), 10)

    def test_one_day_is_8(self):
        self.assertEqual(self.u(1), 8)

    def test_2_to_7_days_is_6(self):
        self.assertEqual(self.u(2), 6)
        self.assertEqual(self.u(5), 6)
        self.assertEqual(self.u(7), 6)

    def test_8_to_30_days_is_3(self):
        self.assertEqual(self.u(8), 3)
        self.assertEqual(self.u(15), 3)
        self.assertEqual(self.u(30), 3)

    def test_over_30_days_is_0(self):
        self.assertEqual(self.u(31), 0)
        self.assertEqual(self.u(100), 0)

    def test_all_boundaries_table(self):
        table = {-10: 10, 0: 10, 1: 8, 2: 6, 7: 6, 8: 3, 30: 3, 31: 0}
        for n, expected in table.items():
            with self.subTest(days_left=n):
                self.assertEqual(self.u(n), expected)

    def test_range_0_10(self):
        for n in range(-5, 60):
            p = self.u(n)
            self.assertGreaterEqual(p, 0)
            self.assertLessEqual(p, 10)


class TestImportancePoints(unittest.TestCase):
    def test_table(self):
        expected = {1: 0, 2: 2.5, 3: 5, 4: 7.5, 5: 10}
        for imp, pts in expected.items():
            with self.subTest(importance=imp):
                self.assertEqual(engine.importance_points(imp), pts)


class TestConsequencePoints(unittest.TestCase):
    def test_table(self):
        expected = {'none': 0, 'unpleasant': 5, 'serious': 10}
        for code, pts in expected.items():
            with self.subTest(consequence=code):
                self.assertEqual(engine.consequence_points(code), pts)


class TestDurationPoints(unittest.TestCase):
    def test_table(self):
        expected = {'5m': 10, '30m': 7, '2h': 4, 'half_day': 0}
        for code, pts in expected.items():
            with self.subTest(duration=code):
                self.assertEqual(engine.duration_points(code), pts)

    def test_shorter_is_higher(self):
        self.assertGreater(engine.duration_points('5m'), engine.duration_points('30m'))
        self.assertGreater(engine.duration_points('30m'), engine.duration_points('2h'))
        self.assertGreater(engine.duration_points('2h'), engine.duration_points('half_day'))


# ---------------------------------------------------------------------------
# §5.3 compute_score
# ---------------------------------------------------------------------------
class TestComputeScore(unittest.TestCase):
    def score(self, a, s=None):
        return engine.compute_score(a, TODAY, s or settings())

    def test_maximum_is_100(self):
        self.assertEqual(self.score(answers(5, 'today', 'serious', duration='5m')), 100.0)

    def test_minimum_is_0(self):
        self.assertEqual(self.score(answers(1, 'none', 'none', duration='half_day')), 0.0)

    def test_reference_row_1(self):
        # 0.45×10×10 + 0.30×10×10 + 0.20×10×10 + 0.05×7×10 = 45 + 30 + 20 + 3.5
        self.assertEqual(self.score(answers(5, 'today', 'serious', duration='30m')), 98.5)

    def test_half_up_rounding_87_25_is_87_3(self):
        # 33.75 + 30 + 20 + 3.5 = 87.25 → 87.3 (half up), а не 87.2 (банковское round)
        self.assertEqual(self.score(answers(4, 'today', 'serious', duration='30m')), 87.3)

    def test_half_up_rounding_other_cases(self):
        cases = [
            (answers(4, 'week', 'unpleasant', duration='5m'), 66.8),    # 66.75
            (answers(4, 'month', 'unpleasant', duration='2h'), 54.8),   # 54.75
            (answers(2, 'none', 'none', duration='2h'), 13.3),          # 13.25
            (answers(2, 'week', 'none', duration='30m'), 32.8),         # 32.75
            (answers(2, 'none', 'none', duration='5m'), 16.3),          # 16.25
            (answers(2, 'none', 'unpleasant', duration='30m'), 24.8),   # 24.75
            (answers(4, 'month', 'none', duration='2h'), 44.8),         # 44.75
            (answers(5, 'none', 'none', duration='30m'), 48.5),         # 45 + 3.5
            (answers(3, 'week', 'none', duration='30m'), 44.0),         # 22.5 + 18 + 3.5
        ]
        for a, expected in cases:
            with self.subTest(answers=a):
                self.assertEqual(self.score(a), expected)

    def test_returns_float(self):
        self.assertIsInstance(self.score(answers(3, 'week', 'none')), float)

    def test_range_and_one_decimal_for_all_combinations(self):
        for imp in range(1, 6):
            for due in ('today', 'tomorrow', 'week', 'month', 'none'):
                for cons in ('none', 'unpleasant', 'serious'):
                    for dur in ('5m', '30m', '2h', 'half_day'):
                        sc = self.score(answers(imp, due, cons, duration=dur))
                        self.assertGreaterEqual(sc, 0.0)
                        self.assertLessEqual(sc, 100.0)
                        self.assertEqual(round(sc, 1), sc,
                                         'балл должен иметь один знак после запятой')

    def test_weights_scaled_x2_give_same_score(self):
        s2 = settings()
        s2['weights'] = dict((k, v * 2) for k, v in s2['weights'].items())
        for a in (answers(4, 'today', 'serious', duration='30m'),   # 87.25 — граница округления
                  answers(5, 'today', 'serious', duration='5m'),
                  answers(2, 'week', 'none', duration='30m'),
                  answers(3, 'month', 'unpleasant', duration='2h')):
            with self.subTest(answers=a):
                self.assertEqual(self.score(a, s2), self.score(a))

    def test_weights_scaled_x3_give_same_score(self):
        s3 = settings()
        s3['weights'] = dict((k, v * 3) for k, v in s3['weights'].items())
        a = answers(3, 'week', 'unpleasant', duration='2h')  # 22.5 + 18 + 10 + 2 = 52.5
        self.assertEqual(self.score(a, s3), 52.5)
        self.assertEqual(self.score(answers(5, 'today', 'serious', duration='5m'), s3), 100.0)

    def test_weights_equal_unnormalized(self):
        s = settings()
        s['weights'] = {'importance': 1, 'urgency': 1, 'consequence': 1, 'duration': 1}
        self.assertEqual(self.score(answers(5, 'none', 'none', duration='half_day'), s), 25.0)
        self.assertEqual(self.score(answers(1, 'none', 'none', duration='5m'), s), 25.0)
        self.assertEqual(self.score(answers(5, 'today', 'serious', duration='5m'), s), 100.0)
        self.assertEqual(self.score(answers(1, 'none', 'none', duration='half_day'), s), 0.0)

    def test_weights_single_dimension(self):
        s = settings()
        s['weights'] = {'importance': 1, 'urgency': 0, 'consequence': 0, 'duration': 0}
        self.assertEqual(self.score(answers(1, 'today', 'serious', duration='5m'), s), 0.0)
        self.assertEqual(self.score(answers(3, 'today', 'serious', duration='5m'), s), 50.0)
        self.assertEqual(self.score(answers(5, 'none', 'none', duration='half_day'), s), 100.0)

    def test_due_date_string_overrides_due(self):
        # due='month' дал бы 45 + 9 + 20 + 5 = 79.0; due_date сегодня → 100.0
        a = answers(5, 'month', 'serious', duration='5m')
        self.assertEqual(self.score(a), 79.0)
        a['due_date'] = '2026-10-25'
        self.assertEqual(self.score(a), 100.0)

    def test_due_date_date_object_overrides_due(self):
        a = answers(5, 'month', 'serious', duration='5m')
        a['due_date'] = TODAY
        self.assertEqual(self.score(a), 100.0)

    def test_due_date_far_future_string(self):
        # 36 дней → срочность 0: 45 + 0 + 20 + 5 = 70.0
        a = answers(5, 'today', 'serious', duration='5m')
        a['due_date'] = '2026-11-30'
        self.assertEqual(self.score(a), 70.0)

    def test_due_date_overdue_string(self):
        # просрочено → срочность 10: 22.5 + 30 + 0 + 3.5 = 56.0
        a = answers(3, 'week', 'none', duration='30m')
        a['due_date'] = '2026-10-20'
        self.assertEqual(self.score(a), 56.0)

    def test_due_date_none_with_due_none(self):
        a = answers(3, 'none', 'none', duration='30m')
        a['due_date'] = None
        self.assertEqual(self.score(a), 26.0)  # 22.5 + 0 + 0 + 3.5

    def test_who_and_depends_on_do_not_affect_score(self):
        base = answers(4, 'week', 'unpleasant', duration='30m')
        delegated = answers(4, 'week', 'unpleasant', who='delegate', delegate_to='Света', duration='30m')
        waiting = answers(4, 'week', 'unpleasant', duration='30m', depends_on='справка из банка')
        self.assertEqual(self.score(delegated), self.score(base))
        self.assertEqual(self.score(waiting), self.score(base))


# ---------------------------------------------------------------------------
# §5.4 assign_group
# ---------------------------------------------------------------------------
class TestAssignGroup(unittest.TestCase):
    def group(self, score, a=None, s=None):
        return engine.assign_group(a or answers(), score, s or settings())

    def test_thresholds_inclusive(self):
        self.assertEqual(self.group(65.0), 'now')
        self.assertEqual(self.group(45.0), 'priority')
        self.assertEqual(self.group(25.0), 'queue')

    def test_just_below_thresholds(self):
        self.assertEqual(self.group(64.9), 'priority')
        self.assertEqual(self.group(44.9), 'queue')
        self.assertEqual(self.group(24.9), 'someday')

    def test_extremes(self):
        self.assertEqual(self.group(100.0), 'now')
        self.assertEqual(self.group(0.0), 'someday')

    def test_middle_values(self):
        self.assertEqual(self.group(80.0), 'now')
        self.assertEqual(self.group(50.0), 'priority')
        self.assertEqual(self.group(30.0), 'queue')
        self.assertEqual(self.group(10.0), 'someday')

    def test_delegate_beats_score_and_dependency(self):
        a = answers(5, 'today', 'serious', who='delegate', delegate_to='Света',
                    duration='5m', depends_on='справка из банка')
        self.assertEqual(self.group(100.0, a), 'delegate')
        self.assertEqual(self.group(0.0, a), 'delegate')

    def test_delegate_with_empty_delegate_to(self):
        a = answers(2, 'none', 'none', who='delegate', delegate_to='', duration='5m')
        self.assertEqual(self.group(16.3, a), 'delegate')

    def test_waiting_at_any_score(self):
        a = answers(5, 'week', 'serious', duration='30m', depends_on='справка из банка')
        self.assertEqual(self.group(100.0, a), 'waiting')
        self.assertEqual(self.group(86.5, a), 'waiting')
        self.assertEqual(self.group(0.0, a), 'waiting')

    def test_empty_depends_on_is_not_waiting(self):
        for dep in ('', None, '   '):
            with self.subTest(depends_on=repr(dep)):
                a = answers(5, 'today', 'serious', duration='5m', depends_on=dep)
                self.assertEqual(self.group(100.0, a), 'now')

    def test_custom_thresholds(self):
        s = settings()
        s['thresholds'] = {'now': 80, 'priority': 50, 'queue': 20}
        self.assertEqual(self.group(80.0, s=s), 'now')
        self.assertEqual(self.group(79.9, s=s), 'priority')
        self.assertEqual(self.group(70.0, s=s), 'priority')
        self.assertEqual(self.group(50.0, s=s), 'priority')
        self.assertEqual(self.group(49.9, s=s), 'queue')
        self.assertEqual(self.group(20.0, s=s), 'queue')
        self.assertEqual(self.group(19.9, s=s), 'someday')

    def test_result_is_known_group(self):
        for sc in (0.0, 24.9, 25.0, 44.9, 45.0, 64.9, 65.0, 100.0):
            self.assertIn(self.group(sc), engine.GROUPS)


# ---------------------------------------------------------------------------
# §5.6 validate_answers
# ---------------------------------------------------------------------------
class TestValidateAnswers(unittest.TestCase):
    REQUIRED = ('importance', 'due', 'consequence', 'who', 'duration')

    def assertFieldError(self, a, field):
        with self.assertRaises(ValueError) as cm:
            engine.validate_answers(a)
        self.assertEqual(cm.exception.args[0], field)

    def test_valid_answers_returned_as_dict(self):
        r = engine.validate_answers(answers(4, 'today', 'serious', duration='30m'))
        self.assertIsInstance(r, dict)
        for key in ('importance', 'due', 'consequence', 'who', 'delegate_to', 'duration', 'depends_on'):
            self.assertIn(key, r)

    def test_importance_string_becomes_int(self):
        r = engine.validate_answers(answers('4', 'today', 'serious', duration='30m'))
        self.assertEqual(r['importance'], 4)
        self.assertIsInstance(r['importance'], int)

    def test_importance_int_kept(self):
        for imp in range(1, 6):
            with self.subTest(importance=imp):
                r = engine.validate_answers(answers(imp, 'today', 'none', duration='5m'))
                self.assertEqual(r['importance'], imp)

    def test_importance_string_all_values(self):
        for imp in ('1', '2', '3', '4', '5'):
            with self.subTest(importance=imp):
                r = engine.validate_answers(answers(imp, 'today', 'none', duration='5m'))
                self.assertEqual(r['importance'], int(imp))

    def test_text_fields_stripped(self):
        r = engine.validate_answers(answers(3, 'week', 'none', who='delegate',
                                            delegate_to='  Света ', duration='30m',
                                            depends_on='  справка из банка  '))
        self.assertEqual(r['delegate_to'], 'Света')
        self.assertEqual(r['depends_on'], 'справка из банка')

    def test_missing_optional_fields_become_empty_string(self):
        a = {'importance': 3, 'due': 'week', 'consequence': 'none', 'who': 'me', 'duration': '30m'}
        r = engine.validate_answers(a)
        self.assertEqual(r['delegate_to'], '')
        self.assertEqual(r['depends_on'], '')

    def test_none_optional_fields_become_empty_string(self):
        r = engine.validate_answers(answers(3, 'week', 'none', duration='30m',
                                            depends_on=None, delegate_to=None))
        self.assertEqual(r['delegate_to'], '')
        self.assertEqual(r['depends_on'], '')

    def test_whitespace_only_depends_on_becomes_empty(self):
        r = engine.validate_answers(answers(3, 'week', 'none', duration='30m', depends_on='   '))
        self.assertEqual(r['depends_on'], '')

    def test_codes_preserved(self):
        for due in ('today', 'tomorrow', 'week', 'month', 'none'):
            for cons in ('none', 'unpleasant', 'serious'):
                for who in ('me', 'delegate'):
                    for dur in ('5m', '30m', '2h', 'half_day'):
                        r = engine.validate_answers(answers(3, due, cons, who=who, duration=dur))
                        self.assertEqual((r['due'], r['consequence'], r['who'], r['duration']),
                                         (due, cons, who, dur))

    def test_delegate_with_empty_delegate_to_is_valid(self):
        r = engine.validate_answers(answers(3, 'week', 'none', who='delegate', duration='30m'))
        self.assertEqual(r['who'], 'delegate')
        self.assertEqual(r['delegate_to'], '')

    def test_importance_0(self):
        self.assertFieldError(answers(0, 'today', 'none', duration='5m'), 'importance')

    def test_importance_6(self):
        self.assertFieldError(answers(6, 'today', 'none', duration='5m'), 'importance')

    def test_importance_text(self):
        self.assertFieldError(answers('abc', 'today', 'none', duration='5m'), 'importance')

    def test_importance_none(self):
        self.assertFieldError(answers(None, 'today', 'none', duration='5m'), 'importance')

    def test_importance_string_out_of_range(self):
        self.assertFieldError(answers('0', 'today', 'none', duration='5m'), 'importance')
        self.assertFieldError(answers('6', 'today', 'none', duration='5m'), 'importance')

    def test_importance_missing(self):
        a = answers(3, 'today', 'none', duration='5m')
        del a['importance']
        self.assertFieldError(a, 'importance')

    def test_due_unknown(self):
        self.assertFieldError(answers(3, 'yesterday', 'none', duration='5m'), 'due')

    def test_due_missing(self):
        a = answers(3, 'today', 'none', duration='5m')
        del a['due']
        self.assertFieldError(a, 'due')

    def test_due_none_value(self):
        self.assertFieldError(answers(3, None, 'none', duration='5m'), 'due')

    def test_consequence_unknown(self):
        self.assertFieldError(answers(3, 'today', 'catastrophic', duration='5m'), 'consequence')

    def test_consequence_missing(self):
        a = answers(3, 'today', 'none', duration='5m')
        del a['consequence']
        self.assertFieldError(a, 'consequence')

    def test_who_unknown(self):
        self.assertFieldError(answers(3, 'today', 'none', who='them', duration='5m'), 'who')

    def test_who_missing(self):
        a = answers(3, 'today', 'none', duration='5m')
        del a['who']
        self.assertFieldError(a, 'who')

    def test_duration_unknown(self):
        self.assertFieldError(answers(3, 'today', 'none', duration='1h'), 'duration')

    def test_duration_missing(self):
        a = answers(3, 'today', 'none', duration='5m')
        del a['duration']
        self.assertFieldError(a, 'duration')

    def test_errors_are_value_error(self):
        with self.assertRaises(ValueError):
            engine.validate_answers({})


# ---------------------------------------------------------------------------
# §5.5 hints_for
# ---------------------------------------------------------------------------
class TestHintsFor(unittest.TestCase):
    def hints(self, a, group='queue'):
        return engine.hints_for(a, group)

    def test_returns_list_of_strings(self):
        h = self.hints(answers(5, 'today', 'serious', who='delegate', duration='5m',
                               depends_on='справка'), 'delegate')
        self.assertIsInstance(h, list)
        for item in h:
            self.assertIsInstance(item, str)

    def test_do_now_5m_importance_3(self):
        self.assertIn('do_now', self.hints(answers(3, 'week', 'none', duration='5m'), 'queue'))

    def test_do_now_5m_importance_4_and_5(self):
        self.assertIn('do_now', self.hints(answers(4, 'week', 'unpleasant', duration='5m'), 'now'))
        self.assertIn('do_now', self.hints(answers(5, 'today', 'serious', duration='5m'), 'now'))

    def test_no_do_now_5m_importance_2(self):
        self.assertNotIn('do_now', self.hints(answers(2, 'week', 'none', duration='5m'), 'queue'))

    def test_no_do_now_5m_importance_1(self):
        self.assertNotIn('do_now', self.hints(answers(1, 'today', 'none', duration='5m'), 'queue'))

    def test_no_do_now_for_longer_duration(self):
        for dur in ('30m', '2h', 'half_day'):
            with self.subTest(duration=dur):
                self.assertNotIn('do_now', self.hints(answers(5, 'today', 'serious', duration=dur), 'now'))

    def test_delegate_hint(self):
        a = answers(3, 'week', 'unpleasant', who='delegate', delegate_to='Света', duration='30m')
        self.assertIn('delegate', self.hints(a, 'delegate'))

    def test_delegate_hint_without_delegate_to(self):
        a = answers(2, 'none', 'none', who='delegate', duration='5m')
        self.assertIn('delegate', self.hints(a, 'delegate'))

    def test_no_delegate_hint_for_me(self):
        self.assertNotIn('delegate', self.hints(answers(3, 'week', 'none', duration='30m'), 'queue'))

    def test_waiting_hint(self):
        a = answers(5, 'week', 'serious', duration='30m', depends_on='справка из банка')
        self.assertIn('waiting', self.hints(a, 'waiting'))

    def test_no_waiting_hint_without_dependency(self):
        self.assertNotIn('waiting', self.hints(answers(5, 'week', 'serious', duration='30m'), 'now'))

    def test_someday_review_only_for_someday_group(self):
        a = answers(2, 'none', 'none', duration='2h')
        for group in engine.GROUPS:
            with self.subTest(group=group):
                has = 'someday_review' in self.hints(a, group)
                self.assertEqual(has, group == 'someday')

    def test_hints_combine_independently(self):
        a = answers(5, 'today', 'serious', who='delegate', delegate_to='Света', duration='5m')
        h = self.hints(a, 'delegate')
        self.assertIn('delegate', h)
        self.assertIn('do_now', h)

    def test_plain_task_has_no_hints(self):
        self.assertEqual(self.hints(answers(3, 'week', 'none', duration='30m'), 'queue'), [])

    def test_no_duplicates(self):
        a = answers(5, 'today', 'serious', who='delegate', duration='5m', depends_on='справка')
        h = self.hints(a, 'delegate')
        self.assertEqual(len(h), len(set(h)))


# ---------------------------------------------------------------------------
# §5.6 evaluate
# ---------------------------------------------------------------------------
class TestEvaluate(unittest.TestCase):
    def ev(self, a, s=None):
        return engine.evaluate(a, TODAY, s or settings())

    def test_result_keys(self):
        r = self.ev(answers(4, 'today', 'serious', duration='30m'))
        self.assertEqual(set(r.keys()) & {'score', 'group', 'due_date', 'hints', 'answers'},
                         {'score', 'group', 'due_date', 'hints', 'answers'})

    def test_result_types(self):
        r = self.ev(answers(4, 'today', 'serious', duration='30m'))
        self.assertIsInstance(r['score'], float)
        self.assertIsInstance(r['group'], str)
        self.assertIsInstance(r['due_date'], str)
        self.assertIsInstance(r['hints'], list)
        self.assertIsInstance(r['answers'], dict)

    def test_due_date_string_for_each_due(self):
        for due, off in DUE_OFFSET.items():
            with self.subTest(due=due):
                r = self.ev(answers(3, due, 'none', duration='30m'))
                if off is None:
                    self.assertIsNone(r['due_date'])
                else:
                    self.assertEqual(r['due_date'], (TODAY + days(off)).isoformat())

    def test_reference_task(self):
        r = self.ev(answers(5, 'today', 'serious', duration='30m'))
        self.assertEqual(r['score'], 98.5)
        self.assertEqual(r['group'], 'now')
        self.assertEqual(r['due_date'], '2026-10-25')

    def test_score_and_group_consistent_with_parts(self):
        a = answers(4, 'month', 'unpleasant', duration='2h')
        r = self.ev(a)
        self.assertEqual(r['score'], engine.compute_score(a, TODAY, settings()))
        self.assertEqual(r['group'], engine.assign_group(a, r['score'], settings()))

    def test_answers_normalized(self):
        a = {'importance': '4', 'due': 'today', 'consequence': 'serious', 'who': 'me', 'duration': '30m'}
        r = self.ev(a)
        self.assertEqual(r['answers']['importance'], 4)
        self.assertEqual(r['answers']['delegate_to'], '')
        self.assertEqual(r['answers']['depends_on'], '')
        self.assertEqual(r['score'], 87.3)

    def test_due_date_in_answers_used_string(self):
        a = answers(5, 'month', 'serious', duration='5m')
        a['due_date'] = '2026-10-25'
        r = self.ev(a)
        self.assertEqual(r['due_date'], '2026-10-25')
        self.assertEqual(r['score'], 100.0)
        self.assertEqual(r['group'], 'now')

    def test_due_date_in_answers_used_date_object(self):
        a = answers(5, 'month', 'serious', duration='5m')
        a['due_date'] = TODAY
        r = self.ev(a)
        self.assertEqual(r['due_date'], '2026-10-25')
        self.assertIsInstance(r['due_date'], str)
        self.assertEqual(r['score'], 100.0)

    def test_hints_included(self):
        r = self.ev(answers(3, 'week', 'unpleasant', who='delegate', delegate_to='Света', duration='30m'))
        self.assertEqual(r['group'], 'delegate')
        self.assertIn('delegate', r['hints'])
        r2 = self.ev(answers(2, 'none', 'none', duration='2h'))
        self.assertEqual(r2['group'], 'someday')
        self.assertIn('someday_review', r2['hints'])
        r3 = self.ev(answers(4, 'week', 'unpleasant', duration='5m'))
        self.assertIn('do_now', r3['hints'])

    def test_waiting_group_and_hint(self):
        r = self.ev(answers(5, 'week', 'serious', duration='30m', depends_on='справка из банка'))
        self.assertEqual(r['group'], 'waiting')
        self.assertIn('waiting', r['hints'])
        self.assertEqual(r['score'], 86.5)

    def test_invalid_answers_raise_value_error_with_field(self):
        with self.assertRaises(ValueError) as cm:
            self.ev(answers(0, 'today', 'none', duration='5m'))
        self.assertEqual(cm.exception.args[0], 'importance')
        with self.assertRaises(ValueError) as cm:
            self.ev(answers(3, 'yesterday', 'none', duration='5m'))
        self.assertEqual(cm.exception.args[0], 'due')

    def test_settings_not_mutated(self):
        s = settings()
        snapshot = copy.deepcopy(s)
        self.ev(answers(4, 'today', 'serious', duration='30m'), s)
        self.assertEqual(s, snapshot)
        self.assertEqual(engine.DEFAULT_SETTINGS, snapshot)


# ---------------------------------------------------------------------------
# §5.6 rescore
# ---------------------------------------------------------------------------
class TestRescore(unittest.TestCase):
    def test_returns_score_and_group(self):
        t = task(1, 26.0, 'queue', importance=3, due='none', duration='30m')
        r = engine.rescore(t, TODAY, settings())
        self.assertIn('score', r)
        self.assertIn('group', r)
        self.assertEqual(r['score'], 26.0)
        self.assertEqual(r['group'], 'queue')

    def test_urgency_grows_as_due_date_approaches(self):
        # создана 25 дней назад, due=month → due_date через 5 дней: срочность 3 → 6
        t = task(1, 44.8, 'queue', importance=4, due='month', consequence='none',
                 duration='2h', created=TODAY - days(25))
        self.assertEqual(t['due_date'], '2026-10-30')
        r = engine.rescore(t, TODAY, settings())
        self.assertEqual(r['score'], 53.8)  # 33.75 + 18 + 0 + 2
        self.assertEqual(r['group'], 'priority')

    def test_overdue_stays_at_max_urgency(self):
        t = task(1, 56.0, 'priority', importance=3, due='today', duration='30m',
                 created=TODAY - days(5))
        r = engine.rescore(t, TODAY, settings())
        self.assertEqual(r['score'], 56.0)  # 22.5 + 30 + 0 + 3.5

    def test_group_manual_ignored_by_rescore(self):
        # group_manual учитывается вызывающим кодом — rescore возвращает группу по формуле
        t = task(1, 100.0, 'someday', importance=5, due='today', consequence='serious',
                 duration='5m', group_manual=1)
        r = engine.rescore(t, TODAY, settings())
        self.assertEqual(r['score'], 100.0)
        self.assertEqual(r['group'], 'now')

    def test_delegate_and_waiting_routing(self):
        d = task(1, 54.0, 'delegate', importance=3, due='week', consequence='unpleasant',
                 who='delegate', delegate_to='Света', duration='30m')
        w = task(2, 86.5, 'waiting', importance=5, due='week', consequence='serious',
                 duration='30m', depends_on='справка из банка')
        self.assertEqual(engine.rescore(d, TODAY, settings())['group'], 'delegate')
        self.assertEqual(engine.rescore(w, TODAY, settings())['group'], 'waiting')

    def test_uses_settings_thresholds(self):
        s = settings()
        s['thresholds'] = {'now': 50, 'priority': 40, 'queue': 10}
        t = task(1, 53.8, 'priority', importance=4, due='month', duration='2h',
                 created=TODAY - days(25))
        self.assertEqual(engine.rescore(t, TODAY, s)['group'], 'now')


# ---------------------------------------------------------------------------
# §5.6 suggest_today / can_add_today
# ---------------------------------------------------------------------------
def cand(task_id, score, due_date=None, created_at='2026-10-25T09:00:00'):
    return {'id': task_id, 'title': 'Кандидат %d' % task_id, 'score': score,
            'due_date': due_date, 'created_at': created_at, 'group': 'now',
            'status': 'open', 'is_today': 0}


class TestSuggestToday(unittest.TestCase):
    def ids(self, lst):
        return [t['id'] for t in lst]

    def test_sorted_by_score_desc(self):
        c = [cand(1, 50.0), cand(2, 90.0), cand(3, 70.0)]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 3)), [2, 3, 1])

    def test_tie_score_due_date_asc_none_last(self):
        c = [cand(1, 80.0, '2026-10-27'), cand(2, 80.0, None), cand(3, 80.0, '2026-10-25')]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 3)), [3, 1, 2])

    def test_tie_score_and_due_date_created_asc(self):
        c = [cand(1, 80.0, '2026-10-25', '2026-10-25T10:00:00'),
             cand(2, 80.0, '2026-10-25', '2026-10-25T08:00:00'),
             cand(3, 80.0, '2026-10-25', '2026-10-25T09:00:00')]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 3)), [2, 3, 1])

    def test_tie_all_none_due_created_asc(self):
        c = [cand(1, 80.0, None, '2026-10-24T10:00:00'),
             cand(2, 80.0, None, '2026-10-23T10:00:00')]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 3)), [2, 1])

    def test_returns_limit_minus_today_count(self):
        c = [cand(i, 100.0 - i) for i in range(1, 6)]
        today = [cand(99, 95.0)]
        r = engine.suggest_today(c, today, 3)
        self.assertEqual(len(r), 2)
        self.assertEqual(self.ids(r), [1, 2])

    def test_full_day_returns_empty(self):
        c = [cand(1, 90.0)]
        today = [cand(10, 80.0), cand(11, 80.0), cand(12, 80.0)]
        self.assertEqual(engine.suggest_today(c, today, 3), [])

    def test_over_full_day_returns_empty(self):
        c = [cand(1, 90.0)]
        today = [cand(10, 80.0), cand(11, 80.0), cand(12, 80.0), cand(13, 80.0)]
        self.assertEqual(engine.suggest_today(c, today, 3), [])

    def test_limits_1_to_10(self):
        c = [cand(i, 100.0 - i) for i in range(1, 13)]
        for limit in range(1, 11):
            with self.subTest(limit=limit):
                r = engine.suggest_today(c, [], limit)
                self.assertEqual(len(r), limit)
                self.assertEqual(self.ids(r), list(range(1, limit + 1)))

    def test_limits_with_partial_day(self):
        c = [cand(i, 100.0 - i) for i in range(1, 13)]
        for limit in range(2, 11):
            with self.subTest(limit=limit):
                today = [cand(100, 99.0)]
                self.assertEqual(len(engine.suggest_today(c, today, limit)), limit - 1)

    def test_fewer_candidates_than_slots(self):
        c = [cand(1, 90.0)]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 3)), [1])

    def test_empty_candidates(self):
        self.assertEqual(engine.suggest_today([], [], 3), [])

    def test_returns_task_dicts(self):
        c = [cand(1, 90.0), cand(2, 80.0)]
        r = engine.suggest_today(c, [], 3)
        for t in r:
            self.assertIsInstance(t, dict)
            self.assertIn('id', t)
            self.assertIn('score', t)

    def test_complex_ordering(self):
        c = [cand(1, 70.0, None, '2026-10-20T09:00:00'),
             cand(2, 90.0, '2026-10-30', '2026-10-21T09:00:00'),
             cand(3, 90.0, '2026-10-26', '2026-10-25T09:00:00'),
             cand(4, 90.0, '2026-10-26', '2026-10-22T09:00:00'),
             cand(5, 90.0, None, '2026-10-19T09:00:00'),
             cand(6, 85.0, '2026-10-25', '2026-10-25T09:00:00')]
        self.assertEqual(self.ids(engine.suggest_today(c, [], 10)), [4, 3, 2, 5, 6, 1])


class TestCanAddToday(unittest.TestCase):
    def test_empty_day(self):
        self.assertTrue(engine.can_add_today([], 3))

    def test_partial_day(self):
        self.assertTrue(engine.can_add_today([cand(1, 90.0)], 3))
        self.assertTrue(engine.can_add_today([cand(1, 90.0), cand(2, 80.0)], 3))

    def test_full_day(self):
        self.assertFalse(engine.can_add_today([cand(1, 90.0), cand(2, 80.0), cand(3, 70.0)], 3))

    def test_over_full_day(self):
        self.assertFalse(engine.can_add_today([cand(i, 50.0) for i in range(4)], 3))

    def test_limit_1(self):
        self.assertTrue(engine.can_add_today([], 1))
        self.assertFalse(engine.can_add_today([cand(1, 90.0)], 1))

    def test_limit_10(self):
        self.assertTrue(engine.can_add_today([cand(i, 50.0) for i in range(9)], 10))
        self.assertFalse(engine.can_add_today([cand(i, 50.0) for i in range(10)], 10))

    def test_returns_bool(self):
        self.assertIs(engine.can_add_today([], 3), True)
        self.assertIs(engine.can_add_today([cand(i, 50.0) for i in range(3)], 3), False)


# ---------------------------------------------------------------------------
# §5.6 plan_rollover
# ---------------------------------------------------------------------------
class TestPlanRollover(unittest.TestCase):
    def plan(self, tasks, s=None):
        return engine.plan_rollover(tasks, TODAY, s or settings())

    # --- структура ---
    def test_result_structure(self):
        p = self.plan([task(1, 26.0, 'queue')])
        self.assertEqual(set(p.keys()) & {'carry', 'review', 'regroup', 'rescore'},
                         {'carry', 'review', 'regroup', 'rescore'})
        self.assertIsInstance(p['carry'], list)
        self.assertIsInstance(p['review'], dict)
        self.assertIsInstance(p['regroup'], dict)
        self.assertIsInstance(p['rescore'], dict)

    def test_empty_input(self):
        p = self.plan([])
        self.assertEqual(p['carry'], [])
        self.assertEqual(p['review'], {})
        self.assertEqual(p['regroup'], {})
        self.assertEqual(p['rescore'], {})

    # --- carry ---
    def test_carry_when_today_date_before_today(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24')
        p = self.plan([t])
        self.assertIn(1, p['carry'])

    def test_carry_when_today_date_long_ago(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(10), is_today=1, today_date='2026-10-15')
        self.assertIn(1, self.plan([t])['carry'])

    def test_no_carry_when_today_date_is_today(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 is_today=1, today_date='2026-10-25')
        self.assertNotIn(1, self.plan([t])['carry'])

    def test_no_carry_when_not_in_today(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 is_today=0, today_date=None)
        self.assertNotIn(1, self.plan([t])['carry'])

    def test_carry_only_affected_tasks(self):
        a = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24')
        b = task(2, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 is_today=1, today_date='2026-10-25')
        c = task(3, 26.0, 'queue')
        self.assertEqual(sorted(self.plan([a, b, c])['carry']), [1])

    # --- review: carried ---
    def test_review_carried_when_limit_reached(self):
        # carry_count=2, carry_limit=3 → после переноса 3 ≥ 3 → review 'carried'
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=2)
        p = self.plan([t])
        self.assertIn(1, p['carry'])
        self.assertEqual(p['review'].get(1), 'carried')

    def test_no_review_when_below_limit(self):
        for cc in (0, 1):
            with self.subTest(carry_count=cc):
                t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                         created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=cc)
                p = self.plan([t])
                self.assertIn(1, p['carry'])
                self.assertNotIn(1, p['review'])

    def test_review_carried_with_limit_1(self):
        s = settings()
        s['carry_limit'] = 1
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=0)
        self.assertEqual(self.plan([t], s)['review'].get(1), 'carried')

    def test_review_carried_with_custom_limit(self):
        s = settings()
        s['carry_limit'] = 5
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=3)
        self.assertNotIn(1, self.plan([t], s)['review'])
        t['carry_count'] = 4
        self.assertEqual(self.plan([t], s)['review'].get(1), 'carried')

    # --- review: stale ---
    def test_review_stale_when_evaluated_long_ago(self):
        t = task(1, 26.0, 'queue', created=TODAY - days(15), evaluated=TODAY - days(15))
        self.assertEqual(self.plan([t])['review'].get(1), 'stale')

    def test_review_stale_8_days(self):
        t = task(1, 26.0, 'queue', created=TODAY - days(8), evaluated=TODAY - days(8))
        self.assertEqual(self.plan([t])['review'].get(1), 'stale')

    def test_not_stale_when_recent(self):
        for n in (0, 1, 3, 6):
            with self.subTest(days_ago=n):
                t = task(1, 26.0, 'queue', created=TODAY - days(n), evaluated=TODAY - days(n))
                self.assertNotIn(1, self.plan([t])['review'])

    def test_stale_uses_settings(self):
        s = settings()
        s['stale_days'] = 30
        t = task(1, 26.0, 'queue', created=TODAY - days(15), evaluated=TODAY - days(15))
        self.assertNotIn(1, self.plan([t], s)['review'])
        t2 = task(2, 26.0, 'queue', created=TODAY - days(40), evaluated=TODAY - days(40))
        self.assertEqual(self.plan([t2], s)['review'].get(2), 'stale')

    def test_stale_uses_evaluated_at_not_created_at(self):
        # создана давно, но мастер пройден недавно → не stale
        t = task(1, 26.0, 'queue', created=TODAY - days(60), evaluated=TODAY - days(2))
        self.assertNotIn(1, self.plan([t])['review'])

    def test_carried_and_stale_both_apply(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(15), evaluated=TODAY - days(15),
                 is_today=1, today_date='2026-10-24', carry_count=2)
        p = self.plan([t])
        self.assertIn(1, p['carry'])
        self.assertIn(p['review'].get(1), ('carried', 'stale'))

    def test_already_needs_review_not_duplicated(self):
        t = task(1, 26.0, 'queue', created=TODAY - days(15), evaluated=TODAY - days(15),
                 needs_review=1, review_reason='stale')
        p = self.plan([t])
        # допустимо: не включать в review (флаг уже стоит) или повторить ту же причину
        self.assertIn(p['review'].get(1, 'stale'), ('stale', 'carried'))
        self.assertIn(1, p['rescore'])

    def test_review_values_are_valid_reasons(self):
        tasks = [
            task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=2),
            task(2, 26.0, 'queue', created=TODAY - days(20), evaluated=TODAY - days(20)),
        ]
        for reason in self.plan(tasks)['review'].values():
            self.assertIn(reason, ('carried', 'stale'))

    # --- regroup ---
    def test_regroup_queue_to_priority(self):
        # создана 25 дней назад, due=month → due_date через 5 дней; imp 4, none, 2h:
        # при создании 33.75 + 9 + 0 + 2 = 44.75 → 44.8 → queue; сейчас 33.75 + 18 + 2 = 53.75 → 53.8 → priority
        t = task(1, 44.8, 'queue', importance=4, due='month', consequence='none',
                 duration='2h', created=TODAY - days(25), evaluated=TODAY - days(2))
        self.assertEqual(t['due_date'], '2026-10-30')
        p = self.plan([t])
        self.assertIn(1, p['regroup'])
        self.assertEqual(tuple(p['regroup'][1]), ('queue', 'priority'))
        self.assertEqual(p['rescore'][1], 53.8)

    def test_regroup_priority_to_now(self):
        # imp 3, week, serious, 30m: при создании (7 дн) 22.5 + 18 + 20 + 3.5 = 64.0 → priority;
        # через 6 дней (1 дн) 22.5 + 24 + 20 + 3.5 = 70.0 → now
        t = task(1, 64.0, 'priority', importance=3, due='week', consequence='serious',
                 duration='30m', created=TODAY - days(6), evaluated=TODAY - days(6))
        self.assertEqual(t['due_date'], '2026-10-26')
        p = self.plan([t])
        self.assertEqual(tuple(p['regroup'][1]), ('priority', 'now'))
        self.assertEqual(p['rescore'][1], 70.0)

    def test_regroup_queue_to_priority_month_to_tomorrow(self):
        # imp 3, month, none, 30m: при создании 22.5 + 9 + 3.5 = 35.0 → queue; завтра срок → 22.5 + 24 + 3.5 = 50.0 → priority
        t = task(1, 35.0, 'queue', importance=3, due='month', consequence='none',
                 duration='30m', created=TODAY - days(29), evaluated=TODAY - days(1))
        p = self.plan([t])
        self.assertEqual(tuple(p['regroup'][1]), ('queue', 'priority'))
        self.assertEqual(p['rescore'][1], 50.0)

    def test_regroup_groups_are_known(self):
        t = task(1, 44.8, 'queue', importance=4, due='month', duration='2h', created=TODAY - days(25))
        for old, new in self.plan([t])['regroup'].values():
            self.assertIn(old, engine.GROUPS)
            self.assertIn(new, engine.GROUPS)
            self.assertNotEqual(old, new)

    def test_no_regroup_for_manual_group(self):
        t = task(1, 44.8, 'queue', importance=4, due='month', duration='2h',
                 created=TODAY - days(25), group_manual=1)
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertEqual(p['rescore'][1], 53.8)  # балл всё равно пересчитан

    def test_no_regroup_for_delegate_group(self):
        t = task(1, 54.0, 'delegate', importance=3, due='week', consequence='unpleasant',
                 who='delegate', delegate_to='Света', duration='30m', created=TODAY - days(6))
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertIn(1, p['rescore'])

    def test_no_regroup_for_waiting_group(self):
        t = task(1, 86.5, 'waiting', importance=5, due='week', consequence='serious',
                 duration='30m', depends_on='справка из банка', created=TODAY - days(6))
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertIn(1, p['rescore'])

    def test_no_regroup_for_delegate_group_even_if_formula_differs(self):
        # §9 п.3: авто-пересчёт не трогает группы delegate/waiting
        t = task(1, 100.0, 'delegate', importance=5, due='today', consequence='serious',
                 who='me', duration='5m', group_manual=0)
        self.assertNotIn(1, self.plan([t])['regroup'])

    def test_no_regroup_for_waiting_group_even_if_formula_differs(self):
        t = task(1, 100.0, 'waiting', importance=5, due='today', consequence='serious',
                 who='me', duration='5m', depends_on='', group_manual=0)
        self.assertNotIn(1, self.plan([t])['regroup'])

    def test_no_regroup_when_group_unchanged(self):
        t = task(1, 26.0, 'queue', importance=3, due='none', duration='30m', created=TODAY - days(25))
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertEqual(p['rescore'][1], 26.0)

    def test_no_regroup_when_score_changes_but_group_same(self):
        # imp 2, week, none, 30m: 32.75 → 32.8 queue; через 6 дней 11.25 + 24 + 3.5 = 38.75 → 38.8 queue
        t = task(1, 32.8, 'queue', importance=2, due='week', consequence='none',
                 duration='30m', created=TODAY - days(6))
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertEqual(p['rescore'][1], 38.8)

    def test_regroup_uses_settings_thresholds(self):
        s = settings()
        s['thresholds'] = {'now': 65, 'priority': 55, 'queue': 25}
        t = task(1, 44.8, 'queue', importance=4, due='month', duration='2h', created=TODAY - days(25))
        p = self.plan([t], s)
        self.assertNotIn(1, p['regroup'])  # 53.8 < 55 → остаётся queue
        self.assertEqual(p['rescore'][1], 53.8)

    def test_overdue_task_not_regrouped_by_urgency(self):
        # просроченная: срочность уже 10 и остаётся 10
        t = task(1, 56.0, 'priority', importance=3, due='today', duration='30m', created=TODAY - days(3))
        p = self.plan([t])
        self.assertNotIn(1, p['regroup'])
        self.assertEqual(p['rescore'][1], 56.0)

    # --- rescore ---
    def test_rescore_contains_all_open_tasks(self):
        tasks = [
            task(1, 26.0, 'queue'),
            task(2, 44.8, 'queue', importance=4, due='month', duration='2h', created=TODAY - days(25)),
            task(3, 54.0, 'delegate', importance=3, due='week', consequence='unpleasant',
                 who='delegate', duration='30m'),
            task(4, 86.5, 'waiting', importance=5, due='week', consequence='serious',
                 duration='30m', depends_on='справка'),
            task(5, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', group_manual=1),
        ]
        p = self.plan(tasks)
        self.assertEqual(set(p['rescore'].keys()), {1, 2, 3, 4, 5})

    def test_rescore_values(self):
        tasks = [
            task(1, 26.0, 'queue', importance=3, due='none', duration='30m'),
            task(2, 44.8, 'queue', importance=4, due='month', duration='2h', created=TODAY - days(25)),
            task(3, 98.5, 'now', importance=5, due='today', consequence='serious', duration='30m'),
        ]
        p = self.plan(tasks)
        self.assertEqual(p['rescore'][1], 26.0)
        self.assertEqual(p['rescore'][2], 53.8)
        self.assertEqual(p['rescore'][3], 98.5)

    def test_rescore_values_are_rounded_floats(self):
        tasks = [task(i, 26.0, 'queue', importance=i, due='week', duration='2h',
                      created=TODAY - days(i)) for i in range(1, 6)]
        for sc in self.plan(tasks)['rescore'].values():
            self.assertIsInstance(sc, float)
            self.assertEqual(round(sc, 1), sc)
            self.assertGreaterEqual(sc, 0.0)
            self.assertLessEqual(sc, 100.0)

    def test_carried_task_is_also_rescored(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24')
        p = self.plan([t])
        self.assertIn(1, p['carry'])
        self.assertIn(1, p['rescore'])

    # --- комплексный сценарий ---
    def test_full_scenario(self):
        tasks = [
            # перенос + review carried
            task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=2),
            # stale
            task(2, 26.0, 'queue', created=TODAY - days(20), evaluated=TODAY - days(20)),
            # regroup
            task(3, 44.8, 'queue', importance=4, due='month', duration='2h',
                 created=TODAY - days(25), evaluated=TODAY - days(1)),
            # ничего
            task(4, 26.0, 'queue'),
            # сегодня — не переносить
            task(5, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 is_today=1, today_date='2026-10-25'),
        ]
        p = self.plan(tasks)
        self.assertEqual(sorted(p['carry']), [1])
        self.assertEqual(p['review'].get(1), 'carried')
        self.assertEqual(p['review'].get(2), 'stale')
        self.assertNotIn(3, p['review'])
        self.assertNotIn(4, p['review'])
        self.assertNotIn(5, p['review'])
        self.assertEqual(set(p['regroup'].keys()), {3})
        self.assertEqual(tuple(p['regroup'][3]), ('queue', 'priority'))
        self.assertEqual(set(p['rescore'].keys()), {1, 2, 3, 4, 5})

    def test_input_tasks_not_mutated(self):
        t = task(1, 70.0, 'now', importance=4, due='today', consequence='unpleasant',
                 created=TODAY - days(1), is_today=1, today_date='2026-10-24', carry_count=2)
        snapshot = copy.deepcopy(t)
        self.plan([t])
        self.assertEqual(t, snapshot)


if __name__ == '__main__':
    unittest.main()
