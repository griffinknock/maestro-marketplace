#!/usr/bin/env bash
# Maestro installer — wires the plugin, status line, and settings into Claude Code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$HERE/maestro"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"

say() { printf '\033[38;5;114m▸\033[0m %s\n' "$1"; }
warn() { printf '\033[38;5;214m▲\033[0m %s\n' "$1"; }

command -v python3 >/dev/null || { warn "python3 not found — install it first"; exit 1; }
command -v claude  >/dev/null || { warn "claude not found on PATH"; exit 1; }

say "checking scripts"
for f in ledger.py subagent_tree.py statusline.py board.py; do
  python3 -m py_compile "$PLUGIN/scripts/$f"
  chmod +x "$PLUGIN/scripts/$f"
done

say "adding marketplace + installing plugin"
# `marketplace add` insists on ./relative or a URL, so run it from the parent.
( cd "$(dirname "$HERE")" && \
  claude plugin marketplace add "./$(basename "$HERE")" >/dev/null 2>&1 || \
  claude plugin marketplace update maestro-marketplace >/dev/null 2>&1 )
claude plugin install maestro@maestro-marketplace || \
  warn "install returned non-zero — run '/plugin' inside Claude Code to finish"

say "wiring the status line into $SETTINGS"
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.maestro-backup.$(date +%s)"

PLUGIN_DIR="$PLUGIN" python3 - "$SETTINGS" <<'PY'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p))
plugin = os.environ["PLUGIN_DIR"]

d["statusLine"] = {
    "type": "command",
    "command": f'python3 "{plugin}/scripts/statusline.py"',
    "refreshInterval": 3,
    "padding": 0,
}
d.setdefault("env", {})
d["env"].setdefault("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")
d.setdefault("teammateMode", "in-process")
d.setdefault("outputStyle", "Maestro")
d.setdefault("alwaysThinkingEnabled", True)
# Clickable badges in the footer for PR/issue refs the agents mention.
d.setdefault("footerLinksRegexes", ["#(\\d+)"])

perms = d.setdefault("permissions", {})
allow = perms.setdefault("allow", [])
for rule in ["Bash(git worktree list)", "Bash(git status:*)", "Bash(git diff:*)",
             "Bash(npx playwright screenshot:*)"]:
    if rule not in allow:
        allow.append(rule)

json.dump(d, open(p, "w"), indent=2)
print("  statusLine, outputStyle, agent teams, worktree perms")
PY

say "done"
cat <<EOF

  Start a session:      claude
  Open the board:       /board
  Plan something:       /orchestrate <task>
  See the tree:         /tree
  Visual pass:          /look http://localhost:3000

  Phone push for blocking questions — pick an unguessable topic, then
  subscribe to it in the ntfy app on your phone:

    echo 'export MAESTRO_NTFY_TOPIC=maestro-<something-random>' >> ~/.zshrc

  Backup of your old settings:  $SETTINGS.maestro-backup.*

EOF
