#!/usr/bin/env python3
"""Integration checks for pace.py against real sweeps built with the real
sweep_state.py CLI (not synthetic history fixtures).

This exists because pace_units.py's synthetic fixtures never happened to
combine two present windows where one verdict carries no wake_at (a plain
`continue`, or a short `sleep` whose true target is under an hour) and the
other does — exactly the shape sweep_state.py produces for a fresh sweep
with both windows present, which crashed `_more_restrictive` with
`TypeError: '<=' not supported between instances of 'NoneType' and 'float'`.
This suite drives the two scripts together end-to-end so a future change to
either side's contract shows up here, not just in a hand-rolled fixture.

    python3 tests/pace_integration_units.py

Exit code is 0 when every check passes.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "maestro" / "scripts"
SWEEP = SCRIPTS / "sweep_state.py"
PACE = SCRIPTS / "pace.py"

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def tmpdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


def base_env(sweeps_dir, usage_file):
    env = dict(os.environ)
    env["MAESTRO_SWEEPS_DIR"] = str(sweeps_dir)
    env["MAESTRO_USAGE_FILE"] = str(usage_file)
    # sweep_state.py takes the chunk owner from Claude Code's session id.
    env["CLAUDE_CODE_SESSION_ID"] = "sess-integration"
    return env


def run_sweep(args, env):
    r = subprocess.run([sys.executable, str(SWEEP), *args], capture_output=True, text=True, env=env)
    if r.returncode not in (0, 2):
        raise AssertionError(f"sweep_state.py {args} crashed: {r.stderr}")
    return r


def run_pace(sweep_dir, usage_file, now=None):
    args = [sys.executable, str(PACE), "--sweep", str(sweep_dir), "--usage", str(usage_file)]
    if now is not None:
        args += ["--now", str(now)]
    r = subprocess.run(args, capture_output=True, text=True)
    check(f"pace.py exits 0 ({sweep_dir.name})", r.returncode == 0, r.stderr)
    try:
        decision = json.loads(r.stdout.strip())
    except ValueError:
        decision = None
    check(f"pace.py prints valid JSON ({sweep_dir.name})", decision is not None, r.stdout)
    return decision


def write_usage(usage_file, five_used=None, five_resets_delta=None,
                 seven_used=None, seven_resets_delta=None, now=None):
    now = now if now is not None else time.time()
    payload = {"captured_at": now, "session_id": "x"}
    payload["five_hour"] = ({"used_percentage": five_used, "resets_at": now + five_resets_delta}
                             if five_used is not None else None)
    payload["seven_day"] = ({"used_percentage": seven_used, "resets_at": now + seven_resets_delta}
                             if seven_used is not None else None)
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(payload))
    return now


def new_sweep(env, slug, n_items=4, policy=None):
    root = tmpdir("maestro-sweep-src-")
    plan = root / "plan.md"
    items = root / "items.txt"
    plan.write_text("plan\n")
    items.write_text("\n".join(f"item-{i}" for i in range(n_items)) + "\n")
    args = ["new", "--slug", slug, "--plan", str(plan), "--items", str(items)]
    if policy is not None:
        args += ["--policy", json.dumps(policy)]
    r = run_sweep(args, env)
    check(f"sweep_state.py new succeeds ({slug})", r.returncode == 0, r.stderr)
    return Path(env["MAESTRO_SWEEPS_DIR"]) / slug


def run_one_chunk(env, slug, sweep_dir):
    r = run_sweep(["next", slug], env)
    ids = [it["id"] for it in json.loads(r.stdout)]
    for item_id in ids:
        run_sweep(["done", slug, item_id], env)
    run_sweep(["end-chunk", slug], env)
    return ids


def run_one_chunk_with_burn(env, slug, usage_file, five_used, seven_used,
                             five_resets_delta, seven_resets_delta, burn_five=0.0, burn_seven=0.0):
    """Like run_one_chunk, but rewrites usage.json between `next` and
    `end-chunk` so the pace.jsonl start/end pair records a real, non-zero
    delta — sweep_state.py snapshots usage fresh at each event, so a chunk
    with no usage change in between produces a zero-burn history entry."""
    write_usage(usage_file, five_used=five_used, five_resets_delta=five_resets_delta,
                seven_used=seven_used, seven_resets_delta=seven_resets_delta)
    r = run_sweep(["next", slug], env)
    ids = [it["id"] for it in json.loads(r.stdout)]
    for item_id in ids:
        run_sweep(["done", slug, item_id], env)
    write_usage(usage_file, five_used=five_used + burn_five, five_resets_delta=five_resets_delta,
                seven_used=seven_used + burn_seven, seven_resets_delta=seven_resets_delta)
    run_sweep(["end-chunk", slug], env)
    return ids


# ── cases ─────────────────────────────────────────────────────────────

def no_history_case():
    print("\n=== integration: fresh sweep, no chunks yet ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "no-history")
    now = write_usage(usage_file, five_used=40, five_resets_delta=3 * 3600,
                       seven_used=50, seven_resets_delta=4 * 86400)
    d = run_pace(sweep_dir, usage_file, now=now)
    check("action is a real verdict, not a crash", d is not None and d.get("action") in
          ("continue", "sleep", "stop", "probe"), d)


def one_chunk_case():
    print("\n=== integration: one completed chunk (real usage snapshots) ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "one-chunk", policy={"chunk_size": 2})
    write_usage(usage_file, five_used=10, five_resets_delta=3 * 3600,
                seven_used=20, seven_resets_delta=4 * 86400)
    run_one_chunk(env, "one-chunk", sweep_dir)
    now = write_usage(usage_file, five_used=12, five_resets_delta=3 * 3600,
                       seven_used=20.5, seven_resets_delta=4 * 86400)
    d = run_pace(sweep_dir, usage_file, now=now)
    check("valid action after one real chunk", d is not None and d.get("action") in
          ("continue", "sleep", "stop", "probe"), d)


def several_chunks_near_five_hour_ceiling_case():
    print("\n=== integration: several chunks approaching the 5h ceiling ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "near-5h", n_items=10, policy={"chunk_size": 2})

    used = 50.0
    for _ in range(3):
        run_one_chunk_with_burn(env, "near-5h", usage_file,
                                 five_used=used, seven_used=30,
                                 five_resets_delta=1800, seven_resets_delta=6 * 86400,
                                 burn_five=10.0)  # burn hard toward the ceiling
        used += 10.0

    now = write_usage(usage_file, five_used=used, five_resets_delta=1800,
                       seven_used=30, seven_resets_delta=6 * 86400)
    d = run_pace(sweep_dir, usage_file, now=now)
    check("near-ceiling, imminent reset -> sleep", d is not None and d.get("action") == "sleep", d)
    # Both windows are present and each independently wants a sleep here
    # (five_hour because its reset is imminent, seven_day on its own,
    # unrelated pacing schedule); combining takes the more conservative
    # (longer) of the two waits, so the reported `window` may legitimately
    # be either — what matters is that combining two live sleep verdicts
    # doesn't crash and stays within the clamp.
    check("driven by a real window", d is not None and d.get("window") in ("five_hour", "seven_day"), d)
    check("delay_s clamped to [60, 3600]", d is not None and 60 <= d.get("delay_s", -1) <= 3600, d)


def weekly_ceiling_case():
    print("\n=== integration: weekly ceiling tight, far from reset -> stop ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "weekly-tight")
    now = write_usage(usage_file, five_used=5, five_resets_delta=3 * 3600,
                       seven_used=89.5, seven_resets_delta=6 * 86400)
    d = run_pace(sweep_dir, usage_file, now=now)
    check("weekly headroom below one chunk -> stop", d is not None and d.get("action") == "stop", d)
    check("driven by seven_day", d is not None and d.get("window") == "seven_day", d)


def window_absent_case():
    print("\n=== integration: seven_day absent from statusline (API-key-shaped account) ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "window-absent")
    now = write_usage(usage_file, five_used=15, five_resets_delta=3 * 3600,
                       seven_used=None)
    d = run_pace(sweep_dir, usage_file, now=now)
    check("still decides with only one window present", d is not None and d.get("action") in
          ("continue", "sleep"), d)


def no_usage_file_at_all_case():
    print("\n=== integration: no usage.json written yet -> probe, never a crash ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"  # never written
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "no-usage")
    d = run_pace(sweep_dir, usage_file, now=time.time())
    check("probes when there is no usage.json yet", d is not None and d.get("action") == "probe", d)


def frozen_usage_file_case():
    print("\n=== integration: usage.json stops updating (headless / no statusLine) -> never continue ===")
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "frozen", n_items=12, policy={"chunk_size": 1})
    # One reading written up front, then never again: every chunk's start
    # and end records carry the same stale reading. The old burn estimate
    # turned each into a zero-burn chunk and kept saying continue.
    t0 = write_usage(usage_file, five_used=75, five_resets_delta=4 * 3600,
                     seven_used=20, seven_resets_delta=5 * 86400)
    actions = []
    for _ in range(4):
        run_one_chunk(env, "frozen", sweep_dir)
        d = run_pace(sweep_dir, usage_file, now=time.time())
        actions.append(d and d.get("action"))
    check("no chunk after a frozen reading is waved through with continue",
          "continue" not in actions, actions)
    check("pace ends on a 'no usage signal' stop once 3 chunks ran unread",
          d is not None and d.get("action") == "stop" and "no usage signal" in d.get("reason", ""), d)

    # Same sweep shape, but the statusline keeps writing fresh readings: no
    # blind stop, the burn is measured.
    sweeps_dir2 = tmpdir("maestro-sweeps-")
    usage_file2 = tmpdir("maestro-usage-") / "usage.json"
    env2 = base_env(sweeps_dir2, usage_file2)
    sweep_dir2 = new_sweep(env2, "fresh", n_items=12, policy={"chunk_size": 1})
    used = 10.0
    for _ in range(4):
        run_one_chunk_with_burn(env2, "fresh", usage_file2, five_used=used, seven_used=20,
                                five_resets_delta=4 * 3600, seven_resets_delta=5 * 86400,
                                burn_five=0.5)
        used += 0.5
    d2 = run_pace(sweep_dir2, usage_file2, now=time.time())
    check("fresh readings every chunk -> not a blind stop",
          d2 is not None and "no usage signal" not in d2.get("reason", ""), d2)


STATUSLINE = SCRIPTS / "statusline.py"


def tick(env, sid, api_ms, five_pct, five_resets, seven_pct=20.0, seven_resets=None):
    """One real statusline tick for session `sid` (as Claude Code would send
    after that session's latest API response, api_ms = its total API time)."""
    payload = {"session_id": sid, "model": {"display_name": "Sonnet"},
               "context_window": {"used_percentage": 10},
               "cost": {"total_cost_usd": 0.1, "total_duration_ms": 1000,
                        "total_api_duration_ms": api_ms},
               "rate_limits": {"five_hour": {"used_percentage": five_pct, "resets_at": five_resets},
                               "seven_day": {"used_percentage": seven_pct,
                                             "resets_at": seven_resets or five_resets + 5 * 86400}}}
    subprocess.run([sys.executable, str(STATUSLINE)], input=json.dumps(payload),
                   capture_output=True, text=True, env=env)


def run_ticked_chunk(env, slug, sweep_dir, sid, api_ms, pct, resets):
    run_sweep(["next", slug], env)
    tick(env, sid, api_ms, pct, resets)   # the statusline tick during the chunk
    for it in json.loads((sweep_dir / "index.json").read_text())["items"]:
        if it["status"] == "running":
            run_sweep(["done", slug, it["id"]], env)
    run_sweep(["end-chunk", slug], env)


def cheap_chunks_on_a_large_plan_case():
    print("\n=== integration: cheap chunks that never move the percentage are NOT blind ===")
    resets = time.time() + 4 * 3600
    # Active conductor: every chunk sees a real API response (api_ms
    # advances) but usage stays at 30.0% — a big plan, cheap items.
    sweeps_dir = tmpdir("maestro-sweeps-")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(sweeps_dir, usage_file)
    sweep_dir = new_sweep(env, "cheap", n_items=8, policy={"chunk_size": 1})
    api = 1000
    tick(env, "conductor", api, 30.0, resets)
    for _ in range(5):
        api += 700
        run_ticked_chunk(env, "cheap", sweep_dir, "conductor", api, 30.0, resets)
    d = run_pace(sweep_dir, usage_file, now=time.time())
    check("5 cheap chunks with live API activity -> no 'no usage signal' stop",
          d is not None and "no usage signal" not in d.get("reason", ""), d)
    check("…and pacing continues", d is not None and d.get("action") == "continue", d)

    # Same, but the only session ticking is idle (its api_ms never
    # advances): still no signal, still stops.
    sweeps_dir2 = tmpdir("maestro-sweeps-")
    usage_file2 = tmpdir("maestro-usage-") / "usage.json"
    env2 = base_env(sweeps_dir2, usage_file2)
    sweep_dir2 = new_sweep(env2, "idle", n_items=8, policy={"chunk_size": 1})
    tick(env2, "idle-session", 500, 30.0, resets)
    for _ in range(4):
        run_ticked_chunk(env2, "idle", sweep_dir2, "idle-session", 500, 30.0, resets)
    d2 = run_pace(sweep_dir2, usage_file2, now=time.time())
    check("an idle session's re-sent reading is still no signal -> stop",
          d2 is not None and d2.get("action") == "stop" and "no usage signal" in d2.get("reason", ""), d2)


def main():
    no_history_case()
    one_chunk_case()
    several_chunks_near_five_hour_ceiling_case()
    weekly_ceiling_case()
    window_absent_case()
    no_usage_file_at_all_case()
    frozen_usage_file_case()
    cheap_chunks_on_a_large_plan_case()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
