import unittest


class NextActionTests(unittest.TestCase):
    def setUp(self):
        from onboarding import domain

        self.domain = domain

    def test_unauthorized_is_blocked_before_other_rules(self):
        for observation in (
            "ordinary", "permission_denied", "policy_denied", "email_code",
            "google_phone_code", "bank_code", "timeout", "anything",
        ):
            with self.subTest(observation=observation):
                self.assertEqual(
                    self.domain.next_action(observation, channel_ready=True),
                    self.domain.Route.BLOCKED,
                )
                self.assertEqual(
                    self.domain.next_action(observation, authorized=False),
                    self.domain.Route.BLOCKED,
                )

    def test_permission_or_policy_denial_waits_for_admin(self):
        for observation in ("permission_denied", "policy_denied"):
            with self.subTest(observation=observation):
                self.assertEqual(
                    self.domain.next_action(
                        observation, authorized=True, channel_ready=True
                    ),
                    self.domain.Route.WAIT_ADMIN,
                )

    def test_authorized_ordinary_step_is_automatic(self):
        for channel_ready in (False, True):
            with self.subTest(channel_ready=channel_ready):
                self.assertEqual(
                    self.domain.next_action(
                        "ordinary", authorized=True, channel_ready=channel_ready
                    ),
                    self.domain.Route.AUTO,
                )

    def test_codes_require_a_ready_channel(self):
        for observation in ("email_code", "google_phone_code", "bank_code"):
            for channel_ready in (False, True):
                with self.subTest(
                    observation=observation, channel_ready=channel_ready
                ):
                    self.assertEqual(
                        self.domain.next_action(
                            observation, authorized=True, channel_ready=channel_ready
                        ),
                        self.domain.Route.AUTO_CHANNEL
                        if channel_ready else self.domain.Route.WAIT_HUMAN,
                    )
            with self.subTest(observation=observation, channel_ready="default"):
                self.assertEqual(
                    self.domain.next_action(observation, authorized=True),
                    self.domain.Route.WAIT_HUMAN,
                )

    def test_other_observations_never_imply_registration_or_success(self):
        for observation in (
            "bad_password", "timeout", "captcha", "identity_unknown",
            "trial_denied", "unrecognized", "arbitrary", "", "success",
            "register", "ORDINARY",
        ):
            for channel_ready in (False, True):
                with self.subTest(
                    observation=observation, channel_ready=channel_ready
                ):
                    self.assertEqual(
                        self.domain.next_action(
                            observation, authorized=True, channel_ready=channel_ready
                        ),
                        self.domain.Route.WAIT_HUMAN,
                    )


class BindingTests(unittest.TestCase):
    def setUp(self):
        from onboarding import domain

        self.domain = domain
        self.state = domain.BindingState

    def test_confirmed_success_and_failure_settle_each_previous_state(self):
        expected_states = (
            (self.state.RESERVED, self.state.LINKED, self.state.RELEASED),
            (self.state.UNKNOWN, self.state.LINKED, self.state.RELEASED),
            (self.state.LINKED, self.state.LINKED, self.state.CONFLICT),
            (self.state.RELEASED, self.state.CONFLICT, self.state.RELEASED),
            (self.state.CONFLICT, self.state.CONFLICT, self.state.CONFLICT),
        )
        for previous, after_success, after_failure in expected_states:
            for evidence, expected in (
                ("success_confirmed", after_success),
                ("failure_confirmed", after_failure),
            ):
                with self.subTest(previous=previous, evidence=evidence):
                    self.assertEqual(
                        self.domain.settle_binding(previous, evidence), expected
                    )

    def test_unknown_events_preserve_occupancy_and_confirmed_states(self):
        for evidence in (
            "timeout", "disconnected", "arbitrary", "", "success",
            "failure", "user_clicked_success",
        ):
            for previous, expected in (
                (self.state.RESERVED, self.state.UNKNOWN),
                (self.state.UNKNOWN, self.state.UNKNOWN),
                (self.state.LINKED, self.state.LINKED),
                (self.state.RELEASED, self.state.RELEASED),
                (self.state.CONFLICT, self.state.CONFLICT),
            ):
                with self.subTest(previous=previous, evidence=evidence):
                    self.assertEqual(
                        self.domain.settle_binding(previous, evidence), expected
                    )

    def test_cancel_only_releases_reserved_when_proven_not_sent(self):
        for previous in self.state:
            for sent in (False, True, None):
                with self.subTest(previous=previous, sent=sent):
                    expected = previous
                    if previous == self.state.RESERVED:
                        expected = (
                            self.state.RELEASED if sent is False
                            else self.state.UNKNOWN
                        )
                    self.assertEqual(
                        self.domain.cancel_binding(previous, sent=sent), expected
                    )

    def test_repeated_success_is_idempotent_and_cancel_cannot_undo_it(self):
        linked = self.domain.settle_binding(self.state.RESERVED, "success_confirmed")
        self.assertEqual(linked, self.state.LINKED)
        for _ in range(3):
            linked = self.domain.settle_binding(linked, "success_confirmed")
            self.assertEqual(linked, self.state.LINKED)
        for sent in (False, True, None):
            with self.subTest(sent=sent):
                self.assertEqual(
                    self.domain.cancel_binding(linked, sent=sent), self.state.LINKED
                )

    def test_late_contradictions_become_irreversible_conflicts(self):
        for previous, evidence in (
            (self.state.RELEASED, "success_confirmed"),
            (self.state.LINKED, "failure_confirmed"),
        ):
            with self.subTest(previous=previous, evidence=evidence):
                conflict = self.domain.settle_binding(previous, evidence)
                self.assertEqual(conflict, self.state.CONFLICT)
                for later_evidence in (
                    "success_confirmed", "failure_confirmed", "timeout", "arbitrary"
                ):
                    self.assertEqual(
                        self.domain.settle_binding(conflict, later_evidence),
                        self.state.CONFLICT,
                    )
                for sent in (False, True, None):
                    self.assertEqual(
                        self.domain.cancel_binding(conflict, sent=sent),
                        self.state.CONFLICT,
                    )

    def test_unknown_only_converges_on_confirmed_success_or_failure(self):
        unknown = self.domain.settle_binding(self.state.RESERVED, "timeout")
        self.assertEqual(unknown, self.state.UNKNOWN)
        for evidence in ("disconnected", "arbitrary", "user_clicked_success"):
            unknown = self.domain.settle_binding(unknown, evidence)
            self.assertEqual(unknown, self.state.UNKNOWN)
        for sent in (False, True, None):
            unknown = self.domain.cancel_binding(unknown, sent=sent)
            self.assertEqual(unknown, self.state.UNKNOWN)
        self.assertEqual(
            self.domain.settle_binding(unknown, "success_confirmed"),
            self.state.LINKED,
        )
        self.assertEqual(
            self.domain.settle_binding(unknown, "failure_confirmed"),
            self.state.RELEASED,
        )


if __name__ == "__main__":
    unittest.main()
