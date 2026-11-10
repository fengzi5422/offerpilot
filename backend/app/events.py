"""Explicit event changes and local calendar projections over stable task identities."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.domain import AppError, Temporal, millis, validated
from app.persistence import Candidate, Email, Task, audit, check_version, get_record


class EventService:
    def __init__(self, db, tasks):
        self.db, self.tasks = db, tasks

    def resolve_change(self, message_id, body):
        task_id=body.get('task_id')
        if not isinstance(task_id,str) or body.get('action') not in ('reschedule','cancel'):
            raise AppError('VALIDATION_ERROR','请选择原事件及处理方式',422)
        with self.db.task_lock(task_id), self.db.tx() as session:
            mail=get_record(session,Email,message_id)
            check_version(mail,body.get('expected_version'))
            candidate=get_record(session,Candidate,body.get('candidate_id'))
            if (candidate.message_id!=mail.id or candidate.state!='open' or
                    'CHANGE_REQUIRES_REVIEW' not in candidate.candidate.get('issues',[])):
                raise AppError('STATE_CONFLICT','这条变更已处理或不属于此邮件',409)
            task=get_record(session,Task,task_id)
            check_version(task,body.get('expected_task_version'))
            if task.status not in ('pending','in_progress'):
                raise AppError('STATE_CONFLICT','原事件已完成或取消，请选择仍在进行的事件',409)
            if body['action']=='cancel':
                result=self.tasks.transition(task.id,task.version,'cancel')
            else:
                temporal=validated(Temporal,body.get('temporal',{}))
                if temporal.needs_confirmation or temporal.precision!='minute':
                    raise AppError('EVENT_TIME_REQUIRED','请先确认新的日期和时间',422)
                expected='starts_at' if task.kind in ('interview','written_exam') else 'due_at'
                if not getattr(temporal,expected):
                    raise AppError('EVENT_TIME_REQUIRED','新时间与原事件类型不一致，请核对',422)
                changes={'temporal':temporal.public()}
                if 'event_details' in body:
                    changes['event_details']=body['event_details']
                result=self.tasks.patch(task.id,task.version,changes)
            # Keep original task source and append the explicit change evidence.
            source=dict(task.source or {})
            source['changes']=[*source.get('changes',[]),{'message_id':mail.id,'subject':mail.subject,
                'action':body['action'],'evidence':candidate.candidate.get('evidence',[])}][-50:]
            task.source=source
            candidate.state,candidate.task_id='accepted',task.id
            session.flush()
            mail.state='review_needed' if session.scalar(select(Candidate.id).where(
                Candidate.message_id==mail.id,Candidate.state=='open')) else 'parsed'
            mail.version+=1
            audit(session,mail,'mail.reviewed',{'action':body['action'],'task_id':task.id})
            return {'task':{**result,'source':source},'message_id':mail.id,'new_version':mail.version}

    def calendar(self, start, end, timezone=None):
        try:
            first,last=date.fromisoformat(start),date.fromisoformat(end)
            if not 0<(last-first).days<=93:
                raise ValueError()
            zone=ZoneInfo(timezone or self.tasks.settings.get()['timezone'])
        except (ValueError,TypeError,ZoneInfoNotFoundError):
            raise AppError('VALIDATION_ERROR','请选择有效日期范围，最多 93 天',422) from None
        lower,upper=millis(datetime.combine(first,time.min,zone)),millis(datetime.combine(last,time.min,zone))
        entries,uncertain=[],[]
        for task in self.tasks.list():
            if task['status'] not in ('pending','in_progress'):
                continue
            start_at=task['starts_at'] or task['due_at']
            if start_at:
                stamp=millis(datetime.fromisoformat(start_at))
                stop=millis(datetime.fromisoformat(task['ends_at'])) if task['ends_at'] else stamp
                if not (lower<=stamp<upper or stamp<lower<stop):
                    continue
                entries.append(task)
                if not task['confirmed'] or task['starts_at'] and not task['ends_at']:
                    uncertain.append(task)
            elif not task['due_date'] or str(first)<=task['due_date']<str(last):
                uncertain.append(task)
        entries.sort(key=lambda t:(t['starts_at'] or t['due_at'],t['id']))
        schedules=[t for t in entries if t['starts_at'] and t['confirmed']]
        conflicts=[]
        truncated=False
        for index,a in enumerate(schedules):
            for b in schedules[index+1:]:
                a0,b0=a['starts_at'],b['starts_at']
                a1,b1=a['ends_at'],b['ends_at']
                # Parse instants: lexical comparison is unsafe for fractional seconds.
                a0,b0=datetime.fromisoformat(a0),datetime.fromisoformat(b0)
                a1=datetime.fromisoformat(a1) if a1 else None
                b1=datetime.fromisoformat(b1) if b1 else None
                overlap=a1 and b1 and a0<b1 and b0<a1
                possible=(not a1 and not b1 and a0==b0 or
                    not a1 and b1 and b0<=a0<b1 or not b1 and a1 and a0<=b0<a1)
                if overlap or possible:
                    conflicts.append({'task_ids':[a['id'],b['id']],'kind':'overlap' if overlap else 'possible'})
                if len(conflicts)>=500:
                    truncated=True
                    break
            if truncated:
                break
        return {'start':str(first),'end':str(last),'timezone':str(zone),'entries':entries,
                'uncertain_entries':uncertain,'conflicts':conflicts,'conflicts_truncated':truncated}
