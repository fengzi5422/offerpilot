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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import websocket

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.version import VERSION


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

    def key(self,key,shift=False):
        code={'Tab':9,'Escape':27,'Enter':13}[key]
        params={'key':key,'code':key,'windowsVirtualKeyCode':code,'modifiers':8 if shift else 0}
        if key=='Enter':
            params.update(text='\r',unmodifiedText='\r')
        self.call('Input.dispatchKeyEvent',{'type':'keyDown',**params})
        self.call('Input.dispatchKeyEvent',{'type':'keyUp',**params})


def check_ai(page, data):
    """Own a synthetic model; never read or call a user's configured service."""
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            time.sleep(.6)
            source = json.loads(payload['messages'][-1]['content'])['body']
            action = {'kind': 'interview', 'quote': '请参加面试', 'company': '示例科技',
                'time_quote': '11月16日8:30', 'location_quote': '地点：上海市示例路200号',
                'meeting_quote': source.splitlines()[-1]}
            content = 'invalid' if len(calls) == 1 else '```json\n' + json.dumps({'actions': [action]}, ensure_ascii=False) + '\n```'
            raw = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        page.click('收件识别')
        page.wait("document.querySelector('input[type=file]')")
        mail = ('From: hr@example.test\r\nSubject: 示例科技技术交流\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
            '请参加面试，时间为11月16日8:30，11月17日9:30为备用场次。\n地点：上海市示例路200号\n会议链接：https://meet.example.test/room?token=SYNTHETIC')
        page.js("(()=>{const input=document.querySelector('input[type=file]');const dt=new DataTransfer();"
            f"dt.items.add(new File([{json.dumps(mail)}],'ai-synthetic.eml',{{type:'message/rfc822'}}));"
            "input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()")
        page.wait("document.querySelector('.candidate-card input[type=date]')")
        page.js("""window.__qaAI=async(method,body)=>{
            const session=(await(await fetch('/api/v1/session')).json()).data;
            const response=await fetch('/api/v1/integrations/ai',{method,headers:{'X-CSRF-Token':session.csrf_token,'Content-Type':'application/json'},body:JSON.stringify(body)});
            if(!response.ok)throw Error('Synthetic configuration failed');return(await response.json()).data;
        }""")
        page.js(f"(async()=>{{const s=(await(await fetch('/api/v1/integrations/ai')).json()).data;return await window.__qaAI('PATCH',{{expected_version:s.version,changes:{{base_url:'http://127.0.0.1:{server.server_port}/v1/chat/completions',model:'synthetic',api_key:'synthetic-only',enabled:true,consent:true}}}});}})()")
        page.click('AI 辅助识别（需启用）')
        page.wait("document.body.innerText.includes('AI 正在') || document.body.innerText.includes('AI 已排队')")
        page.wait("document.querySelector('.modal .error-box')?.innerText.includes('原文')")
        page.wait("[...document.querySelectorAll('button')].some(b=>b.textContent.includes('AI 辅助识别')&&!b.disabled)")
        page.screenshot(data/'ai-actionable-error.png')
        page.click('AI 辅助识别（需启用）')
        page.wait("document.body.innerText.includes('AI 识别完成')")
        assert page.js("document.querySelector('.candidate-card input[type=date]').value") == '2026-11-16'
        assert page.js("document.querySelector('.candidate-card input[type=time]').value") == '08:30'
        assert page.js("[...document.querySelectorAll('.candidate-card input')].some(x=>x.value==='上海市示例路200号')")
        assert page.js("document.querySelector('.candidate-card input[type=url]').value") == 'https://meet.example.test/room?token=SYNTHETIC'
        assert len(calls) == 2
        assert 'SYNTHETIC' not in json.dumps(calls)
        page.screenshot(data/'ai-complete-prefill.png')
        page.click('确认所选事件')
        page.wait("!document.querySelector('.modal')")
        print('PASS: real local model HTTP, immediate AI progress, actionable invalid-output error, retry without version conflict, grounded date/time/location/link prefill and save')
    finally:
        page.js("(async()=>{const s=(await(await fetch('/api/v1/integrations/ai')).json()).data;return window.__qaAI('DELETE',{expected_version:s.version});})()")
        server.shutdown()
        server.server_close()
        worker.join(3)


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
        assert page.js(f"navigator.userAgent.includes('OfferPilotDesktop/{VERSION}')")
        page.click('设置与诊断')
        page.wait("document.querySelector('input[type=number]')?.value==='2026'")
        page.wait("document.querySelector('.ai-settings form') && document.querySelector('.hook-settings form')")
        assert page.js("[...document.querySelectorAll('.ai-settings input[type=checkbox]')].every(x=>!x.checked)")
        assert page.js("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(data/'p3-settings.png')
        page.click('申请看板')
        page.wait("document.body.innerText.includes('新建申请')")
        page.click('新建申请')
        page.wait("document.querySelector('.modal input')")
        page.js("(()=>{const inputs=document.querySelectorAll('.modal input');for(const [i,value] of ['示例科技','开发工程师'].entries()){inputs[i].value=value;inputs[i].dispatchEvent(new Event('input',{bubbles:true}));}})()")
        page.click('创建申请')
        page.wait("document.body.innerText.includes('申请已保存')")
        page.click('关闭')
        page.screenshot(data/'p3-board.png')
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
        # Review is replaced by the editor; select its current backdrop explicitly.
        values=page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).querySelector('input[type=date]').value")
        assert values=='2026-11-16',values
        assert page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).querySelector('input[type=time]').value")=='08:30'
        assert page.js("[...document.querySelectorAll('.modal-backdrop')].at(-1).innerText.includes('地点')")
        page.wait("[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='添加准备模板'&&!b.disabled)")
        page.click('添加准备模板')
        page.wait("document.querySelectorAll('.preparation-items li').length===4")
        page.js("document.querySelector('.preparation-items input[type=checkbox]').click()")
        page.wait("document.querySelectorAll('.preparation-items li.completed').length===1")
        page.js("document.querySelector('.preparation-checklist').scrollIntoView({block:'center'})")
        page.screenshot(data/'p3-preparation.png')
        page.js("[...document.querySelectorAll('.modal summary')].find(x=>x.textContent.trim()==='关联求职申请').click()")
        page.wait("[...document.querySelectorAll('.modal button')].some(b=>b.textContent.trim()==='保存申请关联')")
        page.js("(()=>{const select=[...document.querySelectorAll('.modal select')].find(x=>[...x.options].some(o=>o.text.includes('开发工程师')));select.value=select.options[1].value;select.dispatchEvent(new Event('change',{bubbles:true}));})()")
        page.click('保存申请关联')
        page.wait("document.body.innerText.includes('申请关联已保存')")
        page.screenshot(data/'event-editor.png')
        page.js("document.querySelector('.modal-head button').focus()")
        page.key('Tab',shift=True)
        assert page.js("document.activeElement?.textContent.trim()==='保存修改'"), 'Reverse Tab escaped the upper dialog'
        page.key('Tab')
        assert page.js("document.activeElement?.getAttribute('aria-label')==='关闭'"), 'Tab did not wrap inside upper dialog'
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
        check_ai(page,data)

        def control(command):
            with Client(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as pipe:
                pipe.send(command)
                assert pipe.poll(20)
                return pipe.recv()

        assert control('get_status')=={'version':VERSION,'ui':'desktop','visible':True}
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
        page.click('今日待办')
        page.wait("[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='添加待办')")
        page.js("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='添加待办').focus()")
        page.key('Enter')
        page.wait("document.querySelector('.modal input')")
        assert page.js("document.activeElement?.getAttribute('aria-label')==='关闭'")
        page.key('Escape')
        page.wait("!document.querySelector('.modal')")
        assert page.js("document.activeElement?.textContent.trim()==='添加待办'"), 'Closing the dialog did not restore focus'
        # Halve CSS viewport and double device pixels, as browser zoom does.
        # CSS zoom alone leaves media queries unchanged and is not equivalent.
        # This is emulated content scaling, not physical monitor/DPI acceptance.
        viewport=page.js('({width:innerWidth,height:innerHeight})')
        page.call('Emulation.setDeviceMetricsOverride',{'width':viewport['width']//2,'height':viewport['height']//2,'deviceScaleFactor':2,'mobile':False})
        for label in ('今日待办','申请看板','事件日历','设置与诊断'):
            page.click(label)
            page.wait("!document.querySelector('.loading-block')")
            assert page.js('document.documentElement.scrollWidth<=innerWidth+1'), 'Horizontal overflow at 200%: '+label
        page.screenshot(data/'content-scale-200.png')
        page.click('今日待办')
        page.wait("[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='添加待办')")
        page.click('添加待办')
        page.wait("document.querySelector('.modal input')")
        assert page.js('document.documentElement.scrollWidth<=innerWidth+1'), 'Dialog overflow at 200%'
        assert page.js("(()=>{const b=[...document.querySelectorAll('.modal button')].find(x=>x.textContent.trim()==='添加事件');b.focus();b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth;})()"), 'Save button is clipped at 200%'
        page.screenshot(data/'dialog-scale-200.png')
        page.key('Escape')
        page.wait("!document.querySelector('.modal')")
        page.call('Emulation.clearDeviceMetricsOverride')
        print('PASS: WebView2 keyboard Enter/Tab/Shift-Tab/Escape, focus return, emulated 200% content scale across tasks/board/calendar/settings')
        assert not page.js("document.querySelector('.page-heading .accent-dot')")
        page.js('window.confirm=()=>false')
        page.click('退出程序')
        assert process.poll() is None
        page.js('window.confirm=()=>true')
        page.click('退出程序')
        process.wait(timeout=25)
        assert process.returncode == 0
        assert not (data/'runtime/control.json').exists()
        print('PASS: visible Exit cancel preserves application; confirmed Exit closes native process and releases instance')
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
