"""Synthetic model smoke check. Never print credentials or generated content."""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import httpx
from secretary.local import read_local_config
from secretary.organizer import InteractionOrganizer
from secretary.customer_parser import CustomerVoiceParser


async def main():
    config = read_local_config(root=ROOT)
    statuses = []
    async def response_status(response):
        statuses.append(response.status_code)
    result = {'model': config['model'], 'checks': []}
    for trust in (True, False):
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=trust) as connection:
                response = await connection.post(config['base_url'] + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + config['api_key']},
                    json={'model': config['model'], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                          'max_tokens': 16, 'thinking': {'type': 'disabled'}, 'stream': False})
                result['checks'].append({'name': 'transport', 'trust_env': trust, 'http_status': response.status_code})
        except Exception as error:
            result['checks'].append({'name': 'transport', 'trust_env': trust,
                'error_type': type(error).__name__, 'cause_type': type(error.__cause__).__name__})
    async with httpx.AsyncClient(timeout=40, trust_env=False, event_hooks={'response': [response_status]}) as client:
        organizer = InteractionOrganizer(config['api_key'], config['model'], config['base_url'], client=client)
        parser = CustomerVoiceParser(config['api_key'], config['model'], config['base_url'], client=client)
        for name, function in (
            ('organization', lambda: organizer.organize('我答应发送部署方案，没有约具体时间。', time.time(), {})),
            ('customer', lambda: parser.parse('客户想了解数据库脱敏方案，没有确认客户名称。', time.time())),
        ):
            before = len(statuses)
            try:
                value = await function()
                result['checks'].append({'name': name, 'ok': True, 'http_status': statuses[before:]})
            except Exception as error:
                result['checks'].append({'name': name, 'ok': False, 'http_status': statuses[before:],
                                         'error_type': type(error).__name__})
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
