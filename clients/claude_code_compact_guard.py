#!/usr/bin/env python3
"""Claude Code PreCompact hook: steer what a compaction keeps, and let a session hold auto-compact off for a while.

Auto-compact runs near 200k tokens (autoCompactWindow in settings.json). Exit 0 with stdout adds custom instructions
to the summary; exit 2 blocks the compaction and the session continues uncompacted.

Hold auto-compact off (during a delicate debugging run, say) by creating the flag file:
  python compact_guard.py hold      # or: touch ~/.claude/hooks/state/nocompact
  python compact_guard.py release
A flag older than HOLD_HOURS is ignored, so a forgotten hold cannot bring back 1M-token sessions.
Manual /compact is never blocked. Stdlib only.
"""

import json
import pathlib
import sys
import time

FLAG = pathlib.Path.home() / ".claude" / "hooks" / "state" / "nocompact"
HOLD_HOURS = 3

INSTRUCTIONS = """Keep, in this order of importance:
1. The task the user asked for and where it stands now: what is done, what is in progress, the exact next step.
2. Decisions and changes of direction, with the reason. If the direction changed during the session, keep only the
current direction and say in one line what it replaced, so the old plan is not resumed.
3. What was tried and failed, with the exact error text, and the current hypothesis, so nothing is retried blindly.
4. Files changed or created (paths), commands that build, run or test the work, ports and URLs in use.
5. If NOTES.md exists in the project, say so: it is the working journal to read and keep updated.
Drop file contents and long tool output that can be read again."""


def _held() -> bool:
    try:
        return time.time() - FLAG.stat().st_mtime < HOLD_HOURS * 3600
    except OSError:
        return False


def main() -> None:
    if sys.argv[1:] == ["hold"]:
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.touch()
        print(f"Auto-compact held for up to {HOLD_HOURS} h.")
        return
    if sys.argv[1:] == ["release"]:
        FLAG.unlink(missing_ok=True)
        print("Auto-compact released.")
        return
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        event = {}
    if event.get("trigger") == "auto" and _held():
        print(f"auto-compact held by {FLAG} (release: python compact_guard.py release)", file=sys.stderr)
        sys.exit(2)
    print(INSTRUCTIONS)


if __name__ == "__main__":
    main()
