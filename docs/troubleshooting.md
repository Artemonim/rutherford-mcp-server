# Troubleshooting

## First step for any failure

Run `doctor` before investigating further. It drives each agent with a real read-only ACP round trip
— the only trustworthy health signal — and reports `ok`, `no_answer`, `model_unavailable`,
`handshake_failed`, `not_installed`, or `error` per agent. `capabilities` is the cheaper read (the
static registry: launch command, `default_model`, `model_selection`, `effort_capable`; no spawn).
For live advertised model ids use `doctor(agent=<id>, connect_only=true)`.

---

## Symptoms

### `not_installed` in doctor / `ACP_SPAWN_FAILED`

The agent's launch command was not found on PATH, or the executable failed to start. `ACPSession.open`
spawns the agent as an ACP server; a missing binary, a `working_dir` that resolves to a file, or an
unexecutable command all surface as `ACP_SPAWN_FAILED`, which `doctor` classifies as `not_installed`.

- Install the agent (or its ACP shim — `codex` needs `codex-acp`, `claude_code` needs
  `claude-agent-acp`, `pi` needs `pi-acp`, all `npm i -g`).
- **The CLI is installed but the adapter shim is not.** `codex` / `claude_code` / `pi` launch a *separate*
  npm adapter that fronts the underlying CLI (`codex` / `claude` / `pi`). If you have the CLI but the shim
  is missing, `doctor` does not just say `not_installed` — it adds an `install_hint` with the exact
  `npm i -g <package>`. Run that, or let Rutherford do it: **`setup install_adapters=true`** detects every
  such gap (CLI present, shim absent) and runs the install for you. `setup` (no flag) lists them under
  `adapters.installable` without installing.
- If it is installed but not on the server's PATH, add its directory to PATH before starting the
  Rutherford process.
- On Windows, confirm the resolved command is a real `.exe` or a recognized npm shim. `prepare_argv`
  resolves npm shims to their real target; a non-npm `.bat` falls back to PowerShell, then `cmd /c`.

### `handshake_failed` in doctor / `ACP_HANDSHAKE_FAILED`

The agent spawned but the `initialize` / `new_session` handshake failed — a protocol mismatch, an auth
failure surfacing at session creation, or a slow setup that blew the handshake budget.

- Confirm the agent actually supports ACP and the launch command is the ACP entry point (for example,
  `cursor-agent acp`, not `cursor-agent`; `kiro-cli acp`, not the `kiro` IDE launcher).
- A heavyweight agent that sets up a workspace on `new_session` (OpenHands) may need a larger
  `handshake_timeout_s`. Set `[agents.<id>] handshake_timeout_s = 90` in config.
- Some agents drive over ACP only with their own service auth. `cline`, for instance, returns an empty
  handshake/turn when configured for a ChatGPT subscription or OpenRouter in the desktop app — its
  headless `--acp` path needs Cline's own service auth.

### `no_answer` in doctor / `ACP_REFUSED` / `ACP_EMPTY_ANSWER`

The agent spawned and handshook but ended the turn without an answer — it refused, or produced no
text. Often an auth or model-availability problem that only shows once the agent tries to call its
model.

- Sign in to the agent with its own login, or set its API key, then re-run `doctor`.
- For a local-model agent, confirm the model supports tool-calling — a model without it handshakes but
  fails the agentic turn. See [local-models.md](local-models.md).

### `model_unavailable` in doctor — `claude_code` 400 invalid model on Bedrock / enterprise wrappers

The seat spawned and handshook (it shows `reachable` under `doctor connect_only`), but the turn failed
because the provider rejected the model id:

```
API Error (claude-opus-4-8): 400 The provided model identifier is invalid.
```

This is a Claude Code configured for **AWS Bedrock** / **Google Vertex**, or a **managed enterprise
wrapper**, where the third-party `claude-agent-acp` adapter resolves the model down
to a bare cloud alias (`claude-opus-4-8`) that the provider rejects — it needs an inference-profile id
like `us.anthropic.claude-opus-4-1-20250805-v1:0`. The standalone `claude` CLI works because it resolves
the Bedrock model itself; the SDK/adapter path that Rutherford drives does not. `doctor` attaches a
`remediation_hint` for this case.

The fix is a per-agent `[agents.claude_code.env]` block in Rutherford's own config — it lives outside the
`.claude` tree, so an enterprise wrapper that rewrites `settings.json` cannot revert it, and
`ANTHROPIC_CUSTOM_MODEL_OPTION` survives an enforced model allowlist:

```toml
[agents.claude_code]
default_model = "global.anthropic.claude-opus-4-8[1m]"

[agents.claude_code.env]
ANTHROPIC_MODEL = "global.anthropic.claude-opus-4-8[1m]"
ANTHROPIC_CUSTOM_MODEL_OPTION = "global.anthropic.claude-opus-4-8[1m]"
```

Reconnect the MCP server (config is read once at start) and re-run `doctor agent=claude_code`. See
**[Claude Code on Bedrock / enterprise wrappers](bedrock.md)** for the full mechanism and the approaches
that do *not* work.

### Sync call looks hung: no MCP progress for a long time

`mode="sync"` (the default on `delegate` / `consensus` / `debate`) intentionally waits for the final
result envelope and returns it on the same MCP request. For a single-agent `delegate`, there is no
incremental MCP progress stream while the turn runs: the caller stays blocked until success, a
structured failure, or the relevant deadline. A quiet tool call is therefore **not** evidence of
failure, and it must **not** receive an arbitrary cancellation deadline shorter than the timeouts you
passed (or the configured defaults).

Two budgets apply in sequence; they are not interchangeable:

| Phase | What it covers | Deadline | Failure code |
| --- | --- | --- | --- |
| Pre-prompt startup | sandbox prep, ACP spawn, initialize, session create/load, model/effort selection | `pre_prompt_timeout_s` / `default_pre_prompt_timeout_s` (default 90s) | `ACP_PRE_PROMPT_TIMEOUT` |
| Running prompt | the accepted `session/prompt` turn only | `timeout_s` / `default_timeout_s` (default 300s) | `ACP_TURN_TIMEOUT` |

After the prompt is accepted, silence up to `timeout_s` is expected: the agent may be reasoning, calling
tools over ACP, or waiting on its own model provider. Rutherford does not scrape agent stdout for a
"still alive" signal, and the MCP client UI is not required to show host-specific UI/IDE activity for a
headless ACP child — none of that is a protocol guarantee.

Raising `timeout_s` (for example `timeout_s=1200`) does **not** extend sandbox or other pre-prompt
stages. Those stay under `pre_prompt_timeout_s` / `default_pre_prompt_timeout_s`. For an
`ACP_PRE_PROMPT_TIMEOUT` with `stage` `sandbox` (common on Cursor), set
`[agents.cursor] pre_prompt_timeout_s = 300` or pass per-call `pre_prompt_timeout_s`.

**What to do**

- During startup, leave a sync call alone until the pre-prompt deadline expires. Only after the prompt
  is accepted does the running-prompt `timeout_s` apply — do not wait on `timeout_s` while the turn
  is still in pre-prompt. Abort earlier only with independent evidence the process is wedged (for
  example the Rutherford server process itself is gone).
- If you need non-blocking visibility or cancellation **before** you start, choose `mode="async"`.
  The submit call returns `{job_id, status, tool}` immediately; then use `list_jobs` / `activity` /
  `job_status` / `job_result` / `cancel_job`. See [recipes.md](recipes.md#kick-off-a-long-job-and-keep-working).
- For local operator diagnostics, watch structured JSON logs on the Rutherford process **stderr**
  (`log_level` / `log_format` / `acp_prompt_heartbeat_s` in [configuration.md](configuration.md)).
  Each ACP turn emits `event=acp_lifecycle` records with a `stage` (`queue`, `sandbox`, `spawn`,
  `initialize`, `session`, `model_selection`, `effort_selection`, `prompt`, `finish`, `cancelled`) and
  `phase` (`enter` / `exit` / `heartbeat`). While a prompt is in flight, heartbeats repeat every
  `acp_prompt_heartbeat_s` (default 30s; `0` disables) and only mean Rutherford is still awaiting the
  prompt outcome — not that the model provider is live. Those lines are for the process operator only —
  they are not MCP progress notifications and do not wake a sync caller.
- A sync `consensus` / `debate` may emit best-effort MCP progress when the client supplied a
  `progressToken` (panel voice completion). A sync `delegate` does not; absence of either is still
  not a hang signal by itself.

If the call eventually fails, use the code-specific sections below (`ACP_PRE_PROMPT_TIMEOUT`,
`ACP_TURN_TIMEOUT`, spawn/handshake failures) rather than inventing a client-side abort policy.

### `codex` with both `model` and `effort` — bare vs bracketed model ids

Codex has carried its reasoning effort two different ways, and which one a seat gets decides both the
error you see when it goes wrong and the tier you actually get.

Historically `codex-acp` advertised a catalog of `base[tier]` ids (`gpt-5.5[xhigh]`), so one selection
set the model and the tier together. Newer Codex advertises **bare** ids plus a separate
`reasoning_effort` config option. Rutherford therefore uses the bracket id **only when the agent
advertises it**, and otherwise selects the advertised bare model and applies `reasoning_effort`,
confirming the option's `current_value` before reporting the tier.

What that means when reading a failure:

- A bare model the agent does offer is never reported as `MODEL_UNAVAILABLE`. That code now means the
  base id genuinely is not advertised on any channel.
- If the fallback selects the bare model but the agent advertises no `reasoning_effort` option, or the
  set is not confirmed, the turn fails `ACP_HANDSHAKE_FAILED` naming the **effort**, not the model. The
  effort was requested and could not be proven applied, so it is refused rather than silently dropped.
- A matching bare model id is never taken as evidence the bracket tier applied.

The two channels also have different ceilings. A `base[tier]` id cannot encode `max`, so the rewrite
clamps to `xhigh`; the `reasoning_effort` option is clamped to what the agent advertises, and current
Codex lists `max` there. `effort_applied` always reports the tier that actually landed.

### `ACP_TURN_TIMEOUT` — the turn exceeded its limit

`ACPSession.prompt` wraps the turn in a timeout; on expiry it issues `session/cancel`, preserves any
streamed partial answer on `result.partial`, and fails as `ACP_TURN_TIMEOUT`. The session's descendant
process tree is reaped on close.

- Raise the per-call `timeout_s` on the `delegate` / `consensus` / `debate` call.
- Raise `default_timeout_s` in config (default 300s), or set a per-agent `[agents.<id>] timeout_s` for
  one slow agent.
- For long tasks, use `mode="async"`: the call returns a job id immediately and you poll with
  `job_status` / `job_result`. The timeout still applies to the underlying turn.
- A local model on CPU/iGPU is slow, and the first call is slowest (cold weight-load). Pre-load the
  model and give the agent a generous timeout. See [local-models.md](local-models.md).

### `ACP_PRE_PROMPT_TIMEOUT` — not prompt-ready in time

The hard pre-prompt deadline (`default_pre_prompt_timeout_s`, default 90s) covers sandbox prep, ACP
spawn, initialize, session create/load, and model/effort selection — it ends before `session/prompt`.
Expiry is `ACP_PRE_PROMPT_TIMEOUT`: re-execution-SAFE, no partial answer, unhealthy for cooldown.
Semaphore queue wait does not consume this budget. Error `details` carry `stage`, `budget_s`, and
`elapsed_s`.

- Raise the per-call `pre_prompt_timeout_s`, or `[agents.<id>] pre_prompt_timeout_s` /
  `default_pre_prompt_timeout_s` in config.
- Check whether sandbox prep (large non-git copy) or a slow cold agent start is the stage in `details`.
- Cursor recipe: keep the global default at 90s and set `[agents.cursor] pre_prompt_timeout_s = 300`
  (or pass per-call `pre_prompt_timeout_s=300`).

### `hermes` is slow or times out intermittently

`hermes` is registered and drives over ACP, but the Nous endpoint's latency swings widely. It is
deliberately kept out of the bounded integration test. Check it live with `doctor`, and give it a
longer `timeout_s` if you use it in a panel.

### `WORKSPACE_NOT_TRUSTED` — write or yolo refused

A `write` or `yolo` delegation is mutating. Before spawning, `DelegationService._workspace_trusted`
checks whether `working_dir` is under a configured `trusted_workspaces` path or whether the call passed
`trust_workspace=true`. If neither holds, the delegation fails with `WORKSPACE_NOT_TRUSTED` and no
agent is spawned. A delegation that omits `working_dir` also fails.

- Pass `trust_workspace=true` on the call (per-call opt-in), or
- Add the directory to `trusted_workspaces` in config:

```toml
trusted_workspaces = ["/home/user/projects/myapp", "C:\\Users\\user\\projects\\myapp"]
```

From the repo root, the one-shot CLI registers cwd in the **global** allowlist:

```sh
rutherford-mcp-server trust           # or: python -m rutherford trust [/path]
rutherford-mcp-server untrust         # remove cwd (or a path) from the global allowlist
rutherford-mcp-server trust --list
```

Or set `RUTHERFORD_TRUSTED_WORKSPACES` (paths separated by `;` on Windows, `:` on POSIX).

Rutherford reads config once at server start, so restart or reconnect the MCP server before retrying --
or pass `trust_workspace=true` on the call, which takes effect immediately. This applies to the env var
too: a running server will not pick up either without a restart.

### `UNKNOWN_TARGET` — agent id not recognized

The `cli` (or a `targets` entry's `cli`) does not match any registered agent id. The registry is a
closed mapping; an unknown id fails with `UNKNOWN_TARGET` and lists the known ids.

- Run `capabilities` to see every registered agent id. The built-in ids are `goose`, `opencode`,
  `vibe`, `cline`, `junie`, `kimi`, `openhands`, `codex`, `claude_code`, `copilot`, `qwen`, `droid`,
  `cursor`, `kiro`, `pi`, `hermes`, `gemini`, `qoder`, `grok`, plus any you added in config and any
  auto-detected local model (`ollama-<model>` / `lmstudio-<model>`). The id is case-sensitive.

### `TOO_MANY_TARGETS` — panel fan-out exceeds the cap

A `consensus` / `debate` call lists more targets than `max_targets` (default 8). The call is refused
before any agent is spawned.

- Reduce the targets, or raise `max_targets` in config (or `RUTHERFORD_MAX_TARGETS`).

### `UNKNOWN_ROLE` — named role does not exist

The `role` argument named a persona the `RoleStore` does not know. Five built-ins always load
(`principal-reviewer`, `architect`, `debugger`, `security-reviewer`, `explainer`); a custom role needs
a `role_dirs` entry pointing at the directory with the `.md` file.

- Run `list_roles` to see what is loaded. Add your directory to `role_dirs`:

```toml
role_dirs = ["/home/user/.rutherford/roles"]
```

### Cursor: `confirmed: false` with the correct model is normal

For Cursor, the effective model rides the process launch argv (`cursor-agent acp --model <id>` via
`AgentDescriptor.model_launch_flag`). ACP does **not** attest the runtime model after that launch, so a
successful Cursor turn correctly reports:

- `provenance.confirmed: false`
- `provenance.routing_channel: launch_argv` (when present)
- `provenance.model_confirmation: intent_only` (when present)

`provenance.model` is still the effective model Rutherford intended for the turn (lineage /
correlation). Do **not** “fix” this by calling `session/set_model` or `set_config_option` — those can
echo `currentValue` without changing Cursor inference and may mutate a persistent global default.

If the wrong *family* of model actually ran, that is a routing/entitlement issue; treat
`confirmed: false` alone as expected for launch-argv agents, not as a failure signal. See also
[ACP_PRE_PROMPT_TIMEOUT](#acp_pre_prompt_timeout--not-prompt-ready-in-time) for Cursor sandbox budgets
(`pre_prompt_timeout_s = 300`).

### Cursor: `session/load` without a prior prompt

Cursor’s persisted ACP store under `~/.cursor/acp-sessions/<session_id>/` appears after the **first
prompt** turn. `meta.json` alone is not enough for a successful `session/load`. Loading a
never-prompted or unknown `session_id` typically yields Session not found / `RESUME_FAILED`. Resume
after a completed prompt (same `session_id`) is the supported path — see
[integration testing](integration-testing.md#cursor-opt-in-modules).

### `JOB_NOT_FOUND` — polling a job that no longer exists

Background jobs live in memory with a TTL set by `job_ttl_s` (default 3600s). A finished job is evicted
after the TTL on the next access, and a server restart clears all jobs.

- Collect the result promptly once `job_status` reports `succeeded` or `failed`.
- Confirm the job id is passed back correctly — it is a 12-char hex string from the submit envelope.

### `TOO_MANY_JOBS` — background-job cap reached

The store is full and every retained job is still running, so there is nothing safe to evict. The new
submission is refused with `TOO_MANY_JOBS`.

- Let some jobs finish, or `cancel_job` ones you no longer need, or raise `max_jobs` in config.

### Server exits immediately / `ConfigError` on startup

`ConfigError` is fatal and surfaces before the server serves. It is raised when a TOML config cannot be
parsed or when the merged config fails validation. The message lists the failures:

```
invalid configuration:
  - max_depth: Input should be a valid integer
  - agents.my-agent: agent 'my-agent' is not a built-in agent and has no 'command' or 'base' ...
```

Read the field path and fix the corresponding key. A malformed `acp.json` is *not* fatal — it is
logged and skipped. A non-UTF-8 config file (the UTF-16 that some Windows redirection writes) is
reported as a `ConfigError`, not a raw decode error.

---

## Quick reference

| Code | Immediate diagnostic |
| --- | --- |
| `ACP_SPAWN_FAILED` | Run `doctor`; install the agent (and its ACP shim); confirm it is on PATH. |
| `ACP_HANDSHAKE_FAILED` | Confirm the ACP launch command; raise `handshake_timeout_s`; check the agent's auth. On Codex with `model` + `effort`, this can mean the bare model was advertised but `reasoning_effort` was not confirmed (Codex ACP 1.8); it is not `MODEL_UNAVAILABLE`. |
| `ACP_PRE_PROMPT_TIMEOUT` | Raise `pre_prompt_timeout_s` / `default_pre_prompt_timeout_s`; check sandbox prep or a slow cold start. |
| `ACP_REFUSED` / `ACP_EMPTY_ANSWER` | The agent answered nothing; check auth, or a local model's tool-calling support. |
| `model_unavailable` (doctor) | The provider rejected the model id; on Bedrock/Vertex/an enterprise wrapper pin one via `[agents.claude_code.env]` — see [bedrock.md](bedrock.md). |
| `ACP_TURN_TIMEOUT` | Raise `timeout_s` or `default_timeout_s`; use `mode="async"` for long tasks. |
| Quiet sync / no MCP progress | Expected for `mode="sync"` through pre-prompt (`pre_prompt_timeout_s`) then the running prompt (`timeout_s`); do not invent a shorter cancel. Choose `mode="async"` before start for visibility and cancellation. |
| `ACP_TURN_ERROR` | A transport/protocol error mid-turn; re-run, and check `doctor`. |
| `WORKSPACE_NOT_TRUSTED` | Pass `trust_workspace=true` or add the path to `trusted_workspaces`. |
| `UNKNOWN_TARGET` | Run `capabilities` to list registered agent ids; the id is case-sensitive. |
| `TOO_MANY_TARGETS` | Reduce targets or raise `max_targets` (default 8). |
| `UNKNOWN_ROLE` | Run `list_roles`; add your directory to `role_dirs`. |
| `JOB_NOT_FOUND` | Collect results before TTL (`job_ttl_s`, default 1h); jobs clear on restart. |
| `TOO_MANY_JOBS` | Let jobs finish, `cancel_job`, or raise `max_jobs`. |
| `READONLY_VIOLATED` | A `read_only` / `propose` run changed the git tree; check the run, or disable `verify_read_only`. |
