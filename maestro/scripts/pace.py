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
    "captured_at": epoch|None, "captured": {"five_hour": epoch|None,
    "seven_day": epoch|None}, "resets": {"five_hour": epoch|None,
    "seven_day": epoch|None}}. "captured" (per-window freshness times: the
    window's `fresh_at` from usage.json when recorded, else its captured_at)
    is optional; without it the top-level "captured_at" stands in for both
    windows. An end record written by `recover` carries "interrupted": true.
  - usage.json: the latest raw statusline snapshot, {"captured_at": epoch,
    "session_id": str|None, "five_hour": {"used_percentage": float,
    "resets_at": epoch, "captured_at": epoch|None, "fresh_at": epoch|None}
    | None, "seven_day": {...} | None, "sessions": {...}}. "fresh_at" is the
    last tick on which a session that had just received an API response
    reported the window (see statusline.py) — the freshness signal, even
    when the value did not change; a present-but-null fresh_at means never
    confirmed current. Without "fresh_at", the window's "captured_at" (last
    time its value rose), then the top-level one, stand in for it.

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
   completed chunks: a "start" record followed by a non-interrupted "end"
   record for the same chunk number (chunks closed by `recover` are
   excluded from every duration and burn statistic). A chunk's delta for a
   window only counts when its END reading is FRESH for that window: the
   end value is present, its capture time is after the chunk started, and
   it differs from the start reading's capture time. A frozen usage.json
   (headless run, Maestro's statusLine not installed) stamps the same stale
   reading on both ends of every chunk; counting those as zero-burn chunks
   used to drive mean burn to 0 and wave every chunk through. Also required:
   the start value is present (NaN/non-numeric readings count as absent),
   the window did not roll over in between (resets_at within 60s — the
   statusline jitters it by a second or so) and the used percentage did not
   drop (a drop is discarded, not counted as negative burn). Each delta is
   divided by the number of items that chunk claimed (its start record's
   `items`; a record without them counts as one full chunk) and the result
   scaled by the CURRENT `chunk_size` — raising chunk_size mid-sweep (5 ->
   20) must not gate a 20-item chunk on a 5-item delta. Projections count
   the items claimed since the reading the same way. Two figures:
     - `burn` (mean of the deltas, last 5) — used to space chunks out.
     - `gate_burn` (max(largest recent delta, 1.0 percentage point)) — used
       to decide whether one more chunk still fits before the ceiling, and
       (as max(burn, gate_burn)) to project stale or post-reset readings.
   Chunk duration = mean of (end.at - start.at) over the same deltas used
   for `burn`. With zero eligible deltas, fall back to a conservative
   default burn/gate (see `_DEFAULT_BURN`) and duration (see
   `_DEFAULT_DURATION`), and the reason says the estimate is a
   conservative default (no history).

2. **Freshness / projection.** A usage reading is a LOWER BOUND on current
   usage, never an exact one once time has passed:
     - If this window's `resets_at` has already passed relative to `now`,
       the reading predates the reset and tells us nothing about the new
       window. If no chunk has *started* since `resets_at`, usage is
       unknown — that window's contribution is "probe" (see below). If one
       or more chunks have started since `resets_at`, estimate
       `used = max(burn, gate_burn) * chunks_started_since(resets_at)`.
     - Otherwise, if the reading is stale — its freshness time (`fresh_at`,
       else `captured_at`) predates the end of the last chunk, is more than
       10 minutes old, or was never confirmed (fresh_at recorded as null) —
       project it forward: `used + max(burn, gate_burn) *
       chunks_started_since(fresh time)` (counting chunk *starts*, not
       wall-clock time, since wall-clock time with no chunks running tells
       us nothing about burn).
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
   shorter one) > probe > continue — a probe on one window (its usage is
   genuinely unknown, e.g. post-reset with no chunk run since) is never
   masked by a comfortable continue on the other. `window` on the returned
   decision names which window drove the verdict.

6. **Clamping & long waits.** Any sleep delay is clamped to [60, 3600]
   (ScheduleWakeup's own bounds). A true target beyond 3600s away is
   reported as delay_s=3600 with `wake_at` set to the real target (resets_at
   + margin_s, or the paced target) — the caller just calls decide() again
   after waking, which will recompute a fresh (shorter) sleep or continue.
   We deliberately bias a hair past a reset rather than a hair before it
   (margin_s), since sleeping through the reset and waking just after it is
   cheap, while waking early and re-probing before the window actually
   resets just burns another decide()/probe cycle.

7. **No signal.** A chunk is "unread" when it has no fresh end reading for
   any window (see 1; interrupted chunks are neutral). If `snapshot` is
   None, or neither window is present in it, the account may be
   API-key-billed (no rate_limits at all) or the statusline simply hasn't
   captured a reading yet: return "probe" — run one more (small) chunk to
   get a reading, and say usage is unknown in the reason. Once the most
   recent >= 3 chunks in a row are unread — whether or not usage.json
   exists, since a frozen file is no signal either — probing further just
   spends usage blind: the verdict is "stop" with a reason starting "no
   usage signal —" that names both causes (Maestro's statusLine not
   installed — run install.sh — or a session where no status line renders:
   headless -p, and possibly a backgrounded session, which the docs leave
   unspecified), UNLESS `policy.allow_blind` is set, in which case sleep
   `blind_gap_s` between chunks instead (still clamped to [60, 3600], with
   `wake_at` for the remainder when `blind_gap_s` itself is over an hour).
   This is combined with the window verdicts like any other (stop wins).

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
_DEFAULT_ITEM_BURN = 1.0   # percentage points per item (big chunks scale up)

_WINDOWS = ("five_hour", "seven_day")
_MIN_DELAY = 60
_MAX_DELAY = 3600
_BLIND_PROBE_LIMIT = 3
_STALE_AFTER_S = 600.0  # 10 minutes
_RESET_JITTER_S = 60.0  # resets_at within this is the same window

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
    used_percentage / timestamp read against NaN, inf and non-numeric junk so
    a bad reading is treated as an absent one rather than raising or
    silently poisoning a mean/projection."""
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


def _chunks(history):
    """[(start, end_or_None)] for every chunk with a start record, in the
    order the starts appear."""
    order = []
    starts = {}
    ends = {}
    for rec in history or []:
        if not isinstance(rec, dict):
            continue
        chunk = rec.get("chunk")
        event = rec.get("event")
        if event == "start" and chunk not in starts:
            starts[chunk] = rec
            order.append(chunk)
        elif event == "end" and chunk in starts and chunk not in ends:
            ends[chunk] = rec
    return [(starts[c], ends.get(c)) for c in order]


def _completed_chunks(history):
    """(start, end) pairs for chunks that ended normally — a chunk closed by
    `recover` (end.interrupted) ran for an unknown share of its items and
    its end reading was taken whenever recovery happened, so it is excluded
    from duration and burn statistics."""
    return [(s, e) for s, e in _chunks(history)
            if e is not None and not e.get("interrupted")]


def _chunk_start_times(history):
    return sorted(
        rec.get("at") for rec in (history or [])
        if isinstance(rec, dict) and rec.get("event") == "start"
        and _finite_number(rec.get("at")) is not None
    )


def _chunks_since(history, threshold_at):
    """Count of chunks that *started* at or after `threshold_at`. Used to
    turn a stale or post-reset reading into a burn-based estimate instead of
    a wall-clock guess — only actual chunk activity can have spent usage."""
    if threshold_at is None:
        return 0
    return sum(1 for at in _chunk_start_times(history) if at >= threshold_at)


def _reading(rec, window):
    """(used, resets_at, captured_at) of `window` in a pace record's usage
    snapshot; each None when absent or non-finite."""
    usage = (rec or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return None, None, None
    used = _finite_number(usage.get(window))
    resets = _finite_number((usage.get("resets") or {}).get(window))
    captured_map = usage.get("captured")
    if isinstance(captured_map, dict):
        captured = _finite_number(captured_map.get(window))
    else:
        captured = _finite_number(usage.get("captured_at"))
    return used, resets, captured


def _fresh_end(start, end, window):
    """True when `end` carries a reading of `window` actually taken during
    this chunk: present, captured after the chunk started, and not the same
    capture the start record saw."""
    eu, _, e_cap = _reading(end, window)
    if eu is None or e_cap is None:
        return False
    start_at = _finite_number((start or {}).get("at"))
    if start_at is None or e_cap <= start_at:
        return False
    _, _, s_cap = _reading(start, window)
    return s_cap != e_cap


def _same_window(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= _RESET_JITTER_S


def _trailing_unread(history):
    """How many of the most recent chunks in a row had no fresh end reading
    for any window. Interrupted chunks are neutral (skipped); a chunk with
    no end record yet counts as unread."""
    count = 0
    for start, end in _chunks(history):
        if end is not None and end.get("interrupted"):
            continue
        if end is not None and any(_fresh_end(start, end, w) for w in _WINDOWS):
            count = 0
        else:
            count += 1
    return count


def _item_count(start, chunk_size):
    """Items a chunk claimed — its start record's `items`; a record without
    them (hand-written history) counts as one full chunk of the current
    size."""
    items = (start or {}).get("items")
    return len(items) if isinstance(items, list) and items else chunk_size


def _items_since(history, threshold_at, chunk_size):
    """Items claimed by chunks that started at or after `threshold_at` (all
    chunks when it is None)."""
    total = 0
    for start, _ in _chunks(history):
        at = _finite_number(start.get("at"))
        if at is None or (threshold_at is not None and at < threshold_at):
            continue
        total += _item_count(start, chunk_size)
    return total


def _window_deltas(pairs, window, chunk_size, limit=5):
    """(used_delta PER ITEM, duration) for each completed chunk with a fresh
    end reading for `window`, most recent last, capped to the last `limit`.
    Per item, so a chunk_size raised mid-sweep (set-policy 5 -> 20) scales
    the estimate instead of gating a 20-item chunk on a 5-item delta."""
    deltas = []
    for start, end in pairs:
        if not _fresh_end(start, end, window):
            continue
        su, s_resets, _ = _reading(start, window)
        eu, e_resets, _ = _reading(end, window)
        if su is None:
            continue
        if not _same_window(s_resets, e_resets):
            continue  # window reset mid-chunk; delta is meaningless
        if eu < su:
            continue  # used dropped without a resets_at change — bad data
        s_at = _finite_number(start.get("at"))
        e_at = _finite_number(end.get("at"))
        if s_at is None or e_at is None or e_at < s_at:
            continue
        deltas.append(((eu - su) / _item_count(start, chunk_size), e_at - s_at))
    return deltas[-limit:]


def _burn_and_duration(pairs, window, chunk_size=None):
    """Return (burn_per_chunk, gate_burn, mean_duration, used_default: bool),
    both burns for a chunk of the CURRENT `chunk_size` items (per-item
    deltas scaled up; a probe is 1 item, so this is its upper bound).

    `burn` is the mean delta — a robust average for spacing.
    `gate_burn` is max(largest recent delta, 1.0) — the floor used to decide
    whether one more chunk still fits before the ceiling (and, via
    max(burn, gate_burn), to project readings forward), so a run of
    zero/near-zero coarse readings (or a mean pulled down by a few quiet
    chunks) can never wave a chunk through right at, or past, the ceiling."""
    chunk_size = chunk_size or 1
    deltas = _window_deltas(pairs, window, chunk_size)
    if not deltas:
        burn = max(_DEFAULT_BURN, _DEFAULT_ITEM_BURN * chunk_size)
        return burn, max(burn, 1.0), _DEFAULT_DURATION, True
    burns = [d * chunk_size for d, _ in deltas]
    durations = [d for _, d in deltas]
    mean_burn = sum(burns) / len(burns)
    gate_burn = max(max(burns), 1.0)
    return mean_burn, gate_burn, sum(durations) / len(durations), False


_RANK = {"continue": 0, "probe": 1, "sleep": 2, "stop": 3}


def _more_restrictive(a, b):
    """Pick the more restrictive of two per-window verdicts: rank first
    (stop > sleep > probe > continue), then — for two sleeps — the longer
    delay_s. `probe` outranks a plain `continue` because a post-reset probe
    means one window's usage is genuinely unknown — a comfortable reading
    on the *other* window must never mask that and wave a chunk through
    anyway. A continue/probe verdict carries no wake time at all, so
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


def _blind_verdict(unread, now, allow_blind, blind_gap_s):
    """The verdict once >= _BLIND_PROBE_LIMIT chunks in a row ended with no
    fresh usage reading, else None."""
    if unread < _BLIND_PROBE_LIMIT:
        return None
    if allow_blind:
        target = now + blind_gap_s
        return {
            "action": "sleep",
            "delay_s": _clamp_delay(blind_gap_s),
            "window": None,
            "wake_at": target if blind_gap_s > _MAX_DELAY else None,
            "reason": "no usage signal after %d chunks; allow_blind is set, "
                      "sleeping %ds between chunks instead of running unpaced"
                      % (unread, blind_gap_s),
        }
    return {
        "action": "stop", "delay_s": 0, "window": None, "wake_at": None,
        "reason": "no usage signal — %d chunk(s) in a row ended with no fresh usage "
                  "reading. Run install.sh so Maestro's statusLine records rate_limits, "
                  "or set allow_blind. The status line only runs where Claude Code "
                  "renders one: never in a headless -p run, and the docs don't say "
                  "whether a backgrounded (agent view) session renders it, so a sweep "
                  "loop moved to the background may be blind even when it is installed"
                  % unread,
    }


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
    chunk_size = policy["chunk_size"]

    blind = _blind_verdict(_trailing_unread(history), now, allow_blind, blind_gap_s)

    if not snapshot or not any(snapshot.get(w) for w in _WINDOWS):
        if blind is not None:
            return blind
        return {
            "action": "probe", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "usage unknown (no snapshot yet); running one chunk to get a reading",
        }

    pairs = _completed_chunks(history)
    end_ats = [_finite_number(r.get("at")) for r in (history or [])
               if isinstance(r, dict) and r.get("event") == "end"]
    last_chunk_end_at = max((a for a in end_ats if a is not None), default=None)

    verdict = None
    for window in _WINDOWS:
        reading = snapshot.get(window)
        ceiling = ceilings.get(window)
        if not isinstance(reading, dict) or ceiling is None:
            continue

        used = _finite_number(reading.get("used_percentage"))
        if used is None:
            continue
        resets_at = _finite_number(reading.get("resets_at"))
        captured_at = _finite_number(reading.get("captured_at"))
        if captured_at is None:
            captured_at = _finite_number(snapshot.get("captured_at"))
        # Freshness: `fresh_at` (last tick an actively-responding session
        # reported this window) when the statusline records it; a recorded
        # but null fresh_at means never confirmed current -> always stale.
        # Older files only have the value-rose `captured_at`.
        never_fresh = False
        if "fresh_at" in reading:
            fresh_at = _finite_number(reading.get("fresh_at"))
            never_fresh = fresh_at is None
        else:
            fresh_at = captured_at

        burn, gate_burn, duration, used_default = _burn_and_duration(pairs, window, chunk_size)
        # Projections count the items actually claimed since the reading,
        # at max(burn, gate_burn) per current-size chunk.
        proj_item = max(burn, gate_burn) / chunk_size
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
            projected = proj_item * _items_since(history, resets_at, chunk_size)
            stale_note = " (window reset since reading; estimated from %d chunk(s) run since reset)" \
                % chunks_since_reset
        else:
            stale = (
                never_fresh
                or (last_chunk_end_at is not None and fresh_at is not None and fresh_at < last_chunk_end_at)
                or (fresh_at is not None and (now - fresh_at) > _STALE_AFTER_S)
            )
            if stale:
                since = fresh_at if fresh_at is not None else captured_at
                chunks_since_reading = (_chunks_since(history, since) if since is not None
                                        else len(_chunk_start_times(history)))
                projected = used + proj_item * _items_since(history, since, chunk_size)
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
            # No reset horizon to pace against (absent, or passed with a
            # comfortable post-reset estimate) — just go.
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
        if blind is not None:
            return blind
        return {
            "action": "probe", "delay_s": 0, "window": None, "wake_at": None,
            "reason": "no window had both a ceiling and a reading; probing for a usable snapshot",
        }

    return _more_restrictive(verdict, blind)


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
