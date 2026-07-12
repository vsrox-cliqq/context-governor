<div align="center">

<img src="assets/governor.png" alt="The Governor — context-governor mascot" width="300"/>

# context-governor

**Run Claude Code indefinitely — without the context rot.**

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-58a6ff?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-27%20passing-3fb950?style=flat-square)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-8b949e?style=flat-square)](LICENSE)
[![stdlib only](https://img.shields.io/badge/deps-stdlib%20only-d29922?style=flat-square)](#)

</div>

One task, as many sessions as it takes. The governor measures every tool call, ends the session at 60% of the context window — *before* quality drops — and boots the next session from a structured handoff ledger. Sessions chain automatically; the work never loses its place.

---

## Quick start

```bash
# 1. Install — downloads one stdlib-only Python file, registers two hooks
curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash

# 2. Restart Claude Code, then run this in your project instead of `claude`:
governor engage
```

That's it. The installer merges the hooks into `~/.claude/settings.json` (timestamped backup first, existing hooks never touched) and puts `governor` on your PATH. Python 3.9+ is the only prerequisite; re-running updates in place.

**Or let Claude Code install it for you** — paste this into any session:

```
Install context-governor: fetch https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/AGENT_INSTALL.md and follow it.
```

Claude checks prerequisites, runs the installer, verifies the hooks landed, and tells you when to restart. ([AGENT_INSTALL.md](AGENT_INSTALL.md) is short — read it first if you want to know exactly what it does.)

To confirm it's working, work for a bit and run `governor status` — if recent sessions show up with token counts and model names, the hooks are firing.

<details>
<summary><strong>Other install paths</strong> — plugin, manual</summary>

**As a Claude Code plugin:**

```
/plugin marketplace add vsrox-cliqq/context-governor
/plugin install context-governor@context-governor
```

> The plugin wires the hooks only — it doesn't add the `governor` command, so for `engage`/`run`/`status` use the installer instead. In the desktop app, plugin hooks only fire in **Cowork** sessions, not the regular Code tab; desktop users should prefer the installer (the desktop app shares `~/.claude/settings.json` with the CLI — quit and relaunch afterwards).

**Manual:**

```bash
git clone https://github.com/vsrox-cliqq/context-governor
cd context-governor
python3 governor.py install --claude
```

If `~/.local/bin` wasn't on your PATH, the installer adds one export line to your shell rc — open a new terminal for `governor` to resolve (`--no-modify-path` to opt out).

</details>

---

## Why

Long agentic sessions degrade. Past ~60% of the context window, Claude drifts — it forgets constraints, revisits decisions, and redoes work it already completed. By the time you notice, quality has already slipped. The built-in remedies don't fix this:

| | auto-compact | `/compact` | **context-governor** |
|---|:---:|:---:|:---:|
| Fires at | ~90–95% | whenever you remember | **50% warn / 60% stop** |
| Output | lossy in-session summary | lossy in-session summary | **structured ledger entry** |
| Inspectable / committable | ✗ | ✗ | **✓** |
| Next session bootstrapped | ✗ | ✗ | **✓** |
| Requires you to be watching | ✗ | **✓** | ✗ |

Auto-compact fires after the damage is done and leaves no external record (on 1M-window models it effectively never fires before the 60% handoff, so the two coexist cleanly). `/compact` requires you to be watching and its summary dies with the session. The ecosystem's other answers don't fix it either: loop tools (Ralph-style) rerun Claude until a task is done but fly blind on context — they'll happily let a session rot past 90% before it dies — and memory tools compress and recall after the fact.

What none of them give you: a running external record of where the agent is in the plan, so the *next* session picks up exactly where the last one left off. The governor **measures** the window on every tool call and hands off at exactly the right moment — an indefinite run made of sessions that are all still sharp.

---

## How it works

Context-governor runs as a Claude Code hook — after every tool call and at session start. No new process to manage; the hooks fire automatically.

```
Every tool call
  └─ measure exact token count from transcript
       └─ resolve model window (1M or 200K, auto-detected)

  [ 0% → 50% ]   silent — ~10ms overhead per tool call

  [ at 50% ]     ⚠ WARN: finish the slice in flight, start nothing new
                         handoff triggers at 60%

  [ at 60% ]     🛑 HANDOFF: agent writes a structured entry to
                    handoff/LEDGER.md (next step, open risks, key files,
                    verification status), then ends the session.
                    Re-fires every 3% if the agent ignores it.

                 ↓ background compactor snapshots the transcript

  [ next session start ]
                 SessionStart hook reads the last ledger entry →
                 injects it as context → agent starts from "Next step",
                 not from zero.
```

The ledger is append-only, human-readable, and git-committable. It's not a summary — it's a handoff protocol: exact next action, what was verified, what's open, which files matter.

The hooks work under plain `claude`, too: the agent still gets warned, still writes the handoff, and your next session still starts bootstrapped — you just relaunch it yourself. The two commands below only automate the relaunch.

---

## Commands

### `governor engage` — you at the terminal

```bash
cd your-project
governor engage
```

Run it instead of `claude`. Work normally; when the session hits 60% of the window, the agent writes its handoff entry and ends, and `engage` relaunches a fresh session bootstrapped from that entry. Write the plan once, execute across as many sessions as it takes. It stops when a session ends without a handoff (natural finish, or you quit) or when the ledger says `DONE`.

**Pinning a model** — any `claude` flag passes straight through, and every chained session relaunches with the same flags. This matters for long runs: pin a cheaper or specific model once and the whole chain honors it, instead of resetting to your default on every fresh session:

```bash
governor engage --model claude-sonnet-4-6            # pin Sonnet 4.6 for the whole run
governor engage --model claude-haiku-4-5-20251001    # cheap model for grunt work
governor engage --model claude-opus-4-8 --auto       # heavyweight, no prompts between sessions
```

Other options:

- `--auto` — relaunch without asking between sessions
- `--max-sessions 12` — hard cap on how many sessions it will chain
- `--claude-cmd "my-wrapper"` — replace the `claude` binary entirely (passthrough flags are appended to it)

### `governor run` — nobody at the terminal

```bash
governor run --workspace /path/to/repo --task "Implement the remaining stages of build-plan.md"
```

The same loop, but with no human present: each session runs Claude in headless mode, works one slice of the plan, writes its ledger entry, and exits; `run` starts the next session — until the ledger's last entry says the plan is `DONE`.

Because nobody is watching, it knows when to stop itself:

- a session ends **without writing a new ledger entry** → no progress, stop
- **8 sessions** have run (configurable) → stop
- a single session exceeds **1 hour** (configurable) → stop
- everything each session did is logged under `~/.context-governor/state/`

One decision is yours: a headless session can't ask you "allow this edit?", so you grant permissions up front. For example, `--agent-cmd 'claude -p {prompt} --permission-mode acceptEdits'` lets sessions edit files without asking. Grant only what you're comfortable with.

### The rest

| | |
|---|---|
| `governor status` | table of recent sessions — tokens used, % of window, detected model. Your "is it working?" check. |
| `governor install` | re-run the self-installer (also: `--cursor` variants) |
| `governor compact --transcript <path> --out state.md` | snapshot a transcript into a state file by hand, outside the hook cycle |

---

## Configuration

Optional — the defaults (warn at 50%, handoff at 60%, automatic model detection) are the recommended setup. To change them, copy `config.example.json` to `~/.context-governor/config.json` (user-level) or `<workspace>/.context-governor.json` (per-project, wins over user-level):

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

- `warn_pct` / `handoff_pct` — the two thresholds.
- `refire_delta_pct` — how often the handoff order repeats if the agent ignores it (default: every 3%).
- `summarizer_cmd` — optional LLM compaction. Any command that reads a prompt on stdin and prints a summary: `claude -p --model haiku` or `ollama run llama3.2`. Leave empty for the no-LLM structural digest (fast, free, offline).
- `model_windows` — per-model context-window overrides. Rarely needed: the governor reads the model ID from the transcript and resolves the window automatically — 1M for Fable/Mythos 5, Opus 4.6+, Sonnet 4.6+, and `[1m]`-suffixed beta models; 200K for Haiku 4.5 and older models. Unknown models fall back to `window_tokens` (default 200,000). Any substring of the model ID works as a key: `{ "model_windows": { "claude-sonnet-4-6": 200000 } }`. Every warn/handoff message and `status` row shows the resolved model and window, so a wrong assumption is immediately visible.

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
governor install --cursor-project /path/to/your/repo   # per project (recommended)
governor install --cursor                              # user-level ~/.cursor/hooks.json
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
