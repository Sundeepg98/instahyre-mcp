"""Shared test scaffolding for the Instahyre MCP server.

Three guarantees this file exists to enforce:

1. **No test ever touches the network.** Every client is built on an
   ``httpx.MockTransport``, and an autouse fixture additionally makes the real
   ``httpx`` transports raise, so a client built without a mock fails loudly
   instead of dialling out.
2. **No test ever touches the real ``_state/`` directory.** ``INSTAHYRE_HOME``
   is redirected to a per-test tmp dir, and every ``Store`` is in-memory.
3. **No test ever really sleeps.** The ``time`` module used inside
   ``instahyre_server.http`` is swapped for a recorder, so retry backoff is
   observable and free. Clients are also built with ``min_interval=0``.

An unmocked path is an AssertionError, never an empty response -- the whole
package is built on "a failure must never look like an empty result", and the
test harness holds itself to the same rule.
"""

from __future__ import annotations

import json
import pathlib
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Union

import httpx
import pytest

from instahyre_server import http as http_module
from instahyre_server.cache import Store
from instahyre_server.client import InstahyreClient
from instahyre_server.http import InstahyreHTTP

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
API_PREFIX = "/api/v1"


# ---------------------------------------------------------------------------
# Fixture payload loading
# ---------------------------------------------------------------------------


def fixture_path(name: str) -> Path:
    if not name.endswith(".json"):
        name += ".json"
    return FIXTURE_DIR / name


def fixture_json(name: str) -> Any:
    """Load one golden fixture captured from the live API.

    ``fixture_json("search_backend_blr")`` and ``fixture_json(
    "search_backend_blr.json")`` are equivalent.
    """
    path = fixture_path(name)
    if not path.exists():
        raise AssertionError(
            "Missing fixture %s. Available: %s"
            % (path, sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
        )
    return json.loads(path.read_text(encoding="utf-8"))


# A 48 KB HTML error page, the shape Instahyre really serves for a missing job
# id. Big enough that a naive json.loads() on it is unmistakably wrong.
HTML_404_BODY = (
    "<!DOCTYPE html><html><head><title>Page not found</title></head><body>"
    "<h1>404</h1><p>The page you requested does not exist.</p>"
    + "<p>filler</p>" * 4000
    + "</body></html>"
).encode("utf-8")

HTML_CHALLENGE_BODY = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body><div id='challenge-running'>Checking your browser</div></body></html>"
)


# ---------------------------------------------------------------------------
# Route table -> MockTransport
# ---------------------------------------------------------------------------

RouteSpec = Union[dict, list, httpx.Response, Callable[[httpx.Request], Any]]


def json_response(payload: Any, status: int = 200, headers: Optional[dict] = None) -> httpx.Response:
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return httpx.Response(status, content=json.dumps(payload).encode("utf-8"), headers=merged)


def html_response(
    body: bytes = HTML_404_BODY, status: int = 404, headers: Optional[dict] = None
) -> httpx.Response:
    merged = {"content-type": "text/html; charset=utf-8"}
    merged.update(headers or {})
    return httpx.Response(status, content=body, headers=merged)


def fixture_response(name: str, status: int = 200, headers: Optional[dict] = None) -> httpx.Response:
    return json_response(fixture_json(name), status=status, headers=headers)


def _normalise(path: str) -> str:
    if path.startswith(API_PREFIX):
        path = path[len(API_PREFIX) :]
    return path or "/"


class RouteTable:
    """A path -> response map, plus a recording of every request made.

    Route keys are API-relative (``"/job_search/"``) or absolute
    (``"/api/v1/job_search/"``); both resolve to the same entry. A value may be
    a JSON payload, a ready ``httpx.Response``, or a callable taking the
    request and returning either.
    """

    def __init__(self, routes: Mapping[str, RouteSpec]) -> None:
        self.routes = {_normalise(k): v for k, v in routes.items()}
        self.requests: list[httpx.Request] = []

    # -- dispatch ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = _normalise(request.url.path)
        if path not in self.routes:
            raise AssertionError(
                "Unmocked request: %s %s (query=%r). Declared routes: %s"
                % (
                    request.method,
                    request.url.path,
                    request.url.query.decode("utf-8", "replace"),
                    sorted(self.routes),
                )
            )
        return self._build(self.routes[path], request)

    def _build(self, spec: RouteSpec, request: httpx.Request) -> httpx.Response:
        if isinstance(spec, httpx.Response):
            # Rebuilt each time so repeated calls never share stream state.
            return httpx.Response(spec.status_code, content=spec.content, headers=spec.headers)
        if callable(spec):
            return self._build(spec(request), request)
        if isinstance(spec, (dict, list)):
            return json_response(spec)
        raise AssertionError("Unusable route spec: %r" % (spec,))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- assertions helpers ------------------------------------------------

    @property
    def paths(self) -> list[str]:
        return [_normalise(r.url.path) for r in self.requests]

    def count(self, path: Optional[str] = None) -> int:
        """How many requests were made, optionally to one path only."""
        if path is None:
            return len(self.requests)
        wanted = _normalise(path)
        return sum(1 for p in self.paths if p == wanted)

    def params_for(self, path: str) -> list[list[tuple[str, str]]]:
        """The (sorted) query parameters of every request made to ``path``."""
        wanted = _normalise(path)
        return [
            sorted(r.url.params.multi_items())
            for r in self.requests
            if _normalise(r.url.path) == wanted
        ]

    def last_params(self, path: str) -> dict[str, list[str]]:
        wanted = _normalise(path)
        for request in reversed(self.requests):
            if _normalise(request.url.path) == wanted:
                out: dict[str, list[str]] = {}
                for key, value in request.url.params.multi_items():
                    out.setdefault(key, []).append(value)
                return out
        raise AssertionError("No request was made to %s" % wanted)


# The three taxonomy endpoints, pre-wired. Any test that resolves a location or
# a job function needs these, and forgetting one should not be a puzzle.
def taxonomy_routes() -> dict[str, RouteSpec]:
    from instahyre_server import constants as C

    return {
        C.EP_JOB_FUNCTION: fixture_json("job_functions.json"),
        C.EP_INDUSTRY_TYPE: fixture_json("industry_types.json"),
        C.EP_LOCATION_DATA: fixture_json("location_data.json"),
    }


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def make_http(
    route_map: Optional[Mapping[str, RouteSpec]] = None, **kwargs: Any
) -> InstahyreHTTP:
    """An ``InstahyreHTTP`` wired to a MockTransport, paced at zero.

    The ``RouteTable`` is attached as ``http.routes`` so a test can assert how
    many times the transport was actually hit.
    """
    table = RouteTable(route_map or {})
    kwargs.setdefault("min_interval", 0)
    client = InstahyreHTTP(transport=table.transport, **kwargs)
    client.routes = table  # type: ignore[attr-defined]
    return client


def make_client(
    route_map: Optional[Mapping[str, RouteSpec]] = None,
    *,
    with_taxonomy: bool = True,
    store: Optional[Store] = None,
    **kwargs: Any,
) -> InstahyreClient:
    """An ``InstahyreClient`` over a MockTransport and an in-memory ``Store``.

    Taxonomy endpoints are wired by default because almost every client path
    resolves a filter; pass ``with_taxonomy=False`` to prove a call makes no
    taxonomy request. The ``RouteTable`` is attached as ``client.routes``.
    """
    table: dict[str, RouteSpec] = taxonomy_routes() if with_taxonomy else {}
    table.update(route_map or {})
    http = make_http(table, **kwargs)
    client = InstahyreClient(http=http, store=store or Store(":memory:"))
    client.routes = http.routes  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class RecordingClock:
    """Stands in for the ``time`` module: sleeps are recorded, never taken."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class FakeClock:
    """A hand-cranked clock, for anything that compares two timestamps."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def tick(self, seconds: float = 1.0) -> float:
        self.now += seconds
        return self.now


# ---------------------------------------------------------------------------
# Autouse guards
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> RecordingClock:
    """Retry backoff becomes free and observable. Request it to read sleeps."""
    clock = RecordingClock()
    monkeypatch.setattr(http_module, "time", clock)
    return clock


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``INSTAHYRE_HOME`` -> a tmp dir, so the real ``_state/`` is never touched."""
    home = tmp_path / "instahyre_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INSTAHYRE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test reads whatever ``jobhunt.json`` happens to exist on this machine.

    ``jobcore.config`` walks up from the package looking for a shared config
    file. A developer box that has one would score every test differently from
    CI, which is the same class of bug as a venv that caches an old resolve:
    green here, red there, and nothing in the output says why. ``:none:`` is an
    explicit disable token -- an EMPTY ``JOBHUNT_CONFIG`` means *unset* and
    keeps searching, so unsetting the variable would not be enough.

    The loader also caches per path, so the cache is dropped on the way in AND
    on the way out: a test that points ``JOBHUNT_CONFIG`` at a tmp file must
    not leave that snapshot behind for the next one.
    """
    from instahyre_server import policy as policy_module

    monkeypatch.setenv("JOBHUNT_CONFIG", ":none:")
    monkeypatch.delenv("JOBHUNT_HOME", raising=False)
    monkeypatch.delenv("JOBHUNT_DISABLE", raising=False)
    policy_module.invalidate_cache()
    yield
    policy_module.invalidate_cache()


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any client built without a MockTransport fails loudly instead of dialling."""

    def _blocked(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "Test attempted real network access: %s %s" % (request.method, request.url)
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked, raising=True)


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    db = Store(":memory:")
    yield db
    db.close()


@pytest.fixture
def search_payload() -> dict:
    return fixture_json("search_backend_blr.json")


@pytest.fixture
def empty_payload() -> dict:
    return fixture_json("search_empty.json")


@pytest.fixture
def detail_direct() -> dict:
    return fixture_json("detail_direct.json")


@pytest.fixture
def detail_agency() -> dict:
    return fixture_json("detail_agency.json")


# --- the real state dir must come out of a test run untouched ----------------
#
# _state/ holds live session cookies and the job index. A test that writes there
# would be corrupting the operator's actual session. Snapshotting at COLLECTION
# time (before any test runs) and comparing later catches both creation and
# modification -- asserting mere non-existence would not, because normal
# operation creates the directory legitimately.

def _state_dir_snapshot() -> dict:
    import instahyre_server

    root = pathlib.Path(instahyre_server.__file__).resolve().parent.parent / "_state"
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.stat().st_mtime_ns
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


REAL_STATE_AT_COLLECTION = _state_dir_snapshot()


# ---------------------------------------------------------------------------
# The credential-leak walker
# ---------------------------------------------------------------------------
#
# WHY THIS IS SHARED RATHER THAN LOCAL. Every leak assertion in this suite used
# to be a substring search for one plaintext sentinel, run over a walker that
# visited ``str`` and nothing else. That combination is a check that cannot fail
# for most of the ways a credential actually escapes, and it was measured on
# 2026-08-23: of eight leak shapes carrying the WHOLE cookie value, six were
# invisible -- bytes, an object's repr, a set, base64, and the DPAPI blob in
# either spelling. The fixture in ``test_auth_lifecycle.py`` plants
# ``SECRET_ENCRYPTED_BLOB`` into every jar row precisely so that a wildcard
# select would be caught, and no assertion anywhere hunted it: the trap was set
# and the alarm was disconnected.
#
# The same class of defect was reported on the naukri server the same week -- a
# walker hunting a plaintext marker that cannot appear inside a base64url JWT,
# so every "the token never leaks" test would have passed a result echoing the
# entire credential. Fixing it once, HERE, is what makes a tool added next month
# inherit the fix instead of re-deriving it.
#
# Two independent widenings, and they are independent on purpose:
#   * the WALKER reaches values a str-only walk cannot (bytes, sets, reprs);
#   * the NEEDLES cover spellings an exact-substring search cannot (base64,
#     percent-encoding, repr-escaping).
# A leak needs only one of the two to hide, so both are always run and a failure
# says which instrument saw it -- see :func:`assert_no_credential`.


def credential_strings_in(payload: Any, _trail: str = "", _seen=None) -> list:
    """Every string ANYWHERE in ``payload``, with the path that reached it.

    Wider than a str-only walk in four measured ways, each of which hid a whole
    credential on 2026-08-23:

    * ``bytes`` are decoded (latin-1, which cannot raise and is byte-exact) --
      sqlite hands back a BLOB column as ``bytes``, and Chrome's
      ``encrypted_value`` is exactly that.
    * ``set`` and ``frozenset`` are walked. A str-only walker knows ``list``
      and ``tuple`` and stops.
    * dict KEYS are walked as well as values; a secret in a key is a secret.
    * anything else that is not a plain scalar is rendered through ``repr()``
      AND walked through its ``__dict__``. A cookie object, a
      ``RequestsCookieJar``, an exception -- each prints its value when a
      payload carrying it is formatted or logged, and each was invisible
      before.

    THE REPR IS NOT ENOUGH ON ITS OWN, which was measured here rather than
    assumed: ``logging.LogRecord`` renders as ``<LogRecord: name, 10, file,
    388, "value: %s">`` -- the format STRING, never the ``args`` the credential
    is actually in. A repr-only fallback reported CLEAN on a log record
    carrying the whole cookie. Any object that keeps its state off its repr
    does the same, so both channels are always walked and the attribute walk is
    the one that catches the hiding case.

    ``_seen`` guards against a cycle: a payload that contains itself would
    otherwise recurse forever, turning a leak check into a crash.
    """
    if _seen is None:
        _seen = set()
    marker = id(payload)
    if marker in _seen:
        return []
    out: list = []
    if isinstance(payload, dict):
        _seen.add(marker)
        for key, value in payload.items():
            here = "%s.%s" % (_trail, key)
            out.extend(credential_strings_in(key, here + " (KEY)", _seen))
            out.extend(credential_strings_in(value, here, _seen))
    elif isinstance(payload, (list, tuple)):
        _seen.add(marker)
        for index, value in enumerate(payload):
            out.extend(
                credential_strings_in(value, "%s[%d]" % (_trail, index), _seen)
            )
    elif isinstance(payload, (set, frozenset)):
        _seen.add(marker)
        # Sorted by repr so a failure message is stable between runs; set
        # iteration order is not, and a flaky failure message reads as a flaky
        # test.
        for index, value in enumerate(sorted(payload, key=repr)):
            out.extend(
                credential_strings_in(value, "%s{%d}" % (_trail, index), _seen)
            )
    elif isinstance(payload, str):
        out.append((_trail or "<root>", payload))
    elif isinstance(payload, (bytes, bytearray)):
        out.append(
            ((_trail or "<root>") + " (BYTES)", bytes(payload).decode("latin-1"))
        )
    elif payload is None or isinstance(payload, (bool, int, float)):
        # A scalar cannot carry a 40-character cookie. Rendering it would only
        # add noise to a failure message.
        pass
    else:
        _seen.add(marker)
        out.append(((_trail or "<root>") + " (REPR)", repr(payload)))
        # And the attributes the repr may be hiding. Wrapped because a property
        # can raise, and an instrument that crashes on an awkward object is an
        # instrument that gets deleted.
        try:
            attributes = vars(payload)
        except TypeError:
            attributes = None
        if isinstance(attributes, dict):
            for name, value in attributes.items():
                out.extend(
                    credential_strings_in(
                        value, "%s.%s (ATTR)" % (_trail, name), _seen
                    )
                )
    return out


def credential_needles(secret: Any) -> list:
    """Every SPELLING of ``secret`` a leak could wear, as ``[(label, needle)]``.

    A credential rarely escapes verbatim. It escapes through whatever encoded
    it: a serialised jar, an ``Authorization`` header, a query string, a repr.
    An exact-substring search for the plaintext finds none of those and reports
    CLEAN while the whole credential is on the wire.

    The spellings, and what each one is for:

    * ``exact`` -- the plaintext. Still the common case.
    * ``b64`` and ``b64url`` -- how a serialised cookie jar or a Basic header
      carries it. This is the naukri shape: a plaintext marker cannot appear
      inside base64, so the plaintext detector is structurally blind to it.
      The padding is stripped because a needle must also match a value that was
      embedded inside a longer encoded run, where the ``=`` never appears.
    * ``percent`` -- how a URL, a curl line or a form body carries it. Included
      even though the current sentinels are URL-safe and encode to themselves:
      a sentinel that is changed later must not silently drop a detector, and a
      needle identical to another is deduplicated away at zero cost.
    * ``repr`` -- how a traceback or an ``%r`` format carries it, escapes and
      all. This is the same spelling the path walker had to add for
      ``OSError.__str__``.

    Duplicates are dropped, so a spelling that happens to equal the plaintext
    costs nothing and reports under the name that found it first.
    """
    import base64 as _b64
    import urllib.parse as _urlparse

    # TWO REPRESENTATIONS, AND THEY ARE NOT INTERCHANGEABLE. ``text`` is what a
    # string-valued secret literally IS, so the ``exact`` needle matches what
    # the walker found; ``raw`` is its bytes, which is what an encoder saw.
    # Deriving ``text`` from ``raw`` instead -- a latin-1 read of a utf-8
    # encode -- silently mangles any non-ASCII secret into mojibake and hands
    # back an ``exact`` needle that matches nothing. Caught by this file's own
    # b64url control on 2026-08-23: a needle that cannot match is a detector
    # that cannot fail, which is the disease this whole walker exists to treat.
    if isinstance(secret, (bytes, bytearray)):
        raw = bytes(secret)
        text = raw.decode("latin-1")
    else:
        text = str(secret)
        raw = text.encode("utf-8")
    out: list = []
    seen: set = set()
    for label, needle in (
        ("exact", text),
        ("b64", _b64.b64encode(raw).decode("ascii").rstrip("=")),
        ("b64url", _b64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")),
        ("percent", _urlparse.quote(text, safe="")),
        ("repr", repr(text)[1:-1]),
    ):
        if needle and needle not in seen:
            seen.add(needle)
            out.append((label, needle))
    return out


def _elide_around(text: str, needle: str, margin: int = 24) -> str:
    """``text`` around ``needle``, with the needle itself replaced by a marker.

    A failure must say WHERE the leak is without reprinting the credential into
    a log that outlives the run. The surrounding characters are what identify
    the field; the value is what must not be copied.
    """
    start = text.find(needle)
    head = text[max(0, start - margin):start]
    tail = text[start + len(needle):start + len(needle) + margin]
    return "%r + <THE SECRET, %d chars> + %r" % (head, len(needle), tail)


def assert_no_credential(payload: Any, *secrets: Any, where: str = "payload") -> None:
    """Fail if any SPELLING of any secret appears ANYWHERE in ``payload``.

    Every needle is run over every string the wide walker found, and a hit
    reports the label that saw it. That label is the diagnostic: a ``b64`` or
    ``REPR`` hit with no ``exact`` hit beside it means the payload is leaking
    through a channel a plaintext search cannot see, which is the entire reason
    there is more than one spelling.

    The leak itself is elided out of the failure message -- printing it in full
    would put the credential in the CI log, which is where the operator is least
    able to rotate it.
    """
    strings = credential_strings_in(payload)
    hits: list = []
    for secret in secrets:
        for label, needle in credential_needles(secret):
            for trail, text in strings:
                if needle in text:
                    hits.append(
                        "%s -> [%s] %s = %s"
                        % (where, label, trail, _elide_around(text, needle))
                    )
    assert not hits, (
        "%d credential leak(s) reached a tool result:\n  %s"
        % (len(hits), "\n  ".join(hits))
    )
