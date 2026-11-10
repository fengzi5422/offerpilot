"""Keep immutable versioned installers, portable bundles and SHA256 manifests."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import zipfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def archive_release(dist,version,commit):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.\d+)?',version):
        raise ValueError('Invalid release version')
    dist=Path(dist).resolve()
    installer,program=dist/'OfferPilot-Setup.exe',dist/'OfferPilot'
    if not installer.is_file() or not (program/'OfferPilot.exe').is_file():
        raise FileNotFoundError('Build both installer and portable program first')
    releases=dist/'releases'
    releases.mkdir(exist_ok=True)
    target=releases/version
    if target.exists():
        raise FileExistsError('This release is already archived; keep it and use a new version')
    pending=releases/('.pending-'+str(uuid.uuid4()))
    pending.mkdir()
    setup=pending/f'OfferPilot-Setup-{version}.exe'
    portable=pending/f'OfferPilot-{version}-windows-x64.zip'
    shutil.copy2(installer,setup)
    with zipfile.ZipFile(portable,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(program.rglob('*')):
            if path.is_file():
                if not path.resolve().is_relative_to(program.resolve()):
                    raise ValueError('Portable bundle contains an external file')
                archive.write(path,path.relative_to(dist).as_posix())
    manifest={'version':version,'source_commit':commit,'platform':'windows-x64',
        'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'executable_sha256':digest(program/'OfferPilot.exe'),
        'files':[{'name':path.name,'size':path.stat().st_size,'sha256':digest(path)} for path in (setup,portable)]}
    (pending/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(pending,target)
    return target


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--dist',type=Path,default=Path('dist'))
    parser.add_argument('--version',required=True)
    parser.add_argument('--commit')
    options=parser.parse_args()
    commit=options.commit or subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    print(archive_release(options.dist,options.version,commit))
