# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Helpers for tests that exercise the post-open pre-prompt budget exhaustion branch.

Production distinguishes:
- ``wait_for`` TimeoutError during sandbox open → "was not ready within …"
- open succeeds with remaining ≤ 0 → "exhausted the … pre-prompt budget"

These helpers force the second branch without racing a real ``git worktree`` open against a tight
``wait_for`` budget under xdist load.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest


def install_post_open_budget_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the next successful ``asyncio.wait_for``, make ``time.monotonic`` report the budget spent.

    Patches both ``time.monotonic`` and ``asyncio.wait_for`` so the open completes under the real
    wall clock, then the remaining-budget check in ``_run_sandboxed`` sees a fully spent budget.
    """
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


class LiteSandbox:
    """Minimal sandbox handle (``root`` + ``cleanup``) without git worktree I/O."""

    def __init__(self, root: str, cleanup: Callable[[], None] | None = None) -> None:
        self.root = root
        self._cleanup = cleanup

    def cleanup(self) -> None:
        if self._cleanup is not None:
            self._cleanup()


def lite_sandbox(*, root: str, cleanup: Callable[[], None] | None = None) -> LiteSandbox:
    """Build a :class:`LiteSandbox` for stubbing ``SandboxManager.open`` in budget tests."""
    return LiteSandbox(root, cleanup)
