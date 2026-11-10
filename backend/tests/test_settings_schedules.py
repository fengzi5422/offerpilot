from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.domain import AppError, millis
from app.main import create_app
from app.persistence import Reminder
from test_core import task_body


def service(tmp_path, **kwargs):
    return create_app(tmp_path, clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
                      start_workers=False, **kwargs).state.services


def test_quiet_hours_change_replans_existing_tasks_atomically(tmp_path):
    s = service(tmp_path)
    task = s.tasks.create(task_body())
    settings = s.settings.get()
    s.settings.patch(settings['version'], {'quiet_hours': {'enabled': True, 'start': '13:00', 'end': '14:30'}})
    updated = s.tasks.get(task['id'])
    with s.db.read() as session:
        active = list(session.scalars(select(Reminder).where(Reminder.state == 'pending')))
        assert next(r for r in active if r.offset_seconds == 3600).fire_at == millis(
            datetime(2030, 1, 2, 6, 30, tzinfo=timezone.utc))
    assert updated['schedule_revision'] == task['schedule_revision'] + 1
    assert len([r for r in updated['reminders'] if r['state'] == 'suppressed']) == 3


def test_editable_templates_apply_to_existing_and_new_tasks(tmp_path):
    s = service(tmp_path)
    existing = s.tasks.create(task_body())
    settings = s.settings.get()
    offsets = {**settings.get('reminder_offsets_seconds', {}), 'interview': [7200, 1800]}
    s.settings.patch(settings['version'], {'reminder_offsets_seconds': offsets})
    second = s.tasks.create(task_body())
    for task_id in (existing['id'], second['id']):
        with s.db.read() as session:
            active = list(session.scalars(select(Reminder).where(Reminder.task_id == task_id,
                                                                Reminder.state == 'pending')))
            assert {r.offset_seconds for r in active} == {7200, 1800}


@pytest.mark.parametrize('changes', [
    {'reminder_offsets_seconds': {'interview': [True]}},
    {'reminder_offsets_seconds': {'interview': [0]}},
    {'reminder_offsets_seconds': {'interview': [900, 900]}},
    {'reminder_offsets_seconds': {'interview': [604801]}},
    {'quiet_hours': {'enabled': True, 'start': '12:00', 'end': '12:00'}},
    {'sync_interval_seconds': 60},
])
def test_invalid_settings_do_not_change_state(tmp_path, changes):
    s = service(tmp_path)
    previous = s.settings.get()
    with pytest.raises(AppError) as error:
        s.settings.patch(previous['version'], changes)
    assert error.value.status == 422
    assert s.settings.get() == previous


def test_invalid_autostart_request_never_changes_operating_system(client, app):
    calls = []
    app.state.services.autostart_callback = calls.append
    settings = client.get('/api/v1/settings').json()['data']
    response = client.patch('/api/v1/settings', json={'expected_version': settings['version'],
                            'changes': {'autostart_enabled': True, 'timezone': 'Invalid/Timezone'}})
    assert response.status_code == 422
    assert calls == []


def test_task_replan_failure_rolls_back_settings_and_autostart(client, app, monkeypatch):
    s = app.state.services
    calls = []
    s.autostart_callback = calls.append
    task = s.tasks.create(task_body())
    settings = s.settings.get()

    def fail(*_):
        raise AppError('REPLAN_FAILED', 'Test failure', 503)

    monkeypatch.setattr(s.tasks, 'rebuild', fail)
    response = client.patch('/api/v1/settings', json={'expected_version': settings['version'], 'changes': {
        'autostart_enabled': True, 'quiet_hours': {'enabled': True, 'start': '22:00', 'end': '08:00'}}})
    assert response.status_code == 503
    assert s.settings.get() == settings
    assert s.tasks.get(task['id'])['schedule_revision'] == task['schedule_revision']
    assert calls == [] or calls == [True, False]


def test_title_only_edit_preserves_explicitly_snoozed_reminder(tmp_path):
    s = service(tmp_path)
    task = s.tasks.create(task_body())
    # Snoozing before the original template time must survive an unrelated title edit.
    reminder = s.tasks.get(task['id'])['reminders'][0]
    snoozed = s.reminders.snooze(reminder['id'], reminder['version'], 10)
    s.tasks.patch(task['id'], task['version'], {'title': '新标题'})
    active = [r for r in s.tasks.get(task['id'])['reminders'] if r['state'] == 'pending']
    assert any(r['id'] == snoozed['id'] for r in active)
