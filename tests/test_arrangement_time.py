"""Calendar precision is independent of an activity's execution or reminder."""
from datetime import datetime

import pytest

from secretary.arrangement_time import normalize_time, time_start, time_end, time_signature, ArrangementTimeError
from secretary.store import SHANGHAI


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp()


SUBMITTED = stamp('2026-10-05T23:59:00')


@pytest.mark.parametrize('text,expected', [
    ('本周内', '2026-10-11'), ('下周', '2026-10-18'), ('本月内', '2026-10-31'),
    ('下月底', '2026-11-30'), ('下个月内', '2026-11-30'), ('月底前定下来', '2026-10-31'),
    ('3天内', '2026-10-08'), ('十天内', '2026-10-15'), ('一个月内', '2026-11-05'),
    ('10月8日', '2026-10-08'), ('明天', '2026-10-06'), ('周五再问', '2026-10-09'),
])
def test_relative_times_use_original_submission_calendar(text, expected):
    value = normalize_time(text, SUBMITTED, role='check' if '再问' in text else 'deadline')
    assert value['date'] == expected and value['precision'] == 'date'
    assert value['raw_text'] == text and value['timezone'] == 'Asia/Shanghai'
    assert value['at'] is None and value['boundary_source'] == 'policy'


@pytest.mark.parametrize('submitted,expected', [
    ('2026-01-31T12:00:00', '2026-02-28'), ('2028-01-31T12:00:00', '2028-02-29'),
    ('2026-12-31T12:00:00', '2027-01-31'),
])
def test_rolling_calendar_month_clamps_last_day(submitted, expected):
    assert normalize_time('一个月内', stamp(submitted))['date'] == expected


def test_date_boundary_is_next_day_not_2359_or_execution_clock():
    deadline = normalize_time('2026-10-08', SUBMITTED)
    assert time_start(deadline) == stamp('2026-10-08T00:00:00')
    assert time_end(deadline) == stamp('2026-10-09T00:00:00')
    assert stamp('2026-10-08T23:59:59') < time_end(deadline)
    assert normalize_time('2026-10-08T23:59:00+08:00', SUBMITTED)['precision'] == 'instant'


@pytest.mark.parametrize('period,start,end', [('上午', 9, 12), ('下午', 14, 18), ('晚上', 19, 22)])
def test_window_policy_keeps_precision_without_invented_user_clock(period, start, end):
    value = normalize_time('周五' + period + '再问', SUBMITTED, role='check')
    assert value['precision'] == 'window' and value['window'] == period
    assert value['at'] is None and value['boundary_source'] == 'policy'
    assert time_start(value) == stamp('2026-10-09T' + str(start).zfill(2) + ':00:00')
    assert time_end(value) == stamp('2026-10-09T' + str(end).zfill(2) + ':00:00')
    assert time_start(value) < stamp('2026-10-09T' + str(start + 1).zfill(2) + ':00:00') < time_end(value)


def test_instant_deadline_expires_exactly_at_noon():
    value = normalize_time('周三12点前定', SUBMITTED)
    assert value['precision'] == 'instant' and value['boundary_source'] == 'explicit'
    assert time_start(value) == time_end(value) == stamp('2026-10-07T12:00:00')


def test_adjacent_weekday_and_clock_does_not_swallow_weekday_number():
    with pytest.raises(ValueError, match='上午还是下午'):
        normalize_time('周五三点再问', SUBMITTED, role='check')
    assert normalize_time('周五下午三点再问', SUBMITTED, role='check')['date'] == '2026-10-09'
    assert normalize_time('周五凌晨十二点再问', SUBMITTED, role='check')['at'] == stamp('2026-10-09T00:00:00')


def test_past_deadline_remains_a_value_and_role_does_not_overwrite_other_time():
    deadline = normalize_time('2026-10-01', SUBMITTED)
    check = normalize_time('周五上午再问', SUBMITTED, role='check')
    execution = normalize_time('2026-11-02T15:00:00+08:00', SUBMITTED, role='execution')
    assert time_end(deadline) < time_start(check) < time_start(execution)
    assert deadline['date'] == '2026-10-01' and execution['date'] == '2026-11-02'


def test_bare_clock_only_uses_explicit_existing_date():
    value = normalize_time('下午三点', SUBMITTED, role='execution', base_date='2026-10-08')
    assert value['at'] == stamp('2026-10-08T15:00:00')
    with pytest.raises(ArrangementTimeError):
        normalize_time('下午三点', SUBMITTED, role='execution')


def test_signature_ignores_wording_and_read_helpers_never_mutate():
    first = normalize_time('周五再问', SUBMITTED, role='check')
    other = normalize_time({'precision': 'date', 'date': '2026-10-09', 'raw_text': '星期五'}, SUBMITTED)
    before = dict(first)
    assert time_signature(first) == time_signature(other)
    assert normalize_time(first, SUBMITTED) == first
    assert first == before
    assert time_signature(normalize_time('周五上午', SUBMITTED, role='check')) != time_signature(first)
    assert normalize_time(None, SUBMITTED) is None
    assert time_start(None) is time_end(None) is time_signature(None) is None


@pytest.mark.parametrize('value', [
    True, '', '三点', '周三上午前定', '2026-02-30', '2026-10-08下午三点和晚上六点',
    '本周内或下月内', '10月8日全天', {'precision': 'date', 'date': '2026-10-08', 'at': SUBMITTED},
    {'precision': 'instant', 'at': '2026-10-08T15:00:00'},
    {'precision': 'window', 'date': '2026-10-08', 'window': '上午', 'start_at': SUBMITTED},
    {'precision': 'date', 'date': '2026-10-08', 'timezone': 'UTC'},
    {'precision': 'date', 'date': '2026-10-08', 'hidden_field': 1},
])
def test_ambiguity_illegal_fields_and_inconsistent_projections_are_rejected(value):
    with pytest.raises(ArrangementTimeError):
        normalize_time(value, SUBMITTED)


def test_month_or_week_execution_preserves_range_but_check_needs_a_day():
    for text in ('下周', '下个月'):
        with pytest.raises(ArrangementTimeError):
            normalize_time(text, SUBMITTED, role='check')
        value = normalize_time(text, SUBMITTED, role='execution')
        assert value['precision'] == 'window' and value['window'] == 'calendar'
        assert value['at'] is None and value['boundary_source'] == 'policy'


@pytest.mark.parametrize('text,start,end', [
    ('本周去拜访', '2026-10-05', '2026-10-12'), ('下周', '2026-10-12', '2026-10-19'),
    ('本月', '2026-10-01', '2026-11-01'), ('下个月培训', '2026-11-01', '2026-12-01'),
])
def test_calendar_execution_range_has_exclusive_boundaries(text, start, end):
    value = normalize_time(text, SUBMITTED, role='execution')
    assert value['date'] == start and value['end_date'] == end
    assert time_start(value) == stamp(start + 'T00:00:00')
    assert time_end(value) == stamp(end + 'T00:00:00')
    assert normalize_time(value, SUBMITTED, role='execution') == value


def test_calendar_range_signature_changes_at_end_and_validates_projection():
    value = normalize_time('本周', SUBMITTED, role='execution')
    altered = {key: field for key, field in value.items() if key not in ('start_at', 'end_at')}
    altered['end_date'] = '2026-10-13'
    assert time_signature(value) != time_signature(altered)
    with pytest.raises(ArrangementTimeError):
        normalize_time({**value, 'end_at': value['start_at']}, SUBMITTED, role='execution')
    with pytest.raises(ArrangementTimeError):
        normalize_time({**altered, 'end_date': altered['date']}, SUBMITTED, role='execution')
