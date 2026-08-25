---
description: Plan a task as a conductor — waves, model tiers, worktrees, dependency graph — then ask before executing.
argument-hint: <what you want built>
---

Act as the Conductor for this task: **$ARGUMENTS**

Do **not** start any work yet. Produce the score first.

1. **Score block** — goal, what you know, what you are guessing at, and the shape
   (serial / parallel-N / fan-out-then-merge / explore-then-commit).

2. **Recon first if you are guessing.** If you need facts about this repo to plan
   honestly, send a `scout` now with the full question list — one scout, numbered
   questions — and wait. Split into several scouts (same message) only when the
   questions are independent and the answers gate different waves. Recon is the
   one thing you may do before I approve.

3. **The waves.** A table: wave, agent, model, effort, worktree (yes/no),
   task in one line, and what it depends on. Assign the cheapest agent that can
   actually do each job. Anything that does not consume another task's output
   goes in the same wave. Every row dispatches by its maestro agent type
   (`scout`, `builder`, `scribe`, `adversary`, `surgeon`, `section-lead`,
   `visual-reviewer`) — the type carries the model tier and the `RETURN:`
   contract, so the dispatch prompt states only context, task, and bounds.

4. **The graph.** A ```mermaid flowchart LR with a subgraph per wave, nodes
   labelled `agent · model`, worktree branch in italics where relevant.

5. **The decisions I need to make.** Every ambiguity, as numbered options with
   your pick marked. If a choice is visual, write an HTML mock to
   `.claude/maestro/mocks/` and give me the link rather than describing it.

6. **Cost shape.** Rough token/wall-clock expectation, the maximum concurrency
   you plan to run, and where the phase boundaries fall — the points where you
   will `/handoff` and Griffin clears the session.

Then stop and wait for me. Say exactly: `Approve, or tell me what to change.`
