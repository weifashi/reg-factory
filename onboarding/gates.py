"""Pure prechecks over trusted, typed server snapshots, not authorization commits.

``resource_revision`` must be composed on the server to bind the project, key,
Sub2API resource, contract, and fixed test specification; a client-provided string
is not evidence of that binding. P1b/P4 must recheck current state transactionally
and atomically consume approval before effects. These functions provide neither
atomic consumption nor idempotency and never perform a paid test or enablement.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ApprovalView:
    action: str
    task_id: str
    resource_revision: str
    config_revision: str
    expires_at: datetime
    consumed: bool = False
    revoked: bool = False


@dataclass(frozen=True)
class ExecutionView:
    task_id: str
    resource_revision: str
    config_revision: str
    paused: bool = False
    cancelled: bool = False
    isolation_verified: bool = False
    contract_verified: bool = False
    verification: str = "NOT_SENT"
    scheduling: str = "DISABLED"


def _matching(
    approval: Optional[ApprovalView],
    context: ExecutionView,
    action: str,
    now: datetime,
) -> bool:
    if approval is None or approval.action != action:
        return False
    if not (context.task_id and context.resource_revision and context.config_revision):
        return False
    if now.utcoffset() is None or approval.expires_at.utcoffset() is None:
        return False
    return (
        not approval.consumed
        and not approval.revoked
        # UTC comparison respects absolute expiry across a repeated DST hour.
        and approval.expires_at.astimezone(timezone.utc) > now.astimezone(timezone.utc)
        and approval.task_id == context.task_id
        and approval.resource_revision == context.resource_revision
        and approval.config_revision == context.config_revision
        and not context.paused
        and not context.cancelled
        and context.isolation_verified is True
        and context.contract_verified is True
        and context.scheduling == "DISABLED"
    )


def allow_test(
    approval: Optional[ApprovalView], context: ExecutionView, now: datetime
) -> bool:
    """Whether this snapshot may enter the paid-test transaction's recheck."""
    return _matching(approval, context, "test", now) and context.verification == "NOT_SENT"


def allow_enable(
    approval: Optional[ApprovalView], context: ExecutionView, now: datetime
) -> bool:
    """Whether this snapshot may enter the enablement transaction's recheck."""
    return _matching(approval, context, "enable", now) and context.verification == "VERIFIED"
