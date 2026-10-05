"""Read a user-supplied public text page without accessing private networks.

The connecting resolver validates every resolved address (no preflight DNS
race), redirects are validated per hop, and downloaded text is bounded.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin

import aiohttp
from aiohttp.resolver import ThreadedResolver

from .public_research import public_url, PublicResearchError, _publication

MAX_BYTES = 256_000


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await super().resolve(host, port, family)
        if not rows or any(not ipaddress.ip_address(row['host'].split('%')[0]).is_global for row in rows):
            raise PublicResearchError('该网页解析到了非公开地址，不能读取；可粘贴已核对的公开原文。')
        return rows


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.title_depth, self.title, self.parts, self.published = 0, 0, [], [], None

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg', 'iframe'):
            self.skip += 1
        if tag == 'title':
            self.title_depth += 1
        if tag == 'meta':
            data = dict(attrs)
            if (data.get('property') or data.get('name') or '').lower() in ('article:published_time', 'datepublished', 'pubdate'):
                self.published = _publication(data.get('content')) or self.published
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg', 'iframe'):
            self.skip = max(0, self.skip-1)
        if tag == 'title':
            self.title_depth = max(0, self.title_depth-1)
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        if self.title_depth:
            self.title.append(data)
        else:
            self.parts.append(data)

    def result(self):
        lines = [re.sub(r'[\t \r\f\v]+', ' ', line).strip() for line in ''.join(self.parts).split('\n')]
        return ''.join(self.title).strip()[:500], '\n'.join(line for line in lines if line), self.published


class PublicPageReader:
    def __init__(self, *, session_factory=None, clock=time.time):
        self.session_factory, self.clock = session_factory, clock
        self.slots = asyncio.Semaphore(2)

    async def preview(self, url):
        url = public_url(url)
        if self.slots.locked():
            raise PublicResearchError('已有网页正在读取，请稍后再读取这份资料。')
        async with self.slots:
            return await self._preview(url)

    async def _preview(self, url):
        try:
            async with asyncio.timeout(18):
                if self.session_factory:
                    async with self.session_factory() as session:
                        return await self._read(session, url)
                connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, limit=2)
                async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15),
                    cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    headers={'User-Agent': 'SecretaryPublicReader/1.0', 'Accept': 'text/html,text/plain'}) as session:
                    return await self._read(session, url)
        except PublicResearchError:
            raise
        except (aiohttp.ClientError, TimeoutError, UnicodeError, LookupError):
            raise PublicResearchError('网页暂时无法读取，可重试或粘贴原文；当前资料没有改变。') from None

    async def _read(self, session, initial):
        url = initial
        for hop in range(4):
            async with session.get(url, allow_redirects=False) as result:
                if result.status in (301, 302, 303, 307, 308):
                    location = result.headers.get('Location')
                    if not location or hop == 3:
                        raise PublicResearchError('网页跳转过多或地址无效，请粘贴最终公开页面地址。')
                    url = public_url(urljoin(url, location))
                    continue
                if result.status != 200:
                    raise PublicResearchError('网页需要登录或暂不可用，请粘贴可公开访问的原文。')
                kind = result.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if kind not in ('text/html', 'text/plain', 'application/xhtml+xml'):
                    raise PublicResearchError('当前只读取公开文字网页；PDF或附件请先复制文字。')
                size = result.headers.get('Content-Length', '')
                if size.isdigit() and int(size) > MAX_BYTES:
                    raise PublicResearchError('网页正文过大，请粘贴相关段落。')
                body = bytearray()
                async for chunk in result.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise PublicResearchError('网页正文过大，请粘贴相关段落。')
                encoding = result.charset or 'utf-8'
                text = bytes(body).decode(encoding, errors='replace')
                published = None
                if kind == 'text/plain':
                    title, text = '公开网页正文', text.strip()
                else:
                    parser = PageText()
                    parser.feed(text)
                    title, text, published = parser.result()
                if not text or len(text.strip()) < 20:
                    raise PublicResearchError('网页没有足够正文，可能需要登录或脚本加载；请粘贴原文。')
                original_length = len(text)
                truncated = original_length > 20000
                return {'url': url, 'title': title or '公开网页正文', 'text': text[:20000],
                        'truncated': truncated, 'original_length': original_length,
                        'published_at': published, 'fetched_at': self.clock(),
                        'warning': ('当前为正文节选（前20000字），末尾内容尚未读取，请打开来源核对限制或撤回说明。' if truncated else '网页正文已读取。') + '尚未写入画像。请核对单位身份、年份及正文，再保存为公开观察。'}
        raise PublicResearchError('网页跳转过多，请粘贴最终地址。')
