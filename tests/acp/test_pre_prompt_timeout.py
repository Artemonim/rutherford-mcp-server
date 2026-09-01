# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Focused tests for the hard ACP pre-prompt deadline (cluster C2)."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from rutherford.acp.cooldown import CooldownTracker
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.enums import ReexecutionSafety, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import ConsensusRequest, ConsensusResult, DebateRequest, DelegationRequest, Target
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService
from tests.acp.post_open_budget import install_post_open_budget_exhaustion, lite_sandbox
from tests.paths import FAKE_ACP_CMD, REPO_ROOT

FAKE = AgentDescriptor("fake", "Fake", FAKE_ACP_CMD)
_READ_ONLY = PermissionPolicy(SafetyMode.READ_ONLY)
#: Tight enough that a 2s fake init delay trips it, loose enough that a normal fake spawn still fits.
_TIGHT_PRE_PROMPT_S = 1.0


def _git(path: Path, *args: str) -> None:
    """Run a git command in ``path`` (sync helper so async tests avoid ASYNC221)."""
    subprocess.run(  # noqa: S603 - fixed `git` argv0 plus internal test subcommands, no shell
        ["git", *args],  # noqa: S607 - `git` from PATH is deliberate in tests
        cwd=path,
        check=True,
        capture_output=True,
    )


def _git_repo(path: Path) -> None:
    """Seed a one-commit git repo so the write sandbox can open a worktree."""
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")


def _path_exists(root: str) -> bool:
    """Sync existence check (ASYNC240-safe when called from a sync assertion helper)."""
    return Path(root).exists()


async def test_pre_prompt_timeout_on_slow_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow initialize under a tight pre-prompt deadline is ACP_PRE_PROMPT_TIMEOUT (SAFE, no partial)."""
    monkeypatch.setenv("RUTHERFORD_FAKE_INIT_DELAY_S", "3")
    result = await run_acp_turn(
        FAKE,
        "what is 17 + 25?",
        policy=_READ_ONLY,
        cwd=str(REPO_ROOT),
        timeout_s=60.0,
        pre_prompt_timeout_s=_TIGHT_PRE_PROMPT_S,
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE
    assert result.partial is None
    assert result.error.details is not None
    assert result.error.details["stage"] == "initialize"
    assert result.error.details["budget_s"] == _TIGHT_PRE_PROMPT_S
    assert float(result.error.details["elapsed_s"]) >= _TIGHT_PRE_PROMPT_S
    # * Prompt text must never appear in structured details.
    assert "17 + 25" not in str(result.error.details)
    assert "17 + 25" not in result.error.message


async def test_post_prompt_timeout_unchanged_when_pre_prompt_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once prompt-ready, a hanging prompt still fails as ACP_TURN_TIMEOUT under timeout_s only."""
    monkeypatch.delenv("RUTHERFORD_FAKE_INIT_DELAY_S", raising=False)
    result = await run_acp_turn(
        FAKE,
        "HANG forever",
        policy=_READ_ONLY,
        cwd=str(REPO_ROOT),
        timeout_s=0.4,
        pre_prompt_timeout_s=30.0,
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_TURN_TIMEOUT
    assert result.error.reexecution_safety is not ReexecutionSafety.SAFE


async def test_delegate_pre_prompt_timeout_is_safe_fallback_and_unhealthy() -> None:
    """Delegation surfaces ACP_PRE_PROMPT_TIMEOUT as SAFE (fallback-eligible) and cooldown-unhealthy."""
    slow = AgentDescriptor(
        "slow",
        "Slow",
        FAKE_ACP_CMD,
        env_overrides=(("RUTHERFORD_FAKE_INIT_DELAY_S", "3"),),
    )
    alt = AgentDescriptor("alt", "Alt", FAKE_ACP_CMD)
    cooldown = CooldownTracker(threshold=1, window_s=120.0, duration_s=60.0)
    # * Per-agent tight budget on the primary only; the alternate keeps the generous global default.
    service = DelegationService(
        DescriptorRegistry([slow, alt]),
        RutherfordConfig(
            default_pre_prompt_timeout_s=30.0,
            agents={"slow": AgentConfig(pre_prompt_timeout_s=_TIGHT_PRE_PROMPT_S)},
        ),
        cooldown=cooldown,
    )
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="slow"),
            prompt="what is 17 + 25?",
            working_dir=str(REPO_ROOT),
            fallback=[Target(cli="alt")],
        )
    )
    assert result.ok is True
    assert "42" in result.text
    assert result.fallback_chain is not None
    assert any("slow" in label for label in result.fallback_chain)
    assert cooldown.is_benched("slow") is True


async def test_direct_panel_open_timeout_records_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct ACP-session panel paths bench a seat after a pre-prompt timeout."""

    async def fail_open(_: ACPSession) -> None:
        raise ACPHandshakeError(
            ErrorCode.ACP_PRE_PROMPT_TIMEOUT,
            "fake did not become prompt-ready within 1s (stage=initialize)",
            ReexecutionSafety.SAFE,
            details={"stage": "initialize", "budget_s": 1.0, "elapsed_s": 1.0},
        )

    monkeypatch.setattr(ACPSession, "open", fail_open)
    registry = DescriptorRegistry([FAKE])
    config = RutherfordConfig(cooldown_threshold=1, cooldown_window_s=60.0, cooldown_duration_s=60.0)

    consensus_cooldown = CooldownTracker(threshold=1, window_s=60.0, duration_s=60.0)
    consensus_delegation = DelegationService(registry, config, cooldown=consensus_cooldown)
    consensus = ConsensusService(consensus_delegation, registry, config, cooldown=consensus_cooldown)
    consensus_result = await consensus.consensus(
        ConsensusRequest(
            targets=[Target(cli="fake")],
            prompt="answer",
            working_dir=str(REPO_ROOT),
            time_budget_s=10.0,
        )
    )
    assert isinstance(consensus_result, ConsensusResult)
    assert consensus_result.voices[0].error is not None
    assert consensus_result.voices[0].error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert consensus_cooldown.is_benched("fake") is True

    debate_cooldown = CooldownTracker(threshold=1, window_s=60.0, duration_s=60.0)
    debate_delegation = DelegationService(registry, config, cooldown=debate_cooldown)
    debate = DebateService(registry, config, debate_delegation)
    debate_result = await debate.debate(
        DebateRequest(
            targets=[Target(cli="fake"), Target(cli="fake")],
            prompt="answer",
            working_dir=str(REPO_ROOT),
            rounds=1,
            synthesize=False,
        )
    )
    assert all(
        contribution.error is not None and contribution.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
        for contribution in debate_result.rounds[0].contributions
    )
    assert debate_cooldown.is_benched("fake") is True


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_pre_prompt_timeout_rejects_non_positive_request_values(value: float) -> None:
    """Every public request model rejects a non-positive per-call pre-prompt deadline."""
    with pytest.raises(ValidationError):
        DelegationRequest(target=Target(cli="fake"), prompt="answer", pre_prompt_timeout_s=value)
    with pytest.raises(ValidationError):
        ConsensusRequest(targets=[Target(cli="fake")], prompt="answer", pre_prompt_timeout_s=value)
    with pytest.raises(ValidationError):
        DebateRequest(targets=[Target(cli="fake"), Target(cli="fake")], prompt="answer", pre_prompt_timeout_s=value)


async def test_sandboxed_timeout_reports_configured_total_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout after sandbox preparation reports the total caller-configured pre-prompt budget."""
    total_budget_s = 2.0
    slow = AgentDescriptor(
        "slow",
        "Slow",
        FAKE_ACP_CMD,
        env_overrides=(("RUTHERFORD_FAKE_INIT_DELAY_S", "5"),),
    )
    service = DelegationService(
        DescriptorRegistry([slow]),
        RutherfordConfig(default_pre_prompt_timeout_s=total_budget_s),
    )
    real_open = service._sandbox.open

    def delayed_open(cwd: str) -> object:
        time.sleep(0.4)
        return real_open(cwd)

    monkeypatch.setattr(service._sandbox, "open", delayed_open)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="slow"),
            prompt="answer",
            working_dir=str(tmp_path),
            safety_mode=SafetyMode.PROPOSE,
            pre_prompt_timeout_s=total_budget_s,
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert result.error.details is not None
    assert result.error.details["stage"] != "sandbox"
    assert result.error.details["budget_s"] == total_budget_s
    # * Message quotes the configured total, not the post-sandbox remaining slice.
    assert f"within {total_budget_s:g}s" in result.error.message
    # * elapsed_s stays wall-clock over non-queue work (sandbox + open), at least the total budget.
    assert float(result.error.details["elapsed_s"]) >= total_budget_s - 0.1


async def test_semaphore_queue_wait_does_not_consume_pre_prompt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semaphore queue wait must not start open (so it cannot burn the pre-prompt clock)."""
    monkeypatch.delenv("RUTHERFORD_FAKE_INIT_DELAY_S", raising=False)
    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(max_concurrency=1, default_pre_prompt_timeout_s=30.0),
    )
    open_started = asyncio.Event()
    open_at: list[float] = []
    original_open = ACPSession.open

    async def tracking_open(self: ACPSession) -> None:
        open_at.append(time.monotonic())
        open_started.set()
        await original_open(self)

    monkeypatch.setattr(ACPSession, "open", tracking_open)
    await service.semaphore.acquire()
    task_started = time.monotonic()
    task = asyncio.create_task(
        service.delegate(
            DelegationRequest(
                target=Target(cli="fake"),
                prompt="what is 17 + 25?",
                working_dir=str(REPO_ROOT),
                pre_prompt_timeout_s=30.0,
            )
        )
    )
    # * While the slot is held, the queued turn must not open (pre-prompt clock stays idle).
    await asyncio.sleep(0.5)
    assert not open_started.is_set()
    service.semaphore.release()
    await open_started.wait()
    assert open_at[0] - task_started >= 0.4
    result = await task
    assert result.ok is True
    assert "42" in result.text


async def test_sandbox_pre_prompt_timeout_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A sandbox open that exceeds the pre-prompt budget fails SAFE and eventually cleans up the tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(trusted_workspaces=[str(repo)], default_pre_prompt_timeout_s=0.2),
    )
    real_open = service._sandbox.open
    cleaned: list[str] = []
    cleanup_done = asyncio.Event()

    def tracking_open(cwd: str) -> object:
        time.sleep(0.5)
        sandbox = real_open(cwd)
        real_cleanup = sandbox.cleanup

        def cleanup() -> None:
            cleaned.append(sandbox.root)
            real_cleanup()
            cleanup_done.set()

        sandbox.cleanup = cleanup  # type: ignore[method-assign]
        return sandbox

    monkeypatch.setattr(service._sandbox, "open", tracking_open)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(repo),
            safety_mode=SafetyMode.WRITE,
            trust_workspace=True,
            pre_prompt_timeout_s=0.2,
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE
    assert result.error.details is not None
    assert result.error.details["stage"] == "sandbox"
    await asyncio.wait_for(cleanup_done.wait(), timeout=5.0)
    assert cleaned, "stranded sandbox must be cleaned up on pre-prompt timeout"
    assert all(not _path_exists(root) for root in cleaned)


async def test_sandbox_pre_prompt_timeout_is_hard_for_caller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Caller gets ACP_PRE_PROMPT_TIMEOUT within budget; a blocking open cleans up only after return."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    budget_s = 0.25
    block_s = 1.5
    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(trusted_workspaces=[str(repo)], default_pre_prompt_timeout_s=budget_s),
    )
    real_open = service._sandbox.open
    cleaned: list[str] = []
    cleanup_done = asyncio.Event()
    open_finished = asyncio.Event()

    def blocking_open(cwd: str) -> object:
        time.sleep(block_s)
        sandbox = real_open(cwd)
        open_finished.set()
        real_cleanup = sandbox.cleanup

        def cleanup() -> None:
            cleaned.append(sandbox.root)
            real_cleanup()
            cleanup_done.set()

        sandbox.cleanup = cleanup  # type: ignore[method-assign]
        return sandbox

    monkeypatch.setattr(service._sandbox, "open", blocking_open)
    started = time.monotonic()
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(repo),
            safety_mode=SafetyMode.WRITE,
            trust_workspace=True,
            pre_prompt_timeout_s=budget_s,
        )
    )
    caller_elapsed = time.monotonic() - started
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE
    assert result.error.details is not None
    assert result.error.details["stage"] == "sandbox"
    # * Hard deadline: return without awaiting the still-blocking open thread.
    assert caller_elapsed < block_s
    assert caller_elapsed < budget_s + 0.75
    assert not open_finished.is_set(), "open must still be in flight when the caller returns"
    assert not cleaned, "cleanup must be deferred until the open finishes"
    await asyncio.wait_for(cleanup_done.wait(), timeout=5.0)
    assert open_finished.is_set()
    assert cleaned
    assert all(not _path_exists(root) for root in cleaned)


async def test_sandbox_pre_prompt_timeout_deferred_cleanup_consumes_late_open_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deadline return stays hard when a late sandbox open fails; deferred cleanup consumes the error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    budget_s = 0.25
    block_s = 1.5
    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(trusted_workspaces=[str(repo)], default_pre_prompt_timeout_s=budget_s),
    )
    open_finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    unhandled: list[BaseException] = []
    prior_handler = loop.get_exception_handler()

    def failing_open(cwd: str) -> object:
        time.sleep(block_s)
        open_finished.set()
        raise OSError("simulated late sandbox open failure")

    def capture_unhandled(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            unhandled.append(exc)
        elif prior_handler is not None:
            prior_handler(_loop, context)
        else:
            _loop.default_exception_handler(context)

    monkeypatch.setattr(service._sandbox, "open", failing_open)
    loop.set_exception_handler(capture_unhandled)
    try:
        started = time.monotonic()
        result = await service.delegate(
            DelegationRequest(
                target=Target(cli="fake"),
                prompt="what is 17 + 25?",
                working_dir=str(repo),
                safety_mode=SafetyMode.WRITE,
                trust_workspace=True,
                pre_prompt_timeout_s=budget_s,
            )
        )
        caller_elapsed = time.monotonic() - started
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
        assert result.error.reexecution_safety is ReexecutionSafety.SAFE
        assert result.error.details is not None
        assert result.error.details["stage"] == "sandbox"
        # * Hard deadline: return without awaiting the still-blocking open thread.
        assert caller_elapsed < block_s
        assert caller_elapsed < budget_s + 0.75
        assert not open_finished.is_set(), "open must still be in flight when the caller returns"
        pending = list(service._stranded_cleanup_tasks)
        assert len(pending) == 1, "deferred cleanup must retain a strong ref until the late open settles"
        # * Awaiting the cleanup task would raise if the late OSError were not consumed inside it.
        await asyncio.wait_for(pending[0], timeout=5.0)
        assert open_finished.is_set()
        assert not service._stranded_cleanup_tasks, "done callback must release the deferred-cleanup ref"
        assert not unhandled, f"late open failure must not surface as an unhandled task: {unhandled!r}"
    finally:
        loop.set_exception_handler(prior_handler)


async def test_sandbox_post_open_budget_exhausted_cleanup_is_hard_for_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Open succeeds but budget is already gone: caller returns without awaiting a slow cleanup."""
    # * Stub open (no git worktree) so a tight wait_for budget cannot race real sandbox I/O under xdist.
    repo = tmp_path / "repo"
    repo.mkdir()
    sandbox_root = tmp_path / "lite-sandbox"
    sandbox_root.mkdir()

    budget_s = 1.0
    cleanup_block_s = 1.5
    service = DelegationService(
        DescriptorRegistry([FAKE]),
        RutherfordConfig(trusted_workspaces=[str(repo)], default_pre_prompt_timeout_s=budget_s),
    )
    cleaned: list[str] = []
    cleanup_done = asyncio.Event()
    real_monotonic = time.monotonic

    def slow_cleanup() -> None:
        time.sleep(cleanup_block_s)
        cleaned.append(str(sandbox_root))
        # * Mimic real cleanup removing the leaf so the caller assertion stays meaningful.
        sandbox_root.rmdir()
        cleanup_done.set()

    def tracking_open(_cwd: str) -> object:
        return lite_sandbox(root=str(sandbox_root), cleanup=slow_cleanup)

    monkeypatch.setattr(service._sandbox, "open", tracking_open)
    install_post_open_budget_exhaustion(monkeypatch)

    started = real_monotonic()
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="what is 17 + 25?",
            working_dir=str(repo),
            safety_mode=SafetyMode.WRITE,
            trust_workspace=True,
            pre_prompt_timeout_s=budget_s,
        )
    )
    caller_elapsed = real_monotonic() - started
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_PRE_PROMPT_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE
    assert result.error.details is not None
    assert result.error.details["stage"] == "sandbox"
    assert result.error.details["budget_s"] == budget_s
    assert "exhausted the" in result.error.message
    # * Hard deadline: return without awaiting the deliberately slow cleanup.
    assert caller_elapsed < cleanup_block_s
    assert caller_elapsed < budget_s + 0.75
    assert not cleanup_done.is_set(), "slow cleanup must still be in flight when the caller returns"
    pending = list(service._stranded_cleanup_tasks)
    assert len(pending) == 1, "deferred cleanup must retain a strong ref until teardown finishes"
    await asyncio.wait_for(cleanup_done.wait(), timeout=5.0)
    await asyncio.wait_for(pending[0], timeout=5.0)
    assert cleaned
    assert all(not _path_exists(root) for root in cleaned)
    assert not service._stranded_cleanup_tasks, "done callback must release the deferred-cleanup ref"
