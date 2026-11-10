"""Version-checked application links, with task schedules deliberately preserved."""

from app.applications import ApplicationService
from app.domain import AppError, iso, millis
from app.persistence import Application, Task, audit, check_version, get_record


class ApplicationBoardService:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.applications = ApplicationService(db, clock)

    def _touch(self, record):
        record.version += 1
        record.updated_at = millis(self.clock())

    def _change(self, session, task, source, destination):
        detail = {"old_application_id": source.id if source else None,
                  "new_application_id": destination.id if destination else None,
                  "task_id": task.id, "actor": "user", "at": iso(millis(self.clock()))}
        task.application_id = detail["new_application_id"]
        task.manual_fields = sorted(set(task.manual_fields) | {"application_id"})
        self._touch(task)
        audit(session, task, "task.application_changed", detail)
        if source:
            self._touch(source)
            audit(session, source, "application.task_unlinked", {**detail, "application_version": source.version})
        if destination:
            self._touch(destination)
            audit(session, destination, "application.task_linked", {**detail, "application_version": destination.version})

    def link(self, application_id, expected_application_version, task_id, expected_task_version,
             expected_source_version=None):
        with self.db.task_lock(task_id), self.db.tx() as session:
            destination = get_record(session, Application, application_id)
            task = get_record(session, Task, task_id)
            check_version(destination, expected_application_version)
            check_version(task, expected_task_version)
            if task.application_id == destination.id:
                return self.applications.serialize(destination, session)
            source = get_record(session, Application, task.application_id) if task.application_id else None
            if source:
                check_version(source, expected_source_version)
            self._change(session, task, source, destination)
            return self.applications.serialize(destination, session)

    def unlink(self, application_id, expected_application_version, task_id, expected_task_version):
        with self.db.task_lock(task_id), self.db.tx() as session:
            source = get_record(session, Application, application_id)
            task = get_record(session, Task, task_id)
            check_version(source, expected_application_version)
            check_version(task, expected_task_version)
            if task.application_id is None:
                return self.applications.serialize(source, session)
            if task.application_id != source.id:
                raise AppError("STATE_CONFLICT", "任务已关联其他申请，请刷新后重试", 409)
            self._change(session, task, source, None)
            return self.applications.serialize(source, session)
