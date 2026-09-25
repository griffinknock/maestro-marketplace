#!/usr/bin/env python3
"""Maestro sweep pacing brain — decides continue / sleep / stop / probe for a
long, checkpointed sweep, from real Claude Code usage snapshots rather than a
fixed interval.

Inputs (see AGENTS/handoff for the sweep dir contract):
  - policy.json: {"ceilings": {"five_hour": 80, "seven_day": 90},
                  "deviation": "additive", "chunk_size": 5,
                  "margin_s": 120, "max_attempts": 2}
  - pace.jsonl: append-only, two lines per chunk (start/end), each carrying a
    "usage" snapshot shaped like {"five_hour": pct|None, "seven_day": pct|None,
    "captured_at": epoch|None, "resets": {"five_hour": epoch|None,
    "seven_day": epoch|None}}.
  - usage.json: the latest raw statusline snapshot, {"captured_at": epoch,
    "session_id": str, "five_hour": {"used_percentage": float, "resets_at":
    epoch} | None, "seven_day": {...} | None}.

Core entry point: decide(snapshot, now, policy, history) -> dict. Pure and
deterministic — no file I/O, no clock reads beyond the passed-in `now`. The
CLI at the bottom does the I/O and prints one JSON decision line.

## Decision semantics

For each window ("five_hour", "seven_day") present in `snapshot`:

1. **Burn estimate.** Walk `history` (a flat list of pace.jsonl records) for
   completed chunks: a "start" record immediately followed by an "end" record
   for the same chunk number, where both usage readings for this window are
   present and no reset happened in between (the reset epoch is unchanged and
   the used percentage did not drop between start and end — a drop or reset
   change means the window rolled over mid-chunk and that delta is discarded,
   not counted as negative burn). Burn per chunk = mean of (end.used -
   start.used) over the most recent such deltas (last 5). A zero delta is
   kept — used_percentage is coarse and genuinely can read the same twice in
   a row; the mean over several chunks is what makes that data rather than
   noise. Chunk duration = mean of (end.at - start.at) over the same set.
   With zero eligible deltas, fall back to a conservative default burn (see
   `_DEFAULT_BURN`) and duration (see `_DEFAULT_DURATION`), and the reason
   must say the estimate is a conservative default (no history).

2. **Freshness / projection.** If this window's snapshot reading is stale
   (its `captured_at` predates the end of the last completed chunk), project
   it forward: used + burn * chunks_elapsed_since_that_reading, using mean
   chunk duration to convert elapsed wall time into a chunk count. If the
   window's `resets_at` has already passed relative to `now`, treat it as
   fresh at 0% used regardless of the raw reading, and say so in the reason
   ("stale reading, already past reset").

3. **Headroom.** headroom = ceiling - projected_used. If headroom is less
   than one chunk's burn, that window can't safely run another chunk:
     - five_hour -> sleep until resets_at + margin_s (a bounded wait worth
       taking).
     - seven_day -> stop (a weekly reset is too far off to sleep through;
       hand off to the next session/skill invocation instead).

4. **Otherwise, pace to land at/under the ceiling by resets_at:**
     chunks_affordable = headroom / burn
     spacing = (resets_at - now) / chunks_affordable   # target gap between
                                                          # chunk *starts*
     gap = spacing - mean_chunk_duration                # gap is BETWEEN chunks
     gap < 60s  -> continue now (delay_s = 0)
     else       -> sleep min(gap, 3600)s

5. **Combine windows**: evaluate every present window independently, then
   take the most restrictive verdict: stop > sleep (longer sleep wins over a
   shorter one) > continue. `window` on the returned decision names which
   window drove the verdict.

6. **Clamping & long waits.** Any sleep delay is clamped to [60, 3600]
   (ScheduleWakeup's own bounds). A true target beyond 3600s away is
   reported as delay_s=3600 with `wake_at` set to the real target (resets_at
   + margin_s, or the paced target) — the caller just calls decide() again
   after waking, which will recompute a fresh (shorter) sleep or continue.
   We deliberately bias a hair past a reset rather than a hair before it
   (margin_s), since sleeping through the reset and waking just after it is
   cheap, while waking early and re-probing before the window actually
   resets just burns another decide()/probe cycle.

7. **No signal at all.** If `snapshot` is None, or neither window is present
   in it, the account may be API-key-billed (no rate_limits at all) or the
   statusline simply hasn't captured a reading yet. Return "probe": run one
   more (small) chunk to get a reading, and say usage is unknown in the
   reason. If history already shows >= 3 chunks with a start reading of
   None/absent for both windows (i.e. we've probed and never gotten a
   reading), give up probing and fall back to "continue" with a reason that
   says pacing is blind for this account — sleeping blindly buys nothing.
"""
import json
import sys
from pathlib import Path

# Conservative defaults used only when there is no usable burn history yet.
# A single chunk costs at most this much of a window's ceiling, and takes at
# most this long — both chosen to be pessimistic (fewer, shorter chunks)
# rather than optimistic, since the failure mode of guessing too high is
# tripping the actual account limit mid-sweep.
_DEFAULT_BURN = 5.0        # percentage points per chunk
_DEFAULT_DURATION = 300.0  # seconds per chunk

_WINDOWS = ("five_hour", "seven_day")
_MIN_DELAY = 60
_MAX_DELAY = 3600
_BLIND_PROBE_LIMIT = 3


def _clamp_delay(seconds):
    return max(_MIN_DELAY, min(_MAX_DELAY, int(round(seconds))))


def _completed_chunks(history):
    """Pair start/end pace.jsonl records by chunk number, in order."""
    starts = {}
    pairs = []
    for rec in history or []:
        if not isinstance(rec, dict):
            continue
        chunk = rec.get("chunk")
        event = rec.get("event")
        if event == "start":
            starts[chunk] = rec
        elif event == "end" and chunk in starts:
            pairs.append((starts.pop(chunk), rec))
    return pairs


def _window_deltas(pairs, window, limit=5):
    """(used_delta, duration) for each completed chunk with usable readings
    for `window`, most recent last, capped to the last `limit`."""
    deltas = []
    for start, end in pairs:
        su = (start.get("usage") or {}).get(window)
        eu = (end.get("usage") or {}).get(window)
        if su is None or eu is None:
            continue
        s_resets = ((start.get("usage") or {}).get("resets") or {}).get(window)
        e_resets = ((end.get("usage") or {}).get("resets") or {}).get(window)
        if s_resets != e_resets:
            continue  # window reset mid-chunk; delta is meaningless
        if eu < su:
            continue  # used dropped without a resets_at change — bad data
        duration = end.get("at", 0) - start.get("at", 0)
        if duration < 0:
            continue
        deltas.append((eu - su, duration))
    return deltas[-limit:]


def _burn_and_duration(pairs, window):
    """Return (burn_per_chunk, mean_duration, used_default: bool)."""
    deltas = _window_deltas(pairs, window)
    if not deltas:
        return _DEFAULT_BURN, _DEFAULT_DURATION, True
    burns = [d for d, _ in deltas]
    durations = [d for _, d in deltas]
    return sum(burns) / len(burns), sum(durations) / len(durations), False


def _projected_used(reading_pct, reading_captured_at, resets_at, now, burn, duration, last_chunk_end_at):
    """Project a possibly-stale used_percentage forward to `now`. Returns
    (projected_pct, stale: bool, fresh_after_reset: bool)."""
    if resets_at is not None and resets_at <= now:
        return 0.0, True, True

    stale = last_chunk_end_at is not None and reading_captured_at is not None \
        and reading_captured_at < last_chunk_end_at
    if not stale:
        return reading_pct, False, False

    elapsed = max(0.0, last_chunk_end_at - reading_captured_at) if last_chunk_end_at else 0.0
    # also account for time since the last chunk ended, if any
    elapsed += max(0.0, now - (last_chunk_end_at or reading_captured_at))
    chunks_elapsed = elapsed / duration if duration > 0 else 0.0
    return reading_pct + burn * chunks_elapsed, True, False


_RANK = {"continue": 0, "probe": 0, "sleep": 1, "stop": 2}


def _more_restrictive(a, b):
    """Pick the more restrictive of two per-window verdicts. Longer sleeps
    beat shorter ones; stop beats everything."""
    if a is None:
        return b
    if b is None:
        return a
    ra, rb = _RANK[a["action"]], _RANK[b["action"]]
    if ra != rb:
        return a if ra > rb else b
    if a["action"] == "sleep":
        # Compare by true target wait, not the clamped delay, so a distant
        # 5h-window sleep isn't shadowed by a nearer one incorrectly, and a
        # closer real target wins (we want to wake as soon as any window
        # needs re-checking).
        wa = a.get("wake_at", a.get("_now", 0) + a["delay_s"])
        wb = b.get("wake_at", b.get("_now", 0) + b["delay_s"])
        return a if wa <= wb else b
    return a


def decide(snapshot, now, policy, history):
    """Pure pacing decision. See module docstring for full semantics."""
    policy = policy or {}
    ceilings = policy.get("ceilings") or {}
    margin_s = policy.get("margin_s", 120)

    if not snapshot or not any(snapshot.get(w) for w in _WINDOWS):
        blind_attempts = 0
        for rec in history or []:
            if rec.get("event") != "start":
                continue
            usage = rec.get("usage") or {}
            if usage.get("five_hour") is None and usage.get("seven_day") is None:
                blind_attempts += 1
        if blind_attempts >= _BLIND_PROBE_LIMIT:
            return {
                "action": "continue", "delay_s": 0, "window": None, "wake_at": None,
                "reason": "no rate_limits after %d chunks; pacing is blind, running unpaced"
                          % blind_attempts,
            }
        return {
            "action": "probe", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "usage unknown (no snapshot yet); running one chunk to get a reading",
        }

    pairs = _completed_chunks(history)
    last_chunk_end_at = max((end.get("at", 0) for _, end in pairs), default=None)

    verdict = None
    for window in _WINDOWS:
        reading = snapshot.get(window)
        ceiling = ceilings.get(window)
        if reading is None or ceiling is None:
            continue

        used = reading.get("used_percentage")
        if used is None:
            continue
        resets_at = reading.get("resets_at")
        captured_at = snapshot.get("captured_at")

        burn, duration, used_default = _burn_and_duration(pairs, window)

        projected, stale, fresh_after_reset = _projected_used(
            used, captured_at, resets_at, now, burn, duration, last_chunk_end_at,
        )

        default_note = " (no history yet, using conservative default burn)" if used_default else ""
        stale_note = " (stale reading, already past reset — treating as fresh)" if fresh_after_reset \
            else (" (stale reading, projected forward)" if stale else "")

        headroom = ceiling - projected

        if headroom < burn:
            if window == "five_hour":
                target = (resets_at or now) + margin_s
                delay = target - now
                w = {
                    "action": "sleep",
                    "delay_s": _clamp_delay(delay),
                    "window": window,
                    "wake_at": target if delay > _MAX_DELAY else None,
                    "reason": "five_hour headroom (%.1f%%) below one chunk's burn (%.1f%%)%s%s; "
                              "sleeping until reset + margin" % (headroom, burn, default_note, stale_note),
                    "_now": now,
                }
            else:
                w = {
                    "action": "stop",
                    "delay_s": 0,
                    "window": window,
                    "wake_at": None,
                    "reason": "seven_day headroom (%.1f%%) below one chunk's burn (%.1f%%)%s%s; "
                              "weekly reset too far off to wait, handing off"
                              % (headroom, burn, default_note, stale_note),
                    "_now": now,
                }
            verdict = _more_restrictive(verdict, w)
            continue

        if resets_at is None or resets_at <= now:
            # No reset horizon to pace against (shouldn't normally happen
            # since fresh_after_reset handles the passed case) — just go.
            w = {
                "action": "continue", "delay_s": 0, "window": window, "wake_at": None,
                "reason": "%s headroom (%.1f%%) comfortable, no reset horizon to pace against%s%s"
                          % (window, headroom, default_note, stale_note),
                "_now": now,
            }
            verdict = _more_restrictive(verdict, w)
            continue

        chunks_affordable = headroom / burn if burn > 0 else float("inf")
        time_to_reset = resets_at - now
        spacing = time_to_reset / chunks_affordable if chunks_affordable > 0 else 0.0
        gap = spacing - duration

        if gap < 60:
            w = {
                "action": "continue", "delay_s": 0, "window": window, "wake_at": None,
                "reason": "%s paced gap (%.0fs) under 60s; continuing now%s%s"
                          % (window, gap, default_note, stale_note),
                "_now": now,
            }
        else:
            target = now + gap
            w = {
                "action": "sleep",
                "delay_s": _clamp_delay(gap),
                "window": window,
                "wake_at": target if gap > _MAX_DELAY else None,
                "reason": "%s pacing to stay under %.0f%% by reset: sleeping %.0fs (of %.0fs target)%s%s"
                          % (window, ceiling, min(gap, _MAX_DELAY), gap, default_note, stale_note),
                "_now": now,
            }
        verdict = _more_restrictive(verdict, w)

    if verdict is None:
        return {
            "action": "probe", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "no window had both a ceiling and a reading; probing for a usable snapshot",
        }

    verdict.pop("_now", None)
    return verdict


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _load_jsonl(path):
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return records


def main(argv):
    import argparse
    import os
    import time as time_mod

    parser = argparse.ArgumentParser(description="Sweep pacing decision")
    parser.add_argument("--sweep", required=True, help="sweep directory (policy.json, pace.jsonl)")
    parser.add_argument("--usage", default=None, help="usage.json path (default: $MAESTRO_USAGE_FILE or ~/.claude/maestro/usage.json)")
    parser.add_argument("--now", type=float, default=None, help="override current epoch time")
    args = parser.parse_args(argv)

    sweep_dir = Path(args.sweep)
    policy = _load_json(sweep_dir / "policy.json", {}) or {}
    history = _load_jsonl(sweep_dir / "pace.jsonl")

    usage_path = args.usage or os.environ.get("MAESTRO_USAGE_FILE") \
        or str(Path.home() / ".claude" / "maestro" / "usage.json")
    snapshot = _load_json(usage_path, None)

    now = args.now if args.now is not None else time_mod.time()

    decision = decide(snapshot, now, policy, history)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
