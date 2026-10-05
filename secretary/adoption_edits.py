"""Atomic user edits to a freshly adopted, unconfirmed action.

The original analysis and original_content remain evidence. This seam is not
used to edit existing confirmed plans; those need explicit rescheduling CAS.
"""
from datetime import date
import json

from .action_contract import TERM_FIELDS, EXECUTOR_KINDS
from .crm import _owner, _identifier, _text, RecordConflict
from .store import _timestamp


def edit_unconfirmed_action(crm, owner, record_id, draft, now, *, expected_updated_at,
                            expected_terms_updated_at=None):
    owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
    if not isinstance(draft, dict) or set(draft)-({'title'} | set(TERM_FIELDS)):
        raise ValueError('准备稿行动字段无效。')
    values = dict(draft)
    if 'title' in values:
        values['title'] = _text(values['title'], '行动标题', 120, required=True).strip()
    if 'executor_kind' in values and values['executor_kind'] not in EXECUTOR_KINDS:
        raise ValueError('行动执行主体无效。')
    for key in TERM_FIELDS:
        if key not in values or values[key] is None:
            continue
        if key.endswith('_evidence'):
            values[key] = _text(values[key], '用户补充依据', 2000)
        elif key in ('execution_at', 'deadline_at', 'check_at'):
            values[key] = _timestamp(values[key])
        elif key in ('deadline_date', 'check_date'):
            if not isinstance(values[key], str) or date.fromisoformat(values[key]).isoformat() != values[key]:
                raise ValueError('日期需要为YYYY-MM-DD。')
        elif key == 'duration_minutes' and (type(values[key]) is not int or not 5 <= values[key] <= 720):
            raise ValueError('预计用时需要为5至720分钟。')
    expected_updated_at = _timestamp(expected_updated_at)
    with crm._transaction() as db:
        record = crm._require_record(db, owner, record_id)
        if record['kind'] != 'action' or record['hidden'] or record['status'] == 'done':
            raise ValueError('仅可核对尚未完成的行动准备稿。')
        if record['updated_at'] != expected_updated_at:
            raise RecordConflict('行动已变化，请刷新后核对；准备稿未覆盖新内容。')
        linked = db.execute('SELECT p.* FROM proposals p WHERE p.owner=? AND '
            '(p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))',
            (owner, record['proposal_id'], owner, record_id)).fetchall()
        if any(p['status'] == 'confirmed' or p['task_id'] is not None or p['target_task_id'] is not None for p in linked):
            raise ValueError('行动已有正式日程，请从改期入口核对原安排。')
        prior = db.execute('SELECT * FROM crm_action_terms WHERE owner=? AND record_id=?', (owner, record_id)).fetchone()
        if expected_terms_updated_at != (prior['updated_at'] if prior else None):
            raise RecordConflict('行动责任与时间已有变化，请刷新后核对。')
        terms = json.loads(prior['terms_json']) if prior else {}
        terms.update({key: value for key, value in values.items() if key in TERM_FIELDS})
        title = values.get('title', record['title'])
        changed = title != record['title'] or terms != (json.loads(prior['terms_json']) if prior else {})
        if changed:
            stamp = max(now, record['updated_at'] + .000001, (prior['updated_at'] + .000001) if prior else now)
            # Draft changes must not leave a differently timed pending proposal
            # attached. Its rejection is kept in proposal history.
            db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND status='pending' AND "
                '(id=? OR id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))',
                (stamp, owner, record['proposal_id'], owner, record_id))
            db.execute('UPDATE crm_records SET title=?,proposal_id=NULL,updated_at=? WHERE owner=? AND id=?',
                (title, stamp, owner, record_id))
            db.execute('INSERT INTO crm_action_terms VALUES (?,?,?,?) ON CONFLICT(owner,record_id) '
                'DO UPDATE SET terms_json=excluded.terms_json,updated_at=excluded.updated_at',
                (owner, record_id, json.dumps(terms, ensure_ascii=False, allow_nan=False), stamp))
    return crm.get_record(owner, record_id)
