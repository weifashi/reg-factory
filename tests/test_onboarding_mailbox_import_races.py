"""Real spawn races: separate authenticated sessions and actual app transactions.

The sole instrumentation rendezvous runs before each child's first real vault
write. Neither SQL, service return values nor authorization are mocked. Children
load private settings themselves; only synthetic import text and public fixture
identifiers cross the boundary, never live connections, real credentials, database
passwords, key material or raw exceptions.
"""
from dataclasses import replace
import hashlib
import json
import multiprocessing
import secrets
import unittest
import uuid
from unittest.mock import patch

from onboarding import security
from onboarding_pool_support import PoolCase


def _import_worker(schema, key_directory, operator_id, session_id, text, group,
                   request_key, barrier, entered, output):
    from onboarding import mailboxes
    from onboarding.errors import ServiceError
    from onboarding.keyring import Keyring
    from onboarding.pool_vault import PoolVault, SyntheticPoolPolicy
    from onboarding.request_mac import RequestMac
    from onboarding.settings import BASE, load_settings

    try:
        settings = replace(load_settings(BASE / 'app.json'), schema=schema)
        actor = security.Actor(operator_id, frozenset(), session_id, 1)
        vault = PoolVault(Keyring(key_directory, 'v1'),
                          SyntheticPoolPolicy.from_settings(settings))
        mac = RequestMac(key_directory)
        preview = mailboxes.preview_import(settings, actor, text, group,
                                           vault=vault, mac=mac)
        original = PoolVault.put_locked
        seen = False
        ordered = True
        first_email = min(item['email'] for item in json.loads(text))

        def synchronized_put(instance, conn, current_actor, resource, payload):
            nonlocal seen, ordered
            if not seen:
                seen = True
                ordered = payload.email_norm == first_email
                entered.set()
                barrier.wait(timeout=8)
            return original(instance, conn, current_actor, resource, payload)

        with patch.object(PoolVault, 'put_locked', synchronized_put):
            result = mailboxes.import_text(settings, actor, text, group,
                preview['preview_digest'], request_key, vault=vault, mac=mac)
        if not ordered:
            output.put(('INVALID_RESOURCE_ORDER', (), (), ''))
            return
        if type(result) is not mailboxes.MailboxImportResult:
            output.put(('INVALID_RESULT_TYPE', (), (), ''))
            return
        output.put(('OK', result.created_ids, result.skipped_ids, result.request_key))
    except ServiceError as exc:
        output.put((exc.code.value, (), (), ''))
    except Exception:
        # Never send str(exc), traceback, input, connection options or key bytes.
        output.put(('UNEXPECTED', (), (), ''))


def _text(*names):
    return json.dumps([{'email': name + '@fixture.invalid',
                        'password': 'fixture:mailbox', 'client_id': '',
                        'provider': 'outlook'} for name in names])


class MailboxImportRaceTests(PoolCase):
    def _new_session(self, *, other_owner=False):
        owner = str(uuid.uuid4()) if other_owner else self.actor.operator_id
        session = str(uuid.uuid4())
        with self.uow() as conn:
            if other_owner:
                conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) '
                    'VALUES(%s,%s,%s,%s)', (owner, 'fixture-' + uuid.uuid4().hex,
                    'fixture-only', ['mailboxes:manage']))
            conn.execute('INSERT INTO operator_sessions '
                '(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',"
                "clock_timestamp()+interval '30 minutes')", (session, owner,
                    hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                    hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        return owner, session

    def _race(self, left, right, *, different_owner=False):
        """left/right = (synthetic text, group, request key)."""
        actors = [self._new_session(), self._new_session(other_owner=different_owner)]
        self.assertNotEqual(actors[0][1], actors[1][1])
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(2)
        entered = [context.Event(), context.Event()]
        # One result channel per child keeps winner-to-owner association explicit.
        queues = [context.Queue(), context.Queue()]
        children = [context.Process(target=_import_worker, args=(
            self.settings.schema, str(self.key_directory), *actors[index],
            *request, barrier, entered[index], queues[index]))
            for index, request in enumerate((left, right))]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=25) for queue in queues]
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
            self.assertTrue(all(event.is_set() for event in entered),
                'Both authenticated transactions must enter the first-write rendezvous')
            for code, created, skipped, request_key in results:
                self.assertNotIn(code, ('UNEXPECTED', 'INVALID_RESULT_TYPE', 'INVALID_RESOURCE_ORDER',
                                        'DEPENDENCY_UNAVAILABLE', 'COMMIT_UNKNOWN'))
                self.assertIs(type(created), tuple)
                self.assertIs(type(skipped), tuple)
                for identity in created + skipped:
                    self.assertEqual(str(uuid.UUID(identity)), identity)
                if code != 'OK':
                    self.assertEqual((created, skipped, request_key), ((), (), ''))
            return actors, results
        finally:
            for child in children:
                if child.pid is not None and child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            for queue in queues:
                queue.close()
                queue.join_thread()

    def _assert_storage(self, mailboxes, receipts):
        for table, expected in (('mailbox_registry', mailboxes),
                ('secret_objects', mailboxes),
                ('mailbox_platform_states', mailboxes * 7),
                ('operation_receipts', receipts)):
            self.assertEqual(self.read('SELECT count(*) FROM ' + table)[0][0], expected, table)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects s '
            'LEFT JOIN mailbox_registry m ON m.credential_ref=s.id WHERE m.id IS NULL')[0][0], 0)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='secret.put'")[0][0], mailboxes)
        self.assertEqual(self.read('SELECT count(*) FROM mailbox_platform_states '
            "WHERE identity_status<>'UNKNOWN' OR usage_status<>'HISTORY_UNRECONCILED' "
            'OR credential_ref IS NOT NULL')[0][0], 0)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts '
            "WHERE phase<>'SUCCEEDED' OR task_id IS NOT NULL "
            "OR scope_operator_id IS NULL OR action<>'mailbox.import'")[0][0], 0)

    def test_inverse_order_same_emails_creates_each_once_without_orphan_secret(self):
        _, results = self._race((_text('beta', 'alpha'), 'fixture:group', 'race-left'),
                                (_text('alpha', 'beta'), 'fixture:group', 'race-right'))
        self.assertEqual([item[0] for item in results], ['OK', 'OK'])
        self.assertEqual(sorted((len(item[1]), len(item[2])) for item in results), [(0, 2), (2, 0)])
        self.assertEqual(set(results[0][1] + results[0][2]),
                         set(results[1][1] + results[1][2]))
        self._assert_storage(2, 2)

    def test_same_owner_key_and_hash_returns_identical_original_winner_result(self):
        _, results = self._race((_text('beta', 'alpha'), 'fixture:group', 'race-replay'),
                                (_text('alpha', 'beta'), 'fixture:group', 'race-replay'))
        self.assertEqual([item[0] for item in results], ['OK', 'OK'])
        self.assertEqual(results[0], results[1])
        self.assertEqual((len(results[0][1]), len(results[0][2])), (2, 0))
        row = self.read('SELECT result_summary FROM operation_receipts')[0][0]
        self.assertEqual(row, {'created_ids': list(results[0][1]), 'skipped_ids': [],
                              'request_key': 'race-replay'})
        self._assert_storage(2, 1)

    def test_same_key_different_group_conflicts_and_keeps_winner_group(self):
        _, results = self._race((_text('alpha'), 'fixture:left', 'race-conflict'),
                                (_text('alpha'), 'fixture:right', 'race-conflict'))
        self.assertEqual(sorted(item[0] for item in results), ['IDEMPOTENCY_CONFLICT', 'OK'])
        winner = next(index for index, item in enumerate(results) if item[0] == 'OK')
        self.assertEqual(self.read('SELECT group_ref FROM mailbox_registry')[0][0],
                         ('fixture:left', 'fixture:right')[winner])
        self._assert_storage(1, 1)

    def test_same_key_disjoint_payload_rolls_back_loser_resources_and_secret_audit(self):
        _, results = self._race((_text('alpha'), 'fixture:group', 'race-conflict'),
                                (_text('beta'), 'fixture:group', 'race-conflict'))
        self.assertEqual(sorted(item[0] for item in results), ['IDEMPOTENCY_CONFLICT', 'OK'])
        winner = next(index for index, item in enumerate(results) if item[0] == 'OK')
        self.assertEqual(self.read('SELECT email_norm FROM mailbox_registry')[0][0],
                         ('alpha@fixture.invalid', 'beta@fixture.invalid')[winner])
        self.assertEqual(tuple(str(row[0]) for row in self.read('SELECT id FROM mailbox_registry')),
                         results[winner][1])
        self._assert_storage(1, 1)

    def test_different_owners_same_email_loser_is_forbidden_without_disclosing_ids(self):
        actors, results = self._race((_text('alpha'), 'fixture:group', 'race-left'),
            (_text('alpha'), 'fixture:group', 'race-right'), different_owner=True)
        self.assertEqual(sorted(item[0] for item in results), ['FORBIDDEN', 'OK'])
        winner = next(index for index, item in enumerate(results) if item[0] == 'OK')
        self.assertEqual(str(self.read('SELECT owner_operator_id FROM mailbox_registry')[0][0]),
                         actors[winner][0])
        self.assertEqual(results[1 - winner], ('FORBIDDEN', (), (), ''))
        self._assert_storage(1, 1)


if __name__ == '__main__':
    unittest.main()
