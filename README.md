<div align="center">

<img src="assets/governor.png" alt="The Governor — context-governor mascot" width="300"/>

# context-governor

**Stop Claude Code before quality degrades — not after.**

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-58a6ff?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-27%20passing-3fb950?style=flat-square)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-8b949e?style=flat-square)](LICENSE)
[![stdlib only](https://img.shields.io/badge/deps-stdlib%20only-d29922?style=flat-square)](#)

</div>

---

For a long implementation, instead of opening `claude` and babysitting it across many chats, run this from your project directory:

```bash
governor engage
```

You work exactly as normal — same Claude Code session, same commands. When context approaches the danger zone, the governor fires a clean handoff: the agent appends a structured entry to a local ledger and ends the session. `engage` notices, relaunches a fresh session, and the `SessionStart` hook bootstraps it from that entry. The new session starts from "Next step", not from zero. The between-session re-orientation ritual disappears.

Long agentic sessions degrade: past ~60% of the context window, models drift, forget constraints, and redo work. Claude Code's built-in auto-compact is a seatbelt, not a plan — it fires near ~90–95% (after quality has already degraded), summarizes lossily in-session, and leaves no external record.

`context-governor` is the hook-based engine that makes this work:

1. **Measures** real context usage after every tool call — exact token counts from the transcript, against the **actual model's window** (1M for Fable 5 / Opus 4.6+ / Sonnet 4.6+; detected automatically).
2. **Warns** the agent at 50%: *finish the slice in flight, start nothing new.*
3. **Forces a handoff** at 60%: the agent appends a structured entry to an append-only **handoff ledger** and ends the session — while a detached compactor snapshots state in the background.
4. **Bootstraps** the next session automatically via a `SessionStart` hook — the fresh session starts from "Next step", not from zero.
5. **`engage`** chains the above into a continuous loop: relaunch after each handoff (staying in the loop), or `run` for fully unattended execution until the ledger says DONE.

One file · Python 3.9+ · stdlib only · auditable in five minutes.

---

## Why not just use auto-compact?

| | auto-compact | `/compact` | **context-governor** |
|---|:---:|:---:|:---:|
| Fires at | ~90–95% | user-triggered | **50% warn / 60% stop** |
| Output | in-session summary | in-session summary | **git-committable ledger** |
| Inspectable | ✗ | ✗ | **✓** |
| Next session bootstrapped | ✗ | ✗ | **✓** |
| Human judgment needed | ✗ | **✓** | ✗ |

On 1M-window models (Opus 4.6+, Sonnet 4.6, Fable 5) auto-compact effectively never fires before the 60% handoff — so the two coexist cleanly, and the governor is the sole handoff mechanism.

---

## Install

One install gives you everything — the measuring hooks **and** the `governor` command (`engage`, `run`, `status`). Every path below ends the same way: two hooks merged into `~/.claude/settings.json` (timestamped backup written first, existing hooks never clobbered) plus a `governor` launcher in `~/.local/bin`. Then restart Claude Code — hooks load at session start.

### Easiest: let Claude Code install it

Paste this into any Claude Code session:

```
Install context-governor: fetch https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/AGENT_INSTALL.md and follow it.
```

Claude checks prerequisites, runs the installer, verifies the hooks landed, and tells you when to restart. ([AGENT_INSTALL.md](AGENT_INSTALL.md) is short — read it first if you want to know exactly what it does.)

### One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
```

Installs to `~/.context-governor/app` (re-running updates it in place).

### As a Claude Code plugin

```
/plugin marketplace add vsrox-cliqq/context-governor
/plugin install context-governor@context-governor
```

> **Plugin caveats:** the plugin wires the hooks only — it doesn't add the `governor` command, so for `engage`/`run`/`status` use one of the installer paths instead. And in the desktop app, plugin hooks only fire in **Cowork** sessions, not the regular Code tab; desktop users should also prefer the installer paths (the desktop app shares `~/.claude/settings.json` with the CLI, so one install covers both — **quit and relaunch** afterwards; hooks don't hot-reload).

### Manual

```bash
git clone https://github.com/vsrox-cliqq/context-governor
cd context-governor
python3 install.py --claude
```

### Verify

After restarting Claude Code and working for a bit:

```bash
governor status
```

If recent sessions appear with token counts and model names, the hooks are firing. (If `governor` isn't found, `~/.local/bin` isn't on your PATH — the installer prints the one-liner to fix that; or call `python3 ~/.context-governor/app/governor.py status` directly.)

---

## Model-aware budgeting

The governor reads the model ID from the transcript and resolves the context window automatically — no config needed for common models:

| Model | Window |
|---|---|
| Claude Fable 5 / Mythos 5 | 1,000,000 |
| Claude Opus 4.6 / 4.7 / 4.8 | 1,000,000 |
| Claude Sonnet 4.6 / Sonnet 5 | 1,000,000 |
| Any model with `[1m]` suffix (1M beta) | 1,000,000 |
| Claude Haiku 4.5, older Opus / Sonnet | 200,000 |

Unknown models fall back to `tools.claude_code.window_tokens` (default 200,000). To override any model, add it to `model_windows` in your config — any substring of the model ID works as the key:

```json
{ "model_windows": { "claude-sonnet-4-6": 200000 } }
```

Every warn/handoff message and `status` row shows the resolved model and window, so a wrong assumption is immediately visible.

---

## What happens inside a session

```
[ 0%  → 50% ]  silent — the hook adds ~10ms per tool call, nothing else

[ at 50% ]     ⚠  CONTEXT BUDGET WARNING: finish slice in flight,
                   start nothing new, handoff triggers at 60%

[ at 60% ]     🛑 CONTEXT BUDGET EXCEEDED: park current step, append
                   handoff entry to handoff/LEDGER.md, end the session.
                   Re-fires every 3% if ignored.

[ next session] SessionStart hook injects the last ledger entry → agent
                starts from "Next step", not from zero.
                With `engage`: this relaunch is automatic.
```

---

## `engage` options

```bash
governor engage            # asks before each relaunch
governor engage --auto     # relaunches without asking
governor engage --max-sessions 12 --claude-cmd "claude --profile work"
```

Stops automatically when a session ends without a handoff (natural finish or you quit), or when the ledger's "Next step" says `DONE`. No installation beyond the standard install — `engage` requires only the `claude` CLI and runs in any terminal.

## Fully autonomous mode: `run`

```bash
governor run --workspace /path/to/repo \
  --task "Implement the remaining stages of build-plan.md"
```

Loops headless `claude -p` sessions — each scoped to one plan slice — until the ledger says `DONE`. Safety rails: no-progress stop, `max_sessions` cap (default 8), per-session `session_timeout` (default 1h), full log at `~/.context-governor/state/run-<ts>.log`.

Headless agents need permission flags: `--agent-cmd 'claude -p {prompt} --permission-mode acceptEdits'` — grant only what you're comfortable with.

---

## Commands

```bash
governor status         # context usage of recent sessions (model + window)
governor engage         # chain interactive sessions through the ledger
governor run --task ""  # autonomous multi-session execution
governor compact --transcript <path.jsonl> --out state.md
python3 tests/test_governor.py     # 27 tests, stdlib only
```

(`governor` is the launcher the installer drops in `~/.local/bin`; from a clone, `python3 governor.py <cmd>` is identical.)

---

## Configure

Optional. Copy `config.example.json` to `~/.context-governor/config.json` (user-level) or `<workspace>/.context-governor.json` (per-project, overrides user config):

```json
{
  "warn_pct": 50,
  "handoff_pct": 60,
  "ledger_path": "handoff/LEDGER.md",
  "auto_compact": true,
  "summarizer_cmd": "",
  "model_windows": {}
}
```

- `model_windows` — per-model window overrides (see [Model-aware budgeting](#model-aware-budgeting)). Rarely needed; detection is automatic.
- `summarizer_cmd` — optional LLM compaction. Any command that reads a prompt on stdin and prints a summary: `claude -p --model haiku` or `ollama run llama3.2`. Leave empty for the no-LLM structural digest (fast, free, offline).
- `refire_delta_pct` — re-fire interval if the agent ignores the handoff order (default 3%).

---

## Repository layout

```
governor.py             # the whole engine: hook / compact / status / engage / run
install.py              # merging installer (settings.json merge)
install.sh              # curl-able one-line installer
AGENT_INSTALL.md        # step-by-step guide an AI agent follows to install this
.claude-plugin/         # Claude Code plugin + marketplace manifests
hooks/hooks.json        # plugin hook wiring (PostToolUse + SessionStart)
config.example.json
assets/
  governor.svg          # the Context Cop (animated mascot)
tests/
  test_governor.py      # 27 tests, no dependencies
```

---

## Design lineage

The engine borrows from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — threshold-driven monitoring, head/tail protection, and compaction performed outside the agent's inference loop.

Key adaptation: Hermes owns its inference loop and can rewrite live context in place. Claude Code doesn't expose that — so compaction happens **across sessions**:

```
Hermes:            [head | ~~summarized middle~~ | tail]  → same session continues
context-governor:  session A hits 60% → snapshot + ledger entry
                   session B starts fresh → bootstrapped from ledger
```

The ledger doubles as a write-ahead log and its required entry format — exact next step, verification status, open risks — is a lightweight handoff protocol.

---

<details>
<summary><strong>🧪 Cursor support (experimental)</strong></summary>

Cursor support is experimental. Measurement uses a byte-proxy (Cursor doesn't expose token counts directly), and session bootstrapping uses a project rule rather than a `SessionStart` hook.

```bash
python3 install.py --cursor-project /path/to/your/repo   # per project (recommended)
python3 install.py --cursor                              # user-level ~/.cursor/hooks.json
```

Cursor performs its own context summarization, so the live context may be smaller than the byte estimate — the governor hands off slightly *early*, which is the safe direction. Tune `tools.cursor.bytes_per_token` upward if it fires too soon.

</details>

---

## Limitations

- The forced handoff is injected context, not a hard kill — a confused model could ignore it (it re-fires to compensate).
- Subagent transcripts aren't counted toward the parent session's budget.
- The built-in model-window table is static; new models fall back to `window_tokens` until you add a `model_windows` override.

## License

MIT
