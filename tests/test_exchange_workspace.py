"""Exchange workspaces use synthetic databases and offline analyzers only."""
import asyncio
import json
import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.customer_timeline import TimelineService
from secretary.exchange_workspace import ExchangeWorkspace, ExchangeConflict
from secretary.materials import MaterialService
from secretary.visits import VisitService

NOW = 1_800_000_000.0


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / "fresh-exchange.sqlite3")
    clock = [NOW]
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    profile = ProfileIntelligence(crm, sales, clock=lambda: clock[0])
    timeline = TimelineService(crm, sales, clock=lambda: clock[0])
    customer = crm.create_customer("owner", {"name": "合成星河银行", "notes": "用户原已录入资料"}, NOW)
    person = crm.create_contact("owner", customer["id"], {"name": "李岚", "department": "技术部"}, NOW)
    exchange = ExchangeWorkspace(crm, sales, profile, timeline=timeline, clock=lambda: clock[0])
    yield crm, sales, profile, timeline, exchange, customer, person, clock
    crm.close()


def record(services, text="李岚说关注数据安全合规。 我答应发送接口清单。"):
    crm, _, _, _, _, customer, _, _ = services
    return crm.create_record("owner", {"customer_id": customer["id"], "title": "现场记录", "content": text,
                                       "kind": "note", "category": "conversation"}, NOW)


def prepare(services, row=None):
    row = row or record(services)
    return asyncio.run(services[4].prepare("owner", "record", row["id"])), row


def confirm(services, view, items, request_id="confirm-1"):
    return services[4].confirm("owner", view["source"]["type"], view["source"]["id"], {
        "request_id": request_id, "expected_revision": view["revision"], "source_revision": view["source_revision"],
        "items": [{"id": item["id"], "expected_version": item["version"], **changes} for item, changes in items]})


def find(view, kind):
    return next(item for item in view["items"] if item["kind"] == kind)


def test_confirmed_reflection_kind_is_guard_not_replayed_content(services):
    crm, _, profile, timeline, exchange, _, _, _ = services
    view, row = prepare(services, record(services, "合成星河银行行业是金融。 我答应发送接口清单。"))
    fact = next(item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "industry")
    assert fact["draft"]["basis"] == "reported"
    old_source = next(source for source in profile._sources(crm._db, "owner") if source["type"] == "record" and source["id"] == row["id"])
    event = timeline.get_event("owner", "record:" + str(row["id"]))
    timeline.save_context("owner", event["key"], {"expected_revision": event["revision"], "kind": "reflection"})
    fresh = exchange.get("owner", "record", row["id"])
    assert fresh["source"]["event_kind"] == "reflection"
    assert fresh["source_revision"] != view["source_revision"]
    latest_source = next(source for source in profile._sources(crm._db, "owner") if source["type"] == "record" and source["id"] == row["id"])
    assert latest_source["event_kind"] == "reflection"
    assert latest_source["fingerprint"] == old_source["fingerprint"]
    with pytest.raises(ExchangeConflict):
        confirm(services, view, [(fact, {"expected_fact_id": None})], "old-kind-confirm")
    current = next(item for item in fresh["items"] if item["id"] == fact["id"])
    blocked = confirm(services, fresh, [(current, {"expected_fact_id": None})], "reflection-reported")
    assert blocked["results"][0]["status"] == "blocked"
    assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0
    refreshed = blocked["workspace"]
    current = next(item for item in refreshed["items"] if item["id"] == fact["id"])
    observed = confirm(services, refreshed, [(current, {"expected_fact_id": None, "draft": {"basis": "observation"}})], "reflection-observed")
    assert observed["results"][0]["status"] == "confirmed"
    assert crm._db.execute("SELECT basis FROM crm_customer_facts").fetchone()[0] == "observation"
    assert crm.get_record("owner", row["id"])["category"] == "conversation"


@pytest.mark.parametrize("quoted", [False, True])
def test_same_batch_reflection_guard_keeps_own_source_transition_only(services, quoted):
    prefix = "客户说" if quoted else ""
    view, row = prepare(services, record(services, prefix + "行业是金融。 我答应发送接口清单。"))
    relation, fact, action = find(view, "relationship"), find(view, "profile"), find(view, "action")
    result = confirm(services, view, [(relation, {"draft": {"kind": "reflection", "occurred_at": NOW - 100}}),
                                    (fact, {"expected_fact_id": None}), (action, {})], "reflection-batch")
    assert result["results"][0]["status"] == "confirmed"
    assert result["results"][1]["status"] == ("confirmed" if quoted else "blocked"), result["results"]
    assert result["results"][2]["status"] == "confirmed", result["results"]
    assert result["workspace"]["source"]["event_kind"] == "reflection"
    assert result["workspace"]["source"]["occurred_at"] == NOW - 100
    assert services[0]._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_stale_timeline_kind_does_not_reclassify_corrected_source(services):
    crm, _, profile, timeline, exchange, _, _, clock = services
    row = record(services, "行业是金融。")
    event = timeline.get_event("owner", "record:" + str(row["id"]))
    timeline.save_context("owner", event["key"], {"expected_revision": event["revision"], "kind": "reflection"})
    clock[0] += 1
    crm.update_record("owner", row["id"], {"content": "行业是医疗。"}, clock[0])
    assert timeline.get_event("owner", event["key"])["needs_review"]
    source = next(source for source in profile._sources(crm._db, "owner") if source["id"] == row["id"])
    assert source.get("event_kind") != "reflection"
    prepared = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert prepared["source"]["event_kind"] == "communication"
    assert find(prepared, "profile")["draft"]["basis"] == "reported"


def test_action_and_schedule_scope_show_valid_source_and_adopted_project(services):
    crm, sales, _, _, exchange, unit, _, _ = services
    project = sales.create_opportunity("owner", unit["id"], {"name": "合成密码应用项目"})
    row = record(services)
    sales.link("owner", "record", row["id"], project["id"])
    view, _ = prepare(services, row)
    for item in view["items"]:
        if item["kind"] in ("action", "schedule"):
            assert item["scope"]["opportunity_id"] == project["id"]
    result = confirm(services, view, [(find(view, "action"), {})], "project-action")
    child = result["results"][0]["result"]["record_id"]
    other = sales.create_opportunity("owner", unit["id"], {"name": "另一正式项目"})
    sales.link("owner", "record", child, other["id"])
    refreshed = exchange.get("owner", "record", row["id"])
    for item in refreshed["items"]:
        if item["kind"] in ("action", "schedule"):
            assert item["scope"]["opportunity_id"] == other["id"]
    assert refreshed["source"]["opportunity_id"] == project["id"]


def test_relation_commit_interruption_recovers_exact_kind_date_not_external_change(services, monkeypatch):
    crm, _, _, timeline, exchange, _, person, clock = services
    view, row = prepare(services, record(services, "行业是金融。 我答应发送接口清单。"))
    relation, fact, action = find(view, "relationship"), find(view, "profile"), find(view, "action")
    body = {"request_id": "context-interrupt", "expected_revision": view["revision"], "source_revision": view["source_revision"],
            "items": [{"id": fact["id"], "expected_version": fact["version"], "expected_fact_id": None, "draft": {"basis": "observation"}},
                      {"id": relation["id"], "expected_version": relation["version"], "draft": {"kind": "reflection", "occurred_at": NOW-300,
                       "contact_relations": [{"contact_id": person["id"], "relation": "about"}]}},
                      {"id": action["id"], "expected_version": action["version"]}]}
    original = timeline.save_context
    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("synthetic response lost after context commit")
    monkeypatch.setattr(timeline, "save_context", interrupt)
    with pytest.raises(KeyboardInterrupt):
        exchange.confirm("owner", "record", row["id"], body)
    monkeypatch.setattr(timeline, "save_context", original)
    result = exchange.confirm("owner", "record", row["id"], body)
    assert result["status"] == "complete", result["results"]
    assert result["results"][0]["item_id"] == relation["id"]
    assert result["results"][0]["status"] == "already_confirmed"
    assert len(timeline.get_event("owner", "record:"+str(row["id"]))["contact_relations"]) == 1
    assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 1
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert exchange.confirm("owner", "record", row["id"], body) == result


def test_own_relation_receipt_cannot_rebase_external_source_correction(services, monkeypatch):
    crm, _, _, timeline, exchange, _, _, clock = services
    view, row = prepare(services)
    relation, action = find(view, "relationship"), find(view, "action")
    body = {"request_id": "external-after-context", "expected_revision": view["revision"], "source_revision": view["source_revision"],
            "items": [{"id": relation["id"], "expected_version": relation["version"], "draft": {"kind": "reflection"}},
                      {"id": action["id"], "expected_version": action["version"]}]}
    original = timeline.save_context
    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("synthetic process restart")
    monkeypatch.setattr(timeline, "save_context", interrupt)
    with pytest.raises(KeyboardInterrupt):
        exchange.confirm("owner", "record", row["id"], body)
    monkeypatch.setattr(timeline, "save_context", original)
    clock[0] += 1
    crm.update_record("owner", row["id"], {"content": "客户要求暂缓，原承诺取消待核对。"}, clock[0])
    result = exchange.confirm("owner", "record", row["id"], body)
    assert all(item["status"] == "conflict" for item in result["results"])
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 0
    assert crm.get_record("owner", row["id"])["original_content"] == row["content"]


def test_prepare_groups_preserve_original_and_existing_customer(services):
    crm, _, _, _, _, customer, _, _ = services
    before = crm.get_customer("owner", customer["id"])
    view, row = prepare(services)
    assert view["source"]["text"] == view["source"]["original_text"] == row["content"]
    assert view["capabilities"]["profile_analysis"] == "rules"
    assert view["groups"]["profile"] and view["groups"]["action"] and view["groups"]["relationship"]
    assert all(not item["selected"] for item in view["items"])
    after = crm.get_customer("owner", customer["id"])
    assert {key: value for key, value in after.items() if key != "record_count"} == {key: value for key, value in before.items() if key != "record_count"}
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_edit_and_adopt_only_action_no_schedule_and_request_replay(services):
    crm, _, _, _, exchange, _, _, _ = services
    view, row = prepare(services)
    action = find(view, "action")
    result = confirm(services, view, [(action, {"draft": {"title": "准备并发送适配清单", "executor_kind": "self"}})])
    receipt = result["results"][0]
    assert receipt["status"] == "confirmed", receipt
    adopted = crm.get_record("owner", receipt["result"]["record_id"])
    assert adopted["title"] == "准备并发送适配清单"
    assert adopted["action_terms"]["executor_kind"] == "self"
    assert crm.get_record("owner", row["id"])["original_content"] == row["content"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert result == confirm(services, view, [(action, {"draft": {"title": "准备并发送适配清单", "executor_kind": "self"}})])
    with pytest.raises(ExchangeConflict):
        confirm(services, view, [(action, {"draft": {"title": "不同输入"}})])
    assert len(exchange.get("owner", "record", row["id"])["items"]) >= 3


def test_partial_profile_scope_conflict_keeps_latest_unsaved_value(services):
    view, _ = prepare(services, record(services, "客户说项目预算尚需核实。 我答应发送接口清单。"))
    fact, action = next(x for x in view["items"] if x["kind"] == "profile" and x["scope"]["scope"] == "project"), find(view, "action")
    # The project-level concern is unbound; no project must not silently become a unit fact.
    result = confirm(services, view, [(fact, {"draft": {"value": "尚需核实项目合规要求", "basis": "observation"},
                                                   "expected_fact_id": None}), (action, {})])
    assert result["status"] == "partial"
    assert result["results"][0]["status"] in ("blocked", "conflict")
    assert result["results"][1]["status"] == "confirmed"
    fresh = result["workspace"]
    assert next(x for x in fresh["items"] if x["id"] == fact["id"])["draft"]["value"] == "尚需核实项目合规要求"


def test_source_correction_blocks_old_batch_and_lists_impact(services):
    crm = services[0]
    view, row = prepare(services)
    result = confirm(services, view, [(find(view, "action"), {})])
    crm.update_record("owner", row["id"], {"content": "暂不发送清单，等客户核实。"}, NOW + 1)
    fresh = services[4].get("owner", "record", row["id"])
    assert fresh["status"] == "stale" and fresh["correction_impacts"]
    with pytest.raises(ExchangeConflict):
        confirm(services, view, [(find(view, "relationship"), {})], "old-source")
    assert crm.get_record("owner", result["results"][0]["result"]["record_id"])["status"] != "done"


def test_explicit_people_not_inferred_from_project_members(services):
    crm, _, _, timeline, exchange, customer, person, _ = services
    view, row = prepare(services)
    relation = find(view, "relationship")
    assert relation["draft"]["contact_relations"] == []
    result = confirm(services, view, [(relation, {"draft": {"contact_relations": [
        {"contact_id": person["id"], "relation": "about"}], "kind": "communication"}})])
    assert result["results"][0]["status"] == "confirmed", result["results"]
    event = timeline.get_event("owner", "record:" + str(row["id"]))
    assert event["contact_relations"][0]["relation"] == "about"
    foreign = crm.create_customer("other", {"name": "另一个owner"}, NOW)
    outsider = crm.create_contact("other", foreign["id"], {"name": "李岚"}, NOW)
    fresh = exchange.get("owner", "record", row["id"])
    result = confirm(services, fresh, [(find(fresh, "relationship"), {"draft": {
        "contact_relations": [{"contact_id": outsider["id"], "relation": "direct"}]}})], "foreign-person")
    assert result["results"][0]["status"] in ("blocked", "failed")
    assert timeline.get_event("owner", "record:" + str(row["id"]))["contact_relations"][0]["contact_id"] == person["id"]


def test_owner_size_unknown_and_atomic_validation(services):
    view, row = prepare(services)
    exchange = services[4]
    with pytest.raises(KeyError):
        exchange.get("other", "record", row["id"])
    with pytest.raises(ValueError):
        confirm(services, view, [(find(view, "action"), {})], "   ")
    before = exchange.get("owner", "record", row["id"])
    with pytest.raises(ValueError):
        exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
            "source_revision": view["source_revision"], "items": [{"id": find(view, "action")["id"],
            "expected_version": find(view, "action")["version"], "draft": {"title": "a" * 121}}]})
    assert exchange.get("owner", "record", row["id"])["revision"] == before["revision"]


def test_draft_reopen_and_noop(services):
    view, row = prepare(services)
    exchange = services[4]
    action = find(view, "action")
    body = {"expected_revision": view["revision"], "source_revision": view["source_revision"],
            "items": [{"id": action["id"], "expected_version": action["version"], "draft": {"title": "我的用户改稿"}, "selected": True}]}
    changed = exchange.edit_draft("owner", "record", row["id"], body)
    reopened = ExchangeWorkspace(*services[:3], timeline=services[3], clock=lambda: NOW)
    assert find(reopened.get("owner", "record", row["id"]), "action")["draft"]["title"] == "我的用户改稿"
    body["expected_revision"] = changed["revision"]
    body["items"][0]["expected_version"] = find(changed, "action")["version"]
    assert reopened.edit_draft("owner", "record", row["id"], body)["revision"] == changed["revision"]


def test_schedule_requires_independent_selection_and_future_time(services):
    view, _ = prepare(services)
    schedule = find(view, "schedule")
    result = confirm(services, view, [(schedule, {"draft": {"remind_at": NOW + 3600, "duration_minutes": 30}})])
    assert result["results"][0]["status"] == "blocked"
    assert services[0]._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    view = result["workspace"]
    result = confirm(services, view, [(find(view, "schedule"), {"draft": {"remind_at": NOW + 3600, "duration_minutes": 30}}),
                                     (find(view, "action"), {})], "action-and-schedule")
    assert result["status"] == "complete", result["results"]
    scheduled = next(row for row in result["results"] if row["item_id"].startswith("schedule:"))
    assert services[0].get_task("owner", scheduled["result"]["task_id"])["remind_at"] == NOW + 3600
    assert services[0]._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_schedule_conflict_leaves_action_and_draft_then_reconfirm_new_request(services):
    crm = services[0]
    crm.execute("owner", "existing", {"action": "propose", "title": "已有安排", "remind_at": NOW + 3600}, NOW)
    crm.execute("owner", "existing-confirm", {"action": "confirm", "proposal_id": 1}, NOW)
    view, _ = prepare(services)
    result = confirm(services, view, [(find(view, "action"), {}), (find(view, "schedule"), {"draft": {"remind_at": NOW + 3600, "duration_minutes": 30}})])
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "confirmed"
    assert result["results"][1]["status"] == "conflict"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    view = result["workspace"]
    result = confirm(services, view, [(find(view, "schedule"), {"draft": {"remind_at": NOW + 7200, "duration_minutes": 30}})], "new-time")
    assert result["status"] == "complete", result["results"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


def test_profile_target_cas_conflict_does_not_overwrite_current(services):
    crm, _, _, _, _, customer, person, _ = services
    view, _ = prepare(services)
    item = find(view, "profile")
    crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": item["candidate"]["key"],
        "value": "后来用户核实的最新关注", "basis": "reported", "evidence": "用户手工核实"}, NOW + 1)
    result = confirm(services, view, [(item, {"expected_fact_id": None})])
    assert result["results"][0]["status"] == "conflict"
    assert result["workspace"]["items"][0]["status"] == "pending"
    assert crm.profile("owner", customer["id"])["contacts"][0]["fields"][0]["value"] == "后来用户核实的最新关注"


def test_profile_missing_fact_cas_is_partial_not_aborted(services):
    view, _ = prepare(services)
    result = confirm(services, view, [(find(view, "profile"), {}), (find(view, "action"), {})])
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "blocked"
    assert result["results"][1]["status"] == "confirmed"


def test_profile_committed_then_runtime_error_receipt_recovers_once(services, monkeypatch):
    _, _, profile, _, _, customer, _, _ = services
    view, _ = prepare(services)
    item = find(view, "profile")
    original = profile.decide
    def failed_response(*args):
        original(*args)
        raise RuntimeError("response transport lost after commit")
    monkeypatch.setattr(profile, "decide", failed_response)
    result = confirm(services, view, [(item, {"expected_fact_id": None})])
    assert result["status"] == "complete"
    assert result["results"][0]["status"] == "already_confirmed"
    assert services[0].profile("owner", customer["id"])["history_total"] == 1
    assert result == confirm(services, view, [(item, {"expected_fact_id": None})])


def test_slow_organizer_source_edit_discards_late_response(services):
    crm, _, _, _, exchange, _, _, _ = services
    row = record(services)
    class Organizer:
        async def organize(self, text, now, context):
            crm.update_record("owner", row["id"], {"content": "原话已纠正，不需要该方案。"}, NOW + 1)
            return {"summary": "旧回复", "actions": [{"title": "错误旧行动", "kind": "suggestion", "reason": "旧原话", "owner_hint": "", "remind_at": None}], "key_points": [], "open_questions": []}
    exchange.organizer = Organizer()
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert view["status"] == "stale"
    assert crm.get_analysis("owner", row["id"]) is None
    assert crm.get_record("owner", row["id"])["original_content"] == row["original_content"]


def test_customer_draft_inline_edit_preserves_evidence_and_original(services):
    crm, _, _, _, exchange, customer, _, _ = services
    row = record(services, "合成星河银行总部在上海。")
    draft = crm.create_customer_draft("owner", {"intent": "update", "customer_id": customer["id"],
        "customer_name": customer["name"], "source_text": row["original_content"], "basic": {"contact": "上海"},
        "basic_evidence": {"contact": "总部在上海"}, "contact": {}, "contact_evidence": {}, "attributes": []}, NOW,
        source_record_id=row["id"])
    view = exchange.get("owner", "record", row["id"])
    item = find(view, "customer")
    result = confirm(services, view, [(item, {"draft": {"changes": [{"target": "basic", "key": "contact", "after": "上海总部"}]}})])
    assert result["status"] == "complete", result["results"]
    assert crm.get_customer("owner", customer["id"])["contact"] == "上海总部"
    assert crm.get_record("owner", row["id"])["original_content"] == row["original_content"]
    assert crm.get_customer_draft("owner", draft["id"])["status"] == "stale"


def test_material_and_visit_share_canonical_adoption_and_keep_recap_date(services):
    crm, sales, profile, _, _, customer, _, clock = services
    class Organizer:
        async def organize(self, text, now, context):
            return {"summary": text, "key_points": [], "open_questions": [], "actions": [
                {"title": "发送接口清单", "kind": "commitment", "reason": '原话：“我答应发送接口清单”',
                 "evidence": "我答应发送接口清单", "owner_hint": "我", "remind_at": None}]}
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: clock[0])
    visits = VisitService(crm, materials, materials.lock)
    timeline = TimelineService(crm, sales, visits=visits, clock=lambda: clock[0])
    exchange = ExchangeWorkspace(crm, sales, profile, materials=materials, visits=visits, timeline=timeline, clock=lambda: clock[0])
    visit = visits.create("owner", {"customer_id": customer["id"], "title": "现场拜访", "occurred_at": NOW - 86400})
    added = visits.add_material("owner", visit["id"], {"role": "recording", "provider": "manual", "title": "现场录音", "text": "我答应发送接口清单。"})
    while asyncio.run(materials.process_one()):
        pass
    mid = added["material"]["id"]
    material_view = asyncio.run(exchange.prepare("owner", "material", mid))
    visit_view = asyncio.run(exchange.prepare("owner", "visit", visit["id"]))
    assert material_view["source"]["original_text"] == "我答应发送接口清单。"
    assert len(visit_view["groups"]["action"]) == 1
    result = exchange.confirm("owner", "visit", visit["id"], {"request_id": "visit-adopt", "expected_revision": visit_view["revision"],
        "source_revision": visit_view["source_revision"], "items": [{"id": find(visit_view, "action")["id"], "expected_version": find(visit_view, "action")["version"]}]})
    assert result["status"] == "complete", result["results"]
    reflected = exchange.get("owner", "material", mid)
    assert find(reflected, "action")["current"]["record"]["id"] == result["results"][0]["result"]["record_id"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    # The next day's own reflection is a separate preserved source.
    recap = materials.enqueue("owner", {"provider": "manual", "customer_id": customer["id"], "title": "第二天复盘",
        "text": "我觉得下次先讲清适配范围。", "occurred_at": NOW, "category": "visit_review"}, source_id="next-day")
    assert exchange.get("owner", "material", recap["id"])["source"]["occurred_at"] == NOW


@pytest.mark.parametrize("kind", ["material", "visit"])
def test_mixed_recording_and_next_day_recap_prepare_ready_with_independent_evidence(services, kind):
    from secretary.profile_intelligence import _rule_extract
    crm, sales, profile, _, _, customer, _, clock = services
    class Organizer:
        async def organize(self, text, now, context):
            return {"summary": text, "key_points": [], "open_questions": [], "actions": []}
    class TrialAnalyzer:
        async def extract(self, source, context):
            return _rule_extract(source, context)
    profile.analyzer = TrialAnalyzer()
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: clock[0])
    visits = VisitService(crm, materials, materials.lock)
    timeline = TimelineService(crm, sales, visits=visits, clock=lambda: clock[0])
    exchange = ExchangeWorkspace(crm, sales, profile, materials=materials, visits=visits, timeline=timeline, clock=lambda: clock[0])
    visit = visits.create("owner", {"customer_id": customer["id"], "title": "合成现场录音与隔日复盘", "occurred_at": NOW - 86400})
    recording_text = "客户李岚说兼容性是先决条件，行业是金融。"
    recap_text = "这是隔日个人复盘，行业是医疗，尚待核实。"
    first = visits.add_material("owner", visit["id"], {"role": "recording", "provider": "manual", "title": "合成现场录音", "text": recording_text})["material"]
    second = visits.add_material("owner", visit["id"], {"role": "recap", "provider": "manual", "title": "合成隔日复盘",
        "confirm_time_difference": True, "occurred_at": NOW, "text": recap_text})["material"]
    while asyncio.run(materials.process_one()):
        pass
    identifier = first["id"] if kind == "material" else visit["id"]
    view = asyncio.run(exchange.prepare("owner", kind, identifier, {"force": True}))
    assert view["status"] == "ready", view["message"]
    assert not view["message"]
    with crm._lock:
        sources = profile._sources(crm._db, "owner", customer["id"])
    first_source = next(source for source in sources if source["type"] == "material" and source["id"] == first["id"])
    second_source = next(source for source in sources if source["type"] == "material" and source["id"] == second["id"])
    assert first_source["role"] == "recording" and second_source["role"] == "recap"
    if kind == "visit":
        assert view["source"]["event_kind"] == "communication"
        pieces = {source["id"]: source for source in view["source"]["sources"]}
        assert pieces[first["id"]]["title"] == "合成现场录音"
        assert pieces[second["id"]]["title"] == "合成隔日复盘"
        assert pieces[first["id"]]["text"] == pieces[first["id"]]["original_text"] == recording_text
        assert pieces[second["id"]]["text"] == pieces[second["id"]]["original_text"] == recap_text
        assert pieces[first["id"]]["occurred_at"] == NOW - 86400
        assert pieces[second["id"]]["occurred_at"] == NOW
        assert all("合成隔日复盘" in warning for warning in view["warnings"] if "这是个人复盘" in warning)
        candidates = [item for item in view["items"] if item["kind"] == "profile"]
        assert any(item["candidate"]["source"]["id"] == first["id"] and item["draft"]["basis"] == "reported" for item in candidates)
        assert any(item["candidate"]["source"]["id"] == second["id"] and item["draft"]["basis"] == "observation" for item in candidates)
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["other_source", "source_choice"])
def test_visit_late_fact_reply_guards_other_material_and_inclusion(services, change):
    async def run():
        crm, sales, profile, _, _, customer, _, clock = services
        class Organizer:
            async def organize(self, text, now, context):
                return {"summary": text, "key_points": [], "open_questions": [], "actions": []}
        started, release = asyncio.Event(), asyncio.Event()
        class SlowAnalyzer:
            async def extract(self, source, context):
                started.set()
                await release.wait()
                return [{"key": "industry", "value": "迟到金融资料不应写入", "basis": "reported", "evidence": source["text"]}]
        materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: clock[0])
        visits = VisitService(crm, materials, materials.lock)
        timeline = TimelineService(crm, sales, visits=visits, clock=lambda: clock[0])
        exchange = ExchangeWorkspace(crm, sales, profile, materials=materials, visits=visits, timeline=timeline, clock=lambda: clock[0])
        visit = visits.create("owner", {"customer_id": customer["id"], "title": "合成组合资料CAS", "occurred_at": NOW - 100})
        first = visits.add_material("owner", visit["id"], {"role": "recording", "provider": "manual", "title": "合成现场资料", "text": "客户说行业是金融。"})["material"]
        second = visits.add_material("owner", visit["id"], {"role": "recap", "provider": "manual", "title": "合成补充复盘", "text": "行业是医疗，尚待核实。"})["material"]
        while await materials.process_one():
            pass
        profile.analyzer = SlowAnalyzer()
        task = asyncio.create_task(exchange.prepare("owner", "visit", visit["id"], {"force": True}))
        await asyncio.wait_for(started.wait(), 2)
        clock[0] += 1
        if change == "other_source":
            current = materials.detail("owner", second["id"])["material"]
            materials.update("owner", second["id"], {"revision": current["revision"], "text": "更正复盘：客户要求暂缓，行业还待核实。"})
        else:
            current = visits.detail("owner", visit["id"])["visit"]
            visits.decide_source("owner", visit["id"], second["id"], current["revision"], "excluded", "仅保留原文，暂不作为依据")
        release.set()
        view = await asyncio.wait_for(task, 2)
        assert view["status"] == "stale"
        assert not profile.list_candidates("owner")["items"]
        assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0
        assert materials.detail("owner", first["id"])["text"] == "客户说行业是金融。"
    asyncio.run(run())


def test_action_adopt_commits_then_response_lost_journal_recovers_edit(services, monkeypatch):
    crm = services[0]
    view, _ = prepare(services)
    item = find(view, "action")
    original = crm.adopt_action
    def commit_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lost after canonical adoption")
    monkeypatch.setattr(crm, "adopt_action", commit_then_fail)
    result = confirm(services, view, [(item, {"draft": {"title": "用户修改后的清单行动"}})])
    assert result["status"] == "complete", result["results"]
    assert result["results"][0]["status"] == "already_confirmed"
    assert crm.get_record("owner", result["results"][0]["result"]["record_id"])["title"] == "用户修改后的清单行动"
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1


def test_action_edit_commits_then_response_lost_recovers_without_reapplying(services, monkeypatch):
    import secretary.exchange_workspace as module
    view, _ = prepare(services)
    original = module.edit_unconfirmed_action
    def committed_edit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lost after canonical edit")
    monkeypatch.setattr(module, "edit_unconfirmed_action", committed_edit)
    result = confirm(services, view, [(find(view, "action"), {"draft": {"title": "编辑已实际提交"}})])
    assert result["status"] == "complete", result["results"]
    assert services[0].get_record("owner", result["results"][0]["result"]["record_id"])["title"] == "编辑已实际提交"


def test_interrupted_batch_resumes_exact_item_not_duplicate(services, monkeypatch):
    crm = services[0]
    view, _ = prepare(services)
    item = find(view, "action")
    original = crm.adopt_action
    def abrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("process ended after commit")
    monkeypatch.setattr(crm, "adopt_action", abrupt)
    with pytest.raises(KeyboardInterrupt):
        confirm(services, view, [(item, {"draft": {"title": "中断后仍保留的改稿"}})])
    monkeypatch.setattr(crm, "adopt_action", original)
    result = confirm(services, view, [(item, {"draft": {"title": "中断后仍保留的改稿"}})])
    assert result["status"] == "complete", result["results"]
    assert crm.get_record("owner", result["results"][0]["result"]["record_id"])["title"] == "中断后仍保留的改稿"
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 1


def test_cancelled_profile_analyzer_managed_source_not_taken_by_scan(services):
    _, _, profile, _, exchange, _, _, _ = services
    row = record(services)
    calls = []
    class Analyzer:
        async def extract(self, source, context):
            calls.append(source["id"])
            await profile.scan("owner", force=True)
            raise asyncio.CancelledError()
    profile.analyzer = Analyzer()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert calls == [row["id"]]
    assert exchange.get("owner", "record", row["id"])["status"] == "draft"
    assert profile.list_candidates("owner")["total"] == 0


def test_same_name_contacts_require_scope_preview_and_changed_scope_cas(services):
    crm, _, profile, _, exchange, customer, person, _ = services
    second = crm.create_contact("owner", customer["id"], {"name": "李岚", "department": "采购部"}, NOW)
    view, row = prepare(services)
    item = find(view, "profile")
    assert item["scope"]["contact_id"] is None
    assert {person["department"] for person in item["scope"]["options"]["contacts"]} == {"技术部", "采购部"}
    saved = exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": item["id"], "expected_version": item["version"], "scope": {"contact_id": second["id"]}}]})
    scoped = find(saved, "profile")
    assert scoped["scope"]["contact_id"] == second["id"] and scoped["versions"]["current_fact_id"] is None
    result = confirm(services, saved, [(scoped, {"expected_fact_id": None})])
    assert result["status"] == "complete", result["results"]
    assert profile.get_candidate("owner", scoped["candidate"]["id"])["contact_id"] == second["id"]


def test_interrupted_adoption_does_not_overwrite_manual_edit_on_resume(services, monkeypatch):
    crm = services[0]
    view, _ = prepare(services)
    action = find(view, "action")
    original = crm.adopt_action
    stored = []
    def abrupt(*args, **kwargs):
        stored.append(original(*args, **kwargs)["id"])
        raise KeyboardInterrupt()
    monkeypatch.setattr(crm, "adopt_action", abrupt)
    with pytest.raises(KeyboardInterrupt):
        confirm(services, view, [(action, {"draft": {"title": "准备稿意图"}})])
    crm.update_record("owner", stored[0], {"title": "用户后来明确的行动"}, NOW + 2)
    monkeypatch.setattr(crm, "adopt_action", original)
    result = confirm(services, view, [(action, {"draft": {"title": "准备稿意图"}})])
    assert result["results"][0]["status"] == "conflict", result["results"]
    assert crm.get_record("owner", stored[0])["title"] == "用户后来明确的行动"


def test_new_profile_observation_cannot_be_upgraded_to_reported(services):
    view, _ = prepare(services, record(services, "我觉得李岚关注数据安全。 我准备发接口清单。"))
    item = find(view, "profile")
    assert item["draft"]["basis"] == "observation"
    result = confirm(services, view, [(item, {"draft": {"basis": "reported"}, "expected_fact_id": None})])
    assert result["results"][0]["status"] == "blocked"
    assert services[0]._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0


def test_source_unassigned_preserves_original_and_manual_action_no_facts(services):
    crm, _, _, _, exchange, _, _, _ = services
    row = crm.create_record("owner", {"title": "随口记录", "content": "我准备整理密码适配资料。", "kind": "note"}, NOW)
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert not view["groups"]["profile"] and view["groups"]["action"]
    assert view["source"]["customer_id"] is None
    result = confirm(services, view, [(find(view, "action"), {})])
    assert result["status"] == "complete", result["results"]
    assert crm.get_record("owner", row["id"])["content"] == "我准备整理密码适配资料。"


def test_project_action_scope_and_archived_project_block(services):
    crm, sales, _, _, exchange, customer, _, _ = services
    project = sales.create_opportunity("owner", customer["id"], {"name": "合成密码改造"})
    row = record(services)
    sales.link("owner", "record", row["id"], project["id"])
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert view["source"]["opportunity_id"] == project["id"]
    result = confirm(services, view, [(find(view, "action"), {})])
    assert result["status"] == "complete", result["results"]
    child = result["results"][0]["result"]["record_id"]
    assert exchange.timeline.get_event("owner", "record:" + str(child))["opportunity_id"] == project["id"]
    row2 = record(services)
    sales.link("owner", "record", row2["id"], project["id"])
    view2 = asyncio.run(exchange.prepare("owner", "record", row2["id"]))
    sales.update_opportunity("owner", customer["id"], project["id"], {"archived": True, "expected_revision": project["revision"]})
    result2 = confirm(services, view2, [(find(view2, "action"), {})], "archived-project")
    assert result2["results"][0]["status"] == "blocked"
    assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions WHERE parent_record_id=?", (row2["id"],)).fetchone()[0] == 0


def test_cross_unit_person_options_explicit_project_and_no_inferred_participation(services):
    crm, sales, _, timeline, exchange, customer, _, _ = services
    external = crm.create_customer("owner", {"name": "合成集团采购中心"}, NOW)
    person = crm.create_contact("owner", external["id"], {"name": "李岚", "department": "采购部"}, NOW)
    project = sales.create_opportunity("owner", customer["id"], {"name": "银行密码改造"})
    project = sales.upsert_project_unit("owner", customer["id"], project["id"], {
        "participant_customer_id": external["id"], "expected_revision": project["revision"],
        "roles": ["procurement"], "evidence": "本项目集中采购由该中心负责"})
    sales.upsert_stakeholder("owner", customer["id"], project["id"], {"contact_id": person["id"],
        "roles": ["procurement"], "evidence": "用户核对该人为本项目采购联系人", "expected_revision": project["revision"]})
    row = record(services)
    sales.link("owner", "record", row["id"], project["id"])
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    relation = find(view, "relationship")
    option = next(p for p in relation["scope"]["options"]["contacts"] if p["id"] == person["id"])
    assert option["customer_id"] == external["id"] and option["unit_name"] == external["name"]
    assert relation["draft"]["contact_relations"] == []
    result = confirm(services, view, [(relation, {"draft": {"contact_relations": [{"contact_id": person["id"], "relation": "direct"}]}})])
    assert result["status"] == "complete", result["results"]
    assert timeline.get_event("owner", "record:" + str(row["id"]))["contact_relations"][0]["contact_id"] == person["id"]
    unrelated = record(services)
    plain = exchange.get("owner", "record", unrelated["id"])
    result = confirm(services, plain, [(find(plain, "relationship"), {"draft": {
        "contact_relations": [{"contact_id": person["id"], "relation": "direct"}]}})], "unrelated-person")
    assert result["results"][0]["status"] == "blocked"
    assert not timeline.get_event("owner", "record:" + str(unrelated["id"]))["contact_relations"]


def test_lease_takeover_discards_old_organizer_output(services):
    crm, _, _, _, exchange, _, _, _ = services
    row = record(services)
    class Organizer:
        async def organize(self, text, now, context):
            with crm._transaction() as db:
                db.execute("UPDATE crm_exchange_workspaces SET lease='new-owner-lease' WHERE owner='owner'")
            return {"summary": "不可写入的晚回复", "key_points": [], "open_questions": [], "actions": []}
    exchange.organizer = Organizer()
    asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert crm.get_analysis("owner", row["id"]) is None


def test_corrected_confirmed_fact_impact_survives_reprepare(services):
    crm, _, _, _, exchange, _, _, _ = services
    view, row = prepare(services)
    fact = find(view, "profile")
    result = confirm(services, view, [(fact, {"expected_fact_id": None})])
    assert result["status"] == "complete"
    crm.update_record("owner", row["id"], {"content": "李岚明确说现在不再关注该问题。"}, NOW + 1)
    fresh = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert any(impact["kind"] == "profile" for impact in fresh["correction_impacts"])


def test_visit_two_actions_same_batch_own_revision_changes_do_not_block(services):
    crm, sales, profile, _, _, customer, _, clock = services
    class Organizer:
        async def organize(self, text, now, context):
            return {"summary": text, "key_points": [], "open_questions": [], "actions": [
                {"title": title, "kind": "commitment", "reason": '原话：“' + quote + '”',
                 "evidence": quote, "owner_hint": "我", "remind_at": None}
                for title, quote in [("发送接口清单", "我答应发送接口清单"), ("预约技术交流", "我答应预约技术交流")]]}
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: clock[0])
    visits = VisitService(crm, materials, materials.lock)
    exchange = ExchangeWorkspace(crm, sales, profile, materials=materials, visits=visits, clock=lambda: clock[0])
    visit = visits.create("owner", {"customer_id": customer["id"], "title": "多人交流", "occurred_at": NOW})
    visits.add_material("owner", visit["id"], {"role": "recording", "provider": "manual", "title": "录音", "text": "我答应发送接口清单。 我答应预约技术交流。"})
    while asyncio.run(materials.process_one()):
        pass
    view = asyncio.run(exchange.prepare("owner", "visit", visit["id"]))
    actions = [item for item in view["items"] if item["kind"] == "action"]
    assert len(actions) == 2
    result = exchange.confirm("owner", "visit", visit["id"], {"request_id": "two-actions", "expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": item["id"], "expected_version": item["version"]} for item in actions]})
    assert result["status"] == "complete", result["results"]
    assert crm._db.execute("SELECT count(*) FROM crm_visit_adoptions").fetchone()[0] == 2


def test_edit_without_selection_does_not_check_item_and_false_confirm_atomic(services):
    exchange = services[4]
    view, row = prepare(services)
    item = find(view, "action")
    edited = exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": item["id"], "expected_version": item["version"],
        "draft": {"title": "只是修改，尚未选择"}}]})
    assert not find(edited, "action")["selected"]
    with pytest.raises(ValueError):
        confirm(services, edited, [(find(edited, "action"), {"selected": False})])
    assert services[0]._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 0


def test_stakeholder_requires_project_preview_roles_not_global_authority(services):
    crm, sales, _, _, exchange, customer, person, _ = services
    project = sales.create_opportunity("owner", customer["id"], {"name": "合成密码项目"})
    row = record(services, "李岚说由他负责最终审批。")
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    relation = next(item for item in view["items"] if item.get("subtype") == "stakeholder")
    assert relation["versions"]["project_revision"] is None
    patched = exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": relation["id"], "expected_version": relation["version"],
        "draft": {"contact_id": person["id"], "opportunity_id": project["id"], "roles": ["final_approver"]}}]})
    relation = next(item for item in patched["items"] if item.get("subtype") == "stakeholder")
    assert relation["versions"]["project_revision"] == project["revision"]
    result = confirm(services, patched, [(relation, {})])
    assert result["status"] == "complete", result["results"]
    assert sales.stakeholders("owner", customer["id"], project["id"])["items"][0]["roles"] == ["final_approver"]
    assert crm._db.execute("SELECT count(*) FROM crm_customer_facts WHERE key='authority'").fetchone()[0] == 0


def test_two_project_roles_same_batch_use_only_own_revision_updates(services):
    crm, sales, _, _, exchange, customer, person, _ = services
    second = crm.create_contact("owner", customer["id"], {"name": "张强", "department": "采购部"}, NOW)
    project = sales.create_opportunity("owner", customer["id"], {"name": "多决策人项目"})
    row = record(services, "李岚说负责最终审批。张强说负责采购审批。")
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    items = [item for item in view["items"] if item.get("subtype") == "stakeholder"]
    assert len(items) == 2
    saved = exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": item["id"], "expected_version": item["version"],
        "draft": {"opportunity_id": project["id"], "contact_id": item["scope"]["contact_id"],
        "roles": ["procurement" if item["scope"]["contact_id"] == second["id"] else "final_approver"]}} for item in items]})
    chosen = [item for item in saved["items"] if item.get("subtype") == "stakeholder"]
    result = confirm(services, saved, [(item, {}) for item in chosen])
    assert result["status"] == "complete", result["results"]
    people = sales.stakeholders("owner", customer["id"], project["id"])["items"]
    assert {p["contact_id"] for p in people} == {person["id"], second["id"]}
    assert crm._db.execute("SELECT count(*) FROM crm_customer_facts WHERE key='authority'").fetchone()[0] == 0


def test_reject_incomplete_customer_changes_does_not_silently_apply_omitted_fields(services):
    crm, _, _, _, exchange, customer, _, _ = services
    row = record(services, "合成星河银行总部在上海。")
    crm.create_customer_draft("owner", {"intent": "update", "customer_id": customer["id"],
        "customer_name": customer["name"], "source_text": row["original_content"], "basic": {"contact": "上海"},
        "basic_evidence": {"contact": "总部在上海"}, "contact": {}, "contact_evidence": {}, "attributes": []}, NOW, source_record_id=row["id"])
    view = exchange.get("owner", "record", row["id"])
    with pytest.raises(ValueError):
        confirm(services, view, [(find(view, "customer"), {"draft": {"changes": []}})])
    assert crm.get_customer("owner", customer["id"])["contact"] == ""


def test_confirmed_candidate_new_edit_not_reported_as_new_save(services):
    view, _ = prepare(services)
    first = confirm(services, view, [(find(view, "profile"), {"expected_fact_id": None})])
    assert first["status"] == "complete"
    fresh = first["workspace"]
    item = find(fresh, "profile")
    result = confirm(services, fresh, [(item, {"draft": {"value": "这次修改不能冒充已保存"},
        "expected_fact_id": item["versions"]["current_fact_id"]})], "changed-confirmed")
    assert result["results"][0]["status"] == "blocked"
    assert services[0]._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 1
    assert services[0]._db.execute("SELECT value FROM crm_customer_facts").fetchone()[0] != "这次修改不能冒充已保存"


def test_existing_synthetic_user_data_unchanged_by_sidecar_reopen(services):
    crm, sales, profile, timeline, _, customer, person, _ = services
    row = record(services)
    crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": "professional_goals", "value": "旧已录入目标",
        "basis": "reported", "evidence": "用户核实的旧资料"}, NOW)
    tables = ("crm_customers", "crm_contacts", "crm_records", "crm_customer_facts", "tasks", "proposals")
    before = {table: [tuple(row) for row in crm._db.execute("SELECT * FROM " + table + " ORDER BY id")] for table in tables}
    reopened = ExchangeWorkspace(crm, sales, profile, timeline=timeline, clock=lambda: NOW)
    reopened.get("owner", "record", row["id"])
    after = {table: [tuple(row) for row in crm._db.execute("SELECT * FROM " + table + " ORDER BY id")] for table in tables}
    assert before == after
