import base64
import binascii
import hashlib
import json
import threading
from uuid import UUID

from fastapi import BackgroundTasks, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.data_tools import MAX_BACKUP_BYTES
from app.domain import AppError, millis
from app.icalendar import export_calendar
from app.mail import MAX_EML_BYTES
from app.persistence import Command, Reminder, get_record
from app.version import VERSION


def install_routes(app):
    s = app.state.services
    db, clock, sessions = s.db, s.clock, app.state.sessions
    exit_lock = threading.Lock()
    exit_requested = False

    def respond(request, value, status=200):
        return JSONResponse({"data": value, "request_id": request.state.request_id}, status_code=status)

    def page(request, values):
        try:
            limit = int(request.query_params.get("limit", 50))
            if not 1 <= limit <= 200:
                raise ValueError()
            cursor = request.query_params.get("cursor")
            start = 0
            if cursor:
                last_id = base64.urlsafe_b64decode(cursor.encode()).decode()
                start = next(index + 1 for index, value in enumerate(values) if value["id"] == last_id)
        except (ValueError, StopIteration, UnicodeError, binascii.Error):
            raise AppError("INVALID_CURSOR", "分页参数无效", 422) from None
        batch = values[start:start + limit]
        cursor = base64.urlsafe_b64encode(batch[-1]["id"].encode()).decode() if batch and start + limit < len(values) else None
        return JSONResponse({"data": batch, "next_cursor": cursor, "request_id": request.state.request_id})

    def command(request, value, callback, status=200, atomic=True):
        key = request.headers.get("idempotency-key")
        if not key:
            return respond(request, callback(), status)
        try:
            UUID(key)
        except ValueError:
            raise AppError("INVALID_IDEMPOTENCY_KEY", "幂等键必须为 UUID", 422) from None
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        route = request.method + " " + request.url.path
        def execute():
            with db.read() as session:
                old = session.scalar(select(Command).where(Command.route == route, Command.request_key == key))
                if old:
                    if old.request_hash != fingerprint:
                        raise AppError("IDEMPOTENCY_CONFLICT", "同一操作标识不能用于不同内容", 409)
                    return respond(request, old.response, old.status_code)
            result = callback()
            with db.tx() as session:
                session.add(Command(route=route, request_key=key, request_hash=fingerprint, response=result,
                                    status_code=status, expires_at=millis(clock()) + 7 * 86400000))
            return respond(request, result, status)
        with db.write_lock:
            if atomic:
                with db.tx():
                    return execute()
            return execute()

    @app.get("/health")
    @app.get("/api/v1/health")
    def health(request: Request):
        return respond(request, {"ready": True, "version": VERSION})

    @app.post("/api/v1/session/bootstrap")
    def bootstrap(request: Request, body: dict):
        token, session = sessions.bootstrap(body.get("code"))
        response = respond(request, {key: session[key] for key in ("csrf_token", "expires_at")})
        response.set_cookie("offerpilot_session", token, httponly=True, samesite="strict", max_age=43200, path="/")
        return response

    @app.get("/api/v1/session")
    def get_session(request: Request):
        return respond(request, {key: request.state.session[key] for key in ("csrf_token", "expires_at")})

    @app.post('/api/v1/app/exit')
    def exit_app(request: Request, background_tasks: BackgroundTasks):
        nonlocal exit_requested
        callback = getattr(app.state, 'shutdown_callback', None)
        if callback is None:
            raise AppError('DESKTOP_EXIT_UNAVAILABLE', '此运行方式请从 OfferPilot 托盘选择退出', 409)
        with exit_lock:
            if not exit_requested:
                exit_requested = True
                background_tasks.add_task(callback)
        return respond(request, {'status': 'stopping'}, 202)

    @app.get("/api/v1/tasks")
    def list_tasks(request: Request, view: str | None = None, status: str | None = None):
        return page(request, s.tasks.list(view, status))

    @app.get('/api/v1/integrations/{kind}')
    def integration_settings(request: Request, kind: str):
        return respond(request, {**s.integrations.get(kind), **({'runtime': s.ai.status()} if kind == 'ai' else {})})

    @app.patch('/api/v1/integrations/{kind}')
    def integration_update(request: Request, kind: str, body: dict):
        # Secrets must never enter durable idempotency command receipts.
        return respond(request, s.integrations.patch(kind, body.get('expected_version'), body.get('changes')))

    @app.delete('/api/v1/integrations/{kind}')
    def integration_delete(request: Request, kind: str, body: dict):
        return respond(request, s.integrations.delete(kind, body.get('expected_version')))

    @app.post('/api/v1/integrations/ai/test')
    def ai_test(request: Request):
        return respond(request, s.ai.test())

    @app.post('/api/v1/integrations/webhook/test')
    def webhook_test(request: Request):
        from app.domain import uid
        return respond(request, s.webhook({'idempotency_key': 'test:' + uid()}))

    @app.post("/api/v1/tasks")
    def create_task(request: Request, body: dict):
        return command(request, body, lambda: s.tasks.create(body), 201)

    @app.get('/api/v1/calendar')
    def calendar(request: Request, start: str, end: str, timezone: str | None = None):
        return respond(request,s.events.calendar(start,end,timezone))

    @app.get("/api/v1/calendar/export")
    def calendar_export(request: Request, include_details: bool = False):
        with db.activity():
            content, omitted = export_calendar(s.tasks.list(), clock(), include_details=include_details)
        return Response(content, media_type="text/calendar; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="offerpilot-calendar.ics"',
            "X-OfferPilot-Omitted-Count": str(omitted),
        })

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(request: Request, task_id: str):
        return respond(request, s.tasks.get(task_id))

    @app.patch("/api/v1/tasks/{task_id}")
    def patch_task(request: Request, task_id: str, body: dict):
        return respond(request, s.tasks.patch(task_id, body.get("expected_version"), body.get("changes")))

    @app.get('/api/v1/tasks/{task_id}/preparations')
    def preparations(request: Request, task_id: str):
        return respond(request, s.preparations.list(task_id))

    @app.post('/api/v1/tasks/{task_id}/preparations')
    def preparation_create(request: Request, task_id: str, body: dict):
        value = {k: v for k, v in body.items() if k != 'expected_parent_version'}
        with s.preparations.lock(task_id):
            return command(request, body, lambda: s.preparations.create(task_id, body.get('expected_parent_version'), value), 201)

    @app.post('/api/v1/tasks/{task_id}/preparations/templates')
    def preparation_template(request: Request, task_id: str, body: dict):
        with s.preparations.lock(task_id):
            return command(request, body, lambda: s.preparations.apply_template(task_id, body.get('expected_parent_version')))

    @app.patch('/api/v1/tasks/{task_id}/preparations/{preparation_id}')
    def preparation_patch(request: Request, task_id: str, preparation_id: str, body: dict):
        with s.preparations.lock(task_id, preparation_id):
            return command(request, body, lambda: s.preparations.patch(task_id, preparation_id,
                body.get('expected_parent_version'), body.get('expected_version'), body.get('changes')))

    @app.delete('/api/v1/tasks/{task_id}/preparations/{preparation_id}')
    def preparation_delete(request: Request, task_id: str, preparation_id: str, body: dict):
        with s.preparations.lock(task_id, preparation_id):
            return command(request, body, lambda: s.preparations.delete(task_id, preparation_id,
                body.get('expected_parent_version'), body.get('expected_version')))

    @app.post("/api/v1/tasks/{task_id}/{action}")
    def task_action(request: Request, task_id: str, action: str, body: dict):
        if action not in ("complete", "cancel", "reopen"):
            raise AppError("NOT_FOUND", "操作不存在", 404)
        with db.activity(), db.task_lock(task_id):
            return command(request, body, lambda: s.tasks.transition(task_id, body.get("expected_version"), action))

    @app.get("/api/v1/emails")
    def list_emails(request: Request, state: str | None = None, account_id: str | None = None):
        return page(request, s.mail.list(state, account_id))

    @app.post("/api/v1/emails/import")
    async def import_eml(request: Request, file: UploadFile = File(...)):
        raw = await file.read(MAX_EML_BYTES + 1)
        await file.close()
        if len(raw) > MAX_EML_BYTES:
            raise AppError("FILE_TOO_LARGE", "EML 文件不能超过 10 MiB", 413)
        return await run_in_threadpool(command, request, {"sha256": hashlib.sha256(raw).hexdigest()},
                    lambda: {"job_id": s.jobs.enqueue("eml_import", {"file": s.data_tools.stage_input(raw)})}, 202)

    @app.get("/api/v1/emails/{message_id}")
    def get_email(request: Request, message_id: str):
        return respond(request, s.mail.get(message_id))

    @app.get("/api/v1/reviews")
    def list_reviews(request: Request):
        return page(request, s.mail.reviews())

    @app.post("/api/v1/emails/{message_id}/review")
    def review(request: Request, message_id: str, body: dict):
        return command(request, body, lambda: s.mail.review(message_id, body.get("expected_version"), body.get("decisions")))

    @app.post("/api/v1/emails/{message_id}/reparse")
    def reparse(request: Request, message_id: str, body: dict):
        if body.get('mode') == 'ai':
            return command(request, body, lambda: {'job_id': s.ai.enqueue(message_id, body.get('expected_version'), force=True)}, 202)
        if body.get("mode", "rules") != "rules":
            raise AppError("FEATURE_UNAVAILABLE", "当前版本仅支持规则解析", 400)
        return command(request, body, lambda: {"job_id": s.jobs.enqueue("reparse", {
            "message_id": message_id, "expected_version": body.get("expected_version")})}, 202)

    @app.post('/api/v1/emails/{message_id}/resolve-change')
    def resolve_change(request: Request, message_id: str, body: dict):
        task_id=body.get('task_id')
        if not isinstance(task_id,str) or not task_id:
            raise AppError('VALIDATION_ERROR','请选择需要处理的原事件',422)
        # Same lock order as deliveries: activity -> task -> database. The
        # command receipt, review state and reminder changes commit together.
        with db.activity(), db.task_lock(task_id):
            return command(request,body,lambda:s.events.resolve_change(message_id,body))

    @app.get("/api/v1/accounts")
    def list_accounts(request: Request):
        return page(request, s.accounts.list())

    @app.post("/api/v1/accounts")
    def create_account(request: Request, body: dict):
        return command(request, body, lambda: s.accounts.create(body), 201, atomic=False)

    @app.put("/api/v1/accounts/{account_id}/credential")
    def update_credential(request: Request, account_id: str, body: dict):
        return respond(request, s.accounts.credential(account_id, body.get("authorization_code")))

    @app.patch("/api/v1/accounts/{account_id}")
    def pause_account(request: Request, account_id: str, body: dict):
        return respond(request, s.accounts.pause(account_id, body.get("expected_version"), body.get("paused")))

    @app.delete("/api/v1/accounts/{account_id}")
    def remove_account(request: Request, account_id: str, body: dict):
        return command(request, body, lambda: {"job_id": s.jobs.execute("account_remove", lambda: s.accounts.remove(
            account_id, body.get("expected_version")))}, 202, atomic=False)

    @app.post("/api/v1/accounts/{account_id}/test")
    def test_account(request: Request, account_id: str, background: BackgroundTasks):
        job_id = s.accounts.queue_sync(account_id, force=True, test=True)
        background.add_task(s.accounts.run_sync, job_id, account_id, True)
        return respond(request, {"job_id": job_id}, 202)

    @app.post("/api/v1/sync/trigger")
    def sync(request: Request, body: dict, background: BackgroundTasks):
        def enqueue():
            job_id = s.accounts.queue_sync(body.get("account_id"))
            background.add_task(s.accounts.run_sync, job_id, body.get("account_id"))
            return {"job_id": job_id, "sync_run_id": job_id}
        return command(request, body, enqueue, 202)

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        return respond(request, s.jobs.get(job_id))

    @app.get("/api/v1/notifications")
    def notifications(request: Request, state: str | None = None):
        return page(request, s.reminders.list_notifications(state))

    @app.post("/api/v1/notifications/test")
    def notification_test(request: Request, body: dict):
        if body.get("channel", "windows") != "windows":
            raise AppError("CHANNEL_UNAVAILABLE", "当前版本仅支持 Windows 通知", 400)
        return command(request, body, lambda: {"job_id": s.jobs.execute("notification_test", s.reminders.test)}, 202, atomic=False)

    @app.post("/api/v1/notifications/{item_id}/dismiss")
    def dismiss(request: Request, item_id: str, body: dict):
        return respond(request, s.reminders.dismiss(item_id, body.get("expected_version")))

    @app.post("/api/v1/reminders/{reminder_id}/snooze")
    def snooze(request: Request, reminder_id: str, body: dict):
        with db.read() as session:
            task_id = get_record(session, Reminder, reminder_id).task_id
        with db.activity(), db.task_lock(task_id):
            return command(request, body, lambda: s.reminders.snooze(reminder_id, body.get("expected_version"), body.get("minutes")))

    @app.get("/api/v1/settings")
    def get_settings(request: Request):
        return respond(request, s.settings.get())

    @app.patch("/api/v1/settings")
    def change_settings(request: Request, body: dict):
        return respond(request, s.settings.patch(body.get("expected_version"), body.get("changes"),
                                                autostart_callback=s.autostart_callback))

    @app.get("/api/v1/diagnostics")
    def diagnostics(request: Request):
        return respond(request, s.diagnostics.health())

    @app.post("/api/v1/diagnostics/export")
    def diagnostic_export(request: Request, body: dict):
        return command(request, body, lambda: {"job_id": s.jobs.enqueue("diagnostic_export", {})}, 202)

    @app.post("/api/v1/exports")
    def export(request: Request, body: dict):
        return command(request, body, lambda: {"job_id": s.jobs.enqueue("export", {
            "format": body.get("format"), "entities": body.get("entities")})}, 202)

    @app.post("/api/v1/backups")
    def backup(request: Request, body: dict):
        return command(request, body, lambda: {"job_id": s.jobs.enqueue("backup", {
            "include_evidence": body.get("include_evidence", False)})}, 202)

    @app.get("/api/v1/artifacts/{artifact_id}")
    def artifact(artifact_id: str):
        path, media_type = s.data_tools.artifact(artifact_id)
        return FileResponse(path, media_type=media_type, filename="offerpilot" + path.suffix)

    @app.post("/api/v1/restores/validate")
    async def validate_restore(request: Request, backup: UploadFile = File(...)):
        raw = await backup.read(MAX_BACKUP_BYTES + 1)
        await backup.close()
        if len(raw) > MAX_BACKUP_BYTES:
            raise AppError("FILE_TOO_LARGE", "备份文件不能超过 128 MiB", 413)
        return await run_in_threadpool(command, request, {"sha256": hashlib.sha256(raw).hexdigest()},
                lambda: {"job_id": s.jobs.enqueue("restore_validate", {"file": s.data_tools.stage_input(raw)})}, 202, True)

    @app.get("/api/v1/restores/{restore_id}")
    def restore_preview(request: Request, restore_id: str):
        return respond(request, s.data_tools.restore_preview(restore_id))

    @app.post("/api/v1/restores/{restore_id}/apply")
    def restore_apply(request: Request, restore_id: str, body: dict):
        result = s.data_tools.apply_restore(restore_id, body.get("confirmation_token"))
        job_id = s.jobs.create("restore_apply", status="succeeded", result=result)
        sessions.clear()
        return respond(request, {"job_id": job_id, **result}, 202)

    @app.post("/api/v1/privacy/preview")
    def privacy_preview(request: Request, body: dict):
        return respond(request, s.data_tools.privacy_preview(body.get("scope")))

    @app.post("/api/v1/privacy/purge")
    def privacy_purge(request: Request, body: dict):
        result = s.data_tools.purge(body.get("scope"), body.get("preview_token"))
        job_id = s.jobs.create("privacy_purge", "succeeded", result)
        if body.get("scope") == "all":
            sessions.clear()
        return respond(request, {"job_id": job_id, **result}, 202)

    @app.get("/api/v1/applications")
    def applications(request: Request):
        return page(request, s.applications.list())

    @app.post("/api/v1/applications")
    def create_application(request: Request, body: dict):
        return command(request, body, lambda: s.applications.create(body), 201)

    @app.get("/api/v1/applications/{application_id}")
    def get_application(request: Request, application_id: str):
        return respond(request, s.applications.get(application_id))

    @app.post("/api/v1/applications/{application_id}/link")
    def link_application_task(request: Request, application_id: str, body: dict):
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AppError("VALIDATION_ERROR", "请选择需要关联的任务", 422)
        with db.activity(), db.task_lock(task_id):
            return command(request, body, lambda: s.application_board.link(
                application_id, body.get("expected_version"), task_id,
                body.get("expected_task_version"), body.get("expected_source_version")))

    @app.post("/api/v1/applications/{application_id}/unlink")
    def unlink_application_task(request: Request, application_id: str, body: dict):
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AppError("VALIDATION_ERROR", "请选择需要解除关联的任务", 422)
        with db.activity(), db.task_lock(task_id):
            return command(request, body, lambda: s.application_board.unlink(
                application_id, body.get("expected_version"), task_id, body.get("expected_task_version")))

    @app.patch("/api/v1/applications/{application_id}")
    def patch_application(request: Request, application_id: str, body: dict):
        return respond(request, s.applications.patch(application_id, body.get("expected_version"), body.get("changes")))
