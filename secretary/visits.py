"""An owner-scoped customer exchange and its recording, recap and supplements.

Source evidence stays in MaterialService. This layer groups evidence-backed
actions and stores one adoption per exchange; it never confirms a reminder or
rewrites an existing adoption. Revisions bind the complete current source state.
"""
from __future__ import annotations

import hashlib
import json
import re

from .crm import _identifier, _owner, _text
from .materials import CATEGORIES, _CLOSED
from .store import _timestamp
from .action_contract import TERM_FIELDS, can_schedule, derive_terms


ROLES = ('recording', 'recap', 'supplement')
ROLE_LABELS = {'recording': '现场录音原话', 'recap': '我的复盘/观察', 'supplement': '后续补充'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _identity(title):
    """Only unambiguous spelling variants merge without a person's decision."""
    value = re.sub(r'[\s，,。.!！?？:：;；“”"‘’]', '', title).lower()
    value = re.sub(r'^(?:我(?:们)?(?:答应|承诺|会|将|要)|请|把)', '', value)
    value = re.sub(r'^发(?:出|送)?(?=.{2,}$)', '发送', value)
    return value


def _key(identity):
    return 'a' + _hash(identity)[:24]


class VisitService:
    def __init__(self, crm, materials, lock):
        self.crm, self.materials, self.lock = crm, materials, lock
        self.clock = materials.clock
        materials.visit_service = self
        crm.visit_service = self
        with crm._lock:
            crm._db.executescript('''
                CREATE TABLE IF NOT EXISTS crm_visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,title TEXT NOT NULL,
                    customer_id INTEGER,occurred_at REAL,revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(owner,id),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id)
                );
                CREATE INDEX IF NOT EXISTS crm_visits_owner ON crm_visits(owner,updated_at,id);
                CREATE TABLE IF NOT EXISTS crm_visit_sources (
                    owner TEXT NOT NULL,visit_id INTEGER NOT NULL,material_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('recording','recap','supplement')),created_at REAL NOT NULL,
                    PRIMARY KEY(owner,material_id),
                    FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id),
                    FOREIGN KEY(owner,material_id) REFERENCES crm_materials(owner,id)
                );
                CREATE INDEX IF NOT EXISTS crm_visit_sources_visit ON crm_visit_sources(owner,visit_id,created_at);
                CREATE TABLE IF NOT EXISTS crm_visit_messages (
                    owner TEXT NOT NULL,source_id TEXT NOT NULL,visit_id INTEGER NOT NULL,
                    material_id INTEGER,payload_hash TEXT,PRIMARY KEY(owner,source_id),
                    FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_visit_adoptions (
                    owner TEXT NOT NULL,visit_id INTEGER NOT NULL,action_key TEXT NOT NULL,
                    record_id INTEGER NOT NULL,proposal_id INTEGER,snapshot_json TEXT NOT NULL,
                    created_at REAL NOT NULL,PRIMARY KEY(owner,visit_id,action_key),
                    FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id),
                    FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_visit_action_groups (
                    owner TEXT NOT NULL,visit_id INTEGER NOT NULL,identity TEXT NOT NULL,
                    group_key TEXT NOT NULL,references_json TEXT NOT NULL,created_at REAL NOT NULL,
                    PRIMARY KEY(owner,visit_id,identity),
                    FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_visit_source_choices (
                    owner TEXT NOT NULL,visit_id INTEGER NOT NULL,material_id INTEGER NOT NULL,
                    use_status TEXT NOT NULL CHECK(use_status IN ('included','excluded','deferred')),
                    reason TEXT NOT NULL DEFAULT '',association_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL,
                    PRIMARY KEY(owner,visit_id,material_id),
                    FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id),
                    FOREIGN KEY(owner,material_id) REFERENCES crm_materials(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_visit_source_choice_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,visit_id INTEGER NOT NULL,
                    material_id INTEGER NOT NULL,use_status TEXT NOT NULL,reason TEXT NOT NULL,
                    expected_revision TEXT NOT NULL,source_state_json TEXT NOT NULL,created_at REAL NOT NULL
                );
            ''')
        with crm._transaction() as db:
            columns = {item['name'] for item in db.execute('PRAGMA table_info(crm_visit_messages)')}
            if 'payload_hash' not in columns:
                db.execute('ALTER TABLE crm_visit_messages ADD COLUMN payload_hash TEXT')

    def _require(self, db, owner, identifier):
        row = db.execute('SELECT * FROM crm_visits WHERE owner=? AND id=?', (owner, identifier)).fetchone()
        if row is None:
            raise KeyError('未找到你的交流')
        return row

    @staticmethod
    def _values(data, partial=False):
        allowed = {'title', 'customer_id', 'occurred_at'} | ({'revision'} if partial else set())
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError('交流字段无效')
        values = dict(data) if partial else {'customer_id': None, 'occurred_at': None, **data}
        if partial:
            _text(values.get('revision'), '交流版本', 64, required=True)
        if 'title' in values or not partial:
            values['title'] = _text(values.get('title'), '交流标题', 120, required=True).strip()
        if values.get('customer_id') is not None:
            _identifier(values['customer_id'])
        if values.get('occurred_at') is not None:
            values['occurred_at'] = _timestamp(values['occurred_at'])
        return values

    def _public(self, db, row):
        result = {field: row[field] for field in ('id', 'title', 'customer_id', 'occurred_at', 'created_at', 'updated_at')}
        customer = db.execute('SELECT name FROM crm_customers WHERE owner=? AND id=?',
                              (row['owner'], row['customer_id'])).fetchone()
        result['customer_name'] = customer['name'] if customer else None
        result['revision'] = self._fingerprint(db, row)
        result['source_count'] = db.execute('SELECT COUNT(*) FROM crm_visit_sources WHERE owner=? AND visit_id=?',
                                            (row['owner'], row['id'])).fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
            result['source_count'] += db.execute('SELECT COUNT(*) FROM crm_visit_records l JOIN crm_records r '
                                                'ON r.owner=l.owner AND r.id=l.record_id WHERE l.owner=? AND l.visit_id=? AND r.hidden=0',
                                                (row['owner'], row['id'])).fetchone()[0]
        return result

    def _fingerprint(self, db, row):
        sources = []
        for link in db.execute('SELECT material_id,role FROM crm_visit_sources WHERE owner=? AND visit_id=? ORDER BY material_id',
                               (row['owner'], row['id'])).fetchall():
            material = self.materials._require(db, row['owner'], link['material_id'])
            record = db.execute('SELECT customer_id FROM crm_records WHERE owner=? AND id=?',
                                (row['owner'], material['record_id'])).fetchone()
            sources.append([dict(link), {field: material[field] for field in (
                'id', 'title', 'category', 'status', 'revision', 'customer_id', 'customer_assignment',
                'occurred_at', 'current_version_id', 'analysis_json', 'duplicate_of', 'external_nid')},
                record['customer_id'] if record else None,
                [dict(relation) for relation in self._source_links(db, row['owner'], material['id'])]])
        groups = [dict(item) for item in db.execute('SELECT identity,group_key,references_json FROM crm_visit_action_groups '
            'WHERE owner=? AND visit_id=? ORDER BY identity', (row['owner'], row['id']))]
        drafts = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_customer_drafts'").fetchone():
            drafts = [dict(item) for item in db.execute('SELECT d.id,d.status,d.customer_id,d.updated_at FROM crm_customer_drafts d '
                'JOIN crm_material_drafts m ON m.owner=d.owner AND m.draft_id=d.id JOIN crm_visit_sources s '
                'ON s.owner=m.owner AND s.material_id=m.material_id WHERE s.owner=? AND s.visit_id=? ORDER BY d.id',
                (row['owner'], row['id']))]
        choices = [dict(item) for item in db.execute('SELECT material_id,use_status,reason,association_json,revision '
            'FROM crm_visit_source_choices WHERE owner=? AND visit_id=? ORDER BY material_id', (row['owner'], row['id']))]
        records = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
            for link in db.execute('SELECT * FROM crm_visit_records WHERE owner=? AND visit_id=? ORDER BY record_id',
                                   (row['owner'], row['id'])):
                record = self.crm.get_record(row['owner'], link['record_id'])
                if record is None:
                    continue
                analysis = db.execute('SELECT data_json,input_fingerprint,version FROM crm_analyses WHERE owner=? AND record_id=?',
                                      (row['owner'], link['record_id'])).fetchone()
                records.append([dict(link), {field: record[field] for field in (
                    'id', 'title', 'content', 'original_content', 'customer_id', 'category', 'kind', 'status')},
                    dict(analysis) if analysis else None])
        return _hash([dict(row), sources, groups, drafts, choices, records, self._project_graph(db, row)])

    def _project_reader(self):
        # Read existing workspace tables without running its constructor or
        # starting a nested migration/transaction during adoption.
        from .sales_workspace import SalesWorkspace
        reader = object.__new__(SalesWorkspace)
        reader.crm, reader.clock = self.crm, self.clock
        return reader

    def _source_project(self, db, owner, kind, identifier):
        result = {'type': kind, 'id': identifier, 'opportunity_id': None,
                  'opportunity_name': None, 'link_revision': None, 'valid': True, 'invalid': False}
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_opportunity_links'").fetchone():
            return result
        reader = self._project_reader()
        raw = reader._entity(db, owner, kind, identifier)
        link = db.execute('SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?',
                          (owner, kind, identifier)).fetchone()
        if link is None:
            return result
        result['link_revision'] = link['revision']
        result['source_snapshot'] = link['source_snapshot']
        result['opportunity_id'] = link['opportunity_id']
        project = db.execute('SELECT id,name,archived,revision FROM crm_opportunities WHERE owner=? AND customer_id=? AND id=?',
                             (owner, raw['customer_id'], link['opportunity_id'])).fetchone() if link['opportunity_id'] else None
        result['opportunity_name'] = project['name'] if project else None
        result['archived'] = bool(project['archived']) if project else False
        result['valid'] = (link['customer_id'] == raw['customer_id'] and
                           link['source_snapshot'] == reader._link_snapshot(db, owner, kind, raw) and
                           (link['opportunity_id'] is None or (project is not None and not project['archived'])))
        result['invalid'] = not result['valid']
        return result

    def _project_graph(self, db, visit):
        refs = [('visit', visit['id'])]
        refs.extend(('material', row['material_id']) for row in db.execute(
            'SELECT material_id FROM crm_visit_sources WHERE owner=? AND visit_id=? ORDER BY material_id',
            (visit['owner'], visit['id'])))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
            refs.extend(('record', row['record_id']) for row in db.execute(
                'SELECT l.record_id FROM crm_visit_records l JOIN crm_records r ON r.owner=l.owner AND r.id=l.record_id '
                'WHERE l.owner=? AND l.visit_id=? AND r.hidden=0 ORDER BY l.record_id',
                (visit['owner'], visit['id'])))
        return [self._source_project(db, visit['owner'], kind, identifier) for kind, identifier in refs]

    def _action_project_scope(self, db, owner, visit, action, *, opportunity_id=None, confirm_single_action=False):
        if type(confirm_single_action) is not bool:
            raise ValueError('请明确这是否是一项跨项目行动')
        refs = sorted({('material', ref['material_id']) for ref in action.get('references', [])
                       if ref.get('material_id') and ref.get('action_id') is not None})
        states = [self._source_project(db, owner, kind, identifier) for kind, identifier in refs]
        direct = self._source_project(db, owner, 'visit', visit['id'])
        ids = {state['opportunity_id'] for state in states if state['valid'] and state['opportunity_id']}
        if direct['valid'] and direct['opportunity_id']:
            ids.add(direct['opportunity_id'])
        reasons = []
        invalid = any(state['invalid'] for state in [direct, *states])
        if invalid:
            reasons.append('来源项目关联已变化、归档或失效，请先纠正来源归属后再采用。')
        elif len(ids) > 1:
            reasons.append('来源涉及不同项目；若是两项独立事项请分组或纠正来源，不能自动采用为同一个项目。')
        options = [{'id': state['opportunity_id'], 'name': state['opportunity_name']}
                   for state in [direct, *states] if state['valid'] and state['opportunity_id']]
        options = list({option['id']: option for option in options}.values())
        revision = _hash([visit['id'], states, direct])
        chosen = next(iter(ids)) if len(ids) == 1 and not invalid else None
        explicit = opportunity_id is not None
        if explicit:
            opportunity_id = _identifier(opportunity_id)
            if opportunity_id not in ids or invalid:
                raise ValueError('只能选择当前有效来源明确关联的项目；请先核对来源，未采用事项。')
            if len(ids) > 1 and not confirm_single_action:
                raise ValueError('请先明确这是同一项跨项目行动并核对主项目；两个独立事项需分组。')
            chosen = opportunity_id
            reasons = []
        return {'opportunity_id': chosen, 'status': 'needs_review' if reasons else 'linked' if chosen else 'unassigned',
                'reasons': reasons, 'references': states, 'visit_project': direct, 'options': options,
                'revision': revision, 'explicit': explicit, 'confirm_single_action': confirm_single_action,
                'opportunity_name': next((option['name'] for option in options if option['id'] == chosen), None)}

    def material_action_scopes(self, owner, identifier):
        """The material button adopts its exchange group, not a silent split."""
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            links = self._source_links(self.crm._db, owner, identifier)
            if not links:
                material = self.materials.detail(owner, identifier)
                state = self._source_project(self.crm._db, owner, 'material', identifier)
                scope = {'opportunity_id': state['opportunity_id'] if state['valid'] else None,
                    'opportunity_name': state['opportunity_name'],
                    'status': 'needs_review' if state['invalid'] else 'linked' if state['opportunity_id'] else 'unassigned',
                    'reasons': ['材料的项目关联已变化或失效，请先核对来源'] if state['invalid'] else [],
                    'references': [state], 'visit_project': None,
                    'options': [{'id': state['opportunity_id'], 'name': state['opportunity_name']}] if state['valid'] and state['opportunity_id'] else [],
                    'revision': _hash(['standalone-material', identifier, state]), 'explicit': False, 'confirm_single_action': False}
                return {action['id']: {'project_scope': scope, 'project_scope_revision': scope['revision'],
                    'source_review_reasons': [], 'review_reasons': scope['reasons'],
                    'needs_review': state['invalid']}
                    for action in (material.get('analysis') or {}).get('actions', [])}
            if len({link['visit_id'] for link in links}) != 1:
                raise ValueError('同一录音归档到不同交流，请先核对来源')
            detail = self._snapshot(owner, links[0]['visit_id'])
            actions = {}
            for action in detail['actions']:
                for ref in action['references']:
                    if ref['material_id'] == identifier and ref.get('action_id') is not None:
                        actions[ref['action_id']] = {'visit_id': detail['visit']['id'],
                            'visit_revision': detail['visit']['revision'], 'visit_action_key': action['key'],
                            'project_scope': action['project_scope'],
                            'project_scope_revision': action['project_scope_revision'],
                            'source_review_reasons': action['source_review_reasons'],
                            'review_reasons': action['review_reasons'], 'needs_review': action['needs_review'],
                            'references': action['references']}
            return actions

    def _save_action_project(self, db, owner, record_id, scope):
        if not scope['opportunity_id']:
            return
        old = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                         (owner, record_id)).fetchone()
        if old is not None:
            return  # A prior user's explicit assignment (including null) wins.
        raw = self.crm._require_record(db, owner, record_id)
        snapshot = self._project_reader()._entity_snapshot(raw)
        now = self.clock()
        db.execute("INSERT INTO crm_opportunity_links VALUES (?,'record',?,?,?,?,?,?)",
                   (owner, record_id, raw['customer_id'], scope['opportunity_id'], 1, snapshot, now))
        db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) VALUES (?,'record',?,?,?,?,?,?)",
                   (owner, record_id, raw['customer_id'], scope['opportunity_id'], 1, snapshot, now))

    def create(self, owner, data, *, source_id=None):
        owner, values = _owner(owner), self._values(data)
        payload_hash = _hash(['visit-create-v1', values])
        if source_id is not None:
            source_id = _text(source_id, '消息编号', 512, required=True)
        with self.crm._transaction() as db:
            if source_id is not None:
                prior = db.execute('SELECT * FROM crm_visit_messages WHERE owner=? AND source_id=?', (owner, source_id)).fetchone()
                if prior:
                    self._check_replay(prior, payload_hash)
                    return self._public(db, self._require(db, owner, prior['visit_id']))
            self.crm._require_customer(db, owner, values['customer_id'])
            now = self.clock()
            identifier = db.execute('INSERT INTO crm_visits(owner,title,customer_id,occurred_at,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?)', (owner, values['title'], values['customer_id'], values['occurred_at'], now, now)).lastrowid
            if source_id is not None:
                db.execute('INSERT INTO crm_visit_messages(owner,source_id,visit_id,material_id,payload_hash) VALUES (?,?,?,NULL,?)',
                           (owner, source_id, identifier, payload_hash))
            return self._public(db, self._require(db, owner, identifier))

    def list(self, owner, q='', customer_id=None, limit=50, offset=0):
        owner, q = _owner(owner), _text(q, '关键词', 200).strip()
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 200_000_000:
            raise ValueError('交流分页无效')
        if customer_id is not None:
            _identifier(customer_id)
        where, args = 'v.owner=?', [owner]
        if customer_id is not None:
            where += ' AND v.customer_id=?'; args.append(customer_id)
        if q:
            where += (' AND (instr(v.title,?)>0 OR EXISTS (SELECT 1 FROM crm_customers c WHERE c.owner=v.owner AND c.id=v.customer_id '
                'AND instr(c.name,?)>0) OR EXISTS (SELECT 1 FROM crm_visit_sources s JOIN crm_materials m ON m.owner=s.owner '
                'AND m.id=s.material_id LEFT JOIN crm_material_versions t ON t.owner=m.owner AND t.id=m.current_version_id '
                'WHERE s.owner=v.owner AND s.visit_id=v.id AND (instr(m.title,?)>0 OR instr(t.text,?)>0)))')
            args.extend([q] * 4)
        with self.crm._lock:
            db = self.crm._db
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone()
                    and db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_record_lifecycle'").fetchone()):
                where += " AND NOT EXISTS (SELECT 1 FROM crm_visit_records l JOIN crm_record_lifecycle x " \
                         "ON x.owner=l.owner AND x.record_id=l.record_id JOIN crm_records r ON r.owner=l.owner AND r.id=l.record_id " \
                         "WHERE l.owner=v.owner AND l.visit_id=v.id AND r.hidden=1 AND x.visibility IN ('archived','trash'))"
            total = db.execute('SELECT COUNT(*) FROM crm_visits v WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT v.* FROM crm_visits v WHERE ' + where + ' ORDER BY v.updated_at DESC,v.id DESC LIMIT ? OFFSET ?',
                              [*args, limit, offset]).fetchall()
            return {'items': [self._public(db, row) for row in rows], 'total': total}

    @staticmethod
    def _mismatch(visit, material, association=None):
        if material['customer_id'] is not None and material['customer_id'] != visit['customer_id']:
            raise ValueError('材料客户与交流客户不一致，请先核对客户归属')
        time_confirmed = bool(association and association == [visit['occurred_at'], material['occurred_at']])
        if material['occurred_at'] is not None and material['occurred_at'] != visit['occurred_at'] and not time_confirmed:
            raise ValueError('材料发生时间与交流日期不一致，请先核对发生时间')

    def _inherit(self, db, owner, row, visit, role, *, preserve_time=False):
        values = {}
        for field in ('customer_id', 'occurred_at'):
            if field == 'occurred_at' and preserve_time:
                continue
            if row[field] != visit[field]:
                values[field] = visit[field]
        expected_category = 'conversation' if role == 'recording' else 'visit_review' if role == 'recap' else None
        if expected_category and row['category'] != expected_category:
            values['category'] = expected_category
        if not values:
            return row
        if 'customer_id' in values:
            values['customer_assignment'] = 'explicit' if values['customer_id'] is not None else 'cleared'
        self.materials._stale_reviews(db, owner, row['id'])
        values.update(status='queued', revision=row['revision'] + 1, error='', analysis_json=None, updated_at=self.clock())
        db.execute('UPDATE crm_materials SET ' + ','.join(field + '=?' for field in values) + ' WHERE owner=? AND id=?',
                   [*values.values(), owner, row['id']])
        db.execute("UPDATE crm_material_jobs SET status='superseded',lease_token=NULL WHERE owner=? AND material_id=? "
                   "AND status IN ('queued','reading','organizing')", (owner, row['id']))
        updated = self.materials._require(db, owner, row['id'])
        self.materials._enqueue_job(db, updated)
        return updated

    def update(self, owner, identifier, data):
        owner, identifier, values = _owner(owner), _identifier(identifier), self._values(data, partial=True)
        # Material detail can resume customer drafts, so run it before beginning
        # the transaction that checks the complete revision and changes metadata.
        self.detail(owner, identifier)
        with self.crm._transaction() as db:
            row = self._require(db, owner, identifier)
            if self._fingerprint(db, row) != values.pop('revision'):
                raise ValueError('交流或材料已有变化，请刷新后核对')
            # Checking unchanged metadata is not a new exchange revision. A
            # spurious revision would invalidate its explicit project link and
            # force the user to repeat source and action decisions.
            values = {field: value for field, value in values.items() if value != row[field]}
            if not values:
                return self._public(db, row)
            prospective = {**dict(row), **values}
            self.crm._require_customer(db, owner, prospective['customer_id'])
            links = db.execute('SELECT material_id,role FROM crm_visit_sources WHERE owner=? AND visit_id=?', (owner, identifier)).fetchall()
            for link in links:
                material = self.materials._require(db, owner, link['material_id'])
                choice = self._source_choice(db, owner, identifier, link['material_id'])
                time_confirmed = choice['association'] == [row['occurred_at'], material['occurred_at']]
                if any(other['visit_id'] != identifier for other in self._source_links(db, owner, material['id'])):
                    raise ValueError('来源材料已归档到另一场交流，请先核对归档关系')
                for field in ('customer_id', 'occurred_at'):
                    if material[field] == row[field]:
                        continue
                    if field == 'occurred_at' and time_confirmed:
                        continue
                    # C confirmation can identify a previously unknown customer.
                    # Selecting that exact confirmed customer is an explicit
                    # reconciliation, never an automatic reassignment.
                    confirmed_customer = (field == 'customer_id' and row[field] is None and
                        material['customer_assignment'] == 'confirmed' and material[field] == prospective[field])
                    if not confirmed_customer:
                        raise ValueError('来源材料的客户或发生时间已独立变化，请先核对材料归属与日期')
            for link in links:
                material = self.materials._require(db, owner, link['material_id'])
                choice = self._source_choice(db, owner, identifier, link['material_id'])
                preserve_time = choice['association'] == [row['occurred_at'], material['occurred_at']] and material['occurred_at'] != row['occurred_at']
                self._inherit(db, owner, material, prospective, link['role'], preserve_time=preserve_time)
            values.update(revision=row['revision'] + 1, updated_at=self.clock())
            db.execute('UPDATE crm_visits SET ' + ','.join(field + '=?' for field in values) + ' WHERE owner=? AND id=?',
                       [*values.values(), owner, identifier])
            return self._public(db, self._require(db, owner, identifier))

    def _source_links(self, db, owner, material_id):
        material = self.materials._require(db, owner, material_id)
        return db.execute('SELECT s.* FROM crm_visit_sources s JOIN crm_materials m ON m.owner=s.owner AND m.id=s.material_id '
            'WHERE s.owner=? AND (s.material_id=? OR m.duplicate_of=?)', (owner, material['id'], material['id'])).fetchall()

    def source_for_material(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            links = self._source_links(self.crm._db, owner, identifier)
            if not links:
                return None
            row = self._require(self.crm._db, owner, links[0]['visit_id'])
            return {'id': row['id'], 'title': row['title'], 'revision': self._fingerprint(self.crm._db, row)}

    @staticmethod
    def _check_replay(prior, payload_hash):
        if prior['payload_hash'] is None:
            raise ValueError('这个旧请求编号尚未绑定内容，请保留草稿并刷新核对已保存结果，再使用新的请求编号提交')
        if prior['payload_hash'] != payload_hash:
            raise ValueError('这个请求编号已用于不同内容，请保留草稿并刷新核对后重新提交')

    def _source_payload(self, data):
        if 'material_id' in data:
            payload = {'role': data['role'], 'material_id': _identifier(data['material_id'])}
            if data.get('confirm_time_difference'):
                payload['confirm_time_difference'] = True
            return payload
        role = data['role']
        category = 'conversation' if role == 'recording' else 'visit_review' if role == 'recap' else data.get('category', 'auto')
        values = {field: data[field] for field in ('provider', 'title', 'text', 'customer_id', 'occurred_at') if field in data}
        values['category'] = category
        # Bind client input, not later-inherited exchange metadata. A retry of
        # the original request remains valid after the owner fills its date.
        payload = {'role': role, 'material': self.materials._validate(values),
                   'explicit_scope_fields': sorted(set(data) & {'customer_id', 'occurred_at'})}
        if data.get('confirm_time_difference'):
            payload['confirm_time_difference'] = True
        return payload

    def add_material(self, owner, identifier, data, *, source_id=None):
        owner, identifier = _owner(owner), _identifier(identifier)
        allowed = {'role', 'material_id', 'provider', 'title', 'text', 'category', 'customer_id', 'occurred_at', 'confirm_time_difference'}
        if not isinstance(data, dict) or set(data) - allowed or data.get('role') not in ROLES:
            raise ValueError('交流材料字段或来源角色无效')
        if source_id is not None:
            source_id = _text(source_id, '消息编号', 512, required=True)
        role = data['role']
        confirmed = data.get('confirm_time_difference', False)
        if type(confirmed) is not bool:
            raise ValueError('请明确确认录音时间与交流发生时间的差别')
        if 'material_id' in data and set(data) - {'role', 'material_id', 'confirm_time_difference'}:
            raise ValueError('关联已有材料时只能指定材料编号和来源角色')
        payload_hash = _hash(['visit-material-v1', identifier, self._source_payload(data)])
        with self.crm._lock:
            db, visit = self.crm._db, self._require(self.crm._db, owner, identifier)
            if source_id is not None:
                prior = db.execute('SELECT * FROM crm_visit_messages WHERE owner=? AND source_id=?', (owner, source_id)).fetchone()
                if prior:
                    self._check_replay(prior, payload_hash)
                    if prior['visit_id'] != identifier or prior['material_id'] is None:
                        raise ValueError('这条消息已经归档到其他交流，请核对')
                    return {'visit': self._public(db, visit), 'material': self.materials._public(
                        self.materials._require(db, owner, prior['material_id']))}
            if 'material_id' in data:
                material = self.materials._require(db, owner, _identifier(data['material_id']))
            else:
                for field in ('customer_id', 'occurred_at'):
                    if field in data and data[field] != visit[field] and not (field == 'occurred_at' and confirmed):
                        raise ValueError('材料客户或发生时间必须继承本次交流，请核对客户与日期')
                category = 'conversation' if role == 'recording' else 'visit_review' if role == 'recap' else data.get('category', 'auto')
                if category not in CATEGORIES:
                    raise ValueError('材料分类无效')
                values = {field: data[field] for field in ('provider', 'title', 'text') if field in data}
                values.update(category=category, customer_id=visit['customer_id'], occurred_at=visit['occurred_at'])
                if confirmed and 'occurred_at' in data:
                    values['occurred_at'] = data['occurred_at']
                options = {'namespace': f'visit:{identifier}:{role}'} if values.get('provider') == 'manual' else {}
                material_public = self.materials.enqueue(owner, values, **options)
                material = self.materials._require(db, owner, material_public['id'])
            links = self._source_links(db, owner, material['id'])
            if links:
                if any(link['visit_id'] != identifier for link in links):
                    raise ValueError('这份材料已归档到另一场交流，请先打开原交流核对')
                if any(link['role'] != role for link in links):
                    raise ValueError('这份材料已有不同来源角色，请核对后使用原角色')
            prior_choice = db.execute('SELECT association_json FROM crm_visit_source_choices '
                'WHERE owner=? AND visit_id=? AND material_id=?', (owner, identifier, material['id'])).fetchone()
            association = [visit['occurred_at'], material['occurred_at']] if confirmed else (
                json.loads(prior_choice['association_json']) if prior_choice and prior_choice['association_json'] else None)
            self._mismatch(visit, material, association)
            with self.crm._transaction() as transaction:
                visit = self._require(transaction, owner, identifier)
                if source_id is not None:
                    # Another connection may have committed this request while
                    # enqueue was acquiring its own transaction. Recheck before
                    # changing an association or inherited metadata.
                    prior = transaction.execute('SELECT * FROM crm_visit_messages WHERE owner=? AND source_id=?',
                                                (owner, source_id)).fetchone()
                    if prior:
                        self._check_replay(prior, payload_hash)
                        if prior['visit_id'] != identifier or prior['material_id'] is None:
                            raise ValueError('这条消息已经归档到其他交流，请核对')
                        return {'visit': self._public(transaction, visit), 'material': self.materials._public(
                            self.materials._require(transaction, owner, prior['material_id']))}
                material = self.materials._require(transaction, owner, material['id'])
                self._mismatch(visit, material, association)
                preserve_time = bool(association and material['occurred_at'] != visit['occurred_at'])
                material = self._inherit(transaction, owner, material, visit, role, preserve_time=preserve_time)
                previous = transaction.execute('SELECT visit_id,role FROM crm_visit_sources WHERE owner=? AND material_id=?',
                                               (owner, material['id'])).fetchone()
                if previous and (previous['visit_id'] != identifier or previous['role'] != role):
                    raise ValueError('材料已经归档到其他交流或来源角色，请核对')
                if not previous:
                    transaction.execute('INSERT INTO crm_visit_sources VALUES (?,?,?,?,?)', (owner, identifier, material['id'], role, self.clock()))
                    transaction.execute('UPDATE crm_visits SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (self.clock(), owner, identifier))
                stored_choice = self._source_choice(transaction, owner, identifier, material['id'])
                if confirmed and stored_choice['association'] != association:
                    transaction.execute('INSERT INTO crm_visit_source_choices(owner,visit_id,material_id,use_status,association_json,updated_at) '
                        "VALUES (?,?,?,'included',?,?) ON CONFLICT(owner,visit_id,material_id) DO UPDATE SET "
                        'association_json=excluded.association_json,revision=crm_visit_source_choices.revision+1,updated_at=excluded.updated_at',
                        (owner, identifier, material['id'], _json(association), self.clock()))
                    transaction.execute('INSERT INTO crm_visit_source_choice_history '
                        '(owner,visit_id,material_id,use_status,reason,expected_revision,source_state_json,created_at) '
                        "VALUES (?,?,?,'included',?,?,?,?)", (owner, identifier, material['id'], '确认来源时间差',
                        self._fingerprint(transaction, visit), self._source_state(material), self.clock()))
                if source_id is not None:
                    transaction.execute('INSERT INTO crm_visit_messages(owner,source_id,visit_id,material_id,payload_hash) VALUES (?,?,?,?,?)',
                                        (owner, source_id, identifier, material['id'], payload_hash))
                return {'visit': self._public(transaction, self._require(transaction, owner, identifier)),
                        'material': self.materials._public(material)}

    def _adoption_result(self, owner, record_id, proposal_id):
        with self.crm._lock:
            project = self._source_project(self.crm._db, owner, 'record', record_id)
        return {'record': self.crm.get_record(owner, record_id),
                'proposal': self.crm.get_proposal(owner, proposal_id) if proposal_id else None,
                'project_scope': project}

    @staticmethod
    def _source_choice(db, owner, identifier, material_id):
        saved = db.execute('SELECT * FROM crm_visit_source_choices WHERE owner=? AND visit_id=? AND material_id=?',
                           (owner, identifier, material_id)).fetchone()
        return {'use': saved['use_status'] if saved else 'included', 'reason': saved['reason'] if saved else '',
                'association': json.loads(saved['association_json']) if saved and saved['association_json'] else None}

    @staticmethod
    def _source_state(material):
        return _json({field: material[field] for field in ('id', 'revision', 'status', 'current_version_id', 'customer_id', 'occurred_at')})

    def decide_source(self, owner, identifier, material_id, revision, use, reason=''):
        """Record a reversible, explicit source-use decision, never delete text.

        Deferred sources still block new adoptions. Only explicit exclusion
        removes their unknown content from this exchange's candidate evidence.
        """
        owner, identifier, material_id = _owner(owner), _identifier(identifier), _identifier(material_id)
        _text(revision, '交流版本', 64, required=True)
        use = ('included' if use else 'excluded') if type(use) is bool else use
        if use not in ('included', 'excluded', 'deferred'):
            raise ValueError('请选择使用、暂不使用或稍后核对来源')
        reason = _text(reason, '来源使用说明', 500).strip()
        if use == 'excluded' and not reason:
            raise ValueError('暂不使用来源需要说明本次选择，原材料和未知变更将保留')
        with self.crm._lock:
            self._snapshot(owner, identifier)
            with self.crm._transaction() as db:
                visit = self._require(db, owner, identifier)
                link = db.execute('SELECT * FROM crm_visit_sources WHERE owner=? AND visit_id=? AND material_id=?',
                                  (owner, identifier, material_id)).fetchone()
                if link is None:
                    links = [item for item in self._source_links(db, owner, material_id) if item['visit_id'] == identifier]
                    if len(links) != 1:
                        raise KeyError('请指定本次交流中的原来源链接，暂不使用不能猜测同名录音')
                    link, material_id = links[0], links[0]['material_id']
                material = self.materials._require(db, owner, material_id)
                current = self._fingerprint(db, visit)
                if current != revision:
                    replay = db.execute('SELECT * FROM crm_visit_source_choice_history WHERE owner=? AND visit_id=? '
                        'AND material_id=? AND expected_revision=? ORDER BY id DESC LIMIT 1',
                        (owner, identifier, material_id, revision)).fetchone()
                    choice = self._source_choice(db, owner, identifier, material_id)
                    if not (replay and replay['use_status'] == use and replay['reason'] == reason
                            and replay['source_state_json'] == self._source_state(material)
                            and choice['use'] == use and choice['reason'] == reason):
                        raise ValueError('交流或来源已有变化，请刷新后核对来源使用决定')
                else:
                    db.execute('INSERT INTO crm_visit_source_choices(owner,visit_id,material_id,use_status,reason,updated_at) '
                        'VALUES (?,?,?,?,?,?) ON CONFLICT(owner,visit_id,material_id) DO UPDATE SET '
                        'use_status=excluded.use_status,reason=excluded.reason,revision=crm_visit_source_choices.revision+1,updated_at=excluded.updated_at',
                        (owner, identifier, material_id, use, reason, self.clock()))
                    db.execute('INSERT INTO crm_visit_source_choice_history '
                        '(owner,visit_id,material_id,use_status,reason,expected_revision,source_state_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                        (owner, identifier, material_id, use, reason, revision, self._source_state(material), self.clock()))
            return self._snapshot(owner, identifier)

    def _snapshot(self, owner, identifier):
        db, row = self.crm._db, self._require(self.crm._db, owner, identifier)
        links = db.execute('SELECT * FROM crm_visit_sources WHERE owner=? AND visit_id=? ORDER BY created_at,material_id',
                           (owner, identifier)).fetchall()
        sources, warnings, drafts, candidates, changes = [], [], {}, [], []
        mismatched = False
        record_sources, blocked_records = [], []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
            for link in db.execute('SELECT * FROM crm_visit_records WHERE owner=? AND visit_id=? ORDER BY created_at,record_id',
                                   (owner, identifier)).fetchall():
                record = self.crm.get_record(owner, link['record_id'])
                if record is None:
                    continue
                analysis = self.crm.get_analysis(owner, link['record_id'])
                reasons = []
                if record['customer_id'] != row['customer_id']:
                    reasons.append('关联记录的客户与交流不一致，请核对归属')
                    mismatched = True
                if analysis is None or analysis.get('stale'):
                    blocked_records.append(record['id'])
                    reasons.append('关联原记录尚未整理或原话已修改，请先核对最新原话')
                record_sources.append({'record_id': record['id'], 'record': record, 'analysis': analysis,
                    'role': link['role'], 'source_label': ROLE_LABELS[link['role']],
                    'needs_review': bool(reasons), 'review_reasons': reasons})
                warnings.extend(reasons)
                for match in re.finditer(r'[^。！？\n]+', record['content'] or record['original_content']):
                    if _CLOSED.search(match.group()):
                        changes.append({'record_id': record['id'], 'material_id': None, 'action_id': None,
                            'role': link['role'], 'quote': match.group().strip(), 'change': True})
        for link in links:
            source = self.materials.detail(owner, link['material_id'])
            material = source['material']
            choice = self._source_choice(db, owner, identifier, link['material_id'])
            entry = {**source, 'role': link['role'], 'source_label': ROLE_LABELS[link['role']],
                     'material_id': material['id'], 'linked_material_id': link['material_id'], 'text_material_id': material['id'],
                     'source_use': choice['use'], 'source_use_reason': choice['reason'],
                     'time_association_confirmed': choice['association'] == [row['occurred_at'], material['occurred_at']]}
            entry['source_use_history'] = [dict(item) for item in db.execute(
                'SELECT use_status,reason,created_at FROM crm_visit_source_choice_history '
                'WHERE owner=? AND visit_id=? AND material_id=? ORDER BY id', (owner, identifier, link['material_id']))]
            sources.append(entry)
            if choice['use'] == 'excluded':
                warnings.append('“' + material['title'] + '”已明确暂不使用；原材料保留，未读取内容可能含变更，重新纳入后须核对影响')
                continue
            warnings.extend(source['warnings'])
            try:
                self._mismatch(row, material, choice['association'])
                if material['customer_id'] != row['customer_id'] or (material['occurred_at'] != row['occurred_at'] and not entry['time_association_confirmed']):
                    raise ValueError('来源客户或发生时间未继承已确认的交流，请核对后重新整理')
                parent = self.crm.get_record(owner, material['record_id']) if material['record_id'] else None
                if parent and parent['customer_id'] != row['customer_id']:
                    raise ValueError('来源记录的客户归属已有变化，请核对材料和交流客户')
                if any(other['visit_id'] != identifier for other in self._source_links(db, owner, material['id'])):
                    raise ValueError('聆记同一录音已关联另一场交流，请核对归档，暂不能采纳')
            except ValueError as exc:
                warnings.append(str(exc)); mismatched = True
            for draft in source.get('customer_drafts', []):
                drafts.setdefault(draft['id'], {**draft, 'references': []})['references'].append(
                    {'material_id': material['id'], 'role': link['role'], 'source_label': ROLE_LABELS[link['role']]})
            analysis = source.get('analysis') or {}
            for action in analysis.get('actions', []):
                quote = action.get('evidence')
                saved = db.execute('SELECT * FROM crm_material_actions WHERE owner=? AND material_id=? AND id=?',
                                   (owner, material['id'], action.get('id'))).fetchone()
                current_version = self.materials._require(db, owner, material['id'])['current_version_id']
                saved_data = json.loads(saved['data_json']) if saved else {}
                source_current = bool(saved and saved['version_id'] == current_version and all(
                    saved_data.get(field) == action.get(field) for field in
                    ('title', 'kind', 'reason', 'evidence', 'owner_hint', 'remind_at', *TERM_FIELDS)))
                verified = source_current and isinstance(quote, str) and bool(quote) and quote in source['text']
                native_suggestion = source_current and saved_data.get('kind') == 'suggestion'
                own_intent = (r'我(?:们)?(?:答应|承诺|会|将|要|得|'
                    r'(?:今天|明天|后天|下周|本周|这周|周[一二三四五六日天])(?:上午|下午|晚上)?'
                    r'(?:去|发|提交|联系|拜访|补充|提供|整理|完成|安排))')
                own_promise = any(match and action['title'] in clause[match.start():]
                    for clause in re.split(r'[。！？;；\n]', quote or '')
                    for match in [re.search(own_intent, clause)])
                observation = ((link['role'] == 'recap' or material['category'] == 'visit_review') and not own_promise
                    or bool(re.search(r'我(?:感觉|认为|猜|观察)|估计|可能|猜测', quote or '')))
                candidate = {**action, '_identity': _identity(action['title']), '_source': entry,
                             '_verified': verified, '_observation': bool(observation),
                             '_source_current': source_current, '_native_suggestion': native_suggestion,
                             '_supported': verified or (native_suggestion and not quote),
                             '_original_kind': action.get('kind', 'suggestion'),
                             '_saved': dict(saved) if saved else None}
                if observation and not native_suggestion:
                    candidate['kind'] = 'suggestion'
                    candidate['remind_at'] = None
                candidate.update(derive_terms(candidate, source['text']))
                if observation and not native_suggestion:
                    candidate['execution_at'] = None
                if candidate['executor_kind'] in ('customer', 'team'):
                    candidate['_identity'] += '|executor:' + candidate['executor_kind'] + ':' + str(candidate.get('owner_hint', '待确认'))
                candidates.append(candidate)
            for match in re.finditer(r'[^。！？\n]+', source['text']):
                if _CLOSED.search(match.group()):
                    changes.append({'material_id': material['id'], 'action_id': None, 'role': link['role'],
                                    'quote': match.group().strip(), 'change': True})
        manual = {item['identity']: item['group_key'] for item in db.execute(
            'SELECT identity,group_key FROM crm_visit_action_groups WHERE owner=? AND visit_id=?', (owner, identifier))}
        blocked_sources = [source['linked_material_id'] for source in sources if source['source_use'] != 'excluded'
                           and (source['material']['status'] != 'review' or source['source_use'] == 'deferred')]
        incomplete_sources = bool(blocked_sources or blocked_records)
        if incomplete_sources:
            warnings.append('本次交流仍有素材尚未整理完成，请核对完整来源后再采纳行动')
        grouped = {}
        for candidate in candidates:
            grouped.setdefault(manual.get(candidate['_identity'], _key(candidate['_identity'])), []).append(candidate)
        adoptions = {item['action_key']: dict(item) for item in db.execute('SELECT * FROM crm_visit_adoptions WHERE owner=? AND visit_id=?',
                                                                        (owner, identifier))}
        adopted_history = []
        for saved in adoptions.values():
            old = json.loads(saved['snapshot_json'])
            adopted_history.append((saved['action_key'], old.get('title', '')))
        for source in sources:
            for saved in db.execute('SELECT data_json FROM crm_material_actions WHERE owner=? AND material_id=? AND record_id IS NOT NULL',
                                    (owner, source['material_id'])):
                old_title = json.loads(saved['data_json']).get('title', '')
                old_identity = _identity(old_title)
                adopted_history.append((manual.get(old_identity, _key(old_identity)), old_title))
        actions, current_records = [], set()
        for action_key, group in grouped.items():
            group = sorted(group, key=lambda candidate: (candidate['_source']['role'] == 'recap', candidate['_source']['material_id'], candidate['id']))
            references, reasons = [], []
            commitments = [item for item in group if item.get('kind') == 'commitment' and item['_verified'] and not item['_observation']]
            times = {item['remind_at'] for item in commitments if item.get('remind_at') is not None}
            owners = {item.get('owner_hint') for item in commitments if item.get('owner_hint') not in (None, '', '待确认')}
            executors = {item['executor_kind'] for item in commitments if item['executor_kind'] != 'unknown'}
            durations = {item['duration_minutes'] for item in group if item.get('duration_minutes') is not None}
            if len(times) > 1:
                reasons.append('多个来源给出的时间不一致，请核对双方原话')
            if len(owners) > 1:
                reasons.append('多个来源给出的责任人不一致，请核对双方原话')
            if len(executors) > 1:
                reasons.append('多个来源的执行主体不一致，请核对我的行动与对方承诺')
            matching_subjects = {(item['executor_kind'], item.get('owner_hint')) for item in candidates
                if _identity(item['title']) in {_identity(member['title']) for member in group}
                and item['executor_kind'] != 'unknown'}
            if len(matching_subjects) > 1:
                reasons.append('同名行动有不同执行主体，请明确是两件行动还是责任人变化')
            if len(durations) > 1:
                reasons.append('多个来源给出的预计时长不一致，请核对对应动作')
            for field in ('deadline_at', 'check_at', 'deadline_date', 'check_date'):
                if len({item[field] for item in group if item.get(field) is not None}) > 1:
                    reasons.append('多个来源的截止或检查时间不一致，请核对对应动作')
            if any(not item['_supported'] for item in group):
                reasons.append('部分行动没有可核验的完整转写引用，请核对来源')
            if incomplete_sources:
                reasons.append('部分来源尚未整理完成，请核对最新版本')
            if any(item['_observation'] and not item['_native_suggestion'] for item in group):
                reasons.append('我的观察不等于客户现场承诺，请核对')
            if mismatched:
                reasons.append('来源的客户、时间或归档关系不一致，请核对')
            # A renamed delivery after adoption may refer to the original task.
            # Retain both descriptions for review rather than create a second
            # reminder just because the extractor selected a different verb.
            delivery_objects = {re.sub(r'^(?:发送|提交|提供)', '', item['_identity']) for item in group
                                if re.match(r'^(?:发送|提交|提供)', item['_identity'])}
            if any(old_key != action_key and re.match(r'^(?:发送|提交|提供)', _identity(old_title)) and
                   re.sub(r'^(?:发送|提交|提供)', '', _identity(old_title)) in delivery_objects
                   for old_key, old_title in adopted_history):
                reasons.append('交流已有相似的已采纳事项，请先打开原待办核对，避免重复安排')
            for item in group:
                reference = {'material_id': item['_source']['material_id'], 'action_id': item['id'],
                    'role': item['_source']['role'], 'quote': item.get('evidence', ''),
                    'owner_hint': item.get('owner_hint', '待确认'), 'remind_at': item.get('remind_at'),
                    'verified': item['_verified'], 'source_current': item['_source_current'],
                    'original_kind': item['_original_kind'], 'reason': item.get('reason', '') if item['_native_suggestion'] else '',
                    'basis': 'suggestion' if item['_native_suggestion'] else 'observation' if item['_observation'] else 'source'}
                reference.update({field: item.get(field) for field in TERM_FIELDS})
                if reference not in references:
                    references.append(reference)
            cores = {re.sub(r'^(?:发送|提交|补充|提供|整理|完成|联系|确认|跟进|准备|安排|交付)', '', item['_identity']) for item in group}
            for change in changes:
                change_identity = _identity(change['quote'])
                same_object = any(len(core) >= 2 and core in change_identity for core in cores)
                possible_object = any(noun in change_identity and any(noun in core for core in cores)
                    for noun in ('方案', '资料', '材料', '报价', '合同', '案例', '报告', '邮件'))
                if same_object or possible_object:
                    references.append(change)
                    reasons.append('来源中存在取消、完成或改期信息，请核对是否仍需执行')
            needs_review = bool(reasons)
            when = next(iter(times)) if len(times) == 1 and not needs_review else None
            action = {'key': action_key, 'title': group[0]['title'],
                'kind': 'commitment' if commitments else 'suggestion', 'remind_at': when,
                'reason': '\n'.join(dict.fromkeys(item.get('reason', '') for item in group if item.get('reason'))),
                'owner_hint': next(iter(owners)) if len(owners) == 1 else '待确认',
                'needs_review': needs_review, 'review_reasons': list(dict.fromkeys(reasons)),
                'references': references, 'evidence': '\n'.join(ROLE_LABELS[ref['role']] + '：' + str(ref['quote']) for ref in references),
                'identities': sorted({item['_identity'] for item in group}),
                'adopted_record_id': None, 'proposal_id': None, 'record': None, 'proposal': None}
            action.update({field: group[0].get(field) for field in TERM_FIELDS})
            for field in TERM_FIELDS:
                values = [item.get(field) for item in group if item.get(field) not in (None, '', 'unknown')]
                if values:
                    action[field] = values[0]
            action['execution_at'] = when if not needs_review else None
            action['duration_minutes'] = next(iter(durations)) if len(durations) == 1 else None
            existing = adoptions.get(action_key)
            saved_records = {item['_saved']['record_id'] for item in group if item['_saved'] and item['_saved']['record_id']}
            if existing:
                saved_records.add(existing['record_id'])
            if len(saved_records) > 1:
                action['needs_review'] = True
                action['remind_at'] = None
                action['review_reasons'].append('来源已有不同的采纳事项，请先处理原待办')
            if existing or len(saved_records) == 1:
                record_id = existing['record_id'] if existing else next(iter(saved_records))
                record = self.crm.get_record(owner, record_id)
                proposal_id = existing['proposal_id'] if existing else record.get('proposal_id') if record else None
                action.update(adopted_record_id=record_id, proposal_id=proposal_id,
                              **self._adoption_result(owner, record_id, proposal_id))
                current_records.add(record_id)
                if record and record['customer_id'] != row['customer_id']:
                    action['needs_review'] = True
                    action['review_reasons'].append('已采纳事项的客户与交流不同，请打开原待办核对')
                    warnings.append('“' + action['title'] + '”已采纳事项的客户与交流不同；保留原事项，请打开原待办核对')
                old = json.loads(existing['snapshot_json']) if existing else None
                adopted_terms_changed = bool(old and (old.get('remind_at') != when or old.get('owner_hint') != action['owner_hint']
                    or any(old.get(field) != action.get(field) for field in TERM_FIELDS if field.endswith(('_at', '_date'))
                           or field in ('duration_minutes', 'executor_kind'))))
                if adopted_terms_changed:
                    action['needs_review'] = True
                    action['remind_at'] = None
                    action['execution_at'] = None
                    action['review_reasons'].append('已采纳事项的时间或责任人发生变化，请核对原提醒')
                if needs_review or adopted_terms_changed:
                    warnings.append('“' + action['title'] + '”已采纳后来源出现变化；保留原提醒，请打开原待办核对')
            action['source_review_reasons'] = list(action['review_reasons'])
            scope = self._action_project_scope(db, owner, row, action)
            action['project_scope'] = scope
            action['project_scope_revision'] = scope['revision']
            action['opportunity_id'] = scope['opportunity_id']
            if scope['reasons']:
                action['needs_review'] = True
                action['review_reasons'].extend(scope['reasons'])
            actions.append(action)
        all_records = {item['record_id'] for item in adoptions.values()}
        for source in sources:
            for old in db.execute('SELECT record_id FROM crm_material_actions WHERE owner=? AND material_id=? AND record_id IS NOT NULL',
                                  (owner, source['material_id'])):
                all_records.add(old['record_id'])
        previous = []
        for record_id in sorted(all_records - current_records):
            record = self.crm.get_record(owner, record_id)
            if record:
                previous.append({field: record.get(field) for field in ('id', 'title', 'status', 'task_status', 'proposal_status', 'remind_at', 'proposal_id')})
                warnings.append('“' + record['title'] + '”曾经采纳；当前来源已改变，保留原提醒，请打开原待办核对')
        if row['occurred_at'] is None:
            warnings.append('交流发生时间未确认，未使用材料创建时间推算提醒')
        visit = self._public(db, self._require(db, owner, identifier))
        visit['action_count'] = len(actions)
        return {'visit': visit, 'sources': sources, 'actions': actions, 'customer_drafts': list(drafts.values()),
                'warnings': list(dict.fromkeys(warnings)), 'previous_adoptions': previous,
                'blocked_source_ids': blocked_sources,
                'record_sources': record_sources, 'blocked_record_ids': blocked_records,
                'excluded_sources': [source['linked_material_id'] for source in sources if source['source_use'] == 'excluded']}

    def detail(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            return self._snapshot(owner, identifier)

    def record_action_context(self, owner, record_id, action=None):
        """Read exchange guards for an action that keeps its original record ID.

        Callers take this snapshot before adoption, then compare ``revision``
        using _fingerprint inside their transaction. No action is duplicated or
        confirmed here; an already adopted retry can return its original row.
        """
        owner, record_id = _owner(owner), _identifier(record_id)
        with self.crm._lock:
            db = self.crm._db
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
                return None
            link = db.execute('SELECT visit_id FROM crm_visit_records WHERE owner=? AND record_id=?',
                              (owner, record_id)).fetchone()
            if link is None:
                return None
            detail = self._snapshot(owner, link['visit_id'])
            visit = detail['visit']
            reasons = []
            if detail['blocked_source_ids'] or detail['blocked_record_ids']:
                reasons.append('关联交流仍有来源未整理或稍后核对，请先核对完整来源')
            own = next((item for item in detail['record_sources'] if item['record_id'] == record_id), None)
            if own:
                reasons.extend(own['review_reasons'])
            selected = [source for source in detail['sources'] if source['source_use'] != 'excluded']
            for source in selected:
                material = source['material']
                if (material['customer_id'] != visit['customer_id'] or
                        (material['occurred_at'] != visit['occurred_at'] and not source['time_association_confirmed'])):
                    reasons.append('关联交流来源的客户或发生时间不一致，请核对来源归属')
                if any(other['visit_id'] != visit['id'] for other in self._source_links(db, owner, material['id'])):
                    reasons.append('同一录音关联了不同交流，请先核对归档')
            if isinstance(action, dict):
                core = re.sub(r'^(?:发送|提交|补充|提供|整理|完成|联系|确认|跟进|准备|安排|交付)', '', _identity(action.get('title', '')))
                texts = [source['text'] for source in selected]
                texts.extend(item['record']['content'] or item['record']['original_content'] for item in detail['record_sources'])
                for text in texts:
                    for clause in re.findall(r'[^。！？\n]+', text):
                        if not _CLOSED.search(clause):
                            continue
                        normalized = _identity(clause)
                        if (len(core) >= 2 and core in normalized) or any(noun in core and noun in normalized
                                for noun in ('方案', '资料', '材料', '报价', '合同', '案例', '报告', '邮件')):
                            reasons.append('关联来源中存在取消、完成或改期信息，请核对原事项是否仍需执行')
            return {'visit_id': visit['id'], 'revision': visit['revision'], 'blocked': bool(reasons),
                    'reasons': list(dict.fromkeys(reasons))}

    def merge_actions(self, owner, identifier, keys, revision):
        owner, identifier = _owner(owner), _identifier(identifier)
        _text(revision, '交流版本', 64, required=True)
        if not isinstance(keys, list) or not 2 <= len(keys) <= 50 or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError('请选择至少两项不同的行动核对合并')
        with self.crm._lock:
            detail = self._snapshot(owner, identifier)
            if detail['visit']['revision'] != revision:
                raise ValueError('交流或材料已有变化，请刷新后核对')
            selected = [item for item in detail['actions'] if item['key'] in keys]
            if len(selected) != len(keys):
                raise ValueError('行动不属于当前交流版本，请刷新后核对')
            if any(item['adopted_record_id'] for item in selected):
                raise ValueError('已采纳事项请先打开原待办处理，不能合并创建另一个提醒')
            group_key = 'm' + _hash(sorted(keys))[:24]
            with self.crm._transaction() as db:
                row = self._require(db, owner, identifier)
                if self._fingerprint(db, row) != revision:
                    raise ValueError('交流或材料已有变化，请刷新后核对')
                references = [ref for item in selected for ref in item['references']]
                for identity in {value for item in selected for value in item['identities']}:
                    db.execute('INSERT INTO crm_visit_action_groups VALUES (?,?,?,?,?,?) ON CONFLICT(owner,visit_id,identity) '
                        'DO UPDATE SET group_key=excluded.group_key,references_json=excluded.references_json',
                        (owner, identifier, identity, group_key, _json(references), self.clock()))
                db.execute('UPDATE crm_visits SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (self.clock(), owner, identifier))
            return self._snapshot(owner, identifier)

    def adopt_material(self, owner, identifier, action_id, revision, *, expected_visit_revision=None,
                       project_scope_revision=None, opportunity_id=None, confirm_single_action=False):
        owner, identifier, action_id = _owner(owner), _identifier(identifier), _identifier(action_id)
        if expected_visit_revision is not None and (not isinstance(expected_visit_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_visit_revision)):
            raise ValueError('交流版本无效')
        with self.crm._lock:
            links = self._source_links(self.crm._db, owner, identifier)
            if not links:
                return None
            if len({link['visit_id'] for link in links}) != 1:
                raise ValueError('同一录音归档到不同交流，请先核对来源')
            material = self.materials.detail(owner, identifier)['material']
            if material['revision'] != revision:
                raise ValueError('材料已有变化，请刷新后核对')
            detail = self._snapshot(owner, links[0]['visit_id'])
            linked_sources = [source for source in detail['sources'] if source['material_id'] == material['id']]
            if linked_sources and all(source['source_use'] == 'excluded' for source in linked_sources):
                raise ValueError('这份来源已暂不使用，请先明确重新纳入并核对交流影响')
            action = next((item for item in detail['actions'] if any(ref['material_id'] == material['id'] and ref['action_id'] == action_id
                                                                      for ref in item['references'])), None)
            if action is None:
                raise ValueError('这项行动不属于当前交流版本，请刷新后核对')
            if expected_visit_revision is not None and expected_visit_revision != detail['visit']['revision']:
                raise ValueError('交流或来源项目已有变化，请刷新核对后采用；没有建立待办')
            return self.adopt(owner, links[0]['visit_id'], action['key'], detail['visit']['revision'],
                project_scope_revision=project_scope_revision, opportunity_id=opportunity_id,
                confirm_single_action=confirm_single_action)

    def adopt(self, owner, identifier, action_key, revision, *, project_scope_revision=None,
              opportunity_id=None, confirm_single_action=False):
        owner, identifier = _owner(owner), _identifier(identifier)
        _text(action_key, '行动编号', 128, required=True)
        _text(revision, '交流版本', 64, required=True)
        if project_scope_revision is not None and (not isinstance(project_scope_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', project_scope_revision)):
            raise ValueError('项目范围版本无效')
        if type(confirm_single_action) is not bool:
            raise ValueError('请明确这是否是一项跨项目行动')
        with self.crm._lock:
            detail = self._snapshot(owner, identifier)
            if detail['visit']['revision'] != revision:
                raise ValueError('交流或材料已有变化，请刷新核对后再采纳')
            action = next((item for item in detail['actions'] if item['key'] == action_key), None)
            if action is None:
                raise ValueError('行动不属于当前交流版本，请刷新后核对')
            with self.crm._transaction() as db:
                visit = self._require(db, owner, identifier)
                if self._fingerprint(db, visit) != revision:
                    raise ValueError('交流或材料已有变化，请刷新核对后再采纳')
                prior = db.execute('SELECT * FROM crm_visit_adoptions WHERE owner=? AND visit_id=? AND action_key=?',
                                   (owner, identifier, action_key)).fetchone()
                if prior:
                    # Late sources can discover the same already adopted matter;
                    # link only verified, unadopted references to its original row.
                    if not action['needs_review']:
                        self._link_adoption(db, owner, action, prior['record_id'], prior['proposal_id'])
                    return self._adoption_result(owner, prior['record_id'], prior['proposal_id'])
                # Recover adoption committed before an exchange was assembled,
                # or before a process stopped. Every current source shares it.
                if action['adopted_record_id']:
                    result = self._adoption_result(owner, action['adopted_record_id'], action['proposal_id'])
                    db.execute('INSERT INTO crm_visit_adoptions VALUES (?,?,?,?,?,?,?)',
                        (owner, identifier, action_key, action['adopted_record_id'], action['proposal_id'], _json(action), self.clock()))
                    if not action['needs_review']:
                        self._link_adoption(db, owner, action, action['adopted_record_id'], action['proposal_id'])
                    return result
                scope = self._action_project_scope(db, owner, visit, action,
                    opportunity_id=opportunity_id, confirm_single_action=confirm_single_action)
                if project_scope_revision is not None and project_scope_revision != scope['revision']:
                    raise ValueError('来源项目范围已有变化，请重新核对；没有建立待办')
                if scope['status'] == 'needs_review':
                    raise ValueError('；'.join(scope['reasons']))
                if action.get('source_review_reasons', action['review_reasons']):
                    raise ValueError('这项行动需要核对来源、时间或责任人；请核对材料后再采纳')
                reference = next((ref for ref in action['references'] if ref.get('verified') or
                    (ref.get('source_current') and ref.get('original_kind') == 'suggestion' and not ref.get('quote'))), None)
                if reference is None:
                    raise ValueError('行动没有可核验来源，请核对材料')
                material = self.materials._require(db, owner, reference['material_id'])
                if material['status'] != 'review':
                    raise ValueError('来源材料已有变化，请刷新后核对')
                parent = self.crm._require_record(db, owner, material['record_id'])
                if parent['customer_id'] != visit['customer_id']:
                    raise ValueError('来源记录的客户归属已有变化，请核对')
                label = '明确承诺（用户采纳）' if action['kind'] == 'commitment' else '秘书建议（用户采纳，尚未安排提醒）'
                content = f"来源交流 #{identifier} · {visit['title']}\n类型：{label}\n" + action['evidence']
                if action['reason']:
                    content += '\n整理说明：' + action['reason']
                original = action['evidence'] if any(ref.get('quote') for ref in action['references']) else content
                record_id = self.materials._record(db, owner, f'visit-action:{identifier}:{action_key}', action['title'],
                    original, content, visit['customer_id'], parent['id'], 'action')
                terms = {field: action.get(field) for field in TERM_FIELDS}
                self.materials._save_terms(db, owner, record_id, terms)
                proposal_id = None
                when = action['execution_at']
                if can_schedule(action) and action['kind'] == 'commitment' and when > self.clock() + 5:
                    reply = self.crm._execute(db, owner, {'action': 'propose', 'title': action['title'], 'remind_at': when,
                        'duration_minutes': action['duration_minutes'] if action['duration_minutes'] is not None else 30,
                        'deadline_at': action['deadline_at']}, self.clock())
                    match = re.match(r'^已整理，待你确认：P([0-9]+)', reply)
                    if not match:
                        raise ValueError('安排尚未建立，请刷新后核对时间')
                    proposal_id = int(match[1])
                    self.crm._remember_proposal(db, owner, record_id, proposal_id, self.clock())
                    db.execute('UPDATE crm_records SET proposal_id=? WHERE owner=? AND id=?', (proposal_id, owner, record_id))
                db.execute('INSERT INTO crm_visit_adoptions VALUES (?,?,?,?,?,?,?)',
                           (owner, identifier, action_key, record_id, proposal_id, _json(action), self.clock()))
                self._link_adoption(db, owner, action, record_id, proposal_id)
                self._save_action_project(db, owner, record_id, scope)
                return self._adoption_result(owner, record_id, proposal_id)

    @staticmethod
    def _link_adoption(db, owner, action, record_id, proposal_id):
        for reference in action['references']:
            supported = reference.get('verified') or (reference.get('source_current') and
                reference.get('original_kind') == 'suggestion' and not reference.get('quote'))
            if reference.get('action_id') is not None and supported:
                db.execute('UPDATE crm_material_actions SET record_id=?,proposal_id=? WHERE owner=? AND material_id=? AND id=? '
                           'AND (record_id IS NULL OR record_id=?)',
                           (record_id, proposal_id, owner, reference['material_id'], reference['action_id'], record_id))
