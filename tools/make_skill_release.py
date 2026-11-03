"""Build a self-contained Skill ZIP from Git blobs, verifying every byte."""
import argparse
import hashlib
from pathlib import Path
import zipfile
from make_release import git, tracked_files, unix_entry


def build(ref, output):
    entries = {}
    for path, mode, _ in tracked_files(ref):
        if path.startswith('skills/codex-overload-fix/'):
            target = 'codex-overload-fix/' + path[len('skills/codex-overload-fix/'):]
        elif path.startswith(('relay/', 'scripts/', 'docs/', 'assets/')) or path in ('LICENSE', 'README.md', 'README.zh-CN.md', 'README-eg.md'):
            target = 'codex-overload-fix/runtime/' + path
        else:
            continue
        entries[target] = (path, mode)
    required = ['codex-overload-fix/SKILL.md'] + [
        'codex-overload-fix/runtime/' + p for p in (
            'LICENSE', 'relay/relay.py', 'relay/identity.py', 'scripts/configure_codex.py',
            'scripts/windows-autostart.ps1', 'scripts/install.ps1', 'scripts/uninstall.ps1',
            'scripts/install-mac.sh', 'scripts/uninstall-mac.sh',
            'scripts/install-linux.sh', 'scripts/uninstall-linux.sh')]
    missing = set(required) - set(entries)
    if missing:
        raise ValueError('Missing required files: ' + ', '.join(sorted(missing)))
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for target, (source, mode) in sorted(entries.items()):
            info = unix_entry(target, 0o755 if mode == '100755' else 0o644, False)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, git('cat-file', 'blob', ref + ':' + source))
    with zipfile.ZipFile(output) as archive:
        if set(archive.namelist()) != set(entries):
            raise ValueError('Unexpected archive contents')
        for target, (source, mode) in entries.items():
            if archive.read(target) != git('cat-file', 'blob', ref + ':' + source):
                raise ValueError('Archive differs from Git: ' + target)
            expected = 0o100000 | (0o755 if mode == '100755' else 0o644)
            if archive.getinfo(target).external_attr >> 16 != expected:
                raise ValueError('Incorrect file mode: ' + target)
    digest = hashlib.sha256(Path(output).read_bytes()).hexdigest()
    print('checks ok; SHA-256 ' + digest)
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ref')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    build(args.ref, args.out)
