"""P1c schema contract: isolated manifest schemas only, never a real pool."""
import hashlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'onboarding/migrations/002_pools.sql'
CORE_SHA = '3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782'


class PoolSchemaInterfaceTests(unittest.TestCase):
    def test_second_migration_is_explicit_and_core_unchanged(self):
        self.assertTrue(SCRIPT.is_file(), '002 pool schema not implemented')
        core = ROOT / 'onboarding/migrations/001_core.sql'
        self.assertEqual(hashlib.sha256(core.read_bytes()).hexdigest(), CORE_SHA)
        text = SCRIPT.read_text()
        self.assertEqual(text.count('CREATE TABLE '), 6)
        self.assertNotIn('SECURITY DEFINER', text)
        self.assertNotIn('CREATE SCHEMA', text)


class PoolSchemaDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from onboarding_support import SchemaFixture
        from onboarding import migrate
        cls.fixture = SchemaFixture()
        cls.addClassCleanup(cls.fixture.close)
        with cls.fixture.migrator() as conn:
            migrate.apply_all(conn, cls.fixture.schema, target_version=2)

    def setUp(self):
        import uuid
        self.uuid = uuid.uuid4
        self.conn = self.fixture.migrator()
        self.addCleanup(self.conn.close)
        self.conn.execute('BEGIN')
        self.addCleanup(self.conn.rollback)
        self.operator = self.operator_row()
        self.config = self.uuid()
        self.batch = self.uuid()
        self.task = self.uuid()
        self.conn.execute("INSERT INTO global_configs(id,revision,nonsecret_config,changed_by) "
                          "VALUES(%s,%s,'{}',%s)", (self.config, 'fixture-' + self.uuid().hex, self.operator))
        self.conn.execute("INSERT INTO onboarding_batches(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) "
                          "VALUES(%s,'specified',1,'[\"fixture:mail\"]',%s,%s)", (self.batch, self.config, self.operator))
        self.conn.execute("INSERT INTO onboarding_tasks(id,batch_id,mailbox_ref,config_id) VALUES(%s,%s,'fixture:mail',%s)",
                          (self.task, self.batch, self.config))

    def operator_row(self):
        uid = self.uuid()
        self.conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,'fixture-hash')",
                          (uid, 'fixture-' + uid.hex))
        return uid

    def secret(self, kind):
        uid = self.uuid()
        self.conn.execute("INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) "
                          "VALUES(%s,%s,'fixture-pool-key',%s,%s,'fixture:pool')",
                          (uid, kind, self.uuid().bytes[:12], b'fixture-ciphertext-32-byte-value'))
        return uid

    def mailbox(self, email=None, owner=None):
        uid, secret = self.uuid(), self.secret('mailbox_credential')
        self.conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref) "
                          "VALUES(%s,%s,%s,'outlook',%s)",
                          (uid, owner or self.operator, email or uid.hex + '@fixture.invalid', secret))
        return uid

    def account(self, mailbox=None):
        uid = self.uuid()
        self.conn.execute("INSERT INTO mailbox_platform_states(id,mailbox_id,platform) VALUES(%s,%s,'google')",
                          (uid, mailbox or self.mailbox()))
        return uid

    def billing(self):
        uid = self.uuid()
        self.conn.execute("INSERT INTO billing_identities(id,owner_operator_id,holder_secret_ref,address_secret_ref,country) "
                          "VALUES(%s,%s,%s,%s,'US')", (uid, self.operator, self.secret('billing_holder'), self.secret('billing_address')))
        return uid

    def card(self, *, limit=2, fingerprint=None, owner=None):
        uid = self.uuid()
        fingerprint = fingerprint or hashlib.sha256(uid.bytes).hexdigest()
        self.conn.execute("INSERT INTO payment_cards(id,owner_operator_id,alias,brand,last4,expiry_ref,pan_secret_ref,billing_identity_ref,pan_fingerprint,account_limit) "
                          "VALUES(%s,%s,'fixture-card','visa','1111',%s,%s,%s,%s,%s)",
                          (uid, owner or self.operator, self.secret('card_expiry'), self.secret('pan'), self.billing(), fingerprint, limit))
        return uid

    def reservation(self, card=None, account=None, phase='NOT_SENT'):
        card, account, uid, receipt = card or self.card(), account or self.account(), self.uuid(), self.uuid()
        self.conn.execute("INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) "
                          "VALUES(%s,%s,'fixture.bind','fixture:card',%s,%s,'NOT_SENT',1,1)",
                          (receipt, self.task, uid.hex, 'a' * 64))
        self.conn.execute("INSERT INTO card_reservations(id,card_id,task_id,account_ref,phase,operation_id,card_revision,billing_revision,pan_secret_ref,expiry_secret_ref,billing_identity_ref,holder_secret_ref,address_secret_ref) "
                          "SELECT %s,c.id,%s,%s,%s,%s,c.revision,b.revision,c.pan_secret_ref,c.expiry_ref,b.id,b.holder_secret_ref,b.address_secret_ref "
                          "FROM payment_cards c JOIN billing_identities b ON b.id=c.billing_identity_ref WHERE c.id=%s",
                          (uid, self.task, account, phase, receipt, card))
        return uid, card, account

    def link(self, reservation, card, account):
        self.conn.execute("INSERT INTO card_account_links(id,card_id,account_ref,reservation_id,billing_resource_ref,evidence_ref,confirmed_at) "
                          "VALUES(%s,%s,%s,%s,'fixture:billing','fixture:success',clock_timestamp())",
                          (self.uuid(), card, account, reservation))

    def counts(self, card):
        return self.conn.execute('SELECT linked_count,reserved_count FROM payment_cards WHERE id=%s', (card,)).fetchone()

    def reject(self, statement, params=(), exception=None):
        import psycopg
        with self.assertRaises(exception or psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.conn.execute(statement, params)

    def test_exact_six_new_tables_and_safety_indexes(self):
        from test_onboarding_migrations import EXPECTED_TABLES
        self.assertEqual(self.fixture.table_names() - EXPECTED_TABLES - {'_test_marker'}, {
            'mailbox_registry', 'mailbox_platform_states', 'billing_identities',
            'payment_cards', 'card_reservations', 'card_account_links'})
        indexes = {r[0] for r in self.conn.execute('SELECT indexname FROM pg_indexes WHERE schemaname=%s',
                                                 (self.fixture.schema,))}
        self.assertTrue({'mailboxes_owner_filters', 'platform_filter', 'cards_candidates',
                         'card_one_open_binding', 'reservations_task', 'links_account',
                         'mailbox_active_tasks', 'admin_receipt_request'} <= indexes)
        self.assertNotIn('mailbox_one_active_task', indexes)

    def test_email_unique_normalized_and_history_cannot_be_reset(self):
        import psycopg
        mailbox = self.mailbox('one@fixture.invalid')
        with self.assertRaises(psycopg.errors.UniqueViolation), self.conn.transaction():
            self.mailbox('one@fixture.invalid', owner=self.operator_row())
        self.reject('UPDATE mailbox_registry SET email_norm=%s WHERE id=%s', ('UPPER@fixture.invalid', mailbox))
        self.conn.execute("UPDATE mailbox_registry SET ever_registration_attempted=true WHERE id=%s", (mailbox,))
        self.assertEqual(self.conn.execute('SELECT sale_eligibility FROM mailbox_registry WHERE id=%s', (mailbox,)).fetchone(), ('INELIGIBLE',))
        self.reject('UPDATE mailbox_registry SET ever_registration_attempted=false WHERE id=%s', (mailbox,))
        self.reject("UPDATE mailbox_registry SET sale_eligibility='ELIGIBLE' WHERE id=%s", (mailbox,))

    def test_platform_identity_unique_and_references_exist(self):
        import psycopg
        mailbox = self.mailbox()
        self.account(mailbox)
        with self.assertRaises(psycopg.errors.UniqueViolation), self.conn.transaction():
            self.account(mailbox)
        self.reject("INSERT INTO mailbox_platform_states(id,mailbox_id,platform) VALUES(%s,%s,'unknown')", (self.uuid(), mailbox))
        self.reject("INSERT INTO mailbox_platform_states(id,mailbox_id,platform) VALUES(%s,%s,'google')", (self.uuid(), self.uuid()), psycopg.errors.ForeignKeyViolation)

    def test_pan_identity_is_global_and_cannot_be_moved_or_replaced(self):
        import psycopg
        fingerprint = 'b' * 64
        card = self.card(fingerprint=fingerprint)
        with self.assertRaises(psycopg.errors.UniqueViolation), self.conn.transaction():
            self.card(fingerprint=fingerprint, owner=self.operator_row())
        for column, value in (('pan_fingerprint', 'c' * 64), ('pan_secret_ref', self.secret('pan')),
                              ('owner_operator_id', self.operator_row())):
            self.reject(f'UPDATE payment_cards SET {column}=%s WHERE id=%s', (value, card))
        self.assertEqual(self.counts(card), (0, 0))

    def test_one_open_binding_and_capacity_projection(self):
        import psycopg
        reservation, card, account = self.reservation(card=self.card(limit=1))
        self.assertEqual(self.counts(card), (0, 1))
        with self.assertRaises(psycopg.errors.UniqueViolation), self.conn.transaction():
            self.reservation(card=card)
        self.conn.execute("UPDATE card_reservations SET phase='UNKNOWN' WHERE id=%s", (reservation,))
        self.assertEqual(self.counts(card), (0, 1))
        self.conn.execute("UPDATE card_reservations SET phase='SUCCEEDED' WHERE id=%s", (reservation,))
        self.link(reservation, card, account)
        self.assertEqual(self.counts(card), (1, 0))
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.reservation(card=card)
        self.assertEqual(self.counts(card), (1, 0))

    def test_duplicate_success_is_unique_and_link_cannot_reference_other_identity(self):
        import psycopg
        reservation, card, account = self.reservation(phase='SUCCEEDED')
        self.link(reservation, card, account)
        with self.assertRaises(psycopg.errors.UniqueViolation), self.conn.transaction():
            self.link(reservation, card, account)
        self.assertEqual(self.counts(card), (1, 0))
        other, _, other_account = self.reservation(card=card, phase='SUCCEEDED')
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.link(other, self.card(), account)
        self.assertNotEqual(other_account, account)

    def test_failed_reservation_late_success_preserves_both_facts(self):
        reservation, card, account = self.reservation(card=self.card(limit=1))
        self.conn.execute("UPDATE card_reservations SET phase='FAILED_CONFIRMED' WHERE id=%s", (reservation,))
        newer, _, _ = self.reservation(card=card)
        self.conn.execute('UPDATE payment_cards SET reconciliation_required=true WHERE id=%s', (card,))
        self.conn.execute('UPDATE card_reservations SET conflict_detected=true WHERE id=%s', (reservation,))
        self.link(reservation, card, account)
        self.assertEqual(self.counts(card), (1, 1))
        self.assertEqual(self.conn.execute('SELECT phase FROM card_reservations WHERE id=%s', (newer,)).fetchone(), ('NOT_SENT',))
        self.reject('UPDATE payment_cards SET reconciliation_required=false WHERE id=%s', (card,))

    def test_reservation_identity_and_historical_secret_snapshot_are_immutable(self):
        reservation, card, _ = self.reservation()
        other_card, other_account = self.card(), self.account()
        for column, value in (('card_id', other_card), ('account_ref', other_account),
                              ('task_id', self.uuid()), ('operation_id', self.uuid()),
                              ('card_revision', 2), ('billing_revision', 2),
                              ('pan_secret_ref', self.secret('pan')), ('expiry_secret_ref', self.secret('card_expiry')),
                              ('billing_identity_ref', self.billing()), ('holder_secret_ref', self.secret('billing_holder')),
                              ('address_secret_ref', self.secret('billing_address'))):
            self.reject(f'UPDATE card_reservations SET {column}=%s WHERE id=%s', (value, reservation))
        before = self.conn.execute('SELECT expiry_secret_ref,billing_identity_ref FROM card_reservations WHERE id=%s', (reservation,)).fetchone()
        self.conn.execute('UPDATE payment_cards SET expiry_ref=%s,billing_identity_ref=%s,revision=revision+1 WHERE id=%s',
                          (self.secret('card_expiry'), self.billing(), card))
        self.assertEqual(self.conn.execute('SELECT expiry_secret_ref,billing_identity_ref FROM card_reservations WHERE id=%s', (reservation,)).fetchone(), before)
        self.assertEqual(self.counts(other_card), (0, 0))

    def test_reservation_delete_projection_supports_migrator_cleanup_only(self):
        reservation, card, _ = self.reservation()
        self.conn.execute('DELETE FROM card_reservations WHERE id=%s', (reservation,))
        self.assertEqual(self.counts(card), (0, 0))

    def test_task_fixture_default_and_pool_dual_pins_are_checked(self):
        import json
        mailbox = self.mailbox()
        account = self.account(mailbox)
        secret = self.conn.execute('SELECT credential_ref FROM mailbox_registry WHERE id=%s', (mailbox,)).fetchone()[0]
        self.assertEqual(self.conn.execute('SELECT execution_scope,platform_plan,platform_credential_pins FROM onboarding_tasks WHERE id=%s', (self.task,)).fetchone(), ('fixture', [], {}))
        self.reject("UPDATE onboarding_tasks SET execution_scope='pool' WHERE id=%s", (self.task,))
        pins = {'google': {'state_id': str(account), 'secret_ref': None, 'revision': 1, 'identity_status': 'UNKNOWN'}}
        self.conn.execute("UPDATE onboarding_tasks SET execution_scope='pool',mailbox_id=%s,platform='google',platform_plan='[\"google\"]',credential_version=1,mailbox_credential_ref=%s,platform_credential_pins=%s::jsonb WHERE id=%s", (mailbox, secret, json.dumps(pins), self.task))
        for changes in ("platform_plan='[]'", "platform_plan='[\"google\",\"google\"]'", "platform_credential_pins='{}'", "platform_credential_pins='[]'", "platform_credential_pins='null'"):
            self.reject('UPDATE onboarding_tasks SET ' + changes + ' WHERE id=%s', (self.task,))
        for value in (True, 0, -1, 1.5, '1', None):
            pins['google']['revision'] = value
            self.reject('UPDATE onboarding_tasks SET platform_credential_pins=%s::jsonb WHERE id=%s', (json.dumps(pins), self.task))

    def test_combined_plan_is_bounded_unique_and_never_google(self):
        for platform, plan, expected in (('google', ['google'], True), ('combined', ['claude', 'grok'], True),
                ('combined', ['google', 'grok'], False), ('combined', ['grok', 'grok'], False),
                ('combined', ['grok'], False), ('combined', [True, 'grok'], False),
                ('combined', {}, False), ('combined', None, False), ('unknown', ['unknown'], False)):
            import json
            with self.subTest(plan=plan):
                self.assertIs(self.conn.execute('SELECT valid_platform_plan(%s,%s::jsonb)', (platform, json.dumps(plan))).fetchone()[0], expected)

    def test_admin_receipts_are_operator_scoped_terminal_and_idempotent(self):
        import psycopg
        uid = self.uuid()
        statement = "INSERT INTO operation_receipts(id,scope_operator_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) VALUES(%s,%s,'mailbox.import','fixture:admin','fixture:key',%s,%s,0,1)"
        self.conn.execute(statement, (uid, self.operator, 'a' * 64, 'SUCCEEDED'))
        self.reject(statement, (self.uuid(), self.operator, 'a' * 64, 'UNKNOWN'))
        self.reject(statement, (self.uuid(), self.operator, 'a' * 64, 'SUCCEEDED'), psycopg.errors.UniqueViolation)
        self.reject('UPDATE operation_receipts SET task_id=%s WHERE id=%s', (self.task, uid))
        self.reject('UPDATE operation_receipts SET scope_operator_id=NULL WHERE id=%s', (uid,))

    def test_only_reviewed_secret_kinds_and_no_cvv_otp(self):
        import psycopg
        for kind in ('mailbox_credential', 'platform_credential', 'billing_holder', 'billing_address', 'card_expiry', 'fixture', 'pan'):
            self.secret(kind)
        for kind in ('cvv', 'otp', 'password_dump'):
            with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
                self.secret(kind)

    def test_app_can_run_checks_and_projection_triggers_without_direct_trigger_execute(self):
        import json
        card = self.card(limit=1)
        # Only synthetic base refs are committed so a separate app connection
        # can see them. The actual app mutation transaction is rolled back.
        self.conn.commit()
        migrator = self.conn
        with self.fixture.app() as app, app.transaction(force_rollback=True):
            self.conn = app
            try:
                reservation, _, account = self.reservation(card=card)
                self.assertEqual(self.counts(card), (0, 1))
                mailbox, credential = app.execute(
                    'SELECT m.id,m.credential_ref FROM mailbox_registry m '
                    'JOIN mailbox_platform_states p ON p.mailbox_id=m.id WHERE p.id=%s',
                    (account,)).fetchone()
                pins = {'google': {'state_id': str(account), 'secret_ref': None,
                                   'revision': 1, 'identity_status': 'UNKNOWN'}}
                app.execute("UPDATE onboarding_tasks SET execution_scope='pool',mailbox_id=%s,"
                            "platform='google',platform_plan='[\"google\"]',credential_version=1,"
                            "mailbox_credential_ref=%s,platform_credential_pins=%s::jsonb WHERE id=%s",
                            (mailbox, credential, json.dumps(pins), self.task))
                app.execute('UPDATE mailbox_registry SET ever_registration_attempted=true WHERE id=%s', (mailbox,))
                self.assertEqual(app.execute('SELECT sale_eligibility FROM mailbox_registry WHERE id=%s', (mailbox,)).fetchone(), ('INELIGIBLE',))
                app.execute("UPDATE card_reservations SET phase='SUCCEEDED' WHERE id=%s", (reservation,))
                self.link(reservation, card, account)
                self.assertEqual(self.counts(card), (1, 0))
                self.assertFalse(app.execute("SELECT has_function_privilege(current_user,'reservation_count_projection()','EXECUTE')").fetchone()[0])
            finally:
                self.conn = migrator
        self.assertEqual(self.counts(card), (0, 0))

    def test_app_cannot_delete_or_truncate_not_committed_proof_tables(self):
        # Database-enforced premises of the not-committed proof: rows behind it are never removed,
        # and global_configs is append-only for the application role.
        with self.fixture.app() as app:
            for table in ('global_configs', 'mailbox_registry', 'operation_receipts', 'onboarding_tasks'):
                for action in ('DELETE', 'TRUNCATE'):
                    self.assertFalse(app.execute('SELECT has_table_privilege(current_user,%s,%s)', (table, action)).fetchone()[0],
                                     table + ' ' + action)
            self.assertFalse(app.execute("SELECT has_table_privilege(current_user,'global_configs','UPDATE')").fetchone()[0])
            # A column-level grant would also break append-only while has_table_privilege stays false.
            self.assertFalse(app.execute("SELECT has_any_column_privilege(current_user,'global_configs','UPDATE')").fetchone()[0])

    def test_app_grants_keep_history_append_only_and_functions_narrow(self):
        import psycopg
        with self.fixture.app() as app:
            for table in ('mailbox_registry', 'mailbox_platform_states', 'payment_cards', 'card_reservations'):
                for action in ('SELECT', 'INSERT', 'UPDATE'):
                    self.assertTrue(app.execute('SELECT has_table_privilege(current_user,%s,%s)', (table, action)).fetchone()[0])
                self.assertFalse(app.execute('SELECT has_table_privilege(current_user,%s,\'DELETE\')', (table,)).fetchone()[0])
            for table in ('billing_identities', 'card_account_links'):
                for action in ('SELECT', 'INSERT'):
                    self.assertTrue(app.execute('SELECT has_table_privilege(current_user,%s,%s)', (table, action)).fetchone()[0])
                for action in ('UPDATE', 'DELETE'):
                    self.assertFalse(app.execute('SELECT has_table_privilege(current_user,%s,%s)', (table, action)).fetchone()[0])
            for statement in ('UPDATE billing_identities SET revision=revision+1 WHERE false',
                              'DELETE FROM card_reservations WHERE false',
                              'UPDATE card_account_links SET version=version+1 WHERE false',
                              'ALTER TABLE payment_cards ADD COLUMN forbidden text'):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    app.execute(statement)
            self.assertTrue(app.execute("SELECT has_function_privilege(current_user,'valid_platform_plan(text,jsonb)','EXECUTE')").fetchone()[0])
            self.assertTrue(app.execute("SELECT has_function_privilege(current_user,'valid_platform_credential_pins(jsonb,jsonb)','EXECUTE')").fetchone()[0])
            self.assertFalse(app.execute("SELECT has_function_privilege(current_user,'card_identity_immutable()','EXECUTE')").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
