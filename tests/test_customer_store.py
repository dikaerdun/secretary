import copy
import sqlite3

import pytest

from secretary.crm import CRMStore
from secretary.customer_schema import ACCOUNT_FIELDS, CONTACT_FIELDS
from secretary.customer_store import CustomerStore


NOW = 1_800_000_000.0


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / "profiles.sqlite3")
    yield store
    store.close()


def new_payload():
    return {"intent": "create", "customer_name": "星海医院", "contact_name": "王总",
            "source_text": "新建客户星海医院，联系人王总，技术负责人。需要存储加密，王总希望先微信发材料。",
            "basic": {}, "contact": {"role": "技术负责人"},
            "contact_evidence": {"role": "技术负责人"},
            "attributes": [
                {"target": "account", "key": "crypto_needs", "value": "存储加密", "basis": "reported", "evidence": "需要存储加密"},
                {"target": "contact", "key": "communication_channel", "value": "先微信发材料", "basis": "reported",
                 "evidence": "王总希望先微信发材料"}]}


def create_account(crm, owner="alice"):
    return crm.create_customer(owner, {"name": "星海医院"}, NOW)


def update_payload(customer_id):
    return {"intent": "update", "customer_id": customer_id, "customer_name": "星海医院",
            "source_text": "星海医院需要存储加密", "attributes": [
                {"target": "account", "key": "crypto_needs", "value": "存储加密", "basis": "reported", "evidence": "需要存储加密"}]}


def test_create_confirmation_atomic_idempotent_and_no_schedule(crm):
    payload = new_payload()
    source = crm.capture_message("alice", "message", payload["source_text"], "voice", NOW)
    draft = crm.create_customer_draft("alice", payload, NOW, source_id="message", source_record_id=source["id"])
    assert draft["status"] == "pending"
    assert crm.list_customers("alice")["total"] == 0
    assert crm.get_record("alice", source["id"])["customer_id"] is None
    assert len(draft["changes"]) == 5
    assert draft["changes"][0]["evidence"] == "星海医院"
    # A retried callback returns the same immutable pending result.
    duplicate = crm.create_customer_draft("alice", {}, NOW + 1, source_id="message")
    assert duplicate == draft
    confirmed = crm.confirm_customer_draft("alice", draft["id"], NOW + 2)
    assert confirmed["status"] == "confirmed"
    profile = crm.profile("alice", confirmed["customer_id"])
    assert profile["customer"]["name"] == "星海医院"
    assert profile["fields"][0]["value"] == "存储加密"
    assert profile["contacts"][0]["role"] == "技术负责人"
    assert profile["contacts"][0]["fields"][0]["value"] == "先微信发材料"
    assert all(f["source_record_id"] == source["id"] for f in profile["history"])
    assert crm.get_record("alice", source["id"])["customer_id"] == confirmed["customer_id"]
    assert crm.confirm_customer_draft("alice", draft["id"], NOW + 4) == confirmed
    assert crm.list_customers("alice")["total"] == 1
    for table in ("tasks", "proposals", "notifications"):
        assert crm._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0


def test_mid_transaction_failure_rolls_back_customer_contacts_and_draft(crm):
    draft = crm.create_customer_draft("alice", new_payload(), NOW)
    crm._db.execute("CREATE TRIGGER fail_fact BEFORE INSERT ON crm_customer_facts "
                    "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        crm.confirm_customer_draft("alice", draft["id"], NOW + 1)
    assert crm.list_customers("alice")["total"] == 0
    assert crm._db.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0] == 0
    assert crm.get_customer_draft("alice", draft["id"])["status"] == "pending"


def test_profile_owner_and_cross_customer_isolation(crm):
    a, b = create_account(crm), create_account(crm, "bob")
    second = crm.create_customer("alice", {"name": "第二家公司"}, NOW)
    contact = crm.create_contact("alice", second["id"], {"name": "王总"}, NOW)
    assert crm.profile("bob", a["id"]) is None
    assert crm.find_customers_exact("alice", "星海医院")[0]["id"] == a["id"]
    assert crm.find_contacts_exact("bob", "王总") == []
    assert "owner" not in crm.profile("alice", a["id"])["customer"]
    for owner, customer_id in (("bob", a["id"]), ("alice", b["id"])):
        with pytest.raises(KeyError):
            crm.create_contact(owner, customer_id, {"name": "王总"}, NOW)
        with pytest.raises(KeyError):
            crm.save_fact(owner, customer_id, {"key": "pain_points", "value": "信息", "basis": "reported"}, NOW)
    with pytest.raises(KeyError):
        crm.save_fact("alice", a["id"], {"key": "interests", "value": "茶", "basis": "reported", "contact_id": contact["id"]}, NOW)
    with pytest.raises(KeyError):
        crm.update_contact("alice", a["id"], contact["id"], {"name": "张总"}, NOW)


def test_fact_history_and_current_value_keep_observation_separate(crm):
    customer = create_account(crm)
    contact = crm.create_contact("alice", customer["id"], {"name": "王总"}, NOW)
    original = crm.save_fact("alice", customer["id"], {"key": "concerns", "value": "交付周期",
                            "basis": "observation", "evidence": "我感觉他重视交付", "contact_id": contact["id"]}, NOW)
    replacement = crm.save_fact("alice", customer["id"], {"key": "concerns", "value": "性能",
                               "basis": "reported", "evidence": "他明确问了性能", "contact_id": contact["id"]}, NOW + 1)
    profile = crm.profile("alice", customer["id"])
    assert [f["id"] for f in profile["history"]] == [replacement["id"], original["id"]]
    assert profile["contacts"][0]["fields"][0]["id"] == replacement["id"]
    assert profile["history"][1]["basis"] == "observation"
    assert not profile["fields"]


@pytest.mark.parametrize("kind", ["customer", "contact", "fact", "other_connection"])
def test_unrelated_profile_fields_do_not_invalidate_draft_even_same_timestamp(crm, kind):
    customer = create_account(crm)
    contact = crm.create_contact("alice", customer["id"], {"name": "王总"}, NOW)
    draft = crm.create_customer_draft("alice", update_payload(customer["id"]), NOW)
    if kind == "customer":
        crm.update_customer("alice", customer["id"], {"notes": "changed"}, NOW)
        crm.update_customer("alice", customer["id"], {"notes": ""}, NOW)
    elif kind == "contact":
        crm.update_contact("alice", customer["id"], contact["id"], {"phone": "12345"}, NOW)
    elif kind == "fact":
        crm.save_fact("alice", customer["id"], {"key": "industry", "value": "医疗", "basis": "reported"}, NOW)
    else:
        path = crm._db.execute("PRAGMA database_list").fetchone()[2]
        with sqlite3.connect(path) as other:
            other.execute("UPDATE crm_customers SET notes='另一连接修改' WHERE id=?", (customer["id"],))
    assert crm.confirm_customer_draft("alice", draft["id"], NOW + 1)["status"] == "confirmed"
    assert any(f["key"] == "crypto_needs" for f in crm.profile("alice", customer["id"])["fields"])


def test_unrelated_customer_change_does_not_invalidate_draft(crm):
    a = create_account(crm)
    b = crm.create_customer("alice", {"name": "另一客户"}, NOW)
    draft = crm.create_customer_draft("alice", update_payload(a["id"]), NOW)
    crm.update_customer("alice", b["id"], {"notes": "new"}, NOW)
    assert crm.confirm_customer_draft("alice", draft["id"], NOW)["status"] == "confirmed"


def test_new_customer_name_collision_makes_other_pending_draft_stale(crm):
    first = crm.create_customer_draft("alice", new_payload(), NOW)
    second = crm.create_customer_draft("alice", new_payload(), NOW)
    crm.confirm_customer_draft("alice", first["id"], NOW)
    with pytest.raises(ValueError, match="过期"):
        crm.confirm_customer_draft("alice", second["id"], NOW)
    assert crm.list_customers("alice")["total"] == 1


def test_cancel_and_foreign_draft_are_safe(crm):
    draft = crm.create_customer_draft("alice", new_payload(), NOW)
    assert crm.get_customer_draft("bob", draft["id"]) is None
    assert crm.list_customer_drafts("bob")["total"] == 0
    for call in (crm.confirm_customer_draft, crm.reject_customer_draft):
        with pytest.raises(KeyError):
            call("bob", draft["id"], NOW)
    rejected = crm.reject_customer_draft("alice", draft["id"], NOW)
    assert crm.reject_customer_draft("alice", draft["id"], NOW) == rejected
    with pytest.raises(ValueError):
        crm.confirm_customer_draft("alice", draft["id"], NOW)
    assert crm.list_customers("alice")["total"] == 0


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(owner="bob"),
    lambda p: p.update(intent="confirm"),
    lambda p: p["attributes"][0].update(key="religion"),
    lambda p: p["attributes"][0].update(basis="inferred"),
    lambda p: p["attributes"][0].update(evidence="没有提过的依据"),
    lambda p: p.update(basic={"stage": "won"}),
    lambda p: p["contact_evidence"].update(role="凭空推测"),
    lambda p: p.update(customer_name="另一家公司"),
    lambda p: p["attributes"].append(copy.deepcopy(p["attributes"][0])),
])
def test_unsupported_or_unproven_draft_rejected_before_writes(crm, mutation):
    payload = new_payload()
    mutation(payload)
    with pytest.raises(ValueError):
        crm.create_customer_draft("alice", payload, NOW)
    assert crm.list_customer_drafts("alice")["total"] == 0
    assert crm.list_customers("alice")["total"] == 0


def test_draft_source_and_manual_fact_source_must_be_owned_and_exact(crm):
    customer = create_account(crm)
    text = update_payload(customer["id"])["source_text"]
    record = crm.capture_message("bob", "foreign", text, "voice", NOW)
    with pytest.raises(KeyError):
        crm.create_customer_draft("alice", update_payload(customer["id"]), NOW, source_record_id=record["id"])
    own = crm.capture_message("alice", "own", "不是同一段原话", "voice", NOW)
    with pytest.raises(ValueError, match="原话"):
        crm.create_customer_draft("alice", update_payload(customer["id"]), NOW, source_record_id=own["id"])
    with pytest.raises(ValueError, match="原话"):
        crm.save_fact("alice", customer["id"], {"key": "industry", "value": "医疗", "basis": "reported",
                      "evidence": "医疗", "source_record_id": own["id"]}, NOW)


def test_contact_migration_and_quick_form_preserve_multi_contact_changes(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = CRMStore(path)
    customer = legacy.create_customer("alice", {"name": "星海医院", "contact": "王总", "phone": "12345"}, NOW)
    legacy.close()
    crm = CustomerStore(path)
    person = crm.profile("alice", customer["id"])["contacts"][0]
    assert person["name"] == "王总" and person["phone"] == "12345"
    crm.update_contact("alice", customer["id"], person["id"], {"name": "王经理"}, NOW)
    quick = crm.create_customer("alice", {"name": "新客户", "contact": "张总", "phone": "67890"}, NOW)
    assert crm.find_contacts_exact("alice", "张总")[0]["customer_id"] == quick["id"]
    crm.close()
    reopened = CustomerStore(path)
    try:
        assert [c["name"] for c in reopened.profile("alice", customer["id"])["contacts"]] == ["王经理"]
        assert len(reopened.profile("alice", quick["id"])["contacts"]) == 1
    finally:
        reopened.close()


def test_existing_contact_update_does_not_create_duplicate_or_touch_other_contact(crm):
    customer = create_account(crm)
    first = crm.create_contact("alice", customer["id"], {"name": "王总", "role": "技术"}, NOW)
    second = crm.create_contact("alice", customer["id"], {"name": "张总", "role": "采购"}, NOW)
    payload = {"intent": "update", "customer_id": customer["id"], "customer_name": "星海医院",
               "contact_id": first["id"], "contact_name": "王总", "source_text": "星海医院王总喜欢先看一页概览",
               "attributes": [{"target": "contact", "key": "detail_preference", "value": "先看一页概览",
                               "basis": "reported", "evidence": "喜欢先看一页概览"}]}
    draft = crm.create_customer_draft("alice", payload, NOW)
    assert draft["changes"][0]["before"] is None
    crm.confirm_customer_draft("alice", draft["id"], NOW)
    profile = crm.profile("alice", customer["id"])
    assert len(profile["contacts"]) == 2
    assert profile["contacts"][0]["fields"][0]["value"] == "先看一页概览"
    assert profile["contacts"][1]["id"] == second["id"] and not profile["contacts"][1]["fields"]
    # A caller must resolve an existing contact explicitly, never silently duplicate.
    payload.pop("contact_id")
    with pytest.raises(ValueError, match="同名"):
        crm.create_customer_draft("alice", payload, NOW)


def test_same_name_contacts_are_returned_as_candidates_and_fields_are_separate(crm):
    first = create_account(crm)
    second = crm.create_customer("alice", {"name": "另一客户"}, NOW)
    for customer in (first, second):
        crm.create_contact("alice", customer["id"], {"name": "王总"}, NOW)
    assert len(crm.find_contacts_exact("alice", "王总")) == 2
    assert len(crm.find_contacts_exact("alice", "王总", customer_id=first["id"])) == 1
    assert all("label" in value and "group" in value and value["examples"] for value in ACCOUNT_FIELDS.values())
    assert len(ACCOUNT_FIELDS) == 28 and len(CONTACT_FIELDS) == 10
    assert {'legal_name', 'website'} <= set(ACCOUNT_FIELDS)
    assert {'responsibilities', 'professional_goals'} <= set(CONTACT_FIELDS)


def test_customer_search_and_primary_display_use_current_multi_contacts(crm):
    customer = crm.create_customer("alice", {"name": "星海医院", "contact": "王总", "phone": "旧电话"}, NOW)
    first = crm.profile("alice", customer["id"])["contacts"][0]
    crm.update_contact("alice", customer["id"], first["id"], {"name": "王经理", "phone": ""}, NOW)
    crm.create_contact("alice", customer["id"], {"name": "张采购", "phone": "13112345678"}, NOW)
    match = crm.list_customers("alice", q="张采购")["items"][0]
    assert match["id"] == customer["id"] and match["primary_contact_name"] == "王经理"
    assert match["primary_contact_phone"] == "" and match["contact_count"] == 2
    assert crm.list_customers("alice", q="13112345678")["total"] == 1
    assert crm.list_customers("alice", q="旧电话")["total"] == 0
    assert crm.list_customers("alice", q="王总")["total"] == 0
    assert crm.list_customers("bob", q="张采购")["total"] == 0
    assert crm.list_customers("alice", q="%")["total"] == 0


def test_profile_brief_keeps_undated_open_notes_and_lists_confirmed_followups(crm):
    customer = create_account(crm)
    note = crm.create_record("alice", {"title": "补充方案", "content": "客户需要补充方案", "customer_id": customer["id"]}, NOW)
    reply = crm.execute("alice", "proposal", {"action": "propose", "title": "补充方案", "remind_at": NOW + 3600}, NOW)
    proposal_id = int(reply.split("P", 1)[1].split()[0])
    crm.link_proposal("alice", note["id"], proposal_id, NOW)
    crm.execute("alice", "confirm", {"action": "confirm", "proposal_id": proposal_id}, NOW)
    profile = crm.profile("alice", customer["id"])
    assert profile["brief"]["open_records"][0]["id"] == note["id"]
    assert profile["brief"]["next_tasks"][0]["title"] == "补充方案"
    assert {f["key"] for f in profile["brief"]["missing_fields"]} >= {"pain_points", "decision_chain"}
    assert profile["brief"]["recent_activities"] == []


def test_profile_recent_activities_are_customer_owner_scoped_visible_and_limited(crm):
    customer = create_account(crm)
    another_customer = crm.create_customer('alice', {'name': '另一客户'}, NOW)
    foreign_customer = create_account(crm, 'bob')
    records = [crm.create_record('alice', {'title': title, 'content': '初次沟通',
                'customer_id': customer['id']}, NOW) for title in ('方案交流', '技术验证')]
    for index in range(12):
        crm.add_activity('alice', records[index % 2]['id'], f'客户反馈 {index}', NOW + index)
    other_record = crm.create_record('alice', {'title': '他客户', 'content': '不应混入',
                                              'customer_id': another_customer['id']}, NOW)
    foreign_record = crm.create_record('bob', {'title': '其他用户', 'content': '不应泄漏',
                                              'customer_id': foreign_customer['id']}, NOW)
    hidden_record = crm.create_record('alice', {'title': '隐藏记录', 'content': '不应混入',
                                              'customer_id': customer['id']}, NOW)
    crm.add_activity('alice', other_record['id'], '其他客户的跟进', NOW + 100)
    crm.add_activity('bob', foreign_record['id'], '其他用户的跟进', NOW + 101)
    crm.add_activity('alice', hidden_record['id'], '隐藏记录的跟进', NOW + 102)
    with crm._transaction() as db:
        db.execute('UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?', ('alice', hidden_record['id']))
    items = crm.profile('alice', customer['id'])['brief']['recent_activities']
    assert len(items) == 10
    assert [item['content'] for item in items] == [f'客户反馈 {index}' for index in range(11, 1, -1)]
    assert items[0]['record_title'] == '技术验证'
    assert items[0]['created_at'] == NOW + 11
    assert set(items[0]) == {'id', 'record_id', 'content', 'created_at', 'record_title'}
    assert crm.profile('bob', customer['id']) is None
    assert crm.profile('alice', another_customer['id'])['brief']['recent_activities'][0]['content'] == '其他客户的跟进'
