---
description: Screenshot the running UI and review it against a mock, Figma frame, or stored baseline.
argument-hint: <url or route> [--ref mock|figma|baseline]
---

Run a visual pass on: **$ARGUMENTS**

1. Work out the URL. If I gave you a route rather than a URL, find the dev server
   port from the repo's scripts/config rather than guessing. If nothing is
   running, tell me the exact command to start it and stop.

2. Launch `visual-reviewer` agents **in parallel** — one per viewport (1440×900
   and 390×844), plus one per additional route if I named more than one. Same
   message, not one after another.

3. Each one screenshots to `.claude/maestro/shots/`, compares against the
   reference, and returns a verdict with findings rated
   `blocking` / `should-fix` / `nit`.

4. Report back as:
   - verdict per viewport, one line each
   - findings table: severity · what · where
   - the screenshot paths as clickable `file://` links
   - anything that regressed against the reference

5. If any finding is `blocking`, propose the fix as a wave — do not apply it
   without telling me what you are about to change.

The board shows the shots at http://127.0.0.1:7717/ — mention it only if the
board is already running.
