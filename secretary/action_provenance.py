"""Read existing adoption receipts; never create a new discussion or business fact."""
from __future__ import annotations

import json

from .crm import _identifier, _owner
from .action_origin_scope import (_object, _tables, current_scope, saved_scope,
                                 legacy_scope, compare_scope, target_ids)


def action_origins(crm, owner, record_id, *, workspace=None, timeline=None):
    owner, record_id = _owner(owner), _identifier(record_id)
    with crm._lock:
        db = crm._db
        record = crm._require_record(db, owner, record_id)
        origins, seen = [], set()
        tables = _tables(db)
        current = current_scope(db, owner, record, workspace=workspace, timeline=timeline, tables=tables)
        if 'crm_progress_batch_items' in tables:
            rows = db.execute('''SELECT b.run_id,b.updated_at,i.item_id,i.result_json,i.item_json,
                    r.kind,r.scope_json,r.text,r.mode,r.revision,r.period,r.start_date
                FROM crm_progress_batch_items i
                JOIN crm_progress_batches b ON b.owner=i.owner AND b.request_id=i.request_id
                JOIN crm_progress_runs r ON r.owner=b.owner AND r.id=b.run_id
                WHERE i.owner=? AND i.status='complete' ORDER BY b.updated_at DESC,b.run_id DESC''', (owner,))
            for row in rows:
                result = _object(row['result_json']) or {}
                if record_id not in target_ids(result) or row['run_id'] in seen:
                    continue
                seen.add(row['run_id'])
                scope = _object(row['scope_json']) or {}
                baseline, found = saved_scope(db, owner, 'progress', f'{row["run_id"]}:{row["item_id"]}', record_id, tables=tables)
                basis = 'adoption_snapshot'
                if not found:
                    item = _object(row['item_json'])
                    baseline = legacy_scope(db, owner, record_id, scope,
                        current=item.get('current') if item else None, tables=tables)
                    basis = 'legacy_item' if item and isinstance(item.get('current'), dict) and item['current'].get('record_id') == record_id else 'legacy_context'
                origins.append({'type': 'progress', 'run_id': row['run_id'], 'kind': row['kind'],
                    'scope': scope, 'text': row['text'], 'mode': row['mode'],
                    'period': row['period'], 'start_date': row['start_date'],
                    'adopted_at': row['updated_at'], 'item_id': row['item_id'],
                    **compare_scope(baseline, current, basis)})
        if 'crm_sales_discussion_adoptions' in tables:
            rows = db.execute('''SELECT a.thread_id,a.message_id,a.action_index,a.created_at,
                    t.title,t.customer_id,t.opportunity_id,t.source_record_id,t.contact_id,
                    m.text AS answer_text,u.text AS user_text
                FROM crm_sales_discussion_adoptions a
                JOIN crm_sales_discussions t ON t.owner=a.owner AND t.id=a.thread_id
                JOIN crm_sales_discussion_messages m ON m.owner=a.owner AND m.thread_id=a.thread_id AND m.id=a.message_id
                LEFT JOIN crm_sales_discussion_messages u ON u.owner=m.owner AND u.thread_id=m.thread_id AND u.id=m.reply_to
                WHERE a.owner=? AND a.record_id=? ORDER BY a.created_at DESC''', (owner, record_id))
            for row in rows:
                baseline, found = saved_scope(db, owner, 'discussion', f'{row["thread_id"]}:{row["message_id"]}:{row["action_index"]}', record_id, tables=tables)
                if not found:
                    required = [row['contact_id']] if type(row['contact_id']) is int and row['contact_id'] > 0 else []
                    if 'crm_discussion_adoption_people' in tables:
                        people = db.execute('SELECT contact_ids_json FROM crm_discussion_adoption_people WHERE owner=? AND thread_id=? AND message_id=? AND action_index=?',
                                            (owner, row['thread_id'], row['message_id'], row['action_index'])).fetchone()
                        try:
                            ids = json.loads(people['contact_ids_json']) if people else []
                            if isinstance(ids, list) and all(type(identifier) is int and identifier > 0 for identifier in ids):
                                required.extend(ids)
                        except (ValueError, TypeError):
                            pass
                    baseline = legacy_scope(db, owner, record_id, dict(row), contacts_required=required, tables=tables)
                origins.append({'type': 'discussion', 'thread_id': row['thread_id'],
                    'message_id': row['message_id'], 'action_index': row['action_index'],
                    'title': row['title'], 'user_text': row['user_text'] or '', 'answer_text': row['answer_text'],
                    'scope': {'customer_id': row['customer_id'], 'opportunity_id': row['opportunity_id'],
                              'source_record_id': row['source_record_id']},
                    'adopted_at': row['created_at'],
                    **compare_scope(baseline, current, 'adoption_snapshot' if found else 'legacy_context')})
        parent_id = record['parent_record_id']
        if parent_id:
            parent = db.execute('SELECT title FROM crm_records WHERE owner=? AND id=? AND hidden=0', (owner, parent_id)).fetchone()
            if parent:
                origins.append({'type': 'record', 'record_id': parent_id, 'title': parent['title']})
        origins.sort(key=lambda origin: origin.get('adopted_at') or 0, reverse=True)
        return {'items': origins[:10], 'truncated': len(origins) > 10}
