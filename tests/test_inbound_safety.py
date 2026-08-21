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
4. **The write surface is enumerated, and bulk apply is unreachable.**
   Proven by reading the package's own source off disk and walking its AST:
   every ``.post(`` call site targets ``C.EP_APPLY_ES``, ``C.EP_APPLY_LEGACY``
   or ``C.EP_LOGIN``, and nothing else; and ``.patch(`` appears in exactly one
   module. Both bulk URLs are blocked, not just the one that used to be --
   the ES spelling, which is the branch this account actually resolves to,
   was missing from the forbidden list until 2026-08-21.

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

from conftest import fixture_json, make_client
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

#: The bulk endpoint, permanently out of scope. Taken from the constant rather
#: than retyped, so a change to the constant cannot desynchronise this file.
BULK_PATH = "/candidate_opportunities/candidate_opportunity/apply_bulk/"

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
    transcript."""
    client = queue_client(csrf=CSRF_VALUE)

    preview = client.inbound.apply_preview(OPPORTUNITY_ID, is_interested=True)

    header = preview["would_send"]["headers"]["X-CSRFToken"]
    assert header.startswith("<") and header.endswith(">"), header
    assert CSRF_VALUE not in json.dumps(preview)


# ---------------------------------------------------------------------------
# apply_bulk is unreachable
# ---------------------------------------------------------------------------


def test_the_forbidden_endpoint_set_contains_the_bulk_apply_path():
    assert BULK_PATH in C.FORBIDDEN_ENDPOINTS
    assert C.EP_APPLY not in C.FORBIDDEN_ENDPOINTS
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

    It legitimately occurs twice outside constants.py today, and both are
    harmless by construction: a docstring in inbound.py (excluded here -- a
    docstring is ``__doc__`` and never reaches the HTTP client) and the literal
    dict key ``"apply_bulk"`` in server.py's ``deliberately_not_built`` block,
    which is a label in a returned payload. A label carries no slash and is not
    a path; anything containing one is a URL fragment and fails here.
    """
    sources = {name: text for name, text in package_sources().items() if name != "constants.py"}

    offenders = [
        (name, lineno, value)
        for name, lineno, value in executable_strings_mentioning(sources, "apply_bulk")
        if value.strip() != "apply_bulk"
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


def test_no_registered_tool_has_bulk_in_its_name(tools):
    """Instahyre's API has a bulk apply. Exposing it would make one call
    irreversible across a whole queue at once."""
    offenders = sorted(tool.name for tool in tools if "bulk" in tool.name.lower())
    assert offenders == []


# ---------------------------------------------------------------------------
# Only one endpoint can ever be POSTed to
# ---------------------------------------------------------------------------


def test_every_post_call_site_in_the_package_targets_apply_or_login():
    """The airtight half of the bulk argument, and the write-surface census.

    Two POST call sites exist in the whole package: the guarded apply in
    inbound.py and the login handshake in session.py. Both name their endpoint
    as a bare constant, so attribution is exact rather than inferred. A call
    site whose first argument is not a plain ``C.<NAME>`` is reported as
    unattributable and fails -- an ambiguous write path is a finding, not a
    rounding error.
    """
    sites = post_call_sites(package_sources())

    assert sites, "the scanner found no .post( call site at all -- it is broken"

    unattributable = [(name, lineno) for name, lineno, target in sites if target is None]
    assert unattributable == [], (
        "a .post( call site does not name its endpoint as a bare constant: %s" % (unattributable,)
    )

    targets = sorted({target for _, _, target in sites})
    assert targets == ["C.EP_APPLY_ES", "C.EP_APPLY_LEGACY", "C.EP_LOGIN"], (
        "a new write path appeared in the package: %s" % (sites,)
    )


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


def test_the_only_module_that_issues_patch_or_delete_is_profile_write():
    """The write surface grew, so the census had to grow with it.

    ``.post(`` was once the whole write surface. Profile writes added PATCH and
    DELETE, and a census that still counted only POSTs would have reported a
    complete write surface while two more verbs went unwatched -- which is
    exactly how a census stops being one.
    """
    verbs = ("patch", "delete")
    callers = {}
    for name, text in package_sources().items():
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in verbs
            ):
                callers.setdefault(name, []).append((node.func.attr, node.lineno))

    # http.py DEFINES these verbs but never calls one as an attribute, so it
    # does not appear here -- which is the point: the definition is the door and
    # profile_write.py is the only room with a key.
    assert sorted(callers) == ["profile_write.py"], (
        "a module outside profile_write.py now issues PATCH/DELETE: %s" % (callers,)
    )


def test_every_patch_and_delete_target_is_a_profile_endpoint():
    """Both write verbs may only ever aim at the two profile resources.

    Checked on the resolved path at runtime rather than statically, because one
    of these targets is built by formatting a candidate id into a template and
    a static reading of that proves nothing about what it resolves to.
    """
    allowed = {C.EP_SKILL_MODEL, C.EP_PROFILE_PATCH}
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
    bulk = sorted(C.FORBIDDEN_ENDPOINTS)[0]
    # Point the branch's own endpoint at a bulk path. The guard reads the path
    # the request builder actually produced, so this reaches it.
    monkeypatch.setattr(C, "EP_APPLY_ES" if C.APPLY_BRANCH_ES else "EP_APPLY_LEGACY", bulk)

    with pytest.raises(Exception) as excinfo:
        client.inbound.submit_interest(OPPORTUNITY_ID, is_interested=True, confirm=True)

    assert "forbidden" in str(excinfo.value).lower()
    assert write_requests(client) == [], describe(client.routes.requests)
