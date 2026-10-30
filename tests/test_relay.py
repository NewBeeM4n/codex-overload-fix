"""Isolated HTTP tests for retry, streaming, privacy and local identity."""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
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

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).calls += 1
        status, content_type, payload = type(self).queued.pop(0)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Retry-After', '17')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def event(value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), MockUpstream)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), relay.Handler)
        for server in (cls.upstream, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.upstream, cls.server):
            server.shutdown()
            server.server_close()

    def setUp(self):
        MockUpstream.calls = 0
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
