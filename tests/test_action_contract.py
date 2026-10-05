from datetime import datetime

from secretary.action_contract import TERM_FIELDS, can_schedule, derive_terms


def action(quote, **changes):
    return {'title': '发送方案', 'kind': 'commitment', 'owner_hint': '我',
            'evidence': quote, 'remind_at': 1800000000, **changes}


def test_source_terms_preserve_explicit_duration_and_self_execution():
    quote = '我答应明天下午三点发送方案，预计60分钟'
    terms = derive_terms(action(quote), quote)
    assert set(terms) == TERM_FIELDS
    assert terms['executor_kind'] == 'self'
    assert terms['duration_minutes'] == 60
    assert terms['execution_at'] == 1800000000
    assert can_schedule(terms)


def test_other_actions_duration_and_unquoted_metadata_stay_unset():
    quote = '我答应发送方案。会议预计60分钟'
    terms = derive_terms(action(quote, duration_minutes=60, duration_evidence='预计60分钟'), quote)
    assert terms['duration_minutes'] is None
    forged = derive_terms(action('我答应发送方案', executor_kind='customer'), '我答应发送方案')
    assert forged['executor_kind'] == 'unknown'
    assert derive_terms(action('我答应发送方案'), '客户没有答应发送方案')['execution_at'] is None
    quote = '我答应发送方案，会议预计60分钟'
    assert derive_terms(action(quote, duration_minutes=60, duration_evidence='预计60分钟'), quote)['duration_minutes'] is None
    quote = '我答应两小时后发送方案'
    assert derive_terms(action(quote), quote)['duration_minutes'] is None
    quote = '我答应发送方案，明天下午三点开会，预计60分钟'
    terms = derive_terms(action(quote, execution_at=1800000000, execution_evidence='明天下午三点',
        duration_minutes=60, duration_evidence='预计60分钟'), quote)
    assert terms['duration_minutes'] is None and terms['execution_at'] is None


def test_executor_uses_source_relationship_not_data_owner_or_name():
    for quote, kind in [('客户王工答应发送方案', 'customer'), ('内部同事小王负责发送方案', 'team'),
                        ('王工答应发送方案', 'unknown')]:
        terms = derive_terms(action(quote, owner_hint='王工', owner='alice'), quote)
        assert terms['executor_kind'] == kind
        assert not can_schedule(terms)


def test_missing_old_fields_remain_unknown_and_duration_default_is_not_fact():
    terms = derive_terms({'title': '发送方案', 'kind': 'commitment', 'remind_at': 1800000000})
    assert terms['executor_kind'] == 'unknown'
    assert terms['duration_minutes'] is None
    assert terms['execution_at'] is None
    terms = derive_terms(action('我答应让王工发送方案'), '我答应让王工发送方案')
    assert terms['executor_kind'] == 'unknown'


def test_deadline_does_not_allocate_execution_time_and_clock_evidence_is_local():
    quote = '我答应周五下午三点之前发送方案'
    terms = derive_terms(action(quote, execution_at=1800000000, execution_evidence='周五下午三点',
                                deadline_at=1800000000, deadline_evidence='周五下午三点'), quote)
    assert terms['execution_at'] is None
    assert terms['deadline_at'] == 1800000000
    assert not can_schedule(terms)


def test_chinese_hour_duration_and_invalid_number_do_not_silently_default():
    quote = '我答应发送方案，预计一个小时'
    assert derive_terms(action(quote), quote)['duration_minutes'] == 60
    assert derive_terms(action(quote, duration_minutes=True), quote)['duration_minutes'] is None
    assert derive_terms(action('我答应发送方案，预计1分钟'))['duration_minutes'] is None


def test_date_only_deadline_and_check_preserve_date_without_a_clock():
    now = datetime.fromisoformat('2026-10-03T10:00:00+08:00').timestamp()
    quote = '我答应10月12日前发送方案'
    terms = derive_terms(action(quote, remind_at=None, deadline_date='2026-10-12', deadline_evidence='10月12日前'), quote, now)
    assert terms['deadline_date'] == '2026-10-12'
    assert terms['deadline_at'] is None and terms['execution_at'] is None
    assert not can_schedule(terms)
    quote = '我答应明天检查是否发送方案'
    terms = derive_terms(action(quote, remind_at=None, check_date='2026-10-04', check_evidence='明天检查'), quote, now)
    assert terms['check_date'] == '2026-10-04'
    assert terms['check_at'] is None and terms['execution_at'] is None
    assert derive_terms({**action(quote, remind_at=None), **terms}, quote)['check_date'] == '2026-10-04'
    assert derive_terms(action(quote, check_date='2026-10-05', check_evidence='明天检查'), quote, now)['check_date'] is None


def test_two_different_time_roles_can_coexist_without_borrowing_deadline_clock():
    quote = '我答应明天下午三点发送方案，周五前交付'
    terms = derive_terms(action(quote, execution_at=1800000000, execution_evidence='明天下午三点',
        deadline_evidence='周五前'), quote, datetime.fromisoformat('2026-10-03T10:00:00+08:00').timestamp())
    assert terms['execution_at'] == 1800000000
    assert terms['deadline_date'] == '2026-10-09'


def test_legacy_source_check_time_is_not_an_execution_capacity_block():
    quote = '我答应明天下午三点核实是否收到方案'
    terms = derive_terms({'title': '核实是否收到方案', 'kind': 'commitment', 'owner_hint': '我',
        'evidence': quote, 'time_evidence': '明天下午三点', 'remind_at': 1800000000}, quote)
    assert terms['check_at'] == 1800000000
    assert terms['execution_at'] is None
    assert not can_schedule(terms)


def test_before_and_latest_deadline_words_cannot_be_execution_time():
    for quote in ('我答应明天下午三点之前发送方案', '我答应最迟明天下午三点发送方案'):
        terms = derive_terms(action(quote, execution_at=1800000000, execution_evidence='明天下午三点'), quote)
        assert terms['execution_at'] is None
