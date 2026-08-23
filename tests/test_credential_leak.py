r"""The credential-leak walker, and the proof that it can fail.

WHY THIS FILE EXISTS
--------------------
On 2026-08-23 every "the cookie value never leaks" assertion in this suite was
one substring search for one plaintext sentinel, run over a walker that visited
``str`` and nothing else. Measured against eight payload shapes each carrying
the WHOLE session cookie, **six were invisible**. The suite was green
throughout. A guard that cannot fire certifies nothing, and a security guard
that cannot fire is worse than none, because it is quoted as evidence.

The worst single instance was not a missing test but a DISCONNECTED one:
``test_auth_lifecycle.build_jar`` writes ``SECRET_ENCRYPTED_BLOB`` into the
``encrypted_value`` column of every row it builds, and says in its own docstring
that it is there so that "a reader that ever selects a wildcard, or names the
wrong column, hands them straight to the leak assertions below". No assertion
below, or anywhere else in the repo, ever hunted that value -- and it is
``bytes``, so the walker could not have seen it even if one had. The trap was
set and the alarm was disconnected.

The same class of defect was reported on the naukri server in the same week: a
leak walker hunting a plaintext marker that can never appear inside a base64url
JWT, so every "the token never leaks" test would have passed a result that
echoed the entire credential. That is the ``b64`` needle below, and it is why
the fix is a set of SPELLINGS rather than one wider walk.

HOW THIS FILE IS ORGANISED
--------------------------
Section 1 pins the baseline: the walker fires on the ordinary case.
Section 2 is the controls. Each one reconstructs the OLD detector exactly,
measures it reporting CLEAN on a payload carrying the whole credential, and
then measures the new one firing on the same payload. Both halves are asserted,
so a control that stops discriminating fails instead of quietly agreeing.
Section 3 checks the walker's own edges -- cycles, elision, and the rule that a
failure message must not reprint the credential.
Section 4 points the finished instrument at the real tool payloads.

Every control is named ``..._the_old_detector_could_not_see__CONTROL``, which
is this repo's convention for "this test is the evidence that another test can
fail".
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import pathlib
import sys
import urllib.parse

import pytest

from conftest import (
    CREDENTIAL_RUN_LENGTH,
    assert_no_credential,
    assert_no_credential_shape,
    credential_needles,
    credential_runs,
    credential_shaped_hits,
    credential_strings_in,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import leak_transform_matrix as matrix  # noqa: E402

#: The credential shapes this file reasons about. CREDENTIAL-SHAPED and
#: CREDENTIAL-LENGTH -- 32 lowercase alphanumerics, exactly what Django hands
#: out -- because a marker that is neither cannot exercise the shape scan and
#: one shorter than the truncation window passes truncation for the wrong
#: reason. Still unmistakable at 36**32 spellings, and deliberately DIFFERENT
#: from the sentinels in ``test_auth_lifecycle.py``, so that a copy-paste
#: between the two files cannot make one look green on the other's fixture.
COOKIE = "walkersessionidvalue0123456789ab"

#: A DPAPI-sealed blob, as sqlite hands back Chrome's ``encrypted_value``:
#: ``bytes``, not ``str``, and carrying bytes no text codec would round-trip.
#: The high bytes are the point -- a walker that decoded with ``utf-8`` would
#: raise here, which is why the shared one uses latin-1.
SEALED = b"\x01\x00\x00\x00SECRET-sealed-blob-\xff\xfe\x00payload"


# ===========================================================================
# The OLD detector, reconstructed verbatim
# ===========================================================================
#
# This is not a strawman. It is the code that was in
# ``test_auth_lifecycle.assert_no_secret`` and ``strings_in`` on 2026-08-23,
# copied here so the controls below measure a real historical instrument rather
# than an argument about one. It must stay frozen: if someone "improves" it,
# the controls stop measuring the thing they exist to measure.


def old_strings_in(payload, _trail=""):
    """The str-only walker, as it was. dict / list / tuple / str, nothing else."""
    out = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = "%s.%s" % (_trail, key)
            if isinstance(key, str):
                out.append((here + " (KEY)", key))
            out.extend(old_strings_in(value, here))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            out.extend(old_strings_in(value, "%s[%d]" % (_trail, index)))
    elif isinstance(payload, str):
        out.append((_trail or "<root>", payload))
    return out


def old_detector_sees(payload, secret) -> bool:
    """True when the old plaintext-substring detector would have fired."""
    return any(secret in text for _, text in old_strings_in(payload))


def new_detector_sees(payload, *secrets) -> bool:
    """True when :func:`assert_no_credential` would fire on ``payload``."""
    try:
        assert_no_credential(payload, *secrets)
    except AssertionError:
        return True
    return False


# ===========================================================================
# 1. The baseline -- the ordinary case still fires
# ===========================================================================


class TestTheOrdinaryCase:
    """Widening a detector must not cost it the case it already caught."""

    def test_a_plain_cookie_value_in_a_dict_is_caught(self):
        assert new_detector_sees({"cookie": COOKIE}, COOKIE)

    def test_a_cookie_value_used_as_a_dict_key_is_caught(self):
        assert new_detector_sees({COOKIE: "whatever"}, COOKIE)

    def test_a_cookie_value_nested_in_a_list_of_dicts_is_caught(self):
        payload = {"jar": [{"name": "sessionid", "value": COOKIE}]}
        assert new_detector_sees(payload, COOKIE)

    def test_a_clean_payload_passes(self):
        """The other half of a usable detector: it must also say CLEAN.

        A guard that fires on everything is as useless as one that fires on
        nothing, and it is the failure mode a widening invites.
        """
        payload = {
            "authenticated": True,
            "cookie_names": ["sessionid", "csrftoken"],
            "expires": 1795000000.0,
            "note": "no value is reported here, only names",
        }
        assert_no_credential(payload, COOKIE, SEALED)

    def test_a_url_is_not_reported_as_a_leak(self):
        """The needle set includes a percent-encoded spelling. An ordinary URL
        must not collide with it -- a scanner that cries wolf on every URL gets
        muted, and a muted scanner is a disconnected one."""
        payload = {"url": "https://www.instahyre.com/api/v1/candidate_matching?x=1"}
        assert_no_credential(payload, COOKIE, SEALED)


# ===========================================================================
# 2. The controls -- six shapes the old detector could not see
# ===========================================================================
#
# Each of these was MEASURED clean against the old detector on 2026-08-23. The
# assertion pairs are the record of that measurement, kept executable so it
# cannot rot into a claim.


class TestTheShapesTheOldDetectorMissed:

    def test_a_bytes_value_the_old_detector_could_not_see__CONTROL(self):
        """sqlite returns a BLOB column as ``bytes``. The old walker visited
        ``str`` and stopped, so Chrome's ``encrypted_value`` -- the DPAPI-sealed
        login itself -- was structurally invisible to every leak assertion in
        the suite."""
        payload = {"encrypted_value": SEALED}

        assert not old_detector_sees(payload, SEALED.decode("latin-1")), (
            "the old detector was supposed to be blind to bytes; if it now sees "
            "them this control has stopped measuring anything"
        )
        assert new_detector_sees(payload, SEALED)

    def test_a_cookie_in_bytes_the_old_detector_could_not_see__CONTROL(self):
        """The same hole, reached through the ordinary session cookie rather
        than the sealed blob: one ``.encode()`` anywhere on the path -- a body
        being written, a hash being fed -- and the value left the detector."""
        payload = {"raw_body": COOKIE.encode()}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_an_object_repr_the_old_detector_could_not_see__CONTROL(self):
        """A payload rarely carries a bare string. It carries an object, and
        the object prints its value the moment anything formats or logs it."""

        class Cookie:
            def __init__(self, name, value):
                self.name = name
                self.value = value

            def __repr__(self):
                return "Cookie(name=%r, value=%r)" % (self.name, self.value)

        payload = {"jar": [Cookie("sessionid", COOKIE)]}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_a_set_the_old_detector_could_not_see__CONTROL(self):
        """The old walker knew ``list`` and ``tuple``. A ``set`` of cookie
        values -- the natural shape for "which names do we hold" if a value
        ever slips into it -- fell straight through."""
        payload = {"held": {COOKIE, "csrftoken"}}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_base64_the_old_detector_could_not_see__CONTROL(self):
        """THE NAUKRI SHAPE. A plaintext marker cannot appear inside base64, so
        an exact-substring search over a base64-encoded credential reports
        CLEAN while echoing the whole thing. This is the control for the
        defect that was reported across from the naukri server, reproduced
        here rather than taken on trust."""
        payload = {"serialised_jar": base64.b64encode(COOKIE.encode()).decode()}

        assert not old_detector_sees(payload, COOKIE), (
            "base64 was supposed to be opaque to a plaintext search"
        )
        assert new_detector_sees(payload, COOKIE)

    def test_base64url_the_old_detector_could_not_see__CONTROL(self):
        """The URL-safe alphabet is a different string for any value carrying a
        ``+`` or ``/`` in its standard encoding, so it is a separate needle
        rather than the same one. A JWT and a cookie in a query string both
        wear this spelling."""
        payload = {"token": base64.urlsafe_b64encode(COOKIE.encode()).decode()}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_the_sealed_blob_was_planted_and_never_hunted__CONTROL(self):
        """The disconnected alarm, stated as a measurement.

        ``test_auth_lifecycle.build_jar`` plants a sealed blob in every row so a
        wildcard select would be caught. Both halves of that catch were broken:
        the value was never in any needle set, AND it is bytes, so the walker
        could not have reached it. Fixing one without the other would still
        have left the trap disconnected, which is why both are asserted here.
        """
        payload = {"row": {"name": "sessionid", "encrypted_value": SEALED}}

        # Half one: the walker could not reach it.
        assert not any(
            "encrypted_value" in trail and "SECRET-sealed" in text
            for trail, text in old_strings_in(payload)
        )
        # Half two: even reachable, it was in nobody's needle set. Hunting the
        # cookie alone -- which is what every assertion did -- stays clean.
        assert not new_detector_sees(payload, COOKIE)
        # And with the blob named, the finished instrument fires.
        assert new_detector_sees(payload, SEALED)


class TestTheNeedleSetItself:

    def test_every_spelling_is_generated_and_labelled(self):
        """``exact`` and ``b64`` are always distinct: base64 of a plaintext can
        never equal it. The other three deduplicate whenever they add no
        coverage, which is the design -- each has its own test below, run on a
        value that forces the spelling apart."""
        labels = {label for label, _ in credential_needles(COOKIE)}
        assert {"exact", "b64"} <= labels

    def test_the_b64url_spelling_is_a_real_needle_for_a_value_that_needs_it(self):
        """The URL-safe alphabet substitutes ``-_`` for ``+/``, so it is a
        different string only for a value whose standard encoding uses them.
        For anything else it deduplicates, which is why the label set above
        cannot be asserted wholesale.

        A sealed blob is exactly such a value: DPAPI output is arbitrary bytes,
        so its standard encoding routinely carries ``+`` and ``/``.
        """
        forced = b"SECRET-blob-\xff\xbf\xfe"
        needles = dict((label, n) for label, n in credential_needles(forced))
        assert "b64url" in needles and needles["b64url"] != needles["b64"]
        payload = {"token": needles["b64url"]}

        assert not old_detector_sees(payload, forced.decode("latin-1"))
        assert new_detector_sees(payload, forced)

    def test_a_non_ascii_secret_keeps_an_exact_needle_that_matches_itself(self):
        """The needle builder holds a secret's TEXT and its BYTES separately.

        Deriving one from the other -- reading a utf-8 encode back as latin-1 --
        turns any non-ASCII secret into mojibake, so the ``exact`` needle stops
        matching the value it was built from and the detector reports CLEAN on
        a payload that is literally echoing it. Found by the control above on
        2026-08-23, before this file had a test for it.
        """
        secret = "SECRET-token-\xfb\xff-non-ascii"
        needles = dict((label, n) for label, n in credential_needles(secret))
        assert needles["exact"] == secret
        assert new_detector_sees({"echo": secret}, secret)

    def test_the_repr_spelling_is_a_real_needle_for_a_value_that_needs_it(self):
        """``repr`` of an ordinary ASCII value with no escapes IS that value, so
        it deduplicates away -- the same way ``needles_for`` in
        ``test_path_hygiene.py`` deduplicates on POSIX. A value carrying a
        backslash gets a genuinely different needle, and that is the spelling a
        traceback or an ``%r`` format would carry it in."""
        awkward = "SECRET\\token\twith\nescapes"
        needles = dict((label, n) for label, n in credential_needles(awkward))
        assert needles["repr"] != awkward
        payload = {"traceback": "OSError: %r" % awkward}

        assert not old_detector_sees(payload, awkward), (
            "a repr-escaped value was supposed to be opaque to a plain search"
        )
        assert new_detector_sees(payload, awkward)

    def test_the_percent_spelling_is_deduplicated_when_it_adds_nothing(self):
        """``COOKIE`` is URL-safe, so its percent encoding IS the plaintext.
        The needle set must not carry it twice -- a duplicated needle doubles
        every failure message for no extra coverage."""
        needles = [needle for _, needle in credential_needles(COOKIE)]
        assert len(needles) == len(set(needles))
        assert urllib.parse.quote(COOKIE, safe="") == COOKIE

    def test_the_percent_spelling_is_a_real_needle_for_a_value_that_needs_it(self):
        """And the deduplication above must not be mistaken for the detector
        being absent. A credential carrying URL-unsafe characters gets a
        genuinely distinct percent needle, and it fires."""
        awkward = "SECRET value/with+unsafe=chars&here"
        needles = dict((label, n) for label, n in credential_needles(awkward))
        assert needles["percent"] != awkward
        payload = {"url": "https://x/?s=" + urllib.parse.quote(awkward, safe="")}
        assert not old_detector_sees(payload, awkward)
        assert new_detector_sees(payload, awkward)

    def test_a_bytes_secret_is_accepted_without_a_decode_at_the_call_site(self):
        """Callers hold the sealed blob as bytes. Making each one decode it
        before asserting is how a needle set drifts out of sync with the
        fixture it is meant to hunt."""
        assert credential_needles(SEALED)
        assert new_detector_sees({"x": SEALED}, SEALED)


# ===========================================================================
# 3. The walker's own edges
# ===========================================================================


class TestTheWalkerDoesNotBreak:

    def test_a_self_referential_payload_does_not_recurse_forever(self):
        """A cycle must make the check return, not raise RecursionError. A
        crash and a clean pass are equally useless, but a crash also looks like
        a bug in the code under test rather than in the instrument."""
        payload: dict = {"name": "sessionid"}
        payload["self"] = payload
        assert_no_credential(payload, COOKIE)

    def test_a_cycle_does_not_hide_a_leak_beside_it(self):
        """The cycle guard must not become an early exit that skips siblings."""
        payload: dict = {"leak": COOKIE}
        payload["self"] = payload
        assert new_detector_sees(payload, COOKIE)

    def test_scalar_values_are_skipped_rather_than_stringified(self):
        """Rendering every int and float would fill a failure message with
        noise and could not carry a credential anyway. The KEYS are still
        walked -- a secret in a key is a secret -- so what must be absent is
        the four VALUES, not the four entries."""
        found = dict(credential_strings_in({"n": 42, "f": 1.5, "b": True, "z": None}))
        assert set(found) == {".n (KEY)", ".f (KEY)", ".b (KEY)", ".z (KEY)"}
        assert set(found.values()) == {"n", "f", "b", "z"}

    def test_an_object_hiding_its_state_from_its_repr_is_still_walked__CONTROL(self):
        """The hole a repr-only fallback leaves, isolated from logging.

        This object's repr says nothing about its contents, which is ordinary:
        most classes inherit ``<Foo object at 0x...>``. Walking ``vars()`` is
        what reaches the value, and this control fails if that walk is ever
        removed as redundant.
        """

        class Opaque:
            def __init__(self, value):
                self.token = value

            def __repr__(self):
                return "<Opaque>"

        payload = {"holder": Opaque(COOKIE)}

        assert COOKIE not in repr(payload["holder"]), (
            "this control needs an object whose repr hides its state"
        )
        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_an_object_with_no_dict_does_not_crash_the_walker(self):
        """``vars()`` raises ``TypeError`` on a ``__slots__`` class. An
        instrument that crashes on an awkward object gets deleted, and a
        deleted guard is a passing one."""

        class Slotted:
            __slots__ = ("token",)

            def __init__(self, value):
                self.token = value

        assert_no_credential({"s": Slotted("harmless")}, COOKIE)

    def test_the_failure_message_names_the_field_and_the_spelling(self):
        payload = {"durability": {"stored_in": "jar=" + COOKIE}}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential(payload, COOKIE)
        message = str(excinfo.value)
        assert "durability" in message and "stored_in" in message
        assert "[exact]" in message

    def test_the_failure_message_does_not_reprint_the_credential(self):
        """A failure goes into a CI log the operator cannot redact after the
        fact. It must say where the leak is without copying the value into a
        second place -- otherwise the guard firing is itself a disclosure."""
        payload = {"cookie": COOKIE}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential(payload, COOKIE)
        message = str(excinfo.value)
        assert COOKIE not in message, "the guard leaked the secret while reporting it"
        assert "<THE SECRET, %d chars>" % len(COOKIE) in message

    def test_the_elision_still_reports_a_leak_found_in_bytes(self):
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential({"blob": SEALED}, SEALED)
        assert "(BYTES)" in str(excinfo.value)

    def test_the_elision_still_reports_a_leak_found_in_a_repr(self):
        class Holder:
            def __repr__(self):
                return "Holder(%s)" % COOKIE

        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential({"h": Holder()}, COOKIE)
        assert "(REPR)" in str(excinfo.value)


# ===========================================================================
# 4. The finished instrument, over the real surfaces
# ===========================================================================
#
# The sections above prove the instrument works. These point it at what it was
# built for. The auth-lifecycle payloads are covered in their own file, which
# now delegates to this walker; what is left here are the two surfaces that had
# no credential assertion at all.


class TestTheRealSurfaces:

    def test_a_json_round_trip_does_not_smuggle_a_credential_past_the_walker(self):
        """``json.dumps`` escapes as it serialises. A detector run only over the
        serialised form can be defeated by an escape; this walker runs over the
        structure, and the ``repr`` needle covers the escaped spelling too."""
        awkward = 'SECRET-"quoted"\\and\\escaped'
        payload = {"body": json.loads(json.dumps({"v": awkward}))}
        assert new_detector_sees(payload, awkward)

    def test_no_cookie_value_reaches_a_log_record_object(self, caplog):
        """``caplog.text`` renders records to a string, so a value carried on a
        record's ARGS rather than in its message reaches a log handler while a
        text search stays clean. The walker is pointed at the record objects
        themselves for exactly that reason."""
        logger = logging.getLogger("instahyre_server.test_probe")
        with caplog.at_level(logging.DEBUG):
            logger.debug("cookie names: %s", ["sessionid", "csrftoken"])
        assert_no_credential(list(caplog.records), COOKIE, SEALED)

    def test_that_log_check_can_fail__CONTROL(self, caplog):
        """The check above passing is only meaningful if planting a value in the
        same place makes it fail. Planted on ARGS, not in the message: that is
        the half a ``caplog.text`` search would have missed.

        This control is the one that found the repr-only hole. A ``LogRecord``
        renders as ``<LogRecord: name, 10, file, 388, "value: %s">`` -- the
        format string, never the args -- so the first version of the walker
        reported CLEAN on a record carrying the entire cookie. The attribute
        walk exists because of this measurement.
        """
        logger = logging.getLogger("instahyre_server.test_probe")
        with caplog.at_level(logging.DEBUG):
            logger.debug("value: %s", COOKIE)
        record = caplog.records[-1]

        assert COOKIE not in repr(record), (
            "a LogRecord was supposed to hide its args from its repr; if it no "
            "longer does, this control has stopped measuring the hole it found"
        )
        assert new_detector_sees([record], COOKIE)


# ===========================================================================
# 5. The transform grid -- 9 transforms x 6 entry-point shapes
# ===========================================================================
#
# THE LESSON THIS ENCODES, because it has now been re-learned twice across
# three servers: THE ENCODING DOES NOT HAVE TO BE IN THE CREDENTIAL. IT CAN BE
# IN THE LEAK PATH. A tool that base64s, hex-dumps, splits or merely logs its
# output hides a planted marker regardless of what shape the credential itself
# has. "Our session cookie is an opaque Django string with no encoding step in
# which a marker could hide" is a clearance issued by reasoning, and it is
# wrong -- measured here rather than argued.
#
# The grid ran 18/54 LEAK on 2026-08-23 with three transforms invisible at
# every entry point: hex, split and truncated. It runs 0/54 now. Both numbers
# are in scripts/leak_transform_matrix.py, which prints the map; this is the
# same grid as a gate.


class TestTheTransformGrid:

    @pytest.mark.parametrize("transform_name", [n for n, _ in matrix.TRANSFORMS])
    @pytest.mark.parametrize("entry_name", [n for n, _ in matrix.ENTRY_POINTS])
    def test_no_cell_ships_the_credential(self, transform_name, entry_name):
        transform = dict(matrix.TRANSFORMS)[transform_name]
        wrap = dict(matrix.ENTRY_POINTS)[entry_name]
        assert not matrix.cell_leaks(transform, wrap), (
            "%s through %s shipped the whole credential and the walker said CLEAN"
            % (transform_name, entry_name)
        )

    def test_the_grid_is_the_size_it_claims(self):
        """A grid that quietly shrank would report a clean sweep over fewer
        cells. Pinned, so dropping a transform is a visible edit."""
        rows, leaking, total = matrix.run_grid()
        assert total == 54 and len(rows) == 9
        assert leaking == 0

    def test_every_transform_really_hides_the_plaintext__CONTROL(self):
        """The grid is only evidence if its transforms actually transform.

        A transform that returned the value unchanged would make its whole row
        pass on the strength of the plaintext needle, certifying nothing. Each
        encoding transform is checked to have removed the plaintext; the two
        fragment transforms are checked to hold no field containing the whole
        value, which is the property that defeats whole-string matching.
        """
        value = matrix.SESSIONID
        for name, transform in matrix.TRANSFORMS:
            if name in ("identity", "percent", "repr", "log_only"):
                # percent and repr are identity for an alphanumeric value, and
                # log_only does carry it verbatim -- on an ARG, which is the
                # point. Their coverage is asserted elsewhere in this file.
                continue
            produced = transform(value)
            fields = list(produced.values())
            assert all(value not in str(f) for f in fields), (
                "%s left the plaintext intact -- its row proves nothing" % name
            )

    def test_the_fragment_transforms_hold_no_whole_value__CONTROL(self):
        """split and truncated are the two the run scan exists for. If either
        ever produced a field containing the whole credential, it would be
        caught by the ordinary needle and this file would silently stop
        exercising the run scan at all."""
        value = matrix.SESSIONID
        for name in ("split", "truncated"):
            produced = dict(matrix.TRANSFORMS)[name](value)
            assert all(value not in str(f) for f in produced.values())
            assert any(len(str(f)) >= 12 for f in produced.values()), (
                "%s produced nothing long enough for a 12-char run" % name
            )


# ===========================================================================
# 6. The run scan -- fragments, which no whole-value needle can see
# ===========================================================================


class TestTheRunScan:

    def test_a_split_value_is_caught(self):
        value = COOKIE
        payload = {"prefix": value[:16], "suffix": value[16:]}

        assert not old_detector_sees(payload, value), (
            "a split value was supposed to be invisible to whole-string matching"
        )
        assert new_detector_sees(payload, value)

    def test_a_truncating_redaction_is_caught(self):
        """The redaction that looks responsible. Twelve characters of a
        32-character session id is not usable, but it is a fragment of a live
        credential in a payload that claims to have redacted it."""
        payload = {"redacted": COOKIE[:12] + "..."}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_a_hex_dump_is_caught(self):
        """Hex shares no substring with the plaintext OR with either base64
        form, so it needed its own spelling rather than falling out of the
        others."""
        payload = {"hexed": binascii.hexlify(COOKIE.encode()).decode()}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_a_split_of_an_ENCODED_value_is_caught(self):
        """Runs are taken AFTER encoding, and this is why. A base64 blob cut in
        half shares nothing with the plaintext, so runs of the plaintext would
        never match it."""
        blob = base64.b64encode(COOKIE.encode()).decode()
        payload = {"a": blob[:20], "b": blob[20:]}

        assert not old_detector_sees(payload, COOKIE)
        assert new_detector_sees(payload, COOKIE)

    def test_a_run_shorter_than_the_window_is_NOT_caught__CONTROL(self):
        """The stated limit, kept executable.

        A redaction that truncates below CREDENTIAL_RUN_LENGTH is not caught,
        and that is the intended trade -- eight characters of a 32-character
        session id is not a credential, and hunting shorter runs starts
        matching ordinary words. If this ever starts failing, the window was
        lowered and the false-positive question needs re-asking.
        """
        payload = {"redacted": COOKIE[:8] + "..."}
        assert not new_detector_sees(payload, COOKIE)

    def test_the_window_is_twelve(self):
        assert CREDENTIAL_RUN_LENGTH == 12

    def test_the_runs_carry_the_spelling_that_produced_them(self):
        """A failure has to say WHICH form the fragment came from; "a run
        matched" without that is a puzzle rather than a finding."""
        runs = credential_runs(COOKIE)
        assert set(runs.values()) >= {"exact", "b64", "hex"}
        assert all(len(r) == CREDENTIAL_RUN_LENGTH for r in runs)

    def test_the_failure_message_marks_a_fragment_as_a_run(self):
        payload = {"redacted": COOKIE[:12] + "..."}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential(payload, COOKIE)
        assert "[run/" in str(excinfo.value)

    def test_a_whole_value_leak_is_not_reported_twice(self):
        """A whole-value leak contains every run of itself. Reporting both
        instruments on the same field would triple the noise on the ordinary
        case and bury the fragment hits the run scan exists to surface."""
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential({"cookie": COOKIE}, COOKIE)
        message = str(excinfo.value)
        assert message.count("[run/") == 0
        assert "[exact]" in message

    def test_ordinary_payload_text_does_not_trip_it(self):
        """The false-positive half. Twelve characters is a floor chosen against
        exactly this -- a scanner that fires on prose gets muted."""
        payload = {
            "title": "Senior Backend Engineer, Distributed Systems",
            "company": "A Very Long Company Name Private Limited",
            "url": "https://www.instahyre.com/api/v1/candidate_matching?limit=100",
            "note": "the quick brown fox jumps over the lazy dog repeatedly",
        }
        assert_no_credential(payload, COOKIE, SEALED)


# ===========================================================================
# 7. The marker-free scan -- a leak nobody planted
# ===========================================================================
#
# Everything above finds leaks it was TOLD to expect. This finds one it was
# not, by hunting the SHAPE of a live credential with no marker involved.


class TestTheShapeScan:

    def test_a_real_shaped_session_id_is_caught_with_no_marker(self):
        """The whole point. Nothing planted this value and no needle knows it;
        it is caught because it LOOKS like the operator's session cookie."""
        real = "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3"
        payload = {"credential": {"session": real}}

        assert not new_detector_sees(payload, COOKIE, SEALED), (
            "the marker walker was supposed to be blind to a value it never planted"
        )
        with pytest.raises(AssertionError) as excinfo:
            assert_no_credential_shape(payload)
        assert "sessionid-shaped" in str(excinfo.value)

    def test_a_real_shaped_csrftoken_is_caught(self):
        real = "Ab3Cd5Ef7Gh9Ij1Kl3Mn5Op7Qr9St1Uv3Wx5Yz7Ab9Cd1Ef3Gh5Ij7Kl9Mn1Op3Q"
        assert credential_shaped_hits({"t": real})[0][0] == "csrftoken"

    def test_it_finds_a_credential_embedded_in_prose(self):
        """Requiring the whole field to BE the credential would be a check that
        only fires on the tidiest possible bug. A leak inside a sentence is
        still a leak."""
        real = "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3"
        assert credential_shaped_hits({"why": "renew failed for %s, retrying" % real})

    def test_an_ordinary_auth_payload_is_clean(self):
        """The other half of a usable detector. Names, booleans and dates are
        what these tools actually return, and none of them wears the shape."""
        payload = {
            "authenticated": True,
            "cookie_names": ["sessionid", "csrftoken"],
            "expires": 1795000000.0,
            "expiry_source": "read from the persistent Chrome profile",
            "stored_in": "_state/session.json",
        }
        assert_no_credential_shape(payload)

    def test_the_watcher_identity_collides_and_that_is_why_allow_exists__CONTROL(self):
        """A REAL false positive, measured, not hypothetical.

        ``inbound_watch.activity_identity`` returns ``sha256(...)[:32]`` -- 32
        lowercase alphanumerics, byte-for-byte the shape of a Django session
        id. Neither component is wrong. Loosening the pattern to dodge this
        would blind the scan to the thing it exists for, so the collision is
        handled by an explicit allowlist and this control is what stops the
        allowlist being mistaken for a defect in either.
        """
        from instahyre_server.inbound_watch import activity_identity

        identity = activity_identity(
            {
                "recruiter_id": 111111,
                "recruiter_company": "Acme",
                "job_title": "Backend",
                "hiring_company": "RealCo",
            }
        )
        payload = {"new": [{"identity": identity}]}

        assert credential_shaped_hits(payload), (
            "the collision is gone -- if activity_identity changed shape, this "
            "control and the allowlist below can both be removed"
        )
        assert_no_credential_shape(payload, allow=[identity])

    def test_the_allowlist_only_permits_what_it_names__CONTROL(self):
        """An allowlist is where a scanner goes to die quietly. Permitting one
        value must not permit a second."""
        first = "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3"
        second = "z1y2x3w4v5u6t7s8r9q0p1o2n3m4l5k6"
        assert_no_credential_shape({"a": first}, allow=[first])
        with pytest.raises(AssertionError):
            assert_no_credential_shape({"a": first, "b": second}, allow=[first])

    def test_the_shape_scan_reaches_bytes_and_reprs_too(self):
        """It runs over the same wide walker, so it inherits every channel the
        marker scan reaches rather than re-deriving a narrower one."""
        real = "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3"
        assert credential_shaped_hits({"raw": real.encode()})

    def test_it_does_not_fire_on_a_shorter_or_longer_run__CONTROL(self):
        """The shape is anchored, so a 31- or 33-character id is not a session
        id. Without the anchors, every long hex blob in the tree would match a
        32-character window inside it and the scan would be unusable."""
        assert not credential_shaped_hits({"a": "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d"})
        assert not credential_shaped_hits({"a": "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3x"})
