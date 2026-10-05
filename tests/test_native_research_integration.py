"""Native-search wiring and real HTTP jobs on fresh, synthetic-only stores.

The production factory, provider, web handlers and research worker run together.
Only the remote transport is simulated; no credentials, deployment ports or
business database are read, and this is not a live DeepSeek availability probe.
"""
import asyncio
from contextlib import asynccontextmanager
import json

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import httpx
import pytest

from secretary.customer_store import CustomerStore
from secretary.public_research import make_public_researcher, PublicResearchError, TavilyResearcher
from secretary.store import Store
from secretary.web import create_app, hash_password


NOW = 1_800_000_000.0
OWNER = "synthetic-native-research-owner"
NAME = "合成天河交通通信信息中心"
PRIVATE_NOTE = "私下交流仅供内部使用：联系人喜欢慢跑，暂不对外披露预算"
PRIVATE_PERSON = "合成机密联系人"
PRIVATE_PHONE = "13900001234"
OFFICIAL_KEY = "synthetic-official-model-key"
BODY_EXCERPT_MARKER = "；自动读取公开网页正文节选"


def config(**changes):
    return {"api_key": OFFICIAL_KEY, "base_url": "https://api.deepseek.com",
            "model": "synthetic-conversation-model", "profile_search_api_key": "", **changes}


@pytest.mark.parametrize("base_url", [
    "https://api.deepseek.com", "https://api.deepseek.com/",
    "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/",
    "https://api.deepseek.com:443", "https://api.deepseek.com:443/v1",
])
def test_factory_reuses_only_official_model_credentials(base_url):
    settings = config(base_url=base_url)
    before = settings.copy()
    selected = make_public_researcher(settings)
    assert selected.provider_id == "deepseek"
    assert selected.api_key == OFFICIAL_KEY
    assert selected.model == "deepseek-flash"
    assert settings == before  # Existing conversation choice is not rewritten.


@pytest.mark.parametrize("base_url", [
    "https://synthetic-model-proxy.example", "http://api.deepseek.com",
    "https://api.deepseek.com.attacker.example", "https://fakeapi.deepseek.com",
    "https://api.deepseek.com@attacker.example", "https://user@api.deepseek.com",
    "https://user:password@api.deepseek.com", "https://api.deepseek.com:8443",
    "https://api.deepseek.com/?key=private", "https://api.deepseek.com/#private",
    "https://api.deepseek.com/third-party", "https://api.deepseek.com//v1",
    "https://api.deepseek.com/v1/messages", "", None,
])
def test_factory_never_forwards_a_proxy_key_to_official_search(base_url):
    assert make_public_researcher(config(base_url=base_url, api_key="synthetic-proxy-key")) is None


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://proxy.example"])
def test_explicit_search_service_keeps_priority(base_url):
    selected = make_public_researcher(config(base_url=base_url,
                                            profile_search_api_key="synthetic-tavily-key"))
    assert isinstance(selected, TavilyResearcher)
    assert selected.api_key == "synthetic-tavily-key"


def test_no_model_key_does_not_claim_search_configured():
    assert make_public_researcher(config(api_key="")) is None
    assert make_public_researcher({}) is None


class NativeTransport:
    """Return structured native search blocks, never a substitute researcher."""
    def __init__(self, *, status=200):
        self.status, self.requests = status, []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append({"url": str(request.url), "method": request.method,
                              "payload": payload, "headers": dict(request.headers)})
        if self.status != 200:
            # An untrusted provider can echo keys and internal-looking text.
            # None of it may appear in the persisted run error.
            return httpx.Response(self.status, json={
                "error": f"unsafe provider body {OFFICIAL_KEY} {PRIVATE_NOTE}"})
        query = payload["messages"][0]["content"][0]["text"]
        theme = ("procurement" if "采购公告 招标" in query else
                 "digitalization" if "数字化 信息化" in query else
                 "background" if "机构简介 主营业务" in query else "overview")
        url = f"https://synthetic-public.example/{theme}"
        citation = NAME + "，行业是交通信息服务。总部位于北京。"
        return httpx.Response(200, json={"content": [
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "url": url, "title": NAME + "机构简介",
                 "page_age": "2023-01-02"}]},
            {"type": "text", "text": "模型回答中的未引用推测不能当作资料保存。",
             "citations": [{"type": "web_search_result_location", "url": url,
                            "cited_text": citation}]},
        ]})


PRESERVED_TABLES = (
    "crm_customers", "crm_contacts", "crm_customer_facts", "crm_records",
    "tasks", "proposals", "notifications", "command_results",
)


def snapshot(controller):
    # Only the new fixture database is queried. Exact rows prove more than counts.
    return {table: sorted([dict(row) for row in controller.crm._db.execute(
        f"SELECT * FROM {table}")], key=lambda row: repr(row)) for table in PRESERVED_TABLES}


@asynccontextmanager
async def native_web(tmp_path, *, status=200, selected_researcher="native", transport=None):
    path = tmp_path / "synthetic-native-research.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    transport = transport or NativeTransport(status=status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        researcher = (make_public_researcher(config(), client=remote)
                      if selected_researcher == "native" else selected_researcher)
        if researcher is not None and hasattr(researcher, "clock"):
            researcher.clock = lambda: NOW
        customer = crm.create_customer(OWNER, {"name": NAME, "notes": PRIVATE_NOTE}, NOW)
        crm.create_contact(OWNER, customer["id"], {
            "name": PRIVATE_PERSON, "phone": PRIVATE_PHONE, "department": "合成内部处室"}, NOW)
        crm.save_fact(OWNER, customer["id"], {
            "key": "industry", "value": "用户已经核实的行业", "basis": "reported"}, NOW)
        crm.create_record(OWNER, {"customer_id": customer["id"], "title": "合成内部交流",
                                 "content": PRIVATE_NOTE}, NOW)
        store.execute(OWNER, "preserved-schedule", {"action": "create",
                      "title": "合成已确认拜访", "remind_at": NOW + 3600}, NOW)
        app = create_app(store, crm, asyncio.Lock(), OWNER, hash_password("synthetic-login-password"),
                         public_researcher=researcher, clock=lambda: NOW)
        controller = app.middlewares[0].__self__
        # Manual research must work with no pre-entered official domain.
        assert controller.research_workspace._identity(crm._db, OWNER, customer["id"])["official_domains"] == []
        before = snapshot(controller)
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            login = await client.post('/api/login', json={"password": "synthetic-login-password"})
            assert login.status == 200
            token = (await login.json())["csrf"]

            async def call(method, route, data=None, status=200):
                result = await client.request(method, route, json=data,
                                              headers={"X-CSRF-Token": token})
                payload = await result.json()
                assert result.status == status, (route, result.status, payload)
                return payload

            yield call, controller, customer, transport, before
        finally:
            await client.close()
            crm.close()
            store.close()


async def settled(call, run_id):
    for _ in range(200):
        run = (await call("GET", f"/api/research-runs/{run_id}"))["run"]
        if run["status"] not in ("queued", "searching", "analyzing"):
            return run
        await asyncio.sleep(.01)
    raise AssertionError("The real background research worker did not settle")


def source_count(controller):
    return controller.crm._db.execute("SELECT COUNT(*) FROM crm_profile_public_sources").fetchone()[0]


def candidate_count(controller):
    return controller.crm._db.execute("SELECT COUNT(*) FROM crm_profile_candidates").fetchone()[0]


def assert_private_context_absent(requests):
    for request in requests:
        body = json.dumps(request["payload"], ensure_ascii=False)
        assert NAME in body
        assert all(secret not in body for secret in (PRIVATE_NOTE, PRIVATE_PERSON, PRIVATE_PHONE, OWNER))
        assert request["method"] == "POST"
        assert request["url"] == "https://api.deepseek.com/anthropic/v1/messages"
        assert request["headers"]["x-api-key"] == OFFICIAL_KEY


def test_native_quick_and_three_theme_deep_http_produce_drafts_without_business_writes(tmp_path):
    async def run():
        async with native_web(tmp_path) as (call, controller, customer, transport, before):
            route = f"/api/customers/{customer['id']}/research-workspace"
            capabilities = (await call("GET", route))["capabilities"]
            assert capabilities["research_configured"] is True
            assert capabilities["research_provider"] == "deepseek"
            assert capabilities["research_label"] == "DeepSeek 原生联网研究"
            first = await call("POST", route, {"mode": "quick", "request_id": "native-quick"})
            quick = await settled(call, first["run"]["id"])
            assert quick["status"] == "ready" and quick["sources"] and quick["items"]
            assert len(transport.requests) == 1
            assert transport.requests[0]["payload"]["tools"][0]["max_uses"] == 2
            assert all(not item["selected"] and item["basis"] == "observation" for item in quick["items"])
            assert all(source["published_at"].startswith("2023-01-02")
                       and source["fetched_at"] == NOW and not source["identity_verified"]
                       for source in quick["sources"])
            assert all(BODY_EXCERPT_MARKER not in source["identity_reason"]
                       for source in quick["sources"])
            assert all("模型回答中的未引用推测" not in item["evidence"] for item in quick["items"])
            assert snapshot(controller) == before
            cached = await call("POST", route, {"mode": "quick", "request_id": "native-quick"})
            assert cached["run"]["id"] == quick["id"]
            assert len(transport.requests) == 1
            second = await call("POST", route, {"mode": "deep", "request_id": "native-deep"})
            deep = await settled(call, second["run"]["id"])
            assert deep["status"] == "ready" and len(deep["sources"]) == 3
            assert all(BODY_EXCERPT_MARKER not in source["identity_reason"]
                       for source in deep["sources"])
            assert len(transport.requests) == 4
            queries = [request["payload"]["messages"][0]["content"][0]["text"]
                       for request in transport.requests[1:]]
            assert any("机构简介 主营业务" in query for query in queries)
            assert any("数字化 信息化 数据安全 密码应用" in query for query in queries)
            assert any("采购公告 招标 信息安全 密码" in query for query in queries)
            assert all(request["payload"]["tools"][0]["max_uses"] == 4
                       for request in transport.requests[1:])
            assert all(request["payload"]["model"] == "deepseek-flash" for request in transport.requests)
            assert_private_context_absent(transport.requests)
            assert snapshot(controller) == before
            assert (await call("GET", f"/api/research-runs/{quick['id']}"))["run"]["sources"] == quick["sources"]
            print(json.dumps({"scenario": "native quick and three-theme deep",
                              "transport": "MockTransport, not live provider", "http_search_requests": 4,
                              "research_sources": source_count(controller),
                              "research_candidates": candidate_count(controller),
                              "preserved_table_counts": {key: len(value) for key, value in before.items()}},
                             ensure_ascii=False))
    asyncio.run(run())


@pytest.mark.parametrize("http_status, reason", [
    (401, "密钥未通过验证"), (402, "账户余额不足"), (403, "权限不可用"),
    (404, "接口或模型不可用"), (429, "频率过高"), (500, "服务暂未完成"),
])
def test_safe_native_failure_is_visible_and_retry_recovers_without_duplicate_writes(tmp_path, http_status, reason):
    async def run():
        async with native_web(tmp_path, status=http_status) as (call, controller, customer, transport, before):
            route = f"/api/customers/{customer['id']}/research-workspace"
            started = await call("POST", route, {"mode": "quick", "request_id": "native-error"})
            failed = await settled(call, started["run"]["id"])
            assert failed["status"] == "failed"
            assert source_count(controller) == 0 and failed["items"] == []
            assert reason in failed["errors"][0]["message"]
            assert f"HTTP {http_status}" in failed["errors"][0]["message"]
            persisted = json.dumps(failed, ensure_ascii=False)
            assert OFFICIAL_KEY not in persisted and PRIVATE_NOTE not in persisted
            assert snapshot(controller) == before
            transport.status = 200
            retry_path = f"/api/research-runs/{failed['id']}/retry"
            await call("POST", retry_path, {"expected_revision": failed["revision"]})
            recovered = await settled(call, failed["id"])
            assert recovered["status"] == "ready" and recovered["sources"] and recovered["items"]
            assert recovered["errors"] == [] and source_count(controller) == 1
            prior_count = candidate_count(controller)
            await call("POST", retry_path, {"expected_revision": recovered["revision"]})
            replayed = await settled(call, recovered["id"])
            assert replayed["status"] == "ready"
            assert source_count(controller) == 1 and candidate_count(controller) == prior_count
            # A duplicate stale control request does not reset a completed run.
            await call("POST", retry_path, {"expected_revision": recovered["revision"]}, status=409)
            assert snapshot(controller) == before
            assert_private_context_absent(transport.requests)
            print(json.dumps({"scenario": "failure and retry", "failure_http_status": http_status,
                              "http_search_requests": len(transport.requests),
                              "source_count_after_two_retries": source_count(controller),
                              "candidate_count_after_two_retries": candidate_count(controller),
                              "business_rows_exactly_preserved": True}, ensure_ascii=False))
    asyncio.run(run())


class SyntheticResearcher:
    async def research(self, context):
        return []


@pytest.mark.parametrize("researcher, provider, label", [
    (None, None, ""), (SyntheticResearcher(), "custom", "公开资料检索"),
])
def test_capability_metadata_is_safe_for_unconfigured_and_synthetic_providers(tmp_path, researcher, provider, label):
    async def run():
        async with native_web(tmp_path, selected_researcher=researcher) as (call, controller, customer, transport, before):
            result = await call("GET", f"/api/customers/{customer['id']}/research-workspace")
            capabilities = result["capabilities"]
            assert capabilities["research_provider"] == provider
            assert capabilities["research_label"] == label
            assert capabilities["research_configured"] is (researcher is not None)
            assert transport.requests == [] and snapshot(controller) == before
    asyncio.run(run())


class NoCitationTransport(NativeTransport):
    """Match a native result that identifies sources but has no citations."""
    def __call__(self, request):
        response = super().__call__(request)
        if response.status_code != 200:
            return response
        data = response.json()
        row = data["content"][0]["content"][0]
        data["content"][0]["content"] = [
            {**row, "url": row["url"] + f"-{index}"} for index in range(3)]
        data["content"][1] = {
            "type": "text", "text": "未引用的模型推测：该机构已经承诺采购五百万元。"}
        return httpx.Response(200, json=data)


class SyntheticPageReader:
    """Network boundary only; the native adapter must validate its output."""
    def __init__(self, mode="success"):
        self.mode, self.calls, self.active, self.peak = mode, [], 0, 0

    async def preview(self, url):
        self.calls.append(url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(.02)
            if self.mode == "failure" or (self.mode == "partial" and url.endswith("-0")):
                raise PublicResearchError("网页暂时无法读取，可重试或粘贴原文；当前资料没有改变。")
            if self.mode == "wrong-identity":
                return {"url": "https://unrelated-public.example/about",
                        "title": "另一家合成单位简介", "text": "另一家合成单位，行业是能源。总部位于成都。",
                        "published_at": "2022-05-03", "fetched_at": NOW,
                        "truncated": False, "warning": "合成跳转到了其他单位"}
            return {"url": url, "title": NAME + "真实页面机构简介",
                    "text": NAME + "，行业是交通信息服务。总部位于北京。",
                    "published_at": None if self.mode == "undated" else "2021-07-09T00:00:00+00:00", "fetched_at": NOW,
                    "truncated": False, "original_length": 39,
                    "warning": "公开网页正文已读取，尚未写入画像。"}
        finally:
            self.active -= 1


def install_page_reader(monkeypatch, reader):
    import secretary.public_pages as public_pages
    made = []

    def make_reader(*args, **kwargs):
        made.append(True)
        return reader

    monkeypatch.setattr(public_pages, "PublicPageReader", make_reader)
    return made


@pytest.mark.parametrize("mode, queries", [("quick", 1), ("deep", 3)])
def test_factory_fetches_public_body_for_uncited_native_results_in_real_workspace(tmp_path, monkeypatch, mode, queries):
    reader = SyntheticPageReader()
    made = install_page_reader(monkeypatch, reader)

    async def run():
        async with native_web(tmp_path, transport=NoCitationTransport()) as (call, controller, customer, transport, before):
            route = f"/api/customers/{customer['id']}/research-workspace"
            started = await call("POST", route, {"mode": mode, "request_id": "uncited-native"})
            result = await settled(call, started["run"]["id"])
            assert result["status"] == "ready" and result["items"]
            assert made and len(reader.calls) == queries * 2
            assert reader.peak == 2 and reader.active == 0
            assert len(transport.requests) == queries
            assert len(result["sources"]) == queries * 2
            assert all(source["published_at"].startswith("2021-07-09")
                       and source["fetched_at"] == NOW and not source["identity_verified"]
                       for source in result["sources"])
            # Use the actual fetched title, text and publication date, rather
            # than the native search title/page_age or unreferenced model prose.
            assert all("真实页面机构简介" in source["title"] for source in result["sources"])
            assert all(BODY_EXCERPT_MARKER in source["identity_reason"]
                       for source in result["sources"])
            assert all("五百万元" not in item["evidence"] and not item["selected"]
                       for item in result["items"])
            assert snapshot(controller) == before
            assert_private_context_absent(transport.requests)
            assert all(PRIVATE_PHONE not in url and PRIVATE_PERSON not in url for url in reader.calls)
            print(json.dumps({"scenario": "uncited native body fallback", "mode": mode,
                              "native_requests": len(transport.requests), "body_reads": len(reader.calls),
                              "public_sources": source_count(controller), "candidates": candidate_count(controller),
                              "peak_concurrent_body_reads": reader.peak,
                              "business_rows_exactly_preserved": True}, ensure_ascii=False))
    asyncio.run(run())


def test_uncited_native_partial_body_read_keeps_usable_source_and_user_data(tmp_path, monkeypatch):
    reader = SyntheticPageReader("partial")
    install_page_reader(monkeypatch, reader)

    async def run():
        async with native_web(tmp_path, transport=NoCitationTransport()) as (call, controller, customer, transport, before):
            started = await call("POST", f"/api/customers/{customer['id']}/research-workspace",
                                 {"mode": "quick", "request_id": "partial-body"})
            result = await settled(call, started["run"]["id"])
            assert result["status"] in ("ready", "partial") and len(result["sources"]) == 1
            assert result["items"] and result["sources"][0]["url"].endswith("-1")
            assert len(reader.calls) == 2 and snapshot(controller) == before
            assert_private_context_absent(transport.requests)
            print(json.dumps({"scenario": "partial body failure", "body_reads": 2,
                              "public_sources": source_count(controller), "candidates": candidate_count(controller),
                              "business_rows_exactly_preserved": True}, ensure_ascii=False))
    asyncio.run(run())


def test_uncited_native_all_body_reads_fail_then_retry_recovers_without_duplicates(tmp_path, monkeypatch):
    reader = SyntheticPageReader("failure")
    install_page_reader(monkeypatch, reader)

    async def run():
        async with native_web(tmp_path, transport=NoCitationTransport()) as (call, controller, customer, transport, before):
            started = await call("POST", f"/api/customers/{customer['id']}/research-workspace",
                                 {"mode": "quick", "request_id": "failed-body"})
            failed = await settled(call, started["run"]["id"])
            assert failed["status"] == "failed" and failed["sources"] == [] and failed["items"] == []
            assert "正文" in failed["errors"][0]["message"]
            assert len(reader.calls) == 2 and snapshot(controller) == before
            reader.mode = "success"
            retry_path = f"/api/research-runs/{failed['id']}/retry"
            await call("POST", retry_path, {"expected_revision": failed["revision"]})
            recovered = await settled(call, failed["id"])
            assert recovered["status"] == "ready" and recovered["errors"] == []
            assert source_count(controller) == 2 and candidate_count(controller) == 4
            await call("POST", retry_path, {"expected_revision": recovered["revision"]})
            again = await settled(call, recovered["id"])
            assert again["status"] == "ready" and source_count(controller) == 2 and candidate_count(controller) == 4
            assert len(reader.calls) == 6 and len(transport.requests) == 3
            assert snapshot(controller) == before
            assert_private_context_absent(transport.requests)
            print(json.dumps({"scenario": "all body reads fail then retry twice",
                              "body_reads": len(reader.calls), "native_requests": len(transport.requests),
                              "public_sources": source_count(controller), "candidates": candidate_count(controller),
                              "business_rows_exactly_preserved": True}, ensure_ascii=False))
    asyncio.run(run())


def test_uncited_native_redirected_page_must_match_actual_final_unit_identity(tmp_path, monkeypatch):
    reader = SyntheticPageReader("wrong-identity")
    install_page_reader(monkeypatch, reader)

    async def run():
        async with native_web(tmp_path, transport=NoCitationTransport()) as (call, controller, customer, transport, before):
            started = await call("POST", f"/api/customers/{customer['id']}/research-workspace",
                                 {"mode": "quick", "request_id": "wrong-final-unit"})
            result = await settled(call, started["run"]["id"])
            assert len(reader.calls) == 2
            assert result["sources"] == [] and result["items"] == []
            assert source_count(controller) == 0
            assert snapshot(controller) == before
            assert_private_context_absent(transport.requests)
    asyncio.run(run())


def test_uncited_native_body_unknown_date_stays_unknown_after_source_persistence(tmp_path, monkeypatch):
    reader = SyntheticPageReader("undated")
    install_page_reader(monkeypatch, reader)

    async def run():
        async with native_web(tmp_path, transport=NoCitationTransport()) as (call, controller, customer, transport, before):
            started = await call("POST", f"/api/customers/{customer['id']}/research-workspace",
                                 {"mode": "quick", "request_id": "undated-body"})
            result = await settled(call, started["run"]["id"])
            assert result["status"] == "ready" and len(result["sources"]) == 2
            assert all(source["published_at"] is None and source["fetched_at"] == NOW
                       and BODY_EXCERPT_MARKER in source["identity_reason"]
                       for source in result["sources"])
            assert snapshot(controller) == before
            assert_private_context_absent(transport.requests)
    asyncio.run(run())


def test_legacy_public_import_preserves_only_fixed_body_marker_and_not_provider_identity_claim(tmp_path):
    async def run():
        async with native_web(tmp_path, selected_researcher=None) as (_, controller, customer, transport, before):
            profile = controller.profile_intelligence
            source = {"url": "https://synthetic-public.example/legacy-body",
                      "title": NAME + "机构简介", "text": NAME + "，行业是交通信息服务。总部位于北京。",
                      "published_at": None, "fetched_at": NOW, "entity_name": NAME,
                      "identity_reason": "供应商宣称这是已认证官方身份，绝不能采信" + BODY_EXCERPT_MARKER}
            result = await profile.import_public_source(OWNER, customer["id"], source)
            persisted = dict(controller.crm._db.execute(
                "SELECT * FROM crm_profile_public_sources WHERE id=?", (result["source_id"],)).fetchone())
            assert persisted["identity_verified"] == 0
            assert persisted["identity_reason"] == "全名命中，仍需排除同名单位" + BODY_EXCERPT_MARKER
            assert persisted["published_at"] is None and persisted["fetched_at"] == NOW
            assert "供应商宣称" not in persisted["identity_reason"]
            again = await profile.import_public_source(OWNER, customer["id"], source)
            assert again["source_id"] == result["source_id"] and source_count(controller) == 1
            unmarked = await profile.import_public_source(OWNER, customer["id"], {
                **source, "url": "https://synthetic-public.example/legacy-unmarked",
                "identity_reason": "已核对官方域名；供应商宣称已认证"})
            plain = dict(controller.crm._db.execute(
                "SELECT * FROM crm_profile_public_sources WHERE id=?", (unmarked["source_id"],)).fetchone())
            assert plain["identity_verified"] == 0 and plain["identity_reason"] == "全名命中，仍需排除同名单位"
            assert BODY_EXCERPT_MARKER not in plain["identity_reason"]
            assert snapshot(controller) == before and transport.requests == []
    asyncio.run(run())
