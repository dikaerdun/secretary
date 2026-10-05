"""Read the two verified ListenNote MCP tools without executing CRM actions.

A successful fetch preserves a locally captured text, not an atomic platform
snapshot. Two complete reads provide a limited change check. Transcript offsets
and creation times remain uninterpreted because their units/timezone are unknown.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any

import httpx


ENDPOINT = 'https://collab.ctcdn.cn/mcp'
PROTOCOL_VERSION = '2025-03-26'
SUPPORTED_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18'}
PAGE_CHARS = 20_000
MAX_CONTENT_CHARS = 500_000
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SUMMARY_WARNING = '聆记摘要暂未纳入本次整理；本次仅依据已完整读取的转写正文。'
_MESSAGES = {
    'invalid_title': '请复制粘贴聆记的完整标题，去除首尾空格后须为 1 至 100 字符。',
    'not_configured': '还没有配置聆记只读连接，请先在服务器配置后重试。',
    'invalid_configuration': '聆记连接配置无效，请检查服务器设置。',
    'authentication_failed': '聆记连接认证失败，请检查连接权限后重试。',
    'http_error': '聆记服务暂时无法读取，请检查服务状态、标题和权限后重试。',
    'connection_failed': '聆记连接暂时失败，请稍后重试；没有建立任何待办或提醒。',
    'protocol_error': '聆记返回的连接格式暂不兼容，请稍后重试或检查服务配置。',
    'tool_failed': '聆记暂未返回可读取的材料，请核对标题、权限和转写状态后重试。',
    'invalid_result': '聆记转写格式暂时无法核实，请稍后重试；没有使用不完整内容。',
    'pagination_invalid': '聆记分页未能完整读取，请稍后重新读取；没有使用缺页内容。',
    'limit_exceeded': '这份聆记超过本次完整读取上限，请拆分材料后再试；内容没有被截断整理。',
    'content_changed': '聆记内容或同名来源在读取期间发生变化，请确认转写完成后重新导入。',
    'transcript_empty': '聆记尚未返回有效转写，请在聆记内核对转写状态、标题和权限后重试。',
}


class ListenNoteError(ValueError):
    """Safe user-facing failure; never retain a provider body or secret."""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(_MESSAGES.get(code, _MESSAGES['invalid_result']))


@dataclass
class _Session:
    identifier: str | None = None
    version: str | None = None
    next_id: int = 1


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError('non-finite number')


def _json(value: str):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


class ListenNoteClient:
    def __init__(self, api_key: str, endpoint: str = ENDPOINT,
                 client: httpx.AsyncClient | None = None, *,
                 max_chars: int = MAX_CONTENT_CHARS, max_pages: int = MAX_PAGES):
        self.api_key = api_key.strip() if isinstance(api_key, str) else ''
        self.endpoint = endpoint
        self.client = client
        self.max_chars, self.max_pages = max_chars, max_pages

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def fetch(self, title: str) -> dict:
        """Fetch a precise title, preserving text and raw offset values.

        Each fetch owns its MCP session, including when callers share an injected
        HTTP client. No private records are discovered or fetched by guessed IDs.
        """
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100
                or '\x00' in title):
            raise ListenNoteError('invalid_title')
        title = title.strip()
        if not self.configured:
            raise ListenNoteError('not_configured')
        self._validate_configuration()
        if self.client is None:
            async with httpx.AsyncClient(timeout=40, follow_redirects=False, trust_env=False) as client:
                return await self._fetch(client, title)
        return await self._fetch(self.client, title)

    def _validate_configuration(self):
        try:
            url = httpx.URL(self.endpoint)
            if (url.scheme != 'https' or not url.host or url.userinfo or url.query or url.fragment
                    or type(self.max_chars) is not int or not 1 <= self.max_chars <= MAX_CONTENT_CHARS
                    or type(self.max_pages) is not int or not 1 <= self.max_pages <= MAX_PAGES
                    or '\r' in self.api_key or '\n' in self.api_key):
                raise ValueError()
        except (TypeError, ValueError, httpx.InvalidURL):
            raise ListenNoteError('invalid_configuration') from None

    async def _fetch(self, client: httpx.AsyncClient, title: str) -> dict:
        session = _Session()
        initialized = await self._rpc(client, session, 'initialize', {
            'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
            'clientInfo': {'name': 'personal-wecom-secretary', 'version': '0.1.0'},
        })
        if (not isinstance(initialized, dict)
                or not isinstance(initialized.get('protocolVersion'), str)
                or initialized['protocolVersion'] not in SUPPORTED_VERSIONS):
            raise ListenNoteError('protocol_error')
        session.version = initialized['protocolVersion']
        await self._rpc(client, session, 'notifications/initialized', notification=True)
        first = await self._transcript(client, session, title)
        second = await self._transcript(client, session, title)
        identity = ('nid', 'title', 'create_time', 'source')
        if (any(first[key] != second[key] for key in identity)
                or hashlib.sha256(first['raw_content'].encode('utf-8')).digest()
                != hashlib.sha256(second['raw_content'].encode('utf-8')).digest()):
            raise ListenNoteError('content_changed', retryable=True)
        segments, text = self._segments(first['raw_content'])
        result = {**first, 'segments': segments, 'text': text,
                  'summary_content': '', 'todo_content': '', 'content_type': '', 'warnings': []}
        try:
            summary = await self._tool(client, session, 'note_get_summary', {'title': title})
            if (self._metadata(summary, title)['nid'] != result['nid']
                    or summary['title'] != result['title']):
                raise ListenNoteError('content_changed', retryable=True)
            for field in ('summaryContent', 'todoContent', 'contentType'):
                if (not isinstance(summary.get(field), str) or '\x00' in summary[field]
                        or len(summary[field]) > self.max_chars):
                    raise ListenNoteError('invalid_result')
            result.update(summary_content=summary['summaryContent'], todo_content=summary['todoContent'],
                          content_type=summary['contentType'])
        except ListenNoteError:
            result['warnings'].append(_SUMMARY_WARNING)
        return result

    async def _transcript(self, client: httpx.AsyncClient, session: _Session, title: str) -> dict:
        pieces, cursors = [], set()
        cursor, identity, total = None, None, 0
        for _index in range(self.max_pages):
            arguments = {'title': title, 'limit': PAGE_CHARS}
            if cursor is not None:
                arguments['cursor'] = cursor
            page = await self._tool(client, session, 'note_get_transcript', arguments)
            metadata = self._metadata(page, title)
            source = page.get('source')
            content = page.get('content')
            more, next_cursor = page.get('hasMore'), page.get('nextCursor')
            if (source not in ('OPTIMIZED', 'ORIGINAL') or not isinstance(content, str)
                    or '\x00' in content or type(more) is not bool
                    or (next_cursor is not None and not isinstance(next_cursor, str))):
                raise ListenNoteError('invalid_result')
            current = {**metadata, 'source': source}
            if identity is not None and current != identity:
                raise ListenNoteError('content_changed', retryable=True)
            identity = current
            total += len(content)
            if total > self.max_chars or len(content) > PAGE_CHARS:
                raise ListenNoteError('limit_exceeded')
            pieces.append(content)
            if not more:
                raw = ''.join(pieces)
                if not raw.strip():
                    raise ListenNoteError('transcript_empty', retryable=True)
                return {**identity, 'raw_content': raw}
            if (not next_cursor or len(next_cursor) > 2048 or next_cursor in cursors):
                raise ListenNoteError('pagination_invalid', retryable=True)
            cursors.add(next_cursor)
            cursor = next_cursor
        raise ListenNoteError('limit_exceeded')

    @staticmethod
    def _metadata(value: dict, expected_title: str) -> dict:
        fields = (('nid', 512), ('title', 1000), ('createTime', 100))
        for key, limit in fields:
            item = value.get(key)
            if (not isinstance(item, str) or not item.strip() or len(item) > limit or '\x00' in item):
                raise ListenNoteError('invalid_result')
        if value['title'].strip() != expected_title:
            raise ListenNoteError('content_changed', retryable=True)
        return {'nid': value['nid'], 'title': value['title'], 'create_time': value['createTime']}

    @staticmethod
    def _segments(raw: str) -> tuple[list[dict], str]:
        if not raw.lstrip().startswith(('[', '{')):
            # Ordinary text such as "2026" or "null" must not turn into a
            # JSON scalar and disappear. Only a container is structured data.
            paragraphs = [piece.strip() for piece in re.split(r'(?:\r?\n\s*){2,}', raw) if piece.strip()]
            segments = [{'ordinal': index, 'speaker': None, 'text': paragraph,
                         'start_raw': None, 'end_raw': None}
                        for index, paragraph in enumerate(paragraphs, 1)]
        else:
            try:
                data = _json(raw)
            except (ValueError, TypeError, RecursionError):
                raise ListenNoteError('invalid_result') from None
            if not isinstance(data, list):
                raise ListenNoteError('invalid_result')
            segments = []
            for index, item in enumerate(data, 1):
                if (not isinstance(item, dict) or not isinstance(item.get('Text'), str)
                        or '\x00' in item['Text']):
                    raise ListenNoteError('invalid_result')
                channel = item.get('ChannelId')
                speaker = str(channel) if type(channel) in (str, int) else None
                offsets = []
                for key in ('StartTime', 'EndTime'):
                    value = item.get(key)
                    if (value is not None and (type(value) not in (int, float, str)
                            or (isinstance(value, float) and not math.isfinite(value)))):
                        raise ListenNoteError('invalid_result')
                    offsets.append(value)
                segments.append({'ordinal': index, 'speaker': speaker, 'text': item['Text'],
                                 'start_raw': offsets[0], 'end_raw': offsets[1]})
        text = '\n\n'.join(item['text'] for item in segments)
        if not text.strip():
            raise ListenNoteError('transcript_empty', retryable=True)
        return segments, text

    async def _tool(self, client, session, name, arguments) -> dict:
        # The names are fixed by callers, never taken from model output or text.
        if name not in ('note_get_transcript', 'note_get_summary'):
            raise ListenNoteError('protocol_error')
        result = await self._rpc(client, session, 'tools/call', {'name': name, 'arguments': arguments})
        if not isinstance(result, dict) or type(result.get('isError', False)) is not bool:
            raise ListenNoteError('protocol_error')
        if result.get('isError'):
            raise ListenNoteError('tool_failed', retryable=True)
        value = result.get('structuredContent')
        if value is None:
            content = result.get('content')
            if not isinstance(content, list) or not content:
                raise ListenNoteError('invalid_result')
            try:
                parts = [item['text'] for item in content
                         if isinstance(item, dict) and item.get('type') == 'text']
                if not parts or not all(isinstance(item, str) for item in parts):
                    raise ValueError()
                value = _json('\n'.join(parts))
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ListenNoteError('invalid_result') from None
        if not isinstance(value, dict):
            raise ListenNoteError('invalid_result')
        return value

    async def _rpc(self, client, session, method, params=None, *, notification=False):
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        identifier = None
        if not notification:
            identifier = session.next_id
            session.next_id += 1
            payload['id'] = identifier
        headers = {'Authorization': 'Bearer ' + self.api_key,
                   'Accept': 'application/json, text/event-stream'}
        if session.version:
            headers['MCP-Protocol-Version'] = session.version
        if session.identifier:
            headers['Mcp-Session-Id'] = session.identifier
        try:
            async with client.stream('POST', self.endpoint, headers=headers, json=payload,
                                     timeout=40, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    if response.status_code in (401, 403):
                        raise ListenNoteError('authentication_failed')
                    raise ListenNoteError('http_error', retryable=(response.status_code == 429
                                                                 or response.status_code >= 500))
                if method == 'initialize':
                    received = response.headers.get('Mcp-Session-Id')
                    if received is not None:
                        if (not 1 <= len(received) <= 2048
                                or any(ord(char) < 33 or ord(char) > 126 for char in received)):
                            raise ListenNoteError('protocol_error')
                        session.identifier = received
                if notification:
                    return None
                content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if content_type == 'text/event-stream':
                    return await self._sse(response, identifier)
                if content_type != 'application/json' and not content_type.endswith('+json'):
                    raise ListenNoteError('protocol_error')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ListenNoteError('limit_exceeded')
                try:
                    data = _json(body.decode('utf-8-sig'))
                except (UnicodeError, ValueError, TypeError, RecursionError):
                    raise ListenNoteError('protocol_error') from None
                return self._rpc_result(data, identifier)
        except ListenNoteError:
            raise
        except (httpx.HTTPError, UnicodeError, OSError, RuntimeError, TypeError, ValueError):
            raise ListenNoteError('connection_failed', retryable=True) from None

    @staticmethod
    def _rpc_result(data: Any, identifier: int, *, other_messages=False):
        if not isinstance(data, dict) or data.get('jsonrpc') != '2.0':
            raise ListenNoteError('protocol_error')
        if type(data.get('id')) is not int or data['id'] != identifier:
            if other_messages:
                return None
            raise ListenNoteError('protocol_error')
        if 'error' in data or 'result' not in data:
            raise ListenNoteError('tool_failed', retryable=True)
        return data['result']

    async def _sse(self, response, identifier):
        decoder = codecs.getincrementaldecoder('utf-8-sig')()
        buffer, event_data, size = '', [], 0

        def line(value):
            if value == '':
                if not event_data:
                    return None
                encoded = '\n'.join(event_data)
                event_data.clear()
                try:
                    data = _json(encoded)
                except (ValueError, TypeError, RecursionError):
                    raise ListenNoteError('protocol_error') from None
                return self._rpc_result(data, identifier, other_messages=True)
            if value.startswith('data:'):
                value = value[5:]
                event_data.append(value[1:] if value.startswith(' ') else value)
            return None

        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ListenNoteError('limit_exceeded')
            buffer += decoder.decode(chunk)
            while True:
                match = re.search(r'\r\n|\r|\n', buffer)
                if match is None or (match[0] == '\r' and match.end() == len(buffer)):
                    break
                result = line(buffer[:match.start()])
                buffer = buffer[match.end():]
                if result is not None:
                    return result
        buffer += decoder.decode(b'', final=True)
        for value in re.split(r'\r\n|\r|\n', buffer):
            result = line(value)
            if result is not None:
                return result
        result = line('')
        if result is not None:
            return result
        raise ListenNoteError('protocol_error')
