r"""The scrub guard -- nothing personal reaches a committed capture fixture.

WHY THIS FILE EXISTS
--------------------
On 2026-08-23 a sibling repo in this family pushed twenty REAL recruiter email
addresses. Nobody typed them in: a browser capture escaped its scrub map on the
way out, and the result was committed as a fixture, which is how a local
recording becomes a published one.

This repo now runs the same class of instrument.
``scripts/capture_write_contracts.py`` opens the operator's real signed-in
browser, records the live HTTP request bodies the page builds, and the results
are committed under ``tests/fixtures/write_contracts/``. Same hazard, one
commit away. The only thing standing between the browser and the repo is
``capture_write_contracts.scrub`` -- a single regex map, applied at capture
time, on the run where someone remembered to point it at the right terms. This
file is the tripwire on the other side of it.

THE SCANNER IS DELIBERATELY NOT THE SCRUBBER
--------------------------------------------
Every pattern below is written independently and is NOT imported from the
capture script. A guard that shares its regexes with the thing it guards agrees
with it by construction: a hole in the scrubber becomes a matching hole in the
scanner, and the ``scrub`` control in Section 2 -- which feeds the scrubber a
real-looking payload and requires the OUTPUT to satisfy this scanner --
degenerates into a tautology. Two independently written instruments that agree
is evidence. One instrument checked against itself is not.

For the same reason this guard does not depend on ``EXTRA_SCRUB_TERMS``. That
map is populated only by the script's ``main()`` from ``--scrub-term`` flags,
so it is EMPTY on any run where the operator forgot the flag -- which is
precisely the run this tripwire exists for. The control asserts it is empty, so
that a pass is credited to the built-in regexes and not to a term map that may
not be there next time.

WHAT IT REFUSES TO DO
---------------------
Certify nothing. The fixture directory must exist and must hold at least one
file before a clean sweep means anything, and every file must be non-empty. A
scanner that walks zero files and reports CLEAN is the exact defect this repo
has already paid for once -- see the disconnected alarm described in
``tests/test_credential_leak.py``.

HOW THIS FILE IS ORGANISED
--------------------------
Section 1 points the finished scanner at the real committed fixtures.
Section 2 is the controls. Each one MEASURES the scanner discriminating: the
dirty half fires AND the clean half stays quiet, both asserted in the one test,
so a scanner that has stopped discriminating fails rather than quietly agreeing.
Section 3 is the rule that a failure must not republish what it found.

Controls are named ``..._CONTROL``, this repo's convention for "this test is
the evidence that another test can fail".

A NOTE ON THE LITERALS BELOW
----------------------------
Every real-looking probe value is assembled from fragments rather than written
whole. This file is scanned by its own scanner in Section 2, and a guard whose
own source trips its own rules is a guard people mute. It also means this file
could be dropped into the fixture tree without setting itself off.

Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

from conftest import credential_strings_in

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import capture_write_contracts as capture  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The committed output of the capture script. Discovered by glob, never by a
#: hardcoded list: two files exist today and more are coming, and a guard that
#: names its inputs stops covering the next one the moment it lands.
WRITE_CONTRACT_DIR = REPO / "tests" / "fixtures" / "write_contracts"


# ===========================================================================
# The scanner
# ===========================================================================

#: Deliberately slightly wider than the scrubber's address pattern: the domain
#: must start with an alphanumeric, so ``@.com`` is not an address, but the
#: local part is otherwise permissive. A scanner that is narrower than the
#: scrubber can only ever agree with it.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}")

#: An Indian mobile: ten digits opening 6-9, optionally carrying a +91 country
#: code. Anchored on both sides against digits so that a longer numeric run --
#: an id, an epoch in milliseconds -- cannot supply a ten-digit window.
_MOBILE_RE = re.compile(r"(?<![0-9])(?:\+?91[-. ]?)?[6-9][0-9]{9}(?![0-9])")

#: A ``data:`` URL, matched permissively and judged by LENGTH rather than by a
#: minimum run baked into the pattern, so the finding can report how big the
#: smuggled payload was. The redaction the scrubber leaves behind --
#: ``data:<REDACTED_DATA_URL len=771>`` -- stops at the ``<``, five characters
#: in, which is how a redacted photo stays clean while a real one does not.
_DATA_URL_RE = re.compile(r"data:[A-Za-z0-9+/;,=._%-]*", re.IGNORECASE)

#: Over this many characters, a ``data:`` URL is carrying something rather than
#: declaring something. A real photo or document base64s to thousands.
DATA_URL_MAX_CHARS = 200

#: The ONLY domains an address in a committed fixture may wear. This is a
#: DOMAIN allowlist and it is exact, not a suffix test: a gmail address fails,
#: an address on this platform's own domain fails, and so does anything on a
#: subdomain of a reserved name. A tripwire that accepts ``anything.example.com``
#: accepts a domain someone can register.
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.invalid", "example.com", "example.org", "example.net", "test.invalid"}
)

#: The scrubber's contractual replacement. Subsumed by the domain allowlist
#: above, and kept explicit anyway: it is the one address this repo actively
#: WRITES, so it is named where a reader looking for it will find it.
ALLOWED_EMAIL_LITERALS = frozenset({"redacted@example.invalid"})

_UNPARSED = object()


def email_is_permitted(address: str) -> bool:
    """True when ``address`` is an obvious fake and may be committed."""
    lowered = address.lower()
    if lowered in ALLOWED_EMAIL_LITERALS:
        return True
    _, _, domain = lowered.rpartition("@")
    return domain in ALLOWED_EMAIL_DOMAINS


def scan_text(text: str) -> list:
    """``(rule, value)`` for every piece of personal data in one string."""
    found = []
    for match in _EMAIL_RE.finditer(text):
        address = match.group(0)
        if not email_is_permitted(address):
            found.append(("email", address))
    for match in _MOBILE_RE.finditer(text):
        found.append(("mobile", match.group(0)))
    for match in _DATA_URL_RE.finditer(text):
        url = match.group(0)
        if len(url) > DATA_URL_MAX_CHARS:
            found.append(("data_url", url))
    return found


def scan_payload(payload, where: str = "payload") -> list:
    """``(rule, where, trail, value)`` for a parsed structure.

    Walks with ``conftest.credential_strings_in``, the wide walker this repo
    already proved: it reaches dict KEYS as well as values, nested lists, sets,
    bytes and object reprs. A personal-data scanner that walked only top-level
    string values would miss the shapes measured in Section 2.
    """
    out = []
    for trail, text in credential_strings_in(payload):
        for rule, value in scan_text(text):
            out.append((rule, where, trail, value))
    return out


def scan_file(path) -> list:
    """Scan one file BOTH ways: as parsed JSON, and as raw text.

    Two passes because each sees what the other cannot.

    The STRUCTURAL pass gives a precise trail -- ``.body.contacts[0].email`` --
    which is what makes a finding fixable.

    The RAW pass is the backstop, and it is not redundant. It is the only pass
    that runs at all when a file does not parse, which must never be a silent
    skip -- a malformed capture is exactly the kind of artifact that gets
    committed unread. It is also the only pass that sees a value held as a JSON
    NUMBER: the wide walker deliberately skips plain scalars, so a mobile
    number written unquoted is structurally invisible and textually obvious.
    And it covers files that were never JSON in the first place.

    Decoded latin-1 so the read is byte-exact and cannot raise, the same
    convention the credential walker uses for ``bytes``.
    """
    path = pathlib.Path(path)
    try:
        where = str(path.relative_to(REPO)).replace("\\", "/")
    except ValueError:
        where = path.name

    raw = path.read_bytes().decode("latin-1")
    findings = []
    seen = set()

    parsed = _try_json(raw)
    if parsed is not _UNPARSED:
        for finding in scan_payload(parsed, where):
            findings.append(finding)
            seen.add((finding[0], finding[3]))

    for rule, value in scan_text(raw):
        if (rule, value) in seen:
            # The structural pass already reported this exact value with a
            # better trail. Reporting it twice buries the hits that only one
            # pass can see, which are the interesting ones.
            continue
        findings.append((rule, where, "<raw text>", value))
    return findings


def _try_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return _UNPARSED


def committed_fixtures() -> list:
    """Every file under the write-contract fixture directory, any extension."""
    if not WRITE_CONTRACT_DIR.is_dir():
        return []
    return sorted(p for p in WRITE_CONTRACT_DIR.rglob("*") if p.is_file())


# --- reporting --------------------------------------------------------------


def mask(rule: str, value: str) -> str:
    """Name the SHAPE and enough to locate it. Never the value.

    The domain survives because it is what tells the operator in one glance
    whether this is a real leak or a gap in the allowlist, and a domain is not
    a person. The local part -- which is -- does not survive.
    """
    if rule == "email":
        _, _, domain = value.rpartition("@")
        return "<EMAIL, %d chars, local-part masked, domain %r>" % (len(value), domain)
    if rule == "mobile":
        return "<INDIAN-MOBILE-SHAPED, %d chars, digits withheld>" % len(value)
    if rule == "data_url":
        return "<DATA-URL, %d chars, declared %r>" % (
            len(value),
            value.split(",", 1)[0][:40],
        )
    return "<REDACTED, %d chars>" % len(value)


def report_line(finding) -> str:
    """One masked line. The value is scrubbed out of the LOCATION too.

    A trail can carry the leak: ``credential_strings_in`` builds a dict key's
    trail out of the key itself, so an address used as a key would otherwise be
    reprinted verbatim in the very message that reports it.
    """
    rule, where, trail, value = finding
    return "%s -> [%s] %s = %s" % (
        where.replace(value, "<MASKED>"),
        rule,
        trail.replace(value, "<MASKED>"),
        mask(rule, value),
    )


def report(findings) -> str:
    """The failure message, already masked.

    Returned as a list of finished STRINGS for the caller to assert on, which
    is load-bearing rather than stylistic: pytest rewrites ``assert not X`` to
    include ``repr(X)`` in the message, so asserting on the raw findings would
    reprint every leaked value underneath the carefully masked text. Assert on
    the masked lines and the rewrite has nothing unsafe to print.
    """
    return [report_line(f) for f in findings]


def _header(count: int) -> str:
    return (
        "%d piece(s) of personal data reached a capture record. Capture fixtures "
        "are COMMITTED, so this is one push away from being published -- which is "
        "exactly how a sibling repo shipped twenty real recruiter addresses on "
        "2026-08-23. The values are MASKED below on purpose: a guard that "
        "reprints a leak into a CI log has published it a second time." % count
    )


def assert_no_personal_data(payload, where: str = "payload") -> None:
    """Fail if anything in ``payload`` is an address, a mobile, or a photo."""
    lines = report(scan_payload(payload, where))
    assert not lines, _header(len(lines)) + "\n  " + "\n  ".join(lines)


def scanner_sees(payload) -> bool:
    """True when the scanner would fire on ``payload``."""
    return bool(scan_payload(payload))


# ===========================================================================
# The naive detector, reconstructed
# ===========================================================================
#
# Not a strawman: this is what a "we check that nothing leaks" test looks like
# when it is written from memory of one incident. One address someone
# remembered, an ``in`` test, top-level string values only. It is frozen -- if
# someone "improves" it, the control below stops measuring the thing it exists
# to measure.

#: The one address the naive detector knows about. Assembled from fragments so
#: this file's own source stays clean under its own scanner.
NAIVE_KNOWN_ADDRESS = "recruiter" + "@" + "acme-corp." + "com"


def naive_detector_sees(payload) -> bool:
    """The one-literal, str-only, top-level-values-only detector, as it is."""
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(value, str) and NAIVE_KNOWN_ADDRESS in value
        for value in payload.values()
    )


# --- probe values, all assembled from fragments -----------------------------

REAL_LOOKING_EMAIL = "firstname.lastname" + "@" + "gmail." + "com"
SECOND_REAL_LOOKING_EMAIL = "real.person" + "@" + "gmail." + "com"
HOUSE_DOMAIN_EMAIL = "sundeep" + "@" + "instahyre." + "com"
PERMITTED_TWIN = "firstname.lastname@example.invalid"

REAL_LOOKING_MOBILE = "98765" + "43210"
REAL_LOOKING_MOBILE_E164 = "+91 " + REAL_LOOKING_MOBILE

#: Ten digits that are NOT an Indian mobile: the rule is anchored on an opening
#: 6-9, so this one is safe to write whole.
NOT_A_MOBILE = "1234567890"

#: A photo-sized ``data:`` URL. Built by repetition rather than pasted, both to
#: keep this file clean under its own scanner and so that its length is the
#: only property under test.
BIG_DATA_URL = "data:image/png;base64," + "A" * 240
SMALL_DATA_URL = "data:image/png;base64," + "A" * 40


_COMMITTED_FIXTURES = committed_fixtures()
_FIXTURE_IDS = [
    str(p.relative_to(WRITE_CONTRACT_DIR)).replace("\\", "/")
    for p in _COMMITTED_FIXTURES
]


# ===========================================================================
# 1. The scanner, over the real committed fixtures
# ===========================================================================


class TestTheCommittedFixtures:

    def test_the_scanner_has_something_to_scan(self):
        """The refusal to certify nothing.

        Every per-file test below is parametrized, and an empty parametrize
        list SKIPS rather than fails -- so a directory that lost its contents,
        or moved, would produce a green run over zero evidence. This is the
        assertion that cannot be satisfied that way.
        """
        assert WRITE_CONTRACT_DIR.is_dir(), (
            "the write-contract fixture directory is missing at %s -- if the "
            "captures moved, this guard moved with them or it is guarding "
            "nothing" % WRITE_CONTRACT_DIR
        )
        files = committed_fixtures()
        assert files, (
            "%s holds no files. A scan that walks zero files and reports CLEAN "
            "is not evidence." % WRITE_CONTRACT_DIR
        )

    @pytest.mark.parametrize("path", _COMMITTED_FIXTURES, ids=_FIXTURE_IDS)
    def test_the_fixture_is_not_empty(self, path):
        """An empty file scans clean for the wrong reason."""
        assert path.read_bytes(), "%s is empty" % path

    @pytest.mark.parametrize("path", _COMMITTED_FIXTURES, ids=_FIXTURE_IDS)
    def test_no_personal_data_in_this_fixture(self, path):
        lines = report(scan_file(path))
        assert not lines, _header(len(lines)) + "\n  " + "\n  ".join(lines)

    def test_the_whole_directory_sweeps_clean(self):
        """The same scan as one statement, so the count is visible.

        The parametrized test above is the better bug report; this one is the
        one that cannot be quietly reduced to zero cases.
        """
        files = committed_fixtures()
        assert files
        findings = []
        for path in files:
            findings.extend(scan_file(path))
        lines = report(findings)
        assert not lines, _header(len(lines)) + "\n  " + "\n  ".join(lines)


# ===========================================================================
# 2. The controls
# ===========================================================================


class TestTheScannerDiscriminates:

    def test_the_scanner_fires_on_a_real_looking_address__CONTROL(self):
        """Both halves in one test.

        A scanner is only useful if it says CLEAN as readily as it says DIRTY.
        Splitting these across two tests lets one rot while the other passes;
        together, a scanner that has stopped discriminating in either direction
        fails here.
        """
        dirty = {"body": {"invites": [{"to": REAL_LOOKING_EMAIL}]}}
        clean = {"body": {"invites": [{"to": PERMITTED_TWIN}]}}

        assert scanner_sees(dirty), (
            "a gmail-shaped address in a committed capture is the whole reason "
            "this file exists"
        )
        assert_no_personal_data(clean)

    def test_the_allowlist_is_a_domain_allowlist__CONTROL(self):
        """It permits reserved domains, not plausible ones.

        The failure mode an allowlist invites is quiet widening: one entry for
        a domain that looked harmless, and the tripwire is off for every
        address on it. A real-person address and an address on this platform's
        own domain both fail, and neither is a special case in the code -- they
        fail because their domains are simply not in the set.
        """
        for address in (SECOND_REAL_LOOKING_EMAIL, HOUSE_DOMAIN_EMAIL):
            assert not email_is_permitted(address)
            assert scanner_sees({"contact": address})

        for address in (
            "not-a-real-person@example.invalid",
            "redacted@example.invalid",
            "someone@example.com",
            "someone@example.org",
            "someone@example.net",
            "someone@test.invalid",
        ):
            assert email_is_permitted(address)

        # A suffix test would let this through. An exact domain match does not.
        # Assembled from fragments like every other real-looking literal here:
        # a registrable domain written whole in this file would trip the
        # self-scan below, which is how that control found it.
        assert not email_is_permitted(
            "someone" + "@" + "mail.example.com." + "attacker." + "net"
        )

    def test_a_naive_substring_detector_could_not_see_these__CONTROL(self):
        """Three shapes measured invisible to the detector this replaces.

        The first two are SHAPE holes: the naive walk reads top-level string
        values, so the very address it is hunting sails past inside a nested
        list, or as a dict key. The third is the LITERAL hole: a detector built
        around one remembered address is blind to every other one, however
        plainly it is written. This scanner hunts the shape of an address, not
        a value someone remembered, which is why all three fire.
        """
        nested = {"body": {"invites": [{"to": NAIVE_KNOWN_ADDRESS}]}}
        as_a_key = {NAIVE_KNOWN_ADDRESS: "sent"}
        in_prose = {
            "note": "recruiter asked us to follow up with %s about the role"
            % SECOND_REAL_LOOKING_EMAIL
        }

        for label, payload in (
            ("nested in a list inside a dict", nested),
            ("present only as a dict key", as_a_key),
            ("embedded in a longer sentence", in_prose),
        ):
            assert not naive_detector_sees(payload), (
                "the naive detector was supposed to be blind to an address %s; "
                "if it now sees one, this control has stopped measuring the "
                "hole it exists for" % label
            )
            assert scanner_sees(payload), (
                "the scanner missed an address %s" % label
            )

        # And the naive detector is not a strawman that sees nothing: hand it
        # the one shape it was built for and it does fire.
        assert naive_detector_sees({"to": NAIVE_KNOWN_ADDRESS})

    def test_scrub_is_what_stands_between_the_capture_and_the_fixture__CONTROL(self):
        """The scrubber is load-bearing, measured rather than assumed.

        This is the one control that couples the two halves of the system: the
        INPUT is what the browser hands the capture script, the OUTPUT is what
        gets committed, and the scanner is pointed at both. If the input did
        not fail, the scrubber would be decorative; if the output did not pass,
        it would be broken.

        ``EXTRA_SCRUB_TERMS`` is asserted empty so the pass is credited to the
        built-in regexes. That map is filled only by the script's ``main()``
        from ``--scrub-term`` flags, so it is empty on any run where the
        operator forgot them -- which is the run this tripwire exists for.
        """
        assert capture.EXTRA_SCRUB_TERMS == {}, (
            "a test populated the capture script's extra-terms map and did not "
            "restore it; this control would then be measuring the term map "
            "rather than the scrubber's own patterns"
        )

        before = {
            "body": {
                "candidate": "/api/v1/candidate_misc/profile/limited_candidate/1",
                "contacts": [{"email": REAL_LOOKING_EMAIL}],
                "mobile": REAL_LOOKING_MOBILE_E164,
                "note": "call %s or write to %s"
                % (REAL_LOOKING_MOBILE, SECOND_REAL_LOOKING_EMAIL),
            }
        }

        assert scanner_sees(before), (
            "the input was supposed to be dirty; if it is not, this control "
            "certifies the scrubber against a payload that never needed it"
        )

        after = capture.scrub(before)
        assert_no_personal_data(after, where="scrub() output")

        # And the scrubber really transformed it, rather than the scanner
        # having gone quiet: the redacted spellings are present and the
        # originals are gone from the serialised result.
        text = json.dumps(after)
        assert "redacted@example.invalid" in text
        assert "<PHONE_REDACTED>" in text
        assert REAL_LOOKING_EMAIL not in text
        assert REAL_LOOKING_MOBILE not in text

    def test_the_scanner_does_not_fire_on_the_permitted_fakes__CONTROL(self):
        """The false-positive half. A scanner that cries wolf gets muted.

        Including this file itself. Every capture recipe fills its forms with
        ``example.invalid`` addresses on purpose, and if those tripped the
        guard the guard would be turned off within a week.
        """
        assert_no_personal_data(
            {
                "invite_emails": capture.FAKE_INVITE_EMAILS,
                "placeholder": "redacted@example.invalid",
                "message": capture.FAKE_MESSAGE,
                "phone": "<PHONE_REDACTED>",
                "photo": "data:<REDACTED_DATA_URL len=771>",
                "token": "<TOKEN_REDACTED>",
            }
        )

        assert "not-a-real-person@example.invalid" in capture.FAKE_INVITE_EMAILS, (
            "the capture recipes stopped using reserved-domain addresses -- "
            "this control is now certifying a payload that no longer resembles "
            "what a capture produces"
        )

        lines = report(scan_file(pathlib.Path(__file__).resolve()))
        assert not lines, (
            "this guard's own source trips its own rules, which is how a guard "
            "gets muted:\n  " + "\n  ".join(lines)
        )

    def test_the_mobile_rule_discriminates__CONTROL(self):
        """Ten digits is not enough to be a phone number.

        Without the opening 6-9 anchor the rule would fire on any ten-digit
        identifier in a captured body, and a scanner that fires on ids is one
        that gets an allowlist entry per fixture until it means nothing.
        """
        assert scanner_sees({"mobile": REAL_LOOKING_MOBILE})
        assert scanner_sees({"mobile": REAL_LOOKING_MOBILE_E164})
        assert scanner_sees({"note": "reach me on %s any time" % REAL_LOOKING_MOBILE})

        assert_no_personal_data({"id": NOT_A_MOBILE})
        assert_no_personal_data({"mobile": "<PHONE_REDACTED>"})

        # And the anchors hold: a longer digit run must not supply a window.
        assert_no_personal_data({"id": "1" + REAL_LOOKING_MOBILE + "1"})

    def test_the_data_url_rule_discriminates_by_length__CONTROL(self):
        """A photo smuggled in as base64 is the third thing a capture leaks.

        The profile-image recipe hands the page a 1x1 fake precisely so the
        recorded body is small; a real photograph through the same path is
        thousands of characters. Length is the only signal that separates them,
        and the scrubber's own redaction stops five characters in -- which is
        what makes the redacted form pass.
        """
        assert scanner_sees({"file_b64": BIG_DATA_URL})
        assert len(BIG_DATA_URL) > DATA_URL_MAX_CHARS

        assert_no_personal_data({"file_b64": SMALL_DATA_URL})
        assert_no_personal_data({"file_b64": "data:<REDACTED_DATA_URL len=771>"})

    def test_the_raw_pass_sees_what_the_structural_pass_cannot__CONTROL(
        self, tmp_path
    ):
        """Why every file is scanned twice.

        A mobile number written as a JSON NUMBER is structurally invisible: the
        wide walker skips plain scalars, deliberately, because rendering every
        int would fill a failure message with noise. The raw-text pass sees it
        anyway. A file that does not parse at all is the same case in its worst
        form -- there is no structure to walk, and a silent skip would be a
        clean bill of health for an unread artifact.
        """
        number = tmp_path / "as_a_number.json"
        number.write_text('{"mobile": %s}' % REAL_LOOKING_MOBILE, encoding="ascii")

        parsed = json.loads(number.read_text(encoding="ascii"))
        assert not scan_payload(parsed), (
            "the structural walk was supposed to skip a bare number; if it now "
            "reaches one, this control has stopped measuring the raw pass"
        )
        assert scan_file(number), "the raw pass missed a mobile held as a number"

        broken = tmp_path / "truncated.json"
        broken.write_text(
            '{"body": {"email": "%s"' % REAL_LOOKING_EMAIL, encoding="ascii"
        )
        assert _try_json(broken.read_text(encoding="ascii")) is _UNPARSED
        assert scan_file(broken), (
            "a file that does not parse was skipped silently -- an unread "
            "capture is the one most likely to be carrying something"
        )


# ===========================================================================
# 3. Reporting a leak must not be a second disclosure
# ===========================================================================


class TestTheFailureMessage:

    def test_the_message_does_not_reprint_the_address(self):
        """A CI log outlives the run and the operator cannot redact it.

        The message has to be enough to FIND and FIX -- which file, which
        field, which rule, and the domain that says whether this is a leak or
        an allowlist gap. The local part is the person, and it does not appear.
        """
        payload = {"body": {"contacts": [{"email": REAL_LOOKING_EMAIL}]}}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_personal_data(payload, where="write_contracts/probe.json")
        message = str(excinfo.value)

        assert REAL_LOOKING_EMAIL not in message, (
            "the guard published the address a second time while reporting it"
        )
        assert "write_contracts/probe.json" in message
        assert ".body.contacts[0].email" in message
        assert "local-part masked" in message
        assert "gmail.com" in message

    def test_the_message_masks_an_address_that_is_a_dict_key(self):
        """The trail is built out of the key, so the trail can carry the leak.

        This is the shape that defeats masking the value alone: the location
        string itself becomes a copy of the address. Found here rather than in
        production, which is the point of writing the control before trusting
        the message.
        """
        payload = {"sent": {REAL_LOOKING_EMAIL: "2026-08-23"}}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_personal_data(payload)
        message = str(excinfo.value)

        assert REAL_LOOKING_EMAIL not in message
        assert "<MASKED>" in message

    def test_the_message_withholds_a_mobile_and_a_photo_too(self):
        """The same rule for the other two detectors.

        A ten-digit number printed into a log is as published as an address,
        and a base64 photograph echoed into one is worse -- it is the whole
        artifact, not a reference to it.
        """
        with pytest.raises(AssertionError) as excinfo:
            assert_no_personal_data({"mobile": REAL_LOOKING_MOBILE})
        message = str(excinfo.value)
        assert REAL_LOOKING_MOBILE not in message
        assert "INDIAN-MOBILE-SHAPED" in message

        with pytest.raises(AssertionError) as excinfo:
            assert_no_personal_data({"file_b64": BIG_DATA_URL})
        message = str(excinfo.value)
        assert BIG_DATA_URL not in message
        assert "DATA-URL" in message
        assert "image/png" in message, (
            "the declared type is safe and diagnostic; withholding it makes the "
            "finding harder to act on for no gain"
        )

    def test_the_message_survives_pytest_assertion_rewriting(self):
        """The masking has to hold through the rewrite, not just the string.

        MEASURED, not assumed. pytest rewrites ``assert not X, msg`` into
        ``msg`` followed by a line reading ``assert not <repr of X>``, so the
        operand is reprinted underneath the carefully masked text whether the
        author wants it or not. Asserting on the raw findings would therefore
        publish every leaked value on the line below the mask. That is why
        :func:`report` returns finished, already-masked strings and the
        assertion is made on those -- the rewrite then has nothing unsafe left
        to print.

        The first assertion proves the rewrite really is appending the operand
        here; without it this test would pass on a plain ``AssertionError`` and
        measure nothing.
        """
        payload = {"a": REAL_LOOKING_EMAIL, "b": REAL_LOOKING_MOBILE}
        with pytest.raises(AssertionError) as excinfo:
            assert_no_personal_data(payload)
        message = str(excinfo.value)

        assert "assert not [" in message, (
            "assertion rewriting did not append the operand, so this test is "
            "not measuring the hazard it was written for"
        )
        assert REAL_LOOKING_EMAIL not in message
        assert REAL_LOOKING_MOBILE not in message
