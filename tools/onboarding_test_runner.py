"""Explicit isolated P1b suites. A skipped or failed test is never acceptance.

No real env, cloud/provider operation, startup migration or legacy service launch.
Run using .venv-onboarding/bin/python; browser rendering is a separate check.
"""
from contextlib import contextmanager, ExitStack
import argparse
import datetime
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
DB = ('domain','allocation','gates','contracts','settings','storage','migrations',
      'test_db','repository','leases','receipts','approvals','coordinator','b1_faults','fixture')
SECURITY = ('security','keyring','secrets','downloads','b2_bridge')
WEB = ('route_guard','routes','legacy_security','lifecycle','pages','test_runner',
       'pool_api','pool_ui')
OFFLINE = ('dependencies', 'migration_catalog', 'mailbox_parser', 'pool_secret_types', 'request_mac', 'mailbox_cursor', 'pool_contracts', 'not_committed_guard')
POOLS = ('migration_sequence', 'pool_schema', 'pool_vault', 'mailbox_import', 'mailbox_import_races', 'mailbox_reads', 'mailbox_update', 'mailbox_update_races', 'pool_config', 'pool_config_races', 'pool_repository', 'pool_leases', 'pool_commands', 'pool_batches', 'pool_preflight', 'task_read')
LEGACY = ('test_webui_env_reload','test_webui_update','test_webui_tour','test_account_records')


def selected_modules(suite):
    groups = {'db': DB, 'security': SECURITY, 'web': WEB,
              'offline': OFFLINE, 'pools': POOLS, 'all': DB + SECURITY + WEB + OFFLINE + POOLS}
    if suite not in groups:
        raise ValueError('unknown suite')
    return ['test_onboarding_' + name for name in groups[suite]]


def passed(result):
    return result.wasSuccessful() and not result.skipped


@contextmanager
def legacy_environment():
    """Only the old regression import is mocked, not its business assertions."""
    with tempfile.TemporaryDirectory(prefix='rf-b3-regression-') as directory, ExitStack() as stack:
        envfile = Path(directory) / '.env'
        envfile.write_text('K12_AUTO_START=0\nPROXY_MODE=direct\n')
        env = {'PATH': os.defpath, 'HOME': directory, 'ONBOARDING_MODE': 'off',
               'REG_FACTORY_ENV_FILE': str(envfile), 'REG_FACTORY_DATA_DIR': directory}
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        # Existing modules may have been imported by isolated lifecycle tests.
        # Reload the real config strictly from this temporary non-secret file.
        config = importlib.import_module('config')
        importlib.reload(config)
        from common import proxy_switch
        with patch.object(proxy_switch, 'effective_proxy_url', return_value=''), \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='fixture')):
            server = importlib.import_module('webui.server')
            importlib.reload(server)
        # Unexpected old provider I/O is a failure, not an accidental live check.
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'urllib.request.urlopen',
                     'requests.sessions.Session.request', 'subprocess.Popen', 'subprocess.run'):
            stack.enter_context(patch(name, side_effect=AssertionError('legacy external I/O forbidden')))
        yield


@contextmanager
def legacy_case_environment(test_id):
    """Scope platform/active-mock fixtures to the named old regression only."""
    windows_cases = {
        'test_webui_update.WebUIUpdateTests.test_frozen_runtime_uses_portable_updater',
        'test_webui_update.WebUIUpdateTests.test_starts_detached_windows_updater',
    }
    proxy_case = 'test_webui_env_reload.WebUIEnvReloadTests.test_proxy_save_persists_protocol_link_and_payment_egress'
    from webui import server
    with ExitStack() as stack:
        if test_id in windows_cases:
            # Only server's view is Windows; pathlib/tempfile still use the host OS.
            windows_os = SimpleNamespace(**{**vars(os), 'name': 'nt'})
            stack.enter_context(patch.object(server, 'os', windows_os))
            for key, value in {'CREATE_NO_WINDOW': 0x08000000,
                               'CREATE_NEW_PROCESS_GROUP': 0x00000200,
                               'DETACHED_PROCESS': 0x00000008}.items():
                stack.enter_context(patch.object(server.subprocess, key, value, create=True))
        if test_id == proxy_case:
            reload_module = importlib.reload
            def preserve_active_proxy_mock(module):
                active = getattr(module, 'ensure_proxy_mode', None)
                preserve = module.__name__ == 'common.proxy_switch' and isinstance(active, Mock)
                result = reload_module(module)
                # The original test installs this exact fixture. Reload may reset
                # module globals, but must not accidentally enable real provider I/O.
                if preserve:
                    result.ensure_proxy_mode = active
                return result
            stack.enter_context(patch.object(importlib, 'reload', side_effect=preserve_active_proxy_mock))
        yield


class LegacyTestSuite(unittest.TestSuite):
    """Run old assertions unchanged with per-case synthetic host fixtures."""
    def run(self, result, debug=False):
        for test in self:
            if result.shouldStop:
                break
            if isinstance(test, unittest.TestSuite):
                LegacyTestSuite(test).run(result, debug=debug)
            else:
                with legacy_case_environment(test.id()):
                    if debug:
                        test.debug()
                    else:
                        test(result)
        return result


def _names(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _names(test)
        else:
            yield test.id()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('db','security','web','offline','pools','all'), required=True)
    parser.add_argument('--require-no-skips', action='store_true', help='Always enforced; explicit for audit')
    parser.add_argument('--legacy', action='store_true', help='Also run four named old regression modules')
    parser.add_argument('--json-result', type=Path)
    args = parser.parse_args(argv)
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
    start = time.monotonic()
    names, results = [], []

    def run(modules, *, legacy=False):
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        if legacy:
            suite = LegacyTestSuite(suite)
        names.extend(_names(suite))
        results.append(unittest.TextTestRunner(verbosity=2).run(suite))

    run(selected_modules(args.suite))
    if args.legacy:
        with legacy_environment():
            run(LEGACY, legacy=True)
    data = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'duration_seconds': round(time.monotonic()-start, 3),
            'tests': sum(r.testsRun for r in results),
            'failures': sum(len(r.failures) for r in results),
            'errors': sum(len(r.errors) for r in results),
            'skipped': sum(len(r.skipped) for r in results),
            'success': all(passed(r) for r in results), 'test_ids': names}
    if args.json_result:
        # The caller chooses only an evidence file; no credentials are recorded.
        args.json_result.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps({key:value for key,value in data.items() if key != 'test_ids'}))
    return 0 if data['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
