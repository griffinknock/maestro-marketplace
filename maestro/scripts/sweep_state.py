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
  index.json         slug, created, plan_sha256, plan_versions, items[]
  policy.json        ceilings, deviation, chunk_size, margin_s, max_attempts
  findings.jsonl     append-only — one line per `finding`
  amendments.jsonl   append-only — one line per `add`/`amend-plan`
  pace.jsonl         two lines per chunk: start, then end

`pace.py` (built in parallel) reads these files; the schemas here are the
contract with it — see each write site below.

Every write is atomic (tmp file + os.replace, tmp always cleaned up) and
every command that mutates a sweep's files takes a `.sweep.lock` directory
lock, matching the proper-lockfile-compatible pattern in `ledger.py`.

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
from pathlib import Path

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"

DEFAULT_POLICY = {
    "ceilings": {"five_hour": 80, "seven_day": 90},
    "deviation": "additive",
    "chunk_size": 5,
    "margin_s": 120,
    "max_attempts": 2,
}
STATUSES = ("pending", "running", "done", "failed")
LOCK_STALE = 10.0
USAGE_FILE_DEFAULT = str(Path.home() / ".claude" / "maestro" / "usage.json")


# ── locking + atomic writes ──────────────────────────────────────────────

class Lock:
    """mkdir-to-acquire / rmdir-to-release — the same pattern as ledger.py's
    `_Lock`, so a sweep directory and a ledger session dir can never deadlock
    each other by disagreeing on a convention."""

    def __init__(self, target):
        self.p = Path(str(target) + ".lock")
        self.held = False

    def __enter__(self):
        for _ in range(200):
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
        return self

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
    """U, as defined in the pace.jsonl contract — read fresh, every call."""
    path = Path(os.environ.get("MAESTRO_USAGE_FILE") or USAGE_FILE_DEFAULT)
    d = read_json(path, {}) or {}
    five = d.get("five_hour") if isinstance(d.get("five_hour"), dict) else {}
    seven = d.get("seven_day") if isinstance(d.get("seven_day"), dict) else {}
    return {
        "five_hour": five.get("used_percentage"),
        "seven_day": seven.get("used_percentage"),
        "captured_at": d.get("captured_at"),
        "resets": {
            "five_hour": five.get("resets_at"),
            "seven_day": seven.get("resets_at"),
        },
    }


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


def last_open_chunk(pace):
    """The newest chunk with a `start` and no matching `end`, or None."""
    open_chunks = {}
    for r in pace:
        c = r.get("chunk")
        if r.get("event") == "start":
            open_chunks[c] = True
        elif r.get("event") == "end":
            open_chunks[c] = False
    live = [c for c, is_open in open_chunks.items() if is_open]
    return max(live) if live else None


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
        policy = merge_policy(policy, override)

    d.mkdir(parents=True, exist_ok=True)
    with Lock(d / ".sweep"):
        atomic_write_text(d / "plan.md", plan_text)
        now = time.time()
        items = [{"id": f"i-{i + 1:04d}", "label": lbl, "status": "pending",
                  "attempts": 0, "from_finding": None, "note": None}
                 for i, lbl in enumerate(labels)]
        index = {"slug": args.slug, "created": now,
                 "plan_sha256": sha256_of(plan_text),
                 "plan_versions": [], "items": items}
        write_json(d / "index.json", index)
        write_json(d / "policy.json", policy)
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
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy = read_json(d / "policy.json", DEFAULT_POLICY)
        n = args.n if args.n is not None else policy.get("chunk_size", DEFAULT_POLICY["chunk_size"])
        claimed = []
        for it in index["items"]:
            if len(claimed) >= n:
                break
            if it.get("status") == "pending":
                it["status"] = "running"
                claimed.append(it)
        write_json(d / "index.json", index)
        pace = read_jsonl(d / "pace.jsonl")
        chunk_no = max([r.get("chunk", 0) for r in pace if r.get("event") == "start"],
                       default=0) + 1
        append_jsonl(d / "pace.jsonl", {
            "chunk": chunk_no, "event": "start", "at": time.time(),
            "items": [it["id"] for it in claimed], "usage": usage_snapshot(),
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
        policy = read_json(d / "policy.json", DEFAULT_POLICY)
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
        policy = read_json(d / "policy.json", DEFAULT_POLICY)
        deviation = policy.get("deviation")
        if deviation == "locked":
            print("refused: deviation policy is locked", file=sys.stderr)
            return 2
        new_id = next_item_id(index)
        index["items"].append({
            "id": new_id, "label": args.label, "status": "pending",
            "attempts": 0, "from_finding": args.from_finding, "note": None,
        })
        write_json(d / "index.json", index)
        append_jsonl(d / "amendments.jsonl", {
            "at": time.time(), "op": "add_item", "item": new_id,
            "from_finding": args.from_finding, "policy": deviation,
        })
    print(new_id)
    return 0


def cmd_amend_plan(args):
    d = sweep_dir(args.slug)
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy = read_json(d / "policy.json", DEFAULT_POLICY)
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
    with Lock(d / ".sweep"):
        pace = read_jsonl(d / "pace.jsonl")
        chunk = last_open_chunk(pace)
        if chunk is None:
            print("no open chunk", file=sys.stderr)
            return 2
        append_jsonl(d / "pace.jsonl", {
            "chunk": chunk, "event": "end", "at": time.time(),
            "usage": usage_snapshot(), "interrupted": False,
        })
    return 0


def cmd_recover(args):
    """After a crash: `running` -> `pending` (attempts+1, or `failed` past the
    ceiling), and close any open chunk with an `interrupted: true` end line."""
    d = sweep_dir(args.slug)
    with Lock(d / ".sweep"):
        index = read_json(d / "index.json")
        if index is None:
            print(f"no such sweep: {args.slug}", file=sys.stderr)
            return 2
        policy = read_json(d / "policy.json", DEFAULT_POLICY)
        max_attempts = policy.get("max_attempts", DEFAULT_POLICY["max_attempts"])
        recovered = []
        for it in index["items"]:
            if it.get("status") == "running":
                it["attempts"] = int(it.get("attempts") or 0) + 1
                it["status"] = "pending" if it["attempts"] < max_attempts else "failed"
                recovered.append(it["id"])
        write_json(d / "index.json", index)
        pace = read_jsonl(d / "pace.jsonl")
        chunk = last_open_chunk(pace)
        if chunk is not None:
            append_jsonl(d / "pace.jsonl", {
                "chunk": chunk, "event": "end", "at": time.time(),
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

    for v in index.get("plan_versions", []):
        fname = v.get("file", "")
        try:
            text = (d / fname).read_text()
            if sha256_of(text) != v.get("sha256"):
                reasons.append(f"plan version {fname} does not match its recorded sha256")
        except OSError:
            reasons.append(f"plan version file is missing: {fname}")

    ids = [it.get("id") for it in index.get("items", [])]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        reasons.append(f"duplicate item id(s): {dupes}")

    for it in index.get("items", []):
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
        lines.append(
            f"MAESTRO — active sweep {p.name}: {c['done']}/{total} done, "
            f"{c['failed']} failed. Resume with /loop /maestro:sweep resume {p.name}")
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

    sp = sub.add_parser("recover")
    sp.add_argument("slug")

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
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
