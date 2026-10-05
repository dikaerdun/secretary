"""Explicit action adoption keeps the goal's source and scheduler untouched."""
import json

import pytest

from secretary.customer_store import CustomerStore
from secretary.matters import MatterService
from secretary.matter_adoption import attach_adoption
from secretary.sales_workspace import SalesWorkspace

NOW = 1800000000.0
OWNER = 'fictional-adoption-owner'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-adoption.sqlite3')
    service = MatterService(crm, clock=lambda: NOW)
    customer = crm.create_customer(OWNER, {'name': '虚构方案单位'}, NOW)
    source = crm.create_record(OWNER, {'title': '电力方案现场交流', 'content': '客户建议完善PPT与现场演示',
        'customer_id': customer['id']}, NOW)
    action = crm.create_record(OWNER, {'title': '完善产品PPT', 'content': '用户已明确采纳', 'kind': 'action',
        'status': 'following', 'customer_id': customer['id'], 'parent_record_id': source['id']}, NOW)
    yield {'crm': crm, 'service': service, 'customer': customer, 'source': source, 'action': action}
    crm.close()


def goal(w, request='goal', **extra):
    return w['service'].create(OWNER, {'title': '准备客户汇报材料', 'source_record_ids': [w['source']['id']],
        'request_id': request, **extra})['matter']


def attach(w, kind='record', source_id=None, record_id=None):
    return attach_adoption(w['service'], OWNER, kind, source_id or w['source']['id'], record_id or w['action']['id'])


def legacy(w):
    return {table: [dict(row) for row in w['crm']._db.execute('SELECT * FROM ' + table)] for table in
            ('crm_records', 'crm_activities', 'tasks', 'proposals', 'notifications')}


def test_unique_source_attaches_action_with_no_legacy_mutation(world):
    m = goal(world); before = legacy(world)
    result = attach(world)
    assert result['matter_id'] == m['id']
    assert world['service'].get(OWNER, m['id'])['actions'][0]['id'] == world['action']['id']
    assert legacy(world) == before


def test_repeated_adoption_attachment_is_revision_noop(world):
    m = goal(world); first = attach(world); second = attach(world)
    assert first['matter']['revision'] == second['matter']['revision']
    assert second['matter']['action_count'] == 1


def test_unknown_source_does_not_create_goal_or_hide_original_action(world):
    before = legacy(world)
    result = attach(world)
    assert result['warning'] and result['matter_candidates'] == []
    assert world['service'].list(OWNER)['total'] == 0
    assert legacy(world) == before


def test_multiple_goals_return_choices_not_customer_guess(world):
    one = goal(world); two = goal(world, request='two', title='准备另一个验证目标')
    before = legacy(world); result = attach(world)
    assert {m['id'] for m in result['matter_candidates']} == {one['id'], two['id']}
    assert result.get('matter_id') is None
    assert world['service'].get(OWNER, one['id'])['action_count'] == 0
    assert legacy(world) == before


@pytest.mark.parametrize('state', ['archived', 'trash', 'ended'])
def test_archived_or_ended_goal_is_not_revived(world, state):
    m = goal(world)
    if state == 'ended':
        # Current goal ending policy is explicit; raw sidecar set keeps this
        # helper test independent of the frontend's end-reason vocabulary.
        with world['crm']._transaction() as db:
            db.execute("UPDATE crm_matters SET status='ended',outcome='achieved',revision=revision+1 WHERE owner=? AND id=?", (OWNER, m['id']))
    else:
        world['service'].lifecycle(OWNER, m['id'], {'visibility': state, 'reminder_action': 'keep',
            'expected_revision': m['revision'], 'request_id': 'hide'})
    result = attach(world)
    assert result.get('matter_id') is None and len(result['matter_candidates']) == 1
    assert world['service'].get(OWNER, m['id'])['action_count'] == 0


def test_action_already_in_other_goal_is_not_reassigned(world):
    source_goal = goal(world)
    other = world['service'].create(OWNER, {'title': '用户已纠正到另一目标', 'action_record_ids': [world['action']['id']], 'request_id': 'other'})['matter']
    result = attach(world)
    assert {m['id'] for m in result['matter_candidates']} == {source_goal['id'], other['id']}
    assert world['service'].get(OWNER, other['id'])['action_count'] == 1


def test_foreign_source_and_action_are_both_rejected(world):
    goal(world)
    foreign = world['crm'].create_record('other-owner', {'title': '他人记录', 'content': '私密', 'kind': 'action'}, NOW)
    for source_id, record_id in ((foreign['id'], world['action']['id']), (world['source']['id'], foreign['id'])):
        with pytest.raises(KeyError):
            attach_adoption(world['service'], OWNER, 'record', source_id, record_id)


@pytest.mark.parametrize('kind,source_id,record_id', [('invalid', 1, 2), ('record', True, 2), ('record', 1, True)])
def test_invalid_identifiers_and_type_are_rejected(world, kind, source_id, record_id):
    with pytest.raises(ValueError):
        attach_adoption(world['service'], OWNER, kind, source_id, record_id)


def test_material_source_resolves_same_goal_and_is_not_a_new_goal(world):
    m = goal(world)
    with world['crm']._transaction() as db:
        db.execute('''CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,customer_id INTEGER,
            title TEXT,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_materials VALUES(?,?,?,?,?,?,?)', (1, OWNER, world['source']['id'], world['customer']['id'], '现场录音', NOW, NOW))
    result = attach(world, kind='material', source_id=1)
    assert result['matter_id'] == m['id']
    assert world['service'].list(OWNER)['total'] == 1


def test_material_without_record_link_can_fall_back_once_to_action_parent(world):
    m = goal(world)
    with world['crm']._transaction() as db:
        db.execute('''CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,customer_id INTEGER,
            title TEXT,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_materials VALUES(?,?,?,?,?,?,?)', (1, OWNER, None, world['customer']['id'], '同次来源待补关联', NOW, NOW))
        db.execute('CREATE TABLE crm_material_actions(owner TEXT,material_id INTEGER,record_id INTEGER)')
        db.execute('INSERT INTO crm_material_actions VALUES(?,?,?)', (OWNER, 1, world['action']['id']))
    result = attach(world, kind='material', source_id=1)
    assert result['matter_id'] == m['id']


def test_customer_scope_change_does_not_attach_or_undo_original_adoption(world):
    m = goal(world)
    other = world['crm'].create_customer(OWNER, {'name': '另一个虚构单位'}, NOW)
    world['crm'].update_record(OWNER, world['action']['id'], {'customer_id': other['id']}, NOW + 1)
    before = legacy(world); result = attach(world)
    assert result['warning'] and result.get('matter_id') is None
    assert world['service'].get(OWNER, m['id'])['action_count'] == 0
    assert legacy(world) == before


def test_project_scope_change_is_returned_for_review(world):
    workspace = SalesWorkspace(world['crm'], clock=lambda: NOW)
    a = workspace.create_opportunity(OWNER, world['customer']['id'], {'name': '密码项目'})
    b = workspace.create_opportunity(OWNER, world['customer']['id'], {'name': '数据项目'})
    m = goal(world, opportunity_id=a['id'])
    record = world['crm']._require_record(world['crm']._db, OWNER, world['action']['id'])
    with world['crm']._transaction() as db:
        db.execute('INSERT INTO crm_opportunity_links VALUES(?,?,?,?,?,?,?,?)', (OWNER, 'record', record['id'], world['customer']['id'], b['id'], 1, SalesWorkspace._entity_snapshot(record), NOW))
    before = legacy(world); result = attach(world)
    assert result['warning'] and result.get('matter_id') is None
    assert world['service'].get(OWNER, m['id'])['action_count'] == 0
    assert legacy(world) == before


def test_hook_respects_outer_transaction_abort(world):
    m = goal(world)
    with pytest.raises(RuntimeError):
        with world['crm']._transaction():
            attach(world)
            raise RuntimeError('caller failed')
    assert world['service'].get(OWNER, m['id'])['action_count'] == 0


def test_same_owner_unrelated_source_does_not_assign_a_step(world):
    m = goal(world)
    unrelated = world['crm'].create_record(OWNER, {'title': '同客户另一场讨论', 'content': '另一个独立目标',
        'customer_id': world['customer']['id']}, NOW)
    other = world['service'].create(OWNER, {'title': '另一场讨论目标', 'source_record_ids': [unrelated['id']], 'request_id': 'unrelated'})['matter']
    result = attach(world, source_id=unrelated['id'])
    assert result.get('matter_id') is None and result['warning']
    assert world['service'].get(OWNER, other['id'])['action_count'] == 0


def test_visit_receipt_associates_adopted_step_without_executing_schedule(world):
    crm, service = world['crm'], world['service']
    with crm._transaction() as db:
        db.execute('''CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,title TEXT,customer_id INTEGER,
            created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_visits VALUES(?,?,?,?,?,?)', (1, OWNER, '录音会议', world['customer']['id'], NOW, NOW))
        db.execute('CREATE TABLE crm_visit_adoptions(owner TEXT,visit_id INTEGER,record_id INTEGER)')
        db.execute('INSERT INTO crm_visit_adoptions VALUES(?,?,?)', (OWNER, 1, world['action']['id']))
    m = service.create(OWNER, {'title': '录音中的方案准备', 'visit_ids': [1], 'customer_id': world['customer']['id'], 'request_id': 'visit-goal'})['matter']
    before = legacy(world); result = attach(world, kind='visit', source_id=1)
    assert result['matter_id'] == m['id']
    assert service.get(OWNER, m['id'])['action_count'] == 1
    assert legacy(world) == before
