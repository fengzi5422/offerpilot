"""Small Windows runtime boundary. No business data is written to logs."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

EVENTS = {'app.start', 'app.ready', 'app.stop', 'app.failed', 'control.failed', 'tray.failed'}
RUN_ID = uuid.uuid4().hex


class SafeFormatter(logging.Formatter):
    def format(self, record):
        event = record.msg if isinstance(record.msg, str) and record.msg in EVENTS else 'runtime.event'
        fields = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'run_id': RUN_ID,
                  'level': record.levelname, 'event': event, 'module': record.name}
        if hasattr(record, 'error_id'):
            fields['error_id'] = str(record.error_id) if len(str(record.error_id)) == 32 else 'STARTUP_FAILED'
        return json.dumps(fields, ensure_ascii=False)


def configure_logging(data_dir: Path):
    logs = data_dir / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / 'app.log', maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for old in logs.glob('app.log.*'):
        if old.stat().st_mtime < datetime.now().timestamp() - 14 * 86400:
            old.unlink(missing_ok=True)


def protect_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        flags = subprocess.CREATE_NO_WINDOW
        identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], creationflags=flags,
                                           text=True, encoding='utf-8', errors='replace')
        import csv
        sid = next(csv.reader([identity.strip()]))[1]
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    else:
        path.chmod(0o700)


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            self.handle.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def set_autostart(enabled):
    if sys.platform != 'win32':
        raise RuntimeError('AUTOSTART_UNAVAILABLE')
    import winreg
    if getattr(sys, 'frozen', False):
        command = subprocess.list2cmdline([sys.executable, '--autostart'])
    else:
        # pythonw avoids opening a terminal when logging in.
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        command = subprocess.list2cmdline([str(pythonw if pythonw.exists() else sys.executable),
                                           str(Path(__file__).with_name('launcher.py')), '--autostart'])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enabled:
            winreg.SetValueEx(key, 'OfferPilot', 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, 'OfferPilot')
            except FileNotFoundError:
                pass
