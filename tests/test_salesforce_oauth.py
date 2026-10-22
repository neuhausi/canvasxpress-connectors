"""Salesforce OAuth (client credentials): the token helper and the BYO web app wiring.

No network: the token endpoint is a fake session and the Salesforce read is a fake source.
What matters here is the security shape — an OAuth app is defined only in the server's
environment, a registered source merely names it, no secret is stored or returned, and access
is deny-by-default — plus the token lifecycle (cache, expiry retry).
"""

import json

import pytest

import cx_connectors.web.byo_app as byo
from cx_connectors.sources.salesforce import SalesforceOAuthError, salesforce_oauth_token
from cx_connectors.store import Store, generate_key

SECRET = "s3cr3t-value"


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data))
        return self.response


# ---- token helper -------------------------------------------------------------------------


def test_token_helper_posts_client_credentials_to_the_my_domain_host():
    session = _Session(
        _Response(200, {"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"})
    )
    token = salesforce_oauth_token("acme.my", "cid", SECRET, session=session)
    assert token == {"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
    url, form = session.calls[0]
    assert url == "https://acme.my.salesforce.com/services/oauth2/token"
    assert form == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": SECRET}


def test_token_helper_accepts_a_full_salesforce_host():
    session = _Session(_Response(200, {"access_token": "tok"}))
    token = salesforce_oauth_token("acme.my.salesforce.com", "cid", SECRET, session=session)
    assert session.calls[0][0] == "https://acme.my.salesforce.com/services/oauth2/token"
    assert token["instance_url"] == "https://acme.my.salesforce.com"


@pytest.mark.parametrize(
    "domain",
    [
        "evil.example/x",
        "acme.my@evil.example",
        "https://acme.my",
        "a b",
        "",
        "-acme",
        "acme..my",
        "acme.my:8443",
        "acme.my/../x",
    ],
)
def test_token_helper_rejects_a_domain_that_is_not_a_plain_host(domain):
    session = _Session(_Response(200, {"access_token": "tok"}))
    with pytest.raises(ValueError):
        salesforce_oauth_token(domain, "cid", SECRET, session=session)
    assert session.calls == []  # nothing was sent anywhere


def test_token_helper_requires_both_credentials():
    session = _Session(_Response(200, {"access_token": "tok"}))
    for client_id, client_secret in (("", SECRET), ("cid", "")):
        with pytest.raises(ValueError):
            salesforce_oauth_token("acme.my", client_id, client_secret, session=session)
    assert session.calls == []


def test_token_helper_reports_salesforces_reason_without_the_secret():
    session = _Session(
        _Response(
            400, {"error": "invalid_client", "error_description": "invalid client credentials"}
        )
    )
    with pytest.raises(SalesforceOAuthError) as caught:
        salesforce_oauth_token("acme.my", "cid", SECRET, session=session)
    assert "invalid client credentials" in str(caught.value)
    assert SECRET not in str(caught.value) and "cid" not in str(caught.value)


def test_token_helper_survives_an_html_error_page():
    # A mistyped My Domain is answered with an HTML page (HTTP 420), not JSON.
    session = _Session(_Response(420, None))
    with pytest.raises(SalesforceOAuthError) as caught:
        salesforce_oauth_token("acme.my", "cid", SECRET, session=session)
    assert "HTTP 420" in str(caught.value)
    assert "may not exist" in str(caught.value) and "'acme.my'" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_token_helper_refuses_an_instance_url_outside_salesforce():
    session = _Session(
        _Response(200, {"access_token": "tok", "instance_url": "https://evil.example"})
    )
    with pytest.raises(SalesforceOAuthError):
        salesforce_oauth_token("acme.my", "cid", SECRET, session=session)
    insecure = _Session(
        _Response(200, {"access_token": "tok", "instance_url": "http://acme.my.salesforce.com"})
    )
    with pytest.raises(SalesforceOAuthError):
        salesforce_oauth_token("acme.my", "cid", SECRET, session=insecure)


# ---- web app ------------------------------------------------------------------------------


def _app(name="alice", **settings):
    return dict(
        {"domain": "acme.my", "client_id": "cid", "client_secret": SECRET, "users": [name]},
        **settings,
    )


@pytest.fixture(autouse=True)
def _clean_token_cache():
    byo._SF_TOKENS.clear()
    yield
    byo._SF_TOKENS.clear()


def _client(tmp_path, monkeypatch, apps=None, password_auth=None):
    from fastapi.testclient import TestClient

    if apps is not None:
        monkeypatch.setenv(
            "CXC_SALESFORCE_APPS", apps if isinstance(apps, str) else json.dumps(apps)
        )
    else:
        monkeypatch.delenv("CXC_SALESFORCE_APPS", raising=False)
    if password_auth is None:
        monkeypatch.delenv("CXC_SALESFORCE_PASSWORD_AUTH", raising=False)
    else:
        monkeypatch.setenv("CXC_SALESFORCE_PASSWORD_AUTH", password_auth)
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    app = byo.create_byo_app(
        store=store, session_secret="test", encryption_key=generate_key(), serve_static=False
    )
    client = TestClient(app)
    assert (
        client.post("/auth/login", json={"username": "alice", "password": "secret1"}).status_code
        == 200
    )
    return client, store


SOQL = "SELECT Name, Amount FROM Opportunity"


def _register(client, app_name="default", name="opps", **extra):
    return client.post(
        "/api/sources",
        json=dict(
            {"name": name, "kind": "salesforce", "soql": SOQL, "oauth_app": app_name}, **extra
        ),
    )


def test_register_stores_only_the_app_name(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, {"default": _app()})
    response = _register(client)
    assert response.status_code == 200, response.text
    record = store.get_source("alice", "opps")
    assert json.loads(record["conn_url"]) == {"oauth_app": "default"}
    assert SECRET not in record["conn_url"] and "cid" not in record["conn_url"]


def test_register_rejects_oauth_app_together_with_credentials(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, {"default": _app()})
    response = _register(client, auth={"username": "u", "password": "p"})
    assert response.status_code == 400
    assert store.get_source("alice", "opps") is None


@pytest.mark.parametrize(
    "apps",
    [
        {},  # nothing configured
        {"default": _app("bob")},  # listed for someone else
        {
            "default": {k: v for k, v in _app().items() if k != "users"}
        },  # no users list: deny by default
        {"default": _app(users=[])},  # empty list
    ],
)
def test_register_is_denied_unless_the_user_is_listed(tmp_path, monkeypatch, apps):
    client, store = _client(tmp_path, monkeypatch, apps)
    response = _register(client)
    assert response.status_code == 403
    assert store.get_source("alice", "opps") is None


def test_register_allows_everyone_with_a_star(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app(users="*")})
    assert _register(client).status_code == 200


def test_unknown_and_forbidden_apps_look_the_same(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"private": _app("bob")})
    unknown = _register(client, "nope")
    forbidden = _register(client, "private")
    assert unknown.status_code == forbidden.status_code == 403
    assert unknown.json()["detail"].replace("'nope'", "X") == forbidden.json()["detail"].replace(
        "'private'", "X"
    )


def test_malformed_app_config_is_reported_without_echoing_it(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, "{not json " + SECRET)
    response = _register(client)
    assert response.status_code == 500
    assert "CXC_SALESFORCE_APPS" in response.json()["detail"]
    assert SECRET not in response.text


def test_incomplete_app_is_a_configuration_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": {"domain": "acme.my", "users": "*"}})
    response = _register(client)
    assert response.status_code == 500
    assert "client_id" in response.json()["detail"]


def test_apps_endpoint_lists_names_only(tmp_path, monkeypatch):
    client, _ = _client(
        tmp_path,
        monkeypatch,
        {"zeta": _app(users="*"), "alpha": _app("alice"), "hidden": _app("bob")},
    )
    response = client.get("/api/salesforce/apps")
    assert response.status_code == 200
    assert response.json() == {"apps": ["alpha", "zeta"], "password_auth": True}
    assert (
        SECRET not in response.text
        and "cid" not in response.text
        and "acme.my" not in response.text
    )


def test_apps_endpoint_requires_login(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    client.post("/auth/logout")
    assert client.get("/api/salesforce/apps").status_code == 401


def test_password_auth_can_be_switched_off(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, {"default": _app()}, password_auth="off")
    refused = client.post(
        "/api/sources",
        json={
            "name": "legacy",
            "kind": "salesforce",
            "soql": SOQL,
            "auth": {"username": "u", "password": "p"},
        },
    )
    assert refused.status_code == 403
    assert store.get_source("alice", "legacy") is None
    assert _register(client).status_code == 200  # OAuth still works
    assert client.get("/api/salesforce/apps").json()["password_auth"] is False


def test_password_auth_still_works_by_default(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, {})
    response = client.post(
        "/api/sources",
        json={
            "name": "legacy",
            "kind": "salesforce",
            "soql": SOQL,
            "auth": {"username": "u", "password": "p"},
        },
    )
    assert response.status_code == 200
    assert '"password": "p"' in store.get_source("alice", "legacy")["conn_url"]


# ---- reading ------------------------------------------------------------------------------


class _Mint:
    """Stand-in for salesforce_oauth_token that counts calls and hands out distinct tokens."""

    def __init__(self):
        self.calls = []

    def __call__(self, domain, client_id, client_secret, **kwargs):
        self.calls.append((domain, client_id, client_secret))
        return {
            "access_token": "tok%d" % len(self.calls),
            "instance_url": "https://acme.my.salesforce.com",
        }


def _fake_source(monkeypatch, behaviour=None):
    seen = []

    class FakeSource:
        def __init__(self, soql, **kwargs):
            seen.append((soql, kwargs))

        def read(self):
            if behaviour:
                behaviour(len(seen))
            return ["Name", "Amount"], [["Deal A", 1000.0], ["Deal B", 2500.0]]

    monkeypatch.setattr(byo, "SalesforceSource", FakeSource)
    return seen


def test_read_mints_a_token_and_queries_with_it(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    assert _register(client).status_code == 200
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)
    seen = _fake_source(monkeypatch)

    response = client.get("/api/data", params={"source": "opps"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["y"]["vars"] == ["Amount"] and body["y"]["smps"] == ["Deal A", "Deal B"]
    assert mint.calls == [("acme.my", "cid", SECRET)]
    soql, kwargs = seen[0]
    assert soql == SOQL
    assert kwargs == {"session_id": "tok1", "instance_url": "https://acme.my.salesforce.com"}
    assert SECRET not in response.text


def test_read_reuses_the_token_between_requests(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)
    _fake_source(monkeypatch)
    for _ in range(3):
        assert client.get("/api/data", params={"source": "opps"}).status_code == 200
    assert len(mint.calls) == 1


def test_read_mints_again_once_the_cached_token_has_expired(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)
    _fake_source(monkeypatch)
    assert client.get("/api/data", params={"source": "opps"}).status_code == 200
    key = ("acme.my", "cid")
    byo._SF_TOKENS[key] = (0.0, byo._SF_TOKENS[key][1])  # force it past its TTL
    assert client.get("/api/data", params={"source": "opps"}).status_code == 200
    assert len(mint.calls) == 2


def test_read_retries_once_when_salesforce_says_the_session_expired(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)

    class SalesforceExpiredSession(Exception):
        pass

    def behaviour(call_number):
        if call_number == 1:
            raise SalesforceExpiredSession("Expired session")

    seen = _fake_source(monkeypatch, behaviour)
    response = client.get("/api/data", params={"source": "opps"})
    assert response.status_code == 200, response.text
    assert len(mint.calls) == 2  # a fresh token for the retry
    assert [kwargs["session_id"] for _, kwargs in seen] == ["tok1", "tok2"]


def test_read_does_not_retry_other_errors(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)

    def behaviour(call_number):
        raise RuntimeError("boom")

    _fake_source(monkeypatch, behaviour)
    response = client.get("/api/data", params={"source": "opps"})
    assert response.status_code == 502
    assert len(mint.calls) == 1


def test_read_is_forbidden_once_the_user_is_removed_from_the_app(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    mint = _Mint()
    monkeypatch.setattr(byo, "salesforce_oauth_token", mint)
    _fake_source(monkeypatch)
    assert client.get("/api/data", params={"source": "opps"}).status_code == 200

    monkeypatch.setenv(
        "CXC_SALESFORCE_APPS", json.dumps({"default": _app("bob")})
    )  # operator revokes
    response = client.get("/api/data", params={"source": "opps"})
    assert response.status_code == 403  # a real 403, not a 502
    assert len(mint.calls) == 1  # and no new token was minted


def test_a_token_failure_surfaces_as_a_bad_gateway_without_secrets(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)

    def refuse(*args, **kwargs):
        raise SalesforceOAuthError(
            "Salesforce OAuth token request failed: invalid client credentials"
        )

    monkeypatch.setattr(byo, "salesforce_oauth_token", refuse)
    response = client.get("/api/data", params={"source": "opps"})
    assert response.status_code == 502
    assert "invalid client credentials" in response.json()["detail"]
    assert SECRET not in response.text


def test_scheduled_refresh_path_enforces_the_users_list(tmp_path, monkeypatch):
    """The dashboards server calls these two functions directly for scheduled refresh."""
    client, store = _client(tmp_path, monkeypatch, {"default": _app()})
    _register(client)
    record = store.get_source("alice", "opps")
    byo.authorize_saas_source("alice", record)  # allowed: no error
    monkeypatch.setenv("CXC_SALESFORCE_APPS", json.dumps({"default": _app("bob")}))
    with pytest.raises(PermissionError):
        byo.authorize_saas_source("alice", record)
    with pytest.raises(PermissionError):
        byo._read_saas_source(record, owner="alice")
    # Non-OAuth sources are untouched by the check.
    byo.authorize_saas_source(
        "alice", {"kind": "salesforce", "conn_url": json.dumps({"username": "u"})}
    )
    byo.authorize_saas_source("alice", {"kind": "servicenow", "conn_url": "{}"})
