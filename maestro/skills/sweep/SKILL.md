---
name: sweep
description: Usage-aware sweep for long, slow runs — freeze a plan and an item index once, work it in checkpointed chunks paced against real rate-limit usage, and resume exactly after any interruption.
argument-hint: <new <goal> | run <slug> | resume <slug> | status [slug] | stop <slug>>
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
next iteration — it does not accept a new prompt per wakeup. So the "resume
prompt" is nothing more than that exact `/loop /maestro:sweep run <slug>` (or
`resume`) line: say it once when you start the loop, and every later
wakeup, `stop`'s printed resume command, and the SessionStart anchor line
sweep_state.py's `anchor` command prints (`Resume with /maestro:sweep resume
<slug>`) must all resolve to the same slug and the same subcommand shape. If
you are asked to `run`/`resume` outside a `/loop` turn, do one recover→check→
pace round honestly, then tell Griffin the sleep/stop decision cannot
self-schedule here and hand him the `/loop /maestro:sweep run <slug>` line to
paste.

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
     --policy '{"ceilings":{"five_hour":80,"seven_day":90},"deviation":"additive","chunk_size":5}'
   ```

4. **Show a summary** — slug, item count, method, ceilings, deviation — and
   ask before starting:

   ```
   /loop /maestro:sweep run <slug>
   ```

## `run` / `resume` — the work loop

Runs only under `/loop` (see above). Each turn:

1. `python3 "$STATE" recover <slug>` then `python3 "$STATE" check <slug>`.
   `check` prints `SWEEP FAIL` and reasons → stop, report the reasons, do not
   proceed.
2. If `status`/`counts` show no `pending` items left: print the final
   summary (done/failed counts, findings, items added) and end the loop —
   your next tool call is `ScheduleWakeup` with `stop: true`.
3. Otherwise ask pace. The sweep directory is `$MAESTRO_SWEEPS_DIR/<slug>` if
   that env var is set, else `<git toplevel of cwd>/.claude/maestro/sweeps/<slug>`
   — the same resolution `sweep_state.py` uses internally. Pass it as
   `--sweep`:

   ```bash
   python3 "$PACE" --sweep <sweep-dir>
   ```

   Read the one JSON decision line: `action` is `continue` / `sleep` /
   `stop` / `probe`.

4. **`continue` or `probe`:**
   ```bash
   python3 "$STATE" next <slug> --n <chunk_size>       # probe: --n 1
   ```
   Dispatch every claimed item as its own agent, **all in the same message**
   (§4 of the output style — this is a fan-out, not a queue), per the plan's
   pinned method (agent type, model, done criteria). Each agent's reply must
   be exactly one line — you do not read a full transcript per item, that is
   the entire point of the checkpoint contract. Record each:
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
   on. Then close the chunk and ask pace again:
   ```bash
   python3 "$STATE" end-chunk <slug>
   ```
   **Run at most 3 chunks in one turn.** Every chunk you work adds its
   items' one-line results to your own transcript, which is re-read on every
   later turn in this session — 3 chunks keeps that bounded while still
   making real progress between wakeups; a sweep with hundreds of items
   should take many turns, not one giant one. After the 3rd chunk (or sooner,
   the moment pace says anything other than `continue`), stop claiming new
   chunks and go to the reschedule step below with whatever pace last said.

5. **`sleep`:** call `ScheduleWakeup` with `delaySeconds` = the decision's
   `delay_s` and `reason` = the decision's `reason`. Do not invent your own
   delay — `pace.py` already clamped it to [60, 3600]; a longer real wait
   just means you get woken and immediately call `pace.py` again, which
   returns another `sleep` with a fresh (shorter) delay, chaining until it's
   actually time.

6. **`stop`:** write a short sweep handoff — progress (done/failed/pending
   counts), why it stopped (`pace.py`'s reason — almost always the
   `seven_day` ceiling), when that window resets, and the exact resume
   command:
   ```
   /loop /maestro:sweep resume <slug>
   ```
   Then end the loop with `ScheduleWakeup(stop: true)` and do not reschedule.

The conductor never reads an item's full output — one line per item, ever.
Anything more belongs in the sweep dir (`findings.jsonl`, `pace.jsonl`), not
your transcript.

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
