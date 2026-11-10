from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.domain import AppError, iso, millis, validated
from app.persistence import Application, Audit, Task, audit, check_version, get_record
from app.tasks import TaskService


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    position: str | None = Field(default=None, max_length=200)
    stage: Literal["applied", "assessment", "written_exam", "interview", "awaiting_result", "offer", "closed"] = "applied"

    @model_validator(mode="after")
    def trim(self):
        self.company = self.company.strip()
        if not self.company:
            raise ValueError("公司名称不能为空")
        if self.position is not None:
            self.position = self.position.strip() or None
        return self


class ApplicationService:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def serialize(self, record, session=None):
        result = {"id": record.id, "version": record.version, "company": record.company,
                  "position": record.position, "stage": record.stage, "source": record.source,
                  "manual_fields": record.manual_fields, "closed_at": iso(record.closed_at),
                  "created_at": iso(record.created_at), "updated_at": iso(record.updated_at)}
        if session is not None:
            session.flush()
            serializer = TaskService(self.db, self.clock, None)
            result["tasks"] = [serializer.serialize(task, session) for task in session.scalars(
                select(Task).where(Task.application_id == record.id).order_by(Task.created_at, Task.id))]
            result["history"] = [{"id": event.id, "action": event.action,
                                  "at": event.detail.get("at", iso(event.created_at)),
                                  "actor": event.detail.get("actor", "system"), **event.detail}
                                 for event in session.scalars(select(Audit).where(
                                     Audit.entity_type == Application.__tablename__, Audit.entity_id == record.id)
                                     .order_by(Audit.created_at, Audit.id))]
            result["history"].sort(key=lambda entry: (entry.get("application_version", 1), entry["at"], entry["id"]))
        return result

    def list(self):
        with self.db.read() as session:
            return [self.serialize(record, session) for record in session.scalars(
                select(Application).order_by(Application.created_at, Application.id))]

    def get(self, application_id):
        with self.db.read() as session:
            return self.serialize(get_record(session, Application, application_id), session)

    def create(self, value):
        data = validated(ApplicationInput, value)
        with self.db.tx() as session:
            record = Application(**data.model_dump(), source="manual", manual_fields=list(value),
                                 closed_at=millis(self.clock()) if data.stage == "closed" else None)
            session.add(record)
            session.flush()
            audit(session, record, "application.created", {"old_stage": None, "new_stage": record.stage,
                                                         "actor": "user", "at": iso(millis(self.clock())),
                                                         "application_version": record.version})
            return self.serialize(record, session)

    def patch(self, application_id, expected_version, changes):
        if not isinstance(changes, dict) or set(changes) - {"company", "position", "stage"}:
            raise AppError("VALIDATION_ERROR", "申请修改字段无效", 422)
        with self.db.tx() as session:
            record = get_record(session, Application, application_id)
            check_version(record, expected_version)
            data = validated(ApplicationInput, {**{key: getattr(record, key) for key in ("company", "position", "stage")}, **changes})
            old_stage = record.stage
            for key, value in data.model_dump().items():
                setattr(record, key, value)
            record.manual_fields = sorted(set(record.manual_fields) | set(changes))
            if old_stage != record.stage:
                record.closed_at = millis(self.clock()) if record.stage == "closed" else None
                audit(session, record, "application.stage_changed", {"old_stage": old_stage,
                      "new_stage": record.stage, "actor": "user", "at": iso(millis(self.clock())),
                      "application_version": record.version + 1})
            record.version, record.updated_at = record.version + 1, millis(self.clock())
            audit(session, record, "application.changed", {"fields": list(changes), "actor": "user",
                                                         "at": iso(millis(self.clock())),
                                                         "application_version": record.version})
            return self.serialize(record, session)
