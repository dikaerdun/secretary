import asyncio

import pytest
from aiohttp.resolver import ThreadedResolver

from secretary.public_pages import PublicPageReader, PublicResolver, MAX_BYTES
from secretary.public_research import PublicResearchError


class Content:
    def __init__(self, body):
        self.body = body
    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start+size]


class Reply:
    def __init__(self, body=b'', status=200, **headers):
        self.status, self.headers, self.content, self.charset = status, {'Content-Type': 'text/html', **headers}, Content(body), 'utf-8'
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


class Session:
    def __init__(self, replies): self.replies, self.calls = replies, []
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    def get(self, url, **options):
        self.calls.append((url, options))
        assert options == {'allow_redirects': False}
        return self.replies.pop(0)


def test_public_html_preserves_text_and_dates_without_scripts():
    async def run():
        session = Session([Reply('<html><title>合成官网简介</title><meta property="article:published_time" content="2024-01-02"><script>secret instruction</script><p>合成单位位于杭州，暂无公开预算，条件待核实。</p></html>'.encode())])
        result = await PublicPageReader(session_factory=lambda: session, clock=lambda: 100).preview('https://official.example/a')
        assert result['title'] == '合成官网简介'
        assert '暂无公开预算' in result['text'] and 'secret instruction' not in result['text']
        assert result['published_at'].startswith('2024-01-02') and result['fetched_at'] == 100
        assert '尚未写入画像' in result['warning']
    asyncio.run(run())


@pytest.mark.parametrize('url', ['http://127.0.0.1/a', 'http://192.168.1.131/a', 'https://user:pass@official.example/a', 'http://localhost/a', 'http://[::1]/a'])
def test_private_urls_never_reach_connection(url):
    session = Session([])
    with pytest.raises(PublicResearchError):
        asyncio.run(PublicPageReader(session_factory=lambda: session).preview(url))
    assert session.calls == []


def test_redirect_revalidates_and_stream_is_bounded():
    async def run():
        session = Session([Reply(status=302, Location='http://192.168.1.131/secret')])
        with pytest.raises(PublicResearchError):
            await PublicPageReader(session_factory=lambda: session).preview('https://official.example/a')
        assert len(session.calls) == 1
        session = Session([Reply(b'x'*(MAX_BYTES+1))])
        with pytest.raises(PublicResearchError, match='过大'):
            await PublicPageReader(session_factory=lambda: session).preview('https://official.example/a')
    asyncio.run(run())


def test_connecting_resolver_rejects_dns_rebinding_and_mixed_private_answer(monkeypatch):
    async def run():
        async def fake(_self, _host, _port, _family):
            return [{'hostname': 'public.example', 'host': '8.8.8.8', 'port': 443}, {'hostname': 'public.example', 'host': '10.1.2.3', 'port': 443}]
        monkeypatch.setattr(ThreadedResolver, 'resolve', fake)
        resolver = PublicResolver()
        with pytest.raises(PublicResearchError, match='非公开'):
            await resolver.resolve('public.example', 443)
        await resolver.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['text/html', 'text/plain'])
def test_long_page_exposes_excerpt_and_missing_tail(kind):
    async def run():
        text = '公开说明' + '内容' * 11000 + '末尾撤回说明'
        body = ('<p>' + text + '</p>' if kind == 'text/html' else text).encode()
        session = Session([Reply(body, **{'Content-Type': kind})])
        result = await PublicPageReader(session_factory=lambda: session).preview('https://official.example/long')
        assert result['truncated'] is True and result['original_length'] == len(text)
        assert len(result['text']) == 20000 and '末尾撤回说明' not in result['text']
        assert '正文节选' in result['warning'] and '撤回说明' in result['warning']
    asyncio.run(run())
