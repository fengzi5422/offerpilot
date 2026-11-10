from contextlib import contextmanager

from test_core import eml


class Vault:
    def __init__(self):
        self.values = {}
    def set(self, key, value):
        self.values[key] = value
    def get(self, key):
        return self.values.get(key)
    def delete(self, key):
        self.values.pop(key, None)


class MailAdapter:
    def __init__(self):
        self.uidvalidity, self.uidnext = "123", 3
        self.fetched = []
    @contextmanager
    def open(self, address, credential):
        yield self
    def inspect_folder(self):
        return {"uidvalidity": self.uidvalidity, "uidnext": self.uidnext}
    def list_uids(self, min_uid, since, upper_uid):
        return [u for u in [1, 2] if min_uid <= u <= upper_uid]
    def fetch_message(self, uid):
        self.fetched.append(uid)
        return {"raw": eml(f"请于2030年1月{uid}日18:00前（北京时间）回复面试出席。"),
                "internal_date": "2026-11-09T02:00:00Z"}


def test_readonly_sync_cursor_and_uidvalidity_dedupe(tmp_path):
    from app.main import create_app
    from datetime import datetime, timezone
    adapter, vault = MailAdapter(), Vault()
    application = create_app(tmp_path, mail_adapter=adapter, credential_store=vault, start_workers=False,
                             clock=lambda: datetime(2026, 11, 9, 5, tzinfo=timezone.utc))
    services = application.state.services
    account = services.accounts.create({"provider": "qq", "address": "12345@qq.com",
                                       "authorization_code": "test-secret", "initial_scan": "14"})
    job_id = services.accounts.queue_sync(account["id"])
    services.accounts.run_sync(job_id, account["id"])
    assert services.jobs.get(job_id)["status"] == "succeeded"
    services.mail.parse_pending()
    assert len(services.tasks.list()) == 2
    adapter.uidvalidity = "456"
    job_id = services.accounts.queue_sync(account["id"], force=True)
    services.accounts.run_sync(job_id, account["id"])
    assert len(services.tasks.list()) == 2
    assert "test-secret" not in (tmp_path / "data.sqlite").read_bytes().decode("latin1")


def test_now_boundary_never_fetches_history(tmp_path):
    from app.main import create_app
    adapter = MailAdapter()
    app = create_app(tmp_path, mail_adapter=adapter, credential_store=Vault(), start_workers=False)
    service = app.state.services.accounts
    account = service.create({"provider": "qq", "address": "12345@qq.com", "authorization_code": "secret",
                              "initial_scan": "now"})
    job_id = service.queue_sync(account["id"])
    service.run_sync(job_id, account["id"])
    assert not adapter.fetched


def test_failed_credential_replacement_preserves_old_secret(tmp_path):
    import pytest
    from app.domain import AppError
    from app.main import create_app
    from app.persistence import Account
    adapter, vault = MailAdapter(), Vault()
    application = create_app(tmp_path, mail_adapter=adapter, credential_store=vault, start_workers=False)
    service = application.state.services.accounts
    account = service.create({"provider": "qq", "address": "12345@qq.com", "authorization_code": "original",
                              "initial_scan": "14"})
    @contextmanager
    def reject(address, credential):
        raise AppError("MAIL_AUTH_FAILED", "Invalid credential", 503)
        yield
    adapter.open = reject
    with pytest.raises(AppError):
        service.credential(account["id"], "replacement")
    with service.db.read() as session:
        record = session.get(Account, account["id"])
        assert vault.get(record.credential_ref) == "original"
    assert list(vault.values.values()) == ["original"]


def test_mid_batch_network_failure_does_not_advance_cursor(tmp_path):
    from app.domain import AppError
    from app.main import create_app
    from app.persistence import Account
    adapter = MailAdapter()
    fetch = adapter.fetch_message
    def interrupted(uid):
        if uid == 2:
            raise AppError("MAIL_NETWORK_ERROR", "interrupted", 503)
        return fetch(uid)
    adapter.fetch_message = interrupted
    application = create_app(tmp_path, mail_adapter=adapter, credential_store=Vault(), start_workers=False)
    services = application.state.services
    account = services.accounts.create({"provider": "qq", "address": "12345@qq.com", "authorization_code": "secret",
                                        "initial_scan": "14"})
    job_id = services.accounts.queue_sync(account["id"])
    services.accounts.run_sync(job_id, account["id"])
    assert services.jobs.get(job_id)["status"] == "failed"
    with services.db.read() as session:
        assert session.get(Account, account["id"]).last_uid == 0
    assert services.tasks.list() == []
