"""Synchronize build-time version fields without changing archived releases."""
import argparse
import json
from pathlib import Path
import re
import tomllib


def versions(root):
    root = Path(root)
    values = {'project': tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']}
    lock = tomllib.loads((root / 'uv.lock').read_text())
    values['uv'] = next(p['version'] for p in lock['package'] if p['name'] == 'offerpilot')
    for name in ('package.json', 'package-lock.json'):
        data = json.loads((root / 'frontend' / name).read_text())
        values[name] = data['version']
        if 'packages' in data:
            values['npm-root'] = data['packages']['']['version']
    patterns = {
        'backend/app/version.py': r"VERSION = '([^']+)'",
        'frontend/src/App.vue': r'<span>v([^<]+)</span>',
        'packaging/OfferPilot.iss': r'AppVersion=([^\r\n]+)',
        'scripts/launcher.py': r"'version':'([^']+)'",
        'scripts/desktop_runtime.py': r"OfferPilotDesktop/([^']+)",
    }
    for file, pattern in patterns.items():
        match = re.search(pattern, (root / file).read_text(encoding='utf-8'))
        if not match:
            raise ValueError('Missing version: ' + file)
        values[file] = match[1]
    return values


def synchronize(root, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.\d+)?', version):
        raise ValueError('Expected x.y.z or x.y.z-rc.n')
    # Python metadata uses PEP 440; product labels retain readable SemVer.
    python_version = version.replace('-rc.', 'rc')
    previous = versions(root)
    replacements = {
        'pyproject.toml': ('version = "' + previous['project'] + '"', 'version = "' + python_version + '"'),
        'uv.lock': ('name = "offerpilot"\nversion = "' + previous['uv'] + '"',
                    'name = "offerpilot"\nversion = "' + python_version + '"'),
        'backend/app/version.py': ("VERSION = '" + previous['backend/app/version.py'] + "'", "VERSION = '" + version + "'"),
        'frontend/src/App.vue': ('<span>v' + previous['frontend/src/App.vue'] + '</span>', '<span>v' + version + '</span>'),
        'packaging/OfferPilot.iss': ('AppVersion=' + previous['packaging/OfferPilot.iss'], 'AppVersion=' + version),
        'scripts/launcher.py': ("'version':'" + previous['scripts/launcher.py'] + "'", "'version':'" + version + "'"),
        'scripts/desktop_runtime.py': ('OfferPilotDesktop/' + previous['scripts/desktop_runtime.py'], 'OfferPilotDesktop/' + version),
    }
    for file, (old, new) in replacements.items():
        path = root / file
        text = path.read_text(encoding='utf-8')
        if old not in text:
            raise ValueError('Cannot update version: ' + file)
        path.write_text(text.replace(old, new), encoding='utf-8')
    for name in ('package.json', 'package-lock.json'):
        path = root / 'frontend' / name
        data = json.loads(path.read_text())
        data['version'] = version
        if 'packages' in data:
            data['packages']['']['version'] = version
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--set', dest='version')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.version:
        synchronize(root, args.version)
    print(json.dumps(versions(root), ensure_ascii=False, indent=2))
