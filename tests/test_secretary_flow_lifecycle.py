"""Archived secretary plans cannot resume or apply a late model result."""
import asyncio
import pytest

from secretary.customer_store import CustomerStore
from secretary.record_lifecycle import RecordLifecycle
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow


NOW = 1791163200.0


class Interpreter:
    async def interpret(self, text, now, context):
        return {'intent': 'plan' if not context['plan'] else 'update',
                'changes': {'person': '王工', 'date': '2026-10-08', 'goal': '交流试点'},
                'evidence': {'person': '王工', 'date': '10月8日', 'goal': '聊试点'},
                'suggestions': [{'title': '准备案例', 'reason': '可选建议', 'evidence': ''}]}


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / 'flow-lifecycle.sqlite3')
    flow = SecretaryFlow(crm, SalesWorkspace(crm), asyncio.Lock(), interpreter=Interpreter(), clock=lambda: NOW)
    lifecycle = RecordLifecycle(crm, clock=lambda: NOW)
    yield crm, flow, lifecycle
    crm.close()


def submit(flow, key, saved=None):
    data = {'request_id': key, 'text': '10月8日约王工聊试点'}
    if saved:
        data.update(plan_id=saved['id'], expected_revision=saved['revision'])
    return flow.submit('owner', data)


def apply(flow, turn):
    assert asyncio.run(flow.process_one())
    return flow.turn('owner', turn['id'])


def move(lifecycle, identifier, action):
    return lifecycle.move('owner', identifier, {'action': action,
        'snapshot': lifecycle.preview('owner', identifier)['snapshot']})


def test_plan_turns_archive_as_group_and_independent_adoption_stays_visible(stack):
    crm, flow, lifecycle = stack
    first = apply(flow, submit(flow, 'first'))
    child = flow.adopt('owner', first['id'], 1)
    second = apply(flow, submit(flow, 'second', first['plan']))
    preview = lifecycle.preview('owner', second['record_id'])
    assert preview['root_record_id'] == first['record_id'] and preview['record_count'] == 2
    archived = move(lifecycle, second['record_id'], 'archive')
    assert archived['effects']['hidden_records'] == 2
    assert crm.get_record('owner', child['id'])
    assert flow.list_plans('owner')['items'] == [] and flow.recent_turns('owner')['items'] == []
    assert flow.for_record('owner', second['record_id']) is None
    with pytest.raises((KeyError, ValueError)):
        flow.plan('owner', first['plan']['id'])
    with pytest.raises((KeyError, ValueError)):
        submit(flow, 'blocked', second['plan'])
    with pytest.raises((KeyError, ValueError)):
        flow.adopt('owner', first['id'], 1)
    move(lifecycle, second['record_id'], 'restore')
    assert len(flow.plan('owner', first['plan']['id'])['turns']) == 2
    assert lifecycle.list('owner')['total'] == 0


def test_queued_source_archived_is_not_claimed_and_restore_requires_explicit_retry(stack):
    crm, flow, lifecycle = stack
    queued = submit(flow, 'queued')
    move(lifecycle, queued['record_id'], 'trash')
    assert asyncio.run(flow.process_one()) is False
    with pytest.raises((KeyError, ValueError)):
        flow.retry('owner', queued['id'])
    move(lifecycle, queued['record_id'], 'restore')
    assert asyncio.run(flow.process_one()) is False
    retried = flow.retry('owner', queued['id'])
    assert apply(flow, retried)['status'] == 'done'
    assert crm._db.execute('SELECT count(*) FROM crm_secretary_plans').fetchone()[0] == 1


def test_late_result_after_archive_and_restore_cannot_reactivate_source(stack):
    crm, flow, lifecycle = stack
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Waiting(Interpreter):
            async def interpret(self, text, now, context):
                started.set()
                await release.wait()
                return await super().interpret(text, now, context)
        flow.interpreter = Waiting()
        turn = submit(flow, 'late')
        worker = asyncio.create_task(flow.process_one())
        await started.wait()
        move(lifecycle, turn['record_id'], 'archive')
        move(lifecycle, turn['record_id'], 'restore')
        release.set()
        assert await worker
        assert crm._db.execute('SELECT count(*) FROM crm_secretary_plans').fetchone()[0] == 0
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert crm.get_record('owner', turn['record_id'])['original_content'] == '10月8日约王工聊试点'
    asyncio.run(run())


def test_hidden_plan_blocks_queued_supplement_even_when_turn_itself_visible(stack):
    crm, flow, _ = stack
    first = apply(flow, submit(flow, 'first'))
    queued = submit(flow, 'supplement', first['plan'])
    with crm._transaction() as db:
        db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (first['record_id'],))
    assert asyncio.run(flow.process_one()) is False
    assert flow.recent_turns('owner')['items'] == []
    assert crm.get_record('owner', queued['record_id'])


def test_late_apply_rechecks_hidden_root_without_lifecycle_worker_pause(stack):
    crm, flow, _ = stack
    first = apply(flow, submit(flow, 'first'))
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Waiting(Interpreter):
            async def interpret(self, text, now, context):
                started.set()
                await release.wait()
                return await super().interpret(text, now, context)
        flow.interpreter = Waiting()
        queued = submit(flow, 'supplement', first['plan'])
        worker = asyncio.create_task(flow.process_one())
        await started.wait()
        with crm._transaction() as db:
            db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (first['record_id'],))
        release.set()
        assert await worker
        stored = crm._db.execute('SELECT * FROM crm_secretary_turns WHERE id=?', (queued['id'],)).fetchone()
        assert stored['status'] == 'needs_attention' and stored['lease'] is None
        assert crm._db.execute('SELECT revision FROM crm_secretary_plans WHERE id=?', (first['plan']['id'],)).fetchone()[0] == first['plan']['revision']
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def make_prospect(flow):
    class ProspectInterpreter:
        async def interpret(self, text, now, context):
            return {'intent': 'contact', 'changes': {'person': '李工', 'phone': '13800001111', 'wechat': 'li_test'},
                'evidence': {'person': '李工', 'phone': '13800001111', 'wechat': 'li_test'}}
    flow.interpreter = ProspectInterpreter()
    turn = apply(flow, flow.submit('owner', {'request_id': 'prospect',
        'text': '新认识李工，电话13800001111，微信li_test，单位还不知道'}))
    return turn, flow.prospects('owner')['items'][0]


@pytest.mark.parametrize('already_linked', [False, True])
def test_archived_prospect_source_leaves_list_and_blocks_stale_link_until_restore(stack, already_linked):
    crm, flow, lifecycle = stack
    turn, prospect = make_prospect(flow)
    customer = crm.create_customer('owner', {'name': '虚构线索归属单位'}, NOW)
    payload = {'customer_id': customer['id'], 'expected_updated_at': prospect['updated_at']}
    if already_linked:
        flow.link_prospect('owner', prospect['id'], payload)
        prospect = flow.prospects('owner')['items'][0]
        payload['expected_updated_at'] = prospect['updated_at']
    before = crm._db.execute('SELECT count(*) FROM crm_contacts').fetchone()[0]
    move(lifecycle, turn['record_id'], 'archive')
    assert flow.prospects('owner')['items'] == []
    with pytest.raises((KeyError, ValueError)):
        flow.link_prospect('owner', prospect['id'], payload)
    assert crm._db.execute('SELECT count(*) FROM crm_contacts').fetchone()[0] == before
    move(lifecycle, turn['record_id'], 'restore')
    assert flow.prospects('owner')['items'][0]['id'] == prospect['id']
    linked = flow.link_prospect('owner', prospect['id'], payload)
    assert linked['status'] == 'linked'
    assert crm._db.execute('SELECT count(*) FROM crm_contacts').fetchone()[0] == 1
    assert crm.get_record('owner', turn['record_id'])['original_content'] == turn['text']


def test_foreign_prospect_source_is_neither_listed_nor_resolved(stack):
    crm, flow, _ = stack
    _, prospect = make_prospect(flow)
    foreign = crm.create_record('other', {'title': 'foreign source', 'content': 'private'}, NOW)
    customer = crm.create_customer('owner', {'name': '虚构单位'}, NOW)
    with crm._transaction() as db:
        db.execute('UPDATE crm_secretary_prospects SET source_record_id=? WHERE owner=? AND id=?',
            (foreign['id'], 'owner', prospect['id']))
    assert flow.prospects('owner')['items'] == []
    with pytest.raises(KeyError):
        flow.link_prospect('owner', prospect['id'], {'customer_id': customer['id'],
            'expected_updated_at': prospect['updated_at']})
    assert crm._db.execute('SELECT count(*) FROM crm_contacts').fetchone()[0] == 0
    assert crm.get_record('other', foreign['id'])['content'] == 'private'
