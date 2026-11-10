import json
from datetime import datetime, timedelta, timezone

import pytest

from app.accounts import QQConnection
from app.ai import AIParser
from app.domain import AppError
from app.mail import normalize_mail, parse_temporal
from test_ai import Transport, config, source


@pytest.mark.parametrize(('clock', 'utc'), [
    ('晚上7点', '11:00'), ('傍晚七点', '11:00'), ('晚间7:00', '11:00'),
    ('19:00', '11:00'), ('7:00 PM', '11:00'), ('下午7时30分', '11:30'),
    ('凌晨12点', '16:00'), ('12:00 AM', '16:00'), ('中午12点', '04:00'),
    ('中午1点', '05:00'),
])
def test_local_dayperiod_is_applied_to_each_clock(clock, utc):
    value = parse_temporal('请11月16日' + clock + '参加面试。', 'interview')
    day = '15' if utc == '16:00' else '16'
    assert value['starts_at'] == f'2026-11-{day}T{utc}:00Z'


@pytest.mark.parametrize(('clocks', 'start', 'end'), [
    ('上午11点到下午1点', '03:00', '05:00'),
    ('下午3点到晚上7点', '07:00', '11:00'),
    ('晚上7点到9点', '11:00', '13:00'),
    ('7:00-9:00 PM', '11:00', '13:00'),
    ('7 PM-9 PM', '11:00', '13:00'),
])
def test_mixed_dayperiod_range(clocks, start, end):
    value = parse_temporal('11月16日面试时间：' + clocks, 'interview')
    assert value['starts_at'] == f'2026-11-16T{start}:00Z'
    assert value['ends_at'] == f'2026-11-16T{end}:00Z'


@pytest.mark.parametrize('label', ['时间:', '时间：'])
def test_hour_only_pm_immediately_after_label(label):
    value = parse_temporal('11月16日面试' + label + '7 PM-9 PM', 'interview')
    assert value['starts_at'] == '2026-11-16T11:00:00Z'
    assert value['ends_at'] == '2026-11-16T13:00:00Z'


def test_ai_quote_cannot_drop_evening_qualifier_from_context():
    mail = source('请参加面试，时间：11月16日晚上7点。')
    draft = AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt,
        'time_quote': '7点'}])).parse(mail, {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T11:00:00Z'


def test_ai_structured_event_json_and_24_hour_fields_are_grounded():
    mail = source('请11月16日晚上7点至9点参加面试。')
    action = {'kind': 'interview', 'quote': mail.text_excerpt, 'time_quote': '11月16日晚上7点至9点',
        'date': '2026-11-16', 'time': '19:00', 'end_date': '2026-11-16', 'end_time': '21:00'}
    transport = Transport([action])
    draft = AIParser(transport).parse(mail, {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T11:00:00Z'
    assert draft['temporal']['ends_at'] == '2026-11-16T13:00:00Z'
    prompt = transport.calls[0][1]['messages'][0]['content']
    assert '19:00' in prompt and '"date"' in prompt and '"time"' in prompt


def test_structured_ai_cannot_invent_date_or_override_explicit_evening():
    mail = source('请11月16日晚上7点参加面试。')
    for fields in ({'date': '2026-11-17', 'time': '19:00'}, {'date': '2026-11-16', 'time': '07:00'}):
        with pytest.raises(AppError) as error:
            AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt, **fields}])).parse(mail, {}, config())
        assert error.value.code == 'AI_OUTPUT_INVALID'


def test_reasoning_truncation_retries_once_with_more_output_budget():
    class Recover(Transport):
        def post_json(self, url, payload, **kwargs):
            self.calls.append((url, payload, kwargs))
            if len(self.calls) == 1:
                return 200, json.dumps({'choices': [{'finish_reason': 'length', 'message': {
                    'content': '', 'reasoning_content': 'private reasoning'}}]}).encode()
            return 200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'actions': [{'kind': 'interview', 'quote': source().text_excerpt}]})}}]}).encode()
    transport = Recover()
    assert AIParser(transport).parse(source(), {}, config())[0]['temporal']['starts_at']
    assert len(transport.calls) == 2
    assert transport.calls[1][1]['max_tokens'] > transport.calls[0][1]['max_tokens']


def test_complete_grounded_json_survives_length_finish_reason():
    raw = json.dumps({'choices': [{'finish_reason': 'length', 'message': {
        'content': json.dumps({'actions': [{'kind': 'interview', 'quote': source().text_excerpt}]})}}]}).encode()
    transport = Transport(raw=raw)
    assert AIParser(transport).parse(source(), {}, config())[0]['temporal']['starts_at']
    assert len(transport.calls) == 1


def test_truncation_recovery_is_bounded_and_partial_events_are_never_saved():
    raw = json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': '{"actions":['}}]}).encode()
    transport = Transport(raw=raw)
    with pytest.raises(AppError) as error:
        AIParser(transport).parse(source(), {}, config())
    assert error.value.code == 'AI_OUTPUT_INCOMPLETE'
    assert len(transport.calls) == 2


def test_native_reasoning_budget_does_not_send_incompatible_temperature():
    transport = Transport()
    AIParser(transport).parse(source(), {}, {**config(), 'model': 'o3-mini'})
    payload = transport.calls[0][1]
    assert payload['max_completion_tokens'] >= 8192
    assert 'max_tokens' not in payload and 'temperature' not in payload


def test_explicit_offset_is_not_mistaken_for_another_clock():
    value = parse_temporal('11月16日晚上7点 +08:00 参加面试', 'interview')
    assert value['starts_at'] == '2026-11-16T11:00:00Z'
    assert value['needs_confirmation'] is False


class SinglePartProtocol:
    def __init__(self, body=b'hello'):
        self.calls, self.body = [], body

    def uid(self, command, *args):
        self.calls.append((command, *args))
        fields = args[1]
        if 'BODYSTRUCTURE' in fields:
            header = b'From: hr@example.test\r\nSubject: Single\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
            size = len(self.body) if self.body else 0
            meta = f'1 (BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" {size} 1) BODY[HEADER] '.encode()
            return 'OK', [(meta + f'{{{len(header)}}}'.encode(), header), b')']
        if '.MIME' in fields:
            return 'OK', [b'1 (UID 1 BODY[1.MIME] NIL)']
        if self.body is None:
            return 'OK', [b'1 (UID 1 BODY[TEXT]<0> NIL)']
        return 'OK', [(b'1 (BODY[TEXT]<0> ' + f'{{{len(self.body)}}}'.encode(), self.body), b')']


def test_singlepart_qq_nil_mime_reads_root_text_without_invalid_section():
    protocol = SinglePartProtocol('请11月16日晚上7点参加面试'.encode())
    result = QQConnection(protocol).fetch_message(1)
    assert '晚上7点' in normalize_mail(result['raw'])['text_excerpt']
    assert not any('.MIME' in call[2] for call in protocol.calls)
    assert any('BODY.PEEK[TEXT]' in call[2] for call in protocol.calls)


def test_singlepart_empty_text_is_not_network_failure():
    result = QQConnection(SinglePartProtocol(None)).fetch_message(1)
    assert normalize_mail(result['raw'])['text_excerpt'] == ''


def test_nonempty_root_nil_body_still_fails_without_skipping():
    protocol = SinglePartProtocol(b'nonempty')
    original = protocol.uid
    def missing(command, *args):
        if 'BODY.PEEK[TEXT]' in args[1]:
            return 'OK', [b'1 (UID 1 BODY[TEXT]<0> NIL)']
        return original(command, *args)
    protocol.uid = missing
    with pytest.raises(AppError) as error:
        QQConnection(protocol).fetch_message(1)
    assert error.value.code == 'MAIL_FETCH_FAILED'


def test_nonempty_root_empty_literal_does_not_advance_sync():
    protocol = SinglePartProtocol(b'nonempty')
    original = protocol.uid
    def empty(command, *args):
        if 'BODY.PEEK[TEXT]' in args[1]:
            return 'OK', [(b'1 (BODY[TEXT]<0> {0}', b''), b')']
        return original(command, *args)
    protocol.uid = empty
    with pytest.raises(AppError) as error:
        QQConnection(protocol).fetch_message(1)
    assert error.value.code == 'MAIL_FETCH_FAILED'


def test_legal_quoted_root_body_is_read_without_failure():
    protocol = SinglePartProtocol(b'hello "world"')
    original = protocol.uid
    def quoted(command, *args):
        if 'BODY.PEEK[TEXT]' in args[1]:
            return 'OK', [b'1 (UID 1 BODY[TEXT]<0> "hello \\"world\\"")']
        return original(command, *args)
    protocol.uid = quoted
    assert normalize_mail(QQConnection(protocol).fetch_message(1)['raw'])['text_excerpt'] == 'hello "world"'


def test_root_protocol_runs_through_sync_ingest_and_event_creation(tmp_path, monkeypatch):
    import imaplib
    import ssl
    from app.main import create_app
    from test_mail_adapter import Vault
    from test_mail_providers import Protocol
    class Root(Protocol):
        def uid(self, command, *args):
            if command == 'FETCH':
                return SinglePartProtocol('示例科技请11月16日晚上7点参加面试'.encode()).uid(command, *args)
            return super().uid(command, *args)
    protocol = Root('imap.qq.com', 993, ssl_context=ssl.create_default_context(), timeout=30)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda *args, **kwargs: protocol)
    services = create_app(tmp_path, credential_store=Vault(), start_workers=False).state.services
    account = services.accounts.create({'provider': 'qq', 'address': 'test@qq.com', 'authorization_code': 'synthetic'})
    job = services.accounts.queue_sync(account['id'])
    services.accounts.run_sync(job, account['id'])
    assert services.jobs.get(job)['status'] == 'succeeded'
    services.mail.parse_pending()
    assert services.tasks.list()[0]['temporal']['starts_at'] == '2026-11-16T11:00:00Z'


def test_connection_test_does_not_clear_unresolved_fetch_error(tmp_path):
    from app.main import create_app
    from app.persistence import Account
    from test_mail_adapter import MailAdapter, Vault
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    services = create_app(tmp_path, mail_adapter=MailAdapter(), credential_store=Vault(),
        start_workers=False, clock=lambda: now).state.services
    account = services.accounts.create({'provider': 'qq', 'address': 'test@qq.com', 'authorization_code': 'synthetic'})
    with services.db.tx() as session:
        stored = session.get(Account, account['id'])
        stored.state, stored.last_error_code = 'network_error', 'MAIL_FETCH_FAILED'
        stored.next_sync_at = int((now + timedelta(minutes=5)).timestamp() * 1000)
    job = services.accounts.queue_sync(account['id'], force=True, test=True)
    services.accounts.run_sync(job, account['id'], test=True)
    assert services.jobs.get(job)['status'] == 'succeeded'
    current = services.accounts.list()[0]
    assert current['last_error_code'] == 'MAIL_FETCH_FAILED'
    assert current['next_sync_at'] is not None


def test_ai_can_correct_open_candidate_from_old_incomplete_clock_parser():
    from app.ai_worker import enrich_open_candidate
    mail = source('请11月16日晚间7点参加面试。')
    suggested = AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt}])).parse(mail, {}, config())[0]
    previous = {**suggested, 'parser_source': 'rules', 'temporal': {**suggested['temporal'],
        'starts_at': '2026-11-15T23:00:00Z', 'raw_text': mail.text_excerpt}, 'issues': []}
    result = enrich_open_candidate(previous, suggested)
    assert result is not None
    assert result['temporal']['starts_at'] == '2026-11-16T11:00:00Z'


def test_ai_can_fill_missing_end_on_same_precise_open_event():
    from app.ai_worker import enrich_open_candidate
    mail = source('请11月16日晚上7点至9点参加面试。')
    suggested = AIParser(Transport([{'kind': 'interview', 'quote': mail.text_excerpt}])).parse(mail, {}, config())[0]
    previous = {**suggested, 'parser_source': 'rules', 'temporal': {**suggested['temporal'], 'ends_at': None}}
    result = enrich_open_candidate(previous, suggested)
    assert result is not None and result['temporal']['ends_at'] == '2026-11-16T13:00:00Z'
