from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess


class GitCommandError(RuntimeError):
    """Raised when a Git operation fails."""


@dataclass(frozen=True)
class GitFacts:
    repo_root: Path
    head_sha: str
    branch: str
    dirty_fingerprint: str


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    branch: str
    base_sha: str


def _run(repo: Path, *args: str, text: bool = True):
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            command,
            check=True,
            text=text,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr
        raise GitCommandError(f"git command failed: {' '.join(command)}: {stderr.strip()}") from error


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:40] or "task"


class GitWorkspace:
    def __init__(self, repo: Path):
        candidate = Path(repo).expanduser().resolve()
        root = _run(candidate, "rev-parse", "--show-toplevel").stdout.strip()
        self.repo_root = Path(root).resolve()

    def facts(self) -> GitFacts:
        branch = _run(self.repo_root, "branch", "--show-current").stdout.strip() or "(detached)"
        return GitFacts(
            repo_root=self.repo_root,
            head_sha=_run(self.repo_root, "rev-parse", "HEAD").stdout.strip(),
            branch=branch,
            dirty_fingerprint=self.dirty_fingerprint(),
        )

    def dirty_fingerprint(self) -> str:
        result = _run(self.repo_root, "status", "--porcelain=v1", "-z", text=False)
        return hashlib.sha256(result.stdout).hexdigest()

    def prepare(self, managed_root: Path, task_id: str, slug: str) -> PreparedWorkspace:
        facts = self.facts()
        managed = Path(managed_root).expanduser().resolve()
        repo_hash = hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:12]
        destination = managed / "worktrees" / repo_hash / task_id
        if destination.exists():
            raise FileExistsError(f"managed worktree already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        parts = task_id.split("-")
        if len(parts) < 4 or parts[0] != "oc":
            raise ValueError(f"invalid task id: {task_id}")
        date = parts[1]
        short_id = parts[-1]
        branch = f"opencode/{date}-{_slug(slug)}-{short_id}"
        _run(self.repo_root, "worktree", "add", "-b", branch, str(destination), facts.head_sha)
        return PreparedWorkspace(destination.resolve(), branch, facts.head_sha)

    def changed_files(self, base_sha: str) -> list[str]:
        output = _run(self.repo_root, "diff", "--name-only", base_sha, "--").stdout
        return sorted(line for line in output.splitlines() if line)

    def untracked_files(self) -> list[str]:
        result = _run(
            self.repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            text=False,
        )
        return sorted(path.decode() for path in result.stdout.split(b"\0") if path)

    def diff_stat(self, base_sha: str) -> dict[str, int]:
        output = _run(self.repo_root, "diff", "--numstat", base_sha, "--").stdout
        additions = 0
        deletions = 0
        files = 0
        for line in output.splitlines():
            added, deleted, _path = line.split("\t", 2)
            files += 1
            if added.isdigit():
                additions += int(added)
            if deleted.isdigit():
                deletions += int(deleted)
        return {"files": files, "additions": additions, "deletions": deletions}
