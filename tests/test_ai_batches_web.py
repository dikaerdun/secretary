"""Real HTTP routes, fresh stores, no external API or running user service."""
import asyncio
from contextlib import asynccontextmanager

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.public_pages import PublicPageReader
from secretary.store import Store
from secretary.web import create_app, hash_password

NOW = 1_791_072_000.0
OWNER = "synthetic-batches-owner"


@asynccontextmanager
async def context(tmp_path):
    path = tmp_path / "fresh-batches-web.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    app = create_app(store, crm, asyncio.Lock(), OWNER, hash_password("test-only-password"), clock=lambda: NOW)
    controller = app.middlewares[0].__self__
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        unauthenticated = await client.get("/api/progress-workspaces")
        assert unauthenticated.status == 401
        login = await client.post("/api/login", json={"password": "test-only-password"})
        assert login.status == 200
        token = (await login.json())["csrf"]
        async def call(method, path, data=None, status=200):
            result = await client.request(method, path, json=data, headers={"X-CSRF-Token": token})
            payload = await result.json()
            assert result.status == status, (path, result.status, payload)
            return payload
        unit = (await call("POST", "/api/customers", {"name": "合成多批次银行", "notes": "已录入正式形态的合成资料"}, 201))["customer"]
        yield client, call, controller, unit
    finally:
        await client.close()
        crm.close()
        store.close()


def item(view, kind):
    return next(row for row in view["items"] if row["kind"] == kind)


async def ready(call, run_id):
    for _ in range(100):
        result = (await call("GET", f"/api/progress-runs/{run_id}"))["run"]
        if result["status"] not in ("queued", "processing"):
            return result
        await asyncio.sleep(.02)
    raise AssertionError("Isolated preparation worker did not finish")


def test_http_exchange_original_draft_partial_receipt_schedule_and_cas(tmp_path):
    async def run():
        async with context(tmp_path) as (client, call, controller, unit):
            crm = controller.crm
            row = crm.create_record(OWNER, {"customer_id": unit["id"], "title": "合成现场交流",
                "content": "客户说项目预算还未审批。 我答应发送适配清单。", "kind": "note", "category": "conversation"}, NOW)
            path = f"/api/exchange-workspaces/record/{row['id']}"
            first = (await call("GET", path))["workspace"]
            assert first["source"]["original_text"] == row["original_content"]
            no_csrf = await client.post(path + "/prepare", json={})
            assert no_csrf.status == 403
            view = (await call("POST", path + "/prepare", {}))["workspace"]
            action = item(view, "action")
            edited = (await call("PATCH", path, {"expected_revision": view["revision"], "source_revision": view["source_revision"],
                "items": [{"id": action["id"], "expected_version": action["version"], "selected": True,
                           "draft": {"title": "用户核对后的适配清单", "executor_kind": "self"}}]}))["workspace"]
            assert crm._db.execute("SELECT count(*) FROM crm_analysis_actions").fetchone()[0] == 0
            fact = next(row for row in edited["items"] if row["kind"] == "profile" and row["scope"]["scope"] == "project")
            action = item(edited, "action")
            body = {"request_id": "http-exchange-batch", "expected_revision": edited["revision"], "source_revision": edited["source_revision"],
                "items": [{"id": fact["id"], "expected_version": fact["version"], "expected_fact_id": None},
                          {"id": action["id"], "expected_version": action["version"]}]}
            receipt = await call("POST", path + "/confirm", body)
            assert receipt["status"] == "partial" and receipt["results"][0]["status"] == "blocked"
            assert receipt["results"][1]["status"] == "confirmed"
            record_id = receipt["results"][1]["result"]["record_id"]
            assert crm.get_record(OWNER, record_id)["title"] == "用户核对后的适配清单"
            assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert await call("POST", path + "/confirm", body) == receipt
            changed = {**body, "request_id": body["request_id"], "items": body["items"][1:]}
            await call("POST", path + "/confirm", changed, 409)
            fresh = (await call("GET", path))["workspace"]
            schedule = item(fresh, "schedule")
            scheduled = await call("POST", path + "/confirm", {"request_id": "http-explicit-calendar", "expected_revision": fresh["revision"],
                "source_revision": fresh["source_revision"], "items": [{"id": schedule["id"], "expected_version": schedule["version"],
                "draft": {"remind_at": NOW + 7200, "duration_minutes": 30}}]})
            assert scheduled["status"] == "complete"
            task = crm.get_task(OWNER, scheduled["results"][0]["result"]["task_id"])
            assert task["remind_at"] == NOW + 7200
            crm.update_record(OWNER, row["id"], {"content": "暂不发送清单，原安排需要用户核对。"}, NOW + 1)
            old = {"request_id": "old-source-attempt", "expected_revision": fresh["revision"], "source_revision": fresh["source_revision"],
                "items": [{"id": action["id"], "expected_version": action["version"]}]}
            await call("POST", path + "/confirm", old, 409)
            corrected = (await call("GET", path))["workspace"]
            assert corrected["status"] == "stale" and corrected["correction_impacts"]
            assert crm.get_task(OWNER, task["id"])["status"] == "pending"
            assert crm.get_record(OWNER, row["id"])["original_content"] == row["original_content"]
            foreign = crm.create_record("different-owner", {"title": "其他owner", "content": "不能读取"}, NOW)
            await call("GET", f"/api/exchange-workspaces/record/{foreign['id']}", status=404)
    asyncio.run(run())


def test_http_progress_worker_edit_adopt_feedback_scope_and_owner(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit):
            class Advisor:
                async def reply(self, context, history, text, now):
                    assert not controller.crm._db.in_transaction
                    return {"answer": "先核对接口范围，建议尚未代表客户承诺。", "questions": ["试点边界是否明确？"], "risks": ["预算待确认。"],
                        "next_moves": [{"title": "准备三条接口问题", "reason": "实际需求还需核对", "contact_hint": "联系人待核对", "preparation": "带接口清单", "success_signal": "明确试点范围"}]}
            controller.progress_workspace._advisor = Advisor()
            run = (await call("POST", "/api/progress-workspaces", {"request_id": "http-visit-prepare", "kind": "visit_prepare",
                "customer_id": unit["id"], "text": "准备下次拜访，需要明确接口范围。"}, 201))["run"]
            assert run["status"] == "queued"
            listed = await call("GET", f"/api/progress-workspaces?customer_id={unit['id']}")
            assert listed["runs"][0]["id"] == run["id"]
            prepared = await ready(call, run["id"])
            assert prepared["status"] == "ready" and prepared["mode"] == "model", prepared
            assert not controller.crm.list_records(OWNER)["total"]
            action = prepared["items"][0]
            edited = (await call("PATCH", f"/api/progress-runs/{run['id']}", {"expected_revision": prepared["revision"],
                "items": [{"id": action["id"], "selected": True, "draft": {"title": "用户核对后准备拜访", "content": "带三条范围问题", "executor_kind": "self"}}]}))["run"]
            await call("PATCH", f"/api/progress-runs/{run['id']}", {"expected_revision": prepared["revision"],
                "items": [{"id": action["id"], "selected": True}]}, 409)
            adopted = edited["items"][0]
            body = {"request_id": "http-progress-confirm", "expected_revision": edited["revision"], "items": [{"id": adopted["id"],
                "expected_item_revision": adopted["revision"], "expected_snapshot": adopted["versions"]["snapshot"]}]}
            result = await call("POST", f"/api/progress-runs/{run['id']}/confirm", body)
            assert result["status"] == "complete"
            record_id = result["results"][0]["record_id"]
            assert controller.crm.get_record(OWNER, record_id)["title"] == "用户核对后准备拜访"
            assert controller.crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            replay = await call("POST", f"/api/progress-runs/{run['id']}/confirm", body)
            assert replay["replayed"] and controller.crm.list_records(OWNER)["total"] == 1
            candidates = await call("GET", f"/api/progress-action-candidates?customer_id={unit['id']}")
            assert any(row["id"] == record_id for row in candidates["items"])
            foreign = controller.crm.create_customer("different-owner", {"name": "不能越界"}, NOW)
            await call("GET", f"/api/progress-action-candidates?customer_id={foreign['id']}", status=404)
            outsiders = controller.progress_workspace.create("different-owner", {"request_id": "foreign-run", "kind": "visit_prepare", "customer_id": foreign["id"]})
            await call("GET", f"/api/progress-runs/{outsiders['id']}", status=404)
            feedback = (await call("POST", "/api/progress-workspaces", {"request_id": "http-feedback", "kind": "followup_result",
                "customer_id": unit["id"], "record_id": record_id, "text": "客户尚未反馈，继续等下周消息。"}, 201))["run"]
            feedback = await ready(call, feedback["id"])
            outcome = next(row for row in feedback["items"] if row["type"] == "outcome")
            edited = (await call("PATCH", f"/api/progress-runs/{feedback['id']}", {"expected_revision": feedback["revision"],
                "items": [{"id": outcome["id"], "selected": True, "draft": {"decision": "waiting", "result": "明确未收到客户反馈"}}]}))["run"]
            outcome = next(row for row in edited["items"] if row["type"] == "outcome")
            receipt = await call("POST", f"/api/progress-runs/{feedback['id']}/confirm", {"request_id": "http-feedback-confirm", "expected_revision": edited["revision"],
                "items": [{"id": outcome["id"], "expected_item_revision": outcome["revision"], "expected_snapshot": outcome["versions"]["snapshot"]}]})
            assert receipt["status"] == "complete"
            assert controller.crm.get_record(OWNER, record_id)["status"] != "done"
            assert controller.crm._db.execute("SELECT count(*) FROM crm_action_outcomes").fetchone()[0] == 0
    asyncio.run(run())


def test_http_goals_queries_and_public_read_never_create_customer_evidence(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit):
            before = controller.crm.list_records(OWNER)["total"]
            for text, expected in [("帮我准备下次拜访", "visit_prepare"), ("给我看看本月有哪些日程", "query"),
                                   ("帮我复盘刚才的交流", "recap"), ("客户说预算暂未审批", "record")]:
                goal = await call("POST", "/api/secretary-goals/preview", {"text": text, "customer_id": unit["id"]})
                assert goal["intent"] == expected
            assert controller.crm.list_records(OWNER)["total"] == before
            class Content:
                async def iter_chunked(self, size):
                    yield "<html><title>合成官网</title><script>不能成为证据</script><p>合成银行位于杭州，预算尚待核实。本单位的公开资料仅供了解背景，采购时间与需求范围尚没有公开披露，需要后续核实。</p></html>".encode()
            class Reply:
                status, charset, content = 200, "utf-8", Content()
                headers = {"Content-Type": "text/html"}
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
            class Session:
                calls = []
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                def get(self, url, **options):
                    assert options == {"allow_redirects": False}
                    self.calls.append(url)
                    return Reply()
            session = Session()
            controller.public_pages = PublicPageReader(session_factory=lambda: session, clock=lambda: NOW)
            source = (await call("POST", "/api/public-pages/preview", {"url": "https://official.example/unit"}))["source"]
            assert "预算尚待核实" in source["text"] and "不能成为证据" not in source["text"]
            assert "尚未写入画像" in source["warning"]
            for url in ("http://127.0.0.1/private", "http://192.168.1.131/private", "https://user:password@official.example/private"):
                await call("POST", "/api/public-pages/preview", {"url": url}, 400)
            assert session.calls == ["https://official.example/unit"]
            assert controller.crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0
            assert controller.crm.list_records(OWNER)["total"] == before
    asyncio.run(run())


def test_http_plan_day_week_month_rules_no_automatic_calendar(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit):
            for period in ("day", "week", "month"):
                created = (await call("POST", "/api/progress-workspaces", {"request_id": "http-plan-" + period,
                    "kind": "plan", "customer_id": unit["id"], "period": period, "start_date": "2026-10-04",
                    "text": "梳理该周期的重点推进计划。"}, 201))["run"]
                prepared = await ready(call, created["id"])
                assert prepared["status"] == "ready" and prepared["mode"] == "rules"
                assert prepared["period"] == period and not any(row["selected"] for row in prepared["items"])
                cancelled = (await call("POST", f"/api/progress-runs/{created['id']}/cancel", {"expected_revision": prepared["revision"]}))["run"]
                assert cancelled["status"] == "cancelled"
                retried = (await call("POST", f"/api/progress-runs/{created['id']}/retry", {"expected_revision": cancelled["revision"]}))["run"]
                assert retried["status"] == "queued"
            assert controller.crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert controller.crm.list_records(OWNER)["total"] == 0
    asyncio.run(run())
