#!/usr/bin/env python3
"""Maestro re-check — news the conductor did not already have, or silence.

Wired to PostToolUse (every tool), PostToolUseFailure and TaskCompleted. It
speaks only about things that are true when it says them and that would
change a decision:

  - a subagent report the harness failed to deliver (recovered or missing);
  - an agent with no sign of life — no tool call and no write to its own
    transcript — for STALL_SECONDS;
  - the same agent type failing twice;
  - two live agents writing the same file — the same repo file, whichever
    worktree each one writes it in;
  - another conductor's live agent writing a file this one's agents are
    writing (sessions sharing this project's `.claude/maestro/`);
  - depth 5.

It never speaks on a dispatch call (a batch still landing is not a moment to
judge), never inside a subagent, and never repeats a finding.

What it no longer says, and why
-------------------------------
"Three one-agent dispatches in a row" and "N tool calls since the last
dispatch" were style coaching, not news. In the field they fired on sweep
chunks that are one agent each by design, on dispatches that each waited on
the user's answer to the last, and on the conductor running its own gates —
and every lesson they produced was a rule telling the conductor to ignore
them. The doctrine they enforced lives in the output style. The "agents in
flight" header and the detached Haiku second opinion are gone too: the count
was the least reliable thing on the screen, and every second-opinion verdict
in the field argued confidently for downgrading a tier that was carrying the
work.

Session scoping
---------------
The state dir is resolved from this payload's own `session_id`, never from the
workspace-global `.claude/maestro/current` pointer, which the newest session
owns — reading it made a conductor inherit a concurrent session's agents.

Never wire this to SubagentStop, and never let it run inside a subagent:
additionalContext delivered there lands in the *stopping subagent*, which then
spends its final message answering the re-check instead of returning its
report. Both guards are enforced in main(), independent of hooks.json.

Env:
  MAESTRO_REORCH=0          disable entirely
  MAESTRO_STALL_SECONDS     silence before an agent counts as stalled (600)
  MAESTRO_REPORT_GRACE      seconds to let a resend land before crying loss (5)
  MAESTRO_DEBUG=1           errors to stderr
"""
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import lessons as _lessons
except Exception:
    _lessons = None
try:
    from ledger import last_alive, merge_key, project_root, rel_path, session_ledger, sessions_dir
except Exception:
    def last_alive(state, n):
        return float(n.get("last_activity") or n.get("started") or 0)

    def session_ledger(sid):
        return None

    def project_root(cwd):
        return Path(cwd or ".")

    def rel_path(path, root):
        return str(path)

    def merge_key(path):
        return str(path)

    def sessions_dir():
        return Path("/nonexistent")

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"
OFF = os.environ.get("MAESTRO_REORCH") == "0"
LESSONS_ON = os.environ.get("MAESTRO_LESSONS", "1") != "0"
STALL_SECONDS = float(os.environ.get("MAESTRO_STALL_SECONDS", "600"))
REPORT_GRACE = float(os.environ.get("MAESTRO_REPORT_GRACE", "5"))
PEER_WINDOW = 900            # another session's ledger this stale is not live
LIVE = ("running", "spawning")
ROOT = "root"
# Findings that say something about how the conductor conducted. Delivery and
# stall findings are the harness's or an agent's failure, not the conductor's:
# they were 117 of the 152 candidates ever marked not-a-lesson.
CAPTURE_KINDS = ("collide", "fail", "depth5")


def state_dir(payload):
    """This session's ledger dir — the one its conductor pinned (ledger.py).

    Never the workspace-global `.claude/maestro/current` pointer: the newest
    session owns that, and reading it made a conductor inherit a concurrent
    session's agents.
    """
    sid = (payload.get("session_id") or "")[:8]
    if not sid:
        return None
    d = session_ledger(sid)
    if d is not None and (d / "state.json").is_file():
        return d
    p = Path(payload.get("cwd") or ".").resolve()
    for parent in [p, *p.parents]:
        d = parent / ".claude" / "maestro" / sid
        if (d / "state.json").is_file():
            return d
    return None


def load_said(d):
    try:
        b = json.loads((d / "recheck.json").read_text())
        if isinstance(b, dict):
            return list(b.get("said") or [])
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_said(d, said):
    try:
        tmp = d / "recheck.tmp"
        tmp.write_text(json.dumps({"said": list(said)[-300:]}))
        tmp.replace(d / "recheck.json")
    except OSError:
        pass


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

    The one class that always earns a turn: without it a swallowed report and
    a clean finish are the same frame.
    """
    out = []
    for n in state.get("nodes", {}).values():
        if n.get("id") == ROOT:
            continue
        # A Workflow's agents return to its script, never to the conductor.
        if "workflow-subagent" in (n.get("type"), n.get("name")):
            continue
        st = n.get("report_status")
        if st not in ("recovered", "missing"):
            continue
        # An agent interrupted mid-resend sends the report again a beat later.
        if now - float(n.get("last_stop_at") or 0) < REPORT_GRACE:
            continue
        if st == "recovered":
            out.append((
                f"report:{n.get('id')}:recovered",
                f"REPORT RECOVERED — {who(n)} finished, but the copy delivered to you "
                f"was empty or a bare protocol frame. Its real report, from its own "
                f"transcript, is below. Do not ask it to resend.\n"
                f"{read_report(n)}"))
        else:
            out.append((
                f"report:{n.get('id')}:missing",
                f"REPORT NOT DELIVERED — {who(n)} finished and no report was "
                f"recoverable from its transcript. Ask it to resend, or redo the work; "
                f"do not assume it reported."))
    return out


def check(state, now):
    """Deterministic rules. Returns [(fingerprint, text)]."""
    nodes = [n for n in state.get("nodes", {}).values() if n.get("id") != ROOT]
    live = [n for n in nodes if n.get("status") in LIVE]
    f = []

    # Stalled: no tool call and no transcript write. An idle teammate is
    # waiting for a message, not hung.
    for n in live:
        last = last_alive(state, n) or now
        if float(n.get("idle_at") or 0) >= last:
            continue
        since = now - last
        # Inside one tool call (a 10-minute Bash, a Codex run) there is
        # nothing to write; give it the tool's own timeout on top.
        limit = STALL_SECONDS * 2 if n.get("in_tool_since") else STALL_SECONDS
        if since > limit:
            f.append((f"stall:{n.get('id')}",
                      f"{who(n)}{where(state, n)} has shown no activity for "
                      f"{int(since / 60)}m (no tool call, no transcript write) — "
                      f"check it or stop it."))

    # Repeated failures of the same tier.
    repeat = {}
    for n in nodes:
        if n.get("status") == "failed":
            t = n.get("type") or "agent"     # a None key breaks sorted() below
            repeat[t] = repeat.get(t, 0) + 1
    for t, c in sorted(repeat.items()):
        if c >= 2:
            f.append((f"fail:{t}:{c}",
                      f"{t} has failed {c} times. Escalate to surgeon with both failure "
                      f"transcripts rather than retrying the same tier."))

    # Two live agents writing one file. `writes`, not `files`: agents reading
    # one shared contract is the design. Keyed by repo-relative path, so two
    # builders editing src/x.py in their own worktrees collide; three agents
    # on one file is one line.
    root = project_root(state.get("cwd"))
    seen = {}
    for n in live:
        for key in {merge_key(p) for p in (n.get("writes") or [])}:
            owner = seen.get(key)
            if owner and owner[0] != n.get("id"):
                shown = rel_path(key, root)
                f.append((f"collide:{shown}",
                          f"{owner[1]} and {who(n)} are both live on "
                          f"{shown} — serialize them or the merge conflicts."))
            seen.setdefault(key, (n.get("id"), who(n)))

    # Depth ceiling. Only the hard stop is news.
    if max((n.get("depth", 0) for n in live), default=0) >= 5:
        f.append(("depth5",
                  "Depth 5 reached — nothing below this level may delegate further."))
    return f


def peer_findings(d, state, now):
    """Another conductor's live agent writing a file this session's are.

    Every live registered session counts, not only ones sharing this ledger
    directory — a conductor at a workspace root and one inside a repo of it
    write the same files. Runs only while this session has live writers, so a
    quiet session pays one dict lookup. One finding per peer, listing files.
    """
    live = [n for n in state.get("nodes", {}).values()
            if n.get("id") != ROOT and n.get("status") in LIVE and n.get("writes")]
    if not live:
        return []
    root = project_root(state.get("cwd"))
    mine = {}
    for n in live:
        for p in n["writes"]:
            mine.setdefault(merge_key(p), who(n))
    out = []
    try:
        entries = [f for f in sessions_dir().iterdir() if not f.name.startswith(".")]
    except OSError:
        return out
    for f in entries:
        try:
            sib = Path(f.read_text().strip())
            if not str(sib) or sib == d:
                continue
            sf = sib / "state.json"
            if now - sf.stat().st_mtime > PEER_WINDOW:
                continue
            other = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hits = {}
        for n in other.get("nodes", {}).values():
            if n.get("id") == ROOT or n.get("status") not in LIVE:
                continue
            for p in (n.get("writes") or []):
                k = merge_key(p)
                if k in mine:
                    hits.setdefault(rel_path(k, root), (who(n), mine[k]))
        if not hits:
            continue
        peer = other.get("peer_name")
        name = f'"{peer}"' if peer else f"session {f.name} (run ListAgents to find its name)"
        files = sorted(hits)
        theirs = sorted({v[0] for v in hits.values()})
        out.append((f"peer:{f.name}:{','.join(files)}",
                    f"another conductor ({name}) has {', '.join(theirs)} writing "
                    f"{', '.join(files[:6])}{' …' if len(files) > 6 else ''} — files your "
                    f"agents are writing too. Agree who goes first before either side "
                    f"merges: SendMessage to {name}."))
    return out


def capture_lessons(payload, fresh):
    """Queue conductor-behaviour findings as lesson candidates.

    Best-effort and silent: a capture failure must never change what this
    script prints. Fingerprints collapse to their kind (`collide:/a/b` ->
    `collide`) so the store dedupes repeats of one mechanic.
    """
    if _lessons is None or not LESSONS_ON:
        return
    for fp, text in fresh:
        kind = fp.split(":", 1)[0]
        if kind not in CAPTURE_KINDS:
            continue
        try:
            _lessons.capture_finding(payload.get("session_id"), payload.get("cwd"),
                                     kind, text, tool_use_id=payload.get("tool_use_id"))
        except Exception:
            pass


def main():
    if OFF:
        return
    payload = json.loads(sys.stdin.read() or "{}")
    ev = payload.get("hook_event_name") or ""
    # On SubagentStop — or on any event that fired inside a subagent — the
    # injection lands in that subagent and clobbers its report.
    if ev == "SubagentStop" or payload.get("agent_id"):
        return
    if (payload.get("tool_name") or "") in ("Agent", "Task"):
        return
    d = state_dir(payload)
    if not d:
        return

    now = time.time()
    out = None
    with open(d / ".recheck.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = json.loads((d / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return
        said = set(load_said(d))
        fresh, seen = [], set()
        findings = delivery_findings(state, now) + check(state, now) + peer_findings(d, state, now)
        for fp, text in findings:
            if fp in said or fp in seen:
                continue
            seen.add(fp)
            fresh.append((fp, text))
        if not fresh:
            return
        capture_lessons(payload, fresh)
        reports = [(fp, t) for fp, t in fresh if fp.startswith("report:")]
        shape = [(fp, t) for fp, t in fresh if not fp.startswith("report:")][:5]
        out = "\n".join([t for _, t in reports] + [f"MAESTRO — {t}" for _, t in shape])
        # Only what was shown counts as said; the rest surfaces next call.
        save_said(d, said | {fp for fp, _ in reports + shape})

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
