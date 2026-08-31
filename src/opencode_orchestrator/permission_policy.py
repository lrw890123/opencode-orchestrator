"""Deterministic permission and progress policy contracts.

The policy is deliberately small and data-only.  Normalization makes the
contract safe to persist in a task request and fingerprint, while evaluation
keeps the non-overridable safety gates ahead of user supplied rules.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import fnmatch
import shlex


KNOWN_PERMISSIONS = frozenset(
    {
        "read",
        "edit",
        "glob",
        "grep",
        "list",
        "bash",
        "task",
        "external_directory",
        "todowrite",
        "question",
        "webfetch",
        "websearch",
        "lsp",
        "doom_loop",
        "skill",
    }
)

# These fragments are intentionally conservative.  They are evaluated after
# surrounding each target with spaces so command names at the beginning of a
# bash request are covered too.
HIGH_RISK_FRAGMENTS = (
    "git push",
    "git reset --hard",
    "git rebase",
    "git filter-repo",
    "git filter-branch",
    " rm ",
    "rm -",
    "sudo ",
    "deploy",
    "publish",
    "docker push",
    "kubectl apply",
    "kubectl delete",
    "terraform apply",
    "terraform destroy",
    "gh pr create",
    "curl -x post",
    "curl --request post",
    "wget --post",
    "scp ",
    "rsync ",
    "credential",
    "authorization",
    "chmod ",
    "chown ",
    "drop table",
    "truncate table",
)


@dataclass(frozen=True)
class PermissionDecision:
    """The only outcomes the permission reconciler may produce."""

    action: str
    response: str | None
    reason: str


def _object(value: object | None, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return deepcopy(value)


def _normalize_external_pattern(pattern: str) -> str:
    """Normalize an absolute path or glob without touching the filesystem."""

    if not isinstance(pattern, str) or not pattern.startswith("/"):
        raise ValueError("external_directory allow patterns must be absolute")
    parts: list[str] = []
    for part in pattern.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("external_directory pattern escapes its absolute root")
            parts.pop()
        else:
            parts.append(part)
    return "/" + "/".join(parts)


def _normalize_relative_path(path: str) -> str | None:
    """Normalize a relative path and return ``None`` for root escape."""

    if not isinstance(path, str) or path.startswith("/"):
        return None
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def normalize_permission_policy(value: object | None) -> dict:
    raw = _object(value, "permission_policy")
    allowed = {"default", "persistence", "approval_basis", "rules"}
    unknown = sorted(str(key) for key in set(raw) - allowed)
    if unknown:
        raise ValueError(f"permission_policy has unknown keys: {', '.join(unknown)}")

    default = raw.get("default", "allow")
    persistence = raw.get("persistence", "task")
    basis = raw.get("approval_basis")
    rules = raw.get("rules", [])

    if not isinstance(default, str) or default not in {"allow", "ask", "deny"}:
        raise ValueError("permission_policy.default must be allow, ask, or deny")
    if not isinstance(persistence, str) or persistence not in {"task", "project"}:
        raise ValueError("permission_policy.persistence must be task or project")
    if basis is not None and (not isinstance(basis, str) or not basis.strip()):
        raise ValueError("permission_policy.approval_basis must be null or non-blank")
    if persistence == "project" and not basis:
        raise ValueError("project persistence requires approval_basis")
    if not isinstance(rules, list):
        raise ValueError("permission_policy.rules must be an array")

    normalized_rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) != {"permission", "pattern", "action"}:
            raise ValueError(f"permission_policy.rules[{index}] has invalid keys")
        permission = rule["permission"]
        pattern = rule["pattern"]
        action = rule["action"]
        if not isinstance(permission, str) or not permission.strip():
            raise ValueError(f"permission_policy.rules[{index}].permission must be non-blank")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"permission_policy.rules[{index}].pattern must be non-blank")
        if not isinstance(action, str) or action not in {"allow", "ask", "deny"}:
            raise ValueError(f"permission_policy.rules[{index}].action is invalid")
        if permission == "external_directory" and action == "allow":
            pattern = _normalize_external_pattern(pattern)
        normalized_rules.append(
            {"permission": permission, "pattern": pattern, "action": action}
        )

    return {
        "default": default,
        "persistence": persistence,
        "approval_basis": basis,
        "rules": normalized_rules,
    }


def normalize_progress_policy(value: object | None) -> dict:
    raw = _object(value, "progress_policy")
    allowed = {"input_probe_interval_seconds", "stall_timeout_seconds"}
    unknown = sorted(str(key) for key in set(raw) - allowed)
    if unknown:
        raise ValueError(f"progress_policy has unknown keys: {', '.join(unknown)}")

    probe = raw.get("input_probe_interval_seconds", 15)
    stall = raw.get("stall_timeout_seconds", 600)
    if not isinstance(probe, int) or isinstance(probe, bool) or not 5 <= probe <= 300:
        raise ValueError("input_probe_interval_seconds must be between 5 and 300")
    if not isinstance(stall, int) or isinstance(stall, bool) or not 30 <= stall <= 86400:
        raise ValueError("stall_timeout_seconds must be between 30 and 86400")
    return {"input_probe_interval_seconds": probe, "stall_timeout_seconds": stall}


def _rule_matches(rule: dict, permission: str, targets: list[str]) -> bool:
    return (
        rule["permission"] in {permission, "*"}
        and bool(targets)
        and all(fnmatch.fnmatchcase(target, rule["pattern"]) for target in targets)
    )


def _edit_relative_targets(
    targets: list[str], worktree_path: str | None
) -> list[str] | None:
    """Resolve edit resources to contract-relative paths lexically."""

    root: str | None = None
    if worktree_path is not None:
        try:
            root = _normalize_external_pattern(worktree_path)
        except ValueError:
            return None

    relative_targets: list[str] = []

    for target in targets:
        if target.startswith("/"):
            if root is None:
                return None
            try:
                normalized = _normalize_external_pattern(target)
            except ValueError:
                return None
            if root == "/":
                prefix = "/"
            else:
                prefix = f"{root}/"
            if normalized == root or not normalized.startswith(prefix):
                return None
            normalized = normalized[len(prefix) :]
        else:
            normalized = _normalize_relative_path(target)
            if normalized is None:
                return None
        if not normalized:
            return None
        relative_targets.append(normalized)
    return relative_targets


def _edit_targets_allowed(
    targets: list[str], allowed: list[str], worktree_path: str | None
) -> bool:
    relative_targets = _edit_relative_targets(targets, worktree_path)
    if relative_targets is None:
        return False

    root: str | None = None
    if worktree_path is not None:
        try:
            root = _normalize_external_pattern(worktree_path)
        except ValueError:
            return False

    if not isinstance(allowed, list) or not allowed:
        return False
    normalized_allowed: list[str] = []
    for pattern in allowed:
        if not isinstance(pattern, str) or not pattern.strip():
            return False
        if pattern.startswith("/"):
            if root is None:
                return False
            try:
                absolute_pattern = _normalize_external_pattern(pattern)
            except ValueError:
                return False
            prefix = "/" if root == "/" else f"{root}/"
            if absolute_pattern == root:
                normalized_allowed.append("")
            elif absolute_pattern.startswith(prefix):
                normalized_allowed.append(absolute_pattern[len(prefix) :])
            else:
                return False
        else:
            relative_pattern = _normalize_relative_path(pattern)
            if relative_pattern is None:
                return False
            normalized_allowed.append(relative_pattern)
    return all(
        any(fnmatch.fnmatchcase(target, pattern) for pattern in normalized_allowed)
        for target in relative_targets
    )


def _bash_is_indeterminate(command: str) -> bool:
    """Conservatively reject shell syntax whose effects are not single-valued."""

    try:
        if not shlex.split(command):
            return True
    except ValueError:
        return True
    # The policy has no shell interpreter.  Control flow, expansion, and
    # redirection make a command's effect depend on runtime state, so require
    # an explicit user decision for them.
    return any(
        marker in command
        for marker in (
            ";",
            "&&",
            "||",
            "|",
            ">",
            "<",
            "&",
            "\n",
            "\r",
            "$",
            "*",
            "?",
            "`",
            "$(",
            "${",
            "[",
            "]",
            "{",
            "}",
            "~",
        )
    )


def _relative_bash_operand(value: str) -> bool:
    """Accept only lexical worktree-relative operands for generic readers."""

    if not value or value.startswith(("/", "~")) or "://" in value:
        return False
    return ".." not in value.replace("\\", "/").split("/")


def _options_and_operands(
    arguments: list[str],
    *,
    flags: frozenset[str],
    valued: frozenset[str],
    short_valued: frozenset[str] = frozenset(),
) -> list[str] | None:
    """Parse a small option grammar and return its positional operands."""

    operands: list[str] = []
    index = 0
    options = True
    while index < len(arguments):
        token = arguments[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and token in flags:
            index += 1
            continue
        if options and token in valued:
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if options and any(
            token.startswith(f"{option}=") for option in valued if option.startswith("--")
        ):
            index += 1
            continue
        if options and any(
            token.startswith(option) and token != option for option in short_valued
        ):
            index += 1
            continue
        if options and token.startswith("-") and token != "-":
            return None
        operands.append(token)
        index += 1
    return operands


def _safe_git_read(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] != "git":
        return False
    subcommand = tokens[1]
    arguments = tokens[2:]
    grammars = {
        "status": (
            frozenset(
                {
                    "-s",
                    "--short",
                    "-b",
                    "--branch",
                    "--porcelain",
                    "--ignored",
                    "--no-renames",
                    "--renames",
                    "--ahead-behind",
                    "--no-ahead-behind",
                    "--show-stash",
                    "--no-show-stash",
                }
            ),
            frozenset({"-u", "--untracked-files", "--porcelain"}),
            frozenset(),
        ),
        "diff": (
            frozenset(
                {
                    "--cached",
                    "--staged",
                    "--stat",
                    "--numstat",
                    "--shortstat",
                    "--name-only",
                    "--name-status",
                    "--check",
                    "--summary",
                    "-p",
                    "--patch",
                    "--no-patch",
                    "--raw",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--color",
                    "--no-color",
                }
            ),
            frozenset({"-U", "--unified", "--diff-filter"}),
            frozenset({"-U"}),
        ),
        "log": (
            frozenset(
                {
                    "--oneline",
                    "--decorate",
                    "--no-decorate",
                    "--stat",
                    "--shortstat",
                    "--name-only",
                    "--name-status",
                    "--all",
                    "--graph",
                }
            ),
            frozenset({"-n", "--max-count", "--format", "--pretty"}),
            frozenset({"-n"}),
        ),
        "show": (
            frozenset(
                {
                    "--oneline",
                    "--stat",
                    "--shortstat",
                    "--name-only",
                    "--name-status",
                    "--summary",
                    "--no-patch",
                    "-p",
                    "--patch",
                }
            ),
            frozenset({"--format", "--pretty"}),
            frozenset(),
        ),
        "rev-parse": (
            frozenset(
                {
                    "--git-dir",
                    "--git-common-dir",
                    "--show-toplevel",
                    "--show-prefix",
                    "--is-inside-work-tree",
                    "--is-bare-repository",
                    "--show-current",
                    "--verify",
                    "--abbrev-ref",
                }
            ),
            frozenset({"--short"}),
            frozenset(),
        ),
        "branch": (
            frozenset({"--show-current", "--list", "--merged", "--no-merged"}),
            frozenset({"--contains", "--no-contains", "--format"}),
            frozenset(),
        ),
        "ls-files": (
            frozenset(
                {
                    "--cached",
                    "--deleted",
                    "--modified",
                    "--others",
                    "--ignored",
                    "--stage",
                    "--unmerged",
                    "--error-unmatch",
                }
            ),
            frozenset({"--exclude", "--exclude-from", "--exclude-standard"}),
            frozenset(),
        ),
    }
    grammar = grammars.get(subcommand)
    if grammar is None:
        return False
    operands = _options_and_operands(
        arguments,
        flags=grammar[0],
        valued=grammar[1],
        short_valued=grammar[2],
    )
    return operands is not None and all(_relative_bash_operand(item) for item in operands)


def _bash_is_safe_read_only(command: str) -> bool:
    """Recognize a deliberately narrow, option-aware read-only grammar."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or "/" in tokens[0]:
        return False
    if tokens[0] == "git":
        return _safe_git_read(tokens)
    if tokens[0] == "pwd":
        return all(token in {"-L", "-P", "--logical", "--physical"} for token in tokens[1:])

    grammars = {
        "ls": (
            frozenset({"-a", "-A", "-l", "-h", "-H", "-d", "-F", "-1"}),
            frozenset(),
            frozenset(),
        ),
        "rg": (
            frozenset(
                {
                    "-n",
                    "--line-number",
                    "-i",
                    "--ignore-case",
                    "-F",
                    "--fixed-strings",
                    "--files",
                    "--hidden",
                    "--no-ignore",
                    "--json",
                    "--count",
                    "--count-matches",
                    "--stats",
                }
            ),
            frozenset({"-e", "--regexp", "-g", "--glob", "-t", "--type"}),
            frozenset({"-e", "-g", "-t"}),
        ),
        "head": (
            frozenset(),
            frozenset({"-n", "--lines", "-c", "--bytes"}),
            frozenset({"-n", "-c"}),
        ),
        "tail": (
            frozenset(),
            frozenset({"-n", "--lines", "-c", "--bytes"}),
            frozenset({"-n", "-c"}),
        ),
        "wc": (
            frozenset({"-c", "-l", "-m", "-w", "-L", "--bytes", "--lines", "--chars", "--words"}),
            frozenset(),
            frozenset(),
        ),
    }
    grammar = grammars.get(tokens[0])
    if grammar is None:
        return False
    operands = _options_and_operands(
        tokens[1:],
        flags=grammar[0],
        valued=grammar[1],
        short_valued=grammar[2],
    )
    if operands is None:
        return False
    return bool(operands) and all(_relative_bash_operand(item) for item in operands)


def _safe_relative_test_selector(value: str) -> bool:
    return value != "-" and _relative_bash_operand(value)


def _safe_pytest_arguments(arguments: list[str]) -> bool:
    if any(argument.startswith("@") for argument in arguments):
        return False
    operands = _options_and_operands(
        arguments,
        flags=frozenset(
            {
                "-q",
                "--quiet",
                "-v",
                "--verbose",
                "-x",
                "--exitfirst",
                "--collect-only",
                "--co",
            }
        ),
        valued=frozenset(),
    )
    return operands is not None and all(
        _safe_relative_test_selector(item) for item in operands
    )


def _safe_unittest_discover_arguments(arguments: list[str]) -> bool:
    flags = frozenset(
        {
            "-v",
            "--verbose",
            "-q",
            "--quiet",
            "-f",
            "--failfast",
            "-c",
            "--catch",
            "-b",
            "--buffer",
        }
    )
    relative_valued = frozenset(
        {"-s", "--start-directory", "-t", "--top-level-directory"}
    )
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in flags:
            index += 1
            continue
        if token in relative_valued:
            if index + 1 >= len(arguments) or not _safe_relative_test_selector(
                arguments[index + 1]
            ):
                return False
            index += 2
            continue
        matched = next(
            (
                option
                for option in relative_valued
                if option.startswith("--") and token.startswith(f"{option}=")
            ),
            None,
        )
        if matched is not None:
            if not _safe_relative_test_selector(token.split("=", 1)[1]):
                return False
            index += 1
            continue
        return False
    return True


def _safe_unittest_arguments(arguments: list[str]) -> bool:
    if arguments and arguments[0] == "discover":
        return _safe_unittest_discover_arguments(arguments[1:])
    operands = _options_and_operands(
        arguments,
        flags=frozenset(
            {
                "-v",
                "--verbose",
                "-q",
                "--quiet",
                "-f",
                "--failfast",
                "-c",
                "--catch",
                "-b",
                "--buffer",
            }
        ),
        valued=frozenset(),
    )
    return operands is not None and all(
        _safe_relative_test_selector(item) for item in operands
    )


def _bash_is_exact_contract_test(command: str, task_contract: dict) -> bool:
    tests = task_contract.get("test_commands") if isinstance(task_contract, dict) else None
    exact = isinstance(tests, list) and any(
        isinstance(item, str) and item == command for item in tests
    )
    if not exact:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or "/" in tokens[0]:
        return False

    # Contract membership is authorization for a test runner, not arbitrary
    # interpreter or wrapper code.  Keep this grammar intentionally small:
    # ``-c``/``-e``, scripts, shells, env/sudo wrappers, and other executable
    # forms remain input-required even if copied verbatim into test_commands.
    executable = tokens[0]
    if executable in {"python", "python3"}:
        if len(tokens) < 3 or tokens[1] != "-m":
            return False
        if tokens[2] == "pytest":
            return _safe_pytest_arguments(tokens[3:])
        if tokens[2] == "unittest":
            return _safe_unittest_arguments(tokens[3:])
        return False
    if executable in {"pytest", "py.test"}:
        return _safe_pytest_arguments(tokens[1:])
    return False


def _bash_high_risk(command: str) -> bool:
    """Catch write/destructive/security commands beyond the fixed fragments."""

    try:
        raw_tokens = shlex.split(command)
        tokens = [token.lower() for token in raw_tokens]
    except ValueError:
        return True
    if not tokens:
        return True
    command_name = tokens[0].rsplit("/", 1)[-1]
    if command_name in {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "mv",
        "cp",
        "dd",
        "install",
        "tee",
        "touch",
        "mkdir",
        "mkfifo",
        "truncate",
        "setfacl",
        "useradd",
        "usermod",
    }:
        return True
    if command_name in {"scp", "rsync", "ssh", "sftp", "ftp"}:
        return True
    if command_name in {"http", "httpie", "xh"}:
        if any(token in {"post", "put", "patch", "delete"} for token in tokens[1:]):
            return True
    if command_name in {"curl", "wget"}:
        write_options = {
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-urlencode",
            "--form",
            "--form-string",
            "--post-data",
            "--post-file",
            "--upload-file",
        }
        if any(
            token in write_options
            or token.startswith(
                (
                    "--data=",
                    "--data-raw=",
                    "--data-binary=",
                    "--data-urlencode=",
                    "--form=",
                    "--form-string=",
                    "--upload-file=",
                )
            )
            or token in {"-xpost", "-xput", "-xpatch", "-xdelete"}
            for token in tokens[1:]
        ):
            return True
        if command_name == "curl":
            # Short curl upload/data options are case-sensitive and accept
            # their argument directly after the option (for example -dfoo,
            # -Ffoo, and -Tfoo).  Keep raw spelling so -f remains curl's safe
            # fail-on-error read flag rather than being confused with -F.
            for raw_token in raw_tokens[1:]:
                if raw_token in {"-d", "-F", "-T"}:
                    return True
                if len(raw_token) > 2 and raw_token[:2] in {"-d", "-F", "-T"}:
                    return True
        if command_name == "curl" and "-t" in tokens[1:]:
            return True
        for token in tokens[1:]:
            if token.startswith(("--request=", "--method=")):
                if token.split("=", 1)[1] in {"post", "put", "patch", "delete"}:
                    return True
        for index, token in enumerate(tokens[1:], start=1):
            if token in {"-x", "--request", "--method"} and index + 1 < len(tokens):
                if tokens[index + 1] in {"post", "put", "patch", "delete"}:
                    return True
    if command_name == "git" and len(tokens) > 1:
        # Global options such as ``git -C repo push`` occur before the
        # subcommand and must not hide a history/remote-write operation.
        if any(
            token in {"push", "rebase", "filter-repo", "filter-branch", "clean"}
            for token in tokens[1:]
        ):
            return True
        if "reset" in tokens and "--hard" in tokens:
            return True
        if "commit" in tokens and "--amend" in tokens:
            return True
    if command_name == "aws" and any(
        token in {"cp", "sync", "put", "upload"} for token in tokens[1:]
    ):
        return True
    if command_name in {"helm", "pulumi", "ansible-playbook"} and any(
        token in {"install", "upgrade", "uninstall", "up", "apply"}
        for token in tokens[1:]
    ):
        return True
    if command_name == "kubectl" and any(
        token in {"apply", "delete", "patch", "rollout", "replace", "scale"}
        for token in tokens[1:]
    ):
        return True
    if command_name in {"dropdb", "mysql", "psql", "sqlite3"}:
        joined = " ".join(tokens)
        if any(
            marker in joined
            for marker in (
                "drop table",
                "drop database",
                "truncate table",
                "delete from",
                "insert into",
                "update ",
            )
        ):
            return True
    joined = " ".join(tokens)
    return any(
        marker in joined
        for marker in (
            "delete from",
            "drop database",
            "insert into",
            "update ",
            "npm publish",
            "twine upload",
            "docker login",
            "security",
            "credential",
            "authorization",
            "password",
            "passwd",
            "secret",
            "token",
            "api key",
            "access key",
            "iptables",
            "kubectl patch",
            "gh api",
            "--method post",
            "--method put",
            "requests.post",
            "requests.put",
            "httpx.post",
            "urllib.request.urlopen",
            ".unlink(",
            ".remove(",
            ".rmdir(",
            "os.unlink(",
            "os.remove(",
            "shutil.rmtree(",
            "pathlib.path.unlink(",
        )
    )


def _has_high_risk_action(permission: str, targets: list[str]) -> bool:
    lowered = "\n".join(f" {target.strip().lower()} " for target in targets)
    if any(fragment in lowered for fragment in HIGH_RISK_FRAGMENTS):
        return True
    return permission == "bash" and any(_bash_high_risk(target) for target in targets)


def _normalize_request_targets(permission: str, targets: list[str]) -> list[str] | None:
    if permission != "external_directory":
        return targets
    normalized: list[str] = []
    for target in targets:
        if not target.startswith("/"):
            return None
        try:
            normalized.append(_normalize_external_pattern(target))
        except ValueError:
            return None
    return normalized


def evaluate_permission(
    policy: dict,
    request: dict,
    task_contract: dict,
    worktree_path: str | None = None,
) -> PermissionDecision:
    """Evaluate one normalized OpenCode permission request fail-closed."""

    # Re-normalizing here makes direct compatibility callers receive the same
    # validation and prevents a hand-built policy from bypassing contract
    # defaults or enum checks.
    policy = normalize_permission_policy(policy)
    if not isinstance(request, dict):
        return PermissionDecision("ask", None, "indeterminate-request")
    permission = request.get("permission")
    targets = request.get("patterns")
    if not isinstance(permission, str) or permission not in KNOWN_PERMISSIONS:
        return PermissionDecision("ask", None, "unknown-permission")
    if not isinstance(targets, list) or not targets or any(
        not isinstance(target, str) or not target.strip() for target in targets
    ):
        return PermissionDecision("ask", None, "indeterminate-target")
    if permission == "bash" and any(_bash_is_indeterminate(target) for target in targets):
        return PermissionDecision("ask", None, "indeterminate-bash")

    normalized_targets = _normalize_request_targets(permission, targets)
    if normalized_targets is None:
        return PermissionDecision("ask", None, "indeterminate-target")
    if _has_high_risk_action(permission, targets):
        return PermissionDecision("ask", None, "high-risk-action")
    if permission == "edit":
        relative_targets = _edit_relative_targets(targets, worktree_path)
        if relative_targets is None or not _edit_targets_allowed(
            targets,
            (task_contract or {}).get("allowed_paths", [])
            if isinstance(task_contract, dict)
            else [],
            worktree_path,
        ):
            return PermissionDecision("ask", None, "edit-outside-contract")
        normalized_targets = relative_targets

    forbidden = []
    if isinstance(task_contract, dict):
        forbidden = [
            item.strip().lower()
            for item in task_contract.get("forbidden_actions", [])
            if isinstance(item, str) and item.strip()
        ]
    lowered = "\n".join(f" {target.strip().lower()} " for target in targets)
    if any(item in lowered for item in forbidden):
        return PermissionDecision("deny", "reject", "explicit-contract-prohibition")

    if permission == "bash" and not all(
        _bash_is_exact_contract_test(target, task_contract)
        or _bash_is_safe_read_only(target)
        for target in targets
    ):
        return PermissionDecision("ask", None, "unsupported-bash")

    matched = None
    rule_target_variants = [normalized_targets]
    if permission == "edit" and normalized_targets != targets:
        # OpenCode versions have emitted both worktree-absolute resources and
        # contract-relative resources.  Let an explicit rule use either form
        # while retaining one ordered last-match-wins rule sequence.
        rule_target_variants.append(targets)
    for rule in policy["rules"]:
        if any(_rule_matches(rule, permission, candidate) for candidate in rule_target_variants):
            matched = rule

    if permission == "external_directory" and (
        matched is None or matched["action"] != "allow"
    ):
        if matched is not None and matched["action"] == "deny":
            return PermissionDecision("deny", "reject", "explicit-rule-deny")
        return PermissionDecision("ask", None, "external-directory-not-declared")

    action = matched["action"] if matched is not None else policy["default"]
    if action == "ask":
        return PermissionDecision(
            "ask", None, "explicit-rule-ask" if matched else "default-ask"
        )
    if action == "deny":
        return PermissionDecision(
            "deny",
            "reject",
            "explicit-rule-deny" if matched else "default-deny",
        )
    response = "always" if policy["persistence"] == "project" else "once"
    return PermissionDecision(
        "allow",
        response,
        "explicit-rule-allow" if matched else "default-allow",
    )


__all__ = [
    "HIGH_RISK_FRAGMENTS",
    "KNOWN_PERMISSIONS",
    "PermissionDecision",
    "evaluate_permission",
    "normalize_permission_policy",
    "normalize_progress_policy",
]
