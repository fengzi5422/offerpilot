from datetime import datetime, timezone

from fastapi.testclient import TestClient


def task_body():
    return {"title": "准备面试", "kind": "interview", "priority": 1, "temporal": {
        "kind": "absolute", "raw_text": "2030-01-02 15:00 北京时间", "timezone": "Asia/Shanghai",
        "timezone_source": "user_confirmed", "starts_at": "2030-01-02T07:00:00Z",
        "precision": "minute", "needs_confirmation": False}}


def test_task_persists_version_and_reminders(client, app):
    created = client.post("/api/v1/tasks", json=task_body())
    assert created.status_code == 201, created.text
    task = created.json()["data"]
    assert task["version"] == 1 and task["next_reminder_at"]
    updated = client.patch(f'/api/v1/tasks/{task["id"]}', json={"expected_version": 1,
                           "changes": {"title": "准备技术面试"}})
    assert updated.status_code == 200
    stale = client.patch(f'/api/v1/tasks/{task["id"]}', json={"expected_version": 1,
                         "changes": {"title": "覆盖"}})
    assert stale.status_code == 409
    done = client.post(f'/api/v1/tasks/{task["id"]}/complete', json={"expected_version": 2})
    assert done.json()["data"]["next_reminder_at"] is None
    from app.main import create_app
    again = create_app(app.state.data_dir, origin="http://127.0.0.1:8765", start_workers=False)
    with TestClient(again, base_url="http://127.0.0.1:8765") as c:
        auth = c.post("/api/v1/session/bootstrap", json={"code": again.state.bootstrap_code},
                      headers={"Origin": "http://127.0.0.1:8765"})
        assert auth.status_code == 200
        assert c.get(f'/api/v1/tasks/{task["id"]}').json()["data"]["status"] == "done"


def test_security(client, app):
    with TestClient(app, base_url="http://127.0.0.1:8765") as anonymous:
        assert anonymous.get("/api/v1/tasks").status_code == 401
    assert client.get("/api/v1/tasks", headers={"Host": "evil.test"}).status_code == 403
    assert client.get("/api/v1/tasks", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/v1/tasks", json=task_body(), headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post("/api/v1/session/bootstrap", json={"code": app.state.bootstrap_code}).status_code == 401


def test_date_only_never_invents_time(client):
    body = task_body()
    body["temporal"] = {"kind": "date_only", "raw_text": "2030-01-02", "local_date": "2030-01-02",
                        "precision": "date", "timezone": "Asia/Shanghai", "needs_confirmation": True}
    task = client.post("/api/v1/tasks", json=body).json()["data"]
    assert task["due_date"] == "2030-01-02" and task["due_at"] is None
    assert task["next_reminder_at"] is None and not task["confirmed"]


def test_idempotency_replays_and_rejects_different_payload(client):
    headers = {"Idempotency-Key": "6b4dc6de-cdc7-45d8-89c2-75629541efb1"}
    first = client.post("/api/v1/tasks", json=task_body(), headers=headers)
    second = client.post("/api/v1/tasks", json=task_body(), headers=headers)
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    body = task_body()
    body["title"] = "another"
    assert client.post("/api/v1/tasks", json=body, headers=headers).status_code == 409


def eml(text, subject="示例科技面试通知"):
    return (f"From: HR <hr@example.test>\r\nSubject: {subject}\r\n"
            "Date: Sun, 09 Nov 2026 10:00:00 +0800\r\nMessage-ID: <test@example.test>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n" + text).encode()


def import_mail(client, content):
    response = client.post("/api/v1/emails/import", files={"file": ("example.eml", content, "message/rfc822")})
    assert response.status_code == 202, response.text
    client.app.state.services.jobs.run_once()
    client.app.state.services.mail.parse_pending()
    job = client.get('/api/v1/jobs/' + response.json()["data"]["job_id"]).json()["data"]
    assert job["status"] == "succeeded", job
    return client.get('/api/v1/emails/' + job["result"]["message_id"]).json()["data"]


def test_eml_multi_action_dedupe_and_ambiguity(client):
    content = eml("请于2030年1月1日18:00前（北京时间）回复确认面试出席。\n"
                  "面试安排于2030年1月2日15:00（北京时间）。\n"
                  "请在11月12日前上传材料。")
    mail = import_mail(client, content)
    again = import_mail(client, content)
    assert mail["id"] == again["id"]
    tasks = client.get("/api/v1/tasks").json()["data"]
    assert len(tasks) == 2
    assert {t["kind"] for t in tasks} == {"reply", "interview"}
    assert len([c for c in mail["candidates"] if c["state"] == "open"]) == 1
    assert client.get("/api/v1/reviews").json()["data"]


def test_reschedule_never_creates_task(client):
    mail = import_mail(client, eml("原定面试改期至2030年1月2日15:00（北京时间）。"))
    assert mail["state"] == "review_needed"
    assert client.get("/api/v1/tasks").json()["data"] == []


def test_review_ignore_is_durable(client):
    mail = import_mail(client, eml("请明天上传材料。"))
    decisions = [{"candidate_id": c["candidate_id"], "action": "ignore"} for c in mail["candidates"]]
    result = client.post(f'/api/v1/emails/{mail["id"]}/review',
                         json={"expected_version": mail["version"], "decisions": decisions})
    assert result.status_code == 200, result.text
    mail = import_mail(client, eml("请明天上传材料。"))
    assert mail["ignored_by_user"]
    assert client.get("/api/v1/reviews").json()["data"] == []


def test_backup_roundtrip_and_corruption(client):
    task = client.post("/api/v1/tasks", json=task_body()).json()["data"]
    response = client.post("/api/v1/backups", json={"include_evidence": False})
    assert response.status_code == 202, response.text
    client.app.state.services.jobs.run_once()
    job = client.get('/api/v1/jobs/' + response.json()["data"]["job_id"]).json()["data"]
    archive = client.get('/api/v1/artifacts/' + job["result"]["artifact_id"]).content
    validated = client.post("/api/v1/restores/validate", files={"backup": ("backup.zip", archive)})
    assert validated.status_code == 202, validated.text
    client.app.state.services.jobs.run_once()
    validation_job = client.get('/api/v1/jobs/' + validated.json()["data"]["job_id"]).json()["data"]
    assert validation_job["status"] == "succeeded", validation_job
    restore_id = validation_job["result"]["restore_id"]
    preview = client.get('/api/v1/restores/' + restore_id).json()["data"]
    client.patch('/api/v1/tasks/' + task["id"], json={"expected_version": 1, "changes": {"title": "changed"}})
    assert client.post('/api/v1/restores/' + restore_id + '/apply',
                       json={"confirmation_token": preview["confirmation_token"]}).status_code in (202, 409)
    bad = client.post("/api/v1/restores/validate", files={"backup": ("bad.zip", b"not a zip")})
    assert bad.status_code == 202
    client.app.state.services.jobs.run_once()
    assert client.get('/api/v1/jobs/'+bad.json()['data']['job_id']).json()['data']['error_code']=='BACKUP_INVALID'


def test_unknown_notification_not_repeated(tmp_path):
    from app.main import create_app
    now = [datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)]
    calls = []
    def notify(payload):
        calls.append(payload)
        raise TimeoutError()
    app = create_app(tmp_path, clock=lambda: now[0], notifier=notify, start_workers=False)
    service = app.state.services
    service.tasks.create(task_body())
    now[0] = datetime(2030, 1, 1, 7, 1, tzinfo=timezone.utc)
    service.reminders.run_once()
    service.reminders.run_once()
    assert len(calls) == 1
    assert service.reminders.list_notifications()[0]["state"] == "delivery_unknown"
