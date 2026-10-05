"""Source-bound sales advice, with explicit idempotent adoption into CRM notes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from .crm import _identifier, _owner


def fingerprint(profile):
    context = {key: profile[key] for key in ('customer', 'fields', 'contacts')}
    context['brief'] = {key: profile['brief'][key] for key in ('open_records', 'recent_records', 'next_tasks')}
    context['brief']['recent_activities'] = profile['brief'].get('recent_activities', [])
    if profile.get('coaching_feedback'):
        context['coaching_feedback'] = profile['coaching_feedback']
    if profile.get('projects'):
        context['projects'] = profile['projects']
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class CoachingService:
    def __init__(self, crm, coach, lock, *, clock=time.time):
        self.crm, self.coach, self.lock, self.clock = crm, coach, lock, clock
        self.running, self.errors = {}, {}
        self.slots = asyncio.Semaphore(2)
        self.closed = False
        with crm._lock:
            crm._db.executescript('''
                CREATE TABLE IF NOT EXISTS crm_coaching (
                    owner TEXT NOT NULL, customer_id INTEGER NOT NULL, version INTEGER NOT NULL,
                    input_fingerprint TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(owner, customer_id, version),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_coaching_adoptions (
                    owner TEXT NOT NULL, customer_id INTEGER NOT NULL, version INTEGER NOT NULL,
                    action_index INTEGER NOT NULL, record_id INTEGER NOT NULL,
                    PRIMARY KEY(owner,customer_id,version,action_index),
                    FOREIGN KEY(owner,customer_id,version) REFERENCES crm_coaching(owner,customer_id,version),
                    FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_coaching_feedback (
                    owner TEXT NOT NULL,customer_id INTEGER NOT NULL,version INTEGER NOT NULL,
                    action_index INTEGER NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(owner,customer_id,version,action_index),
                    FOREIGN KEY(owner,customer_id,version) REFERENCES crm_coaching(owner,customer_id,version)
                );
            ''')

    def _profile(self, owner, customer_id):
        profile = self.crm.profile(owner, customer_id)
        if profile is None:
            return None
        intelligence = getattr(self, 'profile_intelligence', None)
        if intelligence is not None and hasattr(intelligence, 'enrich_profile'):
            profile = intelligence.enrich_profile(owner, profile)
        if getattr(self, 'workspace', None):
            profile['projects'] = self.workspace.opportunities(owner, customer_id)['items']
            for project in profile['projects']:
                project.pop('stakeholders_history', None)
                if getattr(self, 'profile_intelligence', None):
                    project['profile_facts'] = self.profile_intelligence.project_facts(owner, customer_id, project['id'])['items']
        with self.crm._lock:
            feedback = [dict(row) for row in self.crm._db.execute('SELECT f.*,c.data_json FROM crm_coaching_feedback f '
                'JOIN crm_coaching c USING(owner,customer_id,version) WHERE f.owner=? AND f.customer_id=? '
                'ORDER BY f.updated_at DESC LIMIT 12', (owner, customer_id))]
        if feedback:
            profile['coaching_feedback'] = []
            for row in feedback:
                moves = json.loads(row.pop('data_json'))['next_moves']
                profile['coaching_feedback'].append({key: row[key] for key in ('version', 'action_index', 'status', 'note', 'updated_at')} |
                                                   {'title': moves[row['action_index'] - 1]['title']})
        return profile

    def _legacy_archived_source(self, db, row, result, profile):
        if result.get('sources_complete') or row['input_fingerprint'] == fingerprint(profile):
            return False
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_record_lifecycle'").fetchone():
            return False
        # Old caches only listed recent notes. A later, explicit lifecycle
        # transition can also invalidate an action/activity used by the coach.
        return bool(db.execute('''SELECT 1 FROM crm_record_lifecycle l
            JOIN crm_records r ON r.owner=l.owner AND r.id=l.record_id
            WHERE l.owner=? AND r.customer_id=? AND r.hidden=1
            AND l.visibility IN ('archived','trash') AND l.updated_at>=?
            AND r.created_at<=? AND (r.kind='note' OR (r.kind='action' AND r.status!='done')
                OR EXISTS (SELECT 1 FROM crm_activities a WHERE a.owner=r.owner AND a.record_id=r.id AND a.created_at<=?))
            LIMIT 1''', (row['owner'], row['customer_id'], row['created_at'],
                        row['created_at'], row['created_at'])).fetchone())

    def view(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        profile = self._profile(owner, customer_id)
        if profile is None:
            raise KeyError()
        with self.crm._lock:
            row = self.crm._db.execute('SELECT * FROM crm_coaching WHERE owner=? AND customer_id=? ORDER BY version DESC LIMIT 1',
                                       (owner, customer_id)).fetchone()
            result = None
            if row:
                result = json.loads(row['data_json'])
                if (any(source.get('record_id') is not None and self.crm.get_record(owner, source['record_id']) is None
                        for source in result.get('sources', [])) or self._legacy_archived_source(self.crm._db, row, result, profile)):
                    result = None
            if result is not None:
                adopted = dict(self.crm._db.execute('SELECT action_index,record_id FROM crm_coaching_adoptions '
                    'WHERE owner=? AND customer_id=? AND version=?', (owner, customer_id, row['version'])).fetchall())
                for index, move in enumerate(result['next_moves'], 1):
                    move['adopted_record_id'] = adopted.get(index)
                    feedback = self.crm._db.execute('SELECT status,note,updated_at FROM crm_coaching_feedback '
                        'WHERE owner=? AND customer_id=? AND version=? AND action_index=?',
                        (owner, customer_id, row['version'], index)).fetchone()
                    move['feedback'] = dict(feedback) if feedback else None
                result.update(version=row['version'], created_at=row['created_at'],
                              stale=row['input_fingerprint'] != fingerprint(profile))
        key = (owner, customer_id)
        return {'recommendation': result, 'generating': key in self.running, 'error': self.errors.get(key),
                'feedback_history': profile.get('coaching_feedback', [])}

    def list_views(self, owner):
        owner = _owner(owner)
        with self.crm._lock:
            rows = self.crm._db.execute('SELECT a.customer_id,c.name AS customer_name,MAX(a.created_at) AS latest '
                'FROM crm_coaching a JOIN crm_customers c ON c.owner=a.owner AND c.id=a.customer_id '
                'WHERE a.owner=? GROUP BY a.customer_id,c.name ORDER BY latest DESC LIMIT 8', (owner,)).fetchall()
        items = []
        for row in rows:
            advice = self.view(owner, row['customer_id'])['recommendation']
            if advice is None:
                continue
            items.append({'customer_id': row['customer_id'], 'customer_name': row['customer_name'],
                          **{key: advice[key] for key in ('summary', 'objective', 'created_at', 'stale')}})
        return {'items': items, 'generating': any(key[0] == owner for key in self.running)}

    def schedule(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        if self.crm.get_customer(owner, customer_id) is None:
            raise KeyError()
        key = (owner, customer_id)
        if self.closed or key in self.running:
            return
        self.errors.pop(key, None)
        self.running[key] = asyncio.create_task(self._refresh(owner, customer_id))

    async def _refresh(self, owner, customer_id):
        key = (owner, customer_id)
        try:
            # A user may add another note during inference. Retry once with the
            # newest snapshot; never publish recommendations for the wrong state.
            for attempt in range(2):
                async with self.lock:
                    profile = self._profile(owner, customer_id)
                    if profile is None:
                        return
                    stamp = fingerprint(profile)
                async with self.slots:
                    advice = await asyncio.wait_for(self.coach.advise(profile, self.clock()), timeout=70)
                async with self.lock:
                    current = self._profile(owner, customer_id)
                    if current is None:
                        return
                    if fingerprint(current) != stamp:
                        if attempt == 0:
                            continue
                        self.errors[key] = '资料仍在更新，请稍后重新生成推进建议。'
                        return
                    with self.crm._transaction() as db:
                        brief = profile['brief']
                        source_records = [*brief.get('recent_records', [])[:5], *brief.get('open_records', [])[:10]]
                        source_records += [{'id': item.get('record_id'), 'title': item.get('record_title', '')}
                                           for item in brief.get('recent_activities', [])[:10]]
                        advice['sources'] = list({item['id']: {'record_id': item['id'], 'title': item['title']}
                            for item in source_records if item.get('id')}.values())
                        advice['sources_complete'] = True
                        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM crm_coaching WHERE owner=? AND customer_id=?',
                                             (owner, customer_id)).fetchone()[0]
                        db.execute('INSERT INTO crm_coaching VALUES (?,?,?,?,?,?)',
                                   (owner, customer_id, version, stamp, json.dumps(advice, ensure_ascii=False), self.clock()))
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.errors[key] = '这次推进建议未生成，原记录仍在，可以稍后重试。'
        finally:
            self.running.pop(key, None)

    def adopt(self, owner, customer_id, version, action_index):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        version, action_index = _identifier(version), _identifier(action_index)
        if action_index > 3:
            raise ValueError('建议编号无效。')
        now = self.clock()
        with self.crm._transaction() as db:
            prior = db.execute('SELECT record_id FROM crm_coaching_adoptions WHERE owner=? AND customer_id=? '
                               'AND version=? AND action_index=?', (owner, customer_id, version, action_index)).fetchone()
            if prior:
                self.crm._require_record(db, owner, prior['record_id'])
                return self.crm.get_record(owner, prior['record_id'])
            profile = self._profile(owner, customer_id)
            if profile is None:
                raise KeyError()
            row = db.execute('SELECT * FROM crm_coaching WHERE owner=? AND customer_id=? AND version=?',
                             (owner, customer_id, version)).fetchone()
            if row is None:
                raise KeyError()
            latest = db.execute('SELECT MAX(version) FROM crm_coaching WHERE owner=? AND customer_id=?',
                                (owner, customer_id)).fetchone()[0]
            # Each adoption advances the saved fingerprint to its own resulting
            # state. Any subsequent manual/profile change invalidates the advice.
            if version != latest or row['input_fingerprint'] != fingerprint(profile):
                raise ValueError('客户资料或跟进事项已变化，请先更新推进建议再采纳。')
            moves = json.loads(row['data_json'])['next_moves']
            if action_index > len(moves):
                raise ValueError('建议编号无效。')
            move = moves[action_index-1]
            if db.execute('SELECT 1 FROM crm_coaching_feedback WHERE owner=? AND customer_id=? AND version=? '
                          'AND action_index=?', (owner, customer_id, version, action_index)).fetchone():
                raise ValueError('这条建议已有执行反馈，请查看更新后的推进建议。')
            content = '\n'.join(['AI推进建议，经你采纳后加入跟进。',
                                 '建议依据：' + move['reason'], '沟通对象：' + move['contact_hint'],
                                 '准备材料：' + move['preparation'], '沟通提问：' + move['talk_track'],
                                 '推进标志：' + move['success_signal']])
            record_id = db.execute('INSERT INTO crm_records(owner,title,content,original_content,source,status,'
                                  'customer_id,classified,kind,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                  (owner, move['title'], content, content, 'web', 'following', customer_id, 1, 'action', now, now)).lastrowid
            db.execute('INSERT INTO crm_coaching_adoptions VALUES (?,?,?,?,?)',
                       (owner, customer_id, version, action_index, record_id))
            # Store the post-adoption fingerprint so other moves in this same
            # recommendation can be adopted without regenerating identical advice.
            db.execute('UPDATE crm_coaching SET input_fingerprint=? WHERE owner=? AND customer_id=? AND version=?',
                       (fingerprint(self._profile(owner, customer_id)), owner, customer_id, version))
        return self.crm.get_record(owner, record_id)

    def feedback(self, owner, customer_id, version, action_index, status, note=''):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        version, action_index = _identifier(version), _identifier(action_index)
        if status not in ('completed', 'blocked', 'paused', 'not_applicable'):
            raise ValueError('请选择已完成、受阻、暂缓或不适用。')
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError('反馈说明请控制在 1000 字以内。')
        with self.crm._transaction() as db:
            row = db.execute('SELECT data_json FROM crm_coaching WHERE owner=? AND customer_id=? AND version=?',
                             (owner, customer_id, version)).fetchone()
            if row is None:
                raise KeyError()
            if action_index > len(json.loads(row['data_json'])['next_moves']):
                raise ValueError('建议编号无效。')
            adopted = db.execute('SELECT record_id FROM crm_coaching_adoptions WHERE owner=? AND customer_id=? '
                'AND version=? AND action_index=?', (owner, customer_id, version, action_index)).fetchone()
            if adopted:
                self.crm._require_record(db, owner, adopted['record_id'])
            db.execute('INSERT INTO crm_coaching_feedback VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,customer_id,version,action_index) '
                       'DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at',
                       (owner, customer_id, version, action_index, status, note.strip(), self.clock()))
            if status == 'completed':
                adoption = db.execute('SELECT record_id FROM crm_coaching_adoptions WHERE owner=? AND customer_id=? '
                    'AND version=? AND action_index=?', (owner, customer_id, version, action_index)).fetchone()
                if adoption:
                    record = db.execute('SELECT r.*,COALESCE(p.task_id,p.target_task_id) AS linked_task_id FROM crm_records r '
                        'LEFT JOIN proposals p ON p.owner=r.owner AND p.id=r.proposal_id WHERE r.owner=? AND r.id=?',
                        (owner, adoption['record_id'])).fetchone()
                    if record and record['linked_task_id']:
                        self.crm._execute(db, owner, {'action': 'complete', 'task_id': record['linked_task_id']}, self.clock())
                    db.execute("UPDATE crm_records SET status='done',updated_at=? WHERE owner=? AND id=?",
                               (self.clock(), owner, adoption['record_id']))
                    db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND status='pending' "
                               "AND id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?)",
                               (self.clock(), owner, owner, adoption['record_id']))
        return self.view(owner, customer_id)

    async def close(self):
        self.closed = True
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def render(recommendation):
        if recommendation is None:
            return '正在根据客户记录生成推进建议，请稍后在客户页查看。'
        lines = ['下一步推进建议（AI建议，请结合实际核对）', '本次目标：' + recommendation['objective'],
                 '为什么：' + recommendation['rationale'][:500]]
        if recommendation.get('stale'):
            lines.append('资料已有变化，以下是上一次建议，请更新后再采纳。')
        for index, move in enumerate(recommendation['next_moves'], 1):
            lines += [f"{index}. {move['title']}", '找谁：' + move['contact_hint'],
                      '怎么问：' + move['talk_track'][:300], '推进标志：' + move['success_signal'][:160]]
        lines.append('后台可查看准备材料和依据，采纳后再补时间；不会自动联系客户。')
        return '\n'.join(lines)[:3500]
