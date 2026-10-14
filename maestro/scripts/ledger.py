#!/usr/bin/env python3
"""Maestro ledger — turns Claude Code hook events into a live agent tree.

Reads one hook payload as JSON on stdin, appends it to events.jsonl, and folds
it into state.json, which the terminal tree renderer and the board both read.

Never blocks Claude Code: any internal error exits 0 silently (or to the debug
log with MAESTRO_DEBUG=1).
"""
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"
ROOT = "root"

# Subagent name -> tier, used by the tree renderer and the board.
_TIER = {
    "scout": "haiku", "scribe": "haiku",
    "builder": "sonnet", "section-lead": "sonnet",
    "visual-reviewer": "sonnet", "adversary": "sonnet",
    "surgeon": "opus",
    "codex": "haiku",       # the Claude-side wrapper; Codex bills separately
}


# Only these declare `isolation: worktree`, so only these can ever own a
# worktree — which is what makes a worktree name a usable parent signal.
ISOLATED = ("section-lead", "builder", "surgeon", "codex")
# `.claude/worktrees/agent-<agent-id>` — the owning agent's id, and the only
# place a delegating agent's id ever appears (it is on no hook payload).
WT_OWNER = re.compile(r"^agent-([0-9a-z]{6,})$", re.I)
# A running node this quiet is not coming back; stop counting it as in flight.
REAP_SECONDS = float(os.environ.get("MAESTRO_REAP_SECONDS", "900"))
# An unmatched dispatch older than this is not the one that just started. Without
# a ceiling, a worktree created now can adopt a dispatch from half an hour ago
# and inherit the wrong type entirely.
PENDING_TTL = float(os.environ.get("MAESTRO_PENDING_TTL", "600"))
# A report is the RETURN block every agent definition mandates. Keep the whole
# thing on disk, not just the tail the board shows.
RETURN_BLOCK = re.compile(r"(?:^|\n)\s*RETURN:\s*(?:\n|$)")
REPORT_MAX = int(os.environ.get("MAESTRO_REPORT_MAX", "40000"))
REPORT_TAIL_BYTES = 512_000
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")
# The first line of a ListAgents result names this session's own address —
# what other conductors pass as SendMessage `to`. No hook payload carries it.
PEER_NAME = re.compile(r"This session is (.+?) \[([0-9a-f]+)\]")


def is_protocol_frame(txt):
    """A machine envelope — an idle ping, a status frame — not prose.

    What a swallowed report looks like from the conductor's side is exactly
    this: `{"type":"idle_notification","from":...,"idleReason":"available"}`
    where a report should have been. Any bare JSON object carrying a `type` is
    a frame no agent composed, and must never be mistaken for its answer.
    """
    t = (txt or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        obj = json.loads(t)
    except ValueError:
        return False
    return isinstance(obj, dict) and isinstance(obj.get("type"), str)


def substantive(txt):
    """Something an agent actually wrote, as opposed to nothing or a frame."""
    return bool((txt or "").strip()) and not is_protocol_frame(txt)


def looks_like_report(txt):
    return substantive(txt) and bool(RETURN_BLOCK.search(txt))


def tail_records(path):
    """Parsed transcript records from the last REPORT_TAIL_BYTES, newest first."""
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - REPORT_TAIL_BYTES))
            blob = fh.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", "replace").splitlines()
    if size > REPORT_TAIL_BYTES and len(lines) > 1:
        del lines[0]                      # sliced mid-line by the seek
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def transcript_texts(path, limit=12):
    """Assistant prose from a subagent's own transcript, newest first.

    `last_assistant_message` is absent on roughly one SubagentStop in twelve
    (146 of 1804 across the ledgers this was built against), and when it is
    present it is only whatever the agent said *last* — which, after an idle
    ping or a follow-up, is not the report. The transcript still holds it, so
    a lost report is recoverable rather than gone.
    """
    out = []
    for rec in tail_records(path):
        if len(out) >= limit:
            break
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        body = msg.get("content")
        if isinstance(body, str):
            text = body
        elif isinstance(body, list):
            text = "\n".join(b.get("text", "") for b in body
                             if isinstance(b, dict) and b.get("type") == "text")
        else:
            continue
        if text.strip():
            out.append(text.strip())
    return out


def transcript_handback(path):
    """The report a background agent handed back on its final turn, or None.

    Background subagents return through the harness's `SubagentHandback`
    tool: the report is that call's `message`, and the tool's own result says
    it was delivered to the caller. The turn ends on that tool call, so
    SubagentStop carries no `last_assistant_message` and the newest prose in
    the transcript is mid-work narration. Reading prose alone turned every
    such delivery into a false REPORT RECOVERED / NOT DELIVERED (81 of 100
    delivery findings in the ledgers this was fixed against).

    Only a handback that is the agent's newest action counts: a resumed agent
    that stopped without handing back has not delivered on this run.
    """
    for rec in tail_records(path):
        msg = rec.get("message")
        body = msg.get("content") if isinstance(msg, dict) else None
        if rec.get("type") == "user":
            if isinstance(body, list) and body and all(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in body):
                continue                  # the handback's own result
            return None                   # a new prompt: a later run began
        if rec.get("type") != "assistant" or not isinstance(body, list):
            continue
        for b in body:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "SubagentHandback"):
                text = (b.get("input") or {}).get("message")
                return text.strip() if isinstance(text, str) and text.strip() else None
        if any(isinstance(b, dict) and (b.get("type") == "tool_use"
               or (b.get("type") == "text" and (b.get("text") or "").strip()))
               for b in body):
            return None                   # the agent acted after any handback
    return None


def transcript_usage(path, skip_sidechain=False):
    """Token totals from a transcript, deduped by API request.

    One API response can land as several assistant records sharing a
    `requestId`, each carrying the same usage object; only the first per
    request may count or the sum inflates. Sidechain records are a subagent's
    activity mirrored into its caller's transcript — skipped when summing the
    caller itself, or every subagent would be counted twice.
    """
    if not path:
        return None
    tot = {"in": 0, "cw": 0, "cr": 0, "out": 0, "reqs": 0}
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                if skip_sidechain and rec.get("isSidechain"):
                    continue
                u = (rec.get("message") or {}).get("usage")
                if not isinstance(u, dict):
                    continue
                rid = rec.get("requestId") or rec.get("uuid")
                if rid is not None and rid in seen:
                    continue
                seen.add(rid)
                try:
                    tot["in"] += int(u.get("input_tokens") or 0)
                    tot["cw"] += int(u.get("cache_creation_input_tokens") or 0)
                    tot["cr"] += int(u.get("cache_read_input_tokens") or 0)
                    tot["out"] += int(u.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    continue
                tot["reqs"] += 1
    except OSError:
        return None
    return tot if tot["reqs"] else None


def record_usage(n, path, key="usage_size"):
    """Fold a transcript's token totals into a node, once per file size.

    `tokens` is the headline spend — uncached input + cache writes + output.
    Cache reads are an order of magnitude cheaper and stay in the breakdown,
    where the board shows them as the cached share.
    """
    if not path:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size == n.get(key):
        return
    u = transcript_usage(path, skip_sidechain=(key == "usage_size_self"))
    if u:
        n[key] = size
        n["usage"] = u
        n["tokens"] = u["in"] + u["cw"] + u["out"]


def write_report(d, n, text):
    """Durable copy of a report, so delivery failure is recoverable offline."""
    if not d:
        return n.get("report_path")
    try:
        rd = d / "reports"
        rd.mkdir(parents=True, exist_ok=True)
        f = rd / (SAFE_ID.sub("_", str(n.get("id") or "agent"))[:64] + ".md")
        f.write_text(text[:REPORT_MAX])
        return str(f)
    except OSError:
        return n.get("report_path")


def record_stop(n, payload, d, now):
    """Fold one SubagentStop into the node, keeping the best report seen.

    SubagentStop is not once per agent: it re-fires on every stop of the
    subagent's loop — 18 times for a single agent in the ledgers this was built
    against, `stop_hook_active` set on the repeats. So the message the harness
    finally hands the conductor can be an idle frame or a bare "report stands"
    long after the real report went past. Keeping only the newest message threw
    the report away; keeping only the first missed genuine updates.

    Keep the best copy, write it somewhere durable, and record separately
    whether the copy the *conductor* received was the report — that difference
    is the whole of "a swallowed report looks exactly like a clean finish".
    """
    n["stops"] = int(n.get("stops") or 0) + 1
    n["last_stop_at"] = now
    if n.get("status") not in ("done", "failed"):
        n["status"] = "failed" if payload.get("error") else "done"
        n["ended"] = now
    elif payload.get("error"):
        n["status"] = "failed"

    msg = (payload.get("last_assistant_message")
           or payload.get("result") or payload.get("response") or "")
    msg = msg.strip() if isinstance(msg, str) else ""
    delivered = substantive(msg)
    tpath = payload.get("agent_transcript_path")

    # A handback is a delivery the harness already made. Check it once per
    # transcript size, like the prose scan below: stops re-fire.
    if not delivered and tpath:
        try:
            size = os.path.getsize(tpath)
        except OSError:
            size = -1
        if size != n.get("handback_size"):
            n["handback_size"] = size
            handed = transcript_handback(tpath)
            if handed:
                n["result"] = handed[-1200:]
                n["report_path"] = write_report(d, n, handed)
                n["reported_at"] = n.get("reported_at") or now
                n["report_status"] = "delivered"
                record_usage(n, tpath)
                return
        elif n.get("report_status") == "delivered":
            return

    prior = n.get("result") or ""
    best = msg if delivered and (looks_like_report(msg)
                                 or not looks_like_report(prior)) else prior
    # Go to the transcript only when we still have no report frame — and only
    # once per size of it. Stops re-fire up to 18 times, and an agent that
    # never uses a RETURN block (a plain `general-purpose` one) would otherwise
    # re-read half a megabyte inside the ledger lock on every one of them.
    if not looks_like_report(best) and tpath:
        try:
            size = os.path.getsize(tpath)
        except OSError:
            size = -1
        if size != n.get("scanned_size"):
            n["scanned_size"] = size
            texts = transcript_texts(tpath)
            pick = next((t for t in texts if looks_like_report(t)), None)
            if pick is None and not substantive(best):
                pick = next((t for t in texts if substantive(t)), None)
            if pick:
                best = pick

    if substantive(best):
        n["result"] = best[-1200:]
        n["report_path"] = write_report(d, n, best)
        n["reported_at"] = n.get("reported_at") or now

    if delivered and (looks_like_report(msg) or not looks_like_report(best)):
        n["report_status"] = "delivered"
    elif substantive(best):
        # A report exists but the conductor did not get it. Say so out loud;
        # `reorchestrate.py` re-delivers it on the conductor's next turn.
        n["report_status"] = "recovered"
    else:
        n["report_status"] = "missing"

    record_usage(n, tpath)


TEAMS = Path.home() / ".claude" / "teams"
PRUNE = os.environ.get("MAESTRO_INBOX_PRUNE", "1") != "0"
# proper-lockfile's default mechanism is an atomically-created *directory*
# beside the file. Match it exactly, or two writers can interleave.
LOCK_STALE = 10.0
# Only ever removed from a mailbox. Every other frame — permission requests,
# plan approvals, shutdown handshakes, task assignments, and all human-written
# prose — is load-bearing protocol and must be left exactly where it is.
PRUNABLE = ("idle_notification",)
# On TeammateIdle, maestro's hook and the harness's own idle-notification hook
# are two entries in the same Stop chain, in no guaranteed order. If ours runs
# first the frame is not written yet. Rather than lose the race, wait for it —
# bounded, and only when the agent is already retired so there is definitely
# something to take back out.
IDLE_CATCH = float(os.environ.get("MAESTRO_IDLE_CATCH", "1.5"))
# A teammate that has reported is stopped when it goes idle. It stays
# addressable: a message by name revives it with its conversation intact.
KEEP_TEAMMATES = os.environ.get("MAESTRO_KEEP_TEAMMATES") == "1"


def team_dir(payload):
    """`~/.claude/teams/session-<sid8>` — a teammate shares its lead's id."""
    sid = (payload.get("session_id") or "")[:8]
    d = TEAMS / f"session-{sid}"
    return d if sid and d.is_dir() else None


def lead_inbox(d):
    """The lead's mailbox path, from the team's own config."""
    try:
        cfg = json.loads((d / "config.json").read_text())
        lead = cfg.get("leadAgentId")
        name = next((m.get("name") for m in cfg.get("members") or []
                     if m.get("agentId") == lead), None) or "team-lead"
    except (OSError, json.JSONDecodeError, TypeError):
        name = "team-lead"
    f = d / "inboxes" / f"{name}.json"
    return f if f.is_file() else None


class _Lock:
    """proper-lockfile-compatible: mkdir to acquire, rmdir to release."""

    def __init__(self, target):
        self.p = Path(str(target) + ".lock")
        self.held = False

    def __enter__(self):
        for _ in range(3):
            try:
                self.p.mkdir()
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > LOCK_STALE:
                        self.p.rmdir()
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
            except OSError:
                break
        return self

    def __exit__(self, *_):
        if self.held:
            self.held = False       # released once; a re-entry must not rmdir twice
            try:
                self.p.rmdir()
            except OSError:
                pass
        return False


def ended_on_report(n):
    """True when the agent's last word is a RETURN report (full copy first;
    `result` keeps only the tail, which can miss the RETURN: header)."""
    txt = ""
    if n.get("report_path"):
        try:
            txt = Path(n["report_path"]).read_text()
        except OSError:
            txt = ""
    return looks_like_report(txt or n.get("result") or "")


def who_named(n, payload):
    return (n or {}).get("name") or payload.get("teammate_name") or "teammate"


def retired(n):
    """Has this agent already said what it came to say, or already stopped?"""
    if not n:
        return False
    if n.get("report_status") in ("delivered", "recovered"):
        return True
    return n.get("status") in ("done", "failed", "orphaned")


# A subagent is never woken when background work it started finishes: the
# completion notice queues until something else wakes it. One teammate sat idle
# 51 minutes on a test run that had passed; the notice, stamped 15:43, reached
# its transcript only when the lead messaged it at 16:34. Monitor events and a
# background agent's handback queue the same way. So inside a subagent all three
# are denied, and a teammate about to idle on one it started anyway is sent back
# to wait for it.
BG_GUARD = os.environ.get("MAESTRO_BG_GUARD", "1") != "0"
LONG_LIVED = re.compile(r"#\s*maestro:\s*long-lived", re.I)
BG_BLOCK_CAP = 3
BG_LAUNCH = re.compile(r"\s*Command running in background with ID: ([A-Za-z0-9_-]+)")
BG_OUTPUT = re.compile(r"Output is being written to: (\S+?\.output)")
MONITOR_LAUNCH = re.compile(r"\s*Monitor started \(task ([A-Za-z0-9_-]+)")
BG_MARKS = ("backgroundTaskId", "running in background with ID", "async_launched",
            "Monitor started (task")
# The trailer the harness appends to a task's output file when it ends.
BG_DONE = r"\[exited with code -?[0-9]+\]|\[killed\]"
BG_STOP = ("TaskStop", "KillShell", "KillBash")
BG_DENY = (
    "Maestro: background commands are blocked inside subagents. A subagent is not "
    "woken when a background command finishes, so ending your turn to wait on one "
    "stalls you until someone messages you. Run it in the foreground instead: Bash "
    "with timeout up to 600000 (10 minutes), and split a longer suite into parts "
    "that each fit. To wait for a condition, run the until-loop itself in the "
    "foreground: until grep -q 'Ready' app.log; do sleep 5; done. To run several "
    "commands at once, background them inside one foreground command: "
    "(cmd_a & cmd_b & wait). For a long-lived process you will NOT wait on (a dev "
    "server, Metro, a log tail), end the command with the comment "
    "# maestro: long-lived")
MONITOR_DENY = (
    "Maestro: Monitor is blocked inside subagents. A subagent is not woken by "
    "Monitor events, so ending your turn to wait on one stalls you until someone "
    "messages you. Wait in the foreground instead: one Bash call with an until-loop "
    "on the condition and timeout up to 600000 (10 minutes), for example "
    "until grep -q 'Ready' app.log; do sleep 5; done. Run it again if the wait "
    "needs longer.")
# Current harnesses launch every Agent call async whatever the flag says, so
# the reason cannot promise a foreground dispatch; it says how to wait instead.
AGENT_DENY = (
    "Maestro: background agents are blocked inside subagents. A subagent is not "
    "woken when a background agent finishes, so ending your turn to wait on one "
    "stalls you until someone messages you. Dispatch without run_in_background; "
    "several Agent calls in one message run in parallel. If a dispatch still comes "
    "back as launched, do not end your turn until each child has handed back: wait "
    "in the foreground with one Bash call, timeout up to 600000, "
    "until grep -q '\"name\":\"SubagentHandback\"' <the output_file its launch "
    "result names>; do sleep 5; done")


def bg_guard(payload):
    """The PreToolUse deny for a subagent's background work, or None.

    Only a subagent's call carries `agent_id`; the conductor's never does and
    is never touched. Payload checks only: this runs on every tool call. The
    long-lived marker frees a Bash command only — a Monitor or a background
    agent always reports back through a notice that will not wake the caller.
    """
    if not BG_GUARD or payload.get("hook_event_name") != "PreToolUse":
        return None
    tool = payload.get("tool_name")
    if tool not in ("Bash", "Monitor", "Agent", "Task") or not payload.get("agent_id"):
        return None
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    bg = ti.get("run_in_background") in (True, "true")
    if tool == "Monitor":
        reason = MONITOR_DENY
    elif tool == "Bash" and bg and not LONG_LIVED.search(str(ti.get("command") or "")):
        reason = BG_DENY
    elif tool in ("Agent", "Task") and bg:
        reason = AGENT_DENY
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _content(rec):
    msg = rec.get("message") if isinstance(rec, dict) else None
    body = msg.get("content") if isinstance(msg, dict) else None
    return body if isinstance(body, list) else None


def bg_launch(text, tur):
    """The background work one tool result started, or None."""
    m = BG_LAUNCH.match(text)
    if m or tur.get("backgroundTaskId"):
        out = BG_OUTPUT.search(text)
        return {"kind": "bash", "id": m.group(1) if m else str(tur["backgroundTaskId"]),
                "output": out.group(1) if out else None}
    # Only the structured result counts for an agent: its launch text also
    # turns up in the output of anything that reads a transcript.
    if tur.get("status") == "async_launched" and tur.get("agentId"):
        return {"kind": "agent", "id": str(tur["agentId"]), "output": tur.get("outputFile")}
    m = MONITOR_LAUNCH.match(text)
    if m or ("taskId" in tur and "timeoutMs" in tur):
        return {"kind": "monitor", "id": m.group(1) if m else str(tur["taskId"]),
                "output": None}
    return None


def pending_background(path):
    """Background work a transcript started and never heard back from.

    Each launch is a tool result:
      bash     "Command running in background with ID: X", or a
               `backgroundTaskId` (a command the user pushed to the background
               lands the same way). Settled by any `<task-id>X</task-id>`.
      agent    `status: async_launched` with an `agentId`. Settled the same way;
               its notice carries the agent id.
      monitor  "Monitor started (task X". Every event is a notice with that id
               too, so only one carrying a `<status>` (the stream ended, expired
               or was stopped) settles it.
    TaskStop settles any of them. A Bash command marked long-lived was never
    meant to be waited on. Returns [{"kind", "id", "what", "output",
    "command"}], oldest first.
    """
    try:
        blob = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not any(m in blob for m in BG_MARKS):
        return []                         # the common case: one substring scan
    lines = blob.splitlines()
    launched = []
    for line in lines:
        if not any(m in line for m in BG_MARKS):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        body = _content(rec)
        if body is None or rec.get("type") != "user":
            continue
        results = [b for b in body if isinstance(b, dict) and b.get("type") == "tool_result"]
        tur = rec.get("toolUseResult") if len(results) == 1 else None
        tur = tur if isinstance(tur, dict) else {}
        for b in results:
            text = b.get("content")
            launch = bg_launch(text if isinstance(text, str) else json.dumps(text), tur)
            if launch:
                launch["use"] = b.get("tool_use_id")
                launched.append(launch)

    def settled(t):
        tag = f"<task-id>{t['id']}</task-id>"
        if t["kind"] != "monitor":
            return tag in blob
        return re.search(re.escape(tag) + r"(?:(?!</task-notification>).)*?<status>",
                         blob, re.S) is not None

    pending = [t for t in launched if not settled(t)]
    if not pending:
        return []
    # What each one ran, and whether the agent stopped it since.
    want = {t["use"]: t for t in pending if t["use"]}
    stopped = set()
    for line in lines:
        if '"tool_use"' not in line or not (any(u in line for u in want)
                                            or any(s in line for s in BG_STOP)):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for b in _content(rec) or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            ti = b.get("input") if isinstance(b.get("input"), dict) else {}
            if b.get("name") in BG_STOP:
                stopped.update(str(v) for v in ti.values())
            elif b.get("id") in want:
                want[b["id"]]["input"] = ti
    out = []
    for t in pending:
        ti = t.get("input") or {}
        cmd = str(ti.get("command") or "")
        if t["id"] in stopped or ti.get("name") in stopped:
            continue
        if t["kind"] == "bash" and LONG_LIVED.search(cmd):
            continue
        out.append({"kind": t["kind"], "id": t["id"], "output": t["output"], "command": cmd,
                    "what": str(ti.get("description") or cmd[:120] or "background work")})
    return out


def teammate_transcript(state, payload, aid):
    """A teammate's own transcript. TeammateIdle's `transcript_path` is the
    session's, so the teammate's sits beside it, as for any subagent."""
    cands = [agent_transcript(state, aid)]
    tp = payload.get("transcript_path")
    if tp:
        p = Path(tp)
        cands.append(p if p.name == f"agent-{aid}.jsonl"
                     else p.with_suffix("") / "subagents" / f"agent-{aid}.jsonl")
    return next((c for c in cands if c and c.is_file()), None)


def bg_finished(out):
    try:
        with open(out, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 256))
            return bool(re.search(BG_DONE, fh.read().decode("utf-8", "replace")))
    except OSError:
        return False


def bg_item(t, scratch, tpath):
    """Feedback lines that settle one piece of pending background work."""
    q = shlex.quote
    wait = "one Bash call with timeout 600000 (if the wait times out, run it again)"
    if t["kind"] == "agent":
        # A background agent's output file links to its transcript, which ends
        # in a SubagentHandback call when it reports (362 of 372 unnamed ones in
        # the field). A named one reports by message instead, and a message does
        # wake an idle teammate.
        out = t["output"] or str(Path(tpath).parent / f"agent-{t['id']}.jsonl")
        return [f'- background agent "{t["what"]}" ({t["id"]}), output_file: {out}',
                f"  Wait for its handback in the foreground, {wait}; its report then "
                "arrives as a notification:",
                f"  until grep -q '\"name\":\"SubagentHandback\"' {q(out)}; do sleep 5; done",
                "  A named agent reports by message instead: ask it with SendMessage to "
                "message you when it is done (a message wakes you; a notification does "
                "not). If it never finishes, stop it with TaskStop."]
    if t["kind"] == "monitor":
        return [f'- Monitor "{t["what"]}" (task {t["id"]})',
                "  Its events do not wake you either. Stop it with TaskStop, then wait "
                f"for what it watches in the foreground with an until-loop, {wait}. "
                f"It watched: {(t['command'] or t['what'])[:200]}"]
    out = t["output"]
    if not out and scratch:
        guess = Path(scratch).parent / "tasks" / f"{t['id']}.output"
        out = str(guess) if guess.is_file() else None
    if not out:
        return [f'- "{t["what"]}" (task {t["id"]}), output: unknown',
                "  Stop it with TaskStop and rerun the command in the foreground, "
                "Bash timeout up to 600000."]
    state = "finished" if bg_finished(out) else "still running"
    return [f'- "{t["what"]}" (task {t["id"]}), {state}, output: {out}',
            f"  Wait for it in the foreground, {wait}:",
            f"  until grep -qE '{BG_DONE}' {q(out)}; do sleep 5; done; tail -40 {q(out)}",
            "  Or stop it with TaskStop and rerun the command in the foreground."]


def bg_hold(state, payload, aid, path, lead, exempt=None):
    """Instructions that settle `aid`'s pending background work, or None.

    Each pending task holds an agent back at most BG_BLOCK_CAP times, counted
    across both hooks that hold, so a wait that cannot finish is let go rather
    than looped.
    """
    pending = pending_background(path) if path else []
    if not pending or (exempt and exempt(path)):
        return None
    blocks = state.setdefault("bg_blocks", {})
    due = [t for t in pending if blocks.get(f"{aid}/{t['id']}", 0) < BG_BLOCK_CAP]
    if not due:
        return None
    for t in due:
        k = f"{aid}/{t['id']}"
        blocks[k] = blocks.pop(k, 0) + 1          # re-inserted: newest last
    while len(blocks) > 200:
        blocks.pop(next(iter(blocks)))
    scratch = payload.get("scratchpad_dir")
    rows = [line for t in due for line in bg_item(t, scratch, path)]
    return (lead + "\n" + "\n".join(rows) + "\n"
            "Then read the result, finish the work and report.")


def bg_backstop(state, payload, mate):
    """TeammateIdle: feedback that keeps a teammate from idling on background
    work, or None.

    Judged on the teammate's own last words, not on `retired`: the SubagentStop
    that precedes TeammateIdle marks any final message delivered, "Waiting for
    the full-suite summary." included. A last word that is a RETURN report is
    left to the retire path; one that is a question is woken by the answer,
    which drains the queued notice with it.
    """
    if not BG_GUARD:
        return None
    aid = payload.get("agent_id") or (mate or {}).get("id")
    if not aid or aid == ROOT:
        return None

    def answered(path):
        last = next(iter(transcript_texts(path, limit=1)), "")
        return looks_like_report(last) or last.rstrip(" \t\n\"'`)*_]").endswith("?")

    return bg_hold(state, payload, aid, teammate_transcript(state, payload, aid),
                   "Maestro: do not go idle yet. You started background work and its "
                   "completion notice has not reached you. Nothing that finishes in the "
                   "background wakes an idle teammate, so you would sit stalled until "
                   "someone messages you.", exempt=answered)


def mark_teammate(state, aid, team):
    if not aid:
        return
    mates = state.setdefault("teammates", {})
    mates[str(aid)] = team or ""
    while len(mates) > 200:
        mates.pop(next(iter(mates)))


def stop_backstop(state, payload):
    """SubagentStop: the block reason that keeps a subagent from returning while
    background work it started is pending, or None.

    A teammate is TeammateIdle's: SubagentStop fires on every teammate turn end
    too, just before TeammateIdle, and two hooks holding one stop would spend
    the cap twice. Teammates are known from the spawn result
    (`teammate_spawned`) and from any TeammateIdle. No exemption for last
    words here: a plain subagent's last word is its report, and a report sent
    while its children still run is the failure itself. `stop_hook_active`
    means this stop is already a continuation a stop hook asked for.
    """
    if not BG_GUARD or payload.get("stop_hook_active") in (True, "true"):
        return None
    aid = payload.get("agent_id")
    if not aid or aid == ROOT or aid in (state.get("teammates") or {}):
        return None
    tp = payload.get("agent_transcript_path")
    path = Path(tp) if tp and Path(tp).is_file() else teammate_transcript(state, payload, aid)
    return bg_hold(state, payload, aid, path,
                   "Maestro: do not stop yet. You started background work and its "
                   "completion notice has not reached you. Once you stop, nothing that "
                   "finishes in the background reaches you, so its result is lost and "
                   "your report is incomplete.")


def prune_inbox(state, payload, deadline=0.0):
    """Drop stale idle pings out of the lead's mailbox before it reads them.

    An idle notification is written by a `Stop` hook running inside the
    *teammate's* session, straight into the lead's inbox file. By the time the
    lead reads it, the same agent's report has usually already arrived and the
    conductor may already have stopped it — so the frame is stale by
    definition, and it is the most expensive thing in the queue: four fields of
    JSON that no human wrote, wrapped in the ~120-word cross-session security
    preamble that exists for peer prose, costing a whole main-loop turn on a
    very large context to read nothing.

    The ledger already knows which agents have delivered and which have
    stopped, so it can answer the only question that matters: would this ping
    tell the conductor something it does not already know? If not, it never
    reaches the model.

    Removals are restricted to unread `idle_notification` frames from agents
    this session dispatched and that are already retired. Anything unexpected
    about the file — not a list, an entry that is not a dict, a frame type not
    on the allow-list — and the whole pass is abandoned untouched.
    """
    if not PRUNE:
        return
    d = team_dir(payload)
    if not d:
        return
    f = lead_inbox(d)
    if not f:
        return
    by_name = {}
    for n in state.get("nodes", {}).values():
        if n.get("name"):
            by_name.setdefault(n["name"], n)
    # Cheap gate. Hooks fire on every tool call, and the harness holds this same
    # lock whenever a teammate writes; taking it thousands of times to find
    # nothing to do is pure contention. Two things can create work: a frame
    # arriving (the file's mtime moves) or an agent retiring, which makes a
    # frame already sitting there droppable without the file changing at all.
    # Gate on both — the first version watched only the mtime and let every
    # ping from an agent that retired after its ping landed straight through.
    # Skipped while waiting on TeammateIdle, where the point is that the frame
    # has not been written yet.
    try:
        gate = (f.stat().st_mtime_ns, sum(1 for n in by_name.values() if retired(n)))
    except OSError:
        return
    if not deadline and state.get("inbox_seen") == list(gate):
        return
    state["inbox_seen"] = list(gate)
    with _Lock(f) as lk:
        if not lk.held:
            return                      # someone else owns it; try again next event
        try:
            msgs = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(msgs, list) or not all(isinstance(m, dict) for m in msgs):
            return
        keep, dropped = [], []
        for m in msgs:
            if m.get("read"):
                keep.append(m)
                continue
            body = m.get("text")
            frame = None
            if isinstance(body, str) and body.lstrip().startswith("{"):
                try:
                    o = json.loads(body)
                    if isinstance(o, dict) and isinstance(o.get("type"), str):
                        frame = o
                except ValueError:
                    frame = None
            if not frame or frame.get("type") not in PRUNABLE:
                keep.append(m)
                continue
            # A teammate's report travels *inside* its idle frame (`result`),
            # and so does a failure. Pruning those deleted the report itself:
            # one field session lost three of seven evaluations this way.
            if frame.get("result") or frame.get("failureReason"):
                keep.append(m)
                continue
            if retired(by_name.get(m.get("from"))):
                dropped.append({"from": m.get("from"), "at": time.time(),
                                "reason": frame.get("idleReason") or "available"})
            else:
                keep.append(m)          # went idle without reporting: that earns a turn
        if not dropped:
            if time.time() < deadline:
                lk.__exit__()           # do not hold the lock while waiting
                time.sleep(0.05)
                return prune_inbox(state, payload, deadline)
            return
        try:
            tmp = f.with_suffix(".maestro-tmp")
            tmp.write_text(json.dumps(keep, indent=2))
            tmp.replace(f)
            state["inbox_seen"] = [f.stat().st_mtime_ns, gate[1]]
        except OSError:
            return
    state.setdefault("pruned", []).extend(dropped)
    del state["pruned"][:-100]


def bare(agent_type):
    """`maestro:scout` -> `scout`. Plugin agents arrive namespaced."""
    return (agent_type or "").split(":")[-1]


def canon_type(agent_type):
    """`scout-apiclient` -> `scout`. Nested dispatches arrive *named*, not typed.

    A subagent that delegates gets `agent_type` set to the name its caller
    chose, not the agent type — so the tier lookup misses and a haiku scout
    reads as sonnet on the board. Recover the type from the leading segment
    when it names a real agent, and leave genuinely unknown names alone.
    """
    t = bare(agent_type)
    if t in _TIER:
        return t
    head = t.split("-")[0]
    return head if head in _TIER else t


def tier_of(agent_type, model=None):
    m = (model or "").lower()
    for t in ("haiku", "sonnet", "opus"):
        if t in m:
            return t
    return _TIER.get(canon_type(agent_type), "sonnet")


def sessions_dir():
    o = os.environ.get("MAESTRO_SESSIONS_DIR")
    return Path(o) if o else Path.home() / ".claude" / "maestro" / "sessions"


def project_root(cwd):
    """The nearest directory above `cwd` holding `.git` or `.claude` — where
    the board, notify, the statusline and /tree look for a ledger."""
    p = Path(cwd or os.getcwd()).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists() or (parent / ".claude").is_dir():
            return parent
    return p


def merge_key(path):
    """The file a write lands on at merge time: its real path with every
    `.claude/worktrees/<name>` segment removed. Two checkouts of one repo —
    and two sessions rooted at different levels of one workspace — agree on
    it. Comparing raw paths meant builders in their own worktrees never
    collided. Symlinks are resolved (macOS /var and /tmp live in /private).
    """
    p = os.path.realpath(str(path)).replace("\\", "/")
    while "/.claude/worktrees/" in p:
        base, rest = p.split("/.claude/worktrees/", 1)
        p = base.rstrip("/") + "/" + (rest.split("/", 1)[1] if "/" in rest else "")
    return p


def rel_path(path, root):
    """`merge_key` shown relative to `root`, for messages."""
    p = merge_key(path)
    r = os.path.realpath(str(root)).replace("\\", "/").rstrip("/") + "/"
    return p[len(r):] if p.startswith(r) else p


def outside_worktrees(cwd):
    """`R/.claude/worktrees/<name>/...` -> `R`; anything else unchanged."""
    parts = Path(cwd or os.getcwd()).resolve().parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return Path(*parts)


def session_ledger(sid):
    """The ledger dir this session's conductor pinned, or None.

    One session, one ledger. Resolving it from each hook's cwd gave every
    builder a private ledger inside its worktree (a worktree root has its own
    `.git`): the conductor never saw those agents, and a start and stop
    landing in different ledgers left phantom "running" agents. So the
    conductor's first event pins the location in a registry, and every later
    event — any agent, any cwd, a `cd ..` included — reads it back.
    """
    try:
        d = Path((sessions_dir() / sid).read_text().strip())
    except OSError:
        return None
    return d if d.is_dir() else None


def _register(sid, d, ev):
    reg = sessions_dir()
    try:
        reg.mkdir(parents=True, exist_ok=True)
        tmp = reg / f".{sid}.tmp"
        tmp.write_text(str(d))
        tmp.replace(reg / sid)
        if ev == "SessionStart":          # keep the registry small
            cutoff = time.time() - 30 * 86400
            for f in reg.iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink()
    except OSError:
        pass


def home(payload):
    """Per-session state directory, inside the project so worktrees can find it."""
    sid = (payload.get("session_id") or "nosession")[:8]
    cwd = payload.get("cwd") or os.getcwd()
    d = session_ledger(sid)
    # A fresh process re-pins: `claude --resume` from another directory gets
    # its ledger where it now runs, as it did before the registry existed.
    if (payload.get("hook_event_name") == "SessionStart" and not payload.get("agent_id")
            and payload.get("source") in ("startup", "resume")):
        d = None
    if d is None:
        if payload.get("agent_id"):
            # Nothing pinned (a session from before the registry). A subagent
            # in a worktree belongs to the project, not to its checkout.
            d = project_root(outside_worktrees(cwd)) / ".claude" / "maestro" / sid
        else:
            d = project_root(cwd) / ".claude" / "maestro" / sid
            d.mkdir(parents=True, exist_ok=True)
            _register(sid, d, payload.get("hook_event_name"))
    base = d.parent.parent.parent
    d.mkdir(parents=True, exist_ok=True)
    # Pointer so `/board` and the tree renderer find the newest session fast.
    try:
        (Path(base) / ".claude" / "maestro" / "current").write_text(str(d))
    except OSError:
        pass
    # A spawn from the board leaves a name and colour waiting. Claim it once,
    # so the rail item is already labelled before the first tool call lands.
    pend = Path(base) / ".claude" / "maestro" / "pending-meta.json"
    try:
        if pend.is_file() and not (d / "meta.json").is_file():
            if time.time() - pend.stat().st_mtime < 300:
                (d / "meta.json").write_text(pend.read_text())
            pend.unlink()
    except OSError:
        pass
    return d


def blank(payload):
    return {
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "started": time.time(),
        "updated": time.time(),
        "nodes": {
            ROOT: {
                "id": ROOT, "parent": None, "type": "conductor", "model": "opus",
                "status": "running", "description": "conducting", "depth": 0,
                "started": time.time(), "ended": None, "tools": {},
                "last_tool": None, "last_file": None, "files": [],
                "worktree": None, "tokens": 0,
            }
        },
        "pending": [],      # spawns seen on PreToolUse, not yet matched to a SubagentStart
        "worktrees": {},
        "waves": [],
        "notes": [],
        "shots": [],
        "needs_input": False,
    }


def node(state, nid, **kw):
    n = state["nodes"].get(nid)
    if not n:
        return None
    n.update(kw)
    return n


def depth_of(state, nid):
    d, seen = 0, set()
    while nid and nid not in seen:
        seen.add(nid)
        n = state["nodes"].get(nid)
        if not n or not n.get("parent"):
            break
        nid = n["parent"]
        d += 1
    return d


def is_ancestor(state, anc, nid, limit=12):
    """Is `anc` somewhere above `nid`? Guards against building a cycle."""
    cur, guard = state["nodes"].get(nid), 0
    while cur and guard < limit:
        p = cur.get("parent")
        if p == anc:
            return True
        cur, guard = state["nodes"].get(p), guard + 1
    return False


def adopt_worktree_owner(state, pid, now):
    """Materialise the agent that owns worktree `agent-<pid>`.

    A delegating agent's id appears on no hook payload — only in the path of
    the worktree it runs in — so its node has to be built from the dispatch we
    recorded in `pending` when the conductor launched it. Only worktree-isolated
    types can own a worktree, so only those are candidates.
    """
    match = next((p for p in state["pending"]
                  if p["type"] in ISOLATED and now - p.get("at", 0) <= PENDING_TTL), None)
    if match:
        state["pending"].remove(match)
    state["nodes"][pid] = {
        "id": pid, "parent": (match or {}).get("parent") or ROOT,
        "type": (match or {}).get("type") or "agent",
        "model": (match or {}).get("model") or tier_of((match or {}).get("type")),
        "status": "running", "description": (match or {}).get("description", ""),
        "prompt": (match or {}).get("prompt", ""),
        "depth": 1, "started": now, "ended": None, "tools": {},
        "last_tool": None, "last_file": None, "files": [],
        "worktree": f"agent-{pid}", "tokens": 0,
        "background": (match or {}).get("background", False),
        "inferred": True,      # reconstructed, never seen on a payload
    }


def link_by_worktree(state, now):
    """Recover the parentage the hook payloads do not carry.

    `PreToolUse(Agent)` has no `agent_id`, so the dispatching agent is anonymous
    and every node would hang off root forever — the depth-5 tree would be
    permanently flat no matter how deep the orchestra actually goes. But a
    delegating agent runs in its own worktree named `agent-<its id>`, and the
    children it spawns inherit that working directory. So the worktree a node
    sits in names its parent. That is the only parent signal these hooks expose.
    """
    for n in list(state["nodes"].values()):
        if n["id"] == ROOT:
            continue
        m = WT_OWNER.match(str(n.get("worktree") or ""))
        if not m:
            continue
        pid = m.group(1)
        if pid == n["id"] or n.get("parent") == pid:
            continue
        if pid not in state["nodes"]:
            adopt_worktree_owner(state, pid, now)
        if pid in state["nodes"] and not is_ancestor(state, n["id"], pid):
            n["parent"] = pid

    # A reconstructed parent was never observed running, so it must not be
    # invented as live — that would inflate the census and hold the caffeinate
    # assertion open forever. Take its liveness from the children it owns.
    for p in state["nodes"].values():
        if not p.get("inferred"):
            continue
        kids = [k for k in state["nodes"].values() if k.get("parent") == p["id"]]
        if not kids:
            continue
        p["last_activity"] = max(
            float(k.get("last_activity") or k.get("started") or 0) for k in kids)
        alive = any(k.get("status") in ("running", "spawning") for k in kids)
        if not alive and p.get("status") in ("running", "spawning"):
            p["status"] = "done"
            p["ended"] = p.get("ended") or p["last_activity"]


def agent_transcript(state, aid):
    """`<dir>/<session>.jsonl` -> `<dir>/<session>/subagents/agent-<aid>.jsonl`.

    No hook payload names a running subagent's own transcript (SubagentStart
    carries only the session's), so it is derived from the session transcript
    the conductor's own hooks report.
    """
    t = state.get("transcript")
    if not t or not aid or aid == ROOT:
        return None
    return Path(t).with_suffix("") / "subagents" / f"agent-{aid}.jsonl"


def last_alive(state, n):
    """Latest sign of life: a tool call, or a write to the agent's transcript.

    An agent reasoning, or writing one large file, makes no tool call for
    minutes while its transcript keeps growing. Judging it by tool calls
    alone called live opus agents stalled.
    """
    last = float(n.get("last_activity") or n.get("started") or 0)
    p = agent_transcript(state, n.get("id"))
    if p:
        try:
            last = max(last, p.stat().st_mtime)
        except OSError:
            pass
    return last


def reap_stale(state, now):
    """Retire nodes whose SubagentStop never landed.

    Nested agents do not reliably emit a stop we can match, so without this a
    node sits `running` for the life of the session — inflating the live count,
    and holding the caffeinate assertion open. Reaped well after the
    re-orchestrator's 300s stall warning, so a real stall is still reported.
    """
    for n in state["nodes"].values():
        if n["id"] == ROOT or n.get("status") not in ("running", "spawning"):
            continue
        last = last_alive(state, n) or now
        if now - last > REAP_SECONDS:
            n["status"] = "orphaned"
            n["ended"] = n.get("ended") or last


def lane_of(state, nid):
    """A lane is the direct child of the conductor that owns this subtree."""
    if nid == ROOT:
        return None
    cur, guard = state["nodes"].get(nid), 0
    while cur and cur.get("parent") and cur["parent"] != ROOT and guard < 8:
        cur = state["nodes"].get(cur["parent"])
        guard += 1
    return (cur or {}).get("id", nid)


def whoami(state, payload):
    """The node this hook fired inside. Unknown ids are adopted under root."""
    aid = payload.get("agent_id")
    if not aid:
        return ROOT
    if aid not in state["nodes"]:
        state["nodes"][aid] = {
            "id": aid, "parent": ROOT,
            "type": canon_type(payload.get("agent_type")) or "agent",
            # Keep what the caller actually named it; the type above is derived.
            "name": bare(payload.get("agent_type")) or None,
            "model": tier_of(payload.get("agent_type")),
            "status": "running", "description": "", "depth": 1,
            "started": time.time(), "ended": None, "tools": {},
            "last_tool": None, "last_file": None, "files": [],
            "worktree": None, "tokens": 0,
        }
    return aid


# Tools that change a file. Only two of these on one path can conflict at
# merge; reads of a shared contract are the design, not a collision.
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def touch_file(n, path, wrote=False):
    if not path:
        return
    if path not in n["files"]:
        n["files"].append(path)
        del n["files"][:-40]
    if wrote:
        writes = n.setdefault("writes", [])
        if path not in writes:
            writes.append(path)
            del writes[:-40]
    n["last_file"] = path


def apply(state, payload, d=None):
    ev = payload.get("hook_event_name") or ""
    now = time.time()
    state["updated"] = now

    if ev == "SessionStart":
        state["nodes"][ROOT]["status"] = "running"
        state["nodes"][ROOT]["started"] = now

    elif ev == "PreToolUse":
        tool = payload.get("tool_name") or ""
        ti = payload.get("tool_input") or {}
        me = whoami(state, payload)
        if tool in ("Agent", "Task"):
            # Authoritative parent link: this fires in the *caller*.
            atype = bare(ti.get("subagent_type") or ti.get("agentType")) or "agent"
            state["pending"].append({
                "parent": me,
                "type": atype,
                "description": (ti.get("description") or ti.get("prompt") or "")[:160],
                # The full brief, not just the label — the board shows it.
                "prompt": (ti.get("prompt") or "")[:800],
                "model": tier_of(atype, ti.get("model")),
                "background": bool(ti.get("run_in_background")),
                "at": now,
            })
            del state["pending"][:-64]
        else:
            n = state["nodes"][me]
            n["last_tool"] = tool
            n["in_tool_since"] = now     # a long Bash call is not a stall
            if tool == "Bash":
                n["description"] = (ti.get("description") or ti.get("command") or "")[:120]

    elif ev in ("PostToolUse", "PostToolUseFailure"):
        me = whoami(state, payload)
        n = state["nodes"][me]
        tool = payload.get("tool_name") or "?"
        n.pop("in_tool_since", None)
        n["tools"][tool] = n["tools"].get(tool, 0) + 1
        n["last_tool"] = tool
        ti = payload.get("tool_input") or {}
        touch_file(n, ti.get("file_path") or ti.get("notebook_path"),
                   wrote=tool in WRITE_TOOLS and ev == "PostToolUse")
        resp = payload.get("tool_response")
        if (tool in ("Agent", "Task") and isinstance(resp, dict)
                and resp.get("status") == "teammate_spawned"):
            # The one payload that says an agent is a teammate before its
            # first stop; the SubagentStop backstop leaves those to TeammateIdle.
            mark_teammate(state, resp.get("agent_id") or resp.get("agentId"),
                          resp.get("team_name"))
        if ev == "PostToolUseFailure":
            n["tools"]["!failed"] = n["tools"].get("!failed", 0) + 1
        elif tool == "ListAgents" and me == ROOT:
            resp = payload.get("tool_response")
            text = resp.get("listing", "") if isinstance(resp, dict) else str(resp or "")
            m = PEER_NAME.search(text)
            if m:
                state["peer_name"], state["peer_ref"] = m.group(1), m.group(2)

    elif ev == "SubagentStart":
        aid = payload.get("agent_id")
        atype = payload.get("agent_type") or "agent"
        # Match the oldest pending spawn of this type; fall back to any pending.
        match = next((p for p in state["pending"] if p["type"] == canon_type(atype)), None)
        if match is None:
            match = state["pending"][0] if state["pending"] else None
        if match:
            state["pending"].remove(match)
        parent = match["parent"] if match else ROOT
        if aid == parent or not aid:
            aid = f"{atype}-{int(now*1000)%10**8}"
        state["nodes"][aid] = {
            "id": aid, "parent": parent, "type": canon_type(atype),
            # Keep the name the caller chose. It is how the conductor addresses
            # this agent, so it is also how a notice about it has to name it —
            # "scout finished" is unusable when five scouts are out.
            "name": bare(atype) or None,
            "model": (match or {}).get("model") or tier_of(atype),
            "status": "running",
            "description": (match or {}).get("description", ""),
            "prompt": (match or {}).get("prompt", ""),
            "depth": 0, "started": now, "ended": None, "tools": {},
            "last_tool": None, "last_file": None, "files": [],
            "worktree": None, "tokens": 0,
            "background": (match or {}).get("background", False),
        }
        state["nodes"][aid]["depth"] = depth_of(state, aid)

    elif ev == "SubagentStop":
        aid = payload.get("agent_id")
        n = state["nodes"].get(aid)
        if n is None:
            atype = payload.get("agent_type")
            n = next((v for v in state["nodes"].values()
                      if v["type"] == canon_type(atype) and v["status"] == "running"), None)
        if n:
            record_stop(n, payload, d, now)

    elif ev == "WorktreeCreate":
        name = payload.get("name") or payload.get("worktree") or "?"
        state["worktrees"][name] = {
            "name": name, "base": payload.get("base_ref"),
            "created": now, "owner": whoami(state, payload),
        }
        node(state, whoami(state, payload), worktree=name)

    elif ev == "WorktreeRemove":
        state["worktrees"].pop(payload.get("name") or "", None)

    elif ev in ("TaskCreated", "TaskCompleted"):
        state["waves"].append({
            "event": ev, "at": now,
            "task": payload.get("task_id") or payload.get("id"),
            "text": (payload.get("prompt") or payload.get("subject") or "")[:140],
        })
        del state["waves"][:-200]

    elif ev == "Notification":
        kind = payload.get("notification_type") or payload.get("matcher") or ""
        state["needs_input"] = kind in ("permission_prompt", "agent_needs_input", "idle_prompt")
        state["notes"].append({"at": now, "kind": kind,
                               "text": (payload.get("message") or "")[:200]})
        del state["notes"][:-50]

    elif ev == "TeammateIdle":
        # Fires inside the teammate that is going idle, naming itself. It is
        # the moment the idle frame is written, so it is the earliest point at
        # which a stale one can be taken back out.
        who = payload.get("teammate_name")
        mark_teammate(state, payload.get("agent_id"), payload.get("team_name"))
        n = next((v for v in state["nodes"].values() if v.get("name") == who), None)
        if n:
            n["idle_at"] = now

    elif ev == "Stop":
        state["nodes"][ROOT]["status"] = "idle"
        state["needs_input"] = False
        # The conductor's own spend, from its own transcript. Sidechain
        # records are skipped — each subagent is counted from its own file.
        record_usage(state["nodes"][ROOT], payload.get("transcript_path"),
                     key="usage_size_self")

    elif ev == "SessionEnd":
        state["nodes"][ROOT]["status"] = "done"
        state["nodes"][ROOT]["ended"] = now
        for n in state["nodes"].values():
            if n["status"] == "running" and n["id"] != ROOT:
                n["status"] = "orphaned"

    # Freshness — the re-orchestrator uses this to spot stalled agents.
    if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart"):
        node(state, whoami(state, payload), last_activity=now)

    # The session transcript, from the conductor's own hooks. Subagent
    # transcripts sit beside it (see agent_transcript).
    if payload.get("transcript_path") and not payload.get("agent_id"):
        state["transcript"] = payload["transcript_path"]

    # Infer worktree membership from the cwd the hook fired in. Never on a
    # stop: whoami() adopts an unknown id as *running*, so a SubagentStop for
    # an agent whose start this ledger never saw minted a phantom that sat in
    # the census — and drew stall nudges — until the reaper caught it 15
    # minutes later. One field session showed 24 of them "in flight".
    cw = payload.get("cwd") or ""
    if ev != "SubagentStop" and ".claude/worktrees/" in cw.replace("\\", "/"):
        wt = cw.replace("\\", "/").split(".claude/worktrees/")[1].split("/")[0]
        me = whoami(state, payload)
        node(state, me, worktree=wt)
        state["worktrees"].setdefault(wt, {"name": wt, "created": now, "owner": me})

    # Drop dispatches too old to still be waiting on a start, so a worktree
    # created now cannot adopt one of them and inherit the wrong type.
    state["pending"] = [p for p in state["pending"]
                        if now - p.get("at", 0) <= PENDING_TTL]

    # Rebuild the parts the payloads cannot tell us, before anything derived
    # from them is recomputed.
    link_by_worktree(state, now)
    reap_stale(state, now)

    # Refresh derived data every event — cheap, and keeps the tree honest.
    for n in state["nodes"].values():
        # A node first seen as a named teammate carries the caller's name where
        # its type belongs. Normalise it (and re-tier), keeping the name around
        # — this also repairs ledgers written before canon_type existed.
        ct = canon_type(n.get("type"))
        if ct != n.get("type"):
            n["name"] = n.get("name") or n.get("type")
            n["type"] = ct
            n["model"] = tier_of(ct)
        n["depth"] = depth_of(state, n["id"])
        n["lane"] = lane_of(state, n["id"])
    shots = Path(state.get("cwd") or ".") / ".claude" / "maestro" / "shots"
    if shots.is_dir():
        state["shots"] = sorted(
            (str(p) for p in shots.glob("*.png")),
            key=lambda p: os.path.getmtime(p), reverse=True,
        )[:40]
    return state


def event_line(payload, limit=20000):
    """One event as a single line of *valid* JSON.

    Slicing the serialized string (the old `json.dumps(...)[:20000]`) cut it
    mid-token, so every oversized event landed on disk as unparseable garbage
    and `/api/events` — and every other reader — silently dropped it. Trim the
    bulky free-text fields instead; the envelope always stays parseable.
    """
    rec = {"at": time.time(), **payload}
    line = json.dumps(rec, default=str)
    if len(line) <= limit:
        return line

    def clip(v, n=400):
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        return s[:n] + "…[trimmed]" if len(s) > n else v

    for key in ("tool_response", "result", "response", "prompt", "message"):
        if key in rec:
            rec[key] = clip(rec[key])
    if isinstance(rec.get("tool_input"), dict):
        rec["tool_input"] = {k: clip(v) for k, v in rec["tool_input"].items()}
    rec["_trimmed"] = True
    line = json.dumps(rec, default=str)
    if len(line) <= limit:
        return line
    # Last resort: keep only the routing fields the tree is built from.
    keep = ("at", "hook_event_name", "session_id", "cwd", "agent_id",
            "agent_type", "tool_name", "tool_use_id")
    return json.dumps({**{k: rec.get(k) for k in keep}, "_trimmed": "hard"},
                      default=str)


def caffeinate(d, state, ev):
    """Hold the Mac awake while agents are in flight, release it when they stop.

    One `caffeinate` per session dir, tracked by pidfile: asserted the moment a
    node goes running/spawning, killed as soon as the last one leaves. Hooks
    fire on every tool call, so the release happens within a beat of the wave
    ending rather than waiting for the session to close.

    A node stuck in `running` must never pin the machine awake forever, and that
    is not hypothetical: a nested agent whose SubagentStop never matches its node
    stays `running` for the life of the session. So a node only counts as live
    while it is still *showing* activity — past the idle ceiling it is treated as
    gone for sleep purposes, whatever the ledger says.

      MAESTRO_CAFFEINATE=0            never assert
      MAESTRO_CAFFEINATE=-dims        flags to pass (default -ims: system stays
                                      awake, display is still allowed to sleep)
      MAESTRO_CAFFEINATE_MAX_IDLE=900 seconds of silence after which a `running`
                                      node stops holding the assertion
    """
    flags = os.environ.get("MAESTRO_CAFFEINATE", "-ims")
    if sys.platform != "darwin" or flags == "0":
        return
    try:
        max_idle = float(os.environ.get("MAESTRO_CAFFEINATE_MAX_IDLE", "900"))
    except ValueError:
        max_idle = 900.0
    pidf = d / "caffeinate.pid"
    try:
        pid = int(pidf.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        try:                       # still ours and still alive?
            os.kill(pid, 0)
        except OSError:
            pid = None
            try:
                pidf.unlink()
            except OSError:
                pass
    now = time.time()
    live = ev not in ("SessionEnd",) and any(
        n.get("status") in ("running", "spawning")
        and now - float(n.get("last_activity") or n.get("started") or now) < max_idle
        for k, n in (state.get("nodes") or {}).items() if k != ROOT)
    if live and pid is None:
        argv = flags.split()
        # Hard ceiling so an assertion can never outlive the thing it was
        # protecting. The pidfile lives inside the session dir, and a session
        # dir can be deleted (a worktree gets cleaned up, someone clears
        # .claude/maestro) — which orphans the process with nothing left
        # pointing at it. Hooks re-assert constantly while work is live, so a
        # ceiling costs nothing and bounds the damage to one interval.
        if "-t" not in argv:
            argv += ["-t", os.environ.get("MAESTRO_CAFFEINATE_TTL", "3600")]
        try:
            p = subprocess.Popen(["caffeinate", *argv],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            pidf.write_text(str(p.pid))
        except (OSError, subprocess.SubprocessError):
            pass
    elif not live and pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            pidf.unlink()
        except OSError:
            pass


def main():
    # The re-orchestrator's second-opinion call is itself a Claude Code session.
    # Without this guard it would write its own ledger and steal the `current`
    # pointer from the session it is auditing.
    if os.environ.get("MAESTRO_LEDGER_OFF") == "1":
        return
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    try:
        deny = bg_guard(payload)
    except Exception:                     # the guard fails open
        deny = None

    d = home(payload)
    lock = d / ".lock"
    retire = hold = block = None
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(d / "events.jsonl", "a") as f:
                f.write(event_line(payload) + "\n")
            sf = d / "state.json"
            state = blank(payload)
            if sf.exists():
                try:
                    state = json.loads(sf.read_text()) or state
                except (json.JSONDecodeError, OSError):
                    pass
            state.setdefault("nodes", {}).setdefault(ROOT, blank(payload)["nodes"][ROOT])
            state = apply(state, payload, d)
            if deny:                      # the call never runs
                (state["nodes"].get(payload.get("agent_id")) or {}).pop("in_tool_since", None)
                if payload.get("tool_name") in ("Agent", "Task") and state["pending"]:
                    state["pending"].pop()     # nor does the dispatch it recorded
            wait = 0.0
            if payload.get("hook_event_name") == "SubagentStop":
                try:
                    block = stop_backstop(state, payload)
                except Exception:         # the backstop fails open
                    block = None
                me = state["nodes"].get(payload.get("agent_id") or "")
                if block and me:
                    # Kept working, so not finished: no report is owed yet.
                    me.update(status="running", ended=None, last_activity=time.time())
            if payload.get("hook_event_name") == "TeammateIdle":
                mate = state["nodes"].get(payload.get("agent_id") or "") or next(
                    (v for v in state["nodes"].values()
                     if v.get("name") == payload.get("teammate_name")), None)
                try:
                    hold = bg_backstop(state, payload, mate)
                except Exception:         # the backstop fails open
                    hold = None
                if hold:
                    # Sent back to work, so it is running again, not idle, and
                    # the re-check's stall rule (which skips idle teammates)
                    # watches it.
                    if mate:
                        mate.pop("idle_at", None)
                        mate.update(status="running", ended=None, last_activity=time.time())
                elif retired(mate):
                    wait = time.time() + IDLE_CATCH
                    # Stop only a teammate whose last word was a RETURN report.
                    # One that ended on a question is waiting for an answer.
                    if not KEEP_TEAMMATES and ended_on_report(mate):
                        retire = who_named(mate, payload)
            prune_inbox(state, payload, wait)
            tmp = sf.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, default=str))
            tmp.replace(sf)
            caffeinate(d, state, payload.get("hook_event_name") or "")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    if payload.get("hook_event_name") == "SessionStart" and not payload.get("agent_id"):
        # Output styles get no ${CLAUDE_PLUGIN_ROOT} and the Bash tool has no
        # such variable, so the conductor learns the scripts' real path here.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"Maestro scripts: {Path(__file__).resolve().parent}"},
            "suppressOutput": True}))
    if retire:
        # Idle teammates kept their context and process until the session
        # ended — about three alive at every stop across the field ledgers —
        # and each one pinged the lead after every turn.
        print(json.dumps({"continue": False,
                          "stopReason": f"Maestro: {retire} already reported, so it was "
                                        f"stopped instead of idling. Message it by name "
                                        f"to resume it."}))
    if deny:
        print(json.dumps(deny))
    if block:
        # SubagentStop `decision: block` keeps the subagent running and hands
        # it `reason` as its next instruction.
        print(json.dumps({"decision": "block", "reason": block}))
    if hold:
        # TeammateIdle exit 2: the teammate gets stderr as feedback and keeps
        # working instead of going idle.
        print(hold, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    code = 0
    try:
        code = main() or 0
    except Exception as e:  # never break the session: any error allows, exit 0
        code = 0
        if DEBUG:
            print(f"maestro ledger: {e}", file=sys.stderr)
    sys.exit(code)
