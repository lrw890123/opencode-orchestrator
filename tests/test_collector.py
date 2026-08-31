from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.collector import collect_git_evidence, last_assistant_text, truncate_text
from tests.test_git_workspace import create_repo


class CollectorTest(unittest.TestCase):
    def test_last_assistant_text_ignores_tool_only_messages(self):
        messages = [
            {"info": {"role": "assistant"}, "parts": [{"type": "tool"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            },
        ]

        self.assertEqual(last_assistant_text(messages), "first\nsecond")

    def test_truncate_text_reports_when_content_is_omitted(self):
        self.assertEqual(truncate_text("abcd", limit=3), ("abc", True))
        self.assertEqual(truncate_text("abc", limit=3), ("abc", False))

    def test_collect_marks_out_of_scope_files(self):
        with TemporaryDirectory() as tmp:
            repo = create_repo(Path(tmp) / "repo")
            base_sha = __import__("subprocess").run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
            (repo / "secrets.txt").write_text("not a real secret\n", encoding="utf-8")

            evidence = collect_git_evidence(repo, base_sha, ["README.md"])

            self.assertEqual(evidence["changed_files"], ["README.md"])
            self.assertEqual(evidence["untracked_files"], ["secrets.txt"])
            self.assertEqual(evidence["out_of_scope"], ["secrets.txt"])
            self.assertEqual(evidence["diff_stat"], {"files": 1, "additions": 1, "deletions": 0})


if __name__ == "__main__":
    unittest.main()
