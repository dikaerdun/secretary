"""Canonical synthetic attribution contexts, with in-memory providers only."""
import asyncio
import copy
import json

import httpx
import pytest

from secretary.crm import CRMStore
from secretary.customer_resolution import CustomerResolutionService, CustomerResolver, MAX_CONTEXT_CHARS
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0


class Recorder:
    available = True
    def __init__(self, result=None, callback=None):
        self.calls = []
        self.result = result or {"items": [], "question": "请核对具体归属。"}
        self.callback = callback
    async def resolve(self, text, context, now):
        self.calls.append((text, copy.deepcopy(context), now))
        if self.callback:
            self.callback()
        return self.result


def pick(unit, project):
    return {"items": [{"customer_id": unit["id"], "opportunity_id": project["id"], "confidence": "high",
                       "reasons": ["按采购部陈工当前项目关系匹配，需核对。"]}], "question": "请确认是这个项目吗？"}


@pytest.fixture
def entities(tmp_path):
    crm = CustomerStore(tmp_path / "synthetic-resolution-context.sqlite3")
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    unit = crm.create_customer("owner", {"name": "合成星河医院", "notes": "不要传出邮箱 chen@example.invalid，电话13800138000"}, NOW)
    technical = crm.create_contact("owner", unit["id"], {"name": "陈工", "role": "业务负责人", "department": "技术部", "phone": "13800138000"}, NOW)
    procurement = crm.create_contact("owner", unit["id"], {"name": "陈工", "role": "业务负责人", "department": "采购部"}, NOW)
    first = workspace.create_opportunity("owner", unit["id"], {"name": "密码项目甲", "contact_ids": [technical["id"]]})
    second = workspace.create_opportunity("owner", unit["id"], {"name": "密码项目乙", "contact_ids": [procurement["id"]]})
    workspace.upsert_stakeholder("owner", unit["id"], second["id"], {"contact_id": procurement["id"], "expected_revision": second["revision"],
                                "roles": ["procurement"], "basis": "reported", "evidence": "本人参与本项目采购"})
    crm.save_fact("owner", unit["id"], {"key": "interests", "contact_id": procurement["id"], "value": "私人兴趣不可发送", "basis": "reported"}, NOW)
    yield crm, workspace, unit, technical, procurement, first, second
    crm.close()


def context_of(entities, recorder, text="采购部陈工负责的那个项目"):
    crm, workspace, *_ = entities
    service = CustomerResolutionService(crm, workspace, asyncio.Lock(), recorder, clock=lambda: NOW)
    result = asyncio.run(service.resolve("owner", text))
    return recorder.calls[0][1], result


def test_model_receives_department_and_valid_project_people_without_private_fields(entities):
    crm, _, unit, _, person, _, project = entities
    before = crm.profile("owner", unit["id"])
    context, result = context_of(entities, Recorder())
    customer = next(item for item in context["customers"] if item["customer_id"] == unit["id"])
    assert {item["department"] for item in customer["contacts"]} == {"技术部", "采购部"}
    item = next(item for item in customer["opportunities"] if item["opportunity_id"] == project["id"])
    participant = next(item for item in item["participants"] if item["contact_id"] == person["id"])
    assert participant["name"] == "陈工" and participant["department"] == "采购部"
    assert participant["roles"] == ["procurement"] and participant["basis"] == "reported"
    wire = json.dumps(context, ensure_ascii=False)
    assert all(value not in wire for value in ("phone", "email", "personal_facts", "13800138000", "chen@example.invalid", "私人兴趣不可发送"))
    assert result["selected_customer_id"] is None and result["requires_confirmation"] is True
    assert crm.profile("owner", unit["id"]) == before


def test_rules_use_department_bound_projects_and_keep_same_name_ambiguity(entities):
    crm, workspace, unit, _, _, first, second = entities
    async def run():
        service = CustomerResolutionService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
        explicit = await service.resolve("owner", "采购部陈工负责的那个项目")
        assert explicit["items"][0]["opportunity_id"] == second["id"]
        ambiguous = await service.resolve("owner", "陈工负责的那个项目")
        assert {item["opportunity_id"] for item in ambiguous["items"]} >= {first["id"], second["id"]}
        assert ambiguous["status"] == "ambiguous" and ambiguous["selected_customer_id"] is None
        assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize("invalidate", ["archive_contact", "remove_membership", "change_roles"])
def test_late_model_cannot_reuse_removed_person_project_basis(entities, invalidate):
    crm, workspace, unit, _, person, _, project = entities
    def changed():
        if invalidate == "archive_contact":
            crm.update_contact("owner", unit["id"], person["id"], {"archived": True}, NOW + 1)
        elif invalidate == "remove_membership":
            current = workspace.stakeholders("owner", unit["id"], project["id"])
            workspace.update_opportunity("owner", unit["id"], project["id"], {"expected_revision": current["project_revision"], "contact_ids": []})
        else:
            current = workspace.stakeholders("owner", unit["id"], project["id"])
            workspace.upsert_stakeholder("owner", unit["id"], project["id"], {"expected_revision": current["project_revision"],
                "contact_id": person["id"], "roles": ["user"], "basis": "observation"})
    _, result = context_of(entities, Recorder(pick(unit, project), changed))
    assert result["method"] != "model", "模型依据的有效人物关系已经失效，不能沿用旧高可信理由"
    assert result["selected_customer_id"] is None
    assert all("当前项目关系匹配" not in reason for item in result["items"] for reason in item["reasons"])


def test_external_valid_member_kept_invalid_and_other_owner_people_excluded(entities):
    crm, workspace, unit, _, _, project, _ = entities
    external = crm.create_customer("owner", {"name": "合成测评机构"}, NOW)
    expert = crm.create_contact("owner", external["id"], {"name": "测评李工", "department": "测评部"}, NOW)
    joined = workspace.upsert_project_unit("owner", unit["id"], project["id"], {"participant_customer_id": external["id"],
        "expected_revision": project["revision"], "roles": ["technical"], "evidence": "客户指定本机构测评"})
    assigned = workspace.upsert_stakeholder("owner", unit["id"], project["id"], {"contact_id": expert["id"],
        "expected_revision": joined["revision"], "roles": ["security_compliance"], "basis": "reported"})
    alien = crm.create_customer("other", {"name": "外owner秘密单位"}, NOW)
    crm.create_contact("other", alien["id"], {"name": "秘密同名人", "department": "私密部门"}, NOW)
    workspace.create_opportunity("other", alien["id"], {"name": "秘密项目"})
    context, _ = context_of(entities, Recorder(), "测评李工负责的测评")
    own = next(item for item in context["customers"] if item["customer_id"] == unit["id"])
    before = next(item for item in own["opportunities"] if item["opportunity_id"] == project["id"])
    assert any(item["contact_id"] == expert["id"] for item in before["participants"])
    assert "秘密" not in json.dumps(context, ensure_ascii=False)
    workspace.archive_project_unit("owner", unit["id"], project["id"], external["id"], {"expected_revision": assigned["revision"], "archived": True})
    after_context, _ = context_of(entities, Recorder(), "测评李工负责的测评")
    own = next(item for item in after_context["customers"] if item["customer_id"] == unit["id"])
    after = next(item for item in own["opportunities"] if item["opportunity_id"] == project["id"])
    assert all(item["contact_id"] != expert["id"] for item in after["participants"])


def test_legacy_store_without_contact_table_keeps_old_contact_hint(tmp_path):
    crm = CRMStore(tmp_path / "synthetic-legacy-resolution.sqlite3")
    try:
        workspace = SalesWorkspace(crm, clock=lambda: NOW)
        unit = crm.create_customer("owner", {"name": "合成旧单位", "contact": "旧陈工"}, NOW)
        recorder = Recorder()
        service = CustomerResolutionService(crm, workspace, asyncio.Lock(), recorder, clock=lambda: NOW)
        result = asyncio.run(service.resolve("owner", "旧陈工上次那件事"))
        own = next(item for item in recorder.calls[0][1]["customers"] if item["customer_id"] == unit["id"])
        assert own["contacts"][0]["name"] == "旧陈工" and own["contacts"][0].get("department", "") == ""
        assert result["requires_confirmation"] is True
    finally:
        crm.close()


def test_legacy_contacts_without_department_column_are_read_without_migration(entities):
    crm, _, _, _, _, _, _ = entities
    # This is a fresh synthetic legacy-schema fixture, never a user database.
    with crm._transaction() as db:
        db.execute("ALTER TABLE crm_contacts DROP COLUMN department")
    context, result = context_of(entities, Recorder())
    assert all(item["department"] == "" for customer in context["customers"] for item in customer["contacts"])
    assert "department" not in {row["name"] for row in crm._db.execute("PRAGMA table_info(crm_contacts)")}
    assert result["requires_confirmation"] is True


def test_exact_project_survives_many_other_projects_with_same_named_member(entities):
    crm, workspace, unit, _, person, target, _ = entities
    workspace.update_opportunity("owner", unit["id"], target["id"], {"expected_revision": target["revision"], "contact_ids": []})
    for number in range(6):
        workspace.create_opportunity("owner", unit["id"], {"name": f"其他密码项目{number}", "contact_ids": [person["id"]]})
    recorder = Recorder()
    context, _ = context_of(entities, recorder, "密码项目甲，采购部陈工说下次再跟进")
    customer = next(item for item in context["customers"] if item["customer_id"] == unit["id"])
    assert len(customer["contacts"]) <= 5 and len(customer["opportunities"]) <= 6
    assert any(item["opportunity_id"] == target["id"] for item in customer["opportunities"]), "明确项目名必须优先于其他项目的共同联系人线索，不能再次被6项上限淘汰"


def test_valid_nested_dimensions_still_reject_oversize_combined_catalogue():
    context = {"customers": []}
    for number in range(60):
        customer = copy.deepcopy(provider_context()["customers"][0])
        customer.update(customer_id=number + 1, name="合成单位" + str(number), notes="合成说明" * 75)
        customer["contacts"] = [{"contact_id": number * 100 + index + 1, "name": "合成名字" * 24, "role": "合成职责" * 40, "department": "合成部门" * 40} for index in range(5)]
        customer["opportunities"] = [{"opportunity_id": number * 100 + index + 1, "name": "合成项目" * 24,
            "scope": "合成范围" * 100, "notes": "合成描述" * 50, "participants": []} for index in range(6)]
        context["customers"].append(customer)
    async def run():
        sent = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request))) as client:
            with pytest.raises(ValueError):
                await CustomerResolver("synthetic", "https://mock.invalid/v1", client=client).resolve("合成项目", context, NOW)
        assert sent == []
    asyncio.run(run())


def provider_context():
    return {"customers": [{"customer_id": 1, "name": "合成单位", "contacts": [{"contact_id": 1, "name": "陈工", "role": "主任", "department": "采购部"}],
        "opportunities": [{"opportunity_id": 1, "name": "合成项目", "participants": [{"contact_id": 1, "name": "陈工", "department": "采购部", "role": "主任", "roles": ["procurement"], "basis": "reported"}]}]}]}


@pytest.mark.parametrize("bad", ["contact_unknown", "department_type", "contact_id_bool", "participant_unknown", "participant_id_zero",
                                      "roles_type", "roles_unknown", "roles_duplicate", "basis_unknown", "participants_count"])
def test_nested_context_rejects_invalid_fields_before_any_transport(bad):
    context = provider_context()
    customer = context["customers"][0]
    person = customer["contacts"][0]
    project = customer["opportunities"][0]
    participant = project["participants"][0]
    if bad == "contact_unknown": person["phone"] = "cannot-send"
    elif bad == "department_type": person["department"] = None
    elif bad == "contact_id_bool": person["contact_id"] = True
    elif bad == "participant_unknown": participant["personal_facts"] = []
    elif bad == "participant_id_zero": participant["contact_id"] = 0
    elif bad == "roles_type": participant["roles"] = "procurement"
    elif bad == "roles_unknown": participant["roles"] = ["unbounded-authority"]
    elif bad == "roles_duplicate": participant["roles"] = ["procurement", "procurement"]
    elif bad == "basis_unknown": participant["basis"] = "inferred"
    elif bad == "participants_count": project["participants"] = [{**participant, "contact_id": index + 1} for index in range(6)]
    async def run():
        sent = []
        def handler(request):
            sent.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[],"question":"核对"}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = CustomerResolver("synthetic", "https://mock.invalid/v1", client=client)
            with pytest.raises(ValueError):
                await resolver.resolve("陈工", context, NOW)
        assert sent == []
    asyncio.run(run())


@pytest.mark.parametrize("legacy", [False, True])
def test_provider_accepts_new_and_old_bounded_catalogues(legacy):
    context = provider_context()
    if legacy:
        customer = context["customers"][0]
        customer["contacts"] = [{"name": "陈工", "role": "主任"}]
        customer["opportunities"][0].pop("participants")
    async def run():
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[],"question":"核对"}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await CustomerResolver("synthetic", "https://mock.invalid/v1", client=client).resolve("陈工", context, NOW)
        assert result["items"] == [] and len(payloads) == 1
        wire = json.loads(payloads[0]["messages"][1]["content"])["context"]
        assert len(json.dumps(wire, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    asyncio.run(run())
