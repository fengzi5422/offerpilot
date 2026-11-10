"""Preparation checklists are independent, versioned and retained across parent changes."""
import io
import json
import sqlite3
import zipfile
from datetime import datetime

import pytest
from sqlalchemy import select

from app.domain import AppError
from test_core import task_body


@pytest.fixture
def prepared(app):
    from app.preparation import PreparationService
    services = app.state.services
    parent = services.tasks.create(task_body())
    return services, PreparationService(services.db, services.tasks.clock, services.tasks), parent


def test_template_is_idempotent_and_preserves_edited_or_deleted_items(prepared):
    services, service, parent = prepared
    first = service.apply_template(parent['id'], parent['version'])
    assert len(first['items']) >= 3
    item = first['items'][0]
    edited = service.patch(parent['id'], item['id'], first['parent_version'], item['version'],
                           {'text': '我的准备计划', 'completed': True})
    second = service.apply_template(parent['id'], edited['parent_version'])
    assert len(second['items']) == len(first['items'])
    retained = next(row for row in second['items'] if row['id'] == item['id'])
    assert retained['text'] == '我的准备计划' and retained['completed']
    deleted = service.delete(parent['id'], item['id'], second['parent_version'], retained['version'])
    repeated = service.apply_template(parent['id'], deleted['parent_version'])
    assert item['id'] not in {row['id'] for row in repeated['items']}
    assert len(services.tasks.list()) == 1  # A template never invents a reminder time.


def test_parent_and_child_versions_and_parent_membership_are_enforced(prepared):
    services, service, parent = prepared
    created = service.create(parent['id'], parent['version'], {'text': '打印简历'})
    item = created['items'][0]
    with pytest.raises(AppError) as conflict:
        service.create(parent['id'], parent['version'], {'text': 'stale'})
    assert conflict.value.code == 'VERSION_CONFLICT'
    with pytest.raises(AppError) as conflict:
        service.patch(parent['id'], item['id'], created['parent_version'], 999, {'text': 'stale'})
    assert conflict.value.code == 'VERSION_CONFLICT'
    other = services.tasks.create(task_body())
    with pytest.raises(AppError) as mismatch:
        service.delete(other['id'], item['id'], other['version'], item['version'])
    assert mismatch.value.code == 'NOT_FOUND'
    assert service.list(parent['id'])['items'][0]['text'] == '打印简历'


def test_parent_reschedule_and_cancel_preserve_items_with_notice(prepared):
    services, service, parent = prepared
    first = service.create(parent['id'], parent['version'], {'text': '带上证件'})
    temporal = {**parent['temporal'], 'starts_at': '2030-01-03T07:00:00Z'}
    moved = services.tasks.patch(parent['id'], first['parent_version'], {'temporal': temporal})
    result = service.list(parent['id'])
    assert result['items'][0]['text'] == '带上证件'
    assert result['items'][0]['parent_changed'] and result['parent_notice']
    services.tasks.transition(parent['id'], moved['version'], 'cancel')
    result = service.list(parent['id'])
    assert len(result['items']) == 1 and '取消' in result['parent_notice']


def test_explicit_reminder_creates_independent_task_and_tracks_completion(prepared):
    from app.persistence import Reminder
    services, service, parent = prepared
    at = '2030-01-01T10:00:00Z'
    created = service.create(parent['id'], parent['version'], {'text': '测试摄像头', 'remind_at': at})
    item = created['items'][0]
    reminder_task = services.tasks.get(item['reminder_task_id'])
    assert reminder_task['kind'] == 'custom' and reminder_task['due_at'] == at
    assert reminder_task['source']['preparation_id'] == item['id']
    assert reminder_task['id'] != parent['id']
    with services.db.read() as session:
        reminders = list(session.scalars(select(Reminder).where(
            Reminder.task_id == reminder_task['id'], Reminder.state == 'pending')))
        assert len(reminders) == 1
        assert reminders[0].offset_seconds == 0
        assert reminders[0].fire_at == int(datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp() * 1000)
        assert reminders[0].expires_at == reminders[0].fire_at + 600000
    done = service.patch(parent['id'], item['id'], created['parent_version'], item['version'], {'completed': True})
    assert services.tasks.get(reminder_task['id'])['status'] == 'done'
    updated = done['items'][0]
    reopened = service.patch(parent['id'], item['id'], done['parent_version'], updated['version'], {'completed': False})
    assert services.tasks.get(reminder_task['id'])['status'] == 'pending'
    service.delete(parent['id'], item['id'], reopened['parent_version'], reopened['items'][0]['version'])
    assert services.tasks.get(reminder_task['id'])['status'] == 'cancelled'
    assert services.tasks.get(parent['id'])['status'] == 'pending'
    with services.db.read() as session:
        assert not list(session.scalars(select(Reminder).where(
            Reminder.task_id == reminder_task['id'], Reminder.state.in_(['pending', 'claimed', 'retry_wait']))))


def test_task_page_edits_sync_checklist_and_are_not_overwritten(prepared):
    services, service, parent = prepared
    first = service.create(parent['id'], parent['version'], {'text': '设备', 'remind_at': '2030-01-01T10:00:00Z'})
    item = first['items'][0]
    task = services.tasks.get(item['reminder_task_id'])
    edited = services.tasks.patch(task['id'], task['version'], {'title': '我的设备准备', 'notes': '保留手写备注',
        'priority': 1, 'temporal': {**task['temporal'], 'due_at': '2030-01-01T11:00:00Z'}})
    done = services.tasks.transition(edited['id'], edited['version'], 'complete')
    current = service.list(parent['id'])
    item = current['items'][0]
    assert item['completed'] and item['text'] == '我的设备准备' and item['remind_at'] == '2030-01-01T11:00:00Z'
    service.patch(parent['id'], item['id'], current['parent_version'], item['version'], {'text': '再次编辑内容'})
    retained = services.tasks.get(done['id'])
    assert retained['status'] == 'done' and retained['due_at'] == '2030-01-01T11:00:00Z'
    assert retained['notes'] == '保留手写备注' and retained['priority'] == 1
    services.tasks.transition(retained['id'], retained['version'], 'cancel')
    assert service.list(parent['id'])['items'][0]['remind_at'] is None


def test_clearing_reminder_suppresses_only_its_independent_task(prepared):
    services, service, parent = prepared
    created = service.create(parent['id'], parent['version'], {'text': '确认网络', 'remind_at': '2030-01-01T10:00:00Z'})
    item = created['items'][0]
    changed = service.patch(parent['id'], item['id'], created['parent_version'], item['version'], {'remind_at': None})
    assert changed['items'][0]['remind_at'] is None
    assert services.tasks.get(item['reminder_task_id'])['status'] == 'cancelled'
    assert services.tasks.get(parent['id'])['next_reminder_at'] is not None


@pytest.mark.parametrize('value', [
    {'text': ''}, {'text': ' '}, {'text': 'x' * 201}, {'text': 'x', 'completed': 'false'},
    {'text': 'x', 'remind_at': '2030-01-01T10:00:00'}, {'text': 'x', 'remind_at': 'not-a-date'},
    {'text': 'x', 'remind_at': '2000-01-01T10:00:00Z'}, {'text': 'x', 'task_id': 'arbitrary'},
])
def test_invalid_values_do_not_modify_parent_or_create_tasks(prepared, value):
    services, service, parent = prepared
    with pytest.raises(AppError) as invalid:
        service.create(parent['id'], parent['version'], value)
    assert invalid.value.code == 'VALIDATION_ERROR'
    assert service.list(parent['id']) == {'items': [], 'parent_version': parent['version'], 'parent_notice': None}
    assert len(services.tasks.list()) == 1


def test_backup_export_and_privacy_counts_include_preparations(prepared):
    services, service, parent = prepared
    service.create(parent['id'], parent['version'], {'text': '=准备简历'})
    tools = services.data_tools
    artifact = tools.export('json')
    path, _ = tools.artifact(artifact['artifact_id'])
    assert json.loads(path.read_text(encoding='utf-8'))['preparations'][0]['text'] == '=准备简历'
    artifact = tools.export('csv', ['preparations'])
    path, _ = tools.artifact(artifact['artifact_id'])
    assert "'=准备简历" in path.read_text(encoding='utf-8-sig')
    artifact = tools.backup()
    path, _ = tools.artifact(artifact['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['schema_revision'] == '0005_preparations'
        assert manifest['record_counts']['preparations'] == 1
    validated = tools.validate_restore(path.read_bytes())
    assert validated['record_counts']['preparations'] == 1
    preview = tools.privacy_preview('all')
    assert preview['counts']['preparations'] == 1
    tools.purge('all', preview['preview_token'])
    from app.persistence import Preparation
    with services.db.read() as session:
        assert not list(session.scalars(select(Preparation)))


def test_version_0004_backup_is_strictly_validated_then_upgraded(prepared, tmp_path):
    from alembic import command
    from sqlalchemy import create_engine
    from app.backup_schema import config
    from app.data_tools import digest
    services, _, _ = prepared
    path = tmp_path / 'historical.sqlite'
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    cfg = config()
    with engine.connect() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, '0004_event_details')
        connection.exec_driver_sql("INSERT INTO settings (id,created_at,updated_at,version,value) VALUES ('profile',1,1,1,'{}')")
        connection.commit()
    engine.dispose()
    data = path.read_bytes()
    manifest = {'format_version': 1, 'schema_revision': '0004_event_details', 'credentials_included': False,
                'record_counts': {'tasks': 0}, 'files': [{'name': 'data.sqlite', 'size': len(data), 'sha256': digest(data)}]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('data.sqlite', data)
    result = services.data_tools.validate_restore(buffer.getvalue())
    restored = services.data_tools.staging_dir / (result['restore_id'] + '.sqlite')
    with sqlite3.connect(restored) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone() == ('0005_preparations',)
        assert connection.execute('SELECT COUNT(*) FROM preparations').fetchone() == (0,)
    # Historical revision labels must not permit an extra, unrecognized table.
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unexpected(id TEXT)')
    data = path.read_bytes()
    manifest['files'][0].update(size=len(data), sha256=digest(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('data.sqlite', data)
    with pytest.raises(AppError) as invalid:
        services.data_tools.validate_restore(buffer.getvalue())
    assert invalid.value.code == 'BACKUP_SCHEMA_INVALID'


def test_reminder_retime_keeps_task_identity_and_parent_schedule(prepared):
    services, service, parent = prepared
    created = service.create(parent['id'], parent['version'], {'text': '原准备内容', 'remind_at': '2030-01-01T10:00:00Z'})
    item = created['items'][0]
    updated = service.patch(parent['id'], item['id'], created['parent_version'], item['version'],
                            {'text': '新准备内容', 'remind_at': '2030-01-01T11:00:00Z'})
    current = updated['items'][0]
    assert current['reminder_task_id'] == item['reminder_task_id']
    reminder = services.tasks.get(current['reminder_task_id'])
    assert reminder['title'] == '新准备内容' and reminder['due_at'] == '2030-01-01T11:00:00Z'
    assert services.tasks.get(parent['id'])['starts_at'] == parent['starts_at']
    assert services.tasks.get(parent['id'])['schedule_revision'] == parent['schedule_revision']


def test_actual_restore_preserves_preparation_identity_and_independent_reminder(prepared):
    services, service, parent = prepared
    created = service.create(parent['id'], parent['version'], {'text': '恢复后的准备', 'remind_at': '2030-01-01T10:00:00Z'})
    item = created['items'][0]
    artifact = services.data_tools.backup()
    path, _ = services.data_tools.artifact(artifact['artifact_id'])
    validated = services.data_tools.validate_restore(path.read_bytes())
    preview = services.data_tools.restore_preview(validated['restore_id'])
    services.data_tools.apply_restore(validated['restore_id'], preview['confirmation_token'])
    restored = service.list(parent['id'])['items'][0]
    assert restored == item
    linked = services.tasks.get(restored['reminder_task_id'])
    assert linked['source']['preparation_id'] == item['id']
    assert linked['due_at'] == '2030-01-01T10:00:00Z'


def test_source_evidence_purge_preserves_preparation_reminder_marker(prepared):
    services, service, parent = prepared
    created = service.create(parent['id'], parent['version'], {'text': '独立准备', 'remind_at': '2030-01-01T10:00:00Z'})
    item = created['items'][0]
    preview = services.data_tools.privacy_preview('source_evidence')
    services.data_tools.purge('source_evidence', preview['preview_token'])
    linked = services.tasks.get(item['reminder_task_id'])
    assert linked['source']['preparation_id'] == item['id']
    assert service.list(parent['id'])['items'][0]['text'] == '独立准备'


def test_api_checklist_mutations_replay_without_losing_parent_version(client):
    parent = client.post('/api/v1/tasks', json=task_body()).json()['data']
    base = f"/api/v1/tasks/{parent['id']}/preparations"
    headers = {'Idempotency-Key': '31ee3fba-d5ba-451d-8c45-33baaa96a35a'}
    body = {'expected_parent_version': parent['version'], 'text': '通过界面准备'}
    first = client.post(base, json=body, headers=headers)
    assert first.status_code == 201, first.text
    replay = client.post(base, json=body, headers=headers)
    assert replay.json()['data'] == first.json()['data']
    created = first.json()['data']
    item = created['items'][0]
    changed = client.patch(base + '/' + item['id'], json={
        'expected_parent_version': created['parent_version'], 'expected_version': item['version'],
        'changes': {'completed': True}})
    assert changed.status_code == 200, changed.text
    current = changed.json()['data']
    assert current['items'][0]['completed'] is True
    stale = client.request('DELETE', base + '/' + item['id'], json={
        'expected_parent_version': parent['version'], 'expected_version': item['version']})
    assert stale.status_code == 409
    fresh = client.get(base).json()['data']
    assert fresh['parent_version'] == current['parent_version'] and len(fresh['items']) == 1
