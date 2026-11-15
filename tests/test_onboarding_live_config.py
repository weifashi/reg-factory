"""Closed live global-config contract; no database, files, or provider calls."""
from datetime import datetime, timezone
import importlib
import importlib.util
import re
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from onboarding.errors import ErrorCode, ServiceError

FIELDS = dict(schema_version=2, workflow='existing_billing_cloud_v1',
              billing_account='AAAAAA-BBBBBB-CCCCCC', region='us-central1',
              project_prefix='rf-owned', service_account_prefix='rf-vertex',
              approved_role='roles/aiplatform.user',
              google_auth_profile_ref='local-authorized-google',
              instance_ref='local-sub2api-approved', group_ref='2',
              timeout_seconds=180, concurrency=1, scheduling_enabled=False,
              model_verification_enabled=False)
LEGACY = dict(model='fixture-gemini', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)


def module(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.live_config'),
                         'missing approved v2 global configuration service')
    return importlib.import_module('onboarding.live_config')


class LiveConfigOfflineTests(unittest.TestCase):
    def setUp(self):
        self.m = module(self)

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('canary', str(caught.exception))

    def test_validated_copy_preserves_exact_complete_shape(self):
        source = FIELDS.copy()
        result = self.m._fields(source)
        self.assertEqual(result, FIELDS)
        self.assertIsNot(result, source)
        source['billing_account'] = 'DDDDDD-EEEEEE-FFFFFF'
        self.assertEqual(result, FIELDS)

    def test_missing_unknown_secret_and_runtime_overrides_rejected(self):
        for name in FIELDS:
            body = FIELDS.copy(); body.pop(name)
            with self.subTest(missing=name), self.assertRaises(ValueError):
                self.m._fields(body)
        for name in ('password', 'access_token', 'private_key', 'auth_file', 'url',
                     'endpoint', 'parent', 'model', 'retention_days', 'transport'):
            with self.subTest(extra=name), self.assertRaises(ValueError):
                self.m._fields({**FIELDS, name: 'private-canary'})
        for body in (None, [], LEGACY, {'nonsecret_config': FIELDS}):
            with self.assertRaises(ValueError): self.m._fields(body)

    def test_only_closed_workflow_role_region_and_registered_references(self):
        cases = {'workflow': ['first_trial', 'existing_billing_cloud_v2'],
                 'region': ['europe-west1', 'fixture-region'],
                 'approved_role': ['roles/owner', 'roles/editor'],
                 'google_auth_profile_ref': ['arbitrary-profile', '/private-canary', 'https://evil.invalid'],
                 'instance_ref': ['fixture:sub2api', 'https://evil.invalid'],
                 'group_ref': ['1', 2, '02']}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.m._fields({**FIELDS, name: value})

    def test_no_bool_int_coercion_subclasses_or_unsupported_values(self):
        class Text(str): pass
        class Number(int): pass
        class Mapping(dict): pass
        for name, value in FIELDS.items():
            invalid = [None, [], {}, 0.0]
            invalid += ([Text(value)] if type(value) is str else
                        [0, 1, 'false'] if type(value) is bool else
                        [True, False, str(value), float(value), Number(value)])
            for bad in invalid:
                with self.subTest(name=name, shape=type(bad).__name__), self.assertRaises(ValueError):
                    self.m._fields({**FIELDS, name: bad})
        for name, value in (('schema_version', 1), ('schema_version', 3),
                            ('timeout_seconds', 181), ('concurrency', 2),
                            ('scheduling_enabled', True), ('model_verification_enabled', True)):
            with self.assertRaises(ValueError): self.m._fields({**FIELDS, name: value})
        with self.assertRaises(ValueError): self.m._fields(Mapping(FIELDS))

    def test_legal_billing_id_and_prefix_limits(self):
        for value in ('ABC123-456DEF-000000', '000000-000000-000000'):
            self.assertEqual(self.m._fields({**FIELDS, 'billing_account': value})['billing_account'], value)
        for value in ('aaaaaa-bbbbbb-cccccc', 'billingAccounts/AAAAAA-BBBBBB-CCCCCC',
                      'AAAAAA-BBBBBB-CCCCCC\n', 'AAAAAA-BBBBBB', 'private-canary'):
            with self.assertRaises(ValueError): self.m._fields({**FIELDS, 'billing_account': value})
        for name in ('project_prefix', 'service_account_prefix'):
            for value in ('a', 'rf', 'a-b', 'a' * 12):
                self.assertEqual(self.m._fields({**FIELDS, name: value})[name], value)
            for value in ('', 'a' * 13, '1rf', '-rf', 'rf-', 'RF', 'a_b', 'a\nb', 'a\n', '中文'):
                with self.assertRaises(ValueError): self.m._fields({**FIELDS, name: value})

    def test_request_inputs_reject_invalid_revision_key_without_echo(self):
        for expected in (True, 1, 'pool-bad', 'pool-' + uuid4().hex, str(uuid4())):
            self.reject(lambda: self.m._inputs(expected, FIELDS, 'request'))
        for key in (None, True, '', ' ', 'x' * 129, 'private-canary\n'):
            self.reject(lambda: self.m._inputs(None, FIELDS, key))
        self.assertEqual(self.m._inputs('pool-' + str(uuid4()), FIELDS, 'live:request-1'), FIELDS)

    def test_shared_projection_reads_both_versions_but_legacy_write_stays_closed(self):
        from onboarding import pool_config
        for fields in (LEGACY, FIELDS):
            row = (uuid4(), 'pool-' + str(uuid4()), fields, {}, uuid4(),
                   datetime.now(timezone.utc), 'pool')
            self.assertEqual(pool_config._project(row)['nonsecret_config'], fields)
        with self.assertRaises(ValueError): pool_config._fields(FIELDS)
        for value in (1, True, 2.0, 3, '2'):
            row = (uuid4(), 'pool-' + str(uuid4()), {**FIELDS, 'schema_version': value},
                   {}, uuid4(), datetime.now(timezone.utc), 'pool')
            self.reject(lambda: pool_config._project(row), ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_live_projection_rejects_unknown_fields_secrets_and_bad_envelope(self):
        from onboarding import pool_config
        row = [uuid4(), 'pool-' + str(uuid4()), FIELDS, {}, uuid4(),
               datetime.now(timezone.utc), 'pool']
        for index, value in ((0, str(uuid4())), (1, 'live-' + str(uuid4())),
                             (2, {**FIELDS, 'password': 'private-canary'}),
                             (3, {'secret': 'private-canary'}), (4, str(uuid4())),
                             (5, datetime.now()), (6, 'live')):
            changed = row.copy(); changed[index] = value
            self.reject(lambda: pool_config._project(changed), ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_plan_is_stable_google_plan_bound_to_fixed_fields_owner_task(self):
        from onboarding.google_cloud import GooglePlan
        owner, task = str(uuid4()), str(uuid4())
        with patch('socket.socket.connect', side_effect=AssertionError('no network')), \
             patch('subprocess.Popen', side_effect=AssertionError('no subprocess')), \
             patch('builtins.open', side_effect=AssertionError('no private file')):
            plan = self.m.plan_for(FIELDS, owner, task)
            self.assertEqual(plan, self.m.plan_for(FIELDS.copy(), owner, task))
        self.assertIs(type(plan), GooglePlan)
        self.assertEqual((plan.owner_id, plan.task_id, plan.billing_account, plan.region,
                          plan.approved_role, plan.parent),
                         (owner, task, FIELDS['billing_account'], 'us-central1', 'roles/aiplatform.user', None))
        self.assertTrue(plan.project_id.startswith(FIELDS['project_prefix'] + '-'))
        self.assertTrue(plan.service_account_id.startswith(FIELDS['service_account_prefix'] + '-'))
        for name in (plan.project_id, plan.service_account_id):
            self.assertRegex(name, r'^[a-z][a-z0-9-]{4,28}[a-z0-9]$')
            self.assertEqual(len(name), 30)
        changed = self.m.plan_for({**FIELDS, 'billing_account': 'DDDDDD-EEEEEE-FFFFFF'}, owner, task)
        self.assertEqual(changed.project_id, plan.project_id)
        self.assertNotEqual(changed.effect_digest('project.create'), plan.effect_digest('project.create'))

    def test_plan_naming_uses_whole_task_uuid_and_fits_largest_prefix(self):
        owner = str(uuid4())
        ids = ('12345678-1234-4234-8234-123456789abc',
               '12345678-1234-4234-8234-123456789abd',
               '22345678-1234-4234-8234-123456789abc')
        plans = [self.m.plan_for({**FIELDS, 'project_prefix': 'a' * 12,
                 'service_account_prefix': 'b' * 12}, owner, value) for value in ids]
        self.assertEqual(len({p.project_id for p in plans}), 3)
        self.assertEqual(len({p.service_account_id for p in plans}), 3)
        for plan in plans:
            self.assertEqual(len(plan.project_id), 30)
            self.assertEqual(len(plan.service_account_id), 30)
            self.assertRegex(plan.project_id[13:], r'^[a-z2-7]{17}$')

    def test_plan_rejects_legacy_payload_overrides_and_bad_identity(self):
        owner, task = str(uuid4()), str(uuid4())
        for fields in (LEGACY, {**FIELDS, 'scheduling_enabled': True}, {'nonsecret_config': FIELDS}):
            self.reject(lambda: self.m.plan_for(fields, owner, task))
        for value in (None, True, UUID(task), uuid4().hex, 'private-canary'):
            self.reject(lambda: self.m.plan_for(FIELDS, value, task))
            self.reject(lambda: self.m.plan_for(FIELDS, owner, value))
