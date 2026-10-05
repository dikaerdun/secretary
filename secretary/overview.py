"""Read-only personal work and explicit-project overview with untruncated counts."""
from __future__ import annotations

from datetime import date, datetime
import json
import time

from .agenda import _window
from .crm import _owner, _identifier, _pagination, _text
from .sales_workspace import _signature
from .store import SHANGHAI, _timestamp
from .progress_workspace import _task_overlaps


_SNAPSHOT_FIELDS = ('id', 'customer_id', 'title', 'content', 'original_content',
                    'revision', 'current_version_id', 'occurred_at', 'hidden')
_ENTITY_TABLES = {'record': 'crm_records', 'material': 'crm_materials', 'visit': 'crm_visits'}


class OverviewService:
    def __init__(self, crm, workspace, review_queue=None, clock=time.time):
        self.crm, self.workspace = crm, workspace
        self.review_queue, self.clock = review_queue, clock

    def _entities(self, db, owner, kind):
        """Bulk-load snapshot fields only for linked sources and visit dependencies."""
        table = _ENTITY_TABLES[kind]
        if not self.workspace._exists(db, table):
            return {}
        present = {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
        fields = [name for name in _SNAPSHOT_FIELDS if name in present]
        where = ('EXISTS (SELECT 1 FROM crm_opportunity_links l WHERE l.owner=e.owner '
                 'AND l.entity_type=? AND l.entity_id=e.id AND l.opportunity_id IS NOT NULL)')
        params = [owner, kind]
        relation, foreign_key = (('crm_visit_sources', 'material_id') if kind == 'material'
                                 else ('crm_visit_records', 'record_id') if kind == 'record' else (None, None))
        if relation and self.workspace._exists(db, relation):
            where += (' OR EXISTS (SELECT 1 FROM '+relation+' s JOIN crm_opportunity_links l '
                      "ON l.owner=s.owner AND l.entity_type='visit' AND l.entity_id=s.visit_id "
                      'WHERE s.owner=e.owner AND s.'+foreign_key+'=e.id AND l.opportunity_id IS NOT NULL)')
        rows = db.execute('SELECT '+','.join('e.'+field for field in fields)+' FROM '+table+
                          ' e WHERE e.owner=? AND ('+where+')', params).fetchall()
        return {row['id']: dict(row) for row in rows}

    def _links(self, db, owner, projects):
        """Match existing workspace fingerprints without a per-record detail query."""
        links = db.execute('SELECT entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot '
                           'FROM crm_opportunity_links WHERE owner=? AND opportunity_id IS NOT NULL '
                           'ORDER BY entity_type,entity_id', (owner,)).fetchall()
        if not links:
            return {}, []
        entities = {kind: self._entities(db, owner, kind) for kind in _ENTITY_TABLES}
        sources, records, choices = {}, {}, {}
        for table, target, fields, order in (
            ('crm_visit_sources', sources, 'material_id,role', 'material_id'),
            ('crm_visit_records', records, 'record_id', 'record_id'),
            ('crm_visit_source_choices', choices, 'material_id,use_status,revision', 'material_id'),
        ):
            if not self.workspace._exists(db, table):
                continue
            rows = db.execute('SELECT s.visit_id,'+','.join('s.'+field for field in fields.split(','))+
                ' FROM '+table+' s WHERE s.owner=? AND EXISTS (SELECT 1 FROM crm_opportunity_links l '
                "WHERE l.owner=s.owner AND l.entity_type='visit' AND l.entity_id=s.visit_id "
                'AND l.opportunity_id IS NOT NULL) ORDER BY s.visit_id,s.'+order, (owner,)).fetchall()
            for row in rows:
                target.setdefault(row['visit_id'], []).append({field: row[field] for field in fields.split(',')})
        def snapshot(kind, identifier):
            source = entities[kind].get(identifier)
            if source is None or source.get('hidden'):
                return 'missing'
            if kind != 'visit':
                return self.workspace._entity_snapshot(source)
            state = {'visit': self.workspace._entity_snapshot(source), 'materials': [], 'records': [],
                     'choices': choices.get(identifier, [])}
            for relation in sources.get(identifier, []):
                state['materials'].append([relation, snapshot('material', relation['material_id'])])
            for relation in records.get(identifier, []):
                state['records'].append([relation['record_id'], snapshot('record', relation['record_id'])])
            return _signature(state)
        valid_records, stale = {}, []
        for row in links:
            source = entities.get(row['entity_type'], {}).get(row['entity_id'])
            project = projects.get(row['opportunity_id'])
            reason = None
            if project is None:
                reason = '关联项目已归档或不再可用，请重新核对。'
            elif source is None or source.get('hidden'):
                reason = '来源已隐藏或不再可用，请重新核对。'
            elif source.get('customer_id') != row['customer_id'] or project['customer_id'] != row['customer_id']:
                reason = '来源的客户归属已变化，请重新核对项目。'
            elif snapshot(row['entity_type'], row['entity_id']) != row['source_snapshot']:
                reason = '来源内容已变化，请重新核对项目关联。'
            if reason:
                stale.append({key: row[key] for key in ('entity_type', 'entity_id', 'customer_id', 'opportunity_id', 'revision')}
                             | {'stale': True, 'reason': reason})
            elif row['entity_type'] == 'record':
                valid_records[row['entity_id']] = row['opportunity_id']
        return valid_records, stale

    @staticmethod
    def _overdue(record, now, today):
        reasons = []
        if record['task_status'] == 'pending' and record['remind_at'] is not None and record['remind_at'] < now:
            reasons.append('已确认日程时间已过，尚未记录完成。')
        terms = record['action_terms']
        for kind, label in (('deadline', '截止'), ('check', '回访／检查')):
            stamp = terms.get(kind+'_at')
            if stamp is not None:
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp < now:
                    reasons.append(label+'时间已过，请核对进展。')
                continue
            value = terms.get(kind+'_date')
            try:
                day = date.fromisoformat(value)
            except (ValueError, TypeError):
                continue
            if day < today:
                reasons.append(f'{label}日期 {day.isoformat()} 已过，原话未指定具体时刻。')
        return ' '.join(reasons)

    def _reviews(self, db, owner):
        if self.review_queue is not None:
            return [item for item in self.review_queue.all_items(owner)
                    if item.get('decision_state', 'pending') == 'pending']
        rows = db.execute('''SELECT p.id,p.title,p.remind_at,p.updated_at,p.change_kind,
            rp.record_id,r.customer_id,c.name AS customer_name
            FROM proposals p LEFT JOIN crm_record_proposals rp ON rp.owner=p.owner AND rp.proposal_id=p.id
            LEFT JOIN crm_records r ON r.owner=rp.owner AND r.id=rp.record_id
            LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id
            WHERE p.owner=? AND p.status='pending' AND (r.id IS NULL OR r.hidden=0) ORDER BY p.id''', (owner,)).fetchall()
        return [{**dict(row), 'key': 'proposal:P'+str(row['id']), 'review_type': 'proposal',
                 'source_type': 'proposal', 'decision_state': 'pending'} for row in rows]

    def _collect(self, owner):
        owner, now = _owner(owner), _timestamp(self.clock())
        current = datetime.fromtimestamp(now, SHANGHAI)
        start, end = (value.timestamp() for value in _window('day', current))
        with self.crm._lock:
            db = self.crm._db
            project_rows = db.execute('''SELECT o.id,o.customer_id,c.name AS customer_name,o.name,o.stage,
                o.amount_cents,o.amount_type,o.approval,o.blockers,o.scope FROM crm_opportunities o
                JOIN crm_customers c ON c.owner=o.owner AND c.id=o.customer_id
                WHERE o.owner=? AND o.archived=0 ORDER BY o.updated_at DESC,o.id DESC''', (owner,)).fetchall()
            projects = {row['id']: {**dict(row), 'opportunity_id': row['id'], 'type': row['amount_type'],
                'open_actions': 0, 'waiting': 0, 'progress': {'done': 0, 'total': 0},
                'next_schedule': None, 'needs_time': 0} for row in project_rows}
            record_projects, stale_links = self._links(db, owner, projects)
            # Full owner-scoped counts use a single thin SELECT, not paged CRM results.
            rows = db.execute('''SELECT r.id,r.title,substr(r.content,1,500) AS content,r.kind,r.status,r.category,
                r.customer_id,c.name AS customer_name,r.created_at,r.updated_at,r.proposal_id,
                p.status AS proposal_status,p.remind_at AS proposal_remind_at,
                t.id AS task_id,t.status AS task_status,t.title AS task_title,t.remind_at AS task_remind_at,a.terms_json
                FROM crm_records r LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id
                LEFT JOIN proposals p ON p.owner=r.owner AND p.id=r.proposal_id
                LEFT JOIN tasks t ON t.owner=r.owner AND t.id=COALESCE(p.task_id,p.target_task_id)
                LEFT JOIN crm_action_terms a ON a.owner=r.owner AND a.record_id=r.id
                WHERE r.owner=? AND r.hidden=0 ORDER BY r.updated_at DESC,r.id DESC''', (owner,)).fetchall()
            my_actions, waiting, overdue, unfiled, records = [], [], [], [], {}
            feedback = {}
            if self.workspace._exists(db, 'crm_progress_feedback'):
                feedback = {row['record_id']: row['decision'] for row in db.execute(
                    'SELECT record_id,decision FROM crm_progress_feedback WHERE owner=? ORDER BY created_at,run_id,item_id', (owner,))}
            for row in rows:
                record = {key: row[key] for key in row.keys() if key not in ('terms_json', 'task_remind_at')}
                terms = json.loads(row['terms_json']) if row['terms_json'] else {}
                record['action_terms'] = terms if isinstance(terms, dict) else {}
                record['record_id'] = record['id']
                record['remind_at'] = row['task_remind_at'] if row['task_status'] == 'pending' else None
                record['schedule_confirmed'] = row['task_status'] == 'pending'
                record['opportunity_id'] = record_projects.get(row['id'])
                record['opportunity_name'] = projects[record['opportunity_id']]['name'] if record['opportunity_id'] else None
                record['opportunity_scope'] = projects[record['opportunity_id']]['scope'] if record['opportunity_id'] else None
                record['latest_feedback_decision'] = feedback.get(record['id'])
                executor = record['action_terms'].get('executor_kind', 'unknown')
                is_waiting = executor in ('customer', 'team') or feedback.get(record['id']) == 'waiting'
                # Completing an appointment does not record the follow-up result.
                is_done = record['status'] == 'done'
                is_action = record['kind'] == 'action'
                timed_pending = record['proposal_status'] == 'pending' and record['proposal_remind_at'] is not None
                record['needs_time'] = bool(is_action and not is_done and not is_waiting
                                            and not record['schedule_confirmed'] and not timed_pending)
                records[record['id']] = record
                if record['status'] == 'unfiled' and not is_done:
                    unfiled.append(record)
                if is_action:
                    project = projects.get(record['opportunity_id'])
                    if project is not None:
                        project['progress']['total'] += 1
                        if is_done:
                            project['progress']['done'] += 1
                        else:
                            project['open_actions'] += 1
                            project['waiting'] += int(is_waiting)
                            project['needs_time'] += int(record['needs_time'])
                    if not is_done:
                        (waiting if is_waiting else my_actions).append(record)
                        reason = self._overdue(record, now, current.date())
                        if reason:
                            overdue.append({**record, 'overdue_reason': reason})
                        if project is not None and record['schedule_confirmed'] and record['remind_at'] is not None and record['remind_at'] >= now:
                            candidate = {'task_id': record['task_id'], 'title': record['task_title'], 'remind_at': record['remind_at']}
                            if project['next_schedule'] is None or candidate['remind_at'] < project['next_schedule']['remind_at']:
                                project['next_schedule'] = candidate
            task_records = {record['task_id']: record for record in records.values() if record['task_id'] is not None}
            # A historical proposal may still point at the effective original task.
            # Such tasks have a source; only genuinely unlinked legacy tasks are orphans.
            task_relations = db.execute('''SELECT COALESCE(p.task_id,p.target_task_id) AS task_id,r.id AS record_id
                FROM proposals p LEFT JOIN crm_record_proposals rp ON rp.owner=p.owner AND rp.proposal_id=p.id
                JOIN crm_records r ON r.owner=p.owner AND (r.id=rp.record_id OR r.proposal_id=p.id)
                WHERE p.owner=? AND r.hidden=0 AND COALESCE(p.task_id,p.target_task_id) IS NOT NULL
                ORDER BY p.updated_at DESC,p.id DESC,r.id''', (owner,)).fetchall()
            for relation in task_relations:
                if relation['task_id'] not in task_records and relation['record_id'] in records:
                    task_records[relation['task_id']] = records[relation['record_id']]
            tasks = db.execute('''SELECT id,title,remind_at,status,duration_minutes,deadline_at,revision FROM tasks
                WHERE owner=? AND status='pending' ORDER BY remind_at,id''', (owner,)).fetchall()
            today_schedules, orphan_tasks = [], []
            overdue_ids = {record['id'] for record in overdue}
            for row in tasks:
                record = task_records.get(row['id'])
                if record is not None and not record['schedule_confirmed'] and record['status'] != 'done':
                    # A new pending proposal cannot hide an original effective
                    # schedule that remains connected through proposal history.
                    project = projects.get(record['opportunity_id'])
                    if project is not None and record['needs_time']:
                        project['needs_time'] -= 1
                    record.update(task_id=row['id'], task_status='pending', task_title=row['title'],
                                  remind_at=row['remind_at'], schedule_confirmed=True, needs_time=False)
                    if record['kind'] == 'action':
                        reason = self._overdue(record, now, current.date())
                        if reason and record['id'] not in overdue_ids:
                            overdue.append({**record, 'overdue_reason': reason})
                            overdue_ids.add(record['id'])
                        if project is not None and row['remind_at'] is not None and row['remind_at'] >= now:
                            candidate = {'task_id': row['id'], 'title': row['title'], 'remind_at': row['remind_at']}
                            if project['next_schedule'] is None or candidate['remind_at'] < project['next_schedule']['remind_at']:
                                project['next_schedule'] = candidate
                summary = {**dict(row), 'task_id': row['id'],
                    'record_id': record['id'] if record else None, 'customer_id': record['customer_id'] if record else None,
                    'customer_name': record['customer_name'] if record else None,
                    'opportunity_id': record['opportunity_id'] if record else None,
                    'opportunity_name': record['opportunity_name'] if record else None,
                    'opportunity_scope': record['opportunity_scope'] if record else None}
                if _task_overlaps(dict(row), start, end):
                    today_schedules.append(summary)
                if record is None:
                    orphan_tasks.append({**summary, 'is_overdue': row['remind_at'] is not None and row['remind_at'] < now})
            reviews = self._reviews(db, owner)
        lists = {'my_actions': my_actions, 'waiting_actions': waiting, 'overdue': overdue, 'unfiled': unfiled,
                 'review_pending': reviews, 'today_schedules': today_schedules,
                 'projects': list(projects.values()), 'stale_links': stale_links, 'orphan_tasks': orphan_tasks}
        counts = {key: len(value) for key, value in lists.items() if key not in ('projects', 'stale_links', 'orphan_tasks')}
        counts['active_projects'] = sum(project['stage'] not in ('won', 'lost') for project in projects.values())
        counts['orphan_overdue'] = sum(task['is_overdue'] for task in orphan_tasks)
        orphan_summary = {'pending_total': len(orphan_tasks), 'overdue': counts['orphan_overdue'],
            'today': sum(_task_overlaps(task, start, end) for task in orphan_tasks),
            'unscheduled': sum(task['remind_at'] is None for task in orphan_tasks)}
        return {'counts': counts, **lists,
                'totals': {'projects': len(projects), 'stale_links': len(stale_links), 'orphan_tasks': len(orphan_tasks)},
                'orphan_task_total': len(orphan_tasks), 'orphan_task_summary': orphan_summary, 'generated_at': now,
                'date': current.date().isoformat(), 'timezone': 'Asia/Shanghai'}

    def get(self, owner, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('总览每组最多显示1至200条')
        result = self._collect(owner)
        keys = ('my_actions', 'waiting_actions', 'overdue', 'unfiled', 'review_pending',
                'today_schedules', 'projects', 'stale_links', 'orphan_tasks')
        return {**result, **{key: result[key][:limit] for key in keys}, 'limit': limit,
                'truncated': {key: len(result[key]) > limit for key in keys}}

    def queue(self, owner, work_queue, *, q='', customer_id=None, page=1, page_size=20):
        labels = {'my_actions': '我的行动', 'waiting_actions': '等待反馈', 'overdue': '逾期行动'}
        if work_queue not in labels:
            raise ValueError('工作队列无效')
        owner, q = _owner(owner), _text(q, '搜索内容', 500).strip()
        offset = _pagination(page, page_size)
        with self.crm._lock:
            if customer_id is not None:
                customer_id = _identifier(customer_id)
                self.crm._require_customer(self.crm._db, owner, customer_id)
            result = self._collect(owner)
            rows = result[work_queue]
            base_total = len(rows)
            if customer_id is not None:
                rows = [row for row in rows if row['customer_id'] == customer_id]
            if q:
                # Search the complete owned source before paging, not its card excerpt.
                escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                ids = {row['id'] for row in self.crm._db.execute(
                    "SELECT r.id FROM crm_records r LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id WHERE r.owner=? AND r.hidden=0 AND (r.title LIKE ? ESCAPE '\\' OR r.content LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\')",
                    (owner, '%'+escaped+'%', '%'+escaped+'%', '%'+escaped+'%'))}
                rows = [row for row in rows if row['id'] in ids]
            total = len(rows)
            return {'items': rows[offset:offset+page_size], 'total': total, 'page': page,
                    'page_size': page_size, 'pages': (total+page_size-1)//page_size,
                    'work_queue': work_queue, 'queue_label': labels[work_queue], 'base_total': base_total,
                    'filters': {'q': q, 'customer_id': customer_id},
                    **{key: result[key] for key in ('generated_at', 'date', 'timezone')}}

    def project_context(self, owner, record_ids):
        """Calendar metadata from the same bulk, validated links as the overview."""
        owner = _owner(owner)
        with self.crm._lock:
            db = self.crm._db
            projects = {row['id']: dict(row) for row in db.execute(
                'SELECT id,customer_id,name,scope FROM crm_opportunities WHERE owner=? AND archived=0', (owner,))}
            valid, stale = self._links(db, owner, projects)
            invalid = {row['entity_id']: row['reason'] for row in stale if row['entity_type']=='record'}
            return {identifier: {'opportunity_id': valid.get(identifier),
                    'opportunity_name': projects[valid[identifier]]['name'] if identifier in valid else None,
                    'opportunity_scope': projects[valid[identifier]]['scope'] if identifier in valid else None,
                    'project_link_stale': identifier in invalid,
                    'project_link_stale_reason': invalid.get(identifier)} for identifier in record_ids}
