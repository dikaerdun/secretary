"""Goal grouping changes only its sidecar; all fixtures are fictional SQLite."""
import json
import re

import pytest

from secretary.customer_store import CustomerStore
from secretary.crm import RecordConflict
from secretary.matters import MatterService
from secretary.sales_workspace import SalesWorkspace

NOW = 1800000000.0
OWNER = 'fictional-matter-owner'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-matters.sqlite3')
    customer = crm.create_customer(OWNER, {'name': '示例电力单位（虚构）'}, NOW)
    source = crm.create_record(OWNER, {'title': '准备电力汇报材料', 'content': '按产品线重组PPT，并补充装置产品化内容',
        'customer_id': customer['id'], 'status': 'done'}, NOW)
    actions = [crm.create_record(OWNER, {'title': title, 'content': title, 'kind': 'action', 'status': 'following',
        'parent_record_id': source['id'], 'customer_id': customer['id']}, NOW) for title in ('重组PPT产品线', '补充防篡改装置内容')]
    service = MatterService(crm, clock=lambda: NOW)
    yield {'crm': crm, 'service': service, 'customer': customer, 'source': source, 'actions': actions}
    crm.close()


def create(w, key='create-main', selected=None, **extra):
    return w['service'].create(OWNER, {'title': '完成电力汇报材料', 'objective': '让客户看懂方案与核心产品',
        'source_record_ids': [w['source']['id']], 'action_record_ids': [a['id'] for a in (w['actions'] if selected is None else selected)],
        'request_id': key, **extra})


def legacy(w):
    db = w['crm']._db
    return {table: [dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY id')] for table in
            ('crm_records', 'crm_customers', 'crm_activities', 'tasks', 'proposals', 'notifications')}


def schedule(w, record, key, delta=3600):
    crm = w['crm']
    reply = crm.execute(OWNER, key, {'action': 'propose', 'title': record['title'], 'remind_at': NOW + delta}, NOW)
    pid = int(re.search(r'P(\d+)', reply)[1])
    crm.link_proposal(OWNER, record['id'], pid, NOW)
    crm.execute(OWNER, key + '-confirm', {'action': 'confirm', 'proposal_id': pid}, NOW)
    return crm.get_proposal(OWNER, pid)['task_id']


def plan(w, record, key=1, task_id=None):
    db = w['crm']._db
    with w['crm']._transaction():
        db.execute('''CREATE TABLE IF NOT EXISTS crm_secretary_plans(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,
            visit_id INTEGER,data_json TEXT,revision INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS crm_secretary_turns(id INTEGER PRIMARY KEY,owner TEXT,plan_id INTEGER,record_id INTEGER)''')
        db.execute('INSERT INTO crm_secretary_plans VALUES(?,?,?,?,?,?,?,?)', (key, OWNER, record['id'], None,
            json.dumps({'title': '与客户沟通电力方案', 'objective': '确认试点范围', 'date': '2026-10-08', 'time': None,
                        'customer_id': w['customer']['id'], 'status': 'draft', 'task_id': task_id}), 1, NOW, NOW))
    return key


def test_same_source_actions_become_one_goal_without_changing_legacy(world):
    before = legacy(world)
    candidates = world['service'].candidates(OWNER)['items']
    assert len(candidates) == 1 and len(candidates[0]['actions']) == 2
    c = candidates[0]
    result = world['service'].confirm_candidate(OWNER, {'key': c['key'], 'fingerprint': c['fingerprint'],
        'title': '准备电力汇报材料', 'objective': '介绍完整产品线', 'request_id': 'confirm-two'})
    m = result['matter']
    assert m['status'] == 'following' and m['action_count'] == 2
    assert m['sources'][0]['status'] == 'done'
    assert legacy(world) == before
    assert world['service'].candidates(OWNER)['items'] == []
    assert world['service'].resolve(OWNER, 'record', world['actions'][0]['id'])['items'][0]['id'] == m['id']


def test_get_candidates_is_pure_and_owner_scoped(world):
    db = world['crm']._db
    before = db.total_changes
    assert len(world['service'].candidates(OWNER)['items']) == 1
    assert world['service'].candidates('other-owner')['items'] == []
    assert db.total_changes == before


def test_source_can_support_two_goals_and_subset_confirmation(world):
    s = world['service']; c = s.candidates(OWNER)['items'][0]
    first = s.confirm_candidate(OWNER, {'key': c['key'], 'fingerprint': c['fingerprint'], 'title': 'PPT结构',
        'action_record_ids': [world['actions'][0]['id']], 'request_id': 'subset-one'})['matter']
    remaining = s.candidates(OWNER)['items'][0]
    second = s.confirm_candidate(OWNER, {'key': remaining['key'], 'fingerprint': remaining['fingerprint'],
        'title': '装置产品化', 'request_id': 'subset-two'})['matter']
    assert first['id'] != second['id']
    assert len(s.resolve(OWNER, 'record', world['source']['id'])['items']) == 2
    assert first['action_count'] == second['action_count'] == 1


def test_candidate_version_change_blocks_and_transaction_rolls_back(world):
    s = world['service']; c = s.candidates(OWNER)['items'][0]
    world['crm'].update_record(OWNER, world['actions'][0]['id'], {'title': '已修改的新目标'}, NOW + 2)
    with pytest.raises(RecordConflict):
        s.confirm_candidate(OWNER, {'key': c['key'], 'fingerprint': c['fingerprint'], 'title': '旧目标', 'request_id': 'stale'})
    assert s.list(OWNER)['total'] == 0


@pytest.mark.parametrize('field,value', [('customer_id', True), ('action_record_ids', [True]),
    ('action_record_ids', [1, 1]), ('title', ''), ('surprise', 'bad')])
def test_create_rejects_bad_inputs_without_partial_goal(world, field, value):
    with pytest.raises((ValueError, KeyError)):
        create(world, **{field: value})
    assert world['service'].list(OWNER)['total'] == 0


def test_foreign_entity_all_endpoints_are_isolated(world):
    crm, s = world['crm'], world['service']
    foreign = crm.create_record('other-owner', {'title': '另一个人的秘密', 'content': '私人内容', 'kind': 'action'}, NOW)
    with pytest.raises(KeyError):
        create(world, action_record_ids=[foreign['id']])
    m = create(world)['matter']
    for invoke in (lambda: s.detail('other-owner', m['id']), lambda: s.resolve(OWNER, 'record', foreign['id']),
                   lambda: s.attach(OWNER, m['id'], 'record', foreign['id']),
                   lambda: s.undo('other-owner', 1, {'expected_revision': 1, 'request_id': 'bad-undo'})):
        with pytest.raises(KeyError):
            invoke()
    assert s.list('other-owner')['items'] == []


def test_create_and_confirmation_are_idempotent_but_payload_cannot_change(world):
    first = create(world)
    replay = create(world)
    assert first['matter']['id'] == replay['matter']['id'] and replay['replayed']
    assert world['service'].list(OWNER)['total'] == 1
    with pytest.raises(RecordConflict):
        create(world, title='同标识另一操作')
    c = world['service'].candidates(OWNER)['items']
    assert c == []


def test_action_unique_even_if_existing_goal_is_archived(world):
    s = world['service']; m = create(world)['matter']
    s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'reminder_action': 'keep', 'request_id': 'archive'})
    with pytest.raises(RecordConflict):
        create(world, key='another-group')
    assert s.list(OWNER, visibility='archived')['total'] == 1


def test_multiple_schedules_use_complete_proposal_history_once(world):
    a = world['actions'][0]
    old_task = schedule(world, a, 'first-meeting')
    world['crm'].execute(OWNER, 'complete-old', {'action': 'complete', 'task_id': old_task}, NOW + 1)
    new_task = schedule(world, a, 'second-meeting', 7200)
    m = create(world)['matter']
    assert {t['id'] for t in m['tasks']} == {old_task, new_task}
    assert m['next_schedule']['id'] == new_task
    assert m['status'] == 'following'
    assert world['service'].resolve(OWNER, 'task', old_task)['items'][0]['id'] == m['id']


def test_date_only_plan_and_supplement_resolve_same_goal(world):
    s = world['service']; p = plan(world, world['actions'][0])
    supplement = world['crm'].create_record(OWNER, {'title': '补充目标', 'content': '确认客户预算', 'status': 'done'}, NOW)
    with world['crm']._transaction() as db:
        db.execute('INSERT INTO crm_secretary_turns VALUES(?,?,?,?)', (1, OWNER, p, supplement['id']))
    m = create(world)['matter']
    assert len(m['plans']) == 1 and m['next_schedule']['date'] == '2026-10-08'
    assert m['tasks'] == []
    assert s.resolve(OWNER, 'plan', p)['items'][0]['id'] == m['id']
    assert s.resolve(OWNER, 'record', supplement['id'])['items'][0]['id'] == m['id']


def test_independent_plan_candidate_not_lost(world):
    record = world['crm'].create_record(OWNER, {'title': '待约客户', 'content': '10月8日吃饭', 'customer_id': world['customer']['id']}, NOW)
    p = plan(world, record)
    candidate = next(c for c in world['service'].candidates(OWNER)['items'] if c['plan_ids'] == [p])
    assert candidate['action_record_ids'] == []
    result = world['service'].confirm_candidate(OWNER, {'key': candidate['key'], 'fingerprint': candidate['fingerprint'],
        'title': '约客户确认试点', 'request_id': 'confirm-independent-plan'})
    assert result['matter']['plans'][0]['id'] == p


def test_search_and_pagination_are_goal_based_and_summary_global(world):
    s = world['service']; create(world, selected=world['actions'][:1])
    create(world, key='second', selected=world['actions'][1:], title='产品化验证')
    filtered = s.list(OWNER, q='防篡改', page_size=1)
    assert filtered['total'] == 1 and filtered['items'][0]['title'] == '产品化验证'
    assert filtered['summary']['active'] == 2
    assert s.list(OWNER, page_size=1)['pages'] == 2
    assert s.list(OWNER, page=2, page_size=1)['items'][0]['action_count'] == 1


def test_merge_then_undo_restores_membership_without_deleting_originals(world):
    s = world['service']; a = create(world, selected=world['actions'][:1])['matter']
    b = create(world, key='second', selected=world['actions'][1:])['matter']; before = legacy(world)
    merged = s.merge(OWNER, a['id'], {'target_id': b['id'], 'expected_revision': a['revision'], 'target_revision': b['revision'], 'request_id': 'merge'})
    assert merged['matter']['action_count'] == 2
    assert s.detail(OWNER, a['id'])['visibility'] == 'archived'
    undone = s.undo(OWNER, merged['operation_id'], {'expected_revision': merged['matter']['revision'], 'request_id': 'undo-merge'})
    assert undone['effects']['reminders_restarted'] is False
    assert s.detail(OWNER, a['id'])['action_count'] == s.detail(OWNER, b['id'])['action_count'] == 1
    assert legacy(world) == before


def test_split_idempotent_and_undo_restores_only_links(world):
    s = world['service']; created = create(world); m = created['matter']; before = legacy(world)
    data = {'action_record_ids': [world['actions'][1]['id']], 'title': '装置产品化', 'expected_revision': m['revision'], 'request_id': 'split'}
    split = s.split(OWNER, m['id'], data)
    assert split['matter']['action_count'] == split['related_matters'][0]['action_count'] == 1
    assert s.split(OWNER, m['id'], data)['related_matters'][0]['id'] == split['related_matters'][0]['id']
    restored = s.undo(OWNER, split['operation_id'], {'expected_revision': split['matter']['revision'], 'request_id': 'undo-split'})
    assert restored['matter']['action_count'] == 2
    assert restored['related_matters'][0]['visibility'] == 'archived'
    assert legacy(world) == before


def test_undo_blocks_if_other_affected_goal_changed(world):
    s = world['service']; m = create(world)['matter']
    split = s.split(OWNER, m['id'], {'action_record_ids': [world['actions'][1]['id']], 'title': '装置', 'expected_revision': m['revision'], 'request_id': 'split'})
    other = split['related_matters'][0]
    s.update(OWNER, other['id'], {'objective': '新推进目标', 'expected_revision': other['revision'], 'request_id': 'update-other'})
    with pytest.raises(RecordConflict):
        s.undo(OWNER, split['operation_id'], {'expected_revision': split['matter']['revision'], 'request_id': 'undo-too-late'})
    assert s.detail(OWNER, other['id'])['objective'] == '新推进目标'


def test_archive_keep_cancel_restore_never_restarts_reminders(world):
    s = world['service']; tid = schedule(world, world['actions'][0], 'meeting'); m = create(world)['matter']
    preview = s.lifecycle_preview(OWNER, m['id'])
    kept = s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'reminder_action': 'keep', 'request_id': 'keep'})
    assert world['crm'].get_task(OWNER, tid)['status'] == 'pending'
    restored = s.lifecycle(OWNER, m['id'], {'visibility': 'active', 'expected_revision': kept['matter']['revision'], 'reminder_action': 'keep', 'request_id': 'restore-kept'})
    fresh = s.lifecycle_preview(OWNER, m['id'])
    cancelled = s.lifecycle(OWNER, m['id'], {'visibility': 'trash', 'expected_revision': restored['matter']['revision'], 'snapshot': fresh['snapshot'], 'reminder_action': 'cancel', 'request_id': 'cancel'})
    assert cancelled['effects']['cancelled_tasks'] == 1
    assert world['crm'].get_task(OWNER, tid)['status'] == 'cancelled'
    s.undo(OWNER, cancelled['operation_id'], {'expected_revision': cancelled['matter']['revision'], 'request_id': 'undo-cancel'})
    assert world['crm'].get_task(OWNER, tid)['status'] == 'cancelled'
    assert world['crm'].get_record(OWNER, world['source']['id'])['status'] == 'done'


def test_cancel_stale_schedule_snapshot_and_shared_event_are_protected(world):
    s = world['service']; tid = schedule(world, world['actions'][0], 'meeting'); m = create(world)['matter']
    preview = s.lifecycle_preview(OWNER, m['id'])
    reply = world['crm'].execute(OWNER, 'snooze', {'action': 'snooze', 'task_id': tid, 'remind_at': NOW + 600}, NOW)
    assert reply.startswith('已调整提醒')
    with pytest.raises(RecordConflict):
        s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'snapshot': preview['snapshot'], 'reminder_action': 'cancel', 'request_id': 'stale-cancel'})
    other = s.create(OWNER, {'title': '同一会议第二目标', 'task_ids': [tid], 'request_id': 'shared'})['matter']
    preview = s.lifecycle_preview(OWNER, m['id'])
    assert preview['shared_tasks'] == 1
    with pytest.raises(ValueError):
        s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'snapshot': preview['snapshot'], 'reminder_action': 'cancel', 'request_id': 'shared-cancel'})
    assert len(s.resolve(OWNER, 'task', tid)['items']) == 2
    assert s.list(OWNER)['summary']['schedule_count'] == 1


def test_nested_outer_transaction_rolls_back_and_caught_failure_has_no_partial_goal(world):
    s, crm = world['service'], world['crm']
    with pytest.raises(RuntimeError):
        with crm._transaction():
            m = create(world)['matter']
            s.attach(OWNER, m['id'], 'record', world['source']['id'], expected_revision=m['revision'])
            raise RuntimeError('simulate caller rollback')
    assert s.list(OWNER)['total'] == 0
    with crm._transaction():
        create(world)
        with pytest.raises(RecordConflict):
            create(world, key='duplicate-action')
    assert s.list(OWNER)['total'] == 1


def test_attach_repeat_is_noop_and_archived_goal_cannot_be_revived_by_late_ai(world):
    s = world['service']; m = create(world)['matter']
    assert s.attach(OWNER, m['id'], 'record', world['source']['id'])['matter']['revision'] == m['revision']
    archived = s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'reminder_action': 'keep', 'request_id': 'archive'})['matter']
    with pytest.raises(RecordConflict):
        s.attach(OWNER, m['id'], 'record', world['source']['id'])
    assert s.detail(OWNER, m['id'])['visibility'] == 'archived'


def test_completion_of_action_or_schedule_does_not_end_goal(world):
    tid = schedule(world, world['actions'][0], 'meeting'); s = world['service']; m = create(world)['matter']
    world['crm'].execute(OWNER, 'calendar-only', {'action': 'complete', 'task_id': tid}, NOW)
    for a in world['actions']:
        world['crm'].update_record(OWNER, a['id'], {'status': 'done'}, NOW + 2)
    detail = s.detail(OWNER, m['id'])
    assert detail['completed_action_count'] == 2 and detail['status'] == 'following'
    ended = s.update(OWNER, m['id'], {'status': 'ended', 'outcome': '汇报已完成', 'expected_revision': m['revision'], 'request_id': 'end'})['matter']
    assert ended['status'] == 'ended' and ended['outcome'] == '汇报已完成'


def test_cross_project_merge_blocks(world):
    ws = SalesWorkspace(world['crm'], clock=lambda: NOW)
    a = ws.create_opportunity(OWNER, world['customer']['id'], {'name': '密码汇报'})
    b = ws.create_opportunity(OWNER, world['customer']['id'], {'name': '数据共享'})
    s = world['service']
    one = create(world, selected=world['actions'][:1], opportunity_id=a['id'])['matter']
    two = create(world, key='second', selected=world['actions'][1:], opportunity_id=b['id'])['matter']
    with pytest.raises(ValueError):
        s.merge(OWNER, one['id'], {'target_id': two['id'], 'expected_revision': one['revision'], 'target_revision': two['revision'], 'request_id': 'bad-merge'})
    assert s.list(OWNER)['total'] == 2


def test_undo_created_group_preserves_id_and_original_evidence(world):
    s = world['service']; before = legacy(world); created = create(world)
    m = created['matter']
    undone = s.undo(OWNER, created['operation_id'], {'expected_revision': m['revision'], 'request_id': 'undo-create'})
    assert undone['matter']['id'] == m['id'] and undone['matter']['visibility'] == 'archived'
    assert undone['matter']['links'] == []
    assert s.candidates(OWNER)['items'][0]['action_record_ids'] == [a['id'] for a in world['actions']]
    assert legacy(world) == before


def test_stale_and_boolean_revision_cannot_overwrite_new_goal(world):
    s = world['service']; m = create(world)['matter']
    s.update(OWNER, m['id'], {'objective': '已确认的新目标', 'expected_revision': m['revision'], 'request_id': 'new-objective'})
    for expected in (m['revision'], True):
        with pytest.raises(RecordConflict):
            s.update(OWNER, m['id'], {'objective': '旧输入', 'expected_revision': expected, 'request_id': 'old-' + str(expected)})
    assert s.get(OWNER, m['id'])['objective'] == '已确认的新目标'


def test_cancel_protects_legacy_action_outside_task_only_goal(world):
    s = world['service']; tid = schedule(world, world['actions'][0], 'meeting')
    m = s.create(OWNER, {'title': '只有活动关联的新目标', 'task_ids': [tid], 'request_id': 'task-only'})['matter']
    preview = s.lifecycle_preview(OWNER, m['id'])
    assert preview['shared_tasks'] == 1
    assert preview['pending_tasks'][0]['shared_active_record_ids'] == [world['actions'][0]['id']]
    with pytest.raises(ValueError):
        s.lifecycle(OWNER, m['id'], {'visibility': 'archived', 'expected_revision': m['revision'], 'snapshot': preview['snapshot'], 'reminder_action': 'cancel', 'request_id': 'cannot-cancel-legacy'})
    assert world['crm'].get_task(OWNER, tid)['status'] == 'pending'


def test_discussion_history_is_linked_searchable_and_owner_isolated(world):
    db = world['crm']._db
    with world['crm']._transaction():
        db.execute('''CREATE TABLE crm_sales_discussions(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,
            opportunity_id INTEGER,source_record_id INTEGER,title TEXT,created_at REAL,updated_at REAL)''')
        db.execute('''CREATE TABLE crm_sales_discussion_messages(id INTEGER PRIMARY KEY,owner TEXT,thread_id INTEGER,
            role TEXT,text TEXT,status TEXT,created_at REAL,updated_at REAL)''')
        db.execute('INSERT INTO crm_sales_discussions VALUES(?,?,?,?,?,?,?,?)', (1, OWNER, world['customer']['id'], None, world['source']['id'], '如何推进试点', NOW, NOW))
        db.execute('INSERT INTO crm_sales_discussions VALUES(?,?,?,?,?,?,?,?)', (2, 'other-owner', None, None, None, '其他人的讨论', NOW, NOW))
        db.execute('INSERT INTO crm_sales_discussion_messages VALUES(?,?,?,?,?,?,?,?)', (1, OWNER, 1, 'user', '先评估专网密评试点范围', 'complete', NOW + 2, NOW + 2))
    s = world['service']; m = create(world)['matter']
    attached = s.attach(OWNER, m['id'], 'discussion', 1)['matter']
    assert attached['discussions'][0]['messages'][0]['text'] == '先评估专网密评试点范围'
    assert s.list(OWNER, q='专网密评')['items'][0]['id'] == m['id']
    assert s.resolve(OWNER, 'discussion', 1)['items'][0]['id'] == m['id']
    assert any(event['type'] == 'discussion' for event in attached['events'])
    with pytest.raises(KeyError):
        s.attach(OWNER, m['id'], 'discussion', 2)


def test_foreign_task_plan_and_customer_are_not_accepted(world):
    foreign_customer = world['crm'].create_customer('other-owner', {'name': '另一个人的单位'}, NOW)
    foreign_record = world['crm'].create_record('other-owner', {'title': '私人会面', 'content': '私人安排', 'kind': 'action'}, NOW)
    reply = world['crm'].execute('other-owner', 'foreign-proposal', {'action': 'propose', 'title': '私人会面', 'remind_at': NOW + 3600}, NOW)
    pid = int(re.search(r'P(\d+)', reply)[1])
    world['crm'].execute('other-owner', 'foreign-confirm', {'action': 'confirm', 'proposal_id': pid}, NOW)
    tid = world['crm'].get_proposal('other-owner', pid)['task_id']
    s = world['service']
    for extra in ({'customer_id': foreign_customer['id']}, {'task_ids': [tid]}):
        with pytest.raises((KeyError, ValueError)):
            create(world, **extra)
    p = plan(world, world['source'])
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET owner=?,record_id=? WHERE id=?', ('other-owner', foreign_record['id'], p))
    with pytest.raises(KeyError):
        create(world, plan_ids=[p])
    assert s.list(OWNER)['total'] == 0


def test_visit_material_sources_expand_but_sibling_actions_do_not(world):
    crm, s = world['crm'], world['service']
    extra = crm.create_record(OWNER, {'title': '会议录音原话', 'content': '这是会议的完整依据', 'customer_id': world['customer']['id']}, NOW)
    with crm._transaction() as db:
        db.execute('''CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,title TEXT,customer_id INTEGER,
            occurred_at REAL,created_at REAL,updated_at REAL)''')
        db.execute('''CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,title TEXT,record_id INTEGER,
            customer_id INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('CREATE TABLE crm_visit_sources(owner TEXT,visit_id INTEGER,material_id INTEGER)')
        db.execute('INSERT INTO crm_visits VALUES(?,?,?,?,?,?,?)', (1, OWNER, '电力方案交流', world['customer']['id'], NOW - 100, NOW, NOW))
        db.execute('INSERT INTO crm_materials VALUES(?,?,?,?,?,?,?)', (1, OWNER, '会议录音', extra['id'], world['customer']['id'], NOW, NOW))
        db.execute('INSERT INTO crm_visit_sources VALUES(?,?,?)', (OWNER, 1, 1))
    m = s.create(OWNER, {'title': '会议中的材料准备目标', 'visit_ids': [1],
        'action_record_ids': [world['actions'][0]['id']], 'request_id': 'visit-goal'})['matter']
    assert m['action_count'] == 1 and m['actions'][0]['id'] == world['actions'][0]['id']
    assert m['sources'][0]['id'] == extra['id']
    assert s.resolve(OWNER, 'material', 1)['items'][0]['id'] == m['id']
    assert s.resolve(OWNER, 'record', extra['id'])['items'][0]['id'] == m['id']
