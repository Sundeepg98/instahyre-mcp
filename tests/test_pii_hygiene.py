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
