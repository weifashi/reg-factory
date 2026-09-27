"""B2 audit actions must be explicit, not a permissive catch-all."""
import unittest
from onboarding import audit

class B2AuditContractTests(unittest.TestCase):
    def test_b2_audit_actions_registered_explicitly(self):
        required={'auth.operator_create','auth.operator_update','auth.bootstrap','auth.login',
                  'auth.login_failed','auth.logout','auth.session_touch','secret.put','secret.use','secret.rotate',
                  'secret.revoke','download.approve','download.issue','download.consume','download.revoke'}
        self.assertTrue(required.issubset(audit._ACTIONS))
        self.assertNotIn('arbitrary.secret.payload', audit._ACTIONS)

from unittest.mock import Mock, patch
from psycopg.pq import TransactionStatus
from onboarding import repository, security
from onboarding.errors import ErrorCode, ServiceError

class AuthenticatedRepositoryBridgeTests(unittest.TestCase):
    def test_authenticated_actor_uses_live_session_revalidation(self):
        conn=Mock(); conn.info.transaction_status=TransactionStatus.INTRANS
        actor=security.Actor('fixture-id',frozenset({'tasks:manage'}),'fixture-session',1)
        with patch('onboarding.security.revalidate',return_value=actor,create=True) as check:
            repository._actor(conn,actor,'tasks:manage')
            check.assert_called_once_with(conn,actor,'tasks:manage')
        conn.execute.assert_not_called()

    def test_plain_objects_cannot_impersonate_authenticated_actor(self):
        conn=Mock(); conn.info.transaction_status=TransactionStatus.INTRANS
        with self.assertRaises(ServiceError) as caught:
            repository._actor(conn,{'operator_id':'forged'},'tasks:manage')
        self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
