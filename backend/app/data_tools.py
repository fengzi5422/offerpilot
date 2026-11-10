import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import zipfile
from contextlib import closing
from pathlib import Path

from sqlalchemy import delete, select

from app.domain import AppError, iso, millis, uid
from app.persistence import (Account, Application, Artifact, Base, Candidate, Email, SCHEMA_REVISION, Setting, Task, Token, get_record)

MAX_BACKUP_BYTES = 128 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(source, target):
    with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(target))) as dst:
        src.backup(dst)


class DataTools:
    def __init__(self, db, clock, tasks, vault):
        self.db, self.clock, self.tasks, self.vault = db, clock, tasks, vault
        self.artifact_dir = db.directory / "artifacts"
        self.staging_dir = db.directory / "staging"
        self.rollback_dir = db.directory / "backups"
        self.backup_lock = threading.Lock()
        for directory in (self.artifact_dir, self.staging_dir, self.rollback_dir):
            directory.mkdir(exist_ok=True)

    def _artifact(self, content, extension, media_type):
        artifact_id = uid()
        name = artifact_id + "." + extension
        (self.artifact_dir / name).write_bytes(content)
        with self.db.tx() as session:
            session.add(Artifact(id=artifact_id, name=name, media_type=media_type,
                                 expires_at=millis(self.clock()) + 86400000))
        return {"artifact_id": artifact_id, "filename": "offerpilot." + extension, "size": len(content)}

    def artifact(self, artifact_id):
        with self.db.read() as session:
            artifact = get_record(session, Artifact, artifact_id)
            if artifact.expires_at < millis(self.clock()):
                raise AppError("ARTIFACT_EXPIRED", "下载已过期，请重新生成", 404)
            path = (self.artifact_dir / artifact.name).resolve()
            if path.parent != self.artifact_dir.resolve() or not path.is_file():
                raise AppError("NOT_FOUND", "文件不存在", 404)
            return path, artifact.media_type

    def export(self, format_name, entities=None):
        if format_name not in ("csv", "json"):
            raise AppError("VALIDATION_ERROR", "导出格式无效", 422)
        if entities is not None and (not isinstance(entities, list) or set(entities) - {"tasks", "applications"}):
            raise AppError("VALIDATION_ERROR", "导出对象无效", 422)
        entities = entities or ["tasks", "applications"]
        tasks = self.tasks.list() if "tasks" in entities else []
        from app.applications import ApplicationService
        applications = ApplicationService(self.db, self.clock).list()
        by_id = {application["id"]: application for application in applications}
        exported_applications = applications if "applications" in entities else []
        if format_name == "json":
            data = {"schema_version": 1, "exported_at": iso(self.clock()), "tasks": tasks, "applications": exported_applications}
            return self._artifact(json.dumps(data, ensure_ascii=False, indent=2).encode(), "json", "application/json")
        columns = ["id", "company", "position", "title", "kind", "status", "priority", "due_at_utc", "due_date",
                   "starts_at_utc", "timezone", "precision", "created_at_utc"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, columns)
        writer.writeheader()
        rows = tasks + [{"id": a["id"], "company": a["company"], "position": a["position"],
                         "title": a["position"] or a["company"], "kind": "application", "status": a["stage"],
                         "due_at": None, "starts_at": None, "temporal": {"precision": "unknown"},
                         "created_at": a["created_at"]} for a in exported_applications]
        for task in rows:
            row = {key: task.get(key, "") or "" for key in columns}
            application = by_id.get(task.get("application_id"))
            if application:
                row.update(company=application["company"], position=application["position"] or "")
            row.update(due_at_utc=task["due_at"] or "", starts_at_utc=task["starts_at"] or "",
                       timezone=task["temporal"].get("timezone") or "", precision=task["temporal"]["precision"],
                       created_at_utc=task["created_at"])
            for key, value in row.items():
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    row[key] = "'" + value
            writer.writerow(row)
        return self._artifact(stream.getvalue().encode("utf-8-sig"), "csv", "text/csv; charset=utf-8")

    def backup(self, include_evidence=False):
        return self._artifact(self._backup_content(include_evidence), "zip", "application/zip")

    def automatic_backup(self):
        """Keep five daily sanitized snapshots; manual and rollback files are independent."""
        with self.backup_lock, self.db.activity():
            copies = sorted((path for path in self.rollback_dir.iterdir()
                             if re.fullmatch(r"automatic-\d{13}-[0-9a-f-]{36}\.zip", path.name)
                             and path.is_file() and not path.is_symlink()), key=lambda path: path.name)
            now = millis(self.clock())
            if copies and now - int(copies[-1].name.split("-")[1]) < 86400000:
                return None
            name = f"automatic-{now:013d}-{uid()}.zip"
            target = self.rollback_dir / name
            partial = target.with_suffix(".partial")
            try:
                partial.write_bytes(self._backup_content(False))
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
            for old in (copies + [target])[:-5]:
                old.unlink(missing_ok=True)
            return {"filename": name, "created_at": iso(self.clock())}

    def _backup_content(self, include_evidence=False):
        stage = self.staging_dir / (uid() + ".sqlite")
        try:
            with self.db.write_lock:
                snapshot(self.db.path, stage)
            with closing(sqlite3.connect(str(stage))) as connection:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("UPDATE mail_accounts SET credential_ref=NULL,state='auth_required',last_job_id=NULL")
                for table in ("api_commands", "maintenance_tokens", "artifacts", "maintenance_jobs"):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("UPDATE reminders SET state='delivery_unknown',lease_until=NULL WHERE state='claimed'")
                connection.execute("UPDATE notification_attempts SET result='delivery_unknown',finished_at=? WHERE finished_at IS NULL",
                                   (millis(self.clock()),))
                if not include_evidence:
                    connection.execute("UPDATE email_messages SET text_excerpt='',links='[]',attachments='[]'")
                    for candidate_id, encoded in connection.execute("SELECT id,candidate FROM review_items").fetchall():
                        candidate = json.loads(encoded)
                        candidate["evidence"] = []
                        connection.execute("UPDATE review_items SET candidate=? WHERE id=?", (json.dumps(candidate), candidate_id))
                    for task_id, encoded in connection.execute("SELECT id,source FROM tasks WHERE source IS NOT NULL").fetchall():
                        source = json.loads(encoded)
                        if source:
                            source["evidence"] = []
                            source["evidence_unavailable"] = True
                            connection.execute("UPDATE tasks SET source=? WHERE id=?", (json.dumps(source), task_id))
                settings = connection.execute("SELECT value FROM settings WHERE id='profile'").fetchone()
                if settings:
                    value = json.loads(settings[0])
                    value["autostart_enabled"] = False
                    connection.execute("UPDATE settings SET value=? WHERE id='profile'", (json.dumps(value),))
                connection.commit()
                connection.execute("VACUUM")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AppError("BACKUP_INVALID", "备份完整性校验失败", 503)
                counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("tasks", "applications", "email_messages", "mail_accounts")}
            content = stage.read_bytes()
            manifest = {"format_version": 1, "app_version": "0.1.0", "schema_revision": SCHEMA_REVISION,
                        "created_at": iso(self.clock()), "files": [{"name": "data.sqlite", "size": len(content),
                        "sha256": digest(content)}], "record_counts": counts, "contains_evidence": bool(include_evidence),
                        "credentials_included": False}
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                archive.writestr("data.sqlite", content)
            return buffer.getvalue()
        finally:
            stage.unlink(missing_ok=True)

    def _fingerprint(self):
        with self.db.read() as session:
            state = []
            for cls in (Task, Email, Account, Setting, Application):
                state.extend((cls.__tablename__, record.id, record.version)
                             for record in session.scalars(select(cls).order_by(cls.id)))
            return digest(json.dumps(state).encode())

    def _check_sqlite(self, path):
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA trusted_schema=OFF")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError()
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError()
                if connection.execute("SELECT version_num FROM alembic_version").fetchone() != (SCHEMA_REVISION,):
                    raise ValueError()
                actual = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
                with closing(sqlite3.connect(str(self.db.path))) as current:
                    expected = dict(current.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
                if actual != expected:
                    raise ValueError()
                for row in connection.execute("SELECT title,kind,priority,temporal,notes,status FROM tasks"):
                    from app.domain import TaskInput, validated
                    validated(TaskInput, {"title": row[0], "kind": row[1], "priority": row[2],
                                          "temporal": json.loads(row[3]), "notes": row[4]})
                    if row[5] not in ("pending", "in_progress", "done", "cancelled"):
                        raise ValueError()
        except (sqlite3.DatabaseError, ValueError, TypeError, AppError):
            raise AppError("BACKUP_SCHEMA_INVALID", "备份数据库结构或完整性校验失败", 422) from None

    def validate_restore(self, content):
        if len(content) > MAX_BACKUP_BYTES:
            raise AppError("FILE_TOO_LARGE", "备份文件不能超过 128 MiB", 413)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)) or set(names) != {"manifest.json", "data.sqlite"}:
                    raise ValueError()
                if sum(entry.file_size for entry in entries) > MAX_BACKUP_BYTES:
                    raise ValueError()
                if any(entry.flag_bits & 1 for entry in entries) or archive.getinfo("manifest.json").file_size > 65536:
                    raise ValueError()
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format_version") != 1 or manifest.get("schema_revision") != SCHEMA_REVISION:
                    raise AppError("BACKUP_VERSION_UNSUPPORTED", "备份版本不兼容，请使用对应版本应用", 422)
                if manifest.get("credentials_included") is not False or len(manifest.get("files", [])) != 1:
                    raise ValueError()
                data = archive.read("data.sqlite")
                file = manifest["files"][0]
                if file.get("name") != "data.sqlite" or file.get("size") != len(data) or file.get("sha256") != digest(data):
                    raise ValueError()
        except AppError:
            raise
        except (zipfile.BadZipFile, ValueError, KeyError, TypeError, RuntimeError, OSError):
            raise AppError("BACKUP_INVALID", "备份损坏、文件内容不受支持或校验失败", 422) from None
        restore_id, confirmation = uid(), secrets.token_urlsafe(32)
        path = self.staging_dir / (restore_id + ".sqlite")
        path.write_bytes(data)
        try:
            self._check_sqlite(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        value = {"restore_id": restore_id, "manifest": manifest, "file_hash": digest(data),
                 "fingerprint": self._fingerprint(), "confirmation_token": confirmation}
        with self.db.tx() as session:
            session.add(Token(id=restore_id, kind="restore", token_hash=digest(confirmation.encode()),
                              expires_at=millis(self.clock()) + 600000, value=value))
        return {"restore_id": restore_id, "record_counts": manifest["record_counts"]}

    def restore_preview(self, restore_id):
        with self.db.read() as session:
            record = get_record(session, Token, restore_id)
            if record.kind != "restore" or record.expires_at <= millis(self.clock()):
                raise AppError("TOKEN_EXPIRED", "恢复校验已过期，请重新上传", 409)
            return record.value

    def apply_restore(self, restore_id, confirmation_token):
        with self.db.maintenance():
            value = self.restore_preview(restore_id)
            if not secrets.compare_digest(value["confirmation_token"], str(confirmation_token)):
                raise AppError("CONFIRMATION_REQUIRED", "恢复确认无效", 403)
            if value["fingerprint"] != self._fingerprint():
                raise AppError("DATABASE_CHANGED", "数据已变化，请重新校验备份后确认恢复", 409)
            path = self.staging_dir / (restore_id + ".sqlite")
            if not path.is_file() or digest(path.read_bytes()) != value["file_hash"]:
                raise AppError("BACKUP_INVALID", "待恢复文件已变化", 422)
            self._check_sqlite(path)
            rollback = self.rollback_dir / ("pre-restore-" + uid() + ".sqlite")
            snapshot(self.db.path, rollback)
            # Re-sanitize supplied databases; manifest assertions alone are not trusted.
            with closing(sqlite3.connect(str(path))) as connection:
                connection.execute("UPDATE mail_accounts SET credential_ref=NULL,state='auth_required',last_job_id=NULL")
                connection.execute("DELETE FROM api_commands")
                connection.execute("DELETE FROM maintenance_tokens")
                connection.execute("UPDATE reminders SET state='delivery_unknown',lease_until=NULL WHERE state='claimed'")
                connection.commit()
            self.db.engine.dispose()
            for suffix in ("-wal", "-shm"):
                Path(str(self.db.path) + suffix).unlink(missing_ok=True)
            try:
                os.replace(path, self.db.path)
            except OSError:
                shutil.copyfile(rollback, self.db.path)
                raise AppError("RESTORE_FAILED", "恢复替换失败，已保留恢复点", 503) from None
            return {"restored": True, "reauthorization_required": True, "session_reset": True}

    def privacy_preview(self, scope):
        if scope not in ("mail_cache", "source_evidence", "all"):
            raise AppError("VALIDATION_ERROR", "清理范围无效", 422)
        token_id, secret = uid(), secrets.token_urlsafe(32)
        with self.db.read() as session:
            counts = {"tasks": len(list(session.scalars(select(Task.id)))),
                      "emails": len(list(session.scalars(select(Email.id)))),
                      "accounts": len(list(session.scalars(select(Account.id))))}
        with self.db.tx() as session:
            session.add(Token(id=token_id, kind="privacy", token_hash=digest(secret.encode()),
                              expires_at=millis(self.clock()) + 600000,
                              value={"scope": scope, "counts": counts, "fingerprint": self._fingerprint()}))
        return {"scope": scope, "counts": counts, "preview_token": token_id + "." + secret,
                "downloaded_files_remain": True}

    def purge(self, scope, preview_token):
        with self.db.maintenance():
            return self._purge(scope, preview_token)

    def _purge(self, scope, preview_token):
        parts = str(preview_token).split(".", 1)
        if len(parts) != 2:
            raise AppError("CONFIRMATION_REQUIRED", "请先预览清理范围", 403)
        with self.db.tx() as session:
            token = get_record(session, Token, parts[0])
            if (token.kind != "privacy" or token.value["scope"] != scope or token.expires_at <= millis(self.clock()) or
                    not secrets.compare_digest(token.token_hash, digest(parts[1].encode()))):
                raise AppError("CONFIRMATION_REQUIRED", "清理预览已过期", 403)
            if token.value["fingerprint"] != self._fingerprint():
                raise AppError("DATABASE_CHANGED", "数据已变化，请重新预览", 409)
            if scope == "all":
                references = [a.credential_ref for a in session.scalars(select(Account)) if a.credential_ref]
                for reference in references:
                    self.vault.delete(reference)
                for table in reversed(Base.metadata.sorted_tables):
                    session.execute(delete(table))
                from app.tasks import DEFAULT_SETTINGS
                session.add(Setting(id="profile", value=DEFAULT_SETTINGS.copy()))
            else:
                for mail in session.scalars(select(Email)):
                    mail.text_excerpt, mail.links, mail.attachments = "", [], []
                    mail.version += 1
                    if scope == "source_evidence":
                        mail.subject, mail.from_addr, mail.message_id_header = "来源已清理", "", None
                if scope == "source_evidence":
                    for candidate in session.scalars(select(Candidate)):
                        candidate.candidate = {**candidate.candidate, "evidence": []}
                    for task in session.scalars(select(Task)):
                        if task.source:
                            task.source = {"source_unavailable": True}
                            task.version += 1
                session.delete(token)
        if scope == "all":
            for directory in (self.artifact_dir, self.staging_dir, self.rollback_dir):
                for path in directory.iterdir():
                    if path.is_file() and path.resolve().parent == directory.resolve():
                        path.unlink(missing_ok=True)
        return {"scope": scope, "purged": True, "downloaded_files_remain": True}
