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


def handback_rec(tid, message):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "SubagentHandback",
         "input": {"message": message}}]}})


def handback_result(tid):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text",
         "text": '{"success":true,"message":"Report delivered to your caller."}'}]}]}})


def text_rec(text, role="assistant"):
    return json.dumps({"type": role, "message": {"role": role,
                       "content": [{"type": "text", "text": text}]}})


def handback_case():
    """A background agent reports through the SubagentHandback tool.

    Its final turn ends on a tool call, so SubagentStop carries no
    last_assistant_message and the transcript's newest prose is mid-work
    narration. The harness already delivered the report; that is not a loss,
    and the narration must never be re-sent as "its real report".
    """
    print("\n=== handback is a delivery ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "cccc9993-0000-0000-0000-00000000000c")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(2, names=["gp-handback", "gp-stale"])
    report = "Lane EYE is finished. Six commits on lane/eye; 201 tests pass."
    fresh = write_lines(ws / "tr" / "handback.jsonl", [
        text_rec("All gates green. Now I'll true up the spec against what was built."),
        handback_rec("toolu_hb1", report),
        handback_result("toolu_hb1"),
    ])
    # Resumed after an earlier handback; this run stopped without handing back.
    stale = write_lines(ws / "tr" / "stale.jsonl", [
        handback_rec("toolu_hb0", "Round 1 is done."),
        handback_result("toolu_hb0"),
        text_rec("Fix the review items.", role="user"),
        text_rec("Reading the review notes"),
    ])
    time.sleep(2.5)
    c.tool_call()
    for name, tr in (("gp-handback", fresh), ("gp-stale", stale)):
        c.fire(hook_event_name="SubagentStop", agent_id=ids[name], agent_type=name,
               agent_transcript_path=str(tr))
    time.sleep(5.5)
    c.tool_call()
    nodes = {v.get("name"): v for v in c.state()["nodes"].values()}
    hb = nodes.get("gp-handback") or {}
    check("handback counts as delivered", hb.get("report_status") == "delivered",
          f"got {hb.get('report_status')}")
    check("the handback message is the report", report in (hb.get("result") or ""),
          repr((hb.get("result") or "")[:80]))
    flagged = [m for m in c.msgs if "gp-handback" in m
               and ("REPORT RECOVERED" in m or "REPORT NOT DELIVERED" in m)]
    check("no false delivery alarm", not flagged, flagged[0][:120] if flagged else "")
    st = nodes.get("gp-stale") or {}
    check("an earlier run's handback is not this run's delivery",
          st.get("report_status") != "delivered", f"got {st.get('report_status')}")


def collide_case():
    """Only two writers can conflict at merge; shared reads are the design.

    Every builder reads the same contract file by design, so counting reads
    turned each contract-first wave into "serialize them or the merge
    conflicts" for a file nobody edited.
    """
    print("\n=== collisions count writers only ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "dddd9994-0000-0000-0000-00000000000d")
    c.fire(hook_event_name="SessionStart", source="startup")
    names = ["rev-a", "rev-b", "impl-a", "impl-b"]
    ids = c.dispatch(4, kind="builder", names=names)
    time.sleep(2.5)
    for name, tool, path in (("rev-a", "Read", "/w/reviewer-contract.md"),
                             ("rev-b", "Read", "/w/reviewer-contract.md"),
                             ("impl-a", "Edit", "/w/WaveTabs.tsx"),
                             ("impl-b", "Write", "/w/WaveTabs.tsx")):
        c.fire(hook_event_name="PostToolUse", tool_name=tool, agent_id=ids[name],
               agent_type=name, tool_input={"file_path": path},
               tool_response={"ok": True})
    c.tool_call()
    time.sleep(5.5)
    c.tool_call()
    said = "\n".join(c.msgs)
    check("two readers of one file are not a collision",
          "reviewer-contract.md" not in said, said[-200:])
    check("two writers of one file still are", "both live on WaveTabs.tsx" in said,
          said[-200:])


def workflow_case():
    """A Workflow's agents return to the script, never to the conductor.

    They end on a StructuredOutput call with no last_assistant_message, and
    the conductor hears from the Workflow's own completion notice. Flagging
    each one as REPORT NOT DELIVERED asked for a resend that cannot exist.
    """
    print("\n=== workflow agents are not a lost report ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "eeee9995-0000-0000-0000-00000000000e")
    c.fire(hook_event_name="SessionStart", source="startup")
    tr = write_lines(ws / "tr" / "wf.jsonl", [json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_so1", "name": "StructuredOutput",
             "input": {"findings": []}}]}})])
    c.fire(hook_event_name="SubagentStart", agent_id="awf000000001",
           agent_type="workflow-subagent")
    time.sleep(2.5)
    c.tool_call()
    c.fire(hook_event_name="SubagentStop", agent_id="awf000000001",
           agent_type="workflow-subagent", agent_transcript_path=str(tr))
    time.sleep(5.5)
    c.tool_call()
    flagged = [m for m in c.msgs if "workflow-subagent" in m
               and ("REPORT NOT DELIVERED" in m or "REPORT RECOVERED" in m)]
    check("no delivery alarm for a workflow agent", not flagged,
          flagged[0][:120] if flagged else "")


def main():
    tokens_case()
    digest_case()
    handoff_case()
    handback_case()
    collide_case()
    workflow_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
