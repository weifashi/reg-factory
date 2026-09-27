"""Configuration boundary tests; no database connection is required."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from dataclasses import FrozenInstanceError
from unittest.mock import patch
import unittest


class SettingsContractTests(unittest.TestCase):
    def test_settings_boundary_exists(self):
        self.assertIsNotNone(
            importlib.util.find_spec("onboarding.settings"),
            "P1b requires a validated settings boundary",
        )


class SettingsTests(unittest.TestCase):
    def setUp(self):
        from onboarding import settings
        from onboarding.errors import ErrorCode, ServiceError
        self.api = settings
        self.assertTrue(callable(getattr(settings, "load_settings", None)),
                        "load_settings boundary is not implemented")
        self.ServiceError = ServiceError
        self.invalid = ErrorCode.INVALID_INPUT
        # This fixture owns only this temporary child, never shared credentials.
        self.temp = tempfile.TemporaryDirectory(
            prefix="settings-test-", dir="/workspace/reg-factory/.local/onboarding-p1b"
        )
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.data = dict(
            host="/workspace/reg-factory/.local/onboarding-p1b/socket",
            port=55433, dbname="rf_onboarding_test", user="rf_onboarding_app",
            password="fixture-password-do-not-print", schema="rf_onboarding",
            instance_marker="rf-onboarding-p1b-v1:fixture",
        )
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self.path.chmod(0o600)

    def rejected(self, path=None, **kwargs):
        with self.assertRaises(self.ServiceError) as caught:
            self.api.load_settings(path or self.path, **kwargs)
        self.assertEqual(caught.exception.code, self.invalid)
        self.assertEqual(str(caught.exception), "INVALID_INPUT")

    def test_frozen_settings_hide_password(self):
        config = self.api.load_settings(self.path)
        self.assertEqual(config.host, self.data["host"])
        self.assertEqual(config.port, 55433)
        self.assertNotIn(self.data["password"], repr(config))
        with self.assertRaises(FrozenInstanceError):
            config.schema = "public"

    def test_explicit_migrator_role_and_unique_test_schema(self):
        self.data.update(user="rf_onboarding_migrator", schema="rf_p1b_test_" + "a" * 32)
        self.write()
        self.assertEqual(self.api.load_settings(self.path, role="migrator").user,
                         "rf_onboarding_migrator")
        self.rejected()
        self.rejected(role="admin")

    def test_forbidden_targets_and_extra_keys_fail_closed(self):
        bad_values = {
            "host": ["127.0.0.1", "localhost", "/workspace/gcloud/.local/socket",
                     self.data["host"] + "/.."],
            "port": [5432, True, "55433"],
            "dbname": ["postgres", "gcloud_demo", ""],
            "user": ["postgres", "rf_onboarding_migrator"],
            "schema": ["public", "pg_catalog", "rf_p1b_test_abc", 'x";DROP SCHEMA public'],
            "instance_marker": ["gcloud-demo", "rf-onboarding-p1b-v1:", ""],
            "password": ["", None, "password\x00oops"],
            "unknown": ["not-allowed"],
        }
        original = self.data.copy()
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.data = dict(original, **{field: value})
                    self.write()
                    self.rejected()
        self.data = original

    def test_missing_duplicate_or_invalid_json_is_rejected(self):
        del self.data["host"]
        self.write()
        self.rejected()
        for raw in ('{"host":"one","host":"two"}', '[1]', '{oops', '{"port":NaN}'):
            self.path.write_text(raw)
            self.rejected()

    def test_file_mode_owner_and_symlink_are_rejected(self):
        self.path.chmod(0o640)
        self.rejected()
        self.path.chmod(0o600)
        with patch.object(self.api.os, "getuid", return_value=os.getuid() + 1):
            self.rejected()
        linked = self.path.with_name("link.json")
        linked.symlink_to(self.path)
        self.rejected(linked)

    def test_parent_symlink_and_loose_directory_are_rejected(self):
        linked = self.path.parent / "linkdir"
        linked.symlink_to(self.path.parent, target_is_directory=True)
        self.rejected(linked / self.path.name)
        self.path.parent.chmod(0o750)
        try:
            self.rejected()
        finally:
            self.path.parent.chmod(0o700)

    def test_outside_base_and_nonregular_file_are_rejected(self):
        self.rejected(Path("/tmp/not-onboarding-config.json"))
        self.rejected(self.path.parent)


if __name__ == "__main__":
    unittest.main()
