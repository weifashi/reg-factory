"""B1 repository/audit contracts use synthetic identities only."""
import importlib.util
import unittest


class RepositoryInterfaceTests(unittest.TestCase):
    def test_repository_interface_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.repository'))

    def test_audit_interface_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.audit'))

    def test_mailbox_import_action_is_explicit_and_summary_remains_restricted(self):
        from onboarding import audit
        from onboarding.errors import ServiceError
        self.assertIn('mailbox.import', audit._ACTIONS)
        self.assertNotIn('mailbox.import.raw', audit._ACTIONS)
        self.assertEqual(audit._summary({'version': 1}), {'version': 1})
        for field in ('password', 'email', 'request_hash', 'source_fingerprint', 'created_ids'):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                audit._summary({field: 'fixture:must-not-enter-audit-summary'})

    def test_mailbox_update_action_keeps_metadata_out_of_audit_summaries(self):
        from onboarding import audit
        from onboarding.errors import ServiceError
        self.assertIn('mailbox.update', audit._ACTIONS)
        self.assertNotIn('mailbox.update.raw', audit._ACTIONS)
        self.assertEqual(audit._summary({'version': 2}), {'version': 2})
        for field in ('group_ref', 'disabled', 'email', 'mailbox_id', 'request_hash', 'changes'):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                audit._summary({field: 'fixture:must-not-enter-audit-summary'})

from unittest.mock import patch
import uuid
from onboarding import repository, audit
from onboarding.errors import ErrorCode, ServiceError
from onboarding_b1_support import B1Case, CONFIG

class RepositoryTests(B1Case):
    def test_create_immutable_config_batch_task_and_audit(self):
        with self.uow() as conn:
            config = repository.create_config(conn, self.actor, CONFIG)
            task = repository.create_fixture_task(conn, self.actor, 'fixture:mailbox-new', config['id'])
        self.assertEqual(task['status'], 'QUEUED')
        self.assertEqual(task['version'], 1)
        self.assertEqual(task['config_revision'], config['revision'])
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_batches')[0][0], 1)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events')[0][0], 2)
        with self.uow() as conn:
            locked = repository.lock_task(conn, self.actor, task['id'], expected_version=1)
        self.assertEqual(locked['id'], task['id'])

    def test_lock_rejects_disabled_wrong_permission_and_other_owner(self):
        task = self.task()
        for change in ('disabled=true', "permissions='{}'"):
            with self.uow() as conn:
                conn.execute('UPDATE operators SET ' + change + ' WHERE id=%s', (self.actor.operator_id,))
            with self.assertRaises(ServiceError) as raised:
                with self.uow() as conn:
                    repository.lock_task(conn, self.actor, task['id'])
            self.assertEqual(raised.exception.code, ErrorCode.FORBIDDEN)
            with self.uow() as conn:
                conn.execute("UPDATE operators SET disabled=false,permissions=ARRAY['tasks:manage'] WHERE id=%s",
                             (self.actor.operator_id,))
        other = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) "
                         "VALUES (%s,%s,'fixture-only',ARRAY['tasks:manage'])", (other, 'fixture-' + other))
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                repository.lock_task(conn, repository.FixtureActor(other), task['id'])
        self.assertEqual(raised.exception.code, ErrorCode.FORBIDDEN)

    def test_task_cas_and_resource_revision(self):
        task = self.task()
        with self.uow() as conn:
            locked = repository.lock_task(conn, self.actor, task['id'], expected_version=1)
            first = repository.resource_revision(locked)
            updated = repository.bump_task(conn, locked, status='RUNNING')
        self.assertEqual(updated['version'], 2)
        self.assertEqual(repository.resource_revision(updated), first)
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                repository.bump_task(conn, locked, status='PAUSED')
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_CONFLICT)
        with self.uow() as conn:
            updated = repository.bump_task(conn, updated, generation=2)
        self.assertNotEqual(repository.resource_revision(updated), first)

    def test_no_transaction_is_rejected(self):
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError) as raised:
                repository.require_transaction(conn)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)

    def test_no_batch_config_override_or_real_reference(self):
        for value in ({**CONFIG, 'pan': 'secret'}, {**CONFIG, 'model': 'real-model'}):
            with self.assertRaises(ServiceError) as raised:
                with self.uow() as conn:
                    repository.create_config(conn, self.actor, value)
            self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                repository.create_fixture_task(conn, self.actor, 'real@example.com', self.config_id)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks')[0][0], 0)

    def test_audit_failure_rolls_back_task_and_batch(self):
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                with patch.object(audit, 'append', side_effect=RuntimeError('fixture failure')) as failing_audit:
                    repository.create_fixture_task(conn, self.actor, 'fixture:rollback', self.config_id)
        failing_audit.assert_called_once()
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks')[0][0], 0)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_batches')[0][0], 0)

    def test_audit_summary_rejects_secret_fields_and_free_text(self):
        task = self.task()
        for summary in ({'password': 'canary'}, {'status': 'canary-password'}, {'version': True}):
            with self.assertRaises(ServiceError) as raised:
                with self.uow() as conn:
                    audit.append(conn, self.actor.operator_id, task['id'], 'task.pause', task['id'],
                                 'OK', str(uuid.uuid4()), after_summary=summary)
            self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events')[0][0], 0)

    def test_audit_persists_minimal_structured_summary(self):
        task = self.task()
        with self.uow() as conn:
            event = audit.append(conn, self.actor.operator_id, task['id'], 'task.pause', task['id'],
                                 'OK', str(uuid.uuid4()), after_summary={'status':'PAUSED','version':2})
        self.assertGreater(event, 0)
        self.assertEqual(self.read('SELECT after_summary FROM audit_events')[0][0], {'status':'PAUSED','version':2})

    def test_corrupted_config_cannot_cross_fixture_boundary(self):
        task = self.task()
        from psycopg.types.json import Jsonb
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                         (Jsonb({**CONFIG, 'model': 'real-provider'}), self.config_id))
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                repository.lock_task(conn, self.actor, task['id'])
        self.assertEqual(raised.exception.code, ErrorCode.FORBIDDEN)

    def test_unhashable_audit_action_and_permission_are_invalid_not_dependency_failure(self):
        task = self.task()
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                audit.append(conn, self.actor.operator_id, task['id'], [], task['id'], 'OK', task['id'])
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                repository.lock_task(conn, self.actor, task['id'], permission=[])
        self.assertEqual(raised.exception.code, ErrorCode.FORBIDDEN)

    def test_001_only_latest_fixture_ignores_newer_nonfixture_revision(self):
        from psycopg.types.json import Jsonb
        with self.uow() as conn:
            conn.execute('INSERT INTO global_configs(id,revision,nonsecret_config,changed_by,created_at) '
                         "VALUES(%s,%s,%s,%s,clock_timestamp()+interval '1 day')",
                         (str(uuid.uuid4()),'pool-'+str(uuid.uuid4()),Jsonb(CONFIG),self.actor.operator_id))
            try:
                task=repository.create_fixture_task(conn,self.actor,'fixture:isolated',self.config_id)
            except ServiceError as exc:
                self.fail('001 latest fixture filter missing: '+exc.code.value)
            self.assertEqual(repository.lock_task(conn,self.actor,task['id'])['id'],task['id'])
        self.assertNotIn('scope',[r[0] for r in self.read("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='global_configs'")])
