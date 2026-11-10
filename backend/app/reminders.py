import sys

from sqlalchemy import select

from app.domain import AppError, iso, millis, uid
from app.persistence import Attempt, Notification, Reminder, Task, check_version, get_record


class WindowsNotifier:
    def __call__(self, payload):
        if sys.platform != "win32":
            return {"status": "unavailable", "error_code": "WINDOWS_ONLY"}
        try:
            from windows_toasts import Toast, WindowsToaster
            toast = Toast()
            toast.text_fields = ["OfferPilot", payload["title"], payload.get("body", "请打开 OfferPilot 查看详情")]
            WindowsToaster("OfferPilot", notifierAUMID="OfferPilot.Lite").show_toast(toast)
            return {"status": "sent"}
        except ImportError:
            return {"status": "unavailable", "error_code": "NOTIFICATION_UNAVAILABLE"}
        except Exception:
            # Once handed to the OS, exceptions cannot prove non-delivery.
            return {"status": "unknown", "error_code": "NOTIFICATION_RESULT_UNKNOWN"}


class ReminderService:
    def __init__(self, db, clock, settings, notifier):
        self.db, self.clock, self.settings, self.notifier = db, clock, settings, notifier

    def _project(self, session, reminder, task):
        item = session.scalar(select(Notification).where(Notification.reminder_id == reminder.id))
        if item is None:
            item = Notification(reminder_id=reminder.id, task_id=task.id, title=task.title,
                                state=reminder.state, detail={})
            session.add(item)
        item.state = reminder.state
        item.detail = {"error_code": reminder.error_code, "scheduled_for": iso(reminder.scheduled_for)}

    def recover(self):
        now = millis(self.clock())
        with self.db.tx() as session:
            for reminder in session.scalars(select(Reminder).where(Reminder.state == "claimed")):
                attempt = session.scalar(select(Attempt).where(Attempt.reminder_id == reminder.id,
                                                               Attempt.finished_at.is_(None)))
                if attempt:
                    reminder.state = "delivery_unknown"
                    reminder.error_code = "INTERRUPTED_DURING_DELIVERY"
                    attempt.result, attempt.finished_at = "delivery_unknown", now
                    self._project(session, reminder, get_record(session, Task, reminder.task_id))
                else:
                    reminder.state = "pending"
                reminder.lease_until = None

    def deliver(self, payload):
        if not self.settings.get()["notifications_enabled"]:
            return {"status": "unavailable", "error_code": "NOTIFICATIONS_DISABLED"}
        try:
            result = self.notifier(payload)
            if isinstance(result, str):
                result = {"status": result}
            return result or {"status": "sent"}
        except TimeoutError:
            return {"status": "unknown", "error_code": "DELIVERY_TIMEOUT"}
        except Exception:
            return {"status": "unknown", "error_code": "DELIVERY_EXCEPTION"}

    def run_once(self):
        with self.db.activity():
            self._run_once()

    def _run_once(self):
        now = millis(self.clock())
        missed = set()
        # Expired templates form one recovery batch even when there are more than 50 rows.
        with self.db.tx() as session:
            for reminder in session.scalars(select(Reminder).where(
                    Reminder.state.in_(["pending", "retry_wait"]), Reminder.expires_at <= now)):
                task = get_record(session, Task, reminder.task_id)
                if task.status not in ("pending", "in_progress") or task.schedule_revision != reminder.schedule_revision:
                    reminder.state = "suppressed"
                else:
                    reminder.state = "missed"
                    self._project(session, reminder, task)
                    missed.add(task.id)
        with self.db.read() as session:
            rows = [(r.id, r.task_id) for r in session.scalars(select(Reminder).where(
                Reminder.state.in_(["pending", "retry_wait", "claimed"]), Reminder.fire_at <= now)
                .order_by(Reminder.fire_at, Reminder.id).limit(50))]
        for reminder_id, task_id in rows:
            with self.db.task_lock(task_id):
                with self.db.tx() as session:
                    reminder = get_record(session, Reminder, reminder_id)
                    task = get_record(session, Task, task_id)
                    if reminder.state == "claimed":
                        if reminder.lease_until and reminder.lease_until > now:
                            continue
                        active = session.scalar(select(Attempt).where(Attempt.reminder_id == reminder.id,
                                                                     Attempt.finished_at.is_(None)))
                        if active:
                            reminder.state = "delivery_unknown"
                            self._project(session, reminder, task)
                            continue
                        reminder.state = "pending"
                    if reminder.state not in ("pending", "retry_wait"):
                        continue
                    if task.status not in ("pending", "in_progress") or task.schedule_revision != reminder.schedule_revision:
                        reminder.state = "suppressed"
                        continue
                    if now >= reminder.expires_at:
                        reminder.state = "missed"
                        self._project(session, reminder, task)
                        missed.add(task_id)
                        continue
                    if reminder.next_attempt_at and now < reminder.next_attempt_at:
                        continue
                    reminder.state, reminder.lease_until = "claimed", now + 60000
                    reminder.attempt_count += 1
                    attempt = Attempt(reminder_id=reminder.id, attempt_no=reminder.attempt_count, started_at=now)
                    session.add(attempt)
                    session.flush()
                    attempt_id = attempt.id
                    title = (task.title if self.settings.get(session)["notification_privacy"] == "details"
                             else "你有一项待办即将到时")
                    payload = {"title": title, "task_id": task.id, "body": "任务时间临近，请打开 OfferPilot 查看安排"}
                # No SQL transaction spans OS delivery. Per-task lock orders cancellation against sending.
                result = self.deliver(payload)
                with self.db.tx() as session:
                    reminder = get_record(session, Reminder, reminder_id)
                    attempt = get_record(session, Attempt, attempt_id)
                    now_finished = millis(self.clock())
                    status = result.get("status", "unknown")
                    if status == "sent":
                        reminder.state = "sent"
                    elif status in ("retryable", "not_delivered"):
                        if reminder.attempt_count < 3 and now_finished < reminder.expires_at:
                            reminder.state = "retry_wait"
                            reminder.next_attempt_at = now_finished + (30000 if reminder.attempt_count == 1 else 120000)
                        else:
                            reminder.state = "failed"
                    elif status in ("unavailable", "failed", "permanent_failure"):
                        reminder.state = "failed"
                    else:
                        reminder.state = "delivery_unknown"
                    reminder.error_code = result.get("error_code")
                    reminder.lease_until = None
                    reminder.version += 1
                    attempt.result, attempt.finished_at = reminder.state, now_finished
                    self._project(session, reminder, get_record(session, Task, task_id))
        if missed:
            # Persist before attempting one summary; a crash never replays a historical batch.
            with self.db.tx() as session:
                item = Notification(kind="recovery", state="delivery_unknown", title=f"有 {len(missed)} 项任务可能错过提醒",
                                    detail={"task_ids": sorted(missed)})
                session.add(item)
                session.flush()
                item_id, title = item.id, item.title
            result = self.deliver({"title": title, "body": "请打开 OfferPilot 查看任务"})
            with self.db.tx() as session:
                item = get_record(session, Notification, item_id)
                item.state = "sent" if result.get("status") == "sent" else "delivery_unknown"

    def list_notifications(self, state=None):
        with self.db.read() as session:
            query = select(Notification).order_by(Notification.created_at.desc(), Notification.id)
            if state != "all":
                query = query.where(Notification.dismissed_at.is_(None))
            return [{"id": n.id, "version": n.version, "kind": n.kind, "title": n.title, "state": n.state,
                     "task_id": n.task_id, "reminder_id": n.reminder_id, "detail": n.detail,
                     "created_at": iso(n.created_at), "dismissed_at": iso(n.dismissed_at)}
                    for n in session.scalars(query)]

    def dismiss(self, item_id, expected_version):
        with self.db.tx() as session:
            item = get_record(session, Notification, item_id)
            check_version(item, expected_version)
            item.dismissed_at, item.version = millis(self.clock()), item.version + 1
            return {"id": item.id, "version": item.version, "dismissed_at": iso(item.dismissed_at)}

    def test(self):
        result = self.deliver({"title": "OfferPilot 测试提醒", "body": "系统通知渠道测试，请检查桌面通知"})
        with self.db.tx() as session:
            session.add(Notification(kind="test", state=result["status"], title="测试通知", detail=result))
        return result

    def snooze(self, reminder_id, expected_version, minutes):
        if minutes not in (10, 30, 60):
            raise AppError("VALIDATION_ERROR", "仅支持延后 10、30、60 分钟", 422)
        with self.db.read() as session:
            task_id = get_record(session, Reminder, reminder_id).task_id
        with self.db.task_lock(task_id), self.db.tx() as session:
            reminder = get_record(session, Reminder, reminder_id)
            check_version(reminder, expected_version)
            task = get_record(session, Task, reminder.task_id)
            anchor = task.starts_at or task.due_at
            fire = millis(self.clock()) + minutes * 60000
            if task.status not in ("pending", "in_progress") or not anchor or fire >= anchor:
                raise AppError("STATE_CONFLICT", "延后时间必须早于任务时间", 409)
            reminder.state, reminder.version = "suppressed", reminder.version + 1
            new = Reminder(task_id=task.id, schedule_revision=task.schedule_revision, offset_seconds=0,
                           scheduled_for=fire, fire_at=fire, expires_at=min(fire + 600000, anchor),
                           parent_reminder_id=reminder.id, idempotency_key=reminder.id + ":snooze:" + uid())
            session.add(new)
            session.flush()
            return {"id": new.id, "version": new.version, "fire_at": iso(fire), "state": new.state}
