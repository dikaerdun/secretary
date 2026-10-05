"""Recording identity and current/history projection on fresh, offline SQLite."""
import asyncio

import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.materials import MaterialService
from secretary.sales_workspace import SalesWorkspace
from secretary.visits import VisitService


NOW = 1791079200.0
ACTUAL = 1791010800.0
OWNER = "alice"


class Connector:
    def __init__(self):
        self.nid, self.text, self.calls = "A", "录音 A 的原文。", 0

    async def fetch(self, title):
        self.calls += 1
        return {"nid": self.nid, "title": title, "create_time": "2026-10-04T10:00:00",
                "source": "OPTIMIZED", "raw_content": self.text, "text": self.text,
                "segments": [], "summary_content": "", "todo_content": "",
                "content_type": "text", "warnings": []}


class Organizer:
    async def organize(self, text, now, context):
        return {"summary": "合成整理摘要：" + text, "key_points": [], "open_questions": [], "actions": []}


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / "fresh-material-projection.sqlite3")
    clock = [NOW]
    connector = Connector()
    materials = MaterialService(crm, asyncio.Lock(), connector=connector,
                                organizer=Organizer(), clock=lambda: clock[0])
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    visits = VisitService(crm, materials, materials.lock)
    timeline = TimelineService(crm, sales, visits=visits, clock=lambda: clock[0])
    unit = crm.create_customer(OWNER, {"name": "合成 A 单位"}, NOW)
    yield {"crm": crm, "clock": clock, "connector": connector, "materials": materials,
           "sales": sales, "visits": visits, "timeline": timeline, "unit": unit}
    crm.close()


def capture(w, **values):
    payload = {"provider": "manual", "category": "conversation",
        "title": "原始录音标题", "text": "密药轮换原始识别错字。", "occurred_at": ACTUAL,
        "customer_id": w["unit"]["id"], **values}
    if payload["provider"] != "manual":
        payload.pop("text")
    row = w["materials"].enqueue(OWNER, payload)
    assert asyncio.run(w["materials"].process_one())
    return w["materials"].detail(OWNER, row["id"])


def revise(w, old, **values):
    w["clock"][0] += 60
    w["materials"].update(OWNER, old["material"]["id"], {"revision": old["material"]["revision"],
        "text": "密钥轮换由用户核实修正。", **values})
    assert asyncio.run(w["materials"].process_one())
    return w["materials"].detail(OWNER, old["material"]["id"])


def context(w, key, **values):
    event = w["timeline"].get_event(OWNER, key)
    return w["timeline"].save_context(OWNER, key, {"expected_revision": event["revision"], **values})


def view(w, **scope):
    return w["timeline"].view(OWNER, {"customer_id": w["unit"]["id"], **scope})


def counts(w):
    return tuple(w["crm"]._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in
                 ("crm_materials", "crm_material_versions", "crm_records", "tasks", "proposals", "notifications"))


@pytest.mark.parametrize("target_already_imported", [False, True])
def test_changed_title_identity_fails_without_redirect_or_new_business_rows(world, target_already_imported):
    w = world
    old = capture(w, provider="listen_note", title="重复完整标题", occurred_at=None)
    connector = w["connector"]
    connector.nid, connector.text = "B", "录音 B 的原文。"
    if target_already_imported:
        unit_b = w["crm"].create_customer(OWNER, {"name": "合成 B 单位"}, NOW)
        other = capture(w, provider="listen_note", title="另一原标题", customer_id=unit_b["id"], occurred_at=None)
    before = counts(w)
    w["materials"].retry(OWNER, old["material"]["id"], old["material"]["revision"])
    assert asyncio.run(w["materials"].process_one())
    after = w["materials"].detail(OWNER, old["material"]["id"])
    assert after["material"]["id"] == old["material"]["id"]
    assert after["material"]["status"] == "failed"
    assert after["material"]["customer_id"] == w["unit"]["id"]
    assert after["text"] == old["text"] and counts(w) == before
    assert w["materials"].get_version(OWNER, old["material"]["id"], old["original_version"]["id"])["text"] == old["text"]
    assert {x["id"] for x in w["materials"].list(OWNER)["items"]} == (
        {old["material"]["id"], other["material"]["id"]} if target_already_imported else {old["material"]["id"]})


def test_new_alias_of_same_recording_replays_original_without_duplicate_version(world):
    w = world
    old = capture(w, provider="listen_note", title="原标题", occurred_at=None)
    alias = capture(w, provider="listen_note", title="同一录音的新别名", occurred_at=None)
    assert alias["material"]["id"] == old["material"]["id"]
    assert alias["text"] == old["text"] and len(alias["versions"]) == 1
    assert w["materials"].list(OWNER)["total"] == 1


def test_same_nid_refresh_creates_version_and_keeps_historical_original(world):
    w = world
    old = capture(w, provider="listen_note", occurred_at=None)
    w["connector"].text = "同一录音的新转写。"
    w["materials"].retry(OWNER, old["material"]["id"], old["material"]["revision"])
    assert asyncio.run(w["materials"].process_one())
    current = w["materials"].detail(OWNER, old["material"]["id"])
    assert current["text"] == w["connector"].text and len(current["versions"]) == 2
    assert current["original_version"]["text"] == old["text"]
    assert view(w)["total"] == 1


def test_retry_cannot_fetch_over_explicit_local_correction(world):
    w = world
    old = capture(w, provider="listen_note", occurred_at=None)
    current = revise(w, old)
    w["connector"].nid, w["connector"].text = "B", "外部已变更原文。"
    w["materials"].retry(OWNER, current["material"]["id"], current["material"]["revision"])
    assert asyncio.run(w["materials"].process_one())
    result = w["materials"].detail(OWNER, old["material"]["id"])
    assert result["text"] == current["text"] and result["source"] == "LOCAL_REVISION"
    assert w["connector"].calls == 1 and result["original_version"]["text"] == old["text"]


def test_current_material_is_single_latest_card_with_current_values_and_read_only_projection(world):
    w = world
    old = capture(w)
    current = revise(w, old, title="已核实的当前标题")
    before = w["crm"]._db.total_changes
    result = view(w)
    event = result["items"][0]
    assert result["total"] == 1 and event["key"] == "material:" + str(current["material"]["id"])
    assert event["title"] == current["material"]["title"] and event["text"] == current["text"]
    assert event["occurred_at"] == ACTUAL and event["recorded_at"] == NOW
    assert result["summary"]["latest_communication"]["key"] == event["key"]
    history = w["timeline"].history_context(OWNER, {"customer_id": w["unit"]["id"]})
    assert [x["key"] for x in history["events"]] == [event["key"]]
    assert w["crm"]._db.total_changes == before


def test_revised_material_and_generated_notes_remain_one_explicit_visit(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    visit = w["visits"].create(OWNER, {"title": "已核实的实际交流", "customer_id": w["unit"]["id"], "occurred_at": ACTUAL})
    w["visits"].add_material(OWNER, visit["id"], {"role": "recording", "material_id": current["material"]["id"]})
    result = view(w)
    assert result["total"] == 1 and result["items"][0]["key"] == "visit:" + str(visit["id"])
    assert current["text"] in result["items"][0]["text"] and old["text"] not in result["items"][0]["text"]
    assert result["summary"]["latest_communication"]["key"] == result["items"][0]["key"]


def test_unknown_material_occurrence_stays_unknown_beside_known_visit(world):
    w = world
    visit = w["visits"].create(OWNER, {"title": "发生时间已知的交流", "customer_id": w["unit"]["id"], "occurred_at": ACTUAL})
    row = w["visits"].add_material(OWNER, visit["id"], {"role": "recap", "provider": "manual",
        "title": "时间未知的观察", "text": "材料未说明日期。", "occurred_at": None,
        "confirm_time_difference": True})["material"]
    assert asyncio.run(w["materials"].process_one())
    key = "material:" + str(row["id"])
    material = w["materials"].detail(OWNER, row["id"])
    source = w["timeline"].get_event(OWNER, key)
    separated = context(w, key, separate_event=True, kind="reflection")
    assert material["material"]["occurred_at"] is None and source["occurred_at"] is None and separated["occurred_at"] is None
    assert separated["recorded_at"] == NOW
    assert w["timeline"].get_event(OWNER, "visit:" + str(visit["id"]))["occurred_at"] == ACTUAL


def test_metadata_revision_is_not_transcript_version(world):
    w = world
    old = capture(w)
    current = revise(w, old, title="只修改标题", text=old["text"])
    assert current["material"]["revision"] == 2 and len(current["versions"]) == 1
    record = w["crm"].get_record(OWNER, current["material"]["record_id"])
    assert "转写版本 1" in record["content"]
    assert view(w)["items"][0]["title"] == "只修改标题"


def test_historical_generated_note_and_transcript_remain_reachable_and_immutable(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    record = w["crm"].get_record(OWNER, old["material"]["record_id"])
    assert "转写版本 1" in record["content"] and old["text"] in record["content"]
    version = w["materials"].get_version(OWNER, current["material"]["id"], old["original_version"]["id"])
    assert version["text"] == old["text"]
    assert w["materials"].source_for_record(OWNER, record["id"])["id"] == current["material"]["id"]
    historical = w["timeline"].get_event(OWNER, "record:" + str(record["id"]))
    assert historical["historical_source"] == {"type": "material", "id": current["material"]["id"], "label": "材料历史整理记录"}
    assert ("record", record["id"]) not in {(x["type"], x["id"]) for x in view(w)["items"][0]["source_refs"]}


def test_explicit_separated_history_keeps_user_kind_people_and_time_with_history_label(world):
    w = world
    person = w["crm"].create_contact(OWNER, w["unit"]["id"], {"name": "历史参与者"}, NOW)
    old = capture(w)
    key = "record:" + str(old["material"]["record_id"])
    context(w, key, separate_event=True, kind="reflection", occurred_at=ACTUAL,
            contact_relations=[{"contact_id": person["id"], "relation": "about"}])
    revise(w, old)
    result = view(w)
    separated = next(x for x in result["items"] if x["key"] == key)
    assert result["total"] == 2 and separated["separate_event"] and separated["kind"] == "reflection"
    assert separated["occurred_at"] == ACTUAL and separated["contact_relations"][0]["contact_id"] == person["id"]
    assert separated["historical_source"]["label"] == "材料历史整理记录"
    assert not next(x for x in result["items"] if x["entity_type"] == "material")["contact_relations"]


@pytest.mark.parametrize("with_visit", [False, True])
def test_old_project_and_people_context_cannot_pollute_current_material_or_visit(world, with_visit):
    w = world
    sales, timeline = w["sales"], w["timeline"]
    project_a = sales.create_opportunity(OWNER, w["unit"]["id"], {"name": "旧项目 A"})
    project_b = sales.create_opportunity(OWNER, w["unit"]["id"], {"name": "当前项目 B"})
    person = w["crm"].create_contact(OWNER, w["unit"]["id"], {"name": "旧版本直接参与人"}, NOW)
    old = capture(w)
    old_key = "record:" + str(old["material"]["record_id"])
    sales.link(OWNER, "record", old["material"]["record_id"], project_a["id"])
    context(w, old_key, contact_relations=[{"contact_id": person["id"], "relation": "direct"}])
    current = revise(w, old)
    current_key = "material:" + str(current["material"]["id"])
    sales.link(OWNER, "material", current["material"]["id"], project_b["id"])
    if with_visit:
        visit = w["visits"].create(OWNER, {"title": "当前项目交流", "customer_id": w["unit"]["id"], "occurred_at": ACTUAL})
        w["visits"].add_material(OWNER, visit["id"], {"role": "recording", "material_id": current["material"]["id"]})
        current_key = "visit:" + str(visit["id"])
    event = timeline.get_event(OWNER, current_key)
    assert event["opportunity_id"] == project_b["id"]
    assert event["contact_relations"] == [] and not event["needs_review"]
    assert not view(w, opportunity_id=project_a["id"])["items"]
    assert view(w, opportunity_id=project_b["id"])["total"] == 1
    assert timeline.view(OWNER, {"contact_id": person["id"]})["items"] == []
    old_source = timeline.get_event(OWNER, old_key)
    assert old_source["contact_relations"][0]["contact_id"] == person["id"]
    assert old_source["historical_source"]["id"] == current["material"]["id"]
    assert ("record", old["material"]["record_id"]) not in {(x["type"], x["id"]) for x in event["source_refs"]}


def test_independent_note_with_lookalike_material_version_text_is_not_grouped(world):
    w = world
    old = capture(w)
    record = w["crm"].create_record(OWNER, {"title": "用户自己的独立记录", "content": "材料 #1 · 转写版本 1。个人观察。",
        "customer_id": w["unit"]["id"]}, NOW)
    result = view(w)
    assert result["total"] == 2 and any(x["key"] == "record:" + str(record["id"]) for x in result["items"])
    assert not w["timeline"].get_event(OWNER, "record:" + str(record["id"])).get("historical_source")
    with pytest.raises(KeyError):
        w["timeline"].get_event("bob", "material:" + str(old["material"]["id"]))


def test_old_project_is_not_inferred_for_new_unlinked_material_version(world):
    w = world
    project = w["sales"].create_opportunity(OWNER, w["unit"]["id"], {"name": "旧版本项目"})
    old = capture(w)
    w["sales"].link(OWNER, "record", old["material"]["record_id"], project["id"])
    current = revise(w, old)
    event = w["timeline"].get_event(OWNER, "material:" + str(current["material"]["id"]))
    assert event["opportunity_id"] is None and event["opportunity_name"] is None
    assert view(w, opportunity_id=project["id"])["total"] == 0


@pytest.mark.parametrize("with_visit", [False, True])
def test_stale_historical_note_context_does_not_mark_current_source_for_review(world, with_visit):
    w = world
    person = w["crm"].create_contact(OWNER, w["unit"]["id"], {"name": "旧核对人物"}, NOW)
    old = capture(w)
    key = "record:" + str(old["material"]["record_id"])
    context(w, key, contact_relations=[{"contact_id": person["id"], "relation": "about"}])
    current = revise(w, old)
    w["crm"].update_record(OWNER, old["material"]["record_id"], {"title": "仅修正历史记录标题"}, w["clock"][0])
    assert w["timeline"].get_event(OWNER, key)["needs_review"]
    current_key = "material:" + str(current["material"]["id"])
    if with_visit:
        visit = w["visits"].create(OWNER, {"title": "当前材料交流", "customer_id": w["unit"]["id"], "occurred_at": ACTUAL})
        w["visits"].add_material(OWNER, visit["id"], {"role": "recording", "material_id": current["material"]["id"]})
        current_key = "visit:" + str(visit["id"])
    event = w["timeline"].get_event(OWNER, current_key)
    assert event["contact_relations"] == [] and not event["needs_review"]
    assert view(w)["summary"]["latest_communication"]["key"] == current_key


def test_current_generated_record_explicit_context_remains_compatible(world):
    w = world
    person = w["crm"].create_contact(OWNER, w["unit"]["id"], {"name": "本次已核对人物"}, NOW)
    old = capture(w)
    context(w, "record:" + str(old["material"]["record_id"]),
            contact_relations=[{"contact_id": person["id"], "relation": "direct"}])
    event = view(w)["items"][0]
    assert event["contact_relations"][0]["contact_id"] == person["id"] and not event["needs_review"]
    assert w["timeline"].view(OWNER, {"contact_id": person["id"]})["total"] == 1


def test_explicit_separated_historical_communication_is_labeled_and_not_latest_actual_exchange(world):
    w = world
    old = capture(w)
    key = "record:" + str(old["material"]["record_id"])
    context(w, key, separate_event=True, kind="communication")
    current = revise(w, old)
    result = view(w)
    historical = next(x for x in result["items"] if x["key"] == key)
    assert historical["kind"] == "communication" and historical["separate_event"]
    assert historical["historical_source"]["label"] == "材料历史整理记录"
    assert result["summary"]["latest_communication"]["key"] == "material:" + str(current["material"]["id"])
