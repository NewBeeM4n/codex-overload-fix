"""Isolated HTTP tests for retry, streaming, privacy and local identity."""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import ssl
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('relay', ROOT / 'relay' / 'relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
spec = importlib.util.spec_from_file_location('identity', ROOT / 'relay' / 'identity.py')
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class MockUpstream(BaseHTTPRequestHandler):
    queued = []
    calls = 0
    retry_delay = '0'

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).calls += 1
        status, content_type, payload = type(self).queued.pop(0)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Retry-After', type(self).retry_delay)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def event(value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


class RelayStartupTests(unittest.TestCase):
    def test_startup_never_waits_for_reverse_dns(self):
        with mock.patch('socket.getfqdn', side_effect=AssertionError('unexpected reverse DNS')):
            server = relay.LocalRelayServer(('127.0.0.1', 0), relay.Handler)
            try:
                self.assertEqual(server.server_name, '127.0.0.1')
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = relay.LocalRelayServer(('127.0.0.1', 0), MockUpstream)
        cls.server = relay.LocalRelayServer(('127.0.0.1', 0), relay.Handler)
        for server in (cls.upstream, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.upstream, cls.server):
            server.shutdown()
            server.server_close()

    def setUp(self):
        MockUpstream.calls = 0
        MockUpstream.retry_delay = '0'
        MockUpstream.queued = []
        relay.ARGS = types.SimpleNamespace(
            upstream_host='127.0.0.1', upstream_port=self.upstream.server_port,
            upstream_scheme='http', upstream_prefix='', proxy=None, log='',
            total_budget=3, max_attempts=1, timeout=2, commit_deadline=2,
            backoff_max=0, backoff_base=0, identity_key=b'', relay_digest='')

    def request(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request('POST', '/responses', b'{}')
        resp = conn.getresponse()
        result = resp.status, dict(resp.getheaders()), resp.read()
        conn.close()
        return result

    def test_retries_overload_before_output(self):
        overload = event({'type': 'error', 'error': {'code': 'server_is_overloaded'}})
        ok = event({'type': 'response.output_text.delta', 'delta': 'test'})
        MockUpstream.queued = [(200, 'text/event-stream', overload), (200, 'text/event-stream', ok)]
        relay.ARGS.max_attempts = 2
        status, _, body = self.request()
        self.assertEqual((status, body, MockUpstream.calls), (200, ok, 2))

    def test_exhausted_429_preserves_status_headers_and_body(self):
        MockUpstream.retry_delay = '17'
        MockUpstream.queued = [(429, 'application/json', b'{"error":"rate limited"}')]
        status, headers, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Retry-After'], '17')
        self.assertEqual(body, b'{"error":"rate limited"}')

    def test_exhausted_sse_overload_preserves_event(self):
        payload = event({'type': 'error', 'error': {'code': 'server_is_overloaded'}})
        MockUpstream.queued = [(200, 'text/event-stream', payload)]
        self.assertEqual(self.request()[2], payload)

    def test_event_text_is_not_logged(self):
        payload = event({'type': 'response.output_text.delta', 'delta': 'SYNTHETIC_PRIVATE_CONTENT'})
        MockUpstream.queued = [(200, 'text/event-stream', payload)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.request()[2], payload)
        self.assertNotIn('SYNTHETIC_PRIVATE_CONTENT', output.getvalue())

    def test_committed_stream_does_not_drop_text_or_retry(self):
        class Handler:
            def __init__(self): self.wfile = io.BytesIO()
            def send_response(self, *args): pass
            def send_header(self, *args): pass
            def end_headers(self): pass
        class Connection:
            def close(self): pass
        class Response:
            def __init__(self, chunk): self.chunks = [chunk, b'']
            def read1(self, size): return self.chunks.pop(0)
        prelude = event({'type': 'response.reasoning_summary_text.delta', 'delta': 'thinking'})
        chunk = event({'type': 'response.output_text.delta', 'delta': 'hello'}) + event({'type': 'error', 'error': {'code': 'server_is_overloaded'}})
        handler = Handler()
        relay.Handler._stream_back(handler, {}, prelude, Connection(), Response(chunk))
        self.assertEqual(handler.wfile.getvalue(), prelude + chunk)

    def test_valid_json_passes_once_with_content_type(self):
        payload = b'{"id":"synthetic","output":[]}'
        MockUpstream.queued = [(200, 'application/json', payload)]
        relay.ARGS.max_attempts = 3
        status, headers, body = self.request()
        self.assertEqual((status, body, MockUpstream.calls), (200, payload, 1))
        self.assertEqual(headers['Content-Type'], 'application/json')

    def test_tool_image_and_unknown_output_never_replayed(self):
        for kind in ('response.function_call_arguments.delta',
                     'response.image_generation_call.partial_image',
                     'response.reasoning_text.delta', 'future.output.event'):
            with self.subTest(kind=kind):
                MockUpstream.calls = 0
                payload = event({'type': kind, 'delta': 'synthetic'}) + event({'type': 'error', 'error': {'code': 'server_is_overloaded'}})
                MockUpstream.queued = [(200, 'text/event-stream', payload)]
                relay.ARGS.max_attempts = 3
                self.assertEqual(self.request()[2], payload)
                self.assertEqual(MockUpstream.calls, 1)

    def test_multiline_crlf_sse_overload_retries(self):
        overload = b'data: {"type":"response.error",\r\ndata: "error":{"code":"server_is_overloaded"}}\r\n\r\n'
        output = event({'type': 'response.completed'})
        MockUpstream.queued = [(200, 'text/event-stream', overload), (200, 'text/event-stream', output)]
        relay.ARGS.max_attempts = 2
        self.assertEqual(self.request()[2], output)
        self.assertEqual(MockUpstream.calls, 2)

    def test_quota_error_does_not_retry_even_if_message_mentions_overload(self):
        payload = event({'type': 'error', 'error': {'code': 'insufficient_quota', 'message': 'overloaded'}})
        MockUpstream.queued = [(200, 'text/event-stream', payload)]
        relay.ARGS.max_attempts = 3
        self.assertEqual(self.request()[2], payload)
        self.assertEqual(MockUpstream.calls, 1)

    def test_retry_after_outlives_budget_preserves_error_without_resend(self):
        MockUpstream.retry_delay = '17'
        MockUpstream.queued = [(429, 'application/json', b'{"error":"rate limited"}')]
        relay.ARGS.max_attempts = 3
        relay.ARGS.total_budget = 0.2
        started = time.monotonic()
        status, headers, body = self.request()
        self.assertEqual((status, MockUpstream.calls), (429, 1))
        self.assertEqual(headers['Retry-After'], '17')
        self.assertLess(time.monotonic() - started, 2)

    def test_identity_rejects_other_key_and_non_relay_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key'
            other = Path(tmp) / 'other'
            relay.ARGS.identity_key = identity.init(path)
            identity.init(other)
            relay.ARGS.relay_digest = hashlib.sha256((ROOT / 'relay' / 'relay.py').read_bytes()).hexdigest()
            self.assertTrue(identity.check(path, self.server.server_port, ROOT / 'relay' / 'relay.py'))
            self.assertFalse(identity.check(other, self.server.server_port, ROOT / 'relay' / 'relay.py'))
            self.assertFalse(identity.check(path, self.upstream.server_port, ROOT / 'relay' / 'relay.py'))
            different = Path(tmp) / 'changed.py'
            different.write_text('changed')
            self.assertFalse(identity.check(path, self.server.server_port, different))


class FramingAndCancellationTests(unittest.TestCase):
    def test_frames_across_every_byte_with_all_line_endings(self):
        parser = relay.SSEFrames()
        payload = b'data: {"a":\r\ndata: 1}\r\n\r\n: comment\r\rdata: next\n\n'
        results = []
        for byte in payload:
            results.extend(parser.feed(bytes([byte])))
        self.assertEqual(results, [b'{"a":\n1}', b'next'])

    def test_retry_after_supports_http_date(self):
        with mock.patch.object(relay.time, 'time', return_value=0):
            self.assertEqual(relay.retry_after({'retry-after': 'Thu, 01 Jan 1970 00:00:05 GMT'}), 5)
        self.assertEqual(relay.retry_after({'Retry-After': 'bad'}), 0)

    def run_blocked_attempt(self, cancel=False):
        accepted = threading.Event()
        finished = threading.Event()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            def upstream():
                conn, _ = listener.accept()
                with conn:
                    conn.recv(65536)
                    accepted.set()
                    finished.wait(3)
            worker = threading.Thread(target=upstream, daemon=True)
            worker.start()
            started = time.monotonic()
            cancelled = lambda: cancel and accepted.is_set()
            try:
                with mock.patch.object(relay, 'upstream_connection', side_effect=lambda timeout: http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)):
                    result = relay.attempt('/responses', {}, b'{}', 10, started + (2 if cancel else 0.3), cancelled)
                self.assertEqual(result[0], 'passthrough')
                self.assertIn(result[1], (502, 504))
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                finished.set()
                worker.join(2)

    def test_https_stream_survives_read_polling_and_commit_deadline(self):
        openssl = shutil.which('openssl')
        if not openssl and Path(r'C:\Program Files\Git\usr\bin\openssl.exe').exists():
            openssl = r'C:\Program Files\Git\usr\bin\openssl.exe'
        if not openssl:
            self.skipTest('openssl not available for disposable TLS fixture')
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / 'cert.pem', Path(tmp) / 'key.pem'
            subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=localhost',
                            '-days', '1', '-out', str(cert), '-keyout', str(key)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            first = event({'type': 'response.output_text.delta', 'delta': 'test'})
            last = event({'type': 'response.completed'})
            class SlowTLS(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(first + last)))
                    self.end_headers()
                    self.wfile.write(first)
                    self.wfile.flush()
                    time.sleep(0.5)
                    self.wfile.write(last)
            server = relay.LocalRelayServer(('127.0.0.1', 0), SlowTLS)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client_context = ssl.create_default_context(cafile=str(cert))
            client_context.check_hostname = False  # isolated localhost fixture certificate
            try:
                with mock.patch.object(relay, 'upstream_connection', side_effect=lambda timeout: http.client.HTTPSConnection('127.0.0.1', server.server_port, timeout=timeout, context=client_context)):
                    mode, status, headers, head, conn, response = relay.attempt('/responses', {}, b'{}', 2, time.monotonic() + 0.4)
                self.assertEqual((mode, status), ('stream', 200))
                try:
                    self.assertEqual(head + response.read(), first + last)
                finally:
                    conn._relay_watch.close()
                    response.close()
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_deadline_interrupts_wait_for_headers(self):
        self.run_blocked_attempt()

    def test_cancel_interrupts_wait_for_headers(self):
        self.run_blocked_attempt(cancel=True)


@unittest.skipIf(os.name == 'nt', 'Unix launchers')
class UnixInstallerTests(unittest.TestCase):
    def test_uninstaller_without_editor_does_not_stop_service(self):
        for platform in ('linux', 'mac'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                scripts = home / 'scripts'
                scripts.mkdir()
                script = scripts / ('uninstall-' + platform + '.sh')
                script.write_bytes((ROOT / 'scripts' / script.name).read_bytes())
                env = dict(os.environ, HOME=tmp, CODEX_HOME=str(home / 'codex'),
                           XDG_CONFIG_HOME=str(home / 'xdg-config'),
                           XDG_STATE_HOME=str(home / 'xdg-state'))
                run = subprocess.run(['bash', str(script)], env=env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                self.assertNotEqual(0, run.returncode, run.stdout)
                self.assertIn('config.toml and the relay were left untouched', run.stdout)

    def test_no_autostart_never_points_config_at_a_dead_port(self):
        for platform in ('linux', 'mac'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                codex = home / 'codex'
                codex.mkdir()
                config = codex / 'config.toml'
                config.write_text('model = "keep"\n')
                env = dict(os.environ, HOME=tmp, CODEX_HOME=str(codex),
                           XDG_CONFIG_HOME=str(home / 'xdg-config'),
                           XDG_STATE_HOME=str(home / 'xdg-state'))
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                run = subprocess.run(['bash', str(ROOT / 'scripts' / ('install-' + platform + '.sh')),
                                      '--port', str(port), '--no-autostart'], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
                self.assertEqual(0, run.returncode, run.stdout)
                self.assertEqual('model = "keep"\n', config.read_text())
                self.assertIn('config.toml was not changed', run.stdout)


if __name__ == '__main__':
    unittest.main()
