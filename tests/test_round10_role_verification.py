"""Explicit project-role verification on fresh local HTTP/service fixtures."""
import httpx
import pytest

from test_round08_planning_boundaries import world, run_async, OWNER, NOW


@pytest.fixture(autouse=True)
def forbid_external_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('R10 uses local TestServer only')
    monkeypatch.setattr(httpx.AsyncClient, '__init__', forbidden)


def setup(w):
    w.project = w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'],
        {'expected_revision': w.project['revision'], 'stage': 'negotiation'})
    person = w.crm.create_contact(OWNER, w.unit['id'], {'name': '合成项目角色王工'}, NOW)
    return person


def payload(w, person, **changes):
    project = w.web.sales_workspace._require_opportunity(w.crm._db, OWNER, w.unit['id'], w.project['id'])
    return {'expected_revision': project['revision'], 'contact_id': person['id'],
        'roles': ['final_approver', 'technical_reviewer', 'business_owner'],
        'basis': 'reported', 'evidence': '合成本人明确说明负责本项目终审、技术评审与业务验收',
        'engagement': 'direct', **changes}


def snapshot(w):
    tables = ('crm_opportunities', 'crm_opportunity_stakeholders', 'crm_opportunity_people_history',
              'crm_customer_facts', 'crm_records', 'tasks', 'proposals')
    return {t: [tuple(x) for x in w.crm._db.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}


def known(w):
    return w.web.profile_intelligence._decision_chain_known(OWNER, w.unit['id'], w.project['id'])


@run_async
async def test_http_capability_explicit_server_confirmation_closes_only_own_project(tmp_path):
    async with world(tmp_path) as w:
        person = setup(w)
        second = w.web.sales_workspace.create_opportunity(OWNER, w.unit['id'], {'name': '另一合成项目', 'stage': 'negotiation'})
        route = f'/api/customers/{w.unit["id"]}/opportunities/{w.project["id"]}/stakeholders'
        before = w.crm._db.total_changes
        dto = await w.call('GET', route)
        assert dto['supports_verify_now'] is True and w.crm._db.total_changes == before
        await w.call('POST', route, payload(w, person))
        assert not known(w)
        assert (await w.call('GET', route))['items'][0]['verified_at'] is None
        w.clock[0] += 30
        result = await w.call('POST', route, payload(w, person, verify_now=True))
        assert result['relationship']['stakeholders'][0]['verified_at'] == w.clock[0]
        assert known(w)
        assert not w.web.profile_intelligence._decision_chain_known(OWNER, w.unit['id'], second['id'])
        assert w.crm.profile(OWNER, w.unit['id'])['contacts'][0]['role'] == ''
        for table in ('crm_customer_facts', 'crm_records', 'tasks', 'proposals'):
            assert w.crm._db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


@run_async
async def test_explicit_clear_reverify_and_legacy_omission_preserve_their_distinct_contracts(tmp_path):
    async with world(tmp_path) as w:
        person = setup(w)
        service = w.web.sales_workspace
        def save(**changes):
            return service.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], payload(w, person, **changes))['stakeholders'][0]
        first = save(verified_at=NOW-100)
        assert first['verified_at'] == NOW-100 and known(w)
        w.clock[0] += 100
        old = save(evidence='旧客户端明确改了依据但省略核实字段', concerns='只补充关切')
        assert old['verified_at'] == first['verified_at']  # old API remains compatible
        cleared = save(evidence='新版表单实质更改依据，用户未重核', verified_at=None)
        assert cleared['verified_at'] is None and not known(w)
        refreshed = save(verify_now=True)
        assert refreshed['verified_at'] == w.clock[0] and known(w)
        unrelated = service.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], {
            'contact_id': person['id'], 'expected_revision': service._require_opportunity(w.crm._db, OWNER, w.unit['id'], w.project['id'])['revision'],
            'next_step': '只更新下一次准备材料', 'stance': 'supportive'})['stakeholders'][0]
        assert unrelated['verified_at'] == refreshed['verified_at']
        false = save(verify_now=False)
        assert false['verified_at'] == refreshed['verified_at']


@pytest.mark.parametrize('flag', [None, 1, 'true', []])
@run_async
async def test_non_boolean_flag_rejected_before_any_relationship_write(tmp_path, flag):
    async with world(tmp_path) as w:
        person = setup(w)
        before = snapshot(w)
        with pytest.raises(ValueError):
            w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], payload(w, person, verify_now=flag))
        assert before == snapshot(w)


@pytest.mark.parametrize('flag', [True, False])
@run_async
async def test_explicit_new_flag_and_historical_timestamp_cannot_coexist(tmp_path, flag):
    async with world(tmp_path) as w:
        person = setup(w)
        before = snapshot(w)
        with pytest.raises(ValueError):
            w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], payload(w, person, verify_now=flag, verified_at=None))
        assert before == snapshot(w)


@pytest.mark.parametrize('changes', [{'roles': []}, {'evidence': '   '}, {'basis': 'observation'}, {'engagement': 'unknown'}, {'engagement': 'not_contacted'}])
@run_async
async def test_incomplete_confirmation_cannot_become_verified(tmp_path, changes):
    async with world(tmp_path) as w:
        person = setup(w)
        before = snapshot(w)
        with pytest.raises(ValueError):
            w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], payload(w, person, verify_now=True, **changes))
        assert before == snapshot(w)


@run_async
async def test_indirect_technical_verification_does_not_invent_remaining_decision_chain(tmp_path):
    async with world(tmp_path) as w:
        person = setup(w)
        result = w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'],
            payload(w, person, roles=['technical_reviewer'], engagement='indirect', verify_now=True))
        assert result['stakeholders'][0]['verified_at'] == NOW
        assert not known(w)


@pytest.mark.parametrize('failure', ['stale', 'archived_project', 'archived_contact', 'foreign_contact'])
@run_async
async def test_scope_and_cas_guards_remain_atomic_for_confirmation(tmp_path, failure):
    async with world(tmp_path) as w:
        person = setup(w)
        body = payload(w, person, verify_now=True)
        if failure == 'stale':
            body['expected_revision'] -= 1
        elif failure == 'archived_project':
            w.web.sales_workspace.update_opportunity(OWNER, w.unit['id'], w.project['id'], {'expected_revision': body['expected_revision'], 'archived': True})
            body = payload(w, person, verify_now=True)
        elif failure == 'archived_contact':
            w.crm.update_contact(OWNER, w.unit['id'], person['id'], {'archived': True}, NOW)
        else:
            unit = w.crm.create_customer('foreign', {'name': 'foreign synthetic'}, NOW)
            body['contact_id'] = w.crm.create_contact('foreign', unit['id'], {'name': '合成外部人'}, NOW)['id']
        before = snapshot(w)
        with pytest.raises((KeyError, ValueError)):
            w.web.sales_workspace.upsert_stakeholder(OWNER, w.unit['id'], w.project['id'], body)
        assert before == snapshot(w)
