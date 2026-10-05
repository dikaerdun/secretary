"""Read MCP metadata only; credentials and recording contents are never printed."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import httpx
from secretary.listen_note import ListenNoteClient, _Session, PROTOCOL_VERSION, SUPPORTED_VERSIONS
from secretary.local import read_local_config


async def main():
    config = read_local_config(root=ROOT)
    connector = ListenNoteClient(config['listen_note_api_key'], config['listen_note_url'])
    connector._validate_configuration()
    session = _Session()
    async with httpx.AsyncClient(timeout=40, follow_redirects=False, trust_env=False) as client:
        initialized = await connector._rpc(client, session, 'initialize', {
            'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
            'clientInfo': {'name': 'personal-wecom-secretary-capability-check', 'version': '0.1.0'},
        })
        if initialized.get('protocolVersion') not in SUPPORTED_VERSIONS:
            raise ValueError('Unsupported protocol')
        session.version = initialized['protocolVersion']
        await connector._rpc(client, session, 'notifications/initialized', notification=True)
        cursor, seen, tools = None, set(), []
        for _ in range(20):
            result = await connector._rpc(client, session, 'tools/list', {'cursor': cursor} if cursor else {})
            for item in result.get('tools', []):
                schema = item.get('inputSchema', {})
                tools.append({'name': item['name'], 'description': item.get('description', ''),
                              'inputSchema': schema})
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise ValueError('Repeated metadata cursor')
            seen.add(cursor)
        else:
            raise ValueError('Incomplete metadata')
        evidence = {'checked_date': '2026-10-02', 'protocol': session.version,
                    'capabilities': initialized.get('capabilities', {}), 'tools': tools}
        target = ROOT / 'reviews' / '2026-10-02-listen-note-capabilities.json'
        target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        print('MCP_METADATA_CHECK_FAILED; no credentials or provider bodies displayed')
        raise SystemExit(1)
