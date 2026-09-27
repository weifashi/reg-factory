"""Only committed, process-local fixture permits can cause synthetic execution."""
import importlib
import importlib.util
import unittest
from unittest.mock import patch


class CoordinatorBoundaryTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.coordinator'),
                             'B1 coordinator is required')
        return importlib.import_module('onboarding.coordinator')

    def test_forged_prepared_action_and_provider_are_rejected(self):
        from onboarding.contracts import PreparedAction
        from onboarding.errors import ServiceError
        api = self.api()
        executor = api.FixtureExecutor()
        with self.assertRaises(ServiceError):
            api.execute_prepared(PreparedAction('forged', 'INTENT', True), executor)
        self.assertEqual(executor.calls, 0)
        with self.assertRaises(ServiceError):
            api.execute_prepared(PreparedAction('forged', 'INTENT', True), lambda: None)

    def test_prepare_mints_permit_only_after_commit_and_never_on_unknown(self):
        from contextlib import contextmanager
        from onboarding.contracts import LeaseToken, PendingAction
        from onboarding.errors import ServiceError, ErrorCode
        api = self.api()
        self.assertTrue(callable(getattr(api, 'prepare_action', None)))
        token = LeaseToken('fixture', 'fixture:r', 'task', 'worker', 1)
        committed = []
        @contextmanager
        def successful(settings):
            yield object()
            committed.append(True)
        with patch('onboarding.storage.unit_of_work', successful), patch(
                'onboarding.receipts.prepare', return_value=PendingAction('receipt', 'INTENT', True), create=True):
            action = api.prepare_action(None, None, token, 'test', 'r', 'c', 'k', {}, 'approval')
        self.assertTrue(committed)
        self.assertTrue(action.dispatch_permit)
        @contextmanager
        def unknown(settings):
            yield object()
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch('onboarding.storage.unit_of_work', unknown), patch(
                'onboarding.receipts.prepare', return_value=PendingAction('unknown', 'INTENT', True), create=True):
            with self.assertRaises(ServiceError) as caught:
                api.prepare_action(None, None, token, 'test', 'r', 'c', 'k', {}, 'approval')
        self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)

    def test_only_original_permit_executes_once_and_failure_never_retries(self):
        from contextlib import contextmanager
        from dataclasses import replace
        from onboarding.contracts import LeaseToken, PendingAction
        from onboarding.errors import ServiceError
        api = self.api()
        token = LeaseToken('fixture', 'fixture:r', 'task', 'worker', 1)
        @contextmanager
        def uow(settings):
            yield object()
        with patch('onboarding.storage.unit_of_work', uow), patch(
                'onboarding.receipts.prepare', return_value=PendingAction('receipt', 'INTENT', True), create=True):
            prepared = api.prepare_action(None, None, token, 'test', 'r', 'c', 'k', {}, 'approval')
        executor = api.FixtureExecutor()
        with self.assertRaises(ServiceError):
            api.execute_prepared(replace(prepared), executor)
        with patch.object(api, '_dispatch_guard', create=True), patch.object(api, '_record_success', create=True):
            try:
                api.execute_prepared(prepared, executor)
            except ServiceError:
                self.fail("Original committed permit must execute the fixture once")
            self.assertEqual(executor.calls, 1)
            with self.assertRaises(ServiceError):
                api.execute_prepared(prepared, executor)
        self.assertEqual(executor.calls, 1)

    def test_executor_cannot_be_replaced_with_an_instance_callback(self):
        api = self.api()
        executor = api.FixtureExecutor()
        with self.assertRaises(AttributeError):
            executor._execute = lambda: None


    def test_permit_cannot_be_inherited_by_a_forked_process(self):
        import os
        from contextlib import contextmanager
        from onboarding.contracts import LeaseToken, PendingAction
        from onboarding.errors import ServiceError
        api = self.api()
        token = LeaseToken('fixture', 'fixture:r', 'task', 'worker', 1)
        @contextmanager
        def uow(settings):
            yield object()
        with patch('onboarding.storage.unit_of_work', uow), patch(
                'onboarding.receipts.prepare', return_value=PendingAction('receipt', 'INTENT', True)):
            prepared = api.prepare_action(None, None, token, 'test', 'r', 'c', 'k', {}, 'approval')
        executor = api.FixtureExecutor()
        with patch.object(api, '_dispatch_guard'), patch.object(api, '_record_success'):
            with patch.object(os, 'getpid', return_value=-1):
                with self.assertRaises(ServiceError):
                    api.execute_prepared(prepared, executor)
            self.assertEqual(executor.calls, 0)
            api.execute_prepared(prepared, executor)
            self.assertEqual(executor.calls, 1)


# Real-PG integration cases are intentionally not skippable when unavailable.
from onboarding_b1_support import B1Case


class CoordinatorIntegrationTests(B1Case):
    def api(self):
        from onboarding import coordinator
        return coordinator

    def current(self, task_id):
        from onboarding import repository
        with self.uow() as conn:
            return repository.lock_task(conn, self.actor, task_id)

    def claimed(self):
        api = self.api()
        self.assertTrue(callable(getattr(api, 'claim_step', None)), 'claim_step missing')
        task = self.task()
        with self.uow() as conn:
            token = api.claim_step(conn, self.actor, task['id'], 'fixture.execute',
                                   'fixture-worker', task['version'])
        return task, token

    def approved(self):
        from onboarding import approvals, repository
        task, token = self.claimed()
        with self.uow() as conn:
            current = repository.lock_task(conn, self.actor, task['id'])
            revision = repository.resource_revision(current)
            approval = approvals.issue(conn, self.actor, task['id'], 'test', revision,
                                       current['config_revision'], 300)
        return task, token, revision, current['config_revision'], approval

    def prepare(self, arguments, key='request-1'):
        task, token, revision, config_revision, approval = arguments
        return self.api().prepare_action(self.settings, self.actor, token, 'test', revision,
                                        config_revision, key,
                                        {'test_spec_revision': 'fixture-v1'}, approval)

    def test_claim_step_records_fixed_fixture_and_audit_atomically(self):
        task, token = self.claimed()
        rows = self.read('SELECT step_key,state,fence FROM task_steps WHERE task_id=%s', (task['id'],))
        self.assertEqual(rows, [('fixture.execute', 'RUNNING', token.fence)])
        self.assertEqual(self.current(task['id'])['status'], 'RUNNING')
        with self.uow() as conn:
            from onboarding.errors import ServiceError
            with self.assertRaises(ServiceError):
                self.api().claim_step(conn, self.actor, task['id'], 'google.billing',
                                      'fixture-worker', self.current(task['id'])['version'])

    def test_real_execution_once_duplicate_and_reconstructed_permit_never_dispatch(self):
        from dataclasses import replace
        from onboarding.errors import ServiceError
        arguments = self.approved()
        prepared = self.prepare(arguments)
        executor = self.api().FixtureExecutor()
        with self.assertRaises(ServiceError):
            self.api().execute_prepared(replace(prepared), executor)
        observation = self.api().execute_prepared(prepared, executor)
        self.assertEqual(observation.code, 'SUCCEEDED')
        again = self.prepare(arguments)
        self.assertEqual(again.receipt_id, prepared.receipt_id)
        self.assertFalse(again.dispatch_permit)
        for forbidden in (prepared, again):
            with self.assertRaises(ServiceError):
                self.api().execute_prepared(forbidden, executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('SUCCEEDED',)])

    def test_pause_cancel_recheck_are_idempotent_cas_commands_without_execution(self):
        from onboarding.errors import ErrorCode, ServiceError
        api = self.api()
        self.assertTrue(callable(getattr(api, 'pause', None)), 'pause missing')
        task = self.task()
        with self.uow() as conn:
            first = api.pause(conn, self.actor, task['id'], task['version'], 'pause-1')
        with self.uow() as conn:
            self.assertEqual(api.pause(conn, self.actor, task['id'], task['version'], 'pause-1'), first)
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                api.pause(conn, self.actor, task['id'], task['version'], 'pause-2')
        self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        current = self.current(task['id'])
        with self.uow() as conn:
            api.recheck(conn, self.actor, task['id'], current['version'], 'check-1')
        self.assertEqual(self.current(task['id'])['status'], 'PAUSED')
        self.assertEqual(self.read("SELECT state FROM task_steps WHERE task_id=%s AND step_key='fixture.recheck'",
                                   (task['id'],)), [('NOT_SENT',)])
        current = self.current(task['id'])
        with self.uow() as conn:
            api.cancel(conn, self.actor, task['id'], current['version'], 'cancel-1')
        self.assertTrue(self.current(task['id'])['cancel_requested'])

    def test_commit_reply_lost_keeps_intent_and_never_returns_dispatch_permit(self):
        from contextlib import contextmanager
        from onboarding import storage
        from onboarding.errors import ErrorCode, ServiceError
        arguments = self.approved()
        original_uow = storage.unit_of_work
        @contextmanager
        def committed_but_reply_lost(settings):
            with original_uow(settings) as conn:
                yield conn
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage, 'unit_of_work', committed_but_reply_lost):
            with self.assertRaises(ServiceError) as caught:
                self.prepare(arguments)
        self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts'), [('INTENT',)])
        again = self.prepare(arguments)
        executor = self.api().FixtureExecutor()
        self.assertFalse(again.dispatch_permit)
        with self.assertRaises(ServiceError):
            self.api().execute_prepared(again, executor)
        self.assertEqual(executor.calls, 0)

    def test_restart_or_send_failure_never_recreates_permit(self):
        from onboarding.errors import ErrorCode, ServiceError
        for point in ('restart', 'before_send', 'after_send'):
            with self.subTest(point=point):
                arguments = self.approved()
                prepared = self.prepare(arguments)
                executor = self.api().FixtureExecutor()
                if point == 'restart':
                    # Simulate loss of all process-local capabilities, not a DB reset.
                    with self.api()._permit_lock:
                        self.api()._permits.clear()
                    with self.assertRaises(ServiceError):
                        self.api().execute_prepared(prepared, executor)
                else:
                    target = '_dispatch_guard' if point == 'before_send' else '_record_success'
                    with patch.object(self.api(), target, side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)):
                        with self.assertRaises(ServiceError):
                            self.api().execute_prepared(prepared, executor)
                again = self.prepare(arguments)
                self.assertFalse(again.dispatch_permit)
                with self.assertRaises(ServiceError):
                    self.api().execute_prepared(again, executor)
                self.assertEqual(executor.calls, 1 if point == 'after_send' else 0)
                self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                           (prepared.receipt_id,)), [('INTENT',)])

    def test_result_audit_failure_rolls_back_observation_without_resending(self):
        from onboarding import audit
        from onboarding.errors import ErrorCode, ServiceError
        arguments = self.approved()
        prepared = self.prepare(arguments)
        before = self.current(arguments[0]['id'])
        original_append = audit.append
        def fail_result(conn, actor_id, task_id, action, *args, **kwargs):
            if action == 'step.observe':
                raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            return original_append(conn, actor_id, task_id, action, *args, **kwargs)
        executor = self.api().FixtureExecutor()
        with patch.object(audit, 'append', fail_result):
            with self.assertRaises(ServiceError):
                self.api().execute_prepared(prepared, executor)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(self.current(arguments[0]['id'])['version'], before['version'])
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('INTENT',)])
        self.assertFalse(self.prepare(arguments).dispatch_permit)
        self.assertEqual(executor.calls, 1)

    def test_claim_and_command_audit_failure_roll_back_all_mutations(self):
        from onboarding import audit
        from onboarding.errors import ErrorCode, ServiceError
        api = self.api()
        task = self.task()
        original_append = audit.append
        for action in ('step.claim', 'task.pause'):
            def fail_selected(conn, actor_id, task_id, name, *args, **kwargs):
                if name == action:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                return original_append(conn, actor_id, task_id, name, *args, **kwargs)
            with patch.object(audit, 'append', fail_selected):
                with self.assertRaises(ServiceError):
                    with self.uow() as conn:
                        if action == 'step.claim':
                            api.claim_step(conn, self.actor, task['id'], 'fixture.execute', 'worker', task['version'])
                        else:
                            api.pause(conn, self.actor, task['id'], task['version'], 'pause-fail')
            self.assertEqual(self.current(task['id'])['version'], task['version'])
            self.assertEqual(self.read('SELECT count(*) FROM task_steps'), [(0,)])
            self.assertEqual(self.read('SELECT count(*) FROM resource_leases'), [(0,)])
            self.assertEqual(self.read('SELECT count(*) FROM operation_receipts'), [(0,)])

    def test_unknown_cancel_keeps_receipt_and_resource_hold(self):
        from onboarding.contracts import Observation
        arguments = self.approved()
        task, token = arguments[:2]
        prepared = self.prepare(arguments)
        with self.uow() as conn:
            self.api().record_observation(conn, self.actor, token, prepared.receipt_id,
                                          Observation('UNKNOWN', 'fixture:unknown', None),
                                          self.current(task['id'])['version'])
        current = self.current(task['id'])
        with self.uow() as conn:
            self.api().cancel(conn, self.actor, task['id'], current['version'], 'cancel-unknown')
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('UNKNOWN',)])
        lease = self.read('SELECT task_id,owner_id,fence FROM resource_leases WHERE resource_id=%s',
                          (token.resource_id,))[0]
        self.assertEqual((str(lease[0]), lease[1], lease[2]), (task['id'], token.owner_id, token.fence))
        self.assertEqual(self.current(task['id'])['status'], 'PAUSED')

    def test_dispatch_rechecks_registered_step_and_action_permission(self):
        from onboarding.errors import ServiceError
        for fault in ('missing_step', 'permission_revoked'):
            with self.subTest(fault=fault):
                arguments = self.approved()
                prepared = self.prepare(arguments)
                task = arguments[0]
                with self.fixture.migrator() as conn:
                    if fault == 'missing_step':
                        conn.execute('DELETE FROM task_steps WHERE task_id=%s', (task['id'],))
                    else:
                        conn.execute("UPDATE operators SET permissions=array_remove(permissions,'fees:approve') WHERE id=%s",
                                     (self.actor.operator_id,))
                executor = self.api().FixtureExecutor()
                with self.assertRaises(ServiceError):
                    self.api().execute_prepared(prepared, executor)
                self.assertEqual(executor.calls, 0)
                self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                           (prepared.receipt_id,)), [('INTENT',)])

    def test_unknown_observation_is_not_a_finished_step(self):
        from onboarding.contracts import Observation
        arguments = self.approved()
        task, token = arguments[:2]
        prepared = self.prepare(arguments)
        current = self.current(task['id'])
        with self.uow() as conn:
            self.api().record_observation(conn, self.actor, token, prepared.receipt_id,
                                          Observation('UNKNOWN', 'fixture:unknown', None), current['version'])
        self.assertEqual(self.read('SELECT state,finished_at FROM task_steps WHERE task_id=%s',
                                   (task['id'],)), [('UNKNOWN', None)])

    def test_only_test_action_can_enter_the_fixture_coordinator(self):
        from onboarding.errors import ErrorCode, ServiceError
        args = self.approved()
        for action in ('enable', 'download', 'google.billing', 'sub2api.enable'):
            with self.subTest(action=action), self.assertRaises(ServiceError) as caught:
                self.api().prepare_action(self.settings, self.actor, args[1], action,
                                          args[2], args[3], 'wrong-action',
                                          {'test_spec_revision': 'fixture-v1'}, args[4])
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts'), [(0,)])

    def test_recovered_owner_can_record_but_never_redispatch_old_intent(self):
        from onboarding import leases
        from onboarding.contracts import Observation
        from onboarding.errors import ServiceError
        arguments = self.approved()
        task, old = arguments[:2]
        prepared = self.prepare(arguments)
        evidence = leases.FixtureStopEvidence(task['id'], old.resource_kind,
                                              old.resource_id, old.owner_id, old.fence)
        with self.uow() as conn:
            recovered = leases.recover(conn, self.actor, task['id'], old.resource_kind,
                                       old.resource_id, 'recovered-worker', evidence)
        current = self.current(task['id'])
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                self.api().record_observation(conn, self.actor, old, prepared.receipt_id,
                                              Observation('SUCCEEDED', 'fixture:late', None), current['version'])
        try:
            with self.uow() as conn:
                self.api().record_observation(conn, self.actor, recovered, prepared.receipt_id,
                                              Observation('SUCCEEDED', 'fixture:reconciled', None), current['version'])
        except ServiceError:
            self.fail('Recovered current owner must be able to reconcile the same step generation')
        self.assertEqual(self.read('SELECT state,fence FROM task_steps WHERE task_id=%s',
                                   (task['id'],)), [('SUCCEEDED', recovered.fence)])
        self.assertFalse(self.prepare(arguments).dispatch_permit)
        executor = self.api().FixtureExecutor()
        with self.assertRaises(ServiceError):
            self.api().execute_prepared(prepared, executor)
        self.assertEqual(executor.calls, 0)

    def test_claim_and_observation_require_explicit_positive_integer_cas(self):
        from onboarding.contracts import Observation
        from onboarding.errors import ErrorCode, ServiceError
        api = self.api()
        task = self.task()
        for bad in (None, True, 0, -1, '1'):
            with self.subTest(operation='claim', value=bad):
                with self.assertRaises(ServiceError) as caught:
                    with self.uow() as conn:
                        api.claim_step(conn, self.actor, task['id'], 'fixture.execute', 'worker', bad)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        args = self.approved()
        prepared = self.prepare(args)
        for bad in (None, True, 0, -1, '1'):
            with self.subTest(operation='observe', value=bad):
                with self.assertRaises(ServiceError) as caught:
                    with self.uow() as conn:
                        api.record_observation(conn, self.actor, args[1], prepared.receipt_id,
                                               Observation('SUCCEEDED', 'fixture:result', None), bad)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)

    def test_inconclusive_late_observation_does_not_rewrite_confirmed_step_evidence(self):
        from onboarding.contracts import Observation
        args = self.approved()
        prepared = self.prepare(args)
        self.api().execute_prepared(prepared, self.api().FixtureExecutor())
        current = self.current(args[0]['id'])
        with self.uow() as conn:
            self.api().record_observation(conn, self.actor, args[1], prepared.receipt_id,
                                          Observation('UNKNOWN', 'fixture:inconclusive', None), current['version'])
        self.assertEqual(self.read('SELECT state,observation_code FROM task_steps WHERE task_id=%s',
                                   (args[0]['id'],)), [('SUCCEEDED', 'SUCCEEDED')])

    def test_pause_commit_before_competing_prepare_blocks_dispatch(self):
        import threading
        from onboarding.errors import ErrorCode, ServiceError
        arguments = self.approved()
        current = self.current(arguments[0]['id'])
        entered = threading.Event()
        outcomes = []
        executor = self.api().FixtureExecutor()
        def prepare_on_independent_connection():
            entered.set()
            try:
                permit = self.prepare(arguments)
                self.api().execute_prepared(permit, executor)
                outcomes.append('SENT')
            except ServiceError as exc:
                outcomes.append(exc.code)
        worker = threading.Thread(target=prepare_on_independent_connection, daemon=True)
        try:
            with self.uow() as conn:
                self.api().pause(conn, self.actor, arguments[0]['id'], current['version'], 'pause-first')
                # Main holds the real PG task row until this transaction commits.
                worker.start()
                self.assertTrue(entered.wait(5), 'competing connection did not start')
            worker.join(10)
            self.assertFalse(worker.is_alive(), 'competing prepare did not finish')
        finally:
            worker.join(10)
        self.assertEqual(outcomes, [ErrorCode.APPROVAL_INVALID])
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='test'"), [(0,)])

    def test_committed_dispatch_guard_can_be_inflight_when_pause_commits(self):
        import threading
        from onboarding.errors import ErrorCode, ServiceError
        args = self.approved()
        prepared = self.prepare(args)
        executor = self.api().FixtureExecutor()
        after_guard, continue_send = threading.Event(), threading.Event()
        outcomes = []
        original_execute = self.api().FixtureExecutor._execute
        def wait_after_committed_guard(instance):
            after_guard.set()
            if not continue_send.wait(5):
                raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            return original_execute(instance)
        def dispatch_on_independent_connection():
            try:
                outcomes.append(self.api().execute_prepared(prepared, executor).code)
            except ServiceError as exc:
                outcomes.append(exc.code)
        worker = threading.Thread(target=dispatch_on_independent_connection, daemon=True)
        with patch.object(self.api().FixtureExecutor, '_execute', wait_after_committed_guard):
            try:
                worker.start()
                self.assertTrue(after_guard.wait(5), 'guard did not commit')
                current = self.current(args[0]['id'])
                with self.uow() as conn:
                    self.api().pause(conn, self.actor, args[0]['id'], current['version'], 'pause-inflight')
                self.assertEqual(self.current(args[0]['id'])['status'], 'PAUSED')
                self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                           (prepared.receipt_id,)), [('INTENT',)])
                continue_send.set()
                worker.join(10)
                self.assertFalse(worker.is_alive(), 'inflight dispatch did not finish')
            finally:
                continue_send.set()
                worker.join(10)
        self.assertEqual(outcomes, ['SUCCEEDED'])
        self.assertEqual(executor.calls, 1)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('SUCCEEDED',)])

    def test_another_fixture_resource_at_same_fence_cannot_drive_this_step(self):
        from onboarding import leases
        from onboarding.contracts import Observation
        from onboarding.errors import ErrorCode, ServiceError
        args = self.approved()
        task, original = args[:2]
        current = self.current(task['id'])
        with self.uow() as conn:
            other = leases.claim(conn, self.actor, task['id'], 'fixture',
                                 'fixture:another-resource', 'another-worker', current['version'])
        self.assertEqual(other.fence, original.fence)
        wrong_args = (task, other, *args[2:])
        prepared = self.prepare(wrong_args)
        executor = self.api().FixtureExecutor()
        with self.assertRaises(ServiceError) as caught:
            self.api().execute_prepared(prepared, executor)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(executor.calls, 0)
        current = self.current(task['id'])
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                self.api().record_observation(conn, self.actor, other, prepared.receipt_id,
                                              Observation('SUCCEEDED', 'fixture:other', None), current['version'])
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT state,fence FROM task_steps WHERE task_id=%s',
                                   (task['id'],)), [('RUNNING', original.fence)])

    def test_non_test_primitive_receipt_cannot_update_fixture_execute_step(self):
        from onboarding import approvals, receipts
        from onboarding.contracts import Observation
        from onboarding.errors import ErrorCode, ServiceError
        for action in ('enable', 'download'):
            with self.subTest(action=action):
                args = self.approved()
                task, token, revision, config_revision, _ = args
                with self.uow() as conn:
                    approval = approvals.issue(conn, self.actor, task['id'], action, revision, config_revision, 300)
                    pending = receipts.prepare(conn, self.actor, token, action, revision, config_revision,
                                               'primitive-key', {'test_spec_revision': 'fixture-v1'}, approval)
                current = self.current(task['id'])
                with self.assertRaises(ServiceError) as caught:
                    with self.uow() as conn:
                        self.api().record_observation(conn, self.actor, token, pending.receipt_id,
                                                      Observation('SUCCEEDED', 'fixture:primitive', None),
                                                      current['version'])
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
                self.assertEqual(self.read('SELECT state FROM task_steps WHERE task_id=%s',
                                           (task['id'],)), [('RUNNING',)])

    def test_generation_change_after_prepare_blocks_old_local_permit(self):
        from onboarding.errors import ServiceError
        args = self.approved()
        prepared = self.prepare(args)
        with self.fixture.migrator() as conn, conn.transaction():
            conn.execute('UPDATE onboarding_tasks SET generation=generation+1 WHERE id=%s', (args[0]['id'],))
            conn.execute('UPDATE task_steps SET generation=generation+1 WHERE task_id=%s', (args[0]['id'],))
        executor = self.api().FixtureExecutor()
        with self.assertRaises(ServiceError):
            self.api().execute_prepared(prepared, executor)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('INTENT',)])

    def test_lease_expiring_while_dispatch_waits_on_receipt_never_sends(self):
        import threading
        from onboarding import leases
        from onboarding.errors import ErrorCode, ServiceError
        args = self.approved()
        prepared = self.prepare(args)
        with self.uow() as conn:
            deadline = conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '2 seconds' "
                                    'WHERE resource_id=%s RETURNING lease_until', (args[1].resource_id,)).fetchone()[0]
        checked = threading.Event()
        outcomes = []
        executor = self.api().FixtureExecutor()
        original_check = leases.assert_current
        def signal_first_live_check(conn, token):
            row = original_check(conn, token)
            checked.set()
            return row
        def independent_dispatch():
            try:
                self.api().execute_prepared(prepared, executor)
                outcomes.append('SENT')
            except ServiceError as exc:
                outcomes.append(exc.code)
        worker = threading.Thread(target=independent_dispatch, daemon=True)
        with patch.object(leases, 'assert_current', signal_first_live_check):
            try:
                with self.uow() as conn:
                    conn.execute('SELECT id FROM operation_receipts WHERE id=%s FOR UPDATE', (prepared.receipt_id,))
                    worker.start()
                    self.assertTrue(checked.wait(5), 'initial live lease check did not finish')
                    # The DB deadline, not a guessed scheduler sleep, releases the
                    # blocked receipt lock only after the validated lease expires.
                    conn.execute('SELECT pg_sleep_until(%s)', (deadline,))
                worker.join(10)
                self.assertFalse(worker.is_alive())
            finally:
                worker.join(10)
        self.assertEqual(outcomes, [ErrorCode.STALE_FENCE])
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',
                                   (prepared.receipt_id,)), [('INTENT',)])
