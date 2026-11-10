import pytest

from app.ai import AIParser
from app.domain import AppError
from test_ai import Transport, config, source


@pytest.mark.parametrize(('text', 'time_quote'), [
    ('请参加面试。\n时间：11月16日  晚上7点', '时间：11月16日 晚上7点'),
    ('请参加面试。\n时间：１１月１６日　晚上７点', '时间:11月16日 晚上7点'),
])
def test_layout_normalization_keeps_original_evidence(text, time_quote):
    action = {'kind': 'interview', 'quote': '请参加面试。', 'time_quote': time_quote,
        'date': '2026-11-16', 'time': '19:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T11:00:00Z'
    for proof in draft['evidence']:
        assert proof['quote'] == text[proof['start']:proof['end']]


def test_repeated_action_quote_is_disambiguated_by_its_time_evidence():
    text = '请参加面试。时间：11月16日19:00。\n请参加面试。时间：11月17日20:00。'
    action = {'kind': 'interview', 'quote': '请参加面试。', 'time_quote': '11月17日20:00',
        'date': '2026-11-17', 'time': '20:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-17T12:00:00Z'
    assert draft['evidence'][0]['start'] == text.rindex('请参加面试。')


def test_two_repeated_actions_with_chinese_dates_remain_distinct():
    text = '请参加面试。时间：十一月十六日晚上七点。\n请参加面试。时间：十一月十七日晚上八点。'
    actions = [{'kind': 'interview', 'quote': '请参加面试。', 'time_quote': quote,
        'date': date, 'time': clock} for quote, date, clock in (
            ('十一月十六日晚上七点', '2026-11-16', '19:00'),
            ('十一月十七日晚上八点', '2026-11-17', '20:00'))]
    drafts = AIParser(Transport(actions)).parse(source(text), {}, config())
    assert len(drafts) == 2 and not drafts.validation_warnings
    assert drafts[0]['temporal']['starts_at'] == '2026-11-16T11:00:00Z'
    assert drafts[1]['temporal']['starts_at'] == '2026-11-17T12:00:00Z'


@pytest.mark.parametrize('text', [
    '请于十一月十六日晚上七点参加面试。',
    'Please attend the interview on November 16, 2026 at 7:00 PM.',
    'Interview: 16 Nov 2026, 7 PM.',
])
def test_correct_structured_date_not_vetoed_by_limited_rule_parser(text):
    action = {'kind': 'interview', 'quote': text, 'time_quote': text,
        'date': '2026-11-16', 'time': '19:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T11:00:00Z'


def test_explicit_chinese_year_overrides_default_year():
    text = '请于二〇二七年十一月十六日晚上七点参加面试。'
    action = {'kind': 'interview', 'quote': text, 'date': '2027-11-16', 'time': '19:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2027-11-16T11:00:00Z'


def test_explicit_cross_day_range_has_both_grounded_clocks():
    text = '请于11月16日19:00至11月17日01:00参加面试。'
    action = {'kind': 'interview', 'quote': text, 'time_quote': '11月16日19:00至11月17日01:00',
        'date': '2026-11-16', 'time': '19:00', 'end_date': '2026-11-17', 'end_time': '01:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T11:00:00Z'
    assert draft['temporal']['ends_at'] == '2026-11-16T17:00:00Z'


def test_cross_day_range_keeps_explicit_timezone_on_both_ends():
    text = '请于11月16日19:00至11月17日01:00（UTC）参加面试。'
    action = {'kind': 'interview', 'quote': text, 'time_quote': text,
        'date': '2026-11-16', 'time': '19:00', 'end_date': '2026-11-17', 'end_time': '01:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['starts_at'] == '2026-11-16T19:00:00Z'
    assert draft['temporal']['ends_at'] == '2026-11-17T01:00:00Z'
    assert draft['temporal']['timezone'] == 'UTC'


def test_one_invalid_action_does_not_drop_other_grounded_actions():
    text = '请11月16日晚上7点参加面试。\n请11月15日18:00提交材料。'
    actions = [
        {'kind': 'interview', 'quote': '请11月16日晚上7点参加面试。', 'date': '2026-11-16', 'time': '19:00'},
        {'kind': 'materials', 'quote': '捏造原文', 'date': '2026-11-15', 'time': '18:00'},
    ]
    drafts = AIParser(Transport(actions)).parse(source(text), {}, config())
    assert len(drafts) == 1 and drafts[0]['kind'] == 'interview'
    assert drafts.validation_warnings == [{'reason_code': 'AI_EVIDENCE_NOT_FOUND', 'field': 'action'}]


def test_invalid_action_type_does_not_drop_other_grounded_actions():
    text = '请11月16日晚上7点参加面试。'
    drafts = AIParser(Transport([{'kind': 'interview', 'quote': text}, None])).parse(source(text), {}, config())
    assert len(drafts) == 1
    assert drafts.validation_warnings == [{'reason_code': 'AI_SCHEMA_INVALID', 'field': 'action'}]


def test_time_disagreement_reports_field_without_leaking_response():
    text = '请11月16日晚上7点参加面试。'
    action = {'kind': 'interview', 'quote': text, 'date': '2026-11-16', 'time': '07:00'}
    with pytest.raises(AppError) as error:
        AIParser(Transport([action])).parse(source(text), {}, config())
    assert error.value.code == 'AI_OUTPUT_INVALID'
    assert error.value.fields == {'reason_code': 'AI_TIME_CONFLICT', 'field': 'time'}


def test_unknown_time_keeps_grounded_action_in_review_instead_of_whole_failure():
    text = '请于下一阶段通知后参加面试。'
    action = {'kind': 'interview', 'quote': text, 'date': '2026-11-16', 'time': '19:00'}
    draft = AIParser(Transport([action])).parse(source(text), {}, config())[0]
    assert draft['temporal']['needs_confirmation']
    assert not draft['temporal']['starts_at']
    assert 'AI_TIME_UNSUPPORTED' in draft['issues']


def test_unrelated_events_cannot_supply_each_others_times():
    text = '请参加面试。时间：11月16日19:00。\n请提交材料。截止时间：11月15日18:00。'
    action = {'kind': 'interview', 'quote': '请参加面试。', 'time_quote': '11月15日18:00',
        'date': '2026-11-15', 'time': '18:00'}
    with pytest.raises(AppError):
        AIParser(Transport([action])).parse(source(text), {}, config())
