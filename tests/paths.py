# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared filesystem anchors for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
FAKE_ACP_AGENT = TESTS_ROOT / "fake_acp_agent.py"
FAKE_ACP_CMD: tuple[str, ...] = (sys.executable, str(FAKE_ACP_AGENT))
