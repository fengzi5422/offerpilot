from datetime import datetime, timezone

import pytest

from app.main import create_app
from test_core import eml, import_mail, task_body


def test_labeled_event_block_keeps_time_range_location_and_meeting_link(client):
    mail=import_mail(client,eml('面试安排如下：\n面试时间：2030年1月2日 15:00-16:00（北京时间）\n'
        '面试地点：上海市示例路100号3楼\n会议链接：https://meet.example.test/room'))
    assert len(mail['candidates'])==1
    candidate=mail['candidates'][0]
    assert candidate['temporal']['starts_at']=='2030-01-02T07:00:00Z'
    assert candidate['temporal']['ends_at']=='2030-01-02T08:00:00Z'
    assert candidate['event_details']['location']=='上海市示例路100号3楼'
    assert candidate['event_details']['meeting_url']=='https://meet.example.test/room'
    assert len(client.get('/api/v1/tasks').json()['data'])==1


def test_labeled_dates_belong_to_their_action_not_another_deadline(client):
    mail=import_mail(client,eml('请回复确认出席。\n回复截止：2030年1月1日18:00（北京时间）\n'
        '面试时间：2030年1月2日15:00（北京时间）\n地点：上海市示例路100号'))
    drafts={c['kind']:c for c in mail['candidates']}
    assert drafts['reply']['temporal']['due_at']=='2030-01-01T10:00:00Z'
    assert drafts['interview']['temporal']['starts_at']=='2030-01-02T07:00:00Z'
    assert drafts['reply']['event_details']['location']==''
    assert drafts['interview']['event_details']['location']=='上海市示例路100号'


def test_event_details_edit_and_restore_roundtrip(client,app):
    body=task_body()
    body['event_details']={'location':'上海市示例路100号','meeting_url':'https://meet.example.test/room'}
    response=client.post('/api/v1/tasks',json=body)
    assert response.status_code==201,response.text
    task=response.json()['data']
    changed=client.patch('/api/v1/tasks/'+task['id'],json={'expected_version':task['version'],
        'changes':{'event_details':{'location':'线上','meeting_url':'https://meet.example.test/new'}}})
    assert changed.status_code==200,changed.text
    assert changed.json()['data']['schedule_revision']==task['schedule_revision']
    s=app.state.services
    backup=s.data_tools.backup(True)
    path,_=s.data_tools.artifact(backup['artifact_id'])
    validated=s.data_tools.validate_restore(path.read_bytes())
    preview=s.data_tools.restore_preview(validated['restore_id'])
    s.data_tools.apply_restore(validated['restore_id'],preview['confirmation_token'])
    assert s.tasks.get(task['id'])['event_details']['location']=='线上'


@pytest.mark.parametrize('url',['javascript:alert(1)','file:///C:/private','data:text/html,unsafe'])
def test_event_links_reject_active_and_local_schemes(client,url):
    body=task_body()
    body['event_details']={'location':'','meeting_url':url}
    assert client.post('/api/v1/tasks',json=body).status_code==422


def test_yearless_date_uses_configured_default_and_keeps_clock(client):
    mail=import_mail(client,eml('请于11月12日14:30参加面试。'))
    temporal=mail['candidates'][0]['temporal']
    assert not temporal['needs_confirmation']
    assert temporal['starts_at']=='2026-11-12T06:30:00Z'


def services(tmp_path):
    return create_app(tmp_path,start_workers=False,clock=lambda:datetime(2029,1,1,tzinfo=timezone.utc)).state.services


def test_change_resolution_updates_same_task_and_suppresses_old_reminders_atomically(client,app):
    from app.persistence import Reminder
    from sqlalchemy import select
    task=client.post('/api/v1/tasks',json=task_body()).json()['data']
    mail=import_mail(client,eml('原定面试改期至2030年1月3日16:00（北京时间）。'))
    candidate=mail['candidates'][0]
    body={'expected_version':mail['version'],'candidate_id':candidate['candidate_id'],'task_id':task['id'],
          'expected_task_version':task['version'],'action':'reschedule','temporal':candidate['temporal']}
    import uuid
    headers={'Idempotency-Key':str(uuid.uuid4())}
    response=client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body,headers=headers)
    assert response.status_code==200,response.text
    assert client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body,headers=headers).json()['data']==response.json()['data']
    updated=client.get('/api/v1/tasks/'+task['id']).json()['data']
    assert updated['starts_at']=='2030-01-03T08:00:00Z'
    assert len(client.get('/api/v1/tasks').json()['data'])==1
    with app.state.services.db.read() as session:
        old=list(session.scalars(select(Reminder).where(Reminder.task_id==task['id'],Reminder.schedule_revision==1)))
        assert old and all(r.state=='suppressed' for r in old)
    assert client.get('/api/v1/emails/'+mail['id']).json()['data']['state']=='parsed'


def test_change_resolution_rolls_back_task_and_review_if_replan_fails(client,app,monkeypatch):
    from app.domain import AppError
    task=client.post('/api/v1/tasks',json=task_body()).json()['data']
    mail=import_mail(client,eml('面试改期至2030年1月3日16:00（北京时间）。'))
    def fail(*args): raise AppError('TEST_FAILURE','测试中断',503)
    monkeypatch.setattr(app.state.services.tasks,'rebuild',fail)
    response=client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json={
        'expected_version':mail['version'],'candidate_id':mail['candidates'][0]['candidate_id'],
        'task_id':task['id'],'expected_task_version':task['version'],'action':'reschedule',
        'temporal':mail['candidates'][0]['temporal']})
    assert response.status_code==503,response.text
    assert client.get('/api/v1/tasks/'+task['id']).json()['data']['version']==task['version']
    assert client.get('/api/v1/emails/'+mail['id']).json()['data']['candidates'][0]['state']=='open'


def test_cancel_change_and_stale_task_version(client):
    task=client.post('/api/v1/tasks',json=task_body()).json()['data']
    mail=import_mail(client,eml('原定面试取消。'))
    body={'expected_version':mail['version'],'candidate_id':mail['candidates'][0]['candidate_id'],
          'task_id':task['id'],'expected_task_version':0,'action':'cancel'}
    response=client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body)
    assert response.status_code==409,response.text
    body['expected_task_version']=task['version']
    assert client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body).status_code==200
    assert client.get('/api/v1/tasks/'+task['id']).json()['data']['status']=='cancelled'


def test_week_calendar_known_overlap_adjacent_and_unknown_duration(client):
    def add(start,end=None):
        body=task_body()
        body['temporal'].update(starts_at=start,ends_at=end)
        response=client.post('/api/v1/tasks',json=body)
        assert response.status_code==201,response.text
        return response.json()['data']['id']
    a=add('2030-01-02T07:00:00Z','2030-01-02T08:00:00Z')
    b=add('2030-01-02T07:30:00Z','2030-01-02T08:30:00Z')
    c=add('2030-01-02T08:30:00Z','2030-01-02T09:00:00Z')
    d=add('2030-01-02T07:15:00Z')
    response=client.get('/api/v1/calendar?start=2030-01-02&end=2030-01-03&timezone=Asia/Shanghai')
    assert response.status_code==200,response.text
    data=response.json()['data']
    pairs={frozenset(x['task_ids']):x['kind'] for x in data['conflicts']}
    assert pairs[frozenset([a,b])]=='overlap'
    assert frozenset([b,c]) not in pairs
    assert pairs[frozenset([a,d])]=='possible'
    assert d in {x['id'] for x in data['uncertain_entries']}
    assert len(data['entries'])==4


def test_calendar_local_midnight_and_invalid_range(client):
    body=task_body()
    body['temporal']['starts_at']='2030-01-01T17:00:00Z'
    task=client.post('/api/v1/tasks',json=body).json()['data']
    data=client.get('/api/v1/calendar?start=2030-01-02&end=2030-01-03&timezone=Asia/Shanghai').json()['data']
    assert data['entries'][0]['id']==task['id']
    assert client.get('/api/v1/calendar?start=2030-01-01&end=2031-01-01').status_code==422


def test_meeting_link_and_location_do_not_introduce_false_clock_matches(client):
    mail=import_mail(client,eml('面试时间：2030年1月2日 15:00（UTC+08:00）\n'
        '地点：上海市示例路100号（门禁时间08:00）\n会议链接：https://meet.example.test:8443/room'))
    assert mail['candidates'][0]['temporal']['starts_at']=='2030-01-02T07:00:00Z'


def test_invalid_parsed_meeting_link_does_not_fail_an_otherwise_valid_event(client):
    mail=import_mail(client,eml('面试时间：2030年1月2日 15:00（北京时间）\n'
        '会议链接：https://user:secret@meet.example.test/room'))
    assert mail['state']!='parse_failed'
    assert mail['candidates'][0]['event_details']['meeting_url']==''


def test_next_week_proposal_uses_message_date_but_requires_confirmation(client):
    mail=import_mail(client,eml('请于下周三下午3点参加面试。'))
    temporal=mail['candidates'][0]['temporal']
    assert temporal['needs_confirmation']
    assert temporal['candidates'][0]['starts_at']=='2026-11-18T07:00:00Z'


def test_nonchange_or_other_mail_candidate_cannot_cancel_task(client):
    task=client.post('/api/v1/tasks',json=task_body()).json()['data']
    mail=import_mail(client,eml('请于11月12日14:30参加面试。'))
    changed=import_mail(client,eml('原定面试取消。'))
    body={'expected_version':mail['version'],'candidate_id':mail['candidates'][0]['candidate_id'],
          'task_id':task['id'],'expected_task_version':task['version'],'action':'cancel'}
    assert client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body).status_code==409
    body['candidate_id']=changed['candidates'][0]['candidate_id']
    assert client.post('/api/v1/emails/'+mail['id']+'/resolve-change',json=body).status_code==409
    assert client.get('/api/v1/tasks/'+task['id']).json()['data']['status']=='pending'


def test_calendar_crossday_and_dateonly_never_invent_conflict(client):
    body=task_body()
    body['temporal'].update(starts_at='2030-01-01T15:00:00Z',ends_at='2030-01-02T16:00:00Z')
    task=client.post('/api/v1/tasks',json=body).json()['data']
    body['temporal']={'kind':'date_only','precision':'date','local_date':'2030-01-02','needs_confirmation':True}
    dateonly=client.post('/api/v1/tasks',json=body).json()['data']
    data=client.get('/api/v1/calendar?start=2030-01-02&end=2030-01-03&timezone=Asia/Shanghai').json()['data']
    assert [x['id'] for x in data['entries']]==[task['id']]
    assert [x['id'] for x in data['uncertain_entries']]==[dateonly['id']]
    assert not data['conflicts']
    assert client.get('/api/v1/calendar?start=2030-01-02&end=2030-01-03&timezone=Invalid/Zone').status_code==422


def test_csv_includes_event_fields_with_spreadsheet_formula_protection(app):
    import csv
    import io
    s=app.state.services
    body=task_body()
    body['event_details']={'location':'=unsafe()','meeting_url':'https://meet.example.test/room'}
    body['temporal']['ends_at']='2030-01-02T08:00:00Z'
    s.tasks.create(body)
    artifact=s.data_tools.export('csv',['tasks'])
    path,_=s.data_tools.artifact(artifact['artifact_id'])
    row=next(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
    assert row['location']=="'=unsafe()"
    assert row['meeting_url']=='https://meet.example.test/room'
    assert row['ends_at_utc']=='2030-01-02T08:00:00Z'


@pytest.mark.parametrize('prefix',['','请于2030年1月1日18:00前回复确认出席（北京时间）\n'])
def test_prefixed_location_before_event_time_belongs_to_event(client,prefix):
    mail=import_mail(client,eml(prefix+'面试地点：上海市示例路100号\n面试时间：2030年1月2日15:00（北京时间）'))
    drafts={c['kind']:c for c in mail['candidates']}
    assert drafts['interview']['event_details']['location']=='上海市示例路100号'
    assert drafts['interview']['temporal']['starts_at']=='2030-01-02T07:00:00Z'
    if prefix:
        assert drafts['reply']['event_details']['location']==''


def test_range_end_rejects_nonexistent_and_ambiguous_local_clock():
    from app.mail import parse_temporal
    for text in ['2030年3月31日00:30-01:30','2030年10月27日00:30-01:30']:
        value=parse_temporal(text,'interview','Europe/London')
        assert value['ends_at'] is None
        assert 'INVALID_TIME_RANGE' in value['reason_codes']


def test_reparse_existing_source_action_requires_linking_instead_of_duplicate_creation(client,app):
    from sqlalchemy import select
    from app.persistence import Email,Candidate
    mail=import_mail(client,eml('面试安排如下：\n面试时间：2030年1月2日15:00（北京时间）\n面试地点：上海市示例路100号'))
    s=app.state.services
    tasks=s.tasks.list()
    assert len(tasks)==1
    with s.db.tx() as session:
        record=session.get(Email,mail['id'])
        record.classification={**record.classification,'rule_version':'rules-v1'}
        old=session.scalar(select(Candidate).where(Candidate.message_id==mail['id']))
        old.action_key+=':legacy-span'
    s.mail.reparse(mail['id'],mail['version'])
    updated=s.mail.get(mail['id'])
    proposal=next(c for c in updated['candidates'] if c['state']=='open')
    assert 'CHANGE_REQUIRES_REVIEW' in proposal['issues']
    response=client.post('/api/v1/emails/'+mail['id']+'/review',json={'expected_version':updated['version'],
        'decisions':[{'candidate_id':proposal['candidate_id'],'action':'accept'}]})
    assert response.status_code==409
    assert len(s.tasks.list())==1


def test_reparse_does_not_revive_ignored_span_or_hide_another_event(client,app):
    from sqlalchemy import select
    from app.persistence import Email,Candidate
    text=('请于2030年1月1日18:00前回复确认出席（北京时间）。\n'
          '面试安排如下：\n面试时间：2030年1月2日15:00（北京时间）\n面试地点：示例楼\n'
          '面试时间：2030年1月5日15:00（北京时间）')
    mail=import_mail(client,eml(text))
    s=app.state.services
    with s.db.tx() as session:
        record=session.get(Email,mail['id'])
        record.classification={**record.classification,'rule_version':'rules-v1'}
        interviews=list(session.scalars(select(Candidate).where(Candidate.message_id==mail['id'])))
        interviews=sorted([c for c in interviews if c.candidate['kind']=='interview'],
                          key=lambda c:c.candidate['temporal']['starts_at'])
        ignored=interviews[0]
        ignored.state,ignored.action_key='ignored',ignored.action_key+':legacy'
        draft=dict(ignored.candidate)
        start=text.index('面试时间：2030年1月2日')
        quote='面试时间：2030年1月2日15:00（北京时间）'
        draft['evidence']=[{'start':start,'end':start+len(quote),'quote':quote}]
        ignored.candidate=draft
        session.delete(interviews[1])
    s.mail.reparse(mail['id'],mail['version'])
    proposals=[c for c in s.mail.get(mail['id'])['candidates'] if c['state']=='open']
    assert len(proposals)==1
    assert proposals[0]['temporal']['starts_at']=='2030-01-05T07:00:00Z'
