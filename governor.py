#!/usr/bin/env python3
"""context-governor — a context-budget engine for hook-based coding agents.

Borrowed from NousResearch/hermes-agent's ContextEngine design:
  * threshold-driven monitoring of context usage,
  * head/tail protection with middle compaction,
  * a background compactor that runs outside the agent's inference loop.

Adapted for tools we don't control (Cursor, Claude Code): we cannot rewrite
the live context in place, so compaction happens ACROSS sessions — when the
budget is hit, a detached compactor snapshots the session (head + digest +
tail) to a state file, the agent appends a handoff entry to an append-only
ledger, and the next session bootstraps from both.

Stdlib only. One file. Python 3.9+.

Subcommands:
  hook     read a Cursor / Claude Code hook payload on stdin, emit hook JSON
  compact  snapshot a transcript into a handoff state file
  status   show estimated context usage for recent sessions
  engage   chain interactive Claude Code sessions through the ledger: when a
           session ends after a handoff, relaunch a fresh bootstrapped one
  run      fully autonomous mode: loop headless agent sessions, each scoped to
           one plan slice ending in a ledger entry, until the ledger says DONE
  install  self-installer: merge the hooks into Claude Code (and/or Cursor)
           config, put a `governor` launcher on the PATH
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = {
    "warn_pct": 50,
    "handoff_pct": 60,
    "refire_delta_pct": 3,
    "ledger_path": "handoff/LEDGER.md",
    "state_dir": "handoff",
    "auto_compact": True,
    "head_chars": 1500,
    "tail_messages": 6,
    "summarizer_cmd": "",
    "agent_cmd": "",
    "max_sessions": 8,
    "session_timeout": 3600,
    "model_windows": {},
    "tools": {
        "cursor": {"window_tokens": 200000, "bytes_per_token": 4},
        "claude_code": {"window_tokens": 200000},
    },
}

# Built-in context windows by model-ID prefix. First match wins, so more
# specific prefixes must come before generic ones. A "[1m]" suffix on the ID
# (Claude Code's 1M-context beta marker) always means 1M regardless of base
# model. Users can override any model via the "model_windows" config map.
MODEL_WINDOWS = [
    ("claude-fable", 1000000),
    ("claude-mythos", 1000000),
    ("claude-opus-4-6", 1000000),
    ("claude-opus-4-7", 1000000),
    ("claude-opus-4-8", 1000000),
    ("claude-sonnet-4-6", 1000000),
    ("claude-sonnet-5", 1000000),
    ("claude-haiku", 200000),
    ("claude-opus", 200000),
    ("claude-sonnet", 200000),
]


def resolve_window(model, cfg):
    """Context window for a model ID.

    Precedence: config "model_windows" override > "[1m]" suffix > built-in
    table > configured window_tokens > 200k.
    """
    fallback = cfg["tools"]["claude_code"].get("window_tokens", 200000)
    if not model:
        return fallback
    for key, win in (cfg.get("model_windows") or {}).items():
        if key in model:
            return int(win)
    if model.endswith("[1m]"):
        return 1000000
    # Tolerate vendor-prefixed IDs like "us.anthropic.claude-opus-4-8-v1:0".
    base = model[model.find("claude-"):] if "claude-" in model else model
    for prefix, win in MODEL_WINDOWS:
        if base.startswith(prefix):
            return win
    return fallback

LEDGER_TEMPLATE = """# Session Handoff Ledger

Append-only log passing state between agent sessions. Managed by
context-governor (https://github.com/vsrox-cliqq/context-governor).

## Rules

- **Append only.** Never edit or delete previous entries; the newest entry at
  the bottom is the authoritative state.
- Every session that hits the context budget (or ends mid-task) MUST append an
  entry before stopping.
- Every fresh session MUST read the LAST entry before doing anything else,
  then follow its "Next step".
- Reference artifacts (plans, progress docs, commits) by path; do not
  duplicate their content here.

## Entry format

```markdown
---
## <YYYY-MM-DD HH:MM> — <short description>

**Plan stage / slice:** <what slice of the plan this session took>
**Completed this session:**
- <what was done, with file paths>

**Verification status:** <tests run and results / explicitly "not verified">
**Current state:** <where things stand; anything half-done and how it was parked>
**Next step (exact):** <the single next atomic action for the next session>
**Open risks / gotchas:** <anything the next session must know>
**Key files:** <paths touched or that must be read first>
**State snapshot:** <path to the machine-written state file, if any>
```

<!-- Entries below. Newest at the bottom. -->
"""


# ---------------------------------------------------------------- config ---

def load_config(workspace=None):
    """Merge config from defaults <- user config <- workspace config <- $CG_CONFIG."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    candidates = [Path.home() / ".context-governor" / "config.json"]
    if workspace:
        candidates.append(Path(workspace) / ".context-governor.json")
    if os.environ.get("CG_CONFIG"):
        candidates.append(Path(os.environ["CG_CONFIG"]))
    for path in candidates:
        try:
            with open(path) as f:
                overlay = json.load(f)
        except (OSError, ValueError):
            continue
        for key, value in overlay.items():
            if key == "tools" and isinstance(value, dict):
                for tool, tool_cfg in value.items():
                    cfg["tools"].setdefault(tool, {}).update(tool_cfg)
            else:
                cfg[key] = value
    return cfg


def state_dir():
    base = os.environ.get("CG_STATE_DIR") or str(
        Path.home() / ".context-governor" / "state"
    )
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base)


# ----------------------------------------------------------- measurement ---

def measure_claude(transcript_path, cfg):
    """Real context size: token usage reported on the last assistant message.

    The window is resolved from the model ID on that same message, so the
    budget respects whichever model the session is actually running.
    """
    try:
        with open(transcript_path, "rb") as f:
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage or "input_tokens" not in usage:
            continue
        model = message.get("model") or ""
        if model == "<synthetic>":
            model = ""
        window = resolve_window(model, cfg)
        tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        return {"tokens": tokens, "window": window,
                "pct": tokens * 100 // window, "model": model or "unknown"}
    return None


def cursor_transcript_path(conversation_id):
    roots = os.environ.get("CG_CURSOR_PROJECTS") or str(
        Path.home() / ".cursor" / "projects"
    )
    pattern = os.path.join(
        roots, "*", "agent-transcripts", conversation_id, conversation_id + ".jsonl"
    )
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def measure_cursor(transcript_path, cfg):
    """Proxy context size: transcript bytes / bytes_per_token."""
    tool_cfg = cfg["tools"]["cursor"]
    window = tool_cfg.get("window_tokens", 200000)
    bpt = tool_cfg.get("bytes_per_token", 4)
    if not transcript_path:
        return None
    try:
        tokens = os.path.getsize(transcript_path) // bpt
    except OSError:
        return None
    return {"tokens": tokens, "window": window, "pct": tokens * 100 // window,
            "transcript": transcript_path}


# -------------------------------------------------------- level tracking ---

def decide_level(session_id, pct, cfg):
    """Return 'warn' | 'handoff' | None using per-session persisted state.

    warn fires once; handoff fires on first crossing then again every
    refire_delta_pct if the agent keeps going.
    """
    sf = state_dir() / (re.sub(r"[^A-Za-z0-9_-]", "_", session_id) + ".json")
    prev = {"level": 0, "pct": 0}
    try:
        with open(sf) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass

    level = 0
    if pct >= cfg["warn_pct"]:
        level = 1
    if pct >= cfg["handoff_pct"]:
        level = 2

    # If usage/thresholds moved back down (config change, recalibration),
    # reset so the escalation can fire again on the next crossing.
    if level < prev["level"]:
        with open(sf, "w") as f:
            json.dump({"level": level, "pct": pct, "ts": time.time()}, f)
        prev = {"level": level, "pct": pct}

    fire = None
    if level == 2 and (
        prev["level"] < 2 or pct - prev["pct"] >= cfg["refire_delta_pct"]
    ):
        fire = "handoff"
    elif level == 1 and prev["level"] < 1:
        fire = "warn"

    if fire:
        with open(sf, "w") as f:
            json.dump({"level": level, "pct": pct, "ts": time.time()}, f)
    return fire


# --------------------------------------------------------------- messages ---

def usage_phrase(m):
    """Human-readable usage like '55% (~110,000 of 200,000 tokens, model X)'."""
    phrase = "{pct}% (~{tok:,} of {win:,} tokens".format(
        pct=m["pct"], tok=m["tokens"], win=m["window"])
    if m.get("model"):
        phrase += ", model {}".format(m["model"])
    return phrase + ")"


def warn_message(m, cfg):
    return (
        "CONTEXT BUDGET WARNING: estimated context usage is {usage} of the "
        "window; forced handoff triggers at {hard}%. Aim to reach a clean "
        "stopping point on the current slice. Do not start any new stage, "
        "large exploration, or broad refactor in this session. Prefer finishing and "
        "verifying what is in flight so the handoff is clean."
    ).format(usage=usage_phrase(m), hard=cfg["handoff_pct"])


def handoff_message(m, cfg, snapshot_path=None):
    snap = (
        " A machine-written state snapshot is being saved to {p}; reference it in "
        "your ledger entry.".format(p=snapshot_path)
        if snapshot_path
        else ""
    )
    return (
        "CONTEXT BUDGET EXCEEDED: estimated context usage is {usage}, above the "
        "{hard}% handoff threshold. STOP taking on new "
        "work now. Do the following in order: (1) finish or safely park ONLY the "
        "atomic step currently in progress — do not start the next step; "
        "(2) append a handoff entry to {ledger} following the entry format "
        "documented at the top of that file (create it from the documented format "
        "if missing) — completed work, verification status, current state, exact "
        "next step, open risks, key files;{snap} (3) end your turn by telling the "
        "user: context budget reached, handoff written, please start a FRESH "
        "session. Do not attempt further implementation in this session."
    ).format(
        usage=usage_phrase(m), hard=cfg["handoff_pct"],
        ledger=cfg["ledger_path"], snap=snap,
    )


# ------------------------------------------------------------ hook entry ---

def emit(payload):
    sys.stdout.write(json.dumps(payload))


def spawn_compactor(transcript, out_path, workspace):
    try:
        subprocess.Popen(
            [
                sys.executable, os.path.abspath(__file__), "compact",
                "--transcript", transcript, "--out", str(out_path),
                "--workspace", workspace,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def last_ledger_entry(ledger_file):
    try:
        text = Path(ledger_file).read_text()
    except OSError:
        return None
    # Only look below the entries marker so the documented entry-format
    # template in the header is never mistaken for a real entry.
    marker = "<!-- Entries below"
    if marker in text:
        text = text.split(marker, 1)[1]
        text = text.split("-->", 1)[-1]
    parts = text.split("\n---\n")
    if len(parts) < 2:
        return None
    return parts[-1].strip() or None


def heartbeat(payload, measurement=None):
    """Record the last hook invocation so `install` problems are debuggable."""
    try:
        with open(state_dir() / "last-invocation.json", "w") as f:
            json.dump({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": payload.get("hook_event_name", "?"),
                "tool_call": payload.get("tool_name", "?"),
                "session": (payload.get("session_id")
                            or payload.get("conversation_id") or "?"),
                "pct": measurement["pct"] if measurement else None,
                "payload_keys": sorted(payload.keys()),
            }, f, indent=1)
    except OSError:
        pass


def cmd_hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        emit({})
        return

    # Cursor payloads carry cursor_version / conversation_id and camelCase
    # event names ("postToolUse"); Claude Code uses PascalCase ("PostToolUse")
    # and has no conversation_id. Both carry transcript_path, so that field
    # cannot distinguish them.
    is_cursor = "cursor_version" in payload or "conversation_id" in payload
    event = payload.get("hook_event_name", "")
    is_claude = not is_cursor and (
        "transcript_path" in payload or event[:1].isupper()
    )

    if is_claude:
        workspace = payload.get("cwd") or os.getcwd()
        cfg = load_config(workspace)

        # Session bootstrap: inject the last ledger entry into a fresh session.
        if payload.get("hook_event_name") == "SessionStart":
            entry = last_ledger_entry(Path(workspace) / cfg["ledger_path"])
            if entry:
                emit({
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            "RESUME PROTOCOL (context-governor): this workspace runs "
                            "long plans across sessions with a context budget. The "
                            "latest handoff ledger entry follows — treat its 'Next "
                            "step' as your starting task and its risks as "
                            "constraints. Scope this session to ONE slice.\n\n" + entry
                        ),
                    }
                })
            else:
                emit({})
            return

        transcript = payload.get("transcript_path", "")
        m = measure_claude(transcript, cfg) if transcript else None
        heartbeat(payload, m)
        if not m:
            emit({})
            return
        session = payload.get("session_id") or "claude-unknown"
        fire = decide_level(session, m["pct"], cfg)
        if fire == "handoff":
            snap = Path(workspace) / cfg["state_dir"] / (
                "state-" + session[:8] + ".md"
            )
            if cfg.get("auto_compact"):
                spawn_compactor(transcript, snap, workspace)
                msg = handoff_message(m, cfg, str(snap))
            else:
                msg = handoff_message(m, cfg)
        elif fire == "warn":
            msg = warn_message(m, cfg)
        else:
            emit({})
            return
        emit({
            "hookSpecificOutput": {
                "hookEventName": payload.get("hook_event_name", "PostToolUse"),
                "additionalContext": msg,
            }
        })
        return

    if is_cursor:
        roots = payload.get("workspace_roots") or []
        workspace = roots[0] if roots else os.getcwd()
        cfg = load_config(workspace)
        transcript = payload.get("transcript_path") or cursor_transcript_path(
            payload.get("conversation_id", "")
        )
        m = measure_cursor(transcript, cfg)
        heartbeat(payload, m)
        if not m:
            emit({})
            return
        conv = payload.get("conversation_id") or payload.get("session_id", "?")
        fire = decide_level(conv, m["pct"], cfg)
        if fire == "handoff":
            snap = Path(workspace) / cfg["state_dir"] / (
                "state-" + conv[:8] + ".md"
            )
            if cfg.get("auto_compact"):
                spawn_compactor(m["transcript"], snap, workspace)
                msg = handoff_message(m, cfg, str(snap))
            else:
                msg = handoff_message(m, cfg)
        elif fire == "warn":
            msg = warn_message(m, cfg)
        else:
            emit({})
            return
        emit({"additional_context": msg})
        return

    emit({})


# ------------------------------------------------------------- compactor ---

def normalize_transcript(path):
    """Parse Cursor or Claude Code JSONL into [(role, text, tools)] messages.

    tools is a list of (tool_name, salient_arg) for tool_use blocks.
    """
    messages = []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return messages
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = entry.get("role") or msg.get("role") or entry.get("type") or "?"
        content = msg.get("content")
        if isinstance(content, str):
            messages.append((role, content, []))
            continue
        if not isinstance(content, list):
            continue
        texts, tools = [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use":
                arg = ""
                inp = block.get("input") or {}
                for key in ("path", "file_path", "target_notebook", "command",
                            "pattern", "url"):
                    if inp.get(key):
                        arg = str(inp[key])
                        break
                tools.append((block.get("name", "?"), arg))
        if texts or tools:
            messages.append((role, "\n".join(texts), tools))
    return messages


def strip_meta(text):
    """Drop injected XML-ish wrappers (<user_query>, <system_reminder>, ...)."""
    inner = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if inner:
        return inner.group(1)
    return re.sub(r"<[^>]+>", "", text).strip()


def truncate(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def structural_digest(messages, cfg):
    """No-LLM fallback: head task + chronological digest + protected tail."""
    head_chars = cfg.get("head_chars", 1500)
    tail_n = cfg.get("tail_messages", 6)

    head = ""
    for role, text, _ in messages:
        if role == "user" and text.strip():
            head = strip_meta(text)
            break

    user_turns, files, commands = [], [], []
    for role, text, tools in messages:
        if role == "user" and text.strip():
            cleaned = strip_meta(text)
            if cleaned and cleaned != head:
                user_turns.append(truncate(cleaned, 200))
        for name, arg in tools:
            if name in ("Shell", "Bash") and arg:
                commands.append(truncate(arg, 120))
            elif arg and ("/" in arg or "\\" in arg):
                files.append(arg)

    def dedupe(seq, cap):
        seen, out = set(), []
        for item in seq:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out[-cap:]

    files = dedupe(files, 40)
    commands = dedupe(commands, 25)

    tail_lines = []
    for role, text, tools in messages[-tail_n:]:
        body = truncate(strip_meta(text), 300) if text.strip() else ""
        tool_note = ", ".join(
            "{}({})".format(n, truncate(a, 60)) if a else n for n, a in tools
        )
        line = "- **{}**: {}".format(role, body or "(no text)")
        if tool_note:
            line += " [tools: {}]".format(tool_note)
        tail_lines.append(line)

    sections = [
        "## Task (protected head)\n\n" + (truncate(head, head_chars) or "(none found)"),
        "## Later user instructions\n\n"
        + ("\n".join("- " + t for t in user_turns[-15:]) or "- (none)"),
        "## Files touched\n\n"
        + ("\n".join("- `{}`".format(f) for f in files) or "- (none recorded)"),
        "## Commands run\n\n"
        + ("\n".join("- `{}`".format(c) for c in commands) or "- (none recorded)"),
        "## Recent activity (protected tail)\n\n" + "\n".join(tail_lines),
    ]
    return "\n\n".join(sections)


def llm_summary(digest, messages, cfg):
    """Optional Hermes-style compaction: summarize the middle with a cheap model."""
    cmd = cfg.get("summarizer_cmd", "").strip()
    if not cmd:
        return None
    middle = messages[1:-cfg.get("tail_messages", 6)]
    middle_text = "\n".join(
        "[{}] {}".format(role, truncate(strip_meta(text), 500))
        for role, text, _ in middle if text.strip()
    )[:48000]
    prompt = (
        "You are compacting an AI coding session for handoff to a fresh session. "
        "Using the structural digest and middle-of-conversation excerpts below, "
        "write a concise state summary (<= 400 words) with sections: Current "
        "objective; Completed work; In-flight step; Recommended next step; Risks "
        "and gotchas; Key files. Be concrete — file paths, commands, decisions.\n\n"
        "=== STRUCTURAL DIGEST ===\n{}\n\n=== MIDDLE EXCERPTS ===\n{}".format(
            digest, middle_text
        )
    )
    try:
        result = subprocess.run(
            cmd, shell=True, input=prompt, capture_output=True, text=True,
            timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def ensure_ledger(workspace, cfg):
    ledger = Path(workspace) / cfg["ledger_path"]
    if not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(LEDGER_TEMPLATE)
    return ledger


def cmd_compact(args):
    cfg = load_config(args.workspace)
    messages = normalize_transcript(args.transcript)
    if not messages:
        sys.exit(1)
    digest = structural_digest(messages, cfg)
    summary = llm_summary(digest, messages, cfg)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "# Session state snapshot",
        "",
        "Generated by context-governor at {} from `{}`.".format(
            datetime.now().strftime("%Y-%m-%d %H:%M"), args.transcript
        ),
        "This is a machine-written compaction (head and tail protected, middle "
        "digested). The authoritative next step lives in the handoff ledger.",
        "",
    ]
    if summary:
        parts += ["## Model summary", "", summary, ""]
    parts += [digest, ""]
    out.write_text("\n".join(parts))

    if args.workspace:
        ensure_ledger(args.workspace, cfg)
    print(str(out))


# ------------------------------------------------------- autonomous run ---

RUN_PROTOCOL = """PROTOCOL (context-governor autonomous run):
You are ONE session in a chain of headless agent sessions executing a larger
plan. Nobody is watching interactively; never ask questions — make reasonable
decisions and record them.

1. Scope this session to exactly ONE plan slice (one stage or sub-task).
2. Verify your work (run tests/build) before finishing; report results honestly.
3. Before finishing you MUST append a handoff entry to {ledger} following the
   entry format documented at the top of that file. Never edit prior entries.
4. The entry MUST contain a line starting with '**Next step (exact):**'.
   Name the single next atomic action — or write exactly DONE if the entire
   plan is complete and verified.
5. After appending the ledger entry, stop. Do not start the next slice.
"""


def is_done(entry):
    m = re.search(r"next step\s*\(exact\)\s*:?\**\s*(.+)", entry, re.I)
    return bool(m and re.match(r"\s*\**\s*DONE\b", m.group(1)))


def detect_agent_cmd():
    if shutil.which("cursor-agent"):
        return "cursor-agent -p {prompt} --output-format text"
    if shutil.which("claude"):
        return "claude -p {prompt}"
    return None


def build_run_prompt(cfg, entry, task):
    parts = [RUN_PROTOCOL.format(ledger=cfg["ledger_path"])]
    if entry:
        parts.append(
            "RESUME: the latest handoff ledger entry follows. Treat its 'Next "
            "step' as this session's task and its risks as constraints.\n\n"
            + entry
        )
        if task:
            parts.append("OVERALL GOAL (for orientation only): " + task)
    else:
        parts.append("TASK (first session of the run): " + task)
    return "\n\n".join(parts)


def cmd_run(args):
    cfg = load_config(args.workspace)
    agent_cmd = args.agent_cmd or cfg.get("agent_cmd") or detect_agent_cmd()
    if not agent_cmd:
        sys.exit("No agent CLI found. Install cursor-agent or claude, or pass "
                 "--agent-cmd 'your-cli {prompt}'.")
    ledger = ensure_ledger(args.workspace, cfg)
    max_sessions = args.max_sessions or cfg.get("max_sessions", 8)
    timeout = cfg.get("session_timeout", 3600)
    log_path = state_dir() / "run-{}.log".format(time.strftime("%Y%m%d-%H%M%S"))

    entry = last_ledger_entry(ledger)
    if not entry and not args.task:
        sys.exit("Ledger has no entries yet; pass --task for the first session.")

    for i in range(1, max_sessions + 1):
        if entry and is_done(entry):
            print("Plan marked DONE in ledger. Stopping after {} session(s)."
                  .format(i - 1))
            return
        prompt = build_run_prompt(cfg, entry, args.task)
        if "{prompt}" in agent_cmd:
            cmd = agent_cmd.replace("{prompt}", shlex.quote(prompt))
            stdin_text = None
        else:
            cmd, stdin_text = agent_cmd, prompt
        print("[session {}/{}] {}".format(i, max_sessions, agent_cmd))
        print("  log: {}".format(log_path))
        with open(log_path, "a") as log:
            log.write("\n===== session {} @ {} =====\n".format(
                i, datetime.now().isoformat(timespec="seconds")))
            log.flush()
            try:
                result = subprocess.run(
                    cmd, shell=True, cwd=args.workspace, input=stdin_text,
                    stdout=log, stderr=subprocess.STDOUT, text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                print("  session timed out after {}s; stopping.".format(timeout))
                sys.exit(1)
        if result.returncode != 0:
            print("  agent exited with code {}; stopping. See log."
                  .format(result.returncode))
            sys.exit(1)
        new_entry = last_ledger_entry(ledger)
        if new_entry == entry:
            print("  no new ledger entry was appended — the agent did not "
                  "follow the handoff protocol; stopping to avoid burning "
                  "sessions. See log.")
            sys.exit(1)
        entry = new_entry
        step = re.search(r"next step.*", entry, re.I)
        print("  ledger updated; {}".format(step.group(0) if step else
                                            "(no next-step line found)"))

    print("Reached max sessions ({}) without DONE. Latest ledger entry:\n\n{}"
          .format(max_sessions, entry))
    sys.exit(1)


# ------------------------------------------------------------------ engage ---

def cmd_engage(args):
    """Chain INTERACTIVE Claude Code sessions through the ledger.

    Launches `claude` in the current terminal; when the session ends and a
    new handoff entry was appended (i.e. the budget fired and the agent wrote
    its handoff), launches a fresh session — which the SessionStart hook
    bootstraps from that entry. The human stays in the loop but never has to
    re-orient the agent between sessions.
    """
    cfg = load_config(args.workspace)
    ledger = ensure_ledger(args.workspace, cfg)
    extra = " ".join(shlex.quote(a) for a in (args.claude_args or []))
    claude_cmd = (args.claude_cmd or "claude") + (" " + extra if extra else "")
    max_sessions = args.max_sessions or cfg.get("max_sessions", 8)

    for i in range(1, max_sessions + 1):
        before = last_ledger_entry(ledger)
        print("\n[context-governor] session {}/{} — launching: {}".format(
            i, max_sessions, claude_cmd))
        try:
            subprocess.run(claude_cmd, shell=True, cwd=args.workspace)
        except KeyboardInterrupt:
            print("\n[context-governor] interrupted; stopping engagement.")
            return
        after = last_ledger_entry(ledger)
        if not after or after == before:
            print("[context-governor] no new ledger entry — session ended "
                  "without a handoff. Stopping engagement.")
            return
        if is_done(after):
            print("[context-governor] ledger says DONE. Engagement complete "
                  "after {} session(s).".format(i))
            return
        step = re.search(r"next step.*", after, re.I)
        print("[context-governor] handoff recorded; {}".format(
            step.group(0) if step else "(no next-step line found)"))
        if not args.auto:
            try:
                answer = input("[context-governor] start fresh session? [Y/n] ")
            except EOFError:
                return
            if answer.strip().lower() in ("n", "no", "q"):
                return
    print("[context-governor] reached max sessions ({}).".format(max_sessions))


# ---------------------------------------------------------------- status ---

def cmd_status():
    cfg = load_config(os.getcwd())
    rows = []
    roots = os.environ.get("CG_CURSOR_PROJECTS") or str(
        Path.home() / ".cursor" / "projects"
    )
    for path in glob.glob(os.path.join(roots, "*", "agent-transcripts", "*", "*.jsonl")):
        tokens = os.path.getsize(path) // cfg["tools"]["cursor"].get("bytes_per_token", 4)
        window = cfg["tools"]["cursor"].get("window_tokens", 200000)
        rows.append((os.path.getmtime(path), "cursor",
                     Path(path).stem[:8], tokens, tokens * 100 // window, ""))
    claude_root = Path.home() / ".claude" / "projects"
    for path in glob.glob(str(claude_root / "*" / "*.jsonl")):
        m = measure_claude(path, cfg)
        if m:
            rows.append((os.path.getmtime(path), "claude",
                         Path(path).stem[:8], m["tokens"], m["pct"],
                         "{} ({:,})".format(m["model"], m["window"])))
    rows.sort(reverse=True)
    print("{:<8} {:<10} {:>12} {:>6}   {:<32} thresholds: warn {}% / handoff {}%".format(
        "tool", "session", "est_tokens", "pct", "model (window)",
        cfg["warn_pct"], cfg["handoff_pct"]))
    for _, tool, session, tokens, pct, model in rows[:15]:
        flag = " <-- OVER BUDGET" if pct >= cfg["handoff_pct"] else (
            " <-- warn" if pct >= cfg["warn_pct"] else "")
        print("{:<8} {:<10} {:>12,} {:>5}%   {:<32}{}".format(
            tool, session, tokens, pct, model, flag))


# --------------------------------------------------------------- install ---

HOOK_CMD = 'python3 "{}" hook'.format(Path(__file__).resolve())

CURSOR_RULE = """\
---
description: Multi-session plan execution protocol — bootstrap from and write to the handoff ledger (context-governor)
alwaysApply: true
---

# Multi-session plan execution protocol

This workspace runs long plans across many agent sessions with a hard context
budget. A hook (context-governor) monitors context usage and will instruct you
when the budget is reached.

## At session start (before any other work)

1. Read the LAST entry of `handoff/LEDGER.md`. If it exists, treat its "Next
   step" as your starting task and its "Open risks" as constraints. If it
   references a state snapshot file, read that too. Do not re-derive state the
   ledger already records.
2. Scope the session to ONE plan slice (one stage or sub-task sized to fit
   well inside the context budget). State the slice you are taking before
   starting.

## While working

- Prefer targeted reads over broad exploration; the plan docs and ledger
  already carry the project state.
- When a hook message reports a CONTEXT BUDGET WARNING, stop starting new work
  and drive the in-flight step to a verifiable stopping point.

## At handoff (when the hook reports CONTEXT BUDGET EXCEEDED, or the session ends mid-plan)

1. Append an entry to `handoff/LEDGER.md` using the format documented at the
   top of that file. Never edit prior entries.
2. A valid entry MUST contain a concrete, singular "Next step" and a
   "Verification status". If verification was not run, say so explicitly — do
   not claim done.
3. Tell the user to start a fresh session; the new session will bootstrap from
   the ledger automatically.
"""


def _install_backup(path):
    if path.exists():
        dest = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(path, dest)
        print("  backup: {}".format(dest))


def _install_load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        print("ERROR: {} exists but is not valid JSON; fix it first.".format(path))
        sys.exit(1)


def install_claude(settings_file):
    data = _install_load_json(settings_file)
    hooks = data.setdefault("hooks", {})
    changed = False
    for event in ("PostToolUse", "SessionStart"):
        matchers = hooks.setdefault(event, [])
        already = any(
            h.get("command") == HOOK_CMD
            for m in matchers
            for h in m.get("hooks", [])
        )
        if already:
            print("  claude: {} already installed".format(event))
            continue
        matchers.append({"hooks": [{"type": "command", "command": HOOK_CMD}]})
        changed = True
        print("  claude: added {} hook".format(event))
    if changed:
        _install_backup(settings_file)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(data, indent=2) + "\n")
        print("  claude: wrote {}".format(settings_file))


def install_cursor(hooks_file):
    data = _install_load_json(hooks_file)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("postToolUse", [])
    if any(e.get("command") == HOOK_CMD for e in entries):
        print("  cursor: already installed in {}".format(hooks_file))
        return
    _install_backup(hooks_file)
    entries.append({"command": HOOK_CMD, "timeout": 10})
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(data, indent=2) + "\n")
    print("  cursor: installed postToolUse hook in {}".format(hooks_file))


def install_cursor_rule(project_dir):
    """Write the session-bootstrap rule into the project (Cursor has no
    hook-based context injection at session start, so a rule does it)."""
    rules_dir = Path(project_dir) / ".cursor" / "rules"
    dest = rules_dir / "session-handoff.mdc"
    if dest.exists():
        print("  cursor: rule already present at {}".format(dest))
        return
    rules_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(CURSOR_RULE)
    print("  cursor: bootstrap rule installed at {}".format(dest))


def _ensure_on_path(bin_dir, modify_path):
    """Make sure bin_dir is reachable. If it isn't on PATH, append one export
    line to the user's shell rc (rustup-style) unless --no-modify-path."""
    if str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep):
        return True
    line = 'export PATH="$HOME/.local/bin:$PATH"'
    if not modify_path:
        print("  NOTE: {} is not on your PATH (--no-modify-path given). "
              "Add it yourself:\n    {}".format(bin_dir, line))
        return False
    shell = os.path.basename(os.environ.get("SHELL", ""))
    rc_name = {"zsh": ".zshrc", "bash": ".bashrc"}.get(shell, ".profile")
    rc_path = Path.home() / rc_name
    try:
        content = rc_path.read_text()
    except (FileNotFoundError, OSError):
        content = ""
    if line in content:
        print("  path: {} already exports ~/.local/bin — open a new shell "
              "for `governor` to resolve".format(rc_path))
        return False
    with open(rc_path, "a") as f:
        f.write("\n# added by context-governor installer\n{}\n".format(line))
    print("  path: added ~/.local/bin to PATH in {} — open a new shell "
          "(or `exec {}`) for `governor` to resolve".format(rc_path, shell or "sh"))
    return False


def install_launcher(modify_path=True):
    """Put a `governor` command on the PATH so engage/status/run are typeable
    from any project directory (instead of python3 <long path>/governor.py)."""
    bin_dir = Path.home() / ".local" / "bin"
    launcher = bin_dir / "governor"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec python3 "{}" "$@"\n'
                        .format(Path(__file__).resolve()))
    launcher.chmod(0o755)
    print("  launcher: {} -> governor.py".format(launcher))
    return _ensure_on_path(bin_dir, modify_path)


def cmd_install(args):
    if args.cursor or args.all:
        install_cursor(Path.home() / ".cursor" / "hooks.json")
    if args.cursor_project:
        install_cursor(Path(args.cursor_project) / ".cursor" / "hooks.json")
        install_cursor_rule(args.cursor_project)
    if args.claude or args.all or not (args.cursor or args.cursor_project):
        install_claude(Path.home() / ".claude" / "settings.json")
    launcher_on_path = install_launcher(modify_path=not args.no_modify_path)

    cli = "governor" if launcher_on_path else 'python3 "{}"'.format(
        Path(__file__).resolve())
    print("\nDone. Restart Claude Code — hooks load at session start.")
    print("Verify after working a while:  {} status".format(cli))
    print("Chain sessions on a long task: {} engage   (from your project dir)"
          .format(cli))
    print("Config (optional): ~/.context-governor/config.json "
          "(see config.example.json).")


# ------------------------------------------------------------------ main ---

def main():
    parser = argparse.ArgumentParser(prog="context-governor")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("hook")
    pc = sub.add_parser("compact")
    pc.add_argument("--transcript", required=True)
    pc.add_argument("--out", required=True)
    pc.add_argument("--workspace", default=os.getcwd())
    sub.add_parser("status")
    pe = sub.add_parser("engage")
    pe.add_argument("--workspace", default=os.getcwd())
    pe.add_argument("--claude-cmd", default="",
                    help="override the full agent command (default: claude)")
    pe.add_argument("--max-sessions", type=int, default=0)
    pe.add_argument("--auto", action="store_true",
                    help="relaunch the next session without asking")
    pr = sub.add_parser("run")
    pr.add_argument("--workspace", default=os.getcwd())
    pr.add_argument("--task", default="",
                    help="task for the first session (required if the ledger "
                         "is empty)")
    pr.add_argument("--agent-cmd", default="",
                    help="agent CLI template; {prompt} is replaced with the "
                         "quoted prompt, otherwise the prompt is piped to "
                         "stdin (default: autodetect cursor-agent / claude)")
    pr.add_argument("--max-sessions", type=int, default=0)
    pi = sub.add_parser("install")
    pi.add_argument("--claude", action="store_true",
                    help="install Claude Code hooks (the default)")
    pi.add_argument("--cursor", action="store_true",
                    help="install user-level Cursor hooks")
    pi.add_argument("--cursor-project", metavar="DIR",
                    help="install Cursor hooks + bootstrap rule in DIR")
    pi.add_argument("--all", action="store_true",
                    help="claude + cursor (user-level)")
    pi.add_argument("--no-modify-path", action="store_true",
                    help="don't append ~/.local/bin to your shell rc")
    args, unknown = parser.parse_known_args()
    if hasattr(args, "claude_args"):
        args.claude_args = (args.claude_args or []) + unknown
    elif args.cmd == "engage":
        args.claude_args = unknown

    if args.cmd == "hook":
        cmd_hook()
    elif args.cmd == "compact":
        cmd_compact(args)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "engage":
        cmd_engage(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "install":
        cmd_install(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
