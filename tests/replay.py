#!/usr/bin/env python3
"""Replay hook events through the maestro hooks and count what a conductor gets.

Every message a hook injects costs the conductor a full main-loop turn on a
very large context, so the number that matters is not bytes, it is *count* —
and of that count, how many carried a number that was true when it was rendered
and would have changed a decision.

This drives the real scripts with real payload shapes, taken from the field
ledgers (`SubagentStop` re-fires with `stop_hook_active`; `last_assistant_message`
is sometimes absent; `agent_transcript_path` is always present).

    python3 tests/replay.py            # the shipped scripts
    python3 tests/replay.py --before   # a git revision, for the before/after
    python3 tests/replay.py --before HEAD~1

Exit code is 0 when every case passes.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "maestro" / "scripts"
ENV = {**os.environ, "MAESTRO_CAFFEINATE": "0", "MAESTRO_QUIET": "1",
       "MAESTRO_REORCH_LLM": "0", "MAESTRO_NTFY_TOPIC": ""}

# Which events each script is actually wired to in maestro/hooks/hooks.json.
# Replaying a wiring that does not ship would prove nothing.
REORCH_EVENTS = {"PostToolUse", "PostToolUseFailure", "TaskCompleted"}
LEGACY_REORCH_TOOLS = {"Agent", "Task"}     # the 0.2.1 matcher

IDLE_FRAME = json.dumps({"type": "idle_notification", "from": "scout-web-referral",
                         "timestamp": "2026-08-21T00:00:00Z",
                         "idleReason": "available"})


def report_text(name):
    return (f"```\nRETURN:\n  answer: {name} traced the attribution rules\n"
            f"  files:  /srv/bff/handler.ts:2201\n  gaps:   none\n```")


class Conductor:
    """One session. `.msgs` is everything the conductor would have been shown."""

    def __init__(self, scripts, repo, sid, legacy=False):
        self.scripts, self.repo, self.sid = Path(scripts), Path(repo), sid
        self.legacy, self.msgs, self.clock = legacy, [], 0.0

    @property
    def dir(self):
        return self.repo / ".claude" / "maestro" / self.sid[:8]

    def _runs(self, ev, tool):
        yield "ledger.py"
        if self.legacy:
            # 0.2.1: PostToolUse matcher "Agent|Task", plus TaskCompleted.
            if (ev == "PostToolUse" and tool in LEGACY_REORCH_TOOLS) or ev == "TaskCompleted":
                yield "reorchestrate.py"
        elif ev in REORCH_EVENTS:
            yield "reorchestrate.py"

    def fire(self, **payload):
        p = {"session_id": self.sid, "cwd": str(self.repo), **payload}
        blob = json.dumps(p)
        ev, tool = p.get("hook_event_name"), p.get("tool_name")
        for script in self._runs(ev, tool):
            r = subprocess.run([sys.executable, str(self.scripts / script)], input=blob,
                               capture_output=True, text=True, env=ENV)
            if script == "reorchestrate.py" and r.stdout.strip():
                try:
                    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
                except (ValueError, KeyError):
                    continue
                if ctx.strip():
                    self.msgs.append(ctx)
        return self

    # --- conductor actions -------------------------------------------------
    def dispatch(self, n, kind="scout", names=None, prompt="trace the rules",
                 order="interleaved"):
        """One assistant message that dispatches n agents.

        `order` is the only thing the harness cannot take from the ledgers: the
        recorded batches are all sequential, so the interleaving of a genuinely
        parallel one is unobserved. Both orderings are replayed.

          interleaved  Pre/Post/Start per agent — reproduces the reported
                       "1 agent in flight" and "3 agents in flight" counts.
          batched      all Pre, then all Post, then all Start — reproduces the
                       reported "0 agents in flight".
        """
        names = names or [f"{kind}-{i}" for i in range(n)]
        ids = [f"a{abs(hash(self.sid + nm)) % 10**12:012x}" for nm in names]
        pre = lambda i: self.fire(
            hook_event_name="PreToolUse", tool_name="Agent", tool_use_id=f"tu-{ids[i]}",
            tool_input={"subagent_type": f"maestro:{kind}", "description": names[i],
                        "prompt": prompt})
        post = lambda i: self.fire(
            hook_event_name="PostToolUse", tool_name="Agent", tool_use_id=f"tu-{ids[i]}",
            tool_input={"subagent_type": f"maestro:{kind}"}, tool_response={"ok": True})
        start = lambda i: self.fire(
            hook_event_name="SubagentStart", agent_id=ids[i], agent_type=names[i])
        if order == "batched":
            for i in range(n): pre(i)
            for i in range(n): post(i)
            for i in range(n): start(i)
        else:
            for i in range(n):
                pre(i); post(i); start(i)
        return dict(zip(names, ids))

    def tool_call(self, name="Bash"):
        self.fire(hook_event_name="PreToolUse", tool_name=name,
                  tool_input={"command": "git status"})
        self.fire(hook_event_name="PostToolUse", tool_name=name,
                  tool_input={"command": "git status"}, tool_response={"stdout": ""})
        return self

    def finish(self, aid, name, last=None, transcript=None):
        self.fire(hook_event_name="SubagentStop", agent_id=aid, agent_type=name,
                  last_assistant_message=last if last is not None else report_text(name),
                  agent_transcript_path=str(transcript) if transcript else None)
        return self

    def state(self):
        return json.loads((self.dir / "state.json").read_text())


def transcript(path, texts):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for t in texts:
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": t}]}}) + "\n")
    return path


def workspace():
    d = Path(tempfile.mkdtemp(prefix="maestro-replay-"))
    (d / ".claude").mkdir()
    return d


# --------------------------------------------------------------------------
# The case from the field report: seven agents, two swallowed reports, a
# sibling session running concurrently in the same workspace.
# --------------------------------------------------------------------------
def field_session(scripts, legacy, order):
    """Seven agents, two swallowed reports, one concurrent sibling session."""
    ws = workspace()
    c = Conductor(scripts, ws, "aaaa1111-0000-0000-0000-00000000000a", legacy)
    sib = Conductor(scripts, ws, "bbbb2222-0000-0000-0000-00000000000b", legacy)
    c.fire(hook_event_name="SessionStart", source="startup")
    sib.fire(hook_event_name="SessionStart", source="startup")

    # The sibling fans six builders onto one shared plan file. Its hooks run
    # between the conductor's, so it owns the workspace-global `current`
    # pointer whenever the conductor next asks a question.
    sib.dispatch(6, kind="builder", names=[f"sib-builder-{i}" for i in range(6)],
                 order=order)
    sib_ids = [f"a{abs(hash(sib.sid + f'sib-builder-{i}')) % 10**12:012x}"
               for i in range(3)]

    def sibling_activity():
        for i, aid in enumerate(sib_ids):
            sib.fire(hook_event_name="PostToolUse", tool_name="Edit", agent_id=aid,
                     agent_type=f"sib-builder-{i}",
                     tool_input={"file_path": "/w/CREATOR-PROGRAM-BINDING-PLAN.md"},
                     tool_response={"ok": True})

    # --- wave 1: five agents in ONE assistant message ---------------------
    names = ["scout-web-referral", "scout-tests-config", "scout-surfaces",
             "scout-bff", "scout-infra"]
    before = len(c.msgs)
    w1 = c.dispatch(5, names=names, order=order)
    mid_batch = len(c.msgs) - before

    sibling_activity()
    time.sleep(2.5)                       # the batch settles
    c.tool_call()                         # the conductor's next turn

    # Two of the five hand back only a protocol frame. Their reports are in
    # their own transcripts, where the harness left them.
    tr = {n: transcript(ws / "tr" / f"{n}.jsonl",
                        ["searching…", report_text(n), IDLE_FRAME]) for n in names}
    swallowed = ["scout-web-referral", "scout-tests-config"]
    for n in swallowed:
        c.finish(w1[n], n, last=IDLE_FRAME, transcript=tr[n])
    for n in names:
        if n not in swallowed:
            c.finish(w1[n], n, transcript=tr[n])

    sibling_activity()
    time.sleep(5.5)                       # past the resend window
    c.tool_call()

    # --- wave 2: two agents in ONE assistant message ----------------------
    before = len(c.msgs)
    c.dispatch(2, kind="builder", names=["builder-web", "builder-infra"], order=order)
    mid_batch += len(c.msgs) - before
    sibling_activity()
    time.sleep(2.5)
    c.tool_call()
    return c, mid_batch, swallowed


def audit(c, swallowed):
    """Classify every message the conductor was shown."""
    state = c.state()
    ours = {f for x in state["nodes"].values() for f in (x.get("files") or [])}
    rows = []
    for m in c.msgs:
        head = m.splitlines()[0]
        accurate, foreign = True, False
        if head.startswith("MAESTRO"):
            tok = head.split()[2] if len(head.split()) > 2 else ""
            claimed = int(tok) if tok.isdigit() else -1
            live = sum(1 for x in state["nodes"].values()
                       if x.get("id") != "root" and x.get("status") in ("running", "spawning"))
            accurate = claimed in (live, live + len(state.get("pending") or []))
        if "CREATOR-PROGRAM-BINDING-PLAN" in m and not any(
                "CREATOR-PROGRAM-BINDING-PLAN" in f for f in ours):
            foreign, accurate = True, False
        rows.append({
            "head": head[:92],
            "accurate": accurate,
            "actionable": bool(
                head.startswith("REPORT")
                or "has had no tool activity" in m or "has failed" in m
                or "both live on" in m or "Three dispatches in a row" in m),
            "foreign": foreign,
            # Repeated *findings*, not repeated prose: a recovered report is
            # quoted verbatim and legitimately repeats lines like "```".
            "dupes": (lambda b: len(b) - len(set(b)))(
                [ln for ln in m.splitlines() if ln.startswith("- ")]),
        })
    surfaced = {n for n in swallowed if any(n in m for m in c.msgs)}
    return rows, surfaced


def bleed_case(scripts, legacy):
    """Defect 7 — a sibling session's tree read as this conductor's.

    Two sessions in one workspace race for `.claude/maestro/current`. Whoever
    fires a hook last owns the pointer, so this is a real interleaving, not a
    contrived one; the replay forces the losing order rather than waiting for
    it. The conductor here has two scouts out and has touched no plan file.
    """
    ws = workspace()
    c = Conductor(scripts, ws, "cccc3333-0000-0000-0000-00000000000c", legacy)
    sib = Conductor(scripts, ws, "dddd4444-0000-0000-0000-00000000000d", legacy)
    c.fire(hook_event_name="SessionStart", source="startup")
    sib.fire(hook_event_name="SessionStart", source="startup")
    c.dispatch(2, names=["scout-a", "scout-b"])
    sib.dispatch(6, kind="builder", names=[f"sib-{i}" for i in range(6)])
    for i in range(3):
        sib.fire(hook_event_name="PostToolUse", tool_name="Edit",
                 agent_id=f"a{abs(hash(sib.sid + f'sib-{i}')) % 10**12:012x}",
                 agent_type=f"sib-{i}",
                 tool_input={"file_path": "/w/CREATOR-PROGRAM-BINDING-PLAN.md"},
                 tool_response={"ok": True})
    pointer = Path((ws / ".claude" / "maestro" / "current").read_text()).name
    time.sleep(2.5)
    # The conductor's own next tool call, evaluated while the sibling holds the
    # pointer — the losing side of the race.
    blob = json.dumps({"session_id": c.sid, "cwd": str(ws),
                       "hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "git status"},
                       "tool_response": {"stdout": ""}})
    r = subprocess.run([sys.executable, str(Path(scripts) / "reorchestrate.py")],
                       input=blob, capture_output=True, text=True, env=ENV)
    msg = ""
    if r.stdout.strip():
        try:
            msg = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            msg = r.stdout.strip()
    return pointer, msg


def inbox_case(scripts, legacy):
    """Defects 3 and 4 — the stale idle ping never reaches the conductor.

    An idle notification is written by a `Stop` hook running inside the
    *teammate's* session, straight into the lead's mailbox at
    `~/.claude/teams/session-<sid8>/inboxes/<lead>.json`. This builds a
    faithful replica of that directory, alongside the frames that must never be
    touched: prose, permission requests, plan approvals, shutdown handshakes,
    already-read entries, and pings from agents this session never dispatched.
    """
    sid = "7e57bed0-0000-0000-0000-0000000000aa"
    team = Path.home() / ".claude" / "teams" / f"session-{sid[:8]}"
    shutil.rmtree(team, ignore_errors=True)
    (team / "inboxes").mkdir(parents=True)
    (team / "config.json").write_text(json.dumps({
        "name": team.name, "leadAgentId": f"team-lead@{team.name}",
        "leadSessionId": sid,
        "members": [{"agentId": f"team-lead@{team.name}", "name": "team-lead"}]}))

    def frame(who, typ="idle_notification", **kw):
        return {"from": who, "timestamp": "t",
                "text": json.dumps({"type": typ, "from": who, "timestamp": "t", **kw})}

    seeded = [
        frame("scout-surfaces"),                                     # reported
        frame("scout-web-referral"),                                 # still working
        {"from": "scout-bff", "text": "My report, in prose.", "timestamp": "t"},
        frame("adv-lock", typ="permission_request", request_id="r1"),
        frame("w1-money", typ="plan_approval_request", requestId="r2"),
        frame("scout-infra", typ="shutdown_request", requestId="r3"),
        dict(frame("scout-tests-config"), read=True),
        frame("a-stranger"),                                         # not ours
    ]
    (team / "inboxes" / "team-lead.json").write_text(json.dumps(seeded))

    ws = workspace()
    c = Conductor(scripts, ws, sid, legacy)
    c.fire(hook_event_name="SessionStart", source="startup")
    ids = c.dispatch(2, names=["scout-surfaces", "scout-web-referral"])
    c.finish(ids["scout-surfaces"], "scout-surfaces")     # delivers its report

    def read():
        return json.load(open(team / "inboxes" / "team-lead.json"))

    after = [m["from"] for m in read()]

    # Now it reports too, then pings again — that ping is stale on arrival.
    c.finish(ids["scout-web-referral"], "scout-web-referral")
    cur = read(); cur.append(frame("scout-web-referral"))
    (team / "inboxes" / "team-lead.json").write_text(json.dumps(cur))
    c.fire(hook_event_name="TeammateIdle", teammate_name="scout-web-referral",
           team_name=team.name)
    final = [m["from"] for m in read()]

    # A malformed mailbox must be left exactly as found.
    safe = True
    for bad in ['{"not":"a list"}', "[1,2,3]", "not json"]:
        (team / "inboxes" / "team-lead.json").write_text(bad)
        c.tool_call()
        safe = safe and (team / "inboxes" / "team-lead.json").read_text() == bad
    shutil.rmtree(team, ignore_errors=True)
    return [m["from"] for m in seeded], after, final, safe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", nargs="?", const="HEAD", default=None,
                    metavar="REV", help="replay a git revision of the scripts")
    args = ap.parse_args()

    legacy = args.before is not None
    scripts, tmp = PLUGIN, None
    if legacy:
        tmp = Path(tempfile.mkdtemp(prefix="maestro-rev-"))
        for name in ("ledger.py", "reorchestrate.py"):
            blob = subprocess.run(["git", "-C", str(REPO), "show",
                                   f"{args.before}:maestro/scripts/{name}"],
                                  capture_output=True, text=True, check=True).stdout
            (tmp / name).write_text(blob)
        scripts = tmp

    label = f"BEFORE ({args.before})" if legacy else "AFTER (working tree)"
    failures = []
    for order in ("interleaved", "batched"):
        print(f"\n=== {label} — five-agent batch, {order} " + "=" * 20)
        c, mid, swallowed = field_session(scripts, legacy, order)
        rows, surfaced = audit(c, swallowed)
        for r in rows:
            flag = ("acc" if r["accurate"] else "WRONG") + "/" + \
                   ("act" if r["actionable"] else "noise") + \
                   ("/foreign" if r["foreign"] else "")
            print(f"  [{flag:>19}] {r['head']}")
        if not rows:
            print("  (no messages)")
        print(f"\n  messages reaching the conductor : {len(rows)}")
        print(f"    …arriving mid-batch           : {mid}")
        print(f"    …accurate when rendered       : {sum(r['accurate'] for r in rows)}")
        print(f"    …would change a decision      : {sum(r['actionable'] for r in rows)}")
        print(f"    …naming another session       : {sum(r['foreign'] for r in rows)}")
        print(f"    …with a repeated line         : {sum(r['dupes'] for r in rows)}")
        print(f"  swallowed reports surfaced      : "
              f"{len(surfaced)}/{len(swallowed)} {sorted(surfaced)}")

        if legacy:
            continue
        bad = []
        if mid:
            bad.append(f"{mid} message(s) arrived mid-batch; target 0")
        if any(not r["accurate"] for r in rows):
            bad.append("a count was not true when it was rendered")
        if any(not r["actionable"] for r in rows):
            bad.append("a message would not have changed a decision")
        if any(r["foreign"] for r in rows):
            bad.append("a message named another session's work")
        if any(r["dupes"] for r in rows):
            bad.append("a finding was repeated inside one message")
        if surfaced != set(swallowed):
            bad.append(f"swallowed reports surfaced: {sorted(surfaced)}, "
                       f"expected {sorted(swallowed)}")
        for b in bad:
            print(f"  FAIL: {b}")
        failures += bad

    print(f"\n=== {label} — concurrent sibling session " + "=" * 22)
    pointer, msg = bleed_case(scripts, legacy)
    print(f"  workspace `current` pointer -> {pointer} (the sibling)")
    if msg:
        bullets = [ln for ln in msg.splitlines() if ln.strip().startswith("- ")]
        dupes = len(bullets) - len(set(bullets))
        print("  conductor was shown:\n     " + msg.replace("\n", "\n     "))
        print(f"  repeated lines inside that one message: {dupes}")
    else:
        print("  conductor was shown: (nothing)")
    if not legacy and msg:
        failures.append("a sibling session's tree leaked into the re-check")

    print(f"\n=== {label} — stale idle pings in the lead's mailbox " + "=" * 12)
    seeded, after, final, safe = inbox_case(scripts, legacy)
    print(f"  seeded             : {len(seeded)} entries")
    print(f"  after it reported  : {after}")
    print(f"  after a stale ping : {final}")
    print(f"  malformed mailbox left untouched: {safe}")
    if not legacy:
        if "scout-surfaces" in after:
            failures.append("a ping from an agent that had already reported survived")
        if "scout-web-referral" not in after:
            failures.append("a ping from an agent still working was dropped")
        if "scout-web-referral" in final:
            failures.append("a ping stale on arrival survived")
        for keeper in ("scout-bff", "adv-lock", "w1-money", "scout-infra",
                       "scout-tests-config", "a-stranger"):
            if keeper not in final:
                failures.append(f"{keeper}'s entry was removed and must not have been")
        if not safe:
            failures.append("a malformed mailbox was rewritten")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    print("\n  " + ("PASS" if not failures else f"FAIL ({len(failures)})"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
