"""A versioned config reader is not permission to mix task execution contracts."""
import unittest
from unittest.mock import patch
from onboarding import live_config, pool_batches, pool_config, pool_repository
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_input_vault import LivePoolPolicy
from onboarding.migrate import apply_all
from test_onboarding_live_config import FIELDS
from test_onboarding_pool_batches import PoolBatchEntryTests


class LiveConfigModeTests(PoolBatchEntryTests):
    # Avoid rerunning the imported base's entire test suite in this dedicated module.
    def setUp(self):
        super().setUp()
        with self.fixture.migrator() as conn:
            apply_all(conn,self.fixture.schema,target_version=4)

    def live_head(self,previous=None):
        return live_config.replace(self.settings,self.actor,
            previous['revision'] if previous else None,FIELDS,'live-head',
            policy=LivePoolPolicy(self.settings),mac=self.mac)

    def test_live_head_rejects_old_preflight_and_create_without_writes(self):
        seed=self.seed(identity='NEW_CONFIRMED'); config=self.live_head()
        before=self.business_snapshot()
        with patch.object(pool_batches,'_resources',side_effect=AssertionError('no allocation')):
            self.reject(lambda:self.call(config,[seed]),ErrorCode.FORBIDDEN)
        self.reject(lambda:pool_batches.preflight(self.settings,self.actor,'automatic',1,[],
            policy=self.vault._policy,mac=self.mac),ErrorCode.FORBIDDEN)
        self.assertEqual(before,self.business_snapshot())

    def test_live_head_does_not_break_old_pinned_v1_replay(self):
        old=self.config(); seed=self.seed(identity='NEW_CONFIRMED')
        result=self.call(old,[seed]); self.live_head(old)
        self.assertEqual(self.call(old,[seed]),result)

    def test_legacy_task_with_relabelled_v2_config_is_rejected_on_replay(self):
        from psycopg.types.json import Jsonb
        old=self.config(); seed=self.seed(identity='NEW_CONFIRMED')
        self.call(old,[seed])
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                (Jsonb(FIELDS),old['config_id']))
        self.reject(lambda:self.call(old,[seed]),ErrorCode.FORBIDDEN)


    def test_legacy_task_lock_rejects_relabelled_v2_config(self):
        from psycopg.types.json import Jsonb
        old=self.config(); seed=self.seed(identity='NEW_CONFIRMED')
        result=self.call(old,[seed])
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                (Jsonb(FIELDS),old['config_id']))
        before=self.business_snapshot()
        def lock():
            with self.uow() as conn:
                return pool_repository.lock_task(conn,self.actor,result['task_ids'][0],
                    policy=self.vault._policy)
        self.reject(lock,ErrorCode.FORBIDDEN)
        self.assertEqual(before,self.business_snapshot())


def load_tests(loader,tests,pattern):
    # Only these new tests; helpers are inherited, not the original 50+ cases.
    return unittest.TestSuite(LiveConfigModeTests(name) for name in
        LiveConfigModeTests.__dict__ if name.startswith('test_'))
