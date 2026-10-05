"""Contract tests for native search, using only synthetic HTTP responses."""
import asyncio
import json

import httpx
import pytest

from secretary.deepseek_research import DeepSeekResearcher
from secretary.public_research import PublicResearchError


UNIT = "合成研究银行"
KEY = "synthetic-private-api-key"
URL = "https://bank.example/about"


def result(url=URL, title="单位简介", age="2026-09-01"):
    return {"type": "web_search_result", "url": url, "title": title, "page_age": age}


def tool(*rows):
    return {"type": "web_search_tool_result", "content": list(rows)}


def prose(*citations, text="模型总结：绝不保存为原文"):
    return {"type": "text", "text": text, "citations": list(citations)}


def cite(url=URL, text="合成研究银行负责数据安全业务。"):
    return {"type": "web_search_result_location", "url": url, "cited_text": text}


def search(response, context=None, *, follow_redirects=False, page_reader=None):
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            return response(request) if callable(response) else httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=follow_redirects) as client:
            kwargs = {"client": client, "clock": lambda: 1000}
            if page_reader is not None:
                kwargs["page_reader"] = page_reader
            provider = DeepSeekResearcher(KEY, **kwargs)
            rows = await provider.research(context or {"customer": {"name": UNIT}})
        return rows, requests
    return asyncio.run(run())


def test_native_request_uses_official_messages_and_bounded_server_tool():
    rows, requests = search({"content": [tool(result()), prose(cite())]})
    assert len(rows) == len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["authorization"] == "Bearer " + KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert all(value == 40 for value in request.extensions["timeout"].values())
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["max_tokens"] == 4096
    assert body["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert len(body["messages"][0]["content"]) == 1
    assert body["messages"][0]["content"][0]["type"] == "text"
    assert "Perform a web search for the query: " in body["messages"][0]["content"][0]["text"]
    assert UNIT in body["messages"][0]["content"][0]["text"]
    assert rows == [{"url": URL, "title": "单位简介", "text": "合成研究银行负责数据安全业务。",
                     "published_at": "2026-09-01T00:00:00+00:00", "fetched_at": 1000,
                     "entity_name": UNIT, "identity_reason": "出现单位正式全名，仍需核对同名机构"}]


def test_only_public_identity_and_fixed_theme_leave_process():
    context = {"customer": {"name": UNIT, "phone": "private-phone", "description": "secret-personal"},
               "settings": {"official_domains": ["bank.example"], "notes": "secret-strategy"},
               "contacts": [{"name": "private-person"}], "source": {"text": "内部预算28万元"},
               "mode": "deep", "theme": "procurement", "question": "private-problem"}
    rows, requests = search({"content": [tool(result()), prose(cite())]}, context)
    sent = requests[0].content.decode()
    assert all(secret not in sent for secret in ("private-phone", "secret-personal", "secret-strategy",
                                                "private-person", "28万元", "private-problem"))
    body = json.loads(requests[0].content)
    assert body["tools"][0]["max_uses"] == 4
    assert "采购公告" in body["messages"][0]["content"][0]["text"]
    assert "site:" not in body["messages"][0]["content"][0]["text"]
    assert rows[0]["identity_reason"] == "已限定用户核对的官网域名"


def test_tool_results_join_citations_across_blocks_and_deduplicate_urls():
    other = "https://tender.example/notice"
    rows, _ = search({"content": [
        prose(cite(URL + "#source"), cite(URL, "较晚的引文不应替换第一处")),
        tool(result(), result(other, title=UNIT + "采购公告")),
        {"type": "thinking", "text": "不要采纳这个块"},
        tool(result(), {"type": "other", "url": "https://other.example/no"}),
        prose(cite(other, UNIT + "采购密码产品。")),
    ]})
    assert [row["url"] for row in rows] == [URL, other]
    assert rows[0]["text"] == "合成研究银行负责数据安全业务。"
    assert all("模型总结" not in row["text"] for row in rows)


def test_explicit_official_domains_constrain_overview_and_background():
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["bank.example"]},
               "theme": "background"}
    offsite = "https://fake-bank.example/about"
    subdomain = "https://news.bank.example/release"
    rows, requests = search({"content": [
        tool(result(), result(offsite, title=UNIT), result(subdomain, title="银行新闻")),
        prose(cite(URL, "官网介绍"), cite(offsite), cite(subdomain, "官网新闻原文")),
    ]}, context)
    assert [row["url"] for row in rows] == [URL, subdomain]
    assert all(row["identity_reason"] == "已限定用户核对的官网域名" for row in rows)
    assert "site:bank.example" in json.loads(requests[0].content)["messages"][0]["content"][0]["text"]


def test_procurement_can_use_public_tenders_but_requires_full_unit_name():
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["bank.example"]},
               "theme": "procurement"}
    good, unrelated = "https://tender.example/notice", "https://other.example/notice"
    rows, _ = search({"content": [
        tool(result(good, title=UNIT + "采购"), result(unrelated, title="合成银行采购")),
        prose(cite(good, UNIT + "采购需求"), cite(unrelated, "合成银行采购需求")),
    ]}, context)
    assert len(rows) == 1 and rows[0]["url"] == good
    assert "同名" in rows[0]["identity_reason"]


def test_no_domain_never_treats_abbreviated_or_other_units_as_confirmed_identity():
    rows, _ = search({"content": [tool(result(title="合成银行")), prose(cite(text="合成银行简介"))]})
    assert rows == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://localhost/a", "http://127.0.0.1/a",
    "http://192.168.1.131/a", "https://name:secret@example.com/a", "https://company.local/a",
    "https://bank.example:8765/a"])
def test_unsafe_urls_never_become_sources_or_followup_requests(url):
    rows, requests = search({"content": [tool(result(url, title=UNIT)), prose(cite(url))]})
    assert rows == [] and len(requests) == 1


def test_results_without_excerpt_are_skipped_instead_of_using_answer_text():
    unquoted = "https://bank.example/no-quote"
    rows, _ = search({"content": [tool(result(), result(unquoted, title=UNIT)),
                                 prose(cite(), text=UNIT + "模型猜测预算100万")]})
    assert [row["url"] for row in rows] == [URL]
    assert "100万" not in rows[0]["text"]


def test_all_results_missing_citations_are_explicitly_not_evidence():
    with pytest.raises(PublicResearchError, match="引用原文"):
        search({"content": [tool(result(title=UNIT)), prose(text=UNIT + "未经引用的模型回答")]})


@pytest.mark.parametrize("content", [[], [{"type": "text", "text": "普通模型回答"}],
                                        [{"type": "server_tool_use", "name": "web_search"}]])
def test_missing_native_result_blocks_raise_clear_error(content):
    with pytest.raises(PublicResearchError, match="联网检索结果"):
        search({"content": content})


def test_empty_native_results_are_empty_not_fabricated():
    rows, _ = search({"content": [tool(), prose(text="未找到匹配资料")]})
    assert rows == []


@pytest.mark.parametrize("payload", [None, [], {"content": None}, {"content": "invalid"},
                                           {"content": [tool(None, "invalid")]}])
def test_malformed_shapes_do_not_escape_raw_provider_payload(payload):
    # A valid result block containing malformed entries is safe to skip.
    if payload == {"content": [tool(None, "invalid")]}:
        assert search(payload)[0] == []
    else:
        with pytest.raises(PublicResearchError, match="格式|联网检索结果"):
            search(lambda request: httpx.Response(200, json=payload))


@pytest.mark.parametrize("status, clue", [(400, "请求"), (401, "密钥"), (402, "余额"), (403, "权限"),
    (404, "接口"), (429, "频率"), (500, "服务")])
def test_http_errors_are_actionable_and_never_expose_provider_body_or_key(status, clue):
    with pytest.raises(PublicResearchError, match=clue) as caught:
        search(lambda request: httpx.Response(status, json={"error": KEY + " private-provider-data"}))
    assert KEY not in str(caught.value) and "private-provider-data" not in str(caught.value)
    assert str(status) in str(caught.value)


def test_redirect_is_not_followed_even_when_supplied_client_follows_redirects():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://elsewhere.example/steal"})

    with pytest.raises(PublicResearchError, match="跳转"):
        search(handler, follow_redirects=True)
    assert len(requests) == 1 and requests[0].url.host == "api.deepseek.com"


@pytest.mark.parametrize("error, clue", [(httpx.ReadTimeout("provider-" + KEY), "超时"),
                                         (httpx.ConnectError("provider-" + KEY), "连接")])
def test_transport_failure_is_safe_and_actionable(error, clue):
    def handler(request):
        raise error
    with pytest.raises(PublicResearchError, match=clue) as caught:
        search(handler)
    assert KEY not in str(caught.value)


def test_invalid_json_is_safe():
    with pytest.raises(PublicResearchError, match="格式") as caught:
        search(lambda request: httpx.Response(200, text="not-json:" + KEY))
    assert KEY not in str(caught.value)


def test_response_size_limit_is_enforced_while_streaming():
    class LargeBody(httpx.AsyncByteStream):
        chunks_read = 0

        async def __aiter__(self):
            for _ in range(100):
                self.chunks_read += 1
                yield b"x" * 10000

    stream = LargeBody()
    with pytest.raises(PublicResearchError, match="过大"):
        search(lambda request: httpx.Response(200, stream=stream))
    assert stream.chunks_read <= 51


def test_titles_and_cited_excerpts_and_results_are_bounded():
    urls = ["https://bank.example/" + str(i) for i in range(9)]
    rows, _ = search({"content": [tool(*(result(url, "x" * 400 + UNIT) for url in urls)),
                                 prose(*(cite(url, UNIT + "x" * 15000) for url in urls))]})
    assert len(rows) == 5
    assert all(len(row["title"]) <= 300 and len(row["text"]) <= 10000 for row in rows)


def test_unparseable_page_age_does_not_invent_publication_date():
    rows, _ = search({"content": [tool(result(age="2 days ago")), prose(cite())]})
    assert rows[0]["published_at"] is None


@pytest.mark.parametrize("context", [{"customer": {"name": ""}}, {"customer": {"name": "x" * 201}},
    {"customer": {"name": UNIT + "\x00"}}, {"customer": {"name": UNIT}, "mode": "unbounded"},
    {"customer": {"name": UNIT}, "theme": "private-strategy"},
    {"customer": {"name": UNIT}, "settings": {"official_domains": ["http://127.0.0.1"]}}])
def test_bad_identity_and_mode_are_rejected_before_dispatch(context):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"content": []})
    with pytest.raises(PublicResearchError):
        search(handler, context)
    assert requests == []


def test_missing_key_is_explicit_and_no_request_is_sent():
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PublicResearchError, match="尚未配置"):
                await DeepSeekResearcher("", client=client).research({"customer": {"name": UNIT}})
        assert requests == []
    asyncio.run(run())


def test_server_tool_error_has_fixed_actionable_text_without_payload():
    payload = {"content": [{"type": "web_search_tool_result", "content": {
        "type": "web_search_tool_result_error", "error_code": "unavailable", "message": KEY}}]}
    with pytest.raises(PublicResearchError, match="联网检索暂不可用") as caught:
        search(payload)
    assert KEY not in str(caught.value)


def test_provider_cannot_be_redirected_through_constructor_configuration():
    with pytest.raises(TypeError):
        DeepSeekResearcher(KEY, endpoint="https://elsewhere.example/messages")


def test_citation_without_native_result_cannot_add_a_source():
    rows, _ = search({"content": [tool(), prose(cite())]})
    assert rows == []


def test_research_domains_snapshot_takes_precedence_over_settings():
    other = "https://old-bank.example/about"
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["old-bank.example"]},
               "research_domains": ["bank.example"], "theme": "overview"}
    rows, requests = search({"content": [tool(result(), result(other, title=UNIT)),
                                         prose(cite(), cite(other))]}, context)
    assert [row["url"] for row in rows] == [URL]
    query = json.loads(requests[0].content)["messages"][0]["content"][0]["text"]
    assert "site:bank.example" in query and "old-bank.example" not in query


def test_malformed_citations_do_not_replace_a_valid_excerpt():
    payload = {"content": [
        prose(None, "invalid", {"url": URL, "cited_text": None}, {"url": URL, "cited_text": []}),
        {"type": "text", "citations": "invalid"},
        tool(result(), result("https://other.example/no", title=None)),
        prose(cite()),
    ]}
    rows, _ = search(payload)
    assert len(rows) == 1 and rows[0]["text"] == "合成研究银行负责数据安全业务。"


def test_content_length_rejects_large_body_before_reading_any_content():
    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("oversized response should never be read")
            yield b"never"

    with pytest.raises(PublicResearchError, match="过大"):
        search(lambda request: httpx.Response(200, headers={"Content-Length": "500001"}, stream=UnreadBody()))


def test_cancellation_propagates_and_closes_stream_instead_of_becoming_retry_error():
    class CancelledBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise asyncio.CancelledError()
            yield b"never"

        async def aclose(self):
            self.closed = True

    stream = CancelledBody()
    with pytest.raises(asyncio.CancelledError):
        search(lambda request: httpx.Response(200, stream=stream))
    assert stream.closed


def test_custom_model_argument_changes_only_model_not_endpoint_or_privacy_boundary():
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"content": [tool()]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows = await DeepSeekResearcher(KEY, client=client, model="trusted-search-model").research({
                "customer": {"name": UNIT}, "theme": "digitalization", "private": "secret-business-data"})
        assert rows == []
        assert json.loads(requests[0].content)["model"] == "trusted-search-model"
        assert str(requests[0].url) == "https://api.deepseek.com/anthropic/v1/messages"
        assert "secret-business-data" not in requests[0].content.decode()
    asyncio.run(run())


def page(url=URL, *, title="真正网页标题", text=UNIT + "真实公开网页正文，已说明其职责与业务方向。",
         published_at=None, fetched_at=1100):
    return {"url": url, "title": title, "text": text,
            "published_at": published_at, "fetched_at": fetched_at,
            "warning": "尚未写入画像", "truncated": False, "original_length": len(text)}


class SyntheticPageReader:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
        self.active = self.max_active = 0

    async def preview(self, url):
        self.calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            value = self.pages[url]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.active -= 1


def test_uncited_native_results_use_real_page_body_never_search_title_or_model_prose():
    reader = SyntheticPageReader({URL: page()})
    payload = {"content": [tool({**result(title=UNIT + "搜索标题"), "encrypted_content": "opaque-data"}),
                           prose(text=UNIT + "模型猜测预算100万")]}
    rows, requests = search(payload, page_reader=reader)
    assert len(rows) == 1 and reader.calls == [URL] and len(requests) == 1
    assert rows[0] == {"url": URL, "title": "真正网页标题", "text": page()["text"],
                       "published_at": None, "fetched_at": 1100, "entity_name": UNIT,
                       "identity_reason": "出现单位正式全名，仍需核对同名机构；自动读取公开网页正文节选"}
    assert "自动读取公开网页正文节选" in rows[0]["identity_reason"]
    assert "搜索标题" not in rows[0]["title"] and "100万" not in rows[0]["text"]
    assert "encrypted_content" not in rows[0] and "warning" not in rows[0]


def test_missing_excerpts_read_no_more_than_two_distinct_pages_concurrently():
    urls = ["https://bank.example/fallback" + str(i) for i in range(5)]
    reader = SyntheticPageReader({url: page(url) for url in urls})
    payload = {"content": [tool(result(urls[0]), result(urls[0]), *(result(url) for url in urls[1:]))]}
    rows, _ = search(payload, page_reader=reader)
    assert reader.calls == urls[:2] and reader.max_active == 2
    assert [row["url"] for row in rows] == urls[:2]


def test_page_redirect_uses_final_public_url_and_page_publication():
    final = "https://www.bank.example/about"
    reader = SyntheticPageReader({URL: page(final, text="真正机构网站介绍的公开正文内容，身份校验不依赖搜索标题。",
                                            published_at="2026-10-01")})
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["bank.example"]}}
    rows, _ = search({"content": [tool(result())]}, context, page_reader=reader)
    assert rows[0]["url"] == final and rows[0]["published_at"] == "2026-10-01T00:00:00+00:00"
    assert rows[0]["identity_reason"] == "已限定用户核对的官网域名；自动读取公开网页正文节选"


def test_final_redirect_to_other_unit_cannot_pass_using_full_name_in_search_title():
    reader = SyntheticPageReader({URL: page("https://other.example/about", title="另一机构",
                                            text="其他单位的公开简介和信息，未包含本次研究单位身份。")})
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["bank.example"]}}
    rows, _ = search({"content": [tool(result(title=UNIT + "官方简介"))]}, context, page_reader=reader)
    assert rows == [] and reader.calls == [URL]


@pytest.mark.parametrize("final", ["http://127.0.0.1/admin", "https://name:secret@example.com/private",
                                    "https://internal.company.local/a"])
def test_reader_final_private_or_credential_url_is_rejected(final):
    reader = SyntheticPageReader({URL: page(final)})
    with pytest.raises(PublicResearchError, match="正文") as caught:
        search({"content": [tool(result())]}, page_reader=reader)
    assert "secret" not in str(caught.value)
    assert reader.calls == [URL]


def test_private_native_result_is_never_sent_to_page_reader():
    reader = SyntheticPageReader({})
    payload = {"content": [tool(result("http://192.168.1.131/private", title=UNIT))]}
    rows, _ = search(payload, page_reader=reader)
    assert rows == [] and reader.calls == []


def test_one_page_failure_preserves_real_success_without_exposing_exception():
    other = "https://bank.example/fails"
    reader = SyntheticPageReader({URL: page(), other: PublicResearchError(KEY + " private-page-error")})
    rows, _ = search({"content": [tool(result(), result(other))]}, page_reader=reader)
    assert len(rows) == 1 and rows[0]["url"] == URL
    assert KEY not in json.dumps(rows)


def test_all_page_failures_show_fixed_error_and_do_not_use_model_answer():
    other = "https://bank.example/fails"
    reader = SyntheticPageReader({URL: RuntimeError(KEY), other: PublicResearchError("private-error")})
    with pytest.raises(PublicResearchError, match="正文") as caught:
        search({"content": [tool(result(), result(other)), prose(text=UNIT + "模型回答") ]},
               page_reader=reader)
    assert KEY not in str(caught.value) and "private-error" not in str(caught.value)


def test_quoted_source_is_kept_when_uncited_page_cannot_be_read():
    other = "https://bank.example/fails"
    reader = SyntheticPageReader({other: PublicResearchError("unavailable")})
    rows, _ = search({"content": [tool(result(), result(other)), prose(cite())]}, page_reader=reader)
    assert len(rows) == 1 and rows[0]["text"] == "合成研究银行负责数据安全业务。"
    assert "自动读取公开网页正文节选" not in rows[0]["identity_reason"]
    assert reader.calls == [other]


def test_page_final_urls_deduplicate_against_each_other_and_quoted_sources():
    other = "https://bank.example/redirect"
    third = "https://bank.example/another-redirect"
    reader = SyntheticPageReader({other: page(), third: page()})
    rows, _ = search({"content": [tool(result(), result(other), result(third)), prose(cite())]},
                     page_reader=reader)
    assert [row["url"] for row in rows] == [URL] and reader.calls == [other, third]


def test_body_fallback_truncation_cannot_use_search_title_to_hide_missing_identity():
    reader = SyntheticPageReader({URL: page(title="网页无单位名", text="普通公开正文" * 3000 + UNIT)})
    rows, _ = search({"content": [tool(result(title=UNIT))]}, page_reader=reader)
    assert rows == []


def test_page_reader_is_only_given_url_and_never_model_auth_or_private_context():
    reader = SyntheticPageReader({URL: page()})
    rows, _ = search({"content": [tool(result())]},
                     {"customer": {"name": UNIT, "phone": "private-phone"},
                      "contacts": [{"name": "private-person"}]}, page_reader=reader)
    assert len(rows) == 1 and reader.calls == [URL]


def test_page_fallback_budget_cancels_pending_read_and_preserves_completed_page(monkeypatch):
    import secretary.deepseek_research as module
    monkeypatch.setattr(module, "_PAGE_TIMEOUT", 0.025, raising=False)
    other = "https://bank.example/slow"

    class SlowReader(SyntheticPageReader):
        cancelled = False

        async def preview(self, url):
            if url == other:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True
            return await super().preview(url)

    reader = SlowReader({URL: page()})
    rows, _ = search({"content": [tool(result(), result(other))]}, page_reader=reader)
    assert len(rows) == 1 and rows[0]["url"] == URL and reader.cancelled


def test_all_page_read_timeout_is_explicit_and_safe(monkeypatch):
    import secretary.deepseek_research as module
    monkeypatch.setattr(module, "_PAGE_TIMEOUT", 0.025, raising=False)

    class TimeoutReader:
        async def preview(self, url):
            await asyncio.Event().wait()

    with pytest.raises(PublicResearchError, match="正文"):
        search({"content": [tool(result())]}, page_reader=TimeoutReader())


def test_quoted_result_does_not_trigger_a_redundant_page_read():
    reader = SyntheticPageReader({})
    rows, _ = search({"content": [tool(result()), prose(cite())]}, page_reader=reader)
    assert len(rows) == 1 and reader.calls == []


def test_domain_constrained_result_does_not_read_an_unrelated_initial_site():
    other = "https://other-bank.example/about"
    reader = SyntheticPageReader({other: page(other)})
    context = {"customer": {"name": UNIT}, "settings": {"official_domains": ["bank.example"]}}
    rows, _ = search({"content": [tool(result(other, title=UNIT))]}, context, page_reader=reader)
    assert rows == [] and reader.calls == []


def test_page_text_and_title_are_bounded_and_unknown_date_is_not_from_search_age():
    reader = SyntheticPageReader({URL: page(title="真实标题" * 100, text=UNIT + "公开正文" * 5000,
                                            published_at="unknown")})
    rows, _ = search({"content": [tool(result(age="2026-01-01"))]}, page_reader=reader)
    assert len(rows) == 1 and len(rows[0]["title"]) == 300 and len(rows[0]["text"]) == 10000
    assert rows[0]["published_at"] is None


@pytest.mark.parametrize("value", [None, [], "not-a-page", {"url": URL, "title": "x", "text": "short"}])
def test_malformed_or_empty_page_body_is_a_safe_failure(value):
    reader = SyntheticPageReader({URL: value})
    with pytest.raises(PublicResearchError, match="正文"):
        search({"content": [tool(result())]}, page_reader=reader)


def test_outer_cancellation_cancels_both_page_reads_and_propagates():
    async def run():
        other = "https://bank.example/other"

        class CancellableReader:
            active, closed = 0, 0

            def __init__(self):
                self.started = asyncio.Event()

            async def preview(self, url):
                self.active += 1
                if self.active == 2:
                    self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.closed += 1

        reader = CancellableReader()
        def handler(request):
            return httpx.Response(200, json={"content": [tool(result(), result(other))]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekResearcher(KEY, client=client, page_reader=reader)
            operation = asyncio.create_task(provider.research({"customer": {"name": UNIT}}))
            await asyncio.wait_for(reader.started.wait(), timeout=1)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        assert reader.closed == 2
    asyncio.run(run())
