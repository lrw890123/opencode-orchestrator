from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.git_workspace import GitWorkspace


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def create_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    return path


class GitWorkspaceTest(unittest.TestCase):
    def test_prepare_uses_exact_head_and_excludes_source_untracked_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_repo(root / "source")
            (source / "local.txt").write_text("user state\n", encoding="utf-8")
            workspace = GitWorkspace(source)
            facts = workspace.facts()

            prepared = workspace.prepare(
                managed_root=root / "managed",
                task_id="oc-20260829-173000-a1b2c3d4",
                slug="Multiply Demo",
            )

            self.assertEqual(prepared.base_sha, facts.head_sha)
            self.assertEqual(prepared.branch, "opencode/20260829-multiply-demo-a1b2c3d4")
            self.assertEqual(git(prepared.path, "rev-parse", "HEAD"), facts.head_sha)
            self.assertFalse((prepared.path / "local.txt").exists())
            self.assertEqual(workspace.dirty_fingerprint(), facts.dirty_fingerprint)

    def test_changed_files_and_untracked_files_are_reported_separately(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_repo(root / "source")
            prepared = GitWorkspace(source).prepare(
                root / "managed",
                "oc-20260829-173000-a1b2c3d4",
                "demo",
            )
            (prepared.path / "README.md").write_text("base\nchanged\n", encoding="utf-8")
            (prepared.path / "new.txt").write_text("new\n", encoding="utf-8")
            worktree = GitWorkspace(prepared.path)

            self.assertEqual(worktree.changed_files(prepared.base_sha), ["README.md"])
            self.assertEqual(worktree.untracked_files(), ["new.txt"])
            self.assertEqual(
                worktree.diff_stat(prepared.base_sha),
                {"files": 1, "additions": 1, "deletions": 0},
            )


if __name__ == "__main__":
    unittest.main()
