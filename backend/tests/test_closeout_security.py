import io
import json
import sqlite3
import zipfile
from contextlib import closing

import pytest

from app.domain import AppError
from app.main import create_app
from test_core import task_body


def old_backup(services, *, trigger=False):
    from app.data_tools import digest
    artifact=services.data_tools.backup()
    path,_=services.data_tools.artifact(artifact['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        database=archive.read('data.sqlite')
    staged=services.db.directory/'old-copy.sqlite'
    staged.write_bytes(database)
    with closing(sqlite3.connect(staged)) as connection:
        connection.execute('DROP TABLE mail_parse_cache')
        connection.execute("UPDATE alembic_version SET version_num='0002_applications'")
        if trigger:
            connection.execute('CREATE TRIGGER untrusted AFTER INSERT ON tasks BEGIN DELETE FROM tasks; END')
        connection.commit()
    database=staged.read_bytes()
    staged.unlink()
    manifest['schema_revision']='0002_applications'
    manifest['files']=[{'name':'data.sqlite','size':len(database),'sha256':digest(database)}]
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('manifest.json',json.dumps(manifest))
        archive.writestr('data.sqlite',database)
    return output.getvalue()


def test_known_older_backup_upgrades_only_the_isolated_copy(tmp_path):
    services=create_app(tmp_path,start_workers=False).state.services
    task=services.tasks.create(task_body())
    content=old_backup(services)
    validated=services.data_tools.validate_restore(content)
    preview=services.data_tools.restore_preview(validated['restore_id'])
    assert preview['migrated_from']=='0002_applications'
    assert services.tasks.get(task['id'])['title']==task['title']
    services.data_tools.apply_restore(validated['restore_id'],preview['confirmation_token'])
    assert services.tasks.get(task['id'])['title']==task['title']


def test_genuine_v1_backup_without_application_columns_can_migrate(tmp_path):
    from sqlalchemy import create_engine
    from alembic import command
    from app.backup_schema import config
    from app.data_tools import digest
    services=create_app(tmp_path,start_workers=False).state.services
    path=tmp_path/'genuine-v1.sqlite'
    engine=create_engine(f'sqlite:///{path.as_posix()}')
    cfg=config()
    with engine.connect() as connection:
        cfg.attributes['connection']=connection
        command.upgrade(cfg,'0001_p0')
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        assert 'application_id' not in {r[1] for r in connection.execute('PRAGMA table_info(tasks)')}
        from app.tasks import DEFAULT_SETTINGS
        connection.execute('INSERT INTO settings(id,created_at,updated_at,version,value) VALUES(?,?,?,?,?)',
                           ('profile',0,0,1,json.dumps(DEFAULT_SETTINGS)))
        connection.commit()
    data=path.read_bytes()
    manifest={'format_version':1,'schema_revision':'0001_p0','created_at':'2026-11-09T00:00:00Z',
              'credentials_included':False,'record_counts':{},'files':[{'name':'data.sqlite','size':len(data),'sha256':digest(data)}]}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('manifest.json',json.dumps(manifest))
        archive.writestr('data.sqlite',data)
    validated=services.data_tools.validate_restore(output.getvalue())
    preview=services.data_tools.restore_preview(validated['restore_id'])
    assert preview['migrated_from']=='0001_p0'
    upgraded=services.data_tools.staging_dir/(validated['restore_id']+'.sqlite')
    with closing(sqlite3.connect(upgraded)) as connection:
        sql=connection.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        assert 'CHECK (version >= 1)' in sql


def test_previous_p0_model_created_schema_with_different_column_order_is_accepted(tmp_path):
    from sqlalchemy import create_engine
    from app.backup_schema import validate_and_upgrade
    from app.persistence import Base, SCHEMA_REVISION
    path=tmp_path/'previous-p0.sqlite'
    engine=create_engine(f'sqlite:///{path.as_posix()}')
    # Previous P0's first migration used create_all: application_id came first,
    # whereas the frozen historical migration adds it at the end.
    Base.metadata.create_all(engine,tables=[t for t in Base.metadata.sorted_tables if t.name!='mail_parse_cache'])
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)')
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0002_applications')")
    engine.dispose()
    assert validate_and_upgrade(path,'0002_applications')=='0002_applications'
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()==(SCHEMA_REVISION,)


def test_legacy_backup_with_trigger_is_refused_before_migrating(tmp_path,monkeypatch):
    from alembic import command
    services=create_app(tmp_path,start_workers=False).state.services
    content=old_backup(services,trigger=True)
    calls=[]
    monkeypatch.setattr(command,'upgrade',lambda *args:calls.append(True))
    with pytest.raises(AppError):
        services.data_tools.validate_restore(content)
    assert calls==[]


def test_diagnostic_export_reconstructs_whitelisted_records_without_personal_data(tmp_path):
    services=create_app(tmp_path,start_workers=False).state.services
    private='private@example.test body-secret C:/Users/private token=private'
    logs=tmp_path/'logs'
    (logs/'injected.log').write_text(private)
    (logs/'backend.log').write_text(json.dumps({'timestamp_utc':'2030-01-01T00:00:00Z',
        'event':'task.changed','run_id':'bad-'+private,'title':private,'safe_detail':private})+'\n'+private)
    artifact=services.diagnostics.export()
    path,_=services.data_tools.artifact(artifact['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist())=={'manifest.json','health.json','logs.jsonl'}
        assert all(private.encode() not in archive.read(name) for name in archive.namelist())
        assert archive.read('logs.jsonl')


def test_business_logs_contain_ids_and_results_but_no_titles_or_credentials(tmp_path):
    services=create_app(tmp_path,start_workers=False).state.services
    body=task_body()
    body['title']='DO_NOT_LOG_PRIVATE_TITLE'
    services.tasks.create(body)
    logs=(tmp_path/'logs'/'backend.log').read_text(encoding='utf-8')
    assert 'task.created' in logs
    assert 'DO_NOT_LOG_PRIVATE_TITLE' not in logs


def test_restore_refuses_invalid_settings_before_replacing_live_data(tmp_path):
    from app.data_tools import digest
    services=create_app(tmp_path,start_workers=False).state.services
    task=services.tasks.create(task_body())
    artifact=services.data_tools.backup()
    path,_=services.data_tools.artifact(artifact['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        raw=archive.read('data.sqlite')
    staged=tmp_path/'invalid-settings.sqlite'
    staged.write_bytes(raw)
    with closing(sqlite3.connect(staged)) as connection:
        connection.execute("UPDATE settings SET value=? WHERE id='profile'",(json.dumps({'timezone':'Invalid/Zone'}),))
        connection.commit()
    raw=staged.read_bytes()
    manifest['files']=[{'name':'data.sqlite','size':len(raw),'sha256':digest(raw)}]
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('manifest.json',json.dumps(manifest))
        archive.writestr('data.sqlite',raw)
    with pytest.raises(AppError):
        services.data_tools.validate_restore(output.getvalue())
    assert services.tasks.get(task['id'])['title']==task['title']
