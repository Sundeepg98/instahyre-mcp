"""The jobcore pin must satisfy what this repo actually uses -- names, not just modules.

THE HOLE THIS CLOSES, WALKED INTO TWICE IN ONE DAY. ``requirements-ci.txt``
pins jobcore to an exact commit. A local venv installs jobcore EDITABLE from
``../jobcore``, so it always has whatever is newest on disk. Adding a
``from jobcore...`` import without bumping the pin therefore leaves the local
suite fully green and kills CI at COLLECTION. The local suite CANNOT fail this
way, so nothing on a developer box will ever tell you the pin is stale.

  2026-08-22, morning: commit 4bfd986 added ``jobcore.buildinfo`` and
  ``jobcore.paths`` while the pin said 16ae934, which contains NEITHER.
  Red on 3.10 and 3.11, every collection::

      ImportError: cannot import name 'paths' from 'jobcore'

  2026-08-22, afternoon: commit 9f3f1f7 changed one call to
  ``jobcore.buildinfo.self_stamp()`` while the pin said d1720c3::

      AttributeError: module 'jobcore.buildinfo' has no attribute 'self_stamp'

A 15-line comment above the pin already spelled out the discipline before the
first break. It was still forgotten, twice, because nothing MECHANICAL checked.

WHY THIS IS NOT A COPY OF THE NAUKRI CHECK
------------------------------------------
The sibling repo's version lists the MODULES present at the pinned commit. That
catches the morning break and would MISS the afternoon one entirely:
``jobcore.buildinfo`` exists at d1720c3 -- it is the ATTRIBUTE ``self_stamp``
that does not. A module-level check passes and CI still dies.

So this asserts at the level the code actually depends on: every NAME this repo
imports from a jobcore module, and every attribute it reads off a jobcore module
ALIAS, must exist in that module's source AT THE PINNED COMMIT.

BOTH FAILURE SHAPES ARE PROVEN, NOT ASSUMED.
``test_a_pin_missing_the_attribute_is_caught`` and
``test_a_pin_missing_the_module_is_caught`` re-run the whole check against
d1720c3 and 16ae934 and require a real complaint from each. A pin check that
catches only one of the two is half an instrument.

IT IS OFFLINE AND CHEAP. One ``git show`` per module against the ``../jobcore``
clone already on the box. It does NOT fetch, and it NEVER imports from the
pinned commit -- it reads the file's TEXT and parses it. When the clone is
absent (a runner checks out instahyre alone) or does not know the commit, every
check SKIPS with a reason rather than inventing an answer. The value is catching
this locally, BEFORE the push, which is the round trip that costs the time.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_CI = REPO_ROOT / "requirements-ci.txt"
JOBCORE_CLONE = REPO_ROOT.parent / "jobcore"

#: ``jobcore @ git+https://github.com/Sundeepg98/jobcore@<hex>``
PIN_RE = re.compile(r"^jobcore\s*@\s*git\+\S+?@([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)

#: Scanned for jobcore usage. ``tests`` is included deliberately: the CI job
#: runs this suite, so a name a TEST imports from jobcore breaks collection
#: exactly as hard as one the package imports.
SCANNED_DIRS = ("instahyre_server", "tests")


# ---------------------------------------------------------------------------
# Reading the pinned commit
# ---------------------------------------------------------------------------


def pinned_sha() -> str:
    match = PIN_RE.search(REQUIREMENTS_CI.read_text(encoding="utf-8"))
    assert match, "no jobcore pin line found in requirements-ci.txt"
    return match.group(1)


def _git(*args: str):
    result = subprocess.run(
        ["git", "-C", str(JOBCORE_CLONE), *args], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def clone_knows(sha: str) -> bool:
    if not (JOBCORE_CLONE / ".git").exists():
        return False
    return _git("cat-file", "-e", "%s^{commit}" % sha) is not None


def modules_at(sha: str) -> set:
    """Module names under ``src/jobcore/`` at ``sha``."""
    listing = _git("ls-tree", "--name-only", sha, "src/jobcore/") or ""
    out = set()
    for line in listing.splitlines():
        name = Path(line.strip()).name
        if name.endswith(".py"):
            out.add(name[:-3])
    return out


def source_at(sha: str, module: str):
    """The TEXT of one module at ``sha``. Never imported -- only parsed."""
    return _git("show", "%s:src/jobcore/%s.py" % (sha, module))


def top_level_names(source: str) -> set:
    """Every name a ``from module import X`` could bind, by parsing the source.

    Includes re-exports (``from .policy import Weights`` in ``__init__.py``)
    and plain imports (``import subprocess`` in ``buildinfo.py``), because both
    are real attributes of the imported module and this repo reads one of each.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# What this repo needs
# ---------------------------------------------------------------------------


def _requirements_in(path: Path):
    """``(module, name)`` pairs this file needs from jobcore, plus its modules.

    ``module`` is a jobcore submodule, or ``"__init__"`` for a name taken off
    the package itself. Two shapes are collected:

    * DIRECT   -- ``from jobcore.paths import relativise_known``
    * INDIRECT -- ``from jobcore import buildinfo as _jb`` ... ``_jb.self_stamp()``

    The indirect one is the whole reason this file exists. It is the shape that
    broke CI in the afternoon, and the shape a module-listing check cannot see.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set(), set()

    needed = set()
    modules = set()
    aliases = {}  # local name -> jobcore submodule, or "__init__"

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "jobcore":
                for alias in node.names:
                    # May be a submodule OR a re-exported name; resolved later.
                    needed.add(("__init__", alias.name))
                    aliases[alias.asname or alias.name] = alias.name
            elif node.module.startswith("jobcore."):
                submodule = node.module.split(".", 1)[1]
                modules.add(submodule)
                for alias in node.names:
                    needed.add((submodule, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jobcore":
                    aliases[alias.asname or "jobcore"] = "__init__"
                elif alias.name.startswith("jobcore."):
                    submodule = alias.name.split(".", 1)[1]
                    modules.add(submodule)
                    if alias.asname:
                        aliases[alias.asname] = submodule

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            target = aliases.get(node.value.id)
            if target is not None:
                needed.add((target, node.attr))

    return needed, modules


def requirements():
    needed = set()
    modules = set()
    for directory in SCANNED_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            file_needed, file_modules = _requirements_in(path)
            needed |= file_needed
            modules |= file_modules
    return needed, modules


def unsatisfied_at(sha: str):
    """Everything this repo needs that jobcore does not provide at ``sha``."""
    needed, modules = requirements()
    available = modules_at(sha)
    cache = {}

    def names_of(module):
        if module not in cache:
            source = source_at(sha, module)
            cache[module] = top_level_names(source) if source else None
        return cache[module]

    problems = []
    for module in sorted(modules):
        if module not in available:
            problems.append("jobcore.%s (MODULE MISSING)" % module)

    for module, name in sorted(needed):
        if module == "__init__":
            # `from jobcore import X`: X is a submodule or a package re-export.
            if name in available:
                continue
            package = names_of("__init__")
            if package is None or name in package:
                continue
            problems.append("jobcore.%s (NAME MISSING from the package)" % name)
            continue
        if module not in available:
            continue  # already reported as a missing module
        present = names_of(module)
        if present is not None and name not in present:
            problems.append("jobcore.%s.%s (ATTRIBUTE MISSING)" % (module, name))

    return sorted(set(problems))


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _require_clone(sha: str):
    if not clone_knows(sha):
        pytest.skip(
            "../jobcore clone is absent or does not know commit %s; this check "
            "is offline by design and never fetches" % sha
        )


def test_the_pin_is_an_exact_commit():
    """A moving ``@master`` would let another repo turn this repo's CI red."""
    sha = pinned_sha()
    assert re.fullmatch(r"[0-9a-fA-F]{7,40}", sha)


def test_the_pinned_commit_provides_everything_this_repo_uses():
    """The check that would have caught BOTH of today's breaks before the push."""
    sha = pinned_sha()
    _require_clone(sha)

    problems = unsatisfied_at(sha)

    assert not problems, (
        "requirements-ci.txt pins jobcore at %s, which does NOT provide:\n  %s\n"
        "CI will die at COLLECTION while every local run stays green, because "
        "the local venv has jobcore installed EDITABLE from ../jobcore. Bump "
        "the pin in the SAME commit that adds the import."
        % (sha[:12], "\n  ".join(problems))
    )


def test_the_scanner_actually_sees_what_this_repo_uses():
    """Guards the guard.

    A scanner that silently found nothing would make the check above pass
    vacuously forever. Every pair below is real usage in this repo today, and
    the list deliberately mixes the direct and indirect shapes.
    """
    needed, modules = requirements()

    for module in ("buildinfo", "paths", "config"):
        assert module in modules or ("__init__", module) in needed, (
            "scanner missed jobcore.%s, which this repo does use" % module
        )

    for pair in (
        ("buildinfo", "self_stamp"),      # indirect, via an aliased module
        ("paths", "display_path"),        # indirect
        ("paths", "relativise_known"),    # direct
        ("config", "Loaded"),             # direct
        ("config", "current"),            # indirect
    ):
        assert pair in needed, (
            "scanner missed jobcore.%s.%s, which this repo does use" % pair
        )


# --- the two failure shapes, both proven ------------------------------------
#
# A pin check nobody watched fail is a claim. These re-run the REAL check
# against two real commits and require a real complaint from each.

#: Has ``jobcore.buildinfo``, but NOT ``buildinfo.self_stamp``. Today's
#: afternoon break, and the case a module-listing check cannot see.
SHA_MISSING_ATTRIBUTE = "d1720c3"

#: Has neither ``jobcore.buildinfo`` nor ``jobcore.paths``. The morning break.
SHA_MISSING_MODULE = "16ae934"


def test_a_pin_missing_the_attribute_is_caught():
    """The AFTERNOON break. The one a module-level check would miss."""
    _require_clone(SHA_MISSING_ATTRIBUTE)

    assert "buildinfo" in modules_at(SHA_MISSING_ATTRIBUTE), (
        "precondition: jobcore.buildinfo must EXIST at this commit, or this "
        "test proves a missing module rather than a missing attribute"
    )

    problems = unsatisfied_at(SHA_MISSING_ATTRIBUTE)

    assert any("buildinfo.self_stamp" in p for p in problems), (
        "the check did not catch the missing ATTRIBUTE; it reported %r" % (problems,)
    )
    assert all("MODULE MISSING" not in p for p in problems), (
        "this commit has every module this repo imports; a MODULE complaint "
        "means the scanner is misreading the tree: %r" % (problems,)
    )


def test_a_pin_missing_the_module_is_caught():
    """The MORNING break, kept so the cheaper failure shape stays covered."""
    _require_clone(SHA_MISSING_MODULE)

    problems = unsatisfied_at(SHA_MISSING_MODULE)

    for module in ("buildinfo", "paths"):
        assert any(
            p.startswith("jobcore.%s (MODULE MISSING)" % module) for p in problems
        ), (
            "the check did not catch missing jobcore.%s; it reported %r"
            % (module, problems)
        )
