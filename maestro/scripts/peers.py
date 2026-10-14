#!/usr/bin/env python3
"""Maestro peers — the other live conductors on this machine, and what they touch.

Reads the session registry (`ledger.sessions_dir()`), so it costs nothing until
a conductor asks. For each live session other than this one it prints the
address to message it by (captured from its own `ListAgents` call), its repo
and branch, the lanes it has in flight, and the files those agents are writing.
Same-repo peers come first: those are the ones whose work can collide.

    python3 peers.py [--json] [--window SECONDS]

A session is live if its ledger moved in the last `--window` seconds (default
1800) and it has not ended. This session is excluded via
CLAUDE_CODE_SESSION_ID.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import ROOT, project_root, rel_path, sessions_dir  # noqa: E402

LIVE = ("running", "spawning")


def branch_of(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def live_sessions(window, now=None):
    """[(sid8, ledger_dir, state)] for every live registered session."""
    now = now or time.time()
    out = []
    try:
        entries = sorted(sessions_dir().iterdir())
    except OSError:
        return out
    for f in entries:
        if f.name.startswith("."):
            continue
        try:
            d = Path(f.read_text().strip())
            sf = d / "state.json"
            if now - sf.stat().st_mtime > window:
                continue
            state = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (state.get("nodes", {}).get(ROOT) or {}).get("status") == "done":
            continue
        out.append((f.name, d, state))
    return out


def summary(sid, d, state, now):
    nodes = state.get("nodes") or {}
    cwd = state.get("cwd") or str(d.parent.parent.parent)
    root = str(project_root(cwd))
    live = [n for n in nodes.values() if n.get("id") != ROOT and n.get("status") in LIVE]
    lanes = {}
    for n in live:
        lane = nodes.get(n.get("lane") or "", n)
        key = lane.get("description") or lane.get("name") or lane.get("type") or "agent"
        lanes.setdefault(key[:70], []).append(n.get("name") or n.get("type") or "agent")
    writes = sorted({rel_path(p, root) for n in live for p in (n.get("writes") or [])})
    return {
        "session": sid,
        "address": state.get("peer_name"),
        "ref": state.get("peer_ref"),
        "repo": root,
        "branch": branch_of(cwd),
        "idle_for_s": int(now - float(state.get("updated") or now)),
        "conductor": (nodes.get(ROOT) or {}).get("status"),
        "lanes": [{"lane": k, "agents": v} for k, v in lanes.items()],
        "writing": writes[:20],
    }


def render(rows, here):
    if not rows:
        return "No other live Maestro conductors."
    lines = []
    for r in rows:
        who = r["address"] or "address unknown (that session has not run ListAgents)"
        same = " · same repo" if r["repo"] == here else ""
        lines.append(f"{who} — {Path(r['repo']).name}@{r['branch'] or '?'}{same} · "
                     f"conductor {r['conductor']}, ledger moved {r['idle_for_s'] // 60}m ago")
        for l in r["lanes"][:6]:
            lines.append(f"  lane {l['lane']}: {', '.join(l['agents'][:4])}")
        if r["writing"]:
            more = "" if len(r["writing"]) < 20 else " …"
            lines.append(f"  writing: {', '.join(r['writing'][:8])}{more}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="peers.py")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window", type=float, default=1800)
    args = ap.parse_args(argv)
    me = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "")[:8]
    here = str(project_root(os.getcwd()))
    now = time.time()
    rows = [summary(sid, d, st, now) for sid, d, st in live_sessions(args.window, now)
            if sid != me]
    rows.sort(key=lambda r: (r["repo"] != here, r["idle_for_s"]))
    print(json.dumps(rows) if args.json else render(rows, here))
    return 0


if __name__ == "__main__":
    sys.exit(main())
