# Local CI

Local CI for this fork follows the [Agent Enforcer 2](https://github.com/Artemonim/AgentEnforcer2) (AE2) blueprint for Rutherford’s Python/`uv` stack. Goal: one quality gate before handing off work, with compact console output and a machine-readable `.ci_cache/report.json`.

Agent operational contract: [`AGENTS.md`](../AGENTS.md).

## Three tiers

| Tier | File | Responsibility |
|------|------|----------------|
| 1. Wrapper | `run.ps1` | Flags, help, forward to the orchestrator. |
| 2. Orchestrator | `build.ps1` | Stages, cache, self-check, report, smoke launch. |
| 3. Language | `build.py` | Python stages via `uv run`. |

## Commands

```powershell
.\run.ps1 -SkipLaunch              # full checks without smoke
.\run.ps1 -Fast -SkipLaunch        # fast agent loop (skip coverage/security)
.\run.ps1                          # checks + smoke (`python -m rutherford --smoke`)
.\run.ps1 -Help
```

> Standard final agent check: `./run.ps1 -SkipLaunch`. Intermediate: `./run.ps1 -Fast -SkipLaunch`. For Cursor: `block_until_ms ≥ 600000`.

Run `uv sync` before the first pass if `.venv` is missing. Activating the venv is optional when using `uv run`, but fine under local convention:

```powershell
.venv\Scripts\Activate.ps1; .\run.ps1 -Fast -SkipLaunch
```

## Stage matrix

Approximate order: `self-check → line-endings → agents-coverage → fmt → lint → line-limits → license-check → compile → test|coverage → security → codebase-memory → build/db-checks (n/a) → launch → archive (n/a)`.

| Stage | Decision | Reason |
|---|---|---|
| `self-check` | implement | Parser + PSScriptAnalyzer for `run.ps1`/`build.ps1`, plus format/lint/compile for `build.py`. |
| `line-endings` | implement | Repo policy is LF (`.gitattributes`); stage normalizes text to LF (`--eol-fix`). |
| `agents-coverage` | implement (warn-only) | `AGENTS.md` must cover `docs/` and `scripts/` (file or ancestor directory). Missing `AGENTS.md` → `fail`. |
| `fmt` | implement | `uv run ruff format` on `src/` and `tests/` (autofix by default). |
| `lint` | implement | `uv run ruff check` with `S` (flake8-bandit), `C901`, `PLR0915`. `S101`/`S311` disabled globally; under `tests/**` only `S101` is ignored. |
| `line-limits` | implement | Executable LOC for `src/**/*.py`, `tests/**/*.py`, and root `*.ps1`; excludes `build.py`/`build.ps1`. Folder fan-out: warn≥35, fail≥70. |
| `license-check` | implement | Rutherford-specific: `uv run python scripts/check_license_headers.py`. |
| `compile` | implement | `compileall` + `uv run mypy` (strict). |
| `test` | implement | `uv run pytest` with pytest-xdist (`-n logical`; override `-n0` for serial). Integration already deselected via pyproject `addopts`. In full profile, covered by the `coverage` stage. |
| `coverage` | implement | pytest-cov (`--cov-fail-under=90`, `parallel=true` for xdist) + `scripts/check_per_file_coverage.py` (floor 80%). Do not weaken. |
| `security` | implement | `pip-audit` via `uvx`/`uv run`; offline/missing → `warn`. Source-level security lives in `lint` (`S`). |
| `codebase-memory` | implement (warn-only) | `codebase-memory-mcp` `index_repository` `mode=full` `persistence=false`; missing binary → `warn`. |
| `build` | not_applicable | No distributable artifact pipeline. |
| `launch` | implement (smoke) | `uv run python -m rutherford --smoke`. Skipped by `-SkipLaunch`. |
| `db-checks` | not_applicable | No Alembic/database. |
| `archive` | not_applicable | No release artifacts. |

### Stage statuses

| Status | Meaning |
|--------|---------|
| `ok` | Success |
| `warn` | Non-critical; CI continues |
| `fail` | Critical; overall `FAIL`, exit `1` |
| `cached` | Skipped via cache |
| `skip` | Skipped by flag/profile/N/A |

## Default toolset

| Tool | Purpose |
|---|---|
| `uv` + `ruff` | format + lint (including `S`) |
| `mypy` + `compileall` | compile stage |
| `pytest` + `pytest-cov` + `pytest-xdist` | test / coverage (parallel workers via `-n logical`) |
| `scripts/check_license_headers.py` | SPDX headers |
| `scripts/check_per_file_coverage.py` | per-file coverage floor |
| `pip-audit` | dependency advisories |
| `PSScriptAnalyzer` | PowerShell self-check |
| `codebase-memory-mcp` | warn-only graph refresh |

## Profiles and flags

| Flag | Effect |
|------|--------|
| (default) | Full profile: all implement stages. |
| `-Fast` | Skip only `coverage` and `security` (`lint` `S`, agents-coverage, codebase-memory, license-check remain). |
| `-SkipLaunch` | Skip smoke `launch`. |
| `-NoCache` / `-ForceAll` | Ignore stage cache. |
| `-Clean` | Remove `.ci_cache/` before the run. |
| `-Help` | Show help. |

## Thresholds

- **coverage:** aggregate fail-under **90%** (pytest-cov) + per-file floor **80%** (`scripts/check_per_file_coverage.py`). Do not weaken.
- **security:** advisories / offline usually → `warn` (softer than coverage).
- **agents-coverage:** missing `AGENTS.md` → `fail`; undescribed `docs/`/`scripts/` → `warn`.
- **license-check:** missing SPDX header → `fail`.
- **line-limits:** folder fan-out warn≥35, fail≥70; LOC warn/fail thresholds in `build.py`.

## Artifacts

| Path | Purpose |
|------|---------|
| `.ci_cache/report.json` | AE2 report (`schema_version: 1`, `ci.passed`, git HEAD) |
| `.ci_cache/logs/` | Tool logs |
| `.enforcer/Enforcer_*.log` | Snapshot / history |

Console output prints **fail/warn problems immediately** after each stage (structured issues + capped log excerpts), then repeats a compact `PROBLEMS:` block in the final summary. Caps live in `build.ps1` (`ProblemDisplayLimits`: issues/stage, excerpt lines/chars). Full detail remains in `.ci_cache/logs/` and `report.json`.

These directories are gitignored. The pre-commit guard (`.githooks/pre-commit` → `scripts/pre_commit_ci_guard.py`) blocks a commit without a fresh successful report for the current HEAD.

## Expected CI files

- `run.ps1` — thin wrapper
- `build.ps1` — orchestrator
- `build.py` — Python stage runner
- `.githooks/pre-commit` + `scripts/pre_commit_ci_guard.py` — HEAD freshness guard
- `PSScriptAnalyzerSettings.psd1`
- `AGENTS.md`, `docs/ci.md` (this file)

## Notes

- Line endings: **LF** (matches upstream Rutherford `.gitattributes`), not CRLF.
- Coverage floors (90% aggregate / 80% per-file) must not be weakened for a green AE2 run.

## Relation to `just`

Upstream `justfile` (`just check`, `just test`, …) remains a valid manual shortcut. The AE2 runner is the canonical gate for agents and pre-commit freshness.
