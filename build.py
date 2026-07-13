# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Python stage runner for Rutherford local CI (Agent Enforcer 2)."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
import tokenize
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PROJECT_DIRECTORIES = ("src", "tests")
PYTHON_CI_DIRECTORIES = ("src", "tests")
PYTHON_CI_STAGES = frozenset({"fmt", "lint", "compile", "license-check"})
LINE_LIMIT_WARN_THRESHOLD = 1500
LINE_LIMIT_FAIL_THRESHOLD = 2500
DIR_ENTRY_WARN_THRESHOLD = 35
DIR_ENTRY_FAIL_THRESHOLD = 70
PATH_DEPTH_WARN_THRESHOLD = 7
IMPORT_DEPTH_WARN_THRESHOLD = 6
PROJECT_IMPORT_PACKAGE = "rutherford"
PACKAGE_SOURCE_PREFIX = "src/rutherford/"
SCRIPTS_SOURCE_PREFIX = "scripts/"
AGENTS_COVERAGE_TRACKED_PREFIXES: tuple[str, ...] = ("docs/", "scripts/")
EXCLUDED_DIR_NAMES = {
    ".ci_cache",
    ".codebase-memory",
    ".enforcer",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}
EOL_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".whl",
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".mp4",
        ".webm",
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
    }
)
# * Repo policy is LF (see .gitattributes); hooks must stay LF on Git for Windows.
LF_ONLY_RELATIVE_PATHS: frozenset[str] = frozenset({".githooks/pre-commit"})
LINE_LIMIT_EXCLUDE_BASENAMES = frozenset({"build.py", "build.ps1"})


def uv_cmd(*args: str) -> list[str]:
    """Build an ``uv run`` argv for project-scoped tool invocations."""

    return ["uv", "run", *args]


def relative_path(path: Path, root: Path) -> str:
    """Return a POSIX path relative to ``root``."""

    return path.resolve().relative_to(root.resolve()).as_posix()


def list_repo_files(root: Path) -> list[Path]:
    """List tracked and untracked repository files that matter for CI."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        files: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
                continue
            files.append(path)
        return sorted(files)

    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = root / line.strip()
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
            continue
        paths.append(path)
    return sorted(paths)


def paths_not_ignored_by_git(rel_paths: list[str], root: Path) -> list[str]:
    """Return repository-relative POSIX paths that are not ignored by git exclude rules."""

    if not rel_paths:
        return []
    if not (root / ".git").exists():
        return list(rel_paths)
    normalized = [p.replace("\\", "/") for p in rel_paths]
    payload = "\0".join(normalized).encode("utf-8") + b"\0"
    proc = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],  # noqa: S607
        cwd=root,
        input=payload,
        capture_output=True,
        check=False,
    )
    ignored: set[str] = set()
    if proc.stdout:
        for part in proc.stdout.decode("utf-8", errors="replace").split("\0"):
            if part:
                ignored.add(part.replace("\\", "/"))
    return [p for p in rel_paths if p.replace("\\", "/") not in ignored]


def ensure_directory(path: Path) -> None:
    """Create ``path`` when missing."""

    path.mkdir(parents=True, exist_ok=True)


def write_log(
    log_dir: Path,
    stage: str,
    tool: str,
    body: str,
    *,
    unified_log: Path | None = None,
) -> str:
    """Write a stage tool log and return its repo-relative path."""

    ensure_directory(log_dir)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"{stage}-{tool}-{stamp}.log"
    log_path.write_text(body, encoding="utf-8", errors="replace")
    if unified_log is not None:
        with unified_log.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n=== {stage}/{tool} ===\n")
            handle.write(body)
            if not body.endswith("\n"):
                handle.write("\n")
        return unified_log.as_posix()
    return log_path.as_posix()


def run_command(
    *,
    root: Path,
    log_dir: Path,
    stage: str,
    tool: str,
    command: list[str],
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Run a fixed argv command and capture stdout/stderr into a log."""

    started = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = int(proc.returncode)
    except OSError as exc:
        stdout = ""
        stderr = str(exc)
        exit_code = 127

    body = (
        f"command: {' '.join(command)}\n"
        f"exit_code: {exit_code}\n"
        f"duration_ms: {int((time.perf_counter() - started) * 1000)}\n\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}\n"
    )
    log_path = write_log(log_dir, stage, tool, body, unified_log=unified_log)
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "log_path": log_path,
    }


def make_stage_result(
    *,
    name: str,
    status: str,
    note: str,
    duration_ms: int,
    cache_key: str,
    issues: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    log_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Build an AE2-compatible stage payload."""

    details: dict[str, Any] = {}
    if log_paths:
        details["log_paths"] = log_paths
    payload: dict[str, Any] = {
        "name": name,
        "status": status,
        "note": note,
        "duration_ms": duration_ms,
        "cache_key": cache_key,
        "issues": issues or [],
        "details": details,
    }
    if metrics:
        payload["metrics"] = metrics
    return payload


def extract_summary(output: str, *, fallback: str) -> str:
    """Return a short one-line summary from tool output."""

    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return fallback


def stage_input_files(stage: str, root: Path) -> list[Path]:
    """Resolve the files that should influence the stage cache."""

    files = list_repo_files(root)
    selected: list[Path] = []
    for path in files:
        rel = relative_path(path, root)
        top_level = rel.split("/", 1)[0]
        python_dirs = PYTHON_CI_DIRECTORIES if stage in PYTHON_CI_STAGES else PROJECT_DIRECTORIES
        is_python_source = path.suffix == ".py" and top_level in python_dirs
        is_script_source = path.suffix == ".py" and rel.startswith(SCRIPTS_SOURCE_PREFIX)

        if stage in {"fmt", "lint", "compile", "test", "coverage"}:
            if is_python_source or rel in {"pyproject.toml", "build.py", "uv.lock"}:
                selected.append(path)
        elif stage == "license-check":
            if (
                is_python_source
                or is_script_source
                or rel
                in {
                    "pyproject.toml",
                    "build.py",
                    "scripts/check_license_headers.py",
                }
            ):
                selected.append(path)
        elif stage == "security":
            if is_python_source or rel in {"pyproject.toml", "build.py", "uv.lock"}:
                selected.append(path)
        elif stage == "line-limits":
            if path.name in LINE_LIMIT_EXCLUDE_BASENAMES:
                continue
            selected.append(path)
        elif stage == "line-endings":
            if path.suffix.lower() in EOL_BINARY_SUFFIXES:
                continue
            selected.append(path)
        elif stage == "agents-coverage" and (
            rel == "AGENTS.md" or any(rel.startswith(prefix) for prefix in AGENTS_COVERAGE_TRACKED_PREFIXES)
        ):
            selected.append(path)

    return sorted(set(selected))


def compute_stage_hash(
    stage: str,
    root: Path,
    *,
    eol_fix: bool = False,
) -> str:
    """Compute a deterministic content hash for stage caching."""

    hasher = hashlib.sha256()
    hasher.update(f"stage:{stage}\n".encode())
    hasher.update(f"eol_fix:{int(eol_fix)}\n".encode())
    for path in stage_input_files(stage, root):
        rel = relative_path(path, root)
        hasher.update(rel.encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def is_binary_payload(data: bytes) -> bool:
    """Heuristic binary detection for line-ending checks."""

    return b"\0" in data[:8192]


def is_pure_lf(data: bytes) -> bool:
    """Return True when newlines are LF-only."""

    return b"\r" not in data


def is_pure_crlf(data: bytes) -> bool:
    """Return True when every newline is CRLF."""

    if b"\r" not in data and b"\n" not in data:
        return True
    return b"\r\n" in data and b"\r" not in data.replace(b"\r\n", b"") and b"\n" not in data.replace(b"\r\n", b"")


def normalize_bytes_to_lf(data: bytes) -> bytes:
    """Normalize newlines to LF."""

    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def expected_line_ending_mode(path: Path, root: Path) -> str:
    """Return the repository line-ending policy for ``path`` (LF for Rutherford)."""

    del path, root
    return "lf"


def stage_line_endings(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
    *,
    eol_fix: bool = False,
) -> dict[str, Any]:
    """Enforce LF line endings for tracked text files (matches .gitattributes)."""

    del cache_dir, log_dir, unified_log
    started_at = time.perf_counter()
    issues: list[dict[str, Any]] = []
    fixed = 0
    checked = 0

    for path in stage_input_files("line-endings", root):
        try:
            data = path.read_bytes()
        except OSError as exc:
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-endings",
                    "rule": "read_error",
                    "count": 1,
                    "message": f"{relative_path(path, root)}: {exc}",
                }
            )
            continue
        if is_binary_payload(data):
            continue
        checked += 1
        rel = relative_path(path, root)
        if is_pure_lf(data):
            continue
        if eol_fix:
            normalized = normalize_bytes_to_lf(data)
            try:
                path.write_bytes(normalized)
                fixed += 1
            except OSError as exc:
                issues.append(
                    {
                        "language": "ci",
                        "tool": "line-endings",
                        "rule": "write_error",
                        "count": 1,
                        "message": f"{rel}: {exc}",
                    }
                )
            continue
        issues.append(
            {
                "language": "ci",
                "tool": "line-endings",
                "rule": "non_lf",
                "count": 1,
                "message": f"Non-LF line endings: {rel}",
            }
        )

    status = "fail" if issues else "ok"
    note = f"Checked {checked} text file(s)"
    if fixed:
        note += f"; auto-fixed {fixed} to LF"
    if issues:
        note += f"; {len(issues)} issue(s)"
    return make_stage_result(
        name="line-endings",
        status=status,
        note=note + ".",
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[],
    )


def stage_fmt(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Apply ruff formatting (autofix by default)."""

    del cache_dir
    started_at = time.perf_counter()
    command = uv_cmd("ruff", "format", *PYTHON_CI_DIRECTORIES)
    outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="fmt",
        tool="ruff-format",
        command=command,
        unified_log=unified_log,
    )
    status = "ok" if outcome["exit_code"] == 0 else "fail"
    dirs_label = ", ".join(PYTHON_CI_DIRECTORIES)
    note = f"ruff format applied ({dirs_label})." if status == "ok" else "ruff format failed."
    issues = []
    if status == "fail":
        issues.append(
            {
                "language": "python",
                "tool": "ruff",
                "rule": "format",
                "count": 1,
                "message": "Formatting drift detected.",
            }
        )
    return make_stage_result(
        name="fmt",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[outcome["log_path"]],
    )


def stage_lint(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Run ruff check with autofix."""

    del cache_dir
    started_at = time.perf_counter()
    command = uv_cmd("ruff", "check", "--fix", "--unsafe-fixes", *PYTHON_CI_DIRECTORIES)
    outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="lint",
        tool="ruff-check",
        command=command,
        unified_log=unified_log,
    )
    status = "ok" if outcome["exit_code"] == 0 else "fail"
    note = (
        "ruff check passed (--fix --unsafe-fixes)."
        if status == "ok"
        else extract_summary(outcome["stdout"] or outcome["stderr"], fallback="ruff reported lint issues.")
    )
    issues: list[dict[str, Any]] = []
    if status == "fail":
        issues.append(
            {
                "language": "python",
                "tool": "ruff",
                "rule": "lint",
                "count": 1,
                "message": "ruff reported lint issues.",
            }
        )
    return make_stage_result(
        name="lint",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[outcome["log_path"]],
    )


def stage_license_check(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Verify SPDX license headers on Python sources."""

    del cache_dir
    started_at = time.perf_counter()
    command = uv_cmd("python", "scripts/check_license_headers.py")
    outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="license-check",
        tool="license-headers",
        command=command,
        unified_log=unified_log,
    )
    status = "ok" if outcome["exit_code"] == 0 else "fail"
    note = (
        "license headers ok."
        if status == "ok"
        else extract_summary(
            outcome["stdout"] or outcome["stderr"],
            fallback="license header check failed.",
        )
    )
    issues = []
    if status == "fail":
        issues.append(
            {
                "language": "python",
                "tool": "license-headers",
                "rule": "missing_header",
                "count": 1,
                "message": "One or more Python files are missing the SPDX license header.",
            }
        )
    return make_stage_result(
        name="license-check",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[outcome["log_path"]],
    )


def stage_compile(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Run compileall and mypy."""

    del cache_dir
    started_at = time.perf_counter()
    logs: list[str] = []
    issues: list[dict[str, Any]] = []

    compileall_outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="compile",
        tool="compileall",
        command=uv_cmd("python", "-m", "compileall", "-q", *PYTHON_CI_DIRECTORIES),
        unified_log=unified_log,
    )
    logs.append(compileall_outcome["log_path"])
    if compileall_outcome["exit_code"] != 0:
        issues.append(
            {
                "language": "python",
                "tool": "compileall",
                "rule": "syntax",
                "count": 1,
                "message": "Python syntax compilation failed.",
            }
        )
        return make_stage_result(
            name="compile",
            status="fail",
            note="compileall reported syntax errors.",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            cache_key=cache_key,
            issues=issues,
            log_paths=logs,
        )

    mypy_outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="compile",
        tool="mypy",
        command=uv_cmd("mypy"),
        unified_log=unified_log,
    )
    logs.append(mypy_outcome["log_path"])
    if unified_log is not None:
        logs = [relative_path(unified_log, root)]

    status = "ok" if mypy_outcome["exit_code"] == 0 else "fail"
    note = (
        "compileall and mypy passed."
        if status == "ok"
        else extract_summary(
            mypy_outcome["stdout"] or mypy_outcome["stderr"],
            fallback="mypy reported type errors.",
        )
    )
    if status == "fail":
        issues.append(
            {
                "language": "python",
                "tool": "mypy",
                "rule": "typecheck",
                "count": 1,
                "message": "mypy reported type errors.",
            }
        )
    return make_stage_result(
        name="compile",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=logs,
    )


def stage_test(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Run the unit-test suite (integration deselected via pyproject addopts)."""

    del cache_dir
    started_at = time.perf_counter()
    outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="test",
        tool="pytest",
        command=uv_cmd("pytest", "-q", "--tb=short"),
        unified_log=unified_log,
    )
    status = "ok" if outcome["exit_code"] == 0 else "fail"
    note = extract_summary(outcome["stdout"] or outcome["stderr"], fallback="pytest completed.")
    issues = []
    if status == "fail":
        issues.append(
            {
                "language": "python",
                "tool": "pytest",
                "rule": "tests_failed",
                "count": 1,
                "message": "One or more pytest checks failed.",
            }
        )
    return make_stage_result(
        name="test",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[outcome["log_path"]],
    )


def stage_coverage(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Run pytest with project coverage floors, then per-file coverage check."""

    del cache_dir
    started_at = time.perf_counter()
    logs: list[str] = []
    issues: list[dict[str, Any]] = []

    pytest_outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="coverage",
        tool="pytest-cov",
        command=uv_cmd("pytest", "-q", "--tb=short"),
        unified_log=unified_log,
    )
    logs.append(pytest_outcome["log_path"])
    if pytest_outcome["exit_code"] != 0:
        return make_stage_result(
            name="coverage",
            status="fail",
            note=extract_summary(
                pytest_outcome["stdout"] or pytest_outcome["stderr"],
                fallback="pytest/coverage failed (cov-fail-under=90 or tests failed).",
            ),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            cache_key=cache_key,
            issues=[
                {
                    "language": "python",
                    "tool": "pytest-cov",
                    "rule": "coverage_failed",
                    "count": 1,
                    "message": "pytest coverage gate failed.",
                }
            ],
            log_paths=logs,
        )

    per_file_outcome = run_command(
        root=root,
        log_dir=log_dir,
        stage="coverage",
        tool="per-file-coverage",
        command=uv_cmd("python", "scripts/check_per_file_coverage.py"),
        unified_log=unified_log,
    )
    logs.append(per_file_outcome["log_path"])
    if per_file_outcome["exit_code"] != 0:
        return make_stage_result(
            name="coverage",
            status="fail",
            note=extract_summary(
                per_file_outcome["stdout"] or per_file_outcome["stderr"],
                fallback="per-file coverage floor (80%) failed.",
            ),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            cache_key=cache_key,
            issues=[
                {
                    "language": "python",
                    "tool": "per-file-coverage",
                    "rule": "per_file_floor",
                    "count": 1,
                    "message": "One or more source files are below the 80% per-file coverage floor.",
                }
            ],
            log_paths=logs,
        )

    return make_stage_result(
        name="coverage",
        status="ok",
        note="pytest coverage (>=90% aggregate) and per-file floor (>=80%) passed.",
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=logs,
    )


def parse_pip_audit_output(output: str) -> int:
    """Return the number of pip-audit vulnerabilities from JSON stdout."""

    if not output.strip():
        return 0
    payload = json.loads(output)
    vulnerability_count = 0
    if isinstance(payload, list):
        for dependency in payload:
            if not isinstance(dependency, dict):
                continue
            vulns = dependency.get("vulns", [])
            if isinstance(vulns, list):
                vulnerability_count += len(vulns)
    return vulnerability_count


def stage_security(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Dependency advisory scan via pip-audit (warn when unavailable)."""

    del cache_dir
    started_at = time.perf_counter()
    issues: list[dict[str, Any]] = []
    notes: list[str] = []
    overall_status = "ok"

    # * Prefer uvx so pip-audit need not be a project dependency; fall back to uv run.
    for command in (
        ["uvx", "pip-audit", "--format", "json", "--progress-spinner", "off"],
        uv_cmd("python", "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"),
    ):
        outcome = run_command(
            root=root,
            log_dir=log_dir,
            stage="security",
            tool="pip-audit",
            command=command,
            unified_log=unified_log,
        )
        if outcome["exit_code"] == 127 or "No module named" in (outcome["stderr"] or ""):
            continue
        if outcome["exit_code"] not in {0, 1}:
            overall_status = "warn"
            notes.append("pip-audit unavailable or offline")
            issues.append(
                {
                    "language": "python",
                    "tool": "pip-audit",
                    "rule": "audit_unavailable",
                    "count": 1,
                    "message": "pip-audit could not complete in the current environment.",
                }
            )
            break
        try:
            vulnerability_count = parse_pip_audit_output(outcome["stdout"])
        except json.JSONDecodeError:
            overall_status = "warn"
            notes.append("pip-audit returned non-JSON output")
            issues.append(
                {
                    "language": "python",
                    "tool": "pip-audit",
                    "rule": "audit_parse_error",
                    "count": 1,
                    "message": "pip-audit returned output that could not be parsed as JSON.",
                }
            )
        else:
            notes.append(f"pip-audit vulnerabilities={vulnerability_count}")
            if vulnerability_count > 0:
                overall_status = "warn"
                issues.append(
                    {
                        "language": "python",
                        "tool": "pip-audit",
                        "rule": "dependency_vulnerabilities",
                        "count": vulnerability_count,
                        "message": "pip-audit reported dependency vulnerabilities that require review.",
                    }
                )
        break
    else:
        overall_status = "warn"
        notes.append("pip-audit unavailable")
        issues.append(
            {
                "language": "python",
                "tool": "pip-audit",
                "rule": "audit_unavailable",
                "count": 1,
                "message": "pip-audit is not installed and could not be resolved via uvx.",
            }
        )
        outcome = {"log_path": ""}

    return make_stage_result(
        name="security",
        status=overall_status,
        note="; ".join(notes) if notes else "security checks completed",
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        log_paths=[outcome["log_path"]] if outcome.get("log_path") else [],
    )


def _collect_python_docstring_lines(node: ast.AST) -> set[int]:
    """Collect line numbers belonging to docstrings."""

    lines: set[int] = set()
    for child in ast.walk(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            continue
        body = getattr(child, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            start = getattr(first, "lineno", None)
            end = getattr(first, "end_lineno", start)
            if isinstance(start, int) and isinstance(end, int):
                lines.update(range(start, end + 1))
    return lines


def count_python_executable_loc(path: Path) -> int:
    """Count non-blank, non-comment, non-docstring lines in a Python file."""

    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return sum(1 for line in source.splitlines() if line.strip() and not line.strip().startswith("#"))
    docstring_lines = _collect_python_docstring_lines(tree)
    comment_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
    except tokenize.TokenError:
        pass
    count = 0
    for index, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        if index in docstring_lines or index in comment_lines:
            continue
        if line.strip().startswith("#"):
            continue
        count += 1
    return count


def count_powershell_executable_loc(path: Path) -> int:
    """Pragmatic PowerShell LOC: non-blank, non-comment lines."""

    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count


def count_executable_loc(path: Path) -> int:
    """Dispatch LOC counting by file type."""

    if path.suffix.lower() == ".py":
        return count_python_executable_loc(path)
    if path.suffix.lower() == ".ps1":
        return count_powershell_executable_loc(path)
    return 0


def iter_repo_directories_for_dir_entry_check(root: Path) -> Iterator[Path]:
    """Walk the working tree for per-directory entry checks."""

    excluded = EXCLUDED_DIR_NAMES | {".git"}
    for dirpath, dirnames, _filenames in os.walk(root, topdown=True):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in excluded and not d.endswith(".egg-info")]
        yield base


def count_non_gitignored_child_entries(directory: Path, root: Path) -> int:
    """Count immediate children of ``directory`` that are not gitignored."""

    children = list(directory.iterdir())
    rels = [relative_path(child, root) if child != root else child.name for child in children]
    kept = paths_not_ignored_by_git(rels, root)
    return len(kept)


def path_depth_for_structure_check(rel_path: str) -> int | None:
    """Return directory segment depth for structure warnings under src/rutherford."""

    if not rel_path.startswith(PACKAGE_SOURCE_PREFIX):
        return None
    remainder = rel_path[len(PACKAGE_SOURCE_PREFIX) :]
    if not remainder:
        return 0
    return len(Path(remainder).parts)


def project_import_module_depth(module_name: str) -> int | None:
    """Return import depth below the package root for absolute ``rutherford.*`` imports."""

    prefix = f"{PROJECT_IMPORT_PACKAGE}."
    if module_name == PROJECT_IMPORT_PACKAGE:
        return 0
    if not module_name.startswith(prefix):
        return None
    return len(module_name[len(prefix) :].split("."))


def collect_deep_project_imports(path: Path) -> list[dict[str, Any]]:
    """Collect absolute project imports deeper than the warn threshold."""

    findings: list[dict[str, Any]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return findings
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                depth = project_import_module_depth(alias.name)
                if depth is not None and depth >= IMPORT_DEPTH_WARN_THRESHOLD:
                    findings.append(
                        {
                            "path": str(path),
                            "module": alias.name,
                            "depth": depth,
                            "lineno": getattr(node, "lineno", 0),
                        }
                    )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            depth = project_import_module_depth(node.module)
            if depth is not None and depth >= IMPORT_DEPTH_WARN_THRESHOLD:
                findings.append(
                    {
                        "path": str(path),
                        "module": node.module,
                        "depth": depth,
                        "lineno": getattr(node, "lineno", 0),
                    }
                )
    return findings


def stage_line_limits(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Executable LOC + folder fan-out checks (pragmatic)."""

    del cache_dir, log_dir, unified_log
    started_at = time.perf_counter()
    issues: list[dict[str, Any]] = []
    loc_warn = 0
    loc_fail = 0
    dir_warn = 0
    dir_fail = 0
    depth_warn = 0
    import_warn = 0

    for path in list_repo_files(root):
        rel = relative_path(path, root)
        if path.name in LINE_LIMIT_EXCLUDE_BASENAMES:
            continue
        top = rel.split("/", 1)[0]
        if top not in {"src", "tests"} and path.suffix.lower() not in {".py", ".ps1"}:
            continue
        if path.suffix.lower() not in {".py", ".ps1"}:
            continue
        # * Root CI scripts (run.ps1) participate; build.* excluded above.
        if (
            top not in {"src", "tests"}
            and not rel.endswith(".ps1")
            and not (path.parent == root and path.suffix.lower() == ".ps1")
        ):
            continue
        loc = count_executable_loc(path)
        if loc >= LINE_LIMIT_FAIL_THRESHOLD:
            loc_fail += 1
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-limits",
                    "rule": "loc_fail",
                    "count": 1,
                    "message": f"{rel}: {loc} executable LOC (>= {LINE_LIMIT_FAIL_THRESHOLD})",
                }
            )
        elif loc >= LINE_LIMIT_WARN_THRESHOLD:
            loc_warn += 1
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-limits",
                    "rule": "loc_warn",
                    "count": 1,
                    "message": f"{rel}: {loc} executable LOC (>= {LINE_LIMIT_WARN_THRESHOLD})",
                }
            )

        depth = path_depth_for_structure_check(rel)
        if depth is not None and depth >= PATH_DEPTH_WARN_THRESHOLD:
            depth_warn += 1
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-limits",
                    "rule": "path_depth_warn",
                    "count": 1,
                    "message": f"{rel}: path depth {depth} (>= {PATH_DEPTH_WARN_THRESHOLD})",
                }
            )

        if path.suffix == ".py" and top in {"src", "tests"}:
            for finding in collect_deep_project_imports(path):
                import_warn += 1
                issues.append(
                    {
                        "language": "ci",
                        "tool": "line-limits",
                        "rule": "import_depth_warn",
                        "count": 1,
                        "message": (
                            f"{rel}:{finding['lineno']}: import `{finding['module']}` depth {finding['depth']}"
                        ),
                    }
                )

    for directory in iter_repo_directories_for_dir_entry_check(root):
        try:
            entry_count = count_non_gitignored_child_entries(directory, root)
        except OSError:
            continue
        rel = "." if directory == root else relative_path(directory, root)
        if entry_count >= DIR_ENTRY_FAIL_THRESHOLD:
            dir_fail += 1
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-limits",
                    "rule": "dir_entries_fail",
                    "count": 1,
                    "message": f"{rel}/: {entry_count} entries (>= {DIR_ENTRY_FAIL_THRESHOLD})",
                }
            )
        elif entry_count >= DIR_ENTRY_WARN_THRESHOLD:
            dir_warn += 1
            issues.append(
                {
                    "language": "ci",
                    "tool": "line-limits",
                    "rule": "dir_entries_warn",
                    "count": 1,
                    "message": f"{rel}/: {entry_count} entries (>= {DIR_ENTRY_WARN_THRESHOLD})",
                }
            )

    hard_fail = loc_fail > 0 or dir_fail > 0
    has_warn = loc_warn > 0 or dir_warn > 0 or depth_warn > 0 or import_warn > 0
    if hard_fail:
        status = "fail"
    elif has_warn:
        status = "warn"
    else:
        status = "ok"
    note = (
        f"loc warn={loc_warn} fail={loc_fail}; "
        f"dir warn={dir_warn} fail={dir_fail}; "
        f"path-depth warn={depth_warn}; import-depth warn={import_warn}"
    )
    return make_stage_result(
        name="line-limits",
        status=status,
        note=note,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        cache_key=cache_key,
        issues=issues,
        metrics={
            "line_limits": {
                "loc_warn": loc_warn,
                "loc_fail": loc_fail,
                "dir_warn": dir_warn,
                "dir_fail": dir_fail,
                "path_depth_warn": depth_warn,
                "import_depth_warn": import_warn,
            }
        },
        log_paths=[],
    )


def _collect_agents_tracked_files(root: Path) -> list[str]:
    """Return repo-relative POSIX paths under docs/ and scripts/ for AGENTS.md coverage."""

    result: list[str] = []
    for path in list_repo_files(root):
        rel = relative_path(path, root).replace("\\", "/")
        if any(rel.startswith(prefix) for prefix in AGENTS_COVERAGE_TRACKED_PREFIXES):
            result.append(rel)
    return sorted(result)


def _agents_md_mentioned_paths(agents_text: str) -> set[str]:
    """Extract file or directory paths mentioned in AGENTS.md backticks."""

    mentioned: set[str] = set()
    for line in agents_text.splitlines():
        for segment in line.split("`"):
            candidate = segment.strip().rstrip("/")
            if any(candidate.startswith(prefix) for prefix in AGENTS_COVERAGE_TRACKED_PREFIXES):
                mentioned.add(candidate.replace("\\", "/"))
    return mentioned


def _path_has_documented_ancestor(rel_path: str, mentioned: set[str]) -> bool:
    """Return True when any ancestor directory of ``rel_path`` is documented."""

    current = Path(rel_path).parent
    while True:
        candidate = str(current).replace("\\", "/")
        if candidate in {"", "."}:
            return False
        if candidate in mentioned:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def stage_agents_coverage(
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    cache_key: str,
    unified_log: Path | None = None,
) -> dict[str, Any]:
    """Warn when docs/ or scripts/ files are missing from AGENTS.md."""

    del cache_dir, log_dir, unified_log
    started_at = time.perf_counter()
    agents_path = root / "AGENTS.md"
    if not agents_path.is_file():
        return make_stage_result(
            name="agents-coverage",
            status="fail",
            note="AGENTS.md not found.",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            cache_key=cache_key,
            issues=[
                {
                    "language": "ci",
                    "tool": "agents-coverage",
                    "rule": "missing_agents_md",
                    "count": 1,
                    "message": "AGENTS.md is missing from the repository root.",
                }
            ],
        )

    agents_text = agents_path.read_text(encoding="utf-8", errors="replace")
    mentioned = _agents_md_mentioned_paths(agents_text)
    tracked = _collect_agents_tracked_files(root)
    undescribed = [rel for rel in tracked if rel not in mentioned and not _path_has_documented_ancestor(rel, mentioned)]
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if not undescribed:
        return make_stage_result(
            name="agents-coverage",
            status="ok",
            note=f"All {len(tracked)} docs/scripts files are described in AGENTS.md.",
            duration_ms=duration_ms,
            cache_key=cache_key,
            issues=[],
        )

    preview = ", ".join(undescribed[:8])
    if len(undescribed) > 8:
        preview += f", … (+{len(undescribed) - 8} more)"
    return make_stage_result(
        name="agents-coverage",
        status="warn",
        note=f"{len(undescribed)} undescribed path(s): {preview}",
        duration_ms=duration_ms,
        cache_key=cache_key,
        issues=[
            {
                "language": "ci",
                "tool": "agents-coverage",
                "rule": "undescribed_path",
                "count": len(undescribed),
                "message": (
                    "Document each path under docs/ and scripts/ in AGENTS.md (file or ancestor directory entry)."
                ),
            }
        ],
    )


def run_stage(
    stage: str,
    root: Path,
    cache_dir: Path,
    log_dir: Path,
    unified_log: Path | None = None,
    *,
    eol_fix: bool = False,
) -> dict[str, Any]:
    """Dispatch a stage to the matching implementation."""

    cache_key = compute_stage_hash(stage, root, eol_fix=eol_fix)
    if stage == "line-endings":
        return stage_line_endings(root, cache_dir, log_dir, cache_key, unified_log, eol_fix=eol_fix)
    handlers = {
        "fmt": stage_fmt,
        "lint": stage_lint,
        "license-check": stage_license_check,
        "compile": stage_compile,
        "test": stage_test,
        "coverage": stage_coverage,
        "security": stage_security,
        "line-limits": stage_line_limits,
        "agents-coverage": stage_agents_coverage,
    }
    return handlers[stage](root, cache_dir, log_dir, cache_key, unified_log)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the Python stage runner."""

    parser = argparse.ArgumentParser(description="Rutherford AE2 Python stage runner.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=[
            "line-endings",
            "agents-coverage",
            "fmt",
            "lint",
            "license-check",
            "line-limits",
            "compile",
            "test",
            "coverage",
            "security",
        ],
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--build-log", type=Path, default=None)
    parser.add_argument("--hash-only", action="store_true")
    parser.add_argument(
        "--eol-fix",
        action="store_true",
        help="For line-endings stage only: rewrite text files to LF before validation.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint that prints one JSON stage payload to stdout."""

    args = parse_args()
    root = args.root.resolve()
    cache_dir = args.cache_dir if args.cache_dir.is_absolute() else root / args.cache_dir
    log_dir = args.log_dir if args.log_dir.is_absolute() else root / args.log_dir
    ensure_directory(cache_dir)
    ensure_directory(log_dir)

    if args.hash_only:
        cache_key = compute_stage_hash(args.stage, root, eol_fix=args.eol_fix)
        sys.stdout.write(json.dumps({"cache_key": cache_key}, ensure_ascii=True))
        sys.stdout.write("\n")
        return 0

    unified_log: Path | None = None
    if args.build_log is not None:
        unified_log = args.build_log if args.build_log.is_absolute() else root / args.build_log
        ensure_directory(unified_log.parent)

    result = run_stage(
        args.stage,
        root,
        cache_dir,
        log_dir,
        unified_log,
        eol_fix=args.eol_fix,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
