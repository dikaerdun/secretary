"""Attribution correction is an association edit, never a second execution."""
import json
import re

import pytest

from secretary.customer_store import CustomerStore
from secretary.matters import MatterService, MatterConflict
from secretary.matter_correction import snapshot, correct, effective_route

NOW = 1800000000.0
OWNER = 'fictional-correction-owner'


class Flow:
    def __init__(self, crm, matters):
        self.crm, self.matters = crm, matters

    def turn(self, owner, identifier):
        row = self.crm._db.execute('SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?', (owner, identifier)).fetchone()
        result = json.loads(row['data_json'])
        route = result.get('matter_route') or {}
        return {'id': row['id'], 'record_id': row['record_id'], 'status': row['status'], 'result': result,
                'matter_route': route, 'matter_id': route.get('matter_id') or row['matter_id']}


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-corrections.sqlite3')
    matters = MatterService(crm, clock=lambda: NOW)
    customer = crm.create_customer(OWNER, {'name': '虚构纠正单位'}, NOW)
    source = crm.create_record(OWNER, {'title': '补充电力PPT', 'content': '补充装置产品化内容，原来的预算沟通已经结束',
        'customer_id': customer['id']}, NOW)
    new_action = crm.create_record(OWNER, {'title': '补充装置产品化', 'content': '补充装置产品化', 'kind': 'action',
        'status': 'following', 'customer_id': customer['id'], 'parent_record_id': source['id']}, NOW)
    existing = crm.create_record(OWNER, {'title': '客户预算沟通', 'content': '客户预算沟通', 'kind': 'action',
        'status': 'done', 'customer_id': customer['id']}, NOW - 100)
    crm.add_activity(OWNER, existing['id'], '这次原话已回报预算沟通结束', NOW)
    old = matters.create(OWNER, {'title': '旧事项', 'customer_id': customer['id'], 'source_record_ids': [source['id']],
        'action_record_ids': [new_action['id'], existing['id']], 'request_id': 'old'})['matter']
    target = matters.create(OWNER, {'title': '正确的材料目标', 'customer_id': customer['id'], 'request_id': 'target'})['matter']
    route = {'kind': 'existing', 'matter_id': old['id'], 'matter': matters._summary(old), 'items': [matters._summary(old)],
             'changes': [{'record_id': new_action['id'], 'title': new_action['title'], 'change': 'created'},
                         {'record_id': existing['id'], 'title': existing['title'], 'change': 'completed'}]}
    with crm._transaction() as db:
        db.execute('''CREATE TABLE crm_secretary_turns(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,plan_id INTEGER,
            matter_id INTEGER,matter_revision INTEGER,scope_json TEXT,data_json TEXT,status TEXT,updated_at REAL)''')
        db.execute('INSERT INTO crm_secretary_turns VALUES(?,?,?,?,?,?,?,?,?,?)', (1, OWNER, source['id'], None,
            old['id'], old['revision'], '{}', json.dumps({'matter_route': route}), 'done', NOW))
    flow = Flow(crm, matters)
    yield {'crm': crm, 'service': matters, 'flow': flow, 'customer': customer, 'source': source,
           'new_action': new_action, 'existing': existing, 'old': old, 'target': target}
    crm.close()


def body(w, key='correct', **extra):
    return {'matter_id': w['target']['id'], 'expected_matter_revision': w['service'].get(OWNER, w['target']['id'])['revision'],
        'expected_turn_snapshot': snapshot(w['flow'], OWNER, 1)['snapshot'], 'request_id': key, **extra}


def legacy(w):
    return {table: [dict(r) for r in w['crm']._db.execute('SELECT * FROM ' + table + ' ORDER BY id')] for table in
            ('crm_records', 'crm_activities', 'tasks', 'proposals', 'notifications')}


def test_move_only_new_step_and_this_source_existing_progress_stays(world):
    before = legacy(world)
    result = correct(world['flow'], OWNER, 1, body(world))
    assert result['turn']['matter_id'] == world['target']['id']
    assert result['matter']['actions'][0]['id'] == world['new_action']['id']
    old = world['service'].get(OWNER, world['old']['id'])
    assert [a['id'] for a in old['actions']] == [world['existing']['id']]
    assert old['actions'][0]['status'] == 'done'
    assert any('进展保留' in warning for warning in result['warnings'])
    assert legacy(world) == before


def test_idempotent_correction_does_not_duplicate_or_move_again(world):
    data = body(world)
    first = correct(world['flow'], OWNER, 1, data)
    second = correct(world['flow'], OWNER, 1, data)
    assert first['operation_id'] == second['operation_id'] and second['replayed']
    assert second['matter']['action_count'] == 1
    with pytest.raises(MatterConflict):
        correct(world['flow'], OWNER, 1, {**data, 'title': '改变同一次请求'})


def test_correction_undo_restores_associations_and_effective_turn_route(world):
    result = correct(world['flow'], OWNER, 1, body(world))
    world['service'].undo(OWNER, result['operation_id'], {'expected_revision': result['matter']['revision'], 'request_id': 'undo-correct'})
    assert world['service'].get(OWNER, world['old']['id'])['action_count'] == 2
    assert world['service'].get(OWNER, world['target']['id'])['action_count'] == 0
    raw = world['flow'].turn(OWNER, 1)['matter_route']
    assert effective_route(world['flow'], OWNER, 1, raw)['matter_id'] == world['old']['id']


def test_fresh_goal_preserves_original_rows_and_undo_keeps_stable_new_id(world):
    before = legacy(world)
    preview = snapshot(world['flow'], OWNER, 1)
    result = correct(world['flow'], OWNER, 1, {'matter_mode': 'fresh', 'title': '电力汇报材料准备',
        'expected_turn_snapshot': preview['snapshot'], 'request_id': 'fresh'})
    assert result['matter']['id'] not in (world['old']['id'], world['target']['id'])
    assert result['matter']['action_count'] == 1
    world['service'].undo(OWNER, result['operation_id'], {'expected_revision': result['matter']['revision'], 'request_id': 'undo-fresh'})
    assert world['service'].get(OWNER, result['matter']['id'])['visibility'] == 'archived'
    assert legacy(world) == before


def test_snapshot_is_read_only_and_stale_progress_is_rejected(world):
    db = world['crm']._db; before = db.total_changes
    data = body(world)
    assert db.total_changes == before
    world['crm'].update_record(OWNER, world['new_action']['id'], {'content': '这条步骤有后续修改'}, NOW + 1)
    with pytest.raises(MatterConflict):
        correct(world['flow'], OWNER, 1, data)
    assert world['service'].get(OWNER, world['old']['id'])['action_count'] == 2


def test_target_revision_or_archived_target_cannot_be_bypassed(world):
    data = body(world)
    world['service'].update(OWNER, world['target']['id'], {'title': '用户已改目标名',
        'expected_revision': world['target']['revision'], 'request_id': 'rename-target'})
    with pytest.raises(MatterConflict):
        correct(world['flow'], OWNER, 1, data)
    target = world['service'].get(OWNER, world['target']['id'])
    world['service'].lifecycle(OWNER, target['id'], {'visibility': 'archived', 'reminder_action': 'keep',
        'expected_revision': target['revision'], 'request_id': 'archive-target'})
    with pytest.raises(MatterConflict):
        correct(world['flow'], OWNER, 1, body(world, key='archived'))


def test_foreign_turn_target_and_different_unit_are_rejected(world):
    data = body(world)
    with pytest.raises(KeyError):
        snapshot(world['flow'], 'another-owner', 1)
    foreign = world['service'].create('another-owner', {'title': '他人的目标', 'request_id': 'foreign'})['matter']
    with pytest.raises(KeyError):
        correct(world['flow'], OWNER, 1, {**data, 'matter_id': foreign['id'], 'expected_matter_revision': foreign['revision']})
    other_customer = world['crm'].create_customer(OWNER, {'name': '另一个虚构单位'}, NOW)
    different = world['service'].create(OWNER, {'title': '另一个单位事项', 'customer_id': other_customer['id'], 'request_id': 'different-unit'})['matter']
    with pytest.raises(ValueError):
        correct(world['flow'], OWNER, 1, {**data, 'matter_id': different['id'], 'expected_matter_revision': different['revision']})


@pytest.mark.parametrize('extra', [{'extra': 'invalid'}, {'matter_mode': 'fresh'}, {'matter_id': True},
    {'expected_turn_snapshot': 'bad'}, {'expected_matter_revision': True}])
def test_invalid_correction_does_not_modify_associations(world, extra):
    with pytest.raises((ValueError, KeyError)):
        correct(world['flow'], OWNER, 1, body(world, **extra))
    assert world['service'].get(OWNER, world['old']['id'])['action_count'] == 2


def test_not_done_turn_needs_finish_before_correction(world):
    with world['crm']._transaction() as db:
        db.execute("UPDATE crm_secretary_turns SET status='processing' WHERE owner=? AND id=1", (OWNER,))
    with pytest.raises(MatterConflict):
        snapshot(world['flow'], OWNER, 1)


def test_ambiguous_confirmation_does_not_reinterpret_or_execute_pending_actions(world):
    with world['crm']._transaction() as db:
        result = {'matter_route': {'kind': 'ambiguous', 'candidates': [world['old'], world['target']], 'changes': []},
                  'pending_matter_decision': {'kind': 'ambiguous', 'actions': [{'title': '未经选择的动作', 'evidence': world['source']['content']}]}}
        db.execute('UPDATE crm_secretary_turns SET matter_id=NULL,data_json=? WHERE owner=? AND id=1', (json.dumps(result), OWNER))
    before = legacy(world)
    result = correct(world['flow'], OWNER, 1, body(world))
    assert result['needs_review'] and result['matter']['action_count'] == 0
    assert result['turn']['matter_id'] == world['target']['id']
    assert legacy(world) == before
    world['service'].undo(OWNER, result['operation_id'], {'expected_revision': result['matter']['revision'], 'request_id': 'undo-ambiguous'})
    route = effective_route(world['flow'], OWNER, 1, world['flow'].turn(OWNER, 1)['matter_route'])
    assert route['kind'] == 'ambiguous' and route['matter_id'] is None


def test_existing_schedule_and_notifications_are_not_moved_or_replayed(world):
    crm = world['crm']; record = world['new_action']
    reply = crm.execute(OWNER, 'meeting', {'action': 'propose', 'title': record['title'], 'remind_at': NOW + 3600}, NOW)
    pid = int(re.search(r'P(\d+)', reply)[1]); crm.link_proposal(OWNER, record['id'], pid, NOW)
    crm.execute(OWNER, 'confirm-meeting', {'action': 'confirm', 'proposal_id': pid}, NOW)
    before = legacy(world)
    result = correct(world['flow'], OWNER, 1, body(world))
    assert result['matter']['tasks'][0]['id'] == crm.get_proposal(OWNER, pid)['task_id']
    assert legacy(world) == before
    assert len(crm._db.execute('SELECT * FROM notifications').fetchall()) == 1


def test_correction_inside_outer_transaction_rolls_back_cleanly(world):
    data = body(world)
    with pytest.raises(RuntimeError):
        with world['crm']._transaction():
            correct(world['flow'], OWNER, 1, data)
            raise RuntimeError('caller aborted')
    assert world['service'].get(OWNER, world['old']['id'])['action_count'] == 2
    assert world['flow'].turn(OWNER, 1)['matter_id'] == world['old']['id']


def test_shared_old_plan_and_material_keep_activity_attribution(world):
    crm, service = world['crm'], world['service']
    material_record = crm.create_record(OWNER, {'title': '项目汇报附件原文', 'content': '项目方案重点',
        'customer_id': world['customer']['id']}, NOW)
    with crm._transaction() as db:
        db.execute('''CREATE TABLE crm_secretary_plans(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,visit_id INTEGER,
            data_json TEXT,revision INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_secretary_plans VALUES(?,?,?,?,?,?,?,?)', (1, OWNER, world['source']['id'], None,
            json.dumps({'title': '原汇报会面', 'date': '2026-10-08', 'goal': '讨论项目', 'status': 'preparing'}), 1, NOW, NOW))
        db.execute('''CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,title TEXT,record_id INTEGER,
            customer_id INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_materials VALUES(?,?,?,?,?,?,?)', (1, OWNER, '汇报材料', material_record['id'], world['customer']['id'], NOW, NOW))
        db.execute('CREATE TABLE crm_secretary_turn_attachments(owner TEXT,turn_id INTEGER,material_id INTEGER)')
        db.execute('CREATE TABLE crm_secretary_plan_attachments(owner TEXT,plan_id INTEGER,material_id INTEGER)')
        db.execute('INSERT INTO crm_secretary_turn_attachments VALUES(?,?,?)', (OWNER, 1, 1))
        db.execute('INSERT INTO crm_secretary_plan_attachments VALUES(?,?,?)', (OWNER, 1, 1))
        db.execute('UPDATE crm_secretary_turns SET plan_id=1 WHERE owner=? AND id=1', (OWNER,))
    service.attach(OWNER, world['old']['id'], 'plan', 1, role='plan')
    service.attach(OWNER, world['old']['id'], 'material', 1)
    plan_before = dict(crm._db.execute('SELECT * FROM crm_secretary_plans WHERE id=1').fetchone())
    before = legacy(world)
    result = correct(world['flow'], OWNER, 1, body(world))
    old = service.get(OWNER, world['old']['id'])
    assert [p['id'] for p in old['plans']] == [1]
    assert [m['id'] for m in old['materials']] == [1]
    assert [m['id'] for m in result['matter']['materials']] == [1]
    assert dict(crm._db.execute('SELECT * FROM crm_secretary_plans WHERE id=1').fetchone()) == plan_before
    assert any('活动及提醒保留' in warning for warning in result['warnings'])
    assert legacy(world) == before


def test_exclusive_turn_attachment_link_moves_but_material_row_is_preserved(world):
    crm, service = world['crm'], world['service']
    material_record = crm.create_record(OWNER, {'title': '新附件原话', 'content': '电力产品资料',
        'customer_id': world['customer']['id']}, NOW)
    with crm._transaction() as db:
        db.execute('''CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,title TEXT,record_id INTEGER,
            customer_id INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_materials VALUES(?,?,?,?,?,?,?)', (1, OWNER, '新附件', material_record['id'], world['customer']['id'], NOW, NOW))
        db.execute('CREATE TABLE crm_secretary_turn_attachments(owner TEXT,turn_id INTEGER,material_id INTEGER)')
        db.execute('INSERT INTO crm_secretary_turn_attachments VALUES(?,?,?)', (OWNER, 1, 1))
    service.attach(OWNER, world['old']['id'], 'material', 1)
    material_before = dict(crm._db.execute('SELECT * FROM crm_materials WHERE id=1').fetchone())
    result = correct(world['flow'], OWNER, 1, body(world))
    assert world['service'].get(OWNER, world['old']['id'])['materials'] == []
    assert [m['id'] for m in result['matter']['materials']] == [1]
    assert dict(crm._db.execute('SELECT * FROM crm_materials WHERE id=1').fetchone()) == material_before
