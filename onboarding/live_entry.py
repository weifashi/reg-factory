"""Confirmed two-transaction startup; no replay or lost-ack auto-restart.

Trusted server composition only. The returned executor must be driven through
``live_completion.run_managed`` by the server-owned background task. This module
does not install an HTTP route, accept arbitrary paths/targets or start a thread.
"""
from dataclasses import dataclass

from . import live_batches, live_config, live_run_store, live_runtime, storage
from .errors import ErrorCode, ServiceError
from .live_executor import LiveExecutor, validate_dependencies


@dataclass(frozen=True, repr=False)
class PreparedRun:
    result: dict
    executor: LiveExecutor | None


def start_committed(settings, actor, mailbox_id, expected_config_revision, request_key, *,
                    policy, mac, admission, key_store, google_auth_config, sub2api_config,
                    google_transport=None, sub2api_transport=None):
    validate_dependencies(settings, key_store=key_store, google_auth_config=google_auth_config,
                          sub2api_config=sub2api_config)
    result = live_batches.create_one(settings, actor, mailbox_id, expected_config_revision,
                                    request_key, policy=policy, mac=mac, admission=admission)
    # TX1 has exited normally. Lost COMMIT acknowledgements raise before here.
    if result['replayed']:
        return PreparedRun(result, None)
    task_id = result['task_ids'][0]
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        worker = live_runtime.handoff(conn, actor, task_id, 2, 'local-entry')
        row = conn.execute('SELECT nonsecret_config FROM global_configs WHERE id=%s',
                           (worker.config_id,)).fetchone()
        if row is None:
            raise ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        plan = live_config.plan_for(row[0], actor.operator_id, task_id)
        live_run_store.bind_plan(conn, worker, plan)
        policy._connection(conn)
        live_runtime.sending_guard(conn, worker, 'project.create', plan.effect_digest('project.create'))
    # No constructor, provider I/O or background scheduling inside either TX.
    executor = LiveExecutor(settings, worker, plan, key_store=key_store,
        google_auth_config=google_auth_config, sub2api_config=sub2api_config,
        google_transport=google_transport, sub2api_transport=sub2api_transport)
    return PreparedRun(result, executor)
