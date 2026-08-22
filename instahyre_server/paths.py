"""Paths a caller can act on, that are not this machine's absolute layout.

WHAT WAS MEASURED
-----------------
On 2026-08-21 a live call to ``instahyre_config()`` against the running server
returned this machine's directory layout in three fields at once::

    "source":        "D:\\Sundeep\\projects\\job-hunting\\config\\jobhunt.json"
    "config_status": "loaded from D:\\Sundeep\\projects\\job-hunting\\config\\jobhunt.json"
    "searched":      ["D:\\Sundeep\\projects\\job-hunting\\config\\jobhunt.json"]

That is wrong twice over: it publishes the operator's layout into any shared
transcript or future public release, and it is paid for in tokens on every
response that carries it. The sibling Naukri server already returns
``"../../config/jobhunt.json"`` for the same call.

RELATIVISE, DO NOT DELETE
-------------------------
The obvious fix -- drop the field -- trades one defect for another. "Where is
the config file even?" is a documented use of these tools: ``instahyre_config``
points a confused reader at ``searched`` to answer "why is my file not being
read". A ``null`` there is a field that looks like an answer and is not one,
which is the same class of defect as the leak.

WHY THIS MODULE EXISTS AT ALL
-----------------------------
``jobcore.paths.display_path`` is the canonical implementation and it takes the
anchor as an argument, because jobcore cannot know where its consumer lives --
a path rendered relative to the *library* would be meaningless to the reader.
This module binds that argument ONCE, to this checkout, so the four call sites
(``policy``, ``server``, ``auth``, and anything added later) cannot drift onto
three different anchors and render the same file three different ways.

It mirrors ``jobcore.paths`` in name deliberately: a reader looking for path
rendering greps for ``paths``, and the sibling module ``buildinfo`` mirrors
``jobcore.buildinfo`` for the same reason. ``buildinfo`` imports
:data:`CHECKOUT_ROOT` from here rather than recomputing it -- the directory a
build stamp is taken FROM and the directory paths are displayed AGAINST are the
same directory, and defining it twice is how they stop being the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from jobcore import paths as _jobcore_paths

__all__ = ["CHECKOUT_ROOT", "display_path"]

#: This checkout's root -- the anchor every displayed path is measured against.
#: ``instahyre_server/paths.py`` -> ``instahyre_server`` -> the repository, so
#: the shared ``jobhunt.json`` two levels above it renders as
#: ``../../config/jobhunt.json``.
#:
#: THE ONE DEFINITION. Every module that renders a path imports this rather
#: than computing its own ``parent.parent``: a file moved one directory deeper
#: would otherwise start rendering against a root one level off, and the
#: symptom would be a path that still looks entirely plausible.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


def display_path(raw: Any) -> Optional[str]:
    """Render ``raw`` with no drive letter and no absolute root.

    Three forms, in order: relative to :data:`CHECKOUT_ROOT`, then
    home-anchored ``~/...``, then the trailing few components as ``.../a/b/c``.
    ``None`` and ``""`` pass straight through, so "no file was found" stays
    distinguishable from "a file at the filesystem root".

    The last form matters more than it looks: its predecessor was the bare
    basename, which collapses every entry of a ``searched`` list to the
    identical string ``jobhunt.json`` -- strictly worse than saying nothing,
    because the list stops distinguishing the places that were tried. See
    ``jobcore.paths`` for the CI failure that found it.
    """
    return _jobcore_paths.display_path(raw, anchor=CHECKOUT_ROOT)
