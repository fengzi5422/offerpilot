"""Exercise arriving mail through the actual production lifespan workers."""
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.ai import AIParser
from app.main import create_app
from test_ai import Transport
from test_core import eml
from test_mail_adapter import Vault


class ArrivingMailbox:
    def __init__(self):
        self.messages = []
        self.scanned = threading.Event()
        self.lock = threading.Lock()
        self.fetched = []

    @contextmanager
    def open(self, *args):
        with self.lock:
            yield self

    def inspect_folder(self):
        self.scanned.set()
        return {'uidvalidity': 'synthetic', 'uidnext': len(self.messages) + 1}

    def list_uids(self, min_uid, since, upper_uid):
        return [uid for uid in range(1, len(self.messages) + 1) if min_uid <= uid <= upper_uid]

    def fetch_message(self, uid):
        self.fetched.append(uid)
        return {'raw': eml(self.messages[uid - 1]), 'internal_date': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}


def wait_until(predicate, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('Production background workers did not process arriving mail')


def test_lifespan_detects_new_mail_and_fills_tasks_and_ai_without_user_clicks(tmp_path):
    mailbox = ArrivingMailbox()
    now = [datetime.now(timezone.utc)]
    class Model:
        def parse(self, mail, profile, config):
            return AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt,
                'company': '示例科技', 'date': '2026-11-17', 'time': '19:00'}])).parse(mail, profile, config)
    app = create_app(tmp_path, mail_adapter=mailbox, credential_store=Vault(),
                     clock=lambda: now[0], ai_parser=Model(), notifier=lambda _: {'status': 'sent'})
    s = app.state.services
    s.integrations.patch('ai', 1, {'enabled': True, 'consent': True, 'model': 'synthetic',
        'base_url': 'https://model.example/v1', 'api_key': 'synthetic'})
    s.accounts.create({'provider': 'qq', 'address': '12345@qq.com',
        'authorization_code': 'synthetic', 'initial_scan': 'now'})
    with TestClient(app):
        wait_until(lambda: bool(s.accounts.list()[0]['last_sync_at']), 5)
        assert s.tasks.list() == []
        with mailbox.lock:
            mailbox.messages.extend([
                '请于11月16日19:00参加示例科技面试。',
                '请于十一月十七日晚上七点参加示例科技面试。',
            ])
        now[0] += timedelta(minutes=4)
        def processed():
            mails = s.mail.list()
            return len(mails) == 2 and any(m['classification'].get('ai_status') == 'succeeded' for m in mails)
        wait_until(processed)
        tasks = s.tasks.list()
        assert len(tasks) == 1 and tasks[0]['starts_at'] == '2026-11-16T11:00:00Z'
        ai_mail = next(m for m in s.mail.list() if m['classification'].get('ai_status') == 'succeeded')
        candidates = s.mail.get(ai_mail['id'])['candidates']
        opened = [c for c in candidates if c['state'] == 'open']
        assert len(opened) == 1 and opened[0]['temporal']['starts_at'] == '2026-11-17T11:00:00Z'
        assert ai_mail['state'] == 'review_needed'
        assert mailbox.fetched == [1, 2]
        assert all(s.worker_health[name] == 'running' for name in ('mail', 'parse', 'ai'))


def test_scheduled_scan_is_idempotent_and_pause_resume_catches_up(tmp_path):
    mailbox = ArrivingMailbox()
    now = [datetime.now(timezone.utc)]
    s = create_app(tmp_path, mail_adapter=mailbox, credential_store=Vault(),
                   start_workers=False, clock=lambda: now[0]).state.services
    account = s.accounts.create({'provider': 'qq', 'address': '12345@qq.com',
        'authorization_code': 'synthetic', 'initial_scan': 'now'})
    s.accounts.tick()
    mailbox.messages.append('请于11月16日19:00参加示例科技面试。')
    now[0] += timedelta(minutes=4)
    s.accounts.tick()
    s.mail.parse_pending()
    assert len(s.tasks.list()) == 1
    now[0] += timedelta(minutes=4)
    s.accounts.tick()
    assert mailbox.fetched == [1]
    current = s.accounts.list()[0]
    s.accounts.pause(account['id'], current['version'], True)
    mailbox.messages.append('请于11月17日20:00参加示例科技面试。')
    now[0] += timedelta(minutes=4)
    s.accounts.tick()
    assert mailbox.fetched == [1]
    current = s.accounts.list()[0]
    s.accounts.pause(account['id'], current['version'], False)
    s.accounts.tick()
    s.mail.parse_pending()
    assert len(s.tasks.list()) == 2 and mailbox.fetched == [1, 2]
