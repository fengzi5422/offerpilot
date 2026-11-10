import threading
from datetime import datetime, timezone

from test_core import task_body


def test_restore_maintenance_waits_for_inflight_delivery(tmp_path):
    from app.main import create_app
    started, release, restored = threading.Event(), threading.Event(), threading.Event()
    now = [datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)]
    def notify(payload):
        started.set()
        assert release.wait(3)
        return {"status": "sent"}
    application = create_app(tmp_path, start_workers=False, clock=lambda: now[0], notifier=notify)
    services = application.state.services
    services.tasks.create(task_body())
    artifact = services.data_tools.backup()
    path, _ = services.data_tools.artifact(artifact["artifact_id"])
    now[0] = datetime(2030, 1, 1, 7, 1, tzinfo=timezone.utc)
    restore = services.data_tools.validate_restore(path.read_bytes())
    preview = services.data_tools.restore_preview(restore["restore_id"])
    worker = threading.Thread(target=services.reminders.run_once)
    worker.start()
    assert started.wait(2)
    def restore_data():
        services.data_tools.apply_restore(restore["restore_id"], preview["confirmation_token"])
        restored.set()
    restorer = threading.Thread(target=restore_data)
    restorer.start()
    try:
        assert not restored.wait(.1)
    finally:
        release.set()
        worker.join(3)
        restorer.join(3)
    assert restored.is_set()
