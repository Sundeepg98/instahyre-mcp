"""Prove this server survives a CLEAN install. Run it before you trust a green suite.

WHY
---
A local venv is a cache of a resolve that happened in the past. It cannot tell
you what a resolve TODAY would produce, and that gap is not theoretical:

    On 2026-08-20 the sibling naukri server declared `mcp[cli]>=1.25.0` with no
    upper bound. `mcp 2.0.0` shipped, relocating `mcp/server/fastmcp` to
    `mcp/server/mcpserver`. Every LOCAL naukri run stayed green -- that venv held
    mcp 1.26.0, installed before 2.0.0 existed. A clean resolve picked 2.0.0 and
    all 55 test modules died at collection: "5 deselected, 55 errors", zero tests
    run. The local venv hid a completely broken clean install for a whole day.

This script is the check that would have caught it, and the only kind that can:
it throws the cached resolve away and starts from the declared requirements.

THE SECOND THING IT CATCHES, which is specific to this server
------------------------------------------------------------
The checkout is deliberately made ALONE, with no `../jobcore` sibling -- because
that is what a fresh clone or a CI runner actually has. jobcore is a REQUIRED
dependency (`instahyre_server/scoring.py` imports it at module scope, and the
local fallback scorer that used to cover its absence has been deleted), so a
sibling-free install has to get it from git. That is what `requirements-ci.txt`
is for, and this script runs exactly that recipe.

Until 2026-08-21 this script asserted the opposite: it PASSED on a sibling-free
clone that resolved to `scoring ENGINE = local-fallback`, and merely printed a
note saying the numbers were not comparable with any other board. A check that
passes on the broken configuration is the check that already failed to fail, so
step 4 now demands `jobcore` by name.

NOTE: this needs NETWORK and `git`, because the pinned jobcore comes from
GitHub. `pip install -r requirements.txt` alone -- the other half of the
recipe -- does not.

WHAT IT DOES
------------
  1. `git clone` this repo into a throwaway workspace -- COMMITTED state only,
     so nothing here reads or disturbs your working tree
  2. build a brand new venv
  3. run the documented sibling-free install recipe from README.md ("Install")
  4. import the server, and REQUIRE that the scoring engine is jobcore
  5. run the suite, and print the resolved version of everything installed

USAGE
-----
    python scripts/clean_install_check.py [--workdir DIR] [--keep]

`--workdir` defaults to a temp directory beside the repo (a throwaway venv is
~200 MB with playwright; do not put it on a full C:). The workspace is deleted
on success unless `--keep` is passed.

Exit code 0 means a clean install works. Non-zero means it does not, which is a
live bug even if every local run is green -- ESPECIALLY if every local run is
green.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPO_NAME = REPO.name

# The documented recipe from README.md, as pip argument lists. Keep this in step
# with the README: if the two disagree, one of them is lying to a new developer.
#
# requirements-ci.txt is `-r requirements.txt` PLUS jobcore pinned to a commit,
# which is the recipe for a checkout with no ../jobcore sibling -- exactly what
# this script builds.
INSTALL = [
    ["install", "-r", "requirements-ci.txt"],
]

IMPORT_PROBE = (
    "import instahyre_server, instahyre_server.server, instahyre_server.scoring as sc; "
    "import importlib.metadata as md; "
    "from instahyre_server import policy; "
    "print('instahyre_server', instahyre_server.__version__, 'on fastmcp', md.version('fastmcp')); "
    "print('scoring ENGINE =', sc.ENGINE, sc.ENGINE_VERSION); "
    "print('jobcore dist =', md.version('jobcore')); "
    "print('config source =', policy.current().source)"
)


def run(cmd, cwd, timeout=2400, env=None):
    """Run one command, echo it and its output verbatim, return (rc, output).

    *env* is MERGED onto the inherited environment, never a replacement: on
    Windows a bare env dict without SYSTEMROOT breaks socket startup, so a
    replacement would produce failures that have nothing to do with the check.
    """
    print("\n$ %s\n  (cwd=%s)" % (subprocess.list2cmdline(cmd), cwd), flush=True)
    started = time.time()
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=child_env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out, flush=True)
    print("[exit %d in %.1fs]" % (proc.returncode, time.time() - started), flush=True)
    return proc.returncode, out


def summary_line(output):
    """pytest's own summary line, quoted rather than re-counted.

    Its ABSENCE is the loudest possible result: it means pytest never got as far
    as running anything, which is exactly what a collection-time import failure
    looks like.
    """
    pattern = re.compile(
        r"\b\d+\s+(passed|failed|error|errors|deselected|skipped)\b|no tests ran"
    )
    for line in reversed(output.splitlines()):
        if pattern.search(line):
            return line.rstrip()
    return "<no pytest summary line was printed -- pytest never ran a test>"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", default=None, help="where to build the throwaway venv")
    parser.add_argument("--keep", action="store_true", help="do not delete the workspace")
    args = parser.parse_args()

    workspace = Path(args.workdir) if args.workdir else REPO.parent / ("_cleaninstall_" + REPO_NAME)
    checkout = workspace / REPO_NAME
    venv = workspace / "venv"
    py = venv / "Scripts" / "python.exe"
    if not sys.platform.startswith("win"):
        py = venv / "bin" / "python"

    print("=" * 78)
    print("CLEAN-INSTALL CHECK: %s   %s" % (REPO_NAME, time.strftime("%Y-%m-%d %H:%M:%S")))
    print("workspace: %s  (throwaway; the live tree at %s is never touched)" % (workspace, REPO))
    print("=" * 78)

    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)

    failures = []

    print("\n--- STEP 1: clone committed state, with NO jobcore sibling ---")
    print("    (jobcore therefore has to arrive from git, via requirements-ci.txt)")
    rc, _ = run(["git", "clone", "--no-hardlinks", "--quiet", str(REPO), str(checkout)],
                cwd=workspace)
    if rc or not checkout.is_dir():
        print("\nCLEAN INSTALL: FAIL (clone failed)")
        return 1
    run(["git", "log", "--oneline", "-1"], cwd=checkout)

    print("\n--- STEP 2: brand new venv ---")
    rc, _ = run([sys.executable, "-m", "venv", str(venv)], cwd=workspace)
    if rc:
        print("\nCLEAN INSTALL: FAIL (venv creation)")
        return 1
    run([str(py), "-m", "pip", "install", "--upgrade", "--quiet", "pip"], cwd=checkout)

    print("\n--- STEP 3: the documented install recipe ---")
    for pip_args in INSTALL:
        rc, _ = run([str(py), "-m", "pip"] + pip_args, cwd=checkout)
        if rc:
            failures.append("pip " + " ".join(pip_args))

    print("\n--- STEP 4: import the server, and REQUIRE the jobcore engine ---")
    rc, out = run([str(py), "-c", IMPORT_PROBE], cwd=checkout,
                  env={"JOBHUNT_CONFIG": ":none:"})
    if rc:
        failures.append("import probe")
    elif "scoring ENGINE = jobcore" not in out:
        # A hard failure, not a note. There is no second scorer to fall back to
        # any more, and an install whose scores are not comparable with the
        # naukri and uplers boards is a broken install, not a variant of one.
        failures.append("scoring engine is not jobcore")
        print("FAIL: this install did not resolve jobcore. Scores from it would "
              "not be comparable with any other board -- if they existed at all.")

    print("\n--- STEP 5: what a resolve TODAY actually picks ---")
    run([str(py), "-m", "pip", "list", "--format=freeze"], cwd=checkout)

    print("\n--- STEP 6: the suite ---")
    rc, out = run([str(py), "-m", "pytest"], cwd=checkout,
                  env={"JOBHUNT_CONFIG": ":none:"})
    if rc:
        failures.append("pytest (exit %d)" % rc)

    print("\n" + "=" * 78)
    print("pytest summary line (verbatim): %s" % summary_line(out))
    print("failed steps: %s" % (", ".join(failures) if failures else "none"))
    print("CLEAN INSTALL: %s" % ("FAIL" if failures else "PASS"))
    print("=" * 78)

    if not failures and not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)
        print("workspace removed (pass --keep to inspect it)")
    else:
        print("workspace kept at %s" % workspace)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
