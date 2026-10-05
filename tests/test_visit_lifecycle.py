"""Composite exchanges respect source lifecycle on disposable synthetic data."""
import asyncio
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.exchange_records import ExchangeRecords
from secretary.materials import MaterialService
from secretary.record_lifecycle import RecordLifecycle
from secretary.review_queue import ReviewQueue
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI
from secretary.visits import VisitService


NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': text, 'actions': [], 'key_points': [], 'open_questions': []}


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / 'visit-lifecycle-synthetic.sqlite3')
    lock = asyncio.Lock()
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=lambda: NOW)
    visits = VisitService(crm, materials, lock)
    ExchangeRecords(crm, visits, lambda: NOW)
    lifecycle = RecordLifecycle(crm, clock=lambda: NOW + 1)
    yield crm, materials, visits, lifecycle, lock
    crm.close()


def move(lifecycle, record_id, action):
    preview = lifecycle.preview('owner', record_id)
    return lifecycle.move('owner', record_id, {'action': action, 'snapshot': preview['snapshot']})


def flow_plan(stack):
    crm, _, visits, _, lock = stack
    customer = crm.create_customer('owner', {'name': '合成归档单位'}, NOW)
    class Interpreter:
        answers = [
            {'intent': 'plan', 'changes': {'title': '与林博士吃饭', 'person': '林博士',
                'date': '2026-10-08', 'activity': 'meal'},
             'evidence': {'person': '林博士', 'date': '10月8日', 'activity': '吃饭'}},
            {'intent': 'update', 'changes': {'goal': '讨论合成平台合作'},
             'evidence': {'goal': '主要聊合成平台合作'}}]
        async def interpret(self, text, now, context):
            return self.answers.pop(0)
    flow = SecretaryFlow(crm, SalesWorkspace(crm, clock=lambda: NOW), lock,
        visits=visits, interpreter=Interpreter(), clock=lambda: NOW)
    first = flow.submit('owner', {'request_id': 'flow-first', 'text': '10月8日约林博士吃饭',
        'customer_id': customer['id']})
    assert asyncio.run(flow.process_one())
    first = flow.turn('owner', first['id'])
    plan = first['plan']
    second = flow.submit('owner', {'request_id': 'flow-second', 'text': '主要聊合成平台合作',
        'plan_id': plan['id'], 'expected_revision': plan['revision']})
    assert asyncio.run(flow.process_one())
    second = flow.turn('owner', second['id'])
    return flow, second['plan'], (first['record_id'], second['record_id'])


def test_archived_two_turn_flow_does_not_break_visit_or_review_lists_and_restore(stack):
    crm, materials, visits, lifecycle, _ = stack
    _, plan, ids = flow_plan(stack)
    assert visits.list('owner')['total'] == 1
    move(lifecycle, plan['record_id'], 'archive')
    assert crm._db.execute('SELECT count(*) FROM crm_records WHERE id IN (?,?) AND hidden=1', ids).fetchone()[0] == 2
    assert visits.list('owner') == {'items': [], 'total': 0}
    queue = ReviewQueue(crm, materials=materials, visits=visits, clock=lambda: NOW)
    assert queue.list('owner', page_size=1)['total'] == 0
    historical = visits.detail('owner', plan['visit_id'])
    assert historical['record_sources'] == []
    move(lifecycle, plan['record_id'], 'restore')
    assert visits.list('owner')['items'][0]['id'] == plan['visit_id']
    assert len(visits.detail('owner', plan['visit_id'])['record_sources']) == 2
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_technical_hidden_record_does_not_hide_or_break_ordinary_exchange(stack):
    crm, _, visits, _, _ = stack
    visit = visits.create('owner', {'title': '技术隐藏兼容的合成交流'})
    source = crm.create_record('owner', {'title': '旧捕获副本', 'content': '合成旧副本'}, NOW)
    with crm._transaction() as db:
        db.execute('INSERT INTO crm_visit_records VALUES (?,?,?,?,?)',
            ('owner', visit['id'], source['id'], 'supplement', NOW))
        db.execute('UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?', ('owner', source['id']))
    result = visits.list('owner')
    assert result['total'] == 1
    assert visits.detail('owner', visit['id'])['record_sources'] == []


def test_other_owner_archival_cannot_hide_owned_exchange(stack):
    crm, _, visits, lifecycle, _ = stack
    own = visits.create('owner', {'title': '本人的合成交流'})
    foreign = crm.create_record('foreign', {'title': '另一人的合成来源', 'content': '独立来源'}, NOW)
    snapshot = lifecycle.preview('foreign', foreign['id'])['snapshot']
    lifecycle.move('foreign', foreign['id'], {'action': 'archive', 'snapshot': snapshot})
    assert visits.list('owner')['items'][0]['id'] == own['id']
    assert visits.list('foreign')['total'] == 0


def test_archived_material_copy_keeps_independent_original_readable(stack):
    crm, materials, _, lifecycle, _ = stack
    item = materials.enqueue('owner', {'provider': 'manual', 'title': '合成独立材料',
        'category': 'memo', 'text': '用户提供的独立合成资料'})
    assert asyncio.run(materials.process_one())
    detail = materials.detail('owner', item['id'])
    record_id = detail['material']['record_id']
    assert record_id
    move(lifecycle, record_id, 'archive')
    assert materials.list('owner')['total'] == 1
    assert materials.detail('owner', item['id'])['text'] == '用户提供的独立合成资料'
    assert crm.get_record('owner', record_id) is None
