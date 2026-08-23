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
import json
import logging
import urllib.parse

import pytest

from conftest import (
    assert_no_credential,
    credential_needles,
    credential_strings_in,
)

#: The credential shapes this file reasons about. Deliberately unmistakable, so
#: a hit is never an accident -- and deliberately DIFFERENT from the sentinels
#: in ``test_auth_lifecycle.py``, so that a copy-paste between the two files
#: cannot make one look green on the other's fixture.
COOKIE = "SECRET-walker-sessionid-value-0123456789abcdef"

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
