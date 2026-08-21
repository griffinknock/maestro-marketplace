# Maestro

A Claude Code plugin that runs a session as a conductor: one orchestrator fans
work out to model-tiered subagents, and the tree it builds is visible in the
terminal and on a local board.

Requires `python3` and `git`. Terminal spawning, notifications and the sleep
assertion are macOS-only; everything else is portable. No pip packages.

---

## Install

```bash
./install.sh
```

Validates the scripts, registers the marketplace, installs the plugin, and
writes `statusLine`, `outputStyle` and agent-teams settings into
`~/.claude/settings.json` (backing up the old file first).

Manual equivalent:

```bash
claude plugin marketplace add ./maestro-marketplace
claude plugin install maestro@maestro-marketplace
```

Then set `statusLine` in `~/.claude/settings.json` to
`python3 "<path>/maestro/scripts/statusline.py"` with `"refreshInterval": 3`.

**Updating.** `claude plugin install` is a no-op when the plugin is already
installed, regardless of version — it will not pick up source edits. To apply
changes:

```bash
claude plugin uninstall maestro@maestro-marketplace
claude plugin install maestro@maestro-marketplace
```

Hooks, agents and `subagentStatusLine` run from the installed copy under
`~/.claude/plugins/cache/`. Only `statusLine` points at the source tree, so
editing source changes the status line immediately and nothing else.

Optional: `brew install terminal-notifier` makes notifications clickable
(they open the board). Without it `osascript` is used, which cannot carry a
click action.

---

## What it adds

| Piece | Where |
|---|---|
| Maestro output style | System prompt — wave planning, brainstorm gate, mermaid graphs |
| 7 tiered agents | `scout` `scribe` (haiku) · `builder` `section-lead` `visual-reviewer` `adversary` (sonnet) · `surgeon` (opus) |
| Indented agent tree | Agent panel, via `subagentStatusLine` — nesting, tier colour, context meter, elapsed |
| Two-row status line | Model · project · branch · cost, then context · agent census · `depth N/5` · board link |
| Board | `http://127.0.0.1:7717` — fan-out DAG, lanes, worktrees, screenshots |
| Ledger | `.claude/maestro/<session>/{events.jsonl,state.json}` |
| Re-orchestration check | One message per dispatch batch, or silence; re-delivers swallowed reports |
| Mailbox prune | Drops stale teammate idle pings before the conductor is ever woken by them |
| Sleep assertion | Holds `caffeinate` while agents are in flight |
| Ghostty config | `ghostty/config` — keybinds, theme pairing, shell integration |

`scout` and `scribe` are read-only. `builder`, `section-lead` and `surgeon`
declare `isolation: worktree` and run in their own checkout.

---

## Commands

| Command | Does |
|---|---|
| `/orchestrate <task>` | Plans waves — agent, model, worktree, dependencies, cost — then stops and asks |
| `/brainstorm <topic>` | No code, no edits. Prior-art recon (web + repo), then one numbered question per message, then alternatives with trade-offs, ending in an `/orchestrate` score |
| `/board` | Starts the board, returns the link. `--lan` also binds to the network |
| `/tree` | Prints the tree, waves, worktrees and a mermaid diagram as text |
| `/look <url>` | Screenshot pass across viewports, compared against a mock or baseline |

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

`reorchestrate.py` audits the shape of the orchestra and pushes findings into
the conductor's context. Every message it sends costs a full main-loop turn on
a large context, so the budget it works to is **one message per dispatch batch,
or silence**.

### It never speaks mid-batch

One assistant message dispatching five agents fires `PostToolUse(Agent)` five
times, each before the other four subagents have emitted `SubagentStart`. A
count taken there is not slightly off, it is meaningless — that is how "only 1
agent in flight, launch more" arrived *during* a five-agent launch, and "0
agents in flight, the wave is done" one beat after a two-agent one.

So an `Agent` call only **records** the batch. The verdict is computed on the
first ordinary tool call after the batch has settled, and the census counts
dispatches seen at `PreToolUse` that have not yet matched a `SubagentStart`
(`state["pending"]`, within `MAESTRO_DISPATCH_GRACE`) as in flight. If the
count changed inside one assistant message, that is one event, not N.

### It only reports what would change the next decision

A stalled agent, the same tier failing twice, two live agents on one file,
depth 5, three consecutive one-agent dispatches, and a finished wave the
conductor has spent eight tool calls working around by hand. Every finding is
fingerprinted and delivered at most once.

Two rules were deleted rather than fixed. *Lane balance* ("N of M lanes are
idle while one keeps working") counted a lane whose agents had all **finished**
as idle, so it fired on ordinary sequential progress; excluding complete lanes
leaves a rule that can almost never fire honestly, and serial drift already
carries that doctrine. *Cheap-work-on-an-expensive-model* judged a live agent
from its tool counts, which is the same mistake the second opinion made below,
and a running agent cannot be re-tiered anyway.

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
the re-check hands the report over in full on the conductor's next turn:

```
REPORT RECOVERED — scout-web-referral finished, but the copy delivered to you
was empty or a bare protocol frame. Its real report, from its own transcript,
is below. Do not ask it to resend.
```

and when nothing is recoverable it says `REPORT NOT DELIVERED` instead of
letting loss look like a clean finish. This is the one class that always earns
a turn.

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

### The second opinion is off by default

When the rules smell something they cannot judge, a detached Haiku call can
give a second opinion that lands on a later turn. It used to be handed a tool
signature and a model name and nothing else, and its answer was printed against
whichever dispatch finished next. It called a 14,000-line attribution trace
"trivial lookups, should use Haiku" and an opus builder writing fail-closed
pagination tests "a read operation".

It now receives the **dispatch prompt** it is judging, must answer
`CONFIDENCE: high` with an explicit `FINDING:`, and the agent ids it judged are
written beside the verdict — if any of them has finished by the time the
verdict is read, it is discarded rather than attributed to a different
dispatch. It is still `MAESTRO_REORCH_LLM=1` to enable: a confidently wrong
tier recommendation costs the conductor more reasoning than silence does, and
it has to earn its way back on.

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

## Blocking questions

The conductor writes `question.json` before it asks. That fires a macOS
notification, an OSC-9 terminal flag, an ntfy push, and a full-width card on the
board with the options as buttons. Clicking one copies its number.

Asking does not stop the orchestra — unblocked lanes keep running.

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
| `MAESTRO_REORCH_LLM` | `0` | `1` enables the detached second opinion |
| `MAESTRO_REORCH_COOLDOWN` | `90` | Seconds between second opinions |
| `MAESTRO_REORCH_SETTLE` | `2` | Seconds a dispatch batch must be quiet before it is judged |
| `MAESTRO_DISPATCH_GRACE` | `60` | Seconds an unstarted dispatch still counts as in flight |
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

---

## Directories it writes

```
.claude/maestro/
  current                  pointer to the newest session dir
  <session-id>/
    events.jsonl           every hook payload, appended
    state.json             the reconstructed agent tree; each finished node
                           keeps the tail of the agent's best report (`result`)
                           and its `report_status`
    reports/<agent-id>.md  the full report, kept whether or not the harness
                           managed to deliver it
    recheck.json           dispatch-batch bookkeeping and findings already said
    caffeinate.pid         sleep assertion held while agents run
    question.json          pending blocking question
  shots/                   visual-reviewer screenshots
  baselines/               reference images
  mocks/                   HTML mocks from /brainstorm
  PLAN.md                  the conductor's durable ledger
```

Add `.claude/maestro/` to `.gitignore`; commit `baselines/` if you want visual
regression references in the repo.

---

## Known limits

- **A `section-lead` cannot await its own children.** Dispatch is asynchronous
  and a subagent has no way to block, so it returns on the launch confirmation
  with an empty summary while its children report to the conductor instead.
  Prefer flat fan-out from the conductor for work whose results you need; use
  `section-lead` for isolation, not for summarising. This needs a platform
  capability, not a prompt change.
- Parent linkage depends on worktree naming — see *How the tree is built*.
- The board is read-only apart from spawning and the music transport.
- Split-pane agent teams need tmux or iTerm2. In-process mode is the default.

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

**Ghostty theme is wrong.** On macOS Ghostty reads both
`~/.config/ghostty/config` and
`~/Library/Application Support/com.mitchellh.ghostty/config`, applying the
latter last. A `theme =` there silently overrides. `+validate-config` will not
catch it — compare `+show-config` against the file.

**Ghostty tab opens and dies.** Ghostty runs `command` under
`bash --noprofile --norc` with the app's GUI PATH, so a binary in `~/.local/bin`
is not found. The spawner resolves `claude` to an absolute path; if you changed
that, resolve it yourself.

**Source edits have no effect.** See *Updating* above.
