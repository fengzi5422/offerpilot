from sqlalchemy import select
import threading
import time
from contextlib import nullcontext

from app.domain import AppError, iso, millis
from app.persistence import Job, get_record


class JobService:
    RESUMABLE = {"export", "backup", "restore_validate", "eml_import", "reparse", "diagnostic_export"}

    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.handlers = {}
        self.lock = threading.Lock()
        with db.tx() as session:
            for job in session.scalars(select(Job).where(Job.status.in_(["queued", "running"]))):
                if job.kind in self.RESUMABLE and "input" in job.progress:
                    job.status, job.error_code = "queued", None
                else:
                    job.status, job.error_code = "interrupted", "APP_RESTARTED"

    def enqueue(self, kind, payload):
        if kind not in self.RESUMABLE:
            raise AppError("JOB_KIND_UNSUPPORTED", "后台操作类型不受支持", 422)
        with self.db.tx() as session:
            job = Job(kind=kind, status="queued", progress={"phase": "queued", "completed": 0, "input": payload})
            session.add(job)
            session.flush()
            return job.id

    def run_once(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.db.activity():
                with self.db.tx() as session:
                    job = session.scalar(select(Job).where(Job.status == "queued", Job.kind.in_(self.RESUMABLE))
                                         .order_by(Job.created_at, Job.id).limit(1))
                    if not job:
                        return
                    job_id, kind, payload = job.id, job.kind, job.progress["input"]
                    job.status = "running"
                    job.progress = {**job.progress, "phase": "running"}
                try:
                    started = time.monotonic()
                    self.db.log("job.start", module="jobs", job_id=job_id)
                    # Reparse changes an expected-version entity. Its receipt must commit with that change.
                    with self.db.tx() if kind == "reparse" else nullcontext():
                        result = self.handlers[kind](payload)
                        self.update(job_id, status="succeeded", result=result, progress={"phase": "complete", "completed": 1})
                except AppError as exc:
                    self.update(job_id, status="failed", error_code=exc.code, progress={"phase": "failed", "completed": 0})
                except Exception:
                    self.update(job_id, status="failed", error_code="JOB_FAILED", progress={"phase": "failed", "completed": 0})
                detail = self.get(job_id)
                self.db.log("job.complete" if detail["status"] == "succeeded" else "job.fail", module="jobs",
                            job_id=job_id, duration_ms=int((time.monotonic()-started)*1000), error_code=detail["error_code"])
                name = payload.get("file")
                if isinstance(name, str):
                    from pathlib import Path
                    if Path(name).name == name and name.endswith(".input"):
                        (self.db.directory / "staging" / name).unlink(missing_ok=True)
        finally:
            self.lock.release()

    def create(self, kind, status="queued", result=None):
        with self.db.tx() as session:
            job = Job(kind=kind, status=status, result=result, progress={"phase": status, "completed": 0})
            session.add(job)
            session.flush()
            return job.id

    def update(self, job_id, *, status=None, progress=None, result=None, error_code=None):
        with self.db.tx() as session:
            job = get_record(session, Job, job_id)
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = progress
            if result is not None:
                job.result = result
            job.error_code, job.updated_at = error_code, millis(self.clock())
            if job.kind in ("mail_sync", "mail_test") and status in ("running", "succeeded", "failed"):
                self.db.log({"running":"mail.sync.start","succeeded":"mail.sync.complete","failed":"mail.sync.fail"}[status],
                            module="mail", job_id=job_id, error_code=error_code)

    def execute(self, kind, fn):
        job_id = self.create(kind, "running")
        try:
            result = fn()
        except AppError as exc:
            self.update(job_id, status="failed", error_code=exc.code)
            raise
        except Exception:
            self.update(job_id, status="failed", error_code="JOB_FAILED")
            raise AppError("JOB_FAILED", "操作未完成，请查看诊断并重试", 503) from None
        self.update(job_id, status="succeeded", result=result, progress={"phase": "complete", "completed": 1})
        return job_id

    def get(self, job_id):
        with self.db.read() as session:
            job = get_record(session, Job, job_id)
            return {"id": job.id, "kind": job.kind, "status": job.status, "progress": {k:v for k,v in job.progress.items() if k != "input"},
                    "result": job.result, "error_code": job.error_code, "created_at": iso(job.created_at),
                    "updated_at": iso(job.updated_at)}
