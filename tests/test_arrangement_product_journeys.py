"""Guide journeys use real offline interpretation and disposable CRM data.

Only matter association is scripted so this suite does not claim model routing
quality. Time, authority, agreement, tasks and notices use production services.
"""
import asyncio
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.matters import MatterService
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()
OWNER = "fictional-guide-arrangement-owner"


class ExactMatterAssociation:
    """A chosen existing goal is stable; no invented action or status changes."""

    def __init__(self, service, identifier):
        self.service, self.identifier = service, identifier

    async def route(self, owner, text, scope=None, matter_id=None, mode="auto"):
        detail = self.service.get(owner, matter_id or self.identifier)
        return {"kind": "existing", "matter_id": detail["id"],
                "base_revision": detail["revision"], "actions": [], "updates": {},
                "reason": "用户已选择这个虚构培训目标，保留独立准备待办。"}


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / "fictional-guide-journey.sqlite3")
    matter_service = MatterService(crm, clock=lambda: NOW)
    flow = SecretaryFlow(crm, SalesWorkspace(crm), asyncio.Lock(), clock=lambda: NOW)
    yield crm, flow, matter_service
    flow.close()
    crm.close()


def say(world, text, key, plan=None, matter=None):
    _, flow, _ = world
    body = {"text": text, "request_id": key}
    if plan:
        body.update(plan_id=plan["id"], expected_revision=plan["revision"])
    if matter:
        body.update(matter_id=matter["id"], matter_revision=matter["revision"])
    submitted = flow.submit(OWNER, body)
    assert asyncio.run(flow.process_one())
    result = flow.turn(OWNER, submitted["id"])
    assert result["status"] == "done", result.get("error")
    assert result.get("plan"), result
    return result


def counts(crm):
    return tuple(crm._db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                 for table in ("crm_secretary_plans", "tasks", "notifications"))


def assert_preparation_open(crm, service, identifier, preparation):
    current = crm.get_record(OWNER, preparation["id"])
    assert current["kind"] == "action" and current["status"] == "following"
    assert current["content"] == preparation["content"]
    assert current["original_content"] == preparation["original_content"]
    matter = service.get(OWNER, identifier)
    assert matter["status"] == "following"
    assert preparation["id"] in {item["id"] for item in matter["actions"]}
    assert not any(item["status"] == "done" for item in matter["actions"])


def test_guide_training_deadline_range_self_schedule_and_bare_day_reschedule(world):
    crm, flow, service = world
    preparation = crm.create_record(OWNER, {
        "title": "准备培训讲义", "kind": "action", "status": "following",
        "content": "虚构培训目标需要讲义完善；安排培训时不能宣称这项准备已完成。"
    }, NOW)
    matter = service.create(OWNER, {
        "title": "虚构团队密码培训", "objective": "完成培训并完善讲义",
        "action_record_ids": [preparation["id"]], "request_id": "training-goal"
    })["matter"]
    flow.matter_router = ExactMatterAssociation(service, matter["id"])

    first = say(world, "本周把下个月培训时间定下来，讲义还要准备。", "training-initial", matter=matter)
    plan = first["plan"]
    initial = plan["arrangement"]
    assert initial["settle_deadline"]["time_spec"]["date"] == "2026-10-11"
    period = initial["proposed_execution"]["time_spec"]
    assert (period["precision"], period["window"], period["date"], period["end_date"]) == (
        "window", "calendar", "2026-11-01", "2026-12-01")
    assert not initial["active_schedule"] and counts(crm) == (1, 0, 0)
    assert plan["id"] in {item["plan_id"] for item in flow.arrangements.list(OWNER)["items"]}
    assert "多个时间范围" not in first["question"]
    assert_preparation_open(crm, service, matter["id"], preparation)

    scheduled = say(world, "安排11月12日下午三点，我给团队培训，不用提醒。", "training-scheduled", plan)
    plan = scheduled["plan"]
    actual = plan["active_schedule"]
    task_id = actual["id"]
    assert plan["arrangement"]["decision_mode"] == "self"
    assert plan["arrangement"]["settling_state"] == "settled"
    assert actual["remind_at"] == datetime(2026, 11, 12, 15, tzinfo=SHANGHAI).timestamp()
    assert not scheduled["question"] and counts(crm) == (1, 1, 0)
    assert_preparation_open(crm, service, matter["id"], preparation)

    changed = say(world, "改到13号，几点没定。", "training-date-change", plan)
    plan = changed["plan"]
    proposed = plan["arrangement"]["proposed_execution"]["time_spec"]
    assert proposed["date"] == "2026-11-13" and proposed["precision"] == "date"
    assert plan["date"] == "2026-11-13" and plan["start_at"] is None
    assert plan["arrangement"]["settling_state"] == "pending"
    assert plan["active_schedule"] == actual and plan["task_id"] == task_id
    assert counts(crm) == (1, 1, 0)
    assert_preparation_open(crm, service, matter["id"], preparation)

    completed_time = say(world, "下午三点，我自己决定，按这个安排，不用提醒。", "training-clock", plan)
    plan = completed_time["plan"]
    assert plan["id"] == first["plan"]["id"]
    assert plan["arrangement"]["settling_state"] == "settled"
    assert plan["active_schedule"]["id"] == task_id
    assert plan["active_schedule"]["revision"] > actual["revision"]
    assert plan["active_schedule"]["remind_at"] == datetime(2026, 11, 13, 15, tzinfo=SHANGHAI).timestamp()
    assert counts(crm) == (1, 1, 0) and not completed_time["question"]
    assert_preparation_open(crm, service, matter["id"], preparation)

    # Finishing the activity has different meaning from finishing its materials.
    recapped = say(world, "培训结束了，复盘：讲义还需要完善。", "training-recap", plan)["plan"]
    assert recapped["arrangement"]["settling_state"] == "settled"
    assert not recapped["arrangement"]["followup_enabled"]
    assert_preparation_open(crm, service, matter["id"], preparation)
    assert counts(crm) == (1, 1, 0)


def test_positive_date_agreement_survives_unconfirmed_clock_then_only_time_needs_reply(world):
    crm, _, _ = world
    first = say(world, "11月12日约赵工吃饭，先定日期即可。", "date-first")["plan"]
    second = say(world, "对方已确定12号，钟点还没定。", "date-agreed", first)["plan"]
    arrangement = second["arrangement"]
    assert arrangement["settlement_scope"] == "date_only"
    assert arrangement["settling_state"] == "settled"
    assert arrangement["agreement_status"] == "agreed"
    fields = arrangement["agreement"]["fields"]
    assert set(fields) == {"date"}
    assert arrangement["proposed_execution"]["time_spec"]["date"] == "2026-11-12"
    assert counts(crm) == (1, 0, 0)

    chosen = say(world, "安排12号下午三点，等他确认钟点。", "date-clock-chosen", second)["plan"]
    arrangement = chosen["arrangement"]
    assert arrangement["settlement_scope"] == "execution_time"
    assert arrangement["settling_state"] == "pending"
    assert arrangement["agreement"]["fields"]["date"] == fields["date"]
    assert arrangement["agreement_missing_fields"] == ["time"]
    assert counts(crm) == (1, 0, 0)

    agreed = say(world, "对方已同意12号下午三点，按这个安排，不用提醒。", "date-clock-agreed", chosen)["plan"]
    assert agreed["id"] == first["id"] and agreed["arrangement"]["settling_state"] == "settled"
    assert agreed["arrangement"]["agreement_status"] == "agreed"
    assert set(agreed["arrangement"]["agreement"]["fields"]) == {"date", "time"}
    assert agreed["active_schedule"]["remind_at"] == datetime(2026, 11, 12, 15, tzinfo=SHANGHAI).timestamp()
    assert counts(crm) == (1, 1, 0)


@pytest.mark.parametrize("text", [
    "对方未确认12号，钟点还没定。",
    "对方并非已确定12号，钟点还没定。",
    "对方说还没约好12号，先等回复。",
])
def test_negative_date_agreement_never_settles_or_creates_schedule(world, text):
    crm, _, _ = world
    initial = say(world, "11月12日约赵工吃饭，先定日期即可。", "negative-initial")["plan"]
    result = say(world, text, "negative-reply", initial)["plan"]
    arrangement = result["arrangement"]
    assert arrangement["settling_state"] == "pending"
    assert arrangement["agreement_status"] != "agreed"
    assert not (arrangement.get("agreement") or {}).get("fields")
    assert not result["active_schedule"] and counts(crm) == (1, 0, 0)


@pytest.mark.parametrize("text", [
    "下午三点不同意，对方已确定12号。",
    "对方已确定12号，下午三点不同意。",
])
def test_confirmed_date_and_rejected_clock_never_become_full_agreement_in_either_order(world, text):
    crm, _, _ = world
    initial = say(world, "11月12日约赵工吃饭，先定日期即可。", "rejected-clock-initial")["plan"]
    dated = say(world, "对方已确定12号，钟点还没定。", "rejected-clock-date", initial)["plan"]
    date_signature = dated["arrangement"]["agreement"]["fields"]["date"]["signature"]
    proposal = say(world, "安排12号下午三点，等他确认钟点。", "rejected-clock-proposed", dated)["plan"]
    assert proposal["arrangement"]["agreement_missing_fields"] == ["time"]
    assert counts(crm) == (1, 0, 0)

    result = say(world, text, "rejected-clock-reply", proposal)["plan"]
    arrangement = result["arrangement"]
    assert arrangement["settling_state"] == "pending"
    assert arrangement["agreement"]["fields"]["date"]["signature"] == date_signature
    assert "time" not in arrangement["agreement"]["fields"]
    assert arrangement["agreement_status"] != "agreed"
    assert not result["active_schedule"] and counts(crm) == (1, 0, 0)
