"""Arrangement migration uses synthetic SQLite and preserves legacy business rows."""
import asyncio
import json

import pytest

from secretary.arrangement_queue import ArrangementQueue, PLAN_COLUMNS, content_signature
from secretary.arrangement_time import normalize_time, time_end, time_start
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from tests.test_secretary_flow import NOW

OWNER = 'fictional-arrangement-owner'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-arrangements.sqlite3')
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    flow = SecretaryFlow(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    yield {'crm': crm, 'workspace': workspace, 'flow': flow}
    crm.close()


def add_plan(w, data, *, owner=OWNER, hidden=False):
    crm = w['crm']
    record = crm.create_record(owner, {'title': '虚构安排原话', 'content': '本周定下下个月活动', 'status': 'following'}, NOW)
    with crm._transaction() as db:
        if hidden:
            db.execute('UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?', (owner, record['id']))
        identifier = db.execute('''INSERT INTO crm_secretary_plans(owner,record_id,data_json,revision,created_at,updated_at)
            VALUES(?,?,?,4,?,?)''', (owner, record['id'], json.dumps(data, ensure_ascii=False), NOW, NOW)).lastrowid
    return identifier


def task(w, state='pending', owner=OWNER, delta=3600):
    crm = w['crm']
    crm.execute(owner, 'make-' + owner + str(delta), {'action': 'propose', 'title': '旧有效执行安排', 'remind_at': NOW + delta}, NOW)
    proposal = crm._db.execute('SELECT MAX(id) FROM proposals').fetchone()[0]
    crm.execute(owner, 'confirm-' + owner + str(delta), {'action': 'confirm', 'proposal_id': proposal}, NOW)
    identifier = crm.get_proposal(owner, proposal)['task_id']
    if state != 'pending':
        crm.execute(owner, state + str(delta), {'action': 'complete' if state == 'completed' else 'cancel', 'task_id': identifier}, NOW)
    return identifier


def legacy_rows(w):
    db = w['crm']._db
    values = {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
              for table in ('crm_records', 'tasks', 'notifications', 'proposals')}
    values['plans'] = [dict(row) for row in db.execute('SELECT id,owner,record_id,visit_id,data_json,revision,created_at,updated_at FROM crm_secretary_plans ORDER BY id')]
    return values


def queue(w):
    return ArrangementQueue(w['crm'], w['workspace'], asyncio.Lock(), clock=lambda: NOW, flow=w['flow'])


def test_old_schema_mapping_preserves_json_ids_tasks_and_execution_notices(world):
    pending_task = task(world)
    done_task = task(world, 'completed', delta=7200)
    cancelled_task = task(world, 'cancelled', delta=10800)
    foreign_task = task(world, owner='other-owner', delta=14400)
    cases = [
        ({'title': '旧日程', 'task_id': pending_task, 'date': '2026-10-08', 'unknown_future_field': {'keep': 1}}, False, 'settled'),
        ({'title': '已经完成', 'task_id': done_task, 'status': 'recapped'}, False, 'settled'),
        ({'title': '明确取消', 'task_id': cancelled_task, 'booking': 'cancelled'}, False, 'abandoned'),
        ({'title': '只有复盘', 'status': 'recapped'}, False, 'pending'),
        ({'title': '未知计划', 'task_id': foreign_task}, False, 'pending'),
        ({'title': '隐藏待定'}, True, 'pending'),
    ]
    plans = [(add_plan(world, data, hidden=hidden), hidden, expected) for data, hidden, expected in cases]
    before = legacy_rows(world)
    service = queue(world)
    assert legacy_rows(world) == before
    columns = {row['name'] for row in world['crm']._db.execute('PRAGMA table_info(crm_secretary_plans)')}
    assert set(PLAN_COLUMNS) <= columns
    for identifier, hidden, expected in plans:
        row = world['crm']._db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
        assert row['settling_state'] == expected and row['schema_version'] == 1
        assert row['followup_enabled'] == 0
        assert row['followup_disable_reason'] == ('source_hidden' if hidden else 'legacy_no_opt_in')
        assert row['deadline_end_at'] is row['next_check_at'] is None
    assert world['crm']._db.execute('SELECT COUNT(*) FROM crm_arrangement_notifications').fetchone()[0] == 0
    assert world['crm']._db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    changes = world['crm']._db.total_changes
    service.upgrade_schema()
    assert world['crm']._db.total_changes == changes and legacy_rows(world) == before


def test_get_is_pure_owner_visible_and_never_infers_new_deadline(world):
    identifier = add_plan(world, {'title': '虚构培训', 'date': '2026-11-10', 'booking': 'confirmed', 'remind_minutes': 60})
    hidden = add_plan(world, {'title': '隐藏安排'}, hidden=True)
    service = queue(world)
    before = legacy_rows(world)
    changes = world['crm']._db.total_changes
    result = service.get(OWNER, identifier)
    assert result['settle_deadline'] is result['next_check'] is None
    assert result['proposed_execution']['time_spec']['date'] == '2026-11-10'
    assert result['proposed_execution']['time_spec']['precision'] == 'date'
    assert result['agreement']['status'] == 'unknown' and result['application_authority']['kind'] == 'none'
    assert result['active_schedule'] is None and not result['can_apply']
    assert 'missing_clock' in result['blocking_reasons']
    with pytest.raises(KeyError):
        service.get('other-owner', identifier)
    with pytest.raises(KeyError):
        service.get(OWNER, hidden)
    assert world['crm']._db.total_changes == changes and legacy_rows(world) == before


def test_projection_uses_actual_task_not_json_active_schedule(world):
    identifier = add_plan(world, {'title': '虚构旧安排', 'active_schedule': {'id': 999, 'status': 'pending'}})
    service = queue(world)
    assert service.get(OWNER, identifier)['active_schedule'] is None
    actual = task(world)
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?',
                   (json.dumps({'title': '实际旧安排', 'task_id': actual}), identifier))
    result = service.get(OWNER, identifier)
    assert result['active_schedule']['id'] == actual and result['active_schedule']['remind_at'] == NOW + 3600


def test_nested_upgrade_rollback_does_not_commit_outer_transaction(world):
    identifier = add_plan(world, {'title': '虚构未知资料'})
    db = world['crm']._db
    before_columns = {row['name'] for row in db.execute('PRAGMA table_info(crm_secretary_plans)')}
    assert db.execute('SELECT schema_version FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0] == 0
    with pytest.raises(RuntimeError):
        with world['crm']._transaction():
            queue(world)
            assert db.execute('SELECT schema_version FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0] == 1
            raise RuntimeError('outer rollback')
    assert before_columns == {row['name'] for row in db.execute('PRAGMA table_info(crm_secretary_plans)')}
    assert db.execute('SELECT schema_version FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0] == 0
    assert not db.execute('SELECT 1 FROM crm_arrangement_notifications').fetchone()


def test_projection_authority_and_agreement_bind_to_execution_not_title(world):
    proposed = {'time_spec': normalize_time('2026-10-08T15:00:00+08:00', NOW, role='execution'), 'place': '客户会议室'}
    signature = content_signature(proposed, decision_mode='external')
    data = {'title': '虚构项目交流', 'decision_mode': 'external', 'proposed_execution': proposed,
        'agreement': {'status': 'reported', 'scope': 'execution_time', 'evidence': '对方已经答应这个时间', 'content_signature': signature},
        'application_authority': {'kind': 'user_reviewed', 'content_signature': signature}}
    identifier = add_plan(world, data)
    service = queue(world)
    assert service.get(OWNER, identifier)['can_apply']
    data['title'] = '标题修改不撤销已有同意'
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?', (json.dumps(data), identifier))
    assert service.get(OWNER, identifier)['can_apply']
    data['proposed_execution']['time_spec'] = normalize_time('2026-10-08T16:00:00+08:00', NOW, role='execution')
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?', (json.dumps(data), identifier))
    result = service.get(OWNER, identifier)
    assert not result['can_apply']
    assert {'authority_changed', 'agreement_changed'} <= set(result['blocking_reasons'])


def test_date_settlement_and_time_projection_checks_are_read_only(world):
    execution = {'time_spec': normalize_time('2026-11-10', NOW, role='execution')}
    signature = content_signature(execution, settlement_scope='date_only', decision_mode='self')
    deadline = normalize_time('2026-10-03', NOW)
    check = normalize_time('2026-10-09上午', NOW, role='check')
    data = {'title': '虚构日期目标', 'decision_mode': 'self', 'settlement_scope': 'date_only',
        'proposed_execution': execution, 'application_authority': {'kind': 'direct_user', 'content_signature': signature},
        'settle_deadline': {'strength': 'required', 'time_spec': deadline},
        'next_check': {'check_id': 'fictional-check', 'time_spec': check, 'status': 'planned'}}
    identifier = add_plan(world, data)
    service = queue(world)
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET deadline_end_at=?,next_check_at=? WHERE id=?',
            (time_end(deadline), time_start(check), identifier))
    before = legacy_rows(world)
    changes = world['crm']._db.total_changes
    result = service.get(OWNER, identifier)
    assert result['can_apply'] and result['active_schedule'] is None
    assert {'deadline_overdue', 'check_after_deadline'} <= set(result['attention_flags'])
    assert world['crm']._db.total_changes == changes and legacy_rows(world) == before


def test_completed_task_never_becomes_active_and_repeated_migration_keeps_user_gate(world):
    old = task(world, 'completed')
    identifier = add_plan(world, {'title': '已结束旧执行', 'task_id': old})
    service = queue(world)
    with world['crm']._transaction() as db:
        db.execute("UPDATE crm_secretary_plans SET settling_state='paused',followup_disable_reason='user_disabled' WHERE id=?", (identifier,))
    service.upgrade_schema()
    result = service.get(OWNER, identifier)
    assert result['settling_state'] == 'paused' and result['execution_status'] == 'completed'
    assert result['active_schedule'] is None and result['followup_disable_reason'] == 'user_disabled'
    assert not result['followup_enabled']
