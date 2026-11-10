"""Synthetic P3 acceptance journey. No real mailbox, model, webhook or OS toast is used."""
import argparse
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading
import tempfile
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


class MemoryVault:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)


class WebhookTransport:
    def __init__(self):
        self.calls = []
        self.outcome = 202

    def post_json(self, url, payload, **options):
        with options.get('guard', nullcontext)():
            self.calls.append({'payload': dict(payload), 'headers': dict(options.get('headers', {}))})
        if self.outcome == 'timeout':
            from app.domain import AppError
            raise AppError('SERVICE_TIMEOUT', '虚构发送后的响应超时', 503)
        return self.outcome, b'{}'


@contextmanager
def local_model(protocol='HTTP/1.0'):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = protocol

        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            payload = json.loads(raw)
            requests.append(payload)
            content = json.loads(payload['messages'][-1]['content'])
            company = '仿真科技' if '仿真科技' in content['subject'] else None
            action = {'kind': 'interview', 'quote': content['body'], 'company': company}
            body = json.dumps({'choices': [{'message': {'content': json.dumps(
                {'actions': [action]}, ensure_ascii=False)}}]}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive(), 'local model did not stop'


def run(data_dir):
    """Create a fresh synthetic directory and return only non-sensitive evidence."""
    from fastapi.testclient import TestClient
    from icalendar import Calendar
    from app.main import create_app

    data_dir = Path(data_dir).resolve()
    if (data_dir / 'data.sqlite').exists() or data_dir.exists() and any(data_dir.iterdir()):
        raise FileExistsError('验收只允许使用全新或空的数据目录')
    now = [datetime(2026, 11, 10, tzinfo=timezone.utc)]
    vault, webhook = MemoryVault(), WebhookTransport()
    windows = {'status': 'unavailable', 'error_code': 'NOTIFICATION_UNAVAILABLE'}
    windows_calls = []
    checks = {}
    fake_secrets = ('P3_SYNTHETIC_AI_KEY', 'P3_SYNTHETIC_HOOK_TOKEN', 'P3_PRIVATE_QUERY')

    def notify(payload):
        windows_calls.append(dict(payload))
        return dict(windows)

    def api(client, path, method='GET', body=None, *, status=200):
        options = {'json': body if body is not None else {}} if method != 'GET' else {}
        if method not in ('GET', 'PATCH'):
            options['headers'] = {'Idempotency-Key': str(uuid4())}
        response = client.request(method, '/api/v1' + path, **options)
        assert response.status_code == status, (f'{method} {path}: expected {status}, received '
            f'{response.status_code} ({response.json().get("error", {}).get("code", "UNKNOWN")})')
        return response.json()['data']

    def authenticate(client, application, *, reopen=False):
        code = application.state.sessions.issue_bootstrap() if reopen else application.state.bootstrap_code
        response = client.post('/api/v1/session/bootstrap', json={'code': code},
                               headers={'Origin': application.state.origin})
        assert response.status_code == 200, 'bootstrap failed'
        client.headers.update({'Origin': application.state.origin,
                               'X-CSRF-Token': response.json()['data']['csrf_token']})

    def task_input(title, at='2026-11-10T02:00:00Z'):
        return {'title': title, 'kind': 'interview', 'notes': '仅本机笔记 P3_PRIVATE_NOTE',
                'temporal': {'kind': 'absolute', 'raw_text': '虚构日期', 'timezone': 'Asia/Shanghai',
                             'timezone_source': 'user_confirmed', 'starts_at': at,
                             'precision': 'minute', 'needs_confirmation': False},
                'event_details': {'location': '虚构会议室',
                                  'meeting_url': 'https://meet.example.test/join?token=P3_MEETING_TOKEN'}}

    def run_job(client, services, job_id):
        for _ in range(20):
            job = api(client, '/jobs/' + job_id)
            if job['status'] == 'succeeded':
                return job['result']
            assert job['status'] not in ('failed', 'interrupted'), f'job failed: {job["kind"]}'
            services.jobs.run_once()
        raise AssertionError('job did not complete')

    def import_email(client, services, text, suffix):
        mail = EmailMessage()
        mail['From'] = 'hr@example.test'
        mail['To'] = 'candidate@example.test'
        mail['Subject'] = '仿真科技招聘面试通知'
        mail['Date'] = 'Tue, 10 Nov 2026 08:00:00 +0800'
        mail['Message-ID'] = f'<p3-{suffix}@example.test>'
        mail.set_content(text)
        response = client.post('/api/v1/emails/import', files={
            'file': ('synthetic.eml', mail.as_bytes(), 'message/rfc822')})
        assert response.status_code == 202, 'synthetic mail import failed'
        result = run_job(client, services, response.json()['data']['job_id'])
        services.mail.parse_pending()
        return api(client, '/emails/' + result['message_id'])

    with local_model() as (model_url, model_requests):
        application = create_app(data_dir, start_workers=False, credential_store=vault,
                                 clock=lambda: now[0], notifier=notify, webhook_transport=webhook)
        services = application.state.services
        with TestClient(application, base_url=application.state.origin) as client:
            assert client.get('/api/v1/tasks').status_code == 401
            authenticate(client, application)
            assert client.post('/api/v1/tasks', json=task_input('不会写入'),
                               headers={'X-CSRF-Token': 'invalid'}).status_code == 403
            checks['authentication_and_csrf'] = True
            settings = api(client, '/settings')
            api(client, '/settings', 'PATCH', {'expected_version': settings['version'], 'changes': {
                'quiet_hours': {'enabled': False, 'start': '22:00', 'end': '08:00'},
                'notification_privacy': 'details'}})
            manual = api(client, '/tasks', 'POST', task_input('手动虚构面试 P3_PRIVATE_TITLE'), status=201)
            rule_mail = import_email(client, services, '请于2026年11月10日11:30（北京时间）参加面试。', 'rule')
            rule_tasks = [task for task in api(client, '/tasks?limit=200')
                          if (task.get('source') or {}).get('message_id') == rule_mail['id']]
            assert len(rule_tasks) == 1 and rule_tasks[0]['confirmed'], 'certain rule mail did not create one task'
            assert not model_requests
            checks['manual_and_certain_mail_tasks'] = True

            ai_config = api(client, '/integrations/ai')
            api(client, '/integrations/ai', 'PATCH', {'expected_version': ai_config['version'], 'changes': {
                'base_url': model_url, 'model': 'synthetic-only', 'api_key': fake_secrets[0],
                'enabled': True, 'consent': True}})
            assert api(client, '/integrations/ai/test', 'POST')['status'] == 'validated'
            before_ai = {task['id'] for task in api(client, '/tasks?limit=200')}
            ai_mail = import_email(client, services,
                '请11月16日8:30参加技术交流。\n联系人：王小明先生 candidate@example.test 13800138000\n'
                '会议资料：https://docs.example.test/item?token=P3_MAIL_TOKEN', 'ai')
            if not api(client, '/integrations/ai')['runtime']['queued']:
                api(client, '/emails/' + ai_mail['id'] + '/reparse', 'POST', {
                    'expected_version': ai_mail['version'], 'mode': 'ai'}, status=202)
            services.ai.run_once()
            ai_mail = api(client, '/emails/' + ai_mail['id'])
            suggestions = [candidate for candidate in ai_mail['candidates']
                           if candidate.get('parser_source') == 'ai' and candidate['state'] == 'open']
            assert len(suggestions) == 1, 'model suggestion missing'
            assert {task['id'] for task in api(client, '/tasks?limit=200')} == before_ai, 'AI wrote tasks without review'
            sent_model_body = json.dumps(model_requests, ensure_ascii=False)
            for private in ('candidate@example.test', '13800138000', '王小明', 'P3_MAIL_TOKEN', fake_secrets[0]):
                assert private not in sent_model_body, 'AI outbound body contains private data'
            accepted = api(client, '/emails/' + ai_mail['id'] + '/review', 'POST', {
                'expected_version': ai_mail['version'], 'decisions': [
                    {'candidate_id': suggestions[0]['candidate_id'], 'action': 'accept'}]})
            assert len(accepted['task_ids']) == 1
            ai_task_id = accepted['task_ids'][0]
            assert api(client, '/tasks/' + ai_task_id)['source']['reviewed']
            checks['local_http_ai_redaction_and_review'] = True

            webhook_config = api(client, '/integrations/webhook')
            api(client, '/integrations/webhook', 'PATCH', {'expected_version': webhook_config['version'], 'changes': {
                'url': 'https://notify.example.test/hook?key=' + fake_secrets[2], 'token': fake_secrets[1],
                'enabled': True, 'consent': True}})
            app_record = api(client, '/applications', 'POST', {'company': '仿真科技', 'position': '后端工程师'}, status=201)
            current = api(client, '/tasks/' + manual['id'])
            reminder_identity = [(item['id'], item['channel'], item['fire_at']) for item in current['reminders']]
            linked = api(client, '/applications/' + app_record['id'] + '/link', 'POST', {
                'expected_version': app_record['version'], 'task_id': current['id'], 'expected_task_version': current['version']})
            for stage in ('interview', 'closed', 'assessment'):
                linked = api(client, '/applications/' + app_record['id'], 'PATCH', {
                    'expected_version': linked['version'], 'changes': {'stage': stage}})
            stage_changes = [item for item in linked['history'] if item['action'] == 'application.stage_changed']
            assert [(item['old_stage'], item['new_stage']) for item in stage_changes] == [
                ('applied', 'interview'), ('interview', 'closed'), ('closed', 'assessment')]
            current = api(client, '/tasks/' + manual['id'])
            api(client, '/applications/' + app_record['id'] + '/unlink', 'POST', {
                'expected_version': linked['version'], 'task_id': current['id'], 'expected_task_version': current['version']})
            current = api(client, '/tasks/' + manual['id'])
            assert current['application_id'] is None and current['status'] == 'pending'
            assert [(item['id'], item['channel'], item['fire_at']) for item in current['reminders']] == reminder_identity
            checks['application_links_stage_history_and_stable_reminders'] = True

            preparation_route = '/tasks/' + manual['id'] + '/preparations'
            checklist = api(client, preparation_route + '/templates', 'POST', {'expected_parent_version': current['version']})
            initial_ids = {item['id'] for item in checklist['items']}
            assert len(initial_ids) >= 3
            checklist = api(client, preparation_route + '/templates', 'POST', {'expected_parent_version': checklist['parent_version']})
            assert {item['id'] for item in checklist['items']} == initial_ids
            item = checklist['items'][0]
            checklist = api(client, preparation_route + '/' + item['id'], 'PATCH', {
                'expected_parent_version': checklist['parent_version'], 'expected_version': item['version'],
                'changes': {'text': '已编辑的虚构准备', 'remind_at': '2026-11-10T01:10:00Z'}})
            item = next(row for row in checklist['items'] if row['id'] == item['id'])
            reminder_task = api(client, '/tasks/' + item['reminder_task_id'])
            assert reminder_task['id'] != manual['id'] and reminder_task['due_at'] == item['remind_at']
            assert reminder_task['source']['preparation_id'] == item['id']
            assert len([row for row in reminder_task['reminders'] if row['state'] == 'pending']) == 2
            checklist = api(client, preparation_route + '/' + item['id'], 'PATCH', {
                'expected_parent_version': checklist['parent_version'], 'expected_version': item['version'],
                'changes': {'completed': True}})
            assert api(client, '/tasks/' + reminder_task['id'])['status'] == 'done'
            item = next(row for row in checklist['items'] if row['id'] == item['id'])
            checklist = api(client, preparation_route + '/' + item['id'], 'DELETE', {
                'expected_parent_version': checklist['parent_version'], 'expected_version': item['version']})
            assert item['id'] not in {row['id'] for row in checklist['items']}
            assert api(client, '/tasks/' + reminder_task['id'])['status'] == 'cancelled'
            checklist = api(client, preparation_route, 'POST', {
                'expected_parent_version': checklist['parent_version'], 'text': '需保留的独立准备',
                'remind_at': '2026-11-10T03:00:00Z'}, status=201)
            retained_item = next(row for row in checklist['items'] if not row['template_key'])
            retained_preparation_id = retained_item['id']
            checks['preparation_template_edit_independent_reminder_complete_delete'] = True

            now[0] = datetime(2026, 11, 10, 1, 0, 1, tzinfo=timezone.utc)
            services.reminders.run_once()
            first_result = api(client, '/tasks/' + manual['id'])['reminders']
            first = [row for row in first_result if row['fire_at'] == '2026-11-10T01:00:00Z']
            assert {row['channel']: row['state'] for row in first} == {'windows': 'failed', 'webhook': 'sent'}
            windows.clear()
            windows['status'] = 'sent'
            webhook.outcome = 'timeout'
            now[0] = datetime(2026, 11, 10, 1, 45, 1, tzinfo=timezone.utc)
            services.reminders.run_once()
            second = [row for row in api(client, '/tasks/' + manual['id'])['reminders']
                      if row['fire_at'] == '2026-11-10T01:45:00Z']
            assert {row['channel']: row['state'] for row in second} == {'windows': 'sent', 'webhook': 'delivery_unknown'}
            calls_before_retry = len(webhook.calls)
            services.reminders.run_once()
            assert len(webhook.calls) == calls_before_retry, 'uncertain webhook was replayed'
            webhook.outcome = 401
            assert api(client, '/integrations/webhook/test', 'POST')['status'] == 'permanent_failure'
            for call in webhook.calls:
                assert set(call['payload']) == {'app', 'message', 'idempotency_key'}
                encoded = json.dumps(call['payload'], ensure_ascii=False)
                for private in ('P3_PRIVATE_TITLE', 'P3_PRIVATE_NOTE', 'P3_MEETING_TOKEN',
                                'candidate@example.test', *fake_secrets):
                    assert private not in encoded, 'webhook payload contains private details'
                assert call['headers']['Idempotency-Key'] == call['payload']['idempotency_key']
            assert any('P3_PRIVATE_TITLE' in call.get('title', '') for call in windows_calls)
            checks['independent_channel_results_generic_payload_and_no_unknown_retry'] = True

            calendar_response = client.get('/api/v1/calendar/export')
            assert calendar_response.status_code == 200
            calendar = Calendar.from_ical(calendar_response.content)
            assert not calendar.errors and calendar.subcomponents
            calendar_ids = {str(component['UID']) for component in calendar.subcomponents}
            assert {manual['id'], rule_tasks[0]['id'], ai_task_id, retained_item['reminder_task_id']} <= calendar_ids
            assert all('URL' not in component and 'DESCRIPTION' not in component for component in calendar.subcomponents)
            components = len(Calendar.from_ical(calendar.to_ical()).subcomponents)
            checks['third_party_calendar_roundtrip_and_privacy'] = True

            backup_job = api(client, '/backups', 'POST', {}, status=202)
            artifact = run_job(client, services, backup_job['job_id'])
            archive_response = client.get('/api/v1/artifacts/' + artifact['artifact_id'])
            assert archive_response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                database_bytes = archive.read('data.sqlite')
            assert manifest['credentials_included'] is False and manifest['contains_evidence'] is False
            assert manifest['record_counts']['preparations'] >= 3
            for secret in fake_secrets:
                assert secret.encode() not in database_bytes, 'backup contains an external credential'
            with tempfile.TemporaryDirectory(prefix='p3-backup-inspection-', dir=data_dir) as inspection:
                snapshot = Path(inspection) / 'snapshot.sqlite'
                snapshot.write_bytes(database_bytes)
                with closing(sqlite3.connect(snapshot)) as connection:
                    configs = dict(connection.execute("SELECT id,value FROM settings WHERE id LIKE 'integration:%'"))
                    for value in configs.values():
                        config = json.loads(value)
                        assert not config['enabled'] and config.get('credential_ref') is None
            assert api(client, '/integrations/ai')['enabled'] and api(client, '/integrations/webhook')['enabled']
            expected_task_ids = {task['id'] for task in api(client, '/tasks?limit=200')}
            restore_response = client.post('/api/v1/restores/validate', files={
                'backup': ('synthetic-backup.zip', archive_response.content, 'application/zip')})
            assert restore_response.status_code == 202
            validated = run_job(client, services, restore_response.json()['data']['job_id'])
            preview = api(client, '/restores/' + validated['restore_id'])
            api(client, '/restores/' + validated['restore_id'] + '/apply', 'POST', {
                'confirmation_token': preview['confirmation_token']}, status=202)
            assert client.get('/api/v1/tasks').status_code == 401
            authenticate(client, application, reopen=True)
            for kind in ('ai', 'webhook'):
                restored_config = api(client, '/integrations/' + kind)
                assert not restored_config['enabled'] and not restored_config['has_secret']
            assert {task['id'] for task in api(client, '/tasks?limit=200')} == expected_task_ids
            assert retained_preparation_id in {row['id'] for row in api(client, preparation_route)['items']}
            model_before, webhook_before = len(model_requests), len(webhook.calls)
            now[0] = datetime(2026, 11, 10, 2, 30, 1, tzinfo=timezone.utc)
            services.ai.run_once()
            services.reminders.run_once()
            assert len(model_requests) == model_before and len(webhook.calls) == webhook_before
            checks['sanitized_backup_restore_preserves_data_and_stops_external_services'] = True
            expected_preparations = api(client, preparation_route)
            expected_application = api(client, '/applications/' + app_record['id'])

        reopened_app = create_app(data_dir, start_workers=False, credential_store=vault,
                                  clock=lambda: now[0], notifier=notify, webhook_transport=webhook)
        with TestClient(reopened_app, base_url=reopened_app.state.origin) as client:
            authenticate(client, reopened_app)
            assert {task['id'] for task in api(client, '/tasks?limit=200')} == expected_task_ids
            assert api(client, preparation_route) == expected_preparations
            assert api(client, '/applications/' + app_record['id']) == expected_application
            assert api(client, '/tasks/' + ai_task_id)['source']['reviewed']
            for kind in ('ai', 'webhook'):
                assert not api(client, '/integrations/' + kind)['enabled']
            reopened_app.state.services.ai.run_once()
            reopened_app.state.services.reminders.run_once()
            assert len(model_requests) == model_before and len(webhook.calls) == webhook_before
            checks['graceful_close_reopen_retains_data_and_no_external_reactivation'] = True
        for log in (data_dir / 'logs').glob('*.log*'):
            content = log.read_text(encoding='utf-8')
            assert not any(secret in content for secret in fake_secrets), 'log contains a credential'
        checks['logs_exclude_credentials'] = True
        return {'passed': True, 'simulation': True, 'ai_transport': 'local_http_stub',
                'webhook_transport': 'injected', 'windows_transport': 'injected', 'checks': checks,
                'ai_http_requests': len(model_requests), 'webhook_requests': len(webhook.calls),
                'windows_attempts': len(windows_calls), 'calendar_components': components,
                'persisted_task_count': len(expected_task_ids)}


def main():
    parser = argparse.ArgumentParser(description='仅在新目录运行P3虚构数据综合验收')
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.data_dir)
    except FileExistsError:
        parser.error('数据目录已包含文件；请选择全新目录，现有数据未修改')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
