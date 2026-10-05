import asyncio
import json

import httpx
import pytest

from secretary.public_research import PublicResearchError, TavilyResearcher, normalize_domains, public_url


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://localhost/x', 'http://127.0.0.1/x',
    'http://192.168.1.131/x', 'http://10.0.0.2/x', 'https://example.com:8765/',
    'https://name:secret@example.com/', 'https://company.local/', 'https://[::1]/'])
def test_private_and_executable_sources_rejected(url):
    with pytest.raises(PublicResearchError):
        public_url(url)


def test_domains_and_dates_are_bounded():
    assert normalize_domains(['Example.com', 'https://example.com/']) == ['example.com']
    with pytest.raises(PublicResearchError):
        normalize_domains(['https://example.com/private/page'])


def test_research_queries_only_public_company_and_filters_entity_domains():
    async def run():
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'results': [
                {'url': 'https://bank.example/about', 'title': '单位简介', 'content': '金融机构行业介绍', 'published_date': '2026-09-01'},
                {'url': 'https://bank.example/repeated', 'title': '禾川银行', 'content': '禾川银行预算旧公告'},
                {'url': 'https://fake-bank.example/no', 'title': '禾川银行', 'content': '同名机构'},
                {'url': 'http://127.0.0.1/admin', 'title': '禾川银行', 'content': '私人数据'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TavilyResearcher('test-key', client=client, clock=lambda: 1000)
            result = await provider.research({'customer': {'name': '禾川银行', 'phone': 'private-phone'},
                'settings': {'official_domains': ['bank.example']},
                'source': {'text': '内部签名预算28万元'}, 'contacts': [{'name': '蒋工'}]})
        assert len(result) == 2
        assert result[0]['published_at'] is not None and result[0]['fetched_at'] == 1000
        assert all(item['entity_name'] == '禾川银行' for item in result)
        sent = json.dumps(requests, ensure_ascii=False)
        assert '蒋工' not in sent and '28万元' not in sent and 'private-phone' not in sent
        assert requests[0]['include_answer'] is False
        assert requests[0]['include_domains'] == ['bank.example']
    asyncio.run(run())


def test_same_name_without_canonical_company_is_not_selected():
    async def run():
        def handler(request):
            return httpx.Response(200, json={'results': [
                {'url': 'https://other.example/a', 'title': '禾川', 'content': '禾川研究院'},
                {'url': 'https://unit.example/a', 'title': '禾川银行', 'content': '禾川银行机构简介'},
                {'url': 'https://unit.example/a', 'title': '重复', 'content': '禾川银行'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await TavilyResearcher('test', client=client).research({'customer': {'name': '禾川银行'}})
        assert len(result) == 1 and '同名' in result[0]['identity_reason']
    asyncio.run(run())


def test_network_failure_never_exposes_key_or_provider_payload():
    async def run():
        def handler(request):
            return httpx.Response(401, json={'error': 'secret-test-key'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PublicResearchError) as caught:
                await TavilyResearcher('secret-test-key', client=client).research({'customer': {'name': '禾川银行'}})
        assert 'secret-test-key' not in str(caught.value)
    asyncio.run(run())
