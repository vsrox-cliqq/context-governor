# Agent install guide — context-governor

You are an AI coding agent (Claude Code or similar) installing **context-governor** on the user's machine. Follow these steps in order. Everything is reversible: the installer merges into existing config and writes a timestamped backup before touching anything.

## What you are installing

The whole engine is **one stdlib-only Python file** (`governor.py`, Python 3.9+), downloaded to `~/.context-governor/app/`. Its self-installer then sets up:

1. **Two hooks** in `~/.claude/settings.json` — `PostToolUse` and `SessionStart` — that run `python3 ~/.context-governor/app/governor.py hook`. They measure context usage after every tool call, warn the agent at 50% of the model's context window, force a structured handoff to a ledger at 60%, and bootstrap the next session from that ledger.
2. **A `governor` launcher** at `~/.local/bin/governor`, so the user can run `governor engage` (chain interactive sessions through the ledger), `governor status`, and `governor run` from any project directory. If `~/.local/bin` isn't on the user's PATH, the installer appends one `export PATH=...` line to their shell rc (`~/.zshrc` / `~/.bashrc` / `~/.profile`) and says so — pass `--no-modify-path` to suppress that.

No dependencies are installed. No git checkout is needed.

## Steps

1. **Check prerequisites.** Run `python3 --version` — must be 3.9 or newer. If missing, stop and tell the user what to install.

2. **Run the installer:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
   ```

   If piping curl to bash is blocked in your environment, do the equivalent manually:

   ```bash
   mkdir -p ~/.context-governor/app
   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/governor.py -o ~/.context-governor/app/governor.py
   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/config.example.json -o ~/.context-governor/app/config.example.json
   python3 ~/.context-governor/app/governor.py install --claude
   ```

   (Re-running either form updates an existing install in place.)

3. **Verify the hooks landed.** Read `~/.claude/settings.json` and confirm there is a `PostToolUse` entry **and** a `SessionStart` entry whose command contains `governor.py" hook`. Also confirm `~/.local/bin/governor` exists and is executable. If the installer printed that it added a PATH line to the user's shell rc, remember it — you must relay that in your final message (a new shell is needed before `governor` resolves).

4. **Smoke-test the engine:**

   ```bash
   python3 ~/.context-governor/app/governor.py status
   ```

   Exit code 0 is a pass. An empty table is normal on a fresh install — no sessions have been measured yet.

5. **Tell the user to restart, and how to use it.** Hooks load at session start, so the governor takes effect only in *new* sessions. Tell the user, in your final message:
   - Restart Claude Code (CLI: start a new session; desktop app: quit and relaunch).
   - After working a while, `governor status` shows recent sessions with token counts and detected model windows — if rows appear, the hooks are firing.
   - **The flagship feature:** for a long task, run `governor engage` in the project directory instead of plain `claude` — it chains interactive sessions through the handoff ledger automatically (requires the `claude` CLI).
   - If the installer added a PATH line to their shell rc, tell them to open a new terminal (or `exec zsh` / `exec bash`) before `governor` resolves.
   - Optional config lives at `~/.context-governor/config.json` (see `config.example.json` next to `governor.py`); defaults are warn at 50%, handoff at 60%.

## Rules

- **Do not edit `~/.claude/settings.json` by hand.** Use the installer — it merges, never clobbers existing hooks, and backs up first.
- If the installer reports that `settings.json` exists but is invalid JSON, **stop and show the user the error** rather than repairing the file silently.
- Do not install the Cursor variant (`--cursor`) unless the user asked for it.
- **Do not edit the user's shell profile yourself.** The installer's PATH fix (one export line, only when missing) is the only sanctioned modification — relay what it printed instead of adding your own.
- Nothing outside `~/.context-governor/`, `~/.claude/settings.json`, `~/.local/bin/governor`, and (only if PATH was missing) one line in the user's shell rc is modified.
