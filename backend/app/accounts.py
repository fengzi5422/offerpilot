import random
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from sqlalchemy import select

from app.domain import AppError, iso, millis, uid
from app.mail_providers import MailProviderAdapter, provider_for_address, validate_authorization_code
from app.persistence import Account, Job, check_version, get_record


class WindowsCredentialStore:
    """Never accepts plaintext-file or third-party fallback keyring backends."""
    def _backend(self):
        if sys.platform != "win32":
            raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "邮箱授权需要 Windows 凭据管理器", 503)
        try:
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
        except Exception:
            raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "Windows 凭据管理器不可用", 503) from None

    def set(self, key, value):
        try:
            self._backend().set_password("OfferPilot", key, value)
        except AppError:
            raise
        except Exception:
            raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "无法安全保存授权码", 503) from None

    def get(self, key):
        try:
            return self._backend().get_password("OfferPilot", key)
        except AppError:
            raise
        except Exception:
            raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "无法读取授权码，请重新授权", 503) from None

    def delete(self, key):
        if not key:
            return
        backend = self._backend()
        try:
            if backend.get_password("OfferPilot", key) is not None:
                backend.delete_password("OfferPilot", key)
        except Exception:
            raise AppError("CREDENTIAL_STORE_UNAVAILABLE", "无法删除授权码，请检查凭据管理器", 503) from None


def _imap_tree(data):
    tokens = re.findall(rb'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', data)
    index = 0
    def read():
        nonlocal index
        token = tokens[index]
        index += 1
        if token == b"(":
            items = []
            while index < len(tokens) and tokens[index] != b")":
                items.append(read())
            index += 1
            return items
        if token.upper() == b"NIL":
            return None
        return token.strip(b'"').decode("utf-8", errors="replace")
    return read()


def _text_parts(tree, prefix=""):
    parts = []
    if not isinstance(tree, list) or len(tree) < 2:
        return parts
    if isinstance(tree[0], list):
        for index, child in enumerate(tree):
            if not isinstance(child, list):
                break
            parts.extend(_text_parts(child, f"{prefix}.{index + 1}".lstrip(".")))
    elif str(tree[0]).upper() == "TEXT" and str(tree[1]).upper() in ("PLAIN", "HTML"):
        disposition = next((x for x in tree[7:] if isinstance(x, list) and x and
                            str(x[0]).upper() in ("ATTACHMENT", "INLINE")), None)
        if not disposition or str(disposition[0]).upper() != "ATTACHMENT":
            parts.append((prefix or "1", str(tree[1]).lower()))
    return parts


class QQConnection:
    def __init__(self, connection):
        self.connection = connection

    def inspect_folder(self):
        kind, _ = self.connection.select("INBOX", readonly=True)
        if kind != "OK":
            raise AppError("MAIL_FOLDER_UNAVAILABLE", "无法只读打开收件箱", 503)
        validity = self.connection.response("UIDVALIDITY")[1]
        next_uid = self.connection.response("UIDNEXT")[1]
        if not validity or not validity[0] or not next_uid or not next_uid[0]:
            raise AppError("MAIL_PROTOCOL_ERROR", "邮箱未返回可靠的 UID 游标", 503)
        return {"uidvalidity": validity[0].decode(), "uidnext": int(next_uid[0])}

    def list_uids(self, min_uid, since, upper_uid):
        if min_uid > upper_uid:
            return []
        criteria = f"UID {min_uid}:{upper_uid}"
        if since:
            months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
            criteria += f" SINCE {since.day:02d}-{months[since.month - 1]}-{since.year}"
        kind, response = self.connection.uid("SEARCH", None, criteria)
        if kind != "OK":
            raise AppError("MAIL_SEARCH_FAILED", "查询邮件失败", 503)
        return sorted({int(x) for x in response[0].split() if min_uid <= int(x) <= upper_uid})[:50]

    def _fetch(self, message_uid, fields):
        kind, response = self.connection.uid("FETCH", str(message_uid), fields)
        if kind != "OK" or not response:
            raise AppError("MAIL_FETCH_FAILED", "读取邮件失败，游标未推进", 503)
        literals = [item for item in response if isinstance(item, tuple)]
        if not literals:
            raise AppError("MAIL_FETCH_FAILED", "邮件响应不完整，游标未推进", 503)
        for item in literals:
            if len(item) != 2 or not all(isinstance(part, bytes) for part in item):
                raise AppError("MAIL_FETCH_FAILED", "邮件响应不完整，游标未推进", 503)
            declared = re.search(rb"\{(\d+)\}\s*$", item[0])
            if declared and len(item[1]) < int(declared[1]):
                raise AppError("MAIL_FETCH_FAILED", "邮件响应被截断，游标未推进", 503)
        return response

    def fetch_message(self, message_uid):
        response = self._fetch(message_uid, "(BODY.PEEK[HEADER] BODYSTRUCTURE INTERNALDATE)")
        metadata = b" ".join(x[0] if isinstance(x, tuple) else x for x in response if isinstance(x, (tuple, bytes)))
        headers = next((x[1] for x in response if isinstance(x, tuple)), b"")
        matched = re.search(rb"BODYSTRUCTURE\s+(\(.*)", metadata, re.I | re.S)
        if not matched:
            raise AppError("MAIL_STRUCTURE_INVALID", "邮件正文结构无法读取", 503)
        try:
            parts = _text_parts(_imap_tree(matched[1]))
        except (ValueError, IndexError):
            parts = []
        parts.sort(key=lambda part: part[1] != "plain")
        text_parts = []
        for section, _ in parts[:4]:
            mime = self._fetch(message_uid, f"(BODY.PEEK[{section}.MIME])")
            mime_bytes = next((x[1] for x in mime if isinstance(x, tuple)), b"")
            body = self._fetch(message_uid, f"(BODY.PEEK[{section}]<0.393216>)")
            body_bytes = next((x[1] for x in body if isinstance(x, tuple)), b"")
            text_parts.append((mime_bytes, body_bytes))
            if parts[0][1] == "plain":
                break
        message = BytesParser(policy=policy.default).parsebytes(headers)
        chosen_headers = b"".join(name.encode() + b": " + str(message[name]).encode("utf-8") + b"\r\n"
                                  for name in ("From", "Subject", "Date", "Message-ID", "References", "In-Reply-To")
                                  if message.get(name))
        if text_parts:
            raw = chosen_headers + text_parts[0][0].rstrip(b"\r\n") + b"\r\n\r\n" + text_parts[0][1]
        else:
            raw = chosen_headers + b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        internal_date = None
        date_match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
        if date_match:
            try:
                internal_date = iso(parsedate_to_datetime(date_match[1].decode()))
            except (ValueError, TypeError):
                pass
        return {"raw": raw, "internal_date": internal_date}


class QQAdapter(MailProviderAdapter):
    """Backward-compatible adapter name; routes all supported mail domains."""


class AccountService:
    def __init__(self, db, clock, vault, adapter, mail, jobs, settings):
        self.db, self.clock, self.vault, self.adapter = db, clock, vault, adapter
        self.mail, self.jobs, self.settings = mail, jobs, settings
        self.locks = {}

    def serialize(self, account):
        local, _, domain = account.address.partition("@")
        return {"id": account.id, "version": account.version, "provider": account.provider,
                "address": local[:2] + "***@" + domain, "masked_address": local[:2] + "***@" + domain,
                "state": account.state, "initial_scan": account.initial_scan,
                "last_sync_at": iso(account.last_sync_at), "next_sync_at": iso(account.next_sync_at),
                "last_error_code": account.last_error_code, "scan_from": iso(account.scan_from)}

    def list(self):
        with self.db.read() as session:
            return [self.serialize(a) for a in session.scalars(select(Account).where(Account.state != "removed"))]

    def create(self, value):
        address = str(value.get("address", "")).strip().lower()
        code = value.get("authorization_code")
        initial = str(value.get("initial_scan", "14"))
        provider = value.get("provider", "qq")
        if provider is None:
            raise AppError("VALIDATION_ERROR", "请选择支持的邮箱服务", 422)
        provider_for_address(address, provider)
        validate_authorization_code(code)
        if initial not in ("14", "30", "now"):
            raise AppError("VALIDATION_ERROR", "请输入授权码并选择同步范围", 422)
        with self.db.read() as session:
            existing = session.scalar(select(Account).where(Account.address == address))
            if existing and existing.state != "removed":
                raise AppError("ACCOUNT_EXISTS", "此邮箱已添加", 409)
            account_id = existing.id if existing else uid()
        reference = "mail:" + account_id + ":" + uid()
        self.vault.set(reference, code)
        try:
            with self.db.tx() as session:
                account = session.get(Account, account_id)
                if account:
                    if account.state != "removed":
                        raise AppError("ACCOUNT_EXISTS", "此邮箱已添加", 409)
                    if account_id in self.locks and self.locks[account_id].locked():
                        raise AppError("ACCOUNT_BUSY", "上次连接仍在结束，请稍后重新添加", 409)
                    account.version += 1
                    account.uidvalidity, account.last_uid, account.initial_upper_uid = None, 0, None
                    account.last_job_id, account.last_sync_at, account.next_sync_at = None, None, None
                    account.last_error_code = None
                else:
                    account = Account(id=account_id, address=address)
                    session.add(account)
                account.provider = provider
                account.credential_ref, account.initial_scan, account.state = reference, initial, "ready"
                account.scan_from = millis(self.clock() - timedelta(days=int(initial))) if initial != "now" else None
                session.flush()
                return self.serialize(account)
        except Exception:
            self.vault.delete(reference)
            raise

    def credential(self, account_id, code):
        validate_authorization_code(code)
        with self.db.read() as session:
            account = get_record(session, Account, account_id)
            old_reference, address, version = account.credential_ref, account.address, account.version
            provider_for_address(address, account.provider)
        reference = "mail:" + account_id + ":" + uid()
        self.vault.set(reference, code)
        try:
            with self.adapter.open(address, code) as connection:
                connection.inspect_folder()
            with self.db.tx() as session:
                account = get_record(session, Account, account_id)
                check_version(account, version)
                account.credential_ref, account.state, account.last_error_code = reference, "ready", None
                account.version += 1
                result = self.serialize(account)
        except Exception:
            self.vault.delete(reference)
            raise
        if old_reference:
            self.vault.delete(old_reference)
        return result

    def pause(self, account_id, expected_version, paused):
        if type(paused) is not bool:
            raise AppError("VALIDATION_ERROR", "暂停值无效", 422)
        with self.db.tx() as session:
            account = get_record(session, Account, account_id)
            check_version(account, expected_version)
            account.state = "paused" if paused else "ready" if account.credential_ref else "auth_required"
            account.version += 1
            return self.serialize(account)

    def remove(self, account_id, expected_version):
        with self.db.tx() as session:
            account = get_record(session, Account, account_id)
            check_version(account, expected_version)
            reference = account.credential_ref
            # Preserve the retryable reference if the OS credential store refuses deletion.
            self.vault.delete(reference)
            if account.last_job_id:
                job = session.get(Job, account.last_job_id)
                if job and job.status in ("queued", "running"):
                    job.status, job.error_code = "interrupted", "ACCOUNT_REMOVED"
            account.state, account.credential_ref = "removed", None
            account.next_sync_at = None
            account.version += 1
        return {"account_id": account_id, "removed": True}

    def queue_sync(self, account_id, force=False, test=False):
        with self.db.tx() as session:
            account = get_record(session, Account, account_id)
            if account.state == "removed" or not test and account.state in ("auth_required", "paused"):
                raise AppError("ACCOUNT_NOT_READY", "邮箱已暂停或需要重新授权", 409)
            retries = 0
            if account.last_job_id:
                existing = session.get(Job, account.last_job_id)
                if existing and (existing.status in ("queued", "running") or
                                 not force and millis(self.clock()) - existing.created_at < 10000):
                    return existing.id
                if existing and existing.status == "failed":
                    retries = existing.progress.get("retry_count", 0)
            job_id = self.jobs.create("mail_test" if test else "mail_sync")
            self.jobs.update(job_id, progress={"phase": "queued", "completed": 0, "retry_count": retries})
            account.last_job_id = job_id
            return job_id

    def run_sync(self, job_id, account_id, test=False):
        with self.db.activity():
            self._run_sync(job_id, account_id, test)

    def _run_sync(self, job_id, account_id, test=False):
        lock = self.locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            if self.jobs.get(job_id)["status"] not in ("queued", "running"):
                return
            self.jobs.update(job_id, status="running")
            with self.db.read() as session:
                account = get_record(session, Account, account_id)
                if account.state == "removed" or account.state == "paused" and not test:
                    self.jobs.update(job_id, status="interrupted", error_code="SYNC_PAUSED")
                    return
                address, reference = account.address, account.credential_ref
                provider_for_address(address, account.provider)
                initial, scan_from, old_validity, last_uid = account.initial_scan, account.scan_from, account.uidvalidity, account.last_uid
            credential = self.vault.get(reference) if reference else None
            if not credential:
                raise AppError("MAIL_AUTH_FAILED", "请更新邮箱授权码", 503)
            saved = fetched = 0
            with self.adapter.open(address, credential) as connection:
                info = connection.inspect_folder()
                if test:
                    with self.db.tx() as session:
                        account = get_record(session, Account, account_id)
                        if account.state == "removed":
                            self.jobs.update(job_id, status="interrupted", error_code="ACCOUNT_REMOVED")
                            return
                        if account.state != "paused":
                            account.state, account.last_error_code, account.next_sync_at = "ready", None, None
                    self.jobs.update(job_id, status="succeeded", result={"ok": True, "readonly": True})
                    return
                validity, upper_uid = str(info["uidvalidity"]), int(info["uidnext"]) - 1
                if old_validity != validity:
                    last_uid = 0
                if initial == "now" and old_validity is None:
                    last_uid, scan_from = upper_uid, millis(self.clock())
                with self.db.tx() as session:
                    account = get_record(session, Account, account_id)
                    if account.state in ("paused", "removed"):
                        self.jobs.update(job_id, status="interrupted", error_code="SYNC_PAUSED")
                        return
                    account.uidvalidity, account.last_uid = validity, last_uid
                    account.initial_upper_uid = account.initial_upper_uid or upper_uid
                    account.scan_from, account.state = scan_from, "syncing"
                since = datetime.fromtimestamp(scan_from / 1000, timezone.utc) if scan_from else None
                while last_uid < upper_uid:
                    with self.db.read() as session:
                        if get_record(session, Account, account_id).state in ("paused", "removed"):
                            self.jobs.update(job_id, status="interrupted", error_code="SYNC_PAUSED")
                            return
                    uids = sorted(set(connection.list_uids(last_uid + 1, since, upper_uid)))
                    uids = [u for u in uids if last_uid < u <= upper_uid][:50]
                    if not uids:
                        break
                    batch = [(message_uid, connection.fetch_message(message_uid)) for message_uid in uids]
                    with self.db.tx() as session:
                        account = get_record(session, Account, account_id)
                        if account.state in ("paused", "removed"):
                            self.jobs.update(job_id, status="interrupted", error_code="SYNC_PAUSED")
                            return
                        for message_uid, message in batch:
                            fetched += 1
                            received = message.get("internal_date")
                            if since and received and datetime.fromisoformat(received) < since:
                                continue
                            self.mail.ingest(message["raw"], account_id=account_id, uidvalidity=validity,
                                             message_uid=message_uid, internal_date=received, session=session)
                            saved += 1
                        account = get_record(session, Account, account_id)
                        account.last_uid = uids[-1]
                        last_uid = uids[-1]
                    progress = self.jobs.get(job_id)["progress"]
                    self.jobs.update(job_id, progress={**progress, "phase": "sync", "completed": fetched})
                with self.db.tx() as session:
                    account = get_record(session, Account, account_id)
                    if account.state in ("paused", "removed"):
                        self.jobs.update(job_id, status="interrupted", error_code="SYNC_PAUSED")
                        return
                    account.state = "ready"
                    account.last_uid = max(last_uid, upper_uid)
                    account.last_sync_at = millis(self.clock())
                    account.next_sync_at = millis(self.clock()) + self.settings.get(session)["sync_interval_seconds"] * 1000
                    account.last_error_code = None
                self.jobs.update(job_id, status="succeeded", result={"fetched_count": fetched, "saved_count": saved})
        except AppError as exc:
            self._sync_failed(job_id, account_id, exc.code)
        except Exception:
            self._sync_failed(job_id, account_id, "MAIL_SYNC_FAILED")
        finally:
            lock.release()

    def _sync_failed(self, job_id, account_id, error_code):
        progress = self.jobs.get(job_id)["progress"]
        retries = min(int(progress.get("retry_count", 0)) + 1, 4)
        delay = (30, 60, 120, 300)[retries - 1] * (1 + random.uniform(0, .2))
        with self.db.tx() as session:
            self.jobs.update(job_id, status="failed", error_code=error_code,
                             progress={**progress, "retry_count": retries})
            account = get_record(session, Account, account_id)
            if account.state not in ("paused", "removed"):
                account.state = "auth_required" if error_code == "MAIL_AUTH_FAILED" else "network_error"
            account.last_error_code = error_code
            account.next_sync_at = (millis(self.clock()) + int(delay * 1000)
                                    if account.state == "network_error" else None)

    def tick(self):
        now = millis(self.clock())
        with self.db.read() as session:
            ids = [a.id for a in session.scalars(select(Account).where(Account.state.in_(["ready", "network_error"])))
                   if a.next_sync_at is None or a.next_sync_at <= now]
        for account_id in ids:
            job_id = self.queue_sync(account_id)
            self.run_sync(job_id, account_id)
