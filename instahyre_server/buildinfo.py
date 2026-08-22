"""What code THIS process is running -- resolved once, at import, then frozen.

WHY
---
A fix committed to disk changes nothing for a server that is already up. On
2026-08-21 that cost real time: a stale process was diagnosed as a regression
and a fix was dispatched for a bug that was already fixed on disk, and the same
class recurred three more times the same day. Every check available then was a
BEHAVIOURAL FINGERPRINT -- does this field appear, is that count right -- and a
fingerprint cannot separate "the fix is absent" from "the fix is present in a
process that predates it". Those two need opposite responses: one wants a
patch, the other wants a restart.

``instahyre_server_info()`` now answers it outright, by reading the constants
below.

THE FREEZE IS THE CONTRACT, NOT AN OPTIMISATION
-----------------------------------------------
These are module-level constants, resolved at import, and the tool READS them.
It must never re-resolve. A per-call ``git rev-parse`` run from a stale process
reports the NEW commit sitting on disk -- strictly worse than reporting
nothing, because it reads as confirmation that the fix is loaded and the thing
it confirms is false. ``jobcore.buildinfo.stamp`` memoises for the same reason;
holding the result here as well makes the intent visible at the call site and
keeps the request path free of any git call at all.

``tests/test_buildinfo.py::test_the_stamp_is_not_re_resolved_per_call`` is the
guard, and it is built so it cannot pass against a re-resolving build: it makes
``subprocess.run`` raise, so an implementation that shells out on the request
path dies rather than quietly returning a fresh answer.

TWO REPOSITORIES, TWO STAMPS -- AND TWO WAYS TO BE INSTALLED
------------------------------------------------------------
This server's scoring arithmetic is jobcore's, so the two move independently. A
stale jobcore is exactly as invisible as a stale server and shifts every fit
score just as silently -- with this server's own commit matching disk the whole
time. Folding them into one stamp would hide precisely that case, so jobcore is
stamped separately.

The two are also installed differently depending on where this runs, and the
stamp has to survive both. On the operator's box jobcore is an EDITABLE install
from a sibling checkout, so it has a work tree and a commit. On CI, and on any
deployment, pip installs it from a git URL into site-packages, which is not a
work tree: there is no commit to report and asking for one yields ``unknown``.
``self_stamp()`` reports the commit where one exists and the installed VERSION
where one does not, so the echo stays useful in the environment where nobody
can just run ``git log``. Its ``source`` field says which answer you are
looking at: ``"git"``, ``"package"``, or ``"unknown"``.

NOTHING HERE MAY BREAK SERVER IMPORT. Every git call inside jobcore is bounded
by a timeout and every failure degrades to a ``source="unknown"`` stamp that
says which failure it was. An unknown stamp is a value; a plausible-looking
hash nobody measured is the defect this module exists to prevent.
"""

from __future__ import annotations

from jobcore import buildinfo as _jobcore_buildinfo

from .paths import CHECKOUT_ROOT

__all__ = ["BUILD", "JOBCORE_BUILD", "CLOCK", "build_block"]

#: The commit THIS server was started from. Frozen at import: it describes the
#: past, and a stamp that moved would answer a different question than the one
#: a reader is asking.
BUILD = _jobcore_buildinfo.stamp(CHECKOUT_ROOT)

#: What the installed jobcore IS -- commit where there is a work tree, released
#: version where there is not.
#:
#: ``self_stamp()``, not ``stamp(jobcore.__file__)``, and CI is what taught the
#: difference. The old call answered correctly on this box, where jobcore is an
#: editable install from a sibling checkout, and answered ``unknown`` on the
#: runner, where pip installs it from a git URL into site-packages -- which is
#: not a work tree, so "which commit" genuinely has no answer there. A version
#: echo that goes silent on a DEPLOYED server is silent in exactly the
#: environment where nobody can run ``git log``, which is the one place it was
#: built for. jobcore now answers the question that DOES have an answer there:
#: ``source="package"`` and the installed version.
JOBCORE_BUILD = _jobcore_buildinfo.self_stamp()

#: When this process came up. Deliberately NOT part of the frozen stamps:
#: uptime is derived fresh on every call, because a cached uptime is a lie that
#: grows. The pid is what identifies WHICH process is the stale one when two
#: are somehow running.
CLOCK = _jobcore_buildinfo.ProcessClock()


def build_block(extra: dict | None = None) -> dict:
    """The ``build`` block for ``instahyre_server_info()``.

    Reads the constants above and resolves nothing. ``extra`` merges in
    whatever else the caller can pin without this module having to know about
    it.
    """
    block = {
        "code": BUILD.as_dict(),
        "jobcore": JOBCORE_BUILD.as_dict(),
        "process": CLOCK.as_dict(),
    }
    if extra:
        block.update(extra)
    return block
