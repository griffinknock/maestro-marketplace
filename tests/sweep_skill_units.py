#!/usr/bin/env python3
"""Checks for maestro/skills/sweep/SKILL.md: valid frontmatter, that every
sweep_state.py / pace.py subcommand or flag it references actually exists
(grepped from their argparse definitions), that the resume/wakeup prompt
string is consistent everywhere it appears, that it matches the anchor
text sweep_state.py's SessionStart hook prints, that the owner comes from
the session id (never a minted token), and that every exit code
sweep_state.py returns is documented.

    python3 tests/sweep_skill_units.py

Exit code is 0 when every check passes.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "maestro" / "skills" / "sweep" / "SKILL.md"
STATE_PY = REPO / "maestro" / "scripts" / "sweep_state.py"
PACE_PY = REPO / "maestro" / "scripts" / "pace.py"

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def read(p):
    return p.read_text()


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


# ── (1) SKILL.md exists with valid frontmatter ───────────────────────────

def frontmatter_case():
    print("\n=== SKILL.md — frontmatter ===")
    check("SKILL.md exists", SKILL.is_file())
    if not SKILL.is_file():
        return None
    text = read(SKILL)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    check("frontmatter block present (--- ... ---)", m is not None)
    if not m:
        return text
    fm = m.group(1)
    fields = {}
    for line in fm.splitlines():
        mm = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if mm:
            fields[mm.group(1)] = mm.group(2).strip()
    check("frontmatter has 'name'", "name" in fields, fm)
    check("frontmatter has 'description'", "description" in fields, fm)
    check("frontmatter has 'argument-hint'", "argument-hint" in fields, fm)
    check("name is 'sweep'", fields.get("name") == "sweep", fields.get("name"))
    check("argument-hint lists takeover", "takeover <slug>" in fields.get("argument-hint", ""),
          fields.get("argument-hint"))
    return text


# ── (2) every sweep_state.py / pace.py subcommand or flag referenced ─────

def script_surface_case(text):
    print("\n=== SKILL.md — script surface matches argparse ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return

    state_src = read(STATE_PY)
    pace_src = read(PACE_PY)

    subcommands = set(re.findall(r'add_parser\("([a-z-]+)"\)', state_src))
    check("found sweep_state.py subcommands via argparse", len(subcommands) >= 10, subcommands)

    # every `sweep_state.py <word>` / `"$STATE" <word>` invocation in the
    # skill must be a real subcommand.
    referenced = set(re.findall(r'\$STATE"?\s+([a-z-]+)', text))
    referenced |= set(re.findall(r'sweep_state\.py"?\s+([a-z-]+)', text))
    unknown = sorted(referenced - subcommands)
    check("every referenced sweep_state.py subcommand exists in argparse",
          not unknown, unknown)
    for sub in ("new", "recover", "check", "next", "done", "fail", "finding", "add",
                "amend-plan", "end-chunk", "status", "list", "set-policy"):
        check(f"skill references '{sub}'", sub in referenced)

    # every --flag the skill mentions must exist in one of the two argparse
    # surfaces — no carve-outs any more: --owner/--stale-after are real.
    flags_in_skill = set(re.findall(r"--([a-z][a-z-]*)", text))
    known_flags = set(re.findall(r'add_argument\("--([a-z-]+)"', state_src))
    known_flags |= set(re.findall(r'add_argument\("--([a-z-]+)"', pace_src))
    unknown_flags = sorted(flags_in_skill - known_flags)
    check("every --flag referenced by the skill exists in argparse", not unknown_flags, unknown_flags)
    check("skill documents recover --takeover", "--takeover" in text and "takeover" in known_flags)
    check("skill explains --stale-after", "--stale-after" in text)

    check("skill references pace.py's --sweep flag", "--sweep" in text)
    check("skill mentions all four pace.py actions",
          all(a in text for a in ("continue", "sleep", "stop", "probe")))


# ── (3) resume/wakeup prompt consistency ─────────────────────────────────

def resume_prompt_case(text):
    print("\n=== SKILL.md — resume/wakeup prompt is one consistent string ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return

    run_count = text.count("/loop /maestro:sweep run <slug>")
    resume_count = text.count("/loop /maestro:sweep resume <slug>")
    check("skill uses '/loop /maestro:sweep run <slug>' to start a sweep", run_count >= 1)
    check("skill uses '/loop /maestro:sweep resume <slug>' to resume one",
          resume_count >= 2, f"found {resume_count}")

    # Every /loop line Griffin is told to type is exactly the slug form —
    # no owner token riding along (the owner is the session id now).
    loop_lines = re.findall(r"^/loop .*$", text, re.M)
    check("every fenced /loop line is exactly '/loop /maestro:sweep run|resume <slug>'",
          loop_lines and all(l.strip() in ("/loop /maestro:sweep run <slug>",
                                           "/loop /maestro:sweep resume <slug>")
                             for l in loop_lines), loop_lines)
    check("no '--owner <owner>' anywhere in the skill", "--owner <owner>" not in text)

    bare = re.findall(r"(?<!/loop )(?<!/loop\n)/maestro:sweep resume <slug>", text)
    check("every '/maestro:sweep resume' in the skill is wrapped in '/loop '", not bare, bare)
    bare_run = re.findall(r"(?<!/loop )(?<!/loop\n)/maestro:sweep run <slug>", text)
    check("every '/maestro:sweep run' in the skill is wrapped in '/loop '", not bare_run, bare_run)

    check("skill names ScheduleWakeup", "ScheduleWakeup" in text)
    check("skill states ScheduleWakeup is /loop-only",
          re.search(r"only exists? inside.*`?/loop`?|only.*available.*`?/loop`?", text, re.I | re.DOTALL) is not None)


# ── (4) the owner is the session id, not a minted token ──────────────────

def owner_case(text):
    print("\n=== SKILL.md / sweep_state.py — owner is Claude Code's session id ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    state_src = read(STATE_PY)
    check("sweep_state.py reads CLAUDE_CODE_SESSION_ID", '"CLAUDE_CODE_SESSION_ID"' in state_src)
    check("SKILL.md explains CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ID" in text)
    check("SKILL.md cites the env-vars reference",
          "https://code.claude.com/docs/en/env-vars" in text)
    check("no random owner-token minting left in SKILL.md",
          "token_hex" not in text and "OWNER=" not in text and '"$OWNER"' not in text)
    check("the anchor no longer prints an owner token",
          "--owner" not in re.search(r"def cmd_anchor\b.*?(?=\n# ── CLI)", state_src, re.DOTALL).group(0))


# ── (5) anchor text matches the skill's quotes ───────────────────────────

def anchor_match_case(text):
    print("\n=== sweep_state.py anchor text matches skill invocation syntax ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    state_src = read(STATE_PY)
    func_m = re.search(r"def cmd_anchor\b.*?(?=\ndef |\n# ── CLI)", state_src, re.DOTALL)
    check("found cmd_anchor in sweep_state.py", func_m is not None)
    if not func_m:
        return
    forms = re.findall(r'f"((?:Resume|Take over) with [^"]*)"', func_m.group(0))
    canonical = sorted(f.replace("{p.name}", "<slug>").strip() for f in forms)
    check("cmd_anchor builds exactly the resume form and the takeover form",
          canonical == ["Resume with /loop /maestro:sweep resume <slug>",
                        "Take over with /maestro:sweep takeover <slug>"], canonical)

    quoted = [norm(q) for q in re.findall(r"`((?:Resume|Take over)\s+with\s+[^`]+)`", text)]
    check("SKILL.md quotes the anchor's lines", len(quoted) >= 2, quoted)
    mismatched = [q for q in quoted if q not in canonical]
    check("every backtick-quoted anchor line in SKILL.md matches sweep_state.py exactly",
          not mismatched, f"expected one of {canonical!r}, got {mismatched!r}")
    for c in canonical:
        check(f"SKILL.md quotes {c!r}", c in quoted, quoted)


# ── (6) every exit code is documented ────────────────────────────────────

def exit_codes_case(text):
    print("\n=== SKILL.md documents every sweep_state.py exit code ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    state_src = read(STATE_PY)
    codes = {int(c) for c in re.findall(r"\breturn ([0-9])\b", state_src)}
    check("sweep_state.py returns codes 0-5", codes == {0, 1, 2, 3, 4, 5}, codes)
    rows = {int(m) for m in re.findall(r"^\| ([0-9]) \|", text, re.M)}
    check("SKILL.md's exit-code table has a row for every code", codes <= rows, (codes, rows))
    check("exit 2 says stop and report", re.search(r"^\| 2 \|.*stop.*report", text, re.M) is not None)
    check("exit 3 is the finished sweep", re.search(r"^\| 3 \|.*(nothing pending or running|finished)",
                                                   text, re.M) is not None)
    check("exit 4 explains takeover", re.search(r"^\| 4 \|.*takeover", text, re.M) is not None)
    check("exit 5 reschedules at the minimum delay",
          re.search(r"^\| 5 \|.*minimum delay", text, re.M) is not None)
    check("step 1 never proceeds past a recover that didn't exit 0",
          "where `recover` did not\n   exit 0" in text or "recover` did not exit 0" in norm(text))
    check("set-policy only on Griffin's explicit request",
          "Only on Griffin's explicit request" in text)
    check("invalid-policy remedy points to set-policy, not a hand edit",
          re.search(r"\*\*invalid policy\*\*.*?set-policy", text, re.DOTALL) is not None
          and "fix\n     `policy.json`" not in text and "re-run `new`" not in text)


def lease_doc_case(text):
    print("\n=== SKILL.md — loop lease and /clear are documented ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    check("skill references the 'lease' subcommand",
          re.search(r'\$STATE"?\s+lease\b', text) is not None)
    check("lease is renewed right before ScheduleWakeup",
          re.search(r"right before every `ScheduleWakeup`", text) is not None
          and "lease <slug> --in <delay_s>" in text)
    check("lease is released when the loop ends", text.count("--release") >= 3, text.count("--release"))
    check("lease exit 4 means another session took over — no reschedule",
          "If `lease` itself exits 4" in text)
    check("/clear changes the session id (open chunk or live lease -> takeover)",
          "**`/clear` changes the session id**" in text and "loop\n  lease held by the pre-`/clear`" in text)
    state_src = read(STATE_PY)
    check("anchor reports the lease holder", "is leased by another session" in state_src)


def skill_blocks(text):
    """SKILL.md split into instruction blocks: a new block at every blank
    line, list item, numbered step or table row."""
    blocks, cur = [], []
    for line in text.splitlines():
        starts = re.match(r"^\s*(- |\| |\d+\. )", line)
        if not line.strip() or starts:
            if cur:
                blocks.append("\n".join(cur))
            cur = [line] if line.strip() else []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def terminal_paths_case(text):
    print("\n=== SKILL.md — every terminal path names ScheduleWakeup(stop: true) ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    stop_call = "ScheduleWakeup(stop: true)"
    stop_norm = lambda b: norm(b).replace("stop: true )", "stop: true)")
    check("SKILL.md cites the fallback-wakeup rule from the scheduled-tasks docs",
          "https://code.claude.com/docs/en/scheduled-tasks" in text
          and "schedules one fallback wakeup about 20 minutes later" in text)
    check("the END procedure releases the lease, then calls the stop",
          re.search(r"\*\*END the loop\*\*.*?lease <slug> --release.*?ScheduleWakeup\(stop: true\)",
                    text, re.DOTALL) is not None)
    forbidden = ["no `ScheduleWakeup`", "do not `ScheduleWakeup`", "don't reschedule, tell Griffin, end",
                 "no\n     `ScheduleWakeup`"]
    found = [f for f in forbidden if f in text]
    check("no path tells you to end WITHOUT the stop call", not found, found)
    # instructions that end the loop: "END with …", "END (…)", "and end",
    # "stop and report", "end the loop with"
    terminal = re.compile(r"\bEND (with|\()|\band end\b|stop and report|end the loop with", re.I)
    missing = [norm(b)[:90] for b in skill_blocks(text)
               if terminal.search(b) and "stop: true" not in norm(b)
               and "## Every iteration ends" not in b and "ScheduleWakeup(stop: true)" not in stop_norm(b)
               and not b.lstrip().startswith("#")]
    check("every block that ends the loop names ScheduleWakeup(stop: true)", not missing, missing)
    rows = {int(m.group(1)): m.group(0) for m in re.finditer(r"^\| ([0-9]) \|.*$", text, re.M)}
    for code in (1, 2, 3, 4):
        check(f"exit-code row {code} ends with {stop_call}", stop_call in rows.get(code, ""), rows.get(code))
    check("done/fail exit 5 is retried, not dropped",
          re.search(r"^\| 5 \|.*retry that same call", text, re.M) is not None)
    check("the exit-5 lease claim is corrected (no 'chunk-start lease still covers')",
          "chunk-start lease still covers" not in text)
    check("background sessions may go blind is documented", "backgrounded (agent view)" in text)


def main():
    text = frontmatter_case()
    script_surface_case(text)
    resume_prompt_case(text)
    owner_case(text)
    anchor_match_case(text)
    exit_codes_case(text)
    lease_doc_case(text)
    terminal_paths_case(text)
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)}): {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
