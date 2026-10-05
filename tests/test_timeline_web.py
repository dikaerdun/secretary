"""Independent timeline HTTP journeys: synthetic DBs and offline providers only."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app, hash_password


NOW = 1_800_000_000.0
PASSWORD = "synthetic-timeline-trial-password"
REPLY = {
    "answer": "先兑现部署资料，再核实采购路径；这仍是销售建议。",
    "next_moves": [{"title": "与王工核对采购材料", "reason": "本次讨论仍缺采购材料清单",
                    "contact_hint": "王工", "preparation": "准备一页部署边界与验收条件",
                    "success_signal": "由王工说明下一次采购沟通需要的材料"}],
    "questions": ["本项目由谁核对采购材料？"], "risks": ["尚未明确审批路径"],
}


class Advisor:
    def __init__(self):
        self.calls = []
        self.error = False
        self.started = asyncio.Event()
        self.release = None

    async def reply(self, context, history, text, now):
        self.calls.append(copy.deepcopy({"context": context, "history": history, "text": text, "now": now}))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise RuntimeError("synthetic provider secret must not appear in HTTP output")
        return copy.deepcopy(REPLY)


class Organizer:
    async def organize(self, text, now, context):
        return {"summary": "客户要求补充部署方案。", "key_points": ["核对试点边界"],
                "open_questions": [], "actions": [{"title": "发送部署方案", "kind": "commitment",
                    "owner_hint": "我", "reason": "原话：我答应发送部署方案", "remind_at": None}]}


@asynccontextmanager
async def timeline_app(tmp_path):
    path = tmp_path / "independent-timeline-synthetic.sqlite3"
    crm, store, lock = CustomerStore(path), Store(path), asyncio.Lock()
    materials = MaterialService(crm, lock, organizer=Organizer())
    advisor = Advisor()
    app = create_app(store, crm, lock, "me", hash_password(PASSWORD), clock=lambda: NOW,
                     materials=materials)
    controller = app.middlewares[0].__self__
    controller.discussions.advisor = advisor
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    login = await client.post("/api/login", json={"password": PASSWORD})
    assert login.status == 200
    csrf = (await login.json())["csrf"]

    async def call(method, route, data=None, status=200, **kwargs):
        headers = {"X-CSRF-Token": csrf, **kwargs.pop("headers", {})}
        result = await client.request(method, route, json=data, headers=headers, **kwargs)
        value = await result.json()
        expected = status if isinstance(status, tuple) else (status,)
        assert result.status in expected, (route, result.status, value)
        return value

    try:
        yield client, call, crm, store, controller, advisor, materials
    finally:
        await client.close()
        await materials.close()
        crm.close()
        store.close()


async def customer(call, name, **values):
    return (await call("POST", "/api/customers", {"name": name, **values}, 201))["customer"]


async def contact(call, customer_id, name="王工", **values):
    return (await call("POST", f"/api/customers/{customer_id}/contacts", {"name": name, **values}, 201))["contact"]


async def project(call, customer_id, name="密码改造项目", **values):
    return (await call("POST", f"/api/customers/{customer_id}/opportunities", {"name": name, **values}, 201))["opportunity"]


async def member(call, project, person, **values):
    return (await call("POST", f"/api/customers/{project['customer_id']}/opportunities/{project['id']}/stakeholders",
        {"contact_id": person["id"], "expected_revision": project["revision"], **values}))["relationship"]


def schedule_counts(crm):
    return {table: crm._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("tasks", "proposals", "notifications")}


def last_assistant(result):
    return next(item for item in reversed(result["messages"]) if item["role"] == "assistant")


async def capture(call, scope, text, *, kind="communication", request_id="capture-1", **values):
    return await call("POST", "/api/timeline/records", {**scope, "request_id": request_id,
        "text": text, "kind": kind, "occurred_at": None, **values}, 201)


async def event(call, key):
    return (await call("GET", f"/api/timeline/events/{key}"))["event"]


async def context_change(call, item, **values):
    return (await call("POST", f"/api/timeline/events/{item['key']}/context",
        {"expected_revision": item["revision"], **values}))["event"]


async def discuss(call, customer_id, *, contact_id=None, opportunity_id=None, event_keys=None, request_id="thread-1"):
    data = {"customer_id": customer_id, "title": "王工下次回访怎么推进", "request_id": request_id}
    if contact_id is not None:
        data["contact_id"] = contact_id
    if opportunity_id is not None:
        data["opportunity_id"] = opportunity_id
    if event_keys is not None:
        data["timeline_event_keys"] = event_keys
    return (await call("POST", "/api/sales-discussions", data, 201))["thread"]


def test_s1_http_person_review_capture_discuss_action_and_explicit_schedule(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            unit = await customer(call, "合成银行时间轴")
            person = await contact(call, unit["id"], department="技术部", phone="13812345678")
            other = await contact(call, unit["id"], "李经理", department="采购部", phone="13987654321")
            p = await project(call, unit["id"])
            p = await member(call, p, person, roles=["technical_reviewer"], evidence="本人明确参与本项目")
            p = await member(call, p, other, roles=["procurement"], evidence="采购流程参加人")
            route = f"/api/timeline?contact_id={person['id']}&opportunity_id={p['id']}"
            empty = await call("GET", route)
            assert empty["scope"]["type"] == "contact" and empty["scope"]["id"] == person["id"]
            assert empty["items"] == [] and empty["summary"]["latest_communication"] is None
            original_text = "王工明确要求下次先展示部署边界，然后再谈材料清单。"
            communicated = await capture(call, {"contact_id": person["id"], "opportunity_id": p["id"]},
                original_text, occurred_at=NOW - 86400, request_id="s1-meeting")
            thought = await capture(call, {"contact_id": person["id"], "opportunity_id": p["id"]},
                "我猜王工关心上线风险，此为个人判断，尚未得到本人确认。", kind="reflection",
                occurred_at=NOW - 3600, request_id="s1-thought", related_event_key=communicated["event"]["key"])
            unrelated = await capture(call, {"contact_id": other["id"], "opportunity_id": p["id"]},
                "无关采购秘密：另一个人的合同争议，不属于王工的直接交流。", request_id="s1-other")
            assert communicated["record"]["content"] == original_text
            assert communicated["event"]["contact_relations"][0]["relation"] == "direct"
            assert thought["event"]["contact_relations"][0]["relation"] == "about"
            reviewed = await call("GET", route)
            assert {item["key"] for item in reviewed["items"]} == {communicated["event"]["key"], thought["event"]["key"]}
            assert reviewed["summary"]["latest_communication"]["key"] == communicated["event"]["key"]
            assert reviewed["items"][0]["key"] == thought["event"]["key"]
            assert reviewed["items"][1]["occurred_at"] == NOW - 86400
            assert reviewed["items"][1]["recorded_at"] == NOW
            thread = await discuss(call, unit["id"], contact_id=person["id"], opportunity_id=p["id"],
                event_keys=[communicated["event"]["key"], thought["event"]["key"]], request_id="s1-thread")
            assert thread["contact_id"] == person["id"] and thread["contact_department"] == "技术部"
            assert thread["timeline_event_keys"] == [communicated["event"]["key"], thought["event"]["key"]]
            assert not advisor.calls  # Creating a discussion is not sending a message.
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "结合上次原话和我的想法，下次回访先做什么？", "request_id": "s1-message"})
            context = advisor.calls[0]["context"]
            assert context["timeline"]["scope"]["id"] == person["id"]
            assert {item["key"] for item in context["timeline"]["events"]} >= {communicated["event"]["key"], thought["event"]["key"]}
            serialized = json.dumps(context, ensure_ascii=False)
            assert "部署边界" in serialized and "个人判断" in serialized
            assert "无关采购秘密" not in serialized
            assert "13812345678" not in serialized and "13987654321" not in serialized
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
            assistant = last_assistant(reply)
            adoption_route = f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt"
            adopted = (await call("POST", adoption_route, {}))["record"]
            repeated = (await call("POST", adoption_route, {}))["record"]
            assert repeated["id"] == adopted["id"]
            viewed = await call("GET", route)
            assert any(action["id"] == adopted["id"] for action in viewed["summary"]["open_actions"])
            assert not any(item["key"] == f"record:{adopted['id']}" and item["kind"] == "communication" for item in viewed["items"])
            other_view = await call("GET", f"/api/timeline?contact_id={other['id']}&opportunity_id={p['id']}")
            assert all(action["id"] != adopted["id"] for action in other_view["summary"]["open_actions"])
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
            calendar_seen = await call("GET", f"/api/records/{adopted['id']}")
            assert len(calendar_seen['schedule_snapshot']) == 64
            scheduled = await call("POST", f"/api/records/{adopted['id']}/schedule", {"remind_at": NOW + 3600, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
            assert scheduled["proposal"]["status"] == "pending"
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 1, "notifications": 0}
            confirmed = await call("POST", f"/api/records/{adopted['id']}/confirm",
                {"proposal_id": scheduled["proposal"]["id"], "updated_at": scheduled["proposal"]["updated_at"]})
            assert confirmed["task"]["status"] == "pending"
            assert schedule_counts(crm) == {"tasks": 1, "proposals": 1, "notifications": 1}
            after_schedule = await call("GET", route)
            assert any(action["id"] == adopted["id"] for action in after_schedule["summary"]["open_actions"])
            filtered = await call("GET", f"/api/sales-discussions?contact_id={person['id']}")
            assert filtered["total"] == 1 and filtered["items"][0]["id"] == thread["id"]
            assert (await call("GET", f"/api/sales-discussions?contact_id={other['id']}"))["items"] == []
            assert unrelated["event"]["key"] not in {item["key"] for item in viewed["items"]}
    asyncio.run(run())


def test_s2_http_many_people_one_visit_recording_group_and_next_day_reflection(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, _, materials):
            unit = await customer(call, "多人合成会谈单位")
            a = await contact(call, unit["id"], department="信息部")
            b = await contact(call, unit["id"], "李经理", department="业务部")
            mentioned = await contact(call, unit["id"], "赵总", department="领导办公室")
            visit = (await call("POST", "/api/visits", {"title": "多人部署会谈", "customer_id": unit["id"], "occurred_at": NOW - 86400}, 201))["visit"]
            sources = []
            for role in ("recording", "recap"):
                saved = await call("POST", f"/api/visits/{visit['id']}/materials", {"role": role,
                    "provider": "manual", "title": role + "原资料", "text": "我答应发送部署方案。王工和李经理参加会谈，提到赵总尚未表态。"}, 202)
                sources.append(saved["material"])
                await materials.process_one()
            unit_view = await call("GET", f"/api/timeline?customer_id={unit['id']}")
            assert [item["key"] for item in unit_view["items"] if item["kind"] == "communication"] == [f"visit:{visit['id']}"]
            grouped = await event(call, f"visit:{visit['id']}")
            assert {source["id"] for source in grouped["source_refs"] if source["type"] == "material"} == {source["id"] for source in sources}
            assert not (await call("GET", f"/api/timeline?contact_id={a['id']}"))["items"]
            grouped = await context_change(call, grouped, contact_relations=[
                {"contact_id": a["id"], "relation": "direct"}, {"contact_id": b["id"], "relation": "direct"},
                {"contact_id": mentioned["id"], "relation": "about"}])
            for person in (a, b, mentioned):
                view = await call("GET", f"/api/timeline?contact_id={person['id']}")
                assert len(view["items"]) == 1 and view["items"][0]["key"] == grouped["key"]
                relation = next(item for item in view["items"][0]["contact_relations"] if item["contact_id"] == person["id"])
                assert relation["relation"] == ("about" if person == mentioned else "direct")
            recap = await event(call, f"material:{sources[1]['id']}")
            assert recap["key"] == f"material:{sources[1]['id']}" and recap["merged_into"] == grouped["key"]
            recap = await context_change(call, recap, separate_event=True, kind="reflection", occurred_at=NOW - 3600,
                related_event_key=grouped["key"], contact_relations=[{"contact_id": a["id"], "relation": "about"}])
            assert recap["occurred_at"] == NOW - 3600 and recap["kind"] == "reflection"
            assert recap["related_event_key"] == grouped["key"]
            a_view = await call("GET", f"/api/timeline?contact_id={a['id']}")
            assert [item["key"] for item in a_view["items"]] == [recap["key"], grouped["key"]]
            assert (await event(call, grouped["key"]))["occurred_at"] == NOW - 86400
            unit_view = await call("GET", f"/api/timeline?customer_id={unit['id']}")
            assert len([item for item in unit_view["items"] if item["kind"] == "communication"]) == 1
            assert crm._db.execute("SELECT COUNT(*) FROM crm_visits").fetchone()[0] == 1
            assert crm._db.execute("SELECT COUNT(*) FROM crm_materials").fetchone()[0] == 2
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_s3_http_group_person_child_project_scope_and_selected_source_guard(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            group = await customer(call, "合成采购集团", unit_type="group")
            child = await customer(call, "合成业务子公司", parent_customer_id=group["id"], unit_type="company")
            buyer = await contact(call, group["id"], department="集团采购中心")
            tech = await contact(call, child["id"], "张工", department="信息部")
            same_name = await contact(call, group["id"], department="其他部门")
            p = await project(call, child["id"], "子公司加密采购")
            p = await member(call, p, buyer, roles=["procurement"], evidence="明确本项目集团采购人")
            p = await member(call, p, tech, roles=["technical_reviewer"], evidence="明确本项目技术人")
            personal = await capture(call, {"contact_id": buyer["id"], "opportunity_id": p["id"]},
                "集团采购王工说明，必须先核对材料，再走本项目审批。", request_id="s3-buyer", occurred_at=NOW - 7200)
            unrelated = await capture(call, {"contact_id": tech["id"], "opportunity_id": p["id"]},
                "张工独有协议细节，不能凭共同项目认定王工参与这次交流。", request_id="s3-tech")
            homonym = await capture(call, {"contact_id": same_name["id"]},
                "同名王工私人复盘无关材料，不属于集团采购中心王工。", request_id="s3-homonym")
            view = await call("GET", f"/api/timeline?contact_id={buyer['id']}&opportunity_id={p['id']}")
            assert view["scope"]["customer_id"] == group["id"]
            assert {item["key"] for item in view["items"]} == {personal["event"]["key"]}
            assert personal["record"]["customer_id"] == child["id"]
            assert any(item["id"] == p["id"] and item["customer_id"] == child["id"] for item in view["projects"])
            unit_view = await call("GET", f"/api/timeline?customer_id={child['id']}")
            assert {personal["event"]["key"], unrelated["event"]["key"]} <= {item["key"] for item in unit_view["items"]}
            assert (await call("GET", f"/api/timeline?customer_id={group['id']}"))["scope"]["id"] == group["id"]
            await call("POST", "/api/sales-discussions", {"customer_id": child["id"], "opportunity_id": p["id"],
                "contact_id": buyer["id"], "timeline_event_keys": [unrelated["event"]["key"]]}, 409)
            await call("POST", "/api/sales-discussions", {"customer_id": child["id"], "opportunity_id": p["id"],
                "contact_id": same_name["id"]}, 400)
            thread = await discuss(call, child["id"], contact_id=buyer["id"], opportunity_id=p["id"],
                event_keys=[personal["event"]["key"]], request_id="s3-discussion")
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "针对采购中心王工，下次具体怎么推进？", "request_id": "s3-message"})
            context = advisor.calls[0]["context"]
            assert context["timeline"]["scope"]["customer_id"] == group["id"]
            assert context["project"]["name"] == p["name"]
            assert [item["key"] for item in context["timeline"]["events"] if item["kind"] == "communication"] == [personal["event"]["key"]]
            text = json.dumps(context, ensure_ascii=False)
            assert "集团采购王工说明" in text and "张工独有协议细节" not in text and "同名王工私人复盘" not in text
            assert "集团采购中心" in text
            assistant = last_assistant(reply)
            action = (await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt", {}))["record"]
            assert action["customer_id"] == child["id"]
            assert any(item["id"] == action["id"] for item in (await call("GET", f"/api/timeline?contact_id={buyer['id']}"))["summary"]["open_actions"])
            assert all(item["id"] != action["id"] for item in (await call("GET", f"/api/timeline?contact_id={tech['id']}"))["summary"]["open_actions"])
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
            assert homonym["event"]["key"] not in {item["key"] for item in view["items"]}
    asyncio.run(run())


def test_http_source_edit_invalidates_person_link_and_old_ai_advice_then_reconfirm(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            unit = await customer(call, "来源改正合成单位")
            person = await contact(call, unit["id"], department="信息部")
            source = await capture(call, {"contact_id": person["id"]}, "王工说先发送部署方案。",
                occurred_at=NOW - 86400, request_id="stale-source")
            thread = await discuss(call, unit["id"], contact_id=person["id"],
                event_keys=[source["event"]["key"]], request_id="stale-thread")
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "请结合原话给建议", "request_id": "stale-message"})
            old_ai = last_assistant(reply)
            assert not old_ai["stale"] and "部署方案" in json.dumps(advisor.calls[0]["context"], ensure_ascii=False)
            changed = (await call("PATCH", f"/api/records/{source['record']['id']}",
                {"content": "更正：当时是李经理说需要方案，不是王工承诺。", "expected_updated_at": source["record"]["updated_at"]}))["record"]
            current = await event(call, source["event"]["key"])
            assert current["needs_review"]
            personal = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert source["event"]["key"] not in {item["key"] for item in personal["items"]}
            unit_view = await call("GET", f"/api/timeline?customer_id={unit['id']}")
            assert next(item for item in unit_view["items"] if item["key"] == source["event"]["key"])["needs_review"]
            refreshed = await call("GET", f"/api/sales-discussions/{thread['id']}")
            assert last_assistant(refreshed)["stale"]
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{old_ai['id']}/actions/1/adopt", {}, 409)
            await call("POST", f"/api/timeline/events/{source['event']['key']}/context",
                {"expected_revision": source["event"]["revision"], "occurred_at": NOW - 7200}, 409)
            assert (await call("GET", f"/api/records/{changed['id']}"))["record"]["content"] == changed["content"]
            current = await context_change(call, current, contact_relations=[{"contact_id": person["id"], "relation": "about"}])
            assert not current["needs_review"] and current["occurred_at"] == NOW - 86400
            assert source["event"]["key"] in {item["key"] for item in (await call("GET", f"/api/timeline?contact_id={person['id']}"))["items"]}
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "来源已经改正并确认是关于王工的资料，请重新研判。", "request_id": "fresh-message"})
            assert not last_assistant(reply)["stale"]
            assert "更正" in json.dumps(advisor.calls[-1]["context"]["timeline"], ensure_ascii=False)
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_http_failed_person_discussion_keeps_original_retry_and_request_idempotency(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            unit = await customer(call, "讨论失败合成单位")
            person = await contact(call, unit["id"])
            source = await capture(call, {"contact_id": person["id"]}, "现场想法\n不能丢掉这两行。", request_id="same-capture")
            replay = await call("POST", "/api/timeline/records", {"contact_id": person["id"], "request_id": "same-capture",
                "text": "现场想法\n不能丢掉这两行。", "kind": "communication", "occurred_at": None})
            assert not replay["created"] and replay["record"]["id"] == source["record"]["id"]
            await call("POST", "/api/timeline/records", {"contact_id": person["id"], "request_id": "same-capture",
                "text": "旧请求编号不能用于新内容。", "kind": "communication", "occurred_at": None}, 409)
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="retry-thread")
            replay_thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="retry-thread")
            assert replay_thread["id"] == thread["id"]
            advisor.error = True
            original = "我现在想讨论王工推进问题，请保留完整原话。"
            route = f"/api/sales-discussions/{thread['id']}/messages"
            payload = {"text": original, "request_id": "retry-message"}
            failed = await call("POST", route, payload)
            assert len(failed["messages"]) == 1 and failed["messages"][0]["status"] == "failed"
            assert failed["messages"][0]["text"] == original
            assert "synthetic provider secret" not in json.dumps(failed)
            advisor.error = False
            succeeded = await call("POST", route, payload)
            assert [item["role"] for item in succeeded["messages"]] == ["user", "assistant"]
            assert succeeded["messages"][0]["id"] == failed["messages"][0]["id"]
            repeated = await call("POST", route, payload)
            assert repeated["messages"] == succeeded["messages"] and len(advisor.calls) == 2
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 1
            assert crm._db.execute("SELECT COUNT(*) FROM crm_sales_discussions").fetchone()[0] == 1
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_http_scope_owner_csrf_auth_and_context_validation(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (client, call, crm, _, controller, _, _):
            unit = await customer(call, "安全合成单位")
            person = await contact(call, unit["id"])
            source = await capture(call, {"contact_id": person["id"]}, "原始记录必须完整保留。", request_id="security-source")
            foreign_unit = crm.create_customer("other", {"name": "另一owner合成单位"}, NOW)
            foreign_person = crm.create_contact("other", foreign_unit["id"], {"name": "另一owner王工"}, NOW)
            foreign_record = crm.create_record("other", {"title": "另一owner私有原话", "content": "不能泄露", "customer_id": foreign_unit["id"]}, NOW)
            for route in (f"/api/timeline?customer_id={foreign_unit['id']}", f"/api/timeline?contact_id={foreign_person['id']}",
                          f"/api/timeline/events/record:{foreign_record['id']}"):
                result = await call("GET", route, status=404)
                assert "不能泄露" not in json.dumps(result, ensure_ascii=False)
            await call("POST", f"/api/timeline/events/{source['event']['key']}/context",
                {"expected_revision": source["event"]["revision"], "contact_relations": [{"contact_id": foreign_person["id"], "relation": "direct"}]}, (400, 404))
            await call("POST", "/api/timeline/records", {"customer_id": unit["id"], "contact_id": person["id"],
                "text": "双焦点不合法", "kind": "communication", "request_id": "bad-scope", "occurred_at": None}, 400)
            await call("GET", "/api/timeline", status=400)
            await call("GET", f"/api/timeline?contact_id={person['id']}&kind=unsupported", status=400)
            for values in ({"occurred_at": "昨天"}, {"occurred_at": True}, {"kind": "ai_fact"},
                {"contact_relations": [{"contact_id": person["id"], "relation": "guess"}]},
                {"related_event_key": source["event"]["key"]}):
                await call("POST", f"/api/timeline/events/{source['event']['key']}/context",
                    {"expected_revision": source["event"]["revision"], **values}, 400)
            for route, payload in (("/api/timeline/records", {"contact_id": person["id"], "text": "无CSRF不写入", "request_id": "forged", "kind": "reflection", "occurred_at": None}),
                (f"/api/timeline/events/{source['event']['key']}/context", {"expected_revision": source["event"]["revision"], "occurred_at": None})):
                result = await client.post(route, json=payload)
                assert result.status == 403
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE owner='me'").fetchone()[0] == 1
            assert (await call("GET", f"/api/records/{source['record']['id']}"))["record"]["content"] == "原始记录必须完整保留。"
            assert controller.timeline is not None
            client.session.cookie_jar.clear()
            for route in (f"/api/timeline?contact_id={person['id']}", f"/api/timeline/events/{source['event']['key']}"):
                result = await client.get(route)
                assert result.status == 401
            result = await client.post("/api/timeline/records", json={"contact_id": person["id"]})
            assert result.status in (401, 403)
    asyncio.run(run())


def test_http_kind_search_pagination_unknown_time_and_same_second_stable_order(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, _, _, _, _, _):
            unit = await customer(call, "检索分页合成单位")
            person = await contact(call, unit["id"])
            known = await capture(call, {"contact_id": person["id"]}, "部署边界现场原话", request_id="search-known", occurred_at=NOW - 86400)
            unknown = await capture(call, {"contact_id": person["id"]}, "未知日期个人想法", kind="reflection", request_id="search-unknown")
            tied = await capture(call, {"contact_id": person["id"]}, "同秒另一条思考", kind="reflection", request_id="search-tied")
            route = f"/api/timeline?contact_id={person['id']}"
            first = await call("GET", route+"&page=1&page_size=2")
            second = await call("GET", route+"&page=2&page_size=2")
            assert first["total"] == second["total"] == 3 and first["pages"] == 2
            assert len(first["items"]) == 2 and len(second["items"]) == 1
            assert {item["key"] for item in first["items"]}.isdisjoint({item["key"] for item in second["items"]})
            assert first["items"] == (await call("GET", route+"&page=1&page_size=2"))["items"]
            reflected = await call("GET", route+"&kind=reflection")
            assert {item["key"] for item in reflected["items"]} == {unknown["event"]["key"], tied["event"]["key"]}
            assert all(item["occurred_at"] is None and item["recorded_at"] == NOW for item in reflected["items"])
            searched = await call("GET", route+"&q=部署边界")
            assert searched["total"] == 1 and searched["items"][0]["key"] == known["event"]["key"]
            for suffix in ("&page=0", "&page_size=0", "&page_size=201", "&page=x"):
                await call("GET", route+suffix, status=400)
    asyncio.run(run())


def test_http_new_relevant_evidence_during_model_call_marks_reply_stale(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            unit = await customer(call, "推理中变更合成单位")
            person = await contact(call, unit["id"])
            await capture(call, {"contact_id": person["id"]}, "王工要求先准备部署说明。", request_id="race-before")
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="race-thread")
            advisor.release = asyncio.Event()
            pending = asyncio.create_task(call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "基于当前资料判断下一步", "request_id": "race-message"}))
            await asyncio.wait_for(advisor.started.wait(), timeout=3)
            await capture(call, {"contact_id": person["id"]}, "新原话：项目暂缓，先补充审批材料。", request_id="race-after")
            advisor.release.set()
            reply = await asyncio.wait_for(pending, timeout=5)
            assistant = last_assistant(reply)
            assert assistant["stale"]
            assert "项目暂缓" not in json.dumps(advisor.calls[0]["context"], ensure_ascii=False)
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt", {}, 400)
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_http_person_result_and_manual_same_second_progress_are_both_visible(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, _, _):
            unit = await customer(call, "结果反馈合成单位")
            person = await contact(call, unit["id"])
            await capture(call, {"contact_id": person["id"]}, "王工让我们先提供方案。", request_id="result-context")
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="result-thread")
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages",
                {"text": "给我后续核对材料的动作", "request_id": "result-message"})
            assistant = last_assistant(reply)
            action = (await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt", {}))["record"]
            detail = await call("GET", f"/api/records/{action['id']}")
            completed = await call("POST", f"/api/records/{action['id']}/complete-outcome",
                {"request_id": "result-finish", "result": "已把材料发给王工，对方确认收到。",
                    "next_step": "下次向王工解释上线风险，先核对双方材料。", "next_title": "继续向王工解释上线风险",
                    "expected_snapshot": detail["completion_snapshot"]})
            await call("POST", f"/api/records/{action['id']}/activities", {"content": "同秒补充进展：对方请我们下次解释上线风险。"}, 201)
            view = await call("GET", f"/api/timeline?contact_id={person['id']}&kind=result")
            assert any(item["key"] == f"outcome:{completed['outcome']['id']}" for item in view["items"])
            assert any("同秒补充进展" in item["text"] for item in view["items"])
            assert sum("已把材料发给王工，对方确认收到" in item["text"] for item in view["items"]) == 1
            assert all(item["id"] != action["id"] for item in view["summary"]["open_actions"])
            assert completed["next_record"] is not None
            assert any(item["id"] == completed["next_record"]["id"] for item in view["summary"]["open_actions"])
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_http_new_person_evidence_outside_initial_selected_keys_stales_and_enters_next_reply(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, crm, _, _, advisor, _):
            unit = await customer(call, "带入历史后新增证据合成单位")
            person = await contact(call, unit["id"])
            earlier = await capture(call, {"contact_id": person["id"]}, "上次王工请先准备试点方案。", request_id="chosen-old")
            thread = await discuss(call, unit["id"], contact_id=person["id"],
                event_keys=[earlier["event"]["key"]], request_id="chosen-thread")
            route = f"/api/sales-discussions/{thread['id']}"
            first = await call("POST", route+"/messages", {"text": "针对上次明确带入的历史，该怎么推进？", "request_id": "chosen-first"})
            old_ai = last_assistant(first)
            assert not old_ai["stale"]
            assert not last_assistant(await call("GET", route))["stale"]  # This discussion's own reply is not new evidence.
            new_evidence = await capture(call, {"contact_id": person["id"]},
                "项目暂停：王工新明确说明，暂时停止试点，先等上级审批。", request_id="chosen-new")
            refreshed = await call("GET", route)
            assert last_assistant(refreshed)["stale"]
            await call("POST", route+f"/messages/{old_ai['id']}/actions/1/adopt", {}, (400, 409))
            second = await call("POST", route+"/messages", {"text": "新增原话已说明项目暂停，建议如何调整？", "request_id": "chosen-second"})
            assert not last_assistant(second)["stale"]
            context = advisor.calls[-1]["context"]["timeline"]
            assert {earlier["event"]["key"], new_evidence["event"]["key"]} <= {item["key"] for item in context["events"]}
            assert "项目暂停" in json.dumps(context, ensure_ascii=False)
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
    asyncio.run(run())
