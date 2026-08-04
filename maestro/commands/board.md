---
description: Open the Maestro board. Pass --lan to also serve it to your phone or iPad.
argument-hint: [--lan]
allowed-tools: Bash
disable-model-invocation: true
---

Start the board if it is not already running, then give me the link.

If `$ARGUMENTS` contains `--lan`, restart it bound to the local network so other
devices can reach it:

```bash
pkill -f "scripts/board.py" 2>/dev/null; sleep 1
nohup python3 "${CLAUDE_PLUGIN_ROOT}/scripts/board.py" "$(pwd)" --no-open --lan >/tmp/maestro-board.log 2>&1 &
sleep 2; cat /tmp/maestro-board.log
```

Otherwise start it local-only if it is not already up:

```bash
(curl -sf http://127.0.0.1:${MAESTRO_PORT:-7717}/api/state >/dev/null 2>&1 && echo "already running") || \
  (nohup python3 "${CLAUDE_PLUGIN_ROOT}/scripts/board.py" "$(pwd)" --no-open >/tmp/maestro-board.log 2>&1 & sleep 1; echo started)
```

Then reply with the link, and — only when `--lan` was used — the other addresses
the log printed, each on its own line, labelled `tailnet` or `same Wi-Fi`.
Mention that the QR code for the first address is on the board itself under
**Connect a device**.

If the log shows anything unexpected, show me its last 5 lines instead.
