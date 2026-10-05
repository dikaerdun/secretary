"""Adoption-time scope, honest historical unknowns and crash-safe provenance."""
import copy
import json

import httpx
import pytest

from secretary.action_provenance import action_origins
from secretary.action_origin_scope import saved_scope
from test_round08_planning_boundaries import world, action, prepare, choose, adopt, run_async, OWNER, NOW


@pytest.fixture(autouse=True)
def no_external_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('R09 only uses offline services and local aiohttp')
    monkeypatch.setattr(httpx.AsyncClient, '__init__', forbidden)


def scope_row(w, origin_type, key, rid):
    return w.crm._db.execute('SELECT * FROM crm_action_origin_scopes WHERE owner=? AND origin_type=? AND origin_key=? AND record_id=?',
                            (OWNER, origin_type, str(key), rid)).fetchone()


async def origins(w, rid, run_id):
    before = w.crm._db.total_changes
    dto = await w.call('GET', f'/api/records/{rid}')
    assert w.crm._db.total_changes == before
    return next(x for x in dto['action_origins']['items'] if x.get('run_id') == run_id)


def people(w, rid, ids):
    event = w.web.timeline.get_event(OWNER, f'record:{rid}')
    return w.web.timeline.save_context(OWNER, event['key'], {'expected_revision': event['revision'], 'kind': 'reflection',
        'occurred_at': None, 'contact_relations': [{'contact_id': identifier, 'relation': 'about'} for identifier in ids]})


async def planned(w, row, request='scope-plan', **scope):
    run = await prepare(w, customer=False, **scope)
    item = next(x for x in run['items'] if (x.get('current') or {}).get('record_id') == row['id'])
    run = choose(w, run, item, remind_at=NOW+3600, duration_minutes=30, executor_kind='self')
    receipt, body = adopt(w, run, request)
    assert receipt['results'][0]['status'] == 'confirmed', receipt
    return run, receipt, body, item['id']


@run_async
async def test_global_plan_preserves_actual_three_dimensions_and_exact_navigation(tmp_path):
    async with world(tmp_path) as w:
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '合成林工'}, NOW)
        row = action(w)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        people(w, row['id'], [person['id']])
        run, _, body, item_id = await planned(w, row)
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_status'] == 'unchanged' and origin['same_customer'] is True
        assert origin['scope_changed_fields'] == [] and not origin['scope_needs_review']
        assert origin['scope_basis'] == 'adoption_snapshot'
        assert all(origin['scope'][key] is None for key in ('customer_id', 'opportunity_id', 'contact_id'))
        baseline = json.loads(scope_row(w, 'progress', f'{run["id"]}:{item_id}', row['id'])['scope_json'])
        assert baseline['customer_id'] == w.unit['id'] and baseline['opportunity_id'] == w.project['id']
        assert baseline['contact_ids'] == [person['id']] and set(baseline['known_fields']) == {'customer', 'project', 'contacts'}
        before = w.crm._db.total_changes
        assert w.progress.confirm(OWNER, run['id'], body)['replayed']
        assert w.crm._db.total_changes == before


@pytest.mark.parametrize('dimension', ['customer', 'project', 'contacts'])
@run_async
async def test_real_change_identifies_only_the_changed_dimension_and_keeps_baseline(tmp_path, dimension):
    async with world(tmp_path) as w:
        row = action(w)
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '合成林工'}, NOW)
        if dimension == 'project':
            w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        if dimension == 'contacts':
            people(w, row['id'], [person['id']])
        run, _, body, item_id = await planned(w, row)
        stored = scope_row(w, 'progress', f'{run["id"]}:{item_id}', row['id'])['scope_json']
        if dimension == 'customer':
            unit2 = w.crm.create_customer(OWNER, {'name': '不同合成单位'}, NOW)
            current = w.crm.get_record(OWNER, row['id'])
            w.crm.update_record(OWNER, row['id'], {'customer_id': unit2['id']}, NOW+1, expected_updated_at=current['updated_at'])
        elif dimension == 'project':
            second = w.web.sales_workspace.create_opportunity(OWNER, w.unit['id'], {'name': '合成项目B'})
            w.web.sales_workspace.link(OWNER, 'record', row['id'], second['id'])
        else:
            other = w.crm.create_contact(OWNER, w.unit['id'], {'name': '合成周工'}, NOW)
            people(w, row['id'], [other['id']])
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_status'] == 'changed' and origin['scope_changed_fields'] == [dimension]
        assert origin['same_customer'] is (dimension != 'customer')
        w.progress.confirm(OWNER, run['id'], body)
        assert scope_row(w, 'progress', f'{run["id"]}:{item_id}', row['id'])['scope_json'] == stored


@run_async
async def test_legacy_global_item_uses_retained_target_but_never_invents_contact_baseline(tmp_path):
    async with world(tmp_path) as w:
        row = action(w)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        run, _, _, item_id = await planned(w, row)
        # Only synthetic metadata is aged to represent a pre-R09 receipt.
        w.crm._db.execute('DELETE FROM crm_action_origin_scopes')
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '后来关联的人'}, NOW)
        people(w, row['id'], [person['id']])
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_status'] == 'unknown' and origin['scope_basis'] == 'legacy_item'
        assert origin['same_customer'] is True and origin['scope_changed_fields'] == []
        second = w.web.sales_workspace.create_opportunity(OWNER, w.unit['id'], {'name': '历史后明确项目B'})
        w.web.sales_workspace.link(OWNER, 'record', row['id'], second['id'])
        changed = await origins(w, row['id'], run['id'])
        assert changed['scope_status'] == 'changed' and changed['scope_changed_fields'] == ['project']
        assert not w.crm._db.execute('SELECT 1 FROM crm_action_origin_scopes').fetchone()


@run_async
async def test_legacy_global_without_target_is_neutral_and_get_does_not_backfill(tmp_path):
    async with world(tmp_path) as w:
        row = action(w)
        run, _, _, item_id = await planned(w, row)
        w.crm._db.execute('DELETE FROM crm_action_origin_scopes')
        old = w.crm._db.execute('SELECT item_json FROM crm_progress_batch_items WHERE owner=? AND item_id=?', (OWNER, item_id)).fetchone()
        item = json.loads(old['item_json']); item.pop('current')
        w.crm._db.execute('UPDATE crm_progress_batch_items SET item_json=? WHERE owner=? AND item_id=?', (json.dumps(item), OWNER, item_id))
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_status'] == 'unknown' and origin['scope_basis'] == 'unknown'
        assert origin['same_customer'] is None and origin['scope_changed_fields'] == []
        assert not w.crm._db.execute('SELECT 1 FROM crm_action_origin_scopes').fetchone()


@pytest.mark.parametrize('invalidity', ['project_archived', 'person_archived', 'source_changed'])
@run_async
async def test_invalidity_is_review_needed_and_does_not_invent_a_scope_move(tmp_path, invalidity):
    async with world(tmp_path) as w:
        row = action(w)
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '保留历史合成人'}, NOW)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        people(w, row['id'], [person['id']])
        run, _, _, _ = await planned(w, row)
        if invalidity == 'project_archived':
            w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'], {'archived': True, 'expected_revision': w.project['revision']})
        elif invalidity == 'person_archived':
            w.crm.update_contact(OWNER, w.unit['id'], person['id'], {'archived': True}, NOW+1)
        else:
            current = w.crm.get_record(OWNER, row['id'])
            w.crm.update_record(OWNER, row['id'], {'content': '后来补充的真实原话'}, NOW+1, expected_updated_at=current['updated_at'])
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_changed_fields'] == [] and origin['same_customer'] is True
        assert origin['scope_status'] == 'unchanged' and origin['scope_needs_review']


@pytest.mark.parametrize('corruption', ['json', 'version', 'known_fields', 'record_id', 'customer_bool', 'foreign_customer', 'contact_ids', 'required_dict', 'required_string', 'required_conflict'])
@run_async
async def test_malformed_or_foreign_snapshot_is_unknown_not_fallback_or_disclosure(tmp_path, corruption):
    async with world(tmp_path) as w:
        row = action(w)
        run, _, _, item_id = await planned(w, row)
        key = f'{run["id"]}:{item_id}'
        baseline = json.loads(scope_row(w, 'progress', key, row['id'])['scope_json'])
        if corruption == 'json':
            value = '[not-json'
        else:
            if corruption == 'version': baseline['version'] = True
            if corruption == 'known_fields': baseline['known_fields'] = [{}]
            if corruption == 'record_id': baseline['record_id'] += 999
            if corruption == 'customer_bool': baseline['customer_id'] = True
            if corruption == 'contact_ids': baseline['contact_ids'] = 'all_people'
            if corruption == 'required_dict': baseline['contacts_required'] = [{'unexpected':1}]
            if corruption == 'required_string': baseline['contacts_required'] = '1'
            if corruption == 'required_conflict': baseline['contacts_required'] = []
            if corruption == 'foreign_customer':
                foreign = w.crm.create_customer('different-owner', {'name': '不可泄漏的单位名'}, NOW)
                baseline['customer_id'] = foreign['id']
            value = json.dumps(baseline)
        w.crm._db.execute('UPDATE crm_action_origin_scopes SET scope_json=? WHERE owner=? AND origin_type=? AND origin_key=? AND record_id=?', (value, OWNER, 'progress', key, row['id']))
        origin = await origins(w, row['id'], run['id'])
        assert origin['scope_status'] == 'unknown' and origin['same_customer'] is None
        assert origin['scope_changed_fields'] == [] and origin['scope_basis'] == 'unknown'
        assert '不可泄漏' not in json.dumps(origin, ensure_ascii=False)


@run_async
async def test_discussion_scope_captures_focus_plus_independent_people_and_replay_is_immutable(tmp_path):
    async with world(tmp_path) as w:
        focus = w.crm.create_contact(OWNER, w.unit['id'], {'name': '焦点林工'}, NOW)
        selected = w.crm.create_contact(OWNER, w.unit['id'], {'name': '独立周工'}, NOW)
        class Advisor:
            async def reply(self, *args):
                return {'answer': '仅是建议，原依据待核实', 'questions': [], 'risks': [], 'next_moves': [
                    {'title': '我去核对接口范围', 'reason': '待确认', 'contact_hint': '林工', 'preparation': '清单', 'success_signal': '明确反馈'}]}
        w.web.discussions.advisor = Advisor()
        thread = w.web.discussions.create_thread(OWNER, {'customer_id': w.unit['id'], 'contact_id': focus['id'], 'request_id': 'scope-discuss'})['thread']
        data = await w.web.discussions.send_message(OWNER, thread['id'], {'text': '怎样继续核对？', 'request_id': 'scope-message'})
        message = next(x for x in data['messages'] if x['role'] == 'assistant')
        body = {'expected_snapshot': message['snapshot'], 'contact_ids': [selected['id']], 'expected_contact_version': data['action_contact_options']['version']}
        record = w.web.discussions.adopt(OWNER, thread['id'], message['id'], 1, body)
        key = f'{thread["id"]}:{message["id"]}:1'
        before = scope_row(w, 'discussion', key, record['id'])['scope_json']
        assert json.loads(before)['contact_ids'] == sorted([focus['id'], selected['id']])
        people(w, record['id'], [selected['id']])
        dto = (await w.call('GET', f'/api/records/{record["id"]}'))['action_origins']['items'][0]
        assert dto['scope_changed_fields'] == ['contacts'] and dto['same_customer'] is True
        assert dto['thread_id'] == thread['id'] and dto['message_id'] == message['id']
        assert dto['user_text'] == '怎样继续核对？' and '原依据待核实' in dto['answer_text']
        again = w.web.discussions.adopt(OWNER, thread['id'], message['id'], 1, body)
        assert again['id'] == record['id'] and scope_row(w, 'discussion', key, record['id'])['scope_json'] == before


@run_async
async def test_partial_done_child_and_remaining_parent_have_separate_atomic_baselines(tmp_path):
    async with world(tmp_path) as w:
        row = action(w, '技术核验与采购反馈', content='技术核验和采购回复都要跟进')
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '合成技术人'}, NOW)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        people(w, row['id'], [person['id']])
        run = await prepare(w, kind='followup_result', customer=True, record_id=row['id'], text='技术核验已通过，采购仍待反馈。')
        item = next(x for x in run['items'] if x['type'] == 'outcome')
        run = choose(w, run, item, decision='partial', result='技术核验已通过；采购仍待反馈',
            completed_title='技术核验已通过', completed_part='技术核验已通过', remaining_title='等采购反馈', remaining_part='采购仍待反馈')
        receipt, _ = adopt(w, run, 'partial-scopes')
        assert receipt['results'][0]['status'] == 'confirmed', receipt
        result = receipt['results'][0]
        assert result['remaining_record_id'] == row['id']
        child = result['completed_record_id']
        key = f'{run["id"]}:{item["id"]}'
        parent_scope = json.loads(scope_row(w, 'progress', key, row['id'])['scope_json'])
        child_scope = json.loads(scope_row(w, 'progress', key, child)['scope_json'])
        assert parent_scope['record_id'] == row['id'] and child_scope['record_id'] == child
        assert parent_scope['contact_ids'] == child_scope['contact_ids'] == [person['id']]
        assert (await origins(w, child, run['id']))['scope_status'] == 'unchanged'
        people(w, child, [])
        assert (await origins(w, child, run['id']))['scope_changed_fields'] == ['contacts']
        assert (await origins(w, row['id'], run['id']))['scope_status'] == 'unchanged'


@run_async
async def test_complete_crash_recovery_copies_business_baseline_not_later_child_edits(tmp_path, monkeypatch):
    async with world(tmp_path) as w:
        row = action(w)
        person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '原有合成人'}, NOW)
        w.web.sales_workspace.link(OWNER, 'record', row['id'], w.project['id'])
        people(w, row['id'], [person['id']])
        run = await prepare(w, kind='followup_result', record_id=row['id'], text='原行动落实；下一步继续问清验收。')
        item = next(x for x in run['items'] if x['type'] == 'outcome')
        run = choose(w, run, item, decision='complete', result='原行动实际落实', next_title='继续问清验收', next_step='核对验收范围', executor_kind='self', duration_minutes=None)
        # Crash after the shared business transaction, before the progress receipt.
        original = w.progress._remember_effect
        def fail(*args, **kwargs):
            raise RuntimeError('isolated simulated receipt interruption')
        monkeypatch.setattr(w.progress, '_remember_effect', fail)
        with pytest.raises(RuntimeError):
            adopt(w, run, 'complete-scopes')
        outcome = dict(w.crm._db.execute('SELECT * FROM crm_action_outcomes WHERE owner=? AND record_id=?', (OWNER, row['id'])).fetchone())
        child = outcome['next_record_id']
        parent_baseline, parent_found = saved_scope(w.crm._db, OWNER, 'outcome', outcome['id'], row['id'])
        child_baseline, child_found = saved_scope(w.crm._db, OWNER, 'outcome', outcome['id'], child)
        assert parent_found and child_found and parent_baseline['record_id'] != child_baseline['record_id']
        assert child_baseline['contact_ids'] == [person['id']]
        people(w, child, [])
        monkeypatch.setattr(w.progress, '_remember_effect', original)
        # The exact submitted request replays the interrupted batch, not a new draft.
        body = {'request_id':'complete-scopes','expected_revision':run['revision'],'items':[{'id':item['id'],
            'expected_item_revision':next(x for x in run['items'] if x['id']==item['id'])['revision'],
            'expected_snapshot':next(x for x in run['items'] if x['id']==item['id'])['versions']['snapshot']}]}
        receipt = w.progress.confirm(OWNER, run['id'], body)
        assert receipt['results'][0]['status'] == 'already_confirmed'
        copied = json.loads(scope_row(w, 'progress', f'{run["id"]}:{item["id"]}', child)['scope_json'])
        assert copied == child_baseline
        assert (await origins(w, child, run['id']))['scope_changed_fields'] == ['contacts']


@run_async
async def test_scope_save_failure_rolls_back_new_business_and_keeps_owner_hidden_boundaries(tmp_path, monkeypatch):
    async with world(tmp_path) as w:
        run = await prepare(w, kind='recap', text='准备一个明确核对动作')
        item = next(x for x in run['items'] if x['type']=='action')
        run = choose(w, run, item, executor_kind='self', duration_minutes=None)
        before = w.crm.list_records(OWNER)['total']
        import secretary.action_origin_scope as helper
        original = helper.save_scope
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('isolated rollback after metadata insertion')
        monkeypatch.setattr(helper, 'save_scope', fail)
        receipt, _ = adopt(w, run, 'rollback-scope')
        assert receipt['results'][0]['status']=='failed'
        assert w.crm.list_records(OWNER)['total']==before
        assert w.crm._db.execute('SELECT count(*) FROM crm_action_origin_scopes').fetchone()[0]==0
        monkeypatch.setattr(helper, 'save_scope', original)
        owned = action(w)
        with pytest.raises(KeyError): action_origins(w.crm, 'different-owner', owned['id'])
        w.crm._db.execute('UPDATE crm_records SET hidden=1 WHERE owner=? AND id=?', (OWNER, owned['id']))
        with pytest.raises(KeyError): action_origins(w.crm, OWNER, owned['id'])
