#!/usr/bin/env python3
"""Tests for context-governor. Stdlib only: python3 tests/test_governor.py"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from shlex import quote as shlex_quote

ROOT = Path(__file__).resolve().parent.parent
GOVERNOR = ROOT / "governor.py"


def make_claude_transcript(path, context_tokens):
    """Write a minimal Claude Code transcript whose last assistant message
    reports the given context size."""
    lines = [
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Build stage 4C of the plan"},
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working on it."},
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "pnpm test"}},
                ],
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": context_tokens - 103,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        }),
    ]
    Path(path).write_text("\n".join(lines) + "\n")


def make_cursor_transcript(root, conversation_id, n_bytes):
    """Write a Cursor-style transcript of roughly n_bytes under a fake
    ~/.cursor/projects layout; return the projects root."""
    d = Path(root) / "projA" / "agent-transcripts" / conversation_id
    d.mkdir(parents=True)
    line = json.dumps({
        "role": "assistant",
        "message": {"content": [
            {"type": "text", "text": "x" * 200},
            {"type": "tool_use", "name": "Read",
             "input": {"path": "/tmp/some/file.ts"}},
        ]},
    })
    reps = max(1, n_bytes // (len(line) + 1))
    (d / (conversation_id + ".jsonl")).write_text("\n".join([line] * reps) + "\n")
    return str(Path(root))


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir()
        self.env = dict(
            os.environ,
            CG_STATE_DIR=str(Path(self.tmp.name) / "state"),
            CG_CONFIG=str(Path(self.tmp.name) / "config.json"),
            CG_CURSOR_PROJECTS=str(Path(self.tmp.name) / "cursor-projects"),
        )
        self.write_config({})

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, overrides):
        cfg = {"warn_pct": 50, "handoff_pct": 60, "auto_compact": False}
        cfg.update(overrides)
        Path(self.env["CG_CONFIG"]).write_text(json.dumps(cfg))

    def run_hook(self, payload):
        result = subprocess.run(
            [sys.executable, str(GOVERNOR), "hook"],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self.env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    # ---- Claude Code -----------------------------------------------------

    def claude_payload(self, transcript, session="sess-1", event="PostToolUse"):
        return {
            "session_id": session,
            "transcript_path": str(transcript),
            "cwd": str(self.workspace),
            "hook_event_name": event,
            "tool_name": "Bash",
        }

    def test_claude_quiet_below_warn(self):
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 40000)  # 20% of 200k
        self.assertEqual(self.run_hook(self.claude_payload(t)), {})

    def test_claude_warn_fires_once(self):
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 110000)  # 55%
        out = self.run_hook(self.claude_payload(t))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CONTEXT BUDGET WARNING", ctx)
        self.assertIn("55%", ctx)
        self.assertEqual(self.run_hook(self.claude_payload(t)), {})

    def test_claude_handoff_and_refire(self):
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 130000)  # 65%
        out = self.run_hook(self.claude_payload(t))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CONTEXT BUDGET EXCEEDED", ctx)
        self.assertIn("handoff/LEDGER.md", ctx)
        # same pct again -> quiet
        self.assertEqual(self.run_hook(self.claude_payload(t)), {})
        # +4pct -> refires
        make_claude_transcript(t, 138000)  # 69%
        out = self.run_hook(self.claude_payload(t))
        self.assertIn("CONTEXT BUDGET EXCEEDED",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_claude_session_start_bootstraps_from_ledger(self):
        ledger = self.workspace / "handoff" / "LEDGER.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "# Ledger\n\nintro\n\n---\n## 2026-07-07 — Stage 4C\n"
            "**Next step (exact):** implement cliqq doctor\n"
        )
        out = self.run_hook(self.claude_payload(
            "/nonexistent", event="SessionStart"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("RESUME PROTOCOL", ctx)
        self.assertIn("implement cliqq doctor", ctx)

    def test_claude_session_start_template_only_ledger_is_quiet(self):
        # A freshly scaffolded ledger contains `---` inside its documented
        # entry-format block; that must not be mistaken for a real entry.
        ledger = self.workspace / "handoff" / "LEDGER.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "# Ledger\n\n## Entry format\n\n```markdown\n---\n## <date>\n"
            "**Next step (exact):** <placeholder>\n```\n\n"
            "<!-- Entries below. Newest at the bottom. -->\n"
        )
        out = self.run_hook(self.claude_payload(
            "/nonexistent", event="SessionStart"))
        self.assertEqual(out, {})

    def test_claude_session_start_entry_below_marker(self):
        ledger = self.workspace / "handoff" / "LEDGER.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "# Ledger\n\n```markdown\n---\n## <template>\n```\n\n"
            "<!-- Entries below. Newest at the bottom. -->\n\n---\n"
            "## 2026-07-07 — real entry\n**Next step (exact):** ship stage 5\n"
        )
        out = self.run_hook(self.claude_payload(
            "/nonexistent", event="SessionStart"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ship stage 5", ctx)
        self.assertNotIn("<template>", ctx)

    def test_claude_session_start_no_ledger(self):
        out = self.run_hook(self.claude_payload(
            "/nonexistent", event="SessionStart"))
        self.assertEqual(out, {})

    # ---- Cursor ----------------------------------------------------------

    def cursor_payload(self, conversation_id):
        return {
            "conversation_id": conversation_id,
            "workspace_roots": [str(self.workspace)],
            "tool_name": "Shell",
        }

    def test_cursor_quiet_below_warn(self):
        make_cursor_transcript(self.env["CG_CURSOR_PROJECTS"], "conv-a",
                               100 * 1024)  # ~25k tokens = 12%
        self.assertEqual(self.run_hook(self.cursor_payload("conv-a")), {})

    def test_cursor_handoff(self):
        make_cursor_transcript(self.env["CG_CURSOR_PROJECTS"], "conv-b",
                               520 * 1024)  # ~133k tokens = 66%
        out = self.run_hook(self.cursor_payload("conv-b"))
        self.assertIn("CONTEXT BUDGET EXCEEDED", out["additional_context"])

    def test_cursor_real_payload_shape(self):
        # Real Cursor payloads include transcript_path, session_id, and a
        # camelCase hook_event_name — they must NOT be routed to the Claude
        # branch (regression: transcript_path alone used to mean "claude").
        root = self.env["CG_CURSOR_PROJECTS"]
        make_cursor_transcript(root, "conv-real", 520 * 1024)  # ~66%
        transcript = str(Path(root) / "projA" / "agent-transcripts" /
                         "conv-real" / "conv-real.jsonl")
        out = self.run_hook({
            "conversation_id": "conv-real",
            "session_id": "conv-real",
            "transcript_path": transcript,
            "hook_event_name": "postToolUse",
            "cursor_version": "2.4.0",
            "tool_name": "Shell",
            "workspace_roots": [str(self.workspace)],
        })
        self.assertIn("CONTEXT BUDGET EXCEEDED", out["additional_context"])

    def test_cursor_missing_transcript(self):
        self.assertEqual(self.run_hook(self.cursor_payload("conv-none")), {})

    def test_garbage_stdin(self):
        result = subprocess.run(
            [sys.executable, str(GOVERNOR), "hook"],
            input="not json", capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    # ---- auto-compact spawn ---------------------------------------------

    def test_handoff_spawns_snapshot(self):
        self.write_config({"auto_compact": True})
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 130000)
        out = self.run_hook(self.claude_payload(t, session="snapsess"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("state snapshot", ctx)
        snap = self.workspace / "handoff" / "state-snapsess.md"
        for _ in range(50):
            if snap.exists():
                break
            import time
            time.sleep(0.1)
        self.assertTrue(snap.exists(), "compactor did not write snapshot")
        body = snap.read_text()
        self.assertIn("Build stage 4C of the plan", body)
        self.assertIn("pnpm test", body)

    # ---- compact command -------------------------------------------------

    def test_compact_structural_digest(self):
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 50000)
        out_file = Path(self.tmp.name) / "snap.md"
        result = subprocess.run(
            [sys.executable, str(GOVERNOR), "compact", "--transcript", str(t),
             "--out", str(out_file), "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = out_file.read_text()
        self.assertIn("protected head", body)
        self.assertIn("Build stage 4C of the plan", body)
        # ledger scaffolded in workspace
        self.assertTrue((self.workspace / "handoff" / "LEDGER.md").exists())

    # ---- autonomous run ---------------------------------------------------

    def fake_agent(self, script_body):
        """Create a fake agent CLI: a python script that receives the prompt
        on stdin and can append ledger entries in the workspace."""
        path = Path(self.tmp.name) / "fake_agent.py"
        path.write_text(script_body)
        return "{} {}".format(shlex_quote(sys.executable), shlex_quote(str(path)))

    def run_driver(self, agent_cmd, task="build the thing", max_sessions=5):
        return subprocess.run(
            [sys.executable, str(GOVERNOR), "run",
             "--workspace", str(self.workspace),
             "--task", task, "--agent-cmd", agent_cmd,
             "--max-sessions", str(max_sessions)],
            capture_output=True, text=True, env=self.env, timeout=60,
        )

    def test_run_loops_until_done(self):
        agent = self.fake_agent(
            "import sys, pathlib\n"
            "prompt = sys.stdin.read()\n"
            "assert 'PROTOCOL' in prompt\n"
            "ledger = pathlib.Path('handoff/LEDGER.md')\n"
            "n = ledger.read_text().count('## session')\n"
            "step = 'DONE' if n >= 2 else 'do slice %d' % (n + 2)\n"
            "ledger.open('a').write('\\n---\\n## session %d\\n"
            "**Next step (exact):** %s\\n' % (n + 1, step))\n"
        )
        result = self.run_driver(agent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Plan marked DONE", result.stdout)
        ledger = (self.workspace / "handoff" / "LEDGER.md").read_text()
        self.assertEqual(ledger.count("## session"), 3)

    def test_run_resumes_from_existing_entry(self):
        ledger = self.workspace / "handoff" / "LEDGER.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "# L\n\n<!-- Entries below. -->\n\n---\n## prior\n"
            "**Next step (exact):** finish stage 9\n"
        )
        agent = self.fake_agent(
            "import sys, pathlib\n"
            "prompt = sys.stdin.read()\n"
            "assert 'finish stage 9' in prompt, 'resume entry not in prompt'\n"
            "pathlib.Path('handoff/LEDGER.md').open('a').write(\n"
            "    '\\n---\\n## after\\n**Next step (exact):** DONE\\n')\n"
        )
        result = self.run_driver(agent, task="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_run_stops_when_agent_skips_ledger(self):
        agent = self.fake_agent("import sys; sys.stdin.read()\n")
        result = self.run_driver(agent)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no new ledger entry", result.stdout)

    def test_run_already_done_is_noop(self):
        ledger = self.workspace / "handoff" / "LEDGER.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "# L\n\n<!-- Entries below. -->\n\n---\n## final\n"
            "**Next step (exact):** DONE\n"
        )
        agent = self.fake_agent("raise SystemExit('must not be called')\n")
        result = self.run_driver(agent, task="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DONE", result.stdout)

    def test_compact_with_summarizer_cmd(self):
        self.write_config({"summarizer_cmd": "echo 'MODEL SUMMARY OK'"})
        t = Path(self.tmp.name) / "t.jsonl"
        make_claude_transcript(t, 50000)
        out_file = Path(self.tmp.name) / "snap2.md"
        subprocess.run(
            [sys.executable, str(GOVERNOR), "compact", "--transcript", str(t),
             "--out", str(out_file), "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=self.env, check=True,
        )
        self.assertIn("MODEL SUMMARY OK", out_file.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
