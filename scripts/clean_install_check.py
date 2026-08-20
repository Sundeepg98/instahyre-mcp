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
that is what a fresh clone or a CI runner actually has.
`instahyre_server/scoring.py` falls back to `../../jobcore/src` and then to a
small local scorer, so a developer box (where jobcore IS a sibling) reports
`scoring ENGINE = jobcore` while a fresh clone reports `local-fallback`. Step 4
prints which one this install got, so the difference is visible instead of
being discovered through scores that quietly stopped being comparable.

WHAT IT DOES
------------
  1. `git clone` this repo into a throwaway workspace -- COMMITTED state only,
     so nothing here reads or disturbs your working tree
  2. build a brand new venv
  3. run the documented install recipe from README.md ("Install")
  4. import the server, and report the scoring engine it resolved to
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
INSTALL = [
    ["install", "-r", "requirements.txt"],
]

IMPORT_PROBE = (
    "import instahyre_server, instahyre_server.server, instahyre_server.scoring as sc; "
    "import importlib.metadata as md; "
    "print('instahyre_server', instahyre_server.__version__, 'on fastmcp', md.version('fastmcp')); "
    "print('scoring ENGINE =', sc.ENGINE)"
)


def run(cmd, cwd, timeout=2400):
    """Run one command, echo it and its output verbatim, return (rc, output)."""
    print("\n$ %s\n  (cwd=%s)" % (subprocess.list2cmdline(cmd), cwd), flush=True)
    started = time.time()
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
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

    print("\n--- STEP 4: import the server, and name the scoring engine ---")
    rc, out = run([str(py), "-c", IMPORT_PROBE], cwd=checkout)
    if rc:
        failures.append("import probe")
    elif "local-fallback" in out:
        # Not a failure: it is the documented behaviour of a sibling-free
        # checkout, and every scoring tool reports `scoring_engine`. It is
        # printed loudly because scores from this install are NOT comparable
        # with a jobcore-backed one, and that is easy to forget.
        print("NOTE: no jobcore here, so scores come from the local fallback and are "
              "not comparable with jobcore-backed boards. `pip install -e ../jobcore` "
              "in a checkout that has the sibling.")

    print("\n--- STEP 5: what a resolve TODAY actually picks ---")
    run([str(py), "-m", "pip", "list", "--format=freeze"], cwd=checkout)

    print("\n--- STEP 6: the suite ---")
    rc, out = run([str(py), "-m", "pytest"], cwd=checkout)
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
