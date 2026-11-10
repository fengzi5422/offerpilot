"""A bounded persistent suggestion queue; HTTP never holds a database/activity lock."""
import hashlib
import threading
from types import SimpleNamespace
from contextlib import contextmanager

from sqlalchemy import func, select

from app.ai import AIParser
from app.domain import AppError, iso, millis
from app.persistence import Candidate, Email, Job, Setting, check_version, get_record

TRANSIENT = {'AI_RATE_LIMITED', 'AI_SERVICE_FAILED', 'SERVICE_TIMEOUT', 'SERVICE_NETWORK_ERROR'}


def enrich_open_candidate(old, suggestion):
    """Fill missing fields on the same reviewed action; keep precise rule values."""
    merged = {**old}
    previous, proposed = old['temporal'], suggestion['temporal']
    precise = previous.get('precision') == 'minute' and not previous.get('needs_confirmation')
    if not precise and (proposed.get('starts_at') or proposed.get('due_at') or proposed.get('local_date')):
        merged['temporal'] = proposed
    merged['event_details'] = {key: old.get('event_details', {}).get(key) or suggestion.get('event_details', {}).get(key, '')
        for key in ('location', 'meeting_url')}
    if not old.get('company') and suggestion.get('company'):
        merged['company'], merged['title'] = suggestion['company'], suggestion['title']
    if all(merged.get(key) == old.get(key) for key in ('temporal', 'event_details', 'company', 'title')):
        return None
    merged['parser_source'] = 'ai'
    merged['rule_enriched'] = True
    reasons = set(previous.get('reason_codes', []))
    merged['issues'] = list(dict.fromkeys(['AI_REQUIRES_REVIEW',
        *[issue for issue in old.get('issues', []) if issue not in reasons and not (issue == 'COMPANY_MISSING' and merged.get('company'))],
        *merged['temporal'].get('reason_codes', [])]))
    merged['evidence'] = old.get('evidence', []) + suggestion.get('evidence', [])
    return merged


class AIWorker:
    def __init__(self, db, settings, integrations, clock, parser=None):
        self.db, self.settings, self.integrations, self.clock = db, settings, integrations, clock
        self.parser = parser or AIParser()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        with db.tx() as session:
            if not session.get(Setting, 'ai_runtime'):
                session.add(Setting(id='ai_runtime', value={'failures': 0, 'paused_until': None, 'last_error_code': None}))
            for job in session.scalars(select(Job).where(Job.kind == 'ai_parse', Job.status == 'interrupted', Job.error_code == 'APP_RESTARTED')):
                job.status, job.error_code = 'queued', None

    def status(self):
        with self.db.read() as session:
            record = session.get(Setting, 'ai_runtime')
            value = record.value if record else {}
            return {'last_error_code': value.get('last_error_code'), 'paused_until': iso(value.get('paused_until')),
                'queued': session.scalar(select(func.count()).select_from(Job).where(Job.kind == 'ai_parse', Job.status == 'queued'))}

    def stop(self):
        with self.db.outbound_gate, self.db.write_lock:
            self.stopping.set()

    def test(self):
        config = self.integrations.snapshot('ai')
        if not config:
            raise AppError('AI_DISABLED', '请先配置并启用 AI', 409)
        @contextmanager
        def guard():
            with self.db.outbound_gate:
                if self.stopping.is_set() or not self.integrations.current(config):
                    raise AppError('AI_INPUT_CHANGED', '测试已取消，请检查设置', 409)
                yield
        config['_dispatch_guard'] = guard
        sample = SimpleNamespace(id='fixed-example', subject='示例公司招聘通知',
            text_excerpt='请于2026年12月18日09:30参加技术面试。', text_hash='fixed-example',
            header_date=None, internal_date=None, text_truncated=False)
        if not self.lock.acquire(blocking=False):
            raise AppError('AI_QUEUE_FULL', 'AI 正在识别，请稍后测试', 409)
        try:
            self.parser.parse(sample, self.settings.get(), config)
            return {'status': 'validated'}
        finally:
            self.lock.release()

    def changed(self, kind, session):
        if kind != 'ai':
            return
        for job in session.scalars(select(Job).where(Job.kind == 'ai_parse', Job.status.in_(['queued', 'running']))):
            job.status, job.error_code = 'interrupted', 'AI_CONFIG_CHANGED'
        record = session.get(Setting, 'ai_runtime')
        if record:
            record.value = {'failures': 0, 'paused_until': None, 'last_error_code': None}

    def enqueue(self, message_id, expected_version=None, force=False):
        if not self.integrations.get('ai')['enabled']:
            if force:
                raise AppError('AI_DISABLED', '请先在设置中配置并启用 AI', 409)
            return None
        with self.db.tx() as session:
            if self.stopping.is_set():
                if force:
                    raise AppError('APP_STOPPING', '应用正在退出，请重新打开后识别', 503)
                return None
            mail = get_record(session, Email, message_id)
            if expected_version is not None:
                check_version(mail, expected_version)
            if mail.ignored_by_user or not mail.text_excerpt:
                if force:
                    raise AppError('SOURCE_UNAVAILABLE', '原文已清理或忽略，请重新导入邮件后识别', 409)
                return None
            runtime = session.get(Setting, 'ai_runtime')
            if runtime and runtime.value.get('auth_blocked'):
                if force:
                    raise AppError('AI_AUTH_FAILED', 'AI 认证或额度不可用，请在设置中更新配置后再识别', 409)
                return None
            if force and runtime and (runtime.value.get('paused_until') or 0) > millis(self.clock()):
                raise AppError('AI_COOLDOWN', 'AI 连续失败后已暂时暂停，请先检查服务配置，或稍后重试', 409)
            if not force and mail.classification.get('relevance') == 'non_recruitment':
                return None
            candidates = list(session.scalars(select(Candidate).where(Candidate.message_id == message_id)))
            if not force and candidates and all(c.state != 'open' for c in candidates):
                return None
            pending = list(session.scalars(select(Job).where(Job.kind == 'ai_parse', Job.status.in_(['queued', 'running']))))
            prior = next((j for j in pending if j.progress.get('message_id') == message_id), None)
            if prior:
                return prior.id
            if len(pending) >= 20:
                if force:
                    raise AppError('AI_QUEUE_FULL', 'AI 识别队列已满，请稍后再试', 409)
                return None
            mail.version += 1
            mail.classification = {**mail.classification, 'ai_status': 'queued'}
            job = Job(kind='ai_parse', status='queued', progress={'message_id': message_id,
                'mail_version': mail.version, 'config_version': self.integrations.get('ai', session)['version'], 'attempt': 0})
            session.add(job)
            session.flush()
            return job.id

    def run_once(self):
        if self.stopping.is_set():
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._run_once()
        finally:
            self.lock.release()

    def _run_once(self):
        now = millis(self.clock())
        with self.db.read() as session:
            runtime = session.get(Setting, 'ai_runtime')
            if runtime and (runtime.value.get('auth_blocked') or (runtime.value.get('paused_until') or 0) > now):
                return
            jobs = list(session.scalars(select(Job).where(Job.kind == 'ai_parse', Job.status == 'queued').order_by(Job.created_at, Job.id)))
            selected = next((j for j in jobs if j.progress.get('retry_at', 0) <= now), None)
            if not selected:
                return
            job_id = selected.id
        try:
            config = self.integrations.snapshot('ai')
        except AppError as error:
            config = None
            self._finish_error(job_id, error.code, None)
            return
        if not config:
            return
        with self.db.tx() as session:
            job = session.get(Job, job_id)
            if self.stopping.is_set() or not job or job.status != 'queued':
                return
            mail = session.get(Email, job.progress['message_id'])
            if (not mail or mail.ignored_by_user or mail.version != job.progress['mail_version'] or not mail.text_excerpt or
                    config['version'] != job.progress['config_version'] or not self.integrations.current(config, session)):
                job.status, job.error_code = 'interrupted', 'AI_INPUT_CHANGED'
                return
            fields = {column.name: getattr(mail, column.name) for column in Email.__table__.columns}
            profile, mail_version = self.settings.get(session), mail.version
            profile_version = profile['version']
            job.status = 'running'
            mail.classification = {**mail.classification, 'ai_status': 'running', 'ai_error_code': None}
        @contextmanager
        def dispatch_guard():
            # Hold the dispatch gate only during request writing, never response waiting.
            with self.db.outbound_gate:
                with self.db.read() as session:
                    mail = session.get(Email, fields['id'])
                    job = session.get(Job, job_id)
                    if (self.stopping.is_set() or not self.integrations.current(config, session) or not job or job.status != 'running' or not mail or
                            mail.version != mail_version or mail.ignored_by_user or self.settings.get(session)['version'] != profile_version):
                        raise AppError('AI_INPUT_CHANGED', '识别已取消，来源或设置已变化', 409)
                yield
        config['_dispatch_guard'] = dispatch_guard
        try:
            if not self.integrations.current(config):
                return
            drafts = self.parser.parse(SimpleNamespace(**fields), profile, config)
        except AppError as error:
            self._finish_error(job_id, error.code, config)
            return
        except Exception:
            self._finish_error(job_id, 'AI_SERVICE_FAILED', config)
            return
        if self.stopping.is_set() or self.db.closed or self.db.generation != config['generation']:
            return
        with self.db.tx() as session:
            job = session.get(Job, job_id)
            mail = session.get(Email, fields['id'])
            if (self.stopping.is_set() or not job or job.status != 'running' or not mail or mail.version != mail_version or mail.ignored_by_user or
                    self.settings.get(session)['version'] != profile_version or not self.integrations.current(config, session)):
                if job and job.status == 'running':
                    job.status, job.error_code = 'interrupted', 'AI_INPUT_CHANGED'
                return
            existing = list(session.scalars(select(Candidate).where(Candidate.message_id == mail.id)))
            for candidate in existing:
                if candidate.state == 'open' and candidate.candidate.get('parser_source') == 'ai' and not candidate.candidate.get('rule_enriched'):
                    candidate.state = 'superseded'
            for draft in drafts:
                # Any prior accepted/ignored action of this kind keeps authority.
                if any(c.state in ('accepted', 'ignored') and c.candidate.get('kind') == draft['kind'] for c in existing):
                    continue
                rules = [c for c in existing if c.state == 'open' and c.candidate.get('kind') == draft['kind'] and
                    (c.candidate.get('parser_source') != 'ai' or c.candidate.get('rule_enriched'))]
                matching = [c for c in rules if any(a.get('start', 0) < b.get('end', 0) and a.get('end', 0) > b.get('start', 0)
                    for a in c.candidate.get('evidence', []) for b in draft.get('evidence', []))]
                if len(matching) == 1:
                    candidate = matching[0]
                    enriched = enrich_open_candidate(candidate.candidate, draft)
                    if enriched:
                        candidate.candidate = enriched
                        candidate.version += 1
                    continue
                if rules:
                    continue
                quote = draft['evidence'][0]['quote']
                action_key = mail.id + ':ai:' + hashlib.sha256((draft['kind'] + quote).encode()).hexdigest()
                prior = next((c for c in existing if c.action_key == action_key), None)
                if prior:
                    if prior.state == 'superseded':
                        prior.state, prior.candidate = 'open', draft
                        prior.version += 1
                    continue
                candidate = Candidate(message_id=mail.id, candidate=draft, action_key=action_key, state='open')
                session.add(candidate)
                existing.append(candidate)
            session.flush()
            mail.state = 'review_needed' if any(c.state == 'open' for c in existing) else 'parsed'
            mail.classification = {**mail.classification, 'ai_status': 'succeeded', 'ai_error_code': None}
            mail.version += 1
            job.status, job.result = 'succeeded', {'message_id': mail.id, 'suggestions': len(drafts)}
            runtime = session.get(Setting, 'ai_runtime')
            if runtime:
                runtime.value = {'failures': 0, 'paused_until': None, 'last_error_code': None}

    def _finish_error(self, job_id, code, config):
        if self.stopping.is_set() or self.db.closed or config and self.db.generation != config['generation']:
            return
        now = millis(self.clock())
        with self.db.tx() as session:
            if self.stopping.is_set():
                return
            job = session.get(Job, job_id)
            if not job or job.status not in ('queued', 'running') or config and not self.integrations.current(config, session):
                return
            mail = session.get(Email, job.progress['message_id'])
            if not mail or mail.version != job.progress['mail_version']:
                job.status, job.error_code = 'interrupted', 'AI_INPUT_CHANGED'
                return
            runtime = session.get(Setting, 'ai_runtime')
            value = dict(runtime.value) if runtime else {}
            failures = value.get('failures', 0) + 1
            if runtime:
                runtime.value = {'failures': failures, 'last_error_code': code,
                    'auth_blocked': code in ('AI_AUTH_FAILED', 'SERVICE_AUTH_REQUIRED'),
                    'paused_until': now + 15 * 60000 if failures >= 3 else None}
            retry = code in TRANSIENT and job.progress.get('attempt', 0) < 1
            job.status, job.error_code = ('queued' if retry else 'failed'), code
            mail.version += 1
            mail.classification = {**mail.classification, 'ai_status': 'retry_wait' if retry else 'failed', 'ai_error_code': code}
            job.progress = {**job.progress, 'attempt': job.progress.get('attempt', 0) + 1, 'retry_at': now + 60000,
                            'mail_version': mail.version}
