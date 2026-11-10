import pytest

from app.domain import AppError


@pytest.mark.parametrize('url', ['http://public.example/v1', 'https://user:pass@public.example/v1',
    'https://public.example/#secret', 'file:///etc/passwd', 'https://public.example/\r\nX: a'])
def test_invalid_endpoint_shape(url):
    from app.outbound import validate_endpoint
    with pytest.raises(AppError):
        validate_endpoint(url, allow_local=True)


@pytest.mark.parametrize('host', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '192.168.1.5', '100.64.0.1'])
def test_webhook_disallows_nonpublic_network(host):
    from app.outbound import endpoint_addresses
    hostname = '[' + host + ']' if ':' in host else host
    with pytest.raises(AppError):
        endpoint_addresses('https://' + hostname + '/hook')


def test_ai_explicit_loopback_and_mixed_dns(monkeypatch):
    from app.outbound import endpoint_addresses
    assert endpoint_addresses('http://127.0.0.1:1234/v1', allow_local=True)[1][0] == '127.0.0.1'
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('10.0.0.1', 443))])
    with pytest.raises(AppError):
        endpoint_addresses('https://public.example/v1')


def test_pinned_socket_json_size_and_redirects(monkeypatch):
    from app.outbound import OutboundClient
    records = {}
    class Sock:
        def settimeout(self, timeout):
            records['timeout'] = timeout
        def close(self):
            pass
    class Response:
        status = 302
    class Connection:
        def __init__(self, *a, **kw):
            self.sock = None
        def request(self, method, path, body, headers):
            records.update(method=method, path=path, body=body)
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr('app.outbound.endpoint_addresses', lambda *a, **k: (__import__('urllib.parse', fromlist=['urlsplit']).urlsplit('http://localhost:1234/v1'), ('127.0.0.1',)))
    monkeypatch.setattr('socket.create_connection', lambda address, **kw: records.setdefault('address', address) and Sock())
    monkeypatch.setattr('http.client.HTTPConnection', Connection)
    with pytest.raises(AppError) as rejected:
        OutboundClient().post_json('http://localhost:1234/v1', {'test': True}, allow_local=True)
    assert rejected.value.code == 'SERVICE_REDIRECT_BLOCKED'
    assert records['address'] == ('127.0.0.1', 1234) and records['path'] == '/v1'


def test_absolute_deadline_includes_dns(monkeypatch):
    import time
    from app.outbound import OutboundClient
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: time.sleep(.35))
    started = time.monotonic()
    with pytest.raises(AppError) as error:
        OutboundClient().post_json('https://example.test/v1', {}, timeout=.05)
    assert error.value.code == 'SERVICE_TIMEOUT'
    assert time.monotonic() - started < .25


def test_absolute_deadline_interrupts_dripping_response_headers():
    import socket
    import threading
    import time
    from app.outbound import OutboundClient
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    port = server.getsockname()[1]
    def respond():
        connection, _ = server.accept()
        with connection:
            connection.recv(4096)
            try:
                for c in b'HTTP/1.1 200 OK\r\nX-Long-Header: slow-response\r\n\r\n':
                    connection.sendall(bytes([c]))
                    time.sleep(.02)
            except OSError:
                pass
    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(AppError) as error:
            OutboundClient().post_json(f'http://127.0.0.1:{port}/v1', {}, allow_local=True, timeout=.1)
        assert error.value.code == 'SERVICE_TIMEOUT'
        assert time.monotonic() - started < .4
    finally:
        server.close()
        thread.join(2)


@pytest.mark.parametrize('protocol', ['HTTP/1.0', 'HTTP/1.1'])
def test_complete_close_response_is_read_without_closed_socket_error(protocol):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.outbound import OutboundClient
    class Handler(BaseHTTPRequestHandler):
        protocol_version = protocol
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'{}')
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert OutboundClient().post_json(f'http://127.0.0.1:{server.server_port}/v1', {}, allow_local=True) == (200, b'{}')
        finally:
            server.shutdown()
            thread.join(2)
