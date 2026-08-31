from __future__ import annotations

from dataclasses import dataclass
import fnmatch


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    user_approval_required: bool
    reasons: tuple[str, ...]


def classify_risk(
    *,
    file_count: int,
    line_count: int,
    cross_module: bool,
    public_interface: bool,
    dependency_change: bool,
    high_risk_actions: list[str],
) -> RiskAssessment:
    if file_count < 0 or line_count < 0:
        raise ValueError("file_count and line_count must be non-negative")
    if high_risk_actions:
        reasons = tuple(f"high-risk action: {action}" for action in sorted(high_risk_actions))
        return RiskAssessment("high", True, reasons)

    reasons: list[str] = []
    if file_count > 5:
        reasons.append("more than 5 files")
    if line_count > 300:
        reasons.append("more than 300 lines")
    if cross_module:
        reasons.append("cross-module change")
    if public_interface:
        reasons.append("public interface change")
    if dependency_change:
        reasons.append("dependency change")
    if reasons:
        return RiskAssessment("large", True, tuple(reasons))
    return RiskAssessment("low", False, ())


def validate_allowed_paths(changed_paths: list[str], patterns: list[str]) -> list[str]:
    violations = [
        path
        for path in sorted(set(changed_paths))
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    ]
    return violations
