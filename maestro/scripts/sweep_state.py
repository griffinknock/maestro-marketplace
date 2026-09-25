#!/usr/bin/env python3
"""Maestro sweep state — a checkpointed store for long, usage-paced runs.

A sweep is a frozen plan plus an item index worked in chunks, so a long run
can be interrupted (compaction, a crash, a new session) and resumed exactly
where it left off, without re-deriving what is done.

Layout, under `$MAESTRO_SWEEPS_DIR/<slug>/`, else
`<git toplevel of cwd>/.claude/maestro/sweeps/<slug>/` (already covered by
this repo's `.claude/maestro/` gitignore):

  plan.md            frozen at `new`; never rewritten by any command
  plan.v2.md, ...    plan amendments (adaptive/autonomous only)
  index.json         slug, created, plan_sha256, policy_sha256, items_sha256,
                      original_count, plan_versions, policy_versions, items[]
  policy.json        the CURRENT policy: ceilings, deviation, chunk_size,
                      margin_s, max_attempts, allow_blind, blind_gap_s —
                      its sha256 must equal index.policy_sha256
  policy.v1.json ... every policy version, append-only (v1 at `new`, one
                      more per `set-policy`)
  findings.jsonl     append-only — one line per `finding`
  amendments.jsonl   append-only — one line per `add` (op add_item),
                      `amend-plan` (amend_plan), `set-policy` (set_policy),
                      and chunk takeover by `recover` (takeover)
  pace.jsonl         two lines per chunk: start (carries "owner"), then end

`pace.py` reads these files; the schemas here are the contract with it — see
each write site below.

Owner: `next`, `recover` and `end-chunk` act for an owner — the Claude Code
session id. Claude Code exports it to every Bash-tool subprocess as
`$CLAUDE_CODE_SESSION_ID` (the same value hooks receive as `session_id`), so
these commands read it from the environment; `--owner` overrides it (tests,
scripts). With neither, they refuse (exit 2) rather than guess an identity
another session could share.

Exit codes: 0 ok · 1 `check` failed · 2 invalid input / no such sweep /
refused · 3 `next`: nothing pending or running (the sweep is finished) ·
4 another live session owns the open chunk (or holds the only running
items) · 5 the sweep lock is busy.

Every write is atomic (tmp file + os.replace, tmp always cleaned up) and
every command that mutates a sweep's files takes a `.sweep.lock` directory
lock, matching the proper-lockfile-compatible pattern in `ledger.py`. On
sustained contention the lock raises rather than silently proceeding
unlocked — see `Lock`/`LockTimeout`.

Usage: see `build_parser()`. `anchor` is special — it is the SessionStart
hook body, reads a hook payload on stdin, and never raises or exits nonzero.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"

DEFAULT_POLICY = {
    "ceilings": {"five_hour": 80, "seven_day": 90},
    "deviation": "additive",
    "chunk_size": 5,
    "margin_s": 120,
    "max_attempts": 2,
    "allow_blind": False,
    "blind_gap_s": 900,
}
STATUSES = ("pending", "running", "done", "failed")
DEVIATIONS = ("locked", "additive", "adaptive", "autonomous")
# Which recorded deviation allows which amendment op.
OP_ALLOWED = {
    "add_item": ("additive", "adaptive", "autonomous"),
    "amend_plan": ("adaptive", "autonomous"),
}
AMENDMENT_OPS = ("add_item", "amend_plan", "set_policy", "takeover")
LOCK_STALE = float(os.environ.get("MAESTRO_LOCK_STALE_S", "10.0"))
LOCK_TIMEOUT_S = float(os.environ.get("MAESTRO_LOCK_TIMEOUT_S", "4.0"))
# A holder on another host (shared filesystem) is presumed gone after this.
LOCK_HARD_STALE_S = float(os.environ.get("MAESTRO_LOCK_HARD_STALE_S", "3600"))
# A same-host holder whose pid is "alive" this long after locking is taken to
# be pid reuse (e.g. after a reboot), not a live holder.
LOCK_PID_REUSE_S = 86400.0
MAX_RECOVERIES = 3
USAGE_FILE_DEFAULT = str(Path.home() / ".claude" / "maestro" / "usage.json")
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
DEFAULT_STALE_AFTER_S = 7200.0
LEASE_MARGIN_S = 600.0   # a loop lease outlives its wake time by this much


# ── locking + atomic writes ──────────────────────────────────────────────

class LockTimeout(Exception):
    """Raised when a `.sweep.lock` could not be acquired before the
    contention timeout. Callers must never proceed unlocked — `main()`
    catches this once, for every command, and exits 5."""


HOST = socket.gethostname()


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


class Lock:
    """mkdir-to-acquire — the same directory-lock convention as ledger.py's
    `_Lock`, so a sweep directory and a ledger session dir can never deadlock
    each other by disagreeing on a convention.

    The lock directory holds a `holder` file ({"pid", "host", "at"}). A lock
    is broken as stale only when its holder is provably gone: same host and
    that pid is no longer alive, or (different host, or a pid alive for an
    implausibly long time — pid reuse) its mtime is very old. A bare mtime
    age is NOT enough: across a laptop sleep every lock looks old while its
    holder is merely suspended. Only a holder-less lock (a legacy one, or
    one caught between mkdir and the holder write) falls back to the short
    mtime rule (`LOCK_STALE`).

    On sustained contention (`LOCK_TIMEOUT_S`, default 4s) this raises
    `LockTimeout` instead of returning unlocked. Callers check the sweep
    exists first (`require_sweep`), so a typo'd slug is "no such sweep",
    never "busy".
    """

    def __init__(self, target):
        self.p = Path(str(target) + ".lock")
        self.held = False

    def _holder(self):
        try:
            h = json.loads((self.p / "holder").read_text())
            return h if isinstance(h, dict) else None
        except (OSError, ValueError):
            return None

    def _stale(self):
        try:
            age = time.time() - self.p.stat().st_mtime
        except OSError:
            return False
        h = self._holder()
        if h is None:
            return age > LOCK_STALE
        if h.get("host") == HOST:
            return not pid_alive(h.get("pid")) or age > LOCK_PID_REUSE_S
        return age > LOCK_HARD_STALE_S

    def _break(self):
        # Rename first (atomic): of two contenders that both judged it stale,
        # only one moves it; the other's rename fails and it just retries.
        tomb = self.p.with_name(f"{self.p.name}.stale{os.getpid()}.{time.time_ns()}")
        try:
            os.rename(self.p, tomb)
        except OSError:
            return
        shutil.rmtree(tomb, ignore_errors=True)

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT_S
        while time.time() < deadline:
            try:
                self.p.mkdir()
            except FileExistsError:
                if self._stale():
                    self._break()
                    continue
                time.sleep(0.02)
                continue
            except OSError:
                break
            self.held = True
            try:
                (self.p / "holder").write_text(json.dumps(
                    {"pid": os.getpid(), "host": HOST, "at": time.time()}))
            except OSError:
                pass
            return self
        raise LockTimeout(f"refused: could not acquire lock (busy): {self.p}")

    def __exit__(self, *_):
        if self.held:
            self.held = False       # released once; re-entry must not remove twice
            h = self._holder()
            if h is None or (h.get("pid") == os.getpid() and h.get("host") == HOST):
                try:
                    (self.p / "holder").unlink()
                except OSError:
                    pass
                try:
                    self.p.rmdir()
                except OSError:
                    pass
        return False


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def policy_text(policy):
    """The exact on-disk text of a policy file — every policy write goes
    through this so its sha256 is reproducible."""
    return json.dumps(policy, indent=2, default=str)


def write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, indent=2, default=str))


def read_jsonl(path, strict=False):
    """Parsed lines of a jsonl file, oldest first. Blank lines are skipped.

    `strict=True` raises on the first line that does not parse — used by
    `check` to name the file as broken rather than silently truncating it.
    """
    path = Path(path)
    out = []
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            if strict:
                raise
            continue
    return out


def append_jsonl(path, obj):
    """Atomic append: read the whole file, add one line, replace it.

    A plain `open(..., 'a')` is not atomic against a reader mid-write and can
    leave a half-written trailing line after a crash; this never can, at the
    cost of rewriting the file on every append (these files stay small — one
    or two lines per chunk/finding/amendment for the life of a sweep).
    """
    path = Path(path)
    line = json.dumps(obj, default=str)
    prefix = path.read_text() if path.exists() else ""
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    atomic_write_text(path, prefix + line + "\n")


def sha256_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def items_hash(items):
    """sha256 over the ordered (id, label) pairs — the check gate's ground
    truth for "were the items edited outside add/amend": a relabel, a
    removal, or a reorder all change this even when ids stay gap-free."""
    pairs = [[it.get("id"), it.get("label")] for it in items]
    return sha256_of(json.dumps(pairs))


def iso(ts):
    try:
        return datetime.fromtimestamp(float(ts or 0), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


# ── locating a sweep ─────────────────────────────────────────────────────

def git_toplevel(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd)


def sweeps_root(cwd=None):
    env = os.environ.get("MAESTRO_SWEEPS_DIR")
    if env:
        return Path(env)
    return git_toplevel(cwd or os.getcwd()) / ".claude" / "maestro" / "sweeps"


def sweep_dir(slug, cwd=None):
    return sweeps_root(cwd) / slug


def require_sweep(d, slug):
    """Exit code 2 (and a message) when there is no such sweep — checked
    BEFORE taking the lock, since a missing directory makes the lock's mkdir
    fail and used to be misreported as "busy" (exit 5)."""
    if not (d / "index.json").is_file():
        print(f"no such sweep: {slug}", file=sys.stderr)
        return 2
    return None


def resolve_owner(args):
    """--owner, else this Claude Code session's id, else None."""
    owner = getattr(args, "owner", None) or os.environ.get(SESSION_ENV) or ""
    owner = owner.strip()
    return owner or None


def no_owner():
    print(f"refused: no owner — ${SESSION_ENV} is unset (run this from Claude Code's "
          f"Bash tool) and no --owner was given", file=sys.stderr)
    return 2


# ── usage (written by statusline.py) ─────────────────────────────────────

def usage_snapshot():
    """U, as defined in the pace.jsonl contract — read fresh, every call.

    usage.json (written by statusline.py) carries, per window, `fresh_at` —
    the last time an actively-responding session reported it — and the
    older `captured_at` (last time the value rose). "captured" holds each
    window's freshness time (fresh_at when recorded, else captured_at):
    pace.py only counts a chunk's burn when the END reading was fresh during
    the chunk, and a frozen or idle usage.json must not pass for a fresh
    one. The top-level captured_at is their max (or the file's own).
    """
    path = Path(os.environ.get("MAESTRO_USAGE_FILE") or USAGE_FILE_DEFAULT)
    d = read_json(path, {}) or {}
    if not isinstance(d, dict):
        d = {}
    five = d.get("five_hour") if isinstance(d.get("five_hour"), dict) else {}
    seven = d.get("seven_day") if isinstance(d.get("seven_day"), dict) else {}
    top = d.get("captured_at")

    def fresh_time(w):
        # `fresh_at` (the last tick a session that had just received an API
        # response reported this window) when the statusline records it —
        # even None, meaning "never confirmed current" — else the older
        # value-rose `captured_at`.
        if not w:
            return None
        if "fresh_at" in w:
            return w.get("fresh_at")
        return w.get("captured_at", top)

    captured = {"five_hour": fresh_time(five), "seven_day": fresh_time(seven)}
    caps = [c for c in captured.values() if c is not None]
    return {
        "five_hour": five.get("used_percentage"),
        "seven_day": seven.get("used_percentage"),
        "captured_at": max(caps) if caps else top,
        "captured": captured,
        "resets": {
            "five_hour": five.get("resets_at"),
            "seven_day": seven.get("resets_at"),
        },
    }


# ── policy validation (C4) ────────────────────────────────────────────────

def validate_policy(policy):
    """Returns None if `policy` is a fully-shaped, valid policy; otherwise a
    short human-readable reason. Every field is required — callers that want
    defaults must merge them in (`merge_policy(DEFAULT_POLICY, ...)`) before
    validating, e.g. `new`."""
    if not isinstance(policy, dict):
        return "policy is not a JSON object"

    ceilings = policy.get("ceilings")
    if not isinstance(ceilings, dict):
        return "ceilings must be an object"
    for k in ("five_hour", "seven_day"):
        v = ceilings.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 100):
            return f"ceilings.{k} must be a number in (0, 100]"

    if policy.get("deviation") not in DEVIATIONS:
        return "deviation must be exactly one of " + "|".join(DEVIATIONS)

    chunk_size = policy.get("chunk_size")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        return "chunk_size must be an integer >= 1"

    margin_s = policy.get("margin_s")
    if isinstance(margin_s, bool) or not isinstance(margin_s, int) or margin_s < 0:
        return "margin_s must be an integer >= 0"

    max_attempts = policy.get("max_attempts")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        return "max_attempts must be an integer >= 1"

    allow_blind = policy.get("allow_blind")
    if not isinstance(allow_blind, bool):
        return "allow_blind must be a boolean"

    blind_gap_s = policy.get("blind_gap_s")
    if isinstance(blind_gap_s, bool) or not isinstance(blind_gap_s, int) or blind_gap_s < 60:
        return "blind_gap_s must be an integer >= 60"

    return None


def load_policy(d, index):
    """Read + verify + validate this sweep's policy.json. Returns (policy,
    None) on success or (None, reason) on failure. Missing, edited (sha256 !=
    index.policy_sha256), corrupt or invalid policy.json is always an error
    here — an existing sweep never silently falls back to DEFAULT_POLICY, and
    an edited policy.json (say, locked -> autonomous for one `add`, then
    restored) must never be honoured. `set-policy` is the only way to change
    a sweep's policy."""
    path = d / "policy.json"
    try:
        text = path.read_text()
    except OSError:
        return None, "policy.json is missing"
    if sha256_of(text) != index.get("policy_sha256"):
        return None, ("policy.json does not match policy_sha256 — it was edited "
                      "outside set-policy")
    try:
        policy = json.loads(text)
    except json.JSONDecodeError:
        return None, "policy.json is not valid JSON"
    err = validate_policy(policy)
    if err:
        return None, f"invalid policy: {err}"
    return policy, None


# ── small helpers shared across commands ─────────────────────────────────

def merge_policy(base, override):
    out = dict(base)
    for k, v in override.items():
        if k == "ceilings" and isinstance(v, dict):
            out["ceilings"] = {**base.get("ceilings", {}), **v}
        else:
            out[k] = v
    return out


def parse_items(raw):
    """One label per line, or a JSON list of labels."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            return [str(x) for x in obj]
    except json.JSONDecodeError:
        pass
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def find_item(index, item_id):
    return next((it for it in index.get("items", []) if it.get("id") == item_id), None)


def counts(index):
    c = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    for it in index.get("items", []):
        c[it.get("status", "pending")] = c.get(it.get("status", "pending"), 0) + 1
    return c


def chunk_count(pace):
    return len({r.get("chunk") for r in pace if r.get("event") == "start"})


def open_chunks(pace):
    """Every chunk with a `start` record and no matching `end`, as the start
    record itself (carries chunk/owner/at/items) — one per still-open chunk,
    not just the newest (C3: `recover`/`end-chunk` must be able to name and
    act on the specific owner's chunk, not "the last open one")."""
    starts = {}
    ended = set()
    for r in pace:
        if not isinstance(r, dict):
            continue
        c = r.get("chunk")
        if r.get("event") == "start":
            starts[c] = r
        elif r.get("event") == "end":
            ended.add(c)
    return [r for c, r in starts.items() if c not in ended]


def last_open_chunk(pace):
    """The newest chunk with a `start` and no matching `end`, or None."""
    chunks = open_chunks(pace)
    return max((r.get("chunk") for r in chunks), default=None)


def next_item_id(index):
    existing = {it.get("id") for it in index.get("items", [])}
    n = len(index.get("items", [])) + 1
    while f"i-{n:04d}" in existing:
        n += 1
    return f"i-{n:04d}"


def return_to_pending(it):
    """A claimed item that came back unreported: back to pending with a
    recovery counted (never an attempt — only `fail` spends attempts), or
    failed once it has been recovered MAX_RECOVERIES times."""
    it["recoveries"] = int(it.get("recoveries") or 0) + 1
    if it["recoveries"] >= MAX_RECOVERIES:
        it["status"] = "failed"
        it["note"] = f"failed after {it['recoveries']} recoveries"
    else:
        it["status"] = "pending"


def finding_exists(d, fid):
    return any(f.get("id") == fid for f in read_jsonl(d / "findings.jsonl"))


# ── loop lease ───────────────────────────────────────────────────────────
# An open chunk only proves ownership while it is open; a /loop sleeping
# between chunks holds nothing. lease.json ({"owner", "until", "at",
# "reason"}) closes that gap: `next` takes/renews it at every chunk start,
# the skill renews it with `lease` right before every ScheduleWakeup (until
# = wake time + LEASE_MARGIN_S), and `lease --release` drops it when the
# loop ends. While it is live, `next`/`recover`/`lease` from any other
# session exit 4 — the same takeover path as an open chunk.

def read_lease(d):
    lease = read_json(d / "lease.json")
    if not isinstance(lease, dict) or not isinstance(lease.get("owner"), str):
        return None
    until = lease.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)) or until != until:
        return None
    return lease


def write_lease(d, owner, until, reason):
    write_json(d / "lease.json", {"owner": owner, "until": until, "at": time.time(),
                                  "reason": reason})


def lease_busy(d, owner, now, slug):
    """The busy line when another session holds a live lease, else None."""
    lease = read_lease(d)
    if not lease or lease["owner"] == owner or lease["until"] <= now:
        return None
    return (f"busy: sweep leased by {lease['owner']} until {iso(lease['until'])} (its /loop is "
            f"between chunks) — if that session is dead, take it over with `recover "
            f"--takeover` (/maestro:sweep takeover {slug}); the lease lapses on its own "
            f"at that time")


# ── commands ──────────────────────────────────────────────────────────

def cmd_new(args):
    d = sweep_dir(args.slug)
    if (d / "index.json").exists():
        print(f"sweep already exists: {args.slug}", file=sys.stderr)
        return 2
    try:
        plan_text = Path(args.plan).read_text()
        items_raw = Path(args.items).read_text()
    except OSError as e:
        print(f"cannot read input: {e}", file=sys.stderr)
        return 2
    labels = parse_items(items_raw)
    policy = dict(DEFAULT_POLICY)
    if args.policy:
        try:
            override = json.loads(args.policy)
        except json.JSONDecodeError:
            print("invalid --policy JSON", file=sys.stderr)
            return 2
        if not isinstance(override, dict):
            print("invalid --policy JSON: must be an object", file=sys.stderr)
            return 2
        policy = merge_policy(policy, override)
    err = validate_policy(policy)
    if err:
        print(f"invalid policy: {err}", file=sys.stderr)
        return 2

    d.mkdir(parents=True, exist_ok=True)
    with Lock(d / ".sweep"):
        now = time.time()
        atomic_write_text(d / "plan.md", plan_text)
        ptext = policy_text(policy)
        atomic_write_text(d / "policy.v1.json", ptext)
        atomic_write_text(d / "policy.json", ptext)
        policy_sha256 = sha256_of(ptext)
        items = [{"id": f"i-{i + 1:04d}", "label": lbl, "status": "pending",
                  "attempts": 0, "recoveries": 0, "from_finding": None, "note": None}
                 for i, lbl in enumerate(labels)]
        index = {"slug": args.slug, "created": now,
                 "plan_sha256": sha256_of(plan_text),
                 "policy_sha256": policy_sha256,
                 "items_sha256": items_hash(items),
                 "original_count": len(items),
                 "plan_versions": [],
                 "policy_versions": [{"version": 1, "file": "policy.v1.json",
                                      "sha256": policy_sha256, "at": now}],
                 "items": items}
        write_json(d / "index.json", index)
        for fname in ("findings.jsonl", "amendments.jsonl", "pace.jsonl"):
            fp = d / fname
            if not fp.exists():
                atomic_write_text(fp, "")
    print(json.dumps({"slug": args.slug, "dir": str(d), "items": len(items)}))
    return 0


def cmd_status(args):
    d = sweep_dir(args.slug)
    index = read_json(d / "index.json")
    if index is None:
        print(f"no such sweep: {args.slug}", file=sys.stderr)
        return 2
    c = counts(index)
    chunks = chunk_count(read_jsonl(d / "pace.jsonl"))
    if args.json:
        print(json.dumps({"slug": args.slug, "chunks": chunks, **c}))
    else:
        print(f"{args.slug}: {c['done']} done, {c['failed']} failed, "
              f"{c['pending']} pending, {c['running']} running — chunk {chunks}")
    return 0


def cmd_list(args):
    root = sweeps_root()
    if not root.is_dir():
        return 0
    for p in sorted(root.iterdir()):
        index = read_json(p / "index.json")
        if not index:
            continue
        c = counts(index)
        chunks = chunk_count(read_jsonl(p / "pace.jsonl"))
        print(f"{p.name}: {c['done']} done, {c['failed']} failed, "
              f"{c['pending']} pending, {c['running']} running — chunk {chunks}")
    return 0


def cmd_next(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    if args.n is not None and args.n < 1:
        print(f"invalid --n {args.n}: a chunk claims at least one item", file=sys.stderr)
        return 2
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d, index)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        now = time.time()
        busy = lease_busy(d, owner, now, args.slug)
        if busy:
            print(json.dumps([]))
            print(busy, file=sys.stderr)
            return 4
        n = args.n if args.n is not None else policy.get("chunk_size", DEFAULT_POLICY["chunk_size"])
        claimed = []
        for it in index["items"]:
            if len(claimed) >= n:
                break
            if it.get("status") == "pending":
                it["status"] = "running"
                claimed.append(it)
        if not claimed:
            # Write nothing either way (an empty start record here would
            # later make `check` fail forever on a finished sweep).
            print(json.dumps([]))
            running = [it["id"] for it in index["items"] if it.get("status") == "running"]
            if not running:
                return 3  # nothing pending or running: the sweep is finished
            holders = [c for c in open_chunks(read_jsonl(d / "pace.jsonl"))
                       if set(c.get("items") or []) & set(running)]
            if holders:
                ch = max(holders, key=lambda c: c.get("chunk") or 0)
                print(f"busy: nothing pending; {len(running)} item(s) still running in "
                      f"chunk {ch.get('chunk')} owned by {ch.get('owner')} since "
                      f"{iso(ch.get('at'))}", file=sys.stderr)
            else:
                print(f"busy: nothing pending; {len(running)} item(s) still running with "
                      f"no open chunk — run recover", file=sys.stderr)
            return 4
        write_json(d / "index.json", index)
        pace = read_jsonl(d / "pace.jsonl")
        chunk_no = max([r.get("chunk", 0) for r in pace if r.get("event") == "start"],
                       default=0) + 1
        append_jsonl(d / "pace.jsonl", {
            "chunk": chunk_no, "event": "start", "at": time.time(),
            "items": [it["id"] for it in claimed], "usage": usage_snapshot(),
            "owner": owner,
        })
        # Chunk start holds the loop lease (never shortening this owner's
        # own longer lease): a chunk is how a loop proves it is alive.
        held = read_lease(d)
        until = now + LEASE_MARGIN_S
        if held and held["owner"] == owner:
            until = max(until, held["until"])
        write_lease(d, owner, until, "chunk")
    print(json.dumps([{"id": it["id"], "label": it["label"]} for it in claimed]))
    return 0


def not_mine(d, it, owner):
    """Why `owner` may not record a result for item `it`, or None. A result
    only lands on an item that is `running` in an open chunk this owner
    holds: a zombie loop's late `done`/`fail` must never finish (or spend an
    attempt on) an item that was reclaimed and re-claimed by another loop."""
    holders = [c for c in open_chunks(read_jsonl(d / "pace.jsonl"))
               if it.get("id") in (c.get("items") or [])]
    if it.get("status") == "running" and any(c.get("owner") == owner for c in holders):
        return None
    where = (f"chunk {holders[-1].get('chunk')} owned by {holders[-1].get('owner')}"
             if holders else "no open chunk")
    return (f"refused: {it.get('id')} is not running in an open chunk owned by this session "
            f"(status {it.get('status')}, {where}) — its result was not recorded")


def renew_lease(d, owner, now):
    """A result from the loop's owner proves it alive: extend its lease to at
    least now + LEASE_MARGIN_S (never shortening it, never touching another
    session's live lease)."""
    held = read_lease(d)
    if held and held["owner"] != owner and held["until"] > now:
        return
    until = now + LEASE_MARGIN_S
    if held and held["owner"] == owner:
        until = max(until, held["until"])
    write_lease(d, owner, until, "result")


def cmd_done(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        it = find_item(index, args.item)
        if it is None:
            print(f"no such item: {args.item}", file=sys.stderr)
            return 2
        why = not_mine(d, it, owner)
        if why:
            print(why, file=sys.stderr)
            return 4
        it["status"] = "done"
        if args.note:
            it["note"] = args.note
        write_json(d / "index.json", index)
        renew_lease(d, owner, time.time())
    return 0


def cmd_fail(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        it = find_item(index, args.item)
        if it is None:
            print(f"no such item: {args.item}", file=sys.stderr)
            return 2
        why = not_mine(d, it, owner)
        if why:
            print(why, file=sys.stderr)
            return 4
        policy, err = load_policy(d, index)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        max_attempts = policy.get("max_attempts", DEFAULT_POLICY["max_attempts"])
        it["attempts"] = int(it.get("attempts") or 0) + 1
        it["note"] = args.reason
        it["status"] = "pending" if it["attempts"] < max_attempts else "failed"
        write_json(d / "index.json", index)
        renew_lease(d, owner, time.time())
        status = it["status"]
    print(status)
    return 0


def cmd_finding(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    with Lock(d / ".sweep"):
        n = len(read_jsonl(d / "findings.jsonl")) + 1
        fid = f"f-{n:04d}"
        append_jsonl(d / "findings.jsonl", {
            "id": fid, "at": time.time(), "item": args.item, "text": args.text,
        })
    print(fid)
    return 0


def cmd_add(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d, index)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        deviation = policy.get("deviation")
        if deviation not in OP_ALLOWED["add_item"]:
            print(f"refused: deviation policy is {deviation}", file=sys.stderr)
            return 2
        if not finding_exists(d, args.from_finding):
            print(f"refused: no such finding: {args.from_finding}", file=sys.stderr)
            return 2
        new_id = next_item_id(index)

        # Write the amendment BEFORE the index: a crash between the two used
        # to leave an item in index.json with no amendment (a "ghost" that
        # `check` couldn't see); an amendment with no matching item is
        # already something `check` catches, so ordering it first means any
        # half-finished `add` is visible instead of invisible.
        append_jsonl(d / "amendments.jsonl", {
            "at": time.time(), "op": "add_item", "item": new_id,
            "from_finding": args.from_finding, "policy": deviation,
        })
        index["items"].append({
            "id": new_id, "label": args.label, "status": "pending",
            "attempts": 0, "recoveries": 0, "from_finding": args.from_finding, "note": None,
        })
        index["items_sha256"] = items_hash(index["items"])
        write_json(d / "index.json", index)
    print(new_id)
    return 0


def cmd_amend_plan(args):
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d, index)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        deviation = policy.get("deviation")
        if deviation not in OP_ALLOWED["amend_plan"]:
            print(f"refused: amend-plan requires adaptive or autonomous deviation "
                  f"(current: {deviation})", file=sys.stderr)
            return 2
        if not finding_exists(d, args.from_finding):
            print(f"refused: no such finding: {args.from_finding}", file=sys.stderr)
            return 2
        try:
            new_text = Path(args.file).read_text()
        except OSError as e:
            print(f"cannot read --file: {e}", file=sys.stderr)
            return 2
        sha = sha256_of(new_text)
        # plan.md is implicitly v1; the first amendment is v2.
        plan_versions = index.get("plan_versions", [])
        vn = len(plan_versions) + 2
        fname = f"plan.v{vn}.md"
        tip = plan_versions[-1].get("sha256") if plan_versions else index.get("plan_sha256")
        now = time.time()
        atomic_write_text(d / fname, new_text)   # plan.md itself is never touched
        # Amendment before index (same reasoning as `add`): a crash between
        # the two leaves an amendment with no plan_versions entry — logged
        # but never applied, which `check` tolerates. A retry of the same
        # amendment completes that one instead of logging a second.
        dangling = [a for a in read_jsonl(d / "amendments.jsonl")
                    if a.get("op") == "amend_plan" and a.get("sha256") == sha
                    and a.get("file") == fname and a.get("from_sha256", tip) == tip]
        if not dangling:
            append_jsonl(d / "amendments.jsonl", {
                "at": now, "op": "amend_plan", "sha256": sha, "file": fname,
                "from_sha256": tip, "from_finding": args.from_finding, "policy": deviation,
            })
        index.setdefault("plan_versions", []).append({
            "sha256": sha, "file": fname, "at": now,
            "from_finding": args.from_finding,
        })
        write_json(d / "index.json", index)
    print(fname)
    return 0


def cmd_set_policy(args):
    """The one supported way to change a sweep's policy: validate, then
    append a new policy version (policy.vN.json + an index.policy_versions
    entry + a set_policy amendment) and make it current (policy.json +
    index.policy_sha256). The --json object is merged onto the current
    policy when policy.json still matches its recorded hash; otherwise the
    current policy can't be trusted and --json must be a complete policy."""
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    try:
        override = json.loads(args.json)
    except json.JSONDecodeError:
        print("invalid --json: not JSON", file=sys.stderr)
        return 2
    if not isinstance(override, dict):
        print("invalid --json: must be an object", file=sys.stderr)
        return 2
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        # A set-policy that crashed after writing policy.json but before
        # index.json is finished first — policy.json already carries it.
        complete_half_applied_policy(d, index)
        current, err = load_policy(d, index)
        if current is not None:
            new_policy = merge_policy(current, override)
        else:
            new_policy = override
        verr = validate_policy(new_policy)
        if verr:
            if current is None:
                print(f"refused: the current policy can't be trusted ({err}), so --json "
                      f"must be a complete policy: {verr}", file=sys.stderr)
            else:
                print(f"invalid policy: {verr}", file=sys.stderr)
            return 2
        text = policy_text(new_policy)
        sha = sha256_of(text)
        if current is not None and sha == index.get("policy_sha256"):
            print(json.dumps({"unchanged": True, "policy": new_policy}))
            return 0

        versions = policy_versions_or_legacy(d, index, trusted=current is not None)
        vn = int(versions[-1].get("version") or len(versions)) + 1
        fname = f"policy.v{vn}.json"
        now = time.time()
        atomic_write_text(d / fname, text)
        # Retry of a set-policy that crashed after logging its amendment:
        # complete THAT amendment rather than logging a second one.
        dangling = [a for a in read_jsonl(d / "amendments.jsonl")
                    if a.get("op") == "set_policy" and a.get("sha256") == sha
                    and a.get("from_sha256") == index.get("policy_sha256")
                    and a.get("file") == fname]
        if dangling:
            vn = dangling[-1].get("version", vn)
        else:
            append_jsonl(d / "amendments.jsonl", {
                "at": now, "op": "set_policy", "version": vn, "file": fname,
                "sha256": sha, "from_sha256": index.get("policy_sha256"),
                "deviation": new_policy["deviation"],
                "from_deviation": current.get("deviation") if current else None,
            })
        atomic_write_text(d / "policy.json", text)
        versions.append({"version": vn, "file": fname, "sha256": sha, "at": now})
        index["policy_versions"] = versions
        index["policy_sha256"] = sha
        write_json(d / "index.json", index)
    print(json.dumps({"version": vn, "policy": new_policy}))
    return 0


def policy_versions_or_legacy(d, index, trusted):
    """index.policy_versions, or — for a sweep created before policy
    versioning — a synthesized v1 entry (preserving policy.json as
    policy.v1.json when it still verifies, else keeping only its hash)."""
    versions = index.get("policy_versions")
    if isinstance(versions, list) and versions:
        return versions
    versions = [{"version": 1, "file": None,
                 "sha256": index.get("policy_sha256"), "at": index.get("created")}]
    if trusted:
        atomic_write_text(d / "policy.v1.json", (d / "policy.json").read_text())
        versions[0]["file"] = "policy.v1.json"
    return versions


def complete_half_applied_policy(d, index):
    """Finish a set-policy that crashed after writing policy.json but before
    index.json: its amendment is logged (from_sha256 == the index's current
    policy_sha256), its version file and policy.json both hash to its
    sha256, but the index never recorded it. Without this, every command
    would refuse the "edited" policy.json forever. Returns True if it
    completed one. Never touches a set-policy that never reached
    policy.json — that one was not applied, and `check` tolerates it."""
    try:
        text = (d / "policy.json").read_text()
    except OSError:
        return False
    sha = sha256_of(text)
    if sha == index.get("policy_sha256"):
        return False
    cands = [a for a in read_jsonl(d / "amendments.jsonl")
             if a.get("op") == "set_policy" and a.get("sha256") == sha
             and a.get("from_sha256") == index.get("policy_sha256")]
    if not cands:
        return False
    a = cands[-1]
    fname = a.get("file")
    try:
        if not fname or sha256_of((d / fname).read_text()) != sha:
            return False
    except OSError:
        return False
    try:
        if validate_policy(json.loads(text)):
            return False
    except ValueError:
        return False
    versions = policy_versions_or_legacy(d, index, trusted=False)
    if versions[0].get("file") is None and (d / "policy.v1.json").exists() \
            and sha256_of((d / "policy.v1.json").read_text()) == versions[0].get("sha256"):
        versions[0]["file"] = "policy.v1.json"
    versions.append({"version": a.get("version") or len(versions) + 1, "file": fname,
                     "sha256": sha, "at": a.get("at")})
    index["policy_versions"] = versions
    index["policy_sha256"] = sha
    write_json(d / "index.json", index)
    return True


def cmd_end_chunk(args):
    """Close this owner's newest open chunk. Any of its items still
    `running` — an item agent that never reported — go back to pending (a
    recovery counted, no attempt spent), so an unreported item can never be
    stranded as `running` on a sweep that otherwise looks finished."""
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    with Lock(d / ".sweep"):
        pace = read_jsonl(d / "pace.jsonl")
        mine = [c for c in open_chunks(pace) if c.get("owner") == owner]
        if not mine:
            print("no open chunk", file=sys.stderr)
            return 2
        ch = max(mine, key=lambda c: c.get("chunk") or 0)
        ids = set(ch.get("items") or [])
        index = read_json(d / "index.json")
        returned = []
        if index is not None:
            for it in index.get("items", []):
                if it.get("id") in ids and it.get("status") == "running":
                    return_to_pending(it)
                    returned.append(it["id"])
            if returned:
                write_json(d / "index.json", index)
        append_jsonl(d / "pace.jsonl", {
            "chunk": ch.get("chunk"), "event": "end", "at": time.time(),
            "usage": usage_snapshot(), "interrupted": False, "returned": returned,
        })
        renew_lease(d, owner, time.time())
    print(json.dumps(returned))
    return 0


def cmd_lease(args):
    """Hold (or release) this session's loop lease. The skill calls
    `lease S --in <delay_s>` right before every ScheduleWakeup and
    `lease S --release` when the loop ends. The stored `until` is the
    given time plus LEASE_MARGIN_S. Exit 4 when another session holds a
    live lease or a fresh open chunk (take it over with recover --takeover)."""
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    with Lock(d / ".sweep"):
        now = time.time()
        for ch in open_chunks(read_jsonl(d / "pace.jsonl")):
            if ch.get("owner") != owner and now - (ch.get("at") or 0) < DEFAULT_STALE_AFTER_S:
                print(f"busy: chunk {ch.get('chunk')} owned by {ch.get('owner')} since "
                      f"{iso(ch.get('at'))} — if that session is dead, take it over with "
                      f"`recover --takeover` (/maestro:sweep takeover {args.slug})",
                      file=sys.stderr)
                return 4
        busy = lease_busy(d, owner, now, args.slug)
        if busy:
            print(busy, file=sys.stderr)
            return 4
        if args.release:
            held = read_lease(d)
            if held and held["owner"] == owner:
                try:
                    (d / "lease.json").unlink()
                except OSError:
                    pass
            print(json.dumps({"released": True}))
            return 0
        base = args.until if args.until is not None else now + args.in_s
        if base != base or base in (float("inf"), float("-inf")) or base < now:
            print("invalid lease time: --until must be a future epoch / --in >= 0",
                  file=sys.stderr)
            return 2
        until = base + LEASE_MARGIN_S
        write_lease(d, owner, until, "wakeup")
    print(json.dumps({"owner": owner, "until": until, "until_iso": iso(until)}))
    return 0


def cmd_recover(args):
    """After a crash: every `running` item -> `pending` (recoveries+1, never
    spending an attempt — `fail()` alone spends attempts), or `failed` past
    MAX_RECOVERIES; every open chunk closes with an `interrupted: true` end
    line. Refuses (exit 4) while another session's open chunk is younger
    than --stale-after, or another session holds a live loop lease — a
    second session must not reclaim items another session still has in
    flight, nor start a loop beside one that is sleeping between chunks —
    unless --takeover says that session is dead. Every foreign chunk or live
    lease reclaimed is logged as a `takeover` amendment."""
    d = sweep_dir(args.slug)
    rc = require_sweep(d, args.slug)
    if rc is not None:
        return rc
    owner = resolve_owner(args)
    if owner is None:
        return no_owner()
    stale_after = args.stale_after
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        # Crash recovery covers a set-policy that died between policy.json
        # and index.json, too (otherwise every command refuses policy.json).
        complete_half_applied_policy(d, index)

        pace = read_jsonl(d / "pace.jsonl")
        chunks = open_chunks(pace)
        now = time.time()
        foreign = [ch for ch in chunks if ch.get("owner") != owner]
        if not args.takeover:
            for ch in foreign:
                if now - (ch.get("at") or 0) < stale_after:
                    print(f"busy: chunk {ch.get('chunk')} owned by {ch.get('owner')} since "
                          f"{iso(ch.get('at'))} — if that session is dead, take it over with "
                          f"`recover --takeover` (/maestro:sweep takeover {args.slug}); it is "
                          f"reclaimed automatically once older than --stale-after "
                          f"({int(stale_after)}s)", file=sys.stderr)
                    return 4
            busy = lease_busy(d, owner, now, args.slug)
            if busy:
                print(busy, file=sys.stderr)
                return 4

        lease = read_lease(d)
        if lease and lease["owner"] != owner and lease["until"] > now:
            # Only reachable with --takeover: log it, and hold the lease
            # ourselves so the old loop's next wakeup is refused.
            append_jsonl(d / "amendments.jsonl", {
                "at": now, "op": "takeover", "chunk": None,
                "from_owner": lease["owner"], "to_owner": owner,
                "reason": "explicit", "lease_until": lease["until"],
            })
            write_lease(d, owner, now + LEASE_MARGIN_S, "takeover")

        for ch in foreign:
            append_jsonl(d / "amendments.jsonl", {
                "at": now, "op": "takeover", "chunk": ch.get("chunk"),
                "from_owner": ch.get("owner"), "to_owner": owner,
                "reason": "explicit" if args.takeover else "stale",
                "age_s": round(now - (ch.get("at") or 0), 1),
            })

        # Past the busy gate, nothing else can legitimately be running: any
        # `running` item is either in a chunk being closed here or orphaned
        # (no open chunk at all — e.g. a crash between `next`'s two writes).
        recovered = []
        for it in index["items"]:
            if it.get("status") == "running":
                return_to_pending(it)
                recovered.append(it["id"])
        write_json(d / "index.json", index)

        for ch in chunks:
            append_jsonl(d / "pace.jsonl", {
                "chunk": ch.get("chunk"), "event": "end", "at": time.time(),
                "usage": usage_snapshot(), "interrupted": True,
            })
    print(json.dumps(recovered))
    return 0


def pace_reasons(pace):
    reasons = []
    seen_starts = set()
    for r in pace:
        if r.get("event") not in ("start", "end"):
            reasons.append(f"pace record has an unknown event: {r.get('event')!r}")
            continue
        c = r.get("chunk")
        if not isinstance(c, int):
            reasons.append(f"pace record has a non-integer chunk: {c!r}")
            continue
        if r["event"] == "start":
            if c in seen_starts:
                reasons.append(f"chunk {c} has two start records")
            if not r.get("items"):
                reasons.append(f"chunk {c} start record has no items")
            seen_starts.add(c)
        elif c not in seen_starts:
            reasons.append(f"chunk {c} end record has no matching start")
    return reasons


def policy_chain_reasons(d, index, amendments):
    """Verify the policy version chain; returns (reasons, deviations) where
    deviations[i] is version i+1's deviation (None when unverifiable)."""
    reasons = []
    versions = index.get("policy_versions")
    set_policy = [a for a in amendments if a.get("op") == "set_policy"]
    if versions is None:
        # Pre-versioning sweep: policy.json is v1 and nothing may have
        # changed it.
        policy = read_json(d / "policy.json")
        dev = policy.get("deviation") if isinstance(policy, dict) else None
        # A set_policy logged but never recorded (crashed) is tolerated.
        return reasons, [dev], set()
    if not isinstance(versions, list) or not versions:
        return ["policy_versions is empty or malformed"], [None], set()

    devs = []
    for i, v in enumerate(versions):
        fname = v.get("file") if isinstance(v, dict) else None
        if fname is None:
            if i != 0:
                reasons.append(f"policy version {i + 1} has no file")
            devs.append(None)
            continue
        try:
            text = (d / fname).read_text()
        except OSError:
            reasons.append(f"policy version file is missing: {fname}")
            devs.append(None)
            continue
        if sha256_of(text) != v.get("sha256"):
            reasons.append(f"policy version {fname} does not match its recorded sha256")
            devs.append(None)
            continue
        try:
            pol = json.loads(text)
        except json.JSONDecodeError:
            pol = None
        err = validate_policy(pol) if pol is not None else "not valid JSON"
        if err:
            reasons.append(f"policy version {fname} is invalid: {err}")
            devs.append(None)
            continue
        devs.append(pol.get("deviation"))

    if versions[-1].get("sha256") != index.get("policy_sha256"):
        reasons.append("policy_sha256 is not the latest policy version's sha256")
    chain = [(versions[i - 1].get("sha256"), versions[i].get("sha256"),
              f"policy version {i + 1}") for i in range(1, len(versions))]
    applied, pair_reasons = pair_amendments(set_policy, chain, "set_policy")
    reasons += pair_reasons
    return reasons, devs, applied


def pair_amendments(amends, chain, what):
    """Pair logged amendments with recorded versions, in order, by (from_sha,
    sha): `chain` is [(from_sha, sha, label)] for each recorded version. An
    amendment that pairs with no version is tolerated only when it was never
    applied — its sha is no recorded version's (a command that crashed after
    logging it, or a retry that chose different content). A second
    amendment for an applied version, or a version with no amendment, fails.
    Returns (ids of the amendments that were applied, reasons)."""
    reasons, applied, k = [], set(), 0
    recorded = {sha for _, sha, _ in chain}
    for a in amends:
        if k < len(chain):
            frm, sha, _ = chain[k]
            if a.get("sha256") == sha and a.get("from_sha256", frm) == frm:
                applied.add(id(a))
                k += 1
                continue
        if a.get("sha256") in recorded:
            reasons.append(f"a {what} amendment for {str(a.get('sha256'))[:12]} is a duplicate "
                           f"or out of order")
    for _, _, label in chain[k:]:
        reasons.append(f"{label} has no matching {what} amendment")
    return applied, reasons


def cmd_check(args):
    """PASS/FAIL like handoff_check.py — see the module docstring for what's checked."""
    d = sweep_dir(args.slug)
    index = read_json(d / "index.json")
    if index is None:
        print("SWEEP FAIL")
        print(f"- no such sweep: {args.slug}")
        return 1
    reasons = []

    try:
        plan_text = (d / "plan.md").read_text()
        if sha256_of(plan_text) != index.get("plan_sha256"):
            reasons.append("plan.md does not match plan_sha256 — plan.md was edited")
    except OSError:
        reasons.append("plan.md is missing")

    try:
        policy_text_now = (d / "policy.json").read_text()
        if sha256_of(policy_text_now) != index.get("policy_sha256"):
            reasons.append("policy.json does not match policy_sha256 — policy.json was edited "
                           "(change a policy only with set-policy)")
        else:
            err = validate_policy(json.loads(policy_text_now)) if policy_text_now.strip() else "empty"
            if err:
                reasons.append(f"policy.json is invalid: {err}")
    except OSError:
        reasons.append("policy.json is missing")
    except json.JSONDecodeError:
        reasons.append("policy.json is not valid JSON")

    for v in index.get("plan_versions", []):
        fname = v.get("file", "")
        try:
            text = (d / fname).read_text()
            if sha256_of(text) != v.get("sha256"):
                reasons.append(f"plan version {fname} does not match its recorded sha256")
        except OSError:
            reasons.append(f"plan version file is missing: {fname}")

    items = index.get("items", [])
    ids = [it.get("id") for it in items]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        reasons.append(f"duplicate item id(s): {dupes}")

    n = len(ids)
    expected_ids = {f"i-{i:04d}" for i in range(1, n + 1)}
    if set(ids) != expected_ids:
        reasons.append(f"item ids are not gap-free i-0001..i-{n:04d}: {sorted(ids)}")

    if index.get("items_sha256") != items_hash(items):
        reasons.append("items_sha256 mismatch — an item was added, removed, or "
                        "relabelled outside add/amend-plan")

    for it in items:
        if it.get("status") not in STATUSES:
            reasons.append(f"invalid status on {it.get('id')}: {it.get('status')!r}")

    try:
        amendments = read_jsonl(d / "amendments.jsonl", strict=True)
    except json.JSONDecodeError:
        amendments = []
        reasons.append("amendments.jsonl has a line that does not parse")
    amendments = [a for a in amendments if isinstance(a, dict)]
    added_ids = {a.get("item") for a in amendments if a.get("op") == "add_item"}
    missing = sorted(added_ids - set(ids), key=str)
    if missing:
        reasons.append(f"item(s) added by an amendment are missing from index.json: {missing}")

    original_count = index.get("original_count", 0)
    extra_ids = set(ids[original_count:])
    unlogged = sorted(extra_ids - added_ids)
    if unlogged:
        reasons.append(f"item(s) beyond the original count have no add_item amendment: {unlogged}")

    # Plan versions <-> amend_plan amendments, paired in order by (from, sha).
    plan_amends = [a for a in amendments if a.get("op") == "amend_plan"]
    plan_versions = index.get("plan_versions", [])
    plan_chain, prev = [], index.get("plan_sha256")
    for v in plan_versions:
        plan_chain.append((prev, v.get("sha256"), f"plan version {v.get('file')}"))
        prev = v.get("sha256")
    _, plan_reasons = pair_amendments(plan_amends, plan_chain, "amend_plan")
    reasons += plan_reasons

    chain_reasons, devs, applied_policy = policy_chain_reasons(d, index, amendments)
    reasons += chain_reasons

    try:
        findings = read_jsonl(d / "findings.jsonl", strict=True)
    except json.JSONDecodeError:
        findings = read_jsonl(d / "findings.jsonl")
        reasons.append("findings.jsonl has a line that does not parse")
    finding_ids = {f.get("id") for f in findings if isinstance(f, dict)}

    # Every amendment: a known op; add_item/amend_plan must cite a real
    # finding and have been allowed by the deviation recorded on it — which
    # must also be the deviation of the policy version in force at the time.
    version_idx = 0
    for i, a in enumerate(amendments, 1):
        op = a.get("op")
        if op not in AMENDMENT_OPS:
            reasons.append(f"amendment {i} has an unknown op: {op!r}")
            continue
        if op == "set_policy":
            if id(a) in applied_policy:   # a never-applied one changed nothing
                version_idx += 1
            continue
        if op not in OP_ALLOWED:
            continue
        if a.get("from_finding") not in finding_ids:
            reasons.append(f"amendment {i} ({op}) cites an unknown finding: "
                           f"{a.get('from_finding')!r}")
        recorded = a.get("policy")
        if recorded not in OP_ALLOWED[op]:
            reasons.append(f"amendment {i} ({op}) was recorded under deviation {recorded!r}, "
                           f"which does not allow it")
        elif version_idx < len(devs) and devs[version_idx] is not None \
                and devs[version_idx] != recorded:
            reasons.append(f"amendment {i} ({op}) records deviation {recorded!r} but policy "
                           f"v{version_idx + 1} in force was {devs[version_idx]!r}")

    try:
        pace = read_jsonl(d / "pace.jsonl", strict=True)
        reasons += pace_reasons(pace)
    except json.JSONDecodeError:
        reasons.append("pace.jsonl has a line that does not parse")

    if reasons:
        print("SWEEP FAIL")
        for r in reasons:
            print(f"- {r}")
        return 1
    print(f"SWEEP PASS — {args.slug}")
    return 0


def cmd_anchor(_args):
    """SessionStart(compact|resume) hook body — silent unless a sweep is active.

    Reads the hook payload on stdin, resolves sweeps from *its* cwd, and never
    raises: this runs on every compaction and resume, and a broken sweep file
    must never take a session down with it.

    The resume hint goes only to a session that may resume: the one owning
    the sweep's open chunk / live loop lease (payload session_id == owner),
    or any session when neither is held. Any other session is told the
    sweep is owned (open chunk) or leased (sleeping loop) by another session
    and until/since when, and how to take it over if that session is dead —
    it must never be handed a line that adopts a live session's sweep.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session = payload.get("session_id") or os.environ.get(SESSION_ENV)
    cwd = payload.get("cwd") or os.getcwd()
    root = sweeps_root(cwd)
    if not root.is_dir():
        return 0

    lines = []
    for p in sorted(root.iterdir()):
        index = read_json(p / "index.json")
        if not isinstance(index, dict):
            continue
        c = counts(index)
        if c["pending"] + c["running"] <= 0:
            continue
        total = len(index.get("items", []))
        progress = f"{c['done']}/{total} done, {c['failed']} failed"
        try:
            pace = read_jsonl(p / "pace.jsonl")
        except Exception:
            pace = []
        foreign = [r for r in open_chunks(pace) if not session or r.get("owner") != session]
        takeover = f"Take over with /maestro:sweep takeover {p.name}"
        lease = read_lease(p)
        if not foreign and lease and lease["until"] > time.time() \
                and (not session or lease["owner"] != session):
            lines.append(
                f"MAESTRO — sweep {p.name} ({progress}) is leased by another session "
                f"({lease['owner']}) until {iso(lease['until'])}: its /loop is sleeping "
                f"between chunks. Do not resume it from this session while that one is "
                f"alive. If that session is dead: {takeover}")
            continue
        if foreign:
            ch = max(foreign, key=lambda r: r.get("chunk") or 0)
            lines.append(
                f"MAESTRO — sweep {p.name} ({progress}) is owned by another session "
                f"({ch.get('owner')}) since {iso(ch.get('at'))}: it holds chunk "
                f"{ch.get('chunk')} open. Do not resume it from this session while that "
                f"one is alive. If that session is dead: {takeover}")
            continue
        resume = f"Resume with /loop /maestro:sweep resume {p.name}"
        lines.append(f"MAESTRO — active sweep {p.name}: {progress}. {resume}")
    if not lines:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        },
        "suppressOutput": True,
    }))
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="sweep_state.py")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("new")
    sp.add_argument("--slug", required=True)
    sp.add_argument("--plan", required=True)
    sp.add_argument("--items", required=True)
    sp.add_argument("--policy")

    sp = sub.add_parser("status")
    sp.add_argument("slug")
    sp.add_argument("--json", action="store_true")

    sub.add_parser("list")

    sp = sub.add_parser("next")
    sp.add_argument("slug")
    sp.add_argument("--n", type=int, default=None)
    sp.add_argument("--owner")

    sp = sub.add_parser("done")
    sp.add_argument("slug")
    sp.add_argument("item")
    sp.add_argument("--note")
    sp.add_argument("--owner")

    sp = sub.add_parser("fail")
    sp.add_argument("slug")
    sp.add_argument("item")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--owner")

    sp = sub.add_parser("finding")
    sp.add_argument("slug")
    sp.add_argument("--text", required=True)
    sp.add_argument("--item")

    sp = sub.add_parser("add")
    sp.add_argument("slug")
    sp.add_argument("--label", required=True)
    sp.add_argument("--from-finding", dest="from_finding", required=True)

    sp = sub.add_parser("amend-plan")
    sp.add_argument("slug")
    sp.add_argument("--file", required=True)
    sp.add_argument("--from-finding", dest="from_finding", required=True)

    sp = sub.add_parser("set-policy")
    sp.add_argument("slug")
    sp.add_argument("--json", required=True)

    sp = sub.add_parser("end-chunk")
    sp.add_argument("slug")
    sp.add_argument("--owner")

    sp = sub.add_parser("recover")
    sp.add_argument("slug")
    sp.add_argument("--owner")
    sp.add_argument("--stale-after", dest="stale_after", type=float,
                    default=DEFAULT_STALE_AFTER_S)
    sp.add_argument("--takeover", action="store_true")

    sp = sub.add_parser("lease")
    sp.add_argument("slug")
    sp.add_argument("--owner")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--until", type=float, default=None)
    g.add_argument("--in", dest="in_s", type=float, default=None)
    g.add_argument("--release", action="store_true")

    sp = sub.add_parser("check")
    sp.add_argument("slug")

    sub.add_parser("anchor")

    return p


COMMANDS = {
    "new": cmd_new, "status": cmd_status, "list": cmd_list, "next": cmd_next,
    "done": cmd_done, "fail": cmd_fail, "finding": cmd_finding, "add": cmd_add,
    "amend-plan": cmd_amend_plan, "set-policy": cmd_set_policy,
    "end-chunk": cmd_end_chunk, "recover": cmd_recover, "lease": cmd_lease,
    "check": cmd_check,
    "anchor": cmd_anchor,
}


def main():
    args = build_parser().parse_args()
    if args.cmd is None:
        build_parser().print_help()
        return 2
    fn = COMMANDS[args.cmd]
    if args.cmd == "anchor":
        # The SessionStart hook body: never let an internal error surface.
        try:
            return fn(args) or 0
        except Exception as e:
            if DEBUG:
                print(f"sweep_state anchor: {e}", file=sys.stderr)
            return 0
    try:
        return fn(args)
    except LockTimeout as e:
        print(str(e), file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
