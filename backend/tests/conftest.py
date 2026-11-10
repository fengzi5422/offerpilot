import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path):
    from app.main import create_app
    return create_app(tmp_path, origin="http://127.0.0.1:8765", start_workers=False,
                      notifier=lambda payload: {"status": "sent"})


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/v1/session/bootstrap", json={"code": app.state.bootstrap_code},
                               headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 200
        client.headers.update({"Origin": "http://127.0.0.1:8765",
                               "X-CSRF-Token": response.json()["data"]["csrf_token"]})
        yield client
