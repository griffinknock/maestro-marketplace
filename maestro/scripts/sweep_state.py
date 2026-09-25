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
                      original_count, plan_versions, items[]
  policy.json        ceilings, deviation, chunk_size, margin_s, max_attempts,
                      allow_blind, blind_gap_s
  findings.jsonl     append-only — one line per `finding`
  amendments.jsonl   append-only — one line per `add`/`amend-plan`
  pace.jsonl         two lines per chunk: start (carries "owner"), then end

`pace.py` (built in parallel) reads these files; the schemas here are the
contract with it — see each write site below.

Every write is atomic (tmp file + os.replace, tmp always cleaned up) and
every command that mutates a sweep's files takes a `.sweep.lock` directory
lock, matching the proper-lockfile-compatible pattern in `ledger.py`. On
sustained contention the lock now raises rather than silently proceeding
unlocked — see `Lock`/`LockTimeout`.

Usage: see `build_parser()`. `anchor` is special — it is the SessionStart
hook body, reads a hook payload on stdin, and never raises or exits nonzero.
"""
import argparse
import hashlib
import json
import os
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
LOCK_STALE = float(os.environ.get("MAESTRO_LOCK_STALE_S", "10.0"))
LOCK_TIMEOUT_S = float(os.environ.get("MAESTRO_LOCK_TIMEOUT_S", "4.0"))
MAX_RECOVERIES = 3
USAGE_FILE_DEFAULT = str(Path.home() / ".claude" / "maestro" / "usage.json")


# ── locking + atomic writes ──────────────────────────────────────────────

class LockTimeout(Exception):
    """Raised when a `.sweep.lock` could not be acquired before the
    contention timeout. Callers must never proceed unlocked — `main()`
    catches this once, for every command, and exits 5."""


class Lock:
    """mkdir-to-acquire / rmdir-to-release — the same pattern as ledger.py's
    `_Lock`, so a sweep directory and a ledger session dir can never deadlock
    each other by disagreeing on a convention.

    On sustained contention (`LOCK_TIMEOUT_S`, default 4s) this raises
    `LockTimeout` instead of returning unlocked — a prior version fell
    through silently after its retry budget ran out, letting two callers
    mutate the same sweep at once.
    """

    def __init__(self, target):
        self.p = Path(str(target) + ".lock")
        self.held = False

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT_S
        while time.time() < deadline:
            try:
                self.p.mkdir()
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > LOCK_STALE:
                        self.p.rmdir()
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
            except OSError:
                break
        raise LockTimeout(f"refused: could not acquire lock (busy): {self.p}")

    def __exit__(self, *_):
        if self.held:
            self.held = False       # released once; re-entry must not rmdir twice
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


# ── usage (written by statusline.py) ─────────────────────────────────────

def usage_snapshot():
    """U, as defined in the pace.jsonl contract — read fresh, every call.

    usage.json (written by statusline.py) now carries a `captured_at` per
    window rather than one top-level timestamp (C1); this keeps the returned
    shape — the contract pace.py reads out of pace.jsonl — unchanged by
    folding the two window timestamps down to their max.
    """
    path = Path(os.environ.get("MAESTRO_USAGE_FILE") or USAGE_FILE_DEFAULT)
    d = read_json(path, {}) or {}
    five = d.get("five_hour") if isinstance(d.get("five_hour"), dict) else {}
    seven = d.get("seven_day") if isinstance(d.get("seven_day"), dict) else {}
    captured = [w.get("captured_at") for w in (five, seven)
                if isinstance(w, dict) and w.get("captured_at") is not None]
    return {
        "five_hour": five.get("used_percentage"),
        "seven_day": seven.get("used_percentage"),
        "captured_at": max(captured) if captured else d.get("captured_at"),
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


def load_policy(d):
    """Read + validate this sweep's policy.json. Returns (policy, None) on
    success or (None, reason) on failure. Missing/corrupt/invalid policy.json
    is always an error here — an existing sweep never silently falls back to
    DEFAULT_POLICY (a deleted or edited policy.json must not quietly turn a
    locked sweep additive)."""
    path = d / "policy.json"
    try:
        text = path.read_text()
    except OSError:
        return None, "policy.json is missing"
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
        atomic_write_text(d / "plan.md", plan_text)
        write_json(d / "policy.json", policy)
        policy_sha256 = sha256_of((d / "policy.json").read_text())
        now = time.time()
        items = [{"id": f"i-{i + 1:04d}", "label": lbl, "status": "pending",
                  "attempts": 0, "recoveries": 0, "from_finding": None, "note": None}
                 for i, lbl in enumerate(labels)]
        index = {"slug": args.slug, "created": now,
                 "plan_sha256": sha256_of(plan_text),
                 "policy_sha256": policy_sha256,
                 "items_sha256": items_hash(items),
                 "original_count": len(items),
                 "plan_versions": [], "items": items}
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
    owner = args.owner or "unknown"
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        n = args.n if args.n is not None else policy.get("chunk_size", DEFAULT_POLICY["chunk_size"])
        claimed = []
        for it in index["items"]:
            if len(claimed) >= n:
                break
            if it.get("status") == "pending":
                it["status"] = "running"
                claimed.append(it)
        if not claimed:
            # Nothing pending: write nothing (an empty start record here
            # would later make `check` fail forever on a finished sweep).
            print(json.dumps([]))
            return 3
        write_json(d / "index.json", index)
        pace = read_jsonl(d / "pace.jsonl")
        chunk_no = max([r.get("chunk", 0) for r in pace if r.get("event") == "start"],
                       default=0) + 1
        append_jsonl(d / "pace.jsonl", {
            "chunk": chunk_no, "event": "start", "at": time.time(),
            "items": [it["id"] for it in claimed], "usage": usage_snapshot(),
            "owner": owner,
        })
    print(json.dumps([{"id": it["id"], "label": it["label"]} for it in claimed]))
    return 0


def cmd_done(args):
    d = sweep_dir(args.slug)
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        it = find_item(index, args.item)
        if it is None:
            print(f"no such item: {args.item}", file=sys.stderr)
            return 2
        it["status"] = "done"
        if args.note:
            it["note"] = args.note
        write_json(d / "index.json", index)
    return 0


def cmd_fail(args):
    d = sweep_dir(args.slug)
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        it = find_item(index, args.item)
        if it is None:
            print(f"no such item: {args.item}", file=sys.stderr)
            return 2
        policy, err = load_policy(d)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        max_attempts = policy.get("max_attempts", DEFAULT_POLICY["max_attempts"])
        it["attempts"] = int(it.get("attempts") or 0) + 1
        it["note"] = args.reason
        it["status"] = "pending" if it["attempts"] < max_attempts else "failed"
        write_json(d / "index.json", index)
        status = it["status"]
    print(status)
    return 0


def cmd_finding(args):
    d = sweep_dir(args.slug)
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
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        deviation = policy.get("deviation")
        if deviation == "locked":
            print("refused: deviation policy is locked", file=sys.stderr)
            return 2
        findings = read_jsonl(d / "findings.jsonl")
        if not any(f.get("id") == args.from_finding for f in findings):
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
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy, err = load_policy(d)
        if err:
            print(f"refused: {err}", file=sys.stderr)
            return 2
        deviation = policy.get("deviation")
        if deviation not in ("adaptive", "autonomous"):
            print(f"refused: amend-plan requires adaptive or autonomous deviation "
                  f"(current: {deviation})", file=sys.stderr)
            return 2
        try:
            new_text = Path(args.file).read_text()
        except OSError as e:
            print(f"cannot read --file: {e}", file=sys.stderr)
            return 2
        sha = sha256_of(new_text)
        # plan.md is implicitly v1; the first amendment is v2.
        vn = len(index.get("plan_versions", [])) + 2
        fname = f"plan.v{vn}.md"
        atomic_write_text(d / fname, new_text)   # plan.md itself is never touched
        index.setdefault("plan_versions", []).append({
            "sha256": sha, "file": fname, "at": time.time(),
            "from_finding": args.from_finding,
        })
        write_json(d / "index.json", index)
        append_jsonl(d / "amendments.jsonl", {
            "at": time.time(), "op": "amend_plan", "sha256": sha,
            "from_finding": args.from_finding, "policy": deviation,
        })
    print(fname)
    return 0


def cmd_end_chunk(args):
    d = sweep_dir(args.slug)
    owner = args.owner or "unknown"
    with Lock(d / ".sweep"):
        pace = read_jsonl(d / "pace.jsonl")
        mine = [c for c in open_chunks(pace) if (c.get("owner") or "unknown") == owner]
        if not mine:
            print("no open chunk", file=sys.stderr)
            return 2
        chunk = max(c.get("chunk") for c in mine)
        append_jsonl(d / "pace.jsonl", {
            "chunk": chunk, "event": "end", "at": time.time(),
            "usage": usage_snapshot(), "interrupted": False,
        })
    return 0


def cmd_recover(args):
    """After a crash: `running` -> `pending` (recoveries+1, never spending an
    attempt — `fail()` alone spends attempts), or `failed` past
    MAX_RECOVERIES. Any chunk still open closes with an `interrupted: true`
    end line — unless it's owned by someone else and still fresh (C3): a
    second session's recover must not reclaim items another session's chunk
    still has in flight."""
    d = sweep_dir(args.slug)
    owner = args.owner or "unknown"
    stale_after = args.stale_after
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2

        pace = read_jsonl(d / "pace.jsonl")
        chunks = open_chunks(pace)
        now = time.time()
        for ch in chunks:
            ch_owner = ch.get("owner") or "unknown"
            if ch_owner == owner:
                continue
            age = now - (ch.get("at") or 0)
            if age < stale_after:
                ts = datetime.fromtimestamp(ch.get("at") or 0, tz=timezone.utc).isoformat()
                print(f"busy: chunk {ch.get('chunk')} owned by {ch_owner} since {ts}",
                      file=sys.stderr)
                return 4

        reclaim_ids = set()
        for ch in chunks:
            reclaim_ids.update(ch.get("items") or [])

        recovered = []
        for it in index["items"]:
            if it.get("status") == "running" and it.get("id") in reclaim_ids:
                it["recoveries"] = int(it.get("recoveries") or 0) + 1
                if it["recoveries"] >= MAX_RECOVERIES:
                    it["status"] = "failed"
                    it["note"] = f"failed after {it['recoveries']} recoveries"
                else:
                    it["status"] = "pending"
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
        policy_text = (d / "policy.json").read_text()
        if sha256_of(policy_text) != index.get("policy_sha256"):
            reasons.append("policy.json does not match policy_sha256 — policy.json was edited")
        else:
            err = validate_policy(json.loads(policy_text)) if policy_text.strip() else "empty"
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
    added_ids = {a.get("item") for a in amendments if a.get("op") == "add_item"}
    missing = sorted(added_ids - set(ids))
    if missing:
        reasons.append(f"item(s) added by an amendment are missing from index.json: {missing}")

    original_count = index.get("original_count", 0)
    extra_ids = set(ids[original_count:])
    unlogged = sorted(extra_ids - added_ids)
    if unlogged:
        reasons.append(f"item(s) beyond the original count have no add_item amendment: {unlogged}")

    for fname in ("findings.jsonl",):
        try:
            read_jsonl(d / fname, strict=True)
        except json.JSONDecodeError:
            reasons.append(f"{fname} has a line that does not parse")

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
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()
    root = sweeps_root(cwd)
    if not root.is_dir():
        return 0

    lines = []
    for p in sorted(root.iterdir()):
        index = read_json(p / "index.json")
        if not index:
            continue
        c = counts(index)
        if c["pending"] + c["running"] <= 0:
            continue
        total = len(index.get("items", []))
        owner = None
        try:
            pace = read_jsonl(p / "pace.jsonl")
        except Exception:
            pace = []
        starts = [r for r in pace if isinstance(r, dict) and r.get("event") == "start"]
        if starts:
            latest = max(starts, key=lambda r: r.get("chunk", 0))
            owner = latest.get("owner")
        if owner:
            resume = (f"Resume with /loop /maestro:sweep resume {p.name} "
                       f"--owner {owner}")
        else:
            resume = f"Resume with /loop /maestro:sweep resume {p.name}"
        lines.append(
            f"MAESTRO — active sweep {p.name}: {c['done']}/{total} done, "
            f"{c['failed']} failed. {resume}")
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

    sp = sub.add_parser("fail")
    sp.add_argument("slug")
    sp.add_argument("item")
    sp.add_argument("--reason", required=True)

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

    sp = sub.add_parser("end-chunk")
    sp.add_argument("slug")
    sp.add_argument("--owner")

    sp = sub.add_parser("recover")
    sp.add_argument("slug")
    sp.add_argument("--owner")
    sp.add_argument("--stale-after", dest="stale_after", type=float, default=7200.0)

    sp = sub.add_parser("check")
    sp.add_argument("slug")

    sub.add_parser("anchor")

    return p


COMMANDS = {
    "new": cmd_new, "status": cmd_status, "list": cmd_list, "next": cmd_next,
    "done": cmd_done, "fail": cmd_fail, "finding": cmd_finding, "add": cmd_add,
    "amend-plan": cmd_amend_plan, "end-chunk": cmd_end_chunk,
    "recover": cmd_recover, "check": cmd_check, "anchor": cmd_anchor,
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
