"""Durable webpage follow-up notices, separate from execution notifications.

There is no scheduler here. The web application's cleanup context owns its
worker. Every validation and publish uses the shared SQLite transaction, so a
second connection cannot publish an earlier plan version after a change commits.
"""
from __future__ import annotations

from datetime import datetime
import json
import time
import uuid

from .arrangement_time import normalize_time, time_end, time_signature, time_start
from .crm import _identifier, _owner
from .store import SHANGHAI, _timestamp


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _object(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


class ArrangementReminders:
    LEASE_SECONDS = 120
    RETRY_BASE_SECONDS = 30
    RETRY_MAX_SECONDS = 3600

    def __init__(self, queue, *, clock=time.time):
        self.queue, self.crm, self.clock = queue, queue.crm, clock
        self.closed = False

    def close(self):
        """No worker or database is owned by this service."""
        self.closed = True

    def _now(self, now):
        if self.closed:
            raise RuntimeError('推进提醒服务已关闭。')
        return _timestamp(self.clock() if now is None else now)

    @staticmethod
    def _local_date(at):
        return datetime.fromtimestamp(at, SHANGHAI).date().isoformat()

    def _visible(self, db, plan):
        source = db.execute('SELECT hidden FROM crm_records WHERE owner=? AND id=?',
                            (plan['owner'], plan['record_id'])).fetchone()
        return bool(source and not source['hidden'] and not (
            self.queue._exists(db, 'crm_secretary_trash') and db.execute(
                'SELECT 1 FROM crm_secretary_trash WHERE owner=? AND record_id=?',
                (plan['owner'], plan['record_id'])).fetchone()))

    def _eligible(self, db, plan):
        return (plan is not None and plan['settling_state'] == 'pending'
                and bool(plan['followup_enabled']) and self._visible(db, plan))

    @staticmethod
    def _held(plan):
        # Only a matching release or timeout clears a hold. An expired timestamp
        # by itself is insufficient authority to release a different generation.
        return bool(plan['followup_dirty'] or plan['followup_hold_until'] is not None
                    or plan['hold_turn_id'] is not None)

    @staticmethod
    def _silent_until(data):
        value = data.get('silent_until')
        if value is None:
            return None
        try:
            if isinstance(value, dict):
                if value.get('time_spec') is not None:
                    return time_start(value['time_spec'])
                for key in ('until_at', 'at', 'start_at', 'until'):
                    if value.get(key) is not None:
                        return _timestamp(value[key])
                return time_start(value)
            return _timestamp(value)
        except (TypeError, ValueError):
            # Malformed quiet-time data must not cause an unexpected notice.
            return float('inf')

    def _causes(self, db, plan, now):
        """Identity and time validity; temporary holds/quiet periods are gates."""
        data = self.queue._data(plan)
        cycle = plan['settling_cycle_id']
        epoch = data.get('followup_epoch', 0)
        if type(epoch) is not int or epoch < 0:
            return {}
        causes = {}
        check = data.get('next_check')
        if isinstance(check, dict) and check.get('status') == 'planned':
            try:
                spec = normalize_time(check.get('time_spec'), plan['created_at'], role='check')
                identifier = check.get('check_id')
                if (spec is not None and isinstance(identifier, str) and identifier
                        and len(identifier) <= 1000 and plan['next_check_at'] == time_start(spec)):
                    key = f'{cycle}:check:{identifier}'
                    causes[key] = {'kind': 'check', 'due_at': time_start(spec), 'check_id': identifier,
                                   'time_signature': time_signature(spec), 'time_spec': spec,
                                   'action': check.get('action') or '继续推进这次安排'}
                    # Deliberately no time_end check: an unhandled date/window
                    # remains valid after its window; only explicit resume gates it.
            except (TypeError, ValueError):
                pass
        deadline = data.get('settle_deadline')
        if isinstance(deadline, dict):
            try:
                spec = normalize_time(deadline.get('time_spec'), plan['created_at'], role='deadline')
                if spec is None or plan['deadline_end_at'] != time_end(spec):
                    return causes
                signature = time_signature(spec)
                end = time_end(spec)
                near = time_start(spec) - 86400 if spec['precision'] == 'date' else end - 86400
                existing = {n['dedupe_key']: n for n in db.execute(
                    'SELECT * FROM crm_arrangement_notifications WHERE owner=? AND plan_id=?',
                    (plan['owner'], plan['id']))}
                for kind, due in (('deadline_near', near), ('deadline_overdue', end)):
                    key = f'{cycle}:epoch:{epoch}:deadline:{signature}:{kind}'
                    old = existing.get(key)
                    old_payload = _object(old['payload_json']) if old else {}
                    registration = data.get('deadline_registered_at')
                    if registration is None:
                        registration = old_payload.get('first_registration', plan['updated_at'])
                    registration = _timestamp(registration)
                    resumed = data.get('followup_resumed_at')
                    resumed = _timestamp(resumed) if resumed is not None else registration
                    if kind == 'deadline_near' and (now >= end or max(registration, resumed) > near):
                        continue
                    causes[key] = {'kind': kind, 'due_at': due, 'deadline_signature': signature,
                                   'time_signature': signature, 'time_spec': spec,
                                   'strength': deadline.get('strength', 'target'),
                                   'first_registration': registration, 'followup_epoch': epoch}
            except (ValueError, TypeError):
                pass
        return causes

    @staticmethod
    def _same_cause(payload, cause):
        return (payload.get('time_signature') == cause.get('time_signature')
                and payload.get('check_id') == cause.get('check_id')
                and payload.get('deadline_signature') == cause.get('deadline_signature'))

    @staticmethod
    def _obsolete(db, identifier):
        return db.execute("""UPDATE crm_arrangement_notifications SET status='obsolete',token=NULL,
            lease_until=NULL WHERE id=? AND status!='obsolete'""", (identifier,)).rowcount

    def _expire_holds(self, db, now, stats):
        if not self.queue._exists(db, 'crm_secretary_turns'):
            return
        for plan in db.execute('''SELECT * FROM crm_secretary_plans
                WHERE hold_turn_id IS NOT NULL AND followup_hold_until<=?''', (now,)).fetchall():
            turn = db.execute('SELECT * FROM crm_secretary_turns WHERE owner=? AND id=? AND plan_id=?',
                              (plan['owner'], plan['hold_turn_id'], plan['id'])).fetchone()
            generation = _object(turn['data_json']).get('arrangement_hold_generation') if turn else None
            if type(generation) is not int or generation != plan['hold_generation']:
                stats['unmatched_holds'] += 1
                continue
            if turn['status'] in ('queued', 'processing'):
                db.execute("""UPDATE crm_secretary_turns SET status='failed',lease=NULL,claimed_at=NULL,
                    error=?,reply=?,question='',updated_at=? WHERE owner=? AND id=?
                    AND status IN ('queued','processing')""",
                    ('这次补充整理超时，原话已保留，可重试或直接完善安排。',
                     '原话已保存，但这次补充未整理成功；推进提示按上次有效状态保留。',
                     now, plan['owner'], turn['id']))
            result = db.execute('''UPDATE crm_secretary_plans SET followup_dirty=0,
                followup_hold_until=NULL,hold_turn_id=NULL,hold_generation=hold_generation+1
                WHERE owner=? AND id=? AND hold_turn_id=? AND hold_generation=? AND followup_hold_until<=?''',
                (plan['owner'], plan['id'], turn['id'], generation, now))
            stats['expired_holds'] += result.rowcount

    def _reconcile_plan(self, db, plan, now, stats):
        previous = db.execute('SELECT * FROM crm_arrangement_notifications WHERE owner=? AND plan_id=?',
                              (plan['owner'], plan['id'])).fetchall()
        if not self._eligible(db, plan):
            for notice in previous:
                stats['obsolete'] += self._obsolete(db, notice['id'])
            return
        causes = self._causes(db, plan, now)
        existing = {row['dedupe_key']: row for row in previous}
        silent = self._silent_until(self.queue._data(plan))
        for notice in previous:
            cause = causes.get(notice['dedupe_key'])
            if (cause is None or notice['settling_cycle_id'] != plan['settling_cycle_id']
                    or not self._same_cause(_object(notice['payload_json']), cause)):
                stats['obsolete'] += self._obsolete(db, notice['id'])
        # Preserve existing causes during processing. A failed turn must not
        # permanently consume a notice's identity or read history.
        if self._held(plan):
            return
        if silent == float('inf'):
            return
        for key, cause in causes.items():
            old = existing.get(key)
            payload = dict(cause, dedupe_key=key, cycle=plan['settling_cycle_id'], plan_id=plan['id'])
            available = max(cause['due_at'], silent if silent is not None else cause['due_at'])
            # Quiet time is policy, while a failed delivery has its own durable
            # retry floor. Moving a check earlier may move the former earlier;
            # it must never discard the latter when a version is rebound.
            old_payload = _object(old['payload_json']) if old is not None else {}
            retry_at = old_payload.get('retry_not_before')
            legacy_retry = old is not None and old['status'] == 'queued' and old['attempts'] > 0 and 'retry_not_before' not in old_payload
            if legacy_retry:
                # An older queued row may already encode a delivery backoff.
                # Capture that floor once, so repeated sweeps cannot silently
                # replace it with null after a version change.
                retry_at = old['available_at']
            payload['retry_not_before'] = retry_at
            if retry_at is not None:
                try:
                    available = max(available, _timestamp(retry_at))
                except (TypeError, ValueError):
                    available = max(available, old['available_at'])
            if old is None:
                db.execute('''INSERT INTO crm_arrangement_notifications(owner,plan_id,settling_cycle_id,
                    followup_version,dedupe_key,kind,due_at,available_at,status,payload_json)
                    VALUES(?,?,?,?,?,?,?,?,'queued',?)''', (plan['owner'], plan['id'], plan['settling_cycle_id'],
                    plan['followup_version'], key, cause['kind'], cause['due_at'], available, _json(payload)))
                stats['generated'] += 1
            elif old['status'] != 'obsolete' and self._same_cause(_object(old['payload_json']), cause):
                if old['followup_version'] != plan['followup_version']:
                    if old['status'] == 'published':
                        db.execute('UPDATE crm_arrangement_notifications SET followup_version=? WHERE id=?',
                                   (plan['followup_version'], old['id']))
                    else:
                        db.execute("""UPDATE crm_arrangement_notifications SET followup_version=?,status='queued',
                            token=NULL,lease_until=NULL,available_at=?,payload_json=? WHERE id=?""",
                            (plan['followup_version'], available, _json(payload), old['id']))
                    stats['rebound'] += 1
                elif old['status'] == 'queued' and (available != old['available_at'] or legacy_retry):
                    db.execute('UPDATE crm_arrangement_notifications SET available_at=?,payload_json=? WHERE id=?',
                               (available, _json(payload), old['id']))

    def _sweep(self, db, now):
        stats = {'generated': 0, 'rebound': 0, 'obsolete': 0, 'expired_holds': 0, 'unmatched_holds': 0}
        self._expire_holds(db, now, stats)
        plans = db.execute('''SELECT p.* FROM crm_secretary_plans p WHERE followup_enabled=1
            OR followup_dirty=1 OR hold_turn_id IS NOT NULL OR EXISTS(SELECT 1 FROM crm_arrangement_notifications n
            WHERE n.owner=p.owner AND n.plan_id=p.id AND n.status!='obsolete') ORDER BY p.id''').fetchall()
        for plan in plans:
            self._reconcile_plan(db, plan, now, stats)
        return stats

    def sweep(self, now=None):
        now = self._now(now)
        with self.queue._transaction() as db:
            return self._sweep(db, now)

    def _valid(self, db, notice, now, *, due=True):
        plan = db.execute('SELECT * FROM crm_secretary_plans WHERE owner=? AND id=?',
                          (notice['owner'], notice['plan_id'])).fetchone()
        if (not self._eligible(db, plan) or self._held(plan)
                or notice['followup_version'] != plan['followup_version']
                or notice['settling_cycle_id'] != plan['settling_cycle_id']):
            return None
        silent = self._silent_until(self.queue._data(plan))
        if silent is not None and now < silent:
            return None
        cause = self._causes(db, plan, now).get(notice['dedupe_key'])
        if (cause is None or not self._same_cause(_object(notice['payload_json']), cause)
                or notice['due_at'] != cause['due_at'] or (due and now < notice['due_at'])):
            return None
        return plan

    def claim_due(self, now=None):
        now = self._now(now)
        with self.queue._transaction() as db:
            self._sweep(db, now)
            candidates = db.execute('''SELECT * FROM crm_arrangement_notifications WHERE due_at<=? AND
                ((status='queued' AND available_at<=?) OR (status='leased' AND lease_until<=?))
                ORDER BY due_at,id''', (now, now, now)).fetchall()
            for notice in candidates:
                if self._valid(db, notice, now) is None:
                    continue
                token = uuid.uuid4().hex
                payload = _object(notice['payload_json'])
                payload.setdefault('retry_not_before', None)
                db.execute("""UPDATE crm_arrangement_notifications SET status='leased',token=?,lease_until=?,
                    attempts=attempts+1,payload_json=? WHERE id=?""", (token, now + self.LEASE_SECONDS, _json(payload), notice['id']))
                result = dict(notice)
                result.update(token=token, status='leased', lease_until=now + self.LEASE_SECONDS,
                              attempts=notice['attempts'] + 1, payload=payload)
                return result
        return None

    def publish(self, identifier, token, now=None):
        identifier, now = _identifier(identifier), self._now(now)
        with self.queue._transaction() as db:
            notice = db.execute("SELECT * FROM crm_arrangement_notifications WHERE id=? AND token=? AND status='leased'",
                                (identifier, token)).fetchone()
            if (notice is None or notice['lease_until'] is None or now >= notice['lease_until']
                    or self._valid(db, notice, now) is None):
                return False
            payload = _object(notice['payload_json'])
            payload['local_date'] = self._local_date(now)
            result = db.execute("""UPDATE crm_arrangement_notifications SET status='published',token=NULL,
                lease_until=NULL,published_at=?,payload_json=? WHERE id=? AND token=? AND status='leased'""",
                (now, _json(payload), identifier, token))
            return result.rowcount == 1

    def retry(self, identifier, token, now=None):
        identifier, now = _identifier(identifier), self._now(now)
        with self.queue._transaction() as db:
            notice = db.execute("SELECT * FROM crm_arrangement_notifications WHERE id=? AND token=? AND status='leased'",
                                (identifier, token)).fetchone()
            if (notice is None or notice['lease_until'] is None or now >= notice['lease_until']
                    or self._valid(db, notice, now) is None):
                return False
            delay = min(self.RETRY_MAX_SECONDS,
                        self.RETRY_BASE_SECONDS * 2 ** min(max(notice['attempts'] - 1, 0), 7))
            payload = _object(notice['payload_json'])
            payload['retry_not_before'] = now + delay
            db.execute("""UPDATE crm_arrangement_notifications SET status='queued',token=NULL,lease_until=NULL,
                available_at=?,payload_json=? WHERE id=? AND token=?""", (now + delay, _json(payload), identifier, token))
            return True

    def _groups(self, db, owner, now):
        groups = {}
        for notice in db.execute("SELECT * FROM crm_arrangement_notifications WHERE owner=? AND status='published' ORDER BY id", (owner,)):
            plan = self._valid(db, notice, now)
            if plan is None:
                continue
            payload = _object(notice['payload_json'])
            local_date = self._local_date(notice['published_at'])
            key = (plan['id'], local_date)
            groups.setdefault(key, {'plan': plan, 'members': [], 'local_date': local_date})['members'].append((notice, payload))
        return sorted(groups.values(), key=lambda group: (
            max(n['published_at'] for n, _ in group['members']), group['plan']['id']), reverse=True)

    def _public_group(self, db, group):
        plan, members = group['plan'], group['members']
        projection = self.queue.projection(db, plan)
        reasons = []
        for notice, payload in members:
            if notice['kind'] == 'check':
                current_check = projection.get('next_check') or {}
                text = current_check.get('action') or payload.get('action') or '继续推进这次安排'
            elif notice['kind'] == 'deadline_near':
                strength = (projection.get('settle_deadline') or {}).get('strength', payload.get('strength'))
                text = '最晚确定期限将到' if strength == 'required' else '希望确定期限将到'
            else:
                strength = (projection.get('settle_deadline') or {}).get('strength', payload.get('strength'))
                text = '最晚确定期限已过' if strength == 'required' else '超过希望确定期限'
            reasons.append({'id': notice['id'], 'kind': notice['kind'], 'text': text, 'due_at': notice['due_at'],
                            'published_at': notice['published_at'], 'read_at': notice['read_at'],
                            'check_id': payload.get('check_id'), 'time_spec': payload.get('time_spec')})
        unread = any(n['read_at'] is None for n, _ in members)
        return {'id': members[0][0]['id'], 'plan_id': plan['id'], 'record_id': plan['record_id'],
                'group_key': f"plan:{plan['id']}:date:{group['local_date']}",
                'local_date': group['local_date'], 'member_ids': [n['id'] for n, _ in members],
                'title': projection['title'] or '待落实安排', 'revision': plan['revision'],
                'causes': reasons, 'reasons': reasons, 'unread': unread,
                'read_at': None if unread else max(n['read_at'] for n, _ in members),
                'published_at': max(n['published_at'] for n, _ in members), 'arrangement': projection}

    def notices(self, owner, limit=50, offset=0):
        owner, now = _owner(owner), self._now(None)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError('提示分页参数无效。')
        with self.crm._lock:
            db = self.crm._db
            groups = self._groups(db, owner, now)
            return {'items': [self._public_group(db, g) for g in groups[offset:offset + limit]],
                    'total': len(groups), 'unread': sum(any(n['read_at'] is None for n, _ in g['members']) for g in groups)}

    def read(self, owner, identifier):
        owner, identifier, now = _owner(owner), _identifier(identifier), self._now(None)
        with self.queue._transaction() as db:
            for group in self._groups(db, owner, now):
                members = [n['id'] for n, _ in group['members']]
                if identifier not in members:
                    continue
                for member in members:
                    db.execute('''UPDATE crm_arrangement_notifications SET read_at=COALESCE(read_at,?)
                        WHERE owner=? AND id=? AND status='published' ''', (now, owner, member))
                refreshed = next(g for g in self._groups(db, owner, now)
                                 if g['plan']['id'] == group['plan']['id'] and g['local_date'] == group['local_date'])
                return self._public_group(db, refreshed)
        raise KeyError('未找到你的有效推进提示。')
