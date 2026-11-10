"""Strict allowlist for local operational events and explicitly requested diagnostics."""
import io
import json
import re
import zipfile
from collections import Counter, deque

from sqlalchemy import func, select

from app.domain import iso
from app.persistence import Account, Candidate, Email, Job, SCHEMA_REVISION, Task

EVENTS = {'app.start','app.stop','app.recovery','db.migration','api.request','api.error','worker.failed',
          'mail.sync.start','mail.sync.complete','mail.sync.fail','mail.parsed','mail.ignored','mail.reviewed',
          'parse.review_needed','parse.failed','task.created','task.changed','task.complete','task.cancel','task.reopen',
          'task.reminders_replanned','reminder.claimed','reminder.result','backup.complete','backup.fail',
          'job.start','job.complete','job.fail'}
ERRORS = {'INTERNAL_ERROR','WORKER_FAILED','JOB_FAILED','PARSE_FAILED','SOURCE_CACHE_EXPIRED','MAIL_NETWORK_ERROR',
          'MAIL_AUTH_FAILED','MAIL_SYNC_FAILED','BACKUP_INVALID','BACKUP_SCHEMA_INVALID','FILE_TOO_LARGE',
          'INVALID_EML','STATE_CONFLICT','VERSION_CONFLICT','JOB_INPUT_UNAVAILABLE','NOTIFICATIONS_DISABLED',
          'DELIVERY_TIMEOUT','DELIVERY_EXCEPTION','NOTIFICATION_RESULT_UNKNOWN','NOTIFICATION_UNAVAILABLE',
          'NOTIFICATION_SETUP_FAILED'}
MODULES = {'runtime','database','api','mail','parse','jobs','tasks','reminders','backup','maintenance'}


def safe_event(event, fields):
    if not isinstance(event,str) or event not in EVENTS:
        return None
    result={'event':event}
    for key in ('run_id','request_id','job_id','entity_id'):
        value=fields.get(key)
        if isinstance(value,str) and re.fullmatch(r'[0-9a-f-]{32,36}',value):
            result[key]=value
    stamp=fields.get('timestamp_utc')
    if isinstance(stamp,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|\+00:00)',stamp):
        result['timestamp_utc']=stamp
    result['level']='ERROR' if fields.get('level')=='ERROR' else 'INFO'
    if isinstance(fields.get('module'),str) and fields['module'] in MODULES:
        result['module']=fields['module']
    for key in ('duration_ms','status','count'):
        if type(fields.get(key)) is int and 0 <= fields[key] <= 10**12:
            result[key]=fields[key]
    if isinstance(fields.get('method'),str) and fields['method'] in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'):
        result['method']=fields['method']
    if isinstance(fields.get('state'),str) and fields['state'] in ('sent','failed','delivery_unknown','missed','retry_wait','review_needed','parsed','succeeded','queued'):
        result['state']=fields['state']
    if 'error_code' in fields and fields['error_code']:
        result['error_code']=fields['error_code'] if isinstance(fields['error_code'],str) and fields['error_code'] in ERRORS else 'UNCLASSIFIED_ERROR'
    return result


class Diagnostics:
    def __init__(self, db, clock, data_tools, worker_health):
        self.db,self.clock,self.data_tools,self.worker_health=db,clock,data_tools,worker_health

    def health(self):
        with self.db.read() as session:
            counts={name:session.scalar(select(func.count()).select_from(model)) for name,model in
                    (('tasks',Task),('emails',Email),('reviews',Candidate),('jobs',Job))}
            known={'ready','syncing','paused','auth_required','network_error','removed'}
            states=dict(Counter(state if state in known else 'unknown' for state in session.scalars(select(Account.state))))
        return {'version':'0.2.0','schema_revision':SCHEMA_REVISION,'ready':True,
                'workers':dict(self.worker_health),'database':{'status':'ready','journal_mode':'wal'},
                'counts':counts,'account_states':states,'notification_channel':'windows','ai_enabled':False}

    def export(self):
        records=deque(maxlen=2000)
        for path in sorted((self.db.directory/'logs').glob('backend.log*'),key=lambda p:p.stat().st_mtime):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('r',encoding='utf-8',errors='replace') as stream:
                for line in stream:
                    if len(line)>16384:
                        continue
                    try:
                        record=json.loads(line)
                        cleaned=safe_event(record.get('event'),record)
                        if cleaned:
                            records.append(json.dumps(cleaned,ensure_ascii=False))
                    except (ValueError,TypeError,AttributeError):
                        continue
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',json.dumps({'format_version':1,'created_at':iso(self.clock()),
                'contents':['version','module_health','counts','sanitized_recent_logs'],
                'business_data_included':False,'credentials_included':False,'uploaded':False}))
            archive.writestr('health.json',json.dumps(self.health(),ensure_ascii=False))
            archive.writestr('logs.jsonl','\n'.join(records))
        return self.data_tools._artifact(output.getvalue(),'zip','application/zip')
