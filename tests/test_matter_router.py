"""Pure routing contracts over disposable CRM data and scripted models."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from secretary.customer_store import CustomerStore
from secretary.matter_router import MatterRouter
from secretary.matters import MatterService
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0


class FakeModel:
    def __init__(self, result, callback=None):
        self.result, self.callback, self.contexts = result, callback, []

    async def complete_json(self, context):
        self.contexts.append(deepcopy(context))
        if self.callback:
            self.callback()
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / "matter-routing-synthetic.sqlite3")
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    service = MatterService(crm, clock=lambda: NOW)
    customer = crm.create_customer("owner", {"name": "示例研究所", "aliases": ["研究所"]}, NOW)
    yield crm, sales, service, customer
    crm.close()


def make(stack, title="完善电力产品汇报材料", *, owner="owner", project=None, actions=(), objective="准备可用于电力客户汇报的PPT", status="following", visibility="active"):
    crm, _, service, customer = stack
    if owner != "owner":
        customer = crm.create_customer(owner, {"name": "私有客户"}, NOW)
    rows = [crm.create_record(owner, {"title": item, "content": item, "kind": "action", "status": "following", "customer_id": customer["id"]}, NOW) for item in actions]
    result = service.create(owner, {"title": title, "objective": objective, "customer_id": customer["id"],
        "opportunity_id": project, "action_record_ids": [item["id"] for item in rows], "request_id": "make:" + title})["matter"]
    if status != "following" or visibility != "active":
        with crm._transaction() as db:
            db.execute("UPDATE crm_matters SET status=?,visibility=? WHERE owner=? AND id=?", (status, visibility, owner, result["id"]))
        result = service.detail(owner, result["id"])
    return result


def answer(kind, text, matter=None, **extra):
    return {"kind": kind, "matter_id": (matter or {}).get("id"), "title": "完善电力汇报材料", "objective": "完成可汇报的电力PPT",
        "reason": "原话是已有材料的补充，目标相同", "evidence": text, "candidates": [], "actions": [], "updates": {}, **extra}


def route(stack, text, *, model=None, **kwargs):
    crm, _, service, _ = stack
    return asyncio.run(MatterRouter(crm, service, model, clock=lambda: NOW).route("owner", text, **kwargs))


def test_same_goal_model_groups_two_preparations_as_one_and_is_pure(stack):
    text = "按产品线调整电力PPT，再补充防篡改装置内容"
    model = FakeModel(answer("new", text, actions=[
        {"title": "调整电力PPT结构", "content": "按产品线调整電力PPT", "evidence": "按产品线调整电力PPT"},
        {"title": "补充防篡改装置内容", "evidence": "补充防篡改装置内容"}]))
    before = "\n".join(stack[0]._db.iterdump())
    result = route(stack, text, model=model)
    assert result["kind"] == "new" and len(result["actions"]) == 2 and result["groups"] == []
    assert result["matter_id"] is None and result["base_revision"] is None
    assert "大模型语义判定" in result["reason"]
    assert "\n".join(stack[0]._db.iterdump()) == before
    assert not stack[0]._db.execute("SELECT 1 FROM tasks").fetchone()
    assert not stack[0]._db.execute("SELECT 1 FROM notifications").fetchone()


def test_followup_can_be_semantically_matched_without_exact_customer_or_title(stack):
    matter = make(stack, actions=["按产品线重组电力PPT结构"])
    text = "那份电力演示稿再加一个防篡改产品案例"
    model = FakeModel(answer("existing", text, matter))
    result = route(stack, text, model=model)
    assert result["kind"] == "existing" and result["matter_id"] == matter["id"]
    assert result["base_revision"] == matter["revision"]
    assert model.contexts[0]["selected_matter_id"] is None
    assert model.contexts[0]["candidates"][0]["id"] == matter["id"]


def test_explicit_scope_continues_factual_supplement_without_chores(stack):
    matter = make(stack)
    result = route(stack, "防篡改装置是电力方向的核心产品", matter_id=matter["id"])
    assert result["kind"] == "existing" and result["actions"] == []
    assert "未使用大模型" in result["reason"]
    assert result["base_revision"] == matter["revision"]


def test_unscoped_fact_does_not_create_goal_or_action(stack):
    result = route(stack, "防篡改装置是电力方向的核心产品")
    assert result["kind"] == "source" and result["actions"] == []


def test_model_fact_cannot_become_an_action(stack):
    matter = make(stack)
    text = "防篡改装置是电力方向的核心产品"
    model = FakeModel(answer("existing", text, matter, actions=[{"title": "准备防篡改产品方案", "evidence": text}]))
    result = route(stack, text, model=model, matter_id=matter["id"])
    assert result["kind"] == "existing" and result["actions"] == []


def test_repeated_action_is_update_even_if_model_forgot_record_id(stack):
    matter = make(stack, actions=["按产品线重组电力PPT结构"])
    text = "按产品线重组电力PPT结构"
    model = FakeModel(answer("existing", text, matter, actions=[{"title": text, "evidence": text}]))
    result = route(stack, text, model=model, matter_id=matter["id"])
    assert result["actions"][0]["existing_record_id"] == matter["actions"][0]["id"]
    assert len(result["actions"]) == 1


def test_direct_short_ppt_completion_updates_one_action_not_the_matter(stack):
    matter = make(stack, actions=["按产品线重组电力PPT结构", "补充防篡改装置内容"])
    text = "PPT已经做好了"
    model = FakeModel(answer("existing", text, matter, actions=[{
        "title": "按产品线重组电力PPT结构", "evidence": text, "existing_record_id": matter["actions"][0]["id"], "status": "done"}]))
    result = route(stack, text, model=model, matter_id=matter["id"])
    assert result["actions"][0]["status"] == "done"
    assert result["updates"] == {}
    assert stack[2].detail("owner", matter["id"])["status"] == "following"
    assert stack[0].get_record("owner", matter["actions"][0]["id"])["status"] != "done"


def test_mixed_progress_finishes_only_the_step_mentioned_in_that_clause(stack):
    matter = make(stack, actions=["重组PPT", "补充产品化内容"])
    result = route(stack, "PPT已经做好了，还要补充产品化内容", matter_id=matter["id"])
    statuses = {item["existing_record_id"]: item["status"] for item in result["actions"]}
    assert statuses == {matter["actions"][0]["id"]: "done", matter["actions"][1]["id"]: "following"}


def test_model_cannot_use_neighbouring_step_completion_as_evidence(stack):
    matter = make(stack, actions=["重组PPT", "补充产品化内容"])
    text = "PPT已经做好了，还要补充产品化内容"
    wrong = {"title": "补充产品化内容", "evidence": text, "existing_record_id": matter["actions"][1]["id"], "status": "done"}
    result = route(stack, text, model=FakeModel(answer("existing", text, matter, actions=[wrong])), matter_id=matter["id"])
    assert "未通过校验" in result["reason"]
    assert not any(item["existing_record_id"] == matter["actions"][1]["id"] and item["status"] == "done" for item in result["actions"])


@pytest.mark.parametrize("text", ["如果PPT做好了就去汇报", "客户说PPT已经做好了", "PPT还没做好，其他材料完成了", "PPT是不是已经做好了"])
def test_quoted_conditional_negative_or_question_cannot_complete_action(stack, text):
    matter = make(stack, actions=["重组PPT"])
    model = FakeModel(answer("existing", text, matter, actions=[{
        "title": "重组PPT", "evidence": text, "existing_record_id": matter["actions"][0]["id"], "status": "done"}]))
    result = route(stack, text, model=model, matter_id=matter["id"])
    assert all(item.get("status") != "done" for item in result["actions"])
    assert "未通过校验" in result["reason"]


def test_ambiguous_two_active_goals_asks_and_does_not_force_customer_match(stack):
    first = make(stack, title="完善电力汇报材料")
    second = make(stack, title="完善交通汇报材料")
    result = route(stack, "这份汇报材料还要再修改", scope={"customer_id": stack[3]["id"]})
    assert result["kind"] == "ambiguous" and result["matter_id"] is None
    assert {item["id"] for item in result["candidates"]} == {first["id"], second["id"]}
    assert result["actions"] == []


def test_different_projects_same_customer_never_appear_in_scoped_model_candidates(stack):
    first = stack[1].create_opportunity("owner", stack[3]["id"], {"name": "电力密码试点"})
    second = stack[1].create_opportunity("owner", stack[3]["id"], {"name": "交通数据安全"})
    mine = make(stack, title="准备电力演示材料", project=first["id"])
    other = make(stack, title="准备交通演示材料", project=second["id"])
    text = "这份演示材料需要修改"
    model = FakeModel(answer("existing", text, other))
    result = route(stack, text, model=model, scope={"opportunity_id": first["id"]})
    assert {item["id"] for item in model.contexts[0]["candidates"]} == {mine["id"]}
    assert result["matter_id"] != other["id"] and "未通过校验" in result["reason"]


def test_named_other_project_does_not_inherit_selected_project(stack):
    first = stack[1].create_opportunity("owner", stack[3]["id"], {"name": "电力密码试点"})
    second = stack[1].create_opportunity("owner", stack[3]["id"], {"name": "交通数据安全"})
    mine = make(stack, title="准备电力演示材料", project=first["id"])
    other = make(stack, title="完善交通报价", project=second["id"])
    result = route(stack, "联系客户完善交通数据安全的报价", matter_id=mine["id"])
    assert result["kind"] == "ambiguous" and result["matter_id"] is None
    assert other["id"] in {item["id"] for item in result["candidates"]}
    assert "另一个项目" in result["reason"]


def test_reference_to_another_project_is_not_a_reassignment_command(stack):
    first = stack[1].create_opportunity("owner", stack[3]["id"], {"name": "电力密码试点"})
    stack[1].create_opportunity("owner", stack[3]["id"], {"name": "交通数据安全"})
    mine = make(stack, project=first["id"])
    result = route(stack, "调整PPT，参考交通数据安全的结构", matter_id=mine["id"])
    assert result["kind"] == "existing" and result["matter_id"] == mine["id"]


def test_foreign_owner_matter_and_action_ids_are_not_model_authority(stack):
    mine = make(stack, actions=["调整PPT"])
    foreign = make(stack, title="私有目标", owner="other", actions=["秘密动作"])
    text = "调整PPT"
    model = FakeModel(answer("existing", text, foreign))
    result = route(stack, text, model=model, matter_id=mine["id"])
    assert result["matter_id"] == mine["id"]
    assert "私有目标" not in json.dumps(model.contexts, ensure_ascii=False)
    bad = FakeModel(answer("existing", text, mine, actions=[{"title": "秘密动作", "evidence": text, "existing_record_id": foreign["actions"][0]["id"]}]))
    assert "未通过校验" in route(stack, text, model=bad, matter_id=mine["id"])["reason"]
    with pytest.raises(KeyError):
        route(stack, text, matter_id=foreign["id"])


@pytest.mark.parametrize("field,value", [("matter_id", True), ("matter_id", 99999), ("updates", {"status": "ended"}), ("remind_at", NOW+100), ("actions", [{"title": "调整PPT", "evidence": "不存在的原话"}])])
def test_model_unknown_ids_execution_fields_and_invented_evidence_are_rejected(stack, field, value):
    matter = make(stack, actions=["调整PPT"])
    text = "调整PPT"
    raw = answer("existing", text, matter)
    raw[field] = value
    result = route(stack, text, model=FakeModel(raw), matter_id=matter["id"])
    assert "未通过校验" in result["reason"]
    assert result["updates"] == {} and not any(item.get("remind_at") for item in result["actions"])


def test_context_is_bounded_and_does_not_include_unrelated_histories(stack):
    mine = make(stack, title="调整电力PPT材料", actions=["调整PPT结构"])
    for index in range(8):
        make(stack, title=f"无关合同{index}", objective="催收采购合同")
    model = FakeModel(answer("existing", "调整电力PPT材料", mine))
    route(stack, "调整电力PPT材料", model=model)
    assert len(model.contexts[0]["candidates"]) <= 6
    assert "无关合同" not in json.dumps(model.contexts, ensure_ascii=False)
    assert "sources" not in json.dumps(model.contexts)


@pytest.mark.parametrize("status,visibility", [("ended", "active"), ("following", "archived"), ("following", "trash")])
def test_inactive_matter_is_not_implicitly_reopened(stack, status, visibility):
    matter = make(stack, status=status, visibility=visibility)
    with pytest.raises(ValueError):
        route(stack, "继续准备材料", matter_id=matter["id"])


def test_late_model_result_does_not_resurrect_archived_matter(stack):
    matter = make(stack)
    text = "补充产品材料"

    def archive():
        with stack[0]._transaction() as db:
            db.execute("UPDATE crm_matters SET visibility='archived',revision=revision+1 WHERE id=?", (matter["id"],))

    result = route(stack, text, model=FakeModel(answer("existing", text, matter), archive), matter_id=matter["id"])
    assert result["kind"] == "source" and result["matter_id"] is None and result["actions"] == []
    assert "不恢复" in result["reason"]


def test_late_failed_model_also_checks_archived_scope(stack):
    matter = make(stack)

    def archive():
        with stack[0]._transaction() as db:
            db.execute("UPDATE crm_matters SET visibility='archived',revision=revision+1 WHERE id=?", (matter["id"],))

    result = route(stack, "补充产品材料", model=FakeModel(ValueError("bad response"), archive), matter_id=matter["id"])
    assert result["kind"] == "source" and result["matter_id"] is None


def test_late_update_requires_fresh_confirmation(stack):
    matter = make(stack)
    text = "补充产品材料"

    def update():
        stack[2].update("owner", matter["id"], {"title": "更新后的目标", "expected_revision": 1, "request_id": "parallel"})

    result = route(stack, text, model=FakeModel(answer("existing", text, matter), update), matter_id=matter["id"])
    assert result["kind"] == "ambiguous" and result["base_revision"] is None
    assert result["candidates"][0]["revision"] == 2
    assert result["actions"] == []


def test_explicit_new_mode_still_uses_model_to_keep_two_independent_goals(stack):
    matter = make(stack)
    text = "整理电力PPT，另外提交交通项目报价"
    groups = [
        {"title": "完善电力PPT", "objective": "电力材料可汇报", "evidence": "整理电力PPT", "actions": [{"title": "整理电力PPT", "evidence": "整理电力PPT"}]},
        {"title": "提交交通报价", "objective": "完成报价提交", "evidence": "提交交通项目报价", "actions": [{"title": "提交交通报价", "evidence": "提交交通项目报价"}]},
    ]
    model = FakeModel(answer("new", text, groups=groups))
    result = route(stack, text, model=model, matter_id=matter["id"], mode="new")
    assert model.contexts and result["kind"] == "new" and len(result["groups"]) == 2
    assert result["matter_id"] is None


def test_model_same_deliverable_in_two_groups_is_one_goal_with_two_steps(stack):
    text = "调整电力PPT结构，补充产品化内容"
    groups = [
        {"title": "调整PPT结构", "objective": "完善电力汇报材料", "evidence": "调整电力PPT结构", "actions": [{"title": "调整电力PPT结构", "evidence": "调整电力PPT结构"}]},
        {"title": "补充产品化内容", "objective": "完善电力汇报材料", "evidence": "补充产品化内容", "actions": [{"title": "补充产品化内容", "evidence": "补充产品化内容"}]},
    ]
    result = route(stack, text, model=FakeModel(answer("new", text, groups=groups)))
    assert result["kind"] == "new" and result["groups"] == [] and len(result["actions"]) == 2
    assert result["objective"] == "完善电力汇报材料"


def test_model_action_content_does_not_sneak_unreported_facts_into_source(stack):
    text = "调整电力PPT结构"
    raw = answer("new", text, actions=[{"title": text, "evidence": text, "content": "客户已经确认采购100台，预算已批准"}])
    result = route(stack, text, model=FakeModel(raw))
    assert result["actions"][0]["content"] == text


def test_explicit_new_goal_leaves_current_context_without_merging(stack):
    matter = make(stack)
    result = route(stack, "另记一件事，联系客户催合同付款", matter_id=matter["id"])
    assert result["kind"] == "new" and result["matter_id"] is None


def test_provider_adapter_reuses_client_and_json_protocol_without_real_network(stack):
    matter = make(stack)
    text = "调整电力PPT"
    seen = []

    async def run():
        async def respond(request):
            seen.append((request.url.path, json.loads(request.content), request.headers.get("authorization")))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer("existing", text, matter), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SimpleNamespace(api_key="fake-test-key", base_url="https://example.invalid/v1", model="fake-model", client=client)
            return await MatterRouter(stack[0], stack[2], provider, clock=lambda: NOW).route("owner", text, matter_id=matter["id"])

    result = asyncio.run(run())
    assert result["kind"] == "existing"
    assert seen[0][0] == "/v1/chat/completions"
    assert seen[0][1]["response_format"] == {"type": "json_object"}
    assert seen[0][1]["stream"] is False and seen[0][2] == "Bearer fake-test-key"


def test_unconfigured_provider_does_not_send_network_or_claim_ai(stack):
    provider = SimpleNamespace(api_key="", base_url="https://example.invalid", model="fake-model")
    result = route(stack, "补充电力产品材料", model=provider)
    assert "未使用大模型" in result["reason"]


def test_goal_change_is_proposed_only_after_direct_instruction(stack):
    matter = make(stack)
    text = "目标改为完成电力客户汇报"
    raw = answer("existing", text, matter, updates={"objective": "完成电力客户汇报"})
    result = route(stack, text, model=FakeModel(raw), matter_id=matter["id"])
    assert result["updates"] == {"objective": "完成电力客户汇报"}
    assert stack[2].detail("owner", matter["id"])["objective"] == matter["objective"]


def test_model_cannot_replace_explicit_new_goal_with_invented_commitment(stack):
    matter = make(stack)
    text = "目标改为完成电力客户汇报"
    raw = answer("existing", text, matter, updates={"objective": "客户已经同意采购一百台"})
    result = route(stack, text, model=FakeModel(raw), matter_id=matter["id"])
    assert result["updates"] == {} and "未通过校验" in result["reason"]


def test_invalid_scope_is_not_silently_discarded(stack):
    with pytest.raises(ValueError):
        route(stack, "补充资料", scope=[])


def test_explicitly_selected_unassigned_matter_can_accept_clarified_scope(stack):
    matter = stack[2].create("owner", {"title": "准备那份材料", "request_id": "unassigned"})["matter"]
    result = route(stack, "补充电力产品材料", matter_id=matter["id"], scope={"customer_id": stack[3]["id"]})
    assert result["kind"] == "existing" and result["matter_id"] == matter["id"]
    # Routing does not fill the relation: the applying transaction reviews it.
    assert stack[2].detail("owner", matter["id"])["customer_id"] is None


def test_explicitly_selected_assigned_matter_cannot_switch_customer(stack):
    matter = make(stack)
    other = stack[0].create_customer("owner", {"name": "另一个示例客户"}, NOW)
    with pytest.raises(ValueError):
        route(stack, "補充材料", matter_id=matter["id"], scope={"customer_id": other["id"]})


def test_existing_mode_requires_explicit_matter_and_blocks_model_switch(stack):
    with pytest.raises(ValueError):
        route(stack, "补充材料", mode="existing")
    mine = make(stack)
    result = route(stack, "补充材料", model=FakeModel(answer("new", "补充材料")), matter_id=mine["id"], mode="existing")
    assert result["kind"] == "existing" and result["matter_id"] == mine["id"]
