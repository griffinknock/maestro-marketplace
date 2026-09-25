#!/usr/bin/env python3
"""Maestro lessons — capture side of the cross-session lesson pipeline.

A "lesson" is a small, human-approved rule about how Maestro orchestrates.
This module owns the CAPTURE queue: candidates fed automatically by
re-orchestration findings (`reorchestrate.py`) and manually by the conductor
when Griffin corrects it (`flag`). It never decides a candidate is a lesson —
that is the validator (`lessons_check.py`) and a later inject/accept/publish
wave, both out of scope here. Keep every function usable standalone so those
waves can import this module without shelling out to its own CLI.

Store layout ($MAESTRO_LESSONS_DIR, else ~/.claude/maestro/lessons/):
  candidates.jsonl   append-only, gitignored, never committed. One creation
                     record per candidate plus later status-change records;
                     folding takes the latest status ("pending" by default).
  rejected.jsonl     append-only, tracked in the store's own git repo. A rule
                     Griffin explicitly rejected, keyed so a later review never
                     re-proposes it.
  .gitignore         written by `init`; contains "candidates.jsonl".

Candidate record (creation):
  {"id": "c-<8 hex>", "at": <epoch float>, "session": "<session id>",
   "repo": "<basename of git toplevel of cwd, or null>",
   "source": "finding"|"correction", "fingerprint": "<finding fingerprint
   or null>", "tool_use_id": "<id or null>", "text": "<one line, <=300 chars>"}

Candidate record (status change, appended later, same "id"):
  {"id": "c-...", "status": "reviewed"|"not-a-lesson"|"accepted", "at": ...}

Rejected record:
  {"key": "<key>", "rule": "<drafted rule text>", "at": <epoch float>}

CLI:
  lessons.py init
  lessons.py flag --session SID "<one line>"
  lessons.py candidates [--pending] [--json]
  lessons.py mark ID --status reviewed|not-a-lesson|accepted
  lessons.py reject --key KEY --rule "<drafted rule text>"
  lessons.py rejected [--json]

Env:
  MAESTRO_LESSONS_DIR   overrides the store directory
"""
import argparse
import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

STATUSES = ("reviewed", "not-a-lesson", "accepted")
CANDIDATES_FILE = "candidates.jsonl"
REJECTED_FILE = "rejected.jsonl"

# Repo-name lookups are one `git rev-parse` per distinct cwd. Cheap on its
# own, but capture_finding can run several times per process (tests, or a
# conductor tool call that surfaces more than one fresh finding at once), so
# cache it rather than re-shell out for the same cwd repeatedly.
_REPO_CACHE = {}


def store_dir():
    """The lessons store directory. Never created here — callers do that."""
    override = os.environ.get("MAESTRO_LESSONS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "maestro" / "lessons"


def repo_name(cwd):
    """Basename of the git toplevel of `cwd`, or None outside a repo."""
    cwd = cwd or os.getcwd()
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]
    name = None
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            top = r.stdout.strip()
            if top:
                name = Path(top).name
    except (OSError, subprocess.SubprocessError):
        name = None
    _REPO_CACHE[cwd] = name
    return name


def init(d=None):
    """mkdir -p the store, `git init` it if it is not already a repo, and
    write a `.gitignore` that keeps `candidates.jsonl` out of it. Idempotent.
    """
    d = Path(d) if d else store_dir()
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").is_dir():
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(d),
                           capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    gi = d / ".gitignore"
    try:
        cur = gi.read_text() if gi.is_file() else ""
        if CANDIDATES_FILE not in cur.splitlines():
            with open(gi, "a") as f:
                if cur and not cur.endswith("\n"):
                    f.write("\n")
                f.write(f"{CANDIDATES_FILE}\n")
    except OSError:
        pass
    return d


def _append_line(d, filename, obj):
    """Append one JSON line under a file lock (mkdir'd store assumed)."""
    lock = d / ".lock"
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(d / filename, "a") as f:
                f.write(json.dumps(obj) + "\n")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _one_line(text, limit=300):
    return (text or "").strip().replace("\n", " ").replace("\r", " ")[:limit]


def _new_candidate(d, session_id, cwd, source, fingerprint, tool_use_id, text):
    cid = f"c-{secrets.token_hex(4)}"
    rec = {
        "id": cid,
        "at": time.time(),
        "session": session_id,
        "repo": repo_name(cwd),
        "source": source,
        "fingerprint": fingerprint,
        "tool_use_id": tool_use_id,
        "text": _one_line(text),
    }
    _append_line(d, CANDIDATES_FILE, rec)
    return cid


def _has_duplicate(d, session_id, fingerprint):
    """A creation record already exists for this session + fingerprint."""
    if fingerprint is None:
        return False
    path = d / CANDIDATES_FILE
    if not path.is_file():
        return False
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if ("text" in rec and rec.get("session") == session_id
                        and rec.get("fingerprint") == fingerprint):
                    return True
    except OSError:
        return False
    return False


def capture_finding(session_id, cwd, fingerprint, text, tool_use_id=None):
    """Append a re-orchestration finding as a candidate lesson.

    Skips silently if a candidate with the same session + fingerprint already
    exists. Never raises — a failure here (an unwritable store, a locked
    file, anything) must never change what the caller prints or does.
    """
    try:
        d = store_dir()
        d.mkdir(parents=True, exist_ok=True)
        if _has_duplicate(d, session_id, fingerprint):
            return None
        return _new_candidate(d, session_id, cwd, "finding", fingerprint,
                              tool_use_id, text)
    except Exception:
        return None


def flag(session_id, text, cwd=None):
    """Record a correction Griffin made to how Maestro orchestrated.

    Source is always "correction"; these are never deduped by fingerprint —
    each one is its own manually-authored event.
    """
    try:
        d = store_dir()
        d.mkdir(parents=True, exist_ok=True)
        return _new_candidate(d, session_id, cwd or os.getcwd(), "correction",
                              None, None, text)
    except Exception:
        return None


def load_candidates(d=None):
    """Every candidate, folded to its latest status ("pending" if none)."""
    d = Path(d) if d else store_dir()
    path = d / CANDIDATES_FILE
    if not path.is_file():
        return []
    creations, order, statuses = {}, [], {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cid = rec.get("id")
                if not cid:
                    continue
                if "text" in rec:
                    if cid not in creations:
                        order.append(cid)
                    creations[cid] = rec
                elif "status" in rec:
                    statuses[cid] = rec["status"]   # later line wins
    except OSError:
        return []
    out = []
    for cid in order:
        rec = dict(creations[cid])
        rec["status"] = statuses.get(cid, "pending")
        out.append(rec)
    return out


def mark(cid, status, d=None):
    """Append a status-change record for candidate `cid`."""
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r} (want one of {STATUSES})")
    d = Path(d) if d else store_dir()
    d.mkdir(parents=True, exist_ok=True)
    _append_line(d, CANDIDATES_FILE, {"id": cid, "status": status, "at": time.time()})


def reject(key, rule, d=None):
    """Record a permanently-rejected rule and commit it in the store repo."""
    d = Path(d) if d else store_dir()
    init(d)
    rec = {"key": key, "rule": rule, "at": time.time()}
    _append_line(d, REJECTED_FILE, rec)
    try:
        subprocess.run(["git", "-C", str(d), "add", REJECTED_FILE],
                       capture_output=True, check=False)
        subprocess.run(
            ["git", "-C", str(d),
             "-c", "user.email=maestro@localhost", "-c", "user.name=maestro",
             "commit", "-q", "-m", f"reject: {key}"],
            capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    return rec


def load_rejected(d=None):
    d = Path(d) if d else store_dir()
    path = d / REJECTED_FILE
    if not path.is_file():
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


# --- CLI --------------------------------------------------------------
def _print_candidate_row(rec):
    print(f"{rec.get('id')}\t{rec.get('source')}\t{rec.get('fingerprint')}\t"
          f"{rec.get('repo')}\t{rec.get('status')}\t{rec.get('text')}")


def _cmd_init(args):
    d = init()
    print(f"lessons store ready at {d}")
    return 0


def _cmd_flag(args):
    text = " ".join(args.text) if isinstance(args.text, list) else args.text
    cid = flag(args.session, text)
    if cid is None:
        print("failed to record correction", file=sys.stderr)
        return 1
    print(cid)
    return 0


def _cmd_candidates(args):
    rows = load_candidates()
    if args.pending:
        rows = [r for r in rows if r.get("status") == "pending"]
    if args.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            _print_candidate_row(r)
    return 0


def _cmd_mark(args):
    known = {r["id"] for r in load_candidates()}
    if args.id not in known:
        print(f"unknown candidate: {args.id}", file=sys.stderr)
        return 1
    try:
        mark(args.id, args.status)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{args.id} -> {args.status}")
    return 0


def _cmd_reject(args):
    reject(args.key, args.rule)
    print(f"rejected: {args.key}")
    return 0


def _cmd_rejected(args):
    rows = load_rejected()
    if args.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            print(f"{r.get('key')}\t{r.get('rule')}")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="lessons.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=_cmd_init)

    p = sub.add_parser("flag")
    p.add_argument("--session", required=True)
    p.add_argument("text", nargs="+")
    p.set_defaults(func=_cmd_flag)

    p = sub.add_parser("candidates")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_candidates)

    p = sub.add_parser("mark")
    p.add_argument("id")
    p.add_argument("--status", required=True, choices=STATUSES)
    p.set_defaults(func=_cmd_mark)

    p = sub.add_parser("reject")
    p.add_argument("--key", required=True)
    p.add_argument("--rule", required=True)
    p.set_defaults(func=_cmd_reject)

    p = sub.add_parser("rejected")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_rejected)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
