---
description: Close out this conductor session at a phase boundary — write a validated HANDOFF.md, then hand Griffin the /clear.
argument-hint: [optional note on where the next session should pick up]
---

End this conductor session cleanly: **$ARGUMENTS**

A conductor's transcript is re-read on every remaining turn, so its cost grows
with the square of its length. The cheap move at a phase boundary is a fresh
session seeded with a handoff — but only if the handoff is correct, concise,
and points at the right places. That is what you produce now.

1. **Refuse mid-wave.** If any subagent is still in flight, stop and say which.
   The ledger and re-check state are keyed to this session id; clearing now
   orphans them and strands the wave. Offer to wait or to stop the agents.

2. **Settle the record.** Update `.claude/maestro/PLAN.md` — decisions since
   the last update, wave status, anything you would hate to lose.

3. **Write `.claude/maestro/HANDOFF.md`.** Pointers and decisions, never
   payloads — the next session reads files itself. Exactly these sections:

   ```markdown
   # Handoff — <one-line goal>
   ## Goal
   <one line: what done looks like>
   ## Decisions
   - <decision> — <one-line rationale>
   ## World state
   - branch: <name>
   - worktree: <path>            (one line per live worktree, omit if none)
   - merged: <what landed where>
   - pr: <url>                   (omit if none)
   - key files: `path/to/file.ts:42` — <why it matters, half a line>
   - reports: `.claude/maestro/<sid>/reports/` (only if a next step needs one)
   ## Open questions
   - <anything unresolved, with your recommendation>
   ## Next
   1. <first action the fresh session takes>
   2. <second>
   3. <third>
   ```

   Hard rules: ≤120 lines; no fenced block over 20 lines; every path, branch,
   and worktree you name must exist right now. No transcript quotes, no code.

4. **Validate. Do not skip this.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff_check.py"
   ```

   Fix every reason it prints and re-run until it says `HANDOFF PASS`. A
   pointer that does not resolve is a bug in the handoff, not a nit.

5. **Hand over.** Print exactly this, then stop:

   Handoff validated. Clear, then seed the next session:

   ```
   /clear
   ```

   ```
   Read .claude/maestro/HANDOFF.md and continue from it.
   ```

Phase boundaries where this is the right move: spec approved, plan approved, a
wave-set merged with checks green, review done, PR opened. Between those, keep
the session — a handoff mid-thought loses more than it saves.
