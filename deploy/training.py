"""Persistent, loopback-only sales exercises with disclosed offline examples.

This entry point never loads configuration, credentials, a formal database, a
BotGateway, ASR, or an external model client. Only the explicitly named training
database and its public marker/manifest are used. Run from the repository root:
``.venv/Scripts/python.exe deploy/training.py --check``.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time

from aiohttp import web

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from secretary.audio import AudioService
from secretary.coaching_service import CoachingService
from secretary.crm import analysis_fingerprint
from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.sales_workspace import SalesWorkspace
from secretary.spoken_changes import SpokenChangeService
from secretary.store import SHANGHAI, Store
from secretary.visits import VisitService
from secretary.web import create_app, hash_password
from secretary.customer_resolution import CustomerResolutionService
from secretary.sales_discussion import DiscussionService

OWNER = 'training-user'
PASSWORD = 'learn-secretary-2026'  # Public exercise password, never a real credential.
DB_NAME = 'data/training-secretary.sqlite3'
MARKER_NAME = 'data/training-secretary.meta.json'
MANIFEST_NAME = 'deploy/training-manifest.local.json'
TRAINING_ID = 'secretary-offline-training-v1'
NOTICE = '演练示例输出（非真实 AI／ASR）'
RANGE_NOTICE = '原文已保存。离线演练只理解预设案例语句；其他内容请手动核对客户、画像和事项，不代表真实 AI 理解效果。'


class TrainingError(ValueError):
    pass


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing.local')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
    os.replace(temporary, path)


def training_config(*, root=APP_ROOT, port=8766, host='127.0.0.1', db_path=None):
    """Select fixed training paths without reading environment or private files."""
    root = Path(root).resolve()
    if host != '127.0.0.1':
        raise TrainingError('演练环境只允许监听 127.0.0.1。')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise TrainingError('演练端口需要为 1024 至 65535。')
    expected = root / DB_NAME
    selected = Path(db_path) if db_path else expected
    if not selected.is_absolute():
        selected = root / selected
    # Reject redirection before opening any database, including symlinked data.
    if selected.resolve() != expected or expected.resolve() != expected:
        raise TrainingError('演练环境必须使用独立 data/training-secretary.sqlite3，不能指定其他数据库。')
    marker = root / MARKER_NAME
    if marker.resolve() != marker:
        raise TrainingError('演练标记路径不能重定向。')
    return {'root': root, 'host': host, 'port': port, 'db_path': expected,
            'marker_path': marker, 'manifest_path': root / MANIFEST_NAME,
            'url': f'http://{host}:{port}'}


def ensure_training_marker(config, now):
    """Unknown pre-existing DBs are refused without opening or copying them."""
    path, db = config['marker_path'], config['db_path']
    if path.exists():
        try:
            marker = json.loads(path.read_text(encoding='utf-8'))
            if (marker['training_id'] != TRAINING_ID or marker['owner'] != OWNER or
                    marker['database'] != DB_NAME or not isinstance(marker['anchor_at'], (float, int))):
                raise ValueError()
            datetime.fromtimestamp(marker['anchor_at'], SHANGHAI)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            raise TrainingError('演练数据库标记无效；请保留资料并检查，不会打开未知数据库。') from None
        if not db.exists() and marker.get('status') == 'ready':
            raise TrainingError('演练数据库缺失；请保留标记并检查，不会静默重建已完成的演练资料。')
        return marker
    if db.exists() or any(Path(str(db) + suffix).exists() for suffix in ('-wal', '-shm')):
        raise TrainingError('已有未标记数据库；不会打开、覆盖或复制它，请保留资料并检查。')
    marker = {'training_id': TRAINING_ID, 'owner': OWNER, 'database': DB_NAME,
              'anchor_at': float(now), 'status': 'initializing', 'public_exercise_data': True}
    write_json(path, marker)
    return marker


def _day(anchor, days, hour=0, minute=0):
    current = datetime.fromtimestamp(anchor, SHANGHAI)
    return (current.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)).timestamp()


def _date(anchor, days):
    return datetime.fromtimestamp(_day(anchor, days), SHANGHAI).date().isoformat()


def _time(anchor, days, hour):
    return datetime.fromtimestamp(_day(anchor, days, hour), SHANGHAI).strftime('%Y-%m-%d %H:%M')


class OfflineOrganizer:
    """Exact exercise phrases only; arbitrary input never gets invented actions."""
    _date_pattern = r'\d{4}-\d{2}-\d{2}'
    _recipes = (
        (r'我答应整理病历签名接口问题，没有约定执行时间',
         '整理病历签名接口问题', 'self', '我'),
        (r'我答应发送数据库加密产品资料，没有约定执行时间',
         '发送数据库加密产品资料', 'self', '我'),
        (r'客户周工答应提供接口清单，截止(?P<deadline_date>' + _date_pattern + ')',
         '提供接口清单', 'customer', '客户周工'),
        (r'我答应演示脱敏网关，执行时间(?P<execution>' + _date_pattern +
         r' \d{2}:\d{2})，预计(?P<duration>\d{1,3})分钟',
         '演示脱敏网关', 'self', '我'),
        (r'我方团队赵工负责完成兼容验证，截止(?P<deadline_date>' + _date_pattern +
         r')，检查(?P<check_date>' + _date_pattern + ')',
         '完成兼容验证', 'team', '我方团队赵工'),
        (r'客户陈工答应确认测试环境，没有约定执行时间；检查(?P<check_date>' + _date_pattern + r')是否收到反馈',
         '确认测试环境', 'customer', '客户陈工'),
    )

    @classmethod
    def supported_actions(cls, text):
        """Whole positive clauses bind each permitted verb to its exact subject.

        A title, a matching substring, or an interrogative is insufficient. Date,
        clock and duration slots are the only variable parts of these exercises.
        """
        actions = []
        for clause in re.finditer(r'([^。！？\n]+)([。！？\n]|$)', text):
            sentence, ending = clause[1].strip(), clause[2]
            if ending in ('？', '！'):
                continue
            for pattern, title, executor, owner in cls._recipes:
                matched = re.fullmatch(pattern, sentence)
                if matched is None:
                    continue
                fields = matched.groupdict()
                try:
                    dates = {field: datetime.strptime(fields[field], '%Y-%m-%d').date().isoformat()
                             for field in ('deadline_date', 'check_date') if fields.get(field)}
                    execution = (datetime.strptime(fields['execution'], '%Y-%m-%d %H:%M').replace(tzinfo=SHANGHAI).timestamp()
                                 if fields.get('execution') else None)
                    duration = int(fields['duration']) if fields.get('duration') else None
                    if duration is not None and not 5 <= duration <= 720:
                        continue
                except (ValueError, OverflowError):
                    continue
                action = {'title': title, 'kind': 'commitment', 'reason': NOTICE + '：按预设案例保留原话。',
                          'evidence': sentence, 'owner_hint': owner, 'remind_at': None,
                          'executor_kind': executor, 'executor_evidence': sentence}
                if execution is not None:
                    action.update(remind_at=execution, execution_at=execution,
                                  execution_evidence=fields['execution'], time_evidence=fields['execution'])
                for field, word in (('deadline', '截止'), ('check', '检查')):
                    if field + '_date' in dates:
                        action[field + '_date'] = dates[field + '_date']
                        action[field + '_evidence'] = word + dates[field + '_date']
                if duration is not None:
                    action.update(duration_minutes=duration, duration_evidence='预计' + fields['duration'] + '分钟')
                actions.append(action)
        return actions

    async def organize(self, text, now, context):
        actions = self.supported_actions(text)
        return {'summary': NOTICE + '：' + ('按预设案例拆分责任人与时间，确认后才启用日程。' if actions else RANGE_NOTICE),
                'key_points': [NOTICE + '：所有客户、项目、录音文本及结果均为虚构教学资料。'],
                'open_questions': [RANGE_NOTICE] if not actions else [], 'actions': actions}


class OfflineCustomerParser:
    async def parse(self, text, now, context=None):
        name = '澄川精密制造集团'
        lead_text = ('新建客户澄川精密制造集团，联系人许工，许工是数据库负责人。'
                     '客户明确需要数据库加密和集中密钥管理。我观察预算可能尚未明确。'
                     '我答应发送数据库加密产品资料，没有约定执行时间。')
        if text == lead_text:
            return {'intent': 'create', 'customer_name': name, 'contact_name': '许工',
                    'basic': {}, 'basic_evidence': {},
                    'contact': {'name': '许工', 'role': '数据库负责人'},
                    'contact_evidence': {'name': '许工', 'role': '许工是数据库负责人'},
                    'attributes': [
                        {'key': 'crypto_needs', 'value': '数据库加密和集中密钥管理', 'basis': 'reported',
                         'target': 'account', 'evidence': '客户明确需要数据库加密和集中密钥管理'},
                        {'key': 'blockers', 'value': '预算可能尚未明确，待核实', 'basis': 'observation',
                         'target': 'account', 'evidence': '我观察预算可能尚未明确'}],
                    'question': NOTICE + '：示例画像需核对确认。'}
        # A second exact phrase supports creating a fresh customer from scratch.
        practice = '新建客户霁星数据研究院，联系人李工，关注数据库加密和密钥管理。'
        if text == practice:
            return {'intent': 'create', 'customer_name': '霁星数据研究院', 'contact_name': '李工',
                    'basic': {}, 'basic_evidence': {}, 'contact': {'name': '李工'},
                    'contact_evidence': {'name': '李工'},
                    'attributes': [{'key': 'crypto_needs', 'value': '数据库加密和密钥管理',
                                    'target': 'account', 'basis': 'reported', 'evidence': '关注数据库加密和密钥管理'}],
                    'question': NOTICE + '：示例画像需核对确认。'}
        selected = (context or {}).get('customer', {})
        if OfflineOrganizer.supported_actions(text):
            return {'intent': 'note', 'customer_name': selected.get('name'), 'contact_name': '',
                    'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                    'attributes': [], 'question': NOTICE + '：请对照原文核对。'}
        return {'intent': 'clarify', 'customer_name': selected.get('name'), 'question': NOTICE + '：' + RANGE_NOTICE}


class OfflineCoach:
    async def advise(self, profile, now):
        return {'summary': NOTICE + '：本条为固定教学建议，请结合演练档案核对。',
                'objective': '练习核对试点范围和采购责任人', 'rationale': NOTICE + '：演练需要补齐可验证的项目边界。',
                'next_moves': [{'title': '核对试点系统与采购责任人', 'reason': NOTICE + '：固定示例建议。',
                                'contact_hint': '以本演练客户联系人为准', 'preparation': '系统清单、验收条件、预算问题',
                                'talk_track': '这次试点覆盖哪个系统？由谁确认采购预算？', 'success_signal': '得到明确的系统与责任人'}],
                'questions': ['哪些条件仍需向客户核实？'], 'risks': ['固定示例不代表真实 AI 推理或行业合规判断。']}


class OfflineResolution:
    """One disclosed vague-name exercise; arbitrary input uses local hints only."""
    available = True
    phrase = '昨天医院陈工那个签名项目。我答应整理病历签名接口问题，没有约定执行时间。'

    async def resolve(self,text,context,now):
        if text != self.phrase:
            raise ValueError('outside offline example')
        customer=next((item for item in context['customers'] if item['name']=='星浦医疗科技集团'),None)
        if customer is None:return {'items':[],'question':NOTICE+'：示例客户不存在，请手动核对。'}
        project=next((item for item in customer['opportunities'] if '签名' in item['name']),None)
        return {'items':[{'customer_id':customer['customer_id'],
            'opportunity_id':project['opportunity_id'] if project else None,'confidence':'medium',
            'reasons':[NOTICE+'：预设“医院＋陈工＋签名”示例映射，请确认归属。']}],
            'question':NOTICE+'：这句话是否属于星浦医疗科技集团的签名项目？'}


class OfflineDiscussion:
    async def reply(self,context,history,text,now):
        return {'answer':NOTICE+'：这是固定讨论示例。先用一次沟通把试点系统、接口约束和验收责任人问清楚，再决定是否推进验证；金额与采购条件仍需核实。',
            'next_moves':[{'title':'核对试点范围与验收责任人','reason':NOTICE+'：练习从讨论形成下一步。',
                'contact_hint':'客户对接人（姓名与职责待确认）','preparation':'准备系统清单、接口问题和验收条件。',
                'success_signal':'客户确认试点范围和验收参与人。'}],
            'questions':['客户最先要解决哪项业务问题？','谁负责技术验收与采购决策？'],
            'risks':['固定示例不代表真实 AI 理解或行业合规判断。']}


class TrainingCustomerService(CustomerService):
    async def handle(self, *args, **kwargs):
        result = await super().handle(*args, **kwargs)
        if result and isinstance(result.get('message'), str) and NOTICE not in result['message']:
            result = {**result, 'message': NOTICE + '\n' + result['message']}
        return result


class OfflineConnector:
    configured = False
    async def fetch(self, title):
        raise TrainingError('离线演练不连接聆记；请手动粘贴虚构录音文本。')


class TrainingAudio(AudioService):
    def capabilities(self):
        result = super().capabilities()
        result.update(provider=None, configured=False, can_transcribe=False,
                      reason='离线演练没有真实 ASR，请粘贴虚构录音文本或使用键盘输入。')
        return result


def _record(crm, key, customer, title, content, now, *, kind='note', category='conversation'):
    record = crm.capture_message(OWNER, 'training-seed:' + key, content, 'web', now)
    if record['title'] != title or record['customer_id'] != customer['id'] or record['kind'] != kind:
        record = crm.update_record(OWNER, record['id'], {'title': title, 'customer_id': customer['id'],
                                   'kind': kind, 'category': category, 'status': 'following'}, now)
    return record


def _customer(crm, name, contact, role, now):
    prior = crm.find_customers_exact(OWNER, name)
    result = prior[0] if prior else crm.create_customer(OWNER, {'name': name, 'contact': contact, 'stage': 'qualified',
                                                  'amount_cents': None, 'notes': '虚构演练客户'}, now)
    person = crm.find_contacts_exact(OWNER, contact, result['id'])[0]
    if person['role'] != role:
        crm.update_contact(OWNER, result['id'], person['id'], {'role': role}, now)
    return result


def _scheduled(crm, record, key, when, created_at, duration=30):
    if record.get('proposal_id') is not None:
        return crm.record_detail(OWNER, record['id'])
    reply = crm.execute(OWNER, 'training-schedule:' + key,
                        {'action': 'propose', 'title': record['title'], 'remind_at': when,
                         'duration_minutes': duration}, created_at)
    match = re.search(r'P([0-9]+)', reply)
    if not match:
        raise TrainingError('演练日程种子创建失败。')
    proposal_id = int(match[1])
    crm.link_proposal(OWNER, record['id'], proposal_id, created_at)
    crm.execute(OWNER, 'training-confirm:' + key, {'action': 'confirm', 'proposal_id': proposal_id}, created_at)
    result = crm.record_detail(OWNER, record['id'])
    if not result.get('task'):
        raise TrainingError('演练日程种子未成功确认。')
    return result


async def seed_scenarios(crm, materials, visits, customer_service, anchor):
    """Use production store/services; first-start manifest prevents resets."""
    with crm._transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS training_seed (training_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)')
        previous = db.execute('SELECT manifest_json FROM training_seed WHERE training_id=?', (TRAINING_ID,)).fetchone()
    if previous:
        return json.loads(previous[0])
    now = anchor
    sales = SalesWorkspace(crm, clock=lambda: now)
    lead_text = ('新建客户澄川精密制造集团，联系人许工，许工是数据库负责人。'
                 '客户明确需要数据库加密和集中密钥管理。我观察预算可能尚未明确。'
                 '我答应发送数据库加密产品资料，没有约定执行时间。')
    lead = await customer_service.handle(OWNER, 'training-seed:lead-create', lead_text, source='web', force=True)
    if not crm.find_customers_exact(OWNER, '澄川精密制造集团'):
        if not lead or not lead.get('draft'):
            raise TrainingError('演练客户种子未生成可确认画像。')
        customer_service.decide(OWNER, lead['draft']['id'], True)
    manufacturing = crm.find_customers_exact(OWNER, '澄川精密制造集团')[0]
    lead_record = crm.get_record(OWNER, lead['record_id'])
    crm.update_record(OWNER, lead_record['id'], {'title': '展会后的数据库加密线索', 'customer_id': manufacturing['id']}, now)
    # Confirmation links the source to its customer. Preserve that fingerprint.
    current_analysis = crm.get_analysis(OWNER, lead_record['id'])
    if current_analysis is None or current_analysis.get('stale'):
        analysis = await customer_service.organizer.organize(lead_text, now, {})
        analysis['input_fingerprint'] = analysis_fingerprint(crm.get_record(OWNER, lead_record['id']))
        current_analysis = crm.save_analysis(OWNER, lead_record['id'], analysis, now)
    lead_todo = crm.adopt_action(OWNER, lead_record['id'], current_analysis['actions'][0]['id'], now)
    city = _customer(crm, '云岚市城运信息中心', '周工', '数据共享接口负责人', now)
    medical = _customer(crm, '星浦医疗科技集团', '陈工', '信息中心项目负责人', now)
    meeting = visits.create(OWNER, {'title': '数据共享与商密整改技术交流', 'customer_id': city['id'], 'occurred_at': now},
                            source_id='training-seed:city-meeting')
    recording_text = ('【虚构录音文本；没有音频，没有真实ASR】\n'
                      f'客户周工答应提供接口清单，截止{_date(anchor, 3)}。\n'
                      f'我答应演示脱敏网关，执行时间{_time(anchor, 2, 15)}，预计45分钟。\n'
                      f'我方团队赵工负责完成兼容验证，截止{_date(anchor, 5)}，检查{_date(anchor, 4)}。')
    recording = visits.add_material(OWNER, meeting['id'], {'role': 'recording', 'provider': 'manual',
        'title': '数据共享会议录音文本（虚构）', 'text': recording_text}, source_id='training-seed:city-recording')
    recap = visits.add_material(OWNER, meeting['id'], {'role': 'recap', 'provider': 'manual',
        'title': '技术交流个人复盘（虚构）', 'text': '【个人观察】我观察客户担心数据出域后的访问审计，需核实脱敏规则和验收指标；这不是客户已确认的要求。'},
        source_id='training-seed:city-recap')
    while await materials.process_one():
        pass
    meeting_detail = visits.detail(OWNER, meeting['id'])
    if any(s['material']['status'] != 'review' for s in meeting_detail['sources']):
        raise TrainingError('演练会议种子整理未完成。')
    first, second = None, None
    for name, stage, amount, amount_type, approval in (
            ('数据库透明加密试点', 'proposal', 18000000, 'estimate', 'unconfirmed'),
            ('电子病历签名验签', 'qualified', 46000000, 'budget', 'approved')):
        existing = next((o for o in sales.opportunities(OWNER, medical['id'])['items'] if o['name'] == name), None)
        op = existing or sales.create_opportunity(OWNER, medical['id'], {'name': name, 'stage': stage,
            'amount_cents': amount, 'amount_type': amount_type, 'approval': approval,
            'scope': '数据库字段透明加密与密钥管理' if amount_type == 'estimate' else '电子病历签名、验签与证据留存',
            'decision_chain': '陈工技术评审；采购与审批人需分别核对', 'notes': '虚构演练项目'})
        if first is None: first = op
        else: second = op
    quote = _record(crm, 'medical-estimate', medical, '透明加密试点金额与审批核对',
                    '数据库透明加密试点估算18万元，预算未审批；不能把估算当作合同金额。', now)
    signature = _record(crm, 'medical-budget', medical, '电子病历签名项目预算核对',
                        '电子病历签名验签项目预算46万元，客户已批准预算；技术验收和采购负责人仍需核实。', now)
    sales.link(OWNER, 'record', quote['id'], first['id'])
    sales.link(OWNER, 'record', signature['id'], second['id'])
    overdue = _record(crm, 'overdue', manufacturing, '发送密钥管理部署方案',
                      f'我答应发送密钥管理部署方案，原执行时间{_time(anchor, -1, 10)}；尚未完成，需要补记实际进展。',
                      _day(anchor, -2, 9), kind='action')
    overdue_detail = _scheduled(crm, overdue, 'overdue', _day(anchor, -1, 10), _day(anchor, -2, 9))
    waiting = _record(crm, 'waiting', medical, '等待客户确认测试环境',
                      f'客户陈工答应确认测试环境，没有约定执行时间；检查{_date(anchor, 3)}是否收到反馈。', now, kind='action')
    crm.save_action_terms(OWNER, waiting['id'], {'executor_kind': 'customer', 'executor_evidence': '客户陈工答应确认测试环境',
                         'execution_at': None, 'check_date': _date(anchor, 3), 'check_evidence': '检查' + _date(anchor, 3)}, now)
    sales.link(OWNER, 'record', waiting['id'], first['id'])
    reschedule = _record(crm, 'reschedule', medical, '原定数据脱敏POC演示', '已核对的数据脱敏POC演示安排。', now, kind='action')
    original_r = _scheduled(crm, reschedule, 'reschedule', _day(anchor, 2, 10), now, 45)
    cancel = _record(crm, 'cancel', medical, '原定密码机选型交流', '已核对的密码机选型交流安排。', now, kind='action')
    original_c = _scheduled(crm, cancel, 'cancel', _day(anchor, 3, 10), now, 30)
    changes = SpokenChangeService(crm, clock=lambda: now)
    changed_r = changes.propose(OWNER, reschedule['id'], '改到' + _date(anchor, 4) + '下午三点，预计45分钟', 'training-seed:reschedule-change')
    changed_c = changes.propose(OWNER, cancel['id'], '取消原提醒', 'training-seed:cancel-change')
    if not changed_r.get('proposal') or not changed_c.get('proposal'):
        raise TrainingError('演练变更种子未生成待确认提案。')
    completed = _record(crm, 'completed', medical, '提交透明加密测试报告', '我已提交透明加密测试报告，客户反馈性能符合本次试点目标。', now, kind='action')
    sales.link(OWNER, 'record', completed['id'], first['id'])
    outcome = sales.complete_record(OWNER, completed['id'], {'request_id': 'training-seed:completed-outcome',
        'result': '虚构结果：测试报告已收到，本次性能指标达标；采购条件仍需核实。',
        'next_step': '向陈工确认试点采购负责人，没有约定执行时间。', 'next_title': '确认试点采购负责人'})
    base = {'anchor_at': anchor, 'base_date': _date(anchor, 0), 'training_id': TRAINING_ID, 'notice': NOTICE,
            'customers': [{'id': c['id'], 'name': c['name']} for c in (manufacturing, city, medical)],
            'practice_customer_input': '新建客户霁星数据研究院，联系人李工，关注数据库加密和密钥管理。'}
    def scene(key, title, description, customer, **fields):
        return {'key': key, 'title': title, 'description': description, 'customer_id': customer['id'],
                'customer_name': customer['name'], **fields}
    base['scenarios'] = [
        scene('lead', '新线索：无时间待办与事实／观察', '事实画像和销售观察分别留依据；TODO没有时间，不会进入日程。', manufacturing,
              record_id=lead_record['id'], record_ids=[lead_record['id'], lead_todo['id']], todo_id=lead_todo['id'], date_relation='无执行时间'),
        scene('meeting', '正式会议：录音文本＋个人复盘', '核对客户、本人、团队责任人，以及执行、截止和检查日期；三项待采纳。', city,
              visit_id=meeting['id'], material_ids=[recording['material']['id'], recap['material']['id']],
              date_relation='我D+2 15:00执行45分钟；客户D+3截止；团队D+5截止、D+4检查'),
        scene('projects', '同客户双项目：分别核对金额和审批', '18万是未审批估算；46万是已批预算；来源分别关联项目。', medical, entry='customer',
              record_id=quote['id'], record_ids=[quote['id'], signature['id']], opportunity_ids=[first['id'], second['id']],
              projects=[{k: o[k] for k in ('id','name','stage','amount_cents','amount_type','approval')} for o in (first, second)]),
        scene('waiting', '逾期推进与等待客户', '一个安排昨天到时仍未完成；一个等待客户，不占个人执行日程。', manufacturing,
              record_id=overdue['id'], record_ids=[overdue['id'], waiting['id']], waiting_customer_id=medical['id'],
              task_ids=[overdue_detail['task']['id']], date_relation='逾期D-1 10:00；等待客户检查D+3'),
        scene('changes', '原任务：待确认改期与取消', '两项原安排仍生效；确认各自提案后才修改或取消。', medical,
              record_id=reschedule['id'], record_ids=[reschedule['id'], cancel['id']],
              task_ids=[original_r['task']['id'], original_c['task']['id']],
              proposal_ids=[changed_r['proposal']['id'], changed_c['proposal']['id']], date_relation='D+2 10:00待改D+4 15:00；D+3 10:00待取消'),
        scene('outcome', '完成结果与无时间下一步', '保留已完成结果，下一步继续关联原项目；没有自动补时间。', medical,
              record_id=completed['id'], record_ids=[completed['id'], outcome['next_record']['id']],
              next_record_id=outcome['next_record']['id'], opportunity_ids=[first['id']], date_relation='下一步无执行时间'),
    ]
    with crm._transaction() as db:
        db.execute('INSERT INTO training_seed VALUES (?,?)', (TRAINING_ID, json.dumps(base, ensure_ascii=False)))
    return base


def runtime_status(manifest):
    return {'mode': 'local', 'training': True, 'label': '本机演练模式（虚构资料）',
            'wecom_connected': False, 'reminders': 'web', 'reminder_status': '演练提醒只在页面显示，不发送网络通知',
            'model_configured': False, 'model_status': NOTICE + '；仅预设案例可整理，自由输入可保存并手动核对',
            'listen_note_configured': False, 'audio_configured': False, 'database': 'training',
            'training_base_date': manifest['base_date'], 'training_scenarios': manifest['scenarios'],
            'training_notice': NOTICE, 'training_manifest_url': '/api/training'}


async def build_training_app(config, *, clock=time.time, stopped=None):
    marker = ensure_training_marker(config, clock())
    store = crm = materials = coaching = None
    try:
        store, crm = Store(config['db_path']), CustomerStore(config['db_path'])
        lock = asyncio.Lock()
        organizer, parser = OfflineOrganizer(), OfflineCustomerParser()
        coaching = CoachingService(crm, OfflineCoach(), lock, clock=clock)
        materials = MaterialService(crm, lock, connector=OfflineConnector(), organizer=organizer,
                                    customer_parser=parser, coaching=coaching, clock=clock)
        visits = VisitService(crm, materials, lock)
        customer_service = TrainingCustomerService(crm, parser, lock, clock=clock, organizer=organizer, coach=coaching)
        manifest = await seed_scenarios(crm, materials, visits, customer_service, marker['anchor_at'])
        write_json(config['manifest_path'], manifest)
        write_json(config['marker_path'], {**marker, 'status': 'ready'})
        app = create_app(store, crm, lock, OWNER, hash_password(PASSWORD), demo=True, connected=lambda: False,
                         public_origin=config['url'], runtime=runtime_status(manifest), clock=clock,
                         organizer=organizer, materials=materials, visits=visits, coaching=coaching,
                         resolution=CustomerResolutionService(crm,SalesWorkspace(crm,clock=clock),lock,OfflineResolution(),clock=clock),
                         discussions=DiscussionService(crm,SalesWorkspace(crm,clock=clock),lock,OfflineDiscussion(),clock=clock),
                         customer_service=customer_service, audio=TrainingAudio(crm))
        async def training_manifest(request):
            return web.json_response(manifest, dumps=lambda value: json.dumps(value, ensure_ascii=False))
        app.router.add_get('/api/training', training_manifest)
        async def training_guide(request):
            guide = APP_ROOT / 'deploy' / '使用指南与案例.html'
            if not guide.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(guide)
        # One fixed public teaching file; no deploy-directory static route.
        app.router.add_get('/guide', training_guide)
        @web.middleware
        async def guide_policy(request, handler):
            result = await handler(request)
            if request.path == '/guide' and result.status == 200:
                source = (APP_ROOT / 'deploy' / '使用指南与案例.html').read_text(encoding='utf-8-sig')
                def hashes(tag):
                    return ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(block.encode('utf-8')).digest()).decode('ascii') + "'"
                                    for block in re.findall(r'<' + tag + r'\b[^>]*>(.*?)</' + tag + '>', source, re.S | re.I))
                result.headers['Content-Security-Policy'] = (
                    "default-src 'self'; script-src 'self' " + hashes('script') + "; style-src 'self' " + hashes('style') +
                    "; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            return result
        app.middlewares.insert(0, guide_policy)
    except BaseException:
        if materials: await materials.close()
        if coaching: await coaching.close()
        if crm: crm.close()
        if store: store.close()
        raise

    async def worker():
        try:
            while True:
                if not await materials.process_one():
                    await asyncio.sleep(.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).error('training_material_worker_failed')
            if stopped is not None: stopped.set()

    async def lifecycle(_app):
        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await materials.close()
            await coaching.close()
            crm.close()
            store.close()
    app.cleanup_ctx.append(lifecycle)
    return app


class TrainingLock:
    def __init__(self, path): self.path, self.file = Path(path), None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise TrainingError('演练数据库已有运行实例，请先停止旧演练。') from None
        return self
    def __exit__(self, *_):
        if self.file: self.file.close()


def check_port(host, port):
    with socket.socket() as probe:
        try:
            if os.name == 'nt': probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((host, port))
        except OSError:
            raise TrainingError('演练端口已占用；不会停止其他服务。') from None


async def serve_training(config, *, state_file, stop_file, instance_id):
    if not re.fullmatch('[a-f0-9]{32}', instance_id):
        raise TrainingError('演练实例编号无效。')
    expected_state = config['root'] / 'deploy/training-process.local.json'
    expected_stop = config['root'] / ('deploy/training-stop-' + instance_id + '.local')
    if Path(state_file).resolve() != expected_state or Path(stop_file).resolve() != expected_stop:
        raise TrainingError('演练进程状态路径无效。')
    state_file, stop_file = expected_state, expected_stop
    with TrainingLock(str(config['db_path']) + '.lock'):
        check_port(config['host'], config['port'])
        stopped = asyncio.Event()
        app = await build_training_app(config, stopped=stopped)
        runner = web.AppRunner(app, access_log=None)
        loop, prior, watcher = asyncio.get_running_loop(), {}, None
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopped.set))
        state = {'pid': os.getpid(), 'executable': str(Path(sys.executable).resolve()),
                 'process_executable': str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                 'app_root': str(config['root']), 'module': 'deploy.training', 'script': str(Path(__file__).resolve()),
                 'instance_id': instance_id, 'url': config['url'], 'port': config['port'],
                 'database': str(config['db_path']), 'stop_file': str(stop_file), 'status': 'ready'}
        try:
            await runner.setup()
            await web.TCPSite(runner, config['host'], config['port']).start()
            write_json(state_file, state)
            async def watch_stop():
                while not stopped.is_set():
                    try:
                        if stop_file.exists() and stop_file.stat().st_size < 256:
                            request = json.loads(stop_file.read_text(encoding='utf-8-sig'))
                            if request.get('instance_id') == instance_id:
                                stopped.set(); return
                    except (OSError, ValueError, AttributeError): pass
                    await asyncio.sleep(.25)
            watcher = asyncio.create_task(watch_stop())
            await stopped.wait()
        finally:
            if watcher:
                watcher.cancel(); await asyncio.gather(watcher, return_exceptions=True)
            await runner.cleanup()
            for sig, previous in prior.items(): signal.signal(sig, previous)
            # Retain every training path; stopped identity documents orderly exit.
            write_json(state_file, {**state, 'status': 'stopped'})


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线演练；虚构资料；不连接外部服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--db')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--state-file', default='deploy/training-process.local.json')
    parser.add_argument('--stop-file')
    parser.add_argument('--instance-id')
    options = parser.parse_args(argv)
    try:
        config = training_config(port=options.port, host=options.host, db_path=options.db)
        if options.check:
            print(json.dumps({'mode': 'training', 'url': config['url'], 'database': DB_NAME,
                              'offline': True, 'demo': True}, ensure_ascii=False))
            return 0
        identifier = options.instance_id or os.urandom(16).hex()
        state = Path(options.state_file)
        state = state if state.is_absolute() else APP_ROOT / state
        stop = Path(options.stop_file) if options.stop_file else APP_ROOT / ('deploy/training-stop-' + identifier + '.local')
        stop = stop if stop.is_absolute() else APP_ROOT / stop
        asyncio.run(serve_training(config, state_file=state, stop_file=stop, instance_id=identifier))
        return 0
    except TrainingError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print('离线演练无法启动，请检查端口、训练路径和进程记录；未打开正式数据库。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
