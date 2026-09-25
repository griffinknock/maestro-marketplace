---
description: Review pending lesson candidates, check the lesson budget, or publish a repo-scoped lesson.
argument-hint: [review|status|publish <id>]
---

Run the lessons flow: **$ARGUMENTS** (default `review`).

## `status`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" status
```

Report the numbers as-is. If it says NEAR BUDGET, go to the consolidation
step below instead of drafting new lessons.

## `publish <id>`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" publish <id>
```

Print exactly what it prints. On success it names the repo file it wrote and
says it is left uncommitted — tell me to commit it with my work. On refusal
(global lesson, or a lesson scoped to a different repo) just show the reason;
do not retry with a different id or scope.

## `review` (default)

1. **Gather.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" candidates --pending
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" rejected
   ```

   List pending candidates, active lessons (read the personal `lessons.md`
   and, if present, this repo's `.claude/maestro-lessons.md`), and rejected
   rules. If there is nothing pending, say so and stop.

2. **Group and draft — at most 3.** Group candidates that point at the same
   underlying mechanic. For each group, draft one lesson: a rule of **at
   most 2 lines**, strictly about Maestro's own orchestration mechanics
   (subagent assignment/tiers, briefs, how reports/messages pass back,
   dispatch and batching, questions, handoffs) — never a project fact. Give
   it:
   - **Why** — one line.
   - **Evidence** — session id prefix · finding fingerprint or tool_use_id ·
     a short quoted line from the candidate's `text`.

   Before drafting, check the rule against the active lessons and the
   rejected list from step 1. **Never re-propose** something that matches an
   active lesson (same mechanic, already covered) or a rejected rule (same
   key). Drop or fold that candidate instead of drafting it again.

3. **Present one draft at a time.** Register the question first, then ask,
   using the ✋ format from the output style:

   ```json
   {"question": "Accept this lesson?\nRule: <rule>\nWhy: <why>\nEvidence: <evidence>",
    "why": "standing rule for every future session",
    "options": [{"label": "Approve", "pick": true}, {"label": "Edit then approve"},
                {"label": "Reject"}, {"label": "Not a lesson"}],
    "blocking": true}
   ```

   - **Approve** →
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" accept \
       --rule "<rule>" --why "<why>" --evidence "<evidence>" \
       --scope global --candidates <comma-separated candidate ids>
     ```
     Use `--scope repo:<name>` instead of `global` when the rule only makes
     sense in this repo. On `LESSONS FAIL`, show the reasons and fix the
     draft — do not retry the same text.
   - **Edit then approve** → take the edit, re-show the draft, then Approve.
   - **Reject** →
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" reject --key "<fingerprint kind>" --rule "<drafted rule text>"
     ```
     then mark every candidate in the group reviewed:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" mark <id> --status reviewed
     ```
   - **Not a lesson** → mark every candidate in the group:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lessons.py" mark <id> --status not-a-lesson
     ```

   Delete `question.json` the moment each one is answered.

4. **Consolidate if NEAR BUDGET (or over).** Do not draft new lessons past
   this point without proposing a consolidation first. Present it as a
   **diff** — the exact new entries that would be appended, each carrying a
   `Supersedes:` line naming the older entry it replaces:

   ```
   + ## L-0NN · scope: global
   + Rule: <merged rule>
   + Why: <why>
   + Evidence: <evidence>
   + Supersedes: L-0AA
   + Accepted: <today>
   ```

   Apply only on an explicit yes, one `accept --supersedes L-0AA` per new
   entry. Never edit or delete an existing entry — the store is append-only
   by construction; a superseded entry stays on disk, just out of the active
   set.
