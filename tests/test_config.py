import os

import pytest

from secretary.__main__ import InstanceLock, read_config
from secretary.gateway import GatewayError


def test_config_never_reveals_secret(monkeypatch, tmp_path):
    for key in ['WECOM_BOT_ID', 'WECOM_BOT_SECRET', 'WECOM_ALLOWED_USER_IDS', 'DEEPSEEK_API_KEY']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('WECOM_BOT_SECRET', 'never-print-this-secret')
    with pytest.raises(GatewayError) as error:
        read_config(str(tmp_path / 'absent.env'))
    assert 'never-print-this-secret' not in str(error.value)
    assert 'WECOM_BOT_ID' in str(error.value)


def test_second_instance_cannot_open_same_database(tmp_path):
    path = tmp_path / 'secretary.lock'
    with InstanceLock(path):
        with pytest.raises(GatewayError):
            with InstanceLock(path):
                pytest.fail('second instance acquired lock')
    with InstanceLock(path):
        pass


@pytest.mark.parametrize('url', ['http://example.test/mcp', 'https://key-secret@example.test/mcp',
                               'https://example.test/mcp?key=key-secret', 'https://example.test/mcp#key-secret'])
def test_listen_note_configuration_rejects_unsafe_endpoint_without_echo(monkeypatch, tmp_path, url):
    for key in ['WECOM_BOT_ID', 'WECOM_BOT_SECRET', 'WECOM_ALLOWED_USER_IDS', 'DEEPSEEK_API_KEY']:
        monkeypatch.setenv(key, 'test-value')
    monkeypatch.setenv('SECRETARY_WEB_ENABLED', '0')
    monkeypatch.setenv('LISTEN_NOTE_MCP_URL', url)
    with pytest.raises(GatewayError) as error:
        read_config(str(tmp_path / 'absent.env'))
    assert 'key-secret' not in str(error.value)
