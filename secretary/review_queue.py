"""One owner-scoped review queue over existing sources, with durable decisions.

The adapters keep existing adoption/confirmation APIs and their race guards.
Decisions apply to a candidate's content signature, never silently to new evidence.
"""
from __future__ import annotations

import hashlib
import json

from .crm import _owner, _pagination, _text
from .store import _timestamp


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


class ReviewQueue:
    def __init__(self, crm, *, materials=None, visits=None, progress_workspace=None, exchange_workspace=None, clock):
        self.crm, self.materials, self.visits, self.clock = crm, materials, visits, clock
        self.progress_workspace = progress_workspace
        self.exchange_workspace = exchange_workspace
        with crm._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS crm_review_decisions (
                owner TEXT NOT NULL, candidate_key TEXT NOT NULL, signature TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('dismiss','defer','reset')),
                until_at REAL, note TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                PRIMARY KEY(owner,candidate_key))''')

    @staticmethod
    def _candidate(key, kind, payload, source, version, *, blocked=False, reasons=None, decision_basis=None):
        item = {**payload, 'key': key, 'review_type': kind, 'source_type': source,
                'version': version, 'blocked': blocked, 'review_reasons': reasons or []}
        if kind == 'action':
            semantic = {name:payload.get(name) for name in ('title','kind','customer_id','owner_hint','executor_kind',
                'evidence','remind_at','duration_minutes','execution_at','deadline_at','deadline_date','check_at','check_date')}
            item['signature'] = signature([key, semantic, decision_basis, blocked, reasons])
        else:
            item['signature'] = signature([key, payload, version, blocked, reasons])
        return item

    @staticmethod
    def _action_key(prefix,action,seen):
        base=prefix+':A'+signature([action['title'].strip().casefold(),action.get('kind'),
            action.get('executor_kind','unknown'),action.get('owner_hint','待确认')])[:24]
        seen[base]=seen.get(base,0)+1
        return base if seen[base]==1 else base+':'+str(seen[base])

    def _decorate(self, owner, item):
        saved = self.crm._db.execute('SELECT * FROM crm_review_decisions WHERE owner=? AND candidate_key=?',
                                     (owner, item['key'])).fetchone()
        item['decision_state'] = 'pending'
        item['decision_note'] = saved['note'] if saved else ''
        if saved and saved['signature'] == item['signature']:
            if saved['decision'] == 'dismiss':
                item['decision_state'] = 'dismissed'
            elif saved['decision'] == 'defer' and saved['until_at'] > self.clock():
                item['decision_state'] = 'deferred'
                item['deferred_until'] = saved['until_at']
        elif saved and saved['decision'] != 'reset':
            item['source_changed_after_decision'] = True
        return item

    def all_items(self, owner):
        owner = _owner(owner)
        items = []
        with self.crm._lock:
            db = self.crm._db
            if hasattr(self.crm, 'list_customer_drafts'):
                for status in ('pending', 'stale'):
                    page = 1
                    while True:
                        result = self.crm.list_customer_drafts(owner, status=status, page=page, page_size=200)
                        for draft in result['items']:
                            if draft.get('source_record_id') is not None and self.crm.get_record(owner, draft['source_record_id']) is None:
                                continue
                            items.append(self._candidate(f"customer:C{draft['id']}", 'customer', draft,
                                'customer', draft['updated_at'], blocked=status == 'stale',
                                reasons=['资料草稿已过期，请按最新来源重新核对'] if status == 'stale' else []))
                        if page >= result['pages']: break
                        page += 1
            for row in db.execute('''SELECT p.*,rp.record_id,r.customer_id,c.name AS customer_name
                    FROM proposals p LEFT JOIN crm_record_proposals rp ON rp.owner=p.owner AND rp.proposal_id=p.id
                    LEFT JOIN crm_records r ON r.owner=rp.owner AND r.id=rp.record_id
                    LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id
                    WHERE p.owner=? AND p.status='pending' AND (r.id IS NULL OR r.hidden=0) ORDER BY p.id''', (owner,)).fetchall():
                payload = {k: v for k, v in dict(row).items() if k != 'owner'}
                payload['title'] = row['title']
                items.append(self._candidate(f"proposal:P{row['id']}", 'proposal', payload,
                    'proposal', row['updated_at']))
            linked_materials, material_records = set(), set()
            if self.visits:
                offset = 0
                while True:
                    visits = self.visits.list(owner, limit=200, offset=offset)
                    for visit in visits['items']:
                        detail = self.visits.detail(owner, visit['id'])
                        decision_basis=[[s['material_id'],s.get('text',''),s.get('role'),s.get('source_use','included')]
                            for s in detail['sources']]
                        for source in detail['sources']:
                            linked_materials.add(source['material_id'])
                            record_id = source['material'].get('record_id')
                            if record_id: material_records.add(record_id)
                        for action in detail['actions']:
                            if action.get('adopted_record_id'): continue
                            payload = {**action, 'action_key': action['key'], 'visit_id': visit['id'],
                                'customer_id': visit.get('customer_id'), 'customer_name': visit.get('customer_name'),
                                'source_title': visit['title']}
                            items.append(self._candidate(f"visit:V{visit['id']}:A{action['key']}", 'action',
                                payload, 'visit', detail['visit']['revision'], blocked=action.get('needs_review', False),
                                reasons=action.get('review_reasons', []),decision_basis=decision_basis))
                    offset += len(visits['items'])
                    if offset >= visits['total'] or not visits['items']: break
            if self.materials:
                page = 1
                while True:
                    result = self.materials.list(owner, page=page, page_size=200)
                    for material in result['items']:
                        if material.get('record_id'): material_records.add(material['record_id'])
                        if material['id'] in linked_materials: continue
                        detail = self.materials.detail(owner, material['id'])
                        seen={}
                        for action in (detail.get('analysis') or {}).get('actions', []):
                            if action.get('adopted_record_id'): continue
                            blocked = material['status'] != 'review' or action.get('needs_review', False)
                            payload = {**action, 'action_id': action['id'], 'material_id': material['id'],
                                'customer_id': material.get('customer_id'), 'source_title': material['title']}
                            items.append(self._candidate(self._action_key(f"material:M{material['id']}",action,seen), 'action',
                                payload, 'material', material['revision'], blocked=blocked,
                                reasons=action.get('review_reasons', []) or (['来源尚未整理完成'] if blocked else []),
                                decision_basis=detail.get('text','')))
                    if page >= result['pages']: break
                    page += 1
            for row in db.execute('SELECT record_id FROM crm_analyses WHERE owner=? ORDER BY updated_at DESC,record_id',
                                  (owner,)).fetchall():
                if row['record_id'] in material_records: continue
                analysis = self.crm.get_analysis(owner, row['record_id'])
                record = self.crm.get_record(owner, row['record_id'])
                if not analysis or not record: continue
                seen={}
                for action in analysis['actions']:
                    if action.get('adopted_record_id'): continue
                    context = (self.visits.record_action_context(owner, record['id'], action)
                               if self.visits and hasattr(self.visits, 'record_action_context') else None)
                    reasons = ['原始记录已修改，请重新整理或核对来源'] if analysis.get('stale') else []
                    if context: reasons.extend(context['reasons'])
                    payload = {**action, 'record_id': record['id'], 'action_id': action['id'],
                        'customer_id': record.get('customer_id'), 'customer_name': record.get('customer_name'),
                        'source_title': record['title']}
                    if context:
                        payload.update(visit_id=context['visit_id'],visit_revision=context['revision'])
                    items.append(self._candidate(self._action_key(f"record:R{record['id']}",action,seen), 'action', payload,
                        'record', analysis['version'], blocked=bool(reasons),
                        reasons=reasons,
                        decision_basis=analysis['input_fingerprint']))
            if self.progress_workspace is not None:
                for entry in self.progress_workspace.review_entries(owner):
                    reasons = entry.get('errors', [])
                    items.append(self._candidate(f"progress:{entry['run_id']}", 'preparation',
                        entry, 'progress', entry['revision'],
                        blocked=entry['status'] in ('failed', 'needs_review'), reasons=reasons))
            if self.exchange_workspace is not None:
                for entry in self.exchange_workspace.review_entries(owner):
                    items.append(self._candidate(f"exchange:{entry['workspace_id']}", 'preparation',
                        entry, 'exchange', entry['version'], blocked=entry['status'] in ('stale', 'partial', 'waiting_source'),
                        reasons=entry.get('errors', [])))
            # The same canonical candidate appears once; similar titles are only hints.
            items = list({item['key']: self._decorate(owner, item) for item in items}.values())
            groups = {}
            for item in items:
                if item['review_type'] != 'action': continue
                term = item.get('executor_kind', 'unknown')
                normalized = ''.join(item.get('title', '').split()).casefold()
                groups.setdefault((item.get('customer_id'), normalized, term), []).append(item['key'])
            for item in items:
                group = groups.get((item.get('customer_id'), ''.join(item.get('title', '').split()).casefold(),
                                    item.get('executor_kind', 'unknown')), [])
                item['similar_keys'] = [key for key in group if key != item['key']]
            return sorted(items, key=lambda item: (not item['blocked'], item['review_type'], item['key']))

    def list(self, owner, *, page=1, page_size=50, state='pending', customer_id=None):
        offset = _pagination(page, page_size)
        if state not in ('pending', 'deferred', 'dismissed', 'all'):
            raise ValueError('核对队列状态无效')
        items = self.all_items(owner)
        if customer_id is not None: items = [i for i in items if i.get('customer_id') == customer_id]
        counts = {name: sum(i['decision_state'] == name for i in items) for name in ('pending', 'deferred', 'dismissed')}
        selected = [i for i in items if state == 'all' or i['decision_state'] == state]
        visible = selected[offset:offset+page_size]
        return {'items': visible, 'total': len(selected), 'page': page, 'page_size': page_size,
                'pages': max(1, (len(selected)+page_size-1)//page_size), 'counts': counts,
                'customers': [i for i in visible if i['review_type'] == 'customer'],
                'proposals': [i for i in visible if i['review_type'] == 'proposal'],
                'preparations': [i for i in visible if i['review_type'] == 'preparation'],
                'actions': [i for i in visible if i['review_type'] == 'action']}

    def decide(self, owner, data):
        owner = _owner(owner)
        if not isinstance(data, dict) or set(data)-{'key','signature','decision','until_at','note'}:
            raise ValueError('核对决定字段无效')
        key = _text(data.get('key'), '事项标识', 200, required=True)
        if key.startswith('progress:'):
            raise ValueError('秘书准备稿请打开原准备稿核对后采用或取消，不能作为普通事项处理')
        if key.startswith('exchange:'):
            raise ValueError('交流准备稿请打开原准备稿继续核对或明确结束本次核对，不能作为普通事项处理')
        decision = data.get('decision')
        if decision not in ('dismiss', 'defer', 'reset'): raise ValueError('请选择不采纳、稍后处理或恢复核对')
        if not key.startswith(('record:', 'material:', 'visit:')):
            raise ValueError('客户资料或日程请使用明确的确认／撤回操作')
        until = _timestamp(data.get('until_at')) if decision == 'defer' else None
        if until is not None and until <= self.clock(): raise ValueError('稍后处理时间应在未来')
        note = _text(data.get('note', ''), '决定说明', 1000)
        with self.crm._lock:
            item = next((i for i in self.all_items(owner) if i['key'] == key), None)
            if item is None: raise KeyError('未找到你的待核对事项')
            if data.get('signature') != item['signature']: raise ValueError('来源已有变化，请刷新后核对')
            with self.crm._transaction() as db:
                db.execute('INSERT INTO crm_review_decisions VALUES (?,?,?,?,?,?,?) '
                           'ON CONFLICT(owner,candidate_key) DO UPDATE SET signature=excluded.signature,'
                           'decision=excluded.decision,until_at=excluded.until_at,note=excluded.note,updated_at=excluded.updated_at',
                           (owner,key,item['signature'],decision,until,note,self.clock()))
            return self._decorate(owner,item)
