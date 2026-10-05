"""AQ54/AQ62: two plans share check IDs but never share notice ownership."""
import json
import copy
import uuid

import pytest

from secretary.arrangement_time import normalize_time
from secretary.arrangement_semantics import checked_arrangement

from tests.test_arrangement_reminders import (
    world, plan, change, notice_rows, publish_all, OWNER, NOW,
)
from tests.test_arrangement_operations import (
    world as operation_world, make_plan, execution, command, OWNER as OP_OWNER,
)
from tests.test_arrangement_lifecycle import pending_turn, sync


def set_check(w, identifier, value):
    return w['queue'].apply_decision(OWNER, identifier, {
        'operation': 'set_check', 'request_id': uuid.uuid4().hex,
        'expected_revision': w['queue'].get(OWNER, identifier)['revision'],
        'next_check': {'time_spec': normalize_time(value, NOW, role='check'), 'action': '询问本次约见'},
    })


def snapshot_plans(w):
    return [dict(row) for row in w['crm']._db.execute('SELECT * FROM crm_secretary_plans ORDER BY id')]


def two_due_plans(w):
    return [plan(w, deadline='2026-10-04', check='2026-10-05', check_id='identical-check-id') for _ in range(2)]


def test_same_check_id_on_two_plans_keeps_four_cause_rows_and_two_groups(world):
    identifiers = two_due_plans(world)
    assert publish_all(world) == 4
    rows = notice_rows(world)
    assert len(rows) == 4 and len({row['id'] for row in rows}) == 4
    assert {row['plan_id'] for row in rows} == set(identifiers)
    checks = [row for row in rows if row['kind'] == 'check']
    assert len(checks) == 2
    assert {json.loads(row['payload_json'])['check_id'] for row in checks} == {'identical-check-id'}
    groups = world['service'].notices(OWNER)
    assert groups['total'] == groups['unread'] == 2
    assert len({item['group_key'] for item in groups['items']}) == 2
    assert len({item['local_date'] for item in groups['items']}) == 1
    for identifier in identifiers:
        item = next(item for item in groups['items'] if item['plan_id'] == identifier)
        assert len(item['member_ids']) == 2
        assert {cause['kind'] for cause in item['causes']} == {'check', 'deadline_overdue'}
        assert {row['id'] for row in rows if row['plan_id'] == identifier} == set(item['member_ids'])
    assert publish_all(world) == 0 and len(notice_rows(world)) == 4


def test_read_only_marks_selected_plan_members_without_consuming_checks_or_revision(world):
    identifiers = two_due_plans(world)
    assert publish_all(world) == 4
    before = snapshot_plans(world)
    first_group = next(item for item in world['service'].notices(OWNER)['items'] if item['plan_id'] == identifiers[0])
    # Any current member locates this same visible group, including deadline.
    deadline_member = next(cause['id'] for cause in first_group['causes'] if cause['kind'] == 'deadline_overdue')
    first_read = world['service'].read(OWNER, deadline_member)
    replay = world['service'].read(OWNER, deadline_member)
    assert first_read == replay and not first_read['unread']
    assert snapshot_plans(world) == before
    for identifier in identifiers:
        check = world['queue'].get(OWNER, identifier)['next_check']
        assert check['status'] == 'planned' and check.get('handled_at') is None
    rows = notice_rows(world)
    assert all(row['read_at'] == NOW for row in rows if row['plan_id'] == identifiers[0])
    assert all(row['read_at'] is None for row in rows if row['plan_id'] == identifiers[1])
    assert world['service'].notices(OWNER)['unread'] == 1
    assert world['crm']._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert world['crm']._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0


def test_handled_cause_keeps_its_deadline_and_other_plan_same_id_check(world):
    identifiers = two_due_plans(world)
    assert publish_all(world) == 4
    original = world['service'].notices(OWNER)
    first = next(item for item in original['items'] if item['plan_id'] == identifiers[0])
    world['service'].read(OWNER, first['id'])
    unchanged_other = dict(world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifiers[1],)).fetchone())
    check = world['queue'].get(OWNER, identifiers[0])['next_check']
    change(world, identifiers[0], data={'next_check': {**check, 'status': 'handled', 'handled_at': NOW}}, next_check_at=None)
    world['service'].sweep()
    current = world['service'].notices(OWNER)
    assert current['total'] == 2 and current['unread'] == 1
    first = next(item for item in current['items'] if item['plan_id'] == identifiers[0])
    second = next(item for item in current['items'] if item['plan_id'] == identifiers[1])
    assert [cause['kind'] for cause in first['causes']] == ['deadline_overdue']
    assert not first['unread'] and first['causes'][0]['read_at'] == NOW
    assert {cause['kind'] for cause in second['causes']} == {'check', 'deadline_overdue'}
    assert all(cause['read_at'] is None for cause in second['causes'])
    assert dict(world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifiers[1],)).fetchone()) == unchanged_other
    first_rows = notice_rows(world, identifiers[0])
    assert next(row for row in first_rows if row['kind'] == 'check')['status'] == 'obsolete'
    assert next(row for row in first_rows if row['kind'] == 'deadline_overdue')['status'] == 'published'
    assert publish_all(world) == 0
    before_read = snapshot_plans(world)
    world['service'].read(OWNER, second['id'])
    assert snapshot_plans(world) == before_read


def test_real_earlier_check_releases_both_due_causes_into_one_group(world):
    tomorrow = normalize_time('2026-10-06', NOW, role='check')['start_at']
    identifier = plan(world, check='2026-10-06', deadline='2026-10-04',
                      silent={'at': tomorrow, 'source': 'user_check'})
    world['service'].sweep()
    original = next(row for row in notice_rows(world) if row['kind'] == 'deadline_overdue')
    assert original['available_at'] == tomorrow and original['attempts'] == 0
    assert world['service'].claim_due() is None
    changed = set_check(world, identifier, '2026-10-05T09:00:00+08:00')
    rebound = next(row for row in notice_rows(world) if row['id'] == original['id'])
    assert rebound['followup_version'] == changed['arrangement']['followup_version']
    # The queue has already rebound the version; worker policy must still move
    # its old quiet gate earlier, rather than wait for a new version mismatch.
    assert publish_all(world) == 2
    deadline = next(row for row in notice_rows(world) if row['id'] == original['id'])
    assert deadline['published_at'] == NOW and deadline['available_at'] <= NOW
    groups = world['service'].notices(OWNER)
    assert groups['total'] == 1
    assert {cause['kind'] for cause in groups['items'][0]['causes']} == {'check', 'deadline_overdue'}
    assert world['queue'].get(OWNER, identifier)['next_check']['status'] == 'planned'
    assert world['crm']._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_real_later_check_defers_untouched_deadline_notice(world):
    identifier = plan(world, check='2026-10-05T09:00:00+08:00', deadline='2026-10-04')
    world['service'].sweep()
    old = next(row for row in notice_rows(world) if row['kind'] == 'deadline_overdue')
    set_check(world, identifier, '2026-10-06T09:00:00+08:00')
    world['service'].sweep()
    row = next(row for row in notice_rows(world) if row['id'] == old['id'])
    assert row['available_at'] == normalize_time('2026-10-06T09:00:00+08:00', NOW, role='check')['at']
    assert row['attempts'] == 0 and world['service'].claim_due() is None


def test_retry_floor_survives_quiet_postponement_then_advance_and_two_causes_group(world):
    identifier = plan(world, check='2026-10-05T09:00:00+08:00', deadline='2026-10-04')
    claimed = world['service'].claim_due()
    assert claimed['kind'] == 'deadline_overdue'
    assert world['service'].retry(claimed['id'], claimed['token'])
    retry_at = NOW + world['service'].RETRY_BASE_SECONDS
    failed = next(row for row in notice_rows(world) if row['id'] == claimed['id'])
    assert json.loads(failed['payload_json'])['retry_not_before'] == retry_at
    set_check(world, identifier, '2026-10-06T09:00:00+08:00')
    world['service'].sweep()
    assert world['service'].claim_due() is None
    postponed = next(row for row in notice_rows(world) if row['id'] == claimed['id'])
    assert postponed['available_at'] > retry_at
    assert json.loads(postponed['payload_json'])['retry_not_before'] == retry_at
    set_check(world, identifier, '2026-10-05T09:00:00+08:00')
    world['service'].sweep()
    advanced = next(row for row in notice_rows(world) if row['id'] == claimed['id'])
    assert advanced['available_at'] == retry_at and advanced['attempts'] == 1
    assert json.loads(advanced['payload_json'])['retry_not_before'] == retry_at
    # The check can publish now; the failed deadline delivery keeps its real
    # thirty-second backoff, then joins that same plan/day group.
    assert publish_all(world) == 1
    world['clock'][0] = retry_at - 1
    assert world['service'].claim_due() is None
    world['clock'][0] = retry_at
    assert publish_all(world) == 1
    groups = world['service'].notices(OWNER)
    assert groups['total'] == 1 and len(groups['items'][0]['causes']) == 2
    final = next(row for row in notice_rows(world) if row['id'] == claimed['id'])
    assert final['attempts'] == 2 and final['published_at'] == retry_at


def test_retry_floor_survives_direct_version_reconciliation(world):
    identifier = plan(world, check=None, deadline='2026-10-04')
    claimed = world['service'].claim_due()
    assert world['service'].retry(claimed['id'], claimed['token'])
    retry_at = NOW + world['service'].RETRY_BASE_SECONDS
    change(world, identifier, data={'last_progress': {'text': '仍在等待'}})
    assert world['service'].sweep()['rebound'] == 1
    row = notice_rows(world)[0]
    assert row['available_at'] == retry_at and json.loads(row['payload_json'])['retry_not_before'] == retry_at
    assert world['service'].claim_due() is None
    world['clock'][0] = retry_at
    assert publish_all(world) == 1


def test_invalidated_lease_without_failed_delivery_follows_earlier_quiet(world):
    identifier = plan(world, check='2026-10-06', deadline='2026-10-04')
    claimed = world['service'].claim_due()
    assert claimed['kind'] == 'deadline_overdue' and claimed['attempts'] == 1
    set_check(world, identifier, '2026-10-06T09:00:00+08:00')
    world['service'].sweep()
    assert world['service'].claim_due() is None
    set_check(world, identifier, '2026-10-05T09:00:00+08:00')
    assert not world['service'].publish(claimed['id'], claimed['token'])
    assert publish_all(world) == 2
    row = next(row for row in notice_rows(world) if row['id'] == claimed['id'])
    assert row['attempts'] == 2 and row['available_at'] <= NOW
    assert json.loads(row['payload_json'])['retry_not_before'] is None


def test_legacy_retry_without_marker_remains_safe_across_repeated_rebinding(world):
    identifier = plan(world, check=None, deadline='2026-10-04')
    claimed = world['service'].claim_due()
    assert world['service'].retry(claimed['id'], claimed['token'])
    row = notice_rows(world)[0]
    payload = json.loads(row['payload_json'])
    payload.pop('retry_not_before')
    legacy_floor = NOW + 120
    world['crm']._db.execute('UPDATE crm_arrangement_notifications SET payload_json=?,available_at=? WHERE id=?',
                             (json.dumps(payload), legacy_floor, row['id']))
    change(world, identifier, data={'last_progress': {'text': '等待对方反馈'}})
    world['service'].sweep()
    set_check(world, identifier, '2026-10-05T09:00:00+08:00')
    world['service'].sweep()
    world['service'].sweep()
    current = next(item for item in notice_rows(world) if item['id'] == row['id'])
    assert current['available_at'] == legacy_floor
    assert json.loads(current['payload_json'])['retry_not_before'] == legacy_floor
    assert publish_all(world) == 1
    world['clock'][0] = legacy_floor
    assert publish_all(world) == 1


def confirmed_external(w, at='2026-10-12T15:00:00+08:00'):
    identifier = make_plan(w, decision_mode='external', proposed_execution=execution(at), remind_minutes=15)
    result = command(w, identifier, 'confirm_arrangement', agreement_attestation={
        'status': 'reported', 'scope': 'execution_time', 'evidence': '对方已经确定12号下午三点'})
    assert result['effect'] == 'scheduled'
    return identifier, result


@pytest.mark.parametrize('text,status,scope', [
    ('下午三点不同意，对方已确定12号', 'reported', 'execution_time'),
    ('该钟点不同意', 'unknown', None),
    ('下午三点不同意', 'unknown', None),
    ('下午三点未确认', 'unknown', None),
    ('15点不同意', 'unknown', None),
    ('15时未确认', 'unknown', None),
])
def test_present_turn_clock_rejection_preserves_date_and_old_execution(operation_world, text, status, scope):
    w = operation_world
    identifier, original = confirmed_external(w)
    source = dict(w['crm']._db.execute('SELECT * FROM crm_records WHERE id=?', (original['arrangement']['record_id'],)).fetchone())
    task = dict(w['crm']._db.execute('SELECT * FROM tasks WHERE id=?', (original['active_schedule']['id'],)).fetchone())
    notices = [dict(row) for row in w['crm']._db.execute('SELECT * FROM notifications')]
    date_field = copy.deepcopy(original['arrangement']['agreement']['fields']['date'])
    turn = pending_turn(w, identifier, text, uuid.uuid4().hex)
    attestation = {'status': status, 'evidence': text}
    if scope is not None:
        attestation['scope'] = scope
    result = sync(w, identifier, turn, {'expected_task_revision': task['revision'], 'changes': {
        'agreement': attestation, 'application_authority': {'kind': 'direct_user', 'evidence': text}}})
    arrangement = result['arrangement']
    assert arrangement['settling_state'] == 'pending' and arrangement['settling_cycle_id'] == 2
    assert set(arrangement['agreement']['fields']) == {'date'}
    assert arrangement['agreement']['fields']['date']['signature'] == date_field['signature']
    if status == 'unknown':
        assert arrangement['agreement']['fields']['date'] == date_field
    assert arrangement['agreement_missing_fields'] == ['time'] and not arrangement['can_apply']
    assert arrangement['application_authority']['kind'] == 'none'
    assert arrangement['agreement']['rejection_turn_id'] == turn['id']
    assert result['active_schedule'] == original['active_schedule']
    assert dict(w['crm']._db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()) == task
    assert [dict(row) for row in w['crm']._db.execute('SELECT * FROM notifications')] == notices
    assert dict(w['crm']._db.execute('SELECT * FROM crm_records WHERE id=?', (source['id'],)).fetchone()) == source


@pytest.mark.parametrize('text,evidence', [
    ('下午四点不同意', '下午四点不同意'),
    ('13号下午三点不同意', '13号下午三点不同意'),
    ('不是下午三点不同意，仍按原时间继续', '不是下午三点不同意，仍按原时间继续'),
    ('下午三点不是不同意，仍按原時間继续', '下午三点不是不同意，仍按原時間继续'),
    ('如果下午三点不同意，再讨论', '如果下午三点不同意，再讨论'),
    ('材料已经准备好了', '下午三点不同意'),
])
def test_other_clock_or_unverified_rejection_does_not_erase_current_consent(operation_world, text, evidence):
    w = operation_world
    identifier, original = confirmed_external(w)
    turn = pending_turn(w, identifier, text, uuid.uuid4().hex)
    result = sync(w, identifier, turn, {'changes': {'agreement': {'status': 'unknown', 'evidence': evidence}}})
    assert result['arrangement']['settling_state'] == 'settled'
    assert result['arrangement']['settling_cycle_id'] == 1
    assert result['arrangement']['agreement'] == original['arrangement']['agreement']
    assert result['active_schedule'] == original['active_schedule']


def test_date_only_checkbox_without_clock_rejection_keeps_prior_verified_time(operation_world):
    w = operation_world
    identifier, original = confirmed_external(w)
    result = command(w, identifier, 'confirm_arrangement', expected_task_revision=original['active_schedule']['revision'],
        agreement_attestation={'status': 'reported', 'scope': 'date_only', 'evidence': '我核对了12号'})
    assert result['arrangement']['agreement']['fields']['time'] == original['arrangement']['agreement']['fields']['time']
    assert result['arrangement']['settling_state'] == 'settled' and result['active_schedule'] == original['active_schedule']


def test_checked_natural_report_keeps_positive_date_and_revokes_rejected_clock(operation_world):
    w = operation_world
    identifier, original = confirmed_external(w)
    text = '下午三点不同意，对方已确定12号'
    turn = pending_turn(w, identifier, text, uuid.uuid4().hex)
    semantic = checked_arrangement({'intent': 'update'}, text, NOW, w['queue'].get(OP_OWNER, identifier))
    assert semantic['changes']['agreement']['scope'] == 'date_only'
    result = sync(w, identifier, turn, semantic)
    assert result['arrangement']['settling_state'] == 'pending'
    assert result['arrangement']['agreement_missing_fields'] == ['time']
    assert result['arrangement']['agreement']['fields']['date']['signature'] == original['arrangement']['agreement']['fields']['date']['signature']
    assert result['active_schedule'] == original['active_schedule']


def test_legacy_whole_content_consent_retains_its_proven_date_on_clock_rejection(operation_world):
    w = operation_world
    identifier, original = confirmed_external(w)
    row = w['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    data = json.loads(row['data_json'])
    data['agreement'].pop('fields')
    w['crm']._db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?', (json.dumps(data), identifier))
    turn = pending_turn(w, identifier, '该钟点不同意', uuid.uuid4().hex)
    result = sync(w, identifier, turn, {'changes': {'agreement': {'status': 'unknown', 'evidence': '该钟点不同意'}}})
    assert set(result['arrangement']['agreement']['fields']) == {'date'}
    assert result['arrangement']['agreement_missing_fields'] == ['time']
    assert result['active_schedule'] == original['active_schedule']


@pytest.mark.parametrize('clock,text', [
    ('03:00:00', '凌晨三点不同意'),
    ('00:00:00', '零点不同意'),
    ('00:00:00', '凌晨零点未确认'),
    ('00:00:00', '午夜未确认'),
])
def test_zero_and_dawn_clock_rejection_binds_actual_time(operation_world, clock, text):
    w = operation_world
    identifier, original = confirmed_external(w, at=f'2026-10-12T{clock}+08:00')
    turn = pending_turn(w, identifier, text, uuid.uuid4().hex)
    semantic = checked_arrangement({'intent': 'update'}, text, NOW, w['queue'].get(OP_OWNER, identifier))
    assert semantic['changes']['agreement']['status'] == 'unknown'
    result = sync(w, identifier, turn, semantic)
    assert result['arrangement']['settling_state'] == 'pending'
    assert result['arrangement']['agreement_missing_fields'] == ['time']
    assert result['active_schedule'] == original['active_schedule']
