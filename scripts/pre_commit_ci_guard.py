# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Block local commits when the current HEAD has no fresh CI report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPORT_RELATIVE_PATH = Path(".ci_cache") / "report.json"


class CiGuardError(RuntimeError):
    """Base error for the pre-commit CI freshness guard."""


class GitCommandError(CiGuardError):
    """A git command required by the guard did not succeed."""


class ReportValidationError(CiGuardError):
    """The local CI report is missing or cannot be trusted."""


@dataclass(frozen=True)
class HeadState:
    """Current repository HEAD state."""

    state: str
    commit: str | None
    ref_name: str | None


@dataclass(frozen=True)
class CiReportState:
    """Subset of ``.ci_cache/report.json`` needed by the guard."""

    passed: bool | None
    overall_status: str | None
    profile: str | None
    finished_at_utc: str | None
    head_state: str | None
    head_commit: str | None


def _default_repo_root() -> Path:
    """Return the repository root inferred from this source file."""

    return Path(__file__).resolve().parents[1]


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a fixed-argv git command inside ``repo_root``."""

    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        details = proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed"
        raise GitCommandError(details)
    return proc


def resolve_head_state(repo_root: Path) -> HeadState:
    """Resolve the current HEAD commit or detect an unborn branch."""

    head_proc = _run_git(repo_root, "rev-parse", "--verify", "HEAD", check=False)
    if head_proc.returncode == 0:
        ref_proc = _run_git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        ref_name = ref_proc.stdout.strip() if ref_proc.returncode == 0 else None
        return HeadState(
            state="commit",
            commit=head_proc.stdout.strip(),
            ref_name=ref_name if ref_name else None,
        )

    ref_proc = _run_git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if ref_proc.returncode == 0:
        ref_name = ref_proc.stdout.strip()
        return HeadState(state="unborn", commit=None, ref_name=ref_name if ref_name else None)

    details = head_proc.stderr.strip() or head_proc.stdout.strip() or "HEAD is unavailable"
    raise GitCommandError(details)


def load_ci_report(report_path: Path) -> CiReportState:
    """Load the CI report written by ``build.ps1`` / ``run.ps1``."""

    if not report_path.is_file():
        raise ReportValidationError(
            f"Не найден локальный CI-отчёт: {report_path.as_posix()}",
        )

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportValidationError(
            f"Не удалось прочитать локальный CI-отчёт {report_path.as_posix()}: {exc}",
        ) from exc

    if not isinstance(payload, dict):
        raise ReportValidationError("Локальный CI-отчёт имеет неожиданный формат.")

    ci_section = payload.get("ci")
    git_section = payload.get("git")
    if not isinstance(ci_section, dict) or not isinstance(git_section, dict):
        raise ReportValidationError(
            "Локальный CI-отчёт не содержит git/ci-метаданные guardrail. Запустите CI повторно.",
        )

    passed = ci_section.get("passed")
    return CiReportState(
        passed=passed if isinstance(passed, bool) else None,
        overall_status=payload.get("status") if isinstance(payload.get("status"), str) else None,
        profile=ci_section.get("profile") if isinstance(ci_section.get("profile"), str) else None,
        finished_at_utc=(payload.get("finished_at_utc") if isinstance(payload.get("finished_at_utc"), str) else None),
        head_state=git_section.get("head_state") if isinstance(git_section.get("head_state"), str) else None,
        head_commit=(git_section.get("head_commit") if isinstance(git_section.get("head_commit"), str) else None),
    )


def _short_commit(commit: str | None) -> str:
    """Return a short human-readable commit id."""

    if commit is None or commit == "":
        return "unknown"
    return commit[:8]


def _report_matches_head(report: CiReportState, head: HeadState) -> bool:
    """Return True when the report is bound to the current HEAD state."""

    if report.head_state != head.state:
        return False
    if head.state == "unborn":
        return True
    return report.head_commit == head.commit


def _remediation_text() -> str:
    """Return the standard remediation hint shown to the developer."""

    return (
        "Что сделать:\n"
        "  1. Запустите ./run.ps1 -SkipLaunch\n"
        "  2. Для быстрого промежуточного прогона можно использовать ./run.ps1 -Fast -SkipLaunch\n"
        "  3. После успешного прогона повторите commit"
    )


def _format_profile_suffix(profile: str | None) -> str:
    """Return a short ``profile=...`` suffix for diagnostics."""

    if profile is None or profile == "":
        return ""
    return f", profile={profile}"


def _format_finished_suffix(finished_at_utc: str | None) -> str:
    """Return a short timestamp suffix for diagnostics."""

    if finished_at_utc is None or finished_at_utc == "":
        return ""
    return f", finished_at_utc={finished_at_utc}"


def _build_block_message(head: HeadState, report: CiReportState | None, *, reason: str) -> str:
    """Build the operator-facing refusal text."""

    current_head = _short_commit(head.commit)
    if reason == "missing_report":
        return (
            "Commit заблокирован: после последнего коммита локальная CI ещё не запускалась.\n"
            f"Текущий HEAD: {current_head}.\n"
            f"{_remediation_text()}"
        )
    if reason == "invalid_report":
        return (
            "Commit заблокирован: локальный CI-отчёт устарел или не содержит метаданные guardrail.\n"
            f"Текущий HEAD: {current_head}.\n"
            f"{_remediation_text()}"
        )
    if reason == "failed_report" and report is not None:
        return (
            "Commit заблокирован: последний прогон локальной CI для текущего HEAD не прошёл.\n"
            f"Текущий HEAD: {current_head}; status={report.overall_status or 'unknown'}"
            f"{_format_profile_suffix(report.profile)}"
            f"{_format_finished_suffix(report.finished_at_utc)}.\n"
            f"{_remediation_text()}"
        )
    if reason == "stale_report" and report is not None:
        return (
            "Commit заблокирован: после последнего коммита локальная CI не запускалась.\n"
            f"Текущий HEAD: {current_head}; последний валидный CI-отчёт привязан к "
            f"{_short_commit(report.head_commit)}"
            f"{_format_profile_suffix(report.profile)}"
            f"{_format_finished_suffix(report.finished_at_utc)}.\n"
            f"{_remediation_text()}"
        )
    return (
        "Commit заблокирован: не удалось подтвердить свежесть локальной CI для текущего HEAD.\n"
        f"Текущий HEAD: {current_head}.\n"
        f"{_remediation_text()}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the guard."""

    parser = argparse.ArgumentParser(
        description="Block commits when the current HEAD has no fresh local CI report.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_default_repo_root(),
        help="Repository root that contains .ci_cache/report.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the local git pre-commit guard."""

    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()

    try:
        head = resolve_head_state(repo_root)
    except GitCommandError as exc:
        print(
            f"Commit заблокирован: не удалось прочитать состояние Git HEAD.\nПричина: {exc}",
            file=sys.stderr,
        )
        return 1

    # * The policy is intentionally scoped to commits made after an existing HEAD commit.
    if head.state == "unborn":
        return 0

    report_path = repo_root / REPORT_RELATIVE_PATH
    try:
        report = load_ci_report(report_path)
    except ReportValidationError as exc:
        reason = "missing_report" if not report_path.exists() else "invalid_report"
        print(f"{_build_block_message(head, None, reason=reason)}\nДетали: {exc}", file=sys.stderr)
        return 1

    if not _report_matches_head(report, head):
        print(_build_block_message(head, report, reason="stale_report"), file=sys.stderr)
        return 1

    if report.passed is not True:
        print(_build_block_message(head, report, reason="failed_report"), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
