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
from replay import Conductor, workspace, PLUGIN, ENV, IDLE_FRAME, report_text

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

    # Multi-repo workspace: the root is not a repo, its children are.
    root = Path(tempfile.mkdtemp(prefix="maestro-handoff-multi-"))
    for name in ("alpha", "beta"):
        repo = root / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        (repo / "f.txt").write_text(name)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com",
                        "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(root / "beta"), "branch", "feat/only-beta"], check=True)

    def multi(branch):
        h = write_lines(root / ".claude" / "maestro" / "HANDOFF.md", [
            "# Handoff — multi", "## Goal", "x", "## Decisions", "- y",
            "## World state", f"- branch: {branch}", "## Next", "1. go",
        ])
        return subprocess.run([sys.executable, str(script), str(h)],
                              capture_output=True, text=True, cwd=root)

    r = multi("feat/only-beta")
    check("non-repo root: branch in second child repo passes",
          r.returncode == 0 and "HANDOFF PASS" in r.stdout, r.stdout.strip())
    r = multi("feat/nowhere")
    check("non-repo root: branch nowhere fails naming checked repos",
          r.returncode == 1 and "feat/nowhere" in r.stdout
          and "alpha" in r.stdout and "beta" in r.stdout, r.stdout.strip())
    shutil.rmtree(root, ignore_errors=True)


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
    check("two writers of one file still are", "both live on" in said and "WaveTabs.tsx" in said,
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


def live_nodes(state):
    return [n for n in state["nodes"].values()
            if n["id"] != "root" and n.get("status") in ("running", "spawning")]


def worktree_ledger_case():
    """A builder's hooks land in the conductor's ledger, not its worktree's."""
    print("\n=== one ledger per session, worktrees included ===")
    ws = workspace()
    (ws / ".git").mkdir()
    c = Conductor(PLUGIN, ws, "aaaa9994-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, kind="builder", names=["builder-wt"])
    wt = ws / ".claude" / "worktrees" / "agent-x1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere\n")      # a worktree's .git is a file
    c.fire(hook_event_name="PostToolUse", tool_name="Edit", agent_id=ids["builder-wt"],
           agent_type="builder-wt", cwd=str(wt), tool_input={"file_path": str(wt / "a.py")},
           tool_response={"ok": True})
    c.fire(hook_event_name="SubagentStop", agent_id=ids["builder-wt"], agent_type="builder-wt",
           cwd=str(wt), last_assistant_message=report_text("builder-wt"))
    check("no private ledger inside the worktree",
          not (wt / ".claude" / "maestro").exists())
    n = c.state()["nodes"].get(ids["builder-wt"], {})
    check("the builder's edit and stop reached the conductor's ledger",
          n.get("status") == "done" and n.get("tools", {}).get("Edit") == 1, n)


def phantom_case():
    """A stop for an agent this ledger never saw start is not adopted as running."""
    print("\n=== no phantom agents from unmatched stops ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "aaaa9995-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    wt = ws / ".claude" / "worktrees" / "big-pulls"
    wt.mkdir(parents=True)
    for i in range(3):
        c.fire(hook_event_name="SubagentStop", agent_id=f"a1e4b5d634b7{i:05d}",
               cwd=str(wt), last_assistant_message="done")
    c.tool_call()
    check("nothing counted as running", live_nodes(c.state()) == [], live_nodes(c.state()))
    check("no message about them", not c.msgs, c.msgs)


def age_node(c, aid, seconds):
    sf = c.dir / "state.json"
    st = json.loads(sf.read_text())
    now = time.time()
    st["nodes"][aid]["last_activity"] = now - seconds
    st["nodes"][aid]["started"] = now - seconds - 10
    sf.write_text(json.dumps(st))
    return st


def stall_case():
    """Liveness counts transcript writes, not just tool calls."""
    print("\n=== stall: transcript writes are signs of life ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "aaaa9996-0000-0000-0000-00000000000a")
    session_tr = ws / "projects" / f"{c.sid}.jsonl"
    session_tr.parent.mkdir(parents=True)
    session_tr.write_text("")
    c.fire(hook_event_name="SessionStart", source="startup",
           transcript_path=str(session_tr))
    ids = c.dispatch(1, kind="surgeon", names=["surgeon-big"])
    aid = ids["surgeon-big"]
    agent_tr = session_tr.with_suffix("") / "subagents" / f"agent-{aid}.jsonl"
    agent_tr.parent.mkdir(parents=True)
    agent_tr.write_text("{}\n")                      # written just now
    age_node(c, aid, 700)                            # past stall (600), before reap (900)
    c.tool_call()
    check("a growing transcript is not a stall",
          not any("no activity" in m for m in c.msgs), c.msgs)

    old = time.time() - 700
    os.utime(agent_tr, (old, old))
    age_node(c, aid, 700)
    c.tool_call()
    c.tool_call()
    stalls = [m for m in c.msgs if "no activity" in m]
    check("a silent agent is reported, once", len(stalls) == 1, c.msgs)


def quiet_case():
    """The retired coaching nudges stay silent."""
    print("\n=== no serial or inline coaching ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "aaaa9997-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    for i in range(3):                                # three solo dispatches
        ids = c.dispatch(1, names=[f"scout-q{i}"])
        time.sleep(2.2)
        c.tool_call()
        c.finish(ids[f"scout-q{i}"], f"scout-q{i}")
    for _ in range(10):                               # then work by hand
        c.tool_call()
    check("nothing said", not c.msgs, c.msgs)


def conductor_in_worktree_case():
    """A conductor running inside a worktree keeps its ledger there, where the
    board, notify and /tree look — and its builders' hooks still reach it."""
    print("\n=== conductor inside a worktree ===")
    ws = workspace()
    (ws / ".git").mkdir()
    feat = ws / ".claude" / "worktrees" / "feat"
    feat.mkdir(parents=True)
    (feat / ".git").write_text("gitdir: elsewhere\n")
    c = Conductor(PLUGIN, feat, "aaaa9998-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, kind="builder", names=["builder-y"])
    wt = ws / ".claude" / "worktrees" / "agent-y"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere\n")
    c.fire(hook_event_name="SubagentStop", agent_id=ids["builder-y"], agent_type="builder-y",
           cwd=str(wt), last_assistant_message=report_text("builder-y"))
    check("ledger and pointer sit in the conductor's worktree",
          (feat / ".claude" / "maestro" / "current").is_file() and c.dir.is_dir())
    check("the sibling worktree's stop reached it",
          c.state()["nodes"].get(ids["builder-y"], {}).get("status") == "done")
    check("no ledger at the main root or in the builder's worktree",
          not (ws / ".claude" / "maestro").exists() and not (wt / ".claude" / "maestro").exists())


def cd_up_case():
    """A conductor command run one directory up does not move the ledger."""
    print("\n=== a cd .. does not move the ledger ===")
    ws = workspace()
    repo = ws / "repo"
    (repo / ".git").mkdir(parents=True)
    c = Conductor(PLUGIN, repo, "aaaa9999-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, names=["scout-up"])
    c.fire(hook_event_name="PostToolUse", tool_name="Bash", cwd=str(ws),
           tool_input={"command": "ls"}, tool_response={"stdout": ""})
    c.finish(ids["scout-up"], "scout-up")
    check("no second ledger above the repo", not (ws / ".claude" / "maestro").exists())
    check("the stop still landed", c.state()["nodes"][ids["scout-up"]].get("status") == "done")
    other = ws / "other"
    (other / ".git").mkdir(parents=True)
    c.fire(hook_event_name="SessionStart", source="resume", cwd=str(other))
    check("a resume from another directory re-pins there",
          (other / ".claude" / "maestro" / c.sid[:8] / "state.json").is_file())


def long_tool_case():
    """An agent inside one long tool call gets the tool's timeout on top."""
    print("\n=== stall: a long tool call is not a stall ===")
    ws = workspace()
    c = Conductor(PLUGIN, ws, "aaaa999a-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(1, kind="codex", names=["codex-run"])
    aid = ids["codex-run"]
    c.fire(hook_event_name="PreToolUse", tool_name="Bash", agent_id=aid,
           agent_type="codex-run", tool_input={"command": "codex exec -"})
    age_node(c, aid, 700)
    c.tool_call()
    check("no stall while its Bash call runs", not c.msgs, c.msgs)


def untyped_failure_case():
    """A failed node with no type must not silence the whole re-check."""
    print("\n=== repeated failures tolerate an untyped node ===")
    sys.path.insert(0, str(PLUGIN))
    import reorchestrate
    st = {"nodes": {"a": {"id": "a", "status": "failed", "type": None},
                    "b": {"id": "b", "status": "failed", "type": "builder"},
                    "c": {"id": "c", "status": "failed", "type": "builder"}}}
    try:
        found = [fp for fp, _ in reorchestrate.check(st, time.time())]
    except TypeError as e:
        found = [f"raised {e}"]
    check("builder's two failures still reported", "fail:builder:2" in found, found)


def ws_git():
    ws = workspace()
    (ws / ".git").mkdir()
    return ws


def wt_edit(c, aid, name, ws, wt, rel):
    path = ws / ".claude" / "worktrees" / wt / rel
    c.fire(hook_event_name="PostToolUse", tool_name="Edit", agent_id=aid, agent_type=name,
           cwd=str(ws / ".claude" / "worktrees" / wt), tool_input={"file_path": str(path)},
           tool_response={"ok": True})


def worktree_collide_case():
    """Two builders editing one repo file in their own worktrees collide."""
    print("\n=== collisions across worktrees ===")
    ws = ws_git()
    c = Conductor(PLUGIN, ws, "bbbb0001-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(2, kind="builder", names=["builder-l", "builder-r"])
    wt_edit(c, ids["builder-l"], "builder-l", ws, "agent-l", "src/x.py")
    wt_edit(c, ids["builder-r"], "builder-r", ws, "agent-r", "src/x.py")
    c.tool_call()
    check("same repo file in two worktrees is a collision",
          any("both live on src/x.py" in m for m in c.msgs), c.msgs)


def peers_case():
    """Conductor addresses are captured, listed by peers.py, and a peer's
    agent writing the same file is reported to the other conductor."""
    print("\n=== conductors see each other ===")
    ws = ws_git()
    a = Conductor(PLUGIN, ws, "bbbb0002-0000-0000-0000-00000000000a")
    b = Conductor(PLUGIN, ws, "bbbb0003-0000-0000-0000-00000000000b")
    for c, name in ((a, "proj-aa"), (b, "proj-bb")):
        c.fire(hook_event_name="SessionStart", source="startup")
        c.fire(hook_event_name="PostToolUse", tool_name="ListAgents", tool_input={},
               tool_response={"listing": f"This session is {name} [abc123] — the name"})
    check("own address captured", a.state().get("peer_name") == "proj-aa", a.state().get("peer_name"))
    ia = a.dispatch(1, kind="builder", names=["builder-a"])
    ib = b.dispatch(1, kind="builder", names=["builder-b"])
    wt_edit(b, ib["builder-b"], "builder-b", ws, "agent-b", "api/handler.ts")
    r = subprocess.run([sys.executable, str(PLUGIN / "peers.py"), "--json"], cwd=ws,
                       capture_output=True, text=True,
                       env={**ENV, "CLAUDE_CODE_SESSION_ID": a.sid})
    here = os.path.realpath(ws)
    rows = [x for x in json.loads(r.stdout or "[]") if os.path.realpath(x["repo"]) == here]
    check("peers.py lists the other conductor, not itself",
          [x["address"] for x in rows] == ["proj-bb"], r.stdout + r.stderr)
    check("with the file its agent is writing",
          rows and rows[0]["writing"] == ["api/handler.ts"], rows)
    wt_edit(a, ia["builder-a"], "builder-a", ws, "agent-a", "api/handler.ts")
    a.tool_call()
    hits = [m for m in a.msgs if "another conductor" in m and '"proj-bb"' in m]
    check("the overlap is reported once, naming the peer's address", len(hits) == 1, a.msgs)
    a.tool_call()
    check("and not repeated", len([m for m in a.msgs if "another conductor" in m]) == 1)


def teammate_retire_case():
    """A teammate that has reported is stopped when it goes idle."""
    print("\n=== reported teammates are retired, not left idling ===")
    ws = ws_git()
    c = Conductor(PLUGIN, ws, "bbbb0004-0000-0000-0000-00000000000a")
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(2, names=["scout-done", "scout-busy"])
    c.finish(ids["scout-done"], "scout-done")

    def idle(name, aid, extra=None):
        p = {"session_id": c.sid, "cwd": str(ws), "hook_event_name": "TeammateIdle",
             "teammate_name": name, "agent_id": aid}
        r = subprocess.run([sys.executable, str(PLUGIN / "ledger.py")], input=json.dumps(p),
                           capture_output=True, text=True, env={**ENV, **(extra or {})})
        return r.stdout.strip()

    out = idle("scout-done", ids["scout-done"])
    check("stopped with continue:false",
          out and json.loads(out).get("continue") is False and "Message it" in out, out)
    check("a teammate still working is left alone", idle("scout-busy", ids["scout-busy"]) == "")
    check("MAESTRO_KEEP_TEAMMATES=1 keeps it",
          idle("scout-done", ids["scout-done"], {"MAESTRO_KEEP_TEAMMATES": "1"}) == "")


def panel_case():
    """Finished agents leave the panel; idle ones are one quiet line."""
    print("\n=== agent panel hides finished rows ===")
    tasks = [{"id": "1", "type": "scout", "status": "running", "description": "reading"},
             {"id": "2", "type": "builder", "status": "completed", "description": "done"},
             {"id": "3", "type": "builder", "status": "idle", "description": "waiting"}]
    r = subprocess.run([sys.executable, str(PLUGIN / "subagent_tree.py")],
                       input=json.dumps({"columns": 100, "tasks": tasks}),
                       capture_output=True, text=True, env=ENV)
    rows = {x["id"]: x["content"] for x in map(json.loads, r.stdout.splitlines())}
    check("completed row hidden", rows.get("2") == "", rows)
    check("idle row has no context bar or clock",
          "idle" in rows.get("3", "") and "%" not in rows.get("3", ""), rows.get("3"))
    check("running row still drawn", "reading" in rows.get("1", ""), rows.get("1"))


def main():
    tokens_case()
    digest_case()
    handoff_case()
    handback_case()
    collide_case()
    workflow_case()
    worktree_ledger_case()
    phantom_case()
    stall_case()
    quiet_case()
    conductor_in_worktree_case()
    cd_up_case()
    long_tool_case()
    untyped_failure_case()
    worktree_collide_case()
    peers_case()
    teammate_retire_case()
    panel_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
