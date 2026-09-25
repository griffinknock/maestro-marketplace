#!/usr/bin/env python3
"""Checks for maestro/skills/sweep/SKILL.md: valid frontmatter, that every
sweep_state.py / pace.py subcommand or flag it references actually exists
(grepped from their argparse definitions), that the resume/wakeup prompt
string is consistent everywhere it appears, and that it matches the anchor
text sweep_state.py's SessionStart hook prints.

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
    check("skill references 'new'", "new" in referenced)
    check("skill references 'recover'", "recover" in referenced)
    check("skill references 'check'", "check" in referenced)
    check("skill references 'next'", "next" in referenced)
    check("skill references 'done'", "done" in referenced)
    check("skill references 'fail'", "fail" in referenced)
    check("skill references 'finding'", "finding" in referenced)
    check("skill references 'add'", "add" in referenced)
    check("skill references 'amend-plan'", "amend-plan" in referenced)
    check("skill references 'end-chunk'", "end-chunk" in referenced)
    check("skill references 'status'", "status" in referenced)
    check("skill references 'list'", "list" in referenced)

    # flags used on --policy / --n / --note / --reason / --text / --item /
    # --label / --from-finding / --file must exist somewhere in argparse.
    #
    # --owner and --stale-after are part of the shared cross-worktree
    # contract (C1: a stable per-loop OWNER threaded through next / recover
    # / end-chunk; C3: recover's --stale-after) that the skill is written
    # against, but the scripts in *this* worktree don't implement yet —
    # other builders are wiring them into sweep_state.py in parallel. Treat
    # them as known-pending rather than failing the build, but say so
    # explicitly so this carve-out is removed once argparse catches up.
    PENDING_CONTRACT_FLAGS = {"owner", "stale-after"}
    flags_in_skill = set(re.findall(r"--([a-z][a-z-]*)", text))
    known_flags = set(re.findall(r'add_argument\("--([a-z-]+)"', state_src))
    known_flags |= set(re.findall(r'add_argument\("--([a-z-]+)"', pace_src))
    pending = sorted(f for f in flags_in_skill if f in PENDING_CONTRACT_FLAGS and f not in known_flags)
    if pending:
        print(f"  note  contract flags referenced but not yet in argparse (expected "
              f"until sweep_state.py/pace.py add them): {pending}")
    # dest= aliases (e.g. --from-finding maps via dest but the flag text itself is literal)
    unknown_flags = sorted(f for f in flags_in_skill
                            if f not in known_flags and f not in PENDING_CONTRACT_FLAGS)
    check("every --flag referenced by the skill exists in argparse or is a "
          "known-pending contract flag",
          not unknown_flags, unknown_flags)

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
    check("skill uses '/loop /maestro:sweep run <slug>' to start a sweep",
          run_count >= 1)
    check("skill uses '/loop /maestro:sweep resume <slug>' to resume one",
          resume_count >= 2, f"found {resume_count} occurrences, expected "
          ">= 2 (stop's printed resume command, and the run/resume section)")

    # C1: OWNER must ride along on the entry-point invocations Griffin is
    # actually told to type (the opening block, the `new`-section
    # confirmation, and `stop`'s printed resume line) — a bare run/resume
    # there can't be threaded through next/recover/end-chunk later in the
    # same loop. This deliberately does NOT require every occurrence to
    # carry --owner: the literal SessionStart anchor quote (see
    # anchor_match_case) only carries --owner once a chunk has actually
    # started — with no chunk started yet, sweep_state.py has no owner to
    # recover and prints the bare form instead.
    owner_run_count = text.count("/loop /maestro:sweep run <slug> --owner <owner>")
    owner_resume_count = text.count("/loop /maestro:sweep resume <slug> --owner <owner>")
    check("skill tells Griffin to type '/loop /maestro:sweep run <slug> "
          "--owner <owner>' to start a sweep",
          owner_run_count >= 1, owner_run_count)
    check("skill tells Griffin to type '/loop /maestro:sweep resume <slug> "
          "--owner <owner>' at least at stop's printed resume line and the "
          "run/resume section",
          owner_resume_count >= 2, owner_resume_count)

    # every occurrence of "/maestro:sweep resume" in the skill must be
    # wrapped in "/loop " — a bare resume can't self-schedule.
    bare = re.findall(r"(?<!/loop )/maestro:sweep resume <slug>", text)
    check("every '/maestro:sweep resume' in the skill is wrapped in '/loop '",
          not bare, bare)
    bare_run = re.findall(r"(?<!/loop )/maestro:sweep run <slug>", text)
    check("every '/maestro:sweep run' in the skill is wrapped in '/loop '",
          not bare_run, bare_run)

    # the skill must state ScheduleWakeup is /loop-only and name the tool
    check("skill names ScheduleWakeup", "ScheduleWakeup" in text)
    check("skill states ScheduleWakeup is /loop-only",
          re.search(r"only exists? inside.*`?/loop`?|only.*available.*`?/loop`?", text, re.I | re.DOTALL) is not None)


# ── (4) anchor text matches the skill's invocation syntax ────────────────

def anchor_match_case(text):
    print("\n=== sweep_state.py anchor text matches skill invocation syntax ===")
    if text is None:
        check("skipped — no SKILL.md text", False)
        return
    state_src = read(STATE_PY)

    # cmd_anchor builds its resume line in two documented forms: with
    # --owner once a chunk has started (owner comes from pace.jsonl's
    # latest start record), and without one when none has started yet.
    # Adjacent f-string literals split across lines are implicit
    # concatenation in Python source, so merge them before extracting the
    # literal "Resume with ..." text each branch actually prints.
    func_m = re.search(r"def cmd_anchor\b.*?(?=\ndef )", state_src, re.DOTALL)
    check("found cmd_anchor in sweep_state.py", func_m is not None)
    if not func_m:
        return
    merged = re.sub(r'"\s*\n\s*f"', "", func_m.group(0))
    resume_lines = re.findall(r"Resume with [^\"]+", merged)
    check("sweep_state.py's cmd_anchor builds exactly two resume-line forms "
          "(with --owner and without)",
          len(resume_lines) == 2, resume_lines)
    if len(resume_lines) != 2:
        return

    canonical = sorted(
        line.replace("{p.name}", "<slug>").replace("{owner}", "<owner>").strip()
        for line in resume_lines
    )
    canonical_with_owner = next(c for c in canonical if "--owner" in c)
    canonical_without_owner = next(c for c in canonical if "--owner" not in c)
    check("one form carries --owner and the other doesn't",
          canonical_with_owner != canonical_without_owner, canonical)
    check("anchor's subcommand ('resume') is documented in the skill as an "
          "entry point (wrapped by /loop)",
          "/loop /maestro:sweep resume <slug>" in text, canonical)

    # The bug this case exists to catch: SKILL.md previously *quoted* the
    # anchor's printed text with the wrong shape (missing /loop, or a form
    # sweep_state.py doesn't actually print) even though
    # "/loop /maestro:sweep resume <slug>" also appeared elsewhere in the
    # doc — a mere substring check would pass either way and miss it.
    # Require every backtick-quoted "Resume with ..." string in SKILL.md —
    # i.e. every place the skill quotes what the anchor prints, not every
    # place it tells Griffin to type a resume command — to match one of
    # the two canonical forms exactly, whitespace/newlines aside.
    quoted = re.findall(r"`(Resume\s+with\s+[^`]+)`", text)
    check("SKILL.md quotes at least one anchor string", len(quoted) >= 1, quoted)
    mismatched = []
    for q in quoted:
        normalized = re.sub(r"\s+", " ", q).strip()
        if normalized not in (canonical_with_owner, canonical_without_owner):
            mismatched.append(normalized)
    check("every backtick-quoted 'Resume with ...' string in SKILL.md "
          "matches one of sweep_state.py's two actual anchor forms exactly",
          not mismatched,
          f"expected one of {(canonical_with_owner, canonical_without_owner)!r}, "
          f"got {mismatched!r}")
    check("SKILL.md quotes the with-owner anchor form at least once",
          any(re.sub(r"\s+", " ", q).strip() == canonical_with_owner for q in quoted),
          quoted)
    check("SKILL.md quotes the without-owner anchor form at least once",
          any(re.sub(r"\s+", " ", q).strip() == canonical_without_owner for q in quoted),
          quoted)


def main():
    text = frontmatter_case()
    script_surface_case(text)
    resume_prompt_case(text)
    anchor_match_case(text)
    print("\n  " + ("PASS" if not FAILURES else f"FAIL ({len(FAILURES)}): {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
