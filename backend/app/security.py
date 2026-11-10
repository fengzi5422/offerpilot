import hashlib
import secrets
import threading
from urllib.parse import urlsplit

from app.domain import AppError, iso, millis


class Sessions:
    def __init__(self, clock, initial_code=None):
        self.clock = clock
        self.lock = threading.RLock()
        self.codes, self.sessions = {}, {}
        self.initial_code = self.issue_bootstrap(initial_code)

    def issue_bootstrap(self, code=None):
        code = code or secrets.token_urlsafe(32)
        with self.lock:
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = millis(self.clock()) + 60000
        return code

    def bootstrap(self, code):
        if not isinstance(code, str):
            raise AppError("INVALID_BOOTSTRAP", "启动链接已失效，请从托盘重新打开", 401)
        now = millis(self.clock())
        with self.lock:
            expiry = self.codes.pop(hashlib.sha256(code.encode()).hexdigest(), 0)
            if expiry <= now:
                raise AppError("INVALID_BOOTSTRAP", "启动链接已失效，请从托盘重新打开", 401)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {
                "csrf_token": csrf, "expires_at": iso(now + 43200000), "expires_ms": now + 43200000}
            return token, self.sessions[hashlib.sha256(token.encode()).hexdigest()]

    def authenticate(self, token):
        with self.lock:
            value = self.sessions.get(hashlib.sha256((token or "").encode()).hexdigest())
            if not value or value["expires_ms"] <= millis(self.clock()):
                raise AppError("SESSION_REQUIRED", "会话已失效，请从托盘重新打开", 401)
            return value

    def clear(self):
        with self.lock:
            self.sessions.clear()
            self.codes.clear()


def check_request(request, origin, sessions):
    expected = urlsplit(origin)
    if request.headers.get("host", "") != expected.netloc:
        raise AppError("INVALID_HOST", "请求主机无效", 403)
    incoming = request.headers.get("origin")
    if incoming is not None and incoming != origin:
        raise AppError("INVALID_ORIGIN", "请求来源无效", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AppError("INVALID_ORIGIN", "不允许跨站请求", 403)
    path = request.url.path
    if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if incoming != origin:
            raise AppError("INVALID_ORIGIN", "写入请求需要本机来源", 403)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        expected_media_type = ("multipart/form-data" if path in
                               ("/api/v1/emails/import", "/api/v1/restores/validate") else "application/json")
        if media_type != expected_media_type:
            raise AppError("UNSUPPORTED_MEDIA_TYPE", "请求内容类型不受支持", 415)
    if path in ("/health", "/api/v1/health", "/api/v1/session/bootstrap") or not path.startswith("/api/"):
        return
    session = sessions.authenticate(request.cookies.get("offerpilot_session"))
    request.state.session = session
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if incoming != origin:
            raise AppError("INVALID_ORIGIN", "写入请求需要本机来源", 403)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf_token"]):
            raise AppError("CSRF_INVALID", "请求验证失败，请刷新页面", 403)


async def limit_request_body(request):
    """Count actual streamed bytes before JSON or multipart parsing, including chunked bodies."""
    if not request.url.path.startswith("/api/") or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    limit = 1024 * 1024
    if request.url.path == "/api/v1/emails/import":
        limit = 10 * 1024 * 1024 + 65536  # Allow multipart framing, not another large file.
    elif request.url.path == "/api/v1/restores/validate":
        limit = 128 * 1024 * 1024 + 65536
    try:
        declared = int(request.headers.get("content-length", "0"))
        if declared < 0:
            raise ValueError()
    except ValueError:
        raise AppError("INVALID_CONTENT_LENGTH", "请求长度无效", 400) from None
    if declared > limit:
        raise AppError("FILE_TOO_LARGE", "请求内容超过允许大小", 413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise AppError("FILE_TOO_LARGE", "请求内容超过允许大小", 413)
        chunks.append(chunk)
    # Starlette's cached Request replays this bounded body to downstream parsers.
    request._body = b"".join(chunks)
