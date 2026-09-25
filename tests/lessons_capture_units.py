#!/usr/bin/env python3
"""Focused checks for the lessons capture side (maestro/scripts/lessons.py).

Covers the CLI/API surface directly, plus one integration case that drives
reorchestrate.py the way tests/replay.py does, to prove the wiring never
changes what the conductor sees.

    python3 tests/lessons_capture_units.py

Exit code is 0 when every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay as replay_mod
from replay import Conductor, workspace, PLUGIN, ENV, IDLE_FRAME, report_text

sys.path.insert(0, str(PLUGIN))
import lessons

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def tmp_store():
    return Path(tempfile.mkdtemp(prefix="maestro-lessons-"))


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
def init_case():
    print("\n=== init is idempotent ===")
    d = tmp_store()
    lessons.init(d)
    check("git repo created", (d / ".git").is_dir())
    check(".gitignore excludes candidates.jsonl",
          "candidates.jsonl" in (d / ".gitignore").read_text().splitlines())
    # second call must not blow up or duplicate the ignore line
    lessons.init(d)
    check("still a git repo after re-init", (d / ".git").is_dir())
    lines = (d / ".gitignore").read_text().splitlines()
    check("gitignore line not duplicated", lines.count("candidates.jsonl") == 1,
          f"{lines}")
    shutil.rmtree(d, ignore_errors=True)


def dedupe_case():
    print("\n=== capture_finding dedupes on session + fingerprint ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        cid1 = lessons.capture_finding("sess-a", "/tmp", "stall:x1:1", "agent stalled")
        cid2 = lessons.capture_finding("sess-a", "/tmp", "stall:x1:1", "agent stalled again")
        cid3 = lessons.capture_finding("sess-a", "/tmp", "stall:x2:1", "different fingerprint")
        cid4 = lessons.capture_finding("sess-b", "/tmp", "stall:x1:1", "different session")
        rows = lessons.load_candidates(d)
        check("first capture returns an id", bool(cid1))
        check("duplicate (same session+fingerprint) is skipped", cid2 is None)
        check("distinct fingerprint creates a new candidate", bool(cid3))
        check("distinct session creates a new candidate", bool(cid4))
        check("exactly 3 candidates landed", len(rows) == 3, f"got {len(rows)}")
        check("all candidates source=finding",
              all(r["source"] == "finding" for r in rows))
        check("truncated to <=300 chars",
              all(len(r["text"]) <= 300 for r in rows))
    finally:
        del os.environ["MAESTRO_LESSONS_DIR"]
        shutil.rmtree(d, ignore_errors=True)


def flag_case():
    print("\n=== flag records a correction ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        cid = lessons.flag("sess-c", "Griffin: always dispatch scouts in one message")
        check("flag returns an id", bool(cid))
        rows = lessons.load_candidates(d)
        check("exactly one candidate", len(rows) == 1, f"got {len(rows)}")
        r = rows[0]
        check("source is correction", r.get("source") == "correction")
        check("fingerprint is null", r.get("fingerprint") is None)
        check("repo recorded (non-empty string or None, never raises)",
              r.get("repo") is None or isinstance(r.get("repo"), str))
        check("status defaults to pending", r.get("status") == "pending")

        # Two flags in the same session are never deduped against each other.
        cid2 = lessons.flag("sess-c", "second correction, same session")
        check("a second flag in the same session is not deduped", bool(cid2) and cid2 != cid)
        check("both flags present", len(lessons.load_candidates(d)) == 2)
    finally:
        del os.environ["MAESTRO_LESSONS_DIR"]
        shutil.rmtree(d, ignore_errors=True)


def mark_fold_case():
    print("\n=== mark folds to the latest status ===")
    d = tmp_store()
    cid = lessons._new_candidate(d, "sess-d", "/tmp", "finding", "fail:builder:2", None,
                                 "builder failed twice")
    rows = lessons.load_candidates(d)
    check("starts pending", rows[0]["status"] == "pending")

    lessons.mark(cid, "reviewed", d)
    rows = lessons.load_candidates(d)
    check("folds to reviewed", rows[0]["status"] == "reviewed")

    lessons.mark(cid, "accepted", d)
    rows = lessons.load_candidates(d)
    check("later status wins over an earlier one", rows[0]["status"] == "accepted")
    check("still exactly one candidate row (status lines don't duplicate it)",
          len(rows) == 1)

    try:
        lessons.mark(cid, "bogus-status", d)
        check("rejects an unrecognized status", False)
    except ValueError:
        check("rejects an unrecognized status", True)

    # CLI surface: mark a real id, and reject an unknown one.
    r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "mark", cid,
                       "--status", "not-a-lesson"],
                      capture_output=True, text=True,
                      env={**os.environ, "MAESTRO_LESSONS_DIR": str(d)})
    check("CLI mark exits 0", r.returncode == 0, r.stderr)
    rows = lessons.load_candidates(d)
    check("CLI mark folds too", rows[0]["status"] == "not-a-lesson")

    r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "mark", "c-doesnotexist",
                       "--status", "accepted"],
                      capture_output=True, text=True,
                      env={**os.environ, "MAESTRO_LESSONS_DIR": str(d)})
    check("CLI mark on an unknown id fails", r.returncode != 0)
    shutil.rmtree(d, ignore_errors=True)


def reject_case():
    print("\n=== reject appends + commits, and is listed ===")
    d = tmp_store()
    rec = lessons.reject("dispatch-fanout", "Always dispatch independent scouts together.", d)
    check("reject returns the record", rec.get("key") == "dispatch-fanout")
    rows = lessons.load_rejected(d)
    check("rejected.jsonl has one entry", len(rows) == 1, f"got {len(rows)}")
    check("rule text preserved", rows[0]["rule"] == "Always dispatch independent scouts together.")

    log = subprocess.run(["git", "-C", str(d), "log", "--oneline"],
                        capture_output=True, text=True).stdout
    check("a commit exists", bool(log.strip()))
    check("commit message names the key", "reject: dispatch-fanout" in log)

    status = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                           capture_output=True, text=True).stdout
    check("rejected.jsonl is committed (clean tree)",
          "rejected.jsonl" not in status)

    # CLI surface.
    r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "reject",
                       "--key", "second-key", "--rule", "some other rule"],
                      capture_output=True, text=True,
                      env={**os.environ, "MAESTRO_LESSONS_DIR": str(d)})
    check("CLI reject exits 0", r.returncode == 0, r.stderr)
    rows = lessons.load_rejected(d)
    check("CLI reject appended a second entry", len(rows) == 2, f"got {rows}")
    shutil.rmtree(d, ignore_errors=True)


def unwritable_case():
    print("\n=== an unwritable store dir is silent, never raises ===")
    parent = tmp_store()
    try:
        os.chmod(parent, 0o500)   # read + execute, no write — mkdir underneath fails
        target = parent / "lessons"
        os.environ["MAESTRO_LESSONS_DIR"] = str(target)
        raised = False
        cid = None
        try:
            cid = lessons.capture_finding("sess-e", "/tmp", "fp-unwritable",
                                          "should not raise")
        except Exception:
            raised = True
        check("capture_finding never raises on an unwritable store", not raised)
        check("capture_finding returns None on failure", cid is None)

        raised = False
        try:
            cid2 = lessons.flag("sess-e", "a correction into an unwritable store")
        except Exception:
            raised = True
            cid2 = None
        check("flag never raises on an unwritable store either", not raised)
        check("flag returns None on failure", cid2 is None)
    finally:
        del os.environ["MAESTRO_LESSONS_DIR"]
        os.chmod(parent, 0o700)
        shutil.rmtree(parent, ignore_errors=True)


# --------------------------------------------------------------------------
class _EnvOverride:
    """Temporarily merges `extra` into replay's shared subprocess ENV dict."""

    def __init__(self, extra):
        self.extra = extra

    def __enter__(self):
        self.saved = dict(replay_mod.ENV)
        replay_mod.ENV.update(self.extra)
        return replay_mod.ENV

    def __exit__(self, *_):
        replay_mod.ENV.clear()
        replay_mod.ENV.update(self.saved)


def _final_payload(c):
    return json.dumps({"session_id": c.sid, "cwd": str(c.repo),
                       "hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "git status"},
                       "tool_response": {"stdout": ""}})


def integration_case():
    """A swallowed report -> REPORT RECOVERED, and a lesson candidate lands.

    Also proves capture never perturbs what the conductor is shown: the exact
    same triggering event, replayed against three lessons configurations
    (a real store, MAESTRO_LESSONS=0, and an unwritable store), must print the
    byte-identical thing on stdout every time.
    """
    print("\n=== integration: swallowed report feeds the lessons queue ===")
    sid = "eeee5555-0000-0000-0000-00000000000e"
    with _EnvOverride({}):
        ws = workspace()
        c = Conductor(PLUGIN, ws, sid)
        c.fire(hook_event_name="SessionStart", source="startup")
        ids = c.dispatch(1, names=["scout-long"])
        long_report = report_text("scout-long")
        tr = write_lines(ws / "tr" / "long.jsonl", [json.dumps(
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": long_report}]}})])
        time.sleep(2.5)
        c.tool_call()
        c.finish(ids["scout-long"], "scout-long", last=IDLE_FRAME, transcript=tr)
        time.sleep(5.5)

    pre = c.dir / "recheck.json"
    pre_bytes = pre.read_bytes() if pre.is_file() else None

    store = tmp_store()
    unwritable_parent = tmp_store()
    os.chmod(unwritable_parent, 0o500)
    unwritable_store = unwritable_parent / "lessons"

    configs = [
        ("capture-on", {"MAESTRO_LESSONS": "1", "MAESTRO_LESSONS_DIR": str(store)}),
        ("capture-disabled", {"MAESTRO_LESSONS": "0", "MAESTRO_LESSONS_DIR": str(store)}),
        ("capture-unwritable", {"MAESTRO_LESSONS": "1",
                                "MAESTRO_LESSONS_DIR": str(unwritable_store)}),
    ]
    stdouts = {}
    for name, extra in configs:
        if pre_bytes is None:
            pre.unlink(missing_ok=True)
        else:
            pre.write_bytes(pre_bytes)
        r = subprocess.run(
            [sys.executable, str(PLUGIN / "reorchestrate.py")],
            input=_final_payload(c), capture_output=True, text=True,
            env={**ENV, **extra})
        stdouts[name] = r.stdout

    check("the triggering call produced output",
          all(bool(s.strip()) for s in stdouts.values()))
    check("REPORT RECOVERED shows up", "REPORT RECOVERED" in stdouts["capture-on"])
    check("stdout is byte-identical: on vs disabled",
          stdouts["capture-on"] == stdouts["capture-disabled"])
    check("stdout is byte-identical: on vs unwritable",
          stdouts["capture-on"] == stdouts["capture-unwritable"])

    rows = lessons.load_candidates(store)
    finding_rows = [r for r in rows if r["source"] == "finding"]
    check("a candidate landed in the real store", len(finding_rows) >= 1,
          f"rows={rows}")
    check("its fingerprint is the delivery-shaped one",
          any((r.get("fingerprint") or "").startswith("delivery:recovered:")
              for r in finding_rows),
          f"rows={finding_rows}")
    check("its session matches the conductor's",
          all(r.get("session") == sid for r in finding_rows))
    check("nothing landed in the unwritable store",
          not (unwritable_store / "candidates.jsonl").is_file())

    os.chmod(unwritable_parent, 0o700)
    shutil.rmtree(store, ignore_errors=True)
    shutil.rmtree(unwritable_parent, ignore_errors=True)
    shutil.rmtree(ws, ignore_errors=True)


def main():
    init_case()
    dedupe_case()
    flag_case()
    mark_fold_case()
    reject_case()
    unwritable_case()
    integration_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
