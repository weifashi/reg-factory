"""Synthetic B1 test records in a manifest-scoped ephemeral PostgreSQL schema."""
from dataclasses import replace
import unittest
import uuid
from psycopg.types.json import Jsonb
from onboarding_support import SchemaFixture
from onboarding.migrate import apply_migration
from onboarding.repository import FixtureActor
from onboarding.storage import unit_of_work, open_app

PERMISSIONS = ['tasks:manage', 'onboarding:read', 'config:manage', 'fees:approve',
               'scheduling:enable', 'keys:download']
CONFIG = {'model': 'fixture-model', 'region': 'fixture-region',
          'instance_ref': 'fixture:sub2api', 'group_ref': 'fixture:group',
          'project_prefix': 'fixture-project'}

class B1Case(unittest.TestCase):
    def setUp(self):
        self.fixture = SchemaFixture()
        self.addCleanup(self.fixture.close)
        with self.fixture.migrator() as conn:
            apply_migration(conn, self.fixture.schema)
        self.settings = replace(self.fixture.app_config, schema=self.fixture.schema)
        self.actor = FixtureActor(str(uuid.uuid4()))
        self.config_id = str(uuid.uuid4())
        self.config_revision = 'fixture-' + uuid.uuid4().hex
        with self.uow() as conn:
            conn.execute('INSERT INTO operators (id, username_norm, password_hash, permissions) '
                         'VALUES (%s,%s,%s,%s)',
                         (self.actor.operator_id, 'fixture-' + uuid.uuid4().hex, 'fixture-only', PERMISSIONS))
            conn.execute('INSERT INTO global_configs (id, revision, nonsecret_config, changed_by) '
                         'VALUES (%s,%s,%s,%s)',
                         (self.config_id, self.config_revision, Jsonb(CONFIG), self.actor.operator_id))

    def task(self, mailbox_ref=None):
        task_id, batch_id = str(uuid.uuid4()), str(uuid.uuid4())
        mailbox_ref = mailbox_ref or 'fixture:' + uuid.uuid4().hex
        with self.uow() as conn:
            conn.execute('INSERT INTO onboarding_batches '
                         '(id, selection_mode, requested_count, selected_mailbox_refs, config_id, created_by) '
                         "VALUES (%s,'specified',1,%s,%s,%s)",
                         (batch_id, Jsonb([mailbox_ref]), self.config_id, self.actor.operator_id))
            cur = conn.execute('INSERT INTO onboarding_tasks (id,batch_id,mailbox_ref,config_id) '
                               'VALUES (%s,%s,%s,%s) RETURNING *',
                               (task_id, batch_id, mailbox_ref, self.config_id))
            task = dict(zip([col.name for col in cur.description], cur.fetchone()))
        for name in ('id', 'batch_id', 'config_id'):
            task[name] = str(task[name])
        task['config_revision'] = self.config_revision
        return task

    def uow(self):
        return unit_of_work(self.settings)

    def read(self, sql, params=()):
        with open_app(self.settings) as conn:
            return conn.execute(sql, params).fetchall()
