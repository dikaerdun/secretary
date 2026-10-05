"""Independent HTTP journey trials: fresh synthetic data, fixed offline providers."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.profile_intelligence import _rule_extract
from secretary.store import Store
from secretary.web import create_app, hash_password


NOW = 1_800_000_000.0
PASSWORD = "synthetic-account-trial-password"


class Analyzer:
    def __init__(self):
        self.calls = []

    async def extract(self, source, context):
        self.calls.append(copy.deepcopy({"source": source, "context": context}))
        return _rule_extract(source, context)


class Researcher:
    def __init__(self):
        self.calls = []

    async def research(self, context):
        self.calls.append(copy.deepcopy(context))
        name = context["customer"]["name"]
        return [{"url": "https://official.example/old-project", "title": name+"公开旧资料",
                 "text": name+"：行业是金融。预算50万元尚未审批。", "entity_name": name,
                 "published_at": "2021-03-01", "fetched_at": NOW}]


class Advisor:
    def __init__(self):
        self.calls = []

    async def reply(self, context, history, text, now):
        self.calls.append(copy.deepcopy(context))
        return {"answer": "先核实采购步骤及审批材料，再安排验证。",
                "next_moves": [{"title": "核实审批材料", "reason": "预算尚未审批",
                                "contact_hint": "王工", "preparation": "准备一页材料", "success_signal": "明确审批步骤"}],
                "questions": ["谁确认预算材料？"], "risks": ["尚未批准预算"]}


@asynccontextmanager
async def app_context(tmp_path):
    path = tmp_path / "http-account-intelligence-synthetic.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    analyzer, researcher = Analyzer(), Researcher()
    app = create_app(store, crm, asyncio.Lock(), "me", hash_password(PASSWORD),
                     clock=lambda: NOW, profile_analyzer=analyzer, public_researcher=researcher)
    controller = app.middlewares[0].__self__
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    login = await client.post("/api/login", json={"password": PASSWORD})
    assert login.status == 200
    csrf = (await login.json())["csrf"]

    async def call(method, route, data=None, status=200, **kwargs):
        headers = {"X-CSRF-Token": csrf, **kwargs.pop("headers", {})}
        result = await client.request(method, route, json=data, headers=headers, **kwargs)
        value = await result.json()
        assert result.status == status, (route, result.status, value)
        return value

    try:
        yield client, call, crm, store, controller, analyzer, researcher
    finally:
        await client.close()
        crm.close()
        store.close()


async def customer(call, name, **values):
    return (await call("POST", "/api/customers", {"name": name, **values}, 201))["customer"]


async def contact(call, customer_id, name, **values):
    return (await call("POST", f"/api/customers/{customer_id}/contacts", {"name": name, **values}, 201))["contact"]


async def project(call, customer_id, name, **values):
    return (await call("POST", f"/api/customers/{customer_id}/opportunities", {"name": name, **values}, 201))["opportunity"]


async def current_project(call, customer_id, project_id):
    items = (await call("GET", f"/api/customers/{customer_id}/opportunities?include_archived=true"))["items"]
    return next(item for item in items if item["id"] == project_id)


async def member(call, project, person, **values):
    return (await call("POST", f"/api/customers/{project['customer_id']}/opportunities/{project['id']}/stakeholders",
                       {"contact_id": person["id"], "expected_revision": project["revision"], **values}))["relationship"]


async def candidate(call, item, **values):
    return (await call("POST", f"/api/profile-suggestions/{item['id']}",
                       {"decision": "confirm", "expected_revision": item["revision"], **values}))["candidate"]


def no_schedule(crm):
    for table in ("tasks", "proposals", "notifications"):
        assert crm._db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] == 0


def test_s1_http_unit_people_two_projects_and_names_in_departments(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            unit = await customer(call, "合成城市银行", unit_type="institution", contact="初始主联系人")
            a = await contact(call, unit["id"], "王工", role="技术经理", department="信息技术部")
            b = await contact(call, unit["id"], "王工", role="采购经理", department="采购部")
            c = await contact(call, unit["id"], "李总", role="负责人", department="分管领导")
            first = await project(call, unit["id"], "数据库加密")
            second = await project(call, unit["id"], "密钥管理")
            first = await member(call, first, a, roles=["technical_reviewer", "final_approver"], stance="supportive", evidence="本项目职责由本人说明")
            first = await member(call, first, c, roles=["final_approver"], basis="observation", evidence="现场主持会议，权限待核实")
            second = await member(call, second, a, roles=["user"], stance="neutral", concerns="操作简单")
            second = await member(call, second, b, roles=["procurement"])
            assert [item["roles"] for item in first["stakeholders"]] == [["final_approver", "technical_reviewer"], ["final_approver"]]
            assert first["stakeholders"][0]["contact_department"] == "信息技术部"
            reverse = await call("GET", f"/api/contacts/{a['id']}/projects")
            assert {item["opportunity_id"]: item["roles"] for item in reverse["items"]} == {first["id"]: ["final_approver", "technical_reviewer"], second["id"]: ["user"]}
            profile = await call("GET", f"/api/customers/{unit['id']}/profile")
            assert {(person["name"], person["department"]) for person in profile["contacts"] if person["name"] == "王工"} == {("王工", "信息技术部"), ("王工", "采购部")}
            assert next(person for person in profile["contacts"] if person["id"] == a["id"])["role"] == "技术经理"
            assert await current_project(call, unit["id"], second["id"]) == second
            units = await call("GET", "/api/units")
            assert isinstance(units["items"], list) and units["items"][0]["id"] == unit["id"]
            assert units["total"] == 1
            network = await call("GET", f"/api/customers/{unit['id']}/account-network")
            assert network["contact_count"] == 4 and network["project_count"] == 2
            no_schedule(crm)
    asyncio.run(run())


def test_http_source_correction_stales_old_candidate_and_recreates_from_new_quote(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            unit = await customer(call, "原话变更测试单位")
            p = await project(call, unit["id"], "正式需求项目")
            record = (await call("POST", "/api/records", {"title": "第一次交流", "content": "预算30万元尚未审批。", "customer_id": unit["id"], "kind": "note"}, 201))["record"]
            await call("POST", "/api/opportunity-links", {"entity_type": "record", "entity_id": record["id"], "opportunity_id": p["id"]})
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            old = next(item for item in (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"] if item["key"] == "budget_notes")
            changed = (await call("PATCH", f"/api/records/{record['id']}", {"content": "预算改为40万元，尚未审批。", "expected_updated_at": record["updated_at"]}))["record"]
            stale = (await call("GET", f"/api/profile-suggestions/{old['id']}"))["candidate"]
            assert stale["status"] == "stale"
            await call("POST", f"/api/profile-suggestions/{old['id']}", {"decision": "confirm", "expected_revision": stale["revision"]}, 409)
            assert (await call("GET", f"/api/records/{record['id']}"))["record"]["content"] == changed["content"]
            await call("POST", "/api/opportunity-links", {"entity_type": "record", "entity_id": record["id"], "opportunity_id": p["id"]})
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            fresh = next(item for item in (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"] if item["key"] == "budget_notes")
            assert fresh["id"] != old["id"] and "40万元" in fresh["source"]["quote"]
            await candidate(call, fresh)
            facts = (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={p['id']}"))["project_facts"]["items"]
            assert next(fact for fact in facts if fact["key"] == "budget_notes")["value"] == "预算改为40万元，尚未审批"
            no_schedule(crm)
    asyncio.run(run())


def test_s2_http_tree_explicit_project_units_cas_archive_restore(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            group = await customer(call, "合成集团", unit_type="group")
            child = await customer(call, "合成子公司", unit_type="company", parent_customer_id=group["id"])
            sibling = await customer(call, "兄弟公司", unit_type="company", parent_customer_id=group["id"])
            external = await customer(call, "外部测评机构", unit_type="institution")
            buyer = await contact(call, group["id"], "集团采购", department="采购中心")
            tech = await contact(call, sibling["id"], "兄弟技术", department="信息部")
            expert = await contact(call, external["id"], "测评专家")
            p = await project(call, child["id"], "子公司密码改造")
            p = await member(call, p, buyer, roles=["procurement"], evidence="负责本次集采")
            p = await member(call, p, tech, roles=["technical_reviewer"])
            base = f"/api/customers/{child['id']}/opportunities/{p['id']}"
            await call("POST", base+"/stakeholders", {"contact_id": expert["id"], "roles": ["security_compliance"], "expected_revision": p["revision"]}, 400)
            await call("POST", base+"/units", {"participant_customer_id": external["id"], "roles": ["technical"], "expected_revision": p["revision"]}, 400)
            p = (await call("POST", base+"/units", {"participant_customer_id": external["id"], "roles": ["technical"], "evidence": "客户明确委托该机构", "expected_revision": p["revision"]}))["relationship"]
            p = await member(call, p, expert, roles=["security_compliance"], basis="reported", evidence="参与本次测评")
            assert {item["unit_name"] for item in p["stakeholders"]} == {"合成集团", "兄弟公司", "外部测评机构"}
            await call("POST", base+"/stakeholders", {"contact_id": expert["id"], "roles": ["final_approver"], "expected_revision": p["revision"]-1}, 400)
            assert (await current_project(call, child["id"], p["id"]))["stakeholders"][-1]["roles"] == ["security_compliance"]
            p = (await call("POST", base+"/units", {"participant_customer_id": external["id"], "archived": True, "expected_revision": p["revision"]}))["relationship"]
            assert not next(person for person in p["stakeholders"] if person["contact_id"] == expert["id"])["membership_valid"]
            assert (await call("GET", base+"/units"))["total"] == 1
            assert (await call("GET", base+"/units?include_archived=true"))["total"] == 2
            p = (await call("POST", base+"/units", {"participant_customer_id": external["id"], "archived": False, "expected_revision": p["revision"]}))["relationship"]
            assert next(person for person in p["stakeholders"] if person["contact_id"] == expert["id"])["membership_valid"]
            p = (await call("POST", base+"/stakeholders", {"contact_id": buyer["id"], "archived": True, "expected_revision": p["revision"]}))["relationship"]
            assert p["stakeholders_history"][0]["roles"] == ["procurement"]
            p = (await call("POST", base+"/stakeholders", {"contact_id": buyer["id"], "archived": False, "expected_revision": p["revision"]}))["relationship"]
            root = await call("GET", f"/api/customers/{group['id']}/account-network")
            assert len(root["units"]) == 3 and root["project_count"] == 1
            own = await call("GET", f"/api/customers/{child['id']}/account-network")
            assert own["contacts"] == [] and own["project_count"] == 1
            await call("PATCH", f"/api/customers/{group['id']}/account-network", {"expected_revision": group["revision"], "parent_customer_id": child["id"]}, 400)
            await call("PATCH", f"/api/customers/{child['id']}/account-network", {"expected_revision": child["revision"], "parent_customer_id": None})
            moved = await current_project(call, child["id"], p["id"])
            assert moved["revision"] == p["revision"]+1
            assert not next(person for person in moved["stakeholders"] if person["contact_id"] == buyer["id"])["membership_valid"]
            assert next(person for person in moved["stakeholders"] if person["contact_id"] == expert["id"])["membership_valid"]
            no_schedule(crm)
    asyncio.run(run())


def test_s3_http_daily_record_candidates_confirm_profile_gap_adopt_once(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, analyzer, _):
            unit = await customer(call, "合成金融单位")
            person = await contact(call, unit["id"], "王工", department="信息部")
            p = await project(call, unit["id"], "加密试点")
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": False})
            before = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={p['id']}")
            text = "痛点是数据散落。预算30万元尚未审批。王工希望先邮件沟通。属于金融行业。"
            record = (await call("POST", "/api/records", {"title": "拜访后日常复盘", "content": text, "customer_id": unit["id"], "kind": "note"}, 201))["record"]
            await call("POST", "/api/opportunity-links", {"entity_type": "record", "entity_id": record["id"], "opportunity_id": p["id"]})
            processed = await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": False})
            assert processed["result"]["created"] >= 4 and analyzer.calls
            result = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence")
            items = {item["key"]: item for item in result["candidates"]["items"]}
            assert items["pain_points"]["scope"] == "project" and items["pain_points"]["opportunity_id"] == p["id"]
            assert items["communication_channel"]["scope"] == "contact" and items["communication_channel"]["contact_id"] == person["id"]
            assert items["industry"]["scope"] == "account"
            reviewed = await call("GET", f"/api/profile-suggestions/{items['pain_points']['id']}")
            assert reviewed["candidate"]["source"]["quote"] == "痛点是数据散落"
            await candidate(call, items["pain_points"], value="数据分散增加业务查找成本", reason="已核对原话含义")
            await candidate(call, items["industry"], value="金融")
            await candidate(call, items["communication_channel"], value="先邮件沟通")
            await call("POST", f"/api/profile-suggestions/{items['budget_notes']['id']}", {"decision": "reject", "expected_revision": items["budget_notes"]["revision"], "reason": "先保留完整审批说明"})
            await candidate(call, items["budget_approval"])
            rejected = (await call("GET", f"/api/profile-suggestions/{items['budget_notes']['id']}"))["candidate"]
            assert rejected["status"] == "rejected" and rejected["decision_history"]
            profile = await call("GET", f"/api/customers/{unit['id']}/profile")
            assert {fact["key"] for fact in profile["fields"]} == {"industry"}
            assert profile["contacts"][0]["fields"][0]["value"] == "先邮件沟通"
            after = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={p['id']}")
            assert after["plan"]["completion"]["known"] > before["plan"]["completion"]["known"]
            assert any("尚未审批" in fact["value"] for fact in after["project_facts"]["items"] if fact["key"] == "budget_approval")
            question = after["plan"]["items"][0]
            first = await call("POST", f"/api/customers/{unit['id']}/profile-questions/adopt", {"key": question["key"]})
            again = await call("POST", f"/api/customers/{unit['id']}/profile-questions/adopt", {"key": question["key"]})
            assert first["created"] and not again["created"] and first["record"]["id"] == again["record"]["id"]
            detail = await call("GET", f"/api/records/{first['record']['id']}")
            assert detail["record"]["kind"] == "action" and detail["record"]["opportunity_id"] == p["id"]
            no_schedule(crm)
    asyncio.run(run())


def test_http_login_csrf_owner_and_validation_failures_preserve_data(tmp_path):
    async def run():
        async with app_context(tmp_path) as (client, call, crm, _, controller, _, _):
            unit = await customer(call, "隔离测试单位", unit_type="company")
            person = await contact(call, unit["id"], "王工")
            p = await project(call, unit["id"], "隔离项目")
            foreign = controller.accounts.create_unit("other", {"name": "其他成员私有单位"})
            foreign_contact = crm.create_contact("other", foreign["id"], {"name": "私有人"}, NOW)
            private_source = crm.create_record("other", {"title": "私有原文", "content": "行业是金融。", "customer_id": foreign["id"], "kind": "note"}, NOW)
            await controller.profile_intelligence.scan("other", foreign["id"], force=True)
            private_item = controller.profile_intelligence.list_candidates("other")["items"][0]
            base = f"/api/customers/{unit['id']}/opportunities/{p['id']}"
            await call("POST", base+"/stakeholders", {"contact_id": person["id"], "roles": ["user"], "expected_revision": p["revision"]}, 403, headers={"X-CSRF-Token": "wrong"})
            await call("POST", base+"/stakeholders", {"contact_id": foreign_contact["id"], "roles": ["user"], "expected_revision": p["revision"]}, 404)
            await call("POST", base+"/stakeholders", {"contact_id": person["id"], "roles": ["boss"], "expected_revision": p["revision"]}, 400)
            for route in (f"/api/customers/{foreign['id']}/profile-intelligence", f"/api/customers/{foreign['id']}/account-network",
                          f"/api/contacts/{foreign_contact['id']}/projects", f"/api/profile-suggestions/{private_item['id']}"):
                await call("GET", route, status=404)
            await call("POST", f"/api/profile-suggestions/{private_item['id']}", {"decision": "confirm"}, 404)
            await call("POST", "/api/customers", {"name": "不能遗留单位", "contact": "不能遗留主联系人", "parent_customer_id": foreign["id"], "unit_type": "company"}, 404)
            assert (await call("GET", "/api/customers"))["total"] == 1
            assert await current_project(call, unit["id"], p["id"]) == p
            assert crm.get_record("other", private_source["id"])["content"] == "行业是金融。"
            await call("POST", "/api/logout", {})
            await call("GET", "/api/units", status=401)
            assert (await client.post("/api/login", json={"password": "wrong-test-password"})).status == 401
    asyncio.run(run())


def test_http_public_research_old_source_identity_and_no_internal_context(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, researcher):
            unit = await customer(call, "合成城市银行股份有限公司", notes="内部拜访秘密")
            p = await project(call, unit["id"], "当前密码项目")
            await contact(call, unit["id"], "王工", phone="13812345678")
            await call("PATCH", f"/api/customers/{unit['id']}/profile-intelligence/settings", {"auto_research": True, "official_domains": ["official.example"]})
            researched = await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/research", {"mode": "search"})
            assert researched["result"]["sources"] == 1 and researcher.calls
            assert "内部拜访秘密" not in json.dumps(researcher.calls, ensure_ascii=False)
            assert "13812345678" not in json.dumps(researcher.calls)
            result = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence")
            industry = next(item for item in result["candidates"]["items"] if item["key"] == "industry")
            budget = next(item for item in result["candidates"]["items"] if item["key"] == "budget_notes")
            assert industry["basis"] == "observation" and industry["source"]["published_at"] == "2021-03-01"
            await call("POST", f"/api/profile-suggestions/{industry['id']}", {"decision": "confirm", "expected_revision": industry["revision"]}, 400)
            await candidate(call, industry, verify_public=True)
            await call("POST", f"/api/profile-suggestions/{budget['id']}", {"decision": "confirm", "expected_revision": budget["revision"], "verify_public": True}, 400)
            await candidate(call, budget, verify_public=True, opportunity_id=p["id"])
            intel = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={p['id']}")
            assert intel["project_facts"]["items"][0]["basis"] == "observation"
            assert intel["plan"]["completion"]["known"] == 0
            unverified = await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/research", {"mode": "import", "url": "https://news.example/entity",
                "title": unit["name"]+"资料", "text": unit["name"]+"：总部位于上海。", "entity_name": unit["name"], "published_at": "2019-01-01"})
            region = next(item for item in unverified["result"]["candidates"] if item["key"] == "region")
            assert region["requires_entity_confirmation"]
            await call("POST", f"/api/profile-suggestions/{region['id']}", {"decision": "confirm", "expected_revision": region["revision"], "verify_public": True}, 400)
            await candidate(call, region, verify_public=True, verify_entity=True)
            no_schedule(crm)
    asyncio.run(run())


def test_http_model_safe_context_includes_project_relations_and_facts_no_phone_stale_adoption(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, controller, analyzer, _):
            unit = await customer(call, "合成银行", phone="400-800-8181")
            person = await contact(call, unit["id"], "王工", phone="13812345678", department="信息技术部")
            p = await project(call, unit["id"], "模型范围试点")
            p = await member(call, p, person, roles=["technical_reviewer"], engagement="direct", evidence="本人明确负责技术评审", verified_at=NOW)
            record = (await call("POST", "/api/records", {"title": "审批复盘", "content": "预算30万元尚未审批。", "customer_id": unit["id"], "kind": "note"}, 201))["record"]
            await call("POST", "/api/opportunity-links", {"entity_type": "record", "entity_id": record["id"], "opportunity_id": p["id"]})
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            items = (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"]
            approval = next(item for item in items if item["key"] == "budget_approval")
            await candidate(call, approval)
            old_public = await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/research", {
                "mode": "import", "url": "https://news.example/old-budget", "title": "合成银行旧资料",
                "text": "合成银行：预算50万元尚未审批。", "entity_name": "合成银行", "published_at": "2021-03-01"})
            old_budget = next(item for item in old_public["result"]["candidates"] if item["key"] == "budget_notes" and item["source"]["type"] == "public")
            await candidate(call, old_budget, opportunity_id=p["id"], verify_public=True, verify_entity=True)
            assert "13812345678" not in json.dumps(analyzer.calls, ensure_ascii=False)
            assert '"phone"' not in json.dumps([item["context"] for item in analyzer.calls], ensure_ascii=False)
            advisor = Advisor()
            controller.discussions.advisor = advisor
            thread = (await call("POST", "/api/sales-discussions", {"customer_id": unit["id"], "opportunity_id": p["id"], "title": "推进讨论"}, 201))["thread"]
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "下一步怎么推进？", "request_id": "safe-discussion-1"})
            context = advisor.calls[0]
            assert context["project"]["stakeholders"][0]["roles"] == ["technical_reviewer"]
            assert any(fact["key"] == "budget_approval" for fact in context["project"]["profile_facts"])
            public_fact = next(fact for fact in context["project"]["profile_facts"] if fact["key"] == "budget_notes")
            assert public_fact["basis"] == "observation"
            assert public_fact["source"]["published_at"] == "2021-03-01"
            assert public_fact["source"]["type"] == "public" and public_fact["recorded_at"] == NOW
            local_fact = next(fact for fact in context["project"]["profile_facts"] if fact["key"] == "budget_approval")
            assert local_fact["source"]["type"] == "record" and local_fact["source"]["occurred_at"] is None
            assert local_fact["source"]["recorded_at"] == NOW
            assert "13812345678" not in json.dumps(context) and "400-800-8181" not in json.dumps(context)
            assistant = next(item for item in reply["messages"] if item["role"] == "assistant")
            p = await member(call, p, person, roles=["user"], concerns="角色变更")
            refreshed = await call("GET", f"/api/sales-discussions/{thread['id']}")
            assert next(item for item in refreshed["messages"] if item["role"] == "assistant")["stale"]
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt", {}, 400)
            no_schedule(crm)
    asyncio.run(run())


def test_http_candidate_selected_scope_preview_old_value_and_concurrent_fact_guard(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            unit = await customer(call, "同名联系人测试单位")
            a = await contact(call, unit["id"], "王工", department="技术部")
            b = await contact(call, unit["id"], "王工", department="采购部")
            p = await project(call, unit["id"], "范围核对项目")
            old_profile = (await call("POST", f"/api/customers/{unit['id']}/facts", {"key": "communication_channel", "value": "先电话", "basis": "reported", "contact_id": b["id"]}))["profile"]
            old = next(person for person in old_profile["contacts"] if person["id"] == b["id"])["fields"][0]
            await call("POST", "/api/records", {"title": "同名人员偏好", "content": "王工希望先邮件沟通。", "customer_id": unit["id"], "kind": "note"}, 201)
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            item = (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"][0]
            assert item["contact_id"] is None and item["requires_contact_confirmation"]
            preview = (await call("GET", f"/api/profile-suggestions/{item['id']}?contact_id={b['id']}"))["candidate"]
            assert preview["current_value"]["value"] == "先电话" and preview["current_value"]["id"] == old["id"]
            assert preview["conflict"]
            newer_profile = (await call("POST", f"/api/customers/{unit['id']}/facts", {"key": "communication_channel", "value": "先微信", "basis": "reported", "contact_id": b["id"]}))["profile"]
            newer = next(person for person in newer_profile["contacts"] if person["id"] == b["id"])["fields"][0]
            await call("POST", f"/api/profile-suggestions/{item['id']}", {"decision": "confirm", "expected_revision": item["revision"], "contact_id": b["id"], "expected_fact_id": old["id"]}, 409)
            profile = await call("GET", f"/api/customers/{unit['id']}/profile")
            assert next(person for person in profile["contacts"] if person["id"] == b["id"])["fields"][0]["value"] == "先微信"
            assert next(person for person in profile["contacts"] if person["id"] == a["id"])["fields"] == []
            # Re-analysis creates a fresh unresolved candidate. Preview reviews the new target value.
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            pending = (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"][0]
            preview = (await call("GET", f"/api/profile-suggestions/{pending['id']}?contact_id={b['id']}"))["candidate"]
            confirmed = await candidate(call, pending, contact_id=b["id"], expected_fact_id=newer["id"], value="先邮件", reason="核对采购部王工的新偏好")
            assert confirmed["status"] == "confirmed"
            no_schedule(crm)
    asyncio.run(run())


def test_http_static_intelligence_assets_and_routes_are_served(tmp_path):
    async def run():
        async with app_context(tmp_path) as (client, _, _, _, _, _, _):
            page = await client.get("/")
            html = await page.text()
            assert "account-intelligence.js" in html and "account-intelligence.css" in html
            js = await client.get("/static/account-intelligence.js")
            css = await client.get("/static/account-intelligence.css")
            assert js.status == css.status == 200
            source = await js.text()
            assert "profile-intelligence" in source and "stakeholders" in source and "account-network" in source
            assert "no-store" in js.headers["Cache-Control"]
    asyncio.run(run())


def test_http_manual_project_fact_cas_history_and_unassigned_scope_preview(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            unit = await customer(call, "项目画像隔离单位")
            a = await project(call, unit["id"], "数据库加密")
            b = await project(call, unit["id"], "密钥管理")
            base = f"/api/customers/{unit['id']}/opportunities/{a['id']}/facts"
            initial = await call("POST", base, {"key": "budget_notes", "value": "预算20万元尚未审批", "basis": "reported", "evidence": "客户首次说明", "expected_fact_id": None})
            assert initial["created"] and initial["project_facts"]["history_total"] == 1
            await call("POST", "/api/records", {"title": "后续预算说明", "content": "预算30万元尚未审批。", "customer_id": unit["id"], "kind": "note"}, 201)
            await call("POST", f"/api/customers/{unit['id']}/profile-intelligence/scan", {"force": True})
            items = (await call("GET", f"/api/customers/{unit['id']}/profile-intelligence"))["candidates"]["items"]
            item = next(item for item in items if item["key"] == "budget_notes")
            assert item["requires_scope_confirmation"] and item["opportunity_id"] is None
            preview = (await call("GET", f"/api/profile-suggestions/{item['id']}?opportunity_id={a['id']}"))["candidate"]
            assert preview["current_value"]["id"] == initial["fact"]["id"] and preview["conflict"]
            await candidate(call, item, opportunity_id=a["id"], expected_fact_id=initial["fact"]["id"], reason="确认属于加密项目")
            intel_a = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={a['id']}")
            intel_b = await call("GET", f"/api/customers/{unit['id']}/profile-intelligence?opportunity_id={b['id']}")
            latest = next(fact for fact in intel_a["project_facts"]["items"] if fact["key"] == "budget_notes")
            assert "30万元" in latest["value"] and "尚未审批" in latest["value"]
            assert intel_a["project_facts"]["history_total"] == 2 and intel_b["project_facts"]["items"] == []
            await call("POST", base, {"key": "budget_notes", "value": "旧页面想覆盖为50万元", "basis": "reported", "expected_fact_id": initial["fact"]["id"]}, 409)
            stable = await call("POST", base, {"key": "budget_notes", "value": latest["value"], "basis": latest["basis"], "evidence": latest["evidence"], "expected_fact_id": latest["id"]})
            assert not stable["created"] and stable["project_facts"]["history_total"] == 2
            await call("POST", base, {"key": "industry", "value": "不该变成项目字段", "basis": "reported", "expected_fact_id": None}, 400)
            assert crm.get_customer("me", unit["id"])["amount_cents"] is None
            no_schedule(crm)
    asyncio.run(run())


def test_http_person_discovery_follows_contact_owning_unit_and_project_and_closes_verified_gaps(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, _, _, _):
            group = await customer(call, "个人探索合成集团", unit_type="group")
            child = await customer(call, "个人探索子公司", unit_type="company", parent_customer_id=group["id"])
            buyer = await contact(call, group["id"], "王工", department="采购中心", role="采购经理")
            user = await contact(call, child["id"], "王工", department="业务部", role="业务经理")
            first = await project(call, child["id"], "子公司加密项目")
            second = await project(call, child["id"], "子公司密钥项目")
            first = await member(call, first, buyer, roles=["procurement"], engagement="direct", evidence="负责这次集中采购")
            second = await member(call, second, user, roles=["user"], engagement="direct", evidence="明确是密钥项目使用人")
            route = f"/api/customers/{child['id']}/profile-intelligence"
            a = await call("GET", route+f"?opportunity_id={first['id']}")
            b = await call("GET", route+f"?opportunity_id={second['id']}")
            gaps = a["plan"]["contact_discovery"]
            assert len(gaps) == 2
            assert all(item["contact_id"] == buyer["id"] and item["contact_customer_id"] == group["id"] and item["opportunity_id"] == first["id"] for item in gaps)
            assert gaps[0]["contact_name"] == "王工" and gaps[0]["contact_department"] == "采购中心"
            assert gaps[0]["roles"] == ["procurement"] and "采购" in gaps[0]["ask"] and gaps[0]["why"]
            assert all(item["contact_id"] == user["id"] and item["contact_customer_id"] == child["id"] and item["opportunity_id"] == second["id"] for item in b["plan"]["contact_discovery"])
            combined = await call("GET", route)
            assert len(combined["plan"]["contact_discovery"]) <= 4
            await call("POST", f"/api/customers/{group['id']}/facts", {"contact_id": buyer["id"], "key": "professional_goals", "value": "采购合规、供应商稳定并按期履约", "basis": "reported", "evidence": "采购部王工本人说明"})
            after = await call("GET", route+f"?opportunity_id={first['id']}")
            assert all(item["field_key"] != "professional_goals" for item in after["plan"]["contact_discovery"])
            assert after["plan"]["completion"] == a["plan"]["completion"]  # Person knowledge does not close project budget/decision facts.
            await call("POST", f"/api/customers/{group['id']}/facts", {"contact_id": buyer["id"], "key": "concerns", "value": "我估计他关心供应商稳定", "basis": "observation", "evidence": "个人观察，需本人核对"})
            observed = await call("GET", route+f"?opportunity_id={first['id']}")
            concern = next(item for item in observed["plan"]["contact_discovery"] if item["field_key"] == "concerns")
            assert concern["needs_verification"] and concern["current_basis"] == "observation"
            group_profile = await call("GET", f"/api/customers/{group['id']}/profile")
            child_profile = await call("GET", f"/api/customers/{child['id']}/profile")
            assert next(person for person in group_profile["contacts"] if person["id"] == buyer["id"])["fields"]
            assert next(person for person in child_profile["contacts"] if person["id"] == user["id"])["fields"] == []
            assert await current_project(call, child["id"], second["id"]) == second
            assert (await call("GET", route+f"?opportunity_id={second['id']}"))["project_facts"]["items"] == []
            await call("PATCH", f"/api/customers/{child['id']}/account-network", {"parent_customer_id": None, "expected_revision": child["revision"]})
            invalid = await call("GET", route+f"?opportunity_id={first['id']}")
            assert invalid["plan"]["contact_discovery"] == []
            valid = await call("GET", route+f"?opportunity_id={second['id']}")
            assert all(item["contact_id"] == user["id"] for item in valid["plan"]["contact_discovery"])
            no_schedule(crm)
    asyncio.run(run())


def test_http_cross_unit_person_facts_enter_ai_with_basis_and_make_old_project_advice_stale(tmp_path):
    async def run():
        async with app_context(tmp_path) as (_, call, crm, _, controller, _, _):
            group = await customer(call, "个人上下文合成集团", unit_type="group")
            child = await customer(call, "个人上下文子公司", unit_type="company", parent_customer_id=group["id"])
            buyer = await contact(call, group["id"], "王工", department="采购中心", role="采购经理", phone="13812345678")
            await contact(call, group["id"], "无关联系人", department="其他部门", phone="13987654321")
            p = await project(call, child["id"], "子公司采购项目")
            p = await member(call, p, buyer, roles=["procurement"], engagement="direct", evidence="本人明确负责本次采购")
            facts_route = f"/api/customers/{group['id']}/facts"
            await call("POST", facts_route, {"contact_id": buyer["id"], "key": "professional_goals", "value": "采购合规、供应商稳定并按期履约", "basis": "reported", "evidence": "本人说明年度工作目标"})
            await call("POST", facts_route, {"contact_id": buyer["id"], "key": "concerns", "value": "我观察他更重视合同边界", "basis": "observation", "evidence": "仅销售个人观察"})
            await call("POST", facts_route, {"contact_id": buyer["id"], "key": "authority", "value": "旧通用权限字段不作为项目权力", "basis": "reported", "evidence": "须逐项目核对"})
            source = (await call("POST", "/api/records", {"title": "采购部日常职责原话", "content": "王工日常负责供应商采购与合同管理。", "customer_id": group["id"], "kind": "note"}, 201))["record"]
            await call("POST", facts_route, {"contact_id": buyer["id"], "key": "responsibilities", "value": "负责供应商采购与合同管理", "basis": "reported", "evidence": "王工日常负责供应商采购与合同管理", "source_record_id": source["id"]})
            advisor = Advisor()
            controller.discussions.advisor = advisor
            thread = (await call("POST", "/api/sales-discussions", {"customer_id": child["id"], "opportunity_id": p["id"], "title": "围绕采购人的推进讨论"}, 201))["thread"]
            reply = await call("POST", f"/api/sales-discussions/{thread['id']}/messages", {"text": "如何结合采购王工的目标推进？", "request_id": "cross-unit-person-1"})
            context = advisor.calls[0]
            person = context["project"]["stakeholders"][0]
            assert person["contact_name"] == "王工" and person["unit_name"] == group["name"] and person["contact_department"] == "采购中心"
            facts = {item["key"]: item for item in person["personal_facts"]}
            assert facts["professional_goals"]["value"] == "采购合规、供应商稳定并按期履约"
            assert facts["professional_goals"]["basis"] == "reported" and facts["professional_goals"]["recorded_at"] == NOW
            assert facts["concerns"]["basis"] == "observation"
            assert facts["responsibilities"]["source"]["type"] == "record" and facts["responsibilities"]["source"]["occurred_at"] is None
            assert "authority" not in facts
            serialized = json.dumps(context, ensure_ascii=False)
            assert "13812345678" not in serialized and "13987654321" not in serialized and '"phone"' not in serialized
            assistant = next(item for item in reply["messages"] if item["role"] == "assistant")
            await call("POST", facts_route, {"contact_id": buyer["id"], "key": "professional_goals", "value": "本季度首要目标变为成本控制", "basis": "reported", "evidence": "采购部王工最新明确说明"})
            current = await current_project(call, child["id"], p["id"])
            assert current["revision"] == p["revision"]  # Personal context alone invalidates the discussion snapshot.
            refreshed = await call("GET", f"/api/sales-discussions/{thread['id']}")
            assert next(item for item in refreshed["messages"] if item["role"] == "assistant")["stale"]
            await call("POST", f"/api/sales-discussions/{thread['id']}/messages/{assistant['id']}/actions/1/adopt", {}, 400)
            await call("PATCH", f"/api/records/{source['id']}", {"content": "王工日常不再负责采购与合同管理。", "expected_updated_at": source["updated_at"]})
            current = await current_project(call, child["id"], p["id"])
            assert all(fact["key"] != "responsibilities" for fact in current["stakeholders"][0]["personal_facts"])
            group_profile = await call("GET", f"/api/customers/{group['id']}/profile")
            assert any(fact["key"] == "responsibilities" and fact["contact_id"] == buyer["id"] for fact in group_profile["history"])
            archived = (await call("POST", f"/api/customers/{child['id']}/opportunities/{p['id']}/stakeholders", {"contact_id": buyer["id"], "archived": True, "expected_revision": current["revision"]}))["relationship"]
            assert archived["stakeholders_history"][0]["personal_facts"] == []
            no_schedule(crm)
    asyncio.run(run())
