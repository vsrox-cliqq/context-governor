# context-governor

**Keep AI coding sessions under a context budget — and make the handoff to the next session automatic.**

Long agentic sessions degrade: past ~60% of the context window, models drift, forget constraints, and redo work. `context-governor` is a tiny hook-based engine for **Cursor** and **Claude Code** that:

1. **Measures** context usage after every tool call (real token counts for Claude Code, a calibrated byte proxy for Cursor).
2. **Warns** the agent at a soft threshold (default 50%): *finish the slice in flight, start nothing new.*
3. **Forces a handoff** at a hard threshold (default 60%): the agent must append a structured entry to an append-only **handoff ledger** and end the session — while a detached **compactor** snapshots the session state in the background.
4. **Bootstraps** the next session automatically from the ledger (via a `SessionStart` hook in Claude Code, and a project rule in Cursor), so multi-session plans continue without you copy-pasting context.

One file, Python 3.9+ stdlib only, no dependencies. Auditable in five minutes.

## Design lineage

The engine borrows the `ContextEngine` ideas from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — threshold-driven monitoring, head/tail protection, and compaction performed by a process *outside* the agent's inference loop.

One key adaptation: Hermes owns its inference loop, so it can rewrite the live context in place and the agent never notices. Cursor and Claude Code don't let external code mutate the live conversation — so compaction here happens **across sessions** instead of within one:

```
Hermes:            [head | ~~summarized middle~~ | tail]  -> same session continues
context-governor:  session A hits 60% -> snapshot (head + digest + tail) + ledger entry
                   session B starts fresh -> bootstrapped from ledger + snapshot
```

The ledger doubles as a write-ahead log (the `tunc-clm` idea) and its required entry format — exact next step, verification status, open risks — acts as a lightweight handoff protocol (the `Agent_Handoff` idea).

## Install

```bash
git clone https://github.com/vsrox-cliqq/context-governor
cd context-governor

# Claude Code (user-level) + Cursor (user-level):
python3 install.py --all

# Or per project (recommended for Cursor — also installs the bootstrap rule):
python3 install.py --cursor-project /path/to/your/repo
python3 install.py --claude
```

The installer **merges** into `~/.cursor/hooks.json` / `~/.claude/settings.json` and writes a timestamped backup first. It never clobbers existing hooks.

## Configure

Copy `config.example.json` to `~/.context-governor/config.json` (user-level) or `<workspace>/.context-governor.json` (per project — overrides user config):

```json
{
  "warn_pct": 50,
  "handoff_pct": 60,
  "ledger_path": "handoff/LEDGER.md",
  "auto_compact": true,
  "summarizer_cmd": "",
  "tools": {
    "cursor": { "window_tokens": 200000, "bytes_per_token": 4 },
    "claude_code": { "window_tokens": 200000 }
  }
}
```

- `window_tokens` — set to your model's context window (1,000,000 for 1M-window models).
- `summarizer_cmd` — optional Hermes-style LLM compaction. Any command that reads a prompt on stdin and prints a summary works, e.g. `claude -p --model haiku` (headless Claude Code) or `ollama run llama3.2`. Leave empty for the no-LLM structural digest (head task + files touched + commands run + protected tail), which is fast, free, and offline.
- `refire_delta_pct` — if the agent ignores the handoff order, it re-fires every N percentage points.

## What the agent experiences

- **Below 50%**: nothing. The hook is silent (`{}`) and adds ~10ms per tool call.
- **At 50%**: a one-time system reminder — wrap up the current slice, start nothing new.
- **At 60%**: a hard order — park the atomic step in progress, append a ledger entry to `handoff/LEDGER.md`, tell the user to start a fresh session. Meanwhile a detached compactor writes `handoff/state-<session>.md`.
- **Next session**: Claude Code injects the last ledger entry at `SessionStart`; Cursor picks it up through the always-on project rule. The agent starts from "Next step", not from zero.

## Commands

```bash
python3 governor.py status      # context usage of recent Cursor + Claude sessions
python3 governor.py compact --transcript <path.jsonl> --out state.md   # manual snapshot
python3 tests/test_governor.py  # run the test suite (12 tests, stdlib only)
```

## How measurement works

| Tool | Method | Accuracy |
|---|---|---|
| Claude Code | `input_tokens + cache_read + cache_creation` from the last assistant message's `usage` in the transcript JSONL | Exact |
| Cursor | transcript file size ÷ `bytes_per_token` (default 4) | Proxy — errs conservative |

Cursor performs its own context summarization, so the live context may be smaller than the byte estimate — meaning the governor hands off slightly *early*, which is the safe direction. Tune `bytes_per_token` upward if it triggers too soon for your usage pattern.

## Repository layout

```
governor.py         # the whole engine: hook / compact / status subcommands
install.py          # merging installer for Cursor + Claude Code
config.example.json
templates/
  session-handoff.mdc   # Cursor bootstrap rule (installed per project)
tests/
  test_governor.py      # 12 tests, no dependencies
```

## Limitations

- Cursor measurement is a proxy (see above); Claude Code is exact.
- The forced handoff is delivered as injected context, not a hard kill — a
  sufficiently confused model could ignore it, which is why it re-fires.
- Subagent transcripts aren't counted toward the parent session's budget.

## License

MIT
