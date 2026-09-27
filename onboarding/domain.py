"""Offline routing decisions only; no registration or external side effects."""

from enum import Enum
from typing import Optional


class Route(str, Enum):
    AUTO = "AUTO"
    AUTO_CHANNEL = "AUTO_CHANNEL"
    WAIT_HUMAN = "WAIT_HUMAN"
    WAIT_ADMIN = "WAIT_ADMIN"
    BLOCKED = "BLOCKED"


def next_action(
    observation: str, *, authorized: bool = False, channel_ready: bool = False
) -> Route:
    """Choose a permitted next step, never infer registration or success."""
    if not authorized:
        return Route.BLOCKED
    if observation in ("permission_denied", "policy_denied"):
        return Route.WAIT_ADMIN
    if observation == "ordinary":
        return Route.AUTO
    if observation in ("email_code", "google_phone_code", "bank_code"):
        return Route.AUTO_CHANNEL if channel_ready else Route.WAIT_HUMAN
    return Route.WAIT_HUMAN


class BindingState(str, Enum):
    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"
    LINKED = "LINKED"
    RELEASED = "RELEASED"
    CONFLICT = "CONFLICT"


def settle_binding(previous: BindingState, evidence: str) -> BindingState:
    """Decide state only, without counts or locks.

    Confirmed evidence must come from a trusted adapter, not a user button.
    This function neither verifies external evidence nor performs registration.
    """
    if previous == BindingState.CONFLICT:
        return BindingState.CONFLICT
    if evidence == "success_confirmed":
        return (
            BindingState.CONFLICT if previous == BindingState.RELEASED
            else BindingState.LINKED
        )
    if evidence == "failure_confirmed":
        return (
            BindingState.CONFLICT if previous == BindingState.LINKED
            else BindingState.RELEASED
        )
    if previous in (BindingState.LINKED, BindingState.RELEASED):
        return previous
    return BindingState.UNKNOWN


def cancel_binding(previous: BindingState, *, sent: Optional[bool]) -> BindingState:
    """Cancellation releases only a reserved operation proven not to be sent."""
    if previous == BindingState.RESERVED:
        return BindingState.RELEASED if sent is False else BindingState.UNKNOWN
    return previous
