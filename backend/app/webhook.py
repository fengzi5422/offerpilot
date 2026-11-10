"""Generic webhook acceptance only; uncertainty never authorizes automatic replay."""
import json
import re
from contextlib import contextmanager, nullcontext

from app.domain import AppError
from app.outbound import OutboundClient, validate_endpoint

GENERIC_MESSAGE = '你有一项求职待办，请打开OfferPilot查看'


class WebhookNotifier:
    def __init__(self, integrations, transport=None):
        self.integrations = integrations
        self.transport = transport or OutboundClient()

    def __call__(self, payload, *, snapshot=None, guard=None):
        return self.send(payload, snapshot=snapshot, guard=guard)

    def send(self, payload, *, snapshot=None, guard=None):
        """Caller owns bounded retries, per-task lock and stable per-channel key.

        config_version binds scheduled reminders to their original destination.
        ``sent`` means HTTP acceptance and never means a phone displayed/read it.
        """
        try:
            if snapshot is None:
                snapshot = self.integrations.snapshot('webhook')
            if snapshot is None:
                return {'status': 'unavailable', 'error_code': 'WEBHOOK_DISABLED'}
            if (not self.integrations.current(snapshot) or
                    payload.get('config_version', snapshot['version']) != snapshot['version']):
                return {'status': 'unavailable', 'error_code': 'WEBHOOK_CONFIG_CHANGED'}
        except Exception:
            return {'status': 'unavailable', 'error_code': 'WEBHOOK_CONFIG_UNAVAILABLE'}
        key = payload.get('idempotency_key')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', key):
            return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_KEY_INVALID'}
        try:
            secret = snapshot['secret']
            if not isinstance(secret, str) or len(secret) > 16384:
                raise ValueError()
            settings = json.loads(secret)
            url, token = settings['url'], settings.get('token', '')
            validate_endpoint(url)
            if not isinstance(token, str) or len(token) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in token):
                raise ValueError()
        except AppError:
            return {'status': 'permanent_failure', 'error_code': 'ENDPOINT_INVALID'}
        except (ValueError, KeyError, TypeError):
            return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_CONFIG_INVALID'}
        headers = {'Idempotency-Key': key}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        # Deliberately ignore task titles, body text, URLs, mailbox addresses and IDs.
        body = {'app': 'OfferPilot', 'message': GENERIC_MESSAGE, 'idempotency_key': key}

        @contextmanager
        def dispatch_guard():
            # Caller already owns task_lock; only request dispatch holds this gate.
            with self.integrations.db.outbound_gate:
                if not self.integrations.current(snapshot):
                    raise AppError('WEBHOOK_CONFIG_CHANGED', '通知配置已变化', 409)
                with guard() if guard else nullcontext():
                    yield

        try:
            status, _ = self.transport.post_json(url, body, headers=headers, allow_local=False,
                timeout=15, max_bytes=16384, guard=dispatch_guard)
        except AppError as error:
            if error.code in ('WEBHOOK_CONFIG_CHANGED', 'DELIVERY_SUPPRESSED', 'REMINDER_CANCELLED'):
                return {'status': 'unavailable', 'error_code': error.code}
            if error.code in ('ENDPOINT_INVALID', 'ENDPOINT_BLOCKED', 'SERVICE_INPUT_TOO_LARGE'):
                return {'status': 'permanent_failure', 'error_code': error.code}
            if error.code == 'SERVICE_REDIRECT_BLOCKED':
                return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_REDIRECT_BLOCKED'}
            # Shared transport cannot prove whether bytes were written for network,
            # timeout or truncated/oversized-response errors. Do not replay them.
            return {'status': 'unknown', 'error_code': 'WEBHOOK_RESULT_UNKNOWN'}
        except Exception:
            return {'status': 'unknown', 'error_code': 'WEBHOOK_RESULT_UNKNOWN'}
        if type(status) is not int:
            return {'status': 'unknown', 'error_code': 'WEBHOOK_RESULT_UNKNOWN'}
        if 200 <= status < 300:
            return {'status': 'sent', 'accepted': True, 'read': False}
        if status == 429:
            return {'status': 'retryable', 'error_code': 'WEBHOOK_RATE_LIMITED'}
        if 500 <= status < 600:
            return {'status': 'retryable', 'error_code': 'WEBHOOK_SERVICE_FAILED'}
        if 300 <= status < 400:
            return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_REDIRECT_BLOCKED'}
        if status in (401, 403):
            return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_AUTH_FAILED'}
        if 400 <= status < 500:
            return {'status': 'permanent_failure', 'error_code': 'WEBHOOK_REQUEST_REJECTED'}
        return {'status': 'unknown', 'error_code': 'WEBHOOK_RESULT_UNKNOWN'}
