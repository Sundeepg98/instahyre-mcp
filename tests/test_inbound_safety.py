"""The guard on the one-way door: proof that nothing here can fire by accident.

This server can send a job application to a real employer on behalf of a real
person, and **an Instahyre application cannot be withdrawn** -- their own FAQ
says the application is sent automatically by the system, so there is no undo,
no support path, and the employer sees it immediately. Declining is equally
final and additionally reshapes which employers are matched in future cycles.

Everything in this file exists to make one class of accident impossible: a
refactor, a merge, or a well-meaning "simplification" that turns a preview into
a send. The assertions are therefore deliberately paranoid in a specific way --
each one checks the WIRE, not the return value. A test that only caught the
``ConfirmationRequired`` exception would pass against an implementation that
POSTs first and raises afterwards, which is the exact bug that would be
invisible in review and permanent in production. So every gate test also
asserts that the recorded request list contains zero writes.

Four properties are pinned here:

1. **The confirm gate holds.** No POST leaves the process unless the caller
   passed ``confirm=True``, and the default in every signature is False.
2. **Previews are inert.** ``apply_preview`` and both MCP tools called
   without ``confirm`` make GETs only.
3. **The preview is the truth.** What the preview says would be sent is
   byte-for-byte what a confirmed submit actually sends, so the human's
   decision is made on real information rather than a stale description.
4. **The write surface is enumerated.** Proven by reading the package's own
   source off disk and walking its AST: every ``.post(`` call site names its
   endpoint as a bare constant, the set of those constants is pinned, and
   ``.patch(`` appears in exactly one module.

   THIS CLAUSE USED TO END "and bulk apply is unreachable". It was true from
   the day this file was written until 2026-08-25, when the ruling that
   whatever is technically possible gets built retired the one ban in this
   package that had said "at any evidence level". Bulk apply is now
   ``instahyre_apply_bulk`` and both its URLs are in the POST census above.

   WHAT REPLACED THE UNREACHABILITY, since that is the question a reader of
   this header is really asking. The ban was never about the endpoint being
   unknown -- its contract ships whole in Instahyre's JavaScript -- it was
   about blast radius: "one call there is an irreversible mass-apply across a
   whole queue". That specific sentence is now false by construction, and not
   by assurance. The caller supplies the id list and nothing in the package
   assembles one; a cap of ten REFUSES a longer list instead of truncating it;
   every id is checked against a freshly-read pending queue; the preview names
   every company and role; and an ``expected_count`` stated apart from the
   list must match what resolved. ``tests/test_bulk_apply.py`` holds one test
   per rail and ``scripts/bulk_apply_controls.py`` breaks each of them in turn
   to show the tests can fail.

   WHAT DID NOT MOVE, and is still asserted here: the READ tier refuses both
   bulk paths exactly as before (``apply_bulk`` never left
   ``MUTATING_PATH_MARKERS``), and SINGLE apply still refuses to POST to a bulk
   URL even when its own endpoint constant is repointed at one.

The source scanners in the last two sections carry their own controls -- a
check that cannot fail certifies nothing, so each scanner is also run against a
synthetic offending source and asserted to catch it.

No network is touched: conftest builds every client on an ``httpx.MockTransport``
and makes the real transports raise, so an unmocked path is an AssertionError
rather than a real request. That guard is itself part of the safety argument.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib

import httpx
import pytest

from conftest import assert_no_credential, fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server import server as server_module
from instahyre_server.errors import InvalidFilter
from instahyre_server.inbound import ConfirmationRequired, Inbound
from instahyre_server.server import mcp

# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------

#: The captured pending queue. Its first record is the one every test acts on.
PENDING = fixture_json("opportunities_pending.json")
OPPORTUNITY = PENDING["objects"][0]

#: The two ids on a queue record, which are NOT interchangeable: ``id`` is the
#: opportunity id (a long numeric STRING) and ``job.id`` is the ordinary
#: integer job id. Sending one where the other belongs is the mistake this
#: platform makes easy, so the fixture values are pinned here and compared
#: field by field further down.
OPPORTUNITY_ID = OPPORTUNITY["id"]
JOB_ID = OPPORTUNITY["job"]["id"]
JOB_TITLE = OPPORTUNITY["job"]["title"]
EMPLOYER_NAME = OPPORTUNITY["employer"]["company_name"]

#: A csrftoken value chosen to be unmistakable if it ever leaks into a preview.
CSRF_VALUE = "csrf-token-that-must-never-be-echoed-1234567890"

#: The two bulk endpoints. SPELLED OUT AS LITERALS, and the comment that used
#: to sit here claimed the opposite -- "taken from the constant rather than
#: retyped" -- while the line below it was a retyped literal all along. The
#: literal is the right choice and now says so: a test built from the constant
#: FOLLOWS the constant, so repointing ``EP_APPLY_BULK_ES`` at something else
#: would move this file's idea of what a bulk path is along with it, and every
#: assertion here would go on passing about a different URL. The same reasoning
#: pins ``SENDABLE_LITERALS`` in tests/test_inbox_writes.py.
#:
#: NO LONGER "permanently out of scope": both were built on 2026-08-25 behind
#: instahyre_apply_bulk. They remain out of scope for the READ tier and for
#: single apply, which is what most of this file is about.
BULK_PATH = "/candidate_opportunities/candidate_opportunity/apply_bulk/"
ES_BULK_PATH = "/candidate_opportunities/candidate_matching/apply_bulk/"

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def queue_client(extra_routes=None, *, csrf=None):
    """A client whose only mocked route is the pending queue.

    Deliberately minimal: the apply endpoint is NOT wired unless a test asks
    for it, so a stray POST becomes a loud "Unmocked request" AssertionError
    from the route table rather than a silently swallowed no-op.
    """
    routes = {C.EP_OPPORTUNITIES: PENDING}
    routes.update(extra_routes or {})
    client = make_client(routes)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    return client


def requests_by_method(client, method):
    return [r for r in client.routes.requests if r.method == method]


def write_requests(client):
    """Every recorded request that could change something server-side."""
    return [r for r in client.routes.requests if r.method in WRITE_METHODS]


def describe(requests):
    return [(r.method, r.url.path) for r in requests]


# --- reading the package's own source off disk ------------------------------

PACKAGE_DIR = pathlib.Path(C.__file__).resolve().parent


def package_sources():
    """Every ``.py`` in ``instahyre_server/``, read from disk as text.

    Read fresh rather than imported: the question these scanners answer is what
    the SOURCE says, and an imported module has already lost its call sites.
    """
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_DIR.glob("*.py"))
    }


def _constant_reference(node):
    """``"C.EP_APPLY"`` for an attribute read off the constants module, else None.

    Anything that is not a plain ``C.<NAME>`` attribute returns None and is
    reported as an unattributable call site rather than quietly accepted --
    "it is probably fine" is not a standard this file is allowed to use.
    """
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "C"
    ):
        return "C." + node.attr
    return None


def post_call_sites(sources):
    """Every ``<something>.post(...)`` call in ``sources``, with its target.

    Returns ``(filename, lineno, target)`` triples where ``target`` is the
    constant name of the first positional argument, or None when the first
    argument is not a bare constant reference (including when there is none).
    """
    sites = []
    for name, text in sources.items():
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "post":
                continue
            first = node.args[0] if node.args else None
            sites.append((name, node.lineno, _constant_reference(first)))
    return sorted(sites)


def _docstring_constants(tree):
    """The id()s of every Constant node that is a docstring, so prose can be
    told apart from code. A docstring is ``__doc__``; it can never be a URL."""
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        head = body[0]
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            found.add(id(head.value))
    return found


def executable_strings_mentioning(sources, token):
    """Every non-docstring string literal containing ``token``.

    Docstrings are excluded on purpose and the exclusion is safe: a docstring
    is discarded into ``__doc__`` and is never handed to the HTTP client.
    Comments are excluded too -- ``ast`` never sees them.
    """
    hits = []
    for name, text in sources.items():
        tree = ast.parse(text, filename=name)
        docstrings = _docstring_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str) or id(node) in docstrings:
                continue
            if token in node.value:
                hits.append((name, node.lineno, node.value))
    return sorted(hits)


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(mcp.list_tools())


@pytest.fixture
def tool_client(monkeypatch):
    """A mocked client installed as the server module's lazy singleton.

    ``get_client()`` builds and memoises ``server._client`` on first use, so
    presetting it is what makes the MCP tool functions run against the mock
    transport instead of building a real one. ``_sessions`` is pinned to None
    alongside it -- it is set in the same branch, and leaving a half-built pair
    behind would be a trap for the next test.
    """
    client = queue_client()
    monkeypatch.setattr(server_module, "_client", client)
    monkeypatch.setattr(server_module, "_sessions", None)
    return client


# ---------------------------------------------------------------------------
# The confirm gate
# ---------------------------------------------------------------------------


def test_submit_interest_without_confirm_raises_confirmation_required():
    client = queue_client()

    with pytest.raises(ConfirmationRequired) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=False)

    assert "confirm=True" in excinfo.value.message


def test_submit_interest_without_confirm_sends_no_post_at_all():
    """The load-bearing half of the gate.

    The exception alone proves nothing: an implementation that sent the
    application and then raised would satisfy the previous test exactly. What
    matters is that the transport recorded no write.
    """
    client = queue_client()

    with pytest.raises(ConfirmationRequired):
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=False)

    assert requests_by_method(client, "POST") == []
    assert describe(write_requests(client)) == []


def test_declining_without_confirm_also_raises_and_sends_no_post():
    """A decline is the same endpoint with the boolean flipped, and is just as
    permanent -- it feeds the matching algorithm. It gets the same gate."""
    client = queue_client()

    with pytest.raises(ConfirmationRequired) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=False, confirm=False)

    assert excinfo.value.context.get("action") == "DECLINE"
    assert describe(write_requests(client)) == []


def test_the_confirm_argument_defaults_to_false_in_the_submit_signature():
    parameters = inspect.signature(Inbound.submit_interest).parameters
    assert parameters["confirm"].default is False


def test_submit_interest_with_confirm_omitted_entirely_still_refuses_and_sends_nothing():
    """Omission must mean refusal, not "unspecified, so proceed"."""
    client = queue_client()

    with pytest.raises(ConfirmationRequired):
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True)

    assert describe(write_requests(client)) == []


# ---------------------------------------------------------------------------
# A preview sends nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_interested", [True, False])
def test_apply_preview_makes_no_write_request(is_interested):
    """GETs are fine -- the queue has no detail route, so the record can only be
    found by scanning. What must never happen is a write."""
    client = queue_client()

    client.inbound.apply_preview(OPPORTUNITY_ID, is_interested=is_interested)

    assert requests_by_method(client, "POST") == []
    assert describe(write_requests(client)) == []
    assert client.routes.count(C.EP_OPPORTUNITIES) >= 1, "the preview did read the queue"


def test_the_apply_tool_with_no_confirm_argument_makes_no_write_request(tool_client):
    result = server_module.instahyre_apply(OPPORTUNITY_ID)

    assert describe(write_requests(tool_client)) == []
    assert result["action"] == "APPLY"
    assert "sent" not in result, "a preview must never report itself as sent"


def test_the_decline_tool_with_no_confirm_argument_makes_no_write_request(tool_client):
    result = server_module.instahyre_decline_opportunity(OPPORTUNITY_ID)

    assert describe(write_requests(tool_client)) == []
    assert result["action"] == "DECLINE"
    assert "sent" not in result


@pytest.mark.parametrize(
    "tool", [server_module.instahyre_apply, server_module.instahyre_decline_opportunity]
)
def test_both_irreversible_tools_default_confirm_to_false(tool):
    assert inspect.signature(tool).parameters["confirm"].default is False


# ---------------------------------------------------------------------------
# The preview is exact, and it is honest
# ---------------------------------------------------------------------------


def test_the_preview_url_is_the_apply_endpoint_and_the_method_is_post():
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    would_send = preview["would_send"]
    assert would_send["method"] == "POST"
    assert would_send["url"].endswith(C.EP_APPLY)
    assert would_send["url"].startswith(C.API_BASE)
    assert "bulk" not in would_send["url"]


@pytest.mark.parametrize(
    "is_interested, action", [(True, "APPLY"), (False, "DECLINE")]
)
def test_the_preview_body_carries_the_interest_boolean_for_each_action(is_interested, action):
    preview = queue_client().inbound.apply_preview(
        OPPORTUNITY_ID, is_interested=is_interested
    )

    assert preview["would_send"]["json_body"]["is_interested"] is is_interested
    assert preview["action"] == action


def test_the_preview_body_matches_the_branch_the_account_is_on():
    """The URL and the body's id key move TOGETHER, and this pins that.

    This test replaces one that asserted the opposite and passed for weeks. The
    old contract sent BOTH ``id`` and ``job_id`` to the legacy URL; a second,
    independent read of Instahyre's dispatcher on 2026-08-21 showed that the
    ``enableCandidateESOpps`` flag switches the ``$resource`` SERVICE, not just
    the body -- so the ES body (``job_id``) is only ever posted to the ES URL.
    The old pairing is one the site never produces, and it was about to be the
    shape of a real, unwithdrawable application.
    """
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)
    body = preview["would_send"]["json_body"]
    url = preview["would_send"]["url"]

    if C.APPLY_BRANCH_ES:
        assert url.endswith(C.EP_APPLY_ES)
        assert body["job_id"] == JOB_ID
        assert isinstance(body["job_id"], int)
        assert "id" not in body, (
            "the ES branch identifies the target by job_id alone; sending the "
            "opportunity id as well is the mixed shape that was just removed"
        )
    else:
        assert url.endswith(C.EP_APPLY_LEGACY)
        assert body["id"] == OPPORTUNITY_ID
        assert isinstance(body["id"], str)


def test_the_preview_body_always_carries_is_activity_page_job():
    """Set unconditionally by the site on every call, on both branches, and
    absent from the original transcription. It is false for every apply this
    server can make -- true requires a deep link from the activity page."""
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    assert preview["would_send"]["json_body"]["is_activity_page_job"] is False


def test_the_es_body_and_the_legacy_url_can_never_be_paired(monkeypatch):
    """The regression control for the bug this section documents.

    Whichever branch is configured, the id key in the body must be the one that
    branch's URL expects. A future edit that pins one without the other fails
    here rather than on a real employer's desk.
    """
    for es in (True, False):
        monkeypatch.setattr(C, "APPLY_BRANCH_ES", es)
        preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)
        url = preview["would_send"]["url"]
        body = preview["would_send"]["json_body"]

        on_es_url = url.endswith(C.EP_APPLY_ES)
        assert on_es_url is es
        assert ("job_id" in body and "id" not in body) is es, (
            "branch %s produced url=%s body keys=%s" % (es, url, sorted(body))
        )


def test_the_preview_shows_a_copy_pasteable_curl_of_the_exact_request():
    """"Show the operator the exact request" is only true if what is shown is
    legible. A nested dict is a shape; a curl line is the request."""
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    curl = preview["curl_equivalent"]
    assert curl.startswith("curl -X POST '")
    assert preview["would_send"]["url"] in curl
    assert '"is_interested": true' in curl
    assert str(JOB_ID) in curl if C.APPLY_BRANCH_ES else str(OPPORTUNITY_ID) in curl
    assert CSRF_VALUE not in curl, "the real CSRF token must never be echoed"


def test_the_preview_is_marked_irreversible_and_warns_that_it_cannot_be_withdrawn():
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    assert preview["irreversible"] is True
    warning = preview["warning"]
    assert isinstance(warning, str) and warning.strip()
    assert "withdraw" in warning.lower(), warning


def test_the_preview_names_the_role_and_the_employer():
    """A human confirming this needs to see WHAT they are applying to. An id is
    not a decision; a title and a company name are."""
    preview = queue_client().inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    opportunity = preview["opportunity"]
    assert opportunity["title"] == JOB_TITLE
    assert opportunity["company"] == EMPLOYER_NAME
    assert opportunity["id"] == OPPORTUNITY_ID
    assert opportunity["job_id"] == JOB_ID


def test_the_preview_never_carries_a_real_csrf_token():
    """The preview is shown to a human and may be logged. The header is
    described, never populated -- so a real session token cannot ride out in a
    transcript.

    Checked with the shared walker rather than ``json.dumps``. Serialising
    first collapses the payload to one string, which loses the field path a
    failure needs, and it can only see values that survive a JSON round trip --
    a token carried on an object, in bytes, or base64-encoded is invisible to
    it. ``test_credential_leak.py`` holds the controls for each of those.
    """
    client = queue_client(csrf=CSRF_VALUE)

    preview = client.inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    header = preview["would_send"]["headers"]["X-CSRFToken"]
    assert header.startswith("<") and header.endswith(">"), header
    assert_no_credential(preview, CSRF_VALUE, where="apply_preview")


# ---------------------------------------------------------------------------
# apply_bulk is unreachable
# ---------------------------------------------------------------------------


def test_the_emptied_blocklist_still_bites_when_something_is_put_back_on_it():
    """The control for a brake with nothing currently on it.

    Lifting the ban left `FORBIDDEN_ENDPOINTS` empty and left the clause that
    reads it in place, deliberately, so a future ruling can re-ban a path in one
    line. That is the right call and it has a cost nobody had paid: **an empty
    blocklist means the clause never executes.** Every apply that gets refused
    today is refused by the MARKER half of the same `if`, so the blocklist half
    is dead code wearing a guard's clothes, and a typo in it -- or its quiet
    deletion in some later tidy-up -- would change nothing observable until the
    day it was needed, which is the worst possible day to find out.

    So the set is planted with a path that carries NO marker, which isolates the
    two halves: `EP_APPLY_ES` contains none of mark_all_read, send_message,
    star_conversation, toggle_message_read or apply_bulk, so if the refusal
    fires it can only have come from the blocklist clause.

    The plant is monkeypatched and reverted, and the final assertion checks the
    live constant is empty again -- a control that leaves a ban behind would be
    worse than no control.
    """
    from instahyre_server.errors import InstahyreError

    original = C.FORBIDDEN_ENDPOINTS
    assert original == frozenset(), "precondition: the set is empty today"
    assert not any(
        marker in C.EP_APPLY_ES.lower() for marker in C.MUTATING_PATH_MARKERS
    ), (
        "the plant is only isolating if the planted path carries no marker; "
        "EP_APPLY_ES now does, so this control is measuring the wrong clause"
    )

    client = queue_client(csrf=CSRF_VALUE)
    try:
        C.FORBIDDEN_ENDPOINTS = frozenset({C.EP_APPLY_ES})
        with pytest.raises(InstahyreError) as excinfo:
            client.inbound.submit_interest(
                OPPORTUNITY_ID, is_interested=True, confirm=True
            )
    finally:
        C.FORBIDDEN_ENDPOINTS = original

    # The apply route is deliberately unwired in this harness, so a FAILURE to
    # refuse would surface as an "Unmocked request" AssertionError rather than
    # as a silent pass. Asserting the specific refusal is what separates "the
    # blocklist stopped it" from "the route table did".
    assert "Refusing to POST" in str(excinfo.value), (
        "raised, but not by the blocklist: %s" % excinfo.value
    )
    assert write_requests(client) == [], "a forbidden path reached the network"
    assert C.FORBIDDEN_ENDPOINTS == frozenset(), "the control left a ban behind"


def test_the_permanent_ban_was_lifted_by_ruling_and_left_an_empty_set():
    """RE-RATIFIED 2026-08-25. This asserted the opposite until that day.

    It read::

        assert BULK_PATH in C.FORBIDDEN_ENDPOINTS

    and that was the strongest single claim in this file: both apply_bulk
    spellings banned, in the words of the constant itself, "at any evidence
    level". The ruling is that whatever is technically POSSIBLE gets built, the
    contract for bulk apply ships whole in Instahyre's own JavaScript, and so
    it was built. The ban is lifted and the set is empty.

    THE ASSERTION IS NOT DELETED, because "is the ban still in force" stays a
    question worth answering out loud -- and because an empty set that nobody
    checks is indistinguishable from a set somebody emptied by accident. What
    it now pins is the SHAPE of the lifting: the paths left the blocklist and
    they went somewhere NAMED, not nowhere. The protection that blocklist
    carried lives in three places that are each asserted elsewhere in this
    suite -- the read tier's marker list (unchanged), the two-entry bulk
    allowlist, and the gate on writes.Writer.bulk_apply.
    """
    assert C.FORBIDDEN_ENDPOINTS == frozenset(), (
        "FORBIDDEN_ENDPOINTS regrew entries: %s. If a path is genuinely being "
        "re-banned that is a ruling, not a patch." % sorted(C.FORBIDDEN_ENDPOINTS)
    )
    # Where the two paths went: a NAMED allowlist of exactly two, reachable
    # only from the bulk write. Not a rule, not a prefix.
    assert C.SENDABLE_BULK_APPLY_PATHS == frozenset({BULK_PATH, ES_BULK_PATH})
    # And the reader is untouched: it still refuses them by marker.
    assert "apply_bulk" in C.MUTATING_PATH_MARKERS
    # Single apply is still not a bulk path, which was always the other half.
    assert C.EP_APPLY not in C.SENDABLE_BULK_APPLY_PATHS
    assert "bulk" not in C.EP_APPLY


def test_the_bulk_apply_path_appears_in_exactly_one_source_file():
    """You cannot POST to a URL whose path is not written anywhere.

    The path string lives in constants.py alone, where it is listed as
    FORBIDDEN. If it turns up in a second file, someone is building a request.
    """
    sources = package_sources()
    carriers = sorted(name for name, text in sources.items() if BULK_PATH in text)

    assert carriers == ["constants.py"], (
        "the bulk apply path escaped constants.py and now appears in %s" % carriers
    )


def test_no_executable_string_outside_constants_names_the_bulk_endpoint():
    """The token ``apply_bulk`` may appear in prose; it may not appear in a
    string that could form a URL.

    THE FILTER WAS RE-RATIFIED ON 2026-08-25 AND IT IS NOW WHAT THE PARAGRAPH
    ABOVE ALWAYS SAID. This docstring has stated the rule as "a label carries no
    slash and is not a path; anything containing one is a URL fragment and fails
    here" since the day it was written, while the code underneath tested
    something stricter and different: ``value.strip() != "apply_bulk"``, an
    exact-match allowlist of one. The two agreed only as long as the single
    permitted string was the only prose anybody wrote.

    Building the bulk tool ended that. ``server.py`` now carries a payload
    string that explains, to whoever calls the server, that apply_bulk was
    banned and is now built and how it is gated -- prose, in a dict value,
    carrying no slash and composable into nothing. Under the old filter that is
    an offender, and the only ways to satisfy it were to stop naming the thing
    the paragraph is about or to bend the prose around a keyword check. This
    file has already learned once (see
    ``test_a_shipped_entry_has_no_wire_recording_on_disk``) that bending prose
    to satisfy a keyword list makes the prose worse to make a test pass.

    SO THE RULE IS NOW THE COMPOSABILITY ONE, AND IT IS NOT WEAKER. A URL path
    needs a separator: every spelling that could be joined into a request --
    ``"apply_bulk/"``, ``"candidate_matching/apply_bulk/"``, a whole path --
    carries a ``/`` and is caught. A bare ``"apply_bulk"`` with no slash was
    ALREADY permitted by the old exact-match filter, so nothing that was
    refused before is admitted now; what changed is that prose containing the
    token alongside other words is no longer treated as a URL fragment. The
    control below still fails on a synthetic path, and the sibling test
    ``test_the_bulk_apply_path_appears_in_exactly_one_source_file`` pins the
    full path strings to constants.py from the other direction.
    """
    sources = {name: text for name, text in package_sources().items() if name != "constants.py"}

    offenders = [
        (name, lineno, value)
        for name, lineno, value in executable_strings_mentioning(sources, "apply_bulk")
        if "/" in value
    ]

    assert offenders == [], (
        "a string outside constants.py could be composed into a bulk apply URL: %s" % (offenders,)
    )


def test_the_bulk_string_scanner_reports_a_synthetic_bulk_url():
    """The control. A scanner that has never been shown failing certifies
    nothing, so it is run against a source that does the forbidden thing."""
    synthetic = {
        "rogue.py": (
            '"""A docstring mentioning apply_bulk, which is prose and allowed."""\n'
            'PATH = "/candidate_opportunities/candidate_opportunity/apply_bulk/"\n'
        )
    }

    hits = executable_strings_mentioning(synthetic, "apply_bulk")

    assert [(name, value) for name, _, value in hits] == [("rogue.py", BULK_PATH)]


def test_the_only_bulk_tool_is_the_one_that_was_ruled_on(tools):
    """RE-RATIFIED 2026-08-25. It asserted ``offenders == []`` until that day.

    The old reading was: "Instahyre's API has a bulk apply. Exposing it would
    make one call irreversible across a whole queue at once." The first
    sentence is still true and the second is what the GATE now answers -- a
    list the caller writes by hand, capped at ten, every id checked against the
    live pending queue, every company and role named in the preview, and a
    separately-stated expected_count. One call cannot reach a whole queue.

    THE ASSERTION IS KEPT AND POINTED AT ONE NAME rather than deleted. "No tool
    has bulk in its name" was doing real work: it caught the family, not the
    instance, so nothing bulk-shaped could arrive unnoticed. Emptying the check
    would retire that. Pinning it to the single ruled-on name keeps it -- a
    SECOND bulk tool, a bulk decline above all, still fails here and still
    needs somebody to come and change this line on purpose.
    """
    bulk_tools = sorted(tool.name for tool in tools if "bulk" in tool.name.lower())
    assert bulk_tools == ["instahyre_apply_bulk"], (
        "a bulk tool appeared that nobody ruled on: %s. Note there is no bulk "
        "DECLINE to build -- the bulk body has no is_interested key." % bulk_tools
    )


# ---------------------------------------------------------------------------
# Only one endpoint can ever be POSTed to
# ---------------------------------------------------------------------------


def test_every_post_call_site_in_the_package_targets_a_measured_endpoint():
    """The airtight half of the bulk argument, and the write-surface census.

    Every POST call site names its endpoint as a bare constant, so attribution
    is exact rather than inferred. A call site whose first argument is not a
    plain ``C.<NAME>`` is reported as unattributable and fails -- an ambiguous
    write path is a finding, not a rounding error.

    THE LIST GREW ON 2026-08-23 and the growth is the point of the census: three
    referral/support endpoints entered the package the same day their request
    bodies were captured. Each addition here is a deliberate re-ratification --
    a new constant appearing in this list without a matching entry in
    ``constants.CAPTURED_WRITE_CONTRACTS`` is a write nobody measured.

    IT GREW AGAIN ON 2026-08-25, by two: ``EP_STAR_CONVERSATION`` and
    ``EP_TOGGLE_MESSAGE_READ``, the second and third inbox writes. Both were
    admitted on the same terms as everything before them -- a captured contract
    first, then a named allowlist entry, then a tool. The inbox's FOURTH write
    admitted that day, mark_all_read, does not appear here and its absence is
    not an oversight: it is a **GET**, so a POST census cannot see it. That is
    precisely why this file's sibling assertion
    ``test_no_get_call_site_aims_at_a_mutating_path_outside_the_gated_one`` in
    ``tests/test_inbox_writes.py`` exists -- a write surface that a verb-shaped
    census is structurally blind to needs a census shaped the other way.
    """
    sites = post_call_sites(package_sources())

    assert sites, "the scanner found no .post( call site at all -- it is broken"

    unattributable = [(name, lineno) for name, lineno, target in sites if target is None]
    assert unattributable == [], (
        "a .post( call site does not name its endpoint as a bare constant: %s" % (unattributable,)
    )

    targets = sorted({target for _, _, target in sites})
    assert targets == [
        # THE TWO BULK PATHS ENTERED THE CENSUS ON 2026-08-25, and they are the
        # only entries here that were ever FORBIDDEN rather than merely
        # unmeasured. Every other addition to this list filled a gap; these two
        # retire a ban whose own constant said "at any evidence level". The
        # terms of admission did not change for them -- a captured contract
        # first, then a named allowlist, then a tool -- which is why they are
        # checked against CAPTURED_WRITE_CONTRACTS below exactly like the rest.
        "C.EP_APPLY_BULK_ES",
        "C.EP_APPLY_BULK_LEGACY",
        "C.EP_APPLY_ES",
        "C.EP_APPLY_LEGACY",
        "C.EP_LOGIN",
        "C.EP_REFERRAL",
        "C.EP_REFERRAL_INVITES",
        "C.EP_SEND_MESSAGE",
        "C.EP_STAR_CONVERSATION",
        "C.EP_SUPPORT_QUERY",
        "C.EP_TOGGLE_MESSAGE_READ",
    ], "a new write path appeared in the package: %s" % (sites,)

    # The re-ratification, asserted rather than trusted to review: every POST
    # target that is not apply or login has to be a surface whose contract was
    # captured. This is what stops the list above from being widened by simply
    # adding a name to it.
    measured = {
        "C.EP_SUPPORT_QUERY": "support_tickets",
        "C.EP_REFERRAL": "referrals",
        "C.EP_REFERRAL_INVITES": "referrals",
        # Added 2026-08-23 with the reply tool. It is the FIRST inbox write in
        # this package, so it is the one addition here that narrows a standing
        # refusal rather than filling a gap -- and it is admitted on the same
        # terms as the others: an entry in CAPTURED_WRITE_CONTRACTS, or it does
        # not belong on the list above.
        "C.EP_SEND_MESSAGE": "inbox_reply",
        # Added 2026-08-25. Both retire a refusal that had already stopped
        # resting on evidence: their contracts were captured and recorded on
        # 2026-08-23 while the tools were withheld on value. They are admitted
        # here on the identical terms as everything above -- a captured
        # contract, named, or they do not belong on the list.
        "C.EP_STAR_CONVERSATION": "inbox_star",
        "C.EP_TOGGLE_MESSAGE_READ": "inbox_mark_read",
        # Added 2026-08-25 with the bulk apply. Note these are NOT added to the
        # exemption below beside their single-apply siblings, even though they
        # are apply endpoints: the two single-apply constants are exempt only
        # because they predate this register, and inheriting that exemption is
        # the one thing bulk apply must not do. It carries a captured contract
        # like every other admitted write, and this line is what requires it.
        "C.EP_APPLY_BULK_ES": "apply_bulk",
        "C.EP_APPLY_BULK_LEGACY": "apply_bulk",
    }
    for target in targets:
        if target in ("C.EP_APPLY_ES", "C.EP_APPLY_LEGACY", "C.EP_LOGIN"):
            continue
        assert target in measured, (
            "%s posts to an endpoint with no captured contract" % target
        )
        assert measured[target] in C.CAPTURED_WRITE_CONTRACTS


def test_the_post_call_site_scanner_sees_every_textual_post_in_the_package():
    """Guards the guard: if the AST walk ever stops matching how a call is
    written, the counts diverge and this speaks up instead of the census
    silently shrinking to zero."""
    sources = package_sources()
    textual = sum(text.count(".post(") for text in sources.values())

    assert len(post_call_sites(sources)) == textual, (
        "the AST scan and a plain text count of '.post(' disagree; a call site "
        "is hiding from the scanner, or '.post(' now appears in a comment"
    )


def test_the_post_call_site_scanner_reports_a_write_to_another_endpoint():
    """The control for the census: it must flag both a foreign endpoint and an
    endpoint it cannot attribute."""
    synthetic = {
        "rogue.py": (
            "def go(http):\n"
            "    http.post(C.EP_SOMETHING_ELSE, json_body={})\n"
            '    http.post("/candidate_opportunities/candidate_opportunity/apply_bulk/")\n'
        )
    }

    sites = post_call_sites(synthetic)

    assert [target for _, _, target in sites] == ["C.EP_SOMETHING_ELSE", None]


def write_verbs_defined_on_the_client():
    """Every write verb ``InstahyreHTTP`` actually defines, read off the class.

    DERIVED, NOT LISTED, and that change is the whole point of this helper.
    Until 2026-08-24 the census below hardcoded ``("patch","delete")``. A
    ``put`` was then added to http.py for the job-search profile -- a new door
    on the class that can reach a live account -- and the census SAID NOTHING,
    because a hand-written verb list cannot notice a verb nobody added to it.
    It passed while the surface it claims to enumerate had grown, which is the
    precise failure its own docstring warns about one function down.

    Reading the class closes that loop: a verb added to http.py enrols itself
    here, so the next new door is a test failure on the day it is cut.
    """
    from instahyre_server.http import InstahyreHTTP

    return tuple(
        sorted(
            name
            for name in vars(InstahyreHTTP)
            if name in ("post", "put", "patch", "delete") and callable(vars(InstahyreHTTP)[name])
        )
    )


def receiver_is_the_http_client(node):
    """True for ``http.VERB(...)`` and ``<anything>.http.VERB(...)``, else False.

    MATCHING ON THE VERB NAME ALONE IS NOT ENOUGH, and that was measured rather
    than foreseen. Adding ``put`` to the client on 2026-08-24 made this census
    report four innocent modules: ``Store.put`` is the cache's own writer, so
    ``self.store.put(...)`` and ``http.put(...)`` are the same attribute name
    on entirely different objects. A census that cannot tell them apart is
    either noisy or, once someone silences the noise, blind.

    Attributing by RECEIVER is also the more honest reading of what this test
    claims. The question is not "which module contains the letters p-u-t"; it
    is "which module can reach the account", and only the HTTP client can.
    """
    if isinstance(node, ast.Name):
        return node.id == "http"
    if isinstance(node, ast.Attribute):
        return node.attr == "http"
    return False


def test_the_receiver_discriminator_separates_the_cache_from_the_client__CONTROL():
    """The control for the discriminator. Both halves matter and both are
    asserted: a rule that accepted everything would restore the noise, and one
    that accepted nothing would empty the census while still passing."""
    tree = ast.parse(
        "def go(http, store, self):\n"
        "    http.put('/a')\n"
        "    self.http.put('/b')\n"
        "    self.store.put('k', 'v')\n"
        "    store.put('k', 'v')\n"
        "    self.cache.put('k', 'v')\n"
    )
    seen = [
        (ast.unparse(node.func.value), receiver_is_the_http_client(node.func.value))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert dict(seen) == {
        "http": True,
        "self.http": True,
        "self.store": False,
        "store": False,
        "self.cache": False,
    }


def test_the_verb_census_reads_the_client_rather_than_a_hardcoded_list__CONTROL():
    """The control for the helper above. If it ever stops seeing the verbs the
    class defines, the census below silently narrows to nothing and every
    module passes -- so the helper is asserted to see the ones that exist."""
    verbs = write_verbs_defined_on_the_client()
    assert "put" in verbs, "http.py defines put(); the census cannot see it"
    assert "patch" in verbs
    assert "post" in verbs
    assert "delete" not in verbs, (
        "http.py deliberately defines no delete(); if one was added, that is the "
        "finding this assertion exists to surface"
    )


def test_only_the_two_write_modules_issue_patch_put_or_delete():
    """The write surface grew, so the census had to grow with it.

    ``.post(`` was once the whole write surface. Profile writes added PATCH and
    DELETE, and a census that still counted only POSTs would have reported a
    complete write surface while two more verbs went unwatched -- which is
    exactly how a census stops being one.

    ``writes.py`` joined on 2026-08-23 with the saved-search alert toggle, which
    is a PATCH. It is named here rather than folded into a wildcard: the value
    of this test is that the set of modules holding a write verb is SHORT and
    ENUMERATED, and a third name appearing without a reason is the finding.

    PUT joined on 2026-08-24 with the job-search profile, and it arrived through
    the hole described in ``write_verbs_defined_on_the_client`` -- this census
    did not fail when it should have, because its verb list was written by hand.
    The list is now read off the client class instead.
    """
    verbs = tuple(v for v in write_verbs_defined_on_the_client() if v != "post")
    callers = {}
    for name, text in package_sources().items():
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in verbs
                and receiver_is_the_http_client(node.func.value)
            ):
                callers.setdefault(name, []).append((node.func.attr, node.lineno))

    # http.py DEFINES these verbs but never calls one as an attribute, so it
    # does not appear here -- which is the point: the definition is the door and
    # profile_write.py is the only room with a key.
    assert sorted(callers) == ["profile_write.py", "writes.py"], (
        "a module outside profile_write.py now issues PATCH/PUT/DELETE: %s" % (callers,)
    )


def test_every_patch_put_and_delete_target_is_a_profile_endpoint():
    """These verbs may only ever aim at the profile resources.

    Checked on the resolved path at runtime rather than statically, because two
    of these targets are built by formatting an id into a template and a static
    reading of that proves nothing about what it resolves to.

    EP_JSP joined on 2026-08-24. Note what it is NOT: the job-search profile's
    own ``resource_uri`` names a ``candidate_jsp`` route, and the site does not
    write there -- it writes to ``candidate_skills/:id``. Both spellings sit
    under the same profile prefix, so this test would have accepted either; the
    reason the right one is used is recorded at constants.EP_JSP, not here.

    EP_EDUCATION joined on 2026-08-25 with the education write, and it is the
    one target here that was already in the package as a READ before it became
    a write target -- the candidate id is recovered from that same collection.
    It is listed anyway rather than exempted: a path this package already GETs
    is exactly the kind that acquires a write verb without anybody noticing.
    """
    allowed = {C.EP_SKILL_MODEL, C.EP_PROFILE_PATCH, C.EP_JSP, C.EP_EDUCATION}
    for path in allowed:
        assert path.startswith("/candidate_misc/profile/"), path
    assert not any(
        marker in path.lower() for path in allowed for marker in C.MUTATING_PATH_MARKERS
    ), "a profile write target collides with a forbidden action name"


def test_only_the_http_module_calls_request_directly():
    """``.post(`` is the only door out for a write because ``.request(`` is
    private to http.py. If another module started calling it, the POST census
    above would stop being a complete write surface."""
    callers = sorted(
        name for name, text in package_sources().items() if ".request(" in text
    )
    assert callers == ["http.py"], (
        "a module outside http.py now issues raw requests: %s" % callers
    )


# ---------------------------------------------------------------------------
# When confirm IS given, it does exactly one thing
# ---------------------------------------------------------------------------


def test_a_confirmed_apply_makes_exactly_one_post_to_the_apply_endpoint():
    """One action, one request. Not two, not a retry loop, not a fan-out."""
    client = queue_client({C.EP_APPLY: {"success": True}}, csrf="tok")

    client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    posts = requests_by_method(client, "POST")
    assert len(posts) == 1, describe(client.routes.requests)
    assert client.routes.count(C.EP_APPLY) == 1
    assert posts[0].url.path.endswith(C.EP_APPLY)
    assert describe(write_requests(client)) == describe(posts)


def test_the_posted_body_is_exactly_what_the_preview_promised():
    """The assertion that stops the preview drifting away from reality.

    A human consents to the preview. If the sent body could differ from it --
    an extra field, a renamed key, a re-ordered payload -- the consent was
    given for something other than what went out.
    """
    client = queue_client({C.EP_APPLY: {"success": True}}, csrf="tok")

    promised = client.inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)
    promised_body = promised["would_send"]["json_body"]
    client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    sent_body = json.loads(requests_by_method(client, "POST")[0].content)
    assert sent_body == promised_body
    assert list(sent_body) == list(promised_body), "even the key order must match"


def test_the_apply_post_carries_the_csrf_token_from_the_cookie():
    client = queue_client({C.EP_APPLY: {"success": True}}, csrf=CSRF_VALUE)

    client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    post = requests_by_method(client, "POST")[0]
    assert post.headers.get("X-CSRFToken") == CSRF_VALUE
    assert post.headers.get("Content-Type") == "application/json"


def test_a_confirmed_apply_reports_what_it_did_and_that_it_cannot_be_undone():
    """The result has to be readable as a receipt for a permanent act."""
    client = queue_client({C.EP_APPLY: {"success": True}}, csrf="tok")

    result = client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    assert result["sent"] is True
    assert result["action"] == "APPLY"
    assert result["irreversible"] is True
    assert result["opportunity"]["id"] == OPPORTUNITY_ID
    assert result["opportunity"]["company"] == EMPLOYER_NAME


# ---------------------------------------------------------------------------
# Profile writes are preview-only
# ---------------------------------------------------------------------------

PROFILE = fixture_json("candidate_profile.json")
EDUCATION = fixture_json("education.json")
CANDIDATE_ID = PROFILE["id"]


def profile_client():
    """Education (which is how the candidate id is recovered) plus the profile
    detail route. No write route is wired, deliberately."""
    return make_client(
        {
            C.EP_EDUCATION: EDUCATION,
            C.EP_PROFILE.format(candidate_id=CANDIDATE_ID): PROFILE,
        }
    )


def tool_named(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_the_apply_tool_description_says_applications_cannot_be_withdrawn(tools):
    description = tool_named(tools, "instahyre_apply").description.lower()
    assert "cannot be withdrawn" in description
    assert "irreversible" in description


def test_the_decline_tool_description_says_it_is_irreversible(tools):
    description = tool_named(tools, "instahyre_decline_opportunity").description.lower()
    assert "irreversible" in description
    assert "permanent" in description


def test_no_assertion_in_this_file_could_have_reached_the_real_instahyre():
    """The premise every assertion above rests on, asserted once out loud.

    Both halves matter: the mock really did serve the call (so the recorded
    request list is the real record of what this package tried to send), and
    the genuine transport really is blocked (so a route this file forgot to
    mock could not have quietly gone out over the wire).
    """
    client = queue_client()

    client.inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)
    assert client.routes.requests, "the mock transport served the preview's reads"

    with pytest.raises(AssertionError, match="real network"):
        httpx.HTTPTransport().handle_request(
            httpx.Request("GET", C.API_BASE + C.EP_JOB_SEARCH)
        )


# ---------------------------------------------------------------------------
# The three guards added after this file first ran
#
# Each one was found by this suite before it existed: the CSRF hole was
# measured (a confirmed apply went out unsigned), the duplicate-send hole was
# reported, and the old bulk tripwire compared two distinct constants so it was
# False by construction. All three now fire, and all three are shown firing.
# ---------------------------------------------------------------------------


def test_a_confirmed_apply_without_a_csrf_token_refuses_before_sending():
    """An unsigned write would 403 -- ambiguously -- on the one call that must not be ambiguous."""
    client = queue_client({C.EP_APPLY: {"success": True}})  # no csrf on purpose

    with pytest.raises(ConfirmationRequired) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    assert "csrf" in str(excinfo.value).lower()
    assert write_requests(client) == [], describe(client.routes.requests)


def test_the_csrf_refusal_names_the_tool_that_fixes_it():
    client = queue_client({C.EP_APPLY: {"success": True}})

    with pytest.raises(ConfirmationRequired) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    assert "instahyre_auth_status" in str(excinfo.value)


def _already_actioned_queue(status):
    """The pending queue, with the first record already in the given state."""
    payload = json.loads(json.dumps(PENDING))
    payload["objects"][0]["interview_status"] = status
    return payload


def test_applying_twice_to_the_same_opportunity_is_refused():
    """An irreversible action spent twice cannot improve the outcome."""
    queue = _already_actioned_queue(1)
    client = make_client({C.EP_OPPORTUNITIES: queue, C.EP_APPLY: {"success": True}})
    client.http.cookies.set("csrftoken", "tok", domain="www.instahyre.com")
    target = queue["objects"][0]["id"]

    with pytest.raises(ConfirmationRequired) as excinfo:
        client.inbound.submit_interest(target, is_interested=True, confirm=True)

    assert "already" in str(excinfo.value).lower()
    assert write_requests(client) == [], describe(client.routes.requests)


def test_declining_something_already_declined_is_refused():
    queue = _already_actioned_queue(2)
    client = make_client({C.EP_OPPORTUNITIES: queue, C.EP_APPLY: {"success": True}})
    client.http.cookies.set("csrftoken", "tok", domain="www.instahyre.com")
    target = queue["objects"][0]["id"]

    with pytest.raises(ConfirmationRequired):
        client.inbound.submit_interest(target, is_interested=False, confirm=True)

    assert write_requests(client) == []


def test_declining_something_already_applied_to_is_still_allowed():
    """Changing your mind is a different action from repeating one."""
    queue = _already_actioned_queue(1)
    client = make_client({C.EP_OPPORTUNITIES: queue, C.EP_APPLY: {"success": True}})
    client.http.cookies.set("csrftoken", "tok", domain="www.instahyre.com")
    target = queue["objects"][0]["id"]

    result = client.inbound.submit_interest(target, is_interested=False, confirm=True)

    assert result["sent"] is True
    assert len(requests_by_method(client, "POST")) == 1


def test_the_bulk_guard_reads_the_endpoint_value_so_it_can_actually_fire(monkeypatch):
    """The guard this replaced compared two distinct constants and was dead code.

    Point EP_APPLY at the bulk path and the guard must refuse. If this test can
    be made to pass with the guard deleted, the guard is decorative again.
    """
    client = queue_client({C.EP_APPLY: {"success": True}}, csrf="tok")
    # RE-SOURCED 2026-08-25. This read ``sorted(C.FORBIDDEN_ENDPOINTS)[0]``
    # until the ban was lifted and that set went empty -- at which point the
    # line would have raised IndexError, not failed an assertion, and a guard
    # whose test cannot even construct its input is a guard nobody is checking.
    # The literal is used rather than the constant on purpose: the point is to
    # aim single apply at a path it must refuse, and reading that path from the
    # very constant the tool is allowed to use would prove nothing.
    bulk = ES_BULK_PATH
    # Point the branch's own endpoint at a bulk path. The guard reads the path
    # the request builder actually produced, so this reaches it.
    monkeypatch.setattr(C, "EP_APPLY_ES" if C.APPLY_BRANCH_ES else "EP_APPLY_LEGACY", bulk)

    with pytest.raises(Exception) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    # ASSERTED ON THE REFUSAL'S SUBSTANCE, not on the word "forbidden", which
    # is what this line checked until 2026-08-25. That word was load-bearing
    # while a forbidden LIST was doing the refusing; now the marker half fires
    # and the message says something more useful -- that single apply may not
    # reach a bulk URL, and that the way to bulk apply is the tool built for
    # it. A test keyed on a word that has left the design would pass on a
    # message that no longer means anything.
    message = str(excinfo.value).lower()
    assert "refusing" in message and "bulk" in message, message
    assert write_requests(client) == [], describe(client.routes.requests)
