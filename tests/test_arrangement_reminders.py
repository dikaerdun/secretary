"""B5: disposable SQLite, injected clocks, no model and no external delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import json
import threading

import pytest

from secretary.arrangement_queue import ArrangementQueue
from secretary.arrangement_reminders import ArrangementReminders
from secretary.arrangement_time import normalize_time, time_end, time_start
from secretary.customer_store import CustomerStore
from secretary.store import SHANGHAI

OWNER = 'fictional-followup-owner'
NOW = datetime(2026, 10, 5, 10, tzinfo=SHANGHAI).timestamp()


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI).timestamp()


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'fictional-followup.sqlite3'
    crm = CustomerStore(path)
    with crm._transaction() as db:
        db.executescript('''CREATE TABLE crm_secretary_plans(
            id INTEGER PRIMARY KEY,owner TEXT NOT NULL,record_id INTEGER NOT NULL,visit_id INTEGER,
            data_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL,
            UNIQUE(owner,id),FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id));
            CREATE TABLE crm_secretary_turns(id INTEGER PRIMARY KEY,owner TEXT NOT NULL,plan_id INTEGER,
                status TEXT NOT NULL,data_json TEXT NOT NULL DEFAULT '{}',lease TEXT,claimed_at REAL,
                reply TEXT NOT NULL DEFAULT '',question TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL);''')
    clock = [NOW]
    queue = ArrangementQueue(crm, None, None, clock=lambda: clock[0])
    service = ArrangementReminders(queue, clock=lambda: clock[0])
    yield {'crm': crm, 'queue': queue, 'service': service, 'clock': clock, 'path': path}
    service.close()
    crm.close()


def plan(w, *, check='2026-10-05', deadline=None, strength='target', owner=OWNER,
         enabled=True, state='pending', hidden=False, registered=None, silent=None, check_id='same-check', epoch=0):
    record = w['crm'].create_record(owner, {'title': '虚构活动原话', 'content': '把未来活动定下来', 'status': 'following'}, NOW)
    data = {'title': '虚构电力方案拜访', 'followup_epoch': epoch, 'decision_mode': 'external'}
    if check is not None:
        data['next_check'] = {'check_id': check_id, 'status': 'planned',
                              'action': '问对方哪天方便', 'time_spec': normalize_time(check, NOW, role='check')}
    if deadline is not None:
        data['settle_deadline'] = {'strength': strength, 'time_spec': normalize_time(deadline, NOW, role='deadline')}
        data['deadline_registered_at'] = NOW if registered is None else registered
    if silent is not None:
        data['silent_until'] = silent
    with w['crm']._transaction() as db:
        if hidden:
            db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (record['id'],))
        identifier = db.execute('''INSERT INTO crm_secretary_plans(owner,record_id,data_json,revision,created_at,
            updated_at,schema_version,settling_state,followup_enabled,followup_disable_reason,next_check_at,deadline_end_at)
            VALUES(?,?,?,1,?,?,1,?,?,?, ?,?)''', (owner, record['id'], json.dumps(data, ensure_ascii=False),
            NOW, NOW, state, int(enabled), '' if enabled else 'user_disabled',
            time_start(data['next_check']['time_spec']) if check is not None else None,
            time_end(data['settle_deadline']['time_spec']) if deadline is not None else None)).lastrowid
    return identifier


def change(w, identifier, *, data=None, **columns):
    with w['crm']._transaction() as db:
        row = db.execute('SELECT * FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
        if data is not None:
            contents = json.loads(row['data_json'])
            contents.update(data)
            columns['data_json'] = json.dumps(contents, ensure_ascii=False)
        columns.setdefault('followup_version', row['followup_version'] + 1)
        columns.setdefault('revision', row['revision'] + 1)
        columns.setdefault('updated_at', w['clock'][0])
        db.execute('UPDATE crm_secretary_plans SET ' + ','.join(name + '=?' for name in columns) + ' WHERE id=?',
                   (*columns.values(), identifier))


def notice_rows(w, identifier=None):
    if identifier is None:
        return [dict(n) for n in w['crm']._db.execute('SELECT * FROM crm_arrangement_notifications ORDER BY id')]
    return [dict(n) for n in w['crm']._db.execute('SELECT * FROM crm_arrangement_notifications WHERE plan_id=? ORDER BY id', (identifier,))]


def publish_all(w):
    count = 0
    while (notice := w['service'].claim_due()) is not None:
        assert w['service'].publish(notice['id'], notice['token'])
        count += 1
    return count


def second_connection(w):
    crm = CustomerStore(w['path'])
    queue = ArrangementQueue(crm, None, None, clock=lambda: w['clock'][0])
    return crm, ArrangementReminders(queue, clock=lambda: w['clock'][0])


def test_construction_and_visible_reads_do_not_start_worker_or_write(world):
    plan(world)
    before = world['crm']._db.total_changes
    another = ArrangementReminders(world['queue'], clock=lambda: NOW)
    assert another.notices(OWNER) == {'items': [], 'total': 0, 'unread': 0}
    assert world['crm']._db.total_changes == before
    assert not hasattr(another, 'worker')
    another.close()
    with pytest.raises(RuntimeError):
        another.sweep()


def test_date_check_is_published_once_and_does_not_create_tasks(world):
    identifier = plan(world)
    assert world['service'].sweep()['generated'] == 1
    assert publish_all(world) == 1
    item = world['service'].notices(OWNER)['items'][0]
    assert item['plan_id'] == identifier and item['causes'][0]['kind'] == 'check'
    assert item['causes'] == item['reasons']
    assert item['causes'][0]['time_spec']['precision'] == 'date'
    assert publish_all(world) == 0
    assert world['crm']._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert world['crm']._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0


@pytest.mark.parametrize('value,before,due', [
    ('2026-10-06', '2026-10-05T23:59:59', '2026-10-06T00:00:00'),
    ('2026-10-06上午', '2026-10-06T08:59:59', '2026-10-06T09:00:00'),
    ('2026-10-06T10:05:00+08:00', '2026-10-06T10:04:59', '2026-10-06T10:05:00'),
])
def test_check_release_precision(world, value, before, due):
    plan(world, check=value)
    world['clock'][0] = at(before)
    assert world['service'].claim_due() is None
    world['clock'][0] = at(due)
    assert publish_all(world) == 1


@pytest.mark.parametrize('value', ['2026-10-04', '2026-10-04上午', '2026-10-04T10:00:00+08:00'])
def test_unhandled_check_not_expired_by_date_window_or_instant_end(world, value):
    plan(world, check=value)
    assert publish_all(world) == 1
    world['clock'][0] += 86400
    assert world['service'].notices(OWNER)['total'] == 1
    assert publish_all(world) == 0


def test_near_date_policy_is_previous_day_and_overdue_is_exclusive_end(world):
    identifier = plan(world, check=None, deadline='2026-10-07')
    world['clock'][0] = at('2026-10-06T00:00:00')
    assert publish_all(world) == 1
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['kind'] == 'deadline_near'
    world['clock'][0] = at('2026-10-07T23:59:59')
    assert publish_all(world) == 0
    world['clock'][0] = at('2026-10-08T00:00:00')
    assert publish_all(world) == 1
    active = world['service'].notices(OWNER)['items'][0]
    assert active['causes'][0]['kind'] == 'deadline_overdue'
    assert [n['status'] for n in notice_rows(world, identifier)] == ['obsolete', 'published']


@pytest.mark.parametrize('deadline,due', [
    ('2026-10-07T12:00:00+08:00', '2026-10-07T12:00:00'),
    ('2026-10-07上午', '2026-10-07T12:00:00'),
])
def test_instant_and_window_deadline_expire_at_end_not_next_day(world, deadline, due):
    plan(world, check=None, deadline=deadline)
    world['clock'][0] = at(due) - 1
    publish_all(world)
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['kind'] == 'deadline_near'
    world['clock'][0] += 1
    assert publish_all(world) == 1
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['kind'] == 'deadline_overdue'


@pytest.mark.parametrize('deadline', ['2026-10-05', '2026-10-05T12:00:00+08:00', '2026-10-05上午'])
def test_new_deadline_after_near_point_never_catches_up_near(world, deadline):
    plan(world, check=None, deadline=deadline)
    world['service'].sweep()
    assert all(n['kind'] == 'deadline_overdue' for n in notice_rows(world))
    assert world['service'].claim_due() is None


def test_ordinary_progress_updated_at_does_not_lose_registered_near(world):
    identifier = plan(world, check=None, deadline='2026-10-07')
    world['clock'][0] = at('2026-10-06T10:00:00')
    change(world, identifier, data={'last_progress': '正在等回复'})
    assert publish_all(world) == 1
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['kind'] == 'deadline_near'


@pytest.mark.parametrize('strength,text', [('required', '最晚确定期限已过'), ('target', '超过希望确定期限')])
def test_overdue_is_one_current_cause_with_strength_copy(world, strength, text):
    plan(world, check=None, deadline='2026-10-03', strength=strength)
    assert publish_all(world) == 1
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['text'] == text
    world['clock'][0] += 5 * 86400
    assert publish_all(world) == 0 and len(notice_rows(world)) == 1


def test_same_day_causes_have_distinct_rows_and_single_visible_group(world):
    identifier = plan(world, deadline='2026-10-04')
    another = plan(world, check_id='same-check')
    assert publish_all(world) == 3
    groups = world['service'].notices(OWNER)
    assert groups['total'] == groups['unread'] == 2
    item = next(i for i in groups['items'] if i['plan_id'] == identifier)
    assert len(item['member_ids']) == 2
    assert {r['kind'] for r in item['causes']} == {'check', 'deadline_overdue'}
    assert len(notice_rows(world, identifier)) == 2 and len(notice_rows(world, another)) == 1
    original = world['queue'].get(OWNER, identifier)
    read = world['service'].read(OWNER, item['id'])
    assert not read['unread'] and all(r['read_at'] == NOW for r in read['causes'])
    assert world['queue'].get(OWNER, identifier) == original
    assert world['service'].notices(OWNER)['unread'] == 1


def test_handling_one_cause_does_not_remove_other_cause(world):
    identifier = plan(world, deadline='2026-10-04')
    publish_all(world)
    check = world['queue'].get(OWNER, identifier)['next_check']
    change(world, identifier, data={'next_check': dict(check, status='handled', handled_at=NOW)})
    world['service'].sweep()
    item = world['service'].notices(OWNER)['items'][0]
    assert [c['kind'] for c in item['causes']] == ['deadline_overdue']
    assert {n['status'] for n in notice_rows(world)} == {'obsolete', 'published'}


def test_new_later_cause_not_premarked_read_by_previous_group(world):
    identifier = plan(world, deadline='2026-10-05T10:01:00+08:00')
    assert publish_all(world) == 1
    item = world['service'].notices(OWNER)['items'][0]
    world['service'].read(OWNER, item['id'])
    world['clock'][0] += 60
    assert publish_all(world) == 1
    item = world['service'].notices(OWNER)['items'][0]
    assert item['plan_id'] == identifier and len(item['causes']) == 2 and item['unread']
    assert sum(c['read_at'] is None for c in item['causes']) == 1


def test_owner_isolation_in_claim_identity_and_visible_read(world):
    plan(world, owner=OWNER)
    plan(world, owner='other-fictional-owner')
    assert publish_all(world) == 2
    mine = world['service'].notices(OWNER)['items'][0]
    foreign = world['service'].notices('other-fictional-owner')['items'][0]
    assert mine['plan_id'] != foreign['plan_id']
    with pytest.raises(KeyError):
        world['service'].read(OWNER, foreign['id'])
    assert world['service'].notices('nobody')['total'] == 0


def test_unpublished_rebind_clears_old_lease_and_stale_token_cannot_publish(world):
    identifier = plan(world)
    old = world['service'].claim_due()
    change(world, identifier, data={'last_progress': '还在等他回复'})
    assert world['service'].sweep()['rebound'] == 1
    assert not world['service'].publish(old['id'], old['token'])
    fresh = world['service'].claim_due()
    assert fresh['id'] == old['id'] and fresh['token'] != old['token']
    assert world['service'].publish(fresh['id'], fresh['token'])


def test_published_read_rebind_never_republishes_or_resets_read(world):
    identifier = plan(world, deadline='2026-10-04')
    publish_all(world)
    item = world['service'].notices(OWNER)['items'][0]
    world['service'].read(OWNER, item['id'])
    before = notice_rows(world)
    world['clock'][0] += 30
    change(world, identifier, data={'last_progress': '还在等', 'title': '补充标题'})
    assert world['service'].sweep()['rebound'] == 2
    after = notice_rows(world)
    assert [n['published_at'] for n in after] == [n['published_at'] for n in before]
    assert [n['read_at'] for n in after] == [n['read_at'] for n in before]
    assert publish_all(world) == 0 and world['service'].notices(OWNER)['unread'] == 0
    assert world['service'].notices(OWNER)['items'][0]['title'] == '补充标题'


def test_current_reason_copy_updates_but_original_publish_evidence_is_retained(world):
    identifier = plan(world, deadline='2026-10-04')
    publish_all(world)
    item = world['service'].notices(OWNER)['items'][0]
    world['service'].read(OWNER, item['id'])
    original = [n['payload_json'] for n in notice_rows(world)]
    current = world['queue'].get(OWNER, identifier)
    change(world, identifier, data={'next_check': dict(current['next_check'], action='核对新的方案反馈'),
           'settle_deadline': dict(current['settle_deadline'], strength='required')})
    world['service'].sweep()
    item = world['service'].notices(OWNER)['items'][0]
    assert {c['text'] for c in item['causes']} == {'核对新的方案反馈', '最晚确定期限已过'}
    assert not item['unread'] and publish_all(world) == 0
    assert original == [n['payload_json'] for n in notice_rows(world)]


@pytest.mark.parametrize('state,enabled', [('paused', True), ('abandoned', True), ('settled', True), ('pending', False)])
def test_inactive_or_gate_off_obsoletes_all_causes_without_touching_task(world, state, enabled):
    identifier = plan(world, deadline='2026-10-04')
    lease = world['service'].claim_due()
    change(world, identifier, settling_state=state, followup_enabled=int(enabled))
    assert not world['service'].publish(lease['id'], lease['token'])
    world['service'].sweep()
    assert {n['status'] for n in notice_rows(world)} == {'obsolete'}
    assert world['service'].notices(OWNER)['total'] == 0


@pytest.mark.parametrize('trash', [False, True])
def test_hidden_source_cannot_publish_and_restore_visibility_does_not_revive(world, trash):
    identifier = plan(world)
    lease = world['service'].claim_due()
    record_id = world['queue'].get(OWNER, identifier)['record_id']
    with world['crm']._transaction() as db:
        if trash:
            db.execute('CREATE TABLE crm_secretary_trash(owner TEXT,record_id INTEGER,trashed_at REAL)')
            db.execute('INSERT INTO crm_secretary_trash VALUES(?,?,?)', (OWNER, record_id, NOW))
        else:
            db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (record_id,))
    assert not world['service'].publish(lease['id'], lease['token'])
    world['service'].sweep()
    with world['crm']._transaction() as db:
        if trash:
            db.execute('DELETE FROM crm_secretary_trash')
        else:
            db.execute('UPDATE crm_records SET hidden=0 WHERE id=?', (record_id,))
    change(world, identifier, followup_enabled=0, followup_disable_reason='source_hidden')
    assert publish_all(world) == 0
    assert notice_rows(world)[0]['status'] == 'obsolete'


def test_explicit_resume_new_epoch_and_check_identity_without_reviving_obsolete(world):
    identifier = plan(world, deadline='2026-10-07')
    world['service'].sweep()
    old = notice_rows(world)
    change(world, identifier, settling_state='paused', followup_enabled=0)
    world['service'].sweep()
    check = world['queue'].get(OWNER, identifier)['next_check']
    change(world, identifier, settling_state='pending', followup_enabled=1,
           data={'followup_epoch': 1, 'followup_resumed_at': NOW,
                 'next_check': dict(check, check_id='explicit-resume-new-check')})
    assert publish_all(world) == 1
    rows = notice_rows(world)
    assert all(n['status'] == 'obsolete' for n in rows if n['id'] in {r['id'] for r in old})
    assert len(rows) == 6
    assert world['queue'].get(OWNER, identifier)['settling_cycle_id'] == 1
    stored = world['crm']._db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    assert json.loads(stored['data_json'])['followup_epoch'] == 1


def test_resume_after_near_skips_near_and_past_gate_stays_off(world):
    identifier = plan(world, check=None, deadline='2026-10-07')
    world['service'].sweep()
    change(world, identifier, settling_state='paused', followup_enabled=0)
    world['service'].sweep()
    world['clock'][0] = at('2026-10-08T10:00:00')
    change(world, identifier, settling_state='pending', followup_enabled=1,
           data={'followup_epoch': 1, 'followup_resumed_at': world['clock'][0]})
    assert publish_all(world) == 1
    assert world['service'].notices(OWNER)['items'][0]['causes'][0]['kind'] == 'deadline_overdue'
    assert sum(n['status'] != 'obsolete' for n in notice_rows(world)) == 1
    change(world, identifier, followup_enabled=0, followup_disable_reason='needs_selection')
    assert publish_all(world) == 0


def test_obsolete_identity_cannot_be_resurrected_by_gate_or_version(world):
    identifier = plan(world)
    publish_all(world)
    change(world, identifier, followup_enabled=0)
    world['service'].sweep()
    change(world, identifier, followup_enabled=1)
    assert publish_all(world) == 0
    assert len(notice_rows(world)) == 1 and notice_rows(world)[0]['status'] == 'obsolete'


def test_quiet_until_blocks_check_and_deadline_without_losing_causes(world):
    boundary = at('2026-10-09T09:00:00')
    plan(world, check='2026-10-09上午', deadline='2026-10-08', silent={'until_at': boundary})
    world['clock'][0] = boundary - 1
    assert world['service'].claim_due() is None
    world['clock'][0] = boundary
    assert publish_all(world) == 2
    item = world['service'].notices(OWNER)['items'][0]
    assert {c['kind'] for c in item['causes']} == {'check', 'deadline_overdue'}
    assert len(item['member_ids']) == 2


def test_new_quiet_period_hides_already_published_then_preserves_read_history(world):
    identifier = plan(world)
    publish_all(world)
    item = world['service'].notices(OWNER)['items'][0]
    world['service'].read(OWNER, item['id'])
    change(world, identifier, data={'silent_until': NOW + 60})
    world['service'].sweep()
    assert world['service'].notices(OWNER)['total'] == 0
    world['clock'][0] += 60
    assert world['service'].notices(OWNER)['unread'] == 0
    assert publish_all(world) == 0


@pytest.mark.parametrize('field,value,projection', [('next_check', {'time_spec': 'bad', 'status': 'planned', 'check_id': 'same-check'}, {}),
    ('silent_until', {'bad': 'quiet'}, {}),
    ('next_check', None, {}),
    ('settle_deadline', {'time_spec': 'bad'}, {'deadline_end_at': 1})])
def test_invalid_time_or_explicit_clear_never_manufactures_notification(world, field, value, projection):
    identifier = plan(world, check=None if field == 'settle_deadline' else '2026-10-05')
    change(world, identifier, data={field: value}, **projection)
    assert publish_all(world) == 0


def test_changed_check_time_with_same_id_obsoletes_not_misuses_old_identity(world):
    identifier = plan(world)
    world['service'].sweep()
    check = world['queue'].get(OWNER, identifier)['next_check']
    spec = normalize_time('2026-10-06', NOW, role='check')
    change(world, identifier, data={'next_check': dict(check, time_spec=spec)}, next_check_at=time_start(spec))
    world['clock'][0] += 86400
    assert publish_all(world) == 0
    assert notice_rows(world)[0]['status'] == 'obsolete'


def test_retry_backoff_and_expired_token_are_strict(world):
    plan(world)
    first = world['service'].claim_due()
    assert world['service'].retry(first['id'], first['token'])
    assert not world['service'].publish(first['id'], first['token'])
    assert world['service'].claim_due() is None
    world['clock'][0] += 30
    second = world['service'].claim_due()
    assert second['attempts'] == 2 and second['token'] != first['token']
    assert world['service'].retry(second['id'], second['token'])
    world['clock'][0] += 59
    assert world['service'].claim_due() is None
    world['clock'][0] += 1
    third = world['service'].claim_due()
    world['clock'][0] += world['service'].LEASE_SECONDS
    assert not world['service'].publish(third['id'], third['token'])
    assert not world['service'].retry(third['id'], third['token'])
    fourth = world['service'].claim_due()
    assert fourth['id'] == first['id'] and fourth['token'] != third['token']
    assert world['service'].publish(fourth['id'], fourth['token'])


def test_visible_read_is_pure_and_pagination_counts_full_groups(world):
    for n in range(5):
        plan(world, check_id=f'check-{n}')
    publish_all(world)
    before = world['crm']._db.total_changes
    page = world['service'].notices(OWNER, limit=2, offset=2)
    assert len(page['items']) == 2 and page['total'] == page['unread'] == 5
    assert world['crm']._db.total_changes == before
    with pytest.raises(ValueError):
        world['service'].notices(OWNER, limit=True)
    with pytest.raises(ValueError):
        world['service'].notices(OWNER, offset=-1)


def test_second_connection_cannot_claim_or_publish_after_pause(world):
    identifier = plan(world)
    crm2, reminders2 = second_connection(world)
    try:
        first = world['service'].claim_due()
        assert reminders2.claim_due() is None
        with crm2._transaction() as db:
            db.execute("UPDATE crm_secretary_plans SET settling_state='paused',followup_version=followup_version+1 WHERE id=?", (identifier,))
        assert not world['service'].publish(first['id'], first['token'])
        assert reminders2.notices(OWNER)['total'] == 0
        reminders2.sweep()
        assert notice_rows(world)[0]['status'] == 'obsolete'
    finally:
        reminders2.close()
        crm2.close()


def test_two_connections_racing_claim_exactly_one_lease(world):
    plan(world)
    crm2, reminders2 = second_connection(world)
    barrier = threading.Barrier(2)
    def run(service):
        barrier.wait(timeout=5)
        return service.claim_due()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, world['service'])
            b = pool.submit(run, reminders2)
            results = [a.result(timeout=10), b.result(timeout=10)]
        assert sum(r is not None for r in results) == 1
        claimed = next(r for r in results if r)
        assert world['service'].publish(claimed['id'], claimed['token'])
        assert not reminders2.publish(claimed['id'], claimed['token'])
        assert len(notice_rows(world)) == 1
    finally:
        reminders2.close()
        crm2.close()


def test_restart_preserves_published_and_read_without_history_flood(world):
    plan(world, deadline='2026-10-04')
    publish_all(world)
    item = world['service'].notices(OWNER)['items'][0]
    world['service'].read(OWNER, item['id'])
    before = notice_rows(world)
    world['clock'][0] += 14 * 86400
    crm2, reminders2 = second_connection(world)
    try:
        assert reminders2.claim_due() is None
        assert reminders2.notices(OWNER)['total'] == 1 and reminders2.notices(OWNER)['unread'] == 0
        assert notice_rows(world) == before
    finally:
        reminders2.close()
        crm2.close()


def test_publish_failure_rolls_back_before_success_receipt(world, monkeypatch):
    plan(world)
    claim = world['service'].claim_due()
    import secretary.arrangement_reminders as module
    def broken(_):
        raise RuntimeError('synthetic write failure')
    monkeypatch.setattr(module, '_json', broken)
    with pytest.raises(RuntimeError):
        world['service'].publish(claim['id'], claim['token'])
    row = notice_rows(world)[0]
    assert row['status'] == 'leased' and row['published_at'] is None and row['token'] == claim['token']


def test_publish_write_before_failed_commit_does_not_lose_or_acknowledge_notice(world, monkeypatch):
    plan(world)
    claim = world['service'].claim_due()
    transaction = world['queue']._transaction
    @contextmanager
    def fail_commit():
        with transaction() as db:
            yield db
            assert db.execute('SELECT status FROM crm_arrangement_notifications WHERE id=?', (claim['id'],)).fetchone()[0] == 'published'
            raise RuntimeError('synthetic failure after write before commit')
    monkeypatch.setattr(world['queue'], '_transaction', fail_commit)
    with pytest.raises(RuntimeError):
        world['service'].publish(claim['id'], claim['token'])
    row = notice_rows(world)[0]
    assert row['status'] == 'leased' and row['published_at'] is None and row['token'] == claim['token']
    monkeypatch.setattr(world['queue'], '_transaction', transaction)
    assert world['service'].publish(claim['id'], claim['token'])


def hold(w, identifier, *, generation=1, turn_generation=None, until=None, turn_id=100):
    with w['crm']._transaction() as db:
        db.execute('''INSERT INTO crm_secretary_turns(id,owner,plan_id,status,data_json,lease,claimed_at,updated_at)
            VALUES(?,?,?,'processing',?,'fictional-processing-lease',?,?)''',
            (turn_id, OWNER, identifier, json.dumps({'arrangement_hold_generation': generation if turn_generation is None else turn_generation}), NOW, NOW))
        db.execute('''UPDATE crm_secretary_plans SET followup_dirty=1,followup_hold_until=?,hold_turn_id=?,hold_generation=?
            WHERE id=?''', (NOW + 60 if until is None else until, turn_id, generation, identifier))


def test_hold_hides_without_consuming_notice_and_timeout_invalidates_processing_lease(world):
    identifier = plan(world)
    publish_all(world)
    before = notice_rows(world)[0]
    hold(world, identifier)
    assert world['service'].notices(OWNER)['total'] == 0
    assert world['service'].claim_due() is None
    assert notice_rows(world)[0]['status'] == 'published'
    world['clock'][0] += 60
    assert world['service'].sweep()['expired_holds'] == 1
    turn = world['crm']._db.execute('SELECT * FROM crm_secretary_turns WHERE id=100').fetchone()
    assert turn['status'] == 'failed' and turn['lease'] is turn['claimed_at'] is None
    assert '原话已保存' in turn['reply'] and '超时' in turn['error']
    assert not world['crm']._db.execute("SELECT 1 FROM crm_secretary_turns WHERE id=100 AND status='processing' AND lease='fictional-processing-lease'").fetchone()
    projected = world['queue'].get(OWNER, identifier)
    assert projected['hold_turn_id'] is None and projected['hold_generation'] == 2
    assert not projected['followup_dirty'] and world['service'].notices(OWNER)['total'] == 1
    after = notice_rows(world)[0]
    assert before['published_at'] == after['published_at'] and after['status'] == 'published'


def test_timeout_fails_only_matching_owner_plan_generation(world):
    identifier = plan(world)
    hold(world, identifier, generation=3, turn_generation=2)
    world['clock'][0] += 60
    stats = world['service'].sweep()
    assert stats['expired_holds'] == 0 and stats['unmatched_holds'] == 1
    assert world['queue'].get(OWNER, identifier)['hold_generation'] == 3
    assert world['service'].claim_due() is None


def test_new_hold_not_released_when_previous_generation_has_expired(world):
    identifier = plan(world)
    hold(world, identifier, generation=1, until=NOW)
    hold(world, identifier, generation=2, until=NOW + 60, turn_id=101)
    assert world['service'].sweep()['expired_holds'] == 0
    projected = world['queue'].get(OWNER, identifier)
    assert projected['hold_turn_id'] == 101 and projected['hold_generation'] == 2 and projected['followup_dirty']
    world['clock'][0] += 60
    assert world['service'].sweep()['expired_holds'] == 1
    turns = {n['id']: n['status'] for n in world['crm']._db.execute('SELECT * FROM crm_secretary_turns')}
    assert turns == {100: 'processing', 101: 'failed'}


def test_expired_hold_cannot_override_paused_or_hidden_persistent_gate(world):
    identifier = plan(world)
    hold(world, identifier, until=NOW)
    change(world, identifier, settling_state='paused', followup_enabled=0, followup_disable_reason='paused')
    assert world['service'].sweep()['expired_holds'] == 1
    assert world['service'].claim_due() is None
    assert not world['queue'].get(OWNER, identifier)['followup_enabled']


def test_nested_notification_transaction_rolls_back_with_outer_transaction(world):
    plan(world)
    with pytest.raises(RuntimeError):
        with world['crm']._transaction():
            world['service'].sweep()
            assert len(notice_rows(world)) == 1
            raise RuntimeError('rollback synthetic outer transaction')
    assert notice_rows(world) == []


def test_old_migrated_plan_never_generates_without_explicit_opt_in(world):
    identifier = plan(world)
    with world['crm']._transaction() as db:
        db.execute('UPDATE crm_secretary_plans SET schema_version=0 WHERE id=?', (identifier,))
    world['queue'].upgrade_schema()
    assert world['queue'].get(OWNER, identifier)['followup_disable_reason'] == 'legacy_no_opt_in'
    assert world['service'].sweep()['generated'] == 0
    assert world['service'].claim_due() is None
    assert notice_rows(world) == []
