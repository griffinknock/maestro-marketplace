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
}


# Only these three declare `isolation: worktree`, so only these can ever own a
# worktree — which is what makes a worktree name a usable parent signal.
ISOLATED = ("section-lead", "builder", "surgeon")
# `.claude/worktrees/agent-<agent-id>` — the owning agent's id, and the only
# place a delegating agent's id ever appears (it is on no hook payload).
WT_OWNER = re.compile(r"^agent-([0-9a-z]{6,})$", re.I)
# A running node this quiet is not coming back; stop counting it as in flight.
REAP_SECONDS = float(os.environ.get("MAESTRO_REAP_SECONDS", "900"))
# An unmatched dispatch older than this is not the one that just started. Without
# a ceiling, a worktree created now can adopt a dispatch from half an hour ago
# and inherit the wrong type entirely.
PENDING_TTL = float(os.environ.get("MAESTRO_PENDING_TTL", "600"))


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


def home(payload):
    """Per-session state directory, inside the project so worktrees can find it."""
    base = payload.get("cwd") or os.getcwd()
    # Climb out of a worktree back to the real project root when possible.
    p = Path(base).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists() or (parent / ".claude").is_dir():
            base = str(parent)
            break
    sid = (payload.get("session_id") or "nosession")[:8]
    d = Path(base) / ".claude" / "maestro" / sid
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
        last = float(n.get("last_activity") or n.get("started") or now)
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


def touch_file(n, path):
    if not path:
        return
    if path not in n["files"]:
        n["files"].append(path)
        del n["files"][:-40]
    n["last_file"] = path


def apply(state, payload):
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
                "model": tier_of(atype, ti.get("model")),
                "background": bool(ti.get("run_in_background")),
                "at": now,
            })
            del state["pending"][:-64]
        else:
            n = state["nodes"][me]
            n["last_tool"] = tool
            if tool == "Bash":
                n["description"] = (ti.get("description") or ti.get("command") or "")[:120]

    elif ev in ("PostToolUse", "PostToolUseFailure"):
        me = whoami(state, payload)
        n = state["nodes"][me]
        tool = payload.get("tool_name") or "?"
        n["tools"][tool] = n["tools"].get(tool, 0) + 1
        n["last_tool"] = tool
        ti = payload.get("tool_input") or {}
        touch_file(n, ti.get("file_path") or ti.get("notebook_path"))
        if ev == "PostToolUseFailure":
            n["tools"]["!failed"] = n["tools"].get("!failed", 0) + 1

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
            "model": (match or {}).get("model") or tier_of(atype),
            "status": "running",
            "description": (match or {}).get("description", ""),
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
            n["status"] = "failed" if payload.get("error") else "done"
            n["ended"] = now
            # The final reply arrives as `last_assistant_message` on this hook;
            # `result`/`response` never appear on real payloads.
            res = (payload.get("last_assistant_message")
                   or payload.get("result") or payload.get("response") or "")
            if isinstance(res, str) and res.strip():
                n["result"] = res.strip()[-1200:]

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

    elif ev == "Stop":
        state["nodes"][ROOT]["status"] = "idle"
        state["needs_input"] = False

    elif ev == "SessionEnd":
        state["nodes"][ROOT]["status"] = "done"
        state["nodes"][ROOT]["ended"] = now
        for n in state["nodes"].values():
            if n["status"] == "running" and n["id"] != ROOT:
                n["status"] = "orphaned"

    # Freshness — the re-orchestrator uses this to spot stalled agents.
    if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart"):
        node(state, whoami(state, payload), last_activity=now)

    # Infer worktree membership from the cwd the hook fired in.
    cw = payload.get("cwd") or ""
    if ".claude/worktrees/" in cw.replace("\\", "/"):
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

    d = home(payload)
    lock = d / ".lock"
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
            state = apply(state, payload)
            tmp = sf.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, default=str))
            tmp.replace(sf)
            caffeinate(d, state, payload.get("hook_event_name") or "")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the session
        if DEBUG:
            print(f"maestro ledger: {e}", file=sys.stderr)
    sys.exit(0)
