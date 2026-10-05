"""Upgrade a stopped existing service after package preflight and data backup."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import time
import zipfile

from dotenv.main import DotEnv


PUBLIC_FILES = {
    'pyproject.toml', 'CRM_IMPLEMENTATION.md', 'README.md',
    'CUSTOMER_PROFILE_DESIGN.md', 'LISTEN_NOTE_GUIDE.md', 'deploy/CRM_GUIDE.md',
    'deploy/live_crm_verify.py', 'deploy/live_customer_verify.py',
    'deploy/live_material_verify.py', 'deploy/training.py', 'deploy/使用指南与案例.html',
}


def service_state():
    try:
        result = subprocess.run(
            ['systemctl', 'show', 'personal-secretary.service', '--no-pager',
             '--property=LoadState,ActiveState,SubState,MainPID,ControlPID'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            raise ValueError('Systemd status unavailable')
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    except Exception:
        raise RuntimeError('SERVICE_STOP_STATE_UNVERIFIED') from None


def require_stopped(probe):
    state = probe()
    if (not isinstance(state, dict) or state.get('LoadState') != 'loaded'
            or state.get('ActiveState') not in ('inactive', 'failed')
            or state.get('SubState') not in ('dead', 'failed')
            or str(state.get('MainPID')) != '0' or str(state.get('ControlPID')) != '0'):
        raise RuntimeError('DEPLOYMENT_REQUIRES_COMPLETED_SERVICE_STOP')


def _inside(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('RELEASE_TARGET_OUTSIDE_APPLICATION')
    return resolved


def preflight(root, archive):
    members, names, destinations, size = [], set(), set(), 0
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            name = member.filename
            relative = PurePosixPath(name)
            allowed = (
                bool(relative.parts)
                and relative.parts[0] in ('secretary', 'tests')
                and relative.suffix in ('.py', '.html', '.css', '.js')
            ) or name in PUBLIC_FILES
            key = name.casefold()
            size += member.file_size
            if (not name or relative.as_posix() != name or '\\' in name or ':' in name or relative.is_absolute()
                    or '..' in relative.parts
                    or any(part.startswith('.') or part == '__pycache__' for part in relative.parts)
                    or member.is_dir() or not allowed or key in names
                    or member.flag_bits & 1 or stat.S_ISLNK(member.external_attr >> 16)
                    or member.file_size > 20 * 1024 * 1024 or size > 64 * 1024 * 1024):
                raise ValueError('INVALID_RELEASE_MEMBER')
            destination = _inside(root, root / Path(*relative.parts))
            _inside(root, destination.with_name(destination.name + '.new'))
            if destination in destinations:
                raise ValueError('DUPLICATE_RELEASE_TARGET')
            names.add(key)
            destinations.add(destination)
            members.append((relative, destination, member))
        if not members or zipped.testzip() is not None:
            raise ValueError('INVALID_RELEASE_ARCHIVE')
        # Read every member before any filesystem mutation, including backups.
        return [(relative, destination, zipped.read(member))
                for relative, destination, member in members]


def database_paths(root, staged_env):
    paths = {}
    for role, env_file in (('current', root / '.env'), ('next', staged_env)):
        if role == 'next' and not env_file.is_file():
            continue
        # Match runtime load_dotenv(override=False), including interpolation precedence.
        values = DotEnv(env_file, override=False).dict() if env_file.is_file() else {}
        value = os.environ.get('SECRETARY_DB_PATH', values.get('SECRETARY_DB_PATH', 'data/secretary.sqlite3'))
        if not isinstance(value, str) or not value.strip() or '\0' in value:
            raise ValueError('INVALID_DATABASE_PATH')
        selected = Path(value)
        selected = (selected if selected.is_absolute() else root / selected).resolve()
        if selected.exists() and not selected.is_file():
            raise ValueError('DATABASE_PATH_NOT_A_FILE')
        paths.setdefault(selected, []).append(role)
    return paths


def install_release(root, *, service_probe=None):
    root = Path(root).resolve()
    probe = service_probe or service_state
    require_stopped(probe)
    staged_env = root / '.env.crm-pending'
    _inside(root, root / '.env')
    _inside(root, staged_env)
    payloads = preflight(root, root / 'deploy' / 'crm-release.zip')
    databases = database_paths(root, staged_env)
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1_000_000_000)
    backup = _inside(root, root / 'deploy' / ('rollback-' + stamp))
    backup.mkdir(mode=0o700)
    manifest = {'format': 1, 'databases': []}
    for index, (database, roles) in enumerate(databases.items()):
        name = 'secretary.sqlite3' if index == 0 else 'database-' + str(index) + '.sqlite3'
        existed = database.is_file()
        manifest['databases'].append({
            'source': str(database), 'backup': name if existed else None,
            'roles': roles, 'existed': existed,
        })
        if existed:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup / name)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('DATABASE_BACKUP_INTEGRITY_FAILED')
            os.chmod(backup / name, 0o600)
    for name in ('.env', 'pyproject.toml'):
        if (root / name).exists():
            shutil.copy2(root / name, backup / name)
            os.chmod(backup / name, 0o600)
    shutil.copytree(root / 'secretary', backup / 'secretary', ignore=shutil.ignore_patterns('__pycache__'))
    manifest_file = backup / 'databases.json'
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(manifest_file, 0o600)
    require_stopped(probe)
    for relative, destination, content in payloads:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.new')
        temporary.write_bytes(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    if staged_env.is_file():
        os.chmod(staged_env, 0o600)
        os.replace(staged_env, root / '.env')
    return backup


if __name__ == '__main__':
    try:
        location = install_release('/opt/personal-secretary')
    except Exception:
        raise SystemExit('CRM_RELEASE_NOT_INSTALLED; inspect stopped service, package and rollback backup') from None
    print('CRM_RELEASE_INSTALLED; rollback=' + str(location))
