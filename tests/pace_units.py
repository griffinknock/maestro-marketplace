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


def probe_beats_continue_five_hour_probes_case():
    print("\n=== probe outranks continue: five_hour probes, seven_day comfortable -> probe wins ===")
    now = 1000.0
    snapshot = snap(
        now,
        five_hour=reading(95.0, now - 10),   # already reset, no chunk since -> probe
        seven_day=reading(50.0, None),       # no reset horizon -> comfortable continue
    )
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("probe beats a comfortable continue on the other window", d["action"] == "probe", d)
    check("window is five_hour (the one that's actually unknown)", d["window"] == "five_hour", d)


def probe_beats_continue_seven_day_probes_case():
    print("\n=== probe outranks continue, reversed: seven_day probes, five_hour comfortable -> probe wins ===")
    now = 1000.0
    snapshot = snap(
        now,
        five_hour=reading(50.0, None),        # no reset horizon -> comfortable continue
        seven_day=reading(95.0, now - 10),    # already reset, no chunk since -> probe
    )
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("probe beats a comfortable continue regardless of window order", d["action"] == "probe", d)
    check("window is seven_day (the one that's actually unknown)", d["window"] == "seven_day", d)


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


def resets_at_passed_no_chunks_since_probes_case():
    print("\n=== resets_at already passed, no chunks since -> usage unknown, probe ===")
    now = 1000.0
    snapshot = snap(now, five_hour=reading(95.0, now - 10))  # window already reset
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("action is probe (reading predates reset, no chunk ran since)", d["action"] == "probe", d)
    check("reason mentions the reset", "reset" in d["reason"], d["reason"])


def stale_far_past_reset_no_chunks_probes_case():
    print("\n=== reading captured 700000s ago, both windows already reset -> probe, not 'continue comfortable' ===")
    now = 1_000_000.0
    old_capture = now - 700000.0
    snapshot = {
        "captured_at": old_capture,
        "five_hour": reading(99.0, old_capture + 100),   # reset long since passed
        "seven_day": reading(99.0, old_capture + 200),   # reset long since passed
    }
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("action is probe, never a comfortable continue on a stale reading", d["action"] == "probe", d)


def post_reset_with_chunks_since_estimates_case():
    print("\n=== resets_at passed, but chunks have started since -> estimate from burn * chunks ===")
    now = 100000.0
    resets_at = now - 500.0
    history = [
        pace_rec(1, "start", resets_at + 50, five_used=5.0, five_resets=resets_at + 100000, captured_at=resets_at + 50),
        pace_rec(1, "end", resets_at + 150, five_used=7.0, five_resets=resets_at + 100000, captured_at=resets_at + 150),
    ]
    snapshot = snap(now, five_hour=reading(99.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is not probe (we have a post-reset chunk to estimate from)", d["action"] != "probe", d)
    check("reason credits the chunk(s) since reset", "since reset" in d["reason"], d["reason"])


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
    print("\n=== >=3 chunks with no reading at all -> stop, never spend usage blind ===")
    history = [pace_rec(i, "start", i * 100, five_used=None, seven_used=None) for i in range(1, 4)]
    d = decide(None, 1000.0, DEFAULT_POLICY, history)
    check("action is stop", d["action"] == "stop", d)
    check("reason says no usage signal", "no usage signal" in d["reason"], d["reason"])


def blind_fallback_with_allow_blind_sleeps_case():
    print("\n=== >=3 unread chunks, allow_blind set -> sleep blind_gap_s instead of stopping ===")
    history = [pace_rec(i, "start", i * 100, five_used=None, seven_used=None) for i in range(1, 4)]
    policy = dict(DEFAULT_POLICY, allow_blind=True, blind_gap_s=120)
    d = decide(None, 1000.0, policy, history)
    check("action is sleep", d["action"] == "sleep", d)
    check("delay_s honors blind_gap_s", d["delay_s"] == 120, d)
    check("reason mentions allow_blind", "allow_blind" in d["reason"], d["reason"])


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


def zero_burn_at_ceiling_case():
    print("\n=== zero measured burn, reading sitting exactly at the ceiling -> not a bare continue ===")
    now = 1000.0
    resets_at = now + 200
    history = [
        pace_rec(1, "start", 0, five_used=80.0, five_resets=resets_at),
        pace_rec(1, "end", 100, five_used=80.0, five_resets=resets_at),
    ]
    snapshot = snap(now, five_hour=reading(80.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is sleep, not continue, right at the ceiling with zero measured burn",
          d["action"] == "sleep", d)
    check("window is five_hour", d["window"] == "five_hour", d)


def mean_burn_undershoots_gate_uses_max_case():
    print("\n=== mean of [0,0,0,0,10]=2 must not let a chunk start at 77% under an 80% ceiling ===")
    now = 1000.0
    resets_at = now + 5000
    history = [
        pace_rec(1, "start", 0, five_used=60.0, five_resets=resets_at),
        pace_rec(1, "end", 100, five_used=60.0, five_resets=resets_at),
        pace_rec(2, "start", 100, five_used=60.0, five_resets=resets_at),
        pace_rec(2, "end", 200, five_used=60.0, five_resets=resets_at),
        pace_rec(3, "start", 200, five_used=60.0, five_resets=resets_at),
        pace_rec(3, "end", 300, five_used=60.0, five_resets=resets_at),
        pace_rec(4, "start", 300, five_used=60.0, five_resets=resets_at),
        pace_rec(4, "end", 400, five_used=60.0, five_resets=resets_at),
        pace_rec(5, "start", 400, five_used=60.0, five_resets=resets_at),
        pace_rec(5, "end", 500, five_used=70.0, five_resets=resets_at),
    ]
    # mean burn = 2.0, but the largest recent delta is 10.0 -> gate_burn = 10.0
    snapshot = snap(now, five_hour=reading(77.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("action is sleep/stop, not a bare continue, with headroom (3) under the max-delta gate (10)",
          d["action"] in ("sleep", "stop"), d)


def nan_used_percentage_treated_as_absent_case():
    print("\n=== NaN / non-numeric used_percentage does not raise, window treated as absent ===")
    now = 1000.0
    snapshot = snap(now, five_hour={"used_percentage": float("nan"), "resets_at": now + 1000},
                     seven_day=reading(10.0, now + 500000))
    d = decide(snapshot, now, DEFAULT_POLICY, [])
    check("no crash, decision returned", d is not None, d)
    check("window is not five_hour (NaN reading skipped)", d["window"] != "five_hour", d)

    snapshot2 = snap(now, five_hour={"used_percentage": "not-a-number", "resets_at": now + 1000})
    d2 = decide(snapshot2, now, DEFAULT_POLICY, [])
    check("non-numeric used_percentage also doesn't raise", d2 is not None, d2)
    # Neither window is usable now -> falls through to "no window matched" probe.
    check("falls back to probe when the only window's reading is unusable", d2["action"] == "probe", d2)


def invalid_policy_types_stop_case():
    print("\n=== invalid policy types -> stop, never a pacing decision ===")
    bad_policies = [
        {},
        None,
        dict(DEFAULT_POLICY, ceilings={"five_hour": 150, "seven_day": 90}),
        dict(DEFAULT_POLICY, deviation="yolo"),
        dict(DEFAULT_POLICY, chunk_size=0),
        dict(DEFAULT_POLICY, margin_s=-1),
        dict(DEFAULT_POLICY, max_attempts="two"),
        dict(DEFAULT_POLICY, allow_blind="yes"),
        dict(DEFAULT_POLICY, blind_gap_s=10),
    ]
    for bad in bad_policies:
        d = decide(snap(1000.0, five_hour=reading(10.0, 11000.0)), 1000.0, bad, [])
        check(f"stop for invalid policy {bad!r}", d["action"] == "stop" and "policy invalid" in d["reason"], d)


def per_window_captured_at_overrides_top_level_case():
    print("\n=== per-window captured_at is read before the top-level fallback ===")
    now = 200.0
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=now + 10000, captured_at=0),
        pace_rec(1, "end", 100, five_used=12.0, five_resets=now + 10000, captured_at=100),
    ]
    # Top-level captured_at is stale (predates the last chunk end); the
    # five_hour window's OWN captured_at is fresh (10s old, after the last
    # chunk ended) and must win, so this reading is NOT treated as stale.
    snapshot = {
        "captured_at": 0,
        "five_hour": {"used_percentage": 12.0, "resets_at": now + 10000, "captured_at": now - 10},
        "seven_day": None,
    }
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("reading not treated as stale (used the window's own fresh captured_at)",
          "stale" not in d["reason"] and "projected" not in d["reason"], d["reason"])


def cli_smoke_case():
    print("\n=== CLI smoke test: malformed/missing files degrade gracefully ===")
    pace_py = str(Path(__file__).resolve().parent.parent / "maestro" / "scripts" / "pace.py")
    with tempfile.TemporaryDirectory() as td:
        sweep = Path(td)
        # No policy.json, no pace.jsonl, no usage.json at all -> no policy at
        # all is invalid, and an invalid/missing policy must stop, not probe
        # (a probe still spends a chunk's worth of usage).
        result = subprocess.run(
            [sys.executable, pace_py, "--sweep", str(sweep), "--usage", str(sweep / "nope.json"), "--now", "1000"],
            capture_output=True, text=True,
        )
        check("CLI exits 0 with no files present", result.returncode == 0, result.stderr)
        try:
            decision = json.loads(result.stdout.strip())
        except ValueError:
            decision = None
        check("CLI prints one JSON decision line", decision is not None, result.stdout)
        check("CLI stops (never probes) with no policy at all",
              decision and decision.get("action") == "stop" and "policy invalid" in decision.get("reason", ""),
              decision)

        # Malformed policy.json / pace.jsonl should not crash the CLI.
        (sweep / "policy.json").write_text("{not json")
        (sweep / "pace.jsonl").write_text("not json either\n")
        (sweep / "usage.json").write_text(json.dumps(snap(1000.0, five_hour=reading(10.0, 11000.0))))
        result2 = subprocess.run(
            [sys.executable, pace_py, "--sweep", str(sweep), "--usage", str(sweep / "usage.json"), "--now", "1000"],
            capture_output=True, text=True,
        )
        check("CLI exits 0 with malformed policy/pace files", result2.returncode == 0, result2.stderr)
        try:
            decision2 = json.loads(result2.stdout.strip())
        except ValueError:
            decision2 = None
        check("CLI still prints valid JSON with malformed inputs", decision2 is not None, result2.stdout)
        check("malformed policy.json parses to {} -> still 'policy invalid', still stop",
              decision2 and decision2.get("action") == "stop", decision2)


def cli_unexpected_exception_stops_case():
    print("\n=== CLI: an unexpected exception while deciding -> stop, not a silent probe ===")
    pace_py = str(Path(__file__).resolve().parent.parent / "maestro" / "scripts" / "pace.py")
    with tempfile.TemporaryDirectory() as td:
        sweep = Path(td)
        (sweep / "policy.json").write_text(json.dumps(DEFAULT_POLICY))
        usage_file = sweep / "usage.json"
        # A JSON *list* at the top level parses fine but has no .get(), so
        # decide()'s snapshot.get(...) calls raise inside the CLI.
        usage_file.write_text(json.dumps([1, 2, 3]))
        result = subprocess.run(
            [sys.executable, pace_py, "--sweep", str(sweep), "--usage", str(usage_file), "--now", "1000"],
            capture_output=True, text=True,
        )
        check("CLI still exits 0", result.returncode == 0, result.stderr)
        try:
            decision = json.loads(result.stdout.strip())
        except ValueError:
            decision = None
        check("CLI prints valid JSON", decision is not None, result.stdout)
        check("CLI stops (not probes) on an unexpected exception",
              decision and decision.get("action") == "stop", decision)


# ── round-3 finding 2: frozen readings must not fabricate zero burn ──────

def frozen_chunks(n, used, captured_at, five_resets, start_at=1000.0, span=100.0):
    """n chunks whose start AND end carry the same stale reading (same
    captured_at) — what a usage.json that stopped updating produces."""
    hist = []
    for i in range(1, n + 1):
        s = start_at + (i - 1) * span
        hist.append(pace_rec(i, "start", s, five_used=used, five_resets=five_resets,
                             captured_at=captured_at))
        hist.append(pace_rec(i, "end", s + span * 0.9, five_used=used, five_resets=five_resets,
                             captured_at=captured_at))
    return hist


def frozen_reading_at_75_case():
    print("\n=== frozen 75% reading, ceiling 80, 12 chunks -> never continue (finding 2 repro a) ===")
    t0 = 900.0
    resets = 1000.0 + 4 * 3600
    history = frozen_chunks(12, 75.0, t0, resets)
    now = 1000.0 + 12 * 100 + 10
    snapshot = {"captured_at": t0, "session_id": None,
                "five_hour": {"used_percentage": 75.0, "resets_at": resets, "captured_at": t0},
                "seven_day": None}
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("not continue on a frozen reading", d["action"] != "continue", d)
    check("stops: 12 chunks in a row ended with no fresh reading",
          d["action"] == "stop" and "no usage signal" in d["reason"], d)
    d1 = decide(snapshot, 1000.0 + 100 + 10, DEFAULT_POLICY, history[:2])
    check("even after ONE frozen chunk the stale reading is projected with the "
          "conservative burn, not zero (75 + 5 >= 80 -> sleep)", d1["action"] == "sleep", d1)


def frozen_reading_700000s_old_case():
    print("\n=== 700000 s-old reading, resets passed, 6 frozen chunks since -> never continue (finding 2 repro b) ===")
    now = 1_000_000.0
    old = now - 700000.0
    history = frozen_chunks(6, 99.0, old, old + 100, start_at=now - 1000)
    snapshot = {"captured_at": old,
                "five_hour": {"used_percentage": 99.0, "resets_at": old + 100, "captured_at": old},
                "seven_day": {"used_percentage": 99.0, "resets_at": old + 200, "captured_at": old}}
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    check("not continue after 6 chunks on a 700000 s-old reading", d["action"] != "continue", d)
    check("stops for no usage signal", d["action"] == "stop" and "no usage signal" in d["reason"], d)
    d2 = decide(snapshot, now, dict(DEFAULT_POLICY, allow_blind=True, blind_gap_s=600), history)
    check("with allow_blind it sleeps blind_gap_s instead of continuing",
          d2["action"] == "sleep" and d2["delay_s"] == 600, d2)


def only_fresh_deltas_count_case():
    print("\n=== a chunk's delta only counts when its end reading is fresh ===")
    from pace import _burn_and_duration, _completed_chunks, _trailing_unread
    resets = 50000.0
    history = [
        # fresh: end captured during the chunk -> delta 4
        pace_rec(1, "start", 0, five_used=10.0, five_resets=resets, captured_at=0),
        pace_rec(1, "end", 100, five_used=14.0, five_resets=resets, captured_at=90),
        # frozen: end carries the start's capture -> not a zero delta, unread
        pace_rec(2, "start", 200, five_used=14.0, five_resets=resets, captured_at=90),
        pace_rec(2, "end", 300, five_used=14.0, five_resets=resets, captured_at=90),
        # end captured BEFORE the chunk started -> not fresh either
        pace_rec(3, "start", 400, five_used=14.0, five_resets=resets, captured_at=90),
        pace_rec(3, "end", 500, five_used=14.0, five_resets=resets, captured_at=350),
    ]
    burn, gate, dur, dflt = _burn_and_duration(_completed_chunks(history), "five_hour")
    check("mean burn is the fresh chunk's 4.0, not diluted to 1.33 by frozen chunks",
          abs(burn - 4.0) < 1e-9 and not dflt, (burn, gate, dur, dflt))
    check("two trailing chunks without a fresh end reading count as unread",
          _trailing_unread(history) == 2, _trailing_unread(history))
    history.append(pace_rec(4, "start", 600, five_used=14.0, five_resets=resets, captured_at=90))
    history.append(pace_rec(4, "end", 700, five_used=14.0, five_resets=resets, captured_at=90))
    snapshot = snap(710.0, five_hour={"used_percentage": 14.0, "resets_at": resets, "captured_at": 90})
    d = decide(snapshot, 710.0, DEFAULT_POLICY, history)
    check("three unread chunks in a row -> stop even though usage.json exists",
          d["action"] == "stop" and "no usage signal" in d["reason"], d)
    history.append(pace_rec(5, "start", 800, five_used=14.0, five_resets=resets, captured_at=90))
    history.append(pace_rec(5, "end", 900, five_used=15.0, five_resets=resets, captured_at=880))
    check("a fresh reading resets the unread run", _trailing_unread(history) == 0)


def per_window_capture_in_pace_records_case():
    print("\n=== per-window 'captured' in pace records is honoured over captured_at ===")
    from pace import _fresh_end
    start = {"chunk": 1, "event": "start", "at": 100,
             "usage": {"five_hour": 10.0, "seven_day": 5.0, "captured_at": 150,
                       "captured": {"five_hour": 50, "seven_day": 150},
                       "resets": {"five_hour": 9000, "seven_day": 900000}}}
    end = {"chunk": 1, "event": "end", "at": 200,
           "usage": {"five_hour": 12.0, "seven_day": 5.0, "captured_at": 190,
                     "captured": {"five_hour": 190, "seven_day": 150},
                     "resets": {"five_hour": 9000, "seven_day": 900000}}}
    check("five_hour end reading is fresh", _fresh_end(start, end, "five_hour"))
    check("seven_day end reading (same capture as start) is not", not _fresh_end(start, end, "seven_day"))


def interrupted_chunks_excluded_case():
    print("\n=== chunks closed by recover (interrupted) are excluded from burn/duration ===")
    from pace import _burn_and_duration, _completed_chunks, _trailing_unread
    resets = 50000.0
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=resets, captured_at=0),
        pace_rec(1, "end", 100, five_used=12.0, five_resets=resets, captured_at=90),
        pace_rec(2, "start", 200, five_used=12.0, five_resets=resets, captured_at=90),
        dict(pace_rec(2, "end", 20000, five_used=40.0, five_resets=resets, captured_at=19000),
             interrupted=True),
    ]
    burn, gate, dur, _ = _burn_and_duration(_completed_chunks(history), "five_hour")
    check("interrupted chunk's 28-point, 19800 s span not counted",
          abs(burn - 2.0) < 1e-9 and abs(dur - 100.0) < 1e-9, (burn, gate, dur))
    check("interrupted chunk is neutral for the unread run", _trailing_unread(history) == 0)


def reset_jitter_delta_case():
    print("\n=== resets_at jitter (1 s) between start and end is not a mid-chunk reset ===")
    from pace import _burn_and_duration, _completed_chunks
    history = [
        pace_rec(1, "start", 0, five_used=10.0, five_resets=50000.0, captured_at=0),
        pace_rec(1, "end", 100, five_used=13.0, five_resets=50001.0, captured_at=90),
    ]
    burn, _, _, dflt = _burn_and_duration(_completed_chunks(history), "five_hour")
    check("jittered chunk still yields its delta", not dflt and abs(burn - 3.0) < 1e-9, (burn, dflt))


def projection_uses_gate_burn_case():
    print("\n=== post-reset projection uses max(burn, gate_burn), not the mean ===")
    now = 100000.0
    resets_at = now - 5000.0          # the snapshot's window has already reset
    new_resets = now + 10000.0
    history = []
    t = resets_at + 10
    # two measured chunks: deltas 1 and 9 -> mean 5, gate 9
    for i, (a, b) in enumerate([(0.0, 1.0), (1.0, 10.0)], 1):
        history.append(pace_rec(i, "start", t, five_used=a, five_resets=new_resets, captured_at=t - 5))
        history.append(pace_rec(i, "end", t + 100, five_used=b, five_resets=new_resets, captured_at=t + 90))
        t += 200
    # six more chunks with fresh end readings whose deltas are discarded
    # (window rolled mid-chunk) — read, so not "blind", but no new burn data
    for i in range(3, 9):
        history.append(pace_rec(i, "start", t, five_used=10.0, five_resets=new_resets, captured_at=t - 5))
        history.append(pace_rec(i, "end", t + 100, five_used=1.0, five_resets=new_resets + 18000,
                                captured_at=t + 90))
        t += 200
    snapshot = snap(now, five_hour=reading(99.0, resets_at))
    d = decide(snapshot, now, DEFAULT_POLICY, history)
    # 8 chunks since reset * gate 9 = 72 -> headroom 8 < 9 -> sleep; the old
    # mean-based estimate (8 * 5 = 40) said "comfortable, continue".
    check("8 chunks x gate 9 = 72 leaves less than one chunk under 80 -> not continue",
          d["action"] == "sleep" and "since reset" in d["reason"], d)


def fresh_at_drives_staleness_case():
    print("\n=== usage.json fresh_at (API activity) decides staleness, not captured_at ===")
    now = 5000.0
    resets = now + 10000
    history = [
        pace_rec(1, "start", 4000, five_used=30.0, five_resets=resets, captured_at=3990),
        pace_rec(1, "end", 4100, five_used=30.0, five_resets=resets, captured_at=4090),
    ]
    # value last ROSE long ago (captured_at 1000) but an active session
    # confirmed it just now (fresh_at 4990): current, not stale.
    fresh = snap(now, five_hour={"used_percentage": 30.0, "resets_at": resets,
                                 "captured_at": 1000.0, "fresh_at": 4990.0})
    d = decide(fresh, now, DEFAULT_POLICY, history)
    check("recent fresh_at -> reading used as-is (not 'stale')", "stale" not in d["reason"], d)
    # fresh_at recorded but null: never confirmed by an active session.
    never = snap(now, five_hour={"used_percentage": 30.0, "resets_at": resets,
                                 "captured_at": 4990.0, "fresh_at": None})
    d2 = decide(never, now, DEFAULT_POLICY, history)
    check("fresh_at null -> treated as stale even with a recent captured_at", "stale" in d2["reason"], d2)


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
    probe_beats_continue_five_hour_probes_case()
    probe_beats_continue_seven_day_probes_case()
    zero_delta_coarse_readings_case()
    stale_reading_projected_forward_case()
    resets_at_passed_no_chunks_since_probes_case()
    stale_far_past_reset_no_chunks_probes_case()
    post_reset_with_chunks_since_estimates_case()
    missing_snapshot_probes_case()
    neither_window_present_probes_case()
    blind_fallback_after_three_unread_chunks_case()
    blind_fallback_with_allow_blind_sleeps_case()
    under_three_unread_chunks_still_probes_case()
    delay_clamps_case()
    used_over_100_case()
    zero_burn_at_ceiling_case()
    mean_burn_undershoots_gate_uses_max_case()
    nan_used_percentage_treated_as_absent_case()
    invalid_policy_types_stop_case()
    per_window_captured_at_overrides_top_level_case()
    cli_smoke_case()
    cli_unexpected_exception_stops_case()
    frozen_reading_at_75_case()
    frozen_reading_700000s_old_case()
    only_fresh_deltas_count_case()
    per_window_capture_in_pace_records_case()
    interrupted_chunks_excluded_case()
    reset_jitter_delta_case()
    projection_uses_gate_burn_case()
    fresh_at_drives_staleness_case()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
