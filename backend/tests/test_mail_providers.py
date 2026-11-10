from contextlib import contextmanager
from datetime import datetime, timezone
import imaplib
import ssl

import pytest
from sqlalchemy import select

from app.accounts import QQAdapter
from app.domain import AppError
from app.main import create_app
from app.persistence import Account, Candidate, Email
from test_core import eml
from test_mail_adapter import MailAdapter, Vault


class Protocol:
    def __init__(self, host, port, *, ssl_context, timeout):
        self.host, self.port, self.context, self.timeout = host, port, ssl_context, timeout
        self.calls = []

    def login(self, address, credential):
        self.calls.append(('LOGIN', address, credential))
        return 'OK', [b'authenticated']

    def xatom(self, name, *args):
        self.calls.append((name, *args))
        return 'OK', [b'ID completed']

    def select(self, folder, readonly=False):
        self.calls.append(('SELECT', folder, readonly))
        return 'OK', [b'1']

    def response(self, name):
        return name, [b'2' if name == 'UIDNEXT' else b'123']

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == 'SEARCH':
            return 'OK', [b'1 1 99']
        if 'BODYSTRUCTURE' in args[1]:
            return 'OK', [(b'1 (BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 5 1) '
                           b'INTERNALDATE "09-Nov-2026 10:00:00 +0800" BODY[HEADER] {30}',
                           b'From: hr@example.test\r\nSubject: Test\r\n\r\n'), b')']
        content = b'Content-Type: text/plain; charset=utf-8\r\n\r\n' if '.MIME' in args[1] else b'hello'
        return 'OK', [(b'1 (BODY[1]', content), b')']

    def logout(self):
        self.calls.append(('LOGOUT',))


@pytest.mark.parametrize(('address', 'host', 'identify'), [
    ('123@qq.com', 'imap.qq.com', False),
    ('user@foxmail.com', 'imap.qq.com', False),
    ('user@163.com', 'imap.163.com', True),
    ('user@126.com', 'imap.126.com', True),
])
def test_fixed_tls_hosts_identification_readonly_and_peek(monkeypatch, address, host, identify):
    connections = []

    def connect(*args, **kwargs):
        connection = Protocol(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect)
    with QQAdapter().open(address, 'credential-never-in-id') as connection:
        assert connection.inspect_folder() == {'uidvalidity': '123', 'uidnext': 2}
        assert connection.list_uids(1, None, 1) == [1]
        assert connection.fetch_message(1)['raw'].endswith(b'hello')
    protocol = connections[0]
    assert (protocol.host, protocol.port, protocol.timeout) == (host, 993, 30)
    assert protocol.context.check_hostname is True
    assert protocol.context.verify_mode == ssl.CERT_REQUIRED
    ids = [call for call in protocol.calls if call[0] == 'ID']
    assert bool(ids) is identify
    if identify:
        assert '"name" "OfferPilot"' in ids[0][1]
        assert '"version"' in ids[0][1]
        assert 'credential-never-in-id' not in repr(ids)
        assert address not in repr(ids)
        assert [call[0] for call in protocol.calls[:3]] == ['LOGIN', 'ID', 'SELECT']
    assert ('SELECT', 'INBOX', True) in protocol.calls
    assert protocol.calls[-1] == ('LOGOUT',)
    for call in protocol.calls:
        assert call[0] not in ('STORE', 'EXPUNGE', 'APPEND', 'COPY', 'MOVE')
        if call[0] == 'FETCH':
            assert 'BODY.PEEK' in call[2]


@pytest.mark.parametrize(('provider', 'address'), [('qq', 'person@163.com'), ('163', 'person@126.com'),
    ('126', 'person@qq.com'), ('custom', 'person@163.com'), ('163', 'person@imap.163.com'),
    ('163', 'person@163.com.evil.test'), ('qq', 'bad\r\n@QQ.com')])
def test_provider_address_mismatch_rejected_without_storing_secret(tmp_path, provider, address):
    vault = Vault()
    service = create_app(tmp_path, mail_adapter=MailAdapter(), credential_store=vault,
                         start_workers=False).state.services.accounts
    with pytest.raises(AppError) as error:
        service.create({'provider': provider, 'address': address, 'authorization_code': 'secret'})
    assert error.value.code == 'VALIDATION_ERROR'
    assert not vault.values
    assert service.list() == []


@pytest.mark.parametrize(('provider', 'domain'), [('qq', 'foxmail.com'), ('163', '163.com'), ('126', '126.com')])
def test_provider_persisted_and_removed_reconnect_reuses_identity(tmp_path, provider, domain):
    vault, adapter = Vault(), MailAdapter()
    services = create_app(tmp_path, mail_adapter=adapter, credential_store=vault,
                          start_workers=False).state.services
    value = {'provider': provider, 'address': 'User@' + domain, 'authorization_code': 'original'}
    account = services.accounts.create(value)
    assert account['provider'] == provider
    job = services.accounts.queue_sync(account['id'])
    services.accounts.run_sync(job, account['id'])
    services.mail.parse_pending()
    tasks = {task['id'] for task in services.tasks.list()}
    current = next(item for item in services.accounts.list() if item['id'] == account['id'])
    services.accounts.remove(account['id'], current['version'])
    reconnected = services.accounts.create({**value, 'authorization_code': 'replacement'})
    assert reconnected['id'] == account['id']
    assert reconnected['provider'] == provider
    assert list(vault.values.values()) == ['replacement']
    job = services.accounts.queue_sync(account['id'], force=True)
    services.accounts.run_sync(job, account['id'])
    services.mail.parse_pending()
    assert {task['id'] for task in services.tasks.list()} == tasks
    with services.db.read() as session:
        assert len(list(session.scalars(select(Account)))) == 1


def test_multiple_accounts_separate_credentials_state_cursors_and_duplicate_review(tmp_path):
    class Routed(MailAdapter):
        @contextmanager
        def open(self, address, credential):
            self.address = address
            assert credential == address.split('@')[1]
            yield self

        def inspect_folder(self):
            return {'uidvalidity': self.address, 'uidnext': 2}

        def list_uids(self, min_uid, since, upper_uid):
            return [1] if min_uid <= 1 <= upper_uid else []

        def fetch_message(self, uid):
            return {'raw': eml('请于2030年1月1日18:00前（北京时间）回复面试出席。'),
                    'internal_date': '2026-11-09T02:00:00Z'}

    vault = Vault()
    services = create_app(tmp_path, mail_adapter=Routed(), credential_store=vault, start_workers=False,
                          clock=lambda: datetime(2026, 11, 9, 5, tzinfo=timezone.utc)).state.services
    accounts = [services.accounts.create({'provider': provider, 'address': 'user@' + domain,
                'authorization_code': domain}) for provider, domain in [('qq', 'qq.com'), ('163', '163.com')]]
    for account in accounts:
        job = services.accounts.queue_sync(account['id'])
        services.accounts.run_sync(job, account['id'])
        assert services.jobs.get(job)['status'] == 'succeeded'
        services.mail.parse_pending()
    with services.db.read() as session:
        rows = [session.get(Account, account['id']) for account in accounts]
        assert len({row.credential_ref for row in rows}) == 2
        assert [row.last_uid for row in rows] == [1, 1]
        assert {row.uidvalidity for row in rows} == {'user@qq.com', 'user@163.com'}
        assert len(list(session.scalars(select(Email)))) == 2
        candidates = list(session.scalars(select(Candidate)))
        assert {candidate.state for candidate in candidates} == {'accepted', 'open'}
        assert any('DUPLICATE_CANDIDATE' in candidate.candidate['issues'] for candidate in candidates)
    assert len(services.tasks.list()) == 1
    services.accounts.pause(accounts[0]['id'], accounts[0]['version'], True)
    states = {account['id']: account['state'] for account in services.accounts.list()}
    assert states == {accounts[0]['id']: 'paused', accounts[1]['id']: 'ready'}
    services.accounts.remove(accounts[0]['id'], accounts[0]['version'] + 1)
    assert len(vault.values) == 1
    assert services.accounts.list()[0]['id'] == accounts[1]['id']


@pytest.mark.parametrize(('failure', 'expected'), [(imaplib.IMAP4.error('secret'), 'MAIL_AUTH_FAILED'),
    (OSError('private address'), 'MAIL_NETWORK_ERROR'), (ssl.SSLCertVerificationError('invalid'), 'MAIL_NETWORK_ERROR')])
def test_auth_and_network_failures_are_safe_and_logout(monkeypatch, failure, expected):
    protocol = Protocol('imap.163.com', 993, ssl_context=ssl.create_default_context(), timeout=30)

    def reject(*args):
        raise failure

    protocol.login = reject
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda *args, **kwargs: protocol)
    with pytest.raises(AppError) as error, QQAdapter().open('user@163.com', 'secret'):
        pytest.fail('failed connection must not yield')
    assert error.value.code == expected
    assert 'secret' not in str(error.value)
    assert protocol.calls[-1] == ('LOGOUT',)


def test_netease_id_failure_cannot_open_inbox(monkeypatch):
    protocol = Protocol('imap.163.com', 993, ssl_context=ssl.create_default_context(), timeout=30)
    protocol.xatom = lambda *args: ('NO', [b'client id required'])
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda *args, **kwargs: protocol)
    with pytest.raises(AppError) as error, QQAdapter().open('user@163.com', 'secret'):
        pytest.fail('ID rejection must stop before mailbox selection')
    assert error.value.code == 'MAIL_CLIENT_ID_FAILED'
    assert all(call[0] != 'SELECT' for call in protocol.calls)
    assert protocol.calls[-1] == ('LOGOUT',)


def test_unsupported_adapter_address_never_attempts_network(monkeypatch):
    attempts = []
    def connect(*args, **kwargs):
        attempts.append(args)
        return Protocol(*args, **kwargs)

    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect)
    with pytest.raises(AppError) as error, QQAdapter().open('user@evil.test', 'secret'):
        pytest.fail('unsupported address must be rejected')
    assert error.value.code == 'VALIDATION_ERROR'
    assert not attempts


def test_stored_provider_mismatch_rejected_before_adapter_or_vault_access(tmp_path):
    adapter, vault = MailAdapter(), Vault()
    services = create_app(tmp_path, mail_adapter=adapter, credential_store=vault,
                          start_workers=False).state.services
    account = services.accounts.create({'provider': 'qq', 'address': 'user@qq.com',
                                        'authorization_code': 'secret'})
    with services.db.tx() as session:
        session.get(Account, account['id']).provider = '163'
    job = services.accounts.queue_sync(account['id'])
    services.accounts.run_sync(job, account['id'])
    assert services.jobs.get(job)['status'] == 'failed'
    assert services.jobs.get(job)['error_code'] == 'VALIDATION_ERROR'
    assert not adapter.fetched
    with pytest.raises(AppError) as error:
        services.accounts.credential(account['id'], 'replacement')
    assert error.value.code == 'VALIDATION_ERROR'
    assert list(vault.values.values()) == ['secret']

@pytest.mark.parametrize('section', ['HEADER', 'MIME', 'BODY'])
def test_truncated_literal_fetch_rejected_without_advancing_cursor(tmp_path, monkeypatch, section):
    protocol = Protocol('imap.163.com', 993, ssl_context=ssl.create_default_context(), timeout=30)
    original_uid = protocol.uid

    def truncated(command, *args):
        fields = args[1] if command == 'FETCH' else ''
        selected = ('HEADER' in fields if section == 'HEADER' else
                    '.MIME' in fields if section == 'MIME' else
                    'BODY.PEEK[1]' in fields)
        if selected:
            return 'OK', [(b'1 (BODY[1] {100}', b'cut'), b')']
        return original_uid(command, *args)

    protocol.uid = truncated
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda *args, **kwargs: protocol)
    services = create_app(tmp_path, credential_store=Vault(), start_workers=False).state.services
    account = services.accounts.create({'provider': '163', 'address': 'user@163.com',
                                        'authorization_code': 'secret'})
    job = services.accounts.queue_sync(account['id'])
    services.accounts.run_sync(job, account['id'])
    assert services.jobs.get(job)['status'] == 'failed'
    assert services.jobs.get(job)['error_code'] == 'MAIL_FETCH_FAILED'
    with services.db.read() as session:
        assert session.get(Account, account['id']).last_uid == 0
        assert not list(session.scalars(select(Email)))


@pytest.mark.parametrize('provider', ['163', '126'])
def test_netease_uidvalidity_reset_does_not_recreate_tasks(tmp_path, provider):
    adapter = MailAdapter()
    services = create_app(tmp_path, mail_adapter=adapter, credential_store=Vault(), start_workers=False,
                          clock=lambda: datetime(2026, 11, 9, 5, tzinfo=timezone.utc)).state.services
    account = services.accounts.create({'provider': provider, 'address': f'user@{provider}.com',
                                        'authorization_code': 'secret'})
    for validity in ['original', 'changed']:
        adapter.uidvalidity = validity
        job = services.accounts.queue_sync(account['id'], force=True)
        services.accounts.run_sync(job, account['id'])
        assert services.jobs.get(job)['status'] == 'succeeded'
        services.mail.parse_pending()
        assert len(services.tasks.list()) == 2
    with services.db.read() as session:
        assert session.get(Account, account['id']).uidvalidity == 'changed'
        assert len(list(session.scalars(select(Email)))) == 2

@pytest.mark.parametrize('phase', ['inspect', 'fetch'])
def test_removal_during_network_read_cannot_revive_account_or_ingest_mail(tmp_path, phase):
    adapter = MailAdapter()
    services = create_app(tmp_path, mail_adapter=adapter, credential_store=Vault(), start_workers=False,
                          clock=lambda: datetime(2026, 11, 9, 5, tzinfo=timezone.utc)).state.services
    account = services.accounts.create({'provider': '163', 'address': 'user@163.com',
                                        'authorization_code': 'secret'})
    original = adapter.inspect_folder if phase == 'inspect' else adapter.fetch_message
    removed = False

    def remove_while_reading(*args):
        nonlocal removed
        if not removed:
            removed = True
            services.accounts.remove(account['id'], account['version'])
        return original(*args)

    if phase == 'inspect':
        adapter.inspect_folder = remove_while_reading
    else:
        adapter.fetch_message = remove_while_reading
    job = services.accounts.queue_sync(account['id'])
    services.accounts.run_sync(job, account['id'])
    assert services.jobs.get(job)['status'] == 'interrupted'
    with services.db.read() as session:
        assert session.get(Account, account['id']).state == 'removed'
        assert not list(session.scalars(select(Email)))
    assert services.accounts.list() == []

@pytest.mark.parametrize('code', ['code\r\nA1 STORE 1 +FLAGS (\\Deleted)', 'code\x00suffix', 'code\nsuffix'])
def test_credential_control_characters_rejected_before_storage_or_network(tmp_path, monkeypatch, code):
    vault = Vault()
    services = create_app(tmp_path, mail_adapter=MailAdapter(), credential_store=vault,
                          start_workers=False).state.services
    with pytest.raises(AppError) as error:
        services.accounts.create({'provider': 'qq', 'address': 'user@qq.com', 'authorization_code': code})
    assert error.value.code == 'VALIDATION_ERROR'
    assert not vault.values
    account = services.accounts.create({'provider': 'qq', 'address': 'user@qq.com', 'authorization_code': 'safe'})
    with pytest.raises(AppError) as error:
        services.accounts.credential(account['id'], code)
    assert error.value.code == 'VALIDATION_ERROR'
    assert list(vault.values.values()) == ['safe']
    attempts = []
    def connect(*args, **kwargs):
        attempts.append(args)
        return Protocol(*args, **kwargs)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect)
    with pytest.raises(AppError) as error, QQAdapter().open('user@qq.com', code):
        pytest.fail('control characters cannot enter IMAP commands')
    assert error.value.code == 'VALIDATION_ERROR'
    assert not attempts
