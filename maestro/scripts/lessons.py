#!/usr/bin/env python3
"""Maestro lessons — the cross-session lesson pipeline.

A "lesson" is a small, human-approved rule about how Maestro orchestrates.
This module owns the whole pipeline except the pure parse/validate/trust
logic, which lives in `lessons_check.py` and is reused here by its public
API (`parse`, `fold`, `active_for`, `render_injection`, `evaluate`, the
approvals-ledger helpers, and `repo_identity`):

  - CAPTURE: candidates fed automatically by re-orchestration findings
    (`reorchestrate.py`) and manually by the conductor when the user corrects
    it (`flag`). Capture never decides a candidate is a lesson.
  - INJECT: the SessionStart hook. Injects active, APPROVED lessons only. If
    the check fails it injects nothing but a one-line warning (fail closed),
    and its output is always hard-capped at the lesson budget.
  - ACCEPT: turns a candidate into an active personal-store lesson (`L-NNN`)
    after the user's explicit yes: appends, validates, records the approval in
    the ledger, commits — and rolls every byte back if any step fails.
  - PUBLISH: copies an accepted, repo-scoped lesson into that repo's own
    `.claude/maestro-lessons.md` (`R-NNN`), uncommitted — the user commits it.
  - TRUST: records the user's approval of an `R-NNN` entry a teammate wrote,
    bound to the exact bytes they were shown (its sha256).
  - REPAIR: shows (and on --apply with the shown sha, removes) an
    uncommitted, unapproved tail left by an interrupted accept.
  - STATUS: budget and queue counters for `/maestro:lessons`.

accept, publish, trust and reject hold an exclusive lock on the store for
their whole sequence (non-blocking with a bounded retry, then a clear
error). Capture never blocks: it gives up silently after a short retry.

Store layout ($MAESTRO_LESSONS_DIR, else ~/.claude/maestro/lessons/):
  lessons.md         the personal lesson file, git-tracked, append-only.
  candidates.jsonl   append-only, gitignored. One creation record per
                     candidate plus later status-change records; folding
                     takes the latest status ("pending" by default).
  .dedupe/<hash>     per-session capture dedupe index (one fingerprint per
                     line), so capture never scans candidates.jsonl.
  .dedupe/pending/<hash>  one marker per fingerprint with a pending
                     candidate (holds its id); `mark` clears it.
  rejected.jsonl     append-only, tracked in the store's own git repo.
  .gitignore         written by `init`.
Approvals ledger: `lessons-approved.jsonl` NEXT TO the store directory
($MAESTRO_LESSONS_APPROVALS overrides) — see lessons_check.py.

Candidate record (creation):
  {"id": "c-<8 hex>", "at": <epoch float>, "session": "<session id>",
   "repo": "<repo identity of cwd, or null>",
   "source": "finding"|"correction", "fingerprint": "<finding fingerprint
   or null>", "tool_use_id": "<id or null>", "text": "<one line, <=300 chars>"}

Candidate record (status change, appended later, same "id"):
  {"id": "c-...", "status": "reviewed"|"not-a-lesson"|"accepted"|"tweak", "at": ...}

Capture skips a finding whose kind is rejected or already has a pending
candidate from any session: one queued candidate per mechanic is enough to
review it.

Tweaks: when the real fix for a candidate is a change to Maestro itself (a
nudge that was wrong, a hook or rule that cost turns), `tweak` appends an
unchecked item to $MAESTRO_TWEAKS_FILE (else ~/.claude/maestro/tweaks.md) and
marks the candidates `tweak`, so they leave the queue and never become a
lesson. A maestro-marketplace session works the list and ticks items off.

Rejected record:
  {"key": "<key>", "rule": "<drafted rule text>", "at": <epoch float>}
Rejection keys: a finding is keyed by its fingerprint kind; a correction
(fingerprint null) is keyed `correction:<first 16 hex of sha256 of its
normalized text>` — normalized = control chars stripped, whitespace
collapsed, lowercased. `candidates` prints it as the `key` column/field.

CLI:
  lessons.py init
  lessons.py flag --session SID "<one line>"
  lessons.py candidates [--pending] [--json]
  lessons.py mark ID --status reviewed|not-a-lesson|accepted|tweak
  lessons.py tweak --note "<what to change in Maestro>" [--candidates c-..,c-..]
                   [--session SID]
  lessons.py reject --key KEY --rule "<drafted rule text>"
  lessons.py rejected [--json]
  lessons.py inject                     (SessionStart hook; reads stdin)
  lessons.py accept --rule R --why W --evidence E [--scope global|repo:<name>]
                    [--supersedes ID[,ID...]] [--candidates c-..,c-..]
                    [--preview | --sha SHA256]
  lessons.py publish ID [--preview | --sha SHA256]
  lessons.py untrusted [--json]
  lessons.py trust R-NNN --sha SHA256
  lessons.py repair [--apply --sha SHA256]
  lessons.py status [--json]

Env:
  MAESTRO_LESSONS_DIR        overrides the store directory
  MAESTRO_LESSONS_APPROVALS  overrides the approvals ledger path
  MAESTRO_TWEAKS_FILE        overrides the tweak-request file
  MAESTRO_LESSONS=0          disable capture, flag and injection (tests/
                             replay.py sets this so a replay never touches
                             the real store). The explicit review commands
                             (accept/reject/mark/publish/trust) still work.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lessons_check

lc = lessons_check

STATUSES = ("reviewed", "not-a-lesson", "accepted", "tweak")
CANDIDATES_FILE = "candidates.jsonl"
REJECTED_FILE = "rejected.jsonl"
LESSONS_FILE = lc.LESSONS_FILE
REPO_LESSONS_RELPATH = lc.REPO_LESSONS_RELPATH
DEDUPE_DIR = ".dedupe"
JSONL_LOCK = ".lock"          # guards candidates.jsonl / rejected.jsonl appends
STORE_LOCK = ".store.lock"    # guards a whole accept/publish/trust/reject
GITIGNORE_LINES = (CANDIDATES_FILE, f"{DEDUPE_DIR}/", JSONL_LOCK, STORE_LOCK)

CAPTURE_LOCK_TRIES, CAPTURE_LOCK_DELAY = 5, 0.02     # <= ~0.1 s, then skip
REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY = 50, 0.1       # <= ~5 s, then error
LOCK_BUSY = ("lessons store is locked by another accept/publish/trust — "
             "nothing was written; try again in a moment")
STATUS_LINE_CHARS = 600      # each status line after the lessons block
GIT_ID = ["-c", "user.email=maestro@localhost", "-c", "user.name=maestro"]

# Repo-name lookups are one `git rev-parse` per distinct cwd; cache them.
_REPO_CACHE = {}


def lessons_on():
    return os.environ.get("MAESTRO_LESSONS", "1") != "0"


def store_dir():
    """The lessons store directory. Never created here — callers do that."""
    return lc.default_store_dir()


def approvals_path(d=None):
    return lc.approvals_path(Path(d) if d else store_dir())


def repo_name(cwd):
    """Repo identity (main working tree basename) of `cwd`, or None."""
    cwd = str(cwd or os.getcwd())
    if cwd not in _REPO_CACHE:
        try:
            _REPO_CACHE[cwd] = lc.repo_identity(cwd)[1]
        except Exception:
            _REPO_CACHE[cwd] = None
    return _REPO_CACHE[cwd]


def init(d=None):
    """mkdir -p the store, `git init` it if it is not already a repo, and
    write a `.gitignore` that keeps queue and lock files out of it."""
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
        have = cur.splitlines()
        missing = [ln for ln in GITIGNORE_LINES if ln not in have]
        if missing:
            with open(gi, "a") as f:
                if cur and not cur.endswith("\n"):
                    f.write("\n")
                f.write("".join(f"{ln}\n" for ln in missing))
    except OSError:
        pass
    return d


# --- locking --------------------------------------------------------------

@contextlib.contextmanager
def _flock(path, tries, delay):
    """Exclusive flock, non-blocking with a bounded retry. Yields True when
    held, False when it could not be taken (or the lock file can't open)."""
    fd = None
    got = False
    try:
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            fd = None
        if fd is not None:
            for attempt in range(max(1, tries)):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    if attempt + 1 < tries:
                        time.sleep(delay)
        yield got
    finally:
        if fd is not None:
            if got:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


def _append_line(d, filename, obj, tries=REVIEW_LOCK_TRIES, delay=REVIEW_LOCK_DELAY):
    """Append one JSON line under the jsonl lock. False if the lock was busy."""
    with _flock(d / JSONL_LOCK, tries, delay) as ok:
        if not ok:
            return False
        with open(d / filename, "a") as f:
            f.write(json.dumps(obj) + "\n")
    return True


# --- capture --------------------------------------------------------------

def _candidate_record(session_id, cwd, source, fingerprint, tool_use_id, text):
    return {
        "id": f"c-{secrets.token_hex(4)}",
        "at": time.time(),
        "session": session_id,
        "repo": repo_name(cwd),
        "source": source,
        "fingerprint": fingerprint,
        "tool_use_id": tool_use_id,
        "text": lc.clean_one_line(text, 300),
    }


def _new_candidate(d, session_id, cwd, source, fingerprint, tool_use_id, text,
                   tries=REVIEW_LOCK_TRIES, delay=REVIEW_LOCK_DELAY):
    d = Path(d)
    fp = lc.clean_one_line(fingerprint, 200) if fingerprint is not None else None
    rec = _candidate_record(session_id, cwd, source, fp, tool_use_id, text)
    if not _append_line(d, CANDIDATES_FILE, rec, tries, delay):
        return None
    return rec["id"]


def _dedupe_path(d, session_id):
    h = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]
    return Path(d) / DEDUPE_DIR / h


def _pending_marker(d, fp):
    h = hashlib.sha256(fp.encode("utf-8")).hexdigest()[:24]
    return Path(d) / DEDUPE_DIR / "pending" / h


def _clear_pending(d, cid):
    """Drop the pending marker that names `cid`, once it has a status."""
    pdir = Path(d) / DEDUPE_DIR / "pending"
    try:
        for m in pdir.iterdir():
            try:
                if m.read_text(encoding="utf-8").strip() == cid:
                    m.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _suppressed(d, fp):
    """True when this finding kind is rejected or already awaiting review.

    Before this check, every nudge minted a candidate: 245 in 31 sessions,
    152 later marked not-a-lesson, and all 19 left pending at 0.5.2 were
    repeats of three kinds filed after a lesson covering each existed.
    """
    if _pending_marker(d, fp).is_file():
        return True
    for r in load_rejected(d):
        k = r.get("key") or ""
        if k and (fp == k or fp.startswith(k + ":")):
            return True
    return False


def capture_finding(session_id, cwd, fingerprint, text, tool_use_id=None):
    """Append a re-orchestration finding as a candidate lesson.

    Skips silently if this session already captured this fingerprint, if the
    kind is rejected, or if a candidate with this fingerprint is already
    pending from any session. Never blocks for more than ~0.1 s on a busy
    lock and never raises: a failure here must never change what the caller
    prints or does.
    """
    try:
        if not lessons_on():
            return None
        d = store_dir()
        d.mkdir(parents=True, exist_ok=True)
        fp = lc.clean_one_line(fingerprint, 200) if fingerprint is not None else None
        rec = _candidate_record(session_id, cwd, "finding", fp, tool_use_id, text)
        with _flock(d / JSONL_LOCK, CAPTURE_LOCK_TRIES, CAPTURE_LOCK_DELAY) as ok:
            if not ok:
                return None
            idx = _dedupe_path(d, session_id) if fp is not None else None
            if idx is not None and idx.is_file():
                with open(idx, encoding="utf-8") as f:
                    if fp in {ln.rstrip("\n") for ln in f}:
                        return None
            if fp is not None and _suppressed(d, fp):
                return None
            with open(d / CANDIDATES_FILE, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if idx is not None:
                idx.parent.mkdir(exist_ok=True)
                with open(idx, "a", encoding="utf-8") as f:
                    f.write(fp + "\n")
                pm = _pending_marker(d, fp)
                pm.parent.mkdir(parents=True, exist_ok=True)
                pm.write_text(rec["id"], encoding="utf-8")
        return rec["id"]
    except Exception:
        return None


def flag(session_id, text, cwd=None):
    """Record a correction the user made to how Maestro orchestrated.

    Source is always "correction"; never deduped by fingerprint. Respects
    MAESTRO_LESSONS=0 like capture and inject. Never raises.
    """
    try:
        if not lessons_on():
            return None
        d = store_dir()
        d.mkdir(parents=True, exist_ok=True)
        return _new_candidate(d, session_id, cwd or os.getcwd(), "correction",
                              None, None, text)
    except Exception:
        return None


def candidate_key(rec):
    """Rejection key for a candidate. Corrections have no fingerprint, so
    they are keyed on their normalized text; findings keep the documented
    "<fingerprint kind>" key chosen by the reviewer (None here)."""
    if rec.get("source") == "correction":
        norm = lc.clean_one_line(rec.get("text") or "").lower()
        return "correction:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
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
                if not isinstance(rec, dict):
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
        key = candidate_key(rec)
        if key:
            rec["key"] = key
        out.append(rec)
    return out


def mark(cid, status, d=None):
    """Append a status-change record for candidate `cid`."""
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r} (want one of {STATUSES})")
    d = Path(d) if d else store_dir()
    d.mkdir(parents=True, exist_ok=True)
    if not _append_line(d, CANDIDATES_FILE,
                        {"id": cid, "status": status, "at": time.time()}):
        raise RuntimeError("lessons store is busy — status not recorded; try again")
    _clear_pending(d, cid)


def reject(key, rule, d=None):
    """Record a permanently-rejected rule and commit it in the store repo."""
    d = Path(d) if d else store_dir()
    init(d)
    rec = {"key": lc.clean_one_line(key, 200), "rule": lc.clean_one_line(rule, 1000),
           "at": time.time()}
    with _flock(d / STORE_LOCK, REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            raise RuntimeError(LOCK_BUSY)
        if not _append_line(d, REJECTED_FILE, rec):
            raise RuntimeError("lessons store is busy — rejection not recorded")
        try:
            subprocess.run(["git", "-C", str(d), "add", "--", REJECTED_FILE],
                           capture_output=True, check=False)
            subprocess.run(["git", "-C", str(d), *GIT_ID, "commit", "-q",
                            "-m", f"reject: {rec['key']}", "--", REJECTED_FILE],
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


def tweaks_path(d=None):
    override = os.environ.get("MAESTRO_TWEAKS_FILE")
    if override:
        return Path(override)
    return (Path(d) if d else store_dir()).parent / "tweaks.md"


def open_tweaks(d=None):
    """Unchecked items in the tweak-request file."""
    try:
        lines = tweaks_path(d).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.startswith("- [ ] ")]


def tweak(note, cids=(), session_id=None, d=None):
    """Record a requested change to Maestro and take its candidates out of
    the lesson queue. Returns the line written."""
    note = lc.clean_one_line(note, 400)
    if not note:
        raise ValueError("a tweak needs a note saying what to change")
    path = tweaks_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [time.strftime("%Y-%m-%d")]
    if session_id:
        meta.append(f"session {str(session_id)[:8]}")
    if cids:
        meta.append("candidates " + ",".join(cids))
    line = f"- [ ] {note} ({'; '.join(meta)})"
    with _flock(path.parent / ".tweaks.lock", REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            raise RuntimeError("tweak file is busy — nothing written; try again")
        new = not path.is_file()
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write("# Maestro tweak requests\n\nChanges to Maestro itself, found "
                        "while using it. Fix in maestro-marketplace, then tick the box.\n\n")
            f.write(line + "\n")
    unmarked = []
    for cid in cids:
        try:
            mark(cid, "tweak", d=d)
        except RuntimeError:
            unmarked.append(cid)
    if unmarked:
        # The line is written; re-running `tweak` would duplicate it.
        raise RuntimeError(f"tweak recorded, but these candidates were not marked: "
                           f"{', '.join(unmarked)} — run `lessons.py mark <id> --status "
                           f"tweak` for each")
    return line


# --- shared file helpers -----------------------------------------------

def _next_id(entries, prefix):
    """The next `<prefix>NNN` id, one past the highest strictly-formed id
    with that prefix, or None once the 3-digit space is exhausted."""
    id_re = lc.ID_RE_PERSONAL if prefix == "L-" else lc.ID_RE_REPO
    nums = [int(e["id"][2:]) for e in entries
            if e.get("id") and id_re.fullmatch(e["id"])]
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n:03d}" if n <= 999 else None


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


def _render_entry(lid, scope, rule, why, evidence, supersedes_ids, accepted):
    """One entry block. Every field must already be clean one-line text."""
    lines = [f"## {lid} · scope: {scope}",
             f"Rule: {rule}",
             f"Why: {why}",
             f"Evidence: {evidence}"]
    if supersedes_ids:
        lines.append("Supersedes: " + ", ".join(supersedes_ids))
    lines.append(f"Accepted: {accepted}")
    return "\n".join(lines) + "\n"


def _append_self_check(existing_text, new_text, expected_id, block):
    """Re-parse after appending: exactly one new entry, with the expected
    id, clean, and byte-identical to the rendered block."""
    old_ids = [e.get("id") for e in lc.parse(existing_text)]
    new = lc.parse(new_text)
    if [e.get("id") for e in new] != old_ids + [expected_id]:
        return "append self-check failed: the new text does not parse as exactly one new entry"
    if new[-1]["errors"] or new[-1]["raw"] != block.rstrip("\n"):
        return "append self-check failed: the new entry does not round-trip"
    return None


def _input_error(name, value):
    if not isinstance(value, str):
        return f"{name} is missing"
    bad = [c for c in value if lc.forbidden_char(c) and c not in "\n\r\t"]
    if bad:
        return (f"{name} contains a control or line-separator character "
                f"(U+{ord(bad[0]):04X}) — refusing; retype it as plain text")
    if not lc.clean_one_line(value):
        return f"{name} is empty"
    return None


def _parse_supersedes(value, id_re):
    """(list of ids, error or None) from a comma-separated string or list."""
    if value is None or value == "" or value == []:
        return [], None
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    ids = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    if not ids:
        return [], None
    bad = [i for i in ids if not id_re.fullmatch(i)]
    if bad:
        return [], ("--supersedes takes comma-separated ids of the form "
                    f"{'L' if id_re is lc.ID_RE_PERSONAL else 'R'}-NNN (ASCII digits)")
    if len(set(ids)) != len(ids):
        return [], "--supersedes names the same id twice"
    if len(ids) > lc.MAX_SUPERSEDES:
        return [], f"--supersedes names {len(ids)} ids, cap is {lc.MAX_SUPERSEDES}"
    return ids, None


def _refuse_nonregular(path):
    """Error text if `path` exists as a symlink or non-regular file."""
    if os.path.lexists(str(path)) and (Path(path).is_symlink()
                                       or not lc._is_regular_nolink(path)):
        return f"{path} is a symlink or not a regular file — refusing to write through it"
    return None


def _restore(path, prev_bytes):
    """Put a lesson file back exactly as it was (or remove it if new)."""
    if prev_bytes is None:
        try:
            Path(path).unlink()
        except OSError:
            pass
    else:
        Path(path).write_bytes(prev_bytes)


def _append_ledger(ap, record):
    """Append one approval line; returns the ledger size before the write."""
    ap = Path(ap)
    ap.parent.mkdir(parents=True, exist_ok=True)
    err = _refuse_nonregular(ap)
    if err:
        raise OSError(err)
    fd = os.open(str(ap), os.O_WRONLY | os.O_APPEND | os.O_CREAT
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        size = os.fstat(fd).st_size
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return size


def _truncate_ledger(ap, size):
    try:
        if Path(ap).stat().st_size > size:
            os.truncate(str(ap), size)
    except OSError:
        pass


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True)


def _commit_lessons(d, lid, rule):
    """(ok, error). Commit ONLY lessons.md; a non-zero exit (a failing hook,
    no git, nothing staged) is a failure."""
    try:
        add = _git(d, "add", "--", LESSONS_FILE)
        if add.returncode != 0:
            tail = (add.stderr or "git add failed").strip().splitlines()
            return False, (tail[-1] if tail else "git add failed")
        first_words = " ".join(rule.split()[:6])
        c = _git(d, *GIT_ID, "commit", "-q", "-m", f"lesson: {lid} {first_words}",
                 "--", LESSONS_FILE)
        if c.returncode != 0:
            tail = (c.stderr or c.stdout or "git commit failed").strip().splitlines()
            return False, (tail[-1] if tail else "git commit failed")
        return True, None
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git unavailable ({e.__class__.__name__})"


def _unstage(d):
    if _git(d, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
        _git(d, "reset", "-q", "HEAD", "--", LESSONS_FILE)
    else:
        _git(d, "rm", "--cached", "-q", "--ignore-unmatch", "--", LESSONS_FILE)


# --- inject (SessionStart) ----------------------------------------------

def build_injection(cwd):
    """The exact `additionalContext` string for a SessionStart in `cwd`, or
    None when there is nothing worth saying (silence is the common case).

    Fails closed per tier:
      - personal file, approvals ledger, or personal budget fails -> the ONLY
        thing injected is one warning line naming the validator and the
        first reason (plus a pointer to `repair` when the cause is an
        unapproved uncommitted tail) — never a lesson;
      - only the repo file fails -> personal lessons still inject, the repo
        file contributes nothing, and one line names the problem.
    Always hard-capped at MAX_BUDGET_CHARS.
    """
    d = store_dir()
    personal_path = d / LESSONS_FILE
    repo_path, repo_name_ = lc.repo_lessons_path(cwd)
    ev = lc.evaluate(personal_path, repo_path, repo_name_, approvals_path())

    if ev["personal_reasons"]:
        first = lc.clean_one_line(ev["personal_reasons"][0], 300)
        hint = ""
        try:
            plan = repair_plan(d)
            if plan["problem"] is None and plan["cut"] is not None:
                hint = (" — this looks like an unapproved uncommitted tail left by an "
                        "interrupted accept: review it with "
                        f"python3 \"{Path(__file__).resolve()}\" repair")
        except Exception:
            hint = ""
        return lc.cap_text(
            "MAESTRO LESSONS OFF — the lessons check failed, so NO lessons were "
            f"injected. Run: python3 \"{lc.SCRIPT_PATH}\" — first reason: {first}{hint}")

    # The lessons block is capped on its own (validation already keeps it
    # within budget); status lines follow it, each bounded, so a status line
    # can never evict an approved lesson. Worst case stays far below the
    # 10,000-char point where Claude Code spills context to a file.
    blocks, lines = [], []
    active = lc.active_set(ev, repo_name_)
    if active:
        blocks.append(lc.cap_text(lc.render_injection(active)))
    if ev["repo_reasons"]:
        first = lc.clean_one_line(ev["repo_reasons"][0], 300)
        lines.append(f"REPO LESSONS OFF — {repo_path} failed the lessons check, so none "
                     "of its lessons were injected (personal lessons were). Run: "
                     f"python3 \"{lc.SCRIPT_PATH}\" — first reason: {first}")
    else:
        untrusted = len(ev["repo"]) - len(ev["trusted"])
        if untrusted > 0:
            lines.append(f"{untrusted} untrusted repo lesson(s) in {repo_path} were "
                         "NOT injected — review with /maestro:lessons")
    pending = [c for c in load_candidates() if c.get("status") == "pending"]
    if pending:
        lines.append(f"{len(pending)} lesson candidate(s) pending — "
                     "review with /maestro:lessons")
    lines = [lc.clean_one_line(ln, STATUS_LINE_CHARS) for ln in lines]
    if not blocks and not lines:
        return None
    return "\n".join(blocks + lines)


def inject(payload):
    """SessionStart hook body. Never raises; returns the hook JSON dict to
    print, or None to stay silent."""
    if not lessons_on():
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

def _accept_inputs(rule, why, evidence, scope, supersedes):
    errs = []
    fields = {}
    for name, val in (("Rule", rule), ("Why", why), ("Evidence", evidence)):
        err = _input_error(name, val)
        if err:
            errs.append(err)
        else:
            fields[name] = lc.clean_one_line(val)
    scope = scope or "global"
    if not isinstance(scope, str) or not lc.SCOPE_RE.fullmatch(scope):
        errs.append("invalid scope (want 'global' or 'repo:<name>', name in [A-Za-z0-9._-])")
    sup_ids, sup_err = _parse_supersedes(supersedes, lc.ID_RE_PERSONAL)
    if sup_err:
        errs.append(sup_err)
    return fields, scope, sup_ids, errs


def accept_preview(rule, why, evidence, scope="global", supersedes=None, d=None):
    """The exact entry block `accept` would append right now — heading with
    id and scope, Rule, Why, Evidence, Supersedes, Accepted — and its
    sha256, without writing anything. Returns (info_or_None, reasons) with
    info = {"id", "block", "sha"}. Show the user the block; pass the sha to
    accept(expect_sha=...) so what lands is exactly what they approved."""
    fields, scope, sup_ids, errs = _accept_inputs(rule, why, evidence, scope, supersedes)
    if errs:
        return None, errs
    d = Path(d) if d else store_dir()
    plan, reasons = _accept_plan(d, fields, scope, sup_ids)
    if plan is None:
        return None, reasons
    return {"id": plan["lid"], "block": plan["block"], "sha": plan["sha"]}, []


def accept(rule, why, evidence, scope="global", supersedes=None,
           candidates=None, d=None, cwd=None, expect_sha=None):
    """Only after the user's explicit yes to this exact entry. Allocates the
    next L-NNN, appends it, validates the post-state (every scope's budget,
    the approvals ledger, append-only history), records the approval, and
    commits. On ANY failure — validation, ledger write, or commit — the file
    is restored byte-for-byte (or removed if this call created it), the
    index is unstaged, the approval is not written, and (None, reasons,
    None) is returned. On success the listed candidates are marked accepted
    and (lid, [], lid) is returned. `supersedes` is one id or a
    comma-separated list/sequence of them (consolidation). With
    `expect_sha` (from accept_preview), refuses unless the block about to be
    appended is byte-for-byte the one the user was shown."""
    fields, scope, sup_ids, errs = _accept_inputs(rule, why, evidence, scope, supersedes)
    if expect_sha is not None and not (isinstance(expect_sha, str)
                                       and lc.SHA_RE.fullmatch(expect_sha)):
        errs.append("--sha must be the full 64-hex sha256 printed by --preview")
    if errs:
        return None, errs, None

    d = Path(d) if d else store_dir()
    init(d)
    with _flock(d / STORE_LOCK, REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            return None, [LOCK_BUSY], None
        lid, reasons = _accept_locked(d, fields, scope, sup_ids, cwd, expect_sha)
    if lid is None:
        return None, reasons, None

    for cid in (candidates or []):
        try:
            mark(cid, "accepted", d=d)
        except (ValueError, RuntimeError):
            pass
    return lid, [], lid


def _accept_plan(d, fields, scope, sup_ids):
    """(plan, reasons): the block to append and the text around it."""
    path = d / LESSONS_FILE
    err = _refuse_nonregular(path)
    if err:
        return None, [err]
    prev_bytes = path.read_bytes() if path.exists() else None
    try:
        existing_text = prev_bytes.decode("utf-8") if prev_bytes is not None else ""
    except UnicodeDecodeError:
        return None, [f"{path} is not valid UTF-8 — refusing to append"]

    existing = lc.parse(existing_text)
    by_id = {e["id"]: e for e in existing if e.get("id")}
    retired = lc.fold(existing)
    for s in sup_ids:
        t = by_id.get(s)
        if t is None:
            return None, [f"--supersedes {s}: no such lesson in {path}"]
        if t.get("scope") != scope:
            return None, [f"--supersedes {s}: it is scope {t.get('scope')}, the new "
                          f"lesson is {scope} — a lesson may only supersede one "
                          "with the same scope"]
        if s in retired:
            return None, [f"--supersedes {s}: already superseded by {retired[s]}"]

    lid = _next_id(existing, "L-")
    if lid is None:
        return None, ["the L-NNN id space is exhausted"]
    block = _render_entry(lid, scope, fields["Rule"], fields["Why"],
                          fields["Evidence"], sup_ids, time.strftime("%Y-%m-%d"))
    new_text = _append_entry_text(existing_text, block)
    err = _append_self_check(existing_text, new_text, lid, block)
    if err:
        return None, [err]
    return {"lid": lid, "block": block, "sha": lc.entry_sha(block.rstrip("\n")),
            "new_text": new_text, "prev_bytes": prev_bytes}, []


def _accept_locked(d, fields, scope, sup_ids, cwd, expect_sha=None):
    plan, reasons = _accept_plan(d, fields, scope, sup_ids)
    if plan is None:
        return None, reasons
    if expect_sha is not None and plan["sha"] != expect_sha:
        return None, ["the entry that would be appended is not the one that was "
                      "shown (sha256 mismatch) — preview again and re-ask"]
    path = d / LESSONS_FILE
    lid, block, new_text, prev_bytes = (plan["lid"], plan["block"], plan["new_text"],
                                        plan["prev_bytes"])

    ap = approvals_path(d)
    records, ledger_reasons = lc.read_approvals(ap)
    if ledger_reasons:
        return None, ledger_reasons
    record = lc.make_approval(lc.ledger_tail(records), lid, "personal",
                              lc.entry_sha(block.rstrip("\n")), "accept")

    repo_path, repo_name_ = lc.repo_lessons_path(cwd)
    path.write_bytes(new_text.encode("utf-8"))
    ev = lc.evaluate(path, repo_path, repo_name_, ap, extra_approvals=[record])
    # A broken repo file does not block a personal accept (it is off on its
    # own); the budget of this repo's context still counts when it is healthy.
    reasons = ev["personal_reasons"] + ev["repo_budget_reasons"]
    if reasons:
        _restore(path, prev_bytes)
        return None, reasons

    try:
        ledger_size = _append_ledger(ap, record)
    except OSError as e:
        _restore(path, prev_bytes)
        return None, [f"could not record the approval ({e}) — rolled back"]

    ok, cerr = _commit_lessons(d, lid, fields["Rule"])
    if not ok:
        _restore(path, prev_bytes)
        _unstage(d)
        _truncate_ledger(ap, ledger_size)
        return None, [f"commit to the lessons store failed — rolled back, "
                      f"nothing accepted: {cerr}"]
    return lid, []


# --- publish ---------------------------------------------------------------

def publish_preview(lesson_id, cwd=None, d=None):
    """The exact R- block `publish` would append (translated Supersedes
    included) and its sha256, without writing. Returns (info_or_None, msg)."""
    if not isinstance(lesson_id, str) or not lc.ID_RE_PERSONAL.fullmatch(lesson_id):
        return None, "publish takes a personal lesson id of the form L-NNN"
    d = Path(d) if d else store_dir()
    rid, msg, info = _publish_locked(lesson_id, cwd, d, preview=True)
    return info, msg


def publish(lesson_id, cwd=None, d=None, expect_sha=None):
    """Copy an accepted, active, repo-scoped lesson from the personal store
    into the current repo's `.claude/maestro-lessons.md` as the next R-NNN,
    and record the publish in the approvals ledger (with its source id).
    Leaves the repo file uncommitted. Returns (rid_or_None, message)."""
    if not isinstance(lesson_id, str) or not lc.ID_RE_PERSONAL.fullmatch(lesson_id):
        return None, "publish takes a personal lesson id of the form L-NNN"
    d = Path(d) if d else store_dir()
    d.mkdir(parents=True, exist_ok=True)
    if expect_sha is not None and not (isinstance(expect_sha, str)
                                       and lc.SHA_RE.fullmatch(expect_sha)):
        return None, "--sha must be the full 64-hex sha256 printed by --preview"
    with _flock(d / STORE_LOCK, REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            return None, LOCK_BUSY
        rid, msg, _ = _publish_locked(lesson_id, cwd, d, expect_sha=expect_sha)
        return rid, msg


def _publish_locked(lesson_id, cwd, d, preview=False, expect_sha=None):
    rid, msg, info = _publish_core(lesson_id, cwd, d, preview, expect_sha)
    return rid, msg, info


def _publish_core(lesson_id, cwd, d, preview, expect_sha):
    personal_path = d / LESSONS_FILE
    ap = approvals_path(d)
    pev = lc.evaluate(personal_path, None, None, ap)
    if pev["reasons"]:
        return None, ("refusing to publish: the personal store fails its check\n"
                      + "\n".join(f"- {r}" for r in pev["reasons"])), None
    entries = pev["personal"]
    entry = next((e for e in entries if e.get("id") == lesson_id), None)
    if entry is None:
        return None, f"no such lesson: {lesson_id}", None
    retired = lc.fold(entries)
    if lesson_id in retired:
        return None, (f"refusing to publish {lesson_id}: it was superseded by "
                      f"{retired[lesson_id]} — publish that one instead"), None

    scope = entry.get("scope") or ""
    if not scope.startswith("repo:"):
        return None, (f"refusing to publish {lesson_id}: scope is "
                      f"{scope!r}, not repo:<name> — global lessons stay "
                      "in the personal store"), None
    target_repo = scope[len("repo:"):]

    top, name = lc.repo_identity(cwd)
    if top is None:
        return None, "cwd is not inside a git repository", None
    if name != target_repo:
        return None, (f"refusing to publish {lesson_id}: scoped to "
                      f"repo:{target_repo}, but cwd is in repo {name!r}"), None
    tier = f"repo:{name}"

    claude_dir = top / REPO_LESSONS_RELPATH.parent
    repo_path = top / REPO_LESSONS_RELPATH
    if claude_dir.is_symlink():
        return None, f"{claude_dir} is a symlink — refusing to write through it", None
    err = _refuse_nonregular(repo_path)
    if err:
        return None, err, None
    prev_bytes = repo_path.read_bytes() if repo_path.exists() else None
    try:
        existing_text = prev_bytes.decode("utf-8") if prev_bytes is not None else ""
    except UnicodeDecodeError:
        return None, f"{repo_path} is not valid UTF-8 — refusing to append", None

    records, ledger_reasons = lc.read_approvals(ap)
    if ledger_reasons:
        return None, "\n".join(ledger_reasons), None
    repo_entries = lc.parse(existing_text)
    repo_by_id = {e["id"]: e for e in repo_entries if e.get("id")}
    published = {}   # source L-id -> R-id currently present in this repo file
    for r in records:
        if r["how"] == "publish" and r["tier"] == tier and r["id"] in repo_by_id:
            published[r.get("source")] = r["id"]
    if lesson_id in published:
        return None, (f"refusing to publish {lesson_id}: already published "
                      f"as {published[lesson_id]} in {repo_path}"), None

    repo_retired = lc.fold(repo_entries)
    sup = []
    for s in entry.get("supersedes_ids") or []:
        rid_for = published.get(s)
        if (rid_for and rid_for not in repo_retired and rid_for not in sup
                and repo_by_id[rid_for].get("scope") == scope):
            sup.append(rid_for)   # translated; an unpublished target is dropped

    rid = _next_id(repo_entries, "R-")
    if rid is None:
        return None, "the R-NNN id space is exhausted", None
    block = _render_entry(rid, scope, entry.get("rule"), entry.get("why"),
                          entry.get("evidence"), sup, entry.get("accepted"))
    new_text = _append_entry_text(existing_text, block)
    err = _append_self_check(existing_text, new_text, rid, block)
    if err:
        return None, err, None
    block_sha = lc.entry_sha(block.rstrip("\n"))
    if preview:
        return None, "", {"id": rid, "block": block, "sha": block_sha}
    if expect_sha is not None and block_sha != expect_sha:
        return None, ("the entry that would be appended is not the one that was "
                      "shown (sha256 mismatch) — preview again and re-ask"), None
    record = lc.make_approval(lc.ledger_tail(records), rid, tier, block_sha,
                              "publish", source=lesson_id)

    created_dir = not os.path.lexists(str(claude_dir))

    def rollback():
        _restore(repo_path, prev_bytes)
        if created_dir:
            try:
                claude_dir.rmdir()
            except OSError:
                pass

    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        repo_path.write_bytes(new_text.encode("utf-8"))
    except OSError as e:
        rollback()
        return None, f"could not write {repo_path}: {e}", None

    reasons = lc.evaluate(personal_path, repo_path, name, ap,
                          extra_approvals=[record])["reasons"]
    if reasons:
        rollback()
        return None, "LESSONS FAIL\n" + "\n".join(f"- {r}" for r in reasons), None
    try:
        _append_ledger(ap, record)
    except OSError as e:
        rollback()
        return None, f"could not record the publish in the approvals ledger: {e}", None

    return rid, (f"Published {lesson_id} as {rid} to {repo_path} — left "
                 "uncommitted. Commit it with your work."), None


# --- trust -----------------------------------------------------------------

def list_untrusted(cwd=None, d=None):
    """Untrusted R- entries in cwd's repo file: [{id, sha256, raw}]."""
    d = Path(d) if d else store_dir()
    repo_path, name = lc.repo_lessons_path(cwd)
    if repo_path is None:
        return []
    ev = lc.evaluate(None, repo_path, name, approvals_path(d))
    trusted = {e["id"] for e in ev["trusted"]}
    return [{"id": e["id"], "sha256": e["sha256"], "raw": e["raw"],
             "errors": list(e["errors"])}
            for e in ev["repo"] if e.get("id") not in trusted]


def trust(rid, sha, cwd=None, d=None):
    """Only after the user was shown the entry verbatim and said yes to it.
    Records an approval for R-NNN bound to `sha` (the sha256 printed by
    `untrusted`); refuses if the entry's bytes no longer match. Returns
    (rid_or_None, message)."""
    if not isinstance(rid, str) or not lc.ID_RE_REPO.fullmatch(rid):
        return None, "trust takes a repo lesson id of the form R-NNN"
    if not isinstance(sha, str) or not lc.SHA_RE.fullmatch(sha):
        return None, "--sha must be the full 64-hex sha256 printed by `untrusted`"
    d = Path(d) if d else store_dir()
    d.mkdir(parents=True, exist_ok=True)
    with _flock(d / STORE_LOCK, REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            return None, LOCK_BUSY
        repo_path, name = lc.repo_lessons_path(cwd)
        if repo_path is None or name is None:
            return None, "no .claude/maestro-lessons.md in this repo"
        ap = approvals_path(d)
        ev = lc.evaluate(d / LESSONS_FILE, repo_path, name, ap)
        entry = next((e for e in ev["repo"] if e.get("id") == rid), None)
        if entry is None:
            return None, f"no such repo lesson: {rid}"
        if any(e.get("id") == rid for e in ev["trusted"]):
            return None, f"{rid} is already trusted"
        if entry["sha256"] != sha:
            return None, (f"refusing to trust {rid}: its bytes changed since they "
                          "were shown (sha256 mismatch) — show it again")
        records, ledger_reasons = lc.read_approvals(ap)
        if ledger_reasons:
            return None, "\n".join(ledger_reasons)
        record = lc.make_approval(lc.ledger_tail(records), rid, f"repo:{name}",
                                  sha, "trust")
        reasons = lc.evaluate(d / LESSONS_FILE, repo_path, name, ap,
                              extra_approvals=[record])["reasons"]
        if reasons:
            return None, "LESSONS FAIL\n" + "\n".join(f"- {r}" for r in reasons)
        try:
            _append_ledger(ap, record)
        except OSError as e:
            return None, f"could not record the approval: {e}"
    return rid, f"Trusted {rid} in {repo_path} — it will be injected from now on."


# --- repair ----------------------------------------------------------------

def _head_bytes(d):
    """lessons.md as committed at HEAD in the store repo (b"" if none)."""
    try:
        if _git(d, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            return b""
        r = subprocess.run(["git", "-C", str(d), "show", f"HEAD:{LESSONS_FILE}"],
                           capture_output=True)
        return r.stdout if r.returncode == 0 else b""
    except (OSError, subprocess.SubprocessError):
        return b""


def repair_plan(d=None):
    """What `repair --apply` would remove, computed read-only. Returns
    {"problem": str|None, "cut": int|None, "removed": str, "rules": [str],
    "sha": str}.

    Removable, and ONLY this: the uncommitted bytes of lessons.md beyond
    HEAD, starting at the first uncommitted entry nobody approved, when
    every entry after it is also unapproved. Never committed bytes, never an
    approved entry, and never the approvals ledger: an approval is only ever
    added. If an approved lesson is missing (deleted, reset away), repair
    refuses — the ledger holds only its sha, so the text can't be rebuilt;
    the remedy is restoring the entry (e.g. from `git reflog` in the store).
    """
    d = Path(d) if d else store_dir()
    plan = {"problem": None, "cut": None, "removed": "", "rules": [], "sha": ""}
    path = d / LESSONS_FILE
    ap = approvals_path(d)
    err = _refuse_nonregular(path) or _refuse_nonregular(ap)
    if err:
        plan["problem"] = err
        return plan
    working = path.read_bytes() if path.exists() else b""
    head = _head_bytes(d)
    if not working.startswith(head):
        plan["problem"] = ("committed content of lessons.md was edited or removed — "
                           "that is not an uncommitted tail; repair will not touch it")
        return plan
    try:
        text = working.decode("utf-8")
    except UnicodeDecodeError:
        plan["problem"] = "lessons.md is not valid UTF-8 — fix by hand"
        return plan
    records, ledger_reasons = lc.read_approvals(ap)
    if ledger_reasons:
        plan["problem"] = "approvals ledger does not verify — " + ledger_reasons[0]
        return plan

    offsets, pos = [], 0
    for ln in text.split("\n"):
        offsets.append(pos)
        pos += len(ln.encode("utf-8")) + 1
    approved = {(r["id"], r["sha256"]) for r in records if r["tier"] == "personal"}
    approved_ids = {r["id"] for r in records if r["tier"] == "personal"}
    entries = lc.parse(text)
    for e in entries:
        e["_start"] = offsets[e["line"] - 1]
        e["_end"] = e["_start"] + len(e["raw"].encode("utf-8"))
    if any(e["_start"] < len(head) < e["_end"] for e in entries):
        plan["problem"] = ("an uncommitted edit extends a committed entry — "
                           "that is not a clean tail; fix by hand")
        return plan
    tail = [e for e in entries if e["_start"] >= len(head)]
    k = next((i for i, e in enumerate(tail) if (e.get("id"), e["sha256"]) not in approved), None)
    cut = None
    if k is not None:
        if any((e.get("id"), e["sha256"]) in approved for e in tail[k + 1:]):
            plan["problem"] = ("an approved entry follows an unapproved one in the "
                               "uncommitted tail — fix by hand")
            return plan
        cut = tail[k]["_start"]
        while cut > len(head) and working[cut - 2:cut] == b"\n\n":
            cut -= 1
    kept_ids = {e.get("id") for e in entries if cut is None or e["_start"] < cut}
    missing = sorted(approved_ids - kept_ids)
    if missing:
        plan["problem"] = (f"approved lesson(s) {', '.join(missing)} missing from lessons.md "
                           "— repair never removes an approval, and it cannot rebuild the "
                           "text (the ledger holds only its sha). Restore the entry, e.g. "
                           "from `git reflog` in the store, then re-check.")
        return plan
    if cut is not None:
        plan["cut"] = cut
        plan["removed"] = working[cut:].decode("utf-8")
        plan["rules"] = [f"{e.get('id')}: {e.get('rule')}" for e in tail[k:]]
    plan["sha"] = hashlib.sha256(plan["removed"].encode("utf-8")).hexdigest()
    return plan


def repair(apply=False, sha=None, d=None):
    """Dry run by default. With apply=True (only after the user's explicit yes
    to the plan it was shown) removes exactly the planned bytes, and only if
    the plan's sha still matches `sha`. Returns (ok, plan, message)."""
    d = Path(d) if d else store_dir()
    if not apply:
        plan = repair_plan(d)
        return plan["problem"] is None, plan, plan["problem"] or ""
    if not isinstance(sha, str) or not lc.SHA_RE.fullmatch(sha):
        return False, None, "--apply needs --sha: the sha256 printed by the dry run the user approved"
    with _flock(d / STORE_LOCK, REVIEW_LOCK_TRIES, REVIEW_LOCK_DELAY) as ok:
        if not ok:
            return False, None, LOCK_BUSY
        plan = repair_plan(d)
        if plan["problem"]:
            return False, plan, plan["problem"]
        if plan["cut"] is None:
            return True, plan, "nothing to repair"
        if plan["sha"] != sha:
            return False, plan, ("refusing: what would be removed changed since it was "
                                 "shown (sha256 mismatch) — show the dry run again")
        path = d / LESSONS_FILE
        path.write_bytes(path.read_bytes()[:plan["cut"]])
        if _git(d, "diff", "--cached", "--quiet", "--", LESSONS_FILE).returncode != 0:
            _unstage(d)
    reasons = lc.evaluate(d / LESSONS_FILE, None, None, approvals_path(d))["personal_reasons"]
    msg = "repaired." + ("" if not reasons else
                         " The store still fails its check:\n" + "\n".join(f"- {r}" for r in reasons))
    return True, plan, msg


# --- status ---------------------------------------------------------------

def status_report(cwd=None, d=None):
    """Active count/chars vs. budget for the current context, plus queue
    counters. See lessons_check for the budget constants."""
    cwd = cwd or os.getcwd()
    d = Path(d) if d else store_dir()
    repo_path, repo_name_ = lc.repo_lessons_path(cwd)
    ev = lc.evaluate(d / LESSONS_FILE, repo_path, repo_name_, approvals_path(d))
    active = lc.active_set(ev, repo_name_)
    text = lc.render_injection(active)
    n_active, n_chars = len(active), len(text)

    max_entries = lc.MAX_BUDGET_ENTRIES
    max_chars = lc.MAX_BUDGET_CHARS
    pct = max(n_active / max_entries, n_chars / max_chars)

    pending = [c for c in load_candidates(d=d) if c.get("status") == "pending"]
    rejected = load_rejected(d=d)

    return {
        "active": n_active,
        "chars": n_chars,
        "max_entries": max_entries,
        "max_chars": max_chars,
        "percent": round(pct * 100, 1),
        "near_budget": pct >= lc.NEAR_BUDGET_RATIO,
        "pending": len(pending),
        "rejected": len(rejected),
        "tweaks": len(open_tweaks(d)),
        "tweaks_file": str(tweaks_path(d)),
        "untrusted": len(ev["repo"]) - len(ev["trusted"]),
        "check_failed": bool(ev["reasons"]),
        "personal_check_failed": bool(ev["personal_reasons"]),
        "repo_check_failed": bool(ev["repo_reasons"]),
        "repo": repo_name_,
    }


# --- CLI --------------------------------------------------------------
def _print_candidate_row(rec):
    print(f"{rec.get('id')}\t{rec.get('source')}\t{rec.get('fingerprint')}\t"
          f"{rec.get('repo')}\t{rec.get('status')}\t{rec.get('key') or '-'}\t"
          f"{rec.get('text')}")


def _cmd_init(args):
    d = init()
    print(f"lessons store ready at {d}")
    return 0


def _cmd_flag(args):
    if not lessons_on():
        print("lessons disabled (MAESTRO_LESSONS=0) — correction not recorded")
        return 0
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
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{args.id} -> {args.status}")
    return 0


def _cmd_reject(args):
    try:
        rec = reject(args.key, args.rule)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"rejected: {rec['key']}")
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


def _fail(reasons):
    print("LESSONS FAIL", file=sys.stderr)
    for r in reasons:
        print(f"- {r}", file=sys.stderr)
    return 1


def _print_preview(info):
    print(f"--- exact entry to append (sha256 {info['sha']}):")
    print(info["block"], end="")


def _cmd_accept(args):
    if args.preview:
        info, reasons = accept_preview(args.rule, args.why, args.evidence,
                                       scope=args.scope or "global",
                                       supersedes=args.supersedes)
        if info is None:
            return _fail(reasons)
        _print_preview(info)
        return 0
    candidates = [c for c in (args.candidates.split(",") if args.candidates else [])
                  if c]
    lid, reasons, _ = accept(args.rule, args.why, args.evidence,
                             scope=args.scope or "global",
                             supersedes=args.supersedes, candidates=candidates,
                             expect_sha=args.sha)
    if reasons:
        return _fail(reasons)
    print(lid)
    return 0


def _cmd_publish(args):
    if args.preview:
        info, msg = publish_preview(args.id)
        if info is None:
            print(msg, file=sys.stderr)
            return 1
        _print_preview(info)
        return 0
    rid, msg = publish(args.id, expect_sha=args.sha)
    if rid is None:
        print(msg, file=sys.stderr)
        return 1
    print(rid)
    print(msg)
    return 0


def _cmd_untrusted(args):
    rows = list_untrusted()
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no untrusted repo lessons")
    for r in rows:
        print(f"--- {r['id']}  sha256 {r['sha256']}")
        print(r["raw"])
    return 0


def _cmd_trust(args):
    rid, msg = trust(args.id, args.sha)
    if rid is None:
        print(msg, file=sys.stderr)
        return 1
    print(rid)
    print(msg)
    return 0


def _print_plan(plan, d):
    d = Path(d) if d else store_dir()
    print(f"repair plan (sha256 {plan['sha']}):")
    print("rules that would be removed (uncommitted, never approved):")
    for r in plan["rules"]:
        print(f"  - {r}")
    print(f"--- exact bytes to remove from the end of {d / LESSONS_FILE}:")
    print(plan["removed"], end="" if plan["removed"].endswith("\n") else "\n")


def _cmd_repair(args):
    ok, plan, msg = repair(apply=args.apply, sha=args.sha)
    if plan is not None and plan.get("problem") is None and plan["cut"] is not None:
        _print_plan(plan, None)
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    if args.apply:
        print(msg)
    elif plan["cut"] is None:
        print("nothing to repair")
    else:
        print("Apply only after the user's explicit yes to exactly this, with:")
        print(f"  lessons.py repair --apply --sha {plan['sha']}")
    return 0


def _cmd_status(args):
    rep = status_report()
    if args.json:
        print(json.dumps(rep))
    else:
        print(f"active: {rep['active']}/{rep['max_entries']} entries, "
              f"{rep['chars']}/{rep['max_chars']} chars ({rep['percent']}%)")
        if rep["check_failed"]:
            print(f"LESSONS CHECK FAILED — run python3 \"{lc.SCRIPT_PATH}\"")
        if rep["near_budget"]:
            print("NEAR BUDGET — propose a consolidation")
        print(f"pending candidates: {rep['pending']}")
        print(f"untrusted repo lessons: {rep['untrusted']}")
        print(f"rejected rules: {rep['rejected']}")
        print(f"open maestro tweaks: {rep['tweaks']} ({rep['tweaks_file']})")
    return 0


def _cmd_tweak(args):
    cids = [c.strip() for c in (args.candidates or "").split(",") if c.strip()]
    known = {r["id"] for r in load_candidates()}
    unknown = [c for c in cids if c not in known]
    if unknown:
        print(f"unknown candidate(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    try:
        line = tweak(" ".join(args.note), cids, args.session)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(line)
    print(f"-> {tweaks_path()}")
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
    p.add_argument("--supersedes", help="one id or a comma-separated list")
    p.add_argument("--candidates")
    p.add_argument("--preview", action="store_true",
                   help="print the exact entry block and its sha256; write nothing")
    p.add_argument("--sha", help="refuse unless the appended block has this sha256")
    p.set_defaults(func=_cmd_accept)

    p = sub.add_parser("publish")
    p.add_argument("id")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--sha")
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("untrusted")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_untrusted)

    p = sub.add_parser("trust")
    p.add_argument("id")
    p.add_argument("--sha", required=True)
    p.set_defaults(func=_cmd_trust)

    p = sub.add_parser("repair")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--sha")
    p.set_defaults(func=_cmd_repair)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("tweak")
    p.add_argument("--note", required=True, nargs="+")
    p.add_argument("--candidates", help="comma-separated candidate ids")
    p.add_argument("--session")
    p.set_defaults(func=_cmd_tweak)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
