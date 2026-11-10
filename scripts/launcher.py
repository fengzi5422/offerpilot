"""OfferPilot entry point: local API, authenticated UI and Windows tray."""
import argparse
import hashlib
import json
import logging
from multiprocessing.connection import Listener
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import threading
import time
import uuid

from runtime_support import InstanceLock, configure_logging, protect_directory, set_autostart
from desktop_runtime import DesktopWindow, activate_existing, validate_assets

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'backend'))


def show_startup_error(error,data,error_id=None):
    code=getattr(error,'code','STARTUP_FAILED')
    descriptions={
        'UI_ASSETS_MISSING':'界面文件不完整。请从完整安装包重新安装，不要只复制 OfferPilot.exe。',
        'LEGACY_INSTANCE_RUNNING':'已有另一目录的旧版 OfferPilot 正在使用此数据。请先从旧版托盘选择“退出”，再打开新版。旧版不会被强制关闭，原数据保持不变。',
        'WEBVIEW2_UNAVAILABLE':'独立窗口需要 Microsoft Edge WebView2 Runtime 和 .NET Framework 4.6.2 或以上。请安装 Windows 官方运行组件后重试，无需打开浏览器。',
        'WINDOW_OPEN_FAILED':'窗口暂时未能恢复，请稍后从托盘重试。',
    }
    message=descriptions.get(code,'程序未能完成启动，请保留日志和 backups 恢复点后重试。')
    message+='\n\n本地数据已保留：'+str(data)
    if error_id:
        message+='\n错误编号：'+error_id
    if sys.platform=='win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,message,'OfferPilot · 启动提示',0x10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--no-browser', '--headless', action='store_true', help='仅后台服务，用于自动化检查')
    parser.add_argument('--no-tray', action='store_true')
    args = parser.parse_args()
    data = (args.data_dir or Path(os.environ.get('OFFERPILOT_DATA_DIR',
            str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'OfferPilot')))).resolve()
    protect_directory(data)
    control_dir = data / 'runtime'
    protect_directory(control_dir)
    lock = InstanceLock(control_dir / 'instance.lock')
    info_path = control_dir / 'control.json'
    if not lock.acquire():
        try:
            info = json.loads(info_path.read_text(encoding='utf-8'))
            activate_existing(info)
        except Exception as exc:
            show_startup_error(exc,data)
            return 1
        return 0
    configure_logging(data)
    logging.getLogger('runtime').info('app.start')
    server = None
    listener = None
    tray = None
    desktop = None
    server_thread = None
    try:
        assets = validate_assets(ROOT)
        import uvicorn
        from app.main import create_app
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        app = create_app(data, origin=origin, static_dir=assets, autostart_callback=set_autostart)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        server_thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        server_thread.start()
        for _ in range(200):
            if server.started:
                break
            if not server_thread.is_alive():
                raise RuntimeError('LOCAL_SERVICE_START_FAILED')
            time.sleep(.05)
        else:
            raise RuntimeError('LOCAL_SERVICE_START_TIMEOUT')

        def ui_url():
            # A changed fragment alone does not remount the Vue application.
            return origin + '/?desktop=1&launch=' + secrets.token_hex(8) + '#code=' + app.state.sessions.issue_bootstrap()

        if not args.no_browser:
            desktop = DesktopWindow(ui_url,keep_running=not args.no_tray,
                                    hidden=args.autostart and not args.no_tray,
                                    session_validator=app.state.sessions.authenticate)

        def open_ui(*_):
            if desktop:
                desktop.open()

        def stop(*_):
            server.should_exit = True
            if tray:
                tray.stop()
            if desktop:
                desktop.quit()

        if sys.platform == 'win32':
            pipe = r'\\.\pipe\OfferPilot-' + hashlib.sha256(str(data).encode()).hexdigest()[:24]
            auth = secrets.token_bytes(32)
            listener = Listener(pipe, family='AF_PIPE', authkey=auth)
            info_path.write_text(json.dumps({'pipe': pipe, 'key': auth.hex(), 'origin': origin}), encoding='utf-8')

            def control_loop():
                while server_thread.is_alive():
                    try:
                        with listener.accept() as connection:
                            command = connection.recv()
                            if command == 'open_ui':
                                open_ui()
                                connection.send({'opened':bool(desktop)})
                            elif command == 'get_status':
                                connection.send({'version':'0.2.2','ui':'desktop' if desktop else 'headless',
                                                 'visible':desktop.visible if desktop else False})
                            elif command == 'close_ui':
                                if desktop:
                                    desktop.close()
                                connection.send({'closed':bool(desktop)})
                            elif command == 'quit':
                                connection.send({'stopping':True})
                                stop()
                            elif command == 'get_url':
                                connection.send(ui_url())
                    except Exception:
                        logging.getLogger('runtime').warning('control.failed')
                        if server.should_exit:
                            return
            threading.Thread(target=control_loop, daemon=True).start()
        logging.getLogger('runtime').info('app.ready')
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if not args.no_tray:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new('RGBA', (64, 64), '#234f42')
            draw = ImageDraw.Draw(image)
            draw.ellipse((14, 14, 49, 48), fill='#dae8c6')
            draw.line((18, 49, 43, 23), fill='#234f42', width=4)

            def exit_requested(*_):
                import ctypes
                if ctypes.windll.user32.MessageBoxW(None, '退出后将停止邮件同步和本地提醒。确定退出？',
                                                   'OfferPilot', 0x24) == 6:
                    stop()
            tray = pystray.Icon('OfferPilot', image, 'OfferPilot · 求职日程助手',
                                menu=pystray.Menu(pystray.MenuItem('打开界面', open_ui, default=True),
                                                  pystray.MenuItem('退出', exit_requested)))
            def run_tray():
                try:
                    tray.run()
                except Exception:
                    logging.getLogger('runtime').error('tray.failed')
                    if desktop:
                        desktop.keep_running=False
                        desktop.open()
            threading.Thread(target=run_tray,daemon=True,name='offerpilot-tray').start()
        if desktop:
            desktop.run(data)
        else:
            while server_thread.is_alive():
                server_thread.join(timeout=.5)
        stop()
        server_thread.join(timeout=30)
        return 0
    except Exception as exc:
        error_id = uuid.uuid4().hex
        logging.getLogger('runtime').error('app.failed', extra={'error_id': error_id,'error_code':getattr(exc,'code','STARTUP_FAILED')})
        print('OfferPilot 启动失败，请查看数据目录下 logs/app.log。')
        show_startup_error(exc,data,error_id)
        return 1
    finally:
        if server:
            server.should_exit = True
        if tray:
            tray.stop()
        if server_thread and server_thread.is_alive():
            server_thread.join(timeout=30)
        if listener:
            listener.close()
        info_path.unlink(missing_ok=True)
        lock.release()
        logging.getLogger('runtime').info('app.stop')


if __name__ == '__main__':
    raise SystemExit(main())
