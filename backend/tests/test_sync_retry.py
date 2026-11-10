from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.domain import AppError
from app.main import create_app
from app.persistence import Account
from test_mail_adapter import MailAdapter, Vault


class FailingAdapter(MailAdapter):
    error = 'MAIL_NETWORK_ERROR'

    @contextmanager
    def open(self, address, credential):
        if self.error:
            raise AppError(self.error, 'test failure', 503)
        yield self


def test_retry_backoff_persists_across_restart_and_success_resets(tmp_path, monkeypatch):
    import random
    monkeypatch.setattr(random, 'uniform', lambda *_: 0)
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    adapter, vault = FailingAdapter(), Vault()
    def build():
        return create_app(tmp_path, clock=lambda: now[0], start_workers=False,
                          mail_adapter=adapter, credential_store=vault).state.services
    s = build()
    account_id = s.accounts.create({'address': '12345@qq.com', 'authorization_code': 'test'})['id']
    for index, seconds in enumerate([30, 60, 120, 300, 300]):
        if index == 2:
            s.db.engine.dispose()
            s = build()
        job = s.accounts.queue_sync(account_id, force=True)
        s.accounts.run_sync(job, account_id)
        account = s.accounts.list()[0]
        assert datetime.fromisoformat(account['next_sync_at']) - now[0] == timedelta(seconds=seconds)
        now[0] += timedelta(seconds=seconds)
    adapter.error = None
    job = s.accounts.queue_sync(account_id, force=True)
    s.accounts.run_sync(job, account_id)
    adapter.error = 'MAIL_NETWORK_ERROR'
    job = s.accounts.queue_sync(account_id, force=True)
    s.accounts.run_sync(job, account_id)
    assert datetime.fromisoformat(s.accounts.list()[0]['next_sync_at']) - now[0] == timedelta(seconds=30)


def test_auth_failure_stops_automatic_retry_and_explicit_test_can_recover(tmp_path):
    adapter = FailingAdapter()
    adapter.error = 'MAIL_AUTH_FAILED'
    s = create_app(tmp_path, start_workers=False, mail_adapter=adapter, credential_store=Vault()).state.services
    account_id = s.accounts.create({'address': '12345@qq.com', 'authorization_code': 'test'})['id']
    job = s.accounts.queue_sync(account_id)
    s.accounts.run_sync(job, account_id)
    account = s.accounts.list()[0]
    assert account['state'] == 'auth_required'
    assert account['next_sync_at'] is None
    adapter.error = None
    job = s.accounts.queue_sync(account_id, test=True, force=True)
    s.accounts.run_sync(job, account_id, test=True)
    assert s.accounts.list()[0]['state'] == 'ready'
    with s.db.read() as session:
        assert session.get(Account, account_id).last_sync_at is None
