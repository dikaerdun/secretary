"""Read-only deployment checks, keeping login details and customer data out of output."""
import hashlib
import json
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
access = json.loads((root / 'deploy/web-access.local.json').read_text(encoding='utf-8'))
with httpx.Client(base_url=access['url'].rstrip('/'), timeout=15) as client:
    login = client.post('/api/login', json={'password': access['password']})
    login.raise_for_status()
    dashboard = client.get('/api/dashboard')
    dashboard.raise_for_status()
    inbox = client.get('/api/review-inbox')
    inbox.raise_for_status()
    audio = client.get('/api/audio/capabilities')
    audio.raise_for_status()
    settings = client.get('/api/voice-settings')
    settings.raise_for_status()
    agendas = [client.get('/api/agenda', params={'period': period}) for period in ('day', 'week', 'month')]
    for item in agendas:
        item.raise_for_status()
    js = client.get('/static/app.js')
    css = client.get('/static/app.css')
    js.raise_for_status()
    css.raise_for_status()
    result = {'code': 'DEPLOYED_WEB_OK', 'login': True, 'bot_connected': dashboard.json()['bot_connected'],
              'queues': set(dashboard.json().get('queues', {})) >= {'overdue', 'today', 'pending_schedule', 'needs_time'},
              'review_inbox': all(key in inbox.json() for key in ('customers', 'proposals', 'actions')),
              'agendas': all(item.status_code == 200 for item in agendas),
              'audio_not_enabled_without_key': audio.json()['can_transcribe'] is False,
              'hotword_configuration': isinstance(settings.json().get('hotwords'), list),
              'javascript_matches': hashlib.sha256(js.content).digest() == hashlib.sha256((root/'secretary/static/app.js').read_bytes()).digest(),
              'css_matches': hashlib.sha256(css.content).digest() == hashlib.sha256((root/'secretary/static/app.css').read_bytes()).digest()}
    client.post('/api/logout', json={}, headers={'X-CSRF-Token': login.json()['csrf']}).raise_for_status()
print(json.dumps(result))
assert all(value is True for key, value in result.items() if key != 'code')
