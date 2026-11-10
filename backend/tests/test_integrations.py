import io
import json
import sqlite3
import zipfile

import pytest

from app.domain import AppError, utcnow
from app.persistence import Database, Setting


class Vault:
    def __init__(self):
        self.values = {}
    def set(self, key, value):
        self.values[key] = value
    def get(self, key):
        return self.values.get(key)
    def delete(self, key):
        self.values.pop(key, None)


def service(tmp_path):
    from app.integrations import IntegrationService
    db, vault = Database(tmp_path), Vault()
    return IntegrationService(db, vault, utcnow), db, vault


def test_default_disabled_and_secret_never_public(tmp_path):
    integrations, db, vault = service(tmp_path)
    assert not integrations.get('ai')['enabled']
    result = integrations.patch('ai', 1, {'base_url': 'https://api.example.test/v1', 'model': 'example',
        'api_key': 'PRIVATE_AI_KEY', 'enabled': True, 'consent': True})
    assert result['enabled'] and result['has_secret']
    assert 'PRIVATE_AI_KEY' not in json.dumps(result)
    with db.read() as session:
        assert 'PRIVATE_AI_KEY' not in json.dumps(session.get(Setting, 'integration:ai').value)
    assert list(vault.values.values()) == ['PRIVATE_AI_KEY']
    snapshot = integrations.snapshot('ai')
    assert snapshot['secret'] == 'PRIVATE_AI_KEY'
    assert integrations.current(snapshot)
    integrations.patch('ai', result['version'], {'enabled': False})
    assert not integrations.current(snapshot)
    db.close()


def test_consent_and_secret_required_for_enabling(tmp_path):
    integrations, db, _ = service(tmp_path)
    for changes in ({'enabled': True}, {'enabled': True, 'base_url': 'https://x.test/v1', 'model': 'a', 'consent': True}):
        with pytest.raises(AppError):
            integrations.patch('ai', 1, changes)
    assert integrations.get('ai')['version'] == 1
    db.close()


def test_webhook_url_is_secret_and_disable_invalidates(tmp_path):
    integrations, db, vault = service(tmp_path)
    target = 'https://notify.example.test/private-token?key=SECRET'
    result = integrations.patch('webhook', 1, {'url': target, 'enabled': True, 'consent': True})
    assert result['endpoint_origin'] == 'https://notify.example.test'
    assert 'private-token' not in json.dumps(result)
    with db.read() as session:
        assert target not in json.dumps(session.get(Setting, 'integration:webhook').value)
    assert any(target in value for value in vault.values.values())
    integrations.delete('webhook', result['version'])
    assert not vault.values and not integrations.get('webhook')['enabled']
    db.close()


def test_stale_update_does_not_leave_orphan_secrets(tmp_path):
    integrations, db, vault = service(tmp_path)
    integrations.patch('ai', 1, {'api_key': 'first'})
    with pytest.raises(AppError):
        integrations.patch('ai', 1, {'api_key': 'second'})
    assert list(vault.values.values()) == ['first']
    db.close()


def test_backup_sanitizes_integrations_and_nested_source(client, app, tmp_path):
    from app.integrations import IntegrationService
    from app.persistence import Task
    from test_core import task_body
    integrations = IntegrationService(app.state.services.db, Vault(), utcnow)
    integrations.patch('ai', 1, {'api_key': 'PRIVATE_AI_KEY', 'base_url': 'https://x.test/v1', 'model': 'a',
                               'enabled': True, 'consent': True})
    task = app.state.services.tasks.create(task_body())
    with app.state.services.db.tx() as session:
        record = session.get(Task, task['id'])
        record.source = {'message_id': 'test', 'changes': [{'evidence': [{'quote': 'NESTED_PRIVATE_EVIDENCE'}]}]}
    tools = app.state.services.data_tools
    path, _ = tools.artifact(tools.backup(False)['artifact_id'])
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        content = archive.read('data.sqlite')
    assert b'NESTED_PRIVATE_EVIDENCE' not in content
    restored = tmp_path / 'backup.sqlite'
    restored.write_bytes(content)
    with sqlite3.connect(restored) as connection:
        value = json.loads(connection.execute("SELECT value FROM settings WHERE id='integration:ai'").fetchone()[0])
        assert value['enabled'] is False and value['credential_ref'] is None


def test_target_change_requires_new_consent_and_privacy_purge_clears_vault(tmp_path):
    from app.main import create_app
    vault = Vault()
    app = create_app(tmp_path, start_workers=False, credential_store=vault)
    from app.integrations import IntegrationService
    integrations = IntegrationService(app.state.services.db, vault, utcnow)
    active = integrations.patch('ai', 1, {'base_url': 'https://first.example/v1', 'model': 'a',
        'api_key': 'SECRET', 'enabled': True, 'consent': True})
    with pytest.raises(AppError):
        integrations.patch('ai', active['version'], {'base_url': 'https://other.example/v1'})
    preview = app.state.services.data_tools.privacy_preview('all')
    app.state.services.data_tools.purge('all', preview['preview_token'])
    assert not vault.values
    assert not integrations.get('ai')['enabled']


def test_denied_consent_cannot_keep_service_active(tmp_path):
    integrations, db, _ = service(tmp_path)
    active = integrations.patch('ai', 1, {'base_url': 'https://x.test/v1', 'model': 'a',
        'api_key': 'secret', 'enabled': True, 'consent': True})
    snapshot = integrations.snapshot('ai')
    disabled = integrations.patch('ai', active['version'], {'consent': False})
    assert not disabled['enabled'] and not integrations.current(snapshot)
    db.close()


def test_outer_rollback_preserves_previous_credential(tmp_path):
    integrations, db, vault = service(tmp_path)
    result = integrations.patch('ai', 1, {'api_key': 'old'})
    with pytest.raises(RuntimeError):
        with db.tx():
            integrations.patch('ai', result['version'], {'api_key': 'new'})
            raise RuntimeError('rollback')
    assert list(vault.values.values()) == ['old']
    assert integrations.get('ai')['version'] == result['version']
    db.close()


def test_delete_rollback_does_not_lose_credential(tmp_path):
    integrations, db, vault = service(tmp_path)
    result = integrations.patch('ai', 1, {'api_key': 'old'})
    integrations.on_change = lambda *args: (_ for _ in ()).throw(RuntimeError('failure'))
    with pytest.raises(RuntimeError):
        integrations.delete('ai', result['version'])
    assert list(vault.values.values()) == ['old']
    assert integrations.get('ai')['has_secret']
    db.close()


def test_api_configs_hide_secrets(client):
    response = client.get('/api/v1/integrations/ai')
    assert response.status_code == 200
    assert response.json()['data']['enabled'] is False


def test_cleanup_failure_keeps_retryable_reference(tmp_path):
    integrations, db, vault = service(tmp_path)
    result = integrations.patch('ai', 1, {'api_key': 'old'})
    real_delete = vault.delete
    vault.delete = lambda key: (_ for _ in ()).throw(OSError('vault unavailable'))
    integrations.delete('ai', result['version'])
    assert not integrations.get('ai')['enabled']
    with db.read() as session:
        assert session.get(Setting, 'integration:ai').value['retired_refs']
    vault.delete = real_delete
    integrations.cleanup_retired('ai')
    assert not vault.values
    db.close()


def test_purge_partial_vault_failure_is_durable_and_retryable(tmp_path):
    from app.main import create_app
    vault = Vault()
    s = create_app(tmp_path, start_workers=False, credential_store=vault).state.services
    s.integrations.patch('ai', 1, {'api_key': 'first'})
    original_delete = vault.delete
    vault.delete = lambda ref: (_ for _ in ()).throw(OSError('vault unavailable'))
    preview = s.data_tools.privacy_preview('all')
    result = s.data_tools.purge('all', preview['preview_token'])
    assert result['credential_cleanup_pending'] is True
    with s.db.read() as session:
        assert session.get(Setting, 'credential_cleanup').value['references']
    vault.delete = original_delete
    s.integrations.retry_cleanup()
    assert not vault.values


def test_old_consent_cannot_dispatch_and_logging_failure_keeps_new_secret(tmp_path, monkeypatch):
    integrations, db, vault = service(tmp_path)
    result = integrations.patch('ai', 1, {'base_url': 'https://x.test/v1', 'model': 'a', 'api_key': 'old',
                                       'enabled': True, 'consent': True})
    snapshot = integrations.snapshot('ai')
    monkeypatch.setattr('app.integrations.CONSENT_VERSION', 'future')
    assert integrations.snapshot('ai') is None and not integrations.current(snapshot)
    monkeypatch.setattr('app.integrations.CONSENT_VERSION', '2026-11-10-v1')
    db.log = lambda *a, **k: (_ for _ in ()).throw(OSError('logging failed'))
    with db.tx() as session:
        integrations.patch('ai', result['version'], {'api_key': 'new'})
        session.info.setdefault('events', []).append({'event': 'task.changed', 'module': 'tasks', 'entity_id': 'id'})
    assert list(vault.values.values()) == ['new']
    db.log = lambda *a, **k: None
    db.close()


def test_mail_temporal_raw_source_is_removed_from_private_export_and_purge(tmp_path):
    from app.main import create_app
    from app.persistence import Candidate, Task
    from sqlalchemy import select
    from test_core import task_body
    s = create_app(tmp_path, start_workers=False, credential_store=Vault()).state.services
    task = s.tasks.create(task_body(), source={'message_id': 'mail'})
    with s.db.tx() as session:
        record = session.get(Task, task['id'])
        record.temporal = {**record.temporal, 'raw_text': 'PRIVATE_SOURCE_SENTINEL'}
        from app.persistence import Email
        mail = Email(canonical_digest='hash', subject='test', from_addr='test', text_excerpt='', text_hash='hash')
        session.add(mail)
        session.flush()
        session.add(Candidate(message_id=mail.id, action_key='source-test', candidate={
            'temporal': {'raw_text': 'PRIVATE_SOURCE_SENTINEL'}, 'evidence': [{'quote': 'PRIVATE_SOURCE_SENTINEL'}]}))
    path, _ = s.data_tools.artifact(s.data_tools.backup(False)['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        assert b'PRIVATE_SOURCE_SENTINEL' not in archive.read('data.sqlite')
    preview = s.data_tools.privacy_preview('source_evidence')
    s.data_tools.purge('source_evidence', preview['preview_token'])
    with s.db.read() as session:
        assert 'PRIVATE_SOURCE_SENTINEL' not in json.dumps(session.scalar(select(Candidate)).candidate)
        assert session.get(Task, task['id']).temporal['raw_text'] == ''
