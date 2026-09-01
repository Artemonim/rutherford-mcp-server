# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ACP lifecycle stderr tracing (C3) and prompt heartbeats (quiet C1)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from rutherford.runtime.acp_trace import (
    PromptHeartbeat,
    acp_trace,
    bind_acp_trace,
    current_acp_trace,
    launch_command_basename,
    path_leaf,
)
from rutherford.runtime.logging import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    configure_logging("info", "off")


def _lifecycle_lines(stream: io.StringIO) -> list[dict[object, object]]:
    rows: list[dict[object, object]] = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("event") == "acp_lifecycle":
            rows.append(payload)
    return rows


def test_path_leaf_and_launch_basename_never_emit_full_paths() -> None:
    assert path_leaf(r"G:\Users\secret\project") == "project"
    assert path_leaf("/tmp/rutherford-sandbox-abc/copy") == "copy"  # noqa: S108 - path-leaf fixture, not a real tmp write
    assert launch_command_basename(r"C:\Program Files\nodejs\node.exe") == "node.exe"
    assert launch_command_basename(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("leaf-name", "leaf-name"),
        ("./relative-leaf", "relative-leaf"),
        ("../relative-leaf", "relative-leaf"),
        # * POSIX absolute (must reduce on Windows hosts too).
        ("/tmp/rutherford-sandbox-abc/copy", "copy"),  # noqa: S108 - path-leaf fixture, not a real tmp write
        ("/var/log/", "log"),
        # * Windows absolute (must reduce on POSIX hosts too).
        (r"G:\Users\secret\project", "project"),
        (r"C:\Program Files\nodejs\node.exe", "node.exe"),
        # * Mixed separators.
        (r"G:\Users\secret/project", "project"),
        (r"C:/Users/secret\project", "project"),
        ("/tmp/mixed\\leaf", "leaf"),  # noqa: S108 - path-leaf fixture, not a real tmp write
        # * Trailing separators.
        ("/tmp/foo/", "foo"),  # noqa: S108 - path-leaf fixture, not a real tmp write
        (r"G:\Users\secret\project\\", "project"),
        ("/tmp/foo///", "foo"),  # noqa: S108 - path-leaf fixture, not a real tmp write
        # * Root / drive edge cases -- no full path, no drive residue.
        ("/", None),
        ("\\", None),
        ("C:\\", None),
        ("C:", None),
        # * Relative current-directory edge case (``.name`` is empty).
        (".", None),
        # * UNC paths (Windows grammar must reduce on POSIX hosts too).
        (r"\\server\share\folder", "folder"),
        (r"\\server\share\project\leaf", "leaf"),
        (r"\\server\share", None),
        (r"\\server\share\\", None),
        # * Extended Windows path forms (``\\?\`` local and ``\\?\UNC\``).
        (r"\\?\C:\Users\secret\project", "project"),
        (r"\\?\UNC\server\share\folder", "folder"),
        # * Forward-slash UNC-style absolute.
        ("//server/share/folder", "folder"),
    ],
)
def test_path_leaf_platform_independent(raw: str | None, expected: str | None) -> None:
    """``path_leaf`` must never emit a full path regardless of host path grammar."""
    leaf = path_leaf(raw)
    assert leaf == expected
    if leaf is not None:
        assert "/" not in leaf
        assert "\\" not in leaf
        assert ":" not in leaf


def test_acp_trace_schema_drops_none_and_forbids_sensitive_extras() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:0", tool="delegate", cli="fake", model="m1", depth=0):
        acp_trace(
            "queue",
            "enter",
            status="ok",
            prompt="SECRET PROMPT",
            env={"API_KEY": "x"},
            argv=["node", "--model", "secret"],
            paths={"cwd_leaf": "repo"},
            timing={"wait_s": 1.2},
        )
    rows = _lifecycle_lines(stream)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "queue" and row["phase"] == "enter"
    assert row["correlation_id"] == "voice:0" and row["cli"] == "fake"
    assert row["paths"] == {"cwd_leaf": "repo"}
    assert "prompt" not in row and "env" not in row and "argv" not in row
    assert "None" not in stream.getvalue()


def test_acp_trace_redacts_nested_and_primary_disallowed_fields() -> None:
    """Boundary must sanitize primary kwargs and nested maps, not only ``**extra``."""
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:0", tool="delegate"):
        acp_trace(
            "spawn",
            "exit",
            status="failed",
            message='Traceback (most recent call last):\n  File "G:\\secret\\x.py"',
            paths={
                "cwd_leaf": r"G:\Users\secret\project",
                "cwd": r"G:\Users\secret\project",
                "prompt": "nested-prompt",
            },
            launch={
                "command": r"C:\Program Files\nodejs\node.exe",
                "argv": ["node", "--model", "leak"],
                "extra_args": ["--secret"],
            },
            error={
                "code": "MODEL_UNAVAILABLE",
                "reexecution_safety": "safe",
                "stage": "model_selection",
                "message": "OSError: [Errno 2] G:\\secret\\missing",
                "exception": "raw boom",
                "answer": "should-not-appear",
            },
            timing={"elapsed_s": 1.5, "budget_s": 90.0, "prompt": "nope"},
            sandbox={"active": True, "kind": "copy", "env": {"TOKEN": "x"}, "root": r"G:\tmp\sbx"},
            answer="full answer text",
            partial="streamed chunk",
            env={"HOME": "/leak"},
        )
    rows = _lifecycle_lines(stream)
    assert len(rows) == 1
    row = rows[0]
    dumped = json.dumps(row)
    assert r"G:\Users" not in dumped and "G:/Users" not in dumped
    assert "Program Files" not in dumped
    assert "Traceback" not in dumped
    assert "OSError" not in dumped
    assert "full answer" not in dumped
    assert "streamed chunk" not in dumped
    assert "TOKEN" not in dumped
    assert "nested-prompt" not in dumped
    assert "--model" not in dumped
    assert "--secret" not in dumped
    assert row["paths"] == {"cwd_leaf": "project"}
    assert row["launch"] == {"command": "node.exe"}
    assert row["error"] == {
        "code": "MODEL_UNAVAILABLE",
        "reexecution_safety": "safe",
        "stage": "model_selection",
    }
    assert row["timing"] == {"elapsed_s": 1.5, "budget_s": 90.0}
    assert row["sandbox"] == {"active": True, "kind": "copy"}
    assert "message" not in row
    assert "answer" not in row and "partial" not in row and "env" not in row


def test_acp_trace_preserves_safe_error_codes_and_known_heartbeat_message() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:0"):
        acp_trace(
            "prompt",
            "heartbeat",
            status="awaiting",
            message="rutherford still awaiting prompt outcome",
            error={"code": "INTERNAL", "category": "setup_failed", "stage": "effort_selection"},
            timing={"elapsed_s": 30.0, "heartbeat_s": 30.0, "timeout_s": 120.0},
        )
    row = _lifecycle_lines(stream)[0]
    assert row["message"] == "rutherford still awaiting prompt outcome"
    assert row["error"] == {
        "code": "INTERNAL",
        "category": "setup_failed",
        "stage": "effort_selection",
    }


def test_log_format_off_silences_acp_trace() -> None:
    stream = io.StringIO()
    configure_logging("info", "off", stream=stream)
    with bind_acp_trace(correlation_id="voice:0", tool="delegate"):
        acp_trace("spawn", "enter")
        acp_trace("prompt", "heartbeat", status="awaiting")
    assert stream.getvalue() == ""


def test_log_level_filters_info_lifecycle() -> None:
    stream = io.StringIO()
    configure_logging("error", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:0"):
        acp_trace("queue", "enter", level=logging.INFO)
    assert stream.getvalue() == ""


async def test_prompt_heartbeat_rate_and_cleanup() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:0", heartbeat_s=0.05):
        beat = PromptHeartbeat(interval_s=0.05, timeout_s=10.0, pid=4242)
        await beat.start()
        await asyncio.sleep(0.18)
        await beat.stop()
    rows = _lifecycle_lines(stream)
    heartbeats = [row for row in rows if row.get("phase") == "heartbeat"]
    assert 2 <= len(heartbeats) <= 5
    for row in heartbeats:
        assert row["stage"] == "prompt"
        assert row["status"] == "awaiting"
        assert row["message"] == "rutherford still awaiting prompt outcome"
        assert "model live" not in str(row.get("message", "")).lower()
    pending = [task for task in asyncio.all_tasks() if "heartbeat" in (task.get_name() or "")]
    assert pending == []


async def test_prompt_heartbeat_disabled_at_zero() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(heartbeat_s=0.0):
        beat = PromptHeartbeat(interval_s=0.0)
        await beat.start()
        await asyncio.sleep(0.05)
        await beat.stop()
    assert _lifecycle_lines(stream) == []


def test_bind_acp_trace_nests_and_inherits() -> None:
    with bind_acp_trace(correlation_id="voice:1", tool="delegate", heartbeat_s=12.0):
        assert current_acp_trace().correlation_id == "voice:1"
        with bind_acp_trace(job_id="job-9"):
            ctx = current_acp_trace()
            assert ctx.correlation_id == "voice:1"
            assert ctx.job_id == "job-9"
            assert ctx.heartbeat_s == 12.0
        assert current_acp_trace().job_id is None


def test_path_leaf_accepts_path_objects(tmp_path: Path) -> None:
    assert path_leaf(tmp_path / "leaf-name") == "leaf-name"
