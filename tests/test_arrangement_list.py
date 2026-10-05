"""Queue filtering is complete before SQL pagination, with no read side effects."""
import json
import pytest

from secretary.arrangement_time import normalize_time, time_end, time_start
from secretary.matters import MatterService
from tests.test_arrangement_operations import world, make_plan, execution, command, counts, OWNER, NOW


def deadline(w, identifier, text):
    spec = normalize_time(text, NOW)
    db = w['crm']._db
    data = json.loads(db.execute('SELECT data_json FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()[0])
    data['settle_deadline'] = {'strength': 'target', 'time_spec': spec}
    db.execute('UPDATE crm_secretary_plans SET data_json=?,deadline_end_at=? WHERE id=?', (json.dumps(data), time_end(spec), identifier))


def test_more_than_200_rows_filter_before_page_and_counts_complete(world):
    for index in range(225):
        identifier = make_plan(world, title='虚构未定' + str(index))
        if index >= 220:
            deadline(world, identifier, '本周内')
    db = world['crm']._db
    before = db.total_changes
    result = world['queue'].list(OWNER, view='week', limit=2, offset=2)
    assert result['total'] == 5 and len(result['items']) == 2
    assert result['counts']['total'] == 225 and result['counts']['undated'] == 220
    assert result['counts']['week'] == 5
    assert db.total_changes == before and counts(world) == (0, 0, 0)


def test_no_deadline_is_in_default_all_but_not_execution_month(world):
    identifier = make_plan(world, proposed_execution=execution('2026-11-10'))
    default = world['queue'].list(OWNER)
    assert default['items'][0]['plan_id'] == identifier
    assert default['counts']['undated'] == 1
    local = world['queue'].list(OWNER, view='month')
    assert local['items'] == [] and local['counts']['total'] == 1


def test_same_customer_multiple_projects_are_isolated_before_count(world):
    crm, workspace = world['crm'], world['workspace']
    customer = crm.create_customer(OWNER, {'name': '虚构多项目单位'}, NOW)
    projects = [workspace.create_opportunity(OWNER, customer['id'], {'name': '虚构项目' + str(index)}) for index in range(2)]
    first = make_plan(world, customer_id=customer['id'], opportunity_id=projects[0]['id'])
    second = make_plan(world, customer_id=customer['id'], opportunity_id=projects[1]['id'])
    deadline(world, first, '本周内')
    deadline(world, second, '本周内')
    result = world['queue'].list(OWNER, customer_id=customer['id'], opportunity_id=projects[0]['id'])
    assert [item['plan_id'] for item in result['items']] == [first]
    assert result['counts']['total'] == result['counts']['week'] == 1


def test_matter_scope_multiple_links_never_duplicates_same_plan(world):
    service = MatterService(world['crm'], clock=lambda: NOW)
    matter = service.create(OWNER, {'title': '虚构归组目标', 'request_id': 'new-matter'})['matter']
    identifier = make_plan(world)
    row = world['crm']._db.execute('SELECT record_id FROM crm_secretary_plans WHERE id=?', (identifier,)).fetchone()
    with service._transaction() as db:
        service._link(db, OWNER, matter['id'], 'plan', identifier, 'plan', NOW)
        service._link(db, OWNER, matter['id'], 'record', row['record_id'], 'source', NOW)
    result = world['queue'].list(OWNER, matter_id=matter['id'])
    assert [item['plan_id'] for item in result['items']] == [identifier] and result['total'] == 1


def test_today_overdue_and_unhandled_past_check_not_future_execution(world):
    overdue = make_plan(world)
    deadline(world, overdue, '2026-10-03')
    old_check = make_plan(world)
    command(world, old_check, 'set_check', next_check={'time_spec': normalize_time('2026-10-02', NOW, role='check')})
    future = make_plan(world, proposed_execution=execution('2026-11-12'))
    result = world['queue'].list(OWNER, view='today')
    assert {item['plan_id'] for item in result['items']} == {overdue, old_check}
    assert result['counts']['total'] == 3 and result['counts']['today'] == 2
    assert future not in {item['plan_id'] for item in result['items']}


def test_hidden_and_other_owner_sources_are_excluded(world):
    visible = make_plan(world)
    hidden = make_plan(world)
    db = world['crm']._db
    db.execute('UPDATE crm_records SET hidden=1 WHERE id=(SELECT record_id FROM crm_secretary_plans WHERE id=?)', (hidden,))
    assert world['queue'].list(OWNER)['total'] == 1
    assert world['queue'].list('another-owner')['total'] == 0
    assert world['queue'].list(OWNER)['items'][0]['plan_id'] == visible


def test_blocker_filter_runs_before_sql_page(world):
    first = make_plan(world, proposed_execution=execution('2026-10-08'))
    second = make_plan(world)
    result = world['queue'].list(OWNER, blocker='missing_clock', limit=1)
    assert result['total'] == 1 and result['items'][0]['plan_id'] == first
    assert second != result['items'][0]['plan_id']


def test_stale_offset_clamps_to_last_real_page(world):
    for index in range(3):
        make_plan(world, title='虚构分页' + str(index))
    result = world['queue'].list(OWNER, limit=2, offset=100)
    assert result['offset'] == 2 and len(result['items']) == 1 and result['total'] == 3


def test_unknown_legacy_json_is_visible_read_only_without_guessing_times(world):
    identifier = make_plan(world)
    db = world['crm']._db
    db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE id=?', ('not-json', identifier))
    before = db.total_changes
    result = world['queue'].list(OWNER)
    assert result['total'] == 1 and result['items'][0]['plan_id'] == identifier
    assert result['items'][0]['proposed_execution'] is None
    assert db.total_changes == before


def test_already_settled_or_abandoned_is_not_counted_overdue_or_due_today(world):
    identifiers = [make_plan(world) for _ in range(4)]
    for identifier in identifiers:
        deadline(world, identifier, '2026-10-03')
    command(world, identifiers[0], 'confirm_arrangement')
    command(world, identifiers[1], 'abandon_coordination')
    command(world, identifiers[2], 'pause')
    for identifier in identifiers[:2]:
        assert 'deadline_overdue' not in world['queue'].get(OWNER, identifier)['attention_flags']
    result = world['queue'].list(OWNER, state='all', view='today')
    assert {item['plan_id'] for item in result['items']} == set(identifiers[2:])
    assert result['counts']['overdue'] == 2 and result['counts']['today'] == 2


@pytest.mark.parametrize('filters', [{'view': 'bad'}, {'state': 'bad'}, {'limit': True}, {'offset': -1}, {'customer_id': True}])
def test_invalid_filters_reject(world, filters):
    with pytest.raises(ValueError):
        world['queue'].list(OWNER, **filters)
