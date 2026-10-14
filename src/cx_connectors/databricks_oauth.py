"""Databricks "sign in as the viewer" through a Posit Connect OAuth integration.

A normal Databricks source stores a credential (a personal access token) in its
connection URL::

    databricks://token:<PAT>@<host>?http_path=/sql/1.0/warehouses/<id>&catalog=<c>&schema=<s>

A *viewer-OAuth* source stores **no** credential. It opts in with the username
``posit-connect`` and no password::

    databricks://posit-connect@<host>?http_path=/sql/1.0/warehouses/<id>&catalog=<c>&schema=<s>

When this content runs on Posit Connect with a Databricks OAuth integration of
the *Viewer* type attached, Connect authenticates each viewer and passes a
short-lived ``Posit-Connect-User-Session-Token`` header on every request. The
public ``posit-sdk`` package exchanges that header for the **viewer's own**
Databricks access token::

    from posit.connect import Client
    Client().oauth.get_credentials(session_token, audience=<integration GUID>)["access_token"]

``Client()`` reads ``CONNECT_SERVER`` and ``CONNECT_API_KEY`` from the content's
environment, which Connect sets. Databricks accepts that token anywhere it
accepts a PAT, so the resolved URL is just the PAT form with a freshly-exchanged
token substituted for the owner's.

:func:`resolve_conn_url` is the one entry point: given a stored URL and the
current request's headers, it returns the URL to actually connect with —
unchanged for every normal source, token-substituted for an opted-in one. A
fresh token is exchanged on **every** request; nothing here stores, logs or
caches it (the per-request ``memo`` only keeps two sides of one join consistent
within a single request). ``posit-sdk`` is an optional extra (the
``[posit-connect]`` install), imported lazily.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit, urlunsplit

# Connect sets this header on every interactive request once a Viewer OAuth
# integration is attached. Starlette lowercases header names, so match lower.
SESSION_TOKEN_HEADER = "posit-connect-user-session-token"

# The username that opts a Databricks source into viewer OAuth (no password).
VIEWER_USER = "posit-connect"

# Env var naming the integration GUID, needed only when more than one OAuth
# integration is attached to the content (so the exchange knows which one).
AUDIENCE_ENV = "CXC_CONNECT_OAUTH_AUDIENCE"


class ViewerOAuthError(RuntimeError):
    """Base class for a viewer-OAuth source that cannot be connected."""


class ViewerSessionRequired(ViewerOAuthError):
    """No viewer session token on the request (not running under Connect, or a
    scheduled refresh with no viewer). The source has no credential of its own,
    so there is nothing to fall back to — the caller must surface a 403."""


class OAuthExchangeFailed(ViewerOAuthError):
    """Connect was asked for the viewer's Databricks token but could not supply
    one (``posit-sdk`` missing, no integration attached, Connect refusing). The
    message never contains the token."""


def is_viewer_oauth_url(url: Any) -> bool:
    """Whether ``url`` is a Databricks source opted into viewer OAuth.

    True only for a ``databricks`` (or ``databricks+<driver>``) URL whose
    username is ``posit-connect`` and which carries no password — i.e. stores no
    credential. A normal ``databricks://token:<PAT>@...`` URL is False, as is any
    non-Databricks URL or a SaaS JSON blob.

    :param url: A stored connection URL (any type; non-strings are False).
    :returns: True if the URL signs in as the viewer through Posit Connect.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    scheme = (parts.scheme or "").split("+", 1)[0].lower()
    return scheme == "databricks" and (parts.username or "") == VIEWER_USER and not parts.password


def session_token_from_headers(headers: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The viewer's Posit Connect session token from the request headers, or None.

    Case-insensitive: Starlette's ``request.headers`` already lowercases, but a
    plain dict may use the header's mixed-case spelling.

    :param headers: A mapping of request headers (or None).
    :returns: The non-empty token string, or None.
    """
    if not headers:
        return None
    value = None
    get = getattr(headers, "get", None)
    if callable(get):
        value = headers.get(SESSION_TOKEN_HEADER) or headers.get("Posit-Connect-User-Session-Token")
    if value is None:
        for key, val in dict(headers).items():
            if isinstance(key, str) and key.lower() == SESSION_TOKEN_HEADER:
                value = val
                break
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _default_client_factory():
    """Build a Posit Connect ``Client`` (reads CONNECT_SERVER / CONNECT_API_KEY
    from the content's environment). Raises :class:`OAuthExchangeFailed` when
    ``posit-sdk`` is not installed."""
    try:
        from posit.connect import Client
    except ImportError as exc:
        raise OAuthExchangeFailed(
            "This Databricks source signs in as the viewer through Posit Connect, "
            "which needs the posit-sdk package (pip install 'posit-sdk>=0.17')."
        ) from exc
    return Client()


def exchange_viewer_token(session_token: str, audience: Optional[str] = None,
                          client_factory: Optional[Callable[[], Any]] = None) -> str:
    """Exchange a viewer session token for the viewer's Databricks access token.

    :param session_token: The ``Posit-Connect-User-Session-Token`` header value.
    :param audience: The integration GUID (only needed when the content has more
        than one OAuth integration attached); None lets Connect pick.
    :param client_factory: Builds the Connect ``Client`` (injected in tests);
        defaults to :func:`_default_client_factory`.
    :returns: The viewer's short-lived Databricks access token.
    :raises OAuthExchangeFailed: posit-sdk missing, or Connect refused / returned
        no token. The error message never includes the token.
    """
    factory = client_factory or _default_client_factory
    try:
        client = factory()
        if audience:
            creds = client.oauth.get_credentials(session_token, audience=audience)
        else:
            creds = client.oauth.get_credentials(session_token)
        access_token = (creds or {}).get("access_token")
    except OAuthExchangeFailed:
        raise
    except Exception as exc:
        # Re-raise as our own type, carrying only the class/short reason — never
        # the session token or the exchanged access token.
        raise OAuthExchangeFailed(
            "Posit Connect could not supply the viewer's Databricks token "
            "(%s). Check that a Databricks Viewer OAuth integration is attached "
            "to this content and the viewer has logged in to it." % type(exc).__name__
        ) from exc
    if not access_token:
        raise OAuthExchangeFailed(
            "Posit Connect returned no Databricks access token for the viewer "
            "(is a Databricks Viewer OAuth integration attached, and logged in?)."
        )
    return access_token


def _substitute_token(url: str, access_token: str) -> str:
    """Return ``url`` with ``token:<access_token>`` as its credentials, preserving
    host, port, path and query (``http_path``, ``catalog``, ``schema``). The
    token is URL-encoded so it is safe in the userinfo of the URL."""
    parts = urlsplit(url)
    netloc = "token:" + quote(access_token, safe="") + "@" + (parts.hostname or "")
    if parts.port:
        netloc += ":" + str(parts.port)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_conn_url(stored_url: str, headers: Optional[Mapping[str, Any]] = None,
                     memo: Optional[dict] = None,
                     client_factory: Optional[Callable[[], Any]] = None) -> str:
    """The URL to actually connect with for a stored source.

    Unchanged for every normal source (including a Databricks PAT source). For a
    viewer-OAuth Databricks source it exchanges a **fresh** token for the current
    viewer and returns the PAT-form URL with that token substituted in.

    :param stored_url: The source's stored connection URL.
    :param headers: The current request's headers (for the viewer session token).
    :param memo: An optional per-request cache ``{stored_url: resolved}`` so the
        two sides of one join resolve to the *same* token (run_join compares the
        two URLs for equality). Never share it across requests.
    :param client_factory: Builds the Connect ``Client`` (injected in tests).
    :returns: The connection URL to open an engine with.
    :raises ViewerSessionRequired: An opted-in source with no viewer session token.
    :raises OAuthExchangeFailed: The token exchange could not be completed.
    """
    if not is_viewer_oauth_url(stored_url):
        return stored_url
    if memo is not None and stored_url in memo:
        return memo[stored_url]
    session_token = session_token_from_headers(headers)
    if not session_token:
        raise ViewerSessionRequired(
            "This Databricks source signs in as the viewer through Posit Connect; "
            "it only works for a signed-in viewer visiting this content on Connect "
            "(not outside Connect, and not on a scheduled refresh with no viewer)."
        )
    audience = os.environ.get(AUDIENCE_ENV) or None
    access_token = exchange_viewer_token(session_token, audience, client_factory)
    resolved = _substitute_token(stored_url, access_token)
    if memo is not None:
        memo[stored_url] = resolved
    return resolved
