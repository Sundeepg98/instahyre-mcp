"""Typed errors.

The governing principle: **a failure must never look like an empty result.**
Every path that could return nothing either returns data or raises something
from this module. `[]` means "the query ran and matched zero jobs" and nothing
else.
"""

from __future__ import annotations

from typing import Any, Optional


class InstahyreError(Exception):
    """Base class. Every error raised by this package derives from it."""

    kind = "error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"error": self.kind, "message": self.message, **self.context}


class NotFound(InstahyreError):
    """The requested id does not exist.

    Raised on HTTP 404. Instahyre serves a 48 KB HTML error page for a missing
    job id, so the body is never parsed -- the status alone is the signal.
    """

    kind = "not_found"


class InvalidFilter(InstahyreError):
    """A filter value was rejected by the server (HTTP 400).

    Instahyre validates ``jobLocations``, ``companies``, ``industry_types``,
    ``company_size`` and ``years`` server-side and answers with a tastypie
    error dict. Location matching is **case-sensitive**: ``bangalore`` is a
    400, ``Bangalore`` is a 200.
    """

    kind = "invalid_filter"

    def __init__(self, message: str, field: Optional[str] = None, **context: Any) -> None:
        super().__init__(message, field=field, **context)
        self.field = field


class AuthRequired(InstahyreError):
    """The endpoint needs a logged-in session.

    Detected by HTTP 401 and/or the exact body ``{"logged_out": true}``.
    """

    kind = "auth_required"


class ChallengeDetected(InstahyreError):
    """Cloudflare bot management has become active on this path.

    This is the tripwire for the single most likely way this server breaks.
    Signals, in order of confidence: a ``Cf-Mitigated`` response header, then
    an HTML body on a JSON endpoint with a 403, then a 503 with ``cf-chl-*``
    cookies. When this fires, stop -- do not attempt to route around it.
    """

    kind = "challenge_detected"


class RateLimited(InstahyreError):
    """HTTP 429. Carries ``retry_after`` seconds when the server supplied it."""

    kind = "rate_limited"


class ApiError(InstahyreError):
    """Any other non-success response, or an unparseable body."""

    kind = "api_error"


class TransportError(InstahyreError):
    """The request never got an answer -- DNS, TLS, connect or read timeout."""

    kind = "transport_error"
