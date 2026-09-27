"""The DB test fixture must use the same target-isolation gate as product code."""
import unittest
from unittest.mock import patch


class FixtureSafetyTests(unittest.TestCase):
    def test_invalid_settings_cannot_be_bypassed_by_fixture(self):
        from onboarding.errors import ServiceError, ErrorCode
        from onboarding_support import SchemaFixture
        fixture = None
        refused = False
        try:
            with patch('onboarding.settings.load_settings',
                       side_effect=ServiceError(ErrorCode.INVALID_INPUT)):
                try:
                    fixture = SchemaFixture()
                except ServiceError as exc:
                    refused = exc.code == ErrorCode.INVALID_INPUT
        finally:
            if fixture is not None:
                fixture.close()
        self.assertTrue(refused, 'Fixture must not bypass settings validation')
