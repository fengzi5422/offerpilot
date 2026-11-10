"""One synthetic cross-service acceptance journey, independent of real user data."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def smoke_module():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'smoke_p3.py'
    assert path.is_file(), 'P3 acceptance runner is missing'
    spec = spec_from_file_location('offerpilot_smoke_p3', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_p3_synthetic_journey_and_reopen(tmp_path):
    result = smoke_module().run(tmp_path / 'fresh-p3')
    assert result['passed'] is True
    assert result['simulation'] is True
    assert result['ai_transport'] == 'local_http_stub'
    assert result['webhook_transport'] == 'injected'
    assert result['checks'] and all(result['checks'].values())
    assert result['ai_http_requests'] >= 2
    assert result['webhook_requests'] >= 2
    assert result['calendar_components'] >= 4


def test_acceptance_runner_refuses_existing_database_without_modifying_it(tmp_path):
    marker = tmp_path / 'data.sqlite'
    marker.write_bytes(b'existing-data-must-not-change')
    with pytest.raises(FileExistsError):
        smoke_module().run(tmp_path)
    assert marker.read_bytes() == b'existing-data-must-not-change'
    assert list(tmp_path.iterdir()) == [marker]


def test_local_ai_http_10_response_is_accepted_as_a_valid_model_reply():
    import json
    from app.outbound import OutboundClient
    with smoke_module().local_model(protocol='HTTP/1.0') as (endpoint, requests):
        payload = {'messages': [{'role': 'user', 'content': json.dumps({
            'subject': '虚构公司', 'body': '请11月16日8:30参加技术交流。'})}]}
        status, content = OutboundClient().post_json(endpoint, payload, allow_local=True, timeout=3)
    assert status == 200
    assert json.loads(content)['choices'][0]['message']['content']
    assert len(requests) == 1
