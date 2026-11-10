"""Check graceful and abrupt restarts using a fresh synthetic data directory."""
import argparse
from contextlib import contextmanager
import json
from multiprocessing.connection import Client
from pathlib import Path
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

import httpx


def check(executable,data):
    root=Path(__file__).resolve().parents[1]
    data=data.resolve()
    if not data.is_relative_to(root/'.runtime') or data.exists():
        raise ValueError('Choose a fresh project .runtime directory')
    data.mkdir(parents=True)
    process=None
    info=None

    def control(command):
        with Client(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as pipe:
            pipe.send(command)
            if not pipe.poll(15):
                raise TimeoutError('Control command timed out')
            return pipe.recv()

    @contextmanager
    def launch():
        nonlocal process,info
        process=subprocess.Popen([str(executable.resolve()),'--data-dir',str(data),'--headless','--no-tray'],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(200):
            path=data/'runtime/control.json'
            if path.exists():
                try:
                    info=json.loads(path.read_text())
                    if httpx.get(info['origin']+'/api/v1/health',timeout=1).status_code==200:
                        break
                except (httpx.HTTPError,ValueError):
                    pass
            if process.poll() is not None:
                raise RuntimeError('Restart failed')
            time.sleep(.2)
        else:
            raise TimeoutError('Restart timed out')
        client=httpx.Client(base_url=info['origin'],headers={'Origin':info['origin']},timeout=20)
        code=parse_qs(urlsplit(control('get_url')).fragment)['code'][0]
        response=client.post('/api/v1/session/bootstrap',json={'code':code})
        response.raise_for_status()
        client.headers['X-CSRF-Token']=response.json()['data']['csrf_token']
        try:
            yield client
        finally:
            client.close()

    def api(client,path,method='GET',body=None):
        response=client.request(method,'/api/v1'+path,**({'json':body} if method!='GET' else {}))
        response.raise_for_status()
        return response.json()['data']

    try:
        with launch() as client:
            settings=api(client,'/settings')
            api(client,'/settings','PATCH',{'expected_version':settings['version'],'changes':{'default_event_year':2027}})
            raw=('From: hr@example.test\r\nSubject: 示例银行面试\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
                 '面试签到时间：11月16日（周二）8:30\n地点：示例会议室').encode()
            response=client.post('/api/v1/emails/import',files={'file':('synthetic.eml',raw)})
            response.raise_for_status()
            for _ in range(200):
                tasks=api(client,'/tasks')
                if tasks:
                    break
                time.sleep(.15)
            assert len(tasks)==1
            original=api(client,'/tasks/'+tasks[0]['id'])
            assert original['starts_at']=='2027-11-16T00:30:00Z'
            assert original['confirmed']
        control('quit')
        process.wait(timeout=35)
        for abrupt in (False,True):
            with launch() as client:
                assert api(client,'/settings')['default_event_year']==2027
                restored=api(client,'/tasks/'+original['id'])
                for key in ('id','title','temporal','status','event_details','schedule_revision','next_reminder_at','reminders'):
                    assert restored[key]==original[key],key
                assert len(api(client,'/tasks'))==1
            if abrupt:
                control('quit')
            else:
                # Simulate unexpected process loss; only this synthetic child is stopped.
                process.kill()
            process.wait(timeout=35)
        print('PASS: packaged graceful exit/restart and forced process stop/restart preserve event, year, location, status, reminder IDs and plans; no duplicate tasks')
    finally:
        if process and process.poll() is None:
            try:
                control('quit')
                process.wait(timeout=35)
            except (OSError,EOFError,subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    options=parser.parse_args()
    check(options.executable,options.data_dir)
