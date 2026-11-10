import io
import json
import zipfile


def test_all_backend_versions_agree(client, app):
    version = app.version
    assert client.get('/api/v1/health').json()['data']['version'] == version
    assert client.get('/api/v1/diagnostics').json()['data']['version'] == version
    tools = app.state.services.data_tools
    artifact = tools.backup(False)
    path, _ = tools.artifact(artifact['artifact_id'])
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        assert json.loads(archive.read('manifest.json'))['app_version'] == version


def test_supported_revisions_include_all_shipped_databases():
    from app.persistence import SUPPORTED_REVISIONS, SCHEMA_REVISION
    assert {'0001_p0', '0002_applications', '0003_parse_cache', '0004_event_details', SCHEMA_REVISION} <= set(SUPPORTED_REVISIONS)
