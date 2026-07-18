# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Pure helpers for Cursor ACP runtime modelName family matching.

Shared by unit and integration tests. Matching is case-insensitive ``startswith`` against explicit
prefixes (not a bare substring). Runtime speed (``*-fast``) is not inspected.
"""

from __future__ import annotations

#: Allowed ``providerOptions.cursor.modelName`` prefixes per launch family (case-insensitive startswith).
#: Explicit prefixes avoid false positives from unrelated ids that merely contain a family substring.
_RUNTIME_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    "grok": ("grok-", "cursor-grok-"),
    "composer": ("composer-",),
}


def _runtime_matches_family(names: list[str], family: str) -> bool:
    """True when any runtime name starts with an allowed prefix for ``family``.

    Matching is case-insensitive ``startswith`` against :data:`_RUNTIME_FAMILY_PREFIXES` (not a bare
    substring). Unknown families never match. Runtime speed (``*-fast``) is not inspected.
    """
    prefixes = _RUNTIME_FAMILY_PREFIXES.get(family.lower())
    if not prefixes:
        return False
    for name in names:
        lowered = name.lower()
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return True
    return False
