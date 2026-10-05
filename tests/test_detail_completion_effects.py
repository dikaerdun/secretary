"""DD28 completion impact reads on disposable synthetic databases."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 6, 10, tzinfo=SHANGHAI).timestamp()
OWNER = 'detail-completion-synthetic'


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'dd28-synthetic.sqlite3')
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    yield SimpleNamespace(crm=crm, sales=sales)
    crm.close()


def action(w, title='核对实际结果', *, owner=OWNER, parent=None):
    return w.crm.create_record(owner, {'title': title, 'content': title, 'kind': 'action',
                                     'status': 'following', 'parent_record_id': parent}, NOW)


def task(w, title, *, owner=OWNER, status='pending', delta=3600, revision=1):
    with w.crm._transaction() as db:
        identifier = db.execute(
            "INSERT INTO tasks(owner,title,remind_at,duration_minutes,status,revision,created_at,updated_at) "
            "VALUES (?,?,?,45,?,?,?,?)", (owner, title, NOW + delta, status, revision, NOW, NOW)).lastrowid
    return w.crm.get_task(owner, identifier)


def proposal(w, record, title, *, owner=OWNER, status='pending', task_id=None,
             target_task_id=None, current=False, change_kind='schedule', record_owner=OWNER):
    # Legacy rows with both references are intentional COALESCE counterexamples.
    with w.crm._transaction() as db:
        identifier = db.execute(
            "INSERT INTO proposals(owner,title,remind_at,status,task_id,target_task_id,change_kind,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (owner, title, NOW + 7200, status, task_id, target_task_id, change_kind, NOW, NOW)).lastrowid
        if record is not None:
            w.crm._remember_proposal(db, record_owner, record['id'], identifier, NOW)
        if current:
            db.execute('UPDATE crm_records SET proposal_id=? WHERE owner=? AND id=?',
                       (identifier, record_owner, record['id']))
    return w.crm.get_proposal(owner, identifier)


def database_state(w):
    schema = [tuple(row) for row in w.crm._db.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    tables = {}
    for row in w.crm._db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        name = row['name']
        quoted = '"' + name.replace('"', '""') + '"'
        tables[name] = [tuple(item) for item in w.crm._db.execute('SELECT * FROM ' + quoted + ' ORDER BY rowid')]
    return schema, tables


def test_dd28_reads_all_record_history_but_excludes_child_and_finished_tasks(world, monkeypatch):
    w = world
    record = action(w)
    first = task(w, '原历史活动', revision=3)
    latest = task(w, '当前活动', delta=10800)
    rejected_task = task(w, '已拒提案仍关联有效活动', delta=14400)
    completed = task(w, '已完成历史活动', status='completed')
    cancelled = task(w, '已取消历史活动', status='cancelled')
    child = action(w, '另一次活动', parent=record['id'])
    child_task = task(w, '子待办日程', delta=18000)
    proposal(w, record, '历史提案', status='confirmed', task_id=first['id'])
    proposal(w, record, '当前提案', status='confirmed', task_id=latest['id'], current=True)
    proposal(w, record, '已拒历史提案', status='rejected', task_id=rejected_task['id'])
    proposal(w, record, '已完成提案', status='confirmed', task_id=completed['id'])
    proposal(w, record, '已取消提案', status='confirmed', task_id=cancelled['id'])
    pending = proposal(w, record, '待确认提案')
    proposal(w, child, '子待办提案', status='confirmed', task_id=child_task['id'], current=True)
    before = database_state(w)
    changes = w.crm._db.total_changes
    monkeypatch.setattr(w.crm, '_execute', lambda *args: pytest.fail('completion read executed a command'))

    effects = w.sales.completion_effects(OWNER, record['id'])

    assert effects['scope'] == 'record_completion' and effects['record_id'] == record['id']
    assert effects['snapshot'] == w.sales.completion_snapshot(OWNER, record['id'])
    assert effects['pending_tasks'] == [
        {key: item[key] for key in ('id', 'title', 'remind_at', 'duration_minutes', 'status', 'revision')}
        for item in (first, latest, rejected_task)]
    assert effects['pending_proposals'] == [
        {key: pending[key] for key in ('id', 'title', 'remind_at', 'change_kind', 'status')}]
    assert effects['pending_task_count'] == 3 and effects['pending_proposal_count'] == 1
    assert w.crm._db.total_changes == changes and database_state(w) == before


def test_dd28_coalesce_precedence_target_only_and_dedup_match_actual_completion(world):
    w = world
    record = action(w)
    primary = task(w, '主任务')
    target_only = task(w, '仅target关联任务', delta=10800)
    snapshot_only = task(w, '只参与宽快照的另一引用', delta=14400)
    proposal(w, record, '主提案', status='confirmed', task_id=primary['id'], current=True)
    duplicate = proposal(w, record, '重复引用', task_id=primary['id'])
    target = proposal(w, record, '变更提案', target_task_id=target_only['id'], change_kind='reschedule')
    both = proposal(w, record, '双引用旧提案', task_id=primary['id'], target_task_id=snapshot_only['id'])
    effects = w.sales.completion_effects(OWNER, record['id'])
    assert [item['id'] for item in effects['pending_tasks']] == [primary['id'], target_only['id']]
    assert [item['id'] for item in effects['pending_proposals']] == [duplicate['id'], target['id'], both['id']]
    assert effects['pending_task_count'] == 2 and effects['pending_proposal_count'] == 3

    w.sales.complete_record(OWNER, record['id'], {'request_id': 'explicit-dd28', 'result': '已核对结果',
                                               'expected_completion_snapshot': effects['snapshot']})

    assert {row['id'] for row in w.crm._db.execute("SELECT id FROM tasks WHERE status='completed'")} == {
        item['id'] for item in effects['pending_tasks']}
    assert {row['id'] for row in w.crm._db.execute("SELECT id FROM proposals WHERE status='rejected'")} == {
        item['id'] for item in effects['pending_proposals']}
    assert w.crm.get_task(OWNER, snapshot_only['id'])['status'] == 'pending'


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_dd28_nonpending_primary_reference_does_not_fall_back_to_pending_target(world, status):
    w = world
    record = action(w)
    primary = task(w, '非pending主引用', status=status)
    target = task(w, '未受完成影响的target', delta=10800)
    pending = proposal(w, record, '双引用提案', task_id=primary['id'], target_task_id=target['id'], current=True)
    effects = w.sales.completion_effects(OWNER, record['id'])
    assert effects['pending_tasks'] == [] and effects['pending_task_count'] == 0
    assert [item['id'] for item in effects['pending_proposals']] == [pending['id']]
    w.sales.complete_record(OWNER, record['id'], {'request_id': 'no-fallback-dd28', 'result': '只结束本待办',
                                               'expected_completion_snapshot': effects['snapshot']})
    assert w.crm.get_task(OWNER, target['id'])['status'] == 'pending'
    assert w.crm.get_task(OWNER, primary['id'])['status'] == status


def test_dd28_current_proposal_is_included_even_without_a_history_link(world):
    w = world
    record = action(w)
    current_task = task(w, '旧版当前任务')
    current = proposal(w, record, '旧版当前提案', task_id=current_task['id'], current=True)
    with w.crm._transaction() as db:
        db.execute('DELETE FROM crm_record_proposals WHERE proposal_id=?', (current['id'],))
    before = database_state(w)
    effects = w.sales.completion_effects(OWNER, record['id'])
    assert [item['id'] for item in effects['pending_tasks']] == [current_task['id']]
    assert [item['id'] for item in effects['pending_proposals']] == [current['id']]
    assert database_state(w) == before


def test_dd28_nested_task_completion_rejections_match_actual_proposal_status_difference(world):
    w = world
    record = action(w)
    other_record = action(w, '另一待办')
    affected = task(w, '将完成的日程')
    unaffected = task(w, '未关联日程', delta=10800)
    proposal(w, record, '当前提案', status='confirmed', task_id=affected['id'], current=True)
    direct = proposal(w, record, '直接关联待确认提案')
    unlinked = proposal(w, None, '未关联记录的变更提案', target_task_id=affected['id'], change_kind='reschedule')
    other = proposal(w, other_record, '另待办的变更提案', target_task_id=affected['id'], current=True)
    unrelated = proposal(w, None, '另一日程的变更提案', target_task_id=unaffected['id'])
    foreign = proposal(w, None, '他人变更提案', owner='another-owner', target_task_id=affected['id'])
    before = {row['id']: row['status'] for row in w.crm._db.execute('SELECT id,status FROM proposals')}
    business = database_state(w)
    changes = w.crm._db.total_changes

    effects = w.sales.completion_effects(OWNER, record['id'])

    expected = {direct['id'], unlinked['id'], other['id']}
    assert {item['id'] for item in effects['pending_proposals']} == expected
    assert effects['pending_proposal_count'] == 3
    assert w.crm._db.total_changes == changes and database_state(w) == business
    w.sales.complete_record(OWNER, record['id'], {'request_id': 'nested-dd28', 'result': '明确完成',
                                               'expected_completion_snapshot': effects['snapshot']})
    after = {row['id']: row['status'] for row in w.crm._db.execute('SELECT id,status FROM proposals')}
    assert {identifier for identifier, status in before.items()
            if status == 'pending' and after[identifier] == 'rejected'} == expected
    assert w.crm.get_proposal(OWNER, unrelated['id'])['status'] == 'pending'
    assert w.crm.get_proposal('another-owner', foreign['id'])['status'] == 'pending'
    assert w.crm.get_record(OWNER, other_record['id'])['status'] == 'following'


def test_dd28_original_snapshot_does_not_bind_unlinked_target_only_proposal_changes(world):
    # Preserve this existing limitation without widening the completion token
    # or changing any write contract in a read-only projection increment.
    w = world
    record = action(w)
    affected = task(w, '将完成的日程')
    proposal(w, record, '当前提案', status='confirmed', task_id=affected['id'], current=True)
    outside = proposal(w, None, '未关联记录的变更提案', target_task_id=affected['id'])
    seen = w.sales.completion_effects(OWNER, record['id'])
    with w.crm._transaction() as db:
        db.execute('UPDATE proposals SET title=?,remind_at=?,updated_at=? WHERE id=?',
                   ('变更后的提案', NOW + 21600, NOW + 1, outside['id']))
    latest = w.sales.completion_effects(OWNER, record['id'])
    assert latest['snapshot'] == seen['snapshot']
    assert latest['pending_proposals'] != seen['pending_proposals']
    assert latest['pending_proposals'][0]['title'] == '变更后的提案'
    w.sales.complete_record(OWNER, record['id'], {'request_id': 'existing-token-scope-dd28', 'result': '原范围保留',
                                               'expected_completion_snapshot': seen['snapshot']})
    assert w.crm.get_proposal(OWNER, outside['id'])['status'] == 'rejected'


@pytest.mark.parametrize('change', ['historical_task', 'pending_proposal', 'snapshot_only_task'])
def test_dd28_old_snapshot_rejects_changed_history_without_partial_completion(world, change):
    w = world
    record = action(w)
    first = task(w, '历史任务')
    latest = task(w, '当前任务', delta=10800)
    snapshot_only = task(w, '只参与快照的任务', delta=14400)
    proposal(w, record, '历史提案', status='confirmed', task_id=first['id'])
    proposal(w, record, '当前提案', status='confirmed', task_id=latest['id'], current=True)
    pending = proposal(w, record, '待确认提案', task_id=latest['id'], target_task_id=snapshot_only['id'])
    effects = w.sales.completion_effects(OWNER, record['id'])
    with w.crm._transaction() as db:
        if change == 'pending_proposal':
            db.execute('UPDATE proposals SET remind_at=?,updated_at=? WHERE id=?', (NOW + 21600, NOW + 1, pending['id']))
        else:
            identifier = first['id'] if change == 'historical_task' else snapshot_only['id']
            db.execute('UPDATE tasks SET remind_at=?,revision=revision+1,updated_at=? WHERE id=?',
                       (NOW + 21600, NOW + 1, identifier))
    before = database_state(w)
    assert w.sales.completion_effects(OWNER, record['id'])['snapshot'] != effects['snapshot']
    with pytest.raises(ValueError, match='变化'):
        w.sales.complete_record(OWNER, record['id'], {'request_id': 'stale-dd28', 'result': '原稿',
                                                   'expected_completion_snapshot': effects['snapshot']})
    assert database_state(w) == before


def test_dd28_completion_reads_snapshot_and_effects_in_one_lock_scope(world, monkeypatch):
    w = world
    record = action(w)
    pending_task = task(w, '同锁日程')
    proposal(w, record, '同锁提案', task_id=pending_task['id'])
    real_lock = w.crm._lock

    class ObservedLock:
        span = 0
        active = None

        def __enter__(self):
            real_lock.acquire()
            self.span += 1
            self.active = self.span
            return self

        def __exit__(self, *args):
            self.active = None
            real_lock.release()

    lock = ObservedLock()
    monkeypatch.setattr(w.crm, '_lock', lock)
    queries = []
    w.crm._db.set_trace_callback(lambda statement: queries.append((statement, lock.active)))
    try:
        effects = w.sales.completion_effects(OWNER, record['id'])
    finally:
        w.crm._db.set_trace_callback(None)
    assert effects['pending_task_count'] == 1
    assert queries and lock.span == 1
    assert {span for statement, span in queries} == {1}
    assert all(statement.lstrip().upper().startswith('SELECT') for statement, span in queries)


@pytest.mark.parametrize('inaccessible', ['foreign_owner', 'hidden', 'missing'])
def test_dd28_completion_projection_preserves_visibility_and_owner_boundaries(world, inaccessible):
    w = world
    record = action(w)
    pending_task = task(w, '私有任务')
    proposal(w, record, '私有提案', task_id=pending_task['id'], current=True)
    owner, identifier = OWNER, record['id']
    if inaccessible == 'foreign_owner':
        owner = 'another-owner'
    elif inaccessible == 'hidden':
        with w.crm._transaction() as db:
            db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (identifier,))
    else:
        identifier += 9999
    before = database_state(w)
    changes = w.crm._db.total_changes
    with pytest.raises(KeyError):
        w.sales.completion_effects(owner, identifier)
    assert w.crm._db.total_changes == changes and database_state(w) == before


def test_dd28_projection_does_not_follow_foreign_owner_proposal_or_task(world):
    w = world
    record = action(w)
    foreign_task = task(w, '他人任务', owner='another-owner')
    proposal(w, record, '损坏的跨owner历史引用', owner='another-owner', task_id=foreign_task['id'])
    local = proposal(w, record, '本owner但指向他人task', task_id=foreign_task['id'], current=True)
    effects = w.sales.completion_effects(OWNER, record['id'])
    assert effects['pending_tasks'] == [] and effects['pending_task_count'] == 0
    assert [item['id'] for item in effects['pending_proposals']] == [local['id']]
    w.sales.complete_record(OWNER, record['id'], {'request_id': 'owner-dd28', 'result': '仅完成本记录',
                                               'expected_completion_snapshot': effects['snapshot']})
    assert w.crm.get_task('another-owner', foreign_task['id'])['status'] == 'pending'


def test_dd28_empty_record_projection_has_a_usable_snapshot(world):
    w = world
    record = action(w)
    effects = w.sales.completion_effects(OWNER, record['id'])
    assert effects['pending_tasks'] == effects['pending_proposals'] == []
    assert effects['pending_task_count'] == effects['pending_proposal_count'] == 0
    assert effects['snapshot'] == w.sales.completion_snapshot(OWNER, record['id'])
