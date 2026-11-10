from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.mail import parse_temporal
from app.persistence import Candidate, Email, Task
from test_core import eml, import_mail, task_body


@pytest.mark.parametrize('clock',['8:30','8：30','08:30'])
def test_screenshot_month_day_clock_becomes_an_immediate_event(clock):
    temporal=parse_temporal(f'1、面试签到时间：11月16日（周一）{clock}','interview')
    assert temporal['starts_at']=='2026-11-16T00:30:00Z'
    assert temporal['needs_confirmation'] is False
    assert temporal['candidates']==[]


def test_configured_year_is_persisted_and_explicit_year_wins(client):
    settings=client.get('/api/v1/settings').json()['data']
    assert settings['default_event_year']==2026
    changed=client.patch('/api/v1/settings',json={'expected_version':settings['version'],
        'changes':{'default_event_year':2027}})
    assert changed.status_code==200,changed.text
    mail=import_mail(client,eml('面试时间：11月16日8:30'))
    assert mail['candidates'][0]['temporal']['starts_at']=='2027-11-16T00:30:00Z'
    assert mail['candidates'][0]['state']=='accepted'
    explicit=import_mail(client,eml('面试时间：2030年11月16日8:30'))
    assert explicit['candidates'][0]['temporal']['starts_at']=='2030-11-16T00:30:00Z'
    for invalid in [True,1999,2100,'2028']:
        settings=client.get('/api/v1/settings').json()['data']
        assert client.patch('/api/v1/settings',json={'expected_version':settings['version'],
            'changes':{'default_event_year':invalid}}).status_code==422


def test_default_year_ignores_mail_year_and_preserves_date_only_and_uncertainty():
    value=parse_temporal('面试11月16日8:30','interview',header_date='2025-12-31T00:00:00Z')
    assert value['starts_at']=='2026-11-16T00:30:00Z'
    date_only=parse_temporal('面试11月16日','interview')
    assert date_only['local_date']=='2026-11-16'
    assert date_only['starts_at'] is None and date_only['needs_confirmation']
    for text in ['面试2月30日8:30','面试11月16日8:30或11月17日9:00','收到后48小时内完成测评']:
        assert parse_temporal(text,'interview')['needs_confirmation']
    interval=parse_temporal('面试11月16日8:30-9:30','interview')
    assert interval['starts_at']=='2026-11-16T00:30:00Z'
    assert interval['ends_at']=='2026-11-16T01:30:00Z'


@pytest.mark.parametrize('text',[
    '面试时间：11月16日8:30 EST',
    '面试时间：11月16日8:30（美国东部时间）',
    '面试时间：11月16日8:30 UTC+09:00',
    '面试时间：11月16日或17日8:30',
    '面试时间：2026年11月16日或11月17日8:30',
    '面试时间：11月160日8:30',
    '面试时间：2026年11月160日8:30（北京时间）',
])
def test_defaults_do_not_silently_accept_conflicting_or_invalid_evidence(text):
    assert parse_temporal(text,'interview')['needs_confirmation']


def test_explicit_gmt_offset_wins_over_default_region_and_iso_day_suffix_works():
    value=parse_temporal('面试时间11月16日8:30 GMT+8','interview','America/New_York')
    assert value['starts_at']=='2026-11-16T00:30:00Z'
    assert not value['needs_confirmation']
    iso_day=parse_temporal('面试时间2026-11-16日8:30','interview')
    assert iso_day['starts_at']=='2026-11-16T00:30:00Z'
    assert not iso_day['needs_confirmation']


def test_events_mails_and_tasks_are_chronological_before_pagination(client):
    later=import_mail(client,eml('面试时间：11月20日9:00\n请于11月12日8:30回复确认出席。'))
    assert [c['kind'] for c in later['candidates']]==['reply','interview']
    earlier=import_mail(client,eml('面试时间：11月10日9:00'))
    import_mail(client,eml('面试时间稍后通知，请准备面试。'))
    data=client.get('/api/v1/emails?limit=1').json()
    assert data['data'][0]['id']==earlier['id']
    following=client.get('/api/v1/emails?limit=1&cursor='+data['next_cursor']).json()
    assert following['data'][0]['id']==later['id']
    tasks=client.get('/api/v1/tasks').json()['data']
    assert [t['starts_at'] or t['due_at'] for t in tasks]==[
        '2026-11-10T01:00:00Z','2026-11-12T00:30:00Z','2026-11-20T01:00:00Z']


def legacy_candidate(services,state='open'):
    message=services.mail.ingest(eml('面试签到时间：11月16日（周一）8:30\n测试状态 '+state))
    with services.db.tx() as session:
        mail=session.get(Email,message)
        mail.state='review_needed'
        mail.classification={'rule_version':'rules-v2'}
        candidate=Candidate(message_id=message,action_key=message+':legacy',state=state,candidate={
            'title':'示例科技 · 参加面试','kind':'interview','priority':2,'company':'示例科技',
            'event_details':{'location':'线上','meeting_url':''},'evidence':[],
            'issues':['YEAR_OR_DATE_MISSING'],'temporal':{
                'kind':'unknown','raw_text':'面试签到时间：11月16日（周一）8:30',
                'timezone':'Asia/Shanghai','timezone_source':'account_default','precision':'unknown',
                'needs_confirmation':True,'reason_codes':['YEAR_OR_DATE_MISSING'],
                'candidates':[{'local_date':'2026-11-16'},{'local_date':'2027-11-16'}]}})
        session.add(candidate)
    return message


def test_legacy_open_year_suggestions_are_backed_up_and_upgraded_once(app):
    services=app.state.services
    message=legacy_candidate(services)
    ignored=legacy_candidate(services,'ignored')
    assert services.mail.upgrade_year_defaults(services.data_tools.year_policy_checkpoint)==1
    candidate=services.mail.get(message)['candidates'][0]
    assert candidate['state']=='accepted'
    assert services.tasks.get(candidate['task_id'])['starts_at']=='2026-11-16T00:30:00Z'
    assert services.mail.get(ignored)['candidates'][0]['state']=='ignored'
    assert list((app.state.data_dir/'backups').glob('before-year-policy-*.zip'))
    assert services.mail.upgrade_year_defaults(services.data_tools.year_policy_checkpoint)==0
    assert len(services.tasks.list())==1


def test_legacy_repair_is_atomic_if_task_creation_fails(app,monkeypatch):
    services=app.state.services
    message=legacy_candidate(services)
    def fail(*args,**kwargs):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(services.tasks,'create',fail)
    with pytest.raises(RuntimeError):
        services.mail.upgrade_year_defaults(services.data_tools.year_policy_checkpoint)
    assert services.mail.get(message)['candidates'][0]['state']=='open'
    with services.db.read() as session:
        assert list(session.scalars(select(Task)))==[]


def test_database_reopen_keeps_year_task_and_reminder_plan(tmp_path):
    from app.main import create_app
    def clock():
        return datetime(2026,11,10,tzinfo=timezone.utc)
    first=create_app(tmp_path,clock=clock,start_workers=False).state.services
    settings=first.settings.get()
    first.settings.patch(settings['version'],{'default_event_year':2028})
    task=first.tasks.create(task_body())
    before=first.tasks.get(task['id'])
    first.db.close()
    second=create_app(tmp_path,clock=clock,start_workers=False).state.services
    try:
        assert second.settings.get()['default_event_year']==2028
        restored=second.tasks.get(task['id'])
        for key in ['id','title','temporal','status','event_details','next_reminder_at','reminders']:
            assert restored[key]==before[key]
    finally:
        second.db.close()
