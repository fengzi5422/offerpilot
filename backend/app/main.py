"""Application factory; the desktop launcher owns sockets, tray, and the instance lock."""
import asyncio
import json
import logging
import sys
import threading
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException

from app.accounts import AccountService, QQAdapter, WindowsCredentialStore
from app.applications import ApplicationService
from app.data_tools import DataTools
from app.domain import AppError, iso, uid, utcnow
from app.jobs import JobService
from app.events import EventService
from app.mail import MailService
from app.observability import Diagnostics, safe_event
from app.persistence import Database, SCHEMA_REVISION
from app.reminders import ReminderService, WindowsNotifier
from app.security import Sessions, check_request, limit_request_body
from app.tasks import SettingsService, TaskService


def create_app(data_dir, *, origin="http://127.0.0.1:8765", bootstrap_code=None, clock=None,
               notifier=None, mail_adapter=None, credential_store=None, start_workers=True,
               autostart_callback=None, static_dir=None, shutdown_timeout=15):
    clock = clock or utcnow
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    run_id = uid()
    logger = logging.getLogger("offerpilot." + run_id)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(log_dir / "backend.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    def log(event, **safe):
        record = safe_event(event, {"timestamp_utc": iso(clock()), "run_id": run_id, "module": "api", **safe})
        if record:
            logger.info(json.dumps(record, ensure_ascii=False))

    db = Database(data_dir)
    db.log = log
    log("db.migration", module="database")
    sessions = Sessions(clock, bootstrap_code)
    db.on_replace = sessions.clear
    settings = SettingsService(db)
    tasks = TaskService(db, clock, settings)
    settings.replan = tasks.rebuild_all
    applications = ApplicationService(db, clock)
    reminders = ReminderService(db, clock, settings, notifier or WindowsNotifier())
    jobs = JobService(db, clock)
    mail = MailService(db, tasks, settings, clock)
    vault = credential_store or WindowsCredentialStore()
    accounts = AccountService(db, clock, vault, mail_adapter or QQAdapter(), mail, jobs, settings)
    data_tools = DataTools(db, clock, tasks, vault)
    jobs.handlers.update({
        "backup": lambda p: data_tools.backup(p.get("include_evidence", False)),
        "export": lambda p: data_tools.export(p.get("format"), p.get("entities")),
        "restore_validate": lambda p: data_tools.validate_restore(data_tools.read_input(p["file"])),
        "eml_import": lambda p: {"message_id": mail.ingest(data_tools.read_input(p["file"]))},
        "reparse": lambda p: mail.reparse(p["message_id"], p["expected_version"]),
    })
    stop, workers = threading.Event(), []
    worker_health = {name: "stopped" for name in ("reminders", "mail", "parse", "maintenance", "backup")}
    diagnostics = Diagnostics(db, clock, data_tools, worker_health)
    jobs.handlers["diagnostic_export"] = lambda p: diagnostics.export()

    def housekeeping():
        with db.activity():
            mail.cleanup()
            data_tools.cleanup()
        data_tools.automatic_backup()

    def worker(name, callback, interval):
        worker_health[name] = "running"
        while not stop.is_set():
            try:
                callback()
                worker_health[name] = "running"
            except Exception:
                worker_health[name] = "degraded"
                log("worker.failed", module=name, error_code="WORKER_FAILED")
            stop.wait(interval)
        worker_health[name] = "stopped"

    @asynccontextmanager
    async def lifespan(_):
        reminders.recover()
        if start_workers:
            for name, callback, interval in (("reminders", reminders.run_once, 15), ("mail", accounts.tick, 30),
                ("parse", mail.parse_pending, 2), ("maintenance", jobs.run_once, 1),
                ("backup", housekeeping, 60)):
                thread = threading.Thread(target=worker, args=(name, callback, interval), daemon=True,
                                          name="offerpilot-" + name)
                thread.start()
                workers.append(thread)
        log("app.start", schema_revision=SCHEMA_REVISION)
        yield
        stop.set()
        await asyncio.gather(*(asyncio.to_thread(thread.join, shutdown_timeout) for thread in workers))
        # A timed-out network adapter cannot write after the database has closed.
        db.close()
        log("app.stop", count=sum(thread.is_alive() for thread in workers))
        handler.close()
        logger.removeHandler(handler)

    app = FastAPI(title="OfferPilot", version="0.2.1", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.data_dir, app.state.bootstrap_code, app.state.sessions = data_dir, sessions.initial_code, sessions
    app.state.services = SimpleNamespace(db=db, settings=settings, tasks=tasks, reminders=reminders, mail=mail,
                                        accounts=accounts, jobs=jobs, data_tools=data_tools, clock=clock,
                                        worker_health=worker_health, autostart_callback=autostart_callback,
                                        applications=applications)
    app.state.services.diagnostics = diagnostics
    app.state.services.events = EventService(db, tasks)
    app.state.origin = origin.rstrip("/")

    def error_response(request, exc):
        request_id = getattr(request.state, "request_id", uid())
        error = {"code": exc.code, "message": exc.message, "retryable": exc.status in (429, 503), **exc.fields}
        return JSONResponse({"error": error, "request_id": request_id}, status_code=exc.status)

    @app.middleware("http")
    async def security(request, call_next):
        request.state.request_id = uid()
        generation_token = db.request_generation.set(db.generation)
        try:
            check_request(request, app.state.origin, sessions)
            await limit_request_body(request)
            response = await call_next(request)
        except AppError as exc:
            response = error_response(request, exc)
        finally:
            db.request_generation.reset(generation_token)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        log("api.request", request_id=request.state.request_id, method=request.method, status=response.status_code)
        return response

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(request, AppError("VALIDATION_ERROR", "请求字段不正确", 422,
                              fields=[{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, AppError("NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                                               "请求的资源不存在" if exc.status_code == 404 else "请求无法处理", exc.status_code))

    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        return error_response(request, AppError("DB_BUSY", "数据暂时忙碌，请稍后重试", 503))

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return error_response(request, AppError("STATE_CONFLICT", "操作与现有数据冲突，请刷新后重试", 409))

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        log("api.error", request_id=getattr(request.state, "request_id", None), error_code="INTERNAL_ERROR")
        return error_response(request, AppError("INTERNAL_ERROR", "操作未完成，请查看诊断并重试", 500))

    from app.api import install_routes
    install_routes(app)
    assets = Path(static_dir) if static_dir else Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "frontend" / "dist"
    if assets.is_dir():
        app.mount("/", StaticFiles(directory=assets, html=True), name="frontend")
    else:
        @app.get("/")
        def no_frontend(request: Request):
            return {"data": {"ready": True, "message": "请先构建 frontend/dist 或使用桌面安装包"},
                    "request_id": request.state.request_id}
    return app
