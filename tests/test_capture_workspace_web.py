"""Actual HTTP journeys on independent synthetic training databases only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from functools import wraps
import json

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import httpx
import pytest

from deploy.training import (NOTICE, OWNER, PASSWORD, OfflineResolution, build_training_app, training_config)
from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI).timestamp()


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture(autouse=True)
def no_provider_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP journey must never construct an external provider client")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)


@asynccontextmanager
async def training_client(tmp_path, *, authenticated=True):
    app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
    controller = next(route.handler.__self__ for route in app.router.routes()
                      if getattr(route.handler, "__name__", "") == "dashboard")
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    headers = {}
    try:
        if authenticated:
            login = await client.post("/api/login", json={"password": PASSWORD})
            assert login.status == 200
            headers = {"X-CSRF-Token": (await login.json())["csrf"]}
        yield controller, client, headers
    finally:
        await client.close()


async def data(response, status=200):
    result = await response.json()
    assert response.status == status, result
    return result


async def wait_capture(client, identifier, *states):
    async with asyncio.timeout(8):
        while True:
            item = (await data(await client.get(f"/api/captures/{identifier}")))["capture"]
            if item["status"] in states:
                return item
            await asyncio.sleep(.02)


async def wait_material(client, identifier, *states):
    async with asyncio.timeout(8):
        while True:
            item = await data(await client.get(f"/api/materials/{identifier}"))
            if item["material"]["status"] in states:
                return item
            await asyncio.sleep(.02)


def counts(crm):
    return {table: crm._db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("tasks", "proposals", "notifications")}


async def manifest(client):
    result = await data(await client.get("/api/training"))
    return {item["key"]: item for item in result["scenarios"]}


@run_async
async def test_new_http_apis_enforce_login_and_csrf_before_mutation(tmp_path):
    async with training_client(tmp_path, authenticated=False) as (controller, client, _):
        for route in ("/api/captures", "/api/captures/1", "/api/overview", "/api/sales-discussions", "/api/sales-discussions/1"):
            assert (await client.get(route)).status == 401
        assert (await client.post("/api/captures", json={"text": "未登录的原话", "request_id": "unauth"})).status == 401
        login = await data(await client.post("/api/login", json={"password": PASSWORD}))
        headers = {"X-CSRF-Token": login["csrf"]}
        before = controller.crm.list_records(OWNER)["total"]
        scene = await manifest(client)
        thread_payload = {"customer_id": scene["projects"]["customer_id"]}
        for route, body in (("/api/captures", {"text": "没有CSRF的原话", "request_id": "no-csrf"}),
                            ("/api/captures/1/classify", {"purpose": "action"}),
                            ("/api/captures/1/retry", {}),
                            ("/api/sales-discussions", thread_payload),
                            ("/api/sales-discussions/1/messages", {"text": "问题", "request_id": "msg"}),
                            ("/api/sales-discussions/1/messages/1/actions/1/adopt", {})):
            assert (await client.post(route, json=body)).status == 403
        assert controller.crm.list_records(OWNER)["total"] == before
        assert (await client.get("/api/overview")).status == 200
        bad_origin = {**headers, "Origin": "https://untrusted.invalid"}
        assert (await client.post("/api/captures", json={"text": "原话", "request_id": "origin"}, headers=bad_origin)).status == 403


@run_async
async def test_save_first_fuzzy_capture_confirm_and_personal_project_overview(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        before = counts(controller.crm)
        scenes = await manifest(client)
        projects = scenes["projects"]
        text = OfflineResolution.phrase
        created = await data(await client.post("/api/captures", json={"text": text, "request_id": "fuzzy-1"}, headers=headers), 202)
        item = created["capture"]
        assert item["status"] == "queued"
        assert item["record"]["original_content"] == text
        assert item["record"]["customer_id"] is None
        record_id, identifier = item["record_id"], item["id"]
        # The response already names a durable real CRM record, before inference.
        saved = await data(await client.get(f"/api/records/{record_id}"))
        assert saved["capture"]["id"] == identifier
        assert saved["record"]["content"] == text
        ready = await wait_capture(client, identifier, "review")
        assert ready["resolution"]["method"] == "model"
        assert ready["resolution"]["requires_confirmation"] is True
        assert ready["resolution"]["selected_customer_id"] is None
        assert ready["record"]["customer_id"] is None
        candidate = ready["resolution"]["items"][0]
        assert candidate["customer_id"] == projects["customer_id"]
        assert candidate["opportunity_id"] == projects["opportunity_ids"][1]
        assert "签名" in candidate["opportunity_name"]
        assert ready["analysis"]["actions"][0]["title"] == "整理病历签名接口问题"
        confirmed = await data(await client.post(f"/api/captures/{identifier}/classify", json={
            "purpose": "action", "customer_id": candidate["customer_id"], "opportunity_id": candidate["opportunity_id"],
            "expected_updated_at": ready["record"]["updated_at"]}, headers=headers))
        record = confirmed["record"]
        assert record["customer_id"] == projects["customer_id"]
        assert record["updated_at"] > ready["record"]["updated_at"]
        assert record["kind"] == "note" and record["proposal_id"] is None
        assert record["original_content"] == text
        overview = await data(await client.get("/api/overview"))
        assert not any(value["record_id"] == record_id for value in overview["my_actions"])
        project = next(value for value in overview["projects"] if value["id"] == candidate["opportunity_id"])
        assert project["amount_cents"] == 46000000 and project["amount_type"] == "budget" and project["approval"] == "approved"
        assert overview["timezone"] == "Asia/Shanghai" and overview["date"] == "2026-10-03"
        assert counts(controller.crm) == before
        detail = await data(await client.get(f"/api/records/{record_id}"))
        assert detail["capture"]["purpose"] == "action" and detail["capture"]["status"] in ("queued", "processing", "filed")
        refreshed = await wait_capture(client, identifier, "filed")
        assert refreshed["analysis"]["stale"] is False
        adopted = (await data(await client.post(f"/api/records/{record_id}/actions/{refreshed['analysis']['actions'][0]['id']}/adopt", json={}, headers=headers)))["record"]
        assert adopted["kind"] == "action" and adopted["parent_record_id"] == record_id
        assert adopted["title"] == "整理病历签名接口问题" and adopted["original_content"] != text
        after = await data(await client.get("/api/overview"))
        mine = next(value for value in after["my_actions"] if value["record_id"] == adopted["id"])
        assert mine["opportunity_id"] == candidate["opportunity_id"] and mine["needs_time"] is True
        assert not any(value["record_id"] == record_id for value in after["my_actions"])
        assert (await data(await client.get(f"/api/records/{record_id}")))["record"]["original_content"] == text


@run_async
async def test_manual_use_when_model_unconfigured_and_schedule_remains_unconfirmed(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        controller.captures.organizer = None
        before = counts(controller.crm)
        created = await data(await client.post("/api/captures", json={"text": "还有一个现场想法，暂时不知道属于谁", "request_id": "manual-1"}, headers=headers), 202)
        ready = await wait_capture(client, created["capture"]["id"], "needs_details")
        assert ready["analysis"] is None and "原话已保存" in ready["error"]
        expected = ready["record"]["updated_at"]
        confirmed = await data(await client.post(f"/api/captures/{ready['id']}/classify", json={"purpose": "schedule", "expected_updated_at": expected}, headers=headers))
        assert confirmed["record"]["customer_id"] is None
        assert confirmed["record"]["kind"] == "note" and confirmed["record"]["task_id"] is None
        assert confirmed["capture"]["purpose"] == "schedule"
        overview = await data(await client.get("/api/overview"))
        assert not any(item["record_id"] == ready["record_id"] for item in overview["my_actions"])
        assert counts(controller.crm) == before


@run_async
async def test_capture_http_idempotence_stale_revision_and_cross_customer_project_rollback(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        body = {"text": "等下复盘项目接口的事情", "request_id": "duplicate-capture"}
        first = (await data(await client.post("/api/captures", json=body, headers=headers), 202))["capture"]
        duplicate = (await data(await client.post("/api/captures", json=body, headers=headers), 202))["capture"]
        assert first["id"] == duplicate["id"] and first["record_id"] == duplicate["record_id"]
        assert (await client.post("/api/captures", json={**body, "text": "不一样的原话"}, headers=headers)).status == 400
        missing = await client.post(f"/api/captures/{first['id']}/classify", json={"purpose": "note"}, headers=headers)
        assert missing.status == 400
        cross = await client.post(f"/api/captures/{first['id']}/classify", json={"purpose": "project_reference", "customer_id": scenes["lead"]["customer_id"],
            "opportunity_id": scenes["projects"]["opportunity_ids"][0], "expected_updated_at": first["record"]["updated_at"]}, headers=headers)
        assert cross.status == 404
        current = (await data(await client.get(f"/api/captures/{first['id']}")))["capture"]
        assert current["record"]["customer_id"] is None and current["purpose"] is None
        good = await data(await client.post(f"/api/captures/{first['id']}/classify", json={"purpose": "note", "expected_updated_at": first["record"]["updated_at"]}, headers=headers))
        old = await client.post(f"/api/captures/{first['id']}/classify", json={"purpose": "action", "expected_updated_at": first["record"]["updated_at"]}, headers=headers)
        assert old.status == 400
        assert (await data(await client.get(f"/api/records/{first['record_id']}")))["record"]["kind"] == "note"
        assert good["record"]["updated_at"] > first["record"]["updated_at"]


@run_async
async def test_two_turn_http_discussion_with_explicit_source_and_adoption_only_creates_todo(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        projects = scenes["projects"]
        before = counts(controller.crm)
        capture = (await data(await client.post("/api/captures", json={"text": "刚才想到测试接口还需要核对，怎么推进？", "request_id": "discussion-source"}, headers=headers), 202))["capture"]
        calls = []
        original = controller.discussions.advisor.reply
        async def measured(context, history, text, now):
            calls.append({"context": context, "history": history, "text": text})
            return await original(context, history, text, now)
        controller.discussions.advisor.reply = measured
        made = await data(await client.post("/api/sales-discussions", json={"customer_id": projects["customer_id"], "opportunity_id": projects["opportunity_ids"][0],
            "source_record_id": capture["record_id"], "title": "如何推进接口验证", "request_id": "make-discussion"}, headers=headers), 201)
        thread_id = made["thread"]["id"]
        assert made["thread"]["source_record_id"] == capture["record_id"]
        first = await data(await client.post(f"/api/sales-discussions/{thread_id}/messages", json={"text": "先做验证还是先找采购？", "request_id": "turn-1"}, headers=headers))
        repeated = await data(await client.post(f"/api/sales-discussions/{thread_id}/messages", json={"text": "先做验证还是先找采购？", "request_id": "turn-1"}, headers=headers))
        assert len(calls) == 1 and first["messages"] == repeated["messages"]
        assert NOTICE in first["messages"][1]["text"]
        second = await data(await client.post(f"/api/sales-discussions/{thread_id}/messages", json={"text": "那对接人还不明确，应该怎么问？", "request_id": "turn-2"}, headers=headers))
        assert len(second["messages"]) == 4 and len(calls) == 2 and len(calls[1]["history"]) == 2
        assert second["messages"][1]["stale"] is True
        assistant = second["messages"][-1]
        assert assistant["stale"] is False
        assert capture["record_id"] in [source["record_id"] for source in assistant["sources"]]
        adopt_url = f"/api/sales-discussions/{thread_id}/messages/{assistant['id']}/actions/1/adopt"
        adopted = (await data(await client.post(adopt_url, json={}, headers=headers)))["record"]
        again = (await data(await client.post(adopt_url, json={}, headers=headers)))["record"]
        assert adopted["id"] == again["id"] and adopted["kind"] == "action" and adopted["status"] == "following"
        assert adopted["customer_id"] == projects["customer_id"] and adopted["task_id"] is None and adopted["proposal_id"] is None
        assert counts(controller.crm) == before
        assert controller.crm.get_record(OWNER, capture["record_id"])["customer_id"] is None
        overview = await data(await client.get("/api/overview"))
        assert any(record["record_id"] == adopted["id"] and record["opportunity_id"] == projects["opportunity_ids"][0] for record in overview["my_actions"])
        listed = await data(await client.get("/api/sales-discussions", params={"opportunity_id": projects["opportunity_ids"][0]}))
        assert listed["items"][0]["id"] == thread_id
        reopened = await data(await client.get(f"/api/sales-discussions/{thread_id}"))
        assert reopened["messages"][-1]["data"]["next_moves"][0]["adopted_record_id"] == adopted["id"]
        # Projects cannot be combined with another customer's discussion.
        assert (await client.post("/api/sales-discussions", json={"customer_id": scenes["lead"]["customer_id"], "opportunity_id": projects["opportunity_ids"][0]}, headers=headers)).status == 404


@run_async
async def test_source_identity_owner_scope_and_unconfigured_discussion_keep_original(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        alien = controller.crm.create_record("another-owner", {"title": "私有原话", "content": "不能泄漏的测试信息"}, NOW)
        assert (await client.get(f"/api/records/{alien['id']}")).status == 404
        source_payload = {"customer_id": scenes["projects"]["customer_id"], "source_record_id": alien["id"]}
        assert (await client.post("/api/sales-discussions", json=source_payload, headers=headers)).status == 404
        thread = await data(await client.post("/api/sales-discussions", json={"customer_id": scenes["lead"]["customer_id"]}, headers=headers), 201)
        controller.discussions.advisor = None
        response = await data(await client.post(f"/api/sales-discussions/{thread['thread']['id']}/messages", json={"request_id": "unconfigured", "text": "没有模型时也请保留这个想法"}, headers=headers))
        assert response["configured"] is False and response["messages"][0]["status"] == "failed"
        assert response["messages"][0]["text"] == "没有模型时也请保留这个想法"
        assert "配置" in response["messages"][0]["error"]


class Attribution:
    def __init__(self, customer_id, project_id=None):
        self.customer_id, self.project_id = customer_id, project_id
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
    async def resolve(self, owner, text, context_customer_id=None):
        self.calls.append((owner, text, context_customer_id))
        self.started.set()
        if self.release:
            await self.release.wait()
        return {"items": [{"customer_id": self.customer_id, "customer_name": "虚构客户", "opportunity_id": self.project_id,
                          "opportunity_name": "虚构项目", "confidence": "medium", "reasons": ["测试口语线索，尚待确认"]}],
                "status": "single", "requires_confirmation": True, "selected_customer_id": None, "method": "model", "question": "是否属于这个客户？", "warning": None}


@run_async
async def test_http_human_edit_discards_running_models_old_attribution_and_analysis(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        resolver = Attribution(scenes["projects"]["customer_id"], scenes["projects"]["opportunity_ids"][1])
        resolver.release = asyncio.Event()
        controller.captures.resolution = resolver
        original = "原口述：昨天医院陈工说签名接口需要核对。"
        capture = (await data(await client.post("/api/captures", json={"text": original, "request_id": "edit-during-inference"}, headers=headers), 202))["capture"]
        async with asyncio.timeout(8):
            await resolver.started.wait()
        try:
            assert (await client.post(f"/api/captures/{capture['id']}/retry", json={}, headers=headers)).status == 400
            corrected = "修正：这句话是我个人想到的密钥管理问题，暂时不关联客户。"
            patch = await data(await client.patch(f"/api/records/{capture['record_id']}", json={"content": corrected}, headers=headers))
            assert patch["record"]["content"] == corrected
        finally:
            resolver.release.set()
        ready = await wait_capture(client, capture["id"], "needs_details")
        assert ready["record"]["content"] == corrected
        assert ready["record"]["original_content"] == original
        assert ready["resolution"] is None and ready["analysis"] is None
        assert "更新" in ready["error"]


@run_async
async def test_http_human_classification_wins_over_running_worker(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        project = scenes["projects"]
        resolver = Attribution(project["customer_id"], project["opportunity_ids"][0])
        resolver.release = asyncio.Event()
        controller.captures.resolution = resolver
        capture = (await data(await client.post("/api/captures", json={"text": "想到一个试点系统范围的问题", "request_id": "classification-wins"}, headers=headers), 202))["capture"]
        async with asyncio.timeout(8):
            await resolver.started.wait()
        try:
            classified = await data(await client.post(f"/api/captures/{capture['id']}/classify", json={
                "purpose": "project_reference", "customer_id": project["customer_id"], "opportunity_id": project["opportunity_ids"][0],
                "expected_updated_at": capture["record"]["updated_at"]}, headers=headers))
        finally:
            resolver.release.set()
        # Let the old call finish, then check that its lease cannot publish over
        # the user's explicit classification into project evidence.
        await asyncio.sleep(.1)
        item = (await data(await client.get(f"/api/captures/{capture['id']}")))["capture"]
        assert item["status"] == "filed" and item["purpose"] == "project_reference"
        assert item["record"]["kind"] == "note" and item["record"]["customer_id"] == project["customer_id"]
        assert item["analysis"] is None and item["resolution"] is None
        assert item["record"]["updated_at"] == classified["record"]["updated_at"]


@run_async
async def test_http_adopted_capture_cannot_change_its_original_source_identity(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        before = counts(controller.crm)
        capture = (await data(await client.post("/api/captures", json={"text": "我答应整理病历签名接口问题，没有约定执行时间。", "request_id": "keep-adopted-source"}, headers=headers), 202))["capture"]
        ready = await wait_capture(client, capture["id"], "review")
        action_id = ready["analysis"]["actions"][0]["id"]
        adopted = (await data(await client.post(f"/api/records/{capture['record_id']}/actions/{action_id}/adopt", json={}, headers=headers)))["record"]
        assert adopted["parent_record_id"] == capture["record_id"] and adopted["customer_id"] is None
        response = await client.post(f"/api/captures/{capture['id']}/classify", json={
            "purpose": "project_reference", "customer_id": scenes["projects"]["customer_id"], "opportunity_id": scenes["projects"]["opportunity_ids"][0],
            "expected_updated_at": ready["record"]["updated_at"]}, headers=headers)
        assert response.status == 400 and "采纳" in (await response.json())["error"]
        unchanged = await data(await client.get(f"/api/records/{capture['record_id']}"))
        assert unchanged["record"]["customer_id"] is None
        assert controller.crm.get_record(OWNER, adopted["id"])["parent_record_id"] == capture["record_id"]
        assert counts(controller.crm) == before


@run_async
async def test_confirming_source_customer_automatically_refreshes_analysis_for_adoption(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        before = counts(controller.crm)
        capture = (await data(await client.post("/api/captures", json={"text": OfflineResolution.phrase, "request_id": "automatic-reorganize"}, headers=headers), 202))["capture"]
        ready = await wait_capture(client, capture["id"], "review")
        candidate = ready["resolution"]["items"][0]
        confirmed = await data(await client.post(f"/api/captures/{capture['id']}/classify", json={
            "purpose": "note", "customer_id": candidate["customer_id"], "opportunity_id": candidate["opportunity_id"],
            "expected_updated_at": ready["record"]["updated_at"]}, headers=headers))
        assert confirmed["capture"]["status"] == "queued"
        fresh = await wait_capture(client, capture["id"], "filed")
        assert fresh["analysis"]["stale"] is False
        assert fresh["analysis"]["version"] > ready["analysis"]["version"]
        assert fresh["record_id"] == ready["record_id"] and fresh["record"]["original_content"] == OfflineResolution.phrase
        action_id = fresh["analysis"]["actions"][0]["id"]
        adopted = (await data(await client.post(f"/api/records/{capture['record_id']}/actions/{action_id}/adopt", json={}, headers=headers)))["record"]
        assert adopted["customer_id"] == candidate["customer_id"] and adopted["title"] == "整理病历签名接口问题"
        assert adopted["task_id"] is None and adopted["proposal_id"] is None
        overview = await data(await client.get("/api/overview"))
        child = next(item for item in overview["my_actions"] if item["record_id"] == adopted["id"])
        assert child["opportunity_id"] == candidate["opportunity_id"]
        assert counts(controller.crm) == before


@run_async
async def test_capture_audio_original_transcript_and_corrected_text_are_separate_and_retry_preserves_them(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        original = "我答应整理病厉千名接口问题，没有约定执行时间。"
        corrected = "我答应整理病历签名接口问题，没有约定执行时间。"
        created = await data(await client.post("/api/captures", json={"text": corrected, "original_transcript": original,
            "customer_id": scenes["projects"]["customer_id"], "request_id": "audio-with-correction"}, headers=headers), 202)
        capture = created["capture"]
        assert capture["record"]["content"] == corrected and capture["record"]["original_content"] == original
        ready = await wait_capture(client, capture["id"], "review")
        detail = await data(await client.get(f"/api/records/{capture['record_id']}"))
        assert detail["transcript"]["original_text"] == original and detail["transcript"]["corrected_text"] == corrected
        assert ready["analysis"]["actions"][0]["title"] == "整理病历签名接口问题"
        repeated = await data(await client.post("/api/captures", json={"text": corrected, "original_transcript": original,
            "customer_id": scenes["projects"]["customer_id"], "request_id": "audio-with-correction"}, headers=headers), 202)
        assert repeated["capture"]["record_id"] == capture["record_id"]
        await data(await client.post(f"/api/captures/{capture['id']}/retry", json={}, headers=headers), 202)
        fresh = await wait_capture(client, capture["id"], "review")
        assert fresh["record"]["original_content"] == original and fresh["record"]["content"] == corrected
        detail = await data(await client.get(f"/api/records/{capture['record_id']}"))
        assert detail["transcript"]["original_text"] == original
        assert detail["transcript"]["corrected_text"] == corrected


@pytest.mark.parametrize("already_scheduled", [False, True])
@run_async
async def test_adopting_specific_capture_action_does_not_double_count_original_but_preserves_its_existing_proposal(tmp_path, already_scheduled):
    async with training_client(tmp_path) as (controller, client, headers):
        before = counts(controller.crm)
        scenes = await manifest(client)
        project_id = scenes["projects"]["opportunity_ids"][1]
        initial = await data(await client.get("/api/overview"))
        initial_open = next(project for project in initial["projects"] if project["id"] == project_id)["open_actions"]
        created = (await data(await client.post("/api/captures", json={"text": OfflineResolution.phrase, "request_id": "adopt-no-double"}, headers=headers), 202))["capture"]
        ready = await wait_capture(client, created["id"], "review")
        candidate = ready["resolution"]["items"][0]
        await data(await client.post(f"/api/captures/{created['id']}/classify", json={"purpose": "action", "customer_id": candidate["customer_id"],
            "opportunity_id": candidate["opportunity_id"], "expected_updated_at": ready["record"]["updated_at"]}, headers=headers))
        fresh = await wait_capture(client, created["id"], "filed")
        if already_scheduled:
            calendar_seen = await data(await client.get(f"/api/records/{created['record_id']}"))
            assert len(calendar_seen['schedule_snapshot']) == 64
            await data(await client.post(f"/api/records/{created['record_id']}/schedule", json={"remind_at": NOW + 8 * 86400, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']}, headers=headers))
            original = await data(await client.get(f"/api/records/{created['record_id']}"))
            assert original["proposal"]["status"] == "pending" and original["task"] is None
            proposal_id = original["proposal"]["id"]
        adopted = (await data(await client.post(f"/api/records/{created['record_id']}/actions/{fresh['analysis']['actions'][0]['id']}/adopt", json={}, headers=headers)))["record"]
        parent = await data(await client.get(f"/api/records/{created['record_id']}"))
        assert adopted["kind"] == "action" and adopted["parent_record_id"] == created["record_id"]
        assert parent["record"]["original_content"] == OfflineResolution.phrase
        assert parent["capture"]["purpose"] == "action"
        overview = await data(await client.get("/api/overview"))
        current = next(project for project in overview["projects"] if project["id"] == project_id)
        assert any(item["record_id"] == adopted["id"] and item["opportunity_id"] == project_id for item in overview["my_actions"])
        if already_scheduled:
            assert parent["record"]["kind"] == "action"
            assert parent["proposal"]["id"] == proposal_id and parent["proposal"]["status"] == "pending"
            assert counts(controller.crm) == {**before, "proposals": before["proposals"] + 1}
        else:
            assert parent["record"]["kind"] == "note"
            assert not any(item["record_id"] == created["record_id"] for item in overview["my_actions"])
            assert current["open_actions"] == initial_open + 1
            assert counts(controller.crm) == before


class MaterialOrganizer:
    async def organize(self, text, now, context):
        return {"summary": "人工测试整理：" + text[:200], "key_points": [], "open_questions": ["需核对来源客户"], "actions": []}


@run_async
async def test_long_recording_material_http_exposes_attribution_without_auto_assignment(tmp_path):
    async with training_client(tmp_path) as (controller, client, headers):
        scenes = await manifest(client)
        project = scenes["projects"]
        resolver = Attribution(project["customer_id"], project["opportunity_ids"][1])
        controller.materials.resolution = resolver
        before = counts(controller.crm)
        text = "虚构录音：医院陈工提出签名接口需要核实。\n" + "这是现场讨论的背景，尚未确认采购条件。\n" * 350
        created = await data(await client.post("/api/materials", json={"provider": "manual", "title": "虚构签名会议录音", "text": text, "category": "meeting"}, headers=headers), 202)
        ready = await wait_material(client, created["material"]["id"], "review")
        assert resolver.calls and "虚构签名会议录音" in resolver.calls[0][1]
        assert ready["attribution"]["items"][0]["opportunity_id"] == project["opportunity_ids"][1]
        assert ready["analysis"]["attribution"] == ready["attribution"]
        assert ready["material"]["customer_id"] is None
        record = await data(await client.get(f"/api/records/{ready['material']['record_id']}"))
        assert record["record"]["customer_id"] is None
        assert ready["text"] == text and len(ready["segments"]) > 300
        assert counts(controller.crm) == before


@run_async
async def test_material_old_resolution_worker_cannot_overwrite_a_new_revision_and_retry(tmp_path):
    crm = CustomerStore(tmp_path / "material-worker-synthetic.sqlite3")
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer("owner", {"name": "虚构医疗客户"}, NOW)
    project = workspace.create_opportunity("owner", customer["id"], {"name": "签名接口试点"})
    resolver = Attribution(customer["id"], project["id"])
    resolver.release = asyncio.Event()
    service = MaterialService(crm, lock, organizer=MaterialOrganizer(), resolution=resolver, clock=lambda: NOW)
    try:
        made = service.enqueue("owner", {"provider": "manual", "title": "原录音", "text": "原文：医院签名接口仍需核对。"})
        task = asyncio.create_task(service.process_one())
        await resolver.started.wait()
        async with lock:
            corrected = service.update("owner", made["id"], {"revision": made["revision"], "text": "修订：本次说的是密钥轮换接口，未确认归属。", "title": "修订录音"})
        resolver.release.set()
        await task
        queued = service.detail("owner", made["id"])
        assert queued["material"]["revision"] == corrected["revision"]
        assert queued["material"]["status"] == "queued" and queued["analysis"] is None
        assert "密钥轮换" in queued["text"] and queued["original_version"]["text"] == "原文：医院签名接口仍需核对。"
        await service.process_one()
        current = service.detail("owner", made["id"])
        assert current["material"]["status"] == "review"
        assert "修订录音" in resolver.calls[-1][1] and "密钥轮换" in resolver.calls[-1][1]
        assert current["material"]["customer_id"] is None
        retried = service.retry("owner", made["id"], current["material"]["revision"])
        assert retried["revision"] > current["material"]["revision"] and retried["status"] == "queued"
        await service.process_one()
        assert service.detail("owner", made["id"])["text"] == current["text"]
        assert counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    finally:
        await service.close()
        crm.close()


@run_async
async def test_material_retry_refreshes_attribution_after_customer_catalogue_changes(tmp_path):
    crm = CustomerStore(tmp_path / "material-attribution-synthetic.sqlite3")
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    first = crm.create_customer("owner", {"name": "最初候选客户"}, NOW)
    second = crm.create_customer("owner", {"name": "后来核实客户"}, NOW)
    resolver = Attribution(first["id"])
    service = MaterialService(crm, lock, organizer=MaterialOrganizer(), resolution=resolver, clock=lambda: NOW)
    try:
        made = service.enqueue("owner", {"provider": "manual", "title": "没有完整客户名的复盘", "text": "昨天那边的接口需要进一步核对。"})
        await service.process_one()
        initial = service.detail("owner", made["id"])
        assert initial["attribution"]["items"][0]["customer_id"] == first["id"]
        resolver.customer_id = second["id"]
        service.retry("owner", made["id"], initial["material"]["revision"])
        await service.process_one()
        current = service.detail("owner", made["id"])
        assert len(resolver.calls) == 2
        assert current["attribution"]["items"][0]["customer_id"] == second["id"]
        assert current["material"]["customer_id"] is None
    finally:
        await service.close()
        crm.close()
