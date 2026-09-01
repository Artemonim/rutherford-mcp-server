# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration: live Cursor launch ``--model`` routing vs session store.db (opt-in, -m integration).

Proves the only working Cursor contract on cursor-agent 2026.06+: the effective model rides the process
launch argv (``cursor-agent acp --model <id>``), not in-session ACP ``set_config_option`` /
``set_model``. Opt-in ids cover bracket forms (``composer-2.5[fast=…]``, Grok effort) and a bare
``composer-2.5``. A unique marker ties Rutherford's ``session_id`` to
``~/.cursor/acp-sessions/<id>/store.db``; the blob graph is searched for
``providerOptions.cursor.modelName`` matching the expected runtime family via explicit prefixes
(Grok: ``grok-`` / ``cursor-grok-``; Composer: ``composer-``), not a bare substring. A ``*-fast``
runtime slug is not treated as failure: live Cursor/entitlement often forces fast even when launch
requested ``fast=false``.

Skips when ``cursor-agent`` is missing, the turn fails, or the session DB is absent -- no user-specific
paths (resolves under ``Path.home()`` after restoring the real home). Deselected by default with the rest
of the integration suite; do not run as part of ordinary unit CI.

Run without the unit-suite coverage fail-under (see docs/integration-testing.md)::

    uv run pytest tests/integration/test_cursor_model_routing.py -m integration -s -q -o addopts=""
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from rutherford.acp.descriptors import default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.domain.enums import ModelConfirmation, ModelRoutingChannel, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationResult
from tests.acp.cursor_runtime import _runtime_matches_family

pytestmark = pytest.mark.integration

#: Captured at import (before the hermetic-home autouse fixture) so Cursor can find auth + write acp-sessions.
_REAL_HOME = {key: os.environ[key] for key in ("USERPROFILE", "HOME") if key in os.environ}
_CURSOR_INSTALLED = shutil.which("cursor-agent") is not None
#: Exact user opt-in Grok bracket id (launch ``--model``); family routing must land a Grok runtime.
_GROK_MODEL = "grok-4.5[effort=high,fast=false]"
#: Exact Composer bracket with ``fast=false`` (launch ``--model``); family check only.
_COMPOSER_MODEL_FAST_FALSE = "composer-2.5[fast=false]"
#: Exact Composer bracket with ``fast=true`` (plan §5 preferred form); family check only.
_COMPOSER_MODEL_FAST_TRUE = "composer-2.5[fast=true]"
#: Bare Composer id without bracket params; family routing must still land Composer.
_COMPOSER_MODEL_BARE = "composer-2.5"
#: Deliberately unknown launch id -- must not succeed on a silent default model.
_UNKNOWN_MODEL = "definitely-not-a-real-model"
#: Live turn budget; Cursor ACP handshake + short read-only reply should finish well under this.
_LIVE_TURN_TIMEOUT_S = 90.0
#: Cursor sandbox prep often needs more than the global 90s default; matches the Cursor recipe.
_LIVE_PRE_PROMPT_TIMEOUT_S = 300.0
#: Failure codes observed (or plausible) when Cursor rejects an unknown launch ``--model``.
_UNKNOWN_MODEL_FAILURE_CODES = frozenset(
    {
        ErrorCode.ACP_HANDSHAKE_FAILED,
        ErrorCode.ACP_SPAWN_FAILED,
        ErrorCode.ACP_TURN_ERROR,
        ErrorCode.ACP_REFUSED,
        ErrorCode.ACP_EMPTY_ANSWER,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.ACP_PRE_PROMPT_TIMEOUT,
    }
)


@pytest.fixture
def _real_agent_home(_isolate_config_scopes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real home so Cursor finds credentials and writes under ``~/.cursor/acp-sessions``."""
    for key, value in _REAL_HOME.items():
        monkeypatch.setenv(key, value)


def _acp_sessions_root() -> Path:
    """Cursor's ACP session store under the real home (not hermetic tmp)."""
    return Path.home() / ".cursor" / "acp-sessions"


def _model_names_from_store(db_path: Path) -> list[str]:
    """Collect ``providerOptions.cursor.modelName`` strings from a closed session ``store.db`` (read-only)."""
    names: list[str] = []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("select data from blobs").fetchall()
    for (data,) in rows:
        if not data:
            continue
        try:
            text = bytes(data).decode("utf-8", "replace")
        except Exception:  # noqa: S112 - an undecodable blob is expected here; logging each would be noise
            continue
        if "modelName" not in text and "providerOptions" not in text:
            continue
        # * Prefer structured JSON walks; fall back to a regex when a blob is a fragment.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            names.extend(re.findall(r'"modelName"\s*:\s*"([^"]+)"', text))
            continue
        names.extend(_walk_model_names(payload))
    return names


def _walk_model_names(node: object) -> list[str]:
    """Depth-first collect cursor modelName values from nested JSON."""
    found: list[str] = []
    if isinstance(node, dict):
        cursor = node.get("cursor")
        if isinstance(cursor, dict):
            name = cursor.get("modelName")
            if isinstance(name, str):
                found.append(name)
        provider = node.get("providerOptions")
        if isinstance(provider, dict):
            found.extend(_walk_model_names(provider))
        for value in node.values():
            found.extend(_walk_model_names(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_model_names(item))
    return found


async def _assert_launch_model_in_session_db(*, model: str, runtime_family: str) -> DelegationResult:
    """Run a read-only launch-``--model`` turn and assert store.db records ``runtime_family``.

    Envelope stays honest: ``selected_model`` stays unset; ``provenance.confirmed`` is false for launch
    argv (ACP has no runtime attestation). ``routing_channel=launch_argv`` and
    ``model_confirmation=intent_only`` are the correct Cursor success diagnostics. Family match uses
    explicit runtime prefixes (see :func:`tests.acp.cursor_runtime._runtime_matches_family`). A
    ``*-fast`` runtime slug is allowed: Cursor may force fast despite an exact ``fast=false`` launch id.
    """
    sessions_root = _acp_sessions_root()
    if not sessions_root.is_dir():
        pytest.skip("Cursor acp-sessions directory is missing under the real home")

    marker = f"RUTHERFORD-CURSOR-MODEL-{uuid.uuid4().hex[:12]}"
    cursor = default_registry().get("cursor")
    assert cursor.model_launch_flag == "--model"
    prompt = (
        f"Reply with ONLY the token {marker} and nothing else. Do not call tools. This is a read-only identity check."
    )
    result = await run_acp_turn(
        cursor,
        prompt,
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(Path.cwd()),
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        pre_prompt_timeout_s=_LIVE_PRE_PROMPT_TIMEOUT_S,
        model=model,
    )
    assert result.ok is True, f"cursor turn failed: {result.error}"
    assert result.session_id is not None
    assert result.argv is not None
    assert result.argv[-2:] == ["--model", model]
    assert result.requested_model == model
    assert result.target.model == model
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False
    assert result.provenance.routing_channel is ModelRoutingChannel.LAUNCH_ARGV
    assert result.provenance.model_confirmation is ModelConfirmation.INTENT_ONLY

    store = sessions_root / result.session_id / "store.db"
    if not store.is_file():
        pytest.skip(f"Cursor session store missing for session_id={result.session_id}")

    model_names = _model_names_from_store(store)
    assert model_names, f"no providerOptions.cursor.modelName in {store}"
    assert _runtime_matches_family(model_names, runtime_family), (
        f"expected a {runtime_family!r} runtime modelName after launch --model {model!r}, "
        f"got {model_names!r}; marker={marker} session_id={result.session_id}"
    )
    return result


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_grok_in_session_db(_real_agent_home: None) -> None:
    """Read-only turn with launch ``--model`` Grok fast=false; session store must record a Grok family runtime."""
    await _assert_launch_model_in_session_db(model=_GROK_MODEL, runtime_family="grok")


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_composer_fast_false_in_session_db(_real_agent_home: None) -> None:
    """Launch ``--model`` Composer ``fast=false``; session store must record a Composer family runtime."""
    await _assert_launch_model_in_session_db(model=_COMPOSER_MODEL_FAST_FALSE, runtime_family="composer")


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_composer_fast_true_in_session_db(_real_agent_home: None) -> None:
    """Launch ``--model`` Composer ``fast=true`` (plan §5 preferred form); family check only, not exact slug."""
    await _assert_launch_model_in_session_db(model=_COMPOSER_MODEL_FAST_TRUE, runtime_family="composer")


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_composer_bare_id_in_session_db(_real_agent_home: None) -> None:
    """Bare launch ``--model composer-2.5`` (no brackets) must still land a Composer family runtime."""
    await _assert_launch_model_in_session_db(model=_COMPOSER_MODEL_BARE, runtime_family="composer")


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_independent_sessions_do_not_bleed(_real_agent_home: None) -> None:
    """Two independent turns (Grok then Composer) must each record their own launch family in store.db.

    Catches a global-default bleed where the second session would inherit the first model's runtime.
    """
    grok = await _assert_launch_model_in_session_db(model=_GROK_MODEL, runtime_family="grok")
    composer = await _assert_launch_model_in_session_db(model=_COMPOSER_MODEL_FAST_TRUE, runtime_family="composer")
    assert grok.session_id != composer.session_id


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_unknown_launch_model_does_not_succeed_on_default(_real_agent_home: None) -> None:
    """Unknown launch ``--model`` must fail the turn; it must not silently answer on a default model.

    Cursor may surface this as ``ACP_HANDSHAKE_FAILED`` (or another current spawn/handshake/turn failure).
    Production code must not broadly remap ``Connection closed`` to ``MODEL_UNAVAILABLE``.
    """
    cursor = default_registry().get("cursor")
    assert cursor.model_launch_flag == "--model"
    marker = f"RUTHERFORD-CURSOR-UNKNOWN-{uuid.uuid4().hex[:12]}"
    result = await run_acp_turn(
        cursor,
        f"Reply with ONLY the token {marker} and nothing else. Do not call tools.",
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(Path.cwd()),
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        pre_prompt_timeout_s=_LIVE_PRE_PROMPT_TIMEOUT_S,
        model=_UNKNOWN_MODEL,
    )
    assert result.ok is False, (
        f"unknown launch model must not succeed on a default; got ok text={result.text!r} "
        f"session_id={result.session_id}"
    )
    assert result.error is not None
    assert result.error.code in _UNKNOWN_MODEL_FAILURE_CODES, (
        f"unexpected failure code for unknown model: {result.error.code} ({result.error.message})"
    )
    # * If argv was built, the unknown id must still be on launch -- never a swapped default.
    if result.argv is not None and "--model" in result.argv:
        model_idx = result.argv.index("--model")
        assert result.argv[model_idx + 1] == _UNKNOWN_MODEL
    # * A successful default answer would echo the marker; absence reinforces ok=False.
    assert marker not in (result.text or "")
    assert marker not in (result.partial or "")
