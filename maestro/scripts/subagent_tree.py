#!/usr/bin/env python3
"""Maestro terminal tree — replaces Claude Code's flat agent panel rows with an
indented, colour-coded tree.

Claude Code pipes one JSON object with a `tasks` array and a `columns` width on
stdin, and expects one JSON line per row on stdout:  {"id": ..., "content": ...}
The content string is rendered verbatim, so ANSI colour and OSC 8 hyperlinks
both work. Depth comes from the ledger written by scripts/ledger.py.
"""
import json
import os
import sys
import time
from pathlib import Path

R = "\033[0m"
DIM = "\033[2m"
B = "\033[1m"
FG = {
    "haiku": "\033[38;5;80m",    # cyan
    "sonnet": "\033[38;5;114m",  # green
    "opus": "\033[38;5;177m",    # violet
    "grey": "\033[38;5;244m",
    "amber": "\033[38;5;214m",
    "red": "\033[38;5;203m",
    "blue": "\033[38;5;75m",
}
ICON = {
    "running": ("✳", "amber"), "active": ("✳", "amber"), "in_progress": ("✳", "amber"),
    "pending": ("◌", "grey"), "queued": ("◌", "grey"),
    "done": ("✓", "sonnet"), "completed": ("✓", "sonnet"), "success": ("✓", "sonnet"),
    "failed": ("✗", "red"), "error": ("✗", "red"),
    "blocked": ("▲", "red"), "needs_input": ("▲", "amber"),
}
PORT = os.environ.get("MAESTRO_PORT", "7717")


def bare(name):
    """`maestro:scout` -> `scout`. Plugin agents arrive namespaced."""
    return (name or "").split(":")[-1]


def tier(model, name):
    m = (model or "").lower()
    for k in ("haiku", "sonnet", "opus"):
        if k in m:
            return k
    return {"scout": "haiku", "scribe": "haiku", "codex": "haiku", "surgeon": "opus"}.get(bare(name), "sonnet")


def link(text, url):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def bar(used, total, width=8):
    if not total:
        return ""
    pct = max(0.0, min(1.0, used / total))
    filled = int(round(pct * width))
    colour = FG["sonnet"] if pct < 0.6 else FG["amber"] if pct < 0.85 else FG["red"]
    return f"{DIM}▕{R}{colour}{'█' * filled}{R}{DIM}{'·' * (width - filled)}▏{int(pct*100):>3}%{R}"


def elapsed(start):
    """startTime may be epoch ms, epoch seconds, or an ISO 8601 string."""
    if not start:
        return ""
    try:
        if isinstance(start, str) and not start.replace(".", "").isdigit():
            from datetime import datetime
            t = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        else:
            t = float(start)
            if t > 1e11:
                t /= 1000.0
    except (TypeError, ValueError):
        return ""
    s = int(time.time() - t)
    if s < 0 or s > 86400:
        return ""
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def load_ledger(cwd):
    """Depth/parent map from the ledger, keyed by agent type in start order."""
    p = Path(cwd or ".").resolve()
    for parent in [p, *p.parents]:
        ptr = parent / ".claude" / "maestro" / "current"
        if ptr.is_file():
            try:
                sf = Path(ptr.read_text().strip()) / "state.json"
                return json.loads(sf.read_text())
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def depths_for(tasks, ledger):
    """Best-effort depth per task: match ledger nodes of the same type, in order."""
    buckets = {}
    for n in sorted((ledger.get("nodes") or {}).values(),
                    key=lambda n: n.get("started") or 0):
        if n.get("id") != "root":
            buckets.setdefault(n.get("type"), []).append(n)
    used, out = {}, []
    for t in tasks:
        name = bare(t.get("type") or t.get("name"))
        i = used.get(name, 0)
        pool = buckets.get(name, [])
        out.append(pool[i].get("depth", 1) if i < len(pool) else 1)
        used[name] = i + 1
    return out


def has_later_sibling(depths, i, level):
    """True if some node after i sits at `level` before the branch closes."""
    for d in depths[i + 1:]:
        if d < level:
            return False
        if d == level:
            return True
    return False


def prefixes(depths):
    """├─ / └─ / │ scaffolding from a flat depth list (children follow parents)."""
    depths = [max(1, min(5, d)) for d in depths]
    out = []
    for i, d in enumerate(depths):
        stem = "".join("│  " if has_later_sibling(depths, i, lvl) else "   "
                       for lvl in range(1, d))
        out.append(stem + ("├─ " if has_later_sibling(depths, i, d) else "└─ "))
    return out


def row(task, prefix, width):
    name = bare(task.get("type") or task.get("name")) or "agent"
    model = tier(task.get("model"), name)
    status = (task.get("status") or "running").lower()
    icon, icol = ICON.get(status, ("·", "grey"))
    desc = (task.get("description") or task.get("label") or "").replace("\n", " ").strip()

    left = (f"{DIM}{prefix}{R}"
            f"{FG[icol]}{icon}{R} "
            f"{FG[model]}{B}{name[:16]:<17}{R}"
            f"{DIM}{model[:3]}{R} ")

    right_bits = []
    tok, ctx = task.get("tokenCount"), task.get("contextWindowSize")
    if tok and ctx:
        right_bits.append(bar(tok, ctx))
    eff = task.get("effort")
    if eff and eff not in ("medium",):
        right_bits.append(f"{FG['blue']}{str(eff)[:4]}{R}")
    el = elapsed(task.get("startTime"))
    if el:
        right_bits.append(f"{DIM}{el:>6}{R}")
    right = "  ".join(right_bits)

    plain_left = len(prefix) + 2 + 16 + 4
    plain_right = sum(len(x) for x in (
        f"{int(tok/ctx*100) if tok and ctx else ''}", el, str(eff or ""))) + 22
    room = max(10, width - plain_left - plain_right)
    if len(desc) > room:
        desc = desc[:room - 1] + "…"

    tid = task.get("id") or ""
    body = link(desc, f"http://127.0.0.1:{PORT}/#agent-{tid}") if desc else ""
    return f"{left}{DIM}{body}{R}  {right}"


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    tasks = payload.get("tasks") or []
    if not tasks:
        return
    width = int(payload.get("columns") or 100)
    ledger = load_ledger(payload.get("cwd"))
    ds = depths_for(tasks, ledger)
    pfx = prefixes(ds)
    for t, p in zip(tasks, pfx):
        print(json.dumps({"id": t.get("id"), "content": row(t, p, width)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if os.environ.get("MAESTRO_DEBUG") == "1":
            print(f"maestro tree: {e}", file=sys.stderr)
    sys.exit(0)
