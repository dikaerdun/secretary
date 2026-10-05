"""Round05: archived-project legacy membership cannot bypass relationship guards.

All fixtures use fresh local synthetic SQLite; web coverage invokes the actual
handler directly and deliberately does not start an HTTP server or workers.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from secretary.account_network import AccountNetwork
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.web import WebCRM


NOW, OWNER = 1_800_200_000.0, "round05-membership-synthetic"


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / "round05-membership.sqlite3"
    crm = CustomerStore(path)
    clock = [NOW]
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    network = AccountNetwork(crm, sales)
    group = network.create_unit(OWNER, {"name": "合成集团"}, {"unit_type": "group"})
    unit = network.create_unit(OWNER, {"name": "合成下属单位"}, {"parent_customer_id": group["id"]})
    alpha = crm.create_contact(OWNER, unit["id"], {"name": "技术甲", "role": "信息中心主任"}, NOW)
    beta = crm.create_contact(OWNER, group["id"], {"name": "采购乙", "role": "采购经理"}, NOW)
    external = network.create_unit(OWNER, {"name": "合成独立测评单位"})
    expert = crm.create_contact(OWNER, external["id"], {"name": "测评专家"}, NOW)
    foreign_unit = network.create_unit("other-owner", {"name": "其他成员私有单位"})
    foreign = crm.create_contact("other-owner", foreign_unit["id"], {"name": "其他成员联系人"}, NOW)
    project = sales.create_opportunity(OWNER, unit["id"], {"name": "合成数据库加密"})
    project = sales.upsert_stakeholder(OWNER, unit["id"], project["id"], {
        "expected_revision": project["revision"], "contact_id": alpha["id"],
        "roles": ["technical_reviewer"], "evidence": "甲明确说负责本项目技术评审"})
    yield SimpleNamespace(path=path, crm=crm, sales=sales, network=network, clock=clock,
        group=group, unit=unit, alpha=alpha, beta=beta, external=external, expert=expert,
        foreign=foreign, project=project)
    crm.close()


def current(rig):
    return next(item for item in rig.sales.opportunities(OWNER, rig.unit["id"], True)["items"]
                if item["id"] == rig.project["id"])


def update(rig, **body):
    return rig.sales.update_opportunity(OWNER, rig.unit["id"], rig.project["id"], {
        "expected_revision": current(rig)["revision"], **body})


def add_beta_history(rig):
    project = current(rig)
    project = rig.sales.upsert_stakeholder(OWNER, rig.unit["id"], project["id"], {
        "expected_revision": project["revision"], "contact_id": rig.beta["id"],
        "roles": ["procurement"], "evidence": "乙说明协助本项目采购"})
    rig.sales.archive_stakeholder(OWNER, rig.unit["id"], project["id"], rig.beta["id"], {
        "expected_revision": project["revision"], "archived": True})


def archive(rig):
    return update(rig, archived=True)


def snapshot(rig):
    """Include durable rows and revision-trigger side effects, not only DTOs."""
    tables = {
        "crm_opportunities": "id", "crm_opportunity_stakeholders": "owner,opportunity_id,contact_id",
        "crm_opportunity_units": "owner,opportunity_id,participant_customer_id",
        "crm_opportunity_people_history": "id", "crm_customer_revisions": "owner,customer_id",
        "tasks": "id", "proposals": "id",
    }
    return {table: [tuple(row) for row in rig.crm._db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in tables.items()}


@pytest.mark.parametrize("change", ["remove", "new_member", "restore_relation"])
@pytest.mark.parametrize("keep_archived", [False, True])
def test_archived_legacy_member_delta_is_rejected_without_side_effects(rig, change, keep_archived):
    if change == "restore_relation":
        add_beta_history(rig)
    archive(rig)
    rig.clock[0] += 60
    before = snapshot(rig)
    body = {"contact_ids": [] if change == "remove" else [rig.alpha["id"], rig.beta["id"]],
            "scope": "这个其他字段也不能在失败时部分保存"}
    if keep_archived:
        body["archived"] = True
    with pytest.raises(ValueError, match="已归档项目.*关系"):
        update(rig, **body)
    assert snapshot(rig) == before


@pytest.mark.parametrize("keep_archived", [False, True])
def test_archived_legacy_unchanged_members_are_noop_and_revision_stable(rig, keep_archived):
    project = archive(rig)
    rig.clock[0] += 60
    before = snapshot(rig)
    body = {"contact_ids": project["contact_ids"]}
    if keep_archived:
        body["archived"] = True
    assert update(rig, **body) == project
    assert snapshot(rig) == before


@pytest.mark.parametrize("send_unchanged_members", [False, True])
def test_archived_metadata_edit_does_not_needlessly_reject_legacy_client(rig, send_unchanged_members):
    project = archive(rig)
    before_history = rig.sales.relation_history(OWNER, rig.unit["id"], project["id"])
    body = {"notes": "已归档项目仍可核对历史资料"}
    if send_unchanged_members:
        body["contact_ids"] = project["contact_ids"]
    result = update(rig, **body)
    assert result["archived"] and result["notes"] == body["notes"]
    assert result["revision"] == project["revision"] + 1
    assert result["stakeholders"] == project["stakeholders"]
    assert rig.sales.relation_history(OWNER, rig.unit["id"], project["id"]) == before_history


@pytest.mark.parametrize("change", ["unchanged", "remove", "new_member", "restore_relation"])
def test_explicit_project_restore_allows_legal_membership_edit_and_preserves_roles(rig, change):
    if change == "restore_relation":
        add_beta_history(rig)
    archived = archive(rig)
    ids = [] if change == "remove" else [rig.alpha["id"]]
    if change in ("new_member", "restore_relation"):
        ids.append(rig.beta["id"])
    result = update(rig, archived=False, contact_ids=ids)
    assert not result["archived"] and result["contact_ids"] == ids
    assert result["revision"] == archived["revision"] + 1
    relations = {person["contact_id"]: person for person in result["stakeholders"]}
    if rig.alpha["id"] in ids:
        assert relations[rig.alpha["id"]]["roles"] == ["technical_reviewer"]
        assert relations[rig.alpha["id"]]["evidence"] == "甲明确说负责本项目技术评审"
    else:
        assert result["stakeholders_history"][0]["roles"] == ["technical_reviewer"]
    if change == "restore_relation":
        assert relations[rig.beta["id"]]["roles"] == ["procurement"]
        assert relations[rig.beta["id"]]["evidence"] == "乙说明协助本项目采购"
    elif change == "new_member":
        assert relations[rig.beta["id"]]["roles"] == []


def test_active_project_legacy_edit_and_atomic_archiving_still_work(rig):
    before = current(rig)
    result = update(rig, contact_ids=[rig.alpha["id"], rig.beta["id"]], archived=True)
    assert result["archived"] and result["revision"] == before["revision"] + 1
    assert {person["contact_id"] for person in result["stakeholders"]} == {rig.alpha["id"], rig.beta["id"]}
    assert result["stakeholders"][0]["roles"] == ["technical_reviewer"]


@pytest.mark.parametrize("method", ["upsert", "archive"])
def test_dedicated_archived_relationship_guard_remains(rig, method):
    project = archive(rig)
    before = snapshot(rig)
    with pytest.raises(ValueError, match="已归档项目"):
        if method == "upsert":
            rig.sales.upsert_stakeholder(OWNER, rig.unit["id"], project["id"], {
                "expected_revision": project["revision"], "contact_id": rig.alpha["id"], "roles": ["user"]})
        else:
            rig.sales.archive_stakeholder(OWNER, rig.unit["id"], project["id"], rig.alpha["id"], {
                "expected_revision": project["revision"], "archived": True})
    assert snapshot(rig) == before


@pytest.mark.parametrize("attempt", ["foreign_member", "foreign_project", "external_undocumented", "external_archived_unit", "archived_contact"])
def test_project_restore_keeps_owner_external_and_contact_validity_boundaries(rig, attempt):
    if attempt == "archived_contact":
        rig.crm.update_contact(OWNER, rig.unit["id"], rig.alpha["id"], {"archived": True}, NOW)
    if attempt == "external_archived_unit":
        rig.sales.upsert_project_unit(OWNER, rig.unit["id"], rig.project["id"], {
            "participant_customer_id": rig.external["id"], "expected_revision": current(rig)["revision"],
            "roles": ["technical"], "evidence": "客户明确委托测评"})
        # Archived participation no longer permits external members, even when
        # project restoration itself is explicitly requested.
        rig.sales.archive_project_unit(OWNER, rig.unit["id"], rig.project["id"], rig.external["id"], {
            "expected_revision": current(rig)["revision"], "archived": True})
    project = archive(rig)
    before = snapshot(rig)
    contact = rig.foreign if attempt == "foreign_member" else rig.expert if attempt.startswith("external_") else rig.alpha
    with pytest.raises((ValueError, KeyError)):
        rig.sales.update_opportunity("other-owner" if attempt == "foreign_project" else OWNER,
            rig.unit["id"], project["id"], {"expected_revision": project["revision"],
            "archived": False, "contact_ids": [contact["id"]]})
    assert snapshot(rig) == before


def test_explicit_restore_accepts_documented_external_member(rig):
    rig.sales.upsert_project_unit(OWNER, rig.unit["id"], rig.project["id"], {
        "participant_customer_id": rig.external["id"], "expected_revision": current(rig)["revision"],
        "roles": ["technical"], "evidence": "客户明确委托测评"})
    archive(rig)
    result = update(rig, archived=False, contact_ids=[rig.alpha["id"], rig.expert["id"]])
    assert not result["archived"]
    assert {person["contact_id"] for person in result["stakeholders"]} == {rig.alpha["id"], rig.expert["id"]}
    assert all(person["membership_valid"] for person in result["stakeholders"])
    assert rig.network.view(OWNER, rig.external["id"])["unit"]["parent_customer_id"] is None


@pytest.mark.parametrize("stale_restore", [False, True])
def test_independent_connection_archive_invalidates_old_member_edit_before_archive_guard(rig, stale_restore):
    old = current(rig)
    other_crm = CustomerStore(rig.path)
    try:
        other_sales = SalesWorkspace(other_crm, clock=lambda: NOW + 60)
        archived = other_sales.update_opportunity(OWNER, rig.unit["id"], old["id"], {
            "expected_revision": old["revision"], "archived": True})
        before = snapshot(rig)
        body = {"expected_revision": old["revision"], "contact_ids": []}
        if stale_restore:
            body["archived"] = False
        with pytest.raises(ValueError, match="商机已更新"):
            rig.sales.update_opportunity(OWNER, rig.unit["id"], old["id"], body)
        assert snapshot(rig) == before and current(rig) == archived
    finally:
        other_crm.close()


@pytest.mark.parametrize("explicit_restore", [False, True])
def test_actual_web_handler_applies_same_guard_without_http_server(rig, explicit_restore):
    project = archive(rig)
    before = snapshot(rig)
    api = WebCRM.__new__(WebCRM)
    api.owner, api.lock, api.sales_workspace = OWNER, asyncio.Lock(), rig.sales
    body = {"expected_revision": project["revision"], "contact_ids": []}
    if explicit_restore:
        body["archived"] = False

    async def read_body(request):
        return dict(body)

    api.body = read_body
    request = SimpleNamespace(match_info={"id": str(rig.unit["id"]), "opp_id": str(project["id"])})
    if explicit_restore:
        response = asyncio.run(api.opportunity_update(request))
        result = json.loads(response.text)["opportunity"]
        assert response.status == 200 and not result["archived"] and result["contact_ids"] == []
        assert result["stakeholders_history"][0]["roles"] == ["technical_reviewer"]
    else:
        with pytest.raises(ValueError, match="已归档项目.*关系"):
            asyncio.run(api.opportunity_update(request))
        assert snapshot(rig) == before
