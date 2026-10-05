"""Deeper sales trials on fresh synthetic stores; no real configuration/services."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import SHANGHAI, Store
from secretary.visits import VisitService
from secretary.web import create_app, hash_password
from test_timeline_web import capture, contact, context_change, customer, discuss, event, last_assistant, member, project, schedule_counts


PASSWORD = "synthetic-deep-timeline-trial-password"
NOW = datetime.fromisoformat("2026-10-03T10:00:00+08:00").timestamp()


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


class Advisor:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.moves = [{"title": "与王工核对推进材料", "reason": "材料与责任分工仍需核实",
            "contact_hint": "王工", "preparation": "准备上次承诺与问题清单", "success_signal": "明确下一步材料与责任人"}]

    async def reply(self, context, history, text, now):
        self.calls.append(copy.deepcopy({"context": context, "history": history, "text": text, "now": now}))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic private provider failure")
        return {"answer": "先核对本轮涉及的项目与责任，再兑现已明确的承诺；这是建议。",
            "next_moves": copy.deepcopy(self.moves), "questions": ["本轮先推进哪个项目？"], "risks": ["尚待核实部分材料"]}


class Organizer:
    async def organize(self, text, now, context):
        return {"summary": "一份合成会谈资料。", "key_points": [], "open_questions": [], "actions": []}


@asynccontextmanager
async def trial_app(tmp_path, *, seed=None):
    path = tmp_path / "deep-trial-synthetic.sqlite3"
    clock, lock = Clock(), asyncio.Lock()
    crm, store = CustomerStore(path), Store(path)
    before = seed(crm, store, clock) if seed else None
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=clock)
    visits = VisitService(crm, materials, lock)
    advisor = Advisor()
    app = create_app(store, crm, lock, "me", hash_password(PASSWORD), clock=clock, materials=materials, visits=visits)
    controller = app.middlewares[0].__self__
    controller.discussions.advisor = advisor
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True),
        json_serialize=lambda value: json.dumps(value, ensure_ascii=False))
    await client.start_server()
    login = await client.post("/api/login", json={"password": PASSWORD})
    assert login.status == 200
    csrf = (await login.json())["csrf"]

    async def call(method, route, data=None, status=200, **kwargs):
        headers = {"X-CSRF-Token": csrf, **kwargs.pop("headers", {})}
        reply = await client.request(method, route, json=data, headers=headers, **kwargs)
        value = await reply.json()
        assert reply.status in (status if isinstance(status, tuple) else (status,)), (route, reply.status, value)
        return value

    try:
        yield client, call, crm, store, controller, advisor, materials, clock, before
    finally:
        await client.close()
        await materials.close()
        crm.close()
        store.close()


async def current_project(call, unit_id, project_id):
    return next(item for item in (await call("GET", f"/api/customers/{unit_id}/opportunities?include_archived=true"))["items"] if item["id"] == project_id)


async def adopt_move(call, thread, *, text="针对当前资料，给我具体的下一步", request_id="move-message"):
    reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": text, "request_id": request_id})
    ai = last_assistant(reply)
    record = (await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{ai['id']}/actions/1/adopt", {}))["record"]
    return reply, record


async def terms(call, record, **values):
    current = (await call("GET", f"/api/records/{record['id']}"))["record"]
    return (await call("PATCH", f"/api/records/{record['id']}/terms", {"expected_updated_at": current["terms_updated_at"], **values}))["record"]


def test_trial_same_name_mult_project_latest_direct_contact_and_action_alignment(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            unit = await customer(call, "深度同名多项目单位")
            tech = await contact(call, unit["id"], department="技术部")
            buyer = await contact(call, unit["id"], department="采购部")
            p1 = await member(call, await project(call, unit["id"], "数据加密试点"), tech, roles=["technical_reviewer"])
            p2 = await member(call, await project(call, unit["id"], "密钥托管采购"), tech, roles=["user"])
            a = await capture(call, {"contact_id": tech["id"], "opportunity_id": p1["id"]}, "技术王工上次要求核对数据库性能。",
                request_id="multi-a", occurred_at=clock() - 7200)
            b = await capture(call, {"contact_id": tech["id"], "opportunity_id": p2["id"]}, "技术王工今天要求先了解密钥操作界面。",
                request_id="multi-b", occurred_at=clock() - 3600)
            await capture(call, {"contact_id": tech["id"], "opportunity_id": p1["id"]}, "别人谈到技术王工，但他未出席。", request_id="multi-about",
                occurred_at=clock() - 1800, contact_relations=[{"contact_id": tech["id"], "relation": "about"}])
            await capture(call, {"contact_id": buyer["id"]}, "采购部同名王工的独有预算口径。", request_id="multi-other", occurred_at=clock() - 900)
            t1 = await discuss(call, unit["id"], contact_id=tech["id"], opportunity_id=p1["id"], request_id="multi-t1")
            _, action1 = await adopt_move(call, t1, request_id="multi-m1")
            t2 = await discuss(call, unit["id"], contact_id=tech["id"], opportunity_id=p2["id"], request_id="multi-t2")
            _, action2 = await adopt_move(call, t2, request_id="multi-m2")
            await terms(call, action1, executor_kind="self", executor_evidence="我明确负责核对性能")
            await terms(call, action2, executor_kind="customer", executor_evidence="王工明确说由他反馈界面问题")
            all_view = await call("GET", f"/api/timeline?contact_id={tech['id']}")
            assert all_view["summary"]["latest_communication"]["key"] == b["event"]["key"]
            first = await call("GET", f"/api/timeline?contact_id={tech['id']}&opportunity_id={p1['id']}")
            second = await call("GET", f"/api/timeline?contact_id={tech['id']}&opportunity_id={p2['id']}")
            assert first["summary"]["latest_communication"]["key"] == a["event"]["key"]
            assert {item["id"] for item in first["summary"]["open_actions"]} == {action1["id"]}
            assert {item["id"] for item in second["summary"]["waiting"]} == {action2["id"]}
            assert not (await call("GET", f"/api/timeline?contact_id={buyer['id']}"))["summary"]["open_actions"]
            followup = await discuss(call, unit["id"], contact_id=tech["id"], opportunity_id=p1["id"], request_id="multi-review")
            await call("POST", f"/api/sales-discussions/{followup['id']}/messages", {"text": "只谈数据库性能，不谈密钥采购", "request_id": "multi-review-message"})
            serialized = json.dumps(advisor.calls[-1]["context"], ensure_ascii=False)
            assert "数据库性能" in serialized and "密钥操作界面" not in serialized and "采购部同名王工" not in serialized
            assert {item["id"] for item in advisor.calls[-1]["context"]["timeline"]["open_actions"]} == {action1["id"]}
    asyncio.run(run())


def test_trial_responsibility_check_deadline_and_execution_stay_distinct(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, advisor, _, clock, _):
            unit = await customer(call, "责任时间合成单位")
            person = await contact(call, unit["id"], department="业务部")
            await capture(call, {"contact_id": person["id"]}, "我发方案，王工给范围，团队准备演示；检查反馈不是代客户执行。", request_id="roles-source")
            actions = {}
            day = datetime.fromtimestamp(clock(), SHANGHAI).date()
            for kind, title in (("self", "我提交部署方案"), ("customer", "等待王工补充系统范围"), ("team", "团队准备验证环境")):
                advisor.moves[0]["title"] = title
                thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="roles-t-"+kind)
                _, action = await adopt_move(call, thread, request_id="roles-m-"+kind)
                actions[kind] = await terms(call, action, executor_kind=kind, executor_evidence="人工核对责任人",
                    check_date=(day+timedelta(days=1)).isoformat(), check_evidence="明天我检查进展",
                    deadline_date=(day+timedelta(days=3)).isoformat(), deadline_evidence="三天后为材料截止")
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert {item["id"] for item in view["summary"]["waiting"]} == {actions["customer"]["id"], actions["team"]["id"]}
            agenda = await call("GET", "/api/agenda?period=month&date="+day.isoformat())
            assert len(agenda["planning_nodes"]) == 6
            assert all(node["date_only"] and node["at"] is None and not node["reminder_active"] for node in agenda["planning_nodes"])
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
            advisor.moves[0]["title"] = "核对责任与材料进度"
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="roles-summary")
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "我明天应该做什么，哪些只需要检查？", "request_id": "roles-summary-message"})
            context = advisor.calls[-1]["context"]["timeline"]
            assert {item["executor_kind"] for item in context["open_actions"]} == {"self", "customer", "team"}
            assert (day+timedelta(days=1)).isoformat() in json.dumps(context)
            assert (day+timedelta(days=3)).isoformat() in json.dumps(context)
    asyncio.run(run())


def test_trial_long_record_tail_change_reaches_advisor_and_original_stays_complete(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            unit = await customer(call, "长会谈合成单位")
            person = await contact(call, unit["id"], department="技术部")
            ending = "最后撤回：客户明确说之前方案不要再发送，项目暂停，等待审批通知。"
            original = "会谈开头曾要求发送部署方案。\n" + "这里是一般技术讨论，介绍系统现状与界面细节。" * 550 + "\n" + ending
            assert 10_000 < len(original) < 20_000
            saved = await capture(call, {"contact_id": person["id"]}, original, request_id="long-tail", occurred_at=clock()-3600)
            detail = await call("GET", f"/api/records/{saved['record']['id']}")
            assert detail["record"]["content"] == original and detail["record"]["original_content"] == original
            thread = await discuss(call, unit["id"], contact_id=person["id"], event_keys=[saved["event"]["key"]], request_id="long-thread")
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "基于这场交流，当前还有什么承诺应落实？", "request_id": "long-message"})
            evidence = next(item for item in advisor.calls[-1]["context"]["timeline"]["events"] if item["key"] == saved["event"]["key"])
            assert "项目暂停" in evidence["text"] and "方案不要再发送" in evidence["text"]
            assert evidence.get("text_truncated") is True or "节选" in evidence["text"] or "截取" in evidence["text"]
            assert len(json.dumps(advisor.calls[-1]["context"], ensure_ascii=False)) <= 32_000
            assert (await event(call, saved["event"]["key"]))["text"] == original
    asyncio.run(run())


def test_trial_all_projects_ai_marks_archived_history_with_actual_unit_and_project(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            unit = await customer(call, "归档背景合成单位")
            person = await contact(call, unit["id"], department="技术部")
            old = await member(call, await project(call, unit["id"], "已结束的旧加密方案"), person, roles=["technical_reviewer"])
            current = await member(call, await project(call, unit["id"], "仍在推进的密钥方案"), person, roles=["user"])
            old_note = await capture(call, {"contact_id": person["id"], "opportunity_id": old["id"]},
                "旧加密项目当时讨论了材料承诺，这属于过去项目。", request_id="archive-old", occurred_at=clock()-7*86400)
            new_note = await capture(call, {"contact_id": person["id"], "opportunity_id": current["id"]},
                "当前密钥项目仅需核对界面，不复用旧加密方案金额与承诺。", request_id="archive-current", occurred_at=clock()-3600)
            old_thread = await discuss(call, unit["id"], contact_id=person["id"], opportunity_id=old["id"], request_id="archive-old-thread")
            _, old_action = await adopt_move(call, old_thread, request_id="archive-old-message")
            old = await current_project(call, unit["id"], old["id"])
            await call("PATCH", f"/api/customers/{unit['id']}/opportunities/{old['id']}", {"expected_revision": old["revision"], "archived": True})
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert {old_note["event"]["key"], new_note["event"]["key"]} <= {item["key"] for item in view["items"]}
            thread = await discuss(call, unit["id"], contact_id=person["id"], event_keys=[], request_id="archive-all-thread")
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "最近该怎么跟进王工，请区分项目。", "request_id": "archive-all-message"})
            context = advisor.calls[-1]["context"]["timeline"]
            old_evidence = next(item for item in context["events"] if item["key"] == old_note["event"]["key"])
            new_evidence = next(item for item in context["events"] if item["key"] == new_note["event"]["key"])
            assert old_evidence["customer_name"] == unit["name"]
            assert old_evidence["opportunity_id"] == old["id"] and old_evidence["opportunity_name"] == old["name"]
            assert old_evidence["opportunity_archived"] is True and old_evidence.get("nature")
            assert new_evidence["opportunity_id"] == current["id"] and new_evidence["opportunity_archived"] is False
            old_open = next((item for item in context["open_actions"] if item["id"] == old_action["id"]), None)
            assert old_open is None or old_open.get("opportunity_archived") is True
    asyncio.run(run())


def test_trial_source_customer_reassignment_then_new_focus_discussion_does_not_mix_old_person(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            first = await customer(call, "第一次误选的合成单位")
            second = await customer(call, "真正交流的合成单位")
            wrong = await contact(call, first["id"], department="旧单位技术部")
            right = await contact(call, second["id"], department="正确单位技术部")
            source = await capture(call, {"contact_id": wrong["id"]}, "王工要求补充上线范围，后来核对是第二个单位的王工。", request_id="reassign-source")
            prior = await discuss(call, first["id"], contact_id=wrong["id"], event_keys=[source["event"]["key"]], request_id="reassign-prior")
            prior_reply = await call("POST", f"/api/sales-discussions/{prior['id']}/messages", {"text": "按现有归属讨论一下", "request_id": "reassign-prior-message"})
            clock.value += 30
            await call("PATCH", f"/api/records/{source['record']['id']}", {"customer_id": second["id"], "expected_updated_at": source["record"]["updated_at"]})
            assert last_assistant(await call("GET", f"/api/sales-discussions/{prior['id']}"))["stale"]
            await call("POST", f"/api/sales-discussions/{prior['id']}/messages/{last_assistant(prior_reply)['id']}/actions/1/adopt", {}, (400,409))
            changed = await event(call, source["event"]["key"])
            await context_change(call, changed, contact_relations=[{"contact_id": right["id"], "relation": "direct"}])
            assert source["event"]["key"] not in {item["key"] for item in (await call("GET", f"/api/timeline?contact_id={wrong['id']}"))["items"]}
            new = await discuss(call, second["id"], contact_id=right["id"], event_keys=[source["event"]["key"]], request_id="reassign-new")
            await call("POST", f"/api/sales-discussions/{new['id']}/messages", {"text": "这次以正确对象继续推进", "request_id": "reassign-new-message"})
            context = advisor.calls[-1]["context"]
            assert context["timeline"]["scope"]["id"] == right["id"] and "上线范围" in json.dumps(context, ensure_ascii=False)
            assert "旧单位技术部" not in json.dumps(context, ensure_ascii=False)
            assert not any(item["key"] == f"discussion:{prior['id']}" for item in context["timeline"]["events"])
    asyncio.run(run())


def test_trial_halfway_context_save_conflict_recovers_without_losing_source_or_old_history(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, _, _, clock, _):
            unit = await customer(call, "半途失败合成单位")
            person = await contact(call, unit["id"], department="业务部")
            exact = "现场原话第一行\n第二行包含具体材料名称，不能被确认窗口失败覆盖。"
            saved = await capture(call, {"contact_id": person["id"]}, exact, request_id="halfway-source")
            first = await event(call, saved["event"]["key"])
            other_tab = await context_change(call, first, occurred_at=clock()-86400)
            own_draft = {"expected_revision": first["revision"], "kind": "reflection", "occurred_at": clock()-3600}
            await call("POST", f"/api/timeline/events/{first['key']}/context", own_draft, 409)
            assert (await event(call, first["key"]))["kind"] == "communication"
            assert crm.get_record("me", saved["record"]["id"])["content"] == exact
            own_draft["expected_revision"] = other_tab["revision"]
            recovered = (await call("POST", f"/api/timeline/events/{first['key']}/context", own_draft))["event"]
            assert recovered["kind"] == "reflection" and recovered["occurred_at"] == clock()-3600
            assert len(recovered["context_history"]) >= 3
            assert crm.get_record("me", saved["record"]["id"])["content"] == exact
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 1
    asyncio.run(run())


def test_trial_calendar_cancel_and_calendar_complete_do_not_complete_person_matter(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, advisor, _, clock, _):
            unit = await customer(call, "日程事项分开合成单位")
            person = await contact(call, unit["id"], department="业务部")
            await capture(call, {"contact_id": person["id"]}, "我核对材料，预约被取消也不能忘记这件事。", request_id="calendar-source")
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="calendar-thread")
            _, action = await adopt_move(call, thread, request_id="calendar-move")
            action = await terms(call, action, executor_kind="self", execution_at=clock()+3600, execution_evidence="人工拟定一小时后核对")
            for outcome in ("cancelled", "completed"):
                calendar_seen = await call("GET", f"/api/records/{action['id']}")
                assert len(calendar_seen['schedule_snapshot']) == 64
                scheduled = await call("POST", f"/api/records/{action['id']}/schedule", {"remind_at": clock()+3600, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
                confirmed = await call("POST", f"/api/records/{action['id']}/confirm",
                    {"proposal_id": scheduled["proposal"]["id"], "updated_at": scheduled["proposal"]["updated_at"]})
                if outcome == "cancelled":
                    calendar_seen = await call("GET", f"/api/records/{action['id']}")
                    assert len(calendar_seen['schedule_snapshot']) == 64
                    await call("POST", f"/api/records/{action['id']}/cancel", {"expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
                else:
                    await call("POST", f"/api/tasks/{confirmed['task']['id']}/complete", {})
                view = await call("GET", f"/api/timeline?contact_id={person['id']}")
                active = next(item for item in view["summary"]["open_actions"] if item["id"] == action["id"])
                assert active["status"] != "done" and active["task_status"] == outcome
                review = await discuss(call, unit["id"], contact_id=person["id"], request_id="calendar-review-"+outcome)
                await call("POST", f"/api/sales-discussions/{review['id']}/messages",
                    {"text": "日程处理后，这个事项到底还有没有需要落实的？", "request_id": "calendar-review-msg-"+outcome})
                context_action = next(item for item in advisor.calls[-1]["context"]["timeline"]["open_actions"] if item["id"] == action["id"])
                assert context_action["status"] != "done" and context_action["task_status"] == outcome
                assert not context_action.get("reminder_active", False)
            assert crm._db.execute("SELECT COUNT(*) FROM crm_action_outcomes").fetchone()[0] == 0
    asyncio.run(run())


def test_trial_archived_project_replays_saved_request_and_restores_same_history(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, _, _, clock, _):
            unit = await customer(call, "归档恢复合成单位")
            person = await contact(call, unit["id"], department="业务部")
            p = await member(call, await project(call, unit["id"], "先暂停后恢复的项目"), person, roles=["business_owner"])
            payload = {"contact_id": person["id"], "opportunity_id": p["id"], "request_id": "archive-replay-source",
                "text": "本项目已保存的明确交流原话。", "kind": "communication", "occurred_at": clock()-3600}
            saved = await call("POST", "/api/timeline/records", payload, 201)
            p = await current_project(call, unit["id"], p["id"])
            p = (await call("PATCH", f"/api/customers/{unit['id']}/opportunities/{p['id']}", {"expected_revision": p["revision"], "archived": True}))["opportunity"]
            replay = await call("POST", "/api/timeline/records", payload)
            assert not replay["created"] and replay["record"]["id"] == saved["record"]["id"]
            await call("POST", "/api/timeline/records", {**payload, "request_id": "archive-new-source"}, 400)
            p = (await call("PATCH", f"/api/customers/{unit['id']}/opportunities/{p['id']}", {"expected_revision": p["revision"], "archived": False}))["opportunity"]
            view = await call("GET", f"/api/timeline?contact_id={person['id']}&opportunity_id={p['id']}")
            assert [item["key"] for item in view["items"]] == [saved["event"]["key"]]
            thread = await discuss(call, unit["id"], contact_id=person["id"], opportunity_id=p["id"], request_id="archive-restored-discussion")
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "恢复后接着上次交流推进", "request_id": "archive-restored-message"})
            assert not last_assistant(reply)["stale"]
            assert crm._db.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 1
    asyncio.run(run())


def test_trial_visit_about_override_survives_child_direct_and_recap_separation(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, _, materials, clock, _):
            unit = await customer(call, "旧会谈关系核对合成单位")
            person = await contact(call, unit["id"], department="技术部")
            visit = (await call("POST", "/api/visits", {"title": "历史会谈", "customer_id": unit["id"], "occurred_at": clock()-86400}, 201))["visit"]
            material = (await call("POST", f"/api/visits/{visit['id']}/materials", {"role": "recap", "provider": "manual",
                "title": "历史复盘材料", "text": "王工被提及，初次错误勾为直接参加，实际不在场。"}, 202))["material"]
            await materials.process_one()
            child = await event(call, f"material:{material['id']}")
            await context_change(call, child, contact_relations=[{"contact_id": person["id"], "relation": "direct"}])
            parent = await event(call, f"visit:{visit['id']}")
            await context_change(call, parent, contact_relations=[{"contact_id": person["id"], "relation": "about"}])
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert view["summary"]["latest_communication"] is None
            assert next(item for item in view["items"][0]["contact_relations"] if item["contact_id"] == person["id"])["relation"] == "about"
            child = await event(call, child["key"])
            await context_change(call, child, separate_event=True, kind="reflection", occurred_at=clock()-3600,
                related_event_key=parent["key"], contact_relations=[{"contact_id": person["id"], "relation": "about"}])
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert len(view["items"]) == 2 and view["summary"]["latest_communication"] is None
            parent = await event(call, parent["key"])
            assert parent["occurred_at"] == clock()-86400
    asyncio.run(run())


def test_trial_old_unfinished_promise_survives_many_recent_fragments(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            unit = await customer(call, "旧承诺保留合成单位")
            person = await contact(call, unit["id"], department="技术部")
            promise = "还欠王工一份先前承诺的验收指标清单。"
            await capture(call, {"contact_id": person["id"]}, promise, request_id="backlog-old", occurred_at=clock()-20*86400)
            advisor.moves[0].update(title="兑现先前承诺的验收指标清单", reason=promise)
            earlier = await discuss(call, unit["id"], contact_id=person["id"], request_id="backlog-adopt")
            _, action = await adopt_move(call, earlier, request_id="backlog-adopt-message")
            for index in range(18):
                await capture(call, {"contact_id": person["id"]}, f"最近独立想法{index}，关于王工的下一次沟通方式。", kind="reflection",
                    request_id=f"backlog-fragment-{index}", occurred_at=clock()-index*60)
            summary = (await call("GET", f"/api/timeline?contact_id={person['id']}&page_size=5"))["summary"]
            assert any(item["id"] == action["id"] for item in summary["open_actions"])
            final = await discuss(call, unit["id"], contact_id=person["id"], request_id="backlog-review")
            await call("POST", f"/api/sales-discussions/{final['id']}/messages", {"text": "最近想法很多，我是不是还有旧事没落实？", "request_id": "backlog-review-message"})
            context = advisor.calls[-1]["context"]
            assert any(item["id"] == action["id"] for item in context["timeline"]["open_actions"])
            assert "验收指标清单" in json.dumps(context, ensure_ascii=False)
            assert len(json.dumps(context, ensure_ascii=False)) <= 32_000
    asyncio.run(run())


def test_trial_midnight_dates_discussion_recent_message_and_unknown_time_are_distinct(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, _, _, clock, _):
            unit = await customer(call, "午夜时序合成单位")
            person = await contact(call, unit["id"])
            before_midnight = datetime.fromisoformat("2026-10-02T23:58:00+08:00").timestamp()
            after_midnight = datetime.fromisoformat("2026-10-03T00:02:00+08:00").timestamp()
            previous = await capture(call, {"contact_id": person["id"]}, "昨晚实际沟通。", request_id="dates-before", occurred_at=before_midnight)
            current = await capture(call, {"contact_id": person["id"]}, "今日凌晨实际沟通。", request_id="dates-after", occurred_at=after_midnight)
            unknown = await capture(call, {"contact_id": person["id"]}, "时间不明确的想法。", kind="reflection", request_id="dates-unknown")
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="dates-thread")
            first = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "先问一次", "request_id": "dates-first"})
            clock.value += 60
            second = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "再补充一个问题", "request_id": "dates-second"})
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            discussion = next(item for item in view["items"] if item["key"] == f"discussion:{thread['id']}")
            assert discussion["last_activity_at"] == clock() and discussion["recorded_at"] == NOW
            assert len([item for item in view["items"] if item["kind"] == "discussion"]) == 1
            assert len(first["messages"]) == 2 and len(second["messages"]) == 4
            assert (await event(call, unknown["event"]["key"]))["occurred_at"] is None
            known = [item for item in view["items"] if item["key"] in (previous["event"]["key"], current["event"]["key"])]
            assert [item["key"] for item in known] == [current["event"]["key"], previous["event"]["key"]]
            assert datetime.fromtimestamp(known[0]["occurred_at"], SHANGHAI).date().isoformat() == "2026-10-03"
            assert datetime.fromtimestamp(known[1]["occurred_at"], SHANGHAI).date().isoformat() == "2026-10-02"
            assert (await event(call, previous["event"]["key"]))["recorded_at"] == NOW
    asyncio.run(run())


def test_trial_preexisting_sources_upgrade_without_editing_content_identity_or_dates(tmp_path):
    def seed(crm, store, clock):
        unit = crm.create_customer("me", {"name": "旧有内容保全合成单位", "notes": "原客户说明不能被初始化改写"}, clock()-10*86400)
        person = crm.create_contact("me", unit["id"], {"name": "王工", "department": "技术部", "role": "原职务"}, clock()-9*86400)
        one = crm.create_record("me", {"title": "原有客户会谈", "content": "原有原话与换行\n客户过去表达的边界。", "customer_id": unit["id"], "kind": "note", "category": "conversation"}, clock()-8*86400)
        two = crm.create_record("me", {"title": "原有个人判断", "content": "原有思考不是客户承诺。", "customer_id": unit["id"], "kind": "note", "category": "visit_review"}, clock()-7*86400)
        ids = [one["id"], two["id"]]
        return {"unit": unit, "person": person, "ids": ids,
            "records": [dict(crm._db.execute("SELECT * FROM crm_records WHERE id=?", (identifier,)).fetchone()) for identifier in ids],
            "customers": [dict(row) for row in crm._db.execute("SELECT * FROM crm_customers")],
            "contacts": [dict(row) for row in crm._db.execute("SELECT * FROM crm_contacts")]}

    async def run():
        async with trial_app(tmp_path, seed=seed) as (_, call, crm, _, controller, _, _, _, before):
            view = await call("GET", f"/api/timeline?customer_id={before['unit']['id']}")
            assert {item["key"] for item in view["items"]} == {f"record:{identifier}" for identifier in before["ids"]}
            assert (await call("GET", f"/api/timeline?contact_id={before['person']['id']}"))["items"] == []
            controller.timeline.view("me", {"customer_id": before["unit"]["id"]})
            after_records = [dict(crm._db.execute("SELECT * FROM crm_records WHERE id=?", (identifier,)).fetchone()) for identifier in before["ids"]]
            assert after_records == before["records"]
            assert [dict(row) for row in crm._db.execute("SELECT * FROM crm_customers")] == before["customers"]
            assert [dict(row) for row in crm._db.execute("SELECT * FROM crm_contacts")] == before["contacts"]
            assert crm._db.execute("SELECT COUNT(*) FROM crm_timeline_contexts").fetchone()[0] == 0
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())


def test_trial_withdrawal_feedback_closes_old_action_and_enters_next_discussion_without_rewriting_promise(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, advisor, _, clock, _):
            unit = await customer(call, "撤回进展合成单位")
            person = await contact(call, unit["id"], department="业务部")
            original = "原交流中我答应给王工发送部署方案。"
            source = await capture(call, {"contact_id": person["id"]}, original, request_id="withdraw-source", occurred_at=clock()-86400)
            advisor.moves[0].update(title="兑现原定发送部署方案", reason=original)
            thread = await discuss(call, unit["id"], contact_id=person["id"], request_id="withdraw-first")
            _, action = await adopt_move(call, thread, request_id="withdraw-first-message")
            calendar_seen = await call("GET", f"/api/records/{action['id']}")
            assert len(calendar_seen['schedule_snapshot']) == 64
            scheduled = await call("POST", f"/api/records/{action['id']}/schedule", {"remind_at": clock()+3600, "duration_minutes": 30, "expected_schedule_snapshot": calendar_seen['schedule_snapshot']})
            confirmed = await call("POST", f"/api/records/{action['id']}/confirm",
                {"proposal_id": scheduled["proposal"]["id"], "updated_at": scheduled["proposal"]["updated_at"]})
            detail = await call("GET", f"/api/records/{action['id']}")
            feedback = {"request_id": "withdraw-feedback", "result": "经王工明确反馈，此方案暂不需要；原发送行动撤回，不再执行。",
                "expected_snapshot": detail["completion_snapshot"]}
            closed = await call("POST", f"/api/records/{action['id']}/complete-outcome", feedback)
            replay = await call("POST", f"/api/records/{action['id']}/complete-outcome", feedback)
            assert closed["outcome"]["id"] == replay["outcome"]["id"]
            assert crm.get_task("me", confirmed["task"]["id"])["status"] != "pending"
            view = await call("GET", f"/api/timeline?contact_id={person['id']}")
            assert not any(item["id"] == action["id"] for item in view["summary"]["open_actions"])
            assert (await call("GET", f"/api/records/{source['record']['id']}"))["record"]["content"] == original
            review = await discuss(call, unit["id"], contact_id=person["id"], request_id="withdraw-review")
            await call("POST", f"/api/sales-discussions/{review['id']}/messages", {"text": "这项发送承诺现在还需要落实吗？", "request_id": "withdraw-review-message"})
            context = advisor.calls[-1]["context"]["timeline"]
            assert "原发送行动撤回，不再执行" in json.dumps(context, ensure_ascii=False)
            assert all(item["id"] != action["id"] for item in context["open_actions"])
            assert len([item for item in context["events"] if item["key"] == f"outcome:{closed['outcome']['id']}"]) == 1
    asyncio.run(run())


def test_trial_large_recording_material_two_excerpt_layers_preserve_final_deferral(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, crm, _, _, advisor, materials, clock, _):
            unit = await customer(call, "大型录音合成单位")
            person = await contact(call, unit["id"], department="技术部")
            ending = "录音最后明确更正：原定验证延期，不按原日期推进，等王工补齐审批材料再联系。"
            original = "录音开头曾约定开展验证。\n" + "一般背景材料与系统现状说明，不是新的承诺。"*2200 + "\n"+ending
            assert len(original) > 40_000
            visit = (await call("POST", "/api/visits", {"title": "一小时现场录音样例", "customer_id": unit["id"], "occurred_at": clock()-3600}, 201))["visit"]
            source = (await call("POST", f"/api/visits/{visit['id']}/materials", {"role": "recording", "provider": "manual",
                "title": "模拟聆记转写长资料", "text": original}, 202))["material"]
            await materials.process_one()
            detail = await call("GET", f"/api/materials/{source['id']}")
            assert detail["text"] == original
            grouped = await event(call, f"visit:{visit['id']}")
            assert grouped["text_truncated"] and "原定验证延期" in grouped["text"]
            await context_change(call, grouped, contact_relations=[{"contact_id": person["id"], "relation": "direct"}])
            thread = await discuss(call, unit["id"], contact_id=person["id"], event_keys=[grouped["key"]], request_id="large-recording-discussion")
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "原定验证还要按原日期执行吗？", "request_id": "large-recording-message"})
            evidence = next(item for item in advisor.calls[-1]["context"]["timeline"]["events"] if item["key"] == grouped["key"])
            assert "原定验证延期" in evidence["text"] and "不按原日期推进" in evidence["text"]
            assert evidence["text_truncated"] is True
            assert (await call("GET", f"/api/materials/{source['id']}"))["text"] == original
            assert schedule_counts(crm) == {"tasks": 0, "proposals": 0, "notifications": 0}
    asyncio.run(run())
