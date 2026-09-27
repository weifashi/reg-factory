"""Immutable records only; no database or dispatch side effects."""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class LeaseToken:
    resource_kind: str
    resource_id: str
    task_id: str
    owner_id: str
    fence: int

@dataclass(frozen=True)
class PendingAction:
    receipt_id: str
    phase: str
    newly_consumed: bool

@dataclass(frozen=True)
class PreparedAction:
    receipt_id: str
    phase: str
    dispatch_permit: bool

@dataclass(frozen=True)
class Observation:
    code: str
    evidence_ref: Optional[str]
    external_ref: Optional[str]
