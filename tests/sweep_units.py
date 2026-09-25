#!/usr/bin/env python3
"""Checks for the sweep-state surface: statusline usage snapshotting,
sweep_state.py's commands, and the SessionStart re-anchor hook.

    python3 tests/sweep_units.py

Exit code is 0 when every check passes.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "maestro" / "scripts"
STATUSLINE = SCRIPTS / "statusline.py"
SWEEP = SCRIPTS / "sweep_state.py"

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def tmpdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


TEST_SESSION = "sess-test-0001"


def base_env(sweeps_dir=None, usage_file=None, session=TEST_SESSION):
    """Claude Code exports $CLAUDE_CODE_SESSION_ID to every Bash-tool
    subprocess; sweep_state.py takes the owner from it. Pin it (or remove
    it, session=None) so these checks behave the same inside and outside a
    Claude Code session."""
    env = dict(os.environ)
    env["MAESTRO_SWEEPS_DIR"] = str(sweeps_dir or tmpdir("maestro-sweeps-"))
    env["MAESTRO_USAGE_FILE"] = str(usage_file or (tmpdir("maestro-usage-") / "usage.json"))
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def run_sweep(args, env, input_text=None):
    return subprocess.run([sys.executable, str(SWEEP), *args], input=input_text,
                          capture_output=True, text=True, env=env)


def run_statusline(payload, env, script=None):
    return subprocess.run([sys.executable, str(script or STATUSLINE)],
                          input=json.dumps(payload), capture_output=True, text=True, env=env)


# ── (1) statusline usage snapshot ────────────────────────────────────────

def usage_snapshot_case():
    print("\n=== statusline — usage snapshot ===")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(usage_file=usage_file)

    payload_none = {"session_id": "s1", "model": {"display_name": "Sonnet"},
                    "context_window": {"used_percentage": 10}}
    run_statusline(payload_none, env)
    check("no write when rate_limits is absent", not usage_file.exists())

    payload = {
        "session_id": "s1", "model": {"display_name": "Sonnet"},
        "context_window": {"used_percentage": 10},
        "rate_limits": {
            "five_hour": {"used_percentage": 42.5, "resets_at": 1999999999},
            "seven_day": {"used_percentage": 10.0, "resets_at": 2000000000},
        },
    }
    r = run_statusline(payload, env)
    check("statusline exits 0 with rate_limits present", r.returncode == 0)
    check("usage file written when rate_limits present", usage_file.is_file())
    rec = json.loads(usage_file.read_text())
    check("five_hour persisted", rec.get("five_hour", {}).get("used_percentage") == 42.5
          and rec["five_hour"].get("resets_at") == 1999999999
          and "captured_at" in rec["five_hour"], rec)
    check("seven_day persisted", rec.get("seven_day", {}).get("used_percentage") == 10.0
          and rec["seven_day"].get("resets_at") == 2000000000
          and "captured_at" in rec["seven_day"], rec)
    check("session_id persisted", rec.get("session_id") == "s1")

    mtime_before = usage_file.stat().st_mtime
    time.sleep(0.05)
    run_statusline(payload, env)
    mtime_after = usage_file.stat().st_mtime
    check("unchanged + fresh -> write skipped", mtime_before == mtime_after)

    changed = json.loads(json.dumps(payload))
    changed["rate_limits"]["five_hour"]["used_percentage"] = 55.0
    time.sleep(0.05)
    run_statusline(changed, env)
    rec2 = json.loads(usage_file.read_text())
    check("changed value still gets written", rec2["five_hour"]["used_percentage"] == 55.0)


def usage_merge_case():
    print("\n=== statusline — C1 cross-session merge contract ===")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(usage_file=usage_file)

    def payload_for(five_pct=None, five_resets=None, seven_pct=None, seven_resets=None, sid="s"):
        p = {"session_id": sid, "model": {"display_name": "Sonnet"},
             "context_window": {"used_percentage": 10}, "rate_limits": {}}
        if five_pct is not None:
            p["rate_limits"]["five_hour"] = {"used_percentage": five_pct, "resets_at": five_resets}
        if seven_pct is not None:
            p["rate_limits"]["seven_day"] = {"used_percentage": seven_pct, "resets_at": seven_resets}
        return p

    # Repro: session A at 79%, then idle session B re-sends a stale 30% for
    # the SAME window (same resets_at) -> B's newer wall-clock write must
    # not regress the persisted value.
    run_statusline(payload_for(five_pct=79, five_resets=5000, sid="A"), env)
    time.sleep(0.05)
    run_statusline(payload_for(five_pct=30, five_resets=5000, sid="B"), env)
    rec = json.loads(usage_file.read_text())
    check("idle session's stale lower reading does not regress the window",
          rec["five_hour"]["used_percentage"] == 79, rec)

    # A payload carrying only seven_day must not remove/null five_hour.
    time.sleep(0.05)
    run_statusline(payload_for(seven_pct=12, seven_resets=9000, sid="C"), env)
    rec2 = json.loads(usage_file.read_text())
    check("seven_day-only payload keeps five_hour intact",
          rec2.get("five_hour", {}).get("used_percentage") == 79, rec2)
    check("seven_day-only payload persists seven_day",
          rec2.get("seven_day", {}).get("used_percentage") == 12, rec2)

    # A newer resets_at replaces outright, even with a lower percentage.
    time.sleep(0.05)
    run_statusline(payload_for(five_pct=2, five_resets=6000, sid="D"), env)
    rec3 = json.loads(usage_file.read_text())
    check("newer resets_at replaces the window outright",
          rec3["five_hour"]["used_percentage"] == 2 and rec3["five_hour"]["resets_at"] == 6000, rec3)

    # NaN used_percentage is ignored, not merged in.
    usage_file2 = tmpdir("maestro-usage-") / "usage.json"
    env2 = base_env(usage_file=usage_file2)
    good = payload_for(five_pct=44, five_resets=7000, sid="E")
    run_statusline(good, env2)
    nan_payload = json.dumps(good).replace("44", "NaN")
    r = subprocess.run([sys.executable, str(STATUSLINE)], input=nan_payload,
                       capture_output=True, text=True, env=env2)
    check("NaN payload doesn't crash statusline", r.returncode == 0, r.stderr)
    rec4 = json.loads(usage_file2.read_text())
    check("NaN used_percentage is ignored, prior value kept",
          rec4["five_hour"]["used_percentage"] == 44, rec4)


def tmp_cleanup_on_replace_failure_case():
    print("\n=== statusline — tmp file cleaned up when os.replace fails ===")
    usage_dir = tmpdir("maestro-usage-")
    usage_file = usage_dir / "usage.json"
    usage_file.mkdir()  # a directory in place of the file -> os.replace onto it fails
    env = base_env(usage_file=usage_file)
    payload = {"session_id": "s", "model": {"display_name": "Sonnet"},
               "context_window": {"used_percentage": 10},
               "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 1000}}}
    r = run_statusline(payload, env)
    check("statusline exits 0 even when the write fails", r.returncode == 0, r.stderr)
    leftovers = glob.glob(str(usage_dir / "*.tmp*"))
    check("no leftover tmp file after a failed os.replace", leftovers == [], leftovers)


def stdout_unchanged_case():
    print("\n=== statusline — stdout unchanged by this edit ===")
    r = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:maestro/scripts/statusline.py"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        print("  skip — no committed prior version of statusline.py to diff against")
        return
    old = tmpdir("maestro-old-statusline-") / "statusline.py"
    old.write_text(r.stdout)

    payload = {"session_id": "s2", "model": {"display_name": "Opus"},
               "cost": {"total_cost_usd": 1.23, "total_duration_ms": 65000},
               "context_window": {"used_percentage": 30},
               "workspace": {"current_dir": str(REPO)}}
    env = base_env()
    before = run_statusline(payload, env, script=old)
    after = run_statusline(payload, env, script=STATUSLINE)
    check("stdout identical to the pre-edit script", before.stdout == after.stdout,
          f"before={before.stdout!r} after={after.stdout!r}")


# ── (2) sweep_state.py lifecycle ─────────────────────────────────────────

def write_plan_and_items(d, labels, plan_text="# Plan\n\nDo the thing.\n"):
    plan = d / "plan.md"
    items = d / "items.txt"
    plan.write_text(plan_text)
    items.write_text("\n".join(labels) + "\n")
    return plan, items


def lifecycle_case():
    print("\n=== sweep_state — new/next/done/fail/finding/add (additive) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one", "two", "three"])

    r = run_sweep(["new", "--slug", "s1", "--plan", str(plan), "--items", str(items)], env)
    check("new exits 0", r.returncode == 0, r.stderr)
    out = json.loads(r.stdout)
    check("new creates 3 items", out.get("items") == 3)

    d = ws / "sweeps" / "s1"
    index = json.loads((d / "index.json").read_text())
    check("policy defaults to additive", json.loads((d / "policy.json").read_text())["deviation"] == "additive")
    check("plan.md frozen with matching sha", index["plan_sha256"] ==
          __import__("hashlib").sha256(plan.read_text().encode()).hexdigest())

    r = run_sweep(["next", "s1", "--n", "2"], env)
    check("next exits 0", r.returncode == 0, r.stderr)
    claimed = json.loads(r.stdout)
    check("next claims 2 items", len(claimed) == 2)
    pace = [json.loads(l) for l in (d / "pace.jsonl").read_text().splitlines() if l.strip()]
    check("next appends one pace start line", len(pace) == 1 and pace[0]["event"] == "start")
    check("pace start carries the claimed ids", pace[0]["items"] == [c["id"] for c in claimed])
    check("pace start carries a usage snapshot shape",
          set(pace[0]["usage"]) == {"five_hour", "seven_day", "captured_at", "captured", "resets"})
    check("pace start's owner is this Claude Code session's id",
          pace[0].get("owner") == TEST_SESSION, pace[0].get("owner"))

    r = run_sweep(["status", "s1"], env)
    check("status mentions 2 running", "2 running" in r.stdout, r.stdout)

    first_id = claimed[0]["id"]
    second_id = claimed[1]["id"]
    r = run_sweep(["done", "s1", first_id, "--note", "ok"], env)
    check("done exits 0", r.returncode == 0, r.stderr)

    r = run_sweep(["fail", "s1", second_id, "--reason", "flaked"], env)
    check("first fail returns to pending (attempts < max)", r.returncode == 0 and r.stdout.strip() == "pending", r.stdout)
    r = run_sweep(["fail", "s1", second_id, "--reason", "flaked again"], env)
    check("second fail becomes failed (attempts == max_attempts)",
          r.returncode == 0 and r.stdout.strip() == "failed", r.stdout)

    r = run_sweep(["finding", "s1", "--text", "found a gotcha"], env)
    check("finding prints an id", r.returncode == 0 and r.stdout.strip() == "f-0001", r.stdout)
    findings = [json.loads(l) for l in (d / "findings.jsonl").read_text().splitlines() if l.strip()]
    check("finding recorded", len(findings) == 1 and findings[0]["text"] == "found a gotcha")

    r = run_sweep(["add", "s1", "--label", "four", "--from-finding", "f-0001"], env)
    check("add under additive succeeds", r.returncode == 0, r.stderr)
    new_id = r.stdout.strip()
    index2 = json.loads((d / "index.json").read_text())
    check("added item present in index", any(it["id"] == new_id for it in index2["items"]))
    check("original items were never removed", len(index2["items"]) == 4)
    amendments = [json.loads(l) for l in (d / "amendments.jsonl").read_text().splitlines() if l.strip()]
    check("add_item amendment recorded", any(a["op"] == "add_item" and a["item"] == new_id
                                             for a in amendments))

    r = run_sweep(["check", "s1"], env)
    check("check passes on a healthy sweep", r.returncode == 0 and "SWEEP PASS" in r.stdout, r.stdout)
    return ws, env, d


def locked_add_case():
    print("\n=== sweep_state — add refused under locked ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"])
    run_sweep(["new", "--slug", "s2", "--plan", str(plan), "--items", str(items),
              "--policy", json.dumps({"deviation": "locked"})], env)
    r = run_sweep(["add", "s2", "--label", "sneaky", "--from-finding", "f-0001"], env)
    check("add refused under locked", r.returncode == 2, f"rc={r.returncode} out={r.stdout} err={r.stderr}")


def amend_plan_case():
    print("\n=== sweep_state — amend-plan (locked refuses, adaptive accepts, plan.md untouched) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"], plan_text="# Plan v1\n")
    run_sweep(["new", "--slug", "s3", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s3"
    original_plan = (d / "plan.md").read_text()

    new_plan = ws / "plan2.md"
    new_plan.write_text("# Plan v2 — amended\n")
    r = run_sweep(["amend-plan", "s3", "--file", str(new_plan), "--from-finding", "f-0001"], env)
    check("amend-plan refused under additive", r.returncode == 2, f"rc={r.returncode}")

    run_sweep(["fail", "s3", "i-0001", "--reason", "x"], env)  # no-op on policy; just touching state
    # bump the policy to adaptive — through set-policy, the only supported way
    r = run_sweep(["set-policy", "s3", "--json", json.dumps({"deviation": "adaptive"})], env)
    check("set-policy to adaptive succeeds", r.returncode == 0, r.stderr)

    r = run_sweep(["amend-plan", "s3", "--file", str(new_plan), "--from-finding", "f-0001"], env)
    check("amend-plan refuses an unknown --from-finding", r.returncode == 2 and "finding" in r.stderr,
          f"rc={r.returncode} err={r.stderr}")
    check("refused amend-plan wrote no plan version", not (d / "plan.v2.md").exists())
    run_sweep(["finding", "s3", "--text", "the plan misses a step"], env)  # -> f-0001

    r = run_sweep(["amend-plan", "s3", "--file", str(new_plan), "--from-finding", "f-0001"], env)
    check("amend-plan accepted under adaptive", r.returncode == 0, r.stderr)
    check("plan.md untouched by amend-plan", (d / "plan.md").read_text() == original_plan)
    check("new version written as plan.v2.md", (d / "plan.v2.md").read_text() == "# Plan v2 — amended\n")
    index = json.loads((d / "index.json").read_text())
    check("plan_versions records the new version",
          any(v["file"] == "plan.v2.md" for v in index.get("plan_versions", [])))
    r = run_sweep(["check", "s3"], env)
    check("check passes after set-policy + amend-plan", r.returncode == 0, r.stdout)


def recover_case():
    print("\n=== sweep_state — recover after a simulated crash ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s4", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s4"
    run_sweep(["next", "s4", "--n", "2"], env)   # both items now "running"; no end-chunk (crash)

    r = run_sweep(["recover", "s4"], env)
    check("recover exits 0", r.returncode == 0, r.stderr)
    recovered = json.loads(r.stdout)
    check("recover reports both items", sorted(recovered) == ["i-0001", "i-0002"])
    index = json.loads((d / "index.json").read_text())
    check("recovered items back to pending with recoveries+1, attempts untouched (fail() spends attempts, not recover())",
          all(it["status"] == "pending" and it["recoveries"] == 1 and it["attempts"] == 0
              for it in index["items"]))
    pace = [json.loads(l) for l in (d / "pace.jsonl").read_text().splitlines() if l.strip()]
    check("open chunk closed with interrupted end line",
          pace[-1]["event"] == "end" and pace[-1]["interrupted"] is True)

    r = run_sweep(["recover", "s4"], env)
    check("second recover is a no-op (nothing running)", json.loads(r.stdout) == [])


def next_drained_case():
    print("\n=== sweep_state — next on a drained sweep writes nothing and exits 3 ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a"])
    run_sweep(["new", "--slug", "s4b", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s4b"
    r = run_sweep(["next", "s4b"], env)
    check("first next claims the only item", json.loads(r.stdout) != [] and r.returncode == 0)
    r1 = run_sweep(["next", "s4b"], env)
    check("next while the only item is still running is not 'finished' (exit 4, not 3)",
          r1.returncode == 4 and r1.stdout.strip() == "[]", f"rc={r1.returncode}")
    run_sweep(["done", "s4b", "i-0001"], env)
    run_sweep(["end-chunk", "s4b"], env)
    pace_before = (d / "pace.jsonl").read_text()

    r2 = run_sweep(["next", "s4b"], env)
    check("next on a drained sweep prints []", r2.stdout.strip() == "[]", r2.stdout)
    check("next on a drained sweep exits 3", r2.returncode == 3)
    check("next on a drained sweep writes nothing to pace.jsonl",
          (d / "pace.jsonl").read_text() == pace_before)

    r3 = run_sweep(["check", "s4b"], env)
    check("check still passes on a finished sweep (no empty start record)",
          r3.returncode == 0 and "SWEEP PASS" in r3.stdout, r3.stdout)


def two_owner_recover_case():
    print("\n=== sweep_state — recover: two-owner busy/stale/recoveries-cap ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s4c", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s4c"

    run_sweep(["next", "s4c", "--owner", "alice"], env)  # claims both (chunk_size default 5)

    r = run_sweep(["recover", "s4c", "--owner", "bob", "--stale-after", "7200"], env)
    check("a different, fresh owner's chunk refuses recover", r.returncode == 4, f"rc={r.returncode} out={r.stdout} err={r.stderr}")
    check("busy message names the chunk and owner", "busy" in r.stderr and "alice" in r.stderr, r.stderr)
    index = json.loads((d / "index.json").read_text())
    check("nothing reclaimed while busy", all(it["status"] == "running" for it in index["items"]))

    r2 = run_sweep(["recover", "s4c", "--owner", "bob", "--stale-after", "0"], env)
    check("a stale owner's chunk IS reclaimed", r2.returncode == 0, r2.stderr)
    check("stale reclaim reports both items", sorted(json.loads(r2.stdout)) == ["i-0001", "i-0002"])
    index2 = json.loads((d / "index.json").read_text())
    check("stale reclaim returns items to pending",
          all(it["status"] == "pending" and it["recoveries"] == 1 for it in index2["items"]))

    # recoveries cap: recover the same item to the failure threshold.
    run_sweep(["next", "s4c", "--owner", "alice", "--n", "2"], env)
    run_sweep(["recover", "s4c", "--owner", "alice", "--stale-after", "0"], env)  # recoveries=2
    run_sweep(["next", "s4c", "--owner", "alice", "--n", "2"], env)
    r3 = run_sweep(["recover", "s4c", "--owner", "alice", "--stale-after", "0"], env)  # recoveries=3 -> failed
    index3 = json.loads((d / "index.json").read_text())
    check("item recovered a 3rd time is marked failed, not pending",
          all(it["status"] == "failed" and it["recoveries"] == 3 for it in index3["items"]), index3["items"])
    check("failed-by-recoveries item carries an explanatory note",
          all("recoveries" in (it.get("note") or "") for it in index3["items"]), index3["items"])


def end_chunk_owner_case():
    print("\n=== sweep_state — end-chunk closes the calling owner's chunk only ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s4d", "--plan", str(plan), "--items", str(items),
              "--policy", json.dumps({"chunk_size": 1})], env)
    d = ws / "sweeps" / "s4d"
    run_sweep(["next", "s4d", "--owner", "alice"], env)
    run_sweep(["next", "s4d", "--owner", "bob"], env)

    r = run_sweep(["end-chunk", "s4d", "--owner", "bob"], env)
    check("end-chunk --owner bob succeeds", r.returncode == 0, r.stderr)
    pace = [json.loads(l) for l in (d / "pace.jsonl").read_text().splitlines() if l.strip()]
    bob_start = next(r for r in pace if r["event"] == "start" and r.get("owner") == "bob")
    bob_end = [r for r in pace if r["event"] == "end" and r["chunk"] == bob_start["chunk"]]
    alice_start = next(r for r in pace if r["event"] == "start" and r.get("owner") == "alice")
    alice_end = [r for r in pace if r["event"] == "end" and r["chunk"] == alice_start["chunk"]]
    check("bob's chunk is closed", len(bob_end) == 1, pace)
    check("alice's chunk stays open", len(alice_end) == 0, pace)


def check_gate_case():
    print("\n=== sweep_state — check gate names each broken thing ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one", "two"])
    run_sweep(["new", "--slug", "s5", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s5"

    r = run_sweep(["check", "s5"], env)
    check("healthy sweep passes", r.returncode == 0 and "SWEEP PASS" in r.stdout)

    # plan.md edited
    (d / "plan.md").write_text("tampered\n")
    r = run_sweep(["check", "s5"], env)
    check("edited plan.md fails", r.returncode == 1 and "plan.md" in r.stdout)
    (d / "plan.md").write_text(plan.read_text())  # restore

    # an item removed from index.json
    index = json.loads((d / "index.json").read_text())
    removed = index["items"].pop()
    (d / "index.json").write_text(json.dumps(index))
    r = run_sweep(["check", "s5"], env)
    ok_after_plain_removal = r.returncode == 0   # removing an item with no amendment isn't itself detectable
    index["items"].append(removed)
    (d / "index.json").write_text(json.dumps(index))

    # duplicate id
    index2 = json.loads((d / "index.json").read_text())
    index2["items"].append(dict(index2["items"][0]))
    (d / "index.json").write_text(json.dumps(index2))
    r = run_sweep(["check", "s5"], env)
    check("duplicate item id fails", r.returncode == 1 and "duplicate item id" in r.stdout, r.stdout)
    index2["items"].pop()
    (d / "index.json").write_text(json.dumps(index2))

    # item referenced by an amendment but missing from index.json
    run_sweep(["finding", "s5", "--text", "seed for add"], env)  # -> f-0001
    run_sweep(["add", "s5", "--label", "three", "--from-finding", "f-0001"], env)
    index3 = json.loads((d / "index.json").read_text())
    index3["items"] = [it for it in index3["items"] if it["label"] != "three"]
    (d / "index.json").write_text(json.dumps(index3))
    r = run_sweep(["check", "s5"], env)
    check("item removed after being added by an amendment fails", r.returncode == 1 and "missing" in r.stdout, r.stdout)

    # corrupt jsonl line
    with open(d / "findings.jsonl", "a") as f:
        f.write("not json at all\n")
    r = run_sweep(["check", "s5"], env)
    check("corrupt jsonl line fails", r.returncode == 1 and "does not parse" in r.stdout, r.stdout)
    check("plain item removal is now caught via items_sha256", not ok_after_plain_removal, "expected removal to be flagged now")


def ghost_add_case():
    print("\n=== sweep_state — check catches a ghost item (index edited outside add) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one", "two"])
    run_sweep(["new", "--slug", "s5b", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s5b"

    index = json.loads((d / "index.json").read_text())
    index["items"].append({"id": "i-0003", "label": "ghost", "status": "pending",
                            "attempts": 0, "recoveries": 0, "from_finding": None, "note": None})
    (d / "index.json").write_text(json.dumps(index))
    r = run_sweep(["check", "s5b"], env)
    check("a ghost item (added outside `add`, no amendment) fails check",
          r.returncode == 1 and ("items_sha256" in r.stdout or "no add_item amendment" in r.stdout), r.stdout)


def unknown_finding_refused_case():
    print("\n=== sweep_state — add refuses a nonexistent --from-finding ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"])
    run_sweep(["new", "--slug", "s5c", "--plan", str(plan), "--items", str(items)], env)
    r = run_sweep(["add", "s5c", "--label", "sneaky", "--from-finding", "f-9999"], env)
    check("add refuses an unknown finding", r.returncode == 2, f"rc={r.returncode} out={r.stdout} err={r.stderr}")


def policy_validation_case():
    print("\n=== sweep_state — policy validation (new + check) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"])

    bad_policies = [
        {"deviation": "Locked"},                 # wrong case, not in the closed set
        {"chunk_size": "5"},                     # string, not int
        {"ceilings": {"five_hour": 0}},           # not > 0
        {"ceilings": {"five_hour": 150}},         # not <= 100
        {"margin_s": -1},
        {"max_attempts": 0},
        {"allow_blind": "false"},                # string, not bool
        {"blind_gap_s": 10},                      # below the 60s floor
    ]
    for i, override in enumerate(bad_policies):
        slug = f"s8-bad-{i}"
        r = run_sweep(["new", "--slug", slug, "--plan", str(plan), "--items", str(items),
                      "--policy", json.dumps(override)], env)
        check(f"invalid policy rejected at new: {override}", r.returncode == 2, f"out={r.stdout} err={r.stderr}")

    r = run_sweep(["new", "--slug", "s8-good", "--plan", str(plan), "--items", str(items)], env)
    check("a sweep with the default policy is created fine", r.returncode == 0, r.stderr)
    d = ws / "sweeps" / "s8-good"
    index = json.loads((d / "index.json").read_text())
    check("index.json records a policy_sha256", bool(index.get("policy_sha256")))

    # policy.json deleted -> every command that reads it refuses, and check fails.
    (d / "policy.json").unlink()
    r2 = run_sweep(["next", "s8-good"], env)
    check("next refuses when policy.json is missing", r2.returncode == 2, r2.stderr)
    r3 = run_sweep(["check", "s8-good"], env)
    check("check fails when policy.json is missing", r3.returncode == 1 and "policy.json is missing" in r3.stdout, r3.stdout)

    # policy.json edited (still valid JSON/shape, but doesn't match the hash) -> check fails.
    run_sweep(["new", "--slug", "s8-edited", "--plan", str(plan), "--items", str(items)], env)
    d2 = ws / "sweeps" / "s8-edited"
    pol = json.loads((d2 / "policy.json").read_text())
    pol["deviation"] = "locked"
    (d2 / "policy.json").write_text(json.dumps(pol))
    r4 = run_sweep(["check", "s8-edited"], env)
    check("check fails when policy.json was edited (sha256 mismatch)",
          r4.returncode == 1 and "policy.json" in r4.stdout, r4.stdout)


def lock_timeout_case():
    print("\n=== sweep_state — lock contention times out (exit 5), never proceeds unlocked ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    env["MAESTRO_LOCK_TIMEOUT_S"] = "0.2"
    env["MAESTRO_LOCK_STALE_S"] = "999"
    plan, items = write_plan_and_items(ws, ["a"])
    run_sweep(["new", "--slug", "s9", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s9"

    lock_dir = Path(str(d / ".sweep") + ".lock")
    lock_dir.mkdir()
    try:
        r = run_sweep(["next", "s9"], env)
        check("contended lock exits 5", r.returncode == 5, f"rc={r.returncode} out={r.stdout} err={r.stderr}")
        check("contended lock prints a clear refusal", "lock" in r.stderr.lower(), r.stderr)
        index = json.loads((d / "index.json").read_text())
        check("nothing mutated while the lock was held", index["items"][0]["status"] == "pending")
    finally:
        lock_dir.rmdir()

    r2 = run_sweep(["next", "s9"], env)
    check("lock released -> next works normally again", r2.returncode == 0, r2.stderr)


def atomic_writes_case():
    print("\n=== sweep_state — atomic writes leave no tmp files ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a", "b", "c"])
    run_sweep(["new", "--slug", "s6", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s6"
    run_sweep(["next", "s6", "--n", "1"], env)
    claimed = json.loads(run_sweep(["next", "s6", "--n", "1"], env).stdout)
    run_sweep(["end-chunk", "s6"], env)
    run_sweep(["finding", "s6", "--text", "x"], env)
    run_sweep(["add", "s6", "--label", "d", "--from-finding", "f-0001"], env)
    leftovers = glob.glob(str(d / "**" / "*.tmp*"), recursive=True) + \
        glob.glob(str(d / "*.tmp*"))
    check("no leftover tmp files after a batch of writes", leftovers == [], leftovers)


def anchor_case():
    print("\n=== sweep_state — anchor (SessionStart hook) ===")
    ws = tmpdir("maestro-ws-")
    sweeps = ws / "sweeps"
    env = base_env(sweeps_dir=sweeps)
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s7", "--plan", str(plan), "--items", str(items)], env)

    payload = json.dumps({"cwd": str(ws), "hook_event_name": "SessionStart", "source": "resume"})
    r = run_sweep(["anchor"], env, input_text=payload)
    check("anchor exits 0", r.returncode == 0)
    check("anchor prints something for an active sweep", bool(r.stdout.strip()))
    if r.stdout.strip():
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("anchor names the sweep and its progress", "s7" in ctx and "0/2 done" in ctx, ctx)
        check("anchor points at resume", "resume s7" in ctx, ctx)

    # finish every item -> no longer "active"
    d = sweeps / "s7"
    index = json.loads((d / "index.json").read_text())
    for it in index["items"]:
        it["status"] = "done"
    (d / "index.json").write_text(json.dumps(index))
    r2 = run_sweep(["anchor"], env, input_text=payload)
    check("anchor is silent once nothing is pending/running", r2.returncode == 0 and r2.stdout.strip() == "",
          repr(r2.stdout))

    # no sweeps at all
    empty_env = base_env(sweeps_dir=ws / "no-sweeps-here")
    r3 = run_sweep(["anchor"], empty_env, input_text=payload)
    check("anchor is silent with no sweeps directory", r3.returncode == 0 and r3.stdout.strip() == "")

    # malformed stdin must not raise
    r4 = run_sweep(["anchor"], env, input_text="not json")
    check("anchor never raises on malformed stdin", r4.returncode == 0)


def anchor_payload(ws, session):
    return json.dumps({"cwd": str(ws), "hook_event_name": "SessionStart",
                       "source": "resume", "session_id": session})


def anchor_ctx(env, payload):
    r = run_sweep(["anchor"], env, input_text=payload)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def anchor_owner_case():
    print("\n=== sweep_state — owner is the session id; anchor hints only the owner (finding 3) ===")
    ws = tmpdir("maestro-ws-")
    sweeps = ws / "sweeps"
    env = base_env(sweeps_dir=sweeps)
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s8", "--plan", str(plan), "--items", str(items)], env)
    d = sweeps / "s8"

    ctx0 = anchor_ctx(env, anchor_payload(ws, "sess-A"))
    check("no chunk open -> plain resume hint, no owner token, no takeover",
          "Resume with /loop /maestro:sweep resume s8" in ctx0 and "--owner" not in ctx0
          and "Take over" not in ctx0, ctx0)

    # Session A opens a chunk; its owner is A's session id (from the env,
    # exactly as the Bash tool provides it — no --owner on the command line).
    env_a = base_env(sweeps_dir=sweeps, session="sess-A")
    run_sweep(["next", "s8", "--n", "1"], env_a)
    pace = [json.loads(l) for l in (d / "pace.jsonl").read_text().splitlines() if l.strip()]
    check("chunk owner is the calling session's CLAUDE_CODE_SESSION_ID",
          pace[-1].get("owner") == "sess-A", pace[-1])

    ctx_a = anchor_ctx(env, anchor_payload(ws, "sess-A"))
    check("the owning session gets the resume hint",
          "Resume with /loop /maestro:sweep resume s8" in ctx_a and "Take over" not in ctx_a, ctx_a)

    # The attack: any other session in the repo used to be handed A's owner
    # token and adopt A's live chunk. Now it is told the sweep is owned
    # elsewhere and is never given a resume line.
    ctx_b = anchor_ctx(env, anchor_payload(ws, "sess-B"))
    check("another session is told the sweep is owned by another session since <time>",
          "owned by another session" in ctx_b and "sess-A" in ctx_b and " since " in ctx_b, ctx_b)
    check("another session gets the takeover line, not a resume line",
          "Take over with /maestro:sweep takeover s8" in ctx_b and "Resume with" not in ctx_b, ctx_b)

    env_b = base_env(sweeps_dir=sweeps, session="sess-B")
    r = run_sweep(["recover", "s8"], env_b)
    check("session B's recover refuses A's fresh chunk (exit 4)", r.returncode == 4, r.stderr)
    check("the busy line names the owner and the takeover remedy",
          "sess-A" in r.stderr and "takeover" in r.stderr, r.stderr)
    r = run_sweep(["end-chunk", "s8"], env_b)
    check("session B cannot close A's chunk", r.returncode == 2, r.stderr)
    index = json.loads((d / "index.json").read_text())
    check("A's item still running after B's attempts",
          index["items"][0]["status"] == "running", index["items"][0])

    # Crashed-session reclaim without waiting 7200 s: explicit takeover.
    r = run_sweep(["recover", "s8", "--takeover"], env_b)
    check("recover --takeover reclaims a fresh foreign chunk", r.returncode == 0
          and json.loads(r.stdout) == ["i-0001"], f"rc={r.returncode} out={r.stdout} err={r.stderr}")
    amendments = [json.loads(l) for l in (d / "amendments.jsonl").read_text().splitlines() if l.strip()]
    check("takeover recorded in amendments (from A to B, explicit)",
          any(a.get("op") == "takeover" and a.get("from_owner") == "sess-A"
              and a.get("to_owner") == "sess-B" and a.get("reason") == "explicit"
              for a in amendments), amendments)
    r = run_sweep(["check", "s8"], env)
    check("check passes after a takeover", r.returncode == 0, r.stdout)
    ctx_b2 = anchor_ctx(env, anchor_payload(ws, "sess-B"))
    check("after takeover (no open chunk) the resume hint is back",
          "Resume with /loop /maestro:sweep resume s8" in ctx_b2, ctx_b2)


def owner_required_case():
    print("\n=== sweep_state — no session id and no --owner -> refuse, never guess (finding 3) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps", session=None)
    plan, items = write_plan_and_items(ws, ["a"])
    run_sweep(["new", "--slug", "so", "--plan", str(plan), "--items", str(items)], env)
    for args in (["next", "so"], ["recover", "so"], ["end-chunk", "so"]):
        r = run_sweep(args, env)
        check(f"{args[0]} refuses without an owner (exit 2)",
              r.returncode == 2 and "CLAUDE_CODE_SESSION_ID" in r.stderr, f"rc={r.returncode} err={r.stderr}")
    index = json.loads((ws / "sweeps" / "so" / "index.json").read_text())
    check("nothing claimed without an owner", index["items"][0]["status"] == "pending")
    r = run_sweep(["next", "so", "--owner", "explicit"], env)
    check("--owner still works as an explicit override", r.returncode == 0, r.stderr)


def anchor_corrupt_pace_case():
    print("\n=== sweep_state — anchor never raises on corrupt pace.jsonl ===")
    ws = tmpdir("maestro-ws-")
    sweeps = ws / "sweeps"
    env = base_env(sweeps_dir=sweeps)
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s9", "--plan", str(plan), "--items", str(items)], env)
    run_sweep(["next", "s9", "--n", "1", "--owner", "owner-a"], env)

    pace_path = sweeps / "s9" / "pace.jsonl"
    pace_path.write_text(pace_path.read_text() + "not json at all\n")

    r = run_sweep(["anchor"], env, input_text=anchor_payload(ws, "owner-a"))
    check("anchor exits 0 with a corrupt pace.jsonl line", r.returncode == 0, r.stderr)
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""
    check("anchor still resolves the open chunk's owner despite the corrupt line",
          "Resume with /loop /maestro:sweep resume s9" in ctx, ctx)
    ctx_b = anchor_ctx(env, anchor_payload(ws, "owner-b"))
    check("…and still refuses to hand another session a resume line", "Take over" in ctx_b, ctx_b)


# ── round-3 adversary findings ───────────────────────────────────────────

def statusline_payload(five_pct=None, five_resets=None, seven_pct=None, seven_resets=None,
                       sid="s"):
    p = {"model": {"display_name": "Sonnet"},
         "context_window": {"used_percentage": 10}, "rate_limits": {}}
    if sid is not None:
        p["session_id"] = sid
    if five_pct is not None:
        p["rate_limits"]["five_hour"] = {"used_percentage": five_pct, "resets_at": five_resets}
    if seven_pct is not None:
        p["rate_limits"]["seven_day"] = {"used_percentage": seven_pct, "resets_at": seven_resets}
    return p


def reset_jitter_case():
    print("\n=== statusline — resets_at jitter is the same window (finding 1) ===")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(usage_file=usage_file)
    # Repro: A 79 -> idle B re-sends 30 with resets_at 1 s later -> A 80.
    run_statusline(statusline_payload(five_pct=79, five_resets=5000, sid="A"), env)
    time.sleep(0.02)
    run_statusline(statusline_payload(five_pct=30, five_resets=5001, sid="B"), env)
    rec = json.loads(usage_file.read_text())
    check("a jittered (+1 s) idle lower reading does not replace the fresh one",
          rec["five_hour"]["used_percentage"] == 79, rec)
    check("the later resets_at is kept", rec["five_hour"]["resets_at"] == 5001, rec)
    time.sleep(0.02)
    run_statusline(statusline_payload(five_pct=80, five_resets=5000, sid="A"), env)
    rec = json.loads(usage_file.read_text())
    check("the fresh session's next reading still lands (not ignored as 'older')",
          rec["five_hour"]["used_percentage"] == 80, rec)
    time.sleep(0.02)
    run_statusline(statusline_payload(five_pct=3, five_resets=5000 + 18000, sid="A"), env)
    rec = json.loads(usage_file.read_text())
    check("a genuinely new window (resets_at well past the jitter) still replaces",
          rec["five_hour"]["used_percentage"] == 3, rec)

    # Non-finite resets_at is absent; non-finite used_percentage never merges.
    usage_file2 = tmpdir("maestro-usage-") / "usage.json"
    env2 = base_env(usage_file=usage_file2)
    raw = json.dumps(statusline_payload(five_pct=50, five_resets=7000)).replace("7000", "Infinity")
    r = subprocess.run([sys.executable, str(STATUSLINE)], input=raw, capture_output=True,
                       text=True, env=env2)
    check("inf resets_at doesn't crash statusline", r.returncode == 0, r.stderr)
    rec2 = json.loads(usage_file2.read_text())
    check("inf resets_at is stored as absent (None)", rec2["five_hour"]["resets_at"] is None, rec2)
    raw = json.dumps(statusline_payload(five_pct=51, five_resets=None)).replace("51", "Infinity")
    subprocess.run([sys.executable, str(STATUSLINE)], input=raw, capture_output=True,
                   text=True, env=env2)
    rec3 = json.loads(usage_file2.read_text())
    check("inf used_percentage is ignored, prior value kept",
          rec3["five_hour"]["used_percentage"] == 50, rec3)


def usage_top_level_case():
    print("\n=== statusline — top-level captured_at restored; session_id may be null (finding 8) ===")
    usage_file = tmpdir("maestro-usage-") / "usage.json"
    env = base_env(usage_file=usage_file)
    run_statusline(statusline_payload(five_pct=10, five_resets=5000, seven_pct=20,
                                      seven_resets=900000, sid=None), env)
    rec = json.loads(usage_file.read_text())
    check("top-level captured_at present", isinstance(rec.get("captured_at"), (int, float)), rec)
    check("top-level captured_at == latest window capture",
          rec.get("captured_at") == max(rec["five_hour"]["captured_at"],
                                        rec["seven_day"]["captured_at"]), rec)
    check("a payload with no session_id still writes (session_id null)",
          "session_id" in rec and rec["session_id"] is None, rec)
    # sweep_state's snapshot reads per-window capture times without relying on session_id.
    ws = tmpdir("maestro-ws-")
    env2 = base_env(sweeps_dir=ws / "sweeps", usage_file=usage_file)
    plan, items = write_plan_and_items(ws, ["a"])
    run_sweep(["new", "--slug", "u8", "--plan", str(plan), "--items", str(items)], env2)
    run_sweep(["next", "u8"], env2)
    pace = [json.loads(l) for l in (ws / "sweeps" / "u8" / "pace.jsonl").read_text().splitlines() if l.strip()]
    u = pace[-1]["usage"]
    check("pace snapshot carries per-window capture times",
          u["captured"]["five_hour"] == rec["five_hour"]["captured_at"]
          and u["captured"]["seven_day"] == rec["seven_day"]["captured_at"], u)


def stranded_running_case():
    print("\n=== sweep_state — unreported items never stranded as running (finding 4) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["a", "b"])
    run_sweep(["new", "--slug", "s10", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "s10"
    claimed = json.loads(run_sweep(["next", "s10"], env).stdout)
    check("both items claimed", len(claimed) == 2)
    run_sweep(["done", "s10", "i-0001"], env)      # i-0002's agent never reports
    r = run_sweep(["end-chunk", "s10"], env)
    check("end-chunk exits 0", r.returncode == 0, r.stderr)
    check("end-chunk reports the item it returned", json.loads(r.stdout) == ["i-0002"], r.stdout)
    it2 = find(d, "i-0002")
    check("unreported item back to pending, a recovery counted, no attempt spent",
          it2["status"] == "pending" and it2["recoveries"] == 1 and it2["attempts"] == 0, it2)
    r = run_sweep(["next", "s10"], env)
    check("next re-claims it (not exit 3 on an unfinished sweep)",
          r.returncode == 0 and [c["id"] for c in json.loads(r.stdout)] == ["i-0002"], r.stdout)
    run_sweep(["done", "s10", "i-0002"], env)
    run_sweep(["end-chunk", "s10"], env)
    r = run_sweep(["next", "s10"], env)
    check("next exits 3 once nothing is pending or running", r.returncode == 3, r.stderr)
    r = run_sweep(["anchor"], env, input_text=anchor_payload(ws, TEST_SESSION))
    check("anchor stops advertising a finished sweep", r.stdout.strip() == "", r.stdout)

    # Nothing pending but items running in another session's open chunk:
    # not "finished" (3) — busy (4), naming the owner.
    ws2 = tmpdir("maestro-ws-")
    env2 = base_env(sweeps_dir=ws2 / "sweeps")
    plan2, items2 = write_plan_and_items(ws2, ["only"])
    run_sweep(["new", "--slug", "s11", "--plan", str(plan2), "--items", str(items2)], env2)
    run_sweep(["next", "s11", "--owner", "sess-A"], env2)
    r = run_sweep(["next", "s11", "--owner", "sess-B"], env2)
    check("next with nothing pending but items running exits 4, not 3",
          r.returncode == 4 and "sess-A" in r.stderr, f"rc={r.returncode} err={r.stderr}")

    # A legacy strand (running item, chunk already closed) is reclaimed by recover.
    pace_path = ws2 / "sweeps" / "s11" / "pace.jsonl"
    with open(pace_path, "a") as f:
        f.write(json.dumps({"chunk": 1, "event": "end", "at": time.time(), "usage": {},
                            "interrupted": False}) + "\n")
    r = run_sweep(["recover", "s11", "--owner", "sess-B"], env2)
    check("recover reclaims an orphaned running item (no open chunk)",
          r.returncode == 0 and json.loads(r.stdout) == ["i-0001"], f"{r.stdout} {r.stderr}")


def find(d, item_id):
    index = json.loads((d / "index.json").read_text())
    return next(it for it in index["items"] if it["id"] == item_id)


def policy_tamper_case():
    print("\n=== sweep_state — policy tampering is never honoured (finding 5) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"])
    run_sweep(["new", "--slug", "t5", "--plan", str(plan), "--items", str(items),
              "--policy", json.dumps({"deviation": "locked"})], env)
    d = ws / "sweeps" / "t5"
    run_sweep(["finding", "t5", "--text", "want more work"], env)   # f-0001

    # The attack: edit policy.json to autonomous, add, restore, check.
    original = (d / "policy.json").read_text()
    pol = json.loads(original)
    pol["deviation"] = "autonomous"
    (d / "policy.json").write_text(json.dumps(pol, indent=2))
    r = run_sweep(["add", "t5", "--label", "sneaky", "--from-finding", "f-0001"], env)
    check("add refuses an edited policy.json (sha mismatch)",
          r.returncode == 2 and "policy_sha256" in r.stderr, f"rc={r.returncode} err={r.stderr}")
    r = run_sweep(["next", "t5"], env)
    check("next refuses an edited policy.json too", r.returncode == 2, r.stderr)
    (d / "policy.json").write_text(original)
    index = json.loads((d / "index.json").read_text())
    check("no item was added", len(index["items"]) == 1, index["items"])
    r = run_sweep(["check", "t5"], env)
    check("check passes on the untouched sweep", r.returncode == 0, r.stdout)

    # A hand-forged add (amendment + item + hashes) under a locked policy.
    index["items"].append({"id": "i-0002", "label": "forged", "status": "pending",
                           "attempts": 0, "recoveries": 0, "from_finding": "f-0001", "note": None})
    import hashlib
    index["items_sha256"] = hashlib.sha256(json.dumps(
        [[it["id"], it["label"]] for it in index["items"]]).encode()).hexdigest()
    (d / "index.json").write_text(json.dumps(index))
    with open(d / "amendments.jsonl", "a") as f:
        f.write(json.dumps({"at": time.time(), "op": "add_item", "item": "i-0002",
                            "from_finding": "f-0001", "policy": "additive"}) + "\n")
    r = run_sweep(["check", "t5"], env)
    check("check rejects an add_item whose recorded deviation isn't the one in force",
          r.returncode == 1 and "in force was 'locked'" in r.stdout, r.stdout)
    lines = (d / "amendments.jsonl").read_text().replace('"policy": "additive"', '"policy": "locked"')
    (d / "amendments.jsonl").write_text(lines)
    r = run_sweep(["check", "t5"], env)
    check("check rejects an add_item recorded under a deviation that doesn't allow it",
          r.returncode == 1 and "does not allow it" in r.stdout, r.stdout)

    # amend-plan <-> plan_versions reconciliation.
    ws2 = tmpdir("maestro-ws-")
    env2 = base_env(sweeps_dir=ws2 / "sweeps")
    plan2, items2 = write_plan_and_items(ws2, ["one"])
    run_sweep(["new", "--slug", "t5b", "--plan", str(plan2), "--items", str(items2),
              "--policy", json.dumps({"deviation": "autonomous"})], env2)
    d2 = ws2 / "sweeps" / "t5b"
    run_sweep(["finding", "t5b", "--text", "plan gap"], env2)
    newplan = ws2 / "p2.md"
    newplan.write_text("# v2\n")
    r = run_sweep(["amend-plan", "t5b", "--file", str(newplan), "--from-finding", "f-0001"], env2)
    check("amend-plan under autonomous succeeds", r.returncode == 0, r.stderr)
    amends = [json.loads(l) for l in (d2 / "amendments.jsonl").read_text().splitlines() if l.strip()]
    check("amend_plan amendment carries the version's sha and file",
          amends and amends[-1].get("op") == "amend_plan" and amends[-1].get("file") == "plan.v2.md", amends)
    idx2 = json.loads((d2 / "index.json").read_text())
    saved = list(idx2["plan_versions"])
    idx2["plan_versions"] = []
    (d2 / "index.json").write_text(json.dumps(idx2))
    r = run_sweep(["check", "t5b"], env2)
    check("check rejects an amend_plan amendment with no plan_versions entry",
          r.returncode == 1 and "amend_plan" in r.stdout, r.stdout)
    idx2["plan_versions"] = saved
    (d2 / "index.json").write_text(json.dumps(idx2))
    (d2 / "amendments.jsonl").write_text("")
    r = run_sweep(["check", "t5b"], env2)
    check("check rejects a plan version with no amend_plan amendment",
          r.returncode == 1 and "amend_plan" in r.stdout, r.stdout)


def set_policy_case():
    print("\n=== sweep_state — set-policy: validated, versioned, chain-checked (finding 6) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    plan, items = write_plan_and_items(ws, ["one"])
    run_sweep(["new", "--slug", "t6", "--plan", str(plan), "--items", str(items)], env)
    d = ws / "sweeps" / "t6"
    check("new writes policy.v1.json", (d / "policy.v1.json").read_text() == (d / "policy.json").read_text())

    sha_before = json.loads((d / "index.json").read_text())["policy_sha256"]
    r = run_sweep(["set-policy", "t6", "--json", json.dumps({"chunk_size": 0})], env)
    check("set-policy rejects an invalid policy (exit 2)", r.returncode == 2, r.stderr)
    check("…and changes nothing",
          json.loads((d / "index.json").read_text())["policy_sha256"] == sha_before
          and not (d / "policy.v2.json").exists())

    r = run_sweep(["set-policy", "t6", "--json", json.dumps({"deviation": "locked", "chunk_size": 2})], env)
    check("set-policy exits 0", r.returncode == 0, r.stderr)
    pol = json.loads((d / "policy.json").read_text())
    check("policy.json is the merged new policy",
          pol["deviation"] == "locked" and pol["chunk_size"] == 2 and pol["ceilings"]["five_hour"] == 80, pol)
    index = json.loads((d / "index.json").read_text())
    check("policy_versions has v1 and v2", [v["version"] for v in index["policy_versions"]] == [1, 2],
          index.get("policy_versions"))
    check("policy.v2.json matches the current policy_sha256",
          (d / "policy.v2.json").read_text() == (d / "policy.json").read_text()
          and index["policy_versions"][-1]["sha256"] == index["policy_sha256"])
    amends = [json.loads(l) for l in (d / "amendments.jsonl").read_text().splitlines() if l.strip()]
    check("a set_policy amendment is recorded",
          any(a.get("op") == "set_policy" and a.get("deviation") == "locked"
              and a.get("from_deviation") == "additive" for a in amends), amends)
    r = run_sweep(["check", "t6"], env)
    check("check passes after set-policy", r.returncode == 0, r.stdout)
    run_sweep(["finding", "t6", "--text", "x"], env)
    r = run_sweep(["add", "t6", "--label", "no", "--from-finding", "f-0001"], env)
    check("the new (locked) policy is enforced", r.returncode == 2, r.stderr)

    # A tampered policy.json can be repaired only with a complete policy.
    (d / "policy.json").write_text("{\"deviation\": \"autonomous\"}")
    r = run_sweep(["check", "t6"], env)
    check("check fails on the tampered policy.json", r.returncode == 1, r.stdout)
    r = run_sweep(["set-policy", "t6", "--json", json.dumps({"deviation": "additive"})], env)
    check("set-policy refuses a partial policy when the current one can't be trusted",
          r.returncode == 2 and "complete policy" in r.stderr, r.stderr)
    full = dict(pol, deviation="additive")
    r = run_sweep(["set-policy", "t6", "--json", json.dumps(full)], env)
    check("set-policy with a complete policy repairs it", r.returncode == 0, r.stderr)
    r = run_sweep(["check", "t6"], env)
    check("check passes after the repair (the remedy SKILL.md points to)", r.returncode == 0, r.stdout)

    # Chain tampering is caught.
    (d / "policy.v2.json").write_text("{}")
    r = run_sweep(["check", "t6"], env)
    check("check fails when a policy version file was edited",
          r.returncode == 1 and "policy.v2.json" in r.stdout, r.stdout)
    (d / "policy.v2.json").write_text((d / "policy.v1.json").read_text())


def missing_sweep_exit_codes_case():
    print("\n=== sweep_state — a missing sweep is exit 2 'no such sweep', never 5 'busy' (finding 7) ===")
    ws = tmpdir("maestro-ws-")
    env = base_env(sweeps_dir=ws / "sweeps")
    env["MAESTRO_LOCK_TIMEOUT_S"] = "0.2"
    (ws / "sweeps").mkdir()
    newplan = ws / "p.md"
    newplan.write_text("x\n")
    cmds = [["next", "typo"], ["recover", "typo"], ["end-chunk", "typo"], ["done", "typo", "i-0001"],
            ["fail", "typo", "i-0001", "--reason", "x"], ["finding", "typo", "--text", "x"],
            ["add", "typo", "--label", "x", "--from-finding", "f-0001"],
            ["amend-plan", "typo", "--file", str(newplan), "--from-finding", "f-0001"],
            ["set-policy", "typo", "--json", "{}"], ["status", "typo"]]
    for args in cmds:
        r = run_sweep(args, env)
        check(f"{args[0]} on a missing sweep exits 2 with 'no such sweep'",
              r.returncode == 2 and "no such sweep" in r.stderr, f"rc={r.returncode} err={r.stderr}")
    check("no sweep directory was created by a typo", not (ws / "sweeps" / "typo").exists())


def main():
    usage_snapshot_case()
    usage_merge_case()
    tmp_cleanup_on_replace_failure_case()
    stdout_unchanged_case()
    lifecycle_case()
    locked_add_case()
    amend_plan_case()
    recover_case()
    next_drained_case()
    two_owner_recover_case()
    end_chunk_owner_case()
    check_gate_case()
    ghost_add_case()
    unknown_finding_refused_case()
    policy_validation_case()
    lock_timeout_case()
    atomic_writes_case()
    anchor_case()
    anchor_owner_case()
    owner_required_case()
    anchor_corrupt_pace_case()
    reset_jitter_case()
    usage_top_level_case()
    stranded_running_case()
    policy_tamper_case()
    set_policy_case()
    missing_sweep_exit_codes_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)}): {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
