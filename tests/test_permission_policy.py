import unittest

from opencode_orchestrator.permission_policy import (
    PermissionDecision,
    evaluate_permission,
    normalize_permission_policy,
    normalize_progress_policy,
)


class PermissionPolicyTest(unittest.TestCase):
    def test_defaults_are_task_local_allow_and_bounded_progress(self):
        self.assertEqual(
            normalize_permission_policy(None),
            {"default": "allow", "persistence": "task", "approval_basis": None, "rules": []},
        )
        self.assertEqual(
            normalize_progress_policy(None),
            {"input_probe_interval_seconds": 15, "stall_timeout_seconds": 600},
        )

    def test_normalizers_reject_unknown_keys_and_invalid_ranges(self):
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            normalize_permission_policy({"unexpected": True})
        with self.assertRaisesRegex(ValueError, "between 5 and 300"):
            normalize_progress_policy({"input_probe_interval_seconds": 4})
        with self.assertRaisesRegex(ValueError, "between 30 and 86400"):
            normalize_progress_policy({"stall_timeout_seconds": 29})

    def test_project_persistence_requires_approval_basis(self):
        with self.assertRaisesRegex(ValueError, "approval_basis"):
            normalize_permission_policy({"persistence": "project"})

    def test_project_persistence_returns_always_only_with_basis(self):
        policy = normalize_permission_policy(
            {"persistence": "project", "approval_basis": "approved by user"}
        )
        decision = evaluate_permission(policy, {"permission": "read", "patterns": ["README.md"]}, {})
        self.assertEqual((decision.action, decision.response), ("allow", "always"))

    def test_external_directory_requires_exact_absolute_allow_rule(self):
        request = {
            "request_id": "per_1",
            "session_id": "ses_1",
            "permission": "external_directory",
            "patterns": ["/refs/old-tree/src/a.py"],
            "metadata": {},
            "message_id": "msg_1",
            "call_id": "call_1",
        }
        ask = evaluate_permission(normalize_permission_policy(None), request, {})
        allow = evaluate_permission(
            normalize_permission_policy(
                {
                    "rules": [
                        {
                            "permission": "external_directory",
                            "pattern": "/refs/old-tree/**",
                            "action": "allow",
                        }
                    ]
                }
            ),
            request,
            {},
        )
        self.assertEqual((ask.action, ask.response), ("ask", None))
        self.assertEqual((allow.action, allow.response), ("allow", "once"))

    def test_external_directory_allow_patterns_must_be_absolute_and_cannot_escape(self):
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            normalize_permission_policy(
                {
                    "rules": [
                        {
                            "permission": "external_directory",
                            "pattern": "refs/**",
                            "action": "allow",
                        }
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "escapes"):
            normalize_permission_policy(
                {
                    "rules": [
                        {
                            "permission": "external_directory",
                            "pattern": "/refs/../../outside/**",
                            "action": "allow",
                        }
                    ]
                }
            )

    def test_external_directory_paths_are_normalized_lexically(self):
        policy = normalize_permission_policy(
            {
                "rules": [
                    {
                        "permission": "external_directory",
                        "pattern": "/refs/old-tree/./src/../src/**",
                        "action": "allow",
                    }
                ]
            }
        )
        request = {
            "permission": "external_directory",
            "patterns": ["/refs/old-tree/src/./nested/../a.py"],
        }
        self.assertEqual(evaluate_permission(policy, request, {}).action, "allow")
        outside = dict(request, patterns=["/refs/old-tree/src/../../outside/a.py"])
        self.assertEqual(evaluate_permission(policy, outside, {}).action, "ask")

    def test_unknown_and_high_risk_bash_never_follow_default_allow(self):
        unknown = {"permission": "future.capability", "patterns": ["x"]}
        push = {"permission": "bash", "patterns": ["git push origin main"]}
        policy = normalize_permission_policy(None)
        self.assertEqual(evaluate_permission(policy, unknown, {}).action, "ask")
        self.assertEqual(evaluate_permission(policy, push, {}).action, "ask")

    def test_high_risk_remote_deploy_security_and_destructive_sql_are_asked(self):
        policy = normalize_permission_policy(
            {
                "rules": [
                    {"permission": "bash", "pattern": "*", "action": "allow"},
                ]
            }
        )
        for command in (
            "curl -X POST https://example.test/hook",
            "curl -XPOST https://example.test/hook",
            "curl --request=POST https://example.test/hook",
            "curl -dfoo https://example.test/hook",
            "curl -Ffoo https://example.test/hook",
            "curl -Tfoo https://example.test/hook",
            "curl --form-string foo https://example.test/hook",
            "curl --upload-file=foo https://example.test/hook",
            "http POST https://example.test/hook value=1",
            "ssh deploy@example.test touch /srv/release",
            "aws s3 cp artifact.tgz s3://releases/",
            "helm upgrade app ./chart",
            "kubectl apply -f deployment.yaml",
            "chmod 777 config.json",
            "git reset --hard HEAD~1",
            "git -C /repo push origin main",
            "git commit --amend --no-edit",
            "DROP TABLE users",
            "rm -rf build",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(policy, {"permission": "bash", "patterns": [command]}, {})
                self.assertEqual(decision.action, "ask")
                self.assertIsNone(decision.response)

    def test_malformed_and_indeterminate_bash_is_asked(self):
        policy = normalize_permission_policy(None)
        for command in (
            'echo "unterminated',
            "echo $(cat secret.txt)",
            "echo hi > output.txt",
            "echo ok & touch output.txt",
            "echo ok\ntouch /tmp/x",
            "echo $TARGET",
            "echo *.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    evaluate_permission(policy, {"permission": "bash", "patterns": [command]}, {}).action,
                    "ask",
                )

    def test_default_allow_bash_rejects_mutation_interpreters_and_shell_expansion(self):
        policy = normalize_permission_policy(None)
        for command in (
            "mv README.md README.old",
            "dd if=/dev/zero of=README.md",
            "python3 -c \"__import__('pathlib').Path('README.md').unlink()\"",
            "r{m,} -rf build",
            "r[m] -rf build",
            "echo ~root",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    policy,
                    {"permission": "bash", "patterns": [command]},
                    {},
                )
                self.assertEqual((decision.action, decision.response), ("ask", None))

    def test_narrow_read_only_bash_commands_follow_default_policy(self):
        for command in (
            "pwd",
            "git status --short",
            "git diff -- README.md",
            "git log -n 3 --oneline",
            "rg -n TODO README.md",
            "head -n 5 README.md",
            "wc -l README.md",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    normalize_permission_policy(None),
                    {"permission": "bash", "patterns": [command]},
                    {},
                )
                self.assertEqual((decision.action, decision.response), ("allow", "once"))

    def test_exact_contract_test_command_is_allowed_after_safety_gates(self):
        command = "python3 -m unittest tests.test_permission_policy -v"
        decision = evaluate_permission(
            normalize_permission_policy(None),
            {"permission": "bash", "patterns": [command]},
            {"test_commands": [command]},
        )
        destructive = "python3 -c \"__import__('os').remove('README.md')\""
        destructive_decision = evaluate_permission(
            normalize_permission_policy(None),
            {"permission": "bash", "patterns": [destructive]},
            {"test_commands": [destructive]},
        )

        self.assertEqual((decision.action, decision.response), ("allow", "once"))
        self.assertEqual(
            (destructive_decision.action, destructive_decision.response),
            ("ask", None),
        )

    def test_exact_contract_cannot_bless_interpreters_or_shell_wrappers(self):
        policy = normalize_permission_policy(None)
        for command in (
            'python3 -c \'open("important.db","w").write("x")\'',
            "perl -e 'unlink \"important.db\"'",
            "bash -lc 'python3 -m unittest tests.test_permission_policy -v'",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    policy,
                    {"permission": "bash", "patterns": [command]},
                    {"test_commands": [command]},
                )
                self.assertEqual((decision.action, decision.response), ("ask", None))

    def test_exact_contract_test_runner_rejects_paths_and_mutating_options(self):
        policy = normalize_permission_policy(None)
        for command in (
            "pytest @/absolute/args.txt",
            "pytest @relative.txt",
            "pytest --basetemp=/absolute/existing-dir tests",
            "pytest --basetemp=.pytest-tmp tests",
            "pytest --cache-clear tests",
            "pytest --junitxml=reports/results.xml tests",
            "pytest --html=reports/results.html tests",
            "pytest --unknown-safe-looking tests",
            "pytest -q /absolute/tests/test_example.py",
            "python3 -m pytest --basetemp=.pytest-tmp tests",
            "python3 -m unittest discover -s /absolute/tests -v",
            "python3 -m unittest /absolute/tests/test_example.py -q",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    policy,
                    {"permission": "bash", "patterns": [command]},
                    {"test_commands": [command]},
                )
                self.assertEqual((decision.action, decision.response), ("ask", None))

    def test_exact_contract_test_runner_allows_safe_relative_selection(self):
        policy = normalize_permission_policy(None)
        for command in (
            "pytest -q tests/test_permission_policy.py",
            "python3 -m pytest -q tests/test_permission_policy.py::PermissionPolicyTest",
            "python3 -m unittest discover -s tests -t . -v",
            "python3 -m unittest tests.test_permission_policy -q",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    policy,
                    {"permission": "bash", "patterns": [command]},
                    {"test_commands": [command]},
                )
                self.assertEqual((decision.action, decision.response), ("allow", "once"))

    def test_network_and_unsupported_bash_commands_require_input(self):
        for command in (
            "curl -f https://example.test/data",
            "wget -O - https://example.test/data",
            "find . -delete",
            "sed -i '' -e s/old/new/ README.md",
            "env git status",
        ):
            with self.subTest(command=command):
                decision = evaluate_permission(
                    normalize_permission_policy(None),
                    {"permission": "bash", "patterns": [command]},
                    {},
                )
                self.assertEqual((decision.action, decision.response), ("ask", None))

    def test_explicit_rules_are_case_sensitive_last_match_wins_and_cover_all_targets(self):
        policy = normalize_permission_policy(
            {
                "default": "deny",
                "rules": [
                    {"permission": "read", "pattern": "src/**", "action": "allow"},
                    {"permission": "read", "pattern": "src/private/**", "action": "deny"},
                    {"permission": "*", "pattern": "tests/**", "action": "allow"},
                ],
            }
        )
        self.assertEqual(
            evaluate_permission(policy, {"permission": "read", "patterns": ["src/a.py"]}, {}).action,
            "allow",
        )
        self.assertEqual(
            evaluate_permission(policy, {"permission": "read", "patterns": ["src/private/a.py"]}, {}).action,
            "deny",
        )
        self.assertEqual(
            evaluate_permission(policy, {"permission": "read", "patterns": ["src/a.py", "other.py"]}, {}).action,
            "deny",
        )
        self.assertEqual(
            evaluate_permission(policy, {"permission": "READ", "patterns": ["src/a.py"]}, {}).action,
            "ask",
        )

    def test_edit_targets_must_stay_within_task_contract_and_worktree(self):
        policy = normalize_permission_policy(None)
        contract = {"allowed_paths": ["src/**"]}
        self.assertEqual(
            evaluate_permission(
                policy,
                {"permission": "edit", "patterns": ["src/a.py"]},
                contract,
            ).action,
            "allow",
        )
        self.assertEqual(
            evaluate_permission(
                policy,
                {"permission": "edit", "patterns": ["tests/a.py"]},
                contract,
            ).action,
            "ask",
        )
        self.assertEqual(
            evaluate_permission(
                policy,
                {"permission": "edit", "patterns": ["/worktree/src/a.py"]},
                contract,
                "/worktree",
            ).action,
            "allow",
        )
        self.assertEqual(
            evaluate_permission(
                policy,
                {"permission": "edit", "patterns": ["/worktree/src/a.py"]},
                {"allowed_paths": ["/worktree/src/**"]},
                "/worktree",
            ).action,
            "allow",
        )
        self.assertEqual(
            evaluate_permission(
                policy,
                {"permission": "edit", "patterns": ["/worktree/../outside.py"]},
                contract,
                "/worktree",
            ).action,
            "ask",
        )

    def test_edit_rule_matching_uses_contract_relative_normalized_targets(self):
        policy = normalize_permission_policy(
            {
                "default": "deny",
                "rules": [
                    {"permission": "edit", "pattern": "src/**", "action": "allow"},
                ],
            }
        )
        decision = evaluate_permission(
            policy,
            {"permission": "edit", "patterns": ["/worktree/src/./nested/../a.py"]},
            {"allowed_paths": ["src/**"]},
            "/worktree",
        )
        self.assertEqual(decision.action, "allow")
        absolute_rule_policy = normalize_permission_policy(
            {
                "default": "deny",
                "rules": [
                    {"permission": "edit", "pattern": "/worktree/src/**", "action": "allow"},
                ],
            }
        )
        absolute_decision = evaluate_permission(
            absolute_rule_policy,
            {"permission": "edit", "patterns": ["/worktree/src/a.py"]},
            {"allowed_paths": ["src/**"]},
            "/worktree",
        )
        self.assertEqual(absolute_decision.action, "allow")

    def test_contract_prohibition_denies_matching_safe_request(self):
        decision = evaluate_permission(
            normalize_permission_policy(None),
            {"permission": "bash", "patterns": ["echo do-not-delete"]},
            {"forbidden_actions": ["do-not-delete"]},
        )
        self.assertEqual(decision, PermissionDecision("deny", "reject", "explicit-contract-prohibition"))


if __name__ == "__main__":
    unittest.main()
