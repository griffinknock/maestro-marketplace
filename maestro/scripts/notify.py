#!/usr/bin/env python3
"""Maestro notifier — makes a blocking question impossible to miss.

Fires on the Notification hook. Four channels, all best-effort:
  1. macOS Notification Center, with a distinct sound
  2. a terminal escape (OSC 9 / OSC 777) so the terminal itself flags the tab
  3. ntfy.sh push to your phone, if MAESTRO_NTFY_TOPIC is set
  4. a flag on the ledger so the board takes over the screen with the question

Env:
  MAESTRO_NTFY_TOPIC   ntfy.sh topic for phone push (pick something unguessable)
  MAESTRO_NTFY_SERVER  self-hosted ntfy base URL (default https://ntfy.sh)
  MAESTRO_SOUND        macOS sound name (default Submarine)
  MAESTRO_QUIET=1      suppress every channel except the board
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

QUIET = os.environ.get("MAESTRO_QUIET") == "1"
SOUND = os.environ.get("MAESTRO_SOUND", "Submarine")
TOPIC = os.environ.get("MAESTRO_NTFY_TOPIC", "").strip()
SERVER = os.environ.get("MAESTRO_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
PORT = os.environ.get("MAESTRO_PORT", "7717")

# Which notification kinds are worth interrupting a human for.
LOUD = {"permission_prompt", "agent_needs_input", "elicitation"}
SOFT = {"idle_prompt", "agent_completed"}


def state_dir(cwd):
    p = Path(cwd or ".").resolve()
    for parent in [p, *p.parents]:
        ptr = parent / ".claude" / "maestro" / "current"
        if ptr.is_file():
            try:
                d = Path(ptr.read_text().strip())
                if d.is_dir():
                    return d
            except OSError:
                return None
    return None


def question_for(d):
    """The conductor writes question.json before it asks. Prefer it if fresh."""
    f = (d / "question.json") if d else None
    try:
        if f and f.is_file() and time.time() - f.stat().st_mtime < 900:
            q = json.loads(f.read_text())
            if isinstance(q, dict) and q.get("question"):
                return q
    except (OSError, json.JSONDecodeError):
        pass
    return None


def mac_notify(title, body, loud):
    if sys.platform != "darwin":
        return
    # terminal-notifier gives a clickable notification; osascript is the fallback.
    if shutil.which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", title, "-message", body[:240],
               "-group", "maestro", "-open", f"http://127.0.0.1:{PORT}/"]
        if loud:
            cmd += ["-sound", SOUND]
    else:
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        script = (f'display notification "{esc(body[:240])}" with title "{esc(title)}"'
                  + (f' sound name "{SOUND}"' if loud else ""))
        cmd = ["osascript", "-e", script]
    try:
        subprocess.run(cmd, timeout=4, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def push(title, body, loud):
    if not TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{SERVER}/{TOPIC}", data=body[:900].encode(),
            headers={
                "Title": title[:120],
                "Priority": "high" if loud else "default",
                "Tags": "raised_hand" if loud else "white_check_mark",
                "Click": f"http://127.0.0.1:{PORT}/",
            })
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    kind = (payload.get("notification_type") or payload.get("matcher") or "").strip()
    if kind not in LOUD and kind not in SOFT:
        return
    loud = kind in LOUD

    d = state_dir(payload.get("cwd"))
    q = question_for(d)
    project = Path(payload.get("cwd") or ".").name

    if q:
        title = f"Maestro needs a call — {project}"
        opts = q.get("options") or []
        body = q["question"] + ("\n" + " · ".join(
            f"{i+1}) {o if isinstance(o, str) else o.get('label','')}"
            for i, o in enumerate(opts[:4])) if opts else "")
    else:
        title = ("Maestro needs you" if loud else "Maestro finished") + f" — {project}"
        body = (payload.get("message") or
                ("waiting on a decision" if loud else "the wave is done"))[:300]

    # Board takeover flag.
    if d:
        try:
            sf = d / "state.json"
            s = json.loads(sf.read_text())
            s["needs_input"] = loud
            s["alert"] = {"at": time.time(), "kind": kind, "loud": loud,
                          "title": title, "body": body, "question": q}
            tmp = sf.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, default=str))
            tmp.replace(sf)
        except (OSError, json.JSONDecodeError):
            pass

    if not QUIET:
        mac_notify(title, body, loud)
        push(title, body, loud)

    # OSC 9 makes the terminal itself raise a flag on the tab; BEL is the fallback
    # for terminals that ignore it. Emitted through the documented hook field so
    # Claude Code writes it to the real tty rather than into the transcript.
    out = {"suppressOutput": True}
    if loud:
        out["terminalSequence"] = f"\033]9;{title}\007\007"
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if os.environ.get("MAESTRO_DEBUG") == "1":
            print(f"maestro notify: {e}", file=sys.stderr)
    sys.exit(0)
