"""Offline migration identity checks; never open a database or apply DDL."""
import hashlib
import unittest
from unittest.mock import patch

from onboarding.errors import ErrorCode, ServiceError


class MigrationCatalogTests(unittest.TestCase):
    def setUp(self):
        from onboarding import migration_catalog
        self.catalog = migration_catalog

    def refused(self, callback, code=ErrorCode.VERSION_CONFLICT):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_001_identity_is_frozen_and_source_verified(self):
        expected = '3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782'
        self.assertEqual(self.catalog.expected_schema_versions(), ((1, expected),))
        self.assertEqual(hashlib.sha256(self.catalog.load_migration(1).encode()).hexdigest(), expected)

    def test_source_drift_is_rejected_without_exposing_content(self):
        with patch('pathlib.Path.read_bytes', return_value=b'fixture-secret-canary'):
            with self.assertRaises(ServiceError) as caught:
                self.catalog.load_migration(1)
        self.assertEqual(str(caught.exception), 'VERSION_CONFLICT')

    def test_missing_file_is_dependency_failure(self):
        with patch('pathlib.Path.read_bytes', side_effect=OSError('private-path-canary')):
            self.refused(lambda: self.catalog.load_migration(1), ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_valid_installed_prefix_returns_version(self):
        rows = list(self.catalog.expected_schema_versions())
        self.assertEqual(self.catalog.validate_applied(rows), 1)
        self.assertEqual(self.catalog.validate_applied(tuple(rows), minimum=1), 1)

    def test_invalid_installed_history_is_not_silently_sorted_or_coerced(self):
        row = self.catalog.expected_schema_versions()[0]
        for rows in ([], [row, row], [(2, row[1])], [(True, row[1])],
                     [('1', row[1])], [(1, '0'*64)], [list(row)],
                     [(1, row[1], 'extra')], None, 'private-canary'):
            with self.subTest(shape=type(rows).__name__):
                self.refused(lambda: self.catalog.validate_applied(rows))

    def test_unknown_targets_and_bool_are_not_future_migrations(self):
        for target in (0, 3, -1, True, 1.0, '1', None, []):
            with self.subTest(type=type(target).__name__):
                self.refused(lambda: self.catalog.expected_schema_versions(target), ErrorCode.INVALID_INPUT)
                self.refused(lambda: self.catalog.load_migration(target), ErrorCode.INVALID_INPUT)
                self.refused(lambda: self.catalog.validate_applied([], minimum=target), ErrorCode.INVALID_INPUT)

    def test_reviewed_002_requires_complete_known_prefix(self):
        rows = self.catalog.expected_schema_versions(2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(hashlib.sha256(self.catalog.load_migration(2).encode()).hexdigest(), rows[1][1])
        self.assertEqual(self.catalog.validate_applied(rows), 2)
        self.assertEqual(self.catalog.validate_applied(rows, minimum=2), 2)
        for invalid in (rows[1:], rows[::-1], (rows[0], rows[0], rows[1])):
            self.refused(lambda: self.catalog.validate_applied(invalid))
        self.refused(lambda: self.catalog.validate_applied(rows[:1], minimum=2))

    def test_catalog_has_no_database_side_effects(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            self.assertEqual(self.catalog.validate_applied(self.catalog.expected_schema_versions()), 1)
            self.assertTrue(self.catalog.load_migration(1).startswith('-- P1b'))


if __name__ == '__main__':
    unittest.main()
