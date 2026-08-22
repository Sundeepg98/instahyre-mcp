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
def restore_server_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Put ``server._client`` and ``server._sessions`` back after the test.

    NOT autouse, and requested by name from the two files that need it, so its
    blast radius is exactly those files.

    ``instahyre_server.server.get_client()`` assigns the process-wide client to
    a MODULE GLOBAL as a side effect, and several tools call it -- including
    ``instahyre_server_info()``, which a test may call purely to inspect its
    payload. The client then survives into later tests, and
    ``test_server.py::test_listing_tools_makes_no_request_and_builds_no_client``
    fails from a different file for a reason unrelated to what it tests. That
    is the most expensive kind of failure to diagnose, so the cost is paid at
    the source.

    Claiming both names through ``monkeypatch`` BEFORE the test body runs is
    what makes the restore correct: a ``monkeypatch.setattr`` issued later,
    inside the test, would record the ALREADY-POLLUTED value as the one to put
    back, and restore the pollution instead of removing it.
    """
    from instahyre_server import server as server_module

    monkeypatch.setattr(server_module, "_client", None)
    monkeypatch.setattr(server_module, "_sessions", None)
    yield
    created = server_module._client
    if created is not None:
        created.store.close()


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
