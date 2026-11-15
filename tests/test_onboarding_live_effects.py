"""Offline permit tests: no database, secrets, Google or browser access."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch
from uuid import uuid4

from onboarding.errors import ErrorCode, ServiceError
from onboarding.security import Actor


class LiveEffectTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_effects'),
                             'durable live effect boundary missing')
        from onboarding import live_effects
        return live_effects

    def context(self, m):
        binding = m.FenceBinding('mailbox', str(uuid4()), 'worker:test', 1, 1)
        return m.EffectContext(Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1),
            str(uuid4()), 1, 'pool-' + str(uuid4()), 'google.login.email.submit', (binding,), 'a'*64)

    def prepared(self, m, *, failure=None, newly=True):
        context = self.context(m)
        record = m.JournalRecord(str(uuid4()), 'INTENT', newly)
        events = []
        @contextmanager
        def uow(settings):
            events.append('begin')
            yield object()
            if failure:
                raise ServiceError(failure)
            events.append('committed')
        with patch('onboarding.storage.unit_of_work', uow), patch.object(m, 'prepare', return_value=record):
            result = m.prepare_committed(object(), context, context.step_key, 'test-key')
        return context, result, events

    def test_raw_record_has_no_send_authority(self):
        m = self.module()
        record = m.JournalRecord(str(uuid4()), 'INTENT', True)
        self.assertFalse(hasattr(record, 'consume'))
        forged = m.PreparedEffect(record.receipt_id, 'INTENT', 'a'*64)
        with self.assertRaises(ServiceError):
            asyncio.run(forged.consume('google.login.email.submit'))

    def test_confirmed_commit_permit_is_consumed_once(self):
        m = self.module()
        context, permit, events = self.prepared(m)
        self.assertEqual(events, ['begin', 'committed'])
        with patch.object(m, '_dispatch_guard') as guard:
            self.assertIs(asyncio.run(permit.consume(context.step_key)), True)
            with self.assertRaises(ServiceError):
                asyncio.run(permit.consume(context.step_key))
            guard.assert_called_once()

    def test_commit_unknown_does_not_register_permit(self):
        m = self.module()
        count = len(m._permits)
        with self.assertRaises(ServiceError) as caught:
            self.prepared(m, failure=ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(len(m._permits), count)

    def test_replay_returns_record_without_permit(self):
        m = self.module()
        _, record, _ = self.prepared(m, newly=False)
        self.assertIs(type(record), m.JournalRecord)
        self.assertFalse(hasattr(record, 'consume'))

    def test_wrong_action_and_guard_failure_burn_permit(self):
        m = self.module()
        for wrong in (True, False):
            context, permit, _ = self.prepared(m)
            with patch.object(m, '_dispatch_guard', side_effect=ServiceError(ErrorCode.STALE_FENCE)) as guard:
                with self.assertRaises(ServiceError):
                    asyncio.run(permit.consume('sub2api.import_disabled' if wrong else context.step_key))
                with self.assertRaises(ServiceError):
                    asyncio.run(permit.consume(context.step_key))
                self.assertEqual(guard.call_count, 0 if wrong else 1)

    def test_cross_process_permit_denied(self):
        m = self.module()
        context, permit, _ = self.prepared(m)
        with patch.object(m.os, 'getpid', return_value=-1), patch.object(m, '_dispatch_guard') as guard:
            with self.assertRaises(ServiceError):
                asyncio.run(permit.consume(context.step_key))
            guard.assert_not_called()

    def test_invalid_context_and_unapproved_actions_fail_before_sql(self):
        m = self.module()
        context = self.context(m)
        for action in ('google.model.test', 'google.billing.bind', ' arbitrary ', '', None):
            with self.assertRaises(ServiceError):
                m.prepare(None, context, action, 'test-key')
        with self.assertRaises(ServiceError):
            m.prepare(None, replace(context, generation=True), context.step_key, 'test-key')

    def test_concurrent_consume_has_one_winner(self):
        m = self.module()
        context, permit, _ = self.prepared(m)
        async def race():
            return await asyncio.gather(permit.consume(context.step_key),permit.consume(context.step_key),
                                        return_exceptions=True)
        with patch.object(m, '_dispatch_guard') as guard:
            results=asyncio.run(race())
        self.assertEqual(sum(result is True for result in results),1)
        self.assertEqual(sum(isinstance(result,ServiceError) for result in results),1)
        guard.assert_called_once()

    def test_fingerprint_excludes_fence_and_worker_but_pins_revisions(self):
        m = self.module()
        context = self.context(m)
        other = replace(context, bindings=(replace(context.bindings[0], owner_id='worker:new', fence=2),))
        self.assertEqual(m.request_hash(context, context.step_key), m.request_hash(other, other.step_key))
        revised = replace(context, bindings=(replace(context.bindings[0], revision=2),))
        self.assertNotEqual(m.request_hash(context, context.step_key), m.request_hash(revised, revised.step_key))

    def test_effect_target_is_bound_in_hash_and_registered_permit(self):
        m = self.module()
        context,permit,_=self.prepared(m)
        changed=replace(context,effect_digest='b'*64)
        self.assertNotEqual(m.request_hash(context,context.step_key),m.request_hash(changed,changed.step_key))
        self.assertEqual(permit.effect_digest,context.effect_digest)
        object.__setattr__(permit,'effect_digest','b'*64)
        with patch.object(m,'_dispatch_guard') as guard, self.assertRaises(ServiceError):
            asyncio.run(permit.consume(context.step_key))
        guard.assert_not_called()

    def test_valid_action_cannot_dispatch_from_a_different_step(self):
        m=self.module();context=self.context(m)
        try:
            m.prepare(None,context,'service_account.key.create','step-mismatch')
        except Exception as error:
            self.assertIsInstance(error,ServiceError)
            self.assertEqual(error.code,ErrorCode.INVALID_INPUT)
        else:
            self.fail('wrong-step action was accepted')
