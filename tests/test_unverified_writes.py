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

    def test_the_workex_entry_carries_its_own_correction(self):
        """This entry asserted, on 2026-08-23, that NO CALLER exists in any
        shipped bundle. One does -- on the onboarding page, which is why a
        profile-page search missed it. An entry that were silently rewritten
        would leave the next session with no way to tell a corrected finding
        from an original one, and this register's whole worth is that its
        entries can be trusted as evidence rather than as opinion."""
        reason = C.UNVERIFIED_WRITE_SURFACES["workex_put"]
        assert "WRONG" in reason, "the correction has to be visible as a correction"
        assert "onBoardingProfileSave" in reason, "the caller has to be named"
        assert "candidate" in reason.lower()

    def test_the_referral_contract_still_says_who_it_would_mail(self):
        """The one the operator explicitly approved, and the one where the
        stakes are highest: send_invites reaches real people permanently.
        Capturing the contract changed what we know, not what it does, and the
        note has to keep saying so -- a reader who arrives months from now
        remembering only the approval must still meet the warning."""
        note = C.CAPTURED_WRITE_CONTRACTS["referrals"]["note"].lower()
        assert "third parties" in note
        assert "unsend" in note or "undo" in note

    def test_it_is_kept_apart_from_what_is_already_built(self):
        """RE-RATIFIED 2026-08-25. The contrast this drew has outlived one side.

        It read: "FORBIDDEN_ENDPOINTS means 'never, at any evidence level'.
        This register means 'not yet, on this evidence'. Merging them would
        either permanently ban six buildable things or quietly unban a
        mass-apply." -- and it asserted ``C.FORBIDDEN_ENDPOINTS`` was non-empty
        under the words "the permanent ban must still hold entries".

        There is no permanent ban any more. It was lifted by ruling and bulk
        apply was built. The half of the distinction that SURVIVES, and that
        this test now measures, is the one that was always doing the work: this
        register is about EVIDENCE and nothing else. An entry here is not
        disapproved of, it is UNMEASURED -- so the failure to guard against is
        a surface sitting in this register while a tool quietly sends to it.
        That is checked directly against the paths this package can actually
        POST to, which is a stronger question than the one about the blocklist
        and does not depend on a blocklist existing.
        """
        sendable = C.SENDABLE_INBOX_PATHS | C.SENDABLE_BULK_APPLY_PATHS
        for surface, fragment in UNVERIFIED_PATH_FRAGMENTS.items():
            assert not any(fragment in path for path in sendable), (
                "%s is registered as UNMEASURED but %r is on a sendable "
                "allowlist" % (surface, fragment)
            )
        assert C.FORBIDDEN_ENDPOINTS == frozenset(), (
            "FORBIDDEN_ENDPOINTS regrew entries. It was emptied by ruling on "
            "2026-08-25; refilling it is a ruling too, not a patch: %s"
            % sorted(C.FORBIDDEN_ENDPOINTS)
        )

    def test_bulk_apply_left_this_register_by_being_measured(self):
        """The old name of this test was ``test_the_bulk_apply_ban_is_untouched``
        and it pinned the ban by count and by content.

        Bulk apply is the case that proves what this whole file claims -- that
        the bar is EVIDENCE, and that clearing it is how a surface becomes
        ordinary work. It was the single hardest-banned path in the package and
        the route out was the same as for everything else: read the contract,
        register it, gate it, build it. So the assertion is inverted rather than
        deleted. It must NOT be in the unmeasured register, it MUST be in the
        captured one, and it must be reachable through a named allowlist rather
        than through nothing.
        """
        assert "apply_bulk" not in C.UNVERIFIED_WRITE_SURFACES
        assert "apply_bulk" in C.CAPTURED_WRITE_CONTRACTS
        assert len(C.SENDABLE_BULK_APPLY_PATHS) == 2
        assert all("apply_bulk" in path for path in C.SENDABLE_BULK_APPLY_PATHS)
        # The read tier never moved and this is the file where a reader looks
        # for "what may this package not send".
        assert "apply_bulk" in C.MUTATING_PATH_MARKERS


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

    def test_it_names_the_thirteen_that_were_captured(self):
        assert set(C.CAPTURED_WRITE_CONTRACTS) == {
            # Added 2026-08-25, and the two entries in this register that run
            # in the opposite direction from every other one. The eleven below
            # are requests HE initiates; these two ANSWER a question Instahyre
            # put to him, and hire_check's answer is a terminal employment
            # outcome on a channel nothing in this server could previously see.
            # Both are SHIPPED: two callers agree on the hire-check body, one
            # ships the rating body, and the rating entry is the one that
            # forced the register to record WHERE a field goes -- its action
            # declares Angular params, so its three fields ride the query
            # string as well as the body.
            #
            # Neither has ever been exercised against live data and neither can
            # be today: all three routes in the cluster answered 200 and EMPTY
            # on 2026-08-25, so both writes refuse every call. The notes say
            # so rather than implying a test that did not happen.
            "hire_check",
            "opportunity_rating",
            # Added 2026-08-25, and the entry with the cleanest evidence in the
            # whole register: a real serialized body, from his own signed-in
            # browser, aborted at the router. It is also the one whose capture
            # CHANGED THE DESIGN rather than confirming it. Reading the shipped
            # source alone would have produced a write that echoed the read back
            # verbatim -- and the wire showed the page collapsing `university`
            # from the expanded object the GET returns down to a resource URI on
            # the way out. A verbatim echo would not have been the measured
            # request. That gap between SHIPPED and WIRE is the reason the two
            # classes are kept apart at all, and this is the first entry where
            # it cost something concrete.
            "education",

            # Added 2026-08-25, and the only entry here that retires a BAN
            # rather than an absence. Both its paths sat in
            # FORBIDDEN_ENDPOINTS, the one refusal in this package that said
            # "at any evidence level". Its contract was never the obstacle --
            # the factory and the body builder ship whole in Instahyre's
            # JavaScript, the same SHIPPED class as the four inbox entries
            # below. What kept it out was blast radius, and that is now a gate
            # rather than a blocklist. Its note is required to say so.
            "apply_bulk",
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
            # Added 2026-08-24, and it is the entry whose evidence class reads
            # BACKWARDS from the others. SHIPPED is normally the weaker word --
            # a body assembled in the page and never serialized. This body is
            # not assembled at all: the site hands its $resource the object the
            # profile GET returned, by reference, so the payload can be read
            # live instead of reconstructed. The usual gap between shipped
            # source and the wire is on the body, and here there is no body to
            # get wrong.
            "job_search_profile",
            # Added 2026-08-25. All three were CAPTURED on 2026-08-23 and sat
            # unregistered while the tools were withheld on value; registering
            # them is what made building them ordinary work rather than a
            # special case. inbox_mark_all_read is the one that changed the
            # SHAPE of this register: its method is GET, and it is the reason
            # the entry check below no longer assumes a write wears a write
            # verb.
            "inbox_star",
            "inbox_mark_read",
            "inbox_mark_all_read",
        }

    def test_the_jsp_contract_says_the_omission_hazard_is_unreachable(self):
        """The one thing SHIPPED evidence genuinely leaves open on this surface
        is whether an omitted key is a deletion. A note that merely mentioned
        the hazard would be describing a risk the tool then takes. This one has
        to say the write cannot omit a key, because that -- not the evidence
        class -- is what makes the surface safe."""
        note = C.CAPTURED_WRITE_CONTRACTS["job_search_profile"]["note"].lower()
        assert "omitted key" in note or "omits" in note
        assert "guard" in note or "refuses" in note

    @pytest.mark.parametrize("surface", sorted(C.CAPTURED_WRITE_CONTRACTS))
    def test_every_entry_declares_its_evidence_class(self, surface):
        """GET JOINED THE ALLOWED METHODS ON 2026-08-25, deliberately.

        Until then this line read ``("POST", "PUT", "PATCH", "DELETE")`` -- a
        list that quietly encoded the assumption a mutation wears a mutating
        verb. ``mark_all_read`` is the counterexample this whole package keeps
        pointing at: Instahyre declares it
        ``{method:'GET',url:url+"mark_all_read"}`` and it clears unread state
        across the entire inbox. Widening the tuple is therefore not a
        loosening; it is the register finally being able to describe the one
        surface whose danger is that it does NOT look like a write. Keeping the
        old tuple would have forced the entry to be mislabelled as a POST to
        get past its own shape check, which is the direction that actually
        costs something.
        """
        entry = C.CAPTURED_WRITE_CONTRACTS[surface]
        assert entry["evidence"] in (C.CONTRACT_WIRE, C.CONTRACT_SHIPPED)
        assert entry["method"] in ("POST", "PUT", "PATCH", "DELETE", "GET")
        assert entry["path"].startswith("/")
        assert entry["body_keys"], "%s: a contract with no body keys is not one" % surface
        assert len(entry["note"]) > 120, "%s: the note carries no finding" % surface

    def test_the_one_GET_entry_says_it_mutates_and_names_its_query_keys(self):
        """A GET in this register is only admissible if it declares WHY.

        The widened method tuple above is safe exactly to the extent that a GET
        entry cannot slip in as an ordinary read. So the two are checked
        together: any entry whose method is GET has to say in its own note that
        it mutates, and has to publish the query keys that stand in for the
        body it does not have.
        """
        gets = {
            surface: entry
            for surface, entry in C.CAPTURED_WRITE_CONTRACTS.items()
            if entry["method"] == "GET"
        }
        assert set(gets) == {"inbox_mark_all_read"}, (
            "a new GET entered the captured-write register: %s. A GET here is a "
            "mutating GET by definition -- say so in its note, or it does not "
            "belong." % sorted(gets)
        )
        for surface, entry in gets.items():
            assert "MUTATE" in entry["note"].upper(), surface
            assert entry["query_keys"], "%s: a GET contract needs its query keys" % surface
            assert "page_loaded_at" in entry["query_keys"]
            # body_keys is required to be truthy by the shape check above, so a
            # GET cannot simply leave it empty. It has to SAY there is no body,
            # which is the reading that stops an empty tuple and an unrecorded
            # body from looking the same.
            declared = " ".join(entry["body_keys"]).lower()
            assert "none" in declared and "query string" in declared, declared

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

    @pytest.mark.parametrize("path", sorted(C.SENDABLE_BULK_APPLY_PATHS))
    def test_a_bulk_path_is_still_spelled_only_in_the_register(self, path):
        """RE-SOURCED 2026-08-25, and the reason is a finding in itself.

        This was parametrized over ``C.FORBIDDEN_ENDPOINTS``. When that set was
        emptied by the ruling, the parametrize list went empty too -- and an
        empty parametrize does not fail, it SKIPS. The test reported "skipped,
        got empty parameter set" and would have gone on reporting that forever,
        a guard that had quietly stopped guarding while the suite stayed green.
        Nothing else in the run would have said so.

        The rule it enforces did not change and is worth keeping now that the
        paths are reachable rather than banned: the two bulk URLs are spelled
        in ``constants.py`` and NOWHERE ELSE, so every call site names a
        constant and "what can this package POST to" stays answerable by
        reading one file. Parametrizing over the sendable set means the list
        can never silently empty -- an empty allowlist would be caught by the
        assertions in ``test_bulk_apply_left_this_register_by_being_measured``.
        """
        for name, _, literal in string_constants(package_sources()):
            if literal == path:
                assert name == "constants.py", (
                    "a bulk apply path is spelled outside the register: %s" % name
                )
