#!/usr/bin/env python3
"""Point Codex at the local relay by editing only this project's TOML entries.

Python 3.9+, standard library only, no dependencies.

The edit is a splice, not a re-serialisation. The root `model_provider`
statement and the `[model_providers.openai-retry]` table are located by
character span and replaced in place, so comments, key order, line endings and
an optional UTF-8 BOM all survive untouched. `install` followed by `remove`
gives back the file byte for byte.

Three actions:

    check    do everything except write; exits non-zero if install would refuse
    install  add the provider and select it
    remove   put back whatever was there before install

Why the care: config.toml is the user's file, not ours. Anything that rewrites
it through a TOML library reformats the whole document and loses every comment
the user wrote. Anything that guesses at line numbers breaks the first time a
different Codex version writes the file. A saved state file next to the config
records what was there, so removal restores it exactly instead of reconstructing
it from the current file.

Design credit: the span-splicing approach and this state-file layout come from
BronyaDev's fork (`c38c18d4`, `c440a855`), which this file is a port of. Changes
in the port: the port number is a parameter instead of a constant, validation
runs on Python 3.9/3.10 where `tomllib` does not exist, and a lock left behind
by a crash is reclaimed instead of blocking every later run.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

try:
    import tomllib
except ImportError:  # Python 3.9/3.10. tomli, if installed, is the same parser.
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

PROVIDER = "openai-retry"
DEFAULT_PORT = 8787
ROOT = 'model_provider = "%s"' % PROVIDER
LOCK_SUFFIX = ".overload-fix.lock"
STATE_SUFFIX = ".overload-fix.json"

# How long a lock file may sit there before we assume the run that made it is
# gone. The critical section is two small file writes, so anything measured in
# minutes is certainly a crash, not a slow competitor.
LOCK_STALE_SECONDS = 300

KEY = re.compile(r'''\s*("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)\s*''')
HEADER = re.compile(r'^\[(\[?)(.*?)\]\]?\s*(?:#.*)?$', re.S)
ASSIGN = re.compile(r'''^((?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_. -])+?)\s*=''')
BASE_URL = re.compile(r'(?m)^(base_url\s*=\s*"http://127\.0\.0\.1:)(\d+)(")[ \t]*$')
SELECTED = re.compile(
    r'''\s*(?:model_provider|"model_provider"|'model_provider')\s*=\s*["']%s["']\s*(?:#.*)?'''
    % re.escape(PROVIDER))


def block_for(port=DEFAULT_PORT):
    """The provider table, for the port the relay actually listens on."""
    return (
        '[model_providers.%s]\n'
        'name = "OpenAI (retry)"\n'
        'base_url = "http://127.0.0.1:%d"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
        'stream_max_retries = 20\n'
        'request_max_retries = 10\n'
    ) % (PROVIDER, port)


BLOCK = block_for()


# --------------------------------------------------------------------- parsing
def key_path(value):
    """Decode a table or key path, including quoted keys; reject ambiguity."""
    parts = []
    while value:
        match = KEY.match(value)
        if not match:
            raise ValueError("Unsupported TOML key syntax; config was not changed")
        token = match[1]
        parts.append(json.loads(token) if token.startswith('"') else token.strip("'"))
        value = value[match.end():]
        if not value:
            break
        if not value.startswith('.'):
            raise ValueError("Unsupported TOML key syntax; config was not changed")
        value = value[1:]
    return parts


def statements(text):
    """Split into statements without mistaking strings or arrays for tables.

    A statement is one logical TOML line. Quoted strings, `#` comments and
    bracket nesting are tracked so that a `[model_providers.openai-retry]`
    inside a multi-line string or an array element is never treated as a real
    table header - which is exactly how a naive line-based editor corrupts a
    file.
    """
    start = 0
    quote = None
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote.startswith('"') and char == '\\':
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
                continue
        elif char == '#':
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
            continue
        elif char in ('"', "'"):
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            continue
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        if char == '\n' and not quote and depth == 0:
            yield start, index + 1, text[start:index + 1]
            start = index + 1
        index += 1
    if quote or depth:
        raise ValueError("Unterminated TOML string or collection; config was not changed")
    if start < len(text):
        yield start, len(text), text[start:]


def locations(text):
    """Return (model_provider span, provider table span, first table header)."""
    root = None
    provider = None
    first_header = None
    headers = []
    section = []
    for start, end, raw in statements(text):
        value = raw.strip()
        if not value or value.startswith('#'):
            continue
        if value.startswith('['):
            header = HEADER.match(value)
            if not header:
                raise ValueError("Unsupported TOML table header")
            if first_header is None:
                first_header = start
            section = key_path(header[2].strip())
            headers.append((start, section))
            if section[:2] == ['model_providers', PROVIDER]:
                if header[1] or len(section) != 2 or provider is not None:
                    raise ValueError("Nested or duplicate relay tables require manual editing")
                provider = (start, len(text))
        else:
            # A quoted key containing '=' is not our concern; the keys we manage
            # are plain and decoded below.
            match = ASSIGN.match(value)
            if not match:
                raise ValueError("Unsupported TOML assignment; config was not changed")
            path = section + key_path(match[1].strip())
            if path == ['model_provider']:
                if root is not None:
                    raise ValueError("Duplicate model_provider")
                root = (start, end)
            if path[:2] == ['model_providers', PROVIDER] and section != path[:2]:
                raise ValueError("Inline/dotted relay settings require manual editing")
    if provider:
        # The table runs to the next header, wherever that is.
        provider = (provider[0], next((start for start, _ in headers if start > provider[0]), len(text)))
    return root, provider, first_header


def piece(text, span):
    return text[span[0]:span[1]] if span else None


def same_block(actual, expected, any_port=False):
    """Compare two provider tables, optionally ignoring the port in base_url.

    any_port is for the untracked path only: a user who installed with
    `-Port 8899` pasted the snippet the installer printed, so port 8899 is as
    recognisably ours as 8787 is. Every other field still has to match exactly,
    which is what stops removal from deleting a provider the user hand-wrote.
    """
    if actual is None or expected is None:
        return actual is None and expected is None
    left, right = actual.rstrip(), expected.rstrip()
    if any_port:
        left = BASE_URL.sub(lambda m: m.group(1) + 'PORT' + m.group(3), left)
        right = BASE_URL.sub(lambda m: m.group(1) + 'PORT' + m.group(3), right)
    return left == right


def block_port(value):
    match = BASE_URL.search(value or '')
    return match.group(2) if match else None


def relay_selected(root):
    return bool(root and SELECTED.fullmatch(root.strip()))


def replace(text, root, block):
    """Splice the managed spans; None means 'remove this entry'."""
    root_span, block_span, first_header = locations(text)
    edits = []
    if root_span:
        edits.append((root_span[0], root_span[1], root or ''))
    if block_span:
        edits.append((block_span[0], block_span[1], block or ''))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if root and not root_span:
        # Root-level keys must come before the first table header, so insert
        # there rather than at the top: a file that starts with a comment block
        # keeps its comment attached to what it describes.
        cut = first_header if first_header is not None else len(text)
        head, tail = text[:cut], text[cut:]
        if head and not head.endswith('\n'):
            head += '\n'
        if head and not head.endswith('\n\n'):
            head += '\n'
        text = head + root + ('\n' if tail else '') + tail
    if block and not block_span:
        text += ('\n' if text and not text.endswith('\n\n') else '') + block
    return text


# ----------------------------------------------------------------------- files
def read(path):
    return path.read_bytes() if path.exists() else b''


def decode(data):
    return data.decode('utf-8-sig')


def encode(text, original):
    return (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate(text):
    """Reject output that would not parse. Returns a word for the message.

    tomllib is Python 3.11+. On 3.9/3.10 there is no full parser in the standard
    library, so fall back to re-scanning the result with the same tokenizer that
    drove the edit: it still catches an unterminated string or bracket, and it
    proves the two managed entries are well formed and appear exactly once,
    which is the whole surface this program touches. It is weaker than a parse
    of the user's unrelated lines, and it says so, rather than skipping
    validation silently.
    """
    if tomllib is not None:
        tomllib.loads(text)
        return "parsed"
    locations(text)
    return "structural"


def load_state(state_path, path):
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    if state and state.get('config_path') != str(path):
        raise ValueError("Backup %s belongs to another config path" % state_path)
    return state


# ------------------------------------------------------------------------ lock
def pid_alive(pid):
    """Best effort, and deliberately never sends a signal.

    os.kill(pid, 0) is not usable here: on Windows any signal other than
    CTRL_C_EVENT terminates the target process, so a liveness probe would kill
    the other installer instead of observing it.
    """
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def lock_is_stale(path, stale_after=LOCK_STALE_SECONDS):
    try:
        info = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        info = {}
    pid = info.get('pid')
    if isinstance(pid, int) and pid > 0 and not pid_alive(pid):
        return True
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age > stale_after


@contextlib.contextmanager
def config_lock(path, stale_after=LOCK_STALE_SECONDS):
    """Exclusive lock held across the read-modify-write.

    open('x') is atomic on every platform we ship to, but it has no owner: a
    crash between create and unlink leaves a lock nobody will ever release, and
    every later install fails with a bare FileExistsError. So a lock whose owner
    is gone, or that is older than stale_after, is reclaimed instead of
    reported. A live competing run still wins, because its pid answers.
    """
    try:
        handle = path.open('x')
    except FileExistsError:
        if not lock_is_stale(path, stale_after):
            raise FileExistsError(
                "Another run is editing this config (%s). If no installer is running, "
                "delete that file and retry." % path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        handle = path.open('x')
    try:
        handle.write(json.dumps({'pid': os.getpid(), 'started': int(time.time())}))
        handle.close()
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------- apply
def apply(action, path, port=DEFAULT_PORT):
    path = path.resolve()
    state_path = path.with_name(path.name + STATE_SUFFIX)
    if action == 'remove' and not path.exists() and not state_path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + LOCK_SUFFIX)
    with config_lock(lock):
        existed = path.exists()
        current = read(path)
        text = decode(current)
        mode = validate(text)
        root_span, block_span, _ = locations(text)
        state = load_state(state_path, path)
        newline = '\r\n' if '\r\n' in text else '\n'
        expected_root = ROOT + newline
        expected_block = block_for(port).replace('\n', newline)
        adopted_manual = relay_selected(piece(text, root_span))
        refreshed = False
        restored = None

        if action in ('check', 'install'):
            if state:
                actual_root, actual_block = piece(text, root_span), piece(text, block_span)
                if (actual_root != state['installed_root']
                        or not same_block(actual_block, state['installed_block'])):
                    raise ValueError(
                        "Relay entries changed since installation; remove or reconcile them first "
                        "(the pre-install backup is %s)" % state.get('backup'))
                if actual_root == expected_root and same_block(actual_block, expected_block):
                    print("Relay configuration is already installed.")
                    return
                # Re-run against a different port: the file still holds exactly
                # what we wrote, only the requested port moved. Rewrite the two
                # entries we own; the originals recorded on first install stay
                # as captured, so remove() still restores them.
                updated = replace(text, expected_root, expected_block)
                refreshed = True
            else:
                actual = piece(text, block_span)
                if adopted_manual and block_span and not same_block(actual, expected_block, any_port=True):
                    raise ValueError(
                        "Active manual relay settings differ from defaults; review them before installation")
                if adopted_manual and block_span and not same_block(actual, expected_block):
                    raise ValueError(
                        "The relay table already points at 127.0.0.1:%s; re-run with --port %s, "
                        "or remove that table first" % (block_port(actual), port))
                updated = replace(text, expected_root, expected_block)
        else:
            if state:
                # Preserve a later provider selection, but never overwrite an
                # edited relay table.
                actual_block = piece(text, block_span)
                if not any(same_block(actual_block, candidate)
                           for candidate in (state['installed_block'], state['original_block'])):
                    raise ValueError(
                        "Relay table changed since installation; review it before removal "
                        "(the pre-install backup is %s)" % state.get('backup'))
                root = piece(text, root_span)
                updated = replace(text, state['original_root'] if root == state['installed_root'] else root,
                                  state['original_block'])
                if not state.get('adopted_manual') and hashlib.sha256(current).hexdigest() == state['installed_hash']:
                    original = Path(state['backup']).read_bytes()
                    if hashlib.sha256(original).hexdigest() != state['original_hash']:
                        raise ValueError("Backup checksum mismatch")
                    updated = decode(original)
                    # Restore the exact bytes, BOM included, rather than
                    # re-encoding the decoded text.
                    restored = original
            else:
                # Also undo the snippet the README and the older installers ask
                # people to paste by hand, whatever port they were told to use.
                root = piece(text, root_span)
                remove_root = relay_selected(root)
                if block_span and not same_block(piece(text, block_span), expected_block, any_port=True):
                    if remove_root:
                        raise ValueError("Untracked relay table differs from defaults; review it before removal")
                    return  # Already restored, or belongs to an unrelated setup.
                if root and PROVIDER in root and not remove_root:
                    raise ValueError("Unrecognized provider selection; review it before removal")
                updated = replace(text, None if remove_root else root, None)
                if updated == text:
                    print("No relay configuration found in %s." % path)
                    return

        mode = validate(updated)
        if action == 'check':
            print("Configuration preflight passed: %s" % path)
            return

        output = restored if restored is not None else encode(updated, current)
        if action == 'install':
            if state and refreshed:
                state = dict(state, installed_root=expected_root, installed_block=expected_block,
                             installed_hash=hashlib.sha256(output).hexdigest())
                atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))
            elif not state:
                backup = path.with_name(path.name + '.overload-fix.' + uuid.uuid4().hex + '.bak')
                atomic_write(backup, current)
                state = dict(config_path=str(path), backup=str(backup), adopted_manual=adopted_manual,
                             created=not existed,
                             original_root=None if adopted_manual else piece(text, root_span),
                             original_block=None if adopted_manual else piece(text, block_span),
                             installed_root=expected_root, installed_block=expected_block,
                             original_hash=hashlib.sha256(current).hexdigest(),
                             installed_hash=hashlib.sha256(output).hexdigest())
                atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))
        if current != output:
            if read(path) != current:
                raise ValueError("Config changed during this operation; nothing was overwritten")
            if action == 'remove' and state and state.get('created') and not output.strip():
                # There was no config.toml before install, so undoing means
                # there is none after it either - not an empty file the user
                # never had.
                path.unlink()
            else:
                if action == 'remove':
                    backup = path.with_name(path.name + '.before-remove.' + uuid.uuid4().hex + '.bak')
                    atomic_write(backup, current)
                atomic_write(path, output)
        if action == 'remove' and state_path.exists():
            state_path.unlink()

        if mode == "structural":
            note = " (structural check only: tomllib needs Python 3.11+)"
        else:
            note = ""
        if action == 'install':
            print("Configuration installed: %s%s" % (path, note))
            print("Pre-install backup: %s" % state['backup'])
        else:
            print("Configuration removed: %s%s" % (path, note))


def default_path():
    base = os.environ.get('CODEX_HOME')
    return (Path(base) if base else Path.home() / '.codex') / 'config.toml'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=('check', 'install', 'remove'))
    parser.add_argument('--config', type=Path, help="config.toml to edit (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help="port the relay listens on (default %d)" % DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        apply(args.action, args.config or default_path(), args.port)
    except (OSError, ValueError, KeyError) as error:
        print('Configuration failed: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())