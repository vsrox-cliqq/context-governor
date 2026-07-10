#!/usr/bin/env python3
"""Installer for context-governor.

Merges the governor hook into Cursor and/or Claude Code hook configs.
Never clobbers existing hooks: reads, merges, writes with a timestamped
backup next to the original.

Usage:
  python3 install.py --cursor                 # user-level ~/.cursor/hooks.json
  python3 install.py --cursor-project DIR     # DIR/.cursor/hooks.json
  python3 install.py --claude                 # ~/.claude/settings.json
  python3 install.py --all                    # cursor (user) + claude
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

GOVERNOR = Path(__file__).resolve().parent / "governor.py"
HOOK_CMD = 'python3 "{}" hook'.format(GOVERNOR)

RULE_TEMPLATE = Path(__file__).resolve().parent / "templates" / "session-handoff.mdc"


def backup(path):
    if path.exists():
        dest = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(path, dest)
        print("  backup: {}".format(dest))


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        print("ERROR: {} exists but is not valid JSON; fix it first.".format(path))
        sys.exit(1)


def install_cursor(hooks_file):
    data = load_json(hooks_file)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("postToolUse", [])
    if any(e.get("command") == HOOK_CMD for e in entries):
        print("  cursor: already installed in {}".format(hooks_file))
        return
    backup(hooks_file)
    entries.append({"command": HOOK_CMD, "timeout": 10})
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(data, indent=2) + "\n")
    print("  cursor: installed postToolUse hook in {}".format(hooks_file))


def install_cursor_rule(project_dir):
    """Copy the session-bootstrap rule into the project (Cursor has no
    hook-based context injection at session start, so a rule does it)."""
    rules_dir = Path(project_dir) / ".cursor" / "rules"
    dest = rules_dir / "session-handoff.mdc"
    if dest.exists():
        print("  cursor: rule already present at {}".format(dest))
        return
    rules_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RULE_TEMPLATE, dest)
    print("  cursor: bootstrap rule installed at {}".format(dest))


def install_launcher():
    """Put a `governor` command on the PATH so engage/status/run are typeable
    from any project directory (instead of python3 <long path>/governor.py)."""
    bin_dir = Path.home() / ".local" / "bin"
    launcher = bin_dir / "governor"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec python3 "{}" "$@"\n'.format(GOVERNOR))
    launcher.chmod(0o755)
    print("  launcher: {} -> governor.py".format(launcher))
    on_path = str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep)
    if not on_path:
        print("  NOTE: {} is not on your PATH; add it to use the `governor` "
              "command:".format(bin_dir))
        print("    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc "
              "&& exec zsh")
    return on_path


def install_claude(settings_file):
    data = load_json(settings_file)
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
        backup(settings_file)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(data, indent=2) + "\n")
        print("  claude: wrote {}".format(settings_file))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cursor", action="store_true")
    parser.add_argument("--cursor-project", metavar="DIR")
    parser.add_argument("--claude", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if not any([args.cursor, args.cursor_project, args.claude, args.all]):
        parser.print_help()
        sys.exit(1)

    if args.cursor or args.all:
        install_cursor(Path.home() / ".cursor" / "hooks.json")
    if args.cursor_project:
        install_cursor(Path(args.cursor_project) / ".cursor" / "hooks.json")
        install_cursor_rule(args.cursor_project)
    if args.claude or args.all:
        install_claude(Path.home() / ".claude" / "settings.json")
    launcher_on_path = install_launcher()

    cli = "governor" if launcher_on_path else 'python3 "{}"'.format(GOVERNOR)
    print("\nDone. Restart Cursor / Claude Code — hooks load at session start.")
    print("Verify after working a while:  {} status".format(cli))
    print("Chain sessions on a long task: {} engage   (from your project dir)"
          .format(cli))
    print("Config (optional): ~/.context-governor/config.json (see config.example.json).")


if __name__ == "__main__":
    main()
