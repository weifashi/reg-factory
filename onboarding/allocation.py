"""Offline candidate selection; no reservation or external state changes."""

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class CardView:
    card_id: str
    linked: int
    reserved: int
    account_limit: int
    last_assigned: int = 0
    busy: bool = False
    enabled: bool = True
    complete: bool = True

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card_id must not be empty")
        for name in ("linked", "reserved", "account_limit", "last_assigned"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an int")
        for name in ("linked", "reserved", "last_assigned"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.account_limit < 1:
            raise ValueError("account_limit must be at least 1")
        if self.linked + self.reserved > self.account_limit:
            raise ValueError("linked + reserved must not exceed account_limit")


def choose_card(cards: Iterable[CardView]) -> Optional[CardView]:
    """Choose a candidate only; P1c database locking and rechecks remain required."""
    eligible = (
        card
        for card in cards
        if card.enabled
        and card.complete
        and not card.busy
        and card.reserved == 0
        and card.linked < card.account_limit
    )
    return min(
        eligible,
        key=lambda card: (card.linked, card.last_assigned, card.card_id),
        default=None,
    )
