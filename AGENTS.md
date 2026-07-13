# AGENTS

## Doctrine

- This is an **Artemonim fork** of upstream [chapmanjw/rutherford-mcp-server](https://github.com/chapmanjw/rutherford-mcp-server) (MIT). Work on `dev`; `main` tracks upstream.
- Rutherford is a stdio MCP server that orchestrates other coding agents over the [Agent Client Protocol (ACP)](https://agentclientprotocol.com). It is the ACP *client*; each coding agent is an ACP *agent* (spawned over stdio). It never calls a model provider API directly and never scrapes agent stdout — the protocol delivers the answer, usage, tool activity, and permissions.
- Adding an agent is config (`[agents.<id>]`) or a built-in `AgentDescriptor`, never a code adapter. See `docs/adding-an-agent.md`.
- Default `SafetyMode` is `read_only`; `write` / `yolo` are explicit opt-in behind a trusted-workspace check.

## Quality Gate

- Primary command: `./run.ps1 -SkipLaunch` (full AE2 run without smoke).
- Intermediate agent check: `.venv/Scripts/Activate.ps1 && ./run.ps1 -Fast -SkipLaunch` (or just `./run.ps1 -Fast -SkipLaunch` — stages use `uv run`).
- **Run the quality gate at the end of any work.** Use other tools only when the runner is insufficient.
- Local commits are blocked by the pre-commit guard (`scripts/pre_commit_ci_guard.py` via `.githooks/pre-commit`) unless the current HEAD has a fresh successful `.ci_cache/report.json`.
- For Cursor Agents: `block_until_ms=600000` or higher. If the terminal times out, decide whether to wait ~5 more minutes or kill the job.

### Toolchain

Details: [`docs/ci.md`](docs/ci.md).

- **uv** — install deps (`uv sync`) and all Python CI tools (`uv run …`).
- **ruff** — `format` + `check` (blocking), including flake8-bandit (`S`). `S101`/`S311` are disabled; under `tests/**` the whole `S` set is ignored. Suppress with `# noqa: S… (reason)`.
- **mypy** + **compileall** — `compile` stage (strict).
- **pytest** + **pytest-cov** + **pytest-xdist** — unit suite (`-n logical`); integration deselected by default (`-m 'not integration'`). Aggregate floor **90%**; after coverage, `scripts/check_per_file_coverage.py` (**80%** per-file). Do not weaken. Use `-n0` for serial debugging.
- **license headers** — `scripts/check_license_headers.py` (two-line SPDX).
- **pip-audit** — `security` stage (dependency CVEs; source-level security lives in `lint`).
- **PSScriptAnalyzer** — PowerShell `self-check`.
- **codebase-memory-mcp** — warn-only `index_repository` (`mode=full`, `persistence=false`).

! Do not use standalone `bandit`; `# nosec` is ignored by ruff — use `# noqa: S…` only.

## Glossary

- runner — `run.ps1`
- orchestrator — `build.ps1`
- Python stage runner — `build.py`
- ACP — Agent Client Protocol
- MCP — Model Context Protocol
- voice — one agent in a panel / consensus / debate

**`build.ps1` / `run.ps1` exit codes:** `0` (overall ok/warn) or `1` (any stage `fail`).

## Architecture

Layers (dependencies point inward toward domain):

```
MCP tool layer (FastMCP)   src/rutherford/server.py + tools/   thin wrappers
        |
services (orchestration)   services/   delegation, consensus, debate, jobs, roles
        |
ACP runtime                acp/   session, journal, permission, descriptors, roster, conformance
        |
domain + config            domain/, config/, io/   models, enums, errors, config
```

### Key seams

- **`AgentDescriptor` / `DescriptorRegistry` (`acp/descriptors.py`)** — id, display name, launch `command`, optional `provider`, `default_model`, handshake budget, env. `HIGH_FIDELITY` is the built-in roster.
- **`ACPSession` / `run_acp_turn` (`acp/session.py`)** — spawn, handshake, multi-turn `session/prompt` on one live session (debate: one session per voice). Journal → `DelegationResult`.
- **`EventJournal` (`acp/journal.py`)** — event-sourced turn record; answer/usage/tools are derived, never scraped from stdout.
- **`PermissionPolicy` (`acp/permission.py`)** — SafetyMode → ACP permission / fs / terminal decisions.
- **Roster (`acp/roster.py`)** — `build_registry(config)`: built-ins → auto-detected local models → config overrides → `enabled_agents` filter.

### Conventions

- Python 3.11+, mypy strict, ruff (120-char lines), SPDX header on every source file.
- Docstrings on the public API of acp / services / domain.
- Tool payloads: TOON via `io/serialize.py`.
- No emojis in source unless a user-visible string clearly benefits.

## Known issues

- **Cursor ACP model routing:** Cursor inference follows the launch `--model` flag (`model_launch_flag` on the descriptor), not in-session `set_config_option` / `set_model` (those can echo `currentValue` without changing runtime). Envelope `provenance.confirmed` stays false for launch selection — ACP does not attest the runtime model. Launch advertisement validation accepts compound ids that differ only in a boolean `fast=` value (exact `--model` argv is preserved). Live Cursor/entitlement may still write a `*-fast` runtime slug in `store.db`; family routing is the reliable check, not a non-fast runtime assertion.
- **ACP SDK model channels:** `agent-client-protocol` 0.10.x exposes unstable `session.models` + `set_session_model`; 0.11+ removes both and keeps stable `config_options`. Rutherford treats the legacy channel as optional (defensive access + capability-gated `set_session_model`) so a config-only SDK does not INTERNAL on open.

## Documentation Map

Project docs are root `*.md` and `docs/`. When you add a file under `docs/` or a new root markdown file, update this section (otherwise `agents-coverage` warns).

### Root

- `README.md` — human runbook / upstream overview.
- `AGENTS.md` — operational contract for agents (this file).
- `CHANGELOG.md` — release history.
- `CONTRIBUTING.md` — contribution / upstream conventions.
- `SECURITY.md` — security policy.
- `LICENSE` — MIT.
- `pyproject.toml` / `uv.lock` / `justfile` — tooling and shortcuts.
- `run.ps1` / `build.ps1` / `build.py` / `PSScriptAnalyzerSettings.psd1` — local AE2 CI.

### docs/

- `docs/ci.md` — local CI (stages, flags, toolset, artifacts).
- `docs/architecture.md` — server architecture.
- `docs/adding-an-agent.md` — how to add an agent (config / descriptor).
- `docs/configuration.md` — configuration.
- `docs/mcp-client-integration.md` — MCP client wiring.
- `docs/integration-testing.md` — integration suite (real ACP agents).
- `docs/local-models.md` — local models.
- `docs/bedrock.md` — Bedrock-related notes.
- `docs/recipes.md` — practical recipes.
- `docs/security.md` — security / safety modes.
- `docs/troubleshooting.md` — diagnostics.
- `docs/images/` — README assets (logo, etc.).

### scripts/

- `scripts/check_license_headers.py` — SPDX header gate.
- `scripts/check_per_file_coverage.py` — per-file coverage floor (80%).
- `scripts/pre_commit_ci_guard.py` — freshness guard for `.ci_cache/report.json`.

## Code Map

- `src/rutherford/` — main package.

### Packages

- `src/rutherford/server.py` — FastMCP entry / tool surface registration.
- `src/rutherford/__main__.py` — `python -m rutherford` (includes `--smoke`).
- `src/rutherford/context.py` — runtime context helpers.
- `src/rutherford/acp/` — ACP runtime: `session`, `journal`, `permission`, `descriptors`, `roster`, `conformance`.
- `src/rutherford/services/` — orchestration: delegation, consensus, debate, jobs, roles.
- `src/rutherford/tools/` — thin MCP tool wrappers (validate → service → envelope).
- `src/rutherford/domain/` — models, enums, errors, error codes.
- `src/rutherford/config/` — config loading / agent overrides.
- `src/rutherford/io/` — serialization (TOON) and I/O seams.
- `src/rutherford/runtime/` — process/runtime helpers.
- `src/rutherford/roles/` — built-in role personas (`.md` package data).
- `tests/` — unit suite packaged to mirror `src/rutherford/` (`acp/`, `services/`, `tools/`, …) plus top-level `fake_acp_agent.py`, `paths.py`, and `integration/` (marked, deselected by default).

## Read-Only And Generated Areas

Do not commit or treat as source of truth:

- `.ci_cache/` — local CI reports and logs
- `.enforcer/` — Enforcer logs
- `.venv/` — uv-managed virtualenv
- `.codebase-memory/` — derived MCP navigation cache (CI uses `persistence=false` and usually does not write an artifact)

## Critical Hotspots

- `src/rutherford/acp/session.py` — handshake / prompt / failure classification
- `src/rutherford/acp/permission.py` — safety → ACP decisions
- `src/rutherford/acp/roster.py` / `descriptors.py` — agent registry
- `src/rutherford/services/` — multi-agent orchestration semantics
- `scripts/check_per_file_coverage.py` — do not lower FLOOR without an explicit decision

## Upstream vs fork

- Upstream packaging/CI (GitHub Actions, `just check`) remain valid.
- This fork adds AE2 local CI (`run.ps1`) and this operational contract.
- Do not weaken coverage floors just to green the AE2 runner.
