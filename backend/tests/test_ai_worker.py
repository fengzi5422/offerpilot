from datetime import datetime, timedelta, timezone
import threading
import socket
from urllib.parse import urlsplit

import pytest

from sqlalchemy import select

from app.domain import AppError
from app.persistence import Job
from test_core import eml
from test_integrations import Vault


def setup(tmp_path, parser=None):
    from app.main import create_app
    now = [datetime(2026, 11, 10, tzinfo=timezone.utc)]
    app = create_app(tmp_path, start_workers=False, credential_store=Vault(), ai_parser=parser, clock=lambda: now[0])
    s = app.state.services
    s.integrations.patch('ai', 1, {'base_url': 'https://model.example/v1', 'model': 'test', 'api_key': 'secret',
                                'enabled': True, 'consent': True})
    return s, now


class Parser:
    def __init__(self):
        self.calls = 0
    def parse(self, mail, profile, config):
        from app.ai import AIParser
        from test_ai import Transport
        self.calls += 1
        return AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt, 'company': '示例科技'}])).parse(mail, profile, config)


def ingest(s, text='请11月16日8:30参加技术交流。'):
    mid = s.mail.ingest(eml(text))
    s.mail.parse_pending()
    return mid


def test_sufficient_rules_and_disabled_never_request(tmp_path):
    parser = Parser()
    s, _ = setup(tmp_path, parser)
    ingest(s, '请于11月16日8:30参加面试。')
    s.ai.run_once()
    assert parser.calls == 0
    active = s.integrations.get('ai')
    s.integrations.patch('ai', active['version'], {'enabled': False})
    ingest(s)
    s.ai.run_once()
    assert parser.calls == 0


def test_ai_improves_open_rule_candidate_in_place_without_duplicate_task(tmp_path):
    class Complete:
        def parse(self, mail, profile, config):
            from app.ai import AIParser
            from test_ai import Transport
            return AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt,
                'time_quote': '11月16日8:30', 'company': '示例科技'}])).parse(mail, profile, config)
    s, _ = setup(tmp_path, Complete())
    mid = ingest(s, '请参加面试，时间为11月16日8:30，11月17日9:30为备用场次。')
    before = s.mail.get(mid)
    candidate = next(c for c in before['candidates'] if c['state'] == 'open')
    assert not candidate['temporal'].get('starts_at')
    s.ai.enqueue(mid, before['version'], force=True)
    s.ai.run_once()
    opened = [c for c in s.mail.get(mid)['candidates'] if c['state'] == 'open']
    assert len(opened) == 1
    assert opened[0]['candidate_id'] == candidate['candidate_id']
    assert opened[0]['temporal']['starts_at'] == '2026-11-16T00:30:00Z'
    assert opened[0]['parser_source'] == 'ai'
    assert s.tasks.list() == []
    s.ai.enqueue(mid, s.mail.get(mid)['version'], force=True)
    s.ai.run_once()
    repeated = [c for c in s.mail.get(mid)['candidates'] if c['state'] == 'open']
    assert len(repeated) == 1 and repeated[0]['candidate_id'] == candidate['candidate_id']


def test_ai_suggestions_are_review_only_and_idempotent(tmp_path):
    parser = Parser()
    s, _ = setup(tmp_path, parser)
    mid = ingest(s)
    s.ai.run_once()
    mail = s.mail.get(mid)
    assert mail['state'] == 'review_needed'
    assert len(mail['candidates']) == 1 and mail['candidates'][0]['parser_source'] == 'ai'
    assert s.tasks.list() == []
    s.ai.enqueue(mid, mail['version'], force=True)
    s.ai.run_once()
    assert len(s.mail.get(mid)['candidates']) == 1


def test_late_result_after_disable_or_source_clear_cannot_write(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Slow(Parser):
        def parse(self, *args):
            started.set()
            assert release.wait(3)
            return super().parse(*args)
    s, _ = setup(tmp_path, Slow())
    mid = ingest(s)
    thread = threading.Thread(target=s.ai.run_once)
    thread.start()
    assert started.wait(3)
    active = s.integrations.get('ai')
    s.integrations.patch('ai', active['version'], {'enabled': False})
    preview = s.data_tools.privacy_preview('mail_cache')
    s.data_tools.purge('mail_cache', preview['preview_token'])
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not s.mail.get(mid)['candidates']


def test_transient_retry_bounded_and_auth_does_not_retry(tmp_path):
    class Failed:
        code = 'AI_RATE_LIMITED'
        calls = 0
        def parse(self, *args):
            self.calls += 1
            raise AppError(self.code, 'error', 503)
    parser = Failed()
    s, now = setup(tmp_path, parser)
    mid = ingest(s)
    s.ai.run_once()
    s.ai.run_once()
    assert parser.calls == 1
    now[0] += timedelta(seconds=61)
    s.ai.run_once()
    s.ai.run_once()
    assert parser.calls == 2
    parser.code = 'AI_AUTH_FAILED'
    s.ai.enqueue(mid, s.mail.get(mid)['version'], force=True)
    s.ai.run_once()
    now[0] += timedelta(minutes=20)
    s.ai.run_once()
    assert parser.calls == 3
    assert s.ai.status()['last_error_code'] == 'AI_AUTH_FAILED'
    with s.db.read() as session:
        assert not list(session.scalars(select(Job).where(Job.kind == 'ai_parse', Job.status == 'queued')))


def test_changed_segmentation_supersedes_old_open_ai_suggestion(tmp_path):
    class Different(Parser):
        def parse(self, mail, profile, config):
            from app.ai import AIParser
            from test_ai import Transport
            self.calls += 1
            quote = mail.text_excerpt.split('\n')[0] if self.calls == 1 else mail.text_excerpt
            return AIParser(Transport([{'kind': 'interview', 'quote': quote, 'company': '示例科技'}])).parse(mail, profile, config)
    s, _ = setup(tmp_path, Different())
    mid = ingest(s, '请11月16日8:30参加技术交流。\n请提前到场。')
    s.ai.run_once()
    s.ai.enqueue(mid, s.mail.get(mid)['version'], force=True)
    s.ai.run_once()
    assert len([c for c in s.mail.get(mid)['candidates'] if c['state'] == 'open']) == 1


def test_auth_failure_blocks_other_queued_mail_until_config_change(tmp_path):
    class Bad(Parser):
        def parse(self, *args):
            self.calls += 1
            raise AppError('AI_AUTH_FAILED', 'authentication failed', 503)
    parser = Bad()
    s, _ = setup(tmp_path, parser)
    mid = ingest(s)
    ingest(s, '请11月17日参加技术交流。')
    s.ai.run_once()
    s.ai.run_once()
    assert parser.calls == 1
    with pytest.raises(AppError) as error:
        s.ai.enqueue(mid, force=True)
    assert error.value.code == 'AI_AUTH_FAILED'


@pytest.mark.parametrize('action', ['disable', 'clear', 'restore', 'stop'])
def test_dns_wait_revocation_prevents_actual_post(tmp_path, monkeypatch, action):
    """Count bytes at a real socket, not merely rejected late database writes."""
    from app.ai import AIParser
    from app.outbound import OutboundClient
    s, _ = setup(tmp_path, AIParser(OutboundClient()))
    ingest(s)
    if action == 'restore':
        path, _ = s.data_tools.artifact(s.data_tools.backup(False)['artifact_id'])
        content = path.read_bytes()
    started, release = threading.Event(), threading.Event()
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    server.settimeout(2)
    port = server.getsockname()[1]
    def dns(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return urlsplit(f'http://127.0.0.1:{port}/v1'), ('127.0.0.1',)
    monkeypatch.setattr('app.outbound.endpoint_addresses', dns)
    thread = threading.Thread(target=s.ai.run_once)
    thread.start()
    assert started.wait(5)
    try:
        if action == 'disable':
            config = s.integrations.get('ai')
            s.integrations.patch('ai', config['version'], {'enabled': False})
        elif action == 'clear':
            preview = s.data_tools.privacy_preview('mail_cache')
            s.data_tools.purge('mail_cache', preview['preview_token'])
        elif action == 'restore':
            result = s.data_tools.validate_restore(content)
            preview = s.data_tools.restore_preview(result['restore_id'])
            s.data_tools.apply_restore(result['restore_id'], preview['confirmation_token'])
        else:
            s.ai.stop()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        # A TCP connection is allowed for cancellation detection, but no HTTP bytes.
        connection, _ = server.accept()
        with connection:
            connection.settimeout(2)
            assert connection.recv(4096) == b''
    finally:
        release.set()
        thread.join(5)
        server.close()


@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_stopped_ai_drops_late_parser_results(tmp_path, outcome):
    entered, release = threading.Event(), threading.Event()
    class Slow(Parser):
        def parse(self, *args):
            entered.set()
            assert release.wait(5)
            if outcome == 'error':
                raise AppError('AI_AUTH_FAILED', 'failed', 503)
            return super().parse(*args)
    s, _ = setup(tmp_path, Slow())
    mid = ingest(s)
    thread = threading.Thread(target=s.ai.run_once)
    thread.start()
    assert entered.wait(5)
    s.ai.stop()
    before = s.mail.get(mid)
    release.set()
    thread.join(5)
    assert not thread.is_alive() and s.mail.get(mid) == before


def test_stop_is_a_barrier_for_final_database_commit(tmp_path, monkeypatch):
    from contextlib import contextmanager
    s, _ = setup(tmp_path, Parser())
    ingest(s)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = s.db.tx
    @contextmanager
    def paused_commit():
        with original() as session:
            yield session
            if threading.current_thread().name == 'ai-commit-test' and any(
                    isinstance(obj, Job) and obj.status == 'succeeded' for obj in session.identity_map.values()):
                entered.set()
                assert release.wait(5)
    monkeypatch.setattr(s.db, 'tx', paused_commit)
    thread = threading.Thread(target=s.ai.run_once, name='ai-commit-test')
    thread.start()
    assert entered.wait(5)
    stopping = threading.Thread(target=lambda: (s.ai.stop(), stopped.set()))
    stopping.start()
    try:
        assert not stopped.wait(.15)
    finally:
        release.set()
        thread.join(5)
        stopping.join(5)
    assert stopped.is_set() and not thread.is_alive()
