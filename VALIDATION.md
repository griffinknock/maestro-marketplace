# Maestro — validation handoff

You are validating a Claude Code plugin called **Maestro** that was built in a
Linux sandbox and has never run on macOS. Your job is to find what is broken,
fix what you can, and report the rest precisely.

Work through this top to bottom. **Do not skip to fixing.** Run each check,
record the actual output, and only then decide whether it passed.

## Ground truth

| | |
|---|---|
| Plugin source | `~/Documents/Development/maestro-marketplace` |
| Scripts | `<source>/maestro/scripts/{ledger,subagent_tree,statusline,board,notify,reorchestrate,lessons,lessons_check,sweep_state,pace}.py` |
| Installed as | `maestro@maestro-marketplace` (user scope) |
| Ledger written to | `<repo>/.claude/maestro/<8-char-session-id>/{state.json,events.jsonl}` |
| Board | `http://127.0.0.1:7717` |
| Hook replay harness | `<source>/tests/replay.py` |
| Terminal | Warp (primary spawn target) with Ghostty 1.3.x as fallback |

Debug switches: `MAESTRO_DEBUG=1` (errors to stderr), `MAESTRO_REORCH_LLM=0`
(no Haiku calls), `MAESTRO_QUIET=1` (no notifications), `MAESTRO_ROOTS`,
`MAESTRO_PORT`, `MAESTRO_TERMINAL=warp|ghostty`.

**Read `README.md` in the plugin source before starting.** It describes intended
behaviour; this file describes how to check it.

---

## Priority 1 — never executed on macOS, most likely broken

### 1.1 Spawning a tab (highest risk)

`board.py` spawns via **Warp by default** (writes a Tab Config TOML, then opens
`warp://tab_config/<slug>`) and falls back to **Ghostty over AppleScript**.
Neither path has ever run on macOS. Test the Warp one first — it is the default.

```bash
cd ~/Documents/Development/maestro-marketplace/maestro/scripts && python3 -c "
import board, pathlib
board.sys.platform='darwin'
print(board.spawn_warp('$HOME/Documents/Development/example-app',
      'probe prompt', title='probe', color='violet'))"
```

**Pass:** a Warp tab opens in that directory, titled `probe`, magenta, running
`claude` with the prompt already submitted. Check the generated TOML at
`~/.warp/tab_configs/maestro-probe.toml` parses and matches
https://docs.warp.dev/terminal/windows/tab-configs/ — the schema was read from
docs, not tested.

Confirm vertical tabs are on: Settings → Appearance → Tabs → *Use vertical tab
layout*. Report what metadata actually appears on a Claude Code tab (branch,
worktree, PR, diff stats, agent-status badge).

Then verify Warp is not billing for it. Run a session, then check Settings →
Billing / usage and confirm credits did **not** move. Their docs claim
third-party CLIs do not consume platform credits — verify that empirically.

Only if Warp fails, test the Ghostty fallback below. **Its tab-titling line is
an inference** — it assumes `perform action` accepts a `:parameter` the way
keybind actions do, which the AppleScript docs do not confirm.

Print the exact script without running it:

```bash
cd ~/Documents/Development/maestro-marketplace/maestro/scripts && python3 -c "
import board, sys
board.sys.platform='darwin'
cap={}
def fake(cmd,**kw):
    cap['s']=cmd[-1]
    class R: returncode=0; stderr=''; stdout=''
    return R()
board.subprocess.run=fake
board.spawn_ghostty('$HOME/Documents/Development/example-app','test prompt',title='probe')
print(cap['s'])"
```

Then run that script for real with `osascript` and record the exact error if any:

```bash
osascript -e 'tell application "Ghostty"
  activate
  set cfg to new surface configuration
  set initial working directory of cfg to "'$HOME'/Documents/Development"
  set command of cfg to "claude"
  set initial input of cfg to "probe"
  set t to new window with configuration cfg
end tell'
```

**Pass:** a Ghostty window opens in that directory with `claude` running and
`probe` typed into the composer but NOT submitted.

**If it fails**, work out which line. Common causes: AppleScript is disabled
(`macos-applescript` in the Ghostty config), macOS Automation permission was
declined (System Settings → Privacy & Security → Automation), or the
`new surface configuration` syntax differs in the installed Ghostty version.
Check `ghostty --version` and consult https://ghostty.org/docs/features/applescript.

Then test titling separately:

```bash
osascript -e 'tell application "Ghostty" to perform action "set_tab_title:probe" on (first window)'
```

**If titling fails**, that is expected and already wrapped in a `try` — the tab
still opens untitled. The fix is to drop the `perform action` block in
`spawn_ghostty` and instead append an OSC 2 sequence to `initial input`, or use
a `set name of` assignment if the AppleScript dictionary exposes one. Open
Script Editor → File → Open Dictionary → Ghostty to see the real dictionary and
report what it actually supports.

Finally, the real path — start the board and spawn from the UI:

```bash
cd ~/Documents/Development/maestro-marketplace && MAESTRO_DEBUG=1 python3 maestro/scripts/board.py ~/Documents/Development/example-app
```

Click **+ New orchestration**, fill it in, hit **Open tab**. Report whether the
tab opens, lands in the right directory, has the prompt typed, and is titled.

### 1.2 The terminal agent tree

`subagentStatusLine` replaces the agent-panel rows with an indented tree. It is
shipped in the plugin's `settings.json`; confirm Claude Code actually honours it
from a plugin rather than only from user settings.

```bash
echo '{"columns":110,"tasks":[{"id":"1","type":"maestro:scout","status":"done","description":"find call sites","model":"claude-haiku-4-5","tokenCount":18000,"contextWindowSize":200000,"startTime":'$(python3 -c 'import time;print(int(time.time()*1000)-45000)')'},{"id":"2","type":"maestro:builder","status":"running","description":"rewrite token refresh","model":"claude-sonnet-4-5","tokenCount":121000,"contextWindowSize":200000,"startTime":'$(python3 -c 'import time;print(int(time.time()*1000)-45000)')'}]}' | python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/subagent_tree.py
```

**Pass:** two JSON lines, each with a `content` string containing ANSI colour
codes and `├─`/`└─` box characters.

Then check it live: start `claude` in a repo, dispatch two subagents in one
message, and look at the agent panel below the prompt. **Pass:** rows are
indented and colour-coded by tier, not the default `name · description · tokens`.

**If the rows look default**, the plugin `settings.json` key may not be honoured.
Test by copying `subagentStatusLine` into `~/.claude/settings.json` directly
(pointing at the absolute script path). If that works, it is a plugin-scope
limitation — report it, and change the installer to write it to user settings.

### 1.3 Status line

```bash
echo '{"model":{"display_name":"Opus 4.6"},"workspace":{"current_dir":"'$PWD'"},"cwd":"'$PWD'","context_window":{"used_percentage":63},"cost":{"total_cost_usd":4.82,"total_duration_ms":1920000}}' | python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/statusline.py
```

**Pass:** two lines — model/dir/branch/cost, then a context meter, agent census,
`depth N/5`, and a `◱ board` link. In Ghostty the link should be Cmd-clickable.

Report whether OSC-8 links are actually clickable in Ghostty, and whether the
box-drawing characters in the meter render cleanly with your font.

### 1.4 Notifications

```bash
osascript -e 'display notification "probe" with title "Maestro" sound name "Submarine"'
```

**Pass:** a Notification Centre banner with sound. If nothing appears, check
System Settings → Notifications for the terminal app.

Then the real path:

```bash
cd <a repo with a maestro ledger> && echo '{"hook_event_name":"Notification","cwd":"'$PWD'","notification_type":"agent_needs_input"}' | MAESTRO_DEBUG=1 python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/notify.py
```

**Pass:** JSON on stdout containing `terminalSequence`, plus a banner. Verify
`state.json` gained `needs_input: true` and an `alert` object.

Also confirm the OSC-9 sequence actually flags the Ghostty tab — this is
unverified. If it does nothing, that is cosmetic; note it and move on.

### 1.5 Music transport

```bash
curl -s http://127.0.0.1:7717/api/music
```

With Spotify open and playing, **pass** = `{"available":true,"playing":true,"track":...}`.
Then click ⏭ on the board and confirm the track actually changes. macOS will
prompt for Automation permission for Spotify the first time.

---

## Priority 2 — worked in the sandbox, confirm on real hardware

### 2.1 Hooks fire and the tree is correct

In a real repo, run one session that fans out in parallel:

```
In ONE message dispatch three maestro:scout subagents in parallel to summarise three different files. Then reply with the three summaries.
```

```bash
python3 -c "
import json,glob,os
f=max(glob.glob('.claude/maestro/*/state.json'),key=os.path.getmtime)
s=json.load(open(f))
for n in sorted(s['nodes'].values(),key=lambda x:x['depth']):
  print(f\"{n['id'][:12]:<14} parent={str(n['parent'])[:12]:<14} {n['type']:<16} d={n['depth']} lane={str(n.get('lane'))[:10]:<12} {n['status']}\")
print('unmatched pending:', len(s['pending']))"
```

**Pass:** three scouts, all `parent=root`, all `d=1`, `model=haiku`,
`unmatched pending: 0`.

Then a nested run to exercise depth:

```
Dispatch one maestro:section-lead. Tell it to dispatch two maestro:scout subagents in parallel and summarise their findings.
```

**Pass:** section-lead at `d=1`, both scouts at `d=2` with `parent=<section-lead id>`,
and both scouts sharing the section-lead's `lane`.

**Known weakness to probe:** parent linkage is reconstructed by matching
`PreToolUse(Agent)` in the caller against `SubagentStart` in the child, oldest-
first per agent type, because Claude Code exposes no `parent_agent_id`. Try to
break it — dispatch four scouts from two different section-leads simultaneously
and check whether any child lands under the wrong parent. Report what you find;
depth and shape should stay right even if a description swaps.

### 2.2 Worktree isolation

Ask a `maestro:builder` to make a small real edit. **Pass:** `git worktree list`
shows a new worktree, `state.json` records `worktree` on that node, and the
board's Worktrees panel lists it with a working diff command.

### 2.3 The re-orchestration check

```bash
echo '{"hook_event_name":"SubagentStop","cwd":"'$PWD'"}' | MAESTRO_REORCH_LLM=0 MAESTRO_DEBUG=1 python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/reorchestrate.py
```

On a healthy tree this prints **nothing** — that is a pass, not a failure. It
must also print nothing on `SubagentStop` *ever*, and nothing on any payload
carrying an `agent_id`: context injected there is delivered to the subagent,
which then answers the re-check instead of returning its report.

```bash
echo '{"hook_event_name":"PostToolUse","tool_name":"Bash","agent_id":"a123","cwd":"'$PWD'"}' | python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/reorchestrate.py
```

**Pass:** no output. Any output here is the report-clobbering bug returning.

To confirm the rules still fire, hand-edit a copy of `state.json` so one agent
is `running` with `last_activity` 700 seconds old, then fire an ordinary
`PostToolUse`. **Pass:** a stall finding, once — firing the same event again
must print nothing, because findings are delivered at most once.

Then verify the LLM path does not pollute the ledger. It is off by default now,
so force it with `MAESTRO_REORCH_LLM=1`, wait ~30s, and check that **no new
session directory** appeared under `.claude/maestro/` and that `current` still
points at your real session. The guard is `MAESTRO_LEDGER_OFF=1` in the child
env — confirm it holds.

---

### 2.3.1 Conductor message budget — the five-agent batch

This is the case the messaging field report was written from. Every hook message
costs the conductor a full main-loop turn on a very large context, so what is
being measured is **count**, and of that count how many carried a number that
was true when it was rendered and would have changed a decision.

**Target for a healthy five-agent batch: one message, accurate, actionable —
and zero while the batch is still landing.**

#### Automated, before and after

```bash
cd ~/Documents/Development/maestro-marketplace && python3 tests/replay.py --before && python3 tests/replay.py
```

`tests/replay.py` drives the real `ledger.py` and `reorchestrate.py` with real
payload shapes taken from the field ledgers, under the wiring each revision
actually shipped. `--before` replays a git revision (default `HEAD`) so the two
runs are comparable. It replays a seven-agent session: one five-agent batch in a
single assistant message, two of whose agents hand back only an
`idle_notification` frame; a two-agent batch; and a second Claude session
running concurrently in the same workspace.

Dispatch ordering inside a parallel batch is the one thing the ledgers cannot
show — every recorded batch is sequential — so both orderings are replayed:
`interleaved` (Pre/Post/Start per agent) and `batched` (all Pre, then all Post,
then all Start). The fix must hold for both.

**Recorded on 2026-08-21, macOS 26, Python 3.14:**

| | before (`0.2.1`) | after |
|---|---|---|
| messages reaching the conductor | 3 interleaved / 2 batched | 1 |
| …arriving mid-batch | all of them | 0 |
| …accurate when rendered | 0 | 1 |
| …that would change a decision | 0 | 1 |
| swallowed reports surfaced | 0 of 2 | 2 of 2 |
| sibling session leaking in | 1 message, 1 duplicated line | 0 |

The `before` run reproduces the reported strings verbatim, which is what makes
it a reproduction rather than a story:

```
MAESTRO RE-CHECK — 1 agent(s) in flight, 1 lane(s) open.      (five were launching)
MAESTRO RE-CHECK — 0 agent(s) in flight, 5 lane(s) open.      (two had just launched)
MAESTRO RE-CHECK — 6 agent(s) in flight, 6 lane(s) open.
- Two live agents touched CREATOR-PROGRAM-BINDING-PLAN.md …   (another session's file)
- Two live agents touched CREATOR-PROGRAM-BINDING-PLAN.md …   (and said twice)
```

**Pass:** `python3 tests/replay.py` exits 0 and prints `PASS`. It fails the run
if any message arrives mid-batch, carries a count it cannot justify, would not
have changed a decision, names another session's work, repeats a finding inside
one message, or if either swallowed report fails to surface.

#### Live, in a real session

The replay proves the hooks; only a real session proves the wiring. In a repo
with the plugin installed:

```
In ONE message, dispatch five maestro:scout subagents in parallel to summarise five different files. Then reply with the five summaries.
```

Count what actually reached you. Then read back what the hooks recorded:

```bash
python3 -c "
import json,glob,os
d=os.path.dirname(max(glob.glob('.claude/maestro/*/state.json'),key=os.path.getmtime))
b=json.load(open(os.path.join(d,'recheck.json')))
s=json.load(open(os.path.join(d,'state.json')))
print('open batch   :',b['open'],'size',b['size'])
print('settled sizes:',b['sizes'])
print('findings said:',len(b['said']))
for n in s['nodes'].values():
  if n['id']!='root': print(f\"  {n.get('name') or n['type']:<24} {n.get('status'):<8} {n.get('report_status')}\")"
```

**Pass:** while the wave is still out, `open batch: True size 5`. Once the
conductor has made one ordinary tool call after it, that moves to
`settled sizes: [5]`. A `[1, 1, 1, 1, 1]` means the batch is not being held
together and the debounce is defeated. Once the scouts finish, every one of
them should read `done` / `delivered`; a `recovered` or `missing` there is the
next section.

#### Report loss, deliberately induced

Loss is the one class that must always speak. Fake a swallowed report against a
live ledger — an agent whose last message is a protocol frame, with its real
report still in its transcript:

```bash
D=$(ls -td .claude/maestro/*/ | head -1) && A=$(python3 -c "
import json;s=json.load(open('$D/state.json'))
print(next(k for k,v in s['nodes'].items() if k!='root'))") && T=$(mktemp) && printf '%s\n' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"RETURN:\n  answer: the real report\n  files: /x.py:1\n  gaps: none"}]}}' > "$T" && echo '{"hook_event_name":"SubagentStop","cwd":"'$PWD'","session_id":"'$(basename $D)'","agent_id":"'$A'","agent_type":"scout","last_assistant_message":"{\"type\":\"idle_notification\",\"from\":\"scout\",\"idleReason\":\"available\"}","agent_transcript_path":"'$T'"}' | python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/ledger.py
```

**Pass:** that node now reads `"report_status": "recovered"` and a
`reports/<agent-id>.md` holds the real text. Wait past `MAESTRO_REPORT_GRACE`
(5s), fire any `PostToolUse`, and the re-check must hand back the report inline
under `REPORT RECOVERED`, once. Firing again must print nothing.

With no transcript to recover from, the status must be `missing` and the notice
must say `REPORT NOT DELIVERED`. A swallowed report that surfaces as silence is
the failure this whole section exists to catch.

#### Stale idle pings in the lead's mailbox

A named teammate that goes idle writes a four-field JSON frame into the lead's
mailbox at `~/.claude/teams/session-<session-id>/inboxes/<lead>.json`, and it
arrives wrapped in the ~120-word cross-session security preamble. `ledger.py`
removes those frames when the ledger shows that agent has already delivered or
already stopped, and leaves them when it has not.

`tests/replay.py` builds a faithful replica of that directory and asserts both
directions, plus every frame type that must survive. To watch it live, list a
real mailbox mid-wave:

```bash
cat ~/.claude/teams/session-$(basename $(cat .claude/maestro/current))/inboxes/*.json | python3 -m json.tool | head -40
```

**Pass:** no `"type":"idle_notification"` entry from an agent that has already
reported. Cross-check against the ledger:

```bash
python3 -c "
import json,glob,os
d=os.path.dirname(max(glob.glob('.claude/maestro/*/state.json'),key=os.path.getmtime))
s=json.load(open(os.path.join(d,'state.json')))
print('pruned pings:', s.get('pruned') or 'none yet')"
```

**Must never be removed** — verify by hand at least once, because a false
positive here silently drops a protocol frame the orchestra depends on: prose
from a peer, `permission_request`, `plan_approval_request`, `shutdown_request`,
entries already marked `read`, and pings from agents this session did not
dispatch. Seed one of each into a scratch mailbox, fire any hook, and confirm
the file is byte-identical apart from the stale pings.

Also confirm the failure mode is inert, not destructive:

```bash
D=~/.claude/teams/session-probe/inboxes && mkdir -p $D && echo '{"not":"a list"}' > $D/team-lead.json
echo '{"hook_event_name":"PostToolUse","tool_name":"Bash","cwd":"'$PWD'","session_id":"probe"}' \
  | python3 ~/Documents/Development/maestro-marketplace/maestro/scripts/ledger.py
cat $D/team-lead.json && rm -rf ~/.claude/teams/session-probe
```

**Pass:** the file is unchanged. A mailbox maestro does not understand is a
mailbox maestro does not write to.

Finally, the ordering race. The frame is written by one entry in the teammate's
`Stop` chain while maestro runs from another, in no guaranteed order. On
`TeammateIdle`, maestro waits up to `MAESTRO_IDLE_CATCH` (1.5s) for a frame it
knows is coming. Confirm the hook still returns promptly when no frame ever
arrives — it must not sit for the full hook timeout on every idle transition.

#### Concurrent sessions in one workspace

`.claude/maestro/current` is workspace-global and the last session to fire a
hook owns it. Run two Claude sessions in the same repo, dispatch a wave in each,
and confirm neither conductor is ever told about the other's agents, lanes, or
file collisions. `tests/replay.py` forces the losing side of that race
deterministically; the live check is whether the counts you see match the agents
you personally dispatched.

### 2.4 Board, multi-repo

```bash
curl -s http://127.0.0.1:7717/api/orchestrations | python3 -m json.tool | head -30
```

**Pass:** every repo under `~/Documents/Development` with a ledger appears, with
correct live/done/failed counts.

Confirm the security gate — this must fail:

```bash
curl -s -X POST http://127.0.0.1:7717/api/spawn -H 'Content-Type: application/json' -d '{"repo":"/etc","name":"x","prompt":"y"}'
```

**Pass:** `"repo is outside the watched roots"`.

Start it with `--lan`, then from your phone confirm the board loads read-only and
that a POST from a non-loopback address is refused with 403.

### 2.5 The question flow

Write a question file by hand:

```bash
D=$(cat .claude/maestro/current) && cat > "$D/question.json" <<'EOF'
{"question":"Probe question?","why":"validation","options":[{"label":"Yes","pick":true},{"label":"No"}]}
EOF
```

**Pass:** within ~3s the board shows the amber takeover card with two clickable
options, the tab title changes to `✋ Maestro — needs you`, the mascot switches to
its alert animation, and clicking an option copies `1` to the clipboard.

Then confirm the conductor writes this file *itself* — run `/orchestrate` on
something genuinely ambiguous and check `question.json` appears without you
creating it. **This is the weakest link in the design**: it depends on the output
style being followed. If the conductor asks in prose without writing the file,
report it — the fix is a stronger instruction or a `Stop`-hook fallback that
detects an unanswered question in the transcript.

### 2.6 Ghostty config

```bash
ghostty +validate-config && ghostty +show-config | grep -iE 'theme|keybind|font-family'
```

**Pass:** theme resolves to `Catppuccin Mocha`/`Latte`. Note that
`+validate-config` does NOT catch an unresolvable theme name — compare
`+show-config` against the file.

Test each Maestro keybind: `Cmd+Shift+B` (board), `Cmd+Shift+A` (`claude agents`),
`Cmd+Shift+T` (`/tree`), `Cmd+I` (rename tab), `Cmd+Shift+S` (scrollback file).
Report any that do nothing — the `text:` action syntax is unverified.

### 2.7 Lessons

#### Automated

```bash
cd ~/Documents/Development/maestro-marketplace \
  && python3 tests/lessons_units.py \
  && python3 tests/lessons_capture_units.py \
  && python3 tests/lessons_flow_units.py
```

`lessons_units.py` is the pure validator: parsing, the size/id/Supersedes
caps, the injection budget, the append-only history proof, and the ledger
chain, all hermetic (`MAESTRO_LESSONS_DIR`/`MAESTRO_LESSONS_APPROVALS` point
into a temp root, so nothing here can resolve to `~/.claude`).
`lessons_capture_units.py` drives the capture side (`flag`, `candidates`,
`mark`, `reject`) plus one integration case through `reorchestrate.py`'s real
wiring, the way `tests/replay.py` does, to prove capture never changes what
the conductor sees mid-session. `lessons_flow_units.py` covers the flow
commands — `inject` (the `SessionStart` hook), `accept`, `publish`, `trust`,
`status`, and the `hooks.json` wiring — with one case per adversarial finding
against the flow, each reproducing the original attack and asserting it no
longer works.

**Pass:** all three exit 0 and print their own `PASS`/failure summary.

#### Live — one real lesson, end to end

In a repo with the plugin installed, correct the conductor on something
genuinely about its own orchestration mechanics (dispatch batching, tiering,
a brief), confirm it runs `lessons.py flag --session <id> "<one line>"`, then
run `/maestro:lessons`:

**Pass:** it lists the candidate, drafts a rule scoped to Maestro mechanics
(never a project fact), shows the exact `accept --preview` block verbatim in
a fenced block, and asks with no option pre-marked as the pick. Approve it,
confirm `~/.claude/maestro/lessons/lessons.md` gained the entry and
`lessons-approved.jsonl` gained a matching approval line, then start a new
session and confirm `MAESTRO LESSONS` is injected with that rule present.
Run `python3 maestro/scripts/lessons_check.py` directly and confirm it prints
`LESSONS PASS`.

**Recorded on 2026-09-28, macOS 26.6, Python 3.14, plugin 0.5.0 plus branch
`validate/0.5.0-live`:**

| check | result |
|---|---|
| three automated suites | pass, all exit 0 |
| session start with candidates queued | pass: `1 lesson candidate(s) pending — review with /maestro:lessons`, no error |
| `flag` from a correction | partial: `flag --session` queued the two candidates the 0.5.0 handoff carried; no live in-session correction happened, so "the conductor flags on its own when corrected" is still unexercised |
| review lists, drafts, previews, asks with no pick | pass: 9 pending, 3 drafted (the cap), each asked with its `--preview` block verbatim and no option pre-marked |
| Griffin approves one real lesson | pass: L-001, L-002 and L-003 approved |
| `lessons.md` and ledger agree | pass: three entries; `lessons-approved.jsonl` has three `accept` lines with matching shas (`73f5654f…`, `f9dcb005…`, `a32321e9…`); store git has one commit per entry |
| `lessons_check.py` | pass: `LESSONS PASS — 3 active, 557 chars` |
| `inject` carries the rules | pass: `MAESTRO LESSONS — 3 active …` lists L-001 to L-003 |
| new session shows `MAESTRO LESSONS` | not yet run; needs a Claude Code restart |

Found and fixed (`a328865`): capture deduped per session on the
re-orchestration fingerprint, which is wave-scoped (`serial:3`, `serial:4`,
`inline:6` …), so every wave queued another copy of the same finding. At
review time, 7 of 9 pending candidates were repeats of two nudges.
`_lesson_fingerprint` now collapses non-report fingerprints to their kind.

### 2.8 Sweeps

#### Automated

```bash
cd ~/Documents/Development/maestro-marketplace \
  && python3 tests/pace_units.py \
  && python3 tests/pace_integration_units.py \
  && python3 tests/sweep_units.py \
  && python3 tests/sweep_skill_units.py
```

`pace_units.py` checks `decide()` against synthetic fixtures — policy
validation, burn/gate-burn estimation, staleness projection, the
sleep/stop/continue/probe verdicts per window, and window combination.
`pace_integration_units.py` drives `pace.py` against a real sweep built with
the real `sweep_state.py` CLI rather than synthetic history, specifically to
catch contract drift between the two scripts (this is the suite that caught
the `_more_restrictive` `TypeError` a hand-rolled fixture never combined).
`sweep_units.py` covers `sweep_state.py`'s commands (`new`, `next`, `done`,
`fail`, `finding`, `add`, `amend-plan`, `set-policy`, `lease`, `recover`,
`check`, `status`) and the statusline's usage-snapshot writer. `sweep_skill_units.py`
checks `maestro/skills/sweep/SKILL.md` itself: every `sweep_state.py`/`pace.py`
subcommand or flag it documents actually exists (grepped from their argparse
definitions), the resume/wakeup prompt string is consistent everywhere it
appears and matches the `anchor` hook's own text, the owner always comes from
the session id and never a minted token, and every exit code
`sweep_state.py` returns is documented.

**Pass:** all four exit 0 and print their own `PASS`/failure summary.

#### Live — a real wakeup and an exact resume

The original procedure here set the five-hour ceiling to current usage + 1.
That cannot make the first chunk run: a sweep with no history gates on the
conservative default burn (5 points a chunk), so pace sleeps until the
five-hour reset before running anything, and a gate-tripped sleep lasts
until that reset, one capped hour per wake. Instead, use a small natural
run:

```bash
python3 maestro/scripts/sweep_state.py new --slug validation-probe \
  --plan <plan.md> --items <items.txt> \
  --policy '{"ceilings":{"five_hour":80,"seven_day":90},
             "deviation":"additive","chunk_size":1}'
```

Five trivial items (a haiku scout running `wc -l` on one file each), then
`/loop /maestro:sweep run validation-probe`.

**Pass:** the first turn runs 3 chunks (the per-turn bound) with pace
answering `continue`. The loop then renews the lease (`lease --in 60`) and
calls `ScheduleWakeup(60)` instead of stopping. On the wakeup the same
prompt re-fires: `recover` returns `[]`, `status` shows the same
done/pending counts as before the sleep, and the remaining chunks run.
`next` exits 3, and the END procedure releases the lease and calls
`ScheduleWakeup(stop: true)`. `pace.jsonl` has a matched start/end pair per
chunk with fresh end readings, `policy.json`'s sha matches
`index.policy_sha256`, and `check` prints `SWEEP PASS`. A pace-driven
`sleep` only happens if a chunk moves the five-hour reading. The sleep
arithmetic itself, including waking on a paced sleep and converging on a
target over an hour away, is pinned in `pace_units.py`.

**Recorded on 2026-09-28, macOS 26.6, Python 3.14, plugin 0.5.0 plus branch
`validate/0.5.0-live`:**

| time | point in the loop | pace | outcome |
|---|---|---|---|
| 09:56:47 | turn 1, before chunk 1 | `continue`: five_hour headroom 79.0% clears one chunk, none run yet | i-0001 done 721 |
| 09:57:05 | after chunk 1 | `continue`: paced gap −15s | i-0002 done 1681 |
| 09:57:22 | after chunk 2 | `continue`: paced gap −14s | i-0003 done 1449 |
| 09:57:43 | after chunk 3 | `continue`, 3-chunk bound | `lease --in 60` (until 10:08:43), `ScheduleWakeup(60)`; status 3 done / 2 pending / chunk 3 |
| 09:59:08 | wakeup | `continue`: paced gap −100s | recover `[]`, `SWEEP PASS`, status unchanged (3 / 2 / chunk 3); i-0004 done 356 |
| 09:59:27 | after chunk 4 | `continue`: paced gap −14s | i-0005 done 419 |
| 09:59:45 | after chunk 5 | `continue` | `next` exit 3; 5 done / 0 failed; lease released; `ScheduleWakeup(stop)` cancelled 1 pending wakeup |

Five-hour usage stayed at 1.0% the whole run, so no chunk moved the reading
and pace never needed to sleep. `pace.jsonl` holds 5 matched pairs, none
interrupted, every end reading fresh; the policy sha matched.

Found and fixed (`beefd19`): 0.5.0's `pace.py` measured the paced gap from
`now` rather than from the last chunk's start, so every wakeup pushed the
target out again. On this exact fresh sweep, 0.5.0 answered
`sleep 3600 (seven_day pacing … of 29442s target)` and would have repeated
that on every wake without running a chunk.

Observed, not changed:
- Items are background agents, so one loop turn spans several invocations
  (one per result). The harness arms its ~20-minute fallback wakeup when an
  invocation ends without `ScheduleWakeup`; the END's `stop` cancelled it
  here. Every path ends in a reschedule or a stop, so this is harmless
  unless a turn dies mid-chunk.
- The re-orchestration serial rule fired on every chunk ("Three dispatches
  in a row of one agent each"), a false positive inside a sweep. It is
  recorded as lesson L-003; the hook itself still fires.

---

## Priority 3 — quality, once the above passes

- Run a real multi-lane task end to end and judge whether the conductor
  **actually parallelises**. Watch for it dispatching one agent at a time when
  the tasks are independent. That is the core failure this plugin exists to fix,
  so if it still happens, the output style needs strengthening — say so plainly.
- Check whether model tiering holds: is `scout` doing recon and `builder` doing
  implementation, or is everything landing on sonnet regardless?
- Check `/look` end to end against a running dev server.
- Watch context: does the conductor stay lean, or is it reading large files
  itself instead of delegating?

---

## How to report

Give me a table: check, pass/fail, and the actual output on failure. Then:

1. **What you fixed** — with the diff.
2. **What is broken and why** — mechanism, not symptom.
3. **What you could not determine** and what you would need.
4. **Your honest read** on whether the parallelisation doctrine is working, or
   whether the conductor is still serialising.

Fix anything mechanical yourself (wrong path, bad AppleScript line, missing
guard). Do **not** redesign the architecture — if something needs a design
change, describe it and stop.
