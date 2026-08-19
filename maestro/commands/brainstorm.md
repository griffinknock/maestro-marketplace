---
description: Think out loud with me before any code gets written. No code, no edits — research, then one question at a time, then options.
argument-hint: <the thing you are undecided about>
---

We are brainstorming: **$ARGUMENTS**

This is a dialogue, not a report. Work in this order, one message per step:

1. **Recon before anything.** Launch several haiku `scout`s in one message —
   web scouts searching for existing tools, libraries, and open-source
   implementations of this problem, and a repo scout for what is already
   installed or built (package.json, sibling components, utilities). Most
   coding problems are already solved; finding the solution is the win.
   Nothing gets presented until this lands. Brainstorming follows the same
   doctrine as everything else: tiered agents, parallel waves, your own
   context protected.

2. **Questions, one per message.** Ask the single question whose answer
   unblocks the most — numbered options, your pick marked, in the ✋ format
   from the output style, registered in `question.json` first. Use what recon
   found to make the options concrete ("@dnd-kit is maintained and handles
   keyboard a11y — adopt it?"). Then stop and wait for my answer. Repeat, one
   question per message, until purpose, constraints, and success criteria are
   pinned. Never bundle two questions, and never present approaches while a
   load-bearing question is open.

3. **Then 2–4 genuinely different approaches**, not one idea in three outfits.
   If two options collapse into the same thing under pressure, say so and cut
   one. For each: the one-line shape, the existing work it builds on, what it
   is good at, what it costs, what it makes hard later, and what would have to
   be true for it to be the right call. "Adopt <library>" appears as an option
   whenever recon found a credible one. Say which you would pick and why, in
   one sentence; then what would change your mind. Name the thing neither of
   us has thought about yet — the constraint, the existing code, the edge case
   that makes this harder than it looks.

4. **Close into a score.** The moment I pick an approach, brainstorming is
   over: run the `/orchestrate` flow for the chosen option — waves, agents,
   models, worktrees, dependency graph — and end with
   `Approve, or tell me what to change.` A brainstorm that ends without a
   score is unfinished; the deliverable is a plan ready to execute.

Rules throughout:

- **Write no code and edit no files.** Scouts gather facts; the one exception
  is a visual mock.
- If a question is visual, build a single self-contained HTML file at
  `.claude/maestro/mocks/<slug>.html` with the options side by side, using this
  project's real components and tokens where they exist, and give me the link.
  Do not describe a layout you could show me instead.
