"""Bounded public-company research; internal notes and people never form queries."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx


class PublicResearchError(ValueError):
    pass


def public_source_identity_reason(data, official):
    """Derive identity locally; preserve only the fixed page-excerpt marker."""
    reason = '已核对官方域名' if official else '全名命中，仍需排除同名单位'
    marker = '；自动读取公开网页正文节选'
    supplied = data.get('identity_reason')
    if isinstance(supplied, str) and supplied.endswith(marker):
        reason += marker
    return reason


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(c.isspace() for c in value):
        raise PublicResearchError('公开资料链接无效，请使用完整网页地址。')
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if parts.scheme not in ('https', 'http') or not host or parts.username or parts.password:
            raise ValueError()
        if parts.port not in (None, 80, 443):
            raise ValueError()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan')) or '.' not in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
    except (ValueError, UnicodeError):
        raise PublicResearchError('请填写公开网页链接，不能使用内网地址或含口令的地址。') from None


def normalize_domains(values):
    if not isinstance(values, list) or len(values) > 10:
        raise PublicResearchError('官网域名最多填写10个。')
    result = []
    for value in values:
        if not isinstance(value, str):
            raise PublicResearchError('官网域名格式无效。')
        value = value.strip().lower()
        if '://' in value:
            parts = urlsplit(public_url(value))
            if parts.query or parts.path not in ('', '/'):
                raise PublicResearchError('官网请填写域名，不用填写页面路径。')
            value = parts.hostname
        url = public_url('https://' + value)
        value = urlsplit(url).hostname
        if value not in result:
            result.append(value)
    return result


def _publication(value):
    if not isinstance(value, str) or len(value) > 120:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    try:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.isoformat()
    except (ValueError, OSError, OverflowError):
        return None


class TavilyResearcher:
    """Search an API endpoint, never fetch arbitrary returned URLs."""
    provider_id = 'tavily'
    def __init__(self, api_key, *, client=None, clock=time.time):
        self.api_key, self.client, self.clock = api_key, client, clock

    async def research(self, context):
        if not self.api_key:
            raise PublicResearchError('尚未配置公开资料检索服务，可先粘贴公开资料。')
        customer = context.get('customer', {})
        name = customer.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or '\x00' in name:
            raise PublicResearchError('请先核对单位正式名称，再检索公开资料。')
        settings = context.get('settings', {})
        domains = normalize_domains(context.get('research_domains', settings.get('official_domains', [])))
        mode, theme = context.get('mode', 'quick'), context.get('theme', 'overview')
        if mode not in ('quick', 'deep') or theme not in ('overview', 'background', 'digitalization', 'procurement'):
            raise PublicResearchError('公开研究模式无效。')
        terms = {'overview': '官网 机构简介 信息化 采购公告', 'background': '官网 机构简介 主营业务',
                 'digitalization': '数字化 信息化 数据安全 密码应用', 'procurement': '采购公告 招标 信息安全 密码'}
        # Procurement often lives on public tender platforms; full-name matching
        # remains required and never grants official-domain identity verification.
        constrained_domains = domains if theme not in ('digitalization', 'procurement') else []
        payload = {'query': name.strip() + ' ' + terms[theme],
                   'search_depth': 'basic', 'max_results': 5, 'topic': 'general',
                   'include_answer': False, 'include_raw_content': False,
                   'include_published_date': True, 'auto_parameters': False}
        if constrained_domains:
            payload['include_domains'] = constrained_domains
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                    data = await self._search(client, payload)
            else:
                data = await self._search(self.client, payload)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise PublicResearchError('公开资料检索暂未完成，原画像保留，可稍后重试或粘贴资料。') from None
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise PublicResearchError('检索结果格式无效，当前画像没有改变。')
        selected, seen = [], set()
        normalize = lambda text: re.sub(r'\s+', '', text).casefold()
        for row in results[:10]:
            if not isinstance(row, dict):
                continue
            try:
                url = public_url(row.get('url'))
            except PublicResearchError:
                continue
            title, text = row.get('title'), row.get('content')
            if not isinstance(title, str) or not isinstance(text, str) or not text.strip():
                continue
            host = urlsplit(url).hostname.lower()
            domain_match = any(host == domain or host.endswith('.' + domain) for domain in domains)
            if constrained_domains and not domain_match:
                continue
            name_match = normalize(name) in normalize(title + '\n' + text)
            if not name_match and not domain_match:
                continue
            if url in seen:
                continue
            seen.add(url)
            selected.append({'url': url, 'title': title[:300], 'text': text[:10000],
                             'published_at': _publication(row.get('published_date')),
                             'fetched_at': self.clock(), 'entity_name': name.strip(),
                             'identity_reason': '已限定用户核对的官网域名' if domain_match else '出现单位正式全名，仍需核对同名机构'})
        return selected[:5]

    async def _search(self, client, payload):
        response = await client.post('https://api.tavily.com/search',
                                     headers={'Authorization': 'Bearer ' + self.api_key},
                                     json=payload, timeout=20)
        response.raise_for_status()
        if len(response.content) > 500000:
            raise PublicResearchError('检索结果过大，请缩小公开资料范围。')
        return response.json()


def _configured_provider(config):
    if config.get('profile_search_api_key'):
        return 'tavily'
    if not config.get('api_key'):
        return None
    try:
        address = urlsplit(config.get('base_url', ''))
        if (address.scheme == 'https' and address.hostname == 'api.deepseek.com'
                and address.port in (None, 443) and not address.username
                and not address.password and not address.query and not address.fragment
                and address.path in ('', '/', '/v1', '/v1/')):
            return 'deepseek'
    except (TypeError, ValueError):
        pass
    return None


def make_public_researcher(config, *, client=None):
    """Reuse official DeepSeek credentials only; retain explicit search choice.

    A key for a custom model gateway is never forwarded to DeepSeek. Selecting a
    provider means configured, not a successful credential or search probe.
    """
    selected = _configured_provider(config)
    if selected == 'tavily':
        return TavilyResearcher(config['profile_search_api_key'], client=client)
    if selected == 'deepseek':
        from .deepseek_research import DeepSeekResearcher
        from .public_pages import PublicPageReader
        # Search can return structured URLs without plaintext citations. The
        # reader uses its own public-only connection and sends no model key.
        return DeepSeekResearcher(config['api_key'], client=client,
                                  page_reader=PublicPageReader())
    return None


def public_research_capabilities(researcher):
    provider = getattr(researcher, 'provider_id', None) if researcher is not None else None
    labels = {'tavily': 'Tavily 公开资料检索', 'deepseek': 'DeepSeek 原生联网研究'}
    return {'research_configured': researcher is not None,
            'research_provider': provider if provider in labels else ('custom' if researcher is not None else None),
            'research_label': labels.get(provider, '公开资料检索' if researcher is not None else '')}


def public_research_configuration(config):
    provider = _configured_provider(config)
    labels = {'tavily': 'Tavily 公开资料检索', 'deepseek': 'DeepSeek 原生联网研究'}
    label = labels.get(provider, '')
    return {'research_configured': provider is not None, 'research_provider': provider,
            'research_label': label,
            'research_status': label + '已配置；运行研究时验证可用性' if provider else
                '联网研究尚未配置；可用官方 DeepSeek API 或另接搜索服务'}
