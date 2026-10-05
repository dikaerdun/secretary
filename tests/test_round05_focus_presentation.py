"""R05 presentation evidence from fresh real services and direct web handlers.

These are not HTTP/security-stack tests. No server, provider, formal database or
runtime configuration is loaded. Current timeline context, not legacy record
category or a person's project membership alone, supplies factual labels.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import WebCRM, hash_password


OWNER = "r05-focus-synthetic"
NOW = 1_800_550_000.0


class NoModel:
    def __init__(self):
        self.calls = 0

    async def reply(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Reading presentation must never invoke a model")


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "round05-focus-fresh.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    clock = [NOW]
    web = WebCRM(store, crm, asyncio.Lock(), OWNER,
                 hash_password("round05-public-synthetic-password"),
                 clock=lambda: clock[0])
    model = NoModel()
    web.discussions.advisor = model
    unit = crm.create_customer(OWNER, {"name": "R05画像测试单位"}, NOW)
    person = crm.create_contact(OWNER, unit["id"],
                                {"name": "R05林工", "department": "信息中心"}, NOW)
    project_a = web.sales_workspace.create_opportunity(
        OWNER, unit["id"], {"name": "A数据库项目"})
    project_b = web.sales_workspace.create_opportunity(
        OWNER, unit["id"], {"name": "B密钥项目"})
    for project, role in ((project_a, "technical_reviewer"),
                          (project_b, "business_owner")):
        current = web.sales_workspace.opportunities(OWNER, unit["id"])["items"]
        current = next(item for item in current if item["id"] == project["id"])
        web.sales_workspace.upsert_stakeholder(
            OWNER, unit["id"], project["id"],
            {"contact_id": person["id"], "expected_revision": current["revision"],
             "roles": [role]})
    yield SimpleNamespace(crm=crm, store=store, web=web, clock=clock, model=model,
                          unit=unit, person=person, a=project_a, b=project_b)
    crm.close()
    store.close()


def scope(w, project=None):
    return {"contact_id": w.person["id"],
            **({"opportunity_id": project["id"]} if project else {})}


def capture(w, request_id, text, *, project=None, kind="communication",
            relation=None, occurred_at=None):
    values = {"request_id": request_id, "text": text, "kind": kind,
              "occurred_at": occurred_at}
    if relation is not None:
        values["contact_relations"] = [{"contact_id": w.person["id"],
                                        "relation": relation}]
    return w.web.timeline.create_record(OWNER, scope(w, project), values)


def thread(w, project=None, **extra):
    return w.web.discussions.create_thread(
        OWNER, {"customer_id": w.unit["id"], "contact_id": w.person["id"],
                **({"opportunity_id": project["id"]} if project else {}),
                **extra})["thread"]


def read_only(w, callback):
    changes = w.crm._db.total_changes
    counts = {table: w.crm._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
              for table in ("crm_records", "tasks", "proposals", "notifications",
                            "crm_sales_discussion_messages", "crm_timeline_contexts")}
    try:
        return callback()
    finally:
        assert w.crm._db.total_changes == changes
        assert counts == {table: w.crm._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                          for table in counts}
        assert w.model.calls == 0


def detail(w, record_id):
    # Actual WebCRM.record handler, intentionally direct: no HTTP claim.
    response = asyncio.run(w.web.record(SimpleNamespace(match_info={"id": str(record_id)})))
    assert response.status == 200
    return json.loads(response.body)


@pytest.mark.parametrize("initial,current,category", [
    ("reflection", "communication", "visit_review"),
    ("communication", "reflection", "conversation"),
])
def test_record_detail_uses_current_timeline_nature_after_context_flip(world, initial, current, category):
    w = world
    saved = capture(w, "nature-flip", "原话保持：这次沟通与我的想法", kind=initial)
    event = saved["event"]
    assert saved["record"]["category"] == category
    w.clock[0] += 10
    changed = w.web.timeline.save_context(
        OWNER, event["key"], {"expected_revision": event["revision"], "kind": current,
                             "contact_relations": [{"contact_id": w.person["id"],
                                                    "relation": "about" if current == "reflection" else "direct"}]})
    result = read_only(w, lambda: detail(w, saved["record"]["id"]))
    assert result["record_nature"] == {"kind": current, "needs_review": False}
    assert result["record"]["category"] == category
    assert result["record"]["original_content"] == saved["record"]["original_content"]
    assert changed["occurred_at"] is None


def test_record_nature_reads_stale_flag_without_reconfirming_or_moving_time(world):
    w = world
    saved = capture(w, "stale-nature", "我认为先核对接口", kind="reflection",
                    occurred_at=NOW - 86400)
    w.crm.update_record(OWNER, saved["record"]["id"], {"content": "我补充了新的判断"}, NOW + 1)
    result = read_only(w, lambda: detail(w, saved["record"]["id"]))
    assert result["record_nature"] == {"kind": "reflection", "needs_review": True}
    event = w.web.timeline.get_event(OWNER, saved["event"]["key"])
    assert event["occurred_at"] == NOW - 86400
    assert event["contact_relations"][0]["valid"] is False


def test_action_detail_does_not_present_legacy_category_as_record_nature(world):
    w = world
    record = w.crm.create_record(OWNER, {"title": "明确待办", "content": "准备清单",
        "kind": "action", "category": "visit_review", "customer_id": w.unit["id"]}, NOW)
    result = read_only(w, lambda: detail(w, record["id"]))
    assert result["record"]["kind"] == "action"
    assert "record_nature" not in result


def test_direct_record_handler_denies_other_owners_record(world):
    w = world
    unit = w.crm.create_customer("other", {"name": "其他人私有单位"}, NOW)
    record = w.crm.create_record("other", {"title": "不可见资料", "customer_id": unit["id"]}, NOW)
    with pytest.raises(KeyError):
        read_only(w, lambda: detail(w, record["id"]))


def test_focus_summary_separates_same_person_projects_and_about_from_direct(world):
    w = world
    direct_a = capture(w, "A-direct", "A真实交流：林工确认数据库边界", project=w.a,
                       occurred_at=NOW - 7200)
    w.clock[0] += 1
    capture(w, "A-about", "别人提及林工可能关注A验收", project=w.a, relation="about",
            occurred_at=NOW - 3600)
    w.clock[0] += 1
    capture(w, "B-thought", "我的B思考：先提供密钥边界", project=w.b, kind="reflection")
    capture(w, "B-mentioned", "他人提及B负责人的偏好", project=w.b, relation="about")
    a, b = thread(w, w.a), thread(w, w.b)
    result_a = read_only(w, lambda: w.web.discussions.get_thread(OWNER, a["id"]))
    result_b = read_only(w, lambda: w.web.discussions.get_thread(OWNER, b["id"]))
    assert result_a["focus_summary"]["opportunity_id"] == w.a["id"]
    assert result_a["focus_summary"]["contact_id"] == w.person["id"]
    assert result_a["focus_summary"]["latest_communication"]["key"] == direct_a["event"]["key"]
    assert result_b["focus_summary"]["opportunity_id"] == w.b["id"]
    assert result_b["focus_summary"]["latest_communication"] is None
    assert "A真实交流" not in json.dumps(result_b["focus_summary"], ensure_ascii=False)


def test_focus_latest_communication_does_not_invent_unknown_occurrence_time(world):
    w = world
    saved = capture(w, "unknown-occurrence", "确实与林工交流，但未记发生时间", project=w.a)
    t = thread(w, w.a)
    result = read_only(w, lambda: w.web.discussions.get_thread(OWNER, t["id"]))
    latest = result["focus_summary"]["latest_communication"]
    assert latest["key"] == saved["event"]["key"]
    assert latest["occurred_at"] is None
    assert latest["recorded_at"] == NOW


def test_selected_old_evidence_does_not_freeze_latest_factual_communication(world):
    w = world
    old = capture(w, "older", "旧的A交流", project=w.a, occurred_at=NOW - 86400)
    w.clock[0] += 2
    newest = capture(w, "newer", "新的A实际交流", project=w.a, occurred_at=NOW - 3600)
    t = thread(w, w.a, timeline_event_keys=[old["event"]["key"]])
    result = read_only(w, lambda: w.web.discussions.get_thread(OWNER, t["id"]))
    assert result["focus_summary"]["latest_communication"]["key"] == newest["event"]["key"]


def test_changed_source_is_not_advertised_as_current_actual_communication(world):
    w = world
    saved = capture(w, "will-change", "A已核对交流", project=w.a)
    t = thread(w, w.a)
    assert w.web.discussions.get_thread(OWNER, t["id"])["focus_summary"]["latest_communication"]
    w.crm.update_record(OWNER, saved["record"]["id"], {"content": "新的原文尚未核对"}, NOW + 1)
    result = read_only(w, lambda: w.web.discussions.get_thread(OWNER, t["id"]))
    assert result["focus_summary"]["latest_communication"] is None


@pytest.mark.parametrize("invalid", ["person", "membership", "project"])
def test_invalid_or_archived_focus_does_not_publish_actual_communication(world, invalid):
    w = world
    capture(w, "before-archive", "归档前A确实交流", project=w.a)
    t = thread(w, w.a)
    if invalid == "person":
        w.crm.update_contact(OWNER, w.unit["id"], w.person["id"], {"archived": True}, NOW + 1)
    else:
        p = next(p for p in w.web.sales_workspace.opportunities(OWNER, w.unit["id"])["items"] if p["id"] == w.a["id"])
        if invalid == "membership":
            w.web.sales_workspace.archive_stakeholder(OWNER, w.unit["id"], w.a["id"], w.person["id"],
                {"expected_revision": p["revision"], "archived": True})
        else:
            w.web.sales_workspace.update_opportunity(OWNER, w.unit["id"], w.a["id"],
                {"expected_revision": p["revision"], "archived": True})
    result = read_only(w, lambda: w.web.discussions.get_thread(OWNER, t["id"]))
    assert result["focus_summary"] is None or result["focus_summary"]["latest_communication"] is None


def test_foreign_owner_cannot_read_focus_summary(world):
    w = world
    capture(w, "private-direct", "私有真实交流", project=w.a)
    t = thread(w, w.a)
    with pytest.raises(KeyError):
        read_only(w, lambda: w.web.discussions.get_thread("other", t["id"]))


def test_unit_discussion_without_contact_does_not_forge_personal_focus(world):
    w = world
    capture(w, "unit-general", "单位交流并不等于每个人参会", project=w.a)
    t = w.web.discussions.create_thread(OWNER, {"customer_id": w.unit["id"],
        "opportunity_id": w.a["id"], "timeline_event_keys": []})["thread"]
    result = read_only(w, lambda: w.web.discussions.get_thread(OWNER, t["id"]))
    assert result["focus_summary"] is None
