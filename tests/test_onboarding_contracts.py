"""B0 typed boundaries, not live authorization."""
import dataclasses
import importlib
import importlib.util
import unittest


class ContractsTests(unittest.TestCase):
    def module(self, name):
        self.assertIsNotNone(importlib.util.find_spec(name), 'B0 module missing')
        return importlib.import_module(name)

    def test_errors_have_only_safe_code(self):
        m = self.module('onboarding.errors')
        for code in m.ErrorCode:
            error = m.ServiceError(code)
            self.assertEqual(str(error), code.value)
            self.assertIs(error.code, code)

    def test_pending_has_no_dispatch_permission(self):
        m = self.module('onboarding.contracts')
        pending = m.PendingAction('receipt-1', 'INTENT', True)
        self.assertFalse(hasattr(pending, 'dispatch_permit'))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            pending.newly_consumed = False

    def test_frozen_lease_token(self):
        m = self.module('onboarding.contracts')
        lease = m.LeaseToken('mailbox', 'm1', 't1', 'worker1', 2)
        self.assertEqual(lease.fence, 2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lease.fence = 0

    def test_observation_contains_references_only(self):
        m = self.module('onboarding.contracts')
        self.assertEqual([f.name for f in dataclasses.fields(m.Observation)],
                         ['code', 'evidence_ref', 'external_ref'])
