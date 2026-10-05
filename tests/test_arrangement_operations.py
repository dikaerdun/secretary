"""State changes operate on one synthetic plan and preserve original evidence."""
import asyncio
import copy
import json
from datetime import datetime
import uuid

import pytest

from secretary.arrangement_queue import ArrangementConflict, content_signature
from secretary.arrangement_time import normalize_time, time_end, time_start
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI

NOW = datetime(2026, 10, 5, 10, tzinfo=SHANGHAI).timestamp()
OWNER = 'synthetic-arrangement-user'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-arrangements.sqlite3')
    now = [NOW]
    workspace = SalesWorkspace(crm, clock=lambda: now[0])
    flow = SecretaryFlow(crm, workspace, asyncio.Lock(), clock=lambda: now[0])
    result = {'crm': crm, 'flow': flow, 'queue': flow.arrangements, 'now': now, 'workspace': workspace}
    yield result
    crm.close()


def execution(text='2026-10-08T15:00:00+08:00', **kwargs):
    return {'time_spec': normalize_time(text, NOW, role='execution'), **kwargs}


def make_plan(w, **extra):
    crm = w['crm']
    record = crm.create_record(OWNER, {'title': '虚构客户会面', 'content': '虚构原话不可改写', 'kind': 'action', 'status': 'following'}, NOW)
    data = {'title': '虚构客户会面', 'decision_mode': 'self', 'settlement_scope': 'execution_time',
            'proposed_execution': execution(), 'duration_minutes': 30, **extra}
    with crm._transaction() as db:
        identifier = db.execute('''INSERT INTO crm_secretary_plans(owner,record_id,data_json,revision,created_at,updated_at,schema_version,followup_enabled,followup_disable_reason)
            VALUES(?,?,?,1,?,?,1,1,'')''', (OWNER, record['id'], json.dumps(data, ensure_ascii=False), NOW, NOW)).lastrowid
    return identifier


def command(w, identifier, operation, **payload):
    data = {'operation': operation, 'request_id': uuid.uuid4().hex, 'expected_revision': w['queue'].get(OWNER, identifier)['revision'], **payload}
    return w['queue'].apply_decision(OWNER, identifier, data)


def counts(w):
    return tuple(w['crm']._db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('tasks', 'notifications', 'crm_arrangement_notifications'))


def test_independent_times_and_original_source_are_preserved(world):
    identifier = make_plan(world)
    row = world['crm']._db.execute('SELECT record_id FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    original = dict(world['crm']._db.execute('SELECT * FROM crm_records WHERE id=?', (row['record_id'],)).fetchone())
    command(world, identifier, 'set_deadline', settle_deadline={'strength': 'required', 'time_spec': normalize_time('本周内', NOW)})
    result = command(world, identifier, 'set_check', next_check={'time_spec': normalize_time('周五再问', NOW, role='check'), 'action': '询问回复'})
    data = result['arrangement']
    assert data['settle_deadline']['time_spec']['date'] == '2026-10-11'
    assert data['deadline_end_at'] == datetime(2026, 10, 12, tzinfo=SHANGHAI).timestamp()
    assert data['next_check']['time_spec']['date'] == '2026-10-09'
    assert data['proposed_execution']['time_spec']['date'] == '2026-10-08'
    assert dict(world['crm']._db.execute('SELECT * FROM crm_records WHERE id=?', (row['record_id'],)).fetchone()) == original
    assert counts(world) == (0, 0, 0)


def test_request_ledger_replays_before_revision_check_and_rejects_reuse(world):
    identifier = make_plan(world)
    request = {'operation': 'pause', 'request_id': 'same-request', 'expected_revision': 1}
    first = world['queue'].apply_decision(OWNER, identifier, request)
    second = world['queue'].apply_decision(OWNER, identifier, request)
    assert first == second and first['revision'] == 2
    assert world['crm']._db.execute('SELECT COUNT(*) FROM crm_arrangement_operations').fetchone()[0] == 1
    with pytest.raises(ArrangementConflict):
        world['queue'].apply_decision(OWNER, identifier, {**request, 'operation': 'resume'})
    with pytest.raises(ArrangementConflict):
        world['queue'].apply_decision(OWNER, identifier, {**request, 'request_id': 'new-request'})


@pytest.mark.parametrize('payload', [
    {'operation': 'pause', 'request_id': 'x', 'expected_revision': True},
    {'operation': 'pause', 'request_id': 'x', 'expected_revision': 1, 'unexpected': 1},
    {'operation': 'set_check', 'request_id': 'x', 'expected_revision': 1, 'next_check': None, 'followup_enabled': 1},
    {'operation': 'set_deadline', 'request_id': 'x', 'expected_revision': 1},
    {'operation': 'confirm_arrangement', 'request_id': 'x', 'expected_revision': 1, 'candidate_id': 'x', 'proposed_execution': execution()},
])
def test_strict_invalid_requests_have_no_partial_write(world, payload):
    identifier = make_plan(world)
    before = world['queue'].get(OWNER, identifier)
    with pytest.raises(ValueError):
        world['queue'].apply_decision(OWNER, identifier, payload)
    assert world['queue'].get(OWNER, identifier) == before
    assert counts(world) == (0, 0, 0)


def test_owner_and_hidden_source_cannot_be_written(world):
    identifier = make_plan(world)
    with pytest.raises(KeyError):
        world['queue'].apply_decision('other-user', identifier, {'operation': 'pause', 'request_id': 'x', 'expected_revision': 1})
    world['crm']._db.execute('UPDATE crm_records SET hidden=1 WHERE owner=?', (OWNER,))
    with pytest.raises(KeyError):
        command(world, identifier, 'pause')


def test_candidates_are_one_plan_and_selection_is_not_consent(world):
    candidates = world['queue']._candidates([execution(), execution('2026-10-09T15:00:00+08:00')], NOW)
    identifier = make_plan(world, decision_mode='external', candidates=candidates, proposed_execution=None)
    result = command(world, identifier, 'select_candidate', candidate_id=candidates[1]['id'])
    assert result['arrangement']['selected_candidate_id'] == candidates[1]['id']
    assert 'waiting_for_agreement' in result['arrangement']['blocking_reasons']
    assert counts(world) == (0, 0, 0)
    repeated = world['queue']._candidates(list(reversed(candidates)), NOW, candidates)
    assert {value['id'] for value in repeated} == {value['id'] for value in candidates}


def test_personal_confirm_creates_schedule_without_execution_notice(world):
    identifier = make_plan(world)
    result = command(world, identifier, 'confirm_arrangement')
    assert result['effect'] == 'scheduled' and result['arrangement']['settling_state'] == 'settled'
    assert counts(world) == (1, 0, 0)
    repeat = command(world, identifier, 'confirm_arrangement', expected_task_revision=result['active_schedule']['revision'])
    assert repeat['effect'] == 'already_settled' and counts(world) == (1, 0, 0)


def test_external_confirm_without_attestation_saves_without_task(world):
    identifier = make_plan(world, decision_mode='external')
    result = command(world, identifier, 'confirm_arrangement')
    assert result['effect'] == 'saved' and 'waiting_for_agreement' in result['arrangement']['blocking_reasons']
    assert counts(world) == (0, 0, 0)


def test_date_only_then_clock_preserves_date_consent_without_extending_it(world):
    identifier = make_plan(world, decision_mode='external', proposed_execution=execution('2026-10-12'))
    date_result = command(world, identifier, 'confirm_arrangement', settlement_scope='date_only',
        agreement_attestation={'status': 'reported', 'scope': 'date_only', 'evidence': '对方已答应12号，几点尚未定'})
    assert date_result['effect'] == 'date_settled' and counts(world) == (0, 0, 0)
    date_field = date_result['arrangement']['agreement']['fields']['date']
    resumed = command(world, identifier, 'continue_set_time')
    assert resumed['arrangement']['settling_cycle_id'] == 2
    assert resumed['arrangement']['settle_deadline'] is resumed['arrangement']['next_check'] is None
    result = command(world, identifier, 'confirm_arrangement', proposed_execution=execution('2026-10-12T15:00:00+08:00'))
    assert result['effect'] == 'saved' and result['arrangement']['agreement_missing_fields'] == ['time']
    assert result['arrangement']['agreement']['fields']['date'] == date_field
    changed = command(world, identifier, 'confirm_arrangement', proposed_execution=execution('2026-10-13T15:00:00+08:00'))
    assert set(changed['arrangement']['agreement_missing_fields']) == {'date', 'time'}
    assert counts(world) == (0, 0, 0)


@pytest.mark.parametrize('spec,before,after', [
    ('2026-10-05', '2026-10-05T23:59:00', '2026-10-06T00:00:00'),
    ('2026-10-05上午', '2026-10-05T11:59:00', '2026-10-05T12:00:00'),
    ('2026-10-05T11:00:00+08:00', '2026-10-05T10:59:00', '2026-10-05T11:00:00'),
])
def test_resume_uses_precision_expiry_and_never_revives_obsolete_notice(world, spec, before, after):
    identifier = make_plan(world)
    command(world, identifier, 'set_check', next_check={'time_spec': normalize_time(spec, NOW, role='check')})
    old_id = world['queue'].get(OWNER, identifier)['next_check']['check_id']
    command(world, identifier, 'pause')
    world['now'][0] = datetime.fromisoformat(before).replace(tzinfo=SHANGHAI).timestamp()
    result = command(world, identifier, 'resume', reuse_next_check=True, followup_enabled=True)
    assert result['arrangement']['followup_enabled']
    assert result['arrangement']['next_check']['check_id'] != old_id
    assert result['arrangement']['settling_cycle_id'] == 1
    command(world, identifier, 'pause')
    world['now'][0] = datetime.fromisoformat(after).replace(tzinfo=SHANGHAI).timestamp()
    expired = command(world, identifier, 'resume', reuse_next_check=True, followup_enabled=True)
    assert not expired['arrangement']['followup_enabled']
    assert expired['arrangement']['next_check']['status'] == 'obsolete'
    assert expired['arrangement']['followup_disable_reason'] == 'needs_selection'


def test_pausing_and_abandoning_coordination_do_not_cancel_execution(world):
    identifier = make_plan(world, remind_minutes=15)
    scheduled = command(world, identifier, 'confirm_arrangement')
    active = scheduled['active_schedule']
    command(world, identifier, 'start_reschedule', expected_task_revision=active['revision'])
    command(world, identifier, 'pause')
    assert world['queue'].get(OWNER, identifier)['active_schedule'] == active
    command(world, identifier, 'resume', next_check=None)
    command(world, identifier, 'abandon_coordination', reason='不再商量改期')
    assert world['queue'].get(OWNER, identifier)['active_schedule'] == active
    assert world['crm']._db.execute("SELECT COUNT(*) FROM notifications WHERE status='queued'").fetchone()[0] == 1


def test_cancel_only_current_plan_and_task_version_is_required(world):
    first, second = make_plan(world), make_plan(world, proposed_execution=execution('2026-10-09T15:00:00+08:00'))
    scheduled = command(world, first, 'confirm_arrangement')
    command(world, second, 'confirm_arrangement')
    with pytest.raises(ValueError):
        command(world, first, 'cancel_activity')
    assert world['queue'].get(OWNER, first)['active_schedule']
    cancelled = command(world, first, 'cancel_activity', expected_task_revision=scheduled['active_schedule']['revision'])
    assert cancelled['active_schedule'] is None and world['queue'].get(OWNER, second)['active_schedule']
    row = world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (first,)).fetchone()
    data = json.loads(row['data_json'])
    assert data['status'] == data['booking'] == 'cancelled'
    assert world['crm']._db.execute('SELECT status FROM crm_records WHERE id=?', (row['record_id'],)).fetchone()[0] == 'done'


def test_conflicting_reschedule_keeps_old_task_and_execution_notice(world):
    first = make_plan(world, remind_minutes=15)
    second = make_plan(world, proposed_execution=execution('2026-10-09T15:00:00+08:00'))
    old = command(world, first, 'confirm_arrangement')['active_schedule']
    command(world, second, 'confirm_arrangement')
    command(world, first, 'start_reschedule', expected_task_revision=old['revision'])
    result = command(world, first, 'confirm_arrangement', proposed_execution=execution('2026-10-09T15:00:00+08:00'), expected_task_revision=old['revision'])
    assert result['effect'] == 'saved' and 'schedule_conflict' in result['arrangement']['blocking_reasons']
    assert result['active_schedule'] == old
    assert counts(world) == (2, 1, 0)


def test_valid_reschedule_reuses_task_id_and_new_cycle_does_not_inherit_deadline(world):
    identifier = make_plan(world)
    command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('本周内', NOW)})
    scheduled = command(world, identifier, 'confirm_arrangement')['active_schedule']
    started = command(world, identifier, 'start_reschedule', expected_task_revision=scheduled['revision'])
    assert started['arrangement']['settle_deadline'] is None
    result = command(world, identifier, 'confirm_arrangement', proposed_execution=execution('2026-10-10T15:00:00+08:00'), expected_task_revision=scheduled['revision'])
    assert result['effect'] == 'rescheduled' and result['active_schedule']['id'] == scheduled['id']
    assert counts(world) == (1, 0, 0)


def test_progress_only_consumes_check_after_explicit_performed_flag(world):
    identifier = make_plan(world)
    command(world, identifier, 'set_check', next_check={'time_spec': normalize_time('周五再問'.replace('問','问'), NOW, role='check')})
    before = world['queue'].get(OWNER, identifier)
    waiting = command(world, identifier, 'update_progress', progress_text='还在等他回复')['arrangement']
    assert waiting['next_check']['status'] == 'planned'
    handled = command(world, identifier, 'update_progress', progress_text='今天问过了，还没回复', check_id=before['next_check']['check_id'], check_handled=True)['arrangement']
    assert handled['next_check']['status'] == 'handled' and handled['next_check_at'] is None
    assert handled['silent_until']['at'] == before['silent_until']['at']
    with pytest.raises(ArrangementConflict):
        command(world, identifier, 'update_progress', progress_text='重复处理旧点', check_id=before['next_check']['check_id'], check_handled=True)


def test_hold_release_only_matches_latest_turn_and_generation(world):
    identifier = make_plan(world)
    first = world['flow'].submit(OWNER, {'text': '周五再问', 'request_id': 'first', 'plan_id': identifier, 'expected_revision': 1})
    # A failure callback can arrive after a later retry has acquired its hold.
    world['crm']._db.execute("UPDATE crm_secretary_turns SET status='failed' WHERE id=?", (first['id'],))
    second = world['flow'].submit(OWNER, {'text': '周六再问', 'request_id': 'second', 'plan_id': identifier, 'expected_revision': 1})
    with world['crm']._transaction() as db:
        generation1 = world['queue'].begin_hold(db, OWNER, identifier, first['id'], NOW)
        generation2 = world['queue'].begin_hold(db, OWNER, identifier, second['id'], NOW)
        assert generation2 > generation1
        assert not world['queue'].release_hold(db, OWNER, identifier, first['id'], generation1, NOW)
        assert world['queue'].release_hold(db, OWNER, identifier, second['id'], generation2, NOW)


def test_source_hidden_persists_gate_through_visibility_restore(world):
    identifier = make_plan(world)
    row = world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    with world['crm']._transaction() as db:
        world['queue'].source_hidden(db, OWNER, row['record_id'], NOW)
        db.execute('UPDATE crm_records SET hidden=0 WHERE id=?', (row['record_id'],))
    result = world['queue'].get(OWNER, identifier)
    assert not result['followup_enabled'] and result['followup_disable_reason'] == 'source_hidden'
    changed = command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('月底', NOW)})
    assert not changed['arrangement']['followup_enabled']


def test_natural_sync_has_no_business_revision_bump_or_recording_execution(world):
    identifier = make_plan(world)
    turn = world['flow'].submit(OWNER, {'text': '虚构新补充', 'request_id': 'sync', 'plan_id': identifier, 'expected_revision': 1})
    with world['crm']._transaction() as db:
        row = db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
        result = world['queue'].sync_from_turn(db, OWNER, identifier, {'source_authorized': False, 'changes': {'application_authority': {'kind': 'direct_user'}, 'proposed_execution': execution()}}, turn['id'], NOW, NOW)
    assert result['revision'] == row['revision'] and counts(world) == (0, 0, 0)


def test_natural_sync_time_change_opens_cycle_and_reuses_active_task(world):
    identifier = make_plan(world)
    old = command(world, identifier, 'confirm_arrangement')['active_schedule']
    turn = world['flow'].submit(OWNER, {'text': '改为10号下午三点', 'request_id': 'sync-reschedule', 'plan_id': identifier, 'expected_revision': 2})
    with world['crm']._transaction() as db:
        result = world['queue'].sync_from_turn(db, OWNER, identifier, {'source_authorized': True, 'expected_task_revision': old['revision'], 'changes': {'proposed_execution': execution('2026-10-10T15:00:00+08:00'), 'application_authority': {'kind': 'direct_user', 'evidence': '10号下午三点'}}}, turn['id'], NOW, NOW)
    assert result['effect'] == 'rescheduled' and result['arrangement']['settling_cycle_id'] == 2
    assert result['active_schedule']['id'] == old['id'] and counts(world) == (1, 0, 0)


def test_explicit_check_clear_does_not_auto_recreate_default_point(world):
    identifier = make_plan(world)
    command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('月底', NOW)})
    command(world, identifier, 'set_check', next_check={'time_spec': normalize_time('周五再问', NOW, role='check')})
    result = command(world, identifier, 'set_check', next_check=None)
    assert result['arrangement']['next_check'] is None and result['arrangement']['followup_enabled']
