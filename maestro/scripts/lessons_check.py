#!/usr/bin/env python3
"""Maestro cross-session lessons — validator, trust check, injection formatter.

A lesson is a small, human-approved rule about how Maestro should orchestrate,
learned in one session and meant to steer every session after it. Nothing
becomes an active rule without Griffin's explicit yes, and a saved lesson is
never edited or removed — a stale rule is only ever superseded by a newer
entry that names it. Those guarantees are only real if something enforces
them, so this script is the gate. It:

  - parses the lesson file format (see below), splitting on "\\n" only
  - refuses symlinks, non-regular files, non-UTF-8 files, and any control or
    line-separator character (U+2028 and friends) anywhere in a lesson file
  - enforces per-entry and per-field size caps, so lessons stay pointers
  - enforces strict ASCII ids, global id uniqueness, and `Supersedes` chains
    that stay inside one file and one scope
  - enforces a hard budget on what actually gets injected — for the
    global-only context and for every repo scope present in either file
  - proves, byte-for-byte and across the full git history, that an existing
    committed entry was never edited, reordered, or deleted — only appended to
  - checks every entry against the APPROVALS LEDGER (below), which lives
    outside the store's git repo and is the external anchor for "Griffin said
    yes to exactly these bytes"

Lesson file format (markdown, optional preamble before the first entry):

  ## L-007 · scope: global
  Rule: <the rule; may wrap to one continuation line indented two spaces>
  Why: <one line>
  Evidence: <session-id prefix> · <finding fingerprint> · "<short quoted line>"
  Supersedes: L-003, L-004
  Accepted: 2026-10-02

Heading is `## <ID> · scope: <scope>`. Ids are exactly `L-NNN` (personal
store) or `R-NNN` (per-repo file), ASCII digits only. Scope is `global` or
`repo:<name>` with <name> in [A-Za-z0-9._-]; entries in a per-repo file must
be scope `repo:<name>`. Required fields, in this order: Rule, Why, Evidence,
then optional Supersedes (a comma-separated list of earlier ids from the same
file and the same scope), then required Accepted.

Approvals ledger ($MAESTRO_LESSONS_APPROVALS, else `lessons-approved.jsonl`
next to the store directory — ~/.claude/maestro/lessons-approved.jsonl by
default). One JSON line per approval:

  {"id", "tier": "personal"|"repo:<name>", "sha256": <sha256 of the entry's
   exact bytes, heading line through last field line joined by \\n>,
   "how": "accept"|"publish"|"trust", "at", ["source": <L-id, publish only>],
   "prev": <previous line's chain, "" for the first>,
   "chain": sha256(prev + id + sha256)}

Only `lessons.py accept`, `publish` and `trust` append to it. The validator
requires: the chain verifies; every personal entry has an approval with the
same id and sha (an unapproved or altered entry FAILS); every approved
personal id is still present (a deletion FAILS even if git history was
rewritten). A repo-file entry without a matching approval is UNTRUSTED: a
warning here, and never injected.

Repo identity is the basename of the MAIN working tree, resolved through
`git rev-parse --git-common-dir`, so a linked worktree shares its repo's
scope. Known limitation: two different repos with the same basename share a
scope name (and therefore each other's repo-scoped personal lessons and
trust records).

Usage:
  python3 lessons_check.py [--personal PATH] [--repo PATH] [--repo-name NAME]
                           [--approvals PATH]

Defaults: personal = $MAESTRO_LESSONS_DIR/lessons.md, else
~/.claude/maestro/lessons/lessons.md. repo = <git toplevel of cwd>/.claude/
maestro-lessons.md if it exists. repo-name = the repo identity above.

Prints LESSONS PASS or LESSONS FAIL with one reason per line, then any WARN
lines; exit 0/1.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

# --- caps -------------------------------------------------------------
MAX_ENTRY_LINES = 6            # heading + fields; blank separators don't count
MAX_RULE_LINES = 2
MAX_RULE_CHARS = 240
MAX_WHY_CHARS = 160
MAX_EVIDENCE_CHARS = 200
MAX_BUDGET_ENTRIES = 24
MAX_BUDGET_CHARS = 6000
MAX_SUPERSEDES = MAX_BUDGET_ENTRIES
NEAR_BUDGET_RATIO = 0.8

# fullmatch + explicit [0-9]: `\d` would accept fullwidth and other Unicode
# digits, and `$` would accept a trailing newline.
ID_RE_PERSONAL = re.compile(r"L-[0-9]{3}")
ID_RE_REPO = re.compile(r"R-[0-9]{3}")
ID_RE_ANY = re.compile(r"[LR]-[0-9]{3}")
SCOPE_RE = re.compile(r"global|repo:[A-Za-z0-9._-]+")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
HEADING_RE = re.compile(r"## (\S+) · scope: (\S+) *")
CONT_RE = re.compile(r"  (\S.*)")
HOWS = ("accept", "publish", "trust")

LESSONS_FILE = "lessons.md"
REPO_LESSONS_RELPATH = Path(".claude") / "maestro-lessons.md"
APPROVALS_FILE = "lessons-approved.jsonl"
SCRIPT_PATH = Path(__file__).resolve()

# Bidi/format controls that can make displayed text differ from stored text.
_SPOOF_CHARS = frozenset("\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e"
                         "\u2066\u2067\u2068\u2069\ufeff")


# --- characters -------------------------------------------------------

def forbidden_char(ch):
    """True for any character a lesson file may not contain. "\\n" is the
    only allowed control: everything str.splitlines() would also split on
    (\\r \\x0b \\x0c \\x1c-\\x1e \\x85 U+2028 U+2029), every other C0/C1
    control including tab, and bidi/format spoofing characters."""
    if ch == "\n":
        return False
    if ch in _SPOOF_CHARS:
        return True
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp", "Cs")


def clean_one_line(text, limit=None):
    """Replace every forbidden character (and newlines) with a space,
    collapse runs of whitespace, strip, and optionally truncate."""
    s = "".join(" " if (c == "\n" or forbidden_char(c)) else c
                for c in str(text or ""))
    s = " ".join(s.split())
    return s[:limit] if limit is not None else s


def entry_sha(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cap_text(text, limit=MAX_BUDGET_CHARS):
    """Hard cap on injected text: whole lines up to `limit` chars."""
    if text is None or len(text) <= limit:
        return text
    marker = "… (truncated at the lessons budget)"
    cut = text[: max(0, limit - len(marker) - 1)]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return (cut + "\n" + marker)[:limit]


# --- parsing ------------------------------------------------------------

def _parse_block(block_lines, line_no):
    entry = {
        "id": None, "scope": None, "rule": None, "why": None,
        "evidence": None, "supersedes": None, "supersedes_ids": [],
        "accepted": None, "line": line_no, "file": None,
        "content_lines": 1, "rule_line_count": 0, "errors": [],
        "raw": None, "sha256": None,
    }
    last = 0
    for k, ln in enumerate(block_lines):
        if ln.strip() != "":
            last = k
    entry["raw"] = "\n".join(block_lines[: last + 1])
    entry["sha256"] = entry_sha(entry["raw"])

    m = HEADING_RE.fullmatch(block_lines[0])
    if not m:
        entry["errors"].append("malformed heading")
        return entry
    entry["id"], entry["scope"] = m.group(1), m.group(2)

    content = [ln for ln in block_lines[1:] if ln.strip() != ""]
    entry["content_lines"] = 1 + len(content)

    i = 0

    def take(prefix):
        nonlocal i
        if i < len(content) and content[i].startswith(prefix):
            val = content[i][len(prefix):].strip()
            i += 1
            return val
        return None

    rule = take("Rule:")
    if rule is not None:
        entry["rule_line_count"] = 1
        if i < len(content) and CONT_RE.fullmatch(content[i]):
            rule = rule + " " + CONT_RE.fullmatch(content[i]).group(1)
            entry["rule_line_count"] = 2
            i += 1
    else:
        entry["errors"].append("missing or out-of-order Rule field")
    entry["rule"] = rule

    why = take("Why:")
    if why is None:
        entry["errors"].append("missing or out-of-order Why field")
    entry["why"] = why

    evidence = take("Evidence:")
    if evidence is None:
        entry["errors"].append("missing or out-of-order Evidence field")
    entry["evidence"] = evidence

    sup = take("Supersedes:")  # optional
    entry["supersedes"] = sup
    if sup is not None:
        entry["supersedes_ids"] = [s.strip() for s in sup.split(",")]

    accepted = take("Accepted:")
    if accepted is None:
        entry["errors"].append("missing or out-of-order Accepted field")
    entry["accepted"] = accepted

    if i != len(content):
        entry["errors"].append(
            f"unexpected trailing content ({len(content) - i} line(s))")

    return entry


def parse(text):
    """Parse lesson-file markdown into a list of entry dicts.

    Splits on "\\n" ONLY — never str.splitlines(), which also splits on
    U+2028, \\x1c and friends and so would let a rule smuggle a heading.

    Keys: id, scope, rule, why, evidence, supersedes (raw string),
    supersedes_ids (list), accepted, line, file, raw (the exact entry text,
    heading through last field line), sha256 (of raw), plus internal
    bookkeeping (content_lines, rule_line_count, errors). `file` is always
    None here — callers stamp it in, since parse() only sees text.
    """
    lines = (text or "").split("\n")
    heading_idxs = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    entries = []
    for pos, idx in enumerate(heading_idxs):
        end = heading_idxs[pos + 1] if pos + 1 < len(heading_idxs) else len(lines)
        entries.append(_parse_block(lines[idx:end], idx + 1))
    return entries


def _is_regular_nolink(path):
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _read_lesson_file(path, label, fail):
    """Text of a lesson file, or None when absent or refused. Refuses (and
    reports) symlinks, non-regular files, a symlinked `.claude` directory,
    undecodable bytes, and forbidden characters — never echoing content."""
    if path is None:
        return None
    p = Path(path)
    if not os.path.lexists(str(p)):
        return None
    if p.parent.name == ".claude" and p.parent.is_symlink():
        fail(f"{label} lessons directory {p.parent} is a symlink — refusing to read it")
        return None
    if p.is_symlink():
        fail(f"{label} lessons file {p} is a symlink — refusing to read it")
        return None
    if not _is_regular_nolink(p):
        fail(f"{label} lessons file {p} is not a regular file — refusing to read it")
        return None
    try:
        data = p.read_bytes()
    except OSError as e:
        fail(f"{label} lessons file {p} is unreadable ({e.__class__.__name__})")
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{label} lessons file {p} is not valid UTF-8")
        return None
    bad = [(n, ch) for n, ln in enumerate(text.split("\n"), 1)
           for ch in ln if forbidden_char(ch)]
    if bad:
        n, ch = bad[0]
        fail(f"{label} lessons file contains a forbidden control/line-separator "
             f"character U+{ord(ch):04X} on line {n} ({len(bad)} in total)")
        return None
    return text


def _entries_from_file(path):
    """Best-effort parse for display; validation goes through evaluate()."""
    text = _read_lesson_file(path, "", lambda _r: None)
    entries = parse(text) if text else []
    for e in entries:
        e["file"] = str(path)
    return entries


# --- folding / active set / rendering -----------------------------------

def fold(entries):
    """id -> superseded_by id, from each entry's Supersedes list. Only a
    same-scope, same-kind (L- to L-, R- to R-) target counts — a Supersedes
    that crosses scopes or tiers retires nothing, even before validation
    rejects it."""
    by_id = {e["id"]: e for e in entries if e.get("id")}
    mapping = {}
    for e in entries:
        eid = e.get("id") or ""
        for sid in e.get("supersedes_ids") or []:
            t = by_id.get(sid)
            if t is None or t is e:
                continue
            if t.get("scope") != e.get("scope") or sid[:2] != eid[:2]:
                continue
            mapping.setdefault(sid, eid)
    return mapping


def active_for(entries, repo_name):
    """Active (non-superseded) entries in scope for repo_name (or global-only
    if repo_name is None), in file order."""
    superseded_ids = set(fold(entries).keys())
    wanted_scope = f"repo:{repo_name}" if repo_name else None
    out = []
    for e in entries:
        if not e.get("id") or e["id"] in superseded_ids:
            continue
        if e.get("scope") == "global" or (wanted_scope and e.get("scope") == wanted_scope):
            out.append(e)
    return out


def render_injection(entries):
    """Exact text the SessionStart hook injects for this entry set."""
    lines = [
        f"MAESTRO LESSONS — {len(entries)} active. "
        "Rules learned from past sessions; follow them."
    ]
    for e in entries:
        rule = " ".join((e.get("rule") or "").split())
        lines.append(f"- {e['id']}: {rule}")
    return "\n".join(lines)


# --- approvals ledger -----------------------------------------------------

def default_store_dir():
    override = os.environ.get("MAESTRO_LESSONS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "maestro" / "lessons"


def approvals_path(store=None):
    """$MAESTRO_LESSONS_APPROVALS, else `lessons-approved.jsonl` next to the
    store directory (outside the store's git repo)."""
    override = os.environ.get("MAESTRO_LESSONS_APPROVALS")
    if override:
        return Path(override)
    return Path(store if store else default_store_dir()).parent / APPROVALS_FILE


def chain_of(prev, id_, sha):
    return hashlib.sha256((prev + id_ + sha).encode("utf-8")).hexdigest()


def make_approval(prev, id_, tier, sha, how, **extra):
    rec = {"id": id_, "tier": tier, "sha256": sha, "how": how,
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rec.update(extra)
    rec["prev"] = prev
    rec["chain"] = chain_of(prev, id_, sha)
    return rec


def ledger_tail(records):
    return records[-1]["chain"] if records else ""


def _valid_record(rec):
    if not isinstance(rec, dict):
        return False
    if not all(isinstance(rec.get(k), str)
               for k in ("id", "tier", "sha256", "how", "prev", "chain")):
        return False
    tier = rec["tier"]
    tier_ok = tier == "personal" or (tier.startswith("repo:") and SCOPE_RE.fullmatch(tier))
    return bool(tier_ok
                and ID_RE_ANY.fullmatch(rec["id"])
                and SHA_RE.fullmatch(rec["sha256"])
                and rec["how"] in HOWS
                and (tier == "personal") == rec["id"].startswith("L-")
                and (tier == "personal") == (rec["how"] == "accept"))


def read_approvals(path):
    """(records, reasons). A missing ledger is an empty one; anything that
    does not verify line by line is a reason."""
    if path is None or not os.path.lexists(str(path)):
        return [], []
    p = Path(path)
    if p.is_symlink() or not _is_regular_nolink(p):
        return [], [f"approvals ledger {p} is a symlink or not a regular file"]
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return [], [f"approvals ledger {p} is unreadable or not UTF-8"]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    records, prev = [], ""
    for n, line in enumerate(lines, 1):
        where = f"approvals ledger {p} line {n}"
        try:
            rec = json.loads(line)
        except ValueError:
            return records, [f"{where}: not JSON — ledger chain broken"]
        if not _valid_record(rec):
            return records, [f"{where}: malformed approval record — ledger chain broken"]
        if rec["prev"] != prev or rec["chain"] != chain_of(prev, rec["id"], rec["sha256"]):
            return records, [f"{where}: hash chain does not verify — the ledger "
                             "was edited, reordered, or cut in the middle"]
        prev = rec["chain"]
        records.append(rec)
    return records, []


# --- validation -----------------------------------------------------------

def _safe_id(x):
    return x if isinstance(x, str) and ID_RE_ANY.fullmatch(x) else "<malformed id>"


def _validate_source(entries, id_re, expect_repo_scope, repo_name, fail, label):
    for e in entries:
        loc = f"{label} {_safe_id(e.get('id'))} (line {e['line']})"
        if e["errors"]:
            for err in e["errors"]:
                fail(f"{loc}: {err}")
            continue

        if not id_re.fullmatch(e["id"]):
            fail(f"{loc}: id must be exactly "
                 f"{'L' if id_re is ID_RE_PERSONAL else 'R'}-NNN (ASCII digits) "
                 f"in a {label} file")

        scope = e.get("scope") or ""
        if not SCOPE_RE.fullmatch(scope):
            fail(f"{loc}: invalid scope (want global or repo:<name>, name in [A-Za-z0-9._-])")
        elif expect_repo_scope:
            if not scope.startswith("repo:"):
                fail(f"{loc}: repo-file entry must have scope repo:<name>, not global")
            elif repo_name and scope != f"repo:{repo_name}":
                fail(f"{loc}: scope does not match repo name {repo_name!r}")

        for field in ("rule", "why", "evidence", "accepted"):
            if not e.get(field):
                fail(f"{loc}: missing required field {field.capitalize()}")

        if e.get("accepted") and not DATE_RE.fullmatch(e["accepted"]):
            fail(f"{loc}: Accepted is not an ISO date")

        if e["content_lines"] > MAX_ENTRY_LINES:
            fail(f"{loc}: entry is {e['content_lines']} lines, cap is {MAX_ENTRY_LINES}")
        if e.get("rule") is not None:
            if e["rule_line_count"] > MAX_RULE_LINES:
                fail(f"{loc}: Rule spans {e['rule_line_count']} lines, cap is {MAX_RULE_LINES}")
            if len(e["rule"]) > MAX_RULE_CHARS:
                fail(f"{loc}: Rule is {len(e['rule'])} chars, cap is {MAX_RULE_CHARS}")
        if e.get("why") and len(e["why"]) > MAX_WHY_CHARS:
            fail(f"{loc}: Why is {len(e['why'])} chars, cap is {MAX_WHY_CHARS}")
        if e.get("evidence") and len(e["evidence"]) > MAX_EVIDENCE_CHARS:
            fail(f"{loc}: Evidence is {len(e['evidence'])} chars, cap is {MAX_EVIDENCE_CHARS}")


def _check_duplicate_ids(all_entries, fail):
    ids = [e["id"] for e in all_entries if e.get("id")]
    for id_, cnt in Counter(ids).items():
        if cnt > 1:
            fail(f"duplicate ID across files: {_safe_id(id_)}")


def _check_supersedes(entries, id_re, label, fail):
    """Within ONE file: every Supersedes target is an earlier entry of the
    same kind and the same scope, and nothing is superseded twice."""
    kind = "L-" if id_re is ID_RE_PERSONAL else "R-"
    seen, retired_by = {}, {}
    for e in entries:
        eid = e.get("id")
        if e.get("supersedes") is not None and eid:
            loc = f"{label} {_safe_id(eid)}"
            ids = e.get("supersedes_ids") or []
            if not ids or any(not id_re.fullmatch(s) for s in ids):
                fail(f"{loc}: Supersedes must list {kind}NNN ids from the same "
                     "file, comma-separated")
            elif len(set(ids)) != len(ids):
                fail(f"{loc}: Supersedes names the same id twice")
            elif len(ids) > MAX_SUPERSEDES:
                fail(f"{loc}: Supersedes names {len(ids)} ids, cap is {MAX_SUPERSEDES}")
            else:
                for s in ids:
                    t = seen.get(s)
                    if t is None:
                        fail(f"{loc}: Supersedes {s} does not reference an earlier "
                             "entry in the same file")
                    elif t.get("scope") != e.get("scope"):
                        fail(f"{loc}: Supersedes {s} crosses scopes — a lesson may "
                             "only supersede one with the same scope")
                    elif s in retired_by:
                        fail(f"{s} is superseded by multiple entries: "
                             f"{retired_by[s]}, {_safe_id(eid)}")
                    else:
                        retired_by[s] = _safe_id(eid)
        if eid and id_re.fullmatch(eid):
            seen[eid] = e


def _budget_contexts(entries, repo_name):
    names = set()
    for e in entries:
        sc = e.get("scope") or ""
        if sc.startswith("repo:") and SCOPE_RE.fullmatch(sc):
            names.add(sc[len("repo:"):])
    if repo_name:
        names.add(repo_name)
    return [None] + sorted(names)


def _check_budget(pool, repo_name, fail):
    """Budget for the global-only context AND for every repo scope present
    in either file (plus the current repo) — never just the current one."""
    for rn in _budget_contexts(pool, repo_name):
        label = "global-only" if rn is None else f"global+repo:{rn}"
        active = active_for(pool, rn)
        n, chars = len(active), len(render_injection(active))
        if n > MAX_BUDGET_ENTRIES:
            fail(f"{label} context has {n} active entries, cap is {MAX_BUDGET_ENTRIES}")
        if chars > MAX_BUDGET_CHARS:
            fail(f"{label} context is {chars} chars, cap is {MAX_BUDGET_CHARS}")


def _check_approvals(personal_entries, check_personal, repo_entries, repo_name,
                     records, fail, warn):
    """Personal entries must match an approval exactly; approved personal ids
    must still exist. Returns the repo entries that are trusted."""
    if check_personal:
        by_id = {}
        for r in records:
            if r["tier"] != "personal":
                continue
            if r["id"] in by_id:
                fail(f"approvals ledger approves {r['id']} more than once")
            by_id[r["id"]] = r
        present = set()
        for e in personal_entries:
            eid = e.get("id")
            if e["errors"] or not eid or not ID_RE_PERSONAL.fullmatch(eid):
                continue
            present.add(eid)
            r = by_id.get(eid)
            if r is None:
                fail(f"personal {eid}: no approval in the ledger — added outside "
                     "`lessons.py accept`, so nobody said yes to it")
            elif r["sha256"] != e["sha256"]:
                fail(f"personal {eid}: content differs from what was approved "
                     "(edited after approval)")
        for rid in by_id:
            if rid not in present:
                fail(f"approved lesson {rid} is missing from the personal lessons "
                     "file — deleted or rewritten")

    tier = f"repo:{repo_name}" if repo_name else None
    trusted = []
    for e in repo_entries:
        eid = e.get("id")
        if e["errors"] or not eid or not ID_RE_REPO.fullmatch(eid):
            continue
        ok = tier is not None and any(
            r["tier"] == tier and r["id"] == eid and r["sha256"] == e["sha256"]
            for r in records)
        if ok:
            trusted.append(e)
        else:
            warn(f"repo {eid}: untrusted — you never approved this exact entry, "
                 "so it is not injected; review with /maestro:lessons")
    return trusted


def _git_toplevel(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


def repo_identity(cwd=None):
    """(worktree toplevel, repo name) for `cwd`, or (None, None) outside a
    repo. The name is the basename of the MAIN working tree, found through
    `--git-common-dir`, so every linked worktree of a repo shares its scope.
    Known limitation: different repos with the same basename collide."""
    cwd = str(cwd or os.getcwd())
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel",
                            "--git-common-dir"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if r.returncode != 0:
        return None, None
    lines = r.stdout.split("\n")
    if len(lines) < 2 or not lines[0] or not lines[1]:
        return None, None
    top = Path(lines[0])
    common = Path(lines[1])
    if not common.is_absolute():
        common = Path(cwd) / common
    common = Path(os.path.realpath(str(common)))
    if common.name == ".git":
        name = common.parent.name
    elif common.name.endswith(".git") and len(common.name) > 4:
        name = common.name[:-4]           # bare repo: foo.git
    else:
        name = common.name                # submodule: .git/modules/<name>
    return top, (name or None)


def repo_lessons_path(cwd=None):
    """(repo lessons path or None, repo name or None) for `cwd`. The path is
    the checked-out `.claude/maestro-lessons.md` of cwd's worktree, returned
    whenever anything exists there (even a symlink — the validator refuses
    it rather than skipping it)."""
    top, name = repo_identity(cwd)
    if top is None:
        return None, None
    p = top / REPO_LESSONS_RELPATH
    return (p if os.path.lexists(str(p)) else None), name


def _existing_ancestor(path):
    """Nearest existing directory above `path`, for use as a `git -C`
    argument even when `path` itself does not (or no longer) exist on disk.
    `git -C` needs a directory, so this always starts from the parent —
    never returns `path` itself, even if `path` happens to be a directory
    that exists."""
    p = Path(os.path.realpath(str(path))).parent
    while not p.exists():
        parent = p.parent
        if parent == p:
            return p  # filesystem root; give up gracefully
        p = parent
    return p


def _check_append_only(path, fail):
    """A lesson file's committed history, plus its current working content,
    must form a strictly-increasing chain of byte prefixes. Any edit,
    delete, reorder, or whitespace/line-ending change of existing bytes
    breaks the chain; appending never does. Deleting the file — from the
    working tree, from HEAD, or anywhere in the middle of its history (even
    if something is later recreated at the same path) — is exactly the kind
    of "erase what was accepted" move this check exists to catch, so it is
    always a break, never treated as a fresh start.

    A path with no git history at all (never committed, nothing to lose) is
    out of scope for this check — the approvals ledger still anchors it.
    """
    top = _git_toplevel(_existing_ancestor(path))
    if top is None:
        return  # never touched a git repo
    abs_path = Path(os.path.realpath(str(path)))
    try:
        relpath = abs_path.relative_to(Path(os.path.realpath(str(top)))).as_posix()
    except ValueError:
        return

    log = subprocess.run(
        ["git", "-C", str(top), "log", "--format=%H", "--reverse", "--", relpath],
        capture_output=True, text=True)
    hashes = [h for h in log.stdout.split("\n") if h]
    if not hashes:
        return  # no history for this path — untracked/never existed

    contents = []  # (label, bytes | None); None means absent at that step
    for h in hashes:
        show = subprocess.run(["git", "-C", str(top), "show", f"{h}:{relpath}"],
                              capture_output=True)
        contents.append((h, show.stdout if show.returncode == 0 else None))

    try:
        working = path.read_bytes() if _is_regular_nolink(path) else None
    except OSError:
        working = None
    contents.append(("WORKING", working))

    # Currently missing (working tree or HEAD) despite having history.
    n = len(contents)
    idx = n - 1
    while idx >= 0 and contents[idx][1] is None:
        idx -= 1
    trailing_start = idx + 1
    if trailing_start < n:
        label0 = contents[trailing_start][0]
        if label0 == "WORKING":
            fail(f"append-only violation: {relpath} has git history but is "
                 "missing from the working copy")
        else:
            fail(f"append-only violation: commit {label0[:7]} deleted "
                 f"{relpath} and it was never recreated")
        return

    # Deleted, then recreated, somewhere in the middle of history — a break
    # regardless of whether the recreated bytes happen to differ.
    for h, c in contents[:-1]:
        if c is None:
            fail(f"append-only violation: commit {h[:7]} deleted {relpath}; "
                 "it was later recreated, which is not a pure append")
            return

    for (h_prev, c_prev), (h_next, c_next) in zip(contents, contents[1:]):
        if not c_next.startswith(c_prev):
            if h_next == "WORKING":
                fail(f"append-only violation: uncommitted edit to {relpath} "
                     "is not a pure append")
            else:
                fail(f"append-only violation: commit {h_next[:7]} edited "
                     f"existing content in {relpath}")
            return


def evaluate(personal, repo, repo_name, approvals=None, extra_approvals=()):
    """Full validation. Returns a dict:
      reasons   failure reasons (empty = PASS)
      warnings  non-fatal findings (untrusted repo entries)
      personal  parsed personal entries
      repo      parsed repo-file entries
      trusted   repo entries with a matching approval (the only repo entries
                that may ever be injected)
    `extra_approvals` are records validated as if already appended — used by
    accept/publish/trust to check the post-state before writing the ledger.
    """
    reasons, warnings = [], []
    fail, warn = reasons.append, warnings.append

    ptext = _read_lesson_file(personal, "personal", fail)
    rtext = _read_lesson_file(repo, "repo", fail)
    personal_entries = parse(ptext) if ptext else []
    repo_entries = parse(rtext) if rtext else []
    for e in personal_entries:
        e["file"] = str(personal)
    for e in repo_entries:
        e["file"] = str(repo)

    _validate_source(personal_entries, ID_RE_PERSONAL, expect_repo_scope=False,
                     repo_name=None, fail=fail, label="personal")
    _validate_source(repo_entries, ID_RE_REPO, expect_repo_scope=True,
                     repo_name=repo_name, fail=fail, label="repo")

    _check_duplicate_ids(personal_entries + repo_entries, fail)
    _check_supersedes(personal_entries, ID_RE_PERSONAL, "personal", fail)
    _check_supersedes(repo_entries, ID_RE_REPO, "repo", fail)

    ap = Path(approvals) if approvals else approvals_path()
    records, ledger_reasons = read_approvals(ap)
    reasons.extend(ledger_reasons)
    records = records + list(extra_approvals)
    trusted = _check_approvals(personal_entries, personal is not None,
                               repo_entries, repo_name, records, fail, warn)

    _check_budget(personal_entries + trusted, repo_name, fail)

    for p in (personal, repo):
        if p is None:
            continue
        p = Path(p)
        if os.path.lexists(str(p)) and not _is_regular_nolink(p):
            continue  # already refused above; never read through it
        if p.parent.name == ".claude" and p.parent.is_symlink():
            continue
        _check_append_only(p, fail)

    return {"reasons": reasons, "warnings": warnings,
            "personal": personal_entries, "repo": repo_entries,
            "trusted": trusted}


def check(personal, repo, repo_name, approvals=None):
    """Validate the personal and/or repo lesson files. Returns a list of
    failure reasons; empty list means PASS."""
    return evaluate(personal, repo, repo_name, approvals)["reasons"]


# --- CLI --------------------------------------------------------------

def _default_personal():
    return default_store_dir() / LESSONS_FILE


def _infer_repo_name(repo_path):
    _, name = repo_identity(_existing_ancestor(repo_path))
    if name is not None:
        return name
    parents = list(repo_path.parents)
    if len(parents) >= 2 and parents[0].name == ".claude":
        return parents[1].name
    return repo_path.parent.name


def main(argv):
    ap = argparse.ArgumentParser(prog="lessons_check.py")
    ap.add_argument("--personal")
    ap.add_argument("--repo")
    ap.add_argument("--repo-name")
    ap.add_argument("--approvals")
    args = ap.parse_args(argv[1:])

    personal = Path(args.personal) if args.personal else _default_personal()

    if args.repo:
        repo = Path(args.repo)
        repo_name = args.repo_name or _infer_repo_name(repo)
    else:
        repo, default_name = repo_lessons_path(Path.cwd())
        repo_name = args.repo_name or default_name

    approvals = Path(args.approvals) if args.approvals else approvals_path()
    res = evaluate(personal, repo, repo_name, approvals)
    if res["reasons"]:
        print("LESSONS FAIL")
        for r in res["reasons"]:
            print(f"- {r}")
        for w in res["warnings"]:
            print(f"WARN - {w}")
        return 1

    pool = res["personal"] + res["trusted"]
    primary = active_for(pool, repo_name)
    primary_text = render_injection(primary)
    near = any(
        len(active_for(pool, rn)) >= NEAR_BUDGET_RATIO * MAX_BUDGET_ENTRIES
        or len(render_injection(active_for(pool, rn))) >= NEAR_BUDGET_RATIO * MAX_BUDGET_CHARS
        for rn in _budget_contexts(pool, repo_name)
    )

    print(f"LESSONS PASS — {len(primary)} active, {len(primary_text)} chars")
    for w in res["warnings"]:
        print(f"WARN - {w}")
    if near:
        print("NEAR BUDGET — propose a consolidation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
