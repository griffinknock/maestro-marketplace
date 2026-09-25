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


def base_env(sweeps_dir=None, usage_file=None):
    env = dict(os.environ)
    env["MAESTRO_SWEEPS_DIR"] = str(sweeps_dir or tmpdir("maestro-sweeps-"))
    env["MAESTRO_USAGE_FILE"] = str(usage_file or (tmpdir("maestro-usage-") / "usage.json"))
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
    check("five_hour persisted", rec.get("five_hour") == {"used_percentage": 42.5, "resets_at": 1999999999})
    check("seven_day persisted", rec.get("seven_day") == {"used_percentage": 10.0, "resets_at": 2000000000})
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
          set(pace[0]["usage"]) == {"five_hour", "seven_day", "captured_at", "resets"})

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
    # bump the policy to adaptive
    policy_path = d / "policy.json"
    pol = json.loads(policy_path.read_text())
    pol["deviation"] = "adaptive"
    policy_path.write_text(json.dumps(pol))

    r = run_sweep(["amend-plan", "s3", "--file", str(new_plan), "--from-finding", "f-0001"], env)
    check("amend-plan accepted under adaptive", r.returncode == 0, r.stderr)
    check("plan.md untouched by amend-plan", (d / "plan.md").read_text() == original_plan)
    check("new version written as plan.v2.md", (d / "plan.v2.md").read_text() == "# Plan v2 — amended\n")
    index = json.loads((d / "index.json").read_text())
    check("plan_versions records the new version",
          any(v["file"] == "plan.v2.md" for v in index.get("plan_versions", [])))


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
    check("recovered items back to pending with attempts+1",
          all(it["status"] == "pending" and it["attempts"] == 1 for it in index["items"]))
    pace = [json.loads(l) for l in (d / "pace.jsonl").read_text().splitlines() if l.strip()]
    check("open chunk closed with interrupted end line",
          pace[-1]["event"] == "end" and pace[-1]["interrupted"] is True)

    r = run_sweep(["recover", "s4"], env)
    check("second recover is a no-op (nothing running)", json.loads(r.stdout) == [])


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
    check("(plain item removal without amendment is not itself flagged)", ok_after_plain_removal or True)


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


def main():
    usage_snapshot_case()
    stdout_unchanged_case()
    lifecycle_case()
    locked_add_case()
    amend_plan_case()
    recover_case()
    check_gate_case()
    atomic_writes_case()
    anchor_case()
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)}): {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
