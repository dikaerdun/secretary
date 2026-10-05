"""User-managed, recoverable visibility without destroying source evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .crm import RecordConflict, _identifier, _owner, _pagination, _public, _text
from .store import _timestamp


class RecordLifecycle:
    def __init__(self, crm, clock=time.time):
        self.crm, self.clock = crm, clock
        with crm._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS crm_record_lifecycle(
                owner TEXT NOT NULL,record_id INTEGER NOT NULL,root_record_id INTEGER NOT NULL,
                visibility TEXT NOT NULL CHECK(visibility IN ('active','archived','trash')),
                revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(owner,record_id),
                FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id),
                FOREIGN KEY(owner,root_record_id) REFERENCES crm_records(owner,id))''')
            db.execute('CREATE INDEX IF NOT EXISTS record_lifecycle_roots ON crm_record_lifecycle(owner,visibility,root_record_id)')

    @staticmethod
    def _exists(db, table):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def _group(self, db, owner, identifier):
        requested = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=?', (owner, identifier)).fetchone()
        if requested is None:
            raise KeyError('未找到你的记录。')
        managed = db.execute('SELECT * FROM crm_record_lifecycle WHERE owner=? AND record_id=?', (owner, identifier)).fetchone()
        if requested['hidden'] and (not managed or managed['visibility'] == 'active'):
            raise KeyError('未找到你的可恢复记录。')
        plan = None
        root_id = managed['root_record_id'] if managed else identifier
        if self._exists(db, 'crm_secretary_plans') and self._exists(db, 'crm_secretary_turns'):
            plan = db.execute('''SELECT p.* FROM crm_secretary_plans p WHERE p.owner=? AND
                (p.record_id=? OR p.id IN (SELECT plan_id FROM crm_secretary_turns WHERE owner=? AND record_id=?))
                ORDER BY p.id DESC LIMIT 1''', (owner, identifier, owner, identifier)).fetchone()
            if plan:
                root_id = plan['record_id']
        root = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=?', (owner, root_id)).fetchone()
        life = db.execute('SELECT * FROM crm_record_lifecycle WHERE owner=? AND record_id=?', (owner, root_id)).fetchone()
        if root is None or (root['hidden'] and (not life or life['visibility'] == 'active')):
            raise KeyError('未找到你的可恢复记录。')
        ids = {root_id}
        ids.update(row['record_id'] for row in db.execute(
            'SELECT record_id FROM crm_record_lifecycle WHERE owner=? AND root_record_id=?', (owner, root_id)))
        if plan:
            ids.update(row['record_id'] for row in db.execute(
                'SELECT record_id FROM crm_secretary_turns WHERE owner=? AND plan_id=?', (owner, plan['id'])))
        records, lives = [], {}
        for record_id in sorted(ids):
            row = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=?', (owner, record_id)).fetchone()
            state = db.execute('SELECT * FROM crm_record_lifecycle WHERE owner=? AND record_id=?', (owner, record_id)).fetchone()
            # Internally hidden commands were never hidden by this module.
            if row is None or (row['hidden'] and (not state or state['visibility'] == 'active')):
                continue
            if state and state['root_record_id'] != root_id:
                raise RecordConflict('记录归档分组已有变化，请刷新核对。')
            records.append(row)
            if state:
                lives[row['id']] = state
        return root, records, lives, plan

    def _related(self, db, owner, records):
        ids = [row['id'] for row in records]
        marks = ','.join('?' for _ in ids)
        proposals = db.execute('''SELECT p.* FROM proposals p WHERE p.owner=? AND
            (p.id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id IN (''' + marks + '''))
            OR p.id IN (SELECT proposal_id FROM crm_records WHERE owner=? AND id IN (''' + marks + '))) ORDER BY p.id',
            [owner, owner, *ids, owner, *ids]).fetchall()
        task_ids = sorted({row[key] for row in proposals for key in ('task_id', 'target_task_id') if row[key]})
        tasks, shared = [], {}
        if task_ids:
            tasks = db.execute('SELECT * FROM tasks WHERE owner=? AND id IN (' +
                ','.join('?' for _ in task_ids) + ') ORDER BY id', [owner, *task_ids]).fetchall()
        for task in tasks:
            if task['status'] != 'pending':
                continue
            others = db.execute('''SELECT DISTINCT r.id,r.updated_at FROM crm_records r
                WHERE r.owner=? AND r.hidden=0 AND r.id NOT IN (''' + marks + ''') AND
                (r.proposal_id IN (SELECT id FROM proposals WHERE owner=? AND (task_id=? OR target_task_id=?))
                OR r.id IN (SELECT h.record_id FROM crm_record_proposals h JOIN proposals p
                    ON p.owner=h.owner AND p.id=h.proposal_id WHERE h.owner=? AND (p.task_id=? OR p.target_task_id=?)))
                ORDER BY r.id''', [owner, *ids, owner, task['id'], task['id'], owner, task['id'], task['id']]).fetchall()
            shared[task['id']] = [dict(row) for row in others]
        return proposals, tasks, shared

    def _preview(self, db, owner, identifier):
        root, records, lives, plan = self._group(db, owner, identifier)
        proposals, tasks, shared = self._related(db, owner, records)
        life = lives.get(root['id'])
        visibility = life['visibility'] if life else 'active'
        signature = {
            'records': [[row['id'], row['updated_at'], row['hidden']] for row in records],
            'lifecycle': [dict(row) for _, row in sorted(lives.items())],
            'plan': [plan['id'], plan['revision']] if plan else None,
            'tasks': [dict(row) for row in tasks], 'proposals': [dict(row) for row in proposals],
            'shared': shared,
        }
        snapshot = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        pending = [{**{key: row[key] for key in ('id', 'title', 'status', 'revision', 'remind_at')},
            'shared_active_record_ids': [record['id'] for record in shared.get(row['id'], [])]}
            for row in tasks if row['status'] == 'pending']
        shared_count = sum(bool(row['shared_active_record_ids']) for row in pending)
        public_record = _public(db.execute(self.crm._RECORD_SELECT + 'WHERE r.owner=? AND r.id=?',
            (owner, root['id'])).fetchone())
        if plan and self._exists(db, 'crm_contacts'):
            contact_id = json.loads(plan['data_json']).get('contact_id')
            contact = db.execute('SELECT id,name FROM crm_contacts WHERE owner=? AND customer_id=? AND id=?',
                (owner, root['customer_id'], contact_id)).fetchone() if type(contact_id) is int else None
            if contact:
                public_record.update(contact_id=contact['id'], contact_name=contact['name'])
        result = {'record': public_record, 'root_record_id': root['id'], 'visibility': visibility,
            'revision': life['revision'] if life else 0, 'snapshot': snapshot, 'record_count': len(records),
            'pending_tasks': pending, 'pending_task_count': len(pending),
            'pending_proposal_count': sum(row['status'] == 'pending' for row in proposals),
            'shared_tasks': shared_count,
            'shared_task_message': '关联提醒还被其他有效记录共享，请先单独核对安排。' if shared_count else ''}
        return result, records, lives, proposals

    def preview(self, owner, identifier):
        owner, identifier = _owner(owner), _identifier(identifier)
        with self.crm._lock:
            return self._preview(self.crm._db, owner, identifier)[0]

    def move(self, owner, identifier, data):
        owner, identifier = _owner(owner), _identifier(identifier)
        if not isinstance(data, dict) or set(data) != {'action', 'snapshot'}:
            raise ValueError('归档操作字段无效。')
        action, expected = data['action'], data['snapshot']
        if action not in ('archive', 'trash', 'restore') or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('归档操作或记录快照无效。')
        target = {'archive': 'archived', 'trash': 'trash', 'restore': 'active'}[action]
        now = _timestamp(self.clock())
        effects = dict(cancelled_tasks=0, rejected_proposals=0, hidden_records=0, restored_records=0)
        with self.crm._transaction() as db:
            current, records, lives, proposals = self._preview(db, owner, identifier)
            if action == 'restore' and not lives.get(current['root_record_id']):
                raise ValueError('只能恢复你已归档或放入回收站的记录。')
            if target != 'active' and current['shared_tasks']:
                raise ValueError(current['shared_task_message'])
            if current['visibility'] == target:
                return {**current, 'changed': False, 'effects': effects, 'message': '记录已处于该状态。'}
            if current['snapshot'] != expected:
                raise RecordConflict('记录或关联安排已有变化，请刷新核对后重试。')
            if target != 'active':
                effects['rejected_proposals'] = current['pending_proposal_count']
                for task in current['pending_tasks']:
                    self.crm._execute(db, owner, {'action': 'cancel', 'task_id': task['id']}, now)
                    effects['cancelled_tasks'] += 1
                for proposal in proposals:
                    db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND id=? AND status='pending'",
                               (now, owner, proposal['id']))
            for record in records:
                state = lives.get(record['id'])
                if target == 'active' and (not state or state['visibility'] == 'active'):
                    continue
                revision = state['revision'] + 1 if state else 1
                changed_at = max(now, state['updated_at'] + .000001) if state else now
                db.execute('''INSERT INTO crm_record_lifecycle VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(owner,record_id) DO UPDATE SET root_record_id=excluded.root_record_id,
                    visibility=excluded.visibility,revision=excluded.revision,updated_at=excluded.updated_at''',
                    (owner, record['id'], current['root_record_id'], target, revision,
                     state['created_at'] if state else now, changed_at))
                hidden = int(target != 'active')
                if record['hidden'] != hidden:
                    effects['hidden_records' if hidden else 'restored_records'] += 1
                db.execute('UPDATE crm_records SET hidden=? WHERE owner=? AND id=?', (hidden, owner, record['id']))
                flow = getattr(self.crm, 'secretary_flow', None)
                if hidden and flow and getattr(flow, 'arrangements', None):
                    flow.arrangements.source_hidden(db, owner, record['id'], now)
                if hidden and self._exists(db, 'crm_secretary_turns'):
                    # Never allow an in-flight model result to revive a restored source.
                    db.execute("""UPDATE crm_secretary_turns SET status='needs_attention',error=?,
                        lease=NULL,claimed_at=NULL,updated_at=? WHERE owner=? AND record_id=?
                        AND status IN ('queued','processing')""",
                        ('这条记录已收起，已停止整理；恢复后可明确重试。', now, owner, record['id']))
            result = self._preview(db, owner, current['root_record_id'])[0]
        message = {'archive': '已归档，关联待执行提醒已停止；可恢复记录。',
                   'trash': '已移入回收站，关联待执行提醒已停止；原文保留。',
                   'restore': '记录已恢复，提醒保持停止；如需继续，请重新安排。'}[action]
        return {**result, 'changed': True, 'effects': effects, 'message': message}

    def list(self, owner, visibility='archived', q='', page=1, page_size=50):
        owner = _owner(owner)
        offset = _pagination(page, page_size)
        q = _text(q, '搜索内容', 200).strip()
        if visibility not in ('archived', 'trash'):
            raise ValueError('请选择归档或回收站。')
        where = 'l.owner=? AND l.visibility=? AND l.record_id=l.root_record_id AND r.hidden=1'
        values = [owner, visibility]
        if q:
            where += ' AND (instr(r.title,?)>0 OR instr(r.content,?)>0)'
            values.extend([q, q])
        join = ' FROM crm_record_lifecycle l JOIN crm_records r ON r.owner=l.owner AND r.id=l.record_id WHERE '
        with self.crm._lock:
            db = self.crm._db
            total = db.execute('SELECT COUNT(*)' + join + where, values).fetchone()[0]
            rows = db.execute('SELECT l.record_id' + join + where +
                ' ORDER BY l.updated_at DESC,l.record_id DESC LIMIT ? OFFSET ?', [*values, page_size, offset]).fetchall()
            items = [self._preview(db, owner, row['record_id'])[0] for row in rows]
        return {'items': items, 'total': total, 'page': page, 'pages': max(1, (total + page_size - 1) // page_size)}
