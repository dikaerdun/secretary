"""Pure arrangement time values, resolved at the original submission time.

Date/window boundaries are notification policy, never invented execution clocks.
No function here reads a database or schedules an activity.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import hashlib
import json
import re

from .secretary_interpreter import clock_from_text, day_from_text, number, NUMBER
from .store import SHANGHAI, _timestamp

TIMEZONE = 'Asia/Shanghai'
WINDOWS = {'上午': (9, 12), '下午': (14, 18), '晚上': (19, 22)}


class ArrangementTimeError(ValueError):
    pass


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ArrangementTimeError('日期需要为YYYY-MM-DD。')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ArrangementTimeError('日期没有识别清楚，请核对具体年月日。') from None


def _point(value):
    try:
        if isinstance(value, str):
            point = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if point.tzinfo is None:
                raise ArrangementTimeError('具体时刻需包含时区，不能猜测时区。')
            return _timestamp(point.timestamp())
        return _timestamp(value)
    except (ValueError, TypeError, OverflowError):
        raise ArrangementTimeError('具体时刻无效，请核对日期、钟点和时区。') from None


def _local(day, hour=0, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI).timestamp()


def _month(day, increment):
    total = day.year * 12 + day.month - 1 + increment
    year, zero_month = divmod(total, 12)
    month = zero_month + 1
    try:
        return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))
    except (ValueError, OverflowError):
        raise ArrangementTimeError('月份超出支持范围。') from None


def _value(precision, day, raw, *, at=None, window=None, start=None, end=None, end_day=None):
    if precision == 'date':
        start, end = _local(day), _local(day + timedelta(days=1))
    elif precision == 'instant':
        start = end = at
    elif window == 'calendar':
        start, end = _local(day), _local(end_day)
    elif start is None:
        hour_start, hour_end = WINDOWS[window]
        start, end = _local(day, hour_start), _local(day, hour_end)
    result = {'precision': precision, 'date': day.isoformat(), 'timezone': TIMEZONE,
              'raw_text': raw, 'at': at, 'start_at': start, 'end_at': end,
              'boundary_source': 'explicit' if precision == 'instant' else 'policy'}
    if window:
        result['window'] = window
    if end_day is not None:
        result['end_date'] = end_day.isoformat()
    return result


def _structured(value):
    allowed = {'precision', 'date', 'timezone', 'raw_text', 'at', 'start_at', 'end_at',
               'window', 'boundary_source', 'end_date'}
    if set(value) - allowed or value.get('precision') not in ('date', 'window', 'instant'):
        raise ArrangementTimeError('时间精度或字段无效。')
    if value.get('timezone', TIMEZONE) != TIMEZONE:
        raise ArrangementTimeError('本版安排使用北京时间，请明确转换后提交。')
    raw = value.get('raw_text', '')
    if not isinstance(raw, str) or len(raw) > 2000:
        raise ArrangementTimeError('时间原话无效。')
    precision = value['precision']
    if precision == 'instant':
        at = _point(value.get('at', value.get('start_at')))
        day = datetime.fromtimestamp(at, SHANGHAI).date()
        if value.get('date') is not None and _date(value['date']) != day:
            raise ArrangementTimeError('具体时刻与本地日期不一致。')
        result = _value(precision, day, raw, at=at)
    else:
        day = _date(value.get('date'))
        if value.get('at') is not None:
            raise ArrangementTimeError('日期或时段精度不能包含伪造的具体执行钟点。')
        if precision == 'window':
            window = value.get('window')
            if window == 'calendar':
                end_day = _date(value.get('end_date'))
                if end_day <= day:
                    raise ArrangementTimeError('执行范围结束日期必须晚于起始日期。')
                result = _value(precision, day, raw, window=window, end_day=end_day)
            elif window not in WINDOWS:
                raise ArrangementTimeError('请明确上午、下午或晚上。')
            else:
                if value.get('end_date') is not None:
                    raise ArrangementTimeError('日内时段不能另存日历范围。')
                result = _value(precision, day, raw, window=window)
        else:
            if value.get('window') is not None:
                raise ArrangementTimeError('日期精度不能同时声明时段。')
            result = _value(precision, day, raw)
    if precision != 'window' and value.get('end_date') is not None:
        raise ArrangementTimeError('仅日历范围可以包含结束日期。')
    # Supplied projections must agree with the authoritative precision/date.
    for key in ('start_at', 'end_at'):
        if value.get(key) is not None and _point(value[key]) != result[key]:
            raise ArrangementTimeError('时间查询边界与时间内容不一致。')
    if value.get('boundary_source') not in (None, result['boundary_source']):
        raise ArrangementTimeError('时间边界来源与精度不一致。')
    return result


def _relative_period(text, today, role):
    matches = []
    for token, week in (('本周', 0), ('这周', 0), ('下周', 1)):
        if re.search(token + r'(?![一二三四五六日天])(?:内|末|底)?', text):
            if role != 'deadline':
                raise ArrangementTimeError('请明确这段时间中的哪一天。')
            matches.append(today + timedelta(days=6 - today.weekday() + week * 7))
    for token, increment in (('本月', 0), ('这个月', 0), ('下月', 1), ('下个月', 1)):
        if token in text:
            if role != 'deadline':
                raise ArrangementTimeError('请明确这个月中的哪一天。')
            target = _month(today.replace(day=1), increment)
            matches.append(target.replace(day=calendar.monthrange(target.year, target.month)[1]))
    if re.search(r'(?<!下)月底', text):
        matches.append(today.replace(day=calendar.monthrange(today.year, today.month)[1]))
    if '下月底' in text:
        target = _month(today.replace(day=1), 1)
        matches.append(target.replace(day=calendar.monthrange(target.year, target.month)[1]))
    for match in re.finditer(r'(' + NUMBER + r')天内', text):
        amount = number(match[1])
        if amount is None or not 0 <= amount <= 36600:
            raise ArrangementTimeError('天数超出支持范围。')
        matches.append(today + timedelta(days=amount))
    for match in re.finditer(r'((?:一个)|' + NUMBER + r')个月内', text):
        amount = number(match[1])
        if amount is None or not 0 <= amount <= 1200:
            raise ArrangementTimeError('月数超出支持范围。')
        matches.append(_month(today, amount))
    if len(set(matches)) > 1:
        raise ArrangementTimeError('这段话有多个时间范围，请明确本次使用哪一个。')
    return matches[0] if matches else None


def _execution_period(text, today):
    ranges = []
    for token, weeks in (('本周', 0), ('这周', 0), ('下周', 1)):
        if re.search(token + r'(?![一二三四五六日天])', text):
            start = today - timedelta(days=today.weekday()) + timedelta(days=weeks * 7)
            ranges.append((start, start + timedelta(days=7)))
    for token, months in (('本月', 0), ('这个月', 0), ('下月', 1), ('下个月', 1)):
        if re.search(token + r'(?![0-9零〇一二两三四五六七八九十百])', text):
            start = _month(today.replace(day=1), months)
            ranges.append((start, _month(start, 1)))
    if len(set(ranges)) > 1:
        raise ArrangementTimeError('有多个执行范围，请明确本次使用哪一个。')
    return ranges[0] if ranges else None


def normalize_time(value, submitted_at, *, role='deadline', base_date=None):
    """Return a canonical TimeSpec, or None for an explicit clear.

Callers preserve omitted fields by checking key membership before calling.
Roles only disambiguate natural calendar periods; they never schedule a task.
"""
    if role not in ('deadline', 'check', 'execution'):
        raise ArrangementTimeError('时间用途无效。')
    submitted_at = _timestamp(submitted_at)
    if base_date is not None:
        _date(base_date)
    if value is None:
        return None
    if isinstance(value, dict):
        return _structured(value)
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        at = _point(value)
        return _value('instant', datetime.fromtimestamp(at, SHANGHAI).date(), '', at=at)
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ArrangementTimeError('请提供可识别的日期、时段或具体时刻。')
    text = value.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:\d{2})', text):
        at = _point(text)
        return _value('instant', datetime.fromtimestamp(at, SHANGHAI).date(), text, at=at)
    if re.search(r'(?:上午|下午|晚上)前', text):
        raise ArrangementTimeError('“时段前”的含义不明确，请补充具体截止时刻。')
    if re.search(r'(?:全天|一整天)', text):
        raise ArrangementTimeError('全天要求应单独保存，本版不能转为定时安排。')
    today = datetime.fromtimestamp(submitted_at, SHANGHAI).date()
    if role == 'execution':
        execution_period = _execution_period(text, today)
        if execution_period:
            return _value('window', execution_period[0], text, window='calendar', end_day=execution_period[1])
    period = _relative_period(text, today, role)
    try:
        literal = day_from_text(text, submitted_at, base_date)
        day = _date(literal) if literal else None
        # A weekday and a Chinese clock are often adjacent. Never let the
        # numeric parser consume the weekday ("周五三点" -> "五三点").
        clock_text = re.sub(r'(?:下|本|这)?(?:周|星期)[一二三四五六日天]', ' ', text)
        # Noon is explicit in the product's deadline example; bare 1–11
        # o'clock still needs morning/afternoon confirmation.
        clock_text = re.sub(r'(上午|下午|晚上|早上|早晨|凌晨|中午|傍晚)?\s*(' + NUMBER + r')(?=点|时)',
            lambda match: '中午12' if not match[1] and number(match[2]) == 12 else match[0], clock_text)
        clock = clock_from_text(clock_text)
    except ValueError as error:
        raise ArrangementTimeError(str(error)) from None
    if period and day and period != day:
        raise ArrangementTimeError('时间范围与具体日期不一致，请核对。')
    day = day or period or (_date(base_date) if base_date else None)
    if day is None:
        raise ArrangementTimeError('日期尚不明确，请先补充哪一天。')
    if clock is not None:
        at = _local(day, *clock)
        return _value('instant', day, text, at=at)
    windows = {word for word in WINDOWS if word in text}
    if len(windows) > 1:
        raise ArrangementTimeError('有多个时段，请选择本次使用的时段。')
    if windows:
        return _value('window', day, text, window=windows.pop())
    return _value('date', day, text)


def time_start(value):
    return None if value is None else _structured(value)['start_at']


def time_end(value):
    """Exclusive expiry boundary; instant expires exactly at the given point."""
    return None if value is None else _structured(value)['end_at']


def time_signature(value):
    if value is None:
        return None
    normalized = _structured(value)
    # Wording changes do not manufacture a new milestone.
    stable = {key: normalized[key] for key in ('precision', 'date', 'timezone', 'at', 'start_at', 'end_at')}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
