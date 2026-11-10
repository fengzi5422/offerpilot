"""Exercise a built/installed app using isolated synthetic data, never LocalAppData."""
import argparse
import io
import json
from multiprocessing.connection import Client
from pathlib import Path
import subprocess
import time
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx


def check(executable, data):
    root = Path(__file__).resolve().parents[1]
    data = data.resolve()
    if not data.is_relative_to(root / '.runtime'):
        raise ValueError('Smoke data must be inside this project .runtime directory')
    data.mkdir(parents=True, exist_ok=True)
    if (data / 'data.sqlite').exists():
        raise ValueError('Choose a fresh smoke directory to avoid modifying previous data')
    process = subprocess.Popen([str(executable.resolve()), '--data-dir', str(data), '--no-browser', '--no-tray'],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        control = data / 'runtime' / 'control.json'
        for _ in range(120):
            if control.exists():
                break
            if process.poll() is not None:
                raise RuntimeError('Packaged process exited before ready')
            time.sleep(.25)
        info = json.loads(control.read_text())

        def login(client):
            with Client(info['pipe'], family='AF_PIPE', authkey=bytes.fromhex(info['key'])) as connection:
                connection.send('get_url')
                url = connection.recv()
            code = parse_qs(urlsplit(url).fragment)['code'][0]
            response = client.post('/api/v1/session/bootstrap', json={'code': code})
            response.raise_for_status()
            client.headers['X-CSRF-Token'] = response.json()['data']['csrf_token']

        with httpx.Client(base_url=info['origin'], headers={'Origin': info['origin']}, timeout=20) as client:
            def get(path):
                response = client.get('/api/v1' + path)
                response.raise_for_status()
                return response.json()['data']

            def post(path, body):
                response = client.post('/api/v1' + path, json=body)
                response.raise_for_status()
                return response.json()['data']

            def wait(job):
                for _ in range(300):
                    result = get('/jobs/' + job['job_id'])
                    if result['status'] == 'succeeded':
                        return result['result']
                    if result['status'] in ('failed', 'interrupted'):
                        raise RuntimeError(result['error_code'])
                    time.sleep(.1)
                raise TimeoutError('Background job did not finish')

            assert get('/health')['ready']
            assert 'OfferPilot' in client.get('/').text
            login(client)
            assert get('/tasks') == []
            task = post('/tasks', {'title': 'PRIVATE_SMOKE_TITLE', 'kind': 'custom', 'priority': 2,
                        'temporal': {'kind': 'absolute', 'timezone': 'UTC', 'timezone_source': 'user_confirmed',
                        'raw_text': '2050-01-02 12:00', 'precision': 'minute', 'needs_confirmation': False,
                        'due_at': '2050-01-02T12:00:00Z'}})
            raw = ('From: hr@example.test\r\nSubject: 示例科技测评通知\r\n'
                   'Content-Type: text/plain; charset=utf-8\r\n\r\n收到后48小时内完成测评。').encode()
            uploaded = client.post('/api/v1/emails/import', files={'file': ('synthetic.eml', raw)})
            uploaded.raise_for_status()
            message = wait(uploaded.json()['data'])['message_id']
            for _ in range(100):
                mail = get('/emails/' + message)
                if mail['state'] != 'new':
                    break
                time.sleep(.1)
            assert mail['state'] == 'review_needed'
            artifact = wait(post('/diagnostics/export', {}))
            diagnostic = client.get('/api/v1/artifacts/' + artifact['artifact_id'])
            diagnostic.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(diagnostic.content)) as archive:
                assert all(b'PRIVATE_SMOKE_TITLE' not in archive.read(name) for name in archive.namelist())
            artifact = wait(post('/backups', {'include_evidence': False}))
            archive = client.get('/api/v1/artifacts/' + artifact['artifact_id'])
            archive.raise_for_status()
            validated = client.post('/api/v1/restores/validate', files={'backup': ('backup.zip', archive.content)})
            validated.raise_for_status()
            restore = wait(validated.json()['data'])
            preview = get('/restores/' + restore['restore_id'])
            post('/restores/' + restore['restore_id'] + '/apply', {'confirmation_token': preview['confirmation_token']})
            assert client.get('/api/v1/tasks').status_code == 401
            login(client)
            assert get('/tasks/' + task['id'])['title'] == 'PRIVATE_SMOKE_TITLE'
            print('PASS: packaged startup, UI, login, task, queued import, independent parse, diagnostics, backup, restore, session reset')
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    options = parser.parse_args()
    check(options.executable, options.data_dir)
