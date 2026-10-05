"""AQ37: an unexpired AI result cannot undo a newer manual decision.

These are full SecretaryFlow turns, disposable data and an actually suspended
model coroutine. No timeout, cancellation or archive substitutes for the race.
"""
import asyncio
from datetime import datetime

import pytest

from secretary.arrangement_time import normalize_time
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI


OWNER = 'fictional-async-manual-arrangement-owner'
NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()


def snapshot_execution(crm):
    return {table: [dict(row) for row in crm._db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
            for table in ('tasks', 'notifications')}


def row(crm, table, identifier):
    return dict(crm._db.execute('SELECT * FROM ' + table + ' WHERE owner=? AND id=?', (OWNER, identifier)).fetchone())


@pytest.mark.parametrize('manual_operation', ['pause', 'start_reschedule'])
def test_unexpired_ai_result_cannot_override_manual_pause_or_reschedule(tmp_path, manual_operation):
    async def run():
        crm = CustomerStore(tmp_path / ('fictional-' + manual_operation + '.sqlite3'))
        flow = SecretaryFlow(crm, SalesWorkspace(crm, clock=lambda: NOW), asyncio.Lock(), clock=lambda: NOW)
        worker = None
        release = asyncio.Event()
        try:
            first = flow.submit(OWNER, {'request_id': 'initial',
                'text': '安排10月8日下午三点给李总打电话，提前一个小时提醒'})
            assert await flow.process_one()
            scheduled = flow.turn(OWNER, first['id'])['plan']
            task = scheduled['active_schedule']
            assert task and scheduled['arrangement']['settling_state'] == 'settled'
            assert len(snapshot_execution(crm)['tasks']) == len(snapshot_execution(crm)['notifications']) == 1

            pending = flow.arrangements.apply_decision(OWNER, scheduled['id'], {
                'operation': 'start_reschedule', 'request_id': 'initial-coordination',
                'expected_revision': scheduled['revision'], 'expected_task_revision': task['revision'],
                'proposed_execution': {'time_spec': normalize_time('2026-10-09T15:00:00+08:00', NOW, role='execution')},
                'settle_deadline': {'time_spec': normalize_time('2026-10-08', NOW)},
                'next_check': {'time_spec': normalize_time('2026-10-07', NOW, role='check'), 'action': '核对电话时间'},
            })
            assert pending['arrangement']['settling_state'] == 'pending'
            assert pending['active_schedule'] == task

            started = asyncio.Event()
            class BlockingModel:
                async def interpret(self, text, now, context):
                    started.set()
                    await release.wait()
                    return {'intent': 'update',
                        'changes': {'activity': 'call', 'date': '2026-10-10',
                                    'start_at': '2026-10-10T15:00:00+08:00', 'remind_minutes': 15},
                        'evidence': {'activity': '打电话', 'date': '10月10日',
                                     'start_at': '10月10日下午三点', 'remind_minutes': '提前15分钟提醒'}}

            flow.interpreter = BlockingModel()
            text = '安排10月10日下午三点给李总打电话，提前15分钟提醒'
            submitted = flow.submit(OWNER, {'request_id': 'blocked-model', 'text': text,
                'plan_id': scheduled['id'], 'expected_revision': pending['revision']})
            worker = asyncio.create_task(flow.process_one())
            await asyncio.wait_for(started.wait(), timeout=5)
            processing = row(crm, 'crm_secretary_turns', submitted['id'])
            held = row(crm, 'crm_secretary_plans', scheduled['id'])
            assert processing['status'] == 'processing' and processing['lease']
            assert held['hold_turn_id'] == submitted['id'] and held['followup_hold_until'] > NOW
            assert held['followup_dirty']
            original_source = row(crm, 'crm_records', submitted['record_id'])

            payload = {'operation': manual_operation, 'request_id': 'manual-wins',
                       'expected_revision': held['revision']}
            if manual_operation == 'start_reschedule':
                payload.update(expected_task_revision=task['revision'],
                    proposed_execution={'time_spec': normalize_time('2026-10-11T16:00:00+08:00', NOW, role='execution')})
            else:
                payload['reason'] = '人工决定先别催协调'
            manual = flow.arrangements.apply_decision(OWNER, scheduled['id'], payload)
            authoritative = row(crm, 'crm_secretary_plans', scheduled['id'])
            execution_after_manual = snapshot_execution(crm)
            assert authoritative['revision'] == held['revision'] + 1
            assert authoritative['hold_generation'] > held['hold_generation']
            assert authoritative['hold_turn_id'] is None and not authoritative['followup_dirty']
            assert manual['active_schedule'] == task
            if manual_operation == 'pause':
                assert manual['arrangement']['settling_state'] == 'paused'
                assert not manual['arrangement']['followup_enabled']
            else:
                assert manual['arrangement']['settling_state'] == 'pending'
                assert manual['arrangement']['proposed_execution']['time_spec']['date'] == '2026-10-11'

            # The injected clock never advanced, so rejecting this callback
            # proves manual version/generation wins even before hold expiry.
            assert NOW < held['followup_hold_until']
            release.set()
            assert await asyncio.wait_for(worker, timeout=5)
            rejected = flow.turn(OWNER, submitted['id'])
            assert rejected['status'] in ('failed', 'needs_attention')
            assert rejected['error'] and '更新' in rejected['error']
            assert row(crm, 'crm_secretary_turns', submitted['id'])['lease'] is None
            assert row(crm, 'crm_secretary_plans', scheduled['id']) == authoritative
            assert snapshot_execution(crm) == execution_after_manual
            assert row(crm, 'crm_records', submitted['record_id']) == original_source
            assert rejected['text'] == text
            assert original_source['original_content'] == text
            assert crm._db.execute('SELECT count(*) FROM crm_secretary_plan_history WHERE owner=? AND turn_id=?',
                                  (OWNER, submitted['id'])).fetchone()[0] == 0
            assert len(execution_after_manual['tasks']) == len(execution_after_manual['notifications']) == 1
        finally:
            release.set()
            if worker is not None and not worker.done():
                await asyncio.wait_for(worker, timeout=5)
            flow.close()
            crm.close()
    asyncio.run(run())
