"""OfferPilot entry point: local API, authenticated UI and Windows tray."""
import argparse
import hashlib
import json
import logging
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser
import uuid

from runtime_support import InstanceLock, configure_logging, protect_directory, set_autostart

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
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
            with Client(info['pipe'], family='AF_PIPE', authkey=bytes.fromhex(info['key'])) as connection:
                connection.send('open_ui')
        except Exception:
            print('OfferPilot 已在运行。请稍后从托盘打开界面。')
        return 0
    configure_logging(data)
    logging.getLogger('runtime').info('app.start')
    server = None
    listener = None
    tray = None
    try:
        import uvicorn
        from app.main import create_app
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        app = create_app(data, origin=origin, autostart_callback=set_autostart)
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
            return origin + '/#code=' + app.state.sessions.issue_bootstrap()

        def open_ui(*_):
            webbrowser.open(ui_url())

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
                            elif command == 'get_url':
                                connection.send(ui_url())
                    except Exception:
                        logging.getLogger('runtime').warning('control.failed')
                        if server.should_exit:
                            return
            threading.Thread(target=control_loop, daemon=True).start()
        logging.getLogger('runtime').info('app.ready')
        if not args.no_browser and not args.autostart:
            open_ui()

        def stop(*_):
            server.should_exit = True
            if tray:
                tray.stop()

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
            tray.run()
        else:
            while server_thread.is_alive():
                server_thread.join(timeout=.5)
        stop()
        server_thread.join(timeout=15)
        return 0
    except Exception:
        error_id = uuid.uuid4().hex
        logging.getLogger('runtime').error('app.failed', extra={'error_id': error_id})
        print('OfferPilot 启动失败，请查看数据目录下 logs/app.log。')
        if sys.platform == 'win32':
            import ctypes
            message = ('OfferPilot 未能完成启动，已保留本地数据。\n\n'
                       '错误编号：' + error_id + '\n日志与备份目录：' + str(data) +
                       '\n\n请保留 backups 目录中的恢复点，勿删除原数据库。\n是否打开数据目录以查看日志和备份？')
            if ctypes.windll.user32.MessageBoxW(None, message, 'OfferPilot · 启动恢复', 0x14) == 6:
                os.startfile(str(data))
        return 1
    finally:
        if server:
            server.should_exit = True
        if listener:
            listener.close()
        info_path.unlink(missing_ok=True)
        lock.release()
        logging.getLogger('runtime').info('app.stop')


if __name__ == '__main__':
    raise SystemExit(main())
