"""The dependency pins, asserted rather than trusted.

THE HAZARD THIS GUARDS
----------------------
On 2026-08-20 the sibling naukri server's CI went red for a breakage no local
run could show. naukri declared `mcp[cli]>=1.25.0` with no upper bound. `mcp
2.0.0` shipped, relocating `mcp/server/fastmcp` to `mcp/server/mcpserver`, and
naukri imports the old path unconditionally -- so a CLEAN resolve picked 2.0.0
and all 55 of its test modules died at collection: "5 deselected, 55 errors",
zero tests run. Every LOCAL naukri run stayed green, because that venv held mcp
1.26.0 installed before 2.0.0 existed.

This server was NOT affected by that move -- it depends on the standalone
`fastmcp` project rather than `mcp[cli]`, and `mcp.server.fastmcp` is not in its
import graph at all (measured; see requirements.txt). But it shared the
underlying disease: an unbounded `>=` on the framework package it does depend
on. These tests hold the treatment in place.

WHY THESE TESTS READ FILES AS TEXT
----------------------------------
Because the alternative is the check that already failed to fail. Asserting
against the INSTALLED version would pass happily in exactly the venv that hides
the bug -- which is what happened to naukri for a full day. The DECLARATION is
the thing under test, not the cache of an old resolve.

Pure: no network, no install, three small reads of repo files. The install
itself is checked by scripts/clean_install_check.py, which throws the cached
resolve away and starts from scratch.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
REQUIREMENTS_CI = REPO / "requirements-ci.txt"
PYPROJECT = REPO / "pyproject.toml"
README = REPO / "README.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The major the suite is actually measured green on. Bumping this line is a
# claim that the suite has been RUN on the newer major, not a formality.
FASTMCP_TESTED_MAJOR = 3


def _requirement_lines(path):
    """Yield the non-comment, non-blank requirement lines of a pip file."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            yield line


def _name_of(requirement):
    """The distribution name at the head of a requirement string.

    The `@` and whitespace in the split set are load-bearing: PEP 508 direct
    references look like `jobcore @ git+https://.../jobcore@d1d44bb`, which
    carries no version operator at all. Splitting on operators alone returns the
    entire URL as the "name", and test_jobcore_is_not_a_requirement_line then
    silently stops catching the exact line it exists to catch -- measured, as a
    live miss, when these guards were run against a mutated tree.
    """
    return re.split(r"[<>=\[!~;@\s]", requirement, maxsplit=1)[0].strip().lower()


def _requirements_for(name):
    return [ln for ln in _requirement_lines(REQUIREMENTS) if _name_of(ln) == name]


def _pyproject_requirements():
    """Every requirement string in pyproject.toml, from `dependencies` and every extra.

    Parsed with a regex rather than a TOML library on purpose: this test must
    run with nothing installed beyond pytest, and it is asserting on what the
    FILE says.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    found = {}
    for block in re.findall(r"=\s*\[(.*?)\]", text, re.S):
        for quoted in re.findall(r'"([^"]+)"', block):
            if re.match(r"^[A-Za-z][A-Za-z0-9_.-]*\s*[<>=~!\[]", quoted) or re.match(
                r"^[A-Za-z][A-Za-z0-9_.-]*$", quoted
            ):
                found.setdefault(_name_of(quoted), []).append(quoted)
    return found


def _upper_bound(requirement):
    """The integer major in a `<N` clause, or None if the requirement has no cap."""
    match = re.search(r"<\s*(\d+)", requirement)
    return int(match.group(1)) if match else None


def test_fastmcp_has_an_upper_bound():
    """Unbounded on a framework package is how naukri's build got broken."""
    lines = _requirements_for("fastmcp")
    assert lines, "the fastmcp requirement disappeared from requirements.txt"
    assert all(_upper_bound(ln) is not None for ln in lines), (
        "fastmcp must carry an upper bound: its next major is code nobody has "
        "run this server against, and fastmcp 2.0 was itself a rewrite of the "
        "library that became mcp.server.fastmcp. Found: %r" % lines
    )


def test_the_fastmcp_cap_is_not_narrowed_below_the_major_the_suite_runs_on():
    """<3 would be naukri's fix cargo-culted onto a repo that does not need it.

    Measured 2026-08-20 in a throwaway venv, nothing borrowed from a local
    install: the resolve picked fastmcp 3.4.7 and the suite gave
    "242 passed in 5.11s". Capping below that pins a working server to an older
    major for no reason anyone could point at.
    """
    for line in _requirements_for("fastmcp"):
        cap = _upper_bound(line)
        assert cap > FASTMCP_TESTED_MAJOR, (
            "the suite is measured green on fastmcp %d.x, so the cap must sit "
            "ABOVE it at the next untested major: %r" % (FASTMCP_TESTED_MAJOR, line)
        )


def test_pyproject_and_requirements_declare_the_same_bounds():
    """Two sources of truth for dependencies is how a cap gets applied to one of them.

    requirements.txt is what a developer installs; pyproject.toml is what `pip
    install instahyre-mcp` resolves. A cap that lands in only one file protects
    only one of those paths, and nothing says which one bit you.
    """
    from_pyproject = _pyproject_requirements()
    for line in _requirement_lines(REQUIREMENTS):
        name = _name_of(line)
        if name not in from_pyproject:
            continue
        for other in from_pyproject[name]:
            assert _upper_bound(other) == _upper_bound(line), (
                "%s is capped differently in the two files: requirements.txt "
                "says %r, pyproject.toml says %r" % (name, line, other)
            )


def test_the_readme_install_recipe_can_actually_run_the_tests():
    """README's Install block runs `pip install -r requirements.txt` then `pytest`.

    If pytest is not in that file, the second line of the published recipe dies
    with ModuleNotFoundError on every fresh clone. It did, until 2026-08-20.
    """
    readme = README.read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in readme, (
        "the README no longer installs from requirements.txt; this guard needs "
        "to be pointed at whatever replaced it"
    )
    assert "pytest" in readme, "the README no longer mentions running pytest"
    assert _requirements_for("pytest"), (
        "README tells a new developer to run pytest straight after "
        "`pip install -r requirements.txt`, so pytest has to be in that file"
    )


def test_mcp_is_not_declared_directly():
    """This server talks to `fastmcp`, and `mcp` is fastmcp's dependency to manage.

    Declaring `mcp` here would create a second, conflicting source of truth for
    a package this code never imports: measured 2026-08-20, after
    `import instahyre_server.server` sys.modules holds mcp.server.auth,
    mcp.server.experimental and mcp.server.models but NOT mcp.server.fastmcp.
    fastmcp already caps it at `mcp<2.0,>=1.24.0`; a direct line here could only
    fight that or duplicate it.
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert _name_of(line) != "mcp", (
            "mcp arrives transitively through fastmcp, which caps it itself: %r" % line
        )


def test_jobcore_is_not_a_requirement_line():
    """A `jobcore @ git+...` line would silently clobber `pip install -e ../jobcore`.

    Measured 2026-08-20 in a throwaway venv: after the editable install,
    installing jobcore from its git URL printed "Attempting uninstall: jobcore /
    Successfully uninstalled" and the "Editable project location" line vanished
    from `pip show`. pip prints no "already satisfied" line for a direct-URL
    requirement, so the clobber is invisible unless you go looking.

    It lives in requirements-ci.txt instead -- see the tests below, which hold
    the other half of the arrangement: absent HERE, present THERE, pinned.
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert _name_of(line) != "jobcore", (
            "jobcore stays a documented sibling step, not a requirement: %r" % line
        )


# ---------------------------------------------------------------------------
# jobcore: absent from requirements.txt, and therefore REQUIRED to be somewhere
# ---------------------------------------------------------------------------
#
# The test above is only half a rule. Keeping jobcore out of requirements.txt
# protects the editable install; on its own it also means a sibling-free clone
# gets no jobcore at all -- which used to be survivable, because scoring.py fell
# back to a second scorer, and is not any more. These three tests are the other
# half: the dependency has to exist, from git, pinned to a commit.


def test_requirements_ci_exists_and_includes_the_base_file():
    assert REQUIREMENTS_CI.is_file(), (
        "requirements-ci.txt is the only recipe a checkout WITHOUT ../jobcore "
        "has; without it a fresh clone cannot import instahyre_server.scoring"
    )
    text = REQUIREMENTS_CI.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text, (
        "requirements-ci.txt must layer ON TOP of requirements.txt, or the two "
        "drift and CI stops testing what a developer installs"
    )


def test_requirements_ci_pins_jobcore_to_an_exact_commit():
    """`@master` would let a commit in another repo turn this repo's CI red.

    A 40-hex sha, not a branch and not a tag: tags move, branches move, and
    tests/test_scoring_policy.py asserts behaviour that arrived in a specific
    jobcore commit. Bumping this line is the visible way to adopt a change.
    """
    lines = [ln for ln in _requirement_lines(REQUIREMENTS_CI)
             if _name_of(ln) == "jobcore"]
    assert lines, "requirements-ci.txt no longer installs jobcore"
    for line in lines:
        assert "git+https://github.com/Sundeepg98/jobcore@" in line, line
        ref = line.split("jobcore@", 1)[1].strip()
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            "pin jobcore to a full commit sha, not %r -- a moving ref makes "
            "this repo's CI depend on another repo's HEAD" % ref
        )


def test_ci_installs_from_the_ci_file_not_the_bare_requirements():
    """A runner has no ../jobcore, so `pip install -r requirements.txt` alone
    would give it a server whose scoring module raises on import."""
    assert CI_WORKFLOW.is_file(), "this repo has no CI workflow"
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "requirements-ci.txt" in text
    assert not re.search(r"pip install -r requirements\.txt\s*$", text, re.M), (
        "CI must install requirements-ci.txt; the bare file omits jobcore"
    )


def test_the_readme_documents_both_ways_to_get_jobcore():
    """One recipe for a developer with the sibling, one for everyone else.

    Publishing only the editable line strands every fresh clone; publishing
    only the git line teaches developers to clobber their own editable install.
    """
    readme = README.read_text(encoding="utf-8")
    assert "pip install -e ../jobcore" in readme
    assert "requirements-ci.txt" in readme


def _touches_sys_path(path):
    """Does this module actually TOUCH ``sys.path``? Parsed, not grepped.

    The text version of this check was written first and immediately went red
    on ``scoring.py`` -- whose docstring explains that the ``sys.path`` hack was
    removed. A checker that cannot tell code from the prose describing its
    absence would force the explanation to be deleted to stay green, which is
    the wrong direction. The AST does not see docstring prose at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            return True
    return False


def test_no_module_still_reaches_jobcore_through_a_sys_path_hack():
    """scoring.py used to insert ``../../jobcore/src`` into sys.path.

    That import works and installs nothing, so `importlib.metadata` cannot see
    it, nothing pins its version, and CI and a developer box silently run
    different scorers. Taking the dependency properly is the fix; this stops it
    coming back the next time an import fails on someone's machine.
    """
    offenders = [p.name for p in (REPO / "instahyre_server").rglob("*.py")
                 if _touches_sys_path(p)]
    assert offenders == [], (
        "%s reaches jobcore by path rather than by dependency" % offenders
    )


def test_the_sys_path_scan_trips_on_the_hack_it_replaced__CONTROL(tmp_path):
    """Fed the exact shape the old scoring.py had, the scanner must say yes."""
    decoy = tmp_path / "old_scoring.py"
    decoy.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "_JOBCORE_SRC = Path(__file__).resolve().parent.parent.parent / 'jobcore' / 'src'\n"
        "if _JOBCORE_SRC.is_dir() and str(_JOBCORE_SRC) not in sys.path:\n"
        "    sys.path.insert(0, str(_JOBCORE_SRC))\n",
        encoding="utf-8",
    )
    assert _touches_sys_path(decoy) is True


def test_every_requirement_declares_a_floor():
    """A bare package name pins nothing and resolves to whatever shipped today.

    Deliberately a FLOOR check, not a ceiling check. Capping every dependency
    would be cargo-culting; only the framework package carries a ceiling here.
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert re.search(r"[<>=~!]", line), "%r declares no version at all" % line
