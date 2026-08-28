"""Salesforce (SOQL) data source.

Runs a **SOQL** query through the Salesforce REST API and returns ``(header, rows)`` so
``reshape.rows_to_cx`` can turn it into a CanvasXpress object. SOQL is query-only, so — like
``SqlSource`` — a read-only guard rejects anything that isn't a ``SELECT`` as defense in depth;
pair it with a least-privilege Salesforce integration user.

Column order comes from the SOQL ``SELECT`` list (records themselves are unordered JSON), so
``SELECT Id, Name, Amount FROM Opportunity`` yields those three columns in that order.
Relationship fields (``Account.Name``) are read by walking the nested record; the ``attributes``
metadata Salesforce attaches to each record is ignored.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)
# The field list sits between the first SELECT and its FROM (SOQL has no leading CTEs).
_FIELDS_RE = re.compile(r"^\s*select\s+(.*?)\s+from\s+", re.IGNORECASE | re.DOTALL)


class ReadOnlyViolation(ValueError):
    """Raised when a statement is not a SOQL SELECT."""


def assert_read_only(soql: str) -> None:
    if not _SELECT_ONLY.match(soql or ""):
        raise ReadOnlyViolation("Only a SOQL SELECT query is allowed")


def soql_field_names(soql: str) -> List[str]:
    """The field names a SOQL ``SELECT`` lists, in order.

    Used as the ``header`` and to pull each field out of the returned records in a stable
    order. Aggregate/aliased or subquery selects fall back to record keys at read time.

    :param soql: The SOQL query.
    :returns: Field names between ``SELECT`` and ``FROM``, or ``[]`` if not parseable.
    """
    match = _FIELDS_RE.match(soql or "")
    if not match:
        return []
    fields: List[str] = []
    for raw in match.group(1).split(","):
        name = raw.strip()
        # A nested subquery like "(SELECT Id FROM Contacts)" isn't a flat column — skip it.
        if name and "(" not in name:
            fields.append(name)
    return fields


def _field_value(record: dict, dotted_name: str) -> Any:
    """Read ``dotted_name`` (e.g. ``Account.Name``) from a Salesforce record.

    :param record: One record dict from the query response.
    :param dotted_name: A field name, possibly a dotted relationship path.
    :returns: The field value, or ``""`` when a step along the path is missing/null.
    """
    value: Any = record
    for part in dotted_name.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
        if value is None:
            return ""
    return value


class SalesforceSource:
    """A read-only SOQL query against Salesforce, reshaped to ``(header, rows)``."""

    def __init__(self, soql: str, username: Optional[str] = None,
                 password: Optional[str] = None, security_token: Optional[str] = None,
                 domain: str = "login", session_id: Optional[str] = None,
                 instance_url: Optional[str] = None, client=None):
        """
        :param soql: The SOQL query to run (must be a ``SELECT``).
        :param username: Salesforce username (username/password/token auth).
        :param password: Salesforce password.
        :param security_token: The user's security token, appended to the password by the API.
        :param domain: ``"login"`` (production) or ``"test"`` (sandbox), or a My Domain host.
        :param session_id: An existing session id (session-based auth) instead of username/password.
        :param instance_url: The instance URL that pairs with ``session_id``.
        :param client: Inject a prebuilt ``simple_salesforce.Salesforce`` client (used in tests);
            when ``None`` one is built lazily from the auth arguments.
        """
        assert_read_only(soql)
        self.soql = soql
        self.username = username
        self.password = password
        self.security_token = security_token
        self.domain = domain
        self.session_id = session_id
        self.instance_url = instance_url
        self._client = client  # inject a prebuilt Salesforce client (used in tests)

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Run the SOQL query and return ``(header, rows)``."""
        client = self._client
        if client is None:
            # Lazy import so the core package doesn't require simple-salesforce.
            from simple_salesforce import Salesforce

            if self.session_id:
                client = Salesforce(session_id=self.session_id,
                                    instance_url=self.instance_url)
            else:
                client = Salesforce(username=self.username, password=self.password,
                                    security_token=self.security_token, domain=self.domain)

        # query_all follows the API's paging cursors so every matching record is returned.
        result = client.query_all(self.soql)
        records = result.get("records", []) if isinstance(result, dict) else []

        header = soql_field_names(self.soql)
        if not header:
            # Aggregate/aliased selects: fall back to record keys (minus the API metadata).
            header = [k for k in (records[0].keys() if records else []) if k != "attributes"]

        rows: List[List[Any]] = [
            [_field_value(record, name) for name in header] for record in records
        ]
        return header, rows
