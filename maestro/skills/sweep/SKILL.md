---
name: sweep
description: Usage-aware sweep for long, slow runs — freeze a plan and an item index once, work it in checkpointed chunks paced against real rate-limit usage, and resume exactly after any interruption.
argument-hint: <new <goal> | run <slug> | resume <slug> | takeover <slug> | status [slug] | stop <slug>>
---

Sweep command: **$ARGUMENTS**

A sweep is a frozen plan + an item index + a policy, worked in small chunks
between pauses that `pace.py` schedules from real usage — not a fixed
interval. `sweep_state.py` is the checkpoint store; `pace.py` is the pacing
brain. Neither is yours to edit; call them as documented in their own
docstrings.

```bash
STATE="${CLAUDE_PLUGIN_ROOT}/scripts/sweep_state.py"
PACE="${CLAUDE_PLUGIN_ROOT}/scripts/pace.py"
```

**ScheduleWakeup only exists inside a self-paced `/loop`.** It is not a tool
you can call from a bare `/maestro:sweep run` — it is only offered to you
when the current turn is running under `/loop` with no fixed interval. That
means `run` and `resume` are only ever entered this way:

```
/loop /maestro:sweep run <slug>
```

```
/loop /maestro:sweep resume <slug>
```

Self-paced `/loop` always re-fires the *same original prompt text* on the
next iteration, so that exact line is the whole resume prompt: say it once
when you start the loop, and every later wakeup, `stop`'s printed resume
command, and the SessionStart anchor line (`Resume with /loop
/maestro:sweep resume <slug>`) all resolve to the same slug and the same
subcommand shape. If you are asked to `run`/`resume` outside a `/loop`
turn, do one recover→check→pace round honestly, then tell Griffin the
sleep/stop decision cannot self-schedule here and hand him the
`/loop /maestro:sweep run <slug>` line to paste.

## OWNER — this session's id, never a token you make up

`next`, `recover` and `end-chunk` act for an owner: the id `recover` uses to
tell "this session holds the open chunk" from "some other live session
does". The owner is the Claude Code session id, and you never type it:
Claude Code exports it to every Bash-tool command as
`$CLAUDE_CODE_SESSION_ID` —
[env-vars reference](https://code.claude.com/docs/en/env-vars): "Set
automatically to the current session ID in Bash and PowerShell tool
subprocesses, hook command subprocesses, and stdio MCP server subprocesses.
For Bash, PowerShell, and hooks this matches the `session_id` field in the
hook JSON input" — and `sweep_state.py` reads it itself. So:

- Never pass `--owner` and never mint an owner token. Every wakeup of this
  `/loop`, and every turn after a compaction, is the same session and so
  the same owner; a second session is a different owner by construction.
- If a command refuses with `no owner — $CLAUDE_CODE_SESSION_ID is unset`,
  this Claude Code build doesn't export it: stop and tell Griffin — do not
  work around it with a made-up `--owner`.
- `/clear` gives the session a new id. A chunk left open before a `/clear`
  then belongs to "another session" — use `takeover` below.

The SessionStart anchor (on compaction or resume) reads the hook's own
`session_id` and prints one of two lines per active sweep:

- to the session that owns the open chunk, or to any session when no
  chunk is open: `Resume with /loop /maestro:sweep resume <slug>`
- to any other session while a chunk is open: that the sweep is owned by
  another session since `<time>`, ending `Take over with /maestro:sweep
  takeover <slug>` — do not resume it; tell Griffin, and only take it over
  if he says the other session is dead.

Trust the anchor's line over anything you remember from before the
compaction.

## Exit codes (`sweep_state.py`)

| code | meaning | what you do |
|------|---------|-------------|
| 0 | ok | carry on |
| 1 | `check` printed `SWEEP FAIL` | stop; report the reasons verbatim; do not proceed |
| 2 | invalid input, refused, or `no such sweep` | stop and report the message verbatim — never retry with guessed arguments |
| 3 | `next`: nothing pending or running — the sweep is finished | go to the final summary and end the loop |
| 4 | owned by another live session (`busy: …`) | quote the busy line, explain `/maestro:sweep takeover <slug>` (only if that session is dead), and end — no `check`, no `next`, no `ScheduleWakeup` |
| 5 | the sweep lock is busy | touch nothing else; `ScheduleWakeup` at the minimum delay (60 s) and try the turn again |

## `new <goal>` — brainstorm-style intake

1. **Recon first.** Fan out cheap `scout` (haiku) agents in one message —
   what already enumerates these items (a script, a glob, an API), what
   agent/model would actually execute one item, what "done" looks like for
   one. Nothing gets asked until recon lands.

2. **One question per message**, in the ✋ format from the output style —
   register `.claude/maestro/<session-id-prefix>/question.json` before you
   print it, numbered options with your pick, then stop and wait. Pin these,
   in this order, before writing anything:
   - **Goal** — one line, what done looks like for the whole sweep.
   - **Item index** — how the index is produced (a scout enumerates it, or a
     script does). Show Griffin the count and a 5-item sample before he
     signs off; do not invent items yourself.
   - **Per-item method** — which agent type and model runs one item, and the
     exact one-line result shape it must return (e.g. `<id> done <result>` /
     `<id> fail <reason>`).
   - **Per-item done criteria** — how you can tell one item is actually
     finished, not just attempted.
   - **Chunk size** — items claimed per `next` call.
   - **Ceilings** — `five_hour` / `seven_day` usage percentages (default 80 /
     90 — only deviate on an explicit reason).
   - **Deviation level** — `locked` (findings only logged) / `additive`
     (findings may add items, never change the plan or drop items — this is
     the default) / `adaptive` (may amend the plan after Griffin approves a
     non-blocking question) / `autonomous` (may amend freely, logged).

3. **Write and register the sweep.** Draft `plan.md` (the frozen plan — goal,
   method, done criteria, one paragraph) and an items file (one label per
   line, or a JSON list) to scratch paths, then:

   ```bash
   python3 "$STATE" new --slug <slug> --plan <plan.md> --items <items.txt> \
     --policy '{"ceilings":{"five_hour":80,"seven_day":90},"deviation":"additive",
                "chunk_size":5,"allow_blind":false,"blind_gap_s":900}'
   ```

   `allow_blind` (default `false`) and `blind_gap_s` (default `900`) govern
   what `pace.py` does when it gets no fresh usage reading — see the "no
   usage signal" case in the `run`/`resume` section below. Omit them to take
   the defaults. `new` exits `2` and writes nothing if the policy is
   invalid — fix the JSON and re-run.

4. **Show a summary** — slug, item count, method, ceilings, deviation — and
   ask before starting:

   ```
   /loop /maestro:sweep run <slug>
   ```

## `run` / `resume` — the work loop

Runs only under `/loop` (see above). Each turn:

1. `python3 "$STATE" recover <slug>` — closes any chunk this session left
   open (a crash, a compaction mid-chunk) and returns its unreported items
   to pending. Act on its exit code before anything else:
   - **0** → `python3 "$STATE" check <slug>`. `SWEEP FAIL` (exit 1) → stop,
     report the reasons, do not proceed.
   - **4** (`busy: chunk <n> owned by <owner> since <iso> …`) → another
     session holds an open chunk. Quote the busy line verbatim, tell Griffin
     how to take over if that session is dead (`/maestro:sweep takeover
     <slug>`, then restart the loop), and end — no `check`, no `next`, no
     `ScheduleWakeup`. A foreign chunk is also reclaimed automatically once
     it is older than `--stale-after` (default 7200 s); never lower that to
     get past a busy line — `takeover` is the explicit, logged way.
   - **5** → reschedule at the minimum delay (60 s); nothing else this turn.
   - **2** → stop and report the message (a typo'd slug is `no such sweep`).

   Never run `check`, `next` or `pace.py` in a turn where `recover` did not
   exit 0.
2. Work chunks (step 4 below) until either `next` exits `3` (nothing
   pending or running) or you've worked 3 chunks this turn. Either way, go
   straight to the final summary: `python3 "$STATE" status <slug> --json`
   for the done/failed/pending counts — never hand-count from your own
   transcript — plus findings and items added. If you stopped because
   `next` exited 3, that is the sweep's natural end: end the loop with
   `ScheduleWakeup(stop: true)`, no reschedule.
3. Otherwise ask pace. The sweep directory is `$MAESTRO_SWEEPS_DIR/<slug>` if
   that env var is set, else `<git toplevel of cwd>/.claude/maestro/sweeps/<slug>`
   — the same resolution `sweep_state.py` uses internally. Pass it as
   `--sweep`:

   ```bash
   python3 "$PACE" --sweep <sweep-dir>
   ```

   Read the one JSON decision line: `action` is `continue` / `sleep` /
   `stop` / `probe` (`probe` means: run exactly one item, to get a usage
   reading). `pace.py` returns `stop` for four distinct reasons — an
   invalid policy, any internal pace error, a real ceiling (almost always
   `seven_day`), or "no usage signal" after 3 chunks in a row ended with no
   fresh usage reading and `allow_blind` false (with `allow_blind` true, it
   sleeps `blind_gap_s` instead of stopping) — read `reason` to tell them
   apart; step 6 below writes a different handoff for each.

4. **`continue` or `probe`:**
   ```bash
   python3 "$STATE" next <slug> --n <chunk_size>   # probe: --n 1
   ```
   **Exit `3`** (prints `[]`, writes nothing): nothing is pending or
   running. Stop claiming chunks right there — even if this is the first
   chunk of the turn — and go to step 2's final summary. **Exit `4`**
   (prints `[]`): nothing is pending but items are still running in another
   session's open chunk — the sweep is *not* finished; handle it like
   `recover`'s exit 4 and end the turn.

   Otherwise dispatch every claimed item as its own agent, **all in the same
   message** (§4 of the output style — this is a fan-out, not a queue), per
   the plan's pinned method (agent type, model, done criteria). Each agent's
   reply must be exactly one line — you do not read a full transcript per
   item, that is the entire point of the checkpoint contract. Record each:
   ```bash
   python3 "$STATE" done <slug> <item-id> [--note "<one line>"]
   python3 "$STATE" fail <slug> <item-id> --reason "<one line>"
   ```
   A finding an item surfaces:
   ```bash
   python3 "$STATE" finding <slug> --text "<one line>" [--item <item-id>]
   ```
   Under `additive`, discovered work becomes a new item, never a plan edit:
   ```bash
   python3 "$STATE" add <slug> --label "<one line>" --from-finding <finding-id>
   ```
   Under `adaptive`, do not call `amend-plan` yet — ask Griffin first with a
   **non-blocking** (`"blocking": false`) ✋ question carrying the proposed
   diff; only on a yes:
   ```bash
   python3 "$STATE" amend-plan <slug> --file <new-plan.md> --from-finding <finding-id>
   ```
   Under `autonomous`, amend freely and log it the same way, no question.
   Under `locked`, `add` refuses at the tool level — log the finding and move
   on. `add` and `amend-plan` both refuse a `--from-finding` that `finding`
   never printed. Then close the chunk and ask pace again:
   ```bash
   python3 "$STATE" end-chunk <slug>
   ```
   `end-chunk` prints the ids of any claimed items that never got a
   `done`/`fail` (an agent that never reported): they go back to pending
   for a later chunk, so mention them in your summary — never mark them
   done yourself.

   **Run at most 3 chunks in one turn.** Every chunk you work adds its
   items' one-line results to your own transcript, which is re-read on every
   later turn in this session — 3 chunks keeps that bounded while still
   making real progress between wakeups; a sweep with hundreds of items
   should take many turns, not one giant one. Stop claiming new chunks the
   moment any of these hits: the 3-chunk bound, `next` exiting 3 or 4, or
   pace saying anything other than `continue`. If it was the 3-chunk bound
   **and pace's last answer this turn was `continue` or `probe`** — there's
   more to do and no usage reason to wait — use step 5's
   reschedule-at-the-floor case, not the normal sleep math. Otherwise carry
   pace's actual last decision into step 5/6.

5. **`sleep`:** call `ScheduleWakeup` with `delaySeconds` = the decision's
   `delay_s` and `reason` = the decision's `reason`. Do not invent your own
   delay — `pace.py` already clamped it to [60, 3600]; a longer real wait
   just means you get woken and immediately call `pace.py` again, which
   returns another `sleep` with a fresh (shorter) delay, chaining until it's
   actually time.

   **Reschedule at the floor** (3-chunk bound hit while pace still says
   `continue` or `probe`, or `sweep_state.py` exited 5): call
   `ScheduleWakeup(delaySeconds: 60, reason: "<why> — rescheduling at the
   minimum delay")`. State this explicitly in your summary — it is the
   transcript-size cap (or a busy lock) talking, not a usage-driven sleep,
   and 60s is `ScheduleWakeup`'s own floor, not a pace estimate.

6. **`stop`:** write a short sweep handoff, then end the loop with
   `ScheduleWakeup(stop: true)` — do not reschedule. Every handoff carries
   progress (done/failed/pending counts from `status --json`), the resume
   line (`/loop /maestro:sweep resume <slug>`), and a reason + remedy pair
   matched to *why* pace said stop:
   - **weekly (`seven_day`) ceiling** — reason: the ceiling and when that
     window resets (from the decision). Remedy: nothing to do but wait;
     resume after the reset with the line above.
   - **invalid policy** — reason: quote pace's error verbatim. Remedy:
     Griffin changes the policy with `set-policy` (below); then resume.
     Never edit `policy.json` by hand — `check` and every command refuse an
     edited one.
   - **internal pace error** — reason: quote the error verbatim. Remedy:
     this is a bug in `pace.py` or its inputs, not a usage limit — do not
     retry blindly; flag the exact error text to Griffin.
   - **no usage signal** (3+ chunks in a row with no fresh reading,
     `allow_blind` false) — reason: pacing is blind because usage.json
     never got a fresh statusline reading (not installed, or this run is
     headless). Remedy: "run install.sh to set Maestro's statusLine" — that
     is what produces the usage snapshot `pace.py` reads — or, only if
     Griffin asks for it, `set-policy` with `allow_blind`. Then resume with
     the line above.

The conductor never reads an item's full output — one line per item, ever.
Anything more belongs in the sweep dir (`findings.jsonl`, `pace.jsonl`), not
your transcript.

## Changing a sweep's policy — `set-policy`

Only on Griffin's explicit request (a ceiling, the deviation level,
`allow_blind`, …) — never on your own initiative, and never to get past a
refusal:

```bash
python3 "$STATE" set-policy <slug> --json '{"deviation":"adaptive"}'
```

It validates, merges onto the current policy, appends a new policy version
(`policy.vN.json`, a `set_policy` amendment) and makes it current; `check`
verifies the whole chain. If `policy.json` was edited by hand, `set-policy`
needs a complete policy in `--json` (it won't merge onto an untrusted file).
Exit 2 means the policy was invalid and nothing changed.

## `takeover <slug>`

Only when Griffin says the session that owns the open chunk is dead (the
anchor or a `busy:` line named it). Not a loop turn — run once:

```bash
python3 "$STATE" recover <slug> --takeover
```

It closes the other session's open chunk as interrupted, returns its
unreported items to pending, logs a `takeover` amendment, and makes this
session the owner of the next chunk. Then hand Griffin the
`/loop /maestro:sweep resume <slug>` line to restart the loop in this
session.

## `status [slug]`

No slug: `python3 "$STATE" list`. With a slug: `python3 "$STATE" status <slug>`
plus the latest `pace.py` decision for that sweep (same `<sweep-dir>`
resolution as above), so Griffin sees both where it is and what it would do
next.

## `stop <slug>`

Marks nothing in the sweep itself — `stop` here means *end the loop
cleanly*, not abandon the sweep. If this turn is running under `/loop`, call
`ScheduleWakeup(stop: true)`. Print the counts and the resume command:
```
/loop /maestro:sweep resume <slug>
```
