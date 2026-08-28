"""Tests for the ServiceNow (Table API) source.

The Table API needs requests + a real instance, so we test the reshape and request-building
via an injected fake requests.Session — the same approach as the other REST sources.
"""

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.servicenow import (
    ServiceNowSource,
    _flatten,
    servicenow_oauth_token,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Mimics requests.Session.get/post(url, ...) -> response."""
    def __init__(self, result=None, token_payload=None):
        self._result = result
        self._token_payload = token_payload

    def get(self, url, params=None, headers=None, timeout=None):
        self.url, self.params, self.headers = url, params, headers
        return _FakeResponse({"result": self._result})

    def post(self, url, data=None, headers=None, timeout=None):
        self.url, self.data = url, data
        return _FakeResponse(self._token_payload)


def test_flatten_prefers_display_value():
    assert _flatten({"display_value": "High", "value": "1"}) == "High"
    assert _flatten({"value": "abc"}) == "abc"
    assert _flatten("plain") == "plain"


def test_servicenow_source_reshapes_with_explicit_fields():
    result = [
        {"number": "INC001", "priority": "3", "category": "network"},
        {"number": "INC002", "priority": "1", "category": "hardware"},
    ]
    src = ServiceNowSource(
        instance="acme", table="incident",
        fields=["number", "priority", "category"],
        session=_FakeSession(result),
    )
    cx = to_cx(src)
    assert cx["y"]["smps"] == ["INC001", "INC002"]        # first field = sample axis
    assert cx["y"]["vars"] == ["priority"]                # numeric column
    assert cx["y"]["data"] == [[3.0, 1.0]]
    assert cx["x"]["category"] == ["network", "hardware"]


def test_servicenow_source_flattens_reference_fields_and_builds_request():
    result = [
        {"number": "INC001", "priority": {"display_value": "3 - Moderate", "value": "3"},
         "assigned_to": {"display_value": "Alice", "value": "abc123"}},
    ]
    session = _FakeSession(result)
    header, rows = ServiceNowSource(
        instance="acme", table="incident", query="active=true",
        fields=["number", "priority", "assigned_to"], limit=10,
        session=session,
    ).read()
    assert rows == [["INC001", "3 - Moderate", "Alice"]]
    assert session.url == "https://acme.service-now.com/api/now/table/incident"
    assert session.params["sysparm_query"] == "active=true"
    assert session.params["sysparm_fields"] == "number,priority,assigned_to"
    assert session.params["sysparm_limit"] == "10"


def test_oauth_token_password_grant():
    session = _FakeSession(token_payload={"access_token": "abc.def", "token_type": "Bearer"})
    token = servicenow_oauth_token(
        "acme", client_id="cid", client_secret="csecret",
        username="integration", password="pw", session=session,
    )
    assert token == "abc.def"
    assert session.url == "https://acme.service-now.com/oauth_token.do"
    assert session.data["grant_type"] == "password"
    assert session.data["username"] == "integration"


def test_oauth_token_refresh_grant():
    session = _FakeSession(token_payload={"access_token": "r.token"})
    token = servicenow_oauth_token(
        "acme", client_id="cid", client_secret="csecret",
        refresh_token="1//refresh", session=session,
    )
    assert token == "r.token"
    assert session.data["grant_type"] == "refresh_token"
    assert session.data["refresh_token"] == "1//refresh"


def test_oauth_token_missing_access_token_raises():
    session = _FakeSession(token_payload={"error": "invalid_grant"})
    with pytest.raises(ValueError):
        servicenow_oauth_token("acme", "cid", "csecret", username="u", password="p",
                               session=session)


def test_servicenow_source_sends_bearer_header():
    session = _FakeSession(result=[{"number": "INC001"}])
    ServiceNowSource(
        instance="acme", table="incident", fields=["number"],
        oauth_token="abc.def", session=session,
    ).read()
    assert session.headers["Authorization"] == "Bearer abc.def"
