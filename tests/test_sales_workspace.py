"""Synthetic G4/G5/G6/G7 workspace regressions; no formal customer data."""
import json
import re
import sqlite3
from datetime import datetime

import pytest

from secretary.crm import analysis_fingerprint
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = 1_800_000_000.0


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "sales-workspace-synthetic.sqlite3"
    crm = CustomerStore(path)
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer("owner", {"name": "星河医院", "aliases": ["星河"], "stage": "negotiation", "amount_cents": 9000000}, NOW)
    person = crm.create_contact("owner", customer["id"], {"name": "王总", "role": "技术"}, NOW)
    yield crm, sales, customer, person, path
    crm.close()


def record(crm, customer_id, title="发送方案", *, owner="owner", status="following", kind="action", created=NOW):
    return crm.create_record(owner, {"title": title, "content": title, "customer_id": customer_id,
                                    "status": status, "kind": kind}, created)


def project(sales, customer_id, name="数据库加密", **kwargs):
    return sales.create_opportunity("owner", customer_id, {"name": name, **kwargs})


def propose(crm, note, *, when=NOW+3600, confirmed=True):
    reply = crm.execute("owner", "schedule:"+str(note["id"]), {"action": "propose", "title": note["title"], "remind_at": when}, NOW)
    identifier = int(re.search(r"P([0-9]+)", reply)[1])
    crm.link_proposal("owner", note["id"], identifier, NOW)
    if confirmed:
        crm.execute("owner", "confirm:"+str(identifier), {"action": "confirm", "proposal_id": identifier}, NOW)
    return crm.get_proposal("owner", identifier)


def test_two_projects_keep_money_stage_contacts_and_action_links_separate(context):
    crm, sales, customer, person, _ = context
    other = crm.create_contact("owner", customer["id"], {"name": "李经理", "role": "采购"}, NOW)
    first = project(sales, customer["id"], stage="qualified", amount_cents=1000000, amount_type="estimate", contact_ids=[person["id"]])
    second = project(sales, customer["id"], "密钥管理", stage="proposal", amount_cents=3000000, amount_type="quote", approval="unconfirmed", contact_ids=[other["id"]])
    note1, note2 = record(crm, customer["id"], "准备加密验证"), record(crm, customer["id"], "密钥方案报价")
    sales.link("owner", "record", note1["id"], first["id"])
    sales.link("owner", "record", note2["id"], second["id"])
    updated = sales.update_opportunity("owner", customer["id"], first["id"], {"expected_revision": 1, "stage": "negotiation", "amount_cents": 1200000})
    assert updated["revision"] == updated["version"] == 2
    assert sales.opportunities("owner", customer["id"])["items"][1] == second
    assert crm.get_customer("owner", customer["id"])["amount_cents"] == 9000000
    bench = sales.workbench("owner", customer["id"])
    assert bench["legacy_unassigned_amount"]["amount_cents"] == 9000000
    assert bench["legacy_unassigned_amount"]["amount_type"] == "unknown"
    assert bench["legacy_unassigned_stage"] == "negotiation"
    assert {link["entity_id"]: link["opportunity_id"] for link in bench["opportunity_links"]} == {note1["id"]: first["id"], note2["id"]: second["id"]}
    priorities = {item["record_id"]: item for item in sales.priorities("owner")["items"]}
    assert [item["contact_id"] for item in priorities[note1["id"]]["whom"]] == [person["id"]]
    assert [item["contact_id"] for item in priorities[note2["id"]]["whom"]] == [other["id"]]


def test_legacy_unknown_amount_is_not_copied_and_init_reopen_is_idempotent(context):
    crm, sales, customer, _, path = context
    opportunity = project(sales, customer["id"])
    assert opportunity["amount_cents"] is None
    assert opportunity["stage"] == "lead"
    reopened = CustomerStore(path)
    try:
        restored = SalesWorkspace(reopened, clock=lambda: NOW)
        SalesWorkspace(reopened, clock=lambda: NOW)
        assert restored.opportunities("owner", customer["id"])["items"] == [opportunity]
        assert reopened.get_customer("owner", customer["id"]) == crm.get_customer("owner", customer["id"])
        assert reopened.profile("owner", customer["id"])["contacts"] == crm.profile("owner", customer["id"])["contacts"]
    finally:
        reopened.close()


@pytest.mark.parametrize("fields", [
    {"amount_cents": True}, {"amount_cents": -1}, {"amount_cents": 1.5}, {"amount_type": "probability"},
    {"approval": "assumed"}, {"stage": "random"}, {"name": ""}, {"scope": "a"*4001},
    {"contact_ids": [True]}, {"archived": 1}, {"unexpected": "field"},
])
def test_invalid_opportunity_fields_are_rejected_before_write(context, fields):
    _, sales, customer, _, _ = context
    with pytest.raises(ValueError):
        project(sales, customer["id"], **fields) if "name" not in fields else sales.create_opportunity("owner", customer["id"], fields)
    assert sales.opportunities("owner", customer["id"])["total"] == 0


def test_foreign_customer_contact_link_and_archived_entities_are_rejected(context):
    crm, sales, customer, person, _ = context
    foreign = crm.create_customer("other", {"name": "私有公司"}, NOW)
    foreign_person = crm.create_contact("other", foreign["id"], {"name": "私有联系人"}, NOW)
    with pytest.raises(KeyError):
        sales.create_opportunity("owner", foreign["id"], {"name": "不可见"})
    for contact_id in (foreign_person["id"],):
        with pytest.raises(ValueError):
            project(sales, customer["id"], contact_ids=[contact_id])
    crm.update_contact("owner", customer["id"], person["id"], {"archived": True}, NOW)
    with pytest.raises(ValueError):
        project(sales, customer["id"], contact_ids=[person["id"]])
    opportunity = project(sales, customer["id"])
    foreign_note = record(crm, foreign["id"], owner="other")
    with pytest.raises(KeyError):
        sales.link("owner", "record", foreign_note["id"], opportunity["id"])
    another = crm.create_customer("owner", {"name": "另一家公司"}, NOW)
    another_note = record(crm, another["id"])
    with pytest.raises(KeyError):
        sales.link("owner", "record", another_note["id"], opportunity["id"])
    unassigned = record(crm, None)
    with pytest.raises(ValueError, match="归属"):
        sales.link("owner", "record", unassigned["id"], opportunity["id"])
    sales.update_opportunity("owner", customer["id"], opportunity["id"], {"expected_revision": 1, "archived": True})
    with pytest.raises(ValueError, match="归档"):
        sales.link("owner", "record", record(crm, customer["id"])["id"], opportunity["id"])
    assert sales.opportunities("owner", customer["id"])["total"] == 0
    assert sales.opportunities("owner", customer["id"], True)["total"] == 1


def test_second_connection_revision_conflict_preserves_first_saved_project(context):
    crm, sales, customer, _, path = context
    original = project(sales, customer["id"])
    other = CustomerStore(path)
    try:
        concurrent = SalesWorkspace(other, clock=lambda: NOW)
        concurrent.update_opportunity("owner", customer["id"], original["id"], {"expected_revision": 1, "scope": "已核实范围"})
        with pytest.raises(ValueError, match="更新"):
            sales.update_opportunity("owner", customer["id"], original["id"], {"expected_revision": 1, "scope": "旧范围"})
        assert sales.opportunities("owner", customer["id"])["items"][0]["scope"] == "已核实范围"
    finally:
        other.close()


def test_links_preserve_identity_history_and_flag_source_correction(context):
    crm, sales, customer, _, _ = context
    first, second = project(sales, customer["id"]), project(sales, customer["id"], "密钥管理")
    note = record(crm, customer["id"])
    linked = sales.link("owner", "record", note["id"], first["id"])
    assert sales.link("owner", "record", note["id"], first["id"]) == linked
    crm.update_record("owner", note["id"], {"content": "更正后的来源内容"}, NOW)
    assert sales.workbench("owner", customer["id"])["opportunity_links"][0]["stale"] is True
    replacement = sales.link("owner", "record", note["id"], second["id"])
    assert replacement["revision"] == 2
    cleared = sales.link("owner", "record", note["id"], None)
    assert cleared["opportunity_id"] is None and cleared["revision"] == 3
    bench = sales.workbench("owner", customer["id"])
    assert len(bench["opportunity_link_history"]) == 3
    assert bench["records"]["items"][0]["original_content"] == "发送方案"


def test_workbench_keeps_visit_material_review_and_correction_source_ids(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"], kind="note")
    opportunity = project(sales, customer["id"])
    with crm._transaction() as db:
        db.execute("CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,occurred_at REAL,revision INTEGER,created_at REAL,updated_at REAL)")
        db.execute("CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,record_id INTEGER,duplicate_of INTEGER,enqueue_key TEXT,created_at REAL,updated_at REAL)")
        db.execute("INSERT INTO crm_visits VALUES (11,?,?,?,?,?,?,?)", ("owner", customer["id"], "项目交流", NOW-86400, 1, NOW, NOW))
        db.execute("INSERT INTO crm_materials VALUES (21,?,?,?,?,?,?,?,?)", ("owner", customer["id"], "交流原材料", note["id"], None, "internal-id", NOW, NOW))
        db.execute("INSERT INTO crm_record_transcripts VALUES (?,?,?,?,?,?)", ("owner", note["id"], "原始识别", "核对后文字", NOW, NOW))
    sales.link("owner", "visit", 11, opportunity["id"])
    sales.link("owner", "material", 21, opportunity["id"])
    class Queue:
        def all_items(self, owner):
            return [{"key": "record:1:action:1", "customer_id": customer["id"], "source_record_id": note["id"]}, {"key": "foreign", "customer_id": -1}]
    bench = sales.workbench("owner", customer["id"], review_queue=Queue())
    assert bench["visits"]["items"][0]["id"] == 11
    assert bench["materials"]["items"][0]["record_id"] == note["id"]
    assert "enqueue_key" not in bench["materials"]["items"][0]
    assert bench["review_queue"]["total"] == 1
    assert bench["source_corrections"][0]["original_text"] == "原始识别"


def test_workbench_reports_truncated_records_but_keeps_all_open_actions(context):
    crm, sales, customer, _, _ = context
    for index in range(205):
        record(crm, customer["id"], f"行动{index}")
    bench = sales.workbench("owner", customer["id"])
    assert bench["records"]["total"] == 205
    assert bench["records"]["truncated"] is True
    assert len(bench["records"]["items"]) == 200
    assert len(bench["open_actions"]) == 205


def test_customer_candidates_explain_company_alias_contact_and_project_without_selection(context):
    crm, sales, customer, person, _ = context
    project(sales, customer["id"], "特定密钥项目")
    for text, kind in (("星河医院", "company"), ("星河的进展", "alias"), ("王总的意见", "contact"), ("特定密钥项目", "opportunity")):
        result = sales.customer_candidates("owner", text)
        assert result["status"] == "single"
        assert result["items"][0]["customer_id"] == customer["id"]
        assert kind in [reason["kind"] for reason in result["items"][0]["reasons"]]
        assert result["selected_customer_id"] is None and result["requires_confirmation"] is True
    other = crm.create_customer("owner", {"name": "另一个公司"}, NOW)
    crm.create_contact("owner", other["id"], {"name": person["name"]}, NOW)
    assert sales.customer_candidates("owner", "王总希望沟通")["status"] == "multiple"
    foreign = crm.create_customer("foreign", {"name": "私有公司"}, NOW)
    assert sales.customer_candidates("owner", foreign["name"])["items"] == []


def test_legacy_colliding_aliases_remain_multiple_candidates(context):
    crm, sales, customer, _, _ = context
    other = crm.create_customer("owner", {"name": "另一医院"}, NOW)
    with crm._transaction() as db:
        db.execute("UPDATE crm_customers SET aliases_json=? WHERE id=?", (json.dumps(["星河"]), other["id"]))
    candidates = sales.customer_candidates("owner", "星河那边的情况")
    assert candidates["status"] == "multiple"
    assert {item["customer_id"] for item in candidates["items"]} == {customer["id"], other["id"]}


def test_completion_finishes_existing_schedule_and_unknown_time_next_is_only_todo(context):
    crm, sales, customer, _, path = context
    note = record(crm, customer["id"])
    opportunity = project(sales, customer["id"])
    sales.link("owner", "record", note["id"], opportunity["id"])
    old = propose(crm, note)
    data = {"request_id": "finish-send", "result": "方案已发送，客户尚未反馈", "next_step": "核实客户是否收到", "expected_snapshot": analysis_fingerprint(note)}
    done = sales.complete_record("owner", note["id"], data)
    assert done["record"]["status"] == "done"
    assert crm.get_task("owner", old["task_id"])["status"] == "completed"
    assert done["next_record"]["status"] == "following"
    assert done["next_record"]["parent_record_id"] == note["id"]
    assert done["next_record"]["proposal_id"] is None and done["proposal"] is None
    assert sales.complete_record("owner", note["id"], data) == done
    assert sales.complete_record("owner", note["id"], {**data, "request_id": "another-click"}) == done
    bench = sales.workbench("owner", customer["id"])
    assert bench["outcomes"][0]["result"] == data["result"]
    assert bench["opportunity_links"][-1]["opportunity_id"] == opportunity["id"]
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert crm._db.execute("SELECT COUNT(*) FROM notifications WHERE status IN ('queued','leased')").fetchone()[0] == 0
    reopened = CustomerStore(path)
    try:
        replay = SalesWorkspace(reopened, clock=lambda: NOW).complete_record("owner", note["id"], data)
        assert replay == done
        assert reopened.list_records("owner")["total"] == 2
    finally:
        reopened.close()


def test_timed_next_step_requires_confirmation_and_completion_is_atomic(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    old = propose(crm, note)
    done = sales.complete_record("owner", note["id"], {"request_id": "timed-next", "result": "收到异议", "next_step": "准备技术澄清", "remind_at": NOW+7200, "duration_minutes": 60})
    assert done["proposal"]["status"] == "pending"
    assert done["proposal"]["duration_minutes"] == 60
    assert crm.get_task("owner", old["task_id"])["status"] == "completed"
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    crm.execute("owner", "explicit-next-confirm", {"action": "confirm", "proposal_id": done["proposal"]["id"]}, NOW)
    assert crm.record_detail("owner", done["next_record"]["id"])["task"]["duration_minutes"] == 60


def test_bad_completion_or_source_race_does_not_complete_original_action(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    old = propose(crm, note)
    for data in ({"request_id": "bad-time", "next_step": "继续", "remind_at": NOW-1}, {"request_id": "stale", "expected_snapshot": "0"*64}):
        with pytest.raises(ValueError):
            sales.complete_record("owner", note["id"], data)
        assert crm.get_task("owner", old["task_id"])["status"] == "pending"
        assert crm.get_record("owner", note["id"])["status"] == "following"
    with pytest.raises(KeyError):
        sales.complete_record("foreign", note["id"], {"request_id": "foreign"})


def test_completion_request_reuse_with_changed_payload_or_other_record_is_rejected(context):
    crm, sales, customer, _, _ = context
    first, second = record(crm, customer["id"]), record(crm, customer["id"], "另一行动")
    sales.complete_record("owner", first["id"], {"request_id": "repeat", "result": "完成"})
    for identifier, data in ((first["id"], {"request_id": "repeat", "result": "改变"}), (second["id"], {"request_id": "repeat", "result": "完成"})):
        with pytest.raises(ValueError):
            sales.complete_record("owner", identifier, data)
    assert crm.get_record("owner", second["id"])["status"] == "following"


def test_priority_reasons_cover_overdue_waiting_check_blocker_and_silence_without_tasks(context):
    crm, sales, customer, _, _ = context
    overdue = record(crm, customer["id"], "到期仍未发方案")
    scheduled = propose(crm, overdue, when=NOW+3600)
    with crm._transaction() as db:
        db.execute("UPDATE tasks SET remind_at=? WHERE id=?", (NOW-60, scheduled["task_id"]))
    waiting = record(crm, customer["id"], "等待采购反馈")
    crm.save_action_terms("owner", waiting["id"], {"executor_kind": "customer", "check_at": NOW-1}, NOW)
    project(sales, customer["id"], blockers="预算待审批")
    crm.update_customer("owner", customer["id"], {"contact_cycle_days": 7}, NOW)
    record(crm, customer["id"], "上次交流", kind="note", created=NOW-20*86400)
    priorities = sales.priorities("owner")["items"]
    assert priorities[0]["record_id"] == overdue["id"]
    assert any("等待客户" in " ".join(item["why"]) for item in priorities)
    assert any("检查时间" in " ".join(item["why"]) for item in priorities)
    assert any("阻力" in " ".join(item["why"]) for item in priorities)
    assert any("联系间隔" in " ".join(item["why"]) for item in priorities)
    for item in priorities:
        assert item["evidence"] and item["talk"] and item["progress"]
        assert item["whom"] or any("未明确联系人" in text for text in item["uncertainties"])
        assert "probability" not in item
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert sales.priorities("foreign")["items"] == []


def test_dismiss_and_defer_do_not_reappear_until_real_evidence_changes_or_reset(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    item = sales.priorities("owner")["items"][0]
    sales.decide_priority("owner", {"key": item["key"], "signature": item["signature"], "decision": "dismiss"})
    assert sales.priorities("owner")["items"] == []
    crm.update_record("owner", note["id"], {"category": "meeting"}, NOW+10)
    assert sales.priorities("owner")["items"] == []
    crm.update_record("owner", note["id"], {"content": "新反馈：需要部署说明"}, NOW)
    changed = sales.priorities("owner")["items"][0]
    assert changed["source_changed_after_decision"] is True
    with pytest.raises(ValueError, match="变化"):
        sales.decide_priority("owner", {"key": item["key"], "signature": item["signature"], "decision": "dismiss"})
    sales.decide_priority("owner", {"key": changed["key"], "signature": changed["signature"], "decision": "defer", "until_at": NOW+86400})
    assert sales.priorities("owner")["items"] == []
    sales.decide_priority("owner", {"key": changed["key"], "signature": changed["signature"], "decision": "reset"})
    assert len(sales.priorities("owner")["items"]) == 1
    sales.complete_record("owner", note["id"], {"request_id": "done"})
    assert sales.priorities("owner")["items"] == []


def test_only_completed_appointment_keeps_priority_until_follow_up_result_recorded(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    scheduled = propose(crm, note)
    crm.update_customer("owner", customer["id"], {"contact_cycle_days": 7}, NOW)
    record(crm, customer["id"], "历史交流", kind="note", created=NOW-20*86400)
    crm.execute("owner", "external-complete", {"action": "complete", "task_id": scheduled["task_id"]}, NOW)
    items = sales.priorities("owner")["items"]
    assert any(item.get('record_id') == note['id'] for item in items)
    sales.complete_record('owner',note['id'],{'request_id':'result-after-time-slot','result':'承诺已落实'})
    assert sales.priorities("owner")["items"] == []


def test_project_association_correction_is_not_implicitly_inherited_by_next_step(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    opportunity = project(sales, customer["id"])
    sales.link("owner", "record", note["id"], opportunity["id"])
    crm.update_record("owner", note["id"], {"content": "更正：这件事属于另一项目"}, NOW)
    done = sales.complete_record("owner", note["id"], {"request_id": "corrected-done", "next_step": "核对新项目范围"})
    assert crm._db.execute("SELECT COUNT(*) FROM crm_opportunity_links WHERE entity_id=? AND entity_type='record'", (done["next_record"]["id"],)).fetchone()[0] == 0


def test_completion_rolls_back_original_task_and_feedback_if_next_proposal_fails(context, monkeypatch):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"])
    scheduled = propose(crm, note)
    real_execute = crm._execute
    def fail_proposal(db, owner, command, now):
        return "未能生成" if command["action"] == "propose" else real_execute(db, owner, command, now)
    monkeypatch.setattr(crm, "_execute", fail_proposal)
    with pytest.raises(ValueError, match="提案"):
        sales.complete_record("owner", note["id"], {"request_id": "rollback", "result": "发送成功", "next_step": "下次核对", "remind_at": NOW+7200})
    assert crm.get_task("owner", scheduled["task_id"])["status"] == "pending"
    assert crm.get_record("owner", note["id"])["status"] == "following"
    assert crm.list_records("owner")["total"] == 1
    assert crm._db.execute("SELECT COUNT(*) FROM crm_action_outcomes").fetchone()[0] == 0
    assert crm._db.execute("SELECT COUNT(*) FROM crm_activities").fetchone()[0] == 0


def test_priority_decisions_survive_restart_and_expired_defer_becomes_visible(context):
    crm, sales, customer, _, path = context
    record(crm, customer["id"])
    item = sales.priorities("owner")["items"][0]
    sales.decide_priority("owner", {"key": item["key"], "signature": item["signature"], "decision": "defer", "until_at": NOW+3600})
    other = CustomerStore(path)
    try:
        assert SalesWorkspace(other, clock=lambda: NOW).priorities("owner")["items"] == []
        resumed = SalesWorkspace(other, clock=lambda: NOW+3601).priorities("owner")
        assert resumed["items"][0]["key"] == item["key"]
    finally:
        other.close()


def test_company_blocker_is_source_backed_and_does_not_copy_into_project(context):
    crm, sales, customer, _, _ = context
    crm.save_fact("owner", customer["id"], {"key": "blockers", "value": "观察：接口团队尚未到位", "basis": "observation"}, NOW)
    opportunity = project(sales, customer["id"])
    priorities = sales.priorities("owner")["items"]
    assert len(priorities) == 1 and "待核实" in priorities[0]["why"][0]
    assert priorities[0]["evidence"][0]["source_type"] == "customer_fact"
    assert opportunity["blockers"] == ""


def test_noop_project_save_does_not_revive_dismissed_priority(context):
    _, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"], blockers="采购节点待核实")
    item = sales.priorities("owner")["items"][0]
    sales.decide_priority("owner", {"key": item["key"], "signature": item["signature"], "decision": "dismiss"})
    saved = sales.update_opportunity("owner", customer["id"], opportunity["id"], {"expected_revision": 1, "blockers": opportunity["blockers"]})
    assert saved["revision"] == 1
    assert sales.priorities("owner")["items"] == []


def test_missing_revision_unknown_entity_duplicate_contacts_and_invalid_priority_decisions(context):
    crm, sales, customer, person, _ = context
    opportunity = project(sales, customer["id"])
    for values in ({"scope": "新范围"}, {"expected_revision": True}, {"expected_revision": 0}):
        with pytest.raises(ValueError):
            sales.update_opportunity("owner", customer["id"], opportunity["id"], values)
    with pytest.raises(ValueError):
        project(sales, customer["id"], contact_ids=[person["id"], person["id"]])
    note = record(crm, customer["id"])
    with pytest.raises(ValueError):
        sales.link("owner", "task", note["id"], opportunity["id"])
    with pytest.raises(ValueError):
        sales.complete_record("owner", note["id"], {"request_id": "title-only", "next_title": "缺少内容"})
    item = sales.priorities("owner")["items"][0]
    for fields in ({"decision": "defer"}, {"decision": "defer", "until_at": NOW-1}, {"decision": "confirm"}):
        with pytest.raises(ValueError):
            sales.decide_priority("owner", {"key": item["key"], "signature": item["signature"], **fields})
    with pytest.raises(KeyError):
        sales.decide_priority("foreign", {"key": item["key"], "signature": item["signature"], "decision": "dismiss"})


def test_schema_rollout_on_synthetic_database_copy_preserves_original_old_values(tmp_path):
    original = CustomerStore(tmp_path / "old-synthetic.sqlite3")
    customer = original.create_customer("owner", {"name": "旧测试客户", "amount_cents": 8880000, "stage": "proposal", "contact": "赵经理"}, NOW)
    original.save_fact("owner", customer["id"], {"key": "requirements", "value": "旧需求保持完整", "basis": "reported"}, NOW)
    before = original.profile("owner", customer["id"])
    copy_path = tmp_path / "schema-rehearsal-copy.sqlite3"
    with sqlite3.connect(copy_path) as target:
        original._db.backup(target)
    copy = CustomerStore(copy_path)
    try:
        sales = SalesWorkspace(copy, clock=lambda: NOW)
        SalesWorkspace(copy, clock=lambda: NOW)
        project(sales, customer["id"])
        assert copy.profile("owner", customer["id"]) == before
        assert original.profile("owner", customer["id"]) == before
        assert not original._db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_opportunities'").fetchone()
        assert sales.workbench("owner", customer["id"])["legacy_unassigned_amount"]["amount_cents"] == 8880000
    finally:
        copy.close()
        original.close()


def test_date_only_deadline_and_check_priorities_keep_clock_unknown_and_make_no_tasks(context):
    crm, sales, customer, _, _ = context
    note = record(crm, customer["id"], "等待结果")
    today = datetime.fromtimestamp(NOW, SHANGHAI).date().isoformat()
    crm.save_action_terms("owner", note["id"], {"executor_kind": "customer", "deadline_date": today, "check_date": today}, NOW)
    item = sales.priorities("owner")["items"][0]
    assert "具体时刻尚未明确" in " ".join(item["why"])
    assert item["evidence"][0]["terms"].get("execution_at") is None
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_visit_source_revision_change_flags_project_link_for_review_without_renaming_visit(context):
    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"])
    with crm._transaction() as db:
        db.execute("CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,occurred_at REAL,revision INTEGER,created_at REAL,updated_at REAL)")
        db.execute("CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,revision INTEGER,duplicate_of INTEGER,created_at REAL,updated_at REAL)")
        db.execute("CREATE TABLE crm_visit_sources(owner TEXT,visit_id INTEGER,material_id INTEGER,role TEXT)")
        db.execute("INSERT INTO crm_visits VALUES (1,?,?,?,?,?,?,?)", ("owner", customer["id"], "项目交流", NOW, 1, NOW, NOW))
        db.execute("INSERT INTO crm_materials VALUES (1,?,?,?,?,?,?,?)", ("owner", customer["id"], "录音", 1, None, NOW, NOW))
        db.execute("INSERT INTO crm_visit_sources VALUES ('owner',1,1,'recording')")
    sales.link("owner", "visit", 1, opportunity["id"])
    assert sales.workbench("owner", customer["id"])["opportunity_links"][0]["stale"] is False
    with crm._transaction() as db:
        db.execute("UPDATE crm_materials SET revision=2 WHERE owner='owner' AND id=1")
    assert sales.workbench("owner", customer["id"])["opportunity_links"][0]["stale"] is True


def derived_action(crm, customer_id, parent_id, title="采纳后行动"):
    return crm.create_record("owner", {"title": title, "content": title, "customer_id": customer_id,
        "kind": "action", "status": "following", "parent_record_id": parent_id}, NOW)


def test_inherit_confirmed_record_project_is_idempotent_and_does_not_schedule(context):
    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"])
    parent = record(crm, customer["id"], "现场原话", kind="note")
    child = derived_action(crm, customer["id"], parent["id"])
    assert sales.inherit_record_link("owner", parent["id"], child["id"]) == {"linked": False}
    sales.link("owner", "record", parent["id"], opportunity["id"])
    expected = {"linked": True, "opportunity_id": opportunity["id"]}
    assert sales.inherit_record_link("owner", parent["id"], child["id"]) == expected
    assert sales.inherit_record_link("owner", parent["id"], child["id"]) == expected
    assert crm._db.execute("SELECT COUNT(*) FROM crm_opportunity_link_history WHERE entity_id=? AND entity_type='record'",
                           (child["id"],)).fetchone()[0] == 1
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_actual_analysis_adoption_flows_into_personal_and_project_overview(context):
    from secretary.overview import OverviewService

    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"], "签名项目")
    parent = record(crm, customer["id"], "我回来整理签名验证方案", kind="note")
    sales.link("owner", "record", parent["id"], opportunity["id"])
    crm.save_analysis("owner", parent["id"], {
        "summary": "下一步由我准备方案，时间待补充。",
        "input_fingerprint": analysis_fingerprint(parent),
        "actions": [{"kind": "commitment", "title": "准备签名验证方案", "reason": "现场明确的下一步",
                     "owner_hint": "我", "evidence": "我回来整理签名验证方案"}],
    }, NOW)
    child = crm.adopt_action("owner", parent["id"], 1, NOW)
    assert sales.inherit_record_link("owner", parent["id"], child["id"])["linked"] is True
    overview = OverviewService(crm, sales, clock=lambda: NOW).get("owner")
    assert overview["counts"]["my_actions"] == 1
    assert overview["my_actions"][0]["opportunity_id"] == opportunity["id"]
    assert overview["projects"][0]["open_actions"] == 1
    assert overview["projects"][0]["needs_time"] == 1
    assert overview["projects"][0]["next_schedule"] is None
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["content", "customer", "archived"])
def test_invalidated_source_project_is_not_inherited(context, change):
    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"])
    parent = record(crm, customer["id"], "现场原话", kind="note")
    child = derived_action(crm, customer["id"], parent["id"])
    sales.link("owner", "record", parent["id"], opportunity["id"])
    if change == "content":
        crm.update_record("owner", parent["id"], {"content": "新的原话内容"}, NOW+1)
    elif change == "customer":
        another = crm.create_customer("owner", {"name": "另一个客户"}, NOW)
        crm.update_record("owner", parent["id"], {"customer_id": another["id"]}, NOW+1)
    else:
        sales.update_opportunity("owner", customer["id"], opportunity["id"], {"expected_revision": 1, "archived": True})
    result = sales.inherit_record_link("owner", parent["id"], child["id"])
    assert result["linked"] is False and "核对" in result["warning"]
    assert not crm._db.execute("SELECT 1 FROM crm_opportunity_links WHERE entity_type='record' AND entity_id=?", (child["id"],)).fetchone()


@pytest.mark.parametrize("existing", ["another", "unassigned", "stale"])
def test_existing_child_project_decision_is_never_overwritten(context, existing):
    crm, sales, customer, _, _ = context
    first, second = project(sales, customer["id"]), project(sales, customer["id"], "密钥托管")
    parent = record(crm, customer["id"], "现场原话", kind="note")
    child = derived_action(crm, customer["id"], parent["id"])
    sales.link("owner", "record", parent["id"], first["id"])
    previous = sales.link("owner", "record", child["id"], None if existing == "unassigned" else second["id"])
    if existing == "stale":
        crm.update_record("owner", child["id"], {"content": "更正的待办依据"}, NOW+1)
    result = sales.inherit_record_link("owner", parent["id"], child["id"])
    after = dict(crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner='owner' AND entity_type='record' AND entity_id=?", (child["id"],)).fetchone())
    assert result["linked"] is False and "保留" in result["warning"]
    assert after["opportunity_id"] == previous["opportunity_id"]
    assert after["revision"] == previous["revision"]
    assert after["source_snapshot"] == previous["source_snapshot"]


def test_inherit_project_rejects_foreign_and_unrelated_child_records(context):
    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"])
    parent = record(crm, customer["id"], "现场原话", kind="note")
    sales.link("owner", "record", parent["id"], opportunity["id"])
    unrelated = record(crm, customer["id"], "非来源行动")
    assert sales.inherit_record_link("owner", parent["id"], unrelated["id"])["linked"] is False
    foreign = crm.create_customer("other", {"name": "外部客户"}, NOW)
    foreign_record = record(crm, foreign["id"], owner="other")
    with pytest.raises(KeyError):
        sales.inherit_record_link("owner", parent["id"], foreign_record["id"])
    with pytest.raises(KeyError):
        sales.inherit_record_link("other", parent["id"], foreign_record["id"])


@pytest.mark.parametrize("entity_type", ["material", "visit"])
def test_material_visit_explicit_project_can_flow_to_verified_adoption(context, entity_type):
    crm, sales, customer, _, _ = context
    opportunity = project(sales, customer["id"])
    source = record(crm, customer["id"], "录音归档", kind="note")
    child = derived_action(crm, customer["id"], source["id"])
    with crm._transaction() as db:
        db.execute("CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,record_id INTEGER,revision INTEGER)")
        db.execute("CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,revision INTEGER)")
        db.execute("CREATE TABLE crm_visit_sources(owner TEXT,visit_id INTEGER,material_id INTEGER,role TEXT)")
        db.execute("CREATE TABLE crm_visit_adoptions(owner TEXT,visit_id INTEGER,record_id INTEGER)")
        db.execute("INSERT INTO crm_materials VALUES (1,?,?,?,?,?)", ("owner", customer["id"], "完整录音", source["id"], 1))
        db.execute("INSERT INTO crm_visits VALUES (2,?,?,?,1)", ("owner", customer["id"], "现场交流"))
        db.execute("INSERT INTO crm_visit_sources VALUES ('owner',2,1,'recording')")
        db.execute("INSERT INTO crm_visit_adoptions VALUES ('owner',2,?)", (child["id"],))
    identifier = 1 if entity_type == "material" else 2
    sales.link("owner", entity_type, identifier, opportunity["id"])
    assert sales.inherit_record_link("owner", identifier, child["id"], entity_type=entity_type) == {
        "linked": True, "opportunity_id": opportunity["id"]}
    another_child = derived_action(crm, customer["id"], source["id"], "另一采纳行动")
    with crm._transaction() as db:
        db.execute("UPDATE crm_materials SET revision=2 WHERE id=1")
        db.execute("INSERT INTO crm_visit_adoptions VALUES ('owner',2,?)", (another_child["id"],))
    assert sales.inherit_record_link("owner", identifier, another_child["id"], entity_type=entity_type)["linked"] is False
