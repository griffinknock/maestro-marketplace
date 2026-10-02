#!/usr/bin/env python3
"""Maestro re-orchestrator — at most one message per dispatch batch, or silence.

Wired to PostToolUse (every tool), PostToolUseFailure and TaskCompleted. It
never speaks while a dispatch batch is still landing, and it never speaks
inside a subagent.

Why every tool and not just `Agent|Task`
----------------------------------------
One assistant message that dispatches five agents fires PostToolUse(Agent)
five times, and each of those fires before the other four subagents have
emitted SubagentStart — so each sees a different partial count. That is where
"only 1 agent in flight, launch more" arrived *during* a five-agent launch,
and "0 agents in flight, the wave is done" arrived right after a two-agent
one. A count taken at that instant is not wrong by a little, it is
meaningless. So an Agent call now only *records* the batch; the verdict is
computed later, on the first ordinary tool call after the batch has settled,
when the census is real. If the count changed inside a single assistant
message, that is one event, not N.

Two rules keep the census honest:
  - a dispatch seen at PreToolUse but not yet matched to a SubagentStart is
    still in flight (`state["pending"]`, within MAESTRO_DISPATCH_GRACE);
  - nothing is emitted while the batch that produced the count is still open.

Session scoping
---------------
The state dir is resolved from this payload's own `session_id`, never from the
workspace-global `.claude/maestro/current` pointer. That pointer is rewritten
by whichever session last fired a hook, so reading it made a conductor inherit
a concurrent session's agents — reporting lanes it never opened and file
collisions on files it never dispatched against. The board still uses
`current` to find the newest session; a re-check must not.

Never wire this to SubagentStop, and never let it run inside a subagent:
additionalContext delivered there lands in the *stopping subagent*, which then
spends its final message answering the re-check instead of returning its
report. The conductor receives "nothing further to do here" and the findings
are stranded. Both guards are enforced in main(), independent of hooks.json.

It also carries the one message class that is always worth a turn: a subagent
report the harness failed to deliver. `ledger.py` keeps every report it sees;
when the copy that reached the conductor was empty or was a bare protocol
frame, the report is re-delivered here as a bounded digest with a pointer to
the durable copy, so recovery costs no round trip and loss is never silent.

Env:
  MAESTRO_REORCH=0          disable entirely
  MAESTRO_REORCH_LLM=1      enable the detached second opinion (default OFF)
  MAESTRO_REORCH_COOLDOWN   seconds between second opinions (default 90)
  MAESTRO_REORCH_SETTLE     seconds a batch must be quiet before judging (2)
  MAESTRO_DISPATCH_GRACE    seconds an unstarted dispatch still counts (60)
  MAESTRO_REPORT_GRACE      seconds to let a resend land before crying loss (5)
  MAESTRO_DEBUG=1           errors to stderr
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lessons as _lessons
except Exception:
    _lessons = None

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"
OFF = os.environ.get("MAESTRO_REORCH") == "0"
LESSONS_ON = os.environ.get("MAESTRO_LESSONS", "1") != "0"
# Default OFF. Every verdict this ever produced in the field was confidently
# wrong and argued for downgrading a tier that was carrying real difficulty.
# The attribution bug behind that is fixed below, but a model recommendation
# has to earn its way back on: a wrong one costs the conductor more reasoning
# than silence does.
USE_LLM = os.environ.get("MAESTRO_REORCH_LLM", "0") == "1"
COOLDOWN = float(os.environ.get("MAESTRO_REORCH_COOLDOWN", "90"))
SETTLE = float(os.environ.get("MAESTRO_REORCH_SETTLE", "2"))
GRACE = float(os.environ.get("MAESTRO_DISPATCH_GRACE", "60"))
REPORT_GRACE = float(os.environ.get("MAESTRO_REPORT_GRACE", "5"))
STALL_SECONDS = 300          # no tool activity this long = probably stuck
VERDICT_TTL = 240            # ignore an LLM opinion older than this
INLINE_CALLS = 8             # conductor tool calls after a wave = doing it itself
LIVE = ("running", "spawning")
ROOT = "root"

BLANK = {
    "open": False,            # a dispatch batch is landing right now
    "size": 0,                # agents dispatched in the open batch
    "last_dispatch_at": 0.0,
    "sizes": [],              # settled batch sizes, oldest first
    "calls_since_dispatch": 0,
    "wave": 0,
    "said": [],               # finding fingerprints already delivered
    "verdict_subjects": [],
}


def state_dir(payload):
    """This session's ledger dir, resolved from its own session_id.

    Deliberately does not consult `.claude/maestro/current`: that pointer is
    workspace-global and the newest session owns it, so a concurrent session in
    the same repo would hand this conductor someone else's agent tree.
    """
    sid = (payload.get("session_id") or "")[:8]
    if not sid:
        return None
    p = Path(payload.get("cwd") or ".").resolve()
    for parent in [p, *p.parents]:
        d = parent / ".claude" / "maestro" / sid
        if (d / "state.json").is_file():
            return d
    return None


def load_batch(d):
    try:
        b = json.loads((d / "recheck.json").read_text())
        if isinstance(b, dict):
            return {**BLANK, **b}
    except (OSError, json.JSONDecodeError):
        pass
    return dict(BLANK)


def save_batch(d, b):
    b["said"] = list(b.get("said") or [])[-300:]
    b["sizes"] = list(b.get("sizes") or [])[-12:]
    try:
        tmp = d / "recheck.tmp"
        tmp.write_text(json.dumps(b))
        tmp.replace(d / "recheck.json")
    except OSError:
        pass


def lanes(state):
    """A lane is a direct child of the conductor, plus everything under it."""
    nodes = state.get("nodes", {})
    out = {}
    for n in nodes.values():
        if n.get("id") == ROOT:
            continue
        cur, guard = n, 0
        while cur.get("parent") and cur["parent"] != ROOT and guard < 8:
            cur = nodes.get(cur["parent"], {})
            guard += 1
            if not cur:
                break
        head = (cur or n).get("id", n.get("id"))
        out.setdefault(head, []).append(n)
    return out


def census(state, now):
    """Agents this conductor actually has out, including ones still starting.

    `pending` holds dispatches seen at PreToolUse that have not yet matched a
    SubagentStart. Counting only started nodes is what produced "0 agents in
    flight" one beat after two agents were launched.
    """
    nodes = [n for n in state.get("nodes", {}).values() if n.get("id") != ROOT]
    live = [n for n in nodes if n.get("status") in LIVE]
    starting = [p for p in (state.get("pending") or [])
                if now - float(p.get("at") or 0) <= GRACE]
    return nodes, live, starting


def who(n):
    return n.get("name") or n.get("type") or "agent"


def where(state, n):
    """` (lane x)` — only when the lane is something other than the agent."""
    lane = n.get("lane")
    if not lane or lane == n.get("id"):
        return ""
    return f" (lane {who(state.get('nodes', {}).get(lane, {'type': lane}))})"


def read_report(n, max_lines=15, max_chars=900):
    """A digest of the recovered report, plus the pointer to the full copy.

    Re-delivering a report in full re-buys its tokens on the conductor's
    largest context every remaining turn. The durable copy already exists on
    disk (`ledger.py` writes it), so past ~15 lines the transcript gets a
    digest and a path, not the payload.
    """
    p = n.get("report_path")
    txt = ""
    if p:
        try:
            txt = Path(p).read_text().strip()
        except OSError:
            txt = ""
    if not txt:
        txt = (n.get("result") or "").strip()
    lines = txt.splitlines()
    digest = "\n".join(lines[:max_lines])[:max_chars].rstrip()
    if len(digest) < len(txt.rstrip()):
        dropped = max(0, len(lines) - len(digest.splitlines()))
        digest += f"\n… digest — {dropped} more line(s) on disk"
        if p:
            digest += f". Read the full report only if the digest is not enough: {p}"
    return digest


def delivery_findings(state, now):
    """Reports the harness did not hand to the conductor.

    This is the only class that always earns a turn: without it a swallowed
    report and a clean finish are the same frame, and the conductor has to
    spend a round trip asking to tell them apart.
    """
    out = []
    for n in state.get("nodes", {}).values():
        if n.get("id") == ROOT:
            continue
        # A Workflow's agents return to its script (StructuredOutput), never to
        # the conductor, which hears from the Workflow's completion notice.
        if "workflow-subagent" in (n.get("type"), n.get("name")):
            continue
        st = n.get("report_status")
        if st not in ("recovered", "missing"):
            continue
        # Stops repeat, and an agent that was interrupted mid-resend will send
        # the report again a beat later. Do not cry loss inside that window.
        if now - float(n.get("last_stop_at") or 0) < REPORT_GRACE:
            continue
        if st == "recovered":
            out.append((
                f"report:{n.get('id')}:recovered",
                f"REPORT RECOVERED — {who(n)} finished, but the copy delivered to you "
                f"was empty or a bare protocol frame. Its real report, from its own "
                f"transcript, is below. Do not ask it to resend.\n"
                f"{read_report(n)}"))  # digest + pointer; full copy stays on disk
        else:
            out.append((
                f"report:{n.get('id')}:missing",
                f"REPORT NOT DELIVERED — {who(n)} finished and no report was "
                f"recoverable from its transcript. Ask it to resend, or redo the work; "
                f"do not assume it reported."))
    return out


def check(state, batch, settled, now):
    """Deterministic rules. Returns [(fingerprint, text)], suspicious."""
    nodes, live, starting = census(state, now)
    f, suspicious = [], False

    # Stalled agents.
    for n in live:
        since = now - float(n.get("last_activity") or n.get("started") or now)
        if since > STALL_SECONDS:
            f.append((f"stall:{n.get('id')}:{int(since // STALL_SECONDS)}",
                      f"{who(n)}{where(state, n)} has had no tool activity for "
                      f"{int(since / 60)}m — check it or stop it."))
            suspicious = True

    # Repeated failures of the same tier.
    repeat = {}
    for n in nodes:
        if n.get("status") == "failed":
            repeat[n.get("type")] = repeat.get(n.get("type"), 0) + 1
    for t, c in sorted(repeat.items()):
        if c >= 2:
            f.append((f"fail:{t}:{c}",
                      f"{t} has failed {c} times. Escalate to surgeon with both failure "
                      f"transcripts rather than retrying the same tier."))
            suspicious = True

    # Worktree collisions — two live agents editing the same file. Fingerprinted
    # by path, so three agents on one file is one line, not three identical ones.
    # `writes`, not `files`: agents reading one shared contract is the design.
    seen = {}
    for n in live:
        for path in (n.get("writes") or []):
            owner = seen.get(path)
            if owner and owner != n.get("id"):
                f.append((f"collide:{path}",
                          f"{owner} and {who(n)} are both live on "
                          f"{path.split('/')[-1]} — serialize them or the merge conflicts."))
                suspicious = True
            seen.setdefault(path, who(n))

    # Depth ceiling. Depth 4 is not a problem, it is a budget the conductor
    # already prints in every section-lead prompt; only the hard stop is news.
    if max((n.get("depth", 0) for n in live), default=0) >= 5:
        f.append(("depth5",
                  "Depth 5 reached — nothing below this level may delegate further."))

    if not settled:
        # The wave finished and the conductor kept going by hand.
        if (not live and not starting and nodes
                and batch.get("calls_since_dispatch", 0) >= INLINE_CALLS):
            f.append((f"inline:{batch.get('wave')}",
                      f"No agents in flight and {batch['calls_since_dispatch']} tool calls "
                      f"since the last dispatch. If what is left splits, dispatch it."))
        return f, suspicious

    # --- shape rules: only meaningful once a batch has actually landed ---

    # Serial drift. One deliberate solo dispatch is not drift; three in a row is.
    sizes = list(batch.get("sizes") or [])
    if len(sizes) >= 3 and all(s == 1 for s in sizes[-3:]):
        f.append((f"serial:{len(sizes)}",
                  "Three dispatches in a row of one agent each. If those tasks did not "
                  "consume each other's output, they should have gone out together."))
        suspicious = True

    # Lane balance is deliberately not a rule. A lane whose agents have all
    # finished is *complete*, not idle, so "N of M lanes are idle while one
    # keeps working" fires on ordinary sequential progress — it fired on a
    # deliberate two-agent dispatch in the field. Excluding complete lanes
    # leaves a rule that can almost never fire honestly, so the doctrine it was
    # protecting is carried by the serial-drift rule above instead, which is
    # measured on dispatch batches and is true when it fires.

    return f, suspicious


def _lesson_fingerprint(fp, is_report):
    """`report:<id>:missing` -> `delivery:missing:<id>` for the lessons queue.

    The reorch-internal fingerprint is keyed for `said`-dedupe within one
    session; the lessons store dedupes across sessions on (session,
    fingerprint), so a delivery finding gets its own stable shape instead.

    Non-report fingerprints (`serial:{len(sizes)}`, `inline:{wave}`, ...) are
    wave-scoped for that same in-session `said` dedupe, so every wave mints a
    "new" candidate with identical text; collapse to the kind — the part
    before the first ':' — so the lessons store dedupes them as one finding.
    """
    if is_report and fp.startswith("report:"):
        parts = fp.split(":", 2)
        if len(parts) == 3:
            _, aid, kind = parts
            return f"delivery:{kind}:{aid}"
    if not is_report:
        return fp.split(":", 1)[0]
    return fp


def capture_lessons(payload, fresh):
    """Feed every NEW finding to the lessons candidate queue.

    Best-effort and silent: a capture failure must never change what this
    script prints or does, so every call is individually guarded.
    """
    if _lessons is None or not LESSONS_ON:
        return
    for fp, text, is_report in fresh:
        try:
            _lessons.capture_finding(
                payload.get("session_id"), payload.get("cwd"),
                _lesson_fingerprint(fp, is_report), text,
                tool_use_id=payload.get("tool_use_id"))
        except Exception:
            pass


def spawn_llm(d, state, live, now):
    """Detached second opinion, judged on the dispatch prompt and attributed.

    The old version handed the model a tool signature and a model name and
    nothing else, then printed whatever came back against whichever dispatch
    finished next. It called a 14,000-line attribution trace "trivial lookups"
    and an opus builder writing pagination tests "a read operation", because
    from a list of Grep counts that is exactly what they look like.

    So: it now gets the dispatch prompt it is judging, it must clear a
    confidence bar, and the ids it judged are written alongside the verdict.
    `take_verdict` throws the answer away if any of those agents is no longer
    live, which is what "attributed" has to mean — a verdict about a dispatch
    that has already finished cannot change anything.
    """
    stamp = d / ".llm-last"
    try:
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < COOLDOWN:
            return
        stamp.write_text(str(time.time()))
    except OSError:
        return

    subjects = [n.get("id") for n in live][:8]
    brief = json.dumps([{
        "id": n.get("id"),
        "agent_type": n.get("type"),
        "model": n.get("model"),
        "running_for_s": int(now - float(n.get("started") or now)),
        "tool_calls": n.get("tools") or {},
        "dispatch_prompt": (n.get("prompt") or n.get("description") or "")[:700],
    } for n in live[:8]])[:12000]

    prompt = (
        "You audit the SHAPE of an agent orchestration — never code correctness.\n"
        f"Agents currently in flight:\n{brief}\n\n"
        "Judge each agent from its dispatch_prompt and nothing else. tool_calls is "
        "context, not evidence: hard work often looks like a pile of Greps, and an "
        "expensive model reading a lot before it writes is normal, not waste. Never "
        "recommend a cheaper model unless the dispatch_prompt itself shows the task "
        "is mechanical — a single known fact, a rename, a file listing.\n\n"
        "Report only: work running in sequence with no dependency between the pieces, "
        "a task whose dispatch_prompt proves it is over-tiered, or a lane that should "
        "have been split. Answer in exactly this form and nothing else:\n"
        "CONFIDENCE: high|low\n"
        "FINDING: <one line, or NONE>\n"
        "Use high only when the dispatch_prompt alone proves it. If you are inferring "
        "from tool counts, elapsed time, or the agent's name, that is low."
    )
    try:
        (d / "verdict.subjects.json").write_text(
            json.dumps({"at": now, "subjects": subjects}))
        # MAESTRO_LEDGER_OFF stops this audit session from writing its own
        # ledger; MAESTRO_REORCH=0 stops it recursing into another audit.
        env = {**os.environ, "MAESTRO_REORCH": "0", "MAESTRO_REORCH_LLM": "0",
               "MAESTRO_LEDGER_OFF": "1", "MAESTRO_QUIET": "1"}
        subprocess.Popen(
            ["bash", "-lc",
             f"claude -p {json.dumps(prompt)} --model haiku --output-format text "
             f"> {json.dumps(str(d / 'verdict.tmp'))} 2>/dev/null && "
             f"mv {json.dumps(str(d / 'verdict.tmp'))} {json.dumps(str(d / 'verdict.txt'))}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


def take_verdict(d, state, now):
    """Read the detached opinion, and discard it unless it still applies."""
    f = d / "verdict.txt"
    try:
        if not f.is_file() or now - f.stat().st_mtime > VERDICT_TTL:
            return None
        txt = f.read_text().strip()
        f.unlink()
    except OSError:
        return None
    if not txt:
        return None

    conf, finding = "", ""
    for line in txt.splitlines():
        head, _, rest = line.partition(":")
        if head.strip().upper() == "CONFIDENCE":
            conf = rest.strip().lower()
        elif head.strip().upper() == "FINDING":
            finding = rest.strip()
    if conf != "high" or not finding or finding.upper() == "NONE":
        return None

    # Attribution: the agents it judged must still be the agents in flight.
    try:
        meta = json.loads((d / "verdict.subjects.json").read_text())
        subjects = meta.get("subjects") or []
    except (OSError, json.JSONDecodeError):
        return None
    if not subjects:
        return None
    for sid in subjects:
        n = state.get("nodes", {}).get(sid)
        if not n or n.get("status") not in LIVE:
            return None      # it judged a dispatch that is already over
    return finding[:400]


def main():
    if OFF:
        return
    payload = json.loads(sys.stdin.read() or "{}")
    ev = payload.get("hook_event_name") or ""
    # Guards, independent of hooks.json. On SubagentStop — or on any event that
    # fired inside a subagent — the injection lands in that subagent and clobbers
    # its report (see the module docstring).
    if ev == "SubagentStop" or payload.get("agent_id"):
        return
    d = state_dir(payload)
    if not d:
        return

    tool = payload.get("tool_name") or ""
    now = time.time()
    out = None

    with open(d / ".recheck.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        b = load_batch(d)

        if tool in ("Agent", "Task"):
            # Record only. Judging here is judging a batch mid-flight.
            if not b["open"]:
                b.update(open=True, size=0, wave=b.get("wave", 0) + 1)
            b["size"] += 1
            b["last_dispatch_at"] = now
            b["calls_since_dispatch"] = 0
            save_batch(d, b)
            return

        b["calls_since_dispatch"] = b.get("calls_since_dispatch", 0) + 1
        settled = b["open"] and now - float(b["last_dispatch_at"]) >= SETTLE
        if settled:
            b["sizes"] = list(b.get("sizes") or []) + [b["size"]]
            b["open"] = False

        try:
            state = json.loads((d / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            save_batch(d, b)
            return

        findings, suspicious = check(state, b, settled, now)
        reports = delivery_findings(state, now)
        verdict = take_verdict(d, state, now)

        said = set(b.get("said") or [])
        fresh, seen = [], set()
        for fp, text in reports + findings:
            if fp in said or fp in seen:
                continue
            seen.add(fp)
            fresh.append((fp, text, fp.startswith("report:")))

        capture_lessons(payload, fresh)

        if fresh or verdict:
            _, live, starting = census(state, now)
            lines = [t for _, t, is_report in fresh if is_report]
            shape = [t for _, t, is_report in fresh if not is_report]
            if shape or verdict:
                head = f"MAESTRO — {len(live) + len(starting)} agent(s) in flight"
                if starting:
                    head += f" ({len(starting)} still starting)"
                lines.append(f"{head}, {len(lanes(state))} lane(s).")
                lines += [f"- {t}" for t in shape[:5]]
                if verdict:
                    lines.append(f"- second opinion (high confidence): {verdict}")
            b["said"] = list(said | seen)
            out = "\n".join(lines)

        if suspicious and USE_LLM and settled:
            _, live, _ = census(state, now)
            if live:
                spawn_llm(d, state, live, now)
        save_batch(d, b)

    if out:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": ev or "PostToolUse",
                "additionalContext": out,
            },
            "suppressOutput": True,
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if DEBUG:
            print(f"maestro reorchestrate: {e}", file=sys.stderr)
    sys.exit(0)
