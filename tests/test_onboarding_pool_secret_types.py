"""Pure synthetic payload contracts. No DB, network, real data or I/O."""
import importlib
import unittest
from dataclasses import FrozenInstanceError, replace

from onboarding.errors import ServiceError


class PoolPayloadTests(unittest.TestCase):
    def api(self):
        return importlib.import_module('onboarding.pool_secret_types')

    def test_exact_payload_types_are_frozen_and_repr_hides_all_fields(self):
        api = self.api()
        values = (api.MailboxCredential('one@fixture.invalid', password='fixture:secret-canary'),
                  api.PlatformCredential('one@fixture.invalid', 'google', 'fixture:secret-canary'),
                  api.Pan('4111111111111111'), api.BillingHolder('fixture:holder'),
                  api.BillingAddress('US', 'fixture:line', '', 'fixture:city', 'fixture:region', 'fixture:postal'),
                  api.CardExpiry(12, 2099))
        for value in values:
            with self.subTest(type=type(value).__name__):
                for canary in ('fixture:', '4111111111111111', '2099', 'fixture.invalid'):
                    self.assertNotIn(canary, repr(value))
                with self.assertRaises(FrozenInstanceError):
                    value.extra = 'secret'

    def test_mailbox_domain_and_synthetic_secrets_are_mandatory(self):
        api = self.api()
        base = api.MailboxCredential('one@fixture.invalid', password='fixture:pass')
        for changes in ({'email_norm': 'one@example.com'}, {'email_norm': 'UPPER@fixture.invalid'},
                        {'email_norm': 'one@fixture.invalid.evil'}, {'email_norm': ' one@fixture.invalid'},
                        {'password': 'ordinary-secret'}, {'refresh_token': 'M.real'},
                        {'mail_api_key': 'real-key'}, {'client_id': 'real-client'},
                        {'two_factor': 'arbitrary-seed'}, {'provider': 'arbitrary'},
                        {'mail_api_url': 'https://mail.fixture.invalid?token=bad'},
                        {'mail_api_url': 'https://user@MAIL.fixture.invalid'}, {'password': True},
                        {'password': 'fixture:\x00bad'}, {'password': 'fixture:\ud800'}):
            with self.subTest(keys=list(changes)), self.assertRaises(ServiceError):
                replace(base, **changes)
        self.assertEqual(api.MailboxCredential('one@sub.fixture.invalid', password='fixture:pass').email_norm,
                         'one@sub.fixture.invalid')

    def test_bounded_exact_types_and_no_dynamic_kind(self):
        api = self.api()
        for build in (lambda: api.Pan('4000000000000002'), lambda: api.Pan(4111111111111111),
                      lambda: api.BillingHolder('Real Person'), lambda: api.CardExpiry(True, 2099),
                      lambda: api.CardExpiry(13, 2099), lambda: api.CardExpiry(12, 2028),
                      lambda: api.PlatformCredential('one@fixture.invalid', 'unknown', 'fixture:p'),
                      lambda: api.MailboxCredential('one@fixture.invalid', password='fixture:' + 'x' * 65536),
                      lambda: api.BillingAddress('GB', 'fixture:a', '', 'fixture:c', 'fixture:r', 'fixture:p')):
            with self.subTest(build=build), self.assertRaises(ServiceError):
                build()
        for name in ('Cvv', 'OTP', 'GoogleCredential', 'ServiceAccountJson'):
            self.assertFalse(hasattr(api, name))
        with self.assertRaises(TypeError):
            api.Pan('4111111111111111', cvv='123')

    def test_resource_and_secret_ref_are_strict(self):
        api = self.api()
        # Include letters so uppercase is always a genuinely noncanonical UUID.
        identity = 'abcdefab-0000-4000-8000-000000000001'
        self.assertEqual(api.PoolResource('mailbox', identity).id, identity)
        self.assertEqual(api.SecretRef(identity, 1).revision, 1)
        for build in (lambda: api.PoolResource('fixture', identity), lambda: api.PoolResource('card', identity.upper()),
                      lambda: api.SecretRef(identity, True), lambda: api.SecretRef(identity, 0)):
            with self.assertRaises(ServiceError):
                build()

    def test_billing_fields_are_exact_fixtures_not_prefix_escape_hatches(self):
        api = self.api()
        with self.assertRaises(ServiceError):
            api.BillingHolder('fixture:real-name')
        base = api.BillingAddress('US','fixture:line','','fixture:city','fixture:region','fixture:postal')
        for key in ('line1','line2','city','region','postal_code'):
            with self.subTest(key=key), self.assertRaises(ServiceError):
                replace(base, **{key: 'fixture:real-address'})

    def test_forged_exact_classes_missing_or_extra_fields_fail_with_fixed_code(self):
        api = self.api()
        for cls in (api.MailboxCredential,api.PlatformCredential,api.Pan,api.BillingHolder,api.BillingAddress,api.CardExpiry):
            with self.subTest(cls=cls.__name__), self.assertRaises(ServiceError):
                api._encode_payload(object.__new__(cls))
        extra = api.Pan('4111111111111111')
        object.__setattr__(extra,'cvv','fixture:forbidden')
        with self.assertRaises(ServiceError):
            api._encode_payload(extra)
