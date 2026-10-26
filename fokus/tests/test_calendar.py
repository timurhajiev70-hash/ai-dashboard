# -*- coding: utf-8 -*-
"""Тесты модуля calendar_ics (AGENTS.md §12): разбор .ics Google Календаря и развёртка повторов.

Написаны ПО СПЕЦИФИКАЦИИ, без чтения кода. Строки .ics собираются прямо в тесте
в стиле экспорта Google (CRLF, X-WR-TIMEZONE, VTIMEZONE, DTSTART;TZID=Europe/Berlin,
VALUE=DATE, время в Z). Окно развёртки — [2026-10-20, 2026-12-01), tz Europe/Berlin;
переход на зимнее время в 2026 году — 25 октября (07:00Z = 09:00 CEST до него, 08:00 CET после).
Сети нет: fetch подменяется словарём url → текст.
Если calendar_ics.py ещё нет — все классы пропускаются (SkipTest).
Запуск: cd fokus && python3 -m unittest tests.test_calendar -v
"""
import datetime
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import calendar_ics  # noqa: E402
except ImportError:
    calendar_ics = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 — модуль всё равно требует zoneinfo
    ZoneInfo = None

TZ = 'Europe/Berlin'
WIN_START = datetime.date(2026, 10, 20)
WIN_END = datetime.date(2026, 12, 1)
TODAY = datetime.date(2026, 10, 27)
EVENT_KEYS = {'uid', 'summary', 'location', 'start', 'end', 'all_day', 'calendar'}
VEVENT_KEYS = {'uid', 'summary', 'location', 'description', 'dtstart', 'dtend', 'all_day',
               'rrule', 'exdates', 'recurrence_id', 'duration', 'status'}

# Блок VTIMEZONE — как в реальном экспорте Google (внутри есть свои DTSTART/RRULE,
# которые разборщик не должен принимать за события).
VTIMEZONE_BERLIN = [
    'BEGIN:VTIMEZONE',
    'TZID:Europe/Berlin',
    'X-LIC-LOCATION:Europe/Berlin',
    'BEGIN:DAYLIGHT',
    'TZOFFSETFROM:+0100',
    'TZOFFSETTO:+0200',
    'TZNAME:CEST',
    'DTSTART:19700329T020000',
    'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU',
    'END:DAYLIGHT',
    'BEGIN:STANDARD',
    'TZOFFSETFROM:+0200',
    'TZOFFSETTO:+0100',
    'TZNAME:CET',
    'DTSTART:19701025T030000',
    'RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU',
    'END:STANDARD',
    'END:VTIMEZONE',
]


def vevent(uid, summary, dtstart, dtend=None, extra=None, status='CONFIRMED', location=''):
    """Строки одного VEVENT в стиле Google. dtstart/dtend — готовые строки свойств."""
    lines = ['BEGIN:VEVENT', dtstart]
    if dtend:
        lines.append(dtend)
    lines += [
        'DTSTAMP:20261020T120000Z',
        'UID:%s' % uid,
        'CREATED:20261001T100000Z',
        'DESCRIPTION:',
        'LAST-MODIFIED:20261001T100000Z',
        'LOCATION:%s' % location,
        'SEQUENCE:0',
    ]
    if status:
        lines.append('STATUS:%s' % status)
    lines += ['SUMMARY:%s' % summary, 'TRANSP:OPAQUE']
    if extra:
        lines += list(extra)
    lines.append('END:VEVENT')
    return lines


def ics(*events, **kw):
    """Собрать текст календаря. events — списки строк от vevent(). Разделитель CRLF, как у Google."""
    tzid = kw.get('tzid', TZ)
    sep = kw.get('sep', '\r\n')
    lines = [
        'BEGIN:VCALENDAR',
        'PRODID:-//Google Inc//Google Calendar 70.9054//EN',
        'VERSION:2.0',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:%s' % kw.get('name', 'Praxis'),
    ]
    if tzid:
        lines.append('X-WR-TIMEZONE:%s' % tzid)
    if kw.get('vtimezone', True):
        lines += VTIMEZONE_BERLIN
    for ev in events:
        lines += list(ev)
    lines.append('END:VCALENDAR')
    text = sep.join(lines) + sep
    if kw.get('bom'):
        text = '﻿' + text
    return text


def berlin(y, m, d, hh=0, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ))


def starts(events):
    return [e['start'] for e in events]


class CalendarCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if calendar_ics is None:
            raise unittest.SkipTest('calendar_ics.py ещё нет')
        if ZoneInfo is None:
            raise unittest.SkipTest('нет zoneinfo')

    def expand(self, text, start=WIN_START, end=WIN_END, tz=TZ):
        return calendar_ics.expand(calendar_ics.parse_ics(text), start, end, tz)

    def one(self, text):
        events = calendar_ics.parse_ics(text)
        self.assertEqual(len(events), 1, events)
        return events[0]


# ---------------------------------------------------------------------------
# unfold (RFC 5545 §3.1)
# ---------------------------------------------------------------------------
class TestUnfold(CalendarCase):
    def test_returns_list_of_lines(self):
        lines = calendar_ics.unfold('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n')
        self.assertIsInstance(lines, list)
        self.assertEqual([l for l in lines if l], ['BEGIN:VCALENDAR', 'VERSION:2.0', 'END:VCALENDAR'])

    def test_space_continuation_crlf(self):
        # перенос «CRLF + один пробел» убирается; второй пробел — часть текста
        text = 'SUMMARY:Besprechung mit dem\r\n  Team\r\nUID:1\r\n'
        lines = calendar_ics.unfold(text)
        self.assertIn('SUMMARY:Besprechung mit dem Team', lines)
        self.assertIn('UID:1', lines)

    def test_tab_continuation(self):
        text = 'DESCRIPTION:Erste Zeile der Besch\r\n\treibung\r\n'
        lines = calendar_ics.unfold(text)
        self.assertIn('DESCRIPTION:Erste Zeile der Beschreibung', lines)

    def test_multiple_continuations_of_one_line(self):
        text = 'DESCRIPTION:aaa\r\n bbb\r\n ccc\r\n ddd\r\nUID:2\r\n'
        lines = calendar_ics.unfold(text)
        self.assertIn('DESCRIPTION:aaabbbcccddd', lines)
        self.assertIn('UID:2', lines)

    def test_lf_only(self):
        text = 'SUMMARY:Zahn\n arzt\nUID:3\n'
        lines = calendar_ics.unfold(text)
        self.assertIn('SUMMARY:Zahnarzt', lines)
        self.assertIn('UID:3', lines)

    def test_no_cr_left_in_lines(self):
        lines = calendar_ics.unfold('A:1\r\nB:2\r\n')
        for l in lines:
            self.assertNotIn('\r', l)
            self.assertNotIn('\n', l)

    def test_bom_stripped(self):
        lines = calendar_ics.unfold('﻿BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n')
        self.assertEqual(lines[0], 'BEGIN:VCALENDAR')

    def test_empty_text(self):
        self.assertEqual([l for l in calendar_ics.unfold('') if l], [])

    def test_unicode_preserved(self):
        lines = calendar_ics.unfold('SUMMARY:Прием у стоматолога — Überprüfung\r\n')
        self.assertIn('SUMMARY:Прием у стоматолога — Überprüfung', lines)


# ---------------------------------------------------------------------------
# parse_ics
# ---------------------------------------------------------------------------
class TestParseIcs(CalendarCase):
    def test_single_event_fields(self):
        text = ics(vevent('abc123@google.com', 'Teambesprechung',
                          'DTSTART;TZID=Europe/Berlin:20261027T090000',
                          'DTEND;TZID=Europe/Berlin:20261027T093000',
                          location='Praxis\\, Raum 2'))
        ev = self.one(text)
        self.assertTrue(VEVENT_KEYS <= set(ev.keys()), VEVENT_KEYS - set(ev.keys()))
        self.assertEqual(ev['uid'], 'abc123@google.com')
        self.assertEqual(ev['summary'], 'Teambesprechung')
        self.assertEqual(ev['location'], 'Praxis, Raum 2')
        self.assertIsInstance(ev['dtstart'], datetime.datetime)
        self.assertIsNotNone(ev['dtstart'].tzinfo)
        self.assertEqual(ev['dtstart'], berlin(2026, 10, 27, 9, 0))
        self.assertEqual(ev['dtend'], berlin(2026, 10, 27, 9, 30))
        self.assertIs(ev['all_day'], False)
        self.assertFalse(ev['rrule'])
        self.assertIsInstance(ev['exdates'], list)
        self.assertEqual(ev['exdates'], [])
        self.assertIsNone(ev['recurrence_id'])
        self.assertEqual(str(ev['status']).upper(), 'CONFIRMED')

    def test_summary_escaping(self):
        text = ics(vevent('e1', 'Besprechung\\, Teil 1\\nZeile 2\\; Ende',
                          'DTSTART;TZID=Europe/Berlin:20261027T090000',
                          'DTEND;TZID=Europe/Berlin:20261027T100000'))
        ev = self.one(text)
        self.assertEqual(ev['summary'], 'Besprechung, Teil 1\nZeile 2; Ende')

    def test_description_folded_and_escaped(self):
        lines = vevent('e2', 'QM-Sitzung',
                       'DTSTART;TZID=Europe/Berlin:20261027T090000',
                       'DTEND;TZID=Europe/Berlin:20261027T100000')
        # заменить пустое DESCRIPTION: на длинное, сложенное в стиле Google (перенос + пробел)
        idx = lines.index('DESCRIPTION:')
        lines[idx:idx + 1] = ['DESCRIPTION:Tagesordnung\\nPunkt 1\\, Punkt 2 und eine sehr lange Zeile die',
                              '  umgebrochen wird']
        ev = self.one(ics(lines))
        self.assertEqual(ev['description'],
                         'Tagesordnung\nPunkt 1, Punkt 2 und eine sehr lange Zeile die umgebrochen wird')

    def test_utc_z_is_aware_utc(self):
        ev = self.one(ics(vevent('e3', 'Telefonat', 'DTSTART:20261027T070000Z', 'DTEND:20261027T073000Z')))
        self.assertEqual(ev['dtstart'], datetime.datetime(2026, 10, 27, 7, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(ev['dtstart'].utcoffset(), datetime.timedelta(0))
        self.assertEqual(ev['dtend'], datetime.datetime(2026, 10, 27, 7, 30, tzinfo=datetime.timezone.utc))

    def test_floating_uses_x_wr_timezone(self):
        ev = self.one(ics(vevent('e4', 'Floating', 'DTSTART:20261027T090000', 'DTEND:20261027T100000')))
        self.assertIsNotNone(ev['dtstart'].tzinfo)
        self.assertEqual(ev['dtstart'], berlin(2026, 10, 27, 9, 0))

    def test_all_day_value_date(self):
        ev = self.one(ics(vevent('e5', 'Fortbildung', 'DTSTART;VALUE=DATE:20261027', 'DTEND;VALUE=DATE:20261029')))
        self.assertIs(ev['all_day'], True)
        self.assertEqual(ev['dtstart'], datetime.date(2026, 10, 27))
        self.assertEqual(ev['dtend'], datetime.date(2026, 10, 29))
        self.assertNotIsInstance(ev['dtstart'], datetime.datetime)

    def test_all_day_without_dtend_is_one_day(self):
        ev = self.one(ics(vevent('e6', 'Feiertag', 'DTSTART;VALUE=DATE:20261027')))
        self.assertIs(ev['all_day'], True)
        self.assertEqual(ev['dtend'], datetime.date(2026, 10, 28))

    def test_datetime_without_dtend_equals_dtstart(self):
        ev = self.one(ics(vevent('e7', 'Erinnerung', 'DTSTART;TZID=Europe/Berlin:20261027T090000')))
        self.assertEqual(ev['dtend'], ev['dtstart'])

    def test_duration_property(self):
        ev = self.one(ics(vevent('e8', 'Mit Dauer', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                                 extra=['DURATION:PT1H30M'])))
        self.assertEqual(ev['dtend'], berlin(2026, 10, 27, 10, 30))
        self.assertTrue(ev['duration'])

    def test_recurring_fields(self):
        text = ics(
            vevent('r1', 'Wöchentlich', 'DTSTART;TZID=Europe/Berlin:20261026T100000',
                   'DTEND;TZID=Europe/Berlin:20261026T110000',
                   extra=['RRULE:FREQ=WEEKLY;BYDAY=MO', 'EXDATE;TZID=Europe/Berlin:20261102T100000']),
            vevent('r1', 'Wöchentlich (verschoben)', 'DTSTART;TZID=Europe/Berlin:20261110T150000',
                   'DTEND;TZID=Europe/Berlin:20261110T160000',
                   extra=['RECURRENCE-ID;TZID=Europe/Berlin:20261109T100000']),
        )
        events = calendar_ics.parse_ics(text)
        self.assertEqual(len(events), 2)
        master = [e for e in events if not e['recurrence_id']][0]
        override = [e for e in events if e['recurrence_id']][0]
        self.assertTrue(master['rrule'])
        self.assertEqual(len(master['exdates']), 1)
        self.assertEqual(master['exdates'][0], berlin(2026, 11, 2, 10, 0))
        self.assertEqual(override['recurrence_id'], berlin(2026, 11, 9, 10, 0))
        self.assertEqual(override['uid'], 'r1')

    def test_multiple_events_and_valarm_ignored(self):
        text = ics(
            vevent('m1', 'Erstes', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                   'DTEND;TZID=Europe/Berlin:20261027T100000',
                   extra=['BEGIN:VALARM', 'ACTION:DISPLAY', 'DESCRIPTION:This is an event reminder',
                          'TRIGGER:-P0DT0H10M0S', 'END:VALARM']),
            vevent('m2', 'Zweites', 'DTSTART;TZID=Europe/Berlin:20261028T090000',
                   'DTEND;TZID=Europe/Berlin:20261028T100000'),
        )
        events = calendar_ics.parse_ics(text)
        self.assertEqual(len(events), 2)
        self.assertEqual(sorted(e['uid'] for e in events), ['m1', 'm2'])
        first = [e for e in events if e['uid'] == 'm1'][0]
        self.assertEqual(first['summary'], 'Erstes')
        self.assertEqual(first['description'], '')

    def test_vtimezone_is_not_an_event(self):
        events = calendar_ics.parse_ics(ics())
        self.assertEqual(events, [])

    def test_bom_and_lf(self):
        text = ics(vevent('b1', 'Mit BOM', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                          'DTEND;TZID=Europe/Berlin:20261027T100000'), bom=True, sep='\n')
        ev = self.one(text)
        self.assertEqual(ev['summary'], 'Mit BOM')
        self.assertEqual(ev['dtstart'], berlin(2026, 10, 27, 9, 0))

    def test_empty_and_garbage(self):
        self.assertEqual(calendar_ics.parse_ics(''), [])
        self.assertEqual(calendar_ics.parse_ics('<html><body>Not Found</body></html>'), [])
        self.assertEqual(calendar_ics.parse_ics('BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n'), [])

    def test_other_tzid(self):
        ev = self.one(ics(vevent('ny', 'Call', 'DTSTART;TZID=America/New_York:20261027T090000',
                                 'DTEND;TZID=America/New_York:20261027T093000')))
        self.assertEqual(ev['dtstart'], datetime.datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo('America/New_York')))
        self.assertEqual(ev['dtstart'], berlin(2026, 10, 27, 14, 0))

    def test_returns_list(self):
        self.assertIsInstance(calendar_ics.parse_ics(ics()), list)


# ---------------------------------------------------------------------------
# expand — окно [2026-10-20, 2026-12-01), tz Europe/Berlin
# ---------------------------------------------------------------------------
class TestExpand(CalendarCase):
    def test_single_timed_event(self):
        events = self.expand(ics(vevent('s1', 'Teambesprechung',
                                        'DTSTART;TZID=Europe/Berlin:20261027T090000',
                                        'DTEND;TZID=Europe/Berlin:20261027T093000',
                                        location='Raum 2')))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertTrue(EVENT_KEYS <= set(ev.keys()), EVENT_KEYS - set(ev.keys()))
        self.assertEqual(ev['start'], '2026-10-27T09:00')
        self.assertEqual(ev['end'], '2026-10-27T09:30')
        self.assertIs(ev['all_day'], False)
        self.assertEqual(ev['uid'], 's1')
        self.assertEqual(ev['summary'], 'Teambesprechung')
        self.assertEqual(ev['location'], 'Raum 2')

    def test_utc_winter_time(self):
        # 25.10.2026 — переход на зимнее время: 07:00Z = 08:00 CET
        events = self.expand(ics(vevent('z1', 'Telefonat', 'DTSTART:20261027T070000Z', 'DTEND:20261027T073000Z')))
        self.assertEqual(starts(events), ['2026-10-27T08:00'])
        self.assertEqual(events[0]['end'], '2026-10-27T08:30')

    def test_utc_summer_time(self):
        events = self.expand(ics(vevent('z2', 'Telefonat', 'DTSTART:20261020T070000Z', 'DTEND:20261020T073000Z')))
        self.assertEqual(starts(events), ['2026-10-20T09:00'])
        self.assertEqual(events[0]['end'], '2026-10-20T09:30')

    def test_other_tzid_converted_to_settings_tz(self):
        events = self.expand(ics(vevent('ny', 'Call', 'DTSTART;TZID=America/New_York:20261027T090000',
                                        'DTEND;TZID=America/New_York:20261027T093000')))
        self.assertEqual(starts(events), ['2026-10-27T14:00'])

    def test_floating_in_calendar_timezone(self):
        events = self.expand(ics(vevent('f1', 'Floating', 'DTSTART:20261027T090000', 'DTEND:20261027T100000')))
        self.assertEqual(starts(events), ['2026-10-27T09:00'])

    def test_all_day_event(self):
        events = self.expand(ics(vevent('a1', 'Fortbildung', 'DTSTART;VALUE=DATE:20261027', 'DTEND;VALUE=DATE:20261028')))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], '2026-10-27')
        self.assertEqual(events[0]['end'], '2026-10-28')
        self.assertIs(events[0]['all_day'], True)

    def test_all_day_without_dtend(self):
        events = self.expand(ics(vevent('a2', 'Feiertag', 'DTSTART;VALUE=DATE:20261027')))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['start'], events[0]['end'], events[0]['all_day']),
                         ('2026-10-27', '2026-10-28', True))

    def test_timed_without_dtend(self):
        events = self.expand(ics(vevent('t1', 'Erinnerung', 'DTSTART;TZID=Europe/Berlin:20261027T090000')))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], '2026-10-27T09:00')
        self.assertEqual(events[0]['end'], '2026-10-27T09:00')

    def test_duration(self):
        events = self.expand(ics(vevent('d1', 'Mit Dauer', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                                        extra=['DURATION:PT1H30M'])))
        self.assertEqual((events[0]['start'], events[0]['end']), ('2026-10-27T09:00', '2026-10-27T10:30'))

    def test_weekly_byday_until(self):
        events = self.expand(ics(vevent('w1', 'Teambesprechung',
                                        'DTSTART;TZID=Europe/Berlin:20261026T100000',
                                        'DTEND;TZID=Europe/Berlin:20261026T103000',
                                        extra=['RRULE:FREQ=WEEKLY;WKST=MO;UNTIL=20261118T235959Z;BYDAY=MO,WE'])))
        self.assertEqual(starts(events), [
            '2026-10-26T10:00', '2026-10-28T10:00', '2026-11-02T10:00', '2026-11-04T10:00',
            '2026-11-09T10:00', '2026-11-11T10:00', '2026-11-16T10:00', '2026-11-18T10:00'])
        for ev in events:
            self.assertEqual(ev['end'], ev['start'][:10] + 'T10:30')
            self.assertEqual(ev['uid'], 'w1')
            self.assertEqual(ev['summary'], 'Teambesprechung')

    def test_monthly_second_tuesday(self):
        events = self.expand(ics(vevent('m1', 'QM-Sitzung',
                                        'DTSTART;TZID=Europe/Berlin:20261013T140000',
                                        'DTEND;TZID=Europe/Berlin:20261013T150000',
                                        extra=['RRULE:FREQ=MONTHLY;BYDAY=2TU'])))
        self.assertEqual(starts(events), ['2026-11-10T14:00'])

    def test_monthly_last_friday(self):
        events = self.expand(ics(vevent('m2', 'Abrechnung',
                                        'DTSTART;TZID=Europe/Berlin:20260925T160000',
                                        'DTEND;TZID=Europe/Berlin:20260925T170000',
                                        extra=['RRULE:FREQ=MONTHLY;BYDAY=-1FR'])))
        self.assertEqual(starts(events), ['2026-10-30T16:00', '2026-11-27T16:00'])

    def test_monthly_bymonthday(self):
        events = self.expand(ics(vevent('m3', 'Miete',
                                        'DTSTART;TZID=Europe/Berlin:20260915T120000',
                                        'DTEND;TZID=Europe/Berlin:20260915T121500',
                                        extra=['RRULE:FREQ=MONTHLY;BYMONTHDAY=15'])))
        self.assertEqual(starts(events), ['2026-11-15T12:00'])

    def test_daily_count_with_exdate(self):
        events = self.expand(ics(vevent('d2', 'Kurs',
                                        'DTSTART;TZID=Europe/Berlin:20261021T080000',
                                        'DTEND;TZID=Europe/Berlin:20261021T090000',
                                        extra=['RRULE:FREQ=DAILY;COUNT=5',
                                               'EXDATE;TZID=Europe/Berlin:20261023T080000'])))
        self.assertEqual(starts(events), ['2026-10-21T08:00', '2026-10-22T08:00',
                                          '2026-10-24T08:00', '2026-10-25T08:00'])

    def test_weekly_interval_2(self):
        events = self.expand(ics(vevent('w2', 'Supervision',
                                        'DTSTART;TZID=Europe/Berlin:20261022T180000',
                                        'DTEND;TZID=Europe/Berlin:20261022T190000',
                                        extra=['RRULE:FREQ=WEEKLY;INTERVAL=2'])))
        self.assertEqual(starts(events), ['2026-10-22T18:00', '2026-11-05T18:00', '2026-11-19T18:00'])

    def test_yearly_birthday(self):
        events = self.expand(ics(vevent('y1', 'Geburtstag Kollege', 'DTSTART;VALUE=DATE:19851107',
                                        'DTEND;VALUE=DATE:19851108', extra=['RRULE:FREQ=YEARLY'])))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['start'], events[0]['end'], events[0]['all_day']),
                         ('2026-11-07', '2026-11-08', True))
        self.assertEqual(events[0]['summary'], 'Geburtstag Kollege')

    def test_yearly_bymonth_bymonthday(self):
        events = self.expand(ics(vevent('y2', 'Jahresabschluss', 'DTSTART;VALUE=DATE:20201120',
                                        extra=['RRULE:FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=20'])))
        self.assertEqual(starts(events), ['2026-11-20'])

    def test_recurrence_id_moves_one_occurrence(self):
        text = ics(
            vevent('r1', 'Freitagsrunde', 'DTSTART;TZID=Europe/Berlin:20261023T110000',
                   'DTEND;TZID=Europe/Berlin:20261023T120000',
                   extra=['RRULE:FREQ=WEEKLY;UNTIL=20261113T235959Z;BYDAY=FR']),
            vevent('r1', 'Freitagsrunde (verschoben)', 'DTSTART;TZID=Europe/Berlin:20261031T150000',
                   'DTEND;TZID=Europe/Berlin:20261031T160000',
                   extra=['RECURRENCE-ID;TZID=Europe/Berlin:20261030T110000']),
        )
        events = self.expand(text)
        self.assertEqual(starts(events), ['2026-10-23T11:00', '2026-10-31T15:00',
                                          '2026-11-06T11:00', '2026-11-13T11:00'])
        self.assertNotIn('2026-10-30T11:00', starts(events))
        moved = [e for e in events if e['start'] == '2026-10-31T15:00'][0]
        self.assertEqual(moved['summary'], 'Freitagsrunde (verschoben)')
        self.assertEqual(moved['end'], '2026-10-31T16:00')
        self.assertTrue(all(e['uid'] == 'r1' for e in events))

    def test_cancelled_event_skipped(self):
        text = ics(
            vevent('c1', 'Abgesagt', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                   'DTEND;TZID=Europe/Berlin:20261027T100000', status='CANCELLED'),
            vevent('c2', 'Findet statt', 'DTSTART;TZID=Europe/Berlin:20261027T110000',
                   'DTEND;TZID=Europe/Berlin:20261027T120000'),
        )
        events = self.expand(text)
        self.assertEqual([e['summary'] for e in events], ['Findet statt'])

    def test_multiday_crossing_window_start(self):
        # 18–21 октября (DTEND исключительный = 22.10) пересекает начало окна 20.10 → показывается
        events = self.expand(ics(vevent('u1', 'Urlaub', 'DTSTART;VALUE=DATE:20261018', 'DTEND;VALUE=DATE:20261022')))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['start'], events[0]['end']), ('2026-10-18', '2026-10-22'))
        self.assertIs(events[0]['all_day'], True)

    def test_outside_window_excluded(self):
        text = ics(
            vevent('o1', 'Vor dem Fenster', 'DTSTART;TZID=Europe/Berlin:20261010T090000',
                   'DTEND;TZID=Europe/Berlin:20261010T100000'),
            vevent('o2', 'Nach dem Fenster', 'DTSTART;TZID=Europe/Berlin:20261215T090000',
                   'DTEND;TZID=Europe/Berlin:20261215T100000'),
            vevent('o3', 'Am Ende des Fensters', 'DTSTART;TZID=Europe/Berlin:20261201T000000',
                   'DTEND;TZID=Europe/Berlin:20261201T010000'),
            vevent('o4', 'Ganztag vor Fenster', 'DTSTART;VALUE=DATE:20261019', 'DTEND;VALUE=DATE:20261020'),
            vevent('o5', 'Drin', 'DTSTART;TZID=Europe/Berlin:20261020T000000',
                   'DTEND;TZID=Europe/Berlin:20261020T001500'),
        )
        events = self.expand(text)
        self.assertEqual([e['summary'] for e in events], ['Drin'])

    def test_sorted_by_start(self):
        text = ics(
            vevent('s3', 'Drittes', 'DTSTART;TZID=Europe/Berlin:20261105T090000',
                   'DTEND;TZID=Europe/Berlin:20261105T100000'),
            vevent('s1', 'Erstes', 'DTSTART;VALUE=DATE:20261027'),
            vevent('s2', 'Zweites', 'DTSTART:20261027T070000Z', 'DTEND:20261027T073000Z'),
            vevent('s0', 'Nulltes', 'DTSTART;TZID=Europe/Berlin:20261021T090000',
                   'DTEND;TZID=Europe/Berlin:20261021T100000'),
        )
        events = self.expand(text)
        self.assertEqual([e['uid'] for e in events], ['s0', 's1', 's2', 's3'])
        self.assertEqual(starts(events), sorted(starts(events)))

    def test_calendar_key_present(self):
        events = self.expand(ics(vevent('k1', 'X', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                                        'DTEND;TZID=Europe/Berlin:20261027T100000')))
        self.assertIn('calendar', events[0])

    def test_daily_unbounded_limited_to_window(self):
        # DAILY без UNTIL/COUNT: ровно столько вхождений, сколько дней в окне (42)
        events = self.expand(ics(vevent('d3', 'Täglich', 'DTSTART;TZID=Europe/Berlin:20260101T070000',
                                        'DTEND;TZID=Europe/Berlin:20260101T071500',
                                        extra=['RRULE:FREQ=DAILY'])))
        self.assertEqual(len(events), (WIN_END - WIN_START).days)
        self.assertEqual(events[0]['start'], '2026-10-20T07:00')
        self.assertEqual(events[-1]['start'], '2026-11-30T07:00')

    def test_limit_5000_does_not_hang(self):
        # 20 лет ежедневных вхождений (> 7000) → не больше 5000 и без зависания
        text = ics(vevent('d4', 'Täglich', 'DTSTART;VALUE=DATE:20100101', extra=['RRULE:FREQ=DAILY']))
        events = self.expand(text, start=datetime.date(2010, 1, 1), end=datetime.date(2030, 1, 1))
        self.assertGreater(len(events), 0)
        self.assertLessEqual(len(events), 5000)

    def test_unknown_rrule_parts_ignored(self):
        events = self.expand(ics(vevent('u2', 'Komisch', 'DTSTART;TZID=Europe/Berlin:20261021T090000',
                                        'DTEND;TZID=Europe/Berlin:20261021T100000',
                                        extra=['RRULE:FREQ=WEEKLY;X-FOO=BAR;COUNT=2;BYDAY=WE'])))
        self.assertEqual(starts(events), ['2026-10-21T09:00', '2026-10-28T09:00'])

    def test_malformed_rrule_does_not_crash(self):
        text = ics(
            vevent('b1', 'Kaputt', 'DTSTART;TZID=Europe/Berlin:20261021T090000',
                   'DTEND;TZID=Europe/Berlin:20261021T100000', extra=['RRULE:FREQ=HOURLY;COUNT=3']),
            vevent('b2', 'Ganz kaputt', 'DTSTART;TZID=Europe/Berlin:20261022T090000',
                   'DTEND;TZID=Europe/Berlin:20261022T100000', extra=['RRULE:NONSENSE']),
            vevent('b3', 'Heil', 'DTSTART;TZID=Europe/Berlin:20261023T090000',
                   'DTEND;TZID=Europe/Berlin:20261023T100000'),
        )
        events = self.expand(text)
        self.assertIsInstance(events, list)
        self.assertIn('2026-10-23T09:00', starts(events))

    def test_empty_input(self):
        self.assertEqual(calendar_ics.expand([], WIN_START, WIN_END, TZ), [])

    def test_empty_window(self):
        vevents = calendar_ics.parse_ics(ics(vevent('e', 'X', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                                                    'DTEND;TZID=Europe/Berlin:20261027T100000')))
        self.assertEqual(calendar_ics.expand(vevents, WIN_START, WIN_START, TZ), [])

    def test_start_end_format(self):
        text = ics(
            vevent('f1', 'Zeit', 'DTSTART;TZID=Europe/Berlin:20261027T090500',
                   'DTEND;TZID=Europe/Berlin:20261027T101500'),
            vevent('f2', 'Tag', 'DTSTART;VALUE=DATE:20261027'),
        )
        events = self.expand(text)
        timed = [e for e in events if not e['all_day']][0]
        allday = [e for e in events if e['all_day']][0]
        self.assertRegex(timed['start'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$')
        self.assertRegex(timed['end'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$')
        self.assertEqual(timed['start'], '2026-10-27T09:05')
        self.assertRegex(allday['start'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertRegex(allday['end'], r'^\d{4}-\d{2}-\d{2}$')


# ---------------------------------------------------------------------------
# fetch_ics — без сети
# ---------------------------------------------------------------------------
class TestFetch(CalendarCase):
    def test_non_https_rejected_without_network(self):
        for url in ('http://calendar.google.com/calendar/ical/x/private-y/basic.ics',
                    'ftp://example.invalid/basic.ics', 'file:///etc/hosts', '', 'calendar.google.com/basic.ics'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm:
                    calendar_ics.fetch_ics(url)
                self.assertEqual(cm.exception.args[0], 'url')

    def test_signature_defaults(self):
        sig = inspect.signature(calendar_ics.fetch_ics)
        self.assertIn('url', sig.parameters)
        self.assertEqual(sig.parameters['timeout'].default, 10.0)
        self.assertEqual(sig.parameters['max_bytes'].default, 5000000)


# ---------------------------------------------------------------------------
# refresh — fetch подменён словарём url → текст
# ---------------------------------------------------------------------------
URL_A = 'https://calendar.google.com/calendar/ical/aaa%40group.calendar.google.com/private-SECRETA/basic.ics'
URL_B = 'https://calendar.google.com/calendar/ical/bbb%40gmail.com/private-SECRETB/basic.ics'
URL_C = 'https://calendar.google.com/calendar/ical/ccc%40gmail.com/private-SECRETC/basic.ics'


def make_fake_fetch(mapping, calls=None):
    """fetch(url, ...) → текст из словаря; значение-исключение бросается."""
    def fake(url, *args, **kwargs):
        if calls is not None:
            calls.append(url)
        value = mapping[url]
        if isinstance(value, BaseException):
            raise value
        return value
    return fake


class TestRefresh(CalendarCase):
    def setUp(self):
        self.text_a = ics(
            vevent('a1', 'Teambesprechung', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                   'DTEND;TZID=Europe/Berlin:20261027T093000'),
            vevent('a2', 'Urlaub', 'DTSTART;VALUE=DATE:20261026', 'DTEND;VALUE=DATE:20261028'),
            vevent('a3', 'Zu alt', 'DTSTART;TZID=Europe/Berlin:20260901T090000',
                   'DTEND;TZID=Europe/Berlin:20260901T100000'),
            name='Praxis')
        self.text_c = ics(
            vevent('c1', 'Zahnarzt', 'DTSTART;TZID=Europe/Berlin:20261028T160000',
                   'DTEND;TZID=Europe/Berlin:20261028T170000'),
            vevent('c2', 'Täglich', 'DTSTART;TZID=Europe/Berlin:20261001T070000',
                   'DTEND;TZID=Europe/Berlin:20261001T071500', extra=['RRULE:FREQ=DAILY']),
            name='Privat')

    def test_all_good(self):
        calls = []
        fetch = make_fake_fetch({URL_A: self.text_a, URL_C: self.text_c}, calls)
        r = calendar_ics.refresh([URL_A, URL_C], TODAY, TZ, fetch=fetch)
        self.assertEqual(sorted(calls), sorted([URL_A, URL_C]))
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['window'], ['2026-10-20', '2026-12-26'])
        datetime.datetime.strptime(r['fetched_at'][:19], '%Y-%m-%dT%H:%M:%S')
        uids = set(e['uid'] for e in r['events'])
        self.assertIn('a1', uids)
        self.assertIn('a2', uids)
        self.assertIn('c1', uids)
        self.assertIn('c2', uids)
        self.assertNotIn('a3', uids, 'событие до окна (today − 7) не попадает в кэш')
        by_uid = dict((e['uid'], e) for e in r['events'] if e['uid'] != 'c2')
        self.assertEqual(by_uid['a1']['calendar'], 0)
        self.assertEqual(by_uid['a2']['calendar'], 0)
        self.assertEqual(by_uid['c1']['calendar'], 1)
        self.assertEqual(starts(r['events']), sorted(starts(r['events'])))
        daily = [e for e in r['events'] if e['uid'] == 'c2']
        self.assertEqual(len(daily), (datetime.date(2026, 12, 26) - datetime.date(2026, 10, 20)).days)

    def test_one_broken_link_does_not_cancel_others(self):
        fetch = make_fake_fetch({URL_A: self.text_a,
                                 URL_B: OSError('[Errno -2] Name or service not known: ' + URL_B),
                                 URL_C: self.text_c})
        r = calendar_ics.refresh([URL_A, URL_B, URL_C], TODAY, TZ, fetch=fetch)
        self.assertEqual(len(r['errors']), 1)
        err = r['errors'][0]
        self.assertEqual(err['url_index'], 1)
        self.assertIsInstance(err['message'], str)
        self.assertTrue(err['message'])
        self.assertNotIn(URL_B, err['message'], 'секретный адрес не должен попадать в сообщение об ошибке')
        self.assertNotIn('SECRETB', err['message'])
        uids = set(e['uid'] for e in r['events'])
        self.assertIn('a1', uids)
        self.assertIn('c1', uids)
        cals = set(e['calendar'] for e in r['events'])
        self.assertEqual(cals, {0, 2}, 'индекс календаря = индекс ссылки, даже если средняя сломана')

    def test_all_links_broken(self):
        fetch = make_fake_fetch({URL_A: OSError('timed out'), URL_B: OSError('refused')})
        r = calendar_ics.refresh([URL_A, URL_B], TODAY, TZ, fetch=fetch)
        self.assertEqual(r['events'], [])
        self.assertEqual(sorted(e['url_index'] for e in r['errors']), [0, 1])
        self.assertIn('fetched_at', r)
        self.assertIn('window', r)

    def test_empty_urls(self):
        r = calendar_ics.refresh([], TODAY, TZ, fetch=make_fake_fetch({}))
        self.assertEqual(r['events'], [])
        self.assertEqual(r['errors'], [])
        self.assertIn('fetched_at', r)
        self.assertEqual(r['window'], ['2026-10-20', '2026-12-26'])

    def test_garbage_response_does_not_crash(self):
        fetch = make_fake_fetch({URL_A: '<html><body>404 Not Found</body></html>', URL_C: self.text_c})
        r = calendar_ics.refresh([URL_A, URL_C], TODAY, TZ, fetch=fetch)
        uids = set(e['uid'] for e in r['events'])
        self.assertIn('c1', uids)
        self.assertNotIn('a1', uids)

    def test_custom_window(self):
        fetch = make_fake_fetch({URL_A: self.text_a})
        r = calendar_ics.refresh([URL_A], TODAY, TZ, days_back=1, days_forward=2, fetch=fetch)
        self.assertEqual(r['window'], ['2026-10-26', '2026-10-29'])
        uids = set(e['uid'] for e in r['events'])
        self.assertEqual(uids, {'a1', 'a2'})

    def test_event_shape_in_cache(self):
        fetch = make_fake_fetch({URL_A: self.text_a})
        r = calendar_ics.refresh([URL_A], TODAY, TZ, fetch=fetch)
        for ev in r['events']:
            self.assertTrue(EVENT_KEYS <= set(ev.keys()), EVENT_KEYS - set(ev.keys()))
            self.assertIsInstance(ev['calendar'], int)
            self.assertIsInstance(ev['all_day'], bool)

    def test_result_is_json_serializable(self):
        import json
        fetch = make_fake_fetch({URL_A: self.text_a, URL_B: OSError('x')})
        r = calendar_ics.refresh([URL_A, URL_B], TODAY, TZ, fetch=fetch)
        json.dumps(r)   # кэш хранится в settings как JSON


# ---------------------------------------------------------------------------
# events_between — выборка из кэша
# ---------------------------------------------------------------------------
class TestEventsBetween(CalendarCase):
    def setUp(self):
        text = ics(
            vevent('e27a', 'Teambesprechung', 'DTSTART;TZID=Europe/Berlin:20261027T090000',
                   'DTEND;TZID=Europe/Berlin:20261027T093000'),
            vevent('e27b', 'Fortbildung', 'DTSTART;VALUE=DATE:20261027'),
            vevent('e28', 'Zahnarzt', 'DTSTART;TZID=Europe/Berlin:20261028T160000',
                   'DTEND;TZID=Europe/Berlin:20261028T170000'),
            vevent('e30', 'Abrechnung', 'DTSTART;TZID=Europe/Berlin:20261030T160000',
                   'DTEND;TZID=Europe/Berlin:20261030T170000'),
            vevent('e26', 'Gestern', 'DTSTART;TZID=Europe/Berlin:20261026T230000',
                   'DTEND;TZID=Europe/Berlin:20261026T233000'),
        )
        self.cache = calendar_ics.refresh([URL_A], TODAY, TZ, fetch=make_fake_fetch({URL_A: text}))

    def test_single_day(self):
        got = calendar_ics.events_between(self.cache, datetime.date(2026, 10, 27), datetime.date(2026, 10, 28))
        self.assertEqual(sorted(e['uid'] for e in got), ['e27a', 'e27b'])
        self.assertEqual(starts(got), sorted(starts(got)))

    def test_range(self):
        got = calendar_ics.events_between(self.cache, datetime.date(2026, 10, 27), datetime.date(2026, 10, 30))
        self.assertEqual(sorted(e['uid'] for e in got), ['e27a', 'e27b', 'e28'])

    def test_end_exclusive(self):
        got = calendar_ics.events_between(self.cache, datetime.date(2026, 10, 28), datetime.date(2026, 10, 30))
        self.assertEqual([e['uid'] for e in got], ['e28'])

    def test_empty_range_and_empty_cache(self):
        self.assertEqual(calendar_ics.events_between(self.cache, datetime.date(2026, 11, 20),
                                                     datetime.date(2026, 11, 21)), [])
        self.assertEqual(calendar_ics.events_between({'events': []}, datetime.date(2026, 10, 27),
                                                     datetime.date(2026, 10, 28)), [])

    def test_does_not_mutate_cache(self):
        before = [dict(e) for e in self.cache['events']]
        calendar_ics.events_between(self.cache, datetime.date(2026, 10, 27), datetime.date(2026, 10, 28))
        self.assertEqual(self.cache['events'], before)

    def test_multiday_all_day_spanning_date(self):
        # Отпуск 26–29.10 (DTEND 30.10) должен показываться и 27.10, и 28.10 (как в «Сегодня»)
        text = ics(vevent('u1', 'Urlaub', 'DTSTART;VALUE=DATE:20261026', 'DTEND;VALUE=DATE:20261030'))
        cache = calendar_ics.refresh([URL_A], TODAY, TZ, fetch=make_fake_fetch({URL_A: text}))
        for day in (datetime.date(2026, 10, 27), datetime.date(2026, 10, 28)):
            with self.subTest(day=day):
                got = calendar_ics.events_between(cache, day, day + datetime.timedelta(days=1))
                self.assertEqual([e['uid'] for e in got], ['u1'])
        got = calendar_ics.events_between(cache, datetime.date(2026, 10, 30), datetime.date(2026, 10, 31))
        self.assertEqual(got, [], 'DTEND исключительный — 30.10 отпуска уже нет')


if __name__ == '__main__':
    unittest.main()
