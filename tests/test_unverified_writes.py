"""The six writes that were commissioned and deliberately not built.

WHY A TEST AND NOT A NOTE
-------------------------
On 2026-08-23 six write surfaces were commissioned -- a saved-search alert
toggle, referrals, screening questionnaires, a workex PUT, a profile image
upload, support tickets -- and a census of every piece of evidence in this tree
found that NOT ONE of them has a recorded request body. Five trace to a single
table whose own heading says "auth INFERRED from shipped code (NOT probed)",
built by resolving an Angular API_PATHS map against $resource action maps: a
technique that yields path strings and action names and cannot yield a body.
No POST has ever been sent to Instahyre from this codebase, so there is no
measured status code for any of them either.

The finding is written into ``constants.UNVERIFIED_WRITE_SURFACES``. This file
is what stops it decaying into prose nobody runs. Two things are pinned:

1. **None of those paths is reachable.** A future session that adds a write to
   one of them without first recording the contract fails here, by path, with
   the reason printed.
2. **The register still says what is missing.** An entry emptied to a shrug
   would pass a "the register exists" assertion while certifying nothing, so
   the assertion is on the CONTENT.

THE DISTINCTION FROM ``FORBIDDEN_ENDPOINTS`` IS LOAD-BEARING and is asserted
below. Those two bulk-apply paths must never be built at any evidence level --
one call is an irreversible mass-apply. These six are unbuilt PENDING
MEASUREMENT: capture the real request in a signed-in browser, record the body,
and they become ordinary work. Collapsing the two registers would either
permanently ban six buildable things or quietly unban a mass-apply.

The scanner carries its own control: it is run against a synthetic source that
posts to a registered path and asserted to catch it. A scanner that has never
been shown failing certifies nothing.
"""

from __future__ import annotations

import ast

import pytest

from instahyre_server import constants as C
from test_inbound_safety import package_sources

#: The path fragments that identify each unverified surface on the wire. Kept
#: as fragments rather than full URLs because the evidence does not record a
#: full URL for any of them -- which is the entire finding. A fragment cannot
#: be matched too narrowly and miss the thing it is guarding against.
UNVERIFIED_PATH_FRAGMENTS = {
    "referrals": "/refer/referral",
    "screening_questionnaires": "/questionnaires/answer",
    "support_tickets": "/support",
}


def string_constants(sources):
    """Every string literal in ``sources`` that is not a docstring or comment.

    Docstrings are excluded deliberately: this file's own guard has to be able
    to NAME the paths it forbids, and so does ``constants.py``. A scanner that
    counted prose would fire on the register that documents it, which is the
    fastest way to get a guard deleted.
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    out = []
    for name, text in sources.items():
        tree = ast.parse(text, filename=name)
        docstrings = set()
        for node in ast.walk(tree):
            if not isinstance(node, holders):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                out.append((name, node.lineno, node.value))
    return out


def looks_like_a_path(literal: str) -> bool:
    """True for a literal that could be sent as a URL path, false for prose.

    A URL path starts with ``/`` and carries no whitespace. Prose that MENTIONS
    a path -- which is exactly what ``UNVERIFIED_WRITE_SURFACES`` is made of --
    fails both halves.

    This discriminator replaced a docstring-exclusion rule that was not enough:
    the register's reasons are dict VALUES, not docstrings, so excluding
    docstrings left the guard firing on the very register that documents it.
    Measured on 2026-08-23, and it is the false-positive case that decides
    whether a guard survives -- a scanner that fires on its own documentation
    gets muted, and a muted scanner is a disconnected one.
    """
    stripped = literal.strip()
    return stripped.startswith("/") and not any(c.isspace() for c in stripped)


def unverified_paths_in(sources):
    """``(surface, file, line, literal)`` for every registered path found.

    Only PATH-SHAPED literals count; see :func:`looks_like_a_path`.
    """
    hits = []
    for file_name, lineno, literal in string_constants(sources):
        if not looks_like_a_path(literal):
            continue
        for surface, fragment in UNVERIFIED_PATH_FRAGMENTS.items():
            if fragment in literal:
                hits.append((surface, file_name, lineno, literal))
    return hits


# ---------------------------------------------------------------------------
# 1. The register says something
# ---------------------------------------------------------------------------


class TestTheRegister:

    def test_it_names_all_six_surfaces(self):
        assert set(C.UNVERIFIED_WRITE_SURFACES) == {
            "saved_search_alert_toggle",
            "referrals",
            "screening_questionnaires",
            "workex_put",
            "profile_image",
            "support_tickets",
        }

    def test_every_entry_says_what_is_MISSING_not_merely_that_it_is_unbuilt(self):
        """A register whose entries read "not built yet" would pass a shape
        check and tell the next session nothing. Each one has to name the gap,
        because the gap is the instruction: measure THIS, then build."""
        for surface, reason in C.UNVERIFIED_WRITE_SURFACES.items():
            assert len(reason) > 120, "%s: too short to carry a finding" % surface
            lowered = reason.lower()
            # The vocabulary of a missing contract. Widened once, on evidence:
            # the profile-image entry names the gap precisely -- multipart vs
            # JSON, the field name, the content type -- and matched none of the
            # first five words. Bending that entry to fit the checklist would
            # have made the prose worse to make the test pass, which is the
            # wrong direction; the checklist was the weaker instrument.
            assert any(
                word in lowered
                for word in (
                    "body",
                    "method",
                    "no read",
                    "shape",
                    "contract",
                    "field name",
                    "content type",
                )
            ), "%s: does not say what evidence is missing" % surface

    def test_the_referral_entry_says_who_it_would_mail(self):
        """The one the operator explicitly approved, and the one where the gap
        matters most: send_invites reaches real people permanently. The reason
        it was not built has to survive being re-read months later by someone
        who remembers only the approval."""
        reason = C.UNVERIFIED_WRITE_SURFACES["referrals"].lower()
        assert "third parties" in reason
        assert "unsend" in reason or "undo" in reason

    def test_it_is_kept_apart_from_the_permanent_ban(self):
        """FORBIDDEN_ENDPOINTS means "never, at any evidence level". This
        register means "not yet, on this evidence". Merging them would either
        permanently ban six buildable things or quietly unban a mass-apply."""
        for fragment in UNVERIFIED_PATH_FRAGMENTS.values():
            assert not any(fragment in path for path in C.FORBIDDEN_ENDPOINTS)
        assert C.FORBIDDEN_ENDPOINTS, "the permanent ban must still hold entries"

    def test_the_bulk_apply_ban_is_untouched(self):
        """The register above must not have diluted the thing that actually
        cannot be built. Pinned by count and by content."""
        assert len(C.FORBIDDEN_ENDPOINTS) == 2
        assert all("apply_bulk" in path for path in C.FORBIDDEN_ENDPOINTS)


# ---------------------------------------------------------------------------
# 2. None of it is reachable
# ---------------------------------------------------------------------------


class TestNoneOfThemIsWiredUp:

    def test_no_module_names_an_unverified_path(self):
        """The tripwire. If this fails, someone added a route whose request
        shape nobody has measured -- and on this platform that is a write that
        succeeds and does something nobody chose."""
        hits = unverified_paths_in(package_sources())
        assert not hits, (
            "an unverified write path reached the package:\n  "
            + "\n  ".join(
                "%s -> %s:%d %r\n      REASON IT IS UNBUILT: %s"
                % (surface, name, line, literal, C.UNVERIFIED_WRITE_SURFACES[surface])
                for surface, name, line, literal in hits
            )
        )

    def test_the_scanner_catches_a_registered_path__CONTROL(self):
        """Fed a module that posts to the referral resource. The scanner has to
        see it, or the test above is a green light with nothing behind it."""
        offending = {
            "rogue.py": (
                "def send(http):\n"
                "    return http.post('/candidate_misc/refer/referral/',\n"
                "                     json={'emails': ['someone@example.com']})\n"
            )
        }
        hits = unverified_paths_in(offending)
        assert [h[0] for h in hits] == ["referrals"]

    def test_the_scanner_catches_each_registered_fragment__CONTROL(self):
        """One control per fragment, so a fragment that stopped matching would
        be caught rather than reported as clean along with the rest."""
        for surface, fragment in UNVERIFIED_PATH_FRAGMENTS.items():
            source = {"rogue.py": "PATH = '%s/'\n" % fragment}
            found = [h[0] for h in unverified_paths_in(source)]
            assert surface in found, "the scanner is blind to %s" % surface

    def test_the_scanner_ignores_prose_that_merely_names_a_path__CONTROL(self):
        """The false-positive half, and the one that decides whether this guard
        survives. ``constants.py`` documents these paths in a comment and this
        file names them in a docstring; a scanner that fired on either would be
        muted within a week, and a muted scanner is a disconnected one."""
        documented = {
            "rogue.py": (
                '"""We do not call /candidate_misc/refer/referral/ -- no body is known."""\n'
                "VALUE = 1\n"
            )
        }
        assert unverified_paths_in(documented) == []

    def test_the_scanner_ignores_the_register_it_is_built_from__CONTROL(self):
        """The specific false positive this guard actually produced.

        The register's reasons are dict VALUES that name the paths they
        explain, so a docstring-only exclusion left the scanner firing on
        ``constants.py`` itself and reporting the documentation as the offence.
        Both halves are asserted: the prose is ignored, AND the same path is
        still caught when it appears as a path.
        """
        prose = C.UNVERIFIED_WRITE_SURFACES["referrals"]
        assert "/refer/referral" in prose, (
            "this control needs a register entry that names its own path"
        )
        assert unverified_paths_in({"rogue.py": "R = %r\n" % prose}) == []
        assert unverified_paths_in(
            {"rogue.py": "R = '/candidate_misc/refer/referral/'\n"}
        )

    def test_a_path_with_a_space_is_not_a_path__CONTROL(self):
        """The discriminator, isolated. A sentence that begins with the path is
        still a sentence."""
        assert not looks_like_a_path("/refer/referral is not called here")
        assert looks_like_a_path("/candidate_misc/refer/referral/")

    def test_the_real_package_is_what_was_scanned__CONTROL(self):
        """A scan of nothing passes. This asserts the source map is real and
        substantial, so a broken ``package_sources`` cannot present itself as a
        clean bill of health."""
        sources = package_sources()
        assert len(sources) > 10
        assert "constants.py" in sources and "server.py" in sources
        assert string_constants(sources), "no string literals found at all"


# ---------------------------------------------------------------------------
# 3. The surfaces that DO exist are unaffected
# ---------------------------------------------------------------------------


class TestTheBuiltWritesStillWork:

    def test_the_skill_write_is_not_caught_by_this_guard(self):
        """A guard that swept up the writes this server legitimately makes
        would be a regression dressed as caution. The skill model is measured,
        replacement-set semantics and all, and must stay reachable."""
        sources = package_sources()
        assert any(
            C.EP_SKILL_MODEL in literal for _, _, literal in string_constants(sources)
        )
        assert not unverified_paths_in(sources)

    @pytest.mark.parametrize("path", sorted(C.FORBIDDEN_ENDPOINTS))
    def test_a_forbidden_path_is_still_absent_from_every_call_site(self, path):
        """Restated here because this file is where a future reader looks for
        "what may this package not send". The write-surface AST scan in
        test_inbound_safety is the primary guard; this is a second reading of
        the same rule from the other direction."""
        for name, _, literal in string_constants(package_sources()):
            if literal == path:
                assert name == "constants.py", (
                    "the forbidden path is spelled outside the register: %s" % name
                )
