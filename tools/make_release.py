#!/usr/bin/env python3
"""Build the release zip from a git ref, so the download is verifiable.

Why this exists. v1.1.0 was assembled by hand and shipped two defects that
nothing caught, because nothing ever compared the archive with the tree it
came from:

- it was built before scripts/install-linux.sh existed, so the README told
  Linux users to run a file the zip did not contain;
- whatever assembled it treated assets/*.png as text and rewrote CRLF as LF,
  which broke the PNG signature itself. Both screenshots at the top of both
  READMEs are dead in that download. (v1.0.0 is fine, so this happened in the
  v1.1.0 build specifically.)

So the rules here are: the file list comes from `git ls-tree`, the bytes come
from `git cat-file`, and every entry is read back out of the finished zip and
compared with its blob before the build reports success. The build fails
loudly rather than producing an archive nobody checked.

    python tools/make_release.py v1.1.0              # build into the cwd
    python tools/make_release.py v1.1.0 --list       # print what would ship

Add --expect for files that release must contain but older tags did not:

    python tools/make_release.py v1.2.0 \\
        --expect scripts/install-linux.sh --expect scripts/uninstall-linux.sh

The archive is named codex-overload-fix-<version>.zip and unpacks to a single
top-level directory, which is what installers and the README assume.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile

# Every entry that must be present, relative to the archive root. If a release
# ships without one of these the build fails -- a missing launcher is exactly
# the failure this list is here to catch. Files a given tag predates belong on
# the command line via --expect.
REQUIRED = [
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "relay/relay.py",
    "scripts/install.ps1",
    "scripts/uninstall.ps1",
    "tools/capture_server.py",
    "tools/analyze_log.py",
]

# Files that arrived after the tags above, so they cannot live in REQUIRED
# without making those tags unbuildable. Every release from the one that
# introduced them onward must list them on the command line:
#
#   --expect scripts/configure_codex.py --expect tests/test_configure_codex.py
#   --expect scripts/install-linux.sh --expect scripts/uninstall-linux.sh
#   --expect scripts/install-mac.sh   --expect scripts/uninstall-mac.sh
#
# install.ps1 and uninstall.ps1 are in REQUIRED because v1.0.0 already had
# them; the four shell scripts and configure_codex.py are not.

# A launcher that is 644 inside the zip still works when invoked as
# `bash script.sh`, but not when run directly -- and on macOS the LaunchAgent
# path goes through the shebang. build() carries the mode into the archive;
# main() refuses to finish if a shell script is not 755 in git.
SHELL_SCRIPTS = "scripts/"

# Timestamps inside the zip are pinned so two builds of the same ref are
# byte-identical; otherwise the sha256 changes on every run and the checksum in
# the release notes means nothing.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args):
    out = subprocess.run(
        ["git", "-C", REPO_ROOT, *args], capture_output=True
    )
    if out.returncode != 0:
        sys.exit(
            f"git {' '.join(args)} failed: "
            f"{out.stderr.decode('utf-8', 'replace').strip()}"
        )
    return out.stdout


def tracked_files(ref):
    """Every file tracked at `ref`, as (path, mode, blob_sha)."""
    raw = git("ls-tree", "-r", ref).decode("utf-8", "replace")
    files = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob":
            continue
        files.append((path, mode, sha))
    return files


def is_executable(mode):
    return mode == "100755"


def unix_entry(prefix_path, mode, is_dir):
    """A ZipInfo carrying a Unix permission mode.

    The mode goes in the high 16 bits of external_attr, which is only
    meaningful to extractors when create_system says the entry came from Unix
    -- so it is set to 3 rather than left at zipfile's default of 0 (FAT).
    Info-ZIP's unzip applies the bits either way, but a tool that checks the
    host first would not, and stamping the mode while saying "FAT" is just a
    lie in the header.
    """
    info = zipfile.ZipInfo(prefix_path, ZIP_EPOCH)
    info.create_system = 3  # Unix
    info.external_attr = ((0o40000 | mode) if is_dir else (0o100000 | mode)) << 16
    if is_dir:
        info.external_attr |= 0x10  # MS-DOS directory bit, for tools that read it
    return info


def build(ref, version, out_path):
    files = tracked_files(ref)
    if not files:
        sys.exit(f"no files at {ref}")

    prefix = f"codex-overload-fix-{version}/"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        dirs = set()
        for path, _, _ in files:
            parts = path.split("/")[:-1]
            for i in range(len(parts)):
                dirs.add("/".join(parts[: i + 1]) + "/")
        for d in sorted(dirs):
            zf.writestr(unix_entry(prefix + d, 0o755, is_dir=True), b"")
        for path, mode, _ in files:
            blob = git("cat-file", "blob", f"{ref}:{path}")
            info = unix_entry(
                prefix + path,
                0o755 if is_executable(mode) else 0o644,
                is_dir=False,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, blob)

    with open(out_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return files, digest


def verify(ref, version, out_path, files):
    """Read the zip back and prove every entry is the git blob, byte for byte.

    This is the check that v1.1.0 needed and did not have. That archive was
    built by a tool that treated assets/*.png as text and rewrote CRLF as LF,
    which silently broke both screenshots in the README: the PNG signature
    itself was mangled, so no image viewer would open them. Nothing in the
    build noticed, because nothing compared the output with the input. A text
    file survives that kind of mangling -- a PNG does not -- so the only
    reliable test is equality with the blob.
    """
    prefix = f"codex-overload-fix-{version}/"
    problems = []
    with zipfile.ZipFile(out_path) as zf:
        got = {i.filename: i for i in zf.infolist()}
        for path, mode, _ in files:
            name = prefix + path
            info = got.get(name)
            if info is None:
                problems.append(f"missing from archive: {path}")
                continue
            want_mode = 0o755 if is_executable(mode) else 0o644
            got_mode = info.external_attr >> 16
            if info.create_system != 3:
                problems.append(f"{path}: create_system is {info.create_system}, not Unix")
            if got_mode != (0o100000 | want_mode):
                problems.append(f"{path}: mode {oct(got_mode)}, want {oct(0o100000 | want_mode)}")
            if zf.read(name) != git("cat-file", "blob", f"{ref}:{path}"):
                problems.append(f"{path}: bytes differ from the blob at {ref}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ref", help="git ref to package, e.g. v1.1.0")
    ap.add_argument("--version", help="version for the filename (default: ref without a leading v)")
    ap.add_argument("--out", help="output zip path (default: ./codex-overload-fix-<version>.zip)")
    ap.add_argument("--expect", action="append", default=[], metavar="PATH",
                    help="extra entry this release must contain (repeatable)")
    ap.add_argument("--list", action="store_true", help="print the archive listing and exit")
    args = ap.parse_args()

    ref = args.ref
    version = args.version or (ref[1:] if ref.startswith("v") else ref)
    out = args.out or f"codex-overload-fix-{version}.zip"

    if args.list:
        for path in sorted(p for p, _, _ in tracked_files(ref)):
            print(path)
        return

    files, digest = build(ref, version, out)
    listing = sorted(p for p, _, _ in files)

    print(f"built  {out}")
    print(f"sha256 {digest}")
    print(f"files  {len(listing)}")

    missing = [r for r in REQUIRED + args.expect if r not in listing]
    if missing:
        sys.exit("archive is missing required entries: " + ", ".join(missing))

    modes = {p: m for p, m, _ in files}
    bad = [
        p for p in listing
        if p.startswith(SHELL_SCRIPTS) and p.endswith(".sh")
        and not is_executable(modes[p])
    ]
    if bad:
        sys.exit(
            "shell scripts not executable at that ref "
            "(git update-index --chmod=+x): " + ", ".join(bad)
        )

    problems = verify(ref, version, out, files)
    if problems:
        sys.exit("archive does not match the ref:\n  " + "\n  ".join(problems))
    print("checks ok")


if __name__ == "__main__":
    main()
