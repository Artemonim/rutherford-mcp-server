# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration coverage for ACP lifecycle stage ordering on stderr (C3)."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, Target
from rutherford.runtime.acp_trace import bind_acp_trace
from rutherford.runtime.logging import configure_logging
from rutherford.services.delegation import DelegationService, emit_activity
from tests.paths import FAKE_ACP_CMD, REPO_ROOT

FAKE = AgentDescriptor("fake", "Fake", FAKE_ACP_CMD)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    configure_logging("info", "off")


def _stages(stream: io.StringIO) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("event") != "acp_lifecycle":
            continue
        ordered.append((str(payload["stage"]), str(payload["phase"])))
    return ordered


def _lifecycle_rows(stream: io.StringIO) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("event") == "acp_lifecycle":
            rows.append(payload)
    return rows


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True, capture_output=True)
    (path / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


async def test_delegate_direct_lifecycle_order_on_stderr() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    service = DelegationService(DescriptorRegistry([FAKE]), RutherfordConfig(acp_prompt_heartbeat_s=0.0))
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(REPO_ROOT),
            timeout_s=60.0,
            pre_prompt_timeout_s=30.0,
        ),
        correlation_id="voice:0",
    )
    assert result.ok is True
    stages = _stages(stream)
    # * Delegate direct path: queue before spawn…effort, then prompt, then finish.
    assert ("queue", "enter") in stages
    assert ("queue", "exit") in stages
    assert stages.index(("queue", "enter")) < stages.index(("spawn", "enter"))
    assert stages.index(("spawn", "enter")) < stages.index(("initialize", "enter"))
    assert stages.index(("initialize", "enter")) < stages.index(("session", "enter"))
    assert stages.index(("session", "enter")) < stages.index(("model_selection", "enter"))
    assert stages.index(("model_selection", "enter")) < stages.index(("effort_selection", "enter"))
    assert stages.index(("effort_selection", "exit")) < stages.index(("prompt", "enter"))
    assert stages.index(("prompt", "enter")) < stages.index(("finish", "exit"))
    finish = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("stage") == "finish"
    )
    assert finish["status"] == "ok"
    assert finish["correlation_id"] == "voice:0"
    assert finish["cli"] == "fake"
    assert "prompt" not in finish
    assert "answer" not in finish
    # * Safe launch identifier only (basename), never full argv.
    spawn_enter = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("stage") == "spawn" and json.loads(line).get("phase") == "enter"
    )
    assert "launch" in spawn_enter
    assert "\\" not in spawn_enter["launch"]["command"] or spawn_enter["launch"]["command"].endswith(
        (".exe", "python", "py")
    )


async def test_sync_delegate_does_not_invoke_activity_callback_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quiet C1: healthy sync delegate stays on stderr only -- no ActivityCallback side effect."""
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    activity_callbacks: list[object] = []
    real_emit = emit_activity

    def tracking_emit(on_activity: object, event: object) -> None:
        activity_callbacks.append(on_activity)
        real_emit(on_activity, event)  # type: ignore[arg-type]

    monkeypatch.setattr("rutherford.services.delegation.emit_activity", tracking_emit)
    monkeypatch.setattr(
        "rutherford.server.make_progress_pusher",
        MagicMock(side_effect=AssertionError("sync delegate must not build an MCP progress pusher")),
    )

    service = DelegationService(DescriptorRegistry([FAKE]), RutherfordConfig(acp_prompt_heartbeat_s=0.0))
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(REPO_ROOT),
            timeout_s=60.0,
        ),
        correlation_id="voice:0",
        on_activity=None,
    )
    assert result.ok is True
    assert any(row.get("event") == "acp_lifecycle" for row in _lifecycle_rows(stream))
    assert activity_callbacks, "delegate still routes through emit_activity"
    assert all(cb is None for cb in activity_callbacks)


async def test_sandbox_budget_exhausted_emits_single_failed_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful open that leaves no remaining pre-prompt budget must not emit ok then failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(trusted_workspaces=[str(repo)], acp_prompt_heartbeat_s=0.0),
    )
    real_monotonic = time.monotonic
    past_open = {"flag": False}
    anchor = real_monotonic()
    real_wait_for = asyncio.wait_for

    def fake_monotonic() -> float:
        # * Only after sandbox open resolves: report the whole budget already spent.
        if past_open["flag"]:
            return anchor + 10.0
        return real_monotonic()

    async def wait_for_then_exhaust(awaitable: object, *args: object, **kwargs: object) -> object:
        result: object = await real_wait_for(awaitable, *args, **kwargs)  # type: ignore[arg-type]
        past_open["flag"] = True
        return result

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(asyncio, "wait_for", wait_for_then_exhaust)

    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(repo),
            safety_mode=SafetyMode.WRITE,
            trust_workspace=True,
            pre_prompt_timeout_s=1.0,
        ),
        correlation_id="voice:sbx",
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    # * Distinguishes the post-open remaining<=0 path from wait_for TimeoutError during open.
    assert "exhausted the" in result.error.message

    sandbox_exits = [
        row for row in _lifecycle_rows(stream) if row.get("stage") == "sandbox" and row.get("phase") == "exit"
    ]
    assert len(sandbox_exits) == 1
    assert sandbox_exits[0]["status"] == "failed"
    assert sandbox_exits[0]["error"]["code"] == ErrorCode.ACP_PRE_PROMPT_TIMEOUT.value
    assert "sandbox_leaf" in sandbox_exits[0].get("paths", {})
    assert not any(
        row.get("stage") == "sandbox" and row.get("phase") == "exit" and row.get("status") == "ok"
        for row in _lifecycle_rows(stream)
    )


async def test_bind_without_activity_still_emits_stderr() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    with bind_acp_trace(correlation_id="voice:9", tool="delegate", cli="fake"):
        from rutherford.runtime.acp_trace import acp_trace

        acp_trace("queue", "enter")
    rows = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert rows[0]["correlation_id"] == "voice:9"
