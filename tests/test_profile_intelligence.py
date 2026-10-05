"""Synthetic discovery scenarios; no live database, microphone or model requests."""
import asyncio
import json

import httpx
import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.sales_workspace import SalesWorkspace
from secretary.sales_discussion import DiscussionService
from secretary.profile_intelligence import ProfileAnalyzer, ProfileConflict, ProfileIntelligence
from secretary.visits import VisitService


NOW = 1_800_000_000.0


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / "intelligence-synthetic.sqlite3")
    clock = [NOW]
    workspace = SalesWorkspace(crm, clock=lambda: clock[0])
    customer = crm.create_customer("owner", {"name": "星河城市银行股份有限公司"}, NOW)
    person = crm.create_contact("owner", customer["id"], {"name": "王工", "role": "技术"}, NOW)
    first = workspace.create_opportunity("owner", customer["id"], {"name": "数据库加密", "stage": "proposal"})
    second = workspace.create_opportunity("owner", customer["id"], {"name": "密钥管理", "stage": "qualified"})
    service = ProfileIntelligence(crm, workspace, clock=lambda: clock[0])
    yield crm, workspace, service, customer, person, first, second, clock
    crm.close()


def note(crm, customer, text, owner="owner"):
    return crm.create_record(owner, {"title": text[:80], "content": text, "customer_id": customer["id"], "kind": "note"}, NOW)


def scan(service, **kwargs):
    return asyncio.run(service.scan("owner", **kwargs))


def candidates(service, customer=None, **kwargs):
    return service.list_candidates("owner", customer["id"] if customer else None, **kwargs)["items"]


def select(service, key, customer=None):
    return next(item for item in candidates(service, customer) if item["key"] == key)


def confirm(service, item, **kwargs):
    return service.decide("owner", item["id"], {"decision": "confirm", "expected_revision": item["revision"], **kwargs})


def test_first_scan_baselines_history_force_replays_and_new_sources_automatic(services):
    crm, _, service, customer, _, _, _, _ = services
    note(crm, customer, "预算尚未审批。")
    assert scan(service)["baselined"] is True
    assert candidates(service) == []
    assert scan(service, force=True)["created"] >= 1
    total = len(candidates(service))
    assert scan(service, force=True)["created"] == 0
    assert len(candidates(service)) == total
    note(crm, customer, "现有系统正在使用旧版密码设备。")
    assert scan(service)["created"] >= 1
    assert service.status("owner")["analysis_mode"] == "rules"


def test_two_project_budgets_and_negation_do_not_pollute_company(services):
    crm, workspace, service, customer, _, first, second, _ = services
    one = note(crm, customer, "预算30万元，尚未审批。")
    two = note(crm, customer, "预算50万元，已经审批。")
    workspace.link("owner", "record", one["id"], first["id"])
    workspace.link("owner", "record", two["id"], second["id"])
    scan(service, force=True)
    for item in candidates(service):
        confirm(service, item)
    a = {item["key"]: item["value"] for item in service.project_facts("owner", customer["id"], first["id"])["items"]}
    b = {item["key"]: item["value"] for item in service.project_facts("owner", customer["id"], second["id"])["items"]}
    assert "尚未审批" in a["budget_approval"]
    assert "50万元" in b["budget_notes"]
    assert crm.profile("owner", customer["id"])["fields"] == []
    assert crm.get_customer("owner", customer["id"])["amount_cents"] is None


def test_unlinked_project_and_unidentified_contact_need_manual_scope(services):
    crm, _, service, customer, person, first, _, _ = services
    note(crm, customer, "预算尚未审批。希望先电话沟通。")
    scan(service, force=True)
    budget = select(service, "budget_notes")
    assert budget["requires_scope_confirmation"]
    with pytest.raises(ValueError, match="哪个项目"):
        confirm(service, budget)
    confirm(service, budget, opportunity_id=first["id"])
    preference = select(service, "communication_channel")
    assert preference["contact_id"] is None and preference["requires_contact_confirmation"]
    confirm(service, preference, contact_id=person["id"])
    assert crm.profile("owner", customer["id"])["contacts"][0]["fields"][0]["key"] == "communication_channel"


def test_same_named_people_do_not_use_primary_contact(services):
    crm, _, service, customer, person, _, _, _ = services
    crm.update_contact("owner", customer["id"], person["id"], {"department": "信息技术部"}, NOW)
    crm.create_contact("owner", customer["id"], {"name": person["name"], "role": "采购", "department": "采购部"}, NOW)
    note(crm, customer, "王工希望先电话沟通。")
    scan(service, force=True)
    item = select(service, "communication_channel")
    assert item["contact_name"] == "王工" and item["contact_id"] is None
    assert item["requires_contact_confirmation"]


def test_contact_exact_preference_and_project_authority_stays_relation_review(services):
    crm, workspace, service, customer, person, first, _, _ = services
    source = note(crm, customer, "王工负责技术评审。王工希望先微信发材料。")
    workspace.link("owner", "record", source["id"], first["id"])
    scan(service, force=True)
    preference = select(service, "communication_channel")
    assert preference["contact_id"] == person["id"]
    assert confirm(service, preference)["status"] == "confirmed"
    authority = select(service, "authority")
    assert authority["scope"] == "stakeholder" and authority["opportunity_id"] == first["id"]
    with pytest.raises(ValueError, match="决策关系"):
        confirm(service, authority)
    assert all(f["key"] != "authority" for f in crm.profile("owner", customer["id"])["contacts"][0]["fields"])


def test_profile_conflict_shows_old_and_new_keeps_history_and_manual_revision(services):
    crm, _, service, customer, _, _, _, _ = services
    old = crm.save_fact("owner", customer["id"], {"key": "industry", "value": "金融", "basis": "reported"}, NOW)
    source = note(crm, customer, "行业是金融科技。")
    scan(service, force=True)
    item = select(service, "industry")
    assert item["conflict"] and item["current_value"]["id"] == old["id"]
    confirm(service, item, value="金融科技", reason="已核对原话")
    profile = crm.profile("owner", customer["id"])
    assert profile["fields"][0]["value"] == "金融科技"
    assert {f["value"] for f in profile["history"]} == {"金融", "金融科技"}
    reviewed = service.get_candidate("owner", item["id"])
    assert reviewed["value"] == source["content"].rstrip("。")
    assert "金融科技" in reviewed["decision_history"][0]["details_json"]
    assert confirm(service, reviewed)["status"] == "confirmed"


def test_intervening_same_field_write_preserves_pending_and_force_keeps_one_draft(services):
    crm, _, service, customer, _, _, _, _ = services
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    item = select(service, "industry")
    crm.save_fact("owner", customer["id"], {"key": "industry", "value": "保险", "basis": "reported"}, NOW + 1)
    with pytest.raises(ProfileConflict):
        confirm(service, item)
    assert service.get_candidate("owner", item["id"])["status"] == "pending"
    assert crm.profile("owner", customer["id"])["fields"][0]["value"] == "保险"
    assert scan(service, force=True)["created"] == 0
    assert len(candidates(service)) == 1
    assert select(service, "industry")["current_value"]["value"] == "保险"
    preview = service.preview_candidate("owner", item["id"])
    confirmed = confirm(service, preview, expected_fact_id=preview["current_fact_id"])
    assert confirmed["status"] == "confirmed"


def test_source_correction_reassignment_and_project_link_invalidate(services):
    crm, workspace, service, customer, _, first, second, _ = services
    source = note(crm, customer, "预算30万元。")
    workspace.link("owner", "record", source["id"], first["id"])
    scan(service, force=True)
    item = select(service, "budget_notes")
    crm.update_record("owner", source["id"], {"content": "预算还没明确。"}, NOW + 1)
    assert service.get_candidate("owner", item["id"])["status"] == "stale"
    workspace.link("owner", "record", source["id"], second["id"])
    scan(service)
    fresh = select(service, "budget_notes")
    assert fresh["opportunity_id"] == second["id"] and "还没明确" in fresh["evidence"]
    another = crm.create_customer("owner", {"name": "另一医院"}, NOW)
    crm.update_record("owner", source["id"], {"customer_id": another["id"]}, NOW + 2)
    assert service.get_candidate("owner", fresh["id"])["status"] == "stale"


def test_rejected_candidate_not_recreated_by_restart_or_rescan(services):
    crm, workspace, service, customer, _, _, _, _ = services
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    item = select(service, "industry")
    service.decide("owner", item["id"], {"decision": "reject", "reason": "并非客户原话"})
    reopened = ProfileIntelligence(crm, workspace)
    assert scan(reopened, force=True)["created"] == 0
    assert reopened.list_candidates("owner", status="all")["total"] == 1
    assert reopened.get_candidate("owner", item["id"])["decision_history"][0]["reason"] == "并非客户原话"


def test_owner_isolation_at_every_read_and_write(services):
    crm, _, service, customer, _, first, _, _ = services
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    item = select(service, "industry")
    assert service.list_candidates("bob")["total"] == 0
    assert service.get_candidate("bob", item["id"]) is None
    for operation in (lambda: service.decide("bob", item["id"], {"decision": "confirm"}),
                      lambda: service.profile_plan("bob", customer["id"]),
                      lambda: service.project_facts("bob", customer["id"], first["id"]),
                      lambda: service.configure("bob", {"official_domains": ["example.com"]}, customer["id"])):
        with pytest.raises(KeyError):
            operation()


def test_disabled_mode_queues_new_sources_manual_scan_is_explicit(services):
    crm, _, service, customer, _, _, _, _ = services
    scan(service)
    service.configure("owner", {"enabled": False})
    note(crm, customer, "行业是金融。")
    assert scan(service)["created"] == 0
    assert service.status("owner")["queue_count"] == 1
    assert scan(service, force=True)["created"] == 1


def test_failed_model_sanitizes_retry_and_no_partial_candidates(services):
    crm, _, service, customer, _, _, _, clock = services
    class Broken:
        async def extract(self, source, context):
            return [{"key": "industry", "value": "金融", "evidence": "行业是金融", "basis": "reported"},
                    {"key": "region", "value": "上海", "evidence": "FAKE API KEY", "basis": "reported"}]
    service.analyzer = Broken()
    note(crm, customer, "行业是金融。")
    assert scan(service, force=True)["failed"] == 1
    assert candidates(service) == []
    assert "FAKE" not in str(service.status("owner"))
    clock[0] += 31
    service.analyzer = None
    assert scan(service)["created"] == 1


def test_model_source_race_no_commit_and_limits_batches(services):
    crm, _, service, customer, _, _, _, _ = services
    record = note(crm, customer, "行业是金融。")
    class Racing:
        async def extract(self, source, context):
            crm.update_record("owner", record["id"], {"content": "行业是保险。"}, NOW + 1)
            return [{"key": "industry", "value": "金融", "evidence": "行业是金融", "basis": "reported"}]
    service.analyzer = Racing()
    assert scan(service, force=True)["failed"] == 1
    assert candidates(service) == []
    service.analyzer = None
    for index in range(5):
        note(crm, customer, f"所在地是示例城市{index}。")
    assert scan(service, limit=2)["processed"] == 2
    assert service.status("owner")["queue_count"] >= 4


def test_material_source_occurrence_versions_exclusion_and_visit_project(services):
    crm, workspace, service, customer, _, first, _, _ = services
    lock = asyncio.Lock()
    materials = MaterialService(crm, lock, clock=lambda: NOW)
    visits = VisitService(crm, materials, lock)
    visit = visits.create("owner", {"title": "现场交流", "customer_id": customer["id"], "occurred_at": NOW - 86400})
    material = visits.add_material("owner", visit["id"], {"provider": "manual", "title": "会议录音转写", "role": "recording", "text": "预算30万元尚未审批。"})["material"]
    workspace.link("owner", "visit", visit["id"], first["id"])
    scan(service, force=True)
    item = select(service, "budget_notes")
    assert item["source"]["type"] == "material" and item["source"]["occurred_at"] == NOW - 86400
    assert item["opportunity_id"] == first["id"]
    with crm._transaction() as db:
        db.execute("INSERT INTO crm_visit_source_choices(owner,visit_id,material_id,use_status,reason,revision,updated_at) VALUES (?,?,?,'excluded','不属本次交流',1,?)", ("owner", visit["id"], material["id"], NOW))
    assert service.get_candidate("owner", item["id"])["status"] == "stale"


def test_user_discussion_and_progress_included_assistant_never_evidence(services):
    crm, workspace, service, customer, _, first, _, _ = services
    discussion = DiscussionService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    thread = discussion.create_thread("owner", {"customer_id": customer["id"], "opportunity_id": first["id"], "title": "推进讨论"})["thread"]
    with crm._transaction() as db:
        for role, text in (("user", "预算尚未审批。"), ("assistant", "预算已审批，可以直接报价。")):
            db.execute("INSERT INTO crm_sales_discussion_messages(owner,thread_id,role,text,status,created_at,updated_at) VALUES (?,?,?,?,'complete',?,?)", ("owner", thread["id"], role, text, NOW, NOW))
    record = note(crm, customer, "交流概况。")
    workspace.link("owner", "record", record["id"], first["id"])
    crm.add_activity("owner", record["id"], "现有系统正在使用旧版密码设备。", NOW)
    scan(service, force=True)
    items = candidates(service)
    assert {item["source"]["type"] for item in items} == {"discussion_user", "activity"}
    assert all("可以直接报价" not in item["evidence"] for item in items)
    assert all(item["opportunity_id"] == first["id"] for item in items)


def test_personal_observations_not_promoted_and_questions_not_facts(services):
    crm, _, service, customer, _, _, _, _ = services
    note(crm, customer, "我觉得王工关注稳定性。预算是否已经审批？建议先问采购流程。")
    scan(service, force=True)
    assert select(service, "concerns")["basis"] == "observation"
    assert {item["key"] for item in candidates(service)} == {"concerns"}


@pytest.mark.parametrize("model", [False, True])
@pytest.mark.parametrize("text,evidence", [
    ("我计划先提供接口清单，确认接口对接范围。", "接口对接范围"),
    ("我准备把预算30万元作为沟通目标。", "预算30万元"),
    ("我希望客户预算30万元。", "预算30万元"),
    ("我想到先提供接口清单。", "接口清单"),
    ("预算是否已经审批？", "预算"),
    ("建议先问采购流程。", "采购流程"),
])
def test_own_proposals_and_questions_never_become_profile_even_with_short_model_quote(services, model, text, evidence):
    crm, _, service, customer, _, _, _, _ = services
    if model:
        class ShortQuote:
            async def extract(self, source, context):
                return [{"key": "compatibility", "value": evidence, "evidence": evidence, "basis": "reported"}]
        service.analyzer = ShortQuote()
    source = note(crm, customer, text)
    result = scan(service, force=True)
    assert result["failed"] == 0
    assert candidates(service) == []
    assert crm.get_record("owner", source["id"])["content"] == text
    assert crm.profile("owner", customer["id"])["fields"] == []


@pytest.mark.parametrize("model", [False, True])
def test_observation_guard_reads_full_clause_not_only_model_quote(services, model):
    crm, _, service, customer, person, first, _, _ = services
    if model:
        class ShortQuote:
            async def extract(self, source, context):
                return [{"key": "concerns", "value": "关注上线风险", "evidence": "关注上线风险", "basis": "reported", "contact_name": "王工"}]
        service.analyzer = ShortQuote()
    note(crm, customer, "我觉得王工关注上线风险。")
    scan(service, force=True)
    item = select(service, "concerns")
    assert item["basis"] == "observation" and item["contact_id"] == person["id"]
    with pytest.raises(ValueError, match="核实|原话"):
        confirm(service, item, basis="reported")
    assert service.get_candidate("owner", item["id"])["status"] == "pending"
    confirm(service, item)
    assert crm.profile("owner", customer["id"])["contacts"][0]["fields"][0]["basis"] == "observation"


@pytest.mark.parametrize("model", [False, True])
def test_reflection_is_observation_but_explicit_customer_statement_stays_reported(services, model):
    crm, _, service, customer, _, _, _, _ = services
    if model:
        class Extract:
            async def extract(self, source, context):
                return [{"key": "concerns", "value": "关注上线风险", "evidence": "关注上线风险", "basis": "reported", "contact_name": "王工"}]
        service.analyzer = Extract()
    first = note(crm, customer, "王工关注上线风险。")
    second = note(crm, customer, "王工明确说他关注上线风险。")
    crm.update_record("owner", first["id"], {"category": "visit_review"}, NOW)
    crm.update_record("owner", second["id"], {"category": "visit_review"}, NOW)
    scan(service, force=True)
    by_source = {item["source"]["id"]: item for item in candidates(service)}
    assert by_source[first["id"]]["basis"] == "observation"
    assert by_source[second["id"]]["basis"] == "reported"


def test_public_guard_applies_to_model_and_enrichment_preserves_original_dates(services):
    crm, _, service, customer, _, _, _, clock = services
    class Extract:
        async def extract(self, source, context):
            return [{"key": "industry", "value": "金融", "evidence": "行业是金融", "basis": "reported"}]
    service.analyzer = Extract()
    imported = asyncio.run(service.import_public_source("owner", customer["id"], {
        "url": "https://news.example.com/about", "title": customer["name"], "text": "行业是金融。",
        "published_at": "2020-01-02", "fetched_at": NOW - 60}))
    item = select(service, "industry")
    assert item["basis"] == "observation"
    clock[0] += 30
    confirm(service, item, verify_public=True, verify_entity=True)
    profile = crm.profile("owner", customer["id"])
    enriched = service.enrich_profile("owner", profile)
    fact = enriched["fields"][0]
    assert fact["source"]["published_at"] == "2020-01-02"
    assert fact["source"]["fetched_at"] == NOW - 60
    assert fact["source"]["recorded_at"] == NOW
    assert fact["recorded_at"] == NOW + 30
    assert "text" not in fact["source"] and "fingerprint" not in fact["source"]
    assert profile["fields"][0]["source"] == "manual"  # caller snapshot not mutated
    with pytest.raises(KeyError):
        service.enrich_profile("bob", profile)
    assert imported["source_id"] == item["source"]["id"]


def test_research_empty_success_and_saved_source_analysis_failure_have_different_receipts(services):
    crm, _, service, customer, _, _, _, clock = services
    class Empty:
        async def research(self, context):
            return []
    service.researcher = Empty()
    empty = asyncio.run(service.research("owner", customer["id"]))
    assert empty["search_status"] == "complete" and empty["status"] == "no_results"
    assert empty["analysis_failed_sources"] == 0 and "error" not in empty
    clock[0] += 86401
    class Result:
        async def research(self, context):
            return [{"url": "https://news.example.com/item", "title": customer["name"], "text": "行业是金融。"}]
    class Bad:
        async def extract(self, source, context):
            raise ValueError("SECRET PRIVATE MODEL PAYLOAD")
    service.researcher, service.analyzer = Result(), Bad()
    failed = asyncio.run(service.research("owner", customer["id"]))
    assert failed["search_status"] == "complete" and failed["status"] == "analysis_failed"
    assert failed["analysis_failed_sources"] == 1 and failed["rejected_sources"] == 0
    assert failed["error"] and "SECRET" not in str(failed)
    assert candidates(service) == []
    assert crm._db.execute("SELECT text FROM crm_profile_public_sources WHERE owner='owner'").fetchone()[0] == "行业是金融。"
    assert service.status("owner", customer["id"])["failed_count"] == 1
    assert service.status("owner", customer["id"])["customer_settings"]["last_research_at"] == clock[0]
    service.analyzer = None
    retried = scan(service, force=True)
    assert retried["created"] == 1 and select(service, "industry")["basis"] == "observation"


@pytest.mark.parametrize("model", [False, True])
def test_customer_first_person_quote_is_not_salesperson_proposal(services, model):
    crm, _, service, customer, person, _, _, _ = services
    if model:
        class Extract:
            async def extract(self, source, context):
                return [{"key": "communication_channel", "value": "先电话沟通", "evidence": "先电话沟通",
                         "basis": "reported", "contact_name": "王工"}]
        service.analyzer = Extract()
    source = note(crm, customer, "王工：我希望先电话沟通。")
    crm.update_record("owner", source["id"], {"category": "visit_review"}, NOW)
    scan(service, force=True)
    item = select(service, "communication_channel")
    assert item["basis"] == "reported" and item["contact_id"] == person["id"]
    confirm(service, item)
    assert crm.profile("owner", customer["id"])["contacts"][0]["fields"][0]["basis"] == "reported"


def test_later_own_plan_after_customer_quote_is_not_customer_attribute(services):
    crm, _, service, customer, _, _, _, _ = services
    class Extract:
        async def extract(self, source, context):
            return [{"key": "compatibility", "value": "接口对接范围", "evidence": "接口对接范围", "basis": "reported"}]
    service.analyzer = Extract()
    note(crm, customer, "王工明确说还没评审，我准备先提供接口对接范围。")
    assert scan(service, force=True)["failed"] == 0
    assert candidates(service) == []


def test_source_kind_correction_stales_pending_without_rewriting_record_or_confirmed_facts(services):
    crm, _, service, customer, _, _, _, _ = services
    source = note(crm, customer, "王工关注上线风险。")
    scan(service, force=True)
    item = select(service, "concerns")
    assert item["basis"] == "reported"
    old_keys = {candidate["key"] for candidate in candidates(service)}
    crm.update_record("owner", source["id"], {"category": "visit_review"}, NOW + 10)
    assert service.get_candidate("owner", item["id"])["status"] == "stale"
    with pytest.raises(ProfileConflict):
        confirm(service, item)
    assert scan(service, force=True)["created"] == len(old_keys)
    assert {candidate["key"] for candidate in candidates(service)} == old_keys
    assert all(candidate["basis"] == "observation" for candidate in candidates(service))
    assert select(service, "concerns")["basis"] == "observation"
    assert crm.get_record("owner", source["id"])["content"] == "王工关注上线风险。"
    assert crm.profile("owner", customer["id"])["fields"] == []


def test_source_guard_upgrade_does_not_replay_legacy_history(services):
    import hashlib
    crm, _, service, customer, _, _, _, _ = services
    source = note(crm, customer, "行业是金融。")
    # A source/job snapshot made by the previous release, without category.
    legacy = {"type": "record", "id": source["id"], "customer_id": customer["id"], "opportunity_id": None,
              "title": source["title"], "text": source["content"], "occurred_at": None, "recorded_at": NOW,
              "source_record_id": source["id"], "original_text": source["original_content"]}
    fingerprint = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    with crm._transaction() as db:
        service._settings(db, "owner")
        db.execute("UPDATE crm_profile_settings SET initialized=1 WHERE owner='owner'")
        db.execute("INSERT INTO crm_profile_source_jobs(owner,source_type,source_id,fingerprint,customer_id,status,updated_at) VALUES ('owner','record',?,?,?,'baseline',?)",
                   (source["id"], fingerprint, customer["id"], NOW))
    before = dict(crm._db.execute("SELECT * FROM crm_records WHERE id=?", (source["id"],)).fetchone())
    upgraded = scan(service)
    assert upgraded["processed"] == 0 and upgraded["created"] == 0
    assert candidates(service) == []
    assert dict(crm._db.execute("SELECT * FROM crm_records WHERE id=?", (source["id"],)).fetchone()) == before
    assert crm._db.execute("SELECT count(*) FROM crm_profile_source_jobs WHERE owner='owner'").fetchone()[0] == 1


def test_research_partial_analysis_and_invalid_identity_are_separate_from_zero_results(services):
    crm, _, service, customer, _, _, _, _ = services
    class Researcher:
        async def research(self, context):
            return [
                {"url": "https://news.example.com/good", "title": customer["name"], "text": "行业是金融。"},
                {"url": "https://news.example.com/failed", "title": customer["name"], "text": "总部位于上海。"},
                {"url": "https://news.example.com/wrong", "title": "另一个单位", "text": "行业是医疗。"},
            ]
    class Extract:
        async def extract(self, source, context):
            if source["url"].endswith("failed"):
                return [{"key": "region", "value": "上海", "evidence": "造出的假引文", "basis": "reported"}]
            return [{"key": "industry", "value": "金融", "evidence": "行业是金融", "basis": "reported"}]
    service.researcher, service.analyzer = Researcher(), Extract()
    receipt = asyncio.run(service.research("owner", customer["id"]))
    assert receipt["status"] == "partial" and receipt["search_status"] == "complete"
    assert receipt["sources"] == 2 and receipt["analysis_failed_sources"] == 1 and receipt["rejected_sources"] == 1
    assert receipt["created"] == 1 and receipt["error"]
    assert {item["key"] for item in candidates(service)} == {"industry"}
    assert crm._db.execute("SELECT count(*) FROM crm_profile_public_sources WHERE owner='owner'").fetchone()[0] == 2
    assert crm.profile("owner", customer["id"])["fields"] == []


def test_public_official_domain_still_requires_verification_and_preserves_dates(services):
    crm, _, service, customer, _, _, _, _ = services
    service.configure("owner", {"official_domains": ["https://bank.example.com/"]}, customer["id"])
    result = asyncio.run(service.import_public_source("owner", customer["id"], {
        "url": "https://bank.example.com/about", "title": "机构简介", "text": "行业是金融。总部位于上海。",
        "entity_name": customer["name"], "published_at": "2020-01-02", "fetched_at": NOW}))
    assert result["identity_verified"]
    item = select(service, "industry")
    assert item["basis"] == "observation" and item["requires_public_verification"]
    assert item["source"]["published_at"] == "2020-01-02" and item["source"]["fetched_at"] == NOW
    with pytest.raises(ValueError, match="时效"):
        confirm(service, item)
    with pytest.raises(ValueError, match="只能"):
        confirm(service, item, verify_public=True, basis="reported")
    confirm(service, item, verify_public=True)
    assert crm.profile("owner", customer["id"])["fields"][0]["basis"] == "observation"
    again = asyncio.run(service.import_public_source("owner", customer["id"], {
        "url": "https://bank.example.com/about", "title": "机构简介", "text": "行业是金融。总部位于上海。",
        "entity_name": customer["name"], "published_at": "2020-01-02", "fetched_at": NOW + 10}))
    assert again["source_id"] == result["source_id"]


def test_public_fullname_alone_needs_identity_confirmation_and_wrong_entity_rejected(services):
    _, _, service, customer, _, _, _, _ = services
    asyncio.run(service.import_public_source("owner", customer["id"], {
        "url": "https://news.example.com/item", "title": customer["name"], "text": "行业是金融。"}))
    item = select(service, "industry")
    assert item["requires_entity_confirmation"]
    with pytest.raises(ValueError, match="同名"):
        confirm(service, item, verify_public=True)
    confirm(service, item, verify_public=True, verify_entity=True)
    with pytest.raises(ValueError, match="单位全名"):
        asyncio.run(service.import_public_source("owner", customer["id"], {
            "url": "https://news.example.com/wrong", "title": "星河银行", "text": "行业是金融。", "entity_name": "星河银行"}))


@pytest.mark.parametrize("url", ["http://127.0.0.1/info", "http://192.168.1.131/", "file:///secret", "https://user:password@example.com/", "http://localhost/", "http://company.internal/"])
def test_public_import_rejects_non_public_urls(services, url):
    _, _, service, customer, _, _, _, _ = services
    with pytest.raises(ValueError):
        asyncio.run(service.import_public_source("owner", customer["id"], {"url": url, "title": customer["name"], "text": "行业是金融。"}))


def test_research_rate_limit_sanitization_and_private_context_boundary(services):
    crm, _, service, customer, _, _, _, clock = services
    note(crm, customer, "内部机密预算30万元。")
    calls = []
    class Researcher:
        async def research(self, context):
            calls.append(context)
            return [{"url": "https://news.example.com/item", "title": customer["name"], "text": "行业是金融。", "entity_name": customer["name"], "published_at": "2021-01-01", "fetched_at": NOW}]
    service.researcher = Researcher()
    assert asyncio.run(service.research("owner", customer["id"]))["sources"] == 1
    assert "内部机密" not in str(calls) and "王工" not in str(calls)
    assert asyncio.run(service.research("owner", customer["id"], force=True))["limited"] is True
    clock[0] += 86401
    assert asyncio.run(service.research("owner", customer["id"]))["sources"] == 1


def test_research_failure_retry_and_unconfigured_honest(services):
    _, _, service, customer, _, _, _, _ = services
    assert asyncio.run(service.research("owner", customer["id"]))["configured"] is False
    class Bad:
        async def research(self, context):
            raise RuntimeError("SECRET TOKEN PRIVATE PAYLOAD")
    service.researcher = Bad()
    result = asyncio.run(service.research("owner", customer["id"]))
    assert result["error"] and "SECRET" not in str(result)
    assert service.status("owner", customer["id"])["customer_settings"]["last_research_at"] is None


def test_stage_specific_top_three_six_total_and_completed_fields_not_asked(services):
    crm, workspace, service, customer, _, first, second, _ = services
    plan = service.profile_plan("owner", customer["id"])
    assert len(plan["items"]) == 6
    assert all(len(group["items"]) == 3 for group in plan["projects"])
    assert plan["items"][0]["field_key"] == "budget_approval"  # proposal stage
    source = note(crm, customer, "预算部门已经审批预算30万元。")
    workspace.link("owner", "record", source["id"], first["id"])
    scan(service, force=True)
    confirm(service, select(service, "budget_approval"))
    assert "budget_approval" not in {item["field_key"] for item in service.profile_plan("owner", customer["id"], first["id"])["items"]}
    assert service.profile_plan("owner", customer["id"], second["id"])["completion"]["known"] == 0


def test_question_adoption_idempotent_explicit_project_and_no_schedule(services):
    crm, _, service, customer, _, first, _, _ = services
    question = service.profile_plan("owner", customer["id"], first["id"])["items"][0]
    assert crm.list_records("owner")["total"] == 0
    adopted = service.adopt_question("owner", customer["id"], question["key"])
    assert adopted["created"] and adopted["record"]["kind"] == "action"
    assert not service.adopt_question("owner", customer["id"], question["key"])["created"]
    assert crm._db.execute("SELECT opportunity_id FROM crm_opportunity_links WHERE entity_id=?", (adopted["record"]["id"],)).fetchone()[0] == first["id"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    with pytest.raises(ProfileConflict):
        service.adopt_question("owner", customer["id"], question["key"], {"title": "另一个不同内容"})


@pytest.mark.parametrize("placeholder", ["待确认", "待核实", "不确定", "未知"])
@pytest.mark.parametrize("field,key,stage,clear", [
    ("scope", "requirements", "lead", "本次仅验证签名接口适配，不包含终端改造"),
    ("milestones", "timeline", "won", "本次已核对：10月完成接口验证，11月切换上线"),
    ("procurement", "procurement_process", "negotiation", "采购处组织公开招标，财务核验采购资金")])
def test_placeholder_project_text_does_not_close_discovery_and_clear_text_still_counts(services, placeholder, field, key, stage, clear):
    crm, workspace, service, customer, _, _, _, _ = services
    project = workspace.create_opportunity("owner", customer["id"], {
        "name": "合成占位文本核对项目", "stage": stage, field: placeholder})
    plan = service.profile_plan("owner", customer["id"], project["id"])
    question_keys = {item["field_key"] for item in plan["items"]}
    if key != "procurement_process":
        assert key in question_keys
    else:
        # Procurement remains part of the decision-chain question in the
        # current product; it is not a separate rendered question yet.
        assert "decision_process" in question_keys
    assert plan["completion"]["known"] == 0
    assert not service._project_field_known(project, key)
    updated = workspace.update_opportunity("owner", customer["id"], project["id"], {
        "expected_revision": project["revision"], field: clear})
    assert service._project_field_known(updated, key)
    clarified = service.profile_plan("owner", customer["id"], project["id"])
    if key != "procurement_process":
        assert clarified["completion"]["known"] == 1
        assert key not in {item["field_key"] for item in clarified["items"]}
    else:
        assert clarified["completion"]["known"] == 0
        assert "decision_process" in {item["field_key"] for item in clarified["items"]}
    assert crm.profile("owner", customer["id"])["fields"] == []
    for table in ("crm_records", "tasks", "proposals", "notifications"):
        assert crm._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_provider_json_adapter_only_cited_attributes_and_sanitized_failures():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"attributes": [{"key": "industry", "value": "金融", "evidence": "行业是金融", "basis": "reported"}]})}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = ProfileAnalyzer("not-a-real-key", client=client)
            return await adapter.extract({"text": "行业是金融。"}, {"customer": {"name": "虚构单位"}})
    assert asyncio.run(run())[0]["key"] == "industry"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "更新画像命令" in requests[0]["messages"][0]["content"]


def test_auto_research_opt_in_trusted_domain_seven_day_period_and_baseline(services):
    _, _, service, customer, _, _, _, clock = services
    calls = []
    class Researcher:
        async def research(self, context):
            calls.append(context)
            return []
    service.researcher = Researcher()
    service.configure("owner", {"auto_research": True, "official_domains": ["bank.example.com"]}, customer["id"])
    assert scan(service)["baselined"]
    assert calls == []
    scan(service)
    assert len(calls) == 1
    clock[0] += 86401
    scan(service)
    assert len(calls) == 1
    clock[0] += 7 * 86400
    scan(service)
    assert len(calls) == 2
    service.configure("owner", {"enabled": False})
    clock[0] += 8 * 86400
    scan(service)
    assert len(calls) == 2


def test_failed_sources_stop_after_five_automatic_attempts_manual_retry_resets(services):
    crm, _, service, customer, _, _, _, clock = services
    class Bad:
        async def extract(self, source, context):
            raise RuntimeError("failed")
    service.analyzer = Bad()
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    for _ in range(4):
        clock[0] += 3601
        scan(service)
    assert service.status("owner")["exhausted_count"] == 1
    clock[0] += 86400
    assert scan(service)["failed"] == 0
    service.analyzer = None
    assert scan(service, force=True)["created"] == 1


def test_inferred_participation_not_decision_coverage_but_verified_roles_count(services):
    _, workspace, service, customer, person, first, _, _ = services
    first = workspace.update_opportunity("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_ids": [person["id"]], "stage": "negotiation"})
    assert service.profile_plan("owner", customer["id"], first["id"])["items"][0]["field_key"] == "decision_process"
    first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {
        "expected_revision": first["revision"], "contact_id": person["id"],
        "roles": ["final_approver", "technical_reviewer", "business_owner"], "engagement": "direct",
        "basis": "reported", "evidence": "王工明确说本项目由自己终审、技术评审和业务验收", "verified_at": NOW})
    assert "decision_process" not in {item["field_key"] for item in service.profile_plan("owner", customer["id"], first["id"])["items"]}


def test_select_project_scope_preview_existing_fact_and_explicit_snapshot_override(services):
    crm, workspace, service, customer, _, first, _, _ = services
    source = note(crm, customer, "预算30万元。")
    workspace.link("owner", "record", source["id"], first["id"])
    scan(service, force=True)
    old = confirm(service, select(service, "budget_notes"))
    note(crm, customer, "预算50万元。")
    scan(service)
    pending = select(service, "budget_notes")
    assert pending["opportunity_id"] is None
    preview = service.preview_candidate("owner", pending["id"], {"opportunity_id": first["id"]})
    assert preview["current_fact_id"] == old["confirmed_fact_id"] and preview["conflict"]
    assert preview["requires_scope_confirmation"] is False
    assert service.get_candidate("owner", pending["id"])["opportunity_id"] is None
    confirmed = confirm(service, pending, opportunity_id=first["id"], expected_fact_id=preview["current_fact_id"])
    assert confirmed["status"] == "confirmed"
    assert len(service.project_facts("owner", customer["id"], first["id"])["history"]) == 2


def test_preview_current_fact_snapshot_catches_second_writer(services):
    crm, _, service, customer, _, _, _, _ = services
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    item = select(service, "industry")
    preview = service.preview_candidate("owner", item["id"])
    assert preview["current_fact_id"] is None
    crm.save_fact("owner", customer["id"], {"key": "industry", "value": "保险", "basis": "reported"}, NOW + 1)
    with pytest.raises(ProfileConflict):
        confirm(service, item, expected_fact_id=preview["current_fact_id"])
    assert crm.profile("owner", customer["id"])["fields"][0]["value"] == "保险"


def test_preview_wrong_owner_scope_and_foreign_project_rejected(services):
    crm, workspace, service, customer, _, first, _, _ = services
    note(crm, customer, "预算尚未审批。")
    scan(service, force=True)
    item = select(service, "budget_notes")
    assert service.preview_candidate("bob", item["id"], {"opportunity_id": first["id"]}) is None
    other = crm.create_customer("owner", {"name": "另一医院"}, NOW)
    foreign = workspace.create_opportunity("owner", other["id"], {"name": "另一项目"})
    with pytest.raises(KeyError):
        service.preview_candidate("owner", item["id"], {"opportunity_id": foreign["id"]})
    with pytest.raises(ValueError):
        service.preview_candidate("owner", item["id"], {"contact_id": 1})


def test_model_summary_cannot_drop_source_negation(services):
    crm, _, service, customer, _, _, _, _ = services
    class Inverting:
        async def extract(self, source, context):
            return [{"key": "budget_approval", "value": "预算已审批", "evidence": "预算尚未审批", "basis": "reported"}]
    service.analyzer = Inverting()
    note(crm, customer, "预算尚未审批。")
    assert scan(service, force=True)["failed"] == 1
    assert candidates(service) == []


def test_manual_project_fact_direct_discovery_and_history_cas_no_schedule(services):
    crm, _, service, customer, _, first, second, _ = services
    data = {"key": "success_criteria", "value": "高峰吞吐量1000笔每秒，技术部验收", "basis": "reported", "evidence": "客户技术负责人当面确认"}
    saved = service.save_project_fact("owner", customer["id"], first["id"], data)
    assert saved["created"] and saved["fact"]["candidate_id"] is None
    assert saved["fact"]["source"]["type"] == "manual"
    assert saved["fact"]["source"]["occurred_at"] is None and saved["fact"]["source"]["recorded_at"] == NOW
    assert "success_criteria" not in {item["field_key"] for item in service.profile_plan("owner", customer["id"], first["id"])["items"]}
    assert service.project_facts("owner", customer["id"], second["id"])["items"] == []
    with pytest.raises(ProfileConflict):
        service.save_project_fact("owner", customer["id"], first["id"], {**data, "value": "新的指标"})
    updated = service.save_project_fact("owner", customer["id"], first["id"], {**data, "value": "新指标2000笔每秒", "expected_fact_id": saved["fact"]["id"]})
    assert len(updated["project_facts"]["history"]) == 2
    with pytest.raises(ProfileConflict):
        service.save_project_fact("owner", customer["id"], first["id"], {**data, "expected_fact_id": saved["fact"]["id"]})
    noop = service.save_project_fact("owner", customer["id"], first["id"], {**data, "value": "新指标2000笔每秒", "expected_fact_id": updated["fact"]["id"]})
    assert noop["created"] is False
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.parametrize("data", [{"key": "industry", "value": "金融", "basis": "reported"},
                                {"key": "authority", "value": "王总审批", "basis": "reported"},
                                {"key": "budget_notes", "value": "", "basis": "reported"},
                                {"key": "budget_notes", "value": "估算", "basis": "confirmed"}])
def test_manual_project_fact_field_guard(services, data):
    _, _, service, customer, _, first, _, _ = services
    with pytest.raises(ValueError):
        service.save_project_fact("owner", customer["id"], first["id"], data)


def test_manual_project_fact_foreign_owner_and_archived_rejected(services):
    _, workspace, service, customer, _, first, _, _ = services
    data = {"key": "budget_notes", "value": "未核实", "basis": "observation"}
    with pytest.raises(KeyError):
        service.save_project_fact("bob", customer["id"], first["id"], data)
    workspace.update_opportunity("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "archived": True})
    with pytest.raises(ValueError, match="归档"):
        service.save_project_fact("owner", customer["id"], first["id"], data)


def test_context_excludes_other_contact_phones_internal_notes_and_amount(services):
    crm, workspace, service, customer, person, first, _, _ = services
    crm.update_contact("owner", customer["id"], person["id"], {"phone": "13812345678", "department": "信息技术部"}, NOW)
    workspace.update_opportunity("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "notes": "私人电话号码13812345678", "amount_cents": 30000000})
    received = []
    class Model:
        async def extract(self, source, context):
            received.append(context)
            return []
    service.analyzer = Model()
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    assert "13812345678" not in str(received) and "amount_cents" not in str(received)
    assert received[0]["contacts"][0]["department"] == "信息技术部"


@pytest.mark.parametrize("choice", [{"finish_reason": "length", "message": {"content": '{"attributes":[]}'}},
                                  {"finish_reason": "stop", "message": {"content": '{"attributes":[],"attributes":[]}'}},
                                  {"finish_reason": "stop", "message": {"content": '{"attributes":['}},
                                  {"finish_reason": "stop", "message": {"content": '{"attributes":"bad"}'}}])
def test_provider_rejects_truncated_duplicate_malformed_sanitized(choice):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [choice]}))) as client:
            adapter = ProfileAnalyzer("not-real", client=client)
            with pytest.raises(ValueError, match="暂时失败") as error:
                await adapter.extract({"text": "行业是金融。"}, {})
            assert error.value.__cause__ is None
    asyncio.run(run())


def test_same_name_selected_contact_conflict_repreview_latest_and_resume(services):
    crm, _, service, customer, person, _, _, _ = services
    crm.update_contact("owner", customer["id"], person["id"], {"department": "信息技术部"}, NOW)
    procurement = crm.create_contact("owner", customer["id"], {"name": "王工", "department": "采购部"}, NOW)
    prior = crm.save_fact("owner", customer["id"], {"contact_id": procurement["id"], "key": "concerns", "value": "关注价格", "basis": "reported"}, NOW)
    note(crm, customer, "王工关注稳定性。")
    scan(service, force=True)
    item = select(service, "concerns")
    assert item["contact_id"] is None
    preview = service.preview_candidate("owner", item["id"], {"contact_id": procurement["id"]})
    assert preview["current_fact_id"] == prior["id"]
    latest = crm.save_fact("owner", customer["id"], {"contact_id": procurement["id"], "key": "concerns", "value": "关注交付周期", "basis": "reported"}, NOW + 1)
    with pytest.raises(ProfileConflict, match="原候选已保留"):
        confirm(service, item, contact_id=procurement["id"], expected_fact_id=prior["id"])
    pending = service.get_candidate("owner", item["id"])
    assert pending["status"] == "pending" and pending["contact_id"] is None
    assert pending["decision_history"][-1]["decision"] == "conflict"
    assert scan(service, force=True)["created"] == 0
    assert {candidate["id"] for candidate in candidates(service)} == {item["id"]}
    refreshed = service.preview_candidate("owner", item["id"], {"contact_id": procurement["id"]})
    assert refreshed["current_fact_id"] == latest["id"] and refreshed["current_value"]["value"] == "关注交付周期"
    adopted = confirm(service, refreshed, contact_id=procurement["id"], expected_fact_id=refreshed["current_fact_id"])
    assert adopted["status"] == "confirmed"
    profile = crm.profile("owner", customer["id"])
    assert next(contact for contact in profile["contacts"] if contact["id"] == procurement["id"])["fields"][0]["value"] == "王工关注稳定性"
    assert next(contact for contact in profile["contacts"] if contact["id"] == person["id"])["fields"] == []


def test_project_target_conflict_resume_then_actual_source_change_stales(services):
    crm, _, service, customer, _, first, _, _ = services
    original = service.save_project_fact("owner", customer["id"], first["id"], {"key": "budget_notes", "value": "预算30万元", "basis": "reported"})["fact"]
    source = note(crm, customer, "预算50万元。")
    scan(service, force=True)
    item = select(service, "budget_notes")
    service.save_project_fact("owner", customer["id"], first["id"], {"key": "budget_notes", "value": "预算70万元", "basis": "reported", "expected_fact_id": original["id"]})
    with pytest.raises(ProfileConflict):
        confirm(service, item, opportunity_id=first["id"], expected_fact_id=original["id"])
    assert service.get_candidate("owner", item["id"])["status"] == "pending"
    crm.update_record("owner", source["id"], {"content": "预算尚未审批。"}, NOW + 1)
    assert service.get_candidate("owner", item["id"])["status"] == "stale"
    assert scan(service, force=True)["created"] >= 1
    assert all(candidate["id"] != item["id"] for candidate in candidates(service))


def test_historical_target_conflict_stale_draft_can_be_revalidated_on_force(services):
    crm, _, service, customer, _, _, _, _ = services
    note(crm, customer, "行业是金融。")
    scan(service, force=True)
    item = select(service, "industry")
    with crm._transaction() as db:
        db.execute("UPDATE crm_profile_candidates SET status='stale',reason=? WHERE id=?", ("当前画像或所选范围已有新信息，请刷新核对旧值后重新分析。", item["id"]))
    assert candidates(service) == []
    assert scan(service, force=True)["created"] == 1
    restored = service.get_candidate("owner", item["id"])
    assert restored["status"] == "pending" and restored["revision"] > item["revision"]


def test_unknown_record_activity_and_discussion_occurrence_not_recorded_time(services):
    crm, workspace, service, customer, _, first, _, _ = services
    record = note(crm, customer, "上周交流，行业是金融。")
    crm.add_activity("owner", record["id"], "昨天说预算尚未审批。", NOW + 600)
    discussion = DiscussionService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    thread = discussion.create_thread("owner", {"customer_id": customer["id"], "opportunity_id": first["id"], "title": "补充回忆"})["thread"]
    with crm._transaction() as db:
        db.execute("INSERT INTO crm_sales_discussion_messages(owner,thread_id,role,text,status,created_at,updated_at) VALUES (?,?,'user',?,'complete',?,?)", ("owner", thread["id"], "前天说现有系统正在使用旧版设备。", NOW + 1200, NOW + 1200))
    scan(service, force=True)
    items = candidates(service)
    source_types = {item["source"]["type"] for item in items}
    assert source_types == {"record", "activity", "discussion_user"}
    for item in items:
        source = item["source"]
        assert source["occurred_at"] is None
        expected = {"record": NOW, "activity": NOW + 600, "discussion_user": NOW + 1200}[source["type"]]
        assert source["recorded_at"] == expected
        if source["type"] == "activity":
            assert source["source_record_id"] == record["id"]
        if source["type"] == "discussion_user":
            assert source["thread_id"] == thread["id"]


def test_person_discovery_requires_explicit_project_members_and_does_not_change_project_totals(services):
    crm, workspace, service, customer, person, first, _, _ = services
    before = service.profile_plan("owner", customer["id"])
    assert before["contact_discovery"] == []  # A company primary contact is not a project member.
    first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": person["id"], "roles": ["technical_reviewer"], "basis": "reported", "evidence": "本项目参加技术评审"})
    after = service.profile_plan("owner", customer["id"])
    assert len(after["contact_discovery"]) == 2
    assert after["items"] == before["items"] and after["completion"] == before["completion"]
    assert crm.list_records("owner")["total"] == 0
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_same_named_person_discovery_preserves_department_id_and_roles_per_project(services):
    crm, workspace, service, customer, person, first, second, _ = services
    crm.update_contact("owner", customer["id"], person["id"], {"department": "信息技术部"}, NOW)
    procurement = crm.create_contact("owner", customer["id"], {"name": "王工", "department": "采购部"}, NOW)
    first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": person["id"], "roles": ["technical_reviewer"], "basis": "reported"})
    first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": procurement["id"], "roles": ["procurement"], "basis": "reported"})
    second = workspace.upsert_stakeholder("owner", customer["id"], second["id"], {"expected_revision": second["revision"], "contact_id": person["id"], "roles": ["procurement"], "basis": "reported"})
    a = service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]
    assert len(a) == 2 and {item["contact_id"] for item in a} == {person["id"], procurement["id"]}
    assert {item["contact_department"] for item in a} == {"信息技术部", "采购部"}
    technical = next(item for item in a if item["contact_id"] == person["id"])
    b = service.profile_plan("owner", customer["id"], second["id"])["contact_discovery"][0]
    assert technical["roles"] == ["technical_reviewer"] and "稳定性" in technical["ask"]
    assert b["roles"] == ["procurement"] and "采购合规" in b["ask"]
    assert technical["key"] != b["key"] and technical["scope"] == b["scope"] == "contact"
    assert technical["contact_customer_id"] == customer["id"]


def test_person_discovery_reported_fields_converge_observation_and_unknown_still_asked(services):
    crm, workspace, service, customer, person, first, _, _ = services
    workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": person["id"], "roles": ["technical_reviewer"], "basis": "reported"})
    crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": "professional_goals", "value": "保证业务系统稳定性", "basis": "reported"}, NOW)
    crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": "concerns", "value": "我感觉他更看重价格", "basis": "observation"}, NOW)
    items = service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]
    assert {item["field_key"] for item in items} == {"concerns", "responsibilities"}
    concerns = next(item for item in items if item["field_key"] == "concerns")
    assert concerns["needs_verification"] and concerns["current_basis"] == "observation"
    crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": "concerns", "value": "未知", "basis": "reported"}, NOW + 1)
    assert "concerns" in {item["field_key"] for item in service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]}
    for key, value in (("concerns", "关注稳定性"), ("responsibilities", "负责数据库运维"), ("communication_channel", "先微信发简明材料")):
        crm.save_fact("owner", customer["id"], {"contact_id": person["id"], "key": key, "value": value, "basis": "reported"}, NOW + 2)
    assert service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"] == []


def test_person_discovery_cross_unit_membership_invalid_and_contact_archive_excluded(services):
    crm, workspace, service, customer, _, first, _, _ = services
    other = crm.create_customer("owner", {"name": "集团采购中心"}, NOW)
    person = crm.create_contact("owner", other["id"], {"name": "李工", "department": "采购部"}, NOW)
    first = workspace.upsert_project_unit("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "participant_customer_id": other["id"], "roles": ["procurement"], "evidence": "负责集团采购"})
    first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": person["id"], "roles": ["procurement"], "basis": "reported"})
    items = service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]
    assert len(items) == 2 and all(item["contact_customer_id"] == other["id"] for item in items)
    assert all(item["unit_name"] == "集团采购中心" for item in items)
    first = workspace.archive_project_unit("owner", customer["id"], first["id"], other["id"], {"expected_revision": first["revision"], "archived": True})
    assert service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"] == []
    first = workspace.archive_project_unit("owner", customer["id"], first["id"], other["id"], {"expected_revision": first["revision"], "archived": False})
    crm.update_contact("owner", other["id"], person["id"], {"archived": True}, NOW)
    assert service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"] == []


def test_person_discovery_limits_two_per_project_four_per_company(services):
    _, workspace, service, customer, person, first, second, _ = services
    third = workspace.create_opportunity("owner", customer["id"], {"name": "脱敏项目"})
    for project in (first, second, third):
        workspace.upsert_stakeholder("owner", customer["id"], project["id"], {"expected_revision": project["revision"], "contact_id": person["id"], "roles": [], "basis": "reported"})
        assert len(service.profile_plan("owner", customer["id"], project["id"])["contact_discovery"]) == 2
    overall = service.profile_plan("owner", customer["id"])
    assert len(overall["contact_discovery"]) == 4
    assert len(overall["items"]) == 6
    assert all("审批权" not in item["ask"] for item in overall["contact_discovery"])


def test_person_discovery_prioritizes_known_roles_current_contact_without_title_inference(services):
    crm, workspace, service, customer, person, first, _, _ = services
    crm.update_contact("owner", customer["id"], person["id"], {"role": "总经理"}, NOW)
    second = crm.create_contact("owner", customer["id"], {"name": "采购联系人"}, NOW)
    third = crm.create_contact("owner", customer["id"], {"name": "技术联系人"}, NOW)
    fourth = crm.create_contact("owner", customer["id"], {"name": "尚未联系的人"}, NOW)
    for contact, fields in ((person, {"roles": []}),
        (second, {"roles": ["procurement"], "engagement": "direct", "influence": "medium"}),
        (third, {"roles": ["technical_reviewer"], "engagement": "direct", "influence": "high"}),
        (fourth, {"roles": ["final_approver"], "engagement": "not_contacted", "influence": "high"})):
        first = workspace.upsert_stakeholder("owner", customer["id"], first["id"], {"expected_revision": first["revision"], "contact_id": contact["id"], "basis": "reported", **fields})
    items = service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]
    assert [item["contact_id"] for item in items] == [third["id"], second["id"]]
    # A title without a confirmed project role never yields assumed authority.
    for key, value in (("professional_goals", "保证业务稳定性"), ("concerns", "技术质量"), ("responsibilities", "参与交付"), ("communication_channel", "微信")):
        for contact in (second, third, fourth):
            crm.save_fact("owner", customer["id"], {"contact_id": contact["id"], "key": key, "value": value, "basis": "reported"}, NOW)
    remaining = service.profile_plan("owner", customer["id"], first["id"])["contact_discovery"]
    assert all(item["contact_id"] == person["id"] for item in remaining)
    assert "预算责任" not in remaining[0]["why"] and "审批" not in remaining[0]["ask"]
