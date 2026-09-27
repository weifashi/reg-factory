"""Actual PostgreSQL rollback faults (no provider calls or shared connections)."""
from dataclasses import replace
import multiprocessing
from psycopg.errors import DeadlockDetected
from onboarding.errors import ErrorCode, ServiceError
from onboarding_b1_support import B1Case


def _deadlock_worker(schema, first, second, barrier, queue):
    from onboarding.settings import BASE, load_settings
    from onboarding.storage import unit_of_work
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    confirmed_deadlock = False
    try:
        with unit_of_work(settings) as conn:
            # Give PostgreSQL's deadlock detector time before generic lock timeout.
            conn.execute("SET LOCAL lock_timeout='4s'")
            conn.execute('UPDATE onboarding_tasks SET version=version+1 WHERE id=%s', (first,))
            barrier.wait(timeout=10)
            try:
                conn.execute('UPDATE onboarding_tasks SET version=version+1 WHERE id=%s', (second,))
            except DeadlockDetected:
                confirmed_deadlock = True
                raise
        queue.put('COMMITTED')
    except ServiceError as error:
        if not confirmed_deadlock or error.code != ErrorCode.DEPENDENCY_UNAVAILABLE:
            queue.put('UNEXPECTED')
            return
        # At most one explicit retry, only after a PostgreSQL-confirmed rollback.
        # This is test code, not a general retry-on-503 strategy.
        with unit_of_work(settings) as conn:
            for task_id in sorted((first, second)):
                conn.execute('UPDATE onboarding_tasks SET version=version+1 WHERE id=%s', (task_id,))
        queue.put('ROLLBACK_THEN_ONE_RETRY')


class B1FaultTests(B1Case):
    def test_real_deadlock_rolls_back_before_single_explicit_retry(self):
        tasks = [self.task()['id'], self.task()['id']]
        ctx = multiprocessing.get_context('spawn')
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=_deadlock_worker,
                                args=(self.settings.schema, tasks[i], tasks[1-i], barrier, queue))
                    for i in range(2)]
        try:
            for child in children:
                child.start()
            outcomes = [queue.get(timeout=20) for _ in children]
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sorted(outcomes), ['COMMITTED', 'ROLLBACK_THEN_ONE_RETRY'])
            self.assertEqual(self.read('SELECT version FROM onboarding_tasks ORDER BY id'), [(3,), (3,)])
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_database_trigger_failure_rolls_back_task_batch_and_audit(self):
        from onboarding import repository
        # Owner installs failure injection only inside this manifest-scoped fixture.
        with self.fixture.migrator() as conn:
            conn.execute("CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                         "BEGIN RAISE EXCEPTION 'fixture audit unavailable'; END; $$")
            conn.execute('CREATE TRIGGER reject_audit BEFORE INSERT ON audit_events '
                         'FOR EACH ROW EXECUTE FUNCTION reject_audit()')
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                repository.create_fixture_task(conn, self.actor, 'fixture:trigger-failure', self.config_id)
        self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks')[0][0], 0)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_batches')[0][0], 0)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events')[0][0], 0)
