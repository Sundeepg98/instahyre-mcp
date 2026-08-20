"""Instahyre MCP server -- job search over a plain HTTP API, no browser.

Instahyre's ``/api/v1/*`` answers a cold, unauthenticated, honestly-identified
client, so this server is httpx end to end. Playwright appears once, in
:mod:`instahyre_server.auth`, purely to let a human complete a Google sign-in.
"""

from .client import InstahyreClient
from .errors import (
    ApiError,
    AuthRequired,
    ChallengeDetected,
    InstahyreError,
    InvalidFilter,
    NotFound,
    RateLimited,
    TransportError,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "InstahyreClient",
    "InstahyreError",
    "NotFound",
    "InvalidFilter",
    "AuthRequired",
    "ChallengeDetected",
    "RateLimited",
    "ApiError",
    "TransportError",
]
