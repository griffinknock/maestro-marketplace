---
description: Print the current agent tree, waves, and worktrees as text plus a mermaid diagram.
allowed-tools: Bash, Read
disable-model-invocation: true
---

Read the current session ledger:

```bash
d=$(cat .claude/maestro/current 2>/dev/null) && cat "$d/state.json"
```

Render it for me as:

1. An indented ASCII tree — one line per agent:
   `<indent><status icon> <type> · <model> · <depth>/5 · <elapsed> · <worktree if any> — <what it is doing>`
2. A count line: how many are running, done, failed, and the deepest live level.
3. A ```mermaid flowchart LR grouping agents into `depth N` subgraphs, edges from
   parent to child, worktree branch in italics on any node that has one.
4. Any agent that has been running more than 5 minutes with no tool activity,
   flagged as possibly stuck.

If the ledger is missing or empty, say so in one line and tell me to check that
the maestro plugin's hooks are enabled with `/hooks`.
