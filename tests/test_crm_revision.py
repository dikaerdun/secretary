"""Regression coverage for customer identity, independent drafts and action queues."""

from datetime import datetime
import sqlite3

import pytest

from secretary.crm import analysis_fingerprint
from secretary.customer_store import CustomerStore
from secretary.store import SHANGHAI


NOW = datetime(2026, 9, 30, 10, tzinfo=SHANGHAI).timestamp()


@pytest.fixture
def crm(tmp_path):
    result = CustomerStore(tmp_path / "revision.sqlite3")
    yield result
    result.close()


def account(crm, name="星海医院", owner="alice", **fields):
    return crm.create_customer(owner, {"name": name, **fields}, NOW)


def note(crm, title="现场交流", owner="alice", **fields):
    return crm.create_record(owner, {"title": title, "content": title, **fields}, NOW)


def analyze(crm, record, **overrides):
    data = {"summary": "客户需要演示方案", "key_points": [], "open_questions": [],
            "actions": [{"title": "演示方案", "kind": "commitment", "remind_at": NOW + 3600}],
            "input_fingerprint": analysis_fingerprint(record), **overrides}
    return crm.save_analysis("alice", record["id"], data, NOW)


def propose(crm, record, when=None, *, confirm=False, owner="alice", task_id=None, title=None):
    command = {"action": "propose_change" if task_id else "propose", "title": title or record["title"], "remind_at": when}
    if task_id:
        command["task_id"] = task_id
    source = "proposal:" + str(crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0])
    crm.execute(owner, source, command, NOW - 86400)
    pid = crm._db.execute("SELECT MAX(id) FROM proposals").fetchone()[0]
    crm.link_proposal(owner, record["id"], pid, NOW)
    if confirm:
        crm.execute(owner, "confirm:" + str(pid), {"action": "confirm", "proposal_id": pid}, NOW - 86400)
    return crm.get_proposal(owner, pid)


def draft_payload(customer, key="crypto_needs", value="存储加密"):
    return {"intent": "update", "customer_id": customer["id"], "customer_name": customer["name"],
            "source_text": customer["name"] + "需要" + value,
            "attributes": [{"target": "account", "key": key, "value": value, "basis": "reported", "evidence": value}]}


def test_unknown_amount_explicit_zero_and_pipeline(crm):
    unknown = account(crm)
    assert unknown["amount_cents"] is None and unknown["amount_known"] is False
    zero = account(crm, "零额商机", amount_cents=0)
    assert zero["amount_cents"] == 0 and zero["amount_known"] is True
    positive = account(crm, "明确商机", amount_cents=12345)
    account(crm, "已赢单", amount_cents=99999, stage="won")
    stats = crm.dashboard("alice", NOW)["stats"]
    assert stats["pipeline_cents"] == 12345 and stats["unknown_amounts"] == 1
    edited = crm.update_customer("alice", positive["id"], {"amount_cents": None}, NOW)
    assert edited["amount_cents"] is None and edited["amount_known"] is False
    assert crm.dashboard("alice", NOW)["stats"]["unknown_amounts"] == 2
    assert crm.update_customer("alice", unknown["id"], {"amount_cents": 2}, NOW)["amount_known"]


def test_amount_migration_preserves_old_zero_as_known(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE crm_customers(id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,name TEXT NOT NULL,"
                   "contact TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',stage TEXT NOT NULL DEFAULT 'lead',"
                   "amount_cents INTEGER NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,"
                   "updated_at REAL NOT NULL,UNIQUE(owner,id))")
        db.execute("INSERT INTO crm_customers(owner,name,created_at,updated_at) VALUES ('alice','旧客户',0,0)")
    for _ in range(2):
        store = CustomerStore(path)
        try:
            old = store.get_customer("alice", 1)
            assert old["amount_known"] is True and old["amount_cents"] == 0
            assert old["aliases"] == [] and old["contact_cycle_days"] is None
        finally:
            store.close()


def test_alias_matching_owner_isolation_and_collisions(crm):
    c = account(crm, aliases=[" 星海 ", "XH"], contact_cycle_days=14)
    assert c["aliases"] == ["星海", "XH"] and c["contact_cycle_days"] == 14
    assert crm.find_customers_exact("alice", " xh ")[0]["id"] == c["id"]
    assert crm.list_customers("alice", q="星海")["total"] == 1
    assert crm.find_customers_exact("bob", "XH") == []
    account(crm, "XH", owner="bob")
    other = account(crm, "其他客户")
    for data in ({"aliases": ["xh"]}, {"name": "星海"}):
        with pytest.raises(ValueError, match="重复"):
            crm.update_customer("alice", other["id"], data, NOW)
    with pytest.raises(ValueError, match="重复"):
        account(crm, "第三客户", aliases=["A", "a"])
    with pytest.raises(ValueError):
        crm.update_customer("alice", c["id"], {"contact_cycle_days": True}, NOW)
    assert crm.update_customer("alice", c["id"], {"contact_cycle_days": None}, NOW)["contact_cycle_days"] is None
    payload = draft_payload(c)
    payload.update(customer_name="XH", source_text="XH需要存储加密")
    d = crm.create_customer_draft("alice", payload, NOW)
    assert crm.confirm_customer_draft("alice", d["id"], NOW)["customer_id"] == c["id"]


def test_primary_contact_archival_retains_facts_and_prevents_wrong_links(crm):
    c = account(crm, contact="王总", phone="12345")
    first = crm.profile("alice", c["id"])["contacts"][0]
    second = crm.create_contact("alice", c["id"], {"name": "李工", "phone": "67890"}, NOW)
    field = crm.save_fact("alice", c["id"], {"contact_id": second["id"], "key": "communication_channel",
                                           "value": "微信", "basis": "reported"}, NOW)
    selected = crm.update_customer("alice", c["id"], {"primary_contact_id": second["id"]}, NOW)
    assert selected["primary_contact_name"] == "李工"
    crm.update_contact("alice", c["id"], second["id"], {"archived": True}, NOW)
    archived = crm.profile("alice", c["id"])["contacts"][1]
    assert archived["archived"] is True and archived["fields"][0]["id"] == field["id"]
    assert crm.get_customer("alice", c["id"])["primary_contact_id"] is None
    assert crm.find_contacts_exact("alice", "李工") == []
    assert crm.list_customers("alice", q="67890")["total"] == 0
    for contact_id in (second["id"], 99999):
        with pytest.raises(ValueError):
            crm.update_customer("alice", c["id"], {"primary_contact_id": contact_id}, NOW)
    other = account(crm, "另一家公司")
    foreign = crm.create_contact("alice", other["id"], {"name": "张经理"}, NOW)
    with pytest.raises(ValueError):
        crm.update_customer("alice", c["id"], {"primary_contact_id": foreign["id"]}, NOW)
    replacement = crm.create_contact("alice", c["id"], {"name": "李工"}, NOW)
    with pytest.raises(ValueError, match="同名"):
        crm.update_contact("alice", c["id"], second["id"], {"archived": False}, NOW)
    crm.update_contact("alice", c["id"], first["id"], {"archived": True}, NOW)
    crm.update_contact("alice", c["id"], replacement["id"], {"archived": True}, NOW)
    assert crm.get_customer("alice", c["id"])["primary_contact_name"] == ""
    assert crm.list_customers("alice", q="王总")["total"] == 0


def test_independent_profile_drafts_confirm_without_overwriting_each_other(crm):
    c = account(crm)
    a = crm.create_customer_draft("alice", draft_payload(c), NOW)
    b = crm.create_customer_draft("alice", draft_payload(c, "industry", "医疗"), NOW)
    crm.confirm_customer_draft("alice", a["id"], NOW)
    crm.confirm_customer_draft("alice", b["id"], NOW)
    assert {field["key"] for field in crm.profile("alice", c["id"])["fields"]} == {"crypto_needs", "industry"}


@pytest.mark.parametrize("basis,value", [("reported", "传输加密"), ("observation", "存储加密")])
def test_same_profile_field_change_conflicts_even_when_value_matches(crm, basis, value):
    c = account(crm)
    d = crm.create_customer_draft("alice", draft_payload(c), NOW)
    crm.save_fact("alice", c["id"], {"key": "crypto_needs", "value": value, "basis": basis}, NOW)
    with pytest.raises(ValueError, match="过期"):
        crm.confirm_customer_draft("alice", d["id"], NOW)
    assert crm.get_customer_draft("alice", d["id"])["status"] == "stale"
    assert crm.profile("alice", c["id"])["fields"][0]["basis"] == basis


def test_noop_basic_field_is_not_replayed_over_manual_changes(crm):
    c = account(crm, stage="lead")
    payload = draft_payload(c)
    payload.update(source_text="星海医院目前是线索，需要存储加密", basic={"stage": "lead"}, basic_evidence={"stage": "线索"})
    d = crm.create_customer_draft("alice", payload, NOW)
    crm.update_customer("alice", c["id"], {"stage": "qualified"}, NOW)
    crm.confirm_customer_draft("alice", d["id"], NOW)
    assert crm.get_customer("alice", c["id"])["stage"] == "qualified"


@pytest.mark.parametrize("change", [{"content": "后来纠正原文"}, {"title": "后来纠正标题"}])
def test_direct_draft_source_changes_always_invalidate(crm, change):
    c = account(crm)
    payload = draft_payload(c)
    source = crm.capture_message("alice", "source", payload["source_text"], "voice", NOW)
    d = crm.create_customer_draft("alice", payload, NOW, source_record_id=source["id"])
    crm.update_record("alice", source["id"], change, NOW)
    with pytest.raises(ValueError, match="过期"):
        crm.confirm_customer_draft("alice", d["id"], NOW)


@pytest.mark.parametrize('change', [{'status':'done'}, {'category':'visit_review'}])
def test_workflow_metadata_does_not_invalidate_source_facts(crm, change):
    c = account(crm)
    payload = draft_payload(c)
    source = crm.capture_message('alice', 'source-metadata', payload['source_text'], 'voice', NOW)
    d = crm.create_customer_draft('alice', payload, NOW, source_record_id=source['id'])
    crm.update_record('alice', source['id'], change, NOW)
    crm.confirm_customer_draft('alice', d['id'], NOW)
    assert crm.get_customer_draft('alice', d['id'])['status'] == 'confirmed'


def test_record_kinds_parent_link_and_owner_validation(crm):
    c = account(crm)
    parent = note(crm, customer_id=c["id"])
    child = note(crm, title="第二次拜访", parent_record_id=parent["id"])
    assert child["kind"] == "note" and child["customer_id"] == c["id"]
    assert child["parent_record_id"] == parent["id"]
    with pytest.raises(KeyError):
        note(crm, owner="bob", parent_record_id=parent["id"])
    other = account(crm, "另一客户")
    with pytest.raises(ValueError):
        note(crm, customer_id=other["id"], parent_record_id=parent["id"])
    saved = analyze(crm, parent)
    action = crm.adopt_action("alice", parent["id"], saved["actions"][0]["id"], NOW)
    assert action["kind"] == "action" and action["parent_record_id"] == parent["id"]
    assert crm.list_records("alice", kind="note")["total"] == 2
    assert crm.list_records("alice", kind="action")["total"] == 1
    assert [r["id"] for r in crm.profile("alice", c["id"])["brief"]["open_records"]] == [action["id"]]


def test_dashboard_queues_enrich_confirmed_agenda_only(crm):
    c = account(crm, contact="王总")
    untimed = note(crm, "等客户补充预算", customer_id=c["id"], kind="action")
    proposed = note(crm, "提交材料", customer_id=c["id"], kind="action")
    propose(crm, proposed, NOW + 7200)
    overdue = note(crm, "上午拜访", customer_id=c["id"], kind="action")
    propose(crm, overdue, NOW - 3600, confirm=True)
    upcoming = note(crm, "下午电话", customer_id=c["id"], kind="action")
    p = propose(crm, upcoming, NOW + 14400, confirm=True)
    note(crm, "普通纪要", customer_id=c["id"])
    result = crm.dashboard("alice", NOW)
    assert result["queues"]["needs_time"]["items"][0]["id"] == untimed["id"]
    assert result["queues"]["pending_schedule"]["items"][0]["id"] == proposed["id"]
    assert result["queues"]["overdue"]["items"][0]["record_id"] == overdue["id"]
    assert result["queues"]["today"]["total"] == 2
    task = result["upcoming"][0]
    assert (task["record_id"], task["customer_id"], task["customer_name"], task["contact_hint"]) == (
        upcoming["id"], c["id"], c["name"], "王总")
    agenda = crm.agenda("alice", now=NOW)["items"]
    assert len(agenda) == 2 and p["task_id"] in {item["id"] for item in agenda}
    assert all(item["record_id"] != proposed["id"] for item in agenda)
    assert crm.dashboard("bob", NOW)["queues"]["today"]["total"] == 0


def test_completing_record_rejects_all_pending_proposal_history(crm):
    r = note(crm, kind="action")
    a, b = propose(crm, r, NOW + 3600), propose(crm, r, NOW + 7200)
    crm.update_record("alice", r["id"], {"status": "done"}, NOW)
    assert crm.get_proposal("alice", a["id"])["status"] == "rejected"
    assert crm.get_proposal("alice", b["id"])["status"] == "rejected"
    crm.execute("alice", "after-done", {"action": "confirm", "proposal_id": a["id"]}, NOW)
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_pending_change_keeps_active_task_and_different_proposed_title(crm):
    c = account(crm)
    r = note(crm, "拜访", customer_id=c["id"], kind="action")
    original = propose(crm, r, NOW + 3600, confirm=True)
    change = propose(crm, r, NOW + 7200, task_id=original["task_id"], title="携方案再次拜访")
    detail = crm.record_detail("alice", r["id"])
    assert detail["task"]["id"] == original["task_id"]
    assert detail["record"]["task_id"] == original["task_id"]
    assert detail["record"]["proposal_remind_at"] == NOW + 7200
    assert detail["record"]["remind_at"] == NOW + 3600
    assert crm.list_records("alice", queue="needs_time")["total"] == 0
    crm.update_record("alice", r["id"], {"title": "手动补记标题"}, NOW)
    assert crm.get_proposal("alice", change["id"])["title"] == "携方案再次拜访"
    assert crm.profile("alice", c["id"])["brief"]["next_tasks"][0]["id"] == original["task_id"]
    assert crm.agenda("alice", now=NOW)["items"][0]["record_id"] == r["id"]
    wrong = note(crm, title="携方案再次拜访")
    with pytest.raises(ValueError, match="不属于"):
        crm.link_proposal("alice", wrong["id"], change["id"], NOW)


def test_reanalysis_withdraws_pending_schedules_and_preserves_history(crm):
    parent = note(crm)
    first = analyze(crm, parent)
    old = crm.adopt_action("alice", parent["id"], first["actions"][0]["id"], NOW)
    p = propose(crm, old, NOW + 3600)
    changed = crm.update_record("alice", parent["id"], {"content": "纠正为周五演示"}, NOW)
    second = analyze(crm, changed, summary="周五演示")
    assert second["version"] == 2 and second["actions"][0]["id"] == 7
    assert crm.get_proposal("alice", p["id"])["status"] == "rejected"
    historical = crm.get_record("alice", old["id"])
    assert historical["superseded_by_analysis_version"] == 2 and historical["status"] == "done"
    assert second["previous_actions"][0]["record_id"] == old["id"]
    assert crm.adopt_action("alice", parent["id"], 1, NOW)["id"] == old["id"]
    new = crm.adopt_action("alice", parent["id"], 7, NOW)
    assert new["id"] != old["id"] and new["kind"] == "action"
    crm.execute("alice", "confirm-old", {"action": "confirm", "proposal_id": p["id"]}, NOW)
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_unadopted_old_action_id_cannot_adopt_new_version(crm):
    parent = note(crm)
    analyze(crm, parent)
    analyze(crm, parent)
    with pytest.raises(KeyError):
        crm.adopt_action("alice", parent["id"], 1, NOW)
    assert crm.list_records("alice")["total"] == 1


def test_confirmed_reminder_prevents_reanalysis_without_rejecting_other_pending(crm):
    parent = note(crm)
    saved = analyze(crm, parent, actions=[{"title": "已确认拜访", "kind": "commitment"},
                                          {"title": "待确认演示", "kind": "commitment"}])
    a = crm.adopt_action("alice", parent["id"], saved["actions"][0]["id"], NOW)
    b = crm.adopt_action("alice", parent["id"], saved["actions"][1]["id"], NOW)
    p = propose(crm, a, NOW + 3600, confirm=True)
    pending = propose(crm, b, NOW + 7200)
    with pytest.raises(ValueError, match="活动提醒"):
        analyze(crm, parent)
    assert crm.get_proposal("alice", pending["id"])["status"] == "pending"
    assert crm.get_task("alice", p["task_id"])["status"] == "pending"
    assert crm.get_analysis("alice", parent["id"])["version"] == 1


def test_action_id_migration_is_incremental_and_retains_existing_adoption(tmp_path):
    path = tmp_path / "legacy-actions.sqlite3"
    store = CustomerStore(path)
    parent = note(store)
    for _ in range(3):
        saved = analyze(store, parent)
    child = store.adopt_action("alice", parent["id"], saved["actions"][0]["id"], NOW)
    with store._transaction() as db:
        db.execute("CREATE TABLE old_actions (owner TEXT NOT NULL,parent_record_id INTEGER NOT NULL,"
                   "action_id INTEGER NOT NULL CHECK(action_id BETWEEN 1 AND 6),child_record_id INTEGER NOT NULL,"
                   "created_at REAL NOT NULL,PRIMARY KEY(owner,parent_record_id,action_id))")
        db.execute("INSERT INTO old_actions SELECT owner,parent_record_id,1,child_record_id,created_at FROM crm_analysis_actions")
        db.execute("DROP TABLE crm_analysis_actions")
        db.execute("ALTER TABLE old_actions RENAME TO crm_analysis_actions")
    store.close()
    for _ in range(2):
        store = CustomerStore(path)
        try:
            action = store.get_analysis("alice", parent["id"])["actions"][0]
            assert action["id"] == 13 and action["adopted_record_id"] == child["id"]
            assert store.adopt_action("alice", parent["id"], 13, NOW)["id"] == child["id"]
        finally:
            store.close()
