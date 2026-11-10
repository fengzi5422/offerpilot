import importlib
from http.cookies import SimpleCookie
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).parent))


def desktop_module():
    return importlib.import_module('desktop_runtime')


def test_missing_frontend_index_fails_before_opening_a_broken_page(tmp_path):
    desktop=desktop_module()
    (tmp_path/'frontend/dist/assets').mkdir(parents=True)
    with pytest.raises(desktop.StartupError) as error:
        desktop.validate_assets(tmp_path)
    assert error.value.code=='UI_ASSETS_MISSING'


def test_missing_bundled_script_and_escaped_paths_are_rejected(tmp_path):
    desktop=desktop_module()
    folder=tmp_path/'frontend/dist'
    folder.mkdir(parents=True)
    index=folder/'index.html'
    index.write_text('<title>OfferPilot</title><script type="module" src="/assets/app.js"></script>')
    with pytest.raises(desktop.StartupError):
        desktop.validate_assets(tmp_path)
    (folder/'assets').mkdir()
    (folder/'assets/app.js').write_text('app')
    assert desktop.validate_assets(tmp_path)==folder
    index.write_text('<script type="module" src="/../secret.js"></script>')
    with pytest.raises(desktop.StartupError):
        desktop.validate_assets(tmp_path)


class Event:
    def __init__(self): self.handlers=[]
    def __iadd__(self,handler):
        self.handlers.append(handler)
        return self


class Window:
    def __init__(self):
        self.events=SimpleNamespace(closing=Event(),closed=Event(),loaded=Event())
        self.calls=[]
    def hide(self): self.calls.append('hide')
    def show(self): self.calls.append('show')
    def restore(self): self.calls.append('restore')
    def load_url(self,url): self.calls.append(('load',url))
    def destroy(self): self.calls.append('destroy')


def fake_webview():
    window=Window()
    def create(title,**options):
        window.title,window.options=title,options
        return window
    return SimpleNamespace(settings={},create_window=create,window=window)


def test_close_hides_to_tray_and_reopen_uses_fresh_bootstrap_without_recreating_window():
    desktop=desktop_module()
    webview=fake_webview()
    shell=desktop.DesktopWindow(lambda:'http://127.0.0.1:1234/?desktop=1#code=test',
                                keep_running=True,webview_module=webview)
    assert shell.on_closing() is False
    assert webview.window.calls==['hide']
    shell.open()
    assert webview.window.calls==['hide',('load','http://127.0.0.1:1234/?desktop=1#code=test'),'show','restore']
    # Repeated launch while editing restores focus without reloading away edits.
    shell.open()
    assert webview.window.calls[-1]=='restore'
    assert len([c for c in webview.window.calls if isinstance(c,tuple)])==1
    shell.quit()
    shell.quit()
    assert webview.window.calls.count('destroy')==1
    assert shell.on_closing() is not False
    assert webview.window.calls[-1]=='destroy'


def test_no_tray_close_exits_and_runtime_never_exposes_python_api():
    webview=fake_webview()
    shell=desktop_module().DesktopWindow(lambda:'http://127.0.0.1:1234/',keep_running=False,webview_module=webview)
    assert shell.on_closing() is not False
    assert not webview.window.calls
    assert not webview.window.options.get('js_api')
    assert webview.settings['ALLOW_FILE_URLS'] is False
    assert webview.settings['ALLOW_DOWNLOADS'] is True


def test_legacy_instance_does_not_get_commanded_to_open_a_browser():
    desktop=desktop_module()
    sent=[]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def send(self,value): sent.append(value)
        def poll(self,timeout): return True
        def recv(self): raise EOFError()
    with pytest.raises(desktop.StartupError) as error:
        desktop.activate_existing({'pipe':'test','key':'00'*32},connector=lambda *args,**kwargs:Connection())
    assert error.value.code=='LEGACY_INSTANCE_RUNNING'
    assert sent==['get_status']


def test_existing_desktop_instance_is_reused():
    desktop=desktop_module()
    sent=[]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def send(self,value): sent.append(value)
        def poll(self,timeout): return True
        def recv(self): return {'ui':'desktop'} if sent[-1]=='get_status' else {'opened':True}
    desktop.activate_existing({'pipe':'test','key':'00'*32},connector=lambda *args,**kwargs:Connection())
    assert sent==['get_status','open_ui']


def test_visible_window_renews_revoked_session_but_preserves_valid_edits():
    webview=fake_webview()
    webview.window.get_cookies=lambda:[SimpleCookie('offerpilot_session=test-token')]
    valid=True
    def authenticate(token):
        assert token=='test-token'
        if not valid:
            raise ValueError('expired')
    shell=desktop_module().DesktopWindow(lambda:'fresh-url',webview_module=webview,session_validator=authenticate)
    shell.open()
    assert webview.window.calls==['show','restore']
    valid=False
    shell.open()
    assert webview.window.calls[-3:]==[('load','fresh-url'),'show','restore']
