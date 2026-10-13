"""Focused, pure offline unit tests for worker/settings.py.

Verifies:
- auto_submit_enabled strictly fails closed when configuration is missing,
  malformed, empty, or non-boolean.
- Only exact boolean True enables submission.
- Truthy equivalents ("true", 1, [True], {"nested": True}) are rejected.
- Non-dict JSON roots (arrays, scalars) fail closed.
- Operates strictly on isolated temporary files; never modifies config.json.
- Zero browser, network, or server execution.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.application import apply_to_job
from worker.runner import process_job
from worker.settings import auto_submit_enabled, load_config, resolve_auto_submit


class SettingsSafetyOfflineTests(unittest.TestCase):
    """Pure offline unit tests for fail-closed configuration parsing."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="test_settings_safety_")
        self.temp_dir_path = Path(self._temp_dir.name)

    def tearDown(self):
        self._temp_dir.cleanup()

    def _write_temp_config(self, content: str) -> Path:
        config_path = self.temp_dir_path / "config.json"
        config_path.write_text(content, encoding="utf-8")
        return config_path

    # 1. Explicit boolean false
    def test_explicit_boolean_false(self):
        path = self._write_temp_config('{"auto_submit": false}')
        self.assertFalse(auto_submit_enabled(path))

    # 2. Explicit boolean true (isolated temporary configuration only)
    def test_isolated_explicit_boolean_true(self):
        path = self._write_temp_config('{"auto_submit": true}')
        self.assertTrue(auto_submit_enabled(path))

    # 3. Missing configuration file
    def test_missing_configuration_file(self):
        non_existent_path = self.temp_dir_path / "non_existent_config.json"
        self.assertFalse(auto_submit_enabled(non_existent_path))
        self.assertEqual(load_config(non_existent_path), {})

    # 4. Empty configuration file (zero bytes)
    def test_empty_configuration_file(self):
        path = self._write_temp_config("")
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 5. Malformed JSON syntax
    def test_malformed_json_syntax(self):
        path = self._write_temp_config('{"auto_submit": true, INVALID_JSON}')
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 6. Missing auto_submit key
    def test_missing_auto_submit_key(self):
        path = self._write_temp_config('{"other_key": "some_value"}')
        self.assertFalse(auto_submit_enabled(path))
        self.assertIsNone(load_config(path).get("auto_submit"))

    # 7. Non-boolean string "true" is rejected (truthy string bypass prevention)
    def test_string_true_rejected(self):
        path = self._write_temp_config('{"auto_submit": "true"}')
        self.assertFalse(auto_submit_enabled(path))

    # 8. Non-boolean string "True" (capitalized) is rejected
    def test_string_capitalized_true_rejected(self):
        path = self._write_temp_config('{"auto_submit": "True"}')
        self.assertFalse(auto_submit_enabled(path))

    # 9. Non-boolean integer 1 is rejected (truthy int bypass prevention)
    def test_integer_one_rejected(self):
        path = self._write_temp_config('{"auto_submit": 1}')
        self.assertFalse(auto_submit_enabled(path))

    # 10. Null / None value is rejected
    def test_null_value_rejected(self):
        path = self._write_temp_config('{"auto_submit": null}')
        self.assertFalse(auto_submit_enabled(path))

    # 11. Nested object is rejected
    def test_nested_object_rejected(self):
        path = self._write_temp_config('{"auto_submit": {"nested": true}}')
        self.assertFalse(auto_submit_enabled(path))

    # 12. List value is rejected
    def test_list_value_rejected(self):
        path = self._write_temp_config('{"auto_submit": [true]}')
        self.assertFalse(auto_submit_enabled(path))

    # 13. Root JSON is array instead of dictionary
    def test_root_json_is_array(self):
        path = self._write_temp_config('[{"auto_submit": true}]')
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 14. Root JSON is scalar string
    def test_root_json_is_scalar_string(self):
        path = self._write_temp_config('"auto_submit"')
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 15. Root JSON is scalar boolean
    def test_root_json_is_scalar_boolean(self):
        path = self._write_temp_config("true")
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 16. Unicode whitespace / whitespace-only file
    def test_whitespace_only_file(self):
        path = self._write_temp_config("   \n\t  ")
        self.assertFalse(auto_submit_enabled(path))
        self.assertEqual(load_config(path), {})

    # 17. Invalid UTF-8 bytes fail closed without crashing
    def test_invalid_utf8_encoding_fails_closed(self):
        config_path = self.temp_dir_path / "config.json"
        config_path.write_bytes(b"\x80\xff\xfe\x00\xaa\xbb")
        self.assertFalse(auto_submit_enabled(config_path))
        self.assertEqual(load_config(config_path), {})

    # 18. Directory path passed as configuration path fails closed
    def test_directory_path_fails_closed(self):
        self.assertFalse(auto_submit_enabled(self.temp_dir_path))
        self.assertEqual(load_config(self.temp_dir_path), {})

    # 19. Extended truthy and falsy strings in config are rejected
    def test_truthy_and_falsy_strings_in_config_rejected(self):
        for val in ("true", "false", "yes", "on", "1", "0", "True", "FALSE", "no", "off"):
            with self.subTest(value=val):
                path = self._write_temp_config(f'{{"auto_submit": "{val}"}}')
                self.assertFalse(auto_submit_enabled(path))

    # 20. Numeric values in config are rejected
    def test_numeric_values_in_config_rejected(self):
        for val in (1, 1.0, -1, 2, 0, 0.0, 99):
            with self.subTest(value=val):
                path = self._write_temp_config(f'{{"auto_submit": {val}}}')
                self.assertFalse(auto_submit_enabled(path))

    # 21. resolve_auto_submit explicit booleans
    def test_resolve_auto_submit_explicit_booleans(self):
        self.assertTrue(resolve_auto_submit(True))
        self.assertFalse(resolve_auto_submit(False))

    # 22. resolve_auto_submit explicit strings and numbers rejected
    def test_resolve_auto_submit_explicit_strings_and_numbers_rejected(self):
        for val in ("true", "false", "yes", "on", "1", "0", 1, 1.0, -1, 2, 0):
            with self.subTest(value=val):
                self.assertFalse(resolve_auto_submit(val))

    # 23. resolve_auto_submit omitted argument follows configuration
    def test_resolve_auto_submit_omitted_argument_follows_config(self):
        true_path = self._write_temp_config('{"auto_submit": true}')
        self.assertTrue(resolve_auto_submit(None, path=true_path))
        false_path = self._write_temp_config('{"auto_submit": false}')
        self.assertFalse(resolve_auto_submit(None, path=false_path))
        missing_path = self.temp_dir_path / "non_existent.json"
        self.assertFalse(resolve_auto_submit(None, path=missing_path))

    # 24. Caller path: apply_to_job explicit auto_submit handling
    @mock.patch("worker.application.prepare_application")
    @mock.patch("worker.application.save_application")
    def test_application_caller_explicit_auto_submit_handling(self, mock_save, mock_prepare):
        mock_prepare.return_value = {"status": "Processing"}
        job = {"company": "TestCo", "title": "Dev", "url": "http://127.0.0.1:8000"}
        applicant = {}
        fake_page = mock.Mock()

        # Explicit True
        apply_to_job(fake_page, job, applicant, auto_submit=True)
        self.assertIs(mock_prepare.call_args[1]["auto_submit"], True)

        # Explicit False
        apply_to_job(fake_page, job, applicant, auto_submit=False)
        self.assertIs(mock_prepare.call_args[1]["auto_submit"], False)

        # Unsafe string / numeric inputs must not coerce to True
        for unsafe in ("true", "false", "yes", "on", "1", 1, 1.0, -1):
            with self.subTest(unsafe=unsafe):
                apply_to_job(fake_page, job, applicant, auto_submit=unsafe)
                self.assertIs(mock_prepare.call_args[1]["auto_submit"], False)

        # Omitted argument follows config (disabled by default)
        apply_to_job(fake_page, job, applicant, auto_submit=None)
        self.assertIs(mock_prepare.call_args[1]["auto_submit"], False)

    # 25. Caller path: process_job explicit auto_submit handling
    @mock.patch("worker.runner.apply_to_job")
    @mock.patch("worker.runner.save_application")
    @mock.patch("worker.runner.mark_job_status")
    @mock.patch("worker.runner.load_applicant_profile")
    @mock.patch("worker.runner.current_page_is_authorized")
    def test_runner_caller_explicit_auto_submit_handling(
        self, mock_auth, mock_profile, mock_status, mock_save, mock_apply
    ):
        mock_apply.return_value = {"status": "Processing"}
        mock_profile.return_value = {}
        fake_nav = mock.Mock()
        fake_nav.last_rejection = None
        fake_page = mock.Mock()
        fake_page.url = "http://127.0.0.1:8000/job"
        job = {"id": "1", "company": "TestCo", "title": "Dev", "url": "http://127.0.0.1:8000/job"}

        # Explicit True
        process_job(fake_page, job, auto_submit=True, navigation_policy=fake_nav)
        self.assertIs(mock_apply.call_args[1]["auto_submit"], True)

        # Explicit False
        process_job(fake_page, job, auto_submit=False, navigation_policy=fake_nav)
        self.assertIs(mock_apply.call_args[1]["auto_submit"], False)

        # Unsafe string / numeric inputs must not coerce to True
        for unsafe in ("true", "false", "yes", "on", "1", 1, 1.0, -1):
            with self.subTest(unsafe=unsafe):
                process_job(fake_page, job, auto_submit=unsafe, navigation_policy=fake_nav)
                self.assertIs(mock_apply.call_args[1]["auto_submit"], False)

        # Omitted argument follows config (disabled by default)
        process_job(fake_page, job, auto_submit=None, navigation_policy=fake_nav)
        self.assertIs(mock_apply.call_args[1]["auto_submit"], False)


if __name__ == "__main__":
    unittest.main()
