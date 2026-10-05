"""Synthetic unit trees; no existing records or external services."""
import pytest

from secretary.account_network import AccountNetwork, MAX_DEPTH
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0


@pytest.fixture
def context(tmp_path):
    crm = CustomerStore(tmp_path / "unit-network.sqlite3")
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    network = AccountNetwork(crm, workspace)
    yield crm, workspace, network
    crm.close()


def unit(network, name, parent=None, kind="company", owner="owner", **fields):
    return network.create_unit(owner, {"name": name, **fields}, {"parent_customer_id": parent, "unit_type": kind})


def test_legacy_defaults_preserve_customer_contact_project_identity(context):
    crm, workspace, network = context
    old = crm.create_customer("owner", {"name": "旧单位", "contact": "甲", "notes": "原资料", "amount_cents": 70000}, NOW)
    person = crm.profile("owner", old["id"])["contacts"][0]
    project = workspace.create_opportunity("owner", old["id"], {"name": "原项目", "contact_ids": [person["id"]]})
    view = network.view("owner", old["id"])
    assert view["unit"]["revision"] == 0
    assert view["unit"]["unit_type"] == "company"
    assert view["unit"]["parent_customer_id"] is None
    assert view["contacts"][0]["id"] == person["id"]
    assert view["projects"][0]["id"] == project["id"]
    assert crm.get_customer("owner", old["id"]) == old
    assert network.update("owner", old["id"], {"expected_revision": 0, "unit_type": "company"}) == view["unit"]
    assert workspace.opportunities("owner", old["id"])["items"][0] == project


def test_group_company_department_view_aggregates_only_descendants(context):
    crm, workspace, network = context
    group = unit(network, "合成集团", kind="group")
    company = unit(network, "子公司", group["id"])
    dept = unit(network, "信息部", company["id"], "department")
    sibling = unit(network, "另一子公司", group["id"])
    outsider = unit(network, "独立单位")
    for item in (group, company, dept, sibling, outsider):
        crm.create_contact("owner", item["id"], {"name": item["name"]+"联系人"}, NOW)
        workspace.create_opportunity("owner", item["id"], {"name": item["name"]+"项目"})
    group_view = network.view("owner", group["id"])
    assert group_view["contact_count"] == group_view["project_count"] == 4
    assert {item["unit_customer_id"] for item in group_view["contacts"]} == {group["id"], company["id"], dept["id"], sibling["id"]}
    assert all(item["unit_name"] for item in group_view["projects"])
    company_view = network.view("owner", company["id"])
    assert company_view["contact_count"] == 2
    assert [item["id"] for item in company_view["ancestors"]] == [group["id"]]
    assert [item["id"] for item in network.view("owner", dept["id"])["ancestors"]] == [company["id"], group["id"]]


def test_archive_filters_are_explicit_and_keep_records(context):
    crm, workspace, network = context
    company = unit(network, "测试机构", kind="institution")
    person = crm.create_contact("owner", company["id"], {"name": "归档联系人"}, NOW)
    project = workspace.create_opportunity("owner", company["id"], {"name": "归档项目"})
    crm.update_contact("owner", company["id"], person["id"], {"archived": True}, NOW)
    workspace.update_opportunity("owner", company["id"], project["id"], {"expected_revision": 1, "archived": True})
    assert network.view("owner", company["id"])["contacts"] == []
    assert network.view("owner", company["id"])["projects"] == []
    assert network.view("owner", company["id"], True)["contact_count"] == 1
    assert network.view("owner", company["id"], True)["project_count"] == 1


@pytest.mark.parametrize("data", [
    {"unit_type": "bogus"}, {"parent_customer_id": True}, {"parent_customer_id": 0},
    {"website": "https://invalid.example"}, {"unit_type": []},
])
def test_invalid_create_is_atomic_including_primary_contact(context, data):
    crm, _, network = context
    with pytest.raises((ValueError, KeyError)):
        network.create_unit("owner", {"name": "不应留下", "contact": "不应留下的人"}, data)
    assert crm.list_customers("owner")["total"] == 0
    assert crm._db.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0] == 0


def test_foreign_or_missing_parent_is_atomic_and_owner_isolation(context):
    crm, _, network = context
    foreign = unit(network, "其他成员单位", owner="other")
    for parent in (foreign["id"], 99999):
        with pytest.raises(KeyError):
            unit(network, "不能创建", parent, contact="不能留下的人")
    assert network.units("owner")["items"] == []
    with pytest.raises(KeyError):
        network.view("owner", foreign["id"])
    with pytest.raises(KeyError):
        network.update("owner", foreign["id"], {"expected_revision": 1, "unit_type": "group"})
    assert crm.list_customers("other")["total"] == 1


def test_self_parent_cycle_and_subtree_cycle_roll_back(context):
    _, _, network = context
    group = unit(network, "集团", kind="group")
    child = unit(network, "分支", group["id"])
    grandchild = unit(network, "部门", child["id"], "department")
    before = network.units("owner")
    for parent in (group["id"], child["id"], grandchild["id"]):
        with pytest.raises(ValueError, match="循环"):
            network.update("owner", group["id"], {"expected_revision": 1, "parent_customer_id": parent})
        assert network.units("owner") == before


def test_bounded_depth_rollback_on_create(context):
    crm, _, network = context
    parent = unit(network, "层0")
    for index in range(MAX_DEPTH-1):
        parent = unit(network, "层"+str(index+1), parent["id"])
    with pytest.raises(ValueError, match="超过"):
        unit(network, "不能超深", parent["id"], contact="不能留下")
    assert crm.list_customers("owner")["total"] == MAX_DEPTH
    assert crm._db.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0] == 0


def test_move_tree_invalidates_only_affected_projects_noop_is_stable(context):
    _, workspace, network = context
    group1 = unit(network, "集团甲", kind="group")
    group2 = unit(network, "集团乙", kind="group")
    child = unit(network, "子公司", group1["id"])
    unrelated = unit(network, "不相关单位")
    originals = {item["id"]: workspace.create_opportunity("owner", item["id"], {"name": item["name"]+"项目"})
                 for item in (group1, group2, child, unrelated)}
    moved = network.update("owner", child["id"], {"expected_revision": 1, "parent_customer_id": group2["id"]})
    assert moved["revision"] == 2 and moved["root_customer_id"] == group2["id"]
    for item in (group1, group2, child):
        project = workspace.opportunities("owner", item["id"])["items"][0]
        assert project["revision"] == 2
        with pytest.raises(ValueError):
            workspace.update_opportunity("owner", item["id"], project["id"], {"expected_revision": 1, "scope": "过期"})
    assert workspace.opportunities("owner", unrelated["id"])["items"][0] == originals[unrelated["id"]]
    stable = network.units("owner")
    network.update("owner", child["id"], {"expected_revision": 2, "parent_customer_id": group2["id"], "unit_type": "company"})
    assert network.units("owner") == stable
    assert workspace.opportunities("owner", child["id"])["items"][0]["revision"] == 2


@pytest.mark.parametrize("expected", [None, True, 0, 2])
def test_revision_required_and_conflicts_do_not_mutate(context, expected):
    _, _, network = context
    company = unit(network, "单位")
    with pytest.raises(ValueError, match="已更新"):
        network.update("owner", company["id"], {"expected_revision": expected, "unit_type": "group"})
    assert network.view("owner", company["id"])["unit"]["unit_type"] == "company"


def test_reopen_preserves_unit_structure_without_copying_records(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    crm = CustomerStore(path)
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    network = AccountNetwork(crm, workspace)
    group = unit(network, "集团", kind="group")
    child = unit(network, "子公司", group["id"], contact="主联系人", phone="123")
    before = network.view("owner", group["id"])
    crm.close()
    reopened = CustomerStore(path)
    try:
        sales = SalesWorkspace(reopened, clock=lambda: NOW)
        restored = AccountNetwork(reopened, sales)
        assert restored.view("owner", group["id"]) == before
        assert reopened.profile("owner", child["id"])["contacts"][0]["name"] == "主联系人"
    finally:
        reopened.close()
