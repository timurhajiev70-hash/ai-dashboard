# -*- coding: utf-8 -*-
"""Fokus — Google Календарь по секретному iCal-адресу (§12 AGENTS.md).

Только чтение: загрузка .ics по https, разбор RFC 5545, развёртка повторов
(RRULE/EXDATE/RECURRENCE-ID), часовые пояса через ``zoneinfo``.
Чистые функции, только стандартная библиотека, синтаксис Python 3.9.

Публичный API (§12): fetch_ics, unfold, parse_ics, expand, refresh, events_between.
Секретный адрес календаря — это пароль: ни в одно сообщение об ошибке он не попадает.
"""
import calendar as _cal
import http.client
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Union

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9 не поддерживается (§1)
    ZoneInfo = None

__all__ = [
    'fetch_ics', 'unfold', 'parse_ics', 'expand', 'refresh', 'events_between',
    'MAX_OCCURRENCES', 'USER_AGENT',
]

MAX_OCCURRENCES = 5000          # предел вхождений на событие и на результат expand()
MAX_PERIODS = 200000            # страховка от бесконечного перебора периодов RRULE
USER_AGENT = 'Fokus/2'
_CHUNK = 65536

_UTC = timezone.utc
_WEEKDAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
_FREQS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')

# Имена поясов Windows/Outlook → IANA (CLDR windowsZones, сокращённый список).
_WINDOWS_TZ = {
    'W. EUROPE STANDARD TIME': 'Europe/Berlin',
    'CENTRAL EUROPE STANDARD TIME': 'Europe/Berlin',
    'CENTRAL EUROPEAN STANDARD TIME': 'Europe/Berlin',
    'ROMANCE STANDARD TIME': 'Europe/Paris',
    'GMT STANDARD TIME': 'Europe/London',
    'GREENWICH STANDARD TIME': 'Atlantic/Reykjavik',
    'E. EUROPE STANDARD TIME': 'Europe/Chisinau',
    'FLE STANDARD TIME': 'Europe/Kiev',
    'GTB STANDARD TIME': 'Europe/Bucharest',
    'RUSSIAN STANDARD TIME': 'Europe/Moscow',
    'TURKEY STANDARD TIME': 'Europe/Istanbul',
    'ISRAEL STANDARD TIME': 'Asia/Jerusalem',
    'ARABIAN STANDARD TIME': 'Asia/Dubai',
    'INDIA STANDARD TIME': 'Asia/Kolkata',
    'CHINA STANDARD TIME': 'Asia/Shanghai',
    'TOKYO STANDARD TIME': 'Asia/Tokyo',
    'KOREA STANDARD TIME': 'Asia/Seoul',
    'SINGAPORE STANDARD TIME': 'Asia/Singapore',
    'AUS EASTERN STANDARD TIME': 'Australia/Sydney',
    'NEW ZEALAND STANDARD TIME': 'Pacific/Auckland',
    'EASTERN STANDARD TIME': 'America/New_York',
    'CENTRAL STANDARD TIME': 'America/Chicago',
    'MOUNTAIN STANDARD TIME': 'America/Denver',
    'PACIFIC STANDARD TIME': 'America/Los_Angeles',
    'ALASKAN STANDARD TIME': 'America/Anchorage',
    'HAWAIIAN STANDARD TIME': 'Pacific/Honolulu',
    'ATLANTIC STANDARD TIME': 'America/Halifax',
    'SA PACIFIC STANDARD TIME': 'America/Bogota',
    'E. SOUTH AMERICA STANDARD TIME': 'America/Sao_Paulo',
    'ARGENTINA STANDARD TIME': 'America/Buenos_Aires',
    'SOUTH AFRICA STANDARD TIME': 'Africa/Johannesburg',
    'EGYPT STANDARD TIME': 'Africa/Cairo',
    'UTC': 'UTC', 'GMT': 'UTC', 'Z': 'UTC', 'COORDINATED UNIVERSAL TIME': 'UTC',
}

_ZONE_CACHE = {}  # type: Dict[str, object]


# ---------------------------------------------------------------- загрузка

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект на не-https адрес запрещён: секретный адрес не должен уйти открытым текстом."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).lower().startswith('https://'):
            raise urllib.error.URLError('redirect to non-https address')
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def fetch_ics(url, timeout=10.0, max_bytes=5000000):
    # type: (str, float, int) -> str
    """Загрузить .ics по https. Только https → иначе ValueError('url').
    Размер больше max_bytes → ValueError('too_large'). Сетевые ошибки — OSError/urllib.error."""
    if not isinstance(url, str) or not url.strip().lower().startswith('https://'):
        raise ValueError('url')
    req = urllib.request.Request(url.strip(), headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/calendar, text/plain;q=0.8, */*;q=0.5',
    })
    chunks = []
    total = 0
    charset = None
    try:
        resp = _opener.open(req, timeout=timeout)
        try:
            length = (resp.headers.get('Content-Length') or '').strip()
            if length.isdigit() and int(length) > max_bytes:
                raise ValueError('too_large')
            charset = resp.headers.get_content_charset()
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError('too_large')
                chunks.append(chunk)
        finally:
            resp.close()
    except http.client.HTTPException as exc:  # IncompleteRead и т. п. — не OSError
        raise OSError('http: %s' % exc.__class__.__name__)
    data = b''.join(chunks)
    if charset:
        try:
            return data.decode(charset, 'replace')
        except LookupError:
            pass
    return data.decode('utf-8', 'replace')


# ---------------------------------------------------------------- разбор

def unfold(text):
    # type: (str) -> List[str]
    """Склеить продолжения строк (RFC 5545 §3.1): строка с ведущим пробелом/табом
    продолжает предыдущую. CRLF и LF; BOM в начале убирается; пустые строки отбрасываются."""
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    if text.startswith('﻿'):
        text = text[1:]
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    out = []  # type: List[str]
    for line in text.split('\n'):
        if not line:
            continue
        if line[0] in ' \t' and out:
            out[-1] += line[1:]
        elif line[0] in ' \t':
            out.append(line[1:])
        else:
            out.append(line)
    return out


def _split_outside_quotes(s, sep):
    # type: (str, str) -> List[str]
    parts = []
    buf = []
    in_q = False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == sep and not in_q:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))
    return parts


def _parse_line(line):
    # type: (str) -> Tuple[str, Dict[str, str], str]
    """'NAME;P=V;Q="a:b":value' → ('NAME', {'P': 'V', 'Q': 'a:b'}, 'value')."""
    i = 0
    in_q = False
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_q = not in_q
        elif ch == ':' and not in_q:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    parts = _split_outside_quotes(head, ';')
    name = parts[0].strip().upper()
    params = {}  # type: Dict[str, str]
    for p in parts[1:]:
        if '=' in p:
            k, v = p.split('=', 1)
            params[k.strip().upper()] = v.strip().strip('"')
        elif p.strip():
            params[p.strip().upper()] = ''
    return name, params, value


_UNESCAPE_RE = re.compile(r'\\([\\;,nN])')


def _unescape_text(value):
    # type: (str) -> str
    def repl(m):
        c = m.group(1)
        if c in 'nN':
            return '\n'
        return c
    return _UNESCAPE_RE.sub(repl, value)


def _fixed_offset_zone(tzid):
    # type: (str) -> Optional[timezone]
    m = re.match(r'^(?:GMT|UTC)\s*([+-])(\d{1,2})(?::?(\d{2}))?$', tzid.strip(), re.I)
    if not m:
        return None
    sign = -1 if m.group(1) == '-' else 1
    hours = int(m.group(2))
    minutes = int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _zone_or_none(name):
    # type: (Optional[str]) -> Optional[object]
    """IANA-имя, Windows-имя или GMT+hh:mm → tzinfo; иначе None."""
    if not name or ZoneInfo is None:
        return None
    name = name.strip().strip('"')
    if not name:
        return None
    if name in _ZONE_CACHE:
        return _ZONE_CACHE[name]
    zone = None
    candidates = [name]
    if name.startswith('/'):
        # /mozilla.org/20050126_1/Europe/Berlin → Europe/Berlin
        parts = [p for p in name.split('/') if p]
        candidates = ['/'.join(parts[i:]) for i in range(len(parts))]
    if ' ' in name:
        candidates.append(name.replace(' ', '_'))
    for cand in candidates:
        try:
            zone = ZoneInfo(cand)
            break
        except Exception:
            zone = None
    if zone is None:
        iana = _WINDOWS_TZ.get(name.upper())
        if iana:
            try:
                zone = ZoneInfo(iana)
            except Exception:
                zone = None
    if zone is None:
        zone = _fixed_offset_zone(name)
    _ZONE_CACHE[name] = zone
    return zone


def _zone(tz):
    # type: (Union[str, object, None]) -> object
    """Пояс настроек: строка → ZoneInfo; tzinfo → как есть; неизвестный → UTC."""
    if tz is None:
        return _UTC
    if not isinstance(tz, str):
        return tz
    return _zone_or_none(tz) or _UTC


def _parse_dt_value(value, params, default_zone, lic_map):
    # type: (str, Dict[str, str], Optional[object], Dict[str, str]) -> Union[date, datetime, None]
    """Значение DATE / DATE-TIME. TZID → zoneinfo (неизвестный → default_zone, иначе naive);
    Z → UTC; без зоны (floating) → default_zone, иначе naive datetime."""
    v = value.strip()
    if not v:
        return None
    m = re.match(r'^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?(Z)?$', v, re.I)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    is_date = params.get('VALUE', '').upper() == 'DATE' or m.group(4) is None
    try:
        if is_date:
            return date(y, mo, d)
        hh, mi, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
        if hh == 24 and mi == 0 and ss == 0:  # «24:00:00» → полночь следующего дня
            naive = datetime(y, mo, d) + timedelta(days=1)
        else:
            naive = datetime(y, mo, d, hh, mi, min(ss, 59))
    except ValueError:
        return None
    if m.group(7):
        return naive.replace(tzinfo=_UTC)
    tzid = params.get('TZID')
    if tzid:
        zone = _zone_or_none(tzid)
        if zone is None and tzid in lic_map:
            zone = _zone_or_none(lic_map[tzid])
        if zone is None:
            zone = default_zone
        return naive.replace(tzinfo=zone) if zone is not None else naive
    if default_zone is not None:
        return naive.replace(tzinfo=default_zone)
    return naive


_DURATION_RE = re.compile(
    r'^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$', re.I)


def _parse_duration(value):
    # type: (str) -> Optional[timedelta]
    m = _DURATION_RE.match(value.strip())
    if not m or value.strip().upper() in ('P', 'PT', '+P', '-P'):
        return None
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                   minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == '-' else td


def _parse_rrule_raw(value):
    # type: (str) -> Dict[str, str]
    rule = {}  # type: Dict[str, str]
    for part in value.split(';'):
        if '=' in part:
            k, v = part.split('=', 1)
            k = k.strip().upper()
            if k:
                rule[k] = v.strip()
    return rule


def parse_ics(text, tz=None):
    # type: (str, Optional[str]) -> List[dict]
    """VEVENT → список словарей {'uid','summary','location','description','dtstart','dtend',
    'all_day','rrule','exdates','recurrence_id','duration','status'}.

    dtstart/dtend: date (VALUE=DATE) или datetime с tzinfo; floating-время получает пояс
    календаря X-WR-TIMEZONE (или `tz`, если задан), иначе остаётся naive — expand() подставит
    свой пояс. DTEND отсутствует → дата +1 день / datetime = dtstart (+DURATION, если есть).
    STATUS:CANCELLED без RECURRENCE-ID → пропускается; отменённое вхождение (с RECURRENCE-ID)
    сохраняется со status='CANCELLED', чтобы expand() убрал базовое вхождение.
    rrule — словарь частей RRULE как есть ({'FREQ': 'WEEKLY', 'BYDAY': 'MO,WE', ...}) или None."""
    lines = unfold(text if text is not None else '')
    cal_zone = _zone_or_none(tz) if tz else None
    lic_map = {}  # type: Dict[str, str]   # TZID из VTIMEZONE → X-LIC-LOCATION
    # первый проход: пояс календаря и VTIMEZONE (их объявляют до событий, но на всякий случай — целиком)
    depth_tz = None
    for line in lines:
        name, params, value = _parse_line(line)
        if name == 'X-WR-TIMEZONE' and cal_zone is None:
            cal_zone = _zone_or_none(value)
        elif name == 'BEGIN' and value.strip().upper() == 'VTIMEZONE':
            depth_tz = {}
        elif depth_tz is not None:
            if name == 'TZID':
                depth_tz['TZID'] = value.strip().strip('"')
            elif name == 'X-LIC-LOCATION':
                depth_tz['LOC'] = value.strip()
            elif name == 'END' and value.strip().upper() == 'VTIMEZONE':
                if depth_tz.get('TZID') and depth_tz.get('LOC'):
                    lic_map[depth_tz['TZID']] = depth_tz['LOC']
                depth_tz = None

    events = []  # type: List[dict]
    stack = []   # type: List[str]
    props = None  # type: Optional[Dict[str, list]]
    counter = 0
    for line in lines:
        name, params, value = _parse_line(line)
        if name == 'BEGIN':
            comp = value.strip().upper()
            stack.append(comp)
            if comp == 'VEVENT' and props is None:
                props = {}
            continue
        if name == 'END':
            comp = value.strip().upper()
            if stack and stack[-1] == comp:
                stack.pop()
            elif comp in stack:
                while stack and stack[-1] != comp:
                    stack.pop()
                if stack:
                    stack.pop()
            if comp == 'VEVENT' and props is not None:
                counter += 1
                ev = _build_event(props, cal_zone, lic_map, counter)
                if ev is not None:
                    events.append(ev)
                props = None
            continue
        if props is None or not stack or stack[-1] != 'VEVENT':
            continue  # вне VEVENT или внутри VALARM
        if not name:
            continue
        props.setdefault(name, []).append((params, value))
    return events


def _first(props, name):
    # type: (Dict[str, list], str) -> Optional[Tuple[Dict[str, str], str]]
    lst = props.get(name)
    return lst[-1] if lst else None


def _build_event(props, cal_zone, lic_map, counter):
    # type: (Dict[str, list], Optional[object], Dict[str, str], int) -> Optional[dict]
    st = _first(props, 'DTSTART')
    if st is None:
        return None
    dtstart = _parse_dt_value(st[1], st[0], cal_zone, lic_map)
    if dtstart is None:
        return None
    all_day = not isinstance(dtstart, datetime)

    status_item = _first(props, 'STATUS')
    status = status_item[1].strip().upper() if status_item else None
    rid_item = _first(props, 'RECURRENCE-ID')
    recurrence_id = None
    if rid_item is not None:
        recurrence_id = _parse_dt_value(rid_item[1], rid_item[0], cal_zone, lic_map)
    if status == 'CANCELLED' and recurrence_id is None:
        return None

    duration = None
    dur_item = _first(props, 'DURATION')
    if dur_item is not None:
        duration = _parse_duration(dur_item[1])

    dtend = None
    en = _first(props, 'DTEND')
    if en is not None:
        dtend = _parse_dt_value(en[1], en[0], cal_zone, lic_map)
    if dtend is not None:
        # тип конца приводим к типу начала
        if all_day and isinstance(dtend, datetime):
            dtend = dtend.date()
        elif not all_day and not isinstance(dtend, datetime):
            dtend = datetime.combine(dtend, time(0)).replace(tzinfo=dtstart.tzinfo)
    if dtend is None:
        if all_day:
            days = duration.days if (duration is not None and duration.days > 0) else 1
            dtend = dtstart + timedelta(days=days)
        elif duration is not None and duration > timedelta(0):
            dtend = dtstart + duration
        else:
            dtend = dtstart
    if all_day and dtend <= dtstart:
        dtend = dtstart + timedelta(days=1)
    if not all_day and _cmp_dt(dtend, dtstart) < 0:
        dtend = dtstart

    exdates = []  # type: list
    for params, value in props.get('EXDATE', []):
        for piece in value.split(','):
            d = _parse_dt_value(piece, params, cal_zone, lic_map)
            if d is not None:
                exdates.append(d)

    rrule = None
    rr = _first(props, 'RRULE')
    if rr is not None:
        parsed = _parse_rrule_raw(rr[1])
        rrule = parsed if parsed else None

    uid_item = _first(props, 'UID')
    uid = uid_item[1].strip() if uid_item and uid_item[1].strip() else '_%d' % counter

    def text_of(name):
        item = _first(props, name)
        return _unescape_text(item[1]).strip() if item else ''

    return {
        'uid': uid,
        'summary': text_of('SUMMARY'),
        'location': text_of('LOCATION'),
        'description': text_of('DESCRIPTION'),
        'dtstart': dtstart,
        'dtend': dtend,
        'all_day': all_day,
        'rrule': rrule,
        'exdates': exdates,
        'recurrence_id': recurrence_id,
        'duration': duration,
        'status': status,
    }


def _cmp_dt(a, b):
    # type: (datetime, datetime) -> int
    """Сравнение datetime, терпимое к смеси naive/aware (naive считаем UTC)."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a if a.tzinfo is not None else a.replace(tzinfo=_UTC)
        b = b if b.tzinfo is not None else b.replace(tzinfo=_UTC)
    return (a > b) - (a < b)


# ---------------------------------------------------------------- развёртка RRULE

def _to_date(value):
    # type: (Union[date, datetime, str]) -> date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _aware(dt, zone):
    # type: (datetime, object) -> datetime
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=zone)


def _week_start(d, wkst_idx):
    # type: (date, int) -> date
    return d - timedelta(days=(d.weekday() - wkst_idx) % 7)


def _add_months(y, m, k):
    # type: (int, int, int) -> Tuple[int, int]
    total = (y * 12 + (m - 1)) + k
    return total // 12, total % 12 + 1


def _compile_rrule(raw, base):
    # type: (Dict[str, str], datetime) -> Optional[dict]
    """Сырой словарь RRULE → разобранные части; неизвестные/битые части игнорируются."""
    if isinstance(raw, str):
        raw = _parse_rrule_raw(raw)
    if not isinstance(raw, dict):
        return None
    freq = str(raw.get('FREQ', '')).strip().upper()
    if freq not in _FREQS:
        return None

    def int_list(key, lo, hi, allow_neg):
        out = []
        for piece in str(raw.get(key, '')).split(','):
            piece = piece.strip()
            if not piece:
                continue
            try:
                n = int(piece)
            except ValueError:
                continue
            if lo <= n <= hi or (allow_neg and -hi <= n <= -lo):
                out.append(n)
        return out

    try:
        interval = int(str(raw.get('INTERVAL', '1')).strip() or 1)
    except ValueError:
        interval = 1
    if interval < 1:
        interval = 1
    count = None
    if raw.get('COUNT'):
        try:
            count = int(str(raw['COUNT']).strip())
        except ValueError:
            count = None
        if count is not None and count < 1:
            count = None
    byday = []  # type: List[Tuple[int, int]]
    for piece in str(raw.get('BYDAY', '')).split(','):
        m = re.match(r'^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$', piece.strip().upper())
        if m:
            n = int(m.group(1)) if m.group(1) else 0
            if -53 <= n <= 53:
                byday.append((n, _WEEKDAYS.index(m.group(2))))
    wkst = str(raw.get('WKST', 'MO')).strip().upper()
    wkst_idx = _WEEKDAYS.index(wkst) if wkst in _WEEKDAYS else 0
    until = None
    if raw.get('UNTIL'):
        until = _parse_dt_value(str(raw['UNTIL']), {}, None, {})
    return {
        'freq': freq,
        'interval': interval,
        'count': count,
        'until': until,
        'byday': byday,
        'bymonthday': int_list('BYMONTHDAY', 1, 31, True),
        'bymonth': int_list('BYMONTH', 1, 12, False),
        'wkst': wkst_idx,
    }


def _month_days(y, m, rule, base_day):
    # type: (int, int, dict, int) -> List[date]
    """Дни месяца по BYMONTHDAY/BYDAY (пересечение, если заданы оба); без них — день dtstart."""
    ndays = _cal.monthrange(y, m)[1]
    days = set()
    if rule['bymonthday']:
        for md in rule['bymonthday']:
            d = md if md > 0 else ndays + 1 + md
            if 1 <= d <= ndays:
                days.add(d)
    if rule['byday']:
        wd_days = set()
        first_wd = date(y, m, 1).weekday()
        for n, wdi in rule['byday']:
            first = 1 + (wdi - first_wd) % 7
            all_days = list(range(first, ndays + 1, 7))
            if n == 0:
                wd_days.update(all_days)
            elif n > 0 and n <= len(all_days):
                wd_days.add(all_days[n - 1])
            elif n < 0 and -n <= len(all_days):
                wd_days.add(all_days[n])
        if rule['bymonthday']:
            days &= wd_days
        else:
            days = wd_days
    if not rule['bymonthday'] and not rule['byday']:
        if base_day <= ndays:
            days.add(base_day)
    return [date(y, m, d) for d in sorted(days)]


def _year_days(y, rule, base):
    # type: (int, dict, datetime) -> List[date]
    months = rule['bymonth'] or [base.month]
    if rule['byday'] and not rule['bymonth'] and not rule['bymonthday']:
        # BYDAY относительно года: 20MO — 20-й понедельник года, MO — все понедельники
        out = set()
        jan1 = date(y, 1, 1)
        ndays = 366 if _cal.isleap(y) else 365
        for n, wdi in rule['byday']:
            first = (wdi - jan1.weekday()) % 7
            all_days = [jan1 + timedelta(days=k) for k in range(first, ndays, 7)]
            if n == 0:
                out.update(all_days)
            elif n > 0 and n <= len(all_days):
                out.add(all_days[n - 1])
            elif n < 0 and -n <= len(all_days):
                out.add(all_days[n])
        return sorted(out)
    out = []
    for m in sorted(set(months)):
        out.extend(_month_days(y, m, rule, base.day))
    return out


def _matches_filters(d, rule, check_byday):
    # type: (date, dict, bool) -> bool
    if rule['bymonth'] and d.month not in rule['bymonth']:
        return False
    if rule['bymonthday']:
        ndays = _cal.monthrange(d.year, d.month)[1]
        ok = False
        for md in rule['bymonthday']:
            if (md > 0 and md == d.day) or (md < 0 and ndays + 1 + md == d.day):
                ok = True
                break
        if not ok:
            return False
    if check_byday and rule['byday']:
        if d.weekday() not in [wdi for _, wdi in rule['byday']]:
            return False
    return True


def _rrule_dates(base, rule, lower, hard_stop, all_day, ev_zone, disp_zone):
    # type: (datetime, dict, date, date, bool, object, object) -> list
    """Генератор naive-дат/времён вхождений (локальное время события) по возрастанию.
    base — naive datetime начала; lower — дата, раньше которой вхождения не нужны (только
    без COUNT: периоды до неё пропускаются); hard_stop — дата, после начала периода за
    которой перебор прекращается. UNTIL и COUNT учитываются здесь."""
    freq = rule['freq']
    interval = rule['interval']
    count = rule['count']
    base_date = base.date()
    base_time = base.time()

    # UNTIL → сравнимая граница
    until = rule['until']
    until_date = None       # для событий на весь день
    until_aware = None      # для событий со временем
    if until is not None:
        if all_day:
            if isinstance(until, datetime):
                u = until if until.tzinfo is not None else until.replace(tzinfo=disp_zone)
                until_date = u.astimezone(disp_zone).date()
            else:
                until_date = until
            hard_stop = min(hard_stop, until_date + timedelta(days=1))
        else:
            if isinstance(until, datetime):
                until_aware = until if until.tzinfo is not None else until.replace(tzinfo=ev_zone)
            else:
                until_aware = datetime.combine(until, time(23, 59, 59)).replace(tzinfo=ev_zone)
            hard_stop = min(hard_stop, until_aware.astimezone(ev_zone).date() + timedelta(days=1))

    def too_late(cand):
        # type: (datetime) -> bool
        if all_day:
            return until_date is not None and cand.date() > until_date
        return until_aware is not None and cand.replace(tzinfo=ev_zone) > until_aware

    # период k → (дата начала периода, даты-кандидаты)
    if freq == 'DAILY':
        def period(k):
            d = base_date + timedelta(days=k * interval)
            return d, ([d] if _matches_filters(d, rule, True) else [])
        k0 = (lower - base_date).days // interval
    elif freq == 'WEEKLY':
        ws0 = _week_start(base_date, rule['wkst'])
        wdays = sorted(set((wdi - rule['wkst']) % 7 for _, wdi in rule['byday'])) \
            or [(base_date.weekday() - rule['wkst']) % 7]

        def period(k):
            ws = ws0 + timedelta(days=7 * k * interval)
            cands = [ws + timedelta(days=off) for off in wdays]
            return ws, [d for d in cands if _matches_filters(d, rule, False)]
        k0 = (lower - ws0).days // (7 * interval)
    elif freq == 'MONTHLY':
        def period(k):
            y, m = _add_months(base_date.year, base_date.month, k * interval)
            if rule['bymonth'] and m not in rule['bymonth']:
                return date(y, m, 1), []
            return date(y, m, 1), _month_days(y, m, rule, base_date.day)
        k0 = ((lower.year - base_date.year) * 12 + (lower.month - base_date.month)) // interval
    else:  # YEARLY
        def period(k):
            y = base_date.year + k * interval
            return date(y, 1, 1), _year_days(y, rule, base)
        k0 = (lower.year - base_date.year) // interval

    if count is not None or k0 < 0:
        k0 = 0   # COUNT считает все вхождения от dtstart — пропускать периоды нельзя

    produced = 0
    if k0 == 0:
        # DTSTART — всегда первое вхождение набора (RFC 5545 §3.8.5.3)
        produced = 1
        if not too_late(base):
            yield base
        else:
            return
    for k in range(k0, k0 + MAX_PERIODS):
        pstart, cands = period(k)
        if pstart > hard_stop:
            return
        for d in cands:
            cand = datetime.combine(d, base_time)
            if cand <= base:
                continue   # раньше dtstart или сам dtstart (уже выдан)
            if too_late(cand):
                return
            produced += 1
            if count is not None and produced > count:
                return
            yield cand
    return


def _span(ev):
    # type: (dict) -> Tuple[date, date]
    """Event (строки) → (дата начала, исключительная дата конца) для проверки окна."""
    s = ev.get('start') or ''
    e = ev.get('end') or s
    sd = date.fromisoformat(s[:10])
    try:
        ed = date.fromisoformat(e[:10])
    except ValueError:
        ed = sd
    if ev.get('all_day'):
        return sd, (ed if ed > sd else sd + timedelta(days=1))
    if ed > sd and len(e) >= 16 and e[11:16] == '00:00':
        return sd, ed   # закончилось ровно в полночь — этот день не занимает
    return sd, (ed + timedelta(days=1) if ed >= sd else sd + timedelta(days=1))


def _fmt(value, zone):
    # type: (Union[date, datetime], object) -> str
    if isinstance(value, datetime):
        return _aware(value, zone).astimezone(zone).strftime('%Y-%m-%dT%H:%M')
    return value.isoformat()


def _occ_keys(value, all_day, zone):
    # type: (Union[date, datetime], bool, object) -> list
    """Ключи сравнения для EXDATE / RECURRENCE-ID с вхождением."""
    if isinstance(value, datetime):
        if all_day:
            return [('d', _aware(value, zone).astimezone(zone).date())]
        a = _aware(value, zone)
        return [('t', a.astimezone(_UTC).replace(tzinfo=None))]
    return [('d', value)]


def expand(vevents, start, end, tz, calendar_index=0):
    # type: (List[dict], date, date, str, int) -> List[dict]
    """Вхождения событий в окне [start, end) (даты в поясе tz) → список Event (§7):
    {'uid','summary','location','start','end','all_day','calendar'}; start/end — строки
    'YYYY-MM-DDTHH:MM' в tz или 'YYYY-MM-DD' (all_day, end исключительный).
    Многодневные вхождения попадают в результат, если пересекаются с окном.
    Сортировка по start, затем summary. Не больше MAX_OCCURRENCES вхождений."""
    zone = _zone(tz)
    start_d = _to_date(start)
    end_d = _to_date(end)
    result = []  # type: List[dict]
    if not vevents or end_d <= start_d:
        return result

    # ключи переопределённых вхождений (RECURRENCE-ID) по UID
    overrides = {}  # type: Dict[str, set]
    for ev in vevents:
        rid = ev.get('recurrence_id')
        if rid is None:
            continue
        keys = overrides.setdefault(ev['uid'], set())
        base_all_day = ev.get('all_day', False)
        for key in _occ_keys(rid, base_all_day, zone):
            keys.add(key)
        if isinstance(rid, datetime):
            # на случай, если базовое событие — на весь день, а RECURRENCE-ID со временем
            keys.add(('d', _aware(rid, zone).astimezone(zone).date()))

    def emit(ev, s_val, e_val, all_day):
        # type: (dict, Union[date, datetime], Union[date, datetime], bool) -> bool
        item = {
            'uid': ev['uid'],
            'summary': ev.get('summary') or '',
            'location': ev.get('location') or '',
            'start': _fmt(s_val, zone),
            'end': _fmt(e_val, zone),
            'all_day': bool(all_day),
            'calendar': calendar_index,
        }
        sd, ed = _span(item)
        if sd >= end_d:
            return False      # дальше по времени — вхождений в окне не будет
        if ed > start_d:
            result.append(item)
        return True

    total_cap_hit = False
    for ev in vevents:
        if total_cap_hit or len(result) >= MAX_OCCURRENCES:
            break
        if (ev.get('status') or '').upper() == 'CANCELLED':
            continue
        dtstart = ev.get('dtstart')
        if dtstart is None:
            continue
        all_day = bool(ev.get('all_day', not isinstance(dtstart, datetime)))
        dtend = ev.get('dtend') or dtstart
        if ev.get('recurrence_id') is not None:
            # перенесённое/изменённое вхождение — одиночное событие на новом месте
            emit(ev, dtstart, dtend, all_day)
            continue

        ev_zone = zone
        if isinstance(dtstart, datetime):
            dtstart = _aware(dtstart, zone)
            ev_zone = dtstart.tzinfo or zone
            if isinstance(dtend, datetime):
                dtend = _aware(dtend, ev_zone)
            else:
                dtend = datetime.combine(dtend, time(0)).replace(tzinfo=ev_zone)
            dur = dtend.astimezone(_UTC) - dtstart.astimezone(_UTC)
            if dur < timedelta(0):
                dur = timedelta(0)
            base = dtstart.replace(tzinfo=None)
        else:
            if isinstance(dtend, datetime):
                dtend = _aware(dtend, zone).astimezone(zone).date()
            dur = dtend - dtstart
            if dur < timedelta(days=1):
                dur = timedelta(days=1)
            base = datetime.combine(dtstart, time(0))

        # ключи исключений
        ex_keys = set()
        for x in ev.get('exdates') or []:
            for key in _occ_keys(x, all_day, ev_zone):
                ex_keys.add(key)
        ov_keys = overrides.get(ev['uid'], set())

        rule = _compile_rrule(ev['rrule'], base) if ev.get('rrule') else None
        if rule is None:
            occurrences = iter([base])
        else:
            # вхождения раньше lower не нужны (с запасом на длительность и разницу поясов)
            lower = start_d - timedelta(days=max(dur.days, 0) + 2)
            hard_stop = end_d + timedelta(days=1)
            occurrences = _rrule_dates(base, rule, lower, hard_stop, all_day, ev_zone, zone)

        produced = 0
        for cand in occurrences:
            if all_day:
                s_val = cand.date()
                e_val = s_val + dur
                keys = [('d', s_val)]
            else:
                s_val = cand.replace(tzinfo=ev_zone)
                e_val = (s_val.astimezone(_UTC) + dur).astimezone(zone)
                keys = [('t', s_val.astimezone(_UTC).replace(tzinfo=None)), ('d', cand.date())]
            if any(k in ex_keys for k in keys) or any(k in ov_keys for k in keys):
                continue
            if not emit(ev, s_val, e_val, all_day):
                break
            produced += 1
            if produced >= MAX_OCCURRENCES:
                break
            if len(result) >= MAX_OCCURRENCES:
                total_cap_hit = True
                break

    result.sort(key=lambda e: (e['start'], e['summary']))
    return result[:MAX_OCCURRENCES]


# ---------------------------------------------------------------- обновление и фильтр

_URL_RE = re.compile(r'[a-z][a-z0-9+.-]*://\S+', re.I)


def _safe_message(exc, urls):
    # type: (BaseException, List[str]) -> str
    """Короткое сообщение об ошибке без секретного адреса."""
    if isinstance(exc, ValueError):
        code = str(exc)
        if code == 'url':
            return 'invalid url: https:// required'
        if code == 'too_large':
            return 'calendar file too large'
    if isinstance(exc, urllib.error.HTTPError):
        msg = 'HTTP %s %s' % (exc.code, exc.reason or '')
    elif isinstance(exc, urllib.error.URLError):
        msg = 'network error: %s' % (exc.reason,)
    elif isinstance(exc, (TimeoutError, OSError)) and 'timed out' in str(exc).lower():
        msg = 'timeout'
    else:
        msg = '%s: %s' % (exc.__class__.__name__, exc)
    for u in urls:
        if isinstance(u, str) and u:
            msg = msg.replace(u, '…')
    msg = _URL_RE.sub('…', msg)
    msg = ' '.join(msg.split())
    return msg[:200] if msg else exc.__class__.__name__


def refresh(urls, today, tz, days_back=7, days_forward=60, fetch=fetch_ics):
    # type: (List[str], date, str, int, int, object) -> dict
    """Загрузить все календари и развернуть события в окне [today-days_back, today+days_forward).
    Результат: {'events': [...], 'fetched_at': ISO, 'errors': [{'url_index', 'message'}],
    'window': ['YYYY-MM-DD', 'YYYY-MM-DD']}. Ошибка одной ссылки не отменяет остальные;
    'calendar' события = индекс ссылки. `fetch` подменяется в тестах."""
    zone = _zone(tz)
    today_d = _to_date(today)
    start_d = today_d - timedelta(days=max(int(days_back), 0))
    end_d = today_d + timedelta(days=max(int(days_forward), 1))
    events = []  # type: List[dict]
    errors = []  # type: List[dict]
    url_list = list(urls or [])
    for i, url in enumerate(url_list):
        try:
            if not isinstance(url, str) or not url.strip():
                raise ValueError('url')
            text = fetch(url.strip())
            vevents = parse_ics(text, tz=tz if isinstance(tz, str) else None)
            events.extend(expand(vevents, start_d, end_d, zone, calendar_index=i))
        except Exception as exc:  # noqa: BLE001 — одна сломанная ссылка не ломает остальные
            errors.append({'url_index': i, 'message': _safe_message(exc, url_list)})
    events.sort(key=lambda e: (e['start'], e['summary']))
    return {
        'events': events[:MAX_OCCURRENCES],
        'fetched_at': datetime.now(zone).isoformat(timespec='seconds'),
        'errors': errors,
        'window': [start_d.isoformat(), end_d.isoformat()],
    }


def events_between(cache, start, end):
    # type: (dict, date, date) -> List[dict]
    """События кэша (результат refresh), пересекающиеся с окном [start, end) по дате начала;
    многодневные — если пересекаются с окном. Отсортировано по start, summary."""
    if not cache:
        return []
    events = cache if isinstance(cache, list) else cache.get('events') or []
    start_d = _to_date(start)
    end_d = _to_date(end)
    out = []  # type: List[dict]
    for ev in events:
        try:
            sd, ed = _span(ev)
        except (ValueError, TypeError, AttributeError):
            continue
        if sd < end_d and ed > start_d:
            out.append(ev)
    out.sort(key=lambda e: (e.get('start') or '', e.get('summary') or ''))
    return out
