"""Verify real model organization using synthetic text, without touching live data."""
import asyncio
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from secretary.__main__ import read_config
from secretary.organizer import InteractionOrganizer


async def main():
    logging.disable(logging.CRITICAL)
    try:
        config = read_config('.env')
        result = await InteractionOrganizer(config['api_key'], config['model'], config['base_url']).organize(
            '今天拜访了演示客户，我答应整理报价，暂时没有约具体时间。客户希望了解产品实施周期。',
            time.time(), {'name': '联调演示客户', 'stage': 'qualified'})
        assert result['summary'] and result['actions']
        assert all(action['remind_at'] is None for action in result['actions'])
        print(json.dumps({'code': 'CRM_MODEL_OK', 'actions': len(result['actions']),
                          'commitments': sum(action['kind'] == 'commitment' for action in result['actions']),
                          'all_times_unset': True}))
    except Exception as error:
        print(json.dumps({'code': 'CRM_MODEL_FAILED', 'exception_type': type(error).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
