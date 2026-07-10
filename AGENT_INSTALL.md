# Agent install guide — context-governor

You are an AI coding agent (Claude Code or similar) installing **context-governor** on the user's machine. Follow these steps in order. Everything is reversible: the installer merges into existing config and writes a timestamped backup before touching anything.

## What you are installing

Two hooks in `~/.claude/settings.json` — `PostToolUse` and `SessionStart` — that run `python3 ~/.context-governor/app/governor.py hook`. They measure context usage after every tool call, warn the agent at 50% of the model's context window, force a structured handoff to a ledger at 60%, and bootstrap the next session from that ledger. No dependencies are installed; the engine is a single Python 3.9+ stdlib-only file.

## Steps

1. **Check prerequisites.** Run `git --version` and `python3 --version`. Python must be 3.9 or newer. If either is missing, stop and tell the user what to install.

2. **Run the installer:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
   ```

   If piping curl to bash is blocked in your environment, do the equivalent manually:

   ```bash
   git clone --depth 1 https://github.com/vsrox-cliqq/context-governor ~/.context-governor/app
   python3 ~/.context-governor/app/install.py --claude
   ```

   (If `~/.context-governor/app` already exists as a git checkout, `git -C ~/.context-governor/app pull --ff-only` instead of cloning.)

3. **Verify the hooks landed.** Read `~/.claude/settings.json` and confirm there is a `PostToolUse` entry **and** a `SessionStart` entry whose command contains `governor.py" hook`.

4. **Smoke-test the engine:**

   ```bash
   python3 ~/.context-governor/app/governor.py status
   ```

   Exit code 0 is a pass. An empty table is normal on a fresh install — no sessions have been measured yet.

5. **Tell the user to restart.** Hooks load at session start, so the governor takes effect only in *new* sessions. Tell the user, in your final message:
   - Restart Claude Code (CLI: start a new session; desktop app: quit and relaunch).
   - After working a while, `python3 ~/.context-governor/app/governor.py status` shows recent sessions with token counts and detected model windows — if rows appear, the hooks are firing.
   - Optional config lives at `~/.context-governor/config.json` (see `config.example.json` in the repo); defaults are warn at 50%, handoff at 60%.

## Rules

- **Do not edit `~/.claude/settings.json` by hand.** Use the installer — it merges, never clobbers existing hooks, and backs up first.
- If the installer reports that `settings.json` exists but is invalid JSON, **stop and show the user the error** rather than repairing the file silently.
- Do not install the Cursor variant (`--cursor`) unless the user asked for it.
- Nothing outside `~/.context-governor/` and `~/.claude/settings.json` is modified.
