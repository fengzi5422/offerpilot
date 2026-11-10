"""Upgrade actual portable programs using only fresh, isolated synthetic directories."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from multiprocessing.connection import Client
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.version import VERSION  # noqa: E402


def _fresh_directory(path):
    path = Path(path).resolve()
    runtime = (ROOT / '.runtime').resolve()
    if path == runtime or not path.is_relative_to(runtime) or path.exists():
        raise ValueError('请选择不存在的项目 .runtime 子目录；现有目录不会修改')
    return path


def _sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _database(path):
    with closing(sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)) as connection:
        revision = connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'database integrity failed'
        assert not connection.execute('PRAGMA foreign_key_check').fetchall(), 'foreign keys failed'
        return revision


def _copy_database(source, destination):
    assert not Path(destination).exists(), 'rollback destination must be new'
    with closing(sqlite3.connect(f'{Path(source).resolve().as_uri()}?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)


def _control(info, command):
    with Client(info['pipe'], family='AF_PIPE', authkey=bytes.fromhex(info['key'])) as connection:
        connection.send(command)
        if not connection.poll(15):
            raise TimeoutError('child control did not answer')
        return connection.recv()


@contextmanager
def _program(executable, directory, expected_version):
    """Own one process; never enumerate, activate or stop another instance."""
    process = subprocess.Popen([str(executable), '--data-dir', str(directory), '--headless', '--no-tray'],
        creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    info, client = None, None
    try:
        deadline = time.monotonic() + 70
        control_path = directory / 'runtime' / 'control.json'
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('isolated child exited before readiness')
            if control_path.is_file():
                try:
                    candidate = json.loads(control_path.read_text(encoding='utf-8'))
                    response = httpx.get(candidate['origin'] + '/api/v1/health', timeout=1, trust_env=False)
                    if response.status_code == 200:
                        info = candidate
                        status = _control(info, 'get_status')
                        assert status['version'] == expected_version, 'executable version does not match expected release'
                        break
                except (OSError, ValueError, KeyError, httpx.HTTPError):
                    pass
            time.sleep(.15)
        else:
            raise TimeoutError('isolated child readiness timed out')
        client = httpx.Client(base_url=info['origin'], headers={'Origin': info['origin']}, timeout=20, trust_env=False)
        url = _control(info, 'get_url')
        code = parse_qs(urlsplit(url).fragment)['code'][0]
        response = client.post('/api/v1/session/bootstrap', json={'code': code})
        assert response.status_code == 200, 'isolated child authentication failed'
        client.headers['X-CSRF-Token'] = response.json()['data']['csrf_token']
        yield client
    finally:
        if client:
            client.close()
        if process.poll() is None:
            try:
                if info is None:
                    raise RuntimeError('no child control available')
                _control(info, 'quit')
                process.wait(timeout=40)
            except (OSError, EOFError, TimeoutError, RuntimeError, subprocess.TimeoutExpired):
                # Fallback affects only this Popen child, never any existing user process.
                process.kill()
                process.wait(timeout=10)
        assert process.poll() == 0, 'isolated child did not exit normally'


def _api(client, path, method='GET', body=None, status=200):
    response = client.request(method, '/api/v1' + path, **({'json': body or {}} if method != 'GET' else {}))
    assert response.status_code == status, f'{method} {path}: unexpected status {response.status_code}'
    return response.json()['data']


def _task_snapshot(task):
    keys = ('id', 'version', 'title', 'kind', 'status', 'priority', 'application_id', 'temporal',
            'due_at', 'starts_at', 'ends_at', 'due_date', 'confirmed', 'notes', 'source', 'manual_fields',
            'event_details', 'schedule_revision', 'next_reminder_at', 'created_at', 'updated_at')
    result = {key: task.get(key) for key in keys}
    result['reminders'] = sorted(({key: reminder.get(key) for key in (
        'id', 'version', 'state', 'fire_at', 'error_code')} for reminder in task['reminders']), key=lambda item: item['id'])
    return result


def _application_snapshot(application):
    return {key: application.get(key) for key in ('id', 'version', 'company', 'position', 'stage', 'source',
        'manual_fields', 'closed_at', 'created_at', 'updated_at')}


def _find_application(client, application_id):
    result = _api(client, '/applications?limit=200')
    return next(application for application in result if application['id'] == application_id)


def check(old_executable, executable, data_dir):
    old_executable, executable = Path(old_executable).resolve(), Path(executable).resolve()
    if not old_executable.is_file() or not executable.is_file() or old_executable == executable:
        raise ValueError('请提供两个不同且已存在的程序文件')
    if os.name != 'nt':
        raise RuntimeError('This acceptance check requires Windows executables and named pipes')
    data_dir = _fresh_directory(data_dir)
    rollback_dir = _fresh_directory(data_dir.with_name(data_dir.name + '-rollback022'))
    checks = {}
    data_dir.mkdir(parents=True)
    with _program(old_executable, data_dir, '0.2.2') as client:
        settings = _api(client, '/settings')
        _api(client, '/settings', 'PATCH', {'expected_version': settings['version'], 'changes': {
            'quiet_hours': {'enabled': False, 'start': '22:00', 'end': '08:00'}}})
        application = _api(client, '/applications', 'POST', {
            'company': '升级验收虚构公司', 'position': '虚构工程师', 'stage': 'applied'}, status=201)
        created = _api(client, '/tasks', 'POST', {
            'title': '升级验收虚构面试', 'kind': 'interview', 'priority': 1,
            'application_id': application['id'], 'notes': '仅属于隔离升级验收',
            'event_details': {'location': '虚构会议室', 'meeting_url': 'https://meet.example.test/upgrade'},
            'temporal': {'kind': 'absolute', 'raw_text': '2050-01-02 15:00 北京时间',
                'timezone': 'Asia/Shanghai', 'timezone_source': 'user_confirmed',
                'starts_at': '2050-01-02T07:00:00Z', 'ends_at': '2050-01-02T08:00:00Z',
                'precision': 'minute', 'needs_confirmation': False}}, status=201)
        before_task = _task_snapshot(_api(client, '/tasks/' + created['id']))
        before_application = _application_snapshot(_find_application(client, application['id']))
        assert before_task['reminders'], 'old program did not generate reminders'
        assert len(_api(client, '/tasks')) == 1
    assert _database(data_dir / 'data.sqlite') == '0004_event_details'
    checks['old022_created_tasks_application_and_reminders'] = True

    with _program(executable, data_dir, VERSION) as client:
        upgraded = _api(client, '/tasks/' + created['id'])
        assert _task_snapshot(upgraded) == before_task, 'upgrade changed task identity, content, version or reminders'
        assert _application_snapshot(_find_application(client, application['id'])) == before_application
        assert _api(client, '/diagnostics')['schema_revision'] == '0005_preparations'
        assert _database(data_dir / 'data.sqlite') == '0005_preparations'
        snapshots = list((data_dir / 'backups').glob('pre-upgrade-*.sqlite'))
        assert len(snapshots) == 1 and _database(snapshots[0]) == '0004_event_details'
        recovery_point = snapshots[0]
        for kind in ('ai', 'webhook'):
            config = _api(client, '/integrations/' + kind)
            assert config['enabled'] is False and config['has_secret'] is False
        checks['upgrade_schema_task_application_and_reminder_identity_preserved'] = True
        checks['matching_schema_pre_upgrade_recovery_point_created'] = True
        checks['external_integrations_default_disabled'] = True
        route = '/tasks/' + created['id'] + '/preparations'
        preparations = _api(client, route, 'POST', {'expected_parent_version': upgraded['version'],
            'text': '升级后的独立准备项', 'remind_at': '2050-01-01T10:00:00Z'}, status=201)
        assert len(preparations['items']) == 1 and preparations['items'][0]['reminder_task_id']
        prepared_task = _task_snapshot(_api(client, '/tasks/' + created['id']))
        preparation_task_id = preparations['items'][0]['reminder_task_id']
        preparation_task = _task_snapshot(_api(client, '/tasks/' + preparation_task_id))
    with _program(executable, data_dir, VERSION) as client:
        assert _api(client, route) == preparations
        assert _task_snapshot(_api(client, '/tasks/' + created['id'])) == prepared_task
        assert _task_snapshot(_api(client, '/tasks/' + preparation_task_id)) == preparation_task
        assert _application_snapshot(_find_application(client, application['id'])) == before_application
        assert len(_api(client, '/tasks')) == 2
        checks['new_preparation_and_independent_reminder_survive_reopen'] = True

    # Only the old-schema recovery point is opened by the old binary.
    rollback_dir.mkdir(parents=True)
    _copy_database(recovery_point, rollback_dir / 'data.sqlite')
    assert _database(rollback_dir / 'data.sqlite') == '0004_event_details'
    with _program(old_executable, rollback_dir, '0.2.2') as client:
        assert _task_snapshot(_api(client, '/tasks/' + created['id'])) == before_task
        assert _application_snapshot(_find_application(client, application['id'])) == before_application
        assert len(_api(client, '/tasks')) == 1
        checks['old022_reads_matching_schema_recovery_point_with_original_identity'] = True
    assert _database(rollback_dir / 'data.sqlite') == '0004_event_details'
    report = {'passed': True, 'simulation': True, 'programs_executed': ['0.2.2', VERSION],
        'created_at_utc': datetime.now(timezone.utc).isoformat(), 'checks': checks,
        'old_executable_sha256': _sha256(old_executable), 'executable_sha256': _sha256(executable),
        'pre_upgrade_sha256': _sha256(recovery_point), 'original_reminder_count': len(before_task['reminders']),
        'data_directory': str(data_dir), 'rollback_directory': str(rollback_dir),
        'old_binary_opened_new_schema': False}
    (data_dir / 'upgrade-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='使用新隔离目录验收0.2.2到当前版本实际程序升级与匹配schema回退')
    parser.add_argument('--old-executable', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.old_executable, args.executable, args.data_dir), ensure_ascii=False, indent=2))
