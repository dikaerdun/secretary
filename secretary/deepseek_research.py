"""Bounded native DeepSeek search; model prose never becomes source evidence.

The Messages server tool returns URLs in web_search_tool_result blocks and
may return excerpts in text-block citations. Missing excerpts can use an
explicitly injected safe public-page reader, never the model's own answer.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from .public_research import PublicResearchError, _publication, normalize_domains, public_url


_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
_RESPONSE_LIMIT = 500000
_PAGE_TIMEOUT = 20
_TERMS = {
    "overview": "官网 机构简介 信息化 采购公告",
    "background": "官网 机构简介 主营业务",
    "digitalization": "数字化 信息化 数据安全 密码应用",
    "procurement": "采购公告 招标 信息安全 密码",
}


def _http_error(status):
    hints = {
        400: "DeepSeek 联网研究请求未被接受，请核对模型配置后重试。",
        401: "DeepSeek 密钥未通过验证，请核对现有模型密钥。",
        402: "DeepSeek 账户余额不足，请补充余额后重试。",
        403: "DeepSeek 联网研究权限不可用，请核对账户权限。",
        404: "DeepSeek 联网研究接口或模型不可用，请核对模型配置后重试。",
        429: "DeepSeek 联网研究请求频率过高，请稍后重试。",
    }
    hint = hints.get(status, "DeepSeek 联网研究服务暂未完成，请稍后重试。")
    return PublicResearchError(f"{hint}（HTTP {status}）当前画像保留。")


def _safe_url(value):
    try:
        return public_url(value)
    except PublicResearchError:
        return None


class DeepSeekResearcher:
    """Use one fixed official endpoint with a finite native-search allowance.

    The caller may inject an HTTP client for tests. Redirect following is always
    disabled on the request, including when that client has it enabled globally.
    Existing business context is accepted for compatibility, but only the unit's
    full name, verified public domains and fixed research theme enter the query.
    """
    provider_id = "deepseek"
    provider_label = "DeepSeek 原生联网研究"

    def __init__(self, api_key, *, client=None, clock=time.time, model="deepseek-flash", page_reader=None):
        self.api_key, self.client, self.clock, self.model = api_key, client, clock, model
        self.page_reader = page_reader

    async def research(self, context):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise PublicResearchError("尚未配置 DeepSeek 联网研究密钥，可先粘贴公开资料。")
        if not isinstance(context, dict) or not isinstance(context.get("customer", {}), dict):
            raise PublicResearchError("请先核对单位正式名称，再检索公开资料。")
        name = context.get("customer", {}).get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "\x00" in name:
            raise PublicResearchError("请先核对单位正式名称，再检索公开资料。")
        settings = context.get("settings", {})
        if not isinstance(settings, dict):
            raise PublicResearchError("公开研究配置格式无效，请核对官网域名。")
        domains = normalize_domains(context.get("research_domains", settings.get("official_domains", [])))
        mode, theme = context.get("mode", "quick"), context.get("theme", "overview")
        if mode not in ("quick", "deep") or not isinstance(theme, str) or theme not in _TERMS:
            raise PublicResearchError("公开研究模式无效。")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 100:
            raise PublicResearchError("DeepSeek 联网研究模型配置无效。")

        # Tender platforms are allowed for procurement/digitalization, with the
        # same full-unit-name filter as Tavily; they do not become official sites.
        constrained_domains = domains if theme not in ("digitalization", "procurement") else []
        query = name.strip() + " " + _TERMS[theme]
        if constrained_domains:
            query += " (" + " OR ".join("site:" + domain for domain in constrained_domains) + ")"
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": [{
                "type": "text", "text": "Perform a web search for the query: " + query,
            }]}],
            "tools": [{"type": "web_search_20250305", "name": "web_search",
                       "max_uses": 2 if mode == "quick" else 4}],
        }
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=40, follow_redirects=False, trust_env=False) as client:
                    data = await self._search(client, payload)
            else:
                data = await self._search(self.client, payload)
        except PublicResearchError:
            raise
        except httpx.TimeoutException:
            raise PublicResearchError("DeepSeek 联网研究超时，当前画像保留，可稍后重试。") from None
        except httpx.HTTPError:
            raise PublicResearchError("暂时无法连接 DeepSeek 联网研究服务，当前画像保留，可稍后重试。") from None
        except (ValueError, TypeError, KeyError):
            raise PublicResearchError("DeepSeek 联网研究结果格式无效，当前画像没有改变。") from None
        return await self._sources(data, name.strip(), domains, constrained_domains)

    async def _search(self, client, payload):
        # A streaming size cap avoids downloading an unbounded provider body.
        # Never read or display an error response body, which may echo secrets.
        async with client.stream("POST", _ENDPOINT,
                                 headers={"x-api-key": self.api_key,
                                          "Authorization": "Bearer " + self.api_key,
                                          "anthropic-version": "2023-06-01",
                                          "Accept": "application/json"},
                                 json=payload, timeout=40, follow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                raise PublicResearchError("DeepSeek 联网研究接口发生跳转，已停止请求，当前画像保留。")
            if not 200 <= response.status_code < 300:
                raise _http_error(response.status_code)
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > _RESPONSE_LIMIT:
                raise PublicResearchError("DeepSeek 检索结果过大，请缩小研究范围后重试。")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(chunk) > _RESPONSE_LIMIT:
                    raise PublicResearchError("DeepSeek 检索结果过大，请缩小研究范围后重试。")
                body.extend(chunk)
            return json.loads(body)

    async def _sources(self, data, name, domains, constrained_domains):
        blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise PublicResearchError("DeepSeek 联网研究结果格式无效，当前画像没有改变。")
        result_blocks = [block for block in blocks
                         if isinstance(block, dict) and block.get("type") == "web_search_tool_result"]
        if not result_blocks:
            raise PublicResearchError("DeepSeek 未返回联网检索结果，不能将模型回答当作公开资料。请稍后重试或粘贴资料。")

        excerpts = {}
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            citations = block.get("citations", [])
            if not isinstance(citations, list):
                continue
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                url, text = _safe_url(citation.get("url")), citation.get("cited_text")
                if url and isinstance(text, str) and text.strip() and url not in excerpts:
                    excerpts[url] = text.strip()[:10000]

        selected, seen, pages, page_seen = [], set(), [], set()
        found_results, found_excerpts, valid_blocks = False, False, False
        for block in result_blocks:
            rows = block.get("content")
            if not isinstance(rows, list):
                continue
            valid_blocks = True
            for row in rows:
                if not isinstance(row, dict) or row.get("type") != "web_search_result":
                    continue
                url, title = _safe_url(row.get("url")), row.get("title")
                if not url:
                    continue
                text = excerpts.get(url)
                if not text:
                    host = urlsplit(url).hostname.lower()
                    domain_match = any(host == domain or host.endswith("." + domain) for domain in domains)
                    if (self.page_reader is not None and url not in page_seen and len(pages) < 2
                            and (not constrained_domains or domain_match)):
                        pages.append(url)
                        page_seen.add(url)
                    if isinstance(title, str) and title.strip():
                        found_results = True
                    continue
                if not isinstance(title, str) or not title.strip():
                    continue
                found_results = True
                found_excerpts = True
                source = self._source(url, title, text, row.get("page_age"), self.clock(),
                                      name, domains, constrained_domains)
                if source is None or url in seen:
                    continue
                seen.add(url)
                selected.append(source)
                if len(selected) == 5:
                    return selected
        if not valid_blocks:
            raise PublicResearchError("DeepSeek 联网检索暂不可用，当前画像保留，请稍后重试。")
        if self.page_reader is not None:
            if pages:
                outcomes = await self._read_pages(pages, name, domains, constrained_domains)
                for read_ok, source in outcomes:
                    if source is not None and source["url"] not in seen:
                        selected.append(source)
                        seen.add(source["url"])
                if not selected and not found_excerpts and not any(read_ok for read_ok, _ in outcomes):
                    raise PublicResearchError("检索已找到链接，但网页正文读取超时或未完成，当前画像保留。可重试或粘贴公开原文。")
            # Successfully read pages that fail the final identity/domain check
            # produce no sources. Search-result titles cannot supply identity.
            return selected[:5]
        if found_results and not found_excerpts:
            raise PublicResearchError("DeepSeek 检索未返回可核对的引用原文，不能将模型回答写入画像。请重试或粘贴资料。")
        return selected

    def _source(self, url, title, text, published, fetched, name, domains, constrained_domains):
        title, text = title.strip()[:300], text.strip()[:10000]
        host = urlsplit(url).hostname.lower()
        domain_match = any(host == domain or host.endswith("." + domain) for domain in domains)
        if constrained_domains and not domain_match:
            return None
        normalize = lambda value: re.sub(r"\s+", "", value).casefold()
        if not domain_match and normalize(name) not in normalize(title + "\n" + text):
            return None
        return {"url": url, "title": title, "text": text,
                "published_at": _publication(published), "fetched_at": fetched, "entity_name": name,
                "identity_reason": "已限定用户核对的官网域名" if domain_match
                    else "出现单位正式全名，仍需核对同名机构"}

    async def _read_page(self, url, name, domains, constrained_domains):
        try:
            # This reader has its own credential-free aiohttp session and
            # per-hop public DNS validation. Only the URL enters its protocol.
            page = await self.page_reader.preview(url)
            if not isinstance(page, dict):
                return False, None
            final = _safe_url(page.get("url"))
            title, text = page.get("title"), page.get("text")
            if (not final or not isinstance(title, str) or not title.strip()
                    or not isinstance(text, str) or len(text.strip()) < 20):
                return False, None
            fetched = page.get("fetched_at")
            if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
                fetched = self.clock()
            source = self._source(final, title, text, page.get("published_at"), fetched,
                                  name, domains, constrained_domains)
            if source is not None:
                source["identity_reason"] += "；自动读取公开网页正文节选"
            return True, source
        except Exception:
            # Public readers may raise connection/encoding/schema errors. Never
            # echo them (or their URLs/body) and never catch task cancellation.
            return False, None

    async def _read_pages(self, urls, name, domains, constrained_domains):
        tasks = [asyncio.create_task(self._read_page(url, name, domains, constrained_domains))
                 for url in urls[:2]]
        done = set()
        try:
            done, _ = await asyncio.wait(tasks, timeout=_PAGE_TIMEOUT)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Drain cancellations so reader connections close before returning;
            # an outer cancellation still propagates after this cleanup.
            await asyncio.gather(*tasks, return_exceptions=True)
        return [task.result() if task in done and not task.cancelled() else (False, None) for task in tasks]
