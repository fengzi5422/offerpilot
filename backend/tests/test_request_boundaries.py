import json

from fastapi.testclient import TestClient

from test_core import task_body


def test_bootstrap_requires_explicit_same_origin_before_consuming_code(app):
    with TestClient(app, base_url=app.state.origin) as browser:
        body = {'code': app.state.bootstrap_code}
        assert browser.post('/api/v1/session/bootstrap', json=body).status_code == 403
        assert browser.post('/api/v1/session/bootstrap', content=json.dumps(body), headers={
            'Origin': app.state.origin, 'Content-Type': 'text/plain'}).status_code == 415
        assert browser.post('/api/v1/session/bootstrap', json=body,
                            headers={'Origin': app.state.origin}).status_code == 200


def test_json_writes_require_json_media_type(client):
    response = client.post('/api/v1/tasks', content=json.dumps(task_body()),
                           headers={'Content-Type': 'text/plain'})
    assert response.status_code == 415
    assert client.get('/api/v1/tasks').json()['data'] == []


def test_oversized_json_rejected_before_route_and_without_declared_length(client):
    body = task_body()
    body['notes'] = 'x' * (1024 * 1024)
    raw = json.dumps(body).encode()
    response = client.post('/api/v1/tasks', content=raw, headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    response = client.post('/api/v1/tasks', content=(raw[i:i + 65536] for i in range(0, len(raw), 65536)),
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert client.get('/api/v1/tasks').json()['data'] == []


def test_oversized_multipart_declaration_rejected_before_parser(client):
    response = client.post('/api/v1/emails/import', content=b'not a multipart',
                           headers={'Content-Type': 'multipart/form-data; boundary=test',
                                    'Content-Length': str(12 * 1024 * 1024)})
    assert response.status_code == 413
