"""Tests for scripts/configure_codex.py.

The point of these tests is the property the README sells: undo is exact. So
every test that installs also removes and compares bytes, and the fixtures are
the shapes that break careless editors - CRLF, a UTF-8 BOM, single-quoted keys,
a `model_provider` line inside a multi-line string, and a file with no trailing
newline.

The Windows tests drive the real install.ps1 / uninstall.ps1 and check the
ordering rule that makes the whole thing safe: the configuration preflight runs
before anything is installed or stopped, so a config this script refuses leaves
the machine exactly as it was. They never touch the real ~/.codex and never
write the registry, because a refused preflight exits before that code runs.
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_codex', ROOT / 'scripts' / 'configure_codex.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'out' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        # A directory with a space in it: the launchers pass this path as an
        # argument, and quoting bugs only show up on paths like this one.
        self.path = Path(self.temp.name) / 'profile with spaces' / 'config.toml'
        self.path.parent.mkdir()

    def write(self, text):
        self.path.write_bytes(text.encode('utf-8'))

    def read(self):
        return self.path.read_text(encoding='utf-8')

    @property
    def lock(self):
        return self.path.with_name(self.path.name + '.overload-fix.lock')

    @property
    def state(self):
        return self.path.with_name(self.path.name + '.overload-fix.json')


class ConfigurationTests(ConfigFixture):
    def test_install_remove_preserves_original_bytes_and_is_idempotent(self):
        originals = [
            b'',
            b'model = "gpt-example"',
            b'model_provider = "other" # keep\r\n[projects."D:/work"]\r\ntrust_level = "trusted"\r\n',
            b'\xef\xbb\xbf# utf8 bom\r\nmodel = "example"\r\n',
            b"'model_provider' = 'other'\n[model_providers.'openai-retry']\nname = 'custom'\n[features]\napps = true\n",
        ]
        for original in originals:
            with self.subTest(original=original):
                self.path.write_bytes(original)
                config.apply('install', self.path)
                installed = self.path.read_bytes()
                self.assertIn(b'http://127.0.0.1:8787', installed)
                config.apply('install', self.path)
                self.assertEqual(installed, self.path.read_bytes(), 'second install changed the file')
                config.apply('remove', self.path)
                self.assertEqual(original, self.path.read_bytes(), 'remove did not restore the original')
                config.apply('remove', self.path)
                self.assertEqual(original, self.path.read_bytes(), 'second remove changed the file')
                self.assertFalse(self.state.exists())

    def test_preserves_later_unrelated_edits_and_provider_selection(self):
        self.write('model_provider = "before"\nmodel = "first"\n')
        config.apply('install', self.path)
        updated = self.read().replace('model = "first"', 'model = "later"')
        updated = updated.replace(config.ROOT, 'model_provider = "user-choice"')
        self.write(updated + '\n[features]\napps = true\n')
        config.apply('remove', self.path)
        result = self.read()
        self.assertIn('model_provider = "user-choice"', result)
        self.assertIn('model = "later"', result)
        self.assertIn('[features]', result)
        self.assertNotIn('[model_providers.openai-retry]', result)

    def test_multiline_strings_arrays_and_quoted_headers_are_not_rewritten(self):
        original = '''notes = """
model_provider = "fake"
[model_providers.openai-retry]
"""
values = [
  "[model_providers.openai-retry]", # array content
]
[projects.'contains # [brackets]']
description = ''' + "'''\nmodel_provider = 'also fake'\n'''\n"
        self.write(original)
        config.apply('install', self.path)
        if config.tomllib:
            parsed = config.tomllib.loads(self.read())
            self.assertEqual(parsed['model_provider'], config.PROVIDER)
            self.assertIn('fake', parsed['notes'])
        config.apply('remove', self.path)
        self.assertEqual(original, self.read())

    def test_edited_relay_table_is_not_silently_deleted_and_lock_is_released(self):
        config.apply('install', self.path)
        self.write(self.read().replace('base_url = "http://127.0.0.1:8787"',
                                       'base_url = "http://127.0.0.1:9999"'))
        before = self.path.read_bytes()
        for action in ('remove', 'check', 'install'):
            with self.assertRaises(ValueError):
                config.apply(action, self.path)
            self.assertEqual(before, self.path.read_bytes())
            self.assertFalse(self.lock.exists(), 'the lock was not released after %s' % action)

    def test_refuses_inline_or_nested_provider_config(self):
        for original in ('model_providers.openai-retry = { name = "mine" }\n',
                         '[model_providers.openai-retry.headers]\nx = "y"\n'):
            self.write(original)
            with self.assertRaises(ValueError):
                config.apply('install', self.path)
            self.assertEqual(original, self.read())
            self.assertFalse(self.lock.exists())

    def test_exact_manual_snippet_can_be_removed_without_saved_state(self):
        self.write(config.ROOT + '\nmodel = "keep"\n\n' + config.BLOCK)
        config.apply('remove', self.path)
        self.assertIn('model = "keep"', self.read())
        self.assertNotIn(config.PROVIDER, self.read())

    def test_manual_snippet_on_another_port_can_be_removed(self):
        # install.ps1 has always taken -Port, so the snippet people paste by hand
        # is not always 8787. Removal has to recognise it anyway.
        original = config.ROOT + '\nmodel = "keep"\n\n' + config.block_for(9001)
        self.write(original)
        config.apply('remove', self.path)
        self.assertIn('model = "keep"', self.read())
        self.assertNotIn(config.PROVIDER, self.read())

    def test_install_adopts_existing_manual_defaults_instead_of_restoring_dead_route(self):
        self.write(config.ROOT + '\nmodel = "keep"\n\n' + config.BLOCK)
        config.apply('install', self.path)
        config.apply('remove', self.path)
        self.assertIn('model = "keep"', self.read())
        self.assertNotIn(config.PROVIDER, self.read())
        config.apply('remove', self.path)

    def test_port_is_parameterised(self):
        self.write('model = "keep"\n')
        config.apply('install', self.path, 9001)
        installed = self.read()
        self.assertIn('http://127.0.0.1:9001', installed)
        self.assertNotIn('8787', installed)
        config.apply('remove', self.path, 9001)
        self.assertEqual('model = "keep"\n', self.read())

    def test_reinstall_on_a_new_port_rewrites_only_our_entries(self):
        original = 'model = "keep"\n\n[features]\napps = true\n'
        self.write(original)
        config.apply('install', self.path, 8787)
        config.apply('install', self.path, 9001)
        updated = self.read()
        self.assertIn('http://127.0.0.1:9001', updated)
        self.assertNotIn('8787', updated)
        self.assertEqual(1, updated.count(config.ROOT))
        config.apply('remove', self.path, 9001)
        self.assertEqual(original, self.read())

    def test_manual_table_on_another_port_is_refused_with_the_port_named(self):
        self.write(config.ROOT + '\n\n' + config.block_for(9001))
        with self.assertRaises(ValueError) as caught:
            config.apply('install', self.path, 8787)
        self.assertIn('9001', str(caught.exception))

    def test_root_key_lands_above_the_first_table_not_above_the_file(self):
        # A root key written after a table header would belong to that table, so
        # it has to be inserted before the first header - but not so high that a
        # leading comment loses the setting it describes.
        self.write('# my settings\nmodel = "gpt-5"\n\n[features]\napps = true\n')
        config.apply('install', self.path)
        installed = self.read()
        self.assertTrue(installed.startswith('# my settings\nmodel = "gpt-5"\n'))
        self.assertLess(installed.index(config.ROOT), installed.index('[features]'))
        if config.tomllib:
            parsed = config.tomllib.loads(installed)
            self.assertEqual(parsed['model_provider'], config.PROVIDER)
            self.assertEqual(parsed['features']['apps'], True)

    def test_exclusive_lock_prevents_concurrent_write(self):
        # A live owner: the lock records this process, which is still running.
        self.lock.write_text('{"pid": %d}' % os.getpid(), encoding='utf-8')
        with self.assertRaises(FileExistsError):
            config.apply('install', self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.lock.exists())

    def test_lock_from_a_dead_process_is_reclaimed(self):
        # A crash between create and unlink must not block every later install.
        self.lock.write_text('{"pid": 4294967290}', encoding='utf-8')
        self.write('model = "keep"\n')
        config.apply('install', self.path)
        self.assertIn(config.ROOT, self.read())
        self.assertFalse(self.lock.exists())

    def test_ancient_unreadable_lock_is_reclaimed(self):
        self.lock.write_text('half-written', encoding='utf-8')
        old = 1_000_000_000  # 2001: long past any plausible run
        os.utime(str(self.lock), (old, old))
        self.write('model = "keep"\n')
        config.apply('install', self.path)
        self.assertIn(config.ROOT, self.read())
        self.assertFalse(self.lock.exists())

    @unittest.skipUnless(os.name != 'nt', 'POSIX permission bits')
    def test_install_preserves_the_file_mode(self):
        # The write goes through mkstemp, which creates 0600; renaming it into
        # place would quietly narrow config.toml to owner-only. The mode has to
        # survive the edit in both directions.
        self.write('model = "keep"\n')
        os.chmod(self.path, 0o640)
        config.apply('install', self.path)
        self.assertEqual(0o640, self.path.stat().st_mode & 0o777)
        config.apply('remove', self.path)
        self.assertEqual(0o640, self.path.stat().st_mode & 0o777)

    def test_cli_check_reports_without_writing(self):
        self.write('model = "keep"\n')
        before = self.path.read_bytes()
        code = config.main(['check', '--config', str(self.path)])
        self.assertEqual(0, code)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.state.exists())

    def test_cli_resolves_codex_home_and_survives_a_missing_file(self):
        old = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = str(self.path.parent)
        try:
            self.assertEqual(0, config.main(['check']))
            self.assertEqual(0, config.main(['remove']))
            # A machine with no config.toml yet: check and remove must both be
            # silent no-ops, not create an empty file and not traceback.
            self.assertFalse(self.path.exists())
        finally:
            if old is None:
                os.environ.pop('CODEX_HOME', None)
            else:
                os.environ['CODEX_HOME'] = old


@unittest.skipUnless(os.name == 'nt', 'Windows installers')
class WindowsInstallerTests(ConfigFixture):
    """The installers refuse before they change anything.

    Both scripts must call configure_codex.py's preflight first:

    - install.ps1 must not create launchers or an autostart entry when the
      config cannot be edited, because the relay would then be running with
      nothing pointed at it;
    - uninstall.ps1 must not stop the relay when the config cannot be restored,
      because that leaves Codex pointed at a dead port.

    A refused preflight exits before either script reaches the registry, so
    these tests are safe to run on a developer machine.
    """

    def run_ps(self, script, *args):
        env = os.environ.copy()
        # The installer finds Python through PATH. Under some runners the
        # interpreter running these tests is not on it, and then the installer
        # correctly reports "no usable Python" and the test measures nothing.
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
        env['CODEX_HOME'] = str(self.path.parent)
        env['LOCALAPPDATA'] = str(Path(self.temp.name) / 'localappdata')
        return subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(ROOT / 'scripts' / script), *args],
            env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', timeout=120)

    def test_install_refuses_a_config_it_cannot_edit_and_installs_nothing(self):
        self.write('[model_providers.openai-retry.headers]\nx = "keep"\n')
        original = self.path.read_bytes()
        result = self.run_ps('install.ps1', '-NoAutostart', '-Port', '18787')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn('Configuration failed', result.stdout)
        self.assertEqual(original, self.path.read_bytes())
        state_dir = Path(self.temp.name) / 'localappdata' / 'codex-overload-fix'
        self.assertFalse(state_dir.exists(), 'the installer wrote files after refusing the config')

    def test_uninstall_refuses_a_config_it_cannot_restore_and_keeps_the_relay(self):
        config.apply('install', self.path)
        self.write(self.read().replace('base_url = "http://127.0.0.1:8787"',
                                       'base_url = "http://127.0.0.1:9999"'))
        before = self.path.read_bytes()
        result = self.run_ps('uninstall.ps1')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn('Configuration failed', result.stdout)
        self.assertEqual(before, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()