from sqlalchemy import select

from app.domain import AppError, iso, millis
from app.persistence import Job, get_record


class JobService:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        with db.tx() as session:
            for job in session.scalars(select(Job).where(Job.status.in_(["queued", "running"]))):
                job.status, job.error_code = "interrupted", "APP_RESTARTED"

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
            return {"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress,
                    "result": job.result, "error_code": job.error_code, "created_at": iso(job.created_at),
                    "updated_at": iso(job.updated_at)}
