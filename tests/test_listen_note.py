"""Contract oracles for the verified, read-only ListenNote MCP tools."""

import asyncio
import json

import httpx
import pytest

from secretary.listen_note import ListenNoteClient, ListenNoteError


TITLE = '客户甲 POC交流'
KEY = 'secret-only-in-request-header'
RAW = json.dumps([
    {'ChannelId': 0, 'StartTime': 80, 'EndTime': 1200, 'Text': '客户要求先验证性能。'},
    {'ChannelId': 1, 'StartTime': 1200, 'EndTime': 512720, 'Text': '我答应明天下午三点补测试方案。'},
], ensure_ascii=False)


def page(body, **changes):
    return {'nid': 'note-1', 'title': TITLE, 'createTime': '2026-09-30 14:30:00',
            'content': body, 'source': 'OPTIMIZED', 'hasMore': False, 'nextCursor': None,
            **changes}


def summary(**changes):
    return {'nid': 'note-1', 'title': TITLE, 'createTime': '2026-09-30 14:30:00',
            'summaryContent': '讨论了性能测试。', 'todoContent': '补充测试方案',
            'contentType': 'text', **changes}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class Server:
    """Independent scripted server: request shapes are checked, not derived."""
    def __init__(self, pages=None, *, summary_result=None, sse=False, text_result=False,
                 session=True):
        self.pages = list(pages if pages is not None else [page(RAW), page(RAW)])
        self.summary_result = summary_result if summary_result is not None else summary()
        self.sse, self.text_result, self.session = sse, text_result, session
        self.calls = []

    def response(self, identifier, result, headers=None):
        payload = {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        if not self.sse:
            return httpx.Response(200, json=payload, headers=headers)
        # A notification, a comment, CRLF, split Unicode bytes and multiline data
        # precede the matching result. The body remains a normal MCP SSE stream.
        encoded = json.dumps(payload, ensure_ascii=False)
        split = encoded.find(',') + 1
        text = (': ping\r\n\r\nevent: message\r\n'
                'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\r\n\r\n'
                'event: message\r\ndata: ' + encoded[:split] + '\r\ndata: ' + encoded[split:] + '\r\n\r\n')
        body = text.encode('utf-8')
        return httpx.Response(200, headers={'content-type': 'text/event-stream', **(headers or {})},
                              stream=Chunks([body[i:i + 7] for i in range(0, len(body), 7)]))

    def __call__(self, request):
        assert str(request.url) == 'https://collab.ctcdn.cn/mcp'
        assert request.method == 'POST'
        assert request.headers['authorization'] == 'Bearer ' + KEY
        assert 'application/json' in request.headers['accept']
        assert 'text/event-stream' in request.headers['accept']
        data = json.loads(request.content)
        assert data['jsonrpc'] == '2.0'
        assert KEY not in request.content.decode()
        self.calls.append(data)
        method = data['method']
        if method == 'initialize':
            assert 'mcp-session-id' not in request.headers
            assert data['params']['protocolVersion'] == '2025-03-26'
            assert data['params']['capabilities'] == {}
            headers = {'Mcp-Session-Id': 'opaque-session'} if self.session else None
            return self.response(data['id'], {'protocolVersion': '2025-03-26', 'capabilities': {},
                                             'serverInfo': {'name': 'office-mcp-center', 'version': '1.0.0'}}, headers)
        assert request.headers['mcp-protocol-version'] == '2025-03-26'
        if self.session:
            assert request.headers['mcp-session-id'] == 'opaque-session'
        if method == 'notifications/initialized':
            assert 'id' not in data
            return httpx.Response(202)
        assert method == 'tools/call'
        arguments = data['params']['arguments']
        assert arguments['title'] == TITLE
        if data['params']['name'] == 'note_get_transcript':
            assert arguments['limit'] == 20000
            assert set(arguments) in ({'title', 'limit'}, {'title', 'limit', 'cursor'})
            assert self.pages, 'unexpected extra transcript call'
            value = self.pages.pop(0)
        else:
            assert data['params']['name'] == 'note_get_summary'
            assert set(arguments) == {'title'}
            value = self.summary_result
        if isinstance(value, httpx.Response):
            return value
        if self.text_result:
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
        else:
            result = {'structuredContent': value, 'content': [], 'isError': False}
        return self.response(data['id'], result)


def fetch(server, title=TITLE, **options):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            return await ListenNoteClient(KEY, client=client, **options).fetch(title)
    return asyncio.run(run())


@pytest.mark.parametrize('sse,text_result,session', [(False, False, True), (True, False, True),
                                                    (False, True, False), (True, True, False)])
def test_verified_contract_json_sse_and_safe_normalized_result(sse, text_result, session):
    server = Server(sse=sse, text_result=text_result, session=session)
    result = fetch(server, '  ' + TITLE + '\n')
    assert result == {
        'nid': 'note-1', 'title': TITLE, 'create_time': '2026-09-30 14:30:00', 'source': 'OPTIMIZED',
        'raw_content': RAW, 'text': '客户要求先验证性能。\n\n我答应明天下午三点补测试方案。',
        'segments': [
            {'ordinal': 1, 'speaker': '0', 'text': '客户要求先验证性能。', 'start_raw': 80, 'end_raw': 1200},
            {'ordinal': 2, 'speaker': '1', 'text': '我答应明天下午三点补测试方案。', 'start_raw': 1200, 'end_raw': 512720},
        ], 'summary_content': '讨论了性能测试。', 'todo_content': '补充测试方案', 'content_type': 'text',
        'warnings': [],
    }
    names = [call['params']['name'] for call in server.calls if call['method'] == 'tools/call']
    assert names == ['note_get_transcript', 'note_get_transcript', 'note_get_summary']
    assert not any(call['method'] == 'tools/list' for call in server.calls)


def test_pagination_concatenates_json_at_field_and_escape_boundaries_without_separator():
    raw = '[{"ChannelId":3,"StartTime":80,"EndTime":900,"Text":"第一行\\n第二行🙂"}]'
    cut = raw.index('\\n') + 1
    pages = [page(raw[:cut], hasMore=True, nextCursor='opaque-甲'), page(raw[cut:]),
             page(raw[:cut], hasMore=True, nextCursor='opaque-甲'), page(raw[cut:])]
    server = Server(pages)
    result = fetch(server)
    assert result['raw_content'] == raw
    assert result['segments'][0]['text'] == '第一行\n第二行🙂'
    assert result['segments'][0]['start_raw'] == 80
    transcript_args = [call['params']['arguments'] for call in server.calls
                       if call.get('params', {}).get('name') == 'note_get_transcript']
    assert ['cursor' in item for item in transcript_args] == [False, True, False, True]
    assert transcript_args[1]['cursor'] == transcript_args[3]['cursor'] == 'opaque-甲'


def test_plain_text_falls_back_to_paragraphs_without_people_or_timestamps():
    raw = '第一段客户诉求。\n\n第二段跟进记录。'
    result = fetch(Server([page(raw, source='ORIGINAL'), page(raw, source='ORIGINAL')]))
    assert result['raw_content'] == raw
    assert result['source'] == 'ORIGINAL'
    assert result['segments'] == [
        {'ordinal': 1, 'speaker': None, 'text': '第一段客户诉求。', 'start_raw': None, 'end_raw': None},
        {'ordinal': 2, 'speaker': None, 'text': '第二段跟进记录。', 'start_raw': None, 'end_raw': None},
    ]


@pytest.mark.parametrize('raw', ['2026', 'null', 'true', '"口述数字"'])
def test_short_ordinary_transcript_is_not_mistaken_for_json_scalar(raw):
    result = fetch(Server([page(raw), page(raw)]))
    assert result['text'] == result['raw_content'] == raw
    assert result['segments'][0]['speaker'] is None


@pytest.mark.parametrize('title', ['', ' \n', 'x' * 101, None, 4, 'bad\x00title'])
def test_invalid_title_never_calls_network(title):
    server = Server()
    with pytest.raises(ListenNoteError) as failure:
        fetch(server, title)
    assert failure.value.code == 'invalid_title'
    assert not failure.value.retryable
    assert server.calls == []


def test_missing_configuration_is_explicit_and_never_contacts_server():
    secretary = ListenNoteClient('  ')
    assert not secretary.configured
    assert ListenNoteClient(KEY).configured
    with pytest.raises(ListenNoteError) as failure:
        asyncio.run(secretary.fetch(TITLE))
    assert failure.value.code == 'not_configured'


@pytest.mark.parametrize('change', [{'nid': 'other-note'}, {'title': 'Other title'},
                                   {'source': 'ORIGINAL'}, {'createTime': 'other-created-time'}])
def test_page_identity_changes_cannot_be_joined(change):
    server = Server([page('first', hasMore=True, nextCursor='next'), page('second', **change)])
    with pytest.raises(ListenNoteError) as failure:
        fetch(server)
    assert failure.value.code == 'content_changed'
    assert failure.value.retryable
    assert len(server.calls) == 4


@pytest.mark.parametrize('pages', [
    [page('a', hasMore=True, nextCursor=None)],
    [page('a', hasMore=True, nextCursor='')],
    [page('a', hasMore=True, nextCursor='a'), page('b', hasMore=True, nextCursor='a')],
    [page('a', hasMore=True, nextCursor='a'), page('b', hasMore=True, nextCursor='b'),
     page('c', hasMore=True, nextCursor='a')],
])
def test_missing_or_repeated_cursor_stops_without_summary(pages):
    server = Server(pages)
    with pytest.raises(ListenNoteError) as failure:
        fetch(server)
    assert failure.value.code == 'pagination_invalid'
    assert not any(call.get('params', {}).get('name') == 'note_get_summary' for call in server.calls)


@pytest.mark.parametrize('second', [page(RAW + ' '), page(RAW, nid='another'), page(RAW, source='ORIGINAL')])
def test_second_complete_read_detects_changes_before_results_or_summary(second):
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page(RAW), second]))
    assert failure.value.code == 'content_changed'
    assert failure.value.retryable


def test_summary_is_optional_and_a_different_identity_is_never_mixed():
    for optional in [summary(nid='other'), summary(title='other'),
                     {'nid': 'note-1'}, httpx.Response(500, text=KEY)]:
        result = fetch(Server(summary_result=optional))
        assert result['text'].startswith('客户要求')
        assert result['summary_content'] == result['todo_content'] == result['content_type'] == ''
        assert result['warnings']
        assert KEY not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('changes', [{'hasMore': 1}, {'hasMore': 'false'}, {'nextCursor': []},
                                    {'source': 'UNKNOWN'}, {'content': []}, {'nid': ''},
                                    {'createTime': None}, {'content': 'bad\x00content'}])
def test_malformed_tool_fields_have_safe_errors(changes):
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page(RAW, **changes)]))
    assert failure.value.code == 'invalid_result'
    assert KEY not in str(failure.value)


@pytest.mark.parametrize('raw', ['', ' \n ', '[]'])
def test_empty_transcript_is_not_interpreted_as_completed_with_no_actions(raw):
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page(raw), page(raw)]))
    assert failure.value.code == 'transcript_empty'
    assert failure.value.retryable


@pytest.mark.parametrize('raw', ['[{"Text":"partial"}', '[{"Text":null}]',
                               '[{"Text":"a","Text":"b"}]', '[{"Text":"a","StartTime":NaN}]'])
def test_incomplete_or_unsafe_json_is_not_downgraded_to_apparently_complete_text(raw):
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page(raw), page(raw)]))
    assert failure.value.code == 'invalid_result'


def test_char_and_page_limits_are_explicit_not_truncation():
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page('123456')]), max_chars=5)
    assert failure.value.code == 'limit_exceeded'
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page('a', hasMore=True, nextCursor='a')]), max_pages=1)
    assert failure.value.code == 'limit_exceeded'
    with pytest.raises(ListenNoteError) as failure:
        fetch(Server([page('x' * 20001)]))
    assert failure.value.code == 'limit_exceeded'


@pytest.mark.parametrize('status', [301, 401, 403, 404, 429, 500])
def test_non_success_http_never_follows_redirect_or_exposes_body(status):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={'location': 'https://untrusted.invalid/' + KEY}, text=KEY)
    with pytest.raises(ListenNoteError) as failure:
        fetch(handler)
    assert KEY not in str(failure.value)
    assert calls == ['https://collab.ctcdn.cn/mcp']
    assert failure.value.code == ('authentication_failed' if status in (401, 403) else 'http_error')


def test_transport_exception_payload_and_bad_json_never_escape():
    def fail(_request):
        raise httpx.ReadTimeout(KEY)
    with pytest.raises(ListenNoteError) as failure:
        fetch(fail)
    assert KEY not in str(failure.value)
    assert failure.value.code == 'connection_failed'
    for value in [KEY, '{}', '[]', '{"jsonrpc":"2.0","id":1,"result":null}',
                  '{"jsonrpc":"2.0","id":1,"error":{"message":"' + KEY + '"}}']:
        with pytest.raises(ListenNoteError) as failure:
            fetch(lambda _request: httpx.Response(200, text=value, headers={'content-type': 'application/json'}))
        assert KEY not in str(failure.value)


def test_sse_without_matching_response_and_response_budget_are_safe():
    for body in [b': ping\n\n', b'data: not-json\n\n',
                 b'data: {"jsonrpc":"2.0","id":999,"result":{}}\n\n']:
        with pytest.raises(ListenNoteError):
            fetch(lambda _request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body))
    with pytest.raises(ListenNoteError) as failure:
        fetch(lambda _request: httpx.Response(200, headers={'content-type': 'application/json'},
                                             content=b' ' * (2 * 1024 * 1024 + 1)))
    assert failure.value.code == 'limit_exceeded'


def test_rpc_tool_error_and_mismatched_rpc_id_are_not_success():
    for value in [
        {'jsonrpc': '2.0', 'id': 999, 'result': {}},
        {'jsonrpc': '2.0', 'id': True, 'result': {}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': 'unsupported'}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': []}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': {KEY: KEY}}},
    ]:
        with pytest.raises(ListenNoteError):
            fetch(lambda _request: httpx.Response(200, json=value))
    server = Server()
    original = server.response
    def response(identifier, result, headers=None):
        if 'structuredContent' in result:
            result = {'isError': True, 'content': [{'type': 'text', 'text': KEY}]}
        return original(identifier, result, headers)
    server.response = response
    with pytest.raises(ListenNoteError) as failure:
        fetch(server)
    assert failure.value.code == 'tool_failed'
    assert KEY not in str(failure.value)


@pytest.mark.parametrize('exception', [RuntimeError(KEY), ValueError(KEY), httpx.RemoteProtocolError(KEY)])
def test_client_failures_do_not_expose_private_payload(exception):
    def handler(_request):
        raise exception
    with pytest.raises(ListenNoteError) as failure:
        fetch(handler)
    assert KEY not in str(failure.value)
    assert failure.value.code == 'connection_failed'


def test_separate_fetches_do_not_reuse_other_requests_sessions():
    async def run():
        # Both readers intentionally receive the same opaque ID from the mock;
        # each must still perform its own handshake, without a prior session.
        server = Server([page(RAW), page(RAW), page(RAW), page(RAW)])
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            adapter = ListenNoteClient(KEY, client=client)
            first = await adapter.fetch(TITLE)
            second = await adapter.fetch(TITLE)
        assert first == second
        assert sum(call['method'] == 'initialize' for call in server.calls) == 2
    asyncio.run(run())
