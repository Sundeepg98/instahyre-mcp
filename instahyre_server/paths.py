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

# DIRECT, not read off the alias above, and the difference is not cosmetic.
# ``tests/test_jobcore_pin.py`` decides what this repo needs from jobcore by
# parsing these import statements, and only the dotted form
# ``from jobcore.paths import ...`` declares a dependency on the SUBMODULE.
# ``from jobcore import paths as _jobcore_paths`` registers a name on the
# PACKAGE instead, and the attribute reads off that alias are then skipped
# when the submodule is absent at the pinned commit. Until 2026-08-22 this
# declaration lived in ``policy.py``; when the leak fix moved that call in
# here it had to come with it, or the pin check would have quietly stopped
# reporting ``jobcore.paths`` as a missing MODULE.
from jobcore.paths import relativise_known

__all__ = ["CHECKOUT_ROOT", "display_path", "repr_spelling", "relativise_prose"]

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


def repr_spelling(raw: Any) -> str:
    r"""How ``OSError.__str__`` spells a filename: ``repr()`` minus its quotes.

    CPython renders an ``OSError``'s ``filename`` through ``%R``, so a Windows
    path arrives in the message with every separator DOUBLED::

        OSError(13, "Permission denied", r"C:\Users\Dell\config\jobhunt.json")
        str(...) -> [Errno 13] Permission denied: 'C:\\Users\\Dell\\config\\jobhunt.json'

    That is the same path, spelled differently -- and an exact-substring
    scrubber looking for the single-separator form finds nothing in it and
    passes the payload through as clean. Measured on 2026-08-22 against
    ``instahyre_config()``: ``config_status`` carried a correctly relativised
    ``../../config/jobhunt.json`` and this machine's full absolute layout, in
    the SAME SENTENCE, because the second half came from ``{exc}``.

    On POSIX the two spellings are identical for any ordinary path, so this
    returns its input unchanged there and the extra needle costs nothing.
    """
    return repr(str(raw))[1:-1]


def relativise_prose(text: Any, known) -> Any:
    r"""``relativise_known``, handed BOTH spellings of every path it is given.

    NOT A SECOND RENDERER, and not the hand post-processing that was deleted on
    2026-08-22. The rendering rule is still exactly one function
    (:func:`display_path`) and the substitution is still exact -- only strings
    the caller already KNOWS it holds are replaced. The single change is that
    each needle is offered in its ``repr`` spelling as well, because that is a
    spelling of the same path that jobcore's own pass structurally cannot see:
    ``Loaded.known_paths`` holds the path as the filesystem spells it, and the
    text it is scrubbing spells it as ``repr`` does.

    Deliberately NOT a hunt for path-shaped text. A regex that went looking for
    ``C:\`` or for slashes in arbitrary prose would eventually eat an
    ``instahyre.com`` API route or a quoted URL, which is how a scrubber does
    more damage than the leak it was written for.

    Both spellings map to the SAME rendering, resolved through a lookup rather
    than by un-escaping the needle: collapsing ``\\`` to ``\`` would corrupt a
    UNC path, whose leading ``\\`` is not an escape at all.

    NOT REDUNDANT ONCE THE PIN CARRIES jobcore 6acc7e6, which closes the same
    class upstream. It will read as dead weight to the next person here, and it
    is not: unlike the sibling servers this repo has NO server-wide boundary
    scrubber over tool results, so this function is the ONLY thing between a
    composed error message and the wire. jobcore fixing its own pass narrows
    what reaches here; it does not cover the sites jobcore never sees, which is
    why ``profile_write.load_snapshot`` needed this too. Retire it only if a
    boundary scrubber is added, and then deliberately.

    Args:
        text: any value; non-strings are returned untouched.
        known: the path strings this caller knows it may have emitted.
    """
    renderings: dict[str, str] = {}
    for raw in known or ():
        if not raw:
            continue
        raw = str(raw)
        rendered = display_path(raw)
        if not rendered:
            continue
        renderings[raw] = str(rendered)
        # Same rendering, second spelling. A no-op on POSIX, where the two
        # spellings of an ordinary path are byte-identical.
        renderings[repr_spelling(raw)] = str(rendered)
    if not renderings:
        return text
    # ``relativise_known`` replaces longest needle first, which is what keeps a
    # parent directory from being substituted inside its own child's path --
    # and it is what makes the repr spelling safe to add, since the doubled
    # form of a path is strictly longer than the single form.
    return relativise_known(
        text, known=renderings.keys(), render=renderings.__getitem__
    )
