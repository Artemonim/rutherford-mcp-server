# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration: live Cursor ACP ``session/load`` resume (opt-in, -m integration).

Follows the goose resume pattern (unique marker in turn 1, ``resume_session_id`` on turn 2) via
:func:`rutherford.acp.session.run_acp_turn`. Skips when ``cursor-agent`` is missing. Deselected by
default with the rest of the integration suite.

Known limitation (documented in the no-prior-prompt test docstring and docs/troubleshooting.md):
Cursor's persisted store appears after the first prompt turn; ``meta.json`` alone is insufficient.
``session/load`` of a never-prompted or unknown id yields Session not found / ``RESUME_FAILED``.

Run without the unit-suite coverage fail-under (see docs/integration-testing.md)::

    uv run pytest tests/integration/test_cursor_session_load.py -m integration -s -q -o addopts=""
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from rutherford.acp.descriptors import default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode

pytestmark = pytest.mark.integration

#: Captured at import (before the hermetic-home autouse fixture) so Cursor can find auth + write acp-sessions.
_REAL_HOME = {key: os.environ[key] for key in ("USERPROFILE", "HOME") if key in os.environ}
_CURSOR_INSTALLED = shutil.which("cursor-agent") is not None
#: Live turn budget for a short read-only recall prompt.
_LIVE_TURN_TIMEOUT_S = 90.0
#: Cursor sandbox prep often needs more than the global 90s default; matches the Cursor recipe.
_LIVE_PRE_PROMPT_TIMEOUT_S = 300.0


@pytest.fixture
def _real_agent_home(_isolate_config_scopes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real home so Cursor finds credentials and writes under ``~/.cursor/acp-sessions``."""
    for key, value in _REAL_HOME.items():
        monkeypatch.setenv(key, value)


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_session_load_resumes_marker_after_prompt(_real_agent_home: None) -> None:
    """Marker in turn 1 → process exit → ``session/load`` same id → marker returns in turn 2.

    Each ``run_acp_turn`` closes the ACP session (kills the child). Recalling the marker on resume
    proves Cursor reloaded persistence via Rutherford's ``resume_session_id``, not a fresh session.
    """
    cursor = default_registry().get("cursor")
    marker = f"RUTHERFORD-CURSOR-RESUME-{uuid.uuid4().hex[:12]}"
    cwd = str(Path.cwd())
    policy = PermissionPolicy(SafetyMode.READ_ONLY)

    established = await run_acp_turn(
        cursor,
        f"Remember this exact token for later: {marker}. Reply with ONLY: OK. Do not call tools.",
        policy=policy,
        cwd=cwd,
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        pre_prompt_timeout_s=_LIVE_PRE_PROMPT_TIMEOUT_S,
    )
    assert established.ok is True, f"establishing the Cursor session failed: {established.error}"
    assert established.session_id is not None

    resumed = await run_acp_turn(
        cursor,
        "What exact token did I ask you to remember? Reply with ONLY that token. Do not call tools.",
        policy=policy,
        cwd=cwd,
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        pre_prompt_timeout_s=_LIVE_PRE_PROMPT_TIMEOUT_S,
        resume_session_id=established.session_id,
    )
    assert resumed.ok is True, f"resuming the Cursor session failed: {resumed.error}"
    assert resumed.session_id == established.session_id
    assert marker in resumed.text, f"resumed session did not recall the marker: {resumed.text!r}"


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_session_load_without_prior_prompt_fails(_real_agent_home: None) -> None:
    """``session/load`` of a never-prompted / invalid id must fail safely (``RESUME_FAILED``).

    Known Cursor limitation: the persisted session store appears after the first prompt turn.
    ``meta.json`` alone is insufficient for ``session/load``; loading an unknown id surfaces
    Session not found (or equivalent) as ``RESUME_FAILED``. This is expected, not a Rutherford bug.
    """
    cursor = default_registry().get("cursor")
    bogus_id = f"rutherford-never-prompted-{uuid.uuid4().hex}"
    result = await run_acp_turn(
        cursor,
        "Reply with ONLY: should-not-run. Do not call tools.",
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(Path.cwd()),
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        pre_prompt_timeout_s=_LIVE_PRE_PROMPT_TIMEOUT_S,
        resume_session_id=bogus_id,
    )
    assert result.ok is False, f"load of never-prompted id must fail; got text={result.text!r}"
    assert result.error is not None
    assert result.error.code is ErrorCode.RESUME_FAILED, (
        f"expected RESUME_FAILED for unknown session load, got {result.error.code}: {result.error.message}"
    )
    # * Safe failure: no successful answer on a silently minted fresh session.
    assert "should-not-run" not in (result.text or "").lower()
