import asyncio
from datetime import datetime

import pytest
import httpx

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.organizer import InteractionOrganizer
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 1, 10, tzinfo=SHANGHAI).timestamp()


class Connector:
    def __init__(self, text='我答应明天下午三点发送方案。', nid='external-1'):
        self.text, self.nid, self.calls = text, nid, 0

    async def fetch(self, title):
        self.calls += 1
        return {'nid': self.nid, 'title': title, 'create_time': '2026-09-30T10:00:00',
                'source': 'OPTIMIZED', 'raw_content': self.text, 'text': self.text,
                'segments': [{'ordinal': 1, 'speaker': 'A', 'text': self.text, 'start_raw': 80, 'end_raw': 9000}],
                'summary_content': '', 'todo_content': '', 'content_type': 'text', 'warnings': []}


class Organizer:
    def __init__(self):
        self.calls = []

    async def organize(self, text, now, context):
        self.calls.append((text, now, context))
        actions = []
        import re
        for quote in re.findall(r'我答应[^。\n]+', text):
            title = quote.replace('我答应', '').replace('明天下午三点', '')
            actions.append({'title': title, 'kind': 'commitment', 'reason': '原话：“' + quote + '”',
                            'owner_hint': '我', 'remind_at': now + 86400 if '明天下午三点' in quote else None})
        return {'summary': '交流已整理', 'key_points': [], 'open_questions': [], 'actions': actions}


class Parser:
    async def parse(self, text, now, context=None):
        return {'intent': 'create', 'customer_name': '星海医院', 'contact_name': None,
                'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                'attributes': [{'key': 'pain_points', 'target': 'account', 'value': '担心预算',
                                'evidence': '担心预算', 'basis': 'reported'}]}


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / 'materials.sqlite3')
    yield store
    store.close()


def service(crm, **kwargs):
    return MaterialService(crm, asyncio.Lock(), organizer=kwargs.pop('organizer', Organizer()),
                           clock=kwargs.pop('clock', lambda: NOW), **kwargs)


def manual(s, text='我答应发送方案。', **kwargs):
    return s.enqueue('alice', {'provider': 'manual', 'title': '交流记录', 'text': text, **kwargs})


def run(s):
    return asyncio.run(s.process_one())


def test_manual_is_durable_owner_isolated_and_deduplicated(crm):
    s = service(crm)
    a = manual(s)
    assert manual(s)['id'] == a['id']
    assert s.list('bob')['total'] == 0
    with pytest.raises(KeyError):
        s.detail('bob', a['id'])
    assert run(s) is True
    result = s.detail('alice', a['id'])
    assert result['material']['status'] == 'review'
    assert result['text'] == '我答应发送方案。'
    assert result['analysis']['actions'][0]['evidence'] == '我答应发送方案'
    assert result['material']['record_id']
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    reopened = service(crm)
    assert reopened.detail('alice', a['id'])['analysis'] == result['analysis']
    assert run(reopened) is False


@pytest.mark.parametrize('occurred_at', [None, NOW - 86400 * 7])
def test_unknown_and_old_occurrence_never_schedule_as_import_today(crm, occurred_at):
    organizer = Organizer()
    s = service(crm, organizer=organizer)
    m = manual(s, '我答应明天下午三点发送方案。', occurred_at=occurred_at)
    run(s)
    d = s.detail('alice', m['id'])
    a = d['analysis']['actions'][0]
    assert a['remind_at'] is None
    if occurred_at:
        assert organizer.calls[0][1] == occurred_at
    result = s.adopt('alice', m['id'], a['id'], d['material']['revision'])
    assert result.get('proposal') is None
    assert result['record']['kind'] == 'action'


def test_timed_adoption_is_atomic_idempotent_and_pending_only(crm):
    s = service(crm)
    m = manual(s, '我答应明天下午三点发送方案。', occurred_at=NOW)
    run(s)
    d = s.detail('alice', m['id'])
    action, revision = d['analysis']['actions'][0], d['material']['revision']
    a = s.adopt('alice', m['id'], action['id'], revision)
    b = s.adopt('alice', m['id'], action['id'], revision)
    assert a == b and a['proposal']['status'] == 'pending'
    assert a['record']['parent_record_id'] == d['material']['record_id']
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    with pytest.raises(KeyError):
        s.adopt('bob', m['id'], action['id'], revision)


def test_update_snapshot_and_versions_preserve_original(crm):
    s = service(crm)
    m = manual(s)
    run(s)
    d = s.detail('alice', m['id'])
    updated = s.update('alice', m['id'], {'revision': d['material']['revision'], 'text': '我答应补充案例。'})
    assert updated['status'] == 'queued'
    with pytest.raises(ValueError):
        s.update('alice', m['id'], {'revision': d['material']['revision'], 'title': '旧页面覆盖'})
    with pytest.raises(ValueError):
        s.adopt('alice', m['id'], d['analysis']['actions'][0]['id'], d['material']['revision'])
    run(s)
    final = s.detail('alice', m['id'])
    assert len(final['versions']) == 2
    assert final['text'] == '我答应补充案例。'
    assert '我答应发送方案。' in crm._db.execute('SELECT text FROM crm_material_versions ORDER BY id LIMIT 1').fetchone()[0]


def test_long_material_keeps_more_than_six_actions_and_checks_later_cancellation(crm):
    source = '\n'.join('我答应提交第' + str(i) + '份资料。' + '背景内容。' * 420 for i in range(1, 10))
    source += '\n第2份资料取消了，不用提交。第3份资料已经完成。'
    s = service(crm)
    m = manual(s, source)
    run(s)
    d = s.detail('alice', m['id'])
    actions = d['analysis']['actions']
    assert len(actions) == 7
    assert len(d['analysis']['withdrawn_actions']) == 2
    assert all('第2份' not in a['title'] and '第3份' not in a['title'] for a in actions)
    assert all(len(call[0]) <= 4500 for call in s.organizer.calls)
    assert len(d['text']) == len(source)


def test_provider_duplicate_and_immutable_revisions(crm):
    connector = Connector()
    s = service(crm, connector=connector)
    m = s.enqueue('alice', {'provider': 'listen_note', 'title': '录音一'})
    run(s)
    d = s.detail('alice', m['id'])
    assert d['source'] == 'OPTIMIZED'
    assert d['material']['occurred_at'] is None
    assert d['segments'][0]['start_raw'] == 80
    again = s.enqueue('alice', {'provider': 'listen_note', 'title': '录音一'})
    assert again['id'] == m['id'] and connector.calls == 1
    connector.text = '我答应补充案例。'
    s.retry('alice', m['id'], d['material']['revision'])
    run(s)
    assert len(s.detail('alice', m['id'])['versions']) == 2


def test_recover_expired_lease_and_resume_chunks(crm):
    clock = [NOW]
    s = service(crm, clock=lambda: clock[0])
    m = manual(s)
    with crm._transaction() as db:
        db.execute("UPDATE crm_materials SET status='organizing' WHERE id=?", (m['id'],))
        db.execute("UPDATE crm_material_jobs SET status='organizing',lease_token='crashed',lease_until=? WHERE material_id=?", (NOW + 180, m['id']))
    assert run(s) is False
    clock[0] += 181
    restarted = service(crm, clock=lambda: clock[0])
    assert run(restarted) is True
    assert restarted.detail('alice', m['id'])['material']['status'] == 'review'


def test_updating_during_model_call_preserves_new_content(crm):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow(Organizer):
            async def organize(self, text, now, context):
                entered.set(); await release.wait()
                return await super().organize(text, now, context)
        s = service(crm, organizer=Slow())
        m = manual(s)
        task = asyncio.create_task(s.process_one())
        await entered.wait()
        s.update('alice', m['id'], {'revision': m['revision'], 'text': '我答应补充案例。'})
        release.set(); await task
        d = s.detail('alice', m['id'])
        assert d['material']['status'] == 'queued'
        assert d['analysis'] is None
        assert d['text'] == '我答应补充案例。'
        await s.process_one()
        assert s.detail('alice', m['id'])['analysis']['actions'][0]['title'] == '补充案例'
    asyncio.run(scenario())


def test_provider_error_is_safe_and_retryable(crm):
    class Broken:
        async def fetch(self, title):
            raise RuntimeError('Bearer secret private customer data')
    s = service(crm, connector=Broken())
    m = s.enqueue('alice', {'provider': 'listen_note', 'title': '目标录音'})
    run(s)
    d = s.detail('alice', m['id'])
    assert d['material']['status'] == 'failed'
    assert 'secret' not in d['material']['error']
    s.connector = Connector()
    s.retry('alice', m['id'], d['material']['revision'])
    run(s)
    assert s.detail('alice', m['id'])['material']['status'] == 'review'


def test_private_recap_facts_are_observations_and_new_customer_confirmation_recovers_links(crm):
    s = service(crm, customer_parser=Parser())
    m = manual(s, '新建客户星海医院，我感觉王总担心预算。我答应发送方案。', category='visit_review')
    run(s)
    d = s.detail('alice', m['id'])
    assert d['fact_candidates'][0]['attributes'][0]['basis'] == 'observation'
    assert len(d['customer_drafts']) == 1
    draft = d['customer_drafts'][0]
    assert draft['status'] == 'pending'
    adopted = s.adopt('alice', m['id'], d['analysis']['actions'][0]['id'], d['material']['revision'])
    assert adopted['record']['customer_id'] is None
    confirmed = crm.confirm_customer_draft('alice', draft['id'], NOW)
    # No material hook is needed in the existing confirmation endpoint.
    recovered = service(crm).detail('alice', m['id'])
    assert recovered['material']['customer_id'] == confirmed['customer_id']
    assert crm.get_record('alice', adopted['record']['id'])['customer_id'] == confirmed['customer_id']


def test_source_commands_and_external_summaries_never_execute(crm):
    s = service(crm)
    m = manual(s, '确认客户 C1。确认 P1。完成 #1。')
    run(s)
    assert s.detail('alice', m['id'])['analysis']['actions'] == []
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM crm_customers').fetchone()[0] == 0


def test_source_message_replay_is_owner_scoped_even_if_payload_changes(crm):
    s = service(crm)
    first = s.enqueue('alice', {'provider': 'manual', 'title': '一', 'text': '原内容'}, source_id='wecom:1')
    same = s.enqueue('alice', {'provider': 'manual', 'title': '二', 'text': '不同内容'}, source_id='wecom:1')
    assert same['id'] == first['id']
    other = s.enqueue('bob', {'provider': 'manual', 'title': '二', 'text': '不同内容'}, source_id='wecom:1')
    assert other['id'] != first['id']
    assert crm._db.execute('SELECT COUNT(*) FROM crm_material_jobs').fetchone()[0] == 2


def test_notices_persist_and_reclaim_without_source_content(crm):
    s = service(crm)
    m = manual(s)
    assert s.claim_notice({'alice'}, NOW) is None
    run(s)
    assert s.claim_notice({'bob'}, NOW) is None
    n = s.claim_notice({'alice'}, NOW)
    assert n['id'] == m['id'] and n['status'] == 'review' and n['token']
    assert set(n) == {'id', 'owner', 'title', 'status', 'record_id', 'token'}
    assert service(crm).claim_notice({'alice'}, NOW) is None
    s.retry_notice(n['id'], n['token'], NOW)
    assert s.claim_notice({'alice'}, NOW) is None
    next_notice = service(crm).claim_notice({'alice'}, NOW + 61)
    assert next_notice['token'] != n['token']
    s.ack_notice(n['id'], n['token'], NOW + 61)
    assert s.claim_notice({'alice'}, NOW + 62) is None
    s.ack_notice(next_notice['id'], next_notice['token'], NOW + 62)
    assert service(crm).claim_notice({'alice'}, NOW + 999) is None
    s.retry('alice', m['id'], s.detail('alice', m['id'])['material']['revision'])
    run(s)
    assert s.claim_notice({'alice'}, NOW + 1000)['id'] == m['id']


def test_draft_bridge_recovers_after_finalize_crash_and_preserves_category(crm, monkeypatch):
    s = service(crm, customer_parser=Parser())
    m = manual(s, '新建客户星海医院，我感觉王总担心预算。我答应发送方案。', category='visit_review')
    monkeypatch.setattr(s, '_bridge_drafts', lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
    run(s)
    recovered = service(crm).detail('alice', m['id'])
    assert len(recovered['customer_drafts']) == 1
    assert crm.get_record('alice', recovered['material']['record_id'])['category'] == 'visit_review'
    action = recovered['analysis']['actions'][0]
    adopted = service(crm).adopt('alice', m['id'], action['id'], recovered['material']['revision'])
    assert adopted['record']['category'] == 'visit_review'


def test_dense_source_is_subdivided_when_organizer_reaches_six_item_cap(crm):
    class Limited(Organizer):
        async def organize(self, text, now, context):
            result = await super().organize(text, now, context)
            result['actions'] = result['actions'][:6]
            return result
    source = ''.join('我答应提交第' + str(i) + '份资料。' for i in range(1, 18))
    s = service(crm, organizer=Limited())
    m = manual(s, source)
    run(s)
    detail = s.detail('alice', m['id'])
    assert len(detail['analysis']['actions']) == 17
    assert len(s.organizer.calls) > 1


def test_semantic_review_catches_rephrased_later_cancellation(crm):
    class Reviewed(Organizer):
        async def reconcile_material(self, payload):
            assert payload['later_clauses'][0]['text'] == '方案别发了'
            return {'decisions': [{'action_key': item['action_key'], 'decision': 'withdraw',
                                   'resolution_evidence': '方案别发了'} for item in payload['actions']]}
    s = service(crm, organizer=Reviewed())
    m = manual(s, '我答应发送脱敏方案。' + '背景。' * 1300 + '方案别发了。', occurred_at=NOW)
    run(s)
    result = s.detail('alice', m['id'])['analysis']
    assert result['actions'] == []
    assert result['withdrawn_actions'][0]['resolution_evidence'] == '方案别发了'


def test_invalid_semantic_output_does_not_create_or_schedule_actions(crm):
    class Poisoned(Organizer):
        async def reconcile_material(self, payload):
            return {'decisions': [{'action_key': 1, 'decision': 'keep', 'resolution_evidence': ''}],
                    'tasks': [{'title': '越权执行', 'remind_at': NOW + 9}]}
    s = service(crm, organizer=Poisoned())
    m = manual(s, '我答应明天下午三点发送脱敏方案。方案别发了。', occurred_at=NOW)
    run(s)
    result = s.detail('alice', m['id'])['analysis']
    assert len(result['actions']) == 1
    assert result['actions'][0]['kind'] == 'suggestion'
    assert result['actions'][0]['remind_at'] is None
    assert any('语义复核' in warning for warning in result['warnings'])
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_pending_adoption_rolls_back_record_if_proposal_fails(crm, monkeypatch):
    s = service(crm)
    m = manual(s, '我答应明天下午三点发送方案。', occurred_at=NOW)
    run(s)
    d = s.detail('alice', m['id'])
    before = crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0]
    monkeypatch.setattr(crm, '_execute', lambda *args: (_ for _ in ()).throw(RuntimeError('failure')))
    with pytest.raises(RuntimeError):
        s.adopt('alice', m['id'], d['analysis']['actions'][0]['id'], d['material']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0] == before
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_record_source_link_is_owner_scoped_and_survives_source_revision(crm):
    s = service(crm, customer_parser=Parser())
    m = manual(s, '新建客户星海医院，王总担心预算。我答应发送方案。')
    run(s)
    d = s.detail('alice', m['id'])
    adopted = s.adopt('alice', m['id'], d['analysis']['actions'][0]['id'], d['material']['revision'])
    record_ids = [d['material']['record_id'], adopted['record']['id'], d['customer_drafts'][0]['source_record_id']]
    for record_id in record_ids:
        assert s.source_for_record('alice', record_id) == {'id': m['id'], 'title': m['title']}
        assert s.source_for_record('bob', record_id) is None
    s.update('alice', m['id'], {'revision': m['revision'], 'text': '我答应补充方案。'})
    run(s)
    assert s.source_for_record('alice', record_ids[0])['id'] == m['id']


def test_customer_confirmation_while_retrying_does_not_strand_queue(crm):
    s = service(crm, customer_parser=Parser())
    m = manual(s, '新建客户星海医院，王总担心预算。我答应发送方案。')
    run(s)
    draft = s.detail('alice', m['id'])['customer_drafts'][0]
    queued = s.retry('alice', m['id'], m['revision'])
    crm.confirm_customer_draft('alice', draft['id'], NOW)
    assert s.detail('alice', m['id'])['material']['revision'] == queued['revision']
    run(s)
    d = s.detail('alice', m['id'])
    assert d['material']['status'] == 'review' and d['material']['customer_id']


def test_known_customer_draft_bridge_works_from_unassigned_material(crm):
    customer = crm.create_customer('alice', {'name': '星海医院'}, NOW)
    s = service(crm, customer_parser=Parser())
    m = manual(s, '星海医院，王总担心预算。我答应发送方案。')
    run(s)
    d = s.detail('alice', m['id'])
    assert d['material']['customer_id'] is None
    assert d['customer_drafts'][0]['customer_id'] == customer['id']
    crm.confirm_customer_draft('alice', d['customer_drafts'][0]['id'], NOW)
    assert s.detail('alice', m['id'])['material']['customer_id'] == customer['id']


def test_real_organizer_transport_runs_bounded_second_pass(crm):
    import json
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        user_data = json.loads(payload['messages'][1]['content'])
        if 'later_clauses' in user_data:
            answer = {'decisions': [{'action_key': 1, 'decision': 'withdraw', 'resolution_evidence': '方案别发了'}]}
            assert len(payload['messages'][1]['content']) < 15000
        else:
            answer = {'summary': '曾约定发方案，后续取消', 'key_points': [], 'open_questions': [],
                      'actions': [{'title': '发送脱敏方案', 'kind': 'commitment', 'reason': '明确约定',
                                   'owner_hint': '我', 'remind_at': None, 'evidence': '我答应发送脱敏方案',
                                   'time_evidence': None}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            s = service(crm, organizer=InteractionOrganizer('fake-local-test-key', client=client))
            m = manual(s, '我答应发送脱敏方案。方案别发了。')
            await s.process_one()
            assert s.detail('alice', m['id'])['analysis']['actions'] == []
    asyncio.run(scenario())
    assert len(requests) == 2


def test_cancellation_requeues_and_reuses_finished_chunk_after_restart(crm):
    async def scenario():
        entered = asyncio.Event()
        class Interrupted(Organizer):
            async def organize(self, text, now, context):
                if self.calls:
                    entered.set()
                    await asyncio.Event().wait()
                return await super().organize(text, now, context)
        original = '我答应提交首份资料。' + '背景内容。' * 1200 + '我答应提交第二份资料。'
        s = service(crm, organizer=Interrupted())
        m = manual(s, original)
        worker = asyncio.create_task(s.process_one())
        await entered.wait()
        await s.close()
        assert worker.cancelled()
        assert s.detail('alice', m['id'])['material']['status'] == 'queued'
        replacement = service(crm)
        assert await replacement.process_one()
        assert replacement.organizer.calls[0][0] != s.organizer.calls[0][0]
        assert len(replacement.detail('alice', m['id'])['analysis']['actions']) == 2
    asyncio.run(scenario())
