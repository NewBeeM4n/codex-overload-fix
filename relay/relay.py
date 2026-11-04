#!/usr/bin/env python3
"""Local retry relay for Codex /responses.

Listens on http://127.0.0.1:<port> and forwards to
https://chatgpt.com/backend-api/codex, retrying when the upstream answers
with a server-overload ("Selected model is at capacity") response instead of
handing that failure straight to Codex.

Everything is byte-transparent: request bodies (including zstd-compressed
ones) and auth headers are forwarded untouched. Only the very start of the
SSE stream is inspected to decide whether the attempt succeeded.
"""

import argparse
import gzip
import contextlib
import email.utils
import hashlib
import hmac
import http.client
import io
import json
import os
import random
import select
import socket
import socketserver
import sys
import threading
import time
import zlib
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

OVERLOAD_CODES = {
    "server_overloaded",      # response.failed / response.error
    "server_is_overloaded",   # bare {"type":"error"} event (observed on real traffic)
    "service_unavailable_error",
    "flex_unavailable",
    "queue_full",
}


# Classifier and framing adapted from lighthouser-agent and ballardmandy69;
# non-streaming handling and output coverage inspired by yyx0506 (MIT).
def is_denied(err):
    if not isinstance(err, dict):
        return False
    text = " ".join(str(err.get(k) or "").lower() for k in ("code", "type", "message"))
    return any(word in text for word in ("quota", "billing", "insufficient", "authentication", "unauthorized", "invalid_api_key"))


def is_overload(err):
    if not isinstance(err, dict) or is_denied(err):
        return False
    code, etype, msg = (str(err.get(k) or "").lower() for k in ("code", "type", "message"))
    return code in OVERLOAD_CODES or etype in OVERLOAD_CODES or "at capacity" in msg or "overloaded" in msg


class SSEFrames:
    """Frame multiline SSE data across chunks, including CR, LF and CRLF."""
    def __init__(self):
        self.line = bytearray()
        self.data = []
        self.after_cr = False

    def feed(self, chunk):
        for char in chunk:
            if self.after_cr:
                self.after_cr = False
                if char == 10:
                    continue
            if char in (10, 13):
                line = bytes(self.line)
                self.line.clear()
                self.after_cr = char == 13
                if not line:
                    if self.data:
                        payload = b"\n".join(self.data)
                        self.data.clear()
                        yield payload
                elif line.startswith(b"data:"):
                    value = line[5:]
                    self.data.append(value[1:] if value.startswith(b" ") else value)
            else:
                self.line.append(char)


def retry_after(headers):
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, min(delay, 86400.0))


class InterruptibleReader(io.RawIOBase):
    """Poll socket reads below BufferedReader, where timeouts remain resumable."""
    def __init__(self, sock, watch, idle_timeout):
        self.sock, self.watch, self.idle_timeout = sock, watch, idle_timeout
        self.reference = sock.makefile('rb', buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        deadline = time.monotonic() + self.idle_timeout
        while True:
            if self.watch.reason:
                raise OSError(self.watch.reason)
            if time.monotonic() >= deadline:
                raise TimeoutError('upstream idle timeout')
            try:
                return self.sock.recv_into(buffer)
            except socket.timeout:
                continue

    def close(self):
        self.reference.close()
        super().close()


class WatchedSocket:
    def __init__(self, sock, watch, timeout):
        self.sock, self.watch, self.timeout = sock, watch, timeout
        sock.settimeout(min(0.2, timeout))

    def makefile(self, mode, buffering=None):
        return io.BufferedReader(InterruptibleReader(self.sock, self.watch, self.timeout))

    def __getattr__(self, name):
        return getattr(self.sock, name)


class ConnectionWatch:
    """Cancel blocked I/O without replaying an ambiguously accepted POST."""
    def __init__(self, conn, cancelled, deadline):
        self.conn, self.cancelled, self.deadline = conn, cancelled, deadline
        self.sock = None
        self.committed = False
        self.reason = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.done.wait(0.05):
            with self.lock:
                if self.cancelled():
                    self.reason = "client closed"
                elif not self.committed and time.monotonic() >= self.deadline:
                    self.reason = "deadline"
                if not self.reason:
                    continue
                sock = self.sock or self.conn.sock
                if sock:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)
                    return

    def commit(self):
        with self.lock:
            if self.reason:
                raise OSError(self.reason)
            self.committed = True

    def close(self):
        self.done.set()
        self.thread.join(timeout=0.2)


LOG_LOCK = threading.Lock()
LOG_SIZE = [0]
LOG_MAX = 8 * 1024 * 1024


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        if ARGS.log:
            try:
                if LOG_SIZE[0] > LOG_MAX:
                    os.replace(ARGS.log, ARGS.log + ".1")
                    LOG_SIZE[0] = 0
                with open(ARGS.log, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                LOG_SIZE[0] += len(line) + 1
            except OSError:
                pass


ARGS = None


def upstream_connection(timeout):
    host = ARGS.upstream_host
    port = ARGS.upstream_port
    if ARGS.proxy:
        phost, pport = ARGS.proxy
        conn = http.client.HTTPSConnection(phost, pport, timeout=timeout)
        conn.set_tunnel(host, port)
        return conn
    if ARGS.upstream_scheme == "http":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    return http.client.HTTPSConnection(host, port, timeout=timeout)


def read_request_body(handler):
    te = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        chunks = []
        while True:
            line = handler.rfile.readline(65536)
            if not line:
                break
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline(65536)
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline(65536)
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


def forward_headers(handler):
    out = {}
    for k, v in handler.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    out["Host"] = ARGS.upstream_host
    out["Accept-Encoding"] = "identity"
    return out


def classify_event(payload):
    """Only known empty setup events may be buffered; unknown output commits."""
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return "commit", "unknown event"
    if not isinstance(evt, dict):
        return "commit", "unknown event"
    kind = evt.get("type")
    response = evt.get("response")
    response = response if isinstance(response, dict) else {}
    err = response.get("error") or evt.get("error")
    if kind in ("error", "response.error", "response.failed"):
        err = err if isinstance(err, dict) else evt
        return ("retry", "overload") if is_overload(err) else ("commit", "error event")
    if kind in ("response.created", "response.in_progress", "response.queued", "response.metadata", "ping"):
        return ("commit", "lifecycle output") if response.get("output") else (None, None)
    item = evt.get("item")
    if (kind == "response.output_item.added" and isinstance(item, dict)
            and item.get("type") == "message" and item.get("role") == "assistant"
            and item.get("content") == []
            and set(item) <= {"id", "type", "role", "status", "content", "phase"}):
        return None, None
    return "commit", "output event"


def attempt(path, headers, body, timeout, commit_by, cancelled=lambda: False):
    """Preserve response bytes; retry explicit refusals or pre-send connect errors."""
    conn = upstream_connection(min(timeout, max(0.05, commit_by - time.monotonic())))
    watch = ConnectionWatch(conn, cancelled, commit_by)
    retained = False
    resp = None
    try:
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            return "retry", 0, {}, b"", None, "connect error: " + type(exc).__name__
        watch.sock = conn.sock
        conn.request("POST", path, body=body, headers=headers)
        conn.sock = WatchedSocket(conn.sock, watch, timeout)
        resp = conn.getresponse()
        response_headers = dict(resp.getheaders())
        if resp.status != 200:
            data = resp.read()
            try:
                doc = json.loads(data)
                error = doc.get("error", doc) if isinstance(doc, dict) else {}
            except ValueError:
                error = {}
            mode = "retry" if resp.status in (429, 500, 502, 503, 504) and not is_denied(error) else "passthrough"
            return mode, resp.status, response_headers, data, None, "http " + str(resp.status)
        ctype = resp.getheader("Content-Type", "").lower()
        encoding = resp.getheader("Content-Encoding", "identity").lower()
        if "text/event-stream" not in ctype or encoding not in ("", "identity"):
            # No SSE inspection of JSON, binary, or compressed bodies.
            watch.commit()
            conn._relay_watch = watch
            retained = True
            return "stream", 200, response_headers, b"", conn, resp
        prelude = bytearray()
        parser = SSEFrames()
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                # EOF is not evidence of overload. Surface it without replay.
                return "passthrough", 502, {"Content-Type": "text/plain"}, b"relay: upstream ended before output\n", None, "early EOF"
            prelude.extend(chunk)
            for payload in parser.feed(chunk):
                if not payload:
                    continue
                verdict, info = classify_event(payload)
                if verdict == "retry":
                    return "retry", 200, response_headers, bytes(prelude), None, info
                if verdict == "commit":
                    break
            else:
                verdict = None
            if verdict == "commit" or len(prelude) >= 262144:
                watch.commit()
                conn._relay_watch = watch
                retained = True
                return "stream", 200, response_headers, bytes(prelude), conn, resp
    except (OSError, ValueError, http.client.HTTPException):
        status = 504 if watch.reason == "deadline" or time.monotonic() >= commit_by else 502
        return "passthrough", status, {"Content-Type": "text/plain"}, b"relay: upstream interrupted; request not replayed\n", None, "transport interrupted"
    finally:
        if not retained:
            watch.close()
            if resp is not None:
                resp.close()
            conn.close()


def analyze(handler, body):
    """One-line size breakdown of a /responses body. Sizes only, no content."""
    if len(body) > 64 << 20:
        return f" | analyze-skipped({len(body)}B too large)"
    enc = (handler.headers.get("Content-Encoding") or "").lower()
    try:
        if enc in ("", "identity"):
            raw = body
        elif enc == "gzip":
            raw = gzip.decompress(body)
        elif enc == "deflate":
            raw = zlib.decompress(body)
        elif enc == "zstd":
            try:
                from compression import zstd
                raw = zstd.decompress(body)
            except Exception:
                import zstandard
                raw = zstandard.ZstdDecompressor().decompress(body, max_output_size=64 << 20)
        else:
            return f" enc={enc} analysis-skipped"
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return f" analyze-failed({exc.__class__.__name__})"

    parts = []
    if doc.get("model"):
        parts.append(f"model={doc['model']}")
    _r = doc.get("reasoning") or {}
    if isinstance(_r, dict) and _r.get("effort"):
        parts.append(f"effort={_r['effort']}")
    if doc.get("service_tier"):
        parts.append(f"tier={doc['service_tier']}")
    if isinstance(doc.get("instructions"), str):
        parts.append(f"instructions={len(doc['instructions'])}c")
    tools = doc.get("tools") or []
    if tools:
        tb = len(json.dumps(tools, ensure_ascii=False).encode())
        parts.append(f"tools={tb}B/{len(tools)}")
    items = doc.get("input") or []
    if isinstance(items, list):
        by = {}
        for it in items:
            role = it.get("type") or "?"
            if role == "message":
                role = "msg:" + (it.get("role") or "?")
            b = len(json.dumps(it, ensure_ascii=False).encode())
            by[role] = by.get(role, 0) + b
        top = ",".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4])
        parts.append(f"input={len(json.dumps(items, ensure_ascii=False).encode())}B/{len(items)}it({top})")
    if doc.get("prompt_cache_key"):
        parts.append("cache_key=yes")
    return " | " + (" ".join(parts) if parts else "empty")


class LocalRelayServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind performs reverse DNS before listen(). That can
        # block indefinitely on macOS even though this server is loopback-only.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.socket.getsockname()[:2]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    def _proxy(self):
        # A nonce signed by the installer's local key proves a listener knows
        # that key without sending the key or any Codex credentials to it.
        parsed = urlsplit(self.path)
        if self.command == "GET" and parsed.path == "/__codex_overload_health":
            nonce = parse_qs(parsed.query).get("nonce", [""])[0]
            if ARGS.identity_key and len(nonce) == 64 and all(c in "0123456789abcdef" for c in nonce):
                proof = nonce + ":" + ARGS.relay_digest
                signature = hmac.new(ARGS.identity_key, proof.encode("ascii"), hashlib.sha256).hexdigest()
                self._send_simple(200, {"Content-Type": "text/plain"}, (ARGS.relay_digest + ":" + signature).encode("ascii"))
            else:
                self._send_simple(404, {"Content-Type": "text/plain"}, b"not found\n")
            return
        path = self.path
        upstream_path = ARGS.upstream_prefix + path
        body = read_request_body(self)
        headers = forward_headers(self)

        is_responses = self.command == "POST" and path.split("?")[0].endswith("/responses")
        if not is_responses:
            self._passthrough(upstream_path, headers, body)
            return

        cport = self.client_address[1]
        log(f"[{cport}] {self.command} {path} ({len(body)}B){analyze(self, body)}")
        deadline = time.monotonic() + ARGS.total_budget
        attempt_no = 0
        last = None
        while True:
            if self._client_gone():
                # Codex cancelled the turn or exited; stop burning upstream quota.
                log(f"[{cport}] abandoning retries after {attempt_no} attempts (client gone)")
                return
            if time.monotonic() >= deadline:
                log(f"[{cport}] giving up after {attempt_no} attempts")
                if last:
                    self._retry_failure(*last)
                else:
                    self._send_simple(504, {"Content-Type": "text/plain"}, b"relay: retry deadline reached\n")
                return
            attempt_no += 1
            mode, status, resp_headers, prelude, conn, resp = attempt(
                upstream_path, headers, body, ARGS.timeout,
                min(deadline, time.monotonic() + ARGS.commit_deadline), self._client_gone,
            )
            if mode == "stream":
                log(f"[{cport}] attempt {attempt_no}: ok, streaming")
                self._stream_back(resp_headers, prelude, conn, resp)
                return
            if mode == "passthrough":
                log(f"[{cport}] attempt {attempt_no}: upstream {status}, passthrough")
                self._send_simple(status, resp_headers, prelude)
                return
            last = (status, resp_headers, prelude)
            log(f"[{cport}] attempt {attempt_no}: retryable ({resp})")
            if attempt_no >= ARGS.max_attempts or time.monotonic() >= deadline:
                log(f"[{cport}] giving up after {attempt_no} attempts")
                self._retry_failure(*last)
                return
            delay = min(ARGS.backoff_max, ARGS.backoff_base * (1.6 ** (attempt_no - 1)))
            delay = max(delay * random.uniform(0.7, 1.3), retry_after(resp_headers))
            end = min(deadline, time.monotonic() + delay)
            while time.monotonic() < end:
                if self._client_gone():
                    log(f"[{cport}] abandoning retries after {attempt_no} attempts (client gone)")
                    return
                time.sleep(min(0.1, max(0, end - time.monotonic())))

    def _retry_failure(self, status, headers, body):
        if status:
            self._send_simple(status, headers, body)
        else:
            self._send_simple(503, {"Content-Type": "text/plain"}, b"relay: upstream unavailable\n")

    def _client_gone(self):
        """True when the Codex side has closed its end of the socket."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _passthrough(self, path, headers, body):
        started = time.time()
        conn = upstream_connection(ARGS.timeout)
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self._send_simple(resp.status, dict(resp.getheaders()), data)
            log(f"[{self.client_address[1]}] {self.command} {path} -> {resp.status} "
                f"{len(data)}B in {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"[{self.client_address[1]}] {self.command} {path} -> error {exc!r}")
            self._send_simple(502, {"Content-Type": "text/plain"}, f"relay: {exc}\n".encode())
        finally:
            conn.close()

    def _stream_back(self, resp_headers, prelude, conn, resp):
        # Once committed, preserve all bytes and close even if header delivery fails.
        try:
            self.send_response(200)
            ctype = next((v for k, v in resp_headers.items() if k.lower() == "content-type"), "text/event-stream")
            self.send_header("Content-Type", ctype)
            for key, value in resp_headers.items():
                if key.lower() not in HOP_BY_HOP | {"content-type", "cache-control"}:
                    self.send_header(key, value)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(prelude)
            self.wfile.flush()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            watch = getattr(conn, "_relay_watch", None)
            if watch:
                watch.close()
            if hasattr(resp, 'close'):
                resp.close()
            conn.close()

    def _send_simple(self, status, headers, data):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy


def parse_upstream(value):
    scheme = "https"
    if value.startswith("http://"):
        scheme, value = "http", value[len("http://"):]
    elif value.startswith("https://"):
        value = value[len("https://"):]
    hostport, _, path = value.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port) if port else (80 if scheme == "http" else 443)
    prefix = "/" + path.strip("/") if path.strip("/") else ""
    return scheme, host, port, prefix


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--upstream", default="chatgpt.com/backend-api/codex")
    p.add_argument("--proxy", default="", help="e.g. http://127.0.0.1:7897")
    p.add_argument("--max-attempts", type=int, default=30)
    p.add_argument("--total-budget", type=float, default=300.0)
    p.add_argument("--backoff-base", type=float, default=1.0)
    p.add_argument("--backoff-max", type=float, default=8.0)
    p.add_argument("--commit-deadline", type=float, default=120.0,
                   help="max seconds to hold the stream head while deciding to retry")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--log", default="")
    p.add_argument("--identity-file", default="", help="local key for installer readiness verification")
    ARGS = p.parse_args()
    ARGS.identity_key = b""
    with open(__file__, "rb") as f:
        ARGS.relay_digest = hashlib.sha256(f.read()).hexdigest()
    if ARGS.identity_file:
        with open(ARGS.identity_file, "rb") as f:
            ARGS.identity_key = f.read()
        if len(ARGS.identity_key) != 32:
            p.error("invalid relay identity key")

    if ARGS.log and os.path.exists(ARGS.log):
        try:
            LOG_SIZE[0] = os.path.getsize(ARGS.log)
        except OSError:
            LOG_SIZE[0] = 0
    ARGS.upstream_scheme, ARGS.upstream_host, ARGS.upstream_port, ARGS.upstream_prefix = (
        parse_upstream(ARGS.upstream)
    )
    if ARGS.proxy:
        host, _, port = ARGS.proxy.replace("http://", "").partition(":")
        ARGS.proxy = (host, int(port or 80))

    srv = LocalRelayServer(("127.0.0.1", ARGS.port), Handler)
    log(f"relay listening on 127.0.0.1:{ARGS.port} -> {ARGS.upstream_scheme}://{ARGS.upstream} via {ARGS.proxy}")
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
