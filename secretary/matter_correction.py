"""Correct a completed turn's grouping without replaying its business intent.

Corrections move associations, never re-run a model, complete an old action,
reschedule an activity or restart notifications. Effective receipts follow the
shared matter operation log, so undo can restore the visible attribution too.
"""
from __future__ import annotations

import json
import re

from .crm import _identifier, _owner, _text
from .matters import MatterConflict, _hash, _json
from .store import _timestamp


def _require_turn(flow, db, owner, identifier):
    row = db.execute('SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?', (owner, _identifier(identifier))).fetchone()
    if row is None:
        raise KeyError('未找到你的秘书记录。')
    if row['status'] != 'done':
        raise MatterConflict('请等这条原话整理完成，再核对所属事项。')
    flow.crm._require_record(db, owner, row['record_id'])
    return row


def effective_route(flow, owner, turn_id, route):
    """Pure projection; explicitly return matter_id=None after undoing grouping."""
    owner, turn_id = _owner(owner), _identifier(turn_id)
    with flow.crm._lock:
        db = flow.crm._db
        if not flow.matters._exists(db, 'crm_matter_corrections'):
            return route
        rows = db.execute('''SELECT c.*,o.undone FROM crm_matter_corrections c
            JOIN crm_matter_operations o ON o.owner=c.owner AND o.id=c.operation_id
            WHERE c.owner=? AND c.turn_id=? ORDER BY c.id''', (owner, turn_id)).fetchall()
        if not rows:
            return route
        result = json.loads(rows[0]['before_route_json'])
        for receipt in rows:
            if not receipt['undone']:
                result = json.loads(receipt['after_route_json'])
        return result


def _original_route(flow, owner, turn):
    result = json.loads(turn['data_json'])
    route = result.get('matter_route') or {}
    route = effective_route(flow, owner, turn['id'], route)
    if 'matter_id' not in route:
        route = {**route, 'matter_id': turn['matter_id']}
    return result, route


def _created(flow, db, owner, turn, route):
    identifiers = []
    for change in route.get('changes') or []:
        if not isinstance(change, dict) or change.get('change') != 'created':
            continue
        identifier = _identifier(change.get('record_id'))
        row = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=? AND hidden=0', (owner, identifier)).fetchone()
        if row is None or row['kind'] != 'action' or row['parent_record_id'] != turn['record_id']:
            raise MatterConflict('本次新建步骤的来源已有变化，请核对后再归组。')
        if identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def _material_ids(flow, db, owner, turn_id):
    if not flow.matters._exists(db, 'crm_secretary_turn_attachments'):
        return []
    return [r[0] for r in db.execute('SELECT material_id FROM crm_secretary_turn_attachments WHERE owner=? AND turn_id=? ORDER BY material_id', (owner, turn_id))]


def _old_ids(route):
    result = set()
    if type(route.get('matter_id')) is int:
        result.add(_identifier(route['matter_id']))
    for item in route.get('items') or []:
        if isinstance(item, dict) and type(item.get('id')) is int:
            result.add(_identifier(item['id']))
    return result


def _state(flow, db, owner, turn):
    result, route = _original_route(flow, owner, turn)
    created = _created(flow, db, owner, turn, route)
    old_ids = _old_ids(route)
    for identifier in old_ids:
        flow.matters._require(db, owner, identifier)
    material_ids = _material_ids(flow, db, owner, turn['id'])
    material_rows = [dict(flow.matters._entity(db, owner, 'material', identifier)) for identifier in material_ids]
    record = dict(flow.crm._require_record(db, owner, turn['record_id']))
    created_rows = [dict(flow.crm._require_record(db, owner, identifier)) for identifier in created]
    receipt_states = [dict(r) for r in db.execute('''SELECT o.id,o.undone,o.after_json FROM crm_matter_operations o
        JOIN crm_matter_corrections c ON c.owner=o.owner AND c.operation_id=o.id WHERE c.owner=? AND c.turn_id=? ORDER BY c.id''', (owner, turn['id']))] if flow.matters._exists(db, 'crm_matter_corrections') else []
    signature = _hash({'turn': dict(turn), 'effective_route': route, 'record': record, 'created': created_rows,
        'materials': material_rows, 'matters': flow.matters._snapshot(db, owner, sorted(old_ids)), 'receipts': receipt_states})
    return result, route, created, old_ids, material_ids, signature


def snapshot(flow, owner, turn_id):
    owner, turn_id = _owner(owner), _identifier(turn_id)
    with flow.crm._lock:
        db = flow.crm._db
        turn = _require_turn(flow, db, owner, turn_id)
        _, route, created, old_ids, material_ids, signature = _state(flow, db, owner, turn)
        return {'turn_id': turn_id, 'expected_turn_snapshot': signature, 'snapshot': signature,
                'turn': _updated_turn(flow, owner, turn_id),
                'matter_id': route.get('matter_id'), 'matter_ids': sorted(old_ids),
                'created_action_ids': created, 'material_ids': material_ids,
                'keeps_existing_action_progress': True, 'keeps_existing_schedules': True,
                'message': '只调整这次原话、本次新步骤和材料的归属；已有步骤进展及活动提醒保持原状。'}


def _shared_material(flow, db, owner, matter_id, material_id, turn_id):
    _, entities, _, _ = flow.matters._entities(db, owner, matter_id)
    if flow.matters._exists(db, 'crm_secretary_turn_attachments'):
        for row in db.execute('''SELECT t.id,t.record_id FROM crm_secretary_turn_attachments a
            JOIN crm_secretary_turns t ON t.owner=a.owner AND t.id=a.turn_id
            WHERE a.owner=? AND a.material_id=? AND t.id!=?''', (owner, material_id, turn_id)):
            if row['record_id'] in entities['record']:
                return True
    if flow.matters._exists(db, 'crm_secretary_plan_attachments'):
        if any(r[0] in entities['plan'] for r in db.execute('SELECT plan_id FROM crm_secretary_plan_attachments WHERE owner=? AND material_id=?', (owner, material_id))):
            return True
    if flow.matters._exists(db, 'crm_visit_sources'):
        if any(r[0] in entities['visit'] for r in db.execute('SELECT visit_id FROM crm_visit_sources WHERE owner=? AND material_id=?', (owner, material_id))):
            return True
    return False


def _updated_turn(flow, owner, identifier):
    output = flow.turn(owner, identifier)
    route = effective_route(flow, owner, identifier, output.get('matter_route') or {})
    output['matter_route'] = route
    output['result'] = {**output['result'], 'matter_route': route}
    output['matter_id'] = route.get('matter_id')
    output['matter'] = flow.matters.get(owner, output['matter_id']) if output['matter_id'] else None
    return output


def correct(flow, owner, turn_id, data):
    owner, turn_id = _owner(owner), _identifier(turn_id)
    service = flow.matters
    service._schema(data, {'matter_id', 'matter_mode', 'title', 'expected_matter_revision', 'expected_turn_snapshot', 'request_id'}, {'expected_turn_snapshot', 'request_id'})
    fresh = data.get('matter_mode') == 'fresh'
    if (data.get('matter_id') is not None) == fresh or (data.get('matter_mode') is not None and not fresh):
        raise ValueError('请选择已有事项，或明确另记一件事。')
    if not isinstance(data['expected_turn_snapshot'], str) or not re.fullmatch('[0-9a-f]{64}', data['expected_turn_snapshot']):
        raise ValueError('原话核对版本无效。')
    if 'title' in data:
        _text(data['title'], '新事项名称', 300, required=True)
    if fresh and 'expected_matter_revision' in data:
        raise ValueError('另记一件事不需要目标事项版本。')
    if not fresh and 'expected_matter_revision' not in data:
        raise ValueError('请选择目标事项的当前版本。')
    with service._transaction() as db:
        replay, payload = service._replay(db, owner, 'correct_turn', data, turn_id)
        if replay:
            updated = _updated_turn(flow, owner, turn_id)
            return {**replay, 'turn': updated, 'matter': updated.get('matter'),
                    'message': '这次请求已有回执，显示当前有效归属；未重复执行。'}
        turn = _require_turn(flow, db, owner, turn_id)
        result, old_route, created, old_ids, material_ids, signature = _state(flow, db, owner, turn)
        if signature != data['expected_turn_snapshot']:
            raise MatterConflict('原话、步骤或归属已有变化，请刷新核对后重试。')
        source = flow.crm._require_record(db, owner, turn['record_id'])
        scope = json.loads(turn['scope_json'])
        now = _timestamp(service.clock())
        if fresh:
            before = service._snapshot(db, owner, sorted(old_ids))
            create_data = {'title': data.get('title') or source['title'][:120],
                'objective': source['content'][:4000], 'customer_id': source['customer_id'] or scope.get('customer_id'),
                'opportunity_id': scope.get('opportunity_id')}
            target_id = service._insert(db, owner, create_data, now)
        else:
            target_id = _identifier(data['matter_id'])
            target = service._require(db, owner, target_id)
            service._revision(target, data['expected_matter_revision'])
            if target['visibility'] != 'active' or target['status'] == 'ended':
                raise MatterConflict('目标事项已收起或结束，请先恢复后再归入。')
            for field, known in (('customer_id', source['customer_id'] or scope.get('customer_id')), ('opportunity_id', scope.get('opportunity_id'))):
                if known and target[field] and known != target[field]:
                    raise ValueError('目标事项与已明确的单位或项目不同，请先核对归属。')
            before = service._snapshot(db, owner, sorted(old_ids | {target_id}))
        warnings = []
        for old_id in sorted(old_ids - {target_id}):
            # This turn may be an event's retained original source. Keep its old
            # activity association, but move only the explicit goal attribution.
            db.execute("DELETE FROM crm_matter_links WHERE owner=? AND matter_id=? AND entity_type='record' AND entity_id=? AND role IN ('source','recap')", (owner, old_id, turn['record_id']))
            for action_id in created:
                db.execute("DELETE FROM crm_matter_links WHERE owner=? AND matter_id=? AND entity_type='record' AND entity_id=? AND role='action'", (owner, old_id, action_id))
            for material_id in material_ids:
                if _shared_material(flow, db, owner, old_id, material_id, turn_id):
                    warnings.append('材料也用于原有交流，保留共享关联。')
                else:
                    db.execute("DELETE FROM crm_matter_links WHERE owner=? AND matter_id=? AND entity_type='material' AND entity_id=?", (owner, old_id, material_id))
            db.execute('UPDATE crm_matters SET revision=revision+1,updated_at=? WHERE owner=? AND id=?', (now, owner, old_id))
        # Attach uses savepoints and never changes the underlying source rows.
        service.attach(owner, target_id, 'record', turn['record_id'], role='source')
        for action_id in created:
            service.attach(owner, target_id, 'record', action_id, role='action')
        for material_id in material_ids:
            service.attach(owner, target_id, 'material', material_id, role='source')
        detail = service.get(owner, target_id)
        changed_old_actions = [c for c in old_route.get('changes') or [] if c.get('change') in ('updated', 'completed')]
        if changed_old_actions:
            warnings.append('已记入旧步骤的进展保留在原事项，没有搬移或重新执行。')
        if turn['plan_id']:
            warnings.append('原活动及提醒保留所属；这次原话可作为新目标的共享依据。')
        pending = result.get('pending_matter_decision') or {}
        needs_review = old_route.get('kind') == 'ambiguous' or bool(pending.get('actions'))
        if needs_review:
            warnings.append('已确认原话归属；未重新解释或自动采纳待核对的动作，可在目标事项继续补充。')
        new_route = {'kind': 'existing', 'matter_id': target_id, 'matter': service._summary(detail),
            'items': [service._summary(detail)], 'changes': [c for c in old_route.get('changes') or [] if c.get('change') == 'created'],
            'reason': '用户已核对并调整这次原话的事项归属。', 'corrected': True,
            'needs_review': needs_review, 'warnings': list(dict.fromkeys(warnings))}
        affected = sorted(old_ids | {target_id})
        operation_id = service._operation(db, owner, 'correct_turn', data, target_id, before, affected, payload)
        db.execute('''CREATE TABLE IF NOT EXISTS crm_matter_corrections(
            id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,turn_id INTEGER NOT NULL,
            operation_id INTEGER NOT NULL,before_route_json TEXT NOT NULL,after_route_json TEXT NOT NULL,
            created_at REAL NOT NULL,UNIQUE(owner,operation_id),
            FOREIGN KEY(owner,operation_id) REFERENCES crm_matter_operations(owner,id))''')
        # operations.id is globally unique; SQLite composite FK additionally
        # needs an explicit identity index for the owner isolation relation.
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS crm_matter_operation_identity ON crm_matter_operations(owner,id)')
        db.execute('INSERT INTO crm_matter_corrections(owner,turn_id,operation_id,before_route_json,after_route_json,created_at) VALUES(?,?,?,?,?,?)',
                   (owner, turn_id, operation_id, _json(old_route), _json(new_route), now))
        db.execute('UPDATE crm_secretary_turns SET matter_id=?,matter_revision=?,data_json=?,updated_at=? WHERE owner=? AND id=?',
            (target_id, detail['revision'], _json({**result, 'matter_route': new_route}), now, owner, turn_id))
        return {'matter': service.get(owner, target_id), 'operation_id': operation_id,
                'turn': _updated_turn(flow, owner, turn_id), 'needs_review': needs_review,
                'warnings': new_route['warnings'], 'message': '归属已调整，原话保留；日程和提醒没有重新执行。'}
