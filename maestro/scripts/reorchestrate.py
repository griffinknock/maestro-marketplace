#!/usr/bin/env python3
"""Maestro re-orchestrator — spot-checks parallelism every time a task finishes.

Wired to SubagentStop and TaskCompleted. Runs deterministic rules in a few
milliseconds and pushes the verdict straight into the conductor's context via
`hookSpecificOutput.additionalContext`, so it reads as a system reminder rather
than as noise in the transcript.

The hook NEVER blocks. When the rules smell something they can't judge, it
spawns a detached Haiku call that writes its opinion to verdict.json; the next
completion picks that up and injects it. So the expensive check is always one
task behind, and never in the critical path.

Env:
  MAESTRO_REORCH_LLM=0     rules only, never spend a Haiku call
  MAESTRO_REORCH_COOLDOWN  seconds between Haiku calls (default 90)
  MAESTRO_DEBUG=1          errors to stderr
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEBUG = os.environ.get("MAESTRO_DEBUG") == "1"
USE_LLM = os.environ.get("MAESTRO_REORCH_LLM", "1") != "0"
COOLDOWN = float(os.environ.get("MAESTRO_REORCH_COOLDOWN", "90"))
STALL_SECONDS = 300          # no tool activity this long = probably stuck
VERDICT_TTL = 240            # ignore an LLM opinion older than this


def state_dir(cwd):
    p = Path(cwd or ".").resolve()
    for parent in [p, *p.parents]:
        ptr = parent / ".claude" / "maestro" / "current"
        if ptr.is_file():
            try:
                d = Path(ptr.read_text().strip())
                if (d / "state.json").is_file():
                    return d
            except OSError:
                return None
    return None


def lanes(state):
    """A lane is a direct child of the conductor, plus everything under it."""
    nodes = state.get("nodes", {})
    out = {}
    for n in nodes.values():
        if n["id"] == "root":
            continue
        cur, guard = n, 0
        while cur.get("parent") and cur["parent"] != "root" and guard < 8:
            cur = nodes.get(cur["parent"], {})
            guard += 1
            if not cur:
                break
        head = (cur or n).get("id", n["id"])
        out.setdefault(head, []).append(n)
    return out


def check(state):
    """Deterministic rules. Returns (findings, suspicious)."""
    nodes = [n for n in state.get("nodes", {}).values() if n["id"] != "root"]
    live = [n for n in nodes if n.get("status") in ("running", "spawning")]
    now = time.time()
    f, suspicious = [], False

    # 1. Serial drift — the failure mode Griffin actually complained about.
    if len(live) == 1:
        f.append(f"Only 1 agent in flight ({live[0]['type']}). If anything else on the "
                 f"plan does not consume its output, launch it now in the same message.")
        suspicious = True
    elif not live and nodes:
        # `and nodes` matters: on a tree where nothing has run yet there is no
        # wave to nudge about, and firing here made every session open with a
        # re-check nag before the conductor had dispatched anything.
        f.append("No agents in flight. Either the wave is genuinely done and you should "
                 "merge, or the next wave should already be launching.")

    # 2. Stalled agents.
    for n in live:
        since = now - float(n.get("last_activity") or n.get("started") or now)
        if since > STALL_SECONDS:
            f.append(f"{n['type']} (lane {n.get('lane', '?')}) has had no tool activity "
                     f"for {int(since/60)}m — check it or stop it.")
            suspicious = True

    # 3. Lane balance.
    ls = lanes(state)
    busy = {k: [n for n in v if n.get("status") in ("running", "spawning")]
            for k, v in ls.items()}
    idle_lanes = [k for k, v in busy.items() if not v]
    if len(ls) > 1 and len(idle_lanes) == len(ls) - 1 and live:
        f.append(f"{len(idle_lanes)} of {len(ls)} lanes are idle while one keeps working. "
                 f"That is a fan-out that collapsed into a queue — re-split it.")
        suspicious = True

    # 4. Worktree collisions — two live agents editing the same file.
    seen = {}
    for n in live:
        for path in n.get("files", []):
            if path in seen and seen[path] != n["id"]:
                f.append(f"Two live agents touched {path.split('/')[-1]} — serialize them "
                         f"or the merge will conflict.")
                suspicious = True
                break
            seen[path] = n["id"]

    # 5. Depth budget.
    deepest = max((n.get("depth", 0) for n in live), default=0)
    if deepest >= 5:
        f.append("Depth 5 reached — nothing below this level may delegate further.")
    elif deepest == 4:
        f.append("Depth 4 in flight. One more level is all you have.")

    # 6. Cheap-work-on-expensive-model.
    for n in live:
        tools = n.get("tools") or {}
        reads = sum(v for k, v in tools.items() if k in ("Read", "Grep", "Glob"))
        if n.get("model") == "opus" and reads > 6 and not any(
                k in tools for k in ("Edit", "Write", "MultiEdit")):
            f.append(f"{n['type']} is on opus but has only been reading "
                     f"({reads} calls). That is scout work — consider handing it to haiku.")
            suspicious = True

    # 7. Failures worth escalating.
    failed = [n for n in nodes if n.get("status") == "failed"]
    repeat = {}
    for n in failed:
        repeat[n["type"]] = repeat.get(n["type"], 0) + 1
    for t, c in repeat.items():
        if c >= 2:
            f.append(f"{t} has failed {c} times. Escalate to surgeon with both failure "
                     f"transcripts rather than retrying the same tier.")
            suspicious = True

    return f, suspicious


def spawn_llm(d, state):
    """Detached Haiku second opinion. Its answer lands on the next completion."""
    stamp = d / ".llm-last"
    try:
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < COOLDOWN:
            return
        stamp.write_text(str(time.time()))
    except OSError:
        return

    live = [{k: n.get(k) for k in ("type", "model", "status", "depth", "description")}
            for n in state.get("nodes", {}).values() if n["id"] != "root"]
    brief = json.dumps(live[:40])
    prompt = (
        "You are auditing an agent orchestration for wasted parallelism. Here is the "
        f"current agent tree as JSON:\n{brief}\n\n"
        "Answer in at most 3 short lines, no preamble. Name only concrete, actionable "
        "problems: work that is running in sequence but has no dependency between the "
        "pieces, a task assigned to a more expensive model than it needs, or a lane that "
        "should have been split further. If the shape looks efficient, reply exactly: OK. "
        "Do not speculate about code correctness — only about the orchestration shape."
    )
    try:
        # MAESTRO_LEDGER_OFF stops this audit session from writing its own
        # ledger; MAESTRO_REORCH_LLM=0 stops it recursing into another audit.
        env = {**os.environ, "MAESTRO_REORCH_LLM": "0",
               "MAESTRO_LEDGER_OFF": "1", "MAESTRO_QUIET": "1"}
        subprocess.Popen(
            ["bash", "-lc",
             f"claude -p {json.dumps(prompt)} --model haiku --output-format text "
             f"> {json.dumps(str(d / 'verdict.tmp'))} 2>/dev/null && "
             f"mv {json.dumps(str(d / 'verdict.tmp'))} {json.dumps(str(d / 'verdict.txt'))}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


def take_verdict(d):
    f = d / "verdict.txt"
    try:
        if not f.is_file() or time.time() - f.stat().st_mtime > VERDICT_TTL:
            return None
        txt = f.read_text().strip()
        f.unlink()
        if not txt or txt.upper().startswith("OK"):
            return None
        return txt[:600]
    except OSError:
        return None


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    d = state_dir(payload.get("cwd"))
    if not d:
        return
    try:
        state = json.loads((d / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return

    findings, suspicious = check(state)
    verdict = take_verdict(d)
    if suspicious and USE_LLM:
        spawn_llm(d, state)

    if not findings and not verdict:
        return

    live = sum(1 for n in state.get("nodes", {}).values()
               if n["id"] != "root" and n.get("status") in ("running", "spawning"))
    lines = [f"MAESTRO RE-CHECK — {live} agent(s) in flight, "
             f"{len(lanes(state))} lane(s) open."]
    lines += [f"- {x}" for x in findings[:6]]
    if verdict:
        lines.append(f"- second opinion: {verdict}")
    lines.append("Act on anything above before starting the next wave. If none of it "
                 "applies, ignore this and continue.")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": payload.get("hook_event_name") or "SubagentStop",
            "additionalContext": "\n".join(lines),
        },
        "suppressOutput": True,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if DEBUG:
            print(f"maestro reorchestrate: {e}", file=sys.stderr)
    sys.exit(0)
