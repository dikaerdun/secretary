"""Project-specific people and participating units on fresh synthetic SQLite."""
import pytest

from secretary.account_network import AccountNetwork
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace, STAKEHOLDER_ROLES


NOW = 1_800_000_000.0


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "stakeholders.sqlite3"
    crm = CustomerStore(path)
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    network = AccountNetwork(crm, sales)
    company = network.create_unit("owner", {"name": "合成客户"})
    person = crm.create_contact("owner", company["id"], {"name": "王总", "role": "信息中心主任"}, NOW)
    project = sales.create_opportunity("owner", company["id"], {"name": "数据加密"})
    yield crm, sales, network, company, person, project, path
    crm.close()


def save(context, person=None, project=None, **values):
    _, sales, _, company, default_person, default_project, _ = context
    person, project = person or default_person, project or default_project
    current = next(item for item in sales.opportunities("owner", project["customer_id"], True)["items"] if item["id"] == project["id"])
    return sales.upsert_stakeholder("owner", project["customer_id"], project["id"], {"contact_id": person["id"], "expected_revision": current["revision"], **values})


def test_person_has_multiple_roles_and_multiple_projects_without_job_pollution(context):
    crm, sales, _, company, person, first, _ = context
    second = sales.create_opportunity("owner", company["id"], {"name": "密钥管理"})
    first = save(context, roles=["technical_reviewer", "final_approver"], stance="supportive", influence="high", evidence="本人说明参与审批")
    second = save(context, project=second, roles=["user"], stance="neutral", concerns="操作易用")
    assert first["stakeholders"][0]["roles"] == ["final_approver", "technical_reviewer"]
    assert second["stakeholders"][0]["roles"] == ["user"]
    assert first["revision"] == second["revision"] == 2
    assert crm.profile("owner", company["id"])["contacts"][0]["role"] == "信息中心主任"
    reverse = sales.contact_projects("owner", person["id"])
    assert {item["opportunity_id"]: item["roles"] for item in reverse["items"]} == {first["id"]: first["stakeholders"][0]["roles"], second["id"]: ["user"]}
    save(context, roles=["economic_buyer"], stance="opposed")
    assert sales.opportunities("owner", company["id"])["items"][1] == second


def test_multiple_decision_people_same_role_and_contact_has_independent_basis(context):
    crm, sales, _, company, _, project, _ = context
    second = crm.create_contact("owner", company["id"], {"name": "李总", "role": "副总"}, NOW)
    save(context, roles=["final_approver"], basis="reported", verified_at=NOW-20)
    changed = save(context, person=second, roles=["final_approver", "economic_buyer"], basis="observation", evidence="会议中多次追问预算", concerns="采购成本", next_step="核实预算权", engagement="indirect")
    assert len(changed["stakeholders"]) == 2
    assert changed["stakeholders"][1]["basis"] == "observation"
    assert changed["stakeholders"][1]["verified_at"] is None
    assert sales.stakeholders("owner", company["id"], project["id"])["project_revision"] == 3


def test_legacy_selection_imports_unknown_roles_and_survives_reopen(context):
    crm, sales, _, company, person, _, path = context
    legacy = sales.create_opportunity("owner", company["id"], {"name": "旧选择", "contact_ids": [person["id"]]})
    assert legacy["contact_ids"] == [person["id"]]
    assert legacy["stakeholders"][0]["roles"] == []
    assert legacy["stakeholders"][0]["roles_unconfirmed"] is True
    with crm._transaction() as db:
        db.execute("DELETE FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=?", ("owner", legacy["id"]))
    reopened = CustomerStore(path)
    try:
        restored = SalesWorkspace(reopened, clock=lambda: NOW)
        assert restored.opportunities("owner", company["id"])["items"][1] == legacy
        SalesWorkspace(reopened, clock=lambda: NOW)
        assert reopened._db.execute("SELECT COUNT(*) FROM crm_opportunity_stakeholders WHERE opportunity_id=?", (legacy["id"],)).fetchone()[0] == 1
    finally:
        reopened.close()


def test_legacy_membership_removal_archives_roles_and_readding_restores(context):
    _, sales, _, company, person, project, _ = context
    changed = save(context, roles=["champion"], evidence="主动介绍其他决策人")
    removed = sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": changed["revision"], "contact_ids": []})
    assert removed["contact_ids"] == [] and removed["stakeholders"] == []
    assert removed["stakeholders_history"][0]["roles"] == ["champion"]
    restored = sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": removed["revision"], "contact_ids": [person["id"]]})
    assert restored["stakeholders"][0]["roles"] == ["champion"]
    assert sales.relation_history("owner", company["id"], project["id"])["total"] == 3


def test_same_tree_group_procurement_sibling_technical_and_contact_units_explicit(context):
    crm, sales, network, company, _, project, _ = context
    group = network.create_unit("owner", {"name": "集团"}, {"unit_type": "group"})
    network.update("owner", company["id"], {"expected_revision": 1, "parent_customer_id": group["id"]})
    sibling = network.create_unit("owner", {"name": "兄弟公司"}, {"parent_customer_id": group["id"]})
    procurement = crm.create_contact("owner", group["id"], {"name": "集团采购"}, NOW)
    technical = crm.create_contact("owner", sibling["id"], {"name": "兄弟技术"}, NOW)
    save(context, person=procurement, roles=["procurement"], evidence="集中采购会议")
    result = save(context, person=technical, roles=["technical_reviewer"])
    assert {item["unit_name"] for item in result["stakeholders"]} == {"集团", "兄弟公司"}
    assert all(item["membership_valid"] for item in result["stakeholders"])
    assert result["project_units"][0]["roles"] == []  # Hierarchy implies no purchasing authority.
    sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": result["revision"], "contact_ids": [procurement["id"], technical["id"]]})


def test_cross_tree_requires_explicit_project_participation_and_evidence(context):
    crm, sales, network, company, _, project, _ = context
    external = network.create_unit("owner", {"name": "外部测评机构"})
    expert = crm.create_contact("owner", external["id"], {"name": "测评专家"}, NOW)
    with pytest.raises(ValueError, match="项目单位"):
        save(context, person=expert, roles=["security_compliance"])
    for values in ({"roles": ["technical"]}, {"evidence": "委托测评"}):
        with pytest.raises(ValueError, match="参与角色和依据"):
            sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": external["id"], "expected_revision": 1, **values})
    joined = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": external["id"], "expected_revision": 1,
                                        "roles": ["technical"], "evidence": "客户说由该机构进行验证", "basis": "reported"})
    result = save(context, person=expert, roles=["security_compliance"], evidence="参加测评需求讨论")
    assert result["stakeholders"][0]["membership_valid"] is True
    assert network.view("owner", external["id"])["unit"]["parent_customer_id"] is None
    assert sales.project_units("owner", company["id"], project["id"])["total"] == 2
    with pytest.raises(ValueError):
        sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": 1, "scope": "旧版"})
    assert joined["revision"] == 2 and result["revision"] == 3


def test_archive_project_unit_preserves_people_and_flags_invalid_membership(context):
    crm, sales, network, company, _, project, _ = context
    external = network.create_unit("owner", {"name": "合作机构"})
    person = crm.create_contact("owner", external["id"], {"name": "合作决策人"}, NOW)
    sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": external["id"], "expected_revision": 1, "roles": ["approver"], "evidence": "合作项目审批"})
    before = save(context, person=person, roles=["final_approver"])
    after = sales.archive_project_unit("owner", company["id"], project["id"], external["id"], {"expected_revision": before["revision"], "archived": True})
    assert after["stakeholders"][0]["roles"] == ["final_approver"]
    assert after["stakeholders"][0]["membership_valid"] is False
    assert sales.contact_projects("owner", person["id"])["items"][0]["membership_valid"] is False
    assert sales.project_units("owner", company["id"], project["id"])["total"] == 1
    assert sales.project_units("owner", company["id"], project["id"], True)["total"] == 2
    with pytest.raises(ValueError):
        save(context, person=person, stance="supportive")
    restored = sales.archive_project_unit("owner", company["id"], project["id"], external["id"], {"expected_revision": after["revision"], "archived": False})
    assert restored["stakeholders"][0]["membership_valid"] is True


def test_reparent_keeps_role_history_without_new_illegal_participation(context):
    crm, sales, network, company, _, project, _ = context
    group = network.create_unit("owner", {"name": "集团"}, {"unit_type": "group"})
    network.update("owner", company["id"], {"expected_revision": 1, "parent_customer_id": group["id"]})
    person = crm.create_contact("owner", group["id"], {"name": "集团领导"}, NOW)
    before = save(context, person=person, roles=["final_approver"])
    network.update("owner", company["id"], {"expected_revision": 2, "parent_customer_id": None})
    after = sales.opportunities("owner", company["id"])["items"][0]
    assert after["revision"] == before["revision"]+1
    assert after["stakeholders"][0]["roles"] == ["final_approver"]
    assert after["stakeholders"][0]["membership_valid"] is False
    with pytest.raises(ValueError):
        save(context, person=person, roles=["economic_buyer"])


@pytest.mark.parametrize("values", [
    {"roles": ["boss"]}, {"roles": ["user", "user"]}, {"roles": "user"}, {"roles": [True]},
    {"stance": "yes"}, {"influence": "huge"}, {"engagement": "yes"}, {"basis": "inferred"},
    {"verified_at": NOW+1}, {"verified_at": True}, {"concerns": "x"*4001}, {"evidence": "x\x00"},
    {"next_step": None}, {"unknown": "field"},
])
def test_invalid_people_values_rejected_before_writes(context, values):
    _, sales, _, company, _, project, _ = context
    with pytest.raises(ValueError):
        save(context, **values)
    assert sales.opportunities("owner", company["id"])["items"][0] == project


@pytest.mark.parametrize("expected", [None, True, 0, 2])
def test_project_cas_required_for_people_mutations(context, expected):
    _, sales, _, company, person, project, _ = context
    with pytest.raises(ValueError, match="已更新"):
        sales.upsert_stakeholder("owner", company["id"], project["id"], {"contact_id": person["id"], "roles": ["user"], "expected_revision": expected})
    assert sales.opportunities("owner", company["id"])["items"][0] == project


def test_noop_canonical_roles_preserve_project_revision_history_and_saved_priority(context):
    crm, sales, _, company, person, project, _ = context
    project = sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": 1, "blockers": "审批待核实"})
    project = save(context, roles=["champion", "technical_reviewer"], evidence="本人说明")
    recommendation = sales.priorities("owner")["items"][0]
    sales.decide_priority("owner", {"key": recommendation["key"], "signature": recommendation["signature"], "decision": "dismiss"})
    before = sales.relation_history("owner", company["id"], project["id"])
    saved = save(context, roles=["technical_reviewer", "champion"], evidence=" 本人说明 ")
    assert saved == project
    assert sales.relation_history("owner", company["id"], project["id"]) == before
    assert sales.priorities("owner")["items"] == []
    save(context, roles=["user"], concerns="新阻力")
    assert sales.priorities("owner")["items"][0]["source_changed_after_decision"] is True


def test_archived_contact_cannot_join_or_restore_but_history_remains(context):
    crm, sales, _, company, person, project, _ = context
    before = save(context, roles=["champion"])
    removed = sales.archive_stakeholder("owner", company["id"], project["id"], person["id"], {"expected_revision": before["revision"], "archived": True})
    crm.update_contact("owner", company["id"], person["id"], {"archived": True}, NOW)
    with pytest.raises(ValueError):
        save(context, roles=["user"])
    with pytest.raises(ValueError):
        sales.archive_stakeholder("owner", company["id"], project["id"], person["id"], {"expected_revision": removed["revision"], "archived": False})
    historical = sales.contact_projects("owner", person["id"], True)["items"][0]
    assert historical["roles"] == ["champion"] and historical["archived"] and historical["contact_archived"]
    assert sales.contact_projects("owner", person["id"])["items"] == []


def test_cross_owner_relations_and_reverse_views_do_not_leak(context):
    crm, sales, network, company, person, project, _ = context
    foreign = network.create_unit("other", {"name": "外部私有客户"})
    foreign_person = crm.create_contact("other", foreign["id"], {"name": "私有联系人"}, NOW)
    for method in (lambda: save(context, person=foreign_person, roles=["user"]),
                   lambda: sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": foreign["id"], "expected_revision": 1, "roles": ["technical"], "evidence": "不允许跨成员"}),
                   lambda: sales.stakeholders("other", company["id"], project["id"]),
                   lambda: sales.contact_projects("other", person["id"]),
                   lambda: sales.relation_history("other", company["id"], project["id"])):
        with pytest.raises(KeyError):
            method()
    assert sales.opportunities("owner", company["id"])["items"][0] == project


def test_archived_project_preserves_relationships_but_blocks_mutation(context):
    _, sales, _, company, person, project, _ = context
    before = save(context, roles=["user"])
    archived = sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": before["revision"], "archived": True})
    assert sales.contact_projects("owner", person["id"])["items"] == []
    assert sales.contact_projects("owner", person["id"], True)["items"][0]["project_archived"] is True
    with pytest.raises(ValueError, match="归档"):
        save(context, stance="supportive")
    with pytest.raises(ValueError, match="归档"):
        sales.archive_stakeholder("owner", company["id"], project["id"], person["id"], {"expected_revision": archived["revision"], "archived": True})


def test_reopen_preserves_roles_and_project_revision_and_no_tasks(context):
    crm, sales, _, company, person, _, path = context
    before = save(context, roles=list(STAKEHOLDER_ROLES), concerns="需满足密码场景", engagement="direct", verified_at=NOW)
    reopened = CustomerStore(path)
    try:
        restored = SalesWorkspace(reopened, clock=lambda: NOW)
        assert restored.opportunities("owner", company["id"])["items"][0] == before
        assert restored.contact_projects("owner", person["id"]) == sales.contact_projects("owner", person["id"])
        assert reopened._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reopened._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    finally:
        reopened.close()


def test_concurrent_connection_people_edit_uses_project_revision_guard(context):
    _, sales, _, company, person, project, path = context
    reopened = CustomerStore(path)
    try:
        concurrent = SalesWorkspace(reopened, clock=lambda: NOW)
        concurrent.upsert_stakeholder("owner", company["id"], project["id"], {"contact_id": person["id"], "roles": ["champion"], "expected_revision": 1})
        with pytest.raises(ValueError, match="已更新"):
            sales.upsert_stakeholder("owner", company["id"], project["id"], {"contact_id": person["id"], "roles": ["user"], "expected_revision": 1})
        assert sales.opportunities("owner", company["id"])["items"][0]["stakeholders"][0]["roles"] == ["champion"]
    finally:
        reopened.close()


def test_participant_units_roles_noop_and_archive_repeated_is_noop(context):
    _, sales, _, company, _, project, _ = context
    updated = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": company["id"], "expected_revision": 1, "roles": ["payer", "user"], "evidence": "明确范围"})
    stable = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": company["id"], "expected_revision": updated["revision"], "roles": ["user", "payer"], "evidence": " 明确范围 "})
    assert stable == updated
    archived = sales.archive_project_unit("owner", company["id"], project["id"], company["id"], {"expected_revision": stable["revision"], "archived": True})
    assert any(item["is_primary"] and not item["explicit"] for item in archived["project_units"])
    assert sales.archive_project_unit("owner", company["id"], project["id"], company["id"], {"expected_revision": archived["revision"], "archived": True}) == archived


def test_priority_whom_uses_project_roles_and_external_unit_without_cross_project_pollution(context):
    crm, sales, network, company, _, project, _ = context
    external = network.create_unit("owner", {"name": "采购机构"})
    person = crm.create_contact("owner", external["id"], {"name": "采购负责人", "role": "单位经理"}, NOW)
    project = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": external["id"], "expected_revision": 1, "roles": ["procurement"], "evidence": "统一采购"})
    project = save(context, person=person, roles=["procurement", "final_approver"], next_step="核实采购条件")
    sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": project["revision"], "blockers": "采购条件未定"})
    recommendation = sales.priorities("owner")["items"][0]
    assert recommendation["whom"][0]["contact_id"] == person["id"]
    assert recommendation["whom"][0]["roles"] == ["final_approver", "procurement"]
    assert recommendation["whom"][0]["role"] == "单位经理"
    assert recommendation["whom"][0]["unit_name"] == "采购机构"


def test_reparent_cannot_turn_undocumented_same_tree_participation_into_external_permission(context):
    crm, sales, network, company, _, project, _ = context
    group = network.create_unit("owner", {"name": "集团"}, {"unit_type": "group"})
    network.update("owner", company["id"], {"expected_revision": 1, "parent_customer_id": group["id"]})
    person = crm.create_contact("owner", group["id"], {"name": "集团联系人"}, NOW)
    current = sales.opportunities("owner", company["id"])["items"][0]
    current = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": group["id"], "expected_revision": current["revision"]})
    current = save(context, person=person, roles=["user"])
    network.update("owner", company["id"], {"expected_revision": 2, "parent_customer_id": None})
    current = sales.opportunities("owner", company["id"])["items"][0]
    assert next(item for item in current["project_units"] if item["participant_customer_id"] == group["id"])["membership_valid"] is False
    assert current["stakeholders"][0]["membership_valid"] is False
    archived = sales.archive_project_unit("owner", company["id"], project["id"], group["id"], {"expected_revision": current["revision"], "archived": True})
    with pytest.raises(ValueError, match="参与角色和依据"):
        sales.archive_project_unit("owner", company["id"], project["id"], group["id"], {"expected_revision": archived["revision"], "archived": False})
    repaired = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": group["id"], "expected_revision": archived["revision"], "roles": ["user"], "evidence": "拆分后仍参与试点"})
    assert repaired["stakeholders"][0]["membership_valid"] is True


@pytest.mark.parametrize("values", [{"roles": ["owner"]}, {"roles": ["user", "user"]}, {"roles": "user"}, {"evidence": "x"*4001}, {"basis": "inferred"}, {"archived": False}])
def test_invalid_project_unit_fields_are_atomic(context, values):
    _, sales, _, company, _, project, _ = context
    with pytest.raises(ValueError):
        sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": company["id"], "expected_revision": 1, **values})
    assert sales.opportunities("owner", company["id"])["items"][0] == project


def test_active_people_limit_including_restore_and_legacy_selection(context):
    crm, sales, _, company, first, project, _ = context
    contacts = [first]+[crm.create_contact("owner", company["id"], {"name": "人"+str(index)}, NOW) for index in range(50)]
    project = sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": 1, "contact_ids": [item["id"] for item in contacts[:50]]})
    with pytest.raises(ValueError, match="最多50"):
        save(context, person=contacts[50], roles=["user"])
    with pytest.raises(ValueError, match="最多50"):
        sales.update_opportunity("owner", company["id"], project["id"], {"expected_revision": project["revision"], "contact_ids": [item["id"] for item in contacts]})
    project = sales.archive_stakeholder("owner", company["id"], project["id"], first["id"], {"expected_revision": project["revision"], "archived": True})
    project = save(context, person=contacts[50], roles=["user"])
    with pytest.raises(ValueError, match="最多50"):
        save(context, person=first, roles=["user"])
    with pytest.raises(ValueError, match="最多50"):
        sales.archive_stakeholder("owner", company["id"], project["id"], first["id"], {"expected_revision": project["revision"], "archived": False})
    assert len(project["stakeholders"]) == 50
    assert project["stakeholders_history"][0]["contact_id"] == first["id"]


def test_active_project_units_limit_including_restore(context):
    _, sales, network, company, _, project, _ = context
    units = [network.create_unit("owner", {"name": "协作单位"+str(index)}) for index in range(51)]
    for unit in units[:50]:
        project = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": unit["id"], "expected_revision": project["revision"], "roles": ["technical"], "evidence": "项目合作"})
    with pytest.raises(ValueError, match="最多50"):
        sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": units[50]["id"], "expected_revision": project["revision"], "roles": ["technical"], "evidence": "项目合作"})
    project = sales.archive_project_unit("owner", company["id"], project["id"], units[0]["id"], {"expected_revision": project["revision"], "archived": True})
    project = sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": units[50]["id"], "expected_revision": project["revision"], "roles": ["technical"], "evidence": "项目合作"})
    with pytest.raises(ValueError, match="最多50"):
        sales.archive_project_unit("owner", company["id"], project["id"], units[0]["id"], {"expected_revision": project["revision"], "archived": False})
    with pytest.raises(ValueError, match="最多50"):
        sales.upsert_project_unit("owner", company["id"], project["id"], {"participant_customer_id": units[0]["id"], "expected_revision": project["revision"], "roles": ["technical"], "evidence": "项目合作"})


@pytest.mark.parametrize("change", ["hidden", "changed_content", "reassigned_customer", "changed_original"])
def test_personal_facts_with_invalid_source_leave_fresh_project_context_but_preserve_history(context, change):
    crm, sales, network, company, person, project, _ = context
    quote = "王总希望提升密码项目交付质量"
    source = crm.create_record("owner", {"title": "合成交流", "content": quote, "customer_id": company["id"]}, NOW)
    fact = crm.save_fact("owner", company["id"], {"contact_id": person["id"], "key": "professional_goals",
                        "value": "提升密码项目交付质量", "basis": "reported", "evidence": quote,
                        "source_record_id": source["id"]}, NOW + 1)
    current = save(context, roles=["user"])
    exported = current["stakeholders"][0]["personal_facts"]
    assert exported[0]["id"] == fact["id"]
    assert exported[0]["source"] == {"type": "record", "record_id": source["id"], "recorded_at": NOW, "occurred_at": None}
    if change == "changed_content":
        crm.update_record("owner", source["id"], {"content": "修正后的原话不包含先前判断"}, NOW + 2)
    elif change == "reassigned_customer":
        other = network.create_unit("owner", {"name": "合成另一单位"})
        crm.update_record("owner", source["id"], {"customer_id": other["id"]}, NOW + 2)
    else:
        with crm._transaction() as db:
            if change == "hidden":
                db.execute("UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?", ("owner", source["id"]))
            else:
                db.execute("UPDATE crm_records SET original_content=? WHERE owner=? AND id=?", ("更正后的原始转写", "owner", source["id"]))
    current = next(item for item in sales.opportunities("owner", company["id"])["items"] if item["id"] == project["id"])
    assert current["stakeholders"][0]["personal_facts"] == []
    assert any(item["id"] == fact["id"] and item["contact_id"] == person["id"]
               for item in crm.profile("owner", company["id"])["history"])
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_cleared_latest_personal_fact_does_not_revive_old_value_in_project_context(context):
    crm, sales, _, company, person, project, _ = context
    old = crm.save_fact("owner", company["id"], {"contact_id": person["id"], "key": "professional_goals",
                       "value": "提高安全审查通过率", "basis": "reported", "evidence": "本人说明"}, NOW)
    current = save(context, roles=["user"])
    assert current["stakeholders"][0]["personal_facts"][0]["id"] == old["id"]
    cleared = crm.save_fact("owner", company["id"], {"contact_id": person["id"], "key": "professional_goals",
                           "value": "", "basis": "reported", "evidence": "本人要求清空"}, NOW + 1)
    current = next(item for item in sales.opportunities("owner", company["id"])["items"] if item["id"] == project["id"])
    assert current["stakeholders"][0]["personal_facts"] == []
    history = crm.profile("owner", company["id"])["history"]
    assert {old["id"], cleared["id"]}.issubset({item["id"] for item in history})
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
