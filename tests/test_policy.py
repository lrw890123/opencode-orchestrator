import unittest

from opencode_orchestrator.policy import classify_risk, validate_allowed_paths


class PolicyTest(unittest.TestCase):
    def test_small_single_module_change_is_low_risk(self):
        result = classify_risk(
            file_count=2,
            line_count=80,
            cross_module=False,
            public_interface=False,
            dependency_change=False,
            high_risk_actions=[],
        )

        self.assertEqual(result.level, "low")
        self.assertFalse(result.user_approval_required)
        self.assertEqual(result.reasons, ())

    def test_size_threshold_requires_approval(self):
        result = classify_risk(
            file_count=6,
            line_count=80,
            cross_module=False,
            public_interface=False,
            dependency_change=False,
            high_risk_actions=[],
        )

        self.assertEqual(result.level, "large")
        self.assertTrue(result.user_approval_required)
        self.assertIn("more than 5 files", result.reasons)

    def test_dependency_change_requires_approval(self):
        result = classify_risk(
            file_count=1,
            line_count=5,
            cross_module=False,
            public_interface=False,
            dependency_change=True,
            high_risk_actions=[],
        )

        self.assertEqual(result.level, "large")
        self.assertIn("dependency change", result.reasons)

    def test_destructive_action_is_high_risk(self):
        result = classify_risk(
            file_count=1,
            line_count=1,
            cross_module=False,
            public_interface=False,
            dependency_change=False,
            high_risk_actions=["git-history-rewrite"],
        )

        self.assertEqual(result.level, "high")
        self.assertTrue(result.user_approval_required)
        self.assertEqual(result.reasons, ("high-risk action: git-history-rewrite",))

    def test_changed_path_must_match_allowlist(self):
        self.assertEqual(
            validate_allowed_paths(
                ["src/a.py", "tests/test_a.py"],
                ["src/**", "tests/**"],
            ),
            [],
        )
        self.assertEqual(
            validate_allowed_paths(
                ["src/a.py", "secrets.txt"],
                ["src/**"],
            ),
            ["secrets.txt"],
        )


if __name__ == "__main__":
    unittest.main()
