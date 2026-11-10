"""Versioned checklists; explicit reminder times become independent custom tasks."""
from datetime import datetime
from contextlib import contextmanager, nullcontext

from sqlalchemy import select

from app.domain import AppError, TaskInput, iso, millis, validated
from app.persistence import Preparation, Task, audit, check_version, get_record

TEMPLATES = {
    "interview": (("resume", "准备简历与项目介绍"), ("research", "了解公司与岗位"),
                  ("questions", "准备提问清单"), ("equipment", "确认地点、会议链接与设备")),
    "written_exam": (("requirements", "确认笔试范围与要求"), ("equipment", "检查设备与考试环境"),
                     ("documents", "准备证件与所需材料")),
    "materials": (("requirements", "核对材料清单"), ("documents", "整理并检查文件"), ("submit", "确认提交方式")),
}
DEFAULT_TEMPLATE = (("requirements", "确认事项要求"), ("documents", "准备所需材料"), ("check", "完成前再次核对"))


class PreparationService:
    def __init__(self, db, clock, tasks):
        self.db, self.clock, self.tasks = db, clock, tasks

    @staticmethod
    def serialize(item, parent):
        return {"id": item.id, "task_id": item.task_id, "text": item.text, "completed": item.completed,
                "version": item.version, "remind_at": iso(item.remind_at),
                "reminder_task_id": item.reminder_task_id, "template_key": item.template_key,
                "parent_changed": item.parent_schedule_revision != parent.schedule_revision,
                "created_at": iso(item.created_at), "updated_at": iso(item.updated_at)}

    def _list(self, parent, session):
        items = [self.serialize(item, parent) for item in session.scalars(select(Preparation).where(
            Preparation.task_id == parent.id, Preparation.deleted.is_(False))
            .order_by(Preparation.created_at, Preparation.id))]
        notice = None
        if parent.status == "cancelled":
            notice = "原事件已取消，准备内容仍保留；请核对独立准备提醒是否仍需要。"
        elif any(item["parent_changed"] for item in items):
            notice = "原事件时间或状态已变更，准备内容与自选提醒时刻已保留，请重新核对。"
        return {"items": items, "parent_version": parent.version, "parent_notice": notice}

    def list(self, task_id):
        with self.db.read() as session:
            return self._list(get_record(session, Task, task_id), session)

    @contextmanager
    def lock(self, task_id, preparation_id=None):
        # Obtain all task locks before the command transaction takes write_lock.
        with self.db.activity(), self.db.task_lock(task_id):
            child_id = None
            if preparation_id:
                with self.db.read() as session:
                    item = session.get(Preparation, preparation_id)
                    if item and item.task_id == task_id:
                        child_id = item.reminder_task_id
            with self.db.task_lock(child_id) if child_id else nullcontext():
                yield

    def _values(self, value, *, create=False):
        if not isinstance(value, dict) or set(value) - {"text", "completed", "remind_at"}:
            raise AppError("VALIDATION_ERROR", "准备事项包含不支持的字段", 422)
        result = dict(value)
        if create or "text" in value:
            text = result.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
                raise AppError("VALIDATION_ERROR", "请填写 1 至 200 字的准备内容", 422)
            result["text"] = text.strip()
        if "completed" in result and type(result["completed"]) is not bool:
            raise AppError("VALIDATION_ERROR", "完成状态必须为勾选值", 422)
        if result.get("remind_at") is not None:
            try:
                if not isinstance(result["remind_at"], str):
                    raise ValueError()
                at = datetime.fromisoformat(result["remind_at"])
                if at.tzinfo is None or at.utcoffset() is None or at <= self.clock():
                    raise ValueError()
                result["remind_at"] = millis(at)
            except (ValueError, TypeError, OverflowError):
                raise AppError("VALIDATION_ERROR", "提醒时间必须是包含时区的未来时刻", 422) from None
        return result

    def _touch(self, parent, session):
        parent.version += 1
        parent.updated_at = millis(self.clock())
        audit(session, parent, "task.preparations_changed")
        session.flush()
        return self._list(parent, session)

    def _item(self, session, task_id, preparation_id, expected_version):
        item = get_record(session, Preparation, preparation_id)
        if item.task_id != task_id or item.deleted:
            raise AppError("NOT_FOUND", "准备事项不存在", 404)
        check_version(item, expected_version)
        return item

    def _sync_reminder(self, item, session):
        reminder_task = session.get(Task, item.reminder_task_id) if item.reminder_task_id else None
        if item.remind_at is None:
            if reminder_task:
                reminder_task.status = "cancelled"
                reminder_task.version += 1
                reminder_task.schedule_revision += 1
                reminder_task.updated_at = millis(self.clock())
                self.tasks.rebuild(session, reminder_task)
                audit(session, reminder_task, "task.preparation_reminder_removed")
            return
        payload = {"title": item.text, "kind": "custom", "temporal": {
            "kind": "absolute", "precision": "minute", "due_at": iso(item.remind_at),
            "timezone": "UTC", "timezone_source": "user_confirmed", "needs_confirmation": False}}
        if reminder_task:
            payload.update(notes=reminder_task.notes, priority=reminder_task.priority,
                           application_id=reminder_task.application_id, event_details=reminder_task.event_details)
        if reminder_task is None:
            # Flush checklist identity before creating a task whose source refers to it.
            session.flush()
            result = self.tasks.create(payload, session=session, source={"preparation_id": item.id})
            item.reminder_task_id = result["id"]
            reminder_task = session.get(Task, result["id"])
        else:
            self.tasks._apply(reminder_task, validated(TaskInput, payload))
            reminder_task.version += 1
        status = "cancelled" if item.deleted else "done" if item.completed else (
            'in_progress' if reminder_task.status == 'in_progress' else 'pending')
        reminder_task.status = status
        reminder_task.schedule_revision += 1
        self.tasks.rebuild(session, reminder_task)
        audit(session, reminder_task, "task.preparation_reminder_changed")

    def create(self, task_id, expected_parent_version, value):
        values = self._values(value, create=True)
        with self.lock(task_id), self.db.tx() as session:
            parent = get_record(session, Task, task_id)
            check_version(parent, expected_parent_version)
            item = Preparation(task_id=task_id, text=values["text"], completed=values.get("completed", False),
                deleted=False, remind_at=values.get("remind_at"), parent_schedule_revision=parent.schedule_revision)
            session.add(item)
            session.flush()
            self._sync_reminder(item, session)
            audit(session, item, "preparation.created")
            return self._touch(parent, session)

    def patch(self, task_id, preparation_id, expected_parent_version, expected_version, changes):
        values = self._values(changes)
        with self.lock(task_id, preparation_id), self.db.tx() as session:
            parent = get_record(session, Task, task_id)
            check_version(parent, expected_parent_version)
            item = self._item(session, task_id, preparation_id, expected_version)
            for key, value in values.items():
                setattr(item, key, value)
            item.version += 1
            item.updated_at = millis(self.clock())
            self._sync_reminder(item, session)
            audit(session, item, "preparation.changed", {"fields": list(values)})
            return self._touch(parent, session)

    def delete(self, task_id, preparation_id, expected_parent_version, expected_version):
        with self.lock(task_id, preparation_id), self.db.tx() as session:
            parent = get_record(session, Task, task_id)
            check_version(parent, expected_parent_version)
            item = self._item(session, task_id, preparation_id, expected_version)
            # Keep a tombstone so reapplying a template never resurrects a removed item.
            item.deleted, item.updated_at = True, millis(self.clock())
            item.version += 1
            self._sync_reminder(item, session)
            audit(session, item, "preparation.deleted")
            return self._touch(parent, session)

    def apply_template(self, task_id, expected_parent_version):
        with self.lock(task_id), self.db.tx() as session:
            parent = get_record(session, Task, task_id)
            check_version(parent, expected_parent_version)
            existing = set(session.scalars(select(Preparation.template_key).where(Preparation.task_id == task_id)))
            for key, text in TEMPLATES.get(parent.kind, DEFAULT_TEMPLATE):
                key = parent.kind + ":" + key
                if key not in existing:
                    item = Preparation(task_id=task_id, text=text, completed=False, deleted=False,
                        template_key=key, parent_schedule_revision=parent.schedule_revision)
                    session.add(item)
                    session.flush()
                    audit(session, item, "preparation.template_added")
            return self._touch(parent, session)
