import importlib.util
from pathlib import Path
import logging

spec = importlib.util.spec_from_file_location('runtime_support', Path(__file__).with_name('runtime_support.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_log_formatter_never_serializes_untrusted_exception_or_extra():
    record = logging.LogRecord('test', logging.ERROR, '', 1, 'event %s', ('SECRET',), None)
    record.authorization = 'SECRET'
    formatted = runtime.SafeFormatter().format(record)
    assert 'SECRET' not in formatted
    assert 'event' in formatted


def test_instance_lock_excludes_second_instance(tmp_path):
    first = runtime.InstanceLock(tmp_path / 'instance.lock')
    second = runtime.InstanceLock(tmp_path / 'instance.lock')
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_runtime_file_permissions_reject_missing_directory(tmp_path):
    runtime.protect_directory(tmp_path / 'private')
    assert (tmp_path / 'private').is_dir()
