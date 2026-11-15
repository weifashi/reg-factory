"""Read-only request lookup before admission; no uncommitted retry authority."""
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from onboarding import live_batches as m
from onboarding.errors import ServiceError


class LiveObserveTests(unittest.TestCase):
    def test_historical_projector_retains_mode_used_by_replay(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        owner, tid, bid, cid, mid, secret, sid = (uuid4() for _ in range(7))
        now = datetime.now(timezone.utc)
        raw = (tid, bid, cid, 'pool', mid, str(mid), 'google', ['google'], 1, secret,
               {'google': dict(state_id=str(sid), secret_ref=str(secret), revision=1,
                               identity_status='UNKNOWN')},
               'QUEUED', None, None, 1, 2, False, now, now, 'live')
        conn = Mock()
        values = iter((raw, (owner, cid, [str(mid)], 1, 'specified'), ('pool-' + str(cid),)))
        conn.execute.side_effect = lambda *args: SimpleNamespace(fetchone=lambda: next(values))
        result = m._task(conn, str(tid))
        self.assertEqual(result['execution_mode'], 'live')
        self.assertEqual(result['execution_scope'], 'pool')
        self.assertEqual(result['created_by'], str(owner))
        self.assertIn(',execution_mode ', conn.execute.call_args_list[0].args[0])

    def test_invalid_context_never_opens_database(self):
        with patch.object(m.storage, 'unit_of_work', side_effect=AssertionError('database reached')):
            with self.assertRaises(ServiceError):
                m.observe_request(object(), object(), str(uuid4()), 'pool-' + str(uuid4()),
                                  'lookup', policy=object(), mac=object())

    def test_lookup_never_issues_admission_claim_or_provider_work(self):
        from types import SimpleNamespace
        actor = SimpleNamespace(operator_id=str(uuid4()))
        settings = object()
        policy, mac, conn = Mock(), Mock(), Mock()
        mac.request_digest.return_value = 'a' * 64
        conn.execute.return_value.fetchone.return_value = None
        trace = []
        @contextmanager
        def uow(value):
            yield conn
            self.assertEqual(trace[-1], 'auth')
        with patch.object(m.live_admission, 'inputs'), patch.object(m.storage, 'unit_of_work', uow), \
                patch.object(m.security, 'revalidate', side_effect=lambda *args: trace.append('auth')), \
                patch.object(m.live_admission, 'check', side_effect=AssertionError('admission')), \
                patch.object(m, 'create_one', side_effect=AssertionError('claim')):
            self.assertIsNone(m.observe_request(settings, actor, str(uuid4()), 'pool-' + str(uuid4()),
                                                'lookup', policy=policy, mac=mac))
        self.assertTrue(conn.execute.called)
        self.assertTrue(all(call.args[0].startswith('SELECT ') for call in conn.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
