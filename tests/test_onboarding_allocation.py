import unittest
from dataclasses import FrozenInstanceError

from onboarding.allocation import CardView, choose_card


class ChooseCardTests(unittest.TestCase):
    def test_prefers_fewer_linked_accounts(self):
        card_a = CardView("A", linked=1, reserved=0, account_limit=3)
        card_b = CardView("B", linked=0, reserved=0, account_limit=2)

        self.assertIs(choose_card([card_a, card_b]), card_b)

    def test_excludes_unavailable_cards(self):
        cases = [
            {"busy": True},
            {"reserved": 1},
            {"linked": 3},
            {"enabled": False},
            {"complete": False},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                values = {"linked": 0, "reserved": 0, "account_limit": 3}
                values.update(changes)
                self.assertIsNone(choose_card([CardView("A", **values)]))

    def test_breaks_ties_by_last_assignment_then_card_id(self):
        older = CardView("C", 1, 0, 3, last_assigned=5)
        newer = CardView("A", 1, 0, 3, last_assigned=10)
        same_time = CardView("B", 1, 0, 3, last_assigned=5)

        self.assertIs(choose_card([newer, older]), older)
        self.assertIs(choose_card([older, same_time]), same_time)

    def test_returns_none_for_empty_input(self):
        self.assertIsNone(choose_card([]))

    def test_rejects_invalid_counter_ranges(self):
        cases = [
            {"linked": -1},
            {"reserved": -1},
            {"account_limit": 0},
            {"linked": 3, "reserved": 1},
            {"last_assigned": -1},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                values = {"linked": 0, "reserved": 0, "account_limit": 3}
                values.update(changes)
                with self.assertRaises(ValueError):
                    CardView("A", **values)

    def test_rejects_non_integer_counters(self):
        class IntegerSubclass(int):
            pass

        for field in ("linked", "reserved", "account_limit", "last_assigned"):
            for invalid in (True, False, 1.0, "1", None, IntegerSubclass(1)):
                with self.subTest(field=field, invalid=invalid):
                    values = {"linked": 0, "reserved": 0, "account_limit": 3}
                    values[field] = invalid
                    with self.assertRaises(ValueError):
                        CardView("A", **values)

    def test_rejects_empty_card_id(self):
        with self.assertRaises(ValueError):
            CardView("", 0, 0, 1)

    def test_card_view_is_frozen_with_available_defaults(self):
        card = CardView("A", 0, 0, 1)

        self.assertEqual(card.last_assigned, 0)
        self.assertFalse(card.busy)
        self.assertTrue(card.enabled)
        self.assertTrue(card.complete)
        with self.assertRaises(FrozenInstanceError):
            card.linked = 1

    def test_accepts_one_pass_iterable(self):
        card = CardView("A", 0, 0, 1)

        self.assertIs(choose_card(iter([card])), card)


if __name__ == "__main__":
    unittest.main()
