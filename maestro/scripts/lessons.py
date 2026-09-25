#!/usr/bin/env python3
"""Maestro lessons — the cross-session lesson pipeline.

A "lesson" is a small, human-approved rule about how Maestro orchestrates.
This module owns the whole pipeline except the pure parse/validate/format
logic, which lives in `lessons_check.py` and is reused here by its public
API (`parse`, `active_for`, `render_injection`, `check`):

  - CAPTURE: candidates fed automatically by re-orchestration findings
    (`reorchestrate.py`) and manually by the conductor when Griffin corrects
    it (`flag`). Capture never decides a candidate is a lesson.
  - INJECT: the SessionStart hook that puts active lessons, a check-failure
    warning, and a pending-candidate count in front of the conductor.
  - ACCEPT: turns a candidate into an active personal-store lesson (`L-NNN`),
    gated by `lessons_check.check()` so a bad entry never lands.
  - PUBLISH: copies an accepted, repo-scoped lesson into that repo's own
    `.claude/maestro-lessons.md` (`R-NNN`), uncommitted — Griffin commits it.
  - STATUS: budget and queue counters for `/maestro:lessons`.

Every function here is usable standalone, so a hook or a command markdown
file can import this module without shelling out to its own CLI.

Store layout ($MAESTRO_LESSONS_DIR, else ~/.claude/maestro/lessons/):
  lessons.md         the personal lesson file, git-tracked, append-only.
                     Parsed and validated by `lessons_check.py`.
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
  lessons.py inject                     (SessionStart hook; reads stdin)
  lessons.py accept --rule R --why W --evidence E [--scope global|repo:<name>]
                    [--supersedes ID] [--candidates c-..,c-..]
  lessons.py publish ID
  lessons.py status [--json]

Env:
  MAESTRO_LESSONS_DIR   overrides the store directory
  MAESTRO_LESSONS=0     disable capture and injection (tests/replay.py sets
                         this so a replay never touches the real store)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lessons_check

STATUSES = ("reviewed", "not-a-lesson", "accepted")
CANDIDATES_FILE = "candidates.jsonl"
REJECTED_FILE = "rejected.jsonl"
LESSONS_FILE = "lessons.md"
REPO_LESSONS_RELPATH = Path(".claude") / "maestro-lessons.md"
LESSONS_ON = os.environ.get("MAESTRO_LESSONS", "1") != "0"

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


# --- shared file helpers -----------------------------------------------

def _read_text_or_empty(path):
    if path is None:
        return ""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _next_id(path, prefix):
    """The next `<prefix>NNN` id, one past the highest existing number for
    that prefix in the file at `path` (or `<prefix>001` if none/missing)."""
    entries = lessons_check.parse(_read_text_or_empty(path))
    nums = []
    for e in entries:
        eid = e.get("id") or ""
        if eid.startswith(prefix):
            digits = eid[len(prefix):]
            if digits.isdigit():
                nums.append(int(digits))
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n:03d}"


def _append_entry_text(existing_text, block):
    """`existing_text` (possibly empty) plus `block` (one rendered entry,
    trailing-newline-terminated), separated by exactly one blank line, with
    a header written for a brand-new file."""
    if not existing_text:
        return "# Maestro lessons\n\n" + block
    if existing_text.endswith("\n\n"):
        sep = ""
    elif existing_text.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    return existing_text + sep + block


def _render_entry(lid, scope, rule, why, evidence, supersedes, accepted):
    lines = [f"## {lid} · scope: {scope}",
             f"Rule: {_one_line(rule, limit=1000)}",
             f"Why: {_one_line(why, limit=1000)}",
             f"Evidence: {_one_line(evidence, limit=1000)}"]
    if supersedes:
        lines.append(f"Supersedes: {supersedes}")
    lines.append(f"Accepted: {accepted}")
    return "\n".join(lines) + "\n"


# --- inject (SessionStart) ----------------------------------------------

def _repo_lessons_path(cwd):
    """(repo_path_or_None, repo_name_or_None) for the git repo containing
    `cwd`. repo_path is None unless `.claude/maestro-lessons.md` exists."""
    top = lessons_check._git_toplevel(str(cwd or os.getcwd()))
    if top is None:
        return None, None
    candidate = top / REPO_LESSONS_RELPATH
    return (candidate if candidate.is_file() else None), top.name


def build_injection(cwd):
    """The exact `additionalContext` string for a SessionStart in `cwd`, or
    None when there is nothing worth saying (silence is the common case)."""
    personal_path = store_dir() / LESSONS_FILE
    repo_path, repo_name_ = _repo_lessons_path(cwd)

    personal_entries = lessons_check.parse(_read_text_or_empty(personal_path))
    repo_entries = lessons_check.parse(_read_text_or_empty(repo_path))
    active = lessons_check.active_for(personal_entries + repo_entries, repo_name_)

    lines = []
    if active:
        lines.append(lessons_check.render_injection(active))

    reasons = lessons_check.check(personal_path, repo_path, repo_name_)
    if reasons:
        lines.append(f"LESSONS CHECK FAILED — run lessons_check.py; {reasons[0]}")

    pending = [c for c in load_candidates() if c.get("status") == "pending"]
    if pending:
        lines.append(f"{len(pending)} lesson candidate(s) pending — "
                      "review with /maestro:lessons")

    if not lines:
        return None
    return "\n".join(lines)


def inject(payload):
    """SessionStart hook body. Never raises; returns the hook JSON dict to
    print, or None to stay silent."""
    if not LESSONS_ON:
        return None
    payload = payload if isinstance(payload, dict) else {}
    try:
        ctx = build_injection(payload.get("cwd"))
    except Exception:
        ctx = None
    if not ctx:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": payload.get("hook_event_name") or "SessionStart",
            "additionalContext": ctx,
        },
        "suppressOutput": True,
    }


# --- accept ---------------------------------------------------------------

def accept(rule, why, evidence, scope="global", supersedes=None,
           candidates=None, d=None):
    """Allocate the next L-NNN, append it to the personal lessons.md, and
    validate. On failure the file is restored byte-for-byte (or removed, if
    this call created it) and (None, reasons, None) is returned. On success
    the store repo gets a commit, the listed candidates are marked accepted,
    and (lid, [], lid) is returned."""
    d = Path(d) if d else store_dir()
    init(d)
    path = d / LESSONS_FILE
    existed = path.is_file()
    prev_bytes = path.read_bytes() if existed else None
    existing_text = prev_bytes.decode("utf-8") if existed else ""

    lid = _next_id(path if existed else None, "L-")
    accepted = time.strftime("%Y-%m-%d")
    block = _render_entry(lid, scope, rule, why, evidence, supersedes, accepted)
    path.write_text(_append_entry_text(existing_text, block), encoding="utf-8")

    reasons = lessons_check.check(path, None, None)
    if reasons:
        if existed:
            path.write_bytes(prev_bytes)
        else:
            try:
                path.unlink()
            except OSError:
                pass
        return None, reasons, None

    try:
        subprocess.run(["git", "-C", str(d), "add", LESSONS_FILE],
                       capture_output=True, check=False)
        first_words = " ".join(rule.split()[:6])
        subprocess.run(
            ["git", "-C", str(d),
             "-c", "user.email=maestro@localhost", "-c", "user.name=maestro",
             "commit", "-q", "-m", f"lesson: {lid} {first_words}"],
            capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass

    for cid in (candidates or []):
        try:
            mark(cid, "accepted", d=d)
        except ValueError:
            pass

    return lid, [], lid


# --- publish ---------------------------------------------------------------

def publish(lesson_id, cwd=None, d=None):
    """Copy an accepted, repo-scoped lesson from the personal store into the
    current repo's `.claude/maestro-lessons.md` as the next R-NNN. Leaves it
    uncommitted. Returns (rid_or_None, message)."""
    d = Path(d) if d else store_dir()
    personal_path = d / LESSONS_FILE
    entries = lessons_check.parse(_read_text_or_empty(personal_path))
    entry = next((e for e in entries if e.get("id") == lesson_id), None)
    if entry is None:
        return None, f"no such lesson: {lesson_id}"

    scope = entry.get("scope") or ""
    if not scope.startswith("repo:"):
        return None, (f"refusing to publish {lesson_id}: scope is "
                       f"{scope!r}, not repo:<name> — global lessons stay "
                       "in the personal store")
    target_repo = scope[len("repo:"):]

    top = lessons_check._git_toplevel(str(cwd or os.getcwd()))
    if top is None:
        return None, "cwd is not inside a git repository"
    if top.name != target_repo:
        return None, (f"refusing to publish {lesson_id}: scoped to "
                       f"repo:{target_repo}, but cwd is in repo {top.name!r}")

    repo_path = top / REPO_LESSONS_RELPATH
    existed = repo_path.is_file()
    prev_bytes = repo_path.read_bytes() if existed else None
    existing_text = prev_bytes.decode("utf-8") if existed else ""

    rid = _next_id(repo_path if existed else None, "R-")
    block = _render_entry(rid, scope, entry.get("rule"), entry.get("why"),
                          entry.get("evidence"), entry.get("supersedes"),
                          entry.get("accepted"))
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(_append_entry_text(existing_text, block), encoding="utf-8")

    reasons = lessons_check.check(None, repo_path, top.name)
    if reasons:
        if existed:
            repo_path.write_bytes(prev_bytes)
        else:
            try:
                repo_path.unlink()
            except OSError:
                pass
        return None, "LESSONS FAIL\n" + "\n".join(f"- {r}" for r in reasons)

    return rid, (f"Published {rid} to {repo_path} — left uncommitted. "
                 "Commit it with your work.")


# --- status ---------------------------------------------------------------

def status_report(cwd=None, d=None):
    """Active count/chars vs. budget for the current context, plus queue
    counters. See lessons_check for the budget constants."""
    cwd = cwd or os.getcwd()
    personal_path = (Path(d) if d else store_dir()) / LESSONS_FILE
    repo_path, repo_name_ = _repo_lessons_path(cwd)

    personal_entries = lessons_check.parse(_read_text_or_empty(personal_path))
    repo_entries = lessons_check.parse(_read_text_or_empty(repo_path))
    active = lessons_check.active_for(personal_entries + repo_entries, repo_name_)
    text = lessons_check.render_injection(active)
    n_active, n_chars = len(active), len(text)

    max_entries = lessons_check.MAX_BUDGET_ENTRIES
    max_chars = lessons_check.MAX_BUDGET_CHARS
    entries_pct = (n_active / max_entries) if max_entries else 0.0
    chars_pct = (n_chars / max_chars) if max_chars else 0.0
    pct = max(entries_pct, chars_pct)

    pending = [c for c in load_candidates(d=d) if c.get("status") == "pending"]
    rejected = load_rejected(d=d)

    return {
        "active": n_active,
        "chars": n_chars,
        "max_entries": max_entries,
        "max_chars": max_chars,
        "percent": round(pct * 100, 1),
        "near_budget": pct >= lessons_check.NEAR_BUDGET_RATIO,
        "pending": len(pending),
        "rejected": len(rejected),
        "repo": repo_name_,
    }


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


def _cmd_inject(args):
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        payload = {}
    try:
        out = inject(payload)
    except Exception:
        out = None
    if out:
        print(json.dumps(out))
    return 0


def _cmd_accept(args):
    scope = args.scope or "global"
    if scope != "global" and not scope.startswith("repo:"):
        print(f"invalid scope: {scope!r} (want 'global' or 'repo:<name>')",
             file=sys.stderr)
        return 1
    candidates = [c for c in (args.candidates.split(",") if args.candidates else [])
                 if c]
    lid, reasons, _ = accept(args.rule, args.why, args.evidence, scope=scope,
                             supersedes=args.supersedes, candidates=candidates)
    if reasons:
        print("LESSONS FAIL", file=sys.stderr)
        for r in reasons:
            print(f"- {r}", file=sys.stderr)
        return 1
    print(lid)
    return 0


def _cmd_publish(args):
    rid, msg = publish(args.id)
    if rid is None:
        print(msg, file=sys.stderr)
        return 1
    print(rid)
    print(msg)
    return 0


def _cmd_status(args):
    rep = status_report()
    if args.json:
        print(json.dumps(rep))
    else:
        print(f"active: {rep['active']}/{rep['max_entries']} entries, "
             f"{rep['chars']}/{rep['max_chars']} chars ({rep['percent']}%)")
        if rep["near_budget"]:
            print("NEAR BUDGET — propose a consolidation")
        print(f"pending candidates: {rep['pending']}")
        print(f"rejected rules: {rep['rejected']}")
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

    sub.add_parser("inject").set_defaults(func=_cmd_inject)

    p = sub.add_parser("accept")
    p.add_argument("--rule", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--scope", default="global")
    p.add_argument("--supersedes")
    p.add_argument("--candidates")
    p.set_defaults(func=_cmd_accept)

    p = sub.add_parser("publish")
    p.add_argument("id")
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
