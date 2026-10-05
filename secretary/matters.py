"""Stable work goals layered over existing evidence, actions and schedules.

Grouping never rewrites CRM rows or creates a reminder. Only an explicit,
snapshot-checked lifecycle cancellation invokes the existing scheduler.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager

from .crm import RecordConflict, _identifier, _owner, _pagination, _public, _text
from .store import _timestamp

MatterConflict = RecordConflict


STATUSES = ("following", "waiting", "paused", "ended")
VISIBILITIES = ("active", "archived", "trash")
TABLES = {"record": "crm_records", "plan": "crm_secretary_plans", "task": "tasks",
          "visit": "crm_visits", "material": "crm_materials", "discussion": "crm_sales_discussions"}
LINK_FIELDS = {"source_record_ids": ("record", "source"), "action_record_ids": ("record", "action"),
               "plan_ids": ("plan", "plan"), "task_ids": ("task", "schedule"),
               "visit_ids": ("visit", "exchange"), "material_ids": ("material", "source")}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _ids(value, name):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError(f"{name}须为最多200个编号的列表。")
    result = [_identifier(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{name}不能重复。")
    return result


class MatterService:
    def __init__(self, crm, *, clock=time.time):
        self.crm, self.clock = crm, clock
        self._savepoint_serial = 0
        with crm._transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS crm_matters(
                id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,title TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'following',
                outcome TEXT NOT NULL DEFAULT '',visibility TEXT NOT NULL DEFAULT 'active',
                customer_id INTEGER,opportunity_id INTEGER,revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(owner,id),
                CHECK(status IN ('following','waiting','paused','ended')),
                CHECK(visibility IN ('active','archived','trash')),
                FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id));
                CREATE INDEX IF NOT EXISTS crm_matters_workspace ON crm_matters(owner,visibility,status,updated_at);
                CREATE TABLE IF NOT EXISTS crm_matter_links(
                owner TEXT NOT NULL,matter_id INTEGER NOT NULL,entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,role TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(owner,matter_id,entity_type,entity_id,role),
                FOREIGN KEY(owner,matter_id) REFERENCES crm_matters(owner,id));
                CREATE UNIQUE INDEX IF NOT EXISTS crm_matter_action_owner
                ON crm_matter_links(owner,entity_id) WHERE entity_type='record' AND role='action';
                CREATE INDEX IF NOT EXISTS crm_matter_entity ON crm_matter_links(owner,entity_type,entity_id);
                CREATE TABLE IF NOT EXISTS crm_matter_operations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                operation TEXT NOT NULL,payload_hash TEXT NOT NULL,matter_id INTEGER NOT NULL,
                before_json TEXT NOT NULL,after_json TEXT NOT NULL,undone INTEGER NOT NULL DEFAULT 0,
                undo_of INTEGER,created_at REAL NOT NULL,UNIQUE(owner,request_id),
                FOREIGN KEY(owner,matter_id) REFERENCES crm_matters(owner,id));''')
        crm.matter_service = self

    @contextmanager
    def _transaction(self):
        """Keep a caller's write transaction; roll back only this nested unit."""
        with self.crm._lock:
            db = self.crm._db
            if not db.in_transaction:
                with self.crm._transaction() as connection:
                    yield connection
                return
            self._savepoint_serial += 1
            name = f'matter_write_{self._savepoint_serial}'
            db.execute(f'SAVEPOINT {name}')
            try:
                yield db
            except BaseException:
                db.execute(f'ROLLBACK TO {name}')
                db.execute(f'RELEASE {name}')
                raise
            else:
                db.execute(f'RELEASE {name}')

    @staticmethod
    def _exists(db, table):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    @staticmethod
    def _schema(data, allowed, required=()):
        if not isinstance(data, dict) or set(data) - set(allowed) or not set(required) <= set(data):
            raise ValueError("事项操作字段无效，请刷新后核对。")

    @staticmethod
    def _request(data):
        return _text(data.get("request_id"), "请求标识", 200, required=True).strip()

    @staticmethod
    def _require(db, owner, identifier):
        row = db.execute("SELECT * FROM crm_matters WHERE owner=? AND id=?", (owner, _identifier(identifier))).fetchone()
        if row is None:
            raise KeyError("未找到你的事项。")
        return row

    @staticmethod
    def _revision(row, expected):
        if type(expected) is not int or expected != row["revision"]:
            raise RecordConflict("事项已有变化，请刷新后核对。")

    def _entity(self, db, owner, kind, identifier):
        if not isinstance(kind, str) or kind not in TABLES:
            raise ValueError("关联类型无效。")
        identifier = _identifier(identifier)
        table = TABLES[kind]
        row = db.execute(f"SELECT * FROM {table} WHERE owner=? AND id=?", (owner, identifier)).fetchone() if self._exists(db, table) else None
        if row is None or (kind == 'record' and row['hidden']):
            raise KeyError("未找到你的有效关联资料。")
        if kind == 'plan':
            self._entity(db, owner, 'record', row['record_id'])
        return row

    def _link(self, db, owner, matter_id, kind, identifier, role, now):
        row = self._entity(db, owner, kind, identifier)
        roles = {'record': ('source', 'action', 'recap'), 'plan': ('plan', 'source'),
                 'task': ('schedule', 'source'), 'visit': ('exchange', 'source'), 'material': ('source',),
                 'discussion': ('source', 'discussion')}
        if role not in roles[kind]:
            raise ValueError("关联用途无效。")
        if kind == 'record' and role == 'action':
            if row['kind'] != 'action':
                raise ValueError("只有原有待办可以作为事项步骤。")
            prior = db.execute("SELECT matter_id FROM crm_matter_links WHERE owner=? AND entity_type='record' AND entity_id=? AND role='action'", (owner, identifier)).fetchone()
            if prior and prior['matter_id'] != matter_id:
                raise RecordConflict("这个步骤已归入另一事项，请先合并或拆分。")
        matter = self._require(db, owner, matter_id)
        customer_id = json.loads(row['data_json']).get('customer_id') if kind == 'plan' else dict(row).get('customer_id')
        if matter['customer_id'] and customer_id and matter['customer_id'] != customer_id:
            raise ValueError("关联资料属于不同单位，请分别整理。")
        if kind in ('record', 'visit', 'material') and self._exists(db, 'crm_opportunity_links'):
            from .sales_workspace import SalesWorkspace
            link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?", (owner, kind, identifier)).fetchone()
            if link and link['source_snapshot'] == SalesWorkspace._entity_snapshot(row) and link['opportunity_id'] and matter['opportunity_id'] and link['opportunity_id'] != matter['opportunity_id']:
                raise ValueError('关联资料属于另一个项目，请先核对事项归属。')
        result = db.execute('INSERT OR IGNORE INTO crm_matter_links VALUES (?,?,?,?,?,?)',
                            (owner, matter_id, kind, identifier, role, now))
        return bool(result.rowcount)

    def _scope(self, db, owner, customer_id, opportunity_id):
        if customer_id is not None:
            _identifier(customer_id)
            if not db.execute('SELECT 1 FROM crm_customers WHERE owner=? AND id=?', (owner, customer_id)).fetchone():
                raise KeyError('未找到你的客户。')
        if opportunity_id is not None:
            _identifier(opportunity_id)
            project = db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?', (owner, opportunity_id)).fetchone() if self._exists(db, 'crm_opportunities') else None
            if project is None:
                raise KeyError('未找到你的项目。')
            if project['archived']:
                raise ValueError('项目已归档，请核对当前目标。')
            if customer_id is not None and project['customer_id'] != customer_id:
                raise ValueError('项目与单位不一致。')
            customer_id = project['customer_id']
        return customer_id, opportunity_id

    def _snapshot(self, db, owner, identifiers):
        result = {}
        for identifier in identifiers:
            row = self._require(db, owner, identifier)
            links = db.execute('SELECT * FROM crm_matter_links WHERE owner=? AND matter_id=? ORDER BY entity_type,entity_id,role', (owner, identifier)).fetchall()
            result[str(identifier)] = {'matter': dict(row), 'links': [dict(link) for link in links]}
        return result

    def _replay(self, db, owner, operation, data, identifier=None):
        request_id = self._request(data)
        payload_hash = _hash({'operation': operation, 'matter_id': identifier, 'data': data})
        row = db.execute('SELECT * FROM crm_matter_operations WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        if row:
            if row['payload_hash'] != payload_hash:
                raise RecordConflict('请求标识已用于另一操作，请重新核对。')
            ids = [int(item) for item in json.loads(row['after_json'])]
            return self._result(db, owner, row['matter_id'], row['id'], ids, replayed=True), payload_hash
        return None, payload_hash

    def _operation(self, db, owner, operation, data, identifier, before, affected, payload_hash, *, undo_of=None):
        return db.execute('''INSERT INTO crm_matter_operations(owner,request_id,operation,payload_hash,
            matter_id,before_json,after_json,undo_of,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
            (owner, self._request(data), operation, payload_hash, identifier, _json(before),
             _json(self._snapshot(db, owner, affected)), undo_of, _timestamp(self.clock()))).lastrowid

    def _result(self, db, owner, identifier, operation_id=None, affected=(), **extra):
        result = {'matter': self._detail(db, owner, identifier), **extra}
        if operation_id:
            result['operation_id'] = operation_id
        related = [self._detail(db, owner, item) for item in affected if item != identifier]
        if related:
            result['related_matters'] = related
        return result

    def _insert(self, db, owner, data, now):
        title = _text(data.get('title'), '事项名称', 300, required=True).strip()
        objective = _text(data.get('objective', ''), '推进目标', 4000).strip()
        links = {field: _ids(data.get(field, []), field) for field in LINK_FIELDS}
        if set(links['source_record_ids']) & set(links['action_record_ids']):
            raise ValueError('同一记录不能同时作为原话和步骤。')
        customers, projects = set(), set()
        for field, identifiers in links.items():
            kind, _ = LINK_FIELDS[field]
            for identifier in identifiers:
                row = self._entity(db, owner, kind, identifier)
                customer = json.loads(row['data_json']).get('customer_id') if kind == 'plan' else dict(row).get('customer_id')
                if customer:
                    customers.add(customer)
                if kind in ('record', 'visit', 'material') and self._exists(db, 'crm_opportunity_links'):
                    from .sales_workspace import SalesWorkspace
                    link = db.execute("SELECT l.*,o.archived FROM crm_opportunity_links l JOIN crm_opportunities o ON o.owner=l.owner AND o.id=l.opportunity_id WHERE l.owner=? AND l.entity_type=? AND l.entity_id=?", (owner, kind, identifier)).fetchone()
                    if link and not link['archived'] and link['source_snapshot'] == SalesWorkspace._entity_snapshot(row) and link['opportunity_id']:
                        projects.add(link['opportunity_id'])
                if kind in ('plan', 'discussion'):
                    project = json.loads(row['data_json']).get('opportunity_id') if kind == 'plan' else row['opportunity_id']
                    if project:
                        projects.add(project)
        if len(customers) > 1:
            raise ValueError('不同单位的资料不能静默合并。')
        if len(projects) > 1:
            raise ValueError('不同项目的步骤需要分别整理，实际会议可共用。')
        if data.get('customer_id') is not None:
            _identifier(data['customer_id'])
        customer = data.get('customer_id') if data.get('customer_id') is not None else next(iter(customers), None)
        project = data.get('opportunity_id') if data.get('opportunity_id') is not None else next(iter(projects), None)
        customer, project = self._scope(db, owner, customer, project)
        if projects and project not in projects:
            raise ValueError('事项与资料的项目不一致。')
        if customers and customer and customer not in customers:
            raise ValueError('事项与资料的单位不一致。')
        identifier = db.execute('''INSERT INTO crm_matters(owner,title,objective,customer_id,
            opportunity_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)''', (owner, title, objective, customer, project, now, now)).lastrowid
        for field, identifiers in links.items():
            kind, role = LINK_FIELDS[field]
            for entity_id in identifiers:
                self._link(db, owner, identifier, kind, entity_id, role, now)
        return identifier

    def create(self, owner, data):
        owner = _owner(owner)
        self._schema(data, {'title', 'objective', 'customer_id', 'opportunity_id', 'request_id', *LINK_FIELDS}, {'title', 'request_id'})
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'create', data)
            if replay:
                return replay
            identifier = self._insert(db, owner, data, _timestamp(self.clock()))
            operation = self._operation(db, owner, 'create', data, identifier, {}, [identifier], payload)
            return self._result(db, owner, identifier, operation)

    def update(self, owner, identifier, data):
        owner, identifier = _owner(owner), _identifier(identifier)
        self._schema(data, {'title', 'objective', 'status', 'outcome', 'customer_id', 'opportunity_id', 'expected_revision', 'request_id'}, {'expected_revision', 'request_id'})
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'update', data, identifier)
            if replay:
                return replay
            row = self._require(db, owner, identifier)
            self._revision(row, data['expected_revision'])
            if row['visibility'] != 'active':
                raise RecordConflict('事项已收起，请先恢复后再修改。')
            values = {key: row[key] for key in ('title', 'objective', 'status', 'outcome', 'customer_id', 'opportunity_id')}
            for key, maximum in (('title', 300), ('objective', 4000), ('outcome', 20000)):
                if key in data:
                    values[key] = _text(data[key], key, maximum, required=key == 'title').strip()
            if 'status' in data:
                if data['status'] not in STATUSES:
                    raise ValueError('事项状态无效。')
                values['status'] = data['status']
            values['customer_id'], values['opportunity_id'] = self._scope(db, owner,
                data.get('customer_id', row['customer_id']), data.get('opportunity_id', row['opportunity_id']))
            if values['customer_id'] != row['customer_id'] and db.execute('SELECT 1 FROM crm_matter_links WHERE owner=? AND matter_id=?', (owner, identifier)).fetchone():
                if row['customer_id'] is not None:
                    raise ValueError('已有来源的事项不能直接换单位，请拆分后核对。')
                for link in db.execute('SELECT * FROM crm_matter_links WHERE owner=? AND matter_id=?', (owner,identifier)).fetchall():
                    entity=self._entity(db,owner,link['entity_type'],link['entity_id'])
                    customer=json.loads(entity['data_json']).get('customer_id') if link['entity_type']=='plan' else dict(entity).get('customer_id')
                    if customer and customer!=values['customer_id']:
                        raise ValueError('已有资料属于不同单位，请先核对。')
            before = self._snapshot(db, owner, [identifier])
            db.execute('UPDATE crm_matters SET title=?,objective=?,status=?,outcome=?,customer_id=?,opportunity_id=?,revision=revision+1,updated_at=? WHERE owner=? AND id=?',
                       (*values.values(), _timestamp(self.clock()), owner, identifier))
            operation = self._operation(db, owner, 'update', data, identifier, before, [identifier], payload)
            return self._result(db, owner, identifier, operation)

    def attach(self, owner, identifier, entity_type, entity_id, role='source', expected_revision=None):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self._transaction() as db:
            row = self._require(db, owner, identifier)
            if expected_revision is not None:
                self._revision(row, expected_revision)
            if row['visibility'] != 'active' or row['status'] == 'ended':
                raise RecordConflict('事项已结束或收起，不能自动追加资料。')
            if self._link(db, owner, identifier, entity_type, entity_id, role, _timestamp(self.clock())):
                db.execute('UPDATE crm_matters SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (_timestamp(self.clock()), owner, identifier))
            return self._result(db, owner, identifier)

    def _entities(self, db, owner, identifier):
        links = [dict(row) for row in db.execute('SELECT * FROM crm_matter_links WHERE owner=? AND matter_id=? ORDER BY entity_type,entity_id,role', (owner, identifier))]
        ids = {kind: set() for kind in TABLES}
        action_ids, source_ids = set(), set()
        for link in links:
            ids[link['entity_type']].add(link['entity_id'])
            if link['entity_type'] == 'record':
                (action_ids if link['role'] == 'action' else source_ids).add(link['entity_id'])
        # Resolve retained source relations to a fixed point, without importing
        # sibling actions. One exchange may legitimately support several goals.
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        previous = None
        while previous != {kind: frozenset(values) for kind, values in ids.items()}:
            previous = {kind: frozenset(values) for kind, values in ids.items()}
            if 'crm_secretary_plans' in tables:
                for row in db.execute('SELECT * FROM crm_secretary_plans WHERE owner=?', (owner,)):
                    turns = set(r[0] for r in db.execute('SELECT record_id FROM crm_secretary_turns WHERE owner=? AND plan_id=?', (owner, row['id']))) if 'crm_secretary_turns' in tables else set()
                    if row['record_id'] in ids['record'] or turns & ids['record']:
                        ids['plan'].add(row['id'])
                    if row['id'] in ids['plan']:
                        ids['record'].update(turns | {row['record_id']})
                        if row['visit_id']:
                            ids['visit'].add(row['visit_id'])
                        task_id = json.loads(row['data_json']).get('task_id')
                        if type(task_id) is int:
                            ids['task'].add(task_id)
            if 'crm_visit_records' in tables:
                for row in db.execute('SELECT * FROM crm_visit_records WHERE owner=?', (owner,)):
                    if row['record_id'] in ids['record']:
                        ids['visit'].add(row['visit_id'])
                    if row['visit_id'] in ids['visit']:
                        ids['record'].add(row['record_id'])
            if 'crm_materials' in tables:
                for row in db.execute('SELECT id,record_id FROM crm_materials WHERE owner=?', (owner,)):
                    if row['record_id'] in ids['record']:
                        ids['material'].add(row['id'])
                    if row['id'] in ids['material'] and row['record_id']:
                        ids['record'].add(row['record_id'])
            if 'crm_visit_sources' in tables:
                for row in db.execute('SELECT visit_id,material_id FROM crm_visit_sources WHERE owner=?', (owner,)):
                    if row['material_id'] in ids['material']:
                        ids['visit'].add(row['visit_id'])
                    if row['visit_id'] in ids['visit']:
                        ids['material'].add(row['material_id'])
            if 'crm_secretary_plan_attachments' in tables:
                for row in db.execute('SELECT plan_id,material_id FROM crm_secretary_plan_attachments WHERE owner=?', (owner,)):
                    if row['plan_id'] in ids['plan']:
                        ids['material'].add(row['material_id'])
            if 'crm_secretary_turn_attachments' in tables and 'crm_secretary_turns' in tables:
                for row in db.execute('''SELECT a.material_id,t.record_id,t.plan_id FROM crm_secretary_turn_attachments a
                    JOIN crm_secretary_turns t ON t.owner=a.owner AND t.id=a.turn_id WHERE a.owner=?''', (owner,)):
                    if row['record_id'] in ids['record'] or row['plan_id'] in ids['plan']:
                        ids['material'].add(row['material_id'])
            if 'crm_sales_discussions' in tables:
                for thread_id in sorted(ids['discussion']):
                    row = db.execute('SELECT source_record_id FROM crm_sales_discussions WHERE owner=? AND id=?', (owner, thread_id)).fetchone()
                    if row and row['source_record_id']:
                        ids['record'].add(row['source_record_id'])
                    if 'crm_discussion_attachments' in tables:
                        ids['material'].update(r[0] for r in db.execute('SELECT material_id FROM crm_discussion_attachments WHERE owner=? AND thread_id=?', (owner, thread_id)))
        source_ids.update(ids['record'] - action_ids)
        proposals = {}
        for record_id in sorted(ids['record']):
            row = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=?', (owner, record_id)).fetchone()
            if row and row['proposal_id']:
                proposals[row['proposal_id']] = True
            for history in db.execute('SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?', (owner, record_id)):
                proposals[history[0]] = True
        for proposal_id in proposals:
            proposal = db.execute('SELECT * FROM proposals WHERE owner=? AND id=?', (owner, proposal_id)).fetchone()
            if proposal:
                ids['task'].update(proposal[key] for key in ('task_id', 'target_task_id') if proposal[key])
        return links, ids, action_ids, source_ids

    def _detail(self, db, owner, identifier):
        row = self._require(db, owner, identifier)
        result = _public(row)
        links, ids, actions, sources = self._entities(db, owner, identifier)
        result.update(links=[{k: v for k, v in link.items() if k != 'owner'} for link in links])
        def records(record_ids):
            return [_public(r) for i in sorted(record_ids) if (r := db.execute(self.crm._RECORD_SELECT + 'WHERE r.owner=? AND r.id=? AND r.hidden=0', (owner, i)).fetchone())]
        result['actions'], result['sources'] = records(actions), records(sources)
        result['plans'] = []
        flow = getattr(self.crm, 'secretary_flow', None)
        for i in sorted(ids['plan']):
            p = db.execute('SELECT * FROM crm_secretary_plans WHERE owner=? AND id=?', (owner, i)).fetchone()
            if p:
                result['plans'].append(flow._public_plan(db, p) if flow else {**json.loads(p['data_json']), **_public(p)})
        for kind, plural in (('task', 'tasks'), ('visit', 'visits'), ('material', 'materials')):
            rows = []
            if self._exists(db, TABLES[kind]):
                for i in sorted(ids[kind]):
                    item = db.execute(f'SELECT * FROM {TABLES[kind]} WHERE owner=? AND id=?', (owner, i)).fetchone()
                    if item:
                        rows.append(_public(item))
            result[plural] = rows
        result['discussions'] = []
        for i in sorted(ids['discussion']):
            item = db.execute('SELECT * FROM crm_sales_discussions WHERE owner=? AND id=?', (owner, i)).fetchone()
            if item:
                thread = _public(item)
                thread['messages'] = [_public(message) for message in db.execute('SELECT * FROM crm_sales_discussion_messages WHERE owner=? AND thread_id=? ORDER BY id', (owner, i))] if self._exists(db, 'crm_sales_discussion_messages') else []
                result['discussions'].append(thread)
        task_context = {item['id']: item for item in self.crm._task_context(owner, result['tasks'])}
        result['tasks'] = sorted(task_context.values(), key=lambda task: (task['remind_at'] or float('inf'), task['id']))
        customer = db.execute('SELECT name FROM crm_customers WHERE owner=? AND id=?', (owner, row['customer_id'])).fetchone()
        project = db.execute('SELECT name FROM crm_opportunities WHERE owner=? AND id=?', (owner, row['opportunity_id'])).fetchone() if self._exists(db, 'crm_opportunities') else None
        result.update(customer_name=customer['name'] if customer else '', project_name=project['name'] if project else '',
                      action_count=len(result['actions']), completed_action_count=sum(item['status'] == 'done' for item in result['actions']))
        result['next_action'] = next((item for item in result['actions'] if item['status'] != 'done'), None)
        result['next_schedule'] = next((item for item in result['tasks'] if item['status'] == 'pending'), None)
        if not result['next_schedule']:
            result['next_schedule'] = next(iter(sorted((p for p in result['plans'] if p.get('date') and p.get('status') not in ('cancelled', 'recapped') and not p.get('task_id')), key=lambda p: (p['date'], p.get('time') or '99:99'))), None)
        events = [{'type': 'source', 'id': item['id'], 'title': item['title'], 'created_at': item['created_at']} for item in result['sources']]
        events += [{'type': 'action', 'id': item['id'], 'title': item['title'], 'created_at': item['created_at']} for item in result['actions']]
        for key, kind in (('plans', 'plan'), ('tasks', 'schedule'), ('visits', 'exchange'), ('materials', 'material')):
            events += [{'type': kind, 'id': item['id'], 'title': item.get('title', ''), 'created_at': item.get('occurred_at') or item['created_at']} for item in result[key]]
        for record_id in sorted(ids['record']):
            events += [{**_public(item), 'type': 'activity'} for item in db.execute('SELECT * FROM crm_activities WHERE owner=? AND record_id=?', (owner, record_id))]
        for thread in result['discussions']:
            events += [{'type': 'discussion', 'id': message['id'], 'thread_id': thread['id'], 'title': thread['title'],
                        'text': message['text'], 'role': message['role'], 'created_at': message['created_at']} for message in thread['messages']]
        result['operations'] = []
        for op in db.execute('SELECT * FROM crm_matter_operations WHERE owner=? ORDER BY id', (owner,)):
            after = json.loads(op['after_json'])
            if str(identifier) not in after:
                continue
            undoable = not op['undone'] and op['operation'] != 'undo' and all((current := db.execute('SELECT revision FROM crm_matters WHERE owner=? AND id=?', (owner, int(i))).fetchone()) and current['revision'] == snapshot['matter']['revision'] for i, snapshot in after.items())
            result['operations'].append({'id': op['id'], 'operation': op['operation'], 'created_at': op['created_at'],
                'undone': bool(op['undone']), 'undoable': bool(undoable), 'matter_id': op['matter_id'],
                'expected_revision': self._require(db, owner, op['matter_id'])['revision']})
            events.append({'type': 'operation', 'id': op['id'], 'title': op['operation'], 'created_at': op['created_at']})
        result['events'] = sorted(events, key=lambda item: (item['created_at'], item['id']))
        return result

    def detail(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            return self._detail(self.crm._db, owner, identifier)

    get = detail

    @staticmethod
    def _summary(detail):
        return {key: value for key, value in detail.items() if key not in ('actions', 'sources', 'plans', 'tasks', 'visits', 'materials', 'discussions', 'events', 'links', 'operations')}

    def list(self, owner, q='', customer_id=None, status='', visibility='active', page=1, page_size=20, opportunity_id=None):
        owner, offset = _owner(owner), _pagination(page, page_size)
        q = _text(q, '搜索', 200).strip().casefold()
        if visibility not in VISIBILITIES or status not in ('', *STATUSES):
            raise ValueError('事项筛选无效。')
        if customer_id is not None:
            customer_id = _identifier(customer_id)
        if opportunity_id is not None:
            opportunity_id = _identifier(opportunity_id)
        with self.crm._lock:
            db = self.crm._db
            scope_where, scope_values = ['owner=?'], [owner]
            if customer_id is not None:
                scope_where.append('customer_id=?')
                scope_values.append(customer_id)
            if opportunity_id is not None:
                scope_where.append('opportunity_id=?')
                scope_values.append(opportunity_id)
            all_items = [self._detail(db, owner, row['id']) for row in db.execute(
                'SELECT id FROM crm_matters WHERE ' + ' AND '.join(scope_where) + ' ORDER BY updated_at DESC,id DESC', scope_values)]
            active = [item for item in all_items if item['visibility'] == 'active']
            items = []
            for item in all_items:
                if item['visibility'] != visibility or (status and item['status'] != status):
                    continue
                texts = [item['title'], item['objective'], item['outcome'], item['customer_name'], item['project_name']]
                texts.extend(str(record.get(key) or '') for record in item['actions'] + item['sources'] for key in ('title', 'content', 'original_content'))
                texts.extend(message['text'] for thread in item['discussions'] for message in thread['messages'])
                if q and not any(q in text.casefold() for text in texts):
                    continue
                items.append(self._summary(item))
            candidates = self._candidates(db, owner)
            candidates = [item for item in candidates if (not customer_id or item['customer_id'] == customer_id)
                          and (not opportunity_id or item['opportunity_id'] == opportunity_id)]
            summary = {'active': len(active), 'archived': sum(i['visibility'] == 'archived' for i in all_items), 'trash': sum(i['visibility'] == 'trash' for i in all_items),
                       'action_count': sum(i['action_count'] for i in active), 'completed_action_count': sum(i['completed_action_count'] for i in active),
                       'schedule_count': len({task['id'] for i in active for task in i['tasks'] if task['status'] == 'pending'})}
            summary.update({s: sum(i['status'] == s for i in active) for s in STATUSES})
            ungrouped_actions = {i for c in candidates for i in c['action_record_ids']}
            ungrouped_plans = {i for c in candidates for i in c.get('plan_ids', [])}
            if customer_id is None and opportunity_id is None:
                ungrouped_records = db.execute("SELECT COUNT(*) FROM crm_records r WHERE r.owner=? AND r.hidden=0 AND NOT EXISTS(SELECT 1 FROM crm_matter_links l WHERE l.owner=r.owner AND l.entity_type='record' AND l.entity_id=r.id)", (owner,)).fetchone()[0]
            else:
                # A shared source may support several project goals. Candidate
                # membership includes that evidence without mixing other projects.
                candidate_records = {identifier for candidate in candidates
                    for field in ('source_record_ids', 'action_record_ids') for identifier in candidate[field]}
                ungrouped_records = 0
                for record in db.execute("SELECT * FROM crm_records r WHERE r.owner=? AND r.hidden=0 AND NOT EXISTS(SELECT 1 FROM crm_matter_links l WHERE l.owner=r.owner AND l.entity_type='record' AND l.entity_id=r.id)", (owner,)):
                    if customer_id and record['customer_id'] != customer_id:
                        continue
                    if opportunity_id and record['id'] not in candidate_records:
                        if not self._exists(db, 'crm_opportunity_links'):
                            continue
                        link = db.execute("SELECT l.*,o.archived FROM crm_opportunity_links l JOIN crm_opportunities o ON o.owner=l.owner AND o.id=l.opportunity_id AND o.customer_id=l.customer_id WHERE l.owner=? AND l.entity_type='record' AND l.entity_id=? AND l.customer_id=?", (owner, record['id'], record['customer_id'])).fetchone()
                        from .sales_workspace import SalesWorkspace
                        if not link or link['archived'] or link['opportunity_id'] != opportunity_id or link['source_snapshot'] != SalesWorkspace._entity_snapshot(record):
                            continue
                    ungrouped_records += 1
            total = len(items)
            return {'items': items[offset:offset + page_size], 'total': total, 'page': page, 'page_size': page_size,
                    'pages': max(1, (total + page_size - 1) // page_size), 'summary': summary,
                    'ungrouped_count': len(candidates), 'ungrouped_action_count': len(ungrouped_actions),
                    'ungrouped_plan_count': len(ungrouped_plans), 'ungrouped_record_count': ungrouped_records}

    def resolve(self, owner, entity_type, entity_id):
        owner, entity_id = _owner(owner), _identifier(entity_id)
        with self.crm._lock:
            db = self.crm._db
            self._entity(db, owner, entity_type, entity_id)
            result = []
            for row in db.execute('SELECT id FROM crm_matters WHERE owner=? ORDER BY updated_at DESC,id DESC', (owner,)):
                _, ids, _, _ = self._entities(db, owner, row['id'])
                if entity_id in ids[entity_type]:
                    result.append(self._summary(self._detail(db, owner, row['id'])))
            return {'items': result}

    def _candidates(self, db, owner):
        grouped = {r[0] for r in db.execute("SELECT entity_id FROM crm_matter_links WHERE owner=? AND entity_type='record' AND role='action'", (owner,))}
        if self._exists(db, 'crm_secretary_plans'):
            grouped.update(r[0] for r in db.execute("SELECT p.record_id FROM crm_secretary_plans p JOIN crm_matter_links l ON l.owner=p.owner AND l.entity_type='plan' AND l.entity_id=p.id WHERE p.owner=?", (owner,)))
        buckets = {}
        for row in db.execute("SELECT * FROM crm_records WHERE owner=? AND hidden=0 AND kind='action' ORDER BY id", (owner,)):
            if row['id'] in grouped:
                continue
            parent = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=? AND hidden=0', (owner, row['parent_record_id'])).fetchone() if row['parent_record_id'] else None
            project = None
            if self._exists(db, 'crm_opportunity_links'):
                link = db.execute("SELECT l.*,o.archived FROM crm_opportunity_links l JOIN crm_opportunities o ON o.owner=l.owner AND o.id=l.opportunity_id AND o.customer_id=l.customer_id WHERE l.owner=? AND l.entity_type='record' AND l.entity_id=? AND l.customer_id=?", (owner, row['id'], row['customer_id'])).fetchone()
                from .sales_workspace import SalesWorkspace
                if link and not link['archived'] and link['source_snapshot'] == SalesWorkspace._entity_snapshot(row):
                    project = link['opportunity_id']
            key = f"source:{parent['id'] if parent else row['id']}:customer:{row['customer_id'] or 0}:project:{project or 0}"
            if key not in buckets:
                buckets[key] = {'key': key, 'title': parent['title'] if parent else row['title'], 'objective': '',
                    'customer_id': row['customer_id'], 'opportunity_id': project, 'source_record_ids': [parent['id']] if parent else [],
                    'action_record_ids': [], 'plan_ids': [], '_rows': [dict(parent)] if parent else []}
            buckets[key]['action_record_ids'].append(row['id'])
            buckets[key]['_rows'].append(dict(row))
        if self._exists(db, 'crm_secretary_plans'):
            for plan in db.execute('SELECT * FROM crm_secretary_plans WHERE owner=? ORDER BY id', (owner,)):
                record = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=? AND hidden=0', (owner, plan['record_id'])).fetchone()
                if not record or record['id'] in grouped or db.execute("SELECT 1 FROM crm_matter_links WHERE owner=? AND entity_type='plan' AND entity_id=?", (owner, plan['id'])).fetchone():
                    continue
                data = json.loads(plan['data_json'])
                customer_id = data.get('customer_id') or record['customer_id']
                if customer_id is not None and (not isinstance(customer_id, int) or isinstance(customer_id, bool) or
                        (record['customer_id'] is not None and record['customer_id'] != customer_id) or
                        not db.execute('SELECT 1 FROM crm_customers WHERE owner=? AND id=?', (owner, customer_id)).fetchone()):
                    customer_id = record['customer_id']
                project_id = data.get('opportunity_id')
                if project_id is not None and (not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0
                        or not self._exists(db, 'crm_opportunities') or
                        not db.execute('SELECT 1 FROM crm_opportunities WHERE owner=? AND id=? AND customer_id=? AND archived=0',
                                       (owner, project_id, customer_id)).fetchone()):
                    project_id = None
                # One original exchange can support several project goals. A
                # plan follows its own scope, never the first shared-source bucket.
                candidate = next((c for c in buckets.values() if c['customer_id'] == customer_id
                    and c['opportunity_id'] == project_id
                    and (record['id'] in c['action_record_ids'] or record['id'] in c['source_record_ids'])), None)
                if candidate is None:
                    key = f"plan:{plan['id']}"
                    candidate = buckets[key] = {'key': key, 'title': data.get('title') or record['title'], 'objective': data.get('objective', ''),
                        'customer_id': customer_id, 'opportunity_id': project_id,
                        'source_record_ids': [record['id']], 'action_record_ids': [], 'plan_ids': [], '_rows': [dict(record)]}
                candidate['plan_ids'].append(plan['id'])
                candidate['_rows'].append(dict(plan))
                if not candidate['objective']:
                    candidate['objective'] = data.get('objective', '')
        result = []
        for candidate in buckets.values():
            rows = candidate.pop('_rows')
            candidate['fingerprint'] = _hash({'candidate': candidate, 'rows': rows})
            candidate['actions'] = [{key: row[key] for key in ('id', 'title', 'status', 'content')} for row in rows if row.get('kind') == 'action' and row['id'] in candidate['action_record_ids']]
            candidate['sources'] = [{key: row[key] for key in ('id', 'title', 'content')} for row in rows if 'kind' in row and row['id'] in candidate['source_record_ids']]
            result.append(candidate)
        return result

    def candidates(self, owner):
        owner = _owner(owner)
        with self.crm._lock:
            return {'items': self._candidates(self.crm._db, owner)}

    def confirm_candidate(self, owner, data):
        owner = _owner(owner)
        self._schema(data, {'key', 'fingerprint', 'title', 'objective', 'action_record_ids', 'request_id'}, {'key', 'fingerprint', 'title', 'request_id'})
        _text(data['key'], '候选标识', 300, required=True)
        _text(data['fingerprint'], '候选版本', 64, required=True)
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'confirm_candidate', data)
            if replay:
                return replay
            candidate = next((item for item in self._candidates(db, owner) if item['key'] == data['key']), None)
            if candidate is None or candidate['fingerprint'] != data['fingerprint']:
                raise RecordConflict('候选来源已有变化，请刷新后重新核对。')
            selected = _ids(data.get('action_record_ids', candidate['action_record_ids']), '步骤')
            if not set(selected) <= set(candidate['action_record_ids']) or (candidate['action_record_ids'] and not selected):
                raise ValueError('请选择这组来源中的有效步骤。')
            payload_data = {key: candidate[key] for key in ('customer_id', 'opportunity_id', 'source_record_ids')}
            payload_data.update(title=data['title'], objective=data.get('objective', candidate['objective']), action_record_ids=selected)
            if set(selected) == set(candidate['action_record_ids']):
                payload_data['plan_ids'] = candidate['plan_ids']
            identifier = self._insert(db, owner, payload_data, _timestamp(self.clock()))
            operation = self._operation(db, owner, 'confirm_candidate', data, identifier, {}, [identifier], payload)
            return self._result(db, owner, identifier, operation)

    def merge(self, owner, identifier, data):
        owner, identifier = _owner(owner), _identifier(identifier)
        self._schema(data, {'target_id', 'expected_revision', 'target_revision', 'request_id'}, {'target_id', 'expected_revision', 'target_revision', 'request_id'})
        target_id = _identifier(data['target_id'])
        if identifier == target_id:
            raise ValueError('不能把事项合并到自身。')
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'merge', data, identifier)
            if replay:
                return replay
            source, target = self._require(db, owner, identifier), self._require(db, owner, target_id)
            self._revision(source, data['expected_revision']); self._revision(target, data['target_revision'])
            if source['visibility'] != 'active' or target['visibility'] != 'active' or target['status'] == 'ended':
                raise RecordConflict('请先恢复需要合并的事项，并核对是否已结束。')
            if source['customer_id'] != target['customer_id'] or source['opportunity_id'] != target['opportunity_id']:
                raise ValueError('不同单位或项目的事项不能直接合并。')
            before = self._snapshot(db, owner, [identifier, target_id])
            links = before[str(identifier)]['links']
            db.execute('DELETE FROM crm_matter_links WHERE owner=? AND matter_id=?', (owner, identifier))
            for link in links:
                db.execute('INSERT OR IGNORE INTO crm_matter_links VALUES (?,?,?,?,?,?)', (owner, target_id, link['entity_type'], link['entity_id'], link['role'], link['created_at']))
            now = _timestamp(self.clock())
            db.execute("UPDATE crm_matters SET visibility='archived',revision=revision+1,updated_at=? WHERE owner=? AND id=?", (now, owner, identifier))
            db.execute('UPDATE crm_matters SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (now, owner, target_id))
            operation = self._operation(db, owner, 'merge', data, target_id, before, [identifier, target_id], payload)
            return self._result(db, owner, target_id, operation, [identifier])

    def split(self, owner, identifier, data):
        owner, identifier = _owner(owner), _identifier(identifier)
        self._schema(data, {'action_record_ids', 'title', 'objective', 'expected_revision', 'request_id'}, {'action_record_ids', 'title', 'expected_revision', 'request_id'})
        selected = _ids(data['action_record_ids'], '步骤')
        if not selected:
            raise ValueError('请选择要拆出的步骤。')
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'split', data, identifier)
            if replay:
                return replay
            source = self._require(db, owner, identifier)
            self._revision(source, data['expected_revision'])
            if source['visibility'] != 'active' or source['status'] == 'ended':
                raise RecordConflict('请先恢复需要拆分的事项。')
            before = self._snapshot(db, owner, [identifier])
            links = before[str(identifier)]['links']
            available = {item['entity_id'] for item in links if item['entity_type'] == 'record' and item['role'] == 'action'}
            if not set(selected) <= available:
                raise ValueError('拆分步骤不属于这个事项。')
            now = _timestamp(self.clock())
            new_id = self._insert(db, owner, {'title': data['title'], 'objective': data.get('objective', ''),
                'customer_id': source['customer_id'], 'opportunity_id': source['opportunity_id']}, now)
            for record_id in selected:
                db.execute("UPDATE crm_matter_links SET matter_id=? WHERE owner=? AND matter_id=? AND entity_type='record' AND entity_id=? AND role='action'", (new_id, owner, identifier, record_id))
            # Evidence may support both goals; actual schedules follow the action's
            # existing proposal history rather than being copied or recreated.
            for link in links:
                if link['entity_type'] == 'record' and link['role'] != 'action':
                    db.execute('INSERT OR IGNORE INTO crm_matter_links VALUES (?,?,?,?,?,?)', (owner, new_id, 'record', link['entity_id'], link['role'], link['created_at']))
            db.execute('UPDATE crm_matters SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (now, owner, identifier))
            operation = self._operation(db, owner, 'split', data, identifier, before, [identifier, new_id], payload)
            return self._result(db, owner, identifier, operation, [new_id])

    def _proposals(self, db, owner, record_ids):
        identifiers = set()
        for record_id in record_ids:
            identifiers.update(r[0] for r in db.execute('SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?', (owner, record_id)))
            row = db.execute('SELECT proposal_id FROM crm_records WHERE owner=? AND id=?', (owner, record_id)).fetchone()
            if row and row['proposal_id']:
                identifiers.add(row['proposal_id'])
        return [dict(p) for i in sorted(identifiers) if (p := db.execute('SELECT * FROM proposals WHERE owner=? AND id=?', (owner, i)).fetchone())]

    def _lifecycle_preview(self, db, owner, identifier):
        detail = self._detail(db, owner, identifier)
        _, ids, _, _ = self._entities(db, owner, identifier)
        pending = [dict(task) for task in detail['tasks'] if task['status'] == 'pending']
        shared = {}
        for task in pending:
            others = []
            for row in db.execute("SELECT id FROM crm_matters WHERE owner=? AND id!=? AND visibility='active'", (owner, identifier)):
                _, ids, _, _ = self._entities(db, owner, row['id'])
                if task['id'] in ids['task']:
                    others.append(row['id'])
            shared[str(task['id'])] = others
            task['shared_matter_ids'] = others
            task['shared_active_record_ids'] = [r['id'] for r in db.execute('''SELECT DISTINCT r.id FROM crm_records r
                WHERE r.owner=? AND r.hidden=0 AND (r.proposal_id IN(SELECT id FROM proposals WHERE owner=? AND (task_id=? OR target_task_id=?))
                OR r.id IN(SELECT h.record_id FROM crm_record_proposals h JOIN proposals p ON p.owner=h.owner AND p.id=h.proposal_id
                WHERE h.owner=? AND (p.task_id=? OR p.target_task_id=?)))''', (owner, owner, task['id'], task['id'], owner, task['id'], task['id'])) if r['id'] not in ids['record']]
        proposals = self._proposals(db, owner, ids['record'])
        shared_proposals = set()
        pending_ids = {p['id'] for p in proposals if p['status'] == 'pending'}
        for row in db.execute("SELECT id FROM crm_matters WHERE owner=? AND id!=? AND visibility='active'", (owner, identifier)):
            _, other_ids, _, _ = self._entities(db, owner, row['id'])
            shared_proposals.update(p['id'] for p in self._proposals(db, owner, other_ids['record']) if p['id'] in pending_ids)
        return {'matter_id': identifier, 'revision': detail['revision'], 'visibility': detail['visibility'],
                'pending_tasks': pending, 'pending_task_count': len(pending),
                'shared_tasks': sum(bool(t['shared_matter_ids'] or t['shared_active_record_ids']) for t in pending),
                'shared_proposal_ids': sorted(shared_proposals),
                'pending_proposal_count': sum(p['status'] == 'pending' for p in proposals),
                'snapshot': _hash({'revision': detail['revision'], 'tasks': pending, 'proposals': proposals, 'links': detail['links']}),
                'restore_message': '恢复事项不会重新启动已停止的提醒。'}

    def lifecycle_preview(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            return self._lifecycle_preview(self.crm._db, owner, identifier)

    def lifecycle(self, owner, identifier, data):
        owner, identifier = _owner(owner), _identifier(identifier)
        self._schema(data, {'visibility', 'expected_revision', 'reminder_action', 'snapshot', 'request_id'}, {'visibility', 'expected_revision', 'reminder_action', 'request_id'})
        if data['visibility'] not in VISIBILITIES or data['reminder_action'] not in ('keep', 'cancel'):
            raise ValueError('归档或提醒操作无效。')
        if data['visibility'] == 'active' and data['reminder_action'] != 'keep':
            raise ValueError('恢复事项不能重新操作提醒。')
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'lifecycle', data, identifier)
            if replay:
                return replay
            row = self._require(db, owner, identifier)
            self._revision(row, data['expected_revision'])
            preview = self._lifecycle_preview(db, owner, identifier)
            before = self._snapshot(db, owner, [identifier])
            effects = {'cancelled_tasks': 0, 'rejected_proposals': 0, 'reminders_restarted': False}
            if data['reminder_action'] == 'cancel':
                if data.get('snapshot') != preview['snapshot']:
                    raise RecordConflict('安排已有变化，请刷新归档影响后核对。')
                if preview['shared_tasks'] or preview['shared_proposal_ids']:
                    raise ValueError('安排还被其他有效事项共享，请保留提醒或单独核对这次安排。')
                for task in preview['pending_tasks']:
                    self.crm._execute(db, owner, {'action': 'cancel', 'task_id': task['id']}, _timestamp(self.clock()))
                    effects['cancelled_tasks'] += 1
                _, ids, _, _ = self._entities(db, owner, identifier)
                for proposal in self._proposals(db, owner, ids['record']):
                    effects['rejected_proposals'] += db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND id=? AND status='pending'", (_timestamp(self.clock()), owner, proposal['id'])).rowcount
            db.execute('UPDATE crm_matters SET visibility=?,revision=revision+1,updated_at=? WHERE owner=? AND id=?', (data['visibility'], _timestamp(self.clock()), owner, identifier))
            operation = self._operation(db, owner, 'lifecycle', data, identifier, before, [identifier], payload)
            return self._result(db, owner, identifier, operation, effects=effects)

    def undo(self, owner, operation_id, data):
        owner, operation_id = _owner(owner), _identifier(operation_id)
        self._schema(data, {'expected_revision', 'request_id'}, {'expected_revision', 'request_id'})
        with self._transaction() as db:
            replay, payload = self._replay(db, owner, 'undo', data, operation_id)
            if replay:
                return replay
            operation = db.execute('SELECT * FROM crm_matter_operations WHERE owner=? AND id=?', (owner, operation_id)).fetchone()
            if operation is None:
                raise KeyError('未找到你的归组操作。')
            if operation['undone'] or operation['operation'] == 'undo':
                raise RecordConflict('这项操作已经撤销或不能再次撤销。')
            self._revision(self._require(db, owner, operation['matter_id']), data['expected_revision'])
            before, after = json.loads(operation['before_json']), json.loads(operation['after_json'])
            affected = [int(i) for i in after]
            current = self._snapshot(db, owner, affected)
            for i, snapshot in after.items():
                if current[i]['matter']['revision'] != snapshot['matter']['revision']:
                    raise RecordConflict('关联事项已有新进展，不能直接撤销旧操作。')
            now = _timestamp(self.clock())
            for i in affected:
                db.execute('DELETE FROM crm_matter_links WHERE owner=? AND matter_id=?', (owner, i))
            for i in affected:
                saved = before.get(str(i))
                if not saved:
                    db.execute("UPDATE crm_matters SET visibility='archived',revision=revision+1,updated_at=? WHERE owner=? AND id=?", (now, owner, i))
                    continue
                m = saved['matter']
                db.execute('UPDATE crm_matters SET title=?,objective=?,status=?,outcome=?,visibility=?,customer_id=?,opportunity_id=?,revision=revision+1,updated_at=? WHERE owner=? AND id=?',
                           (m['title'], m['objective'], m['status'], m['outcome'], m['visibility'], m['customer_id'], m['opportunity_id'], now, owner, i))
                for link in saved['links']:
                    db.execute('INSERT INTO crm_matter_links VALUES (?,?,?,?,?,?)', (owner, i, link['entity_type'], link['entity_id'], link['role'], link['created_at']))
            db.execute('UPDATE crm_matter_operations SET undone=1 WHERE owner=? AND id=?', (owner, operation_id))
            identifier = operation['matter_id']
            undo_id = self._operation(db, owner, 'undo', data, identifier, current, affected, payload, undo_of=operation_id)
            return self._result(db, owner, identifier, undo_id, affected, effects={'reminders_restarted': False})
