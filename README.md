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
| Re-orchestration check | Runs on task completion; deterministic rules, optional Haiku second opinion |
| Sleep assertion | Holds `caffeinate` while agents are in flight |
| Ghostty config | `ghostty/config` — keybinds, theme pairing, shell integration |

`scout` and `scribe` are read-only. `builder`, `section-lead` and `surgeon`
declare `isolation: worktree` and run in their own checkout.

---

## Commands

| Command | Does |
|---|---|
| `/orchestrate <task>` | Plans waves — agent, model, worktree, dependencies, cost — then stops and asks |
| `/brainstorm <topic>` | No code, no edits. Alternatives with trade-offs |
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

Each time an `Agent`/`Task` tool call returns to its caller (and on
`TaskCompleted`), `reorchestrate.py` audits the shape and pushes findings into
the conductor's context. It reports: a single agent in flight, an agent
idle for 5 minutes, most lanes idle while one works, two live agents on the
same file, depth 4 or 5 reached, an opus agent only reading, and the same agent
type failing twice. Silent when nothing trips and when nothing has run yet.

It must never be wired to `SubagentStop`: context injected there is delivered
to the *stopping subagent*, which spends its final message answering the
re-check instead of returning its report — the conductor then receives
"nothing further to do" while the findings sit unread in the subagent's
transcript. The script also guards against this wiring internally. As a second
layer, every agent's definition ends with a resend rule: any message that
arrives after it has reported gets the same `RETURN:` block again, verbatim.

When the rules cannot judge, it spawns a detached Haiku call whose opinion
arrives on the *next* completion, so the hook never blocks. That child runs with
`MAESTRO_LEDGER_OFF=1` so it cannot write its own ledger.

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
| `MAESTRO_REORCH_LLM` | `1` | `0` = rules only, never spend a Haiku call |
| `MAESTRO_REORCH_COOLDOWN` | `90` | Seconds between second opinions |
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
                           keeps the tail of the agent's final reply (`result`)
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
