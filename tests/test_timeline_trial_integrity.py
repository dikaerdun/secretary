"""New deep-trial regressions; all 'entered customer data' is synthetic.

No environment, network, app server or pre-existing database is used. Snapshot
comparisons guard every original persisted row, not only currently visible UI.
"""
import asyncio
import json
import re
import sqlite3

import pytest

from secretary.account_network import AccountNetwork
from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineConflict, TimelineService
from secretary.exchange_records import ExchangeRecords
from secretary.materials import MaterialService
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace
from secretary.visits import VisitService


NOW = 1_800_000_000.0


def snapshot(db, *, include_timeline=True):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    if not include_timeline:
        tables = [table for table in tables if not table.startswith("crm_timeline_")]
    return {table: [dict(row) for row in db.execute('SELECT * FROM "' + table + '" ORDER BY rowid')] for table in tables}


@pytest.fixture
def entered(tmp_path):
    path = tmp_path / "synthetic-user-entered.sqlite3"
    crm = CustomerStore(path)
    clock = [NOW]
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: clock[0])
    network = AccountNetwork(crm, workspace, clock=lambda: clock[0])
    materials = MaterialService(crm, lock, clock=lambda: clock[0])
    visits = VisitService(crm, materials, lock)
    exchange = ExchangeRecords(crm, visits, lambda: clock[0])
    discussions = DiscussionService(crm, workspace, lock, clock=lambda: clock[0])
    unit = network.create_unit("sales", {"name": "合成城市银行", "notes": "用户已录入的正式风格资料", "aliases": ["合成城行"], "amount_cents": 12345600})
    a = crm.create_contact("sales", unit["id"], {"name": "王工", "department": "技术部", "role": "架构负责人", "phone": "13800000001"}, NOW)
    b = crm.create_contact("sales", unit["id"], {"name": "王工", "department": "采购部", "role": "采购负责人", "phone": "13800000002"}, NOW)
    first = workspace.create_opportunity("sales", unit["id"], {"name": "合成数据加密项目", "amount_cents": 30000000, "contact_ids": [a["id"], b["id"]]})
    second = workspace.create_opportunity("sales", unit["id"], {"name": "合成密钥管理项目", "contact_ids": [a["id"], b["id"]]})
    source = crm.create_record("sales", {"title": "已经录入的复盘", "content": "王工说先提供测试材料\n我的观察：预算需再核实", "customer_id": unit["id"], "category": "visit_review"}, NOW - 7200)
    workspace.link("sales", "record", source["id"], first["id"])
    crm.save_fact("sales", unit["id"], {"contact_id": a["id"], "key": "professional_goals", "value": "降低密码项目交付风险", "basis": "reported", "evidence": "本人说明"}, NOW - 3600)
    visit = visits.create("sales", {"title": "已录入的交流", "customer_id": unit["id"], "occurred_at": NOW - 86400})
    material = visits.add_material("sales", visit["id"], {"role": "recording", "provider": "manual", "title": "已录入的会议录音", "text": "王工说明先验证兼容"})["material"]
    foreign = network.create_unit("other", {"name": "他人的单位"})
    foreign_record = crm.create_record("other", {"title": "他人的记录", "content": "另一owner的原文", "customer_id": foreign["id"]}, NOW - 1)
    # The timeline upgrade starts only after these original rows already exist.
    yield {"path": path, "crm": crm, "clock": clock, "workspace": workspace, "network": network,
           "materials": materials, "visits": visits, "exchange": exchange, "discussions": discussions,
           "unit": unit, "a": a, "b": b, "first": first, "second": second, "source": source,
           "visit": visit, "material": material, "foreign": foreign, "foreign_record": foreign_record}
    crm.close()


def service(w):
    return TimelineService(w["crm"], w["workspace"], visits=w["visits"], discussions=w["discussions"], clock=lambda: w["clock"][0])


def confirm(t, key, **values):
    event = t.get_event("sales", key)
    return t.save_context("sales", key, {"expected_revision": event["revision"], **values})


def capture(w, t, request="entered", *, person="a", project="first", **values):
    scope = {"contact_id": w[person]["id"]}
    if project:
        scope["opportunity_id"] = w[project]["id"]
    data = {"request_id": request, "text": "合成已保存的现场原话", "kind": "communication", "occurred_at": NOW - 20, **values}
    return t.create_record("sales", scope, data), scope, data


def action(w, project="first"):
    row = w["crm"].create_record("sales", {"title": "已录入待办", "content": "提供测试资料", "customer_id": w["unit"]["id"], "kind": "action", "status": "following"}, NOW)
    if project:
        w["workspace"].link("sales", "record", row["id"], w[project]["id"])
    return row


def test_upgrade_and_reopen_preserve_every_existing_row_and_identifier(entered):
    w = entered
    before = snapshot(w["crm"]._db, include_timeline=False)
    t = service(w)
    assert snapshot(w["crm"]._db, include_timeline=False) == before
    for _ in range(3):
        t = service(w)
        t.view("sales", {"customer_id": w["unit"]["id"]})
        t.history_context("sales", {"customer_id": w["unit"]["id"]})
        t.get_event("sales", "record:" + str(w["source"]["id"]))
    assert snapshot(w["crm"]._db, include_timeline=False) == before
    reopened = CustomerStore(w["path"])
    try:
        old_rows = snapshot(reopened._db, include_timeline=False)
        workspace = SalesWorkspace(reopened, clock=lambda: NOW)
        TimelineService(reopened, workspace, clock=lambda: NOW).view("sales", {"customer_id": w["unit"]["id"]})
        assert snapshot(reopened._db, include_timeline=False) == old_rows == before
        assert reopened.get_record("sales", w["source"]["id"])["original_content"] == w["source"]["original_content"]
    finally:
        reopened.close()


def test_reading_formally_confirmed_context_does_not_rewrite_original_data(entered):
    w, t = entered, service(entered)
    saved, scope, _ = capture(w, t)
    before = snapshot(w["crm"]._db)
    for _ in range(3):
        t.get_event("sales", saved["event"]["key"])
        t.view("sales", scope)
        t.history_context("sales", scope, event_keys=[saved["event"]["key"]])
        t.view("other", {"customer_id": w["foreign"]["id"]})
    assert snapshot(w["crm"]._db) == before


@pytest.mark.parametrize("archived", ["person", "project", "membership"])
def test_saved_retry_remains_idempotent_after_scope_archival(entered, archived):
    w, t = entered, service(entered)
    saved, scope, data = capture(w, t)
    if archived == "person":
        w["crm"].update_contact("sales", w["unit"]["id"], w["a"]["id"], {"archived": True}, NOW + 1)
    elif archived == "project":
        current = w["workspace"].opportunities("sales", w["unit"]["id"])["items"][0]
        w["workspace"].update_opportunity("sales", w["unit"]["id"], current["id"], {"expected_revision": current["revision"], "archived": True})
    else:
        current = next(item for item in w["workspace"].opportunities("sales", w["unit"]["id"])["items"] if item["id"] == w["first"]["id"])
        w["workspace"].archive_stakeholder("sales", w["unit"]["id"], current["id"], w["a"]["id"], {"expected_revision": current["revision"], "archived": True})
    before = snapshot(w["crm"]._db)
    replay = t.create_record("sales", scope, data)
    assert replay["created"] is False and replay["record"]["id"] == saved["record"]["id"]
    assert snapshot(w["crm"]._db) == before
    with pytest.raises(ValueError):
        t.create_record("sales", scope, {**data, "request_id": "really-new"})
    assert snapshot(w["crm"]._db) == before


def test_historical_project_filter_survives_person_membership_removal(entered):
    w, t = entered, service(entered)
    saved, scope, _ = capture(w, t)
    current = next(item for item in w["workspace"].opportunities("sales", w["unit"]["id"])["items"] if item["id"] == w["first"]["id"])
    w["workspace"].archive_stakeholder("sales", w["unit"]["id"], current["id"], w["a"]["id"], {"expected_revision": current["revision"], "archived": True})
    before = snapshot(w["crm"]._db)
    view = t.view("sales", {"contact_id": w["a"]["id"]})
    assert w["first"]["id"] in {item["id"] for item in view["projects"]}
    assert saved["event"]["key"] in {item["key"] for item in t.view("sales", scope)["items"]}
    assert snapshot(w["crm"]._db) == before


@pytest.mark.parametrize("relation", ["about", "clear"])
def test_visit_explicit_person_correction_not_overwritten_by_old_source_people(entered, relation):
    w, t = entered, service(entered)
    key = "material:" + str(w["material"]["id"])
    confirm(t, key, contact_relations=[{"contact_id": w["a"]["id"], "relation": "direct"}])
    parent_key = "visit:" + str(w["visit"]["id"])
    people = [] if relation == "clear" else [{"contact_id": w["a"]["id"], "relation": "about"}]
    before_original = snapshot(w["crm"]._db, include_timeline=False)
    corrected = confirm(t, parent_key, contact_relations=people)
    if relation == "clear":
        assert corrected["contact_relations"] == []
        assert not any(item["key"] == parent_key for item in t.view("sales", {"contact_id": w["a"]["id"]})["items"])
    else:
        assert corrected["contact_relations"][0]["relation"] == "about"
        assert t.view("sales", {"contact_id": w["a"]["id"]})["summary"]["latest_communication"] is None
    assert snapshot(w["crm"]._db, include_timeline=False) == before_original


def test_fresh_visit_confirmation_not_poisoned_by_obsolete_child_context(entered):
    w, t = entered, service(entered)
    key = "material:" + str(w["material"]["id"])
    confirm(t, key, contact_relations=[{"contact_id": w["a"]["id"], "relation": "direct"}])
    material = w["materials"].list("sales")["items"][0]
    w["materials"].update("sales", material["id"], {"revision": material["revision"], "text": "王工表示现在先等采购确认"})
    parent_key = "visit:" + str(w["visit"]["id"])
    renewed = confirm(t, parent_key, contact_relations=[{"contact_id": w["b"]["id"], "relation": "about"}])
    assert not renewed["needs_review"]
    assert [(item["contact_id"], item["relation"]) for item in renewed["contact_relations"]] == [(w["b"]["id"], "about")]
    assert t.history_context("sales", {"contact_id": w["b"]["id"]})["events"][0]["key"] == parent_key


def test_linking_another_person_keeps_existing_action_people_and_original_rows(entered):
    w, t = entered, service(entered)
    row = action(w)
    t.link_action("sales", row["id"], {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]})
    before = snapshot(w["crm"]._db, include_timeline=False)
    t.link_action("sales", row["id"], {"contact_id": w["b"]["id"], "opportunity_id": w["first"]["id"]})
    people = t.get_event("sales", "record:" + str(row["id"]))["contact_relations"]
    assert {person["contact_id"] for person in people} == {w["a"]["id"], w["b"]["id"]}
    assert snapshot(w["crm"]._db, include_timeline=False) == before
    assert t.view("sales", {"contact_id": w["a"]["id"]})["summary"]["open_actions"][0]["id"] == row["id"]


def test_wrong_project_action_link_cannot_contaminate_another_project(entered):
    w, t = entered, service(entered)
    row = action(w, project="first")
    before = snapshot(w["crm"]._db)
    with pytest.raises(ValueError):
        t.link_action("sales", row["id"], {"contact_id": w["a"]["id"], "opportunity_id": w["second"]["id"]})
    assert snapshot(w["crm"]._db) == before


def test_completed_schedule_keeps_unresolved_action_and_marks_schedule_inactive(entered):
    w, t = entered, service(entered)
    row = action(w)
    t.link_action("sales", row["id"], {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]})
    reply = w["crm"].execute("sales", "schedule", {"action": "propose", "title": row["title"], "remind_at": NOW + 86400}, NOW)
    proposal_id = int(re.search(r"P(\d+)", reply)[1])
    w["crm"].link_proposal("sales", row["id"], proposal_id, NOW)
    w["crm"].execute("sales", "confirm", {"action": "confirm", "proposal_id": proposal_id}, NOW)
    task_id = w["crm"].get_proposal("sales", proposal_id)["task_id"]
    w["crm"].execute("sales", "complete", {"action": "complete", "task_id": task_id}, NOW + 1)
    before = snapshot(w["crm"]._db)
    item = t.view("sales", {"contact_id": w["a"]["id"]})["summary"]["open_actions"][0]
    assert item["id"] == row["id"] and item["status"] == row["status"] == "following"
    assert item["task_status"] == "completed" and item["active_schedule"] is False and item["remind_at"] is None
    assert t.history_context("sales", {"contact_id": w["a"]["id"]})["open_actions"][0]["id"] == row["id"]
    assert snapshot(w["crm"]._db) == before


@pytest.mark.parametrize("failure", ["request", "history", "project_link"])
def test_new_capture_failure_rolls_back_all_tables_including_outer_transaction(entered, failure):
    w, t = entered, service(entered)
    target = {"request": "crm_timeline_requests", "history": "crm_timeline_context_history", "project_link": "crm_opportunity_links"}[failure]
    with w["crm"]._transaction() as db:
        db.execute("CREATE TRIGGER trial_failure BEFORE INSERT ON " + target + " BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
    before = snapshot(w["crm"]._db)
    with pytest.raises(sqlite3.IntegrityError):
        with w["crm"]._transaction():
            capture(w, t, request="roll-back")
    assert snapshot(w["crm"]._db) == before


def test_failed_context_history_does_not_overwrite_formal_people_or_date(entered):
    w, t = entered, service(entered)
    saved, _, _ = capture(w, t)
    with w["crm"]._transaction() as db:
        db.execute("CREATE TRIGGER trial_history_failure BEFORE INSERT ON crm_timeline_context_history BEGIN SELECT RAISE(ABORT,'synthetic history failure'); END")
    before = snapshot(w["crm"]._db)
    with pytest.raises(sqlite3.IntegrityError):
        confirm(t, saved["event"]["key"], occurred_at=NOW - 1000,
                contact_relations=[{"contact_id": w["b"]["id"], "relation": "about"}])
    assert snapshot(w["crm"]._db) == before
    assert t.get_event("sales", saved["event"]["key"])["occurred_at"] == NOW - 20


def test_source_reassigned_to_other_unit_does_not_rebind_old_person_on_retry(entered):
    w, t = entered, service(entered)
    saved, scope, data = capture(w, t)
    other = w["network"].create_unit("sales", {"name": "合成另一客户"})
    w["crm"].update_record("sales", saved["record"]["id"], {"customer_id": other["id"]}, NOW + 10)
    before = snapshot(w["crm"]._db)
    replay = t.create_record("sales", scope, data)
    assert replay["record"]["customer_id"] == other["id"] and replay["event"]["needs_review"]
    assert t.history_context("sales", {"contact_id": w["a"]["id"]})["events"] == []
    assert snapshot(w["crm"]._db) == before


def test_separating_and_rejoining_material_changes_sidecar_only(entered):
    w, t = entered, service(entered)
    key, parent = "material:" + str(w["material"]["id"]), "visit:" + str(w["visit"]["id"])
    before = snapshot(w["crm"]._db, include_timeline=False)
    confirm(t, key, separate_event=True, kind="reflection", occurred_at=NOW - 10, related_event_key=parent,
            contact_relations=[{"contact_id": w["a"]["id"], "relation": "about"}])
    assert "merged_into" not in t.get_event("sales", key)
    confirm(t, key, separate_event=False)
    assert t.get_event("sales", key)["merged_into"] == parent
    assert snapshot(w["crm"]._db, include_timeline=False) == before


def test_hidden_source_blocks_retry_and_continuation_without_recreating_original(entered):
    w, t = entered, service(entered)
    saved, scope, data = capture(w, t)
    row = action(w)
    t.link_action("sales", row["id"], scope, source_event_key=saved["event"]["key"])
    outcome = w["workspace"].complete_record("sales", row["id"], {"request_id": "continue", "result": "测试材料已发", "next_step": "等采购确认"})
    with w["crm"]._transaction() as db:
        db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("sales", row["id"]))
        db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("sales", saved["record"]["id"]))
    before = snapshot(w["crm"]._db)
    with pytest.raises(TimelineConflict):
        t.create_record("sales", scope, data)
    assert t.view("sales", {"contact_id": w["a"]["id"]})["summary"]["open_actions"] == []
    next_row = w["crm"].get_record("sales", outcome["next_record"]["id"])
    assert next_row["content"] == "等采购确认"
    assert snapshot(w["crm"]._db) == before


def test_long_recording_context_preserves_tail_and_marks_excerpt_without_altering_source(entered):
    w, t = entered, service(entered)
    text = "开场明确先兼容旧密码设备。" + "合成会议中段讨论。" * 4000 + "结尾重要决定：项目已经暂停，请不要继续发方案。"
    material = w["materials"].enqueue("sales", {"provider": "manual", "title": "合成长会议", "text": text, "customer_id": w["unit"]["id"], "category": "conversation"})
    confirm(t, "material:" + str(material["id"]), contact_relations=[{"contact_id": w["a"]["id"], "relation": "direct"}])
    before = snapshot(w["crm"]._db)
    event = t.get_event("sales", "material:" + str(material["id"]))
    assert event["text_truncated"] is True and event["text_length"] == len(text)
    assert len(event["text"]) <= 20000 and event["text"].startswith("开场明确")
    assert "结尾重要决定：项目已经暂停" in event["text"] and "省略中间部分" in event["text"]
    history = t.history_context("sales", {"contact_id": w["a"]["id"]})["events"][0]
    assert history["text_truncated"] and history["text_length"] == len(text)
    assert "不要继续发方案" in history["text"]
    version = w["crm"]._db.execute("SELECT text FROM crm_material_versions WHERE owner=? AND material_id=?", ("sales", material["id"])).fetchone()
    assert version["text"] == text and snapshot(w["crm"]._db) == before


def test_archived_project_history_carries_exact_names_and_archive_state(entered):
    w, t = entered, service(entered)
    saved, scope, _ = capture(w, t)
    first = next(item for item in w["workspace"].opportunities("sales", w["unit"]["id"])["items"] if item["id"] == w["first"]["id"])
    before = t.history_context("sales", scope)
    w["workspace"].update_opportunity("sales", w["unit"]["id"], first["id"], {"expected_revision": first["revision"], "archived": True})
    rows = snapshot(w["crm"]._db)
    after = t.history_context("sales", scope)
    event = next(item for item in after["events"] if item["key"] == saved["event"]["key"])
    assert event["customer_name"] == "合成城市银行" and event["opportunity_name"] == "合成数据加密项目"
    assert event["opportunity_archived"] is True and after["fingerprint"] != before["fingerprint"]
    assert snapshot(w["crm"]._db) == rows


def test_archived_project_action_stays_readable_with_explicit_project_identity(entered):
    w, t = entered, service(entered)
    row = action(w)
    t.link_action("sales", row["id"], {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]})
    first = next(item for item in w["workspace"].opportunities("sales", w["unit"]["id"])["items"] if item["id"] == w["first"]["id"])
    w["workspace"].update_opportunity("sales", w["unit"]["id"], first["id"], {"expected_revision": first["revision"], "archived": True})
    before = snapshot(w["crm"]._db)
    old = t.history_context("sales", {"contact_id": w["a"]["id"]})["open_actions"][0]
    assert old["opportunity_id"] == first["id"] and old["opportunity_archived"]
    assert old["opportunity_name"] == first["name"] and old["customer_name"] == w["unit"]["name"]
    assert old["entity_type"] == "record" and old["entity_id"] == row["id"]
    assert t.view("sales", {"contact_id": w["a"]["id"], "opportunity_id": w["second"]["id"]})["summary"]["open_actions"] == []
    assert snapshot(w["crm"]._db) == before


@pytest.mark.parametrize("executor", ["self", "customer", "team"])
def test_action_person_history_carries_check_deadline_and_executor_without_calendar(entered, executor):
    w, t = entered, service(entered)
    row = action(w)
    terms = {"executor_kind": executor, "executor_evidence": "人工明确负责人", "check_date": "2026-10-04", "check_evidence": "2026-10-04检查反馈",
             "deadline_date": "2026-10-06", "deadline_evidence": "2026-10-06前交付", "execution_at": None}
    w["crm"].save_action_terms("sales", row["id"], terms, NOW, explicit=True)
    t.link_action("sales", row["id"], {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]})
    before = snapshot(w["crm"]._db)
    history = t.history_context("sales", {"contact_id": w["a"]["id"]})
    item = history["open_actions"][0]
    assert item["content"] == row["content"] and item["executor_kind"] == executor
    assert item["action_terms"] == terms and item["active_schedule"] is False
    assert bool(history["waiting"]) == (executor in ("customer", "team"))
    assert snapshot(w["crm"]._db) == before
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_pending_proposal_confirmation_and_cancel_are_distinct_from_actual_schedule(entered):
    w, t = entered, service(entered)
    row = action(w)
    scope = {"contact_id": w["a"]["id"]}
    t.link_action("sales", row["id"], scope)
    reply = w["crm"].execute("sales", "pending", {"action": "propose", "title": row["title"], "remind_at": NOW + 1000}, NOW)
    proposal_id = int(re.search(r"P(\d+)", reply)[1])
    w["crm"].link_proposal("sales", row["id"], proposal_id, NOW)
    pending = t.view("sales", scope)["summary"]["open_actions"][0]
    assert pending["active_schedule"] is False and pending["remind_at"] is None
    assert pending["proposed_remind_at"] == NOW + 1000 and pending["proposal_status"] == "pending"
    w["crm"].execute("sales", "confirmed", {"action": "confirm", "proposal_id": proposal_id}, NOW)
    confirmed = t.view("sales", scope)["summary"]["open_actions"][0]
    assert confirmed["active_schedule"] and confirmed["remind_at"] == NOW + 1000
    w["crm"].execute("sales", "cancelled", {"action": "cancel", "task_id": confirmed["task_id"]}, NOW + 1)
    before = snapshot(w["crm"]._db)
    cancelled = t.view("sales", scope)["summary"]["open_actions"][0]
    assert cancelled["active_schedule"] is False and cancelled["remind_at"] is None
    assert cancelled["task_status"] == "cancelled" and cancelled["proposed_remind_at"] is None
    assert snapshot(w["crm"]._db) == before


def test_stale_action_cannot_be_implicitly_reconfirmed_by_linking_another_person(entered):
    w, t = entered, service(entered)
    row = action(w)
    t.link_action("sales", row["id"], {"contact_id": w["a"]["id"]})
    w["crm"].update_record("sales", row["id"], {"content": "原事项已经改成另一个沟通目标"}, NOW + 1)
    before = snapshot(w["crm"]._db)
    with pytest.raises(TimelineConflict):
        t.link_action("sales", row["id"], {"contact_id": w["b"]["id"]})
    assert snapshot(w["crm"]._db) == before
    assert t.get_event("sales", "record:" + str(row["id"]))["needs_review"]


def test_parent_person_confirmation_does_not_bypass_actual_source_unit_mismatch(entered):
    w, t = entered, service(entered)
    other = w["network"].create_unit("sales", {"name": "独立客户"})
    material = w["materials"].list("sales")["items"][0]
    w["materials"].update("sales", material["id"], {"revision": material["revision"], "customer_id": other["id"]})
    before = snapshot(w["crm"]._db, include_timeline=False)
    parent = confirm(t, "visit:" + str(w["visit"]["id"]), contact_relations=[{"contact_id": w["a"]["id"], "relation": "direct"}])
    assert parent["needs_review"]
    assert t.history_context("sales", {"contact_id": w["a"]["id"]})["events"] == []
    assert snapshot(w["crm"]._db, include_timeline=False) == before


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_pending_reschedule_retains_original_schedule_until_confirm_or_reject(entered, decision):
    w, t = entered, service(entered)
    row = action(w)
    scope = {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]}
    t.link_action("sales", row["id"], scope)
    old_time, new_time = NOW + 3600, NOW + 86400
    reply = w["crm"].execute("sales", "initial", {"action": "propose", "title": row["title"], "remind_at": old_time}, NOW)
    old_id = int(re.search(r"P(\d+)", reply)[1])
    w["crm"].link_proposal("sales", row["id"], old_id, NOW)
    w["crm"].execute("sales", "initial-confirm", {"action": "confirm", "proposal_id": old_id}, NOW)
    task_id = w["crm"].get_proposal("sales", old_id)["task_id"]
    original_proposal = dict(w["crm"]._db.execute("SELECT * FROM proposals WHERE id=?", (old_id,)).fetchone())
    original_task = dict(w["crm"]._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    reply = w["crm"].execute("sales", "change", {"action": "propose_change", "task_id": task_id,
                                                "title": row["title"], "remind_at": new_time}, NOW + 1)
    change_id = int(re.search(r"P(\d+)", reply)[1])
    w["crm"].link_proposal("sales", row["id"], change_id, NOW + 1)
    before = snapshot(w["crm"]._db)
    pending = t.view("sales", scope)["summary"]["open_actions"][0]
    assert pending["proposal_id"] == change_id and pending["proposal_status"] == "pending"
    assert pending["active_schedule"] and pending["task_status"] == "pending"
    assert pending["task_id"] == task_id and pending["remind_at"] == old_time
    assert pending["proposed_remind_at"] == new_time and not pending["needs_review"]
    assert dict(w["crm"]._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == original_task
    assert snapshot(w["crm"]._db) == before

    w["crm"].execute("sales", "change-" + decision, {"action": decision, "proposal_id": change_id}, NOW + 2)
    before = snapshot(w["crm"]._db)
    actual = t.history_context("sales", scope)["open_actions"][0]
    assert actual["active_schedule"] and actual["task_status"] == "pending" and actual["task_id"] == task_id
    assert actual["remind_at"] == (new_time if decision == "confirm" else old_time)
    assert actual["proposed_remind_at"] is None
    assert actual["proposal_status"] == ("confirmed" if decision == "confirm" else "rejected")
    assert actual["status"] == row["status"] == "following" and not actual["needs_review"]
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM tasks WHERE owner='sales'").fetchone()[0] == 1
    assert dict(w["crm"]._db.execute("SELECT * FROM proposals WHERE id=?", (old_id,)).fetchone()) == original_proposal
    if decision == "reject":
        assert dict(w["crm"]._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == original_task
    assert snapshot(w["crm"]._db) == before


def test_cross_unit_project_people_options_are_explicit_active_and_owner_scoped(entered):
    w, t = entered, service(entered)
    group = w["network"].create_unit("sales", {"name": "合成集团总部"}, {"unit_type": "group"})
    w["network"].update("sales", w["unit"]["id"], {"expected_revision": 1, "parent_customer_id": group["id"]})
    procurement = w["crm"].create_contact("sales", group["id"], {"name": "王工", "department": "集团采购部"}, NOW)
    archived = w["crm"].create_contact("sales", group["id"], {"name": "已归档总部人"}, NOW)
    sibling = w["network"].create_unit("sales", {"name": "兄弟子公司"}, {"parent_customer_id": group["id"]})
    unjoined = w["crm"].create_contact("sales", sibling["id"], {"name": "同树但未参与"}, NOW)
    removed = w["crm"].create_contact("sales", sibling["id"], {"name": "已移出项目人"}, NOW)
    foreign = w["crm"].create_contact("other", w["foreign"]["id"], {"name": "另一owner的王工"}, NOW)
    external = w["network"].create_unit("sales", {"name": "合成外部测评机构"})
    invalid = w["crm"].create_contact("sales", external["id"], {"name": "参与单位已移除"}, NOW)

    def project():
        return next(item for item in w["workspace"].opportunities("sales", w["unit"]["id"])["items"] if item["id"] == w["first"]["id"])

    for person in (procurement, archived, removed):
        w["workspace"].upsert_stakeholder("sales", w["unit"]["id"], w["first"]["id"],
                                          {"contact_id": person["id"], "expected_revision": project()["revision"], "roles": ["procurement"], "evidence": "明确参与本项目"})
    w["crm"].update_contact("sales", group["id"], archived["id"], {"archived": True}, NOW + 1)
    w["workspace"].archive_stakeholder("sales", w["unit"]["id"], w["first"]["id"], removed["id"],
                                     {"expected_revision": project()["revision"], "archived": True})
    w["workspace"].upsert_project_unit("sales", w["unit"]["id"], w["first"]["id"],
                                      {"participant_customer_id": external["id"], "expected_revision": project()["revision"], "roles": ["technical"], "evidence": "明确外部测评职责"})
    w["workspace"].upsert_stakeholder("sales", w["unit"]["id"], w["first"]["id"],
                                    {"contact_id": invalid["id"], "expected_revision": project()["revision"], "roles": ["technical_reviewer"], "evidence": "明确项目测评人"})
    w["workspace"].archive_project_unit("sales", w["unit"]["id"], w["first"]["id"], external["id"],
                                      {"expected_revision": project()["revision"], "archived": True})

    scope = {"contact_id": procurement["id"], "opportunity_id": w["first"]["id"]}
    before = snapshot(w["crm"]._db)
    options = t.view("sales", scope)["contacts"]
    expected = {procurement["id"], w["a"]["id"], w["b"]["id"]}
    assert {person["id"] for person in options} == expected
    assert next(person for person in options if person["id"] == w["a"]["id"])["unit_name"] == w["unit"]["name"]
    assert not any("relation" in person or "selected" in person for person in options)
    assert {person["id"] for person in t.view("sales", {"contact_id": procurement["id"]})["contacts"]} == {procurement["id"]}
    assert snapshot(w["crm"]._db) == before

    created = t.create_record("sales", scope, {"request_id": "cross-choose", "text": "明确只与总部采购人谈项目", "kind": "communication"})
    key = created["event"]["key"]
    assert {person["id"] for person in t.get_event("sales", key)["contacts"]} == expected
    assert [rel["contact_id"] for rel in created["event"]["contact_relations"]] == [procurement["id"]]
    assert t.view("sales", {"contact_id": w["a"]["id"], "opportunity_id": w["first"]["id"]})["total"] == 0
    before = snapshot(w["crm"]._db, include_timeline=False)
    checked = confirm(t, key, contact_relations=[{"contact_id": procurement["id"], "relation": "direct"},
                                               {"contact_id": w["a"]["id"], "relation": "direct"},
                                               {"contact_id": w["b"]["id"], "relation": "about"}])
    assert {rel["contact_id"]: rel["relation"] for rel in checked["contact_relations"]} == {
        procurement["id"]: "direct", w["a"]["id"]: "direct", w["b"]["id"]: "about"}
    assert snapshot(w["crm"]._db, include_timeline=False) == before
    second = t.create_record("sales", scope, {"request_id": "cross-new", "text": "明确多人本次沟通", "kind": "communication",
                                              "contact_relations": [{"contact_id": w["a"]["id"], "relation": "direct"}, {"contact_id": w["b"]["id"], "relation": "about"}]})
    assert {rel["contact_id"] for rel in second["event"]["contact_relations"]} == expected
    for person in (unjoined, removed, invalid, archived, foreign):
        before = snapshot(w["crm"]._db)
        with pytest.raises((ValueError, KeyError)):
            t.create_record("sales", scope, {"request_id": "bad-person-" + str(person["id"]), "text": "不可误写的人物关系", "kind": "communication",
                                              "contact_relations": [{"contact_id": person["id"], "relation": "direct"}]})
        assert snapshot(w["crm"]._db) == before
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
