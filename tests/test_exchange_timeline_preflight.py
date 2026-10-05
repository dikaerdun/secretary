"""Round01 scope versus optional timeline: synthetic source edits and journals."""
import asyncio
import json

import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.customer_timeline import TimelineService, TimelineConflict
from secretary.exchange_workspace import ExchangeWorkspace, ExchangeConflict

NOW = 1_800_000_000.0


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / "fresh-exchange-timeline-preflight.sqlite3")
    clock = [NOW]
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    profile = ProfileIntelligence(crm, sales, clock=lambda: clock[0])
    timeline = TimelineService(crm, sales, clock=lambda: clock[0])
    exchange = ExchangeWorkspace(crm, sales, profile, timeline=timeline, clock=lambda: clock[0])
    unit = crm.create_customer("owner", {"name": "合成归属单位"}, NOW)
    person = crm.create_contact("owner", unit["id"], {"name": "李岚", "department": "技术部"}, NOW)
    project = sales.create_opportunity("owner", unit["id"], {"name": "合成密码项目"})
    source = crm.create_record("owner", {"customer_id": unit["id"], "title": "先前明确核对的原文",
        "content": "我答应发送适配清单。", "category": "conversation", "kind": "note"}, NOW)
    sales.link("owner", "record", source["id"], project["id"])
    event = timeline.get_event("owner", "record:" + str(source["id"]))
    timeline.save_context("owner", event["key"], {"expected_revision": event["revision"],
        "contact_relations": [{"contact_id": person["id"], "relation": "direct"}]})
    yield crm, sales, profile, timeline, exchange, unit, person, project, source, clock
    profile.close()
    crm.close()


def prepare(services):
    return asyncio.run(services[4].prepare("owner", "record", services[8]["id"]))


def body(view):
    action = next(item for item in view["items"] if item["kind"] == "action")
    return {"request_id": "real-action-result", "expected_revision": view["revision"], "source_revision": view["source_revision"],
        "items": [{"id": action["id"], "expected_version": action["version"], "draft": {"title": "核对后发送适配清单"}}]}


def test_corrected_source_optional_old_people_does_not_fake_adoption_failure(services):
    crm, _, _, timeline, exchange, unit, _, _, source, clock = services
    clock[0] += 1
    crm.update_record("owner", source["id"], {"content": "我答应发送适配清单。先核实验收负责人。"}, clock[0])
    assert timeline.get_event("owner", "record:" + str(source["id"]))["needs_review"]
    view = prepare(services)
    assert view["source"]["opportunity_id"] is None and view["source"]["customer_id"] == unit["id"]
    request = body(view)
    result = exchange.confirm("owner", "record", source["id"], request)
    receipt = result["results"][0]
    assert receipt["status"] == "confirmed", receipt
    assert receipt["result"]["timeline_status"] == "needs_review"
    assert "已采用" in receipt["result"]["message"] and "核对" in receipt["result"]["message"]
    child = crm.get_record("owner", receipt["result"]["record_id"])
    assert child["title"] == "核对后发送适配清单" and child["customer_id"] == unit["id"]
    child_event = timeline.get_event("owner", "record:" + str(child["id"]))
    assert child_event["opportunity_id"] is None and child_event["contact_relations"] == []
    assert not child_event["needs_review"]
    assert exchange.confirm("owner", "record", source["id"], request) == result
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm.get_record("owner", source["id"])["original_content"] == source["content"]


def test_source_unit_changes_after_review_reject_before_any_adoption(services):
    crm, _, _, _, exchange, _, _, _, source, clock = services
    view = prepare(services)
    other = crm.create_customer("owner", {"name": "另一个合成单位"}, NOW)
    clock[0] += 1
    crm.update_record("owner", source["id"], {"customer_id": other["id"]}, clock[0])
    with pytest.raises(ExchangeConflict):
        exchange.confirm("owner", "record", source["id"], body(view))
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0


def test_project_archived_after_review_blocks_before_any_adoption(services):
    crm, sales, _, _, exchange, unit, _, project, source, _ = services
    view = prepare(services)
    sales.update_opportunity("owner", unit["id"], project["id"], {"archived": True, "expected_revision": project["revision"]})
    result = exchange.confirm("owner", "record", source["id"], body(view))
    assert result["results"][0]["status"] == "blocked", result
    assert "归档" in result["results"][0]["error"]
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_optional_timeline_conflict_after_adoption_returns_real_success_once(services, monkeypatch):
    crm, _, _, timeline, exchange, _, _, _, source, _ = services
    view = prepare(services)
    def changed_optional_history(*args, **kwargs):
        raise TimelineConflict("所选历程归属或依据已失效，请重新核对后讨论")
    monkeypatch.setattr(timeline, "link_action", changed_optional_history)
    request = body(view)
    result = exchange.confirm("owner", "record", source["id"], request)
    assert result["results"][0]["status"] == "confirmed", result
    assert result["results"][0]["result"]["timeline_status"] == "needs_review"
    assert crm.get_record("owner", result["results"][0]["result"]["record_id"])["title"] == "核对后发送适配清单"
    assert exchange.confirm("owner", "record", source["id"], request) == result
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1


def test_legacy_blocked_timeline_receipt_recovers_proven_adoption_without_rewriting(services, monkeypatch):
    crm, _, _, timeline, exchange, _, _, _, source, _ = services
    view = prepare(services)
    def late_conflict(*args, **kwargs):
        raise TimelineConflict("所选历程归属或依据已失效，请重新核对后讨论")
    monkeypatch.setattr(timeline, "link_action", late_conflict)
    request = body(view)
    result = exchange.confirm("owner", "record", source["id"], request)
    batch = crm._db.execute("SELECT * FROM crm_exchange_batches WHERE request_id=?", (request["request_id"],)).fetchone()
    entry = crm._db.execute("SELECT * FROM crm_exchange_batch_items WHERE batch_id=?", (batch["id"],)).fetchone()
    checkpoint = json.loads(entry["checkpoint_json"])
    child_id = checkpoint["record_id"]
    original_child = crm.get_record("owner", child_id)
    # Recreate the exact previous version's false blocked receipt over a real,
    # canonically linked adoption; do not invent an adopted-record foreign key.
    old_receipt = {"item_id": entry["item_id"], "status": "blocked", "error": "所选历程归属或依据已失效，请重新核对后讨论",
                   "result": {"record_id": child_id, "stage": "draft_edited"}}
    old_response = {**result, "status": "partial", "results": [old_receipt]}
    with crm._transaction() as db:
        db.execute("UPDATE crm_exchange_batch_items SET result_json=? WHERE owner=? AND batch_id=? AND item_id=?",
                   (json.dumps(old_receipt, ensure_ascii=False), "owner", batch["id"], entry["item_id"]))
        db.execute("UPDATE crm_exchange_batches SET status='partial',response_json=? WHERE owner=? AND id=?",
                   (json.dumps(old_response, ensure_ascii=False), "owner", batch["id"]))
    recovered = exchange.confirm("owner", "record", source["id"], request)
    receipt = recovered["results"][0]
    assert receipt["status"] == "already_confirmed", recovered
    assert receipt["result"]["record_id"] == child_id and receipt["result"]["timeline_status"] == "needs_review"
    assert receipt["previous_receipt"] == old_receipt
    assert crm.get_record("owner", child_id) == original_child
    assert exchange.confirm("owner", "record", source["id"], request) == recovered
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1


@pytest.mark.parametrize("tamper", ["different_error", "different_owner_record"])
def test_legacy_recovery_requires_exact_error_and_owned_adoption_identity(services, tamper):
    crm, _, _, _, exchange, _, _, _, source, _ = services
    request = body(prepare(services))
    result = exchange.confirm("owner", "record", source["id"], request)
    batch = crm._db.execute("SELECT * FROM crm_exchange_batches WHERE owner='owner' AND request_id=?", (request["request_id"],)).fetchone()
    entry = crm._db.execute("SELECT * FROM crm_exchange_batch_items WHERE owner='owner' AND batch_id=?", (batch["id"],)).fetchone()
    checkpoint = json.loads(entry["checkpoint_json"])
    old = {"item_id": entry["item_id"], "status": "blocked", "error": "所选历程归属或依据已失效，请重新核对后讨论",
           "result": {"record_id": checkpoint["record_id"], "stage": "draft_edited"}}
    if tamper == "different_error":
        old["error"] = "项目范围已失效，必须重新核对"
    else:
        unit = crm.create_customer("other", {"name": "另一owner单位"}, NOW)
        alien = crm.create_record("other", {"customer_id": unit["id"], "kind": "action", "title": "不能泄露或修改的行动"}, NOW)
        checkpoint["record_id"] = alien["id"]
    old_response = {**result, "status": "partial", "results": [old]}
    with crm._transaction() as db:
        db.execute("UPDATE crm_exchange_batch_items SET result_json=?,checkpoint_json=? WHERE owner='owner' AND batch_id=? AND item_id=?",
            (json.dumps(old, ensure_ascii=False), json.dumps(checkpoint), batch["id"], entry["item_id"]))
        db.execute("UPDATE crm_exchange_batches SET status='partial',response_json=? WHERE owner='owner' AND id=?",
            (json.dumps(old_response, ensure_ascii=False), batch["id"]))
    assert exchange.confirm("owner", "record", source["id"], request) == old_response
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1
    if tamper == "different_owner_record":
        assert crm.get_record("other", alien["id"]) == alien


def test_other_owner_cannot_replay_owned_batch_or_source(services):
    crm, _, _, _, exchange, _, _, _, source, _ = services
    request = body(prepare(services))
    result = exchange.confirm("owner", "record", source["id"], request)
    with pytest.raises(KeyError):
        exchange.confirm("other", "record", source["id"], request)
    assert exchange.confirm("owner", "record", source["id"], request) == result
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1


def test_interrupt_before_adoption_claim_does_not_overwrite_external_adoption_edit(services, monkeypatch):
    crm, _, _, _, exchange, _, _, _, source, clock = services
    request = body(prepare(services))
    original = exchange._action_timeline_plan
    def stopped_before_claim(*args, **kwargs):
        raise KeyboardInterrupt("synthetic stop after applying but before adoption claim")
    monkeypatch.setattr(exchange, "_action_timeline_plan", stopped_before_claim)
    with pytest.raises(KeyboardInterrupt):
        exchange.confirm("owner", "record", source["id"], request)
    entry = crm._db.execute("SELECT status,stage FROM crm_exchange_batch_items").fetchone()
    assert entry["status"] == "applying" and entry["stage"] == ""
    clock[0] += 1
    action = crm.get_analysis("owner", source["id"])["actions"][0]
    child = crm.adopt_action("owner", source["id"], action["id"], clock[0])
    clock[0] += 1
    crm.update_record("owner", child["id"], {"title": "用户后来明确编辑的行动标题"}, clock[0])
    before = crm.get_record("owner", child["id"])
    monkeypatch.setattr(exchange, "_action_timeline_plan", original)
    resumed = exchange.confirm("owner", "record", source["id"], request)
    assert resumed["results"][0]["status"] == "conflict", resumed
    assert crm.get_record("owner", child["id"]) == before
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
