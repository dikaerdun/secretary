"""Opt-in real-model oracle with synthetic text and a disposable SQLite database.

Run from the project root: .venv/Scripts/python.exe deploy/live_material_verify.py
Only DeepSeek is contacted. No WeCom, ListenNote, production database or external
customer is read or written. Stdout is JSONL of fixed step/code names, counts and
booleans; exception messages and model responses are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from secretary.customer_parser import CustomerVoiceParser
from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.organizer import InteractionOrganizer
from secretary.store import SHANGHAI


def emit(step, **values):
    print(json.dumps({'step': step, **values}, sort_keys=True), flush=True)


def configuration(path):
    # Never print the dotenv mapping; unrelated credentials are not used.
    values = dotenv_values(path, interpolate=False)
    def get(name, default=''):
        return str(os.environ.get(name, values.get(name) or default)).strip()
    key = get('DEEPSEEK_API_KEY')
    model = get('DEEPSEEK_MODEL', 'deepseek-flash')
    base = get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    address = urlsplit(base)
    if not key or not model:
        raise ValueError('configuration_missing')
    if (address.scheme != 'https' or not address.hostname or address.username or address.password
            or address.query or address.fragment):
        raise ValueError('configuration_invalid')
    return key, model, base


class Oracle:
    def __init__(self, crm, service, now):
        self.crm, self.service, self.now = crm, service, now
        self.checks = 0
        self.failures = 0

    def check(self, step, passed, **counts):
        self.checks += 1
        self.failures += not bool(passed)
        emit(step, ok=bool(passed), **counts)
        return bool(passed)

    def count(self, table, owner):
        # Fixed internal table names only, never caller/model-provided SQL.
        if table not in {'tasks', 'proposals', 'crm_customers', 'crm_contacts'}:
            raise ValueError('invalid_oracle_table')
        return self.crm._db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE owner=?', (owner,)).fetchone()[0]

    async def material(self, step, owner, text, *, category='conversation', occurred_at=None):
        item = self.service.enqueue(owner, {'provider': 'manual', 'title': '合成行业材料验证',
            'text': text, 'category': category, 'occurred_at': occurred_at})
        progressed = await self.service.process_one()
        detail = self.service.detail(owner, item['id'])
        analysis = detail.get('analysis') or {}
        self.check(step + '.processed', progressed and detail['material']['status'] == 'review',
                   source_characters=len(text), actions=len(analysis.get('actions', [])),
                   customer_drafts=len(detail.get('customer_drafts', [])),
                   warnings=len(detail.get('warnings', [])))
        return detail

    async def customer_and_schedule(self):
        owner = 'synthetic-material-case-1'
        future = (datetime.fromtimestamp(self.now, SHANGHAI) + timedelta(days=2)).replace(
            hour=15, minute=0, second=0, microsecond=0)
        text = ('新建客户联调合成密码科技有限公司，联系人赵经理，赵经理是技术负责人，'
                '客户关注数据库字段加密和密钥管理。'
                '我答应' + future.strftime('%Y年%m月%d日下午三点') + '发送数据库加密方案。')
        detail = await self.material('case1', owner, text, occurred_at=self.now)
        if detail['material']['status'] != 'review': return
        drafts = detail['customer_drafts']
        pending = [draft for draft in drafts if draft['status'] == 'pending']
        self.check('case1.customer_pending_only', bool(pending) and self.count('crm_customers', owner) == 0
                   and self.count('crm_contacts', owner) == 0,
                   pending_drafts=len(pending), customers=self.count('crm_customers', owner),
                   contacts=self.count('crm_contacts', owner))
        actions = detail['analysis']['actions']
        timed = [a for a in actions if a['kind'] == 'commitment' and a['remind_at'] is not None
                 and '方案' in a['title']]
        self.check('case1.explicit_future_action', bool(timed), timed_actions=len(timed),
                   commitments=sum(a['kind'] == 'commitment' for a in actions))
        adopted = None
        if timed:
            adopted = self.service.adopt(owner, detail['material']['id'], timed[0]['id'], detail['material']['revision'])
            proposal = adopted.get('proposal')
            self.check('case1.adoption_pending_no_task', bool(proposal) and proposal['status'] == 'pending'
                       and self.count('tasks', owner) == 0,
                       proposals=self.count('proposals', owner), tasks=self.count('tasks', owner))
            repeated = self.service.adopt(owner, detail['material']['id'], timed[0]['id'], detail['material']['revision'])
            self.check('case1.adoption_idempotent', repeated['record']['id'] == adopted['record']['id']
                       and self.count('proposals', owner) == 1)
        if pending:
            confirmed = self.crm.confirm_customer_draft(owner, pending[0]['id'], self.now)
            recovered = self.service.detail(owner, detail['material']['id'])
            self.check('case1.customer_confirmed_with_contact', confirmed['status'] == 'confirmed'
                       and self.count('crm_customers', owner) == 1 and self.count('crm_contacts', owner) >= 1,
                       customers=self.count('crm_customers', owner), contacts=self.count('crm_contacts', owner))
            if adopted:
                record = self.crm.get_record(owner, adopted['record']['id'])
                self.check('case1.confirmed_customer_links_action', record['customer_id'] == confirmed['customer_id']
                           and recovered['material']['customer_id'] == confirmed['customer_id'])
        if adopted and adopted.get('proposal'):
            # Deliberate human-confirm equivalent inside this disposable database.
            self.crm.execute(owner, 'synthetic-explicit-confirm',
                {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, self.now)
            proposal = self.crm.get_proposal(owner, adopted['proposal']['id'])
            self.check('case1.only_explicit_confirm_activates', proposal['status'] == 'confirmed'
                       and self.count('tasks', owner) == 1, tasks=self.count('tasks', owner))

    async def uncertain_and_old_dates(self):
        for label, occurred_at in (('unknown', None), ('old', self.now - 30 * 86400)):
            owner = 'synthetic-material-case-2-' + label
            text = '我答应明天下午三点发送商用密码产品资料，没有其他安排。'
            detail = await self.material('case2.' + label, owner, text, occurred_at=occurred_at)
            if detail['material']['status'] != 'review': continue
            actions = detail['analysis']['actions']
            self.check('case2.' + label + '.no_import_relative_schedule', bool(actions)
                       and all(a['remind_at'] is None for a in actions), actions=len(actions))
            for action in actions:
                adopted = self.service.adopt(owner, detail['material']['id'], action['id'], detail['material']['revision'])
                self.check('case2.' + label + '.adoption_unscheduled', adopted.get('proposal') is None)
            self.check('case2.' + label + '.no_proposal_or_task', self.count('proposals', owner) == 0
                       and self.count('tasks', owner) == 0, proposals=self.count('proposals', owner),
                       tasks=self.count('tasks', owner))

    async def long_correction_and_idea(self):
        owner = 'synthetic-material-case-3'
        opening = ('合成项目会议。销售说：我答应明天下午三点给王总发报价。'
                   '我答应明天下午四点完成三个字段的加密试点。\n')
        background = ('技术背景记录：数据库字段加密通过密钥管理接口取得密钥，'
                      '现场讨论性能、权限边界、接口兼容和测试环境，当前这一段仅描述技术背景。\n')
        source = opening + background * 80 + ('\n王总说报价先不发了，等内部需求确认。'
                   '试点范围改为先评估一个字段，不沿用前面三个字段的实施约定。具体执行日期另行讨论。')
        self.check('case3.long_source_crosses_chunks', len(source) > 4500, source_characters=len(source))
        detail = await self.material('case3', owner, source, category='meeting', occurred_at=self.now)
        if detail['material']['status'] == 'review':
            actions = detail['analysis']['actions']
            withdrawn = detail['analysis'].get('withdrawn_actions', [])
            affected = [a for a in actions if '报价' in a['title'] or '三个字段' in a['title']]
            self.check('case3.canceled_or_changed_actions_unscheduled', all(a['remind_at'] is None for a in affected),
                       affected_actions=len(affected), withdrawn_actions=len(withdrawn),
                       timed_actions=sum(a['remind_at'] is not None for a in actions))
            self.check('case3.correction_is_visible', bool(withdrawn) or any(a['kind'] == 'suggestion' for a in affected)
                       or bool(detail['analysis']['open_questions']),
                       open_questions=len(detail['analysis']['open_questions']))
            for action in actions:
                result = self.service.adopt(owner, detail['material']['id'], action['id'], detail['material']['revision'])
                if '报价' in action['title'] or '三个字段' in action['title']:
                    self.check('case3.no_withdrawn_promise_proposal', result.get('proposal') is None)
            self.check('case3.no_automatic_activation', self.count('tasks', owner) == 0, tasks=self.count('tasks', owner))
        idea_owner = 'synthetic-material-case-3-idea'
        idea = ('个人想法：我想先整理一份商用密码产品与数据库字段加密的场景对照表，方便下次交流；'
                '这只是我的想法，没有客户承诺，也没有约时间。')
        detail = await self.material('case3.idea', idea_owner, idea, category='idea', occurred_at=self.now)
        if detail['material']['status'] != 'review': return
        actions = detail['analysis']['actions']
        self.check('case3.idea_stays_suggestion', bool(actions)
                   and all(a['kind'] == 'suggestion' and a['remind_at'] is None for a in actions), actions=len(actions))
        if actions:
            adopted = self.service.adopt(idea_owner, detail['material']['id'], actions[0]['id'], detail['material']['revision'])
            self.check('case3.idea_manual_adoption', adopted['record']['kind'] == 'action' and adopted.get('proposal') is None
                       and self.count('tasks', idea_owner) == 0)


async def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--env', default=str(Path(__file__).resolve().parents[1] / '.env'))
    options = args.parse_args()
    logging.disable(logging.CRITICAL)
    step, requests, last_status, errors = 'configuration', 0, 0, 0
    async def requested(_request):
        nonlocal requests
        requests += 1
    async def received(response):
        nonlocal last_status
        last_status = response.status_code
    try:
        key, model, base = configuration(options.env)
        emit(step, ok=True)
        async with httpx.AsyncClient(timeout=40, trust_env=False, follow_redirects=False,
                event_hooks={'request': [requested], 'response': [received]}) as client:
            organizer = InteractionOrganizer(key, model, base, client=client)
            parser = CustomerVoiceParser(key, model, base, client=client)
            now = time.time()
            with tempfile.TemporaryDirectory(prefix='secretary-material-live-') as folder:
                crm = CustomerStore(Path(folder) / 'synthetic-only.sqlite3')
                service = MaterialService(crm, asyncio.Lock(), organizer=organizer,
                                          customer_parser=parser, clock=lambda: now)
                oracle = Oracle(crm, service, now)
                try:
                    for step, scenario in (('case1', oracle.customer_and_schedule),
                                           ('case2', oracle.uncertain_and_old_dates),
                                           ('case3', oracle.long_correction_and_idea)):
                        emit(step + '.started', ok=True)
                        try:
                            await asyncio.wait_for(scenario(), timeout=480)
                        except asyncio.TimeoutError:
                            errors += 1
                            emit(step, ok=False, code='SCENARIO_TIMEOUT')
                        except Exception:
                            errors += 1
                            emit(step, ok=False, code='SCENARIO_FAILED')
                    passed = oracle.failures == 0 and errors == 0
                    emit('complete', ok=passed, checks=oracle.checks, failed_checks=oracle.failures,
                         errors=errors, model_requests=requests, production_untouched=True, wecom_sent=0)
                    return 0 if passed else 1
                finally:
                    await service.close()
                    crm.close()
    except Exception:
        code = ('CONFIGURATION_ERROR' if step == 'configuration' else 'UPSTREAM_AUTH_ERROR'
                if last_status in (401, 403) else 'LIVE_VERIFY_FAILED')
        emit(step, ok=False, code=code, model_requests=requests, production_untouched=True, wecom_sent=0)
        return 2


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
