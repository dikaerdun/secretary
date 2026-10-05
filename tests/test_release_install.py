"""Isolated deployment oracles: preserve data and reject unsafe installs first."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import warnings
import zipfile

import pytest
from dotenv import load_dotenv


STOPPED = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
           'MainPID': '0', 'ControlPID': '0'}


@pytest.fixture
def install_release():
    path = Path(__file__).resolve().parents[1] / 'deploy' / 'install_crm_release.py'
    spec = importlib.util.spec_from_file_location('isolated_release_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.install_release


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv('SECRETARY_DB_PATH', raising=False)
    (tmp_path / 'deploy').mkdir()
    (tmp_path / 'secretary').mkdir()
    (tmp_path / 'data').mkdir()
    (tmp_path / 'secretary' / '__init__.py').write_text('VERSION = "old"\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="audit-old"\n')
    # This file is a newly created fixture containing no account credentials.
    (tmp_path / '.env').write_text('SECRETARY_DB_PATH=data/secretary.sqlite3\n')
    archive(tmp_path)
    return tmp_path


def archive(root, extra=()):
    with zipfile.ZipFile(root / 'deploy' / 'crm-release.zip', 'w', zipfile.ZIP_STORED) as zipped:
        zipped.writestr('secretary/__init__.py', 'VERSION = "new"\n')
        zipped.writestr('pyproject.toml', '[project]\nname="audit-new"\n')
        for name, content in extra:
            zipped.writestr(name, content)


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def seed_database(path, *, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.executescript('''
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT, status TEXT);
        CREATE TABLE notifications(id INTEGER PRIMARY KEY, task_id INTEGER, status TEXT);
        INSERT INTO customers VALUES (1, 'existing customer');
        INSERT INTO tasks VALUES (1, 'existing follow-up', 'pending');
        INSERT INTO notifications VALUES (1, 1, 'pending');
    ''')
    connection.commit()
    return connection


def database_rows(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
                for table in ('customers', 'tasks', 'notifications')}
    finally:
        connection.close()


@pytest.mark.parametrize('changed', [
    {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '42'},
    {'ActiveState': 'deactivating', 'SubState': 'stop-sigterm', 'MainPID': '42'},
    {'ActiveState': 'activating', 'SubState': 'start', 'ControlPID': '42'},
    {'MainPID': '42'},
    {'ControlPID': '42'},
    {'LoadState': 'not-found'},
])
def test_service_must_be_fully_stopped_before_any_write(root, install_release, changed):
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, SystemExit)):
        install_release(root, service_probe=lambda: {**STOPPED, **changed})
    assert snapshot(root) == before
    assert list((root / 'deploy').glob('rollback-*')) == []


@pytest.mark.parametrize('unsafe_name', ['outside.txt', '../outside.txt', 'secretary/../../outside.txt'])
def test_late_invalid_member_cannot_partially_replace_source(root, install_release, unsafe_name):
    archive(root, [(unsafe_name, 'invalid final member')])
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, SystemExit)):
        install_release(root, service_probe=lambda: STOPPED)
    assert snapshot(root) == before
    assert list((root / 'deploy').glob('rollback-*')) == []


def test_duplicate_members_rejected_before_any_write(root, install_release):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        archive(root, [('secretary/__init__.py', 'duplicate override')])
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, SystemExit)):
        install_release(root, service_probe=lambda: STOPPED)
    assert snapshot(root) == before


@pytest.mark.parametrize('alias', ['secretary/./__init__.py', 'secretary//__init__.py'])
def test_noncanonical_member_alias_cannot_override_same_destination(root, install_release, alias):
    archive(root, [(alias, 'noncanonical duplicate override')])
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, SystemExit)):
        install_release(root, service_probe=lambda: STOPPED)
    assert snapshot(root) == before


def test_bad_crc_rejected_before_any_write(root, install_release):
    path = root / 'deploy' / 'crm-release.zip'
    content = path.read_bytes()
    assert b'VERSION = "new"' in content
    path.write_bytes(content.replace(b'VERSION = "new"', b'VERSION = "bad"', 1))
    before = snapshot(root)
    with pytest.raises((RuntimeError, ValueError, SystemExit, zipfile.BadZipFile)):
        install_release(root, service_probe=lambda: STOPPED)
    assert snapshot(root) == before


@pytest.mark.parametrize('absolute', [False, True])
def test_configured_database_is_backed_up_with_latest_committed_wal(root, install_release, absolute):
    selected = root / 'data' / 'custom.sqlite3'
    value = str(selected) if absolute else 'data/custom.sqlite3'
    (root / '.env').write_text('SECRETARY_DB_PATH=' + value + '\n')
    original_config = (root / '.env').read_bytes()
    connection = seed_database(selected, wal=True)
    try:
        connection.execute("INSERT INTO tasks VALUES (2, 'latest committed in WAL', 'pending')")
        connection.execute("INSERT INTO notifications VALUES (2, 2, 'pending')")
        connection.commit()
        assert Path(str(selected) + '-wal').is_file()
        expected = database_rows(selected)
        backup = install_release(root, service_probe=lambda: STOPPED)
        assert isinstance(backup, Path) and backup.is_dir()
        assert (backup / '.env').read_bytes() == original_config
        assert (root / '.env').read_bytes() == original_config
        assert database_rows(selected) == expected
        candidates = list(backup.rglob('*.sqlite3'))
        assert len(candidates) == 1
        assert database_rows(candidates[0]) == expected
        manifest = json.loads((backup / 'databases.json').read_text(encoding='utf-8'))
        assert manifest == {'format': 1, 'databases': [
            {'source': str(selected.resolve()), 'backup': 'secretary.sqlite3',
             'roles': ['current'], 'existed': True}]}
        assert (backup / 'secretary' / '__init__.py').read_text() == 'VERSION = "old"\n'
        assert (root / 'secretary' / '__init__.py').read_text() == 'VERSION = "new"\n'
    finally:
        connection.close()


def test_process_database_path_priority_is_preserved(root, install_release, monkeypatch):
    selected = root / 'data' / 'process-selected.sqlite3'
    monkeypatch.setenv('SECRETARY_DB_PATH', str(selected))
    connection = seed_database(selected)
    connection.close()
    expected = database_rows(selected)
    backup = install_release(root, service_probe=lambda: STOPPED)
    candidates = list(backup.rglob('*.sqlite3'))
    assert len(candidates) == 1
    assert database_rows(candidates[0]) == expected
    assert database_rows(selected) == expected
    manifest = json.loads((backup / 'databases.json').read_text(encoding='utf-8'))
    assert manifest['databases'] == [
        {'source': str(selected.resolve()), 'backup': 'secretary.sqlite3',
         'roles': ['current'], 'existed': True}]


def test_database_interpolation_matches_runtime_process_priority(root, install_release, monkeypatch):
    runtime_directory = root / 'data' / 'runtime-selected'
    monkeypatch.setenv('DATA_DIR', str(runtime_directory))
    (root / '.env').write_text('DATA_DIR=data/file-declared\n'
                              'SECRETARY_DB_PATH=${DATA_DIR}/custom.sqlite3\n')
    # Obtain the oracle through the actual runtime library call. Track the
    # output variable explicitly so this synthetic load never leaks into other
    # tests or masks the installer's independent interpolation step.
    with monkeypatch.context() as runtime:
        runtime.setenv('SECRETARY_DB_PATH', '__tracked_fixture__')
        runtime.delenv('SECRETARY_DB_PATH')
        load_dotenv(root / '.env', override=False)
        selected = Path(os.environ['SECRETARY_DB_PATH']).resolve()
    assert selected == (runtime_directory / 'custom.sqlite3').resolve()
    assert 'SECRETARY_DB_PATH' not in os.environ
    connection = seed_database(selected)
    connection.close()
    decoy = root / 'data' / 'file-declared' / 'custom.sqlite3'
    decoy_connection = seed_database(decoy)
    decoy_connection.execute("UPDATE customers SET name='file-declared decoy'")
    decoy_connection.commit()
    decoy_connection.close()
    expected = database_rows(selected)
    backup = install_release(root, service_probe=lambda: STOPPED)
    manifest = json.loads((backup / 'databases.json').read_text(encoding='utf-8'))
    assert manifest['databases'] == [
        {'source': str(selected), 'backup': 'secretary.sqlite3',
         'roles': ['current'], 'existed': True}]
    assert database_rows(backup / 'secretary.sqlite3') == expected
    assert database_rows(selected) == expected


def test_current_and_staged_databases_both_backed_up_with_exact_mapping(root, install_release):
    current = root / 'data' / 'secretary.sqlite3'
    next_database = root / 'data' / 'next.sqlite3'
    current_connection = seed_database(current)
    current_connection.close()
    next_connection = seed_database(next_database)
    next_connection.execute("UPDATE customers SET name='next config customer'")
    next_connection.commit()
    next_connection.close()
    old_config = (root / '.env').read_bytes()
    new_config = b'SECRETARY_DB_PATH=data/next.sqlite3\n'
    (root / '.env.crm-pending').write_bytes(new_config)
    old_rows, next_rows = database_rows(current), database_rows(next_database)
    backup = install_release(root, service_probe=lambda: STOPPED)
    manifest = json.loads((backup / 'databases.json').read_text(encoding='utf-8'))
    assert manifest == {'format': 1, 'databases': [
        {'source': str(current.resolve()), 'backup': 'secretary.sqlite3',
         'roles': ['current'], 'existed': True},
        {'source': str(next_database.resolve()), 'backup': 'database-1.sqlite3',
         'roles': ['next'], 'existed': True}]}
    assert database_rows(backup / 'secretary.sqlite3') == old_rows
    assert database_rows(backup / 'database-1.sqlite3') == next_rows
    assert database_rows(current) == old_rows
    assert database_rows(next_database) == next_rows
    assert (backup / '.env').read_bytes() == old_config
    assert (root / '.env').read_bytes() == new_config
    assert not (root / '.env.crm-pending').exists()


def test_equal_current_and_staged_database_has_one_backup_and_both_roles(root, install_release):
    selected = root / 'data' / 'secretary.sqlite3'
    connection = seed_database(selected)
    connection.close()
    (root / '.env.crm-pending').write_text('SECRETARY_DB_PATH=' + str(selected) + '\n')
    backup = install_release(root, service_probe=lambda: STOPPED)
    manifest = json.loads((backup / 'databases.json').read_text(encoding='utf-8'))
    assert manifest == {'format': 1, 'databases': [
        {'source': str(selected.resolve()), 'backup': 'secretary.sqlite3',
         'roles': ['current', 'next'], 'existed': True}]}
    assert len(list(backup.glob('*.sqlite3'))) == 1


def test_service_restart_after_backup_does_not_replace_source_or_config(root, install_release):
    selected = root / 'data' / 'secretary.sqlite3'
    connection = seed_database(selected)
    connection.close()
    source_before = (root / 'secretary' / '__init__.py').read_bytes()
    config_before = (root / '.env').read_bytes()
    staged = b'SECRETARY_DB_PATH=data/new.sqlite3\n'
    (root / '.env.crm-pending').write_bytes(staged)
    probes = []

    def probe():
        probes.append(1)
        if len(probes) == 1:
            return STOPPED
        return {**STOPPED, 'ActiveState': 'active', 'SubState': 'running', 'MainPID': '42'}

    expected = database_rows(selected)
    with pytest.raises((RuntimeError, ValueError, SystemExit)):
        install_release(root, service_probe=probe)
    assert len(probes) == 2
    assert (root / 'secretary' / '__init__.py').read_bytes() == source_before
    assert (root / '.env').read_bytes() == config_before
    assert (root / '.env.crm-pending').read_bytes() == staged
    assert database_rows(selected) == expected
    backups = list((root / 'deploy').glob('rollback-*'))
    assert len(backups) == 1
    manifest = json.loads((backups[0] / 'databases.json').read_text(encoding='utf-8'))
    assert manifest['databases'] == [
        {'source': str(selected.resolve()), 'backup': 'secretary.sqlite3',
         'roles': ['current'], 'existed': True},
        {'source': str((root / 'data' / 'new.sqlite3').resolve()), 'backup': None,
         'roles': ['next'], 'existed': False}]
    assert database_rows(backups[0] / 'secretary.sqlite3') == expected
