from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.domain import AppError, TaskInput, iso, millis, validated
from app.persistence import Application, Reminder, Setting, Task, audit, check_version, get_record

DEFAULT_SETTINGS = {
    "timezone": "Asia/Shanghai", "onboarding_completed": False, "notifications_enabled": True,
    "sync_interval_seconds": 180, "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
    "autostart_enabled": False,
    "notification_privacy": "generic",
}
OFFSETS = {"interview": [86400, 3600, 900], "written_exam": [86400, 3600, 900],
           "assessment": [86400, 10800], "materials": [86400, 10800], "reply": [86400, 3600],
           "offer": [86400, 3600], "custom": [3600]}
DEFAULT_SETTINGS["reminder_offsets_seconds"] = OFFSETS


class SettingsService:
    def __init__(self, db):
        self.db = db
        self.replan = None
        with db.tx() as session:
            if not session.get(Setting, "profile"):
                session.add(Setting(id="profile", value=deepcopy(DEFAULT_SETTINGS)))

    def get(self, session=None):
        if session is None:
            with self.db.read() as s:
                return self.get(s)
        record = session.get(Setting, "profile")
        return {**deepcopy(DEFAULT_SETTINGS), **record.value, "version": record.version}

    def _validate(self, current, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULT_SETTINGS):
            raise AppError("VALIDATION_ERROR", "设置包含不支持的字段", 422)
        merged = {**current, **changes}
        if "reminder_offsets_seconds" in changes:
            supplied = changes["reminder_offsets_seconds"]
            if not isinstance(supplied, dict) or set(supplied) - set(OFFSETS):
                raise AppError("VALIDATION_ERROR", "提醒模板包含不支持的事项类型", 422)
            merged["reminder_offsets_seconds"] = {**current["reminder_offsets_seconds"], **supplied}
        try:
            ZoneInfo(merged["timezone"])
            if merged["notification_privacy"] not in ("generic", "details"):
                raise ValueError()
            for key in ("onboarding_completed", "notifications_enabled", "autostart_enabled"):
                if type(merged[key]) is not bool:
                    raise ValueError()
            if type(merged["sync_interval_seconds"]) is not int or not 120 <= merged["sync_interval_seconds"] <= 300:
                raise ValueError()
            quiet = merged["quiet_hours"]
            if set(quiet) != {"enabled", "start", "end"} or type(quiet["enabled"]) is not bool:
                raise ValueError()
            for key in ("start", "end"):
                datetime.strptime(quiet[key], "%H:%M")
            if quiet["start"] == quiet["end"]:
                raise ValueError()
            for offsets in merged["reminder_offsets_seconds"].values():
                if (not isinstance(offsets, list) or len(offsets) > 10 or
                        any(type(offset) is not int or not 60 <= offset <= 604800 for offset in offsets) or
                        len(set(offsets)) != len(offsets)):
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise AppError("VALIDATION_ERROR", "设置值无效", 422) from None
        merged.pop("version", None)
        return merged

    def patch(self, expected_version, changes, *, autostart_callback=None):
        # Drain deliveries before replacing plans; no task-lock / database-lock inversion.
        with self.db.maintenance():
            external_changed = False
            old_autostart = False
            try:
                with self.db.tx() as session:
                    record = session.get(Setting, "profile")
                    check_version(record, expected_version)
                    current = self.get(session)
                    merged = self._validate(current, changes)
                    record.value = merged
                    record.version += 1
                    if self.replan and any(merged[key] != current[key] for key in
                                           ("quiet_hours", "timezone", "reminder_offsets_seconds")):
                        self.replan(session)
                    if "autostart_enabled" in changes:
                        if merged["autostart_enabled"] and autostart_callback is None:
                            raise AppError("AUTOSTART_UNAVAILABLE", "请通过桌面启动器配置开机启动", 400)
                        if autostart_callback:
                            old_autostart = current["autostart_enabled"]
                            external_changed = True
                            try:
                                autostart_callback(merged["autostart_enabled"])
                            except Exception:
                                raise AppError("AUTOSTART_FAILED", "无法修改开机启动，请检查 Windows 启动项后重试", 503) from None
                    audit(session, record, "settings.changed", {"fields": list(changes)})
                    return {**record.value, "version": record.version}
            except Exception:
                if external_changed:
                    try:
                        autostart_callback(old_autostart)
                    except Exception:
                        raise AppError("AUTOSTART_ROLLBACK_REQUIRED", "设置未保存，请在 Windows 启动项中核对 OfferPilot 状态", 503) from None
                raise


class TaskService:
    def __init__(self, db, clock, settings):
        self.db, self.clock, self.settings = db, clock, settings

    def serialize(self, task, session):
        next_reminder = session.scalar(select(Reminder).where(Reminder.task_id == task.id,
            Reminder.state.in_(["pending", "retry_wait", "claimed"])).order_by(Reminder.fire_at))
        return {"id": task.id, "title": task.title, "kind": task.kind, "status": task.status,
                "application_id": task.application_id, "event_details": {"location":"","meeting_url":"",**(task.event_details or {})},
                "priority": task.priority, "version": task.version, "temporal": task.temporal,
                "due_at": iso(task.due_at), "starts_at": iso(task.starts_at), "ends_at": iso(task.ends_at),
                "due_date": task.due_date, "confirmed": task.confirmed, "notes": task.notes,
                "source": task.source, "manual_fields": task.manual_fields,
                "created_at": iso(task.created_at), "updated_at": iso(task.updated_at),
                "schedule_revision": task.schedule_revision,
                "next_reminder_at": iso(next_reminder.fire_at) if next_reminder else None}

    def _apply(self, task, data):
        temporal = data.temporal
        task.title, task.kind, task.priority, task.notes = data.title, data.kind, data.priority, data.notes
        task.application_id = data.application_id
        task.event_details = data.event_details.model_dump()
        task.temporal = temporal.public()
        task.due_at = millis(temporal.due_at) if temporal.due_at else None
        task.starts_at = millis(temporal.starts_at) if temporal.starts_at else None
        task.ends_at = millis(temporal.ends_at) if temporal.ends_at else None
        task.due_date = str(temporal.local_date) if temporal.local_date else None
        task.confirmed = not temporal.needs_confirmation and temporal.precision == "minute"
        task.updated_at = millis(self.clock())

    def rebuild(self, session, task):
        for reminder in session.scalars(select(Reminder).where(Reminder.task_id == task.id,
                                            Reminder.state.in_(["pending", "retry_wait", "claimed"]))):
            reminder.state = "suppressed"
            reminder.version += 1
        now = millis(self.clock())
        anchor = task.starts_at if task.kind in ("interview", "written_exam") else task.due_at or task.starts_at
        if not task.confirmed or task.status not in ("pending", "in_progress") or not anchor or anchor <= now:
            return
        settings = self.settings.get(session)
        for offset in settings["reminder_offsets_seconds"][task.kind]:
            scheduled = anchor - offset * 1000
            if scheduled <= now:
                continue
            fire = scheduled
            quiet = settings["quiet_hours"]
            if quiet["enabled"]:
                zone = ZoneInfo(settings["timezone"])
                local = datetime.fromtimestamp(scheduled / 1000, zone)
                start = datetime.strptime(quiet["start"], "%H:%M").time()
                end = datetime.strptime(quiet["end"], "%H:%M").time()
                current = local.time().replace(tzinfo=None)
                within = start <= current < end if start < end else current >= start or current < end
                if within:
                    end_date = local.date() + timedelta(days=1 if start >= end and current >= start else 0)
                    fire = millis(datetime.combine(end_date, end, zone))
            session.add(Reminder(task_id=task.id, schedule_revision=task.schedule_revision,
                                offset_seconds=offset, scheduled_for=scheduled, fire_at=fire,
                                expires_at=min(fire + 600000, anchor), state="pending" if fire < anchor else "missed",
                                idempotency_key=f"{task.id}:{task.schedule_revision}:windows:{offset}"))
        session.flush()

    def rebuild_all(self, session):
        for task in session.scalars(select(Task).where(Task.status.in_(["pending", "in_progress"]))):
            task.schedule_revision += 1
            task.version += 1
            task.updated_at = millis(self.clock())
            self.rebuild(session, task)
            audit(session, task, "task.reminders_replanned", {"source": "settings"})

    def create(self, value, *, session=None, source=None, action_key=None):
        data = validated(TaskInput, value)
        if session is None:
            with self.db.tx() as s:
                return self.create(value, session=s, source=source, action_key=action_key)
        if action_key:
            existing = session.scalar(select(Task).where(Task.action_key == action_key))
            if existing:
                return self.serialize(existing, session)
        if data.application_id:
            get_record(session, Application, data.application_id)
        task = Task(status="pending", schedule_revision=1, version=1, source=source, action_key=action_key,
                    manual_fields=list(value) if source is None else [])
        self._apply(task, data)
        session.add(task)
        session.flush()
        self.rebuild(session, task)
        audit(session, task, "task.created", {"source": "manual" if source is None else "mail"})
        return self.serialize(task, session)

    def get(self, task_id):
        with self.db.read() as session:
            result = self.serialize(get_record(session, Task, task_id), session)
            result["reminders"] = [{"id": r.id, "version": r.version, "state": r.state,
                                    "fire_at": iso(r.fire_at), "error_code": r.error_code}
                                   for r in session.scalars(select(Reminder).where(Reminder.task_id == task_id)
                                                            .order_by(Reminder.fire_at))]
            return result

    def list(self, view=None, status=None):
        with self.db.read() as session:
            query = select(Task).order_by(Task.created_at.desc(), Task.id)
            if status:
                if status not in ("pending", "in_progress", "done", "cancelled", "all"):
                    raise AppError("VALIDATION_ERROR", "任务状态无效", 422)
                if status != "all":
                    query = query.where(Task.status == status)
            items = [self.serialize(t, session) for t in session.scalars(query)]
        now = self.clock()
        zone = ZoneInfo(self.settings.get()["timezone"])
        today = now.astimezone(zone).date()
        if view in ("today", "week", "overdue", "upcoming"):
            filtered = []
            for task in items:
                anchor = task["due_at"] or task["starts_at"]
                day = datetime.fromisoformat(anchor).astimezone(zone).date() if anchor else None
                if not day and task["due_date"]:
                    day = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
                selected = (view == "today" and day == today or
                            view == "week" and day and today <= day <= today + timedelta(days=7) or
                            view == "overdue" and day and (datetime.fromisoformat(anchor) < now if anchor else day < today) or
                            view == "upcoming" and day and day >= today)
                if selected and task["status"] in ("pending", "in_progress"):
                    filtered.append(task)
            return filtered
        return items

    def patch(self, task_id, expected_version, changes):
        if not isinstance(changes, dict) or set(changes) - {"title", "kind", "priority", "temporal", "notes", "status", "application_id", "event_details"}:
            raise AppError("VALIDATION_ERROR", "任务包含不支持的字段", 422)
        with self.db.task_lock(task_id), self.db.tx() as session:
            task = get_record(session, Task, task_id)
            check_version(task, expected_version)
            if "status" in changes:
                if task.status not in ("pending", "in_progress") or changes["status"] not in ("pending", "in_progress"):
                    raise AppError("STATE_CONFLICT", "完成、取消和重开请使用对应操作", 409)
                task.status = changes["status"]
            if changes.get("application_id"):
                get_record(session, Application, changes["application_id"])
            original = {key: getattr(task, key) for key in ("title", "kind", "priority", "temporal", "notes", "application_id", "event_details")}
            self._apply(task, validated(TaskInput, {**original, **{key: value for key, value in changes.items() if key != "status"}}))
            task.manual_fields = sorted(set(task.manual_fields) | set(changes))
            task.version += 1
            if any(key in changes and changes[key] != original[key] for key in ("kind", "temporal")):
                task.schedule_revision += 1
                self.rebuild(session, task)
            audit(session, task, "task.changed", {"fields": list(changes)})
            return self.serialize(task, session)

    def transition(self, task_id, expected_version, action):
        with self.db.task_lock(task_id), self.db.tx() as session:
            task = get_record(session, Task, task_id)
            check_version(task, expected_version)
            status = {"complete": "done", "cancel": "cancelled", "reopen": "pending"}[action]
            if task.status == status:
                raise AppError("STATE_CONFLICT", "任务已处于该状态", 409)
            task.status, task.updated_at = status, millis(self.clock())
            task.version += 1
            task.schedule_revision += 1
            self.rebuild(session, task)
            audit(session, task, "task." + action)
            return self.serialize(task, session)
