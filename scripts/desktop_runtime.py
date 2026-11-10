"""Windows app window and startup checks; no Python bridge is exposed to pages."""
from html.parser import HTMLParser
from pathlib import Path
from multiprocessing.connection import Client
import threading
import sys
from urllib.parse import unquote, urlsplit


class StartupError(RuntimeError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='script' and attrs.get('src'):
            self.paths.append(attrs['src'])
        elif tag=='link' and attrs.get('rel') in ('stylesheet','modulepreload','icon') and attrs.get('href'):
            self.paths.append(attrs['href'])


def validate_assets(root):
    folder=(Path(root)/'frontend/dist').resolve()
    try:
        parser=AssetReferences()
        parser.feed((folder/'index.html').read_text(encoding='utf-8'))
        if not parser.paths:
            raise ValueError()
        for ref in parser.paths:
            url=urlsplit(ref)
            if url.scheme or url.netloc:
                raise ValueError()
            target=(folder/unquote(url.path).lstrip('/')).resolve()
            if not target.is_relative_to(folder) or not target.is_file():
                raise ValueError()
    except (OSError,ValueError):
        raise StartupError('UI_ASSETS_MISSING') from None
    return folder


def activate_existing(info, connector=Client):
    try:
        with connector(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as connection:
            connection.send('get_status')
            if not connection.poll(3):
                raise StartupError('LEGACY_INSTANCE_RUNNING')
            status=connection.recv()
        if not isinstance(status,dict) or status.get('ui')!='desktop':
            raise StartupError('LEGACY_INSTANCE_RUNNING')
        with connector(info['pipe'],family='AF_PIPE',authkey=bytes.fromhex(info['key'])) as connection:
            connection.send('open_ui')
            if not connection.poll(10) or not connection.recv().get('opened'):
                raise StartupError('WINDOW_OPEN_FAILED')
    except (EOFError,KeyError,ValueError,OSError):
        raise StartupError('LEGACY_INSTANCE_RUNNING') from None


def require_webview2():
    if sys.platform!='win32':
        raise StartupError('WEBVIEW2_UNAVAILABLE')
    import winreg
    found=False
    for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for base in (r'SOFTWARE\Microsoft\EdgeUpdate\Clients',r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients'):
            try:
                with winreg.OpenKey(hive,base+r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') as key:
                    found=int(str(winreg.QueryValueEx(key,'pv')[0]).split('.')[0])>=86 or found
            except (OSError,ValueError):
                pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as key:
            found=found and winreg.QueryValueEx(key,'Release')[0]>=394802
    except OSError:
        found=False
    if not found:
        raise StartupError('WEBVIEW2_UNAVAILABLE')


class DesktopWindow:
    def __init__(self,url_factory,*,keep_running=True,hidden=False,webview_module=None,session_validator=None):
        if webview_module is None:
            import webview as webview_module
        self.webview=webview_module
        self.url_factory=url_factory
        self.session_validator=session_validator
        self.keep_running,self.visible,self.exiting=keep_running,not hidden,False
        self._open_lock=threading.Lock()
        self.webview.settings.update(ALLOW_FILE_URLS=False,ALLOW_DOWNLOADS=True,
            OPEN_EXTERNAL_LINKS_IN_BROWSER=True,OPEN_DEVTOOLS_IN_DEBUG=False)
        self.window=self.webview.create_window('OfferPilot · 求职日程助手',url=url_factory(),
            width=1200,height=820,min_size=(720,540),hidden=hidden,background_color='#faf9f5',
            text_select=True,confirm_close=False)
        self.window.events.closing+=self.on_closing
        self.window.events.closed+=self.on_closed

    def on_closed(self):
        self.exiting=True
        self.visible=False

    def on_closing(self):
        if self.keep_running and not self.exiting:
            self.visible=False
            self.window.hide()
            return False

    def open(self):
        with self._open_lock:
            if self.exiting:
                return
            if not self.visible or not self.session_is_valid():
                self.window.load_url(self.url_factory())
                self.visible=True
            self.window.show()
            self.window.restore()

    def session_is_valid(self):
        if self.session_validator is None:
            return True
        try:
            for cookie in self.window.get_cookies():
                if 'offerpilot_session' in cookie:
                    self.session_validator(cookie['offerpilot_session'].value)
                    return True
        except Exception:
            # Invalid/expired sessions are replaced only by a user-requested reopen.
            pass
        return False

    def quit(self):
        if self.exiting:
            return
        self.exiting=True
        self.window.destroy()

    def close(self):
        """Use the same native close event as the title bar, including tray hiding."""
        self.window.destroy()

    def run(self,data_dir):
        require_webview2()
        self.webview.start(gui='edgechromium',private_mode=True,
            storage_path=str(Path(data_dir)/'runtime/webview'),
            user_agent='OfferPilotDesktop/1.0.0-rc.2',localization={
                'global.quitConfirmation':'退出后将停止邮件同步与提醒。确定退出？',
                'global.ok':'确定','global.cancel':'取消','global.saveFile':'保存文件',
                'windows.fileFilter.allFiles':'所有文件'})
