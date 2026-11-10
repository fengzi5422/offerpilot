"""Bounded JSON POST with pinned DNS addresses, TLS verification and no redirects/proxies."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from contextlib import nullcontext
from urllib.parse import urlsplit

from app.domain import AppError

DNS_SLOTS = threading.BoundedSemaphore(2)


def _resolve(host, port, timeout):
    # OS name resolution cannot be cancelled. Bound both its wait and outstanding calls.
    deadline = time.monotonic() + timeout
    if not DNS_SLOTS.acquire(timeout=max(0, timeout)):
        raise AppError('SERVICE_TIMEOUT', '服务地址解析超时', 503)
    done, result = threading.Event(), {}
    def resolve():
        try:
            result['rows'] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            result['error'] = True
        finally:
            DNS_SLOTS.release()
            done.set()
    threading.Thread(target=resolve, daemon=True, name='offerpilot-dns').start()
    if not done.wait(max(0, deadline - time.monotonic())):
        raise AppError('SERVICE_TIMEOUT', '服务地址解析超时', 503)
    if result.get('error'):
        raise OSError()
    return result['rows']


def validate_endpoint(url, *, allow_local=False):
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError()
        parsed = urlsplit(url)
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or
                parsed.scheme != 'https' and not (allow_local and local and parsed.scheme == 'http') or
                parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise AppError('ENDPOINT_INVALID', '请使用有效的 HTTPS 服务地址；本机 AI 可使用 localhost HTTP 地址', 422) from None


def endpoint_addresses(url, *, allow_local=False, timeout=30):
    parsed = validate_endpoint(url, allow_local=allow_local)
    try:
        rows = _resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), timeout)
        addresses = tuple(dict.fromkeys(row[4][0] for row in rows))
        if not addresses:
            raise OSError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global and not (allow_local and parsed.hostname in ('localhost', '127.0.0.1', '::1') and ip.is_loopback):
                raise AppError('ENDPOINT_BLOCKED', '此地址不允许用于外部服务，请检查服务地址', 422)
        return parsed, addresses
    except (OSError, ValueError):
        raise AppError('SERVICE_NETWORK_ERROR', '无法连接服务，请检查地址和网络', 503) from None


class OutboundClient:
    def post_json(self, url, payload, *, headers=None, allow_local=False, timeout=30, max_bytes=65536, guard=None):
        with guard() if guard else nullcontext():
            pass
        deadline = time.monotonic() + timeout
        parsed, addresses = endpoint_addresses(url, allow_local=allow_local, timeout=timeout)
        content = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(content) > 128 * 1024:
            raise AppError('SERVICE_INPUT_TOO_LARGE', '发送内容超过限制', 422)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError('SERVICE_TIMEOUT', '服务响应超时', 503)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
        sockets, socket_lock, expired = [], threading.Lock(), threading.Event()
        def expire():
            expired.set()
            with socket_lock:
                for sock in sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
        def track(sock):
            with socket_lock:
                sockets.append(sock)
                if expired.is_set():
                    sock.close()
                    raise TimeoutError()
            return sock
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        try:
            # Resolve once and connect to that exact validated address. TLS still checks the original host.
            sock = track(socket.create_connection((addresses[0], port), timeout=remaining))
            if parsed.scheme == 'https':
                try:
                    sock = track(ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname, do_handshake_on_connect=False))
                    sock.do_handshake()
                except Exception:
                    sock.close()
                    raise
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            with guard() if guard else nullcontext():
                connection.request('POST', path, body=content, headers={
                    'Content-Type': 'application/json', 'Accept': 'application/json', **(headers or {})})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise AppError('SERVICE_REDIRECT_BLOCKED', '服务返回跳转，请直接填写最终服务地址', 503)
            data = bytearray()
            while True:
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(remaining)
                chunk = response.read1(min(8192, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise AppError('SERVICE_RESPONSE_TOO_LARGE', '服务返回内容超过限制', 503)
            if expired.is_set():
                raise TimeoutError()
            return response.status, bytes(data)
        except AppError:
            raise
        except (TimeoutError, socket.timeout):
            raise AppError('SERVICE_TIMEOUT', '服务响应超时，本机功能仍可使用', 503) from None
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise AppError('SERVICE_TIMEOUT', '服务响应超时，本机功能仍可使用', 503) from None
            raise AppError('SERVICE_NETWORK_ERROR', '服务连接未完成，请检查网络后重试', 503) from None
        finally:
            timer.cancel()
            connection.close()
