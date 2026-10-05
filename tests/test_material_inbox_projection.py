"""Read-only inbox projection preserves editable material history on fresh SQLite."""
import pytest

from test_material_identity_projection import NOW, OWNER, capture, context, revise, world


def test_revised_material_keeps_one_current_inbox_item_and_matching_counter(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    before = w["crm"]._db.total_changes
    inbox = w["crm"].list_records(OWNER, status="unfiled")
    assert inbox["total"] == 1 and [x["id"] for x in inbox["items"]] == [current["material"]["record_id"]]
    assert w["crm"].dashboard(OWNER, NOW)["stats"]["unfiled"] == inbox["total"]
    assert w["crm"].get_record(OWNER, old["material"]["record_id"])["status"] == "unfiled"
    assert w["materials"].get_version(OWNER, old["material"]["id"], old["original_version"]["id"])["text"] == old["text"]
    assert w["crm"]._db.total_changes == before


def test_history_folding_applies_before_filters_pagination_and_customer_totals(world):
    w = world
    old = capture(w)
    middle = revise(w, old, title="当前标题")
    current = revise(w, middle, text="第三版已核实原文。")
    own = w["crm"].create_record(OWNER, {"title": "独立个人补充", "content": "我自己的后续观察。",
        "customer_id": w["unit"]["id"]}, NOW)
    first = w["crm"].list_records(OWNER, status="unfiled", customer_id=w["unit"]["id"], page_size=1)
    second = w["crm"].list_records(OWNER, status="unfiled", customer_id=w["unit"]["id"], page_size=1, page=2)
    assert first["total"] == second["total"] == 2 and first["pages"] == second["pages"] == 2
    assert {x["id"] for page in (first, second) for x in page["items"]} == {current["material"]["record_id"], own["id"]}
    assert w["crm"].list_records(OWNER, status="unfiled", q="原始录音标题")["total"] == 0
    assert w["crm"].list_records(OWNER, kind="note", category="conversation")["total"] == 2
    assert w["crm"].dashboard(OWNER, NOW)["stats"]["unfiled"] == 2


def test_explicitly_edited_old_note_remains_available_for_inbox_review(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    w["crm"].update_record(OWNER, old["material"]["record_id"], {"content": "我明确修正旧记录，仍需整理。"}, NOW)
    inbox = w["crm"].list_records(OWNER, status="unfiled")
    assert {x["id"] for x in inbox["items"]} == {old["material"]["record_id"], current["material"]["record_id"]}
    assert w["crm"].dashboard(OWNER, NOW)["stats"]["unfiled"] == 2


def test_explicitly_separated_old_note_keeps_its_inbox_path_and_original_unknown_time(world):
    w = world
    old = capture(w, occurred_at=None)
    context(w, "record:" + str(old["material"]["record_id"]), separate_event=True, kind="reflection")
    current = revise(w, old)
    inbox = w["crm"].list_records(OWNER, status="unfiled")
    assert {x["id"] for x in inbox["items"]} == {old["material"]["record_id"], current["material"]["record_id"]}
    assert w["materials"].detail(OWNER, old["material"]["id"])["material"]["occurred_at"] is None
    assert w["timeline"].get_event(OWNER, "record:" + str(old["material"]["record_id"]))["occurred_at"] is None


@pytest.mark.parametrize("change", [{"title": "我已核对的新标题"}, {"content": "我已核对的新内容。"},
    {"category": "idea"}, {"status": "following"}, {"status": "done"}])
def test_explicit_old_record_edits_and_status_decisions_survive_default_projection(world, change):
    w = world
    old = capture(w)
    current = revise(w, old)
    record = w["crm"].update_record(OWNER, old["material"]["record_id"], change, NOW)
    result = w["crm"].list_records(OWNER)
    assert {x["id"] for x in result["items"]} == {record["id"], current["material"]["record_id"]}
    if change.get("status"):
        assert w["crm"].list_records(OWNER, status=change["status"])["items"][0]["id"] == record["id"]
        assert w["crm"].dashboard(OWNER, NOW)["stats"]["unfiled"] == 1
    else:
        assert w["crm"].dashboard(OWNER, NOW)["stats"]["unfiled"] == 2


def test_explicit_historical_project_link_is_a_preserved_user_decision(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    project = w["sales"].create_opportunity(OWNER, w["unit"]["id"], {"name": "旧来源核对项目"})
    w["sales"].link(OWNER, "record", old["material"]["record_id"], project["id"])
    assert {x["id"] for x in w["crm"].list_records(OWNER, status="unfiled")["items"]} == {
        old["material"]["record_id"], current["material"]["record_id"]}


def test_old_explicit_people_context_remains_available_without_becoming_current_material_people(world):
    w = world
    old = capture(w)
    person = w["crm"].create_contact(OWNER, w["unit"]["id"], {"name": "历史核对人物"}, NOW)
    context(w, "record:" + str(old["material"]["record_id"]),
            contact_relations=[{"contact_id": person["id"], "relation": "about"}])
    current = revise(w, old)
    assert w["crm"].list_records(OWNER, status="unfiled")["total"] == 2
    assert not w["timeline"].get_event(OWNER, "material:" + str(current["material"]["id"]))["contact_relations"]


@pytest.mark.parametrize("supplement", ["activity", "child"])
def test_user_supplement_keeps_original_record_as_an_editable_inbox_source(world, supplement):
    w = world
    old = capture(w)
    revise(w, old)
    if supplement == "activity":
        w["crm"].add_activity(OWNER, old["material"]["record_id"], "我补充的本次观察。", NOW)
    else:
        w["crm"].create_record(OWNER, {"title": "本人新补充", "content": "新的实际观察。",
            "customer_id": w["unit"]["id"], "parent_record_id": old["material"]["record_id"]}, NOW)
    assert old["material"]["record_id"] in {x["id"] for x in w["crm"].list_records(OWNER, status="unfiled")["items"]}


def test_matching_display_text_or_source_prefix_without_generated_job_identity_is_not_excluded(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    ordinary = w["crm"].create_record(OWNER, {"title": old["material"]["title"],
        "content": "材料 #1 · 转写版本 1。本人独立记录。", "customer_id": w["unit"]["id"]}, NOW)
    forged_prefix = w["crm"].capture_message(OWNER,
        "material:" + str(old["material"]["id"]) + ":" + "a" * 64 + ":note", "本人从独立文字来源记录。", "text", NOW)
    items = w["crm"].list_records(OWNER, status="unfiled")["items"]
    assert {x["id"] for x in items} == {ordinary["id"], forged_prefix["id"], current["material"]["record_id"]}


def test_owner_isolation_and_readable_source_history_survive_projection(world):
    w = world
    old = capture(w)
    current = revise(w, old)
    other = w["crm"].create_record("bob", {"title": "另一成员记录", "content": "另一成员原文。"}, NOW)
    assert [x["id"] for x in w["crm"].list_records("bob", status="unfiled")["items"]] == [other["id"]]
    assert w["crm"].dashboard("bob", NOW)["stats"]["unfiled"] == 1
    assert [x["id"] for x in w["crm"].list_records(OWNER, status="unfiled")["items"]] == [current["material"]["record_id"]]
    assert w["crm"].get_record(OWNER, old["material"]["record_id"])["content"]
    assert w["crm"].get_record("bob", old["material"]["record_id"]) is None
