"""Fresh synthetic progress journeys. No production config/provider/network."""
import asyncio
import json
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.profile_intelligence import ProfileIntelligence
from secretary.progress_workspace import ProgressConflict, ProgressWorkspace
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 4, 8, tzinfo=SHANGHAI).timestamp()


@pytest.fixture
def context(tmp_path):
    crm = CustomerStore(tmp_path / 'progress-synthetic.sqlite3')
    clock = [NOW]
    workspace = SalesWorkspace(crm, clock=lambda: clock[0])
    discussion = DiscussionService(crm, workspace, asyncio.Lock(), clock=lambda: clock[0])
    timeline = TimelineService(crm, workspace, discussions=discussion, clock=lambda: clock[0])
    discussion.timeline = timeline
    discussion.profile_intelligence = ProfileIntelligence(crm, workspace, clock=lambda: clock[0])
    unit = crm.create_customer('owner', {'name': '合成数据安全银行'}, NOW)
    person = crm.create_contact('owner', unit['id'], {'name': '王工', 'department': '科技'}, NOW)
    project = workspace.create_opportunity('owner', unit['id'], {'name': '合成密码改造', 'contact_ids': [person['id']]})
    service = ProgressWorkspace(crm, workspace, discussion, clock=lambda: clock[0])
    yield crm, workspace, discussion, timeline, service, unit, person, project, clock
    crm.close()


def prepare(service, data, request='create-one'):
    run = service.create('owner', {'request_id': request, **data})
    asyncio.run(service.process_pending('owner'))
    return service.get('owner', run['id'])


def choose(service, run, item, **draft):
    return service.edit_draft('owner', run['id'], {'expected_revision': run['revision'], 'items': [{'id': item['id'], 'selected': True, 'draft': draft}]})


def adopt(service, run, ids=None, request='adopt-one'):
    items = [item for item in run['items'] if item['selected'] and (ids is None or item['id'] in ids)]
    data = {'request_id': request, 'expected_revision': run['revision'], 'items': [{'id': item['id'], 'expected_item_revision': item['revision'], 'expected_snapshot': item['versions']['snapshot']} for item in items]}
    return service.confirm('owner', run['id'], data), data


def action(crm, unit, title='发送合成接口清单', terms=None):
    record = crm.create_record('owner', {'title': title, 'content': title, 'kind': 'action', 'status': 'following', 'customer_id': unit['id']}, NOW)
    if terms:
        crm.save_action_terms('owner', record['id'], terms, NOW)
    return crm.get_record('owner', record['id'])


def schedule(crm, record, when):
    with crm._transaction() as db:
        crm._execute(db, 'owner', {'action': 'propose', 'title': record['title'], 'remind_at': when, 'duration_minutes': 30}, NOW)
        p = db.execute("SELECT * FROM proposals WHERE owner='owner' ORDER BY id DESC LIMIT 1").fetchone()
        db.execute("UPDATE crm_records SET proposal_id=? WHERE owner='owner' AND id=?", (p['id'], record['id']))
        crm._remember_proposal(db, 'owner', record['id'], p['id'], NOW)
        crm._execute(db, 'owner', {'action': 'confirm', 'proposal_id': p['id']}, NOW)
    return crm.record_detail('owner', record['id'])['task']


def model_reply(title='核对试点验收条件'):
    return {'answer': '建议先核对试点范围；这不是新增客户事实。', 'questions': ['验收负责人是否明确？'], 'risks': ['预算仍待确认。'],
            'next_moves': [{'title': title, 'reason': '依据已有沟通，审批范围尚未明确。', 'contact_hint': '王工', 'preparation': '准备接口清单和待确认问题', 'success_signal': '得到明确试点范围'}]}


def test_visit_rules_one_input_to_edited_unscheduled_action_and_restart(context):
    crm, _, _, _, service, unit, _, _, _ = context
    original = '准备下次拜访，先明确接口范围，不代表客户已经承诺。'
    run = prepare(service, {'kind': 'visit_prepare', 'customer_id': unit['id'], 'text': original})
    assert run['status'] == 'ready' and run['mode'] == 'rules' and '没有调用大模型' in run['summary']
    assert run['text'] == original and crm.list_records('owner')['total'] == 0
    run = choose(service, run, run['items'][0], title='核对王工的接口范围', content='带三条接口问题拜访。', executor_kind='self')
    result, data = adopt(service, run)
    assert result['status'] == 'complete'
    record = crm.get_record('owner', result['results'][0]['record_id'])
    assert record['title'] == '核对王工的接口范围' and '三条接口问题' in record['content']
    assert record['kind'] == 'action' and record['proposal_id'] is None and record['action_terms']['executor_kind'] == 'self'
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    restored = ProgressWorkspace(crm, service.workspace, service.discussions, clock=lambda: NOW)
    replay = restored.confirm('owner', run['id'], data)
    assert replay['replayed'] and crm.list_records('owner')['total'] == 1


def test_recap_model_context_is_real_scoped_history_without_duplicate_exchange(context):
    crm, workspace, discussion, timeline, service, unit, person, project, _ = context
    row = timeline.create_record('owner', {'contact_id': person['id'], 'opportunity_id': project['id']}, {'request_id': 'meeting', 'text': '王工明确说审批仍需两周。', 'kind': 'communication', 'occurred_at': NOW-86400, 'contact_relations': [{'contact_id': person['id'], 'relation': 'direct'}]})
    record = row['record'] if 'record' in row else row
    outsider = crm.create_customer('owner', {'name': '无关合成医院'}, NOW)
    action(crm, outsider, '无关单位绝密项目')
    class Advisor:
        async def reply(self, context, history, text, now):
            self.context, self.text = context, text
            assert not crm._db.in_transaction
            return model_reply()
    advisor = Advisor(); service._advisor = advisor
    run = prepare(service, {'kind': 'recap', 'customer_id': unit['id'], 'contact_id': person['id'], 'opportunity_id': project['id'], 'text': '我觉得需先准备性能验证，尚未向客户承诺。'})
    assert run['status'] == 'ready' and run['mode'] == 'model'
    sent = json.dumps(advisor.context, ensure_ascii=False)
    assert '审批仍需两周' in sent and '无关单位绝密项目' not in sent
    assert advisor.context['focus_contact']['name'] == '王工'
    assert '不能默认客户已经确认' in advisor.context['progress_goal']['original_input_nature']
    assert crm.list_records('owner')['total'] == 2  # original meeting + outsider; recap not fabricated meeting
    run = choose(service, run, run['items'][0], executor_kind='self')
    result, _ = adopt(service, run)
    rid = result['results'][0]['record_id']
    personal = timeline.view('owner', {'contact_id': person['id'], 'opportunity_id': project['id']})
    assert any(item['id'] == rid for item in personal['summary']['open_actions'])
    assert crm.profile('owner', unit['id'])['fields'] == []


@pytest.mark.parametrize('decision', ['continue', 'waiting'])
def test_feedback_does_not_complete_old_action_or_schedule(context, decision):
    crm, _, _, _, service, unit, _, _, _ = context
    old = action(crm, unit, terms={'executor_kind': 'self'})
    task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '客户尚未反馈，后续继续落实。'})
    assert run['items'][0]['type'] == 'outcome' and run['items'][0]['draft']['decision'] == 'continue'
    run = choose(service, run, run['items'][0], decision=decision, result='实际仍未收到反馈')
    result, data = adopt(service, run)
    assert result['status'] == 'complete'
    assert crm.get_record('owner', old['id'])['status'] == 'following'
    assert crm.record_detail('owner', old['id'])['task']['status'] == 'pending'
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == task['remind_at']
    assert crm._db.execute('SELECT count(*) FROM crm_action_outcomes').fetchone()[0] == 0
    service.confirm('owner', run['id'], data)
    assert crm._db.execute('SELECT count(*) FROM crm_activities').fetchone()[0] == 1


def test_explicit_complete_reuses_outcome_contract_and_retains_next_unscheduled(context):
    crm, _, _, _, service, unit, _, _, _ = context
    old = action(crm, unit); schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '已发送接口清单，客户确认收到。'})
    run = choose(service, run, run['items'][0], decision='complete', result='客户已确认收到清单', next_title='询问试点反馈', next_step='等待试点验证结果后回访。')
    result, data = adopt(service, run)
    assert result['status'] == 'complete'
    receipt = result['results'][0]
    assert crm.get_record('owner', old['id'])['status'] == 'done'
    assert crm.record_detail('owner', old['id'])['task']['status'] == 'completed'
    next_record = crm.get_record('owner', receipt['next_record_id'])
    assert next_record['parent_record_id'] == old['id'] and next_record['proposal_id'] is None
    service.confirm('owner', run['id'], data)
    assert crm._db.execute('SELECT count(*) FROM crm_action_outcomes').fetchone()[0] == 1


@pytest.mark.parametrize('period', ['day', 'week', 'month'])
def test_global_plan_scope_dates_mine_waiting_and_unscheduled(context, period):
    crm, _, _, _, service, unit, _, _, _ = context
    mine = action(crm, unit, terms={'executor_kind': 'self'})
    waiting = action(crm, unit, '客户补充接口清单', {'executor_kind': 'customer', 'check_at': NOW+3600})
    schedule(crm, mine, NOW+7200)
    # Unlinked personal calendar event must also remain visible.
    with crm._transaction() as db:
        crm._execute(db, 'owner', {'action': 'propose', 'title': '个人内部会议', 'remind_at': NOW+14400, 'duration_minutes': 30}, NOW)
        p = db.execute("SELECT max(id) FROM proposals WHERE owner='owner'").fetchone()[0]
        crm._execute(db, 'owner', {'action': 'confirm', 'proposal_id': p}, NOW)
    run = prepare(service, {'kind': 'plan', 'period': period, 'start_date': '2026-10-04', 'text': '整理我的推进计划。'})
    assert run['status'] == 'ready'
    assert [item['record_id'] for item in run['mine']] == [mine['id']]
    assert [item['record_id'] for item in run['waiting']] == [waiting['id']]
    assert [item['record_id'] for item in run['unscheduled']] == [waiting['id']]
    assert {item['title'] for item in run['existing_schedule']} == {'发送合成接口清单', '个人内部会议'}
    assert run['period'] == period and run['start_date'] == '2026-10-04'
    assert crm.get_record('owner', waiting['id'])['proposal_id'] is None


def test_plan_confirm_new_time_one_confirmation_and_conflict_keeps_old_schedule(context):
    crm, _, _, _, service, unit, _, _, _ = context
    old = action(crm, unit, terms={'executor_kind': 'self'})
    task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'})
    item = next(item for item in run['items'] if item['current'])
    assert item['type'] == 'reschedule' and item['current']['task']['remind_at'] == task['remind_at']
    run = choose(service, run, item, remind_at=NOW+14400, duration_minutes=30)
    result, data = adopt(service, run)
    assert result['results'][0]['status'] == 'confirmed'
    changed = crm.record_detail('owner', old['id'])['task']
    assert changed['id'] == task['id'] and changed['remind_at'] == NOW+14400
    service.confirm('owner', run['id'], data)
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_pending_time_conflict_does_not_silently_move_existing_task(context):
    crm, _, _, _, service, unit, _, _, _ = context
    old = action(crm, unit, terms={'executor_kind': 'self'}); original = schedule(crm, old, NOW+7200)
    other = action(crm, unit, '另一个固定会议', {'executor_kind': 'self'}); schedule(crm, other, NOW+14400)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'})
    item = next(x for x in run['items'] if x['current']['record_id'] == old['id'])
    run = choose(service, run, item, remind_at=NOW+14400, duration_minutes=30)
    result, _ = adopt(service, run)
    assert result['results'][0]['status'] == 'schedule_pending'
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == original['remind_at']
    assert crm.get_proposal('owner', result['results'][0]['proposal_id'])['status'] == 'pending'


def test_waiting_check_time_not_customer_execution_and_unknown_no_appointment(context):
    crm, _, _, _, service, unit, _, _, _ = context
    row = action(crm, unit, '客户负责提供范围', {'executor_kind': 'customer'})
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'})
    run = choose(service, run, run['items'][0], remind_at=NOW+7200, duration_minutes=30)
    result, _ = adopt(service, run)
    assert result['status'] == 'blocked' and crm.get_record('owner', row['id'])['proposal_id'] is None
    run = choose(service, result['run'], result['run']['items'][0], check_at=NOW+7200)
    result, _ = adopt(service, run, request='check-customer')
    assert result['status'] == 'complete' and crm.record_detail('owner', row['id'])['task']['status'] == 'pending'


def test_owner_strict_body_cas_idempotency_and_no_formal_fact_side_effects(context):
    crm, _, _, _, service, unit, _, _, _ = context
    data = {'kind': 'visit_prepare', 'customer_id': unit['id'], 'request_id': 'stable', 'text': '准备拜访'}
    first = service.create('owner', data); assert service.create('owner', data)['id'] == first['id']
    with pytest.raises(ProgressConflict): service.create('owner', {**data, 'text': '不同内容'})
    with pytest.raises(KeyError): service.get('other', first['id'])
    with pytest.raises(KeyError): service.create('other', {**data, 'request_id': 'other'})
    with pytest.raises(ValueError): service.create('owner', {**data, 'owner': 'other'})
    asyncio.run(service.process_pending('owner')); run = service.get('owner', first['id'])
    with pytest.raises(ProgressConflict): service.edit_draft('owner', run['id'], {'expected_revision': run['revision']-1, 'items': [{'id': run['items'][0]['id'], 'selected': True}]})
    with pytest.raises(ValueError): service.edit_draft('owner', run['id'], {'expected_revision': run['revision'], 'items': [{'id': run['items'][0]['id'], 'current': {}}]})
    with pytest.raises(ProgressConflict): adopt(service, {**run, 'items': [{**run['items'][0], 'selected': True}]})
    assert crm.profile('owner', unit['id'])['fields'] == []


def test_source_edit_marks_stale_without_replacing_captured_old_values(context):
    crm, _, _, _, service, unit, _, _, _ = context
    old = action(crm, unit, terms={'executor_kind': 'self'}); task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'})
    run = choose(service, run, run['items'][0], remind_at=NOW+10800, duration_minutes=30)
    captured = run['items'][0]['current']
    crm.update_record('owner', old['id'], {'content': '原事项已经改为其他范围'}, NOW+1)
    current = service.get('owner', run['id'])
    assert current['status'] == 'needs_review' and current['items'][0]['current'] == captured
    with pytest.raises(ProgressConflict): adopt(service, run)
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == task['remind_at']


def test_cancel_late_model_reply_retry_retains_input_and_custom_edits(context):
    _, _, _, _, service, unit, _, _, _ = context
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Advisor:
            async def reply(self, *args):
                entered.set(); await release.wait(); return model_reply()
        service._advisor = Advisor()
        run = service.create('owner', {'request_id': 'late', 'kind': 'recap', 'customer_id': unit['id'], 'text': '我的原始会后思考'})
        task = asyncio.create_task(service.process_pending('owner')); await asyncio.wait_for(entered.wait(), 2)
        processing = service.get('owner', run['id']); cancelled = service.cancel('owner', run['id'], {'expected_revision': processing['revision']})
        release.set(); await task
        assert service.get('owner', run['id'])['status'] == 'cancelled'
        retry = service.retry('owner', run['id'], {'expected_revision': cancelled['revision']}); await service.process_pending('owner')
        current = service.get('owner', run['id']); assert current['text'] == '我的原始会后思考' and current['status'] == 'ready'
        current = choose(service, current, current['items'][0], content='我补充的完整准备材料')
        cancelled = service.cancel('owner', current['id'], {'expected_revision': current['revision']})
        service.retry('owner', current['id'], {'expected_revision': cancelled['revision']}); await service.process_pending('owner')
        assert service.get('owner', current['id'])['items'][0]['draft']['content'] == '我补充的完整准备材料'
    asyncio.run(scenario())


def test_worker_failure_safe_receipt_restart_and_lease_recovery(context):
    crm, _, _, _, service, unit, _, _, clock = context
    class Fail:
        async def reply(self, *args): raise RuntimeError('secret-provider-key-not-public')
    service._advisor = Fail()
    run = prepare(service, {'kind': 'recap', 'customer_id': unit['id'], 'text': '原话必须保留'})
    assert run['status'] == 'failed' and 'secret-provider' not in json.dumps(run)
    service._advisor = None
    run = service.retry('owner', run['id'], {'expected_revision': run['revision']})
    claimed = service._claim('owner'); clock[0] += 181
    restored = ProgressWorkspace(crm, service.workspace, service.discussions, clock=lambda: clock[0])
    asyncio.run(restored.process_pending('owner'))
    assert restored.get('owner', run['id'])['status'] == 'ready'
    assert restored.get('owner', run['id'])['text'] == '原话必须保留'


@pytest.mark.parametrize('body', [
    {'kind': 'plan', 'period': 'quarter', 'start_date': '2026-10-04'},
    {'kind': 'plan', 'period': 'day', 'start_date': '2026-02-30'},
    {'kind': 'plan', 'period': 'day', 'start_date': '2026-1-1'},
    {'kind': 'plan', 'period': 'day', 'start_date': '9999-12-31'},
    {'kind': 'visit_prepare'},
])
def test_invalid_scope_and_date_no_preparation_written(context, body):
    crm, _, _, _, service, *_ = context
    with pytest.raises(ValueError): service.create('owner', {'request_id': 'bad', **body})
    assert crm._db.execute('SELECT count(*) FROM crm_progress_runs').fetchone()[0] == 0


def test_cross_unit_contact_legal_only_in_current_project_membership(context):
    crm, workspace, _, _, service, unit, _, project, _ = context
    parent = crm.create_customer('owner', {'name': '合成集团'}, NOW)
    person = crm.create_contact('owner', parent['id'], {'name': '总部周工'}, NOW)
    with pytest.raises(ValueError): service.create('owner', {'request_id': 'wrong', 'kind': 'visit_prepare', 'customer_id': unit['id'], 'contact_id': person['id']})
    # Membership requires an explicit project unit relation first.
    workspace.upsert_project_unit('owner', unit['id'], project['id'], {'participant_customer_id': parent['id'], 'roles': ['approver'], 'basis': 'reported', 'evidence': '总部明确参与决策', 'expected_revision': project['revision']})
    project = workspace.opportunities('owner', unit['id'])['items'][0]
    workspace.upsert_stakeholder('owner', unit['id'], project['id'], {'contact_id': person['id'], 'roles': ['final_approver'], 'basis': 'reported', 'evidence': '明确项目决策人', 'expected_revision': project['revision']})
    run = prepare(service, {'kind': 'visit_prepare', 'customer_id': unit['id'], 'contact_id': person['id'], 'opportunity_id': project['id']})
    assert run['status'] == 'ready' and run['scope']['contact_name'] == '总部周工'


def test_cancelled_worker_returns_queued_for_explicit_resume(context):
    _, _, _, _, service, unit, *_ = context
    async def scenario():
        entered = asyncio.Event()
        class Advisor:
            async def reply(self, *args): entered.set(); await asyncio.Event().wait()
        service._advisor = Advisor()
        run = service.create('owner', {'request_id': 'interrupt', 'kind': 'visit_prepare', 'customer_id': unit['id']})
        task = asyncio.create_task(service.process_pending('owner')); await asyncio.wait_for(entered.wait(), 2); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert service.get('owner', run['id'])['status'] == 'queued'
    asyncio.run(scenario())


def test_complete_after_commit_exception_recovers_durable_outcome_without_duplicate(context, monkeypatch):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit); schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '确认材料已交付'})
    run = choose(service, run, run['items'][0], decision='complete', result='客户确认收到', next_title='回访试点反馈', next_step='再回访一次')
    real = service._complete
    def fail_after_commit(*args):
        real(*args); raise RuntimeError('synthetic receipt failure')
    monkeypatch.setattr(service, '_complete', fail_after_commit)
    first, data = adopt(service, run)
    assert crm.get_record('owner', old['id'])['status'] == 'done'
    assert first['results'][0]['status'] == 'already_confirmed'
    monkeypatch.setattr(service, '_complete', real)
    again = service.confirm('owner', run['id'], data)
    assert again['replayed'] and again['results'][0]['next_record_id']
    assert crm._db.execute('SELECT count(*) FROM crm_action_outcomes').fetchone()[0] == 1
    assert crm._db.execute('SELECT count(*) FROM crm_records WHERE parent_record_id=?', (old['id'],)).fetchone()[0] == 1


def test_outcome_does_not_complete_task_changed_after_visible_snapshot(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit); task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '准备确认材料已交付'})
    run = choose(service, run, run['items'][0], decision='complete', result='已交付')
    with crm._transaction() as db:
        db.execute('UPDATE tasks SET revision=revision+1,remind_at=? WHERE owner=? AND id=?', (NOW+14400, 'owner', task['id']))
    with pytest.raises(ProgressConflict): adopt(service, run)
    assert crm.get_record('owner', old['id'])['status'] == 'following'
    assert crm.record_detail('owner', old['id'])['task']['status'] == 'pending'


def test_partial_adoption_keeps_success_then_explicitly_repair_blocked_item(context):
    crm, _, _, _, service, unit, *_ = context
    class Advisor:
        async def reply(self, *args):
            reply = model_reply('确认试点范围')
            reply['next_moves'].append({**reply['next_moves'][0], 'title': '收集客户反馈'})
            return reply
    service._advisor = Advisor()
    run = prepare(service, {'kind': 'visit_prepare', 'customer_id': unit['id']})
    run = service.edit_draft('owner', run['id'], {'expected_revision': run['revision'], 'items': [
        {'id': run['items'][0]['id'], 'selected': True, 'draft': {'executor_kind': 'self'}},
        {'id': run['items'][1]['id'], 'selected': True, 'draft': {'executor_kind': 'customer', 'remind_at': NOW+7200}}]})
    result, data = adopt(service, run)
    assert result['status'] == 'partial'
    assert [item['status'] for item in result['results']] == ['confirmed', 'blocked']
    assert crm.list_records('owner')['total'] == 1
    repeat = service.confirm('owner', run['id'], data)
    assert repeat['replayed'] and crm.list_records('owner')['total'] == 1
    run = choose(service, result['run'], result['run']['items'][1], remind_at=None)
    result, _ = adopt(service, run, ['move:2'], request='repaired-second')
    assert result['status'] == 'complete' and crm.list_records('owner')['total'] == 2


def test_model_wait_allows_independent_update_and_rejects_late_source_version(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Advisor:
            async def reply(self, context, *args):
                assert not crm._db.in_transaction
                entered.set(); await release.wait(); return model_reply()
        service._advisor = Advisor()
        run = service.create('owner', {'request_id': 'race', 'kind': 'visit_prepare', 'customer_id': unit['id']})
        worker = asyncio.create_task(service.process_pending('owner')); await asyncio.wait_for(entered.wait(), 2)
        crm.update_record('owner', old['id'], {'content': '项目已暂停，不能继续安排试点'}, NOW+1)
        release.set(); await worker
        current = service.get('owner', run['id'])
        assert current['status'] == 'needs_review' and current['items'] == []
        assert crm.get_record('owner', old['id'])['content'] == '项目已暂停，不能继续安排试点'
    asyncio.run(scenario())


def test_additive_upgrade_keeps_existing_rows_and_never_replays_history(context):
    crm, ws, discussion, _, service, unit, *_ = context
    old = action(crm, unit); schedule(crm, old, NOW+7200)
    before = {table: [tuple(row) for row in crm._db.execute('SELECT * FROM '+table)] for table in ('crm_records', 'crm_customers', 'tasks', 'proposals', 'crm_customer_facts')}
    ProgressWorkspace(crm, ws, discussion, clock=lambda: NOW)
    after = {table: [tuple(row) for row in crm._db.execute('SELECT * FROM '+table)] for table in before}
    assert before == after and service.list_runs('owner')['runs'] == []


def test_existing_pending_proposal_is_reused_not_duplicated(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, terms={'executor_kind': 'self'})
    with crm._transaction() as db:
        crm._execute(db, 'owner', {'action': 'propose', 'title': old['title'], 'remind_at': NOW+7200, 'duration_minutes': 30}, NOW)
        pid = db.execute("SELECT max(id) FROM proposals WHERE owner='owner'").fetchone()[0]
        db.execute('UPDATE crm_records SET proposal_id=? WHERE owner=? AND id=?', (pid, 'owner', old['id']))
        crm._remember_proposal(db, 'owner', old['id'], pid, NOW)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'week', 'start_date': '2026-10-05'})
    run = choose(service, run, run['items'][0], remind_at=NOW+86400+7200, duration_minutes=30)
    result, _ = adopt(service, run)
    assert result['results'][0]['proposal_id'] == pid
    assert crm._db.execute('SELECT count(*) FROM proposals').fetchone()[0] == 1
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == NOW+86400+7200


def test_date_only_customer_deadline_and_check_stay_unscheduled(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, '等待客户确认范围', {'executor_kind': 'customer', 'deadline_date': '2026-10-10', 'check_date': '2026-10-06'})
    run = prepare(service, {'kind': 'plan', 'period': 'week', 'start_date': '2026-10-05'})
    assert run['waiting'][0]['current']['terms']['check_date'] == '2026-10-06'
    assert run['unscheduled'][0]['record_id'] == old['id'] and run['existing_schedule'] == []
    assert run['items'][0]['draft']['remind_at'] is None
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_person_feedback_cannot_target_unrelated_same_unit_action(context):
    crm, _, _, _, service, unit, person, _, _ = context
    old = action(crm, unit)
    with pytest.raises((ValueError, KeyError)):
        service.create('owner', {'request_id': 'wrong-person', 'kind': 'followup_result', 'customer_id': unit['id'], 'contact_id': person['id'], 'record_id': old['id'], 'text': '不能把其他人的行动当王工落实结果'})
    assert crm._db.execute('SELECT count(*) FROM crm_progress_runs').fetchone()[0] == 0


def test_waiting_can_explicitly_add_next_action_without_closing_parent(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit); task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '客户还未补充，明天回访'})
    run = choose(service, run, run['items'][0], decision='waiting', result='客户尚未回复', next_title='回访缺失信息', next_step='我再回访一次核对接口', executor_kind='self')
    result, data = adopt(service, run)
    child = crm.get_record('owner', result['results'][0]['next_record_id'])
    assert child['parent_record_id'] == old['id'] and child['proposal_id'] is None
    assert crm.get_record('owner', old['id'])['status'] == 'following'
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == task['remind_at']
    service.confirm('owner', run['id'], data)
    assert crm._db.execute('SELECT count(*) FROM crm_records WHERE parent_record_id=?', (old['id'],)).fetchone()[0] == 1


def test_date_only_edit_adopts_dates_without_inventing_clock_or_replacing_task(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, '等待客户确认范围', {'executor_kind': 'customer', 'deadline_date': '2026-10-10', 'check_date': '2026-10-06'})
    run = prepare(service, {'kind': 'plan', 'period': 'week', 'start_date': '2026-10-05'})
    assert run['items'][0]['draft']['check_date'] == '2026-10-06'
    run = choose(service, run, run['items'][0], check_date='2026-10-07', deadline_date='2026-10-11')
    result, data = adopt(service, run)
    assert result['status'] == 'complete'
    terms = crm.get_record('owner', old['id'])['action_terms']
    assert terms['check_date'] == '2026-10-07' and terms['deadline_date'] == '2026-10-11'
    assert terms.get('execution_at') is None and terms.get('check_at') is None
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    service.confirm('owner', run['id'], data)
    assert crm._db.execute('SELECT count(*) FROM proposals').fetchone()[0] == 0


def test_date_only_edit_keeps_existing_exact_task(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, terms={'executor_kind': 'self'}); task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'plan', 'period': 'day', 'start_date': '2026-10-04'})
    run = choose(service, run, run['items'][0], deadline_date='2026-10-10')
    result, _ = adopt(service, run)
    assert result['status'] == 'complete'
    assert crm.record_detail('owner', old['id'])['task'] == task


@pytest.mark.parametrize('invalid', ['2026-02-30', '2026-1-1', '', True, 1800000000])
def test_date_only_invalid_draft_does_not_save(context, invalid):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, terms={'executor_kind': 'customer'})
    run = prepare(service, {'kind': 'plan', 'period': 'day', 'start_date': '2026-10-04'})
    with pytest.raises(ValueError): choose(service, run, run['items'][0], check_date=invalid)
    assert crm.get_record('owner', old['id'])['action_terms'].get('check_date') is None


def test_target_candidates_are_owner_scope_valid_person_and_project_not_guessed(context):
    crm, workspace, discussion, timeline, service, unit, person, project, _ = context
    mine = action(crm, unit, '王工范围待落实'); workspace.link('owner', 'record', mine['id'], project['id'])
    timeline.link_action('owner', mine['id'], {'contact_id': person['id'], 'opportunity_id': project['id']})
    unrelated = action(crm, unit, '另一个人的事项'); workspace.link('owner', 'record', unrelated['id'], project['id'])
    done = action(crm, unit, '已完成不能选'); crm.update_record('owner', done['id'], {'status': 'done'}, NOW+1)
    foreign_unit = crm.create_customer('foreign', {'name': '其他成员单位'}, NOW)
    crm.create_record('foreign', {'title': '别人的行动', 'content': '不能泄漏', 'kind': 'action', 'customer_id': foreign_unit['id']}, NOW)
    result = service.target_candidates('owner', {'customer_id': unit['id'], 'contact_id': person['id'], 'opportunity_id': project['id']})
    assert [item['id'] for item in result['items']] == [mine['id']]
    assert result['requires_confirmation'] and result['selected_record_id'] is None
    assert result['items'][0]['current']['record_id'] == mine['id']
    assert result['items'][0]['current']['opportunity_id'] == project['id']
    assert result['items'][0]['current']['status'] == 'following'
    assert result['items'][0]['current']['customer_name'] == unit['name']
    assert {item['id'] for item in service.target_candidates('owner')['items']} == {mine['id'], unrelated['id']}
    crm.update_record('owner', mine['id'], {'content': '来源修正，项目关联尚未核对'}, NOW+2)
    assert service.target_candidates('owner', {'customer_id': unit['id'], 'contact_id': person['id'], 'opportunity_id': project['id']})['items'] == []


@pytest.mark.parametrize('bad', [[], {}, True, 7])
def test_item_ids_are_strict_no_internal_exception_or_write(context, bad):
    crm, _, _, _, service, unit, *_ = context
    run = prepare(service, {'kind': 'visit_prepare', 'customer_id': unit['id']})
    with pytest.raises(ValueError):
        service.edit_draft('owner', run['id'], {'expected_revision': run['revision'], 'items': [{'id': bad, 'selected': True}]})
    with pytest.raises(ValueError):
        service.confirm('owner', run['id'], {'expected_revision': run['revision'], 'request_id': 'bad', 'items': [{'id': bad, 'expected_item_revision': 1, 'expected_snapshot': ''}]})
    assert crm._db.execute('SELECT count(*) FROM crm_progress_batches').fetchone()[0] == 0


def test_explicit_internal_action_feedback_has_no_invented_unit(context):
    crm, _, _, _, service, unit, *_ = context
    old = crm.create_record('owner', {'title': '内部完善测评材料', 'content': '准备内部材料', 'kind': 'action', 'status': 'following'}, NOW)
    targets = service.target_candidates('owner')
    assert targets['selected_record_id'] is None
    assert any(item['id'] == old['id'] and item['customer_id'] is None for item in targets['items'])
    run = prepare(service, {'kind': 'followup_result', 'record_id': old['id'], 'text': '内部测评材料已整理，下一步核对技术版。'})
    assert run['scope']['customer_id'] is None and run['scope']['contact_id'] is None and run['scope']['opportunity_id'] is None
    assert run['items'][0]['current']['customer_name'] == '我的工作 · 单位未指定'
    run = choose(service, run, run['items'][0], decision='complete', result='材料完成', next_step='核对技术版', executor_kind='self')
    result, body = adopt(service, run)
    assert crm.get_record('owner', old['id'])['status'] == 'done'
    child = crm.get_record('owner', result['results'][0]['next_record_id'])
    assert child['customer_id'] is None and child['action_terms']['executor_kind'] == 'self'
    again = service.confirm('owner', run['id'], body)
    assert again['replayed'] and again['results'][0]['next_record_id'] == child['id']
    unit_action = action(crm, unit)
    with pytest.raises(KeyError):
        service.create('owner', {'request_id': 'no-unit-guess', 'kind': 'followup_result', 'record_id': unit_action['id'], 'text': '不能从原行动推断单位。'})


@pytest.mark.parametrize('decision', ['waiting', 'continue', 'complete'])
def test_feedback_next_dates_require_explicit_next_step_do_not_disappear(context, decision):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, terms={'executor_kind': 'customer', 'check_date': '2026-10-06'})
    task = schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '这轮反馈需要核对。'})
    run = choose(service, run, run['items'][0], decision=decision, result='尚需后续核对', check_date='2026-10-08')
    result, _ = adopt(service, run)
    assert result['status'] == 'blocked' and result['run']['items'][0]['draft']['check_date'] == '2026-10-08'
    assert crm.get_record('owner', old['id'])['status'] == 'following'
    assert crm.get_record('owner', old['id'])['action_terms']['check_date'] == '2026-10-06'
    assert crm.record_detail('owner', old['id'])['task'] == task
    assert crm._db.execute('SELECT count(*) FROM crm_activities').fetchone()[0] == 0


def test_completion_preserves_next_responsibility_date_only_and_real_check(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '清单已交付，等待客户确认。'})
    run = choose(service, run, run['items'][0], decision='complete', result='清单已交付', next_title='等待客户确认范围', next_step='对照客户的范围确认反馈', executor_kind='customer',
                 check_date='2026-10-06', deadline_date='2026-10-10', remind_at=NOW+7200, check_at=NOW+7200, deadline_at=NOW+14400, duration_minutes=30)
    result, _ = adopt(service, run)
    assert result['status'] == 'complete'
    child = crm.get_record('owner', result['results'][0]['next_record_id'])
    assert child['action_terms']['executor_kind'] == 'customer' and child['action_terms']['execution_at'] is None
    assert child['action_terms']['check_at'] == NOW+7200 and child['action_terms']['check_date'] == '2026-10-06'
    assert child['action_terms']['deadline_date'] == '2026-10-10'
    assert crm.record_detail('owner', child['id'])['task']['deadline_at'] == NOW+14400
    plan = prepare(service, {'kind': 'plan', 'period': 'week', 'start_date': '2026-10-05'}, request='waiting-plan')
    assert [item['record_id'] for item in plan['waiting']] == [child['id']]
    assert not plan['mine']


def test_completion_recovery_finishes_pristine_next_terms_and_check_once(context, monkeypatch):
    crm, workspace, _, _, service, unit, *_ = context
    old = action(crm, unit)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '已交付，随后本人检查客户反馈。'})
    run = choose(service, run, run['items'][0], decision='complete', result='交付完成', next_step='检查客户反馈', executor_kind='customer', check_at=NOW+7200,
                 remind_at=NOW+7200, check_date='2026-10-06', deadline_at=NOW+14400, duration_minutes=30)
    real = workspace.complete_record
    def crash_after_business_commit(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('synthetic interruption between business commit and receipt')
    monkeypatch.setattr(workspace, 'complete_record', crash_after_business_commit)
    result, body = adopt(service, run)
    receipt = result['results'][0]
    assert receipt['status'] == 'already_confirmed' and not receipt['schedule_pending']
    child = crm.get_record('owner', receipt['next_record_id'])
    assert child['action_terms']['executor_kind'] == 'customer' and child['action_terms']['check_date'] == '2026-10-06'
    task = crm.record_detail('owner', child['id'])['task']
    assert task['status'] == 'pending' and task['remind_at'] == NOW+7200 and task['deadline_at'] == NOW+14400
    service.confirm('owner', run['id'], body)
    assert crm._db.execute('SELECT count(*) FROM crm_action_outcomes').fetchone()[0] == 1
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_completion_recovery_does_not_overwrite_child_changed_after_commit(context, monkeypatch):
    crm, workspace, _, _, service, unit, *_ = context
    old = action(crm, unit)
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '已交付，后续待核对。'})
    run = choose(service, run, run['items'][0], decision='complete', result='已交付', next_step='检查客户反馈', executor_kind='customer', check_at=NOW+7200, remind_at=NOW+7200, duration_minutes=30)
    real = workspace.complete_record
    def mutate_then_interrupt(*args, **kwargs):
        result = real(*args, **kwargs)
        crm.update_record('owner', result['next_record']['id'], {'title': '同事随后修订的下一步'}, NOW+1)
        crm.save_action_terms('owner', result['next_record']['id'], {'executor_kind': 'team', 'check_date': '2026-10-09'}, NOW+1)
        raise RuntimeError('synthetic committed followup changed before recovery')
    monkeypatch.setattr(workspace, 'complete_record', mutate_then_interrupt)
    result, _ = adopt(service, run)
    child = crm.get_record('owner', result['results'][0]['next_record_id'])
    assert child['title'] == '同事随后修订的下一步'
    assert child['action_terms'] == {'executor_kind': 'team', 'check_date': '2026-10-09'}
    assert crm.record_detail('owner', child['id'])['task'] is None
    assert crm.record_detail('owner', child['id'])['proposal']['status'] == 'pending'
    assert result['results'][0]['schedule_pending'] and '未覆盖' in result['results'][0]['message']


def test_plan_confirmed_check_updates_responsibility_but_conflict_keeps_old_terms(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, terms={'executor_kind': 'self', 'execution_at': NOW+7200, 'deadline_at': NOW+20000})
    schedule(crm, old, NOW+7200)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'})
    assert run['items'][0]['draft']['deadline_at'] == NOW+20000
    run = choose(service, run, run['items'][0], executor_kind='customer', remind_at=NOW+10800, check_at=NOW+10800, duration_minutes=30)
    result, _ = adopt(service, run)
    terms = crm.get_record('owner', old['id'])['action_terms']
    assert terms['executor_kind'] == 'customer' and terms['execution_at'] is None and terms['check_at'] == NOW+10800
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == NOW+10800
    blocking = action(crm, unit, '日程冲突'); schedule(crm, blocking, NOW+14400)
    run = prepare(service, {'kind': 'plan', 'customer_id': unit['id'], 'period': 'day', 'start_date': '2026-10-04'}, request='second-plan')
    item = next(item for item in run['items'] if item.get('current', {}).get('record_id') == old['id'])
    run = choose(service, run, item, executor_kind='self', remind_at=NOW+14400, check_at=None, duration_minutes=30)
    result, _ = adopt(service, run, [item['id']], request='second-adopt')
    assert result['results'][0]['schedule_pending']
    assert crm.get_record('owner', old['id'])['action_terms'] == terms
    assert crm.record_detail('owner', old['id'])['task']['remind_at'] == NOW+10800


def test_completion_recovery_does_not_repurpose_child_moved_to_other_project(context, monkeypatch):
    crm, workspace, _, _, service, unit, _, project, *_ = context
    old = action(crm, unit); workspace.link('owner', 'record', old['id'], project['id'])
    other = workspace.create_opportunity('owner', unit['id'], {'name': '后续另一个项目'})
    run = prepare(service, {'kind': 'followup_result', 'customer_id': unit['id'], 'record_id': old['id'], 'text': '交付完成后留检查事项。'})
    run = choose(service, run, run['items'][0], decision='complete', result='已交付', next_step='检查客户反馈', executor_kind='customer', check_at=NOW+7200, remind_at=NOW+7200, duration_minutes=30)
    real = workspace.complete_record
    def move_child_then_interrupt(*args, **kwargs):
        result = real(*args, **kwargs)
        workspace.link('owner', 'record', result['next_record']['id'], other['id'])
        raise RuntimeError('synthetic identity change after commit')
    monkeypatch.setattr(workspace, 'complete_record', move_child_then_interrupt)
    result, _ = adopt(service, run)
    child = crm.get_record('owner', result['results'][0]['next_record_id'])
    assert service._current('owner', child['id'])['opportunity_id'] == other['id']
    assert not child['action_terms'] and crm.record_detail('owner', child['id'])['task'] is None
    assert result['results'][0]['schedule_pending'] and '未覆盖' in result['results'][0]['message']


def test_plan_201_records_exposes_source_limit_without_claiming_all_actions(context):
    crm, _, _, _, service, unit, *_ = context
    old = action(crm, unit, '旧未完承诺仍需保留')
    for index in range(200):
        crm.create_record('owner', {'title': f'合成近期记录{index}', 'content': '近期记录', 'kind': 'note', 'customer_id': unit['id']}, NOW+index+1)
    run = prepare(service, {'kind': 'plan', 'period': 'day', 'start_date': '2026-10-04'})
    limits = run['read_limits']
    assert limits['record_count'] == 201 and limits['record_limit'] == 200 and limits['records_truncated']
    assert limits['action_count'] == 1 and limits['observed_action_count'] == 0 and limits['truncated']
    assert run['warnings'] and '未读取范围内全部记录' in ' '.join(run['warnings'])
    assert '不能当作完整计划' in run['summary'] and run['errors'] == []
    assert crm.get_record('owner', old['id'])['status'] == 'following'
    assert crm._db.execute('SELECT count(*) FROM crm_records').fetchone()[0] == 201


def test_plan_31_actions_exposes_ui_and_actual_model_input_limits(context):
    crm, _, _, _, service, unit, *_ = context
    rows = [action(crm, unit, f'合成未完事项{i}', {'executor_kind': 'self'}) for i in range(31)]
    class Advisor:
        async def reply(self, context, *args):
            self.sent = context
            return model_reply()
    advisor = Advisor(); service._advisor = advisor
    run = prepare(service, {'kind': 'plan', 'period': 'week', 'start_date': '2026-10-05'})
    assert len(run['mine']) == 30 and len(advisor.sent['open_actions']) == 20
    assert run['read_limits']['actions_truncated'] and run['read_limits']['model_actions_truncated']
    assert run['read_limits']['action_count'] == 31 and run['read_limits']['observed_action_count'] == 31
    assert advisor.sent['progress_read_limits']['model_action_limit'] == 20
    assert '其他事项仍保留' in ' '.join(run['warnings']) and '未送入模型' in ' '.join(run['warnings'])
    assert '不能当作完整计划' in run['summary']
    assert all(crm.get_record('owner', row['id'])['status'] == 'following' for row in rows)
    assert crm._db.execute('SELECT count(*) FROM proposals').fetchone()[0] == 0
