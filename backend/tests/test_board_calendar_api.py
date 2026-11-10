from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain import AppError
from app.persistence import Command
from test_core import task_body


def create_application(client, position='后端工程师'):
    response = client.post('/api/v1/applications', json={'company': '示例公司', 'position': position})
    assert response.status_code == 201, response.text
    return response.json()['data']


def test_board_routes_link_move_unlink_and_idempotent_replay(client):
    first = create_application(client)
    second = create_application(client, '产品经理')
    task = client.post('/api/v1/tasks', json=task_body()).json()['data']
    before = client.get('/api/v1/tasks/' + task['id']).json()['data']
    key = {'Idempotency-Key': str(uuid4())}
    body = {'expected_version': 1, 'task_id': task['id'], 'expected_task_version': 1}
    route = '/api/v1/applications/' + first['id'] + '/link'
    response = client.post(route, json=body, headers=key)
    assert response.status_code == 200, response.text
    assert client.post(route, json=body, headers=key).json()['data'] == response.json()['data']
    assert client.get('/api/v1/applications/' + first['id']).json()['data']['tasks'][0]['id'] == task['id']
    moved = client.post('/api/v1/applications/' + second['id'] + '/link', json={
        'expected_version': 1, 'task_id': task['id'], 'expected_task_version': 2, 'expected_source_version': 2})
    assert moved.status_code == 200, moved.text
    assert client.get('/api/v1/applications/' + first['id']).json()['data']['version'] == 3
    unlinked = client.post('/api/v1/applications/' + second['id'] + '/unlink', json={
        'expected_version': 2, 'task_id': task['id'], 'expected_task_version': 3})
    assert unlinked.status_code == 200, unlinked.text
    after = client.get('/api/v1/tasks/' + task['id']).json()['data']
    assert after['application_id'] is None
    assert before['reminders'] == after['reminders'] and before['schedule_revision'] == after['schedule_revision']


def test_board_link_route_holds_activity_and_task_lock_before_command_transaction(client, app, monkeypatch):
    application = create_application(client)
    task = client.post('/api/v1/tasks', json=task_body()).json()['data']
    db = app.state.services.db
    original_tx = db.tx
    task_guard = db.task_lock(task['id'])
    entered = []
    @contextmanager
    def checked_transaction():
        entered.append(True)
        assert db.active_operations > 0
        assert task_guard._is_owned()
        with original_tx() as session:
            yield session
    monkeypatch.setattr(db, 'tx', checked_transaction)
    response = client.post('/api/v1/applications/' + application['id'] + '/link', json={
        'expected_version': 1, 'task_id': task['id'], 'expected_task_version': 1},
        headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    assert entered


def test_board_route_failure_rolls_back_command_receipt_and_link_then_same_key_can_retry(client, app, monkeypatch):
    application = create_application(client)
    task = client.post('/api/v1/tasks', json=task_body()).json()['data']
    board = app.state.services.application_board
    original = board.link
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise AppError('SIMULATED_FAILURE', '模拟事务中断', 503)
    monkeypatch.setattr(board, 'link', fail)
    key = str(uuid4())
    route = '/api/v1/applications/' + application['id'] + '/link'
    body = {'expected_version': 1, 'task_id': task['id'], 'expected_task_version': 1}
    response = client.post(route, json=body, headers={'Idempotency-Key': key})
    assert response.status_code == 503, response.text
    assert client.get('/api/v1/tasks/' + task['id']).json()['data']['application_id'] is None
    assert client.get('/api/v1/applications/' + application['id']).json()['data']['version'] == 1
    with app.state.services.db.read() as session:
        assert session.scalar(select(Command).where(Command.request_key == key)) is None
    monkeypatch.setattr(board, 'link', original)
    assert client.post(route, json=body, headers={'Idempotency-Key': key}).status_code == 200


def test_generic_task_patch_cannot_link_move_or_unlink_application(client):
    first, second = create_application(client), create_application(client, '前端工程师')
    task = client.post('/api/v1/tasks', json=task_body()).json()['data']
    response = client.patch('/api/v1/tasks/' + task['id'], json={
        'expected_version': 1, 'changes': {'application_id': first['id']}})
    assert response.status_code == 422
    assert client.post('/api/v1/applications/' + first['id'] + '/link', json={
        'expected_version': 1, 'task_id': task['id'], 'expected_task_version': 1}).status_code == 200
    for value in [second['id'], None]:
        response = client.patch('/api/v1/tasks/' + task['id'], json={
            'expected_version': 2, 'changes': {'application_id': value}})
        assert response.status_code == 422
    assert client.get('/api/v1/tasks/' + task['id']).json()['data']['application_id'] == first['id']


def test_legacy_task_creation_with_application_is_atomic_and_advances_application_history(client):
    application = create_application(client)
    response = client.post('/api/v1/tasks', json={**task_body(), 'application_id': application['id']})
    assert response.status_code == 201, response.text
    task = response.json()['data']
    detail = client.get('/api/v1/applications/' + application['id']).json()['data']
    assert detail['version'] == 2
    assert detail['tasks'][0]['id'] == task['id']
    assert detail['history'][-1]['task_id'] == task['id']


def test_calendar_export_requires_session_and_is_private_by_default(client, app):
    body = {**task_body(), 'notes': '私密准备笔记',
            'event_details': {'location': '会议室二', 'meeting_url': 'https://meet.example.test/join?token=chosen'}}
    task = client.post('/api/v1/tasks', json=body).json()['data']
    assert client.post('/api/v1/tasks/' + task['id'] + '/cancel', json={'expected_version': 1}).status_code == 200
    todo = {**task_body(), 'kind': 'custom', 'temporal': {**task_body()['temporal'],
            'starts_at': None, 'due_at': '2030-01-02T07:00:00Z'}}
    completed = client.post('/api/v1/tasks', json=todo).json()['data']
    client.post('/api/v1/tasks/' + completed['id'] + '/complete', json={'expected_version': 1})
    client.post('/api/v1/tasks', json={'title': '日期未定', 'kind': 'custom', 'temporal': {
        'kind': 'unknown', 'raw_text': '', 'timezone': 'Asia/Shanghai', 'precision': 'unknown', 'needs_confirmation': True}})
    anonymous = TestClient(app, base_url=app.state.origin)
    try:
        assert anonymous.get('/api/v1/calendar/export').status_code == 401
    finally:
        anonymous.close()
    response = client.get('/api/v1/calendar/export')
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/calendar')
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['X-OfferPilot-Omitted-Count'] == '1'
    assert b'STATUS:CANCELLED' in response.content and b'STATUS:COMPLETED' in response.content
    assert '私密准备笔记' not in response.text and 'chosen' not in response.text
    explicit = client.get('/api/v1/calendar/export?include_details=true')
    assert explicit.status_code == 200
    assert '私密准备笔记' in explicit.text and 'chosen' in explicit.text
    assert client.get('/api/v1/calendar/export?include_details=invalid').status_code == 422


def test_calendar_export_uses_complete_task_list_without_api_pagination(client, app):
    services = app.state.services
    for index in range(205):
        services.tasks.create({**task_body(), 'title': f'事件{index}'})
    response = client.get('/api/v1/calendar/export?limit=1')
    assert response.status_code == 200, response.text
    assert response.content.count(b'BEGIN:VEVENT\r\n') == 205
