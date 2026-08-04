#!/usr/bin/env python3
"""Maestro board — a tiny local server for the agent tree dashboard.

  python3 board.py [--port 7717] [--no-open] [--lan] [project_dir]

Serves the dashboard, the current session's state.json, and any screenshots the
visual-reviewer produced. Binds to 127.0.0.1 by default; `--lan` also listens on
the local network so a phone or iPad can reach it. Read-only apart from the
music transport.
"""
import argparse
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── music ───────────────────────────────────────────────────────────────
# AppleScript against whichever player is actually running. Silently absent on
# anything but macOS, and never launches an app that is not already open.
PLAYERS = {"Spotify": "Spotify", "Music": "Music"}
ACTIONS = {"next": "next track", "prev": "previous track", "playpause": "playpause"}


def osa(script, timeout=4):
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def running_player():
    for app in PLAYERS:
        if osa(f'tell application "System Events" to (name of processes) '
               f'contains "{app}"') == "true":
            return app
    return None


def now_playing():
    app = running_player()
    if not app:
        return {"available": False}
    state = osa(f'tell application "{app}" to player state as string') or ""
    if "not" in state.lower() or not state:
        return {"available": True, "app": app, "playing": False}
    track = osa(f'tell application "{app}" to name of current track') or ""
    artist = osa(f'tell application "{app}" to artist of current track') or ""
    return {"available": True, "app": app, "playing": state.lower() == "playing",
            "track": track, "artist": artist}


def music_do(action):
    app = running_player()
    if not app or action not in ACTIONS:
        return {"ok": False}
    osa(f'tell application "{app}" to {ACTIONS[action]}')
    time.sleep(0.35)          # let the player settle before we read it back
    return {"ok": True, **now_playing()}

HERE = Path(__file__).resolve().parent
INDEX = HERE.parent / "dashboard" / "index.html"


def lan_ip():
    """The address this machine actually answers on, without touching the net."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))       # TEST-NET-1: routed nowhere, never sends
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tailnet_name():
    """MagicDNS name, if Tailscale is up. Reachable from anywhere, not just Wi-Fi."""
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        try:
            r = subprocess.run([exe, "status", "--json"], capture_output=True,
                               text=True, timeout=3)
            if r.returncode == 0:
                d = json.loads(r.stdout)
                dns = (d.get("Self") or {}).get("DNSName", "").rstrip(".")
                if dns:
                    return dns
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            continue
    return None


def icon_png(size=180):
    """The mascot as a home-screen icon. Pure stdlib so there is nothing to install."""
    import struct
    import zlib
    bg, ink, body, baton = (18, 21, 27), (233, 236, 241), (162, 171, 189), (249, 156, 0)
    c, rows = size / 180.0, b""
    for y in range(size):
        row = b"\x00"
        for x in range(size):
            px, py = x / c, y / c
            p = bg
            # torso: a rounded trapezoid
            if 96 <= py <= 150 and abs(px - 90) <= 26 + (py - 96) * 0.42:
                p = body
            # arm + baton
            if abs((py - 96) + (px - 104) * 0.9) < 6 and 104 <= px <= 146:
                p = body
            if (px - 150) ** 2 + (py - 56) ** 2 <= 130:
                p = baton
            # head
            if (px - 90) ** 2 + (py - 66) ** 2 <= 34 ** 2:
                p = ink
                for ex in (77, 103):
                    if (px - ex) ** 2 + (py - 62) ** 2 <= 36:
                        p = bg
            row += bytes(p)
        rows += row

    def chunk(t, d):
        c_ = t + d
        return struct.pack(">I", len(d)) + c_ + struct.pack(">I", zlib.crc32(c_))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


ROOTS_ENV = "MAESTRO_ROOTS"
DEFAULT_ROOTS = ["~/Documents/Development", "~/Development", "~/code", "~/src"]


def search_roots(extra=None):
    """Directories to scan for repos. One board, every project."""
    raw = os.environ.get(ROOTS_ENV, "")
    paths = [p for p in raw.split(os.pathsep) if p.strip()] or DEFAULT_ROOTS
    out = []
    for p in ([str(extra)] if extra else []) + paths:
        d = Path(p).expanduser()
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def repos(extra=None):
    """Every project with a Maestro ledger, plus every git repo we could start one in."""
    found, seen = [], set()
    for root in search_roots(extra):
        candidates = [root] + sorted(c for c in root.iterdir() if c.is_dir()) \
            if root.is_dir() else []
        for c in candidates:
            try:
                if c in seen or c.name.startswith("."):
                    continue
                if (c / ".git").exists() or (c / ".claude").is_dir():
                    seen.add(c)
                    found.append(c)
            except OSError:
                continue
    return found


def sessions_in(repo):
    base = repo / ".claude" / "maestro"
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if d.is_dir() and (d / "state.json").is_file():
            out.append(d)
    return sorted(out, key=lambda d: (d / "state.json").stat().st_mtime, reverse=True)


def meta_of(d):
    try:
        m = json.loads((d / "meta.json").read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def summarise(repo, d):
    """One rail item: what it is, who named it, and how it is doing."""
    try:
        s = json.loads((d / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    nodes = [n for n in (s.get("nodes") or {}).values() if n.get("id") != "root"]
    live = [n for n in nodes if n.get("status") in ("running", "spawning")]
    m = meta_of(d)
    q = (d / "question.json").is_file()
    return {
        "id": str(d), "repo": str(repo), "repoName": repo.name,
        "session": s.get("session_id"),
        "name": m.get("name") or repo.name,
        "color": m.get("color") or "blue",
        "createdAt": m.get("createdAt"),
        "agents": len(nodes), "live": len(live),
        "done": sum(1 for n in nodes if n.get("status") == "done"),
        "failed": sum(1 for n in nodes if n.get("status") == "failed"),
        "depth": max([n.get("depth", 0) for n in live], default=0),
        "lanes": len({n.get("lane") or n.get("id") for n in nodes}),
        "needsInput": bool(s.get("needs_input")) or q,
        "updated": s.get("updated") or (d / "state.json").stat().st_mtime,
        "conductor": (s.get("nodes") or {}).get("root", {}).get("status"),
    }


def all_orchestrations(extra=None):
    out = []
    for repo in repos(extra):
        for d in sessions_in(repo):
            row = summarise(repo, d)
            if row:
                out.append(row)
    out.sort(key=lambda r: (not r["needsInput"], -r["live"], -(r["updated"] or 0)))
    return out


def state_by_id(sid):
    d = Path(sid)
    if not (d / "state.json").is_file():
        return {"empty": True}
    try:
        s = json.loads((d / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"empty": True}
    repo = d.parent.parent.parent          # <repo>/.claude/maestro/<id>
    s["root"] = str(repo)
    s["stateDir"] = str(d)
    s["meta"] = meta_of(d)
    return s


# ── spawning ───────────────────────────────────────────────────────────
def osa_str(s):
    """Quote a Python string as an AppleScript literal.

    AppleScript string literals cannot span lines, so newlines and tabs have to
    become escapes rather than raw characters.
    """
    out = (str(s).replace("\\", "\\\\").replace('"', '\\"')
           .replace("\r", "").replace("\n", "\\n").replace("\t", "\\t"))
    return '"' + out + '"'


# Terminal colour names differ; Warp takes the 8 ANSI names.
WARP_COLOR = {"blue": "blue", "emerald": "green", "amber": "yellow",
              "violet": "magenta", "red": "red", "zinc": "white"}


def slugify(name):
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name).lower())
    return ("maestro-" + out.strip("-"))[:60] or "maestro-session"


def warp_installed():
    return Path("/Applications/Warp.app").is_dir() or (Path.home() / ".warp").is_dir()


def claude_bin():
    """Absolute path to the `claude` CLI.

    Ghostty runs `command` as `login -flp <user> /bin/bash --noprofile --norc`,
    which inherits Ghostty.app's own GUI environment — on macOS that is the bare
    launchd PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). With no profile and no rc
    file, nothing puts `~/.local/bin` back, so a bare `claude` is not found and
    the surface dies with "Ghostty failed to launch the requested command".
    Resolving it here, in a process that does have the user's PATH, is the fix.
    """
    found = shutil.which("claude")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "claude",
              Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude")):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return "claude"


def spawn_warp(repo, prompt, title=None, color="blue", new_window=False):
    """Open a Warp tab via a Tab Config + the warp:// URI scheme.

    More robust than driving a terminal with AppleScript: Warp reads a TOML file
    we own, so there is no scripting dictionary to guess at. Parameters cannot
    be passed through the URI, so the prompt is baked into the command.
    """
    if sys.platform != "darwin":
        return {"ok": False, "error": "Warp spawning is macOS only"}
    slug = slugify(title or Path(repo).name)
    d = Path.home() / ".warp" / "tab_configs"
    cmd = "claude"
    if prompt:
        one = " ".join(str(prompt).split())
        cmd = "claude " + shlex.quote(one)
    toml = [
        f'name = {json.dumps(str(title or Path(repo).name))}',
        f'color = {json.dumps(WARP_COLOR.get(color, "blue"))}',
        f'title = {json.dumps(str(title or Path(repo).name))}',
        "",
        "[[panes]]",
        'id = "main"',
        'type = "terminal"',
        f'directory = {json.dumps(str(repo))}',
        f'commands = [{json.dumps(cmd)}]',
        "is_focused = true",
        "",
    ]
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.toml").write_text("\n".join(toml))
    except OSError as e:
        return {"ok": False, "error": f"could not write tab config: {e}"}
    uri = f"warp://tab_config/{slug}" + ("?new_window=true" if new_window else "")
    try:
        r = subprocess.run(["open", uri], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "open failed").strip()[:300],
                    "uri": uri}
        return {"ok": True, "uri": uri, "config": str(d / f"{slug}.toml")}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)[:300], "uri": uri}


def pick_terminal():
    """warp | ghostty. Explicit env wins; otherwise prefer whatever is installed."""
    want = (os.environ.get("MAESTRO_TERMINAL") or "auto").lower()
    if want in ("warp", "ghostty"):
        return want
    if warp_installed():
        return "warp"
    return "ghostty"


def spawn(repo, prompt, title=None, color="blue", new_window=False):
    which = pick_terminal()
    if which == "warp":
        res = spawn_warp(repo, prompt, title, color, new_window)
        if res.get("ok"):
            return {**res, "terminal": "warp"}
        # Warp is the default but should never be a dead end.
        alt = spawn_ghostty(repo, prompt, title, new_window)
        return {**alt, "terminal": "ghostty",
                "note": "Warp spawn failed, fell back to Ghostty: " + str(res.get("error"))}
    res = spawn_ghostty(repo, prompt, title, new_window)
    return {**res, "terminal": "ghostty"}


def spawn_ghostty(repo, prompt, title=None, new_window=False):
    """Open a Ghostty tab in `repo` running `claude`, with `prompt` pre-typed.

    Ghostty 1.3+ AppleScript. `initial input` types the prompt but does NOT send
    it — you read it and hit enter. That is deliberate: a prompt you never saw
    is exactly the kind of thing this whole setup exists to prevent.
    """
    if sys.platform != "darwin":
        return {"ok": False, "error": "AppleScript spawning is macOS only"}
    body = [
        'tell application "Ghostty"',
        "  activate",
        "  set cfg to new surface configuration",
        f"  set initial working directory of cfg to {osa_str(repo)}",
        f"  set command of cfg to {osa_str(claude_bin())}",
    ]
    if prompt:
        # One line, deliberately. `initial input` types into the prompt without
        # sending, but an embedded newline WOULD send it — and a prompt that
        # submits before you have read it defeats the point of the review step.
        body.append(f"  set initial input of cfg to {osa_str(' '.join(prompt.split()))}")
    if new_window:
        body.append("  set t to new window with configuration cfg")
    else:
        body += [
            "  set t to missing value",
            "  try",
            "    set w to first window",
            "    set t to new tab in w with configuration cfg",
            "  on error",
            "    set t to new window with configuration cfg",
            "  end try",
        ]
    if title:
        # `perform action` only accepts a *terminal* target. Handing it the
        # window or tab that `new window` / `new tab` returns fails with
        # -1715 "Missing terminal target" — which the outer `try` then swallowed,
        # leaving every tab untitled. The `set_tab_title:<arg>` syntax itself is
        # fine; `name` on window/tab/terminal is read-only (-10006), so this is
        # the only route.
        body += ["  try",
                 "    try",
                 "      set surf to focused terminal of (selected tab of t)",
                 "    on error",
                 "      set surf to focused terminal of t",
                 "    end try",
                 f'    perform action {osa_str("set_tab_title:" + str(title))} on surf',
                 "  end try"]
    body.append("end tell")
    script = "\n".join(body)
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=20)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "osascript failed").strip()[:400],
                    "script": script}
        return {"ok": True}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)[:300], "script": script}


def find_root(start):
    p = Path(start).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".claude" / "maestro").is_dir():
            return parent
        if (parent / ".git").exists():
            return parent
    return p


def state_dir(root):
    ptr = root / ".claude" / "maestro" / "current"
    if ptr.is_file():
        d = Path(ptr.read_text().strip())
        if d.is_dir():
            return d
    base = root / ".claude" / "maestro"
    dirs = [d for d in base.glob("*") if d.is_dir() and (d / "state.json").exists()] if base.is_dir() else []
    return max(dirs, key=lambda d: (d / "state.json").stat().st_mtime) if dirs else None


def read_state(root):
    d = state_dir(root)
    if not d:
        return {"empty": True, "root": str(root)}
    try:
        s = json.loads((d / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"empty": True, "root": str(root)}
    s["root"] = str(root)
    s["stateDir"] = str(d)
    return s


class Handler(BaseHTTPRequestHandler):
    root = Path(".")

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)),
                   "Cache-Control": "no-store", **(extra or {})}
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            try:
                return self._send(200, INDEX.read_text(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, "dashboard/index.html missing", "text/plain")

        if u.path == "/api/state":
            sid = q.get("id", [""])[0]
            data = state_by_id(sid) if sid else read_state(self.root)
            return self._send(200, json.dumps(data, default=str))

        if u.path == "/api/orchestrations":
            return self._send(200, json.dumps(
                {"orchestrations": all_orchestrations(self.root),
                 "repos": [{"path": str(r), "name": r.name} for r in repos(self.root)]},
                default=str))

        if u.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sid = q.get("id", [""])[0]
            last = None
            try:
                while True:
                    d = Path(sid) if sid else state_dir(self.root)
                    try:
                        m = (d / "state.json").stat().st_mtime if d else 0
                    except OSError:
                        m = 0
                    if m != last:
                        last = m
                        payload = json.dumps(
                            state_by_id(sid) if sid else read_state(self.root), default=str)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        if u.path == "/api/shot":
            p = Path(q.get("p", [""])[0]).resolve()
            ok = any((r / ".claude" / "maestro").resolve() in p.parents
                     for r in repos(self.root) + [self.root])
            if not ok or not p.is_file():
                return self._send(404, "no", "text/plain")
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            return self._send(200, p.read_bytes(), ctype)

        if u.path == "/manifest.webmanifest":
            return self._send(200, json.dumps({
                "name": "Maestro", "short_name": "Maestro",
                "start_url": "/", "display": "standalone",
                "background_color": "#12151b", "theme_color": "#12151b",
                "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"},
                          {"src": "/icon.png?size=512", "sizes": "512x512",
                           "type": "image/png", "purpose": "any maskable"}],
            }), "application/manifest+json")

        if u.path == "/icon.png":
            try:
                size = max(64, min(512, int(q.get("size", ["180"])[0])))
            except ValueError:
                size = 180
            return self._send(200, icon_png(size), "image/png",
                              {"Cache-Control": "public, max-age=86400"})

        if u.path == "/api/net":
            host = self.headers.get("Host", "").split(":")[0]
            port = self.server.server_address[1]
            urls = []
            tn = tailnet_name()
            if tn:
                urls.append({"kind": "tailnet", "url": f"http://{tn}:{port}/",
                             "note": "works anywhere, private to your tailnet"})
            ip = lan_ip()
            if ip:
                urls.append({"kind": "lan", "url": f"http://{ip}:{port}/",
                             "note": "same Wi-Fi only"})
            return self._send(200, json.dumps({
                "urls": urls, "port": port, "host": host,
                "bound": self.server.server_address[0],
                "localOnly": self.server.server_address[0] in ("127.0.0.1", "localhost"),
            }))

        if u.path == "/api/music":
            a = q.get("a", [""])[0]
            return self._send(200, json.dumps(music_do(a) if a else now_playing()))

        if u.path == "/api/question":
            sid = q.get("id", [""])[0]
            d = Path(sid) if sid else state_dir(self.root)
            f = (d / "question.json") if d else None
            if not f or not f.is_file():
                return self._send(200, "null")
            try:
                return self._send(200, f.read_text())
            except OSError:
                return self._send(200, "null")

        if u.path == "/api/dismiss":
            sid = q.get("id", [""])[0]
            d = Path(sid) if sid else state_dir(self.root)
            for name in ("question.json",):
                try:
                    (d / name).unlink()
                except (OSError, TypeError):
                    pass
            try:
                sf = d / "state.json"
                s = json.loads(sf.read_text())
                s["needs_input"] = False
                s.pop("alert", None)
                sf.write_text(json.dumps(s, default=str))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            return self._send(200, '{"ok":true}')

        if u.path == "/api/events":
            d = state_dir(self.root)
            f = (d / "events.jsonl") if d else None
            if not f or not f.is_file():
                return self._send(200, "[]")
            lines = f.read_text(errors="replace").splitlines()[-400:]
            rows = []
            for l in lines:
                try:
                    rows.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
            return self._send(200, json.dumps(rows, default=str))

        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        # Everything below changes state on this machine. When the board is
        # served over --lan, only loopback may do it — a read-only dashboard on
        # the Wi-Fi is fine, a remote "run this command" endpoint is not.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._send(403, json.dumps(
                {"ok": False, "error": "control actions are loopback-only"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, json.dumps({"ok": False, "error": "bad json"}))

        if u.path == "/api/spawn":
            repo = Path(str(body.get("repo") or "")).expanduser()
            if not repo.is_dir():
                return self._send(400, json.dumps(
                    {"ok": False, "error": f"no such directory: {repo}"}))
            known = {str(r) for r in repos(self.root)} | {str(self.root)}
            if str(repo.resolve()) not in {str(Path(k).resolve()) for k in known}:
                return self._send(403, json.dumps(
                    {"ok": False, "error": "repo is outside the watched roots"}))
            name = (body.get("name") or repo.name)[:60]
            prompt = body.get("prompt") or ""
            res = spawn(repo, prompt, title=name,
                        color=body.get("color") or "blue",
                        new_window=bool(body.get("newWindow")))
            # Stamp the name/colour onto whichever session appears next, so the
            # rail item is already yours before the first tool call lands.
            if res.get("ok"):
                pend = repo / ".claude" / "maestro"
                try:
                    pend.mkdir(parents=True, exist_ok=True)
                    (pend / "pending-meta.json").write_text(json.dumps({
                        "name": name, "color": body.get("color") or "blue",
                        "createdAt": time.time()}))
                except OSError:
                    pass
            return self._send(200 if res.get("ok") else 500, json.dumps(res))

        if u.path == "/api/meta":
            d = Path(str(body.get("id") or ""))
            if not (d / "state.json").is_file():
                return self._send(404, json.dumps({"ok": False, "error": "unknown id"}))
            m = meta_of(d)
            for k in ("name", "color"):
                if body.get(k):
                    m[k] = str(body[k])[:60]
            try:
                (d / "meta.json").write_text(json.dumps(m))
            except OSError as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "meta": m}))

        return self._send(404, json.dumps({"ok": False, "error": "not found"}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?", default=os.getcwd())
    ap.add_argument("--port", type=int, default=int(os.environ.get("MAESTRO_PORT", 7717)))
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--lan", action="store_true",
                    help="also listen on the local network so a phone or iPad can reach it")
    ap.add_argument("--host", default=None, help="explicit bind address (implies --lan)")
    a = ap.parse_args()

    bind = a.host or ("0.0.0.0" if a.lan or os.environ.get("MAESTRO_LAN") == "1"
                      else "127.0.0.1")
    Handler.root = find_root(a.project)
    try:
        srv = ThreadingHTTPServer((bind, a.port), Handler)
    except OSError:
        print(f"maestro board: port {a.port} is already in use — "
              f"it is probably already running at http://127.0.0.1:{a.port}/")
        return

    url = f"http://127.0.0.1:{a.port}/"
    print(f"maestro board  →  {url}\n  watching {Handler.root}")

    if bind != "127.0.0.1":
        others = []
        tn = tailnet_name()
        if tn:
            others.append((f"http://{tn}:{a.port}/", "tailnet — works anywhere"))
        ip = lan_ip()
        if ip:
            others.append((f"http://{ip}:{a.port}/", "same Wi-Fi"))
        for u, note in others:
            print(f"  {u}   ({note})")
        if others and shutil.which("qrencode"):
            print()
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", others[0][0]])
        elif others:
            print("  (brew install qrencode to get a scannable code here)")
        print("\n  Read-only, except the music transport. Anyone on this network can\n"
              "  see the board while it is bound this way.")

    if not a.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
