#!/usr/bin/env python3
"""Create a local identity key and verify an installed relay's challenge."""
import argparse
import hashlib
import hmac
import http.client
import os
from pathlib import Path
import secrets
import socket
import sys


def init(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as f:
            f.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('invalid identity key')
    return key


def check(path, port, relay_path):
    key = Path(path).read_bytes()
    if len(key) != 32:
        return False
    digest = hashlib.sha256(Path(relay_path).read_bytes()).hexdigest()
    nonce = secrets.token_hex(32)
    proof = nonce + ':' + digest
    expected = digest + ':' + hmac.new(key, proof.encode('ascii'), hashlib.sha256).hexdigest()
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        conn.request('GET', '/__codex_overload_health?nonce=' + nonce)
        response = conn.getresponse()
        return response.status == 200 and hmac.compare_digest(response.read(256), expected.encode('ascii'))
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'check', 'occupied', 'packaged'])
    parser.add_argument('--identity-file', required=True)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--relay', default=str(Path(__file__).with_name('relay.py')))
    args = parser.parse_args()
    try:
        if args.action == 'packaged':
            if os.name != 'nt':
                return 1
            import ctypes
            size = ctypes.c_uint32(0)
            status = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(size), None)
            if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE
                return 1
            if status == 122:  # ERROR_INSUFFICIENT_BUFFER
                return 0
            raise OSError('cannot determine package identity (%d)' % status)
        if args.action == 'init':
            init(args.identity_file)
            return 0
        if args.action == 'occupied':
            with socket.socket() as sock:
                sock.settimeout(1)
                return 0 if sock.connect_ex(('127.0.0.1', args.port)) == 0 else 1
        return 0 if check(args.identity_file, args.port, args.relay) else 1
    except (OSError, ValueError) as exc:
        print('relay identity check failed: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
