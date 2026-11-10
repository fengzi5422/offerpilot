from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
datas = [(str(root / 'frontend' / 'dist'), 'frontend/dist'),
         (str(root / 'backend' / 'migrations'), 'backend/migrations')]
binaries = []
hiddenimports = ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'sqlalchemy.dialects.sqlite',
                 'keyring.backends.Windows', 'pystray._win32']
for package in ['tzdata', 'windows_toasts']:
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ['alembic.runtime.migration', 'alembic.operations', 'alembic.ddl.sqlite']
a = Analysis([str(root / 'scripts' / 'launcher.py')], pathex=[str(root / 'backend'), str(root / 'scripts')],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='OfferPilot', console=False)
coll = COLLECT(exe, a.binaries, a.datas, name='OfferPilot')
