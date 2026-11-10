import io
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from test_core import eml, import_mail, task_body


@pytest.mark.parametrize("text,kind,confirm,precision", [
    ("2030年1月2日15:00（北京时间）", "interview", False, "minute"),
    ("2030-01-02 15:00 UTC", "interview", False, "minute"),
    ("2030年1月2日15:00", "interview", True, "minute"),
    ("2030年1月2日前", "materials", True, "date"),
    ("1月2日15:00 北京时间", "interview", True, "unknown"),
    ("收到后48小时", "assessment", True, "unknown"),
    ("48小时内", "assessment", True, "unknown"),
    ("明天15:00", "materials", True, "unknown"),
    ("本周五", "interview", True, "unknown"),
    ("2030年2月30日15:00 北京时间", "interview", True, "unknown"),
    ("2030年1月2日15:00 CST", "interview", True, "minute"),
    ("2030年1月2日15:00或16:00 北京时间", "interview", True, "unknown"),
])
def test_temporal_decision_table(text, kind, confirm, precision):
    from app.mail import parse_temporal
    result = parse_temporal(text, kind)
    assert result["needs_confirmation"] is confirm
    assert result["precision"] == precision


def test_no_action_email_can_be_ignored(client):
    mail = import_mail(client, eml("感谢您的投递，我们已收到您的申请。"))
    response = client.post(f'/api/v1/emails/{mail["id"]}/review', json={"expected_version": mail["version"],
                           "decisions": [{"candidate_id": "all", "action": "ignore"}]})
    assert response.status_code == 200
    assert response.json()["data"]["email"]["ignored_by_user"]


def test_actual_restore_roundtrip_and_session_reset(client, app):
    task = client.post("/api/v1/tasks", json=task_body()).json()["data"]
    job_id = client.post("/api/v1/backups", json={}).json()["data"]["job_id"]
    artifact_id = client.get('/api/v1/jobs/' + job_id).json()["data"]["result"]["artifact_id"]
    raw = client.get('/api/v1/artifacts/' + artifact_id).content
    client.patch('/api/v1/tasks/' + task["id"], json={"expected_version": 1, "changes": {"title": "changed"}})
    job_id = client.post("/api/v1/restores/validate", files={"backup": ("backup.zip", raw)}).json()["data"]["job_id"]
    restore_id = client.get('/api/v1/jobs/' + job_id).json()["data"]["result"]["restore_id"]
    token = client.get('/api/v1/restores/' + restore_id).json()["data"]["confirmation_token"]
    restored = client.post('/api/v1/restores/' + restore_id + '/apply', json={"confirmation_token": token})
    assert restored.status_code == 202, restored.text
    assert restored.json()["data"]["session_reset"]
    assert client.get('/api/v1/tasks').status_code == 401
    code = app.state.sessions.issue_bootstrap()
    auth = client.post('/api/v1/session/bootstrap', json={"code": code}).json()["data"]
    client.headers["X-CSRF-Token"] = auth["csrf_token"]
    assert client.get('/api/v1/tasks/' + task["id"]).json()["data"]["title"] == task["title"]


def test_zip_traversal_rejected_without_touching_tasks(client):
    client.post('/api/v1/tasks', json=task_body())
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../data.sqlite", "bad")
        archive.writestr("manifest.json", "{}")
    result = client.post('/api/v1/restores/validate', files={"backup": ("evil.zip", stream.getvalue())})
    assert result.status_code == 422
    assert len(client.get('/api/v1/tasks').json()["data"]) == 1


def test_html_is_text_and_unsafe_urls_removed(client):
    content = ("From: hr@example.test\r\nSubject: HTML\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               '<script>evil()</script><img src="https://tracker.test/pixel"><p>请明天上传材料</p>'
               '<a href="javascript:evil()">bad</a><a href="https://example.test/path?secret=1">safe</a>').encode()
    mail = import_mail(client, content)
    assert "evil()" not in mail["text_excerpt"]
    assert len(mail["links"]) == 1
    assert mail["links"][0]["domain"] == "example.test"


def test_csv_formula_is_escaped(client):
    body = task_body()
    body["title"] = '=HYPERLINK("https://example.test")'
    client.post('/api/v1/tasks', json=body)
    job_id = client.post('/api/v1/exports', json={"format": "csv"}).json()["data"]["job_id"]
    artifact_id = client.get('/api/v1/jobs/' + job_id).json()["data"]["result"]["artifact_id"]
    data = client.get('/api/v1/artifacts/' + artifact_id).content.decode('utf-8-sig')
    assert "'=HYPERLINK" in data


def test_in_progress_does_not_cancel_reminders(client):
    task = client.post('/api/v1/tasks', json=task_body()).json()["data"]
    response = client.patch('/api/v1/tasks/' + task["id"], json={"expected_version": 1,
                            "changes": {"status": "in_progress"}})
    assert response.status_code == 200
    assert response.json()["data"]["next_reminder_at"]


def test_retry_is_bounded_and_cancellation_suppresses(tmp_path):
    from app.main import create_app
    from app.persistence import Reminder
    now = [datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)]
    calls = []
    def notifier(payload):
        calls.append(payload)
        return {"status": "retryable", "error_code": "DEFINITELY_NOT_SENT"}
    application = create_app(tmp_path, start_workers=False, clock=lambda: now[0], notifier=notifier)
    services = application.state.services
    task = services.tasks.create(task_body())
    for minute in [1, 2, 5, 6]:
        now[0] = datetime(2030, 1, 1, 7, minute, tzinfo=timezone.utc)
        services.reminders.run_once()
    assert len(calls) == 3
    with services.db.read() as session:
        assert session.scalar(select(Reminder).where(Reminder.task_id == task["id"], Reminder.offset_seconds == 86400)).state == "failed"
    services.tasks.transition(task["id"], 1, "cancel")
    now[0] = datetime(2030, 1, 2, 6, 1, tzinfo=timezone.utc)
    services.reminders.run_once()
    assert len(calls) == 3


def test_privacy_clears_body_and_source_evidence(client):
    import_mail(client, eml("请于2030年1月1日18:00前（北京时间）回复面试出席。"))
    token = client.post('/api/v1/privacy/preview', json={"scope": "source_evidence"}).json()["data"]["preview_token"]
    result = client.post('/api/v1/privacy/purge', json={"scope": "source_evidence", "preview_token": token})
    assert result.status_code == 202, result.text
    task = client.get('/api/v1/tasks').json()["data"][0]
    assert task["source"] == {"source_unavailable": True}
    mail_id = client.get('/api/v1/emails').json()["data"][0]["id"]
    assert client.get('/api/v1/emails/' + mail_id).json()["data"]["text_excerpt"] == ""


def test_invalid_dst_time_is_rejected(client):
    body = task_body()
    body["temporal"].update(timezone="America/New_York", starts_at="2030-03-10T02:30:00-05:00")
    assert client.post('/api/v1/tasks', json=body).status_code == 422


@pytest.mark.parametrize("text,subject,task_count,review", [
    ("请于2030年1月2日15:00（北京时间）参加面试。", "示例科技面试", 1, False),
    ("请于2030年1月2日15:00参加面试。", "示例科技面试", 0, True),
    ("请在2030年1月2日前上传材料。", "示例科技材料", 0, True),
    ("收到后48小时内完成测评。", "示例科技测评", 0, True),
    ("请于11月12日前上传材料。", "示例科技材料", 0, True),
    ("原定面试取消。", "示例科技面试取消", 0, True),
    ("转发邮件：请于2030年1月2日15:00（北京时间）参加面试。", "Fwd: 示例科技面试", 0, True),
    ("请于2030年1月2日15:00（北京时间）参加面试。", "面试通知", 0, True),
    ("限时促销，优惠券即将过期，可退订。", "促销", 0, False),
    ("已收到您的申请，无需回复。", "示例科技投递确认", 0, False),
])
def test_ten_synthetic_eml_flows(client, text, subject, task_count, review):
    mail = import_mail(client, eml(text, subject))
    assert len(client.get('/api/v1/tasks').json()["data"]) == task_count
    assert (mail["state"] == "review_needed") is review


def test_manual_application_linking_is_persistent_and_versioned(client):
    created = client.post('/api/v1/applications', json={"company": "示例科技", "position": "后端工程师", "stage": "applied"})
    assert created.status_code == 201
    application = created.json()["data"]
    body = task_body()
    body["application_id"] = application["id"]
    task = client.post('/api/v1/tasks', json=body).json()["data"]
    assert task["application_id"] == application["id"]
    changed = client.patch('/api/v1/applications/' + application["id"],
                           json={"expected_version": 1, "changes": {"position": "平台工程师"}})
    assert changed.status_code == 200 and changed.json()["data"]["version"] == 2
    assert client.patch('/api/v1/applications/' + application["id"],
                        json={"expected_version": 1, "changes": {"stage": "offer"}}).status_code == 409
    assert len(client.get('/api/v1/applications').json()["data"]) == 1
