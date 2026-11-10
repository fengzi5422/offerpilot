"""Explicit external-service consent and OS-vault credential lifecycle."""
import json

from sqlalchemy import select

from app.domain import AppError, iso, uid
from app.outbound import validate_endpoint
from app.persistence import Setting, check_version

CONSENT_VERSION = '2026-11-10-v1'
DEFAULTS = {
    'ai': {'enabled': False, 'base_url': '', 'model': '', 'credential_ref': None, 'consent_version': None, 'consented_at': None},
    'webhook': {'enabled': False, 'endpoint_origin': '', 'credential_ref': None, 'consent_version': None, 'consented_at': None},
}


def sanitize_integration_settings(connection):
    """Apply even to untrusted uploaded backups; never trust supplied vault references."""
    connection.execute("DELETE FROM settings WHERE id='credential_cleanup'")
    for record_id, encoded in connection.execute("SELECT id,value FROM settings WHERE id LIKE 'integration:%'").fetchall():
        kind = record_id.partition(':')[2]
        value = dict(DEFAULTS[kind]) if kind in DEFAULTS else {'enabled': False, 'credential_ref': None}
        if kind == 'ai':
            old = json.loads(encoded)
            value.update(base_url=old.get('base_url', ''), model=old.get('model', ''))
        connection.execute('UPDATE settings SET value=? WHERE id=?', (json.dumps(value), record_id))


def credential_references(session):
    refs = []
    for s in session.scalars(select(Setting).where(Setting.id.like('integration:%'))):
        refs.extend(s.value.get('retired_refs', []))
        if s.value.get('credential_ref'):
            refs.append(s.value['credential_ref'])
    return list(dict.fromkeys(refs))


def retry_pending_credentials(db, vault):
    with db.read() as session:
        record = session.get(Setting, 'credential_cleanup')
        refs = list(record.value.get('references', [])) if record else []
    for ref in refs:
        try:
            vault.delete(ref)
        except Exception:
            continue
        with db.tx() as session:
            record = session.get(Setting, 'credential_cleanup')
            if record:
                remaining = [r for r in record.value.get('references', []) if r != ref]
                if remaining:
                    record.value = {'references': remaining}
                else:
                    session.delete(record)


def serialized_change(method):
    def change(self, *args, **kwargs):
        with self.db.outbound_gate:
            return method(self, *args, **kwargs)
    return change


class IntegrationService:
    def __init__(self, db, vault, clock):
        self.db, self.vault, self.clock = db, vault, clock
        self.on_change = None
        with db.tx() as session:
            for kind, defaults in DEFAULTS.items():
                if session.get(Setting, 'integration:' + kind) is None:
                    session.add(Setting(id='integration:' + kind, value=dict(defaults)))

    def get(self, kind, session=None):
        if kind not in DEFAULTS:
            raise AppError('NOT_FOUND', '服务配置不存在', 404)
        if session is None:
            with self.db.read() as s:
                return self.get(kind, s)
        record = session.get(Setting, 'integration:' + kind)
        value = {**DEFAULTS[kind], **(record.value if record else {})}
        value['enabled'] = bool(value['enabled'] and value['consent_version'] == CONSENT_VERSION)
        return {**{k: v for k, v in value.items() if k not in ('credential_ref', 'retired_refs')}, 'has_secret': bool(value['credential_ref']),
                'version': record.version if record else 1}

    @serialized_change
    def patch(self, kind, expected_version, changes):
        if kind not in DEFAULTS or not isinstance(changes, dict):
            raise AppError('VALIDATION_ERROR', '服务配置无效', 422)
        allowed = {'enabled', 'consent'} | ({'base_url', 'model', 'api_key'} if kind == 'ai' else {'url', 'token'})
        if set(changes) - allowed or any(type(changes[k]) is not bool for k in ('enabled', 'consent') if k in changes):
            raise AppError('VALIDATION_ERROR', '服务配置字段无效', 422)
        new_ref = None
        old_ref = None
        try:
            with self.db.tx() as session:
                record = session.get(Setting, 'integration:' + kind)
                if record is None:
                    record = Setting(id='integration:' + kind, value=dict(DEFAULTS[kind]), version=1)
                    session.add(record)
                check_version(record, expected_version)
                old = {**DEFAULTS[kind], **record.value}
                value = dict(old)
                old_ref = old['credential_ref']
                if kind == 'ai':
                    for key in ('base_url', 'model'):
                        if key in changes:
                            if not isinstance(changes[key], str) or len(changes[key]) > (4096 if key == 'base_url' else 200):
                                raise AppError('VALIDATION_ERROR', 'AI 地址或模型无效', 422)
                            value[key] = changes[key].strip()
                    if value['base_url']:
                        parsed = validate_endpoint(value['base_url'], allow_local=True)
                        if parsed.query:
                            raise AppError('ENDPOINT_INVALID', 'AI 地址不能包含查询参数，请单独填写密钥', 422)
                    if 'api_key' in changes and changes['api_key']:
                        secret = changes['api_key']
                        if not isinstance(secret, str) or not 1 <= len(secret) <= 4096 or any(ord(c) < 32 for c in secret):
                            raise AppError('VALIDATION_ERROR', '密钥无效', 422)
                        new_ref = 'ai:' + uid()
                        self.vault.set(new_ref, secret)
                        session.info.setdefault('rollback_callbacks', []).append(lambda ref=new_ref: self.vault.delete(ref))
                        value['credential_ref'] = new_ref
                    target_changed = value['base_url'] != old['base_url'] or value['model'] != old['model']
                else:
                    target_changed = bool(changes.get('url'))
                    if target_changed or 'token' in changes:
                        previous = json.loads(self.vault.get(old_ref) or '{}') if old_ref else {}
                        url = changes.get('url') or previous.get('url', '')
                        parsed = validate_endpoint(url)
                        token = changes.get('token', previous.get('token', ''))
                        if not isinstance(token, str) or len(token) > 4096 or any(ord(c) < 32 for c in token):
                            raise AppError('VALIDATION_ERROR', '通知密钥无效', 422)
                        value['endpoint_origin'] = parsed.scheme + '://' + parsed.netloc
                        new_ref = 'webhook:' + uid()
                        self.vault.set(new_ref, json.dumps({'url': url, 'token': token}))
                        session.info.setdefault('rollback_callbacks', []).append(lambda ref=new_ref: self.vault.delete(ref))
                        value['credential_ref'] = new_ref
                value['enabled'] = changes.get('enabled', value['enabled'])
                if target_changed or changes.get('consent') is False:
                    value['consent_version'], value['consented_at'] = None, None
                if changes.get('consent') is False:
                    value['enabled'] = False
                if changes.get('consent'):
                    value['consent_version'], value['consented_at'] = CONSENT_VERSION, iso(self.clock())
                if value['enabled'] and (not value['credential_ref'] or value['consent_version'] != CONSENT_VERSION or
                    kind == 'ai' and (not value['base_url'] or not value['model'])):
                    raise AppError('CONSENT_REQUIRED', '请完整配置服务并确认发送范围后启用', 422)
                if new_ref and old_ref:
                    value['retired_refs'] = list(dict.fromkeys(value.get('retired_refs', []) + [old_ref]))
                    session.info.setdefault('commit_callbacks', []).append(lambda: self.cleanup_retired(kind))
                record.value, record.version = value, record.version + 1
                if self.on_change:
                    self.on_change(kind, session)
                result = self.get(kind, session)
        except Exception:
            if new_ref:
                self.vault.delete(new_ref)
            raise
        return result

    @serialized_change
    def delete(self, kind, expected_version):
        if kind not in DEFAULTS:
            raise AppError('NOT_FOUND', '服务配置不存在', 404)
        with self.db.tx() as session:
            record = session.get(Setting, 'integration:' + kind)
            check_version(record, expected_version)
            refs = list(record.value.get('retired_refs', []))
            if record.value.get('credential_ref'):
                refs.append(record.value['credential_ref'])
            record.value, record.version = {**DEFAULTS[kind], 'retired_refs': refs}, record.version + 1
            session.info.setdefault('commit_callbacks', []).append(lambda: self.cleanup_retired(kind))
            if self.on_change:
                self.on_change(kind, session)
            return self.get(kind, session)

    def cleanup_retired(self, kind):
        with self.db.read() as session:
            record = session.get(Setting, 'integration:' + kind)
            refs = list(record.value.get('retired_refs', [])) if record else []
        for ref in refs:
            self.vault.delete(ref)
            with self.db.tx() as session:
                record = session.get(Setting, 'integration:' + kind)
                if record:
                    record.value = {**record.value, 'retired_refs': [r for r in record.value.get('retired_refs', []) if r != ref]}

    def retry_cleanup(self):
        retry_pending_credentials(self.db, self.vault)
        for kind in DEFAULTS:
            try:
                self.cleanup_retired(kind)
            except AppError:
                pass

    def snapshot(self, kind):
        with self.db.read() as session:
            record = session.get(Setting, 'integration:' + kind)
            if not record or not record.value.get('enabled') or record.value.get('consent_version') != CONSENT_VERSION:
                return None
            value = dict(record.value)
            generation, version = self.db.generation, record.version
        secret = self.vault.get(value.get('credential_ref'))
        if not secret:
            raise AppError('SERVICE_AUTH_REQUIRED', '请重新配置服务密钥', 503)
        return {**value, 'secret': secret, 'kind': kind, 'generation': generation, 'version': version}

    def current(self, snapshot, session=None):
        if not snapshot or self.db.closed or self.db.generation != snapshot['generation']:
            return False
        if session is None:
            with self.db.read() as s:
                return self.current(snapshot, s)
        record = session.get(Setting, 'integration:' + snapshot['kind'])
        return bool(record and record.value.get('enabled') and record.version == snapshot['version'] and
                    record.value.get('consent_version') == CONSENT_VERSION and
                    record.value.get('credential_ref') == snapshot['credential_ref'])
