from datetime import datetime, timezone

from sqlalchemy import select

from test_core import task_body


def test_imap_uses_readonly_peek_and_fetches_text_only():
    from app.accounts import QQConnection
    class Protocol:
        def __init__(self):
            self.calls = []
        def select(self, folder, readonly=False):
            self.calls.append(("SELECT", folder, readonly))
            return "OK", [b"1"]
        def response(self, name):
            return name, [b"20" if name == "UIDNEXT" else b"123"]
        def uid(self, command, *args):
            self.calls.append((command, *args))
            if command == "SEARCH":
                return "OK", [b"18 19 20"]
            fields = args[1]
            if "BODYSTRUCTURE" in fields:
                structure = (b'19 (BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 5 1) '
                             b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 999999 NIL ("ATTACHMENT" ("FILENAME" "a.pdf"))) '
                             b'"MIXED") INTERNALDATE "09-Nov-2026 10:00:00 +0800" BODY[HEADER] {30}')
                return "OK", [(structure, b"From: hr@example.test\r\nSubject: Test\r\n\r\n"), b")"]
            if ".MIME" in fields:
                return "OK", [(b"19 (BODY[1.MIME]", b"Content-Type: text/plain; charset=utf-8\r\n\r\n"), b")"]
            return "OK", [(b"19 (BODY[1]<0>", b"hello"), b")"]
    protocol = Protocol()
    adapter = QQConnection(protocol)
    assert adapter.inspect_folder() == {"uidvalidity": "123", "uidnext": 20}
    assert adapter.list_uids(19, None, 19) == [19]
    message = adapter.fetch_message(19)
    assert message["raw"].endswith(b"hello")
    assert message["internal_date"] == "2026-11-09T02:00:00Z"
    assert protocol.calls[0] == ("SELECT", "INBOX", True)
    for call in protocol.calls:
        assert call[0] not in ("STORE", "EXPUNGE")
        if call[0] == "FETCH":
            assert "BODY.PEEK" in call[2]
            assert "[2]" not in call[2]


def test_restart_started_attempt_becomes_unknown_without_repeat(tmp_path):
    from app.main import create_app
    from app.persistence import Attempt, Reminder
    now = [datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)]
    calls = []
    app = create_app(tmp_path, start_workers=False, clock=lambda: now[0], notifier=lambda payload: calls.append(payload))
    services = app.state.services
    task = services.tasks.create(task_body())
    with services.db.tx() as session:
        reminder = session.scalar(select(Reminder).where(Reminder.task_id == task["id"], Reminder.offset_seconds == 86400))
        reminder.state, reminder.attempt_count = "claimed", 1
        session.add(Attempt(reminder_id=reminder.id, attempt_no=1, started_at=reminder.fire_at))
    now[0] = datetime(2030, 1, 1, 7, 1, tzinfo=timezone.utc)
    services.reminders.recover()
    services.reminders.run_once()
    assert not calls
    assert services.reminders.list_notifications()[0]["state"] == "delivery_unknown"


def test_eml_upload_limit_rejected_before_import(client):
    content = b"x" * (10 * 1024 * 1024 + 1)
    assert client.post('/api/v1/emails/import', files={"file": ("large.eml", content)}).status_code == 413
    assert client.get('/api/v1/emails').json()["data"] == []


def test_backup_omits_credential_and_reference(tmp_path):
    import io
    import zipfile
    from app.main import create_app
    from test_mail_adapter import MailAdapter, Vault
    vault = Vault()
    app = create_app(tmp_path, start_workers=False, credential_store=vault, mail_adapter=MailAdapter())
    services = app.state.services
    services.accounts.create({"provider": "qq", "address": "12345@qq.com", "authorization_code": "unique-secret-value",
                              "initial_scan": "14"})
    reference = next(iter(vault.values))
    artifact = services.data_tools.backup()
    path, _ = services.data_tools.artifact(artifact["artifact_id"])
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        payload = archive.read('data.sqlite')
        assert b"unique-secret-value" not in payload and reference.encode() not in payload


def test_large_sleep_recovery_sends_one_summary(tmp_path):
    from app.main import create_app
    now = [datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)]
    calls = []
    app = create_app(tmp_path, start_workers=False, clock=lambda: now[0],
                     notifier=lambda payload: calls.append(payload) or {"status": "sent"})
    for index in range(20):
        body = task_body()
        body["title"] = "恢复测试 " + str(index)
        app.state.services.tasks.create(body)
    now[0] = datetime(2030, 1, 4, 0, 0, tzinfo=timezone.utc)
    app.state.services.reminders.run_once()
    app.state.services.reminders.run_once()
    assert len(calls) == 1
    assert "20" in calls[0]["title"]


def test_relative_mail_lists_date_and_receipt_references():
    from app.mail import parse_temporal
    temporal = parse_temporal("收到后48小时内完成测评", "assessment", header_date="2026-11-09T02:00:00Z",
                              internal_date="2026-11-09T03:00:00Z")
    assert temporal["needs_confirmation"]
    assert {item["reference_kind"] for item in temporal["candidates"]} == {"message_date", "internal_date"}
    assert {item["due_at"] for item in temporal["candidates"]} == {"2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z"}


def test_malformed_eml_returns_validation_error(client):
    response = client.post('/api/v1/emails/import', files={"file": ("bad.eml", b"not an email")})
    assert response.status_code == 422
