from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import pytest

from app.main import create_app
from test_core import task_body


def services(tmp_path, now):
    return create_app(tmp_path, clock=lambda: now[0], start_workers=False).state.services


def test_automatic_backup_captures_wal_and_runs_once_per_day_after_restart(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    s = services(tmp_path, now)
    s.tasks.create(task_body())
    result = s.data_tools.automatic_backup()
    path = s.data_tools.rollback_dir / result['filename']
    assert path.is_file()
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {'manifest.json', 'data.sqlite'}
    preview = s.data_tools.validate_restore(path.read_bytes())
    assert preview['record_counts']['tasks'] == 1
    assert s.data_tools.automatic_backup() is None
    s.db.engine.dispose()
    restarted = services(tmp_path, now)
    assert restarted.data_tools.automatic_backup() is None
    now[0] += timedelta(days=1)
    assert restarted.data_tools.automatic_backup()


def test_automatic_retention_preserves_manual_backups_and_restoration_points(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    s = services(tmp_path, now)
    manual = s.data_tools.rollback_dir / 'manual.zip'
    rollback = s.data_tools.rollback_dir / 'pre-restore-test.sqlite'
    manual.write_bytes(b'manual')
    rollback.write_bytes(b'rollback')
    created = []
    for _ in range(7):
        created.append(s.data_tools.automatic_backup()['filename'])
        now[0] += timedelta(days=1)
    remaining = {p.name for p in s.data_tools.rollback_dir.glob('automatic-*.zip')}
    assert remaining == set(created[-5:])
    assert manual.read_bytes() == b'manual'
    assert rollback.read_bytes() == b'rollback'


def test_failed_automatic_backup_preserves_previous_copy_and_cleans_partial_file(tmp_path, monkeypatch):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    s = services(tmp_path, now)
    first = s.data_tools.automatic_backup()['filename']
    now[0] += timedelta(days=1)
    original = Path.write_bytes

    def fail(path, content):
        if path.suffix == '.partial':
            original(path, b'partial')
            raise OSError('disk full')
        return original(path, content)

    monkeypatch.setattr(Path, 'write_bytes', fail)
    with pytest.raises(OSError):
        s.data_tools.automatic_backup()
    assert (s.data_tools.rollback_dir / first).is_file()
    assert not list(s.data_tools.rollback_dir.glob('*.partial'))
    monkeypatch.setattr(Path, 'write_bytes', original)
    assert s.data_tools.automatic_backup()
