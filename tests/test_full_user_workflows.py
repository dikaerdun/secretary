"""Cross-feature user journeys through HTTP on isolated, offline training DBs.

These tests intentionally verify user outcomes across source, project, review,
schedule and completion pages. They are not a substitute for browser UX trials.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime
from functools import wraps

from aiohttp import CookieJar, FormData
from aiohttp.test_utils import TestClient, TestServer
import httpx
import pytest

from deploy.training import OWNER, PASSWORD, OfflineDiscussion, build_training_app, training_config
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI).timestamp()


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture(autouse=True)
def no_external_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic user trial must not create external provider clients")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)


@asynccontextmanager
async def trial(tmp_path):
    app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
    controller = next(route.handler.__self__ for route in app.router.routes()
                      if getattr(route.handler, "__name__", "") == "dashboard")
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        login = await client.post("/api/login", json={"password": PASSWORD})
        assert login.status == 200
        headers = {"X-CSRF-Token": (await login.json())["csrf"]}
        scenes = {item["key"]: item for item in (await (await client.get("/api/training")).json())["scenarios"]}
        yield controller, client, headers, scenes
    finally:
        await client.close()


async def read(client, path):
    response = await client.get(path)
    body = await response.json()
    assert response.status == 200, body
    return body


async def write(client, headers, method, path, payload, status=200):
    response = await client.request(method, path, json=payload, headers=headers)
    body = await response.json()
    assert response.status == status, body
    return body


async def material_ready(client, identifier):
    async with asyncio.timeout(8):
        while True:
            result = await read(client, f"/api/materials/{identifier}")
            if result["material"]["status"] in ("review", "failed"):
                assert result["material"]["status"] == "review", result
                return result
            await asyncio.sleep(.02)


async def create_timed_material(client, headers, customer_id):
    created = await write(client, headers, "POST", "/api/materials", {
        "provider": "manual", "title": "合成用户试用：接口演示会议",
        "category": "meeting", "customer_id": customer_id,
        "occurred_at": NOW - 3600,
        "text": "我答应演示脱敏网关，执行时间2026-10-05 15:00，预计45分钟。"}, 202)
    return await material_ready(client, created["material"]["id"])


async def capture_ready(client, identifier):
    async with asyncio.timeout(8):
        while True:
            result = (await read(client, f"/api/captures/{identifier}"))["capture"]
            if result["status"] not in ("queued", "processing"):
                return result
            await asyncio.sleep(.02)


async def confirm_schedule(client, headers, record_id):
    detail = await read(client, f"/api/records/{record_id}")
    proposal = detail["proposal"]
    assert proposal["status"] == "pending"
    return await write(client, headers, "POST", f"/api/records/{record_id}/confirm", {
        "proposal_id": proposal["id"], "updated_at": proposal["updated_at"]})


@run_async
async def test_no_change_material_save_preserves_project_pending_schedule_and_sources(tmp_path):
    """A user who clicks save after checking unchanged fields loses no decisions."""
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        detail = await create_timed_material(client, headers, project["customer_id"])
        material = detail["material"]
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "material", "entity_id": material["id"],
            "opportunity_id": project["opportunity_ids"][0]})
        action = detail["analysis"]["actions"][0]
        adoption = await write(client, headers, "POST",
            f"/api/materials/{material['id']}/actions/{action['id']}/adopt",
            {"revision": material["revision"]})
        before = await read(client, f"/api/records/{adoption['record']['id']}")
        assert before["proposal"]["status"] == "pending"
        # Exact body emitted by the correction form even when no input changed.
        saved = await write(client, headers, "PATCH", f"/api/materials/{material['id']}", {
            "revision": material["revision"], "title": material["title"],
            "category": material["category"], "customer_id": material["customer_id"],
            "occurred_at": material["occurred_at"]})
        after = await read(client, f"/api/records/{adoption['record']['id']}")
        print({"zero_change": {"revision_before": material["revision"],
            "revision_after": saved["material"]["revision"],
            "proposal_before": before["proposal"]["status"],
            "proposal_after": after["proposal"]["status"]}})
        assert saved["material"]["revision"] == material["revision"]
        assert saved["material"]["status"] == "review"
        assert after["proposal"]["status"] == "pending"
        assert after["record"]["id"] == adoption["record"]["id"]
        refreshed = await read(client, f"/api/materials/{material['id']}")
        assert refreshed["analysis"]["actions"][0]["adopted_record_id"] == adoption["record"]["id"]
        repeated = await write(client, headers, "POST",
            f"/api/materials/{material['id']}/actions/{action['id']}/adopt",
            {"revision": material["revision"]})
        assert repeated["project_inheritance"]["linked"] is True
        assert repeated["record"]["id"] == adoption["record"]["id"]


@pytest.mark.parametrize("unknown_customer", [True, False])
@run_async
async def test_checked_unchanged_material_text_and_null_fields_keep_original_version(tmp_path, unknown_customer):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        payload = {"provider": "manual", "title": "合成用户试用：只有想法，没有预约",
            "text": "现场提到需要更换技术资料展示方式，等我有空再细化。", "category": "auto",
            "customer_id": None if unknown_customer else scenes["lead"]["customer_id"], "occurred_at": None}
        created = await write(client, headers, "POST", "/api/materials", payload, 202)
        detail = await material_ready(client, created["material"]["id"])
        material = detail["material"]
        assignment = controller.crm._db.execute("SELECT customer_assignment FROM crm_materials WHERE id=?",
            (material["id"],)).fetchone()[0]
        # A full form including the unchanged transcript must preserve segments,
        # source type and actual-time unknown state as well as its current category.
        unchanged = await write(client, headers, "PATCH", f"/api/materials/{material['id']}", {
            "revision": material["revision"], "title": "  " + material["title"] + "  ",
            "text": detail["text"], "category": material["category"],
            "customer_id": material["customer_id"], "occurred_at": None})
        current = await read(client, f"/api/materials/{material['id']}")
        assert unchanged["material"]["revision"] == material["revision"]
        assert current["versions"] == detail["versions"]
        assert current["segments"] == detail["segments"] and current["source"] == detail["source"]
        assert current["material"]["occurred_at"] is None
        assert controller.crm._db.execute("SELECT customer_assignment FROM crm_materials WHERE id=?",
            (material["id"],)).fetchone()[0] == assignment


@run_async
async def test_true_material_revision_and_explicit_retry_still_require_new_review(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        detail = await create_timed_material(client, headers, scenes["projects"]["customer_id"])
        material, action = detail["material"], detail["analysis"]["actions"][0]
        adopted = await write(client, headers, "POST", f"/api/materials/{material['id']}/actions/{action['id']}/adopt",
            {"revision": material["revision"]})
        confirmed = await confirm_schedule(client, headers, adopted["record"]["id"])
        original_time = confirmed["task"]["remind_at"]
        corrected_text = "我答应演示脱敏网关，执行时间2026-10-05 16:00，预计45分钟。"
        corrected = await write(client, headers, "PATCH", f"/api/materials/{material['id']}", {
            "revision": material["revision"], "text": corrected_text, "customer_id": None, "occurred_at": None})
        assert corrected["material"]["revision"] == material["revision"] + 1
        assert corrected["material"]["customer_id"] is None
        current = await material_ready(client, material["id"])
        assert current["text"] == corrected_text and current["original_version"]["text"] == detail["text"]
        assert len(current["versions"]) == len(detail["versions"]) + 1
        original_action = await read(client, f"/api/records/{adopted['record']['id']}")
        assert original_action["task"]["status"] == "pending" and original_action["task"]["remind_at"] == original_time
        assert original_action["record"]["customer_id"] == scenes["projects"]["customer_id"]
        assert any(item["id"] == adopted["record"]["id"] for item in current["previous_adoptions"])
        # Unlike an unchanged save, pressing the explicit reprocess button is a
        # deliberate new processing attempt; transcript versions remain immutable.
        retry = await write(client, headers, "POST", f"/api/materials/{material['id']}/retry",
            {"revision": current["material"]["revision"]}, 202)
        assert retry["material"]["revision"] == current["material"]["revision"] + 1
        retried = await material_ready(client, material["id"])
        assert retried["text"] == corrected_text and retried["versions"] == current["versions"]
        assert (await read(client, f"/api/records/{adopted['record']['id']}"))["task"]["remind_at"] == original_time


@run_async
async def test_new_customer_contacts_evidence_and_independent_projects_are_usable_everywhere(tmp_path):
    async with trial(tmp_path) as (_, client, headers, _):
        created = await write(client, headers, "POST", "/api/customers", {
            "name": "合成客户：东港安全研究院", "aliases": ["东港院"], "contact": "合成李工",
            "phone": "13800000001", "stage": "qualified", "notes": "展会相识"}, 201)
        customer = created["customer"]
        technical = (await write(client, headers, "POST", f"/api/customers/{customer['id']}/contacts", {
            "name": "合成陈主任", "role": "技术评审", "phone": "13800000002"}, 201))["contact"]
        await write(client, headers, "PATCH", f"/api/customers/{customer['id']}/contacts/{technical['id']}",
            {"role": "技术评审与验收", "phone": "13800000003"})
        source_text = "陈主任说先发一页概览。客户明确要病历签名验签。我观察预算尚未审批。"
        source = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：客户画像依据", "content": source_text, "kind": "note",
            "category": "conversation", "customer_id": customer["id"]}, 201))["record"]
        for fact in (
            {"key": "crypto_needs", "value": "病历签名验签", "basis": "reported", "evidence": "客户明确要病历签名验签"},
            {"key": "blockers", "value": "预算尚未审批，待核实", "basis": "observation", "evidence": "我观察预算尚未审批"},
            {"key": "detail_preference", "value": "先发一页概览", "basis": "reported", "contact_id": technical["id"],
             "evidence": "陈主任说先发一页概览"}):
            await write(client, headers, "POST", f"/api/customers/{customer['id']}/facts", {**fact, "source_record_id": source["id"]})
        projects = []
        for title, amount, kind, approval in (
            ("合成签名项目", 12000000, "budget", "approved"),
            ("合成加密项目", 4000000, "estimate", "unconfirmed")):
            projects.append((await write(client, headers, "POST", f"/api/customers/{customer['id']}/opportunities", {
                "name": title, "amount_cents": amount, "amount_type": kind, "approval": approval,
                "contact_ids": [technical["id"]]}, 201))["opportunity"])
        bench = await read(client, f"/api/customers/{customer['id']}/workbench")
        assert {item["id"] for item in bench["opportunities"]["items"]} == {item["id"] for item in projects}
        profile = await read(client, f"/api/customers/{customer['id']}/profile")
        assert len(profile["contacts"]) == 2
        assert {item["basis"] for item in profile["fields"]} == {"reported", "observation"}
        person = next(item for item in profile["contacts"] if item["id"] == technical["id"])
        assert any(item["key"] == "detail_preference" for item in person["fields"])
        found = await read(client, "/api/customers?q=东港院")
        assert found["total"] == 1 and found["items"][0]["id"] == customer["id"]
        candidates = await write(client, headers, "POST", "/api/customer-candidates", {"text": "东港院陈主任那个合成签名项目"})
        assert any(item["customer_id"] == customer["id"] for item in candidates["items"])
        assert candidates["selected_customer_id"] is None
        overview = await read(client, "/api/overview")
        visible = [item for item in overview["projects"] if item["customer_id"] == customer["id"]]
        assert {(item["amount_cents"], item["amount_type"], item["approval"]) for item in visible} == {
            (12000000, "budget", "approved"), (4000000, "estimate", "unconfirmed")}


@run_async
async def test_capture_raw_correction_discussion_adoption_completion_and_project_continuation(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        raw, corrected = "医院晨工那个千名项目，要问验收负责人", "医院陈工那个签名项目，要问验收负责人"
        body = {"text": corrected, "original_transcript": raw, "request_id": "synthetic-voice-recap"}
        first, duplicate = await asyncio.gather(*[write(client, headers, "POST", "/api/captures", body, 202) for _ in range(2)])
        capture = first["capture"]
        assert capture["id"] == duplicate["capture"]["id"]
        ready = await capture_ready(client, capture["id"])
        assert ready["record"]["original_content"] == raw and ready["record"]["content"] == corrected
        assert ready["record"]["customer_id"] is None and ready["record"]["task_id"] is None
        await write(client, headers, "POST", f"/api/captures/{capture['id']}/classify", {
            "purpose": "project_reference", "customer_id": project["customer_id"],
            "opportunity_id": project["opportunity_ids"][1], "expected_updated_at": ready["record"]["updated_at"]})
        await capture_ready(client, capture["id"])
        thread = await write(client, headers, "POST", "/api/sales-discussions", {
            "customer_id": project["customer_id"], "opportunity_id": project["opportunity_ids"][1],
            "source_record_id": ready["record_id"], "title": "合成用户试用：签名验收推进"}, 201)
        identifier = thread["thread"]["id"]
        for index, question in enumerate(("我下一步应该问谁？", "如果客户说预算未批，应该如何验证这点？")):
            reply = await write(client, headers, "POST", f"/api/sales-discussions/{identifier}/messages",
                {"text": question, "request_id": f"user-trial-message-{index}"})
        assert [item["role"] for item in reply["messages"]] == ["user", "assistant", "user", "assistant"]
        assistant = reply["messages"][-1]
        adoption_path = f"/api/sales-discussions/{identifier}/messages/{assistant['id']}/actions/1/adopt"
        action = (await write(client, headers, "POST", adoption_path, {}))["record"]
        repeated = (await write(client, headers, "POST", adoption_path, {}))["record"]
        assert action["id"] == repeated["id"] and action["task_id"] is None
        detail = await read(client, f"/api/records/{action['id']}")
        assert detail["record"]["opportunity_id"] == project["opportunity_ids"][1]
        done = await write(client, headers, "POST", f"/api/records/{action['id']}/complete-outcome", {
            "request_id": "finish-discussion-action", "result": "客户明确陈工负责技术评审，采购负责人仍未确认",
            "next_step": "请陈工引荐采购负责人", "expected_snapshot": detail["completion_snapshot"]})
        next_record = done["next_record"]
        assert done["record"]["status"] == "done" and next_record["proposal_id"] is None
        assert (await read(client, f"/api/records/{next_record['id']}"))["record"]["opportunity_id"] == project["opportunity_ids"][1]
        overview = await read(client, "/api/overview")
        assert action["id"] not in {item["record_id"] for item in overview["my_actions"]}
        assert next_record["id"] in {item["record_id"] for item in overview["my_actions"]}
        assert capture["record_id"] not in {item["record_id"] for item in overview["my_actions"]}
        persisted = await read(client, f"/api/sales-discussions/{identifier}")
        assert len(persisted["messages"]) == 4
        assert persisted["messages"][-1]["data"]["next_moves"][0]["adopted_record_id"] == action["id"]


@run_async
async def test_waiting_customer_and_team_promises_complete_into_my_next_step_without_execution_time(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        customer_id = scenes["projects"]["customer_id"]
        for executor in ("customer", "team"):
            record = (await write(client, headers, "POST", "/api/records", {
                "title": f"合成用户试用：等{executor}确认接口", "kind": "action", "status": "following",
                "customer_id": customer_id}, 201))["record"]
            await write(client, headers, "PATCH", f"/api/records/{record['id']}/terms", {
                "executor_kind": executor, "executor_evidence": "对接人答应提供接口清单", "check_date": "2026-10-03",
                "deadline_date": "2026-10-06", "expected_updated_at": None})
            overview = await read(client, "/api/overview")
            assert record["id"] in {item["record_id"] for item in overview["waiting_actions"]}
            assert record["id"] not in {item["record_id"] for item in overview["my_actions"]}
            detail = await read(client, f"/api/records/{record['id']}")
            assert detail["task"] is None
            completed = await write(client, headers, "POST", f"/api/records/{record['id']}/complete-outcome", {
                "request_id": "completed-waiting-" + executor, "result": "已收到接口清单",
                "next_step": "我核对清单，准备技术交流", "expected_snapshot": detail["completion_snapshot"]})
            followup = completed["next_record"]
            assert followup["task_id"] is None
            await write(client, headers, "PATCH", f"/api/records/{followup['id']}/terms", {
                "executor_kind": "self", "expected_updated_at": None})
            after = await read(client, "/api/overview")
            assert record["id"] not in {item["record_id"] for item in after["waiting_actions"]}
            assert followup["id"] in {item["record_id"] for item in after["my_actions"]}
            agenda = await read(client, "/api/agenda?period=month&date=2026-10-03")
            assert record["id"] not in {item["record_id"] for item in agenda["planning_nodes"]}


@run_async
async def test_schedule_views_change_rejection_confirmation_and_cancellation_keep_one_live_task(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：安排技术访谈", "kind": "action", "status": "following",
            "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
        initial_time = NOW + 86400 + 3600
        calendar_seen = await read(client, f"/api/records/{record['id']}")
        assert re.fullmatch(r'[0-9a-f]{64}', calendar_seen['schedule_snapshot'])
        schedule = await write(client, headers, "POST", f"/api/records/{record['id']}/schedule", {
            "remind_at": initial_time, "duration_minutes": 45, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
        assert schedule["task"] is None and schedule["proposal"]["status"] == "pending"
        confirmed = await confirm_schedule(client, headers, record["id"])
        task_id = confirmed["task"]["id"]
        for period in ("day", "week", "month"):
            agenda = await read(client, f"/api/agenda?period={period}&date=2026-10-04")
            entry = next(item for item in agenda["items"] if item["id"] == task_id)
            assert entry["remind_at"] == initial_time and entry["duration_minutes"] == 45
        changed_time = initial_time + 3600
        calendar_seen = await read(client, f"/api/records/{record['id']}")
        assert re.fullmatch(r'[0-9a-f]{64}', calendar_seen['schedule_snapshot'])
        change = await write(client, headers, "POST", f"/api/records/{record['id']}/schedule", {
            "remind_at": changed_time, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
        assert change["task"]["remind_at"] == initial_time
        proposal = change["proposal"]
        await write(client, headers, "POST", f"/api/proposals/{proposal['id']}/reject", {"updated_at": proposal["updated_at"]})
        preserved = await read(client, f"/api/records/{record['id']}")
        assert preserved["task"]["remind_at"] == initial_time and preserved["task"]["status"] == "pending"
        assert re.fullmatch(r'[0-9a-f]{64}', preserved['schedule_snapshot'])
        await write(client, headers, "POST", f"/api/records/{record['id']}/schedule", {
            "remind_at": changed_time, "duration_minutes": 30, "expected_schedule_snapshot": preserved['schedule_snapshot']})
        revised = await confirm_schedule(client, headers, record["id"])
        assert revised["task"]["id"] == task_id and revised["task"]["remind_at"] == changed_time
        agenda = await read(client, "/api/agenda?period=day&date=2026-10-04")
        assert len([item for item in agenda["items"] if item["id"] == task_id]) == 1
        calendar_seen = await read(client, f"/api/records/{record['id']}")
        assert re.fullmatch(r'[0-9a-f]{64}', calendar_seen['schedule_snapshot'])
        cancelled = await write(client, headers, "POST", f"/api/records/{record['id']}/cancel", {"expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
        assert cancelled["task"]["status"] == "cancelled"
        assert cancelled["record"]["status"] != "done"
        assert cancelled["record"]["original_content"] == record["original_content"]


@run_async
async def test_next_visit_from_confirmed_project_keeps_that_project(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        source = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：项目现场交流", "content": "客户陈工提出签名接口需要再对齐。", "kind": "note",
            "category": "conversation", "customer_id": project["customer_id"]}, 201))["record"]
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "record", "entity_id": source["id"], "opportunity_id": project["opportunity_ids"][1]})
        visit = await write(client, headers, "POST", f"/api/records/{source['id']}/next-visit", {
            "title": "合成用户试用：再访陈工确认签名接口"}, 201)
        detail = await read(client, f"/api/records/{visit['record']['id']}")
        print({"next_visit": {"customer_id": detail["record"]["customer_id"],
            "opportunity_id": detail["record"].get("opportunity_id"), "expected_project": project["opportunity_ids"][1]}})
        assert detail["record"]["opportunity_id"] == project["opportunity_ids"][1]
        assert detail["record"]["task_id"] is None


@run_async
async def test_all_nine_navigation_data_reads_and_search_pagination_preserve_new_notes(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        markers = []
        for index in range(3):
            created = await write(client, headers, "POST", "/api/captures", {
                "text": f"合成试用独有词：三分钟见客户想法第{index}条", "request_id": f"nav-phrase-{index}"}, 202)
            markers.append(created["capture"])
        for capture in markers:
            await capture_ready(client, capture["id"])
        for path in ("/api/dashboard", "/api/overview", "/api/sales-discussions", "/api/records?status=unfiled",
                     "/api/visits", "/api/materials", "/api/customers", "/api/records?kind=action",
                     "/api/agenda?period=day&date=2026-10-03"):
            assert isinstance(await read(client, path), dict)
        pages = [await read(client, f"/api/records?q=合成试用独有词&page={page}&page_size=2") for page in (1, 2)]
        assert [page["total"] for page in pages] == [3, 3]
        assert {record["id"] for page in pages for record in page["items"]} == {item["record_id"] for item in markers}
        inbox = await read(client, "/api/records?status=unfiled&page_size=100")
        assert {item["record_id"] for item in markers}.issubset({item["id"] for item in inbox["items"]})
        for capture in markers:
            saved = await read(client, f"/api/records/{capture['record_id']}")
            assert saved["record"]["original_content"] == capture["record"]["original_content"]
        caps = await read(client, "/api/materials/capabilities")
        assert caps["discovery"] is False and caps["configured"] is False
        runtime = (await read(client, "/api/session"))["runtime"]
        assert runtime["training"] is True and runtime["wecom_connected"] is False
        assert (await client.get("/guide")).status == 200


@run_async
async def test_voice_vocabulary_updates_from_customer_and_manual_capture_works_without_asr(tmp_path):
    async with trial(tmp_path) as (_, client, headers, _):
        customer = (await write(client, headers, "POST", "/api/customers", {
            "name": "合成客户：山海密码中心", "aliases": ["山海中心"], "contact": "合成宋工"}, 201))["customer"]
        words = await write(client, headers, "PATCH", "/api/voice-settings", {"hotwords": ["签名验签", "签名验签", "  密钥轮换  "]})
        assert words["hotwords"] == ["签名验签", "密钥轮换"]
        assert {"合成客户：山海密码中心", "山海中心", "合成宋工"}.issubset(set(words["automatic_hotwords"]))
        assert (await read(client, "/api/voice-settings")) == words
        capabilities = await read(client, "/api/audio/capabilities")
        assert capabilities["can_transcribe"] is False
        form = FormData()
        form.add_field("audio", b"synthetic-audio-not-sent-externally", filename="synthetic.wav", content_type="audio/wav")
        unavailable = await client.post("/api/audio/transcribe", data=form, headers=headers)
        assert unavailable.status == 503
        created = await write(client, headers, "POST", "/api/captures", {
            "text": "宋工提到下周需要再核对密钥轮换流程，时间还没说。",
            "original_transcript": "送工提到下周需要再核对密钥轮换流程，时间还没说。",
            "customer_id": customer["id"], "request_id": "fallback-manual-audio"}, 202)
        saved = await capture_ready(client, created["capture"]["id"])
        assert saved["record"]["customer_id"] == customer["id"]
        assert saved["record"]["original_content"].startswith("送工") and saved["record"]["content"].startswith("宋工")


@run_async
async def test_provider_failure_keeps_capture_and_explicit_retry_can_finish_same_source(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        original = controller.captures.organizer
        class Failure:
            async def organize(self, *args, **kwargs):
                raise RuntimeError("synthetic-provider-failure-private-message-must-not-leak")
        controller.captures.organizer = Failure()
        created = await write(client, headers, "POST", "/api/captures", {
            "text": "我答应发送数据库加密产品资料，没有约定执行时间。", "request_id": "failing-model-input"}, 202)
        failed = await capture_ready(client, created["capture"]["id"])
        assert failed["status"] == "failed" and "private-message" not in failed["error"]
        assert failed["record"]["original_content"] == created["capture"]["record"]["content"]
        controller.captures.organizer = original
        await write(client, headers, "POST", f"/api/captures/{failed['id']}/retry", {}, 202)
        ready = await capture_ready(client, failed["id"])
        assert ready["status"] == "review" and ready["record_id"] == failed["record_id"]
        assert len(ready["analysis"]["actions"]) == 1
        assert ready["record"]["customer_id"] is None
        note = await write(client, headers, "POST", f"/api/captures/{failed['id']}/classify", {
            "purpose": "note", "customer_id": scenes["lead"]["customer_id"],
            "expected_updated_at": ready["record"]["updated_at"]})
        latest = await capture_ready(client, failed["id"])
        assert latest["record"]["original_content"] == failed["record"]["original_content"]
        assert latest["record"]["kind"] == "note"


@run_async
async def test_customer_voice_draft_confirmation_rejection_and_readback_are_separate_from_tasks(tmp_path):
    async with trial(tmp_path) as (_, client, headers, _):
        phrase = "新建客户霁星数据研究院，联系人李工，关注数据库加密和密钥管理。"
        before = (await read(client, "/api/customers"))["total"]
        command = await write(client, headers, "POST", "/api/customer-command", {"text": phrase, "category": "visit_review"})
        draft = command["draft"]
        assert draft["status"] == "pending" and (await read(client, "/api/customers?q=霁星"))["total"] == 0
        queue = await read(client, "/api/review-inbox")
        assert any(item.get("id") == draft["id"] and item["review_type"] == "customer" for item in queue["items"])
        confirmed = await write(client, headers, "POST", f"/api/customer-drafts/{draft['id']}/confirm", {})
        assert confirmed["draft"]["status"] == "confirmed"
        customer_id = confirmed["customer_id"]
        profile = await read(client, f"/api/customers/{customer_id}/profile")
        assert profile["customer"]["name"] == "霁星数据研究院"
        assert profile["contacts"][0]["name"] == "李工"
        assert profile["fields"][0]["key"] == "crypto_needs"
        assert (await read(client, "/api/customers"))["total"] == before + 1
        source = await read(client, f"/api/records/{command['record_id']}")
        assert source["record"]["original_content"] == phrase and source["task"] is None
        # A second update draft can be rejected without undoing the first approved archive.
        second = await write(client, headers, "POST", "/api/customer-command", {"text": phrase, "customer_id": customer_id})
        if second.get("draft"):
            rejected = await write(client, headers, "POST", f"/api/customer-drafts/{second['draft']['id']}/reject", {})
            assert rejected["draft"]["status"] == "rejected"
        assert (await read(client, "/api/customers?q=霁星"))["total"] == 1


@run_async
async def test_exchange_two_sources_review_decisions_and_adoption_have_one_action_identity(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：一次交流多份来源", "customer_id": project["customer_id"],
            "request_key": "user-trial-visit"}, 201))["visit"]
        materials = []
        phrase = "我答应整理病历签名接口问题，没有约定执行时间。"
        for role in ("recap", "recording"):
            body = {"role": role, "provider": "manual", "title": "合成" + role, "text": phrase,
                "request_key": "exchange-source-" + role}
            first = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", body, 202)
            duplicate = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", body, 202)
            assert first["material"]["id"] == duplicate["material"]["id"]
            materials.append(first["material"])
            await material_ready(client, first["material"]["id"])
        detail = await read(client, f"/api/visits/{visit['id']}")
        assert len(detail["sources"]) == 2 and len(detail["actions"]) == 1
        assert len(detail["actions"][0]["references"]) == 2
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "visit", "entity_id": visit["id"], "opportunity_id": project["opportunity_ids"][1]})
        # Linking a project changes the scope the user reviewed. An old action
        # page must fail without adoption; refresh and inspect the new scope.
        old_action = detail["actions"][0]
        await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{old_action['key']}/adopt",
            {"revision": detail["visit"]["revision"]}, 400)
        refreshed = await read(client, f"/api/visits/{visit['id']}")
        assert refreshed["visit"]["revision"] != detail["visit"]["revision"]
        assert refreshed["actions"][0]["adopted_record_id"] is None
        assert refreshed["actions"][0]["project_scope"]["opportunity_id"] == project["opportunity_ids"][1]
        detail = refreshed
        item = next(value for value in (await read(client, "/api/review-inbox"))["items"]
                    if value.get("visit_id") == visit["id"] and value["review_type"] == "action")
        decision = {"key": item["key"], "signature": item["signature"]}
        for choice, state_name in (("dismiss", "dismissed"), ("defer", "deferred")):
            extra = {"until_at": NOW + 86400} if choice == "defer" else {}
            await write(client, headers, "POST", "/api/review-decisions", {**decision, "decision": choice, **extra})
            for material in materials:
                source = await read(client, f"/api/materials/{material['id']}")
                assert source["analysis"]["actions"][0]["decision_state"] == state_name
            assert (await read(client, f"/api/visits/{visit['id']}"))["actions"][0]["decision_state"] == state_name
            await write(client, headers, "POST", "/api/review-decisions", {**decision, "decision": "reset"})
        action = detail["actions"][0]
        adoption = await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
            {"revision": detail["visit"]["revision"]})
        record_id = adoption["record"]["id"]
        assert adoption["record"]["proposal_id"] is None
        record = await read(client, f"/api/records/{record_id}")
        assert record["record"]["opportunity_id"] == project["opportunity_ids"][1]
        for material in materials:
            source = await read(client, f"/api/materials/{material['id']}")
            alias = source["analysis"]["actions"][0]
            repeated = await write(client, headers, "POST", f"/api/materials/{material['id']}/actions/{alias['id']}/adopt",
                {"revision": source["material"]["revision"]})
            assert repeated["record"]["id"] == record_id
        reviews = await read(client, "/api/review-inbox")
        assert item["key"] not in {item["key"] for item in reviews["items"]}


@run_async
async def test_failed_listen_source_exclusion_is_reversible_and_manual_transcript_preserves_title(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：聆记暂不能读取", "customer_id": scenes["lead"]["customer_id"]}, 201))["visit"]
        manual = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
            "role": "recap", "provider": "manual", "title": "合成口述复盘",
            "text": "我答应发送数据库加密产品资料，没有约定执行时间。"}, 202)
        await material_ready(client, manual["material"]["id"])
        failed = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
            "role": "recording", "provider": "listen_note", "title": "合成录音完整标题：原样保留"}, 202)
        async with asyncio.timeout(8):
            while True:
                source = await read(client, f"/api/materials/{failed['material']['id']}")
                if source["material"]["status"] == "failed": break
                await asyncio.sleep(.02)
        detail = await read(client, f"/api/visits/{visit['id']}")
        action = detail["actions"][0]
        assert action["needs_review"] is True
        await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
            {"revision": detail["visit"]["revision"]}, 400)
        decision_path = f"/api/visits/{visit['id']}/sources/{failed['material']['id']}/decision"
        await write(client, headers, "POST", decision_path, {"revision": detail["visit"]["revision"], "use": "excluded"}, 400)
        excluded = await write(client, headers, "POST", decision_path, {
            "revision": detail["visit"]["revision"], "use": "excluded", "reason": "录音暂不能读取，本次只用已核对复盘"})
        assert excluded["actions"][0]["needs_review"] is False
        included = await write(client, headers, "POST", decision_path, {
            "revision": excluded["visit"]["revision"], "use": "included", "reason": "准备补完整原文"})
        assert included["actions"][0]["needs_review"] is True
        corrected = await write(client, headers, "PATCH", f"/api/materials/{failed['material']['id']}", {
            "revision": source["material"]["revision"], "text": "我答应发送数据库加密产品资料，没有约定执行时间。"})
        ready = await material_ready(client, failed["material"]["id"])
        assert ready["material"]["provider"] == "listen_note" and ready["material"]["title"] == source["material"]["title"]
        assert ready["source"] == "LOCAL_REVISION"
        current = await read(client, f"/api/visits/{visit['id']}")
        assert current["actions"][0]["needs_review"] is False and len(current["sources"]) == 2


@run_async
async def test_ai_discussion_failure_preserves_question_same_request_retry_and_saved_context(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        project = scenes["projects"]
        thread = await write(client, headers, "POST", "/api/sales-discussions", {
            "customer_id": project["customer_id"], "opportunity_id": project["opportunity_ids"][1],
            "title": "合成用户试用：失败后继续讨论"}, 201)
        identifier = thread["thread"]["id"]
        class Failure:
            async def reply(self, *args, **kwargs):
                raise RuntimeError("synthetic-private-provider-failure")
        controller.discussions.advisor = Failure()
        body = {"text": "我担心客户还没确认试点范围，该怎么问？", "request_id": "failed-question-preserved"}
        failed = await write(client, headers, "POST", f"/api/sales-discussions/{identifier}/messages", body)
        assert len(failed["messages"]) == 1 and failed["messages"][0]["status"] == "failed"
        assert failed["messages"][0]["text"] == body["text"]
        assert "private-provider" not in failed["messages"][0]["error"]
        controller.discussions.advisor = OfflineDiscussion()
        retried = await write(client, headers, "POST", f"/api/sales-discussions/{identifier}/messages", body)
        assert len(retried["messages"]) == 2
        assert retried["messages"][0]["id"] == failed["messages"][0]["id"]
        assert retried["messages"][0]["status"] == "complete"
        repeated = await write(client, headers, "POST", f"/api/sales-discussions/{identifier}/messages", body)
        assert repeated["messages"] == retried["messages"]
        assert repeated["thread"]["opportunity_id"] == project["opportunity_ids"][1]


@run_async
async def test_dashboard_priority_defer_reset_and_actual_source_changes_do_not_modify_tasks(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        identifier = scenes["waiting"]["record_ids"][0]
        detail = await read(client, f"/api/records/{identifier}")
        task = detail["task"]
        item = next(item for item in (await read(client, "/api/priorities"))["items"] if item.get("record_id") == identifier)
        for choice in ("defer", "dismiss"):
            await write(client, headers, "POST", "/api/priority-decisions", {
                "key": item["key"], "signature": item["signature"], "decision": choice,
                **({"until_at": NOW + 86400} if choice == "defer" else {})})
            assert item["key"] not in {value["key"] for value in (await read(client, "/api/priorities"))["items"]}
            assert (await read(client, f"/api/records/{identifier}"))["task"] == task
            await write(client, headers, "POST", "/api/priority-decisions", {"key": item["key"], "signature": item["signature"], "decision": "reset"})
            assert item["key"] in {value["key"] for value in (await read(client, "/api/priorities"))["items"]}
        await write(client, headers, "POST", "/api/priority-decisions", {"key": item["key"], "signature": item["signature"], "decision": "dismiss"})
        await write(client, headers, "PATCH", f"/api/records/{identifier}", {"content": "新的实际进展：客户已指定技术负责人。"})
        changed = next(value for value in (await read(client, "/api/priorities"))["items"] if value["key"] == item["key"])
        assert changed["source_changed_after_decision"] is True
        assert (await read(client, f"/api/records/{identifier}"))["task"] == task


@pytest.mark.parametrize("project_state", ["no_link", "stale", "current"])
@run_async
async def test_next_visit_project_boundaries_and_future_schedule_remain_explicit_proposals(tmp_path, project_state):
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        source = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：拜访接续边界", "content": "现场只讨论签名验签接口。", "kind": "note",
            "category": "conversation", "customer_id": project["customer_id"]}, 201))["record"]
        if project_state != "no_link":
            await write(client, headers, "POST", "/api/opportunity-links", {
                "entity_type": "record", "entity_id": source["id"], "opportunity_id": project["opportunity_ids"][1]})
        if project_state == "stale":
            await write(client, headers, "PATCH", f"/api/records/{source['id']}", {
                "content": "后来补充：可能改为另一个系统，需要再核对项目。"})
        result = await write(client, headers, "POST", f"/api/records/{source['id']}/next-visit", {
            "title": "合成用户试用：明确未来拜访", "remind_at": NOW + 7200, "duration_minutes": 30}, 201)
        assert result["proposal"]["status"] == "pending" and result["task"] is None
        detail = await read(client, f"/api/records/{result['record']['id']}")
        assert detail["record"]["customer_id"] == project["customer_id"]
        if project_state == "current":
            assert result["project_inheritance"]["linked"] is True
            assert detail["record"]["opportunity_id"] == project["opportunity_ids"][1]
        else:
            assert result["project_inheritance"]["linked"] is False
            assert detail["record"].get("opportunity_id") is None
            if project_state == "stale":
                assert "项目" in result["warning"]
            else:
                assert not result.get("warning")


@run_async
async def test_no_change_exchange_metadata_save_preserves_explicit_project_link(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        project = scenes["projects"]
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：核对后保存相同交流信息", "customer_id": project["customer_id"],
            "occurred_at": NOW - 3600}, 201))["visit"]
        source = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
            "role": "recording", "provider": "manual", "title": "合成正式交流",
            "text": "我答应整理病历签名接口问题，没有约定执行时间。"}, 202)
        await material_ready(client, source["material"]["id"])
        detail = await read(client, f"/api/visits/{visit['id']}")
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "visit", "entity_id": visit["id"], "opportunity_id": project["opportunity_ids"][1]})
        before = await read(client, f"/api/customers/{project['customer_id']}/workbench")
        link = next(item for item in before["opportunity_links"] if item["entity_type"] == "visit" and item["entity_id"] == visit["id"])
        assert link["stale"] is False
        stale = detail["visit"]
        await write(client, headers, "PATCH", f"/api/visits/{visit['id']}", {
            "revision": stale["revision"], "title": stale["title"],
            "customer_id": stale["customer_id"], "occurred_at": stale["occurred_at"]}, 400)
        detail = await read(client, f"/api/visits/{visit['id']}")
        assert detail["visit"]["revision"] != stale["revision"]
        assert all(detail["visit"][key] == stale[key] for key in ("title", "customer_id", "occurred_at"))
        current = detail["visit"]
        saved = await write(client, headers, "PATCH", f"/api/visits/{visit['id']}", {
            "revision": current["revision"], "title": current["title"],
            "customer_id": current["customer_id"], "occurred_at": current["occurred_at"]})
        after = await read(client, f"/api/customers/{project['customer_id']}/workbench")
        link_after = next(item for item in after["opportunity_links"] if item["entity_type"] == "visit" and item["entity_id"] == visit["id"])
        print({"zero_change_exchange": {"project_link_before_stale": link["stale"], "project_link_after_stale": link_after["stale"]}})
        assert link_after["stale"] is False
        assert saved["visit"]["revision"] == current["revision"]


@run_async
async def test_explicit_exchange_merge_keeps_all_evidence_and_material_versions(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        class SynonymOrganizer:
            async def organize(self, text, now, context):
                quote = next(sentence for sentence in text.split("。") if "我答应" in sentence)
                title = "提交接口问题清单" if "提交" in quote else "整理接口问题清单"
                return {"summary": "合成固定同义复盘示例", "key_points": [], "open_questions": [],
                    "actions": [{"title": title, "kind": "commitment", "reason": "合成固定例句",
                        "owner_hint": "我", "evidence": quote, "remind_at": None}]}
        controller.materials.organizer = SynonymOrganizer()
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：整理与提交是同一承诺", "customer_id": scenes["projects"]["customer_id"]}, 201))["visit"]
        sources = []
        for role, text in (("recording", "我答应整理接口问题清单。"), ("recap", "我答应提交接口问题清单。")):
            source = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
                "role": role, "provider": "manual", "title": "合成" + role, "text": text}, 202)
            sources.append(await material_ready(client, source["material"]["id"]))
        current = await read(client, f"/api/visits/{visit['id']}")
        assert len(current["actions"]) == 2
        merged = await write(client, headers, "POST", f"/api/visits/{visit['id']}/merge", {
            "revision": current["visit"]["revision"], "keys": [item["key"] for item in current["actions"]]})
        assert len(merged["actions"]) == 1 and len(merged["actions"][0]["references"]) == 2
        action = merged["actions"][0]
        adoption = await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
            {"revision": merged["visit"]["revision"]})
        assert adoption["record"]["task_id"] is None
        reread = await read(client, f"/api/visits/{visit['id']}")
        assert reread["actions"][0]["adopted_record_id"] == adoption["record"]["id"]
        for source in sources:
            now = await read(client, f"/api/materials/{source['material']['id']}")
            assert now["versions"] == source["versions"] and now["text"] == source["text"]


@run_async
async def test_original_task_spoken_change_is_proposed_and_not_archived_as_an_unrelated_meeting(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record_id = scenes["changes"]["record_ids"][0]
        original = await read(client, f"/api/records/{record_id}")
        proposals = scenes["changes"]["proposal_ids"]
        # Reject the teaching proposal first, then practice a new spoken change.
        old = original["proposal"]
        await write(client, headers, "POST", f"/api/proposals/{old['id']}/reject", {"updated_at": old["updated_at"]})
        count = (await read(client, "/api/visits"))["total"]
        changed = await write(client, headers, "POST", f"/api/records/{record_id}/activities/organize", {
            "content": "改到2026-10-06下午3点，预计45分钟", "request_id": "original-task-spoken-revision"})
        assert (await read(client, "/api/visits"))["total"] == count
        current = await read(client, f"/api/records/{record_id}")
        assert current["task"]["remind_at"] == original["task"]["remind_at"]
        assert current["proposal"]["status"] == "pending" and current["proposal"]["target_task_id"] == original["task"]["id"]
        assert current["proposal"]["duration_minutes"] == 45
        saved = await confirm_schedule(client, headers, record_id)
        assert saved["task"]["remind_at"] != original["task"]["remind_at"]
        assert saved["task"]["id"] == original["task"]["id"]


@run_async
async def test_manual_progress_archive_customer_advice_adoption_and_feedback_work_as_one_customer_journey(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        customer_id = scenes["lead"]["customer_id"]
        record_id = scenes["lead"]["todo_id"]
        simple = await write(client, headers, "POST", f"/api/records/{record_id}/activities", {
            "content": "合成试用：已电话沟通，客户正在确认系统清单。"}, 201)
        assert simple["activity"]["content"].startswith("合成试用")
        extra = await write(client, headers, "POST", f"/api/records/{record_id}/activities/organize", {
            "content": "我答应发送数据库加密产品资料，没有约定执行时间。", "request_id": "new-communication-trial"}, 201)
        assert extra["record"]["id"] != record_id and extra["visit_ref"] is not None
        source = await read(client, f"/api/records/{extra['record']['id']}")
        assert source["record"]["parent_record_id"] == record_id
        assert source["record"]["original_content"] == "我答应发送数据库加密产品资料，没有约定执行时间。"
        coaching_path = f"/api/customers/{customer_id}/coaching"
        await write(client, headers, "POST", coaching_path, {}, 202)
        async with asyncio.timeout(8):
            while True:
                advice = await read(client, coaching_path)
                if not advice["generating"] and advice["recommendation"]: break
                await asyncio.sleep(.02)
        recommendation = advice["recommendation"]
        adoption_path = coaching_path + f"/{recommendation['version']}/actions/1/adopt"
        action = (await write(client, headers, "POST", adoption_path, {}))["record"]
        assert action["customer_id"] == customer_id and action["task_id"] is None
        detail = await read(client, f"/api/records/{action['id']}")
        assert detail["proposal"] is None
        await write(client, headers, "POST", f"/api/customers/{customer_id}/coach-feedback", {
            "version": recommendation["version"], "index": 1, "status": "completed", "note": "合成试用：已核对试点系统"})
        completed = await read(client, f"/api/records/{action['id']}")
        assert completed["record"]["status"] == "done"
        bench = await read(client, f"/api/customers/{customer_id}/workbench")
        assert action["id"] not in {item["id"] for item in bench["open_actions"]}
        async with asyncio.timeout(8):
            while True:
                regenerated = await read(client, coaching_path)
                if not regenerated["generating"] and regenerated["recommendation"]: break
                await asyncio.sleep(.02)
        assert regenerated["recommendation"]["version"] > recommendation["version"]
        feedback = next(item for item in regenerated["feedback_history"] if item["version"] == recommendation["version"])
        assert feedback["title"] == recommendation["next_moves"][0]["title"]
        assert feedback["status"] == "completed" and feedback["note"] == "合成试用：已核对试点系统"
        assert feedback["updated_at"] == NOW
        other = await read(client, f"/api/customers/{scenes['projects']['customer_id']}/coaching")
        assert not any(item["note"] == feedback["note"] for item in other["feedback_history"])


@pytest.mark.parametrize("role", ["supplement", "recap"])
@pytest.mark.parametrize("source_time", [None, NOW])
@run_async
async def test_recap_and_later_supplement_keep_explicit_own_time_after_restart(tmp_path, role, source_time):
    async with trial(tmp_path) as (_, client, headers, scenes):
        meeting_time = NOW - 86400
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：昨日交流今日补充", "customer_id": scenes["lead"]["customer_id"],
            "occurred_at": meeting_time}, 201))["visit"]
        addition = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
            "role": role, "provider": "manual", "title": "合成独立时间来源",
            "text": "今天电话收到新的系统接口信息，后续再确认。",
            "occurred_at": source_time, "confirm_time_difference": True}, 202)
        material_id, visit_id = addition["material"]["id"], visit["id"]
        ready = await material_ready(client, material_id)
        current = await read(client, f"/api/visits/{visit_id}")
        assert current["visit"]["occurred_at"] == meeting_time
        assert ready["material"]["occurred_at"] == source_time
        source = next(item for item in current["sources"] if item["material_id"] == material_id)
        assert source["time_association_confirmed"] is True
        assert not any("发生时间" in reason for reason in source.get("review_reasons", []))
    # Rebuild all production services against the isolated synthetic database.
    # Persisted association must survive service restart and checking unchanged metadata.
    async with trial(tmp_path) as (_, client, headers, _):
        current = await read(client, f"/api/visits/{visit_id}")
        saved = await write(client, headers, "PATCH", f"/api/visits/{visit_id}", {
            "revision": current["visit"]["revision"], "title": current["visit"]["title"],
            "customer_id": current["visit"]["customer_id"], "occurred_at": current["visit"]["occurred_at"]})
        material = (await read(client, f"/api/materials/{material_id}"))["material"]
        assert saved["visit"]["occurred_at"] == meeting_time
        assert material["occurred_at"] == source_time


@run_async
async def test_project_no_change_edit_preserves_revision_and_real_change_rejects_old_page(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        customer_id = scenes["projects"]["customer_id"]
        project = (await read(client, f"/api/customers/{customer_id}/opportunities"))["items"][0]
        original_revision = project["revision"]
        path = f"/api/customers/{customer_id}/opportunities/{project['id']}"
        same = await write(client, headers, "PATCH", path, {
            "expected_revision": original_revision, "name": "  " + project["name"] + "  ",
            "amount_cents": project["amount_cents"], "amount_type": project["amount_type"],
            "approval": project["approval"], "stage": project["stage"], "contact_ids": project["contact_ids"]})
        assert same["opportunity"]["revision"] == original_revision
        actual = await write(client, headers, "PATCH", path, {
            "expected_revision": original_revision, "scope": "真实试用修改：只验证一个试点系统"})
        assert actual["opportunity"]["revision"] == original_revision + 1
        await write(client, headers, "PATCH", path, {
            "expected_revision": original_revision, "scope": "旧页面不应覆盖"}, 400)
        current = (await read(client, f"/api/customers/{customer_id}/opportunities"))["items"]
        assert next(item for item in current if item["id"] == project["id"])["scope"] == actual["opportunity"]["scope"]


@pytest.mark.parametrize("target", ["customer", "contact"])
@run_async
async def test_unchanged_customer_or_contact_save_keeps_advice_current(tmp_path, target):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        customer_id = scenes["lead"]["customer_id"]
        coaching_path = f"/api/customers/{customer_id}/coaching"
        await write(client, headers, "POST", coaching_path, {}, 202)
        async with asyncio.timeout(8):
            while True:
                before = await read(client, coaching_path)
                if not before["generating"] and before["recommendation"]: break
                await asyncio.sleep(.02)
        assert before["recommendation"]["stale"] is False
        # Hold the next refresh: inspect whether checking unchanged fields alone
        # disables the currently displayed advice, before a background retry masks it.
        class PausedCoach:
            async def advise(self, *args, **kwargs):
                await asyncio.Event().wait()
        controller.coaching.coach = PausedCoach()
        controller.clock = lambda: NOW + 60
        profile = await read(client, f"/api/customers/{customer_id}/profile")
        if target == "customer":
            customer = profile["customer"]
            await write(client, headers, "PATCH", f"/api/customers/{customer_id}", {
                key: customer[key] for key in ("name", "stage", "amount_cents", "notes", "aliases", "contact_cycle_days")})
        else:
            contact = profile["contacts"][0]
            await write(client, headers, "PATCH", f"/api/customers/{customer_id}/contacts/{contact['id']}", {
                key: contact[key] for key in ("name", "role", "phone")})
        after = await read(client, coaching_path)
        print({"unchanged_profile": {"target": target, "advice_stale_before": before["recommendation"]["stale"],
            "advice_stale_after": after["recommendation"]["stale"]}})
        assert after["recommendation"]["stale"] is False


@run_async
async def test_two_completion_requests_and_stale_page_do_not_make_duplicate_next_actions(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：完成后的接续请求", "kind": "action", "status": "following",
            "content": "准备一页概览", "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
        old = await read(client, f"/api/records/{record['id']}")
        await write(client, headers, "PATCH", f"/api/records/{record['id']}", {"content": "已核对：需提供技术版本概览"})
        body = {"request_id": "concurrent-finish-trial", "result": "已发概览", "next_step": "询问客户初步反馈",
            "remind_at": NOW + 86400, "duration_minutes": 30, "expected_snapshot": old["completion_snapshot"]}
        await write(client, headers, "POST", f"/api/records/{record['id']}/complete-outcome", body, 400)
        latest = await read(client, f"/api/records/{record['id']}")
        assert latest["record"]["status"] != "done"
        body["expected_snapshot"] = latest["completion_snapshot"]
        first, duplicate = await asyncio.gather(*[write(client, headers, "POST", f"/api/records/{record['id']}/complete-outcome", body) for _ in range(2)])
        assert first["outcome"]["id"] == duplicate["outcome"]["id"]
        assert first["next_record"]["id"] == duplicate["next_record"]["id"]
        assert first["proposal"]["status"] == "pending" and first["next_record"]["task_id"] is None
        await write(client, headers, "POST", f"/api/records/{record['id']}/complete-outcome", {**body, "result": "另一份内容不可覆盖"}, 400)
        assert (await read(client, f"/api/records/{record['id']}"))["record"]["content"] == latest["record"]["content"]


@run_async
async def test_wrong_customer_links_fact_evidence_and_private_records_are_rejected_without_reassignment(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        first, second = scenes["lead"]["customer_id"], scenes["projects"]["customer_id"]
        note = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：不能串客户的原话", "content": "客户明确需要数据库加密。",
            "kind": "note", "customer_id": first}, 201))["record"]
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "record", "entity_id": note["id"], "opportunity_id": scenes["projects"]["opportunity_ids"][0]}, 404)
        await write(client, headers, "POST", f"/api/customers/{second}/facts", {
            "key": "crypto_needs", "value": "数据库加密", "basis": "reported", "source_record_id": note["id"],
            "evidence": "客户明确需要数据库加密"}, 400)
        outside = controller.crm.create_customer("other-owner", {"name": "合成外部客户不应被读取"}, NOW)
        outside_record = controller.crm.create_record("other-owner", {"title": "合成私人事项",
            "content": "合成隐私内容", "customer_id": outside["id"]}, NOW)
        for route in (f"/api/records/{outside_record['id']}", f"/api/customers/{outside['id']}/profile",
                      f"/api/customers/{outside['id']}/workbench"):
            assert (await client.get(route)).status == 404
        current = await read(client, f"/api/records/{note['id']}")
        assert current["record"]["customer_id"] == first and current["record"].get("opportunity_id") is None


@run_async
async def test_long_material_and_past_unlinked_schedule_keep_full_source_and_complete_normally(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        text = "无动作的完整会议背景。" * 1500 + "\n我答应整理病历签名接口问题，没有约定执行时间。"
        created = await write(client, headers, "POST", "/api/materials", {
            "provider": "manual", "title": "合成用户试用：长录音全文", "category": "meeting", "text": text}, 202)
        material = await material_ready(client, created["material"]["id"])
        assert len(text) > 6000 and material["text"] == text
        assert material["original_version"]["text"] == text
        assert len(material["analysis"]["actions"]) == 1
        # Legacy imported schedule without a CRM source: complete the actual task,
        # never pretend there is a source record to open or invent a customer.
        controller.store.execute(OWNER, "synthetic-orphan-propose", {
            "action": "propose", "title": "合成旧导入无来源安排", "remind_at": NOW + 7200, "duration_minutes": 30}, NOW)
        proposal = controller.crm._db.execute("SELECT id FROM proposals WHERE owner=? AND title=?",
            (OWNER, "合成旧导入无来源安排")).fetchone()[0]
        controller.store.execute(OWNER, "synthetic-orphan-confirm", {"action": "confirm", "proposal_id": proposal}, NOW)
        detail = controller.crm.get_proposal(OWNER, proposal)
        task_id = detail["task_id"]
        overview = await read(client, "/api/overview")
        assert task_id in {item["id"] for item in overview["orphan_tasks"]}
        agenda = await read(client, "/api/agenda?period=day&date=2026-10-03")
        item = next(item for item in agenda["items"] if item["id"] == task_id)
        assert item["record_id"] is None
        await write(client, headers, "POST", f"/api/tasks/{task_id}/complete", {})
        after = await read(client, "/api/overview")
        assert task_id not in {item["id"] for item in after["orphan_tasks"]}


@run_async
async def test_logout_wrong_password_and_csrf_do_not_change_saved_sources(tmp_path):
    async with trial(tmp_path) as (_, client, headers, _):
        created = await write(client, headers, "POST", "/api/captures", {
            "text": "合成用户试用：退出之前已保存", "request_id": "logout-source"}, 202)
        await capture_ready(client, created["capture"]["id"])
        assert (await client.post("/api/captures", json={"text": "缺少CSRF", "request_id": "no-token"})).status == 403
        await write(client, headers, "POST", "/api/logout", {})
        assert (await client.get(f"/api/records/{created['capture']['record_id']}")).status == 401
        assert (await client.post("/api/login", json={"password": "incorrect-synthetic-password"})).status == 401
        login = await client.post("/api/login", json={"password": PASSWORD})
        assert login.status == 200
        current = await read(client, f"/api/records/{created['capture']['record_id']}")
        assert current["record"]["original_content"] == "合成用户试用：退出之前已保存"


@run_async
async def test_existing_material_can_join_one_exchange_and_cannot_be_reused_for_another_customer(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        customer = scenes["projects"]["customer_id"]
        material = (await write(client, headers, "POST", "/api/materials", {
            "provider": "manual", "title": "合成用户试用：先材料后交流", "customer_id": customer,
            "text": "我答应整理病历签名接口问题，没有约定执行时间。"}, 202))["material"]
        await material_ready(client, material["id"])
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：归入一次交流", "customer_id": customer}, 201))["visit"]
        await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
            "material_id": material["id"], "role": "recording", "request_key": "attach-existing"}, 202)
        await material_ready(client, material["id"])
        detail = await read(client, f"/api/materials/{material['id']}")
        assert detail["visit_ref"]["id"] == visit["id"]
        current = await read(client, f"/api/visits/{visit['id']}")
        assert current["visit"]["source_count"] == 1 and len(current["actions"]) == 1
        other = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：另一个客户交流", "customer_id": scenes["lead"]["customer_id"]}, 201))["visit"]
        await write(client, headers, "POST", f"/api/visits/{other['id']}/materials", {
            "material_id": material["id"], "role": "recording"}, 400)
        assert (await read(client, f"/api/visits/{other['id']}"))["visit"]["source_count"] == 0
        assert (await read(client, f"/api/materials/{material['id']}"))["visit_ref"]["id"] == visit["id"]


@run_async
async def test_conflicting_recording_recap_times_require_correction_before_one_action_schedule(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        visit = (await write(client, headers, "POST", "/api/visits", {
            "title": "合成用户试用：两份来源预约冲突", "customer_id": scenes["projects"]["customer_id"],
            "occurred_at": NOW}, 201))["visit"]
        material_ids = []
        for role, hour in (("recording", 15), ("recap", 16)):
            source = await write(client, headers, "POST", f"/api/visits/{visit['id']}/materials", {
                "role": role, "provider": "manual", "title": f"合成{role}预约",
                "text": f"我答应演示脱敏网关，执行时间2026-10-05 {hour}:00，预计45分钟。"}, 202)
            material_ids.append(source["material"]["id"])
            await material_ready(client, source["material"]["id"])
        detail = await read(client, f"/api/visits/{visit['id']}")
        assert len(detail["actions"]) == 1
        action = detail["actions"][0]
        assert action["needs_review"] and "时间不一致" in "".join(action["review_reasons"])
        await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
            {"revision": detail["visit"]["revision"]}, 400)
        wrong = await read(client, f"/api/materials/{material_ids[1]}")
        await write(client, headers, "PATCH", f"/api/materials/{material_ids[1]}", {
            "revision": wrong["material"]["revision"],
            "text": "我答应演示脱敏网关，执行时间2026-10-05 15:00，预计45分钟。"})
        corrected = await material_ready(client, material_ids[1])
        assert len(corrected["versions"]) == 2
        current = await read(client, f"/api/visits/{visit['id']}")
        assert len(current["actions"]) == 1 and current["actions"][0]["needs_review"] is False
        action = current["actions"][0]
        adoption = await write(client, headers, "POST", f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
            {"revision": current["visit"]["revision"]})
        assert adoption["proposal"]["status"] == "pending" and adoption["record"]["task_id"] is None
        confirmed = await confirm_schedule(client, headers, adoption["record"]["id"])
        expected = datetime(2026, 10, 5, 15, tzinfo=SHANGHAI).timestamp()
        assert confirmed["task"]["remind_at"] == expected and confirmed["task"]["duration_minutes"] == 45


@run_async
async def test_record_correction_retains_raw_source_and_active_reminder_until_separate_change_confirmation(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record_id = scenes["changes"]["record_ids"][0]
        original = await read(client, f"/api/records/{record_id}")
        corrected = await write(client, headers, "PATCH", f"/api/records/{record_id}", {
            "content": "合成用户试用：识别校正为真正的数据脱敏 POC 演示", "title": "合成修正后的演示主题",
            "mode": "note_only"})
        assert corrected["record"]["content"].startswith("合成用户试用")
        assert corrected["record"]["original_content"] == original["record"]["original_content"]
        assert corrected["active_reminder"]["remind_at"] == original["task"]["remind_at"]
        assert corrected["active_reminder"]["title"] == original["task"]["title"]
        assert "原时间" in corrected["warning"]
        # A new concrete arrangement proposes a change; accepted reminder is still the old one.
        revised = await write(client, headers, "PATCH", f"/api/records/{record_id}", {
            "title": "合成修正后的演示主题", "mode": "sync_reminder", "remind_at": NOW + 5 * 86400,
            "duration_minutes": 45})
        assert revised["reminder_change"]["status"] == "pending"
        assert (await read(client, f"/api/records/{record_id}"))["task"]["remind_at"] == original["task"]["remind_at"]
        changed = await confirm_schedule(client, headers, record_id)
        assert changed["task"]["title"] == "合成修正后的演示主题" and changed["task"]["remind_at"] == NOW + 5 * 86400


@run_async
async def test_date_only_deadline_today_vs_overdue_and_completed_nodes_are_consistent(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        records = {}
        for day in ("2026-10-02", "2026-10-03", "2026-10-04"):
            record = (await write(client, headers, "POST", "/api/records", {
                "title": "合成用户试用：日期节点" + day, "kind": "action", "status": "following",
                "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
            records[day] = record["id"]
            await write(client, headers, "PATCH", f"/api/records/{record['id']}/terms", {
                "executor_kind": "self", "deadline_date": day, "expected_updated_at": None})
        overview = await read(client, "/api/overview")
        overdue = {item["record_id"] for item in overview["overdue"]}
        assert records["2026-10-02"] in overdue
        assert records["2026-10-03"] not in overdue and records["2026-10-04"] not in overdue
        agenda = await read(client, "/api/agenda?period=day&date=2026-10-03")
        assert records["2026-10-03"] in {item["record_id"] for item in agenda["planning_nodes"]}
        assert not any(item.get("record_id") in records.values() for item in agenda["items"])
        completion = await read(client, f"/api/records/{records['2026-10-03']}")
        snapshot = completion["completion_snapshot"]
        assert isinstance(snapshot, str) and len(snapshot) == 64 and all(char in "0123456789abcdef" for char in snapshot)
        await write(client, headers, "POST", f"/api/records/{records['2026-10-03']}/complete-outcome", {
            "request_id": "complete-date-node", "result": "今天已发送，不需下一步", "expected_snapshot": snapshot})
        after = await read(client, "/api/agenda?period=day&date=2026-10-03")
        assert records["2026-10-03"] not in {item["record_id"] for item in after["planning_nodes"]}


@run_async
async def test_old_record_edit_cannot_overwrite_new_content_or_change_accepted_schedule(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：跨页面修改记录", "content": "最初现场原话", "kind": "action",
            "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
        calendar_seen = await read(client, f"/api/records/{record['id']}")
        assert re.fullmatch(r'[0-9a-f]{64}', calendar_seen['schedule_snapshot'])
        await write(client, headers, "POST", f"/api/records/{record['id']}/schedule", {
            "remind_at": NOW + 3600, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
        await confirm_schedule(client, headers, record["id"])
        original = await read(client, f"/api/records/{record['id']}")
        version = original["record"]["updated_at"]
        newer = await write(client, headers, "PATCH", f"/api/records/{record['id']}", {
            "content": "新页面已记下客户确认的负责人", "expected_updated_at": version})
        assert newer["record"]["updated_at"] > version
        conflict = await write(client, headers, "PATCH", f"/api/records/{record['id']}", {
            "content": "旧页面仅知道负责人待定", "title": "旧页面改动", "expected_updated_at": version,
            "mode": "sync_reminder", "remind_at": NOW + 7200, "duration_minutes": 45}, 409)
        assert "没有覆盖" in conflict["error"]
        current = await read(client, f"/api/records/{record['id']}")
        assert current["record"]["content"] == newer["record"]["content"]
        assert current["record"]["original_content"] == record["original_content"]
        assert current["record"]["title"] == original["record"]["title"]
        assert current["task"] == original["task"]
        assert current["proposal"]["status"] == "confirmed"
        # A checked, unchanged form retains its revision and original analysis.
        same = await write(client, headers, "PATCH", f"/api/records/{record['id']}", {
            "expected_updated_at": newer["record"]["updated_at"], "content": newer["record"]["content"],
            "title": newer["record"]["title"]})
        assert same["record"]["updated_at"] == newer["record"]["updated_at"]
        # Existing integrations without this optional field retain compatibility.
        legacy = await write(client, headers, "PATCH", f"/api/records/{record['id']}", {"content": "兼容原接口"})
        assert legacy["record"]["content"] == "兼容原接口"


@run_async
async def test_two_record_editors_at_same_clock_save_once_with_http_conflict(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：同秒两个编辑页面", "kind": "note",
            "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
        async def edit(content):
            response = await client.patch(f"/api/records/{record['id']}", json={
                "content": content, "expected_updated_at": record["updated_at"]}, headers=headers)
            return response.status, await response.json()
        attempts = await asyncio.gather(edit("A页已确认销售节点"), edit("B页仍在补充思路"))
        assert sorted(status for status, _ in attempts) == [200, 409]
        saved = next(body["record"] for status, body in attempts if status == 200)
        current = (await read(client, f"/api/records/{record['id']}"))["record"]
        assert current["content"] == saved["content"] and current["updated_at"] > record["updated_at"]


@run_async
async def test_old_record_reinterpret_rejects_before_new_analysis_draft_or_reminder(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：旧页面不能覆盖重整", "content": "我答应发送数据库加密产品资料，没有约定执行时间。",
            "kind": "note", "customer_id": scenes["lead"]["customer_id"]}, 201))["record"]
        before = await read(client, f"/api/records/{record['id']}")
        version = before["record"]["updated_at"]
        newer = await write(client, headers, "PATCH", f"/api/records/{record['id']}", {
            "content": "另一页已保存：后续改为讨论兼容性证据", "expected_updated_at": version})
        class ForbiddenCustomerService:
            async def handle(self, *args, **kwargs):
                raise AssertionError("Conflicting old page must stop before model processing")
        real_service = controller.customer_service
        controller.customer_service = ForbiddenCustomerService()
        conflict = await write(client, headers, "POST", f"/api/records/{record['id']}/reinterpret", {
            "content": record["content"], "expected_updated_at": version}, 409)
        assert "没有覆盖" in conflict["error"]
        current = await read(client, f"/api/records/{record['id']}")
        assert current["record"]["content"] == newer["record"]["content"]
        assert current["analysis"] == before["analysis"] and current["proposal"] == before["proposal"]
        controller.customer_service = real_service
        valid = await write(client, headers, "POST", f"/api/records/{record['id']}/reinterpret", {
            "content": record["content"], "customer_id": record["customer_id"],
            "expected_updated_at": current["record"]["updated_at"]})
        assert valid.get("record_id") == record["id"]


@pytest.mark.parametrize("guard", [True, "not-a-timestamp"])
@run_async
async def test_invalid_record_edit_guard_cannot_write(tmp_path, guard):
    async with trial(tmp_path) as (_, client, headers, scenes):
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：无效版本不能覆盖", "content": "有效现有记录", "kind": "note"}, 201))["record"]
        await write(client, headers, "PATCH", f"/api/records/{record['id']}", {
            "content": "不应被保存", "expected_updated_at": guard}, 400)
        assert (await read(client, f"/api/records/{record['id']}"))["record"]["content"] == record["content"]


@run_async
async def test_only_complete_appointment_keeps_follow_up_in_work_customer_priorities_and_planning(tmp_path):
    async with trial(tmp_path) as (controller, client, headers, scenes):
        project = scenes["projects"]
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：拜访日程完成，方案仍待客户回复", "kind": "action", "status": "following",
            "customer_id": project["customer_id"]}, 201))["record"]
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "record", "entity_id": record["id"], "opportunity_id": project["opportunity_ids"][0]})
        await write(client, headers, "PATCH", f"/api/records/{record['id']}/terms", {
            "executor_kind": "self", "check_date": "2026-10-03", "deadline_date": "2026-10-03"})
        calendar_seen = await read(client, f"/api/records/{record['id']}")
        assert re.fullmatch(r'[0-9a-f]{64}', calendar_seen['schedule_snapshot'])
        await write(client, headers, "POST", f"/api/records/{record['id']}/schedule", {
            "remind_at": NOW + 3600, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
        confirmed = await confirm_schedule(client, headers, record["id"])
        before = await read(client, "/api/overview")
        old_progress = next(item for item in before["projects"] if item["id"] == project["opportunity_ids"][0])["progress"]
        await write(client, headers, "POST", f"/api/tasks/{confirmed['task']['id']}/complete", {})
        current = await read(client, f"/api/records/{record['id']}")
        assert current["task"]["status"] == "completed" and current["record"]["status"] == "following"
        overview = await read(client, "/api/overview")
        assert record["id"] in {item["record_id"] for item in overview["my_actions"]}
        new_progress = next(item for item in overview["projects"] if item["id"] == project["opportunity_ids"][0])["progress"]
        assert new_progress == old_progress
        bench = await read(client, f"/api/customers/{project['customer_id']}/workbench")
        assert record["id"] in {item["id"] for item in bench["open_actions"]}
        assert not any(item["record_id"] == record["id"] for item in bench["outcomes"])
        priorities = await read(client, "/api/priorities")
        assert any(item.get("record_id") == record["id"] for item in priorities["items"])
        agenda = await read(client, "/api/agenda?period=day&date=2026-10-03")
        assert {item["kind"] for item in agenda["planning_nodes"] if item["record_id"] == record["id"]} == {"check", "deadline"}
        assert next(item for item in agenda["items"] if item["id"] == confirmed["task"]["id"])["status"] == "completed"
        # The actual model request must still know this follow-up is unfinished.
        contexts = []
        class ContextProbe(OfflineDiscussion):
            async def reply(self, context, history, text, now):
                contexts.append(context)
                return await super().reply(context, history, text, now)
        controller.discussions.advisor = ContextProbe()
        thread = (await write(client, headers, "POST", "/api/sales-discussions", {
            "customer_id": project["customer_id"], "opportunity_id": project["opportunity_ids"][0],
            "title": "合成用户试用：拜访后方案没落实如何推进"}, 201))["thread"]
        await write(client, headers, "POST", f"/api/sales-discussions/{thread['id']}/messages", {
            "text": "拜访日程结束了，这项方案还需怎样落实？", "request_id": "discuss-after-time-slot"})
        assert any(item["title"] == record["title"] and item["task_status"] == "completed"
                   for item in contexts[-1]["open_actions"])
        completion = await read(client, f"/api/records/{record['id']}")
        snapshot = completion["completion_snapshot"]
        assert isinstance(snapshot, str) and len(snapshot) == 64 and all(char in "0123456789abcdef" for char in snapshot)
        await write(client, headers, "POST", f"/api/records/{record['id']}/complete-outcome", {
            "request_id": "completed-follow-up-after-calendar", "result": "客户已确认方案，本项落实完毕", "expected_snapshot": snapshot})
        finished = await read(client, "/api/overview")
        assert record["id"] not in {item["record_id"] for item in finished["my_actions"]}
        final_progress = next(item for item in finished["projects"] if item["id"] == project["opportunity_ids"][0])["progress"]
        assert final_progress["done"] == old_progress["done"] + 1
        final_bench = await read(client, f"/api/customers/{project['customer_id']}/workbench")
        assert record["id"] not in {item["id"] for item in final_bench["open_actions"]}
        assert not any(item.get("record_id") == record["id"] for item in (await read(client, "/api/priorities"))["items"])
        assert record["id"] not in {item["record_id"] for item in (await read(client, "/api/agenda?period=day&date=2026-10-03"))["planning_nodes"]}
        await write(client, headers, "POST", f"/api/sales-discussions/{thread['id']}/messages", {
            "text": "方案已经落实，哪些后续事项还需关注？", "request_id": "discuss-after-follow-up-done"})
        assert not any(item["title"] == record["title"] for item in contexts[-1]["open_actions"])


@run_async
async def test_project_archive_restore_preserves_source_action_and_separate_amounts(tmp_path):
    async with trial(tmp_path) as (_, client, headers, scenes):
        scene = scenes["projects"]
        customer_id = scene["customer_id"]
        projects = (await read(client, f"/api/customers/{customer_id}/opportunities"))["items"]
        project = projects[0]
        record = (await write(client, headers, "POST", "/api/records", {
            "title": "合成用户试用：历史项目需要保留线索", "content": "继续保存已交流的证据", "kind": "action",
            "customer_id": customer_id}, 201))["record"]
        await write(client, headers, "POST", "/api/opportunity-links", {
            "entity_type": "record", "entity_id": record["id"], "opportunity_id": project["id"]})
        path = f"/api/customers/{customer_id}/opportunities/{project['id']}"
        archived = (await write(client, headers, "PATCH", path, {
            "expected_revision": project["revision"], "archived": True}))["opportunity"]
        active = (await read(client, f"/api/customers/{customer_id}/opportunities"))["items"]
        assert project["id"] not in {item["id"] for item in active}
        all_projects = (await read(client, f"/api/customers/{customer_id}/opportunities?include_archived=true"))["items"]
        assert next(item for item in all_projects if item["id"] == project["id"])["archived"] is True
        linked = (await read(client, f"/api/records/{record['id']}"))["record"]
        assert linked["customer_id"] == customer_id and linked["status"] != "done"
        assert linked["content"] == record["content"]
        restored = (await write(client, headers, "PATCH", path, {
            "expected_revision": archived["revision"], "archived": False}))["opportunity"]
        assert restored["amount_cents"] == project["amount_cents"] and restored["archived"] is False
        final = (await read(client, f"/api/customers/{customer_id}/opportunities"))["items"]
        assert len(final) == len(projects)
        assert {item["id"]: item["amount_cents"] for item in final} == {item["id"]: item["amount_cents"] for item in projects}


def test_record_edit_guard_is_atomic_across_two_store_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from secretary.crm import RecordConflict
    from secretary.customer_store import CustomerStore
    path = tmp_path / "record-two-writers.sqlite3"
    first, second = CustomerStore(path), CustomerStore(path)
    try:
        record = first.create_record(OWNER, {"title": "合成用户试用：两进程编辑", "content": "原始现场信息"}, NOW)
        def edit(args):
            store, content = args
            try:
                return {"status": "saved", "record": store.update_record(OWNER, record['id'], {
                    "content": content}, NOW, expected_updated_at=record['updated_at'])}
            except RecordConflict:
                return {"status": "conflict"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, [(first, "第一进程的新内容"), (second, "第二进程的新内容")]))
        assert sorted(item["status"] for item in results) == ["conflict", "saved"]
        winner = next(item["record"] for item in results if item["status"] == "saved")
        assert first.get_record(OWNER, record['id'])["content"] == winner['content']
        assert second.get_record(OWNER, record['id'])["updated_at"] > record['updated_at']
    finally:
        first.close()
        second.close()
