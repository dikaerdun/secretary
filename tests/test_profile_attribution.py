"""Round 01 original-source attribution and explicit duration counterexamples."""
import asyncio

import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.exchange_workspace import ExchangeWorkspace

NOW = 1_800_000_000.0


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / "fresh-attribution-round01.sqlite3")
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    unit = crm.create_customer("owner", {"name": "合成归因银行"}, NOW)
    project = sales.create_opportunity("owner", unit["id"], {"name": "合成密码项目"})
    profile = ProfileIntelligence(crm, sales, clock=lambda: NOW)
    exchange = ExchangeWorkspace(crm, sales, profile, clock=lambda: NOW)
    yield crm, sales, profile, exchange, unit, project
    profile.close()
    crm.close()


def prepare(services, text, analyzer=None):
    crm, sales, profile, exchange, unit, project = services
    row = crm.create_record("owner", {"customer_id": unit["id"], "title": "归因原句", "content": text,
        "kind": "note", "category": "conversation"}, NOW)
    sales.link("owner", "record", row["id"], project["id"])
    profile.analyzer = analyzer
    view = asyncio.run(exchange.prepare("owner", "record", row["id"]))
    assert view["status"] == "ready", view["message"]
    assert crm.get_record("owner", row["id"])["original_content"] == text
    return row, view


def request(view, *items, request_id="round01"):
    return {"request_id": request_id, "expected_revision": view["revision"], "source_revision": view["source_revision"],
        "items": [{"id": item["id"], "expected_version": item["version"], **data} for item, data in items]}


@pytest.mark.parametrize("text", [
    "只是我的想法，不是客户承诺，先问谁验收。",
    "那个轮换回滚先别扩范围，先问谁验收再补材料……这是我的想法，没跟对方约时间。",
])
def test_own_question_is_useful_suggestion_not_customer_criteria_or_commitment(services, text):
    crm, _, _, exchange, _, _ = services
    row, view = prepare(services, text)
    assert not any(item["kind"] == "profile" and item["draft"]["basis"] == "reported" for item in view["items"])
    actions = [item for item in view["items"] if item["kind"] == "action"]
    assert actions, "自己的下一步问题应仍能落地为待办建议"
    assert all(item["current"]["action"]["kind"] == "suggestion" for item in actions)
    action = actions[0]
    result = exchange.confirm("owner", "record", row["id"], request(view, (action, {})))
    assert result["results"][0]["status"] == "confirmed", result
    child = crm.get_record("owner", result["results"][0]["result"]["record_id"])
    assert action["evidence"] in child["content"] and "先问谁验收" in child["content"]
    assert crm.get_record("owner", row["id"])["original_content"] == text
    assert crm._db.execute("SELECT count(*) FROM crm_project_profile_facts").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_customer_budget_survives_own_question_and_is_confirmed_only_in_correct_project(services):
    crm, sales, profile, exchange, unit, project = services
    other = sales.create_opportunity("owner", unit["id"], {"name": "无关合成项目"})
    text = "客户说预算30万元，尚未审批，我准备问谁来验收。"
    row, view = prepare(services, text)
    budget = next(item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "budget_notes")
    assert budget["draft"]["basis"] == "reported"
    assert budget["evidence"] == "客户说预算30万元，尚未审批"
    assert not any(item["kind"] == "profile" and item["candidate"]["key"] == "success_criteria" for item in view["items"])
    body = request(view, (budget, {"expected_fact_id": None}))
    result = exchange.confirm("owner", "record", row["id"], body)
    assert result["results"][0]["status"] == "confirmed", result
    assert exchange.confirm("owner", "record", row["id"], body) == result
    facts = profile.project_facts("owner", unit["id"], project["id"])["items"]
    assert len(facts) == 1 and facts[0]["basis"] == "reported" and "尚未审批" in facts[0]["value"]
    assert profile.project_facts("owner", unit["id"], other["id"])["items"] == []
    assert crm.profile("owner", unit["id"])["fields"] == []
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_short_model_quote_cannot_escape_postposed_whole_thought_disclaimer(services):
    text = "验收要技术部负责，先补轮换回滚材料……这是我的想法，没跟对方约时间。"
    class ShortenedModel:
        async def extract(self, source, context):
            return [{"key": "success_criteria", "value": "验收要技术部负责", "evidence": "验收要技术部负责", "basis": "reported"}]
    _, view = prepare(services, text, ShortenedModel())
    assert all(item["draft"]["basis"] == "observation" for item in view["items"] if item["kind"] == "profile")


@pytest.mark.parametrize("text", [
    "客户说预算30万元尚未审批我准备问谁来验收。",
    "客户说：‘预算30万元尚未审批’我准备问谁来验收。",
])
def test_customer_statement_without_comma_survives_later_own_intent(services, text):
    _, view = prepare(services, text)
    matches = [item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "budget_notes"]
    assert matches and all(item["draft"]["basis"] == "reported" for item in matches)
    assert all("我准备" not in item["evidence"] for item in matches)


def test_postposed_explicit_thought_references_prior_unattributed_sentence(services):
    class ShortenedModel:
        async def extract(self, source, context):
            return [{"key": "success_criteria", "value": "验收要技术部负责", "evidence": "验收要技术部负责", "basis": "reported"}]
    _, view = prepare(services, "验收要技术部负责。这是我的想法，没跟对方确认。", ShortenedModel())
    assert all(item["draft"]["basis"] != "reported" for item in view["items"] if item["kind"] == "profile")


def test_retraction_does_not_erase_an_earlier_independent_customer_budget(services):
    _, view = prepare(services, "客户说预算30万元，尚未审批。客户说验收负责人是李岚，但刚才我记错了，这不是客户确认。")
    budget = [item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "budget_notes"]
    criteria = [item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "success_criteria"]
    assert budget and all(item["draft"]["basis"] == "reported" and "尚未审批" in item["evidence"] for item in budget)
    assert all(item["draft"]["basis"] != "reported" for item in criteria)


def test_model_short_budget_stays_observation_through_real_confirmation(services):
    crm, _, profile, exchange, unit, project = services
    class ShortenedModel:
        async def extract(self, source, context):
            return [{"key": "budget_notes", "value": "预算30万元", "evidence": "预算30万元", "basis": "reported"}]
    row, view = prepare(services, "预算30万元，只是方案假设，尚需客户核实。", ShortenedModel())
    item = next(item for item in view["items"] if item["kind"] == "profile")
    assert item["draft"]["basis"] == "observation"
    blocked = exchange.confirm("owner", "record", row["id"], request(view,
        (item, {"draft": {"basis": "reported"}, "expected_fact_id": None}), request_id="cannot-upgrade"))
    assert blocked["results"][0]["status"] == "blocked"
    assert crm._db.execute("SELECT count(*) FROM crm_project_profile_facts").fetchone()[0] == 0
    fresh = blocked["workspace"]
    item = next(item for item in fresh["items"] if item["kind"] == "profile")
    adopted = exchange.confirm("owner", "record", row["id"], request(fresh,
        (item, {"draft": {"basis": "observation"}, "expected_fact_id": None}), request_id="observation-explicit"))
    assert adopted["results"][0]["status"] == "confirmed", adopted
    fact = profile.project_facts("owner", unit["id"], project["id"])["items"][0]
    assert fact["basis"] == "observation" and fact["value"] == "预算30万元"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_old_reported_question_candidate_cannot_be_confirmed_after_guard_upgrade(services, monkeypatch):
    import secretary.profile_intelligence as module
    crm, sales, profile, _, unit, project = services
    text = "只是我的想法，不是客户承诺，先问谁验收。"
    row = crm.create_record("owner", {"customer_id": unit["id"], "title": "升级前已记录资料", "content": text,
        "kind": "note", "category": "conversation"}, NOW)
    sales.link("owner", "record", row["id"], project["id"])
    source = next(source for source in profile._sources(crm._db, "owner") if source["type"] == "record" and source["id"] == row["id"])
    # Emulate the old erroneous guard only during candidate storage, preserving
    # the very same source hash/content to exercise an actual upgrade boundary.
    with monkeypatch.context() as old_version:
        old_version.setattr(module, "_source_basis", lambda source, evidence, requested, context: requested)
        with crm._transaction() as db:
            profile._store_candidates(db, "owner", source, [{"key": "success_criteria", "value": text,
                "evidence": text, "basis": "reported"}], profile._context(db, "owner", unit["id"]))
    candidate = crm._db.execute("SELECT id,revision FROM crm_profile_candidates").fetchone()
    with pytest.raises(ValueError, match="行动|问题"):
        profile.decide("owner", candidate["id"], {"decision": "confirm", "expected_revision": candidate["revision"], "expected_fact_id": None})
    assert profile.get_candidate("owner", candidate["id"])["status"] == "stale"
    assert crm.get_record("owner", row["id"])["original_content"] == text
    assert crm._db.execute("SELECT count(*) FROM crm_project_profile_facts").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_unknown_schedule_duration_can_be_saved_without_blocking_action(services):
    crm, _, _, exchange, _, _ = services
    row, view = prepare(services, "我答应发送接口清单。")
    action = next(item for item in view["items"] if item["kind"] == "action")
    schedule = next(item for item in view["items"] if item["kind"] == "schedule")
    assert schedule["draft"]["duration_minutes"] is None, "没有依据时不能悄悄默认30分钟"
    valued = exchange.edit_draft("owner", "record", row["id"], {"expected_revision": view["revision"],
        "source_revision": view["source_revision"], "items": [{"id": schedule["id"], "expected_version": schedule["version"],
            "selected": False, "draft": {"duration_minutes": 45}}]})
    assert next(item for item in valued["items"] if item["id"] == schedule["id"])["draft"]["duration_minutes"] == 45
    view = valued
    schedule = next(item for item in view["items"] if item["kind"] == "schedule")
    edit = {"expected_revision": view["revision"], "source_revision": view["source_revision"], "items": [
        {"id": schedule["id"], "expected_version": schedule["version"], "selected": False,
         "draft": {"remind_at": NOW + 3600, "duration_minutes": None}}]}
    fresh = exchange.edit_draft("owner", "record", row["id"], edit)
    action = next(item for item in fresh["items"] if item["id"] == action["id"])
    result = exchange.confirm("owner", "record", row["id"], request(fresh, (action, {})))
    assert result["results"][0]["status"] == "confirmed"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0


def test_explicit_schedule_without_duration_is_blocked_before_creating_proposal(services):
    crm, _, _, exchange, _, _ = services
    row, view = prepare(services, "我答应发送接口清单。")
    action = next(item for item in view["items"] if item["kind"] == "action")
    adopted = exchange.confirm("owner", "record", row["id"], request(view, (action, {}), request_id="adopt-only"))
    child_id = adopted["results"][0]["result"]["record_id"]
    fresh = exchange.get("owner", "record", row["id"])
    schedule = next(item for item in fresh["items"] if item["kind"] == "schedule")
    result = exchange.confirm("owner", "record", row["id"], request(fresh,
        (schedule, {"draft": {"remind_at": NOW + 3600, "duration_minutes": None}})))
    assert result["results"][0]["status"] == "blocked", result
    assert "用时" in result["results"][0]["error"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    assert crm.get_record("owner", child_id)["proposal_id"] is None
    assert next(item for item in result["workspace"]["items"] if item["id"] == schedule["id"])["draft"]["duration_minutes"] is None


@pytest.mark.parametrize("model", [False, True])
@pytest.mark.parametrize("text", ["我答应发送适配清单。", "我承诺整理接口适配问题。"])
def test_salesperson_commitment_is_action_not_customer_technical_fact(services, model, text):
    crm, _, _, _, _, _ = services
    class ShortenedModel:
        async def extract(self, source, context):
            return [{"key": "compatibility", "value": "适配", "evidence": "适配", "basis": "reported"}]
    row, view = prepare(services, text, ShortenedModel() if model else None)
    assert not any(item["kind"] == "profile" for item in view["items"])
    actions = [item for item in view["items"] if item["kind"] == "action"]
    assert actions and any(item["current"]["action"]["kind"] == "commitment" for item in actions)
    assert crm.get_record("owner", row["id"])["original_content"] == text
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("model", [False, True])
def test_customers_first_person_commitment_keeps_customer_attribution(services, model):
    class ShortenedModel:
        async def extract(self, source, context):
            return [{"key": "compatibility", "value": "信创适配清单", "evidence": "信创适配清单", "basis": "reported"}]
    _, view = prepare(services, "客户说：“我承诺提供信创适配清单。”", ShortenedModel() if model else None)
    matches = [item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "compatibility"]
    assert matches and all(item["draft"]["basis"] == "reported" for item in matches)


def test_customer_requirement_and_our_commitment_keep_distinct_evidence(services):
    _, view = prepare(services, "客户说技术需求是兼容信创，我方答应发送适配清单。")
    matches = [item for item in view["items"] if item["kind"] == "profile" and item["candidate"]["key"] == "compatibility"]
    assert len(matches) == 1
    assert matches[0]["draft"]["basis"] == "reported" and matches[0]["evidence"] == "客户说技术需求是兼容信创"
    assert any(item["kind"] == "action" and "我方答应" in item["draft"]["title"] for item in view["items"])
