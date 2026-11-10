import threading
import sys
import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.domain import AppError, millis, uid, utcnow

SCHEMA_REVISION = "0003_parse_cache"


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    created_at: Mapped[int] = mapped_column(Integer, default=lambda: millis(utcnow()))
    updated_at: Mapped[int] = mapped_column(Integer, default=lambda: millis(utcnow()))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Task(Record, Base):
    __tablename__ = "tasks"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    temporal: Mapped[dict] = mapped_column(JSON)
    due_at: Mapped[int | None] = mapped_column(Integer, index=True)
    starts_at: Mapped[int | None] = mapped_column(Integer, index=True)
    ends_at: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[str | None] = mapped_column(String)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_fields: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[dict | None] = mapped_column(JSON)
    action_key: Mapped[str | None] = mapped_column(String, unique=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("version >= 1"), CheckConstraint("status IN ('pending','in_progress','done','cancelled')"),
                     CheckConstraint("priority IN (1,2,3)"))


class Reminder(Record, Base):
    __tablename__ = "reminders"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer)
    offset_seconds: Mapped[int] = mapped_column(Integer)
    scheduled_for: Mapped[int] = mapped_column(Integer)
    fire_at: Mapped[int] = mapped_column(Integer, index=True)
    expires_at: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    channel: Mapped[str] = mapped_column(String, default="windows")
    next_attempt_at: Mapped[int | None] = mapped_column(Integer)
    lease_until: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    parent_reminder_id: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)


class Application(Record, Base):
    __tablename__ = "applications"
    company: Mapped[str] = mapped_column(String)
    position: Mapped[str | None] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String, default="applied")
    source: Mapped[str] = mapped_column(String, default="manual")
    manual_fields: Mapped[list] = mapped_column(JSON, default=list)
    closed_at: Mapped[int | None] = mapped_column(Integer)


class Attempt(Record, Base):
    __tablename__ = "notification_attempts"
    reminder_id: Mapped[str] = mapped_column(ForeignKey("reminders.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[int] = mapped_column(Integer)
    finished_at: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("uq_attempt", "reminder_id", "attempt_no", unique=True),)


class Notification(Record, Base):
    __tablename__ = "notification_items"
    reminder_id: Mapped[str | None] = mapped_column(String, unique=True)
    task_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="reminder")
    state: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    dismissed_at: Mapped[int | None] = mapped_column(Integer)


class Email(Record, Base):
    __tablename__ = "email_messages"
    canonical_digest: Mapped[str] = mapped_column(String, index=True)
    message_id_header: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    from_addr: Mapped[str] = mapped_column(String)
    header_date: Mapped[str | None] = mapped_column(String)
    internal_date: Mapped[str | None] = mapped_column(String)
    text_excerpt: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String)
    text_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    links: Mapped[list] = mapped_column(JSON, default=list)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String, default="new", index=True)
    ignored_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[dict] = mapped_column(JSON, default=dict)


class MailCache(Base):
    __tablename__ = "mail_parse_cache"
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)


class Occurrence(Record, Base):
    __tablename__ = "mail_occurrences"
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"))
    occurrence_key: Mapped[str] = mapped_column(String, unique=True)
    account_id: Mapped[str | None] = mapped_column(String)
    folder: Mapped[str | None] = mapped_column(String)
    uidvalidity: Mapped[str | None] = mapped_column(String)
    uid: Mapped[int | None] = mapped_column(Integer)


class Candidate(Record, Base):
    __tablename__ = "review_items"
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"), index=True)
    candidate: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, default="open", index=True)
    task_id: Mapped[str | None] = mapped_column(String)
    action_key: Mapped[str] = mapped_column(String, unique=True)


class Account(Record, Base):
    __tablename__ = "mail_accounts"
    provider: Mapped[str] = mapped_column(String, default="qq")
    address: Mapped[str] = mapped_column(String, unique=True)
    credential_ref: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="ready")
    initial_scan: Mapped[str] = mapped_column(String, default="14")
    scan_from: Mapped[int | None] = mapped_column(Integer)
    uidvalidity: Mapped[str | None] = mapped_column(String)
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    initial_upper_uid: Mapped[int | None] = mapped_column(Integer)
    last_sync_at: Mapped[int | None] = mapped_column(Integer)
    next_sync_at: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String)
    last_job_id: Mapped[str | None] = mapped_column(String)


class Job(Record, Base):
    __tablename__ = "maintenance_jobs"
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String)


class Setting(Record, Base):
    __tablename__ = "settings"
    value: Mapped[dict] = mapped_column(JSON)


class Command(Record, Base):
    __tablename__ = "api_commands"
    route: Mapped[str] = mapped_column(String)
    request_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[int] = mapped_column(Integer)
    __table_args__ = (Index("uq_api_command", "route", "request_key", unique=True),)


class Audit(Record, Base):
    __tablename__ = "audit_events"
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Artifact(Record, Base):
    __tablename__ = "artifacts"
    name: Mapped[str] = mapped_column(String)
    media_type: Mapped[str] = mapped_column(String)
    expires_at: Mapped[int] = mapped_column(Integer)


class Token(Record, Base):
    __tablename__ = "maintenance_tokens"
    kind: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[int] = mapped_column(Integer)
    value: Mapped[dict] = mapped_column(JSON)


class Database:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "data.sqlite"
        self.write_lock = threading.RLock()
        self.local = threading.local()
        self.activity_condition = threading.Condition()
        self.active_operations = 0
        self.maintenance_active = False
        self.task_locks = {}
        self.log = lambda *args, **kwargs: None
        self.closed = False
        self.generation = 0
        self.request_generation = ContextVar("database_generation", default=None)
        self.on_replace = lambda: None
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
                                    connect_args={"check_same_thread": False, "timeout": 5})
        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        config = Config()
        migration_root = (Path(sys._MEIPASS) / "backend" if getattr(sys, "frozen", False)
                          else Path(__file__).parent.parent)
        config.set_main_option("script_location", str(migration_root / "migrations"))
        self._migrate(config)

    def _migrate(self, config):
        previous = None
        if self.path.exists() and self.path.stat().st_size:
            with closing(sqlite3.connect(str(self.path))) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                revisions = list(connection.execute("SELECT version_num FROM alembic_version")) if "alembic_version" in tables else []
                if tables and (len(revisions) != 1 or revisions[0][0] not in ("0001_p0", "0002_applications", SCHEMA_REVISION)):
                    raise AppError("DATABASE_VERSION_UNSUPPORTED", "数据库版本不兼容，请使用对应版本或保留备份后恢复", 503)
                previous = revisions[0][0] if revisions else None
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AppError("DATABASE_INVALID", "数据库校验失败，请保留数据并从备份恢复", 503)
        if previous == SCHEMA_REVISION:
            return
        backup, trial = None, None
        live_started = False
        try:
            if previous:
                backup_dir, stage_dir = self.directory / "backups", self.directory / "staging"
                backup_dir.mkdir(exist_ok=True)
                stage_dir.mkdir(exist_ok=True)
                backup = backup_dir / ("pre-upgrade-" + uid() + ".sqlite")
                trial = stage_dir / ("migration-" + uid() + ".sqlite")
                self._copy_database(self.path, backup)
                self._copy_database(backup, trial)
                trial_engine = create_engine(f"sqlite:///{trial.as_posix()}")
                try:
                    with trial_engine.connect() as connection:
                        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                        connection.commit()
                        config.attributes["connection"] = connection
                        command.upgrade(config, SCHEMA_REVISION)
                        if (connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok" or
                                connection.exec_driver_sql("PRAGMA foreign_key_check").first() or
                                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() != SCHEMA_REVISION):
                            raise ValueError("Migration integrity check failed")
                finally:
                    trial_engine.dispose()
            live_started = True
            with self.engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, SCHEMA_REVISION)
                if (connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok" or
                        connection.exec_driver_sql("PRAGMA foreign_key_check").first() or
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() != SCHEMA_REVISION):
                    raise ValueError("Migration integrity check failed")
        except Exception:
            self.engine.dispose()
            if live_started and backup:
                try:
                    self._copy_database(backup, self.path)
                except Exception:
                    raise AppError("MIGRATION_RECOVERY_REQUIRED", "升级未完成，请保留数据库和 backups 中的升级前快照以恢复", 503) from None
            raise AppError("MIGRATION_FAILED", "升级未完成，原数据已保留；请查看 backups 中的恢复点", 503) from None
        finally:
            if trial:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(trial) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _copy_database(source, target):
        with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(target))) as dst:
            src.backup(dst)

    def task_lock(self, task_id):
        with self.write_lock:
            return self.task_locks.setdefault(task_id, threading.RLock())

    def close(self):
        with self.write_lock:
            self.closed = True
            self.engine.dispose()

    def replaced(self):
        self.generation += 1
        self.on_replace()
        self.request_generation.set(self.generation)

    def _check_access(self):
        if self.closed:
            raise AppError("APP_STOPPING", "应用正在退出，请重新打开后继续", 503)
        expected = self.request_generation.get()
        if expected is not None and expected != self.generation:
            raise AppError("SESSION_REQUIRED", "数据已恢复或重置，请从托盘重新打开", 401)

    @contextmanager
    def read(self):
        with self.write_lock, self.Session() as session:
            self._check_access()
            yield session

    @contextmanager
    def activity(self):
        with self.activity_condition:
            while self.maintenance_active:
                self.activity_condition.wait()
            self.active_operations += 1
        try:
            yield
        finally:
            with self.activity_condition:
                self.active_operations -= 1
                self.activity_condition.notify_all()

    @contextmanager
    def maintenance(self):
        with self.activity_condition:
            while self.maintenance_active:
                self.activity_condition.wait()
            self.maintenance_active = True
            while self.active_operations:
                self.activity_condition.wait()
        try:
            with self.write_lock:
                yield
        finally:
            with self.activity_condition:
                self.maintenance_active = False
                self.activity_condition.notify_all()

    @contextmanager
    def tx(self):
        active = getattr(self.local, "session", None)
        if active is not None:
            yield active
            return
        with self.write_lock, self.Session() as session:
            self._check_access()
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self.local.session = session
            try:
                yield session
                session.commit()
                for event in session.info.pop("events", []):
                    self.log(event["event"], module=event["module"], entity_id=event["entity_id"])
            except Exception:
                session.rollback()
                raise
            finally:
                self.local.session = None


def get_record(session, cls, record_id):
    record = session.get(cls, record_id)
    if not record:
        raise AppError("NOT_FOUND", "记录不存在", 404)
    return record


def check_version(record, expected):
    if type(expected) is not int or expected != record.version:
        raise AppError("VERSION_CONFLICT", "数据已更新，请刷新后重试", 409, current_version=record.version)


def audit(session, record, action, detail=None):
    session.add(Audit(entity_type=record.__tablename__, entity_id=record.id, action=action, detail=detail or {}))
    session.info.setdefault("events", []).append({"event": action, "entity_id": record.id,
                                                 "module": "tasks" if action.startswith("task.") else "mail"})
