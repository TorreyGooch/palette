"""The instructions every agent reads, and the guard that keeps main integrated.

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`. The rules that must hold
for every agent therefore live in `AGENTS.md`, which `CLAUDE.md` imports, and
the subsystem detail lives in `docs/`. Documentation rots silently - a moved
file, a renamed section, a rule edited away - so the parts that would hurt to
lose are pinned here.

`main` is what both machines pull, so only the integrator moves it. For a clone
with hooks enabled that is enforced by `hooks/guard-main.sh`, run from
`pre-push`; these tests run the guard itself.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "AGENTS.md"
CLAUDE = REPO / "CLAUDE.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# -- the instructions -----------------------------------------------------------

def test_claude_code_loads_the_shared_file():
    """Without the import a Claude session would never see the shared rules."""
    lines = [l.strip() for l in text(CLAUDE).splitlines()]
    assert "@AGENTS.md" in lines


def test_every_docs_path_named_in_the_instructions_exists():
    named = set()
    for path in (AGENTS, CLAUDE, *(REPO / ".claude" / "skills").glob("*/SKILL.md")):
        named |= set(re.findall(r"docs/[a-z_]+\.md", text(path)))
    assert named, "the instructions should point somewhere"
    missing = sorted(p for p in named if not (REPO / p).is_file())
    assert not missing, f"named but absent: {missing}"


def test_no_doc_is_orphaned():
    """A file nobody is told about is a file nobody reads."""
    index = text(AGENTS)
    orphans = sorted(p.name for p in (REPO / "docs").glob("*.md")
                     if f"docs/{p.name}" not in index)
    assert not orphans, f"not listed in AGENTS.md: {orphans}"


@pytest.mark.parametrize("rule", [
    "PaletteLibrary",          # never edit the media library's files
    "--impersonate",           # anonymous YouTube, no forged fingerprints
    "QS_IGNORE_COOLDOWN",      # a limit means wait
    "QS_CUT_ALIGN_MIN",        # the alignment guard is not a knob
    "tailscale funnel",        # the app has no authentication
    "stored shape",            # schema changes need a person
    "PALETTE_INTEGRATOR",      # only the integrator moves main
])
def test_the_rules_every_agent_follows_are_in_the_shared_file(rule):
    """These bind Codex too, and Codex reads nothing under .claude/."""
    assert rule in text(AGENTS)


def test_each_role_brief_points_at_the_shared_file():
    for brief in (REPO / ".claude" / "skills").glob("*/SKILL.md"):
        assert "AGENTS.md" in text(brief), f"{brief.parent.name} brief"


# -- the guard on main ------------------------------------------------------------

GUARD = REPO / "hooks" / "guard-main.sh"


def _bash():
    """A bash that can run a script given inline, or None.

    Run as `bash -c <script>` so no path has to be translated - the bash found
    on Windows may be Git Bash or WSL, and they disagree about paths.
    """
    exe = shutil.which("bash")
    if not exe:
        return None
    try:
        ok = subprocess.run([exe, "-c", "echo ok"], capture_output=True,
                            text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if ok.returncode == 0 and ok.stdout.strip() == "ok" else None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no usable bash")


def push(*remote_refs, integrator=False):
    lines = "".join(f"refs/heads/x abc123 {ref} def456\n" for ref in remote_refs)
    env = {"PATH": shutil.os.environ.get("PATH", "")}
    if integrator:
        env["PALETTE_INTEGRATOR"] = "1"
    return subprocess.run([BASH, "-c", GUARD.read_bytes().decode("utf-8")],
                          input=lines, capture_output=True, text=True,
                          env=env, timeout=20)


def test_the_guard_has_unix_line_endings():
    """A CRLF in a bash script fails as '$'\\r': command not found' on the
    first line, which would disable the guard exactly where it is needed."""
    assert b"\r" not in GUARD.read_bytes()


@needs_bash
def test_a_push_to_main_is_refused():
    result = push("refs/heads/main")
    assert result.returncode == 1
    assert "only the integrator" in result.stderr


@needs_bash
def test_the_integrator_may_push_main():
    assert push("refs/heads/main", integrator=True).returncode == 0


@needs_bash
def test_a_branch_push_passes():
    assert push("refs/heads/codex/typed-bodies").returncode == 0


@needs_bash
def test_main_hidden_among_other_refs_is_still_refused():
    """`git push --all` sends several refs at once; one of them being main is
    enough."""
    assert push("refs/heads/architect/docs", "refs/heads/main",
                "refs/heads/codex/x").returncode == 1


@needs_bash
def test_a_branch_merely_named_like_main_passes():
    assert push("refs/heads/main-notes").returncode == 0


def test_pre_push_runs_the_guard_before_the_suite():
    hook = text(REPO / "hooks" / "pre-push")
    assert "guard-main.sh" in hook
    assert hook.index("guard-main.sh") < hook.index("pytest")


def test_deploy_checks_the_branch_before_it_pushes():
    """It used to push main from any branch and then blame the server."""
    deploy = text(REPO / "deploy.ps1")
    branch_check = deploy.index("git branch --show-current")
    push_main = deploy.index("git push origin main")
    assert branch_check < push_main
    assert deploy.index("PALETTE_INTEGRATOR") < push_main
