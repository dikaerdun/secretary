import asyncio
import json

import httpx
import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_resolution import CustomerResolutionService, CustomerResolver
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_goals import SecretaryGoals, GoalInterpreter, rule_goal

NOW = 1791082800


@pytest.mark.parametrize('text,intent', [
    ('帮我研究合成银行，做单位画像，准备拜访', 'research'),
    ('帮我准备下次拜访的提纲', 'visit_prepare'),
    ('帮我复盘刚才的会议', 'recap'),
    ('下周优先做什么，整理工作计划', 'plan'),
    ('给我看看本月有哪些日程', 'query'),
    ('讨论下一步怎么推进王工', 'discussion'),
    ('今天王工说帮我研究密码方案', 'record'),
    ('王工说：帮我查一下接口文档', 'record'),
    ('帮我记下：下周研究公司画像', 'record'),
    ('客户说预算暂未审批', 'record'),
    ('帮我整理这次跟进结果', 'followup_result'),
    ('帮我复盘今天的沟通，完成情况另行核对', 'recap'),
])
def test_routing_keeps_speech_and_current_request_distinct(text, intent):
    assert rule_goal(text)['intent'] == intent


def test_model_semantic_route_and_provider_failure_are_read_only(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'synthetic-goals.sqlite3')
        workspace = SalesWorkspace(crm, clock=lambda: NOW)
        unit = crm.create_customer('me', {'name': '合成银行'}, NOW)
        def reply(request):
            sent = json.loads(request.content)
            assert '识别用户给销售秘书的目标' in sent['messages'][0]['content']
            assert '13800138000' not in sent['messages'][1]['content']
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"intent":"visit_prepare"}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            interpreter = GoalInterpreter(CustomerResolver('synthetic-key', client=client))
            resolution = CustomerResolutionService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
            service = SecretaryGoals(resolution, interpreter)
            before = crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0]
            output = await service.preview('me', {'text': '帮我准备见13800138000，想知道该带什么材料', 'customer_id': unit['id']})
            assert output['intent'] == 'visit_prepare' and output['method'] == 'model'
            assert output['scope']['customer_id'] == unit['id']
            assert crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0] == before
            with pytest.raises((KeyError, ValueError)):
                await service.preview('another-owner', {'text': '今天有哪些待办', 'customer_id': unit['id']})
        class Failing:
            available = True
            async def classify(self, text):
                raise ValueError('invalid provider output')
        output = await SecretaryGoals(resolution, Failing()).preview('me', {'text': '下周先做什么'})
        assert output['intent'] == 'plan' and output['period'] == 'week' and output['warning']
        crm.close()
    asyncio.run(run())


def test_model_cannot_return_identity_or_execution(tmp_path):
    async def run():
        def reply(_request):
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"intent":"plan","period":"week","customer_id":1,"confirm":true}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(ValueError):
                await GoalInterpreter(CustomerResolver('synthetic-key', client=client)).classify('帮我做一周计划')
    asyncio.run(run())


def test_personal_plan_dates_use_calendar_windows_without_creating_tasks(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'calendar-goals.sqlite3')
        workspace = SalesWorkspace(crm, clock=lambda: NOW)
        resolution = CustomerResolutionService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
        service = SecretaryGoals(resolution)
        # NOW is a Sunday in Shanghai. Explicit relative periods remain query windows.
        for text, date in [('帮我安排本周计划', '2026-09-28'), ('帮我安排下周计划', '2026-10-05'),
                           ('帮我安排本月计划', '2026-10-01'), ('帮我安排下月计划', '2026-11-01')]:
            output = await service.preview('me', {'text': text})
            assert output['intent'] == 'plan' and output['start_date'] == date
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        crm.close()
    asyncio.run(run())
