"""Independent attribution counterexamples: synthetic tmp databases, no providers.

This review deliberately does not read a user's database, settings or audio. It
checks observable candidate and action semantics rather than regex internals.
"""
import asyncio

import pytest

from secretary.customer_store import CustomerStore
from secretary.exchange_workspace import ExchangeWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0


@pytest.fixture
def attribution_services(tmp_path):
    crm = CustomerStore(tmp_path / "synthetic-independent-attribution.sqlite3")
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer("review-owner", {"name": "合成归因银行"}, NOW)
    crm.create_contact("review-owner", customer["id"], {"name": "李岚", "department": "技术部"}, NOW)
    project = sales.create_opportunity("review-owner", customer["id"], {"name": "合成加密项目", "stage": "qualified"})
    profile = ProfileIntelligence(crm, sales, clock=lambda: NOW)
    exchange = ExchangeWorkspace(crm, sales, profile, clock=lambda: NOW)
    yield crm, sales, profile, exchange, customer, project
    profile.close()
    crm.close()


def capture(services, text, category="conversation"):
    crm, sales, _, _, customer, project = services
    row = crm.create_record("review-owner", {"customer_id": customer["id"], "title": "合成归因原文",
        "content": text, "kind": "note", "category": category}, NOW)
    sales.link("review-owner", "record", row["id"], project["id"])
    return row


def scan(services, text, model_attribute=None, category="conversation"):
    crm, _, profile, _, customer, _ = services
    row = capture(services, text, category)
    if model_attribute is not None:
        class SyntheticExtractor:
            async def extract(self, source, context):
                assert source["text"] == text
                return [dict(model_attribute)]
        profile.analyzer = SyntheticExtractor()
    asyncio.run(profile.scan("review-owner", force=True))
    result = profile.list_candidates("review-owner", customer["id"])["items"]
    assert crm.get_record("review-owner", row["id"])["original_content"] == text
    assert crm._db.execute("SELECT count(*) FROM crm_project_profile_facts").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    return result


@pytest.mark.parametrize("text,key", [
    ("李岚反馈：预算没有审批，暂不推进项目。", "budget_notes"),
    ("客户说：我不希望临时改约，不要临时打电话。", "avoidances"),
])
def test_real_customer_negative_feedback_is_retained_reported(attribution_services, text, key):
    candidates = scan(attribution_services, text)
    matches = [item for item in candidates if item["key"] == key]
    assert matches, "客户明确否定反馈仍是事实依据，不能因为否定词整体丢弃"
    assert all(item["basis"] == "reported" for item in matches)
    assert all("不" in item["evidence"] for item in matches)


@pytest.mark.parametrize("text,key", [
    ("客户说：我想先微信发材料，沟通时请讲技术细节。", "communication_channel"),
    ("李岚说：我认为预算没有审批，采购要暂缓。", "budget_notes"),
    ("李岚说：“我想先微信发材料，沟通时请讲技术细节。”", "communication_channel"),
])
def test_customer_first_person_quote_is_not_salesperson_proposal(attribution_services, text, key):
    matches = [item for item in scan(attribution_services, text) if item["key"] == key]
    assert matches, "客户引用中的我想/我认为应保留为客户明确表达"
    assert all(item["basis"] == "reported" for item in matches)


@pytest.mark.parametrize("text", [
    "我想先微信发材料，沟通时请讲技术细节。",
    "我认为预算没有审批，采购要暂缓。",
])
def test_salesperson_first_person_never_becomes_reported(attribution_services, text):
    assert all(item["basis"] != "reported" for item in scan(attribution_services, text))


@pytest.mark.parametrize("text", [
    "客户说预算30万元，尚未审批，我准备问谁来验收。",
    "客户说预算30万元，尚未审批，我想先发送方案。",
    "客户说预算30万元，尚未审批，我认为这个客户还需继续了解。",
])
def test_mixed_customer_budget_and_sales_followup_keeps_customer_half(attribution_services, text):
    candidates = scan(attribution_services, text)
    matches = [item for item in candidates if item["key"] == "budget_notes"]
    assert matches, "销售后续问题或计划不能吞掉同句前面的真实客户预算反馈"
    assert all(item["basis"] == "reported" for item in matches)
    assert all("30万元" in item["evidence"] and "尚未审批" in item["evidence"] for item in matches)
    assert all("我准备问" not in item["evidence"] and "我想" not in item["evidence"] and "我认为" not in item["evidence"] for item in matches)


def test_comma_separated_technical_metrics_remain_one_complete_evidence(attribution_services):
    text = "客户说：验收指标是吞吐量1000TPS，延迟低于20ms，并发连接5000个。"
    matches = [item for item in scan(attribution_services, text) if item["key"] == "success_criteria"]
    assert len(matches) == 1, "逗号中的技术参数列举不应被切为多份零碎画像"
    assert matches[0]["basis"] == "reported"
    assert all(term in matches[0]["evidence"] for term in ("1000TPS", "20ms", "5000个"))


@pytest.mark.parametrize("text", [
    "客户说：预算30万元。我估计：预算30万元。",
    "客户说：预算30万元。我打算把预算30万元作为方案估算。",
])
def test_repeated_identical_short_quote_uses_conservative_attribution(attribution_services, text):
    attribute = {"key": "budget_notes", "value": "预算30万元", "evidence": "预算30万元", "basis": "reported"}
    candidates = scan(attribution_services, text, attribute)
    assert all(item["basis"] != "reported" for item in candidates), "重复摘录出现销售估计/计划时不能任取第一次客户归因"


@pytest.mark.parametrize("text", [
    "预算30万元只是我估计，不是客户确认。",
    "预算30万元，这只是我的想法，并非客户原话。",
    "预算30万元，只是方案假设，尚需客户核实。",
])
def test_model_short_quote_cannot_remove_same_sentence_disclaimer(attribution_services, text):
    attribute = {"key": "budget_notes", "value": "预算30万元", "evidence": "预算30万元", "basis": "reported"}
    candidates = scan(attribution_services, text, attribute)
    assert all(item["basis"] != "reported" for item in candidates), "模型短摘不能绕开原文中的销售推测或待核实免责声明"


def test_model_client_quote_survives_sales_question_outside_quote(attribution_services):
    text = "客户说预算30万元，尚未审批，我准备问谁来验收。"
    attribute = {"key": "budget_notes", "value": "预算30万元，尚未审批",
                 "evidence": "客户说预算30万元，尚未审批", "basis": "reported"}
    candidates = scan(attribution_services, text, attribute)
    assert len(candidates) == 1
    assert candidates[0]["basis"] == "reported"
    assert candidates[0]["evidence"] == attribute["evidence"]


def test_model_short_own_question_cannot_borrow_customer_attribution(attribution_services):
    text = "客户说预算30万元，尚未审批，我准备问谁来验收。"
    attribute = {"key": "success_criteria", "value": "验收负责人待确认",
                 "evidence": "谁来验收", "basis": "reported"}
    assert scan(attribution_services, text, attribute) == []


def test_model_whole_mixed_quote_is_not_reported(attribution_services):
    text = "客户说预算30万元，尚未审批，我认为这个客户还需继续了解。"
    attribute = {"key": "budget_notes", "value": "预算30万元，尚未审批",
                 "evidence": text, "basis": "reported"}
    assert all(item["basis"] != "reported" for item in scan(attribution_services, text, attribute))


@pytest.mark.parametrize("text", [
    "这不是客户承诺，我计划下周提供方案。",
    "客户没有承诺提供接口清单，我准备下周再跟进。",
    "客户未承诺提供接口清单，我准备下周再跟进。",
    "客户不承诺提供接口清单，我准备下周再跟进。",
    "客户拒绝承诺提供接口清单，我准备下周再跟进。",
    "我不会承诺交付时间，我准备先核实研发进度。",
])
def test_negative_customer_commitment_is_not_labeled_commitment(attribution_services, text):
    crm, _, _, exchange, _, _ = attribution_services
    row = capture(attribution_services, text)
    view = asyncio.run(exchange.prepare("review-owner", "record", row["id"]))
    assert view["status"] == "ready"
    analysis = crm.get_analysis("review-owner", row["id"])
    assert analysis["actions"], "销售自己的下一步计划仍应可整理"
    assert all(action["kind"] != "commitment" for action in analysis["actions"]), "否定客户承诺不能升级为已承诺行动"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_explicit_customer_commitment_still_has_commitment_action(attribution_services):
    crm, _, _, exchange, _, _ = attribution_services
    row = capture(attribution_services, "客户承诺下周提供接口清单。")
    asyncio.run(exchange.prepare("review-owner", "record", row["id"]))
    assert any(action["kind"] == "commitment" for action in crm.get_analysis("review-owner", row["id"])["actions"])


def test_customer_non_commitment_does_not_erase_separate_sales_commitment(attribution_services):
    crm, _, _, exchange, _, _ = attribution_services
    row = capture(attribution_services, "客户没有承诺提供接口清单，但我答应下周发送方案。")
    asyncio.run(exchange.prepare("review-owner", "record", row["id"]))
    actions = crm.get_analysis("review-owner", row["id"])["actions"]
    assert any(action["kind"] == "commitment" and "我答应" in action["reason"] for action in actions), "局部否定不能抹掉销售另外明确作出的承诺"


@pytest.mark.parametrize("text,key", [
    ("李岚说：「我想先微信发材料，沟通时请讲技术细节。」", "communication_channel"),
    ("技术负责人说：我认为预算没有审批，采购要暂缓。", "budget_notes"),
    ("李岚说：“预算30万元，尚未审批。我认为采购要暂缓。”", "budget_notes"),
])
def test_supported_quote_marks_roles_and_multiple_sentences_preserve_customer_voice(attribution_services, text, key):
    matches = [item for item in scan(attribution_services, text) if item["key"] == key]
    assert matches, "被支持的引号、客户角色或引语内换句不能失去客户归因"
    assert all(item["basis"] == "reported" for item in matches)


@pytest.mark.parametrize("text", [
    "客户说预算30万元尚未审批我准备问谁来验收。",
    "李岚说：“预算30万元，尚未审批。”我准备问谁来验收。",
])
def test_clear_speaker_transition_without_comma_keeps_customer_budget(attribution_services, text):
    matches = [item for item in scan(attribution_services, text) if item["key"] == "budget_notes"]
    assert matches, "口述转写缺少逗号、或闭引号后立即换说话人时，明确客户前文仍应保留"
    assert all(item["basis"] == "reported" for item in matches)
    assert all("30万元" in item["evidence"] and "尚未审批" in item["evidence"] for item in matches)
    assert all("我准备问" not in item["evidence"] for item in matches)


def test_explicit_retraction_cannot_leave_withdrawn_customer_budget_reported(attribution_services):
    text = "客户说预算30万元，但刚才我记错了，这不是客户确认。"
    matches = [item for item in scan(attribution_services, text) if item["key"] == "budget_notes" and "30万元" in item["value"]]
    assert all(item["basis"] != "reported" for item in matches), "明确撤回刚才客户归因后，不能继续展示撤回内容为客户事实"


@pytest.mark.parametrize("category", ["idea", "visit_review"])
@pytest.mark.parametrize("text,expected", [
    ("客户未确认预算。", "observation"),
    ("客户没有确认预算。", "observation"),
    ("客户说预算未确认。", "reported"),
])
def test_reflection_customer_non_confirmation_requires_explicit_reporting(attribution_services, category, text, expected):
    matches = [item for item in scan(attribution_services, text, category=category) if item["key"] == "budget_notes"]
    assert matches, "暂未确认的信息仍可作为待核实画像保留"
    assert all(item["basis"] == expected for item in matches), "客户未确认本身不是客户说；明确说的负面反馈仍须保留reported"
