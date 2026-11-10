import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0,str(Path(__file__).parent))
from archive_release import archive_release, digest


def test_archived_release_is_complete_and_never_overwritten(tmp_path):
    (tmp_path/'OfferPilot/_internal/frontend/dist').mkdir(parents=True)
    (tmp_path/'OfferPilot-Setup.exe').write_bytes(b'installer-v1')
    (tmp_path/'OfferPilot/OfferPilot.exe').write_bytes(b'program-v1')
    (tmp_path/'OfferPilot/_internal/frontend/dist/index.html').write_text('UI')
    result=archive_release(tmp_path,'0.2.1','source-commit')
    manifest=json.loads((result/'manifest.json').read_text())
    assert manifest['source_commit']=='source-commit'
    for entry in manifest['files']:
        assert digest(result/entry['name'])==entry['sha256']
    with zipfile.ZipFile(result/'OfferPilot-0.2.1-windows-x64.zip') as bundle:
        assert bundle.read('OfferPilot/_internal/frontend/dist/index.html')==b'UI'
    before=(result/'OfferPilot-Setup-0.2.1.exe').read_bytes()
    (tmp_path/'OfferPilot-Setup.exe').write_bytes(b'installer-v2')
    with pytest.raises(FileExistsError):
        archive_release(tmp_path,'0.2.1','new-commit')
    assert (result/'OfferPilot-Setup-0.2.1.exe').read_bytes()==before
