"""Pool records validate shapes only: no authentication or execution capability."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields
from enum import Enum
import importlib
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

from onboarding.errors import ErrorCode, ServiceError
from onboarding.security import Actor


TASK = 'abcdefab-0000-4000-8000-000000000001'
RESOURCE = 'abcdefab-0000-4000-8000-000000000002'
OPERATOR = 'abcdefab-0000-4000-8000-000000000003'
SESSION = 'abcdefab-0000-4000-8000-000000000004'
MAX = 9223372036854775807


class Text(str):
    pass


class Number(int):
    pass


class Permissions(frozenset):
    pass


class PoolContractsTests(unittest.TestCase):
    def api(self):
        # Missing implementation is a clear assertion failure, not import ERROR.
        self.assertIsNotNone(importlib.util.find_spec('onboarding.pool_contracts'),
                             'missing approved pure pool contracts module')
        return importlib.import_module('onboarding.pool_contracts')

    def actor(self, **changes):
        values = dict(operator_id=OPERATOR, permissions=frozenset(),
                      session_id=SESSION, auth_epoch=1)
        values.update(changes)
        return Actor(**values)

    def lease(self, **changes):
        values = dict(task_id=TASK, resource_kind='mailbox', resource_id=RESOURCE,
                      owner_id='local.owner:1-worker', fence=1, credential_version=1)
        values.update(changes)
        return self.api().PoolLeaseToken(**values)

    def context(self, **changes):
        values = dict(actor=self.actor(), task_id=TASK,
                      platform=self.api().Platform('google'), lease=self.lease())
        values.update(changes)
        return self.api().PoolExecutionContext(**values)

    def invalid(self, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertIs(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(str(caught.exception), 'INVALID_INPUT')

    def test_platform_is_exact_seven_and_matches_both_existing_sources(self):
        api = self.api()
        from onboarding.pool_secret_types import _PLATFORMS
        expected = {'google', 'claude', 'chatgpt', 'grok', 'kiro', 'github', 'k12'}
        self.assertTrue(issubclass(api.Platform, str))
        self.assertTrue(issubclass(api.Platform, Enum))
        self.assertEqual(len(api.Platform.__members__), 7)
        self.assertEqual({item.value for item in api.Platform}, expected)
        self.assertEqual(expected, _PLATFORMS)
        sql = (Path(__file__).resolve().parents[1] /
               'onboarding/migrations/002_pools.sql').read_text()
        mailbox_table = sql.split('CREATE TABLE mailbox_platform_states (', 1)[1].split('\n);', 1)[0]
        declaration = re.search(r'CHECK\(platform IN \(([^)]+)\)\)', mailbox_table)
        self.assertIsNotNone(declaration)
        self.assertEqual(set(re.findall(r"'([^']+)'", declaration.group(1))), expected)
        # Task orchestration additionally allows combined; an execution context does not.
        task_declaration = re.search(
            r'ALTER TABLE onboarding_tasks ADD COLUMN platform text CHECK\(platform IN \(([^)]+)\)\)', sql)
        self.assertIsNotNone(task_declaration)
        self.assertEqual(set(re.findall(r"'([^']+)'", task_declaration.group(1))),
                         expected | {'combined'})
        with self.assertRaises(ValueError):
            api.Platform('combined')
        for platform in api.Platform:
            self.assertIsNone(self.context(platform=platform).validate())

    def test_exact_fields_frozen_hidden_repr_and_no_capability(self):
        for value, names, representation in (
                (self.lease(), ('task_id', 'resource_kind', 'resource_id', 'owner_id',
                                'fence', 'credential_version'), '<PoolLeaseToken>'),
                (self.context(), ('actor', 'task_id', 'platform', 'lease'),
                 '<PoolExecutionContext>')):
            self.assertEqual(tuple(field.name for field in fields(value)), names)
            self.assertEqual(set(vars(value)), set(names))
            self.assertEqual(repr(value), representation)
            self.assertIsNone(value.validate())
            with self.assertRaises(FrozenInstanceError):
                value.task_id = RESOURCE
            with self.assertRaises(FrozenInstanceError):
                value.extra = True
            with self.assertRaises(TypeError):
                type(value)(**vars(value), permit=True)

    def test_canonical_exact_uuid_fields(self):
        for bad in (TASK.upper(), TASK.replace('-', ''), '{' + TASK + '}',
                    ' ' + TASK, TASK + '\n', '', None, 1, Text(TASK)):
            for name in ('task_id', 'resource_id'):
                with self.subTest(name=name, value=repr(bad)):
                    self.invalid(lambda: self.lease(**{name: bad}))
            self.invalid(lambda: self.context(task_id=bad))
            for name in ('operator_id', 'session_id'):
                self.invalid(lambda: self.context(actor=self.actor(**{name: bad})))
        self.invalid(lambda: self.context(task_id=RESOURCE))

    def test_resource_kind_and_owner_exact_boundaries(self):
        for bad in ('card', 'platform', 'mailbox\n', '', Text('mailbox'), None):
            self.invalid(lambda: self.lease(resource_kind=bad))
        for valid in ('a', '0', 'Z', 'a' * 128, 'a_.:-Z09'):
            self.assertIsNone(self.lease(owner_id=valid).validate())
        for bad in ('', 'a' * 129, '-a', '_a', '.a', ':a', 'a b', 'a/b',
                    'a\n', 'a\x00', 'é', 'aé', Text('worker'), None, True):
            self.invalid(lambda: self.lease(owner_id=bad))

    def test_platform_requires_declared_member_identity_not_forged_exact_type(self):
        api = self.api()
        for text in ('combined', 'google'):
            forged = str.__new__(api.Platform, text)
            self.assertIs(type(forged), api.Platform)
            with self.subTest(text=text, boundary='constructor'):
                self.invalid(lambda: self.context(platform=forged))
            context = self.context()
            object.__setattr__(context, 'platform', forged)
            with self.subTest(text=text, boundary='validate'):
                self.invalid(context.validate)
        for member in api.Platform:
            self.assertIsNone(self.context(platform=member).validate())

    def test_bigints_are_exact_positive_and_bounded(self):
        for value in (1, MAX):
            self.assertIsNone(self.lease(fence=value, credential_version=value).validate())
            self.assertIsNone(self.context(actor=self.actor(auth_epoch=value)).validate())
        for bad in (False, True, 0, -1, MAX + 1, 1.0, '1', Number(1), None):
            for field in ('fence', 'credential_version'):
                self.invalid(lambda: self.lease(**{field: bad}))
            self.invalid(lambda: self.context(actor=self.actor(auth_epoch=bad)))

    def test_permissions_shape_does_not_authenticate_or_authorize(self):
        for value in (frozenset(), frozenset({'not-a-real-permission', ''})):
            self.assertIsNone(self.context(actor=self.actor(permissions=value)).validate())
        for bad in (set(), [], (), None, Permissions(), frozenset({1}),
                    frozenset({Text('tasks:manage')})):
            self.invalid(lambda: self.context(actor=self.actor(permissions=bad)))

    def test_old_types_dicts_and_subclasses_are_rejected(self):
        api = self.api()
        from onboarding.contracts import LeaseToken
        from onboarding.repository import FixtureActor
        class ActorChild(Actor):
            pass
        class LeaseChild(api.PoolLeaseToken):
            pass
        class ContextChild(api.PoolExecutionContext):
            pass
        class OtherPlatform(str, Enum):
            GOOGLE = 'google'
        for bad in (FixtureActor(OPERATOR), vars(self.actor()),
                    ActorChild(**vars(self.actor())), None):
            self.invalid(lambda: self.context(actor=bad))
        for bad in (LeaseToken('mailbox', RESOURCE, TASK, 'worker', 1),
                    vars(self.lease()), object.__new__(LeaseChild), None):
            self.invalid(lambda: self.context(lease=bad))
        for bad in ('google', OtherPlatform.GOOGLE, None):
            self.invalid(lambda: self.context(platform=bad))
        self.invalid(lambda: LeaseChild(**vars(self.lease())))
        self.invalid(lambda: ContextChild(**vars(self.context())))
        self.invalid(lambda: api.PoolLeaseToken.validate(vars(self.lease())))
        self.invalid(lambda: api.PoolExecutionContext.validate(vars(self.context())))

    def test_missing_extra_and_equal_nonexact_keys_rejected_at_every_layer(self):
        api = self.api()
        factories = (self.lease, self.context, self.actor)
        for factory in factories:
            original = factory()
            for field in vars(original):
                for mode in ('missing', 'extra', 'subclass-key'):
                    damaged = factory()
                    if mode == 'missing':
                        object.__delattr__(damaged, field)
                    elif mode == 'extra':
                        object.__setattr__(damaged, 'extra', True)
                    else:
                        value = vars(damaged).pop(field)
                        vars(damaged)[Text(field)] = value
                    with self.subTest(record=type(original).__name__, field=field, mode=mode):
                        if type(damaged) is Actor:
                            self.invalid(lambda: self.context(actor=damaged))
                            context = self.context()
                            object.__setattr__(context, 'actor', damaged)
                            self.invalid(context.validate)
                        else:
                            self.invalid(damaged.validate)
                            if type(damaged) is api.PoolLeaseToken:
                                self.invalid(lambda: self.context(lease=damaged))
            empty = object.__new__(type(original))
            if type(empty) is Actor:
                self.invalid(lambda: self.context(actor=empty))
            else:
                self.invalid(empty.validate)

    def test_frozen_bypass_revalidates_values_and_ignores_instance_validator(self):
        for field, bad in (('task_id', TASK.upper()), ('resource_id', 'bad'),
                           ('resource_kind', 'card'), ('owner_id', 'bad\n'),
                           ('fence', True), ('credential_version', 0)):
            lease = self.lease()
            object.__setattr__(lease, field, bad)
            self.invalid(lease.validate)
            self.invalid(lambda: self.context(lease=lease))
        for field, bad in (('task_id', RESOURCE), ('platform', 'google'),
                           ('lease', {}), ('actor', {})):
            context = self.context()
            object.__setattr__(context, field, bad)
            self.invalid(context.validate)
        lease = self.lease()
        object.__setattr__(lease, 'validate', lambda: None)
        self.invalid(lambda: self.context(lease=lease))
        self.invalid(lambda: type(lease).validate(lease))
        context = self.context()
        object.__setattr__(context, 'validate', lambda: None)
        self.invalid(lambda: type(context).validate(context))
        for field, bad in (('operator_id', 'bad'), ('session_id', 'bad'),
                           ('permissions', set()), ('auth_epoch', True)):
            context = self.context()
            object.__setattr__(context.actor, field, bad)
            self.invalid(context.validate)

    def test_external_action_check_preserves_canonical_classes_and_objects(self):
        api = self.api()
        names = ('Platform', 'PoolLeaseToken', 'PoolExecutionContext')
        before = tuple(getattr(api, name) for name in names)
        lease, context = self.lease(), self.context()
        modules_before = dict(sys.modules)
        self.test_import_construct_and_validate_never_perform_external_actions()
        for name, original in zip(names, before):
            with self.subTest(export=name):
                self.assertIs(getattr(api, name), original)
        with self.subTest(record='existing lease'):
            try:
                self.assertIsNone(lease.validate())
            except ServiceError as exc:
                self.fail('previously valid lease invalidated: ' + str(exc))
        with self.subTest(record='existing context'):
            try:
                self.assertIsNone(context.validate())
            except ServiceError as exc:
                self.fail('previously valid context invalidated: ' + str(exc))
        self.assertIs(self.api(), api)
        self.assertEqual({name for name in sys.modules if name.startswith('onboarding.')},
                         {name for name in modules_before if name.startswith('onboarding.')})

    def test_import_construct_and_validate_never_perform_external_actions(self):
        # Block actual boundary calls, not merely helpers that might be bypassed.
        alias = 'onboarding._pool_contracts_no_external_actions_test'
        self.assertNotIn(alias, sys.modules)
        spec = importlib.util.spec_from_file_location(alias, self.api().__file__)
        api = importlib.util.module_from_spec(spec)
        targets = ('psycopg.connect', 'psycopg.Connection.connect',
                   'psycopg.AsyncConnection.connect', 'psycopg.Connection.execute',
                   'psycopg.Connection.cursor', 'psycopg.Cursor.execute',
                   'socket.socket', 'socket.create_connection', 'subprocess.Popen',
                   'subprocess.run', 'os.system', 'onboarding.security.require',
                   'onboarding.security.revalidate', 'onboarding.security._authenticate')
        with ExitStack() as stack:
            spies = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                     for target in targets]
            # Register only the temporary namespace for dataclass processing;
            # never reload or replace the canonical module and its classes.
            stack.enter_context(patch.dict(sys.modules, {alias: api}))
            spec.loader.exec_module(api)
            lease_args = dict(task_id=TASK, resource_kind='mailbox', resource_id=RESOURCE,
                              owner_id='local.worker', fence=1, credential_version=1)
            lease = api.PoolLeaseToken(**lease_args)
            context_args = dict(actor=self.actor(), task_id=TASK,
                                platform=api.Platform('google'), lease=lease)
            self.assertIsNone(lease.validate())
            self.assertIsNone(api.PoolExecutionContext(**context_args).validate())
            self.invalid(lambda: api.PoolLeaseToken(**{**lease_args, 'fence': True}))
            self.invalid(lambda: api.PoolExecutionContext(**{**context_args, 'task_id': RESOURCE}))
            self.assertFalse(hasattr(api, 'ExecutionPermit'))
            for spy in spies:
                spy.assert_not_called()
        self.assertNotIn(alias, sys.modules)


if __name__ == '__main__':
    unittest.main()
