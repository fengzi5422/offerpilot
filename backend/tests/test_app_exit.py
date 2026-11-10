def test_exit_is_authenticated_and_runs_shutdown_once(client, app):
    calls = []
    app.state.shutdown_callback = lambda: calls.append('stop')
    assert client.post('/api/v1/app/exit', json={}, headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    assert calls == []
    first = client.post('/api/v1/app/exit', json={})
    assert first.status_code == 202
    assert first.json()['data']['status'] == 'stopping'
    assert calls == ['stop']
    assert client.post('/api/v1/app/exit', json={}).status_code == 202
    assert calls == ['stop']


def test_exit_without_desktop_callback_is_explicit(client):
    response = client.post('/api/v1/app/exit', json={})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'DESKTOP_EXIT_UNAVAILABLE'
