#!/usr/bin/env python3
"""Focused checks for the background-stall guard.

A subagent is never woken when a background command it started finishes, so
ledger.py denies a subagent's background Bash on PreToolUse, and on
TeammateIdle sends a teammate back to wait for one it never heard back from.

    python3 tests/bg_stall_units.py

Exit code is 0 when every check passes.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import Conductor, workspace, PLUGIN, ENV, report_text

FAILURES = []
LEDGER = PLUGIN / "ledger.py"


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def run(payload=None, extra=None, raw=None):
    blob = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(LEDGER)], input=blob,
                          capture_output=True, text=True, env={**ENV, **(extra or {})})


def decision(r):
    try:
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError, TypeError):
        return None


def session(sid):
    ws = workspace()
    (ws / ".git").mkdir()
    c = Conductor(PLUGIN, ws, sid)
    c.fire(hook_event_name="SessionStart", source="startup")
    return c, ws


# --- the transcript a teammate leaves behind --------------------------------
def launch(tid, out, tu, command="npx jest --maxWorkers=2", desc="Run the full suite"):
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tu, "name": "Bash",
             "input": {"command": command, "description": desc, "timeout": 600000,
                       "run_in_background": True}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tu, "is_error": False,
             "content": f"Command running in background with ID: {tid}. Output is being "
                        f"written to: {out}. You will be notified when it completes."}]},
         "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": tid}},
    ]


def said(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def notice(tid, tu, out):
    """The shape the harness writes once the agent is awake to receive it."""
    return {"type": "attachment", "attachment": {
        "type": "queued_command", "commandMode": "task-notification",
        "prompt": f"<task-notification>\n<task-id>{tid}</task-id>\n<tool-use-id>{tu}"
                  f"</tool-use-id>\n<output-file>{out}</output-file>\n<status>completed"
                  f"</status>\n</task-notification>"}}


def stop_call(tid):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_stop", "name": "TaskStop", "input": {"task_id": tid}}]}}


class Mate:
    """One teammate in its own session, with a transcript beside the session's."""

    def __init__(self, sid, name="impl-bg"):
        self.c, self.ws = session(sid)
        self.name = name
        self.aid = self.c.dispatch(1, kind="builder", names=[name])[name]
        self.session_tr = self.ws / "tr" / f"{sid}.jsonl"
        self.tr = self.ws / "tr" / sid / "subagents" / f"agent-{self.aid}.jsonl"
        self.tr.parent.mkdir(parents=True, exist_ok=True)
        self.recs = []

    def out(self, tid, text="PASS src/a.test.ts\n"):
        p = self.ws / "tasks" / f"{tid}.output"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def add(self, *recs):
        self.recs.extend(recs)
        self.tr.write_text("".join(json.dumps(r) + "\n" for r in self.recs))
        return self

    def stop(self, last):
        """The SubagentStop that lands just before TeammateIdle, as in the field."""
        self.c.finish(self.aid, self.name, last=last)
        return self

    def idle(self, extra=None, agent_id=True):
        p = {"session_id": self.c.sid, "cwd": str(self.ws), "hook_event_name": "TeammateIdle",
             "teammate_name": self.name, "transcript_path": str(self.session_tr),
             "scratchpad_dir": str(self.ws / "scratchpad")}
        if agent_id:
            p["agent_id"] = self.aid
        return run(p, extra)

    def node(self):
        return self.c.state()["nodes"].get(self.aid) or {}


# --- PreToolUse --------------------------------------------------------------
def guard_case():
    print("\n=== a subagent's background Bash is denied ===")
    c, ws = session("cccc0001-0000-0000-0000-00000000000a")

    def bash(command, bg=True, agent=True, extra=None):
        p = {"session_id": c.sid, "cwd": str(ws), "hook_event_name": "PreToolUse",
             "tool_name": "Bash", "tool_use_id": "toolu_x",
             "tool_input": {"command": command, "description": "run the suite",
                            "timeout": 600000, "run_in_background": bg}}
        if agent:
            p.update(agent_id="abuilder000001", agent_type="builder")
        return run(p, extra)

    r = bash("npx jest --maxWorkers=2")
    reason = (json.loads(r.stdout or "{}").get("hookSpecificOutput") or {}).get(
        "permissionDecisionReason", "")
    check("denied", decision(r) == "deny" and r.returncode == 0, r.stdout + r.stderr)
    check("the reason says subagents are not woken", "not woken" in reason, reason)
    check("the reason says foreground with timeout 600000", "foreground" in reason
          and "600000" in reason and "split" in reason, reason)
    check("the reason names the long-lived marker", "# maestro: long-lived" in reason, reason)
    node = c.state()["nodes"].get("abuilder000001") or {}
    check("a denied call is not left counted as in flight", "in_tool_since" not in node, node)

    r = bash("npm run dev -- --port 5173  # maestro: long-lived")
    check("the same call with the marker is allowed", r.stdout.strip() == "" and r.returncode == 0,
          r.stdout)
    r = bash("npx expo start #Maestro:  LONG-LIVED")
    check("the marker is matched loosely", r.stdout.strip() == "", r.stdout)
    r = bash("npx jest --maxWorkers=2", agent=False)
    check("the conductor's background Bash is allowed", r.stdout.strip() == "", r.stdout)
    r = bash("npx jest --maxWorkers=2", bg=False)
    check("a subagent's foreground Bash is allowed", r.stdout.strip() == "", r.stdout)
    r = bash("npx jest", extra={"MAESTRO_BG_GUARD": "0"})
    check("MAESTRO_BG_GUARD=0 turns it off", r.stdout.strip() == "", r.stdout)
    p = {"session_id": c.sid, "cwd": str(ws), "hook_event_name": "PreToolUse",
         "tool_name": "Monitor", "agent_id": "abuilder000001",
         "tool_input": {"command": "tail -f x.log", "run_in_background": True}}
    check("only Bash is guarded", run(p).stdout.strip() == "")


# --- TeammateIdle ------------------------------------------------------------
def idle_pending_case():
    print("\n=== an idle with a pending background task is held ===")
    m = Mate("cccc0002-0000-0000-0000-00000000000a")
    out = m.out("bx1")
    m.add(*launch("bx1", out, "toolu_bg1"), said("Waiting for the full-suite summary."))
    m.stop("Waiting for the full-suite summary.")
    r = m.idle()
    check("exit 2", r.returncode == 2, f"rc={r.returncode} {r.stderr}")
    check("feedback names the command, task and output file",
          '"Run the full suite"' in r.stderr and "bx1" in r.stderr and str(out) in r.stderr,
          r.stderr)
    check("feedback says it is still running and how to wait in the foreground",
          "still running" in r.stderr and "until grep -qE" in r.stderr
          and "timeout 600000" in r.stderr and "TaskStop" in r.stderr, r.stderr)
    check("not retired while held", "continue" not in r.stdout, r.stdout)
    check("the ledger shows it running again, not idle",
          m.node().get("status") == "running" and "idle_at" not in m.node(), m.node())

    out.write_text(out.read_text() + "Tests: 3 passed\n[exited with code 0]")
    r = m.idle()
    check("a finished task whose notice never arrived is still held",
          r.returncode == 2 and "finished" in r.stderr, r.stderr)

    m.add(notice("bx1", "toolu_bg1", out))
    r = m.idle()
    check("once the notice is in the transcript, idle is allowed",
          r.returncode == 0 and r.stderr == "", f"rc={r.returncode} {r.stderr}")

    print("=== older payloads without agent_id ===")
    n = Mate("cccc0003-0000-0000-0000-00000000000a")
    out = n.out("bx9")
    n.add(*launch("bx9", out, "toolu_bg9"), said("Running the suite now."))
    r = n.idle(agent_id=False)
    check("the teammate is found by name", r.returncode == 2 and "bx9" in r.stderr, r.stderr)


def cap_case():
    print("\n=== at most 3 holds per pending task ===")
    m = Mate("cccc0004-0000-0000-0000-00000000000a")
    out = m.out("bc1")
    m.add(*launch("bc1", out, "toolu_c1"), said("Waiting on the suite."))
    codes = [m.idle().returncode for _ in range(4)]
    check("held three times, then let go", codes == [2, 2, 2, 0], codes)
    out2 = m.out("bc2")
    m.add(*launch("bc2", out2, "toolu_c2", desc="Run lint"), said("Waiting on lint."))
    r = m.idle()
    check("a new pending task gets its own three",
          r.returncode == 2 and "bc2" in r.stderr and "bc1" not in r.stderr, r.stderr)


def settled_case():
    print("\n=== settled or exempt tasks never hold ===")
    m = Mate("cccc0005-0000-0000-0000-00000000000a")
    out = m.out("bs1")
    m.add(*launch("bs1", out, "toolu_s1"), stop_call("bs1"), said("Stopped it; nothing pending."))
    check("a task stopped with TaskStop", m.idle().returncode == 0)

    m = Mate("cccc0006-0000-0000-0000-00000000000a")
    out = m.out("bl1")
    m.add(*launch("bl1", out, "toolu_l1", command="npx expo start  # maestro: long-lived"),
          said("Metro is up; screenshots next."))
    check("a command marked long-lived", m.idle().returncode == 0)

    m = Mate("cccc0007-0000-0000-0000-00000000000a")
    out = m.out("bq1")
    m.add(*launch("bq1", out, "toolu_q1"), said("Should the suite also cover the old rows?"))
    m.stop("Should the suite also cover the old rows?")
    r = m.idle()
    check("a teammate that ended on a question",
          r.returncode == 0 and r.stderr == "" and "continue" not in r.stdout, r.stdout + r.stderr)

    m = Mate("cccc0008-0000-0000-0000-00000000000a")
    m.add(said("All done, nothing in the background."))
    check("no background task at all", m.idle().returncode == 0)
    check("MAESTRO_BG_GUARD=0 turns the backstop off",
          Mate("cccc0009-0000-0000-0000-00000000000a").add(
              *launch("bg0", "/nope/bg0.output", "toolu_g0"), said("waiting"))
          .idle({"MAESTRO_BG_GUARD": "0"}).returncode == 0)


def retire_case():
    print("\n=== a reported teammate is still retired ===")
    m = Mate("cccc0010-0000-0000-0000-00000000000a", name="impl-done")
    out = m.out("br1")
    m.add(*launch("br1", out, "toolu_r1"), said(report_text("impl-done")))
    m.stop(report_text("impl-done"))
    r = m.idle()
    msg = json.loads(r.stdout) if r.stdout.strip() else {}
    check("continue:false, exit 0, no hold", r.returncode == 0 and msg.get("continue") is False
          and "Message it" in msg.get("stopReason", "") and r.stderr == "", r.stdout + r.stderr)

    print("=== an old report does not retire a teammate now waiting ===")
    m = Mate("cccc0011-0000-0000-0000-00000000000a", name="impl-again")
    m.stop(report_text("impl-again"))            # it reported once ...
    out = m.out("ba1")
    m.add(said(report_text("impl-again")), *launch("ba1", out, "toolu_a1"),
          said("Rerunning the suite after the follow-up."))   # ... then took more work
    r = m.idle()
    check("held, not stopped", r.returncode == 2 and "continue" not in r.stdout,
          f"rc={r.returncode} {r.stdout}")


def fail_open_case():
    print("\n=== malformed input fails open ===")
    for raw in ("not json", "[1, 2]", "null", ""):
        r = run(raw=raw)
        check(f"stdin {raw!r}: exit 0, silent",
              r.returncode == 0 and r.stdout == "" and r.stderr == "", r.stdout + r.stderr)
    c, ws = session("cccc0012-0000-0000-0000-00000000000a")
    p = {"session_id": c.sid, "cwd": str(ws), "hook_event_name": "PreToolUse",
         "tool_name": "Bash", "agent_id": "abuilder000002", "tool_input": "run_in_background"}
    r = run(p)
    check("tool_input that is not an object", r.returncode == 0 and r.stdout == "", r.stdout)
    p = {"session_id": 12345, "cwd": str(ws), "hook_event_name": "PreToolUse",
         "tool_name": "Bash", "agent_id": "abuilder000002",
         "tool_input": {"command": "npx jest", "run_in_background": True}}
    r = run(p)
    check("an internal error allows the call, exit 0", r.returncode == 0 and r.stdout == "",
          r.stdout + r.stderr)

    m = Mate("cccc0013-0000-0000-0000-00000000000a")
    m.tr.write_text("{not json\n[]\nnull\n42\n"
                    + json.dumps({"type": "user", "message": {
                        "content": "Command running in background with ID: zz1"}}) + "\n"
                    + json.dumps({"type": "user", "message": {"content": [
                        {"type": "tool_result", "content": [{"type": "text", "text": "x"}]}]},
                        "toolUseResult": "backgroundTaskId"}) + "\n")
    r = m.idle()
    check("a garbage transcript", r.returncode == 0 and r.stderr == "", r.stderr)

    m = Mate("cccc0014-0000-0000-0000-00000000000a")
    out = m.out("bf1")
    m.add(*launch("bf1", out, "toolu_f1"), said("waiting"))
    sf = m.c.dir / "state.json"
    st = json.loads(sf.read_text())
    st["bg_blocks"] = ["corrupt"]
    sf.write_text(json.dumps(st))
    r = m.idle()
    check("a corrupt block counter lets it idle", r.returncode == 0 and r.stderr == "",
          f"rc={r.returncode} {r.stderr}")

    m = Mate("cccc0015-0000-0000-0000-00000000000a")
    m.tr.mkdir()
    r = m.idle()
    check("a transcript path that is a directory", r.returncode == 0 and r.stderr == "")


def main():
    guard_case()
    idle_pending_case()
    cap_case()
    settled_case()
    retire_case()
    fail_open_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
