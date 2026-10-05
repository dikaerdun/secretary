"""Action target project is explicit and independent of the original discussion."""
import copy
import json

import httpx
import pytest

from secretary.action_origin_scope import saved_scope
from test_round08_planning_boundaries import world, run_async, OWNER, NOW


@pytest.fixture(autouse=True)
def forbid_external_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('R10 only uses offline advisor and local TestServer')
    monkeypatch.setattr(httpx.AsyncClient, '__init__', forbidden)


REPLY = {'answer': '建议仍是整体联系人讨论，不是客户事实。', 'next_moves': [
    {'title': '核对加密接口', 'reason': '范围待确认', 'contact_hint': '林工', 'preparation': '准备问题', 'success_signal': '明确接口'},
    {'title': '核对密钥部署', 'reason': '部署条件待确认', 'contact_hint': '林工', 'preparation': '整理材料', 'success_signal': '明确条件'}], 'questions': [], 'risks': []}


class OfflineAdvisor:
    async def reply(self, *args):
        return copy.deepcopy(REPLY)


async def setup(w, *, fixed=False, source=None):
    w.focus = w.crm.create_contact(OWNER, w.unit['id'], {'name': '林工', 'department': '技术'}, NOW)
    w.peer = w.crm.create_contact(OWNER, w.unit['id'], {'name': '林工', 'department': '采购'}, NOW)
    w.project = w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'],
        {'expected_revision': w.project['revision'], 'contact_ids': [w.focus['id']]})
    w.second = w.web.sales_workspace.create_opportunity(OWNER, w.unit['id'], {'name': '合成第二项目', 'contact_ids': [w.focus['id']]})
    w.empty = w.web.sales_workspace.create_opportunity(OWNER, w.unit['id'], {'name': '合成非参与项目'})
    w.web.discussions.advisor = OfflineAdvisor()
    body = {'customer_id': w.unit['id'], 'contact_id': w.focus['id'], 'request_id': 'r10-focus-thread'}
    if fixed:
        body['opportunity_id'] = w.project['id']
    if source:
        row = w.crm.create_record(OWNER, {'title': '合成明确项目来源', 'content': '项目A的资料，不是项目B的承诺', 'customer_id': w.unit['id']}, NOW)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        body['source_record_id'] = row['id']
    w.thread = (await w.call('POST', '/api/sales-discussions', body, 201))['thread']
    await w.call('POST', f'/api/sales-discussions/{w.thread["id"]}/messages', {'text': '我下一步如何与林工推进？', 'request_id': 'r10-first-question'})
    return await dto(w)


async def dto(w):
    return await w.call('GET', f'/api/sales-discussions/{w.thread["id"]}')


def message(data):
    return next(x for x in reversed(data['messages']) if x['role'] == 'assistant')


def body(data, project, *, people=None):
    option = next(x for x in data['action_project_options']['projects'] if x['id'] == project['id'])
    value = {'opportunity_id': project['id'], 'expected_project_version': option['version'], 'expected_snapshot': message(data)['snapshot']}
    if people is not None:
        value.update(contact_ids=people, expected_contact_version=option['action_contact_options']['version'])
    return value


def path(w, msg, index=1):
    return f'/api/sales-discussions/{w.thread["id"]}/messages/{msg["id"]}/actions/{index}/adopt'


def state(w):
    names = [x[0] for x in w.crm._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: [tuple(r) for r in w.crm._db.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in names}


@run_async
async def test_http_two_explicit_targets_preserve_original_sources_people_and_baselines(tmp_path):
    async with world(tmp_path) as w:
        first_dto = await setup(w)
        change_count = w.crm._db.total_changes
        first_dto = await dto(w)
        assert w.crm._db.total_changes == change_count
        assert [x['id'] for x in first_dto['action_project_options']['projects']] == [w.project['id'], w.second['id']]
        assert first_dto['action_project_options']['fixed_project_id'] is None
        first = (await w.call('POST', path(w, message(first_dto)), body(first_dto, w.project, people=[w.peer['id']])))['record']
        fresh = await dto(w)
        second = (await w.call('POST', path(w, message(fresh), 2), body(fresh, w.second)))['record']
        after = await dto(w)
        assert after['thread']['opportunity_id'] is None
        assert [x['text'] for x in first_dto['messages']] == [x['text'] for x in after['messages']]
        for record, target, index in [(first, w.project, 1), (second, w.second, 2)]:
            detail = await w.call('GET', f'/api/records/{record["id"]}')
            assert detail['record']['opportunity_id'] == target['id']
            event = w.web.timeline.get_event(OWNER, f'record:{record["id"]}')
            assert event['kind'] == 'reflection' and event['occurred_at'] is None
            assert event['related_event_key'] == f'discussion:{w.thread["id"]}'
            assert all(x['relation'] == 'about' for x in event['contact_relations'])
            baseline, found = saved_scope(w.crm._db, OWNER, 'discussion', f'{w.thread["id"]}:{message(after)["id"]}:{index}', record['id'])
            assert found and baseline['opportunity_id'] == target['id']
            origin = detail['action_origins']['items'][0]
            assert origin['scope']['opportunity_id'] is None and origin['scope_status'] == 'unchanged'
        assert first['action_contact_ids'] == sorted([w.focus['id'], w.peer['id']])
        assert second['action_contact_ids'] == [w.focus['id']]
        for table in ('tasks', 'proposals', 'crm_customer_facts'):
            assert w.crm._db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


@pytest.mark.parametrize('explicit', [False, True])
@run_async
async def test_omitted_legacy_or_explicit_unassigned_target_stays_unassigned_and_replays(tmp_path, explicit):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = {'opportunity_id': None, 'expected_snapshot': message(data)['snapshot']} if explicit else {}
        record = (await w.call('POST', path(w, message(data)), value))['record']
        before = state(w)
        again = (await w.call('POST', path(w, message(data)), value))['record']
        assert again['id'] == record['id'] and state(w) == before
        assert w.crm._db.execute("SELECT count(*) FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?", (OWNER, record['id'])).fetchone()[0] == 0


@pytest.mark.parametrize('invalid', ['nonmember', 'foreign_owner', 'other_unit', 'archived_project', 'archived_focus', 'bad_id'])
@run_async
async def test_invalid_target_scope_is_rejected_before_any_adoption_write(tmp_path, invalid):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = body(data, w.project)
        if invalid == 'nonmember':
            value['opportunity_id'] = w.empty['id']
        elif invalid in ('foreign_owner', 'other_unit'):
            owner = 'foreign' if invalid == 'foreign_owner' else OWNER
            unit = w.crm.create_customer(owner, {'name': '另一合成单位'}, NOW)
            value['opportunity_id'] = w.web.sales_workspace.create_opportunity(owner, unit['id'], {'name': '另一单位项目'})['id']
        elif invalid == 'archived_project':
            w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'], {'expected_revision': w.project['revision'], 'archived': True})
        elif invalid == 'archived_focus':
            w.crm.update_contact(OWNER, w.unit['id'], w.focus['id'], {'archived': True}, NOW)
        else:
            value['opportunity_id'] = True
        before = state(w)
        await w.call('POST', path(w, message(data)), value, 400 if invalid != 'foreign_owner' and invalid != 'other_unit' else 404)
        assert before == state(w)


@pytest.mark.parametrize('target', ['other', 'none'])
@run_async
async def test_fixed_project_suggestion_cannot_override_or_remove_its_project(tmp_path, target):
    async with world(tmp_path) as w:
        data = await setup(w, fixed=True)
        assert data['action_project_options']['projects'] == []
        before = state(w)
        await w.call('POST', path(w, message(data)), {'opportunity_id': w.second['id'] if target == 'other' else None,
            'expected_snapshot': message(data)['snapshot'], 'expected_project_version': data['action_contact_options']['version']}, 400)
        assert state(w) == before


@pytest.mark.parametrize('change', ['project', 'membership', 'people_token'])
@run_async
async def test_target_and_people_option_versions_are_independently_required(tmp_path, change):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = body(data, w.project, people=[w.peer['id']])
        if change == 'project':
            w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'], {'expected_revision': w.project['revision'], 'scope': '已修订部署范围'})
        elif change == 'membership':
            w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], {'expected_revision': w.project['revision'], 'contact_id': w.focus['id'], 'roles': ['technical_reviewer']})
        else:
            value['expected_contact_version'] = data['action_contact_options']['version']
        before = state(w)
        result = await w.call('POST', path(w, message(data)), value, 400)
        assert '联系人或项目参与关系' in result['error']
        assert state(w) == before


@run_async
async def test_current_target_token_does_not_make_old_advice_after_new_question_fresh(tmp_path):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = body(data, w.project)
        await w.call('POST', f'/api/sales-discussions/{w.thread["id"]}/messages', {'text': '补充新的现场问题，请重新讨论。', 'request_id': 'new-question'})
        before = state(w)
        await w.call('POST', path(w, message(data)), value, 400)
        assert state(w) == before


@run_async
async def test_explicit_source_project_is_not_borrowed_by_another_action_target(tmp_path):
    async with world(tmp_path) as w:
        data = await setup(w, source=True)
        assert [x['id'] for x in data['action_project_options']['projects']] == [w.project['id']]
        value = {**body(data, w.project), 'opportunity_id': w.second['id']}
        before = state(w)
        await w.call('POST', path(w, message(data)), value, 400)
        assert state(w) == before


@run_async
async def test_completed_replay_uses_first_target_and_keeps_current_manual_project_people(tmp_path):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = body(data, w.project, people=[w.peer['id']])
        record = (await w.call('POST', path(w, message(data)), value))['record']
        w.web.sales_workspace.link(OWNER, 'record', record['id'], w.second['id'])
        event = w.web.timeline.get_event(OWNER, f'record:{record["id"]}')
        w.web.timeline.save_context(OWNER, event['key'], {'expected_revision': event['revision'], 'kind': 'reflection', 'occurred_at': None,
            'contact_relations': [{'contact_id': w.focus['id'], 'relation': 'about'}]})
        before = state(w)
        repeated = (await w.call('POST', path(w, message(data)), value))['record']
        assert repeated['id'] == record['id'] and repeated['action_contact_ids'] == [w.focus['id']]
        assert state(w) == before
        await w.call('POST', path(w, message(data)), {**value, 'opportunity_id': w.second['id']}, 400)
        assert state(w) == before
        detail = await w.call('GET', f'/api/records/{record["id"]}')
        assert detail['record']['opportunity_id'] == w.second['id']
        assert detail['action_origins']['items'][0]['scope_changed_fields'] == ['project', 'contacts']


@pytest.mark.parametrize('legacy', ['missing', 'malformed'])
@run_async
async def test_unknown_historical_target_is_not_inferred_from_current_link(tmp_path, legacy):
    async with world(tmp_path) as w:
        data = await setup(w)
        record = (await w.call('POST', path(w, message(data)), {}))['record']
        # Simulate a historical missing/corrupt sidecar, not a fabricated business
        # adoption. All real actions above are created through the public API.
        with w.crm._transaction() as db:
            if legacy == 'missing':
                db.execute('DELETE FROM crm_action_origin_scopes WHERE record_id=?', (record['id'],))
            else:
                db.execute('UPDATE crm_action_origin_scopes SET scope_json=? WHERE record_id=?', (json.dumps({'version': 'invalid'}), record['id']))
        w.web.sales_workspace.link(OWNER, 'record', record['id'], w.project['id'])
        before = state(w)
        await w.call('POST', path(w, message(data)), body(await dto(w), w.project), 400)
        assert state(w) == before
        again = (await w.call('POST', path(w, message(data)), {}))['record']
        assert again['id'] == record['id'] and state(w) == before


@run_async
async def test_people_binding_failure_rolls_back_target_link_baseline_and_adoption_then_retry_once(tmp_path, monkeypatch):
    async with world(tmp_path) as w:
        data = await setup(w)
        value = body(data, w.project)
        before = state(w)
        original = w.web.timeline.save_context
        def fail(*args, **kwargs):
            raise ValueError('合成第二阶段人物写入失败')
        monkeypatch.setattr(w.web.timeline, 'save_context', fail)
        await w.call('POST', path(w, message(data)), value, 400)
        assert state(w) == before
        monkeypatch.setattr(w.web.timeline, 'save_context', original)
        record = (await w.call('POST', path(w, message(data)), value))['record']
        assert w.crm._db.execute('SELECT count(*) FROM crm_records').fetchone()[0] == 1
        assert (await w.call('POST', path(w, message(data)), value))['record']['id'] == record['id']
