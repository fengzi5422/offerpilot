"""Real WebView2 UI smoke, with synthetic data and a debug port only on this child.

Does not enable debugging in normal launches or connect to a user's browser.
Native file chooser/save dialogs and manual accessibility remain separate checks.
"""
import argparse
import base64
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import socket
import subprocess
import time

import httpx
import websocket


class Page:
    def __init__(self, url):
        self.connection=websocket.create_connection(url,timeout=20,suppress_origin=True)
        self.sequence=0

    def call(self,method,params=None):
        self.sequence+=1
        self.connection.send(json.dumps({'id':self.sequence,'method':method,'params':params or {}}))
        while True:
            result=json.loads(self.connection.recv())
            if result.get('id')==self.sequence:
                if 'error' in result:
                    raise RuntimeError(result['error'])
                return result.get('result',{})

    def js(self,expression):
        result=self.call('Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
        if result.get('exceptionDetails'):
            raise RuntimeError(result['exceptionDetails'].get('text','JavaScript failed'))
        return result.get('result',{}).get('value')

    def wait(self,expression):
        for _ in range(160):
            if self.js('Boolean('+expression+')'):
                return
            time.sleep(.15)
        raise TimeoutError(expression)

    def click(self,text):
        literal=json.dumps(text)
        self.js(f"(()=>{{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==={literal});if(!b)throw Error('Button missing');b.click();}})()")

    def screenshot(self,path):
        path.write_bytes(base64.b64decode(self.call('Page.captureScreenshot',{'format':'png'})['data']))


def check(executable,data,source=False):
    root=Path(__file__).resolve().parents[1]
    data=data.resolve()
    if not data.is_relative_to(root/'.runtime') or data.exists():
        raise ValueError('Choose a fresh project .runtime directory')
    data.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    env=os.environ.copy()
    env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS']=f'--remote-debugging-port={port}'
    command=[str(executable.resolve())]
    if source:
        command.append(str(root/'scripts/launcher.py'))
    command+=['--data-dir',str(data)]
    output=(data/'process.log').open('w',encoding='utf-8')
    process=subprocess.Popen(command,env=env,creationflags=subprocess.CREATE_NO_WINDOW,stdout=output,stderr=output)
    info=None
    page=None
    try:
        for _ in range(240):
            control=data/'runtime/control.json'
            if control.exists():
                info=json.loads(control.read_text())
                try:
                    targets=httpx.get(f'http://127.0.0.1:{port}/json/list',timeout=1).json()
                    target=next((t for t in targets if t.get('url','').startswith(info['origin'])),None)
                    if target:
                        page=Page(target['webSocketDebuggerUrl'])
                        break
                except (httpx.HTTPError,ValueError):
                    pass
            if process.poll() is not None:
                raise RuntimeError('Desktop process exited before ready')
            time.sleep(.25)
        if page is None:
            raise TimeoutError('Native WebView2 did not start')

        page.wait("document.body.innerText.includes('求职路上')")
        page.click('先体验待办')
        page.wait("!document.querySelector('.modal') && document.querySelector('.desktop')")
        assert page.js("navigator.userAgent.includes('OfferPilotDesktop/0.2.2')")
        page.click('设置与诊断')
        page.wait("document.querySelector('input[type=number]')?.value==='2026'")
        page.click('收件识别')
        page.wait("document.querySelector('input[type=file]')!==null")
        mail=('From: hr@example.test\r\nSubject: 示例科技面试安排\r\n'
              'Content-Type: text/plain; charset=utf-8\r\n\r\n'
              '面试安排如下：\n1、面试签到时间：11月16日（周一）8:30-9:30\n'
              '面试地点：上海市示例路100号\n会议链接：https://meet.example.test/room')
        page.js("(()=>{const input=document.querySelector('input[type=file]');const dt=new DataTransfer();"
                f"dt.items.add(new File([{json.dumps(mail)}],'synthetic.eml',{{type:'message/rfc822'}}));"
                "input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()")
        page.wait("document.querySelector('.modal') && document.body.innerText.includes('已处理。以保存的事件信息为准')")
        page.click('查看并编辑已保存的事件 →')
        page.wait("document.querySelector('.modal-head')?.innerText.includes('编辑事件')")
        # Select the last dialog explicitly: review and editor are sibling backdrops.
        values=page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).querySelector('input[type=date]').value")
        assert values=='2026-11-16',values
        assert page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).querySelector('input[type=time]').value")=='08:30'
        assert page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).innerText.includes('地点')")
        page.screenshot(data/'event-editor.png')
        # Confirm uses a native dialog in production; accept only this synthetic test's reschedule.
        page.js("window.confirm=()=>true;(()=>{const input=[...document.querySelectorAll('.modal-backdrop')].at(-1).querySelector('input[type=time]');input.value='08:45';input.dispatchEvent(new Event('input',{bubbles:true}));})()")
        page.click('保存修改')
        page.wait("!document.querySelector('.modal')")
        page.click('事件日历')
        page.wait("document.querySelector('.calendar-jump input') && !document.querySelector('.calendar-jump input').disabled")
        page.js("(()=>{const input=document.querySelector('.calendar-jump input');input.value='2026-11-16';input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));})()")
        page.wait("document.querySelector('.calendar-event')?.innerText.includes('08:45')")
        page.screenshot(data/'calendar.png')
        assert page.js('document.documentElement.scrollWidth<=innerWidth')

        def control(command):
            with Client(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as pipe:
                pipe.send(command)
                assert pipe.poll(20)
                return pipe.recv()

        assert control('get_status')=={'version':'0.2.2','ui':'desktop','visible':True}
        page.js('window.__qaRetained=true')
        second=subprocess.run(command,env=env,creationflags=subprocess.CREATE_NO_WINDOW,timeout=35)
        assert second.returncode==0
        assert page.js('window.__qaRetained===true')
        # Restore a real synthetic backup: the backend revokes the still-visible window's session.
        page.js("""(async()=>{
            const session=await (await fetch('/api/v1/session')).json();
            async function api(path,body){
                const headers={'X-CSRF-Token':session.data.csrf_token};
                const options={headers};
                if(body!==undefined){options.method='POST';if(body instanceof FormData)options.body=body;
                    else{headers['Content-Type']='application/json';options.body=JSON.stringify(body);}}
                const response=await fetch('/api/v1'+path,options);
                if(!response.ok)throw Error('Synthetic restore failed');
                return (await response.json()).data;
            }
            async function wait(job){for(let i=0;i<200;i++){
                const value=await api('/jobs/'+job.job_id);
                if(value.status==='succeeded')return value.result;
                if(value.status==='failed')throw Error('Synthetic job failed');
                await new Promise(r=>setTimeout(r,100));}throw Error('Job timeout');}
            const backup=await wait(await api('/backups',{include_evidence:false}));
            const blob=await (await fetch('/api/v1/artifacts/'+backup.artifact_id)).blob();
            const form=new FormData();form.append('backup',blob,'synthetic.zip');
            const validated=await wait(await api('/restores/validate',form));
            const preview=await api('/restores/'+validated.restore_id);
            await api('/restores/'+validated.restore_id+'/apply',{confirmation_token:preview.confirmation_token});
            return (await fetch('/api/v1/session')).status===401;
        })()""")
        second=subprocess.run(command,env=env,creationflags=subprocess.CREATE_NO_WINDOW,timeout=35)
        assert second.returncode==0
        page.wait("window.__qaRetained===undefined && document.querySelector('.desktop') && !document.querySelector('.connection-state')")
        page.call('Emulation.setDeviceMetricsOverride',{'width':720,'height':540,'deviceScaleFactor':1,'mobile':False})
        assert page.js('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(data/'compact-window.png')
        page.call('Emulation.clearDeviceMetricsOverride')
        # Real close event hides the window; service and page stay alive.
        assert control('close_ui')=={'closed':True}
        assert control('get_status')['visible'] is False
        time.sleep(.3)
        assert process.poll() is None
        assert httpx.get(info['origin']+'/api/v1/health').status_code==200
        # A second EXE invocation takes the real single-instance path.
        second=subprocess.run(command,env=env,creationflags=subprocess.CREATE_NO_WINDOW,timeout=35)
        assert second.returncode==0
        assert control('get_status')['visible'] is True
        page.wait("document.querySelector('.desktop') && !document.querySelector('.connection-state')")
        page.screenshot(data/'restored-window.png')
        print('PASS: actual WebView2, bootstrap, onboarding, UI mail import, date/time prefill, reschedule, calendar, close-to-tray, second launch restores window; no external browser launched')
    except Exception:
        if page:
            page.screenshot(data/'failure.png')
            (data/'failure.txt').write_text(str(page.js('document.body.innerText')),encoding='utf-8')
        raise
    finally:
        if page:
            page.connection.close()
        if info:
            try:
                with Client(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as pipe:
                    pipe.send('quit')
                    if pipe.poll(10):
                        pipe.recv()
            except (OSError,EOFError):
                pass
        try:
            process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        output.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--source',action='store_true')
    options=parser.parse_args()
    check(options.executable,options.data_dir,options.source)
