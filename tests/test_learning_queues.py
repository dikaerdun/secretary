"""Customer promises should remain visible without becoming personal scheduling chores."""
from secretary.customer_store import CustomerStore


def test_waiting_promises_are_not_personal_needs_time(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-learning.sqlite3')
    now = 1790992800.0
    try:
        customer = crm.create_customer('learner', {'name': '虚构演练机构'}, now)
        records = {}
        for executor in ('self', 'customer', 'team', 'unknown', 'legacy'):
            row = crm.create_record('learner', {'title': executor + '的行动',
                'kind': 'action', 'status': 'following', 'customer_id': customer['id']}, now)
            records[executor] = row
            if executor != 'legacy':
                crm.save_action_terms('learner', row['id'], {'executor_kind': executor}, now)
        queue = crm.list_records('learner', queue='needs_time', page_size=2)
        assert queue['total'] == 3
        second = crm.list_records('learner', queue='needs_time', page=2, page_size=2)
        visible = {row['id'] for row in queue['items'] + second['items']}
        assert visible == {records[k]['id'] for k in ('self', 'unknown', 'legacy')}
        # Waiting commitments are retained in ordinary customer follow-up lists.
        assert crm.list_records('learner', customer_id=customer['id'], kind='action')['total'] == 5
        assert crm.dashboard('learner', now)['queues']['needs_time']['total'] == 3
        assert crm.list_records('unrelated', queue='needs_time')['total'] == 0
    finally:
        crm.close()


def test_agenda_exposes_real_linked_record_kind_and_status(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-agenda.sqlite3')
    now = 1790992800.0
    try:
        records = []
        for index, kind in enumerate(('action', 'note', None)):
            crm.execute('learner', f'propose-{index}', {'action': 'propose',
                'title': f'演练安排{index}', 'remind_at': now + (index + 1) * 3600, 'duration_minutes': 30}, now)
            proposal_id = crm._db.execute('SELECT max(id) FROM proposals').fetchone()[0]
            if kind:
                record = crm.create_record('learner', {'title': f'演练安排{index}',
                    'kind': kind, 'status': 'following'}, now)
                crm.link_proposal('learner', record['id'], proposal_id, now)
                records.append(record)
            crm.execute('learner', f'confirm-{index}', {'action': 'confirm', 'proposal_id': proposal_id}, now)
            if kind == 'note':
                # Linking a proposal makes a record an action. An older or
                # manually reclassified record can still be a note afterwards.
                crm.update_record('learner', record['id'], {'kind': 'note'}, now + 1)
        items = crm.agenda('learner', now=now)['items']
        assert [(item['record_kind'], item['record_status']) for item in items] == [
            ('action', 'following'), ('note', 'following'), (None, None)]
        crm.update_record('learner', records[0]['id'], {'status': 'done'}, now + 1)
        items = crm.agenda('learner', now=now)['items']
        action = next(item for item in items if item['record_id'] == records[0]['id'])
        assert action['record_status'] == 'done'
        assert crm.agenda('unrelated', now=now)['items'] == []
    finally:
        crm.close()
