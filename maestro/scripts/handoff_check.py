#!/usr/bin/env python3
"""Maestro handoff validator — a handoff is only useful if it is true.

The whole point of a handoff is that a fresh session can trust it blind. A
pointer that does not resolve costs the next conductor a hunt on its cheapest
context; a pasted transcript costs it tokens on every turn for the rest of the
session. So the contract is mechanical and this script enforces it:

  - required sections: ## Goal, ## Decisions, ## World state, ## Next
  - hard cap 120 lines — a handoff is pointers and decisions, not a transcript
  - payload smell: no fenced block over 20 lines, no more than 40 fenced
    lines in total — pointers, not payloads
  - every backticked path must exist on disk (`:line` suffixes stripped)
  - every `branch: <name>` under World state must resolve in git
  - every `worktree: <path>` under World state must be a directory

Usage:
  python3 handoff_check.py [path-to-HANDOFF.md]

Default path is .claude/maestro/HANDOFF.md under the current directory.
Prints HANDOFF PASS or HANDOFF FAIL with one reason per line; exit 1 on fail.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_LINES = 120
MAX_FENCE_BLOCK = 20
MAX_FENCE_TOTAL = 40
REQUIRED = ("## Goal", "## Decisions", "## World state", "## Next")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
FIELD = re.compile(r"^\s*[-*]?\s*(branch|worktree):\s*`?([^`\s]+)`?", re.I)
# A backticked token worth checking: path-shaped, no spaces, no markup.
TOKEN = re.compile(r"`([^`\n]+)`")
PATHISH = re.compile(r"^[A-Za-z0-9_.@/~+-]+$")


def base_of(handoff):
    """Paths in the handoff resolve against the workspace, not this script."""
    parts = handoff.resolve().parts
    if len(parts) >= 4 and parts[-3:-1] == (".claude", "maestro"):
        return Path(*parts[:-3])
    return Path.cwd()


def split_fences(lines):
    """(prose lines, fenced blocks as lists of lines)."""
    prose, blocks, cur = [], [], None
    for ln in lines:
        if ln.strip().startswith("```"):
            if cur is None:
                cur = []
            else:
                blocks.append(cur)
                cur = None
            continue
        (prose if cur is None else cur).append(ln)
    if cur is not None:
        blocks.append(cur)          # unterminated fence still counts
    return prose, blocks


def check_paths(prose, base, fail):
    for token in TOKEN.findall("\n".join(prose)):
        t = token.strip()
        if " " in t or "://" in t or "<" in t or "*" in t or "?" in t:
            continue
        bare = LINE_SUFFIX.sub("", t)
        if not PATHISH.match(bare):
            continue
        if "/" not in bare and "." not in os.path.basename(bare):
            continue                # a word in backticks, not a path
        p = Path(os.path.expanduser(bare))
        if not (p.is_absolute() and p.exists()) and not (base / bare).exists():
            fail(f"path does not exist: {t}")


def check_fields(lines, base, fail):
    for ln in lines:
        m = FIELD.match(ln)
        if not m:
            continue
        kind, val = m.group(1).lower(), m.group(2)
        if "<" in val:
            fail(f"{kind} is a placeholder, not a value: {val}")
        elif kind == "worktree":
            p = Path(os.path.expanduser(val))
            if not (p.is_absolute() and p.is_dir()) and not (base / val).is_dir():
                fail(f"worktree does not exist: {val}")
        else:
            r = subprocess.run(
                ["git", "-C", str(base), "rev-parse", "--verify", "--quiet",
                 f"{val}^{{commit}}"],
                capture_output=True, text=True)
            if r.returncode != 0:
                fail(f"branch does not resolve in git: {val}")


def main(argv):
    handoff = Path(argv[1]) if len(argv) > 1 else \
        Path.cwd() / ".claude" / "maestro" / "HANDOFF.md"
    reasons = []
    fail = reasons.append

    try:
        text = handoff.read_text()
    except OSError:
        print(f"HANDOFF FAIL\n- no handoff at {handoff}")
        return 1

    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        fail(f"{len(lines)} lines — cap is {MAX_LINES}; cut prose, keep pointers")
    for sec in REQUIRED:
        if not any(ln.strip().startswith(sec) for ln in lines):
            fail(f"missing required section: {sec}")

    prose, blocks = split_fences(lines)
    fenced = sum(len(b) for b in blocks)
    for b in blocks:
        if len(b) > MAX_FENCE_BLOCK:
            fail(f"a fenced block is {len(b)} lines — cap is {MAX_FENCE_BLOCK}; "
                 "point at the file instead")
    if fenced > MAX_FENCE_TOTAL:
        fail(f"{fenced} fenced lines in total — cap is {MAX_FENCE_TOTAL}")

    base = base_of(handoff)
    check_paths(prose, base, fail)
    check_fields(lines, base, fail)

    if reasons:
        print("HANDOFF FAIL")
        for r in reasons:
            print(f"- {r}")
        return 1
    print(f"HANDOFF PASS — {len(lines)} lines, {handoff}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
