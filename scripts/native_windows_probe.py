"""Explicitly invoked native probe; it never reads existing user credentials."""
import argparse
from pathlib import Path
import secrets
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.accounts import WindowsCredentialStore
from app.reminders import WindowsNotifier


def main(notifications=False):
    if sys.platform != 'win32':
        raise RuntimeError('Windows is required')
    vault = WindowsCredentialStore()
    key = 'probe:' + str(uuid.uuid4())
    sample = secrets.token_urlsafe(32)
    try:
        vault.set(key, sample)
        assert vault.get(key) == sample
    finally:
        vault.delete(key)
    assert vault.get(key) is None
    print('PASS: isolated Windows Credential Manager set/get/delete')
    if notifications:
        result = WindowsNotifier()({'title': 'OfferPilot P0 验收测试', 'body': '这是一条本机测试通知，请核对通知中心中的显示。'})
        print('Windows notification channel:', result['status'])
        if result['status'] != 'sent':
            raise RuntimeError('Windows notification channel did not confirm submission')
        print('Submission confirmed; visual delivery still requires human verification.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--notifications', action='store_true')
    options = parser.parse_args()
    main(options.notifications)
