"""Associate an explicitly adopted action with its existing, unambiguous goal.

No goal, action, proposal, task or notification is created here. Ambiguity and
scope changes produce a receipt while leaving the original adoption intact.
"""
from __future__ import annotations

from .crm import _identifier, _owner
from .matters import MatterConflict
from .sales_workspace import SalesWorkspace


def _project(service, db, owner, kind, row):
    if kind not in ('record', 'visit', 'material') or not service._exists(db, 'crm_opportunity_links'):
        return None, False
    link = db.execute('SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?',
                      (owner, kind, row['id'])).fetchone()
    if not link:
        return None, False
    project = db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?', (owner, link['opportunity_id'])).fetchone() if link['opportunity_id'] else None
    stale = bool(link['source_snapshot'] != SalesWorkspace._entity_snapshot(row) or
                 link['customer_id'] != dict(row).get('customer_id') or
                 (link['opportunity_id'] and (project is None or project['archived'])))
    return link['opportunity_id'], stale


def _receipt(items=(), warning=''):
    return {'matter_candidates': list(items), 'warning': warning}


def _origin_matches(service, db, owner, kind, source, record):
    parent = record['parent_record_id']
    if kind == 'record':
        return parent == source['id']
    if kind == 'material':
        if source['record_id'] and parent == source['record_id']:
            return True
        return bool(service._exists(db, 'crm_material_actions') and db.execute(
            'SELECT 1 FROM crm_material_actions WHERE owner=? AND material_id=? AND record_id=?',
            (owner, source['id'], record['id'])).fetchone())
    if kind == 'visit':
        if service._exists(db, 'crm_visit_adoptions') and db.execute('SELECT 1 FROM crm_visit_adoptions WHERE owner=? AND visit_id=? AND record_id=?', (owner, source['id'], record['id'])).fetchone():
            return True
        if service._exists(db, 'crm_visit_records') and db.execute('SELECT 1 FROM crm_visit_records WHERE owner=? AND visit_id=? AND record_id=?', (owner, source['id'], parent)).fetchone():
            return True
        return bool(service._exists(db, 'crm_visit_sources') and service._exists(db, 'crm_materials') and db.execute('''SELECT 1 FROM crm_visit_sources s JOIN crm_materials m ON m.owner=s.owner AND m.id=s.material_id
            WHERE s.owner=? AND s.visit_id=? AND m.record_id=?''', (owner, source['id'], parent)).fetchone())
    if kind == 'discussion':
        return bool(service._exists(db, 'crm_sales_discussion_adoptions') and db.execute(
            'SELECT 1 FROM crm_sales_discussion_adoptions WHERE owner=? AND thread_id=? AND record_id=?',
            (owner, source['id'], record['id'])).fetchone())
    if kind == 'plan':
        return parent == source['record_id'] or bool(service._exists(db, 'crm_secretary_turns') and db.execute(
            'SELECT 1 FROM crm_secretary_turns WHERE owner=? AND plan_id=? AND record_id=?',
            (owner, source['id'], parent)).fetchone())
    return False


def attach_adoption(service, owner, kind, source_id, record_id):
    owner, source_id, record_id = _owner(owner), _identifier(source_id), _identifier(record_id)
    with service._transaction() as db:
        source = service._entity(db, owner, kind, source_id)
        record = service._entity(db, owner, 'record', record_id)
        if record['kind'] != 'action':
            raise ValueError('只有用户已采纳的原待办可以归入事项。')
        if not _origin_matches(service, db, owner, kind, source, record):
            return _receipt(warning='待办已采纳，但来源引用需要核对，未自动改变所属事项。')
        # Validate ownership independently for both ends. Never infer a goal
        # from the customer alone or generate one when no source is grouped.
        items = service.resolve(owner, kind, source_id)['items']
        parent_id = record['parent_record_id']
        if not items and parent_id and not (kind == 'record' and parent_id == source_id):
            parent = db.execute('SELECT * FROM crm_records WHERE owner=? AND id=? AND hidden=0', (owner, parent_id)).fetchone()
            if parent:
                items = service.resolve(owner, 'record', parent_id)['items']
        if not items:
            return _receipt(warning='待办已采纳，原来源尚未归入事项；可稍后选择所属目标。')
        if len(items) != 1:
            return _receipt(items, '待办已采纳；这份来源关联多个目标，请选择步骤所属事项。')
        target = items[0]
        if target['visibility'] != 'active' or target['status'] == 'ended':
            return _receipt(items, '待办已采纳；原事项已收起或结束，未自动恢复或追加步骤。')
        existing = db.execute("SELECT matter_id FROM crm_matter_links WHERE owner=? AND entity_type='record' AND entity_id=? AND role='action'",
                              (owner, record_id)).fetchone()
        if existing and existing['matter_id'] != target['id']:
            other = service._summary(service.get(owner, existing['matter_id']))
            return _receipt([target, other], '待办已有另一事项归属，保留原归属；请核对后合并或拆分。')
        record_customer = record['customer_id']
        if record_customer != target['customer_id']:
            return _receipt(items, '待办与事项目标的单位归属尚未一致，请核对后再关联。')
        source_customer = dict(source).get('customer_id')
        if source_customer and record_customer and source_customer != record_customer:
            return _receipt(items, '来源与待办的单位归属已有变化，请先核对。')
        action_project, action_stale = _project(service, db, owner, 'record', record)
        source_project, source_stale = _project(service, db, owner, kind, source)
        known_project = action_project if action_project is not None else source_project
        if action_stale or source_stale or (known_project is not None and known_project != target['opportunity_id']):
            return _receipt(items, '来源或步骤的项目关联需要核对，待办保持原样，未自动归入。')
        try:
            result = service.attach(owner, target['id'], 'record', record_id, role='action', expected_revision=target['revision'])
        except MatterConflict:
            return _receipt(items, '事项已有新进展，请刷新后确认步骤归属；原待办已保留。')
        except ValueError:
            return _receipt(items, '步骤与事项归属需要核对；原待办已保留，未自动追加。')
        return {'matter_id': result['matter']['id'], 'matter': service._summary(result['matter']),
                'matter_candidates': [], 'warning': ''}
