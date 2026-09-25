#!/usr/bin/env python3
"""Maestro sweep pacing brain — decides continue / sleep / stop / probe for a
long, checkpointed sweep, from real Claude Code usage snapshots rather than a
fixed interval.

Inputs (see AGENTS/handoff for the sweep dir contract):
  - policy.json: {"ceilings": {"five_hour": 80, "seven_day": 90},
                  "deviation": "additive", "chunk_size": 5,
                  "margin_s": 120, "max_attempts": 2,
                  "allow_blind": false, "blind_gap_s": 900}
    `allow_blind`/`blind_gap_s` are optional (defaults shown); every other
    key is required — see "Policy validation" below.
  - pace.jsonl: append-only, two lines per chunk (start/end), each carrying a
    "usage" snapshot shaped like {"five_hour": pct|None, "seven_day": pct|None,
    "captured_at": epoch|None, "resets": {"five_hour": epoch|None,
    "seven_day": epoch|None}}.
  - usage.json: the latest raw statusline snapshot, {"captured_at": epoch,
    "session_id": str, "five_hour": {"used_percentage": float, "resets_at":
    epoch, "captured_at": epoch|None} | None, "seven_day": {...} | None}.
    Each window may carry its own "captured_at" (fresher than the top-level
    one, e.g. when one window was re-read more recently); pace.py reads the
    per-window value first and falls back to the top-level one when a window
    doesn't have its own.

Core entry point: decide(snapshot, now, policy, history) -> dict. Pure and
deterministic — no file I/O, no clock reads beyond the passed-in `now`. The
CLI at the bottom does the I/O and prints one JSON decision line.

## Policy validation

`policy` must be a non-empty object carrying every key above except
`allow_blind`/`blind_gap_s` (which default to `false`/`900`), with:
  - ceilings.five_hour / ceilings.seven_day (when present): numbers in (0, 100]
  - deviation: one of locked|additive|adaptive|autonomous
  - chunk_size: int >= 1
  - margin_s: int >= 0
  - max_attempts: int >= 1
  - allow_blind: bool
  - blind_gap_s: int >= 60
A missing, empty, or malformed policy never falls through to a pacing
decision that might spend usage — `decide()` returns `stop` with
`"policy invalid: <reason>"` before looking at any reading. Real sweeps
built via `sweep_state.py new` always merge onto a full `DEFAULT_POLICY`, so
this only fires for a hand-broken or absent policy.json.

## Decision semantics

For each window ("five_hour", "seven_day") present in `snapshot`:

1. **Burn estimate.** Walk `history` (a flat list of pace.jsonl records) for
   completed chunks: a "start" record immediately followed by an "end"
   record for the same chunk number, where both usage readings for this
   window are present (NaN/non-numeric readings count as absent) and no
   reset happened in between (the reset epoch is unchanged and the used
   percentage did not drop between start and end — a drop or reset change
   means the window rolled over mid-chunk and that delta is discarded, not
   counted as negative burn). Two burn figures come out of this history:
     - `burn` (mean of the deltas, last 5) — a robust average used to space
       chunks out and to project a stale reading forward.
     - `gate_burn` (max(largest recent delta, 1.0 percentage point)) — used
       only to decide whether one more chunk still fits before the ceiling.
       The mean under-states the gate: a single big recent chunk (or a run
       of zero-delta coarse readings averaging near zero) must not let a
       chunk start right at, or past, the ceiling.
   Chunk duration = mean of (end.at - start.at) over the same deltas used
   for `burn`. With zero eligible deltas, fall back to a conservative
   default burn/gate (see `_DEFAULT_BURN`) and duration (see
   `_DEFAULT_DURATION`), and the reason must say the estimate is a
   conservative default (no history).

2. **Freshness / projection.** A usage reading is a LOWER BOUND on current
   usage, never an exact one once time has passed:
     - If this window's `resets_at` has already passed relative to `now`,
       the reading predates the reset and tells us nothing about the new
       window. If no chunk has *started* since `resets_at`, usage is
       unknown — that window's contribution is "probe" (see below). If one
       or more chunks have started since `resets_at`, estimate
       `used = burn * chunks_started_since(resets_at)`.
     - Otherwise, if the reading is stale — its own `captured_at` predates
       the end of the last completed chunk, or is more than 10 minutes old
       — project it forward: `used + burn * chunks_started_since(captured_at)`
       (counting chunk *starts*, not wall-clock time, since wall-clock time
       with no chunks running tells us nothing about burn).
     - Otherwise the raw reading is used as-is.

3. **Headroom.** headroom = ceiling - projected_used. If headroom is less
   than one chunk's `gate_burn`, that window can't safely run another
   chunk:
     - five_hour -> sleep until resets_at + margin_s (a bounded wait worth
       taking).
     - seven_day -> stop (a weekly reset is too far off to sleep through;
       hand off to the next session/skill invocation instead).

4. **Otherwise, pace to land at/under the ceiling by resets_at** (skipped,
   falling through to a plain "continue", when there is no reset horizon to
   pace against — `resets_at` absent or already passed):
     chunks_affordable = headroom / burn   # mean burn, not gate_burn
     spacing = (resets_at - now) / chunks_affordable   # target gap between
                                                          # chunk *starts*
     gap = spacing - mean_chunk_duration                # gap is BETWEEN chunks
     gap < 60s  -> continue now (delay_s = 0)
     else       -> sleep min(gap, 3600)s

5. **Combine windows**: evaluate every present window independently, then
   take the most restrictive verdict: stop > sleep (longer sleep wins over a
   shorter one) > {continue, probe}. `window` on the returned decision names
   which window drove the verdict.

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
   reason. Once history shows >= 3 chunks started with no reading at all for
   either window, probing further just spends usage blind for no signal —
   return "stop" with reason "no usage signal — run install.sh so Maestro's
   statusLine records rate_limits, or set allow_blind", UNLESS
   `policy.allow_blind` is set, in which case sleep `blind_gap_s` between
   chunks instead (still clamped to [60, 3600], with `wake_at` for the
   remainder when `blind_gap_s` itself is over an hour).

Any unexpected exception while computing a decision is never allowed to
fall back to a default that spends usage — the CLI catches it and returns
"stop" with the error text in the reason.
"""
import json
import math
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
_STALE_AFTER_S = 600.0  # 10 minutes

_REQUIRED_POLICY_KEYS = ("ceilings", "deviation", "chunk_size", "margin_s", "max_attempts")
_DEVIATIONS = {"locked", "additive", "adaptive", "autonomous"}
_DEFAULT_ALLOW_BLIND = False
_DEFAULT_BLIND_GAP_S = 900


def _clamp_delay(seconds):
    return max(_MIN_DELAY, min(_MAX_DELAY, int(round(seconds))))


def _is_bool(v):
    return isinstance(v, bool)


def _is_int(v):
    return isinstance(v, int) and not _is_bool(v)


def _is_number(v):
    return isinstance(v, (int, float)) and not _is_bool(v)


def _finite_number(v):
    """Coerce a reading to a real, finite float — or None. Guards every
    used_percentage read against NaN and non-numeric junk so a bad reading
    is treated as an absent one rather than raising or silently poisoning a
    mean/projection with NaN."""
    if not _is_number(v):
        return None
    v = float(v)
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _validate_policy(policy):
    """Return an error string if `policy` fails the contract, else None."""
    if not isinstance(policy, dict) or not policy:
        return "missing or empty policy"

    for key in _REQUIRED_POLICY_KEYS:
        if key not in policy:
            return "missing required key %r" % key

    ceilings = policy.get("ceilings")
    if not isinstance(ceilings, dict):
        return "ceilings must be an object"
    for window in _WINDOWS:
        if window in ceilings:
            v = ceilings[window]
            if not _is_number(v) or not (0 < v <= 100):
                return "ceilings.%s must be a number in (0, 100]" % window

    if policy.get("deviation") not in _DEVIATIONS:
        return "deviation must be one of %s" % sorted(_DEVIATIONS)

    chunk_size = policy.get("chunk_size")
    if not _is_int(chunk_size) or chunk_size < 1:
        return "chunk_size must be an int >= 1"

    margin_s = policy.get("margin_s")
    if not _is_int(margin_s) or margin_s < 0:
        return "margin_s must be an int >= 0"

    max_attempts = policy.get("max_attempts")
    if not _is_int(max_attempts) or max_attempts < 1:
        return "max_attempts must be an int >= 1"

    if "allow_blind" in policy and not _is_bool(policy["allow_blind"]):
        return "allow_blind must be a bool"

    if "blind_gap_s" in policy:
        blind_gap_s = policy["blind_gap_s"]
        if not _is_int(blind_gap_s) or blind_gap_s < 60:
            return "blind_gap_s must be an int >= 60"

    return None


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


def _chunk_start_times(history):
    return sorted(
        rec.get("at", 0) for rec in (history or [])
        if isinstance(rec, dict) and rec.get("event") == "start"
    )


def _chunks_since(history, threshold_at):
    """Count of chunks that *started* at or after `threshold_at`. Used to
    turn a stale or post-reset reading into a burn-based estimate instead of
    a wall-clock guess — only actual chunk activity can have spent usage."""
    if threshold_at is None:
        return 0
    return sum(1 for at in _chunk_start_times(history) if at >= threshold_at)


def _count_blind_starts(history):
    """Chunks started with no usage reading at all for either window."""
    count = 0
    for rec in history or []:
        if not isinstance(rec, dict) or rec.get("event") != "start":
            continue
        usage = rec.get("usage") or {}
        if usage.get("five_hour") is None and usage.get("seven_day") is None:
            count += 1
    return count


def _window_deltas(pairs, window, limit=5):
    """(used_delta, duration) for each completed chunk with usable readings
    for `window`, most recent last, capped to the last `limit`."""
    deltas = []
    for start, end in pairs:
        su = _finite_number((start.get("usage") or {}).get(window))
        eu = _finite_number((end.get("usage") or {}).get(window))
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
    """Return (burn_per_chunk, gate_burn, mean_duration, used_default: bool).

    `burn` is the mean delta — a robust average for spacing/projection.
    `gate_burn` is max(largest recent delta, 1.0) — the floor used only to
    decide whether one more chunk still fits before the ceiling, so a run of
    zero/near-zero coarse readings (or a mean pulled down by a few quiet
    chunks) can never wave a chunk through right at, or past, the ceiling."""
    deltas = _window_deltas(pairs, window)
    if not deltas:
        gate_burn = max(_DEFAULT_BURN, 1.0)
        return _DEFAULT_BURN, gate_burn, _DEFAULT_DURATION, True
    burns = [d for d, _ in deltas]
    durations = [d for _, d in deltas]
    mean_burn = sum(burns) / len(burns)
    gate_burn = max(max(burns), 1.0)
    return mean_burn, gate_burn, sum(durations) / len(durations), False


_RANK = {"continue": 0, "probe": 0, "sleep": 1, "stop": 2}


def _more_restrictive(a, b):
    """Pick the more restrictive of two per-window verdicts: rank first
    (stop > sleep > {continue, probe}), then — for two sleeps — the longer
    delay_s. A continue/probe verdict carries no wake time at all, so
    ranking must settle ties before any wake-time-shaped field is touched;
    comparing delay_s (always an int, always present) rather than wake_at
    (often None) is what keeps this total and crash-free."""
    if a is None:
        return b
    if b is None:
        return a
    ra, rb = _RANK[a["action"]], _RANK[b["action"]]
    if ra != rb:
        return a if ra > rb else b
    if a["action"] == "sleep":
        return a if a["delay_s"] >= b["delay_s"] else b
    return a


def decide(snapshot, now, policy, history):
    """Pure pacing decision. See module docstring for full semantics."""
    policy = policy or {}
    policy_error = _validate_policy(policy)
    if policy_error:
        return {
            "action": "stop", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "policy invalid: %s" % policy_error,
        }

    ceilings = policy.get("ceilings") or {}
    margin_s = policy.get("margin_s", 120)
    allow_blind = policy.get("allow_blind", _DEFAULT_ALLOW_BLIND)
    blind_gap_s = policy.get("blind_gap_s", _DEFAULT_BLIND_GAP_S)

    if not snapshot or not any(snapshot.get(w) for w in _WINDOWS):
        blind_attempts = _count_blind_starts(history)
        if blind_attempts >= _BLIND_PROBE_LIMIT:
            if allow_blind:
                target = now + blind_gap_s
                return {
                    "action": "sleep",
                    "delay_s": _clamp_delay(blind_gap_s),
                    "window": None,
                    "wake_at": target if blind_gap_s > _MAX_DELAY else None,
                    "reason": "no usage signal after %d chunks; allow_blind is set, "
                              "sleeping %ds between chunks instead of running unpaced"
                              % (blind_attempts, blind_gap_s),
                }
            return {
                "action": "stop", "delay_s": 0, "window": None, "wake_at": None,
                "reason": "no usage signal — run install.sh so Maestro's statusLine "
                          "records rate_limits, or set allow_blind",
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

        used = _finite_number(reading.get("used_percentage"))
        if used is None:
            continue
        resets_at = reading.get("resets_at")
        captured_at = reading.get("captured_at")
        if captured_at is None:
            captured_at = snapshot.get("captured_at")

        burn, gate_burn, duration, used_default = _burn_and_duration(pairs, window)
        default_note = " (no history yet, using conservative default burn)" if used_default else ""

        reset_happened = resets_at is not None and resets_at <= now
        if reset_happened:
            chunks_since_reset = _chunks_since(history, resets_at)
            if chunks_since_reset == 0:
                w = {
                    "action": "probe", "delay_s": 0, "window": window, "wake_at": None,
                    "reason": "%s window reset since the last reading and no chunk has run "
                              "since; usage unknown, probing for a fresh reading" % window,
                }
                verdict = _more_restrictive(verdict, w)
                continue
            projected = burn * chunks_since_reset
            stale_note = " (window reset since reading; estimated from %d chunk(s) run since reset)" \
                % chunks_since_reset
        else:
            stale = (
                (last_chunk_end_at is not None and captured_at is not None and captured_at < last_chunk_end_at)
                or (captured_at is not None and (now - captured_at) > _STALE_AFTER_S)
            )
            if stale:
                chunks_since_reading = _chunks_since(history, captured_at)
                projected = used + burn * chunks_since_reading
                stale_note = " (stale reading, projected forward by %d chunk(s) since capture)" \
                    % chunks_since_reading
            else:
                projected = used
                stale_note = ""

        headroom = ceiling - projected

        if headroom < gate_burn:
            if window == "five_hour":
                target = (resets_at or now) + margin_s
                delay = target - now
                w = {
                    "action": "sleep",
                    "delay_s": _clamp_delay(delay),
                    "window": window,
                    "wake_at": target if delay > _MAX_DELAY else None,
                    "reason": "five_hour headroom (%.1f%%) below one chunk's burn (%.1f%%)%s%s; "
                              "sleeping until reset + margin" % (headroom, gate_burn, default_note, stale_note),
                }
            else:
                w = {
                    "action": "stop",
                    "delay_s": 0,
                    "window": window,
                    "wake_at": None,
                    "reason": "seven_day headroom (%.1f%%) below one chunk's burn (%.1f%%)%s%s; "
                              "weekly reset too far off to wait, handing off"
                              % (headroom, gate_burn, default_note, stale_note),
                }
            verdict = _more_restrictive(verdict, w)
            continue

        if resets_at is None or resets_at <= now:
            # No reset horizon to pace against (shouldn't normally happen
            # since the reset_happened branch above handles the passed case
            # unless it fell through with a comfortable estimate) — just go.
            w = {
                "action": "continue", "delay_s": 0, "window": window, "wake_at": None,
                "reason": "%s headroom (%.1f%%) comfortable, no reset horizon to pace against%s%s"
                          % (window, headroom, default_note, stale_note),
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
            }
        verdict = _more_restrictive(verdict, w)

    if verdict is None:
        return {
            "action": "probe", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "no window had both a ceiling and a reading; probing for a usable snapshot",
        }

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

    try:
        decision = decide(snapshot, now, policy, history)
    except Exception as e:  # a pacing bug must never spend usage blind
        decision = {
            "action": "stop", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "pace.py raised %s: %s; stopping rather than trusting a bad decision"
                      % (type(e).__name__, e),
        }
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
