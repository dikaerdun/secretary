"""Synthetic HTTP diagnosis of the reported homepage research request.

Uses the existing unconfigured research Web fixture. It never opens deployed
data or invokes model/public-research providers. Expected errors are protocol
controls, not reproductions of the user's deployed failure.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest

from test_research_workspace_web import context


GOAL_TEXT = "帮我研究下交通部通信信息中心，看看有哪些职责和我们的市场方向一致，我们可以去突破"
ROUTE = "/api/secretary-goals/preview"
OWNER = "research-owner"
TABLES = (
    "crm_records", "tasks", "proposals", "notifications",
    "crm_research_runs", "crm_progress_runs", "crm_captures",
)


def _body(text=GOAL_TEXT):
    return json.dumps({"text": text}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _counts(controller):
    return {
        table: controller.crm._db.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in TABLES
    }


@asynccontextmanager
async def _isolated(tmp_path):
    external_calls = []

    async def deny_external_request(_client, method, url, **_kwargs):
        external_calls.append({"method": method, "url": str(url)})
        raise AssertionError("This fixture must not invoke a model provider")

    with patch.object(httpx.AsyncClient, "request", deny_external_request):
        async with context(tmp_path, configured=False) as fixture:
            client, call, controller, unit, researcher = fixture
            assert researcher is None
            assert not controller.secretary_goals.interpreter.available
            assert not controller.resolution.available
            session_response = await client.get("/api/session")
            assert session_response.status == 200
            session = await session_response.json()
            assert session["authenticated"] is True
            headers = {
                "Content-Type": "application/json",
                "X-CSRF-Token": session["csrf"],
            }
            try:
                yield client, call, controller, unit, headers, external_calls
            finally:
                # Do not let a swallowed provider error masquerade as no call.
                assert external_calls == []


async def _observe(client, controller, headers, method, route, body, label):
    before = _counts(controller)
    response = await client.request(method, route, data=body, headers=headers)
    payload = await response.json()
    after = _counts(controller)
    assert after == before
    assert all(value == 0 for value in before.values())
    evidence = {
        "label": label,
        "method": method,
        "path": route,
        "actual_status": response.status,
        "submitted_utf8_bytes": len(body) if body is not None else 0,
        "intent": payload.get("intent"),
        "method_of_classification": payload.get("method"),
        "error": payload.get("error"),
        "resolution_status": (payload.get("resolution") or {}).get("status"),
        "candidate_customer_ids": [
            item["customer_id"]
            for item in (payload.get("resolution") or {}).get("items", [])
        ],
        "counts_before": before,
        "counts_after": after,
        "business_writes": False,
        "scope": "Synthetic local TestServer only; not a formal-port request",
    }
    print("GOAL_REQUEST_DIAGNOSIS " + json.dumps(evidence, ensure_ascii=False))
    return response, payload


@pytest.mark.parametrize("matching_unit", [False, True],
                         ids=["no-matching-unit", "matching-unit"])
def test_original_goal_http_200_rules_without_business_writes(tmp_path,
                                                            matching_unit):
    async def run():
        async with _isolated(tmp_path) as fixture:
            client, call, controller, _, headers, external_calls = fixture
            matching = None
            if matching_unit:
                matching = (await call("POST", "/api/customers", {
                    "name": "交通部通信信息中心",
                }, 201))["customer"]
            submitted = _body()
            assert len(GOAL_TEXT) == 40
            assert len(submitted) == 131
            response, result = await _observe(
                client, controller, headers, "POST", ROUTE, submitted,
                "original-matching" if matching_unit else "original-unmatched",
            )
            assert response.status == 200
            assert result["intent"] == "research"
            assert result["method"] == "rules"
            assert result["text"] == GOAL_TEXT
            assert result["scope"] == {}
            resolution = result["resolution"]
            assert resolution["selected_customer_id"] is None
            assert resolution["requires_confirmation"] is True
            if matching_unit:
                assert resolution["status"] == "single"
                assert [item["customer_id"] for item in resolution["items"]] == [matching["id"]]
            else:
                assert resolution["status"] == "none"
                assert resolution["items"] == []
            assert external_calls == []
    asyncio.run(run())


def test_missing_goal_route_returns_generic_404_without_business_writes(tmp_path):
    async def run():
        async with _isolated(tmp_path) as fixture:
            client, _, controller, _, headers, _ = fixture
            response, result = await _observe(
                client, controller, headers, "POST",
                "/api/secretary-goals/missing-preview", _body(),
                "synthetic-missing-route",
            )
            assert response.status == 404
            assert result == {"error": "请求地址或请求内容无效。"}
    asyncio.run(run())


def test_correct_goal_route_wrong_method_returns_generic_405(tmp_path):
    async def run():
        async with _isolated(tmp_path) as fixture:
            client, _, controller, _, headers, _ = fixture
            response, result = await _observe(
                client, controller, headers, "GET", ROUTE, None,
                "synthetic-wrong-method",
            )
            assert response.status == 405
            assert result == {"error": "请求地址或请求内容无效。"}
    asyncio.run(run())


def test_oversized_goal_body_is_413_control_not_original_sentence(tmp_path):
    async def run():
        async with _isolated(tmp_path) as fixture:
            client, _, controller, _, headers, _ = fixture
            oversized = _body("帮我研究" + "x" * (64 * 1024))
            assert len(oversized) > 64 * 1024
            assert len(_body()) == 131
            response, result = await _observe(
                client, controller, headers, "POST", ROUTE, oversized,
                "synthetic-oversize-control",
            )
            assert response.status == 413
            assert result == {"error": "提交的内容过长。"}
    asyncio.run(run())
