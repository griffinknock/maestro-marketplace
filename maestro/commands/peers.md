---
description: List the other live Maestro conductors on this machine — their message address, repo, branch, lanes in flight, and files being written — so work blocks can be coordinated.
allowed-tools: Bash, ListAgents, SendMessage
---

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/peers.py"
```

Show the result as-is, same-repo conductors first. A peer listed with
"address unknown" has not run `ListAgents` yet; `ListAgents` here shows every
live session's address if you need to reach it anyway.

Then, only for peers whose lanes or files overlap what this session is doing
or about to do, say in one line each what overlaps and what you propose
(who goes first, what waits for what). Do not message anyone from this
command unless the user asked you to; when messaging is warranted, follow
"Other conductors" in the output style.
