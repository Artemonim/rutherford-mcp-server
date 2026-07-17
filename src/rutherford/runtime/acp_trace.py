# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Structured stderr ACP lifecycle diagnostics (C3) and prompt heartbeats (quiet C1).

Emits ``acp_lifecycle`` JSON lines through :func:`~rutherford.runtime.logging.log_event` so an
operator can distinguish queue / sandbox / spawn / handshake / prompt / finish without waking a
sync MCP caller. Never logs prompt text, answers, environment, full paths, or raw argv.

The :func:`acp_trace` boundary defensively allowlists primary and nested fields before
``log_event``: disallowed keys and unsafe values are dropped or reduced to safe leaves even when a
future caller passes them through ``message``, ``paths``, ``launch``, ``error``, or ``**extra``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .logging import LOGGER_NAME, log_event

#: Default seconds between prompt heartbeats when no trace context / config override is bound.
DEFAULT_PROMPT_HEARTBEAT_S = 30.0

#: Keys that must never appear at any nesting level of an ``acp_lifecycle`` record.
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "answer",
        "partial",
        "env",
        "argv",
        "extra_args",
        "command_args",
        "stderr",
        "stdout",
        "exception",
        "exc",
        "traceback",
        "tb",
        "message_text",
        "raw",
    }
)

#: Documented top-level metadata keys retained after sanitization (plus nested maps below).
_SAFE_TOP_KEYS: frozenset[str] = frozenset(
    {
        "stage",
        "phase",
        "status",
        "correlation_id",
        "tool",
        "job_id",
        "attempt",
        "cli",
        "model",
        "depth",
        "pid",
        "session_id",
        "sandbox",
        "paths",
        "launch",
        "timing",
        "error",
        "message",
    }
)

_SAFE_SANDBOX_KEYS: frozenset[str] = frozenset({"active", "kind"})
_SAFE_PATHS_KEYS: frozenset[str] = frozenset({"cwd_leaf", "sandbox_leaf"})
_SAFE_LAUNCH_KEYS: frozenset[str] = frozenset({"command"})
_SAFE_TIMING_KEYS: frozenset[str] = frozenset(
    {"wait_s", "elapsed_s", "budget_s", "stage_elapsed_s", "timeout_s", "heartbeat_s"}
)
#: Structured error metadata only -- codes / stages / timing, never exception text.
_SAFE_ERROR_KEYS: frozenset[str] = frozenset(
    {"code", "reexecution_safety", "stage", "budget_s", "elapsed_s", "category"}
)

#: Fixed operator messages known to be free of secrets / paths / exception text.
_SAFE_MESSAGES: frozenset[str] = frozenset({"rutherford still awaiting prompt outcome"})


@dataclass(frozen=True)
class AcpTraceContext:
    """Correlation and target metadata merged into every ``acp_lifecycle`` record.

    Bound via :func:`bind_acp_trace` at the start of a delegate attempt or panel voice. Nested binds
    inherit unset fields from the parent so a job wrapper can add ``job_id`` without clearing the
    voice correlation.
    """

    correlation_id: str = ""
    tool: str = ""
    job_id: str | None = None
    attempt: int = 1
    cli: str | None = None
    model: str | None = None
    depth: int = 0
    #: Seconds between prompt heartbeats; ``0`` disables. Taken from config when a service binds.
    heartbeat_s: float = DEFAULT_PROMPT_HEARTBEAT_S


_TRACE: ContextVar[AcpTraceContext | None] = ContextVar("rutherford_acp_trace", default=None)


def current_acp_trace() -> AcpTraceContext:
    """Return the currently bound ACP trace context (defaults when nothing is bound)."""
    return _TRACE.get() or AcpTraceContext()


@contextmanager
def bind_acp_trace(**fields: Any) -> Iterator[AcpTraceContext]:
    """Bind an :class:`AcpTraceContext` for the current task, merging over the parent context.

    Only keys present in :class:`AcpTraceContext` are applied; unknown keys are ignored so callers
    can pass ``**extras`` safely. ``None`` for an optional field clears it; omit a key to inherit.
    """
    parent = current_acp_trace()
    updates = {key: value for key, value in fields.items() if key in AcpTraceContext.__dataclass_fields__}
    ctx = replace(parent, **updates)
    token = _TRACE.set(ctx)
    try:
        yield ctx
    finally:
        _TRACE.reset(token)


def path_leaf(path: str | Path | None) -> str | None:
    """Return the final path component only (never a full absolute path).

    Reduces both POSIX- and Windows-style paths on any host. A foreign absolute path
    (Windows separators on POSIX, or POSIX separators on Windows) must never escape as a
    full path into stderr lifecycle records.
    """
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    # * Prefer the most-reduced leaf across both path grammars (shorter => more separators recognized).
    posix_leaf = PurePosixPath(text).name
    windows_leaf = PureWindowsPath(text).name
    leaf = min(posix_leaf, windows_leaf, key=len)
    return leaf or None


def launch_command_basename(command: str | None) -> str | None:
    """Return a safe launch identifier: basename of argv[0] only."""
    return path_leaf(command)


def _is_safe_scalar(value: Any) -> bool:
    """Whether ``value`` is a JSON-safe scalar allowed in lifecycle metadata."""
    return isinstance(value, (str, int, float, bool)) or value is None


def _sanitize_leaf_str(value: Any) -> str | None:
    """Reduce a path-like string to its final component; reject non-strings."""
    if not isinstance(value, str):
        return None
    return path_leaf(value)


def _sanitize_mapping(
    raw: Mapping[str, Any] | None, allowed: frozenset[str], *, leaves: bool = False
) -> dict[str, Any] | None:
    """Keep only allowlisted keys; optionally force string values through :func:`path_leaf`."""
    if not raw:
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _FORBIDDEN_KEYS or key not in allowed or value is None:
            continue
        if leaves:
            leaf = _sanitize_leaf_str(value)
            if leaf is not None:
                out[key] = leaf
            continue
        if isinstance(value, Mapping):
            # * Nested maps under scalar allowlists are disallowed (keep the surface flat).
            continue
        if _is_safe_scalar(value):
            out[key] = value
    return out or None


def _sanitize_message(message: str | None) -> str | None:
    """Retain only known-safe operator messages; drop anything that could carry secrets or paths."""
    if message is None:
        return None
    if message in _SAFE_MESSAGES:
        return message
    return None


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Defensive allowlist pass before :func:`log_event` -- primary and nested."""
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or key in _FORBIDDEN_KEYS or key not in _SAFE_TOP_KEYS:
            continue
        if key == "sandbox":
            cleaned = _sanitize_mapping(value if isinstance(value, Mapping) else None, _SAFE_SANDBOX_KEYS)
            if cleaned is not None:
                sanitized[key] = cleaned
        elif key == "paths":
            cleaned = _sanitize_mapping(value if isinstance(value, Mapping) else None, _SAFE_PATHS_KEYS, leaves=True)
            if cleaned is not None:
                sanitized[key] = cleaned
        elif key == "launch":
            cleaned = _sanitize_mapping(value if isinstance(value, Mapping) else None, _SAFE_LAUNCH_KEYS, leaves=True)
            if cleaned is not None:
                sanitized[key] = cleaned
        elif key == "timing":
            cleaned = _sanitize_mapping(value if isinstance(value, Mapping) else None, _SAFE_TIMING_KEYS)
            if cleaned is not None:
                sanitized[key] = cleaned
        elif key == "error":
            cleaned = _sanitize_mapping(value if isinstance(value, Mapping) else None, _SAFE_ERROR_KEYS)
            if cleaned is not None:
                sanitized[key] = cleaned
        elif key == "message":
            safe_msg = _sanitize_message(value if isinstance(value, str) else None)
            if safe_msg is not None:
                sanitized[key] = safe_msg
        elif _is_safe_scalar(value):
            sanitized[key] = value
    return sanitized


def acp_trace(
    stage: str,
    phase: str,
    *,
    level: int = logging.INFO,
    status: str | None = None,
    pid: int | None = None,
    session_id: str | None = None,
    sandbox: Mapping[str, Any] | None = None,
    paths: Mapping[str, Any] | None = None,
    launch: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    message: str | None = None,
    **extra: Any,
) -> None:
    """Emit one ``acp_lifecycle`` stderr record for ``stage`` / ``phase``.

    Drops ``None`` values. Sanitizes primary kwargs, nested maps, and ``**extra`` against a
    documented allowlist before calling :func:`log_event` so prompt / answer / env / raw paths /
    raw argv / exception text cannot reach stderr through this boundary.
    """
    ctx = current_acp_trace()
    fields: dict[str, Any] = {
        "stage": stage,
        "phase": phase,
        "status": status,
        "correlation_id": ctx.correlation_id or None,
        "tool": ctx.tool or None,
        "job_id": ctx.job_id,
        "attempt": ctx.attempt,
        "cli": ctx.cli,
        "model": ctx.model,
        "depth": ctx.depth,
        "pid": pid,
        "session_id": session_id,
        "sandbox": dict(sandbox) if sandbox else None,
        "paths": dict(paths) if paths else None,
        "launch": dict(launch) if launch else None,
        "timing": dict(timing) if timing else None,
        "error": dict(error) if error else None,
        "message": message,
    }
    # * Extra top-level keys only survive when they are on the documented allowlist (not forbidden).
    for key, value in extra.items():
        if key in _FORBIDDEN_KEYS or key not in _SAFE_TOP_KEYS or value is None:
            continue
        # * Do not let extras overwrite already-built nested maps with unsanitized payloads blindly;
        # they still pass through _sanitize_fields below.
        if key not in fields or fields[key] is None:
            fields[key] = value
    log_event("acp_lifecycle", level=level, **_sanitize_fields(fields))


class PromptHeartbeat:
    """Rate-limited stderr heartbeats while Rutherford awaits a prompt outcome.

    Claims only that Rutherford is still waiting -- never model or network liveness. Cancelled and
    awaited in :meth:`stop` on every success / failure / timeout / cancellation path.
    """

    def __init__(
        self,
        interval_s: float | None = None,
        *,
        timeout_s: float | None = None,
        pid: int | None = None,
        session_id: str | None = None,
    ) -> None:
        ctx = current_acp_trace()
        self._interval = ctx.heartbeat_s if interval_s is None else interval_s
        self._timeout_s = timeout_s
        self._pid = pid
        self._session_id = session_id
        self._started = time.monotonic()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the heartbeat helper when the interval is positive and logging is enabled."""
        if self._interval <= 0:
            return
        # * Skip the task entirely when logging is off / below INFO -- avoids idle sleeps for nothing.
        if not logging.getLogger(LOGGER_NAME).isEnabledFor(logging.INFO):
            return
        self._task = asyncio.create_task(self._loop(), name="rutherford-acp-prompt-heartbeat")

    async def stop(self) -> None:
        """Cancel and await the helper task; idempotent and never raises."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                elapsed = round(time.monotonic() - self._started, 3)
                acp_trace(
                    "prompt",
                    "heartbeat",
                    status="awaiting",
                    pid=self._pid,
                    session_id=self._session_id,
                    timing={
                        "elapsed_s": elapsed,
                        "timeout_s": self._timeout_s,
                        "heartbeat_s": self._interval,
                    },
                    message="rutherford still awaiting prompt outcome",
                )
        except asyncio.CancelledError:
            raise
