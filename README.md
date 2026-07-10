# context-governor

**Keep Claude Code sessions under a context budget — and make the handoff to the next session automatic.**

Long agentic sessions degrade: past ~60% of the context window, models drift, forget constraints, and redo work. Claude Code's built-in auto-compact is a seatbelt, not a plan — it fires near ~90–95% (after quality has already degraded), summarizes lossily in-session, and leaves no external record. `context-governor` is a tiny hook-based engine that acts *before* the wall:

1. **Measures** real context usage after every tool call — exact token counts from the transcript, against the **actual model's window** (1M for Fable 5 / Opus 4.6+ / Sonnet 4.6+, 200K for Haiku and older; `[1m]` beta suffix detected).
2. **Warns** the agent at a soft threshold (default 50%): *finish the slice in flight, start nothing new.*
3. **Forces a handoff** at a hard threshold (default 60%): the agent must append a structured entry to an append-only **handoff ledger** and end the session — while a detached **compactor** snapshots the session state in the background.
4. **Bootstraps** the next session automatically: a `SessionStart` hook injects the last ledger entry, so the fresh session starts from "Next step", not from zero.
5. **Chains sessions** for long engagements: `governor.py engage` relaunches a fresh session after each handoff (you stay in the loop), and `governor.py run` does it fully unattended with headless sessions until the plan says DONE.

One file, Python 3.9+ stdlib only, no dependencies. Auditable in five minutes. Cursor is also supported (see [Cursor support](#cursor-support)).

## Install

**Option A — Claude Code plugin (recommended):**

```
/plugin marketplace add vsrox-cliqq/context-governor
/plugin install context-governor@context-governor
```

**Option B — shell one-liner:**

```bash
curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
```

**Option C — manual:**

```bash
git clone https://github.com/vsrox-cliqq/context-governor
cd context-governor
python3 install.py --claude
```

Options B and C **merge** into `~/.claude/settings.json` and write a timestamped backup first — existing hooks are never clobbered. Restart Claude Code afterwards.

## Model-aware budgeting

The governor reads the model ID from the last assistant message in the transcript and resolves the context window automatically:

| Model | Window |
|---|---|
| Claude Fable 5 / Mythos 5 | 1,000,000 |
| Claude Opus 4.6 / 4.7 / 4.8 | 1,000,000 |
| Claude Sonnet 4.6 / Sonnet 5 | 1,000,000 |
| Any model with a `[1m]` suffix (1M beta) | 1,000,000 |
| Claude Haiku 4.5, older Opus/Sonnet | 200,000 |

Unknown models fall back to `tools.claude_code.window_tokens` (default 200,000). To pin or correct a model, add it to `model_windows` in your config — any substring of the model ID works as the key:

```json
{ "model_windows": { "claude-sonnet-4-6": 200000 } }
```

Every warn/handoff message and `status` row shows the resolved model and window, so a wrong assumption is immediately visible.

## What the agent experiences

- **Below 50%**: nothing. The hook is silent (`{}`) and adds ~10ms per tool call.
- **At 50%**: a one-time system reminder — wrap up the current slice, start nothing new.
- **At 60%**: a hard order — park the atomic step in progress, append a ledger entry to `handoff/LEDGER.md`, tell the user to start a fresh session. Meanwhile a detached compactor writes `handoff/state-<session>.md`. If the agent keeps going, the order re-fires every `refire_delta_pct` points.
- **Next session**: the `SessionStart` hook injects the last ledger entry. The agent starts from "Next step", not from zero.

## Long engagements: `governor.py engage`

For a long implementation you'd otherwise babysit across many chats:

```bash
python3 governor.py engage            # in your project directory
```

`engage` launches an interactive `claude` session in your terminal. When the session ends *and a new handoff entry was appended* (the budget fired and the agent wrote its handoff), it launches a fresh session — which the `SessionStart` hook bootstraps from that entry. You keep working with the agent normally; the between-session re-orientation disappears.

- Stops when a session ends **without** a handoff (natural finish or you quit).
- Stops when the ledger's "Next step" says `DONE`.
- Asks before each relaunch; pass `--auto` to chain without asking, `--max-sessions N` to cap (default 8).

## Fully autonomous mode: `governor.py run`

`run` removes the human entirely: it loops **headless** agent sessions — each scoped to one plan slice and required to end with a ledger entry — until the ledger says `DONE`:

```bash
python3 governor.py run --workspace /path/to/repo \
  --task "Implement the remaining stages of build-plan.md"
```

Each iteration builds a prompt from the latest ledger entry (or `--task` on the first run), executes the agent CLI (`claude -p` or `cursor-agent`, autodetected; override with `--agent-cmd 'your-cli {prompt}'`), and checks the ledger afterwards. Safety rails:

- **No-progress stop**: if a session ends without appending a ledger entry, the loop stops immediately instead of burning sessions.
- **`max_sessions`** cap (default 8) and per-session `session_timeout` (default 1h).
- Full agent output logged to `~/.context-governor/state/run-<ts>.log`.

Headless agents typically need permission flags to edit files unattended (e.g. `--agent-cmd 'claude -p {prompt} --permission-mode acceptEdits'`) — grant only what you're comfortable with; the driver intentionally doesn't default to skipping permissions.

## Commands

```bash
python3 governor.py status      # context usage of recent sessions (model + window shown)
python3 governor.py engage      # chain interactive sessions through the ledger
python3 governor.py run --task "..."    # autonomous multi-session execution
python3 governor.py compact --transcript <path.jsonl> --out state.md   # manual snapshot
python3 tests/test_governor.py  # run the test suite (27 tests, stdlib only)
```

## Configure

Optional. Copy `config.example.json` to `~/.context-governor/config.json` (user-level) or `<workspace>/.context-governor.json` (per project — overrides user config):

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

- `model_windows` — per-model window overrides (see [Model-aware budgeting](#model-aware-budgeting)). You should rarely need this; detection is automatic.
- `summarizer_cmd` — optional Hermes-style LLM compaction. Any command that reads a prompt on stdin and prints a summary works, e.g. `claude -p --model haiku` (headless Claude Code) or `ollama run llama3.2`. Leave empty for the no-LLM structural digest (head task + files touched + commands run + protected tail), which is fast, free, and offline.
- `refire_delta_pct` — if the agent ignores the handoff order, it re-fires every N percentage points.

## Relationship to auto-compact and `/compact`

Claude Code's auto-compact fires only near the top of the window (~90–95%), is a lossy in-session summary you can't inspect, and doesn't help the *next* session. `/compact` is the same mechanism triggered manually — which puts the "when should we wrap up?" judgment back on you. The governor makes that judgment mechanical and early (50/60%), and produces a durable, git-committable artifact instead of an invisible summary. On 1M-window models the two coexist fine: the governor hands off at 60%, so auto-compact simply never fires.

## Design lineage

The engine borrows the `ContextEngine` ideas from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — threshold-driven monitoring, head/tail protection, and compaction performed by a process *outside* the agent's inference loop.

One key adaptation: Hermes owns its inference loop, so it can rewrite the live context in place and the agent never notices. Claude Code doesn't let external code mutate the live conversation — so compaction here happens **across sessions** instead of within one:

```
Hermes:            [head | ~~summarized middle~~ | tail]  -> same session continues
context-governor:  session A hits 60% -> snapshot (head + digest + tail) + ledger entry
                   session B starts fresh -> bootstrapped from ledger + snapshot
```

The ledger doubles as a write-ahead log (the `tunc-clm` idea) and its required entry format — exact next step, verification status, open risks — acts as a lightweight handoff protocol (the `Agent_Handoff` idea).

## How measurement works

| Tool | Method | Accuracy |
|---|---|---|
| Claude Code | `input_tokens + cache_read + cache_creation` from the last assistant message's `usage`, window resolved from that message's `model` | Exact |
| Cursor | transcript file size ÷ `bytes_per_token` (default 4) | Proxy — errs conservative |

## Cursor support

Cursor works through the same hook with a byte-proxy measurement (Cursor doesn't expose token counts) and a project rule instead of a `SessionStart` hook:

```bash
python3 install.py --cursor-project /path/to/your/repo   # per project (recommended)
python3 install.py --cursor                              # user-level ~/.cursor/hooks.json
```

Cursor performs its own context summarization, so the live context may be smaller than the byte estimate — meaning the governor hands off slightly *early*, which is the safe direction. Tune `tools.cursor.bytes_per_token` upward if it triggers too soon.

## Repository layout

```
governor.py             # the whole engine: hook / compact / status / engage / run
install.py              # merging installer (settings.json / hooks.json)
install.sh              # curl-able one-line installer
.claude-plugin/         # Claude Code plugin + marketplace manifests
hooks/hooks.json        # plugin hook wiring (PostToolUse + SessionStart)
config.example.json
templates/
  session-handoff.mdc   # Cursor bootstrap rule (installed per project)
tests/
  test_governor.py      # 27 tests, no dependencies
```

## Limitations

- The forced handoff is delivered as injected context, not a hard kill — a sufficiently confused model could ignore it, which is why it re-fires.
- Subagent transcripts aren't counted toward the parent session's budget.
- Cursor measurement is a proxy (see above); Claude Code is exact.
- The built-in model-window table is static; new models fall back to `window_tokens` until you add a `model_windows` override (or update).

## License

MIT
