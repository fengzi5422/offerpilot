from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.applications import ApplicationService
from app.domain import AppError
from app.persistence import Audit, Database
from app.tasks import SettingsService, TaskService


@pytest.fixture
def board_services(tmp_path):
    db = Database(tmp_path / 'board-test')
    def clock():
        return datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
    applications = ApplicationService(db, clock)
    tasks = TaskService(db, clock, SettingsService(db))
    yield db, applications, tasks, clock
    db.close()


def create_task(tasks, source=None):
    return tasks.create({'title': '参加技术面试', 'kind': 'interview', 'temporal': {
        'kind': 'absolute', 'raw_text': '2030年1月2日15:00', 'timezone': 'Asia/Shanghai',
        'timezone_source': 'user_confirmed', 'starts_at': '2030-01-02T07:00:00Z',
        'precision': 'minute', 'needs_confirmation': False}}, source=source)


def service(services):
    from app.application_board import ApplicationBoardService
    db, _, _, clock = services
    return ApplicationBoardService(db, clock)


def test_same_company_different_positions_are_independent_and_board_has_sources(board_services):
    _, applications, tasks, _ = board_services
    first = applications.create({'company': '示例公司', 'position': '后端工程师'})
    second = applications.create({'company': '示例公司', 'position': '产品经理'})
    task = create_task(tasks, source={'message_id': 'local-mail', 'type': 'mail'})
    service(board_services).link(first['id'], 1, task['id'], 1)
    listed = applications.list()
    assert len(listed) == 2 and first['id'] != second['id']
    detail = applications.get(first['id'])
    assert detail['tasks'][0]['source'] == task['source']
    assert applications.get(second['id'])['tasks'] == []
    assert detail['tasks'][0]['id'] == task['id']


def test_manual_stage_moves_backwards_close_and_reopen_with_explicit_history(board_services):
    _, applications, tasks, clock = board_services
    application = applications.create({'company': '示例公司'})
    task = create_task(tasks)
    service(board_services).link(application['id'], 1, task['id'], 1)
    for version, stage in enumerate(['offer', 'assessment', 'closed', 'interview'], 2):
        application = applications.patch(application['id'], version, {'stage': stage})
        assert application['stage'] == stage
        assert (application['closed_at'] is not None) == (stage == 'closed')
    detail = applications.get(application['id'])
    changes = [entry for entry in detail['history'] if entry['action'] == 'application.stage_changed']
    assert [(h['old_stage'], h['new_stage']) for h in changes] == [
        ('applied', 'offer'), ('offer', 'assessment'), ('assessment', 'closed'), ('closed', 'interview')]
    assert all(h['actor'] == 'user' and h['at'] == clock().isoformat().replace('+00:00', 'Z') for h in changes)
    assert tasks.get(task['id'])['status'] == 'pending'


def test_link_move_unlink_keep_task_and_reminder_identity_and_schedule(board_services):
    _, applications, tasks, _ = board_services
    board = service(board_services)
    first = applications.create({'company': '示例公司', 'position': '后端'})
    second = applications.create({'company': '示例公司', 'position': '前端'})
    task = create_task(tasks)
    before = tasks.get(task['id'])
    assert before['reminders']
    board.link(first['id'], 1, task['id'], 1)
    board.link(second['id'], 1, task['id'], 2, expected_source_version=2)
    assert applications.get(first['id'])['version'] == 3
    assert applications.get(first['id'])['tasks'] == []
    assert applications.get(second['id'])['version'] == 2
    assert tasks.get(task['id'])['application_id'] == second['id']
    result = board.unlink(second['id'], 2, task['id'], 3)
    assert result['version'] == 3 and result['tasks'] == []
    after = tasks.get(task['id'])
    assert after['application_id'] is None
    assert after['id'] == before['id'] and after['schedule_revision'] == before['schedule_revision']
    assert after['reminders'] == before['reminders']
    assert applications.get(first['id'])['history'][-1]['task_id'] == task['id']


@pytest.mark.parametrize('destination_version,task_version,source_version', [(99, 2, 2), (1, 99, 2), (1, 2, 99), (1, 2, None)])
def test_move_checks_both_application_versions_and_task_before_writing(board_services, destination_version, task_version, source_version):
    db, applications, tasks, _ = board_services
    board = service(board_services)
    source = applications.create({'company': '原申请'})
    target = applications.create({'company': '新申请'})
    task = create_task(tasks)
    board.link(source['id'], 1, task['id'], 1)
    with db.read() as session:
        audit_count = len(list(session.scalars(select(Audit))))
    with pytest.raises(AppError) as caught:
        board.link(target['id'], destination_version, task['id'], task_version, source_version)
    assert caught.value.code == 'VERSION_CONFLICT'
    assert applications.get(source['id'])['version'] == 2
    assert applications.get(target['id'])['version'] == 1
    assert tasks.get(task['id'])['application_id'] == source['id']
    with db.read() as session:
        assert len(list(session.scalars(select(Audit)))) == audit_count


def test_link_and_unlink_are_idempotent_for_unchanged_association(board_services):
    db, applications, tasks, _ = board_services
    board = service(board_services)
    application = applications.create({'company': '示例公司'})
    task = create_task(tasks)
    board.link(application['id'], 1, task['id'], 1)
    first = applications.get(application['id'])
    assert board.link(application['id'], 2, task['id'], 2) == first
    board.unlink(application['id'], 2, task['id'], 2)
    second = applications.get(application['id'])
    assert board.unlink(application['id'], 3, task['id'], 3) == second
    with pytest.raises(AppError):
        board.link(application['id'], 1, task['id'], 3)


def test_link_audit_failure_rolls_back_task_and_both_applications(board_services, monkeypatch):
    import app.application_board as module
    db, applications, tasks, _ = board_services
    board = service(board_services)
    source = applications.create({'company': '原申请'})
    target = applications.create({'company': '新申请'})
    task = create_task(tasks)
    board.link(source['id'], 1, task['id'], 1)
    original = module.audit
    calls = []
    def fail_after_first_audit(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('simulated audit failure')
    monkeypatch.setattr(module, 'audit', fail_after_first_audit)
    before = applications.list(), tasks.get(task['id'])
    with pytest.raises(RuntimeError, match='simulated audit failure'):
        board.link(target['id'], 1, task['id'], 2, 2)
    assert (applications.list(), tasks.get(task['id'])) == before


def test_unlink_rejects_another_application_and_stale_task_without_changes(board_services):
    _, applications, tasks, _ = board_services
    board = service(board_services)
    first = applications.create({'company': '申请一'})
    second = applications.create({'company': '申请二'})
    task = create_task(tasks)
    board.link(first['id'], 1, task['id'], 1)
    with pytest.raises(AppError) as caught:
        board.unlink(second['id'], 1, task['id'], 2)
    assert caught.value.code == 'STATE_CONFLICT'
    with pytest.raises(AppError) as caught:
        board.unlink(first['id'], 2, task['id'], 1)
    assert caught.value.code == 'VERSION_CONFLICT'
    assert tasks.get(task['id'])['application_id'] == first['id']


def test_history_keeps_operation_order_when_wall_clock_is_identical(board_services, monkeypatch):
    import app.domain as domain
    import app.persistence as persistence
    from itertools import count
    _, applications, _, clock = board_services
    identifiers = count(10000, -1)
    monkeypatch.setattr(persistence, 'utcnow', clock)
    monkeypatch.setattr(domain, 'uuid4', lambda: f'{next(identifiers):032d}')
    application = applications.create({'company': '示例公司'})
    for version, stage in enumerate(['offer', 'closed', 'assessment', 'interview'], 1):
        applications.patch(application['id'], version, {'stage': stage})
    history = applications.get(application['id'])['history']
    assert [entry['new_stage'] for entry in history if entry['action'] == 'application.stage_changed'] == [
        'offer', 'closed', 'assessment', 'interview']


def test_stage_audit_failure_rolls_back_manual_stage_and_version(board_services, monkeypatch):
    import app.applications as module
    _, applications, _, _ = board_services
    application = applications.create({'company': '示例公司'})
    before = applications.get(application['id'])
    def fail_audit(*args, **kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(module, 'audit', fail_audit)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        applications.patch(application['id'], 1, {'stage': 'closed'})
    assert applications.get(application['id']) == before
