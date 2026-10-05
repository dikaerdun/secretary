"""Real Store persistence and app lifecycle boundaries, using synthetic tmp DBs."""
import asyncio
import json

from aiohttp import web
import pytest

from deploy.training import build_training_app, training_config
from secretary.store import Store
from secretary.web import ARRANGEMENT_WORKER

NOW = 1791172800.0
START = NOW + 86400
CHANGED = START + 7200
OWNER = 'synthetic-delivery-boundary-user'
MODES = ('missing', 'disabled', 'timestamp')


def notice_fields(mode, at):
    return {} if mode == 'missing' else {'execution_notice_at': None if mode == 'disabled' else at}


def create_proposal(store, source, *, mode='missing', start=START, action='propose', task_id=None):
    command = {'action': action, 'title': '虚构客户技术汇报', 'remind_at': start,
               'duration_minutes': 45, **notice_fields(mode, start - 1800)}
    if task_id is not None:
        command['task_id'] = task_id
    store.execute(OWNER, source, command, NOW)
    row = store._db.execute('SELECT * FROM proposals WHERE owner=? ORDER BY id DESC LIMIT 1', (OWNER,)).fetchone()
    assert row is not None and row['status'] == 'pending'
    return row['id']


def queued(store):
    return [dict(row) for row in store._db.execute(
        "SELECT * FROM notifications WHERE status IN ('queued','leased') ORDER BY id")]


def row_snapshot(store):
    return {table: [dict(row) for row in store._db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
            for table in ('tasks', 'notifications', 'proposals')}


def assert_repeat_confirmation(store, identifier, original):
    before = row_snapshot(store)
    assert store.execute(OWNER, 'confirm', {'action': 'confirm', 'proposal_id': identifier}, NOW) == original
    reply = store.execute(OWNER, 'confirm-again', {'action': 'confirm', 'proposal_id': identifier}, NOW)
    assert '不会重复建立任务' in reply
    assert row_snapshot(store) == before


@pytest.mark.parametrize('mode', MODES)
def test_creation_three_notice_states_survive_two_restarts_and_duplicate_confirm(tmp_path, mode):
    path = tmp_path / 'synthetic-create.sqlite3'
    store = Store(path)
    try:
        identifier = create_proposal(store, 'create', mode=mode)
        encoded = store._db.execute('SELECT execution_notice_json FROM proposals WHERE id=?', (identifier,)).fetchone()[0]
        assert encoded is None if mode == 'missing' else json.loads(encoded) == (None if mode == 'disabled' else START - 1800)
    finally:
        store.close()
    store = Store(path)
    try:
        original = store.execute(OWNER, 'confirm', {'action': 'confirm', 'proposal_id': identifier}, NOW)
        task = store._db.execute('SELECT * FROM tasks WHERE owner=?', (OWNER,)).fetchone()
        assert task['remind_at'] == START and task['duration_minutes'] == 45
        notices = queued(store)
        assert [row['due_at'] for row in notices] == ([] if mode == 'disabled' else [START if mode == 'missing' else START - 1800])
    finally:
        store.close()
    store = Store(path)
    try:
        assert_repeat_confirmation(store, identifier, original)
    finally:
        store.close()


@pytest.mark.parametrize('initial_mode', MODES)
@pytest.mark.parametrize('patch_mode', MODES)
def test_pending_proposal_time_change_preserves_missing_null_and_timestamp_intent(tmp_path, initial_mode, patch_mode):
    path = tmp_path / 'synthetic-reschedule.sqlite3'
    store = Store(path)
    try:
        identifier = create_proposal(store, 'create', mode=initial_mode)
        change = {'action': 'reschedule_proposal', 'proposal_id': identifier, 'remind_at': CHANGED,
                  **notice_fields(patch_mode, CHANGED - 900)}
        store.execute(OWNER, 'move', change, NOW)
    finally:
        store.close()
    store = Store(path)
    try:
        original = store.execute(OWNER, 'confirm', {'action': 'confirm', 'proposal_id': identifier}, NOW)
        task = store._db.execute('SELECT * FROM tasks WHERE owner=?', (OWNER,)).fetchone()
        assert task['remind_at'] == CHANGED
        if patch_mode == 'missing':
            expected = None if initial_mode == 'disabled' else CHANGED if initial_mode == 'missing' else START - 1800
        else:
            expected = None if patch_mode == 'disabled' else CHANGED - 900
        assert [row['due_at'] for row in queued(store)] == ([] if expected is None else [expected])
    finally:
        store.close()
    store = Store(path)
    try:
        assert_repeat_confirmation(store, identifier, original)
        assert store._db.execute('SELECT count(*) FROM tasks WHERE owner=?', (OWNER,)).fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize('initial_mode', MODES)
@pytest.mark.parametrize('change_mode', MODES)
def test_confirmed_task_change_keeps_one_task_obsoletes_old_notice_and_replays(tmp_path, initial_mode, change_mode):
    path = tmp_path / 'synthetic-change.sqlite3'
    store = Store(path)
    try:
        original_id = create_proposal(store, 'create', mode=initial_mode)
        store.execute(OWNER, 'initial-confirm', {'action': 'confirm', 'proposal_id': original_id}, NOW)
        task_id = store._db.execute('SELECT id FROM tasks WHERE owner=?', (OWNER,)).fetchone()[0]
        old_notices = queued(store)
        change_id = create_proposal(store, 'change', mode=change_mode, start=CHANGED,
                                    action='propose_change', task_id=task_id)
        assert queued(store) == old_notices  # Pending discussion never withdraws the old valid reminder.
    finally:
        store.close()
    store = Store(path)
    try:
        original = store.execute(OWNER, 'confirm', {'action': 'confirm', 'proposal_id': change_id}, NOW)
        tasks = [dict(row) for row in store._db.execute('SELECT * FROM tasks WHERE owner=?', (OWNER,))]
        assert len(tasks) == 1 and tasks[0]['id'] == task_id
        assert tasks[0]['remind_at'] == CHANGED and tasks[0]['revision'] == 2
        assert [row['due_at'] for row in queued(store)] == ([] if change_mode == 'disabled' else [CHANGED if change_mode == 'missing' else CHANGED - 1800])
        for old in old_notices:
            assert store._db.execute('SELECT status FROM notifications WHERE id=?', (old['id'],)).fetchone()[0] == 'obsolete'
    finally:
        store.close()
    store = Store(path)
    try:
        assert_repeat_confirmation(store, change_id, original)
    finally:
        store.close()


def test_one_registered_arrangement_worker_and_repeat_cleanup_leave_no_live_worker(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
        contexts = [callback for callback in app.cleanup_ctx if callback.__name__ == 'arrangement_lifecycle']
        assert len(contexts) == 1
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()  # Real app startup, no listening port and no external provider.
            await asyncio.sleep(0)
            worker = app[ARRANGEMENT_WORKER]
            alive = [task for task in asyncio.all_tasks() if not task.done()
                     and '.arrangement_lifecycle.' in task.get_coro().__qualname__]
            assert alive == [worker]
            assert not worker.done()
        finally:
            await runner.cleanup()
        assert worker.done()
        await runner.cleanup()  # Cleanup may be called again after shutdown; it must not revive a worker.
        assert worker.done()
        assert not [task for task in asyncio.all_tasks() if not task.done()
                    and '.arrangement_lifecycle.' in task.get_coro().__qualname__]
    asyncio.run(run())
