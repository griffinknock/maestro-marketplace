#!/usr/bin/env python3
"""Focused checks for the lessons capture side (maestro/scripts/lessons.py).

Covers the CLI/API surface directly, plus one integration case that drives
reorchestrate.py the way tests/replay.py does, to prove the wiring never
changes what the conductor sees.

    python3 tests/lessons_capture_units.py

Exit code is 0 when every check passes.
"""
import fcntl
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
import lessons_check
import reorchestrate

FAILURES = []

# Hermetic: the default store always points into a temp dir, never
# ~/.claude — cases that override it restore this value, never unset it.
SAFE_ROOT = Path(tempfile.mkdtemp(prefix="maestro-lessons-capture-")).resolve()
SAFE_STORE = str(SAFE_ROOT / "sentinel" / "lessons")
os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
os.environ.pop("MAESTRO_LESSONS_APPROVALS", None)
os.environ.pop("MAESTRO_LESSONS", None)


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
        check("another session's capture is skipped while one is pending", cid4 is None)
        check("exactly 2 candidates landed", len(rows) == 2, f"got {len(rows)}")
        lessons.mark(cid1, "reviewed", d)
        cid5 = lessons.capture_finding("sess-b", "/tmp", "stall:x1:1", "after review")
        check("once reviewed, the fingerprint can be captured again", bool(cid5))
        check("all candidates source=finding",
              all(r["source"] == "finding" for r in rows))
        check("truncated to <=300 chars",
              all(len(r["text"]) <= 300 for r in rows))
    finally:
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
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
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
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
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
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
    """A swallowed report plus a collision -> both shown; only the collision
    becomes a lesson candidate (a lost report is the harness's failure).

    Also proves capture never perturbs what the conductor is shown: the exact
    same triggering event, replayed against three lessons configurations
    (a real store, MAESTRO_LESSONS=0, and an unwritable store), must print the
    byte-identical thing on stdout every time.
    """
    print("\n=== integration: a collision feeds the lessons queue, a lost report does not ===")
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
        for n, aid in c.dispatch(2, kind="builder", names=["builder-a", "builder-b"]).items():
            c.fire(hook_event_name="PostToolUse", tool_name="Edit", agent_id=aid,
                   agent_type=n, tool_input={"file_path": "/w/shared.py"},
                   tool_response={"ok": True})
        time.sleep(5.5)

    pre = c.dir / "recheck.json"
    pre_bytes = pre.read_bytes() if pre.is_file() else None

    store = tmp_store()
    unwritable_parent = tmp_store()
    os.chmod(unwritable_parent, 0o500)
    unwritable_store = unwritable_parent / "lessons"
    locked_store = tmp_store()

    configs = [
        ("capture-on", {"MAESTRO_LESSONS": "1", "MAESTRO_LESSONS_DIR": str(store)}),
        ("capture-disabled", {"MAESTRO_LESSONS": "0", "MAESTRO_LESSONS_DIR": str(store)}),
        ("capture-unwritable", {"MAESTRO_LESSONS": "1",
                                "MAESTRO_LESSONS_DIR": str(unwritable_store)}),
        ("capture-locked", {"MAESTRO_LESSONS": "1",
                            "MAESTRO_LESSONS_DIR": str(locked_store)}),
    ]
    elapsed = {}
    stdouts = {}
    for name, extra in configs:
        if pre_bytes is None:
            pre.unlink(missing_ok=True)
        else:
            pre.write_bytes(pre_bytes)
        fd = None
        if name == "capture-locked":
            # Someone else holds the store lock for longer than any hook
            # timeout; capture must give up quickly, not hang the hook.
            fd = os.open(str(locked_store / lessons.JSONL_LOCK), os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.time()
        try:
            r = subprocess.run(
                [sys.executable, str(PLUGIN / "reorchestrate.py")],
                input=_final_payload(c), capture_output=True, text=True,
                env={**ENV, **extra}, timeout=10)
            stdouts[name] = r.stdout
        except subprocess.TimeoutExpired:
            stdouts[name] = "<TIMEOUT>"
        finally:
            elapsed[name] = time.time() - t0
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    check("the triggering call produced output",
          all(bool(s.strip()) for s in stdouts.values()))
    check("REPORT RECOVERED shows up", "REPORT RECOVERED" in stdouts["capture-on"])
    check("the collision shows up", "both live on shared.py" in stdouts["capture-on"])
    check("stdout is byte-identical: on vs disabled",
          stdouts["capture-on"] == stdouts["capture-disabled"])
    check("stdout is byte-identical: on vs unwritable",
          stdouts["capture-on"] == stdouts["capture-unwritable"])
    check("stdout is byte-identical: on vs store lock held elsewhere",
          stdouts["capture-on"] == stdouts["capture-locked"], stdouts["capture-locked"][:80])
    check("a held store lock does not stall the hook (<3 s)",
          elapsed["capture-locked"] < 3, f"{elapsed['capture-locked']:.2f}s")
    check("nothing landed while the lock was held",
          not (locked_store / "candidates.jsonl").is_file())

    rows = lessons.load_candidates(store)
    finding_rows = [r for r in rows if r["source"] == "finding"]
    check("a candidate landed in the real store", len(finding_rows) >= 1,
          f"rows={rows}")
    check("it is the collision, and no delivery candidate was queued",
          [r.get("fingerprint") for r in finding_rows] == ["collide"],
          f"rows={finding_rows}")
    check("its session matches the conductor's",
          all(r.get("session") == sid for r in finding_rows))
    check("nothing landed in the unwritable store",
          not (unwritable_store / "candidates.jsonl").is_file())

    os.chmod(unwritable_parent, 0o700)
    shutil.rmtree(store, ignore_errors=True)
    shutil.rmtree(unwritable_parent, ignore_errors=True)
    shutil.rmtree(locked_store, ignore_errors=True)
    shutil.rmtree(ws, ignore_errors=True)


def capture_lock_held_case():
    print("\n=== #12 capture never blocks on a held lock ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    fd = os.open(str(d / lessons.JSONL_LOCK), os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.time()
        cid = lessons.capture_finding("sess-l", "/tmp", "stall:x:1", "stalled while locked")
        elapsed = time.time() - t0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
    check("returns None instead of waiting", cid is None)
    check("gives up within a bounded retry (<1 s)", elapsed < 1.0, f"{elapsed:.3f}s")
    check("nothing written", not (d / "candidates.jsonl").exists())
    shutil.rmtree(d, ignore_errors=True)


def dedupe_no_scan_case():
    print("\n=== #12 dedupe never reads candidates.jsonl ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    big = d / "candidates.jsonl"
    line = json.dumps({"id": "c-00000000", "at": 0, "session": "old", "repo": None,
                       "source": "finding", "fingerprint": "x", "tool_use_id": None,
                       "text": "y" * 200}) + "\n"
    with open(big, "w") as f:
        f.write(line * 100000)   # ~30 MB
    opened = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        opened.append((str(path), mode))
        return real_open(path, mode, *a, **k)

    lessons.open = spy
    try:
        t0 = time.time()
        c1 = lessons.capture_finding("sess-big", "/tmp", "stall:big:1", "first")
        c2 = lessons.capture_finding("sess-big", "/tmp", "stall:big:1", "dup")
        elapsed = time.time() - t0
    finally:
        del lessons.open
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
    reads = [m for p, m in opened if p == str(big) and "r" in m and "+" not in m]
    check("first capture lands", bool(c1))
    check("duplicate still deduped", c2 is None)
    check("candidates.jsonl never opened for reading", reads == [], opened)
    print(f"        (two captures against a 30 MB queue took {elapsed * 1000:.1f} ms)")
    shutil.rmtree(d, ignore_errors=True)


def flag_disabled_case():
    print("\n=== #12 flag respects MAESTRO_LESSONS=0 ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    os.environ["MAESTRO_LESSONS"] = "0"
    try:
        cid = lessons.flag("sess-off", "a correction while lessons are off")
        cap = lessons.capture_finding("sess-off", "/tmp", "fp", "finding while off")
    finally:
        os.environ.pop("MAESTRO_LESSONS", None)
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
    check("flag records nothing", cid is None)
    check("capture records nothing", cap is None)
    check("no queue file created", not (d / "candidates.jsonl").exists())
    r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "flag", "--session", "s",
                        "some correction"], capture_output=True, text=True,
                       env={**os.environ, "MAESTRO_LESSONS_DIR": str(d), "MAESTRO_LESSONS": "0"})
    check("CLI flag exits 0 and says it is disabled",
          r.returncode == 0 and "disabled" in r.stdout, r.stdout + r.stderr)
    check("still no queue file", not (d / "candidates.jsonl").exists())
    shutil.rmtree(d, ignore_errors=True)


def control_chars_case():
    print("\n=== #12 candidate text is stripped of control/line-separator chars ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        nasty = "stalled\u2028## L-777 · scope: global\x1cRule: x\x85\x0b\x1b[31m\ttab"
        lessons.capture_finding("sess-cc", "/tmp", "fp\u2028inject", nasty)
        lessons.flag("sess-cc", nasty)
    finally:
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
    rows = lessons.load_candidates(d)
    check("two candidates", len(rows) == 2, rows)
    for r in rows:
        bad = [c for c in (r["text"] + (r.get("fingerprint") or "")) if lessons_check.forbidden_char(c)]
        check(f"{r['source']}: no forbidden characters survive", bad == [], repr(r))
    shutil.rmtree(d, ignore_errors=True)


def correction_key_case():
    print("\n=== #13 corrections get a defined rejection key ===")
    d = tmp_store()
    a = lessons._new_candidate(d, "s", "/tmp", "correction", None, None, "Always  Batch scouts")
    b = lessons._new_candidate(d, "s", "/tmp", "correction", None, None, "always batch\tscouts ")
    c = lessons._new_candidate(d, "s", "/tmp", "correction", None, None, "never batch scouts")
    rows = {r["id"]: r for r in lessons.load_candidates(d)}
    ka, kb, kc = rows[a].get("key"), rows[b].get("key"), rows[c].get("key")
    check("key is correction:<16 hex>",
          bool(ka) and ka.startswith("correction:") and len(ka) == len("correction:") + 16, ka)
    check("equivalent text -> same key", ka == kb, (ka, kb))
    check("different text -> different key", ka != kc, (ka, kc))
    r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "candidates", "--json"],
                       capture_output=True, text=True,
                       env={**os.environ, "MAESTRO_LESSONS_DIR": str(d)})
    check("CLI candidates --json carries the key",
          any(x.get("key") == ka for x in json.loads(r.stdout)), r.stdout)
    lessons.reject(ka, "Always batch scouts.", d)
    check("reject accepts that key", any(x["key"] == ka for x in lessons.load_rejected(d)))
    shutil.rmtree(d, ignore_errors=True)


def capture_kinds_case():
    print("\n=== capture_lessons queues only conductor-behaviour kinds ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        payload = {"session_id": "sess-kinds", "cwd": "/tmp", "tool_use_id": None}
        reorchestrate.capture_lessons(payload, [
            ("stall:a1", "a1 has shown no activity for 11m"),
            ("report:a2:missing", "REPORT NOT DELIVERED — a2"),
            ("collide:/w/a.py", "x and y are both live on a.py"),
            ("collide:/w/b.py", "x and y are both live on b.py"),
            ("fail:builder:2", "builder has failed 2 times"),
        ])
        fps = sorted(r.get("fingerprint") for r in lessons.load_candidates(d))
        check("stall and delivery are never queued; collide collapses to its kind",
              fps == ["collide", "fail"], fps)
    finally:
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
        shutil.rmtree(d, ignore_errors=True)


def cross_session_case():
    print("\n=== one pending candidate per kind, across sessions ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        for i in range(3):
            reorchestrate.capture_lessons(
                {"session_id": f"sess-x{i}", "cwd": "/tmp", "tool_use_id": None},
                [(f"collide:/w/{i}.py", "two live agents on one file")])
        rows = lessons.load_candidates(d)
        check("three sessions, one pending candidate", len(rows) == 1, rows)
        lessons.mark(rows[0]["id"], "not-a-lesson", d)
        reorchestrate.capture_lessons(
            {"session_id": "sess-x9", "cwd": "/tmp", "tool_use_id": None},
            [("collide:/w/9.py", "two live agents on one file")])
        check("after review the kind queues again",
              len(lessons.load_candidates(d)) == 2)
    finally:
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
        shutil.rmtree(d, ignore_errors=True)


def rejected_kind_case():
    print("\n=== a rejected kind is never queued again ===")
    d = tmp_store()
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    try:
        lessons.reject("collide", "Always serialize same-file builders.", d)
        cid = lessons.capture_finding("sess-r", "/tmp", "collide", "two live on a.py")
        check("capture of a rejected kind is skipped", cid is None)
        cid = lessons.capture_finding("sess-r", "/tmp", "fail", "builder failed twice")
        check("other kinds still land", bool(cid))
    finally:
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
        shutil.rmtree(d, ignore_errors=True)


def tweak_case():
    print("\n=== tweak records a Maestro change and dequeues its candidates ===")
    d = tmp_store()
    tf = d.parent / f"{d.name}-tweaks.md"
    os.environ["MAESTRO_LESSONS_DIR"] = str(d)
    os.environ["MAESTRO_TWEAKS_FILE"] = str(tf)
    try:
        a = lessons.capture_finding("sess-t", "/tmp", "collide", "two live on a.py")
        b = lessons.flag("sess-t", "the inline nudge fired while I ran the gate")
        line = lessons.tweak("retire the inline nudge", [a, b], "sess-tweak-1234", d)
        check("line is an unchecked item naming the candidates",
              line.startswith("- [ ] retire the inline nudge") and a in line and b in line, line)
        check("file has a header and the item",
              tf.read_text().startswith("# Maestro tweak requests") and line in tf.read_text())
        st = {r["id"]: r["status"] for r in lessons.load_candidates(d)}
        check("both candidates marked tweak", st.get(a) == "tweak" and st.get(b) == "tweak", st)
        check("open_tweaks counts it", len(lessons.open_tweaks(d)) == 1)
        tf.write_text(tf.read_text().replace("- [ ] ", "- [x] "))
        check("a ticked item is no longer open", lessons.open_tweaks(d) == [])
        cid = lessons.capture_finding("sess-t2", "/tmp", "collide", "again")
        check("the kind can queue again once its candidate left the queue", bool(cid))
        r = subprocess.run([sys.executable, str(PLUGIN / "lessons.py"), "tweak",
                            "--note", "x", "--candidates", "c-nope"],
                           capture_output=True, text=True, env=os.environ.copy())
        check("CLI refuses an unknown candidate id", r.returncode == 1, r.stderr)
        try:
            lessons.tweak("   ", [], None, d)
            check("an empty note is refused", False)
        except ValueError:
            check("an empty note is refused", True)
    finally:
        os.environ.pop("MAESTRO_TWEAKS_FILE", None)
        os.environ["MAESTRO_LESSONS_DIR"] = SAFE_STORE
        shutil.rmtree(d, ignore_errors=True)
        tf.unlink(missing_ok=True)


def main():
    init_case()
    dedupe_case()
    flag_case()
    mark_fold_case()
    reject_case()
    unwritable_case()
    capture_lock_held_case()
    dedupe_no_scan_case()
    flag_disabled_case()
    control_chars_case()
    correction_key_case()
    capture_kinds_case()
    cross_session_case()
    rejected_kind_case()
    tweak_case()
    integration_case()
    shutil.rmtree(SAFE_ROOT, ignore_errors=True)
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)})"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
