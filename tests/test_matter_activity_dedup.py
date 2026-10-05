"""The same occurrence has one plan; preparation remains a separate step."""
import asyncio
import json
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.matters import MatterService
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.matter_flow_integration import _represented_activity, apply_decision
from secretary.store import SHANGHAI
from tests.test_secretary_flow import ScriptedInterpreter, NOW, initial

OWNER = 'fictional-activity-owner'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-activity.sqlite3')
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    service = MatterService(crm, clock=lambda: NOW)
    flow = SecretaryFlow(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    flow.matters = service
    crm.secretary_flow = flow
    customer = crm.create_customer(OWNER, {'name': '示例产品单位（虚构）'}, NOW)
    matter = service.create(OWNER, {'title': '推进项目方案', 'customer_id': customer['id'], 'request_id': 'goal'})['matter']
    yield {'crm': crm, 'service': service, 'flow': flow, 'matter': matter}
    crm.close()


class Router:
    def __init__(self, service, matter_id, actions):
        self.service, self.matter_id, self.actions = service, matter_id, actions

    async def route(self, owner, *args, **kwargs):
        matter = self.service.get(owner, self.matter_id)
        return {'kind': 'existing', 'matter_id': matter['id'], 'base_revision': matter['revision'], 'actions': self.actions}


def run_turn(w, text, actions, answer=None):
    flow, matter = w['flow'], w['service'].get(OWNER, w['matter']['id'])
    flow.interpreter = ScriptedInterpreter([answer or initial()])
    flow.matter_router = Router(w['service'], matter['id'], actions)
    turn = flow.submit(OWNER, {'text': text, 'matter_id': matter['id'], 'matter_revision': matter['revision'], 'request_id': 'utterance'})
    assert asyncio.run(flow.process_one())
    result = flow.turn(OWNER, turn['id'])
    assert result['status'] == 'done', result.get('error')
    return result


def value(title, evidence=None, **extra):
    return {'title': title, 'content': evidence or title, 'evidence': evidence or title, 'status': 'following', **extra}


def test_real_date_only_meal_flow_has_plan_without_duplicate_action(world):
    text = '10月8日约林博士吃饭'
    result = run_turn(world, text, [value(text)])
    matter = world['service'].get(OWNER, world['matter']['id'])
    assert len(matter['plans']) == 1 and matter['plans'][0]['date'] == '2026-10-08'
    assert matter['plans'][0]['start_at'] is None
    assert matter['actions'] == [] and result['matter_route']['changes'] == []
    assert world['crm'].get_record(OWNER, result['record_id'])['original_content'] == text
    assert world['crm']._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0


@pytest.mark.parametrize('text,activity,title,activity_quote', [
    ('10月8日拜访林博士', 'visit', '拜访林博士', '拜访'),
    ('10月8日给林博士打电话', 'call', '给林博士打电话', '打电话'),
])
def test_real_visit_and_phone_activity_have_one_representation(world, text, activity, title, activity_quote):
    answer = {'intent': 'plan', 'changes': {'title': title, 'person': '林博士', 'date': '2026-10-08', 'activity': activity},
        'evidence': {'person': '林博士', 'date': '10月8日', 'activity': activity_quote}}
    result = run_turn(world, text, [value(text)], answer)
    matter = world['service'].get(OWNER, world['matter']['id'])
    assert len(matter['plans']) == 1 and matter['actions'] == []
    assert result['matter_route']['changes'] == []


def test_same_utterance_preserves_material_preparation_while_meal_stays_plan(world):
    text = '先准备电力方案PPT，10月8日约林博士吃饭'
    result = run_turn(world, text, [value('准备电力方案PPT', '准备电力方案PPT'), value('10月8日约林博士吃饭', text)])
    matter = world['service'].get(OWNER, world['matter']['id'])
    assert len(matter['plans']) == 1
    assert [action['title'] for action in matter['actions']] == ['准备电力方案PPT']
    assert len(result['matter_route']['changes']) == 1
    assert world['crm'].get_record(OWNER, result['record_id'])['original_content'] == text


def test_preparation_with_event_keyword_is_a_real_step(world):
    text = '准备拜访资料，10月8日约林博士吃饭'
    result = run_turn(world, text, [value('准备拜访资料', '准备拜访资料'), value('10月8日约林博士吃饭')])
    assert [action['title'] for action in world['service'].get(OWNER, world['matter']['id'])['actions']] == ['准备拜访资料']


def plan_row(w, *, activity='meal', person='林博士', date='2026-10-08', clock=None, status='preparing'):
    source = w['crm'].create_record(OWNER, {'title': '计划依据', 'content': '计划依据'}, NOW)
    data = {'title': '与林博士吃饭', 'person': person, 'activity': activity, 'date': date,
            'start_at': clock, 'status': status}
    with w['crm']._transaction() as db:
        identifier = db.execute('INSERT INTO crm_secretary_plans(owner,record_id,visit_id,data_json,created_at,updated_at) VALUES(?,?,NULL,?,?,?)',
            (OWNER, source['id'], json.dumps(data), NOW, NOW)).lastrowid
    return identifier


@pytest.mark.parametrize('title,evidence,activity,person,date,clock,expected', [
    ('10月8日约林博士吃饭', '10月8日约林博士吃饭', 'meal', '林博士', '2026-10-08', None, True),
    ('10月8日约杨工吃饭', '10月8日约杨工吃饭', 'meal', '林博士', '2026-10-08', None, False),
    ('10月9日约林博士吃饭', '10月9日约林博士吃饭', 'meal', '林博士', '2026-10-08', None, False),
    ('10月8日给林博士打电话', '10月8日给林博士打电话', 'meal', '林博士', '2026-10-08', None, False),
    ('整理林博士的汇报材料', '整理林博士的汇报材料，10月8日约林博士吃饭', 'meal', '林博士', '2026-10-08', None, False),
    ('电话前准备PPT', '电话前准备PPT，10月8日给林博士打电话', 'call', '林博士', '2026-10-08', None, False),
    ('准备拜访林博士的资料', '准备拜访林博士的资料，10月8日拜访林博士', 'visit', '林博士', '2026-10-08', None, False),
    ('约林博士吃饭', '10月8日约林博士吃饭，10月9日约林博士吃饭', 'meal', '林博士', '2026-10-08', None, False),
    ('10月8日下午三点约林博士吃饭', '10月8日下午三点约林博士吃饭', 'meal', '林博士', '2026-10-08', None, False),
])
def test_occurrence_match_requires_evidence_not_shared_customer(world, title, evidence, activity, person, date, clock, expected):
    identifier = plan_row(world, activity=activity, person=person, date=date, clock=clock)
    assert _represented_activity(world['crm']._db, OWNER, identifier, title, evidence, NOW) is expected


def test_different_clock_is_a_different_occurrence_and_matching_clock_is_represented(world):
    start = datetime(2026, 10, 8, 15, tzinfo=SHANGHAI).timestamp()
    identifier = plan_row(world, clock=start)
    assert _represented_activity(world['crm']._db, OWNER, identifier, '10月8日下午三点约林博士吃饭', '10月8日下午三点约林博士吃饭', NOW)
    assert not _represented_activity(world['crm']._db, OWNER, identifier, '10月8日晚上六点约林博士吃饭', '10月8日晚上六点约林博士吃饭', NOW)


def test_no_plan_or_foreign_plan_cannot_suppress_a_user_action(world):
    identifier = plan_row(world)
    db = world['crm']._db
    assert not _represented_activity(db, OWNER, None, '约林博士吃饭', '约林博士吃饭', NOW)
    assert not _represented_activity(db, 'other-owner', identifier, '约林博士吃饭', '约林博士吃饭', NOW)


@pytest.mark.parametrize('status', ['cancelled', 'recapped'])
def test_finished_occurrence_does_not_suppress_a_new_invitation(world, status):
    identifier = plan_row(world, status=status)
    assert not _represented_activity(world['crm']._db, OWNER, identifier, '10月8日约林博士吃饭', '10月8日约林博士吃饭', NOW)


def test_existing_step_progress_is_preserved_despite_same_named_plan(world):
    crm, service = world['crm'], world['service']; text = '10月8日约林博士吃饭'
    source = crm.create_record(OWNER, {'title': text, 'content': text}, NOW)
    action = crm.create_record(OWNER, {'title': text, 'content': '之前明确采纳的邀约步骤', 'kind': 'action', 'status': 'following'}, NOW)
    service.attach(OWNER, world['matter']['id'], 'record', action['id'], role='action')
    identifier = plan_row(world)
    matter = service.get(OWNER, world['matter']['id'])
    turn = {'owner': OWNER, 'id': 99, 'record_id': source['id'], 'source_kind': 'user'}
    decision = {'kind': 'existing', 'matter_id': matter['id'], 'base_revision': matter['revision'],
        'actions': [value(text, text, existing_record_id=action['id'])]}
    with crm._transaction() as db:
        result = apply_decision(service, db, turn, decision, text=text, now=NOW, plan_id=identifier)
    assert result['changes'][0]['change'] == 'updated'
    assert service.get(OWNER, matter['id'])['action_count'] == 1
    assert len(crm.record_detail(OWNER, action['id'])['activities']) == 1


def test_activity_word_without_activity_plan_does_not_get_dropped(world):
    text = '研究一下怎么约林博士吃饭'
    result = run_turn(world, text, [value(text)], {'intent': 'note', 'changes': {}})
    assert result['plan_id'] is None
    assert [action['title'] for action in world['service'].get(OWNER, world['matter']['id'])['actions']] == [text]


@pytest.mark.parametrize('text,activity', [
    ('10月8日约林博士吃饭', 'meal'),
    ('10月8日给林博士打电话确认预算', 'call'),
    ('10月8日拜访林博士', 'visit'),
])
def test_note_model_fallback_identifies_one_current_activity_without_guessing_clock(world, text, activity):
    result = run_turn(world, text, [value(text)], {'intent': 'note', 'changes': {}})
    matter = world['service'].get(OWNER, world['matter']['id'])
    assert result['plan_id'] is not None and len(matter['plans']) == 1
    assert matter['plans'][0]['activity'] == activity
    assert matter['plans'][0]['date'] == '2026-10-08' and matter['plans'][0]['start_at'] is None
    assert matter['plans'][0]['arrangement']['settling_state'] == 'pending'
    assert matter['actions'] == []
    assert world['crm']._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert world['crm']._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    assert world['crm'].get_record(OWNER, result['record_id'])['original_content'] == text


def test_phone_with_conversation_goal_is_still_the_same_activity(world):
    identifier = plan_row(world, activity='call')
    text = '10月8日给林博士打电话确认预算'
    assert _represented_activity(world['crm']._db, OWNER, identifier, text, text, NOW)
    preparation = '电话前确认预算资料'
    assert not _represented_activity(world['crm']._db, OWNER, identifier, preparation, preparation + '，' + text, NOW)


@pytest.mark.parametrize('title', ['10月8日前约林博士吃饭', '明天之前约林博士吃饭', '最迟10月8日约林博士吃饭'])
def test_invitation_deadline_is_not_the_actual_meal_date(world, title):
    identifier = plan_row(world)
    assert not _represented_activity(world['crm']._db, OWNER, identifier, title, title, NOW)


def test_phone_to_confirm_a_meal_is_a_separate_preparation_action(world):
    identifier = plan_row(world)
    title = '打电话向林博士确认10月8日饭局'
    assert not _represented_activity(world['crm']._db, OWNER, identifier, title, title, NOW)
