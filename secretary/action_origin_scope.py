"""Immutable adoption-time attribution; reads never create or repair metadata."""
from __future__ import annotations

import json


DIMENSIONS = ('customer', 'project', 'contacts')


def init_scope_schema(db):
    """Called by service initialization, never by a GET projection."""
    db.execute('''CREATE TABLE IF NOT EXISTS crm_action_origin_scopes (
        owner TEXT NOT NULL,origin_type TEXT NOT NULL,origin_key TEXT NOT NULL,
        record_id INTEGER NOT NULL,scope_json TEXT NOT NULL,created_at REAL NOT NULL,
        PRIMARY KEY(owner,origin_type,origin_key,record_id),
        FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id))''')


def _object(value):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else None
    except (ValueError, TypeError):
        return None


def _id(value):
    return type(value) is int and value > 0


def _nullable_id(value):
    return value is None or _id(value)


def _tables(db):
    return {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def target_ids(result):
    """Only documented business targets, never arbitrary ids in a reply."""
    return sorted({result[key] for key in ('record_id', 'next_record_id', 'completed_record_id', 'remaining_record_id')
                   if _id(result.get(key))})


def current_scope(db, owner, record, *, workspace=None, timeline=None, tables=None):
    tables = _tables(db) if tables is None else tables
    rid = record['id']
    value = {'version': 1, 'record_id': rid, 'customer_id': record['customer_id'],
             'known_fields': ['customer'], 'needs_review': False}
    link = None
    if 'crm_opportunity_links' in tables:
        link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?", (owner, rid)).fetchone()
    value['opportunity_id'] = link['opportunity_id'] if link else None
    value['known_fields'].append('project')
    if link:
        if link['customer_id'] != record['customer_id']:
            value['needs_review'] = True
        if workspace is not None and link['source_snapshot'] != workspace._link_snapshot(db, owner, 'record', record):
            value['needs_review'] = True
        if link['opportunity_id'] is not None and 'crm_opportunities' in tables:
            project = db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?', (owner, link['opportunity_id'])).fetchone()
            if project is None or project['archived'] or project['customer_id'] != record['customer_id']:
                value['needs_review'] = True
    if timeline is not None:
        try:
            event = timeline.get_event(owner, 'record:'+str(rid))
            # Keep actual owned historical IDs when archived/stale, and separately
            # flag invalidity. Dropping them would invent a person-removal event.
            people = event['contact_relations']
            value['contact_ids'] = sorted({person['contact_id'] for person in people if _id(person.get('contact_id'))})
            value['known_fields'].append('contacts')
            value['needs_review'] |= bool(event['needs_review'] or event.get('opportunity_archived')
                                          or any(not person.get('valid') or person.get('archived') for person in people))
            if event.get('opportunity_id') is not None:
                value['needs_review'] |= any(person.get('customer_id') != record['customer_id']
                    and not timeline._participates(db, owner, person, event['opportunity_id']) for person in people)
        except (KeyError, ValueError, TypeError):
            value['needs_review'] = True
    elif 'crm_timeline_contexts' not in tables:
        # No person-association sidecar exists in this service/database. This is
        # a known empty relation, unlike an old receipt missing its person data.
        value['contact_ids'] = []
        value['known_fields'].append('contacts')
    return value


def validate_scope(db, owner, value, record_id, *, tables=None):
    value = _object(value)
    if value is None or type(value.get('version')) is not int or value['version'] != 1 or value.get('record_id') != record_id or not _id(value.get('record_id')):
        return None
    known = value.get('known_fields')
    if not isinstance(known, list) or any(not isinstance(field, str) or field not in DIMENSIONS for field in known) or len(set(known)) != len(known):
        return None
    tables = _tables(db) if tables is None else tables
    for dimension, field, table in (('customer', 'customer_id', 'crm_customers'), ('project', 'opportunity_id', 'crm_opportunities')):
        if dimension in known:
            if field not in value or not _nullable_id(value[field]):
                return None
            if value[field] is not None and table in tables and not db.execute('SELECT 1 FROM '+table+' WHERE owner=? AND id=?', (owner, value[field])).fetchone():
                return None
    if 'contacts' in known and 'contacts_required' in value:
        return None
    for field in (['contact_ids'] if 'contacts' in known else []) + (['contacts_required'] if 'contacts_required' in value else []):
        ids = value.get(field)
        if not isinstance(ids, list) or len(ids) > 50 or any(not _id(identifier) for identifier in ids) or ids != sorted(set(ids)):
            return None
        if 'crm_contacts' in tables and any(not db.execute('SELECT 1 FROM crm_contacts WHERE owner=? AND id=?', (owner, identifier)).fetchone() for identifier in ids):
            return None
    return value


def save_scope(db, owner, origin_type, origin_key, record_id, scope, now):
    if validate_scope(db, owner, scope, record_id) is None:
        raise ValueError('采用归属基线无效；没有建立虚构来源')
    # A later replay, or a recovery of a durable success, cannot rewrite history.
    db.execute('INSERT OR IGNORE INTO crm_action_origin_scopes VALUES (?,?,?,?,?,?)',
               (owner, origin_type, str(origin_key), record_id,
                json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False), now))


def save_targets(crm, db, owner, origin_type, origin_key, result, now, *, workspace=None, timeline=None):
    for rid in target_ids(result):
        record = crm._require_record(db, owner, rid)
        save_scope(db, owner, origin_type, origin_key, rid,
                   current_scope(db, owner, record, workspace=workspace, timeline=timeline), now)


def saved_scope(db, owner, origin_type, origin_key, record_id, *, tables=None):
    tables = _tables(db) if tables is None else tables
    if 'crm_action_origin_scopes' not in tables:
        return None, False
    row = db.execute('SELECT scope_json FROM crm_action_origin_scopes WHERE owner=? AND origin_type=? AND origin_key=? AND record_id=?',
                     (owner, origin_type, str(origin_key), record_id)).fetchone()
    return (validate_scope(db, owner, row['scope_json'], record_id, tables=tables), True) if row else (None, False)


def legacy_scope(db, owner, record_id, scope, *, current=None, contacts_required=None, tables=None):
    """Only retained historical dimensions; null filters are not target facts."""
    result = {'version': 1, 'record_id': record_id, 'known_fields': []}
    if isinstance(current, dict) and current.get('record_id') == record_id:
        for dimension, field in (('customer', 'customer_id'), ('project', 'opportunity_id')):
            if field in current and _nullable_id(current[field]):
                result[field] = current[field]
                result['known_fields'].append(dimension)
    for dimension, field in (('customer', 'customer_id'), ('project', 'opportunity_id')):
        if dimension not in result['known_fields'] and _id(scope.get(field)):
            result[field] = scope[field]
            result['known_fields'].append(dimension)
    required = contacts_required if contacts_required is not None else ([scope['contact_id']] if _id(scope.get('contact_id')) else [])
    if not isinstance(required, list) or any(not _id(identifier) for identifier in required):
        return None
    if required:
        result['contacts_required'] = sorted(set(required))
    if not result['known_fields'] and not required:
        return None
    return validate_scope(db, owner, result, record_id, tables=tables)


def compare_scope(baseline, current, basis):
    changed, known = [], baseline.get('known_fields', []) if baseline else []
    for dimension, field in (('customer', 'customer_id'), ('project', 'opportunity_id'), ('contacts', 'contact_ids')):
        if dimension in known and dimension in current['known_fields'] and baseline[field] != current[field]:
            changed.append(dimension)
    if baseline and baseline.get('contacts_required') and 'contacts' in current['known_fields'] and not set(baseline['contacts_required']).issubset(current['contact_ids']):
        changed.append('contacts')
    same_customer = (baseline['customer_id'] == current['customer_id']) if 'customer' in known else None
    complete = all(field in known and field in current['known_fields'] for field in DIMENSIONS)
    return {'scope_status': 'changed' if changed else 'unchanged' if complete else 'unknown',
            'scope_changed': True if changed else False if complete else None,
            'scope_changed_fields': changed, 'scope_basis': basis if baseline else 'unknown',
            'scope_needs_review': bool(current['needs_review']), 'same_customer': same_customer}
