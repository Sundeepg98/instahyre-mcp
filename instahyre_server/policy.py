"""Where this server's scoring numbers come from -- the one module here that reads a file.

THE SPLIT, AND WHY IT IS DRAWN HERE
-----------------------------------
``jobcore``'s scoring path never touches the filesystem. That is deliberate and
it is enforced by a test in that repo (``test_independence`` runs a clean
interpreter with the cwd somewhere else and asserts a score of 100): if the
scorer read a file, the same job would score differently on two machines and
jobcore would stop being a library.

So the file-reading happens *here*, at the edge, and the result is INJECTED
downwards. One snapshot is bound at tool entry and carried through the whole
call, because half a ranking scored under old weights and half under new is
worse than either.

WHAT IT READS
-------------
``jobcore.config.current()`` looks for ``jobhunt.json`` -- ``$JOBHUNT_CONFIG``,
then ``$JOBHUNT_HOME``, then a walk up from *this file* toward a
``.jobhunt-root`` marker, then ``~/.jobhunt/``. It never raises: a missing file
means the built-in defaults, a malformed one means the built-in defaults plus a
loud ``config_error``. The defaults are exactly the literals this server scored
with before the config existed, so a checkout with no file behaves identically.

WHAT IT CANNOT DO
-----------------
Nothing in this module can grant autonomous authority. This server has no
agent, no scheduler and no unattended apply path -- ``instahyre_apply`` requires
``confirm=True`` from a human every single time, in source, not in config. On
top of that, jobcore refuses to LOAD its Tier C keys from the file at all
(``Loaded.tier_c_refusals`` names any that were attempted), so a config write
cannot reach an apply selector on any server in the family.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from jobcore import config as _jobcore_config
from jobcore.config import Loaded
from jobcore.paths import relativise_known

from .paths import display_path

__all__ = [
    "SERVER",
    "SECTIONS",
    "current",
    "invalidate_cache",
    "scoring_args",
    "report",
    "summary",
    "Loaded",
]

#: This server's name in the ``servers`` block of the config file.
SERVER = "instahyre"

#: What ``instahyre_config(section=...)`` will narrow to.
SECTIONS = ("candidate", "scoring", "server", "provenance")

#: The walk-up starts from THIS file, not from jobcore's. jobcore cannot know
#: who imported it, and starting from its own location only works by luck --
#: true under an editable install, false under a normal wheel, where the walk
#: finds nothing and the server silently runs on defaults forever.
_START = Path(__file__)


def current(start: Optional[Path] = None) -> Loaded:
    """The effective policy right now.

    Re-read only when the file's BYTES changed (sha256, not mtime -- 12 atomic
    replaces produced only 8 distinct ``(mtime_ns, size)`` pairs when that was
    measured, and a ``0.6`` -> ``0.8`` edit does not change the size at all).
    So editing the file and asking again moves the number, with no restart.
    """
    return _jobcore_config.current(start or _START)


def invalidate_cache() -> None:
    """Drop the loader's cached snapshots. Tests and an explicit reload use this."""
    _jobcore_config.invalidate_cache()


def scoring_args(loaded: Optional[Loaded] = None) -> dict:
    """The three kwargs :func:`instahyre_server.scoring.score_job` needs.

    Keeps the call sites from having to know which attributes of a snapshot the
    scorer wants -- and, more to the point, makes "forgot to pass the policy"
    a visibly missing ``**policy.scoring_args(...)`` rather than a silently
    default-scored result.
    """
    if loaded is None:
        loaded = current()
    return {
        "policy": loaded.scoring,
        "candidate": loaded.candidate,
        "policy_rev": loaded.policy_rev,
    }


def summary(loaded: Optional[Loaded] = None) -> dict:
    """The small provenance block a tool result carries alongside its scores."""
    if loaded is None:
        loaded = current()
    # BOTH fingerprints, and they are not interchangeable. ``policy_hash``
    # covers scoring AND candidate -- the whole effective config. A scored
    # result can only vouch for the arithmetic, so it stamps ``scoring_hash``;
    # printing that one here is what lets a stored score be matched back to the
    # config that produced it. Comparing a result's stamp against
    # ``policy_hash`` reports a difference that does not exist.
    #
    # ``config_source`` is RELATIVISED, not printed raw and not dropped. A live
    # sweep on 2026-08-21 found this machine's full layout here and in
    # ``instahyre_config()``; deleting the field would have passed a leak scan
    # while destroying the only thing it is for, which is telling a reader
    # which file produced these numbers. See :mod:`instahyre_server.paths`.
    out = {
        "policy_rev": loaded.policy_rev,
        "policy_hash": loaded.policy_hash,
        "scoring_hash": loaded.scoring_hash,
        "config_source": display_path(loaded.source),
    }
    # Only surfaced when there is something to say. A quiet field that is
    # always ``null`` trains a reader to stop looking at it.
    #
    # RENDERED THROUGH ``_prose``, and this is the field that made the first
    # pass of this fix incomplete. ``config_error`` is not a path, it is a
    # SENTENCE jobcore composed with the path already inside it -- and this
    # block is spread into ``instahyre_rank_jobs`` and
    # ``instahyre_inbound_digest``, so one unparseable config file put the
    # machine's layout into two SCORING results that name no file anywhere in
    # their own source.
    if loaded.config_error:
        out["config_error"] = _prose(loaded, loaded.config_error)
    if loaded.tier_c_refusals:
        out["tier_c_refusals"] = list(loaded.tier_c_refusals)
    if loaded.external_edit:
        out["external_edit"] = dict(loaded.external_edit)
    return out


def _prose(loaded: Loaded, text: Any) -> Any:
    """Render any absolute path that jobcore BAKED INTO a composed message.

    Renaming a path field does not reach a path that was never in a field.
    jobcore's loader composes ``f"{path} is not valid JSON: {exc}"``,
    ``f"cannot read {path}: {exc}"`` and ``f"could not append to {ledger}:
    {exc}"``, so the leak survives in prose after every path FIELD is clean.
    Measured on 2026-08-22: one unparseable ``jobhunt.json`` and the full
    machine layout was back in four places at once.

    Substitution is EXACT -- only strings the snapshot already knows it holds
    are replaced, never a regex hunt for path-shaped text, which would
    eventually eat a URL or a quoted API route.
    """
    return relativise_known(text, known=loaded.known_paths, render=display_path)


def report(section: Optional[str] = None, loaded: Optional[Loaded] = None) -> dict:
    """The payload ``instahyre_config()`` returns.

    Args:
        section: narrow to one of :data:`SECTIONS`. ``None`` returns everything.
        loaded: an already-bound snapshot; omitted means read now.
    """
    if loaded is None:
        loaded = current()
    # ``display`` is handed DOWN rather than the result post-processed here.
    # jobcore then renders ``source``, ``searched`` AND the prose of
    # ``config_status`` / ``config_error`` / ``ledger_error`` in one place,
    # using the anchor this server supplies. The hand-maintained version of
    # this that lived here rendered three fields and missed the other three,
    # because a list of fields to keep in sync is a list that falls behind.
    full: dict[str, Any] = loaded.report(server=SERVER, display=display_path)
    if section is None:
        return full
    key = str(section).strip().lower()
    if key not in SECTIONS:
        raise ValueError(
            "unknown section %r; expected one of %s"
            % (section, ", ".join(SECTIONS))
        )
    # A CONFIG READOUT, not a result stamp: this is what ``instahyre_config()``
    # hands back, and the unnarrowed branch above returns jobcore's own report,
    # which prints both fingerprints. Narrowing must not drop one -- a section
    # view that carried only ``policy_hash`` could not answer "is the score I
    # stored yesterday comparable with this config", which is the single
    # question the two-hash split exists to make answerable.
    return {
        "section": key,
        key: full.get(key),
        "policy_rev": full.get("policy_rev"),
        "policy_hash": full.get("policy_hash"),
        "scoring_hash": full.get("scoring_hash"),
        "source": full.get("source"),
    }
