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

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
PYPROJECT = REPO / "pyproject.toml"
README = REPO / "README.md"

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
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert _name_of(line) != "jobcore", (
            "jobcore stays a documented sibling step, not a requirement: %r" % line
        )


def test_every_requirement_declares_a_floor():
    """A bare package name pins nothing and resolves to whatever shipped today.

    Deliberately a FLOOR check, not a ceiling check. Capping every dependency
    would be cargo-culting; only the framework package carries a ceiling here.
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert re.search(r"[<>=~!]", line), "%r declares no version at all" % line
