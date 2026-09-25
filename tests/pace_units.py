#!/usr/bin/env python3
"""Focused checks for maestro/scripts/pace.py — the sweep pacing brain.

    python3 tests/pace_units.py

Exit code is 0 when every check passes.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maestro" / "scripts"))
from pace import decide, _clamp_delay  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


DEFAULT_POLICY = {
    "ceilings": {"five_hour": 80, "seven_day": 90},
    "deviation": "additive",
    "chunk_size": 5,
    "margin_s": 120,
    "max_attempts": 2,
}


def reading(used, resets_at):
    return {"used_percentage": used, "resets_at": resets_at}


def snap(now, five_hour=None, seven_day=None):
    return {
        "captured_at": now,
        "session_id": "s1",
        "five_hour": five_hour,
        "seven_day": seven_day,
    }


def pace_rec(chunk, event, at, five_used=None, seven_used=None,
             five_resets=None, seven_resets=None, captured_at=None):
    return {
        "chunk": chunk, "event": event, "at": at,
        "usage": {
            "five_hour": five_used, "seven_day": seven_used,
            "captured_at": captured_at if captured_at is not None else at,
            "resets": {"five_hour": five_resets, "seven_day": seven_resets},
        },
    }


def no_history_case():
    print("\n=== no history -> conservative default ===")
    now = 1000.0
    snapshot = snap(now, five_hour=reading(10.0, now + 10000))
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("action is continue or sleep (never stop/probe)", d["action"] in ("continue", "sleep"), d)
    check("reason mentions no history / default", "default" in d["reason"] or "no history" in d["reason"], d["reason"])


def low_usage_far_from_reset_case():
    print("\n=== low usage, far from reset -> continue ===")
    now = 1000.0
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=now + 10000),
        pace_rec(1, "end", 100, five_used=10.5, five_resets=now + 10000),
    ]
    snapshot = snap(now, five_hour=reading(10.5, now + 10000))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is continue", d["action"] == "continue", d)
    check("delay_s is 0", d["delay_s"] == 0, d)


def moderate_usage_computed_gap_case():
    print("\n=== moderate usage, lots of time left -> sleep with computed gap ===")
    now = 100.0
    # burn = 2 pts / 100s chunk
    history = [
        pace_rec(1, "start", 0, five_used=30.0, five_resets=now + 10000),
        pace_rec(1, "end", 100, five_used=32.0, five_resets=now + 10000),
    ]
    snapshot = snap(now, five_hour=reading(40.0, now + 10000))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    # headroom = 80-40=40, burn=2, chunks_affordable=20, time_to_reset=10000,
    # spacing=500, gap=500-100=400
    check("action is sleep", d["action"] == "sleep", d)
    check("delay_s ~= 400s", abs(d["delay_s"] - 400) <= 2, d)
    check("window is five_hour", d["window"] == "five_hour", d)


def five_hour_headroom_below_chunk_short_wait_case():
    print("\n=== 5h headroom < 1 chunk, short wait -> sleep until reset+margin ===")
    now = 1000.0
    resets_at = now + 50
    history = [
        pace_rec(1, "start", 0, five_used=70.0, five_resets=resets_at),
        pace_rec(1, "end", 100, five_used=75.0, five_resets=resets_at),
    ]
    # burn=5, headroom = 80-79=1 < 5
    snapshot = snap(now, five_hour=reading(79.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is sleep", d["action"] == "sleep", d)
    check("delay_s clamped to >=60", d["delay_s"] >= 60, d)
    check("delay_s ~= resets_at+margin-now", abs(d["delay_s"] - (resets_at + 120 - now)) <= 2, d)
    check("window is five_hour", d["window"] == "five_hour", d)


def five_hour_headroom_below_chunk_long_wait_case():
    print("\n=== 5h headroom < 1 chunk, long wait (>1h) -> 3600 + wake_at ===")
    now = 1000.0
    resets_at = now + 5000
    history = [
        pace_rec(1, "start", 0, five_used=70.0, five_resets=resets_at),
        pace_rec(1, "end", 100, five_used=75.0, five_resets=resets_at),
    ]
    snapshot = snap(now, five_hour=reading(79.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is sleep", d["action"] == "sleep", d)
    check("delay_s clamped to 3600", d["delay_s"] == 3600, d)
    check("wake_at is the true target (past 1h)", d["wake_at"] == resets_at + 120, d)


def seven_day_headroom_below_chunk_stops_case():
    print("\n=== weekly headroom < 1 chunk -> stop ===")
    now = 1000.0
    snapshot = snap(now, seven_day=reading(89.0, now + 500000))
    d = decide(snapshot, now, DEFAULT_POLICY, [])  # no history -> default burn 5
    check("action is stop", d["action"] == "stop", d)
    check("window is seven_day", d["window"] == "seven_day", d)
    check("delay_s is 0", d["delay_s"] == 0, d)


def most_restrictive_wins_case():
    print("\n=== both windows present -> most restrictive wins ===")
    now = 1000.0
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=now + 10000,
                 seven_used=10.0, seven_resets=now + 500000),
        pace_rec(1, "end", 100, five_used=10.5, five_resets=now + 10000,
                 seven_used=10.5, seven_resets=now + 500000),
    ]
    snapshot = snap(
        now,
        five_hour=reading(10.5, now + 10000),   # comfortable -> continue
        seven_day=reading(89.9, now + 500000),  # tight -> stop
    )
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("stop beats continue", d["action"] == "stop", d)
    check("window is seven_day", d["window"] == "seven_day", d)


def both_windows_continue_case():
    print("\n=== both windows present, both comfortable -> continue combines cleanly ===")
    now = 1000.0
    # Small measured burn (0.5 pts / 100s chunk) against ample headroom and a
    # short-ish reset horizon on both windows means the paced gap is
    # negative for each independently -> both windows say "continue", and
    # combining two continues must not crash (there is no wake_at at all).
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=now + 5000,
                 seven_used=10.0, seven_resets=now + 8000),
        pace_rec(1, "end", 100, five_used=10.5, five_resets=now + 5000,
                 seven_used=10.5, seven_resets=now + 8000),
    ]
    snapshot = snap(
        now,
        five_hour=reading(10.5, now + 5000),
        seven_day=reading(10.5, now + 8000),
    )
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("continue, no crash combining two continues", d["action"] == "continue", d)


def both_windows_sleep_mismatched_wake_at_case():
    print("\n=== both windows sleep, one short (wake_at=None) one long (>1h) -> no crash ===")
    # This is the exact shape that crashed _more_restrictive against the real
    # sweep_state.py fixtures: a near five_hour reset (short sleep, no
    # wake_at) combined with a distant seven_day reset (long sleep,
    # wake_at set) — comparing None <= float used to blow up.
    now = 1000.0
    snapshot = snap(
        now,
        five_hour=reading(40.0, now + 3 * 3600),
        seven_day=reading(50.0, now + 4 * 86400),
    )
    d = decide(snapshot, now, DEFAULT_POLICY, [])  # no history -> default burn
    check("action is sleep, not a crash", d["action"] == "sleep", d)
    check("window is seven_day (the longer sleep wins)", d["window"] == "seven_day", d)
    check("delay_s clamped to [60, 3600]", 60 <= d["delay_s"] <= 3600, d)


def zero_delta_coarse_readings_case():
    print("\n=== zero-delta coarse readings -> treated as real (zero) burn ===")
    now = 1000.0
    history = [
        pace_rec(1, "start", 0, five_used=20.0, five_resets=now + 10000),
        pace_rec(1, "end", 100, five_used=20.0, five_resets=now + 10000),
        pace_rec(2, "start", 100, five_used=20.0, five_resets=now + 10000),
        pace_rec(2, "end", 200, five_used=20.0, five_resets=now + 10000),
    ]
    snapshot = snap(now, five_hour=reading(20.0, now + 10000))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is continue (zero burn -> no urgency)", d["action"] == "continue", d)
    check("delay_s is 0", d["delay_s"] == 0, d)


def stale_reading_projected_forward_case():
    print("\n=== stale reading -> projected forward using burn/duration ===")
    now = 1000.0
    resets_at = now + 10000
    history = [
        pace_rec(1, "start", 0, five_used=30.0, five_resets=resets_at, captured_at=0),
        pace_rec(1, "end", 100, five_used=32.0, five_resets=resets_at, captured_at=100),
    ]
    # snapshot reading is from before the last chunk ended (stale), used=31 at t=50
    snapshot = {
        "captured_at": 50,
        "five_hour": reading(31.0, resets_at),
        "seven_day": None,
    }
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    # burn=2/100s duration. elapsed = (100-50) + (1000-100) = 950s -> 9.5 chunks
    # projected = 31 + 2*9.5 = 50 -> headroom = 30, still > burn(2) -> paced sleep
    check("action is sleep (projected use is higher than raw reading)", d["action"] == "sleep", d)
    check("reason notes the reading was stale/projected", "stale" in d["reason"] or "projected" in d["reason"], d["reason"])


def resets_at_passed_treated_fresh_case():
    print("\n=== resets_at already passed -> treated as fresh (0% used) ===")
    now = 1000.0
    snapshot = snap(now, five_hour=reading(95.0, now - 10))  # window already reset
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("action is continue (fresh window, not near ceiling)", d["action"] == "continue", d)
    check("reason mentions the reset already passed", "reset" in d["reason"], d["reason"])


def missing_snapshot_probes_case():
    print("\n=== missing snapshot -> probe ===")
    d = decide(None, 1000.0, DEFAULT_POLICY, [])
    check("action is probe", d["action"] == "probe", d)
    check("reason says usage is unknown", "unknown" in d["reason"], d["reason"])


def neither_window_present_probes_case():
    print("\n=== snapshot present but neither window -> probe ===")
    snapshot = {"captured_at": 1000.0, "five_hour": None, "seven_day": None}
    d = decide(snapshot, 1000.0, DEFAULT_POLICY, [])
    check("action is probe", d["action"] == "probe", d)


def blind_fallback_after_three_unread_chunks_case():
    print("\n=== >=3 chunks with no reading at all -> blind continue ===")
    history = [pace_rec(i, "start", i * 100, five_used=None, seven_used=None) for i in range(1, 4)]
    d = decide(None, 1000.0, DEFAULT_POLICY, history)
    check("action is continue", d["action"] == "continue", d)
    check("reason says pacing is blind", "blind" in d["reason"], d["reason"])


def under_three_unread_chunks_still_probes_case():
    print("\n=== <3 unread chunks -> still probe, not blind yet ===")
    history = [pace_rec(i, "start", i * 100, five_used=None, seven_used=None) for i in range(1, 3)]
    d = decide(None, 1000.0, DEFAULT_POLICY, history)
    check("action is probe", d["action"] == "probe", d)


def delay_clamps_case():
    print("\n=== delay_s always clamped to [60, 3600] ===")
    check("clamp below floor", _clamp_delay(5) == 60)
    check("clamp above ceiling", _clamp_delay(999999) == 3600)
    check("clamp passthrough", _clamp_delay(200) == 200)


def used_over_100_case():
    print("\n=== used_percentage > 100 -> treated as already over ceiling ===")
    now = 1000.0
    resets_at = now + 200
    snapshot = snap(now, five_hour=reading(110.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("action is sleep (over ceiling, short wait)", d["action"] == "sleep", d)
    check("window is five_hour", d["window"] == "five_hour", d)


def cli_smoke_case():
    print("\n=== CLI smoke test: malformed/missing files degrade gracefully ===")
    with tempfile.TemporaryDirectory() as td:
        sweep = Path(td)
        # No policy.json, no pace.jsonl, no usage.json at all.
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "maestro" / "scripts" / "pace.py"),
             "--sweep", str(sweep), "--usage", str(sweep / "nope.json"), "--now", "1000"],
            capture_output=True, text=True,
        )
        check("CLI exits 0 with no files present", result.returncode == 0, result.stderr)
        try:
            decision = json.loads(result.stdout.strip())
        except ValueError:
            decision = None
        check("CLI prints one JSON decision line", decision is not None, result.stdout)
        check("CLI degrades to probe with no inputs", decision and decision.get("action") == "probe", decision)

        # Malformed policy.json / pace.jsonl should not crash the CLI.
        (sweep / "policy.json").write_text("{not json")
        (sweep / "pace.jsonl").write_text("not json either\n")
        (sweep / "usage.json").write_text(json.dumps(snap(1000.0, five_hour=reading(10.0, 11000.0))))
        result2 = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "maestro" / "scripts" / "pace.py"),
             "--sweep", str(sweep), "--usage", str(sweep / "usage.json"), "--now", "1000"],
            capture_output=True, text=True,
        )
        check("CLI exits 0 with malformed policy/pace files", result2.returncode == 0, result2.stderr)
        try:
            decision2 = json.loads(result2.stdout.strip())
        except ValueError:
            decision2 = None
        check("CLI still prints valid JSON with malformed inputs", decision2 is not None, result2.stdout)


def main():
    no_history_case()
    low_usage_far_from_reset_case()
    moderate_usage_computed_gap_case()
    five_hour_headroom_below_chunk_short_wait_case()
    five_hour_headroom_below_chunk_long_wait_case()
    seven_day_headroom_below_chunk_stops_case()
    most_restrictive_wins_case()
    both_windows_continue_case()
    both_windows_sleep_mismatched_wake_at_case()
    zero_delta_coarse_readings_case()
    stale_reading_projected_forward_case()
    resets_at_passed_treated_fresh_case()
    missing_snapshot_probes_case()
    neither_window_present_probes_case()
    blind_fallback_after_three_unread_chunks_case()
    under_three_unread_chunks_still_probes_case()
    delay_clamps_case()
    used_over_100_case()
    cli_smoke_case()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
