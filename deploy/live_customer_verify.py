"""Real-model integration with synthetic customers in a disposable local database."""
import asyncio
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from secretary.__main__ import read_config
from secretary.customer_parser import CustomerVoiceParser
from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.sales_coach import SalesCoach
from secretary.coaching_service import CoachingService
from secretary.organizer import InteractionOrganizer
from secretary.store import SHANGHAI


async def main():
    logging.disable(logging.CRITICAL)
    step = 'configuration'
    diagnostic = {}
    try:
        config = read_config('.env')
        parser = CustomerVoiceParser(config['api_key'], config['model'], config['base_url'])
        with tempfile.TemporaryDirectory() as folder:
            crm = CustomerStore(Path(folder) / 'synthetic.sqlite3')
            try:
                lock = asyncio.Lock()
                service = CustomerService(crm, parser, lock,
                    organizer=InteractionOrganizer(config['api_key'], config['model'], config['base_url']))
                result = await service.handle('synthetic', 'create', '新建客户联调演示医院，联系人王总，关注数据库加密和密钥管理，希望先微信发材料。', force=True)
                step = 'create'
                assert result.get('draft') and result['draft']['status'] == 'pending'
                assert crm.list_customers('synthetic')['total'] == 0
                confirmed = service.decide('synthetic', result['draft']['id'], True)
                profile = crm.profile('synthetic', confirmed['customer_id'])
                assert profile['contacts'] and profile['fields']
                updated = await service.handle('synthetic', 'update', '补充客户联调演示医院，联系人王总，我感觉他更关注实施周期。', force=True)
                step = 'observation'
                assert updated.get('draft')
                changes = updated['draft']['changes']
                assert any(c.get('basis') == 'observation' for c in changes)
                note = await service.handle('synthetic', 'multi-note',
                    '今天和王总、李工讨论，客户说不要修改原系统，希望先验证数据库字段加密和密钥管理。不是全库加密，是三个核心字段。',
                    force=True, selected_customer_id=confirmed['customer_id'])
                step = 'multi_person_note'
                record = crm.get_record('synthetic', note['record_id'])
                assert record['customer_id'] == confirmed['customer_id']
                assert '三个核心字段' in record['original_content']
                assert '已有多个联系人' not in note['message']
                when = (datetime.now(SHANGHAI) + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)
                mixed_text = ('新增客户联调演示科技，联系人赵经理，关注数据库加密。'
                              '我答应' + when.strftime('%Y年%m月%d日15点') + '发送数据库加密方案。')
                mixed = await service.handle('synthetic', 'mixed', mixed_text, force=True)
                step = 'mixed_capture'
                diagnostic = {'has_draft': bool(mixed.get('draft')), 'has_analysis': bool(mixed.get('analysis')),
                              'actions': len(mixed.get('actions', [])), 'proposals': len(mixed.get('proposals', [])),
                              'organize_failed': '自动整理暂未完成' in mixed.get('message', ''),
                              'needs_selection': bool(mixed.get('candidates'))}
                assert mixed.get('draft') and mixed.get('analysis') and mixed.get('actions')
                assert mixed.get('proposals') and all(p['status'] == 'pending' for p in mixed['proposals'])
                replay = await service.handle('synthetic', 'mixed', mixed_text, force=True)
                step = 'replay'
                assert replay == mixed
                coach = CoachingService(crm, SalesCoach(config['api_key'], config['model'], config['base_url']), lock)
                step = 'coaching'
                try:
                    coach.schedule('synthetic', confirmed['customer_id'])
                    await asyncio.gather(*list(coach.running.values()))
                    view = coach.view('synthetic', confirmed['customer_id'])
                    assert view['recommendation'] and not view['recommendation']['stale']
                    assert view['recommendation']['next_moves']
                finally:
                    await coach.close()
                assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
                print(json.dumps({'code': 'CUSTOMER_VOICE_MODEL_OK', 'create_confirmed': True,
                                  'update_pending': True, 'observation_preserved': True,
                                  'natural_multi_person_note': True, 'sales_coaching': True,
                                  'mixed_capture_pending_only': True, 'complete_replay': True,
                                  'real_database_untouched': True}))
            finally:
                crm.close()
    except Exception as error:
        print(json.dumps({'code': 'CUSTOMER_VOICE_MODEL_FAILED', 'exception_type': type(error).__name__, 'step': step,
                          'diagnostic': diagnostic}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
