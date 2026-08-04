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
| Scripts | `<source>/maestro/scripts/{ledger,subagent_tree,statusline,board,notify,reorchestrate}.py` |
| Installed as | `maestro@maestro-marketplace` (user scope) |
| Ledger written to | `<repo>/.claude/maestro/<8-char-session-id>/{state.json,events.jsonl}` |
| Board | `http://127.0.0.1:7717` |
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
print(board.spawn_warp('$HOME/Documents/Development/MintSplice/web-application',
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
board.spawn_ghostty('$HOME/Documents/Development/MintSplice/web-application','test prompt',title='probe')
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
cd ~/Documents/Development/maestro-marketplace && MAESTRO_DEBUG=1 python3 maestro/scripts/board.py ~/Documents/Development/MintSplice/web-application
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

On a healthy tree this prints **nothing** — that is a pass, not a failure.

To confirm the rules actually fire, hand-edit a copy of `state.json` so one agent
is `running` with `last_activity` 700 seconds old and everything else `done`,
then re-run. **Pass:** findings about serial drift, a stall, and idle lanes.

Then verify the LLM path does not pollute the ledger. Let it trigger with
`MAESTRO_REORCH_LLM=1`, wait ~30s, and check that **no new session directory**
appeared under `.claude/maestro/` and that `current` still points at your real
session. The guard is `MAESTRO_LEDGER_OFF=1` in the child env — confirm it holds.

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
