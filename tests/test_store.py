import sqlite3
import sys
from types import SimpleNamespace

import pytest

from secretary.store import Store


NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "secretary.sqlite3")
    yield result
    result.close()


def create(store, owner="alice", source="create-1", remind_at=NOW + 10):
    return store.execute(
        owner, source, {"action": "create", "title": "提交方案", "remind_at": remind_at}, NOW
    )


def test_repeated_source_returns_original_reply_without_another_task(store):
    original = create(store)
    assert store.get_result("alice", "create-1") == original
    assert store.get_result("bob", "create-1") is None
    assert store.execute("alice", "create-1", {"action": "cancel", "task_id": 1}, NOW) == original
    listing = store.execute("alice", "list-1", {"action": "list"}, NOW)
    assert "#1" in listing
    assert "#2" not in listing
    assert store.claim_due(NOW + 10) is not None
    assert store.claim_due(NOW + 10) is None


@pytest.mark.parametrize("action", ["complete", "cancel", "snooze"])
def test_owners_cannot_change_each_others_tasks(store, action):
    create(store)
    reply = store.execute(
        "bob", "change-1", {"action": action, "task_id": 1, "remind_at": NOW + 100}, NOW
    )
    assert "未找到" in reply
    assert "提交方案" not in store.execute("bob", "list-1", {"action": "list"}, NOW)
    notification = store.claim_due(NOW + 10)
    assert notification["owner"] == "alice"


def test_reopening_database_preserves_task_and_idempotency(tmp_path):
    path = tmp_path / "persist.sqlite3"
    first = Store(path)
    reply = create(first)
    first.close()
    second = Store(path)
    try:
        assert second.get_result("alice", "create-1") == reply
        assert second.claim_due(NOW + 9) is None
        notification = second.claim_due(NOW + 10)
        assert "#1" in notification["text"]
        assert "提交方案" in notification["text"]
        assert "北京时间" in notification["text"]
    finally:
        second.close()


def test_failed_delivery_retries_after_backoff_and_ack_stops_delivery(store):
    create(store)
    first = store.claim_due(NOW + 10)
    assert store.retry(first["id"], first["token"], NOW + 10)
    assert store.claim_due(NOW + 39) is None
    second = store.claim_due(NOW + 40)
    assert second["id"] == first["id"]
    assert second["token"] != first["token"]
    assert not store.ack(first["id"], first["token"], NOW + 40)
    assert not store.retry(first["id"], first["token"], NOW + 40)
    assert store.ack(second["id"], second["token"], NOW + 40)
    assert not store.retry(second["id"], second["token"], NOW + 41)
    assert store.claim_due(NOW + 100_000) is None


@pytest.mark.parametrize("action", ["complete", "cancel"])
def test_completing_or_cancelling_invalidates_claimed_reminder(store, action):
    create(store)
    notification = store.claim_due(NOW + 10)
    reply = store.execute("alice", "change-1", {"action": action, "task_id": 1}, NOW + 11)
    assert "#1" in reply
    assert not store.retry(notification["id"], notification["token"], NOW + 12)
    assert not store.ack(notification["id"], notification["token"], NOW + 12)
    assert store.claim_due(NOW + 100_000) is None
    assert "提交方案" not in store.execute("alice", "list-1", {"action": "list"}, NOW + 12)


def test_snooze_invalidates_old_claim_and_creates_one_new_reminder(store):
    create(store)
    old = store.claim_due(NOW + 10)
    store.execute(
        "alice", "snooze-1", {"action": "snooze", "task_id": 1, "remind_at": NOW + 200}, NOW + 11
    )
    assert not store.retry(old["id"], old["token"], NOW + 12)
    assert store.claim_due(NOW + 199) is None
    current = store.claim_due(NOW + 200)
    assert current["id"] != old["id"]
    assert store.ack(current["id"], current["token"], NOW + 200)
    assert store.claim_due(NOW + 100_000) is None


def test_expired_lease_recovers_after_process_restarts(tmp_path):
    path = tmp_path / "lease.sqlite3"
    first_store = Store(path)
    create(first_store)
    first = first_store.claim_due(NOW + 10)
    first_store.close()
    second_store = Store(path)
    try:
        assert second_store.claim_due(NOW + 129) is None
        second = second_store.claim_due(NOW + 130)
        assert second["id"] == first["id"]
        assert second["token"] != first["token"]
        assert not second_store.ack(first["id"], first["token"], NOW + 131)
        assert second_store.ack(second["id"], second["token"], NOW + 131)
    finally:
        second_store.close()


def test_missing_time_saves_an_unscheduled_task(store):
    reply = create(store, remind_at=None)
    assert "#1" in reply and "待安排" in reply
    assert store.claim_due(NOW + 100_000) is None
    store.execute(
        "alice", "schedule-1", {"action": "snooze", "task_id": 1, "remind_at": NOW + 20}, NOW
    )
    assert store.claim_due(NOW + 20) is not None


def test_same_source_is_independent_for_each_owner(store):
    create(store, owner="alice")
    reply = create(store, owner="bob")
    assert "#2" in reply
    assert store.claim_due(NOW + 10)["owner"] == "alice"
    assert store.claim_due(NOW + 10)["owner"] == "bob"


@pytest.mark.parametrize("remind_at", [float("nan"), float("inf"), "tomorrow", True, 1e30, 10**400])
def test_invalid_timestamp_is_not_saved(store, remind_at):
    reply = create(store, remind_at=remind_at)
    assert "时间" in reply
    assert "提交方案" not in store.execute("alice", "list-1", {"action": "list"}, NOW)


def test_oversized_task_id_does_not_crash_or_change_existing_tasks(store):
    create(store)
    reply = store.execute("alice", "oversized-id", {"action": "complete", "task_id": 10**30}, NOW)
    assert "任务编号" in reply
    assert store.claim_due(NOW + 10) is not None


def test_database_transactions_do_not_duplicate_task_between_store_instances(tmp_path):
    path = tmp_path / "two-instances.sqlite3"
    first = Store(path)
    second = Store(path)
    try:
        assert create(first) == create(second)
        one = first.claim_due(NOW + 10)
        assert second.claim_due(NOW + 10) is None
        assert second.ack(one["id"], one["token"], NOW + 10)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    finally:
        first.close()
        second.close()


def propose(store, source="proposal-1", owner="alice", **changes):
    command = {"action": "propose", "title": "准备产品方案", "remind_at": NOW + 3600}
    command.update(changes)
    return store.execute(owner, source, command, NOW)


def test_proposals_never_send_notifications_before_confirmation(store):
    reply = propose(store)
    assert "P1" in reply and "确认 P1" in reply and "取消提案 P1" in reply
    assert "估计" in reply and "30" in reply
    assert "准备产品方案" not in store.execute("alice", "list-before", {"action": "list"}, NOW)
    assert store.claim_due(NOW + 100_000) is None


def test_confirmation_creates_exactly_one_task_and_notification(store):
    original = propose(store)
    assert propose(store) == original
    reply = store.execute("alice", "confirm-1", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "#1" in reply and "启用" in reply
    again = store.execute("alice", "confirm-2", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "#1" in again
    assert "#2" not in store.execute("alice", "list-after", {"action": "list"}, NOW)
    assert store.claim_due(NOW + 3600) is not None
    assert store.claim_due(NOW + 3600) is None


def test_rejected_proposal_never_creates_a_reminder(store):
    propose(store)
    reply = store.execute("alice", "reject-1", {"action": "reject", "proposal_id": 1}, NOW)
    assert "已取消" in reply
    reply = store.execute("alice", "confirm-rejected", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "已取消" in reply
    assert store.claim_due(NOW + 100_000) is None


def test_missing_time_is_not_guessed_and_must_be_added_before_confirmation(store):
    reply = propose(store, remind_at=None)
    assert "待补时间" in reply and "改到" in reply
    failed = store.execute("alice", "confirm-missing", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "补充" in failed
    changed = store.execute(
        "alice", "proposal-time", {"action": "reschedule_proposal", "proposal_id": 1, "remind_at": NOW + 200}, NOW
    )
    assert "确认 P1" in changed
    assert store.claim_due(NOW + 200) is None
    store.execute("alice", "confirm-after-time", {"action": "confirm", "proposal_id": 1}, NOW)
    assert store.claim_due(NOW + 200) is not None


def test_explicit_conflicting_time_is_preserved_but_cannot_be_confirmed(store):
    create(store, remind_at=NOW + 3600)
    reply = propose(store, remind_at=NOW + 3600)
    assert "冲突" in reply and "#1" in reply
    failed = store.execute("alice", "confirm-conflict", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "冲突" in failed
    assert "准备产品方案" not in store.execute("alice", "list-conflict", {"action": "list"}, NOW)


def test_confirmation_rechecks_conflicts_created_after_proposal(store):
    propose(store)
    create(store, remind_at=NOW + 3600)
    failed = store.execute("alice", "confirm-late-conflict", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "冲突" in failed
    store.execute("alice", "complete-blocker", {"action": "complete", "task_id": 1}, NOW)
    success = store.execute("alice", "confirm-no-conflict", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "启用" in success


def test_pending_proposals_warn_but_do_not_block_the_first_confirmation(store):
    propose(store)
    reply = propose(store, source="proposal-2", title="另一事项")
    assert "重叠" in reply and "P1" in reply
    success = store.execute("alice", "confirm-first-draft", {"action": "confirm", "proposal_id": 2}, NOW)
    assert "启用" in success
    failed = store.execute("alice", "confirm-after-real-task", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "冲突" in failed


def test_adjacent_slots_do_not_conflict_and_duration_can_be_corrected(store):
    create(store, remind_at=NOW + 3600)
    propose(store, remind_at=NOW + 1800, duration_minutes=60)
    failed = store.execute("alice", "confirm-too-long", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "冲突" in failed
    changed = store.execute(
        "alice", "duration-fix", {"action": "reschedule_proposal", "proposal_id": 1, "duration_minutes": 30}, NOW
    )
    assert "确认 P1" in changed
    success = store.execute("alice", "confirm-shorter", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "启用" in success


def test_past_or_deadline_exceeding_proposals_cannot_be_confirmed(store):
    propose(store, remind_at=NOW - 1)
    failed = store.execute("alice", "confirm-past", {"action": "confirm", "proposal_id": 1}, NOW)
    assert "已过" in failed
    propose(store, source="deadline-proposal", deadline_at=NOW + 3700)
    failed = store.execute("alice", "confirm-deadline", {"action": "confirm", "proposal_id": 2}, NOW)
    assert "截止" in failed
    assert store.claim_due(NOW + 100_000) is None


@pytest.mark.parametrize("action", ["confirm", "reject", "reschedule_proposal"])
def test_proposals_are_owner_isolated(store, action):
    propose(store)
    reply = store.execute(
        "bob", "foreign-proposal", {"action": action, "proposal_id": 1, "remind_at": NOW + 7200}, NOW
    )
    assert "未找到" in reply
    assert "准备产品方案" not in store.execute("bob", "foreign-proposals", {"action": "proposals"}, NOW)
    assert "准备产品方案" in store.execute("alice", "own-proposals", {"action": "proposals"}, NOW)


def test_agenda_receives_confirmed_scheduled_tasks_including_completed_for_owner(store, monkeypatch):
    create(store, remind_at=NOW + 10)
    create(store, owner="bob", source="bob-task", remind_at=NOW + 20)
    create(store, source="unscheduled-task", remind_at=None)
    create(store, source="done-task", remind_at=NOW + 30)
    store.execute("alice", "complete-for-agenda", {"action": "complete", "task_id": 4}, NOW)
    create(store, source="cancelled-task", remind_at=NOW + 40)
    store.execute("alice", "cancel-for-agenda", {"action": "cancel", "task_id": 5}, NOW)
    propose(store)
    calls = []

    def render_agenda(tasks, period, now, page=1):
        calls.append((tasks, period, now, page))
        return "本周安排展示"

    monkeypatch.setitem(sys.modules, "secretary.agenda", SimpleNamespace(render_agenda=render_agenda))
    reply = store.execute("alice", "agenda-week", {"action": "agenda", "period": "week", "page": 2}, NOW)
    assert reply == "本周安排展示"
    tasks, period, now, page = calls[0]
    assert [task["id"] for task in tasks] == [1, 4]
    assert [task["status"] for task in tasks] == ["pending", "completed"]
    assert tasks[0]["duration_minutes"] == 30
    assert tasks[0]["deadline_at"] is None
    assert (period, now, page) == ("week", NOW, 2)


def test_existing_database_is_migrated_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, "
            "title TEXT NOT NULL, remind_at REAL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO tasks(owner, title, status, created_at, updated_at) VALUES ('alice', '旧事项', 'pending', ?, ?)",
            (NOW, NOW),
        )
    for _ in range(2):
        migrated = Store(path)
        migrated.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT duration_minutes, deadline_at FROM tasks").fetchone() == (30, None)


def test_pending_proposal_survives_restart_and_can_be_confirmed(tmp_path):
    path = tmp_path / "pending-proposal.sqlite3"
    first = Store(path)
    propose(first)
    first.close()
    second = Store(path)
    try:
        assert "P1" in second.execute("alice", "list-proposals", {"action": "proposals"}, NOW)
        second.execute("alice", "confirm-restarted", {"action": "confirm", "proposal_id": 1}, NOW)
        assert second.claim_due(NOW + 3600) is not None
    finally:
        second.close()


@pytest.mark.parametrize("duration", [0, False, 1.5, 10081])
def test_invalid_proposed_duration_is_not_silently_replaced_with_default(store, duration):
    reply = propose(store, duration_minutes=duration)
    assert "未保存" in reply
    assert "准备产品方案" not in store.execute("alice", "proposals-after-invalid", {"action": "proposals"}, NOW)


def test_proposals_enforce_title_bound_and_keep_a_full_page_below_twenty_kilobytes(store):
    invalid = propose(store, source="long-title", title="事" * 121)
    assert "120" in invalid
    for index in range(20):
        propose(store, source=f"bounded-title-{index}", title="事" * 120, deadline_at=NOW + 7200)
    reply = store.execute("alice", "full-proposals-page", {"action": "proposals"}, NOW)
    assert "共 20 项" in reply
    assert len(reply.encode("utf-8")) < 20_000
