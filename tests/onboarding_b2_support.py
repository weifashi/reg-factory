"""Trusted fixture identities for B2; never a public account/session endpoint."""
import hashlib
import os
import secrets
import shutil
import uuid

from onboarding_b1_support import B1Case, PERMISSIONS
from onboarding import security
from onboarding.settings import BASE

class B2Case(B1Case):
    def setUp(self):
        super().setUp()
        self.fixture_actor = self.actor
        self.token, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.session_id = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions '
                         '(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES (%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',"
                         "clock_timestamp()+interval '30 minutes')",
                         (self.session_id, self.actor.operator_id,
                          hashlib.sha256(self.token.encode()).hexdigest(),
                          hashlib.sha256(self.csrf.encode()).hexdigest()))
        self.actor = security.Actor(self.fixture_actor.operator_id, frozenset(PERMISSIONS),
                                    self.session_id, 1)

    def make_store(self):
        from onboarding.keyring import Keyring
        from onboarding.secret_store import SecretStore
        parent = BASE / 'keyrings'
        parent.mkdir(mode=0o700, exist_ok=True)
        directory = parent / ('fixture-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, directory)
        fd = os.open(directory / 'v1.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
        self.key_directory = directory
        return SecretStore(Keyring(directory, 'v1'))
