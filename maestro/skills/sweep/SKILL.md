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

## Every iteration ends in exactly one of two calls

A `/loop` iteration must finish by **rescheduling** (`lease <slug> --in`
then `ScheduleWakeup(delaySeconds: …)`, see step 5) or by **ending the
loop**. Never let a turn just stop talking: per the
[scheduled-tasks docs](https://code.claude.com/docs/en/scheduled-tasks),
"If an iteration ends without either rescheduling or stopping, Claude Code
schedules one fallback wakeup about 20 minutes later" — a zombie iteration
that could wake up on a sweep another session has taken over since.

**END the loop** means, in this order:

1. `python3 "$STATE" lease <slug> --release` — only when this session may
   hold the lease (skip it when a `busy:` line says another session holds
   it; an exit 4 from the release itself just means the lease isn't ours).
2. `ScheduleWakeup(stop: true)` — cancels the pending wakeup, so no
   fallback wakeup fires. Always, on every terminal path below, including
   error paths.

## OWNER — this session's id, never a token you make up

`next`, `recover`, `end-chunk`, `done`, `fail` and `lease` act for an owner:
the id that tells "this session holds the open chunk" from "some other live
session does". The owner is the Claude Code session id, and you never type
it: Claude Code exports it to every Bash-tool command as
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
  this Claude Code build doesn't export it: tell Griffin and END with
  `ScheduleWakeup(stop: true)` — do not work around it with a made-up
  `--owner`.
- **`/clear` changes the session id** (the env-vars reference: it "is
  updated on `/clear`"). After a `/clear`, a chunk left open or a loop
  lease held by the pre-`/clear` session belongs to "another session":
  `recover`/`next` exit 4 until it lapses, and the way through is
  `takeover` below — the pre-`/clear` loop is gone, so that is safe once
  Griffin confirms it.

### The loop lease

An open chunk only proves ownership while it is open; a `/loop` sleeping
between chunks holds nothing on its own. So the loop also holds a lease
(`lease.json`: owner + until): `next`, `done`, `fail` and `end-chunk` renew
it to at least 10 minutes ahead, and **right before every `ScheduleWakeup`**
you renew it to cover the wait:

```bash
python3 "$STATE" lease <slug> --in <delay_s>   # holds until now + delay_s + 10 min
```

When the loop ends, the END procedure above releases it so another session
may resume right away:

```bash
python3 "$STATE" lease <slug> --release
```

While a lease is live, `recover`, `next` and `lease` from any other
session exit 4 (`busy: sweep leased by <owner> until <iso> …`) — the same
takeover path as an open chunk. A dead loop's lease lapses on its own 10
minutes after its last scheduled wake. If `lease` itself exits 4, another
session has taken the sweep over: tell Griffin and END with
`ScheduleWakeup(stop: true)` (skip the release — it isn't ours).

The SessionStart anchor (on compaction or resume) reads the hook's own
`session_id` and prints one of two lines per active sweep:

- to the session that owns the open chunk / live lease, or to any session
  when neither is held: `Resume with /loop /maestro:sweep resume <slug>`
- to any other session: that the sweep is owned (open chunk, since
  `<time>`) or leased (sleeping loop, until `<time>`) by another session,
  ending `Take over with /maestro:sweep takeover <slug>` — do not resume
  it; tell Griffin, and only take it over if he says the other session is
  dead.

Trust the anchor's line over anything you remember from before the
compaction.

## Exit codes (`sweep_state.py`)

| code | meaning | what you do |
|------|---------|-------------|
| 0 | ok | carry on |
| 1 | `check` printed `SWEEP FAIL` | stop and report the reasons verbatim, then END with `ScheduleWakeup(stop: true)` |
| 2 | invalid input, refused, or `no such sweep` | stop and report the message verbatim — never retry with guessed arguments — then END with `ScheduleWakeup(stop: true)` |
| 3 | `next`: nothing pending or running — the sweep is finished | final summary, then END with `ScheduleWakeup(stop: true)` |
| 4 | owned by another live session — its open chunk or live loop lease (`busy: …`); from `done`/`fail`: that item is no longer this session's | quote the busy line, explain `/maestro:sweep takeover <slug>` (only if that session is dead), and END with `ScheduleWakeup(stop: true)` — no `check`, no `next`, no release (for `done`/`fail`, see step 4) |
| 5 | the sweep lock is busy | `done`/`fail`: retry that same call (up to 3 times, a few seconds apart) — never drop a result. Anything else: touch nothing more; reschedule at the minimum delay (60 s) and try the turn again |

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
   open (a crash, a compaction mid-chunk), returns its unreported items to
   pending, and finishes a `set-policy` a crash interrupted. Act on its exit
   code before anything else:
   - **0** → `python3 "$STATE" check <slug>`. `SWEEP FAIL` (exit 1) → stop
     and report the reasons, then END with `ScheduleWakeup(stop: true)`.
   - **4** (`busy: chunk <n> owned by <owner> since <iso> …` or `busy: sweep
     leased by <owner> until <iso> …`) → another session holds an open chunk
     or a live loop lease. Quote the busy line verbatim, tell Griffin how to
     take over if that session is dead (`/maestro:sweep takeover <slug>`,
     then restart the loop), and END with `ScheduleWakeup(stop: true)` (no
     release — the lease isn't ours; no `check`, no `next`). A foreign
     chunk is also reclaimed automatically once it is older than
     `--stale-after` (default 7200 s) and its lease has lapsed; never lower
     that to get past a busy line — `takeover` is the explicit, logged way.
   - **5** → reschedule at the minimum delay (60 s); nothing else this turn.
   - **2** → stop and report the message (a typo'd slug is `no such sweep`),
     then END with `ScheduleWakeup(stop: true)`.

   Never run `check`, `next` or `pace.py` in a turn where `recover` did not
   exit 0.
2. Work chunks (step 4 below) until either `next` exits `3` (nothing
   pending or running) or you've worked 3 chunks this turn. Either way, go
   straight to the final summary: `python3 "$STATE" status <slug> --json`
   for the done/failed/pending counts — never hand-count from your own
   transcript — plus findings and items added. If you stopped because
   `next` exited 3, that is the sweep's natural end: END (release the
   lease, then `ScheduleWakeup(stop: true)`), no reschedule.
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
   apart; step 6 below writes a different handoff for each. Pace sizes its
   estimates per item, so a `chunk_size` changed with `set-policy` is paced
   correctly from the next chunk on.

4. **`continue` or `probe`:**
   ```bash
   python3 "$STATE" next <slug> --n <chunk_size>   # probe: --n 1
   ```
   **Exit `3`** (prints `[]`, writes nothing): nothing is pending or
   running. Stop claiming chunks right there — even if this is the first
   chunk of the turn — and go to step 2's final summary. **Exit `4`**
   (prints `[]`, claims nothing): another session holds the loop lease, or
   nothing is pending but items are still running in another session's
   open chunk — the sweep is *not* finished; handle it like `recover`'s
   exit 4: quote it and END with `ScheduleWakeup(stop: true)`.

   Otherwise dispatch every claimed item as its own agent, **all in the same
   message** (§4 of the output style — this is a fan-out, not a queue), per
   the plan's pinned method (agent type, model, done criteria). Each agent's
   reply must be exactly one line — you do not read a full transcript per
   item, that is the entire point of the checkpoint contract. Record each:
   ```bash
   python3 "$STATE" done <slug> <item-id> [--note "<one line>"]
   python3 "$STATE" fail <slug> <item-id> --reason "<one line>"
   ```
   `done`/`fail` only land on an item still running in *this session's* open
   chunk. **Exit 4** from one means that item was reclaimed (returned to
   pending, or the chunk was taken over): its result was not recorded — note
   it and carry on with the rest. **Exit 5** (lock busy): retry the same
   call, up to 3 times a few seconds apart; never drop a result silently.
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
   never printed. If `amend-plan` or `set-policy` dies mid-way, re-running
   the same command completes it. Then close the chunk and ask pace again:
   ```bash
   python3 "$STATE" end-chunk <slug>
   ```
   `end-chunk` prints the ids of any claimed items that never got a
   `done`/`fail` (an agent that never reported): they go back to pending
   for a later chunk, so mention them in your summary — never mark them
   done yourself. If `end-chunk` exits 2 with `no open chunk`, another
   session took this chunk over: say so and END with
   `ScheduleWakeup(stop: true)`.

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

   **Every `ScheduleWakeup` below that reschedules is preceded by the lease
   renewal** for the same delay — `python3 "$STATE" lease <slug> --in
   <delaySeconds>` — so no other session can start a loop on this sweep
   while this one sleeps. If that exits 4, another session took the sweep
   over: tell Griffin and END with `ScheduleWakeup(stop: true)` instead of
   rescheduling. If it exits 5, retry it once; if it is still 5, reschedule
   at the 60 s floor rather than the pace delay — the lease that `next`,
   `done`/`fail` and `end-chunk` renewed runs 10 minutes past this turn's
   last write, which covers a 60 s wakeup but not a long sleep.

5. **`sleep`:** renew the lease (`lease <slug> --in <delay_s>`), then call
   `ScheduleWakeup` with `delaySeconds` = the decision's
   `delay_s` and `reason` = the decision's `reason`. Do not invent your own
   delay — `pace.py` already clamped it to [60, 3600]; a longer real wait
   just means you get woken and immediately call `pace.py` again, which
   returns another `sleep` with a fresh (shorter) delay, chaining until it's
   actually time.

   **Reschedule at the floor** (3-chunk bound hit while pace still says
   `continue` or `probe`, or `sweep_state.py` exited 5): renew the lease
   (`lease <slug> --in 60`; after a lock-busy exit 5 the last write's lease
   still covers 60 s), then call `ScheduleWakeup(delaySeconds: 60, reason:
   "<why> — rescheduling at the minimum delay")`. State this explicitly in
   your summary — it is the transcript-size cap (or a busy lock) talking,
   not a usage-driven sleep, and 60s is `ScheduleWakeup`'s own floor, not a
   pace estimate.

6. **`stop`:** write a short sweep handoff, then END (release the lease with
   `python3 "$STATE" lease <slug> --release`, then `ScheduleWakeup(stop:
   true)`) — do not reschedule. Every handoff carries progress
   (done/failed/pending counts from `status --json`), the resume line
   (`/loop /maestro:sweep resume <slug>`), and a reason + remedy pair
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
     `allow_blind` false) — reason: pacing is blind because no fresh
     statusline reading reached usage.json. Either Maestro's statusLine is
     not installed, or this session runs where no status line renders: a
     headless `-p` run never renders one, and the docs don't say whether a
     backgrounded (agent view) session does — a sweep loop moved to the
     background may go blind even with the statusLine installed. Remedy:
     "run install.sh to set Maestro's statusLine" — that is what produces
     the usage snapshot `pace.py` reads — and run the loop in a foreground
     session; or, only if Griffin asks for it, `set-policy` with
     `allow_blind`. Then resume with the line above.

The conductor never reads an item's full output — one line per item, ever.
Anything more belongs in the sweep dir (`findings.jsonl`, `pace.jsonl`), not
your transcript.

## Changing a sweep's policy — `set-policy`

Only on Griffin's explicit request (a ceiling, the deviation level,
`chunk_size`, `allow_blind`, …) — never on your own initiative, and never to
get past a refusal:

```bash
python3 "$STATE" set-policy <slug> --json '{"deviation":"adaptive"}'
```

It validates, merges onto the current policy, appends a new policy version
(`policy.vN.json`, a `set_policy` amendment) and makes it current; `check`
verifies the whole chain. If `policy.json` was edited by hand, `set-policy`
needs a complete policy in `--json` (it won't merge onto an untrusted file).
Exit 2 means the policy was invalid and nothing changed.

## `takeover <slug>`

Only when Griffin says the session that owns the open chunk or holds the
loop lease is dead (the anchor or a `busy:` line named it — including this
same conversation before a `/clear`). Not a loop turn — run once:

```bash
python3 "$STATE" recover <slug> --takeover
```

It closes the other session's open chunk as interrupted, returns its
unreported items to pending, moves the loop lease to this session, logs a
`takeover` amendment, and makes this session the owner of the next chunk.
If the old loop does wake up later, its `recover`/`next`/`done`/`fail`
exit 4 and it ends itself. Then hand Griffin the
`/loop /maestro:sweep resume <slug>` line to restart the loop in this
session.

## `status [slug]`

No slug: `python3 "$STATE" list`. With a slug: `python3 "$STATE" status <slug>`
plus the latest `pace.py` decision for that sweep (same `<sweep-dir>`
resolution as above), so Griffin sees both where it is and what it would do
next.

## `stop <slug>`

Marks nothing in the sweep itself — `stop` here means *end the loop
cleanly*, not abandon the sweep. Release this session's lease
(`python3 "$STATE" lease <slug> --release`; an exit 4 means another session
holds it — leave it), and if this turn is running under `/loop`, END with
`ScheduleWakeup(stop: true)`. Print the counts and the resume command:
```
/loop /maestro:sweep resume <slug>
```
