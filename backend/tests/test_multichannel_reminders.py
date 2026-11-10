from datetime import datetime, timedelta, timezone
import threading

from sqlalchemy import select

from app.main import create_app
from app.persistence import Attempt, Notification, Reminder
from test_core import task_body
from test_integrations import Vault


def setup(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    sent = []
    s = create_app(tmp_path, clock=lambda: now[0], credential_store=Vault(),
                   notifier=lambda payload: sent.append(payload) or {'status': 'sent'}, start_workers=False).state.services
    webhook = []
    s.reminders.webhook = lambda payload, **kwargs: webhook.append(payload) or {'status': 'sent'}
    return s, now, sent, webhook


def active(s, task_id):
    with s.db.read() as session:
        return [(r.id, r.channel, r.state, r.fire_at) for r in session.scalars(select(Reminder).where(
            Reminder.task_id == task_id, Reminder.state.in_(['pending', 'retry_wait'])))]


def enable(s, url='https://notify.example.test/hook'):
    config = s.integrations.get('webhook')
    return s.integrations.patch('webhook', config['version'], {'url': url, 'enabled': True, 'consent': True})


def test_channels_are_independent_and_reconfigure_preserves_windows(tmp_path):
    s, now, sent, webhook = setup(tmp_path)
    task = s.tasks.create(task_body())
    original = active(s, task['id'])
    config = enable(s)
    planned = active(s, task['id'])
    assert len(planned) == 6 and [r for r in planned if r[1] == 'windows'] == original
    enable(s, 'https://another.example.test/hook')
    assert len(active(s, task['id'])) == 6
    assert [r for r in active(s, task['id']) if r[1] == 'windows'] == original
    now[0] += timedelta(hours=7)
    s.reminders.run_once()
    assert len(sent) == 1 and len(webhook) == 1
    assert webhook[0]['idempotency_key'] and webhook[0]['config_version'] > config['version']


def test_disable_suppresses_only_webhook_and_snooze_keeps_channel(tmp_path):
    s, _, _, _ = setup(tmp_path)
    config = enable(s)
    task = s.tasks.create(task_body())
    with s.db.read() as session:
        reminder = session.scalar(select(Reminder).where(Reminder.task_id == task['id'], Reminder.channel.like('webhook:%')))
        reminder_id, version = reminder.id, reminder.version
    snoozed = s.reminders.snooze(reminder_id, version, 10)
    with s.db.read() as session:
        assert session.get(Reminder, snoozed['id']).channel.startswith('webhook:')
    s.integrations.patch('webhook', config['version'], {'enabled': False})
    assert all(row[1] == 'windows' for row in active(s, task['id']))


def test_windows_failure_does_not_prevent_webhook_and_unknown_not_replayed(tmp_path):
    s, now, _, webhook = setup(tmp_path)
    enable(s)
    task = s.tasks.create(task_body())
    s.reminders.notifier = lambda payload: {'status': 'failed'}
    s.reminders.webhook = lambda payload, **kwargs: webhook.append(payload) or {'status': 'unknown'}
    now[0] += timedelta(hours=7)
    s.reminders.run_once()
    s.reminders.run_once()
    assert len(webhook) == 1
    with s.db.read() as session:
        states = {(r.channel.split(':')[0], r.state) for r in session.scalars(select(Reminder).where(Reminder.task_id == task['id']))}
        assert ('windows', 'failed') in states and ('webhook', 'delivery_unknown') in states


def test_preparation_reminder_fires_at_explicit_time(tmp_path):
    s, now, sent, _ = setup(tmp_path)
    value = task_body()
    value['kind'] = 'custom'
    value['temporal']['due_at'] = value['temporal'].pop('starts_at')
    task = s.tasks.create(value, source={'preparation_id': 'example'})
    rows = active(s, task['id'])
    assert len(rows) == 1 and rows[0][3] == int(datetime(2030, 1, 2, 7, tzinfo=timezone.utc).timestamp() * 1000)
    now[0] = datetime(2030, 1, 2, 7, tzinfo=timezone.utc)
    s.reminders.run_once()
    assert len(sent) == 1


def test_disabling_after_dispatch_keeps_actual_receipt(tmp_path):
    s, now, _, _ = setup(tmp_path)
    config = enable(s)
    s.tasks.create(task_body())
    dispatched, release = threading.Event(), threading.Event()
    def send(payload, guard=None):
        with guard():
            dispatched.set()
        assert release.wait(5)
        return {'status': 'sent'}
    s.reminders.webhook = send
    now[0] += timedelta(hours=7)
    thread = threading.Thread(target=s.reminders.run_once)
    thread.start()
    assert dispatched.wait(5)
    s.integrations.patch('webhook', config['version'], {'enabled': False})
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    with s.db.read() as session:
        attempt = session.scalar(select(Attempt).join(Reminder).where(Reminder.channel.like('webhook:%')))
        assert attempt.finished_at is not None and attempt.result == 'sent'
        assert session.scalar(select(Notification).where(Notification.reminder_id == attempt.reminder_id)).state == 'sent'
