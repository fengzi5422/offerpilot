from datetime import datetime, timedelta, timezone
from app.main import create_app
from app.persistence import Email
from test_core import eml, import_mail


def test_ingest_is_durable_and_does_not_run_parser_until_worker(tmp_path, monkeypatch):
    from app import mail
    services=create_app(tmp_path,start_workers=False).state.services
    parse=mail.classify_and_parse
    def fail(*args):
        raise RuntimeError('parser unavailable')
    monkeypatch.setattr(mail,'classify_and_parse',fail)
    message=services.mail.ingest(eml('请于2030年1月2日18:00前（北京时间）上传材料。'))
    assert services.mail.get(message)['state']=='new'
    assert services.tasks.list()==[]
    monkeypatch.setattr(mail,'classify_and_parse',parse)
    services.db.engine.dispose()
    services=create_app(tmp_path,start_workers=False).state.services
    services.mail.parse_pending()
    assert services.mail.get(message)['state']=='parsed'
    assert len(services.tasks.list())==1


def test_parser_clears_temporary_fulltext_but_retains_action_evidence(tmp_path):
    from app.persistence import MailCache
    services=create_app(tmp_path,start_workers=False).state.services
    content='不需长期保存的正文。'*2500+'\n请于2030年1月2日18:00前（北京时间）上传材料。'
    message=services.mail.ingest(eml(content))
    with services.db.read() as session:
        assert session.get(MailCache,message).content==content
        assert len(session.get(Email,message).text_excerpt)<=4096
    services.mail.parse_pending()
    with services.db.read() as session:
        assert session.get(MailCache,message) is None
    task=services.tasks.list()[0]
    assert '上传材料' in task['source']['evidence'][0]['quote']


def test_truncated_eml_never_automatically_creates_a_task(client):
    content='请于2030年1月2日18:00前（北京时间）上传材料。\n'+'正文。'*40000
    message=import_mail(client,eml(content))
    assert message['text_truncated'] and message['state']=='review_needed'
    assert any('TEXT_TRUNCATED' in c['issues'] for c in message['candidates'])
    assert client.get('/api/v1/tasks').json()['data']==[]


def test_unknown_charset_and_broken_bytes_remain_inspectable_without_inventing_time(client):
    raw=(b'From: hr@example.test\r\nSubject: Broken encoding\r\n'
         b'Content-Type: text/plain; charset=not-a-real-charset\r\n\r\n\xff\xfe')
    message=import_mail(client,raw)
    assert message['state']=='parsed' and '\ufffd' in message['text_excerpt']
    assert message['candidates']==[]
    assert client.get('/api/v1/tasks').json()['data']==[]


def test_expired_parser_cache_becomes_visible_failure_without_blocking_other_mail(tmp_path):
    from app.persistence import MailCache
    now=[datetime(2030,1,1,tzinfo=timezone.utc)]
    services=create_app(tmp_path,start_workers=False,clock=lambda:now[0]).state.services
    expired=services.mail.ingest(eml('待处理正文'))
    now[0]+=timedelta(days=8)
    services.mail.cleanup()
    with services.db.read() as session:
        assert session.get(MailCache,expired) is None
    assert services.mail.get(expired)['state']=='parse_failed'
    fresh=services.mail.ingest(eml('请于2030年2月2日18:00前（北京时间）上传材料。'))
    services.mail.parse_pending()
    assert services.mail.get(fresh)['state']=='parsed'


def test_background_backup_is_queued_and_resumed_after_restart(tmp_path):
    services=create_app(tmp_path,start_workers=False).state.services
    job=services.jobs.enqueue('backup',{'include_evidence':False})
    assert services.jobs.get(job)['status']=='queued'
    services.jobs.update(job,status='running')
    services.db.engine.dispose()
    services=create_app(tmp_path,start_workers=False).state.services
    assert services.jobs.get(job)['status']=='queued'
    services.jobs.run_once()
    detail=services.jobs.get(job)
    assert detail['status']=='succeeded'
    assert services.data_tools.artifact(detail['result']['artifact_id'])[0].is_file()


def test_api_returns_before_export_execution_and_failure_is_queryable(client, app, monkeypatch):
    calls=[]
    def fail(*args):
        calls.append(True)
        raise OSError('private error text')
    monkeypatch.setattr(app.state.services.data_tools,'export',fail)
    response=client.post('/api/v1/exports',json={'format':'json'})
    assert response.status_code==202 and calls==[]
    job=response.json()['data']['job_id']
    assert client.get('/api/v1/jobs/'+job).json()['data']['status']=='queued'
    app.state.services.jobs.run_once()
    detail=client.get('/api/v1/jobs/'+job).json()['data']
    assert detail['status']=='failed' and detail['error_code']=='JOB_FAILED'
    assert 'private error text' not in str(detail)


def test_privacy_purge_cancels_queued_import_and_deletes_its_raw_input(tmp_path):
    services=create_app(tmp_path,start_workers=False).state.services
    name=services.data_tools.stage_input(eml('PRIVATE PENDING BODY'))
    job=services.jobs.enqueue('eml_import',{'file':name})
    preview=services.data_tools.privacy_preview('mail_cache')
    services.data_tools.purge('mail_cache',preview['preview_token'])
    services.jobs.run_once()
    assert services.jobs.get(job)['status']=='interrupted'
    assert not (services.data_tools.staging_dir/name).exists()
    assert services.mail.list()==[]


def test_failed_parser_can_retry_full_cache_and_expired_source_can_be_reimported(tmp_path,monkeypatch):
    from app import mail
    from app.persistence import MailCache
    services=create_app(tmp_path,start_workers=False).state.services
    raw=eml('请于2030年1月2日18:00前（北京时间）上传材料。')
    message=services.mail.ingest(raw)
    original=mail.classify_and_parse
    monkeypatch.setattr(mail,'classify_and_parse',lambda *a:(_ for _ in ()).throw(RuntimeError()))
    services.mail.parse_pending()
    monkeypatch.setattr(mail,'classify_and_parse',original)
    failed=services.mail.get(message)
    services.mail.reparse(message,failed['version'])
    services.mail.parse_pending()
    assert len(services.tasks.list())==1
    with services.db.tx() as session:
        record=session.get(Email,message)
        record.state='parse_failed'
        record.classification={'error_code':'SOURCE_CACHE_EXPIRED'}
        assert session.get(MailCache,message) is None
    assert services.mail.ingest(raw)==message
    assert services.mail.get(message)['state']=='new'
    services.mail.parse_pending()
    assert len(services.tasks.list())==1


def test_reparse_handler_and_completion_are_atomic_across_a_crash(tmp_path,monkeypatch):
    from app import mail
    services=create_app(tmp_path,start_workers=False).state.services
    message=services.mail.ingest(eml('请明天上传材料。'))
    original=mail.classify_and_parse
    monkeypatch.setattr(mail,'classify_and_parse',lambda *a:(_ for _ in ()).throw(RuntimeError()))
    services.mail.parse_pending()
    monkeypatch.setattr(mail,'classify_and_parse',original)
    failed=services.mail.get(message)
    job=services.jobs.enqueue('reparse',{'message_id':message,'expected_version':failed['version']})
    update=services.jobs.update
    def crash(*args,**kwargs):
        if kwargs.get('status')=='succeeded':
            raise SystemExit('process stopped after handler')
        return update(*args,**kwargs)
    monkeypatch.setattr(services.jobs,'update',crash)
    import pytest
    with pytest.raises(SystemExit):
        services.jobs.run_once()
    services.db.engine.dispose()
    services=create_app(tmp_path,start_workers=False).state.services
    services.jobs.run_once()
    assert services.jobs.get(job)['status']=='succeeded'
    services.mail.parse_pending()
    assert services.mail.get(message)['state']=='review_needed'


def test_shutdown_stops_late_workers_writing_and_preserves_restart_receipt(tmp_path):
    import threading
    import pytest
    from fastapi.testclient import TestClient
    from app.domain import AppError
    started,release=threading.Event(),threading.Event()
    application=create_app(tmp_path,start_workers=True,shutdown_timeout=.05)
    services=application.state.services
    def blocked(payload):
        started.set()
        assert release.wait(5)
        return {}
    services.jobs.handlers['export']=blocked
    try:
        with TestClient(application):
            job=services.jobs.enqueue('export',{'format':'json'})
            assert started.wait(3)
        with pytest.raises(AppError) as exc:
            services.tasks.create({'title':'late write','kind':'custom','priority':2,'temporal':{}})
        assert exc.value.code=='APP_STOPPING'
    finally:
        release.set()
    restarted=create_app(tmp_path,start_workers=False).state.services
    assert restarted.jobs.get(job)['status']=='queued'
    restarted.jobs.run_once()
    assert restarted.jobs.get(job)['status']=='succeeded'
