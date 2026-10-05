"""R10 title-only edits preserve current human scope, never old AI validity.

The first eight cases preserve the original offline before probe's exact input
and business oracle functions. All worlds use fresh SQLite :memory:, no HTTP,
model, QA database or network. Production service initialization installs hooks.
"""
import asyncio

import pytest

from secretary.capture_inbox import CaptureService
from secretary.crm import RecordConflict, analysis_fingerprint
from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineConflict, TimelineService
from secretary.exchange_records import ExchangeRecords
from secretary.materials import MaterialService
from secretary.overview import OverviewService
from secretary.sales_workspace import SalesWorkspace
from secretary.visits import VisitService

NOW = 1800000000.0
OWNER = 'r10-independent-synthetic-owner'
BODY = '我与王工核对数据库加密试点，我答应发送产品资料。审批权限和客户反馈日期尚未知。'
LONG_TITLE = '十轮-R10-独立内存探针：数据库加密项目与王工核对产品资料，尚未核实客户审批权限'
SHORT_TITLE = '数据库加密资料核对'


class World:
    def __init__(self, *, body=BODY, explicit=True):
        self.crm = CustomerStore(':memory:')
        self.clock = [NOW]
        self.workspace = SalesWorkspace(self.crm, clock=lambda: self.clock[0])
        self.timeline = TimelineService(self.crm, self.workspace, clock=lambda: self.clock[0])
        self.capture = CaptureService(self.crm, self.workspace, asyncio.Lock(), clock=lambda: self.clock[0])
        self.overview = OverviewService(self.crm, self.workspace, clock=lambda: self.clock[0])
        self.a = self.crm.create_customer(OWNER, {'name': '十轮-R10-独立合成单位A'}, NOW)
        self.b = self.crm.create_customer(OWNER, {'name': '十轮-R10-独立合成单位B'}, NOW)
        self.person = self.crm.create_contact(OWNER, self.a['id'], {'name': '王工', 'department': '技术部'}, NOW)
        self.project = self.workspace.create_opportunity(OWNER, self.a['id'], {'name': '十轮-R10-独立数据库试点'})
        self.workspace.upsert_stakeholder(OWNER, self.a['id'], self.project['id'], {
            'contact_id': self.person['id'], 'expected_revision': self.project['revision'],
            'roles': ['technical_reviewer'], 'basis': 'reported', 'evidence': '本项目协调技术资料'})
        self.capture_id = None
        if body:
            item = self.capture.capture(OWNER, {'text': body, 'request_id': 'fresh-memory-capture'})
            self.capture_id = item['id']
            filed = self.capture.classify(OWNER, item['id'], {
                'purpose': 'project_reference' if explicit else 'note', 'customer_id': self.a['id'],
                'opportunity_id': self.project['id'] if explicit else None,
                'expected_updated_at': item['record']['updated_at']})
            self.record_id = filed['record']['id']
            self.crm.update_record(OWNER, self.record_id, {'title': LONG_TITLE}, NOW + 1)
            if explicit:
                self.workspace.link(OWNER, 'record', self.record_id, self.project['id'])
        else:
            record = self.crm.create_record(OWNER, {'title': LONG_TITLE, 'content': '', 'customer_id': self.a['id']}, NOW)
            self.record_id = record['id']
            if explicit:
                self.workspace.link(OWNER, 'record', self.record_id, self.project['id'])
        self.key = 'record:' + str(self.record_id)
        if explicit:
            event = self.timeline.get_event(OWNER, self.key)
            self.timeline.save_context(OWNER, self.key, {
                'expected_revision': event['revision'], 'kind': 'communication', 'occurred_at': None,
                'contact_relations': [{'contact_id': self.person['id'], 'relation': 'direct'}]})
        record = self.crm.get_record(OWNER, self.record_id)
        self.analysis = {'input_fingerprint': analysis_fingerprint(record), 'summary': '独立合成分析初稿',
                         'key_points': [], 'open_questions': ['审批权限未知'],
                         'actions': [{'title': '核对发送数据库资料', 'kind': 'suggestion',
                                      'reason': '用户仍需明确采纳', 'owner_hint': '我', 'remind_at': None}]}
        self.crm.save_analysis(OWNER, self.record_id, self.analysis, NOW + 2)

    def state(self):
        record = self.crm.get_record(OWNER, self.record_id)
        event = self.timeline.get_event(OWNER, self.key)
        project = self.overview.project_context(OWNER, {self.record_id})[self.record_id]
        link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?", (OWNER, self.record_id)).fetchone()
        context = self.crm._db.execute('SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?', (OWNER, self.key)).fetchone()
        capture = self.capture.get(OWNER, self.capture_id) if self.capture_id is not None else None
        return {'title': record['title'], 'content': record['content'], 'original_content': record['original_content'],
                'customer_id': record['customer_id'], 'updated_at': record['updated_at'],
                'record_fingerprint': analysis_fingerprint(record), 'analysis_stale': self.crm.get_analysis(OWNER, self.record_id)['stale'],
                'project': project, 'stored_link': dict(link) if link else None,
                'event': {'revision': event['revision'], 'needs_review': event['needs_review'],
                          'kind': event['kind'], 'occurred_at': event['occurred_at'],
                          'opportunity_id': event['opportunity_id'], 'contact_relations': event['contact_relations']},
                'stored_context': dict(context) if context else None,
                'person_timeline_count': self.timeline.view(OWNER, {'contact_id': self.person['id'], 'opportunity_id': self.project['id']})['total'],
                'capture_status': capture['status'] if capture else None,
                'capture_confirmed_component_gate': capture['status'] == 'filed' and not project['project_link_stale'] if capture else None}

    def update(self, data, *, expected=None):
        self.clock[0] += 60
        return self.crm.update_record(OWNER, self.record_id, data, self.clock[0], expected_updated_at=expected)

    def counts(self):
        return {table: self.crm._db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('crm_records', 'crm_analysis_actions', 'tasks', 'proposals', 'notifications',
                              'crm_opportunity_link_history', 'crm_timeline_context_history')}


def valid(s):
    return (not s['project']['project_link_stale'] and not s['event']['needs_review']
            and s['person_timeline_count'] == 1
            and any(r['valid'] and r['relation'] == 'direct' for r in s['event']['contact_relations']))


def title_only(w):
    before = w.state()
    assert valid(before) and before['capture_confirmed_component_gate'] and not before['analysis_stale']
    w.update({'title': SHORT_TITLE}, expected=before['updated_at'])
    after = w.state()
    assert before['content'] == after['content'] and before['original_content'] == after['original_content']
    assert after['analysis_stale'] and before['event']['revision'] != after['event']['revision']
    return valid(after) and after['capture_confirmed_component_gate'], {'before': before, 'after': after}


def protective_change(w, data):
    before = w.state()
    assert valid(before)
    w.update(data(w), expected=before['updated_at'])
    after = w.state()
    return (after['project']['project_link_stale'] and after['event']['needs_review']
            and after['person_timeline_count'] == 0 and after['analysis_stale']), {'before': before, 'after': after}


def already_stale(w):
    w.update({'content': BODY + ' 另一位同事后来纠正了项目范围。'})
    before = w.state()
    assert before['project']['project_link_stale'] and before['event']['needs_review']
    w.update({'title': SHORT_TITLE})
    after = w.state()
    return after['project']['project_link_stale'] and after['event']['needs_review'] and after['person_timeline_count'] == 0, {'before': before, 'after': after}


def unconfirmed(w):
    before = w.state()
    assert before['project']['opportunity_id'] is None and not before['event']['contact_relations']
    w.update({'title': SHORT_TITLE})
    after = w.state()
    return after['project']['opportunity_id'] is None and not after['event']['contact_relations'] and after['person_timeline_count'] == 0, {'before': before, 'after': after}


def old_cas(w):
    before = w.state()
    w.update({'title': SHORT_TITLE})
    counts = w.counts()
    rejected = {}
    for name, action in [('old_timeline_revision', lambda: w.timeline.save_context(OWNER, w.key, {'expected_revision': before['event']['revision'], 'occurred_at': None})),
                         ('old_analysis_input', lambda: w.crm.save_analysis(OWNER, w.record_id, w.analysis, NOW + 70)),
                         ('old_unadopted_analysis_action', lambda: w.crm.adopt_action(OWNER, w.record_id, 1, NOW + 70))]:
        try:
            action()
            rejected[name] = False
        except (ValueError, TimelineConflict) as error:
            rejected[name] = {'class': type(error).__name__, 'message': str(error)}
    after = w.state()
    return all(rejected.values()) and w.counts() == counts and after['analysis_stale'], {'before': before, 'after': after, 'rejected': rejected, 'counts_unchanged': w.counts() == counts}


def rejected_record_cas(w):
    before = w.state()
    counts = w.counts()
    try:
        w.update({'title': SHORT_TITLE}, expected=before['updated_at'] - 1)
    except RecordConflict as error:
        after = w.state()
        return before == after and counts == w.counts(), {'before': before, 'after': after, 'error': str(error)}
    return False, {'error': 'Old record CAS unexpectedly accepted'}


ORIGINAL_CASES = [
    ('T01_title_only_full_text_retains_explicit_project_and_people', {}, title_only),
    ('T02_changed_body_invalidates_old_project_and_people', {}, lambda w: protective_change(w, lambda _: {'content': BODY + ' 项目内容已被修正。'})),
    ('T03_changed_customer_invalidates_old_project_and_people', {}, lambda w: protective_change(w, lambda x: {'customer_id': x.b['id']})),
    ('T04_title_only_source_empty_body_remains_protected', {'body': ''}, lambda w: protective_change(w, lambda _: {'title': SHORT_TITLE})),
    ('T05_preexisting_stale_relation_never_revives_on_title_edit', {}, already_stale),
    ('T06_no_explicit_relation_is_not_inferred_from_title', {'explicit': False}, unconfirmed),
    ('T07_old_analysis_and_timeline_CAS_stay_strict_after_title_edit', {}, old_cas),
    ('T08_rejected_record_CAS_has_no_business_effect', {}, rejected_record_cas),
]


@pytest.mark.parametrize('name,options,check', ORIGINAL_CASES, ids=[case[0] for case in ORIGINAL_CASES])
def test_original_title_business_oracles(name, options, check):
    w = World(**options)
    try:
        assert w.crm._db.execute('PRAGMA database_list').fetchone()['file'] == ''
        passed, evidence = check(w)
        assert passed, (name, evidence)
    finally:
        w.crm.close()


@pytest.fixture
def world():
    w = World()
    try:
        yield w
    finally:
        w.crm.close()


def relation_rows(w):
    return {
        table: [tuple(row) for row in w.crm._db.execute('SELECT * FROM ' + table)]
        for table in ('crm_opportunity_links', 'crm_opportunity_link_history',
                      'crm_timeline_contexts', 'crm_timeline_context_history')
    }


def no_new_work(w, counts):
    assert all(w.counts()[table] == counts[table]
               for table in ('crm_records', 'crm_analysis_actions', 'tasks', 'proposals', 'notifications'))


def test_foreign_owner_cannot_rename_or_carry_relations(world):
    w = world
    before, rows, counts = w.state(), relation_rows(w), w.counts()
    with pytest.raises(KeyError):
        w.crm.update_record('r10-other-synthetic-owner', w.record_id, {'title': SHORT_TITLE}, NOW + 60,
                            expected_updated_at=before['updated_at'])
    assert w.state() == before
    assert relation_rows(w) == rows and w.counts() == counts


def test_archived_project_title_change_does_not_restore_confirmed_scope(world):
    w = world
    current = w.workspace.opportunities(OWNER, w.a['id'])['items'][0]
    w.workspace.update_opportunity(OWNER, w.a['id'], current['id'],
                                   {'expected_revision': current['revision'], 'archived': True})
    before, rows, counts = w.state(), relation_rows(w), w.counts()
    w.update({'title': SHORT_TITLE}, expected=before['updated_at'])
    after = w.state()
    assert relation_rows(w) == rows
    assert after['event']['needs_review'] and after['analysis_stale']
    assert after['stored_link'] == before['stored_link'] and after['stored_context'] == before['stored_context']
    no_new_work(w, counts)


def test_archived_person_is_not_reconfirmed_by_title_change(world):
    w = world
    w.crm.update_contact(OWNER, w.a['id'], w.person['id'], {'archived': True}, NOW + 10)
    old_context = w.crm._db.execute('SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?',
                                    (OWNER, w.key)).fetchone()
    old_history = w.crm._db.execute('SELECT count(*) FROM crm_timeline_context_history').fetchone()[0]
    counts = w.counts()
    w.update({'title': SHORT_TITLE})
    event = w.timeline.get_event(OWNER, w.key)
    current_context = w.crm._db.execute('SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?',
                                        (OWNER, w.key)).fetchone()
    assert tuple(current_context) == tuple(old_context)
    assert w.crm._db.execute('SELECT count(*) FROM crm_timeline_context_history').fetchone()[0] == old_history
    assert event['needs_review'] and not any(item.get('valid') for item in event['contact_relations'])
    assert w.crm.get_analysis(OWNER, w.record_id)['stale']
    no_new_work(w, counts)


def test_visit_merged_source_keeps_old_relation_versions_instead_of_rebasing(world):
    w = world
    lock = asyncio.Lock()
    materials = MaterialService(w.crm, lock, clock=lambda: w.clock[0])
    visits = VisitService(w.crm, materials, lock)
    exchange = ExchangeRecords(w.crm, visits, lambda: w.clock[0])
    parent = exchange.archive(OWNER, w.record_id, {'role': 'supplement'})
    event = w.timeline.get_event(OWNER, w.key)
    assert event['merged_into'] == 'visit:' + str(parent['visit_id'])
    rows, counts = relation_rows(w), w.counts()
    w.update({'title': SHORT_TITLE})
    assert relation_rows(w) == rows
    current = w.timeline.get_event(OWNER, w.key)
    assert current['needs_review'] and current['merged_into'] == event['merged_into']
    assert w.crm.get_analysis(OWNER, w.record_id)['stale']
    no_new_work(w, counts)


def test_material_historical_source_is_not_revived_by_title_change(world):
    w = world
    materials = MaterialService(w.crm, asyncio.Lock(), clock=lambda: w.clock[0])
    material = materials.enqueue(OWNER, {'provider': 'manual', 'title': '保留合成材料历史', 'text': BODY,
                                         'customer_id': w.a['id']})
    replacement = w.crm.create_record(OWNER, {'title': '当前材料整理', 'content': BODY,
                                              'customer_id': w.a['id']}, NOW + 10)
    # Synthetic durable material-source identity, not a model call or guessed
    # title association. The ordinary original record becomes a historical note.
    with w.crm._transaction() as db:
        db.execute('UPDATE crm_materials SET record_id=? WHERE owner=? AND id=?',
                   (replacement['id'], OWNER, material['id']))
        db.execute('UPDATE crm_records SET source_id=? WHERE owner=? AND id=?',
                   ('material:' + str(material['id']) + ':' + 'a' * 64 + ':note', OWNER, w.record_id))
    w.workspace.link(OWNER, 'record', w.record_id, w.project['id'])
    old = w.timeline.get_event(OWNER, w.key)
    w.timeline.save_context(OWNER, w.key, {'expected_revision': old['revision'], 'kind': 'communication',
                                         'occurred_at': None,
                                         'contact_relations': [{'contact_id': w.person['id'], 'relation': 'direct'}]})
    event = w.timeline.get_event(OWNER, w.key)
    assert event['historical_source']['id'] == material['id']
    assert not event['needs_review']
    rows, counts = relation_rows(w), w.counts()
    w.update({'title': SHORT_TITLE})
    assert relation_rows(w) == rows
    current = w.timeline.get_event(OWNER, w.key)
    assert current['needs_review'] and current['historical_source'] == event['historical_source']
    no_new_work(w, counts)


def test_no_timeline_lifecycle_conservatively_keeps_relation_snapshots(world):
    w = world
    before, rows, counts = w.state(), relation_rows(w), w.counts()
    w.workspace.timeline = None
    w.update({'title': SHORT_TITLE}, expected=before['updated_at'])
    assert relation_rows(w) == rows
    after = w.state()
    assert after['project']['project_link_stale'] and after['event']['needs_review'] and after['analysis_stale']
    assert after['person_timeline_count'] == 0
    no_new_work(w, counts)


def test_context_write_failure_rolls_back_title_project_and_all_history(world, monkeypatch):
    w = world
    before, rows, counts = w.state(), relation_rows(w), w.counts()

    def fail_after_project(db, owner, event, values):
        # Prove the failure occurs after the new project snapshot, exercising
        # rollback of both the earlier record UPDATE and the relationship write.
        link = db.execute("SELECT revision FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                          (OWNER, w.record_id)).fetchone()
        assert link['revision'] == before['stored_link']['revision'] + 1
        assert w.crm._require_record(db, OWNER, w.record_id)['title'] == SHORT_TITLE
        raise RuntimeError('synthetic context persistence failure')

    monkeypatch.setattr(w.timeline, '_write_context', fail_after_project)
    with pytest.raises(RuntimeError, match='synthetic context persistence failure'):
        w.update({'title': SHORT_TITLE}, expected=before['updated_at'])
    assert w.state() == before and relation_rows(w) == rows and w.counts() == counts


def test_latest_context_is_preserved_but_old_context_CAS_still_rejected(world):
    w = world
    original = w.timeline.get_event(OWNER, w.key)
    latest = w.timeline.save_context(OWNER, w.key,
                                     {'expected_revision': original['revision'], 'kind': 'reflection',
                                      'contact_relations': [{'contact_id': w.person['id'], 'relation': 'about'}]})
    before = w.state()
    w.update({'title': SHORT_TITLE}, expected=before['updated_at'])
    event = w.timeline.get_event(OWNER, w.key)
    assert event['kind'] == 'reflection' and event['occurred_at'] is None and not event['needs_review']
    assert [(item['contact_id'], item['relation'], item['valid']) for item in event['contact_relations']] == [(w.person['id'], 'about', True)]
    rows, counts = relation_rows(w), w.counts()
    with pytest.raises(TimelineConflict):
        w.timeline.save_context(OWNER, w.key, {'expected_revision': latest['revision'],
                                             'contact_relations': [{'contact_id': w.person['id'], 'relation': 'direct'}]})
    assert relation_rows(w) == rows and w.counts() == counts
    assert w.crm.get_analysis(OWNER, w.record_id)['stale']


def test_same_value_fields_are_title_only_and_noop_does_not_create_history(world):
    w = world
    before, counts = w.state(), w.counts()
    w.update({'title': SHORT_TITLE, 'content': before['content'], 'customer_id': before['customer_id']},
             expected=before['updated_at'])
    after = w.state()
    assert valid(after) and after['analysis_stale'] and after['capture_confirmed_component_gate']
    assert after['stored_context']['data_json'] == before['stored_context']['data_json']
    assert after['stored_link']['revision'] == before['stored_link']['revision'] + 1
    no_new_work(w, counts)
    rows, counts = relation_rows(w), w.counts()
    w.update({'title': SHORT_TITLE, 'content': after['content'], 'customer_id': after['customer_id']},
             expected=after['updated_at'])
    assert w.state() == after and relation_rows(w) == rows and w.counts() == counts
