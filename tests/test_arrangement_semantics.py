"""User language gives meaning and authority; models and materials do not."""
from datetime import datetime
import pytest
from secretary.store import SHANGHAI
from secretary.arrangement_semantics import checked_arrangement

NOW = datetime(2026, 10, 5, 18, tzinfo=SHANGHAI).timestamp()


def parse(text, raw=None, current=None, kind='user'):
    return checked_arrangement(raw or {'intent': 'plan', 'changes': {'activity': 'visit'}}, text, NOW, current, kind)


def test_two_dates_are_independent_and_parsed_at_submission():
    result = parse('本周定下来，下月去拜访')
    assert result['changes']['settle_deadline']['time_spec']['date'] == '2026-10-11'
    assert result['changes']['proposed_execution']['time_spec']['precision']=='window'
    assert result['changes']['decision_mode'] == 'external'


def test_execution_week_never_becomes_settlement_deadline():
    result = parse('本周去拜访客户')
    assert 'settle_deadline' not in result['changes']
    assert result['changes']['proposed_execution']['time_spec']['date']=='2026-10-05'
    assert result['changes']['proposed_execution']['time_spec']['end_date']=='2026-10-12'
    result = parse('本周约一下客户')
    assert '去见面' in result['issues'][0]


def test_check_and_deadline_are_not_interchanged():
    result = parse('最晚月底前定下来，周五再问')
    assert result['changes']['settle_deadline']['strength'] == 'required'
    assert result['changes']['settle_deadline']['time_spec']['date'] == '2026-10-31'
    assert result['changes']['next_check']['time_spec']['date'] == '2026-10-09'


@pytest.mark.parametrize('text,strength', [
    ('周四前定，但周五再问', 'target'),
    ('周四之前定，但周五再问', 'target'),
    ('周四必须定，周五上午再看', 'required'),
])
@pytest.mark.parametrize('model_role', ['offline', 'deadline', 'wrong_execution'])
def test_bare_settlement_verb_keeps_deadline_and_later_check_independent(text, strength, model_role):
    quote = text.split('，')[0]
    raw = {'intent': 'update', 'changes': {}}
    if model_role != 'offline':
        field = 'settle_deadline' if model_role == 'deadline' else 'proposed_execution'
        raw.update(arrangement={field: {'time_text': quote}}, arrangement_evidence={field: quote})
    result = parse(text, raw, current={'activity': 'visit'})
    assert result['changes']['settle_deadline']['time_spec']['date'] == '2026-10-08'
    assert result['changes']['settle_deadline']['strength'] == strength
    assert result['changes']['next_check']['time_spec']['date'] == '2026-10-09'
    assert 'proposed_execution' not in result['changes']


@pytest.mark.parametrize('text', ['周四去拜访', '把周四的培训定一下', '周四必须定价'])
def test_execution_day_and_compound_words_do_not_become_coordination_deadline(text):
    result = parse(text, current={'activity': 'visit'})
    assert 'settle_deadline' not in result['changes']
    assert result['changes']['proposed_execution']['time_spec']['date'] == '2026-10-08'


@pytest.mark.parametrize('kind,text', [('recording', '本周定下来，已经约好周五下午三点'),
                                      ('user', '如果本周定下来，就安排周五下午三点'),
                                      ('user', '他说周五下午三点已经约好了')])
def test_reference_and_hypothetical_cannot_grant_authority(kind, text):
    result = parse(text, kind=kind)
    assert result['changes'] == {}


def test_model_cannot_forge_time_evidence_or_authority():
    result = parse('客户情况需要了解', {'intent': 'plan', 'arrangement': {
        'proposed_execution': {'time_text': '明天下午三点'},
        'application_authority': {'kind': 'direct_user'}},
        'arrangement_evidence': {'proposed_execution': '明天下午三点', 'application_authority': '客户情况需要了解'}})
    assert 'proposed_execution' not in result['changes']
    assert not result['changes'].get('application_authority')


def test_candidacy_never_withdraws_effective_schedule():
    assert parse('改期，时间还没定', current={'title': '会面'})['operation'] == 'start_reschedule'
    assert not parse('可能改周五，等答复', current={'title': '会面'})['withdraw_explicit']
    result = parse('原时间不去了，新时间还没定', current={'title': '会面'})
    assert result['operation'] == 'withdraw_execution' and result['withdraw_explicit']


def test_waiting_does_not_claim_check_performed():
    assert not parse('还在等回复', current={'title': '会面'})['check_handled']
    assert parse('问过仍没回，周五再问', current={'title': '会面'})['check_handled']


def test_review_request_overrides_direct_language():
    result = parse('安排周五下午三点，先给我确认')
    assert result['changes']['application_authority']['kind'] == 'none'


def test_ambiguous_clock_is_preserved_as_one_question():
    result = parse('周五三点再问')
    assert 'next_check' not in result['changes']
    assert len(result['issues']) == 1 and '上午还是下午' in result['issues'][0]


def test_model_datetime_does_not_replace_verbatim_time():
    result = parse('周五下午三点打电话', {'intent': 'plan', 'changes': {'activity': 'call'},
        'arrangement': {'proposed_execution': {'time_text': '周五下午三点', 'at': 1}},
        'arrangement_evidence': {'proposed_execution': '周五下午三点'}})
    assert result['changes']['proposed_execution']['time_spec']['date'] == '2026-10-09'
    assert result['changes']['proposed_execution']['time_spec']['precision'] == 'instant'
    assert result['changes']['decision_mode'] == 'self'


@pytest.mark.parametrize('text', ['今天客户说：取消这次活动。先作为沟通记录。',
                                 '不是取消这次活动，按原时间继续。',
                                 '我不想暂停协调，继续推进。'])
def test_reported_or_negated_operation_never_changes_coordination(text):
    result = parse(text, {'intent':'update','changes':{}}, current={'title':'会面'})
    assert result['operation'] is None


def test_current_instruction_after_report_has_its_own_authority():
    result = parse('客户说已同意周五下午三点，请安排周五下午三点的会面',
                   {'intent':'update','changes':{}}, current={'title':'会面'})
    assert not result['reference_only']
    assert result['changes']['application_authority']['kind'] == 'direct_user'


def test_reported_date_does_not_confirm_current_candidate_clock():
    result = parse('对方已确定12号，几点没定', {'intent':'update','changes':{}},
                   current={'date':'2026-10-12', 'proposed_execution':{'time_spec':{'precision':'instant'}}})
    assert result['changes']['agreement']['scope'] == 'date_only'


def test_date_agreement_survives_separate_undecided_clock_clause():
    result = parse('对方已确定12号，钟点还没定', {'intent':'update','changes':{}},
                   current={'date':'2026-10-12'})
    assert result['changes']['agreement']['scope'] == 'date_only'
    assert not parse('对方并非已确定12号', {'intent':'update','changes':{}}, current={'date':'2026-10-12'})['changes'].get('agreement')


def test_explicit_self_action_can_replace_external_activity_mode():
    result = parse('安排11月12日下午三点给赵工打电话，不用提醒', {'intent':'update','changes':{}},
                   current={'activity':'visit','date':'2026-11-12'})
    assert result['changes']['decision_mode']=='self'


def test_unambiguous_twenty_four_hour_clock_is_full_agreement():
    result=parse('对方已确认12号15点，按这个安排', {'intent':'update','changes':{}},
                 current={'activity':'meal','date':'2026-11-12','settlement_scope':'date_only'})
    assert result['changes']['agreement']['scope']=='execution_time'
    assert result['changes']['settlement_scope']=='execution_time'


@pytest.mark.parametrize('text',['主要聊如何打电话争取反馈','讨论打电话的销售话术','不要给赵工打电话'])
def test_discussing_or_negating_a_call_does_not_change_external_decision_mode(text):
    result=parse(text, {'intent':'update','changes':{}}, current={'activity':'meal','date':'2026-10-12'})
    assert result['changes'].get('decision_mode') != 'self'
