"""Matter-specific advice and adoption on disposable data only."""
import asyncio
from copy import deepcopy

import pytest

from secretary.customer_store import CustomerStore
from secretary.matters import MatterService
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0
REPLY = {'answer': '先完善材料，再核对客户汇报目标。', 'questions': ['这次汇报需要什么结果？'], 'risks': [],
    'next_moves': [
        {'title': '补充防篡改产品案例', 'reason': '汇报材料尚缺客户场景案例', 'contact_hint': '负责人（姓名与职责待确认）',
         'preparation': '整理一页产品场景', 'success_signal': '形成可用于汇报的案例'},
        {'title': '核对客户汇报目标', 'reason': '需要明确本次要达成的结果', 'contact_hint': '负责人（姓名与职责待确认）',
         'preparation': '准备两个确认问题', 'success_signal': '明确汇报目标'}]}


class Advisor:
    def __init__(self):
        self.contexts = []
        self.callback = None

    async def reply(self, context, history, text, now):
        self.contexts.append(deepcopy(context))
        if self.callback:
            self.callback()
        return deepcopy(REPLY)


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / 'matter-discussion-synthetic.sqlite3')
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    matters = MatterService(crm, clock=lambda: NOW)
    advisor = Advisor()
    discussion = DiscussionService(crm, sales, asyncio.Lock(), advisor=advisor, clock=lambda: NOW)
    customer = crm.create_customer('owner', {'name': '示例研究所'}, NOW)
    project = sales.create_opportunity('owner', customer['id'], {'name': '电力密码方案'})
    source = crm.create_record('owner', {'title': '材料准备想法', 'content': '按产品线组织PPT，再补充装置内容',
        'kind': 'note', 'status': 'done', 'customer_id': customer['id']}, NOW)
    action = crm.create_record('owner', {'title': '整理电力PPT结构', 'content': '整理电力PPT结构',
        'kind': 'action', 'status': 'following', 'customer_id': customer['id']}, NOW)
    matter = matters.create('owner', {'title': '完善电力汇报材料', 'objective': '形成可用于客户汇报的电力PPT',
        'customer_id': customer['id'], 'opportunity_id': project['id'], 'source_record_ids': [source['id']],
        'action_record_ids': [action['id']], 'request_id': 'initial-matter'})['matter']
    yield {'crm': crm, 'sales': sales, 'matters': matters, 'discussion': discussion, 'advisor': advisor,
           'customer': customer, 'project': project, 'source': source, 'action': action, 'matter': matter}
    asyncio.run(discussion.close())
    crm.close()


def thread(stack, **extra):
    return stack['discussion'].create_thread('owner', {'matter_id': stack['matter']['id'], 'request_id': 'matter-thread', **extra})['thread']


def ask(stack, item, *, key='question'):
    result = asyncio.run(stack['discussion'].send_message('owner', item['id'], {'text': '这件事下一步怎么推进？', 'request_id': key}))
    return result, next(item for item in reversed(result['messages']) if item['role'] == 'assistant')


def archive(stack, matter=None):
    matter = matter or stack['matters'].detail('owner', stack['matter']['id'])
    with stack['crm']._transaction() as db:
        db.execute("UPDATE crm_matters SET visibility='archived',revision=revision+1 WHERE owner='owner' AND id=?", (matter['id'],))


def test_matter_thread_inherits_scope_and_attaches_once_inside_transaction(stack):
    first = thread(stack)
    assert first['matter_id'] == stack['matter']['id']
    assert first['customer_id'] == stack['customer']['id'] and first['opportunity_id'] == stack['project']['id']
    revision = stack['matters'].detail('owner', stack['matter']['id'])['revision']
    assert thread(stack)['id'] == first['id']
    assert stack['matters'].detail('owner', stack['matter']['id'])['revision'] == revision
    assert stack['crm']._db.execute("SELECT count(*) FROM crm_matter_links WHERE entity_type='discussion'").fetchone()[0] == 1
    assert stack['crm']._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_same_request_cannot_bind_different_matter_even_with_same_scope(stack):
    first = thread(stack)
    other = stack['matters'].create('owner', {'title': '另一个目标', 'customer_id': stack['customer']['id'],
        'opportunity_id': stack['project']['id'], 'request_id': 'other-matter'})['matter']
    with pytest.raises(ValueError):
        stack['discussion'].create_thread('owner', {'matter_id': other['id'], 'request_id': 'matter-thread'})
    assert stack['discussion'].list_threads('owner')['total'] == 1
    assert stack['discussion'].get_thread('owner', first['id'])['thread']['matter_id'] == stack['matter']['id']


def test_unassigned_matter_requires_customer_selection_and_preserves_original(stack):
    item = stack['matters'].create('owner', {'title': '未归属的材料准备', 'request_id': 'unassigned'})['matter']
    with pytest.raises(ValueError, match='选择'):
        stack['discussion'].create_thread('owner', {'matter_id': item['id']})
    result = stack['discussion'].create_thread('owner', {'matter_id': item['id'], 'customer_id': stack['customer']['id']})
    assert result['thread']['matter_id'] == item['id']
    assert stack['matters'].detail('owner', item['id'])['customer_id'] is None


def test_owned_matter_cannot_switch_customer_or_project_for_discussion(stack):
    customer = stack['crm'].create_customer('owner', {'name': '另一示例单位'}, NOW)
    project = stack['sales'].create_opportunity('owner', stack['customer']['id'], {'name': '另一交通项目'})
    for extra in ({'customer_id': customer['id']}, {'opportunity_id': project['id']}, {'opportunity_id': None}):
        with pytest.raises(ValueError):
            thread(stack, **extra)


def test_foreign_owner_matter_is_rejected(stack):
    foreign = stack['matters'].create('other', {'title': '私有目标', 'request_id': 'private'})['matter']
    with pytest.raises(KeyError):
        stack['discussion'].create_thread('owner', {'matter_id': foreign['id'], 'customer_id': stack['customer']['id']})


def test_context_focuses_on_one_goal_and_keeps_source_done_separate(stack):
    unrelated = stack['crm'].create_record('owner', {'title': '另一个合同付款目标', 'content': '催收付款',
        'kind': 'action', 'status': 'following', 'customer_id': stack['customer']['id']}, NOW)
    result, reply = ask(stack, thread(stack))
    context = stack['advisor'].contexts[0]
    assert context['matter']['objective'] == stack['matter']['objective']
    assert context['matter']['status'] == 'following'
    assert context['matter']['sources'][0]['status'] == 'done'
    assert context['matter']['actions'][0]['status'] == 'following'
    assert unrelated['id'] not in {item['id'] for item in context['open_actions']}
    assert result['thread']['matter_id'] == stack['matter']['id'] and not reply['stale']


def test_adopt_links_action_to_same_goal_and_refreshes_snapshot_for_second_move(stack):
    item = thread(stack)
    _, reply = ask(stack, item)
    first = stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    again = stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    second = stack['discussion'].adopt('owner', item['id'], reply['id'], 2)
    current = stack['matters'].detail('owner', stack['matter']['id'])
    assert first['id'] == again['id']
    assert {first['id'], second['id']} <= {action['id'] for action in current['actions']}
    assert current['status'] == 'following'
    assert not stack['crm']._db.execute('SELECT 1 FROM tasks').fetchone()
    assert not stack['crm']._db.execute('SELECT 1 FROM notifications').fetchone()


def test_matter_change_makes_old_advice_stale_and_prevents_adoption(stack):
    item = thread(stack)
    _, reply = ask(stack, item)
    current = stack['matters'].detail('owner', stack['matter']['id'])
    stack['matters'].update('owner', current['id'], {'objective': '先核对新的汇报范围', 'expected_revision': current['revision'], 'request_id': 'change-goal'})
    response = stack['discussion'].get_thread('owner', item['id'])
    assert next(message for message in response['messages'] if message['id'] == reply['id'])['stale']
    count = stack['crm'].list_records('owner')['total']
    with pytest.raises(ValueError):
        stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    assert stack['crm'].list_records('owner')['total'] == count


@pytest.mark.parametrize('field,value', [('visibility', 'archived'), ('visibility', 'trash'), ('status', 'ended')])
def test_inactive_matter_keeps_history_and_replay_but_blocks_new_adoption(stack, field, value):
    item = thread(stack)
    _, reply = ask(stack, item)
    with stack['crm']._transaction() as db:
        db.execute(f'UPDATE crm_matters SET {field}=?,revision=revision+1 WHERE id=?', (value, stack['matter']['id']))
    assert stack['discussion'].get_thread('owner', item['id'])['messages']
    assert thread(stack)['id'] == item['id']
    with pytest.raises(ValueError):
        stack['discussion'].create_thread('owner', {'matter_id': stack['matter']['id'], 'request_id': 'new-thread'})
    with pytest.raises(ValueError):
        stack['discussion'].adopt('owner', item['id'], reply['id'], 1)


def test_late_ai_after_archive_is_saved_as_stale_history(stack):
    item = thread(stack)
    stack['advisor'].callback = lambda: archive(stack)
    result, reply = ask(stack, item)
    assert reply['stale'] and result['messages'][0]['status'] == 'complete'
    with pytest.raises(ValueError):
        stack['discussion'].adopt('owner', item['id'], reply['id'], 1)


def test_failed_discussion_attach_rolls_back_new_thread(stack, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('fake association failure')
    monkeypatch.setattr(stack['matters'], 'attach', fail)
    with pytest.raises(ValueError):
        thread(stack)
    assert stack['discussion'].list_threads('owner')['total'] == 0


def test_failed_action_attach_rolls_back_adoption_and_record(stack, monkeypatch):
    item = thread(stack)
    _, reply = ask(stack, item)
    before = stack['crm'].list_records('owner')['total']
    def fail(*args, **kwargs):
        raise ValueError('fake action association failure')
    monkeypatch.setattr(stack['matters'], 'attach', fail)
    with pytest.raises(ValueError):
        stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    assert stack['crm'].list_records('owner')['total'] == before
    assert stack['crm']._db.execute('SELECT count(*) FROM crm_sales_discussion_adoptions').fetchone()[0] == 0


def test_merged_matter_context_follows_canonical_link_not_old_request_id(stack):
    item = thread(stack)
    old = stack['matters'].detail('owner', stack['matter']['id'])
    target = stack['matters'].create('owner', {'title': '客户电力方案汇报', 'customer_id': stack['customer']['id'],
        'opportunity_id': stack['project']['id'], 'request_id': 'merge-target'})['matter']
    stack['matters'].merge('owner', old['id'], {'target_id': target['id'], 'expected_revision': old['revision'],
        'target_revision': target['revision'], 'request_id': 'merge'})
    assert stack['discussion'].get_thread('owner', item['id'])['thread']['matter_id'] == target['id']
    _, reply = ask(stack, item)
    action = stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    assert action['id'] in {row['id'] for row in stack['matters'].detail('owner', target['id'])['actions']}


def test_multiple_associated_goals_are_not_silently_picked_for_adoption(stack):
    item = thread(stack)
    other = stack['matters'].create('owner', {'title': '另一件独立目标', 'customer_id': stack['customer']['id'],
        'opportunity_id': stack['project']['id'], 'request_id': 'second-goal'})['matter']
    stack['matters'].attach('owner', other['id'], 'discussion', item['id'], role='discussion')
    _, reply = ask(stack, item)
    assert stack['advisor'].contexts[0]['matter'] is None
    assert stack['advisor'].contexts[0]['matter_scope_warning']
    with pytest.raises(ValueError, match='多件事'):
        stack['discussion'].adopt('owner', item['id'], reply['id'], 1)


def test_long_source_has_labelled_head_and_tail_excerpt(stack):
    long = '原始准备方向：考虑试点。' + '资料正文' * 500 + '最后复盘：客户尚未确认测试，需先核对。'
    stack['crm'].update_record('owner', stack['source']['id'], {'content': long}, NOW+1)
    ask(stack, thread(stack))
    excerpt = stack['advisor'].contexts[0]['matter']['sources'][0]
    assert excerpt['content_truncated'] and '客户尚未确认测试' in excerpt['content']


def test_many_finished_steps_do_not_hide_the_current_preparation(stack):
    for index in range(15):
        action = stack['crm'].create_record('owner', {'title': f'已完成的材料步骤{index}', 'content': '历史步骤',
            'kind': 'action', 'status': 'done', 'customer_id': stack['customer']['id']}, NOW)
        stack['matters'].attach('owner', stack['matter']['id'], 'record', action['id'], role='action')
    ask(stack, thread(stack))
    matter = stack['advisor'].contexts[0]['matter']
    assert matter['truncated'] and len(matter['actions']) == 12
    assert matter['actions'][0]['id'] == stack['action']['id']


def test_legacy_discussion_without_matter_still_works(stack):
    item = stack['discussion'].create_thread('owner', {'customer_id': stack['customer']['id'], 'request_id': 'legacy'})['thread']
    _, reply = ask(stack, item)
    assert stack['advisor'].contexts[0]['matter'] is None
    action = stack['discussion'].adopt('owner', item['id'], reply['id'], 1)
    assert stack['matters'].resolve('owner', 'record', action['id'])['items'] == []
