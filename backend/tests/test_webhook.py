import json
from contextlib import contextmanager, nullcontext

import pytest

from app.domain import AppError
from app.webhook import WebhookNotifier
from test_integrations import service


class Transport:
    def __init__(self, status=202, error=None):
        self.status, self.error, self.calls = status, error, []
        self.before_dispatch = None
        self.after_dispatch = None

    def post_json(self, url, payload, **kwargs):
        if self.before_dispatch:
            self.before_dispatch()
        with kwargs.get('guard', nullcontext)():
            self.calls.append({'url': url, 'payload': payload, **kwargs})
        if self.after_dispatch:
            self.after_dispatch()
        if self.error:
            raise self.error
        return self.status, b'PRIVATE response body with secret-token and <script>'


def configured(tmp_path, *, token='PRIVATE_TOKEN', status=202, error=None):
    integrations, db, vault = service(tmp_path)
    integrations.patch('webhook', 1, {'enabled': True, 'consent': True,
        'url': 'https://notify.example.test/PRIVATE_PATH?key=PRIVATE_QUERY', 'token': token})
    transport = Transport(status, error)
    return WebhookNotifier(integrations, transport), integrations, transport, db


def test_disabled_never_reads_credentials_or_calls_transport(tmp_path):
    integrations, db, vault = service(tmp_path)
    transport = Transport()
    result = WebhookNotifier(integrations, transport)({'idempotency_key': 'stable-id'})
    assert result == {'status': 'unavailable', 'error_code': 'WEBHOOK_DISABLED'}
    assert not transport.calls and not vault.values
    db.close()


@pytest.mark.parametrize('token', ['', 'PRIVATE_TOKEN'])
def test_generic_payload_stable_key_optional_bearer_and_single_request(tmp_path, token):
    notifier, _, transport, db = configured(tmp_path, token=token)
    payload = {'idempotency_key': 'stable-reminder:1', 'task_id': 'PRIVATE_TASK',
        'title': 'PRIVATE_TITLE', 'body': 'PRIVATE_BODY', 'email': 'PRIVATE_EMAIL', 'url': 'PRIVATE_LINK'}
    assert notifier(payload) == {'status': 'sent', 'accepted': True, 'read': False}
    call = transport.calls[0]
    assert len(transport.calls) == 1
    assert call['payload'] == {'app': 'OfferPilot', 'message': '你有一项求职待办，请打开OfferPilot查看',
                               'idempotency_key': 'stable-reminder:1'}
    assert call['headers']['Idempotency-Key'] == 'stable-reminder:1'
    assert call['allow_local'] is False and call['timeout'] == 15 and call['max_bytes'] == 16384
    assert call['headers'].get('Authorization') == ('Bearer ' + token if token else None)
    assert 'PRIVATE' not in json.dumps(call['payload'])
    notifier(payload)
    assert transport.calls[-1]['payload'] == call['payload']
    db.close()


@pytest.mark.parametrize(('status', 'expected', 'code'), [
    (200, 'sent', None), (204, 'sent', None), (299, 'sent', None),
    (400, 'permanent_failure', 'WEBHOOK_REQUEST_REJECTED'),
    (401, 'permanent_failure', 'WEBHOOK_AUTH_FAILED'), (403, 'permanent_failure', 'WEBHOOK_AUTH_FAILED'),
    (404, 'permanent_failure', 'WEBHOOK_REQUEST_REJECTED'),
    (301, 'permanent_failure', 'WEBHOOK_REDIRECT_BLOCKED'),
    (429, 'retryable', 'WEBHOOK_RATE_LIMITED'), (500, 'retryable', 'WEBHOOK_SERVICE_FAILED'),
    (503, 'retryable', 'WEBHOOK_SERVICE_FAILED'),
])
def test_explicit_http_results_do_not_retry_inside_adapter(tmp_path, status, expected, code):
    notifier, _, transport, db = configured(tmp_path, status=status)
    result = notifier({'idempotency_key': 'stable'})
    assert result['status'] == expected
    assert result.get('error_code') == code
    assert len(transport.calls) == 1
    assert 'PRIVATE' not in json.dumps(result)
    assert not result.get('read')
    db.close()


@pytest.mark.parametrize('error', [TimeoutError('PRIVATE_SECRET'), OSError('PRIVATE_SECRET'),
    AppError('SERVICE_TIMEOUT', 'PRIVATE_SECRET', 503), AppError('SERVICE_NETWORK_ERROR', 'PRIVATE_SECRET', 503),
    AppError('SERVICE_RESPONSE_TOO_LARGE', 'PRIVATE_SECRET', 503), RuntimeError('PRIVATE_SECRET')])
def test_uncertain_dispatch_is_unknown_and_never_retried(tmp_path, error):
    notifier, _, transport, db = configured(tmp_path, error=error)
    result = notifier({'idempotency_key': 'stable'})
    assert result == {'status': 'unknown', 'error_code': 'WEBHOOK_RESULT_UNKNOWN'}
    assert len(transport.calls) == 1
    db.close()


@pytest.mark.parametrize('key', [None, '', 'x\r\nAuthorization: secret', 'x' * 257])
def test_missing_or_injected_idempotency_key_blocks_dispatch(tmp_path, key):
    notifier, _, transport, db = configured(tmp_path)
    result = notifier({'idempotency_key': key})
    assert result == {'status': 'permanent_failure', 'error_code': 'WEBHOOK_KEY_INVALID'}
    assert not transport.calls
    db.close()


def test_stale_plan_configuration_blocks_dispatch(tmp_path):
    notifier, integrations, transport, db = configured(tmp_path)
    result = notifier({'idempotency_key': 'stable', 'config_version': 1})
    assert result == {'status': 'unavailable', 'error_code': 'WEBHOOK_CONFIG_CHANGED'}
    snapshot = integrations.snapshot('webhook')
    integrations.patch('webhook', snapshot['version'], {'enabled': False})
    assert notifier.send({'idempotency_key': 'stable'}, snapshot=snapshot)['status'] == 'unavailable'
    assert not transport.calls
    db.close()


def test_disable_between_snapshot_and_dispatch_blocks_write(tmp_path):
    notifier, integrations, transport, db = configured(tmp_path)
    transport.before_dispatch = lambda: integrations.patch('webhook', integrations.get('webhook')['version'], {'enabled': False})
    assert notifier({'idempotency_key': 'stable'}) == {'status': 'unavailable', 'error_code': 'WEBHOOK_CONFIG_CHANGED'}
    assert not transport.calls
    db.close()


@pytest.mark.parametrize('cancel_code', ['DELIVERY_SUPPRESSED', 'REMINDER_CANCELLED'])
def test_guard_cancellation_blocks_dispatch_and_configuration_can_change_while_waiting(tmp_path, cancel_code):
    notifier, integrations, transport, db = configured(tmp_path)
    @contextmanager
    def cancelled():
        raise AppError(cancel_code, 'PRIVATE_DETAILS', 409)
        yield
    assert notifier.send({'idempotency_key': 'stable'}, guard=cancelled)['status'] == 'unavailable'
    assert not transport.calls
    transport.after_dispatch = lambda: integrations.patch('webhook', integrations.get('webhook')['version'], {'enabled': False})
    assert notifier({'idempotency_key': 'stable'})['status'] == 'sent'
    assert len(transport.calls) == 1
    db.close()


@pytest.mark.parametrize('host', ['127.0.0.1', '10.1.1.1', '169.254.169.254', '192.168.2.2', '100.64.0.1', '::1'])
def test_production_transport_refuses_private_destinations_without_socket_write(tmp_path, monkeypatch, host):
    integrations, db, vault = service(tmp_path)
    domain = f'[{host}]' if ':' in host else host
    integrations.patch('webhook', 1, {'enabled': True, 'consent': True, 'url': f'https://{domain}/secret'})
    attempts = []
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: attempts.append(a))
    result = WebhookNotifier(integrations)({'idempotency_key': 'stable'})
    assert result == {'status': 'permanent_failure', 'error_code': 'ENDPOINT_BLOCKED'}
    assert not attempts
    db.close()


def test_redirect_response_is_permanent_and_never_exposes_destination(tmp_path):
    notifier, _, transport, db = configured(tmp_path, error=AppError('SERVICE_REDIRECT_BLOCKED', 'PRIVATE_URL', 503))
    assert notifier({'idempotency_key': 'stable'}) == {'status': 'permanent_failure', 'error_code': 'WEBHOOK_REDIRECT_BLOCKED'}
    assert len(transport.calls) == 1
    db.close()

def test_callable_accepts_reminder_dispatch_guard(tmp_path):
    notifier, _, transport, db = configured(tmp_path)
    entered = []
    @contextmanager
    def guard():
        entered.append(True)
        yield
    assert notifier({'idempotency_key': 'stable'}, guard=guard)['status'] == 'sent'
    assert entered == [True] and len(transport.calls) == 1
    db.close()

@pytest.mark.parametrize(('status', 'error', 'count', 'state'), [
    (202, None, 1, 'sent'), (401, None, 1, 'failed'), (429, None, 3, 'failed'),
    (503, None, 3, 'failed'), (202, AppError('SERVICE_TIMEOUT', 'PRIVATE', 503), 1, 'delivery_unknown')])
def test_real_reminder_adapter_honors_bounded_retries_and_unknown_outcomes(tmp_path, status, error, count, state):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.main import create_app
    from app.persistence import Reminder
    from test_core import task_body
    from test_integrations import Vault
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    transport = Transport(status, error)
    services = create_app(tmp_path, clock=lambda: now[0], credential_store=Vault(), webhook_transport=transport,
                          notifier=lambda _: {'status': 'sent'}, start_workers=False).state.services
    services.integrations.patch('webhook', 1, {'enabled': True, 'consent': True, 'url': 'https://notify.example.test/private'})
    task = services.tasks.create(task_body())
    now[0] += timedelta(hours=7)
    for _ in range(4):
        services.reminders.run_once()
        now[0] += timedelta(minutes=2)
    assert len(transport.calls) == count
    assert len({call['payload']['idempotency_key'] for call in transport.calls}) == 1
    assert all(set(call['payload']) == {'app', 'message', 'idempotency_key'} for call in transport.calls)
    with services.db.read() as session:
        assert session.scalar(select(Reminder).where(Reminder.task_id == task['id'],
            Reminder.channel.like('webhook:%'), Reminder.attempt_count > 0)).state == state
    services.db.close()


def test_configuration_gate_released_before_waiting_for_remote_response(tmp_path):
    import threading
    notifier, integrations, transport, db = configured(tmp_path)
    changed = threading.Event()
    workers = []
    def while_waiting():
        def disable():
            integrations.patch('webhook', integrations.get('webhook')['version'], {'enabled': False})
            changed.set()
        thread = threading.Thread(target=disable, daemon=True)
        workers.append(thread)
        thread.start()
        assert changed.wait(1), 'dispatch gate must not span remote response wait'
    transport.after_dispatch = while_waiting
    try:
        assert notifier({'idempotency_key': 'stable'})['status'] == 'sent'
    finally:
        for thread in workers:
            thread.join(1)
        db.close()
