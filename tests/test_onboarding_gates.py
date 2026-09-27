import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo

from onboarding.gates import ApprovalView, ExecutionView, allow_enable, allow_test


class NoOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        return None


class RepeatedHourTimezone(tzinfo):
    """Simulate only a repeated hour, not complete New York timezone history."""

    def utcoffset(self, dt):
        return None if dt is None else timedelta(hours=-4 - dt.fold)

    def dst(self, dt):
        return None if dt is None else timedelta(hours=1 - dt.fold)

    def tzname(self, dt):
        return None if dt is None else ("REPEATED_STANDARD" if dt.fold else "REPEATED_DST")


class OnboardingGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        self.context = ExecutionView(
            task_id="task-1",
            resource_revision="server-composite-resource-r1",
            config_revision="config-r1",
            isolation_verified=True,
            contract_verified=True,
        )
        self.approval = ApprovalView(
            action="test",
            task_id=self.context.task_id,
            resource_revision=self.context.resource_revision,
            config_revision=self.context.config_revision,
            expires_at=self.now + timedelta(minutes=5),
        )

    def test_matching_approval_allows_transaction_precheck(self):
        self.assertTrue(allow_test(self.approval, self.context, self.now))
        self.assertFalse(self.approval.consumed)

    def test_default_unverified_context_is_denied(self):
        context = ExecutionView(
            task_id=self.context.task_id,
            resource_revision=self.context.resource_revision,
            config_revision=self.context.config_revision,
        )
        self.assertFalse(allow_test(self.approval, context, self.now))

    def test_missing_expired_revoked_consumed_or_wrong_action_cannot_test(self):
        approvals = (
            None,
            replace(self.approval, expires_at=self.now),
            replace(self.approval, expires_at=self.now - timedelta(seconds=1)),
            replace(self.approval, revoked=True),
            replace(self.approval, consumed=True),
            replace(self.approval, action="enable"),
            replace(self.approval, action="UNKNOWN"),
        )
        for approval in approvals:
            with self.subTest(approval=approval):
                self.assertFalse(allow_test(approval, self.context, self.now))

    def test_changed_task_resource_or_configuration_is_denied(self):
        for field in ("task_id", "resource_revision", "config_revision"):
            with self.subTest(field=field):
                context = replace(self.context, **{field: "changed"})
                self.assertFalse(allow_test(self.approval, context, self.now))

    def test_unsafe_execution_context_cannot_test(self):
        changes = (
            {"paused": True},
            {"cancelled": True},
            {"isolation_verified": False},
            {"contract_verified": False},
            {"verification": "VERIFYING"},
            {"verification": "UNKNOWN"},
            {"verification": "VERIFIED"},
            {"scheduling": "ENABLED"},
            {"scheduling": "UNKNOWN"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(
                    allow_test(self.approval, replace(self.context, **change), self.now)
                )

    def test_enable_needs_independent_approval_and_successful_verification(self):
        verified = replace(self.context, verification="VERIFIED")
        enable_approval = replace(self.approval, action="enable")
        self.assertFalse(allow_enable(self.approval, verified, self.now))
        self.assertFalse(allow_enable(None, verified, self.now))
        self.assertTrue(allow_enable(enable_approval, verified, self.now))
        for status in ("NOT_SENT", "VERIFYING", "UNKNOWN", "FAILED"):
            with self.subTest(status=status):
                self.assertFalse(
                    allow_enable(
                        enable_approval,
                        replace(verified, verification=status),
                        self.now,
                    )
                )

    def test_unknown_or_enabled_scheduling_cannot_enable_again(self):
        approval = replace(self.approval, action="enable")
        for scheduling in ("UNKNOWN", "ENABLED"):
            with self.subTest(scheduling=scheduling):
                context = replace(
                    self.context, verification="VERIFIED", scheduling=scheduling
                )
                self.assertFalse(allow_enable(approval, context, self.now))

    def test_enable_rechecks_common_approval_and_execution_guards(self):
        approval = replace(self.approval, action="enable")
        context = replace(self.context, verification="VERIFIED")
        for change in (
            {"expires_at": self.now},
            {"consumed": True},
            {"revoked": True},
            {"task_id": "changed"},
            {"resource_revision": "changed"},
            {"config_revision": "changed"},
        ):
            with self.subTest(approval_change=change):
                self.assertFalse(
                    allow_enable(replace(approval, **change), context, self.now)
                )
        for change in (
            {"paused": True},
            {"cancelled": True},
            {"isolation_verified": False},
            {"contract_verified": False},
        ):
            with self.subTest(context_change=change):
                self.assertFalse(
                    allow_enable(approval, replace(context, **change), self.now)
                )

    def test_empty_identifiers_are_denied_even_when_both_views_match(self):
        for field in ("task_id", "resource_revision", "config_revision"):
            with self.subTest(field=field):
                approval = replace(self.approval, **{field: ""})
                context = replace(self.context, **{field: ""})
                self.assertFalse(allow_test(approval, context, self.now))

    def test_naive_or_no_offset_dates_are_denied(self):
        for invalid in (
            self.now.replace(tzinfo=None),
            self.now.replace(tzinfo=NoOffsetTimezone()),
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(allow_test(self.approval, self.context, invalid))
                self.assertFalse(
                    allow_test(
                        replace(self.approval, expires_at=invalid),
                        self.context,
                        self.now,
                    )
                )

    def test_different_timezone_offsets_compare_absolute_time(self):
        offset = timezone(timedelta(hours=8))
        expires_at = (self.now + timedelta(seconds=1)).astimezone(offset)
        self.assertTrue(
            allow_test(
                replace(self.approval, expires_at=expires_at), self.context, self.now
            )
        )
        self.assertFalse(
            allow_test(
                replace(self.approval, expires_at=self.now.astimezone(offset)),
                self.context,
                self.now,
            )
        )

    def test_dst_fold_cannot_make_expired_approval_look_valid(self):
        zone = RepeatedHourTimezone()
        now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
        expired = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
        self.assertFalse(
            allow_test(replace(self.approval, expires_at=expired), self.context, now)
        )

    def test_dst_fold_does_not_reject_future_approval(self):
        zone = RepeatedHourTimezone()
        now = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
        future = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
        self.assertTrue(
            allow_test(replace(self.approval, expires_at=future), self.context, now)
        )

    def test_views_are_frozen_snapshots(self):
        with self.assertRaises(FrozenInstanceError):
            self.approval.consumed = True
        with self.assertRaises(FrozenInstanceError):
            self.context.scheduling = "ENABLED"


if __name__ == "__main__":
    unittest.main()
