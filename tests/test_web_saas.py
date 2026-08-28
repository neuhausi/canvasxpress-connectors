"""SaaS (Salesforce / ServiceNow) registration + dispatch through the BYO web app.

Registration needs no network (it only validates + stores). For the /api/data dispatch we
monkeypatch the source classes so no live API is called — the point under test is that the
app decrypts the stored blob, builds the right source, and reshapes the result.
"""

import cx_connectors.web.byo_app as byo
from cx_connectors.store import Store, generate_key


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from cx_connectors.web.byo_app import create_byo_app

    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    app = create_byo_app(store=store, session_secret="test",
                         encryption_key=generate_key(), serve_static=False)
    client = TestClient(app)
    assert client.post("/auth/login",
                       json={"username": "alice", "password": "secret1"}).status_code == 200
    return client, store


def test_register_salesforce_source_stores_kind_and_secret(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/api/sources", json={
        "name": "opps", "kind": "salesforce",
        "soql": "SELECT Name, Amount FROM Opportunity",
        "auth": {"username": "u@acme.com", "password": "pw", "security_token": "tok"},
    })
    assert r.status_code == 200, r.text
    assert "opps" in r.json()["sources"]
    rec = store.get_source("alice", "opps")
    assert rec["kind"] == "salesforce"
    assert rec["sql"] == "SELECT Name, Amount FROM Opportunity"
    assert '"password": "pw"' in rec["conn_url"]     # auth JSON stored (encrypted at rest)


def test_register_salesforce_rejects_non_select(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/sources", json={
        "name": "bad", "kind": "salesforce", "soql": "DELETE FROM Opportunity", "auth": {},
    })
    assert r.status_code == 400


def test_register_servicenow_requires_instance_and_table(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/sources", json={"name": "inc", "kind": "servicenow", "table": ""})
    assert r.status_code == 400


def test_salesforce_data_dispatch(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    client.post("/api/sources", json={
        "name": "opps", "kind": "salesforce",
        "soql": "SELECT Name, Amount FROM Opportunity", "auth": {"username": "u"},
    })

    class _FakeSf:
        def __init__(self, soql, **auth):
            assert soql.startswith("SELECT")
            assert auth.get("username") == "u"       # decrypted auth reached the source

        def read(self):
            return ["Name", "Amount"], [["Deal A", "10"], ["Deal B", "20"]]

    monkeypatch.setattr(byo, "SalesforceSource", _FakeSf)
    data = client.get("/api/data", params={"source": "opps"}).json()
    assert data["y"]["smps"] == ["Deal A", "Deal B"]
    assert data["y"]["vars"] == ["Amount"]


def test_servicenow_oauth_data_dispatch(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    client.post("/api/sources", json={
        "name": "inc", "kind": "servicenow", "instance": "acme", "table": "incident",
        "fields": "number,priority", "auth": {"type": "oauth", "client_id": "cid",
                                              "client_secret": "csec", "refresh_token": "r"},
    })

    captured = {}

    def _fake_token(instance, client_id, client_secret, **kw):
        captured["minted"] = (instance, client_id, kw.get("refresh_token"))
        return "bearer-xyz"

    class _FakeSn:
        def __init__(self, oauth_token=None, **cfg):
            captured["token"] = oauth_token
            captured["cfg"] = cfg

        def read(self):
            return ["number", "priority"], [["INC001", "3"], ["INC002", "1"]]

    monkeypatch.setattr(byo, "servicenow_oauth_token", _fake_token)
    monkeypatch.setattr(byo, "ServiceNowSource", _FakeSn)
    data = client.get("/api/data", params={"source": "inc"}).json()
    assert data["y"]["smps"] == ["INC001", "INC002"]
    assert captured["token"] == "bearer-xyz"                 # minted token was passed through
    assert captured["minted"] == ("acme", "cid", "r")
    assert captured["cfg"]["table"] == "incident"
    assert "auth" not in captured["cfg"]                     # auth stripped before building source
