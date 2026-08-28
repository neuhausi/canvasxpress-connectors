"""ServiceNow (Table API) data source.

Reads records from a ServiceNow table through the REST **Table API**
(``GET /api/now/table/<table>``) and returns ``(header, rows)`` for ``reshape.rows_to_cx``.
The Table API is queried with an encoded ``sysparm_query``; a source only ever issues ``GET``,
so it is read-only by construction — pair it with a least-privilege ServiceNow user.

Reference fields come back as ``{"value": ..., "link": ...}`` (or a display string with
``display_value=True``); this flattens them to a single cell — the display value when present,
else the raw value — so a record is a flat row. ``fields`` fixes the column order; without it,
the first record's keys are used.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


def servicenow_oauth_token(instance: str, client_id: str, client_secret: str,
                           username: Optional[str] = None, password: Optional[str] = None,
                           refresh_token: Optional[str] = None, session=None) -> str:
    """Fetch a ServiceNow OAuth 2.0 access token from ``/oauth_token.do``.

    Supports the two grants a server-side integration uses: **password** (resource-owner,
    pass ``username``/``password``) and **refresh_token** (pass ``refresh_token``). The
    returned access token is short-lived — mint one per read rather than storing it; store
    the long-lived ``refresh_token`` (or the password credentials) encrypted instead.

    :param instance: Instance name (``"acme"``) or full host.
    :param client_id: OAuth application client id (ServiceNow → Application Registry).
    :param client_secret: OAuth application client secret.
    :param username: Resource-owner username (password grant).
    :param password: Resource-owner password (password grant).
    :param refresh_token: A refresh token (refresh_token grant) — used if no username given.
    :param session: Inject a prebuilt ``requests.Session`` (used in tests).
    :returns: The bearer access token string.
    :raises ValueError: If the token response carries no ``access_token``.
    """
    host = instance if "." in instance else instance + ".service-now.com"
    url = "https://" + host + "/oauth_token.do"
    form = {"client_id": client_id, "client_secret": client_secret}
    if refresh_token and not username:
        form["grant_type"] = "refresh_token"
        form["refresh_token"] = refresh_token
    else:
        form["grant_type"] = "password"
        form["username"] = username
        form["password"] = password

    if session is None:
        # Lazy import so the core package doesn't require requests.
        import requests

        session = requests.Session()
    response = session.post(url, data=form,
                            headers={"Accept": "application/json"}, timeout=60)
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise ValueError("ServiceNow OAuth response contained no access_token")
    return token


def _flatten(cell: Any) -> Any:
    """Reduce a Table API cell to a scalar.

    :param cell: A raw cell value — a scalar, or a reference dict with ``display_value``/``value``.
    :returns: The display value if present, else the raw value, else the cell unchanged.
    """
    if isinstance(cell, dict):
        if cell.get("display_value") not in (None, ""):
            return cell["display_value"]
        return cell.get("value", "")
    return cell


class ServiceNowSource:
    """A read-only ServiceNow Table API query, reshaped to ``(header, rows)``."""

    def __init__(self, instance: str, table: str, query: str = "",
                 fields: Optional[Sequence[str]] = None, limit: Optional[int] = None,
                 username: Optional[str] = None, password: Optional[str] = None,
                 oauth_token: Optional[str] = None, display_value: bool = True, session=None):
        """
        :param instance: The instance name (``"acme"`` for ``acme.service-now.com``) or a full host.
        :param table: The table to read (e.g. ``"incident"``).
        :param query: An encoded ServiceNow query for ``sysparm_query`` (e.g.
            ``"active=true^priority=1"``); empty means all rows.
        :param fields: Columns to request (``sysparm_fields``); also fixes the output order.
        :param limit: Max rows (``sysparm_limit``).
        :param username: Basic-auth user (ignored when ``oauth_token`` or ``session`` is used).
        :param password: Basic-auth password.
        :param oauth_token: An OAuth 2.0 bearer access token — sent as ``Authorization: Bearer``
            instead of basic auth. Mint one with :func:`servicenow_oauth_token`.
        :param display_value: Request human-readable display values for reference/choice fields.
        :param session: Inject a prebuilt ``requests.Session`` (used in tests); when ``None`` one
            is built lazily.
        """
        self.instance = instance
        self.table = table
        self.query = query
        self.fields = list(fields) if fields else None
        self.limit = limit
        self.username = username
        self.password = password
        self.oauth_token = oauth_token
        self.display_value = display_value
        self._session = session  # inject a prebuilt requests.Session (used in tests)

    def _base_url(self) -> str:
        host = self.instance
        if "." not in host:
            host = host + ".service-now.com"
        return "https://" + host + "/api/now/table/" + self.table

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Fetch the table records and return ``(header, rows)``."""
        params = {"sysparm_display_value": "true" if self.display_value else "false"}
        if self.query:
            params["sysparm_query"] = self.query
        if self.fields:
            params["sysparm_fields"] = ",".join(self.fields)
        if self.limit is not None:
            params["sysparm_limit"] = str(self.limit)

        session = self._session
        if session is None:
            # Lazy import so the core package doesn't require requests.
            import requests

            session = requests.Session()
            # Bearer token takes precedence; else basic auth when a username is given.
            if self.oauth_token is None and self.username is not None:
                session.auth = (self.username, self.password)

        headers = {"Accept": "application/json"}
        if self.oauth_token is not None:
            headers["Authorization"] = "Bearer " + self.oauth_token

        response = session.get(self._base_url(), params=params,
                               headers=headers, timeout=60)
        response.raise_for_status()
        records = response.json().get("result", [])

        header: List[str] = self.fields or (list(records[0].keys()) if records else [])
        rows: List[List[Any]] = [
            [_flatten(record.get(name, "")) for name in header] for record in records
        ]
        return header, rows
