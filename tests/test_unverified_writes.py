"""The writes that were commissioned: four now measured, two still not.

WHY A TEST AND NOT A NOTE
-------------------------
On 2026-08-23 six write surfaces were commissioned -- a saved-search alert
toggle, referrals, screening questionnaires, a workex PUT, a profile image
upload, support tickets -- and a census of every piece of evidence in this tree
found that NOT ONE of them had a recorded request body. Five traced to a single
table whose own heading says "auth INFERRED from shipped code (NOT probed)",
built by resolving an Angular API_PATHS map against $resource action maps: a
technique that yields path strings and action names and cannot yield a body.

LATER THE SAME DAY, four of them were measured, by the route this register
itself named. ``scripts/capture_write_contracts.py`` opens the real signed-in
browser, records the request a control WOULD send, and aborts it at the router
before it leaves the machine; and the authenticated-tier JavaScript bundles --
which no earlier pass had downloaded -- carry the call sites the public ones do
not. Those four moved to ``constants.CAPTURED_WRITE_CONTRACTS``, each stamped
with WHICH of the two evidence classes it has, and section 1b pins that.

The two that remain are not leftovers. Each is blocked on something the
capture technique cannot reach: a questionnaire can only be opened by pressing
Apply on a real opportunity, which is the one action this server must never
take; and the workex PUT has no caller in any shipped bundle and no control on
the signed-in profile page, so there is nothing to intercept.

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
import json
import pathlib

import pytest

from instahyre_server import constants as C
from test_inbound_safety import package_sources

#: The path fragments that identify each unverified surface on the wire. Kept
#: as fragments rather than full URLs because the evidence does not record a
#: full URL for any of them -- which is the entire finding. A fragment cannot
#: be matched too narrowly and miss the thing it is guarding against.
UNVERIFIED_PATH_FRAGMENTS = {
    "screening_questionnaires": "/questionnaires/answer",
    "workex_put": "/onboarding_workex",
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

    def test_it_names_the_two_that_are_still_unmeasured(self):
        """Four of the original six came off this register on 2026-08-23 when
        their contracts were captured. The two left are not left by oversight:
        each is blocked on something the capture technique cannot reach, and
        the entries say which."""
        assert set(C.UNVERIFIED_WRITE_SURFACES) == {
            "screening_questionnaires",
            "workex_put",
        }

    def test_nothing_is_in_both_registers(self):
        """A surface is either measured or it is not. An entry in both would
        let a build cite the captured contract while the tripwire still
        believes it is unbuilt -- the two guards would disagree silently."""
        overlap = set(C.UNVERIFIED_WRITE_SURFACES) & set(C.CAPTURED_WRITE_CONTRACTS)
        assert not overlap, "a surface cannot be both measured and unmeasured: %s" % overlap

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

    def test_the_referral_contract_still_says_who_it_would_mail(self):
        """The one the operator explicitly approved, and the one where the
        stakes are highest: send_invites reaches real people permanently.
        Capturing the contract changed what we know, not what it does, and the
        note has to keep saying so -- a reader who arrives months from now
        remembering only the approval must still meet the warning."""
        note = C.CAPTURED_WRITE_CONTRACTS["referrals"]["note"].lower()
        assert "third parties" in note
        assert "unsend" in note or "undo" in note

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
# 1b. The four that were captured on 2026-08-23
# ---------------------------------------------------------------------------

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "write_contracts"


class TestTheCapturedContracts:
    """A captured contract has to carry its evidence class, or it is a claim.

    The whole point of the register these came off is that a body read from a
    JavaScript factory and a body recorded off the wire are DIFFERENT things.
    Collapsing them would rebuild, one level up, exactly the confusion that
    made a guessed apply body look measured.
    """

    def test_it_names_the_five_that_were_captured(self):
        assert set(C.CAPTURED_WRITE_CONTRACTS) == {
            # Added 2026-08-23. inbox_reply is SHIPPED, and it is the entry that
            # most needs its class read: it is the only irreversible surface in
            # this register that reaches another person, and unlike the other
            # four it cannot currently be wire-confirmed at all -- the inbox
            # holds no threads to intercept a send in.
            "inbox_reply",
            "support_tickets",
            "saved_search_alert_toggle",
            "referrals",
            "profile_image",
        }

    @pytest.mark.parametrize("surface", sorted(C.CAPTURED_WRITE_CONTRACTS))
    def test_every_entry_declares_its_evidence_class(self, surface):
        entry = C.CAPTURED_WRITE_CONTRACTS[surface]
        assert entry["evidence"] in (C.CONTRACT_WIRE, C.CONTRACT_SHIPPED)
        assert entry["method"] in ("POST", "PUT", "PATCH", "DELETE")
        assert entry["path"].startswith("/")
        assert entry["body_keys"], "%s: a contract with no body keys is not one" % surface
        assert len(entry["note"]) > 120, "%s: the note carries no finding" % surface

    def test_the_two_evidence_classes_are_not_the_same_string(self):
        """If these ever collapse to one value the distinction stops being
        enforceable, and every SHIPPED entry silently becomes WIRE."""
        assert C.CONTRACT_WIRE != C.CONTRACT_SHIPPED

    @pytest.mark.parametrize(
        "surface",
        sorted(
            s
            for s, e in C.CAPTURED_WRITE_CONTRACTS.items()
            if e["evidence"] == C.CONTRACT_WIRE
        ),
    )
    def test_a_wire_entry_has_the_recording_it_claims(self, surface):
        """WIRE means a recording exists. This finds it and checks it agrees.

        Without this, "WIRE" is a word in a dict. The fixture is the artefact;
        the constant is the summary of it, and a summary that has drifted from
        its artefact is worse than no summary.
        """
        entry = C.CAPTURED_WRITE_CONTRACTS[surface]
        path = FIXTURE_DIR / (surface.rstrip("s") + ".json")
        candidates = [path, FIXTURE_DIR / (surface + ".json")]
        found = [p for p in candidates if p.is_file()]
        assert found, "no recording on disk for WIRE contract %r (looked for %s)" % (
            surface,
            [p.name for p in candidates],
        )
        fixture = json.loads(found[0].read_text(encoding="ascii"))
        assert fixture["method"] == entry["method"]
        assert entry["path"].split("/:")[0] in fixture["url"]
        assert tuple(sorted(fixture["body_keys"])) == tuple(sorted(entry["body_keys"]))
        assert "WIRE" in fixture["_provenance"]

    def test_a_shipped_entry_has_no_wire_recording_on_disk(self):
        """The honest half, asserted structurally rather than by vocabulary.

        A SHIPPED contract is a reading of Instahyre's JavaScript; no request
        was ever serialized. The failure this guards is someone hand-writing a
        plausible fixture beside the captured ones, at which point the evidence
        class in the register and the artefacts on disk say different things
        and the weaker claim wins by looking like the stronger one.

        Checking for the FILE rather than for words in the note is deliberate.
        This file has already learned once that bending prose to satisfy a
        keyword list makes the prose worse to make a test pass.
        """
        for surface, entry in C.CAPTURED_WRITE_CONTRACTS.items():
            if entry["evidence"] != C.CONTRACT_SHIPPED:
                continue
            stray = [
                p.name
                for p in FIXTURE_DIR.glob("*.json")
                if p.stem in (surface, surface.rstrip("s"))
            ]
            assert not stray, (
                "%s is registered as %s evidence but a wire fixture exists for it "
                "(%s). Either it was captured -- in which case promote the entry to "
                "%s -- or the fixture is invented."
                % (surface, C.CONTRACT_SHIPPED, stray, C.CONTRACT_WIRE)
            )

    def test_the_trailing_slash_measurement_is_recorded_not_assumed(self):
        """Angular strips it, and that was measured here rather than recalled:
        the factory declares candidate_query with a trailing slash and the wire
        capture went to the stripped spelling. Every path constant below
        depends on that, so it is pinned."""
        fixture = json.loads((FIXTURE_DIR / "support_ticket.json").read_text("ascii"))
        assert fixture["url"].endswith("/candidate_query")
        assert not fixture["url"].endswith("/candidate_query/")
        assert C.EP_SUPPORT_QUERY == "/candidate_misc/support/candidate_query"

    def test_the_profile_image_contract_says_no_tool_is_built_on_it(self):
        """A recorded contract is not permission to build. This one is blocked
        on an encoder the package has no dependency for, and on a CREATE branch
        that was never exercised -- both named in the note so the next session
        does not read a captured body as a green light."""
        note = C.CAPTURED_WRITE_CONTRACTS["profile_image"]["note"].lower()
        assert "no tool is built" in note
        assert "webp" in note


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
        """Fed a module that posts to the questionnaire resource. The scanner
        has to see it, or the test above is a green light with nothing behind
        it.

        The REFERRAL resource used to be this control's subject. It was
        retargeted on 2026-08-23, when the referral contract was captured and
        that surface left the register -- a control has to point at something
        the register still forbids, or it stops discriminating.
        """
        offending = {
            "rogue.py": (
                "def send(http):\n"
                "    return http.post('/questionnaires/answer',\n"
                "                     json={'answers': []})\n"
            )
        }
        hits = unverified_paths_in(offending)
        assert [h[0] for h in hits] == ["screening_questionnaires"]

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
                '"""We do not call /questionnaires/answer -- no body is known."""\n'
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
        prose = C.UNVERIFIED_WRITE_SURFACES["screening_questionnaires"]
        assert "/questionnaires/answer" in prose, (
            "this control needs a register entry that names its own path"
        )
        assert unverified_paths_in({"rogue.py": "R = %r\n" % prose}) == []
        assert unverified_paths_in({"rogue.py": "R = '/questionnaires/answer'\n"})

    def test_a_path_with_a_space_is_not_a_path__CONTROL(self):
        """The discriminator, isolated. A sentence that begins with the path is
        still a sentence."""
        assert not looks_like_a_path("/questionnaires/answer is not called here")
        assert looks_like_a_path("/questionnaires/answer")

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
