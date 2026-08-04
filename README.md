# Maestro

A conductor-first setup for Claude Code: one orchestrator that fans work out to
model-tiered subagents up to depth 5, an indented live agent tree in the
terminal, and a local board with the fan-out graph, worktrees, screenshots,
clickable links, and copyable commands.

Built against Claude Code **v2.1.220**. Verified end to end.

---

## Install

```bash
./install.sh
```

That validates the scripts, registers the marketplace, installs the plugin, and
writes `statusLine` / `outputStyle` / agent-teams settings into
`~/.claude/settings.json` (with a timestamped backup of your old file).

Manual equivalent, if you'd rather not run the script:

```bash
claude plugin marketplace add ./maestro-marketplace
```

```bash
claude plugin install maestro@maestro-marketplace
```

Then set `statusLine` in `~/.claude/settings.json` to
`python3 "<path>/maestro/scripts/statusline.py"` with `"refreshInterval": 3`.

Requirements: `python3` (macOS ships it), `git`. `tmux` only if you want
split-pane agent teams. No pip packages.

---

## What you get

| Piece | Where it shows |
|---|---|
| **Maestro output style** | The system prompt. Every session opens as a conductor: score block, brainstorm gate, wave planning, mermaid diagrams. |
| **7 tiered subagents** | `scout` `scribe` (haiku) · `builder` `section-lead` `visual-reviewer` `adversary` (sonnet) · `surgeon` (opus) |
| **Indented agent tree** | The agent panel under your prompt, via `subagentStatusLine` — real `├─ │ └─` nesting, tier colour, context meter, effort chip, elapsed, OSC-8 link into the board. |
| **Two-row status line** | Model · project · branch/worktree · cost, then context meter · live agent census by tier · `depth N/5` · clickable board link. |
| **The board** | `http://127.0.0.1:7717` — auto-collapsed agent folders, live mermaid fan-out DAG, worktree list with ready-made diff commands, screenshot gallery, copy buttons everywhere. Light and dark, and it remembers which you picked. |
| **Ledger** | `.claude/maestro/<session>/{events.jsonl,state.json}` — every hook event, and the reconstructed tree. |
| **Re-orchestration check** | Runs on every task completion. Deterministic rules first, a Haiku second opinion only on suspicion. Findings land in the conductor's context as a system reminder. |
| **Loud interrupts** | Blocking questions fire a macOS notification with sound, an OSC-9 terminal flag, an ntfy push to your phone, and a full-width question card on the board with the options as clickable buttons. |
| **Ghostty config** | `ghostty/config` — versionable terminal setup with Maestro keybinds, light/dark theme pairing, and shell integration. See `ghostty/SETUP.md`. |

## The rail — the board as a control surface

The board watches **every repo under your development roots**, not just the one
you started it in. The left rail lists one item per orchestration: your name,
your colour, the repo underneath, and live counts. Click to switch the whole
board to it. Double-click to rename and cycle the colour.

```
MAESTRO_ROOTS=~/Documents/Development:~/work
```

Defaults to `~/Documents/Development`, then `~/Development`, `~/code`, `~/src`.
Colon-separated, same as `PATH`.

**+ New orchestration** opens a form — name, colour, repo, and what you want
done — and spawns a real terminal tab: correct working directory, `claude`
running with your prompt, tab titled and coloured to match the rail item.

**Warp is the default target.** It writes a Tab Config to
`~/.warp/tab_configs/maestro-<slug>.toml` and opens `warp://tab_config/<slug>`.
That is more robust than driving a terminal over AppleScript — Warp reads a file
we own, so there is no scripting dictionary to guess at. Turn on
Settings → Appearance → Tabs → **Use vertical tab layout** and each spawned tab
carries its own branch, worktree, PR status, diff stats, and a coloured
agent-status badge.

Ghostty is the fallback, driven over AppleScript, and is used automatically if
Warp is not installed. Force either one:

```
MAESTRO_TERMINAL=warp      # or: ghostty
```

One behavioural difference worth knowing: the Warp path passes the prompt as an
argument, so it **submits** when the tab opens. The Ghostty path types it into
the composer without sending. You wrote the prompt in the form and clicked the
button, so submitting is the honest reading of that — but if you want the pause,
use Ghostty.

The name and colour are staked out before the session exists — the spawn writes
`pending-meta.json`, and the first hook to fire claims it. So the rail item is
already labelled by the time the first agent appears.

Requires Warp (any recent build) or Ghostty 1.3+. The Ghostty path additionally
needs macOS Automation permission, which macOS prompts for on first use. If
either fails the spawn returns the error and nothing else breaks — and a failed
Warp spawn falls through to Ghostty automatically.

**Warp does not bill you for this.** Their docs are explicit: *"Third-party
agent CLIs run directly don't use platform credits when you run `claude`,
`codex`, or another agent CLI outside of Oz."* Only Warp's own agent spends
credits. Turn telemetry off in Settings → Privacy and, per their privacy docs,
*"no console interactions are ever persisted on Warp's servers."* Note the free
plan requires telemetry on to use *Warp's* AI — irrelevant if you only ever run
Claude Code in it.

**Control actions are loopback-only.** `/api/spawn` and `/api/meta` refuse any
request that did not come from the Mac itself, so serving the board over `--lan`
gives your phone a read-only view and no ability to run anything.

## Lanes and the re-check

Every direct child of the conductor opens a **lane**, and everything it spawns
belongs to it. The board shows lanes as cards — work name, agent count, a
progress bar split done/running/failed. Click one to jump to it in the tree.

When any task completes, `reorchestrate.py` audits the shape in a few
milliseconds and pushes what it finds straight into the conductor's context:

- only one agent in flight when the plan had independent work left
- an agent with no tool activity for five minutes
- most lanes idle while one keeps working — a fan-out that collapsed into a queue
- two live agents touching the same file
- depth 4 or 5 reached
- an opus agent that has only been reading — scout work on the expensive tier
- the same agent type failing twice, which should have escalated to `surgeon`

If nothing trips, it says nothing. When something trips that the rules can't
judge, it spawns a **detached** Haiku call whose opinion arrives on the *next*
completion — so the hook itself never blocks, and the expensive check is always
one task behind rather than in the critical path.

```
MAESTRO_REORCH_LLM=0        rules only, never spend a Haiku call
MAESTRO_REORCH_COOLDOWN=90  minimum seconds between second opinions
```

## Staying awake for the length of the run

A fan-out that puts the Mac to sleep halfway through is a fan-out you get to run
twice. The ledger already knows exactly what is in flight, so it holds a
`caffeinate` assertion for precisely as long as something is running and drops
it the moment the last agent finishes — no babysitting, and no machine left
pinned awake overnight because you forgot.

One assertion per session, tracked by `caffeinate.pid` in the session dir.
Hooks fire on every tool call, so the release lands within a beat of the wave
ending.

```
MAESTRO_CAFFEINATE=0            never assert
MAESTRO_CAFFEINATE=-dims        flags to pass; default -ims keeps the system
                                awake but still lets the display sleep
MAESTRO_CAFFEINATE_TTL=3600     hard ceiling on any single assertion
MAESTRO_CAFFEINATE_MAX_IDLE=900 a `running` node this quiet stops holding it
```

Two things stop it becoming a machine that never sleeps: a node whose stop
event never arrives stops counting as live after `MAX_IDLE`, and every
assertion carries a `-t` ceiling so one orphaned by a deleted session dir dies
on its own.

## Getting asked, without being the bottleneck

The conductor writes `question.json` before it asks. That one file fires every
channel at once: macOS notification with sound, terminal flag, phone push, and
the board takeover. Clicking an option copies its number to your clipboard.

The doctrine is in the output style: asking does **not** stop the orchestra.
The conductor keeps every unblocked lane running while it waits and tells you
which ones (`waiting on Q1 · lanes 2 and 3 still working`). It only goes idle
when every remaining path depends on your answer.

```
MAESTRO_NTFY_TOPIC=maestro-<random>   phone push via ntfy.sh
MAESTRO_NTFY_SERVER=https://...       self-hosted ntfy instead
MAESTRO_SOUND=Submarine               macOS notification sound
MAESTRO_QUIET=1                       board only, nothing interrupts you
```

## Commands

| Command | Does |
|---|---|
| `/orchestrate <task>` | Plans the whole thing as waves — agent, model, worktree, dependencies, mermaid graph, cost shape — then stops and asks before executing. |
| `/brainstorm <topic>` | No code, no edits. 2–4 real alternatives with trade-offs, plus an HTML mock if the question is visual. |
| `/board` | Starts the board server and gives you the link. |
| `/tree` | Prints the current tree, waves, worktrees, and a mermaid diagram as text. |
| `/look <url>` | Parallel screenshot pass across viewports, compared against a mock, Figma frame, or stored baseline. |

## The depth-5 contract

Claude Code caps subagent nesting at 5 levels. Maestro makes that budget
explicit rather than implicit: the conductor prefixes every `section-lead`
prompt with

```
[maestro depth=1/5 · fanout<=4 · parent=conductor]
```

and each section-lead decrements it for its own children, refusing to delegate
at `depth=5`. The status line and board both surface the deepest live level so
you can see it drifting before it becomes a problem.

## Phone and iPad

Two halves, and they solve different problems.

**The session** — use Claude Code's own remote control. It syncs the running
CLI session to the Claude app's Code tab and to `claude.ai/code`, including
subagent and workflow progress (v2.1.207+). Execution stays on your Mac.

```
/rc maestro
```

Turn on its push notifications once, in `/config`:

- **Push when actions required** (`actionRequiredNotifEnabled`) — permission
  prompts and decisions
- **Push when Claude decides** (`agentPushNotifEnabled`) — long tasks finishing

That is the better channel for "the conductor needs a call", because it is
first-party and the answer goes straight back into the session. Maestro's ntfy
push stays useful as a redundant one that fires even when remote control is off.

**The board** — there is no built-in tunnel, so serve it yourself:

```
/board --lan
```

The board then prints a tailnet address (if Tailscale is running) and a LAN
address, shows a scannable QR under **Connect a device**, and installs as a real
app via Share → Add to Home Screen — its own icon, no Safari chrome.

| Route | Reach | Setup |
|---|---|---|
| `--lan` | Same Wi-Fi | None |
| Tailscale | Anywhere, private | `brew install --cask tailscale`, sign in on both devices |
| `tailscale serve 7717` | Anywhere, HTTPS, no port in the URL | Tailscale + one command |

Tailscale is the one worth doing. `--lan` alone means anyone on that network can
read the board, which is fine at home and a bad idea at a coffee shop.

## Directories it writes

```
.claude/maestro/
  current                  pointer to the newest session dir
  <session-id>/
    events.jsonl           every hook payload, appended
    state.json             the reconstructed agent tree
    caffeinate.pid         the sleep assertion held while agents are running
  shots/                   visual-reviewer screenshots
  baselines/               reference images for regression comparison
  mocks/                   throwaway HTML mocks from /brainstorm
  PLAN.md                  the conductor's durable ledger (survives /compact)
```

Add `.claude/maestro/` to `.gitignore` unless you want the baselines committed —
committing `baselines/` and ignoring the rest is a reasonable split.

## Turning bits off

- **Status line only, no board**: skip `/board`. Nothing depends on it.
- **No agent teams**: remove `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` from
  `~/.claude/settings.json`. Subagents and the depth-5 tree are unaffected —
  teams are a separate, flatter mechanism that caps at one level.
- **Different output style**: `/output-style` to switch. The agents, hooks, tree
  and board all keep working; you just lose the conductor doctrine.
- **Quieter hooks**: the ledger is wired to 11 events in
  `maestro/hooks/hooks.json`. Dropping `PreToolUse`/`PostToolUse` costs you
  per-agent tool and file tracking but keeps the tree.

## Troubleshooting

**Tree rows look default (flat).** `subagentStatusLine` is shipped in the
plugin's `settings.json`; confirm the plugin loaded with `claude plugin list`.
Test the renderer directly:

```bash
echo '{"columns":100,"tasks":[{"id":"1","type":"scout","status":"running","description":"test","model":"haiku"}]}' | python3 maestro/scripts/subagent_tree.py
```

**Board says "no session yet".** The ledger only writes once hooks fire. Run one
turn in Claude Code, then reload. Check `.claude/maestro/current` exists.

**Graph area shows source instead of a diagram.** Mermaid loads from a CDN; if
you're offline or behind a proxy it degrades to copyable source. Warp renders
that source natively if you paste it.

**Nothing at all in `.claude/maestro/`.** Hooks are disabled or untrusted — run
`/hooks` inside Claude Code and confirm the maestro entries are listed. Set
`MAESTRO_DEBUG=1` to send ledger errors to stderr.

## Known limits

- **Parent linkage comes from worktree names, not from the hook payload.**
  `PreToolUse(Agent)` carries no `agent_id`, so the agent that made a dispatch
  is anonymous — and `transcript_path` and `prompt_id` are identical for every
  agent in a session, so there is nothing there to correlate on either. What
  does survive: a delegating agent runs in `.claude/worktrees/agent-<its id>`
  and its children inherit that directory, so the worktree a node sits in names
  its parent. That is what builds the tree below depth 1.
  Consequences worth knowing:
  - Only `section-lead`, `builder` and `surgeon` declare `isolation: worktree`.
    A read-only agent that somehow delegates would have no worktree, and its
    children would fall back to hanging off the conductor.
  - A delegating agent's own node is reconstructed from the matching entry in
    `pending`, oldest-first among worktree-isolated types. With several
    same-type leads launched at once the descriptions can still swap between
    them; depth, lane and shape stay correct.
  - Nodes carry `inferred: true` when they were rebuilt this way rather than
    seen on a payload.
- Nested agents arrive *named* rather than typed — `agent_type` is whatever the
  caller called them (`scout-apiclient`). The type is recovered from the leading
  segment when it names a real agent, and the original is kept as `name`. A
  fully custom name that matches no agent stays as-is and tiers as sonnet.
- A subagent that never emits a matchable `SubagentStop` is reaped after
  `MAESTRO_REAP_SECONDS` (900) and marked `orphaned`, so it stops inflating the
  live count and holding the caffeinate assertion open.
- The board is read-only. It shows you the orchestra; it doesn't conduct it.
- Split-pane agent teams need tmux or iTerm2 — not Warp, Ghostty, or Windows
  Terminal. In-process mode works everywhere and is the default here.
