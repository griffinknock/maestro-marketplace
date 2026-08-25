#!/usr/bin/env python3
"""Focused checks for the 0.4.0 surface: token accounting, digest recovery,
and the handoff gate. Complements replay.py, which owns the messaging cases.

    python3 tests/units.py

Exit code is 0 when every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import Conductor, workspace, PLUGIN, ENV, IDLE_FRAME

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def usage_rec(rid, i=0, cw=0, cr=0, out=0, sidechain=False, text="…"):
    return json.dumps({
        "type": "assistant", "requestId": rid, "isSidechain": sidechain,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": i,
                              "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": cr,
                              "output_tokens": out}}})


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def tokens_case():
    """SubagentStop sums the agent transcript, deduped by requestId."""
    print("\n=== token accounting — subagent ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "aaaa9991-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, names=["scout-tok"])
    tr = write_lines(ws / "tr" / "tok.jsonl", [
        usage_rec("R1", i=2, cw=100, cr=50, out=10),
        usage_rec("R1", i=2, cw=100, cr=50, out=10),      # same request, split record
        json.dumps({"type": "user", "message": {"role": "user"}}),
        usage_rec("R2", i=1, cw=5, cr=200, out=20),
        json.dumps({"type": "assistant", "message": {"role": "assistant"}}),
    ])
    c.finish(ids["scout-tok"], "scout-tok", transcript=tr)
    n = next(v for v in c.state()["nodes"].values() if v.get("name") == "scout-tok")
    check("tokens = in + cache write + out, deduped", n.get("tokens") == 138,
          f"got {n.get('tokens')}")
    check("cached reads kept in the breakdown", (n.get("usage") or {}).get("cr") == 250,
          f"got {(n.get('usage') or {}).get('cr')}")
    check("two requests counted, not three", (n.get("usage") or {}).get("reqs") == 2)

    # The conductor's own spend, sidechains excluded.
    print("=== token accounting — conductor ===")
    main_tr = write_lines(ws / "tr" / "main.jsonl", [
        usage_rec("R3", i=10, cw=1000, cr=5000, out=200),
        usage_rec("R4", i=99, cw=99, cr=99, out=99, sidechain=True),
    ])
    c.fire(hook_event_name="Stop", transcript_path=str(main_tr))
    root = c.state()["nodes"]["root"]
    check("root tokens exclude sidechain records", root.get("tokens") == 1210,
          f"got {root.get('tokens')}")


def digest_case():
    """A recovered report arrives as a digest plus a pointer, not in full."""
    print("\n=== recovered report is a digest ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "bbbb9992-0000-0000-0000-00000000000b")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, names=["scout-long"])
    long_report = "```\nRETURN:\n  answer: line one of many\n" + \
        "\n".join(f"  detail {i}: something that happened" for i in range(40)) + "\n```"
    tr = write_lines(ws / "tr" / "long.jsonl", [json.dumps(
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": long_report}]}})])
    time.sleep(2.5)
    c.tool_call()
    c.finish(ids["scout-long"], "scout-long", last=IDLE_FRAME, transcript=tr)
    time.sleep(5.5)
    c.tool_call()
    msg = next((m for m in c.msgs if "REPORT RECOVERED" in m), "")
    check("swallowed report still surfaces", bool(msg))
    if msg:
        check("digest is bounded", len(msg.splitlines()) <= 25,
              f"{len(msg.splitlines())} lines")
        check("pointer to the durable copy", "on disk" in msg and "/reports/" in msg)


def handoff_case():
    """handoff_check.py passes a true handoff and names every lie in a false one."""
    print("\n=== handoff gate ===")
    script = PLUGIN / "handoff_check.py"
    ws = Path(tempfile.mkdtemp(prefix="maestro-handoff-"))
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"], check=True)
    (ws / "src").mkdir()
    (ws / "src" / "auth.ts").write_text("export const x = 1\n")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)

    good = write_lines(ws / ".claude" / "maestro" / "HANDOFF.md", [
        "# Handoff — token column",
        "## Goal",
        "Ship the token column.",
        "## Decisions",
        "- headline = in + cw + out — cache reads are 10x cheaper",
        "## World state",
        "- branch: main",
        "- key files: `src/auth.ts:1` — the entry point",
        "## Open questions",
        "- none",
        "## Next",
        "1. Merge it.",
    ])
    r = subprocess.run([sys.executable, str(script), str(good)],
                       capture_output=True, text=True, cwd=ws)
    check("valid handoff passes", r.returncode == 0 and "HANDOFF PASS" in r.stdout,
          r.stdout.strip())

    bad = write_lines(ws / ".claude" / "maestro" / "HANDOFF.md", [
        "# Handoff — broken",
        "## Goal",
        "x",
        "## World state",
        "- branch: feat/does-not-exist",
        "- worktree: .claude/worktrees/ghost",
        "- key files: `src/missing.ts:9`",
        "```",
        *(f"pasted transcript line {i}" for i in range(30)),
        "```",
        "## Next",
        "1. n/a",
    ])
    r = subprocess.run([sys.executable, str(script), str(bad)],
                       capture_output=True, text=True, cwd=ws)
    out = r.stdout
    check("invalid handoff fails", r.returncode == 1 and "HANDOFF FAIL" in out)
    check("missing section named", "## Decisions" in out)
    check("dead path named", "src/missing.ts" in out)
    check("dead branch named", "feat/does-not-exist" in out)
    check("dead worktree named", "ghost" in out)
    check("oversize fence named", "fenced block" in out)
    shutil.rmtree(ws, ignore_errors=True)


def main():
    tokens_case()
    digest_case()
    handoff_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
