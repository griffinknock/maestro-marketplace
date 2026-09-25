#!/usr/bin/env python3
"""Maestro cross-session lessons — validator and injection formatter.

A lesson is a small, human-approved rule about how Maestro should orchestrate,
learned in one session and meant to steer every session after it. The store
is append-only by construction: nothing is ever edited or removed, a stale
rule is only ever superseded by a newer entry that names it. That guarantee
is only real if something enforces it, so this script is the gate:

  - parses the lesson file format (see below)
  - enforces per-entry and per-field size caps, so lessons stay pointers
  - enforces global uniqueness of IDs and validity of `Supersedes` chains
  - enforces a hard budget on what actually gets injected into a session
  - proves, byte-for-byte and across the full git history, that an existing
    committed entry was never edited, reordered, or deleted — only appended to

Lesson file format (markdown, optional preamble before the first entry):

  ## L-007 · scope: global
  Rule: <the rule; may wrap to one continuation line indented two spaces>
  Why: <one line>
  Evidence: <session-id prefix> · <finding fingerprint> · "<short quoted line>"
  Supersedes: L-003
  Accepted: 2026-10-02

Heading is `## <ID> · scope: <scope>`. Personal-store IDs are `L-NNN`;
per-repo file IDs are `R-NNN`. Scope is `global` or `repo:<name>`; entries in
a per-repo file must be scope `repo:<name>`. Required fields, in this order:
Rule, Why, Evidence, then optional Supersedes, then required Accepted.

Usage:
  python3 lessons_check.py [--personal PATH] [--repo PATH] [--repo-name NAME]

Defaults: personal = $MAESTRO_LESSONS_DIR/lessons.md, else
~/.claude/maestro/lessons/lessons.md. repo = <git toplevel of cwd>/.claude/
maestro-lessons.md if it exists. repo-name = basename of that toplevel.

Prints LESSONS PASS or LESSONS FAIL with one reason per line; exit 0/1.
"""
import argparse
import os
import re
import subprocess
import sys
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
NEAR_BUDGET_RATIO = 0.8

ID_RE_PERSONAL = re.compile(r"^L-\d+$")
ID_RE_REPO = re.compile(r"^R-\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADING_RE = re.compile(r"^## (\S+) · scope: (\S+)\s*$")
CONT_RE = re.compile(r"^  (\S.*)$")
FIELD_PREFIXES = ("Rule:", "Why:", "Evidence:", "Supersedes:", "Accepted:")


# --- parsing ------------------------------------------------------------

def _parse_block(block_lines, line_no):
    entry = {
        "id": None, "scope": None, "rule": None, "why": None,
        "evidence": None, "supersedes": None, "accepted": None,
        "line": line_no, "file": None,
        "content_lines": 1, "rule_line_count": 0, "errors": [],
    }
    heading = block_lines[0]
    m = HEADING_RE.match(heading)
    if not m:
        entry["errors"].append(f"malformed heading: {heading!r}")
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
        if i < len(content) and CONT_RE.match(content[i]):
            cont = CONT_RE.match(content[i]).group(1)
            rule = rule + " " + cont
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

    entry["supersedes"] = take("Supersedes:")  # optional

    accepted = take("Accepted:")
    if accepted is None:
        entry["errors"].append("missing or out-of-order Accepted field")
    entry["accepted"] = accepted

    if i != len(content):
        entry["errors"].append(f"unexpected trailing content: {content[i:]!r}")

    return entry


def parse(text):
    """Parse lesson-file markdown into a list of entry dicts.

    Keys: id, scope, rule, why, evidence, supersedes, accepted, line, file
    (plus internal bookkeeping: content_lines, rule_line_count, errors).
    `file` is always None here — callers stamp it in, since parse() only
    ever sees text, never a path.
    """
    lines = text.splitlines()
    heading_idxs = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    entries = []
    for pos, idx in enumerate(heading_idxs):
        end = heading_idxs[pos + 1] if pos + 1 < len(heading_idxs) else len(lines)
        entries.append(_parse_block(lines[idx:end], idx + 1))
    return entries


def _entries_from_file(path):
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = parse(text)
    for e in entries:
        e["file"] = str(path)
    return entries


# --- folding / active set / rendering -----------------------------------

def fold(entries):
    """id -> superseded_by id, derived from each entry's Supersedes field."""
    mapping = {}
    for e in entries:
        sup = e.get("supersedes")
        if sup:
            mapping[sup] = e["id"]
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


# --- validation -----------------------------------------------------------

def _validate_source(entries, id_re, expect_repo_scope, repo_name, fail, label):
    for e in entries:
        loc = f"{label} {e.get('id') or '<no id>'} (line {e['line']})"
        if e["errors"]:
            for err in e["errors"]:
                fail(f"{loc}: {err}")
            continue

        if not id_re.match(e["id"]):
            fail(f"{loc}: id does not match required pattern for {label} file")

        scope = e.get("scope") or ""
        if expect_repo_scope:
            if not scope.startswith("repo:"):
                fail(f"{loc}: repo-file entry must have scope repo:<name>, got {scope!r}")
            elif repo_name and scope != f"repo:{repo_name}":
                fail(f"{loc}: scope {scope!r} does not match repo name {repo_name!r}")
        elif scope != "global" and not scope.startswith("repo:"):
            fail(f"{loc}: invalid scope {scope!r}")

        for field in ("rule", "why", "evidence", "accepted"):
            if not e.get(field):
                fail(f"{loc}: missing required field {field.capitalize()}")

        if e.get("accepted") and not DATE_RE.match(e["accepted"]):
            fail(f"{loc}: Accepted is not an ISO date: {e['accepted']!r}")

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
            fail(f"duplicate ID across files: {id_}")


def _check_supersedes(all_entries, fail):
    seen = []
    targets = Counter(e["supersedes"] for e in all_entries if e.get("supersedes"))
    reported_double = set()
    for e in all_entries:
        sup = e.get("supersedes")
        if sup:
            earlier_ids = {x["id"] for x in seen}
            if sup not in earlier_ids:
                fail(f"{e['id']}: Supersedes {sup} does not reference an earlier entry")
            if targets[sup] > 1 and sup not in reported_double:
                reported_double.add(sup)
                who = [x["id"] for x in all_entries if x.get("supersedes") == sup]
                fail(f"{sup} is superseded by multiple entries: {', '.join(who)}")
        if e.get("id"):
            seen.append(e)


def _check_budget(all_entries, repo_name, fail):
    contexts = [(None, "global-only")]
    if repo_name:
        contexts.append((repo_name, f"global+{repo_name}"))
    for rn, label in contexts:
        active = active_for(all_entries, rn)
        text = render_injection(active)
        n, chars = len(active), len(text)
        if n > MAX_BUDGET_ENTRIES:
            fail(f"{label} context has {n} active entries, cap is {MAX_BUDGET_ENTRIES}")
        if chars > MAX_BUDGET_CHARS:
            fail(f"{label} context is {chars} chars, cap is {MAX_BUDGET_CHARS}")


def _git_toplevel(cwd):
    r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


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
    out of scope for this check — parse/cap/budget checks still apply to it.
    """
    top = _git_toplevel(_existing_ancestor(path))
    if top is None:
        return  # never touched a git repo
    abs_path = Path(os.path.realpath(str(path)))
    try:
        relpath = abs_path.relative_to(top).as_posix()
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
        working = path.read_bytes()
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


def check(personal, repo, repo_name):
    """Validate the personal and/or repo lesson files. Returns a list of
    failure reasons; empty list means PASS."""
    reasons = []
    fail = reasons.append

    personal_entries = _entries_from_file(personal)
    repo_entries = _entries_from_file(repo)

    _validate_source(personal_entries, ID_RE_PERSONAL, expect_repo_scope=False,
                     repo_name=None, fail=fail, label="personal")
    _validate_source(repo_entries, ID_RE_REPO, expect_repo_scope=True,
                     repo_name=repo_name, fail=fail, label="repo")

    all_entries = personal_entries + repo_entries
    _check_duplicate_ids(all_entries, fail)
    _check_supersedes(all_entries, fail)
    _check_budget(all_entries, repo_name, fail)

    if personal is not None:
        _check_append_only(personal, fail)
    if repo is not None:
        _check_append_only(repo, fail)

    return reasons


# --- CLI --------------------------------------------------------------

def _default_personal():
    d = os.environ.get("MAESTRO_LESSONS_DIR")
    if d:
        return Path(d) / "lessons.md"
    return Path.home() / ".claude" / "maestro" / "lessons" / "lessons.md"


def _default_repo():
    top = _git_toplevel(Path.cwd())
    if top is None:
        return None, None
    p = top / ".claude" / "maestro-lessons.md"
    if p.exists():
        return p, top.name
    return None, None


def _infer_repo_name(repo_path):
    top = _git_toplevel(repo_path.parent)
    if top is not None:
        return top.name
    parents = list(repo_path.parents)
    if len(parents) >= 2 and parents[0].name == ".claude":
        return parents[1].name
    return repo_path.parent.name


def main(argv):
    ap = argparse.ArgumentParser(prog="lessons_check.py")
    ap.add_argument("--personal")
    ap.add_argument("--repo")
    ap.add_argument("--repo-name")
    args = ap.parse_args(argv[1:])

    personal = Path(args.personal) if args.personal else _default_personal()

    if args.repo:
        repo = Path(args.repo)
        repo_name = args.repo_name or _infer_repo_name(repo)
    else:
        repo, default_name = _default_repo()
        repo_name = args.repo_name or default_name

    reasons = check(personal, repo, repo_name)
    if reasons:
        print("LESSONS FAIL")
        for r in reasons:
            print(f"- {r}")
        return 1

    personal_entries = _entries_from_file(personal)
    repo_entries = _entries_from_file(repo)
    all_entries = personal_entries + repo_entries

    primary = active_for(all_entries, repo_name)
    global_only = active_for(all_entries, None)
    primary_text = render_injection(primary)

    contexts = [global_only] if repo_name is None else [global_only, primary]
    near = any(
        len(ctx) >= NEAR_BUDGET_RATIO * MAX_BUDGET_ENTRIES
        or len(render_injection(ctx)) >= NEAR_BUDGET_RATIO * MAX_BUDGET_CHARS
        for ctx in contexts
    )

    print(f"LESSONS PASS — {len(primary)} active, {len(primary_text)} chars")
    if near:
        print("NEAR BUDGET — propose a consolidation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
