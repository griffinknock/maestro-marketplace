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
/loop /maestro:sweep run <slug> --owner <owner>
```

```
/loop /maestro:sweep resume <slug> --owner <owner>
```

Self-paced `/loop` always re-fires the *same original prompt text* on the
next iteration — it does not accept a new prompt per wakeup. So the "resume
prompt" is nothing more than that exact line: say it once when you start the
loop, and every later wakeup, `stop`'s printed resume command, and the
SessionStart anchor line `sweep_state.py`'s `anchor` command prints (`Resume
with /loop /maestro:sweep resume <slug>` — see the OWNER note below on why
that anchor line has no `--owner`) must all resolve to the same slug and the
same subcommand shape. If you are asked to `run`/`resume` outside a `/loop`
turn, do one recover→check→pace round honestly, then tell Griffin the
sleep/stop decision cannot self-schedule here and hand him the
`/loop /maestro:sweep run <slug> --owner <owner>` line to paste.

## OWNER — a stable per-loop identity

`next`, `recover`, and `end-chunk` all take `--owner OWNER`: the id
`recover` uses to tell "this session still holds the open chunk" from "some
other live session does." It must stay the same across every wakeup of
*this* `/loop`, and differ from any other session's — including a second
`/loop` started on the same sweep later.

There is no documented environment variable that hands a Bash-tool command
its own session id. Checked
[the env-vars reference](https://code.claude.com/docs/en/env-vars): the
closest candidates are `CLAUDECODE` ("Set to `1` in subprocesses spawned by
Claude Code (Bash, PowerShell, tmux, hooks, status line, MCP servers)") and
`CLAUDE_CODE_CHILD_SESSION` ("Check if process was spawned directly by tool
call or hook (vs. within stdio MCP server)") — both flag *that* a subprocess
runs under Claude Code, neither identifies *which* session. So mint an
owner token yourself, once, before the first `/loop` line is ever said out
loud:

```bash
OWNER="sweep-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
```

Because self-paced `/loop` re-fires the exact prompt text you gave it,
baking `--owner "$OWNER"` into that text is what keeps it stable across
wakeups — you never regenerate it mid-loop. Say the resulting line — with
the real token substituted in place of `<owner>` — everywhere a resume
command is shown below: the `run` confirmation, every
`next`/`recover`/`end-chunk` call this turn, and `stop`'s printed resume
line. Starting a fresh `/loop /maestro:sweep run <slug>` with no prior owner
mints a new token the same way, so two sessions on the same sweep never
collide.

`sweep_state.py`'s SessionStart anchor does not carry `--owner` yet — it
still prints exactly `Resume with /loop /maestro:sweep resume <slug>`, no
token. Until that changes, treat the anchor as a slug reminder only and pull
the real `--owner` value from your own last resume line, not from the
anchor.

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
   what `pace.py` does when it can never get a usage reading — see the "no
   usage signal" case in the `run`/`resume` section below. Omit them to take
   the defaults. `new` exits `2` and writes nothing if the policy is
   invalid — fix the JSON and re-run.

4. **Show a summary** — slug, item count, method, ceilings, deviation — mint
   `OWNER` as described above, and ask before starting:

   ```
   /loop /maestro:sweep run <slug> --owner <owner>
   ```

## `run` / `resume` — the work loop

Runs only under `/loop` (see above). Every `next` / `recover` / `end-chunk`
call below carries the same `--owner "$OWNER"` this loop started with — see
the OWNER section. Each turn:

1. `python3 "$STATE" recover <slug> --owner "$OWNER" [--stale-after SECONDS]`.
   - **Exit 4** (`busy: chunk <n> owned by <owner> since <iso>`): another
     live session holds the open chunk. Do not proceed and do not touch
     anything else — no `check`, no `next`, no `ScheduleWakeup`. Tell
     Griffin a sweep session is already running, quoting the exact busy
     line verbatim, and end there.
   - Otherwise, `python3 "$STATE" check <slug>`. `check` prints `SWEEP FAIL`
     and reasons → stop, report the reasons, do not proceed.
2. Work chunks (step 4 below) until either `next` exits `3` (nothing
   pending) or you've worked 3 chunks this turn. Either way, go straight to
   the final summary: `python3 "$STATE" status <slug> --json` for the
   done/failed/pending counts — never hand-count from your own transcript —
   plus findings and items added. If you stopped because `next` exited 3,
   that is the sweep's natural end: end the loop with
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
   `seven_day`), or "no usage signal" after 3 chunks with no reading and
   `allow_blind` false (with `allow_blind` true, it sleeps `blind_gap_s`
   instead of stopping) — read `reason` to tell them apart; step 6 below
   writes a different handoff for each.

4. **`continue` or `probe`:**
   ```bash
   python3 "$STATE" next <slug> --n <chunk_size> --owner "$OWNER"   # probe: --n 1
   ```
   **Exit `3`** (prints `[]`, writes nothing): nothing is pending. Stop
   claiming chunks right there — even if this is the first chunk of the
   turn — and go to step 2's final summary.

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
   on. Then close the chunk and ask pace again:
   ```bash
   python3 "$STATE" end-chunk <slug> --owner "$OWNER"
   ```
   **Run at most 3 chunks in one turn.** Every chunk you work adds its
   items' one-line results to your own transcript, which is re-read on every
   later turn in this session — 3 chunks keeps that bounded while still
   making real progress between wakeups; a sweep with hundreds of items
   should take many turns, not one giant one. Stop claiming new chunks the
   moment any of these hits: the 3-chunk bound, `next` exiting 3, or pace
   saying anything other than `continue`. If it was the 3-chunk bound **and
   pace's last answer this turn was `continue` or `probe`** — there's more
   to do and no usage reason to wait — use step 5's reschedule-at-the-floor
   case, not the normal sleep math. Otherwise carry pace's actual last
   decision into step 5/6.

5. **`sleep`:** call `ScheduleWakeup` with `delaySeconds` = the decision's
   `delay_s` and `reason` = the decision's `reason`. Do not invent your own
   delay — `pace.py` already clamped it to [60, 3600]; a longer real wait
   just means you get woken and immediately call `pace.py` again, which
   returns another `sleep` with a fresh (shorter) delay, chaining until it's
   actually time.

   **Reschedule at the floor** (3-chunk bound hit while pace still says
   `continue` or `probe`): call `ScheduleWakeup(delaySeconds: 60, reason:
   "3-chunk in-turn cap reached; pace says <action> — more work pending,
   rescheduling at the minimum delay")`. State this explicitly in your
   summary — it is the transcript-size cap from step 4 talking, not a
   usage-driven sleep, and 60s is `ScheduleWakeup`'s own floor, not a pace
   estimate.

6. **`stop`:** write a short sweep handoff, then end the loop with
   `ScheduleWakeup(stop: true)` — do not reschedule. Every handoff carries
   progress (done/failed/pending counts from `status --json`), the resume
   line (`/loop /maestro:sweep resume <slug> --owner <owner>`), and a
   reason + remedy pair matched to *why* pace said stop:
   - **weekly (`seven_day`) ceiling** — reason: the ceiling and when that
     window resets (from the decision). Remedy: nothing to do but wait;
     resume after the reset with the line above.
   - **invalid policy** — reason: quote pace's error verbatim. Remedy: fix
     `policy.json` (or re-run `new` with corrected `--policy` JSON), then
     resume.
   - **internal pace error** — reason: quote the error verbatim. Remedy:
     this is a bug in `pace.py` or its inputs, not a usage limit — do not
     retry blindly; flag the exact error text to Griffin.
   - **no usage signal** (blind for 3+ chunks, `allow_blind` false) —
     reason: say pacing is blind because no statusline usage reading ever
     arrived. Remedy: "run install.sh to set Maestro's statusLine" — that
     is what produces the usage snapshot `pace.py` reads. Once it's set,
     resume with the line above and pace will get real readings.

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
/loop /maestro:sweep resume <slug> --owner <owner>
```
