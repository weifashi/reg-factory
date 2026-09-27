"""Stable error codes: never expose connection strings or provider errors."""
from enum import Enum

class ErrorCode(str, Enum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    INVALID_INPUT = "INVALID_INPUT"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    RESOURCE_HELD = "RESOURCE_HELD"
    STALE_FENCE = "STALE_FENCE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    GRANT_UNAVAILABLE = "GRANT_UNAVAILABLE"
    LEGACY_EXECUTION_BLOCKED = "LEGACY_EXECUTION_BLOCKED"

class ServiceError(Exception):
    def __init__(self, code: ErrorCode, *, not_committed: bool = False):
        self.code = code
        # Only the five allowlisted raises may set this (static guard test).
        self.not_committed = not_committed is True
        super().__init__(code.value)
