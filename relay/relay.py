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
import hashlib
import hmac
import http.client
import json
import os
import random
import select
import socket
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


def is_overload(err):
    code = (err.get("code") or "").lower()
    etype = (err.get("type") or "").lower()
    msg = (err.get("message") or "").lower()
    return (
        code in OVERLOAD_CODES
        or etype in OVERLOAD_CODES
        or "at capacity" in msg
        or "overloaded" in msg
    )

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
    """Return ('retry'|'commit'|None, info)."""
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return None, None
    kind = evt.get("type")
    if kind == "response.failed":
        err = (evt.get("response") or {}).get("error") or {}
        code = err.get("code") or ""
        msg = err.get("message") or ""
        if is_overload(err):
            return "retry", f"overload: {code or 'unknown'}"
        return "commit", f"non-retryable failure {code}"
    if kind in (
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.output_item.done",
        "response.completed",
        "response.incomplete",
    ):
        return "commit", kind
    if kind == "error":
        err = evt.get("error") or {}
        if is_overload(err):
            return "retry", f"error event overload: {(err.get('code') or err.get('type') or '')}"
        return "commit", "error event"
    return None, None


def attempt(path, headers, body, timeout, commit_by):
    """One upstream attempt.

    Returns (mode, status, resp_headers, prelude, conn, resp)
    mode in {"stream", "retry", "passthrough"}
    """
    conn = upstream_connection(timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
    except Exception as exc:  # noqa: BLE001 - network failures are retryable
        conn.close()
        return "retry", 0, {}, b"", None, f"connect/read error: {exc.__class__.__name__}"

    if resp.status != 200:
        data = b""
        try:
            data = resp.read()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
        status = resp.status
        if status in (429, 500, 502, 503, 504):
            return "retry", status, dict(resp.getheaders()), data, None, f"http {status}"
        return "passthrough", status, dict(resp.getheaders()), data, None, ""

    prelude = bytearray()
    buf = b""
    seen = 0
    while True:
        try:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        except Exception as exc:  # noqa: BLE001
            conn.close()
            return "retry", 0, {}, b"", None, f"stream error: {exc.__class__.__name__}"
        if not chunk:
            conn.close()
            return "retry", 0, {}, b"", None, "stream ended early"
        prelude.extend(chunk)
        buf += chunk
        # Only need the head of the stream to make the call.
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            seen += 1
            if seen <= 12:
                try:
                    _ev = json.loads(payload)
                    _t = _ev.get("type")
                except (ValueError, TypeError):
                    _ev, _t = {}, "?"
                _served = (_ev.get("response") or {}).get("model") if _ev else None
                _tag = f"[served={_served}] " if _served else ""
                log(f"  evt#{seen} {_t} {_tag}".rstrip())
            verdict, info = classify_event(payload)
            if verdict == "retry":
                conn.close()
                return "retry", 200, {}, bytes(prelude), None, info or "overload"
            if verdict == "commit":
                return "stream", 200, dict(resp.getheaders()), bytes(prelude), conn, resp
        if len(prelude) > 262144:
            # Nothing conclusive after 256 KiB - assume it is a real stream.
            return "stream", 200, dict(resp.getheaders()), bytes(prelude), conn, resp
        if time.time() >= commit_by:
            # Held the head back long enough; stop delaying the client.
            return "stream", 200, dict(resp.getheaders()), bytes(prelude), conn, resp


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
        deadline = time.time() + ARGS.total_budget
        attempt_no = 0
        last = None
        while True:
            if self._client_gone():
                # Codex cancelled the turn or exited; stop burning upstream quota.
                log(f"abandoning retries after {attempt_no} attempts (client gone)")
                return
            attempt_no += 1
            mode, status, resp_headers, prelude, conn, resp = attempt(
                upstream_path, headers, body, ARGS.timeout,
                time.time() + ARGS.commit_deadline,
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
            if attempt_no >= ARGS.max_attempts or time.time() >= deadline:
                log(f"[{cport}] giving up after {attempt_no} attempts")
                if last[0] == 200 and last[2]:
                    self._send_simple(200, {"Content-Type": "text/event-stream"}, last[2])
                elif last[0] and last[0] != 200:
                    self._send_simple(last[0], last[1], last[2])
                else:
                    self._send_simple(503, {"Content-Type": "text/plain"}, b"relay: upstream unavailable\n")
                return
            delay = min(ARGS.backoff_max, ARGS.backoff_base * (1.6 ** (attempt_no - 1)))
            delay *= random.uniform(0.7, 1.3)
            time.sleep(delay)

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
        self.send_response(200)
        ctype = resp_headers.get("Content-Type") or "text/event-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        # Once any event is committed, never retry or discard later data. The
        # client owns recovery after that point, including partial tool output.
        try:
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

    log(f"relay listening on 127.0.0.1:{ARGS.port} -> https://{ARGS.upstream} via {ARGS.proxy}")
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
