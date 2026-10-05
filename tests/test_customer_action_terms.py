"""F5/G2 service regressions: only source-backed personal execution uses capacity."""

import asyncio
from datetime import datetime

import pytest

from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.parser import SHANGHAI


NOW = datetime(2026, 10, 3, 10, tzinfo=SHANGHAI).timestamp()
WHEN = datetime(2026, 10, 4, 15, tzinfo=SHANGHAI).timestamp()


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / "action-terms-synthetic.sqlite3")
    yield store
    store.close()


def capture(crm, quote, *, kind="commitment", extras=None, more_actions=()):
    class Parser:
        async def parse(self, *_args, **_kwargs):
            return {"intent": "note", "customer_name": None}

    class Organizer:
        async def organize(self, text, now, context):
            action = {"title": "发送方案", "kind": kind, "reason": "原话依据", "evidence": quote,
                      "time_evidence": "明天下午三点", "owner_hint": "我", "remind_at": WHEN}
            return {"summary": "讨论发送方案", "actions": [{**action, **(extras or {})}, *more_actions],
                    "key_points": [], "open_questions": []}

    async def scenario():
        service = CustomerService(crm, Parser(), asyncio.Lock(), clock=lambda: NOW, organizer=Organizer())
        result = await service.handle("owner", "terms-capture", quote, force=True)
        return service, result

    return asyncio.run(scenario())


@pytest.mark.parametrize("duration", [None, 60])
def test_personal_execution_duration_flows_to_proposal_task_and_persisted_terms(crm, duration):
    quote = "我答应明天下午三点发送方案" + (f"，预计{duration}分钟" if duration else "") + "。"
    service, result = capture(crm, quote)
    assert len(result["proposals"]) == 1
    proposal = result["proposals"][0]
    action = result["actions"][0]
    assert action["executor_kind"] == "self"
    assert proposal["duration_minutes"] == (duration or 30)
    assert action["duration_defaulted"] is (duration is None)
    assert ("（默认，可修改）" in result["message"]) is (duration is None)
    child_id = action["record_id"]
    assert crm.get_record("owner", child_id)["action_terms"]["duration_minutes"] == duration
    resumed = service.prepare_analysis_result("owner", result["record_id"])
    assert resumed["actions"][0]["record_id"] == child_id
    assert resumed["proposals"][0]["id"] == proposal["id"]
    assert crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
    crm.execute("owner", "terms-confirm", {"action": "confirm", "proposal_id": proposal["id"]}, NOW)
    assert crm.record_detail("owner", child_id)["task"]["duration_minutes"] == (duration or 30)


@pytest.mark.parametrize("quote,kind", [
    ("客户答应明天下午三点发送方案，预计60分钟。", "customer"),
    ("内部同事答应明天下午三点发送方案，预计60分钟。", "team"),
    ("王经理答应明天下午三点发送方案，预计60分钟。", "unknown"),
])
def test_other_or_unknown_executor_never_automatically_occupies_personal_schedule(crm, quote, kind):
    _, result = capture(crm, quote)
    assert result["actions"][0]["executor_kind"] == kind
    assert result["actions"][0]["record_id"] is None
    assert result["proposals"] == []
    assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    analysis = crm.get_analysis("owner", result["record_id"])
    child = crm.adopt_action("owner", result["record_id"], analysis["actions"][0]["id"], NOW)
    assert child["action_terms"]["executor_kind"] == kind
    assert child["proposal_id"] is None


@pytest.mark.parametrize("extras,quote", [
    ({"evidence": "我答应明天下午三点发送方案"}, "讨论发送方案，时间尚未定。"),
    ({"executor_kind": "self"}, "客户答应明天下午三点发送方案。"),
    ({}, "我答应最迟明天下午三点之前发送方案。"),
    ({}, "我不要明天下午三点发送方案。"),
])
def test_forged_executor_unmatched_quote_deadline_and_cancelled_action_do_not_prepare(crm, extras, quote):
    _, result = capture(crm, quote, extras=extras)
    assert result["proposals"] == []
    assert result["actions"][0]["record_id"] is None


def test_duration_for_other_action_does_not_leak_to_send_action(crm):
    send = "我答应明天下午三点发送方案。"
    meeting = "我答应后天下午三点召开会议，预计60分钟。"
    service, result = capture(crm, send + meeting, extras={"evidence": send}, more_actions=[
        {"title": "召开会议", "kind": "commitment", "reason": "原话依据", "evidence": meeting,
         "owner_hint": "我", "remind_at": WHEN + 86400, "time_evidence": "后天下午三点"}])
    assert [item["duration_minutes"] for item in result["proposals"]] == [30, 60]
    assert [item["duration_defaulted"] for item in result["actions"]] == [True, False]
    assert service.prepare_analysis_result("owner", result["record_id"])["proposals"] == result["proposals"]


def test_old_analysis_without_quotes_is_readable_and_not_implicitly_scheduled(crm):
    _, result = capture(crm, "我答应明天下午三点发送方案。", extras={"evidence": "", "reason": "旧版整理"})
    assert crm.get_analysis("owner", result["record_id"])["actions"]
    assert result["actions"][0]["executor_kind"] == "unknown"
    assert result["proposals"] == []
