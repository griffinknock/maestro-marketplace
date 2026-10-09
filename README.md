# Maestro

Maestro is a Claude Code plugin that runs a session as a conductor. One
orchestrator hands work to model-tiered subagents, a set of hooks watches what
they do, and the tree they build shows up in the terminal and on a local board.

I built it because multi-agent Claude Code sessions kept failing in the same
few ways, and the lead agent usually couldn't tell:

- **Reports go missing.** A subagent finishes, but what reaches the conductor
  is an idle frame or a bare "report stands." It reads that as a clean finish.
- **Failures repeat.** The same tier fails twice, or two agents work on one
  file, and nothing flags it.
- **Claims go unchecked.** A finding gets trusted because an agent stated it
  confidently.
- **Context piles up.** Every turn re-reads the whole transcript, so a long
  session keeps paying for everything it ever read, and `/compact` decides on
  its own what to keep.

What Maestro does about each:

| Failure | Response |
|---|---|
| Missing reports | Keeps the best report each agent produced, recovers it from the agent's own transcript when needed, and hands the conductor a digest plus a pointer. If nothing is recoverable it says `REPORT NOT DELIVERED` |
| Repeated failures | A re-check speaks only with news: a stalled agent, the same agent type failing twice, two live agents on one file, and depth 5. Each finding lands once, on the conductor's next turn, so it can re-plan |
| Unchecked claims | An `adversary` agent whose job is to refute a claim before anyone trusts it, and an opus `surgeon` for work a builder has already failed twice |
| Context pile-up | Per-agent token accounting, and `/handoff`, which writes a validated `HANDOFF.md` at a phase boundary so the next session starts clean |

Everything below is the technical reference.

---

## Install

### Claude Code

```bash
claude plugin marketplace add griffinknock/maestro-marketplace
claude plugin install maestro@maestro-marketplace
```

Or from inside a session: `/plugin marketplace add griffinknock/maestro-marketplace`,
then `/plugin install maestro@maestro-marketplace`, then `/reload-plugins`.

That gives you the agents, hooks, commands, the sweep skill, the agent tree and
the board. Two settings turn the rest on. Put them in `~/.claude/settings.json`:

- `"outputStyle": "Maestro"` makes the session a conductor. Without it the
  agents and hooks still work, but nothing plans waves or tiers dispatches.
- `"statusLine"` set to `python3 "<plugin dir>/scripts/statusline.py"` with
  `"refreshInterval": 3` gives you the two-row status line, and sweeps need it
  for pacing (it records rate-limit usage). A marketplace install lives in
  `~/.claude/plugins/cache/maestro-marketplace/maestro/<version>/`, so re-point
  this after an update — or install from a clone, below, and it never moves.

**Full setup from a clone.** `install.sh` does all of the above in one step. It
validates the scripts, registers the marketplace, installs the plugin, and
writes `statusLine`, `outputStyle`, agent-teams and a few read-only `git`
permissions into `~/.claude/settings.json`. It backs up the old file first.

```bash
git clone https://github.com/griffinknock/maestro-marketplace.git
cd maestro-marketplace
./install.sh
```

Requires `python3` and `git`. Terminal spawning, notifications and the sleep
assertion are macOS-only; everything else is portable. No pip packages.
Optional: `brew install terminal-notifier` makes notifications clickable
(they open the board). Without it `osascript` is used, which cannot carry a
click action.

**Updating.** Third-party marketplaces do not auto-update unless you turn it on
in `/plugin` → Marketplaces. To take a new release:

```bash
claude plugin marketplace update maestro-marketplace
claude plugin update maestro@maestro-marketplace
```

then `/reload-plugins` or restart. `marketplace update` only refreshes the
listing; `plugin update` is what installs the new version.

**Developing Maestro.** `claude plugin install` is a no-op when the plugin is
already installed, so it will not pick up source edits. Uninstall and install
again. Hooks, agents and `subagentStatusLine` run from the installed copy under
`~/.claude/plugins/cache/`; only `statusLine` points at the source tree.

### Codex

There are two ways to combine them, and only the first gives you Maestro.

- **Codex as a Maestro agent (recommended).** The `codex` agent hands a
  well-specified change or a review to the Codex CLI in its own worktree, then
  verifies and commits the result. Install Maestro in Claude Code as above,
  install and log in to `codex`, and dispatch `codex` like any other agent.
  See *Codex lane*.
- **Maestro inside Codex.** Codex reads this marketplace as-is:

  ```bash
  codex plugin marketplace add griffinknock/maestro-marketplace
  codex plugin add maestro@maestro-marketplace
  ```

  But a Codex plugin carries skills, MCP servers and apps, not hooks, agents or
  output styles. Codex sees only the `sweep` skill, and that skill depends on
  Claude Code's `/loop` and status line. The conductor, the re-check, the
  ledger, the tree and the board are Claude Code features.

### Ollama, gateways and cloud providers

Maestro's hooks are local `python3` scripts and do not care which model
answers. The agents ask for tiers by alias (`haiku`, `sonnet`, `opus`), so on
any backend other than the Anthropic API, **map every alias to a real model**:

```bash
export ANTHROPIC_DEFAULT_HAIKU_MODEL=<model for scout, scribe, codex>
export ANTHROPIC_DEFAULT_SONNET_MODEL=<model for builder, adversary, section-lead, visual-reviewer>
export ANTHROPIC_DEFAULT_OPUS_MODEL=<model for surgeon>
```

To put every subagent on one model instead, set
`CLAUDE_CODE_SUBAGENT_MODEL=<model>` and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`.

- **Ollama (local or Ollama Cloud).** `ollama launch claude` starts Claude Code
  against Ollama, or set it up by hand
  ([Ollama's guide](https://docs.ollama.com/integrations/claude-code)):

  ```bash
  export ANTHROPIC_BASE_URL=http://localhost:11434
  export ANTHROPIC_AUTH_TOKEN=ollama
  export ANTHROPIC_API_KEY=""
  claude --model <model>
  ```

  Pick models that support tool calling and give local ones a 64K+ context
  window. Ollama's API has no prompt caching, so a long conductor session
  re-pays its whole context every turn — hand off at phase boundaries early.
  If requests fail with 400s, try `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`
  or `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`.
- **Other gateways (LiteLLM, OpenRouter, a company proxy).** Anything that
  serves the Anthropic Messages API: `ANTHROPIC_BASE_URL=<gateway>` plus
  `ANTHROPIC_AUTH_TOKEN` (sent as a Bearer token) or `ANTHROPIC_API_KEY`
  ([gateway docs](https://code.claude.com/docs/en/llm-gateway)).
- **Amazon Bedrock, Google Vertex, Microsoft Foundry.** `CLAUDE_CODE_USE_BEDROCK=1`,
  `CLAUDE_CODE_USE_VERTEX=1` (plus `CLOUD_ML_REGION`) or
  `CLAUDE_CODE_USE_FOUNDRY=1`, with that provider's credentials. Pin the three
  aliases here too: unpinned, they resolve to built-in defaults that can lag
  the newest models (on Foundry `haiku` is still Haiku 4.5).

Anthropic does not support routing Claude Code to non-Claude models, and
Maestro is developed and tested against the Anthropic API. Expect a smaller
model to follow the conductor doctrine and the agents' report contract less
reliably than a Claude model does.

---

## What it adds

| Piece | Where |
|---|---|
| Maestro output style | System prompt — wave planning, brainstorm gate, mermaid graphs |
| 8 tiered agents | `scout` `scribe` `codex` (haiku) · `builder` `section-lead` `visual-reviewer` `adversary` (sonnet) · `surgeon` (opus) |
| Indented agent tree | Agent panel, via `subagentStatusLine` — nesting, tier colour, context meter, elapsed |
| Two-row status line | Model · project · branch · cost, then context · agent census · `depth N/5` · board link. Also persists each `rate_limits` reading to `usage.json` for sweep pacing |
| Board | `http://127.0.0.1:7717` — fan-out DAG, lanes, worktrees, screenshots |
| Ledger | `.claude/maestro/<session>/{events.jsonl,state.json}`, one per session, pinned where the conductor starts (its project root, or its own worktree if it runs in one); every subagent's hooks, worktree builders included, land there via `~/.claude/maestro/sessions/<session>` |
| Token accounting | Per-agent and conductor spend from the transcripts, on the board and lanes |
| Phase-boundary handoffs | `/handoff` writes a validated `HANDOFF.md`, then the session is cleared |
| Re-check | Speaks only with news — an undelivered report, a silent agent, a repeated failure, a file collision, depth 5. Re-delivers swallowed reports as digest + pointer |
| Mailbox prune | Drops stale teammate idle pings before the conductor is ever woken by them |
| Sleep assertion | Holds `caffeinate` while agents are in flight |
| Cross-session lessons | Human-approved rules captured in one session, injected at the start of every later one — see *Lessons* |
| Usage-aware sweep | Checkpointed long runs paced against real rate-limit usage instead of a fixed interval — see *Sweeps* |

`scout` and `scribe` are read-only. `builder`, `section-lead`, `surgeon` and
`codex` declare `isolation: worktree` and run in their own checkout.

`scout` and `scribe` run on Haiku 5.5 through the `haiku` alias: a 1M context
window, at roughly a twentieth of Sonnet 5.5's price.

### Codex lane

`codex` hands a well-specified change, or a review, to OpenAI's Codex CLI in
its own worktree, then verifies and commits what Codex did. Use it for a
cross-vendor second implementation or an independent review, or to spend
Codex quota instead of Claude's. It is a haiku wrapper: it writes the brief,
runs Codex, checks the result and reports. It does not write the change
itself.

It needs `codex` installed and logged in. Without `codex` on `PATH` it
returns `blocked: codex CLI not installed`.

The invocation it runs, with the brief read from stdin:

```bash
codex exec -C <worktree> -s workspace-write -o <file> -
```

Review mode uses `-s read-only` instead and makes no commit. Codex's sandbox
has no network by default.

Codex tokens bill to the Codex account, not the ledger, so they do not appear
in the token accounting.

---

## Commands

| Command | Does |
|---|---|
| `/orchestrate <task>` | Plans waves — agent, model, worktree, dependencies, cost — then stops and asks |
| `/brainstorm <topic>` | No code, no edits. Prior-art recon (web + repo), then one numbered question per message, then alternatives with trade-offs, ending in an `/orchestrate` score |
| `/handoff [note]` | Closes a conductor session at a phase boundary: updates `PLAN.md`, writes `HANDOFF.md`, validates it with `handoff_check.py`, hands over the `/clear` |
| `/board` | Starts the board, returns the link. `--lan` also binds to the network |
| `/tree` | Prints the tree, waves, worktrees and a mermaid diagram as text |
| `/look <url>` | Screenshot pass across viewports, compared against a mock or baseline |
| `/maestro:lessons [review\|status\|publish <id>\|trust\|repair]` | Reviews pending lesson candidates, checks the lesson budget, publishes a repo-scoped lesson, or trusts/repairs one — see *Lessons* |
| `/maestro:sweep <new\|run\|resume\|status\|stop\|takeover> <slug>` | Runs or manages a usage-paced sweep — see *Sweeps* |

---

## The board

Watches every repo under the search roots, not just the one it started in.
The left rail lists one item per orchestration; click to switch, double-click
to rename.

`+ New orchestration` spawns a real terminal tab in the right directory with
`claude` running.

- **Warp** (default): writes `~/.warp/tab_configs/maestro-<slug>.toml` and opens
  `warp://tab_config/<slug>`. Passes the prompt as an argument, so it **submits**.
- **Ghostty** (fallback, used if Warp is absent): AppleScript. Uses `initial
  input`, so the prompt is **typed but not sent**. Needs macOS Automation
  permission, granted on first use.

`POST /api/spawn` and `/api/meta` refuse any non-loopback client, so `--lan`
gives other devices a read-only view. Spawn also rejects a repo outside the
watched roots.

### API

| Route | Returns |
|---|---|
| `GET /api/state[?id=]` | Reconstructed tree for a session |
| `GET /api/orchestrations` | Every session across every watched repo |
| `GET /api/stream[?id=]` | SSE, pushes on `state.json` mtime change |
| `GET /api/events` | Last 400 ledger events |
| `GET /api/question[?id=]` | Pending blocking question, or `null` |
| `GET /api/dismiss[?id=]` | Clears the question and `needs_input` |
| `GET /api/music[?a=]` | Spotify/Music transport; `a=next\|prev\|playpause` |
| `GET /api/shot?p=` | A screenshot, path-checked against the watched roots |
| `GET /api/net` | Bind address plus reachable tailnet/LAN URLs |
| `POST /api/spawn` | `{repo,name,prompt,color,newWindow}` — loopback only |
| `POST /api/meta` | `{id,name,color}` — loopback only |

---

## How the tree is built

Claude Code's `PreToolUse(Agent)` payload carries **no `agent_id`**, and
`transcript_path` and `prompt_id` are identical for every agent in a session.
The dispatching agent is therefore unidentifiable, and nothing in the payload
can link a child to its parent.

Parentage is recovered from worktrees instead. A delegating agent runs in
`.claude/worktrees/agent-<its id>` and the children it spawns inherit that
working directory, so the worktree a node sits in names its parent. The
parent's own node — whose id appears nowhere else — is reconstructed from the
matching entry in `pending`, restricted to worktree-isolated types and bounded
by `MAESTRO_PENDING_TTL`. Such nodes carry `inferred: true`.

Consequences:

- A delegating agent with no worktree would have its children fall back to the
  conductor.
- With several same-type leads launched at once, descriptions can swap between
  them. Depth, lane and shape stay correct.
- Nested agents arrive *named* rather than typed (`agent_type: scout-apiclient`).
  The type is recovered from the leading segment when it names a real agent;
  the original is kept as `name`. An unrecognised name tiers as sonnet.
- A node whose stop event never arrives is reaped after `MAESTRO_REAP_SECONDS`
  and marked `orphaned`.

### Depth

Claude Code caps nesting at 5. The conductor prefixes every `section-lead`
prompt with `[maestro depth=1/5 · fanout<=4 · parent=conductor]`; each lead
decrements it and refuses to delegate at 5. The status line and board show the
deepest live level.

---

## Re-orchestration check

`reorchestrate.py` (the re-check) watches the shape of the orchestra and pushes
a finding into the conductor's context, but only when the finding is news: a
thing the conductor does not already know, that would change its next
decision. Every message it sends costs a full main-loop turn on a large
context, so the budget is **silence, unless there is a finding**.

### It only reports what would change the next decision

- **An undelivered report.** A subagent's report the harness swallowed —
  recovered from its transcript, or lost. See *It re-delivers reports the
  harness swallowed* below.
- **A stalled agent.** An agent with no sign of life for `MAESTRO_STALL_SECONDS`
  (default `600`, ten minutes): no tool call and no write to its own
  transcript. A write to the transcript counts as life, so a long-running
  step that is still writing is not a stall; inside one tool call (a long
  Bash, a Codex run) the limit doubles. It fires once per agent. Idle
  teammates are skipped.
- **The same agent type failing twice.**
- **Two live agents writing the same file.**
- **Depth 5.**

Every finding is fingerprinted and delivered at most once. It never speaks on
a dispatch call, since a batch still landing is not a moment to judge, and it
never speaks inside a subagent.

### It re-delivers reports the harness swallowed

`SubagentStop` is not once per agent: it re-fires on every stop of the
subagent's loop, 18 times for one agent in the ledgers this was built against.
So the message the conductor is finally handed can be an idle frame or a bare
"report stands", long after the real report went past — and `last_assistant_message`
is absent outright on roughly one stop in twelve.

`ledger.py` therefore keeps the *best* report it has seen rather than the
newest message, recovers one from `agent_transcript_path` when the payload has
none, writes it to `<session>/reports/<agent-id>.md`, and records separately
whether the copy the **conductor** received was that report. When it was not,
the re-check hands over a **digest plus a pointer** on the conductor's next
turn — the first ~15 lines, then the path to the durable copy:

```
REPORT RECOVERED — scout-web-referral finished, but the copy delivered to you
was empty or a bare protocol frame. Its real report, from its own transcript,
is below. Do not ask it to resend.
…
… digest — 27 more line(s) on disk. Read the full report only if the digest
is not enough: .claude/maestro/<sid>/reports/<agent-id>.md
```

Re-delivering in full would re-buy the report's tokens on the conductor's
largest context every remaining turn; the digest carries the verdict and the
pointer carries the rest. When nothing is recoverable it says
`REPORT NOT DELIVERED` instead of letting loss look like a clean finish. This
is the one class that always earns a turn.

### It reads its own session, never the workspace pointer

`.claude/maestro/current` is workspace-global and the last session to fire a
hook owns it. Resolving state through it made a conductor inherit a concurrent
session's agents — reporting lanes it never opened and file collisions on files
it never dispatched against. The re-check resolves `.claude/maestro/<session-id>`
from its own payload instead. The board still uses `current` to find the newest
session; a re-check must not.

### It must never run inside a subagent

Context injected on `SubagentStop`, or on any event that fired inside a
subagent, is delivered to that **subagent**, which spends its final message
answering the re-check instead of returning its report. The conductor gets
"nothing further to do here" and the findings are stranded. Both guards are in
`main()`, independent of `hooks.json`. As a second layer, every agent's
definition ends with a resend rule: any message arriving after it has reported
gets the same `RETURN:` block again, verbatim.

### Stale idle pings never reach the conductor

When a named teammate goes idle it sends the lead a four-field JSON frame:

```json
{"type":"idle_notification","from":"scout-surfaces","timestamp":"…","idleReason":"available"}
```

which arrives wrapped in the ~120-word cross-session security preamble that
exists for peer prose. Zero information, maximum ceremony, and a whole
main-loop turn on a very large context to read it — usually *after* that agent's
report had already landed, and sometimes after the conductor had already
stopped it.

That frame is not generated by the harness's notification path — it is written
by a `Stop` hook running inside the *teammate's own session*
(`id: teammate-idle-notification`) directly into the lead's mailbox at
`~/.claude/teams/session-<session-id>/inboxes/<lead>.json`. It is a plain JSON
array on disk, guarded by a `proper-lockfile` directory lock, drained when the
lead reads it. So it can be intercepted before it is ever read, and `ledger.py`
does exactly that.

The ledger already knows which agents have delivered and which have stopped, so
it can answer the only question worth a turn: **would this ping tell the
conductor something it does not already know?**

- Agent already delivered its report, or already done/failed/orphaned → the
  frame is removed and no turn is spent.
- Agent went idle **without** reporting → the frame is left alone. That is a
  real state change, and it is the one case the conductor must act on.

The frame is written by one entry in the teammate's `Stop` chain while maestro
runs from another, in no guaranteed order — so on `TeammateIdle` (which names
the teammate going idle) the prune waits up to `MAESTRO_IDLE_CATCH` for a frame
it knows must be coming, rather than losing the race and letting it through.

Everything else in that mailbox is load-bearing protocol and is never touched:
prose from a peer, permission requests, plan approvals, shutdown handshakes,
task assignments, entries already marked read, and pings from agents this
session did not dispatch. Removal is restricted to unread frames whose type is
on a one-item allow-list. If the file is not a list of objects, or anything in
it is unexpected, the pass is abandoned and nothing is written. Set
`MAESTRO_INBOX_PRUNE=0` to disable it entirely.

Dispatch discipline still helps and costs nothing: the output style tells the
conductor to send one-shot work out **unnamed**, since a `name` is what keeps
an agent addressable and pinging after it has reported.

---

## Token accounting

Agent-loop cost is quadratic in turn count — every turn re-reads the whole
transcript — and each subagent spawn pays a fixed 20–30K-token cold prefill
before it reads its brief. Neither number is visible from inside a session, so
the ledger measures both.

On every `SubagentStop` the ledger sums the agent's own transcript; on every
conductor `Stop` it sums the main transcript (skipping sidechain records, so
subagents are never double-counted). Records are deduplicated by `requestId` —
one API response can land as several transcript lines sharing one usage
object. Each node gets:

- `tokens` — the headline spend: uncached input + cache writes + output.
  Cache reads are an order of magnitude cheaper and deliberately excluded.
- `usage` — the full breakdown: `in`, `cw` (cache write), `cr` (cache read),
  `out`, `reqs`.

The board shows a `⛁` pill per agent (hover for the breakdown), a per-lane
sum, and the session total in the header. Re-runs are cheap: totals refresh
only when the transcript's size changes.

What the numbers are for: if scouts carry a big share of the session, batch
their questions; if the conductor's own total dominates, the session has run
past a phase boundary — see the next section.

The doctrine the output style pairs with this: batch scout questions into one
spawn, default to a single multi-lens `adversary` (a 2–3 panel only for
load-bearing claims), keep small greps inline instead of paying a spawn, and
emit the mermaid plan once rather than per fan-out.

---

## Phase boundaries and handoffs

The cheapest large context is the one you stop carrying. At a phase boundary —
spec approved, plan approved, a wave-set merged with checks green, review
done, PR opened — the conductor runs `/handoff` and the session ends.

`/handoff` refuses to run mid-wave (the ledger, re-check state, and report
store are keyed to the session id; clearing with agents in flight orphans
them). Otherwise it updates `PLAN.md`, writes `.claude/maestro/HANDOFF.md` —
goal, decisions with one-line rationale, world state, open questions, next
actions — and validates it:

```bash
python3 maestro/scripts/handoff_check.py
```

The validator is what makes the handoff trustworthy blind: required sections,
a 120-line cap, no fenced block over 20 lines (pointers, not payloads), every
backticked path resolved on disk, every `branch:` resolved in git, every
`worktree:` a real directory. The conductor iterates until `HANDOFF PASS`,
then prints the `/clear` and the one-line seed for the next session. The next
conductor reads `HANDOFF.md` before its opening Score and starts from `Next`.

A handoff beats `/compact`: compaction is one uncontrolled summarization at
the session's largest context size, and what it keeps is not up to you.

---

## Blocking questions

The conductor writes `question.json` before it asks. That fires a macOS
notification, an OSC-9 terminal flag, an ntfy push, and a full-width card on the
board with the options as buttons. Clicking one copies its number.

Asking does not stop the orchestra — unblocked lanes keep running.

---

## Lessons

A lesson is a small, human-approved rule about how Maestro should
**orchestrate** — subagent tiering, dispatch/batching, briefs, how reports and
messages pass back, questions, handoffs — never a project fact. Nothing
becomes an active rule without the user's explicit yes to the exact bytes, and
a saved lesson is never edited or removed: a stale rule is only ever
superseded by a newer entry that names it.

**Capture.** Candidates arrive two ways. Automatically, when the re-check's
findings point at the conductor's own behaviour: a file collision, a repeated
failure, or depth 5. Only those three kinds are queued. Manually, when the user
corrects the conductor mid-session: `lessons.py flag --session <id> "<one line>"`.
Capture never decides a candidate is a lesson; it only queues it. It skips a
kind whose candidate was rejected, and keeps one pending candidate per kind
across sessions.

**Review.** `/maestro:lessons` (default `review`) groups pending candidates,
drafts a rule per group, and previews the exact block `accept` would write —
heading, Rule, Why, Evidence, optional Supersedes, Accepted — before ever
asking. `/maestro:handoff` runs the same review for anything still pending
before it writes the handoff, since a candidate carries this session's id and
the session is about to end.

**Tweak or lesson?** Some candidates are about Maestro itself — a hook or
rule that cost turns — and no rule the conductor should follow would fix them.
`lessons.py tweak --note "..." [--candidates ids] [--session sid]` records
that as a change to Maestro. It appends an unchecked item to
`~/.claude/maestro/tweaks.md` (override with `MAESTRO_TWEAKS_FILE`) and marks
the named candidates `tweak`, so they leave the queue. A tweak writes no rule
and needs no approval. `/maestro:lessons` and `/handoff` both offer
**Tweak maestro** for these.

**Entry format** (markdown):

```
## L-007 · scope: global
Rule: <the rule; may wrap to one continuation line indented two spaces>
Why: <one line>
Evidence: <session-id prefix> · <finding fingerprint> · "<short quoted line>"
Supersedes: L-003, L-004
Accepted: 2026-10-02
```

Ids are `L-NNN` in the personal store, `R-NNN` in a repo file. Scope is
`global` or `repo:<name>`.

**Two tiers.**

- **Personal store** — `~/.claude/maestro/lessons/lessons.md` (or
  `$MAESTRO_LESSONS_DIR`). Git-tracked, append-only. `accept` is the only way
  in.
- **Per-repo file** — `<repo>/.claude/maestro-lessons.md`. `publish <id>`
  copies an accepted, repo-scoped personal lesson into it (uncommitted —
  the user commits it with their work), so a team can share a rule. A repo
  file's entry a teammate wrote is **untrusted** until the user runs `trust
  <R-NNN> --sha <sha256>`, binding the approval to the exact bytes shown.

**The approvals ledger** (`lessons-approved.jsonl`, next to the store
directory, or `$MAESTRO_LESSONS_APPROVALS`) is the external anchor for
"the user said yes to exactly these bytes": one JSON line per approval, each
carrying the sha256 of the entry it covers and a hash chain over every
record, so editing, reordering, or cutting a line out of the middle is
detectable. It is **tamper-evident** against accidental or naive edits — a
hand edit, a script rewriting a file, a git history rewrite — and it is
**not** a defense against deliberate forgery by code that already has write
access to `~/.claude`: there is no secret, so such code could append a
forged entry and a matching chain. The real guard against that is never
running `accept`/`publish`/`trust` except immediately after the user's
explicit yes to the exact bytes shown.

**Injection.** The `SessionStart` hook (`lessons.py inject`) prints an active,
approved lesson block, capped at a hard budget (24 entries / 6000 characters
per scope — global, and each repo scope present). Injection is **fail
closed**: if the personal store or its ledger fails validation, nothing is
injected and the session sees `MAESTRO LESSONS OFF` with the reason to fix,
never a silent partial set. A repo file failing on its own (malformed,
tampered, symlinked, too large, too long a history to verify, or over budget)
only turns off that tier — `REPO LESSONS OFF` — personal lessons still
inject.

**Consolidation.** Near or over budget, `/maestro:lessons review` proposes
merging several older entries into one, via `--supersedes L-0AA,L-0BB,L-0CC`
on a fresh `accept` — the active set shrinks; superseded entries stay on disk,
just out of the active set.

**Repair.** `repair` shows (dry run) and, on `--apply --sha <sha256>`,
removes an uncommitted, unapproved tail left by an interrupted `accept` — the
one failure mode that can leave stray bytes behind. It never touches a
committed entry, an approved entry, or anything in the ledger.

`node`-free: the whole pipeline is `maestro/scripts/lessons.py` (capture,
inject, accept, publish, trust, repair, tweak, status) plus
`maestro/scripts/lessons_check.py` (parse/validate/trust logic, reused by
`lessons.py`).

---

## Sweeps

A sweep is a frozen plan plus an item index, worked in small checkpointed
chunks between pauses that a pacing brain schedules from **real rate-limit
usage** — not a fixed interval:

```
/maestro:sweep new <goal>
/maestro:sweep run <slug>
/maestro:sweep resume <slug>
/maestro:sweep status [slug]
/maestro:sweep stop <slug>
/maestro:sweep takeover <slug>
```

`run` and `resume` only run inside `/loop` — self-paced (`/loop
/maestro:sweep run <slug>`), so the same prompt text re-fires on every wakeup
and is the whole resume line.

**State.** `sweep_state.py` is the checkpoint store, under
`<repo>/.claude/maestro/sweeps/<slug>/` (or `$MAESTRO_SWEEPS_DIR`):
`plan.md` (frozen at `new`; amendments are `plan.v2.md`, …), `index.json`
(items and their status), `policy.json` (the current policy; every version is
kept as `policy.vN.json`), `findings.jsonl`, `amendments.jsonl`, and
`pace.jsonl` (two lines per chunk: start, then end).

**Deviation levels** govern what a finding may do to the plan, default
`additive`:

| Level | A finding may |
|---|---|
| `locked` | log only — `add` refuses |
| `additive` (default) | add a new item, never edit the plan or drop items |
| `adaptive` | amend the plan after the user approves a non-blocking question |
| `autonomous` | amend the plan freely, logged |

**Pacing.** `pace.py` reads `usage.json` (written by the statusLine's own
`rate_limits` reading — see below) and `pace.jsonl`'s chunk history to decide
`continue` / `sleep` / `stop` / `probe` for each configured ceiling
(`five_hour` / `seven_day`, default 80 / 90). A reading is a lower bound, and
is projected forward when stale; when headroom for one more chunk is gone,
`five_hour` sleeps until its reset (plus a margin), `seven_day` stops (a
weekly reset is too far off to sleep through — it hands off instead). With no
fresh usage reading for 3 chunks running, it stops with "no usage signal"
unless the policy's `allow_blind` is set, in which case it sleeps
`blind_gap_s` instead.

**Ownership.** `next`, `recover`, `end-chunk`, `done`, `fail` and `lease` act
for an owner — the Claude Code session id (`$CLAUDE_CODE_SESSION_ID`), never
a minted token. A `/loop` also holds a lease so a sleeping loop still owns
the sweep between chunks; `/clear` changes the session id, so a chunk or
lease left by the pre-`/clear` session needs `takeover`. `set-policy` changes
a running sweep's policy (a ceiling, `chunk_size`, deviation, `allow_blind`)
only on the user's explicit request.

**Exit codes** (`sweep_state.py`):

| code | meaning |
|---|---|
| 0 | ok |
| 1 | `check` printed `SWEEP FAIL` |
| 2 | invalid input, refused, or no such sweep |
| 3 | `next`: nothing pending or running — the sweep is finished |
| 4 | owned by another live session (open chunk or held lease) |
| 5 | the sweep lock is busy |

Needs Maestro's statusLine (`./install.sh`) for pacing — `rate_limits` only
appears in the statusLine payload on a Pro/Max plan, after the first API
response of a session — and a foreground session, since whether a
backgrounded session renders the statusLine at all is undocumented.

---

## Staying awake

The ledger holds a `caffeinate` assertion for exactly as long as agents are in
flight and drops it when the last one finishes. One per session, tracked by
`caffeinate.pid` in the session dir.

Two bounds stop it pinning the machine: a `running` node stops counting as live
after `MAESTRO_CAFFEINATE_MAX_IDLE`, and every assertion carries a `-t` ceiling
so one orphaned by a deleted session dir dies on its own.

---

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `MAESTRO_ROOTS` | `~/Documents/Development`, `~/Development`, `~/code`, `~/src` | Colon-separated repo search roots |
| `MAESTRO_PORT` | `7717` | Board port |
| `MAESTRO_LAN` | off | `1` binds the board to all interfaces |
| `MAESTRO_TERMINAL` | auto | `warp` or `ghostty` |
| `MAESTRO_DEBUG` | off | `1` sends hook errors to stderr |
| `MAESTRO_QUIET` | off | `1` suppresses every alert channel except the board |
| `MAESTRO_SOUND` | `Submarine` | macOS notification sound |
| `MAESTRO_NTFY_TOPIC` | unset | ntfy.sh topic for phone push |
| `MAESTRO_NTFY_SERVER` | `https://ntfy.sh` | Self-hosted ntfy |
| `MAESTRO_REORCH` | on | `0` disables the re-check entirely |
| `MAESTRO_STALL_SECONDS` | `600` | Silence (no tool call, no transcript write) before an agent counts as stalled |
| `MAESTRO_REPORT_GRACE` | `5` | Seconds to let a resend land before reporting loss |
| `MAESTRO_REPORT_MAX` | `40000` | Characters of a report kept on disk |
| `MAESTRO_INBOX_PRUNE` | on | `0` stops maestro removing stale idle pings from the mailbox |
| `MAESTRO_IDLE_CATCH` | `1.5` | Seconds to wait on `TeammateIdle` for the frame to be written |
| `MAESTRO_LEDGER_OFF` | off | `1` disables ledger writes entirely |
| `MAESTRO_CAFFEINATE` | `-ims` | Flags to pass; `0` never asserts |
| `MAESTRO_CAFFEINATE_TTL` | `3600` | Hard ceiling on one assertion |
| `MAESTRO_CAFFEINATE_MAX_IDLE` | `900` | Idle seconds before a node stops holding it |
| `MAESTRO_REAP_SECONDS` | `900` | Idle seconds before a `running` node is orphaned |
| `MAESTRO_PENDING_TTL` | `600` | Age limit on an unmatched dispatch |
| `MAESTRO_LESSONS_DIR` | `~/.claude/maestro/lessons/` | Personal lesson store directory |
| `MAESTRO_LESSONS_APPROVALS` | `lessons-approved.jsonl` next to the store | Approvals ledger path |
| `MAESTRO_LESSONS` | on | `0` disables capture, `flag`, and injection |
| `MAESTRO_TWEAKS_FILE` | `~/.claude/maestro/tweaks.md` | Tweak-request file that `lessons.py tweak` appends to |
| `MAESTRO_USAGE_FILE` | `~/.claude/maestro/usage.json` | Statusline usage snapshot read by `pace.py`/`sweep_state.py` |
| `MAESTRO_SWEEPS_DIR` | `<git toplevel>/.claude/maestro/sweeps` | Sweep state root |
| `MAESTRO_LOCK_STALE_S` | `10.0` | Seconds before a sweep lock is considered stale under contention |
| `MAESTRO_LOCK_TIMEOUT_S` | `4.0` | Seconds a sweep command retries a busy lock before giving up |
| `MAESTRO_LOCK_HARD_STALE_S` | `3600` | Seconds before a lock holder on another host is presumed gone |

---

## Directories it writes

```
.claude/maestro/
  current                  pointer to the newest session dir
  <session-id>/
    events.jsonl           every hook payload, appended
    state.json             the reconstructed agent tree; each finished node
                           keeps the tail of the agent's best report (`result`),
                           its `report_status`, and its token spend
                           (`tokens`, `usage`)
    reports/<agent-id>.md  the full report, kept whether or not the harness
                           managed to deliver it
    recheck.json           findings already said, so none repeats
    caffeinate.pid         sleep assertion held while agents run
    question.json          pending blocking question
  shots/                   visual-reviewer screenshots
  baselines/               reference images
  mocks/                   HTML mocks from /brainstorm
  PLAN.md                  the conductor's durable ledger
  HANDOFF.md               the validated phase-boundary handoff (/handoff)
  sweeps/<slug>/           frozen plan, item index, policy versions, findings,
                           amendments, and pace history for one sweep

~/.claude/maestro/
  lessons/                 personal lesson store ($MAESTRO_LESSONS_DIR)
    lessons.md             the personal lesson file, git-tracked, append-only
    candidates.jsonl       append-only, gitignored, capture queue
    rejected.jsonl         append-only, tracked in the store's own git repo
  lessons-approved.jsonl   the approvals ledger ($MAESTRO_LESSONS_APPROVALS)
  tweaks.md                Maestro changes requested via `lessons.py tweak`
  sessions/<session>       where that session's ledger is pinned
                           ($MAESTRO_SESSIONS_DIR; pruned after 30 days)
  usage.json               latest statusline rate-limit snapshot, read by
                           pace.py/sweep_state.py

<repo>/.claude/maestro-lessons.md   published, repo-scoped lessons (R-NNN) —
                                    uncommitted by `publish`; the user commits it
```

Add `.claude/maestro/` to `.gitignore`; commit `baselines/` if you want visual
regression references in the repo, and commit `.claude/maestro-lessons.md` if
you want repo-scoped lessons shared with the team.

---

## Known limits

- **A `section-lead` cannot await its own children.** Dispatch is asynchronous
  and a subagent has no way to block, so it returns on the launch confirmation
  with an empty summary while its children report to the conductor instead.
  Prefer flat fan-out from the conductor for work whose results you need; use
  `section-lead` for isolation, not for summarising. This needs a platform
  capability, not a prompt change.
- Parent linkage depends on worktree naming — see *How the tree is built*.
- Token totals refresh on stop events only: a live agent shows nothing until
  its first `SubagentStop`, and an agent whose stop never matches its node
  (see reaping) keeps `tokens: 0`. Totals are floor values, not billing.
- The board is read-only apart from spawning and the music transport.
- Split-pane agent teams need tmux or iTerm2. In-process mode is the default.
- **Lessons ledger is tamper-evident, not tamper-proof.** It catches
  accidental or naive edits, not deliberate forgery by code that already has
  write access to `~/.claude` — there is no secret backing the chain. Nothing
  mechanically proves a human said yes; the docs bind the conductor.
- Two repos with the same directory basename share one lesson scope (and so
  each other's repo-scoped personal lessons and trust records).
- `lessons.py inject`'s pending-candidate count is read by folding the whole
  of `candidates.jsonl` at every `SessionStart`.
- Sweep pacing's freshness signal assumes `cost.total_api_duration_ms`
  advances in step with a `rate_limits` update — both are documented Claude
  Code fields, but that they co-update is not independently verified.
- Whether a backgrounded (agent view) or headless (`-p`) session renders the
  statusLine at all is undocumented; a sweep loop that goes blind stops with
  a reason that says so rather than guessing.
- A sweep's loop lease depends on the sweep skill renewing it before every
  wakeup; `/clear` changes the session id, so a lease or open chunk from
  before a `/clear` needs `takeover`.
- `sweep_state.py add` has no idempotent retry after a mid-write crash
  between its amendment and index writes — re-run `add`.
- `rate_limits`, and so all sweep pacing, only exists for a Pro/Max plan
  after the session's first API response; it is absent on API-key billing.

---

## Troubleshooting

**Tree rows look flat.** Confirm the plugin loaded with `claude plugin list`,
then test the renderer:

```bash
echo '{"columns":100,"tasks":[{"id":"1","type":"scout","status":"running","description":"t","model":"haiku"}]}' \
  | python3 maestro/scripts/subagent_tree.py
```

**Nothing in `.claude/maestro/`.** Hooks are disabled or untrusted. Run
`/hooks` and confirm the maestro entries. `MAESTRO_DEBUG=1` surfaces errors.

**Board says "no session yet".** The ledger writes on the first hook. Run one
turn, then reload.

**Ghostty tab opens and dies.** Ghostty runs `command` under
`bash --noprofile --norc` with the app's GUI PATH, so a binary in `~/.local/bin`
is not found. The spawner resolves `claude` to an absolute path; if you changed
that, resolve it yourself.

**Source edits have no effect.** See *Updating* above.

---

## License

MIT. See [LICENSE](LICENSE).
