#!/usr/bin/env python3
"""Maestro status line — two rows under the prompt.

  row 1  model · project · branch/worktree · cost · elapsed
  row 2  context meter · live agent census by tier · clickable board link
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

R, DIM, B = "\033[0m", "\033[2m", "\033[1m"
FG = {
    "haiku": "\033[38;5;80m", "sonnet": "\033[38;5;114m", "opus": "\033[38;5;177m",
    "grey": "\033[38;5;244m", "amber": "\033[38;5;214m", "red": "\033[38;5;203m",
    "blue": "\033[38;5;75m", "white": "\033[38;5;252m",
}
PORT = os.environ.get("MAESTRO_PORT", "7717")
CACHE_TTL = 4


def link(text, url):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def meter(pct, width=14):
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    c = FG["sonnet"] if pct < 0.6 else FG["amber"] if pct < 0.85 else FG["red"]
    return f"{DIM}▕{R}{c}{'█' * filled}{R}{DIM}{'·' * (width - filled)}▏{R} {c}{int(pct*100)}%{R}"


def git(cwd):
    """Branch + dirty count, cached on disk so this stays cheap."""
    cache = Path(cwd) / ".claude" / "maestro" / ".gitcache"
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL:
            return json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    out = {"branch": "", "dirty": 0}
    try:
        out["branch"] = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=1.5).stdout.strip()
        st = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=1.5).stdout
        out["dirty"] = len([l for l in st.splitlines() if l.strip()])
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out


def census(cwd):
    """Live agent counts by tier, plus max depth in flight."""
    p = Path(cwd or ".").resolve()
    for parent in [p, *p.parents]:
        ptr = parent / ".claude" / "maestro" / "current"
        if ptr.is_file():
            try:
                state = json.loads((Path(ptr.read_text().strip()) / "state.json").read_text())
            except (OSError, json.JSONDecodeError):
                return {}, 0, False
            live = [n for n in state.get("nodes", {}).values()
                    if n.get("id") != "root" and n.get("status") in ("running", "spawning")]
            by = {}
            for n in live:
                by[n.get("model", "sonnet")] = by.get(n.get("model", "sonnet"), 0) + 1
            deepest = max([n.get("depth", 1) for n in live], default=0)
            return by, deepest, bool(state.get("needs_input"))
    return {}, 0, False


def main():
    d = json.loads(sys.stdin.read() or "{}")
    ws = d.get("workspace") or {}
    cwd = ws.get("current_dir") or d.get("cwd") or os.getcwd()
    model = (d.get("model") or {}).get("display_name") or "?"
    cost = (d.get("cost") or {}).get("total_cost_usd")
    dur = (d.get("cost") or {}).get("total_duration_ms")
    ctx = d.get("context_window") or {}
    pct = float(ctx.get("used_percentage") or 0) / 100.0

    tier = "opus" if "opus" in model.lower() else "haiku" if "haiku" in model.lower() else "sonnet"
    wt = ws.get("git_worktree")
    g = git(cwd)
    branch = wt or g["branch"]

    seg = [f"{FG[tier]}{B}◆ {model}{R}",
           f"{FG['white']}{Path(cwd).name}{R}"]
    if branch:
        icon = "⑂" if wt else "⎇"
        colour = FG["blue"] if wt else FG["grey"]
        seg.append(f"{colour}{icon} {branch}{R}" +
                   (f"{FG['amber']} ●{g['dirty']}{R}" if g["dirty"] else ""))
    if cost:
        seg.append(f"{DIM}${cost:.2f}{R}")
    if dur:
        m = int(dur / 60000)
        seg.append(f"{DIM}{m}m{R}" if m else f"{DIM}<1m{R}")
    print(f"{DIM} · {R}".join(seg))

    by, deepest, needs = census(cwd)
    row = [meter(pct)]
    if by:
        chips = " ".join(f"{FG[k]}●{v}{R}" for k, v in
                         sorted(by.items(), key=lambda kv: ("haiku", "sonnet", "opus").index(kv[0])
                                if kv[0] in ("haiku", "sonnet", "opus") else 9))
        row.append(f"{chips}{DIM} live{R}")
        row.append(f"{DIM}depth {R}{FG['blue']}{deepest}{DIM}/5{R}")
    else:
        row.append(f"{DIM}no agents{R}")
    if needs:
        row.append(f"{FG['amber']}{B}▲ waiting on you{R}")
    row.append(link(f"{FG['blue']}◱ board{R}", f"http://127.0.0.1:{PORT}/"))
    print(f"{DIM} · {R}".join(row))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\033[2mmaestro\033[0m")
    sys.exit(0)
