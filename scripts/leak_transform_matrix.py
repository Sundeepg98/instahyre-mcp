"""The leak-transform grid: 9 transforms x 6 entry-point shapes, measured.

WHAT THIS ANSWERS
-----------------
"Does our credential-leak walker actually fire?" is not answerable by reading
it. This builds a payload that ECHOES a planted credential through each of nine
transforms, wraps it in each of six payload shapes the auth tools really use,
and runs the live walker over every cell. A cell is reported LEAK when the
walker says CLEAN -- which is to say, the credential shipped and nothing
noticed.

THE POINT THAT KEEPS BEING RE-LEARNED: the encoding does not have to be in the
credential. It can be in the LEAK PATH. A tool that base64s, hex-dumps, splits
or merely logs its output hides a planted marker no matter what shape the
credential itself has, so "our cookie is opaque, there is no encoding step"
protects nothing. Reasoning about exposure is how a clean bill of health gets
issued for a dirty tree; this script exists so nobody has to reason.

MEASURED ON INSTAHYRE, 2026-08-23
---------------------------------
Against the walker as it stood that morning: **18 of 54 cells LEAK**, with
three transforms invisible at EVERY entry point -- ``hex``, ``split`` and
``truncated``. base64, base64url, percent, repr and log-only were already
caught by the widening committed earlier the same day.

After adding the hex spelling and the run scan: **0 of 54**. That pair of
numbers is the whole justification for both changes, and re-running this script
is how the claim stays true.

USAGE
-----
    venv/Scripts/python.exe scripts/leak_transform_matrix.py

Exits non-zero if any cell leaks, so it can be wired into a gate. The same grid
runs as ``TestTheTransformGrid`` in ``tests/test_credential_leak.py``; this
script is the human-readable form and the one to reach for when adding a new
transform, because it prints the map rather than a pass/fail.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import sys
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

from conftest import assert_no_credential  # noqa: E402

#: Credential-SHAPED and credential-LENGTH. A real Django ``sessionid`` is 32
#: lowercase alphanumerics; a ``csrftoken`` is 64 mixed-case. A short or
#: hyphenated marker would survive a 12-character truncation whole and make the
#: walker look stronger than it is -- the grid would show a green cell for a
#: reason that has nothing to do with the detector.
SESSIONID = "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3"
CSRFTOKEN = "Ab3Cd5Ef7Gh9Ij1Kl3Mn5Op7Qr9St1Uv3Wx5Yz7Ab9Cd1Ef3Gh5Ij7Kl9Mn1Op3Q"

assert len(SESSIONID) == 32, "the session marker is not session-shaped"
assert len(CSRFTOKEN) == 64, "the csrf marker is not csrf-shaped"


# --- the nine transforms ----------------------------------------------------
#
# Each returns the payload FRAGMENT an echoing build would produce. They are
# ordinary things real code does, not exotic attacks: serialising, dumping,
# quoting, redacting, logging.


def t_identity(value):
    """The bare value. The case every walker already catches."""
    return {"cookie": value}


def t_b64(value):
    """A serialised jar, or a Basic header."""
    return {"blob": base64.b64encode(value.encode()).decode()}


def t_b64url(value):
    """The URL-safe alphabet, as a token in a query string wears it."""
    return {"token": base64.urlsafe_b64encode(value.encode()).decode()}


def t_hex(value):
    """A hex dump. Shares NO substring with either base64 form or the plaintext."""
    return {"hexed": binascii.hexlify(value.encode()).decode()}


def t_percent(value):
    """Quoted into a URL, a curl line or a form body."""
    return {"url": "https://x/?s=" + urllib.parse.quote(value, safe="")}


def t_repr(value):
    """Through an ``%r`` format, which is how a traceback carries it."""
    return {"traceback": "OSError: %r" % value}


def t_split(value):
    """Halved across two fields. NO field holds the whole credential, so every
    whole-string needle reports CLEAN while both halves ship together."""
    half = len(value) // 2
    return {"prefix": value[:half], "suffix": value[half:]}


def t_truncated(value):
    """A redaction that truncates. Looks responsible and leaks a usable prefix;
    again, no field holds the whole value."""
    return {"redacted": value[:12] + "..."}


def t_log_only(value):
    """Never returned -- passed as a logging ARG. A ``LogRecord`` renders as its
    format string, so the value is invisible to a text search of the log."""
    logger = logging.getLogger("instahyre.leakgrid")
    return [
        logger.makeRecord(
            "instahyre.leakgrid", logging.DEBUG, __file__, 1, "session: %s", (value,), None
        )
    ]


TRANSFORMS = (
    ("identity", t_identity),
    ("b64", t_b64),
    ("b64url", t_b64url),
    ("hex", t_hex),
    ("percent", t_percent),
    ("repr", t_repr),
    ("split", t_split),
    ("truncated", t_truncated),
    ("log_only", t_log_only),
)


class _Opaque:
    """An object that keeps its state off its repr, as most classes do."""

    def __init__(self, payload):
        self.payload = payload

    def __repr__(self):
        return "<Opaque>"


#: The payload SHAPES the auth surface really returns -- flat, nested, in a
#: list, behind an object. A walker that handles one shape and not another is
#: exposed by the columns rather than by the rows.
ENTRY_POINTS = (
    ("auth_status", lambda p: {"authenticated": True, "detail": p}),
    ("session_info", lambda p: {"credential": {"session": p}, "durability": {}}),
    ("logout", lambda p: {"authenticated": False, "problems": [p]}),
    ("reauth", lambda p: {"renewed": True, "record": {"before": p, "after": {}}}),
    ("login_browser", lambda p: {"steps": [{"name": "signin", "result": p}]}),
    ("server_info", lambda p: {"build": _Opaque(p)}),
)


def cell_leaks(transform, wrap) -> bool:
    """True when the walker says CLEAN -- i.e. this cell shipped the credential."""
    inner = transform(SESSIONID)
    # log_only returns records, which are the payload; everything else is a
    # fragment to be wrapped in the entry point's shape.
    payload = inner if isinstance(inner, list) else wrap(inner)
    try:
        assert_no_credential(payload, SESSIONID, CSRFTOKEN)
        return True
    except AssertionError:
        return False


def run_grid():
    """``([(transform, [bool, ...])], leaking, total)``."""
    rows = []
    leaking = 0
    total = 0
    for name, transform in TRANSFORMS:
        cells = []
        for _, wrap in ENTRY_POINTS:
            shipped = cell_leaks(transform, wrap)
            cells.append(shipped)
            leaking += shipped
            total += 1
        rows.append((name, cells))
    return rows, leaking, total


def main() -> int:
    rows, leaking, total = run_grid()
    width = max(len(name) for name, _ in ENTRY_POINTS) + 2
    header = "transform".ljust(14) + "".join(n.ljust(width) for n, _ in ENTRY_POINTS)
    print(header)
    print("-" * len(header))
    for name, cells in rows:
        print(
            name.ljust(14)
            + "".join(("LEAK" if c else "  . ").ljust(width) for c in cells)
        )
    print()
    print("LEAK cells (walker said CLEAN while the credential shipped): %d of %d"
          % (leaking, total))
    blind = [name for name, cells in rows if all(cells)]
    print("transforms invisible at EVERY entry point: %s" % (blind or "none"))
    return 1 if leaking else 0


if __name__ == "__main__":
    raise SystemExit(main())
