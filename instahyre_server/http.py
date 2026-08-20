"""The HTTP layer: pacing, retries, and turning every failure into a type.

Deliberately *not* a browser. Instahyre's ``/api/v1/*`` is exempt from bot
management and answers a plain client cold, so a browser here would burn
seconds and tokens for nothing. Playwright appears in exactly one place in this
package -- :mod:`instahyre_server.auth`, for the login handshake -- and never
for fetching data.
"""

from __future__ import annotations

import json as _json
import logging
import random
import threading
import time
from typing import Any, Mapping, Optional, Sequence

import httpx

from . import constants as C
from .errors import (
    ApiError,
    AuthRequired,
    ChallengeDetected,
    InvalidFilter,
    NotFound,
    RateLimited,
    TransportError,
)

log = logging.getLogger("instahyre.http")

# Params accepted as a list -> emitted as repeated query keys.
ParamValue = Any


def _flatten_params(params: Optional[Mapping[str, ParamValue]]) -> list[tuple[str, str]]:
    """Turn a dict into httpx's repeated-key form, dropping Nones.

    ``{"skills": ["Node.js", "TypeScript"], "years": 5}`` becomes
    ``[("skills", "Node.js"), ("skills", "TypeScript"), ("years", "5")]``.
    Instahyre ORs repeated keys with dedupe.
    """
    out: list[tuple[str, str]] = []
    if not params:
        return out
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if item is None:
                    continue
                out.append((key, str(item)))
        elif isinstance(value, bool):
            out.append((key, "true" if value else "false"))
        else:
            out.append((key, str(value)))
    return out


def _looks_like_html(body: bytes, content_type: str) -> bool:
    if "html" in content_type.lower():
        return True
    head = body[:200].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


class InstahyreHTTP:
    """A paced, retrying JSON client for the Instahyre API.

    One instance owns one cookie jar, so the same object serves anonymous and
    authenticated calls -- authenticating simply populates the jar.
    """

    def __init__(
        self,
        *,
        base_url: str = C.API_BASE,
        min_interval: float = C.DEFAULT_MIN_INTERVAL_S,
        timeout: float = C.DEFAULT_TIMEOUT_S,
        max_retries: int = C.MAX_RETRIES,
        transport: Optional[httpx.BaseTransport] = None,
        cookies: Optional[httpx.Cookies] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self.request_count = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": C.USER_AGENT, "Accept": "application/json"},
            transport=transport,
            cookies=cookies,
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "InstahyreHTTP":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    # -- pacing ------------------------------------------------------------

    def _wait_turn(self) -> None:
        """Space requests out. Cheap, global, and the thing that keeps this
        usage indistinguishable from a person clicking around quickly."""
        with self._lock:
            gap = time.monotonic() - self._last_request_at
            if self._last_request_at and gap < self.min_interval:
                time.sleep(self.min_interval - gap + random.uniform(0, 0.15))
            self._last_request_at = time.monotonic()

    # -- the one request path ---------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, ParamValue]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """Perform one API call and return parsed JSON, or raise a typed error.

        Never returns ``None`` and never returns a parsed error page. A caller
        that gets a value back got a real answer.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        query = _flatten_params(params)
        headers = dict(extra_headers or {})
        if json_body is not None:
            headers.setdefault("Content-Type", "application/json")
            csrf = self._client.cookies.get("csrftoken")
            if csrf:
                headers.setdefault("X-CSRFToken", csrf)
                headers.setdefault("Referer", C.SITE_BASE + "/")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._wait_turn()
            self.request_count += 1
            try:
                response = self._client.request(
                    method,
                    url,
                    params=query or None,
                    json=json_body,
                    headers=headers or None,
                )
            except httpx.TimeoutException:
                last_error = TransportError(f"Request to {path} timed out", path=path)
                log.warning("timeout on %s (attempt %d/%d)", path, attempt, self.max_retries)
            except httpx.HTTPError as exc:
                last_error = TransportError(
                    f"Request to {path} failed: {type(exc).__name__}: {exc}", path=path
                )
                log.warning("transport error on %s: %s", path, exc)
            else:
                try:
                    return self._interpret(response, path)
                except (RateLimited, ApiError) as exc:
                    # Only these two are worth another go; 4xx are verdicts.
                    if attempt >= self.max_retries or not _is_retryable(response.status_code):
                        raise
                    last_error = exc
                    delay = _backoff_delay(attempt, response)
                    log.info("retrying %s in %.1fs after %s", path, delay, exc.kind)
                    time.sleep(delay)
                    continue
            # transport failures land here
            if attempt >= self.max_retries:
                break
            time.sleep(_backoff_delay(attempt, None))

        assert last_error is not None
        raise last_error

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    # -- response interpretation ------------------------------------------

    def _interpret(self, response: httpx.Response, path: str) -> Any:
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        body = response.content
        html = _looks_like_html(body, content_type)

        # The Cloudflare tripwire, checked before anything else: if bot
        # management is on, the status code is a lie about what happened.
        if response.headers.get("cf-mitigated"):
            raise ChallengeDetected(
                "Cloudflare bot management is now active on this path "
                f"(Cf-Mitigated: {response.headers['cf-mitigated']}). Stop and reassess; "
                "do not attempt to work around it.",
                path=path,
                status=status,
            )

        if status == 404:
            # Instahyre serves a full HTML 404 page here. Never parse it --
            # this is the case that would otherwise become a silent empty dict.
            raise NotFound(f"No such resource: {path}", path=path, status=404)

        if status == 401 or (status < 500 and not html and _json_says_logged_out(body)):
            raise AuthRequired(
                f"{path} requires a logged-in session. Call instahyre_login first.",
                path=path,
                status=status,
            )

        if status == 403:
            if html:
                raise ChallengeDetected(
                    f"{path} returned an HTML 403 -- Cloudflare is challenging this client.",
                    path=path,
                    status=403,
                )
            raise ApiError(f"{path} returned 403 Forbidden", path=path, status=403)

        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimited(
                f"{path} returned 429. Slow down.",
                path=path,
                status=429,
                retry_after=_as_float(retry_after),
            )

        if status == 400:
            raise _invalid_filter_from(body, path)

        if status >= 500:
            raise ApiError(
                f"{path} returned server error {status}", path=path, status=status
            )

        if status not in (200, 201, 202, 204):
            raise ApiError(f"{path} returned unexpected status {status}", path=path, status=status)

        if status == 204 or not body:
            return {}

        if html:
            # A JSON endpoint answering HTML with a 200 means something has
            # changed underneath us. Typed, not a parse crash.
            raise ChallengeDetected(
                f"{path} returned HTML where JSON was expected -- the API contract or the "
                "Cloudflare posture has changed.",
                path=path,
                status=status,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                f"{path} returned a body that is not valid JSON: {exc}", path=path, status=status
            ) from exc


def _json_says_logged_out(body: bytes) -> bool:
    if b"logged_out" not in body:
        return False
    try:
        parsed = _json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("logged_out") is True


def _invalid_filter_from(body: bytes, path: str) -> InvalidFilter:
    """Turn a tastypie 400 body into a typed, readable error.

    Bodies look like ``{"job_locations": ["Invalid location"]}``. Note the
    response key is not always the request key (``jobLocations`` ->
    ``job_locations``), so the field name is reported as the server gave it.
    """
    try:
        parsed = _json.loads(body)
    except ValueError:
        return InvalidFilter(
            f"{path} rejected the request (400): {body[:200].decode('utf-8', 'replace')}",
            path=path,
        )
    if isinstance(parsed, dict) and parsed:
        field, detail = next(iter(parsed.items()))
        if isinstance(detail, list):
            detail = "; ".join(str(d) for d in detail)
        return InvalidFilter(f"{field}: {detail}", field=str(field), path=path, status=400)
    return InvalidFilter(f"{path} rejected the request (400)", path=path, status=400)


def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _backoff_delay(attempt: int, response: Optional[httpx.Response]) -> float:
    if response is not None:
        retry_after = _as_float(response.headers.get("retry-after"))
        if retry_after:
            return min(retry_after, 60.0)
    return min(2.0 ** attempt, 30.0) + random.uniform(0, 0.5)


def _as_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
