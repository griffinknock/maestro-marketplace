#!/usr/bin/env python3
"""Focused checks for the lessons flow (maestro/scripts/lessons.py):
`inject` (SessionStart hook), `accept`, `publish`, `status`, and the
hooks.json wiring. Complements lessons_units.py (the pure validator) and
lessons_capture_units.py (the capture queue).

    python3 tests/lessons_flow_units.py

Exit code is 0 when every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "maestro" / "scripts"
sys.path.insert(0, str(PLUGIN))
import lessons
import lessons_check

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def tmp_store():
    return Path(tempfile.mkdtemp(prefix="maestro-lessons-flow-")).resolve()


def git_repo():
    ws = Path(tempfile.mkdtemp(prefix="maestro-lessons-flow-repo-")).resolve()
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"], check=True)
    return ws


def entry(id_, scope, rule="Do the thing.", why="Because it broke once.",
          evidence='ab12cd34 · fail:builder:2 · "quote"',
          supersedes=None, accepted="2026-01-01"):
    lines = [f"## {id_} · scope: {scope}", f"Rule: {rule}", f"Why: {why}",
             f"Evidence: {evidence}"]
    if supersedes:
        lines.append(f"Supersedes: {supersedes}")
    lines.append(f"Accepted: {accepted}")
    return "\n".join(lines)


def write_lessons_file(path, entries_list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Maestro lessons\n\n" + "\n\n".join(entries_list) + "\n")
    return path


def write_raw(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def run_inject(cwd, extra_env=None):
    env = {**os.environ, **(extra_env or {})}
    payload = json.dumps({"cwd": str(cwd), "session_id": "sess-inject",
                          "hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "inject"],
                          input=payload, capture_output=True, text=True, env=env)


# --------------------------------------------------------------------------
def inject_silent_case():
    print("\n=== inject: silent when there is nothing to say ===")
    store, repo = tmp_store(), git_repo()
    try:
        r = run_inject(repo, {"MAESTRO_LESSONS_DIR": str(store)})
        check("exits 0", r.returncode == 0, r.stderr)
        check("stdout is empty", r.stdout.strip() == "", repr(r.stdout))
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def inject_both_tiers_and_scope_case():
    print("\n=== inject: personal + repo tiers, scope filtering ===")
    store, repo = tmp_store(), git_repo()
    reponame = repo.name
    try:
        write_lessons_file(store / "lessons.md", [
            entry("L-001", "global", rule="Always dispatch scouts together."),
            entry("L-002", f"repo:not-{reponame}", rule="A lesson scoped elsewhere."),
        ])
        write_lessons_file(repo / ".claude" / "maestro-lessons.md", [
            entry("R-001", f"repo:{reponame}", rule="Repo-local rule."),
        ])
        r = run_inject(repo, {"MAESTRO_LESSONS_DIR": str(store)})
        check("exits 0", r.returncode == 0, r.stderr)
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("global personal lesson present", "L-001" in ctx, ctx)
        check("repo-local lesson present", "R-001" in ctx, ctx)
        check("lesson scoped to a different repo is excluded", "L-002" not in ctx, ctx)
        check("hookEventName carried through",
              out["hookSpecificOutput"]["hookEventName"] == "SessionStart")
        check("suppressOutput set", out["suppressOutput"] is True)
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def inject_pending_line_case():
    print("\n=== inject: pending candidates line ===")
    store, repo = tmp_store(), git_repo()
    try:
        lessons._new_candidate(store, "sess-p", str(repo), "correction", None, None,
                               "griffin corrected tiering")
        lessons._new_candidate(store, "sess-p", str(repo), "correction", None, None,
                               "griffin corrected briefs")
        r = run_inject(repo, {"MAESTRO_LESSONS_DIR": str(store)})
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("pending count line present", "2 lesson candidate(s) pending" in ctx, ctx)
        check("points at the review command", "/maestro:lessons" in ctx, ctx)
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def inject_check_failed_case():
    print("\n=== inject: check-failed line, still injects what parses ===")
    store, repo = tmp_store(), git_repo()
    try:
        broken = ("## L-001 · scope: global\n"
                 "Rule: A rule missing its evidence field.\n"
                 "Why: Because it broke once.\n"
                 "Accepted: 2026-01-01\n")
        write_raw(store / "lessons.md", "# Maestro lessons\n\n" + broken)
        r = run_inject(repo, {"MAESTRO_LESSONS_DIR": str(store)})
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("still injects the parseable entry", "L-001" in ctx, ctx)
        check("carries the check-failed line", "LESSONS CHECK FAILED" in ctx, ctx)
        check("names the validator", "lessons_check.py" in ctx, ctx)
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def inject_lessons_off_case():
    print("\n=== inject: MAESTRO_LESSONS=0 is silent ===")
    store, repo = tmp_store(), git_repo()
    try:
        write_lessons_file(store / "lessons.md", [entry("L-001", "global")])
        r = run_inject(repo, {"MAESTRO_LESSONS_DIR": str(store), "MAESTRO_LESSONS": "0"})
        check("exits 0", r.returncode == 0, r.stderr)
        check("stdout empty even though a lesson exists", r.stdout.strip() == "",
              repr(r.stdout))
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


# --------------------------------------------------------------------------
def accept_basic_case():
    print("\n=== accept: appends parseable, commits, marks candidates ===")
    store = tmp_store()
    try:
        cid = lessons._new_candidate(store, "sess-a", "/tmp", "correction", None, None,
                                     "griffin corrected tiering assignment")
        lid, reasons, _ = lessons.accept(
            rule="Assign the cheapest agent tier that can do the job.",
            why="Griffin corrected an over-tiered dispatch.",
            evidence='sess-a1 · c-abc123 · "assign cheapest tier"',
            scope="global", candidates=[cid], d=store)
        check("accept returns L-001", lid == "L-001", lid)
        check("no failure reasons", reasons == [], reasons)

        path = store / "lessons.md"
        check("file is parseable and valid", lessons_check.check(path, None, None) == [])
        text = path.read_text()
        check("heading present", "## L-001 · scope: global" in text, text)

        log = subprocess.run(["git", "-C", str(store), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        check("committed", "lesson: L-001" in log, log)

        status = subprocess.run(["git", "-C", str(store), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        check("clean tree after commit", "lessons.md" not in status, status)

        rows = lessons.load_candidates(store)
        row = next(r for r in rows if r["id"] == cid)
        check("listed candidate marked accepted", row["status"] == "accepted", rows)
    finally:
        shutil.rmtree(store, ignore_errors=True)


def accept_rollback_case():
    print("\n=== accept: rolls back byte-exactly on a cap violation ===")
    store = tmp_store()
    try:
        too_long_rule = "x" * 300

        # Fresh store: a failing accept must leave no lessons.md at all.
        lid, reasons, _ = lessons.accept(rule=too_long_rule, why="w", evidence="e", d=store)
        check("accept fails on a cap violation", lid is None)
        check("reasons mention the Rule cap", any("Rule is" in r for r in reasons), reasons)
        check("no lessons.md was left behind", not (store / "lessons.md").is_file())

        # With a pre-existing valid file, a failing accept restores it byte-exactly.
        lid2, reasons2, _ = lessons.accept(rule="A fine short rule.", why="w",
                                           evidence="e", d=store)
        check("first accept on this store succeeds", lid2 == "L-001", (lid2, reasons2))
        before = (store / "lessons.md").read_bytes()

        lid3, reasons3, _ = lessons.accept(rule=too_long_rule, why="w", evidence="e", d=store)
        check("second (bad) accept fails", lid3 is None, reasons3)
        after = (store / "lessons.md").read_bytes()
        check("file restored byte-for-byte", after == before)
    finally:
        shutil.rmtree(store, ignore_errors=True)


def accept_supersedes_case():
    print("\n=== accept: supersedes retires the old entry from the active set ===")
    store = tmp_store()
    try:
        lid1, r1, _ = lessons.accept(rule="Old rule about dispatch batching.",
                                     why="w", evidence="e", d=store)
        check("first accept ok", lid1 == "L-001", r1)
        lid2, r2, _ = lessons.accept(rule="New rule about dispatch batching.",
                                     why="w", evidence="e", supersedes=lid1, d=store)
        check("second accept ok", lid2 == "L-002", r2)

        entries = lessons_check.parse((store / "lessons.md").read_text())
        active = lessons_check.active_for(entries, None)
        ids = {e["id"] for e in active}
        check("superseded entry is inactive", lid1 not in ids, ids)
        check("new entry is active", lid2 in ids, ids)
        check("whole file still validates",
              lessons_check.check(store / "lessons.md", None, None) == [])
    finally:
        shutil.rmtree(store, ignore_errors=True)


def accept_cli_case():
    print("\n=== accept: CLI surface ===")
    store = tmp_store()
    try:
        r = subprocess.run(
            [sys.executable, str(PLUGIN / "lessons.py"), "accept",
             "--rule", "Batch independent questions to one scout.",
             "--why", "Five one-question scouts cost five spawns.",
             "--evidence", 'sess-b1 · c-def456 · "one scout, five questions"'],
            capture_output=True, text=True,
            env={**os.environ, "MAESTRO_LESSONS_DIR": str(store)})
        check("CLI accept exits 0", r.returncode == 0, r.stderr)
        check("CLI prints the new id", r.stdout.strip() == "L-001", r.stdout)
        check("file exists and validates",
              lessons_check.check(store / "lessons.md", None, None) == [])

        r2 = subprocess.run(
            [sys.executable, str(PLUGIN / "lessons.py"), "accept",
             "--rule", "r", "--why", "w", "--evidence", "e", "--scope", "bogus"],
            capture_output=True, text=True,
            env={**os.environ, "MAESTRO_LESSONS_DIR": str(store)})
        check("CLI rejects an invalid scope before writing anything", r2.returncode != 0)
    finally:
        shutil.rmtree(store, ignore_errors=True)


# --------------------------------------------------------------------------
def publish_case():
    print("\n=== publish: writes an R- entry, uncommitted ===")
    store, repo = tmp_store(), git_repo()
    reponame = repo.name
    try:
        lid, reasons, _ = lessons.accept(
            rule="Batch every independent tool call in one message.",
            why="Sequential Edits to the same file serialize.",
            evidence='sess-c1 · c-ghi789 · "batch independent calls"',
            scope=f"repo:{reponame}", d=store)
        check("accept ok", lid == "L-001", reasons)

        rid, msg = lessons.publish(lid, cwd=str(repo), d=store)
        check("publish returns R-001", rid == "R-001", msg)
        check("message says uncommitted", "uncommitted" in msg, msg)

        repo_path = repo / ".claude" / "maestro-lessons.md"
        check("repo file written", repo_path.is_file())
        check("repo file validates",
              lessons_check.check(None, repo_path, reponame) == [])

        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        check("left uncommitted", ".claude" in status, status)
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def publish_refusals_case():
    print("\n=== publish: refuses global and other-repo lessons ===")
    store, repo, other = tmp_store(), git_repo(), git_repo()
    try:
        lid_global, _, _ = lessons.accept(rule="A global rule.", why="w", evidence="e",
                                          scope="global", d=store)
        rid, msg = lessons.publish(lid_global, cwd=str(repo), d=store)
        check("refuses a global lesson", rid is None, msg)
        check("refusal names the reason", "repo:" in msg, msg)

        lid_other, _, _ = lessons.accept(rule="A repo-scoped rule.", why="w",
                                         evidence="e", scope=f"repo:{other.name}", d=store)
        rid2, msg2 = lessons.publish(lid_other, cwd=str(repo), d=store)
        check("refuses a lesson scoped to a different repo", rid2 is None, msg2)

        r = subprocess.run(
            [sys.executable, str(PLUGIN / "lessons.py"), "publish", lid_global],
            capture_output=True, text=True, cwd=str(repo),
            env={**os.environ, "MAESTRO_LESSONS_DIR": str(store)})
        check("CLI publish exits 1 on refusal", r.returncode != 0)
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


# --------------------------------------------------------------------------
def status_case():
    print("\n=== status: counts vs budget, pending, rejected ===")
    store, repo = tmp_store(), git_repo()
    try:
        lessons.accept(rule="A global rule for status counting.", why="w",
                       evidence="e", scope="global", d=store)
        lessons._new_candidate(store, "sess-d", str(repo), "correction", None, None,
                               "pending one")
        lessons.reject("some-key", "a rejected rule", store)

        rep = lessons.status_report(cwd=str(repo), d=store)
        check("active counted", rep["active"] == 1, rep)
        check("chars positive", rep["chars"] > 0, rep)
        check("max_entries from lessons_check",
              rep["max_entries"] == lessons_check.MAX_BUDGET_ENTRIES)
        check("percent computed", 0 < rep["percent"] < 100, rep)
        check("not near budget with one entry", rep["near_budget"] is False, rep)
        check("pending counted", rep["pending"] == 1, rep)
        check("rejected counted", rep["rejected"] == 1, rep)

        r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "status", "--json"],
                           capture_output=True, text=True, cwd=str(repo),
                           env={**os.environ, "MAESTRO_LESSONS_DIR": str(store)})
        check("CLI status --json exits 0", r.returncode == 0, r.stderr)
        out = json.loads(r.stdout)
        check("CLI JSON matches direct call", out == rep, (out, rep))
    finally:
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


# --------------------------------------------------------------------------
def hooks_json_case():
    print("\n=== hooks.json: lessons.py inject wired into SessionStart, ledger.py kept ===")
    hooks_path = REPO / "maestro" / "hooks" / "hooks.json"
    data = json.loads(hooks_path.read_text())
    session_start = data["hooks"]["SessionStart"]
    commands = [h["command"] for group in session_start for h in group["hooks"]]
    check("ledger.py still wired", any("ledger.py" in c for c in commands), commands)
    check("lessons.py inject wired",
          any("lessons.py" in c and "inject" in c for c in commands), commands)


def main():
    inject_silent_case()
    inject_both_tiers_and_scope_case()
    inject_pending_line_case()
    inject_check_failed_case()
    inject_lessons_off_case()
    accept_basic_case()
    accept_rollback_case()
    accept_supersedes_case()
    accept_cli_case()
    publish_case()
    publish_refusals_case()
    status_case()
    hooks_json_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
