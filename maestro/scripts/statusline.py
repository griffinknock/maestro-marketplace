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

# ── usage snapshot ──────────────────────────────────────────────────────
# Claude Code's statusLine payload carries `rate_limits` (Pro/Max, after the
# first API response of a session): five_hour / seven_day / spend_limit, each
# optional. This is the only place that reading arrives, so it is persisted
# here for `sweep_state.py` (and `pace.py`) to read pacing from — but this
# script runs every 3s in a live status line, so the write has to be cheap
# and, above all, invisible: it must never change what gets printed and must
# never raise.
#
# usage.json shape (C1): {"captured_at", "session_id", "five_hour":
# {"used_percentage", "resets_at", "captured_at"} | None, "seven_day": {...}
# | None}. Each window carries its OWN captured_at: the last time its value
# was seen to rise (or the window to roll over). This is what makes
# cross-session merging correct: every open session's statusline writes every
# 3s, so an idle session sitting on an hours-old, lower reading of the SAME
# window must never overwrite a fresher, higher reading from another session
# just because it re-sends with a newer wall-clock time. The top-level
# `captured_at` is the later of the two windows' (kept for older readers);
# `session_id` is whichever session wrote last and may be null — readers
# must not rely on it.
USAGE_FILE = Path(os.environ.get("MAESTRO_USAGE_FILE")
                  or (Path.home() / ".claude" / "maestro" / "usage.json"))
# Two readings of one window whose resets_at differ by at most this many
# seconds are the SAME window: resets_at jitters by a second or so between
# sessions and responses, and a jittered idle reading must never pass for a
# new window and replace a fresh one.
RESET_JITTER_S = 60.0


def _finite(v):
    """float(v) when v is a real, finite number (bools excluded), else None."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _window(w):
    """One rate-limit window -> `{"used_percentage", "resets_at"}` or None.
    A NaN/inf/non-numeric `used_percentage` makes the window absent (it must
    never merge in); a NaN/inf/non-numeric `resets_at` is treated as None."""
    if not isinstance(w, dict) or "used_percentage" not in w:
        return None
    pct = _finite(w.get("used_percentage"))
    if pct is None:
        return None
    resets = w.get("resets_at")
    resets = _finite(resets) if resets is not None else None
    return {"used_percentage": pct, "resets_at": resets}


def _same_window(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= RESET_JITTER_S


def _merge_window(old, new, now):
    """Merge one freshly-read rate-limit window (`new`, from `_window` — no
    `captured_at` yet) into the persisted one (`old`, may be None). Returns
    the window to persist, always carrying `captured_at`.

    Contract (C1):
      - `new` is None (window absent from this payload) -> keep `old`
        untouched; a payload missing a window must never remove/null it.
      - same window (resets_at within RESET_JITTER_S of `old`'s) -> keep
        max(used_percentage) and the later resets_at; captured_at only
        updates (to `now`) when the new value is strictly greater than the
        existing one — an idle session re-sending an hours-old, lower (or
        equal) reading must not revive its timestamp and look fresh, and a
        truly-unchanged tick must not force a write.
      - `new.resets_at` later than that -> replace outright (a new window
        has begun).
      - `new.resets_at` earlier than that -> ignore (a stale cross-window
        race; never regress).
    """
    if new is None:
        return old
    if old is None:
        return {**new, "captured_at": now}
    old_resets = old.get("resets_at")
    old_resets = _finite(old_resets) if old_resets is not None else None
    new_resets = new.get("resets_at")
    if _same_window(old_resets, new_resets):
        resets = max(old_resets, new_resets) if new_resets is not None else None
        old_pct = _finite(old.get("used_percentage"))
        if old_pct is None or new["used_percentage"] > old_pct:
            return {"used_percentage": new["used_percentage"], "resets_at": resets,
                    "captured_at": now}
        if resets != old.get("resets_at"):
            return {**old, "resets_at": resets}
        return old
    if new_resets is not None and (old_resets is None or new_resets > old_resets):
        return {**new, "captured_at": now}
    return old


def snapshot_usage(payload):
    """Persist this tick's rate-limit reading, if any. Best-effort, silent.

    Never writes when `rate_limits` is absent — a session between API
    responses must not clobber a good reading left by another session. Skips
    the write when the merge changes nothing, so a 3s-interval status line
    does not thrash the disk once usage is stable.
    """
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return
    five = _window(rl.get("five_hour"))
    seven = _window(rl.get("seven_day"))
    if five is None and seven is None:
        return

    old = {}
    try:
        if USAGE_FILE.is_file():
            old = json.loads(USAGE_FILE.read_text())
            if not isinstance(old, dict):
                old = {}
    except (OSError, ValueError):
        old = {}
    old_five = old.get("five_hour") if isinstance(old.get("five_hour"), dict) else None
    old_seven = old.get("seven_day") if isinstance(old.get("seven_day"), dict) else None

    now = time.time()
    merged_five = _merge_window(old_five, five, now)
    merged_seven = _merge_window(old_seven, seven, now)
    if merged_five == old_five and merged_seven == old_seven:
        return  # nothing changed — skip the write

    caps = [_finite(w.get("captured_at")) for w in (merged_five, merged_seven)
            if isinstance(w, dict)]
    caps = [c for c in caps if c is not None]
    rec = {"captured_at": max(caps) if caps else None,
           "session_id": payload.get("session_id"),
           "five_hour": merged_five, "seven_day": merged_seven}
    tmp = None
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_name(USAGE_FILE.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, USAGE_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        # A failed os.replace used to leak this tmp file on every tick.
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


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
    try:
        snapshot_usage(d)
    except Exception:
        pass
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
