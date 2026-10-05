"""Recoverable user lifecycle on disposable CRM records and reminders."""
import pytest

from secretary.customer_store import CustomerStore
from secretary.record_lifecycle import RecordLifecycle


NOW = 1791163200.0


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / 'lifecycle.sqlite3')
    lifecycle = RecordLifecycle(crm, clock=lambda: NOW)
    yield crm, lifecycle
    crm.close()


def note(crm, title='temporary idea', owner='owner'):
    return crm.create_record(owner, {'title': title, 'content': 'original source',
                                   'kind': 'action', 'status': 'following'}, NOW)


def proposal(crm, record, key, *, delta=3600, confirm=True):
    crm.execute('owner', key, {'action': 'propose', 'title': record['title'],
                              'remind_at': NOW + delta}, NOW)
    row = crm._db.execute('SELECT * FROM proposals ORDER BY id DESC LIMIT 1').fetchone()
    crm.link_proposal('owner', record['id'], row['id'], NOW)
    if confirm:
        crm.execute('owner', key + '-confirm', {'action': 'confirm', 'proposal_id': row['id']}, NOW)
    return crm._db.execute('SELECT * FROM proposals WHERE id=?', (row['id'],)).fetchone()


def move(lifecycle, record_id, action):
    return lifecycle.move('owner', record_id, {'action': action,
        'snapshot': lifecycle.preview('owner', record_id)['snapshot']})


def test_archive_cancel_all_historical_tasks_and_pending_proposals_then_restore(stack):
    crm, lifecycle = stack
    record = note(crm)
    first = proposal(crm, record, 'first')
    second = proposal(crm, record, 'second', delta=7200)
    pending = proposal(crm, record, 'pending', delta=10800, confirm=False)
    claim = crm.claim_due(NOW + 3601)
    assert claim
    preview = lifecycle.preview('owner', record['id'])
    assert preview['pending_task_count'] == 2 and preview['pending_proposal_count'] == 1
    archived = move(lifecycle, record['id'], 'archive')
    assert archived['visibility'] == 'archived' and archived['effects']['cancelled_tasks'] == 2
    assert archived['effects']['rejected_proposals'] == 1
    assert crm.get_record('owner', record['id']) is None
    assert {row[0] for row in crm._db.execute('SELECT status FROM tasks')} == {'cancelled'}
    assert {row[0] for row in crm._db.execute('SELECT status FROM notifications')} == {'obsolete'}
    assert crm.ack(claim['id'], claim['token'], NOW + 3601) is False
    assert crm._db.execute('SELECT status FROM proposals WHERE id=?', (pending['id'],)).fetchone()[0] == 'rejected'
    restored = move(lifecycle, record['id'], 'restore')
    assert restored['visibility'] == 'active' and restored['record']['status'] == record['status']
    assert restored['record']['original_content'] == record['original_content']
    assert crm.claim_due(NOW + 20000) is None
    assert {row[0] for row in crm._db.execute('SELECT status FROM tasks')} == {'cancelled'}
    assert {first['task_id'], second['task_id']} == {row[0] for row in crm._db.execute('SELECT id FROM tasks')}


def test_same_target_replay_and_reopen_are_safe(stack):
    crm, lifecycle = stack
    record = note(crm)
    snapshot = lifecycle.preview('owner', record['id'])['snapshot']
    first = lifecycle.move('owner', record['id'], {'action': 'trash', 'snapshot': snapshot})
    repeat = lifecycle.move('owner', record['id'], {'action': 'trash', 'snapshot': snapshot})
    assert first['changed'] and not repeat['changed'] and repeat['revision'] == first['revision']
    assert RecordLifecycle(crm, clock=lambda: NOW).preview('owner', record['id'])['visibility'] == 'trash'
    assert move(lifecycle, record['id'], 'archive')['visibility'] == 'archived'


def test_lifecycle_revision_invalidates_an_old_restore_snapshot(stack):
    crm, lifecycle = stack
    record = note(crm)
    archived = move(lifecycle, record['id'], 'archive')
    move(lifecycle, record['id'], 'trash')
    with pytest.raises(ValueError):
        lifecycle.move('owner', record['id'], {'action': 'restore', 'snapshot': archived['snapshot']})
    assert lifecycle.preview('owner', record['id'])['visibility'] == 'trash'


@pytest.mark.parametrize('change', ['record', 'task', 'proposal'])
def test_snapshot_rejects_changes_atomically(stack, change):
    crm, lifecycle = stack
    record = note(crm)
    scheduled = proposal(crm, record, 'scheduled')
    pending = proposal(crm, record, 'pending', delta=7200, confirm=False)
    snapshot = lifecycle.preview('owner', record['id'])['snapshot']
    if change == 'record':
        crm.update_record('owner', record['id'], {'content': 'edited source'}, NOW + 1)
    elif change == 'task':
        crm.execute('owner', 'adjust', {'action': 'snooze', 'task_id': scheduled['task_id'], 'remind_at': NOW + 4000}, NOW + 1)
    else:
        crm.execute('owner', 'reject', {'action': 'reject', 'proposal_id': pending['id']}, NOW + 1)
    with pytest.raises(ValueError):
        lifecycle.move('owner', record['id'], {'action': 'archive', 'snapshot': snapshot})
    assert crm.get_record('owner', record['id'])
    assert crm.get_task('owner', scheduled['task_id'])['status'] == 'pending'


def test_shared_active_task_refuses_whole_operation(stack):
    crm, lifecycle = stack
    record, other = note(crm), note(crm, 'independent action')
    scheduled = proposal(crm, record, 'scheduled')
    with crm._transaction() as db:
        shared = db.execute("INSERT INTO proposals(owner,title,status,task_id,created_at,updated_at) VALUES (?,?,'confirmed',?,?,?)",
                            ('owner', other['title'], scheduled['task_id'], NOW, NOW)).lastrowid
        crm._remember_proposal(db, 'owner', other['id'], shared, NOW)
        db.execute('UPDATE crm_records SET proposal_id=? WHERE owner=? AND id=?', (shared, 'owner', other['id']))
    preview = lifecycle.preview('owner', record['id'])
    assert preview['shared_tasks'] == 1
    assert preview['pending_tasks'][0]['shared_active_record_ids'] == [other['id']]
    with pytest.raises(ValueError, match='共享|其他'):
        move(lifecycle, record['id'], 'trash')
    assert crm.get_record('owner', record['id']) and crm.get_record('owner', other['id'])
    assert crm.get_task('owner', scheduled['task_id'])['status'] == 'pending'


def test_owner_legacy_hidden_and_validation_do_not_recover_internal_commands(stack):
    crm, lifecycle = stack
    record = note(crm)
    foreign = note(crm, 'private', 'other')
    hidden = note(crm, 'internal command')
    with crm._transaction() as db:
        db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (hidden['id'],))
    for identifier in (foreign['id'], hidden['id']):
        with pytest.raises(KeyError):
            lifecycle.preview('owner', identifier)
        with pytest.raises(KeyError):
            lifecycle.move('owner', identifier, {'action': 'restore', 'snapshot': '0' * 64})
    assert crm._db.execute('SELECT hidden FROM crm_records WHERE id=?', (hidden['id'],)).fetchone()[0] == 1
    for identifier in (True, 0, -1, '1'):
        with pytest.raises(ValueError):
            lifecycle.preview('owner', identifier)
    snapshot = lifecycle.preview('owner', record['id'])['snapshot']
    for payload in ({'action': 'destroy', 'snapshot': snapshot}, {'action': 'archive'},
                    {'action': 'archive', 'snapshot': snapshot, 'owner': 'other'}):
        with pytest.raises(ValueError):
            lifecycle.move('owner', record['id'], payload)


def test_root_only_lists_keep_search_and_pagination_isolated(stack):
    crm, lifecycle = stack
    first, second = note(crm, 'search-one'), note(crm, 'search-two')
    move(lifecycle, first['id'], 'archive')
    move(lifecycle, second['id'], 'archive')
    private = note(crm, 'search-private', 'other')
    lifecycle.move('other', private['id'], {'action': 'archive', 'snapshot': lifecycle.preview('other', private['id'])['snapshot']})
    page = lifecycle.list('owner', q='search', page=1, page_size=1)
    assert page['total'] == 2 and page['pages'] == 2 and len(page['items']) == 1
    assert lifecycle.list('owner', visibility='trash')['total'] == 0
    assert lifecycle.list('owner', q='search-one')['items'][0]['record']['id'] == first['id']
    for args in ({'visibility': 'active'}, {'page_size': True}, {'q': None}):
        with pytest.raises(ValueError):
            lifecycle.list('owner', **args)


def test_customer_name_survives_hidden_preview_and_archive_listing(stack):
    crm, lifecycle = stack
    customer = crm.create_customer('owner', {'name': '虚构归档客户'}, NOW)
    record = crm.create_record('owner', {'title': '已有客户的临时想法', 'content': '保留原文',
        'customer_id': customer['id']}, NOW)
    assert lifecycle.preview('owner', record['id'])['record']['customer_name'] == customer['name']
    archived = move(lifecycle, record['id'], 'archive')
    assert archived['record']['customer_name'] == customer['name']
    retained = lifecycle.list('owner')['items'][0]['record']
    assert retained['customer_name'] == customer['name']
    assert retained['original_content'] == record['original_content']
    assert crm.get_record('owner', record['id']) is None
