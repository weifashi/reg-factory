"""Disposable formal live setup with real source/import/admission/create services.

Only the Google auth filesystem/token boundary and Billing HTTP are offline
fixtures. No DB service/proof parser is mocked, no task is relabelled, and no
real credential/profile or provider is accessed. Call populate after PoolCase
setup; create=False leaves an imported, unclaimed source for entry tests.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import httpx
from onboarding import live_admission, live_batches, live_config, live_input_vault
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.live_mailbox_source import OwnedMailboxSource
from onboarding.migrate import apply_all
from test_onboarding_live_config import FIELDS


def _run(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    # Existing async executor/completion tests sometimes need a new batch from
    # their synchronous fixture helper. Own/join the separate event-loop thread.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(asyncio.run, awaitable).result()


def admit(case, config=None):
    config = case.cfg if config is None else config
    def billing(request):
        case.assertEqual(request.method, 'GET')
        case.assertEqual(request.url.host, 'cloudbilling.googleapis.com')
        case.assertEqual(request.url.path, '/v1/billingAccounts/' + FIELDS['billing_account'])
        return httpx.Response(200, json={'name':'billingAccounts/' + FIELDS['billing_account'], 'open':True})
    with patch('onboarding.google_auth_runtime._verify_paths'), \
         patch('onboarding.live_admission.GoogleTokenProvider.__call__',
               new=AsyncMock(return_value='unit-test-authorized-token')):
        return _run(live_admission.prepare(case.settings, case.actor, case.source, case.resource['id'],
            config['revision'], policy=case.live_policy, mac=case.mac,
            google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
            google_transport=httpx.MockTransport(billing)))


def create_batch(case, config, seeds, *, request_key='runtime:create'):
    case.assertEqual([seed['id'] for seed in seeds], [case.resource['id']])
    admission = admit(case, config)
    return live_batches.create_one(case.settings, case.actor, case.resource['id'], config['revision'],
        request_key, policy=case.live_policy, mac=case.mac, admission=admission)


def populate(case, *, create=True):
    with case.fixture.migrator() as conn:
        apply_all(conn, case.fixture.schema, target_version=4)
    case.live_policy = live_input_vault.LivePoolPolicy(case.settings)
    case.live_vault = live_input_vault.LiveInputVault(case.keyring, case.live_policy)
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    root = Path(directory.name); root.chmod(0o700)
    path = root / 'emails.txt'
    path.write_text('weifashi68@gmail.com----unit-test-owned-password\n')
    path.chmod(0o600)
    case.source = case.live_source = OwnedMailboxSource(root, case.actor.operator_id, 'weifashi68@gmail.com')
    preview = live_input_vault.preview_source(case.settings, case.actor, case.source, 'owned',
                                            vault=case.live_vault, mac=case.mac)
    case.source_digest = preview['source_digest']
    imported = live_input_vault.import_source(case.settings, case.actor, case.source, 'owned',
        case.source_digest, 'runtime:mailbox', vault=case.live_vault, mac=case.mac)
    case.cfg = live_config.replace(case.settings, case.actor, None, FIELDS, 'runtime:config',
                                   policy=case.live_policy, mac=case.mac)
    mid = imported['mailbox_id']
    with case.uow() as conn:
        secret = str(conn.execute('SELECT credential_ref FROM mailbox_registry WHERE id=%s', (mid,)).fetchone()[0])
        sid = str(conn.execute("SELECT id FROM mailbox_platform_states WHERE mailbox_id=%s AND platform='google'",
                               (mid,)).fetchone()[0])
    case.resource = {'id':mid, 'state_id':sid, 'secret_id':secret}
    if create:
        case.batch = create_batch(case, case.cfg, [case.resource])
        case.tid = case.batch['task_ids'][0]
        case.plan = live_config.plan_for(FIELDS, case.actor.operator_id, case.tid)


seed_live_runtime = populate
