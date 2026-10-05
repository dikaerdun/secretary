"""Incremental coordination over existing plans, with guarded execution.

Original plan JSON, task IDs, formal execution notices and source rows stay
unchanged during migration. Structured and natural edits share one writer.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import calendar
from datetime import datetime, timedelta
import hashlib
import json
import re
import time
import uuid

from .arrangement_time import ArrangementTimeError, normalize_time, time_end, time_start, time_signature
from .crm import _identifier, _owner
from .store import _timestamp
from .store import SHANGHAI

SCHEMA_VERSION = 1
STATES = ('pending', 'settled', 'paused', 'abandoned')
PLAN_COLUMNS = {
    'schema_version': 'INTEGER NOT NULL DEFAULT 0',
    'settling_state': "TEXT NOT NULL DEFAULT 'pending' CHECK(settling_state IN ('pending','settled','paused','abandoned'))",
    'settling_cycle_id': 'INTEGER NOT NULL DEFAULT 1 CHECK(settling_cycle_id>0)',
    'followup_version': 'INTEGER NOT NULL DEFAULT 1 CHECK(followup_version>0)',
    'deadline_end_at': 'REAL', 'next_check_at': 'REAL',
    'followup_enabled': 'INTEGER NOT NULL DEFAULT 0 CHECK(followup_enabled IN (0,1))',
    'followup_disable_reason': "TEXT NOT NULL DEFAULT 'legacy_no_opt_in'",
    'followup_dirty': 'INTEGER NOT NULL DEFAULT 0 CHECK(followup_dirty IN (0,1))',
    'followup_hold_until': 'REAL', 'hold_turn_id': 'INTEGER',
    'hold_generation': 'INTEGER NOT NULL DEFAULT 0 CHECK(hold_generation>=0)',
}
JSON_DEFAULTS = {
    'settle_deadline': None, 'next_check': None, 'silent_until': None,
    'decision_mode': 'unknown', 'settlement_scope': 'execution_time',
    'proposed_execution': None, 'candidates': [], 'selected_candidate_id': None,
    'agreement': {'status': 'unknown'}, 'application_authority': {'kind': 'none'},
    'waiting_for': None, 'last_progress': None, 'execution_reminder': None,
}
OPERATIONS = {
    'update_progress': {'progress_text', 'check_id', 'check_handled', 'waiting_for', 'next_check'},
    'set_deadline': {'settle_deadline'}, 'set_check': {'next_check', 'followup_enabled'},
    'select_candidate': {'candidate_id'},
    'confirm_arrangement': {'candidate_id', 'proposed_execution', 'agreement_attestation', 'settlement_scope'},
    'pause': {'reason'}, 'resume': {'next_check', 'reuse_next_check', 'followup_enabled'},
    'abandon_coordination': {'reason'},
    'start_reschedule': {'proposed_execution', 'candidates', 'settle_deadline', 'next_check'},
    'continue_set_time': {'next_check', 'settle_deadline'},
    'withdraw_execution': {'reason', 'continue_coordination'}, 'cancel_activity': {'reason'},
}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _string(value, name, maximum=1200, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(name + '无效。')
    return value.strip()


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(name + '需要为 true 或 false。')
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError('请提供有效的安排版本。')
    return value


def _field_signatures(proposal):
    if not isinstance(proposal, dict) or not proposal.get('time_spec'):
        return {}
    spec = normalize_time(proposal['time_spec'], 0, role='execution')
    fields = {'date': _digest({'date': spec['date'], 'end_date': spec.get('end_date')})}
    if spec['precision'] == 'instant':
        fields['time'] = time_signature(spec)
    if proposal.get('place'):
        fields['place'] = _digest(proposal['place'])
    if proposal.get('conditions'):
        fields['conditions'] = _digest(proposal['conditions'])
    return fields


class ArrangementConflict(ValueError):
    pass


def content_signature(proposed_execution, *, settlement_scope='execution_time', decision_mode='unknown'):
    """Bind agreement/authority to execution content, never title or attachments."""
    if not isinstance(proposed_execution, dict) or not proposed_execution.get('time_spec'):
        return None
    spec = normalize_time(proposed_execution['time_spec'], 0, role='execution')
    if settlement_scope == 'date_only':
        spec = normalize_time({'precision': 'date', 'date': spec['date']}, 0, role='execution')
    stable = {'time': time_signature(spec), 'place': proposed_execution.get('place') or '',
              'conditions': proposed_execution.get('conditions') or [], 'all_day': bool(proposed_execution.get('all_day')),
              'settlement_scope': settlement_scope, 'decision_mode': decision_mode}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ArrangementQueue:
    def __init__(self, crm, workspace, lock, *, clock=time.time, flow=None):
        self.crm, self.workspace, self.lock, self.clock, self.flow = crm, workspace, lock, clock, flow
        self.upgrade_schema()

    @contextmanager
    def _transaction(self):
        with self.crm._lock:
            db = self.crm._db
            if not db.in_transaction:
                with self.crm._transaction():
                    yield db
                return
            name = 'arrangement_' + uuid.uuid4().hex
            db.execute('SAVEPOINT ' + name)
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK TO ' + name)
                db.execute('RELEASE ' + name)
                raise
            else:
                db.execute('RELEASE ' + name)

    @staticmethod
    def _exists(db, table):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    @staticmethod
    def _data(row):
        try:
            data = json.loads(row['data_json'])
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def upgrade_schema(self):
        with self._transaction() as db:
            if not self._exists(db, 'crm_secretary_plans'):
                raise ValueError('请先初始化秘书计划根表，再升级安排服务。')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(crm_secretary_plans)')}
            for name, definition in PLAN_COLUMNS.items():
                if name not in columns:
                    db.execute('ALTER TABLE crm_secretary_plans ADD COLUMN ' + name + ' ' + definition)
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS arrangement_plan_identity ON crm_secretary_plans(owner,id)')
            db.execute('CREATE INDEX IF NOT EXISTS arrangement_plan_queue ON crm_secretary_plans(owner,settling_state,deadline_end_at,next_check_at,id)')
            db.execute('CREATE INDEX IF NOT EXISTS arrangement_plan_followup ON crm_secretary_plans(followup_enabled,settling_state,followup_hold_until,id)')
            statements = [
                '''CREATE TABLE IF NOT EXISTS crm_arrangement_operations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                    plan_id INTEGER NOT NULL,operation TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,result_revision INTEGER NOT NULL,response_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at REAL NOT NULL,
                    UNIQUE(owner,request_id),FOREIGN KEY(owner,plan_id) REFERENCES crm_secretary_plans(owner,id))''',
                '''CREATE TABLE IF NOT EXISTS crm_arrangement_notifications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,plan_id INTEGER NOT NULL,
                    settling_cycle_id INTEGER NOT NULL,followup_version INTEGER NOT NULL,dedupe_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('check','deadline_near','deadline_overdue')),
                    due_at REAL NOT NULL,available_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','leased','published','obsolete')),
                    token TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,published_at REAL,read_at REAL,
                    payload_json TEXT NOT NULL,UNIQUE(owner,plan_id,dedupe_key),
                    FOREIGN KEY(owner,plan_id) REFERENCES crm_secretary_plans(owner,id))''',
                'CREATE INDEX IF NOT EXISTS arrangement_notifications_due ON crm_arrangement_notifications(status,available_at,lease_until,id)',
                'CREATE INDEX IF NOT EXISTS arrangement_notifications_owner ON crm_arrangement_notifications(owner,status,published_at,id)',
            ]
            for statement in statements:
                db.execute(statement)
            # Do not reclassify a plan once this migration has been applied.
            for row in db.execute('SELECT * FROM crm_secretary_plans WHERE schema_version=0').fetchall():
                data = self._data(row)
                task = self._task(db, row['owner'], data.get('task_id'))
                state = ('settled' if task and task['status'] in ('pending', 'completed') else
                         'abandoned' if data.get('status') == 'cancelled' or data.get('booking') == 'cancelled' else 'pending')
                source = db.execute('SELECT hidden FROM crm_records WHERE owner=? AND id=?', (row['owner'], row['record_id'])).fetchone()
                hidden = not source or bool(source['hidden'])
                if self._exists(db, 'crm_secretary_trash'):
                    hidden = hidden or bool(db.execute('SELECT 1 FROM crm_secretary_trash WHERE owner=? AND record_id=?',
                                                        (row['owner'], row['record_id'])).fetchone())
                db.execute('''UPDATE crm_secretary_plans SET schema_version=?,settling_state=?,
                    followup_enabled=0,followup_disable_reason=?,followup_dirty=0,followup_hold_until=NULL,
                    hold_turn_id=NULL WHERE owner=? AND id=? AND schema_version=0''',
                    (SCHEMA_VERSION, state, 'source_hidden' if hidden else 'legacy_no_opt_in', row['owner'], row['id']))
        return {'schema_version': SCHEMA_VERSION}

    def _task(self, db, owner, task_id):
        if type(task_id) is not int or task_id <= 0 or not self._exists(db, 'tasks'):
            return None
        return db.execute('SELECT * FROM tasks WHERE owner=? AND id=?', (owner, task_id)).fetchone()

    def _require(self, db, owner, identifier):
        row = db.execute('''SELECT p.* FROM crm_secretary_plans p JOIN crm_records r
            ON r.owner=p.owner AND r.id=p.record_id WHERE p.owner=? AND p.id=? AND r.hidden=0''',
            (owner, identifier)).fetchone()
        if row is None:
            raise KeyError('未找到你的可见安排。')
        if self._exists(db, 'crm_secretary_trash') and db.execute('SELECT 1 FROM crm_secretary_trash WHERE owner=? AND record_id=?',
                (owner, row['record_id'])).fetchone():
            raise KeyError('未找到你的可见安排。')
        return row

    @staticmethod
    def _legacy_execution(data, submitted_at):
        try:
            if data.get('start_at') is not None:
                return {'time_spec': normalize_time(data['start_at'], submitted_at, role='execution'),
                        'place': data.get('place') or '', 'origin': 'legacy'}
            if data.get('date'):
                return {'time_spec': normalize_time(data['date'], submitted_at, role='execution'),
                        'place': data.get('place') or '', 'origin': 'legacy'}
        except (ValueError, TypeError):
            pass
        return None

    def projection(self, db, row, *, now=None):
        """Read authoritative fields without repairing JSON or changing state."""
        row = dict(row)
        data = self._data(row)
        result = {key: row.get(key) for key in PLAN_COLUMNS}
        result.update(plan_id=row['id'], revision=row['revision'], record_id=row['record_id'],
                      title=data.get('title') or '', customer_id=data.get('customer_id'),
                      contact_id=data.get('contact_id'), opportunity_id=data.get('opportunity_id'))
        result.update({key: copy.deepcopy(data[key] if key in data else value) for key, value in JSON_DEFAULTS.items()})
        if 'proposed_execution' not in data:
            result['proposed_execution'] = self._legacy_execution(data, row['created_at'])
        task = self._task(db, row['owner'], data.get('task_id'))
        result['active_schedule'] = ({key: task[key] for key in task.keys() if key != 'owner'}
                                     if task and task['status'] == 'pending' else None)
        result['execution_status'] = task['status'] if task else None
        blockers, flags = [], []
        if not isinstance(result['title'], str) or not result['title'].strip():
            blockers.append('missing_content')
        proposal = result['proposed_execution']
        spec = proposal.get('time_spec') if isinstance(proposal, dict) else None
        # A later writer must use the same canonical validator before commit.
        try:
            spec = normalize_time(spec, row['created_at'], role='execution') if spec is not None else None
        except (ValueError, TypeError):
            spec = None
            blockers.append('invalid_execution')
        if spec is None:
            blockers.append('missing_date')
        elif spec.get('window') == 'calendar':
            blockers.append('missing_date')
            if result['settlement_scope'] != 'date_only':
                blockers.append('missing_clock')
        elif result['settlement_scope'] != 'date_only' and spec['precision'] != 'instant':
            blockers.append('missing_clock')
        if not isinstance(result['candidates'], list):
            result['candidates'] = []
            blockers.append('invalid_candidates')
        if len(result['candidates']) > 1 and not result['selected_candidate_id']:
            blockers.append('candidate_selection')
        if result['decision_mode'] not in ('self', 'external'):
            blockers.append('decision_mode_unknown')
        agreement = result['agreement'] if isinstance(result['agreement'], dict) else {}
        authority = result['application_authority'] if isinstance(result['application_authority'], dict) else {}
        reported = (agreement.get('status') == 'reported' and isinstance(agreement.get('evidence'), str)
                    and bool(agreement['evidence'].strip()))
        if authority.get('kind') not in ('direct_user', 'user_reviewed'):
            blockers.append('user_review_required')
        try:
            signature = content_signature(proposal, settlement_scope=result['settlement_scope'], decision_mode=result['decision_mode'])
        except (ValueError, TypeError):
            signature = None
        result['content_signature'] = signature
        if authority.get('kind') in ('direct_user', 'user_reviewed') and (not signature or authority.get('content_signature') != signature):
            blockers.append('authority_changed')
        required_fields = ['date'] + (['time'] if result['settlement_scope'] != 'date_only' else [])
        if data.get('location_required'):
            required_fields.append('place')
        if isinstance(proposal, dict) and proposal.get('conditions'):
            required_fields.append('conditions')
        try:
            field_signatures = _field_signatures(proposal)
        except (ValueError, TypeError):
            field_signatures = {}
        fields = agreement.get('fields')
        if isinstance(fields, dict):
            missing = [field for field in required_fields if not isinstance(fields.get(field), dict)
                or not fields[field].get('evidence') or fields[field].get('signature') != field_signatures.get(field)]
        else:
            missing = [] if (reported and agreement.get('scope') == result['settlement_scope']
                and signature and agreement.get('content_signature') == signature) else required_fields
        result['agreement_missing_fields'] = missing
        result['agreement_field_signatures'] = field_signatures
        result['agreement_status'] = 'not_required' if result['decision_mode'] == 'self' else 'agreed' if reported and not missing else 'pending'
        if result['decision_mode'] == 'external' and (not reported or missing):
            blockers.append('waiting_for_agreement')
            if reported and (isinstance(fields, dict) and any(field in fields for field in missing)
                             or not fields and agreement.get('content_signature') != signature):
                blockers.append('agreement_changed')
        if data.get('identity_question'):
            blockers.append('identity_review')
        if isinstance(proposal, dict) and proposal.get('all_day'):
            blockers.append('all_day_unsupported')
        if data.get('location_required') and not (proposal.get('place') if isinstance(proposal, dict) else None):
            blockers.append('missing_location')
        if result['settling_state'] != 'pending':
            blockers.append('coordination_inactive')
        now = _timestamp(self.clock() if now is None else now)
        if spec and spec['precision'] == 'instant' and spec['at'] <= now:
            blockers.append('execution_in_past')
        if spec and spec['precision'] == 'instant' and result['settlement_scope'] != 'date_only':
            duration = proposal.get('duration_minutes', data.get('duration_minutes', 30)) if isinstance(proposal, dict) else 30
            if type(duration) is not int or not 5 <= duration <= 720:
                blockers.append('invalid_duration')
            elif self._exists(db, 'tasks') and db.execute('''SELECT 1 FROM tasks WHERE owner=? AND status='pending'
                    AND id!=? AND remind_at<? AND remind_at+duration_minutes*60>? LIMIT 1''',
                    (row['owner'], task['id'] if task and task['status'] == 'pending' else -1,
                     spec['at'] + duration * 60, spec['at'])).fetchone():
                blockers.append('schedule_conflict')
        if isinstance(result['settle_deadline'], dict):
            try:
                deadline = normalize_time(result['settle_deadline'].get('time_spec'), row['created_at'], role='deadline')
                if result['deadline_end_at'] != time_end(deadline):
                    flags.append('deadline_projection_mismatch')
                if deadline and now >= time_end(deadline) and result['settling_state'] in ('pending', 'paused'):
                    flags.append('deadline_overdue')
            except (ValueError, TypeError):
                flags.append('invalid_deadline')
        if isinstance(result['next_check'], dict):
            try:
                check = normalize_time(result['next_check'].get('time_spec'), row['created_at'], role='check')
                if result['next_check_at'] != time_start(check):
                    flags.append('check_projection_mismatch')
                if check and result['deadline_end_at'] is not None and time_start(check) >= result['deadline_end_at']:
                    flags.append('check_after_deadline')
            except (ValueError, TypeError):
                flags.append('invalid_check')
        result['blocking_reasons'] = list(dict.fromkeys(blockers))
        result['attention_flags'] = flags
        result['can_apply'] = not blockers
        result['followup_enabled'] = bool(result['followup_enabled'])
        result['followup_dirty'] = bool(result['followup_dirty'])
        result['suggested_next_check'] = self._suggest_check(result, now)
        for key, table in (('customer', 'crm_customers'), ('contact', 'crm_contacts'), ('opportunity', 'crm_opportunities')):
            identifier = data.get(key + '_id')
            entity = db.execute('SELECT name FROM ' + table + ' WHERE owner=? AND id=?', (row['owner'], identifier)).fetchone() if identifier and self._exists(db, table) else None
            result[key + '_name'] = entity['name'] if entity else ''
        return result

    def get(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            return self.projection(self.crm._db, self._require(self.crm._db, owner, identifier))

    @staticmethod
    def _deadline(value, submitted_at, *, turn_id=None):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {'strength', 'time_spec', 'evidence', 'raw_text', 'origin', 'source_turn_id'}:
            raise ValueError('确定期限字段无效。')
        if value.get('strength', 'target') not in ('target', 'required'):
            raise ValueError('请区分希望期限与最晚期限。')
        spec = normalize_time(value.get('time_spec'), submitted_at, role='deadline')
        if spec is None:
            raise ValueError('确定期限需要日期或时刻。')
        result = {'strength': value.get('strength', 'target'), 'time_spec': spec,
                  'evidence': _string(value.get('evidence', value.get('raw_text', '')), '期限依据', 2000)}
        if turn_id is not None:
            result['source_turn_id'] = turn_id
        return result

    @staticmethod
    def _check(value, submitted_at, *, turn_id=None):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {'id', 'check_id', 'time_spec', 'action', 'origin', 'source_turn_id', 'status', 'handled_at', 'evidence'}:
            raise ValueError('下一推进点字段无效。')
        spec = normalize_time(value.get('time_spec'), submitted_at, role='check')
        if spec is None:
            raise ValueError('下一推进点需要日期或时刻。')
        identifier = uuid.uuid4().hex
        return {'id': identifier, 'check_id': identifier, 'time_spec': spec,
                'action': _string(value.get('action', '核对进展'), '推进动作', 1200),
                'origin': 'automatic' if value.get('origin') == 'automatic' else 'user',
                'source_turn_id': turn_id, 'status': 'planned', 'handled_at': None}

    @staticmethod
    def _execution(value, submitted_at):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {'time_spec', 'place', 'conditions', 'all_day', 'duration_minutes', 'label', 'id', 'candidate_id', 'origin', 'evidence', 'source_turn_id'}:
            raise ValueError('拟执行方案字段无效。')
        spec = normalize_time(value.get('time_spec'), submitted_at, role='execution')
        if spec is None:
            raise ValueError('拟执行方案需要日期或时刻。')
        result = {'time_spec': spec, 'place': _string(value.get('place', ''), '地点', 500),
                  'conditions': value.get('conditions', []), 'all_day': value.get('all_day', False)}
        if not isinstance(result['conditions'], list) or len(result['conditions']) > 20:
            raise ValueError('约定条件应为最多20项文字。')
        result['conditions'] = [_string(item, '约定条件', 500, empty=False) for item in result['conditions']]
        _boolean(result['all_day'], '全天要求')
        if 'duration_minutes' in value:
            duration = value['duration_minutes']
            if type(duration) is not int or not 5 <= duration <= 720:
                raise ValueError('预计用时需要为5至720分钟。')
            result['duration_minutes'] = duration
        if 'label' in value:
            result['label'] = _string(value['label'], '方案说明', 160)
        return result

    def _candidates(self, values, submitted_at, old=None):
        if not isinstance(values, list) or not 1 <= len(values) <= 6:
            raise ValueError('一次活动请提供1至6个候选。')
        previous = {content_signature(item): item for item in old or [] if isinstance(item, dict)}
        result, seen = [], set()
        for value in values:
            candidate = self._execution(value, submitted_at)
            signature = content_signature(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            prior = previous.get(signature) or {}
            identifier = prior.get('id', prior.get('candidate_id')) or uuid.uuid4().hex
            candidate.update(id=identifier, candidate_id=identifier)
            result.append(candidate)
        return result

    def _validate_scope(self, db, owner, row, data):
        # Identity must be rechecked at the actual write, never inferred from a
        # same-company label or trusted only because it was valid at submission.
        customer = data.get('customer_id')
        if self.flow:
            self.flow._scope(db, owner, {key: data[key] for key in ('customer_id', 'contact_id', 'opportunity_id') if data.get(key) is not None})
        if customer is not None:
            self.crm._require_customer(db, owner, _identifier(customer))
        if data.get('contact_id') is not None:
            contact = db.execute('SELECT * FROM crm_contacts WHERE owner=? AND id=? AND archived=0',
                                 (owner, _identifier(data['contact_id']))).fetchone()
            if not contact:
                raise KeyError('未找到你的有效联系人。')
            if not self.flow and customer is not None and contact['customer_id'] != customer and not (
                data.get('opportunity_id') and any(item['contact_id'] == contact['id'] and item.get('membership_valid')
                    for item in self.workspace.stakeholders(owner, customer, data['opportunity_id'])['items'])):
                raise ValueError('联系人与安排单位不一致，请核对。')
        if data.get('opportunity_id') is not None:
            self.workspace._require_opportunity(db, owner, customer, _identifier(data['opportunity_id']))
        if self._exists(db, 'crm_matter_links'):
            for matter in db.execute('''SELECT m.* FROM crm_matter_links l JOIN crm_matters m
                ON m.owner=l.owner AND m.id=l.matter_id WHERE l.owner=? AND l.entity_type='plan' AND l.entity_id=?''',
                (owner, row['id'])).fetchall():
                if matter['visibility'] != 'active' or matter['status'] == 'ended':
                    raise ArrangementConflict('关联事项已收起或结束，请先核对安排归属。')
                if matter['customer_id'] and customer and matter['customer_id'] != customer:
                    raise ValueError('安排与事项单位不一致。')
                if matter['opportunity_id'] and data.get('opportunity_id') and matter['opportunity_id'] != data['opportunity_id']:
                    raise ValueError('安排与事项项目不一致。')

    @staticmethod
    def _rejected_agreement_fields(data, attestation, source_text):
        """Only a present-turn rejection of this proposal's clock revokes it.

        A missing checkbox is not a rejection. A different clock, a question,
        a hypothetical, and a negated rejection must not erase prior consent.
        """
        if not isinstance(attestation, dict):
            return set()
        evidence = attestation.get('evidence')
        if not isinstance(evidence, str) or not evidence.strip() or not isinstance(source_text, str) or evidence not in source_text:
            return set()
        proposal = data.get('proposed_execution') or {}
        try:
            spec = normalize_time(proposal.get('time_spec'), 0, role='execution')
        except (TypeError, ValueError):
            return set()
        if not spec or spec['precision'] != 'instant':
            return set()
        current = datetime.fromtimestamp(spec['at'], SHANGHAI)
        clock_pattern = r'(?:(?:上午|下午|晚上|早上|早晨|中午|傍晚|凌晨)\s*)?[零〇一二两三四五六七八九十\d]{1,3}(?:点|时)(?:[零〇一二两三四五六七八九十\d]{1,3}分|半)?|\d{1,2}[:：]\d{2}|午夜'
        target_pattern = clock_pattern + r'|(?:该|这个|当前|原|现在)?(?:钟点|具体时间|具体时刻|几点)'
        rejected = False
        for clause in re.split(r'[，,。；;\n]', evidence):
            if re.search(r'如果|假如|若是|能否|是否|建议|可能|[?？]', clause):
                continue
            for target in re.finditer(target_pattern, clause):
                if re.search(r'(?:不是|并非|不代表|没(?:有)?说).{0,8}$', clause[:target.start()]):
                    continue
                clock_text = target.group()
                if re.fullmatch(clock_pattern, clock_text):
                    try:
                        proposed = normalize_time('00:00' if clock_text == '午夜' else clock_text, spec['at'], role='execution', base_date=spec['date'])
                    except (TypeError, ValueError):
                        continue
                    local = datetime.fromtimestamp(proposed['at'], SHANGHAI)
                    if (local.hour, local.minute) != (current.hour, current.minute):
                        continue
                if re.search(r'\d{1,4}(?:年|月|日|号|[-/])|(?:下周|本周|周|星期)[一二三四五六日天]|明天|后天|今天', clause[:target.start()]):
                    try:
                        bound = normalize_time(clause[:target.end()].replace('午夜', '00:00'), spec['at'], role='execution', base_date=spec['date'])
                    except (TypeError, ValueError):
                        continue
                    if bound['date'] != spec['date']:
                        continue
                suffix = clause[target.end():]
                if re.match(r'.{0,8}(?:不同意|不行|不方便|未确认|没(?:有)?确认|还没定|未定|没定)', suffix) and not re.match(r'.{0,5}(?:不是|并非|不代表).{0,3}(?:不同意|不行|未确认|没确认)', suffix):
                    rejected = True
                elif re.match(r'.{0,8}(?:已经|已)?(?:同意|确认|确定|约好|没问题)', suffix):
                    # A later affirmative report of this same clock supersedes
                    # an earlier refusal in this one input.
                    rejected = False
        return {'time'} if rejected else set()

    @staticmethod
    def _agreement(data, attestation, *, turn_id=None, rejected_fields=()):
        if not isinstance(attestation, dict) or set(attestation) - {'status', 'scope', 'evidence', 'content_signature', 'fields'}:
            raise ValueError('对方约定依据无效。')
        previous = copy.deepcopy(data.get('agreement') or {})
        fields = previous.get('fields', {})
        if not isinstance(fields, dict):
            raise ValueError('已有约定字段需要核对。')
        if rejected_fields:
            # Legacy full-content consent can be decomposed only if its whole
            # signature still matches the current proposal. Do not invent any
            # date evidence for an already stale or unknown agreement.
            if not fields and previous.get('status') == 'reported' and previous.get('evidence') and previous.get('content_signature') == content_signature(data.get('proposed_execution'),
                    settlement_scope=previous.get('scope', 'execution_time'), decision_mode=data.get('decision_mode', 'unknown')):
                signatures = _field_signatures(data.get('proposed_execution'))
                for key in ['date'] + (['time'] if previous.get('scope') != 'date_only' else []):
                    if key in signatures:
                        fields[key] = {'signature': signatures[key], 'evidence': previous['evidence'], 'source_turn_id': previous.get('source_turn_id')}
            for key in rejected_fields:
                fields.pop(key, None)
        if attestation.get('status') == 'unknown':
            if rejected_fields:
                evidence = _string(attestation.get('evidence'), '约定撤回依据', 2000, empty=False)
                previous.update(fields=fields, scope='date_only', rejected_fields=sorted(rejected_fields),
                    rejection_evidence=evidence, rejection_turn_id=turn_id)
                if not fields:
                    previous['status'] = 'unknown'
                data['agreement'] = previous
            else:
                # Unknown without a bound, explicit rejection does not erase
                # an earlier verified field (nor treat unchecking as refusal).
                data['agreement'] = previous or {'status': 'unknown', 'fields': fields}
            return
        if attestation.get('status') != 'reported' or attestation.get('scope') not in ('date_only', 'execution_time'):
            raise ValueError('请明确本次报告的是日期还是完整时刻约定。')
        evidence = _string(attestation.get('evidence'), '对方约定依据', 2000, empty=False)
        scope = attestation['scope']
        if 'time' in rejected_fields:
            scope = 'date_only'
        if turn_id is not None:
            if re.search(r'几点.{0,5}(?:没定|未定|待定|不确定)|(?:只|仅).{0,5}(?:确认|同意|确定).{0,8}(?:日|号|日期)|钟点.{0,5}(?:没定|未定|待定)', evidence):
                scope = 'date_only'
        signatures = _field_signatures(data.get('proposed_execution'))
        required = ['date'] + (['time'] if scope == 'execution_time' else [])
        for key in required:
            if key in signatures:
                fields[key] = {'signature': signatures[key], 'evidence': evidence, 'source_turn_id': turn_id}
        additional = attestation.get('fields', {})
        if not isinstance(additional, dict) or set(additional) - {'date', 'time', 'place', 'conditions'}:
            raise ValueError('约定字段核对清单无效。')
        if scope == 'date_only' and 'time' in additional:
            raise ValueError('日期核对不能授权未核对的钟点。')
        if turn_id is None:
            for key, item in additional.items():
                if not isinstance(item, dict) or set(item) - {'value', 'signature', 'evidence'}:
                    raise ValueError('请提供明确字段依据和当前展示值签名。')
                if ('value' in item) == ('signature' in item):
                    raise ValueError('核对字段需要明确展示值，不接受空白或重复依据。')
                supplied_signature = item.get('signature')
                if 'value' in item:
                    value = item['value']
                    if key == 'date':
                        spec = normalize_time({'precision': 'date', 'date': value}, 0, role='execution')
                        supplied_signature = _digest({'date': spec['date'], 'end_date': None})
                    elif key == 'time':
                        supplied_signature = time_signature(normalize_time(value, 0, role='execution'))
                    else:
                        supplied_signature = _digest(value)
                if key not in signatures or supplied_signature != signatures[key]:
                    raise ArrangementConflict('核对字段的值已有变化，请按当前方案重新确认。')
                field_evidence = _string(item.get('evidence'), '字段核对依据', 2000, empty=False)
                fields[key] = {'signature': signatures[key], 'evidence': field_evidence, 'source_turn_id': None}
        else:
            # A report of a date/time agreement does not silently approve a
            # place or a participation condition stored from an older turn.
            clauses = [part.strip() for part in re.split(r'[，,。；;\n]', evidence) if part.strip()]
            def agreed_quote(value):
                if not isinstance(value, str) or not value:
                    return None
                for clause in clauses:
                    if value not in clause or re.search(r'没(?:有)?确认|未确认|没同意|不同意|未同意|待确认|需要确认|还没|尚未|不确定|未定|没定|能否|可能|计划|建议|如果|假如|[?？]', clause):
                        continue
                    if re.search(r'确认|确定|同意|答应|约好|没问题', clause):
                        return clause
                return None
            proposal = data.get('proposed_execution') or {}
            quote = agreed_quote(proposal.get('place'))
            if quote and 'place' in signatures:
                fields['place'] = {'signature': signatures['place'], 'evidence': quote, 'source_turn_id': turn_id}
            conditions = proposal.get('conditions') or []
            quotes = [agreed_quote(condition) for condition in conditions]
            if conditions and all(quotes):
                fields['conditions'] = {'signature': signatures['conditions'], 'evidence': '；'.join(dict.fromkeys(quotes)), 'source_turn_id': turn_id}
        data['agreement'] = {'status': 'reported', 'scope': scope, 'evidence': evidence, 'fields': fields,
            'source_turn_id': turn_id, 'content_signature': content_signature(data.get('proposed_execution'),
                settlement_scope=scope, decision_mode=data.get('decision_mode', 'unknown'))}
        if rejected_fields:
            data['agreement'].update(rejected_fields=sorted(rejected_fields), rejection_evidence=evidence, rejection_turn_id=turn_id)

    @staticmethod
    def _mark_check(data, check_id, now):
        check = data.get('next_check')
        if not isinstance(check, dict) or check.get('status') != 'planned' or str(check.get('check_id', check.get('id'))) != str(check_id):
            raise ArrangementConflict('当前推进点已有变化，请按最新进展处理。')
        check.update(status='handled', handled_at=now)

    def _invalidate_notices(self, db, owner, identifier, old, new):
        data, previous = self._data(new), self._data(old)
        inactive = new['settling_state'] != 'pending' or not new['followup_enabled']
        changed_cycle = (old['settling_cycle_id'] != new['settling_cycle_id'] or
                         previous.get('followup_epoch', 0) != data.get('followup_epoch', 0))
        deadline_changed = time_signature((previous.get('settle_deadline') or {}).get('time_spec')) != time_signature((data.get('settle_deadline') or {}).get('time_spec'))
        before_check, after_check = previous.get('next_check') or {}, data.get('next_check') or {}
        check_changed = (before_check.get('check_id', before_check.get('id')) != after_check.get('check_id', after_check.get('id'))
                         or after_check.get('status') != 'planned')
        for notice in db.execute('SELECT * FROM crm_arrangement_notifications WHERE owner=? AND plan_id=? AND status!=?',
                                  (owner, identifier, 'obsolete')).fetchall():
            obsolete = inactive or changed_cycle or (notice['kind'] == 'check' and check_changed) or (notice['kind'] != 'check' and deadline_changed)
            if obsolete:
                db.execute("UPDATE crm_arrangement_notifications SET status='obsolete',token=NULL,lease_until=NULL WHERE owner=? AND id=?", (owner, notice['id']))
            else:
                try:
                    payload = json.loads(notice['payload_json'])
                    payload = payload if isinstance(payload, dict) else {}
                except (TypeError, ValueError):
                    payload = {}
                if notice['status'] == 'leased':
                    # An invalidated lease is not a failed delivery. Legacy
                    # rows lacking a retry marker may follow new quiet policy.
                    payload.setdefault('retry_not_before', None)
                db.execute("UPDATE crm_arrangement_notifications SET followup_version=?,status=CASE WHEN status='leased' THEN 'queued' ELSE status END,token=NULL,lease_until=NULL,payload_json=? WHERE owner=? AND id=?",
                           (new['followup_version'], _encoded(payload), owner, notice['id']))

    def _save(self, db, owner, old, data, columns, now, *, bump_revision):
        values = {key: old[key] for key in PLAN_COLUMNS}
        values.update(columns)
        values['schema_version'] = SCHEMA_VERSION
        values['deadline_end_at'] = time_end((data.get('settle_deadline') or {}).get('time_spec'))
        check = data.get('next_check') or {}
        values['next_check_at'] = time_start(check.get('time_spec')) if check.get('status') == 'planned' else None
        revision = old['revision'] + int(bump_revision)
        statement = ','.join(key + '=?' for key in PLAN_COLUMNS)
        db.execute('UPDATE crm_secretary_plans SET ' + statement + ',data_json=?,revision=?,updated_at=? WHERE owner=? AND id=?',
                   (*[values[key] for key in PLAN_COLUMNS], _encoded(data), revision, now, owner, old['id']))
        row = db.execute('SELECT * FROM crm_secretary_plans WHERE owner=? AND id=?', (owner, old['id'])).fetchone()
        self._invalidate_notices(db, owner, old['id'], old, row)
        return row

    @staticmethod
    def _new_cycle(data, columns, now):
        columns['settling_cycle_id'] += 1
        columns.update(settling_state='pending', followup_enabled=1, followup_disable_reason='')
        for key in ('settle_deadline', 'next_check', 'silent_until'):
            data[key] = None
        data['deadline_registered_at'] = now

    def _resume(self, data, columns, payload, now):
        if 'next_check' in payload and payload.get('reuse_next_check'):
            raise ValueError('请在新推进点和沿用旧点之间选择一项。')
        columns.update(settling_state='pending', followup_enabled=1, followup_disable_reason='')
        data['followup_epoch'] = int(data.get('followup_epoch', 0)) + 1
        data['followup_resumed_at'] = data['deadline_registered_at'] = now
        if 'next_check' in payload:
            data['next_check'] = self._check(payload['next_check'], now)
        elif payload.get('reuse_next_check'):
            check = data.get('next_check')
            if not isinstance(check, dict) or check.get('status') != 'planned' or time_end(check.get('time_spec')) <= now:
                if isinstance(check, dict):
                    check['status'] = 'obsolete'
                columns.update(followup_enabled=0, followup_disable_reason='needs_selection')
            else:
                check = copy.deepcopy(check)
                identifier = uuid.uuid4().hex
                check.update(id=identifier, check_id=identifier)
                data['next_check'] = check
        elif data.get('next_check'):
            data['next_check']['status'] = 'obsolete'
            columns.update(followup_enabled=0, followup_disable_reason='needs_selection')
        if 'followup_enabled' in payload:
            enabled = _boolean(payload['followup_enabled'], '推进提示开关')
            if not (enabled and columns['followup_disable_reason'] == 'needs_selection'):
                columns.update(followup_enabled=int(enabled), followup_disable_reason='' if enabled else 'user_disabled')
        check = data.get('next_check')
        if check and check.get('status') == 'planned':
            data['silent_until'] = {'at': time_start(check['time_spec']), 'source': 'user_check'}
        elif not columns['followup_enabled']:
            data['silent_until'] = None

    def begin_hold(self, db, owner, plan_id, turn_id, now):
        owner, plan_id, turn_id = _owner(owner), _identifier(plan_id), _identifier(turn_id)
        row = self._require(db, owner, plan_id)
        if not db.execute('SELECT 1 FROM crm_secretary_turns WHERE owner=? AND id=? AND plan_id=?', (owner, turn_id, plan_id)).fetchone():
            raise KeyError('未找到属于这次安排的输入。')
        generation = row['hold_generation'] + 1
        db.execute('UPDATE crm_secretary_plans SET followup_dirty=1,hold_turn_id=?,hold_generation=?,followup_hold_until=? WHERE owner=? AND id=?',
                   (turn_id, generation, _timestamp(now) + 240, owner, plan_id))
        return generation

    def release_hold(self, db, owner, plan_id, turn_id, generation, now):
        _timestamp(now)
        result = db.execute('UPDATE crm_secretary_plans SET followup_dirty=0,hold_turn_id=NULL,followup_hold_until=NULL WHERE owner=? AND id=? AND hold_turn_id=? AND hold_generation=?',
                            (_owner(owner), _identifier(plan_id), _identifier(turn_id), _revision(generation)))
        return bool(result.rowcount)

    @staticmethod
    def _suggest_check(arrangement, now):
        check = arrangement.get('next_check')
        if isinstance(check, dict) and check.get('status') == 'planned':
            return None
        deadline = arrangement.get('settle_deadline')
        if not isinstance(deadline, dict) or not deadline.get('time_spec'):
            return None
        try:
            if time_end(deadline['time_spec']) <= now:
                return None
        except (ValueError, TypeError):
            return None
        today = datetime.fromtimestamp(now, SHANGHAI).date()
        last_day = datetime.fromisoformat(deadline['time_spec']['date']).date()
        distance = (last_day - today).days
        target = min(last_day, today + timedelta(days=0 if distance <= 2 else 2 if distance <= 7 else 7))
        return {'time_spec': normalize_time({'precision': 'date', 'date': target.isoformat()}, now, role='check'),
                'action': '核对当前安排进展', 'origin': 'suggestion', 'label': '秘书建议，可调整'}

    def _adopt_default_check(self, db, owner, data, columns, now):
        if not columns['followup_enabled'] or columns['settling_state'] != 'pending':
            return
        if self._exists(db, 'crm_secretary_settings'):
            setting = db.execute('SELECT * FROM crm_secretary_settings WHERE owner=?', (owner,)).fetchone()
            enabled = setting and 'arrangement_auto_check' in setting.keys() and bool(setting['arrangement_auto_check'])
            if not enabled:
                return
        else:
            return
        suggestion = self._suggest_check(data, now)
        if suggestion:
            suggestion.pop('label')
            suggestion['origin'] = 'automatic'
            data['next_check'] = self._check(suggestion, now)

    def _response(self, db, owner, row, effect='saved', receipt='安排已保存。'):
        arrangement = self.projection(db, row)
        return {'plan_id': row['id'], 'revision': row['revision'], 'arrangement': arrangement,
                'active_schedule': arrangement['active_schedule'], 'receipt': receipt, 'effect': effect,
                'attention_flags': arrangement['attention_flags']}

    def apply_current(self, db, owner, plan_id, now, *, expected_task_revision=None, source_turn_id=None):
        """Common execution commit. Incomplete intent is saved, never guessed.

The caller owns the SQLite transaction and plan revision. The task proposal
and confirmation, CRM association and authoritative plan update are one unit.
"""
        owner, plan_id, now = _owner(owner), _identifier(plan_id), _timestamp(now)
        row = self._require(db, owner, plan_id)
        data = self._data(row)
        self._validate_scope(db, owner, row, data)
        projection = self.projection(db, row, now=now)
        task = self._task(db, owner, data.get('task_id'))
        active = task if task and task['status'] == 'pending' else None
        settled_same_content = row['settling_state'] == 'settled' and data.get('applied_signature') == projection['content_signature']
        if settled_same_content and (data.get('settlement_scope') == 'date_only' or not active):
            return self._response(db, owner, row, 'already_settled', '这次安排已落实，没有重复建立日程。')
        remaining_blockers = [code for code in projection['blocking_reasons'] if not (settled_same_content and code == 'coordination_inactive')]
        if remaining_blockers:
            return self._response(db, owner, row, 'saved', '拟定安排已保存，信息或约定尚需补充，没有改变有效日程。')
        columns = {key: row[key] for key in PLAN_COLUMNS}
        if data.get('settlement_scope') == 'date_only':
            columns.update(settling_state='settled', followup_enabled=0, followup_disable_reason='settled', followup_version=row['followup_version'] + 1)
            data['applied_signature'] = projection['content_signature']
            data['date_settled_at'] = now
            spec = data['proposed_execution']['time_spec']
            data.update(date=spec['date'], start_at=None, status='tentative')
            row = self._save(db, owner, row, data, columns, now, bump_revision=False)
            return self._response(db, owner, row, 'date_settled', '日期已定，钟点待定；没有建立或改动正式日程。')
        if active and (_revision(expected_task_revision) != active['revision']):
            raise ArrangementConflict('当前有效日程已有更新，请核对最新安排。')
        proposal = data['proposed_execution']
        at = proposal['time_spec']['at']
        duration = proposal.get('duration_minutes', data.get('duration_minutes', 30))
        reminder = data.get('execution_reminder')
        notice_at = None
        if isinstance(reminder, dict) and reminder.get('enabled') is False:
            notice_at = None
        elif isinstance(reminder, dict) and reminder.get('at') is not None:
            notice_at = _timestamp(reminder['at'])
        elif isinstance(reminder, dict) and 'minutes' in reminder:
            notice_at = at - reminder['minutes'] * 60
        elif data.get('reminder_at') is not None:
            notice_at = _timestamp(data['reminder_at'])
        else:
            minutes = reminder.get('minutes') if isinstance(reminder, dict) and 'minutes' in reminder else data.get('remind_minutes')
            if minutes is None and self.flow:
                minutes = self.flow.settings(owner).get('remind_minutes')
            if minutes is not None:
                if type(minutes) is not int or not 0 <= minutes <= 10080:
                    raise ValueError('提前提醒分钟数无效。')
                notice_at = at - minutes * 60
        if notice_at is not None and notice_at > at:
            raise ValueError('执行提醒不能晚于执行开始。')
        if notice_at is not None and notice_at <= now:
            # An already missed notification cannot be sent in the past. The
            # execution remains valid, with an explicit saved explanation.
            notice_at = None
            data['execution_notice_issue'] = '指定的提前提醒时刻已过去，本次日程不补发过去提醒。'
        execution_signature = _digest({'title': data['title'], 'at': at, 'duration_minutes': duration, 'execution_notice_at': notice_at})
        current_notice = db.execute("SELECT due_at FROM notifications WHERE task_id=? AND task_revision=? AND status IN ('queued','leased','sent') ORDER BY id DESC LIMIT 1",
                                   (active['id'], active['revision'])).fetchone() if active else None
        current_notice_at = current_notice['due_at'] if current_notice else None
        if (active and active['title'] == data['title'] and active['remind_at'] == at
                and active['duration_minutes'] == duration and data.get('applied_signature') == projection['content_signature']
                and current_notice_at == notice_at):
            columns.update(settling_state='settled', followup_enabled=0, followup_disable_reason='settled')
            row = self._save(db, owner, row, data, columns, now, bump_revision=False)
            return self._response(db, owner, row, 'already_settled', '当前有效安排一致，没有重复建立日程。')
        command = {'action': 'propose_change' if active else 'propose', 'title': data['title'], 'remind_at': at,
            'duration_minutes': duration, 'execution_notice_at': notice_at,
            'schedule_note': '依据本次明确决定落实安排；推进期限与执行时间独立。'}
        if active:
            command['task_id'] = active['id']
        # Recheck the scheduler's invariants before touching any proposal or
        # existing execution notice. A conflict never withdraws an old task.
        problem = self.crm._proposal_problem(db, {**command, 'owner': owner, 'deadline_at': active['deadline_at'] if active else None}, now,
                                             exclude_task_id=active['id'] if active else None)
        if problem:
            raise ArrangementConflict(problem)
        message = self.crm._execute(db, owner, command, now)
        import re
        match = re.search(r'P([0-9]+)', message)
        if not match:
            raise ValueError('执行提案未建立：' + message)
        proposal_id = int(match[1])
        self.crm._execute(db, owner, {'action': 'confirm', 'proposal_id': proposal_id}, now)
        scheduled = db.execute('SELECT * FROM proposals WHERE owner=? AND id=?', (owner, proposal_id)).fetchone()
        if not scheduled or scheduled['status'] != 'confirmed':
            raise ArrangementConflict('执行条件发生变化，原日程保持不变，请重新核对。')
        old_source = db.execute('SELECT proposal_id FROM crm_records WHERE owner=? AND id=?', (owner, row['record_id'])).fetchone()
        if old_source and old_source['proposal_id']:
            self.crm._remember_proposal(db, owner, row['record_id'], old_source['proposal_id'], now)
        self.crm._remember_proposal(db, owner, row['record_id'], proposal_id, now)
        db.execute("UPDATE crm_records SET proposal_id=?,kind='action',updated_at=? WHERE owner=? AND id=?",
                   (proposal_id, now, owner, row['record_id']))
        if data.get('opportunity_id') and self.flow:
            self.flow._link_project(db, owner, 'record', row['record_id'], data['opportunity_id'], now)
        data.update(task_id=scheduled['task_id'], status='scheduled', booking='confirmed' if data.get('decision_mode') == 'external' else data.get('booking', 'unknown'),
                    date=proposal['time_spec']['date'], start_at=at, duration_minutes=duration, reminder_at=notice_at,
                    applied_signature=projection['content_signature'], applied_execution_signature=execution_signature, execution_notice_at=notice_at)
        columns.update(settling_state='settled', followup_enabled=0, followup_disable_reason='settled', followup_version=row['followup_version'] + 1)
        row = self._save(db, owner, row, data, columns, now, bump_revision=False)
        return self._response(db, owner, row, 'rescheduled' if active else 'scheduled',
            '已更新当前有效日程，原日程编号保留。' if active else '已加入正式日程。' + ('未启用提前执行提醒。' if notice_at is None else '已保存提前执行提醒。'))

    def _withdraw(self, db, owner, row, data, expected_task_revision, now):
        task = self._task(db, owner, data.get('task_id'))
        if task and task['status'] == 'pending':
            if _revision(expected_task_revision) != task['revision']:
                raise ArrangementConflict('当前有效日程已有更新，请刷新后核对。')
            self.crm._execute(db, owner, {'action': 'cancel', 'task_id': task['id']}, now)
        data['status'] = 'tentative'
        data['execution_withdrawn_at'] = now
        # Keep task_id for history, but active_schedule reads its actual status.
        return task is not None and task['status'] == 'pending'

    def _apply_fields(self, data, columns, changes, submitted_at, now, *, turn_id=None, authorized=False, rejected_fields=()):
        if 'settle_deadline' in changes:
            before = time_signature((data.get('settle_deadline') or {}).get('time_spec'))
            data['settle_deadline'] = self._deadline(changes['settle_deadline'], submitted_at, turn_id=turn_id)
            if before != time_signature((data.get('settle_deadline') or {}).get('time_spec')):
                data['deadline_registered_at'] = now
        if 'next_check' in changes:
            data['next_check'] = self._check(changes['next_check'], submitted_at, turn_id=turn_id)
            data['silent_until'] = {'at': time_start(data['next_check']['time_spec']), 'source': 'user_check'} if data['next_check'] else None
        if any(key in changes and changes[key] is not None for key in ('next_check', 'settle_deadline')) and columns['followup_disable_reason'] == 'legacy_no_opt_in' and columns['settling_state'] == 'pending':
            columns.update(followup_enabled=1, followup_disable_reason='')
        if 'proposed_execution' in changes:
            old = data.get('proposed_execution') or {}
            execution = self._execution(changes['proposed_execution'], submitted_at)
            # A clock supplement must not erase an explicitly known location or
            # required condition just because it wasn't repeated in this turn.
            if execution is not None and isinstance(changes['proposed_execution'], dict):
                for key in ('place', 'conditions', 'duration_minutes', 'all_day'):
                    if key not in changes['proposed_execution'] and key in old:
                        execution[key] = copy.deepcopy(old[key])
            data['proposed_execution'] = execution
            data['selected_candidate_id'] = None
            data['date'] = execution['time_spec']['date'] if execution and execution['time_spec'].get('window') != 'calendar' else None
            data['start_at'] = execution['time_spec']['at'] if execution and execution['time_spec']['precision'] == 'instant' else None
            if execution and execution.get('place'):
                data['place'] = execution['place']
        if 'candidates' in changes:
            data['candidates'] = self._candidates(changes['candidates'], submitted_at, data.get('candidates'))
            if data.get('selected_candidate_id') not in {item['id'] for item in data['candidates']}:
                data['selected_candidate_id'] = None
        for key, choices in (('decision_mode', ('self', 'external', 'unknown')), ('settlement_scope', ('date_only', 'execution_time'))):
            if key in changes:
                if changes[key] not in choices:
                    raise ValueError('安排决定方式或完成范围无效。')
                data[key] = changes[key]
        for key in ('waiting_for', 'last_progress'):
            if key in changes:
                value = changes[key]
                if value is not None:
                    if not isinstance(value, dict) or set(value) - {'text', 'evidence'}:
                        raise ValueError('推进记录字段无效。')
                    value = {'text': _string(value.get('text', ''), '推进记录', 6000),
                             'evidence': _string(value.get('evidence', ''), '进展依据', 6000), 'source_turn_id': turn_id, 'at': now}
                data[key] = value
        if 'agreement' in changes:
            self._agreement(data, changes['agreement'], turn_id=turn_id, rejected_fields=rejected_fields)
        if 'application_authority' in changes:
            authority = changes['application_authority']
            if not isinstance(authority, dict) or authority.get('kind') not in ('none', 'direct_user', 'user_reviewed'):
                raise ValueError('安排执行授权无效。')
            kind = authority['kind'] if authorized else 'none'
            data['application_authority'] = {'kind': kind, 'source_turn_id': turn_id,
                'evidence': _string(authority.get('evidence', ''), '本次安排依据', 6000),
                'content_signature': content_signature(data.get('proposed_execution'),
                    settlement_scope=data.get('settlement_scope', 'execution_time'), decision_mode=data.get('decision_mode', 'unknown')) if kind != 'none' else None}
        if 'execution_reminder' in changes:
            reminder = changes['execution_reminder']
            if reminder is not None and (not isinstance(reminder, dict) or set(reminder) - {'enabled', 'at', 'minutes', 'evidence'}):
                raise ValueError('执行提醒字段无效。')
            if reminder is not None:
                if 'enabled' in reminder:
                    _boolean(reminder['enabled'], '执行提醒开关')
                if reminder.get('at') is not None:
                    _timestamp(reminder['at'])
                if 'minutes' in reminder and (type(reminder['minutes']) is not int or not 0 <= reminder['minutes'] <= 10080):
                    raise ValueError('提前提醒分钟数无效。')
            data['execution_reminder'] = reminder

    def _operate(self, db, owner, row, data, columns, operation, payload, now):
        state = columns['settling_state']
        effect, receipt = 'saved', '本次选择已保存。'
        if operation in ('select_candidate', 'confirm_arrangement', 'pause', 'abandon_coordination') and state not in ('pending', 'paused'):
            if operation != 'confirm_arrangement' or state != 'settled':
                raise ValueError('本轮协调已结束，请先明确重新协调。')
        if operation == 'update_progress':
            progress = _string(payload.get('progress_text'), '本次进展', 6000, empty=False)
            data['last_progress'] = {'text': progress, 'at': now, 'origin': 'user'}
            if 'check_handled' in payload:
                handled = _boolean(payload['check_handled'], '检查点处理选择')
                if handled:
                    self._mark_check(data, _string(payload.get('check_id'), '检查点编号', 200, empty=False), now)
            elif 'check_id' in payload:
                raise ValueError('只有明确完成当前推进行动时才消费检查点。')
            if 'waiting_for' in payload:
                data['waiting_for'] = {'text': _string(payload['waiting_for'], '等待进展', 1200), 'at': now} if payload['waiting_for'] is not None else None
            if 'next_check' in payload:
                self._apply_fields(data, columns, {'next_check': payload['next_check']}, now, now)
        elif operation in ('set_deadline', 'set_check'):
            key = 'settle_deadline' if operation == 'set_deadline' else 'next_check'
            if key not in payload:
                raise ValueError('请提供新的时间或明确清空。')
            self._apply_fields(data, columns, {key: payload[key]}, now, now)
            if 'followup_enabled' in payload:
                enabled = _boolean(payload['followup_enabled'], '推进提示开关')
                if enabled and (state != 'pending' or columns['followup_disable_reason'] in ('source_hidden', 'paused', 'needs_selection')):
                    raise ValueError('请通过恢复推进重新选择，不因修改时间重启旧提示。')
                columns.update(followup_enabled=int(enabled), followup_disable_reason='' if enabled else 'user_disabled')
        elif operation in ('select_candidate', 'confirm_arrangement'):
            if payload.get('candidate_id') is not None and 'proposed_execution' in payload:
                raise ValueError('候选和自定义方案只能选一个。')
            if payload.get('candidate_id') is not None:
                identifier = _string(payload['candidate_id'], '候选编号', 200, empty=False)
                candidate = next((item for item in data.get('candidates', []) if item.get('id', item.get('candidate_id')) == identifier), None)
                if not candidate:
                    raise ArrangementConflict('这个候选已有变化，请核对最新方案。')
                data['proposed_execution'] = copy.deepcopy(candidate)
                data['selected_candidate_id'] = identifier
            elif operation == 'select_candidate':
                raise ValueError('请选择一个有效候选。')
            elif 'proposed_execution' in payload:
                self._apply_fields(data, columns, {'proposed_execution': payload['proposed_execution']}, now, now)
            if operation == 'confirm_arrangement':
                if state == 'paused':
                    raise ValueError('协调已暂缓，请先明确恢复。')
                if 'settlement_scope' in payload:
                    self._apply_fields(data, columns, {'settlement_scope': payload['settlement_scope']}, now, now)
                if 'agreement_attestation' in payload:
                    self._agreement(data, payload['agreement_attestation'])
                data['application_authority'] = {'kind': 'user_reviewed', 'evidence': '用户核对本次展示的方案并允许落实',
                    'content_signature': content_signature(data.get('proposed_execution'), settlement_scope=data.get('settlement_scope', 'execution_time'),
                                                           decision_mode=data.get('decision_mode', 'unknown'))}
                if state == 'settled' and data.get('applied_signature') != data['application_authority']['content_signature']:
                    raise ValueError('修改已落实安排需先进入商量改期，原日程继续保留。')
        elif operation == 'pause':
            if 'reason' in payload:
                data['pause_reason'] = _string(payload['reason'], '暂停说明', 1200)
            columns.update(settling_state='paused', followup_enabled=0, followup_disable_reason='paused')
            effect, receipt = 'paused', '已暂缓协调，推进提示停止；当前有效日程继续保留。'
        elif operation == 'resume':
            if state not in ('pending', 'paused'):
                raise ValueError('请使用重新协调开启新一轮，不从结束状态直接恢复。')
            if 'reuse_next_check' in payload:
                _boolean(payload['reuse_next_check'], '沿用推进点选择')
            self._resume(data, columns, payload, now)
            effect, receipt = 'resumed', '已恢复协调。' + ('需要选择新的推进点，旧提示保持停用。' if not columns['followup_enabled'] else '按这次明确选择继续推进。')
        elif operation == 'abandon_coordination':
            if 'reason' in payload:
                data['abandon_reason'] = _string(payload['reason'], '放弃说明', 1200)
            columns.update(settling_state='abandoned', followup_enabled=0, followup_disable_reason='abandoned')
            effect, receipt = 'abandoned', '已放弃本轮协调，当前有效日程继续保留。'
        elif operation in ('start_reschedule', 'continue_set_time'):
            if state == 'paused':
                raise ValueError('协调已暂缓，请先明确恢复推进。')
            active = self._task(db, owner, data.get('task_id'))
            if active and active['status'] == 'pending' and _revision(payload.get('expected_task_revision')) != active['revision']:
                raise ArrangementConflict('当前有效日程已有变化，请核对最新安排。')
            if operation == 'continue_set_time' and (state != 'settled' or data.get('settlement_scope') != 'date_only'):
                raise ValueError('只有已落实日期目标才能继续确定钟点。')
            if state in ('settled', 'abandoned'):
                self._new_cycle(data, columns, now)
            columns['settling_state'] = 'pending'
            if state == 'abandoned' or data.get('status') == 'cancelled':
                data.update(status='tentative', booking='unknown')
                db.execute("UPDATE crm_records SET status='following',updated_at=? WHERE owner=? AND id=?", (now, owner, row['record_id']))
            data['application_authority'] = {'kind': 'none'}
            if operation == 'continue_set_time':
                data['settlement_scope'] = 'execution_time'
            elif 'proposed_execution' not in payload and 'candidates' not in payload:
                data['proposed_execution'] = None
                data['candidates'] = []
                data['selected_candidate_id'] = None
            self._apply_fields(data, columns, {key: payload[key] for key in ('proposed_execution', 'candidates', 'settle_deadline', 'next_check') if key in payload}, now, now)
            effect, receipt = 'coordination_started', '已继续协调，当前有效日程保留；本轮未明确的期限与推进点不从旧周期继承。'
        elif operation in ('withdraw_execution', 'cancel_activity'):
            if 'reason' in payload:
                data['withdraw_reason'] = _string(payload['reason'], '取消说明', 1200)
            cancelled = self._withdraw(db, owner, row, data, payload.get('expected_task_revision'), now)
            ongoing = operation == 'withdraw_execution' and _boolean(payload.get('continue_coordination', False), '继续协调选择')
            if ongoing:
                if state in ('settled', 'abandoned'):
                    self._new_cycle(data, columns, now)
                columns['settling_state'] = 'pending'
                data['proposed_execution'] = None
                data['application_authority'] = {'kind': 'none'}
            else:
                columns.update(settling_state='abandoned', followup_enabled=0, followup_disable_reason='abandoned')
            if operation == 'cancel_activity':
                data.update(status='cancelled', booking='cancelled')
                db.execute("UPDATE crm_records SET status='done',updated_at=? WHERE owner=? AND id=?", (now, owner, row['record_id']))
            effect, receipt = 'cancelled', ('当前有效日程及其待发执行提醒已取消。' if cancelled else '本轮协调已停止。') + ('新时间待定，可继续协调。' if ongoing else '其他活动、事项和项目继续保留。')
        return effect, receipt

    def apply_decision(self, owner, plan_id, payload):
        owner, plan_id = _owner(owner), _identifier(plan_id)
        if not isinstance(payload, dict) or payload.get('operation') not in OPERATIONS:
            raise ValueError('安排操作无效。')
        operation = payload['operation']
        common = {'operation', 'request_id', 'expected_revision'}
        if operation in ('confirm_arrangement', 'start_reschedule', 'continue_set_time', 'withdraw_execution', 'cancel_activity'):
            common.add('expected_task_revision')
        if set(payload) - common - OPERATIONS[operation]:
            raise ValueError('本次安排操作包含无效字段。')
        request_id = _string(payload.get('request_id'), '请求标识', 200, empty=False)
        expected = _revision(payload.get('expected_revision'))
        digest = _digest({'plan_id': plan_id, 'payload': payload})
        with self._transaction() as db:
            old_operation = db.execute('SELECT * FROM crm_arrangement_operations WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
            if old_operation:
                if old_operation['payload_hash'] != digest:
                    raise ArrangementConflict('请求标识已用于不同内容，旧选择未被覆盖。')
                return json.loads(old_operation['response_json'])
            row = self._require(db, owner, plan_id)
            if row['revision'] != expected:
                raise ArrangementConflict('这次安排已有更新，请保留输入并核对最新版本。')
            data = {**copy.deepcopy(JSON_DEFAULTS), **self._data(row)}
            if 'proposed_execution' not in self._data(row):
                data['proposed_execution'] = self._legacy_execution(data, row['created_at'])
            self._validate_scope(db, owner, row, data)
            columns = {key: row[key] for key in PLAN_COLUMNS}
            columns.update(followup_version=row['followup_version'] + 1, followup_dirty=0, followup_hold_until=None,
                           hold_turn_id=None, hold_generation=row['hold_generation'] + 1)
            now = _timestamp(self.clock())
            effect, receipt = self._operate(db, owner, row, data, columns, operation, payload, now)
            if operation in ('update_progress', 'set_deadline') and 'next_check' not in payload:
                self._adopt_default_check(db, owner, data, columns, now)
            before = {'columns': {key: row[key] for key in PLAN_COLUMNS}, 'data': self._data(row)}
            new = self._save(db, owner, row, data, columns, now, bump_revision=True)
            if operation == 'confirm_arrangement':
                result = self.apply_current(db, owner, plan_id, now, expected_task_revision=payload.get('expected_task_revision'))
            else:
                result = self._response(db, owner, new, effect, receipt)
            final = self._require(db, owner, plan_id)
            after = {'columns': {key: final[key] for key in PLAN_COLUMNS}, 'data': self._data(final)}
            db.execute('''INSERT INTO crm_arrangement_operations(owner,request_id,plan_id,operation,payload_hash,base_revision,result_revision,response_json,before_json,after_json,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (owner, request_id, plan_id, operation, digest, expected, final['revision'], _encoded(result), _encoded(before), _encoded(after), now))
            return result

    def sync_from_turn(self, db, owner, plan_id, semantic, turn_id, submitted_at, now):
        """Called inside flow's guarded turn commit, which owns revision/history."""
        owner, plan_id, turn_id = _owner(owner), _identifier(plan_id), _identifier(turn_id)
        submitted_at, now = _timestamp(submitted_at), _timestamp(now)
        row = self._require(db, owner, plan_id)
        data = {**copy.deepcopy(JSON_DEFAULTS), **self._data(row)}
        columns = {key: row[key] for key in PLAN_COLUMNS}
        if not isinstance(semantic, dict) or not isinstance(semantic.get('changes', {}), dict):
            raise ValueError('安排理解无效，原话保留。')
        turn = db.execute('SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?', (owner, turn_id)).fetchone()
        if turn is None:
            raise KeyError('未找到你的输入。')
        if turn['plan_id'] is not None and turn['plan_id'] != plan_id:
            raise ValueError('本次输入不属于这个安排。')
        hold_generation = self._data(turn).get('arrangement_hold_generation')
        if hold_generation is not None and (row['hold_turn_id'] != turn_id or row['hold_generation'] != hold_generation or not row['followup_hold_until'] or row['followup_hold_until'] <= now):
            raise ArrangementConflict('新补充的处理已被后续操作替代或超时，原话已保留。')
        columns.update(schema_version=1, followup_version=row['followup_version'] + 1)
        if semantic.get('new_plan'):
            columns.update(settling_state='pending', followup_enabled=1, followup_disable_reason='')
        authorized = semantic.get('source_authorized') is True
        if semantic.get('intent') == 'recap':
            columns.update(settling_state='settled', followup_enabled=0, followup_disable_reason='settled')
        elif authorized:
            operation = semantic.get('operation')
            changes = semantic.get('changes', {})
            source = db.execute('SELECT content FROM crm_records WHERE owner=? AND id=?', (owner, turn['record_id'])).fetchone() if 'record_id' in turn.keys() else None
            rejections = self._rejected_agreement_fields(data, changes.get('agreement'), source['content'] if source else '') if 'source_kind' in turn.keys() and turn['source_kind'] == 'user' else set()
            if rejections and columns['settling_state'] == 'settled' and operation not in ('cancel_activity', 'withdraw_execution', 'abandon_coordination'):
                self._new_cycle(data, columns, now)
                data.update(status='tentative', booking='unknown')
                data['application_authority'] = {'kind': 'none'}
            if not operation and columns['settling_state'] == 'settled' and ('proposed_execution' in changes or 'candidates' in changes):
                if 'proposed_execution' in changes:
                    changed_execution = self._execution(changes['proposed_execution'], submitted_at)
                    execution_changed = content_signature(changed_execution) != content_signature(data.get('proposed_execution'))
                else:
                    execution_changed = True
                if execution_changed:
                    self._new_cycle(data, columns, now)
                    data['application_authority'] = {'kind': 'none'}
            if operation:
                if operation not in OPERATIONS:
                    raise ValueError('安排理解操作无效。')
                op_payload = {key: changes[key] for key in OPERATIONS[operation] if key in changes}
                if semantic.get('expected_task_revision') is not None:
                    op_payload['expected_task_revision'] = semantic['expected_task_revision']
                if operation == 'withdraw_execution':
                    if not semantic.get('withdraw_explicit'):
                        raise ValueError('撤销原日程需要本次明确依据。')
                    op_payload['continue_coordination'] = True
                if operation == 'resume' and 'next_check' not in changes:
                    op_payload['reuse_next_check'] = True
                self._operate(db, owner, row, data, columns, operation, op_payload, now)
            self._apply_fields(data, columns, changes, submitted_at, now, turn_id=turn_id, authorized=authorized, rejected_fields=rejections)
            if rejections:
                data['application_authority'] = {'kind': 'none'}
            if semantic.get('check_handled') and data.get('next_check') and 'next_check' not in changes:
                self._mark_check(data, data['next_check'].get('check_id', data['next_check'].get('id')), now)
            if 'next_check' not in changes:
                self._adopt_default_check(db, owner, data, columns, now)
        self._validate_scope(db, owner, row, data)
        columns.update(followup_dirty=0, followup_hold_until=None, hold_turn_id=None)
        new = self._save(db, owner, row, data, columns, now, bump_revision=False)
        if authorized and semantic.get('intent') != 'recap' and (columns['settling_state'] == 'pending' or
                columns['settling_state'] == 'settled' and 'execution_reminder' in semantic.get('changes', {})):
            return self.apply_current(db, owner, plan_id, now,
                expected_task_revision=semantic.get('expected_task_revision'), source_turn_id=turn_id)
        return self._response(db, owner, new)

    def source_hidden(self, db, owner, record_id, now):
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        rows = db.execute('''SELECT p.* FROM crm_secretary_plans p WHERE p.owner=? AND
            (p.record_id=? OR p.hold_turn_id IN (SELECT id FROM crm_secretary_turns WHERE owner=? AND record_id=?))''',
            (owner, record_id, owner, record_id)).fetchall()
        for row in rows:
            db.execute('''UPDATE crm_secretary_plans SET followup_enabled=0,followup_disable_reason='source_hidden',
                followup_version=followup_version+1,followup_dirty=0,followup_hold_until=NULL,hold_turn_id=NULL,
                hold_generation=hold_generation+1 WHERE owner=? AND id=?''', (owner, row['id']))
            db.execute("UPDATE crm_arrangement_notifications SET status='obsolete',token=NULL,lease_until=NULL WHERE owner=? AND plan_id=? AND status!='obsolete'", (owner, row['id']))
        return len(rows)

    def list(self, owner, *, view='all', state='pending', customer_id=None, contact_id=None,
             opportunity_id=None, matter_id=None, blocker=None, limit=20, offset=0, now=None):
        """Filter the complete owner-visible set in SQL before pagination."""
        owner = _owner(owner)
        if view not in ('all', 'today', 'week', 'month') or state not in (*STATES, 'all'):
            raise ValueError('队列视角或推进状态无效。')
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 100000000:
            raise ValueError('队列分页参数无效。')
        if blocker is not None:
            _string(blocker, '信息卡点', 100, empty=False)
        now = _timestamp(self.clock() if now is None else now)
        local = datetime.fromtimestamp(now, SHANGHAI)
        today, tomorrow = local.date(), local.date() + timedelta(days=1)
        first_week = today - timedelta(days=today.weekday())
        first_month = today.replace(day=1)
        last_month = first_month.replace(day=calendar.monthrange(today.year, today.month)[1])
        clauses = ['p.owner=?', 'r.hidden=0']
        args = [owner]
        safe_json = "CASE WHEN json_valid(p.data_json) THEN p.data_json ELSE '{}' END"
        with self.crm._lock:
            db = self.crm._db
            if self._exists(db, 'crm_secretary_trash'):
                clauses.append('NOT EXISTS(SELECT 1 FROM crm_secretary_trash x WHERE x.owner=p.owner AND x.record_id=p.record_id)')
            if state != 'all':
                clauses.append('p.settling_state=?')
                args.append(state)
            for key, identifier, table in (('customer_id', customer_id, 'crm_customers'), ('contact_id', contact_id, 'crm_contacts'), ('opportunity_id', opportunity_id, 'crm_opportunities')):
                if identifier is not None:
                    identifier = _identifier(identifier)
                    if not db.execute('SELECT 1 FROM ' + table + ' WHERE owner=? AND id=?', (owner, identifier)).fetchone():
                        raise KeyError('未找到你指定的客户、联系人或项目。')
                    clauses.append("json_extract(" + safe_json + ",'$." + key + "')=?")
                    args.append(identifier)
            if matter_id is not None:
                matter_id = _identifier(matter_id)
                if not self._exists(db, 'crm_matters') or not db.execute("SELECT 1 FROM crm_matters WHERE owner=? AND id=? AND visibility='active'", (owner, matter_id)).fetchone():
                    raise KeyError('未找到你的可见事项。')
                clauses.append("EXISTS(SELECT 1 FROM crm_matter_links ml WHERE ml.owner=p.owner AND ml.matter_id=? AND ((ml.entity_type='plan' AND ml.entity_id=p.id) OR (ml.entity_type='record' AND ml.entity_id=p.record_id)))")
                args.append(matter_id)
            if blocker:
                def has_blocker(plan_owner, identifier, code):
                    current = db.execute('SELECT * FROM crm_secretary_plans WHERE owner=? AND id=?', (plan_owner, identifier)).fetchone()
                    return int(bool(current and code in self.projection(db, current, now=now)['blocking_reasons']))
                db.create_function('arrangement_has_blocker', 3, has_blocker)
                clauses.append('arrangement_has_blocker(p.owner,p.id,?)=1')
                args.append(blocker)
            base = ' FROM crm_secretary_plans p JOIN crm_records r ON r.owner=p.owner AND r.id=p.record_id WHERE ' + ' AND '.join(clauses)
            deadline_day = "json_extract(" + safe_json + ",'$.settle_deadline.time_spec.date')"
            check_planned = "json_extract(" + safe_json + ",'$.next_check.status')='planned'"
            unsettled = "p.settling_state IN ('pending','paused')"
            due_today = '(' + unsettled + ' AND ((' + check_planned + ' AND p.next_check_at<?) OR p.deadline_end_at<=? OR ' + deadline_day + '=?))'
            daily_args = [datetime.combine(tomorrow, datetime.min.time(), SHANGHAI).timestamp(), now, today.isoformat()]
            week_sql = deadline_day + '>=? AND ' + deadline_day + '<=?'
            week_args = [first_week.isoformat(), (first_week + timedelta(days=6)).isoformat()]
            month_args = [first_month.isoformat(), last_month.isoformat()]
            count = lambda extra='', values=(): db.execute('SELECT COUNT(*)' + base + (' AND (' + extra + ')' if extra else ''), (*args, *values)).fetchone()[0]
            counts = {'total': count(), 'today': count(due_today, daily_args), 'week': count(week_sql, week_args),
                      'month': count(week_sql, month_args), 'overdue': count(unsettled + ' AND p.deadline_end_at<=?', [now]),
                      'undated': count('p.deadline_end_at IS NULL')}
            for selected_state in STATES:
                counts[selected_state] = count('p.settling_state=?', [selected_state])
            extra, values = ('', []) if view == 'all' else (due_today, daily_args) if view == 'today' else (week_sql, week_args if view == 'week' else month_args)
            total = count(extra, values)
            if offset and offset >= total:
                offset = ((total - 1) // limit) * limit if total else 0
            sort = ' CASE WHEN p.deadline_end_at<=? THEN 0 WHEN ' + check_planned + ' AND p.next_check_at<? THEN 1 WHEN ' + deadline_day + '<=? THEN 2 ELSE 3 END,COALESCE(p.next_check_at,p.deadline_end_at,999999999999),p.id'
            rows = db.execute('SELECT p.*' + base + (' AND (' + extra + ')' if extra else '') + ' ORDER BY' + sort + ' LIMIT ? OFFSET ?',
                (*args, *values, now, daily_args[0], (today + timedelta(days=2)).isoformat(), limit, offset)).fetchall()
            return {'items': [self.projection(db, row, now=now) for row in rows], 'total': total, 'counts': counts,
                    'limit': limit, 'offset': offset, 'view': view, 'state': state}

    def count(self, owner, **filters):
        return self.list(owner, limit=1, offset=0, **filters)['counts']
