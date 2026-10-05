"""Package only source, tests and public documentation; never configuration/data."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
destination = root / 'deploy' / 'crm-release.zip'
files = []
for folder in ('secretary', 'tests'):
    for path in (root / folder).rglob('*'):
        if path.is_file() and path.suffix in ('.py', '.html', '.css', '.js') and '__pycache__' not in path.parts:
            files.append(path)
files.extend(root / name for name in ('pyproject.toml', 'CRM_IMPLEMENTATION.md', 'README.md',
    'CUSTOMER_PROFILE_DESIGN.md', 'LISTEN_NOTE_GUIDE.md', 'deploy/CRM_GUIDE.md',
    'deploy/live_crm_verify.py', 'deploy/live_customer_verify.py', 'deploy/live_material_verify.py', 'deploy/training.py', 'deploy/使用指南与案例.html'))
with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, path.relative_to(root).as_posix())
print(f'RELEASE_PACKAGED; files={len(set(files))}; bytes={destination.stat().st_size}')
