"""Natural and page edits preserve execution and persistent reminder gates."""
import copy
import json
from datetime import datetime

import pytest

from secretary.arrangement_queue import ArrangementConflict
from secretary.arrangement_time import normalize_time
from secretary.store import SHANGHAI
from tests.test_arrangement_operations import world, make_plan, execution, command, counts, OWNER, NOW


def pending_turn(w, identifier, text, key):
    return w['flow'].submit(OWNER, {'text': text, 'request_id': key, 'plan_id': identifier,
        'expected_revision': w['queue'].get(OWNER, identifier)['revision']})


def sync(w, identifier, turn, semantic):
    with w['crm']._transaction() as db:
        return w['queue'].sync_from_turn(db, OWNER, identifier,
            {'source_authorized': True, **semantic}, turn['id'], turn['created_at'], w['now'][0])


def test_new_plan_defaults_followup_enabled_without_implicit_check(world):
    identifier = make_plan(world)
    world['crm']._db.execute("UPDATE crm_secretary_plans SET followup_enabled=0,followup_disable_reason='legacy_no_opt_in' WHERE id=?", (identifier,))
    turn = pending_turn(world, identifier, '想约客户见面', 'new-default')
    result = sync(world, identifier, turn, {'new_plan': True, 'changes': {}})['arrangement']
    assert result['followup_enabled'] and result['next_check'] is None
    assert counts(world) == (0, 0, 0)


def test_first_explicit_legacy_deadline_enables_but_user_disabled_remains_off(world):
    identifier = make_plan(world)
    world['crm']._db.execute("UPDATE crm_secretary_plans SET followup_enabled=0,followup_disable_reason='legacy_no_opt_in' WHERE id=?", (identifier,))
    result = command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('本周内', NOW)})
    assert result['arrangement']['followup_enabled']
    command(world, identifier, 'set_check', next_check=None, followup_enabled=False)
    result = command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('月底', NOW)})
    assert not result['arrangement']['followup_enabled'] and result['arrangement']['followup_disable_reason'] == 'user_disabled'


def test_deadline_registration_is_stable_for_same_value_and_progress(world):
    identifier = make_plan(world)
    value = {'time_spec': normalize_time('本周内', NOW)}
    command(world, identifier, 'set_deadline', settle_deadline=value)
    before = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])['deadline_registered_at']
    world['now'][0] += 60
    command(world, identifier, 'update_progress', progress_text='还在等待回复')
    command(world, identifier, 'set_deadline', settle_deadline=value)
    data = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    assert data['deadline_registered_at'] == before


def test_natural_partial_date_agreement_never_binds_existing_clock(world):
    identifier = make_plan(world, decision_mode='external', proposed_execution=execution('2026-10-12T15:00:00+08:00'))
    turn = pending_turn(world, identifier, '只确认12号，几点没定', 'partial-date')
    result = sync(world, identifier, turn, {'changes': {
        'agreement': {'status': 'reported', 'scope': 'execution_time', 'evidence': '只确认12号，几点没定'},
        'application_authority': {'kind': 'direct_user', 'evidence': '只确认12号'}}})
    assert result['effect'] == 'saved'
    assert result['arrangement']['agreement']['scope'] == 'date_only'
    assert result['arrangement']['agreement_missing_fields'] == ['time']
    assert counts(world) == (0, 0, 0)


def test_natural_reminder_change_updates_same_task_without_reopening_cycle(world):
    identifier = make_plan(world)
    old = command(world, identifier, 'confirm_arrangement')['active_schedule']
    turn = pending_turn(world, identifier, '提前一个小时提醒我', 'change-reminder')
    result = sync(world, identifier, turn, {'expected_task_revision': old['revision'], 'changes': {
        'execution_reminder': {'enabled': True, 'minutes': 60, 'evidence': '提前一个小时'}}})
    assert result['active_schedule']['id'] == old['id']
    assert result['arrangement']['settling_cycle_id'] == 1
    notice = world['crm']._db.execute("SELECT * FROM notifications WHERE status='queued'").fetchone()
    assert notice['due_at'] == old['remind_at'] - 3600
    assert world['crm']._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_title_attachment_only_sync_does_not_reopen_or_reset_notice(world):
    identifier = make_plan(world, remind_minutes=15)
    old = command(world, identifier, 'confirm_arrangement')['active_schedule']
    turn = pending_turn(world, identifier, '补一份准备材料', 'title-only')
    with world['crm']._transaction() as db:
        notices = [dict(row) for row in db.execute('SELECT * FROM notifications')]
        data = json.loads(db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
        data['title'] = '修改展示标题'
        db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?', (json.dumps(data), identifier))
        result = world['queue'].sync_from_turn(db, OWNER, identifier, {'source_authorized': True, 'changes': {}}, turn['id'], NOW, NOW)
        assert [dict(row) for row in db.execute('SELECT * FROM notifications')] == notices
    assert result['active_schedule'] == old
    assert result['arrangement']['settling_state'] == 'settled' and result['arrangement']['settling_cycle_id'] == 1


def test_expired_hold_rejects_late_result_without_changing_schedule(world):
    identifier = make_plan(world)
    turn = pending_turn(world, identifier, '安排下周三下午三点', 'late-result')
    with world['crm']._transaction() as db:
        generation = world['queue'].begin_hold(db, OWNER, identifier, turn['id'], NOW)
        db.execute('UPDATE crm_secretary_turns SET data_json=? WHERE id=?', (json.dumps({'arrangement_hold_generation': generation}), turn['id']))
    world['now'][0] = NOW + 241
    before = world['queue'].get(OWNER, identifier)
    with pytest.raises(ArrangementConflict):
        sync(world, identifier, turn, {'changes': {'application_authority': {'kind': 'direct_user'}}})
    assert world['queue'].get(OWNER, identifier) == before and counts(world) == (0, 0, 0)


def test_recap_does_not_reopen_even_with_candidate_data(world):
    identifier = make_plan(world, status='recapped')
    turn = pending_turn(world, identifier, '实际见面已经结束', 'actual-recap')
    result = sync(world, identifier, turn, {'intent': 'recap', 'changes': {'proposed_execution': execution('2026-10-12')}})
    assert result['arrangement']['settling_state'] == 'settled' and not result['arrangement']['followup_enabled']
    raw = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    assert raw['status'] == 'recapped' and counts(world) == (0, 0, 0)


def test_date_only_natural_supplement_clears_proposed_clock_but_keeps_old_active(world):
    identifier = make_plan(world)
    old = command(world, identifier, 'confirm_arrangement')['active_schedule']
    turn = pending_turn(world, identifier, '改到12号，几点尚未定', 'new-date-only')
    result = sync(world, identifier, turn, {'expected_task_revision': old['revision'], 'changes': {'proposed_execution': execution('2026-10-12')}})
    assert result['active_schedule'] == old
    raw = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    assert raw['date'] == '2026-10-12' and raw['start_at'] is None
    assert result['arrangement']['settling_state'] == 'pending'


def test_execution_week_remains_range_without_becoming_monday_appointment(world):
    identifier = make_plan(world, proposed_execution=None)
    turn = pending_turn(world, identifier, '本周去拜访客户', 'execution-week')
    result = sync(world, identifier, turn, {'changes': {'proposed_execution': execution('本周去拜访客户')}})
    proposal = result['arrangement']['proposed_execution']['time_spec']
    assert proposal['window'] == 'calendar' and proposal['end_date'] == '2026-10-12'
    assert {'missing_date', 'missing_clock'} <= set(result['arrangement']['blocking_reasons'])
    raw = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    assert raw['date'] is raw['start_at'] is None and counts(world) == (0, 0, 0)


def test_partial_report_does_not_approve_place_or_participation_conditions(world):
    identifier = make_plan(world, decision_mode='external', location_required=True,
        proposed_execution=execution('2026-10-12T15:00:00+08:00', place='客户办公室', conditions=['王主任参加']))
    turn = pending_turn(world, identifier, '对方已确定12号，几点没定', 'partial-with-conditions')
    result = sync(world, identifier, turn, {'changes': {
        'agreement': {'status': 'reported', 'scope': 'date_only', 'evidence': '对方已确定12号，几点没定'},
        'application_authority': {'kind': 'direct_user', 'evidence': '对方已确定12号'}}})
    assert set(result['arrangement']['agreement']['fields']) == {'date'}
    assert set(result['arrangement']['agreement_missing_fields']) == {'time', 'place', 'conditions'}
    assert counts(world) == (0, 0, 0)


def test_time_checkbox_only_does_not_approve_extra_conditions(world):
    identifier = make_plan(world, decision_mode='external', location_required=True,
        proposed_execution=execution(place='客户办公室', conditions=['王主任参加']))
    result = command(world, identifier, 'confirm_arrangement', agreement_attestation={
        'status': 'reported', 'scope': 'execution_time', 'evidence': '用户确认对方同意核对的时段'})
    assert result['effect'] == 'saved'
    assert set(result['arrangement']['agreement_missing_fields']) == {'place', 'conditions'}
    assert counts(world) == (0, 0, 0)


def test_signed_fields_must_match_displayed_values_and_preserve_unchanged_evidence(world):
    identifier = make_plan(world, decision_mode='external', location_required=True,
        proposed_execution=execution(place='客户办公室', conditions=['王主任参加']))
    displayed = world['queue'].get(OWNER, identifier)['agreement_field_signatures']
    with pytest.raises(ArrangementConflict):
        command(world, identifier, 'confirm_arrangement', agreement_attestation={
            'status': 'reported', 'scope': 'execution_time', 'evidence': '对方已同意时段',
            'fields': {'place': {'signature': 'stale-signature', 'evidence': '核对地点客户办公室'}}})
    assert world['queue'].get(OWNER, identifier)['revision'] == 1 and counts(world) == (0, 0, 0)
    confirmed = command(world, identifier, 'confirm_arrangement', agreement_attestation={
        'status': 'reported', 'scope': 'execution_time', 'evidence': '对方已同意时段',
        'fields': {'place': {'signature': displayed['place'], 'evidence': '明确核对客户办公室'},
                   'conditions': {'signature': displayed['conditions'], 'evidence': '明确核对王主任参加'}}})
    assert confirmed['effect'] == 'scheduled'
    active = confirmed['active_schedule']
    old_place = confirmed['arrangement']['agreement']['fields']['place']
    command(world, identifier, 'start_reschedule', expected_task_revision=active['revision'])
    same_place = command(world, identifier, 'confirm_arrangement', proposed_execution=execution('2026-10-09T15:00:00+08:00', place='客户办公室', conditions=['王主任参加']),
        expected_task_revision=active['revision'], agreement_attestation={'status': 'reported', 'scope': 'execution_time', 'evidence': '只重新核对9号下午三点'})
    assert same_place['effect'] == 'rescheduled'
    assert same_place['arrangement']['agreement']['fields']['place'] == old_place


def test_natural_field_evidence_needs_exact_value_and_positive_clause(world):
    identifier = make_plan(world, decision_mode='external', location_required=True,
        proposed_execution=execution('2026-10-12', place='客户办公室', conditions=['王主任参加']))
    turn = pending_turn(world, identifier, '对方确定12号，地点可能客户办公室，王主任参加需要确认', 'needs-confirmation')
    result = sync(world, identifier, turn, {'changes': {'agreement': {'status': 'reported', 'scope': 'date_only',
        'evidence': '对方确定12号，地点可能客户办公室，王主任参加需要确认'}}})
    assert set(result['arrangement']['agreement']['fields']) == {'date'}
    data = json.loads(world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    world['queue']._agreement(data, {'status': 'reported', 'scope': 'date_only',
        'evidence': '对方确定12号，确定在客户办公室，已确认王主任参加'}, turn_id=turn['id'])
    assert {'date', 'place', 'conditions'} == set(data['agreement']['fields'])


def test_date_only_attestation_rejects_unreviewed_time_field(world):
    identifier = make_plan(world, decision_mode='external')
    displayed = world['queue'].get(OWNER, identifier)['agreement_field_signatures']
    with pytest.raises(ValueError):
        command(world, identifier, 'confirm_arrangement', settlement_scope='date_only', agreement_attestation={
            'status': 'reported', 'scope': 'date_only', 'evidence': '只同意日期',
            'fields': {'time': {'signature': displayed['time'], 'evidence': '未核对的新钟点'}}})
    assert counts(world) == (0, 0, 0)


def test_cancelled_plan_reopens_dto_without_reviving_cancelled_task(world):
    identifier = make_plan(world, remind_minutes=15)
    active = command(world, identifier, 'confirm_arrangement')['active_schedule']
    command(world, identifier, 'cancel_activity', expected_task_revision=active['revision'])
    restarted = command(world, identifier, 'start_reschedule')
    assert restarted['arrangement']['settling_state'] == 'pending' and restarted['active_schedule'] is None
    row = world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    data = json.loads(row['data_json'])
    assert data['status'] == 'tentative' and data['booking'] == 'unknown' and data['task_id'] == active['id']
    assert world['crm']._db.execute('SELECT status FROM tasks WHERE id=?', (active['id'],)).fetchone()[0] == 'cancelled'
    assert world['crm']._db.execute("SELECT COUNT(*) FROM notifications WHERE status='queued'").fetchone()[0] == 0


@pytest.mark.parametrize('days,expected', [(2, '2026-10-05'), (3, '2026-10-07'), (7, '2026-10-07'), (8, '2026-10-12')])
def test_suggested_check_policy_calendar_boundaries(world, days, expected):
    identifier = make_plan(world)
    from datetime import timedelta
    day = datetime.fromtimestamp(NOW, SHANGHAI).date() + timedelta(days=days)
    result = command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time(day.isoformat(), NOW)})
    assert result['arrangement']['suggested_next_check']['time_spec']['date'] == expected
    assert result['arrangement']['next_check'] is None


def test_overdue_deadline_does_not_suggest_past_or_looping_check(world):
    identifier = make_plan(world)
    result = command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('2026-10-04', NOW)})
    assert result['arrangement']['suggested_next_check'] is None and result['arrangement']['next_check'] is None


def test_auto_check_preference_survives_restart_without_rewriting_existing_plan(world):
    from secretary.customer_store import CustomerStore
    from secretary.sales_workspace import SalesWorkspace
    from secretary.secretary_flow import SecretaryFlow
    import asyncio
    identifier = make_plan(world)
    command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('2026-10-13', NOW)})
    settings = world['flow'].settings(OWNER)
    saved = world['flow'].update_settings(OWNER, {'expected_revision': settings['revision'], 'arrangement_auto_check': True})
    assert saved['arrangement_auto_check'] and world['queue'].get(OWNER, identifier)['next_check'] is None
    filename = world['crm']._db.execute('PRAGMA database_list').fetchone()['file']
    restarted = CustomerStore(filename)
    try:
        flow = SecretaryFlow(restarted, SalesWorkspace(restarted, clock=lambda: NOW), asyncio.Lock(), clock=lambda: NOW)
        assert flow.settings(OWNER)['arrangement_auto_check']
        assert flow.arrangements.get(OWNER, identifier)['next_check'] is None
        current = flow.arrangements.get(OWNER, identifier)
        result = flow.arrangements.apply_decision(OWNER, identifier, {'operation': 'set_deadline', 'request_id': 'enable-new-point',
            'expected_revision': current['revision'], 'settle_deadline': {'time_spec': normalize_time('2026-10-14', NOW)}})
        assert result['arrangement']['next_check']['origin'] == 'automatic'
        assert result['arrangement']['next_check']['time_spec']['date'] == '2026-10-12'
    finally:
        restarted.close()


def test_date_to_time_to_confirmed_15_oclock_is_one_real_activity(world):
    identifier = make_plan(world, decision_mode='external', proposed_execution=execution('2026-10-12'))
    command(world, identifier, 'set_deadline', settle_deadline={'time_spec': normalize_time('2026-10-04', NOW)})
    date = command(world, identifier, 'confirm_arrangement', settlement_scope='date_only', agreement_attestation={
        'status': 'reported', 'scope': 'date_only', 'evidence': '对方确定12号，几点没定'})
    assert date['effect'] == 'date_settled' and counts(world) == (0, 0, 0)
    continued = command(world, identifier, 'continue_set_time')
    assert continued['arrangement']['settling_cycle_id'] == 2
    assert continued['arrangement']['settle_deadline'] is continued['arrangement']['next_check'] is None
    time = command(world, identifier, 'confirm_arrangement', proposed_execution=execution('2026-10-12T15:00:00+08:00'))
    assert time['effect'] == 'saved' and time['arrangement']['agreement_missing_fields'] == ['time']
    final = command(world, identifier, 'confirm_arrangement', agreement_attestation={
        'status': 'reported', 'scope': 'execution_time', 'evidence': '对方现在确认12号15点'})
    assert final['effect'] == 'scheduled' and final['active_schedule']['remind_at'] == datetime(2026, 10, 12, 15, tzinfo=SHANGHAI).timestamp()
    assert final['arrangement']['settling_cycle_id'] == 2
    assert counts(world) == (1, 0, 0)
    assert world['crm']._db.execute('SELECT COUNT(*) FROM crm_secretary_plans').fetchone()[0] == 1
