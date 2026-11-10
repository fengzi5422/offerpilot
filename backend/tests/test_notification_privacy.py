from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from app.main import create_app
from app.reminders import WindowsNotifier
from test_core import task_body


def test_default_system_notification_hides_task_details(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    sent = []
    s = create_app(tmp_path, clock=lambda: now[0], start_workers=False,
                   notifier=lambda payload: sent.append(payload) or {'status': 'sent'}).state.services
    body = task_body()
    body['title'] = 'PRIVATE_COMPANY_JOB_DETAILS'
    s.tasks.create(body)
    now[0] = datetime(2030, 1, 1, 7, 1, tzinfo=timezone.utc)
    s.reminders.run_once()
    assert len(sent) == 1
    assert 'PRIVATE_COMPANY_JOB_DETAILS' not in str(sent[0])
    assert s.reminders.list_notifications()[0]['title'] == body['title']


def test_windows_notification_uses_installed_shortcut_identity(monkeypatch):
    captured = []

    class Toaster:
        def __init__(self, name, notifierAUMID):
            captured.append(notifierAUMID)

        def show_toast(self, toast):
            pass

    monkeypatch.setitem(sys.modules, 'windows_toasts', SimpleNamespace(Toast=SimpleNamespace, WindowsToaster=Toaster))
    monkeypatch.setattr(sys, 'platform', 'win32')
    result = WindowsNotifier()({'title': 'test'})
    installer = (Path(__file__).parents[2] / 'packaging' / 'OfferPilot.iss').read_text(encoding='utf-8')
    app_ids = set(re.findall(r'AppUserModelID:\s*"([^"]+)"', installer))
    assert result['status'] == 'sent'
    assert captured == [next(iter(app_ids))]
