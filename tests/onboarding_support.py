"""B0-only ephemeral schemas. No legacy configuration import or silent skips."""
import json
from dataclasses import replace
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

BASE = Path('/workspace/reg-factory/.local/onboarding-p1b')


class SchemaFixture:
    def __init__(self):
        from onboarding.settings import load_settings
        self.config = load_settings(BASE / 'migrator.json', role='migrator')
        self.app_config = load_settings(BASE / 'app.json', role='app')
        self.token = uuid.uuid4().hex
        self.schema = 'rf_p1b_test_' + self.token
        self.manifest = BASE / (self.schema + '.json')
        manifest = {'instance_marker': self.config.instance_marker, 'schema': self.schema,
                    'schema_token': self.token, 'owner': 'rf_onboarding_migrator'}
        fd = os.open(self.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(manifest, f)
        with self._connect(self.config) as conn, conn.transaction():
            conn.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION rf_onboarding_migrator').format(
                sql.Identifier(self.schema)))
            conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE TABLE {}._test_marker '
                                 '(instance_marker text NOT NULL, schema_token text NOT NULL)').format(
                                     sql.Identifier(self.schema)))
            conn.execute(sql.SQL('INSERT INTO {}._test_marker VALUES (%s,%s)').format(
                sql.Identifier(self.schema)), (self.config.instance_marker, self.token))

    def _connect(self, config):
        from onboarding.storage import open_app, open_migrator
        settings = replace(config, schema=self.schema)
        connector = open_app if settings.user == 'rf_onboarding_app' else open_migrator
        return connector(settings)

    def migrator(self):
        return self._connect(self.config)

    def app(self):
        return self._connect(self.app_config)

    def table_names(self):
        with self.migrator() as conn:
            return {row[0] for row in conn.execute(
                'SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=%s',
                (self.schema,)).fetchall()}

    def close(self):
        # Only our manifest-scoped test schema is eligible; never production schema.
        from tools.onboarding_test_db import cleanup_schema
        cleanup_schema(self.schema, self.manifest)
        self.manifest.unlink(missing_ok=True)
