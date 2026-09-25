#!/usr/bin/env python3
"""Focused checks for maestro/scripts/lessons_check.py: the cross-session
lessons validator and injection formatter.

    python3 tests/lessons_units.py

Exit code is 0 when every check passes.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maestro" / "scripts"))

import lessons_check as lc

FAILURES = []
SCRIPT = Path(__file__).resolve().parent.parent / "maestro" / "scripts" / "lessons_check.py"


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def entry(id_, scope, rule="Do the thing.", why="Because it broke once.",
          evidence='ab12cd34 · fail:builder:2 · "quote"',
          supersedes=None, accepted="2026-10-02"):
    lines = [f"## {id_} · scope: {scope}", f"Rule: {rule}", f"Why: {why}",
             f"Evidence: {evidence}"]
    if supersedes:
        lines.append(f"Supersedes: {supersedes}")
    lines.append(f"Accepted: {accepted}")
    return "\n".join(lines)


def write_lessons(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(entries) + "\n")
    return path


def git_repo():
    ws = Path(tempfile.mkdtemp(prefix="maestro-lessons-"))
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"], check=True)
    return ws


def commit_all(ws, msg):
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "-c", "user.email=t@example.com",
                    "-c", "user.name=t", "commit", "-qm", msg], check=True)


def run_cli(personal=None, repo=None, repo_name=None, cwd=None):
    args = [sys.executable, str(SCRIPT)]
    if personal is not None:
        args += ["--personal", str(personal)]
    if repo is not None:
        args += ["--repo", str(repo)]
    if repo_name is not None:
        args += ["--repo-name", repo_name]
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


# --- parse / fold / active_for / render_injection --------------------------

def parse_case():
    print("\n=== parse / fold / active_for / render_injection ===")
    text = "\n\n".join([
        entry("L-001", "global", rule="Rule wraps here\n  and continues."),
        entry("L-002", "global", supersedes="L-001", accepted="2026-10-03"),
    ])
    entries = lc.parse(text)
    check("two entries parsed", len(entries) == 2, str(len(entries)))
    check("wrapped rule joined to one line",
          entries[0]["rule"] == "Rule wraps here and continues.",
          repr(entries[0]["rule"]))
    check("rule_line_count reflects wrap", entries[0]["rule_line_count"] == 2)
    check("scope parsed", entries[0]["scope"] == "global")
    check("supersedes parsed", entries[1]["supersedes"] == "L-001")
    check("file defaults to None", entries[0]["file"] is None)

    folded = lc.fold(entries)
    check("fold maps superseded id", folded.get("L-001") == "L-002")

    active = lc.active_for(entries, None)
    check("active excludes superseded", [e["id"] for e in active] == ["L-002"])

    rendered = lc.render_injection(active)
    check("render header names count", rendered.startswith("MAESTRO LESSONS — 1 active."))
    check("render lists id and rule", "- L-002:" in rendered)


# --- basic pass ------------------------------------------------------------

def valid_file_case():
    print("\n=== valid file passes ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global"),
        entry("L-002", "global", supersedes="L-001", accepted="2026-10-03"),
    ])
    commit_all(ws, "add lessons")
    reasons = lc.check(personal, None, None)
    check("valid file has no reasons", reasons == [], "; ".join(reasons))
    r = run_cli(personal=personal, cwd=ws)
    check("cli passes", r.returncode == 0 and "LESSONS PASS" in r.stdout, r.stdout + r.stderr)
    shutil.rmtree(ws, ignore_errors=True)


def missing_personal_case():
    print("\n=== missing personal file is a pass ===")
    ws = git_repo()
    r = run_cli(personal=ws / "nope" / "lessons.md", cwd=ws)
    check("missing personal passes with 0 active", r.returncode == 0 and "0 active" in r.stdout,
          r.stdout + r.stderr)
    shutil.rmtree(ws, ignore_errors=True)


# --- append-only guarantee --------------------------------------------------

def uncommitted_edit_case():
    print("\n=== uncommitted edit of existing entry fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    text = personal.read_text()
    personal.write_text(text.replace("Do the thing.", "Do the other thing."))
    reasons = lc.check(personal, None, None)
    check("edit detected", any("append-only" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def deletion_case():
    print("\n=== deletion of existing entry fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global"), entry("L-002", "global", accepted="2026-10-03"),
    ])
    commit_all(ws, "add")
    lines = personal.read_text().splitlines(keepends=True)
    personal.write_text("".join(lines[:6]))  # drop the second entry
    reasons = lc.check(personal, None, None)
    check("deletion detected", any("append-only" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def reorder_case():
    print("\n=== reorder of existing entries fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global"), entry("L-002", "global", accepted="2026-10-03"),
    ])
    commit_all(ws, "add")
    e1 = entry("L-001", "global")
    e2 = entry("L-002", "global", accepted="2026-10-03")
    personal.write_text(e2 + "\n\n" + e1 + "\n")
    reasons = lc.check(personal, None, None)
    check("reorder detected", any("append-only" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def crlf_case():
    print("\n=== CRLF conversion of existing content fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    raw = personal.read_bytes().replace(b"\n", b"\r\n")
    personal.write_bytes(raw)
    reasons = lc.check(personal, None, None)
    check("CRLF conversion detected", any("append-only" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def uncommitted_file_delete_case():
    print("\n=== uncommitted delete of the file itself fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    personal.unlink()
    reasons = lc.check(personal, None, None)
    check("uncommitted file delete detected",
          any("append-only" in r and "missing from the working copy" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def committed_file_delete_case():
    print("\n=== committed delete of the file fails, names the commit ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    subprocess.run(["git", "-C", str(ws), "rm", "-q", "lessons.md"], check=True)
    commit_all(ws, "remove lessons file")
    reasons = lc.check(personal, None, None)
    check("committed file delete names the deleting commit",
          any("append-only" in r and "deleted" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def delete_then_recreate_case():
    print("\n=== delete-then-recreate with fewer entries fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global"), entry("L-002", "global", accepted="2026-10-03"),
    ])
    commit_all(ws, "add")
    subprocess.run(["git", "-C", str(ws), "rm", "-q", "lessons.md"], check=True)
    commit_all(ws, "remove")
    write_lessons(personal, [entry("L-003", "global", accepted="2026-10-04")])
    commit_all(ws, "recreate with fewer entries")
    reasons = lc.check(personal, None, None)
    check("delete-then-recreate detected",
          any("later recreated" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def git_mv_away_new_file_case():
    print("\n=== git mv away + new file at old path fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    subprocess.run(["git", "-C", str(ws), "mv", "lessons.md", "lessons-old.md"], check=True)
    commit_all(ws, "rename away")
    write_lessons(personal, [entry("L-002", "global", accepted="2026-10-03")])
    commit_all(ws, "new unrelated file at old path")
    reasons = lc.check(personal, None, None)
    check("rename-away then new file at old path detected",
          any("append-only" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def committed_edit_case():
    print("\n=== committed edit fails via history walk, names the commit ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    bad_sha = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()[:7]
    text = personal.read_text().replace("Do the thing.", "Something else entirely.")
    personal.write_text(text)
    commit_all(ws, "sneaky edit")
    # append a legit new entry after the sneaky edit, then check again
    reasons = lc.check(personal, None, None)
    named = any("append-only" in r and (bad_sha in r or "commit" in r) for r in reasons)
    check("history-walk edit detected and reason present", named, "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def append_passes_case():
    print("\n=== a pure append passes ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    commit_all(ws, "add")
    with personal.open("a") as f:
        f.write("\n" + entry("L-002", "global", accepted="2026-10-03") + "\n")
    reasons = lc.check(personal, None, None)
    check("append is clean", reasons == [], "; ".join(reasons))
    # uncommitted append (working ahead of HEAD) should also pass
    with personal.open("a") as f:
        f.write("\n" + entry("L-003", "global", accepted="2026-10-04") + "\n")
    reasons2 = lc.check(personal, None, None)
    check("uncommitted append is clean too", reasons2 == [], "; ".join(reasons2))
    shutil.rmtree(ws, ignore_errors=True)


def untracked_file_case():
    print("\n=== untracked file skips append-only, still gets cap checks ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global", rule="x" * 500)])
    # never committed -> untracked
    reasons = lc.check(personal, None, None)
    check("untracked file not append-only-checked but still cap-checked",
          any("Rule is" in r for r in reasons) and not any("append-only" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


# --- structural validation ---------------------------------------------

def duplicate_id_case():
    print("\n=== duplicate ID across files fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global")])
    repo = write_lessons(ws / "repo-lessons.md", [entry("L-001", "repo:career-ops")])
    reasons = lc.check(personal, repo, "career-ops")
    check("duplicate id named", any("duplicate ID" in r and "L-001" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def supersedes_unknown_case():
    print("\n=== Supersedes unknown reference fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global", supersedes="L-999"),
    ])
    reasons = lc.check(personal, None, None)
    check("unknown supersedes named", any("L-999" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def supersedes_forward_case():
    print("\n=== Supersedes forward reference fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global", supersedes="L-002"),
        entry("L-002", "global", accepted="2026-10-03"),
    ])
    reasons = lc.check(personal, None, None)
    check("forward reference rejected", any("L-002" in r and "earlier" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def double_supersession_case():
    print("\n=== double supersession fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [
        entry("L-001", "global"),
        entry("L-002", "global", supersedes="L-001", accepted="2026-10-03"),
        entry("L-003", "global", supersedes="L-001", accepted="2026-10-04"),
    ])
    reasons = lc.check(personal, None, None)
    check("double supersession named", any("superseded by multiple" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def repo_scope_mismatch_case():
    print("\n=== repo-file entry with global scope is rejected ===")
    ws = git_repo()
    repo = write_lessons(ws / "repo-lessons.md", [entry("R-001", "global")])
    reasons = lc.check(None, repo, "career-ops")
    check("global scope in repo file rejected",
          any("R-001" in r and "repo:<name>" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def missing_evidence_case():
    print("\n=== missing Evidence field fails ===")
    ws = git_repo()
    text = "\n".join([
        "## L-001 · scope: global",
        "Rule: Do the thing.",
        "Why: Because it broke once.",
        "Accepted: 2026-10-02",
    ])
    personal = ws / "lessons.md"
    personal.write_text(text + "\n")
    reasons = lc.check(personal, None, None)
    check("missing evidence named", any("Evidence" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def rule_too_long_case():
    print("\n=== Rule over char cap fails ===")
    ws = git_repo()
    personal = write_lessons(ws / "lessons.md", [entry("L-001", "global", rule="x" * 300)])
    reasons = lc.check(personal, None, None)
    check("rule length cap enforced", any("Rule is" in r for r in reasons), "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


# --- budget ---------------------------------------------------------------

def over_budget_count_case():
    print("\n=== over budget on entry count fails ===")
    ws = git_repo()
    entries = [entry(f"L-{i:03d}", "global", accepted="2026-10-02") for i in range(1, 26)]
    personal = write_lessons(ws / "lessons.md", entries)
    reasons = lc.check(personal, None, None)
    check("entry-count cap enforced", any("active entries" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def over_budget_chars_case():
    print("\n=== over budget on chars fails ===")
    ws = git_repo()
    entries = [entry(f"L-{i:03d}", "global", rule="x" * 240, accepted="2026-10-02")
               for i in range(1, 25)]
    personal = write_lessons(ws / "lessons.md", entries)
    reasons = lc.check(personal, None, None)
    check("char-budget cap enforced", any("chars" in r and "cap is" in r for r in reasons),
          "; ".join(reasons))
    shutil.rmtree(ws, ignore_errors=True)


def near_budget_case():
    print("\n=== near-budget prints a warning but still passes ===")
    ws = git_repo()
    entries = [entry(f"L-{i:03d}", "global", accepted="2026-10-02") for i in range(1, 21)]
    personal = write_lessons(ws / "lessons.md", entries)
    commit_all(ws, "add")
    r = run_cli(personal=personal, cwd=ws)
    check("still passes at 20/24 entries", r.returncode == 0 and "LESSONS PASS" in r.stdout,
          r.stdout + r.stderr)
    check("near-budget line printed", "NEAR BUDGET" in r.stdout, r.stdout)
    shutil.rmtree(ws, ignore_errors=True)


def main():
    parse_case()
    valid_file_case()
    missing_personal_case()
    uncommitted_edit_case()
    deletion_case()
    reorder_case()
    crlf_case()
    uncommitted_file_delete_case()
    committed_file_delete_case()
    delete_then_recreate_case()
    git_mv_away_new_file_case()
    committed_edit_case()
    append_passes_case()
    untracked_file_case()
    duplicate_id_case()
    supersedes_unknown_case()
    supersedes_forward_case()
    double_supersession_case()
    repo_scope_mismatch_case()
    missing_evidence_case()
    rule_too_long_case()
    over_budget_count_case()
    over_budget_chars_case()
    near_budget_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
