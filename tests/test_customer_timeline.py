"""Owner-isolated original-source timelines on fresh, offline SQLite."""
import asyncio
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


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / "timeline.sqlite3")
    clock = [NOW]
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: clock[0])
    network = AccountNetwork(crm, workspace, clock=lambda: clock[0])
    materials = MaterialService(crm, lock, clock=lambda: clock[0])
    visits = VisitService(crm, materials, lock)
    exchange = ExchangeRecords(crm, visits, lambda: clock[0])
    discussions = DiscussionService(crm, workspace, lock, clock=lambda: clock[0])
    timeline = TimelineService(crm, workspace, visits=visits, discussions=discussions, clock=lambda: clock[0])
    unit = network.create_unit("me", {"name": "合成银行"})
    person = crm.create_contact("me", unit["id"], {"name": "王工", "department": "信息中心", "role": "主管"}, NOW)
    same = crm.create_contact("me", unit["id"], {"name": "王工", "department": "采购中心"}, NOW)
    project = workspace.create_opportunity("me", unit["id"], {"name": "密码改造"})
    yield {"crm": crm, "clock": clock, "workspace": workspace, "network": network, "materials": materials,
           "visits": visits, "exchange": exchange, "discussions": discussions, "timeline": timeline,
           "unit": unit, "person": person, "same": same, "project": project}
    crm.close()


def note(w, text="讨论密码设备", **values):
    return w["crm"].create_record("me", {"title": text[:120], "content": text, "customer_id": w["unit"]["id"], **values}, w["clock"][0])


def confirm(w, key, **values):
    timeline = w["timeline"]
    event = timeline.get_event("me", key)
    return timeline.save_context("me", key, {"expected_revision": event["revision"], **values})


def join(w, person=None, project=None):
    person, project = person or w["person"], project or w["project"]
    current = next(item for item in w["workspace"].opportunities("me", project["customer_id"])["items"] if item["id"] == project["id"])
    return w["workspace"].upsert_stakeholder("me", project["customer_id"], project["id"], {"contact_id": person["id"], "expected_revision": current["revision"], "roles": ["technical_reviewer"]})


def no_scheduling(w):
    db = w["crm"]._db
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_person_default_explicit_direct_and_about_without_project_attendance_guess(world):
    w, timeline = world, world["timeline"]
    join(w)
    unrelated = note(w, "项目会议未写参会人")
    w["workspace"].link("me", "record", unrelated["id"], w["project"]["id"])
    mention = note(w, "王工可能关心集成效率")
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["items"] == []
    possible = timeline.view("me", {"contact_id": w["person"]["id"]})["possible_related"]
    assert [item["key"] for item in possible] == ["record:" + str(mention["id"])]
    assert {item["department"] for item in possible[0]["candidate_contacts"]} == {"信息中心", "采购中心"}
    confirm(w, "record:" + str(mention["id"]), kind="reflection", contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    direct = timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "d1", "text": "我和王工核对设备场景", "kind": "communication", "occurred_at": NOW - 3600})
    view = timeline.view("me", {"contact_id": w["person"]["id"]})
    assert view["total"] == 2
    assert view["summary"]["latest_communication"]["key"] == direct["event"]["key"]
    assert timeline.view("me", {"contact_id": w["same"]["id"]})["total"] == 0
    assert unrelated["id"] not in {item["entity_id"] for item in view["items"]}
    assert {item["kind"] for item in timeline.history_context("me", {"contact_id": w["person"]["id"]})["events"]} == {"communication", "reflection"}
    no_scheduling(w)


def test_unit_scope_is_self_not_administrative_descendants(world):
    w, timeline = world, world["timeline"]
    child = w["network"].create_unit("me", {"name": "分行"}, {"parent_customer_id": w["unit"]["id"]})
    note(w, "总行讨论")
    note(w, "分行讨论", customer_id=child["id"])
    assert [item["title"] for item in timeline.view("me", {"customer_id": w["unit"]["id"]})["items"]] == ["总行讨论"]
    assert [item["title"] for item in timeline.view("me", {"customer_id": child["id"]})["items"]] == ["分行讨论"]


def test_latest_person_communication_requires_direct_contact_not_about_mention(world):
    w, timeline = world, world["timeline"]
    about = note(w, "李总说王工可能关注设备兼容")
    confirm(w, "record:" + str(about["id"]), contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["total"] == 1
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["summary"]["latest_communication"] is None
    direct = timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "direct", "text": "我和王工直接核对技术参数", "kind": "communication"})
    w["clock"][0] += 100
    about2 = note(w, "李总再谈王工的材料偏好")
    confirm(w, "record:" + str(about2["id"]), contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["summary"]["latest_communication"]["key"] == direct["event"]["key"]


def test_one_visit_recording_transcript_note_dedup_and_separate_next_day_recap(world):
    w, timeline = world, world["timeline"]
    visit = w["visits"].create("me", {"title": "昨天现场交流", "customer_id": w["unit"]["id"], "occurred_at": NOW - 86400})
    recording = w["visits"].add_material("me", visit["id"], {"role": "recording", "provider": "manual", "title": "现场录音", "text": "王工说设备须兼容旧系统"})["material"]
    recap = w["visits"].add_material("me", visit["id"], {"role": "recap", "provider": "manual", "title": "我的隔天复盘", "text": "我认为应先安排测试"})["material"]
    # A generated original recording record remains one durable source, not a
    # second customer conversation or one item per transcript version.
    raw = note(w, "王工说设备须兼容旧系统")
    with w["crm"]._transaction() as db:
        db.execute("UPDATE crm_materials SET record_id=? WHERE owner=? AND id=?", (raw["id"], "me", recording["id"]))
    view = timeline.view("me", {"customer_id": w["unit"]["id"]})
    assert view["total"] == 1 and view["items"][0]["key"] == "visit:" + str(visit["id"])
    assert {ref["type"] for ref in view["items"][0]["source_refs"]} == {"visit", "material", "record"}
    source = timeline.get_event("me", "material:" + str(recap["id"]))
    assert source["key"] == "material:" + str(recap["id"]) and source["merged_into"] == "visit:" + str(visit["id"])
    independent = confirm(w, source["key"], separate_event=True, kind="reflection", occurred_at=NOW,
                          related_event_key="visit:" + str(visit["id"]), contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    assert independent["occurred_at"] == NOW and "merged_into" not in independent
    view = timeline.view("me", {"customer_id": w["unit"]["id"]})
    assert view["total"] == 2 and view["items"][0]["key"] == source["key"]
    meeting = next(item for item in view["items"] if item["entity_type"] == "visit")
    assert meeting["occurred_at"] == NOW - 86400
    assert "我认为应先安排测试" not in meeting["text"]
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["items"][0]["key"] == source["key"]
    no_scheduling(w)


def test_multi_people_event_direct_vs_about_and_group_person_cross_unit_project(world):
    w, timeline = world, world["timeline"]
    group = w["network"].create_unit("me", {"name": "集团"}, {"unit_type": "group"})
    w["network"].update("me", w["unit"]["id"], {"expected_revision": 1, "parent_customer_id": group["id"]})
    procurement = w["crm"].create_contact("me", group["id"], {"name": "集团采购人", "department": "采购"}, NOW)
    join(w, person=procurement)
    created = timeline.create_record("me", {"contact_id": procurement["id"], "opportunity_id": w["project"]["id"]},
                                    {"request_id": "cross", "text": "与集团采购人交流，技术王工没有出席但建议先了解他的关注", "kind": "communication", "occurred_at": None,
                                     "contact_relations": [{"contact_id": procurement["id"], "relation": "direct"}, {"contact_id": w["person"]["id"], "relation": "about"}]})
    assert created["record"]["customer_id"] == w["unit"]["id"]
    view = timeline.view("me", {"contact_id": procurement["id"], "opportunity_id": w["project"]["id"]})
    assert view["scope"]["customer_id"] == group["id"] and view["projects"][0]["customer_id"] == w["unit"]["id"]
    assert view["items"][0]["customer_id"] == w["unit"]["id"]
    assert next(item for item in timeline.view("me", {"contact_id": w["person"]["id"]})["items"][0]["contact_relations"] if item["contact_id"] == w["person"]["id"])["relation"] == "about"
    assert timeline.view("me", {"customer_id": group["id"]})["total"] == 0
    second = w["workspace"].create_opportunity("me", w["unit"]["id"], {"name": "另一项目"})
    with pytest.raises(ValueError, match="未明确参与"):
        timeline.create_record("me", {"contact_id": procurement["id"], "opportunity_id": second["id"]}, {"request_id": "bad", "text": "不应误写", "kind": "reflection"})
    no_scheduling(w)


def test_source_cas_stale_scope_reconfirmation_and_noop_history(world):
    w, timeline = world, world["timeline"]
    record = note(w, "王工说先测试")
    key = "record:" + str(record["id"])
    confirmed = confirm(w, key, contact_relations=[{"contact_id": w["person"]["id"], "relation": "direct"}], occurred_at=NOW - 120)
    unchanged = timeline.save_context("me", key, {"expected_revision": confirmed["revision"], "occurred_at": NOW - 120})
    assert unchanged == confirmed
    w["clock"][0] += 400
    w["crm"].update_record("me", record["id"], {"content": "王工说延期测试"}, w["clock"][0])
    with pytest.raises(TimelineConflict):
        timeline.save_context("me", key, {"expected_revision": confirmed["revision"], "occurred_at": None})
    stale = timeline.get_event("me", key)
    assert stale["needs_review"] and stale["occurred_at"] == NOW - 120
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["items"] == []
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["possible_related"][0]["key"] == key
    assert timeline.history_context("me", {"contact_id": w["person"]["id"]})["events"] == []
    with pytest.raises(TimelineConflict):
        timeline.history_context("me", {"contact_id": w["person"]["id"]}, event_keys=[key])
    renewed = timeline.save_context("me", key, {"expected_revision": stale["revision"], "contact_relations": [{"contact_id": w["person"]["id"], "relation": "about"}]})
    assert not renewed["needs_review"] and len(renewed["context_history"]) == 2
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["total"] == 1
    no_scheduling(w)


def test_unknown_occurrence_recorded_date_and_edit_does_not_reorder_history(world):
    w, timeline = world, world["timeline"]
    first = timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "first", "text": "首次复盘", "kind": "reflection", "occurred_at": None})
    w["clock"][0] += 100
    second = timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "second", "text": "第二次复盘", "kind": "reflection", "occurred_at": None})
    w["clock"][0] += 100
    w["crm"].update_record("me", first["record"]["id"], {"title": "首次复盘补标题"}, w["clock"][0])
    confirm(w, first["event"]["key"], contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    view = timeline.view("me", {"contact_id": w["person"]["id"]})
    assert [item["key"] for item in view["items"]] == [second["event"]["key"], first["event"]["key"]]
    assert [item["recorded_at"] for item in view["items"]] == [NOW + 100, NOW]
    assert all(item["occurred_at"] is None for item in view["items"])
    no_scheduling(w)


def test_idempotent_exact_capture_atomic_validation_and_request_replay(world):
    w, timeline = world, world["timeline"]
    scope = {"contact_id": w["person"]["id"]}
    data = {"request_id": "req", "text": "  原话\n保留换行  ", "kind": "reflection", "occurred_at": None}
    first = timeline.create_record("me", scope, data)
    second = timeline.create_record("me", scope, data)
    assert first["created"] and not second["created"]
    assert first["record"]["content"] == first["record"]["original_content"] == data["text"]
    assert second["record"]["id"] == first["record"]["id"]
    with pytest.raises(TimelineConflict):
        timeline.create_record("me", scope, {**data, "text": "另一个内容"})
    count = w["crm"]._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0]
    with pytest.raises(KeyError):
        timeline.create_record("me", scope, {**data, "request_id": "bad", "related_event_key": "record:9999"})
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == count
    with w["crm"]._transaction() as db:
        db.execute("CREATE TRIGGER reject_timeline_context BEFORE INSERT ON crm_timeline_contexts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        timeline.create_record("me", scope, {**data, "request_id": "fail"})
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == count
    no_scheduling(w)


def test_followup_action_returns_to_person_and_true_result_deduplicates_generated_activity(world):
    w, timeline = world, world["timeline"]
    source = timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "source", "text": "与王工约定提供测试材料", "kind": "communication"})
    action = note(w, "提供测试材料", kind="action", parent_record_id=source["record"]["id"], status="following")
    # Called within a real outer adoption transaction: no nested BEGIN, and
    # rollback of an outer caller must also roll back the new relation.
    with w["crm"]._transaction():
        timeline.link_action("me", action["id"], {"contact_id": w["person"]["id"]}, source_event_key=source["event"]["key"])
    view = timeline.view("me", {"contact_id": w["person"]["id"]})
    assert view["total"] == 1 and view["summary"]["open_actions"][0]["id"] == action["id"]
    assert view["items"][0]["actions"][0]["id"] == action["id"]
    w["crm"].save_action_terms("me", action["id"], {"executor_kind": "customer", "executor_evidence": "王工"}, NOW, explicit=True)
    # save_action_terms uses its evidence contract, not a timeline-made task.
    completed = w["workspace"].complete_record("me", action["id"], {"request_id": "done", "result": "材料已收到", "next_step": "继续安排设备测试"})
    w["crm"].add_activity("me", action["id"], "人工进展：等待下一次测试", NOW)
    view = timeline.view("me", {"contact_id": w["person"]["id"]})
    results = [item for item in view["items"] if item["kind"] == "result"]
    assert len(results) == 2
    assert {item["entity_type"] for item in results} == {"outcome", "activity"}
    assert [item["id"] for item in view["summary"]["open_actions"]] == [completed["next_record"]["id"]]
    assert timeline.get_event("me", "record:" + str(completed["next_record"]["id"]))["related_event_key"] == "outcome:" + str(completed["outcome"]["id"])
    assert timeline.view("me", {"contact_id": w["same"]["id"]})["total"] == 0
    no_scheduling(w)


def test_next_action_does_not_inherit_person_after_original_action_source_changes(world):
    w, timeline = world, world["timeline"]
    action = note(w, "提供测试材料", kind="action", status="following")
    timeline.link_action("me", action["id"], {"contact_id": w["person"]["id"]})
    outcome = w["workspace"].complete_record("me", action["id"], {"request_id": "then", "result": "已交付", "next_step": "继续确认验收"})
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["summary"]["open_actions"][0]["id"] == outcome["next_record"]["id"]
    w["crm"].update_record("me", action["id"], {"content": "原事项明确归属于另一人"}, NOW + 1)
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["summary"]["open_actions"] == []
    assert timeline.history_context("me", {"contact_id": w["person"]["id"]})["waiting"] == []
    no_scheduling(w)


def test_action_link_outer_rollback_and_unrelated_owner_scope_rejected(world):
    w, timeline = world, world["timeline"]
    action = note(w, "整理材料", kind="action")
    with pytest.raises(RuntimeError):
        with w["crm"]._transaction():
            timeline.link_action("me", action["id"], {"contact_id": w["person"]["id"]})
            raise RuntimeError("synthetic caller rollback")
    assert timeline.get_event("me", "record:" + str(action["id"]))["contact_relations"] == []
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["summary"]["open_actions"] == []


def test_action_schedule_lifecycle_preserves_person_and_changes_current_ai_fingerprint(world):
    w, timeline = world, world["timeline"]
    action = note(w, "我提供测试材料", kind="action", status="following")
    timeline.link_action("me", action["id"], {"contact_id": w["person"]["id"]})
    scope = {"contact_id": w["person"]["id"]}
    before = timeline.history_context("me", scope)
    reply = w["crm"].execute("me", "schedule-lifecycle", {"action": "propose", "title": action["title"], "remind_at": NOW + 86400}, NOW)
    proposal_id = int(reply.split("P", 1)[1].split()[0].rstrip("：:，,"))
    w["crm"].link_proposal("me", action["id"], proposal_id, NOW)
    w["crm"].execute("me", "schedule-confirm", {"action": "confirm", "proposal_id": proposal_id}, NOW)
    after = timeline.history_context("me", scope)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["open_actions"][0]["id"] == action["id"]
    assert after["open_actions"][0]["task_status"] == "pending"
    assert not timeline.get_event("me", "record:" + str(action["id"]))["needs_review"]
    w["crm"].execute("me", "schedule-complete", {"action": "complete", "task_id": after["open_actions"][0]["task_id"]}, NOW + 1)
    assert timeline.history_context("me", scope)["fingerprint"] != after["fingerprint"]


@pytest.mark.parametrize("change", ["hide", "customer", "original", "content"])
def test_invalid_source_never_enters_fresh_person_history(world, change):
    w, timeline = world, world["timeline"]
    record = note(w, "王工已确认旧系统兼容")
    key = "record:" + str(record["id"])
    confirm(w, key, contact_relations=[{"contact_id": w["person"]["id"], "relation": "direct"}])
    if change == "hide":
        with w["crm"]._transaction() as db:
            db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("me", record["id"]))
    elif change == "customer":
        other = w["network"].create_unit("me", {"name": "另一单位"})
        w["crm"].update_record("me", record["id"], {"customer_id": other["id"]}, NOW + 1)
    elif change == "original":
        with w["crm"]._transaction() as db:
            db.execute("UPDATE crm_records SET original_content=? WHERE owner=? AND id=?", ("更正原始转写", "me", record["id"]))
    else:
        w["crm"].update_record("me", record["id"], {"content": "王工尚未确认兼容"}, NOW + 1)
    assert timeline.history_context("me", {"contact_id": w["person"]["id"]})["events"] == []
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["total"] == 0
    if change == "hide":
        with pytest.raises(KeyError):
            timeline.get_event("me", key)
    else:
        assert timeline.get_event("me", key)["needs_review"]
    no_scheduling(w)


@pytest.mark.parametrize("values", [
    {"kind": "unknown"}, {"occurred_at": True}, {"occurred_at": NOW + 86400},
    {"separate_event": "true"}, {"contact_relations": "everyone"},
    {"contact_relations": [{"contact_id": 1, "relation": "attended"}]},
    {"related_event_key": "record:0"}, {"unexpected": "field"},
])
def test_context_bad_input_has_no_history_write(world, values):
    w, timeline = world, world["timeline"]
    record = note(w)
    key = "record:" + str(record["id"])
    before = timeline.get_event("me", key)
    with pytest.raises((ValueError, KeyError)):
        timeline.save_context("me", key, {"expected_revision": before["revision"], **values})
    assert timeline.get_event("me", key) == before


def test_repeated_people_related_cycle_and_cross_owner_context_rejected(world):
    w, timeline = world, world["timeline"]
    first, second = note(w, "第一次"), note(w, "第二次")
    one, two = "record:" + str(first["id"]), "record:" + str(second["id"])
    with pytest.raises(ValueError, match="重复"):
        confirm(w, one, contact_relations=[{"contact_id": w["person"]["id"], "relation": "direct"}] * 2)
    confirm(w, one, related_event_key=two)
    with pytest.raises(ValueError, match="循环"):
        confirm(w, two, related_event_key=one)
    foreign = w["network"].create_unit("other", {"name": "不可见单位"})
    person = w["crm"].create_contact("other", foreign["id"], {"name": "不可见人"}, NOW)
    record = w["crm"].create_record("other", {"title": "秘密", "content": "不可见内容", "customer_id": foreign["id"]}, NOW)
    for operation in (lambda: timeline.view("me", {"contact_id": person["id"]}),
                      lambda: timeline.view("me", {"customer_id": foreign["id"]}),
                      lambda: timeline.get_event("me", "record:" + str(record["id"])),
                      lambda: confirm(w, two, contact_relations=[{"contact_id": person["id"], "relation": "about"}]),
                      lambda: confirm(w, two, related_event_key="record:" + str(record["id"]))):
        with pytest.raises(KeyError):
            operation()
    no_scheduling(w)


def test_search_filter_pagination_and_stable_context_without_clock(world):
    w, timeline = world, world["timeline"]
    for index in range(5):
        w["clock"][0] += 1
        timeline.create_record("me", {"contact_id": w["person"]["id"]},
                               {"request_id": "page" + str(index), "text": "兼容测试 " + str(index), "kind": "reflection" if index % 2 else "communication"})
    first = timeline.view("me", {"contact_id": w["person"]["id"]}, page=1, page_size=2)
    second = timeline.view("me", {"contact_id": w["person"]["id"]}, page=2, page_size=2)
    assert first["total"] == 5 and first["pages"] == 3 and len(second["items"]) == 2
    assert not {item["key"] for item in first["items"]} & {item["key"] for item in second["items"]}
    assert timeline.view("me", {"contact_id": w["person"]["id"]}, kind="reflection")["total"] == 2
    assert timeline.view("me", {"contact_id": w["person"]["id"]}, q="测试 3")["total"] == 1
    before = timeline.history_context("me", {"contact_id": w["person"]["id"]})
    w["clock"][0] += 86400
    assert timeline.history_context("me", {"contact_id": w["person"]["id"]}) == before


def test_archived_person_historical_records_readable_new_capture_rejected(world):
    w, timeline = world, world["timeline"]
    timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "old", "text": "以前沟通", "kind": "communication"})
    w["crm"].update_contact("me", w["unit"]["id"], w["person"]["id"], {"archived": True}, NOW + 1)
    assert timeline.view("me", {"contact_id": w["person"]["id"]})["total"] == 1
    with pytest.raises(ValueError, match="归档"):
        timeline.create_record("me", {"contact_id": w["person"]["id"]}, {"request_id": "new", "text": "不创建", "kind": "communication"})
    no_scheduling(w)


def test_discussion_actual_last_message_time_and_exclude_current_thread(world):
    w, timeline = world, world["timeline"]
    record = note(w, "客户明确关注兼容")
    confirm(w, "record:" + str(record["id"]), contact_relations=[{"contact_id": w["person"]["id"], "relation": "direct"}])
    thread = w["discussions"].create_thread("me", {"customer_id": w["unit"]["id"], "title": "讨论推进"})["thread"]
    key = "discussion:" + str(thread["id"])
    confirm(w, key, contact_relations=[{"contact_id": w["person"]["id"], "relation": "about"}])
    before = timeline.history_context("me", {"contact_id": w["person"]["id"]}, exclude_discussion_id=thread["id"])
    with w["crm"]._transaction() as db:
        db.execute("INSERT INTO crm_sales_discussion_messages(owner,thread_id,role,text,status,created_at,updated_at) VALUES (?,?,'user',?,'complete',?,?)",
                   ("me", thread["id"], "如何推进", NOW + 60, NOW + 60))
    assert timeline.get_event("me", key)["last_activity_at"] == NOW + 60
    assert timeline.history_context("me", {"contact_id": w["person"]["id"]}, exclude_discussion_id=thread["id"]) == before
    no_scheduling(w)


@pytest.mark.parametrize("original", [None, "", "\x00bad", "x" * 20001, 3])
def test_invalid_original_transcript_rolls_back_capture(world, original):
    w = world
    with pytest.raises(ValueError):
        w["timeline"].create_record("me", {"contact_id": w["person"]["id"]},
                                    {"request_id": "bad-asr", "text": "用户核对文字", "original_transcript": original, "kind": "communication"})
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 0
    no_scheduling(w)


def test_corrected_voice_preserves_original_transcript_and_idempotent_input(world):
    w, timeline = world, world["timeline"]
    values = {"request_id": "asr", "text": "正确客户说密码项目需先测试", "original_transcript": "错误客户说密码项目需先测式", "kind": "communication"}
    saved = timeline.create_record("me", {"contact_id": w["person"]["id"]}, values)
    assert saved["record"]["content"] == values["text"]
    assert saved["record"]["original_content"] == values["original_transcript"]
    assert saved["event"]["text"] == values["text"] and values["original_transcript"] not in saved["event"]["text"]
    assert timeline.create_record("me", {"contact_id": w["person"]["id"]}, values)["created"] is False
    with pytest.raises(TimelineConflict):
        timeline.create_record("me", {"contact_id": w["person"]["id"]}, {**values, "original_transcript": "另一个原始转写"})
    no_scheduling(w)


@pytest.mark.parametrize("relations", [None, {}, "everyone", 1])
def test_nonlist_capture_relations_rejected_without_partial_note(world, relations):
    w = world
    with pytest.raises(ValueError):
        w["timeline"].create_record("me", {"contact_id": w["person"]["id"]},
                                   {"request_id": "invalid-relations", "text": "不应部分保存", "kind": "communication", "contact_relations": relations})
    assert w["crm"]._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 0


def test_selected_history_includes_new_scope_events_and_full_revision_fingerprint(world):
    w, timeline = world, world["timeline"]
    scope = {"contact_id": w["person"]["id"]}
    old = timeline.create_record("me", scope, {"request_id": "selected", "text": "之前客户准备采购", "kind": "communication"})
    before = timeline.history_context("me", scope, event_keys=[old["event"]["key"]], limit=2)
    w["clock"][0] += 1
    paused = timeline.create_record("me", scope, {"request_id": "paused", "text": "最新客户说项目已暂停", "kind": "communication"})
    after = timeline.history_context("me", scope, event_keys=[old["event"]["key"]], limit=2)
    assert after["fingerprint"] != before["fingerprint"]
    assert {event["key"] for event in after["events"]} == {old["event"]["key"], paused["event"]["key"]}
    # Even when output limit is filled by chosen historical evidence, a newly
    # recorded event changes the complete-scope fingerprint.
    limited = timeline.history_context("me", scope, event_keys=[old["event"]["key"]], limit=1)
    assert limited["events"][0]["key"] == old["event"]["key"] and len(limited["scope_event_revisions"]) == 2
    w["crm"].update_record("me", paused["record"]["id"], {"content": "最新客户说项目取消"}, NOW + 2)
    assert timeline.history_context("me", scope, event_keys=[old["event"]["key"]], limit=1)["fingerprint"] != limited["fingerprint"]
