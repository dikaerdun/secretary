"""Execution time and its optional reminder survive proposal persistence."""
from secretary.store import Store

NOW = 1791172800.0
START = NOW + 86400


def propose(store, request, **extra):
    store.execute('owner', request, {'action': 'propose', 'title': '虚构电话准备', 'remind_at': START, **extra}, NOW)
    return store._db.execute("SELECT max(id) FROM proposals WHERE owner='owner'").fetchone()[0]


def test_explicit_none_survives_reopen_and_creates_only_task(tmp_path):
    path = tmp_path/'notice.sqlite3'
    store = Store(path)
    identifier = propose(store, 'first', execution_notice_at=None)
    store.close()
    store = Store(path)
    message = store.execute('owner', 'confirm', {'action':'confirm', 'proposal_id':identifier}, NOW)
    assert '未启用提前提醒' in message
    assert store._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert store._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    store.execute('owner', 'another-confirm', {'action':'confirm', 'proposal_id':identifier}, NOW)
    assert store._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    store.close()


def test_missing_keeps_legacy_and_explicit_timestamp_is_independent(tmp_path):
    store = Store(tmp_path/'notice.sqlite3')
    identifier = propose(store, 'legacy')
    store.execute('owner', 'confirm', {'action':'confirm', 'proposal_id':identifier}, NOW)
    assert store._db.execute('SELECT due_at FROM notifications').fetchone()[0] == START
    store.execute('owner', 'cancel', {'action':'cancel', 'task_id':1}, NOW)
    identifier = propose(store, 'specific', execution_notice_at=START-3600)
    store.execute('owner', 'specific-confirm', {'action':'confirm', 'proposal_id':identifier}, NOW)
    task = store._db.execute('SELECT * FROM tasks WHERE status=\'pending\'').fetchone()
    notice = store._db.execute('SELECT * FROM notifications WHERE task_id=?', (task['id'],)).fetchone()
    assert task['remind_at'] == START and notice['due_at'] == START-3600
    store.close()


def test_proposal_time_change_preserves_explicit_disabled_notice(tmp_path):
    store = Store(tmp_path/'notice.sqlite3')
    identifier = propose(store, 'first', execution_notice_at=None)
    store.execute('owner', 'change', {'action':'reschedule_proposal', 'proposal_id':identifier, 'remind_at':START+3600}, NOW)
    store.execute('owner', 'confirm', {'action':'confirm', 'proposal_id':identifier}, NOW)
    assert store._db.execute('SELECT remind_at FROM tasks').fetchone()[0] == START+3600
    assert store._db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    store.close()
