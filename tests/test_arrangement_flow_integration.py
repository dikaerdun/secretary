"""Real conversation pipeline against disposable records, never formal data."""
import asyncio
from datetime import datetime
import pytest
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI
from secretary.secretary_interpreter import SecretaryInterpreter

NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()
OWNER = 'fictional-arrangement-owner'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path/'arrangement.sqlite3')
    current = [NOW]
    flow = SecretaryFlow(crm, SalesWorkspace(crm), asyncio.Lock(), clock=lambda:current[0])
    yield crm, flow, current
    flow.close()
    crm.close()


def say(flow, text, key, plan=None, **extra):
    body = {'text':text, 'request_id':key, **extra}
    if plan:
        body.update(plan_id=plan['id'], expected_revision=plan['revision'])
    turn = flow.submit(OWNER, body)
    assert asyncio.run(flow.process_one())
    result = flow.turn(OWNER, turn['id'])
    assert result['status'] == 'done', result.get('error')
    return result


def test_deadline_check_and_actual_activity_stay_distinct(world):
    crm, flow, _ = world
    result = say(flow, '本周定下来，下月去拜访客户，周五再问', 'three-times')
    plan = result['plan']
    arrangement = plan['arrangement']
    assert arrangement['settle_deadline']['time_spec']['date'] == '2026-10-11'
    assert arrangement['next_check']['time_spec']['date'] == '2026-10-09'
    execution = arrangement['proposed_execution']['time_spec']
    assert execution['precision']=='window' and execution['window']=='calendar'
    assert execution['date']=='2026-11-01' and execution['end_date']=='2026-12-01'
    assert arrangement['settling_state'] == 'pending'
    assert arrangement['followup_enabled']
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert flow.arrangements.list(OWNER)['total'] == 1


@pytest.mark.parametrize('text,strength', [
    ('周四前定，但周五再问', 'target'),
    ('周四之前定，但周五再问', 'target'),
    ('周四必须定，周五上午再看', 'required'),
])
@pytest.mark.parametrize('model_role', ['offline', 'deadline', 'wrong_execution'])
def test_bare_settlement_updates_deadline_keeps_future_activity_and_flags_later_check(world, text, strength, model_role):
    crm, flow, _ = world
    original = say(flow, '11月18日约赵工吃饭', 'future-meeting')['plan']
    quote = text.split('，')[0]
    if model_role != 'offline':
        class Proposal:
            async def interpret(self, *args):
                field = 'settle_deadline' if model_role == 'deadline' else 'proposed_execution'
                return {'intent': 'update', 'changes': {'date': '2026-10-08'},
                        'evidence': {'date': quote},
                        'arrangement': {field: {'time_text': quote}},
                        'arrangement_evidence': {field: quote}}
        flow.interpreter = Proposal()
    result = say(flow, text, 'settlement-only', original)
    plan = result['plan']
    assert plan['id'] == original['id']
    assert plan['arrangement']['proposed_execution'] == original['arrangement']['proposed_execution']
    assert plan['arrangement']['settle_deadline']['time_spec']['date'] == '2026-10-08'
    assert plan['arrangement']['settle_deadline']['strength'] == strength
    assert plan['arrangement']['next_check']['time_spec']['date'] == '2026-10-09'
    assert 'check_after_deadline' in plan['arrangement']['attention_flags']
    assert '下一推进点晚于确定期限，两项选择已保留' in result['reply']
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    record = crm._db.execute('SELECT content,original_content FROM crm_records WHERE id=?', (result['record_id'],)).fetchone()
    assert record['content'] == text and record['original_content'] == text


def test_full_self_arrangement_needs_no_customer_goal_or_default_reminder(world):
    crm, flow, _ = world
    result = say(flow, '安排10月8日下午三点给赵工打电话，不用提醒', 'self')
    plan = result['plan']
    assert plan['arrangement']['settling_state'] == 'settled'
    assert plan['active_schedule']
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert crm._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    assert not result['question']


def test_external_confirmation_applies_same_plan_without_extra_activity(world):
    crm, flow, _ = world
    first = say(flow, '10月8日约赵工吃饭', 'initial')['plan']
    second = say(flow, '主要聊密码方案，晚上六点，已经约好了，提前一个小时提醒', 'answer', first)['plan']
    assert second['id'] == first['id']
    assert second['arrangement']['settling_state'] == 'settled'
    assert second['active_schedule']
    assert second['reminder_at'] == second['start_at'] - 3600
    assert crm._db.execute('SELECT count(*) FROM crm_secretary_plans').fetchone()[0] == 1


def test_tentative_reschedule_preserves_old_then_explicit_withdrawal_cancels(world):
    crm, flow, _ = world
    first = say(flow, '安排10月8日下午三点给赵工打电话，提前一个小时提醒', 'self')['plan']
    task_id = first['task_id']
    second = say(flow, '改期，时间还没定', 'tentative', first)['plan']
    assert second['arrangement']['settling_state'] == 'pending'
    assert second['task_id'] == task_id and second['active_schedule']['id'] == task_id
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='queued'").fetchone()[0] == 1
    third = say(flow, '原时间不去了，新时间还没定', 'withdraw', second)['plan']
    assert not third['active_schedule'] and third['task_id'] == task_id
    assert third['arrangement']['settling_state'] == 'pending'
    assert crm._db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()[0] == 'cancelled'
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE task_id=? AND status='queued'",(task_id,)).fetchone()[0] == 0


def test_relative_dates_resolve_when_submitted_not_when_processed(world):
    _, flow, current = world
    turn = flow.submit(OWNER, {'text':'安排明天下午三点给赵工打电话', 'request_id':'delayed'})
    current[0] += 86400
    assert asyncio.run(flow.process_one())
    result = flow.turn(OWNER, turn['id'])
    assert result['status'] == 'done'
    assert result['plan']['arrangement']['proposed_execution']['time_spec']['date'] == '2026-10-06'


def test_model_failure_releases_only_its_own_hold_and_preserves_plan(world):
    crm, flow, _ = world
    plan = say(flow, '本周定下来，下月去拜访客户，明天再问', 'initial')['plan']
    class Failed:
        async def interpret(self, *args):
            raise RuntimeError('synthetic unavailable')
    flow.interpreter = Failed()
    turn = flow.submit(OWNER, {'text':'问过了还没回', 'request_id':'failed', 'plan_id':plan['id'], 'expected_revision':plan['revision']})
    hold = crm._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?',(plan['id'],)).fetchone()
    assert hold['followup_dirty'] and hold['hold_turn_id'] == turn['id']
    asyncio.run(flow.process_one())
    assert flow.turn(OWNER, turn['id'])['status'] == 'failed'
    hold = crm._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?',(plan['id'],)).fetchone()
    assert not hold['followup_dirty'] and hold['revision'] == plan['revision']


def test_settings_auto_check_off_and_explicit_check_still_works(world):
    _, flow, _ = world
    assert not flow.settings(OWNER)['arrangement_auto_check']
    no_check = say(flow, '本周定下来，下月去拜访客户', 'off')['plan']['arrangement']
    assert no_check['next_check'] is None and no_check['suggested_next_check']
    explicit = say(flow, '本周定下来，下月去拜访客户，明天再问', 'explicit')['plan']['arrangement']
    assert explicit['next_check']['origin'] == 'user'


def test_extending_settlement_deadline_does_not_move_activity_or_existing_check(world):
    crm, flow, _ = world
    original = say(flow, '本周定下来，下月去拜访客户，周五再问', 'initial-deadline')['plan']
    changed = say(flow, '延到下月底定下来', 'extended-deadline', original)['plan']
    assert changed['id'] == original['id']
    assert changed['arrangement']['settle_deadline']['time_spec']['date'] == '2026-11-30'
    for field in ('proposed_execution', 'candidates', 'next_check', 'settling_cycle_id'):
        assert changed['arrangement'][field] == original['arrangement'][field]
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0


def test_current_user_instruction_after_report_applies_but_plain_report_cannot(world):
    crm, flow, _ = world
    result = say(flow, '客户说10月8日下午三点已经约好了，请帮我加入日程', 'reported-current')
    assert result['plan']['active_schedule'] and result['plan']['arrangement']['settling_state'] == 'settled'
    before = crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]
    plain = say(flow, '客户说10月9日下午三点已经约好了，先作为沟通记录', 'plain-report')
    assert not plain.get('plan')
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


def test_date_only_arrangement_can_be_completed_by_speaking_confirmed_clock(world):
    crm, flow, _ = world
    first = say(flow, '10月12日约赵工吃饭', 'date-first')['plan']
    dated = say(flow, '先定日期，对方已确定12号，钟点还没定', 'date-agreement', first)['plan']
    assert dated['arrangement']['settling_state']=='settled'
    assert dated['arrangement']['settlement_scope']=='date_only' and not dated['active_schedule']
    completed = say(flow, '12号下午三点已经约好，按这个安排', 'clock-agreement', dated)['plan']
    assert completed['id']==first['id']
    assert completed['arrangement']['settlement_scope']=='execution_time'
    assert completed['arrangement']['settling_state']=='settled' and completed['active_schedule']
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1


@pytest.mark.parametrize('text',['今天客户说：取消这次活动。先作为沟通记录。',
                                  '不是取消这次活动，按原时间继续。',
                                  '如果饭吃完了，复盘：合作继续'])
def test_model_update_or_recap_cannot_execute_quoted_negative_or_conditional_command(world, text):
    crm, flow, _ = world
    plan = say(flow, '安排10月8日下午三点给赵工打电话', 'initial')['plan']
    class UnsafeProposal:
        async def interpret(self, *args):
            return {'intent':'recap' if '复盘' in text else 'update','changes':{'booking':'cancelled'},
                    'evidence':{'booking':text}}
    flow.interpreter = UnsafeProposal()
    result = say(flow, text, 'reported-negative', plan)['plan']
    assert result['active_schedule']['id'] == plan['task_id']
    assert crm._db.execute('SELECT status FROM tasks WHERE id=?',(plan['task_id'],)).fetchone()[0] == 'pending'
