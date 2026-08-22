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

TWO REPOSITORIES, TWO STAMPS
----------------------------
This server's scoring arithmetic is jobcore's, installed editable from a
sibling checkout, so the two move independently. A stale jobcore is exactly as
invisible as a stale server and shifts every fit score just as silently -- with
this server's own commit matching disk the whole time. Folding them into one
stamp would hide precisely that case, so jobcore is stamped separately.

NOTHING HERE MAY BREAK SERVER IMPORT. Every git call inside jobcore is bounded
by a timeout and every failure degrades to a ``source="unknown"`` stamp that
says which failure it was. An unknown stamp is a value; a plausible-looking
hash nobody measured is the defect this module exists to prevent.
"""

from __future__ import annotations

import jobcore
from jobcore import buildinfo as _jobcore_buildinfo

from .paths import CHECKOUT_ROOT

__all__ = ["BUILD", "JOBCORE_BUILD", "CLOCK", "build_block"]

#: The commit THIS server was started from. Frozen at import: it describes the
#: past, and a stamp that moved would answer a different question than the one
#: a reader is asking.
BUILD = _jobcore_buildinfo.stamp(CHECKOUT_ROOT)

#: The commit the installed jobcore was at when this process imported it.
#: Stamped from ``jobcore.__file__`` rather than a guessed sibling directory,
#: so it follows the editable install wherever it actually points -- and
#: honestly reports ``unknown`` under a normal wheel, where there is no work
#: tree to read and no true answer to give.
JOBCORE_BUILD = _jobcore_buildinfo.stamp(jobcore.__file__)

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
