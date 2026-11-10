import sqlite3
from contextlib import closing

import pytest

from app.domain import AppError
from app.main import create_app
from app.persistence import Database, SCHEMA_REVISION
from test_core import task_body


def downgrade_fixture(services, revision):
    """Use the real migration chain so new tables never leak into old fixtures."""
    from alembic import command
    from sqlalchemy import create_engine
    from app.backup_schema import config

    services.db.engine.dispose()
    engine = create_engine(f"sqlite:///{services.db.path.as_posix()}")
    try:
        cfg = config()
        with engine.connect() as connection:
            cfg.attributes['connection'] = connection
            command.downgrade(cfg, revision)
    finally:
        engine.dispose()


def old_database(path):
    s = create_app(path, start_workers=False).state.services
    task = s.tasks.create(task_body())
    downgrade_fixture(s, "0001_p0")
    return s.db.path, task['id']


def test_upgrade_first_migrates_copy_and_preserves_preupgrade_snapshot(tmp_path, monkeypatch):
    from app import persistence
    path, task_id = old_database(tmp_path)
    original = persistence.command.upgrade
    targets = []

    def inspect(config, revision):
        targets.append(config.attributes['connection'].engine.url.database)
        original(config, revision)

    monkeypatch.setattr(persistence.command, 'upgrade', inspect)
    db = Database(tmp_path)
    assert len(targets) == 2
    assert targets[0] != targets[1] == path.as_posix()
    backups = list((tmp_path / 'backups').glob('pre-upgrade-*.sqlite'))
    assert len(backups) == 1
    with closing(sqlite3.connect(str(backups[0]))) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0001_p0'
        assert connection.execute('SELECT id FROM tasks').fetchone()[0] == task_id
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT version_num FROM alembic_version').scalar() == SCHEMA_REVISION


@pytest.mark.parametrize('fail_in_trial', [True, False])
def test_failed_upgrade_retains_original_data_and_schema(tmp_path, monkeypatch, fail_in_trial):
    from app import persistence
    path, task_id = old_database(tmp_path)
    original = persistence.command.upgrade

    def fail(config, revision):
        connection = config.attributes['connection']
        trial = connection.engine.url.database != path.as_posix()
        if trial == fail_in_trial:
            connection.exec_driver_sql('CREATE TABLE partial_upgrade(value TEXT)')
            connection.commit()
            raise RuntimeError('migration interrupted')
        original(config, revision)

    monkeypatch.setattr(persistence.command, 'upgrade', fail)
    with pytest.raises(AppError) as exc:
        Database(tmp_path)
    assert exc.value.code == 'MIGRATION_FAILED'
    with closing(sqlite3.connect(str(path))) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0001_p0'
        assert connection.execute('SELECT id FROM tasks').fetchone()[0] == task_id
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='partial_upgrade'").fetchone()
    assert not list((tmp_path / 'staging').glob('migration-*.sqlite*'))


def test_newer_schema_is_refused_before_upgrade(tmp_path):
    path, _ = old_database(tmp_path)
    with closing(sqlite3.connect(str(path))) as connection:
        connection.execute("UPDATE alembic_version SET version_num='9999_future'")
        connection.commit()
    with pytest.raises(AppError) as exc:
        Database(tmp_path)
    assert exc.value.code == 'DATABASE_VERSION_UNSUPPORTED'
    with closing(sqlite3.connect(str(path))) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '9999_future'


def test_p0_live_upgrade_adds_event_details_without_changing_task_or_reminders(tmp_path):
    s=create_app(tmp_path,start_workers=False).state.services
    task=s.tasks.create(task_body())
    downgrade_fixture(s, "0003_parse_cache")
    with closing(sqlite3.connect(s.db.path)) as connection:
        reminders=list(connection.execute('SELECT id,state,fire_at FROM reminders ORDER BY id'))
    upgraded=create_app(tmp_path,start_workers=False).state.services
    saved=upgraded.tasks.get(task['id'])
    assert saved['starts_at']==task['starts_at']
    assert saved['version']==task['version']
    assert saved['event_details']=={'location':'','meeting_url':''}
    with closing(sqlite3.connect(upgraded.db.path)) as connection:
        assert list(connection.execute('SELECT id,state,fire_at FROM reminders ORDER BY id'))==reminders
    snapshots=list((tmp_path/'backups').glob('pre-upgrade-*.sqlite'))
    assert len(snapshots)==1
    with closing(sqlite3.connect(snapshots[0])) as connection:
        assert 'event_details' not in {r[1] for r in connection.execute('PRAGMA table_info(tasks)')}
