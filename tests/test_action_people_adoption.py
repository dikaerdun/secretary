"""R02 action/contact adoption contracts on disposable, offline SQLite only."""
import asyncio
import copy
import json
from unittest.mock import patch

import pytest

from secretary.account_network import AccountNetwork
from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0
REPLY = {"answer": "先核实病历签名接口和采购牵头人。", "next_moves": [
    {"title": "向李工核实签名接口", "reason": "接口范围尚未明确", "contact_hint": "李工",
     "preparation": "准备接口问题清单", "success_signal": "明确下一步接口核实范围"},
    {"title": "整理问题一页纸", "reason": "供后续交流核对", "contact_hint": "李工",
     "preparation": "整理问题", "success_signal": "形成问题清单"}], "questions": [], "risks": []}


class OfflineAdvisor:
    async def reply(self, context, history, text, now):
        return copy.deepcopy(REPLY)


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / "r02-action-people-synthetic.sqlite3")
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    network = AccountNetwork(crm, workspace, clock=lambda: NOW)
    unit = network.create_unit("me", {"name": "合成医疗集团"}, {"unit_type": "group"})
    other = network.create_unit("me", {"name": "合成医疗科技"}, {"parent_customer_id": unit["id"]})
    person = crm.create_contact("me", unit["id"], {"name": "李工", "department": "信息中心"}, NOW)
    colleague = crm.create_contact("me", unit["id"], {"name": "李工", "department": "采购部"}, NOW)
    namesake = crm.create_contact("me", other["id"], {"name": "李工", "department": "技术部"}, NOW)
    foreign_unit = crm.create_customer("other", {"name": "另一用户单位"}, NOW)
    foreign = crm.create_contact("other", foreign_unit["id"], {"name": "李工"}, NOW)
    project = workspace.create_opportunity("me", unit["id"], {"name": "病历电子签名接口"})
    service = DiscussionService(crm, workspace, asyncio.Lock(), advisor=OfflineAdvisor(), clock=lambda: NOW)
    timeline = TimelineService(crm, workspace, discussions=service, clock=lambda: NOW)
    service.timeline = timeline
    thread = service.create_thread("me", {"customer_id": unit["id"], "opportunity_id": project["id"]})["thread"]
    result = {"crm": crm, "workspace": workspace, "network": network, "unit": unit, "other": other,
              "person": person, "colleague": colleague, "namesake": namesake, "foreign": foreign,
              "project": project, "service": service, "timeline": timeline, "thread": thread}
    yield result
    asyncio.run(service.close())
    crm.close()


def suggestion(w):
    response = asyncio.run(w["service"].send_message("me", w["thread"]["id"], {"text": "下一步怎么跟进？", "request_id": "offline-1"}))
    return next(message for message in response["messages"] if message["role"] == "assistant")


def choose(w, *people):
    options = w["service"].get_thread("me", w["thread"]["id"])["action_contact_options"]
    return {"contact_ids": [person["id"] for person in people], "expected_contact_version": options["version"]}


def adopt(w, message, body=None, index=1):
    return w["service"].adopt("me", w["thread"]["id"], message["id"], index, body or {})


def relations(w, record):
    return w["timeline"].get_event("me", "record:" + str(record["id"]))["contact_relations"]


def business_counts(w):
    names = ("crm_records", "crm_sales_discussion_adoptions", "crm_opportunity_links",
             "crm_timeline_contexts", "crm_timeline_context_history", "tasks", "proposals", "notifications")
    return {table: w["crm"]._db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in names}


def test_explicit_entity_select_appears_in_unit_project_and_person_without_communication(world):
    w = world
    message = suggestion(w)
    selected = choose(w, w["person"])
    record = adopt(w, message, {**selected, "draft": {"contact_hint": "集团信息中心李工"}, "expected_snapshot": message["snapshot"]})
    assert [item["contact_id"] for item in relations(w, record)] == [w["person"]["id"]]
    event = w["timeline"].get_event("me", "record:" + str(record["id"]))
    assert event["kind"] == "reflection" and event["occurred_at"] is None
    assert relations(w, record)[0]["relation"] == "about"
    assert record["action_contact_ids"] == [w["person"]["id"]]
    assert record["action_event_key"] == event["key"]
    for scope in ({"customer_id": w["unit"]["id"]}, {"customer_id": w["unit"]["id"], "opportunity_id": w["project"]["id"]}, {"contact_id": w["person"]["id"]}):
        assert record["id"] in [item["id"] for item in w["timeline"].history_context("me", scope)["open_actions"]]
    assert record["id"] not in [item["id"] for item in w["timeline"].history_context("me", {"contact_id": w["namesake"]["id"]})["open_actions"]]
    assert business_counts(w)["tasks"] == business_counts(w)["notifications"] == business_counts(w)["proposals"] == 0


@pytest.mark.parametrize("body", [None, {"contact_ids": []}])
def test_unselected_action_does_not_parse_hint_copy_source_attendees_or_project_members(world, body):
    w = world
    project = w["workspace"].update_opportunity("me", w["unit"]["id"], w["project"]["id"], {"contact_ids": [w["person"]["id"], w["colleague"]["id"]], "expected_revision": w["project"]["revision"]})
    source = w["timeline"].create_record("me", {"customer_id": w["unit"]["id"], "opportunity_id": project["id"]}, {
        "request_id": "source", "text": "李工参与了前次现场交流", "kind": "communication", "occurred_at": NOW - 100,
        "contact_relations": [{"contact_id": w["person"]["id"], "relation": "direct"}]})
    w["thread"] = w["service"].create_thread("me", {"customer_id": w["unit"]["id"], "opportunity_id": project["id"], "source_record_id": source["record"]["id"]})["thread"]
    record = adopt(w, suggestion(w), body)
    assert relations(w, record) == []
    assert record["action_contact_ids"] == []
    assert record["id"] not in [item["id"] for item in w["timeline"].history_context("me", {"contact_id": w["person"]["id"]})["open_actions"]]


def test_options_are_owner_scoped_disambiguated_and_available_with_model_disabled(world):
    w = world
    w["service"].advisor = None
    options = w["service"].get_thread("me", w["thread"]["id"])["action_contact_options"]
    assert {item["id"] for item in options["contacts"]} == {w["person"]["id"], w["colleague"]["id"]}
    person = next(item for item in options["contacts"] if item["id"] == w["person"]["id"])
    assert person["name"] == "李工" and person["department"] == "信息中心" and person["unit_name"] == w["unit"]["name"]
    assert "phone" not in person and "owner" not in person


@pytest.mark.parametrize("selected", ["foreign", "namesake"])
def test_illegal_owner_or_scope_selection_is_rejected_before_any_business_write(world, selected):
    w = world
    message, body = suggestion(w), choose(w, w[selected])
    before = business_counts(w)
    with pytest.raises((ValueError, KeyError)):
        adopt(w, message, body)
    assert business_counts(w) == before


@pytest.mark.parametrize("change", ["archive", "department"])
def test_expired_contact_options_reject_all_business_writes(world, change):
    w = world
    message, body = suggestion(w), choose(w, w["person"])
    data = {"archived": True} if change == "archive" else {"department": "新部门"}
    w["crm"].update_contact("me", w["unit"]["id"], w["person"]["id"], data, NOW + 1)
    before = business_counts(w)
    with pytest.raises(ValueError):
        adopt(w, message, body)
    assert business_counts(w) == before


def test_cross_unit_contact_requires_explicit_valid_membership_and_binds_about(world):
    w = world
    w["workspace"].upsert_stakeholder("me", w["unit"]["id"], w["project"]["id"], {"contact_id": w["namesake"]["id"], "roles": ["technical_reviewer"], "expected_revision": w["project"]["revision"]})
    record = adopt(w, suggestion(w), choose(w, w["namesake"]))
    assert [(person["contact_id"], person["relation"]) for person in relations(w, record)] == [(w["namesake"]["id"], "about")]


def test_membership_revocation_invalidates_candidate_version_before_write(world):
    w = world
    project = w["workspace"].upsert_stakeholder("me", w["unit"]["id"], w["project"]["id"], {"contact_id": w["namesake"]["id"], "expected_revision": w["project"]["revision"]})
    message, body = suggestion(w), choose(w, w["namesake"])
    w["workspace"].archive_stakeholder("me", w["unit"]["id"], project["id"], w["namesake"]["id"], {"expected_revision": project["revision"], "archived": True})
    before = business_counts(w)
    with pytest.raises(ValueError):
        adopt(w, message, body)
    assert business_counts(w) == before


@pytest.mark.parametrize("invalid", [[True], [1, 1], "李工", None, [0], ["1"]])
def test_entity_selection_shape_is_strict_and_empty_client_is_compatible(world, invalid):
    w = world
    message = suggestion(w)
    before = business_counts(w)
    with pytest.raises(ValueError):
        adopt(w, message, {"contact_ids": invalid})
    assert business_counts(w) == before
    assert adopt(w, message)["id"]


def test_nonempty_selection_requires_options_version_and_metadata_does_not_widen_draft(world):
    w = world
    message = suggestion(w)
    for body in ({"contact_ids": [w["person"]["id"]]}, {"contact_ids": [w["person"]["id"]], "expected_contact_version": "bad"},
                 {"draft": {"contact_ids": [w["person"]["id"]]}, "expected_snapshot": message["snapshot"]}):
        before = business_counts(w)
        with pytest.raises(ValueError):
            adopt(w, message, body)
        assert business_counts(w) == before


def test_adoption_replay_never_rebinds_after_user_changes_people_or_record(world):
    w = world
    message, body = suggestion(w), choose(w, w["person"])
    record = adopt(w, message, body)
    event = w["timeline"].get_event("me", record["action_event_key"])
    w["timeline"].save_context("me", event["key"], {"expected_revision": event["revision"], "contact_relations": [{"contact_id": w["colleague"]["id"], "relation": "about"}]})
    before = business_counts(w)
    replay = adopt(w, message, body)
    assert replay["id"] == record["id"] and replay["action_contact_ids"] == [w["colleague"]["id"]]
    assert business_counts(w) == before
    with pytest.raises(ValueError):
        adopt(w, message, choose(w, w["colleague"]))
    w["crm"].update_record("me", record["id"], {"title": "用户后来改过标题"}, NOW + 10)
    assert adopt(w, message, body)["title"] == "用户后来改过标题"
    assert [item["contact_id"] for item in relations(w, record)] == [w["colleague"]["id"]]


def test_contact_scope_discussion_keeps_legacy_explicit_focus_relation(world):
    w = world
    project = w["workspace"].update_opportunity("me", w["unit"]["id"], w["project"]["id"], {"contact_ids": [w["person"]["id"]], "expected_revision": w["project"]["revision"]})
    w["thread"] = w["service"].create_thread("me", {"customer_id": w["unit"]["id"], "opportunity_id": project["id"], "contact_id": w["person"]["id"]})["thread"]
    record = adopt(w, suggestion(w), {"contact_ids": []})
    assert record["action_contact_ids"] == [w["person"]["id"]]
    assert relations(w, record)[0]["relation"] == "about"


def test_failure_during_people_binding_rolls_back_action_links_and_adoption(world):
    w = world
    message, body = suggestion(w), choose(w, w["person"])
    before = business_counts(w)
    with patch.object(w["timeline"], "save_context", side_effect=ValueError("合成关联失败")):
        with pytest.raises(ValueError):
            adopt(w, message, body)
    assert business_counts(w) == before
    assert adopt(w, message, body)["action_contact_ids"] == [w["person"]["id"]]


def test_multiple_contacts_have_stable_id_order_and_replay_does_not_duplicate_context(world):
    w = world
    message, body = suggestion(w), choose(w, w["colleague"], w["person"])
    record = adopt(w, message, body)
    before = business_counts(w)
    replay = adopt(w, message, {**body, "contact_ids": list(reversed(body["contact_ids"]))})
    assert replay["action_contact_ids"] == sorted(body["contact_ids"])
    assert business_counts(w) == before


def test_unit_tree_membership_invalidation_prevents_cross_unit_binding(world):
    w = world
    w["workspace"].upsert_stakeholder("me", w["unit"]["id"], w["project"]["id"], {"contact_id": w["namesake"]["id"], "expected_revision": w["project"]["revision"]})
    message, body = suggestion(w), choose(w, w["namesake"])
    w["network"].update("me", w["other"]["id"], {"parent_customer_id": None, "expected_revision": w["other"]["revision"]})
    assert w["namesake"]["id"] not in {person["id"] for person in w["service"].get_thread("me", w["thread"]["id"])["action_contact_options"]["contacts"]}
    before = business_counts(w)
    with pytest.raises(ValueError):
        adopt(w, message, body)
    assert business_counts(w) == before


def test_helper_cas_preserves_later_explicit_relation_and_checks_owner_and_hidden(world):
    from secretary.action_people import action_people_receipt, bind_action_contacts
    w = world
    record = adopt(w, suggestion(w), choose(w, w["person"]))
    event = w["timeline"].get_event("me", record["action_event_key"])
    w["timeline"].save_context("me", event["key"], {"expected_revision": event["revision"], "contact_relations": []})
    before = business_counts(w)
    with pytest.raises(ValueError):
        bind_action_contacts(w["timeline"], "me", record["id"], [w["person"]["id"]], expected_revision=event["revision"])
    assert relations(w, record) == [] and business_counts(w) == before
    with pytest.raises(KeyError):
        action_people_receipt(w["timeline"], "other", record["id"])
    with w["crm"]._transaction() as db:
        db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("me", record["id"]))
    with pytest.raises(KeyError):
        action_people_receipt(w["timeline"], "me", record["id"])


def test_hidden_adoption_retry_is_explicit_missing_and_does_not_recreate(world):
    w = world
    message, body = suggestion(w), choose(w, w["person"])
    record = adopt(w, message, body)
    with w["crm"]._transaction() as db:
        db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("me", record["id"]))
    before = business_counts(w)
    with pytest.raises(KeyError):
        adopt(w, message, body)
    assert business_counts(w) == before


def test_selected_contacts_work_without_web_sidecar_initialization(world):
    w = world
    del w["service"].timeline
    record = adopt(w, suggestion(w), choose(w, w["person"]))
    assert record["action_contact_ids"] == [w["person"]["id"]]


def test_replay_of_later_archived_person_reports_current_review_need_without_write(world):
    w = world
    message, body = suggestion(w), choose(w, w["person"])
    record = adopt(w, message, body)
    w["crm"].update_contact("me", w["unit"]["id"], w["person"]["id"], {"archived": True}, NOW + 1)
    before = business_counts(w)
    replay = adopt(w, message, body)
    assert replay["id"] == record["id"] and replay["action_contact_needs_review"] is True
    assert business_counts(w) == before


def test_model_cannot_supply_contact_entity_execution_metadata(world):
    from secretary.sales_discussion import validate_reply
    data = copy.deepcopy(REPLY)
    data["next_moves"][0]["contact_ids"] = [world["person"]["id"]]
    with pytest.raises(ValueError):
        validate_reply(data)
