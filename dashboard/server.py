"""Optional loopback-only relay statistics. Based on jdp22's log dashboard (MIT).

No response capture, raw log endpoint, remote binding or external dependencies.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socketserver
import sys
from urllib.parse import urlsplit

STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$')
REQUEST = re.compile(r'^\[(\d+)\] POST /[^ ]*responses(?:\?[^ ]*)? \((\d+)B\)')
RESULT = re.compile(r'^\[(\d+)\] attempt (\d+): (.*)$')
ROOT = Path(__file__).resolve().parent
MAX_LOG = 8 * 1024 * 1024


def metrics(path, minutes=60, now=None):
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=minutes)
    counts = Counter(requests=0, retries=0, started=0, exhausted=0, cancelled=0, http_errors=0)
    buckets = {}
    events = []
    available = True
    try:
        with Path(path).open('rb') as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - MAX_LOG))
            if size > MAX_LOG:
                handle.readline(MAX_LOG)  # discard a partial first line
            lines = handle.read(MAX_LOG).decode('utf8', 'replace').splitlines()
    except OSError:
        available, lines = False, []
    for line in lines:
        match = STAMP.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        if not cutoff <= stamp <= now:
            continue
        message = match[2]
        category = None
        if REQUEST.match(message):
            category = 'requests'
        else:
            result = RESULT.match(message)
            if result:
                detail = result[3]
                if detail.startswith('retryable ('):
                    category = 'retries'
                elif detail == 'ok, streaming':
                    category = 'started'
                elif re.fullmatch(r'upstream [45]\d\d, passthrough', detail):
                    category = 'http_errors'
            elif re.match(r'^\[\d+\] giving up after \d+ attempts$', message):
                category = 'exhausted'
            elif re.match(r'^\[\d+\] abandoning retries after \d+ attempts \(client gone\)$', message):
                category = 'cancelled'
        if category:
            counts[category] += 1
            bucket = stamp.strftime('%H:%M')
            buckets.setdefault(bucket, Counter())[category] += 1
            # Whitelisted categories only: never send original log text.
            events.append({'time': stamp.strftime('%H:%M:%S'), 'kind': category})
    return {'available': available, 'minutes': minutes, 'counts': dict(counts),
            'trend': [{'time': t, 'requests': c['requests'], 'retries': c['retries']}
                      for t, c in buckets.items()], 'events': events[-30:][::-1]}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
        origin = self.headers.get('Origin')
        if self.headers.get('Host') not in hosts or (origin and origin not in {'http://' + h for h in hosts}):
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path == '/api/metrics':
            data = json.dumps(metrics(self.server.log_path, self.server.minutes)).encode()
            ctype = 'application/json'
        elif path == '/':
            data = (ROOT / 'index.html').read_bytes()
            ctype = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.socket.getsockname()[:2]


def default_log():
    if sys.platform == 'win32':
        return Path.home() / '.codex-overload-fix/relay.log'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/codex-overload-fix/relay.log'
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'codex-overload-fix/relay.log'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, default=default_log())
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--minutes', type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.minutes <= 1440:
        parser.error('port must be 1..65535; minutes must be 1..1440')
    server = Server(('127.0.0.1', args.port), Handler)
    server.log_path, server.minutes = args.log, args.minutes
    print('Relay statistics: http://127.0.0.1:%d/' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
