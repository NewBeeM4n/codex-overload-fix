"""Built-in provider migration must retain OpenAI chats and reversible config."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_config', ROOT / 'scripts/configure_codex.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class NativeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.toml'

    def test_roundtrip_preserves_bytes_and_openai_identity(self):
        for original in (b'', b'model = "keep"',
                         b'\xef\xbb\xbf# hello\r\nmodel_provider = "openai" # retain\r\n[features]\r\napps = true\r\n',
                         b"'openai_base_url' = 'https://example.invalid/v1'\nmodel_provider = 'other'\n"):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                config.apply('check', self.path)
                self.assertEqual(self.path.read_bytes(), original)
                config.apply('install', self.path)
                installed = self.path.read_bytes()
                self.assertIn(b'model_provider = "openai"', installed)
                self.assertIn(b'openai_base_url = "http://127.0.0.1:8787"', installed)
                self.assertNotIn(b'openai-retry', installed)
                config.apply('install', self.path)
                self.assertEqual(self.path.read_bytes(), installed)
                config.apply('remove', self.path)
                self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_migration_restores_pre_legacy_config_on_uninstall(self):
        original = b'\xef\xbb\xbf# retain\r\nmodel_provider = "openai"\r\n[features]\r\napps = true\r\n'
        self.path.write_bytes(original)
        config.apply_legacy('install', self.path)
        legacy = self.path.read_bytes()
        config.apply('check', self.path)
        self.assertEqual(self.path.read_bytes(), legacy)
        config.apply('install', self.path, 9001)
        self.assertNotIn(b'openai-retry', self.path.read_bytes())
        self.assertIn(b'9001', self.path.read_bytes())
        config.apply('remove', self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_migration_preserves_later_edits(self):
        self.path.write_text('model = "before"\n', encoding='utf-8')
        config.apply_legacy('install', self.path)
        self.path.write_text(self.path.read_text().replace('"before"', '"later"'), encoding='utf-8')
        config.apply('install', self.path)
        config.apply('remove', self.path)
        result = self.path.read_text()
        self.assertIn('model = "later"', result)
        self.assertNotIn('openai-retry', result)
        self.assertNotIn('openai_base_url', result)

    def test_changed_managed_url_refused_without_mutation(self):
        config.apply('install', self.path)
        self.path.write_text(self.path.read_text().replace(':8787', ':9999'), encoding='utf-8')
        before = self.path.read_bytes()
        for action in ('check', 'install', 'remove'):
            with self.assertRaises(ValueError):
                config.apply(action, self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_uninstall_preserves_unrelated_changes_and_new_selection(self):
        self.path.write_text('model = "keep"\n', encoding='utf-8')
        config.apply('install', self.path)
        self.path.write_text(self.path.read_text().replace('model_provider = "openai"', 'model_provider = "other"')
                             + '\n[features]\napps = true\n', encoding='utf-8')
        config.apply('remove', self.path)
        result = self.path.read_text()
        self.assertIn('model_provider = "other"', result)
        self.assertIn('[features]', result)
        self.assertNotIn('openai_base_url', result)

    def test_missing_file_roundtrip_and_port_change(self):
        config.apply('install', self.path)
        config.apply('install', self.path, 9001)
        self.assertNotIn('8787', self.path.read_text())
        config.apply('remove', self.path)
        self.assertFalse(self.path.exists())

    def test_structural_fallback_and_multiline_url_decoy(self):
        original = 'notes = """\nopenai_base_url = "fake"\n"""\n[features]\napps = true\n'
        self.path.write_text(original, encoding='utf-8')
        parser = config.tomllib
        try:
            config.tomllib = None
            config.apply('install', self.path)
            self.assertIn('openai_base_url = "fake"', self.path.read_text())
            config.apply('remove', self.path)
            self.assertEqual(original, self.path.read_text())
        finally:
            config.tomllib = parser

    def test_edited_legacy_table_refused_before_migration(self):
        config.apply_legacy('install', self.path)
        self.path.write_text(self.path.read_text().replace('stream_max_retries = 20', 'stream_max_retries = 99'), encoding='utf-8')
        before = self.path.read_bytes()
        for action in ('check', 'install'):
            with self.assertRaises(ValueError):
                config.apply(action, self.path)
            self.assertEqual(before, self.path.read_bytes())

    def test_manual_native_adoption_and_removal_never_leave_dead_route(self):
        for install in (False, True):
            self.path.write_text(config.native_root(9001) + 'model = "keep"\n', encoding='utf-8')
            if install:
                config.apply('install', self.path, 9001)
            config.apply('remove', self.path, 9001)
            self.assertIn('model = "keep"', self.path.read_text())
            self.assertNotIn('openai_base_url', self.path.read_text())

    def test_manual_native_different_port_refused(self):
        self.path.write_text(config.native_root(9001), encoding='utf-8')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            config.apply('install', self.path, 8787)
        self.assertEqual(before, self.path.read_bytes())
