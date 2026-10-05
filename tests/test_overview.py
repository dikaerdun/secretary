"""A synthetic work/project overview, with full counts and honest date semantics."""
import json
import re
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.overview import OverviewService
from secretary.review_queue import ReviewQueue
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SHANGHAI).timestamp()


@pytest.fixture
def context(tmp_path):
    crm = CustomerStore(tmp_path / 'overview-synthetic.sqlite3')
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer('me', {'name': '星浦医院', 'amount_cents': 900000000}, NOW)
    first = workspace.create_opportunity('me', customer['id'], {'name': '病历签名', 'amount_cents': 18000000,
        'amount_type': 'estimate', 'approval': 'unconfirmed', 'stage': 'proposal', 'blockers': '采购负责人待核实'})
    second = workspace.create_opportunity('me', customer['id'], {'name': '数据加密', 'amount_type': 'unknown'})
    foreign = crm.create_customer('other', {'name': '不可见客户'}, NOW)
    foreign_project = workspace.create_opportunity('other', foreign['id'], {'name': '不可见项目'})
    yield crm, workspace, customer, first, second, foreign, foreign_project
    crm.close()


def action(crm, customer, title, *, executor='self', status='following', owner='me', terms=None):
    record = crm.create_record(owner, {'title': title, 'content': title, 'customer_id': customer['id'],
        'kind': 'action', 'status': status}, NOW)
    crm.save_action_terms(owner, record['id'], {'executor_kind': executor, **(terms or {})}, NOW)
    return record


def schedule(crm, record, when, *, owner='me', confirmed=True):
    scheduled_at = min(NOW, when-3600)
    reply = crm.execute(owner, 'propose-'+str(record['id']), {'action': 'propose', 'title': record['title'],
        'remind_at': when}, scheduled_at)
    identifier = int(re.search(r'P(\d+)', reply)[1])
    crm.link_proposal(owner, record['id'], identifier, scheduled_at)
    if confirmed:
        crm.execute(owner, 'confirm-'+str(identifier), {'action': 'confirm', 'proposal_id': identifier}, scheduled_at)
    return crm.get_proposal(owner, identifier)


def overview(context, review_queue=None, limit=20):
    crm, workspace, *_ = context
    return OverviewService(crm, workspace, review_queue, clock=lambda: NOW).get('me', limit=limit)


def test_two_projects_use_explicit_links_and_keep_legacy_money_out(context):
    crm, workspace, customer, first, second, *_ = context
    mine = action(crm, customer, '准备签名方案')
    wait = action(crm, customer, '等客户提供测试接口', executor='customer', terms={'check_date': '2026-10-02'})
    done = action(crm, customer, '完成现状调研', status='done')
    another = action(crm, customer, '开展加密试点')
    unrelated = action(crm, customer, '客户公司层面事项')
    for record in (mine, wait, done):
        workspace.link('me', 'record', record['id'], first['id'])
    workspace.link('me', 'record', another['id'], second['id'])
    result = overview(context)
    assert result['counts']['my_actions'] == 3
    assert result['counts']['waiting_actions'] == 1
    assert result['counts']['active_projects'] == 2
    assert {row['id'] for row in result['my_actions']} == {mine['id'], another['id'], unrelated['id']}
    projects = {row['id']: row for row in result['projects']}
    assert projects[first['id']]['open_actions'] == 2
    assert projects[first['id']]['waiting'] == 1
    assert projects[first['id']]['progress'] == {'done': 1, 'total': 3}
    assert projects[first['id']]['needs_time'] == 1
    assert projects[first['id']]['amount_cents'] == 18000000
    assert projects[first['id']]['type'] == 'estimate'
    assert projects[first['id']]['approval'] == 'unconfirmed'
    assert projects[second['id']]['amount_cents'] is None
    assert projects[second['id']]['open_actions'] == 1
    assert next(row for row in result['my_actions'] if row['id'] == unrelated['id'])['opportunity_id'] is None
    assert all(row['amount_cents'] != 900000000 for row in result['projects'])


def test_source_correction_and_customer_change_make_links_stale_not_counted(context):
    crm, workspace, customer, first, second, *_ = context
    edited = action(crm, customer, '原始签名资料')
    moved = action(crm, customer, '更换客户归属')
    for record in (edited, moved):
        workspace.link('me', 'record', record['id'], first['id'])
    crm.update_record('me', edited['id'], {'content': '后来更正的资料'}, NOW+1)
    another_customer = crm.create_customer('me', {'name': '另一家企业'}, NOW)
    crm.update_record('me', moved['id'], {'customer_id': another_customer['id']}, NOW+1)
    result = overview(context)
    assert result['counts']['my_actions'] == 2
    assert {row['entity_id'] for row in result['stale_links']} == {edited['id'], moved['id']}
    assert all(row['stale'] for row in result['stale_links'])
    assert all(row['opportunity_id'] is None for row in result['my_actions'])
    assert next(row for row in result['projects'] if row['id'] == first['id'])['progress']['total'] == 0
    workspace.link('me', 'record', edited['id'], second['id'])
    refreshed = overview(context)
    assert next(row for row in refreshed['projects'] if row['id'] == second['id'])['progress']['total'] == 1


def test_only_completed_appointment_keeps_follow_up_open_while_hidden_record_stays_out(context):
    crm, workspace, customer, first, *_ = context
    completed = action(crm, customer, '任务已完成但记录未手改状态')
    proposal = schedule(crm, completed, NOW+3600)
    crm.execute('me', 'complete-example', {'action': 'complete', 'task_id': proposal['task_id']}, NOW)
    hidden = action(crm, customer, '隐藏记录')
    workspace.link('me', 'record', completed['id'], first['id'])
    workspace.link('me', 'record', hidden['id'], first['id'])
    with crm._transaction() as db:
        db.execute('UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?', ('me', hidden['id']))
    result = overview(context)
    assert result['counts']['my_actions'] == 1
    assert result['counts']['overdue'] == 0
    assert next(row for row in result['projects'] if row['id'] == first['id'])['progress'] == {'done': 0, 'total': 1}
    assert any(row['entity_id'] == hidden['id'] for row in result['stale_links'])
    workspace.complete_record('me', completed['id'], {'request_id': 'follow-up-result', 'result': '已核对落实'})
    finished = overview(context)
    assert finished['counts']['my_actions'] == 0
    assert next(row for row in finished['projects'] if row['id'] == first['id'])['progress'] == {'done': 1, 'total': 1}


def test_pending_schedule_is_review_not_a_confirmed_appointment(context):
    crm, workspace, customer, first, *_ = context
    record = action(crm, customer, '待确认安排')
    schedule(crm, record, NOW+3600, confirmed=False)
    workspace.link('me', 'record', record['id'], first['id'])
    result = overview(context, ReviewQueue(crm, clock=lambda: NOW))
    assert result['counts']['review_pending'] == 1
    assert result['counts']['today_schedules'] == 0
    assert result['projects'][0]['next_schedule'] is None
    assert result['my_actions'][0]['remind_at'] is None
    assert result['my_actions'][0]['proposal_remind_at'] == NOW+3600
    assert result['my_actions'][0]['needs_time'] is False


def test_date_only_overdue_is_yesterday_not_today_or_artificial_midnight(context):
    crm, _, customer, *_ = context
    yesterday = action(crm, customer, '昨天截止', terms={'deadline_date': '2026-10-02'})
    today = action(crm, customer, '今天截止', terms={'deadline_date': '2026-10-03'})
    tomorrow = action(crm, customer, '明天回访', executor='customer', terms={'check_date': '2026-10-04'})
    earlier = action(crm, customer, '精确检查时间已过', executor='team', terms={'check_at': NOW-1})
    now = action(crm, customer, '精确截止刚好现在', terms={'deadline_at': NOW})
    result = overview(context)
    assert {row['id'] for row in result['overdue']} == {yesterday['id'], earlier['id']}
    day_item = next(row for row in result['overdue'] if row['id'] == yesterday['id'])
    assert day_item['action_terms']['deadline_date'] == '2026-10-02'
    assert 'deadline_at' not in day_item['action_terms']
    assert '2026-10-02' in day_item['overdue_reason']
    assert not {today['id'], tomorrow['id'], now['id']} & {row['id'] for row in result['overdue']}


def test_today_and_next_schedule_are_confirmed_and_shanghai_day_scoped(context):
    crm, workspace, customer, first, *_ = context
    day_start = datetime(2026, 10, 3, tzinfo=SHANGHAI).timestamp()
    earlier = action(crm, customer, '当天早间安排')
    later = action(crm, customer, '当天后续安排')
    midnight = action(crm, customer, '下一天零点')
    for record, stamp in ((earlier, day_start+3600), (later, NOW+3600), (midnight, day_start+86400)):
        schedule(crm, record, stamp)
        workspace.link('me', 'record', record['id'], first['id'])
    result = overview(context)
    assert result['counts']['today_schedules'] == 2
    assert {row['title'] for row in result['today_schedules']} == {earlier['title'], later['title']}
    assert next(row for row in result['projects'] if row['id'] == first['id'])['next_schedule']['title'] == later['title']
    assert result['counts']['overdue'] == 1


def test_counts_are_full_even_when_lists_have_more_than_twenty(context):
    crm, workspace, customer, *_ = context
    for number in range(26):
        action(crm, customer, f'我的行动{number}')
        action(crm, customer, f'客户反馈{number}', executor='customer')
        crm.create_record('me', {'title': f'随口记{number}', 'content': '尚未补归属和输出目的'}, NOW+number)
        workspace.create_opportunity('me', customer['id'], {'name': f'项目{number}'})
    result = overview(context, limit=20)
    assert result['counts']['my_actions'] == 26
    assert result['counts']['waiting_actions'] == 26
    assert result['counts']['unfiled'] == 26
    assert result['totals']['projects'] == 28
    assert result['counts']['active_projects'] == 28
    for name in ('my_actions', 'waiting_actions', 'unfiled', 'projects'):
        assert len(result[name]) == 20
        assert result['truncated'][name] is True
    assert result['limit'] == 20


def test_owner_isolation_in_every_list_and_count(context):
    crm, workspace, customer, first, _, foreign, foreign_project = context
    foreign_record = action(crm, foreign, '不应显示的行动', owner='other', terms={'deadline_date': '2026-01-01'})
    schedule(crm, foreign_record, NOW+3600, owner='other')
    workspace.link('other', 'record', foreign_record['id'], foreign_project['id'])
    crm.create_record('other', {'title': '不应显示的原话', 'content': '秘密内容'}, NOW)
    result = overview(context, ReviewQueue(crm, clock=lambda: NOW))
    serialized = json.dumps(result, ensure_ascii=False)
    assert '不应显示' not in serialized and '不可见' not in serialized and '秘密内容' not in serialized
    assert result['counts']['my_actions'] == 0
    assert result['counts']['today_schedules'] == 0
    assert result['counts']['review_pending'] == 0
    assert result['counts']['active_projects'] == 2
    assert '"owner"' not in serialized


def test_closed_and_archived_projects_have_honest_active_counts(context):
    crm, workspace, customer, first, second, *_ = context
    workspace.update_opportunity('me', customer['id'], first['id'], {'expected_revision': 1, 'stage': 'won'})
    workspace.update_opportunity('me', customer['id'], second['id'], {'expected_revision': 1, 'archived': True})
    result = overview(context)
    assert result['counts']['active_projects'] == 0
    assert result['totals']['projects'] == 1
    assert result['projects'][0]['stage'] == 'won'


def test_review_deferred_dismissed_do_not_count_as_pending(context):
    crm, _, _, *_ = context
    class Queue:
        def all_items(self, owner):
            assert owner == 'me'
            return [{'key': 'pending', 'decision_state': 'pending'},
                    {'key': 'deferred', 'decision_state': 'deferred'},
                    {'key': 'dismissed', 'decision_state': 'dismissed'}]
    result = overview(context, Queue())
    assert result['counts']['review_pending'] == 1
    assert [row['key'] for row in result['review_pending']] == ['pending']


def test_invalid_limits_rejected(context):
    for value in (0, -1, 201, True, 1.5, '20'):
        with pytest.raises(ValueError):
            overview(context, limit=value)


def test_material_and_visit_source_changes_are_reported_stale(context):
    crm, workspace, customer, first, *_ = context
    with crm._transaction() as db:
        db.execute('CREATE TABLE crm_materials(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,revision INTEGER,current_version_id INTEGER)')
        db.execute('CREATE TABLE crm_visits(id INTEGER PRIMARY KEY,owner TEXT,customer_id INTEGER,title TEXT,revision INTEGER,occurred_at REAL)')
        db.execute('CREATE TABLE crm_visit_sources(owner TEXT,visit_id INTEGER,material_id INTEGER,role TEXT)')
        db.execute('CREATE TABLE crm_visit_records(owner TEXT,visit_id INTEGER,record_id INTEGER)')
        db.execute('CREATE TABLE crm_visit_source_choices(owner TEXT,visit_id INTEGER,material_id INTEGER,use_status TEXT,revision INTEGER)')
        db.execute('INSERT INTO crm_materials VALUES (41,?,?,?,?,?)', ('me', customer['id'], '录音整理', 1, 101))
        db.execute('INSERT INTO crm_materials VALUES (42,?,?,?,?,?)', ('me', customer['id'], '口述复盘', 1, 102))
        db.execute('INSERT INTO crm_visits VALUES (51,?,?,?,?,?)', ('me', customer['id'], '现场交流', 1, NOW))
        db.execute('INSERT INTO crm_visit_sources VALUES (?,?,?,?)', ('me', 51, 42, 'recap'))
        db.execute('INSERT INTO crm_visit_source_choices VALUES (?,?,?,?,?)', ('me', 51, 42, 'included', 1))
    source_record = action(crm, customer, '当日现场讨论')
    with crm._transaction() as db:
        db.execute('INSERT INTO crm_visit_records VALUES (?,?,?)', ('me', 51, source_record['id']))
    workspace.link('me', 'material', 41, first['id'])
    workspace.link('me', 'visit', 51, first['id'])
    assert overview(context)['stale_links'] == []
    with crm._transaction() as db:
        db.execute('UPDATE crm_materials SET revision=2,current_version_id=103 WHERE id=41')
        db.execute('UPDATE crm_visit_source_choices SET use_status=?,revision=2 WHERE owner=? AND visit_id=?',
                   ('excluded', 'me', 51))
    result = overview(context)
    assert {(row['entity_type'], row['entity_id']) for row in result['stale_links']} == {('material', 41), ('visit', 51)}
    workspace.link('me', 'material', 41, first['id'])
    workspace.link('me', 'visit', 51, first['id'])
    assert overview(context)['stale_links'] == []
    crm.update_record('me', source_record['id'], {'content': '核对后的讨论文字'}, NOW+1)
    assert {(row['entity_type'], row['entity_id']) for row in overview(context)['stale_links']} == {('visit', 51)}


def test_real_review_counts_remain_full_and_read_does_not_mutate(context):
    crm, _, customer, *_ = context
    for number in range(26):
        record = action(crm, customer, f'待核对安排{number}')
        schedule(crm, record, NOW+3600+number*3600, confirmed=False)
    queue = ReviewQueue(crm, clock=lambda: NOW)
    before = {table: crm._db.execute('SELECT count(*) FROM '+table).fetchone()[0]
              for table in ('tasks', 'proposals', 'crm_records', 'crm_opportunity_links')}
    result = overview(context, queue)
    assert result['counts']['review_pending'] == 26
    assert len(result['review_pending']) == 20
    assert result['truncated']['review_pending'] is True
    assert result['counts']['today_schedules'] == 0
    after = {table: crm._db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in before}
    assert before == after


def test_query_count_is_bounded_instead_of_n_plus_one_for_linked_actions(context):
    crm, workspace, customer, first, *_ = context
    record = action(crm, customer, '一条大正文')
    crm.update_record('me', record['id'], {'content': '正文'*9000}, NOW)
    workspace.link('me', 'record', record['id'], first['id'])
    queries = []
    crm._db.set_trace_callback(queries.append)
    single = overview(context)
    single_count = len(queries)
    crm._db.set_trace_callback(None)
    assert len(single['my_actions'][0]['content']) == 500
    assert 'original_content' not in single['my_actions'][0]
    for number in range(50):
        record = action(crm, customer, f'行动{number}')
        workspace.link('me', 'record', record['id'], first['id'])
    queries = []
    crm._db.set_trace_callback(queries.append)
    many = overview(context)
    crm._db.set_trace_callback(None)
    assert many['counts']['my_actions'] == 51
    assert len(queries) == single_count
    assert len(queries) < 35


def test_archived_project_link_is_explicitly_out_of_statistics(context):
    crm, workspace, customer, first, *_ = context
    record = action(crm, customer, '归档前行动')
    workspace.link('me', 'record', record['id'], first['id'])
    workspace.update_opportunity('me', customer['id'], first['id'], {'expected_revision': 1, 'archived': True})
    result = overview(context)
    assert result['my_actions'][0]['opportunity_id'] is None
    assert result['stale_links'][0]['entity_id'] == record['id']
    assert result['stale_links'][0]['stale'] is True
    assert '归档' in result['stale_links'][0]['reason']


def test_unlinked_legacy_tasks_keep_separate_units_and_include_overdue(context):
    crm, *_ = context
    for number, stamp in enumerate((NOW-3600, NOW+3600, NOW+86400)):
        created = NOW-7200
        reply = crm.execute('me', f'legacy-{number}', {'action': 'propose', 'title': f'旧版日程{number}',
            'remind_at': stamp}, created)
        identifier = int(re.search(r'P(\d+)', reply)[1])
        crm.execute('me', f'legacy-confirm-{number}', {'action': 'confirm', 'proposal_id': identifier}, created)
    result = overview(context)
    assert result['counts']['my_actions'] == 0
    assert result['counts']['overdue'] == 0
    assert result['counts']['orphan_overdue'] == 1
    assert result['counts']['today_schedules'] == 2
    assert result['orphan_task_total'] == 3
    assert len(result['orphan_tasks']) == 3
    assert result['orphan_task_summary'] == {'pending_total': 3, 'overdue': 1, 'today': 2, 'unscheduled': 0}
    assert all(row['record_id'] is None and row['task_id'] for row in result['orphan_tasks'])


def test_historical_proposal_link_is_not_misclassified_as_an_orphan(context):
    crm, workspace, customer, first, *_ = context
    record = action(crm, customer, '当前原日程')
    original = schedule(crm, record, NOW+3600)
    reply = crm.execute('me', 'pending-new-arrangement', {'action': 'propose', 'title': record['title'],
        'remind_at': NOW+86400}, NOW)
    new_identifier = int(re.search(r'P(\d+)', reply)[1])
    crm.link_proposal('me', record['id'], new_identifier, NOW)
    workspace.link('me', 'record', record['id'], first['id'])
    result = overview(context)
    assert result['orphan_task_total'] == 0
    assert result['today_schedules'][0]['task_id'] == original['task_id']
    assert result['today_schedules'][0]['record_id'] == record['id']
    assert result['my_actions'][0]['schedule_confirmed'] is True
    assert result['my_actions'][0]['remind_at'] == NOW+3600
    assert result['my_actions'][0]['proposal_remind_at'] == NOW+86400
    assert next(row for row in result['projects'] if row['id'] == first['id'])['next_schedule']['task_id'] == original['task_id']
