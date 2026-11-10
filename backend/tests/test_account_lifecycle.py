import pytest

from app.domain import AppError
from app.main import create_app
from app.persistence import Account
from test_mail_adapter import MailAdapter, Vault


def setup(tmp_path):
    vault = Vault()
    service = create_app(tmp_path, mail_adapter=MailAdapter(), credential_store=vault,
                         start_workers=False).state.services.accounts
    value = {"provider": "qq", "address": "12345@qq.com", "authorization_code": "sample", "initial_scan": "14"}
    return service, vault, value, service.create(value)


def test_failed_credential_deletion_keeps_reference_for_retry(tmp_path, monkeypatch):
    service, vault, _, account = setup(tmp_path)
    delete = vault.delete
    def fail(key):
        raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "Cannot delete", 503)
    monkeypatch.setattr(vault, "delete", fail)
    with pytest.raises(AppError):
        service.remove(account['id'], account['version'])
    with service.db.read() as session:
        stored = session.get(Account, account['id'])
        assert stored.state != 'removed'
        assert vault.get(stored.credential_ref) == 'sample'
        assert stored.version == account['version']
    monkeypatch.setattr(vault, "delete", delete)
    service.remove(account['id'], account['version'])
    assert not service.list() and not vault.values


def test_removed_mailbox_can_be_reconnected_without_losing_account_identity(tmp_path):
    service, vault, value, account = setup(tmp_path)
    service.remove(account['id'], account['version'])
    reconnected = service.create({**value, 'authorization_code': 'replacement', 'initial_scan': 'now'})
    assert reconnected['id'] == account['id']
    assert reconnected['state'] == 'ready'
    assert reconnected['version'] > account['version']
    assert reconnected['initial_scan'] == 'now'
    assert list(vault.values.values()) == ['replacement']


def test_reconnection_does_not_execute_a_previous_connections_queued_job(tmp_path):
    service, _, value, account = setup(tmp_path)
    job = service.queue_sync(account['id'])
    service.remove(account['id'], account['version'])
    service.create(value)
    service.run_sync(job, account['id'])
    assert service.jobs.get(job)['status'] == 'interrupted'
    assert service.adapter.fetched == []


def test_reconnection_waits_for_an_old_sync_to_leave(tmp_path):
    import threading
    service, _, value, account = setup(tmp_path)
    service.remove(account['id'], account['version'])
    service.locks[account['id']] = lock = threading.Lock()
    with lock, pytest.raises(AppError) as exc:
        service.create(value)
    assert exc.value.code == 'ACCOUNT_BUSY'
