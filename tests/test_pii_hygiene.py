"""Personal-data hygiene guard over every tracked file in this repository.

WHY THIS FILE HUNTS BY SHAPE AND ALLOWLISTS THE SYNTHETIC
---------------------------------------------------------
The check this replaces was a committed list of real literal strings. A list
of real values IS a de-anonymisation key: it leaks exactly what it claims to
protect, and every reader of the repo gets a copy. So this module inverts the
polarity. It carries NO real value anywhere. It matches PII by SHAPE and then
lets a match through only when the value is provably fake -- a reserved
domain that can never resolve, an all-zero number, a slug carrying a
synthetic token. Fake values are safe to commit; real ones are not, so only
fake ones appear below.

Two consequences worth stating out loud:

  * When a check fires on something that is genuinely already synthetic, the
    repair is to WIDEN AN ALLOWLIST, never to narrow a shape and never to
    delete a check. Every widening below names the class it exists for.
  * When a check fires on something real, the repair is to fix the DATA. Do
    not add the value here. Adding it would rebuild the key.

Assertion messages never print a full identifier. CI logs are readable by
anyone who can read the build, so a guard that prints what it found has
merely moved the leak.

WHAT THIS GUARD COVERS
----------------------
Eight checks, in two families.

SHAPED VALUES (checks 1-5). Things a human recognises on sight: an email
address, a phone number, a LinkedIn profile slug, an opaque LinkedIn company
id or member urn, a JWT or session cookie. Check 6 is structural rather than
shaped: it hunts the pair table that would reverse any redaction.

SHAPELESS IDENTIFIERS (checks 7-8). Added after a sibling repository was
scrubbed, certified clean by a guard built exactly like this one, and still
published a live account primary key. It survived because it has NO SHAPE --
no at-sign, no dialling code, no human name, just an opaque integer sitting
next to an account-ish key. Every one of checks 1-5 would pass it. Check 7
therefore keys on the KEY PATH rather than the value, and demands that an
account-scoped record id be provably synthetic. Check 8 is its wider,
shape-free net for anything token-like standing next to an account-ish key.

WHAT THIS GUARD DOES NOT COVER
------------------------------
Stated plainly, because a guard that is silent about its blind spots buys a
green tick with someone else's privacy.

  * PERSONAL NAMES. A name has no shape. "Priya Sharma" and "Fairmount
    Institute" are the same regex, and so are a real name and an invented
    one. Nothing here can tell them apart, and nothing here tries. Names are
    scrubbed by hand and verified by a human reading the diff.

  * THE RE-IDENTIFICATION TUPLE. Employer, institute, graduation year, city
    and skill list are each individually innocuous and jointly a fingerprint
    that names one person. This module checks values one at a time and so is
    constitutionally unable to see a tuple. A fixture can pass all eight
    checks and still identify its subject.

  * SHARED-TAXONOMY ROWS THAT POINT AT A BIOGRAPHY -- PARTLY. A university,
    degree, specialisation or language id is a row in a table shared by every
    user, so the key-path test that separates account-scoped from public would
    let all four through. But such a row is CHOSEN by the account holder, which
    makes it a reverse lookup back into the tuple above: scrubbing an institute
    NAME while leaving its numeric id in place undoes the scrub for anyone who
    can resolve the id.
    `universities` was moved into scope on 2026-08-24 and is now checked. The
    order that made it possible is the transferable part: the DATA was fixed
    first, so a synthetic value existed to allowlist; only then could the id be
    guarded. Fix the data, then move the id -- never the reverse, or the check
    goes red with nothing to admit and gets reverted.
    `degrees`, `specializations` and `language` are still let through, and that
    is a decision rather than an oversight: their cardinality is tiny, so a
    degree id is shared with millions and re-identifies nobody. The institute
    was the sharp one, because a small institute in one year is a small cohort.

  * TOKENS SEPARATED FROM THEIR KEY BY A NEWLINE. Check 8 reads one line at a
    time, so a token whose account-ish key sits on a previous line is not
    seen. The fixtures here are pretty-printed one key per line, which is why
    this is affordable today.

FILE LIST
---------
`git ls-files` from the repository root, so a file is covered the day it is
added rather than the day someone remembers to extend a hardcoded list.
Binary suffixes are skipped, as is this module itself -- it is full of
regexes that look exactly like the shapes it hunts.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# Repository walk
# --------------------------------------------------------------------------

BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".db", ".ico",
        ".woff", ".woff2", ".ttf", ".pyc", ".so", ".dll", ".exe", ".whl",
    }
)

#: Dependency pins and lock files are dense with base16/base64 hashes that
#: collide with several shapes below. They hold no personal data by
#: construction: a hash is not a person.
LOCK_FILENAMES = frozenset(
    {"package-lock.json", "poetry.lock", "uv.lock", "cargo.lock", "yarn.lock"}
)

#: A 40-hex git SHA, which contains long digit runs that look like phones.
GIT_SHA40 = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")


def _repo_root() -> Path:
    """The directory holding .git, found by walking up from this file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError(
        "test_pii_hygiene: no .git found above %s -- the guard cannot "
        "enumerate tracked files and must not silently pass." % here
    )


@functools.lru_cache(maxsize=1)
def _tracked_text_files():
    """(relpath, Path, text) for every tracked, non-binary, readable file.

    Raises rather than degrading. A hygiene guard that quietly scans nothing
    is worse than no guard at all: it manufactures a green tick.
    """
    root = _repo_root()
    self_rel = Path(__file__).resolve().relative_to(root).as_posix()
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "test_pii_hygiene: `git ls-files` failed in %s (exit %d): %s"
            % (root, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        )
    out = []
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", "surrogateescape").strip()
        if not rel or rel == self_rel:
            continue
        path = root / rel
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            continue
        out.append((rel, path, data.decode("utf-8", "replace")))
    return tuple(out)


def _is_pins_or_lock(rel: str) -> bool:
    name = Path(rel).name.lower()
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    if name.endswith(".lock"):
        return True
    return name in LOCK_FILENAMES


def _iter_lines(skip=None):
    """Yield (relpath, lineno, line) over the tracked text corpus."""
    for rel, _path, text in _tracked_text_files():
        if skip is not None and skip(rel):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            yield rel, lineno, line


# --------------------------------------------------------------------------
# Redaction -- assertion messages must never carry a full identifier
# --------------------------------------------------------------------------


def _fingerprint(value: str) -> str:
    kinds = []
    if any(c.isdigit() for c in value):
        kinds.append("digits")
    if any(c.isalpha() for c in value):
        kinds.append("letters")
    if any((not c.isalnum()) and (not c.isspace()) for c in value):
        kinds.append("punct")
    return "<%d chars, %s>" % (len(value), "+".join(kinds) or "empty")


def _redact(value: str) -> str:
    """First two characters, an ellipsis, the last two. Nothing else."""
    text = str(value)
    if len(text) >= 6:
        return "%s...%s" % (text[:2], text[-2:])
    return _fingerprint(text)


def _report(hits) -> str:
    return "\n".join("  " + h for h in hits)


# --------------------------------------------------------------------------
# Check 1 -- email shape
# --------------------------------------------------------------------------

EMAIL_SHAPE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")

#: RFC 2606 / RFC 6761 reserve these. Nothing here can ever resolve or reach
#: a person, so an address in this space is safe to commit by construction.
RESERVED_EXACT_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "localhost"}
)
RESERVED_DOMAIN_SUFFIXES = (
    ".invalid",          # reserved TLD
    ".example.com",      # WIDENED: subdomains of the reserved names are
    ".example.org",      # reserved too, and the suites use them for
    ".example.net",      # adversary/victim roles (attacker@evil.example.com)
)

#: Full domains used as unit-test stubs, kept explicit so the set stays
#: auditable. Every entry is a value that cannot belong to anybody.
#:   x.com, b.co        -- shortest-possible-address regex probes
#:   example.invalid.co -- WIDENED: a deliberate near-miss fixture proving
#:                         that ".invalid.co" is not the ".invalid" TLD.
STUB_EMAIL_DOMAINS = frozenset({"x.com", "b.co", "example.invalid.co"})


def _email_domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].rstrip(".").lower()


def _email_allowed(addr: str) -> bool:
    domain = _email_domain(addr)
    if domain in RESERVED_EXACT_DOMAINS or domain in STUB_EMAIL_DOMAINS:
        return True
    return domain.endswith(RESERVED_DOMAIN_SUFFIXES)


def test_no_real_email_addresses():
    """No address outside the reserved/synthetic domain space."""
    hits = []
    for rel, lineno, line in _iter_lines(skip=_is_pins_or_lock):
        for match in EMAIL_SHAPE.finditer(line):
            addr = match.group(0)
            if _email_allowed(addr):
                continue
            hits.append(
                "%s:%d  EMAIL  %s  (domain %s not reserved/allowlisted)"
                % (rel, lineno, _redact(addr), _redact(_email_domain(addr)))
            )
    assert not hits, (
        "Email-shaped values at non-synthetic domains are tracked in this "
        "repo. Replace the DATA with a .invalid / example.* address; only "
        "widen STUB_EMAIL_DOMAINS for a domain that provably belongs to "
        "nobody.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 2 -- phone shape
# --------------------------------------------------------------------------

PHONE_IN_SHAPE = re.compile(r"(?<![\d.])(?:\+?91[-\s]?)?[6-9]\d{9}(?![\d.])")
PHONE_E164_SHAPE = re.compile(r"\+\d{1,3}[-\s]?\d{6,12}")

#: Numbers reserved by convention for documentation and tests.
CLASSIC_TEST_NUMBERS = frozenset({"9876543210", "1000000000", "0000000000"})

#: WIDENED -- numeric-id contexts that collide with the ten-digit Indian
#: mobile shape. Job-board posting ids and opportunity ids run to ten digits
#: and start with 6-9 just as an Indian mobile does, so they fire on shape
#: alone. Each alternative below exists because an already-committed,
#: NON-PERSONAL identifier fired in that position:
#:   1. a posting id in a URL query string   ...?gh_jid=<10 digits>
#:   2. a REST resource path segment         /api/v1/.../<resource>/<id>
#:   3. the value of a key whose NAME ends in an id-ish token
#:                                           "id": <id>, "..._id": "<id>"
#: These allow a CONTEXT, never a value, so no identifier is recorded here
#: -- which is the whole point of this module. The known cost: a phone
#: hidden under a key named "*_id" would pass. A phone under any honest key
#: name (phone, mobile, contact, number) still fires.
PHONE_ID_CONTEXT = re.compile(
    r"(?:"
    r"[?&][A-Za-z0-9_]{0,24}(?:jid|job_id|jobid|posting_id|req_id)="
    r"|/[A-Za-z0-9_\-]+/(?:[A-Za-z0-9_\-]+/)*"
    r"|[\"']?[A-Za-z0-9_]*(?:id|uri|opp)[\"']?\s*[:=]\s*[\"']?"
    r")$",
    re.IGNORECASE,
)


def _phone_allowed(match_text: str) -> bool:
    digits = re.sub(r"\D", "", match_text)
    if not digits:
        return True
    candidates = {digits}
    for cc_len in (1, 2, 3):
        if len(digits) > cc_len:
            candidates.add(digits[cc_len:])
    for candidate in candidates:
        if candidate and set(candidate) == {"0"}:
            return True
        if candidate in CLASSIC_TEST_NUMBERS:
            return True
    return False


def _phone_line_skipped(rel: str, line: str) -> bool:
    return rel.lower().endswith(".md") and bool(GIT_SHA40.search(line))


def test_no_real_phone_numbers():
    """No phone-shaped run outside the zeroed/classic/known-id space."""
    hits = []
    for rel, lineno, line in _iter_lines(skip=_is_pins_or_lock):
        if _phone_line_skipped(rel, line):
            continue
        for name, pattern in (
            ("PHONE-IN", PHONE_IN_SHAPE),
            ("PHONE-E164", PHONE_E164_SHAPE),
        ):
            for match in pattern.finditer(line):
                value = match.group(0)
                if _phone_allowed(value):
                    continue
                if PHONE_ID_CONTEXT.search(line[: match.start()]):
                    continue
                hits.append("%s:%d  %s  %s" % (rel, lineno, name, _redact(value)))
    assert not hits, (
        "Phone-shaped values are tracked in this repo. Replace the DATA "
        "with an all-zero or classic test number; widen "
        "CLASSIC_TEST_NUMBERS / PHONE_ID_CONTEXT only for a value that is "
        "provably not a person's number.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 3 -- LinkedIn personal slug
# --------------------------------------------------------------------------

LINKEDIN_SLUG = re.compile(r"(?:linkedin\.com)?/in/([A-Za-z0-9\-_%]{3,})")

#: A slug carrying one of these reads as obviously invented to any human,
#: which is the whole test. "fake" is deliberately NOT here: it is a token
#: the shown-failing demonstration plants with, and a guard that allows its
#: own probe cannot be shown failing.
SYNTHETIC_SLUG_TOKENS = (
    "test",
    "someone",
    "somebody",
    "example",
    "anonymous",
    "a-real-person",
    "another-person",
    "candidate",
    "placeholder",
    "redacted",  # WIDENED: the capture scripts rewrite every contact to
                 # /in/redacted-contact-<n> before anything is written down.
)

#: Known-fake slugs that carry no synthetic token. Each repo adds its own
#: here as they appear. Empty is correct until one does.
SYNTHETIC_SLUGS = frozenset()


def _has_synthetic_token(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in SYNTHETIC_SLUG_TOKENS)


def _slug_allowed(slug: str) -> bool:
    if slug in SYNTHETIC_SLUGS:
        return True
    return _has_synthetic_token(slug)


def test_no_personal_linkedin_slugs():
    """No /in/<slug> that could name a real person."""
    hits = []
    for rel, lineno, line in _iter_lines():
        for match in LINKEDIN_SLUG.finditer(line):
            slug = match.group(1)
            if _slug_allowed(slug):
                continue
            hits.append("%s:%d  LINKEDIN-SLUG  %s" % (rel, lineno, _redact(slug)))
    assert not hits, (
        "LinkedIn profile slugs that could name a real person are tracked "
        "in this repo. Rewrite the DATA to a slug carrying a synthetic "
        "token.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 4 -- LinkedIn numeric company id and member URN
# --------------------------------------------------------------------------

LINKEDIN_COMPANY_ID = re.compile(r"(?:/company/|currentCompany=|companyId=)(\d{3,})")
LINKEDIN_MEMBER_TOKEN = re.compile(r"ACoAA[A-Za-z0-9_\-]{10,}")
LINKEDIN_URN_ID = re.compile(r"urn:li:[a-zA-Z]+:\(?(\d{6,})")

#: Known-fake LinkedIn ids. Each repo adds its OWN known-fake ids here as
#: they appear -- an id is only ever admitted after someone confirms it
#: names nothing. There are none in this repo today, so the empty set is
#: correct and the check is live rather than decorative.
SYNTHETIC_LINKEDIN_IDS = frozenset()


def test_no_linkedin_numeric_ids():
    """No opaque LinkedIn company id, member token, or numeric URN."""
    hits = []
    for rel, lineno, line in _iter_lines():
        for name, pattern, group in (
            ("LI-COMPANY-ID", LINKEDIN_COMPANY_ID, 1),
            ("LI-MEMBER-TOKEN", LINKEDIN_MEMBER_TOKEN, 0),
            ("LI-URN-ID", LINKEDIN_URN_ID, 1),
        ):
            for match in pattern.finditer(line):
                value = match.group(group)
                if value in SYNTHETIC_LINKEDIN_IDS:
                    continue
                hits.append("%s:%d  %s  %s" % (rel, lineno, name, _redact(value)))
    assert not hits, (
        "Opaque LinkedIn identifiers are tracked in this repo. They name a "
        "real company or member even though they read as noise. Remove the "
        "DATA, or add the id to SYNTHETIC_LINKEDIN_IDS only once it is "
        "confirmed invented.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 5 -- opaque credential and session-token shapes
# --------------------------------------------------------------------------

JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
COOKIE_ASSIGNMENT = re.compile(
    r"(li_at|JSESSIONID|li_rm|bcookie|bscookie|nauk_at|sessionid|csrftoken)"
    r"\s*[=:]\s*[\"']?(\S{20,})"
)

#: A value carrying one of these is a stand-in, not a credential.
PLACEHOLDER_MARKERS = (
    "xxx",
    "dummy",
    "fake",
    "redacted",
    "placeholder",
    "<",
    "...",
)


def _credential_allowed(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    stripped = value.strip("\"'")
    return bool(stripped) and len(set(stripped)) == 1


def test_no_credential_or_session_tokens():
    """No JWT and no session-cookie assignment carrying a live value."""
    hits = []
    for rel, lineno, line in _iter_lines(skip=_is_pins_or_lock):
        for match in JWT_SHAPE.finditer(line):
            value = match.group(0)
            if _credential_allowed(value):
                continue
            hits.append("%s:%d  JWT  %s" % (rel, lineno, _redact(value)))
        for match in COOKIE_ASSIGNMENT.finditer(line):
            value = match.group(2)
            if _credential_allowed(value):
                continue
            hits.append(
                "%s:%d  COOKIE[%s]  %s"
                % (rel, lineno, match.group(1), _fingerprint(value))
            )
    assert not hits, (
        "Credential-shaped values are tracked in this repo. Rotate the "
        "secret first, then replace the DATA with an obvious "
        "placeholder.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 6 -- mapping table / de-anonymisation key
# --------------------------------------------------------------------------
#
# The one that would have caught the incident. It is STRUCTURAL, not
# keyword-driven: a de-anonymisation key is recognised by being a table of
# pairs whose LEFT column holds real-value-shaped strings, whatever it is
# called. The variable name only lowers the threshold; it never carries the
# finding on its own.

BARE_INT_4PLUS = re.compile(r"^\d{4,}$")
TITLECASE_WORDS = re.compile(r"^[A-Z][a-z'\-]+(?: [A-Z][a-z'\-]+)+$")
LOWER_SLUG_2SEG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")

SUSPICIOUS_TABLE_NAME = re.compile(
    r"(?i)(mask|map|subst|replac|anon|scrub|sanit|real|alias|rename"
    r"|forbidden|slugs|operator|posting|employers)"
)

MIN_PAIRS_STRUCTURAL = 3
MIN_REAL_LEFTS_STRUCTURAL = 3
MIN_PAIRS_BY_NAME = 5
#: WIDENED -- the name-triggered rule additionally requires at least one
#: real-value-shaped left. Without it the rule fires on ordinary schema
#: maps whose left column is a field NAME rather than a value (a scrubber's
#: field -> placeholder map, a shared-key -> local-field map). Those hold no
#: values at all, so they cannot be a de-anonymisation key. A table with the
#: same suspicious name that DOES hold one real-shaped value still fires,
#: two pairs below the structural threshold.
MIN_REAL_LEFTS_BY_NAME = 1


def _shape_of_real_value(text: str):
    """Name the PII/proper-noun shape of `text`, or None if it looks fake.

    Order matters. An explicit PII shape that the allowlists accept returns
    None immediately: an allowlisted test number must not fall through and
    be re-flagged as a bare integer.
    """
    value = text.strip()
    if not value:
        return None
    if _has_synthetic_token(value):
        return None

    matched_pii = False
    for shape, pattern, allowed in (
        ("email shape", EMAIL_SHAPE, lambda m: _email_allowed(m.group(0))),
        ("phone shape", PHONE_IN_SHAPE, lambda m: _phone_allowed(m.group(0))),
        ("E.164 shape", PHONE_E164_SHAPE, lambda m: _phone_allowed(m.group(0))),
        ("linkedin slug", LINKEDIN_SLUG, lambda m: _slug_allowed(m.group(1))),
        (
            "linkedin company id",
            LINKEDIN_COMPANY_ID,
            lambda m: m.group(1) in SYNTHETIC_LINKEDIN_IDS,
        ),
        (
            "linkedin member token",
            LINKEDIN_MEMBER_TOKEN,
            lambda m: m.group(0) in SYNTHETIC_LINKEDIN_IDS,
        ),
        (
            "linkedin urn id",
            LINKEDIN_URN_ID,
            lambda m: m.group(1) in SYNTHETIC_LINKEDIN_IDS,
        ),
        ("jwt", JWT_SHAPE, lambda m: _credential_allowed(m.group(0))),
        (
            "session cookie",
            COOKIE_ASSIGNMENT,
            lambda m: _credential_allowed(m.group(2)),
        ),
    ):
        match = pattern.search(value)
        if match is None:
            continue
        matched_pii = True
        if not allowed(match):
            return shape
    if matched_pii:
        return None

    if BARE_INT_4PLUS.match(value):
        return "bare integer"
    if TITLECASE_WORDS.match(value):
        return "Titlecase words"
    if LOWER_SLUG_2SEG.match(value):
        return "lowercase slug"
    return None


def _string_pairs(node):
    """String (left, right) pairs from a list/tuple of tuples, or a dict."""
    pairs = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            if not isinstance(element, (ast.Tuple, ast.List)):
                continue
            if len(element.elts) < 2:
                continue
            left, right = element.elts[0], element.elts[1]
            if (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and isinstance(right, ast.Constant)
                and isinstance(right.value, str)
            ):
                pairs.append((left.value, right.value))
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                pairs.append((key.value, value.value))
    return pairs


def _assignments(scope):
    """(name, value_node, lineno) for each assignment directly in `scope`."""
    for statement in scope.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        yield (names[0] if names else "<unnamed>"), statement.value, statement.lineno


def test_no_mapping_table_of_real_values():
    """No committed pair table that reverses a redaction.

    Reports the variable name, its location, and the pair count only. The
    values are never printed: printing them into a CI log recreates exactly
    the leak this check exists to prevent.
    """
    hits = []
    for rel, _path, text in _tracked_text_files():
        if not rel.lower().endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            continue

        scopes = [(tree, True)]
        scopes += [
            (node, False) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]
        for scope, is_module_level in scopes:
            for name, value, lineno in _assignments(scope):
                pairs = _string_pairs(value)
                if not pairs:
                    continue
                shapes = [
                    shape
                    for shape in (_shape_of_real_value(left) for left, _ in pairs)
                    if shape is not None
                ]
                structural = (
                    len(pairs) >= MIN_PAIRS_STRUCTURAL
                    and len(shapes) >= MIN_REAL_LEFTS_STRUCTURAL
                )
                by_name = (
                    is_module_level
                    and len(pairs) >= MIN_PAIRS_BY_NAME
                    and len(shapes) >= MIN_REAL_LEFTS_BY_NAME
                    and bool(SUSPICIOUS_TABLE_NAME.search(name))
                )
                if not (structural or by_name):
                    continue
                sample = ", ".join(sorted({"<%s>" % shape for shape in shapes})[:3])
                hits.append(
                    "%s:%d  MAPPING-TABLE  %s  pairs=%d  real_shaped_lefts=%d"
                    "  rule=%s  left-column shapes: %s"
                    % (
                        rel,
                        lineno,
                        name,
                        len(pairs),
                        len(shapes),
                        "structural" if structural else "suspicious-name",
                        sample,
                    )
                )
    assert not hits, (
        "A pair table whose left column holds real-value-shaped strings is "
        "tracked in this repo. That is a de-anonymisation key: it reverses "
        "whatever redaction was applied elsewhere. Delete the TABLE. Do not "
        "add its values to any allowlist here.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 7 -- account-scoped record ids
# --------------------------------------------------------------------------
#
# Checks 1-5 all hunt a value that LOOKS like something. A durable record id
# looks like nothing: 7770001 and 4412093 are the same object to every regex
# above, and only one of them names a real row in Instahyre's database. So
# this check inverts the question. It does not ask "does this value look
# dangerous"; it asks "is this value provably invented", and it decides which
# values must answer by reading the KEY PATH they sit under.
#
# THE PUBLIC/PRIVATE LINE IS DRAWN ON THE KEY, NEVER ON THE NUMBER.
# A job id, an employer id, an industry id, a location id, a job_function id
# and a skill-taxonomy id are all real, all durable, and all harmless: they
# identify a posting or a company, not a person, and every user of the site
# sees the same ones. Flagging them would make this check unrunnable and
# would teach the next reader to disable it. Only rows scoped to the account
# holder HIMSELF are in scope -- his profile, his resume, his education
# entry, his opportunities. Those are the rows whose ids are, in effect, his
# customer number.

#: Path segments naming a row that belongs to the account holder. Longest
#: first so the alternation cannot match a prefix of a longer sibling.
#:
#: NOTE THE TRAP THIS ORDERING AND THE ANCHORED "/" DEFEND AGAINST:
#: "candidate_opportunity" is his own opportunity row and IS in scope, while
#: "candidate_opportunity_employer" is the EMPLOYER on that opportunity and is
#: public. One is a prefix of the other. Matching by substring would flag
#: every employer on the board; matching whole slash-delimited segments does
#: not. The same trap guards "candidate" against "candidate_misc",
#: "candidate_settings", "candidate_conversation" and "candidatejob".
ACCOUNT_SCOPED_RESOURCES = (
    "candidate_skill_model",
    "profile_field_updates",
    "candidate_opportunity",
    "social_accounts_user",
    "candidate_matching",
    "limited_candidate",
    "diversity_info",
    "candidate_jsp",
    "profile_image",
    "education",
    "candidate",
    "resume",
    # ADMITTED 2026-08-24, and it is the one entry here that is not his row.
    # A `universities` id is shared by every alumnus, so by the key-path test
    # that separates this list from PUBLIC_RESOURCE_IDS it belongs below, not
    # here. It is here anyway, because the test that matters is not "who owns
    # the row" but "does this value point back at him" -- and this one does:
    # it is SELECTED by him, sits inside his education record, and resolves to
    # his institute even after the institute NAME has been scrubbed. Leaving
    # it public made the name scrub cosmetic.
    # This module previously argued the id could not be moved here because
    # doing so "would demand a synthetic value for a row this repo does not
    # own". That objection was correct and is now spent: the data was fixed
    # first, the value in the fixtures is invented, and it is declared below.
    # Fix the data, then move the id -- in that order, never the reverse.
    "universities",
)

#: Deliberately NOT checked, recorded so the omission is a decision rather
#: than an oversight. Each names a row shared by every user of the site:
#:   job_search, candidatejob, employer_public_jobs -- a posting
#:   candidate_opportunity_employer, anon_employer_limited -- a company
#:   industry_type, job_function, job_category -- taxonomy
#:   universities, degrees, specializations, language -- taxonomy
#: `universities` USED TO BE ON THIS LIST and was moved up on 2026-08-24. The
#: argument for keeping it here was sound and is worth preserving: the id names
#: a shared taxonomy row, not his, and moving it up "would demand a synthetic
#: value for a row this repo does not own". What retired that argument was
#: doing the data fix first -- the institute id in the fixtures is now invented
#: and declared in SYNTHETIC_ACCOUNT_IDS, so the demand is met and the id can
#: be guarded. The ordering is the lesson: fix the data, then move the id.
#:
#: `degrees` and `specializations` stay here deliberately. They are selected by
#: him in the same way, but their cardinality is tiny -- a degree id is shared
#: with millions and re-identifies nobody. The institute was the sharp one.
PUBLIC_RESOURCE_IDS = (
    "job_search", "candidatejob", "employer_public_jobs",
    "candidate_opportunity_employer", "anon_employer_limited",
    "industry_type", "job_function", "job_category",
    "degrees", "specializations", "language",
)

ACCOUNT_SCOPED_ID_IN_PATH = re.compile(
    r"/(%s)/([^/\"'\s?]+)" % "|".join(ACCOUNT_SCOPED_RESOURCES)
)

#: The spelling already used in tests/fixtures/write_contracts/ for an id the
#: capture scripts refused to write down: <CANDIDATE_ID>, <PROFILE_IMAGE_ID>.
SYNTHETIC_ID_PLACEHOLDER = re.compile(r"^<[A-Z][A-Z0-9_]*>$")

#: Invented ids, and the ONLY non-placeholder non-repdigit values admitted.
#: Safe to commit precisely because none of them names anything: they were
#: made up to replace the real ids this repo used to carry. They are listed
#: one by one rather than matched by a "starts with a repeated digit" shape
#: on purpose -- a shape rule would silently admit 7778234 as well, and the
#: entire point of this check is that a NEW id cannot arrive unnoticed.
#: Adding an entry here is a claim that the value was INVENTED. Never add a
#: value merely because the check fired on it; that rebuilds the key this
#: module exists to refuse.
SYNTHETIC_ACCOUNT_IDS = frozenset(
    {
        # profile rows
        "7770001", "7770002", "7770003", "7770004", "7770005", "7770006",
        # profile_field_updates rows
        "7770011", "7770012", "7770013", "7770014", "7770015", "7770016",
        # candidate_skill_model rows
        "88880001", "88880002", "88880003", "88880004",
        # candidate_opportunity / candidate_matching rows
        "6100000001", "6100000002", "6100000003",
        "6100000004", "6100000005", "6100000006",
        # the classic ascending stand-in, used as a candidate id
        "1234567",
        # the university row, invented 2026-08-24 -- see the note beside
        # "universities" in ACCOUNT_SCOPED_RESOURCES for why that id is
        # treated as account-scoped despite naming a shared taxonomy row.
        "41007",
    }
)


def _synthetic_account_id(value) -> bool:
    """True when `value` is provably invented rather than merely opaque."""
    text = str(value).strip()
    if not text:
        return True
    if SYNTHETIC_ID_PLACEHOLDER.match(text):
        return True
    if text.isdigit() and len(set(text)) == 1:
        return True
    return text in SYNTHETIC_ACCOUNT_IDS


def _account_id_hits(node, path):
    """Yield (key path, resource, value) for every in-scope id under `node`."""
    if isinstance(node, dict):
        resource_uri = node.get("resource_uri")
        if isinstance(resource_uri, str) and "id" in node:
            match = ACCOUNT_SCOPED_ID_IN_PATH.search(resource_uri)
            if match is not None:
                yield path + ".id", match.group(1), node["id"]
        for key, value in node.items():
            if isinstance(value, str):
                for match in ACCOUNT_SCOPED_ID_IN_PATH.finditer(value):
                    yield path + "." + key, match.group(1), match.group(2)
            for hit in _account_id_hits(value, path + "." + key):
                yield hit
    elif isinstance(node, list):
        for index, value in enumerate(node):
            for hit in _account_id_hits(value, "%s[%d]" % (path, index)):
                yield hit


def test_account_scoped_record_ids_are_synthetic():
    """Every id naming one of the account holder's own rows is invented.

    THE HAZARD. A sibling repository was scrubbed of names, emails and phone
    numbers, certified clean by a guard of this exact design, and published
    anyway with a live account primary key in a test fixture. Nobody caught
    it because there was nothing to catch by eye: it was a seven-digit
    integer under a key called "id". A record id is not noise. It is the
    durable, stable, server-side handle for one human being's row, it does
    not rotate the way a session token does, and anyone holding it can ask
    the platform to resolve it back to him for as long as the account exists.
    Published once, it is published permanently.

    WHAT IS ASSERTED. Every tracked .json file is parsed, and every id sitting
    under one of ACCOUNT_SCOPED_RESOURCES -- whether as the "id" key of an
    object that declares such a resource_uri, or as the tail of any
    resource_uri-shaped string value anywhere in the document -- must be an
    angle-bracket placeholder, a repdigit run, or a value explicitly listed in
    SYNTHETIC_ACCOUNT_IDS.

    WHAT IS NOT ASSERTED. Public ids are untouched by design; see
    PUBLIC_RESOURCE_IDS for the list and the reasoning. Confusing the two
    would either flag every employer on the board or, worse, teach the next
    reader that this check cries wolf.

    The failure message names the file, the key path and the SHAPE. It never
    prints the value: a CI log is world-readable to anyone who can read the
    build, so echoing the id would publish exactly what the check caught.
    """
    hits = []
    for rel, _path, text in _tracked_text_files():
        if not rel.lower().endswith(".json"):
            continue
        if _is_pins_or_lock(rel):
            continue
        try:
            document = json.loads(text)
        except ValueError as exc:
            hits.append(
                "%s:  UNPARSEABLE-JSON  (%s) -- a file this check cannot read "
                "is a file it cannot clear" % (rel, type(exc).__name__)
            )
            continue
        for key_path, resource, value in _account_id_hits(document, ""):
            if _synthetic_account_id(value):
                continue
            hits.append(
                "%s  %s  ACCOUNT-ID[%s]  %s"
                % (rel, key_path or "<root>", resource, _fingerprint(str(value)))
            )
    assert not hits, (
        "Ids scoped to the account holder's own records are tracked in this "
        "repo and are not provably synthetic. A record id is a durable "
        "primary key: it does not rotate, and it resolves back to one person "
        "for as long as the account exists. Replace the DATA with a repdigit "
        "run or an <ANGLE_BRACKET> placeholder. Only extend "
        "SYNTHETIC_ACCOUNT_IDS with a value you know was INVENTED -- never "
        "with one the check just caught.\n%s" % _report(hits)
    )


# --------------------------------------------------------------------------
# Check 8 -- high-entropy token beside an account-ish key
# --------------------------------------------------------------------------
#
# Check 7 needs to know the resource name in advance. This one does not: it is
# the net for the identifier nobody enumerated, and it trades precision for
# reach. It asks a single question of every line in the repository -- is there
# something token-shaped standing next to a word that means "this belongs to
# the account holder" -- and makes the answer justify itself.

#: Key names that mean "the account holder", as opposed to a job or a company.
ACCOUNT_KEY_NAME = re.compile(
    r"(?i)(candidate|profile|resume|session|csrf|token|conv_id|recruiter"
    r"|auth|cookie)"
)

#: A standalone alphanumeric run. The lookbehind refuses a run glued to a
#: preceding ".", "_" or "-": those are compounds, not values --
#: base64.urlsafe_b64encode is a function, output.b5bfe43563f0.js is a build
#: artefact filename, and 2019-08-09T153322 is a timestamp. A genuine token in
#: a value position is preceded by a quote, a colon, an equals sign or a
#: slash, all of which still match.
ENTROPIC_ATOM = re.compile(r"(?<![A-Za-z0-9._-])[A-Za-z0-9]{8,}(?![A-Za-z0-9])")

#: EIGHT, AND THE NUMBER WAS PAID FOR. An earlier sweep of this repository ran
#: at >= 16 characters, which is where "high-entropy token" scanners
#: conventionally sit, and it reported the repo clean. The identifier actually
#: leaked here was TEN characters of lowercase hex. Sixteen missed it
#: completely. Do not raise this back up because the check is noisy; widen a
#: named allowlist below instead, the way every other allowlist in this module
#: was widened.
MIN_ENTROPIC_LEN = 8

#: Values that announce their own fakeness. A real credential does not contain
#: the English words "never" or "secret", and no real identifier spells
#: "placeholder".
SELF_ANNOUNCING_MARKERS = (
    "fake",
    "sentinel",
    "example",
    "must-never",
    "redacted",
    "placeholder",
    "token_redacted",
    "secret",   # WIDENED: the credential-leak suite plants values spelled
    "never",    # SECRET...MustNeverBeReturned to prove its detector fires.
)

#: WIDENED -- employer and recruiter avatars on the public media CDN. The path
#: carries a content hash that is exactly the ten-hex shape this check hunts,
#: and there are 93 of them in the fixtures. They address a COMPANY's logo on
#: a public bucket, which is not a person and not the account holder. Note
#: what this deliberately does NOT cover: /base/candidate/ is absent, so the
#: account holder's OWN avatar hash is still checked.
PUBLIC_AVATAR_PATH = re.compile(
    r"/images/profile/base/(?:employer|recruiter)/\d+/[A-Za-z0-9]+/"
)

#: Ascending-digit runs, the oldest stand-in there is (...0123456789...).
ASCENDING_DIGITS = "0123456789"

#: WIDENED -- credential-SHAPED markers invented for the leak-detector
#: controls, where a short or hyphenated stand-in would survive a truncation
#: whole and make the detector look stronger than it is. Both are asserted to
#: be exactly session- and csrf-length by the script that owns them. They are
#: listed here rather than matched by shape because they are values, and a
#: shape loose enough to admit them would admit a real token too.
SYNTHETIC_ENTROPIC_TOKENS = frozenset(
    {
        "k9x2m4p7q1w8e3r5t6y0u2i4o6a8s1d3",
        "Ab3Cd5Ef7Gh9Ij1Kl3Mn5Op7Qr9St1Uv3Wx5Yz7Ab9Cd1Ef3Gh5Ij7Kl9Mn1Op3Q",
    }
)

#: Contracts captured from the wire with every identifier replaced at capture
#: time by an <ANGLE_BRACKET> placeholder, plus a _scrubbed note recording it.
#: Their remaining opaque strings are declared stand-ins.
WRITE_CONTRACTS_DIR = "tests/fixtures/write_contracts/"


def _has_ascending_run(value: str) -> bool:
    return any(
        ASCENDING_DIGITS[index : index + 6] in value
        for index in range(len(ASCENDING_DIGITS) - 5)
    )


def _entropic_atom_allowed(value: str, line: str, start: int, end: int) -> bool:
    """True when this atom is provably not an account-holder identifier."""
    if len(set(value)) == 1:
        return True
    if not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
        return True
    if value in SYNTHETIC_ENTROPIC_TOKENS:
        return True
    if _has_ascending_run(value):
        return True
    lowered = value.lower()
    if any(marker in lowered for marker in SELF_ANNOUNCING_MARKERS):
        return True
    return any(
        span.start() <= start and end <= span.end()
        for span in PUBLIC_AVATAR_PATH.finditer(line)
    )


def _entropy_scan_skipped(rel: str) -> bool:
    return _is_pins_or_lock(rel) or rel.startswith(WRITE_CONTRACTS_DIR)


def test_no_high_entropy_token_beside_an_account_key():
    """No opaque fixed-length token sits next to an account-ish key.

    THE HAZARD. Check 7 can only defend resource names somebody thought to
    enumerate. This is the net for the one nobody did. An account's durable
    handles do not all arrive as tidy integers under "id": they turn up as a
    hex digest in a media path, an opaque string in a conv_id, a hash on a
    recruiter record. None of them has a shape any of checks 1-5 recognises,
    and each is as durable and as resolvable as a primary key.

    WHY THE THRESHOLD IS EIGHT AND MUST STAY EIGHT. This repository was swept
    once at the conventional >= 16 characters and came back clean. The
    identifier that had actually leaked was TEN characters of lowercase hex,
    and the sweep walked straight past it. Sixteen is a number borrowed from
    secret-scanning, where the thing hunted is an API key; a per-account
    record handle is much shorter and no less permanent. MIN_ENTROPIC_LEN
    carries that finding, and lowering the noise by raising it would restore
    the exact blind spot that produced this check.

    WHAT IS ASSERTED. On any line mentioning an account-ish key name, every
    standalone alphanumeric run of MIN_ENTROPIC_LEN or more that mixes letters
    and digits must justify itself: a repdigit, an ascending-digit stand-in, a
    git SHA, a self-announcing sentinel, a declared synthetic token, or a
    public-CDN employer avatar path. Anything else fails.

    WHAT IS NOT ASSERTED. Pure-digit runs are left to checks 2 and 7 -- a
    thirteen-digit epoch is not a token. Runs glued into a dotted or
    underscored compound are identifiers and filenames, not values. And the
    scan is line-at-a-time, so a token whose key sits on the previous line is
    invisible to it; the module docstring records that.

    Failures print a shape and a location, never the token.
    """
    hits = []
    for rel, lineno, line in _iter_lines(skip=_entropy_scan_skipped):
        if not ACCOUNT_KEY_NAME.search(line):
            continue
        if GIT_SHA40.search(line):
            continue
        for match in ENTROPIC_ATOM.finditer(line):
            value = match.group(0)
            if len(value) < MIN_ENTROPIC_LEN:
                continue
            if _entropic_atom_allowed(value, line, match.start(), match.end()):
                continue
            hits.append(
                "%s:%d  ENTROPIC-TOKEN  %s  (beside an account-ish key)"
                % (rel, lineno, _fingerprint(value))
            )
    assert not hits, (
        "Opaque high-entropy tokens sit next to account-scoped key names in "
        "this repo. A durable per-account handle reads as noise but resolves "
        "back to one person for the life of the account. Replace the DATA "
        "with an obvious stand-in. Widen an allowlist above only for a class "
        "that provably names no person, and name the class when you do.\n%s"
        % _report(hits)
    )
