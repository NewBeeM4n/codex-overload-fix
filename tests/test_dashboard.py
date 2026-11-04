import importlib.util
from datetime import datetime
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard', ROOT / 'dashboard/server.py')
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def test_statistics_do_not_expose_log_text_or_count_forwarding_as_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'relay.log'
            path.write_text('\n'.join([
                '2026-11-04 12:00:01 [10] POST /responses (123B) | SECRET_PRIVATE_CONTENT',
                '2026-11-04 12:00:02 [10] attempt 1: retryable (SECRET_PRIVATE_CONTENT)',
                '2026-11-04 12:00:03 [10] attempt 2: ok, streaming',
                '2026-11-04 12:00:04 evt#1 SECRET_PRIVATE_CONTENT',
                '2026-11-04 12:00:05 [11] attempt 1: upstream 401, passthrough',
                '2026-11-04 12:00:06 [12] giving up after 3 attempts',
                '2026-11-04 12:00:07 [13] abandoning retries after 1 attempts (client gone)',
                '2026-11-03 12:00:00 [14] POST /responses (8B)',
            ]), encoding='utf8')
            result = dashboard.metrics(path, now=datetime(2026, 11, 4, 12, 10))
            self.assertEqual(result['counts'], dict(requests=1, retries=1, started=1, exhausted=1, cancelled=1, http_errors=1))
            self.assertNotIn('SECRET_PRIVATE_CONTENT', json.dumps(result))
            self.assertNotIn('successful', result['counts'])
            self.assertFalse(dashboard.metrics(Path(tmp) / 'missing')['available'])

    def test_foreign_origin_and_host_rejected_and_raw_log_route_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = dashboard.Server(('127.0.0.1', 0), dashboard.Handler)
            server.log_path, server.minutes = Path(tmp) / 'missing', 60
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def get(path, headers=None):
                client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    client.request('GET', path, headers=headers or {})
                    response = client.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    client.close()
            try:
                status, headers, body = get('/api/metrics')
                self.assertEqual(status, 200)
                self.assertNotIn('Access-Control-Allow-Origin', headers)
                self.assertFalse(json.loads(body)['available'])
                self.assertEqual(get('/api/metrics', {'Origin': 'https://foreign.example'})[0], 403)
                self.assertEqual(get('/api/metrics', {'Host': 'foreign.example'})[0], 403)
                self.assertEqual(get('/relay.log')[0], 404)
                self.assertEqual(get('/')[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
